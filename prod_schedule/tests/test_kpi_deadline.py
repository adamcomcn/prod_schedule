"""KPI deadline: a date brought forward leaves the inspector 3 working days;
admins can leave a report out of the on-time rate with a reason."""
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

JOB = 'MELBOURNE|PO-1|RSV0100'


def change(old, new, at):
    """A task_date_changes row recorded at `at` (server time = UTC)."""
    return {'old_est': old, 'new_est': new, 'created_at': at}


class DeadlineRuleTests(unittest.TestCase):
    def test_add_workdays_skips_weekends(self):
        self.assertEqual(app.add_workdays(date(2026, 10, 9), 3), date(2026, 10, 14))    # Fri -> Wed
        self.assertEqual(app.add_workdays(date(2026, 10, 6), 3), date(2026, 10, 9))     # Tue -> Fri
        self.assertEqual(app.add_workdays(date(2026, 10, 10), 0), date(2026, 10, 10))

    def test_rules(self):
        d = app.kpi_deadline
        self.assertEqual(d('2026/10/30 ready', [], 3), (date(2026, 10, 30), False))       # no change
        self.assertEqual(d('', [change('2026-10-14', '2026-10-21', '2026-10-09 01:00:00')], 3),
                         (date(2026, 10, 21), False))                                     # delayed
        self.assertEqual(d('', [change('2026-10-30', '2026-10-20', '2026-10-09 01:00:00')], 3),
                         (date(2026, 10, 20), False))                                     # earlier, time enough
        self.assertEqual(d('', [change('2026-10-30', '2026-10-10', '2026-10-09 01:00:00')], 3),
                         (date(2026, 10, 14), True))                                      # sudden: Fri + 3 wd
        self.assertEqual(d('', [change('2026-10-30', '2026-10-01', '2026-10-09 01:00:00')], 3),
                         (date(2026, 10, 14), True))                                      # moved into the past
        self.assertEqual(d('', [change('', '2026-10-09 ready', '2026-10-09 01:00:00')], 3),
                         (date(2026, 10, 14), True))                                      # date added late
        self.assertEqual(d('', [change('2026-10-30', '2026-10-10', '2026-10-09 01:00:00'),
                                change('2026-10-10', 'TBC', '2026-10-10 01:00:00'),
                                change('TBC', '2026-11-05', '2026-10-12 01:00:00')], 3),
                         (date(2026, 11, 5), False))                                      # removed, then later
        # recorded late in the UTC evening = next day in China
        self.assertEqual(d('', [change('2026-10-30', '2026-10-10', '2026-10-08 20:00:00')], 3),
                         (date(2026, 10, 14), True))

    def test_inspector_kpis_with_deadlines_and_exclusions(self):
        reports = {JOB: [{'inspector_name': 'yu', 'inspection_date': '2026-10-13', 'result': 'Pass'},
                         {'inspector_name': 'yu', 'inspection_date': '2026-10-20', 'result': 'Pass'}]}
        users = [{'id': 1, 'username': 'yu', 'display_name': '', 'role': 'inspector'}]
        deadlines = {JOB: (date(2026, 10, 14), True)}
        s = dict(app.inspector_kpis(reports, {JOB: '2026-10-10'}, users, ['2026-W42'], deadlines=deadlines))['yu']
        self.assertEqual((s['on_time'], s['late'], s['extended'], s['excluded']), (1, 1, 2, 0))
        s = dict(app.inspector_kpis(reports, {}, users, [], deadlines=deadlines, exclusions={(JOB, 1)}))['yu']
        self.assertEqual((s['on_time'], s['late'], s['excluded'], s['ontime_pct']), (1, 0, 1, 100))
        s = dict(app.inspector_kpis(reports, {JOB: '2026-10-10'}, users, []))['yu']   # old behaviour
        self.assertEqual((s['on_time'], s['late']), (0, 2))


class KpiPagesTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for t in ('users', 'inspection_tasks', 'task_date_changes', 'kpi_exclusions', 'outstanding_jobs'):
                conn.execute(f'DELETE FROM {t}')
            for name, role in (('boss', 'admin'), ('murphy', 'lead'), ('yu', 'inspector')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (name, generate_password_hash('x' * 12), role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
            conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,est_completion,status) "
                         "VALUES (?, 'DPL1', 'MELBOURNE', 'RSV0100', '2026-10-10', 'Completed')", (JOB,))
            conn.execute("INSERT INTO task_date_changes (job_key, old_est, new_est, created_at) "
                         "VALUES (?, '2026-10-30', '2026-10-10', '2026-10-09 01:00:00')", (JOB,))
        app.save_json(app.INSPECTIONS_CACHE, {JOB: [
            {'inspector_name': 'yu', 'inspection_date': '2026-10-13', 'result': 'Pass', 'job_key': JOB}]})
        app.save_json(app.CURRENT_FILE, {})
        app.save_json(app.PREVIOUS_FILE, {})
        cfg = app.load_config(); cfg['modules'] = {}; cfg.pop('kpi_grace_workdays', None)
        app.save_json(app.CONFIG_FILE, cfg)

    def client(self, name):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[name]
            s['_csrf_token'] = 'tok'
        return c

    def page(self, name='boss'):
        with mock.patch.object(app, 'find_job', return_value={'job_key': JOB, 'Item Code': 'RSV0100',
                                                             'Item Description': 'DN100 Gate Valve'}):
            return self.client(name).get(f'/inspect/{JOB}').get_data(as_text=True)

    def test_dashboard_counts_the_extension(self):
        page = self.client('boss').get('/dashboard').get_data(as_text=True)
        kpi = page[page.index('检验员 KPI'):]
        self.assertIn('100%', kpi)                       # 10-13 is within the extended deadline 10-14
        self.assertIn('顺延 1', kpi)
        self.assertIn('3 个工作日', page)

    def test_inspection_page_and_exclusion(self):
        page = self.page()
        self.assertIn('考核截止日', page)
        self.assertIn('2026-10-14', page)
        self.assertIn('已顺延到收到变动后 3 个工作日', page)
        self.assertIn('按时', page)
        self.assertNotIn('不计入原因', self.page('murphy'))                       # admins only
        self.assertEqual(self.client('murphy').post(f'/inspect/{JOB}/report/0/kpi', data={
            '_csrf_token': 'tok', 'action': 'exclude', 'reason': 'x'}).status_code, 403)
        boss = self.client('boss')
        boss.post(f'/inspect/{JOB}/report/0/kpi', data={'_csrf_token': 'tok', 'action': 'exclude', 'reason': ''})
        self.assertEqual(app.kpi_exclusions(), {})                                 # reason required
        boss.post(f'/inspect/{JOB}/report/0/kpi', data={'_csrf_token': 'tok', 'action': 'exclude',
                                                        'reason': '工厂停电，无法进厂'})
        self.assertEqual(app.kpi_exclusions()[(JOB, 0)]['reason'], '工厂停电，无法进厂')
        self.assertIn('工厂停电，无法进厂', self.page())
        kpi = self.client('boss').get('/dashboard').get_data(as_text=True)
        self.assertIn('不计入 1', kpi)
        boss.post(f'/inspect/{JOB}/report/0/kpi', data={'_csrf_token': 'tok', 'action': 'include'})
        self.assertEqual(app.kpi_exclusions(), {})
        self.assertEqual(boss.post(f'/inspect/{JOB}/report/5/kpi', data={'_csrf_token': 'tok'}).status_code, 404)

    def test_grace_setting_and_date_change_e_mail(self):
        self.client('boss').post('/settings', data={'_csrf_token': 'tok', 'kpi_grace_workdays': '5'})
        self.assertEqual(app.load_config()['kpi_grace_workdays'], 5)
        self.assertEqual(app.kpi_deadlines({JOB: '2026-10-10'})[JOB], (date(2026, 10, 16), True))
        sent = []
        with app.app.test_request_context(), \
                mock.patch.object(app, 'china_today', return_value=date(2026, 10, 9)), \
                mock.patch.object(app, '_task_recipients', return_value=['m@example.com']), \
                mock.patch.object(app, '_smtp_send', side_effect=lambda s, b, r, **k: (sent.append(b), (True, 'ok'))[1]):
            app._send_date_change_email([dict(job_key=JOB, region='MELBOURNE', order_number='DPL1', item_code='RSV0100',
                                              assigned_to=None, old_est='2026-10-30', new_est='2026-10-10',
                                              old_ship='', new_ship='')])
        self.assertIn('最晚检验日 Inspect by: 2026-10-16（含 5 个工作日准备时间', sent[0])
        self.assertNotIn('考核', sent[0])
        self.assertNotIn('KPI', sent[0])
        self.assertIn('个工作日', self.client('boss').get('/settings').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
