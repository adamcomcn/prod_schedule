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

    def test_far_date_not_reminded(self):
        self.add((date.today() + timedelta(days=40)).isoformat())
        with app.app.test_request_context():
            self.assertEqual(app.send_due_reminders(), 0)


if __name__ == '__main__':
    unittest.main()
