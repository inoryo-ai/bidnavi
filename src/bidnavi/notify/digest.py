"""日次ダイジェスト（要件定義 v2.0 FR-306 / FR-307 / 指摘#10「通知疲れ」）

関連度の低い案件が毎日20件届くと、ユーザーは3日で見なくなる。
機能ではなく設計思想の問題なので、既定形を「1日1通・件数上限つき」にする。

ただし上限で切るときに、**関連度の高い案件を切ってはいけない**。
並び順は「締切が近い × 関連度が高い」を優先し、
切り捨てた分は件数だけ明示する（黙って消さない）。
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

from ..pipeline.lifecycle import is_closed

#: 1通あたりの既定の掲載件数上限
DEFAULT_MAX_ITEMS = 10
#: 既定の通知スコア閾値
DEFAULT_MIN_SCORE = 50

DISCLAIMER = (
    '※ 本内容はAIによる推定を含みます。情報の正確性・網羅性は保証されません。'
    '応札の判断前に必ず発注機関の原文をご確認ください。'
)


@dataclass(frozen=True, slots=True)
class DigestItem:
    natural_key: str
    title: str
    organization_name: str
    score: int
    reason: str
    source_url: str
    deadline_display: str
    days_left: int | None
    price_display: str

    def render(self) -> str:
        return (
            f'[関連度{self.score:3d}] {self.title}\n'
            f'  機関   : {self.organization_name}\n'
            f'  締切   : {self.deadline_display}'
            + (f'（あと{self.days_left}日）' if self.days_left is not None else '') + '\n'
            f'  予定価格: {self.price_display}\n'
            f'  理由   : {self.reason}\n'
            f'  原文   : {self.source_url}'
        )


@dataclass(frozen=True, slots=True)
class Digest:
    company_id: str
    generated_at: dt.datetime
    items: tuple[DigestItem, ...]
    omitted_count: int
    total_matched: int

    @property
    def is_empty(self) -> bool:
        return not self.items

    def render(self) -> str:
        header = f'【bidnavi】{self.generated_at:%Y-%m-%d} の新着案件'
        if self.is_empty:
            return (f'{header}\n\n'
                    '本日は条件に合う新着案件がありませんでした。\n\n'
                    f'{DISCLAIMER}')

        body = '\n\n'.join(item.render() for item in self.items)
        footer = ''
        if self.omitted_count:
            # 黙って切らない。切ったことを必ず伝える。
            footer = (f'\n\nほか {self.omitted_count} 件は本メールに掲載していません'
                      f'（該当 {self.total_matched} 件中 {len(self.items)} 件を表示）。'
                      'すべての案件は管理画面でご確認ください。')
        return f'{header}（{self.total_matched}件）\n\n{body}{footer}\n\n{DISCLAIMER}'


def _price_display(amount: int | None, undisclosed: bool) -> str:
    """§7.3-3: 非公表を 0円 と表示しない。"""
    if undisclosed:
        return '非公表'
    if amount is None:
        return '記載なし'
    return f'{amount:,}円'


def _deadline_display(date_str: str | None, time_str: str | None) -> str:
    if date_str is None:
        return '記載なし'
    if time_str is None:
        return f'{date_str}（時刻記載なし）'
    return f'{date_str} {time_str}必着'


def build_digest(
    conn: sqlite3.Connection,
    company_id: str,
    *,
    now: dt.datetime,
    max_items: int = DEFAULT_MAX_ITEMS,
    min_score: int = DEFAULT_MIN_SCORE,
) -> Digest:
    """通知対象を集めてダイジェストを組み立てる。

    締切を過ぎた案件は含めない。ただし判定は lifecycle.is_closed に委ね、
    「時刻不明なら当日23:59まで有効」という規則をここで再実装しない。
    """
    rows = conn.execute(
        """
        SELECT m.natural_key, m.score, m.reason,
               t.title, t.source_url, t.bid_deadline_date, t.bid_deadline_time,
               t.estimated_price, t.price_undisclosed, t.status,
               o.name AS organization_name
        FROM match_result m
        JOIN tender t ON t.natural_key = m.natural_key
        JOIN organization o ON o.id = t.organization_id
        WHERE m.company_id = ? AND m.should_notify = 1 AND m.score >= ?
        """,
        (company_id, min_score),
    ).fetchall()

    items: list[DigestItem] = []
    for r in rows:
        deadline_date = (dt.date.fromisoformat(r['bid_deadline_date'])
                         if r['bid_deadline_date'] else None)
        deadline_time = (dt.time.fromisoformat(r['bid_deadline_time'])
                         if r['bid_deadline_time'] else None)
        # 募集中（open）以外は通知しない。
        # cancelled だけを弾く書き方だと、結果公表として closed にした案件や
        # 締切超過で closed にした案件が通知に載る（§7.2-1）。
        # 「除外リスト」ではなく「許可リスト」で書く。
        if r['status'] != 'open':
            continue
        if is_closed(deadline_date, deadline_time, now=now):
            continue

        days_left = (deadline_date - now.date()).days if deadline_date else None
        items.append(DigestItem(
            natural_key=r['natural_key'],
            title=r['title'],
            organization_name=r['organization_name'],
            score=int(r['score']),
            reason=r['reason'],
            source_url=r['source_url'],
            deadline_display=_deadline_display(r['bid_deadline_date'],
                                               r['bid_deadline_time']),
            days_left=days_left,
            price_display=_price_display(r['estimated_price'],
                                         bool(r['price_undisclosed'])),
        ))

    # 締切が近い順 → 関連度が高い順。締切不明は最後に回す。
    items.sort(key=lambda i: (i.days_left if i.days_left is not None else 10**6,
                              -i.score))

    shown = tuple(items[:max_items])
    return Digest(
        company_id=company_id,
        generated_at=now,
        items=shown,
        omitted_count=max(0, len(items) - len(shown)),
        total_matched=len(items),
    )
