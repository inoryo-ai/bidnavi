"""マッチングのテスト（Loop 4 / 天城）

最重点は FR-305:
  コスト削減のフィルタが、ユーザーの取りこぼしを作っていないこと。
  「AIが関係ないと判断したから通知しない」は、このプロダクトで最悪の欠陥。
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from bidnavi.core.db import connect
from bidnavi.core.normalize.price import parse_price
from bidnavi.match.engine import LLM_NOTIFY_THRESHOLD, evaluate, funnel_stats
from bidnavi.match.llm import (
    BudgetedLlmJudge,
    BudgetExceeded,
    LlmVerdict,
    StubLlmClient,
    cache_key,
    llm_calls_this_month,
)
from bidnavi.match.profile import CompanyProfile
from bidnavi.match.stages import Stage, similarity, stage_one, stage_two

NOW = dt.datetime(2026, 9, 7, 10, 0)
COMPANY = 'co-test'
SPEC = '本業務は自治体の業務システムの改修を行うものである。' * 20


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = connect(':memory:')
    c.execute(
        """INSERT INTO organization (id, name, org_type, entry_url, crawler_id, crawler_kind)
           VALUES ('org', 'テスト市', 'municipality', 'https://e.test/', 'x', 'http')"""
    )
    c.commit()
    return c


@pytest.fixture()
def profile(conn: sqlite3.Connection) -> CompanyProfile:
    p = CompanyProfile(
        company_id=COMPANY,
        categories=('業務システム開発', 'ソフトウェア開発'),
        prefectures=('14',),
        keywords=('システム', 'ソフトウェア'),
        ng_keywords=('警備',),
        price_min=1_000_000,
        price_max=50_000_000,
        llm_monthly_cap=3,
    )
    p.save(conn)
    return p


def make_tender(conn: sqlite3.Connection, natural_key: str, title: str) -> str:
    conn.execute(
        """INSERT INTO tender (natural_key, natural_key_version, natural_key_anchor,
            organization_id, source_url, source_captured_at, title, title_normalized,
            method, first_seen_at, last_seen_at)
           VALUES (?, 1, 'announced', 'org', 'https://e.test/1', ?, ?, ?, 'open', ?, ?)""",
        (natural_key, NOW.isoformat(), title, title, NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    return natural_key


def run(conn, profile, *, key='k1', title='業務システム改修業務委託',
        price='3,000,000円', pref='14', spec=SPEC, judge=None):
    make_tender(conn, key, title)
    return evaluate(
        conn=conn, natural_key=key, title=title, profile=profile,
        price=parse_price(price), prefecture_code=pref, spec_text=spec,
        judge=judge, now=NOW)


# ---------------------------------------------------------------------------
# 1段目
# ---------------------------------------------------------------------------

def test_stage_one_excludes_ng_keywords(profile: CompanyProfile) -> None:
    r = stage_one(title='庁舎警備業務委託', profile=profile,
                  price=parse_price('3,000,000円'), prefecture_code='14')
    assert r.hard_excluded is True
    assert r.promote is False


def test_stage_one_keeps_unknown_price(profile: CompanyProfile) -> None:
    """金額が非公表の案件を落とさない（Recall優先）。"""
    r = stage_one(title='業務システム改修', profile=profile,
                  price=parse_price('非公表'), prefecture_code='14')
    assert r.keyword_hit is True
    assert r.promote is True


def test_stage_one_keeps_unknown_prefecture(profile: CompanyProfile) -> None:
    """都道府県が読めなかった案件を「エリア外」として落とさない。"""
    r = stage_one(title='業務システム改修', profile=profile,
                  price=parse_price('3,000,000円'), prefecture_code=None)
    assert r.promote is True


def test_stage_one_rejects_out_of_area(profile: CompanyProfile) -> None:
    r = stage_one(title='業務システム改修', profile=profile,
                  price=parse_price('3,000,000円'), prefecture_code='01')
    assert r.promote is False
    assert r.keyword_hit is True   # キーワードには当たっている


# ---------------------------------------------------------------------------
# 2段目
# ---------------------------------------------------------------------------

def test_similarity_is_higher_for_related_titles() -> None:
    a = similarity('業務システム改修業務委託', '業務システム開発')
    b = similarity('給食調理業務委託', '業務システム開発')
    assert a > b


def test_similarity_bounds() -> None:
    assert similarity('同じ文字列', '同じ文字列') == pytest.approx(1.0)
    assert similarity('', 'あいうえお') == 0.0


def test_stage_two_promotes_when_no_corpus() -> None:
    """比較対象が無いときは判定不能。落とさず次段に進める。"""
    empty = CompanyProfile(company_id='x')
    assert stage_two(title='何か', profile=empty).promote is True


# ---------------------------------------------------------------------------
# FR-305: 取りこぼしを作らないこと（最重要）
# ---------------------------------------------------------------------------

def test_keyword_hit_is_notified_even_when_llm_says_irrelevant(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """§7.3-1 の最悪ケースの回帰テスト。

    キーワードに完全一致しているのにAIが「関連なし」と判断して通知されない、
    という事故を構造的に防げているか。
    """
    judge = BudgetedLlmJudge(conn, StubLlmClient(score=5))   # AIは関連なしと判定
    outcome = run(conn, profile, judge=judge)

    assert outcome.stage_reached is Stage.LLM
    assert outcome.llm is not None and outcome.llm.score < LLM_NOTIFY_THRESHOLD
    assert outcome.should_notify is True          # ← それでも通知する
    assert outcome.llm_overridden is True
    assert 'キーワード' in outcome.reason


def test_budget_exhaustion_does_not_stop_notification(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """予算上限に達してもユーザーへの通知は止めない（§7.1a）。"""
    judge = BudgetedLlmJudge(conn, StubLlmClient(score=90))

    for i in range(profile.llm_monthly_cap):
        out = run(conn, profile, key=f'k{i}', title=f'業務システム改修その{i}',
                  judge=judge)
        assert out.stage_reached is Stage.LLM

    over = run(conn, profile, key='over', title='業務システム更改業務', judge=judge)
    assert over.stage_reached is Stage.VECTOR      # LLMは呼ばれていない
    assert over.should_notify is True              # 通知は止まらない
    assert 'LLM未実行' in over.reason


def test_ng_keyword_is_the_only_hard_exclusion(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """通知しないのは、ユーザーが明示的に拒否したNGキーワードだけ。"""
    out = run(conn, profile, key='ng', title='庁舎警備業務委託', judge=None)
    assert out.should_notify is False
    assert out.stage_reached is Stage.RULE


def test_out_of_area_keyword_hit_still_notifies(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """エリア外でもキーワードに当たっていれば通知する（FR-305）。"""
    out = run(conn, profile, key='area', title='業務システム改修', pref='01')
    assert out.should_notify is True
    assert out.matched_by == 'rule'


# ---------------------------------------------------------------------------
# コスト制御
# ---------------------------------------------------------------------------

def test_llm_is_not_called_when_no_spec_and_title_is_unrelated(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """仕様書が無い案件は、案件名の類似度で足切りする（コスト）。

    案件名しか手掛かりが無いので、この足切りは妥当。
    """
    client = StubLlmClient()
    out = run(conn, profile, key='far', title='学校給食調理業務委託',
              spec=None, judge=BudgetedLlmJudge(conn, client))
    assert client.calls == 0
    assert out.stage_reached is not Stage.LLM
    assert out.should_notify is False


def test_llm_is_called_when_spec_exists_even_if_title_similarity_is_weak(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """Loop 5 で入れた設計変更を固定する（トレードオフの記録）。

    3段目の存在理由は「案件名では判定できない案件を仕様書で拾う」こと。
    その到達可否を案件名の類似度で決めると循環し、
    ネットワーク/LAN/GIS/RPA のような真陽性を構造的に取りこぼす（実測26.7%）。

    代償として3段目の到達率は上がる。コストは月間上限（§7.1a）で抑える。
    この交換を選んだのは、§2.2 が Recall を Precision より優先すると
    明記しているため。
    """
    client = StubLlmClient(score=90)
    out = run(conn, profile, key='weak', title='庁内LAN更改に伴う設定業務',
              judge=BudgetedLlmJudge(conn, client))
    assert client.calls == 1
    assert out.stage_reached is Stage.LLM
    assert out.should_notify is True


def test_llm_decides_for_unrelated_tender_when_spec_exists(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """仕様書がある無関係な案件は、2段目ではなくLLMが落とす。

    Precision の担保が2段目から3段目に移った、ということ。
    ここがLLMの精度に依存するようになったのが、この設計変更の代償。
    """
    client = StubLlmClient(score=5)   # LLMは関連なしと判定
    out = run(conn, profile, key='unrel', title='学校給食調理業務委託',
              judge=BudgetedLlmJudge(conn, client))
    assert out.stage_reached is Stage.LLM
    assert out.should_notify is False   # キーワードにも当たらないので通知しない


def test_cache_prevents_repeated_billing(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """同じ仕様書に二度課金しない。TTLではなく内容ハッシュの永続キャッシュ。"""
    client = StubLlmClient(score=90)
    judge = BudgetedLlmJudge(conn, client)

    first = judge.judge(company_id=COMPANY, monthly_cap=10, title='T',
                        spec_text=SPEC, profile_text='P', now=NOW)
    second = judge.judge(company_id=COMPANY, monthly_cap=10, title='T',
                         spec_text=SPEC, profile_text='P', now=NOW)

    assert client.calls == 1
    assert first.cache_hit is False and second.cache_hit is True
    assert second.cost_jpy() == 0.0
    assert first.score == second.score


def test_cache_persists_across_months(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """再公告が翌月でもキャッシュが効く。TTLだとここで課金が復活する。"""
    client = StubLlmClient()
    judge = BudgetedLlmJudge(conn, client)
    judge.judge(company_id=COMPANY, monthly_cap=10, title='T',
                spec_text=SPEC, profile_text='P', now=NOW)
    later = judge.judge(company_id=COMPANY, monthly_cap=10, title='T',
                        spec_text=SPEC, profile_text='P',
                        now=NOW + dt.timedelta(days=120))
    assert client.calls == 1
    assert later.cache_hit is True


def test_cache_hit_does_not_consume_budget(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """キャッシュヒットは課金されないので、予算も消費しない。"""
    judge = BudgetedLlmJudge(conn, StubLlmClient())
    for i in range(5):
        run(conn, profile, key=f'c{i}', title='業務システム改修業務委託',
            judge=judge)   # 同一タイトル・同一仕様書 → 2件目以降はキャッシュ
    assert llm_calls_this_month(conn, COMPANY, now=NOW) == 1


def test_budget_counts_only_billed_calls(conn: sqlite3.Connection) -> None:
    assert llm_calls_this_month(conn, COMPANY, now=NOW) == 0


def test_cache_key_is_content_addressed() -> None:
    a = cache_key(title='T', spec_text='本文  A', profile_text='P')
    b = cache_key(title='T', spec_text='本文 A', profile_text='P')   # 空白違い
    c = cache_key(title='T', spec_text='本文 B', profile_text='P')
    assert a == b
    assert a != c


def test_cache_key_does_not_lowercase() -> None:
    """英字の型番を潰さない（安易に lower() すると SV-100 と sv-100 が同一になる）。"""
    assert cache_key(title='SV-100', spec_text='x', profile_text='p') != \
           cache_key(title='sv-100', spec_text='x', profile_text='p')


def test_cost_is_recorded_per_match(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    judge = BudgetedLlmJudge(conn, StubLlmClient(score=90))
    out = run(conn, profile, judge=judge)
    assert out.cost_jpy > 0

    row = conn.execute('SELECT * FROM match_result').fetchone()
    assert row['llm_input_tokens'] > 0
    assert row['llm_cost_jpy'] > 0
    assert row['stage_reached'] == 3


def test_verdict_cost_is_zero_on_cache_hit() -> None:
    v = LlmVerdict(score=1, reason='', summary='',
                   input_tokens=10_000, output_tokens=1_000, cache_hit=True)
    assert v.cost_jpy() == 0.0


def test_funnel_stats_measures_stage3_ratio(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """§7.1a の通過率が設計目標どおりかを実測できること。"""
    judge = BudgetedLlmJudge(conn, StubLlmClient(score=90))
    run(conn, profile, key='a', title='業務システム改修業務委託', judge=judge)
    run(conn, profile, key='b', title='学校給食調理業務委託', judge=judge)
    run(conn, profile, key='c', title='庁舎警備業務委託', judge=judge)

    stats = funnel_stats(conn, COMPANY)
    assert stats.total == 3
    # NG キーワード（警備）は1段目で確実に止まり、LLMには回らない
    assert stats.reached_stage3 == 2
    assert stats.total_cost_jpy > 0

    # ⚠️ 到達率 67% は §7.1a の設計目標 1% を大きく超えている。
    # 文字バイグラムでは関連判定ができず、Recall を取るために
    # 2段目の足切りを緩めた結果。本番では埋め込みモデルが必要。
    assert stats.stage3_ratio > 0.01


def test_spec_text_is_truncated_before_billing(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    """長大なPDFで青天井に課金しない。"""
    from bidnavi.match.llm import MAX_SPEC_CHARS

    client = StubLlmClient()
    judge = BudgetedLlmJudge(conn, client)
    huge = 'あ' * (MAX_SPEC_CHARS * 3)
    verdict = judge.judge(company_id=COMPANY, monthly_cap=10, title='T',
                          spec_text=huge, profile_text='P', now=NOW)
    assert verdict.input_tokens < MAX_SPEC_CHARS * 1.5


def test_budget_exceeded_is_raised_not_swallowed(
    conn: sqlite3.Connection, profile: CompanyProfile
) -> None:
    judge = BudgetedLlmJudge(conn, StubLlmClient())
    with pytest.raises(BudgetExceeded):
        judge.judge(company_id=COMPANY, monthly_cap=0, title='T',
                    spec_text='本文', profile_text='P', now=NOW)


def test_profile_roundtrip(conn: sqlite3.Connection, profile: CompanyProfile) -> None:
    loaded = CompanyProfile.load(conn, COMPANY)
    assert loaded == profile


# ---------------------------------------------------------------------------
# Loop 4 のレビューで発見した実バグの回帰テスト
# ---------------------------------------------------------------------------

@pytest.fixture()
def keywordless_profile(conn: sqlite3.Connection) -> CompanyProfile:
    """業種とエリアだけ設定し、キーワードを設定していない利用者。"""
    p = CompanyProfile(
        company_id='co-nokw',
        categories=('業務システム開発',),
        prefectures=('14',),
        keywords=(),
        llm_monthly_cap=10,
    )
    p.save(conn)
    return p


def test_profile_without_keywords_still_gets_notified(
    conn: sqlite3.Connection, keywordless_profile: CompanyProfile
) -> None:
    """キーワード未設定の利用者に通知が1件も届かない、という事故の回帰テスト。

    キーワードは「必ず通知する」保証であって、
    「これに当たらなければ捨てる」フィルタではない。
    """
    judge = BudgetedLlmJudge(conn, StubLlmClient(score=95))
    out = run(conn, keywordless_profile, key='nokw',
              title='業務システム改修業務委託', judge=judge)

    assert out.stage_reached is Stage.LLM
    assert out.should_notify is True


def test_keywordless_profile_still_filters_irrelevant_without_spec(
    conn: sqlite3.Connection, keywordless_profile: CompanyProfile
) -> None:
    """通過条件を緩めても、仕様書の無い無関係な案件は2段目で落ちること。"""
    judge = BudgetedLlmJudge(conn, StubLlmClient(score=95))
    out = run(conn, keywordless_profile, key='nokw2',
              title='学校給食調理業務委託', spec=None, judge=judge)

    assert out.stage_reached is Stage.VECTOR
    assert out.should_notify is False


def test_stage_one_promotes_without_keywords(
    keywordless_profile: CompanyProfile,
) -> None:
    r = stage_one(title='何らかの業務委託', profile=keywordless_profile,
                  price=parse_price('3,000,000円'), prefecture_code='14')
    assert r.promote is True
    assert r.keyword_hit is False


def test_ng_and_area_still_filter_without_keywords(
    keywordless_profile: CompanyProfile,
) -> None:
    """通過条件を緩めても、エリアと金額の絞り込みは効くこと。"""
    out_of_area = stage_one(title='業務システム改修', profile=keywordless_profile,
                            price=parse_price('3,000,000円'), prefecture_code='01')
    assert out_of_area.promote is False
