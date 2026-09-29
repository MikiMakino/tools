"""官報確認: Windows desktop UI. Private PDF and ledger data stays local."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from core import Store, read_ledger, match_candidates
from runtime import DataLock, data_folder
from windows_ocr import availability, ocr_pdf_page, ocr_pdf_layout, render_pdf_page
from notice_scope import parse_page_ranges, format_page_ranges, find_notice_start
from pdf_access import ensure_readable
from pipeline import process_document
from ocr_review import match_with_review, update_ledger_flags


SCOPE_LABELS = {'unconfirmed': '範囲未判定', 'provisional': '範囲候補・未確定',
                'confirmed': '範囲確認済み', 'excluded': '公告対象なし'}
PROCESS_LABELS = {'archived': '保存済み・処理待ち', 'processing': '処理中', 'completed': '処理済み',
                  'needs_review': '要確認', 'cancelled': '中断', 'failed': '処理失敗'}


def result_message(result):
    return result.get('message', str(result)) if isinstance(result, dict) else str(result)


def ledger_signature(ledger):
    return hashlib.sha256(json.dumps(ledger, ensure_ascii=False, sort_keys=True).encode('utf-8')).digest()


def matching_key(row, ledger_key):
    content = (row['text'] + '\0' + row.get('analysis_json', '{}')).encode('utf-8')
    return row['document_id'], row['page'], ledger_key, hashlib.sha256(content).digest()


class App(tk.Tk):
    def __init__(self, folder=None):
        super().__init__()
        self.withdraw()
        self.folder = Path(folder) if folder else data_folder()
        try:
            self.lock = DataLock(self.folder)
            self.store = Store(self.folder)
        except Exception:
            if hasattr(self, 'lock'):
                self.lock.close()
            self.destroy()
            raise
        self.title('官報公告確認 0.7 — 処理後の確認待ち一覧')
        self.geometry('1240x830')
        self.minsize(1000, 680)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.busy = False
        self.current = None
        self.rows = {}
        self.match_cache = {}
        self.cancel_event = threading.Event()
        self.events = queue.Queue()
        self._selecting = False
        self.ocr_enabled = tk.BooleanVar(value=True)
        toolbar = ttk.Frame(self, padding=(12, 10))
        toolbar.pack(fill='x')
        for label, action in [('当日分を取得（1日1回）', self.fetch_dialog), ('保存PDFを取り込む', self.import_pdf),
                              ('取引先台帳を読み込む', self.import_ledger), ('保存PDFの処理を再開', self.resume_processing),
                              ('再照合', self.rematch)]:
            ttk.Button(toolbar, text=label, command=action).pack(side='left', padx=3)
        ttk.Checkbutton(toolbar, text='画像の本文をOCR', variable=self.ocr_enabled).pack(side='left', padx=12)
        utilities = ttk.Frame(self, padding=(12, 0))
        utilities.pack(fill='x')
        for label, action in [('公告範囲を確認', self.edit_notice_scope), ('保存先を開く', self.open_storage),
                              ('CSV出力', self.export), ('全データのバックアップ', self.backup)]:
            ttk.Button(utilities, text=label, command=action).pack(side='left', padx=3)
        self.download_status = tk.StringVar()
        ttk.Label(utilities, textvariable=self.download_status).pack(side='right', padx=6)
        ttk.Label(self, text='実行後は席を離れて構いません。終了後、この一覧から公告範囲と原文を確認します。印がない箇所も正確に読めた保証はありません。',
                  padding=(16, 4)).pack(fill='x')
        filters = ttk.Frame(self, padding=(12, 6))
        filters.pack(fill='x')
        ttk.Label(filters, text='検索').pack(side='left')
        self.query = tk.StringVar()
        entry = ttk.Entry(filters, textvariable=self.query, width=32)
        entry.pack(side='left', padx=6)
        entry.bind('<Return>', lambda event: self.apply_filter())
        self.filter_state = tk.StringVar(value='確認待ち')
        ttk.Combobox(filters, textvariable=self.filter_state, state='readonly', width=12,
                     values=['確認待ち', 'すべて', '未確認', '要確認', '確認済み']).pack(side='left')
        self.candidates_only = tk.BooleanVar()
        ttk.Checkbutton(filters, text='候補ありのみ', variable=self.candidates_only).pack(side='left', padx=6)
        ttk.Button(filters, text='絞り込み', command=self.apply_filter).pack(side='left', padx=4)
        ttk.Button(filters, text='官報サイト', command=lambda: webbrowser.open('https://www.kanpo.go.jp/')).pack(side='right')
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, padding=(16, 6)).pack(fill='x')
        panels = ttk.Panedwindow(self, orient='vertical')
        panels.pack(fill='both', expand=True, padx=12)
        top = ttk.Frame(panels)
        self.tree = ttk.Treeview(top, columns=('file', 'page', 'match', 'issue', 'state'), show='headings',
                                 height=12, selectmode='browse')
        for key, label, width in [('file', 'PDF名', 250), ('page', 'ページ', 55), ('match', '候補数', 60),
                                  ('issue', '処理結果・確認が必要な理由', 410), ('state', '人の確認', 90)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=40)
        scroll = ttk.Scrollbar(top, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select)
        panels.add(top, weight=1)
        lower = ttk.Frame(panels)
        self.detail = tk.Text(lower, wrap='word', font=('Yu Gothic UI', 10), state='disabled')
        scroll = ttk.Scrollbar(lower, command=self.detail.yview)
        self.detail.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.detail.pack(fill='both', expand=True)
        panels.add(lower, weight=1)
        controls = ttk.Frame(self, padding=12)
        controls.pack(fill='x')
        for label, action in [('原文PDF', self.open_pdf), ('印を原文と確認', self.preview_page),
                              ('OCR再実行', self.retry_ocr), ('履歴', self.show_history)]:
            ttk.Button(controls, text=label, command=action).pack(side='left', padx=2)
        self.state = tk.StringVar(value='未確認')
        self.state_box = ttk.Combobox(controls, textvariable=self.state, values=['未確認', '要確認', '確認済み'],
                                     state='readonly', width=9)
        self.state_box.pack(side='left', padx=8)
        ttk.Label(controls, text='メモ').pack(side='left')
        self.note = ttk.Entry(controls)
        self.note.pack(side='left', fill='x', expand=True, padx=8)
        ttk.Button(controls, text='確認結果を保存', command=self.save).pack(side='left')
        progress = ttk.Frame(self, padding=(12, 0, 12, 8))
        progress.pack(fill='x')
        self.progress = ttk.Progressbar(progress, mode='indeterminate')
        self.progress.pack(side='left', fill='x', expand=True)
        self.cancel_button = ttk.Button(progress, text='中止', command=self.cancel, state='disabled')
        self.cancel_button.pack(side='left', padx=6)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.background('保存データと台帳を読み込んでいます', lambda store, report, cancel: '', lambda result: None)
        self.deiconify()

    def guard(self):
        if self.busy:
            messagebox.showinfo('処理中', '完了を待つか「中止」を押してください。処理中の読取・通信が終わると停止します。', parent=self)
        return self.busy

    def ensure_saved(self):
        if not self.current or self.current.get('kind') == 'document':
            return True
        if (self.state.get(), self.note.get()) == (self.current['state'], self.current['note']):
            return True
        if self.busy:
            messagebox.showinfo('未保存の変更', '処理完了後に確認結果を保存してください。', parent=self)
            return False
        choice = messagebox.askyesnocancel('未保存の確認結果', '確認状態とメモの変更を保存しますか？', parent=self)
        if choice is None:
            return False
        return self.save(refresh=False) if choice else True

    def detail_text(self, text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')

    def refresh(self, keep=None):
        keep = keep or (self.current and self.current['key'])
        self.current = None
        self.tree.delete(*self.tree.get_children())
        ledger = self.store.ledger()
        ledger_key = ledger_signature(ledger)
        self.rows = {}
        needle = self.query.get().strip().casefold()
        documents = {d['id']: d for d in self.store.documents()}
        selected_state = self.filter_state.get()
        pending_documents = []
        for document in documents.values():
            if (document.get('scope_status') in ('unconfirmed', 'provisional')
                    or (document.get('scope_status') != 'excluded' and
                        (document.get('processing_status') in ('archived', 'failed', 'cancelled', 'processing')
                         or document['notice_pages'] < len(document.get('scope_pages', []))))):
                pending_documents.append(document)
                if selected_state == '確認済み':
                    continue
                if needle and needle not in (document['original_name'] + document.get('processing_detail', '')).casefold():
                    continue
                key = 'document:' + document['id']
                row = {**document, 'key': key, 'kind': 'document', 'document_id': document['id'],
                       'page': 0, 'state': '要確認', 'note': '', 'matches': []}
                self.rows[key] = row
                reason = SCOPE_LABELS.get(document.get('scope_status'), '範囲未判定') + ' / ' + PROCESS_LABELS.get(document.get('processing_status'), '処理状況未確認')
                self.tree.insert('', 'end', iid=key, values=(document['original_name'], '全体', '—', reason, '要確認'))
        all_rows = self.store.pages(notice_only=True)
        for source in all_rows:
            row = dict(source)
            analysis = self.store.page_analysis(row['document_id'], row['page'])
            cache_key = matching_key(row, ledger_key)
            matches = self.match_cache.get(cache_key)
            if matches is None:
                matches = match_with_review(row['text'], ledger, analysis=analysis)
                self.match_cache[cache_key] = matches
            analysis = update_ledger_flags(row['text'], analysis, ledger, matches=matches)
            scope = documents[row['document_id']].get('scope_status', 'unconfirmed')
            key = f"{row['document_id']}:{row['page']}"
            row.update(key=key, kind='page', matches=matches, analysis=analysis, scope_status=scope)
            if selected_state == '確認待ち' and row['state'] == '確認済み':
                continue
            if selected_state not in ('すべて', '確認待ち') and row['state'] != selected_state:
                continue
            if self.candidates_only.get() and not matches:
                continue
            if needle and needle not in (row['original_name'] + row['text'] + row['note'] +
                                         ''.join(m['code'] + m['name'] for m in matches)).casefold():
                continue
            self.rows[key] = row
            flags = analysis.get('flags', [])
            reason = ' / '.join(flag.get('reason', '') for flag in flags[:2]) or row['issue']
            if scope != 'confirmed':
                reason = '公告範囲は暫定 / ' + reason
            self.tree.insert('', 'end', iid=key, values=(row['original_name'], row['page'], len(matches), reason, row['state']))
        remaining = sum(row['state'] != '確認済み' for row in all_rows)
        self.status.set(f'取引先 {len(ledger)}件 ／ 公告対象（暫定含む）{len(all_rows)}ページ ／ 人の確認待ち {remaining}ページ ／ 範囲・処理の確認待ち {len(pending_documents)}PDF')
        attempt = self.store.download_day()
        day_label = {'running': '取得中または前回中断', 'completed': '取得済み', 'partial': '一部取得',
                     'failed': '取得失敗', 'cancelled': '取得中止'}
        self.download_status.set('本日の取得：' + (day_label.get(attempt['status'], attempt['status']) + '（再取得は手動）' if attempt else '未実行'))
        if keep in self.rows:
            self.tree.selection_set(keep)
            self.tree.see(keep)
            self.select()
        else:
            self.state.set('未確認')
            self.state_box.configure(state='readonly')
            self.note.configure(state='normal')
            self.note.delete(0, 'end')
            self.detail_text('「保存PDFを取り込む」から複数のPDFを選ぶと、保存・公告範囲の探索・読取を順に進めます。\n処理中に確認の操作は不要です。終了後、「全体」の行から公告範囲を確認し、各ページの原文を確認してください。\n範囲不明・処理失敗も一覧に残ります。確認済みは人が原文を確認したうえで保存します。')

    def apply_filter(self):
        if not self.guard() and self.ensure_saved():
            self.refresh()

    def select(self, event=None):
        if self._selecting:
            return
        selected = self.tree.selection()
        if not selected or selected[0] not in self.rows:
            return
        if self.current and self.current['key'] == selected[0]:
            return
        if not self.ensure_saved():
            self._selecting = True
            self.tree.selection_set(self.current['key'])
            self._selecting = False
            return
        row = self.current = self.rows[selected[0]]
        self.note.configure(state='normal')
        self.state_box.configure(state='readonly')
        if row.get('kind') == 'document':
            self.detail_text(f"原文：{row['original_name']}\n保存先：{self.folder / 'pdf' / (row['document_id'] + '.pdf')}\n"
                             + SCOPE_LABELS.get(row.get('scope_status'), '範囲未判定') + '\n'
                             + row.get('scope_detail', '') + '\n' + row.get('processing_detail', '')
                             + '\n\n「公告範囲を確認」で原文と対象ページを確認します。\n中断・失敗の文書は「保存PDFの処理を再開」で、ダウンロードせず再処理できます。')
            self.state.set('要確認')
            self.note.delete(0, 'end')
            self.note.configure(state='disabled')
            self.state_box.configure(state='disabled')
            return
        lines = [f"原文：{row['original_name']} — PDFの{row['page']}ページ目", f"抽出状況：{row['issue']}"]
        lines += ['公告範囲：' + SCOPE_LABELS.get(row['scope_status'], '範囲未判定'), '', '【機械が確認を求める箇所】']
        flags = row['analysis'].get('flags', [])
        lines += [f"{index}. {flag.get('reason', '')}" + ('（ページ全体・位置不明）' if not flag.get('bbox') else '（原文に印を表示）')
                  for index, flag in enumerate(flags, 1)]
        if not flags:
            lines.append('特定の印はありません。誤読や完全な読み落としがないことを保証するものではありません。')
        if row.get('source_url'):
            lines.append('出典：官報発行サイト ' + row['source_url'])
        lines += ['', '【公告対象ページ内の取引先候補】', '同じページに別の記事がある場合、候補が公告内に属するか原文で確認してください。']
        lines += [f"{m['code']} / {m['name']} / {m['address']}\n  {m['reason']}" for m in row['matches']]
        if not row['matches']:
            lines.append('候補なし。文字抽出漏れ・旧社名などの可能性があるため、原文確認が必要です。')
        lines += ['', '【抽出テキスト／OCR結果（原文から加工した参考情報）】', row['text'] or '文字なし。原文PDFを確認してください。']
        self.detail_text('\n'.join(lines))
        self.state.set(row['state'])
        self.note.delete(0, 'end')
        self.note.insert(0, row['note'])

    @staticmethod
    def ocr_callback(enabled):
        if not enabled:
            return None
        result = availability()
        if not result.get('available'):
            raise RuntimeError('OCRを利用できません。' + result.get('message', '') +
                               '\nOCRを外して取り込む場合は、画面のチェックを外して再実行してください。')
        return ocr_pdf_page

    @staticmethod
    def processing_callbacks(enabled):
        """Keep failures as batch results, instead of requiring an operator mid-run."""
        if not enabled:
            return None, None
        try:
            App.ocr_callback(True)
            return ocr_pdf_page, ocr_pdf_layout
        except Exception as error:
            message = str(error)
            def unavailable(*args, **kwargs):
                raise RuntimeError(message)
            return unavailable, unavailable

    def background(self, label, job, done=None):
        self.busy = True
        self.note.configure(state='disabled')
        self.state_box.configure(state='disabled')
        self.tree.configure(selectmode='none')
        self.cancel_event.clear()
        self.progress.start(12)
        self.cancel_button.configure(state='normal')
        self.status.set(label)
        previous_matches = self.match_cache
        def work():
            store = Store(self.folder)
            try:
                result = job(store, self.events.put, self.cancel_event)
                ledger = store.ledger()
                ledger_key = ledger_signature(ledger)
                matches = {}
                pages = store.pages(notice_only=True)
                for index, row in enumerate(pages, 1):
                    key = matching_key(row, ledger_key)
                    if key in previous_matches:
                        matches[key] = previous_matches[key]
                    else:
                        self.events.put(f'保存済み本文と台帳を照合中 {index}/{len(pages)}ページ')
                        matches[key] = match_with_review(row['text'], ledger,
                            analysis=store.page_analysis(row['document_id'], row['page']))
                return result, matches
            finally:
                store.db.close()
        future = self.pool.submit(work)
        def poll():
            while not self.events.empty():
                self.status.set(self.events.get_nowait())
            if not future.done():
                self.after(150, poll)
                return
            self.busy = False
            self.note.configure(state='normal')
            self.state_box.configure(state='readonly')
            self.tree.configure(selectmode='browse')
            self.progress.stop()
            self.cancel_button.configure(state='disabled')
            try:
                result, self.match_cache = future.result()
                self.refresh()
                if done:
                    done(result)
                else:
                    self.show_report(label, result_message(result))
            except Exception as error:
                self.refresh()
                messagebox.showerror(label, str(error), parent=self)
        self.after(150, poll)

    def cancel(self):
        self.cancel_event.set()
        self.status.set('中止を受け付けました。実行中の読取・通信が終わると停止します。保存したPDFと完了済みの処理結果は残ります。')

    def show_report(self, title, text):
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.geometry('800x450')
        body = tk.Text(dialog, wrap='word', padx=12, pady=12)
        body.pack(fill='both', expand=True)
        body.insert('1.0', text)
        body.configure(state='disabled')
        ttk.Button(dialog, text='閉じる', command=dialog.destroy).pack(pady=8)

    def import_pdf(self):
        if self.guard() or not self.ensure_saved():
            return
        paths = filedialog.askopenfilenames(parent=self, filetypes=[('PDF', '*.pdf')])
        if not paths:
            return
        enabled = self.ocr_enabled.get()
        def job(store, report, cancel):
            messages, documents = [], []
            for index, path in enumerate(paths, 1):
                if cancel.is_set():
                    messages.append('中止しました。残りのPDFは未保存です。')
                    break
                report(f'原文PDFを保存 {index}/{len(paths)}：{Path(path).name}')
                try:
                    documents.append(store.archive_pdf(path))
                except Exception as error:
                    messages.append(Path(path).name + '：保存失敗 — ' + str(error))
            ocr, layout = self.processing_callbacks(enabled)
            for document in documents:
                if cancel.is_set():
                    messages.append('読取を中止しました。保存したPDFは「保存PDFの処理を再開」で続行できます。')
                    break
                try:
                    result = process_document(store, document, ocr=ocr, layout_ocr=layout,
                                              cancelled=cancel.is_set, progress=report)
                    messages.append(document['original_name'] + '：' + result_message(result))
                except Exception as error:
                    store.set_processing(document['id'], 'failed', str(error))
                    messages.append(document['original_name'] + '：処理失敗 — ' + str(error))
            messages.append('処理を終えました。確認待ち一覧から、公告範囲・要確認の印と原文を確認してください。')
            return '\n'.join(messages)
        self.background('PDF取り込み', job)

    def resume_processing(self):
        if self.guard() or not self.ensure_saved():
            return
        documents = self.store.documents()
        if self.current:
            documents = [d for d in documents if d['id'] == self.current['document_id']]
        if not documents:
            messagebox.showinfo('保存PDFの処理', '保存したPDFがありません。', parent=self)
            return
        enabled = self.ocr_enabled.get()
        def job(store, report, cancel):
            ocr, layout = self.processing_callbacks(enabled)
            messages = []
            for document in documents:
                if cancel.is_set():
                    messages.append('中止しました。保存済みの結果から再開できます。')
                    break
                try:
                    messages.append(document['original_name'] + '：' + result_message(process_document(
                        store, document, ocr=ocr, layout_ocr=layout, cancelled=cancel.is_set, progress=report)))
                except Exception as error:
                    store.set_processing(document['id'], 'failed', str(error))
                    messages.append(document['original_name'] + '：処理失敗 — ' + str(error))
            return '\n'.join(messages)
        self.background('保存PDFの処理を再開', job)

    def rematch(self):
        if self.guard() or not self.ensure_saved():
            return
        def job(store, report, cancel):
            ledger = store.ledger()
            count, candidates = 0, 0
            for row in store.pages(notice_only=True):
                if cancel.is_set():
                    break
                candidates += len(match_with_review(row['text'], ledger,
                    analysis=store.page_analysis(row['document_id'], row['page'])))
                count += 1
                report(f'保存済み本文を再照合：{count}ページ')
            return f'{count}ページを現在の台帳と照合しました。候補延べ{candidates}件。\nPDFの再ダウンロードは行いません。近似する社名も確認候補であり、同一法人の確定ではありません。'
        self.background('台帳との再照合', job)

    def select_notice_pages(self, path, total, current=None):
        """Require an explicit scope; never infer all pages from an empty value."""
        if not total:
            raise ValueError('PDFにページがありません。')
        dialog = tk.Toplevel(self)
        dialog.title('公告ページの指定')
        dialog.geometry('770x630')
        dialog.minsize(730, 560)
        dialog.transient(self)
        panel = ttk.Frame(dialog, padding=16)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text=f'{Path(path).name}\nPDF内の総ページ数: {total}', wraplength=630).pack(anchor='w')
        ttk.Label(panel, text='官庁・裁判所・特殊法人等・地方公共団体・会社その他・政府調達の公告を対象にします。\n原文で公告の範囲を確認してください。法令・告示などは対象から外します。', wraplength=630).pack(anchor='w', pady=10)
        full = tk.BooleanVar(value=False)
        entry_value = tk.StringVar(value=format_page_ranges(current or []))
        ttk.Label(panel, text='PDF内のページ番号（印刷された紙面の番号とは異なる場合があります）').pack(anchor='w')
        entry = ttk.Entry(panel, textvariable=entry_value, width=45)
        entry.pack(anchor='w', pady=5)
        ttk.Label(panel, text='例: 9-12,15　　同一ページに別の記事がある場合、本文の切り分けは原文で確認します。', wraplength=630).pack(anchor='w')
        full_button = ttk.Checkbutton(panel, text='このPDFは公告だけなので、全ページを対象にする', variable=full,
                                     command=lambda: entry.configure(state='disabled' if full.get() else 'normal'))
        full_button.pack(anchor='w', pady=10)
        search_status = tk.StringVar(value='号ごとに公告の開始位置を調べます。ページ番号は固定しません。')
        ttk.Label(panel, textvariable=search_status, wraplength=665).pack(anchor='w', pady=4)
        result = []
        searching = {'active': False, 'close_after': False}
        search_cancel = threading.Event()
        updates = queue.Queue()
        def close_dialog():
            if searching['active']:
                searching['close_after'] = True
                search_cancel.set()
                search_status.set('探索を中止しています。処理中のページが終わると閉じます。')
            else:
                dialog.destroy()
        dialog.protocol('WM_DELETE_WINDOW', close_dialog)
        def discover_start():
            if searching['active']:
                return
            searching['active'] = True
            self.busy = True
            search_cancel.clear()
            search_button.configure(state='disabled')
            accept_button.configure(state='disabled')
            entry.configure(state='disabled')
            full_button.configure(state='disabled')
            enabled = self.ocr_enabled.get()
            def job():
                progress = lambda page, count, method: updates.put(f'開始ページを探索中: {page}/{count}ページ（' +
                    {'text': '文字の確認', 'ocr': '見出しの読取', 'layout_ocr': '向きと配置の確認'}.get(method, method) + '）')
                callback, layout_callback, unavailable = None, None, None
                if enabled:
                    try:
                        callback = self.ocr_callback(True)
                        layout_callback = ocr_pdf_layout
                    except Exception as error:
                        unavailable = str(error)
                # Search every earlier page before accepting a later text heading.
                found = find_notice_start(Path(path).resolve(), ocr=callback if layout_callback is None else None,
                                          layout_ocr=layout_callback,
                                          cancelled=search_cancel.is_set, progress=progress)
                if unavailable:
                    found['warnings'].append(unavailable)
                return found
            future = self.pool.submit(job)
            def poll():
                while not updates.empty():
                    search_status.set(updates.get_nowait())
                if not future.done():
                    self.after(100, poll)
                    return
                searching['active'] = False
                self.busy = False
                if searching['close_after']:
                    dialog.destroy()
                    return
                search_button.configure(state='normal')
                accept_button.configure(state='normal')
                full_button.configure(state='normal')
                entry.configure(state='disabled' if full.get() else 'normal')
                try:
                    found = future.result()
                    start = found['start_page']
                    if start is not None:
                        full.set(False)
                        entry.configure(state='normal')
                        entry_value.set(format_page_ranges(range(start, total + 1)))
                        qualification = '（見出しの一部から推定）' if found.get('evidence_strength') == 'weak' else ''
                        text = f'「公告」の開始候補{qualification}: PDF {start}ページ目。候補から末尾を入力しました。原文で開始位置と対象範囲を確認してください。'
                        if found.get('reason'):
                            text += '\n' + found['reason']
                    else:
                        text = '公告の開始ページを特定できませんでした。原文を見てページ範囲を入力してください。'
                    if found['warnings']:
                        text += '\n' + ' / '.join(found['warnings'][:2])
                    search_status.set(text)
                except Exception as error:
                    search_status.set('開始ページの探索に失敗しました: ' + str(error))
            self.after(100, poll)
        search_button = ttk.Button(panel, text='「公告」の開始ページ候補を探す', command=discover_start)
        search_button.pack(anchor='w', pady=6)
        actions = ttk.Frame(panel)
        actions.pack(fill='x', pady=8)
        def open_original():
            try:
                resolved = Path(path).resolve()
                if os.name == 'nt':
                    os.startfile(str(resolved))
                else:
                    webbrowser.open(resolved.as_uri())
            except OSError as error:
                messagebox.showerror('PDFを開けません', str(error), parent=dialog)
        def accept():
            try:
                pages = list(range(1, total + 1)) if full.get() else parse_page_ranges(entry_value.get(), total)
            except ValueError as error:
                messagebox.showerror('公告ページを確認してください', str(error), parent=dialog)
                return
            result.extend(pages)
            dialog.destroy()
        ttk.Button(actions, text='原文PDFを開く', command=open_original).pack(side='left')
        ttk.Button(actions, text='キャンセル', command=close_dialog).pack(side='right', padx=4)
        accept_button = ttk.Button(actions, text='この範囲を対象にする', command=accept)
        accept_button.pack(side='right', padx=4)
        dialog.grab_set()
        if current is None:
            # New imports search automatically; editing an existing scope keeps it.
            dialog.after_idle(discover_start)
        self.wait_window(dialog)
        return result or None

    def edit_notice_scope(self):
        if self.guard() or not self.ensure_saved():
            return
        documents = self.store.documents()
        if not documents:
            messagebox.showinfo('公告範囲', '先に公告PDFを取り込んでください。', parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title('処理後の公告範囲の確認')
        dialog.geometry('860x420')
        ttk.Label(dialog, text='対象外にしたページも原文と確認履歴を保持します。範囲は後から変更できます。', padding=12).pack(anchor='w')
        listing = ttk.Treeview(dialog, columns=('file', 'total', 'notice', 'unscoped'), show='headings', selectmode='browse')
        for key, title, width in [('file', 'PDF名', 380), ('total', '元PDF総頁', 90), ('notice', '公告対象頁', 90), ('unscoped', '範囲の確認状態', 160)]:
            listing.heading(key, text=title)
            listing.column(key, width=width)
        listing.pack(fill='both', expand=True, padx=12)
        for index, document in enumerate(documents):
            listing.insert('', 'end', iid=str(index), values=(document['original_name'], document['page_count'], document['notice_pages'], SCOPE_LABELS.get(document.get('scope_status'), '範囲未判定')))
            if self.current and self.current['document_id'] == document['id']:
                listing.selection_set(str(index))
        def choose():
            selection = listing.selection()
            if not selection or self.guard() or not self.ensure_saved():
                return
            document = documents[int(selection[0])]
            path = self.folder / 'pdf' / (document['id'] + '.pdf')
            current = json.loads(document.get('scope_pages_json', '[]'))
            try:
                pages = self.select_notice_pages(path, document['page_count'], current)
            except Exception as error:
                messagebox.showerror('公告範囲を確認できません', str(error), parent=dialog)
                return
            if pages is None:
                return
            self.store.confirm_scope(document['id'], pages)
            dialog.destroy()
            enabled = self.ocr_enabled.get()
            def job(store, report, cancel):
                ocr, layout = self.processing_callbacks(enabled)
                result = process_document(store, document['id'], ocr=ocr, layout_ocr=layout,
                                          cancelled=cancel.is_set, progress=report)
                return '公告範囲を ' + format_page_ranges(pages) + ' で確認しました。\n' + result_message(result)
            self.background('公告範囲の設定', job)
        def exclude_all():
            selection = listing.selection()
            if not selection or self.guard() or not self.ensure_saved():
                return
            document = documents[int(selection[0])]
            if not messagebox.askyesno('公告対象なし', 'このPDFの全ページを公告対象から外しますか？原文・メモ・確認履歴は保持します。', parent=dialog):
                return
            try:
                self.store.exclude_document_from_notices(document['id'])
                dialog.destroy()
                self.refresh()
            except Exception as error:
                messagebox.showerror('公告範囲を変更できません', str(error), parent=dialog)
        actions = ttk.Frame(dialog, padding=12)
        actions.pack(fill='x')
        ttk.Button(actions, text='選択したPDFは公告対象なし', command=exclude_all).pack(side='left')
        ttk.Button(actions, text='選択したPDFの範囲を設定', command=choose).pack(side='right')

    def import_ledger(self):
        if self.guard() or not self.ensure_saved():
            return
        path = filedialog.askopenfilename(parent=self, filetypes=[('取引先台帳', '*.csv *.xlsx')])
        if not path:
            return
        try:
            rows = read_ledger(path)
            if messagebox.askyesno('台帳更新', f'{len(rows)}件に差し替え、全ページを未確認に戻します。続けますか？', parent=self):
                def job(store, report, cancel):
                    store.backup()
                    store.replace_ledger(rows)
                    return f'台帳{len(rows)}件を読み込み、保存済みの本文を再照合しました。人の確認状態は未確認に戻しました。'
                self.background('取引先台帳の読み込みと再照合', job)
        except Exception as error:
            messagebox.showerror('台帳を更新できません', str(error), parent=self)

    def retry_ocr(self):
        if self.guard() or not self.current or not self.ensure_saved():
            return
        row = self.current.copy()
        if row.get('kind') == 'document':
            self.resume_processing()
            return
        def job(store, report, cancel):
            from ocr_review import analyze_page
            ocr, layout = self.processing_callbacks(True)
            store.process_page(row['document_id'], row['page'], ocr=ocr, force=True)
            current = next(p for p in store.pages() if p['document_id'] == row['document_id'] and p['page'] == row['page'])
            report('方向・領域を確認しています')
            analysis = analyze_page(self.folder / 'pdf' / (row['document_id'] + '.pdf'), row['page'],
                                    layout, base_text=current['text'], ledger=store.ledger())
            store.save_page_analysis(row['document_id'], row['page'], analysis)
            return '選択したページを再読取しました。処理結果と確認の印は一覧から確認してください。人の確認状態は未完了に戻ります。'
        self.background('OCR再実行', job)

    def fetch_dialog(self):
        if self.guard() or not self.ensure_saved():
            return
        attempt = self.store.download_day()
        if attempt:
            self.show_report('本日の取得は実行済みです', '自動取得は1日1回です。失敗・中止した場合や追加の号は、ブラウザで保存して手動で取り込んでください。\n\n' + attempt.get('detail', ''))
            return
        from official_fetcher import OfficialFetcher
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone(timedelta(hours=9))).date()
        dialog = tk.Toplevel(self)
        dialog.title('当日分を取得 — 1日1回')
        dialog.geometry('670x320')
        box = ttk.Frame(dialog, padding=16)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text=f'対象日：{today.isoformat()}（日本時間）\n\n当日分の公開一覧を確認し、選択した号をまとめて取得します。\n取得を開始できるのは1日1回です。後から追加された号・再取得は手動で対応します。\n\nPDFは通信間隔を空けて1件ずつ保存します。サイトから拒否・回数制限の応答がある場合は停止します。\n保存後の読取・照合まで、途中の確認操作なしで進めます。', wraplength=625).pack(anchor='w')
        agreed = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text='公式サイトの利用条件を確認した', variable=agreed).pack(anchor='w', pady=4)
        ttk.Button(box, text='公式の利用条件を開く', command=lambda: webbrowser.open('https://www.kanpo.go.jp/guidance.html')).pack(anchor='w')
        def discover():
            if self.guard():
                return
            if not agreed.get():
                messagebox.showinfo('利用条件', '公式サイトの利用条件を確認し、チェックしてください。', parent=dialog)
                return
            if not self.ensure_saved():
                return
            dialog.destroy()
            fetcher = OfficialFetcher(cancelled=self.cancel_event.is_set, daily_manual=True)
            self.background('官報の公開一覧を確認', lambda store, report, cancel: fetcher.list_issues(today, today),
                            lambda result: self.choose_issues(fetcher, result))
        ttk.Button(box, text='公開一覧を確認', command=discover).pack(anchor='e', pady=12)

    def choose_issues(self, fetcher, discovery):
        from acquisition import find_saved_issue
        if not discovery.items:
            self.show_report('官報の公開一覧', '\n'.join(discovery.warnings) or '指定期間に公開一覧へ掲載された号がありません。')
            return
        dialog = tk.Toplevel(self)
        dialog.title('取得する号を選択')
        dialog.geometry('940x520')
        ttk.Label(dialog, text='Ctrl／Shiftキーで複数選択できます。この取得バッチの開始は1日1回です。以降の確認は処理後に行います。', padding=12).pack(anchor='w')
        listing = ttk.Treeview(dialog, columns=('date', 'title', 'pages', 'state'), show='headings', selectmode='extended')
        for key, label, width in [('date', '発行日', 130), ('title', '号', 290), ('pages', 'ページ', 140), ('state', '取得状態', 120)]:
            listing.heading(key, text=label)
            listing.column(key, width=width)
        listing.pack(fill='both', expand=True, padx=12)
        restricted = set(discovery.restricted_urls)
        for index, issue in enumerate(discovery.items):
            invalid = False
            try:
                known = bool(find_saved_issue(self.store, issue))
            except (OSError, ValueError):
                known, invalid = False, True
            blocked = issue.viewer_url in restricted
            listing.insert('', 'end', iid=str(index), values=(issue.date, issue.title,
                           f'{issue.first_page}–{issue.last_page}', '保存原本を確認' if invalid else ('取得済み' if known else ('自動取得不可' if blocked else '未取得'))))
            if not known and not blocked and not invalid:
                listing.selection_add(str(index))
        if discovery.warnings:
            ttk.Label(dialog, text='\n'.join(discovery.warnings), wraplength=890, padding=10).pack(fill='x')
        def begin():
            chosen = [discovery.items[int(key)] for key in listing.selection()]
            if not chosen or self.guard() or not self.ensure_saved():
                return
            if any(issue.viewer_url in restricted for issue in chosen):
                messagebox.showinfo('自動取得の制限', '自動取得不可の号が選ばれています。ブラウザでPDFを保存し、「PDF取り込み」を使用してください。', parent=dialog)
                return
            enabled = self.ocr_enabled.get()
            dialog.destroy()
            def job(store, report, cancel):
                from acquisition import run_daily_acquisition
                ocr, layout = self.processing_callbacks(enabled)
                return run_daily_acquisition(store, fetcher, chosen, ocr=ocr, layout_ocr=layout,
                                             cancelled=cancel.is_set, progress=report)
            self.background('当日分の取得と読取', job)
        actions = ttk.Frame(dialog, padding=12)
        actions.pack(fill='x')
        def open_selected():
            selection = listing.selection()
            if len(selection) != 1:
                messagebox.showinfo('号を選択', 'ブラウザで開く号を1つ選択してください。', parent=dialog)
                return
            webbrowser.open(discovery.items[int(selection[0])].viewer_url)
        ttk.Button(actions, text='選択した1号をブラウザで開く', command=open_selected).pack(side='left')
        ttk.Button(actions, text='選択した号を取得・保存して読取開始', command=begin,
                   state='disabled' if len(restricted) == len(discovery.items) else 'normal').pack(side='right')

    def open_pdf(self):
        if not self.current:
            return
        path = self.folder / 'pdf' / (self.current['document_id'] + '.pdf')
        try:
            if not path.is_file():
                raise FileNotFoundError('保存した原文PDFが見つかりません。バックアップを確認してください。')
            if os.name == 'nt':
                os.startfile(str(path))
            else:
                webbrowser.open(path.as_uri())
        except OSError as error:
            messagebox.showerror('PDFを開けません', str(error), parent=self)

    def open_storage(self):
        path = (self.folder / 'pdf').resolve()
        try:
            if os.name == 'nt':
                os.startfile(str(path))
            else:
                webbrowser.open(path.as_uri())
        except OSError as error:
            messagebox.showerror('保存先を開けません', str(error), parent=self)

    def preview_page(self):
        if self.guard() or not self.current or not self.ensure_saved():
            return
        row = self.current.copy()
        if row.get('kind') == 'document':
            messagebox.showinfo('ページを選択', 'ページ番号のある行を選んでください。範囲が不明なPDFは「原文PDF」で確認できます。', parent=self)
            return
        workspace = tempfile.TemporaryDirectory(prefix='kanpo-preview-')
        output = Path(workspace.name) / 'page.png'
        def job(store, report, cancel):
            try:
                return render_pdf_page(self.folder / 'pdf' / (row['document_id'] + '.pdf'), row['page'], output)
            except Exception:
                workspace.cleanup()
                raise
        def done(result):
            from review_ui import ReviewPreview
            try:
                # PhotoImage loads the image; the derivative can then be removed.
                ReviewPreview(self, row, result, self.reread_region)
            finally:
                workspace.cleanup()
        self.background('原文の確認用画像を表示', job, done)

    def reread_region(self, row, region, rotations, preview):
        if self.guard() or not self.ensure_saved():
            return
        def job(store, report, cancel):
            from ocr_review import analyze_layout
            old = store.page_analysis(row['document_id'], row['page'])
            current = next(p for p in store.pages() if p['document_id'] == row['document_id'] and p['page'] == row['page'])
            box = dict(zip(('x', 'y', 'width', 'height'), region))
            path = self.folder / 'pdf' / (row['document_id'] + '.pdf')
            try:
                layout = ocr_pdf_layout(path, row['page'], regions=[region], rotations=rotations)
                local = analyze_layout(layout, ledger=store.ledger())
                addition = local.get('text', '')
                # The original readings and old warning evidence remain available.
                text = current['text'] + ('\n\n【手動指定範囲の補足OCR】\n' + addition if addition.strip() else '')
                analysis = {**old, 'text': text, 'status': 'needs_review',
                            'supplement_succeeded': bool(addition.strip()),
                            'views': old.get('views', []) + local.get('views', []),
                            'evidence': old.get('evidence', []) + local.get('evidence', []),
                            'flags': old.get('flags', []) + local.get('flags', []) + [{
                                'kind': 'manual_region', 'bbox': box, 'text': addition,
                                'reason': '手動指定した範囲の補足OCRです。元の読み取りと原文を比較してください。' if addition.strip() else '手動指定した範囲から文字を取得できませんでした。原文を確認してください。'}]}
            except Exception as error:
                analysis = {**old, 'text': current['text'], 'status': 'needs_review',
                            'supplement_succeeded': False,
                            'flags': old.get('flags', []) + [{'kind': 'region_failed', 'bbox': box,
                                'text': '', 'reason': '選択範囲の再読取に失敗しました: ' + str(error)}]}
            store.save_page_analysis(row['document_id'], row['page'], analysis)
            store.review(row['document_id'], row['page'], '要確認', current['note'])
            return analysis
        def done(analysis):
            if preview.winfo_exists():
                text = next(p['text'] for p in self.store.pages() if p['document_id'] == row['document_id'] and p['page'] == row['page'])
                preview.update_analysis(update_ledger_flags(text, analysis, self.store.ledger()))
                preview.reason.set('選択範囲の処理結果を保存しました。原文と比較してから、主画面で確認結果を保存してください。')
        self.background('選択範囲の再読取', job, done)

    def save(self, refresh=True):
        if self.guard() or not self.current:
            return False
        if self.current.get('kind') == 'document':
            messagebox.showinfo('公告範囲を確認', 'PDF全体の行は「公告範囲を確認」から操作してください。', parent=self)
            return False
        if self.state.get() == '確認済み' and self.current.get('scope_status') != 'confirmed':
            messagebox.showinfo('公告範囲が未確定', '先に「公告範囲を確認」で対象範囲を原文と確認してください。', parent=self)
            return False
        try:
            row = self.current
            self.store.review(row['document_id'], row['page'], self.state.get(), self.note.get())
            row.update(state=self.state.get(), note=self.note.get())
            if refresh:
                self.refresh(keep=row['key'])
            return True
        except Exception as error:
            messagebox.showerror('保存できません', str(error), parent=self)
            return False

    def show_history(self):
        if self.current:
            rows = self.store.history(document=self.current['document_id'], page=self.current['page'] or None)
            text = '\n\n'.join(f"{row['at']}  {row['action']}\n{json.dumps(row['details'], ensure_ascii=False, indent=2)}" for row in rows)
            self.show_report('ページの変更履歴', text or 'このページの変更履歴はありません。')

    def export(self):
        if self.guard() or not self.ensure_saved():
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.csv', initialfile='官報確認結果.csv', filetypes=[('CSV', '*.csv')])
        if path:
            def job(store, report, cancel):
                store.export(path, notice_only=True)
                return '公告対象ページと範囲・処理の確認待ちPDFを出力しました。暫定範囲はCSVにも明記します。\n' + path
            self.background('CSV出力', job)

    def backup(self):
        if self.guard() or not self.ensure_saved():
            return
        self.background('全データのバックアップ', lambda store, report, cancel: str(store.backup_bundle()) +
                        '\nPDF・台帳・確認結果・履歴を保存しました。このZIPを社内で許可された別媒体にもコピーしてください。')

    def close(self):
        if self.guard() or not self.ensure_saved():
            return
        try:
            self.store.backup()
        except Exception as error:
            if not messagebox.askyesno('バックアップ失敗', str(error) + '\nバックアップせず終了しますか？', parent=self):
                return
        self.pool.shutdown(wait=False)
        self.store.db.close()
        self.lock.close()
        self.destroy()


def main():
    parser = argparse.ArgumentParser(description='官報確認 Windows')
    parser.add_argument('--data-dir', type=Path, help='検証用の別保存先')
    args = parser.parse_args()
    try:
        App(args.data_dir).mainloop()
    except Exception as error:
        try:
            messagebox.showerror('官報確認を起動できません', str(error))
        except Exception:
            print('官報確認を起動できません:', error)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
