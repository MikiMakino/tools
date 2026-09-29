"""Daily acquisition UI contracts; no display, HTTP request or native OCR."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app import App
from core import Store
from official_fetcher import Issue


class Widget:
    """Only the widget methods used by the two acquisition dialogs."""
    created = []

    def __init__(self, *args, **kwargs):
        self.options = kwargs
        self.selected = []
        self.rows = {}
        self.destroyed = False
        self.created.append(self)

    def pack(self, **kwargs):
        pass

    def title(self, value):
        pass

    def geometry(self, value):
        pass

    def heading(self, *args, **kwargs):
        pass

    def column(self, *args, **kwargs):
        pass

    def insert(self, parent, position, iid, values):
        self.rows[iid] = values

    def selection_add(self, key):
        self.selected.append(key)

    def selection(self):
        return tuple(self.selected)

    def destroy(self):
        self.destroyed = True

    def invoke(self):
        return self.options['command']()


def example_issue():
    day = datetime.now(timezone(timedelta(hours=9))).date()
    stamp = day.strftime('%Y%m%d')
    return Issue(day.isoformat(), '検証用本紙',
                 f'https://www.kanpo.go.jp/{stamp}/{stamp}h09999/{stamp}h09999full00010001f.html',
                 f'https://www.kanpo.go.jp/{stamp}/{stamp}.fullcontents.html', 1, 1)


class DailyUITests(unittest.TestCase):
    def setUp(self):
        Widget.created = []
        for name in ('app.tk.Toplevel', 'app.ttk.Frame', 'app.ttk.Label',
                     'app.ttk.Button', 'app.ttk.Checkbutton', 'app.ttk.Treeview'):
            replacement = patch(name, Widget)
            replacement.start()
            self.addCleanup(replacement.stop)

    def button(self, text):
        return next(w for w in Widget.created if w.options.get('text') == text)

    def test_discovery_explicitly_enables_daily_manual_mode(self):
        window = SimpleNamespace(guard=lambda: False, ensure_saved=lambda: True,
            store=SimpleNamespace(download_day=lambda: None), cancel_event=threading.Event(),
            background=Mock(), choose_issues=Mock())
        with patch('app.tk.BooleanVar', return_value=SimpleNamespace(get=lambda: True)), \
             patch('official_fetcher.OfficialFetcher') as factory:
            App.fetch_dialog(window)
            self.button('公開一覧を確認').invoke()
        self.assertIs(factory.call_args.kwargs['daily_manual'], True)
        self.assertIn('cancelled', factory.call_args.kwargs)
        self.assertEqual(window.background.call_count, 1)

    def test_robots_advisory_is_selectable_and_starts_one_background_batch(self):
        issue = example_issue()
        report = SimpleNamespace(items=[issue], warnings=['robots.txt 除外の案内'],
                                 restricted_urls=[], robots_excluded_urls=[issue.viewer_url])
        window = SimpleNamespace(guard=lambda: False, ensure_saved=lambda: True,
            store=SimpleNamespace(documents=lambda: []), background=Mock(),
            ocr_enabled=SimpleNamespace(get=lambda: True), processing_callbacks=Mock(return_value=('ocr', 'layout')))
        fetcher = object()
        with patch('app.messagebox.showinfo') as info, \
             patch('app.messagebox.askyesno') as ask, \
             patch('app.messagebox.askyesnocancel') as ask_cancel:
            App.choose_issues(window, fetcher, report)
            listing = next(w for w in Widget.created if w.rows)
            self.assertEqual(listing.selection(), ('0',))
            begin = self.button('選択した号を取得・保存して読取開始')
            self.assertNotEqual(begin.options.get('state'), 'disabled')
            begin.invoke()
            self.assertEqual(window.background.call_count, 1)
            task = window.background.call_args.args[1]
            store, progress, cancel = object(), Mock(), threading.Event()
            with patch('acquisition.run_daily_acquisition', return_value={'status': '取得済み'}) as acquire:
                result = task(store, progress, cancel)
            self.assertEqual(result['status'], '取得済み')
            self.assertEqual(acquire.call_args.args, (store, fetcher, [issue]))
            self.assertEqual(acquire.call_args.kwargs['ocr'], 'ocr')
            self.assertEqual(acquire.call_args.kwargs['layout_ocr'], 'layout')
            self.assertFalse(info.called or ask.called or ask_cancel.called)
        self.assertTrue(any('robots' in str(w.options.get('text', '')) for w in Widget.created))

    def test_actual_restriction_remains_disabled(self):
        issue = example_issue()
        report = SimpleNamespace(items=[issue], warnings=[],
            restricted_urls=[issue.viewer_url], robots_excluded_urls=[issue.viewer_url])
        window = SimpleNamespace(store=SimpleNamespace(documents=lambda: []))
        App.choose_issues(window, object(), report)
        listing = next(w for w in Widget.created if w.rows)
        self.assertEqual(listing.selection(), ())
        self.assertEqual(self.button('選択した号を取得・保存して読取開始').options['state'], 'disabled')

    def test_persisted_daily_attempt_blocks_before_fetcher_or_dialog(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Store(Path(folder))
            first.claim_download_day()
            first.finish_download_day('failed', '検証用の中断')
            first.db.close()
            reopened = Store(Path(folder))
            try:
                report = Mock()
                window = SimpleNamespace(guard=lambda: False, ensure_saved=lambda: True,
                    store=reopened, show_report=report)
                with patch('official_fetcher.OfficialFetcher') as factory:
                    App.fetch_dialog(window)
                factory.assert_not_called()
                self.assertEqual(Widget.created, [])
                self.assertIn('1日1回', report.call_args.args[1])
            finally:
                reopened.db.close()


if __name__ == '__main__':
    unittest.main()
