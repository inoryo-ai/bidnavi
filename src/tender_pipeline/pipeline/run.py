"""クロール実行のライフサイクル（要件定義 v2.0 FR-111 / FR-105）

クローラは必ず途中で失敗する。「昨日の分だけやり直す」ができない設計だと
毎回全件やり直しになるため、(機関, 対象日) 単位で再実行できるようにする。

例外の扱い（方針: 例外を握りつぶさない）:
  CrawlSession は例外を **記録してから再送出する**。
  ログに残すことと、呼び出し側に失敗を伝えることは両立させる。
  ここで握りつぶすと、上位のバッチが「全機関成功」と報告してしまう。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from types import TracebackType

from ..core.types import CrawlStatus


@dataclass(slots=True)
class CrawlSession:
    """1回のクロール実行を表す。with 文で使う。

    with CrawlSession(conn, 'jp-x', target_date, now) as s:
        s.record(fetched=10, inserted=3, updated=1)
    """
    conn: sqlite3.Connection
    organization_id: str
    target_date: dt.date
    now: dt.datetime
    run_id: int = field(init=False, default=0)
    attempt: int = field(init=False, default=1)
    _fetched: int = field(init=False, default=0)
    _inserted: int = field(init=False, default=0)
    _updated: int = field(init=False, default=0)
    _finished: bool = field(init=False, default=False)

    def __enter__(self) -> CrawlSession:
        row = self.conn.execute(
            """
            SELECT COALESCE(MAX(attempt), 0) + 1 AS next_attempt
            FROM crawl_run WHERE organization_id = ? AND target_date = ?
            """,
            (self.organization_id, self.target_date.isoformat()),
        ).fetchone()
        self.attempt = int(row['next_attempt'])

        cur = self.conn.execute(
            """
            INSERT INTO crawl_run
                (organization_id, target_date, attempt, started_at, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (self.organization_id, self.target_date.isoformat(),
             self.attempt, self.now.isoformat()),
        )
        self.conn.commit()
        self.run_id = int(cur.lastrowid or 0)
        return self

    def record(self, *, fetched: int = 0, inserted: int = 0, updated: int = 0) -> None:
        self._fetched += fetched
        self._inserted += inserted
        self._updated += updated

    def _finish(self, status: CrawlStatus, *, error_kind: str | None = None,
                error_message: str | None = None) -> None:
        if self._finished:
            return
        self.conn.execute(
            """
            UPDATE crawl_run
               SET finished_at = ?, status = ?, fetched_count = ?,
                   inserted_count = ?, updated_count = ?,
                   error_kind = ?, error_message = ?
             WHERE id = ?
            """,
            (self.now.isoformat(), str(status), self._fetched,
             self._inserted, self._updated, error_kind, error_message, self.run_id),
        )
        self.conn.commit()
        self._finished = True

    def mark_disabled(self) -> None:
        """無効化された機関の実行。成功ではないので専用ステータスにする。"""
        self._finish(CrawlStatus.DISABLED)

    def mark_partial(self, reason: str) -> None:
        self._finish(CrawlStatus.PARTIAL, error_kind='partial', error_message=reason)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is not None:
            self._finish(CrawlStatus.FAILED,
                         error_kind=type(exc).__name__, error_message=str(exc))
            return False  # 再送出する。握りつぶさない。

        if not self._finished:
            # FR-105①: 0件は「成功」ではなく専用ステータスにする。
            # ここを CrawlStatus.OK にした瞬間、サイレント故障が検知不能になる。
            status = CrawlStatus.OK if self._fetched > 0 else CrawlStatus.OK_EMPTY
            self._finish(status)
        return False


def last_completed_run(
    conn: sqlite3.Connection, organization_id: str, target_date: dt.date
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM crawl_run
        WHERE organization_id = ? AND target_date = ? AND status != 'running'
        ORDER BY attempt DESC LIMIT 1
        """,
        (organization_id, target_date.isoformat()),
    ).fetchone()


def needs_rerun(
    conn: sqlite3.Connection, organization_id: str, target_date: dt.date
) -> bool:
    """その日の再実行が必要か。

    未実行・失敗・0件・一部失敗 は再実行対象。
    0件を再実行対象に含めるのが要点で、「一度0件で正常終了したから完了」
    とすると壊れたまま先に進む。

    disabled は含めない。無効な機関を毎日リトライしても意味が無く、
    attempt が際限なく増えてログが読めなくなる。
    無効のまま放置されていることは health 側で DISABLED として警告する。
    """
    row = last_completed_run(conn, organization_id, target_date)
    if row is None:
        return True
    return row['status'] in {'failed', 'ok_empty', 'partial'}
