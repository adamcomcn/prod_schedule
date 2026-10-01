"""Lead inspector assigns tasks; e-mail notification; 'My tasks' view."""
import email
import os
import sys
import tempfile
import unittest
from email.header import decode_header, make_header
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

SMTP_ENV = {'SMTP_HOST': 'smtp.example.test', 'SMTP_PORT': '465',
            'SMTP_USERNAME': 'noreply@example.test', 'SMTP_PASSWORD': 'x'}


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self): pass
    def starttls(self): pass
    def login(self, user, pwd): pass

    def sendmail(self, sender, recipients, raw):
        FakeSMTP.sent.append((sender, list(recipients), raw))


class TaskAssignmentTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        FakeSMTP.sent = []
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute('DELETE FROM inspection_tasks')
            for username, role, name, mail in (
                    ('murphy', 'lead', 'Murphy', ''),
                    ('yu', 'inspector', 'Mr. Yu', 'yu@example.test'),
                    ('newbie', 'inspector', '', '')):
                conn.execute(
                    'INSERT INTO users (username,password_hash,role,display_name,email) VALUES (?,?,?,?,?)',
                    (username, generate_password_hash(username * 4), role, name, mail))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
            for n in range(3):
                conn.execute(
                    'INSERT INTO inspection_tasks (job_key, order_number, region, item_code, description, '
                    'quantity, est_completion, status) VALUES (?,?,?,?,?,?,?,?)',
                    (f'MELBOURNE|PO-{n}|ITEM{n}', f'DPL{n}', 'MELBOURNE', f'ITEM{n}', f'Item {n}',
                     '10', '2026-10-20', 'Pending'))
            self.tasks = [r['id'] for r in conn.execute('SELECT id FROM inspection_tasks ORDER BY id')]
        config = app.load_config()
        config['task_notify_emails'] = 'lead@example.test'
        config['modules'] = {}
        app.save_json(app.CONFIG_FILE, config)

    def client_for(self, username):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.ids[username]
            session['_csrf_token'] = 'tok'
        return client

    def assign(self, client, task_ids, assignee, note=''):
        return client.post('/tasks/assign', data={
            '_csrf_token': 'tok', 'task_ids': [str(t) for t in task_ids],
            'assignee_id': str(assignee) if assignee else '', 'note': note, 'next': '/tasks'})

    @mock.patch.dict(os.environ, SMTP_ENV)
    @mock.patch('smtplib.SMTP_SSL', FakeSMTP)
    def test_lead_assigns_to_inspector_and_email_is_sent(self):
        murphy = self.client_for('murphy')
        response = self.assign(murphy, self.tasks[:2], self.ids['yu'], note='周三前完成')
        self.assertEqual(response.status_code, 302)
        with db_conn() as conn:
            rows = conn.execute('SELECT assigned_to, assigned_by, assign_note FROM inspection_tasks '
                                'ORDER BY id').fetchall()
        self.assertEqual([r['assigned_to'] for r in rows], [self.ids['yu'], self.ids['yu'], None])
        self.assertEqual(rows[0]['assigned_by'], 'murphy')
        self.assertEqual(rows[0]['assign_note'], '周三前完成')

        self.assertEqual(len(FakeSMTP.sent), 1)
        _, recipients, raw = FakeSMTP.sent[0]
        self.assertEqual(recipients, ['lead@example.test', 'yu@example.test'])
        msg = email.message_from_string(raw)
        subject = str(make_header(decode_header(msg['Subject'])))
        self.assertIn('Mr. Yu', subject)
        self.assertIn('2', subject)
        body = msg.get_payload(decode=True).decode('utf-8')
        self.assertIn('DPL0', body)
        self.assertIn('DPL1', body)
        self.assertNotIn('DPL2', body)
        self.assertIn('周三前完成', body)
        self.assertIn('/inspect/MELBOURNE%7CPO-0%7CITEM0', body)

    @mock.patch.dict(os.environ, SMTP_ENV)
    @mock.patch('smtplib.SMTP_SSL', FakeSMTP)
    def test_lead_can_assign_to_self(self):
        self.assign(self.client_for('murphy'), self.tasks[:1], self.ids['murphy'])
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT assigned_to FROM inspection_tasks WHERE id=?',
                                          (self.tasks[0],)).fetchone()[0], self.ids['murphy'])
        self.assertEqual(FakeSMTP.sent[0][1], ['lead@example.test'])

    def test_assignment_saved_even_without_smtp(self):
        with mock.patch.dict(os.environ, {'SMTP_HOST': ''}):
            response = self.assign(self.client_for('murphy'), self.tasks[:1], self.ids['newbie'])
        self.assertEqual(response.status_code, 302)
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT assigned_to FROM inspection_tasks WHERE id=?',
                                          (self.tasks[0],)).fetchone()[0], self.ids['newbie'])

    def test_inspector_cannot_assign(self):
        response = self.assign(self.client_for('yu'), self.tasks[:1], self.ids['yu'])
        self.assertEqual(response.status_code, 403)

    def test_my_tasks_is_default_for_inspectors(self):
        with db_conn() as conn:
            conn.execute('UPDATE inspection_tasks SET assigned_to=? WHERE id=?',
                         (self.ids['yu'], self.tasks[0]))
        page = self.client_for('yu').get('/tasks').get_data(as_text=True)
        self.assertIn('DPL0', page)
        self.assertNotIn('DPL1', page)
        self.assertNotIn('assignForm', page)  # no bulk-assign bar for inspectors
        lead_page = self.client_for('murphy').get('/tasks').get_data(as_text=True)
        self.assertIn('DPL1', lead_page)
        self.assertIn('assignForm', lead_page)
        unassigned = self.client_for('murphy').get('/tasks?scope=unassigned').get_data(as_text=True)
        self.assertNotIn('DPL0', unassigned)
        self.assertIn('DPL2', unassigned)

    def test_inspector_can_only_update_own_task_status(self):
        with db_conn() as conn:
            conn.execute('UPDATE inspection_tasks SET assigned_to=? WHERE id=?',
                         (self.ids['yu'], self.tasks[0]))
        yu = self.client_for('yu')
        ok = yu.post(f'/tasks/{self.tasks[0]}/status', data={'_csrf_token': 'tok', 'status': 'In Progress'})
        self.assertEqual(ok.status_code, 302)
        denied = yu.post(f'/tasks/{self.tasks[1]}/status', data={'_csrf_token': 'tok', 'status': 'Completed'})
        self.assertEqual(denied.status_code, 403)
        bad = yu.post(f'/tasks/{self.tasks[0]}/status', data={'_csrf_token': 'tok', 'status': 'Whatever'})
        self.assertEqual(bad.status_code, 400)

    def test_only_lead_can_close_or_reopen(self):
        with db_conn() as conn:
            conn.execute('UPDATE inspection_tasks SET assigned_to=? WHERE id IN (?, ?)',
                         (self.ids['yu'], self.tasks[0], self.tasks[1]))
        yu, murphy = self.client_for('yu'), self.client_for('murphy')

        def set_status(client, task, status):
            return client.post(f'/tasks/{task}/status', data={'_csrf_token': 'tok', 'status': status})

        # Inspector cannot close his own task, and does not see the option.
        self.assertEqual(set_status(yu, self.tasks[0], 'Closed').status_code, 403)
        page = yu.get('/tasks').get_data(as_text=True)
        self.assertNotIn('value="Closed"', page)
        self.assertIn('value="On Hold"', page)

        # The lead can close it; the inspector then cannot reopen it.
        self.assertEqual(set_status(murphy, self.tasks[0], 'Closed').status_code, 302)
        self.assertEqual(set_status(yu, self.tasks[0], 'Pending').status_code, 403)
        with db_conn() as conn:
            status = conn.execute('SELECT status FROM inspection_tasks WHERE id=?',
                                  (self.tasks[0],)).fetchone()[0]
        self.assertEqual(status, 'Closed')
        self.assertIn('value="Closed"', murphy.get('/tasks').get_data(as_text=True))

        # The lead can reopen; other statuses stay open to the inspector.
        self.assertEqual(set_status(murphy, self.tasks[0], 'Pending').status_code, 302)
        self.assertEqual(set_status(yu, self.tasks[1], 'On Hold').status_code, 302)

    def test_inspection_result_updates_task_status(self):
        app._update_task_after_inspection('MELBOURNE|PO-0|ITEM0', 'Pass')
        app._update_task_after_inspection('MELBOURNE|PO-1|ITEM1', 'Fail')
        with db_conn() as conn:
            statuses = [r[0] for r in conn.execute('SELECT status FROM inspection_tasks ORDER BY id')]
        self.assertEqual(statuses, ['Completed', 'In Progress', 'Pending'])

    def test_admin_can_create_lead_with_email(self):
        with db_conn() as conn:
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('boss','x','admin')")
            self.ids['boss'] = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
        response = self.client_for('boss').post('/admin/users/create', data={
            '_csrf_token': 'tok', 'username': 'lead2', 'password': 'lead2-password-long',
            'role': 'lead', 'email': 'lead2@example.test', 'display_name': 'Lead Two'})
        self.assertEqual(response.status_code, 302)
        with db_conn() as conn:
            row = conn.execute("SELECT role, email, display_name FROM users WHERE username='lead2'").fetchone()
        self.assertEqual(tuple(row), ('lead', 'lead2@example.test', 'Lead Two'))

    def test_notify_email_setting_is_cleaned(self):
        self.assertEqual(app._email_list('a@x.com; bad, A@X.com  b@y.org'), ['a@x.com', 'b@y.org'])


if __name__ == '__main__':
    unittest.main()
