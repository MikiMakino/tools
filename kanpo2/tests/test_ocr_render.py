from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import windows_ocr


class OCRRenderTests(unittest.TestCase):
    def test_preview_path_is_data_and_existing_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "確認 &'$.png"
            def request(path, page, **options):
                self.assertEqual(options['mode'], 'render')
                self.assertEqual(options['output'], str(output))
                output.write_bytes(b'\x89PNG\r\n\x1a\n')
                return {'ok': True}
            with patch('windows_ocr._pdf_request', side_effect=request) as invoke:
                self.assertEqual(windows_ocr.render_pdf_page(Path('unused.pdf'), 1, output), output)
                with self.assertRaises(ValueError):
                    windows_ocr.render_pdf_page(Path('unused.pdf'), 1, output)
                self.assertEqual(invoke.call_count, 1)
            self.assertEqual(output.read_bytes(), b'\x89PNG\r\n\x1a\n')

    def test_non_png_or_relative_preview_path_is_rejected_before_pdf_read(self):
        with patch('windows_ocr._pdf_request') as invoke:
            for output in (Path('relative.png'), Path('C:/test/preview.pdf')):
                with self.assertRaises(ValueError):
                    windows_ocr.render_pdf_page(Path('unused.pdf'), 1, output)
            invoke.assert_not_called()

    def test_missing_preview_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder, patch('windows_ocr._pdf_request', return_value={'ok': True}):
            with self.assertRaises(windows_ocr.OCRError):
                windows_ocr.render_pdf_page(Path('unused.pdf'), 1, Path(folder)/'missing.png')

    def test_layout_timeout_is_bounded(self):
        with patch('windows_ocr._pdf_request') as invoke:
            for timeout in (0, -1, 181, float('nan'), True):
                with self.assertRaises(ValueError):
                    windows_ocr.ocr_pdf_layout(Path('unused.pdf'), 1, timeout=timeout)
            invoke.assert_not_called()


if __name__ == '__main__':
    unittest.main()
