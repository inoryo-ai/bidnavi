"""3段フィルタ（要件定義 §7.1a / FR-311 キーワード未設定でも動く）

全件をLLMに通すとAPI費用が売上を上回って即死する。
段階化して、LLMに到達する件数を設計時点で固定する。

    1段目 ルールベース      100% → 5%   ほぼ0円
    2段目 案件名の類似度      5% → 1%   極小
    3段目 LLMで仕様書読解     1%のみ    ここだけ課金

**最重要の但し書き（FR-305）**
  1・2段目は「除外」ではなく「LLMに回すかどうかの振り分け」である。
  フィルタで落ちた案件も、キーワードに当たっていれば通知はされる。
  コスト削減のためにユーザーの取りこぼしを作ってはならない。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from enum import IntEnum

from ..core.types import Price
from .profile import CompanyProfile


class Stage(IntEnum):
    RULE = 1
    VECTOR = 2
    LLM = 3


@dataclass(frozen=True, slots=True)
class StageOneResult:
    """1段目の結果。

    hard_excluded:
        NGキーワードに当たった。通知もしない（ユーザーが明示的に拒否したもの）。
    keyword_hit:
        キーワードに当たった。**この場合は必ず通知する**（FR-305）。
        後段のAIが「関係なし」と判断しても覆さない。
    promote:
        2段目に進めるか。
    """
    hard_excluded: bool
    keyword_hit: bool
    matched_keywords: tuple[str, ...]
    promote: bool
    reasons: tuple[str, ...]


def _norm(text: str) -> str:
    return unicodedata.normalize('NFKC', text).lower()


def stage_one(
    *,
    title: str,
    profile: CompanyProfile,
    price: Price,
    prefecture_code: str | None,
) -> StageOneResult:
    """ルールベースの振り分け。API呼び出しは一切しない。"""
    haystack = _norm(title)
    reasons: list[str] = []

    ng_hits = [k for k in profile.ng_keywords if _norm(k) in haystack]
    if ng_hits:
        return StageOneResult(
            hard_excluded=True, keyword_hit=False, matched_keywords=(),
            promote=False, reasons=(f'NGキーワード: {"/".join(ng_hits)}',))

    hits = tuple(k for k in profile.keywords if _norm(k) in haystack)
    if hits:
        reasons.append(f'キーワード一致: {"/".join(hits)}')

    # エリア: プロフィールに指定があり、案件の都道府県が分かっていて、
    # かつ範囲外のときだけ落とす。**不明なら落とさない**（Recall優先）。
    area_ok = True
    if profile.prefectures and prefecture_code is not None:
        area_ok = prefecture_code in profile.prefectures
        if not area_ok:
            reasons.append(f'対応エリア外: {prefecture_code}')

    # 金額: 非公表・不明は in_range が True を返すので落ちない
    price_ok = price.in_range(profile.price_min, profile.price_max)
    if not price_ok:
        reasons.append(f'金額レンジ外: {price.amount}')

    # キーワード一致を通過条件にしてはいけない。
    # キーワードを設定していない利用者（業種とエリアだけ登録した人）に
    # 通知が1件も届かなくなる。キーワードは「必ず通知する」保証であって、
    # 「これに当たらなければ捨てる」フィルタではない。
    #
    # なお、キーワード未設定のプロフィールは2段目に進む件数が増えるため
    # LLM到達率が上がる。2段目はローカル計算で無料なので通信費は増えないが、
    # 3段目の予算上限（§7.1a）がその分だけ効きやすくなる。
    promote = area_ok and price_ok
    return StageOneResult(
        hard_excluded=False,
        keyword_hit=bool(hits),
        matched_keywords=hits,
        promote=promote,
        reasons=tuple(reasons) or ('条件に該当せず',),
    )


# ---------------------------------------------------------------------------
# 2段目: 案件名の類似度
# ---------------------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r'[\s　,、。・/／\-−ー()（）\[\]【】]+')


def _bigrams(text: str) -> Counter[str]:
    """文字バイグラム。日本語は分かち書きが無いのでこれが手堅い。

    ⚠️ 「本番では埋め込みに差し替えれば精度が上がる」は**実測で否定された**
    （2026-09-07 / 要件定義 v3.0 §0.3）。

    multilingual-e5-small で同じ評価をしたところ、
    関係あり群（0.840〜0.856）と無関係群（0.821〜0.855）の**分離幅は -0.001** で、
    「庁内LAN更改（関係あり・0.856）」と「庁舎警備（無関係・0.855）」が区別できなかった。
    評価30件の Recall@K もバイグラムとほぼ同じ。

    したがって**この関数の精度を上げても、2段目は足切りには使えない**。
    2段目の役割は「通過可否の判定」ではなく「表示順の並び替え」に降格した。
    コスト制御は要件 v3.0 §9.2（構造化メタデータのルール絞り込み＋LLMの2段構え）に移した。
    """
    s = _norm(text)
    s = _TOKEN_SPLIT.sub('', s)
    if len(s) < 2:
        return Counter([s]) if s else Counter()
    return Counter(s[i:i + 2] for i in range(len(s) - 1))


def similarity(a: str, b: str) -> float:
    """コサイン類似度（0.0〜1.0）。"""
    va, vb = _bigrams(a), _bigrams(b)
    if not va or not vb:
        return 0.0
    common = set(va) & set(vb)
    dot = sum(va[k] * vb[k] for k in common)
    if dot == 0:
        return 0.0
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb)


@dataclass(frozen=True, slots=True)
class StageTwoResult:
    score: float
    best_match: str | None
    promote: bool


def stage_two(
    *, title: str, profile: CompanyProfile, threshold: float = 0.25
) -> StageTwoResult:
    """案件名だけで類似度を測る。**仕様書本文は読まない**（コストを掛けない）。"""
    corpus = [*profile.categories, *profile.keywords]
    if not corpus:
        # 比較対象が無いなら判定不能。落とさず次段に進める（Recall優先）。
        return StageTwoResult(score=0.0, best_match=None, promote=True)

    scored = [(similarity(title, c), c) for c in corpus]
    best_score, best = max(scored, key=lambda pair: pair[0])
    return StageTwoResult(score=best_score, best_match=best,
                          promote=best_score >= threshold)
