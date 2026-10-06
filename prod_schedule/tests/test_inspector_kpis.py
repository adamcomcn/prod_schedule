"""Inspector KPIs on the dashboard: who is counted and how on-time is decided."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

WEEKS = ['2026-W39', '2026-W40', '2026-W41']
USERS = [
    {'id': 1, 'username': 'admin', 'display_name': 'Jayson', 'role': 'admin'},
    {'id': 2, 'username': 'murphy', 'display_name': 'Murphy', 'role': 'lead'},
    {'id': 3, 'username': 'yu', 'display_name': '于工', 'role': 'inspector'},
    {'id': 4, 'username': 'hq1', 'display_name': '', 'role': 'hq'},
]


def report(name, day, result='Pass'):
    return {'inspector_name': name, 'inspection_date': day, 'result': result}


def kpis(inspections, est_map=None):
    return dict(app.inspector_kpis(inspections, est_map or {}, USERS, WEEKS))


class InspectorKpiTests(unittest.TestCase):
    def test_admin_and_hq_reports_are_left_out(self):
        stats = kpis({'J1': [report('admin', '2026-10-02'), report('Jayson', '2026-10-02'),
                             report('HQ1', '2026-10-02'), report('murphy', '2026-10-01')]})
        self.assertEqual(list(stats), ['Murphy'])

    def test_reports_are_grouped_per_account_whatever_name_was_typed(self):
        stats = kpis({'J1': [report('murphy', '2026-10-01'), report('Murphy ', '2026-10-02')],
                      'J2': [report('YU', '2026-10-01'), report('于工', '2026-10-01', 'Fail')]})
        self.assertEqual(stats['Murphy']['total'], 2)
        self.assertEqual((stats['于工']['total'], stats['于工']['failed']), (2, 1))

    def test_names_without_an_account_are_kept(self):
        stats = kpis({'J1': [report('New Hire', '2026-10-01'), report('new hire', '2026-10-02')]})
        self.assertEqual(stats['New Hire']['total'], 2)

    def test_on_time_uses_real_dates_even_with_remarks_in_the_cell(self):
        stats = kpis({'A': [report('murphy', '2026-10-01')],     # after 15 Jun -> late
                      'B': [report('murphy', '2026-10-20')],     # on 24 Oct -> on time
                      'C': [report('murphy', '2026-10-24')],     # same day -> on time
                      'D': [report('murphy', '2026-10-01')],     # TBC -> no date
                      'E': [report('murphy', '2026-10-01')]},    # no est at all
                     {'A': '2026/6/15 ready for ship', 'B': 'the components are completed 2026/10/24',
                      'C': '24/10/2026', 'D': 'TBC'})
        s = stats['Murphy']
        self.assertEqual((s['on_time'], s['late'], s['no_est']), (2, 1, 2))
        self.assertEqual((s['rated'], s['ontime_pct'], s['small_sample']), (3, 67, True))

    def test_counts_weekly_series_and_partial(self):
        stats = kpis({'A': [report('yu', '2026-09-28'), report('yu', '2026-10-05', 'Partial Pass'),
                            report('yu', '2026-10-06', 'Fail'), report('yu', '', 'Pass')]})
        s = stats['于工']
        self.assertEqual((s['total'], s['passed'], s['failed'], s['partial']), (4, 2, 1, 1))
        self.assertEqual(s['weekly'], {'2026-W39': 0, '2026-W40': 1, '2026-W41': 2})
        self.assertEqual((s['max_weekly'], s['last_date']), (2, '2026-10-06'))


class DashboardKpiTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for t in ('users', 'weekly_snapshots', 'outstanding_jobs', 'inspection_tasks'):
                conn.execute(f'DELETE FROM {t}')
            for name, role in (('admin', 'admin'), ('murphy', 'lead')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (name, generate_password_hash('x' * 12), role))
            self.uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,est_completion,status) "
                         "VALUES ('MEL|PO1|I1','D1','MEL','I1','2026/10/24 ready','Completed')")
        cfg = app.load_config(); cfg['modules'] = {}; app.save_json(app.CONFIG_FILE, cfg)
        app.save_json(app.CURRENT_FILE, {}); app.save_json(app.PREVIOUS_FILE, {})
        app.save_json(app.INSPECTIONS_CACHE, {
            'MEL|PO1|I1': [report('murphy', '2026-10-01', 'Fail'), report('murphy', '2026-10-30')],
            'MEL|PO2|I2': [report('admin', '2026-10-02')],
        })

    def test_dashboard_lists_murphy_only(self):
        client = app.app.test_client()
        with client.session_transaction() as s:
            s['user_id'] = self.uid
        page = client.get('/dashboard').get_data(as_text=True)
        kpi = page[page.index('Inspector KPIs') if 'Inspector KPIs' in page else page.index('检验员 KPI'):]
        self.assertIn('murphy', kpi)
        self.assertNotIn('>admin<', kpi.replace('\n', '').replace(' ', ''))
        self.assertIn('不计入', page)


if __name__ == '__main__':
    unittest.main()
