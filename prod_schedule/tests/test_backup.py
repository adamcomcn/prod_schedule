"""Backup downloads, the daily backup e-mail and the SMTP test e-mail."""
import io
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn


class BackupTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute("INSERT INTO users (username,password_hash,role,email) VALUES ('boss','x','admin','boss@example.com')")
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('yu','x','inspector')")
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        app.save_json(app.INSPECTIONS_CACHE, {'MELBOURNE|PO-1|X': [{'result': 'Pass'}]})
        job_dir = os.path.join(app.UPLOAD_DIR, 'testjob')
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, 'photo.jpg'), 'wb') as fh:
            fh.write(b'jpeg')
        config = app.load_config()
        for key in ('backup_emails', 'backup_last_day', 'backup_last_at', 'backup_last_status'):
            config.pop(key, None)
        app.save_json(app.CONFIG_FILE, config)

    def client_for(self, name):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.ids[name]
            session['_csrf_token'] = 'tok'
        return client

    def download(self, scope):
        response = self.client_for('boss').post('/settings/backup', data={'_csrf_token': 'tok', 'scope': scope})
        self.assertEqual(response.status_code, 200)
        data = response.get_data()
        response.close()
        return zipfile.ZipFile(io.BytesIO(data))

    def test_data_backup_has_database_and_records_but_no_photos(self):
        names = self.download('data').namelist()
        self.assertIn('data/app.db', names)
        self.assertIn('data/inspections_cache.json', names)
        self.assertFalse(any(n.startswith('uploads/') for n in names))

    def test_full_backup_includes_attachments_and_a_working_database(self):
        zf = self.download('full')
        self.assertIn('uploads/testjob/photo.jpg', zf.namelist())
        with tempfile.TemporaryDirectory() as tmp:
            path = zf.extract('data/app.db', tmp)
            conn = sqlite3.connect(path)
            users = {r[0] for r in conn.execute('SELECT username FROM users')}
            conn.close()
        self.assertEqual(users, {'boss', 'yu'})

    def test_only_admins_can_download(self):
        response = self.client_for('yu').post('/settings/backup', data={'_csrf_token': 'tok', 'scope': 'full'})
        self.assertEqual(response.status_code, 403)

    def test_daily_backup_email_once_a_day(self):
        sent = []
        with app.app.test_request_context(), \
                mock.patch.object(app, '_smtp_send', side_effect=lambda *a: sent.append(a) or (True, 'ok')):
            self.assertFalse(app.send_backup_email()[0])          # no address configured
            config = app.load_config()
            config['backup_emails'] = 'it@example.com'
            app.save_json(app.CONFIG_FILE, config)
            self.assertTrue(app.send_backup_email()[0])
            self.assertFalse(app.send_backup_email()[0])          # already sent today
            self.assertTrue(app.send_backup_email(force=True)[0])
        self.assertEqual(len(sent), 2)
        subject, _body, recipients, attachments = sent[0]
        self.assertEqual(recipients, ['it@example.com'])
        filename, data, mimetype = attachments[0]
        self.assertTrue(filename.endswith('.zip'))
        self.assertIn('data/app.db', zipfile.ZipFile(io.BytesIO(data)).namelist())
        self.assertEqual(app.load_config()['backup_last_status'], 'ok')

    def test_settings_shows_backup_card_and_test_email(self):
        page = self.client_for('boss').get('/settings').get_data(as_text=True)
        self.assertIn('id="backup"', page)
        self.assertIn('value="boss@example.com"', page)       # test e-mail defaults to own address

    def test_test_email(self):
        with mock.patch.object(app, '_smtp_send', return_value=(True, 'sent')) as send:
            response = self.client_for('boss').post('/settings/test-email',
                                                    data={'_csrf_token': 'tok', 'to': 'me@example.com'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(send.call_args[0][2], ['me@example.com'])
        with mock.patch.object(app, '_smtp_send') as send:
            self.client_for('boss').post('/settings/test-email', data={'_csrf_token': 'tok', 'to': 'nope'})
        send.assert_not_called()
        self.assertEqual(self.client_for('yu').post('/settings/test-email',
                                                    data={'_csrf_token': 'tok', 'to': 'a@b.co'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()


class SenderNameTests(unittest.TestCase):
    def test_from_header_uses_display_name(self):
        env = {'SMTP_USERNAME': 'qc@example.com', 'SMTP_FROM': '', 'SMTP_FROM_NAME': 'DAEMCO-QC'}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(app.smtp_sender(), ('qc@example.com', 'DAEMCO-QC <qc@example.com>'))
        with mock.patch.dict(os.environ, {'SMTP_USERNAME': 'qc@example.com', 'SMTP_FROM': 'noreply@example.com',
                                          'SMTP_FROM_NAME': ''}):
            self.assertEqual(app.smtp_sender(), ('noreply@example.com', 'noreply@example.com'))
        with mock.patch.dict(os.environ, {'SMTP_USERNAME': 'qc@example.com', 'SMTP_FROM': '',
                                          'SMTP_FROM_NAME': '质检系统'}):
            address, header = app.smtp_sender()
            self.assertEqual(address, 'qc@example.com')
            self.assertTrue(header.startswith('=?utf-8?') and header.endswith('<qc@example.com>'))


class SmtpErrorTests(unittest.TestCase):
    def test_failure_names_the_step_and_server_reply(self):
        import smtplib
        env = {'SMTP_HOST': 'smtp.example.com', 'SMTP_PORT': '465', 'SMTP_USERNAME': 'qc@example.com',
               'SMTP_PASSWORD': 'secret-code', 'SMTP_FROM': '', 'SMTP_FROM_NAME': ''}

        class FakeSSL:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, *a): raise smtplib.SMTPServerDisconnected('Connection unexpectedly closed')

        with mock.patch.dict(os.environ, env), mock.patch('smtplib.SMTP_SSL', FakeSSL), \
                app.app.test_request_context(), self.assertLogs(app.logger, 'ERROR') as logs:
            ok, msg = app._smtp_send('s', 'b', ['to@example.com'])
        self.assertFalse(ok)
        self.assertIn('SMTPServerDisconnected: Connection unexpectedly closed', msg)
        self.assertIn('登录', msg)                                     # failed at the login step
        self.assertIn('host=smtp.example.com port=465 user=qc@example.com', logs.output[0])
        self.assertNotIn('secret-code', logs.output[0] + msg)
