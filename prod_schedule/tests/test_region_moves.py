"""The supplier moves an order line (same PO + item) to another region sheet."""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn

H = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date']


def row(order, po, item, qty):
    return [order, po, item, f'{item} desc', str(qty), '2026-10-30']


def week(**sheets):
    return {name: [H] + rows for name, rows in sheets.items()}


class DetectionTests(unittest.TestCase):
    def changes(self, before, after):
        info = {}
        statuses, _typos, shipped = app.compute_changes(before, after, moves_out=info)
        shipped_keys = {f'{s}|{r[1]}|{r[2]}' for s, rows in shipped.items() for r in rows}
        return statuses, shipped_keys, info['moves']

    def test_partial_move_like_po_4364(self):
        # real case: DI FITTING 48 -> FIJI 36 + DI FITTING 15
        before = week(**{'DI FITTING': [row('DPL2620', 'PO-4364', 'DFC30PF', 48)], 'FIJI': []})
        after = week(**{'DI FITTING': [row('DPL2620', 'PO-4364', 'DFC30PF', 15)],
                        'FIJI': [row('DPL2620', 'PO-4364', 'DFC30PF', 36)]})
        statuses, shipped, moves = self.changes(before, after)
        self.assertEqual(statuses['FIJI|PO-4364|DFC30PF'], 'moved_in')
        self.assertEqual(statuses['DI FITTING|PO-4364|DFC30PF'], 'partially_moved')
        self.assertEqual(shipped, set())
        self.assertEqual(moves[0]['kind'], 'partial')
        with app.app.test_request_context():
            self.assertIn('DI FITTING 48 → FIJI 36', app.move_text(moves[0]))

    def test_whole_move_is_not_shipped(self):
        before = week(MELBOURNE=[row('D1', 'PO-1', 'A', 10)], QLD=[])
        after = week(MELBOURNE=[], QLD=[row('D1', 'PO-1', 'A', 10)])
        statuses, shipped, moves = self.changes(before, after)
        self.assertEqual(statuses['QLD|PO-1|A'], 'moved_in')
        self.assertEqual(shipped, set())
        self.assertEqual(moves[0]['kind'], 'full')

    def test_move_with_less_total_still_counts_the_shipped_part(self):
        before = week(MELBOURNE=[row('D1', 'PO-1', 'A', 10)], QLD=[])
        after = week(MELBOURNE=[], QLD=[row('D1', 'PO-1', 'A', 6)])
        statuses, shipped, moves = self.changes(before, after)
        self.assertEqual(statuses['QLD|PO-1|A'], 'moved_in')
        self.assertEqual(shipped, {'MELBOURNE|PO-1|A'})          # 4 shipped -> QA BRT rules apply
        self.assertTrue(moves[0]['also_shipped'])

    def test_extra_quantity_for_a_new_region_is_just_new(self):
        before = week(MELBOURNE=[row('D1', 'PO-1', 'A', 10)], QLD=[])
        after = week(MELBOURNE=[row('D1', 'PO-1', 'A', 10)], QLD=[row('D1', 'PO-1', 'A', 5)])
        statuses, _shipped, moves = self.changes(before, after)
        self.assertEqual(statuses['QLD|PO-1|A'], 'new')
        self.assertEqual(moves, [])

    def test_shared_reports(self):
        reports = app.SharedReports({'DI FITTING|PO-4364|DFC30PF': [{'result': 'Pass'}]})
        self.assertEqual(reports.get('FIJI|PO-4364|DFC30PF'), [{'result': 'Pass'}])
        self.assertEqual(reports.get('FIJI|PO-9|X', []), [])


class ApplyTests(unittest.TestCase):
    OLD_KEY, NEW_KEY = 'MELBOURNE|PO-1|A', 'QLD|PO-1|A'
    SPLIT_OLD, SPLIT_NEW = 'DI FITTING|PO-2|B', 'FIJI|PO-2|B'

    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks', 'inspection_attachments', 'inspection_reviews',
                          'outstanding_jobs', 'region_moves'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute("INSERT INTO users (username,password_hash,role,display_name,email) "
                         "VALUES ('yu','x','inspector','Mr. Yu','yu@example.com')")
            self.yu = conn.execute("SELECT id FROM users WHERE username='yu'").fetchone()[0]
            for key, region, item in ((self.OLD_KEY, 'MELBOURNE', 'A'), (self.SPLIT_OLD, 'DI FITTING', 'B')):
                conn.execute("INSERT INTO inspection_tasks (job_key, order_number, region, item_code, status, "
                             "assigned_to, assigned_by) VALUES (?, 'D1', ?, ?, 'In Progress', ?, 'murphy')",
                             (key, region, item, self.yu))
            conn.execute("INSERT INTO inspection_attachments (job_key, insp_index, evidence_type, original_name, "
                         "saved_name, file_path) VALUES (?, 0, 'brt', 'brt.pdf', 'x.pdf', '/tmp/x.pdf')",
                         (self.OLD_KEY,))
        app.save_json(app.INSPECTIONS_CACHE, {self.OLD_KEY: [{'job_key': self.OLD_KEY, 'region': 'MELBOURNE',
                                                              'result': 'Pass', 'inspection_date': '2026-10-01'}]})
        before = week(MELBOURNE=[row('D1', 'PO-1', 'A', 10)], QLD=[],
                      **{'DI FITTING': [row('D2', 'PO-2', 'B', 48)], 'FIJI': []})
        app.save_json(app.CURRENT_FILE, before)
        app.save_json(app.PREVIOUS_FILE, before)
        config = app.load_config()
        config.update(task_notify_emails='lead@example.com')
        app.save_json(app.CONFIG_FILE, config)

    def apply(self):
        after = week(MELBOURNE=[], QLD=[row('D1', 'PO-1', 'A', 10)],
                     **{'DI FITTING': [row('D2', 'PO-2', 'B', 15)], 'FIJI': [row('D2', 'PO-2', 'B', 36)]})
        with mock.patch.object(app, '_smtp_send', return_value=(True, 'sent')) as send, \
                app.app.test_request_context(base_url='https://qc.example.com'):
            app._apply_schedule(after)
        return send

    def test_tasks_and_reports_follow_the_move(self):
        send = self.apply()
        with db_conn() as conn:
            tasks = {r['job_key']: dict(r) for r in conn.execute('SELECT * FROM inspection_tasks')}
            att = conn.execute('SELECT job_key FROM inspection_attachments').fetchone()['job_key']
            outstanding = conn.execute('SELECT COUNT(*) FROM outstanding_jobs').fetchone()[0]
            moves = conn.execute('SELECT COUNT(*) FROM region_moves').fetchone()[0]
        # whole move: same task, now QLD, same inspector and status, reports moved along
        self.assertNotIn(self.OLD_KEY, tasks)
        self.assertEqual((tasks[self.NEW_KEY]['region'], tasks[self.NEW_KEY]['assigned_to'],
                          tasks[self.NEW_KEY]['status']), ('QLD', self.yu, 'In Progress'))
        self.assertEqual(att, self.NEW_KEY)
        cache = app.load_json(app.INSPECTIONS_CACHE, {})
        self.assertEqual(cache[self.NEW_KEY][0]['region'], 'QLD')
        self.assertNotIn(self.OLD_KEY, cache)
        # partial move: original task kept, new FIJI task for the same inspector
        self.assertEqual(tasks[self.SPLIT_OLD]['assigned_to'], self.yu)
        self.assertEqual(tasks[self.SPLIT_NEW]['assigned_to'], self.yu)
        self.assertIn('DI FITTING', tasks[self.SPLIT_NEW]['assign_note'])
        # nothing counted as shipped, history recorded
        self.assertEqual(outstanding, 0)
        self.assertEqual(moves, 2)
        # one region-move e-mail to the lead and the inspector; no "new task" e-mail
        subjects = [c.args[0] for c in send.call_args_list]
        self.assertTrue(any('订单转区' in s for s in subjects))
        self.assertFalse(any('新任务' in s for s in subjects))
        move_call = next(c for c in send.call_args_list if '订单转区' in c.args[0])
        self.assertEqual(set(move_call.args[2]), {'lead@example.com', 'yu@example.com'})

    def test_preview_and_pages(self):
        after = week(MELBOURNE=[], QLD=[row('D1', 'PO-1', 'A', 10)],
                     **{'DI FITTING': [row('D2', 'PO-2', 'B', 15)], 'FIJI': [row('D2', 'PO-2', 'B', 36)]})
        with app.app.test_request_context():
            summary = app.schedule_diff_summary(app.load_schedule(app.CURRENT_FILE), after)
        self.assertEqual(len(summary['moves']), 2)
        self.assertEqual(summary['shipped'], [])
        self.assertEqual(summary['new'], [])
        self.assertEqual(summary['partial'], [])

        self.apply()
        with db_conn() as conn:
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('boss','x','admin')")
            boss = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
        app.save_json(app.INSPECTIONS_CACHE, {self.SPLIT_OLD: [{'result': 'Pass', 'inspection_date': '2026-10-02',
                                                                'inspector_name': 'Mr. Yu'}]})
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = boss
        page = client.get('/?sheet=FIJI').get_data(as_text=True)
        self.assertIn('转入', page)
        self.assertIn('DI FITTING 48 → FIJI 36', page)
        page = client.get(f'/inspect/{self.SPLIT_NEW}').get_data(as_text=True)
        self.assertIn('同 PO + 产品在其他地区的检验报告', page)
        pdf = client.get(f'/inspect/{self.SPLIT_NEW}/report.pdf')
        self.assertEqual(pdf.status_code, 302)                      # shared report of DI FITTING
        self.assertIn('DI%20FITTING', pdf.headers['Location'])


if __name__ == '__main__':
    unittest.main()
