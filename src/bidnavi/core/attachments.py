"""添付ファイルの形式判定と抽出（要件定義 v2.0 §7.6 / FR-103 / FR-104 / FR-108）

原則:
  1. **対象外は「無視」ではない。** 必ず ExtractionStatus を立てて記録し、
     UI で「未対応形式の添付があります（原本を確認してください）」と出す。
     黙って落とすと §7.3-1 の取りこぼしになる。
  2. **実体を自社に保存しない（FR-108）。** URL と抽出テキストだけ持つ。
  3. スキャン画像PDF（テキスト層なし）は Phase 1 では対象外。
     OCRは精度が落ちるため、誤ったテキストを作るより「人が開く」に倒す。

Phase 1 の線引きは §7.6 の表のとおり。全部対応しようとすると永久に終わらない。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from .types import ExtractionStatus

#: Phase 1 で対応する拡張子
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({'.pdf', '.docx', '.xlsx', '.html', '.htm', '.txt', '.csv'})

#: 明示的に「今回はやらない」と決めた拡張子（§7.6）
UNSUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    '.jtd',   # 一太郎。自治体でまだ現役だが標準ライブラリで開けない
    '.jtdc', '.jsw', '.jtt',
    '.xls',   # 旧Excel形式。Phase 2
    '.doc',   # 旧Word形式。Phase 2
    '.zip', '.lzh', '.7z',  # 中に複数ファイル。Phase 2
    '.jpg', '.jpeg', '.png', '.tif', '.tiff',  # 画像。OCR対象外
})

#: テキスト層があると判定するのに必要な最低文字数。
#: これを下回るPDFはスキャン画像とみなす。
MIN_PDF_TEXT_CHARS = 50

#: 文字コードを解決できなかった文字。PDFのフォントに ToUnicode マップが
#: 無い場合、抽出は「成功」するが中身が (cid:1234) の羅列になる。
#: 日本語の官公庁PDFで実際に起きる。文字数は多いので件数監視では検知できない。
_CID_TOKEN = re.compile(r'\(cid:\d+\)')

#: テキストのうちこの割合以上が cid トークンなら、解読できていないとみなす
MAX_CID_RATIO = 0.2


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    status: ExtractionStatus
    text: str | None
    chars: int
    file_ext: str
    note: str = ''

    @property
    def needs_human(self) -> bool:
        """人が原本を開く必要があるか。UI に導線を出す判断に使う。"""
        return self.status in {
            ExtractionStatus.UNSUPPORTED_SCANNED,
            ExtractionStatus.UNSUPPORTED_FORMAT,
            ExtractionStatus.FETCH_FAILED,
        }


def guess_extension(url: str) -> str:
    """URL から拡張子を推定する。クエリ文字列とURLエンコードを考慮する。"""
    path = unquote(urlparse(url).path)
    _, dot, ext = path.rpartition('.')
    if not dot or '/' in ext:
        return ''
    return f'.{ext.lower()}'


def classify(url: str) -> ExtractionStatus | None:
    """URL だけで対象外と分かる場合にステータスを返す。判定できなければ None。

    ダウンロードする前に落とせるものはここで落として、無駄な通信をしない。
    """
    ext = guess_extension(url)
    if ext in UNSUPPORTED_EXTENSIONS:
        return ExtractionStatus.UNSUPPORTED_FORMAT
    if ext and ext not in SUPPORTED_EXTENSIONS:
        return ExtractionStatus.UNSUPPORTED_FORMAT
    return None


def extract_pdf(content: bytes, *, url: str = '') -> ExtractionResult:
    """PDF からテキストを取り出す。

    テキスト層が無い（＝スキャン画像）場合は UNSUPPORTED_SCANNED を返す。
    ここで空文字を ok として返すと「仕様書が空の案件」としてDBに入り、
    AI判定が「関係なし」と結論して取りこぼしになる。
    """
    ext = guess_extension(url) or '.pdf'
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover - 環境依存
        return ExtractionResult(ExtractionStatus.SKIPPED, None, 0, ext,
                                'pdfplumber が未インストール')

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [(page.extract_text() or '') for page in pdf.pages]
    except Exception as exc:
        # 破損PDF等。握りつぶさずステータスとして残す。
        return ExtractionResult(ExtractionStatus.FETCH_FAILED, None, 0, ext,
                                f'{type(exc).__name__}: {exc}')

    text = '\n'.join(pages).strip()
    if len(text) < MIN_PDF_TEXT_CHARS:
        return ExtractionResult(
            ExtractionStatus.UNSUPPORTED_SCANNED, None, len(text), ext,
            f'テキスト層が {len(text)} 文字しかありません（スキャン画像の可能性）')

    ratio = cid_ratio(text)
    if ratio > MAX_CID_RATIO:
        # 抽出は成功しているが中身が (cid:1234) の羅列。
        # 文字数は十分あるので件数監視では絶対に検知できない。
        # ここで止めないと、意味不明な文字列をLLMに投げて
        # 「関係なし」と判定され、取りこぼしになる。
        return ExtractionResult(
            ExtractionStatus.UNSUPPORTED_SCANNED, None, len(text), ext,
            f'文字コードを解決できませんでした（cid率 {ratio:.0%}）。'
            'フォントに ToUnicode マップが無いPDFの可能性があります')

    return ExtractionResult(ExtractionStatus.OK, text, len(text), ext)


def cid_ratio(text: str) -> float:
    """解読できなかったグリフの割合。0 に近いほど正しく解読できている。

    文字数ではなく**グリフ数**で数える。`(cid:229)` は10文字だが実体は1グリフなので、
    文字数比で測ると短いテキストで過大に出て誤検知する。
    """
    if not text:
        return 0.0
    cid_count = len(_CID_TOKEN.findall(text))
    if cid_count == 0:
        return 0.0
    other_glyphs = len(_CID_TOKEN.sub('', text))
    return cid_count / (cid_count + other_glyphs)


def extract(content: bytes, *, url: str) -> ExtractionResult:
    """拡張子に応じて抽出する。対象外は必ずステータスを立てて返す。"""
    ext = guess_extension(url)

    pre = classify(url)
    if pre is not None:
        return ExtractionResult(pre, None, 0, ext,
                                f'Phase 1 では未対応の形式です: {ext or "(拡張子なし)"}')

    if ext == '.pdf':
        return extract_pdf(content, url=url)

    if ext in {'.txt', '.csv', '.html', '.htm'}:
        text = content.decode('utf-8', errors='replace').strip()
        return ExtractionResult(ExtractionStatus.OK, text, len(text), ext)

    if ext == '.docx':
        try:
            import docx  # python-docx
        except ImportError:  # pragma: no cover
            return ExtractionResult(ExtractionStatus.SKIPPED, None, 0, ext,
                                    'python-docx が未インストール')
        try:
            document = docx.Document(io.BytesIO(content))
            text = '\n'.join(p.text for p in document.paragraphs).strip()
        except Exception as exc:
            return ExtractionResult(ExtractionStatus.FETCH_FAILED, None, 0, ext,
                                    f'{type(exc).__name__}: {exc}')
        return ExtractionResult(ExtractionStatus.OK, text, len(text), ext)

    if ext == '.xlsx':
        try:
            import openpyxl
        except ImportError:  # pragma: no cover
            return ExtractionResult(ExtractionStatus.SKIPPED, None, 0, ext,
                                    'openpyxl が未インストール')
        try:
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True,
                                        data_only=True)
            parts: list[str] = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        parts.append('\t'.join(cells))
            text = '\n'.join(parts).strip()
        except Exception as exc:
            return ExtractionResult(ExtractionStatus.FETCH_FAILED, None, 0, ext,
                                    f'{type(exc).__name__}: {exc}')
        return ExtractionResult(ExtractionStatus.OK, text, len(text), ext)

    return ExtractionResult(ExtractionStatus.UNSUPPORTED_FORMAT, None, 0, ext,
                            f'判定できない形式です: {ext or "(拡張子なし)"}')
