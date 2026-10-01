"""A Supplier column added to the schedule later fills in existing tasks."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn

OLD_H = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Quantity', 'Estimated Completion Date']
NEW_H = OLD_H + ['Supplier']


class SupplierSyncTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM inspection_tasks')
            for n, status in ((1, 'Pending'), (2, 'Completed'), (3, 'Pending')):
                conn.execute(
                    "INSERT INTO inspection_tasks (job_key,order_number,region,item_code,quantity,est_completion,status,supplier) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (f'MEL|PO{n}|I{n}', f'D{n}', 'MEL', f'I{n}', '5', '2026-10-30', status,
                     'Keep Co' if n == 3 else ''))
        old = {'MEL': [OLD_H] + [[f'D{n}', f'PO{n}', f'I{n}', '5', '2026-10-30'] for n in (1, 2, 3)]}
        app.save_json(app.CURRENT_FILE, old)
        app.save_json(app.PREVIOUS_FILE, old)

    def test_new_supplier_column_fills_existing_tasks_but_never_blanks(self):
        new = {'MEL': [NEW_H,
                       ['D1', 'PO1', 'I1', '5', '2026-10-30', 'XM'],
                       ['D2', 'PO2', 'I2', '5', '2026-10-30', 'RB'],
                       ['D3', 'PO3', 'I3', '5', '2026-10-30', '']]}
        with app.app.test_request_context():
            app._apply_schedule(new)
        with db_conn() as conn:
            got = {r['job_key']: r['supplier'] for r in conn.execute('SELECT job_key, supplier FROM inspection_tasks')}
        self.assertEqual(got, {'MEL|PO1|I1': 'XM', 'MEL|PO2|I2': 'RB', 'MEL|PO3|I3': 'Keep Co'})


if __name__ == '__main__':
    unittest.main()
