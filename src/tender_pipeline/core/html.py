"""HTML の読み込み（要件定義 DQ-01 文字化け検出）

文字コードの事故を構造的に防ぐ。

実測で見つかった事故（2026-09-07 / 横浜市）:
  レスポンスヘッダが `Content-Type: text/html` で **charset を含まない**。
  requests はこの場合 RFC に従って ISO-8859-1 とみなすため、
  `response.text` が文字化けする。

  例外は出ない。ページも200で返る。件数も正しい。
  しかし案件名が壊れるので:
    - natural_key が毎回変わり、名寄せが完全に破綻する
    - 【契約結果公表】の判定が効かず、終了案件を募集中として通知する
    - ユーザーには意味不明な文字列が届く

  つまり「データレベルのサイレント故障」であり、FR-105 の監視でも
  件数は正常に見えるため検知できない。入口で潰すしかない。

対策:
  クローラは response.text を使わない。必ず bytes を soup_from() に渡す。
  BeautifulSoup は meta charset と BOM から実際の文字コードを判定する。
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

#: UTF-8 を Latin-1 系として読んだときに頻出する文字
_MOJIBAKE_CHARS = re.compile(r'[ÃÂãåäæÐ][-¿†-™]')


def soup_from(content: bytes | str, *, parser: str = 'lxml') -> BeautifulSoup:
    """HTML を BeautifulSoup にする。

    bytes を渡すこと。str を渡す経路も残してあるのはテスト用の固定文字列のため。
    実際のレスポンスからは必ず `response.content`（bytes）を渡す。
    """
    return BeautifulSoup(content, parser)


def looks_mojibake(text: str, *, threshold: int = 2) -> bool:
    """文字化けしていそうかを判定する。

    完全な判定はできないので、監視・テストの補助として使う。
    「化けていない」ことの証明には使えないが、
    「化けている」ことの検出には十分実用的。
    """
    return len(_MOJIBAKE_CHARS.findall(text)) >= threshold
