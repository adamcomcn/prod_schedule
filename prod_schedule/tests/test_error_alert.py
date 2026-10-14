"""Unhandled page errors are e-mailed to the admins, rate limited, without form contents."""
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
from werkzeug.security import generate_password_hash


class ErrorAlertTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app._error_alerts['sent'].clear(); app._error_alerts['repeats'].clear(); app._error_alerts['recent'].clear()
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute("INSERT INTO users (username,password_hash,role,email) VALUES ('boss',?,'admin','boss@example.com')",
                         (generate_password_hash('x' * 12),))

    def fail(self, sent, path='/reports'):
        try:
            raise ValueError('boom')
        except ValueError as exc:
            with app.app.test_request_context(path, method='POST', data={'password': 'secret-value'}), \
                    mock.patch.object(app, 'smtp_configured', return_value=True), \
                    mock.patch.object(app, '_smtp_send', side_effect=lambda *a: sent.append(a) or (True, 'ok')):
                app.notify_error(exc)

    def test_admin_gets_one_mail_per_hour_with_repeat_count(self):
        sent = []
        self.fail(sent)
        self.assertEqual(len(sent), 1)
        subject, body, recipients = sent[0][:3]
        self.assertIn('ValueError', subject)
        self.assertEqual(recipients, ['boss@example.com'])
        self.assertIn('/reports', body)
        self.assertNotIn('secret-value', body)                 # form contents are never sent
        self.fail(sent); self.fail(sent)
        self.assertEqual(len(sent), 1)                         # same error within the hour: quiet
        with mock.patch.object(app.time, 'time', return_value=app.time.time() + 3700):
            self.fail(sent)
        self.assertEqual(len(sent), 2)
        self.assertIn('2 more time', sent[1][1])


if __name__ == '__main__':
    unittest.main()
