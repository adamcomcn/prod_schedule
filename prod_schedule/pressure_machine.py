"""Pressure test machine exports (Dalian factory).

The machine's MCGS touch screen exports one row per test as CSV (GB18030):
    "MCGS_TIME","MCGS_TIMEMS","C保压时间","C阀门型号","C试验结果","C试验内容","C产品编号","C试验压力"
    2026/06/16 07:38:47,670,60.000000,"DN150*6度","合格","壳体试验","承口弯头",2.493333
The same columns saved as .xlsx are read too. Columns are found by keyword,
so a re-ordered or renamed export still works as long as it has a result and
a pressure column.
"""
import csv
import io
import re
from datetime import datetime

MAX_ROWS = 3000
# Rows listed in the PDF report; the original file is attached for the rest.
PDF_MAX_ROWS = 400

FIELDS = ('time', 'hold', 'model', 'ok', 'content', 'product', 'pressure')

# (field, keywords): the first header containing one of the keywords wins
_COLUMNS = (
    ('hold', ('保压时间', '保压', 'HOLD')),
    ('result', ('试验结果', '结果', 'RESULT')),
    ('pressure', ('试验压力', '压力', 'PRESSURE')),
    ('model', ('阀门型号', '型号', '规格', 'MODEL', 'SIZE')),
    ('content', ('试验内容', '内容', '项目', 'TEST')),
    ('product', ('产品编号', '产品', 'PRODUCT')),
    ('time', ('MCGS_TIME', '日期时间', '时间', '日期', 'TIME', 'DATE')),
)


class MachineFileError(ValueError):
    def __init__(self, zh, en):
        super().__init__(en)
        self.zh, self.en = zh, en


def is_machine_file(filename):
    return (filename or '').lower().rsplit('.', 1)[-1] in ('csv', 'xlsx', 'xlsm')


def _clean_header(value):
    text = ' '.join(str(value or '').split())
    # MCGS names string variables "C…" (C保压时间); drop that prefix
    if len(text) > 1 and text[0] in 'Cc' and '一' <= text[1] <= '鿿':
        text = text[1:]
    return text.upper()


def _map_columns(header):
    names = [_clean_header(h) for h in header]
    used, mapping = set(), {}
    for field, keys in _COLUMNS:
        for i, name in enumerate(names):
            if i in used or name == 'MCGS_TIMEMS':
                continue
            if any(k in name for k in keys):
                mapping[field] = i
                used.add(i)
                break
    return mapping


def _number(value):
    if value is None or value == '':
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r'-?\d+(?:\.\d+)?', str(value))
    return float(m.group()) if m else None


def _result(value):
    text = str(value or '').strip().upper()
    if not text:
        return None
    if any(k in text for k in ('不合格', 'NG', 'FAIL', 'NOK', '×')):
        return False
    if any(k in text for k in ('合格', 'OK', 'PASS', '√')):
        return True
    return None


def _time_text(value):
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    text = ' '.join(str(value or '').split())
    return text.replace('/', '-')


def _decode(data):
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('latin-1')


def _rows_from_csv(data):
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def _rows_from_xlsx(data):
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise MachineFileError('无法打开这个 Excel 文件', 'This Excel file cannot be opened') from exc
    try:
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            if any(_map_columns(r).keys() >= {'result', 'pressure'} for r in rows[:10]):
                return rows
        return [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)] if wb.worksheets else []
    finally:
        wb.close()


def parse_machine_file(data, filename):
    """Rows of a machine export: [{'time', 'hold', 'model', 'ok', 'content',
    'product', 'pressure'}]. Raises MachineFileError when the file is not one."""
    ext = (filename or '').lower().rsplit('.', 1)[-1]
    if ext == 'csv':
        raw = _rows_from_csv(data)
    elif ext in ('xlsx', 'xlsm'):
        raw = _rows_from_xlsx(data)
    else:
        raise MachineFileError('请上传打压机导出的 CSV 或 Excel (.xlsx) 文件',
                               'Upload the CSV or Excel (.xlsx) file exported by the machine')
    # the header is the first of the first 10 rows that has a result and a pressure column
    start, mapping = None, {}
    for i, row in enumerate(raw[:10]):
        m = _map_columns(row)
        if {'result', 'pressure'} <= m.keys():
            start, mapping = i, m
            break
    if start is None:
        raise MachineFileError('无法识别：文件里没有“试验结果”和“试验压力”列',
                               'Not recognised: the file has no "result" and "pressure" columns')

    def cell(row, field):
        i = mapping.get(field)
        return row[i] if i is not None and i < len(row) else None

    rows = []
    for row in raw[start + 1:]:
        if not any(str(v or '').strip() for v in row):
            continue
        pressure, ok = _number(cell(row, 'pressure')), _result(cell(row, 'result'))
        if pressure is None and ok is None:
            continue
        rows.append({'time': _time_text(cell(row, 'time')), 'hold': _number(cell(row, 'hold')),
                     'model': ' '.join(str(cell(row, 'model') or '').split()), 'ok': ok,
                     'content': ' '.join(str(cell(row, 'content') or '').split()),
                     'product': ' '.join(str(cell(row, 'product') or '').split()),
                     'pressure': pressure})
        if len(rows) > MAX_ROWS:
            raise MachineFileError(f'记录太多（超过 {MAX_ROWS} 条），请按订单分开导出',
                                   f'Too many rows (over {MAX_ROWS}); export one order at a time')
    if not rows:
        raise MachineFileError('文件里没有测试记录', 'The file has no test records')
    return rows


def row_flags(row, min_pressure=None, min_hold=None):
    """Reasons a test row needs attention: 'fail', 'pressure', 'hold'."""
    flags = []
    if row.get('ok') is False:
        flags.append('fail')
    if min_pressure and row.get('pressure') is not None and row['pressure'] < min_pressure:
        flags.append('pressure')
    if min_hold and row.get('hold') is not None and row['hold'] < min_hold:
        flags.append('hold')
    return flags


def summarise(rows, min_pressure=None, min_hold=None, quantity=None):
    """Counts and ranges of a machine export, checked against the limits in Settings."""
    def rng(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return (min(vals), max(vals)) if vals else (None, None)

    groups = {}
    for r in rows:
        key = (r.get('model', ''), r.get('product', ''), r.get('content', ''))
        g = groups.setdefault(key, {'model': key[0], 'product': key[1], 'content': key[2],
                                    'tests': 0, 'passed': 0, 'failed': 0})
        g['tests'] += 1
        g['passed'] += r.get('ok') is True
        g['failed'] += r.get('ok') is False
    flags = [row_flags(r, min_pressure, min_hold) for r in rows]
    times = sorted(r['time'] for r in rows if r.get('time'))
    p_min, p_max = rng('pressure')
    h_min, h_max = rng('hold')
    passed = sum(r.get('ok') is True for r in rows)
    summary = {
        'tests': len(rows), 'passed': passed, 'failed': sum(r.get('ok') is False for r in rows),
        'low_pressure': sum('pressure' in f for f in flags), 'short_hold': sum('hold' in f for f in flags),
        'pressure_min': p_min, 'pressure_max': p_max, 'hold_min': h_min, 'hold_max': h_max,
        'first': times[0] if times else '', 'last': times[-1] if times else '',
        'groups': list(groups.values()),
        'min_pressure': min_pressure or None, 'min_hold': min_hold or None,
        'quantity': quantity, 'short_count': bool(quantity and passed < quantity),
    }
    summary['attention'] = bool(summary['failed'] or summary['low_pressure'] or summary['short_hold']
                                or summary['short_count'])
    return summary


def _num_text(value, digits=3):
    if value is None:
        return '—'
    text = f'{value:.{digits}f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text


def summary_lines(summary):
    """[(zh, en)] lines describing a summary (inspection page, PDF)."""
    s = summary
    lines = [(f"共 {s['tests']} 次测试：合格 {s['passed']}，不合格 {s['failed']}",
              f"{s['tests']} tests: {s['passed']} passed, {s['failed']} failed")]
    if s.get('pressure_min') is not None:
        lines.append((f"试验压力 {_num_text(s['pressure_min'])} – {_num_text(s['pressure_max'])} MPa",
                      f"Test pressure {_num_text(s['pressure_min'])} – {_num_text(s['pressure_max'])} MPa"))
    if s.get('hold_min') is not None:
        hold = (_num_text(s['hold_min'], 1) if s['hold_min'] == s['hold_max']
                else f"{_num_text(s['hold_min'], 1)} – {_num_text(s['hold_max'], 1)}")
        lines.append((f'保压时间 {hold} 秒', f'Holding time {hold} s'))
    if s.get('first'):
        when = s['first'] if s['first'] == s['last'] else f"{s['first']} – {s['last']}"
        lines.append((f'测试时间 {when}', f'Tested {when}'))
    if s.get('low_pressure'):
        lines.append((f"{s['low_pressure']} 次压力低于 {_num_text(s['min_pressure'])} MPa",
                      f"{s['low_pressure']} test(s) below {_num_text(s['min_pressure'])} MPa"))
    if s.get('short_hold'):
        lines.append((f"{s['short_hold']} 次保压少于 {_num_text(s['min_hold'], 1)} 秒",
                      f"{s['short_hold']} test(s) held under {_num_text(s['min_hold'], 1)} s"))
    if s.get('short_count'):
        lines.append((f"合格件数 {s['passed']} 少于订单数量 {_num_text(s['quantity'], 0)}",
                      f"{s['passed']} passed is less than the {_num_text(s['quantity'], 0)} ordered"))
    return lines


def codes_from_text(text):
    """Item codes typed in Settings (one per line, or separated by commas / spaces)."""
    seen, out = set(), []
    for code in re.split(r'[\s,;，；]+', text or ''):
        code = code.strip().upper()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def codes_from_workbook(data):
    """Item codes from the "Product Code" (or "Item Code" / "Code" / "编码") column
    of any sheet of an .xlsx file."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise MachineFileError('无法打开这个 Excel 文件', 'This Excel file cannot be opened') from exc
    codes = []
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            for header in rows:
                names = [' '.join(str(h or '').split()).lower() for h in header]
                col = next((i for i, n in enumerate(names)
                            if n in ('product code', 'item code', 'code', 'itemcode', '产品编码', '编码', '料号')),
                           None)
                if col is None:
                    continue
                for row in rows:
                    if col < len(row) and row[col]:
                        codes.append(str(row[col]))
                break
    finally:
        wb.close()
    return codes_from_text('\n'.join(codes))


def code_matches(item_code, codes):
    """True when the item code is in the list (patterns ending in * match a prefix)."""
    code = (item_code or '').strip().upper()
    if not code:
        return False
    for c in codes or []:
        c = c.strip().upper()
        if (c.endswith('*') and code.startswith(c[:-1])) or c == code:
            return True
    return False
