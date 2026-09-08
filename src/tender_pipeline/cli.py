"""入札案件情報パイプライン CLI（技術検証フェーズの操作口）

    python -m tender_pipeline.cli init                  DBを作る
    python -m tender_pipeline.cli crawl                 クロール→取り込み
    python -m tender_pipeline.cli crawl --offline       保存済みHTMLで実行（通信しない）
    python -m tender_pipeline.cli health                サイレント故障の判定
    python -m tender_pipeline.cli match --company co-1  マッチング実行
    python -m tender_pipeline.cli digest --company co-1 日次ダイジェストを出力
    python -m tender_pipeline.cli funnel --company co-1 3段フィルタの通過率とコスト
    python -m tender_pipeline.cli evaluate <cases.jsonl> --company co-1  精度計測
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

from .core.db import connect
from .core.http import ContactNotConfigured, RateLimitedClient, build_user_agent
from .core.normalize.price import parse_price
from .core.types import Price, TaxBasis
from .crawlers.base import CrawlerError
from .crawlers.yokohama import YokohamaCrawler
from .evaluation.metrics import Prediction, load_cases, misses, reachability, score
from .match.engine import evaluate, funnel_stats
from .match.llm import BudgetedLlmJudge, StubLlmClient
from .match.profile import CompanyProfile
from .notify.digest import build_digest
from .pipeline.health import evaluate_all
from .pipeline.ingest import ingest_tender
from .pipeline.lifecycle import close_expired, find_disappeared
from .pipeline.run import CrawlSession

DEFAULT_DB = 'tender.db'
FIXTURE = (pathlib.Path(__file__).parent.parent.parent
           / 'tests' / 'fixtures' / 'yokohama_listing.html')

ORGANIZATIONS = [
    ('city-yokohama', '横浜市', 'municipality', '14',
     'https://www.city.yokohama.lg.jp/business/nyusatsu/',
     'yokohama-listing', 'http'),
]


def cmd_init(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    for row in ORGANIZATIONS:
        conn.execute(
            """INSERT OR REPLACE INTO organization
               (id, name, org_type, prefecture_code, entry_url, crawler_id, crawler_kind)
               VALUES (?, ?, ?, ?, ?, ?, ?)""", row)
    conn.commit()
    print(f'初期化しました: {args.db}（機関 {len(ORGANIZATIONS)} 件）')
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    now = dt.datetime.now()

    if args.offline:
        crawler = YokohamaCrawler()
    else:
        # CR-102: 連絡先の無い User-Agent で自治体のサーバを叩かない
        try:
            crawler = YokohamaCrawler(
                RateLimitedClient(user_agent=build_user_agent()))
        except ContactNotConfigured as exc:
            print(f'{exc}', file=sys.stderr)
            print('（通信せずに試すなら --offline を付けてください）', file=sys.stderr)
            return 2
    org_id = crawler.meta.organization_id

    exit_code = 0
    try:
        with CrawlSession(conn, org_id, now.date(), now) as session:
            if args.offline:
                if not FIXTURE.exists():
                    raise CrawlerError(f'フィクスチャがありません: {FIXTURE}')
                items = crawler.parse(FIXTURE.read_bytes(), now=now)
            else:
                items = crawler.fetch_listing(now=now)

            inserted = updated = 0
            for raw in items:
                result = ingest_tender(conn, raw, now=now)
                inserted += result.outcome == 'inserted'
                updated += result.outcome == 'updated'
                if result.deadline_changed:
                    print(f'  ⚠ 締切変更: {raw.title[:40]} {result.changed_fields}')
            session.record(fetched=len(items), inserted=inserted, updated=updated)
            print(f'{crawler.meta.organization_name}: 取得 {len(items)} 件 '
                  f'（新規 {inserted} / 更新 {updated}）')
    except CrawlerError as exc:
        # 握りつぶさない。失敗として終了コードに出す。
        print(f'クロール失敗: {type(exc).__name__}: {exc}', file=sys.stderr)
        exit_code = 1

    closed = close_expired(conn, now=now)
    if closed:
        print(f'締切超過で closed にした案件: {closed} 件')
    return exit_code


def cmd_health(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    now = dt.datetime.now()
    reports = evaluate_all(conn, now=now)
    if not reports:
        print('機関が登録されていません。先に init を実行してください。')
        return 1

    alerts = 0
    for r in reports:
        mark = '🔴' if r.is_alert else '🟢'
        alerts += r.is_alert
        print(f'{mark} {r.organization_id:20} {r.state:12} {r.reason}')
        if r.baseline_avg is not None:
            print(f'     直近={r.latest_count} 平均={r.baseline_avg:.1f} '
                  f'(n={r.baseline_samples})')

    for org_id in [r.organization_id for r in reports]:
        gone = find_disappeared(conn, org_id, now=now)
        for g in gone:
            print(f'🟡 {org_id}: 締切前に一覧から消えました '
                  f'({g.days_missing}日) {g.title[:40]}')

    print(f'\nアラート {alerts} 件 / 機関 {len(reports)} 件')
    return 1 if alerts else 0


def _load_or_seed_profile(conn: sqlite3.Connection, company_id: str) -> CompanyProfile:
    try:
        return CompanyProfile.load(conn, company_id)
    except KeyError:
        profile = CompanyProfile(
            company_id=company_id,
            categories=('業務システム開発', 'ソフトウェア開発', '情報処理'),
            prefectures=('14',),
            keywords=('システム', 'ソフトウェア', '情報', 'デジタル'),
            ng_keywords=('警備', '給食'),
            price_min=None, price_max=None,
            llm_monthly_cap=50,
        )
        profile.save(conn)
        print(f'プロフィールが無いので既定値で作成しました: {company_id}')
        return profile


def cmd_match(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    now = dt.datetime.now()
    profile = _load_or_seed_profile(conn, args.company)
    judge = BudgetedLlmJudge(conn, StubLlmClient(score=75)) if args.stub_llm else None
    if judge is None:
        print('※ LLM段は無効です（--stub-llm で疑似実行できます）。'
              '実測には ANTHROPIC_API_KEY が必要です。')

    rows = conn.execute(
        """SELECT natural_key, title, estimated_price, price_undisclosed,
                  price_tax_basis, price_raw, place_prefecture_code
           FROM tender WHERE status = 'open'"""
    ).fetchall()

    notified = 0
    for r in rows:
        price = Price(amount=r['estimated_price'],
                      undisclosed=bool(r['price_undisclosed']),
                      tax_basis=TaxBasis(r['price_tax_basis']),
                      raw=r['price_raw'] or '')
        outcome = evaluate(
            conn=conn, natural_key=r['natural_key'], title=r['title'],
            profile=profile, price=price,
            prefecture_code=r['place_prefecture_code'],
            spec_text=None, judge=judge, now=now)
        notified += outcome.should_notify
        if outcome.llm_overridden:
            print(f'  ⚠ AI判定を上書き（キーワード一致のため通知維持）: {r["title"][:40]}')

    print(f'評価 {len(rows)} 件 / 通知対象 {notified} 件')
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    digest = build_digest(conn, args.company, now=dt.datetime.now(),
                          max_items=args.max_items, min_score=args.min_score)
    print(digest.render())
    return 0


def cmd_funnel(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    s = funnel_stats(conn, args.company)
    print(f'評価総数        : {s.total}')
    print(f'2段目到達       : {s.reached_stage2}')
    print(f'3段目到達(LLM)  : {s.reached_stage3}  '
          f'({s.stage3_ratio:.1%} / 設計目標 1%)')
    print(f'  うち課金      : {s.llm_calls_billed}')
    print(f'  うちキャッシュ: {s.cache_hits}')
    print(f'累計コスト      : {s.total_cost_jpy:.2f} 円')
    print(f'通知対象        : {s.notified}')
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    conn = connect(':memory:')
    cases = load_cases(args.cases)
    profile = _load_or_seed_profile(conn, args.company)
    judge = BudgetedLlmJudge(conn, StubLlmClient(score=75)) if args.stub_llm else None
    now = dt.datetime.now()

    conn.execute(
        """INSERT OR IGNORE INTO organization
           (id, name, org_type, entry_url, crawler_id, crawler_kind)
           VALUES ('eval', '評価用', 'other', 'https://e.invalid/', 'x', 'http')""")

    predictions: list[Prediction] = []
    for case in cases:
        conn.execute(
            """INSERT OR REPLACE INTO tender
               (natural_key, natural_key_version, natural_key_anchor, organization_id,
                source_url, source_captured_at, title, title_normalized, method,
                first_seen_at, last_seen_at)
               VALUES (?, 1, 'announced', 'eval', 'https://e.invalid/1', ?, ?, ?,
                       'open', ?, ?)""",
            (case.case_id, now.isoformat(), case.title, case.title,
             now.isoformat(), now.isoformat()))
        outcome = evaluate(
            conn=conn, natural_key=case.case_id, title=case.title, profile=profile,
            price=parse_price(case.price_text), prefecture_code=case.prefecture_code,
            spec_text=case.spec_text, judge=judge, now=now)
        predictions.append(Prediction(
            case.case_id, outcome.should_notify,
            had_a_chance=outcome.should_notify or int(outcome.stage_reached) >= 3))

    metrics = score(cases, predictions)
    print(metrics.report())

    reach = reachability(cases, predictions)
    print()
    print('--- 構造的な取りこぼし（LLMの精度に依存しない指標） ---')
    print(f'関係ある案件      : {reach.relevant_total}')
    print(f'判断の機会を得た  : {reach.relevant_with_chance}')
    print(f'到達率(再現率上限): {reach.coverage:.1%}')
    print(f'構造的取りこぼし  : {reach.structural_miss_rate:.1%}  '
          f'← AIをどれだけ良くしても拾えない分''')

    missed = misses(cases, predictions)
    if missed:
        print(f'\n--- 取りこぼし {len(missed)} 件（ここを1件ずつ潰す） ---')
        for m in missed:
            print(f'  {m.case_id}: {m.title}')
    return 0 if metrics.meets_targets() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='tender-pipeline', description=__doc__)
    parser.add_argument('--db', default=DEFAULT_DB)
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('init').set_defaults(func=cmd_init)

    p_crawl = sub.add_parser('crawl')
    p_crawl.add_argument('--offline', action='store_true',
                         help='保存済みHTMLで実行（通信しない）')
    p_crawl.set_defaults(func=cmd_crawl)

    sub.add_parser('health').set_defaults(func=cmd_health)

    p_match = sub.add_parser('match')
    p_match.add_argument('--company', default='co-demo')
    p_match.add_argument('--stub-llm', action='store_true')
    p_match.set_defaults(func=cmd_match)

    p_digest = sub.add_parser('digest')
    p_digest.add_argument('--company', default='co-demo')
    p_digest.add_argument('--max-items', type=int, default=10)
    p_digest.add_argument('--min-score', type=int, default=50)
    p_digest.set_defaults(func=cmd_digest)

    p_funnel = sub.add_parser('funnel')
    p_funnel.add_argument('--company', default='co-demo')
    p_funnel.set_defaults(func=cmd_funnel)

    p_eval = sub.add_parser('evaluate')
    p_eval.add_argument('cases')
    p_eval.add_argument('--company', default='co-demo')
    p_eval.add_argument('--stub-llm', action='store_true')
    p_eval.set_defaults(func=cmd_evaluate)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
