import sys, os, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as a

docs = r'C:\Users\JWan\Documents'
curr_path = next(p for p in glob.glob(os.path.join(docs, '*.xlsx')) if '05.21' in p or '5.21' in p)
with open(curr_path, 'rb') as f:
    curr = a.parse_excel(f.read(), 'castings1')

print("Item codes where description contains 'valve' (unique prefixes):\n")
prefixes = {}
for sheet, rows in curr.items():
    if not rows: continue
    headers = rows[0]
    desc_idx = next((i for i,h in enumerate(headers) if h.lower() == 'item description'), -1)
    code_idx = next((i for i,h in enumerate(headers) if h.lower() == 'item code'), -1)
    for row in rows[1:]:
        desc = row[desc_idx] if desc_idx >= 0 and desc_idx < len(row) else ''
        code = row[code_idx] if code_idx >= 0 and code_idx < len(row) else ''
        if 'valve' in desc.lower() and code:
            prefix = ''.join(c for c in code if c.isalpha())[:6]
            if prefix not in prefixes:
                prefixes[prefix] = (code, desc[:60])

for p, (code, desc) in sorted(prefixes.items()):
    print(f"  Prefix: {p:<10}  Example code: {code:<25}  {desc}")
