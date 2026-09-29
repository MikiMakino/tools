from pathlib import Path
import tempfile
import unittest
from pypdf import PdfWriter
from core import Store


class ScopeExclusionTests(unittest.TestCase):
    def test_whole_pdf_exclusion_preserves_reviews_and_can_be_reversed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            writer.add_blank_page(width=595, height=842)
            path = root / 'test.pdf'
            writer.write(path)
            store = Store(root / 'data')
            try:
                store.import_pdf(path, selected_pages=[1, 2])
                document = store.documents()[0]['id']
                store.review(document, 1, '確認済み', '保持するメモ')
                store.exclude_document_from_notices(document)
                self.assertEqual(store.pages(notice_only=True), [])
                self.assertEqual(store.pages()[0]['note'], '保持するメモ')
                self.assertEqual(store.pages()[0]['state'], '確認済み')
                self.assertTrue((store.folder / 'pdf' / (document + '.pdf')).is_file())
                store.import_pdf(path, selected_pages=[1])
                self.assertEqual([row['page'] for row in store.pages(notice_only=True)], [1])
                self.assertEqual(store.pages(notice_only=True)[0]['note'], '保持するメモ')
                with self.assertRaises(ValueError):
                    store.exclude_document_from_notices('missing')
            finally:
                store.db.close()
