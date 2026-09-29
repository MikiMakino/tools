import csv
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
from core import Store, normalize, match_candidates, read_ledger


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'data')

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_normalization(self):
        self.assertEqual(normalize('㈱ ＡＢＣ'), normalize('株式会社ABC'))
        self.assertNotEqual(normalize('株式会社ABC'), normalize('ABC株式会社'))
        self.assertEqual(normalize(0), '0')

    def test_empty_normalized_name_is_never_a_candidate(self):
        self.assertEqual(match_candidates('株式会社ABC', [{'code': '1', 'name': '\u3000', 'address': ''}]), [])

    def test_xlsx_uses_first_sheet_and_preserves_numeric_zero(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(['取引先コード', '会社名', '住所'])
        book.active.append([0, '株式会社ゼロ', None])
        book.create_sheet('別シート').append(['対象外'])
        book.active = 1
        path = self.root / 'ledger.xlsx'
        book.save(path)
        book.close()
        self.assertEqual(read_ledger(path), [{'code': '0', 'name': '株式会社ゼロ', 'address': ''}])

    def test_same_name_companies_remain_separate_candidates(self):
        rows = [{'code':'1','name':'株式会社ABC','address':'東京都'},
                {'code':'2','name':'株式会社ABC','address':'大阪府'}]
        matches = match_candidates('株式会社ABC 東京都',rows)
        self.assertEqual(len(matches),2)
        self.assertIn('社名・住所',matches[0]['reason'])
        self.assertIn('要原文確認',matches[1]['reason'])

    def test_invalid_ledger_does_not_replace_data(self):
        original = [{'code':'001','name':'A','address':''}]
        self.store.replace_ledger(original)
        path = self.root / 'bad.csv'
        path.write_text('取引先コード,会社名\n001,A\n001,B\n',encoding='utf-8-sig')
        with self.assertRaises(ValueError): read_ledger(path)
        self.assertEqual(self.store.ledger(), original)

    def test_replace_ledger_validates_before_changing_any_data(self):
        original = [{'code': '001', 'name': 'A', 'address': ''}]
        self.store.replace_ledger(original)
        self.add_page()
        self.store.review('id', 1, '確認済み', '保持するメモ')
        history = self.store.history()
        for rows in ([], None, [{'code': '1', 'name': '　'}],
                     [{'code': '1', 'name': 'A'}, {'code': '1', 'name': 'B'}],
                     [{'name': 'A'}], ['not a row']):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    self.store.replace_ledger(rows)
                self.assertEqual(self.store.ledger(), original)
                self.assertEqual(self.store.pages()[0]['state'], '確認済み')
                self.assertEqual(self.store.history(), history)

    def test_duplicate_ledger_header_is_rejected(self):
        path = self.root / 'duplicate.csv'
        path.write_text('取引先コード,会社名,会社名\n1,A,B\n', encoding='utf-8-sig')
        with self.assertRaises(ValueError):
            read_ledger(path)

    def test_replace_ledger_accepts_generator_and_missing_address(self):
        self.store.replace_ledger(row for row in [{'code': 0, 'name': ' A '}])
        self.assertEqual(self.store.ledger(), [{'code': '0', 'name': 'A', 'address': ''}])

    def test_cp932_and_leading_zero(self):
        path = self.root / 'ledger.csv'
        path.write_bytes('取引先コード,会社名,住所\n001,株式会社テスト,東京都\n'.encode('cp932'))
        self.assertEqual(read_ledger(path)[0]['code'],'001')

    def add_page(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO documents(id,original_name,imported_at,page_count) VALUES (?,?,?,?)',
                                  ('id','test.pdf','2026-09-29',1))
            self.store.db.execute('INSERT INTO pages(document_id,page,text,issue) VALUES (?,?,?,?)',
                                  ('id',1,'株式会社ABC','文字抽出済み'))

    def test_review_persists_and_ledger_change_reopens(self):
        self.add_page()
        self.store.review('id',1,'確認済み','原文確認')
        reopened = Store(self.root / 'data')
        self.assertEqual(reopened.pages()[0]['state'],'確認済み')
        reopened.db.close()
        self.store.replace_ledger([{'code':'1','name':'株式会社ABC','address':''}])
        self.assertEqual(self.store.pages()[0]['state'],'未確認')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM history').fetchone()[0],2)

    def test_csv_formula_escaping_and_backup(self):
        self.add_page()
        self.store.review('id',1,'要確認','=1+1')
        path = self.root / 'result.csv'
        self.store.export(path)
        with path.open(encoding='utf-8-sig') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[1][rows[0].index('メモ')],"'=1+1")
        backup = self.store.backup()
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM pages').fetchone()[0],1)
        # A released connection is necessary for copying/removing this file on Windows.
        backup.unlink()

    def make_pdf(self, pages=1):
        from pypdf import PdfWriter
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=595, height=842)
        path = self.root / 'blank.pdf'
        with path.open('wb') as stream:
            writer.write(stream)
        writer.close()
        return path

    def make_image_pdf(self, *, nested=False, inline=False, password=None):
        from pypdf import PdfWriter
        from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=595, height=842)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        resources = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        content = b'BT /F1 12 Tf 10 800 Td (Kanpo header 2026) Tj ET\n'
        if inline:
            content += b'q 100 0 0 100 10 10 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID \x00\x00\x00 EI Q\n'
        else:
            picture = DecodedStreamObject()
            picture.set_data(b'\x00\x00\x00')
            picture.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Image'),
                            NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
                            NameObject('/BitsPerComponent'): NumberObject(8), NameObject('/ColorSpace'): NameObject('/DeviceRGB')})
            xobjects = DictionaryObject({NameObject('/Im1'): writer._add_object(picture)})
            if nested:
                form = DecodedStreamObject()
                form.set_data(b'/Im1 Do')
                form.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Form'),
                             NameObject('/BBox'): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(1), NumberObject(1)]),
                             NameObject('/Resources'): DictionaryObject({NameObject('/XObject'): xobjects})})
                resources[NameObject('/XObject')] = DictionaryObject({NameObject('/Fm1'): writer._add_object(form)})
                content += b'q 100 0 0 100 10 10 cm /Fm1 Do Q\n'
            else:
                resources[NameObject('/XObject')] = xobjects
                content += b'q 100 0 0 100 10 10 cm /Im1 Do Q\n'
        stream = DecodedStreamObject()
        stream.set_data(content)
        page[NameObject('/Resources')] = resources
        page[NameObject('/Contents')] = writer._add_object(stream)
        if password is not None:
            writer.encrypt(user_password=password, owner_password='synthetic-owner-only')
        path = self.root / 'header-and-image.pdf'
        with path.open('wb') as output:
            writer.write(output)
        writer.close()
        return path

    def test_image_body_is_ocrd_even_when_header_text_exists(self):
        path = self.make_image_pdf()
        callback = Mock(return_value='株式会社画像本文 東京都')
        self.store.import_pdf(path, callback, selected_pages=[1])
        callback.assert_called_once_with(path, 1)
        row = self.store.pages()[0]
        self.assertIn('Kanpo header', row['extracted_text'])
        self.assertEqual(row['ocr_text'], '株式会社画像本文 東京都')
        self.assertIn('Kanpo header', row['text'])
        self.assertIn('株式会社画像本文', row['text'])
        self.assertEqual(row['has_images'], 1)
        self.assertIn('OCR補足済み', row['issue'])
        matches = match_candidates(row['text'], [{'code': '1', 'name': '株式会社画像本文', 'address': '東京都'}])
        self.assertEqual(len(matches), 1)

    def test_images_in_forms_and_inline_images_are_detected_without_ocr(self):
        for options in ({}, {'nested': True}, {'inline': True}):
            with self.subTest(options=options):
                path = self.make_image_pdf(**options)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.store.import_pdf(path)
                row = next(row for row in self.store.pages() if row['document_id'] == digest)
                self.assertEqual(row['has_images'], 1)
                self.assertIn('画像本文未OCR', row['issue'])
                self.assertIn('Kanpo header', row['text'])

    def test_image_ocr_failure_or_empty_result_preserves_extracted_header(self):
        for index, outcome in enumerate(('', RuntimeError('認識失敗'))):
            with self.subTest(outcome=outcome):
                store = Store(self.root / f'ocr-variant-{index}')
                try:
                    store.import_pdf(self.make_image_pdf(), Mock(side_effect=[outcome]))
                    row = store.pages()[0]
                    self.assertIn('Kanpo header', row['text'])
                    self.assertEqual(row['ocr_text'], '')
                    self.assertIn('元の抽出本文を保持', row['issue'])
                    self.assertIn('OCR文字なし' if index == 0 else 'OCR失敗', row['issue'])
                finally:
                    store.db.close()

    def test_image_retry_replaces_ocr_without_growth_and_failure_keeps_previous_body(self):
        path = self.make_image_pdf()
        self.store.import_pdf(path, lambda pdf, number: '旧OCR本文')
        document = self.store.pages()[0]['document_id']
        self.store.review(document, 1, '確認済み', '保持する確認メモ')
        callback = Mock(return_value='新OCR本文')
        self.assertEqual(self.store.retry_ocr(callback)['succeeded'], 1)
        row = self.store.pages()[0]
        self.assertIn('Kanpo header', row['text'])
        self.assertIn('新OCR本文', row['text'])
        self.assertNotIn('旧OCR本文', row['text'])
        self.assertEqual((row['state'], row['note']), ('要確認', '保持する確認メモ'))
        self.assertEqual(self.store.history(document, 1)[0]['details']['previous_ocr_text'], '旧OCR本文')
        expected = row['text']
        self.store.retry_ocr(callback)
        self.assertEqual(self.store.pages()[0]['text'], expected)
        self.assertEqual(expected.count('新OCR本文'), 1)
        for outcome in (RuntimeError('認識失敗'), ''):
            result = self.store.retry_ocr(Mock(side_effect=[outcome]))
            self.assertEqual(result['processed'], 1)
            row = self.store.pages()[0]
            self.assertEqual(row['text'], expected)
            self.assertEqual(row['ocr_text'], '新OCR本文')
            self.assertIn('既存OCR本文を保持', row['issue'])

    def test_retry_discovers_images_in_previously_imported_header_only_page(self):
        self.store.import_pdf(self.make_image_pdf())
        with self.store.db:
            self.store.db.execute('UPDATE pages SET has_images=-1')
        result = self.store.retry_ocr(lambda pdf, number: '追加された画像本文')
        self.assertEqual(result['succeeded'], 1)
        self.assertIn('追加された画像本文', self.store.pages()[0]['text'])
        self.assertEqual(self.store.pages()[0]['has_images'], 1)

    def test_empty_password_pdf_is_readable_and_original_bytes_stay_encrypted(self):
        from pypdf import PdfReader
        import io
        path = self.make_image_pdf(password='')
        original = path.read_bytes()
        self.assertTrue(PdfReader(io.BytesIO(original)).is_encrypted)
        self.store.import_pdf(path, lambda pdf, number: '画像の公告本文', selected_pages=[1])
        row = self.store.pages()[0]
        self.assertIn('Kanpo header', row['text'])
        self.assertIn('画像の公告本文', row['text'])
        saved = self.store.folder / 'pdf' / (row['document_id'] + '.pdf')
        self.assertEqual(saved.read_bytes(), original)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(PdfReader(io.BytesIO(saved.read_bytes())).is_encrypted)
        self.assertEqual(self.store.retry_ocr(lambda pdf, number: '再認識本文')['succeeded'], 1)

    def test_nonempty_password_pdf_is_not_unlocked(self):
        with self.assertRaises(ValueError):
            self.store.import_pdf(self.make_image_pdf(password='required-password'))
        self.assertEqual(self.store.pages(), [])
        self.assertEqual(list((self.store.folder / 'pdf').iterdir()), [])

    def test_blank_pdf_kept_for_review_and_deduplicated(self):
        path = self.make_pdf()
        self.assertTrue(self.store.import_pdf(path))
        self.assertFalse(self.store.import_pdf(path))
        page = self.store.pages()[0]
        self.assertIn('文字なし', page['issue'])
        self.assertEqual(page['state'], '未確認')

    def test_selected_pages_only_are_extracted_with_original_page_numbers(self):
        path = self.make_pdf(4)
        original_bytes = path.read_bytes()
        callback = Mock(side_effect=lambda pdf, number: f'公告 {number}')
        with patch('pypdf._page.PageObject.extract_text', return_value='') as extract:
            self.assertTrue(self.store.import_pdf(path, callback, selected_pages=[4, 2]))
        self.assertEqual(extract.call_count, 2)
        self.assertEqual([call.args[1] for call in callback.call_args_list], [2, 4])
        rows = self.store.pages(notice_only=True)
        self.assertEqual([row['page'] for row in rows], [2, 4])
        self.assertEqual([row['notice_scope'] for row in rows], [1, 1])
        document = self.store.documents()[0]
        self.assertEqual(document['page_count'], 4)
        self.assertEqual(document['stored_pages'], 2)
        self.assertEqual(document['notice_pages'], 2)
        self.assertEqual(document['unscoped_pages'], 0)
        self.assertEqual((self.store.folder / 'pdf' / (document['id'] + '.pdf')).read_bytes(), original_bytes)
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_invalid_selected_pages_leave_all_data_unchanged(self):
        path = self.make_pdf(3)
        self.store.import_pdf(path, selected_pages=[1])
        before_pages = [dict(row) for row in self.store.pages()]
        before_documents = self.store.documents()
        before_history = self.store.history()
        for selection in ([], [1, 1], [0], [-1], [4], ['1'], [1.0], [True], [None],
                          '1', b'1', {1: True}, 1, False):
            with self.subTest(selection=selection):
                callback = Mock()
                with self.assertRaises(ValueError):
                    self.store.import_pdf(path, callback, source_url='invalid-change', selected_pages=selection)
                callback.assert_not_called()
                self.assertEqual([dict(row) for row in self.store.pages()], before_pages)
                self.assertEqual(self.store.documents(), before_documents)
                self.assertEqual(self.store.history(), before_history)
        self.assertEqual(len(list((self.store.folder / 'pdf').iterdir())), 1)

    def test_reimport_adds_pages_and_replaces_scope_without_losing_reviews(self):
        path = self.make_pdf(4)
        self.store.import_pdf(path, selected_pages=[1, 3])
        document = self.store.documents()[0]['id']
        self.store.review(document, 1, '確認済み', '保持する1')
        self.store.review(document, 3, '要確認', '保持する3')
        before = {row['page']: dict(row) for row in self.store.pages()}
        history = self.store.history()
        saved_path = self.store.folder / 'pdf' / (document + '.pdf')
        original_mtime = saved_path.stat().st_mtime_ns
        callback = Mock(return_value='裁判所 破産公告')
        self.assertTrue(self.store.import_pdf(saved_path, callback, selected_pages=[1, 2]))
        callback.assert_called_once_with(saved_path, 2)
        rows = {row['page']: dict(row) for row in self.store.pages()}
        self.assertEqual({number: row['notice_scope'] for number, row in rows.items()}, {1: 1, 2: 1, 3: -1})
        for number in (1, 3):
            for key in ('text', 'issue', 'state', 'note', 'reviewed_at'):
                self.assertEqual(rows[number][key], before[number][key])
        self.assertEqual(self.store.documents()[0]['original_name'], 'blank.pdf')
        self.assertEqual(self.store.documents()[0]['page_count'], 4)
        self.assertEqual(saved_path.stat().st_mtime_ns, original_mtime)
        self.assertEqual(self.store.history()[1:], history)
        change = self.store.history(document, 3)[0]
        self.assertEqual(change['action'], '公告範囲変更')
        self.assertIn({'page': 3, 'before': 1, 'after': -1}, change['details']['changes'])
        callback.reset_mock()
        self.assertFalse(self.store.import_pdf(saved_path, callback, selected_pages=[3]))
        callback.assert_not_called()
        restored = self.store.pages(notice_only=True)[0]
        self.assertEqual((restored['page'], restored['state'], restored['note']), (3, '要確認', '保持する3'))

    def test_identical_selection_does_not_create_extra_history(self):
        path = self.make_pdf(2)
        self.store.import_pdf(path, selected_pages=[1])
        history = self.store.history()
        callback = Mock()
        self.assertFalse(self.store.import_pdf(path, callback, selected_pages=[1]))
        self.assertEqual(self.store.history(), history)
        callback.assert_not_called()

    def test_unspecified_selection_keeps_legacy_import_behavior(self):
        path = self.make_pdf(3)
        self.store.import_pdf(path, selected_pages=[2])
        self.assertTrue(self.store.import_pdf(path))
        self.assertEqual({row['page']: row['notice_scope'] for row in self.store.pages()}, {1: 0, 2: 1, 3: 0})
        self.assertEqual(self.store.documents()[0]['unscoped_pages'], 2)
        self.assertEqual([row['page'] for row in self.store.pages(notice_only=True)], [2])

    def test_notice_export_and_retry_ocr_exclude_out_of_scope_pages(self):
        path = self.make_pdf(3)
        self.store.import_pdf(path)
        document = self.store.documents()[0]['id']
        self.store.review(document, 1, '確認済み', '対象外でも保持')
        self.store.import_pdf(path, selected_pages=[2])
        callback = Mock(return_value='裁判所 破産公告')
        self.assertEqual(self.store.retry_ocr(callback, notice_only=True)['processed'], 1)
        self.assertEqual(callback.call_args.args[1], 2)
        self.assertEqual(self.store.pages()[0]['state'], '確認済み')
        output = self.root / 'notices.csv'
        self.store.export(output, notice_only=True)
        with output.open(encoding='utf-8-sig') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][rows[0].index('ページ')], '2')
        self.assertEqual(rows[1][rows[0].index('公告範囲')], '公告')
        self.store.export(output)
        with output.open(encoding='utf-8-sig') as stream:
            self.assertEqual(len(list(csv.reader(stream))), 4)

    def test_reimport_does_not_overwrite_damaged_saved_original(self):
        path = self.make_pdf(2)
        self.store.import_pdf(path, selected_pages=[1])
        document = self.store.documents()[0]['id']
        saved = self.store.folder / 'pdf' / (document + '.pdf')
        saved.write_bytes(b'changed externally')
        history = self.store.history()
        with self.assertRaisesRegex(ValueError, '変更'):
            self.store.import_pdf(path, selected_pages=[2])
        self.assertEqual(saved.read_bytes(), b'changed externally')
        self.assertEqual([row['page'] for row in self.store.pages()], [1])
        self.assertEqual(self.store.history(), history)

    def test_ocr_import_records_success_empty_and_failure(self):
        path = self.make_pdf(3)
        callback = Mock(side_effect=['株式会社ABC', '　', RuntimeError('処理失敗')])
        self.assertTrue(self.store.import_pdf(path, callback))
        pages = self.store.pages()
        self.assertEqual(callback.call_args_list[0].args, (path, 1))
        self.assertEqual(callback.call_args_list[-1].args, (path, 3))
        self.assertEqual(pages[0]['text'], '株式会社ABC')
        self.assertIn('OCR抽出済み', pages[0]['issue'])
        self.assertIn('OCR文字なし', pages[1]['issue'])
        self.assertIn('OCR失敗', pages[2]['issue'])
        self.assertEqual([row['state'] for row in pages], ['未確認'] * 3)

    def test_ocr_not_called_on_extractable_text(self):
        path = self.make_pdf()
        callback = Mock()
        with patch('pypdf._page.PageObject.extract_text', return_value='株式会社ABC'):
            self.store.import_pdf(path, callback)
        callback.assert_not_called()
        self.assertEqual(self.store.pages()[0]['text'], '株式会社ABC')

    def test_ocr_retries_extract_failure_and_invalid_result_is_visible(self):
        path = self.make_pdf()
        with patch('pypdf._page.PageObject.extract_text', side_effect=RuntimeError('抽出不可')):
            self.store.import_pdf(path, lambda pdf, page: None)
        self.assertIn('OCR失敗', self.store.pages()[0]['issue'])
        self.assertIn('文字列', self.store.pages()[0]['issue'])

    def test_retry_ocr_reopens_review_and_keeps_history_and_note(self):
        self.store.import_pdf(self.make_pdf())
        document = self.store.pages()[0]['document_id']
        self.store.review(document, 1, '確認済み', '原文のメモ\n')
        callback = Mock(return_value='株式会社ABC')
        result = self.store.retry_ocr(callback, document, 1)
        self.assertEqual(result, {'processed': 1, 'succeeded': 1, 'empty': 0, 'failed': 0})
        callback.assert_called_once_with(self.store.folder / 'pdf' / (document + '.pdf'), 1)
        row = self.store.pages()[0]
        self.assertEqual(row['state'], '要確認')
        self.assertIsNone(row['reviewed_at'])
        self.assertEqual(row['note'], '原文のメモ\n')
        history = self.store.history(document, 1)
        self.assertEqual([row['action'] for row in history], ['OCR再処理', '確認変更'])
        self.assertEqual(history[0]['details']['previous_state'], '確認済み')
        self.assertEqual(history[1]['details']['state'], '確認済み')
        self.assertEqual(self.store.retry_ocr(callback)['processed'], 0)
        self.assertEqual(callback.call_count, 1)

    def test_retry_ocr_failure_and_empty_still_reopen_reviews(self):
        self.store.import_pdf(self.make_pdf(2))
        document = self.store.pages()[0]['document_id']
        for page in (1, 2):
            self.store.review(document, page, '確認済み', '')
        result = self.store.retry_ocr(Mock(side_effect=['', RuntimeError('失敗')]))
        self.assertEqual(result, {'processed': 2, 'succeeded': 0, 'empty': 1, 'failed': 1})
        self.assertEqual([row['state'] for row in self.store.pages()], ['要確認', '要確認'])
        with self.assertRaises(ValueError):
            self.store.retry_ocr(lambda p, n: '', document, 99)
        with self.assertRaises(ValueError):
            self.store.retry_ocr(lambda p, n: '', page=1)

    def test_missing_pdf_is_reported_as_ocr_failure(self):
        self.store.import_pdf(self.make_pdf())
        document = self.store.pages()[0]['document_id']
        (self.store.folder / 'pdf' / (document + '.pdf')).unlink()
        callback = Mock()
        result = self.store.retry_ocr(callback)
        self.assertEqual(result['failed'], 1)
        callback.assert_not_called()
        self.assertIn('原文PDFがありません', self.store.pages()[0]['issue'])

    def test_review_nonexistent_page_does_not_create_history(self):
        with self.assertRaises(ValueError):
            self.store.review('missing', 1, '確認済み', '')
        self.assertEqual(self.store.history(), [])

    def test_source_url_attaches_to_duplicate_without_changing_review(self):
        path = self.make_pdf()
        self.store.import_pdf(path)
        document = self.store.pages()[0]['document_id']
        self.store.review(document, 1, '確認済み', '確認した')
        url = 'https://example.test/official.pdf'
        self.assertFalse(self.store.has_source(url))
        self.assertFalse(self.store.import_pdf(path, source_url=url))
        self.assertTrue(self.store.has_source(url))
        self.assertFalse(self.store.has_source(''))
        self.assertEqual(self.store.pages()[0]['state'], '確認済み')
        export = self.root / 'source.csv'
        self.store.export(export)
        with export.open(encoding='utf-8-sig') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[0][-1], '出典URL')
        self.assertEqual(rows[1][-1], url)

    def test_legacy_database_gets_source_column_without_losing_data(self):
        folder = self.root / 'legacy'
        folder.mkdir()
        with closing(sqlite3.connect(folder / 'kanpo.sqlite3')) as db:
            db.execute('CREATE TABLE documents(id TEXT PRIMARY KEY, original_name TEXT NOT NULL, '
                       'imported_at TEXT NOT NULL, page_count INTEGER NOT NULL)')
            db.execute("INSERT INTO documents VALUES ('legacy','old.pdf','2026-01-01',1)")
            db.execute('CREATE TABLE pages(document_id TEXT NOT NULL,page INTEGER NOT NULL,text TEXT NOT NULL,'
                       'issue TEXT NOT NULL,state TEXT NOT NULL,note TEXT NOT NULL,reviewed_at TEXT,'
                       'PRIMARY KEY(document_id,page))')
            db.execute("INSERT INTO pages VALUES ('legacy',1,'旧本文','文字抽出済み','確認済み','旧メモ','2026-01-02')")
            db.commit()
        legacy = Store(folder)
        try:
            row = legacy.db.execute('SELECT * FROM documents').fetchone()
            self.assertEqual(row['id'], 'legacy')
            self.assertEqual(row['source_url'], '')
            page = legacy.pages()[0]
            self.assertEqual((page['notice_scope'], page['state'], page['note']), (0, '確認済み', '旧メモ'))
            self.assertEqual(page['extracted_text'], '旧本文')
            self.assertEqual(page['ocr_text'], '')
            self.assertEqual(page['has_images'], -1)
            self.assertEqual(legacy.pages(notice_only=True), [])
            self.assertEqual(legacy.documents()[0]['unscoped_pages'], 1)
        finally:
            legacy.db.close()

    def test_backup_bundle_restores_database_and_original_pdf(self):
        path = self.make_pdf(3)
        self.store.import_pdf(path, source_url='https://example.test/a.pdf', selected_pages=[1, 3])
        self.store.replace_ledger([{'code': '001', 'name': '会社A'}])
        document = self.store.pages()[0]['document_id']
        self.store.review(document, 1, '確認済み', 'メモ')
        bundle = self.store.backup_bundle()
        restored_folder = self.root / 'restored'
        with zipfile.ZipFile(bundle) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            for name, digest in manifest['files'].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)
            archive.extractall(restored_folder)
        restored = Store(restored_folder)
        try:
            self.assertEqual(restored.ledger(), self.store.ledger())
            self.assertEqual(dict(restored.pages()[0]), dict(self.store.pages()[0]))
            self.assertEqual(restored.documents(), self.store.documents())
            self.assertEqual([row['page'] for row in restored.pages(notice_only=True)], [1, 3])
            self.assertEqual(restored.history(), self.store.history())
            self.assertEqual((restored_folder / 'pdf' / (document + '.pdf')).read_bytes(), path.read_bytes())
        finally:
            restored.db.close()

    def test_backup_bundle_refuses_missing_or_changed_pdf(self):
        self.store.import_pdf(self.make_pdf())
        document = self.store.pages()[0]['document_id']
        pdf = self.store.folder / 'pdf' / (document + '.pdf')
        pdf.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '変更'):
            self.store.backup_bundle()
        pdf.unlink()
        with self.assertRaisesRegex(ValueError, 'ありません'):
            self.store.backup_bundle()
        self.assertEqual(list((self.store.folder / 'backups').iterdir()), [])

    def test_import_failure_rolls_back_rows_and_temporary_files(self):
        path = self.make_pdf()
        with patch.object(Path, 'replace', side_effect=OSError('保存失敗')):
            with self.assertRaises(OSError):
                self.store.import_pdf(path)
        self.assertEqual(len(self.store.pages()), 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 0)
        self.assertEqual(list((self.store.folder / 'pdf').iterdir()), [])

    def test_invalid_pdf_not_registered(self):
        path = self.root / 'invalid.pdf'
        path.write_bytes(b'not a pdf')
        with self.assertRaises(Exception): self.store.import_pdf(path)
        self.assertEqual(len(self.store.pages()),0)


if __name__ == '__main__': unittest.main()
