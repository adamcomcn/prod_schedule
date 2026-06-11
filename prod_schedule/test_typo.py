import sys, glob, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as a

docs = r'C:\Users\JWan\Documents'
all_xlsx = glob.glob(os.path.join(docs, '*.xlsx'))
prev_path = next(p for p in all_xlsx if '5.14' in p)
curr_path = next(p for p in all_xlsx if '05.21' in p or '5.21' in p)

with open(prev_path, 'rb') as f:
    prev = a.parse_excel(f.read(), os.environ.get('EXCEL_PASSWORD', ''))
with open(curr_path, 'rb') as f:
    curr = a.parse_excel(f.read(), os.environ.get('EXCEL_PASSWORD', ''))

statuses, typo_flags, shipped_rows = a.compute_changes(prev, curr)

counts = {}
for v in statuses.values():
    counts[v] = counts.get(v, 0) + 1

print("Status counts:", counts)
print(f"\nTypo flags found: {len(typo_flags)}")
for t in typo_flags:
    print()
    print(f"  [{t['sheet']}] {t['reason']} — {t['score']}% similar")
    print(f"    This week : Order={t['curr_order']}  Item={t['curr_item']}")
    print(f"    Last week : Order={t['prev_order']}  Item={t['prev_item']}")
