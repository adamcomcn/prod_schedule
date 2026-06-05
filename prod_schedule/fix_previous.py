import sys, openpyxl, json
from datetime import datetime, date

SKIP = {"TOOLING", "LEADTIMES"}

def fmt(v):
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return str(v)[:10]
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(round(v, 4))
    return str(v).strip()

def read_xlsx(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    result = {}
    for ws in wb.worksheets:
        if ws.title in SKIP:
            continue
        rows = []
        for row in ws.iter_rows(values_only=True):
            r = [fmt(v) for v in row]
            if any(x.strip() for x in r):
                rows.append(r)
        if rows:
            result[ws.title] = rows
    return result

print("Reading Production Schedule 5.28.xlsx...")
data528 = read_xlsx(r"C:\Users\JWan\Documents\Production Schedule 5.28.xlsx")
for sheet, rows in data528.items():
    print(f"  [{sheet}] {len(rows)} rows")

with open("data/previous_week.json", "w", encoding="utf-8") as f:
    json.dump(data528, f, ensure_ascii=False)
print("Written to previous_week.json")

# Verify current is still 6.4
with open("data/current_week.json", encoding="utf-8") as f:
    cur = json.load(f)
print()
print("current_week.json (6.4) check:")
for sheet, rows in cur.items():
    print(f"  [{sheet}] {len(rows)} rows")
print()
print("Fix complete. current=6.4, previous=5.28")
