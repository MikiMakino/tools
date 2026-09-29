from datetime import datetime
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfWriter

import acquisition
from core import Store
from official_fetcher import DownloadResult, FetchCancelled, FetchError, Issue


DAY = '2026-09-29'
NOW = datetime(2026, 9, 29, 10, 15, tzinfo=acquisition.JST)


def issue(number=1, day=DAY, kind='h'):
    compact = day.replace('-', '')
    identifier = compact + kind + str(number).zfill(5)
    return Issue(day, '検証用本紙' + str(number),
                 f'https://www.kanpo.go.jp/{compact}/{identifier}/{identifier}full00010001f.html',
                 f'https://www.kanpo.go.jp/{compact}/{compact}.fullcontents.html', 1, 1)


def pdf_url(item):
    viewer = item.viewer_url
    prefix, name = viewer.rsplit('/', 1)
    return prefix + '/pdf/' + name.replace('f.html', '.pdf')


class FakeFetcher:
    max_items = 40

    def __init__(self, fixtures, events, failures=None, after_download=None):
        self.fixtures, self.events = fixtures, events
        self.failures, self.after_download = failures or {}, after_download
        self.calls = []

    def download(self, item, directory):
        index = len(self.calls)
        self.calls.append(item)
        self.events.append(('download', item.title))
        if index in self.failures:
            raise self.failures[index]
        path = Path(directory) / (str(index) + '.pdf')
        shutil.copyfile(self.fixtures[index], path)
        if self.after_download:
            self.after_download(index)
        return DownloadResult(path, hashlib.sha256(path.read_bytes()).hexdigest(),
                              pdf_url(item), item)


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / 'data')
        self.addCleanup(lambda: self.store.db.close())
        self.clock = patch.object(acquisition, '_jst_now', return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.events, self.fixtures = [], []
        for index in range(4):
            path = self.root / f'fixture{index}.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=100 + index, height=100)
            writer.write(path)
            self.fixtures.append(path)

    def fetcher(self, **kwargs):
        return FakeFetcher(self.fixtures, self.events, **kwargs)

    def processor(self, store, document, **kwargs):
        self.events.append(('process', document['id']))
        self.assertTrue(Path(document['path']).is_file())
        self.assertNotEqual(store.download_day(DAY)['status'], 'running')
        return {'status': 'completed', 'document': document, 'processed': 1, 'message': '完了'}

    def run_batch(self, fetcher, items=None, **kwargs):
        with patch.object(acquisition, '_process_document', side_effect=self.processor):
            return acquisition.run_daily_acquisition(self.store, fetcher,
                                                     items or [issue(1), issue(2)], **kwargs)

    def test_every_original_is_archived_before_processing_and_temporary_cleanup(self):
        archive = self.store.archive_pdf
        def record(path, **kwargs):
            self.events.append(('archive', Path(path).name))
            return archive(path, **kwargs)
        with patch.object(self.store, 'archive_pdf', side_effect=record):
            result = self.run_batch(self.fetcher())
        self.assertEqual([event[0] for event in self.events],
                         ['download', 'archive', 'download', 'archive', 'process', 'process'])
        self.assertEqual((result['status'], result['downloaded'], result['processed']), ('取得済み', 2, 2))
        self.assertIn('10:15', result['message'])
        self.assertIn('に取得を開始した選択分', result['message'])
        self.assertNotIn('開始時点の公開分', result['message'])
        self.assertTrue(result['checked_at'].endswith('+09:00'))
        self.assertEqual(self.store.download_day(DAY)['status'], 'completed')
        self.assertFalse(list((self.store.folder / 'download_staging').iterdir()))
        for document, original in zip(result['documents'], self.fixtures):
            self.assertEqual(Path(document['path']).read_bytes(), original.read_bytes())

    def test_same_day_claim_survives_store_restart(self):
        self.run_batch(self.fetcher())
        self.store.db.close()
        self.store = Store(self.root / 'data')
        fetcher = self.fetcher()
        result = self.run_batch(fetcher)
        self.assertFalse(result['attempted'])
        self.assertEqual(result['status'], '取得済み')
        self.assertEqual(fetcher.calls, [])

    def test_daily_claim_is_committed_before_first_network_operation(self):
        fetcher = self.fetcher()
        original_download = fetcher.download
        observed = []
        def download(item, directory):
            # A separate connection must see the claim before the transport starts.
            independent = Store(self.store.folder)
            try:
                attempt = independent.download_day(DAY)
                self.assertIsNotNone(attempt)
                self.assertEqual(attempt['status'], 'running')
                self.assertFalse(independent.claim_download_day(DAY))
                observed.append(item.viewer_url)
            finally:
                independent.db.close()
            return original_download(item, directory)
        fetcher.download = download
        result = self.run_batch(fetcher)
        self.assertEqual(len(observed), 2)
        self.assertTrue(result['attempted'])

    def test_failed_and_cancelled_attempts_survive_restart_with_zero_second_network_calls(self):
        for failure in (FetchError('接続できない'), FetchCancelled()):
            with self.subTest(failure=type(failure).__name__):
                folder = self.root / ('restart-' + type(failure).__name__)
                store = Store(folder)
                try:
                    fetcher = self.fetcher(failures={0: failure})
                    result = acquisition.run_daily_acquisition(store, fetcher, [issue(1)])
                    self.assertEqual(result['status'], '取得失敗')
                finally:
                    store.db.close()
                store = Store(folder)
                try:
                    no_transport = Mock()
                    no_transport.max_items = 40
                    again = acquisition.run_daily_acquisition(store, no_transport, [issue(1)])
                    self.assertFalse(again['attempted'])
                    no_transport.download.assert_not_called()
                finally:
                    store.db.close()

    def test_old_or_mismatched_issue_is_rejected_before_claim(self):
        stale = issue(1, '2026-09-28')
        forged = Issue(DAY, stale.title, stale.viewer_url, stale.source_url, 1, 1)
        for item in (stale, forged):
            with self.subTest(item=item):
                with self.assertRaises(ValueError):
                    self.run_batch(self.fetcher(), [item])
                self.assertIsNone(self.store.download_day(DAY))

    def test_empty_selection_does_not_claim_day(self):
        with self.assertRaises(ValueError):
            acquisition.run_daily_acquisition(self.store, self.fetcher(), [])
        self.assertIsNone(self.store.download_day(DAY))

    def test_non_stopping_download_failure_allows_later_originals(self):
        fetcher = self.fetcher(failures={1: FetchError('一時的な失敗')})
        result = self.run_batch(fetcher, [issue(1), issue(2), issue(3)])
        self.assertEqual(len(fetcher.calls), 3)
        self.assertEqual((result['status'], result['downloaded'], result['failed']), ('一部取得', 2, 1))
        self.assertEqual(self.store.download_day(DAY)['status'], 'partial')
        self.assertEqual([event[0] for event in self.events],
                         ['download', 'download', 'download', 'process', 'process'])

    def test_four_selected_issues_keep_partial_failures_and_process_all_successful_originals(self):
        items = [issue(1), issue(212, kind='g'), issue(214, kind='g'), issue(5, kind='t')]
        fetcher = self.fetcher(failures={1: FetchError('公開PDFの1件が失敗')})
        result = self.run_batch(fetcher, items)
        self.assertEqual(len(fetcher.calls), 4)
        self.assertEqual((result['total'], result['downloaded'], result['failed'], result['not_downloaded']), (4, 3, 1, 1))
        self.assertEqual(result['status'], '一部取得')
        self.assertEqual(len(result['processing_results']), 3)
        self.assertEqual(self.store.download_day(DAY)['status'], 'partial')
        for document, index in zip(result['documents'], (0, 2, 3)):
            self.assertEqual(Path(document['path']).read_bytes(), self.fixtures[index].read_bytes())

    def test_site_403_or_429_stops_remaining_four_issue_selection(self):
        for code in (403, 429):
            with self.subTest(code=code):
                store = Store(self.root / f'http-{code}')
                try:
                    fetcher = self.fetcher(failures={1: FetchError(f'HTTP {code}', stop_all=True)})
                    with patch.object(acquisition, '_process_document', return_value={'status': 'needs_review', 'processed': 0}):
                        result = acquisition.run_daily_acquisition(store, fetcher, [issue(i) for i in range(1, 5)])
                    self.assertEqual(len(fetcher.calls), 2)
                    self.assertEqual((result['downloaded'], result['failed'], result['not_downloaded']), (1, 1, 3))
                    self.assertEqual(store.download_day(DAY)['status'], 'partial')
                    self.assertTrue(Path(result['documents'][0]['path']).is_file())
                finally:
                    store.db.close()

    def test_site_restriction_stops_later_downloads_without_bypass(self):
        fetcher = self.fetcher(failures={1: FetchError('robots.txtで禁止', stop_all=True)})
        result = self.run_batch(fetcher, [issue(1), issue(2), issue(3)])
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual((result['downloaded'], result['not_downloaded']), (1, 2))
        self.assertEqual(result['status'], '一部取得')
        self.assertEqual(len(result['processing_results']), 1)
        again = self.run_batch(self.fetcher())
        self.assertFalse(again['attempted'])

    def test_cancel_after_receiving_preserves_original_before_stopping(self):
        cancel = [False]
        fetcher = self.fetcher(after_download=lambda index: cancel.__setitem__(0, True))
        result = self.run_batch(fetcher, cancelled=lambda: cancel[0])
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(result['status'], '一部取得')
        self.assertEqual(self.store.download_day(DAY)['status'], 'partial')
        self.assertTrue(Path(result['documents'][0]['path']).is_file())
        self.assertEqual(result['processing_results'], [])

    def test_cancel_after_only_pdf_completed_still_records_saved_original(self):
        cancel = [False]
        fetcher = self.fetcher(after_download=lambda index: cancel.__setitem__(0, True))
        result = self.run_batch(fetcher, [issue(1)], cancelled=lambda: cancel[0])
        self.assertEqual(result['status'], '取得済み')
        self.assertEqual(self.store.download_day(DAY)['status'], 'completed')
        self.assertEqual(Path(result['documents'][0]['path']).read_bytes(), self.fixtures[0].read_bytes())
        self.assertEqual(self.store.document(result['documents'][0]['id'])['processing_status'], 'archived')
        self.assertEqual(result['processing_results'], [])

    def test_fetch_cancel_without_original_still_consumes_daily_attempt(self):
        result = self.run_batch(self.fetcher(failures={0: FetchCancelled()}))
        self.assertEqual(result['status'], '取得失敗')
        self.assertEqual(self.store.download_day(DAY)['status'], 'cancelled')
        self.assertFalse(self.run_batch(self.fetcher())['attempted'])

    def test_cancel_before_start_does_not_claim_day(self):
        fetcher = self.fetcher()
        result = self.run_batch(fetcher, cancelled=lambda: True)
        self.assertEqual(result['status'], '未取得')
        self.assertIsNone(self.store.download_day(DAY))
        self.assertEqual(fetcher.calls, [])

    def test_processing_exception_does_not_change_download_status(self):
        with patch.object(acquisition, '_process_document', side_effect=[RuntimeError('OCRエラー'),
                           {'status': 'needs_review', 'message': '確認必要', 'processed': 0}]):
            result = acquisition.run_daily_acquisition(self.store, self.fetcher(), [issue(1), issue(2)])
        self.assertEqual(result['status'], '取得済み')
        self.assertEqual(self.store.download_day(DAY)['status'], 'completed')
        self.assertEqual([item['status'] for item in result['processing_results']], ['failed', 'needs_review'])
        self.assertTrue(all(Path(document['path']).is_file() for document in result['documents']))
        failed = self.store.document(result['documents'][0]['id'])
        self.assertEqual(failed['processing_status'], 'failed')
        self.assertIn('OCRエラー', failed['processing_detail'])

    def test_archive_failure_retains_received_pdf_for_manual_recovery(self):
        fetcher = self.fetcher()
        with patch.object(self.store, 'archive_pdf', side_effect=OSError('保存先が使用できません')):
            result = self.run_batch(fetcher)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(result['status'], '取得失敗')
        self.assertEqual(result['processing_results'], [])
        self.assertEqual(len(result['retained_paths']), 1)
        retained = Path(result['retained_paths'][0])
        self.assertEqual(retained.read_bytes(), self.fixtures[0].read_bytes())
        retained.resolve().relative_to((self.store.folder / 'download_staging').resolve())
        self.assertEqual(self.store.download_day(DAY)['status'], 'failed')

    def test_duplicate_public_link_is_downloaded_only_once(self):
        fetcher = self.fetcher()
        result = self.run_batch(fetcher, [issue(1), issue(1)])
        self.assertEqual((result['total'], result['downloaded']), (1, 1))
        self.assertEqual(len(fetcher.calls), 1)

    def test_saved_actual_pdf_source_is_reused_after_hash_check(self):
        saved = self.store.archive_pdf(self.fixtures[0], source_url=pdf_url(issue(1)))
        fetcher = self.fetcher()
        result = self.run_batch(fetcher, [issue(1)])
        self.assertEqual(fetcher.calls, [])
        self.assertEqual((result['downloaded'], result['reused']), (1, 1))
        self.assertEqual(result['documents'][0]['id'], saved['id'])
        self.assertEqual(result['status'], '取得済み')

    def test_changed_cached_source_is_not_reused_or_silently_redownloaded(self):
        saved = self.store.archive_pdf(self.fixtures[0], source_url=pdf_url(issue(1)))
        Path(saved['path']).write_bytes(b'changed synthetic fixture')
        fetcher = self.fetcher()
        result = self.run_batch(fetcher, [issue(1)])
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(result['status'], '取得失敗')
        self.assertEqual(result['processing_results'], [])

    def test_real_pipeline_keeps_machine_scope_provisional_after_download(self):
        with patch('notice_scope.find_notice_start', return_value={
                'start_page': 1, 'reason': '合成紙面の検証用候補', 'warnings': []}):
            result = acquisition.run_daily_acquisition(self.store, self.fetcher(), [issue(1)],
                                                       ocr=lambda path, page: '株式会社検証用会社')
        self.assertEqual(result['status'], '取得済み')
        self.assertEqual(result['processing_results'][0]['status'], 'completed')
        saved = self.store.document(result['documents'][0]['id'])
        self.assertEqual(saved['scope_status'], 'provisional')
        self.assertEqual(saved['processing_status'], 'completed')
        self.assertEqual(self.store.pages(notice_only=True)[0]['state'], '未確認')

    def test_scope_not_found_keeps_download_completed_and_document_pending_after_restart(self):
        with patch('notice_scope.find_notice_start', return_value={
                'start_page': None, 'reason': '見出しを確認できない', 'warnings': []}):
            result = acquisition.run_daily_acquisition(self.store, self.fetcher(), [issue(1)])
        document = result['documents'][0]
        self.assertEqual(result['status'], '取得済み')
        self.assertEqual(result['processing_results'][0]['status'], 'needs_review')
        self.assertEqual(self.store.pages(), [])
        self.store.db.close()
        self.store = Store(self.root / 'data')
        pending = self.store.document(document['id'])
        self.assertEqual(pending['scope_status'], 'unconfirmed')
        self.assertEqual(pending['processing_status'], 'needs_review')
        self.assertEqual(Path(pending['path']).read_bytes(), self.fixtures[0].read_bytes())
        self.assertEqual(self.store.download_day(DAY)['status'], 'completed')
        source = acquisition.find_saved_issue(self.store, issue(1))
        self.assertEqual(source['id'], document['id'])

    def test_processing_cancellation_after_all_four_archives_does_not_undo_downloads(self):
        with patch.object(acquisition, '_process_document', return_value={'status': 'cancelled', 'processed': 0}) as processor:
            result = acquisition.run_daily_acquisition(self.store, self.fetcher(), [issue(i) for i in range(1, 5)])
        self.assertEqual(result['status'], '取得済み')
        self.assertEqual(result['downloaded'], 4)
        self.assertEqual(processor.call_count, 1)
        self.assertTrue(all(Path(document['path']).is_file() for document in result['documents']))
        self.assertEqual(self.store.download_day(DAY)['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
