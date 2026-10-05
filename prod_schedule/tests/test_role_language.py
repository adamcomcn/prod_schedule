"""Interface language is fixed per role; only admins can switch."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn


class RoleLanguageTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            for name, role, lang in (('boss', 'admin', 'en'), ('murphy', 'lead', 'en'),
                                     ('yu', 'inspector', ''), ('mel', 'hq', 'zh')):
                conn.execute('INSERT INTO users (username,password_hash,role,language) VALUES (?,?,?,?)',
                             (name, 'x', role, lang))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}

    def page(self, name, url='/tasks'):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.ids[name]
        return client, client.get(url).get_data(as_text=True)

    def test_fixed_language_and_no_toggle(self):
        for name, marker in (('murphy', '检验任务跟踪'), ('yu', '检验任务跟踪'), ('mel', 'Inspection Task Tracker')):
            with self.subTest(name=name):
                _client, html = self.page(name)
                self.assertIn(marker, html)
                self.assertNotIn('class="lang-btn"', html)

    def test_admin_keeps_the_toggle(self):
        _client, html = self.page('boss')
        self.assertIn('Inspection Task Tracker', html)      # admin's saved choice: English
        self.assertIn('class="lang-btn"', html)

    def test_switching_does_not_change_fixed_roles(self):
        client, _html = self.page('yu')
        client.get('/lang/en')
        self.assertIn('检验任务跟踪', client.get('/tasks').get_data(as_text=True))
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT language FROM users WHERE username=?', ('yu',)).fetchone()[0], '')


if __name__ == '__main__':
    unittest.main()
