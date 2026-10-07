"""Country column on the sign-in history (offline DB-IP Lite lookup)."""
import gzip
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import geoip
from db import db_conn
from werkzeug.security import generate_password_hash

COUNTRIES = {'114.114.114.114': {'country': {'iso_code': 'CN', 'names': {'en': 'China', 'zh-CN': '中国'}}},
             '1.1.1.1': {'country': {'iso_code': 'AU', 'names': {'en': 'Australia', 'zh-CN': '澳大利亚'}}}}


class FakeReader:
    def get(self, ip):
        return COUNTRIES.get(ip)


class GeoipLookupTests(unittest.TestCase):
    def test_lookup_public_private_and_invalid(self):
        with mock.patch.object(geoip, '_reader', return_value=FakeReader()):
            self.assertEqual(geoip.lookup('114.114.114.114', '/x')[0], 'CN')
            self.assertIsNone(geoip.lookup('8.8.4.4', '/x'))          # not in the database
        self.assertEqual(geoip.lookup('127.0.0.1', '/x'), ('LAN', {}))
        self.assertEqual(geoip.lookup('10.1.2.3', '/x'), ('LAN', {}))
        self.assertIsNone(geoip.lookup('not-an-ip', '/x'))
        with mock.patch.object(geoip, '_reader', return_value=None):  # database not downloaded yet
            self.assertIsNone(geoip.lookup('1.1.1.1', '/x'))

    def test_missing_database_starts_one_background_download(self):
        data_dir = tempfile.mkdtemp()
        geoip._state.update(reader=None, mtime=None, downloading=False, last_try=0.0)
        with mock.patch.object(geoip, '_refresh_in_background') as refresh:
            self.assertIsNone(geoip._reader(data_dir, auto_download=True))
            refresh.assert_called_once()
            geoip._reader(data_dir, auto_download=False)
            refresh.assert_called_once()
        geoip._state['last_try'] = time.time()                        # tried recently: wait an hour
        with mock.patch.object(geoip, '_refresh_in_background') as refresh:
            geoip._reader(data_dir, auto_download=True)
            refresh.assert_not_called()

    def test_failed_download_leaves_no_file(self):
        path = os.path.join(tempfile.mkdtemp(), 'geoip', 'db.mmdb')
        broken = mock.MagicMock()
        broken.__enter__.return_value.read.return_value = gzip.compress(b'not a database')
        with mock.patch.object(geoip.urllib.request, 'urlopen', return_value=broken):
            self.assertFalse(geoip.download(path))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(os.listdir(os.path.dirname(path)), [])


class LoginHistoryCountryTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute('DELETE FROM login_events')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('geo-admin',?,'admin')",
                         (generate_password_hash('x' * 12),))
            self.uid = conn.execute("SELECT id FROM users WHERE username='geo-admin'").fetchone()[0]
            for ip in ('114.114.114.114', '1.1.1.1', '127.0.0.1'):
                conn.execute("INSERT INTO login_events (created_at, user_id, username, event, ip) "
                             "VALUES ('2026-10-07 01:00:00', ?, 'geo-admin', 'login', ?)", (self.uid, ip))

    def page(self, url):
        client = app.app.test_client()
        with client.session_transaction() as s:
            s['user_id'] = self.uid
        with mock.patch.object(geoip, '_reader', return_value=FakeReader()):
            return client.get(url).get_data(as_text=True)

    def test_country_column_and_last_sign_in(self):
        page = self.page('/admin/logins')
        self.assertIn('国家 / 地区', page)
        self.assertIn('CN 中国', page)
        self.assertIn('AU 澳大利亚', page)
        self.assertIn('内网', page)
        self.assertIn('db-ip.com', page)
        self.assertRegex(self.page('/admin/users'), r'· (CN 中国|AU 澳大利亚)')   # last sign-in shows its country


if __name__ == '__main__':
    unittest.main()
