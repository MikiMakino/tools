"""Offline, page-level review foundation. Never treats text search as final matching."""
import csv
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone, date
import zipfile
from pdf_access import ensure_readable


def _cell_text(value):
    return '' if value is None else str(value).strip()


def normalize(value):
    value = unicodedata.normalize('NFKC', _cell_text(value))
    value = value.replace('(株)', '株式会社').replace('(有)', '有限会社')
    return re.sub(r'\s+', '', value).casefold()


def read_ledger(path):
    path = Path(path)
    if path.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = list(book.worksheets[0].values)
        finally:
            book.close()
        if not rows:
            raise ValueError('台帳が空です。')
        headers = [_cell_text(v) for v in rows[0]]
        data = [dict(zip(headers, row)) for row in rows[1:]]
    else:
        raw = path.read_bytes()
        try:
            content = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            content = raw.decode('cp932')
        reader = csv.DictReader(io.StringIO(content))
        headers = [_cell_text(v) for v in reader.fieldnames or []]
        reader.fieldnames = headers
        data = list(reader)
    if not {'取引先コード', '会社名'}.issubset(headers):
        raise ValueError('列名「取引先コード」「会社名」が必要です。「住所」は任意です。')
    if any(headers.count(header) > 1 for header in ('取引先コード', '会社名', '住所')):
        raise ValueError('台帳に同じ列名が複数あります。')
    result = []
    for row in data:
        if not any(v is not None and str(v).strip() for v in row.values()):
            continue
        result.append({'code': row.get('取引先コード'), 'name': row.get('会社名'),
                       'address': row.get('住所')})
    return _validate_ledger(result)


def _validate_ledger(rows):
    result, codes = [], set()
    try:
        rows = iter(rows)
    except TypeError as error:
        raise ValueError('台帳の行形式が不正です。') from error
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('台帳の行形式が不正です。')
        code, name = _cell_text(row.get('code')), _cell_text(row.get('name'))
        if not code or not normalize(name) or code in codes:
            raise ValueError('取引先コード・会社名の空欄、またはコード重複があります。台帳は更新されません。')
        codes.add(code)
        result.append({'code': code, 'name': name, 'address': _cell_text(row.get('address'))})
    if not result:
        raise ValueError('有効な取引先がありません。')
    return result


def match_candidates(text, ledger):
    normalized = normalize(text)
    candidates = []
    for company in ledger:
        name = normalize(company.get('name'))
        if name and name in normalized:
            address = normalize(company.get('address'))
            candidates.append({**company, 'reason': '社名・住所が同じページ内に出現（要原文確認）'
                               if address and address in normalized else '社名がページ内に出現（要原文確認）'})
    return candidates


def _page_has_images(page):
    """Inspect PDF image objects without decoding them or requiring Pillow."""
    from pypdf.generic import ContentStream
    visited = set()

    def resolved(value):
        return value.get_object() if hasattr(value, 'get_object') else value

    def inline_images(stream):
        if stream is None:
            return False
        content = stream if isinstance(stream, ContentStream) else ContentStream(stream, page.pdf)
        return any(operator == b'INLINE IMAGE' for operands, operator in content.operations)

    def resource_images(resources):
        resources = resolved(resources)
        if not resources or id(resources) in visited:
            return False
        visited.add(id(resources))
        objects = resolved(resources.get('/XObject', {}))
        for reference in objects.values():
            obj = resolved(reference)
            if obj.get('/Subtype') == '/Image':
                return True
            if obj.get('/Subtype') == '/Form':
                if resource_images(obj.get('/Resources')) or inline_images(obj):
                    return True
        return False

    return resource_images(page.get('/Resources')) or inline_images(page.get_contents())


def _extract_page(page):
    extraction_ok = True
    try:
        text = page.extract_text() or ''
        issue = '文字抽出済み・公告分割未検証' if text.strip() else '文字なし・画像を原文確認してください'
    except Exception as error:
        text, issue, extraction_ok = '', '文字抽出失敗: ' + str(error), False
    try:
        images = int(_page_has_images(page))
        if images:
            issue += ' / 画像本文未OCR・原文確認が必要です'
    except Exception:
        images = -1
        issue += ' / 画像有無を判定できません・原文確認が必要です'
    return {'extracted_text': text, 'issue': issue, 'has_images': images, 'extraction_ok': extraction_ok}


def _merge_page_text(extracted, recognized):
    if not extracted.strip():
        return recognized
    if not recognized.strip() or recognized.strip() in extracted:
        return extracted
    if extracted.strip() in recognized:
        return recognized
    return extracted.rstrip() + '\n\n' + recognized.lstrip()


def _ocr_issue(extracted, issue, status, previous_ocr=''):
    if extracted.strip():
        if status == 'succeeded':
            issue = 'OCR補足済み・抽出文字と統合（誤認識・公告分割を原文確認してください）'
        else:
            issue += ' / 元の抽出本文を保持・画像本文は原文確認してください'
    if previous_ocr and status != 'succeeded':
        issue += ' / 既存OCR本文を保持'
    return issue


def _selected_pages(pages, total):
    if isinstance(pages, (str, bytes, dict)):
        raise ValueError('公告ページはページ番号の一覧で指定してください。')
    try:
        pages = list(pages)
    except TypeError as error:
        raise ValueError('公告ページはページ番号の一覧で指定してください。') from error
    if not pages or any(type(number) is not int or not 1 <= number <= total for number in pages):
        raise ValueError('公告ページは元PDFの範囲内の整数で、1ページ以上指定してください。')
    if len(set(pages)) != len(pages):
        raise ValueError('公告ページ番号が重複しています。')
    return sorted(pages)


def _download_date(day=None):
    if day is None:
        return datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError('取得日は YYYY-MM-DD 形式で指定してください。')
    return day


class Store:
    def __init__(self, folder):
        self.folder = Path(folder)
        (self.folder / 'pdf').mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.folder / 'kanpo.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, original_name TEXT NOT NULL, imported_at TEXT NOT NULL,
                page_count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (
                document_id TEXT NOT NULL, page INTEGER NOT NULL, text TEXT NOT NULL,
                issue TEXT NOT NULL, state TEXT NOT NULL DEFAULT '未確認', note TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT, PRIMARY KEY(document_id, page));
            CREATE TABLE IF NOT EXISTS ledger (code TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history (
                at TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS download_days (
                day TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL, finished_at TEXT);
        ''')
        if 'source_url' not in {row['name'] for row in self.db.execute('PRAGMA table_info(documents)')}:
            with self.db:
                self.db.execute("ALTER TABLE documents ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")
        if 'notice_scope' not in {row['name'] for row in self.db.execute('PRAGMA table_info(pages)')}:
            with self.db:
                self.db.execute('ALTER TABLE pages ADD COLUMN notice_scope INTEGER NOT NULL DEFAULT 0 '
                                'CHECK (notice_scope IN (-1,0,1))')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(pages)')}
        with self.db:
            if 'extracted_text' not in columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN extracted_text TEXT NOT NULL DEFAULT ''")
                self.db.execute("UPDATE pages SET extracted_text=text WHERE issue NOT LIKE 'OCR%'")
            if 'ocr_text' not in columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN ocr_text TEXT NOT NULL DEFAULT ''")
                self.db.execute("UPDATE pages SET ocr_text=text WHERE issue LIKE 'OCR%'")
            if 'has_images' not in columns:
                self.db.execute('ALTER TABLE pages ADD COLUMN has_images INTEGER NOT NULL DEFAULT -1 '
                                'CHECK (has_images IN (-1,0,1))')
        document_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(documents)')}
        with self.db:
            for name, default in (('processing_status', 'archived'), ('processing_detail', ''),
                                  ('scope_status', 'unconfirmed'), ('scope_detail', ''), ('scope_pages_json', '[]')):
                if name not in document_columns:
                    self.db.execute(f"ALTER TABLE documents ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            if 'scope_status' not in document_columns:
                # Explicit scopes in earlier releases were selected by a person.
                for row in self.db.execute('SELECT id FROM documents').fetchall():
                    saved = self.db.execute('SELECT page,notice_scope FROM pages WHERE document_id=? ORDER BY page', (row['id'],)).fetchall()
                    chosen = [item['page'] for item in saved if item['notice_scope'] == 1]
                    state = 'confirmed' if chosen else ('excluded' if saved and all(item['notice_scope'] == -1 for item in saved) else 'unconfirmed')
                    self.db.execute('UPDATE documents SET scope_status=?,scope_pages_json=? WHERE id=?',
                                    (state, json.dumps(chosen), row['id']))
            if 'analysis_json' not in columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN analysis_json TEXT NOT NULL DEFAULT '{}'")
            if 'machine_status' not in columns:
                self.db.execute("ALTER TABLE pages ADD COLUMN machine_status TEXT NOT NULL DEFAULT 'not_processed'")
                self.db.execute("UPDATE pages SET machine_status=CASE WHEN issue LIKE 'OCR失敗%' THEN 'failed' "
                                "WHEN issue LIKE 'OCR文字なし%' THEN 'empty' WHEN ocr_text<>'' THEN 'succeeded' ELSE 'not_processed' END")
            if 'processing_status' not in document_columns:
                # Earlier releases registered documents only while importing pages.
                # Such a document is not a new, archive-only pending download.
                for row in self.db.execute('SELECT DISTINCT document_id FROM pages').fetchall():
                    statuses = [page['machine_status'] for page in self.db.execute(
                        'SELECT machine_status FROM pages WHERE document_id=?', (row['document_id'],))]
                    status = 'completed' if all(value == 'succeeded' for value in statuses) else 'needs_review'
                    self.db.execute('UPDATE documents SET processing_status=?,processing_detail=? WHERE id=?',
                                    (status, '以前の取込結果を移行しました。原文確認の状態は保持しています。', row['document_id']))

    def document(self, document):
        document = document['id'] if isinstance(document, dict) else document
        row = self.db.execute('SELECT * FROM documents WHERE id=?', (document,)).fetchone()
        if row is None:
            raise ValueError('指定されたPDFがありません。')
        result = dict(row)
        result['scope_pages'] = json.loads(result['scope_pages_json'])
        result['path'] = str(self.folder / 'pdf' / (result['id'] + '.pdf'))
        return result

    def archive_pdf(self, path, source_url=''):
        """Persist an unchanged original before any extraction, scope search or OCR."""
        from pypdf import PdfReader
        path = Path(path)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        reader = PdfReader(io.BytesIO(raw))
        ensure_readable(reader)
        count = len(reader.pages)
        if not count:
            raise ValueError('ページがありません。')
        destination = self.folder / 'pdf' / (digest + '.pdf')
        temporary = None
        try:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                existing = self.db.execute('SELECT 1 FROM documents WHERE id=?', (digest,)).fetchone()
                if existing:
                    if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                        raise ValueError('保存済みの原文PDFがないか内容が変更されています。バックアップから復元してください。')
                    if source_url:
                        self.db.execute("UPDATE documents SET source_url=? WHERE id=? AND source_url=''", (_cell_text(source_url), digest))
                else:
                    if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.tmp', delete=False) as stream:
                            temporary = Path(stream.name)
                            stream.write(raw)
                        temporary.replace(destination)
                    self.db.execute('INSERT INTO documents(id,original_name,imported_at,page_count,source_url) VALUES (?,?,?,?,?)',
                                    (digest, path.name, datetime.now().isoformat(), count, _cell_text(source_url)))
                    self.db.execute('INSERT INTO history VALUES (?,?,?)', (datetime.now().isoformat(), '原本保存',
                                    json.dumps({'document': digest, 'original_name': path.name}, ensure_ascii=False)))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return self.document(digest)

    def set_processing(self, document, status, detail=''):
        if status not in ('archived', 'processing', 'completed', 'needs_review', 'cancelled', 'failed'):
            raise ValueError('不正な処理状態です。')
        saved = self.document(document)
        with self.db:
            self.db.execute('UPDATE documents SET processing_status=?,processing_detail=? WHERE id=?',
                            (status, _cell_text(detail), saved['id']))

    def set_scope_detail(self, document, detail):
        saved = self.document(document)
        with self.db:
            self.db.execute('UPDATE documents SET scope_detail=? WHERE id=?', (_cell_text(detail), saved['id']))

    def _set_scope(self, document, pages, status, detail=''):
        saved = self.document(document)
        pages = _selected_pages(pages, saved['page_count'])
        chosen = set(pages)
        with self.db:
            changes = []
            for row in self.db.execute('SELECT page,notice_scope FROM pages WHERE document_id=?', (saved['id'],)):
                after = 1 if row['page'] in chosen else -1
                if after != row['notice_scope']:
                    changes.append({'page': row['page'], 'before': row['notice_scope'], 'after': after})
            self.db.executemany('UPDATE pages SET notice_scope=? WHERE document_id=? AND page=?',
                                [(item['after'], saved['id'], item['page']) for item in changes])
            self.db.execute('UPDATE documents SET scope_status=?,scope_detail=?,scope_pages_json=? WHERE id=?',
                            (status, _cell_text(detail), json.dumps(pages), saved['id']))
            if changes or saved['scope_status'] != status or saved['scope_pages'] != pages:
                self.db.execute('INSERT INTO history VALUES (?,?,?)', (datetime.now().isoformat(), '公告範囲変更', json.dumps({
                    'document': saved['id'], 'selected_pages': pages, 'scope_status': status,
                    'previous_scope_status': saved['scope_status'], 'changes': changes}, ensure_ascii=False)))
        return self.document(saved['id'])

    def confirm_scope(self, document, pages):
        """Record a human's scope decision without marking any page reviewed."""
        return self._set_scope(document, pages, 'confirmed')

    def set_provisional_scope(self, document, pages, detail=''):
        saved = self.document(document)
        if saved['scope_status'] in ('confirmed', 'excluded'):
            raise ValueError('人が確定した公告範囲を自動判定で上書きできません。')
        return self._set_scope(document, pages, 'provisional', detail)

    def claim_download_day(self, day=None):
        day = _download_date(day)
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO download_days(day,status,started_at) VALUES (?,?,?)',
                                      (day, 'running', datetime.now(timezone(timedelta(hours=9))).isoformat()))
        return inserted.rowcount == 1

    def finish_download_day(self, status, detail='', day=None):
        if status not in ('completed', 'partial', 'failed', 'cancelled'):
            raise ValueError('不正な取得結果です。')
        with self.db:
            updated = self.db.execute('UPDATE download_days SET status=?,detail=?,finished_at=? WHERE day=?',
                                     (status, _cell_text(detail), datetime.now(timezone(timedelta(hours=9))).isoformat(), _download_date(day)))
            if updated.rowcount != 1:
                raise ValueError('その日の取得処理は開始されていません。')

    def download_day(self, day=None):
        row = self.db.execute('SELECT * FROM download_days WHERE day=?', (_download_date(day),)).fetchone()
        return dict(row) if row else None

    def ledger(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM ledger ORDER BY code')]

    def replace_ledger(self, rows):
        rows = _validate_ledger(rows)
        with self.db:
            self.db.execute('DELETE FROM ledger')
            self.db.executemany('INSERT INTO ledger VALUES (:code,:name,:address)', rows)
            # New ledger invalidates prior page-level matching decisions.
            self.db.execute("UPDATE pages SET state='未確認', reviewed_at=NULL")
            self.db.execute('INSERT INTO history VALUES (?,?,?)',
                            (datetime.now().isoformat(), '台帳更新', json.dumps(rows, ensure_ascii=False)))

    def has_source(self, url):
        url = _cell_text(url)
        return bool(url and self.db.execute('SELECT 1 FROM documents WHERE source_url=?', (url,)).fetchone())

    def exclude_document_from_notices(self, document):
        """Reversibly exclude a mistaken whole-PDF selection; preserve originals/reviews."""
        with self.db:
            if not self.db.execute('SELECT 1 FROM documents WHERE id=?', (document,)).fetchone():
                raise ValueError('指定されたPDFがありません。')
            rows = self.db.execute('SELECT page,notice_scope FROM pages WHERE document_id=?', (document,)).fetchall()
            changes = [{'page': row['page'], 'before': row['notice_scope'], 'after': -1}
                       for row in rows if row['notice_scope'] != -1]
            self.db.execute('UPDATE pages SET notice_scope=-1 WHERE document_id=?', (document,))
            self.db.execute("UPDATE documents SET scope_status='excluded',scope_pages_json='[]',scope_detail='' WHERE id=?", (document,))
            if changes:
                self.db.execute('INSERT INTO history VALUES (?,?,?)',
                                (datetime.now().isoformat(), '公告範囲変更', json.dumps({
                                    'document': document, 'selected_pages': [], 'changes': changes,
                                }, ensure_ascii=False)))

    @staticmethod
    def _ocr_page(path, number, ocr):
        try:
            text = ocr(path, number)
            if not isinstance(text, str):
                raise ValueError('OCRの戻り値が文字列ではありません。')
            if text.strip():
                return text, 'OCR抽出済み・誤認識と公告分割を原文確認してください', 'succeeded'
            return '', 'OCR文字なし・画像を原文確認してください', 'empty'
        except Exception as error:
            return '', 'OCR失敗: ' + str(error), 'failed'

    def import_pdf(self, path, ocr=None, source_url='', selected_pages=None):
        """Import selected original page numbers; an explicit selection replaces the notice scope.

        Previously stored text/reviews are retained, including excluded pages. None keeps
        legacy whole-PDF import behavior and leaves new pages' notice scope unassigned.
        Return True only when at least one previously unstored page was added.
        """
        from pypdf import PdfReader
        path = Path(path)
        if ocr is not None and not callable(ocr):
            raise ValueError('OCR処理を指定してください。')
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        # Reading from bytes avoids leaving the user's original PDF open on Windows.
        reader = PdfReader(io.BytesIO(raw))
        ensure_readable(reader)
        page_count = len(reader.pages)
        if not page_count:
            raise ValueError('ページがありません。')
        if selected_pages is None:
            selected = list(range(1, page_count + 1))
        else:
            if isinstance(selected_pages, (str, bytes, dict)):
                raise ValueError('公告ページはページ番号の一覧で指定してください。')
            try:
                selected = list(selected_pages)
            except TypeError as error:
                raise ValueError('公告ページはページ番号の一覧で指定してください。') from error
            if not selected or any(type(number) is not int or not 1 <= number <= page_count
                                   for number in selected):
                raise ValueError('公告ページは元PDFの1ページ目から総ページ数までの整数で、1ページ以上指定してください。')
            if len(set(selected)) != len(selected):
                raise ValueError('公告ページ番号が重複しています。')
            selected.sort()
        # Even cancellation or a later OCR/database failure must not discard the original.
        self.archive_pdf(path, source_url=source_url)
        already_saved = {row['page'] for row in self.db.execute(
            'SELECT page FROM pages WHERE document_id=?', (digest,))}
        pages = []
        for number in selected:
            if number in already_saved:
                continue
            page = reader.pages[number - 1]
            extracted = _extract_page(page)
            text, issue, recognized = extracted['extracted_text'], extracted['issue'], ''
            status = 'succeeded' if text.strip() and extracted['has_images'] == 0 else 'not_processed'
            if ocr is not None and (not text.strip() or extracted['has_images'] != 0):
                recognized, issue, status = self._ocr_page(path, number, ocr)
                issue = _ocr_issue(text, issue, status)
            pages.append((digest, number, _merge_page_text(text, recognized), issue,
                          text, recognized, extracted['has_images'], status))
        destination = self.folder / 'pdf' / (digest + '.pdf')
        temporary = None
        installed = False
        try:
            if not self.db.execute('SELECT 1 FROM documents WHERE id=?', (digest,)).fetchone():
                with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.tmp', delete=False) as stream:
                    temporary = Path(stream.name)
                temporary.write_bytes(raw)
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                existing = self.db.execute('SELECT 1 FROM documents WHERE id=?', (digest,)).fetchone()
                if existing:
                    if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                        raise ValueError('保存済みの原文PDFがないか内容が変更されています。バックアップから復元してください。')
                    if source_url:
                        self.db.execute("UPDATE documents SET source_url=? WHERE id=? AND source_url=''",
                                        (_cell_text(source_url), digest))
                else:
                    self.db.execute('INSERT INTO documents(id,original_name,imported_at,page_count,source_url) '
                                    'VALUES (?,?,?,?,?)',
                                    (digest, path.name, datetime.now().isoformat(), page_count, _cell_text(source_url)))
                    temporary.replace(destination)
                    installed = True
                added = self.db.executemany(
                    'INSERT INTO pages(document_id,page,text,issue,extracted_text,ocr_text,has_images,machine_status) '
                    'VALUES (?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(document_id,page) DO NOTHING', pages).rowcount
                if selected_pages is not None:
                    self.db.execute("UPDATE documents SET scope_status='confirmed',scope_pages_json=?,scope_detail='' WHERE id=?",
                                    (json.dumps(selected), digest))
                    selected_set = set(selected)
                    changes = []
                    for row in self.db.execute('SELECT page,notice_scope FROM pages WHERE document_id=?', (digest,)):
                        scope = 1 if row['page'] in selected_set else -1
                        if scope != row['notice_scope']:
                            changes.append({'page': row['page'], 'before': row['notice_scope'], 'after': scope})
                    if changes:
                        self.db.executemany('UPDATE pages SET notice_scope=? WHERE document_id=? AND page=?',
                                            [(item['after'], digest, item['page']) for item in changes])
                        self.db.execute('INSERT INTO history VALUES (?,?,?)',
                                        (datetime.now().isoformat(), '公告範囲変更', json.dumps({
                                            'document': digest, 'selected_pages': selected, 'changes': changes,
                                        }, ensure_ascii=False)))
                selected_set = set(selected)
                saved_pages = [row for row in self.db.execute(
                    'SELECT page,machine_status,analysis_json FROM pages WHERE document_id=?', (digest,))
                    if row['page'] in selected_set]
                status = ('completed' if saved_pages and all(
                    row['machine_status'] == 'succeeded' and not json.loads(row['analysis_json']).get('flags')
                    for row in saved_pages) else 'needs_review')
                self.db.execute('UPDATE documents SET processing_status=?,processing_detail=? WHERE id=?',
                                (status, 'ページの取込結果を保存しました。本文は人の確認が必要です。', digest))
        except Exception:
            if installed and not self.db.execute('SELECT 1 FROM documents WHERE id=?', (digest,)).fetchone():
                destination.unlink(missing_ok=True)
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return added > 0

    def page_analysis(self, document, page):
        saved = self.document(document)
        row = self.db.execute('SELECT analysis_json FROM pages WHERE document_id=? AND page=?', (saved['id'], page)).fetchone()
        if row is None:
            raise ValueError('指定されたページがありません。')
        return json.loads(row['analysis_json'])

    def save_page_analysis(self, document, page, analysis):
        saved = self.document(document)
        if not isinstance(analysis, dict) or not isinstance(analysis.get('flags', []), list):
            raise ValueError('ページ解析結果の形式が不正です。')
        if any(not isinstance(flag, dict) for flag in analysis.get('flags', [])):
            raise ValueError('要確認箇所の形式が不正です。')
        json.dumps(analysis, ensure_ascii=False, allow_nan=False)
        row = self.db.execute('SELECT * FROM pages WHERE document_id=? AND page=?', (saved['id'], page)).fetchone()
        if row is None:
            raise ValueError('指定されたページがありません。')
        supplement_succeeded = analysis.get('supplement_succeeded') is True
        # A successful region read does not establish that the whole page was read.
        # Keep its previous processing state so a failed/incomplete page is retried.
        status = row['machine_status'] if supplement_succeeded else analysis.get('machine_status', row['machine_status'])
        if status not in ('not_processed', 'succeeded', 'empty', 'failed'):
            raise ValueError('不正な機械処理状態です。')
        if 'text' in analysis and not isinstance(analysis['text'], str):
            raise ValueError('解析本文は文字列で指定してください。')
        recognized = row['ocr_text']
        if (status == 'succeeded' or supplement_succeeded) and analysis.get('text', '').strip():
            # A manual region result supplements the whole page; it cannot erase
            # text outside that region. Full replacement uses process_page(force=True).
            recognized = _merge_page_text(recognized, analysis['text'])
        text = _merge_page_text(row['extracted_text'], recognized) or row['text']
        analysis = {**analysis, 'text': text, 'machine_status': status}
        issue = {'succeeded': 'OCR解析結果を補足・原文の再確認が必要です',
                 'empty': 'OCR文字なし・既存本文を保持・原文確認が必要です',
                 'failed': 'OCR失敗・既存本文を保持・原文確認が必要です',
                 'not_processed': 'OCR未処理・原文確認が必要です'}[status]
        if supplement_succeeded:
            issue += ' / 局所OCRを補足しました。ページ全体の処理状態は継続しています'
        encoded = json.dumps(analysis, ensure_ascii=False, allow_nan=False)
        with self.db:
            self.db.execute("UPDATE pages SET analysis_json=?,machine_status=?,text=?,ocr_text=?,issue=?,state='要確認',reviewed_at=NULL "
                            'WHERE document_id=? AND page=?', (encoded, status, text, recognized, issue, saved['id'], page))
            self.db.execute('INSERT INTO history VALUES (?,?,?)', (datetime.now().isoformat(), 'ページ解析更新', json.dumps({
                'document': saved['id'], 'page': page, 'result': status, 'previous_state': row['state'],
                'previous_reviewed_at': row['reviewed_at'], 'previous_ocr_text': row['ocr_text'],
                'previous_issue': row['issue'], 'previous_flags': json.loads(row['analysis_json']).get('flags', []),
                'supplement_succeeded': supplement_succeeded,
                'state': '要確認', 'note': row['note']}, ensure_ascii=False)))

    def process_page(self, document, page, ocr=None, force=False, analyze=None):
        """Persist one page atomically; OCR success never means human confirmation.

        analyze, when supplied, receives (path, page, extracted_text) and returns
        the quality module's JSON result. Completed pages can be skipped on resume.
        """
        from pypdf import PdfReader
        saved = self.document(document)
        if type(page) is not int or not 1 <= page <= saved['page_count']:
            raise ValueError('ページが元PDFの範囲外です。')
        if any(callback is not None and not callable(callback) for callback in (ocr, analyze)):
            raise ValueError('OCRまたは解析処理を指定してください。')
        previous = self.db.execute('SELECT * FROM pages WHERE document_id=? AND page=?', (saved['id'], page)).fetchone()
        path = Path(saved['path'])
        integrity_error = None
        try:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != saved['id']:
                raise ValueError('保存された原文PDFの内容が変更されています。')
        except Exception as error:
            integrity_error = error
        if previous and previous['machine_status'] == 'succeeded' and not force and integrity_error is None:
            return {**dict(previous), 'flags': json.loads(previous['analysis_json']).get('flags', []), 'skipped': True}
        base = previous['extracted_text'] if previous else ''
        recognized = previous['ocr_text'] if previous else ''
        old_recognized = recognized
        images, analysis, status = -1, {}, 'not_processed'
        try:
            if integrity_error is not None:
                raise integrity_error
            reader = PdfReader(io.BytesIO(raw))
            ensure_readable(reader)
            extracted = _extract_page(reader.pages[page - 1])
            if extracted['extraction_ok']:
                base = extracted['extracted_text']
            images, issue = extracted['has_images'], extracted['issue']
            if analyze is not None:
                analysis = analyze(path, page, base)
                if not isinstance(analysis, dict) or not isinstance(analysis.get('text'), str):
                    raise ValueError('ページ解析結果の形式が不正です。')
                status = analysis.get('machine_status', 'succeeded' if analysis['text'].strip() else 'empty')
                if status not in ('succeeded', 'empty', 'failed'):
                    raise ValueError('不正な機械処理状態です。')
                if status == 'succeeded':
                    recognized = analysis['text']
                issue = {'succeeded': 'OCR処理済み・読み取り精度と公告範囲は人の確認が必要です',
                         'empty': 'OCR文字なし・原文確認が必要です', 'failed': 'OCR失敗・原文確認が必要です'}[status]
            elif ocr is not None and (not base.strip() or images != 0):
                new_text, issue, status = self._ocr_page(path, page, ocr)
                if status == 'succeeded':
                    recognized = new_text
                issue = _ocr_issue(base, issue, status, previous_ocr=recognized)
            elif base.strip() and images == 0:
                status = 'succeeded'
            elif ocr is None:
                status = 'not_processed' if extracted['extraction_ok'] else 'failed'
            flags = analysis.get('flags', [])
            if not isinstance(flags, list) or any(not isinstance(flag, dict) for flag in flags):
                raise ValueError('要確認箇所の形式が不正です。')
            if status in ('empty', 'failed', 'not_processed') and not flags:
                flags = [{'kind': 'processing', 'reason': issue, 'bbox': None, 'text': ''}]
            analysis = {**analysis, 'machine_status': status, 'flags': flags,
                        'status': analysis.get('status', 'needs_review' if flags else 'read')}
            json.dumps(analysis, ensure_ascii=False, allow_nan=False)
        except Exception as error:
            recognized = old_recognized
            status, issue = 'failed', 'OCRまたは文字抽出失敗: ' + str(error)
            analysis = {'status': 'failed', 'machine_status': status, 'flags': [
                {'kind': 'processing', 'reason': issue, 'bbox': None, 'text': ''}]}
        text = _merge_page_text(base, recognized)
        analysis['text'] = text
        scope = 1 if page in saved['scope_pages'] else (-1 if saved['scope_status'] != 'unconfirmed' else 0)
        state = '要確認' if previous or analysis['flags'] else '未確認'
        stamp = datetime.now().isoformat()
        with self.db:
            self.db.execute('INSERT INTO pages(document_id,page,text,issue,extracted_text,ocr_text,has_images,notice_scope,state,machine_status,analysis_json) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(document_id,page) DO UPDATE SET '
                            'text=excluded.text,issue=excluded.issue,extracted_text=excluded.extracted_text,ocr_text=excluded.ocr_text,'
                            'has_images=excluded.has_images,notice_scope=excluded.notice_scope,state=excluded.state,reviewed_at=NULL,'
                            'machine_status=excluded.machine_status,analysis_json=excluded.analysis_json',
                            (saved['id'], page, text, issue, base, recognized, images, scope, state, status,
                             json.dumps(analysis, ensure_ascii=False, allow_nan=False)))
            self.db.execute('INSERT INTO history VALUES (?,?,?)', (stamp, 'ページ処理', json.dumps({
                'document': saved['id'], 'page': page, 'result': status,
                'previous_state': previous['state'] if previous else None,
                'previous_reviewed_at': previous['reviewed_at'] if previous else None,
                'previous_issue': previous['issue'] if previous else None,
                'previous_ocr_text': previous['ocr_text'] if previous else None,
                'state': state, 'note': previous['note'] if previous else ''}, ensure_ascii=False)))
        row = self.db.execute('SELECT * FROM pages WHERE document_id=? AND page=?', (saved['id'], page)).fetchone()
        return {**dict(row), 'flags': analysis['flags'], 'skipped': False}

    def retry_ocr(self, ocr, document=None, page=None, notice_only=False):
        """Reprocess empty or image-bearing saved pages, preserving originals and prior OCR on failure."""
        from pypdf import PdfReader
        if not callable(ocr):
            raise ValueError('OCR処理を指定してください。')
        if page is not None and document is None:
            raise ValueError('ページを指定するときはPDFも指定してください。')
        query, args = 'SELECT * FROM pages WHERE 1=1', []
        if notice_only:
            query += ' AND notice_scope=1'
        if document is not None:
            query += ' AND document_id=?'
            args.append(document)
        if page is not None:
            query += ' AND page=?'
            args.append(page)
        selected = self.db.execute(query, args).fetchall()
        if document is not None and not selected:
            raise ValueError('指定されたPDFまたはページがありません。')
        counts = {'processed': 0, 'succeeded': 0, 'empty': 0, 'failed': 0}
        readers = {}
        for saved in selected:
            if saved['text'].strip() and saved['has_images'] == 0:
                continue
            path = self.folder / 'pdf' / (saved['document_id'] + '.pdf')
            original = saved['extracted_text'] or (saved['text'] if not saved['ocr_text'] else '')
            recognized, images, text = saved['ocr_text'], saved['has_images'], saved['text']
            try:
                if saved['document_id'] not in readers:
                    try:
                        if not path.is_file():
                            raise ValueError('保存された原文PDFがありません。')
                        raw = path.read_bytes()
                        if hashlib.sha256(raw).hexdigest() != saved['document_id']:
                            raise ValueError('保存された原文PDFの内容が変更されています。')
                        reader = PdfReader(io.BytesIO(raw))
                        ensure_readable(reader)
                        readers[saved['document_id']] = reader
                    except Exception as error:
                        readers[saved['document_id']] = error
                reader = readers[saved['document_id']]
                if isinstance(reader, Exception):
                    raise reader
                extracted = _extract_page(reader.pages[saved['page'] - 1])
                images = extracted['has_images']
                if saved['text'].strip() and images == 0:
                    continue
                if extracted['extracted_text'].strip():
                    original = extracted['extracted_text']
                new_ocr, issue, status = self._ocr_page(path, saved['page'], ocr)
                if status == 'succeeded':
                    recognized = new_ocr
                issue = _ocr_issue(original, issue, status, previous_ocr=saved['ocr_text'])
                if not extracted['extraction_ok']:
                    issue += ' / 元PDFの文字抽出失敗・以前の本文を保持'
                text = _merge_page_text(original, recognized)
            except Exception as error:
                issue, status = 'OCR失敗: ' + str(error) + ' / 既存本文を保持', 'failed'
            stamp = datetime.now().isoformat()
            with self.db:
                self.db.execute("UPDATE pages SET text=?,issue=?,extracted_text=?,ocr_text=?,has_images=?,"
                                "state='要確認',reviewed_at=NULL,machine_status=?,analysis_json=? "
                                'WHERE document_id=? AND page=?',
                                (text, issue, original, recognized, images, status,
                                 json.dumps({'status': 'needs_review', 'machine_status': status, 'text': text,
                                             'flags': [{'kind': 'reprocessed', 'reason': '再OCR後の原文確認が必要です。', 'bbox': None, 'text': ''}]}, ensure_ascii=False),
                                 saved['document_id'], saved['page']))
                self.db.execute('INSERT INTO history VALUES (?,?,?)', (stamp, 'OCR再処理', json.dumps({
                    'document': saved['document_id'], 'page': saved['page'], 'result': status,
                    'previous_state': saved['state'], 'previous_reviewed_at': saved['reviewed_at'],
                    'previous_issue': saved['issue'], 'state': '要確認', 'note': saved['note'],
                    'previous_ocr_text': saved['ocr_text'],
                    'issue': issue}, ensure_ascii=False)))
            counts['processed'] += 1
            counts[status] += 1
        return counts

    def documents(self):
        result = [dict(row) for row in self.db.execute('''SELECT d.*, COUNT(p.page) AS stored_pages,
            SUM(CASE WHEN p.notice_scope=0 THEN 1 ELSE 0 END) AS unscoped_pages,
            SUM(CASE WHEN p.notice_scope=1 THEN 1 ELSE 0 END) AS notice_pages
            FROM documents d LEFT JOIN pages p ON p.document_id=d.id
            GROUP BY d.id ORDER BY d.imported_at DESC,d.id''')]
        for row in result:
            row['scope_pages'] = json.loads(row['scope_pages_json'])
        return result

    def pages(self, notice_only=False):
        query = '''SELECT p.*,d.original_name,d.source_url,d.scope_status,d.scope_detail,
            d.processing_status,d.processing_detail FROM pages p JOIN documents d
            ON d.id=p.document_id'''
        if notice_only:
            query += ' WHERE p.notice_scope=1'
        rows = [dict(row) for row in self.db.execute(query + ' ORDER BY d.imported_at DESC,p.page')]
        for row in rows:
            row['flags'] = json.loads(row['analysis_json']).get('flags', [])
        return rows

    def review(self, document, page, state, note):
        if state not in ('未確認', '要確認', '確認済み'):
            raise ValueError('不正な確認状態')
        if state == '確認済み' and self.document(document)['scope_status'] == 'provisional':
            raise ValueError('公告範囲が暫定です。先に人が公告範囲を確定してください。')
        stamp = datetime.now().isoformat()
        note = '' if note is None else str(note)
        with self.db:
            updated = self.db.execute('UPDATE pages SET state=?,note=?,reviewed_at=? WHERE document_id=? AND page=?',
                                     (state, note, stamp, document, page))
            if updated.rowcount != 1:
                raise ValueError('指定されたページがありません。')
            self.db.execute('INSERT INTO history VALUES (?,?,?)', (stamp, '確認変更', json.dumps(
                {'document': document, 'page': page, 'state': state, 'note': note}, ensure_ascii=False)))

    def history(self, document=None, page=None):
        result = []
        for saved in self.db.execute('SELECT * FROM history ORDER BY rowid DESC'):
            row = dict(saved)
            try:
                row['details'] = json.loads(row['details'])
            except (ValueError, TypeError):
                pass
            details = row['details'] if isinstance(row['details'], dict) else {}
            if document is not None and details.get('document') != document:
                continue
            changed_pages = {change.get('page') for change in details.get('changes', [])
                             if isinstance(change, dict)}
            if page is not None and details.get('page') != page and page not in changed_pages:
                continue
            result.append(row)
        return result

    def export(self, path, notice_only=False):
        from ocr_review import match_with_review, update_ledger_flags
        def safe(value):
            value = str(value)
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
        ledger = self.ledger()
        with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['PDF名', 'ページ', '候補取引先コード', '候補会社名', '候補理由', '抽出状況', '確認状態', 'メモ', '公告範囲',
                             '範囲確定状態', '機械処理状態', '要確認理由', '出典URL'])
            for page in self.pages(notice_only=notice_only):
                analysis = json.loads(page['analysis_json'])
                matches = match_with_review(page['text'], ledger, analysis)
                flags = update_ledger_flags(page['text'], analysis, ledger, matches=matches).get('flags', [])
                for match in matches or [{'code': '', 'name': '', 'reason': '候補なし（該当なしの確定ではありません）'}]:
                    writer.writerow([safe(v) for v in [page['original_name'], page['page'], match['code'],
                        match['name'], match['reason'], page['issue'], page['state'], page['note'],
                        {1: '公告', 0: '範囲未設定', -1: '対象外'}[page['notice_scope']],
                        page['scope_status'], page['machine_status'], ' / '.join(str(flag.get('reason', '')) for flag in flags), page['source_url']]])
            # Documents without a usable scope/page must remain visible in the CSV.
            for document in self.documents():
                if document['scope_status'] == 'excluded':
                    continue
                if (document['scope_status'] != 'confirmed' or document['processing_status'] in ('archived', 'processing', 'cancelled', 'failed')
                        or document['notice_pages'] < len(document['scope_pages'])):
                    detail = document['processing_detail'] or document['scope_detail'] or '公告範囲・処理結果を人が確認してください。'
                    writer.writerow([safe(v) for v in [document['original_name'], '', '', '', '文書全体を要確認',
                        document['processing_status'], '要確認', '', '文書保留',
                        document['scope_status'], '', detail, document['source_url']]])

    def backup(self):
        folder = self.folder / 'backups'
        folder.mkdir(exist_ok=True)
        path = folder / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.sqlite3')
        with closing(sqlite3.connect(path)) as target:
            self.db.backup(target)
        return path

    def backup_bundle(self):
        """Create a database snapshot and all of its source PDFs in one restorable ZIP."""
        folder = self.folder / 'backups'
        folder.mkdir(exist_ok=True)
        path = folder / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.zip')
        temporary = path.with_suffix('.tmp')
        try:
            with tempfile.TemporaryDirectory(dir=folder) as workspace:
                snapshot = Path(workspace) / 'kanpo.sqlite3'
                with closing(sqlite3.connect(snapshot)) as target:
                    self.db.backup(target)
                    documents = target.execute('SELECT id FROM documents ORDER BY id').fetchall()
                manifest = {'format': 1, 'created_at': datetime.now().isoformat(), 'files': {}}
                with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.write(snapshot, 'kanpo.sqlite3')
                    manifest['files']['kanpo.sqlite3'] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
                    for (document,) in documents:
                        pdf = self.folder / 'pdf' / (document + '.pdf')
                        if pdf.resolve().parent != (self.folder / 'pdf').resolve() or not pdf.is_file():
                            raise ValueError('完全バックアップを作成できません。保存PDFがありません: ' + document)
                        checksum = hashlib.sha256(pdf.read_bytes()).hexdigest()
                        if checksum != document:
                            raise ValueError('完全バックアップを作成できません。保存PDFの内容が変更されています: ' + document)
                        name = 'pdf/' + pdf.name
                        archive.write(pdf, name)
                        manifest['files'][name] = checksum
                    archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path
