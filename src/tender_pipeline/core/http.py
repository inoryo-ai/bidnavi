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

    def get(self, url: str, **kwargs: object) -> requests.Response:
        """GET する。失敗は例外で伝える（Noneを返して黙らせない）。"""
        # CR-102: 連絡先が入っていない User-Agent で自治体のサーバを叩かない。
        # 相手が問題を感じたときに連絡できないクローラは走らせてはいけない。
        if UNSET_CONTACT in self.user_agent:
            raise ContactNotConfigured(
                f'User-Agent に問い合わせ先が設定されていません。'
                f'環境変数 {USER_AGENT_CONTACT_ENV} を設定するか、'
                f'RateLimitedClient(user_agent=build_user_agent()) を使ってください。'
            )

        if self.respect_robots and not self._policy_for(url).can_fetch(self.user_agent, url):
            raise RobotsDisallowed(f'robots.txt により禁止されています: {url}')

        host = urlparse(url).netloc
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._wait_for_host(host)
            try:
                res = self._session.get(
                    url, timeout=self.timeout,
                    headers={'User-Agent': self.user_agent}, **kwargs)  # type: ignore[arg-type]
            except requests.RequestException as exc:
                last_error = exc
            else:
                if res.status_code < 400:
                    return res
                # 5xx は一時障害の可能性があるのでリトライ、4xx は即座に失敗
                last_error = FetchError(f'HTTP {res.status_code}: {url}')
                if res.status_code < 500:
                    break
            if attempt < self.max_retries:
                time.sleep(self.min_interval * (attempt + 1))

        raise FetchError(f'取得に失敗しました: {url}') from last_error
