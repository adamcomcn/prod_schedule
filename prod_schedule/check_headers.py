import json, pathlib, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

p = pathlib.Path(r'C:\Users\JWan\Documents\prod_schedule\data')

for fname, label in [('previous_week.json','MAY 21'), ('current_week.json','MAY 28')]:
    d = json.load(open(str(p / fname), encoding='utf-8'))
    di = d.get('DI FITTING', [])
    print(f'\n=== {label} — DI FITTING headers ===')
    print(di[0] if di else 'EMPTY')
    print(f'\n=== {label} — DFR151000F rows ===')
    hdr = di[0] if di else []
    po_idx   = next((i for i,h in enumerate(hdr) if 'daemco purchase order' in str(h).lower()), -1)
    ic_idx   = next((i for i,h in enumerate(hdr) if 'item code' in str(h).lower()), -1)
    qty_idx  = next((i for i,h in enumerate(hdr) if h and str(h).lower() == 'quantity'), -1)
    print(f'  po_col={po_idx}, ic_col={ic_idx}, qty_col={qty_idx}')
    for row in di[1:]:
        if ic_idx >= 0 and ic_idx < len(row) and row[ic_idx] == 'DFR151000F':
            po_val  = row[po_idx]  if po_idx  >= 0 else 'N/A'
            qty_val = row[qty_idx] if qty_idx >= 0 else 'N/A'
            print(f'  PO={po_val}  item={row[ic_idx]}  qty={qty_val}')
            print(f'  full row: {row}')
