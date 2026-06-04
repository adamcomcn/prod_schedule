import sqlite3

conn = sqlite3.connect("data/app.db")
conn.row_factory = sqlite3.Row

# List all tables
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print("Tables:", [t[0] for t in tables])
print()

# Check for schedule-related tables
for t in tables:
    name = t[0]
    if any(kw in name.lower() for kw in ['schedule', 'shipment', 'dispatch', 'order', 'week', 'plan']):
        cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
        print(f"\nTable: {name}")
        print("  Columns:", [c[1] for c in cols])
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        print(f"  Row count: {count}")

conn.close()
