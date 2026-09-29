"""Explicit PDF-page scopes and conservative, review-required heading suggestions."""
import io
import math
from pathlib import Path
import re
import unicodedata

from pdf_access import ensure_readable


_SECTION_LABELS = ('諸事項', '官庁', '裁判所', '地方公共団体', '会社その他',
                   '会社その他の公告', '特殊法人等', '政府調達')
_NOTICE_TITLES = ('相続財産清算人の選任及び相続権主張の催告',
                  '破産手続開始及び免責許可申立てに関する意見申述期間',
                  '前払式支払手段発行者の発行保証金に係る仮配当表公示',
                  '相続財産清算人の選任', '破産手続開始', '破産手続終結', '破産手続廃止',
                  '公示送達', '失踪宣告', '除権決定', '合併公告', '解散公告', '入札公告')
_NUMBER = r'[0-9〇零一二三四五六七八九十百]+'
_REFERENCE_LINE = re.compile(
    r'^(?:' + '|'.join(re.escape(label) for label in ('公告',) + _SECTION_LABELS) + r')[.・…⋯─―ー-]*'
    + rf'\(?{_NUMBER}(?:[-〜~]{_NUMBER})?(?:頁|ページ)?\)?$')
_PAGE_NUMBER_LINE = re.compile(rf'^[.・…⋯─―ー-]*\(?{_NUMBER}(?:[-〜~]{_NUMBER})?(?:頁|ページ)?\)?$')
_DOTTED_REFERENCE = re.compile(r'[.・…⋯]{2,}' + _NUMBER + r'(?:頁|ページ)?$')


def _normalized_lines(text):
    lines = [re.sub(r'\s+', '', unicodedata.normalize('NFKC', line))
             for line in text.splitlines()]
    return [line for line in lines if line]


def _label_spans(lines, label):
    """Match complete lines or wrapped labels, never a substring of running prose."""
    for offset in range(len(lines)):
        for count in range(1, min(len(label), len(lines) - offset) + 1):
            joined = ''.join(lines[offset:offset + count])
            if joined == label:
                yield offset, offset + count
            if len(joined) >= len(label):
                break


def _contents_claims(lines):
    """Potential TOC clues, with line spans for later spatial verification."""
    for index, line in enumerate(lines):
        if '目次' in line:
            yield 'heading', index, index + 1
        elif lines[index:index + 2] == ['目', '次']:
            yield 'heading', index, index + 2
        if _REFERENCE_LINE.fullmatch(line):
            yield 'reference', index, index + 1
    for label in ('公告',) + _SECTION_LABELS + _NOTICE_TITLES:
        for index, line in enumerate(lines):
            if line.startswith(label) and _PAGE_NUMBER_LINE.fullmatch(line[len(label):]):
                yield 'reference', index, index + 1
        for start, end in _label_spans(lines, label):
            if end < len(lines) and _PAGE_NUMBER_LINE.fullmatch(lines[end]):
                yield 'reference', start, end + 1


def _has_contents_evidence(text):
    return next(_contents_claims(_normalized_lines(text)), None) is not None


def _notice_heading_reason(text):
    """Return evidence for an isolated heading, never for a word inside a sentence."""
    lines = _normalized_lines(text)
    # A contents page can contain both the heading and every section label.
    if _has_contents_evidence(text):
        return None
    for index, line in enumerate(lines):
        if line == '公告':
            heading_end = index + 1
        elif line == '公' and lines[index + 1:index + 2] == ['告']:
            heading_end = index + 2
        else:
            continue
        context = lines[heading_end:heading_end + 20]
        if not context:
            continue
        # OCR sometimes splits "公告 …… 8" into a heading and a number line.
        if (_PAGE_NUMBER_LINE.fullmatch(context[0]) or any(_REFERENCE_LINE.fullmatch(v) for v in context)
                or any(_DOTTED_REFERENCE.search(v) for v in context)):
            continue
        found_labels = []
        for label in _SECTION_LABELS:
            if any(_label_spans(context, label)):
                found_labels.append(label)
        if found_labels:
            return f'独立した見出し「公告」と近接する節見出し「{found_labels[0]}」を検出しました。原文で開始位置を確認してください。'
        for title in _NOTICE_TITLES:
            if any(_label_spans(context, title)):
                return f'独立した見出し「公告」と近接する「{title}」を検出しました。原文で開始位置を確認してください。'
    return None


def _layout_views(layout):
    if (not isinstance(layout, dict) or layout.get('coordinate_system') != 'normalized_page'
            or not isinstance(layout.get('views'), list)):
        raise ValueError('見出しOCRの座標形式が不正です。')
    views = layout['views']
    if any(not isinstance(view, dict) or not isinstance(view.get('text', ''), str)
           or not isinstance(view.get('lines', []), list) for view in views):
        raise ValueError('見出しOCRの行形式が不正です。')
    return views


def _layout_contents(layout):
    for view in _layout_views(layout):
        if _has_contents_evidence(view.get('text', '')):
            return True
        lines = [line.get('text', '') for line in view.get('lines', []) if isinstance(line, dict)]
        if all(isinstance(text, str) for text in lines) and _has_contents_evidence('\n'.join(lines)):
            return True
    return False


def _box_gap(first, second):
    dx = max(first['x'] - second['x'] - second['width'], second['x'] - first['x'] - first['width'], 0)
    dy = max(first['y'] - second['y'] - second['height'], second['y'] - first['y'] - first['height'], 0)
    return math.hypot(dx, dy)


def _axis_overlap(first, second, axis, extent):
    return max(0, min(first[axis] + first[extent], second[axis] + second[extent])
               - max(first[axis], second[axis]))


def _flow_directions(first, following, maximum_gap=0.08):
    """A heading stack follows horizontal rows down or vertical columns left.

    The following label's shape supplies its reading orientation; proximity alone
    must not join horizontal labels from neighbouring columns.
    """
    directions = set()
    if _box_gap(first, following) > maximum_gap:
        return directions
    if (following['width'] >= following['height'] * 1.2
            and _axis_overlap(first, following, 'x', 'width') >= min(first['width'], following['width']) * .25
            and following['y'] >= first['y'] + first['height'] * .4):
        directions.add('down')
    if (following['height'] >= following['width'] * 1.2
            and _axis_overlap(first, following, 'y', 'height') >= min(first['height'], following['height']) * .25
            and following['x'] + following['width'] <= first['x'] + first['width'] * .6):
        directions.add('left')
    return directions


def _union_box(boxes):
    left, top = min(box['x'] for box in boxes), min(box['y'] for box in boxes)
    right = max(box['x'] + box['width'] for box in boxes)
    bottom = max(box['y'] + box['height'] for box in boxes)
    return {'x': left, 'y': top, 'width': right - left, 'height': bottom - top}


def _wrapped_label_directions(first, second):
    if first['view_index'] != second['view_index'] or abs(first['line_index'] - second['line_index']) > 3:
        return set()
    a, b = first['bbox'], second['bbox']
    if _box_gap(a, b) > 0.03:
        return set()
    horizontal_overlap = max(0, min(a['x'] + a['width'], b['x'] + b['width']) - max(a['x'], b['x']))
    vertical_overlap = max(0, min(a['y'] + a['height'], b['y'] + b['height']) - max(a['y'], b['y']))
    # Both horizontal wrapping (next line down) and Japanese vertical wrapping
    # (next column left) are possible after mapping rotated OCR back to the page.
    below = b['y'] >= a['y'] + a['height'] * 0.4 and (
        horizontal_overlap >= min(a['width'], b['width']) * 0.5 or abs(a['x'] - b['x']) <= 0.025)
    left = b['x'] + b['width'] <= a['x'] + a['width'] * 0.6 and (
        vertical_overlap >= min(a['height'], b['height']) * 0.5 or abs(a['y'] - b['y']) <= 0.025)
    return ({'down'} if below else set()) | ({'left'} if left else set())


def _geometric_labels(lines, labels):
    results = []
    for label in labels:
        for first in lines:
            if not first['text'] or not label.startswith(first['text']):
                continue
            stack = [(first['text'], [first], {'down', 'left'})]
            while stack:
                text, parts, directions = stack.pop()
                if text == label:
                    results.append({'text': label, 'bbox': _union_box([part['bbox'] for part in parts]),
                                    'rotation': first['rotation'], 'parts': parts})
                    continue
                if len(parts) >= 3:
                    continue
                for following in lines:
                    combined = text + following['text']
                    if following not in parts and following['text'] and label.startswith(combined):
                        joined_directions = directions & _wrapped_label_directions(parts[-1], following)
                        if joined_directions:
                            stack.append((combined, parts + [following], joined_directions))
    return results


def _reference_near(label, number):
    """A detached page number must align with its label, not merely be nearby."""
    a, b = label['bbox'], number['bbox']
    if _box_gap(a, b) > .08:
        return False
    same_row = _axis_overlap(a, b, 'y', 'height') >= min(a['height'], b['height']) * .5
    same_column = _axis_overlap(a, b, 'x', 'width') >= min(a['width'], b['width']) * .5
    return (same_row and b['x'] >= a['x'] + a['width'] * .5
            or same_column and b['y'] >= a['y'] + a['height'] * .5)


def _contents_regions(views, lines):
    """Localize TOC headings and references; retain a veto if localization fails."""
    regions = []
    unlocated = False
    checked = set()
    located = {(line['view_index'], line['line_index']): line for line in lines}
    for view_index, view in enumerate(views):
        entries = []
        for line_index, line in enumerate(view.get('lines', [])):
            if isinstance(line, dict) and isinstance(line.get('text'), str):
                text = ''.join(_normalized_lines(line['text']))
                if text:
                    entries.append((text, located.get((view_index, line_index))))
        texts = [entry[0] for entry in entries]
        claims = list(_contents_claims(texts))
        # A textual clue whose corresponding OCR line has no coordinates cannot
        # safely be separated from the proposed announcement block.
        claim_texts = {''.join(texts[start:end]) for _, start, end in claims}
        flattened = _normalized_lines(view.get('text', ''))
        if any(''.join(flattened[start:end]) not in claim_texts
               for _, start, end in _contents_claims(flattened)):
            unlocated = True
        for kind, start, end in claims:
            parts = [entry[1] for entry in entries[start:end]]
            if any(part is None for part in parts):
                unlocated = True
                continue
            checked.add(''.join(texts[start:end]))
            boxes = [part['bbox'] for part in parts]
            if len(parts) > 1:
                if kind == 'heading':
                    a, b = boxes[0], boxes[-1]
                    if not (_box_gap(a, b) <= .08 and (
                            _axis_overlap(a, b, 'x', 'width') >= min(a['width'], b['width']) * .5
                            or _axis_overlap(a, b, 'y', 'height') >= min(a['height'], b['height']) * .5)):
                        continue
                elif not _reference_near({'bbox': _union_box(boxes[:-1])}, parts[-1]):
                    # Reading order alone can put a remote margin numeral after
                    # a real title. Its coordinates disprove the apparent link.
                    continue
            regions.append({'kind': kind, 'bbox': _union_box(boxes)})
    labels = _geometric_labels(lines, ('公告',) + _SECTION_LABELS + _NOTICE_TITLES)
    for label in labels:
        for number in lines:
            if _PAGE_NUMBER_LINE.fullmatch(number['text']) and _reference_near(label, number):
                regions.append({'kind': 'reference', 'bbox': _union_box([label['bbox'], number['bbox']])})
    return regions, unlocated, sorted(checked)


def _candidate_in_contents(parts, regions):
    for region in regions:
        box = region['bbox']
        for part in parts:
            candidate = part['bbox']
            if region['kind'] == 'reference':
                if (_axis_overlap(box, candidate, 'x', 'width') > 0
                        and _axis_overlap(box, candidate, 'y', 'height') > 0):
                    return True
                continue
            # A TOC heading governs its own aligned column/row, not the whole page.
            horizontal = box['width'] >= box['height']
            aligned = (_axis_overlap(box, candidate, 'x', 'width') if horizontal
                       else _axis_overlap(box, candidate, 'y', 'height'))
            if aligned > 0 and (candidate['y'] >= box['y'] if horizontal
                                else candidate['x'] <= box['x'] + box['width']):
                return True
    return False


def notice_layout_evidence(layout):
    """Evaluate independent OCR labels across views in normalized original-page coordinates.

    A complete heading plus a nearby section is strong supporting evidence. One
    isolated heading character requires both a nearby section and an independent
    nearby notice title and remains weak. This never confirms the notice scope.
    """
    views = _layout_views(layout)
    lines = []
    for view_index, view in enumerate(views):
        for line_index, line in enumerate(view.get('lines', [])):
            if not isinstance(line, dict) or not isinstance(line.get('text'), str):
                continue
            box = line.get('bbox')
            if not isinstance(box, dict):
                continue
            values = [box.get(key) for key in ('x', 'y', 'width', 'height')]
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
                continue
            x, y, width, height = values
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1.00001 or y + height > 1.00001:
                continue
            text = ''.join(_normalized_lines(line['text']))
            if text:
                lines.append({'text': text, 'bbox': dict(zip(('x', 'y', 'width', 'height'), values)),
                              'view_index': view_index, 'line_index': line_index, 'rotation': view.get('rotation', 0)})
    contents_regions, unlocated_contents, checked_contents = _contents_regions(views, lines)
    if unlocated_contents:
        return None
    sections = _geometric_labels(lines, _SECTION_LABELS)
    headings = _geometric_labels(lines, ('公告',))
    for heading in headings:
        for section in sections:
            directions = _flow_directions(heading['bbox'], section['bbox'])
            if directions and not _candidate_in_contents((heading, section), contents_regions):
                return {'strength': 'strong', 'heading': heading, 'section': section, 'title': None,
                        'direction': sorted(directions)[0], 'contents_localized': bool(contents_regions),
                        'contents_checked': checked_contents,
                        'reason': f'独立した「公告」と節見出し「{section["text"]}」の近接配置を確認しました。原文で開始位置を確認してください。'}
    titles = _geometric_labels(lines, _NOTICE_TITLES)
    for heading in lines:
        if heading['text'] not in ('公', '告') or min(heading['bbox']['width'], heading['bbox']['height']) < 0.005:
            continue
        for section in sections:
            if section['text'] != '諸事項':
                continue
            directions = _flow_directions(heading['bbox'], section['bbox'])
            if not directions:
                continue
            for title in titles:
                consistent = directions & _flow_directions(section['bbox'], title['bbox'], maximum_gap=0.10)
                if (consistent and _box_gap(heading['bbox'], title['bbox']) <= 0.15
                        and not _candidate_in_contents((heading, section, title), contents_regions)):
                    return {'strength': 'weak', 'heading': heading, 'section': section, 'title': title,
                            'direction': sorted(consistent)[0], 'contents_localized': bool(contents_regions),
                            'contents_checked': checked_contents,
                            'reason': f'片字「{heading["text"]}」、節見出し「{section["text"]}」、独立した「{title["text"]}」の近接配置による弱い候補です。大見出しは全文認識できていないため、原文確認が必要です。'}
    return None


def find_notice_start(path, ocr=None, cancelled=None, progress=None, layout_ocr=None):
    """Suggest an original PDF page; no scope or extracted content is persisted.

    Inspect each page's extractable text and optional OCR before advancing, so an
    earlier image heading is not skipped in favour of a later text heading.
    Callbacks receive (Path, page) and (page, total, method). scanned_pages counts
    distinct inspected PDF pages, not text/OCR attempts.
    """
    from pypdf import PdfReader
    for callback, name in ((ocr, 'OCR'), (cancelled, '中止確認'), (progress, '進捗'), (layout_ocr, '見出しOCR')):
        if callback is not None and not callable(callback):
            raise ValueError(f'{name}の処理が不正です。')
    path = Path(path).resolve()
    reader = PdfReader(io.BytesIO(path.read_bytes()))
    ensure_readable(reader)
    total = len(reader.pages)
    if total < 1:
        raise ValueError('PDFにページがありません。')
    result = {'total_pages': total, 'start_page': None, 'method': None, 'reason': '',
              'scanned_pages': 0, 'text_scanned_pages': 0, 'ocr_scanned_pages': 0,
              'warnings': [], 'cancelled': False, 'evidence_strength': None, 'layout_evidence': None}
    scanned, unreadable = set(), set()

    def finish(reason=None):
        result['scanned_pages'] = len(scanned)
        if reason:
            result['reason'] = reason
        relevant = sorted(number for number in unreadable
                          if result['start_page'] is None or number < result['start_page'])
        if relevant:
            shown = ', '.join(map(str, relevant[:20])) + (' ほか' if len(relevant) > 20 else '')
            result['warnings'].append('見出しを十分に確認できないページ: ' + shown
                                      + '。公告の開始位置を取りこぼした可能性があるため、原文確認が必要です。')
        return result

    def stopped():
        if cancelled is not None and cancelled():
            result['cancelled'] = True
            result['warnings'].append('探索を中止しました。未確認ページがあり、公告範囲は確定していません。')
            return True
        return False

    consecutive_failures = 0
    for number, page in enumerate(reader.pages, 1):
        if stopped():
            return finish('探索を中止しました。必要な公告範囲を原文から指定してください。')
        if progress is not None:
            progress(number, total, 'text')
        scanned.add(number)
        result['text_scanned_pages'] += 1
        try:
            text = page.extract_text() or ''
        except Exception:
            text = ''
        if stopped():
            return finish('探索を中止しました。必要な公告範囲を原文から指定してください。')
        if not text.strip():
            unreadable.add(number)
        text_reason = _notice_heading_reason(text)
        contents_page = _has_contents_evidence(text)
        ocr_reason = None
        layout_evidence = None
        layout_success = False
        if ocr is not None or layout_ocr is not None:
            if stopped():
                return finish('探索を中止しました。必要な公告範囲を原文から指定してください。')
            if progress is not None:
                progress(number, total, 'ocr')
            result['ocr_scanned_pages'] += 1
            try:
                if layout_ocr is not None:
                    layout = layout_ocr(path, number)
                    views = _layout_views(layout)
                    upright = next((view for view in views if view.get('rotation', 0) == 0), views[0] if views else {})
                    recognized = upright.get('text', '')
                    layout_evidence = notice_layout_evidence(layout)
                    layout_success = True
                else:
                    recognized = ocr(path, number)
                if not isinstance(recognized, str):
                    raise ValueError('OCRの戻り値が文字列ではありません。')
            except Exception as error:
                unreadable.add(number)
                consecutive_failures += 1
                result['warnings'].append(f'OCR失敗（PDF {number}ページ）: {str(error)[:160]}')
            else:
                consecutive_failures = 0
                if recognized.strip() or layout_evidence:
                    unreadable.discard(number)
                else:
                    unreadable.add(number)
                if not layout_success:
                    contents_page = contents_page or _has_contents_evidence(recognized)
                    ocr_reason = _notice_heading_reason(recognized)
        if stopped():
            return finish('探索を中止しました。必要な公告範囲を原文から指定してください。')
        # Once layout OCR succeeded, flattened reading order must not override its
        # rejection of distant columns or incomplete heading evidence.
        direct_reason = None if layout_success else text_reason or ocr_reason
        # Positioned TOC evidence can coexist with a separate announcement block.
        # Without positions, including plain-text-only OCR, retain the conservative veto.
        text_lines = _normalized_lines(text)
        native_claims = {''.join(text_lines[start:end]) for _, start, end in _contents_claims(text_lines)}
        contents_ok = not contents_page or bool(layout_evidence and native_claims.issubset(
            set(layout_evidence['contents_checked'])))
        if contents_ok and (direct_reason or layout_evidence):
            reason = direct_reason or layout_evidence['reason']
            strength = 'strong' if direct_reason else layout_evidence['strength']
            result.update(start_page=number, method='text' if direct_reason and text_reason else 'ocr', reason=reason,
                          evidence_strength=strength, layout_evidence=layout_evidence)
            return finish()
        if consecutive_failures >= 3:
            result['warnings'].append('OCRが3ページ連続で失敗したため探索を停止しました。取りこぼしの可能性があります。')
            return finish('OCRで開始候補を確認できませんでした。原文から手動で指定してください。')
    if stopped():
        return finish('探索を中止しました。必要な公告範囲を原文から指定してください。')
    return finish('確かな開始候補を検出できませんでした。原文を確認して公告ページを手動で指定してください。')


def parse_page_ranges(value, total_pages):
    if type(total_pages) is not int or total_pages < 1:
        raise ValueError('PDFのページ数を確認できません。')
    value = unicodedata.normalize('NFKC', str(value)).strip()
    for old in ('、', ';', '；'):
        value = value.replace(old, ',')
    for old in ('〜', '～', '~', '–', '—'):
        value = value.replace(old, '-')
    if not value:
        raise ValueError('公告が載っているPDF内のページ番号を入力してください。例: 9-12,15')
    result = set()
    for part in value.split(','):
        match = re.fullmatch(r'\s*([0-9]+)\s*(?:-\s*([0-9]+)\s*)?', part)
        if not match:
            raise ValueError('ページは「9-12,15」のように入力してください。')
        first = int(match[1])
        last = int(match[2] or match[1])
        if not 1 <= first <= last <= total_pages:
            raise ValueError(f'1〜{total_pages}の範囲で、小さい番号から指定してください。')
        result.update(range(first, last + 1))
    return sorted(result)


def format_page_ranges(pages):
    pages = sorted(set(pages))
    if not pages:
        return ''
    groups = []
    start = previous = pages[0]
    for number in pages[1:]:
        if number != previous + 1:
            groups.append(str(start) if start == previous else f'{start}-{previous}')
            start = number
        previous = number
    groups.append(str(start) if start == previous else f'{start}-{previous}')
    return ','.join(groups)
