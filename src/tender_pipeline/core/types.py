"""コア型定義（要件定義 v3.0 §6）

設計上の約束:
  - 締切は date と time を分離する。時刻不明を 00:00 で埋めない (FR-203)
  - 予定価格は「非公表」と「0円」を区別する (§7.3-3)
  - 取得成功(件数あり) と 取得成功(0件) を別ステータスにする (FR-105①)
  - 正規化に失敗した項目は None。推測値で埋めない (FR-207)
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# ---------------------------------------------------------------------------
# 統制語彙
# ---------------------------------------------------------------------------

class BidMethod(StrEnum):
    OPEN = 'open'              # 一般競争入札
    SELECTIVE = 'selective'    # 指名競争入札
    NEGOTIATED = 'negotiated'  # 随意契約
    PROPOSAL = 'proposal'      # 企画競争・公募型プロポーザル
    UNKNOWN = 'unknown'


class TenderStatus(StrEnum):
    OPEN = 'open'
    CORRECTED = 'corrected'
    CANCELLED = 'cancelled'
    CLOSED = 'closed'


class CrawlerKind(StrEnum):
    """FR-112: 実行基盤を分けるための分類。混ぜると全体が遅い方に引きずられる。"""
    HTTP = 'http'
    BROWSER = 'browser'


class CrawlStatus(StrEnum):
    """クロール実行の結果 (FR-105①)

    OK と OK_EMPTY を分けることがサイレント故障対策の土台。
    「正常終了した」と「案件が0件だった」を同じ扱いにした瞬間に事故る。

    DISABLED を独立させているのは意図的。よくある Adapter 実装は
    「無効なら ok=True（no-op）を返す」設計になっているが、これをクローラに流用すると
    『無効化されたクローラが成功に見える』というサイレント故障そのものになる。
    """
    RUNNING = 'running'
    OK = 'ok'                # 1件以上取得できた
    OK_EMPTY = 'ok_empty'    # 正常終了したが0件 ← 異常の可能性として別枠で扱う
    PARTIAL = 'partial'      # 一部ページで失敗
    FAILED = 'failed'
    DISABLED = 'disabled'    # 設定で無効。成功ではない


#: 「成功した」と見なしてよいステータス。OK_EMPTY を含めないのが要点。
HEALTHY_STATUSES: frozenset[CrawlStatus] = frozenset({CrawlStatus.OK})


class ExtractionStatus(StrEnum):
    """添付ファイルの抽出結果 (§7.6)。対象外は「無視」ではなく必ず記録する。"""
    OK = 'ok'
    UNSUPPORTED_SCANNED = 'unsupported_scanned'  # スキャン画像PDF（テキスト層なし）
    UNSUPPORTED_FORMAT = 'unsupported_format'    # .jtd / パスワード付きZIP など
    FETCH_FAILED = 'fetch_failed'
    SKIPPED = 'skipped'


class ChangeKind(StrEnum):
    NEW = 'new'
    CORRECTED = 'corrected'
    RECALL = 'recall'
    CANCELLED = 'cancelled'


class TimeSource(StrEnum):
    EXPLICIT = 'explicit'  # 原文に時刻の記載があった
    ABSENT = 'absent'      # 原文に時刻の記載が無かった（time は None）


class TaxBasis(StrEnum):
    INCLUDED = 'included'
    EXCLUDED = 'excluded'
    UNKNOWN = 'unknown'


# ---------------------------------------------------------------------------
# 値オブジェクト
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Deadline:
    """締切。日付と時刻を必ず分離して持つ (FR-203)。

    「時刻が無い」ことを型で表現するのが目的。DATE型に丸めると通知が1日ズレる。

    ambiguous:
        原文に複数の日付が書かれていた（「○日から○日まで」「第1回…第2回…」）。
        この場合 date には最も早い日付を入れる。締切は「早い側に倒す方が安全」で、
        遅い側に倒すとユーザーが締切を過ぎてから通知を受け取ることになるため。
        UI では原文確認を促す必要がある。
    """
    date: dt.date
    time: dt.time | None
    time_source: TimeSource
    raw: str
    ambiguous: bool = False

    def __post_init__(self) -> None:
        # 不変条件: time があるのに absent、time が無いのに explicit はバグ
        if (self.time is None) != (self.time_source is TimeSource.ABSENT):
            raise ValueError(
                f'time と time_source が矛盾しています: '
                f'time={self.time!r} time_source={self.time_source!r}'
            )

    def notify_at(self) -> dt.datetime:
        """通知の基準時刻。時刻不明のときは安全側（当日9:00）に倒す (FR-203)。"""
        if self.time is not None:
            return dt.datetime.combine(self.date, self.time)
        return dt.datetime.combine(self.date, dt.time(9, 0))

    def display(self) -> str:
        """表示は「17:00必着」「時刻記載なし」を明示的に出し分ける (FR-203)。"""
        if self.time is not None:
            base = f'{self.date:%Y-%m-%d} {self.time:%H:%M}必着'
        else:
            base = f'{self.date:%Y-%m-%d}（時刻記載なし）'
        return base + '（複数日付あり・原文確認）' if self.ambiguous else base


@dataclass(frozen=True, slots=True)
class Price:
    """予定価格。undisclosed=True のとき amount は必ず None。

    0円と非公表を混同しないための表現 (§7.3-3)。
    """
    amount: int | None
    undisclosed: bool
    tax_basis: TaxBasis
    raw: str

    def __post_init__(self) -> None:
        if self.undisclosed and self.amount is not None:
            raise ValueError('非公表なのに金額が入っています')

    def in_range(self, lo: int | None, hi: int | None) -> bool:
        """金額レンジ絞り込み。

        非公表・不明は「範囲外」ではなく「判定不能」なので True を返す。
        ここで False にすると、金額が書かれていない案件を全部取りこぼす。
        """
        if self.amount is None:
            return True
        if lo is not None and self.amount < lo:
            return False
        if hi is not None and self.amount > hi:
            return False
        return True


# ---------------------------------------------------------------------------
# エンティティ
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Organization:
    id: str
    name: str
    org_type: str            # national / prefecture / municipality / agency / other
    prefecture_code: str | None
    entry_url: str
    crawler_id: str
    crawler_kind: CrawlerKind
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class RawTender:
    """クローラが返す生データ。まだ解釈していない。

    ここでは原文の文字列のまま保持し、解釈は normalize/ 層に任せる。
    クローラ側で日付や金額を解釈し始めると、サイトごとに解釈がぶれる。
    """
    organization_id: str
    source_url: str
    captured_at: dt.datetime
    title: str
    external_ref: str | None = None
    method_text: str | None = None
    announced_text: str | None = None
    bid_deadline_text: str | None = None
    application_deadline_text: str | None = None
    qa_deadline_text: str | None = None
    price_text: str | None = None
    place_text: str | None = None
    qualification_text: str | None = None
    attachment_urls: tuple[str, ...] = ()
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Tender:
    """正規化済みの案件。DBに入る形。"""
    natural_key: str
    organization_id: str
    source_url: str
    captured_at: dt.datetime
    title: str
    title_normalized: str
    method: BidMethod
    price: Price
    status: TenderStatus = TenderStatus.OPEN
    external_ref: str | None = None
    announced_date: dt.date | None = None
    bid_deadline: Deadline | None = None
    application_deadline: Deadline | None = None
    qa_deadline: Deadline | None = None
    place_prefecture_code: str | None = None
    qualification_note: str | None = None
