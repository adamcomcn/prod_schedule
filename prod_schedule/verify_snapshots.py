import sys, sqlite3
sys.stdout.reconfigure(encoding='utf-8')
from db import init_db
init_db()
conn = sqlite3.connect('data/app.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(
    'SELECT week_label, region, total_orders FROM weekly_snapshots ORDER BY week_date, region'
).fetchall()
print(f'weekly_snapshots: {len(rows)} rows')
for r in rows:
    print(f'  {r["week_label"]:<18} {r["region"]:<16} {r["total_orders"]}')
conn.close()
