"""Local Japanese OCR using Windows' built-in PDF renderer and OCR engine."""

import json
import io
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from pdf_access import ensure_readable


class OCRError(RuntimeError):
    """Windows OCR could not complete; callers must keep the page unreviewed."""


def _invoke(request, timeout):
    if sys.platform != 'win32':
        raise OCRError('Windows標準OCRはWindowsでのみ利用できます。')
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    script = Path(__file__).resolve().with_suffix('.ps1')
    if not powershell.is_file() or not script.is_file():
        raise OCRError('Windows OCRの実行ファイルが見つかりません。配布フォルダー全体を展開してください。')
    try:
        result = subprocess.run(
            # Command text is our bundled code, never a document path or user input.
            # No execution-policy change is made; enterprise application controls apply.
            [str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script.read_text(encoding='utf-8-sig')],
            input=json.dumps(request, ensure_ascii=True).encode('utf-8'),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise OCRError('Windows OCRが制限時間内に完了しませんでした。原文を確認してください。') from error
    except OSError as error:
        raise OCRError('Windows OCRを起動できませんでした。') from error
    try:
        response = json.loads(result.stdout.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OCRError('Windows OCRから結果を取得できませんでした。PowerShellの実行制限も確認してください。') from error
    if not isinstance(response, dict):
        raise OCRError('Windows OCRの応答形式が不正です。')
    if result.returncode != 0 or not response.get('ok'):
        raise OCRError(str(response.get('error') or 'Windows OCRに失敗しました。'))
    return response


def availability():
    """Return availability without changing language settings or installing anything."""
    try:
        result = _invoke({'mode': 'availability'}, timeout=30)
        return {key: value for key, value in result.items() if key != 'ok'}
    except OCRError as error:
        return {'available': False, 'languages': [], 'message': str(error)}


def _pdf_request(path, page_number, mode='page', _timeout=180, **options):
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError('ページ番号は1以上の整数を指定してください。')
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('PDFは絶対パスで指定してください。')
    try:
        path = path.resolve(strict=True)
    except OSError as error:
        raise ValueError('PDFが見つかりません。') from error
    if not path.is_file() or path.suffix.lower() != '.pdf':
        raise ValueError('PDFファイルを指定してください。')
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(path.read_bytes()))
    ensure_readable(reader)
    if page_number > len(reader.pages):
        raise ValueError('ページ番号がPDFの総ページ数を超えています。')
    if reader.is_encrypted:
        # Windows' PDF renderer rejects some publicly readable, empty-password PDFs.
        # Render a temporary single-page copy only after ordinary reading succeeds.
        # The original PDF and its hash are never changed. Never guess a password.
        with tempfile.TemporaryDirectory(prefix='kanpo-ocr-page-') as temporary:
            rendered = Path(temporary) / 'page.pdf'
            writer = PdfWriter()
            # Form/signature annotations can contain malformed encrypted strings.
            # They are not page body text; omit them only from this rendering copy.
            writer.add_page(reader.pages[page_number - 1], excluded_keys=['/Annots'])
            writer.write(str(rendered))
            result = _invoke({'mode': mode, 'path': str(rendered), 'page': 1, **options}, timeout=_timeout)
    else:
        result = _invoke({'mode': mode, 'path': str(path), 'page': page_number, **options}, timeout=_timeout)
    return result


def ocr_pdf_page(path: Path, page_number: int) -> str:
    """Recognize one PDF page (one-based) locally; never mark it reviewed."""
    result = _pdf_request(path, page_number)
    text = result.get('text')
    if not isinstance(text, str):
        raise OCRError('Windows OCRが読み取り本文を返しませんでした。')
    return text


def render_pdf_page(path: Path, page_number: int, output_path: Path) -> Path:
    """Save a new PNG in displayed-page orientation, without changing the PDF."""
    output = Path(output_path)
    if not output.is_absolute() or output.suffix.lower() != '.png':
        raise ValueError('プレビューの保存先はPNGの絶対パスを指定してください。')
    output = output.resolve()
    if output.exists() or not output.parent.is_dir():
        raise ValueError('プレビューは存在するフォルダー内の新しいファイルへ保存してください。')
    _pdf_request(path, page_number, mode='render', output=str(output), _timeout=60)
    try:
        with output.open('rb') as stream:
            signature = stream.read(8)
    except OSError as error:
        raise OCRError('原文のプレビュー画像を取得できませんでした。') from error
    if signature != b'\x89PNG\r\n\x1a\n':
        raise OCRError('原文のプレビューがPNG形式ではありません。')
    return output


def _page_bbox(raw, rotation, source_width, source_height, region):
    """Map a rotated OCR pixel rectangle to the original displayed page."""
    left, top, right, bottom = (float(raw[key]) for key in ('x', 'y', 'right', 'bottom'))
    if not all(math.isfinite(value) for value in (left, top, right, bottom)) or right < left or bottom < top:
        raise OCRError('Windows OCRの位置情報が不正です。')
    points = []
    for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):
        if rotation == 90:
            x, y = y, source_height - x
        elif rotation == 180:
            x, y = source_width - x, source_height - y
        elif rotation == 270:
            x, y = source_width - y, x
        points.append((region[0] + x / source_width * region[2], region[1] + y / source_height * region[3]))
    xs, ys = zip(*points)
    x0, y0 = min(1.0, max(0.0, min(xs))), min(1.0, max(0.0, min(ys)))
    x1, y1 = max(0.0, min(1.0, max(xs))), max(0.0, min(1.0, max(ys)))
    return {'x': x0, 'y': y0, 'width': max(0.0, x1 - x0), 'height': max(0.0, y1 - y0)}


def _layout_result(raw, page_number):
    views = []
    try:
        for source in raw['views']:
            region = source['region']
            rotation = source['rotation']
            if type(rotation) is not int or rotation not in (0, 90, 180, 270) or len(region) != 4:
                raise ValueError('invalid view orientation')
            if not all(math.isfinite(value) for value in region):
                raise ValueError('invalid region')
            width, height = float(source['source_width']), float(source['source_height'])
            if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise ValueError('invalid dimensions')
            lines = []
            for line in source['lines']:
                if not isinstance(line['text'], str) or any(not isinstance(word['text'], str) for word in line['words']):
                    raise ValueError('invalid OCR text')
                words = [{'text': word['text'], 'bbox': _page_bbox(word['bounds'], rotation, width, height, region)}
                         for word in line['words']]
                if not words:
                    continue
                left = min(word['bbox']['x'] for word in words)
                top = min(word['bbox']['y'] for word in words)
                right = max(word['bbox']['x'] + word['bbox']['width'] for word in words)
                bottom = max(word['bbox']['y'] + word['bbox']['height'] for word in words)
                lines.append({'text': line['text'], 'bbox': {'x': left, 'y': top, 'width': right-left, 'height': bottom-top},
                              'words': words})
            if not isinstance(source['text'], str):
                raise ValueError('invalid view text')
            views.append({'region': region, 'rotation': rotation, 'text': source['text'], 'lines': lines})
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise OCRError('Windows OCRの構造化結果を読み取れませんでした。') from error
    return {'page': page_number, 'coordinate_system': 'normalized_page', 'language': 'ja', 'views': views, 'errors': []}


def ocr_pdf_layout(path: Path, page_number: int, *, regions=None, rotations=(0, 90, 180, 270), timeout=90) -> dict:
    """Return lines and word boxes in normalized original display coordinates.

    A region is (x, y, width, height), from the displayed page's top-left after
    PDF /Rotate is applied. Rotations are clockwise, applied only during OCR.
    All returned boxes are mapped back to the same original page orientation.
    Views remain separate: rotation can improve headers while hurting body text.
    OCR confidence is not supplied by Windows and is intentionally not invented.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 180:
        raise ValueError('OCRの制限時間は1〜180秒を指定してください。')
    if not isinstance(rotations, (tuple, list)) or not 1 <= len(rotations) <= 4:
        raise ValueError('OCRの回転角度を1〜4個指定してください。')
    if any(type(angle) is not int or angle not in (0, 90, 180, 270) for angle in rotations) or len(set(rotations)) != len(rotations):
        raise ValueError('回転角度は0、90、180、270から重複なく指定してください。')
    regions = [(0.0, 0.0, 1.0, 1.0)] if regions is None else regions
    if not isinstance(regions, (tuple, list)) or not 1 <= len(regions) <= 12:
        raise ValueError('OCR領域を1〜12個指定してください。')
    normalized = []
    for region in regions:
        if not isinstance(region, (tuple, list)) or len(region) != 4:
            raise ValueError('OCR領域はx、y、幅、高さで指定してください。')
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in region):
            raise ValueError('OCR領域には有限の数値を指定してください。')
        x, y, width, height = map(float, region)
        if not (0 <= x < 1 and 0 <= y < 1 and width > 0 and height > 0 and x + width <= 1 and y + height <= 1):
            raise ValueError('OCR領域はページ内の0〜1の範囲で指定してください。')
        normalized.append([x, y, width, height])
    result = _pdf_request(path, page_number, mode='layout', regions=normalized, rotations=list(rotations), _timeout=timeout)
    if not isinstance(result.get('views'), list) or len(result['views']) != len(normalized) * len(rotations):
        raise OCRError('Windows OCRの領域結果が不足しています。')
    return _layout_result(result, page_number)
