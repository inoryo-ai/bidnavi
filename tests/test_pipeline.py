"""収集基盤のテスト（Loop 2 / 天城）

重点:
  - AC-09 意図的に壊したときアラートが発火するか（障害注入）
  - FR-105① ok と ok_empty が別扱いになっているか
  - FR-111 冪等な再実行
  - FR-107 訂正で履歴が積まれ、締切変更が検出されるか
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from bidnavi.core.db import connect
from bidnavi.core.types import CrawlStatus, RawTender
from bidnavi.pipeline.health import HealthState, evaluate_all, evaluate_health
from bidnavi.pipeline.ingest import IngestOutcome, classify_method, ingest_tender
from bidnavi.pipeline.run import CrawlSession, needs_rerun

NOW = dt.datetime(2026, 9, 7, 10, 0)
ORG = 'city-test'


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(':memory:')
    c.execute(
        """
        INSERT INTO organization
            (id, name, org_type, prefecture_code, entry_url, crawler_id, crawler_kind)
        VALUES (?, 'テスト市', 'municipality', '12', 'https://example.test/', 'test', 'http')
        """,
        (ORG,),
    )
    c.commit()
    return c


def make_raw(title: str = '○○業務委託', **kw: object) -> RawTender:
    base: dict[str, object] = {
        'organization_id': ORG,
        'source_url': 'https://example.test/t/1',
        'captured_at': NOW,
        'title': title,
        'announced_text': '令和8年3月1日',
        'bid_deadline_text': '令和8年3月20日 午後5時',
        'price_text': '1,234,567円',
        'method_text': '一般競争入札',
    }
    base.update(kw)
    return RawTender(**base)  # type: ignore[arg-type]


def seed_runs(conn: sqlite3.Connection, counts: list[int], *, end: dt.date) -> None:
    """過去の正常実行を仕込んでベースラインを作る。"""
    for offset, count in enumerate(reversed(counts), start=1):
        day = end - dt.timedelta(days=offset)
        conn.execute(
            """
            INSERT INTO crawl_run
                (organization_id, target_date, attempt, started_at, finished_at,
                 status, fetched_count)
            VALUES (?, ?, 1, ?, ?, 'ok', ?)
            """,
            (ORG, day.isoformat(), f'{day}T09:00:00', f'{day}T09:05:00', count),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# crawl_run のライフサイクル
# ---------------------------------------------------------------------------

def test_session_marks_ok_when_items_fetched(conn: sqlite3.Connection) -> None:
    with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW) as s:
        s.record(fetched=5, inserted=5)
    row = conn.execute('SELECT * FROM crawl_run').fetchone()
    assert row['status'] == CrawlStatus.OK
    assert row['fetched_count'] == 5


def test_session_marks_ok_empty_not_ok_when_zero(conn: sqlite3.Connection) -> None:
    """FR-105①: 0件を ok にしない。ここが崩れると全ての検知が死ぬ。"""
    with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW):
        pass
    row = conn.execute('SELECT * FROM crawl_run').fetchone()
    assert row['status'] == CrawlStatus.OK_EMPTY
    assert row['status'] != CrawlStatus.OK


def test_session_records_failure_and_reraises(conn: sqlite3.Connection) -> None:
    """例外は記録した上で再送出する。握りつぶさない。"""
    with pytest.raises(RuntimeError, match='boom'):
        with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW) as s:
            s.record(fetched=3)
            raise RuntimeError('boom')

    row = conn.execute('SELECT * FROM crawl_run').fetchone()
    assert row['status'] == CrawlStatus.FAILED
    assert row['error_kind'] == 'RuntimeError'
    assert 'boom' in row['error_message']
    assert row['fetched_count'] == 3  # 途中まで取れた分も残る


def test_disabled_is_not_success(conn: sqlite3.Connection) -> None:
    with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW) as s:
        s.mark_disabled()
    row = conn.execute('SELECT * FROM crawl_run').fetchone()
    assert row['status'] == CrawlStatus.DISABLED
    assert row['status'] != CrawlStatus.OK


def test_reruns_increment_attempt(conn: sqlite3.Connection) -> None:
    """FR-111: 同じ日を再実行しても衝突せず attempt が積み上がる。"""
    day = dt.date(2026, 9, 7)
    for _ in range(3):
        with CrawlSession(conn, ORG, day, NOW) as s:
            s.record(fetched=1)
    attempts = [r['attempt'] for r in
                conn.execute('SELECT attempt FROM crawl_run ORDER BY attempt')]
    assert attempts == [1, 2, 3]


def test_needs_rerun_logic(conn: sqlite3.Connection) -> None:
    day = dt.date(2026, 9, 7)
    assert needs_rerun(conn, ORG, day) is True          # 未実行

    with CrawlSession(conn, ORG, day, NOW):             # 0件で終了
        pass
    assert needs_rerun(conn, ORG, day) is True          # 0件は再実行対象

    with CrawlSession(conn, ORG, day, NOW) as s:
        s.record(fetched=4)
    assert needs_rerun(conn, ORG, day) is False         # 取得できたら完了


# ---------------------------------------------------------------------------
# サイレント故障の検知（AC-09 障害注入）
# ---------------------------------------------------------------------------

def test_health_detects_silent_zero_result(conn: sqlite3.Connection) -> None:
    """AC-09: セレクタが空振りして0件になったのを検知できること。

    例外は一切出ていない。これがサイレント故障の典型。
    """
    seed_runs(conn, [20, 22, 19, 21], end=dt.date(2026, 9, 7))
    with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW):
        pass  # ← サイト改修でセレクタが空振りした状態

    report = evaluate_health(conn, ORG, now=NOW)
    assert report.state is HealthState.EMPTY
    assert report.is_alert is True


def test_health_detects_volume_drop(conn: sqlite3.Connection) -> None:
    """一部のセレクタだけ壊れて件数が激減したケース。"""
    seed_runs(conn, [20, 22, 19, 21], end=dt.date(2026, 9, 7))
    with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW) as s:
        s.record(fetched=3)   # 平均20件に対して3件

    report = evaluate_health(conn, ORG, now=NOW)
    assert report.state is HealthState.VOLUME_DROP
    assert report.is_alert is True
    assert report.baseline_avg is not None and report.baseline_avg > 15


def test_health_accepts_normal_fluctuation(conn: sqlite3.Connection) -> None:
    """正常な変動でアラートを出さない（誤報で信用を失わないため）。"""
    seed_runs(conn, [20, 22, 19, 21], end=dt.date(2026, 9, 7))
    with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW) as s:
        s.record(fetched=14)   # 平均20の70%。閾値50%より上

    assert evaluate_health(conn, ORG, now=NOW).state is HealthState.HEALTHY


def test_health_detects_stale(conn: sqlite3.Connection) -> None:
    """一定期間 ok が出ていないことを検知する。"""
    old = dt.date(2026, 8, 1)
    seed_runs(conn, [20, 20, 20, 20], end=old)
    with CrawlSession(conn, ORG, old, dt.datetime(2026, 8, 1, 9, 0)) as s:
        s.record(fetched=20)

    report = evaluate_health(conn, ORG, now=NOW)
    assert report.state is HealthState.STALE
    assert report.is_alert is True


def test_health_flags_never_run(conn: sqlite3.Connection) -> None:
    report = evaluate_health(conn, ORG, now=NOW)
    assert report.state is HealthState.NEVER_RUN
    assert report.is_alert is True


def test_health_flags_disabled_org(conn: sqlite3.Connection) -> None:
    """無効化されたまま忘れられるのを防ぐ。「無効だから正常」にしない。"""
    conn.execute('UPDATE organization SET enabled = 0 WHERE id = ?', (ORG,))
    conn.commit()
    report = evaluate_health(conn, ORG, now=NOW)
    assert report.state is HealthState.DISABLED
    assert report.is_alert is True


def test_health_warming_up_with_few_samples(conn: sqlite3.Connection) -> None:
    """サンプル不足のときは異常と断定しない（判定不能を正直に返す）。"""
    seed_runs(conn, [20], end=dt.date(2026, 9, 7))
    with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW) as s:
        s.record(fetched=1)
    assert evaluate_health(conn, ORG, now=NOW).state is HealthState.WARMING_UP


def test_baseline_excludes_broken_runs(conn: sqlite3.Connection) -> None:
    """ok_empty をベースラインに混ぜない。

    混ぜると壊れた状態が「正常な平均」に取り込まれ、
    平均が0に近づいて異常を検知できなくなる。
    """
    seed_runs(conn, [20, 20, 20], end=dt.date(2026, 9, 7))
    for offset in (4, 5, 6):
        day = dt.date(2026, 9, 7) - dt.timedelta(days=offset)
        conn.execute(
            """INSERT INTO crawl_run (organization_id, target_date, attempt,
               started_at, finished_at, status, fetched_count)
               VALUES (?, ?, 1, ?, ?, 'ok_empty', 0)""",
            (ORG, day.isoformat(), f'{day}T09:00:00', f'{day}T09:05:00'),
        )
    conn.commit()

    with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW) as s:
        s.record(fetched=2)

    report = evaluate_health(conn, ORG, now=NOW)
    assert report.baseline_avg == 20.0   # 0件の実行は平均に含めない
    assert report.state is HealthState.VOLUME_DROP


def test_evaluate_all_puts_alerts_first(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO organization (id, name, org_type, entry_url, crawler_id, crawler_kind)
           VALUES ('org-ok', 'OK市', 'municipality', 'https://e.test/', 'test', 'http')"""
    )
    conn.commit()
    seed_runs(conn, [20, 20, 20, 20], end=dt.date(2026, 9, 7))
    with CrawlSession(conn, ORG, dt.date(2026, 9, 7), NOW) as s:
        s.record(fetched=20)

    reports = evaluate_all(conn, now=NOW)
    assert reports[0].organization_id == 'org-ok'   # NEVER_RUN が先頭
    assert reports[0].is_alert is True


# ---------------------------------------------------------------------------
# 取り込み（冪等性・訂正履歴）
# ---------------------------------------------------------------------------

def test_ingest_inserts_then_is_idempotent(conn: sqlite3.Connection) -> None:
    """FR-111: 同じ入力を何度流しても増えない。"""
    first = ingest_tender(conn, make_raw(), now=NOW)
    assert first.outcome is IngestOutcome.INSERTED

    second = ingest_tender(conn, make_raw(), now=NOW)
    assert second.outcome is IngestOutcome.UNCHANGED
    assert second.natural_key == first.natural_key

    count = conn.execute('SELECT COUNT(*) AS n FROM tender').fetchone()['n']
    assert count == 1


def test_ingest_merges_notation_variants(conn: sqlite3.Connection) -> None:
    """FR-106: 表記ゆれは同一案件に寄る。"""
    ingest_tender(conn, make_raw('○○業務委託'), now=NOW)
    ingest_tender(conn,
                  make_raw('【入札公告】令和８年度　○○業務委託について'), now=NOW)
    assert conn.execute('SELECT COUNT(*) AS n FROM tender').fetchone()['n'] == 1


def test_ingest_records_revision_on_deadline_change(conn: sqlite3.Connection) -> None:
    """FR-107: 訂正で締切が変わったら履歴を積み、変更を検出する。"""
    ingest_tender(conn, make_raw(), now=NOW)
    result = ingest_tender(
        conn,
        make_raw(bid_deadline_text='令和8年3月27日 午後5時'),  # 締切が1週間延びた
        now=NOW + dt.timedelta(days=1),
    )

    assert result.outcome is IngestOutcome.UPDATED
    assert result.revision == 2
    assert result.deadline_changed is True
    assert 'bid_deadline_date' in result.changed_fields

    revisions = conn.execute(
        'SELECT * FROM tender_revision ORDER BY revision'
    ).fetchall()
    assert len(revisions) == 2
    diff = json.loads(revisions[1]['diff_json'])
    assert diff['bid_deadline_date'] == ['2026-03-20', '2026-03-27']


def test_deadline_change_does_not_split_the_tender(conn: sqlite3.Connection) -> None:
    """締切が延びても別案件にならない（公告日をアンカーにしている理由）。"""
    ingest_tender(conn, make_raw(), now=NOW)
    ingest_tender(conn, make_raw(bid_deadline_text='令和8年3月27日'), now=NOW)
    assert conn.execute('SELECT COUNT(*) AS n FROM tender').fetchone()['n'] == 1


def test_ingest_never_deletes(conn: sqlite3.Connection) -> None:
    """物理削除しない。過去の値は履歴から復元できる。"""
    ingest_tender(conn, make_raw(price_text='1,234,567円'), now=NOW)
    ingest_tender(conn, make_raw(price_text='2,000,000円'), now=NOW)

    revisions = conn.execute(
        'SELECT diff_json FROM tender_revision ORDER BY revision'
    ).fetchall()
    diff = json.loads(revisions[1]['diff_json'])
    assert diff['estimated_price'] == [1234567, 2000000]


def test_ingest_preserves_undisclosed_price(conn: sqlite3.Connection) -> None:
    """非公表が 0円 として保存されないこと（DB制約でも守る）。"""
    ingest_tender(conn, make_raw(price_text='非公表'), now=NOW)
    row = conn.execute('SELECT * FROM tender').fetchone()
    assert row['estimated_price'] is None
    assert row['price_undisclosed'] == 1


def test_db_rejects_undisclosed_with_amount(conn: sqlite3.Connection) -> None:
    """アプリのバグをDB側でも止める（多層防御）。"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO tender (natural_key, natural_key_version, natural_key_anchor,
                organization_id, source_url, source_captured_at, title, title_normalized,
                method, estimated_price, price_undisclosed, first_seen_at, last_seen_at)
               VALUES ('k', 1, 'announced', ?, 'u', 'now', 't', 't', 'open',
                       100, 1, 'now', 'now')""",
            (ORG,),
        )


def test_ingest_stores_time_absence_as_null(conn: sqlite3.Connection) -> None:
    """FR-203: 時刻不明を 00:00 として保存しない。"""
    ingest_tender(conn, make_raw(bid_deadline_text='令和8年3月20日'), now=NOW)
    row = conn.execute('SELECT * FROM tender').fetchone()
    assert row['bid_deadline_date'] == '2026-03-20'
    assert row['bid_deadline_time'] is None


def test_ingest_updates_last_seen_even_when_unchanged(conn: sqlite3.Connection) -> None:
    """内容が同じでも「今日も見えていた」ことは記録する。

    last_seen_at が止まっている＝案件が消えた、を後で検知するため。
    """
    ingest_tender(conn, make_raw(), now=NOW)
    later = NOW + dt.timedelta(days=1)
    ingest_tender(conn, make_raw(), now=later)
    row = conn.execute('SELECT * FROM tender').fetchone()
    assert row['last_seen_at'] == later.isoformat()
    assert row['first_seen_at'] == NOW.isoformat()


# ---------------------------------------------------------------------------
# ライフサイクル（締切判定・消えた案件）
# ---------------------------------------------------------------------------

def test_tender_stays_visible_until_deadline_time_passes(conn: sqlite3.Connection) -> None:
    """AC-07 / §7.3-2: 締切当日の案件を、締切時刻を過ぎるまで消さない。"""
    from bidnavi.pipeline.lifecycle import close_expired

    ingest_tender(conn, make_raw(bid_deadline_text='令和8年3月20日 午後5時'), now=NOW)

    # 締切当日の午前中
    assert close_expired(conn, now=dt.datetime(2026, 3, 20, 10, 0)) == 0
    assert conn.execute('SELECT status FROM tender').fetchone()['status'] == 'open'

    # 締切時刻を過ぎた
    assert close_expired(conn, now=dt.datetime(2026, 3, 20, 17, 1)) == 1
    assert conn.execute('SELECT status FROM tender').fetchone()['status'] == 'closed'


def test_time_absent_deadline_survives_the_whole_day(conn: sqlite3.Connection) -> None:
    """時刻不明の締切を 00:00 として扱うと、当日の午前中に消える。"""
    from bidnavi.pipeline.lifecycle import close_expired

    ingest_tender(conn, make_raw(bid_deadline_text='令和8年3月20日'), now=NOW)

    assert close_expired(conn, now=dt.datetime(2026, 3, 20, 0, 1)) == 0
    assert close_expired(conn, now=dt.datetime(2026, 3, 20, 23, 0)) == 0
    assert close_expired(conn, now=dt.datetime(2026, 3, 21, 0, 1)) == 1


def test_unknown_deadline_is_never_auto_closed(conn: sqlite3.Connection) -> None:
    """締切が読めなかった案件を「不明だから消す」のは取りこぼし。"""
    from bidnavi.pipeline.lifecycle import close_expired, is_closed

    ingest_tender(conn, make_raw(bid_deadline_text=None), now=NOW)
    assert close_expired(conn, now=dt.datetime(2030, 1, 1)) == 0
    assert is_closed(None, None, now=dt.datetime(2030, 1, 1)) is False


def test_notify_and_close_lean_opposite_directions() -> None:
    """時刻不明の扱いは通知と締切判定で逆になる。どちらも取りこぼさない側。"""
    from bidnavi.core.normalize.dates import parse_deadline
    from bidnavi.pipeline.lifecycle import deadline_expires_at

    d = parse_deadline('令和8年3月20日')
    assert d is not None and d.time is None
    assert d.notify_at() == dt.datetime(2026, 3, 20, 9, 0)          # 早い側
    assert deadline_expires_at(d.date, d.time).hour == 23           # 遅い側


def test_find_disappeared_flags_tenders_gone_before_deadline(
    conn: sqlite3.Connection,
) -> None:
    """締切前なのに一覧から消えた案件を、黙って closed にせず人に見せる。"""
    from bidnavi.pipeline.lifecycle import find_disappeared

    ingest_tender(conn, make_raw(bid_deadline_text='令和8年12月20日'), now=NOW)

    assert find_disappeared(conn, ORG, now=NOW + dt.timedelta(days=1)) == []

    gone = find_disappeared(conn, ORG, now=NOW + dt.timedelta(days=5))
    assert len(gone) == 1
    assert gone[0].days_missing == 5


def test_find_disappeared_ignores_already_expired(conn: sqlite3.Connection) -> None:
    """締切済みの案件が見えなくなるのは当然なので警告しない。"""
    from bidnavi.pipeline.lifecycle import find_disappeared

    ingest_tender(conn, make_raw(bid_deadline_text='令和8年3月20日'), now=NOW)
    assert find_disappeared(conn, ORG, now=dt.datetime(2026, 4, 1)) == []


def test_needs_rerun_does_not_retry_disabled(conn: sqlite3.Connection) -> None:
    """無効な機関を毎日リトライしない（attempt が際限なく増えるため）。"""
    day = dt.date(2026, 9, 7)
    with CrawlSession(conn, ORG, day, NOW) as s:
        s.mark_disabled()
    assert needs_rerun(conn, ORG, day) is False


@pytest.mark.parametrize(('text', 'expected'), [
    ('一般競争入札', 'open'),
    ('条件付一般競争入札', 'open'),
    ('指名競争入札', 'selective'),
    ('随意契約', 'negotiated'),
    ('公募型プロポーザル', 'proposal'),
    ('企画競争', 'proposal'),
    (None, 'unknown'),
    ('よくわからない方式', 'unknown'),
])
def test_classify_method(text: str | None, expected: str) -> None:
    assert str(classify_method(text)) == expected
