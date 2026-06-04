import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as a

# Find files by pattern (handles non-breaking spaces in filenames)
docs = r'C:\Users\JWan\Documents'
all_xlsx = glob.glob(os.path.join(docs, '*.xlsx'))
prev_path = next((p for p in all_xlsx if '5.14' in p or '05.14' in p), None)
curr_path = next((p for p in all_xlsx if '05.21' in p or '5.21' in p), None)

print("Found files:")
print(f"  Previous: {prev_path}")
print(f"  Current:  {curr_path}")
assert prev_path and curr_path, "Could not find one or both Excel files"

print("Reading previous week (05.14)...")
with open(prev_path, 'rb') as f:
    prev_data = a.parse_excel(f.read(), 'castings1')

print("Reading current week (05.21)...")
with open(curr_path, 'rb') as f:
    curr_data = a.parse_excel(f.read(), 'castings1')

# Save previous week to disk so the app uses it
a.save_json(a.PREVIOUS_FILE, prev_data)
print("Saved previous_week.json\n")

# Run comparison
statuses = a.compute_changes(prev_data, curr_data)

# Summary per sheet
from collections import defaultdict
sheet_stats = defaultdict(lambda: {'new': [], 'ongoing': [], 'shipped': []})

for key, status in statuses.items():
    sheet = key.split('|')[0]
    order = key.split('|')[1]
    item  = key.split('|')[2]
    sheet_stats[sheet][status].append(f"{order} / {item}")

print("=" * 65)
print(f"{'SHEET':<20} {'NEW':>6} {'ONGOING':>8} {'SHIPPED':>8}  TOTAL")
print("=" * 65)
for sheet in curr_data.keys():
    s = sheet_stats[sheet]
    n, o, sh = len(s['new']), len(s['ongoing']), len(s['shipped'])
    print(f"{sheet:<20} {n:>6} {o:>8} {sh:>8}  {n+o+sh}")

print("=" * 65)
total_new  = sum(len(v['new'])     for v in sheet_stats.values())
total_on   = sum(len(v['ongoing']) for v in sheet_stats.values())
total_ship = sum(len(v['shipped']) for v in sheet_stats.values())
print(f"{'TOTAL':<20} {total_new:>6} {total_on:>8} {total_ship:>8}  {total_new+total_on+total_ship}")

# Show examples
print("\n--- Sample NEW jobs (first 5) ---")
new_examples = [(k,v) for k,v in statuses.items() if v == 'new'][:5]
for k, _ in new_examples:
    parts = k.split('|')
    print(f"  [{parts[0]}] Order: {parts[1]}  Item: {parts[2]}")

print("\n--- Sample SHIPPED jobs (first 5) ---")
ship_examples = [(k,v) for k,v in statuses.items() if v == 'shipped'][:5]
for k, _ in ship_examples:
    parts = k.split('|')
    print(f"  [{parts[0]}] Order: {parts[1]}  Item: {parts[2]}")

print("\n--- Sample ONGOING jobs (first 5) ---")
on_examples = [(k,v) for k,v in statuses.items() if v == 'ongoing'][:5]
for k, _ in on_examples:
    parts = k.split('|')
    print(f"  [{parts[0]}] Order: {parts[1]}  Item: {parts[2]}")

# Cross-check: verify a known ONGOING job really appears in BOTH files
if on_examples:
    test_key = on_examples[0][0]
    sheet, order, item = test_key.split('|')
    in_prev = any(
        a.make_job_key(sheet, row, prev_data[sheet][0]) == test_key
        for row in prev_data[sheet][1:]
    ) if sheet in prev_data and prev_data[sheet] else False
    in_curr = any(
        a.make_job_key(sheet, row, curr_data[sheet][0]) == test_key
        for row in curr_data[sheet][1:]
    ) if sheet in curr_data and curr_data[sheet] else False
    print(f"\nVerify ONGOING key '{test_key}':")
    print(f"  In previous week: {in_prev}")
    print(f"  In current week:  {in_curr}")
    print(f"  Logic correct: {in_prev and in_curr}")

# Cross-check: verify a SHIPPED job is in prev but NOT in curr
if ship_examples:
    test_key = ship_examples[0][0]
    sheet, order, item = test_key.split('|')
    in_prev = any(
        a.make_job_key(sheet, row, prev_data[sheet][0]) == test_key
        for row in prev_data[sheet][1:]
    ) if sheet in prev_data and prev_data[sheet] else False
    in_curr = any(
        a.make_job_key(sheet, row, curr_data[sheet][0]) == test_key
        for row in curr_data[sheet][1:]
    ) if sheet in curr_data and curr_data[sheet] else False
    print(f"\nVerify SHIPPED key '{test_key}':")
    print(f"  In previous week: {in_prev}")
    print(f"  In current week:  {in_curr}")
    print(f"  Logic correct: {in_prev and not in_curr}")

# Cross-check: verify a NEW job is in curr but NOT in prev
if new_examples:
    test_key = new_examples[0][0]
    sheet, order, item = test_key.split('|')
    in_prev = any(
        a.make_job_key(sheet, row, prev_data[sheet][0]) == test_key
        for row in prev_data[sheet][1:]
    ) if sheet in prev_data and prev_data[sheet] else False
    in_curr = any(
        a.make_job_key(sheet, row, curr_data[sheet][0]) == test_key
        for row in curr_data[sheet][1:]
    ) if sheet in curr_data and curr_data[sheet] else False
    print(f"\nVerify NEW key '{test_key}':")
    print(f"  In previous week: {in_prev}")
    print(f"  In current week:  {in_curr}")
    print(f"  Logic correct: {not in_prev and in_curr}")
