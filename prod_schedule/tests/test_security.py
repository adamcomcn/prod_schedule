import io
import os
import sys
import tempfile
import unittest

TEST_DATA_DIR = tempfile.mkdtemp(prefix='prod-schedule-tests-')
os.environ['APP_DATA_DIR'] = TEST_DATA_DIR
os.environ['SECRET_KEY'] = 'test-secret-key'

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash


class SecurityAndProductionTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app._login_failures.clear()
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute(
                'INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                ('admin-test', generate_password_hash('admin-test-password'), 'admin'))
            conn.execute(
                'INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                ('inspector-test', generate_password_hash('inspector-test-password'), 'inspector'))
        self.client = app.app.test_client()

    def login_as(self, username):
        with db_conn() as conn:
            user_id = conn.execute(
                'SELECT id FROM users WHERE username=?', (username,)
            ).fetchone()[0]
        with self.client.session_transaction() as session:
            session['user_id'] = user_id
            session['_csrf_token'] = 'test-csrf-token'

    def test_health_is_public_and_business_routes_require_login(self):
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertIn('/login', self.client.get('/').location)

    def test_post_requires_csrf(self):
        response = self.client.post(
            '/login',
            data={'username': 'admin-test', 'password': 'admin-test-password'})
        self.assertEqual(response.status_code, 400)

    def test_inspector_cannot_access_admin_endpoints(self):
        self.login_as('inspector-test')
        self.assertEqual(self.client.get('/settings').status_code, 403)
        self.assertEqual(self.client.get('/admin/users').status_code, 403)
        response = self.client.post(
            '/upload',
            data={'_csrf_token': 'test-csrf-token'},
            content_type='multipart/form-data')
        self.assertEqual(response.status_code, 403)

    def test_admin_can_manage_users_without_default_passwords(self):
        self.login_as('admin-test')
        self.assertEqual(self.client.get('/admin/users').status_code, 200)
        with db_conn() as conn:
            usernames = {row[0] for row in conn.execute('SELECT username FROM users')}
        self.assertNotIn('admin', usernames)
        self.assertNotIn('qc1', usernames)
        self.assertNotIn('qc2', usernames)

    def test_upload_rejects_non_xlsx_and_security_headers_are_set(self):
        self.login_as('admin-test')
        response = self.client.post(
            '/upload',
            data={
                '_csrf_token': 'test-csrf-token',
                'file': (io.BytesIO(b'not an excel file'), 'payload.txt'),
            },
            content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302)
        page = self.client.get('/')
        self.assertEqual(page.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(page.headers['X-Content-Type-Options'], 'nosniff')

    def test_schedule_is_paginated_and_knowledge_tolerates_bad_image_json(self):
        headers = ['Order Number', 'Item Code', 'Description']
        rows = [headers] + [[f'PO-{i}', f'ITEM-{i}', f'Description {i}'] for i in range(500)]
        app.save_json(app.CURRENT_FILE, {'MELBOURNE': rows})
        app.save_json(app.PREVIOUS_FILE, {'MELBOURNE': rows})
        with db_conn() as conn:
            conn.execute(
                "UPDATE kb_articles SET images='invalid-json' "
                "WHERE id=(SELECT MIN(id) FROM kb_articles)")
        self.login_as('admin-test')
        response = self.client.get('/?sheet=MELBOURNE&per_page=50')
        self.assertEqual(response.status_code, 200)
        self.assertLess(len(response.data), 200_000)
        self.assertEqual(self.client.get('/knowledge').status_code, 200)


if __name__ == '__main__':
    unittest.main()
