"""SQLite スキーマ（要件定義 v2.0 §6）

技術検証フェーズは sqlite3（標準ライブラリ・ネイティブ依存なし）で動かすが、
スキーマは本番の Supabase/PostgreSQL に1対1で移植できる形に保つ。

設計上の要点:
  - tender の主キーは natural_key（FR-106）
  - 訂正は上書きせず tender_revision に積む。物理削除しない（FR-107）
  - 締切は date と time を別カラムにする（FR-203）
  - crawl_run は (organization_id, target_date, attempt) で一意（FR-111）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    org_type        TEXT NOT NULL,
    prefecture_code TEXT,
    entry_url       TEXT NOT NULL,
    crawler_id      TEXT NOT NULL,
    -- FR-112: http / browser で実行基盤を分ける
    crawler_kind    TEXT NOT NULL CHECK (crawler_kind IN ('http', 'browser')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    notes           TEXT
);

-- FR-111: サイト×日付単位で再実行できるようにする。書き込みは全て UPSERT 前提。
CREATE TABLE IF NOT EXISTS crawl_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id TEXT NOT NULL REFERENCES organization(id),
    target_date     TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 1,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    -- FR-105①: ok と ok_empty を別ステータスにする。disabled も成功ではない。
    status          TEXT NOT NULL
                    CHECK (status IN ('running','ok','ok_empty','partial','failed','disabled')),
    fetched_count   INTEGER NOT NULL DEFAULT 0,
    inserted_count  INTEGER NOT NULL DEFAULT 0,
    updated_count   INTEGER NOT NULL DEFAULT 0,
    error_kind      TEXT,
    error_message   TEXT,
    UNIQUE (organization_id, target_date, attempt)
);

CREATE INDEX IF NOT EXISTS idx_crawl_run_org_date
    ON crawl_run (organization_id, target_date DESC);

CREATE TABLE IF NOT EXISTS tender (
    natural_key            TEXT PRIMARY KEY,
    natural_key_version    INTEGER NOT NULL,
    natural_key_anchor     TEXT NOT NULL,
    organization_id        TEXT NOT NULL REFERENCES organization(id),
    source_url             TEXT NOT NULL,
    source_captured_at     TEXT NOT NULL,
    external_ref           TEXT,
    title                  TEXT NOT NULL,
    title_normalized       TEXT NOT NULL,
    method                 TEXT NOT NULL,
    announced_date         TEXT,
    -- FR-203: 日付と時刻を分離。時刻不明を 00:00 で埋めない。
    qa_deadline_date       TEXT,
    qa_deadline_time       TEXT,
    application_deadline_date TEXT,
    application_deadline_time TEXT,
    bid_deadline_date      TEXT,
    bid_deadline_time      TEXT,
    bid_deadline_ambiguous INTEGER NOT NULL DEFAULT 0,
    -- §7.3-3: 非公表と 0円 を混同しない
    estimated_price        INTEGER,
    price_undisclosed      INTEGER NOT NULL DEFAULT 0,
    price_tax_basis        TEXT NOT NULL DEFAULT 'unknown',
    price_raw              TEXT,
    place_prefecture_code  TEXT,
    qualification_note     TEXT,
    status                 TEXT NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open','corrected','cancelled','closed')),
    current_revision       INTEGER NOT NULL DEFAULT 1,
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL,
    CHECK (NOT (price_undisclosed = 1 AND estimated_price IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_tender_org      ON tender (organization_id);
CREATE INDEX IF NOT EXISTS idx_tender_deadline ON tender (bid_deadline_date);

-- FR-107: 訂正・再公告は上書きせず履歴を積む。物理削除しない。
CREATE TABLE IF NOT EXISTS tender_revision (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    natural_key     TEXT NOT NULL REFERENCES tender(natural_key),
    revision        INTEGER NOT NULL,
    captured_at     TEXT NOT NULL,
    change_kind     TEXT NOT NULL
                    CHECK (change_kind IN ('new','corrected','recall','cancelled')),
    diff_json       TEXT NOT NULL DEFAULT '{}',
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (natural_key, revision)
);

CREATE TABLE IF NOT EXISTS company (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    plan       TEXT NOT NULL DEFAULT 'trial',
    -- §7.1a: 3段目(LLM)の月間実行上限。予算ガードレール。
    llm_monthly_cap INTEGER NOT NULL DEFAULT 50
);

CREATE TABLE IF NOT EXISTS company_profile (
    company_id     TEXT PRIMARY KEY REFERENCES company(id),
    categories     TEXT NOT NULL DEFAULT '[]',
    prefectures    TEXT NOT NULL DEFAULT '[]',
    qualifications TEXT NOT NULL DEFAULT '[]',
    keywords       TEXT NOT NULL DEFAULT '[]',
    ng_keywords    TEXT NOT NULL DEFAULT '[]',
    price_min      INTEGER,
    price_max      INTEGER
);

-- §7.1a: どの段まで到達したか、いくらかかったかを毎回記録する。
-- 予算管理はこの実測値で回す。
CREATE TABLE IF NOT EXISTS match_result (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    natural_key       TEXT NOT NULL REFERENCES tender(natural_key),
    company_id        TEXT NOT NULL REFERENCES company(id),
    evaluated_at      TEXT NOT NULL,
    stage_reached     INTEGER NOT NULL CHECK (stage_reached BETWEEN 1 AND 3),
    score             INTEGER NOT NULL,
    reason            TEXT NOT NULL DEFAULT '',
    matched_by        TEXT NOT NULL CHECK (matched_by IN ('rule','vector','llm','none')),
    should_notify     INTEGER NOT NULL DEFAULT 0,
    llm_input_tokens  INTEGER NOT NULL DEFAULT 0,
    llm_output_tokens INTEGER NOT NULL DEFAULT 0,
    llm_cost_jpy      REAL NOT NULL DEFAULT 0.0,
    llm_cache_hit     INTEGER NOT NULL DEFAULT 0,
    feedback          TEXT,
    UNIQUE (natural_key, company_id)
);

CREATE INDEX IF NOT EXISTS idx_match_company_month
    ON match_result (company_id, evaluated_at);

-- LLM 呼び出しの永続キャッシュ。
-- 社内の既存実装は TTL 30分のインメモリキャッシュだったが、
-- 仕様書の中身は変わらないので TTL は不要。むしろ永続化しないと
-- 再公告のたびに同じPDFへ課金することになる。
CREATE TABLE IF NOT EXISTS llm_cache (
    cache_key     TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    hit_count     INTEGER NOT NULL DEFAULT 0,
    response_json TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);

-- FR-108: 添付の実体は保存しない。URL と解析結果だけ持つ。
CREATE TABLE IF NOT EXISTS attachment (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    natural_key       TEXT NOT NULL REFERENCES tender(natural_key),
    file_url          TEXT NOT NULL,
    file_ext          TEXT,
    byte_size         INTEGER,
    -- §7.6: 対象外形式は「無視」ではなく必ず記録する
    extraction_status TEXT NOT NULL
                      CHECK (extraction_status IN
                             ('ok','unsupported_scanned','unsupported_format',
                              'fetch_failed','skipped')),
    extracted_text    TEXT,
    extracted_chars   INTEGER,
    UNIQUE (natural_key, file_url)
);
"""


def connect(path: str | Path = ':memory:') -> sqlite3.Connection:
    """接続を作りスキーマを適用する。

    detect_types は使わない。日付は全て ISO 文字列で保持し、
    Python 側の型変換を1箇所（core/types）に集約する。
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(
        'INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)',
        ('schema_version', str(SCHEMA_VERSION)),
    )
    conn.commit()
    return conn
