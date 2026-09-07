"""クローラの共通インターフェース（要件定義 v2.0 FR-105 / FR-112）

一般的な Adapter パターンを踏襲するが、**1点だけ意図的に変えている**。

  よくある実装: 無効なアダプタは ok=True（no-op）を返す
  bidnavi:      無効・0件は成功として返さない

理由は、入札クローラにとって「静かに何も返さない」ことが最悪の事故だから。
「例外が出なかった＝正常」という判定をこの層で一切行わない。

HTTP群とブラウザ群は同じ ListingCrawler インターフェースに従うので、
上位のパイプラインからは区別なく扱える。実行基盤だけを分ける。
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core.types import CrawlerKind, RawTender


class CrawlerError(RuntimeError):
    """クローラの失敗。握りつぶさず上位に伝える。"""


class SelectorMissError(CrawlerError):
    """ページは取れたが、期待した要素が1つも見つからなかった。

    これを「0件」として静かに返すのがサイレント故障の入口。
    ページ取得に成功しているぶん、通信エラーより発見が遅れて危険なので、
    通常の0件（本当に案件が無い）とは区別して例外にする。

    「本当に0件」と区別できるように、クローラは
    『一覧コンテナは存在するが行が0』のときだけ空リストを返し、
    『一覧コンテナ自体が見つからない』ときはこの例外を送出する。
    """


@dataclass(frozen=True, slots=True)
class CrawlerMeta:
    id: str
    kind: CrawlerKind
    organization_id: str
    organization_name: str
    entry_url: str
    #: このクローラが依拠しているDOM構造。壊れたときに何を直すか分かるようにする。
    structure_note: str


class ListingCrawler(ABC):
    """一覧ページから案件を取り出すクローラ。"""

    meta: CrawlerMeta

    @abstractmethod
    def fetch_listing(self, *, now: dt.datetime) -> list[RawTender]:
        """一覧を取得して RawTender のリストを返す。

        :raises SelectorMissError: 一覧コンテナ自体が見つからなかった場合
        :raises CrawlerError: その他の取得・解析失敗

        戻り値が空リストになるのは「一覧はあるが行が0件」のときだけ。
        """


class CrawlerRegistry:
    """クローラの登録簿。機関IDから解決する。"""

    def __init__(self) -> None:
        self._by_id: dict[str, ListingCrawler] = {}

    def register(self, crawler: ListingCrawler) -> None:
        if crawler.meta.id in self._by_id:
            raise ValueError(f'クローラIDが重複しています: {crawler.meta.id}')
        self._by_id[crawler.meta.id] = crawler

    def get(self, crawler_id: str) -> ListingCrawler:
        try:
            return self._by_id[crawler_id]
        except KeyError:
            raise KeyError(f'未登録のクローラです: {crawler_id}') from None

    def ids(self) -> list[str]:
        return sorted(self._by_id)

    def by_kind(self, kind: CrawlerKind) -> list[ListingCrawler]:
        """FR-112: 実行基盤を分けるための取り出し。

        HTTP群は並列度を上げられるが、ブラウザ群は逐次実行する。
        混ぜると全体が遅い方に引きずられる。
        """
        return [c for c in self._by_id.values() if c.meta.kind is kind]


registry = CrawlerRegistry()
