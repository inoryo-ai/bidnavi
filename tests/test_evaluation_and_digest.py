"""計測と通知のテスト（Loop 5 / 天城）

重点:
  - Recall優先の指標設計になっているか（F1を主指標にしない）
  - 予測の欠損を「通知しなかった」として厳しく数えるか
  - ダイジェストが黙って案件を切り捨てないか
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3

import pytest

from tender_pipeline.core.db import connect
from tender_pipeline.evaluation.metrics import (
    LabeledCase,
    Metrics,
    Prediction,
    load_cases,
    misses,
    reachability,
    score,
)
from tender_pipeline.notify.digest import DISCLAIMER, build_digest

NOW = dt.datetime(2026, 9, 7, 10, 0)
SEED = pathlib.Path(__file__).parent / 'fixtures' / 'eval_seed.jsonl'


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------

def test_miss_rate_is_the_primary_metric() -> None:
    m = Metrics(total=100, true_positive=90, false_positive=10,
                true_negative=0, false_negative=10)
    assert m.recall == pytest.approx(0.9)
    assert m.miss_rate == pytest.approx(0.1)
    assert m.precision == pytest.approx(0.9)


def test_targets_reject_high_miss_rate_even_with_perfect_precision() -> None:
    """取りこぼしが多ければ、適合率が100%でも不合格にする。

    Recall優先の設計思想が指標に反映されているかの確認。
    F1を主指標にすると、この構成が「良い」と評価されてしまう。
    """
    m = Metrics(total=100, true_positive=50, false_positive=0,
                true_negative=0, false_negative=50)
    assert m.precision == pytest.approx(1.0)
    assert m.meets_targets() is False


def test_missing_prediction_counts_as_not_notified() -> None:
    """予測が欠けている件を無視しない。

    無視すると、パイプラインが落ちた分だけ成績が良く見える。
    """
    cases = [LabeledCase('A', 'co', 'タイトルA', relevant=True),
             LabeledCase('B', 'co', 'タイトルB', relevant=True)]
    m = score(cases, [Prediction('A', True)])   # B の予測が無い
    assert m.false_negative == 1
    assert m.miss_rate == pytest.approx(0.5)


def test_misses_lists_the_lost_cases() -> None:
    cases = [LabeledCase('A', 'co', 'A', relevant=True),
             LabeledCase('B', 'co', 'B', relevant=True)]
    lost = misses(cases, [Prediction('A', True), Prediction('B', False)])
    assert [c.case_id for c in lost] == ['B']


def test_reachability_measures_structural_loss() -> None:
    """AIの精度に依存しない指標であること。

    3段目に到達すらしていない関係案件は、AIをどれだけ良くしても拾えない。
    """
    cases = [LabeledCase('A', 'co', 'A', relevant=True),
             LabeledCase('B', 'co', 'B', relevant=True),
             LabeledCase('C', 'co', 'C', relevant=False)]
    r = reachability(cases, [
        Prediction('A', True, had_a_chance=True),
        Prediction('B', False, had_a_chance=False),
        Prediction('C', False, had_a_chance=False),
    ])
    assert r.relevant_total == 2
    assert r.coverage == pytest.approx(0.5)
    assert r.structural_miss_rate == pytest.approx(0.5)


def test_seed_cases_load_and_are_unique() -> None:
    cases = load_cases(SEED)
    assert len(cases) >= 30
    assert len({c.case_id for c in cases}) == len(cases)
    assert any(c.relevant for c in cases)
    assert any(not c.relevant for c in cases)


def test_duplicate_case_ids_are_rejected(tmp_path: pathlib.Path) -> None:
    p = tmp_path / 'dup.jsonl'
    p.write_text(
        '{"case_id":"X","company_id":"c","title":"a","relevant":true}\n'
        '{"case_id":"X","company_id":"c","title":"b","relevant":false}\n',
        encoding='utf-8')
    with pytest.raises(ValueError, match='重複'):
        load_cases(p)


# ---------------------------------------------------------------------------
# ダイジェスト
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(':memory:')
    c.execute(
        """INSERT INTO organization (id,name,org_type,entry_url,crawler_id,crawler_kind)
           VALUES ('org','テスト市','municipality','https://e.test/','x','http')""")
    c.execute("INSERT INTO company (id, name) VALUES ('co', 'テスト社')")
    c.commit()
    return c


def add_match(conn: sqlite3.Connection, key: str, title: str, score_value: int,
              *, deadline: str | None = '2026-09-20',
              deadline_time: str | None = '17:00',
              price: int | None = 1_000_000, undisclosed: bool = False,
              status: str = 'open', notify: bool = True) -> None:
    conn.execute(
        """INSERT INTO tender (natural_key, natural_key_version, natural_key_anchor,
            organization_id, source_url, source_captured_at, title, title_normalized,
            method, bid_deadline_date, bid_deadline_time, estimated_price,
            price_undisclosed, status, first_seen_at, last_seen_at)
           VALUES (?,1,'announced','org',?,?,?,?, 'open', ?,?,?,?,?,?,?)""",
        (key, f'https://e.test/{key}', NOW.isoformat(), title, title,
         deadline, deadline_time, price, int(undisclosed), status,
         NOW.isoformat(), NOW.isoformat()))
    conn.execute(
        """INSERT INTO match_result (natural_key, company_id, evaluated_at,
            stage_reached, score, reason, matched_by, should_notify)
           VALUES (?, 'co', ?, 1, ?, '理由', 'rule', ?)""",
        (key, NOW.isoformat(), score_value, int(notify)))
    conn.commit()


def test_digest_includes_disclaimer(conn: sqlite3.Connection) -> None:
    """CR-106: 免責を必ず出す。"""
    add_match(conn, 'k1', '業務システム改修', 80)
    assert DISCLAIMER in build_digest(conn, 'co', now=NOW).render()


def test_empty_digest_still_has_disclaimer(conn: sqlite3.Connection) -> None:
    digest = build_digest(conn, 'co', now=NOW)
    assert digest.is_empty is True
    assert DISCLAIMER in digest.render()


def test_digest_does_not_silently_truncate(conn: sqlite3.Connection) -> None:
    """指摘#10: 件数上限で切るとき、切ったことを必ず伝える。"""
    for i in range(15):
        add_match(conn, f'k{i}', f'業務システム改修その{i}', 80)

    digest = build_digest(conn, 'co', now=NOW, max_items=10)
    assert len(digest.items) == 10
    assert digest.omitted_count == 5
    assert digest.total_matched == 15
    rendered = digest.render()
    assert 'ほか 5 件' in rendered


def test_digest_orders_by_deadline_then_score(conn: sqlite3.Connection) -> None:
    """締切が近い順 → 関連度が高い順。"""
    add_match(conn, 'far', '遠い案件', 95, deadline='2026-12-01')
    add_match(conn, 'near_low', '近い案件低', 60, deadline='2026-09-10')
    add_match(conn, 'near_high', '近い案件高', 90, deadline='2026-09-10')

    items = build_digest(conn, 'co', now=NOW).items
    assert [i.natural_key for i in items] == ['near_high', 'near_low', 'far']


def test_digest_excludes_expired_but_keeps_same_day(conn: sqlite3.Connection) -> None:
    """AC-07: 締切当日の案件を、締切時刻を過ぎるまで落とさない。"""
    add_match(conn, 'today', '当日案件', 80,
              deadline='2026-09-07', deadline_time='17:00')

    morning = build_digest(conn, 'co', now=dt.datetime(2026, 9, 7, 10, 0))
    assert len(morning.items) == 1

    evening = build_digest(conn, 'co', now=dt.datetime(2026, 9, 7, 17, 1))
    assert len(evening.items) == 0


def test_digest_keeps_time_absent_deadline_all_day(conn: sqlite3.Connection) -> None:
    add_match(conn, 'noon', '時刻なし案件', 80,
              deadline='2026-09-07', deadline_time=None)
    late = build_digest(conn, 'co', now=dt.datetime(2026, 9, 7, 22, 0))
    assert len(late.items) == 1
    assert '時刻記載なし' in late.items[0].deadline_display


def test_digest_shows_undisclosed_price_not_zero(conn: sqlite3.Connection) -> None:
    """§7.3-3: 非公表を 0円 と表示しない。"""
    add_match(conn, 'u', '非公表案件', 80, price=None, undisclosed=True)
    item = build_digest(conn, 'co', now=NOW).items[0]
    assert item.price_display == '非公表'
    assert '0円' not in item.render()


def test_digest_shows_missing_price_as_not_stated(conn: sqlite3.Connection) -> None:
    add_match(conn, 'n', '金額不明案件', 80, price=None, undisclosed=False)
    assert build_digest(conn, 'co', now=NOW).items[0].price_display == '記載なし'


def test_digest_excludes_cancelled(conn: sqlite3.Connection) -> None:
    add_match(conn, 'c', '中止案件', 90, status='cancelled')
    assert build_digest(conn, 'co', now=NOW).is_empty is True


def test_digest_respects_score_threshold(conn: sqlite3.Connection) -> None:
    add_match(conn, 'low', '低スコア', 30)
    add_match(conn, 'high', '高スコア', 80)
    items = build_digest(conn, 'co', now=NOW, min_score=50).items
    assert [i.natural_key for i in items] == ['high']


def test_digest_excludes_non_notify_matches(conn: sqlite3.Connection) -> None:
    add_match(conn, 'ng', 'NG案件', 90, notify=False)
    assert build_digest(conn, 'co', now=NOW).is_empty is True


def test_digest_item_contains_required_fields(conn: sqlite3.Connection) -> None:
    """FR-307: 案件名・機関・締切・予定価格・関連度・原文URL を含む。"""
    add_match(conn, 'k', '業務システム改修業務委託', 85)
    rendered = build_digest(conn, 'co', now=NOW).items[0].render()
    for expected in ('業務システム改修業務委託', 'テスト市', '2026-09-20',
                     '1,000,000円', '85', 'https://e.test/k'):
        assert expected in rendered
