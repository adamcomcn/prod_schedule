"""Completion-date parsing, change e-mails and the two-week reminder."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from test_task_assignment import FakeSMTP, SMTP_ENV


class ParseTests(unittest.TestCase):
    def test_text_after_date(self):
        self.assertEqual(app.split_est('2026/6/15 ready for ship'), (date(2026, 6, 15), 'ready for ship'))
        self.assertEqual(app.split_est('2026/6/7 and ready pending the body arrivals.')[0], date(2026, 6, 7))
        self.assertEqual(app.split_est('2026-05-15 00:00:00'), (date(2026, 5, 15), ''))
        self.assertEqual(app.split_est('TBC'), (None, 'TBC'))

    def test_changed_ignores_formatting(self):
        self.assertFalse(app.est_changed('2026-05-15', '2026/5/15 00:00:00'))
        self.assertTrue(app.est_changed('2026-05-15', '2026-05-22'))
        self.assertTrue(app.est_changed('2026/6/15', '2026/6/15 ready for ship'))


class NotifyTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        FakeSMTP.sent = []
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute('DELETE FROM inspection_tasks')
            conn.execute("INSERT INTO users (username,password_hash,role,email) VALUES ('yu','x','inspector','yu@example.test')")
            self.yu = conn.execute("SELECT id FROM users WHERE username='yu'").fetchone()['id']
        cfg = app.load_config()
        cfg['task_notify_emails'] = 'murphy@example.test'
        app.save_json(app.CONFIG_FILE, cfg)

    def add(self, est, assigned=True):
        with db_conn() as conn:
            conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,est_completion,assigned_to) "
                         "VALUES ('R|PO|I','DPL1','R','I',?,?)", (est, self.yu if assigned else None))
            return dict(conn.execute('SELECT * FROM inspection_tasks').fetchone())

    @mock.patch.dict(os.environ, SMTP_ENV)
    @mock.patch('smtplib.SMTP_SSL', FakeSMTP)
    def test_date_change_mail_goes_to_lead_and_assignee(self):
        t = self.add('2026-06-12')
        with app.app.test_request_context():
            ok, _ = app._send_date_change_email([dict(t, old_est='2026-06-12', new_est='2026-06-05',
                                                      old_ship='', new_ship='')])
        self.assertTrue(ok)
        _, rcpt, raw = FakeSMTP.sent[0]
        self.assertEqual(rcpt, ['murphy@example.test', 'yu@example.test'])
        import email
        body = next(p for p in email.message_from_string(raw).walk() if p.get_content_type() == 'text/plain').get_payload(decode=True).decode()
        self.assertIn('提前 7 天', body)

    def test_date_change_mail_says_how_each_date_moved(self):
        t = self.add('')
        with db_conn() as conn:
            conn.execute("UPDATE users SET display_name='Mr. Yu'")
        changes = [
            # the screenshot case: no date before, a date in the past now, must-ship added
            dict(t, order_number='DPL2607', item_code='RSVSP100ACC', description='DN100 Spigot Gate Valve',
                 old_est='', new_est='2026-06-15 ready for ship', old_ship='', new_ship='2026-10-10'),
            dict(t, order_number='DPL2', item_code='A', old_est='2026-10-01', new_est='2026-10-31', old_ship='', new_ship=''),
            dict(t, order_number='DPL3', item_code='B', old_est='2026-10-01', new_est='2026-10-06', old_ship='', new_ship=''),
            dict(t, order_number='DPL4', item_code='C', old_est='2026-10-20', new_est='2026-10-17',
                 old_ship='2026-11-01', new_ship='2026-11-05'),
            dict(t, order_number='DPL5', item_code='D', old_est='2026-10-30', new_est='2026-10-30 waiting casting',
                 old_ship='2026-11-01', new_ship='2026-11-08'),                  # remark + ship delayed
            dict(t, order_number='DPL6', item_code='E', old_est='2026-10-30', new_est='TBC', old_ship='', new_ship=''),
        ]
        sent = []
        with app.app.test_request_context(), \
                mock.patch.object(app, 'china_today', return_value=date(2026, 10, 9)), \
                mock.patch.object(app, '_smtp_send', side_effect=lambda s, b, r, **k: (sent.append((s, b)), (True, 'ok'))[1]):
            app._send_date_change_email(changes)
        subject, body = sent[0]
        self.assertIn('6 项：延后 3 · 提前 1 · 新增日期 1 · 日期被删除 1', subject)
        self.assertIn('3 delayed, 1 earlier, 1 date added, 1 date removed', subject)
        sections = [l for l in body.splitlines() if l.startswith('■')]
        self.assertEqual(sections, ['■ ⬆ 延后 / Delayed (3)', '■ ⬇ 提前 / Earlier (1)',
                                    '■ ＋ 新增日期 / Date added (1)', '■ ✕ 日期被删除 / Date removed (1)'])
        delayed = body.split('■ ⬆ 延后 / Delayed (3)')[1].split('■')[0]
        self.assertLess(delayed.index('DPL2'), delayed.index('DPL5'))            # 30 d before 7 d
        self.assertLess(delayed.index('DPL5'), delayed.index('DPL3'))            # 7 d before 5 d
        self.assertIn('[⬆ 延后 30 天 / delayed 30 d]', body)
        self.assertIn('[⬇ 提前 3 天 / earlier by 3 d]', body)
        self.assertIn('最迟出货 Must ship: 2026-11-01  →  2026-11-05   [⬆ 延后 4 天 / delayed 4 d]', body)
        self.assertIn('[✎ 只改了备注 / remark only]', body)                    # DPL5 est, grouped as delayed
        added = body.split('■ ＋ 新增日期 / Date added (1)')[1]
        self.assertIn('DPL2607  RSVSP100ACC  DN100 Spigot Gate Valve', added)
        self.assertIn('—  →  2026-06-15 (ready for ship)   [＋ 新增日期 / date added]   ⚠ 新日期已过 116 天', added)
        self.assertIn('已分配 Assigned: Mr. Yu', body)
        self.assertNotIn('备注变化 / remark changed', body)

    @mock.patch.dict(os.environ, SMTP_ENV)
    @mock.patch('smtplib.SMTP_SSL', FakeSMTP)
    def test_reminder_within_two_weeks_sent_once_and_rearmed_on_change(self):
        soon = (app.china_today() + timedelta(days=10)).isoformat()
        self.add(soon + ' ready for ship')
        with app.app.test_request_context():
            self.assertEqual(app.send_due_reminders(), 1)
            self.assertEqual(app.send_due_reminders(), 0)
            with db_conn() as conn:
                conn.execute('UPDATE inspection_tasks SET est_completion=?',
                             ((app.china_today() + timedelta(days=5)).isoformat(),))
            self.assertEqual(app.send_due_reminders(), 1)
        self.assertEqual(len(FakeSMTP.sent), 2)

    def test_failed_send_does_not_consume_reminder(self):
        self.add((app.china_today() + timedelta(days=3)).isoformat())
        with app.app.test_request_context():
            with mock.patch.object(app, '_smtp_send', return_value=(False, 'boom')):
                self.assertEqual(app.send_due_reminders(), 0)
            with mock.patch.object(app, '_smtp_send', return_value=(True, 'ok')):
                self.assertEqual(app.send_due_reminders(), 1)

    def test_far_date_not_reminded(self):
        self.add((app.china_today() + timedelta(days=40)).isoformat())
        with app.app.test_request_context():
            self.assertEqual(app.send_due_reminders(), 0)


if __name__ == '__main__':
    unittest.main()


class EstEditTests(unittest.TestCase):
    HEADERS = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Estimated Completion Date', 'Must Ship Date']

    def setUp(self):
        app.app.config.update(TESTING=True)
        FakeSMTP.sent = []
        from werkzeug.security import generate_password_hash
        with db_conn() as conn:
            for t in ('users', 'inspection_tasks', 'est_overrides', 'task_date_changes'):
                conn.execute(f'DELETE FROM {t}')
            for u, role, mail in (('boss', 'admin', 'boss@example.test'), ('murphy', 'lead', 'murphy@example.test'),
                                  ('yu', 'inspector', 'yu@example.test'),
                                  ('other', 'inspector', 'other@example.test')):
                conn.execute('INSERT INTO users (username,password_hash,role,email) VALUES (?,?,?,?)',
                             (u, generate_password_hash(u * 4), role, mail))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
            conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,est_completion,assigned_to) "
                         "VALUES ('MEL|PO1|ITM','DPL1','MEL','ITM','2026-06-12',?)", (self.ids['yu'],))
        cfg = app.load_config(); cfg['task_notify_emails'] = 'lead@example.test'; cfg['modules'] = {}
        app.save_json(app.CONFIG_FILE, cfg)
        self.sched = lambda est: {'MEL': [self.HEADERS, ['DPL1', 'PO1', 'ITM', est, '2026-06-30']]}
        app.save_json(app.CURRENT_FILE, self.sched('2026-06-12'))

    def client(self, user):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[user]; s['_csrf_token'] = 'tok'
        return c

    @mock.patch.dict(os.environ, SMTP_ENV)
    @mock.patch('smtplib.SMTP_SSL', FakeSMTP)
    def test_admin_edit_updates_everything_and_notifies(self):
        r = self.client('boss').post('/schedule/est', data={
            '_csrf_token': 'tok', 'job_key': 'MEL|PO1|ITM', 'est': '2026-07-12', 'next': '/'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(app.load_json(app.CURRENT_FILE)['MEL'][1][3], '2026-07-12')
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT est_completion FROM inspection_tasks').fetchone()[0], '2026-07-12')
            ov = conn.execute('SELECT * FROM est_overrides').fetchone()
        self.assertEqual((ov['original'], ov['corrected']), ('2026-06-12', '2026-07-12'))
        self.assertEqual(FakeSMTP.sent[0][1], ['lead@example.test', 'yu@example.test'])

    def test_lead_and_inspector_cannot_edit(self):
        for user in ('murphy', 'yu'):                     # the date drives their KPI: admin only
            r = self.client(user).post('/schedule/est', data={
                '_csrf_token': 'tok', 'job_key': 'MEL|PO1|ITM', 'est': '2026-07-12'})
            self.assertEqual(r.status_code, 403)
        self.assertEqual(app.load_json(app.CURRENT_FILE)['MEL'][1][3], '2026-06-12')

    @mock.patch.dict(os.environ, SMTP_ENV)
    @mock.patch('smtplib.SMTP_SSL', FakeSMTP)
    def test_correction_survives_upload_until_supplier_changes_it(self):
        self.client('boss').post('/schedule/est', data={
            '_csrf_token': 'tok', 'job_key': 'MEL|PO1|ITM', 'est': '2026-07-12'})
        data, kept = app._apply_est_overrides(self.sched('2026-06-12'))      # supplier unchanged
        self.assertEqual((data['MEL'][1][3], kept), ('2026-07-12', 1))
        data, kept = app._apply_est_overrides(self.sched('2026-07-01'))      # supplier changed it
        self.assertEqual((data['MEL'][1][3], kept), ('2026-07-01', 0))
        with db_conn() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM est_overrides').fetchone())


class QaBrtSystemTruthTests(unittest.TestCase):
    H = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'QA BRTs Sent?']

    def test_excel_yes_is_not_a_report(self):
        self.assertTrue(app.qa_brt_missing('shipped', []))
        self.assertTrue(app.qa_brt_missing('partially_shipped', None))
        self.assertFalse(app.qa_brt_missing('shipped', [{'result': 'Pass'}]))
        self.assertFalse(app.qa_brt_missing('not_shipped', []))

    def test_mismatch_when_excel_yes_but_no_report(self):
        row = ['D1', 'PO1', 'X', 'YES']
        self.assertTrue(app.qa_excel_mismatch(row, self.H, []))
        self.assertFalse(app.qa_excel_mismatch(row, self.H, [{'result': 'Pass'}]))
        self.assertFalse(app.qa_excel_mismatch(['D1', 'PO1', 'X', ''], self.H, []))

    def test_region_bars_explain_the_symbols(self):
        from werkzeug.security import generate_password_hash
        with db_conn() as conn:
            conn.execute('DELETE FROM users'); conn.execute('DELETE FROM inspection_tasks')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('adm',?,'admin')",
                         (generate_password_hash('x' * 12),))
            uid = conn.execute("SELECT id FROM users WHERE username='adm'").fetchone()[0]
            conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,est_completion,status) "
                         "VALUES ('M|P|I','D1','MELBOURNE','I','2020-01-01','Pending')")
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = uid
        page = c.get('/tasks').get_data(as_text=True)
        self.assertIn('⚠ 逾期 1', page)
        self.assertIn('预计完成日已过、仍未完成的任务数', page)


class TaskPageLabelTests(unittest.TestCase):
    def test_status_overview_labels_are_rendered(self):
        from werkzeug.security import generate_password_hash
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('adm',?,'admin')",
                         (generate_password_hash('x' * 12),))
            uid = conn.execute("SELECT id FROM users WHERE username='adm'").fetchone()[0]
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = uid
        page = c.get('/tasks').get_data(as_text=True)
        self.assertNotIn('{{', page)
        self.assertIn('待处理', page)
