"""Inspectors may only inspect jobs assigned to them; everyone may view."""
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

HEADERS = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity']
MINE = 'MELBOURNE|PO-1|ITEM-A'
OTHERS = 'MELBOURNE|PO-2|ITEM-B'
FREE = 'MELBOURNE|PO-3|ITEM-C'


class InspectPermissionTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True, SEND_EMAIL_SYNC=True)
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks', 'inspection_attachments', 'form_responses'):
                conn.execute(f'DELETE FROM {table}')
            for name, role, display in (('yu', 'inspector', 'Mr. Yu'), ('zhang', 'inspector', 'Zhang'),
                                        ('murphy', 'lead', 'Murphy')):
                conn.execute('INSERT INTO users (username,password_hash,role,display_name) VALUES (?,?,?,?)',
                             (name, 'x', role, display))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        schedule = {'MELBOURNE': [HEADERS,
                                  ['DPL1', 'PO-1', 'ITEM-A', 'Mine', '5'],
                                  ['DPL2', 'PO-2', 'ITEM-B', 'Others', '5'],
                                  ['DPL3', 'PO-3', 'ITEM-C', 'Unassigned', '5']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {})
        config = app.load_config()
        config.update(hq_report_emails='', task_notify_emails='', modules={})
        app.save_json(app.CONFIG_FILE, config)
        assign_job(MINE, 'yu')
        assign_job(OTHERS, 'zhang')

    def tearDown(self):
        app.app.config['SEND_EMAIL_SYNC'] = False

    def client_for(self, name):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.ids[name]
            session['_csrf_token'] = 'tok'
        return client

    def submit(self, client, job, inspector='Someone Else'):
        return client.post(f'/inspect/{job}/submit', data={
            '_csrf_token': 'tok', 'region': 'MELBOURNE', 'order_number': 'DPL', 'item_code': 'X',
            'inspector_name': inspector, 'inspection_date': '2026-10-09',
            'quantity_inspected': '5', 'result': 'Pass'}, content_type='multipart/form-data')

    def records(self, job):
        return app.load_json(app.INSPECTIONS_CACHE, {}).get(job, [])

    def test_inspector_can_inspect_own_task_under_own_name(self):
        yu = self.client_for('yu')
        page = yu.get(f'/inspect/{MINE}').get_data(as_text=True)
        self.assertIn('id="inspect-form"', page)
        self.assertEqual(yu.get(f'/inspect/{MINE}/can-submit').get_json()['ok'], True)
        self.assertEqual(self.submit(yu, MINE, inspector='Fake Name').status_code, 302)
        self.assertEqual(self.records(MINE)[0]['inspector_name'], 'Mr. Yu')

    def test_inspector_only_views_other_and_unassigned_jobs(self):
        yu = self.client_for('yu')
        for job in (OTHERS, FREE):
            response = yu.get(f'/inspect/{job}')
            self.assertEqual(response.status_code, 200)  # viewing is allowed
            page = response.get_data(as_text=True)
            self.assertNotIn('id="inspect-form"', page)
            self.assertIn('readonly-note', page)
            self.assertFalse(yu.get(f'/inspect/{job}/can-submit').get_json()['ok'])
            self.submit(yu, job)
            self.assertEqual(self.records(job), [])
        self.assertIn('Zhang', yu.get(f'/inspect/{OTHERS}/can-submit').get_json()['message'])

    def test_closed_task_cannot_be_inspected_by_inspector(self):
        assign_job(MINE, 'yu', status='Closed')
        yu = self.client_for('yu')
        self.assertFalse(yu.get(f'/inspect/{MINE}/can-submit').get_json()['ok'])
        self.submit(yu, MINE)
        self.assertEqual(self.records(MINE), [])

    def test_reassigned_task_is_refused(self):
        yu = self.client_for('yu')
        assign_job(MINE, 'zhang')  # Murphy reassigned it while Yu had the form open
        self.submit(yu, MINE)
        self.assertEqual(self.records(MINE), [])

    def test_lead_can_inspect_anything_and_name_someone(self):
        murphy = self.client_for('murphy')
        self.assertEqual(self.submit(murphy, FREE, inspector='Mr. Yu').status_code, 302)
        self.assertEqual(self.records(FREE)[0]['inspector_name'], 'Mr. Yu')
        self.assertIn('id="inspect-form"', murphy.get(f'/inspect/{OTHERS}').get_data(as_text=True))

    def test_lead_assigns_from_inspection_page_creating_the_task(self):
        murphy = self.client_for('murphy')
        response = murphy.post(f'/inspect/{FREE}/assign', data={
            '_csrf_token': 'tok', 'assignee_id': str(self.ids['yu']), 'note': 'today'})
        self.assertEqual(response.status_code, 302)
        with db_conn() as conn:
            task = conn.execute('SELECT assigned_to, order_number, assign_note FROM inspection_tasks '
                                'WHERE job_key=?', (FREE,)).fetchone()
        self.assertEqual((task['assigned_to'], task['order_number'], task['assign_note']),
                         (self.ids['yu'], 'DPL3', 'today'))
        self.assertTrue(self.client_for('yu').get(f'/inspect/{FREE}/can-submit').get_json()['ok'])
        # Inspectors cannot assign.
        self.assertEqual(self.client_for('yu').post(f'/inspect/{FREE}/assign', data={
            '_csrf_token': 'tok', 'assignee_id': str(self.ids['yu'])}).status_code, 403)

    def test_schedule_shows_inspect_button_only_for_own_jobs(self):
        page = self.client_for('yu').get('/').get_data(as_text=True)
        self.assertIn('/inspect/MELBOURNE%7CPO-1%7CITEM-A', page)
        self.assertNotIn('/inspect/MELBOURNE%7CPO-2%7CITEM-B"', page)
        self.assertNotIn('/inspect/MELBOURNE%7CPO-3%7CITEM-C"', page)
        lead_page = self.client_for('murphy').get('/').get_data(as_text=True)
        self.assertIn('/inspect/MELBOURNE%7CPO-3%7CITEM-C', lead_page)

    def test_checklist_submit_follows_the_same_rule(self):
        with db_conn() as conn:
            conn.execute("INSERT INTO form_templates (name, sections_json, keywords) VALUES "
                         "('T', '[{\"name\": \"S\", \"questions\": [{\"guideline\": \"Q\"}]}]', '')")
            tpl = conn.execute("SELECT id FROM form_templates WHERE name='T'").fetchone()[0]
        data = {'_csrf_token': 'tok', 'tpl_id': str(tpl), 'inspector': 'Fake', 'insp_date': '2026-10-09',
                'overall': 'Pass', 'ans_0_0': 'Pass'}
        self.client_for('yu').post(f'/inspect/{OTHERS}/checklist', data=data)
        self.client_for('yu').post(f'/inspect/{MINE}/checklist', data=data)
        with db_conn() as conn:
            rows = conn.execute('SELECT job_key, inspector FROM form_responses').fetchall()
            conn.execute('DELETE FROM form_templates WHERE id=?', (tpl,))
        self.assertEqual([(r['job_key'], r['inspector']) for r in rows], [(MINE, 'Mr. Yu')])


if __name__ == '__main__':
    unittest.main()
