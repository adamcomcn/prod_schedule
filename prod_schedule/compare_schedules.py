"""
Compare Production Schedule 05.21.xlsx vs 5.28.xlsx (DI FITTING sheet).
Uses Daemco Purchase Order + Item Code as the key — same logic as the app.
"""
import win32com.client, pathlib, os, sys, io
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

docs = pathlib.Path(r"C:\Users\JWan\Documents")
path_21 = str(docs / "Production\xa0Schedule\xa005.21.xlsx")
path_28 = str(docs / "Production\xa0Schedule\xa05.28.xlsx")

xl = win32com.client.Dispatch("Excel.Application")
xl.Visible = False
xl.DisplayAlerts = False

def read_sheet(path, sheet_name):
    abs_path = os.path.abspath(path)
    wb = xl.Workbooks.Open(abs_path, ReadOnly=True)
    ws = wb.Sheets(sheet_name)
    used = ws.UsedRange
    nrows = used.Rows.Count
    ncols = used.Columns.Count
    data = []
    for i in range(1, nrows + 1):
        row = [ws.Cells(i, j).Value for j in range(1, ncols + 1)]
        data.append(row)
    wb.Close(False)
    return data

try:
    di21 = read_sheet(path_21, 'DI FITTING')
    di28 = read_sheet(path_28, 'DI FITTING')
finally:
    xl.Quit()

def find_col(headers, *names):
    hl = [str(h or '').lower() for h in headers]
    for name in names:
        if name.lower() in hl:
            return hl.index(name.lower())
    return -1

hdr21 = di21[0]
hdr28 = di28[0]

# 21st: Priority(0), Order Number(1), Daemco PO(2), Date(3), Reliable(4), Item Code(5), Desc(6), Qty(8)
# 28th: Order Number(0), Daemco PO(1), Date(2), Reliable(3), Item Code(4), Desc(5), Qty(7)
po21  = find_col(hdr21, 'daemco purchase order', 'purchase order')
ic21  = find_col(hdr21, 'item code')
qty21 = find_col(hdr21, 'quantity')
st21  = find_col(hdr21, 'current status', 'status')
desc21= find_col(hdr21, 'item description')
crate21=find_col(hdr21, 'pieces per crate')

po28  = find_col(hdr28, 'daemco purchase order', 'purchase order')
ic28  = find_col(hdr28, 'item code')
qty28 = find_col(hdr28, 'quantity')
st28  = find_col(hdr28, 'current status', 'status')
desc28= find_col(hdr28, 'item description')
crate28=find_col(hdr28, 'pieces per crate')

def parse_rows(data, po_col, ic_col, qty_col, desc_col, crate_col):
    """Group by (PO, item_code) and SUM quantities."""
    groups = defaultdict(lambda: {'qty': 0, 'desc': '', 'crate': 0, 'rows': 0})
    for row in data[1:]:
        po   = str(row[po_col]   or '').strip() if po_col >= 0  else ''
        ic   = str(row[ic_col]   or '').strip() if ic_col >= 0  else ''
        qty  = row[qty_col] or 0               if qty_col >= 0 else 0
        desc = str(row[desc_col] or '').strip() if desc_col >= 0 else ''
        crate= row[crate_col] or 0              if crate_col >= 0 else 0
        if not po or not ic:
            continue
        key = (po, ic)
        groups[key]['qty']  += qty
        groups[key]['rows'] += 1
        if not groups[key]['desc']:
            groups[key]['desc'] = desc
        if not groups[key]['crate'] and crate:
            groups[key]['crate'] = crate
    return groups

g21 = parse_rows(di21, po21, ic21, qty21, desc21, crate21)
g28 = parse_rows(di28, po28, ic28, qty28, desc28, crate28)

keys21 = set(g21.keys())
keys28 = set(g28.keys())

print("=" * 72)
print("DI FITTING: 21 May → 28 May  (key = Daemco PO + Item Code)")
print("=" * 72)

# --- Removed ---
removed = sorted(keys21 - keys28)
print(f"\n[SHIPPED OUT / REMOVED] {len(removed)} PO-lines gone:")
for po, ic in removed:
    info = g21[(po, ic)]
    crate = info['crate']
    crates = f"{info['qty']/crate:.1f} crates" if crate else ''
    print(f"  {po:12s}  {ic:22s}  qty={info['qty']:6.0f}  {crates:12s}  {info['desc'][:50]}")

# --- New ---
added = sorted(keys28 - keys21)
print(f"\n[NEW in 28th] {len(added)} new PO-lines:")
for po, ic in added:
    info = g28[(po, ic)]
    crate = info['crate']
    crates = f"{info['qty']/crate:.1f} crates" if crate else ''
    print(f"  {po:12s}  {ic:22s}  qty={info['qty']:6.0f}  {crates:12s}  {info['desc'][:50]}")

# --- Changed ---
changed = []
for key in sorted(keys21 & keys28):
    q21 = g21[key]['qty']
    q28 = g28[key]['qty']
    if q21 != q28:
        changed.append((key, q21, q28))

print(f"\n[QTY CHANGED] {len(changed)} PO-lines:")
for (po, ic), q21, q28 in changed:
    delta = q28 - q21
    info  = g28[(po, ic)]
    crate = info['crate']
    crates_str = f"({abs(delta)/crate:.1f} crates)" if crate else ''
    arrow = f"SHIPPED {abs(delta):.0f} pcs {crates_str}" if delta < 0 else f"ADDED +{delta:.0f} pcs"
    print(f"  {po:12s}  {ic:22s}  21st={q21:6.0f}  →  28th={q28:6.0f}  ← {arrow}")
    print(f"    {info['desc'][:65]}")

print(f"\n[UNCHANGED] {sum(1 for k in keys21 & keys28 if g21[k]['qty']==g28[k]['qty'])} PO-lines")
print(f"\nSUMMARY: {len(removed)} removed, {len(added)} new, {len(changed)} qty-changed")
