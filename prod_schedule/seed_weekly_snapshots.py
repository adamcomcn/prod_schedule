"""
Populate weekly_snapshots table from all available Excel schedule files.
Run once to seed historical data; safe to re-run (INSERT OR REPLACE).
"""
import sys, openpyxl, sqlite3, pathlib
from datetime import datetime, date

sys.stdout.reconfigure(encoding='utf-8')

BASE   = pathlib.Path(__file__).parent
DB     = BASE / 'data' / 'app.db'
DOCS   = pathlib.Path(r'C:\Users\JWan\Documents')
SKIP   = {'TOOLING', 'LEADTIMES'}

# (filename_glob_part, week_date YYYY-MM-DD, week_label)
WEEKS = [
    ('5.14',  '2026-05-14', '14 May 2026'),
    ('05.21', '2026-05-21', '21 May 2026'),
    ('5.28',  '2026-05-28', '28 May 2026'),
    ('6.4',   '2026-06-04', '4 Jun 2026'),
]

def count_rows(path):
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    result = {}
    for ws in wb.worksheets:
        if ws.title in SKIP:
            continue
        count = 0
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue  # skip header
            if any(v is not None and str(v).strip() for v in row):
                count += 1
        if count > 0:
            result[ws.title] = count
    wb.close()
    return result

conn = sqlite3.connect(DB)
conn.execute('''
    CREATE TABLE IF NOT EXISTS weekly_snapshots (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        week_label   TEXT    NOT NULL,
        week_date    TEXT    NOT NULL,
        region       TEXT    NOT NULL,
        total_orders INTEGER DEFAULT 0,
        UNIQUE(week_label, region)
    )
''')

for glob_part, week_date, week_label in WEEKS:
    matches = list(DOCS.glob(f'Production*Schedule*{glob_part}*.xlsx'))
    if not matches:
        print(f'[SKIP] No file found for {week_label} (looking for *{glob_part}*)')
        continue
    path = matches[0]
    print(f'[{week_label}] Reading {path.name}...')
    counts = count_rows(path)
    for region, total in counts.items():
        conn.execute(
            'INSERT OR REPLACE INTO weekly_snapshots (week_label, week_date, region, total_orders) VALUES (?,?,?,?)',
            (week_label, week_date, region, total)
        )
        print(f'  {region}: {total}')

conn.commit()
conn.close()
print('\nDone. weekly_snapshots populated.')
