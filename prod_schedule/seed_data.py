import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as a

docs = r'C:\Users\JWan\Documents'
all_xlsx = glob.glob(os.path.join(docs, '*.xlsx'))
path = next((p for p in all_xlsx if '05.21' in p or '5.21' in p), None)
assert path, "Could not find 05.21 Excel file"
print(f"Reading: {path}")

with open(path, 'rb') as f:
    file_bytes = f.read()

data = a.parse_excel(file_bytes, 'castings1')
a.save_json(a.CURRENT_FILE, data)

config = a.load_config()
config['upload_date'] = '21 May 2026'
a.save_json(a.CONFIG_FILE, config)

print("Seeded current_week.json:")
for sheet, rows in data.items():
    print(f"  {sheet}: {len(rows)} rows")
