"""案件の同一性判定キー（要件定義 v2.0 FR-106）

入札案件には全国共通のIDが存在しない。同じ案件が
「公告」→「訂正公告」→「再公告」と複数回出たり、
県サイトと市サイトの両方に載ったりする。

ここを後から直すと全レコード再構築になるため、収集開始前に確定させる。

キーの構成:
    sha256(organization_id \x00 normalized_title \x00 anchor_date)

anchor_date の選び方:
    公告日 > 入札締切日 > (なし)
    どちらも無ければ空文字とし、タイトル＋機関のみでキーを作る。
    この場合は衝突リスクがあるため、呼び出し側で警告できるよう
    build_natural_key() は使用した anchor の種別も返す。
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from enum import StrEnum

from .normalize.text import normalize_title

_SEP = '\x00'

#: natural_key の生成規則を変えたら必ずこれを上げる。
#: DB側にも保存し、バージョン不一致を検知して再計算のトリガーにする。
NATURAL_KEY_VERSION = 1


class EmptyTitleError(ValueError):
    """案件名が正規化後に空になった。抽出失敗として扱う。

    「例外を握りつぶさない」方針に従い、ここでは None を返さず送出する。
    黙って空キーを作ると全案件が1件に潰れ、サイレント故障になる。
    """


class AnchorKind(StrEnum):
    ANNOUNCED = 'announced'      # 公告日を使った（最も安定）
    BID_DEADLINE = 'bid_deadline'
    NONE = 'none'                # 日付が一つも取れなかった（衝突リスクあり）


@dataclass(frozen=True, slots=True)
class NaturalKey:
    value: str
    anchor_kind: AnchorKind
    normalized_title: str

    @property
    def is_weak(self) -> bool:
        """日付アンカーが無いキーは弱い。監視対象にする。"""
        return self.anchor_kind is AnchorKind.NONE


def choose_anchor(
    announced_date: dt.date | None,
    bid_deadline_date: dt.date | None,
) -> tuple[dt.date | None, AnchorKind]:
    """アンカー日付を選ぶ。

    公告日を最優先にする理由: 締切は訂正公告で変わることがあるため、
    締切をキーに含めると「締切が延びた同じ案件」が別案件になってしまう。
    """
    if announced_date is not None:
        return announced_date, AnchorKind.ANNOUNCED
    if bid_deadline_date is not None:
        return bid_deadline_date, AnchorKind.BID_DEADLINE
    return None, AnchorKind.NONE


def build_natural_key(
    organization_id: str,
    title: str,
    *,
    announced_date: dt.date | None = None,
    bid_deadline_date: dt.date | None = None,
) -> NaturalKey:
    """案件の natural_key を組み立てる。

    >>> a = build_natural_key('jp-mlit', '【入札公告】令和８年度　○○業務　その１について',
    ...                       announced_date=dt.date(2026, 3, 1))
    >>> b = build_natural_key('jp-mlit', '○○業務その1',
    ...                       announced_date=dt.date(2026, 3, 1))
    >>> a.value == b.value          # 表記ゆれを吸収して同一と判定
    True

    :raises EmptyTitleError: 正規化後のタイトルが空になった場合。
        タイトルが取れていない案件はパース失敗であり、キーを作ってはいけない。
        空文字でハッシュすると「同一機関・同一日付の全案件」が1件に潰れる。
        ここで握りつぶさず送出し、crawl_run に失敗として記録させる。
    """
    normalized = normalize_title(title)
    if not normalized:
        raise EmptyTitleError(
            f'案件名が正規化後に空になりました。抽出失敗の可能性があります: '
            f'organization_id={organization_id!r} title={title!r}'
        )
    anchor_date, anchor_kind = choose_anchor(announced_date, bid_deadline_date)
    anchor = anchor_date.isoformat() if anchor_date is not None else ''

    payload = _SEP.join((str(NATURAL_KEY_VERSION), organization_id, normalized, anchor))
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()

    return NaturalKey(value=digest, anchor_kind=anchor_kind, normalized_title=normalized)
