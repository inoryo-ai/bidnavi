"""テキスト正規化（要件定義 v2.0 §7.7）

natural_key の安定性はこのルールの精度で決まる。
ここを後から変えると全レコードの natural_key が変わり、DB再構築になる。
→ 変更する場合は必ずマイグレーションとセットで行うこと。

社内の既存実装にある正規化は NFKC + 小文字化 + 空白畳み込みのみで、
案件名の名寄せには不足している（年度表記・括弧・定型句・枝番の扱いが無い）。
思想は流用するが、実装はここで作り直している。
"""
from __future__ import annotations

import re
import unicodedata

# 括弧記号（中身は残し、記号だけ落とす）
_BRACKETS = re.compile(r'[【】（）()「」『』［］\[\]〔〕｛｝{}〈〉《》]')

# 年度表記。「令和8年度」「R8年度」「2026年度」など。元号は「元年度」も拾う
_FISCAL_YEAR = re.compile(r'(?:令和|平成|昭和|大正|[RHST])(?:元|\d{1,2})年度|\d{4}年度')

# 連続する区切り記号
_REPEATED_SYMBOLS = re.compile(r'([・\-−ー~〜_])\1+')

# 全種の空白
_WHITESPACE = re.compile(r'[\s　]+')

# 先頭・末尾に残った区切り記号
_EDGE_SYMBOLS_HEAD = re.compile(r'^[・\-−ー~〜_,、。:：;；]+')
_EDGE_SYMBOLS_TAIL = re.compile(r'[・\-−ー~〜_,、。:：;；]+$')

# 定型の接頭辞。長いものから順に評価する（前方一致でのみ除去）
_PREFIXES: tuple[str, ...] = (
    '条件付一般競争入札公告',
    '一般競争入札公告',
    '指名競争入札公告',
    '条件付一般競争入札',
    '公募型プロポーザル',
    '一般競争入札',
    '指名競争入札',
    '企画競争公告',
    '入札公告',
    '入札情報',
    '公告',
)

# 定型の接尾辞。長いものから順に評価する（後方一致でのみ除去）
_SUFFIXES: tuple[str, ...] = (
    'の実施について',
    'の入札について',
    'の公告について',
    'に係る公告',
    'について',
    'の件',
)


def normalize_text(value: str) -> str:
    """汎用のテキスト正規化。

    NFKC で全角英数字を半角に、半角カナを全角に揃え、空白を1つに畳む。
    案件名以外（機関名・場所など）にも使う。
    """
    return _WHITESPACE.sub(' ', unicodedata.normalize('NFKC', value)).strip()


def _strip_affixes(value: str, affixes: tuple[str, ...], *, prefix: bool) -> str:
    """前方/後方一致する定型句を、変化しなくなるまで繰り返し除去する。

    「【入札公告】一般競争入札 ○○業務」のような二重の定型を落とすため。
    完全一致で消え去るのを防ぐため、残りが空になる場合は除去しない。
    """
    result = value
    changed = True
    while changed:
        changed = False
        for affix in affixes:
            hit = result.startswith(affix) if prefix else result.endswith(affix)
            if hit and len(result) > len(affix):
                result = result[len(affix):] if prefix else result[:-len(affix)]
                changed = True
                break
    return result


def normalize_title(value: str) -> str:
    """案件名の正規化（§7.7）。

    重要な非対称性:
      - 年度表記（令和8年度）は「除去する」
        … 同一案件が年度表記の有無で別扱いになるのを防ぐ
      - 枝番（その1・第2回・第2次）は「残す」
        … これは別案件なので、寄せてはいけない

    >>> normalize_title('【入札公告】令和８年度　○○業務委託　その１について')
    '○○業務委託その1'
    """
    base = _WHITESPACE.sub('', _BRACKETS.sub('', unicodedata.normalize('NFKC', value)))

    s = _FISCAL_YEAR.sub('', base)                  # 年度表記を落とす
    s = _strip_affixes(s, _PREFIXES, prefix=True)   # 定型句を落とす
    s = _strip_affixes(s, _SUFFIXES, prefix=False)
    s = _REPEATED_SYMBOLS.sub(r'\1', s)             # 連続記号を1文字に畳む
    s = _EDGE_SYMBOLS_HEAD.sub('', s)               # 端の区切り記号を落とす
    s = _EDGE_SYMBOLS_TAIL.sub('', s)

    # 除去しきって空になった場合は、除去前の文字列に戻す。
    # 空文字を返すと natural_key が「同一機関・同一日付の全案件」で衝突する。
    # これは FR-106 の誤結合そのものなので、絶対に空を返してはならない。
    return s or base or unicodedata.normalize('NFKC', value).strip()
