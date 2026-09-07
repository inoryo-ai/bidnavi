"""日付・時刻の正規化（要件定義 v2.0 FR-203）

守るべき原則:
  1. 時刻が書かれていないとき、00:00 で補完してはならない。
     DATE型に丸めると通知が1日ズレて事故る。
     → time=None + TimeSource.ABSENT で「無い」ことを明示的に持つ。
  2. 和暦の「元年」を必ず扱う（令和元年 = 2019、平成元年 = 1989）。
  3. 全角数字（令和８年３月１２日）を扱う。→ NFKC で吸収。
  4. 解釈できなければ None を返す。推測で埋めない (FR-207)。
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

from ..types import Deadline, TimeSource

# 元号 → 「和暦年 + base = 西暦年」の base
# 令和元年=2019 → 2018 + 1。平成元年=1989 → 1988 + 1。
_ERA_BASE: dict[str, int] = {
    '令和': 2018, 'R': 2018,
    '平成': 1988, 'H': 1988,
    '昭和': 1925, 'S': 1925,
    '大正': 1911, 'T': 1911,
}

# 元号の有効期間。範囲外なら誤読とみなして None を返す。
# （「平成35年」のような、実在しないが慣用で使われる表記を弾くため）
_ERA_RANGE: dict[str, tuple[dt.date, dt.date]] = {
    '令和': (dt.date(2019, 5, 1), dt.date(2099, 12, 31)),
    '平成': (dt.date(1989, 1, 8), dt.date(2019, 4, 30)),
    '昭和': (dt.date(1926, 12, 25), dt.date(1989, 1, 7)),
    '大正': (dt.date(1912, 7, 30), dt.date(1926, 12, 24)),
}
_ERA_ALIAS: dict[str, str] = {'R': '令和', 'H': '平成', 'S': '昭和', 'T': '大正'}

_SEP = r'[年\./\-]'
_SEP2 = r'[月\./\-]'

# 令和8年3月12日 / R8.3.12 / 令和元年5月1日
_ERA_DATE = re.compile(
    r'(令和|平成|昭和|大正|[RHST])\s*(元|\d{1,2})\s*' + _SEP +
    r'\s*(\d{1,2})\s*' + _SEP2 + r'\s*(\d{1,2})\s*日?'
)

# 2026年3月12日 / 2026/3/12 / 2026-03-12
_WESTERN_DATE = re.compile(
    r'(\d{4})\s*' + _SEP + r'\s*(\d{1,2})\s*' + _SEP2 + r'\s*(\d{1,2})\s*日?'
)

# 午後5時 / 17時00分 / 17:00 / 午前10時30分
_TIME = re.compile(r'(午前|午後)?\s*(\d{1,2})\s*(?:時|:)\s*(\d{1,2})?\s*分?')
_NOON = re.compile(r'正午')


def _to_ascii(value: str) -> str:
    """全角数字・全角記号を半角に寄せる。和暦パースの前提。"""
    return unicodedata.normalize('NFKC', value)


def _era_match_to_date(m: re.Match[str]) -> dt.date | None:
    era_raw, year_raw = m.group(1), m.group(2)
    month, day = int(m.group(3)), int(m.group(4))
    era = _ERA_ALIAS.get(era_raw, era_raw)
    year_num = 1 if year_raw == '元' else int(year_raw)
    if year_num < 1:
        return None
    try:
        parsed = dt.date(_ERA_BASE[era_raw] + year_num, month, day)
    except ValueError:
        return None
    lo, hi = _ERA_RANGE[era]
    return parsed if lo <= parsed <= hi else None


def _western_match_to_date(m: re.Match[str]) -> dt.date | None:
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def find_dates(value: str | None) -> list[tuple[dt.date, int]]:
    """テキスト中の全ての日付を (日付, マッチ終了位置) で返す。出現順。

    元号表記を優先して消費し、その残りを西暦として解釈する。
    こうしないと「令和8年3月12日」の "8年3月12" を西暦として二重に拾う。
    """
    if not value:
        return []
    text = _to_ascii(value)

    found: list[tuple[dt.date, int]] = []
    consumed: list[tuple[int, int]] = []

    for m in _ERA_DATE.finditer(text):
        consumed.append((m.start(), m.end()))
        parsed = _era_match_to_date(m)
        if parsed is not None:
            found.append((parsed, m.end()))

    for m in _WESTERN_DATE.finditer(text):
        if any(start <= m.start() < end for start, end in consumed):
            continue
        parsed = _western_match_to_date(m)
        if parsed is not None:
            found.append((parsed, m.end()))

    found.sort(key=lambda pair: pair[1])
    return found


def parse_japanese_date(value: str | None) -> dt.date | None:
    """和暦・西暦の混在した日本語テキストから日付を1つ取り出す（最初の1件）。

    見つからない/不正な日付なら None（推測で埋めない）。

    >>> parse_japanese_date('令和８年３月１２日')
    datetime.date(2026, 3, 12)
    >>> parse_japanese_date('令和元年5月1日')
    datetime.date(2019, 5, 1)
    >>> parse_japanese_date('平成35年1月1日') is None   # 実在しない元号年
    True
    """
    found = find_dates(value)
    return found[0][0] if found else None


def parse_japanese_time(value: str | None) -> dt.time | None:
    """テキストから時刻を1つ取り出す。無ければ None。

    「午後5時」→ 17:00、「正午」→ 12:00。
    日付部分（3月12日）を時刻と誤読しないよう、時/コロンを必須にしている。
    """
    if not value:
        return None
    text = _to_ascii(value)

    if _NOON.search(text):
        return dt.time(12, 0)

    m = _TIME.search(text)
    if not m:
        return None

    meridiem, hour_s, minute_s = m.group(1), m.group(2), m.group(3)
    hour = int(hour_s)
    minute = int(minute_s) if minute_s is not None else 0

    if meridiem == '午後':
        # 午後12時 = 正午。午後1時 = 13時。
        hour = 12 if hour == 12 else hour + 12
    elif meridiem == '午前':
        # 午前12時 = 深夜0時。
        hour = 0 if hour == 12 else hour

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dt.time(hour, minute)


def parse_deadline(value: str | None) -> Deadline | None:
    """締切テキストを Deadline に変換する。

    日付が取れなければ None。日付は取れたが時刻が無い場合は
    time=None / time_source=ABSENT とし、00:00 を補完しない。

    複数の日付が書かれている場合（「○日から○日まで」「第1回…第2回…」）は
    **最も早い日付**を採用し ambiguous=True を立てる。
    締切を遅い側に倒すと、ユーザーが締切を過ぎてから通知を受け取ることになる。
    早い側は「まだ間に合う」ので安全。取りこぼしを作らない方に倒す。

    時刻の探索は「採用した日付より後ろ」に限定する。
    そうしないと「令和8年3月12日」の数字を時刻と誤読しうる。
    """
    if not value:
        return None

    found = find_dates(value)
    if not found:
        return None

    text = _to_ascii(value)
    date_value, match_end = min(found, key=lambda pair: pair[0])
    time_value = parse_japanese_time(text[match_end:])

    return Deadline(
        date=date_value,
        time=time_value,
        time_source=TimeSource.EXPLICIT if time_value is not None else TimeSource.ABSENT,
        raw=value.strip(),
        ambiguous=len({d for d, _ in found}) > 1,
    )
