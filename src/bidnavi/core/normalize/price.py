"""金額の正規化（要件定義 v2.0 §7.3-3）

守るべき原則:
  「非公表」を 0円 として取り込むと、金額レンジ絞り込みに混入して
  ユーザーの取りこぼしになる。amount=None + undisclosed=True で表現する。
"""
from __future__ import annotations

import re
import unicodedata

from ..types import Price, TaxBasis

# 「金額が公表されていない」ことを示す表記
_UNDISCLOSED_MARKERS: tuple[str, ...] = (
    '非公表', '非公開', '公表しない', '公表せず', '未定', '不明',
    '事後公表', '予定価格を公表しない', '入札後に公表', '開札後に公表',
    '落札後に公表',
)

# 値が空であることを示す記号（単独で現れた場合のみ）
_EMPTY_MARKERS: frozenset[str] = frozenset({
    '', '-', '−', '―', 'ー', '‐', '－', '─', '/', '−−', '--', 'なし', '該当なし',
})

_TAX_EXCLUDED = re.compile(r'税抜|税別|消費税.{0,4}(?:抜|除|含まない|含まず)')
_TAX_INCLUDED = re.compile(r'税込|消費税.{0,4}含')

# 単位。自治体の予算表では「千円」「百万円」単位が普通に使われる。
# ここを取りこぼすと金額が 1/1000 になり、レンジ絞り込みが壊滅する。
_HYAKUMAN = re.compile(r'(\d+)\s*百万')
_OKU = re.compile(r'(\d+)\s*億')
_MAN = re.compile(r'(\d+)\s*万')
_SEN = re.compile(r'(\d+)\s*千')
_YEN = re.compile(r'(\d+)\s*円')
_BARE_NUMBER = re.compile(r'\d+')

_UNKNOWN = Price(amount=None, undisclosed=False, tax_basis=TaxBasis.UNKNOWN, raw='')


def _detect_tax_basis(text: str) -> TaxBasis:
    # 「税込」と「税抜」が両方書かれている場合は判定不能とする（推測しない）
    included = bool(_TAX_INCLUDED.search(text))
    excluded = bool(_TAX_EXCLUDED.search(text))
    if included and excluded:
        return TaxBasis.UNKNOWN
    if included:
        return TaxBasis.INCLUDED
    if excluded:
        return TaxBasis.EXCLUDED
    return TaxBasis.UNKNOWN


#: 大きい単位から順に消費する。百万は「万」より先に見ないと 100 万 と誤読する。
_UNITS: tuple[tuple[re.Pattern[str], int], ...] = (
    (_OKU, 100_000_000),
    (_HYAKUMAN, 1_000_000),
    (_MAN, 10_000),
    (_SEN, 1_000),
)


def _parse_amount(text: str) -> int | None:
    """「1億2,000万円」「123万円」「1,234千円」「1,234,567円」を整数に変換する。

    「1,234千円」= 1,234,000円。単位付き表記を取りこぼすと桁が3つずれる。
    """
    rest = text.replace(',', '').replace('，', '')

    total = 0
    matched = False

    for pattern, multiplier in _UNITS:
        m = pattern.search(rest)
        if m:
            total += int(m.group(1)) * multiplier
            rest = rest[m.end():]
            matched = True

    m = _YEN.search(rest)
    if m:
        total += int(m.group(1))
        return total

    if matched:
        return total

    # 単位も「円」も無いが数字だけある場合（表のセルなど）
    m = _BARE_NUMBER.search(rest)
    return int(m.group(0)) if m else None


def parse_price(value: str | None) -> Price:
    """金額テキストを Price に変換する。

    解釈できない場合は amount=None / undisclosed=False（＝「不明」）を返す。
    「非公表」（意図的に公表されていない）と「不明」（読めなかった）は
    別の状態なので、undisclosed フラグで区別する。

    >>> parse_price('非公表').undisclosed
    True
    >>> parse_price('金1,234,567円（税抜）').amount
    1234567
    >>> parse_price('1億2,000万円').amount
    120000000
    """
    if value is None:
        return _UNKNOWN
    raw = value.strip()
    text = unicodedata.normalize('NFKC', raw)

    if text.strip() in _EMPTY_MARKERS:
        return Price(amount=None, undisclosed=False, tax_basis=TaxBasis.UNKNOWN, raw=raw)

    if any(marker in text for marker in _UNDISCLOSED_MARKERS):
        return Price(amount=None, undisclosed=True,
                     tax_basis=_detect_tax_basis(text), raw=raw)

    amount = _parse_amount(text)
    return Price(amount=amount, undisclosed=False,
                 tax_basis=_detect_tax_basis(text), raw=raw)
