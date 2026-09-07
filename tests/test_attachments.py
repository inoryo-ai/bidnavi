"""添付ファイル処理のテスト（Loop 3 / 天城・§7.6）

重点: 対象外形式が「無視」ではなく「記録」されること。
黙って落とすと取りこぼしになる。
"""
from __future__ import annotations

import io

import pytest

from tender_pipeline.core.attachments import (
    ExtractionResult,
    classify,
    extract,
    extract_pdf,
    guess_extension,
)
from tender_pipeline.core.types import ExtractionStatus


@pytest.mark.parametrize(('url', 'expected'), [
    ('https://e.test/a/spec.pdf', '.pdf'),
    ('https://e.test/a/spec.PDF', '.pdf'),
    ('https://e.test/a/仕様書.xlsx', '.xlsx'),
    ('https://e.test/a/spec.pdf?id=3', '.pdf'),
    ('https://e.test/a/%E4%BB%95%E6%A7%98%E6%9B%B8.jtd', '.jtd'),
    ('https://e.test/a/noext', ''),
    ('https://e.test/a.b/noext', ''),
])
def test_guess_extension(url: str, expected: str) -> None:
    assert guess_extension(url) == expected


@pytest.mark.parametrize('url', [
    'https://e.test/仕様書.jtd',      # 一太郎
    'https://e.test/spec.zip',
    'https://e.test/spec.xls',        # 旧Excel
    'https://e.test/scan.jpg',
])
def test_unsupported_formats_are_recorded_not_ignored(url: str) -> None:
    """§7.6: 対象外は必ずステータスを立てる。None や空を返さない。"""
    assert classify(url) is ExtractionStatus.UNSUPPORTED_FORMAT
    result = extract(b'', url=url)
    assert result.status is ExtractionStatus.UNSUPPORTED_FORMAT
    assert result.needs_human is True
    assert result.note, '理由が記録されていません'


@pytest.mark.parametrize('url', [
    'https://e.test/spec.pdf',
    'https://e.test/spec.xlsx',
    'https://e.test/spec.docx',
])
def test_supported_formats_pass_pre_classification(url: str) -> None:
    assert classify(url) is None


def test_text_extraction() -> None:
    result = extract('入札公告 ○○業務'.encode(), url='https://e.test/a.txt')
    assert result.status is ExtractionStatus.OK
    assert result.text is not None and '入札公告' in result.text
    assert result.needs_human is False


def test_scanned_pdf_is_flagged_not_returned_as_empty_ok() -> None:
    """テキスト層の無いPDFを ok/空文字 で返さないこと。

    空文字を ok として返すと「仕様書が空の案件」としてDBに入り、
    AI判定が「関係なし」と結論して取りこぼしになる。
    """
    pdf = _make_pdf(text=None)
    result = extract_pdf(pdf, url='https://e.test/scan.pdf')
    assert result.status is ExtractionStatus.UNSUPPORTED_SCANNED
    assert result.text is None
    assert result.needs_human is True


def test_text_pdf_is_extracted() -> None:
    """テキスト層のあるPDFから本文が取れること。

    埋め込みフォントの都合で日本語を確実に往復させるのが難しいため、
    ここでは抽出機構そのものをASCIIで確認する。
    日本語PDF特有の問題は test_undecodable_pdf_is_not_reported_as_ok で押さえる。
    """
    body = 'Bid announcement 2026-03-12 estimated price 1,234,567 JPY ' * 5
    result = extract_pdf(_make_pdf(text=body), url='https://e.test/spec.pdf')
    assert result.status is ExtractionStatus.OK
    assert result.text is not None
    assert 'Bid announcement' in result.text
    assert result.chars >= 50


def test_undecodable_pdf_is_not_reported_as_ok() -> None:
    """日本語PDFで実際に起きる事故の回帰テスト。

    フォントに ToUnicode マップが無いと、pdfplumber は例外を出さず
    (cid:1234) の羅列を返す。文字数は十分あるので件数監視では検知できない。
    これを ok として通すと、意味不明な文字列をLLMに投げることになり、
    「関係なし」と判定されて取りこぼしになる。
    """
    body = '入札公告 令和8年3月12日 予定価格 金1,234,567円 ' * 5
    result = extract_pdf(_make_pdf(text=body), url='https://e.test/spec.pdf')
    assert result.status is ExtractionStatus.UNSUPPORTED_SCANNED
    assert result.text is None
    assert result.needs_human is True
    assert 'cid' in result.note


@pytest.mark.parametrize(('text', 'expected_ok'), [
    ('入札公告 ○○業務委託 令和8年3月12日', True),
    ('(cid:229)(cid:133)(cid:165)(cid:230)(cid:156)(cid:173)', False),
    ('入札公告(cid:229)○○業務委託の実施について 予定価格は非公表とする', True),
])
def test_cid_ratio_threshold(text: str, expected_ok: bool) -> None:
    from tender_pipeline.core.attachments import MAX_CID_RATIO, cid_ratio
    assert (cid_ratio(text) <= MAX_CID_RATIO) is expected_ok


def test_broken_pdf_is_recorded_as_failure() -> None:
    """破損ファイルを握りつぶさない。"""
    result = extract_pdf(b'not a pdf at all', url='https://e.test/x.pdf')
    assert result.status is ExtractionStatus.FETCH_FAILED
    assert result.needs_human is True
    assert result.note


def test_xlsx_extraction() -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['件名', '○○業務委託'])
    ws.append(['予定価格', '1,234,567円'])
    buf = io.BytesIO()
    wb.save(buf)

    result = extract(buf.getvalue(), url='https://e.test/spec.xlsx')
    assert result.status is ExtractionStatus.OK
    assert result.text is not None
    assert '○○業務委託' in result.text


def test_needs_human_covers_all_non_ok_states() -> None:
    for status in (ExtractionStatus.UNSUPPORTED_SCANNED,
                   ExtractionStatus.UNSUPPORTED_FORMAT,
                   ExtractionStatus.FETCH_FAILED):
        r = ExtractionResult(status, None, 0, '.pdf')
        assert r.needs_human is True
    assert ExtractionResult(ExtractionStatus.OK, 'x', 1, '.pdf').needs_human is False


def _make_pdf(*, text: str | None) -> bytes:
    """テキスト層あり/なしのPDFを作る。

    text=None のとき図形だけを描画し、テキスト層の無いPDF
    （＝スキャン画像PDF相当）を作る。
    """
    import pypdf
    from pypdf.generic import DecodedStreamObject, NameObject

    # reportlab が無い環境でも動くよう、pypdf で最小のPDFを組み立てる
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    if text is not None:
        content = _text_content_stream(text)
        stream = DecodedStreamObject()
        stream.set_data(content.encode('utf-8'))
        page[NameObject('/Contents')] = writer._add_object(stream)
        page[NameObject('/Resources')] = writer._add_object(_font_resource(writer))
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _text_content_stream(text: str) -> str:
    lines = []
    y = 800
    for chunk in [text[i:i + 60] for i in range(0, len(text), 60)]:
        escaped = chunk.replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')
        lines.append(f'BT /F1 10 Tf 50 {y} Td ({escaped}) Tj ET')
        y -= 14
        if y < 40:
            break
    return '\n'.join(lines)


def _font_resource(writer: object) -> dict[str, object]:
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject
    font = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
    })
    return DictionaryObject({
        NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})
    })
