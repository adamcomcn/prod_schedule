"""Dashboard numbers must add up and agree with each other."""
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from flask import template_rendered
from werkzeug.security import generate_password_hash

H = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date']


def rows(*specs):
    return [H] + [[f'D{n}', f'PO{n}', f'I{n}', 'Part', '5', est] for n, est in specs]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for t in ('users', 'weekly_snapshots', 'outstanding_jobs', 'inspection_tasks'):
                conn.execute(f'DELETE FROM {t}')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('adm',?,'admin')",
                         (generate_password_hash('x' * 12),))
            self.uid = conn.execute("SELECT id FROM users WHERE username='adm'").fetchone()[0]
        cfg = app.load_config(); cfg['modules'] = {}; app.save_json(app.CONFIG_FILE, cfg)
        app.save_json(app.INSPECTIONS_CACHE, {})

    @contextmanager
    def context(self):
        captured = []
        def record(sender, template, context, **extra):
            captured.append(context)
        template_rendered.connect(record, app.app)
        try:
            c = app.app.test_client()
            with c.session_transaction() as s:
                s['user_id'] = self.uid
            resp = c.get('/dashboard')
            self.assertEqual(resp.status_code, 200)
            yield captured[-1], resp.get_data(as_text=True)
        finally:
            template_rendered.disconnect(record, app.app)

    def test_typo_rows_count_as_new_and_parts_add_up(self):
        prev = {'MEL': rows((1, ''), (2, ''))}
        cur = {'MEL': rows((1, ''), (3, ''), (4, ''))}          # 2 shipped, 3 new, 4 new (typo)
        app.save_json(app.PREVIOUS_FILE, prev); app.save_json(app.CURRENT_FILE, cur)
        statuses = {'MEL|PO1|I1': 'not_shipped', 'MEL|PO3|I3': 'new', 'MEL|PO4|I4': 'typo'}
        shipped = {'MEL': [prev['MEL'][2]]}
        with mock.patch.object(app, 'compute_changes', return_value=(statuses, [{}], shipped)):
            with self.context() as (ctx, page):
                t = ctx['totals']
        self.assertEqual((t['new'], t['typo'], t['not_shipped'], t['shipped']), (2, 1, 1, 1))
        self.assertEqual(t['total'], t['new'] + t['not_shipped'] + t['partially_shipped'] + t['shipped'])
        self.assertEqual(ctx['curr_in_schedule'], 3)            # the typo row is in this week's schedule
        self.assertEqual(ctx['prev_total'], 2)

    def test_trend_has_one_point_per_week_and_last_point_is_live(self):
        cur = {'MEL': rows((1, ''), (2, ''), (3, ''))}
        app.save_json(app.PREVIOUS_FILE, cur); app.save_json(app.CURRENT_FILE, cur)
        with db_conn() as conn:
            for label, d, n in (('a', '2026-09-22', 90), ('b', '2026-09-30', 100),
                                ('c', '2026-10-01', 111)):        # b and c are the same ISO week
                conn.execute('INSERT INTO weekly_snapshots (week_label, week_date, region, total_orders) VALUES (?,?,?,?)',
                             (label, d, 'MEL', n))
            conn.execute('INSERT INTO weekly_snapshots (week_label, week_date, region, total_orders) VALUES (?,?,?,?)',
                         ('x', '2026-09-22', 'OLDREG', 0))
        with self.context() as (ctx, page):
            labels, sets = ctx['chart_labels'], ctx['chart_datasets']
        self.assertEqual(labels, ['2026-W39', '2026-W40'])
        self.assertEqual([d['region'] for d in sets], ['MEL'])      # all-zero region dropped
        self.assertEqual(sets[0]['data'], [90, 3])                  # last point = live unique orders
        self.assertEqual(ctx['totals']['total'], 3)

    def test_snapshot_counts_unique_orders(self):
        sheet = [H, ['D1', 'PO1', 'I1', 'x', '5', ''], ['D1', 'PO1', 'I1', 'x', '2', ''],   # split lot
                 ['', '', 'I9', 'x', '1', '']]                                              # no PO / order
        self.assertEqual(app.count_unique_jobs('MEL', sheet), 1)

    def test_kpi_small_sample_and_reconciliation(self):
        cur = {'MEL': rows((1, '2026-12-01'))}
        app.save_json(app.PREVIOUS_FILE, cur); app.save_json(app.CURRENT_FILE, cur)
        with db_conn() as conn:       # the on-time rate reads the estimated date from the task
            conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,est_completion,status) "
                         "VALUES ('MEL|PO1|I1','D1','MEL','I1','2026-12-01','Completed')")
        app.save_json(app.INSPECTIONS_CACHE, {
            'MEL|PO1|I1': [{'result': 'Pass', 'inspector_name': 'murphy', 'inspection_date': '2026-10-01'}],
            'MEL|PO9|I9': [{'result': 'Fail', 'inspector_name': 'murphy', 'inspection_date': '2026-10-02'},
                           {'result': 'Pass', 'inspector_name': 'murphy', 'inspection_date': '2026-10-03'}],
        })
        with self.context() as (ctx, page):
            stats = dict(ctx['inspector_stats'])['murphy']
            self.assertEqual(ctx['inspections_all'], {'jobs': 2, 'reports': 3})
            self.assertEqual(ctx['inspections_outside'], {'jobs': 1, 'reports': 2})
            self.assertEqual(ctx['totals']['insp_pass'], 1)
        self.assertTrue(stats['small_sample'])
        self.assertEqual(stats['rated'], 1)
        self.assertIn('样本不足', page)
        self.assertIn('对账', page)
        self.assertIn('数据口径说明', page)

    def test_region_bar_length_follows_order_count(self):
        cur = {'BIG': rows(*[(n, '') for n in range(10)]), 'SMALL': rows((101, ''), (102, ''))}
        app.save_json(app.PREVIOUS_FILE, cur); app.save_json(app.CURRENT_FILE, cur)
        with self.context() as (ctx, page):
            pass
        self.assertIn('width:100.0%; height:100%', page)        # largest region fills the row
        self.assertIn('width:20.0%; height:100%', page)         # 2 of 10 orders

    def test_trend_chart_has_bar_and_line_modes(self):
        cur = {'MEL': rows((1, ''), (2, ''))}
        app.save_json(app.PREVIOUS_FILE, cur); app.save_json(app.CURRENT_FILE, cur)
        with db_conn() as conn:
            for d in ('2026-09-22', '2026-09-30'):
                conn.execute('INSERT INTO weekly_snapshots (week_label, week_date, region, total_orders) VALUES (?,?,?,?)',
                             (d, d, 'MEL', 5))
        with self.context() as (ctx, page):
            pass
        self.assertIn('data-mode="bar"', page)
        self.assertIn('data-mode="line"', page)
        self.assertIn("stack: 'orders'", page)


if __name__ == '__main__':
    unittest.main()
