"""Light / dark switch: admin only, remembered in the 'theme' cookie."""
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


class DarkModeTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            for name, role in (('boss', 'admin'), ('wang', 'inspector')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (name, generate_password_hash('x' * 12), role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}

    def page(self, user, theme=None):
        c = app.app.test_client()
        if theme:
            c.set_cookie('theme', theme)
        with c.session_transaction() as s:
            s['user_id'] = self.ids[user]
        return c.get('/reports').get_data(as_text=True)

    def test_admin_gets_switch_and_dark_page(self):
        light = self.page('boss')
        self.assertIn('id="theme-btn"', light)
        self.assertNotIn('<html lang="zh-CN" data-theme="dark">', light)
        self.assertIn('<html lang="zh-CN" data-theme="dark">', self.page('boss', 'dark'))
        self.assertNotIn('<html lang="zh-CN" data-theme="dark">', self.page('boss', 'light'))

    def test_other_roles_stay_light(self):
        page = self.page('wang', 'dark')
        self.assertNotIn('id="theme-btn"', page)
        self.assertNotIn('<html lang="zh-CN" data-theme="dark">', page)


if __name__ == '__main__':
    unittest.main()
