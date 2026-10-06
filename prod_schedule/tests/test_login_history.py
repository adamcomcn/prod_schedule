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

CSRF = 'test-csrf-token'
ANDROID_WECHAT = ('Mozilla/5.0 (Linux; Android 13; V2219A) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/116.0 Mobile Safari/537.36 MicroMessenger/8.0.47')


class LoginHistoryTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app._login_failures.clear()
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute('DELETE FROM login_events')
            conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                         ('hist-admin', generate_password_hash('hist-admin-password'), 'admin'))
            conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                         ('hist-insp', generate_password_hash('hist-insp-password'), 'inspector'))
        self.client = app.app.test_client()

    def login(self, username, password, ip='203.0.113.5', ua=ANDROID_WECHAT, client=None):
        client = client or self.client
        with client.session_transaction() as s:
            s['_csrf_token'] = CSRF
        return client.post('/login', data={'username': username, 'password': password, '_csrf_token': CSRF},
                           headers={'User-Agent': ua}, environ_base={'REMOTE_ADDR': ip})

    def events(self):
        with db_conn() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM login_events ORDER BY id')]

    def test_successful_login_records_ip_time_and_device(self):
        response = self.login('hist-insp', 'hist-insp-password')
        self.assertEqual(response.status_code, 302)
        [event] = self.events()
        self.assertEqual(event['event'], 'login')
        self.assertEqual(event['username'], 'hist-insp')
        self.assertEqual(event['ip'], '203.0.113.5')
        self.assertIn('MicroMessenger', event['user_agent'])
        self.assertIsNotNone(app.parse_server_time(event['created_at']))

    def test_failed_unknown_blocked_and_logout_are_recorded(self):
        self.login('hist-insp', 'wrong-password')
        self.login('nobody', 'whatever-password')
        for _ in range(app.LOGIN_MAX_FAILURES + 1):
            self.login('hist-admin', 'wrong-password', ip='198.51.100.9')
        self.login('hist-insp', 'hist-insp-password')
        with self.client.session_transaction() as s:   # login issues a fresh CSRF token
            s['_csrf_token'] = CSRF
        self.client.post('/logout', data={'_csrf_token': CSRF})
        kinds = [(e['username'], e['event']) for e in self.events()]
        self.assertEqual(kinds[0], ('hist-insp', 'failed'))
        self.assertEqual(kinds[1], ('nobody', 'failed'))
        self.assertIsNone(self.events()[1]['user_id'])
        self.assertIn(('hist-admin', 'blocked'), kinds)
        self.assertEqual(kinds[-2:], [('hist-insp', 'login'), ('hist-insp', 'logout')])
        # the password is never stored
        self.assertNotIn('wrong-password', str(self.events()))

    def test_only_admin_can_open_history_and_new_ip_is_flagged(self):
        self.login('hist-insp', 'hist-insp-password', ip='203.0.113.5', client=app.app.test_client())
        self.login('hist-insp', 'hist-insp-password', ip='203.0.113.5', client=app.app.test_client())
        self.login('hist-insp', 'hist-insp-password', ip='192.0.2.77', client=app.app.test_client())

        inspector = app.app.test_client()
        self.login('hist-insp', 'hist-insp-password', client=inspector)
        self.assertEqual(inspector.get('/admin/logins').status_code, 403)

        self.login('hist-admin', 'hist-admin-password')
        page = self.client.get('/admin/logins?user=hist-insp').get_data(as_text=True)
        self.assertIn('192.0.2.77', page)
        self.assertIn('203.0.113.5', page)
        self.assertEqual(page.count('class="new-ip"'), 1)   # first-ever login is not flagged
        self.assertIn('Android', page)
        self.assertNotIn('hist-admin</strong>', page)        # filtered to one user

        users_page = self.client.get('/admin/users').get_data(as_text=True)
        self.assertIn('/admin/logins?user=hist-insp', users_page)

    def test_old_entries_are_pruned_and_logging_errors_do_not_block_login(self):
        with db_conn() as conn:
            conn.execute("INSERT INTO login_events (created_at, username, event) VALUES "
                         "('2000-01-01 00:00:00', 'ancient', 'login')")
        self.login('hist-insp', 'hist-insp-password')
        self.assertNotIn('ancient', [e['username'] for e in self.events()])

        with db_conn() as conn:
            conn.execute('ALTER TABLE login_events RENAME TO login_events_tmp')
        try:
            response = self.login('hist-admin', 'hist-admin-password', client=app.app.test_client())
            self.assertEqual(response.status_code, 302)
        finally:
            with db_conn() as conn:
                conn.execute('ALTER TABLE login_events_tmp RENAME TO login_events')

    def test_device_label(self):
        with app.app.test_request_context():
            app.g.lang = 'en'
            self.assertEqual(app.device_label(ANDROID_WECHAT), 'Android · WeChat')
            self.assertEqual(app.device_label(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/129.0 Safari/537.36 Edg/129.0'), 'Windows · Edge')
            self.assertEqual(app.device_label(''), '—')


if __name__ == '__main__':
    unittest.main()
