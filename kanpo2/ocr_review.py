"""Conservative OCR supplements and review evidence, never confidence scores."""
import math
import re
import time


FORMS = ('株式会社', '有限会社', '合同会社', '協同組合', '合資会社', '合名会社', '一般社団法人', '一般財団法人')
CONFUSABLE = str.maketrans({'工': 'エ', 'ェ': 'エ', '-': 'ー', '0': 'o',
                           'ァ': 'ア', 'ィ': 'イ', 'ゥ': 'ウ', 'ォ': 'オ',
                           'ッ': 'ツ', 'ャ': 'ヤ', 'ュ': 'ユ', 'ョ': 'ヨ'})


def _norm(text):
    from core import normalize
    return normalize(text)


def _box(value):
    try:
        x, y, w, h = (float(value[key]) for key in ('x', 'y', 'width', 'height'))
        if not all(math.isfinite(v) for v in (x, y, w, h)) or min(x, y) < 0 or min(w, h) <= 0 or x+w > 1.00001 or y+h > 1.00001:
            return None
        return {'x': x, 'y': y, 'width': min(w, 1-x), 'height': min(h, 1-y)}
    except (TypeError, KeyError, ValueError):
        return None


def _union(a, b):
    x, y = min(a['x'], b['x']), min(a['y'], b['y'])
    return {'x': x, 'y': y, 'width': max(a['x']+a['width'], b['x']+b['width'])-x,
            'height': max(a['y']+a['height'], b['y']+b['height'])-y}


def _overlap(a, b):
    if not a or not b:
        return 0
    area = max(0, min(a['x']+a['width'], b['x']+b['width'])-max(a['x'], b['x'])) * max(0, min(a['y']+a['height'], b['y']+b['height'])-max(a['y'], b['y']))
    return area / min(a['width']*a['height'], b['width']*b['height'])


def _oriented(b, angle):
    x, y, w, h = (b[k] for k in ('x', 'y', 'width', 'height'))
    if angle == 90:
        return 1-y-h, x, h, w
    if angle == 180:
        return 1-x-w, 1-y-h, w, h
    if angle == 270:
        return y, 1-x-w, h, w
    return x, y, w, h


def _segments(view):
    angle = view.get('rotation', 0)
    lines = [{'text': str(line.get('text', '')), 'bbox': _box(line.get('bbox')), 'rotation': angle}
             for line in view.get('lines', [])]
    lines = [line for line in lines if line['text'].strip() and line['bbox']]
    segments = list(lines)
    # Join only adjacent short lines in the same oriented column, never whole views.
    for a, b in zip(lines, lines[1:]):
        ax, ay, aw, ah = _oriented(a['bbox'], angle)
        bx, by, bw, bh = _oriented(b['bbox'], angle)
        if (abs(ax-bx) <= .018 and .3*ah <= by-ay <= 2.5*max(ah, bh)
                and .45 <= bh/ah <= 2.2 and bw <= aw*1.35 and len(_norm(a['text']+b['text'])) <= 160):
            segments.append({'text': a['text']+'\n'+b['text'], 'bbox': _union(a['bbox'], b['bbox']), 'rotation': angle})
    return segments


def _compact_views(views):
    # Review and overlays need line boxes, not every word duplicated four times in SQLite.
    return [{'rotation': v.get('rotation', 0), 'region': v.get('region', [0, 0, 1, 1]),
             'text': str(v.get('text', '')), 'lines': [
                 {'text': str(line.get('text', '')), 'bbox': _box(line.get('bbox'))}
                 for line in v.get('lines', []) if _box(line.get('bbox'))]}
            for v in views]


def _anchor(text):
    s = _norm(text)
    if not 5 <= len(s) <= 160:
        return False
    useful = sum(bool(re.match(r'[\w\u3040-\u30ff\u3400-\u9fff]', ch)) for ch in s)
    return useful/len(s) >= .75 and (any(form in s and len(s.replace(form, '')) >= 3 for form in FORMS)
                                     or bool(re.search(r'(公告|公示|催告|手続開始|手続終結)$', s)))


def _fold(text):
    chars, positions = [], []
    i = 0
    while i < len(text):
        if text[i:i+2] == '/ヾ':
            chars.append('パ'); positions.append((i, i+2)); i += 2
        else:
            chars.append(text[i].translate(CONFUSABLE)); positions.append((i, i+1)); i += 1
    return ''.join(chars), positions


def _distance(a, b, limit=2):
    if abs(len(a)-len(b)) > limit:
        return limit+1
    row = list(range(len(b)+1))
    for i, left in enumerate(a, 1):
        new = [i]
        for j, right in enumerate(b, 1):
            new.append(min(new[-1]+1, row[j]+1, row[j-1]+(left != right)))
        if min(new) > limit:
            return limit+1
        row = new
    return row[-1]


def _name_parts(name):
    body = name
    for form in FORMS:
        body = body.replace(form, '')
    return body, _fold(name)[0]


def _near_name(name, text, parts=None, folded_unit=None):
    body, target = parts if parts is not None else _name_parts(name)
    if len(name) < 7 or len(body) < 3 or not any(form in name and form in text for form in FORMS):
        return None
    folded, positions = folded_unit if folded_unit is not None else _fold(text)
    found = folded.find(target)
    if found >= 0:
        observed = text[positions[found][0]:positions[found+len(target)-1][1]]
        if 0 < _distance(name, observed) <= 2:
            return observed
    # One edit only for longer names, anchored by an unchanged part of the name.
    if len(body) >= 5:
        starts = set()
        for anchor in (body[:3], body[-3:]):
            start = text.find(anchor)
            if start >= 0:
                starts.update(start-name.find(anchor)+delta for delta in (-1, 0, 1))
        for start in sorted(starts):
            if start < 0:
                continue
            for length in (len(name)-1, len(name), len(name)+1):
                observed = text[start:start+length]
                if _distance(name, observed, 1) == 1:
                    return observed
    return None


def match_with_review(text, ledger, analysis=None):
    """Exact legacy matches plus explicitly approximate candidates; no correction."""
    from core import match_candidates
    ledger = list(ledger or [])
    if not ledger:
        return []
    evidence = (analysis or {}).get('evidence') or [
        {'text': line, 'bbox': None, 'rotation': 0} for line in text.splitlines() if line.strip()]
    exact = match_candidates(text, ledger)
    results = []
    found = set()
    for item in exact:
        name = _norm(item['name'])
        source = next((e for e in evidence if name in _norm(e['text'])), {})
        results.append({**item, 'match_type': 'exact', 'bbox': source.get('bbox'),
                        'observed_text': source.get('text', '')})
        found.add((item.get('code'), item.get('name')))
    units, raw_grams, folded_grams = [], {}, {}
    for source in evidence:
        observed = _norm(source['text'])
        if len(observed) > 180 or not any(form in observed for form in FORMS):
            continue
        folded = _fold(observed)
        index = len(units)
        units.append((source, observed, folded))
        for value, lookup in ((observed, raw_grams), (folded[0], folded_grams)):
            for offset in range(len(value)-2):
                lookup.setdefault(value[offset:offset+3], set()).add(index)
    for item in ledger:
        if (item.get('code'), item.get('name')) in found:
            continue
        name = _norm(item.get('name'))
        parts = _name_parts(name)
        body, target = parts
        if len(name) < 7 or len(body) < 3:
            continue
        # A folded substring must contain all its trigrams. Using the smallest
        # posting list is only a prefilter: _near_name still makes the decision.
        postings = [folded_grams.get(target[offset:offset+3], set()) for offset in range(len(target)-2)]
        candidates = set(min(postings, key=len)) if postings else set()
        # Preserve the existing one-edit search, which requires one of these anchors.
        if len(body) >= 5:
            candidates.update(raw_grams.get(body[:3], ()))
            candidates.update(raw_grams.get(body[-3:], ()))
        for index in sorted(candidates):
            source, observed, folded = units[index]
            near = _near_name(name, observed, parts, folded)
            if near:
                results.append({**item, 'match_type': 'approximate', 'bbox': source.get('bbox'),
                                'observed_text': near,
                                'reason': '台帳の社名に似た読み取りがあります。文字の誤認または別の会社の可能性があるため原文確認が必要です。'})
                break
    return results


def update_ledger_flags(text, analysis, ledger, matches=None):
    """Refresh only ledger-dependent flags; return a new analysis, preserving evidence."""
    result = dict(analysis or {})
    flags = [dict(flag) for flag in result.get('flags', []) if flag.get('kind') != 'approximate_name']
    if matches is None:
        matches = match_with_review(text, ledger, result)
    for match in matches:
        if match.get('match_type') == 'approximate':
            flags.append({'kind': 'approximate_name', 'reason': match['reason'], 'bbox': match.get('bbox'),
                          'text': match.get('observed_text', ''), 'ledger_code': match.get('code'),
                          'ledger_name': match.get('name')})
    result['flags'] = flags
    if result.get('machine_status') == 'failed' or result.get('status') == 'failed':
        result['status'] = 'failed'
    else:
        result['status'] = 'needs_review' if flags else ('read' if text.strip() else 'empty')
    return result


def analyze_layout(layout, base_text='', ledger=None):
    """Pure analysis of already-read views in normalized original-page coordinates."""
    views = list(layout.get('views', []))
    evidence, flags, supplements = [], [], []
    normal = [entry for view in views if view.get('rotation') == 0 for entry in _segments(view)]
    evidence.extend(normal)
    if len(views) == 1 and views[0].get('rotation') != 0:
        evidence.extend(_segments(views[0]))
    # PDF extraction can contain only the printed header. Always retain normal OCR.
    # One explicitly chosen view (e.g. manual 90-degree region OCR) returns all its text.
    primary = ([views[0]] if len(views) == 1 else [v for v in views if v.get('rotation') == 0])
    text = str(base_text or '')
    for chosen in primary:
        recognized = str(chosen.get('text', ''))
        if recognized.strip() and _norm(recognized) not in _norm(text):
            text += ('\n' if text else '') + recognized
    normalized = _norm(text)
    for view in views:
        if view.get('rotation') == 0:
            continue
        for entry in _segments(view):
            value = _norm(entry['text'])
            if not _anchor(entry['text']) or value in normalized:
                continue
            # Evidence from another orientation is compared only at the same location.
            local = [old for old in normal if _overlap(old['bbox'], entry['bbox']) >= .5]
            if any(value in _norm(old['text']) for old in local):
                continue
            if any(value in _norm(old['text']) for old in supplements):
                continue
            supplements.append(entry)
            evidence.append(entry)
            flags.append({**entry, 'kind': 'rotated_supplement',
                          'reason': '向きを変えたOCRでこの位置の文字を補足しました。通常方向との読み取り差を原文で確認してください。'})
            text += '\n' + entry['text']
            normalized = _norm(text)
    for entry in normal:
        value = _norm(entry['text'])
        if re.fullmatch(r'[()甲乙丙]*('+'|'.join(FORMS)+r')', value):
            flags.append({**entry, 'kind': 'incomplete_name', 'reason': '法人種別だけが読めています。続く社名の文字が欠けている可能性があります。'})
        elif any(form in value for form in FORMS) and re.search(r'[/\\<>◆ヾ]', value):
            flags.append({**entry, 'kind': 'unusual_characters', 'reason': '社名付近に記号として読まれた文字があります。字形を原文で確認してください。'})
    result = {'text': text, 'flags': flags, 'views': _compact_views(views), 'evidence': evidence, 'supplements': supplements,
              'machine_status': 'succeeded' if text.strip() else 'empty'}
    if not any(str(view.get('text', '')).strip() for view in views):
        flags.append({'kind': 'empty_ocr', 'bbox': None, 'text': '',
                      'reason': 'OCRで文字を取得できませんでした。抽出文字が残っていても、画像の本文は原文確認が必要です。'})
        result['machine_status'] = 'empty'
    flags = update_ledger_flags(text, result, ledger)['flags']
    unique = []
    for flag in flags:
        if not any(old['kind'] == flag['kind'] and old['text'] == flag['text'] and old['bbox'] == flag['bbox'] for old in unique):
            unique.append(flag)
    result['flags'] = unique
    result['status'] = 'needs_review' if unique else ('read' if text.strip() else 'empty')
    return result


def analyze_page(path, page, layout_ocr, base_text='', ledger=None):
    """One layout request and at most two local retries; preserve original OCR text."""
    started = time.monotonic()
    try:
        layout = layout_ocr(path, page)
        result = analyze_layout(layout, base_text, ledger)
    except Exception as error:
        return {'text': str(base_text), 'flags': [{'kind': 'ocr_failed', 'bbox': None, 'text': '',
                'reason': '位置付きOCRを完了できませんでした: ' + str(error)}], 'views': [],
                'evidence': [], 'supplements': [], 'machine_status': 'failed', 'status': 'failed'}
    retries = []
    selected = []
    for flag in sorted(result['flags'], key=lambda f: f['kind'] != 'approximate_name'):
        if flag['kind'] not in ('approximate_name', 'incomplete_name', 'unusual_characters') or not flag.get('bbox'):
            continue
        b = flag['bbox']
        if any(_overlap(b, old) >= .6 for old in selected):
            continue
        if len(selected) >= 2 or time.monotonic()-started >= 60:
            break
        selected.append(b)
        x, y = max(0, b['x']-.006), max(0, b['y']-.006)
        extra_y = .045 if flag['kind'] == 'incomplete_name' and b['height'] > b['width'] else .006
        w = min(1-x, b['x']+b['width']+.012-x)
        h = min(1-y, b['y']+b['height']+extra_y-y)
        try:
            region = layout_ocr(path, page, regions=[(x, y, w, h)], rotations=(0,),
                                timeout=max(1, min(30, 90-(time.monotonic()-started))))
            local = analyze_layout(region, ledger=ledger)
            retry = {'bbox': {'x': x, 'y': y, 'width': w, 'height': h}, 'text': local['text'], 'status': local['status']}
            retries.append(retry)
            for entry in local['evidence']:
                if _anchor(entry['text']) and _norm(entry['text']) not in _norm(result['text']):
                    result['text'] += '\n' + entry['text']
                    result['evidence'].append(entry)
                    result['supplements'].append(entry)
                    result['flags'].append({**entry, 'kind': 'region_supplement',
                        'reason': 'この領域を再OCRして文字を補足しました。最初の読み取りも保持しているため原文と比較してください。'})
            result['views'].extend(_compact_views(region.get('views', [])))
        except Exception as error:
            retries.append({'bbox': {'x': x, 'y': y, 'width': w, 'height': h}, 'status': 'failed', 'text': ''})
            result['flags'].append({'kind': 'region_failed', 'bbox': retries[-1]['bbox'], 'text': '',
                                    'reason': 'この領域の再OCRを完了できませんでした: '+str(error)})
    result['region_retries'] = retries
    if result['flags']:
        result['status'] = 'needs_review'
    return update_ledger_flags(result['text'], result, ledger) if retries else result
