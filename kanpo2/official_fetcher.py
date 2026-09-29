"""Bounded, user-started retrieval of links published by the official Kanpo site.

No account, browser automation, external dependency, scheduled crawling or URL
guessing is used. The caller must show the current site rules and let the user
review the discovered items before downloading. Retrieved PDFs are unmodified;
the application does not verify their electronic signatures.
"""
from dataclasses import dataclass, field
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
from http.client import HTTPException
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser


BASE_URL = 'https://www.kanpo.go.jp/'
GUIDANCE_URL = BASE_URL + 'guidance.html'
USER_AGENT = 'KanpoChecker/0.7 (user-initiated daily document retrieval)'
MAX_RANGE_DAYS = 7
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_PDF_BYTES = 100 * 1024 * 1024
VIEWER_PATTERN = re.compile(
    r'^/(?P<day>\d{8})/(?P<issue>\d{8}[a-z]\d+)/'
    r'(?P=issue)full(?P<first>\d{4})(?P<last>\d{4})f\.html$')
PDF_PATTERN = re.compile(
    r'^/(?P<day>\d{8})/(?P<issue>\d{8}[a-z]\d+)/pdf/'
    r'(?P=issue)full(?P<first>\d{4})(?P<last>\d{4})\.pdf$')
CONTENTS_PATTERN = re.compile(r'^/(\d{8})/\1\.fullcontents\.html$')


class FetchError(RuntimeError):
    """A visible failure. stop_all=True means the caller must stop the batch."""
    def __init__(self, message, *, stop_all=False):
        super().__init__(message)
        self.stop_all = stop_all


class FetchCancelled(FetchError):
    def __init__(self):
        super().__init__('取得を中止しました。', stop_all=True)


@dataclass(frozen=True)
class Issue:
    date: str
    title: str
    viewer_url: str
    source_url: str
    first_page: int
    last_page: int


@dataclass
class DiscoveryReport:
    items: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    restricted_urls: list = field(default_factory=list)
    robots_excluded_urls: list = field(default_factory=list)


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    sha256: str
    url: str
    issue: Issue


def official_url(url):
    """Validate before any request, including redirects. Never trust a suffix."""
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == 'https' and parts.hostname == 'www.kanpo.go.jp'
                 and parts.port in (None, 443) and parts.username is None
                 and parts.password is None and not parts.query
                 and not any(c in url for c in ('\\', '\r', '\n', '\t')))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise FetchError('公式サイト以外、または安全でない取得先を拒否しました。', stop_all=True)
    return parts._replace(fragment='').geturl()


class _OfficialRedirect(HTTPRedirectHandler):
    def __init__(self, validate=None):
        self.validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        official_url(newurl)
        if self.validate:
            self.validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.current = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.current = [attrs['href'], []]
        elif tag in ('iframe', 'embed') and attrs.get('src'):
            self.links.append((attrs['src'], attrs.get('title', '')))
        elif tag == 'object' and attrs.get('data'):
            self.links.append((attrs['data'], attrs.get('title', '')))
        elif tag == 'br' and self.current is not None:
            self.current[1].append(' ')

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            href, text = self.current
            self.links.append((href, re.sub(r'\s+', ' ', ''.join(text)).strip()))
            self.current = None


def _links(html):
    parser = _Links()
    parser.feed(html)
    return parser.links


def _as_date(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('日付は YYYY-MM-DD の形式で指定してください。') from None


def _today_jst():
    return datetime.now(timezone(timedelta(hours=9))).date()


def parse_index(html, start, end, *, today=None, max_items=40):
    """Pure parser for the homepage's advertised whole-issue viewer links."""
    start, end = _as_date(start), _as_date(end)
    today = _today_jst() if today is None else _as_date(today)
    if start > end or (end - start).days >= MAX_RANGE_DAYS:
        raise ValueError('開始日から終了日までを7日以内で指定してください。')
    if end > today or start < today - timedelta(days=89):
        raise ValueError('直近90日以内（未来を除く）の日付を指定してください。')
    links, contents, titles = _links(html), {}, {}
    for href, label in links:
        url = urljoin(BASE_URL, href)
        try:
            official_url(url)
        except FetchError:
            continue
        path = urlsplit(url).path
        match = CONTENTS_PATTERN.fullmatch(path)
        if match:
            contents[match[1]] = url
        # Issue title links precede one or more whole-issue PDF segments.
        parts = path.split('/')
        if len(parts) == 4 and re.fullmatch(r'\d{8}[a-z]\d+', parts[2]):
            if parts[3] == parts[2] + '0000f.html':
                titles[parts[2]] = label
    report, seen, advertised = DiscoveryReport(), set(), 0
    for href, label in links:
        url = urljoin(BASE_URL, href)
        match = VIEWER_PATTERN.fullmatch(urlsplit(url).path)
        if not match:
            continue
        # A matching-looking link on another host is a structural integrity error.
        official_url(url)
        try:
            issued = date.fromisoformat(match['day'][:4] + '-' + match['day'][4:6] + '-' + match['day'][6:])
        except ValueError:
            raise FetchError('公式一覧の日付形式を認識できません。取得を中止しました。') from None
        if not match['issue'].startswith(match['day']):
            raise FetchError('公式一覧の発行日と号情報が一致しません。')
        advertised += 1
        if not start <= issued <= end or url in seen:
            continue
        first, last = int(match['first']), int(match['last'])
        if first < 1 or first > last:
            raise FetchError('公式一覧のページ範囲を認識できません。')
        source = contents.get(match['day'])
        if not source:
            raise FetchError('日別目次リンクを確認できません。サイト構造を確認してください。')
        seen.add(url)
        title = titles.get(match['issue'], match['issue'])
        report.items.append(Issue(issued.isoformat(), title, url, source, first, last))
    if not advertised:
        raise FetchError('一括PDFリンクを確認できません。通信内容またはサイト構造を確認してください。')
    if len(report.items) > max_items:
        raise FetchError(f'対象が上限の{max_items}件を超えました。期間を短くしてください。')
    report.items.sort(key=lambda item: (item.date, item.viewer_url))
    dates = {item.date for item in report.items}
    missing = [(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)
               if (start + timedelta(days=i)).isoformat() not in dates]
    if missing:
        report.warnings.append('公開一覧に対象リンクのない日: ' + ', '.join(missing)
                               + '（休日・未公開等。発行なしの確定ではありません）')
    return report


class OfficialFetcher:
    """One short user-approved batch; reuse this object to retain rate limits.

    list_issues accepts datetime.date or YYYY-MM-DD strings and returns a report.
    download returns path, actual PDF URL and SHA-256 for local import/deduplication.
    cancelled is an optional zero-argument predicate (e.g. threading.Event.is_set).
    daily_manual records robots exclusions as advisory while limiting downloads
    to this session's advertised JST-today issues. The caller must still persist
    the daily attempt before downloading; this object alone is not a daily lock.
    """
    def __init__(self, timeout=30, interval=3, max_items=40, *, cancelled=None,
                 daily_manual=False):
        if not 1 <= timeout <= 60 or not 3 <= interval <= 60 or not 1 <= max_items <= 40:
            raise ValueError('timeoutは1～60秒、intervalは3～60秒、max_itemsは1～40で指定してください。')
        self.timeout, self.interval, self.max_items = timeout, interval, max_items
        self.cancelled = cancelled or (lambda: False)
        if type(daily_manual) is not bool:
            raise ValueError('daily_manualは真偽値で指定してください。')
        self.daily_manual = daily_manual
        self._opener = build_opener(_OfficialRedirect(self._redirect_allowed))
        self._last_request, self._requests = None, 0
        self._robots, self._robots_loaded = None, False
        self._listed_day, self._advertised = None, {}
        self._attempted_viewers = set()
        self.robots_excluded_urls = set()
        self._expected_download = None

    def _check_cancel(self):
        if self.cancelled():
            raise FetchCancelled()

    def _redirect_allowed(self, url):
        self._check_cancel()
        self._validate_download_target(url)
        if self._robots is not None and not self._robots.can_fetch('KanpoChecker', url):
            self.robots_excluded_urls.add(url)
            if not self.daily_manual:
                raise FetchError('転送先の取得がrobots.txtで許可されていません。', stop_all=True)
        self._before_request()

    def _validate_download_target(self, url):
        url = official_url(url)
        if self._expected_download is not None:
            kind, expected = self._expected_download
            pattern = VIEWER_PATTERN if kind == 'viewer' else PDF_PATTERN
            match = pattern.fullmatch(urlsplit(url).path)
            if not match or match.groupdict() != expected:
                raise FetchError('選択した号・ページと異なる取得先への転送を拒否しました。', stop_all=True)

    @contextmanager
    def _expect_download_target(self, kind, expected):
        previous = self._expected_download
        self._expected_download = (kind, expected)
        try:
            yield
        finally:
            self._expected_download = previous

    def _before_request(self):
        self._check_cancel()
        if self._requests >= self.max_items * 2 + 5:
            raise FetchError('1回の操作の通信上限に達しました。', stop_all=True)
        if self._last_request is not None:
            remaining = self.interval - (time.monotonic() - self._last_request)
            while remaining > 0:
                time.sleep(min(remaining, 0.2))
                self._check_cancel()
                remaining = self.interval - (time.monotonic() - self._last_request)
        if self.daily_manual and self._expected_download is not None and self._listed_day != _today_jst():
            raise FetchError('取得中に日本時間の日付が変わりました。後続の取得を停止します。', stop_all=True)
        self._last_request = time.monotonic()
        self._requests += 1

    def _open(self, url):
        url = official_url(url)
        self._before_request()
        try:
            response = self._opener.open(Request(url, headers={'User-Agent': USER_AGENT,
                                                             'Accept-Encoding': 'identity'}),
                                         timeout=self.timeout)
            try:
                self._validate_download_target(response.geturl())
            except FetchError:
                response.close()
                raise
            return response
        except HTTPError as error:
            error.close()
            if error.code in (403, 429):
                raise FetchError(f'公式サイトが取得を制限しました（HTTP {error.code}）。再試行せず停止します。',
                                 stop_all=True) from None
            if error.code == 404:
                raise FetchError('公開リンクが見つかりません（HTTP 404）。公開期間・公式サイトをご確認ください。') from error
            raise FetchError(f'公式サイトで通信エラーが発生しました（HTTP {error.code}）。') from None
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            raise FetchError('公式サイトに接続できません。ネットワーク・社内プロキシ・証明書設定をご確認ください。') from error

    def _bytes(self, url, limit=MAX_HTML_BYTES):
        try:
            with self._open(url) as response:
                content = response.read(limit + 1)
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            raise FetchError('公式サイトからのデータ受信に失敗しました。') from error
        self._check_cancel()
        if len(content) > limit:
            raise FetchError('取得データがサイズ上限を超えました。')
        return content

    def _allowed(self, url):
        if not self._robots_loaded:
            try:
                raw = self._bytes(BASE_URL + 'robots.txt', 256 * 1024)
            except FetchError as error:
                if not isinstance(error.__cause__, HTTPError) or error.__cause__.code != 404:
                    raise
            else:
                self._robots = RobotFileParser()
                self._robots.parse(raw.decode('utf-8-sig', errors='replace').splitlines())
                delay = self._robots.crawl_delay('KanpoChecker') or self._robots.crawl_delay('*')
                if delay:
                    if delay > 60:
                        raise FetchError('公式サイトが長い取得間隔を指定しています。ブラウザで確認してください。', stop_all=True)
                    self.interval = max(self.interval, delay)
            self._robots_loaded = True
        if self._robots is not None and not self._robots.can_fetch('KanpoChecker', url):
            self.robots_excluded_urls.add(url)
            if not self.daily_manual:
                raise FetchError('公式サイトのrobots.txtがこの取得を許可していません。', stop_all=True)

    def list_issues(self, start, end):
        # Validate the requested range before making any network request.
        start, end = _as_date(start), _as_date(end)
        today = _today_jst()
        if self.daily_manual and (start != today or end != today):
            raise ValueError('利用者が開始する日次取得は、日本時間の当日分だけを対象にします。')
        if start > end or (end-start).days >= MAX_RANGE_DAYS:
            raise ValueError('開始日から終了日までを7日以内で指定してください。')
        if end > today or start < today - timedelta(days=89):
            raise ValueError('直近90日以内（未来を除く）の日付を指定してください。')
        self._allowed(BASE_URL)
        raw = self._bytes(BASE_URL)
        try:
            html = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            html = raw.decode('cp932', errors='replace')
        report = parse_index(html, start, end, today=today, max_items=self.max_items)
        if self._robots is not None:
            report.robots_excluded_urls = [item.viewer_url for item in report.items
                                          if not self._robots.can_fetch('KanpoChecker', item.viewer_url)]
            self.robots_excluded_urls.update(report.robots_excluded_urls)
            if not self.daily_manual:
                report.restricted_urls = list(report.robots_excluded_urls)
        if report.restricted_urls:
            report.warnings.append(
                f'公式サイトのrobots.txtがPDFの自動取得を許可していないため、対象{len(report.restricted_urls)}件は'
                'このアプリから取得できません。官報サイトをブラウザで開いてPDFを保存し、'
                '「PDF取り込み」を使ってください。')
        elif report.robots_excluded_urls:
            report.warnings.append(
                'robots.txtには対象経路のクロール除外指定があります。利用者が開始する当日1回の取得モードで、'
                '公開一覧にある号だけを間隔を空けて取得します。HTTP 403・429等の拒否では停止します。')
        if self.daily_manual:
            self._listed_day = today
            self._advertised = {item.viewer_url: item for item in report.items}
        return report

    def download(self, issue, directory):
        official_url(issue.viewer_url)
        viewer_match = VIEWER_PATTERN.fullmatch(urlsplit(issue.viewer_url).path)
        if not viewer_match:
            raise FetchError('公式の一括PDF表示リンクではありません。')
        if self.daily_manual:
            today = _today_jst()
            if self._listed_day != today or issue.date != today.isoformat():
                raise FetchError('当日の公開一覧を確認した日次取得セッションでのみ取得できます。', stop_all=True)
            if self._advertised.get(issue.viewer_url) != issue:
                raise FetchError('この取得セッションの公開一覧にない号は取得できません。', stop_all=True)
            if issue.viewer_url in self._attempted_viewers:
                raise FetchError('同じ号の取得は既に試行済みです。このセッションでは再取得しません。', stop_all=True)
            self._attempted_viewers.add(issue.viewer_url)
        self._allowed(issue.viewer_url)
        with self._expect_download_target('viewer', viewer_match.groupdict()):
            html = self._bytes(issue.viewer_url).decode('utf-8-sig', errors='replace')
        urls = set()
        for href, label in _links(html):
            url = urljoin(issue.viewer_url, href)
            match = PDF_PATTERN.fullmatch(urlsplit(url).path)
            if match:
                official_url(url)
                if match.groupdict() != viewer_match.groupdict():
                    continue
                urls.add(url)
        if len(urls) != 1:
            raise FetchError('対応する一括PDFの実リンクを確認できません。公式サイトを確認してください。')
        url = urls.pop()
        self._allowed(url)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / Path(urlsplit(url).path).name
        temporary = None
        digest, length, started = hashlib.sha256(), 0, time.monotonic()
        try:
            with self._expect_download_target('pdf', viewer_match.groupdict()), self._open(url) as response:
                raw_length = response.headers.get('Content-Length')
                try:
                    expected = int(raw_length) if raw_length is not None else None
                except ValueError:
                    raise FetchError('PDFのサイズ情報を認識できません。') from None
                if expected is not None and (expected < 5 or expected > MAX_PDF_BYTES):
                    raise FetchError('PDFがサイズ上限（100MB）を超えるか、空のファイルです。')
                with tempfile.NamedTemporaryFile(prefix='.kanpo-', suffix='.part', dir=directory,
                                                 delete=False) as stream:
                    temporary = Path(stream.name)
                    while True:
                        # Preserve a completed response even if cancellation arrived
                        # immediately after the final declared bytes were received.
                        if expected is None or length < expected:
                            self._check_cancel()
                        if time.monotonic() - started > 180:
                            raise FetchError('PDFの取得時間が上限の180秒を超えました。')
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        if length == 0 and not chunk.startswith(b'%PDF-'):
                            raise FetchError('PDF以外の応答を受信しました。保存しません。')
                        length += len(chunk)
                        if length > MAX_PDF_BYTES:
                            raise FetchError('PDFがサイズ上限（100MB）を超えました。')
                        digest.update(chunk)
                        stream.write(chunk)
                if length < 5 or (expected is not None and length != expected):
                    raise FetchError('PDFの受信が完了していません。保存しません。')
            # Atomic replacement keeps interrupted downloads away from importers.
            temporary.replace(destination)
            return DownloadResult(destination, digest.hexdigest(), url, issue)
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            raise FetchError('PDFの取得または保存に失敗しました。ネットワーク・空き容量をご確認ください。') from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
