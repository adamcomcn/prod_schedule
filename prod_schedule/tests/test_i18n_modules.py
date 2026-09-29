"""Language switching and optional-module tests."""
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

HEADERS = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description',
           'Quantity', 'Current Status', 'Estimated Completion Date', 'QA BRTs Sent?']
JOB = 'MELBOURNE|PO-1|RSV0100FL'

PAGES = ['/', '/dashboard', '/tasks', '/forms', '/orders', '/orders/new', '/suppliers',
         '/suppliers/new', '/products', '/products/items/new', '/regions', '/employees',
         '/employees/new', '/settings', '/admin/users', f'/inspect/{JOB}']


class I18nAndModuleTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app._login_failures.clear()
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute(
                'INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                ('i18n-admin', generate_password_hash('i18n-admin-password'), 'admin'))
            self.uid = conn.execute("SELECT id FROM users WHERE username='i18n-admin'").fetchone()[0]
        schedule = {'MELBOURNE': [HEADERS, ['DPL1', 'PO-1', 'RSV0100FL', 'Valve DN100', '5',
                                            'In Production', '2026-10-20', 'NO']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        config = app.load_config()
        config['modules'] = {}
        app.save_json(app.CONFIG_FILE, config)
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.uid
            session['_csrf_token'] = 'tok'

    def test_default_language_is_chinese(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('lang="zh-CN"', page)
        self.assertIn('排期', page)
        self.assertIn('预计完成日', page)  # translated column header
        self.assertIn('生产中', page)       # translated Current Status value

    def test_switch_to_english_is_remembered_per_user(self):
        response = self.client.get('/lang/en?next=/tasks')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/tasks'))
        with db_conn() as conn:
            lang = conn.execute('SELECT language FROM users WHERE id=?', (self.uid,)).fetchone()[0]
        self.assertEqual(lang, 'en')
        # A fresh browser (no cookie) still gets English for this account.
        other = app.app.test_client()
        with other.session_transaction() as session:
            session['user_id'] = self.uid
        page = other.get('/').get_data(as_text=True)
        self.assertIn('lang="en"', page)
        self.assertIn('Estimated Completion Date', page)

    def test_language_switch_rejects_external_redirect(self):
        response = self.client.get('/lang/zh?next=//evil.example.com')
        self.assertNotIn('evil', response.location)
        self.assertEqual(self.client.get('/lang/fr').status_code, 404)

    def test_login_page_can_switch_language_without_login(self):
        anon = app.app.test_client()
        response = anon.get('/lang/en?next=/login')
        self.assertEqual(response.status_code, 302)
        page = anon.get('/login').get_data(as_text=True)
        self.assertIn('Sign in to continue', page)

    def test_all_pilot_pages_render_in_both_languages(self):
        for lang in ('zh', 'en'):
            self.client.get(f'/lang/{lang}')
            for path in PAGES:
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, f'{lang} {path}')

    def test_disabled_modules_are_hidden_and_unreachable(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('href="/hr"', page)
        self.assertNotIn('href="/training"', page)
        self.assertNotIn('href="/knowledge"', page)
        for path in ('/hr', '/training', '/knowledge'):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_admin_can_enable_module(self):
        response = self.client.post('/settings/modules', data={'_csrf_token': 'tok', 'module_hr': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get('/hr').status_code, 200)
        self.assertIn('href="/hr"', self.client.get('/').get_data(as_text=True))
        self.assertEqual(self.client.get('/training').status_code, 404)

    def test_evidence_guidance_is_translated(self):
        self.client.get('/lang/zh')
        page = self.client.get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('V-Trust 压力测试视频', page)
        self.assertIn('上传 V-Trust 压力测试视频', page)
        self.client.get('/lang/en')
        page = self.client.get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('V-Trust Pressure Test Video', page)


if __name__ == '__main__':
    unittest.main()
