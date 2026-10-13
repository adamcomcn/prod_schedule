"""Disk usage in Settings, full-backup tracking and the daily storage / backup reminder."""
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

Disk = namedtuple('Disk', 'total used free')
GB = 1024 ** 3


class StorageAlertTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        config = app.load_config()
        for key in ('full_backup_last_at', 'full_backup_reminder_last', 'disk_alert_last_day'):
            config.pop(key, None)
        config['backup_emails'] = 'it@example.com'
        app.save_json(app.CONFIG_FILE, config)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('boss',?,'admin')",
                         (generate_password_hash('x' * 12),))
            self.uid = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]

    def alert(self, used_gb, total_gb=10):
        sent = []
        disk = Disk(total_gb * GB, used_gb * GB, (total_gb - used_gb) * GB)
        with mock.patch.object(app.shutil, 'disk_usage', return_value=disk), \
                mock.patch.object(app, '_smtp_send', side_effect=lambda *a: sent.append(a) or (True, 'ok')), \
                app.app.test_request_context(base_url='https://qc.example.com'):
            app.send_storage_alert()
        return sent

    def mark_full_backup(self, days_ago=0):
        from datetime import datetime, timedelta, timezone
        when = datetime.now(timezone.utc) - timedelta(days=days_ago)
        app.update_config(full_backup_last_at=when.strftime('%Y-%m-%d %H:%M'))

    def test_disk_warning_once_a_day(self):
        self.mark_full_backup()
        sent = self.alert(used_gb=8.5)
        self.assertEqual(len(sent), 1)
        self.assertIn('85%', sent[0][0])
        self.assertNotIn('Full backup due', sent[0][0])
        self.assertEqual(self.alert(used_gb=8.5), [])                       # not again today

    def test_quiet_when_disk_ok_and_backup_recent(self):
        self.mark_full_backup(days_ago=3)
        self.assertEqual(self.alert(used_gb=5), [])

    def test_backup_reminder_weekly_when_none_for_30_days(self):
        self.mark_full_backup(days_ago=31)
        sent = self.alert(used_gb=5)
        self.assertEqual(len(sent), 1)
        self.assertIn('Full backup due', sent[0][0])
        self.assertIn('31', sent[0][1])
        self.assertEqual(self.alert(used_gb=5), [])                         # weekly, not daily

    def test_no_recipients_no_mail(self):
        app.update_config(backup_emails='')
        self.assertEqual(self.alert(used_gb=9.5), [])

    def client(self):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.uid
            s['_csrf_token'] = 'tok'
        return c

    def test_full_backup_download_is_recorded_and_shown(self):
        page = self.client().get('/settings').get_data(as_text=True)
        self.assertIn('id="storage"', page)
        self.assertIn('还没有下载过完整备份', page)
        response = self.client().post('/settings/backup', data={'_csrf_token': 'tok', 'scope': 'full'})
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(app.full_backup_age_days(), 0)
        self.assertEqual(app.load_config()['full_backup_last_by'], 'boss')
        self.client().post('/settings/backup', data={'_csrf_token': 'tok', 'scope': 'data'}).close()
        page = self.client().get('/settings').get_data(as_text=True)
        self.assertIn('上次下载完整备份', page)


if __name__ == '__main__':
    unittest.main()
