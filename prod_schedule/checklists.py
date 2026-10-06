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

QUESTION_TYPES = ('yes_no', 'rating', 'number', 'text')   # text: recorded only (e.g. a date code)
RATINGS = ('good', 'fair', 'poor')
PHOTO_RULES = ('fail', 'always')
APPLICABLE_RE = re.compile(r'\(\s*if\s+applicable\s*\)', re.I)
RATING_RE = re.compile(r'\[\s*good\s*/\s*fair\s*/\s*poor\s*\]', re.I)
ANSWER_HINT_RE = re.compile(r'\[\s*(y\s*/\s*n|mmyy)\s*\]', re.I)     # [Y/N], [MMYY]
DUAL_ONLY_RE = re.compile(r'^\s*(dual|single)\s+only\s*:', re.I)


def new_id(prefix='q'):
    return prefix + secrets.token_hex(4)


def _clean(text):
    return ' '.join(str(text or '').split())


def _clean_text(text):
    """Like _clean but keeps line breaks (lists inside a question)."""
    lines = [' '.join(line.split()) for line in str(text or '').splitlines()]
    return '\n'.join(line for line in lines if line)


def parse_condition(condition, text=''):
    """Turn the Excel 'IF' column into a question type and fail rule:
    'If no' / 'If yes' / 'If Poor' / 'If < 300μm'."""
    cond = _clean(condition)
    low = cond.lower()
    if cond in ('-', '—'):
        return {'type': 'text'}
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
        elif 'FREQUENCY' in h or '频率' in h:
            cols['hint_zh' if zh else 'hint'] = i
    return cols


def parse_checklist_workbook(file_bytes):
    """Read a checklist in the Daemco Excel layout into a template dict.
    Raises ValueError when the layout is not recognised."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    brt = next((i for i, r in enumerate(rows)
                if {'TEST', 'CRITERIA'} <= {_clean(c).upper() for c in r}), None)
    if brt is not None:
        return _parse_brt_plan(rows, brt)
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
            'hint': cell(row, 'hint'), 'hint_zh': cell(row, 'hint_zh'),
            'optional': bool(APPLICABLE_RE.search(text)),
            'photo': 'fail', **rule}
        current['questions'].append(question)
    for s in sections:
        s.pop('_source', None)
    sections = [s for s in sections if s['questions']]
    if not sections:
        raise ValueError('No questions were found under the header row')
    return normalise_template({'title': title, 'product_photo': True, 'sections': sections})


def parse_criteria(criteria):
    """BRT plan 'Criteria' column: 'If No, Reject & Check 100%' ->
    ({'type': 'yes_no', 'fail_on': 'no'}, 'Reject & Check 100%'); '-' -> text."""
    text = _clean(criteria)
    if text in ('', '-', '—'):
        return {'type': 'text'}, ''
    m = re.match(r'if\s+(yes|no|fail|failed|poor)\b[^,]*,?\s*(.*)$', text, re.I)
    if not m:
        return {'type': 'yes_no', 'fail_on': 'no'}, text
    word, action = m.group(1).lower(), m.group(2).strip()
    if word == 'poor':
        return {'type': 'rating', 'fail_on': 'poor'}, action
    return {'type': 'yes_no', 'fail_on': 'yes' if word == 'yes' else 'no'}, action


def _parse_brt_plan(rows, header_idx):
    """'Batch Release Test' layout: Test / Description / Checking Frequency /
    Result / Criteria; groups are separated by blank rows. Optional Chinese
    columns: 'Test 中文', 'Description 中文', 'Frequency 中文', 'Action 中文'."""
    cols = {}
    for i, c in enumerate(rows[header_idx]):
        h = _clean(c).upper()
        zh = '中文' in h
        if h.startswith('TEST'):
            cols['label_zh' if zh else 'label'] = i
        elif h.startswith('DESCRIPTION'):
            cols['text_zh' if zh else 'text'] = i
        elif 'FREQUENCY' in h:
            cols['hint_zh' if zh else 'hint'] = i
        elif h.startswith('CRITERIA'):
            cols['criteria'] = i
        elif h.startswith('ACTION') and zh:
            cols['action_zh'] = i

    def cell(row, key, keep_lines=False):
        idx = cols.get(key)
        value = row[idx] if idx is not None and idx < len(row) else ''
        return _clean_text(value) if keep_lines else _clean(value)

    title = ''
    for row in rows[:header_idx]:
        for c in row:
            if 'BATCH RELEASE TEST' in _clean(c).upper() or 'CHECKLIST' in _clean(c).upper():
                title = _clean(c)
                break
        if title:
            break

    def section_name(label):
        return re.sub(r'\s*#\s*\d+\s*$', '', label).strip()

    sections, current = [], None
    for row in rows[header_idx + 1:]:
        label, text = cell(row, 'label'), cell(row, 'text', keep_lines=True)
        if label.rstrip(':').upper() in ('RELIABLE SIGNATURE', 'BATCH RESULT (PASS OR REJECT)'):
            break
        if not label or not text:
            if not any(c not in (None, '') for c in row):
                current = None                     # blank row: next group
            continue
        if current is None:
            current = {'id': new_id('s'), 'name': section_name(label),
                       'name_zh': section_name(cell(row, 'label_zh')), 'optional': False, 'questions': []}
            sections.append(current)
        rule, action = parse_criteria(cell(row, 'criteria'))
        text_zh = cell(row, 'text_zh', keep_lines=True)
        label_zh = cell(row, 'label_zh')
        only = DUAL_ONLY_RE.search(text)
        current['questions'].append({
            'id': new_id(),
            'only_if': only.group(1).capitalize() if only else '',
            'text': f'{label} — ' + ANSWER_HINT_RE.sub('', text).strip(),
            'text_zh': ((f'{label_zh} — ' if label_zh else '') + ANSWER_HINT_RE.sub('', text_zh).strip()) if text_zh else '',
            'hint': cell(row, 'hint'), 'hint_zh': cell(row, 'hint_zh'),
            'action': action, 'action_zh': cell(row, 'action_zh'),
            'optional': bool(DUAL_ONLY_RE.search(text)) or bool(APPLICABLE_RE.search(text)),
            'photo': 'always' if ('video' in text.lower() or 'been provided' in text.lower()) else 'fail', **rule})
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
            text = _clean_text(q.get('text'))
            if not text:
                raise ValueError(f'A question in "{name}" has no text')
            qtype = q.get('type') if q.get('type') in QUESTION_TYPES else 'yes_no'
            qid = str(q.get('id') or '') or new_id()
            while qid in seen:
                qid = new_id()
            seen.add(qid)
            item = {'id': qid, 'text': text, 'text_zh': _clean_text(q.get('text_zh')), 'type': qtype,
                    'action': _clean(q.get('action')), 'action_zh': _clean(q.get('action_zh')),
                    'hint': _clean(q.get('hint')), 'hint_zh': _clean(q.get('hint_zh')),
                    'only_if': _clean(q.get('only_if')),
                    'optional': bool(q.get('optional')),
                    'photo': q.get('photo') if q.get('photo') in PHOTO_RULES else 'fail'}
            if qtype == 'yes_no':
                item['fail_on'] = 'yes' if q.get('fail_on') == 'yes' else 'no'
            elif qtype == 'rating':
                item['fail_on'] = 'fair' if q.get('fail_on') == 'fair' else 'poor'
            elif qtype == 'text':
                pass
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
    if question['type'] == 'text':
        return 'ok'
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


ANSWER_LABELS = {'yes': '是 Yes', 'no': '否 No', 'good': '好 Good', 'fair': '一般 Fair', 'poor': '差 Poor',
                 'na': '不适用 N/A'}


def answer_label(value):
    """Bilingual display text of an answer ('yes' -> '是 Yes'; numbers as is)."""
    text = '' if value is None else str(value).strip()
    return ANSWER_LABELS.get(text.lower(), text)


def question_count(template):
    return sum(len(s['questions']) for s in template.get('sections', []))


def applies(question, description):
    """False when the question is limited to items whose description contains
    a keyword (only_if, e.g. 'Dual') and this item's does not."""
    keyword = (question.get('only_if') or '').strip().lower()
    return not keyword or keyword in (description or '').lower()


def fill_not_applicable(template, answers, description):
    """Answers with every question that does not apply to this item set to N/A."""
    answers = dict(answers or {})
    for s in template.get('sections', []):
        for q in s['questions']:
            if not applies(q, description):
                answers[q['id']] = {'v': 'na'}
    return answers


def answer_problems(template, answers, photo_refs, has_product_photo):
    """Everything that still blocks submission: [(ref, reason)] where reason is
    'product_photo' | 'unanswered' | 'na_not_allowed' | 'photo'.
    photo_refs: question ids that have at least one photo or video."""
    problems = []
    if template.get('product_photo') and not has_product_photo:
        problems.append(('product', 'product_photo'))
    for s in template.get('sections', []):
        for q in s['questions']:
            answer = answers.get(q['id']) or {}
            result = evaluate(q, answer.get('v'))
            if not result:
                problems.append((q['id'], 'unanswered'))
            elif result == 'na':
                if not (q.get('optional') or s.get('optional') or q.get('only_if')):
                    problems.append((q['id'], 'na_not_allowed'))
            elif (result == 'fail' or q.get('photo') == 'always') and q['id'] not in photo_refs:
                problems.append((q['id'], 'photo'))
    return problems


def summarise(template, answers):
    """(counts, failed items) for a filled-in checklist."""
    counts = {'ok': 0, 'fail': 0, 'na': 0, 'total': 0}
    failed = []
    for s in template.get('sections', []):
        for n, q in enumerate(s['questions'], 1):
            answer = answers.get(q['id']) or {}
            result = evaluate(q, answer.get('v'))
            counts['total'] += 1
            if result in counts:
                counts[result] += 1
            if result == 'fail':
                failed.append({'id': q['id'], 'section': s['name'], 'section_zh': s.get('name_zh', ''), 'num': n,
                               'text': q['text'], 'text_zh': q.get('text_zh', ''), 'value': answer.get('v'),
                               'unit': q.get('unit', ''), 'action': q.get('action', ''),
                               'action_zh': q.get('action_zh', ''), 'occurrences': answer.get('occ', ''),
                               'supervisor': bool(answer.get('sup')), 'note': answer.get('note', '')})
    return counts, failed


def suggested_result(template, answers):
    """'Fail' when a failed item's action includes Reject, 'Partial Pass' for
    other failed items (rework / clean / re-apply), otherwise 'Pass'."""
    _counts, failed = summarise(template, answers)
    if any('reject' in (f['action'] or '').lower() for f in failed):
        return 'Fail'
    return 'Partial Pass' if failed else 'Pass'
