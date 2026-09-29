import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfWriter
from core import Store, _download_date
from pipeline import process_document, process_documents


class PipelineFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / 'data')
        self.pdf = self.make_pdf('source.pdf', 3)

    def tearDown(self):
        self.store.db.close()
        self.temporary.cleanup()

    def make_pdf(self, name, pages):
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=595, height=842)
        path = self.root / name
        writer.write(path)
        return path

    def archive(self):
        return self.store.archive_pdf(self.pdf, 'https://example.invalid/source.pdf')

    @staticmethod
    def suggestion(start=2):
        return {'start_page': start, 'reason': '独立した公告見出しの候補', 'warnings': [], 'cancelled': False}


class ArchiveAndScopeTests(PipelineFixture):
    def test_archive_has_no_extraction_or_ocr_and_survives_reopening(self):
        before = self.pdf.read_bytes()
        with patch('core._extract_page', side_effect=AssertionError('archive must not extract')):
            document = self.archive()
        self.assertEqual(self.store.pages(), [])
        self.assertEqual(document['page_count'], 3)
        self.assertEqual(document['scope_status'], 'unconfirmed')
        self.assertEqual(document['processing_status'], 'archived')
        self.assertEqual(Path(document['path']).read_bytes(), before)
        self.store.db.close()
        self.store = Store(self.root / 'data')
        self.assertEqual(self.store.document(document['id'])['id'], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.pdf.read_bytes(), before)

    def test_duplicate_archive_preserves_original_name_scope_and_processing_state(self):
        document = self.archive()
        self.store.confirm_scope(document, [2])
        self.store.set_processing(document, 'needs_review', '人の判断が必要')
        copy = self.root / 'different-name.pdf'
        copy.write_bytes(self.pdf.read_bytes())
        saved = self.store.archive_pdf(copy)
        self.assertEqual(saved['original_name'], self.pdf.name)
        self.assertEqual(saved['scope_pages'], [2])
        self.assertEqual(saved['processing_status'], 'needs_review')
        self.assertEqual(saved['source_url'], document['source_url'])
        self.assertEqual(len(self.store.documents()), 1)

    def test_archive_rejects_changed_stored_original_instead_of_overwriting(self):
        document = self.archive()
        original = Path(document['path'])
        original.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.archive()
        self.assertEqual(original.read_bytes(), b'changed')

    def test_legacy_import_archives_before_first_ocr_callback(self):
        def ocr(path, page):
            self.assertEqual(len(self.store.documents()), 1)
            self.assertTrue((self.store.folder / 'pdf' / (self.store.documents()[0]['id'] + '.pdf')).is_file())
            raise RuntimeError('OCR unavailable')
        self.store.import_pdf(self.pdf, selected_pages=[2], ocr=ocr)
        self.assertEqual(self.store.pages()[0]['machine_status'], 'failed')

    def test_legacy_import_finishes_processing_instead_of_leaving_archive_only_state(self):
        self.store.import_pdf(self.pdf)
        document = self.store.documents()[0]
        self.assertEqual(document['processing_status'], 'needs_review')
        self.store.import_pdf(self.pdf, selected_pages=[2])
        self.store.retry_ocr(lambda path, page: '読み直し本文', notice_only=True)
        export = self.root / 'legacy-export.csv'
        self.store.export(export, notice_only=True)
        with export.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row['ページ'] for row in rows], ['2'])
        other = self.make_pdf('fully-read.pdf', 1)
        self.store.import_pdf(other, ocr=lambda path, page: '原文', selected_pages=[1])
        self.assertEqual(next(doc for doc in self.store.documents() if doc['original_name'] == other.name)['processing_status'], 'completed')

    def test_confirm_scope_accepts_pages_not_yet_processed_and_does_not_review(self):
        document = self.archive()
        self.store.confirm_scope(document, [3, 1])
        self.assertEqual(self.store.pages(), [])
        self.assertEqual(self.store.document(document)['scope_pages'], [1, 3])
        self.store.process_page(document, 3, ocr=lambda path, page: '原文')
        page = self.store.pages()[0]
        self.assertEqual(page['notice_scope'], 1)
        self.assertEqual(page['state'], '未確認')
        self.assertEqual(self.store.document(document)['scope_status'], 'confirmed')

    def test_invalid_scope_is_atomic_and_human_scope_cannot_be_replaced_by_machine(self):
        document = self.archive()
        self.store.confirm_scope(document, [2])
        for invalid in ([], [0], [4], [True], ['2'], '2', [2, 2], None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.confirm_scope(document, invalid)
            self.assertEqual(self.store.document(document)['scope_pages'], [2])
        with self.assertRaises(ValueError):
            self.store.set_provisional_scope(document, [1, 2, 3])

    def test_provisional_scope_cannot_be_marked_reviewed_until_human_confirmation(self):
        document = self.archive()
        self.store.set_provisional_scope(document, [2], '候補')
        self.store.process_page(document, 2, ocr=lambda path, page: '原文')
        with self.assertRaises(ValueError):
            self.store.review(document['id'], 2, '確認済み', 'まだ不可')
        self.store.review(document['id'], 2, '要確認', '範囲の確認待ち')
        self.store.confirm_scope(document, [2])
        self.assertEqual(self.store.pages()[0]['state'], '要確認')
        self.assertEqual(self.store.pages()[0]['note'], '範囲の確認待ち')
        self.store.review(document['id'], 2, '確認済み', '原文を確認')
        self.assertEqual(self.store.pages()[0]['state'], '確認済み')

    def test_excluded_archived_document_is_respected_without_pages(self):
        document = self.archive()
        self.store.exclude_document_from_notices(document['id'])
        callback = Mock()
        result = process_document(self.store, document, ocr=callback)
        self.assertEqual(result['status'], 'excluded')
        callback.assert_not_called()
        self.assertEqual(self.store.pages(), [])

    def test_document_without_pages_and_provisional_state_are_visible_in_export(self):
        document = self.archive()
        self.store.set_processing(document, 'needs_review', '開始位置を確認できません')
        export = self.root / 'pending.csv'
        self.store.export(export, notice_only=True)
        with export.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['ページ'], '')
        self.assertEqual(rows[0]['範囲確定状態'], 'unconfirmed')
        self.assertIn('開始位置', rows[0]['要確認理由'])
        self.store.set_provisional_scope(document, [2])
        self.store.process_page(document, 2, ocr=lambda path, page: '原文')
        self.store.export(export, notice_only=True)
        with export.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({row['ページ'] for row in rows}, {'', '2'})
        self.assertTrue(all(row['範囲確定状態'] == 'provisional' for row in rows))

    def test_historical_machine_review_status_does_not_leave_document_pending_forever(self):
        document = self.archive()
        self.store.confirm_scope(document, [1])
        self.store.process_page(document, 1, ocr=lambda path, page: '確認した原文')
        self.store.set_processing(document, 'needs_review', '機械の要確認判定')
        self.store.review(document['id'], 1, '確認済み', '全体を原文確認')
        export = self.root / 'reviewed.csv'
        self.store.export(export, notice_only=True)
        with export.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['ページ'], '1')
        self.assertEqual(rows[0]['確認状態'], '確認済み')


class PageProcessingTests(PipelineFixture):
    def test_failure_empty_and_not_processed_are_distinct_and_persist(self):
        document = self.archive()
        self.store.confirm_scope(document, [1, 2, 3])
        failed = self.store.process_page(document, 1, ocr=Mock(side_effect=RuntimeError('broken')))
        empty = self.store.process_page(document, 2, ocr=lambda path, page: '')
        pending = self.store.process_page(document, 3)
        self.assertEqual([failed['machine_status'], empty['machine_status'], pending['machine_status']], ['failed', 'empty', 'not_processed'])
        self.assertTrue(all(row['flags'] and row['state'] == '要確認' for row in self.store.pages()))

    def test_analysis_flags_and_coordinates_survive_database_and_export(self):
        document = self.archive()
        self.store.confirm_scope(document, [1])
        flag = {'reason': '方向間で不一致', 'bbox': {'x': .1, 'y': .2, 'width': .3, 'height': .05}, 'text': '会社名', 'kind': 'disagreement'}
        analysis = {'text': '株式会社例示', 'machine_status': 'succeeded', 'status': 'needs_review', 'flags': [flag], 'views': []}
        result = self.store.process_page(document, 1, analyze=lambda path, page, text: analysis)
        self.assertEqual(result['state'], '要確認')
        self.assertEqual(self.store.page_analysis(document, 1)['flags'], [flag])
        self.assertEqual(self.store.pages()[0]['flags'], [flag])
        self.assertEqual(self.store.pages()[0]['text'], '株式会社例示')

    def test_successful_resume_skips_ocr_and_force_preserves_notes_but_reopens_review(self):
        document = self.archive()
        self.store.confirm_scope(document, [1])
        ocr = Mock(return_value='初回原文')
        self.store.process_page(document, 1, ocr=ocr)
        self.store.review(document['id'], 1, '確認済み', '確認メモ')
        self.assertTrue(self.store.process_page(document, 1, ocr=ocr)['skipped'])
        self.assertEqual(ocr.call_count, 1)
        result = self.store.process_page(document, 1, ocr=lambda path, page: '新しい原文', force=True)
        self.assertFalse(result['skipped'])
        self.assertEqual(result['text'], '新しい原文')
        self.assertEqual(result['note'], '確認メモ')
        self.assertEqual(result['state'], '要確認')
        history = self.store.history(document['id'], 1)[0]['details']
        self.assertEqual(history['previous_state'], '確認済み')
        self.assertEqual(history['previous_ocr_text'], '初回原文')

    def test_failed_or_invalid_analysis_preserves_previous_ocr(self):
        document = self.archive()
        self.store.confirm_scope(document, [1])
        self.store.process_page(document, 1, ocr=lambda path, page: '保存済みOCR')
        for analysis in ({'text': '失敗で得た断片', 'machine_status': 'failed', 'flags': []},
                         {'text': '不正な結果', 'machine_status': 'succeeded', 'flags': ['bad']},
                         {'text': '不正な結果', 'machine_status': 'succeeded', 'flags': [], 'bad': float('nan')}):
            with self.subTest(analysis=analysis):
                result = self.store.process_page(document, 1, force=True, analyze=lambda path, page, text: analysis)
                self.assertEqual(result['machine_status'], 'failed')
                self.assertEqual(result['text'], '保存済みOCR')
                self.assertTrue(result['flags'])

    def test_analysis_roundtrip_and_validation_do_not_silently_create_pages(self):
        document = self.archive()
        with self.assertRaises(ValueError):
            self.store.save_page_analysis(document, 1, {'flags': []})
        self.store.process_page(document, 1)
        analysis = {'status': 'needs_review', 'machine_status': 'empty', 'flags': [{'reason': '確認', 'bbox': None}]}
        self.store.save_page_analysis(document, 1, analysis)
        self.assertEqual(self.store.page_analysis(document, 1), {**analysis, 'text': ''})
        with self.assertRaises(ValueError):
            self.store.save_page_analysis(document, 1, {'machine_status': 'confident', 'flags': []})
        self.assertEqual(self.store.page_analysis(document, 1), {**analysis, 'text': ''})

    def test_local_analysis_adds_searchable_text_and_invalidates_human_review(self):
        document = self.archive()
        self.store.confirm_scope(document, [1])
        self.store.process_page(document, 1, ocr=lambda path, page: '既存のページ本文')
        self.store.review(document['id'], 1, '確認済み', '大切なメモ')
        analysis = {'text': '局所で取得した社名', 'status': 'read', 'machine_status': 'succeeded', 'flags': []}
        self.store.save_page_analysis(document, 1, analysis)
        page = self.store.pages()[0]
        self.assertIn('既存のページ本文', page['text'])
        self.assertIn('局所で取得した社名', page['text'])
        self.assertEqual(page['state'], '要確認')
        self.assertEqual(page['note'], '大切なメモ')
        self.assertIsNone(page['reviewed_at'])
        self.assertEqual(self.store.history(document['id'], 1)[0]['details']['previous_state'], '確認済み')
        self.store.save_page_analysis(document, 1, analysis)
        self.assertEqual(self.store.pages()[0]['text'].count('局所で取得した社名'), 1)
        self.store.save_page_analysis(document, 1, {'text': '失敗時の断片', 'machine_status': 'failed', 'flags': []})
        self.assertEqual(self.store.pages()[0]['text'], page['text'])

    def test_resume_cannot_skip_integrity_check_of_original(self):
        document = self.archive()
        self.store.process_page(document, 1, ocr=lambda path, page: '保持本文')
        Path(document['path']).write_bytes(b'changed original')
        callback = Mock()
        page = self.store.process_page(document, 1, ocr=callback)
        self.assertFalse(page['skipped'])
        self.assertEqual(page['machine_status'], 'failed')
        self.assertEqual(page['text'], '保持本文')
        callback.assert_not_called()

    def test_successful_region_supplement_keeps_incomplete_page_retryable(self):
        for status in ('failed', 'empty', 'not_processed'):
            with self.subTest(status=status):
                source = self.make_pdf(status + '.pdf', {'failed': 1, 'empty': 2, 'not_processed': 4}[status])
                document = self.store.archive_pdf(source)
                self.store.confirm_scope(document, [1])
                first_ocr = (Mock(side_effect=RuntimeError('whole-page failure')) if status == 'failed'
                             else (lambda path, page: '') if status == 'empty' else None)
                self.store.process_page(document, 1, ocr=first_ocr)
                self.assertEqual(self.store.document(document)['scope_status'], 'confirmed')
                self.store.review(document['id'], 1, '要確認', '全頁を確認するメモ')
                self.store.save_page_analysis(document, 1, {'text': '局所で読めた株式会社例示',
                    'machine_status': 'succeeded', 'supplement_succeeded': True, 'flags': []})
                row = next(row for row in self.store.pages() if row['document_id'] == document['id'])
                self.assertEqual(row['machine_status'], status)
                self.assertIn('局所で読めた株式会社例示', row['text'])
                self.assertEqual(row['state'], '要確認')
                self.assertEqual(row['note'], '全頁を確認するメモ')
                self.assertIsNone(row['reviewed_at'])
                self.assertTrue(self.store.history(document['id'], 1)[0]['details']['supplement_succeeded'])
                self.assertEqual(self.store.page_analysis(document, 1)['machine_status'], status)
                export = self.root / (status + '.csv')
                self.store.replace_ledger([{'code': 'regional', 'name': '株式会社例示', 'address': ''}])
                self.store.export(export, notice_only=True)
                with export.open(encoding='utf-8-sig', newline='') as stream:
                    rows = list(csv.DictReader(stream))
                target = next(row for row in rows if row['PDF名'] == source.name and row['ページ'] == '1')
                self.assertEqual(target['候補取引先コード'], 'regional')
                self.assertEqual(target['機械処理状態'], status)
                retry = Mock(return_value='全体の読み直し本文')
                result = process_document(self.store, document, ocr=retry)
                self.assertEqual(result['processed'], 1)
                self.assertEqual(result['skipped'], 0)
                self.assertEqual(retry.call_count, 1)

    def test_failed_region_supplement_cannot_replace_saved_text(self):
        document = self.archive()
        self.store.process_page(document, 1, ocr=lambda path, page: '保存本文')
        self.store.save_page_analysis(document, 1, {'text': '失敗時の断片', 'machine_status': 'failed',
                                                   'supplement_succeeded': False, 'flags': []})
        self.assertEqual(self.store.pages()[0]['text'], '保存本文')

    def test_export_includes_approximate_matches_from_same_quality_matcher(self):
        document = self.archive()
        self.store.confirm_scope(document, [1])
        self.store.process_page(document, 1, ocr=lambda path, page: 'OCR誤認の本文')
        export = self.root / 'approximate.csv'
        with patch('ocr_review.match_with_review', return_value=[{'code': 'A', 'name': '候補会社', 'reason': '近似候補・原文確認', 'match_type': 'approximate'}]) as matcher:
            self.store.export(export, notice_only=True)
        self.assertEqual(matcher.call_count, 1)
        with export.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]['候補会社名'], '候補会社')
        self.assertEqual(rows[0]['候補理由'], '近似候補・原文確認')

    def test_export_refreshes_ledger_flags_without_rewriting_saved_analysis(self):
        document = self.archive()
        self.store.confirm_scope(document, [1])
        self.store.process_page(document, 1, ocr=lambda path, page: '原文')
        analysis = {'text': '原文', 'machine_status': 'succeeded', 'flags': [
            {'kind': 'approximate_name', 'reason': '古い台帳の近似候補', 'text': '古い社名', 'bbox': None},
            {'kind': 'ocr_failed', 'reason': '原文確認を残す', 'text': '', 'bbox': None}]}
        self.store.save_page_analysis(document, 1, analysis)
        self.store.replace_ledger([{'code': 'N', 'name': '新しい台帳の会社', 'address': ''}])
        export = self.root / 'new-ledger.csv'
        self.store.export(export, notice_only=True)
        with export.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertNotIn('古い台帳', rows[0]['要確認理由'])
        self.assertIn('原文確認を残す', rows[0]['要確認理由'])
        self.assertEqual(len(self.store.page_analysis(document, 1)['flags']), 2)


class PipelineTests(PipelineFixture):
    def test_new_document_uses_provisional_range_without_setting_human_review_done(self):
        document = self.archive()
        with patch('notice_scope.find_notice_start', return_value=self.suggestion()):
            result = process_document(self.store, document, ocr=lambda path, page: 'OCR原文')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['scope_status'], 'provisional')
        self.assertEqual([row['page'] for row in self.store.pages()], [2, 3])
        self.assertTrue(all(row['state'] == '未確認' for row in self.store.pages()))
        self.assertEqual(self.store.document(document)['scope_pages'], [2, 3])

    def test_no_heading_retains_original_and_document_review_queue(self):
        document = self.archive()
        with patch('notice_scope.find_notice_start', return_value=self.suggestion(None)):
            result = process_document(self.store, document)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(self.store.pages(), [])
        self.assertTrue(Path(document['path']).is_file())
        self.assertEqual(self.store.document(document)['scope_status'], 'unconfirmed')

    def test_confirmed_scope_is_not_replaced_even_when_force_is_requested(self):
        document = self.archive()
        self.store.confirm_scope(document, [1, 3])
        with patch('notice_scope.find_notice_start', side_effect=AssertionError('must not search')):
            result = process_document(self.store, document, ocr=lambda path, page: '原文', force=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([row['page'] for row in self.store.pages()], [1, 3])

    def test_cancellation_commits_finished_page_and_restart_skips_it(self):
        document = self.archive()
        self.store.confirm_scope(document, [1, 2, 3])
        stop = {'value': False}
        calls = []
        def ocr(path, page):
            calls.append(page)
            stop['value'] = True
            return '原文'
        result = process_document(self.store, document, ocr=ocr, cancelled=lambda: stop['value'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual([row['page'] for row in self.store.pages()], [1])
        self.store.db.close()
        self.store = Store(self.root / 'data')
        ocr = Mock(return_value='続き')
        result = process_document(self.store, document['id'], ocr=ocr)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['skipped'], 1)
        self.assertEqual([call.args[1] for call in ocr.call_args_list], [2, 3])

    def test_one_page_failure_does_not_stop_following_pages(self):
        document = self.archive()
        self.store.confirm_scope(document, [1, 2, 3])
        ocr = Mock(side_effect=['一頁', RuntimeError('page failed'), '三頁'])
        result = process_document(self.store, document, ocr=ocr)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['failed'], 1)
        self.assertEqual(result['processed'], 3)
        self.assertEqual(self.store.pages()[2]['text'], '三頁')

    def test_structured_quality_analysis_receives_base_text_and_ledger(self):
        document = self.archive()
        self.store.confirm_scope(document, [2])
        self.store.replace_ledger([{'code': 'A', 'name': '株式会社例示', 'address': ''}])
        callback = Mock()
        analyzer = Mock(return_value={'text': 'OCR本文', 'machine_status': 'succeeded', 'flags': [], 'status': 'read'})
        with patch('ocr_review.analyze_page', analyzer):
            result = process_document(self.store, document, layout_ocr=callback)
        self.assertEqual(result['status'], 'completed')
        self.assertIs(analyzer.call_args.args[2], callback)
        self.assertEqual(analyzer.call_args.kwargs['ledger'][0]['code'], 'A')
        self.assertIn('base_text', analyzer.call_args.kwargs)

    def test_skipped_pages_count_current_ledger_flags_without_changing_human_state(self):
        document = self.archive()
        self.store.confirm_scope(document, [1])
        analysis = {'text': '株式会社旧台帳の社名', 'machine_status': 'succeeded', 'flags': [
            {'kind': 'approximate_name', 'reason': '以前の台帳に似た社名', 'text': '株式会社旧台帳の社名', 'bbox': None}]}
        self.store.process_page(document, 1, analyze=lambda path, page, base: analysis)
        self.store.replace_ledger([{'code': 'NEW', 'name': '株式会社架空検証用星雲探検隊', 'address': ''}])
        self.store.review(document['id'], 1, '確認済み', '変更後の台帳を原文確認')
        before = self.store.pages()[0]
        callback = Mock(side_effect=AssertionError('skipped pages must not launch OCR'))
        result = process_document(self.store, document, ocr=callback)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['flagged'], 0)
        self.assertEqual(result['status'], 'completed')
        after = self.store.pages()[0]
        for key in ('state', 'reviewed_at', 'note', 'analysis_json', 'text', 'machine_status', 'flags'):
            self.assertEqual(after[key], before[key])
        callback.assert_not_called()

    def test_batch_continues_after_document_scope_error(self):
        first = self.archive()
        second = self.store.archive_pdf(self.make_pdf('second.pdf', 2))
        with patch('notice_scope.find_notice_start', side_effect=[RuntimeError('scope failure'), self.suggestion(1)]):
            results = process_documents(self.store, [first, second], ocr=lambda path, page: '原文')
        self.assertEqual([result['status'] for result in results], ['failed', 'completed'])
        self.assertEqual(self.store.document(first)['processing_status'], 'failed')
        self.assertTrue(Path(first['path']).is_file())


class DownloadDayTests(PipelineFixture):
    def test_daily_attempt_persists_even_after_failed_download_and_restart(self):
        self.assertIsNone(self.store.download_day('2026-09-29'))
        self.assertTrue(self.store.claim_download_day('2026-09-29'))
        self.store.finish_download_day('failed', '接続失敗', '2026-09-29')
        self.store.db.close()
        self.store = Store(self.root / 'data')
        self.assertFalse(self.store.claim_download_day('2026-09-29'))
        self.assertEqual(self.store.download_day('2026-09-29')['status'], 'failed')
        self.assertTrue(self.store.claim_download_day('2026-09-30'))
        self.assertEqual(self.store.download_day('2026-09-30')['status'], 'running')

    def test_claim_is_atomic_across_connections(self):
        barrier = threading.Barrier(2)
        folder = self.store.folder
        def claim():
            store = Store(folder)
            try:
                barrier.wait(timeout=5)
                return store.claim_download_day('2026-09-29')
            finally:
                store.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_default_day_is_jst_not_local_system_calendar(self):
        fixed = datetime(2026, 9, 29, 15, 1, tzinfo=timezone.utc)
        with patch('core.datetime') as clock:
            clock.now.side_effect = lambda tz: fixed.astimezone(tz)
            self.assertEqual(_download_date(), '2026-09-30')
            self.assertEqual(clock.now.call_args.args[0].utcoffset(None), timedelta(hours=9))

    def test_invalid_day_or_unclaimed_finish_is_rejected(self):
        for day in ('2026-9-29', 'invalid', '2026-02-30', True, 20260929):
            with self.subTest(day=day), self.assertRaises((ValueError, TypeError)):
                self.store.claim_download_day(day)
        with self.assertRaises(ValueError):
            self.store.finish_download_day('completed', day='2026-09-29')


class MigrationTests(PipelineFixture):
    def test_pre_pipeline_database_preserves_human_scope_and_reviews(self):
        legacy = self.root / 'legacy'
        legacy.mkdir()
        connection = sqlite3.connect(legacy / 'kanpo.sqlite3')
        connection.executescript('''
            CREATE TABLE documents(id TEXT PRIMARY KEY,original_name TEXT,imported_at TEXT,page_count INTEGER);
            CREATE TABLE pages(document_id TEXT,page INTEGER,text TEXT,issue TEXT,state TEXT,note TEXT,
                               reviewed_at TEXT,notice_scope INTEGER,PRIMARY KEY(document_id,page));
            INSERT INTO documents VALUES ('a','legacy.pdf','2026-09-28',3);
            INSERT INTO documents VALUES ('b','excluded.pdf','2026-09-28',1);
            INSERT INTO documents VALUES ('c','unknown.pdf','2026-09-28',1);
            INSERT INTO documents VALUES ('d','no-pages.pdf','2026-09-28',1);
            INSERT INTO pages VALUES ('a',2,'保存本文','文字抽出済み','確認済み','保持メモ','2026-09-28',1);
            INSERT INTO pages VALUES ('a',3,'対象外本文','文字抽出済み','未確認','','',-1);
            INSERT INTO pages VALUES ('b',1,'対象外','文字抽出済み','未確認','','',-1);
            INSERT INTO pages VALUES ('c',1,'未設定','文字抽出済み','未確認','','',0);
        ''')
        connection.close()
        store = Store(legacy)
        try:
            self.assertEqual(store.document('a')['scope_status'], 'confirmed')
            self.assertEqual(store.document('a')['scope_pages'], [2])
            self.assertEqual(store.document('b')['scope_status'], 'excluded')
            self.assertEqual(store.document('c')['scope_status'], 'unconfirmed')
            self.assertEqual(store.document('a')['processing_status'], 'needs_review')
            self.assertEqual(store.document('c')['processing_status'], 'needs_review')
            self.assertEqual(store.document('d')['processing_status'], 'archived')
            row = next(row for row in store.pages() if row['document_id'] == 'a' and row['page'] == 2)
            self.assertEqual((row['state'], row['note'], row['text']), ('確認済み', '保持メモ', '保存本文'))
        finally:
            store.db.close()
        store = Store(legacy)
        try:
            self.assertEqual(store.document('a')['scope_pages'], [2])
        finally:
            store.db.close()
