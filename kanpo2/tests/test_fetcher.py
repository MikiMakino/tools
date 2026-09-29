"""Offline regression cases based on the public site's 2026-09 link format."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request
from urllib.robotparser import RobotFileParser

import official_fetcher as fetcher


DAY = date(2026, 9, 25)
ROOT = 'https://www.kanpo.go.jp/'
VIEWER = ROOT + '20260925/20260925t00052/20260925t00052full00010002f.html'
PDF_URL = ROOT + '20260925/20260925t00052/pdf/20260925t00052full00010002.pdf'
PDF = b'%PDF-1.7\nexample offline fixture\n%%EOF\n'
INDEX = '''<dl><dt><a href="./20260925/20260925.fullcontents.html">全体目次はこちら</a></dt>
<dd><a class="articleTop" href="./20260925/20260925t00052/20260925t000520000f.html">特別号外<br>(第52号)</a>
<a href="./20260925/20260925t00052/20260925t00052full00010002f.html"><img alt="PDF">1-2頁[1MB]</a></dd></dl>'''
VIEWER_HTML = '<iframe src="./pdf/20260925t00052full00010002.pdf" title="一括PDF"></iframe>'


def issue():
    return fetcher.parse_index(INDEX, DAY, DAY, today=DAY).items[0]


class Response(io.BytesIO):
    def __init__(self, content=PDF, length=None):
        super().__init__(content)
        self.headers = {} if length is None else {'Content-Length': str(length)}

    def geturl(self):
        return PDF_URL


def client(response=None, html=VIEWER_HTML):
    value = fetcher.OfficialFetcher()
    value._allowed = Mock()
    value._bytes = Mock(return_value=html.encode())
    value._open = Mock(return_value=response or Response())
    return value


class IndexTests(unittest.TestCase):
    def test_default_index_date_uses_jst_and_explicit_date_does_not_read_clock(self):
        with patch.object(fetcher, '_today_jst', return_value=DAY) as clock:
            report = fetcher.parse_index(INDEX, DAY, DAY)
        self.assertEqual(len(report.items), 1)
        clock.assert_called_once_with()
        with patch.object(fetcher, '_today_jst', side_effect=AssertionError('unexpected clock')):
            self.assertEqual(len(fetcher.parse_index(INDEX, DAY, DAY, today=DAY).items), 1)

    def test_observed_iframe_link_format_and_source_attribution(self):
        report = fetcher.parse_index(INDEX, DAY, DAY, today=DAY)
        self.assertEqual(len(report.items), 1)
        item = report.items[0]
        self.assertEqual(item.date, '2026-09-25')
        self.assertEqual(item.title, '特別号外 (第52号)')
        self.assertEqual((item.first_page, item.last_page), (1, 2))
        self.assertEqual(item.source_url, ROOT + '20260925/20260925.fullcontents.html')
        self.assertEqual(report.warnings, [])

    def test_split_volumes_preserved_and_duplicate_links_removed(self):
        second = INDEX.replace('full00010002', 'full00030004')
        report = fetcher.parse_index(INDEX + INDEX + second, DAY, DAY, today=DAY)
        self.assertEqual([(v.first_page, v.last_page) for v in report.items], [(1, 2), (3, 4)])

    def test_range_must_be_seven_days_or_less(self):
        with self.assertRaises(ValueError):
            fetcher.parse_index(INDEX, DAY-timedelta(days=7), DAY, today=DAY)
        with self.assertRaises(ValueError):
            fetcher.parse_index(INDEX, DAY, DAY-timedelta(days=1), today=DAY)

    def test_future_or_expired_window_rejected(self):
        for requested in (DAY+timedelta(days=1), DAY-timedelta(days=90)):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                fetcher.parse_index(INDEX, requested, requested, today=DAY)

    def test_empty_selected_date_warns_instead_of_claiming_no_issue(self):
        report = fetcher.parse_index(INDEX, DAY-timedelta(days=1), DAY-timedelta(days=1), today=DAY)
        self.assertEqual(report.items, [])
        self.assertIn('2026-09-24', report.warnings[0])
        self.assertIn('確定ではありません', report.warnings[0])

    def test_structure_change_is_visible_failure(self):
        with self.assertRaises(fetcher.FetchError):
            fetcher.parse_index('<html>Maintenance</html>', DAY, DAY, today=DAY)
        with self.assertRaises(fetcher.FetchError):
            fetcher.parse_index(INDEX.replace('fullcontents.html', 'contents.html'), DAY, DAY, today=DAY)

    def test_limit_is_failure_not_silent_partial_result(self):
        with self.assertRaises(fetcher.FetchError):
            fetcher.parse_index(INDEX + INDEX.replace('full00010002', 'full00030004'),
                                DAY, DAY, today=DAY, max_items=1)

    def test_lookalike_host_advertisement_rejected(self):
        wrong = INDEX.replace('./20260925/20260925t00052/20260925t00052full',
                              'https://www.kanpo.go.jp.example.org/20260925/20260925t00052/20260925t00052full')
        with self.assertRaises(fetcher.FetchError):
            fetcher.parse_index(wrong, DAY, DAY, today=DAY)


class DownloadTests(unittest.TestCase):
    def test_cancel_after_declared_bytes_preserves_completed_pdf(self):
        cancelled = [False]
        class CompletedResponse(Response):
            def read(self, size=-1):
                data = super().read(size)
                if data:
                    cancelled[0] = True
                return data
        with tempfile.TemporaryDirectory() as folder:
            value = client(CompletedResponse(PDF, len(PDF)))
            value.cancelled = lambda: cancelled[0]
            result = value.download(issue(), folder)
            self.assertEqual(result.path.read_bytes(), PDF)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_cancel_after_response_finished_without_length_preserves_pdf(self):
        cancelled = [False]
        class FinishedResponse(Response):
            def __exit__(self, *args):
                cancelled[0] = True
                return super().__exit__(*args)
        with tempfile.TemporaryDirectory() as folder:
            value = client(FinishedResponse(PDF))
            value.cancelled = lambda: cancelled[0]
            result = value.download(issue(), folder)
            self.assertEqual(result.path.read_bytes(), PDF)

    def test_valid_pdf_written_with_actual_url_and_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            value = client(Response(PDF, len(PDF)))
            result = value.download(issue(), folder)
            self.assertEqual(result.path.read_bytes(), PDF)
            self.assertEqual(result.url, PDF_URL)
            self.assertEqual(result.sha256, hashlib.sha256(PDF).hexdigest())
            value._open.assert_called_once_with(PDF_URL)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_html_response_never_becomes_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(fetcher.FetchError):
                client(Response(b'<html>blocked</html>')).download(issue(), folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_partial_download_keeps_previous_file_and_removes_part(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / Path(PDF_URL).name
            destination.write_bytes(b'existing saved data')
            with self.assertRaises(fetcher.FetchError):
                client(Response(PDF, len(PDF) + 5)).download(issue(), folder)
            self.assertEqual(destination.read_bytes(), b'existing saved data')
            self.assertEqual(list(Path(folder).iterdir()), [destination])

    def test_stream_without_content_length_still_has_size_limit(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(fetcher, 'MAX_PDF_BYTES', 10):
            with self.assertRaises(fetcher.FetchError):
                client().download(issue(), folder)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_viewer_cannot_substitute_another_issue(self):
        with tempfile.TemporaryDirectory() as folder:
            value = client(html=VIEWER_HTML.replace('t00052', 't00051'))
            with self.assertRaises(fetcher.FetchError):
                value.download(issue(), folder)
            value._open.assert_not_called()

    def test_cancel_removes_partial_and_stops_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            value = client()
            value.cancelled = lambda: True
            with self.assertRaises(fetcher.FetchCancelled) as caught:
                value.download(issue(), folder)
            self.assertTrue(caught.exception.stop_all)
            self.assertEqual(list(Path(folder).iterdir()), [])


class TransportTests(unittest.TestCase):
    def test_jst_clock_uses_next_day_when_utc_is_still_previous_day(self):
        utc_now = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)
        with patch.object(fetcher, 'datetime') as clock:
            clock.now.side_effect = lambda zone: utc_now.astimezone(zone)
            self.assertEqual(fetcher._today_jst(), date(2026, 9, 29))
        self.assertEqual(clock.now.call_args.args[0].utcoffset(None), timedelta(hours=9))

    def test_discovery_uses_one_jst_date_for_validation_and_index_parsing(self):
        today = date(2050, 1, 1)
        html = INDEX.replace('20260925', today.strftime('%Y%m%d')).encode('utf-8')
        value = fetcher.OfficialFetcher()
        value._bytes = Mock(side_effect=[b'User-agent: *\nDisallow:\n', html])
        with patch.object(fetcher, '_today_jst', side_effect=[today]) as clock:
            report = value.list_issues(today, today)
        self.assertEqual(len(report.items), 1)
        self.assertEqual(report.items[0].date, today.isoformat())
        clock.assert_called_once_with()

    def test_discovery_marks_pdfs_forbidden_by_current_site_robots(self):
        today = fetcher._today_jst()
        html = INDEX.replace('20260925', today.strftime('%Y%m%d')).encode('utf-8')
        value = fetcher.OfficialFetcher()
        value._bytes = Mock(side_effect=[b'User-agent: *\nDisallow: /20\nDisallow: /old/\n', html])
        report = value.list_issues(today, today)
        self.assertEqual(len(report.items), 1)
        self.assertEqual(report.restricted_urls, [report.items[0].viewer_url])
        self.assertIn('PDFの自動取得を許可していない', report.warnings[0])
        self.assertIn('PDF取り込み', report.warnings[0])
        # Discovery never requests a forbidden issue viewer/PDF URL.
        self.assertEqual([call.args[0] for call in value._bytes.call_args_list],
                         [ROOT + 'robots.txt', ROOT])

    def test_discovery_with_allowed_pdfs_has_no_restrictions(self):
        today = fetcher._today_jst()
        html = INDEX.replace('20260925', today.strftime('%Y%m%d')).encode('utf-8')
        value = fetcher.OfficialFetcher()
        value._bytes = Mock(side_effect=[b'User-agent: *\nDisallow:\n', html])
        report = value.list_issues(today, today)
        self.assertEqual(len(report.items), 1)
        self.assertEqual(report.restricted_urls, [])
        self.assertEqual(report.warnings, [])

    def test_host_scheme_port_and_credentials_are_restricted(self):
        for url in ('http://www.kanpo.go.jp/', 'https://kanpo.go.jp/',
                    'https://www.kanpo.go.jp:444/', 'https://user@www.kanpo.go.jp/',
                    'https://www.kanpo.go.jp.example.com/', 'file:///C:/sample.pdf',
                    'https://www.kanpo.go.jp/?token=sample'):
            with self.subTest(url=url), self.assertRaises(fetcher.FetchError):
                fetcher.official_url(url)

    def test_redirect_validated_before_request_to_other_host(self):
        with self.assertRaises(fetcher.FetchError):
            fetcher._OfficialRedirect().redirect_request(Request(ROOT), None, 302, '', {},
                                                         'https://example.org/test.pdf')

    def test_403_and_429_stop_without_retries(self):
        for code in (403, 429):
            with self.subTest(code=code):
                value = fetcher.OfficialFetcher()
                value._opener = Mock()
                value._opener.open.side_effect = HTTPError(ROOT, code, 'blocked', {}, io.BytesIO())
                with self.assertRaises(fetcher.FetchError) as caught:
                    value._open(ROOT)
                self.assertTrue(caught.exception.stop_all)
                self.assertEqual(value._opener.open.call_count, 1)

    def test_robots_disallow_is_respected(self):
        value = fetcher.OfficialFetcher()
        value._bytes = Mock(return_value=b'User-agent: *\nDisallow: /\n')
        with self.assertRaises(fetcher.FetchError) as caught:
            value._allowed(ROOT)
        self.assertTrue(caught.exception.stop_all)

    def test_robots_crawl_delay_raises_minimum_interval(self):
        value = fetcher.OfficialFetcher()
        value._bytes = Mock(return_value=b'User-agent: *\nCrawl-delay: 8\nDisallow:\n')
        value._allowed(ROOT)
        self.assertEqual(value.interval, 8)

    def test_robots_disallowed_redirect_is_also_rejected(self):
        value = fetcher.OfficialFetcher()
        value._robots = RobotFileParser()
        value._robots.parse(['User-agent: *', 'Disallow: /restricted/'])
        with self.assertRaises(fetcher.FetchError):
            value._redirect_allowed(ROOT + 'restricted/file.pdf')

    def test_short_or_negative_interval_not_accepted(self):
        for interval in (-1, 0, 1, 2):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                fetcher.OfficialFetcher(interval=interval)


class ManualDailyTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(fetcher, '_today_jst', return_value=DAY)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def listed_client(self, robots=b'User-agent: *\nDisallow: /20\nCrawl-delay: 8\n'):
        value = fetcher.OfficialFetcher(daily_manual=True)
        value._bytes = Mock(side_effect=[robots, INDEX.encode('utf-8')])
        report = value.list_issues(DAY, DAY)
        value._bytes = Mock(return_value=VIEWER_HTML.encode('utf-8'))
        value._open = Mock(return_value=Response(PDF, len(PDF)))
        return value, report

    def test_manual_mode_records_robots_advisory_and_downloads_listed_issue(self):
        value, report = self.listed_client()
        self.assertEqual(report.restricted_urls, [])
        self.assertEqual(report.robots_excluded_urls, [VIEWER])
        self.assertIn('クロール除外指定', report.warnings[0])
        self.assertEqual(value.interval, 8)
        with tempfile.TemporaryDirectory() as folder:
            result = value.download(report.items[0], folder)
            self.assertEqual(result.path.read_bytes(), PDF)
        value._open.assert_called_once_with(PDF_URL)
        self.assertEqual(value.robots_excluded_urls, {VIEWER, PDF_URL})

    def test_manual_mode_rejects_other_dates_before_network(self):
        value = fetcher.OfficialFetcher(daily_manual=True)
        value._bytes = Mock()
        for start, end in ((DAY-timedelta(days=1), DAY), (DAY+timedelta(days=1), DAY+timedelta(days=1))):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                value.list_issues(start, end)
        value._bytes.assert_not_called()

    def test_manual_download_requires_discovery_in_same_session(self):
        value = fetcher.OfficialFetcher(daily_manual=True)
        value._bytes = Mock()
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(fetcher.FetchError) as caught:
                value.download(issue(), folder)
        self.assertTrue(caught.exception.stop_all)
        value._bytes.assert_not_called()

    def test_unadvertised_or_changed_issue_is_rejected_before_network(self):
        value, report = self.listed_client()
        original = report.items[0]
        unknown = fetcher.Issue(original.date, original.title, original.viewer_url.replace('t00052', 't00051'),
                                original.source_url, original.first_page, original.last_page)
        changed = fetcher.Issue(original.date, original.title, original.viewer_url,
                                original.source_url, 2, original.last_page)
        with tempfile.TemporaryDirectory() as folder:
            for item in (unknown, changed):
                with self.subTest(item=item), self.assertRaises(fetcher.FetchError):
                    value.download(item, folder)
        value._bytes.assert_not_called()
        value._open.assert_not_called()

    def test_same_url_is_not_retried_even_when_first_attempt_failed(self):
        value, report = self.listed_client()
        value._bytes.side_effect = fetcher.FetchError('通信失敗')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(fetcher.FetchError):
                value.download(report.items[0], folder)
            with self.assertRaisesRegex(fetcher.FetchError, '試行済み'):
                value.download(report.items[0], folder)
        self.assertEqual(value._bytes.call_count, 1)

    def test_date_change_invalidates_discovered_session(self):
        value, report = self.listed_client()
        with patch.object(fetcher, '_today_jst', return_value=DAY+timedelta(days=1)):
            with tempfile.TemporaryDirectory() as folder, self.assertRaises(fetcher.FetchError):
                value.download(report.items[0], folder)
        value._bytes.assert_not_called()
        value._open.assert_not_called()

    def test_date_change_between_download_requests_stops_before_http(self):
        value = fetcher.OfficialFetcher(daily_manual=True)
        value._listed_day = DAY
        value._opener = Mock()
        expected = fetcher.PDF_PATTERN.fullmatch('/20260925/20260925t00052/pdf/20260925t00052full00010002.pdf').groupdict()
        with value._expect_download_target('pdf', expected):
            with patch.object(fetcher, '_today_jst', return_value=DAY+timedelta(days=1)):
                with self.assertRaises(fetcher.FetchError) as caught:
                    value._open(PDF_URL)
        self.assertTrue(caught.exception.stop_all)
        value._opener.open.assert_not_called()

    def test_same_host_redirect_cannot_substitute_another_issue_or_html(self):
        value, report = self.listed_client()
        expected = fetcher.PDF_PATTERN.fullmatch('/20260925/20260925t00052/pdf/20260925t00052full00010002.pdf').groupdict()
        value._before_request = Mock()
        wrong_urls = (PDF_URL.replace('t00052', 't00051'), PDF_URL.replace('full00010002', 'full00030004'),
                      VIEWER, ROOT + 'maintenance.html')
        with value._expect_download_target('pdf', expected):
            for url in wrong_urls:
                with self.subTest(url=url), self.assertRaises(fetcher.FetchError) as caught:
                    fetcher._OfficialRedirect(value._redirect_allowed).redirect_request(
                        Request(PDF_URL), None, 302, '', {}, url)
                self.assertTrue(caught.exception.stop_all)
        value._before_request.assert_not_called()
        self.assertIsNone(value._expected_download)

    def test_final_response_url_is_checked_and_wrong_response_closed(self):
        value = fetcher.OfficialFetcher(daily_manual=True)
        value._before_request = Mock()
        response = Response(PDF)
        response.geturl = lambda: PDF_URL.replace('t00052', 't00051')
        value._opener = Mock()
        value._opener.open.return_value = response
        expected = fetcher.PDF_PATTERN.fullmatch('/20260925/20260925t00052/pdf/20260925t00052full00010002.pdf').groupdict()
        with value._expect_download_target('pdf', expected), self.assertRaises(fetcher.FetchError):
            value._open(PDF_URL)
        self.assertTrue(response.closed)

    def test_manual_mode_still_stops_http_403_and_429_without_retry(self):
        for code in (403, 429):
            with self.subTest(code=code):
                value = fetcher.OfficialFetcher(daily_manual=True)
                value._opener = Mock()
                value._opener.open.side_effect = HTTPError(ROOT, code, 'blocked', {}, io.BytesIO())
                with self.assertRaises(fetcher.FetchError) as caught:
                    value._open(ROOT)
                self.assertTrue(caught.exception.stop_all)
                self.assertEqual(value._opener.open.call_count, 1)


if __name__ == '__main__':
    unittest.main()
