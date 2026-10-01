"""Melbourne head-office role: views everything incl. dashboard and prices,
reviews reports, but does not inspect or assign."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from helpers import assign_job

HEADERS = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description',
           'Quantity', 'Updated Price']
JOB = 'MELBOURNE|PO-1|ITEM-A'


class HqRoleTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True, SEND_EMAIL_SYNC=True)
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks', 'inspection_reviews', 'report_emails'):
                conn.execute(f'DELETE FROM {table}')
            for name, role in (('jayson', 'hq'), ('yu', 'inspector'), ('boss', 'admin')):
                conn.execute('INSERT INTO users (username,password_hash,role,display_name) VALUES (?,?,?,?)',
                             (name, 'x', role, name.title()))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        schedule = {'MELBOURNE': [HEADERS, ['DPL1', 'PO-1', 'ITEM-A', 'Valve', '5', '123.45']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {JOB: [{
            'job_key': JOB, 'order_number': 'DPL1', 'item_code': 'ITEM-A', 'result': 'Fail',
            'inspector_name': 'Yu', 'inspection_date': '2026-10-09', 'submitted_by': 'yu',
            'submitted_at': '2026-10-09T10:00:00'}]})
        config = app.load_config()
        config.update(hq_report_emails='', task_notify_emails='', modules={})
        app.save_json(app.CONFIG_FILE, config)
        assign_job(JOB, 'yu')

    def tearDown(self):
        app.app.config['SEND_EMAIL_SYNC'] = False

    def client_for(self, name):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.ids[name]
            session['_csrf_token'] = 'tok'
        return client

    def test_hq_sees_dashboard_and_prices_inspector_does_not(self):
        hq, yu = self.client_for('jayson'), self.client_for('yu')
        self.assertEqual(hq.get('/dashboard').status_code, 200)
        self.assertIn('href="/dashboard"', hq.get('/').get_data(as_text=True))
        self.assertEqual(yu.get('/dashboard').status_code, 403)
        self.assertIn('123.45', hq.get('/').get_data(as_text=True))
        self.assertNotIn('123.45', yu.get('/').get_data(as_text=True))
        self.assertEqual(self.client_for('boss').get('/dashboard').status_code, 200)

    def test_hq_reviews_but_cannot_inspect_or_assign(self):
        hq = self.client_for('jayson')
        page = hq.get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertNotIn('id="inspect-form"', page)
        self.assertIn('/report/0/review', page)  # approve / return form is shown
        self.assertFalse(hq.get(f'/inspect/{JOB}/can-submit').get_json()['ok'])
        hq.post(f'/inspect/{JOB}/submit', data={
            '_csrf_token': 'tok', 'inspector_name': 'x', 'inspection_date': '2026-10-10',
            'result': 'Pass'}, content_type='multipart/form-data')
        self.assertEqual(len(app.load_json(app.INSPECTIONS_CACHE)[JOB]), 1)

        response = hq.post(f'/inspect/{JOB}/report/0/review',
                           data={'_csrf_token': 'tok', 'action': 'approve', 'comment': 'OK'})
        self.assertEqual(response.status_code, 302)
        with db_conn() as conn:
            review = conn.execute('SELECT status, reviewer FROM inspection_reviews WHERE job_key=?',
                                  (JOB,)).fetchone()
        self.assertEqual(tuple(review), ('approved', 'jayson'))

        self.assertEqual(hq.post('/tasks/assign', data={
            '_csrf_token': 'tok', 'task_ids': ['1'], 'assignee_id': str(self.ids['yu'])}).status_code, 403)
        self.assertEqual(hq.post(f'/inspect/{JOB}/assign', data={
            '_csrf_token': 'tok', 'assignee_id': str(self.ids['yu'])}).status_code, 403)

    def test_inspector_cannot_review(self):
        response = self.client_for('yu').post(f'/inspect/{JOB}/report/0/review',
                                              data={'_csrf_token': 'tok', 'action': 'approve'})
        self.assertEqual(response.status_code, 403)

    def test_hq_task_list_shows_all_tasks_without_assign_bar(self):
        page = self.client_for('jayson').get('/tasks').get_data(as_text=True)
        self.assertIn('DPL1', page)
        self.assertNotIn('assignForm', page)

    def test_admin_can_create_hq_account(self):
        response = self.client_for('boss').post('/admin/users/create', data={
            '_csrf_token': 'tok', 'username': 'hq2', 'password': 'hq2-password-long',
            'role': 'hq', 'email': 'hq2@example.test', 'display_name': 'HQ Two'})
        self.assertEqual(response.status_code, 302)
        with db_conn() as conn:
            self.assertEqual(conn.execute("SELECT role FROM users WHERE username='hq2'").fetchone()[0], 'hq')
        self.assertIn('value="hq"', self.client_for('boss').get('/admin/users').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
