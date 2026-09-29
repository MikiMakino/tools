import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import windows_ocr
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject


class OCRTests(unittest.TestCase):
    def assert_box(self, actual, expected):
        for key in ('x', 'y', 'width', 'height'):
            self.assertAlmostEqual(actual[key], expected[key])

    def test_rejects_invalid_page_and_relative_path_before_starting_process(self):
        for page in (0, -1, 1.5, True, '1'):
            with self.subTest(page=page), self.assertRaises(ValueError):
                windows_ocr.ocr_pdf_page(Path('missing.pdf'), page)
        with self.assertRaises(ValueError):
            windows_ocr.ocr_pdf_page(Path('relative.pdf'), 1)

    def test_path_is_json_data_and_never_a_shell_command(self):
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / "取引先 & '(test) $.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            writer.add_blank_page(width=595, height=842)
            writer.write(str(pdf))
            with patch('windows_ocr._invoke', return_value={'text': '株式会社テスト'}) as invoke:
                self.assertEqual(windows_ocr.ocr_pdf_page(pdf, 2), '株式会社テスト')
                request = invoke.call_args.args[0]
                self.assertEqual(request, {'mode': 'page', 'path': str(pdf.resolve()), 'page': 2})

    def test_unavailable_is_reported_without_raising(self):
        with patch('windows_ocr._invoke', side_effect=windows_ocr.OCRError('利用不可')):
            result = windows_ocr.availability()
        self.assertFalse(result['available'])
        self.assertIn('利用不可', result['message'])

    def test_timeout_and_invalid_response_are_not_empty_success(self):
        with patch('windows_ocr.sys.platform', 'win32'), patch.object(Path, 'is_file', return_value=True):
            with patch('windows_ocr.subprocess.run', side_effect=subprocess.TimeoutExpired('powershell', 1)):
                with self.assertRaises(windows_ocr.OCRError):
                    windows_ocr._invoke({'mode': 'availability'}, 1)
            for payload in (b'not json', b'[]', json.dumps({'ok': False, 'error': 'failure'}).encode()):
                result = subprocess.CompletedProcess([], 0, payload, b'')
                with patch('windows_ocr.subprocess.run', return_value=result), self.assertRaises(windows_ocr.OCRError):
                    windows_ocr._invoke({'mode': 'availability'}, 1)

    def test_process_uses_argument_array_and_utf8_json_input(self):
        result = subprocess.CompletedProcess([], 0, b'{"ok":true,"text":"recognized"}', b'')
        request = {'mode': 'page', 'path': r"C:\work\name'&$(evil).pdf", 'page': 1}
        with patch('windows_ocr.sys.platform', 'win32'), patch.object(Path, 'is_file', return_value=True), patch('windows_ocr.subprocess.run', return_value=result) as run:
            windows_ocr._invoke(request, 30)
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertNotIn(request['path'], run.call_args.args[0])
        self.assertEqual(json.loads(run.call_args.kwargs['input']), request)
        self.assertFalse(run.call_args.kwargs.get('shell', False))

    def test_empty_password_pdf_uses_single_page_copy_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / 'public.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            annotated = writer.add_blank_page(width=612, height=792)
            annotated[NameObject('/Annots')] = ArrayObject([DictionaryObject({
                NameObject('/Subtype'): NameObject('/Text'),
                NameObject('/Rect'): ArrayObject([NumberObject(value) for value in (0, 0, 20, 20)]),
                NameObject('/Contents'): TextStringObject('synthetic annotation'),
            })])
            writer.encrypt(user_password='', owner_password='owner-only', algorithm='AES-256')
            writer.write(str(original))
            original_bytes = original.read_bytes()
            temporary_paths = []

            def invoke(request, timeout):
                derivative = Path(request['path'])
                temporary_paths.append(derivative)
                self.assertNotEqual(derivative, original)
                self.assertEqual(request['page'], 1)
                reader = PdfReader(derivative)
                self.assertFalse(reader.is_encrypted)
                self.assertEqual(len(reader.pages), 1)
                self.assertEqual(reader.pages[0].mediabox.width, 612)
                self.assertNotIn('/Annots', reader.pages[0])
                return {'text': '架空の確認文字'}

            with patch('windows_ocr._invoke', side_effect=invoke):
                self.assertEqual(windows_ocr.ocr_pdf_page(original, 2), '架空の確認文字')
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertTrue(temporary_paths)
            self.assertTrue(all(not path.exists() and not path.parent.exists() for path in temporary_paths))

            def fail(request, timeout):
                temporary_paths.append(Path(request['path']))
                raise windows_ocr.OCRError('OCR failed')

            with patch('windows_ocr._invoke', side_effect=fail), self.assertRaises(windows_ocr.OCRError):
                windows_ocr.ocr_pdf_page(original, 1)
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertTrue(all(not path.exists() and not path.parent.exists() for path in temporary_paths))

    def test_actual_password_required_pdf_is_rejected_before_ocr(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / 'password.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            writer.encrypt(user_password='required', owner_password='owner-only')
            writer.write(str(original))
            with patch('windows_ocr._invoke') as invoke, self.assertRaises(ValueError):
                windows_ocr.ocr_pdf_page(original, 1)
            invoke.assert_not_called()

    def test_rotated_pixel_boxes_return_to_same_original_page_coordinates(self):
        fixtures = {
            0: {'x': 40, 'y': 20, 'right': 60, 'bottom': 30},
            90: {'x': 70, 'y': 40, 'right': 80, 'bottom': 60},
            180: {'x': 140, 'y': 70, 'right': 160, 'bottom': 80},
            270: {'x': 20, 'y': 140, 'right': 30, 'bottom': 160},
        }
        for angle, pixels in fixtures.items():
            with self.subTest(rotation=angle):
                self.assert_box(windows_ocr._page_bbox(pixels, angle, 200, 100, [0, 0, 1, 1]),
                                {'x': .2, 'y': .2, 'width': .1, 'height': .1})
                self.assert_box(windows_ocr._page_bbox(pixels, angle, 200, 100, [.1, .2, .4, .5]),
                                {'x': .18, 'y': .3, 'width': .04, 'height': .05})

    def test_layout_keeps_original_page_number_and_separate_rotations(self):
        view = {'region': [0, 0, 1, 1], 'rotation': 90, 'source_width': 200, 'source_height': 100,
                'text': '公', 'lines': [{'text': '公', 'words': [{'text': '公',
                'bounds': {'x': 70, 'y': 40, 'right': 80, 'bottom': 60}}]}]}
        with patch('windows_ocr._pdf_request', return_value={'views': [view]}) as request:
            result = windows_ocr.ocr_pdf_layout(Path('unused.pdf'), 8, rotations=(90,))
        self.assertEqual(result['page'], 8)
        self.assertEqual(result['coordinate_system'], 'normalized_page')
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['views'][0]['rotation'], 90)
        self.assert_box(result['views'][0]['lines'][0]['bbox'], {'x': .2, 'y': .2, 'width': .1, 'height': .1})
        self.assertEqual(request.call_args.kwargs['mode'], 'layout')
        self.assertEqual(request.call_args.kwargs['rotations'], [90])

    def test_layout_rejects_invalid_regions_and_angles_before_processing_pdf(self):
        invalid = [
            {'rotations': ()}, {'rotations': (0, 0)}, {'rotations': (True,)}, {'rotations': (45,)},
            {'regions': []}, {'regions': [[0, 0, 1]]}, {'regions': [[0, 0, 0, 1]]},
            {'regions': [[.9, 0, .2, 1]]}, {'regions': [[float('nan'), 0, 1, 1]]},
        ]
        with patch('windows_ocr._pdf_request') as request:
            for options in invalid:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    windows_ocr.ocr_pdf_layout(Path('unused.pdf'), 1, **options)
        request.assert_not_called()

    def test_layout_missing_views_and_invalid_dimensions_are_errors(self):
        with patch('windows_ocr._pdf_request', return_value={'views': []}), self.assertRaises(windows_ocr.OCRError):
            windows_ocr.ocr_pdf_layout(Path('unused.pdf'), 1)
        invalid = {'views': [{'region': [0, 0, 1, 1], 'rotation': 0, 'source_width': 0,
                             'source_height': 100, 'text': '', 'lines': []}]}
        with patch('windows_ocr._pdf_request', return_value=invalid), self.assertRaises(windows_ocr.OCRError):
            windows_ocr.ocr_pdf_layout(Path('unused.pdf'), 1, rotations=(0,))


if __name__ == '__main__':
    unittest.main()
