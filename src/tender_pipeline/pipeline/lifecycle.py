"""案件のライフサイクル判定（要件定義 FR-401 / FR-402 締切当日の可視性 / FR-405 消えた案件の検出 / AC-07 / §7.3-2）

時刻が不明な締切をどちらに倒すかは、用途によって逆になる。
どちらも「ユーザーを取りこぼさない側」に倒すのが原則。

    通知（notify_at）      … 早い側（当日 9:00）に倒す
                             → まだ間に合ううちに知らせる
    締切判定（is_closed）  … 遅い側（当日 23:59）に倒す
                             → 締切当日の案件を昼に消してしまわない

この非対称性は意図的なものであり、統一してはいけない。
片方に揃えると、必ずどちらかで案件を失う。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

#: 案件が公告一覧から消えたとみなすまでの日数
DEFAULT_DISAPPEARED_DAYS = 3


def deadline_expires_at(
    deadline_date: dt.date, deadline_time: dt.time | None
) -> dt.datetime:
    """締切の「これを過ぎたら終了」の瞬間を返す。

    時刻が不明なときは当日の終わり（23:59）とする。
    00:00 に丸めると、締切当日の午前中に案件が一覧から消える（§7.3-2）。
    """
    if deadline_time is not None:
        return dt.datetime.combine(deadline_date, deadline_time)
    return dt.datetime.combine(deadline_date, dt.time(23, 59, 59))


def is_closed(
    deadline_date: dt.date | None,
    deadline_time: dt.time | None,
    *,
    now: dt.datetime,
) -> bool:
    """締切を過ぎているか。

    締切が読めなかった案件（date=None）は False を返す。
    「締切不明だから消す」は取りこぼしなので、消さずに残して人に見せる。
    """
    if deadline_date is None:
        return False
    return now > deadline_expires_at(deadline_date, deadline_time)


def _parse_date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _parse_time(value: str | None) -> dt.time | None:
    return dt.time.fromisoformat(value) if value else None


def close_expired(conn: sqlite3.Connection, *, now: dt.datetime) -> int:
    """締切を過ぎた案件を closed にする。件数を返す。

    SQL 側で日付比較せず Python 側で判定しているのは、
    「時刻 NULL は当日23:59とみなす」規則を一箇所に閉じ込めるため。
    SQL に散らすと、いずれ 00:00 で比較する実装が混入する。
    """
    rows = conn.execute(
        """
        SELECT natural_key, bid_deadline_date, bid_deadline_time
        FROM tender
        WHERE status = 'open' AND bid_deadline_date IS NOT NULL
        """
    ).fetchall()

    expired = [
        r['natural_key'] for r in rows
        if is_closed(_parse_date(r['bid_deadline_date']),
                     _parse_time(r['bid_deadline_time']), now=now)
    ]
    if expired:
        conn.executemany(
            "UPDATE tender SET status = 'closed' WHERE natural_key = ?",
            [(k,) for k in expired],
        )
        conn.commit()
    return len(expired)


@dataclass(frozen=True, slots=True)
class DisappearedTender:
    natural_key: str
    title: str
    last_seen_at: str
    days_missing: int


def find_disappeared(
    conn: sqlite3.Connection,
    organization_id: str,
    *,
    now: dt.datetime,
    threshold_days: int = DEFAULT_DISAPPEARED_DAYS,
) -> list[DisappearedTender]:
    """締切前なのに公告一覧から見えなくなった案件を探す。

    「掲載が取り下げられた」のか「クローラが取れなくなった」のかは
    ここでは区別できない。区別できないからこそ、人に見せる必要がある。
    黙って closed にすると、クローラの故障が案件の終了として記録される。
    """
    rows = conn.execute(
        """
        SELECT natural_key, title, last_seen_at, bid_deadline_date, bid_deadline_time
        FROM tender
        WHERE organization_id = ? AND status = 'open'
        """,
        (organization_id,),
    ).fetchall()

    result: list[DisappearedTender] = []
    for r in rows:
        if is_closed(_parse_date(r['bid_deadline_date']),
                     _parse_time(r['bid_deadline_time']), now=now):
            continue  # 締切済みなら見えなくなって当然
        missing = (now - dt.datetime.fromisoformat(r['last_seen_at'])).days
        if missing >= threshold_days:
            result.append(DisappearedTender(
                natural_key=r['natural_key'], title=r['title'],
                last_seen_at=r['last_seen_at'], days_missing=missing))

    result.sort(key=lambda d: d.days_missing, reverse=True)
    return result
