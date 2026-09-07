"""マッチングエンジン（要件定義 v2.0 §7.1a / FR-303 / FR-305）

3段フィルタを束ねる。この層の責務は2つだけ。

  A. LLMに到達する件数を絞る（コスト）
  B. **どの段で落ちても、キーワードに当たった案件は必ず通知する**（Recall）

Bが本体である。Aだけを実装するとコストは下がるが、
「AIが関係ないと判断したから通知しない」設計になり、
§7.3-1 の最悪の取りこぼしが起きる。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

from ..core.types import Price
from .llm import BudgetedLlmJudge, BudgetExceeded, LlmVerdict
from .profile import CompanyProfile
from .stages import Stage, stage_one, stage_two

#: LLM判定でこのスコア以上なら通知する
LLM_NOTIFY_THRESHOLD = 60

#: 2段目の閾値。仕様書が読めるかどうかで変える。
#:
#: 3段目の存在理由は「案件名だけでは判定できない案件を、仕様書を読んで拾う」こと
#: （§7.2-2）。その3段目への到達可否を**案件名の類似度で決めてはいけない**。
#: 案件名で判定できるなら3段目は要らないので、循環している。
#:
#: 実測（Loop 5）: 文字バイグラム類似度では「ネットワーク機器保守」「庁内LAN更改」
#: 「統合型GIS運用支援」が「業務システム開発」に対して 0.10〜0.12 しか出ない。
#: IT領域として関連があることを、字面の重なりでは捉えられない。
#:
#: 追試（要件 v3.0 §0.3）: 埋め込み（multilingual-e5-small）でも解決しなかった。
#: 関係あり群と無関係群の分離幅は -0.001 で分布が重なり、閾値が引けない。
#: → **2段目は原理的に足切りに使えない**。閾値を緩めているのは
#:   「良い閾値が見つかるまでの暫定」ではなく「足切りを放棄した」ということ。
#:   コスト制御は §9.2（構造化メタデータ＋LLMの2段構え）が担う。
VECTOR_THRESHOLD_WITH_SPEC = 0.08
#: 仕様書が無い場合は案件名しか手掛かりが無いので、従来どおりの閾値を使う。
VECTOR_THRESHOLD_TITLE_ONLY = 0.25


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    natural_key: str
    company_id: str
    stage_reached: Stage
    score: int
    reason: str
    matched_by: str          # 'rule' | 'vector' | 'llm' | 'none'
    should_notify: bool
    llm: LlmVerdict | None = None
    cost_jpy: float = 0.0
    #: LLMが「関係なし」と判定したが、ルール一致のため通知を維持したケース
    llm_overridden: bool = False


def evaluate(
    *,
    conn: sqlite3.Connection,
    natural_key: str,
    title: str,
    profile: CompanyProfile,
    price: Price,
    prefecture_code: str | None,
    spec_text: str | None,
    judge: BudgetedLlmJudge | None,
    now: dt.datetime,
) -> MatchOutcome:
    """1案件 × 1社を判定する。"""

    # ---- 1段目: ルール -----------------------------------------------------
    one = stage_one(title=title, profile=profile, price=price,
                    prefecture_code=prefecture_code)

    if one.hard_excluded:
        # NGキーワードはユーザーが明示的に拒否したもの。これだけは通知しない。
        return _record(conn, MatchOutcome(
            natural_key, profile.company_id, Stage.RULE, 0,
            '; '.join(one.reasons), 'none', should_notify=False), now)

    if not one.promote:
        # 2段目に進めないが、キーワードに当たっていれば通知する（FR-305）。
        return _record(conn, MatchOutcome(
            natural_key, profile.company_id, Stage.RULE,
            50 if one.keyword_hit else 0,
            '; '.join(one.reasons), 'rule' if one.keyword_hit else 'none',
            should_notify=one.keyword_hit), now)

    # ---- 2段目: 案件名の類似度（仕様書は読まない） ---------------------------
    can_read_spec = spec_text is not None and judge is not None
    two = stage_two(
        title=title, profile=profile,
        threshold=(VECTOR_THRESHOLD_WITH_SPEC if can_read_spec
                   else VECTOR_THRESHOLD_TITLE_ONLY))
    vector_reason = (f'案件名類似度 {two.score:.2f}'
                     + (f'（{two.best_match}）' if two.best_match else ''))

    if not two.promote or spec_text is None or judge is None:
        # LLMに回さない。ただしルール一致は通知を維持する。
        note = vector_reason if two.promote else f'{vector_reason} < 閾値'
        if spec_text is None:
            note += ' / 仕様書テキストなし'
        return _record(conn, MatchOutcome(
            natural_key, profile.company_id, Stage.VECTOR,
            _rule_score(one, two), f'{"; ".join(one.reasons)}; {note}',
            'rule' if one.keyword_hit else 'vector',
            should_notify=one.keyword_hit or two.promote), now)

    # ---- 3段目: LLM --------------------------------------------------------
    profile_text = _profile_text(profile)
    try:
        verdict = judge.judge(
            company_id=profile.company_id, monthly_cap=profile.llm_monthly_cap,
            title=title, spec_text=spec_text, profile_text=profile_text, now=now)
    except BudgetExceeded as exc:
        # 予算上限。**通知は止めない。** 1・2段目の結果で続行する。
        return _record(conn, MatchOutcome(
            natural_key, profile.company_id, Stage.VECTOR,
            _rule_score(one, two),
            f'{"; ".join(one.reasons)}; {vector_reason}; LLM未実行（{exc}）',
            'rule' if one.keyword_hit else 'vector',
            should_notify=one.keyword_hit or two.promote), now)

    llm_says_notify = verdict.score >= LLM_NOTIFY_THRESHOLD
    # FR-305: AIが「関係なし」と言っても、キーワード一致なら通知する。
    should_notify = llm_says_notify or one.keyword_hit
    overridden = one.keyword_hit and not llm_says_notify

    reason = f'{verdict.reason}（関連度{verdict.score}）'
    if overridden:
        reason += (f' ※AIは関連なしと判定したが、キーワード '
                   f'{"/".join(one.matched_keywords)} に一致するため通知を維持')

    return _record(conn, MatchOutcome(
        natural_key, profile.company_id, Stage.LLM,
        max(verdict.score, 50 if one.keyword_hit else 0),
        reason, 'llm', should_notify=should_notify, llm=verdict,
        cost_jpy=verdict.cost_jpy(input_rate=judge.input_rate,
                                  output_rate=judge.output_rate),
        llm_overridden=overridden), now)


def _rule_score(one: object, two: object) -> int:
    keyword_hit = getattr(one, 'keyword_hit', False)
    score = getattr(two, 'score', 0.0)
    return max(50 if keyword_hit else 0, int(score * 100))


def _profile_text(profile: CompanyProfile) -> str:
    parts = [
        f'業種・営業品目: {"、".join(profile.categories) or "指定なし"}',
        f'対応エリア: {"、".join(profile.prefectures) or "指定なし"}',
        f'保有資格: {"、".join(profile.qualifications) or "指定なし"}',
        f'注目キーワード: {"、".join(profile.keywords) or "指定なし"}',
    ]
    if profile.price_min is not None or profile.price_max is not None:
        parts.append(f'対応可能金額: {profile.price_min}〜{profile.price_max}')
    return '\n'.join(parts)


def _record(
    conn: sqlite3.Connection, outcome: MatchOutcome, now: dt.datetime
) -> MatchOutcome:
    """§7.1a: どの段まで到達したか・いくらかかったかを必ず記録する。"""
    conn.execute(
        """
        INSERT OR REPLACE INTO match_result
            (natural_key, company_id, evaluated_at, stage_reached, score, reason,
             matched_by, should_notify, llm_input_tokens, llm_output_tokens,
             llm_cost_jpy, llm_cache_hit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (outcome.natural_key, outcome.company_id, now.isoformat(),
         int(outcome.stage_reached), outcome.score, outcome.reason,
         outcome.matched_by, int(outcome.should_notify),
         outcome.llm.input_tokens if outcome.llm else 0,
         outcome.llm.output_tokens if outcome.llm else 0,
         outcome.cost_jpy,
         int(bool(outcome.llm and outcome.llm.cache_hit))),
    )
    conn.commit()
    return outcome


@dataclass(frozen=True, slots=True)
class FunnelStats:
    """§7.1a の通過率が設計目標どおりかを実測する。"""
    total: int
    reached_stage2: int
    reached_stage3: int
    llm_calls_billed: int
    cache_hits: int
    total_cost_jpy: float
    notified: int

    @property
    def stage3_ratio(self) -> float:
        return self.reached_stage3 / self.total if self.total else 0.0


def funnel_stats(conn: sqlite3.Connection, company_id: str) -> FunnelStats:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(stage_reached >= 2) AS s2,
               SUM(stage_reached >= 3) AS s3,
               SUM(stage_reached = 3 AND llm_cache_hit = 0) AS billed,
               SUM(llm_cache_hit) AS hits,
               COALESCE(SUM(llm_cost_jpy), 0) AS cost,
               SUM(should_notify) AS notified
        FROM match_result WHERE company_id = ?
        """,
        (company_id,),
    ).fetchone()
    return FunnelStats(
        total=int(row['total']),
        reached_stage2=int(row['s2'] or 0),
        reached_stage3=int(row['s3'] or 0),
        llm_calls_billed=int(row['billed'] or 0),
        cache_hits=int(row['hits'] or 0),
        total_cost_jpy=float(row['cost'] or 0.0),
        notified=int(row['notified'] or 0),
    )
