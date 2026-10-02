"""Google Sheets / Drive sync was removed (blocked in mainland China)."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn


class NoGoogleTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('boss','x','admin')")
            self.uid = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
        app.save_json(app.CURRENT_FILE, {'MELBOURNE': [['Order Number', 'Daemco Purchase Order', 'Item Code'],
                                                       ['DPL1', 'PO-1', 'X']]})

    def client(self):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.uid
            session['_csrf_token'] = 'tok'
        return client

    def test_pages_do_not_mention_google(self):
        client = self.client()
        for url in ('/', '/settings', '/inspect/MELBOURNE|PO-1|X'):
            with self.subTest(url=url):
                page = client.get(url).get_data(as_text=True)
                self.assertNotIn('Google', page)

    def test_saving_settings_drops_old_google_ids(self):
        config = app.load_config()
        config.update(sheet_id='abc', drive_folder_id='def')
        app.save_json(app.CONFIG_FILE, config)
        self.client().post('/settings', data={'_csrf_token': 'tok', 'valve_prefixes': 'RSV'})
        config = app.load_config()
        self.assertNotIn('sheet_id', config)
        self.assertNotIn('drive_folder_id', config)


if __name__ == '__main__':
    unittest.main()
