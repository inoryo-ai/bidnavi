"""正規化層のテスト（Loop 1 / 天城）

重点は要件定義 §7.3「間違った結果の具体例」の再現防止:
  - 締切の時刻欠落を 00:00 で埋めない
  - 非公表を 0円 にしない
  - 年度表記ゆれで同一案件が別扱いにならない
  - 枝番（その1/その2）は別案件のまま残す
"""
from __future__ import annotations

import datetime as dt

import pytest

from bidnavi.core.natural_key import AnchorKind, build_natural_key, choose_anchor
from bidnavi.core.normalize.dates import (
    parse_deadline,
    parse_japanese_date,
    parse_japanese_time,
)
from bidnavi.core.normalize.price import parse_price
from bidnavi.core.normalize.text import normalize_text, normalize_title
from bidnavi.core.types import Deadline, TaxBasis, TimeSource


# ---------------------------------------------------------------------------
# 日付
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('text', 'expected'), [
    ('令和8年3月12日', dt.date(2026, 3, 12)),
    ('令和８年３月１２日', dt.date(2026, 3, 12)),          # 全角
    ('令和元年5月1日', dt.date(2019, 5, 1)),               # 元年
    ('平成元年1月8日', dt.date(1989, 1, 8)),               # 元年（平成の初日）
    ('平成31年4月30日', dt.date(2019, 4, 30)),             # 平成の最終日
    ('R8.3.12', dt.date(2026, 3, 12)),
    ('R8/3/12', dt.date(2026, 3, 12)),
    ('2026年3月12日', dt.date(2026, 3, 12)),
    ('2026/3/12', dt.date(2026, 3, 12)),
    ('2026-03-12', dt.date(2026, 3, 12)),
    ('入札日は令和8年3月12日(木)とする', dt.date(2026, 3, 12)),
])
def test_parse_japanese_date_ok(text: str, expected: dt.date) -> None:
    assert parse_japanese_date(text) == expected


@pytest.mark.parametrize('text', [
    '',
    None,
    '未定',
    '3月12日',              # 年が無い → 推測しない
    '令和8年2月30日',        # 存在しない日
    '平成35年1月1日',        # 実在しない元号年（平成は31年まで）
    '令和元年4月30日',       # 令和は2019-05-01 から
])
def test_parse_japanese_date_returns_none(text: str | None) -> None:
    assert parse_japanese_date(text) is None


@pytest.mark.parametrize(('text', 'expected'), [
    ('17時00分', dt.time(17, 0)),
    ('17:00', dt.time(17, 0)),
    ('午後5時', dt.time(17, 0)),
    ('午後5時30分', dt.time(17, 30)),
    ('午前10時30分', dt.time(10, 30)),
    ('正午', dt.time(12, 0)),
    ('午後12時', dt.time(12, 0)),      # 正午
    ('午前12時', dt.time(0, 0)),       # 深夜0時
    ('１７時', dt.time(17, 0)),        # 全角
])
def test_parse_japanese_time_ok(text: str, expected: dt.time) -> None:
    assert parse_japanese_time(text) == expected


@pytest.mark.parametrize('text', ['', None, '締切厳守', '3月12日', '25時'])
def test_parse_japanese_time_returns_none(text: str | None) -> None:
    assert parse_japanese_time(text) is None


def test_deadline_keeps_time_when_present() -> None:
    d = parse_deadline('令和8年3月12日 午後5時00分まで')
    assert d is not None
    assert d.date == dt.date(2026, 3, 12)
    assert d.time == dt.time(17, 0)
    assert d.time_source is TimeSource.EXPLICIT
    assert d.display() == '2026-03-12 17:00必着'


def test_deadline_does_not_invent_midnight_when_time_absent() -> None:
    """§7.3-2 の再現防止。時刻が無いときに 00:00 を作らない。"""
    d = parse_deadline('令和8年3月12日')
    assert d is not None
    assert d.date == dt.date(2026, 3, 12)
    assert d.time is None
    assert d.time_source is TimeSource.ABSENT
    assert d.display() == '2026-03-12（時刻記載なし）'
    # 通知は安全側（当日9:00）に倒す
    assert d.notify_at() == dt.datetime(2026, 3, 12, 9, 0)


def test_deadline_does_not_read_time_out_of_the_date_part() -> None:
    """「令和8年3月12日」の数字を時刻と誤読しないこと。"""
    d = parse_deadline('令和8年3月12日')
    assert d is not None and d.time is None


def test_deadline_returns_none_without_date() -> None:
    assert parse_deadline('午後5時まで') is None
    assert parse_deadline('') is None


def test_deadline_rejects_inconsistent_construction() -> None:
    """不変条件: time があるのに ABSENT はバグなので構築時に落とす。"""
    with pytest.raises(ValueError):
        Deadline(date=dt.date(2026, 3, 12), time=dt.time(17, 0),
                 time_source=TimeSource.ABSENT, raw='x')
    with pytest.raises(ValueError):
        Deadline(date=dt.date(2026, 3, 12), time=None,
                 time_source=TimeSource.EXPLICIT, raw='x')


# ---------------------------------------------------------------------------
# 金額
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(('text', 'expected'), [
    ('金1,234,567円', 1_234_567),
    ('1,234,567円', 1_234_567),
    ('１，２３４，５６７円', 1_234_567),   # 全角
    ('123万円', 1_230_000),
    ('1億2,000万円', 120_000_000),
    ('1億円', 100_000_000),
    ('5000000', 5_000_000),               # 単位なしの数字だけ
])
def test_parse_price_amount(text: str, expected: int) -> None:
    p = parse_price(text)
    assert p.amount == expected
    assert p.undisclosed is False


@pytest.mark.parametrize('text', ['非公表', '予定価格は非公表', '事後公表', '未定'])
def test_parse_price_undisclosed_is_not_zero(text: str) -> None:
    """§7.3-3 の再現防止。非公表を 0円 にしない。"""
    p = parse_price(text)
    assert p.undisclosed is True
    assert p.amount is None
    assert p.amount != 0


@pytest.mark.parametrize('text', ['', '-', '―', 'なし', None])
def test_parse_price_empty_is_unknown_not_undisclosed(text: str | None) -> None:
    """「読めなかった(不明)」と「意図的に非公表」は別状態。"""
    p = parse_price(text)
    assert p.amount is None
    assert p.undisclosed is False


@pytest.mark.parametrize(('text', 'expected'), [
    ('1,000円（税抜）', TaxBasis.EXCLUDED),
    ('1,000円（消費税込み）', TaxBasis.INCLUDED),
    ('1,000円', TaxBasis.UNKNOWN),
    ('1,000円（税込・税抜併記）', TaxBasis.UNKNOWN),   # 両方あれば判定しない
])
def test_parse_price_tax_basis(text: str, expected: TaxBasis) -> None:
    assert parse_price(text).tax_basis is expected


def test_price_range_filter_keeps_unknown_amounts() -> None:
    """金額不明の案件をレンジ絞り込みで取りこぼさない（Recall優先）。"""
    assert parse_price('非公表').in_range(1_000_000, 5_000_000) is True
    assert parse_price('').in_range(1_000_000, 5_000_000) is True
    assert parse_price('3,000,000円').in_range(1_000_000, 5_000_000) is True
    assert parse_price('9,000,000円').in_range(1_000_000, 5_000_000) is False


def test_price_rejects_inconsistent_construction() -> None:
    from bidnavi.core.types import Price
    with pytest.raises(ValueError):
        Price(amount=100, undisclosed=True, tax_basis=TaxBasis.UNKNOWN, raw='x')


# ---------------------------------------------------------------------------
# 案件名
# ---------------------------------------------------------------------------

def test_normalize_text_basic() -> None:
    assert normalize_text('　ＡＢＣ　　１２３　') == 'ABC 123'


@pytest.mark.parametrize(('raw', 'expected'), [
    ('【入札公告】令和８年度　○○業務委託　その１について', '○○業務委託その1'),
    ('一般競争入札　○○業務委託', '○○業務委託'),
    ('公告　○○業務委託の実施について', '○○業務委託'),
    ('○○業務委託（第2次）', '○○業務委託第2次'),
])
def test_normalize_title(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


def test_normalize_title_absorbs_fiscal_year_variants() -> None:
    """年度表記の有無で同一案件が別扱いにならないこと。"""
    variants = [
        '令和8年度 ○○業務委託',
        '令和８年度　○○業務委託',
        'R8年度 ○○業務委託',
        '2026年度 ○○業務委託',
        '○○業務委託',
    ]
    normalized = {normalize_title(v) for v in variants}
    assert normalized == {'○○業務委託'}


def test_normalize_title_keeps_branch_numbers() -> None:
    """枝番は別案件。寄せてはいけない。"""
    a = normalize_title('○○工事 その1')
    b = normalize_title('○○工事 その2')
    assert a != b


def test_normalize_title_does_not_erase_whole_title() -> None:
    """タイトルが定型句だけの場合、空文字にしない。"""
    assert normalize_title('入札公告') == '入札公告'


# ---------------------------------------------------------------------------
# natural_key
# ---------------------------------------------------------------------------

def test_natural_key_merges_notation_variants() -> None:
    """FR-106: 表記ゆれを吸収して同一案件と判定する。"""
    a = build_natural_key(
        'jp-mlit', '【入札公告】令和８年度　○○業務　その１について',
        announced_date=dt.date(2026, 3, 1))
    b = build_natural_key(
        'jp-mlit', '○○業務その1',
        announced_date=dt.date(2026, 3, 1))
    assert a.value == b.value


def test_natural_key_separates_different_organizations() -> None:
    """県サイトと市サイトで同名の案件があっても、機関が違えば別案件。"""
    a = build_natural_key('pref-chiba', '○○業務', announced_date=dt.date(2026, 3, 1))
    b = build_natural_key('city-funabashi', '○○業務', announced_date=dt.date(2026, 3, 1))
    assert a.value != b.value


def test_natural_key_separates_branch_numbers() -> None:
    a = build_natural_key('jp-mlit', '○○工事その1', announced_date=dt.date(2026, 3, 1))
    b = build_natural_key('jp-mlit', '○○工事その2', announced_date=dt.date(2026, 3, 1))
    assert a.value != b.value


def test_natural_key_survives_deadline_change() -> None:
    """訂正公告で締切が延びても、公告日が同じなら同一案件のまま。

    これが choose_anchor() で公告日を最優先にしている理由。
    """
    a = build_natural_key('jp-mlit', '○○業務',
                          announced_date=dt.date(2026, 3, 1),
                          bid_deadline_date=dt.date(2026, 3, 20))
    b = build_natural_key('jp-mlit', '○○業務',
                          announced_date=dt.date(2026, 3, 1),
                          bid_deadline_date=dt.date(2026, 3, 27))  # 締切が延びた
    assert a.value == b.value
    assert a.anchor_kind is AnchorKind.ANNOUNCED


def test_natural_key_falls_back_to_deadline() -> None:
    key = build_natural_key('jp-mlit', '○○業務', bid_deadline_date=dt.date(2026, 3, 20))
    assert key.anchor_kind is AnchorKind.BID_DEADLINE
    assert key.is_weak is False


def test_natural_key_without_any_date_is_flagged_weak() -> None:
    """日付が無いキーは衝突リスクがあるので、監視できるよう印を付ける。"""
    key = build_natural_key('jp-mlit', '○○業務')
    assert key.anchor_kind is AnchorKind.NONE
    assert key.is_weak is True


# ---------------------------------------------------------------------------
# Loop 1 のレビューで発見した実バグの回帰テスト
# ---------------------------------------------------------------------------

def test_price_handles_sen_yen_unit() -> None:
    """自治体の予算表は「千円」単位。取りこぼすと金額が 1/1000 になる。"""
    assert parse_price('1,234千円').amount == 1_234_000
    assert parse_price('500千円').amount == 500_000


def test_price_handles_hyakuman_yen_unit() -> None:
    """「百万」は「万」より先に評価しないと 100万 と誤読する。"""
    assert parse_price('12百万円').amount == 12_000_000


def test_price_post_award_disclosure_is_undisclosed() -> None:
    assert parse_price('予定価格は入札後に公表').undisclosed is True
    assert parse_price('開札後に公表').undisclosed is True


def test_normalize_title_keeps_content_when_only_boilerplate() -> None:
    """定型句・年度表記だけのタイトルでも、内容がある限り空にしない。"""
    for raw in ['令和8年度', '入札公告', '【公告】', 'について']:
        assert normalize_title(raw) != '', f'{raw!r} が空文字になった'


def test_normalize_title_of_blank_input_is_empty() -> None:
    """空白だけの入力は本当に空。ここで偽の値を作らないのが正しい。"""
    assert normalize_title('　') == ''


def test_natural_key_rejects_empty_title() -> None:
    """タイトルが取れていない案件はパース失敗。キーを作らず送出する。

    空文字でハッシュすると同一機関・同一日付の全案件が1件に潰れる。
    """
    from bidnavi.core.natural_key import EmptyTitleError
    with pytest.raises(EmptyTitleError):
        build_natural_key('jp-x', '　', announced_date=dt.date(2026, 3, 1))


def test_natural_key_does_not_collide_on_degenerate_titles() -> None:
    """定型句だけのタイトル同士が誤結合しないこと。"""
    a = build_natural_key('jp-x', '令和8年度', announced_date=dt.date(2026, 3, 1))
    b = build_natural_key('jp-x', '入札公告', announced_date=dt.date(2026, 3, 1))
    assert a.value != b.value


def test_deadline_takes_earliest_when_range_given() -> None:
    """「○日から○日まで」は早い側に倒す。遅い側に倒すと締切を過ぎてから通知が届く。"""
    d = parse_deadline('令和8年3月12日から令和8年3月19日まで')
    assert d is not None
    assert d.date == dt.date(2026, 3, 12)
    assert d.ambiguous is True
    assert '複数日付あり' in d.display()


def test_deadline_takes_earliest_regardless_of_written_order() -> None:
    """出現順ではなく日付の早さで選ぶ。"""
    d = parse_deadline('第2回 令和8年4月2日 / 第1回 令和8年3月12日')
    assert d is not None
    assert d.date == dt.date(2026, 3, 12)
    assert d.ambiguous is True


def test_deadline_single_date_is_not_ambiguous() -> None:
    d = parse_deadline('令和8年3月12日(木)午後5時00分まで（必着）')
    assert d is not None
    assert d.ambiguous is False
    assert d.time == dt.time(17, 0)


def test_find_dates_does_not_double_count_era_dates() -> None:
    """「令和8年3月12日」を和暦と西暦で二重に拾わないこと。"""
    from bidnavi.core.normalize.dates import find_dates
    found = find_dates('令和8年3月12日')
    assert [d for d, _ in found] == [dt.date(2026, 3, 12)]


def test_kanji_numeral_dates_return_none_rather_than_wrong_value() -> None:
    """漢数字は未対応。誤った値を返すより None を返す方が安全（既知の制約）。"""
    assert parse_japanese_date('令和八年三月十二日') is None


def test_choose_anchor_priority() -> None:
    assert choose_anchor(dt.date(2026, 1, 1), dt.date(2026, 2, 2)) == (
        dt.date(2026, 1, 1), AnchorKind.ANNOUNCED)
    assert choose_anchor(None, dt.date(2026, 2, 2)) == (
        dt.date(2026, 2, 2), AnchorKind.BID_DEADLINE)
    assert choose_anchor(None, None) == (None, AnchorKind.NONE)
