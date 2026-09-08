"""横浜市 入札・契約 一覧クローラ（B枠: 素直なHTML）

実データで確認したDOM構造（2026-09-07 時点）:

    div#tmp_section_info > ul.news_list > li > span.link > a[href^="/business/nyusatsu/"]

一覧に出るのは案件名とリンクのみで、締切・予定価格・入札方式は詳細ページにある。
案件名の先頭に【公募型指名競争入札】【契約結果公表】のような区分が付く。

  - 【契約結果公表】は「結果」であって募集中の案件ではない。
    §5.4 FR-401「募集終了案件は除外」に該当するため、ここで status を落とす。
  - 入札方式は案件名の角括弧から取れる場合がある。取れなければ None のままにし、
    詳細ページの解析（Phase 2）に委ねる。ここで推測しない（FR-207）。
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urljoin

from ..core.html import soup_from
from ..core.http import RateLimitedClient
from ..core.types import CrawlerKind, RawTender
from .base import CrawlerMeta, ListingCrawler, SelectorMissError

ENTRY_URL = 'https://www.city.yokohama.lg.jp/business/nyusatsu/'
ORGANIZATION_ID = 'city-yokohama'

#: 一覧コンテナ。これが見つからない＝サイト改修。0件として扱ってはいけない。
LISTING_SELECTOR = 'ul.news_list'
#: 案件リンク。一覧コンテナ内の相対リンク。
LINK_SELECTOR = 'li a[href]'

#: 案件名の先頭に付く角括弧の区分
_BRACKET_TAG = re.compile(r'^【([^】]+)】')
#: 募集中ではないことを示す区分
_RESULT_TAGS = ('契約結果公表', '入札結果', '結果公表', '中止')


class YokohamaCrawler(ListingCrawler):
    meta = CrawlerMeta(
        id='yokohama-listing',
        kind=CrawlerKind.HTTP,
        organization_id=ORGANIZATION_ID,
        organization_name='横浜市',
        entry_url=ENTRY_URL,
        structure_note=(
            f'{LISTING_SELECTOR} 配下の {LINK_SELECTOR}。'
            '一覧は案件名とリンクのみ。締切・金額は詳細ページ。'
        ),
    )

    def __init__(self, client: RateLimitedClient | None = None) -> None:
        self._client = client or RateLimitedClient()

    def fetch_listing(self, *, now: dt.datetime) -> list[RawTender]:
        res = self._client.get(ENTRY_URL)
        # res.text は使わない。このサイトは Content-Type に charset を持たないため
        # requests が ISO-8859-1 と誤認して文字化けする（core/html.py 参照）。
        return self.parse(res.content, now=now, base_url=ENTRY_URL)

    def parse(self, html: bytes | str, *, now: dt.datetime,
              base_url: str = ENTRY_URL) -> list[RawTender]:
        """HTMLを解析する。取得と分離しておくと、保存済みHTMLで再現テストできる。"""
        soup = soup_from(html)

        containers = soup.select(LISTING_SELECTOR)
        if not containers:
            # ここが要。ページは200で返ってきているのに一覧が無い＝構造変化。
            # 空リストを返すと ok_empty にすらならず「案件が無い日」に見える。
            raise SelectorMissError(
                f'一覧コンテナ {LISTING_SELECTOR!r} が見つかりません。'
                f'サイト改修の可能性があります: {base_url}'
            )

        results: list[RawTender] = []
        seen: set[str] = set()

        for container in containers:
            for anchor in container.select(LINK_SELECTOR):
                href = anchor.get('href')
                if not isinstance(href, str) or not href.strip():
                    continue
                title = anchor.get_text(strip=True)
                if not title:
                    continue

                url = urljoin(base_url, href)
                if url in seen:
                    continue
                seen.add(url)

                results.append(RawTender(
                    organization_id=ORGANIZATION_ID,
                    source_url=url,
                    captured_at=now,
                    title=title,
                    method_text=_extract_method_text(title),
                    raw_payload={
                        'listing_title': title,
                        'href': href,
                        # FR-401: 募集中でない記事はここで印を付け、取り込み側が
                        # status を落とす。判定をクローラ側に置くのは、
                        # 「何が結果公表か」がサイトごとの表記に依存するため。
                        'is_result_announcement': is_result_announcement(title),
                    },
                ))

        return results


def _extract_method_text(title: str) -> str | None:
    """案件名の先頭【】から入札方式らしき文字列を取り出す。

    取れなければ None。ここで「たぶん一般競争だろう」と埋めない（FR-207）。
    """
    m = _BRACKET_TAG.match(title)
    if not m:
        return None
    tag = m.group(1)
    return None if any(t in tag for t in _RESULT_TAGS) else tag


def is_result_announcement(title: str) -> bool:
    """募集中ではなく「結果公表」の記事か（FR-401 の除外対象）。"""
    m = _BRACKET_TAG.match(title)
    if not m:
        return False
    return any(t in m.group(1) for t in _RESULT_TAGS)
