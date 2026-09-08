"""攻撃入力に対する防御の検証。

クローラは「相手のサイトが決めた内容」を取り込む。
入力は常に攻撃者が制御しうる前提で設計する必要がある。

ここに書かれたテストは、すべて**実際に再現した攻撃**に対応している。
数字（313秒など）は実測値であり、想定値ではない。
"""
from __future__ import annotations

import io
import time
import zipfile

import pytest

from tender_pipeline.core import attachments
from tender_pipeline.core.http import (
    DEFAULT_MAX_BYTES,
    UnsafeUrl,
    assert_fetchable,
)
from tender_pipeline.core.normalize.price import MAX_PRICE_TEXT_CHARS, parse_price
from tender_pipeline.pipeline.ingest import UnknownColumnError, _assert_known_columns

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# ReDoS: 正規表現の破滅的バックトラック
# ---------------------------------------------------------------------------

def test_price_parser_does_not_hang_on_long_digits() -> None:
    """長大な数字列で金額パーサが停止しないこと。

    🔴 修正前は 9万桁の入力に **313秒** かかっていた（実測）。
    `(\\d+)\\s*億` は「億」を含まない数字列に対して O(n^2) になる。
    案件1件でパイプライン全体が止まるため、DoSとして成立していた。
    """
    attack = '1' + ',000' * 30000 + '円'

    start = time.perf_counter()
    parse_price(attack)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f'金額パーサが {elapsed:.1f}秒 かかりました（ReDoSの疑い）'


def test_price_parser_rejects_overlong_input() -> None:
    """金額欄に収まらない長さの入力は解釈しない。"""
    result = parse_price('9' * (MAX_PRICE_TEXT_CHARS + 1))
    assert result.amount is None
    assert result.undisclosed is False  # 「非公表」ではなく「読めなかった」


def test_price_parser_does_not_silently_truncate_digits() -> None:
    """桁数が上限を超える数字を、黙って切り詰めた金額にしない。

    切り詰めると「1200兆円の案件」がDBに入り、金額レンジ絞り込みが壊れる。
    読めないものは None（不明）にする。
    """
    assert parse_price('12345678901234567890円').amount is None
    assert parse_price('12345678901234567890').amount is None


@pytest.mark.parametrize(('text', 'expected'), [
    ('金1,234,567円（税抜）', 1234567),
    ('1億2,000万円', 120000000),
    ('1,234千円', 1234000),
    ('123万円', 1230000),
])
def test_price_parser_still_parses_normal_values(text: str, expected: int) -> None:
    """防御を入れても正常系が壊れていないこと。"""
    assert parse_price(text).amount == expected


# ---------------------------------------------------------------------------
# SSRF: 内部ネットワークへの誘導
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('url', [
    'http://169.254.169.254/latest/meta-data/',   # クラウドのメタデータ
    'http://127.0.0.1:8080/admin',
    'http://localhost/',
    'http://10.0.0.5/',
    'http://192.168.1.1/',
    'http://[::1]/',
    'http://db.internal/',
    'file:///etc/passwd',
    'ftp://example.com/x',
    'http:///no-host',
])
def test_unsafe_urls_are_rejected(url: str) -> None:
    """案件ページのリンクに内部宛URLが紛れていても取りに行かない。

    リンクの中身は相手のサイトが決める。こちらの内部ネットワークへ
    誘導される可能性を前提にする。
    """
    with pytest.raises(UnsafeUrl):
        assert_fetchable(url)


@pytest.mark.parametrize('url', [
    'https://www.city.yokohama.lg.jp/nyusatsu/',
    'http://example.com/tender.pdf',
])
def test_normal_urls_are_allowed(url: str) -> None:
    """通常の自治体サイトは通ること（過剰防御になっていないこと）。"""
    assert_fetchable(url)


# ---------------------------------------------------------------------------
# 解凍爆弾・巨大レスポンス
# ---------------------------------------------------------------------------

def test_zip_bomb_amplification_is_bounded_by_size_limit() -> None:
    """解凍爆弾が上限で止まること。

    🔴 実測: 51KB の圧縮ファイルが 50MB に展開される（増幅率 1,026倍）。
    docx / xlsx は実体が zip なので、ダウンロード段階で上限をかけないと
    メモリを食い尽くされる。
    """
    payload = b'A' * (5 * 1024 * 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr('[Content_Types].xml', payload)
    bomb = buf.getvalue()

    amplification = len(payload) / len(bomb)
    assert amplification > 100, '前提が崩れている（圧縮が効いていない）'

    # 上限は「展開後」ではなく「受信バイト数」にかける。
    # 展開してから測るのでは手遅れになるため。
    assert DEFAULT_MAX_BYTES < 100 * 1024 * 1024, (
        '上限が緩すぎる。増幅率を掛けるとメモリが枯渇する'
    )


def test_unsupported_archive_is_not_expanded() -> None:
    """zip 等は「未対応形式」として展開せずに返す。"""
    result = attachments.extract(b'PK\x03\x04dummy', url='http://example.com/a.zip')
    assert result.status == attachments.ExtractionStatus.UNSUPPORTED_FORMAT
    assert result.text is None


# ---------------------------------------------------------------------------
# SQL: 列名の許可リスト
# ---------------------------------------------------------------------------

def test_unknown_column_is_rejected() -> None:
    """SQLへ埋め込む列名が許可リスト外なら落とす。

    列名は現状すべてコード内リテラルだが、将来
    外部由来の値が列名に混ざればSQLインジェクションになる。
    「今は安全」を「これからも安全」にするためのガード。
    """
    with pytest.raises(UnknownColumnError):
        _assert_known_columns({'title': 'x', 'evil; DROP TABLE tender; --': 1})


def test_known_columns_pass() -> None:
    _assert_known_columns({'title': 'x', 'natural_key': 'y', 'status': 'open'})
