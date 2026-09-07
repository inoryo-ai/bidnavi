"""案件の取り込み（要件定義 v2.0 FR-106 / FR-107 / FR-111）

RawTender（原文のまま） → Tender（正規化済み） → DB。

守るべき原則:
  - UPSERT 前提。同じ日を何度実行しても結果が変わらない（冪等）
  - 訂正は上書きせず tender_revision に積む。物理削除しない
  - 締切の変更は最重要情報なので、差分として必ず検出する
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from ..core.natural_key import build_natural_key
from ..core.normalize.dates import parse_deadline, parse_japanese_date
from ..core.normalize.price import parse_price
from ..core.normalize.text import normalize_text, normalize_title
from ..core.types import (
    BidMethod,
    ChangeKind,
    Deadline,
    RawTender,
    Tender,
    TenderStatus,
)

#: 差分を監視するフィールド。締切系を最優先で見る。
_WATCHED_FIELDS: tuple[str, ...] = (
    'bid_deadline_date', 'bid_deadline_time',
    'application_deadline_date', 'application_deadline_time',
    'qa_deadline_date', 'qa_deadline_time',
    'estimated_price', 'price_undisclosed', 'method', 'title', 'status',
)

#: 締切に関わるフィールド。変わったら必ず利用者に通知する必要がある。
DEADLINE_FIELDS: frozenset[str] = frozenset({
    'bid_deadline_date', 'bid_deadline_time',
    'application_deadline_date', 'application_deadline_time',
})

_METHOD_RULES: tuple[tuple[str, BidMethod], ...] = (
    ('公募型プロポーザル', BidMethod.PROPOSAL),
    ('プロポーザル', BidMethod.PROPOSAL),
    ('企画競争', BidMethod.PROPOSAL),
    ('指名競争', BidMethod.SELECTIVE),
    ('随意契約', BidMethod.NEGOTIATED),
    ('随契', BidMethod.NEGOTIATED),
    ('一般競争', BidMethod.OPEN),
    ('条件付一般競争', BidMethod.OPEN),
)


class IngestOutcome(StrEnum):
    INSERTED = 'inserted'
    UPDATED = 'updated'
    UNCHANGED = 'unchanged'


@dataclass(frozen=True, slots=True)
class IngestResult:
    natural_key: str
    outcome: IngestOutcome
    revision: int
    changed_fields: tuple[str, ...] = ()

    @property
    def deadline_changed(self) -> bool:
        """締切が変わったか。変わっていたら利用者への通知が必要。"""
        return bool(DEADLINE_FIELDS.intersection(self.changed_fields))


def classify_method(text: str | None) -> BidMethod:
    """入札方式を統制語彙に分類する。読めなければ UNKNOWN（推測しない）。"""
    if not text:
        return BidMethod.UNKNOWN
    normalized = normalize_text(text)
    for needle, method in _METHOD_RULES:
        if needle in normalized:
            return method
    return BidMethod.UNKNOWN


def normalize_tender(raw: RawTender) -> Tender:
    """RawTender を正規化して Tender にする。

    ここが唯一の解釈地点。クローラ側で日付・金額を解釈させない。
    """
    announced = parse_japanese_date(raw.announced_text)
    bid = parse_deadline(raw.bid_deadline_text)
    application = parse_deadline(raw.application_deadline_text)
    qa = parse_deadline(raw.qa_deadline_text)

    key = build_natural_key(
        raw.organization_id,
        raw.title,
        announced_date=announced,
        bid_deadline_date=bid.date if bid else None,
    )

    # FR-401: 「契約結果公表」等は募集中の案件ではない。
    # open のまま取り込むと、終了した案件を募集中として通知してしまう（§7.2-1）。
    status = (TenderStatus.CLOSED
              if raw.raw_payload.get('is_result_announcement')
              else TenderStatus.OPEN)

    return Tender(
        status=status,
        natural_key=key.value,
        organization_id=raw.organization_id,
        source_url=raw.source_url,
        captured_at=raw.captured_at,
        title=normalize_text(raw.title),
        title_normalized=key.normalized_title,
        method=classify_method(raw.method_text),
        price=parse_price(raw.price_text),
        external_ref=raw.external_ref,
        announced_date=announced,
        bid_deadline=bid,
        application_deadline=application,
        qa_deadline=qa,
        qualification_note=(normalize_text(raw.qualification_text)
                            if raw.qualification_text else None),
    )


def _deadline_columns(prefix: str, deadline: Deadline | None) -> dict[str, object]:
    if deadline is None:
        return {f'{prefix}_date': None, f'{prefix}_time': None}
    return {
        f'{prefix}_date': deadline.date.isoformat(),
        f'{prefix}_time': deadline.time.isoformat(timespec='minutes')
                          if deadline.time else None,
    }


def _to_columns(tender: Tender, key_meta: tuple[int, str]) -> dict[str, object]:
    version, anchor = key_meta
    cols: dict[str, object] = {
        'natural_key': tender.natural_key,
        'natural_key_version': version,
        'natural_key_anchor': anchor,
        'organization_id': tender.organization_id,
        'source_url': tender.source_url,
        'source_captured_at': tender.captured_at.isoformat(),
        'external_ref': tender.external_ref,
        'title': tender.title,
        'title_normalized': tender.title_normalized,
        'method': str(tender.method),
        'announced_date': (tender.announced_date.isoformat()
                           if tender.announced_date else None),
        'bid_deadline_ambiguous': int(bool(
            tender.bid_deadline and tender.bid_deadline.ambiguous)),
        'estimated_price': tender.price.amount,
        'price_undisclosed': int(tender.price.undisclosed),
        'price_tax_basis': str(tender.price.tax_basis),
        'price_raw': tender.price.raw,
        'place_prefecture_code': tender.place_prefecture_code,
        'qualification_note': tender.qualification_note,
        'status': str(tender.status),
    }
    cols.update(_deadline_columns('bid_deadline', tender.bid_deadline))
    cols.update(_deadline_columns('application_deadline', tender.application_deadline))
    cols.update(_deadline_columns('qa_deadline', tender.qa_deadline))
    return cols


def _diff(existing: sqlite3.Row, cols: dict[str, object]) -> dict[str, list[object]]:
    changes: dict[str, list[object]] = {}
    for field in _WATCHED_FIELDS:
        if field not in cols:
            continue
        before, after = existing[field], cols[field]
        if before != after:
            changes[field] = [before, after]
    return changes


def ingest_tender(
    conn: sqlite3.Connection,
    raw: RawTender,
    *,
    now: dt.datetime,
) -> IngestResult:
    """1件を取り込む。同じ入力を何度流しても結果が変わらない（冪等）。

    既存と差分があれば revision を1つ進め、tender_revision に履歴を積む。
    既存レコードは上書きするが、変更前の値は履歴に残るので復元できる。
    """
    tender = normalize_tender(raw)
    key = build_natural_key(
        raw.organization_id, raw.title,
        announced_date=tender.announced_date,
        bid_deadline_date=tender.bid_deadline.date if tender.bid_deadline else None,
    )
    from ..core.natural_key import NATURAL_KEY_VERSION
    cols = _to_columns(tender, (NATURAL_KEY_VERSION, str(key.anchor_kind)))

    existing = conn.execute(
        'SELECT * FROM tender WHERE natural_key = ?', (tender.natural_key,)
    ).fetchone()

    if existing is None:
        cols['current_revision'] = 1
        cols['first_seen_at'] = now.isoformat()
        cols['last_seen_at'] = now.isoformat()
        placeholders = ', '.join('?' * len(cols))
        conn.execute(
            f'INSERT INTO tender ({", ".join(cols)}) VALUES ({placeholders})',
            tuple(cols.values()),
        )
        conn.execute(
            """
            INSERT INTO tender_revision
                (natural_key, revision, captured_at, change_kind, diff_json, raw_payload_json)
            VALUES (?, 1, ?, ?, '{}', ?)
            """,
            (tender.natural_key, now.isoformat(), str(ChangeKind.NEW),
             json.dumps(raw.raw_payload, ensure_ascii=False, default=str)),
        )
        conn.commit()
        return IngestResult(tender.natural_key, IngestOutcome.INSERTED, 1)

    changes = _diff(existing, cols)
    if not changes:
        # 内容は同じでも「今日も見えていた」ことは記録する。
        # last_seen_at が止まっている＝案件が消えた、を検知するため。
        conn.execute(
            'UPDATE tender SET last_seen_at = ? WHERE natural_key = ?',
            (now.isoformat(), tender.natural_key),
        )
        conn.commit()
        return IngestResult(tender.natural_key, IngestOutcome.UNCHANGED,
                            int(existing['current_revision']))

    revision = int(existing['current_revision']) + 1
    change_kind = (ChangeKind.CANCELLED
                   if cols.get('status') == str(TenderStatus.CANCELLED)
                   else ChangeKind.CORRECTED)

    cols['current_revision'] = revision
    cols['last_seen_at'] = now.isoformat()
    assignments = ', '.join(f'{c} = ?' for c in cols)
    conn.execute(
        f'UPDATE tender SET {assignments} WHERE natural_key = ?',
        (*cols.values(), tender.natural_key),
    )
    conn.execute(
        """
        INSERT INTO tender_revision
            (natural_key, revision, captured_at, change_kind, diff_json, raw_payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (tender.natural_key, revision, now.isoformat(), str(change_kind),
         json.dumps(changes, ensure_ascii=False, default=str),
         json.dumps(raw.raw_payload, ensure_ascii=False, default=str)),
    )
    conn.commit()
    return IngestResult(tender.natural_key, IngestOutcome.UPDATED, revision,
                        tuple(changes))
