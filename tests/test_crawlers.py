"""クローラのテスト（Loop 3 / 天城）

重点:
  - V-1 サイレント故障: 構造変化を注入したとき、0件ではなく例外になるか
  - 文字化け（データレベルのサイレント故障）が再発しないか
  - 一覧コンテナはあるが行が0のとき、正しく「本当に0件」と区別できるか

ネットワークを使うテストは -m network で分離し、既定では実行しない。
CIが外部サイトに依存すると、サイトの都合でCIが落ちるようになるため。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3

import pytest

from tender_pipeline.core.db import connect
from tender_pipeline.core.html import looks_mojibake, soup_from
from tender_pipeline.core.types import CrawlerKind, CrawlStatus
from tender_pipeline.crawlers.base import (
    CrawlerRegistry,
    SelectorMissError,
)
from tender_pipeline.crawlers.yokohama import (
    LISTING_SELECTOR,
    ORGANIZATION_ID,
    YokohamaCrawler,
    is_result_announcement,
)
from tender_pipeline.pipeline.health import HealthState, evaluate_health
from tender_pipeline.pipeline.ingest import ingest_tender
from tender_pipeline.pipeline.run import CrawlSession

NOW = dt.datetime(2026, 9, 7, 10, 0)
FIXTURE = pathlib.Path(__file__).parent / 'fixtures' / 'yokohama_listing.html'


@pytest.fixture()
def listing_html() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(':memory:')
    c.execute(
        """
        INSERT INTO organization
            (id, name, org_type, prefecture_code, entry_url, crawler_id, crawler_kind)
        VALUES (?, '横浜市', 'municipality', '14',
                'https://www.city.yokohama.lg.jp/business/nyusatsu/',
                'yokohama-listing', 'http')
        """,
        (ORGANIZATION_ID,),
    )
    c.commit()
    return c


# ---------------------------------------------------------------------------
# 実データの解析
# ---------------------------------------------------------------------------

def test_parses_real_listing(listing_html: bytes) -> None:
    items = YokohamaCrawler().parse(listing_html, now=NOW)
    assert len(items) >= 1
    assert all(t.source_url.startswith('https://www.city.yokohama.lg.jp/') for t in items)
    assert all(t.organization_id == ORGANIZATION_ID for t in items)
    assert all(t.captured_at == NOW for t in items)


def test_titles_are_not_mojibake(listing_html: bytes) -> None:
    """実測で見つかった事故の回帰テスト。

    横浜市は Content-Type に charset を持たないため、requests の
    response.text を使うと ISO-8859-1 と誤認されて文字化けする。
    例外も出ず件数も正しいので、監視では絶対に検知できない。
    """
    items = YokohamaCrawler().parse(listing_html, now=NOW)
    for t in items:
        assert not looks_mojibake(t.title), f'文字化けしています: {t.title!r}'
    # 日本語が実際に読めていることの積極的な確認
    assert any('業務' in t.title or '購入' in t.title or 'リース' in t.title
               for t in items)


def test_response_text_would_have_been_mojibake(listing_html: bytes) -> None:
    """なぜ bytes を渡す必要があるのかを、失敗する側で固定しておく。

    このテストが落ちたら「サイトが charset を返すようになった」ということなので、
    その時点で調べ直せばよい。黙って直す（ように見える）ことを防ぐ。
    """
    broken = listing_html.decode('iso-8859-1')  # requests の既定挙動を再現
    items = YokohamaCrawler().parse(broken, now=NOW)
    assert any(looks_mojibake(t.title) for t in items)


def test_result_announcements_are_identified(listing_html: bytes) -> None:
    """FR-401: 【契約結果公表】は募集中の案件ではない。"""
    items = YokohamaCrawler().parse(listing_html, now=NOW)
    flagged = [t for t in items if is_result_announcement(t.title)]
    for t in flagged:
        assert '結果' in t.title or '中止' in t.title
        assert t.method_text is None   # 結果公表から入札方式を推測しない


def test_method_text_is_none_when_unknown(listing_html: bytes) -> None:
    """FR-207: 読めないものを推測で埋めない。"""
    items = YokohamaCrawler().parse(listing_html, now=NOW)
    plain = [t for t in items if not t.title.startswith('【')]
    assert plain, 'テスト前提が崩れています（角括弧なしの案件が無い）'
    assert all(t.method_text is None for t in plain)


# ---------------------------------------------------------------------------
# サイレント故障（構造変化の注入）— V-1 の中核
# ---------------------------------------------------------------------------

def test_structure_change_raises_instead_of_returning_empty(
    listing_html: bytes,
) -> None:
    """AC-09: 一覧コンテナが消えたら、0件ではなく例外にする。

    ここで空リストを返すと ok_empty ですらなく「案件が無い日」に見える。
    ページは200で取れているぶん、通信エラーより発見が遅れて危険。
    """
    broken = listing_html.replace(b'news_list', b'news_list_v2')
    with pytest.raises(SelectorMissError, match=LISTING_SELECTOR.replace('.', r'\.')):
        YokohamaCrawler().parse(broken, now=NOW)


def test_empty_container_is_reported_as_zero_not_error(listing_html: bytes) -> None:
    """一覧はあるが行が0のときは「本当に0件」なので例外にしない。

    この2つを区別できることが、サイレント故障検知の前提になる。
    """
    soup = soup_from(listing_html)
    for ul in soup.select(LISTING_SELECTOR):
        ul.clear()
    items = YokohamaCrawler().parse(str(soup), now=NOW)
    assert items == []


def test_structure_change_surfaces_as_failed_run(
    conn: sqlite3.Connection, listing_html: bytes
) -> None:
    """構造変化がパイプライン全体で failed として記録されること。

    クローラの例外 → CrawlSession が failed で記録 → health がアラート、
    という一本の線がつながっているかを確認する。
    """
    broken = listing_html.replace(b'news_list', b'news_list_v2')

    with (
        pytest.raises(SelectorMissError),
        CrawlSession(conn, ORGANIZATION_ID, NOW.date(), NOW) as session,
    ):
        items = YokohamaCrawler().parse(broken, now=NOW)
        session.record(fetched=len(items))

    row = conn.execute('SELECT * FROM crawl_run').fetchone()
    assert row['status'] == CrawlStatus.FAILED
    assert row['error_kind'] == 'SelectorMissError'

    report = evaluate_health(conn, ORGANIZATION_ID, now=NOW)
    assert report.state is HealthState.FAILED
    assert report.is_alert is True


def test_healthy_run_end_to_end(
    conn: sqlite3.Connection, listing_html: bytes
) -> None:
    """正常系: 解析 → 取り込み → ok で記録されるまで通す。"""
    with CrawlSession(conn, ORGANIZATION_ID, NOW.date(), NOW) as session:
        items = YokohamaCrawler().parse(listing_html, now=NOW)
        inserted = 0
        for raw in items:
            if ingest_tender(conn, raw, now=NOW).outcome == 'inserted':
                inserted += 1
        session.record(fetched=len(items), inserted=inserted)

    row = conn.execute('SELECT * FROM crawl_run').fetchone()
    assert row['status'] == CrawlStatus.OK
    assert row['fetched_count'] >= 1
    assert row['inserted_count'] == row['fetched_count']

    stored = conn.execute('SELECT COUNT(*) AS n FROM tender').fetchone()['n']
    assert stored == row['fetched_count']


def test_result_announcements_are_not_stored_as_open(
    conn: sqlite3.Connection, listing_html: bytes
) -> None:
    """§7.2-1: 終了案件を募集中として取り込まないこと。

    判定関数を作っただけで取り込みに繋いでいなかった、という欠陥の回帰テスト。
    """
    items = YokohamaCrawler().parse(listing_html, now=NOW)
    for raw in items:
        ingest_tender(conn, raw, now=NOW)

    rows = conn.execute('SELECT title, status FROM tender').fetchall()
    assert rows, 'テスト前提が崩れています'
    for r in rows:
        if is_result_announcement(r['title']):
            assert r['status'] == 'closed', f'結果公表が open のまま: {r["title"]}'
        else:
            assert r['status'] == 'open'

    # このフィクスチャには結果公表が最低1件含まれている前提
    assert any(is_result_announcement(r['title']) for r in rows)


def test_result_announcement_is_excluded_from_digest(
    conn: sqlite3.Connection, listing_html: bytes
) -> None:
    """終了案件が通知に載らないこと（経路の端まで確認する）。"""
    from tender_pipeline.notify.digest import build_digest

    conn.execute("INSERT INTO company (id, name) VALUES ('co', 'テスト社')")
    for raw in YokohamaCrawler().parse(listing_html, now=NOW):
        result = ingest_tender(conn, raw, now=NOW)
        conn.execute(
            """INSERT INTO match_result (natural_key, company_id, evaluated_at,
                stage_reached, score, reason, matched_by, should_notify)
               VALUES (?, 'co', ?, 1, 90, '理由', 'rule', 1)""",
            (result.natural_key, NOW.isoformat()))
    conn.commit()

    titles = [i.title for i in build_digest(conn, 'co', now=NOW).items]
    assert not any(is_result_announcement(t) for t in titles)


def test_crawl_is_idempotent_against_real_data(
    conn: sqlite3.Connection, listing_html: bytes
) -> None:
    """FR-111: 同じ一覧を2回流しても案件は増えない。"""
    items = YokohamaCrawler().parse(listing_html, now=NOW)
    for _ in range(2):
        for raw in items:
            ingest_tender(conn, raw, now=NOW)
    assert conn.execute('SELECT COUNT(*) AS n FROM tender').fetchone()['n'] == len(items)


# ---------------------------------------------------------------------------
# レジストリ
# ---------------------------------------------------------------------------

def test_registry_separates_http_and_browser_crawlers() -> None:
    """FR-112: 実行基盤を分けるための取り出しができること。"""
    reg = CrawlerRegistry()
    reg.register(YokohamaCrawler())
    assert reg.ids() == ['yokohama-listing']
    assert len(reg.by_kind(CrawlerKind.HTTP)) == 1
    assert reg.by_kind(CrawlerKind.BROWSER) == []


def test_registry_rejects_duplicate_ids() -> None:
    reg = CrawlerRegistry()
    reg.register(YokohamaCrawler())
    with pytest.raises(ValueError, match='重複'):
        reg.register(YokohamaCrawler())


def test_registry_unknown_id_raises() -> None:
    with pytest.raises(KeyError, match='未登録'):
        CrawlerRegistry().get('nope')


# ---------------------------------------------------------------------------
# ネットワークテスト（既定では実行しない）
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_live_fetch_matches_fixture_structure() -> None:
    """実サイトが今もこの構造かを確認する。落ちたら構造変化のサイン。"""
    import os

    from tender_pipeline.core.http import (
        USER_AGENT_CONTACT_ENV,
        RateLimitedClient,
        build_user_agent,
    )

    if not os.environ.get(USER_AGENT_CONTACT_ENV):
        pytest.skip(f'{USER_AGENT_CONTACT_ENV} が未設定のため実サイトへは接続しない')

    client = RateLimitedClient(user_agent=build_user_agent(), min_interval=2.0)
    items = YokohamaCrawler(client).fetch_listing(now=NOW)
    assert items, '実サイトから1件も取得できませんでした'
    assert not any(looks_mojibake(t.title) for t in items)


# ---------------------------------------------------------------------------
# 連絡先の設定漏れ防止（CR-102）
# ---------------------------------------------------------------------------

def test_client_refuses_to_fetch_without_contact(monkeypatch) -> None:
    """問い合わせ先が未設定のまま自治体のサーバを叩かせない。

    相手が問題を感じたときに連絡できないクローラを走らせてはいけない。
    """
    from tender_pipeline.core.http import ContactNotConfigured, RateLimitedClient

    client = RateLimitedClient()   # 既定の User-Agent は連絡先が未設定
    with pytest.raises(ContactNotConfigured):
        client.get('https://example.invalid/')


def test_build_user_agent_requires_env(monkeypatch) -> None:
    from tender_pipeline.core.http import (
        USER_AGENT_CONTACT_ENV,
        ContactNotConfigured,
        build_user_agent,
    )

    monkeypatch.delenv(USER_AGENT_CONTACT_ENV, raising=False)
    with pytest.raises(ContactNotConfigured):
        build_user_agent()

    monkeypatch.setenv(USER_AGENT_CONTACT_ENV, 'ops@example.com')
    ua = build_user_agent()
    assert 'ops@example.com' in ua
    assert 'tender-pipeline' in ua


def test_no_personal_email_in_source() -> None:
    """ソースに個人のメールアドレスが混入していないこと。

    公開した瞬間に永続的に露出するため、テストで固定しておく。
    """
    import re
    root = pathlib.Path(__file__).parent.parent / 'src'
    pattern = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
    allowed = {'you@example.com', 'ops@example.com'}
    for path in root.rglob('*.py'):
        for hit in pattern.findall(path.read_text(encoding='utf-8')):
            assert hit.endswith('example.com') or hit in allowed, \
                f'{path.name} にメールアドレスが埋め込まれています: {hit}'
