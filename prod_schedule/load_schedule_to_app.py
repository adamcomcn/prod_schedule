"""
One-time script: read Production Schedule 05.21.xlsx and 5.28.xlsx via
Excel COM, convert to the JSON format the Flask app uses, and write to
data/previous_week.json  (21st = "previous")
data/current_week.json   (28th = "current")

The app's compute_changes() will then automatically mark rows where
current qty < previous qty as 'partially_shipped'.
"""
import win32com.client, pathlib, os, json, sys
from decimal import Decimal

docs     = pathlib.Path(r"C:\Users\JWan\Documents")
path_21  = str(docs / "Production\xa0Schedule\xa005.21.xlsx")
path_28  = str(docs / "Production\xa0Schedule\xa05.28.xlsx")

BASE_DIR = pathlib.Path(__file__).parent
OUT_PREV = BASE_DIR / 'data' / 'previous_week.json'
OUT_CURR = BASE_DIR / 'data' / 'current_week.json'

SKIP_SHEETS = {'TOOLING', 'LEADTIMES'}

def fmt_val(v):
    """Convert any cell value to a plain string, like the app's fmt_date()."""
    if v is None:
        return ''
    # pywintypes.datetime  →  YYYY-MM-DD
    t = type(v).__name__
    if t == 'datetime':
        try:
            return v.strftime('%Y-%m-%d')
        except Exception:
            return str(v)
    if isinstance(v, Decimal):
        f = float(v)
        return str(int(f)) if f == int(f) else str(round(f, 4))
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(round(v, 4))
    s = str(v).strip()
    # Clean Excel datetime strings
    if '00:00:00' in s:
        s = s.replace(' 00:00:00', '').replace('pywintypes.datetime(', '').rstrip(')')
    return s

xl = win32com.client.Dispatch("Excel.Application")
xl.Visible = False
xl.DisplayAlerts = False

def read_workbook(path):
    abs_path = os.path.abspath(path)
    wb = xl.Workbooks.Open(abs_path, ReadOnly=True)
    result = {}
    for i in range(1, wb.Sheets.Count + 1):
        ws   = wb.Sheets(i)
        name = ws.Name
        if name in SKIP_SHEETS:
            continue
        used  = ws.UsedRange
        nrows = used.Rows.Count
        ncols = used.Columns.Count
        sheet_data = []
        for r in range(1, nrows + 1):
            row = [fmt_val(ws.Cells(r, c).Value) for c in range(1, ncols + 1)]
            # Skip entirely blank rows
            if any(v.strip() for v in row):
                sheet_data.append(row)
        result[name] = sheet_data
        print(f"  [{name}] {len(sheet_data)} rows")
    wb.Close(False)
    return result

try:
    print(f"\nReading May 21 (previous)...")
    data21 = read_workbook(path_21)
    print(f"\nReading May 28 (current)...")
    data28 = read_workbook(path_28)
finally:
    xl.Quit()

OUT_PREV.parent.mkdir(exist_ok=True)
with open(OUT_PREV, 'w', encoding='utf-8') as f:
    json.dump(data21, f, ensure_ascii=False)
with open(OUT_CURR, 'w', encoding='utf-8') as f:
    json.dump(data28, f, ensure_ascii=False)

print(f"\nWritten:")
print(f"  {OUT_PREV}")
print(f"  {OUT_CURR}")
print("\nRestart the Flask app — the main page will now show partially_shipped status.")
