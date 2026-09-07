"""3段目: LLMによる仕様書読解・適合判定（要件定義 v2.0 §7.1a / FR-303 / FR-304）

コスト制御が本体である。守るもの:

  1. **永続キャッシュ**（TTLなし）
     よくある応答キャッシュは TTL 付きのインメモリだが、それでは足りない。
     仕様書の中身は変わらないので TTL は不要で、むしろ永続化しないと
     再公告のたびに同じPDFへ課金することになる。
     キーは「仕様書本文＋プロフィール」の内容ハッシュ。

  2. **月間実行上限**（1社あたり既定50件）
     上限に達したらLLM判定を止める。ただし**通知は止めない**。
     1・2段目の結果で通知する（FR-305）。
     コスト削減のためにユーザーの取りこぼしを作ってはならない。

  3. **トークン数と円換算コストを毎回記録**
     予算管理は推測ではなく実測で回す。

LLMClient は差し替え可能にしてある。テストではスタブを使い、
本番では AnthropicClient を使う。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Protocol

#: 円換算の単価（100万トークンあたり）。実測時にオーナーの契約単価で上書きする。
#: ⚠️ 既定値は暫定。V-3 の実測はオーナーのAPIキーが必要（未実施）。
DEFAULT_INPUT_JPY_PER_MTOK = 450.0
DEFAULT_OUTPUT_JPY_PER_MTOK = 2250.0

#: LLMに渡す仕様書本文の上限文字数。長大なPDFで青天井に課金しないため。
MAX_SPEC_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class LlmVerdict:
    """LLMの判定結果。監査できるよう入力と一緒に保存する（§7.5）。"""
    score: int              # 0-100 の関連度
    reason: str             # 判定理由
    summary: str            # 3〜5行の要約（FR-304）
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False

    def cost_jpy(
        self,
        *,
        input_rate: float = DEFAULT_INPUT_JPY_PER_MTOK,
        output_rate: float = DEFAULT_OUTPUT_JPY_PER_MTOK,
    ) -> float:
        if self.cache_hit:
            return 0.0
        return (self.input_tokens * input_rate
                + self.output_tokens * output_rate) / 1_000_000


class LlmClient(Protocol):
    """LLM呼び出しの抽象。テストではスタブ、本番では Anthropic。"""

    def judge(self, *, title: str, spec_text: str, profile_text: str) -> LlmVerdict:
        ...


class BudgetExceeded(RuntimeError):
    """月間上限に達した。呼び出し側は通知を止めず1・2段目で続行する。"""


def cache_key(*, title: str, spec_text: str, profile_text: str) -> str:
    """内容ハッシュ。同じ仕様書＋同じプロフィールなら必ず同じキーになる。

    正規化してからハッシュすることで、実質同じ入力を1つのキーに寄せる。
    ただし lower() はしない（日本語では意味が無く、英字の型番を潰す）。
    """
    def norm(s: str) -> str:
        return ' '.join(unicodedata.normalize('NFKC', s).split())

    payload = '\x00'.join((norm(title), norm(spec_text), norm(profile_text)))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def month_key(when: dt.datetime) -> str:
    return f'{when:%Y-%m}'


def llm_calls_this_month(
    conn: sqlite3.Connection, company_id: str, *, now: dt.datetime
) -> int:
    """今月の課金対象呼び出し回数。キャッシュヒットは数えない。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM match_result
        WHERE company_id = ? AND stage_reached = 3 AND llm_cache_hit = 0
          AND evaluated_at >= ? AND evaluated_at < ?
        """,
        (company_id, f'{now:%Y-%m}-01T00:00:00', _next_month(now)),
    ).fetchone()
    return int(row['n'])


def _next_month(now: dt.datetime) -> str:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return f'{year:04d}-{month:02d}-01T00:00:00'


@dataclass(slots=True)
class BudgetedLlmJudge:
    """キャッシュと予算上限をかけた上でLLMを呼ぶ。"""
    conn: sqlite3.Connection
    client: LlmClient
    input_rate: float = DEFAULT_INPUT_JPY_PER_MTOK
    output_rate: float = DEFAULT_OUTPUT_JPY_PER_MTOK

    def judge(
        self,
        *,
        company_id: str,
        monthly_cap: int,
        title: str,
        spec_text: str,
        profile_text: str,
        now: dt.datetime,
    ) -> LlmVerdict:
        """判定する。

        :raises BudgetExceeded: 今月の上限に達していて、かつキャッシュも無い場合
        """
        trimmed = spec_text[:MAX_SPEC_CHARS]
        key = cache_key(title=title, spec_text=trimmed, profile_text=profile_text)

        cached = self.conn.execute(
            'SELECT * FROM llm_cache WHERE cache_key = ?', (key,)
        ).fetchone()
        if cached is not None:
            self.conn.execute(
                'UPDATE llm_cache SET hit_count = hit_count + 1 WHERE cache_key = ?',
                (key,))
            self.conn.commit()
            payload = json.loads(cached['response_json'])
            return LlmVerdict(
                score=payload['score'], reason=payload['reason'],
                summary=payload['summary'],
                input_tokens=int(cached['input_tokens']),
                output_tokens=int(cached['output_tokens']),
                cache_hit=True)

        used = llm_calls_this_month(self.conn, company_id, now=now)
        if used >= monthly_cap:
            raise BudgetExceeded(
                f'{company_id} の今月のLLM実行が上限に達しました '
                f'({used}/{monthly_cap})。通知は1・2段目の結果で継続します。')

        verdict = self.client.judge(
            title=title, spec_text=trimmed, profile_text=profile_text)

        self.conn.execute(
            """
            INSERT OR REPLACE INTO llm_cache
                (cache_key, created_at, hit_count, response_json,
                 input_tokens, output_tokens)
            VALUES (?, ?, 0, ?, ?, ?)
            """,
            (key, now.isoformat(),
             json.dumps({'score': verdict.score, 'reason': verdict.reason,
                         'summary': verdict.summary}, ensure_ascii=False),
             verdict.input_tokens, verdict.output_tokens),
        )
        self.conn.commit()
        return verdict


# ---------------------------------------------------------------------------
# 実装
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StubLlmClient:
    """テスト・見積り用。APIを呼ばずに決定的な結果を返す。

    トークン数は概算（日本語は概ね1文字≒1トークン弱）で見積もる。
    実測はオーナーのAPIキーが必要（V-3 未実施）。
    """
    score: int = 80
    calls: int = 0

    def judge(self, *, title: str, spec_text: str, profile_text: str) -> LlmVerdict:
        self.calls += 1
        input_tokens = int((len(title) + len(spec_text) + len(profile_text)) * 0.9)
        return LlmVerdict(
            score=self.score,
            reason=f'スタブ判定（{title[:20]}）',
            summary='スタブ要約',
            input_tokens=input_tokens,
            output_tokens=200,
        )


PROMPT_TEMPLATE = """\
あなたは公共調達の入札案件を、事業者に関係があるかどうか判定する担当者です。

# 事業者のプロフィール
{profile_text}

# 案件名
{title}

# 仕様書（抜粋）
{spec_text}

# 指示
1. この案件が上記事業者に関係あるかを 0〜100 の関連度で判定してください。
2. 判定理由を1〜2文で述べてください。
3. 「何を・いつまでに・どんな条件で」が分かる3〜5行の要約を作ってください。

**仕様書に書かれていないことを書いてはいけません。**
書かれていない条件を推測して補うと、事業者が誤った判断をします。
不明な項目は「記載なし」としてください。

次のJSONだけを返してください:
{{"score": <0-100>, "reason": "<理由>", "summary": "<要約>"}}
"""


@dataclass(slots=True)
class AnthropicLlmClient:
    """本番用。ANTHROPIC_API_KEY が必要。

    ⚠️ 技術検証フェーズでは未実行（APIキー未設定のため）。
    V-3 のコスト実測にはオーナーのキーが要る。
    """
    model: str = 'claude-sonnet-5'
    max_tokens: int = 1024

    def judge(self, *, title: str, spec_text: str, profile_text: str) -> LlmVerdict:
        import anthropic

        client = anthropic.Anthropic()
        prompt = PROMPT_TEMPLATE.format(
            profile_text=profile_text, title=title, spec_text=spec_text)
        message = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{'role': 'user', 'content': prompt}],
        )
        text = ''.join(
            block.text for block in message.content
            if getattr(block, 'type', None) == 'text'
        )
        payload = json.loads(_extract_json(text))
        return LlmVerdict(
            score=int(payload['score']),
            reason=str(payload['reason']),
            summary=str(payload['summary']),
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )


def _extract_json(text: str) -> str:
    start, end = text.find('{'), text.rfind('}')
    if start == -1 or end == -1:
        raise ValueError(f'JSONが見つかりません: {text[:200]}')
    return text[start:end + 1]
