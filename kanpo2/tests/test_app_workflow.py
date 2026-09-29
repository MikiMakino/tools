"""Workflow tests use real storage and mocked OCR, without a desktop or network."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import App
from core import Store
from review_ui import normalized_box


class AppWorkflowTests(unittest.TestCase):
    def test_multiple_imports_archive_before_reading_and_do_not_ask_during_processing(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = []
            for index in (1, 2):
                writer = PdfWriter()
                writer.add_blank_page(width=500+index, height=700)
                path = root / f'example{index}.pdf'
                writer.write(path)
                paths.append(str(path))
            store = Store(root / 'data')
            jobs = []
            def read(path, page):
                self.assertEqual(len(store.documents()), 2, 'All originals must be retained before OCR starts')
                self.assertTrue(Path(path).is_file())
                if store.document(Path(path).stem)['original_name'] == 'example1.pdf':
                    raise RuntimeError('simulated OCR failure')
                return '株式会社架空検証'
            window = SimpleNamespace(guard=lambda: False, ensure_saved=lambda: True,
                ocr_enabled=SimpleNamespace(get=lambda: True),
                processing_callbacks=lambda enabled: (read, None),
                background=lambda title, job: jobs.append(job), select_notice_pages=Mock())
            try:
                with patch('app.filedialog.askopenfilenames', return_value=paths), \
                     patch('app.messagebox.showerror') as error, \
                     patch('app.messagebox.askyesno') as ask:
                    App.import_pdf(window)
                    with patch('notice_scope.find_notice_start', return_value={
                        'start_page': 1, 'reason': 'test candidate', 'warnings': []}):
                        result = jobs[0](store, lambda text: None, threading.Event())
                    self.assertFalse(error.called)
                    self.assertFalse(ask.called)
                self.assertFalse(window.select_notice_pages.called)
                self.assertEqual(len(store.documents()), 2)
                pages = store.pages(notice_only=True)
                self.assertEqual(len(pages), 2)
                self.assertEqual({p['machine_status'] for p in pages}, {'failed', 'succeeded'})
                self.assertTrue(all(p['state'] != '確認済み' for p in pages))
                self.assertIn('確認待ち一覧', result)
            finally:
                store.db.close()

    def test_unavailable_ocr_is_a_recordable_callback_failure(self):
        with patch('app.availability', return_value={'available': False, 'message': 'test unavailable'}):
            ocr, layout = App.processing_callbacks(True)
        for callback in (ocr, layout):
            with self.assertRaisesRegex(RuntimeError, 'test unavailable'):
                callback('file.pdf', 1)
        self.assertEqual(App.processing_callbacks(False), (None, None))

    def test_daily_attempt_stops_before_network_dialog(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            try:
                store.claim_download_day()
                report = Mock()
                window = SimpleNamespace(guard=lambda: False, ensure_saved=lambda: True,
                                         store=store, show_report=report)
                with patch('app.tk.Toplevel') as dialog:
                    App.fetch_dialog(window)
                self.assertFalse(dialog.called)
                self.assertIn('1日1回', report.call_args.args[1])
            finally:
                store.db.close()

    def test_preview_selection_clips_and_reverses_drag_without_moving_pdf_coordinates(self):
        self.assertEqual(normalized_box((90, 80), (10, 20), 100, 100), [.1, .2, .8, .6])
        self.assertEqual(normalized_box((-10, -20), (120, 200), 100, 100), [0, 0, 1, 1])
        self.assertIsNone(normalized_box((10, 10), (11, 11), 100, 100))
        self.assertIsNone(normalized_box((0, 0), (10, 10), 0, 100))

    def test_region_recovery_from_failed_page_saves_text_without_claiming_full_page_success(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            writer = PdfWriter()
            writer.add_blank_page(width=500, height=700)
            path = root / 'failed.pdf'
            writer.write(path)
            store = Store(root / 'data')
            try:
                document = store.archive_pdf(path)
                store.confirm_scope(document['id'], [1])
                store.process_page(document['id'], 1, ocr=Mock(side_effect=RuntimeError('test failure')))
                jobs = []
                window = SimpleNamespace(guard=lambda: False, ensure_saved=lambda: True,
                    folder=store.folder, background=lambda title, job, done: jobs.append(job))
                row = store.pages()[0]
                layout = {'views': [{'rotation': 0, 'region': [.1, .2, .3, .1],
                    'text': '株式会社架空回復', 'lines': [{'text': '株式会社架空回復',
                    'bbox': {'x': .1, 'y': .2, 'width': .3, 'height': .1}}]}]}
                App.reread_region(window, row, [.1, .2, .3, .1], (0,), Mock())
                with patch('app.ocr_pdf_layout', return_value=layout):
                    jobs[0](store, lambda text: None, threading.Event())
                saved = store.pages()[0]
                self.assertIn('株式会社架空回復', saved['text'])
                self.assertEqual(saved['machine_status'], 'failed')
                self.assertEqual(saved['state'], '要確認')
            finally:
                store.db.close()


if __name__ == '__main__':
    unittest.main()
