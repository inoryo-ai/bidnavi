"""事業者プロフィール（要件定義 v2.0 FR-301）

マッチングの条件。これは事業者の営業戦略そのものなので、
本番では会社単位で厳格に分離する（NFR-107 / RLS）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CompanyProfile:
    company_id: str
    #: 業種・営業品目
    categories: tuple[str, ...] = ()
    #: 対応エリア（都道府県コード）
    prefectures: tuple[str, ...] = ()
    #: 保有資格
    qualifications: tuple[str, ...] = ()
    #: 拾いたいキーワード。1つでも当たれば必ず通知する（FR-305）
    keywords: tuple[str, ...] = ()
    #: 明示的に除外したいキーワード
    ng_keywords: tuple[str, ...] = ()
    price_min: int | None = None
    price_max: int | None = None
    #: 3段目（LLM）の月間実行上限（§7.1a）
    llm_monthly_cap: int = 50

    @classmethod
    def load(cls, conn: sqlite3.Connection, company_id: str) -> CompanyProfile:
        row = conn.execute(
            """
            SELECT p.*, c.llm_monthly_cap
            FROM company_profile p JOIN company c ON c.id = p.company_id
            WHERE p.company_id = ?
            """,
            (company_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f'プロフィールが未登録です: {company_id}')
        return cls(
            company_id=company_id,
            categories=tuple(json.loads(row['categories'])),
            prefectures=tuple(json.loads(row['prefectures'])),
            qualifications=tuple(json.loads(row['qualifications'])),
            keywords=tuple(json.loads(row['keywords'])),
            ng_keywords=tuple(json.loads(row['ng_keywords'])),
            price_min=row['price_min'],
            price_max=row['price_max'],
            llm_monthly_cap=int(row['llm_monthly_cap']),
        )

    def save(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            'INSERT OR IGNORE INTO company (id, name) VALUES (?, ?)',
            (self.company_id, self.company_id),
        )
        conn.execute(
            'UPDATE company SET llm_monthly_cap = ? WHERE id = ?',
            (self.llm_monthly_cap, self.company_id),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO company_profile
                (company_id, categories, prefectures, qualifications,
                 keywords, ng_keywords, price_min, price_max)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (self.company_id,
             json.dumps(list(self.categories), ensure_ascii=False),
             json.dumps(list(self.prefectures), ensure_ascii=False),
             json.dumps(list(self.qualifications), ensure_ascii=False),
             json.dumps(list(self.keywords), ensure_ascii=False),
             json.dumps(list(self.ng_keywords), ensure_ascii=False),
             self.price_min, self.price_max),
        )
        conn.commit()
