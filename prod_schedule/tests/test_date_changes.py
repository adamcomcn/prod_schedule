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
        body = email.message_from_string(raw).get_payload(decode=True).decode()
        self.assertIn('提前 7 天', body)

    @mock.patch.dict(os.environ, SMTP_ENV)
    @mock.patch('smtplib.SMTP_SSL', FakeSMTP)
    def test_reminder_within_two_weeks_sent_once_and_rearmed_on_change(self):
        soon = (date.today() + timedelta(days=10)).isoformat()
        self.add(soon + ' ready for ship')
        with app.app.test_request_context():
            self.assertEqual(app.send_due_reminders(), 1)
            self.assertEqual(app.send_due_reminders(), 0)
            with db_conn() as conn:
                conn.execute('UPDATE inspection_tasks SET est_completion=?',
                             ((date.today() + timedelta(days=5)).isoformat(),))
            self.assertEqual(app.send_due_reminders(), 1)
        self.assertEqual(len(FakeSMTP.sent), 2)

    def test_failed_send_does_not_consume_reminder(self):
        self.add((date.today() + timedelta(days=3)).isoformat())
        with app.app.test_request_context():
            with mock.patch.object(app, '_smtp_send', return_value=(False, 'boom')):
                self.assertEqual(app.send_due_reminders(), 0)
            with mock.patch.object(app, '_smtp_send', return_value=(True, 'ok')):
                self.assertEqual(app.send_due_reminders(), 1)

    def test_far_date_not_reminded(self):
        self.add((date.today() + timedelta(days=40)).isoformat())
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
            for u, role, mail in (('murphy', 'lead', 'murphy@example.test'),
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
    def test_lead_edit_updates_everything_and_notifies(self):
        r = self.client('murphy').post('/schedule/est', data={
            '_csrf_token': 'tok', 'job_key': 'MEL|PO1|ITM', 'est': '2026-07-12', 'next': '/'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(app.load_json(app.CURRENT_FILE)['MEL'][1][3], '2026-07-12')
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT est_completion FROM inspection_tasks').fetchone()[0], '2026-07-12')
            ov = conn.execute('SELECT * FROM est_overrides').fetchone()
        self.assertEqual((ov['original'], ov['corrected']), ('2026-06-12', '2026-07-12'))
        self.assertEqual(FakeSMTP.sent[0][1], ['lead@example.test', 'yu@example.test'])

    def test_inspector_cannot_edit(self):
        r = self.client('yu').post('/schedule/est', data={
            '_csrf_token': 'tok', 'job_key': 'MEL|PO1|ITM', 'est': '2026-07-12'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(app.load_json(app.CURRENT_FILE)['MEL'][1][3], '2026-06-12')

    @mock.patch.dict(os.environ, SMTP_ENV)
    @mock.patch('smtplib.SMTP_SSL', FakeSMTP)
    def test_correction_survives_upload_until_supplier_changes_it(self):
        self.client('murphy').post('/schedule/est', data={
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
