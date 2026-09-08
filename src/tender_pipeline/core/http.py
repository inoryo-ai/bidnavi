"""HTTPクライアント（要件定義 v2.0 FR-109 / CR-102）

相手は自治体のサーバである。負荷をかけないことが最優先。
  - ホスト単位で最小リクエスト間隔を空ける（既定1秒）
  - 同時接続は1（このクライアントは逐次実行専用）
  - User-Agent にサービス名と連絡先を明記する
  - robots.txt を尊重する

robots.txt の扱いで一箇所だけ注意がある:
  多くの自治体サイトは robots.txt を置いておらず、存在しないパスに対して
  **HTTPステータス200のまま「お探しのページは見つかりません」HTMLを返す**。
  これを robotparser にそのまま食わせると、HTMLを規則として解釈してしまう。
  実際に横浜市で同じ挙動を確認したため、content-type と中身で防御している。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

logger = logging.getLogger('tender_pipeline.http')

#: CR-102: User-Agent にはサービス名と**到達可能な問い合わせ先**を明記する。
#: 相手（自治体）が問題を感じたときに連絡できないクローラを走らせてはいけない。
#:
#: 連絡先は環境変数で渡す。ソースに個人のメールアドレスを埋め込むと、
#: リポジトリを公開した瞬間に永続的に露出する。
#: 未設定のまま実サイトへクロールすることは許さない（build_user_agent 参照）。
USER_AGENT_CONTACT_ENV = 'TENDER_PIPELINE_CONTACT'
USER_AGENT_TEMPLATE = 'public-tender-pipeline/0.1 (+{url}; contact: {contact})'
DEFAULT_PROJECT_URL = 'https://github.com/inoryo-ai/public-tender-pipeline'

#: 連絡先未設定時のプレースホルダ。これで外部サイトを叩かせない。
UNSET_CONTACT = 'CONTACT-NOT-SET'
DEFAULT_USER_AGENT = USER_AGENT_TEMPLATE.format(
    url=DEFAULT_PROJECT_URL, contact=UNSET_CONTACT)


class ContactNotConfigured(RuntimeError):
    """問い合わせ先が未設定のまま外部サイトへアクセスしようとした。"""


def build_user_agent(*, url: str = DEFAULT_PROJECT_URL) -> str:
    """環境変数 TENDER_PIPELINE_CONTACT から User-Agent を組み立てる。

    :raises ContactNotConfigured: 未設定の場合
    """
    contact = os.environ.get(USER_AGENT_CONTACT_ENV, '').strip()
    if not contact:
        raise ContactNotConfigured(
            f'環境変数 {USER_AGENT_CONTACT_ENV} に問い合わせ先を設定してください。'
            f'例: export {USER_AGENT_CONTACT_ENV}="you@example.com"'
        )
    return USER_AGENT_TEMPLATE.format(url=url, contact=contact)
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2


class FetchError(RuntimeError):
    """取得に失敗した。呼び出し側に必ず伝わるよう例外にする。"""


class RobotsDisallowed(FetchError):
    """robots.txt で禁止されている。"""


@dataclass(slots=True)
class RobotsPolicy:
    """1ホスト分の robots.txt。取得できなければ「制限なし」として扱う。"""
    allowed_all: bool
    parser: urllib.robotparser.RobotFileParser | None
    source: str  # 'parsed' | 'missing' | 'not_robots' | 'error'

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.allowed_all or self.parser is None:
            return True
        return self.parser.can_fetch(user_agent, url)


def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith('<!doctype html') or head.startswith('<html') or '<head>' in head


def fetch_robots(base_url: str, *, user_agent: str, timeout: float) -> RobotsPolicy:
    """robots.txt を取得して解釈する。

    404 でも、HTMLが返ってきても、通信に失敗しても「制限なし」とする。
    ただし source にどれだったかを残し、後から監査できるようにする。
    """
    parsed = urlparse(base_url)
    robots_url = f'{parsed.scheme}://{parsed.netloc}/robots.txt'
    try:
        res = requests.get(robots_url, timeout=timeout,
                           headers={'User-Agent': user_agent})
    except requests.RequestException as exc:
        logger.warning('robots.txt 取得に失敗（制限なしとして扱う） %s: %s', robots_url, exc)
        return RobotsPolicy(True, None, 'error')

    if res.status_code != 200:
        return RobotsPolicy(True, None, 'missing')

    content_type = res.headers.get('Content-Type', '')
    if 'html' in content_type.lower() or _looks_like_html(res.text):
        # 404ページをHTMLで返すサイト。規則として解釈してはいけない。
        logger.warning('robots.txt がHTMLを返した（制限なしとして扱う）: %s', robots_url)
        return RobotsPolicy(True, None, 'not_robots')

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(res.text.splitlines())
    return RobotsPolicy(False, parser, 'parsed')


#: 1リクエストで受け取ってよい最大バイト数（既定 25MB）。
#: 自治体の仕様書PDFは通常 数百KB〜数MB。これを大きく超えるものは
#: 取り違えか攻撃入力とみなす。
#:
#: 🔴 上限が無いと、docx/xlsx（実体はzip）で解凍爆弾が成立する。
#:    実測: 51KB の圧縮ファイルが 50MB に展開される（増幅率 1,026倍）。
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

#: 取得を許可するスキーム。file:// や ftp:// でローカルを読ませない。
_ALLOWED_SCHEMES: frozenset[str] = frozenset({'http', 'https'})


class UnsafeUrl(ValueError):
    """取得してはいけないURL（SSRF対策）。"""


class ResponseTooLarge(FetchError):
    """レスポンスが上限を超えた。解凍爆弾・巨大ファイル対策。"""


def _is_private_host(host: str) -> bool:
    """内部ネットワーク宛かどうか。

    名前解決までは行わない（DNSリバインディングは別の対策が要る）。
    ここで止めたいのは、案件ページのリンクに
    `http://169.254.169.254/`（クラウドのメタデータ）や `http://127.0.0.1:8080/`
    が紛れていた場合に、それをそのまま取りに行ってしまうこと。
    """
    name = host.split(':')[0].strip('[]').lower()
    if not name or name == 'localhost' or name.endswith('.localhost'):
        return True
    if name.endswith('.internal') or name.endswith('.local'):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False  # ホスト名。名前解決の結果までは見ない
    return (address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast or address.is_unspecified)


def assert_fetchable(url: str) -> None:
    """取得してよいURLかを検査する。危険なら UnsafeUrl。

    クローラは「案件ページに書かれているリンク」を辿る。
    リンクの中身は相手のサイトが決めるので、こちらの内部ネットワークへ
    誘導される可能性を常に前提にする。
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrl(f'許可されていないスキームです: {parsed.scheme or "(なし)"} — {url}')
    if not parsed.netloc:
        raise UnsafeUrl(f'ホストがありません: {url}')
    if _is_private_host(parsed.netloc):
        raise UnsafeUrl(f'内部ネットワーク宛のURLは取得しません: {url}')


def _assert_within_size_limit(res: requests.Response, max_bytes: int) -> None:
    """本文サイズの上限を検査する。

    Content-Length は自己申告なので、実体のバイト数でも必ず確認する。
    申告だけ信じると、嘘の Content-Length で上限を回避できてしまう。
    """
    declared = res.headers.get('Content-Length')
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise ResponseTooLarge(
            f'Content-Length が上限を超えています: {int(declared):,} > {max_bytes:,} — {res.url}')

    actual = len(res.content)
    if actual > max_bytes:
        raise ResponseTooLarge(
            f'レスポンスが上限を超えています: {actual:,} > {max_bytes:,} — {res.url}')


@dataclass(slots=True)
class RateLimitedClient:
    """ホスト単位でレート制限をかける逐次HTTPクライアント。

    並列化したくなったら、このクラスではなく上位でホストを分けること。
    同一ホストへの同時接続を増やしてはいけない。
    """
    user_agent: str = DEFAULT_USER_AGENT
    min_interval: float = DEFAULT_MIN_INTERVAL
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    respect_robots: bool = True
    max_bytes: int = DEFAULT_MAX_BYTES
    _last_request_at: dict[str, float] = field(default_factory=dict, init=False)
    _robots: dict[str, RobotsPolicy] = field(default_factory=dict, init=False)
    _session: requests.Session = field(default_factory=requests.Session, init=False)

    def _wait_for_host(self, host: str) -> None:
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last_request_at[host] = time.monotonic()

    def _policy_for(self, url: str) -> RobotsPolicy:
        host = urlparse(url).netloc
        if host not in self._robots:
            self._robots[host] = fetch_robots(
                url, user_agent=self.user_agent, timeout=self.timeout)
        return self._robots[host]

    def get(self, url: str) -> requests.Response:
        """GET する。失敗は例外で伝える（Noneを返して黙らせない）。

        **任意の kwargs を requests に素通しさせない**のは意図的。
        通していると、呼び出し側が timeout や headers を上書きでき、
        レート制限・User-Agent・タイムアウトという「相手に迷惑をかけない」
        ための設定を、このクラスの外から壊せてしまう。
        必要な設定が出てきたら、このクラスの属性として明示的に足す。
        """
        # CR-102: 連絡先が入っていない User-Agent で自治体のサーバを叩かない。
        # 相手が問題を感じたときに連絡できないクローラは走らせてはいけない。
        if UNSET_CONTACT in self.user_agent:
            raise ContactNotConfigured(
                f'User-Agent に問い合わせ先が設定されていません。'
                f'環境変数 {USER_AGENT_CONTACT_ENV} を設定するか、'
                f'RateLimitedClient(user_agent=build_user_agent()) を使ってください。'
            )

        # 取得先の妥当性を、robots.txt を見に行くより先に検査する。
        # 不正なURLで robots.txt を取りに行くこと自体が SSRF になるため。
        assert_fetchable(url)

        if self.respect_robots and not self._policy_for(url).can_fetch(self.user_agent, url):
            raise RobotsDisallowed(f'robots.txt により禁止されています: {url}')

        host = urlparse(url).netloc
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._wait_for_host(host)
            try:
                res = self._session.get(
                    url, timeout=self.timeout,
                    headers={'User-Agent': self.user_agent})
            except requests.RequestException as exc:
                last_error = exc
            else:
                if res.status_code < 400:
                    # リダイレクト先が内部アドレスへ誘導されていないか確認する。
                    # requests は既定でリダイレクトを追うため、最終URLで再検査する。
                    assert_fetchable(res.url)
                    _assert_within_size_limit(res, self.max_bytes)
                    return res
                # 5xx は一時障害の可能性があるのでリトライ、4xx は即座に失敗
                last_error = FetchError(f'HTTP {res.status_code}: {url}')
                if res.status_code < 500:
                    break
            if attempt < self.max_retries:
                time.sleep(self.min_interval * (attempt + 1))

        raise FetchError(f'取得に失敗しました: {url}') from last_error
