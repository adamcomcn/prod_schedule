"""
Restore previous_week.json = May 21 schedule.
current_week.json (May 28, just uploaded by user) is left unchanged.
Uses msoffcrypto + openpyxl, same as the app's own parse_excel().
"""
import io, json, pathlib
import msoffcrypto, openpyxl

XLSX_PATH = pathlib.Path(r"C:\Users\JWan\Documents") / "Production\xa0Schedule\xa005.21.xlsx"
OUT_PATH  = pathlib.Path(r"C:\Users\JWan\Documents\prod_schedule\data\previous_week.json")
PASSWORD  = 'castings1'

def fmt_date(val):
    s = str(val) if val is not None else ''
    if '00:00:00' in s:
        s = s.replace(' 00:00:00', '')
    return s

# Decrypt
with open(str(XLSX_PATH), 'rb') as f:
    enc = io.BytesIO(f.read())

office_file = msoffcrypto.OfficeFile(enc)
office_file.load_key(password=PASSWORD)
dec = io.BytesIO()
office_file.decrypt(dec)
dec.seek(0)

wb = openpyxl.load_workbook(dec, data_only=True)
print(f"Sheets: {wb.sheetnames}")

result = {}
for sheet_name in wb.sheetnames:
    ws = wb[sheet_name]
    rows = []
    for row in ws.iter_rows(values_only=True):
        r = [fmt_date(c) for c in row]
        if any(v.strip() for v in r):
            rows.append(r)
    result[sheet_name] = rows
    print(f"  [{sheet_name}] {len(rows)} rows")

with open(str(OUT_PATH), 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False)

print(f"\nprevious_week.json written ({OUT_PATH})")
print("current_week.json unchanged — still has May 28 data.")
print("Restart Flask and the schedule page will show correct PARTIAL status.")
