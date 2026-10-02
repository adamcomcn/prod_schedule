"""One-time fix: old shipped jobs with a report but no assignee -> Murphy."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn

HEADERS = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity']
SHIPPED = 'MELBOURNE|PO-1|OLD-A'        # report, no assignee, gone from schedule -> Murphy
STILL_ON = 'MELBOURNE|PO-2|LIVE-B'      # report, no assignee, still scheduled -> unchanged
ASSIGNED = 'MELBOURNE|PO-3|OLD-C'       # report, already assigned -> unchanged
NO_REPORT = 'MELBOURNE|PO-4|OLD-D'      # no report -> unchanged


def report(job):
    return [{'job_key': job, 'result': 'Pass', 'inspector_name': 'x', 'submitted_at': '2026-09-01T10:00:00'}]


class OldReportsFixTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('yu','x','inspector')")
            self.yu = conn.execute("SELECT id FROM users WHERE username='yu'").fetchone()[0]
            for job in (SHIPPED, STILL_ON, NO_REPORT):
                conn.execute("INSERT INTO inspection_tasks (job_key, region, status) VALUES (?, 'MELBOURNE', 'Completed')",
                             (job,))
            conn.execute("INSERT INTO inspection_tasks (job_key, region, status, assigned_to) VALUES (?, 'MELBOURNE', 'Completed', ?)",
                         (ASSIGNED, self.yu))
        schedule = {'MELBOURNE': [HEADERS, ['DPL2', 'PO-2', 'LIVE-B', 'Still on schedule', '5']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {SHIPPED: report(SHIPPED), STILL_ON: report(STILL_ON),
                                              ASSIGNED: report(ASSIGNED)})
        config = app.load_config()
        config.pop(app.OLD_REPORTS_FIX_FLAG, None)
        app.save_json(app.CONFIG_FILE, config)

    def assignees(self):
        with db_conn() as conn:
            return {r['job_key']: (r['assigned_to'], r['assigned_by']) for r in
                    conn.execute('SELECT job_key, assigned_to, assigned_by FROM inspection_tasks')}

    def add_murphy(self):
        with db_conn() as conn:
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('murphy','x','lead')")
            return conn.execute("SELECT id FROM users WHERE username='murphy'").fetchone()[0]

    def test_only_shipped_reported_unassigned_tasks_go_to_murphy(self):
        murphy = self.add_murphy()
        self.assertEqual(app._assign_old_shipped_reports_to_lead(), 1)
        a = self.assignees()
        self.assertEqual(a[SHIPPED], (murphy, 'system'))
        self.assertEqual(a[STILL_ON], (None, ''))
        self.assertEqual(a[ASSIGNED][0], self.yu)
        self.assertEqual(a[NO_REPORT], (None, ''))

    def test_runs_only_once(self):
        self.add_murphy()
        app._assign_old_shipped_reports_to_lead()
        with db_conn() as conn:
            conn.execute('UPDATE inspection_tasks SET assigned_to=NULL WHERE job_key=?', (SHIPPED,))
        self.assertEqual(app._assign_old_shipped_reports_to_lead(), 0)
        self.assertEqual(self.assignees()[SHIPPED][0], None)

    def test_waits_for_murphy_account(self):
        self.assertEqual(app._assign_old_shipped_reports_to_lead(), 0)
        self.assertFalse(app.load_config().get(app.OLD_REPORTS_FIX_FLAG))
        murphy = self.add_murphy()
        self.assertEqual(app._assign_old_shipped_reports_to_lead(), 1)
        self.assertEqual(self.assignees()[SHIPPED][0], murphy)

    def test_report_without_task_gets_task_then_murphy(self):
        murphy = self.add_murphy()
        orphan = 'MELBOURNE|PO-9|NO-TASK'
        cache = app.load_json(app.INSPECTIONS_CACHE)
        cache[orphan] = report(orphan)
        app.save_json(app.INSPECTIONS_CACHE, cache)
        app._assign_old_shipped_reports_to_lead()
        self.assertEqual(self.assignees()[orphan][0], murphy)


if __name__ == '__main__':
    unittest.main()
