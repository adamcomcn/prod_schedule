"""Inspection checklists (per product type): template format, Excel import
and answer evaluation.

A template version is stored as JSON:

    {"title": "...", "product_photo": true,
     "sections": [{"id": "s1", "name": "Product Marking", "name_zh": "产品标识",
                   "optional": false,
                   "questions": [{"id": "q1", "text": "...", "text_zh": "...",
                                  "type": "yes_no" | "rating" | "number",
                                  "fail_on": "yes" | "no" | "poor",   # yes_no / rating
                                  "min": 300, "max": null, "unit": "μm",  # number
                                  "action": "Reject", "action_zh": "拒收",
                                  "optional": false,      # N/A allowed (IF APPLICABLE)
                                  "photo": "fail" | "always"}]}]}

Question ids stay the same when a template is edited, so answers keep their
meaning across versions; every report stores the version it was filled in.
"""
import io
import re
import secrets

QUESTION_TYPES = ('yes_no', 'rating', 'number')
RATINGS = ('good', 'fair', 'poor')
PHOTO_RULES = ('fail', 'always')
APPLICABLE_RE = re.compile(r'\(\s*if\s+applicable\s*\)', re.I)
RATING_RE = re.compile(r'\[\s*good\s*/\s*fair\s*/\s*poor\s*\]', re.I)


def new_id(prefix='q'):
    return prefix + secrets.token_hex(4)


def _clean(text):
    return ' '.join(str(text or '').split())


def parse_condition(condition, text=''):
    """Turn the Excel 'IF' column into a question type and fail rule:
    'If no' / 'If yes' / 'If Poor' / 'If < 300μm'."""
    cond = _clean(condition)
    low = cond.lower()
    number = re.search(r'([<>])\s*=?\s*([\d.]+)\s*([^\s\d]*)', cond)
    if number:
        value = float(number.group(2))
        value = int(value) if value == int(value) else value
        rule = {'type': 'number', 'unit': number.group(3)}
        rule['min' if number.group(1) == '<' else 'max'] = value
        return rule
    if 'poor' in low or RATING_RE.search(text or ''):
        return {'type': 'rating', 'fail_on': 'poor'}
    if re.search(r'\byes\b', low):
        return {'type': 'yes_no', 'fail_on': 'yes'}
    return {'type': 'yes_no', 'fail_on': 'no'}


def _find_columns(header):
    """Column index per field from the header row ('PART', 'Q.', 'INSPECTION
    GUIDELINE', 'IF', 'WHAT TO DO?' plus optional '... 中文' columns)."""
    cols = {}
    for i, cell in enumerate(header):
        h = _clean(cell).upper()
        if not h:
            continue
        zh = '中文' in h or 'CHINESE' in h
        if h.startswith('PART') or h.startswith('部位'):
            cols['part_zh' if zh else 'part'] = i
        elif h in ('Q.', 'Q', 'NO.', 'NO'):
            cols['num'] = i
        elif 'GUIDELINE' in h or '检查内容' in h:
            cols['text_zh' if zh else 'text'] = i
        elif h in ('IF', 'CONDITION'):
            cols['condition'] = i
        elif 'WHAT TO DO' in h or 'ACTION' in h or '处理' in h:
            cols['action_zh' if zh else 'action'] = i
    return cols


def parse_checklist_workbook(file_bytes):
    """Read a checklist in the Daemco Excel layout into a template dict.
    Raises ValueError when the layout is not recognised."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    header_idx = next((i for i, r in enumerate(rows)
                       if any(_clean(c).upper() in ('Q.', 'Q') for c in r)), None)
    if header_idx is None:
        raise ValueError('No header row with a "Q." column was found')
    cols = _find_columns(rows[header_idx])
    if 'text' not in cols or 'num' not in cols:
        raise ValueError('The header row needs "Q." and "INSPECTION GUIDELINE" columns')

    def cell(row, key):
        idx = cols.get(key)
        return _clean(row[idx]) if idx is not None and idx < len(row) else ''

    title = ''
    for row in rows[:header_idx]:
        for c in row:
            if 'CHECKLIST' in _clean(c).upper():
                title = _clean(c)
                break
        if title:
            break

    sections, current = [], None
    for row in rows[header_idx + 1:]:
        text = cell(row, 'text')
        part = cell(row, 'part')
        if part and part.upper() in ('DATE', 'SIGNATURE'):
            continue
        if part and (current is None or part != current['_source']):
            optional = bool(APPLICABLE_RE.search(part))
            current = {'id': new_id('s'), '_source': part,
                       'name': APPLICABLE_RE.sub('', part).strip(),
                       'name_zh': APPLICABLE_RE.sub('', cell(row, 'part_zh')).strip(),
                       'optional': optional, 'questions': []}
            sections.append(current)
        if not text:
            continue
        if current is None:
            current = {'id': new_id('s'), '_source': '', 'name': 'General', 'name_zh': '通用',
                       'optional': False, 'questions': []}
            sections.append(current)
        rule = parse_condition(cell(row, 'condition'), text)
        question = {
            'id': new_id(),
            'text': RATING_RE.sub('', APPLICABLE_RE.sub('', text)).strip(),
            'text_zh': RATING_RE.sub('', APPLICABLE_RE.sub('', cell(row, 'text_zh'))).strip(),
            'action': cell(row, 'action'), 'action_zh': cell(row, 'action_zh'),
            'optional': bool(APPLICABLE_RE.search(text)),
            'photo': 'fail', **rule}
        current['questions'].append(question)
    for s in sections:
        s.pop('_source', None)
    sections = [s for s in sections if s['questions']]
    if not sections:
        raise ValueError('No questions were found under the header row')
    return normalise_template({'title': title, 'product_photo': True, 'sections': sections})


def _number(value):
    if value in (None, ''):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) else v


def normalise_template(data):
    """Validate and clean a template coming from the editor or an import.
    Raises ValueError with a readable message."""
    if not isinstance(data, dict):
        raise ValueError('Template must be an object')
    sections = []
    seen = set()
    for s in data.get('sections') or []:
        name = _clean(s.get('name'))
        if not name:
            raise ValueError('Every section needs a name')
        questions = []
        for q in s.get('questions') or []:
            text = _clean(q.get('text'))
            if not text:
                raise ValueError(f'A question in "{name}" has no text')
            qtype = q.get('type') if q.get('type') in QUESTION_TYPES else 'yes_no'
            qid = str(q.get('id') or '') or new_id()
            while qid in seen:
                qid = new_id()
            seen.add(qid)
            item = {'id': qid, 'text': text, 'text_zh': _clean(q.get('text_zh')), 'type': qtype,
                    'action': _clean(q.get('action')), 'action_zh': _clean(q.get('action_zh')),
                    'optional': bool(q.get('optional')),
                    'photo': q.get('photo') if q.get('photo') in PHOTO_RULES else 'fail'}
            if qtype == 'yes_no':
                item['fail_on'] = 'yes' if q.get('fail_on') == 'yes' else 'no'
            elif qtype == 'rating':
                item['fail_on'] = 'fair' if q.get('fail_on') == 'fair' else 'poor'
            else:
                item['min'], item['max'] = _number(q.get('min')), _number(q.get('max'))
                item['unit'] = _clean(q.get('unit'))
                if item['min'] is None and item['max'] is None:
                    raise ValueError(f'Measurement question "{text}" needs a minimum or a maximum')
            questions.append(item)
        if not questions:
            raise ValueError(f'Section "{name}" has no questions')
        sections.append({'id': str(s.get('id') or '') or new_id('s'), 'name': name,
                         'name_zh': _clean(s.get('name_zh')), 'optional': bool(s.get('optional')),
                         'questions': questions})
    if not sections:
        raise ValueError('The checklist has no sections')
    return {'title': _clean(data.get('title')), 'product_photo': bool(data.get('product_photo', True)),
            'sections': sections}


def evaluate(question, answer):
    """'ok' | 'fail' | 'na' | '' (unanswered) for one answer value."""
    value = '' if answer is None else str(answer).strip().lower()
    if not value:
        return ''
    if value == 'na':
        return 'na'
    if question['type'] == 'yes_no':
        if value not in ('yes', 'no'):
            return ''
        return 'fail' if value == question.get('fail_on', 'no') else 'ok'
    if question['type'] == 'rating':
        if value not in RATINGS:
            return ''
        bad = ('fair', 'poor') if question.get('fail_on') == 'fair' else ('poor',)
        return 'fail' if value in bad else 'ok'
    num = _number(value)
    if num is None:
        return ''
    if question.get('min') is not None and num < question['min']:
        return 'fail'
    if question.get('max') is not None and num > question['max']:
        return 'fail'
    return 'ok'


def question_count(template):
    return sum(len(s['questions']) for s in template.get('sections', []))
