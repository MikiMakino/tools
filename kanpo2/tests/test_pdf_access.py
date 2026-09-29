import io
from pathlib import Path
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter

from pdf_access import ensure_readable


class PDFAccessTests(unittest.TestCase):
    @staticmethod
    def make_aes256_pdf(path, password):
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.add_metadata({'/Title': 'Synthetic AES-256 validation'})
        writer.encrypt(user_password=password, owner_password='synthetic-owner-only',
                       algorithm='AES-256')
        writer.write(str(path))
        writer.close()

    def test_empty_password_aes256_is_readable_without_changing_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'public-aes256.pdf'
            self.make_aes256_pdf(path, '')
            original = path.read_bytes()
            reader = PdfReader(io.BytesIO(original))
            encryption = reader.trailer['/Encrypt']
            self.assertEqual((encryption['/V'], encryption['/R'], encryption['/Length']),
                             (5, 6, 256))

            ensure_readable(reader)

            self.assertEqual(len(reader.pages), 1)
            self.assertEqual(reader.pages[0].mediabox.width, 595)
            self.assertEqual(reader.metadata.title, 'Synthetic AES-256 validation')
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(PdfReader(io.BytesIO(path.read_bytes())).is_encrypted)

    def test_nonempty_password_aes256_is_rejected_without_changing_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'password-required-aes256.pdf'
            self.make_aes256_pdf(path, 'synthetic-required-password')
            original = path.read_bytes()
            reader = PdfReader(io.BytesIO(original))
            self.assertEqual(reader.trailer['/Encrypt']['/R'], 6)

            with self.assertRaisesRegex(ValueError, '閲覧パスワードが必要'):
                ensure_readable(reader)

            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(PdfReader(io.BytesIO(path.read_bytes())).is_encrypted)
            # Rejection did not modify or damage the document: its supplied
            # password still opens the original bytes through normal reading.
            reopened = PdfReader(io.BytesIO(original))
            self.assertTrue(reopened.decrypt('synthetic-required-password'))
            self.assertEqual(len(reopened.pages), 1)
            self.assertEqual(reopened.metadata.title, 'Synthetic AES-256 validation')


if __name__ == '__main__':
    unittest.main()
