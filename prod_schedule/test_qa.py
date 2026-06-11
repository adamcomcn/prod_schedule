import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as a

docs = r'C:\Users\JWan\Documents'
all_xlsx = glob.glob(os.path.join(docs, '*.xlsx'))
prev_path = next(p for p in all_xlsx if '5.14' in p)
curr_path = next(p for p in all_xlsx if '05.21' in p or '5.21' in p)

with open(prev_path, 'rb') as f: prev = a.parse_excel(f.read(), os.environ.get('EXCEL_PASSWORD', ''))
with open(curr_path, 'rb') as f: curr = a.parse_excel(f.read(), os.environ.get('EXCEL_PASSWORD', ''))

statuses, typo_flags, shipped_rows = a.compute_changes(prev, curr)

print("QA BRT violations (shipped or partially shipped, no QA BRT confirmed):\n")

total = 0
for sheet_name, rows in curr.items():
    if not rows: continue
    headers = rows[0]
    qa_idx = next((i for i, h in enumerate(headers) if 'qa brt' in h.lower()), -1)
    if qa_idx == -1: continue

    for row in rows[1:]:
        ns_order = ns_item = ''
        for i, h in enumerate(headers):
            if h.lower() == 'order number': ns_order = row[i]
            if h.lower() == 'item code':    ns_item  = row[i]
        job_key = f"{sheet_name}|{ns_order}|{ns_item}"
        status  = statuses.get(job_key, '')
        qa_val  = (row[qa_idx] if qa_idx < len(row) else '').lower().strip()
        if status in ('shipped', 'partially_shipped') and qa_val not in ('yes', 'y'):
            print(f"  [{sheet_name}] {ns_order} / {ns_item}  status={status}  QA='{row[qa_idx]}'")
            total += 1

# Also check shipped rows (from prev week)
for sheet_name, srows in shipped_rows.items():
    headers = curr.get(sheet_name, [[]])[0] if curr.get(sheet_name) else []
    if not headers: continue
    qa_idx = next((i for i, h in enumerate(headers) if 'qa brt' in h.lower()), -1)
    if qa_idx == -1: continue
    for row in srows:
        ns_order = ns_item = ''
        for i, h in enumerate(headers):
            if h.lower() == 'order number': ns_order = row[i] if i < len(row) else ''
            if h.lower() == 'item code':    ns_item  = row[i] if i < len(row) else ''
        qa_val = (row[qa_idx] if qa_idx < len(row) else '').lower().strip()
        if qa_val not in ('yes', 'y'):
            print(f"  [{sheet_name}] {ns_order} / {ns_item}  status=SHIPPED(prev)  QA='{row[qa_idx] if qa_idx < len(row) else ''}'")
            total += 1

print(f"\nTotal violations: {total}")
