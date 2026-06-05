import sys, openpyxl, json, shutil
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

print("Step 1: Backing up 5.28 as previous_week.json...")
shutil.copy("data/current_week.json", "data/previous_week.json")
print("  Done.")

print("Step 2: Loading Production Schedule 6.4.xlsx...")
data64 = read_xlsx(r"C:\Users\JWan\Documents\Production Schedule 6.4.xlsx")
for sheet, rows in data64.items():
    print(f"  [{sheet}] {len(rows)} rows (incl header)")

with open("data/current_week.json", "w", encoding="utf-8") as f:
    json.dump(data64, f, ensure_ascii=False)
print("  Written to current_week.json")

print("Step 3: Updating config.json...")
with open("data/config.json", encoding="utf-8") as f:
    cfg = json.load(f)
cfg["upload_date"] = "4 Jun 2026"
with open("data/config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("  Done.")
print()
print("All done! Restart the Flask app to see the new data.")
