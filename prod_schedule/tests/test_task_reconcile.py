"""Task statuses follow the submitted inspection reports."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM inspection_tasks')
            for n, status in ((1, 'Pending'), (2, 'Pending'), (3, 'On Hold')):
                conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,status) VALUES (?,?,?,?,?)",
                             (f'MEL|PO{n}|I{n}', f'D{n}', 'MEL', f'I{n}', status))
        app.save_json(app.INSPECTIONS_CACHE, {
            'MEL|PO1|I1': [{'result': 'Pass'}],
            'MEL|PO2|I2': [{'result': 'Pass'}, {'result': 'Fail'}],      # latest report wins
            'MEL|PO3|I3': [{'result': 'Pass'}],                          # manual On Hold is respected
            'MEL|PO9|I9': [{'result': 'Pass', 'order_number': 'D9', 'region': 'MEL', 'item_code': 'I9'}],
        })

    def test_statuses_follow_reports_and_missing_tasks_are_created(self):
        self.assertEqual(app.reconcile_tasks_with_inspections(), (2, 1))
        with db_conn() as conn:
            got = {r['job_key']: r['status'] for r in conn.execute('SELECT job_key, status FROM inspection_tasks')}
        self.assertEqual(got, {'MEL|PO1|I1': 'Completed', 'MEL|PO2|I2': 'In Progress',
                               'MEL|PO3|I3': 'On Hold', 'MEL|PO9|I9': 'Completed'})
        self.assertEqual(app.reconcile_tasks_with_inspections(), (0, 0))   # idempotent


if __name__ == '__main__':
    unittest.main()
