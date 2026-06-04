"""
One-time script: import ReferenceData.xlsx into products + product_categories tables.
Adds extra columns (sub_category, crate_qty, inspect_pcs, inspect_mins, inspect_hrs, notes)
to the products table if not already present.
"""
import sqlite3, openpyxl, pathlib

DB_PATH   = pathlib.Path(__file__).parent / 'data' / 'app.db'
XLSX_PATH = pathlib.Path(r'C:\Users\JWan\Documents\ReferenceData.xlsx')

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = ON")

# --- 1. Add extra columns if missing ---
existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
new_cols = [
    ("sub_category",   "TEXT DEFAULT ''"),
    ("crate_qty",      "INTEGER"),
    ("inspect_pcs",    "TEXT DEFAULT ''"),
    ("inspect_mins",   "REAL"),
    ("inspect_hrs",    "REAL"),
]
for col_name, col_def in new_cols:
    if col_name not in existing_cols:
        conn.execute(f"ALTER TABLE products ADD COLUMN {col_name} {col_def}")
        print(f"  Added column: {col_name}")
conn.commit()

# --- 2. Read Excel ---
wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
ws = wb['ReferenceData']
rows = list(ws.iter_rows(values_only=True))

# Skip header row
data_rows = [r for r in rows[1:] if r[0]]  # must have item_code

# --- 3. Build category map (name -> id), creating missing ones ---
def get_or_create_cat(name, code=''):
    r = conn.execute("SELECT id FROM product_categories WHERE name=?", (name,)).fetchone()
    if r:
        return r[0]
    cur = conn.execute(
        "INSERT INTO product_categories (code, name) VALUES (?,?)", (code, name))
    conn.commit()
    return cur.lastrowid

cat_map = {}  # category name -> id

# --- 4. Import products ---
inserted = 0
skipped  = 0

for row in data_rows:
    item_code   = str(row[0]).strip()
    description = str(row[1]).strip() if row[1] else ''
    category    = str(row[2]).strip() if row[2] else ''
    sub_cat     = str(row[3]).strip() if row[3] else ''
    crate_qty   = row[4] if isinstance(row[4], (int, float)) else None
    inspect_pcs = str(row[5]).strip() if row[5] is not None else ''
    inspect_mins= row[6] if isinstance(row[6], (int, float)) else None
    inspect_hrs = row[7] if isinstance(row[7], (int, float)) else None
    notes       = str(row[9]).strip() if row[9] else ''

    # Skip if already imported
    exists = conn.execute(
        "SELECT id FROM products WHERE item_code=?", (item_code,)).fetchone()
    if exists:
        skipped += 1
        continue

    # Category
    if category not in cat_map:
        cat_map[category] = get_or_create_cat(category)
    cat_id = cat_map.get(category)

    conn.execute(
        """INSERT INTO products
           (category_id, item_code, name, description,
            sub_category, crate_qty, inspect_pcs, inspect_mins, inspect_hrs)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (cat_id, item_code, description, notes,
         sub_cat,
         int(crate_qty) if crate_qty is not None else None,
         inspect_pcs,
         float(inspect_mins) if inspect_mins is not None else None,
         float(inspect_hrs) if inspect_hrs is not None else None)
    )
    inserted += 1

conn.commit()
conn.close()

print(f"\nDone. Inserted: {inserted}, Skipped (already existed): {skipped}")
print(f"Categories created: {len(cat_map)}")
for name, cid in sorted(cat_map.items()):
    print(f"  [{cid}] {name}")
