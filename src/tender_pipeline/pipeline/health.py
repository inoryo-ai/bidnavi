"""サイレント故障の検知（要件定義 v2.0 FR-105 / NFR-105）

このプロダクトで最悪の事故は、クローラが落ちることではなく
**エラーを出さずに0件を返し続けること**である。
HTML構造が変わってセレクタが空振りしても、コードは正常終了する。
ユーザーは「今週は案件がなかったんだな」と思い、締切を逃す。

したがってここでは「例外が出なかった＝正常」という判定を一切しない。
判定材料は3つ:
  1. 直近の実行ステータス（ok_empty は成功ではなく異常候補）
  2. 直近30日の平均取得件数との比率
  3. 最後に ok だった時刻からの経過日数
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

#: 直近取得件数がベースライン平均のこの割合を下回ったら異常とみなす
DEFAULT_DROP_RATIO = 0.5
#: ok がこの日数出ていなければ異常とみなす
DEFAULT_STALE_DAYS = 3
#: ベースラインを計算する窓
DEFAULT_WINDOW_DAYS = 30
#: ベースラインとして信頼するのに必要な最低サンプル数
MIN_BASELINE_SAMPLES = 3


class HealthState(StrEnum):
    HEALTHY = 'healthy'
    NEVER_RUN = 'never_run'        # 一度も実行されていない
    DISABLED = 'disabled'          # 設定で無効。正常ではない
    FAILED = 'failed'              # 直近が明示的な失敗
    EMPTY = 'empty'                # 正常終了したが0件 ← サイレント故障の典型
    VOLUME_DROP = 'volume_drop'    # 件数がベースラインを大きく下回った
    STALE = 'stale'                # 一定期間 ok が出ていない
    WARMING_UP = 'warming_up'      # サンプル不足で判定できない


#: 管理画面で赤表示にすべき状態
ALERT_STATES: frozenset[HealthState] = frozenset({
    HealthState.NEVER_RUN,
    HealthState.DISABLED,
    HealthState.FAILED,
    HealthState.EMPTY,
    HealthState.VOLUME_DROP,
    HealthState.STALE,
})


@dataclass(frozen=True, slots=True)
class HealthReport:
    organization_id: str
    state: HealthState
    reason: str
    last_status: str | None
    last_run_at: str | None
    last_success_at: str | None
    latest_count: int | None
    baseline_avg: float | None
    baseline_samples: int

    @property
    def is_alert(self) -> bool:
        return self.state in ALERT_STATES


def _latest_run(conn: sqlite3.Connection, organization_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM crawl_run
        WHERE organization_id = ? AND status != 'running'
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (organization_id,),
    ).fetchone()


def _last_success_at(conn: sqlite3.Connection, organization_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT finished_at, started_at FROM crawl_run
        WHERE organization_id = ? AND status = 'ok'
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (organization_id,),
    ).fetchone()
    if row is None:
        return None
    return row['finished_at'] or row['started_at']


def _baseline(
    conn: sqlite3.Connection,
    organization_id: str,
    *,
    since: dt.date,
    exclude_run_id: int | None,
) -> tuple[float | None, int]:
    """直近窓の平均取得件数。

    ベースラインには status='ok' の実行だけを使う。
    ok_empty を混ぜると、壊れた状態が「正常な平均」に取り込まれて
    異常を検知できなくなる（壊れたまま平均が0に近づいていく）。
    """
    rows = conn.execute(
        """
        SELECT fetched_count FROM crawl_run
        WHERE organization_id = ?
          AND status = 'ok'
          AND target_date >= ?
          AND (? IS NULL OR id != ?)
        """,
        (organization_id, since.isoformat(), exclude_run_id, exclude_run_id),
    ).fetchall()
    counts = [int(r['fetched_count']) for r in rows]
    if not counts:
        return None, 0
    return sum(counts) / len(counts), len(counts)


def evaluate_health(
    conn: sqlite3.Connection,
    organization_id: str,
    *,
    now: dt.datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    drop_ratio: float = DEFAULT_DROP_RATIO,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> HealthReport:
    """1機関の健全性を判定する。

    判定順は「確実に異常と言える順」。
    先に EMPTY / FAILED を確定させ、そのあとで統計的な判定に進む。
    """
    org = conn.execute(
        'SELECT enabled FROM organization WHERE id = ?', (organization_id,)
    ).fetchone()
    if org is None:
        raise KeyError(f'未登録の機関です: {organization_id}')

    latest = _latest_run(conn, organization_id)
    last_success = _last_success_at(conn, organization_id)
    since = (now - dt.timedelta(days=window_days)).date()

    def report(state: HealthState, reason: str, *, avg: float | None = None,
               samples: int = 0) -> HealthReport:
        return HealthReport(
            organization_id=organization_id,
            state=state,
            reason=reason,
            last_status=latest['status'] if latest else None,
            last_run_at=latest['started_at'] if latest else None,
            last_success_at=last_success,
            latest_count=int(latest['fetched_count']) if latest else None,
            baseline_avg=avg,
            baseline_samples=samples,
        )

    if not org['enabled']:
        # 「無効だから成功」にしない。無効化されたまま忘れられるのを防ぐ。
        return report(HealthState.DISABLED, '機関が無効化されています')

    if latest is None:
        return report(HealthState.NEVER_RUN, '一度も実行されていません')

    if latest['status'] == 'failed':
        return report(HealthState.FAILED,
                      f"直近の実行が失敗: {latest['error_kind'] or 'unknown'}")

    if latest['status'] == 'ok_empty':
        # 例外は出ていない。しかしこれこそがサイレント故障の典型。
        return report(HealthState.EMPTY,
                      '正常終了したが取得0件。セレクタ空振りの可能性')

    if latest['status'] == 'disabled':
        return report(HealthState.DISABLED, '直近の実行が無効化されていました')

    # ここから先は latest['status'] が 'ok' か 'partial'
    if last_success is not None:
        elapsed = now - dt.datetime.fromisoformat(last_success)
        if elapsed > dt.timedelta(days=stale_days):
            return report(HealthState.STALE,
                          f'{elapsed.days}日間 ok が出ていません')

    avg, samples = _baseline(conn, organization_id, since=since,
                             exclude_run_id=int(latest['id']))
    if avg is None or samples < MIN_BASELINE_SAMPLES:
        return report(HealthState.WARMING_UP,
                      f'ベースラインのサンプル不足（{samples}件）',
                      avg=avg, samples=samples)

    latest_count = int(latest['fetched_count'])
    if latest_count < avg * drop_ratio:
        return report(
            HealthState.VOLUME_DROP,
            f'取得件数がベースラインを大きく下回りました: '
            f'{latest_count}件 < 平均{avg:.1f}件 × {drop_ratio}',
            avg=avg, samples=samples)

    if latest['status'] == 'partial':
        return report(HealthState.HEALTHY,
                      '一部ページで失敗しましたが件数は正常範囲です',
                      avg=avg, samples=samples)

    return report(HealthState.HEALTHY, '正常', avg=avg, samples=samples)


def evaluate_all(
    conn: sqlite3.Connection, *, now: dt.datetime, **kwargs: object
) -> list[HealthReport]:
    """全機関を判定し、アラート対象を先頭に並べて返す。"""
    ids = [r['id'] for r in conn.execute('SELECT id FROM organization ORDER BY id')]
    reports = [evaluate_health(conn, i, now=now, **kwargs) for i in ids]  # type: ignore[arg-type]
    reports.sort(key=lambda r: (not r.is_alert, r.organization_id))
    return reports
