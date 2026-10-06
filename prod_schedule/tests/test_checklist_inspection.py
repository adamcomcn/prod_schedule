"""Checklist on the inspection page: server draft, photos and submission."""
import io
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import checklists
from db import db_conn
from helpers import assign_job

JOB = 'MELBOURNE|PO-7|DFT1008F'
TEMPLATE = {'title': 'DI fittings', 'product_photo': True, 'sections': [
    {'id': 's1', 'name': 'Product Marking', 'name_zh': '产品标识', 'optional': False, 'questions': [
        {'id': 'q1', 'text': 'Is the DN correct?', 'text_zh': 'DN 是否正确？', 'type': 'yes_no', 'fail_on': 'no',
         'action': 'Reject', 'action_zh': '拒收'},
        {'id': 'q2', 'text': 'Any spray paint on stickers?', 'type': 'yes_no', 'fail_on': 'yes', 'action': 'Clean'}]},
    {'id': 's2', 'name': 'External Body', 'optional': False, 'questions': [
        {'id': 'q3', 'text': 'Coating thickness', 'type': 'number', 'min': 300, 'unit': 'μm', 'action': 'Reject'}]},
    {'id': 's3', 'name': 'Spigot', 'optional': True, 'questions': [
        {'id': 'q4', 'text': 'Casting defects?', 'type': 'yes_no', 'fail_on': 'yes', 'action': 'Reject'}]},
]}


class ChecklistInspectionTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks', 'inspection_attachments', 'inspection_drafts', 'draft_files',
                          'checklist_templates', 'checklist_versions', 'product_reference'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute("INSERT INTO users (username,password_hash,role,display_name) VALUES ('yu','x','inspector','Mr. Yu')")
            conn.execute("INSERT INTO users (username,password_hash,role,display_name) VALUES ('wang','x','inspector','Wang')")
            conn.execute("INSERT INTO users (username,password_hash,role,display_name) VALUES ('murphy','x','lead','Murphy')")
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
            tid = conn.execute("INSERT INTO checklist_templates (name, product_types) VALUES ('DI', 'di_fitting')").lastrowid
            conn.execute('INSERT INTO checklist_versions (template_id, version, data_json) VALUES (?, 1, ?)',
                         (tid, json.dumps(checklists.normalise_template(TEMPLATE))))
            conn.execute('UPDATE checklist_templates SET current_version=1 WHERE id=?', (tid,))
            self.tid = tid
        schedule = {'MELBOURNE': [['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity'],
                                  ['DPL7', 'PO-7', 'DFT1008F', 'DN100 x 80 Flange Tee DI', '20']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {})
        config = app.load_config()
        config.update(hq_report_emails='', task_notify_emails='')
        app.save_json(app.CONFIG_FILE, config)
        assign_job(JOB, 'yu')

    def client_for(self, name):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.ids[name]
            session['_csrf_token'] = 'tok'
        return client

    def upload(self, client, ref, name='p.jpg'):
        response = client.post(f'/inspect/{JOB}/draft/files', headers={'X-CSRF-Token': 'tok'}, data={
            'ref': ref, 'template_id': self.tid, 'version': 1, 'file': (io.BytesIO(b'jpegdata'), name)},
            content_type='multipart/form-data')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()['file']

    def submit(self, client, answers, **extra):
        data = {'_csrf_token': 'tok', 'region': 'MELBOURNE', 'order_number': 'DPL7', 'item_code': 'DFT1008F',
                'item_description': 'DN100 x 80 Flange Tee DI', 'inspection_date': '2026-10-09',
                'quantity_inspected': '20', 'result': 'Fail', 'checklist_template_id': self.tid,
                'checklist_version': 1, 'checklist_json': json.dumps(answers)}
        data.update(extra)
        return client.post(f'/inspect/{JOB}/submit', data=data, content_type='multipart/form-data')

    def test_page_draft_and_photos(self):
        yu = self.client_for('yu')
        page = yu.get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('id="checklist-config"', page)
        self.assertIn('checklist.js', page)
        saved = yu.post(f'/inspect/{JOB}/draft', headers={'X-CSRF-Token': 'tok'},
                        json={'template_id': self.tid, 'version': 1, 'answers': {'q1': {'v': 'yes'}},
                              'fields': {'quantity_inspected': '20'}})
        self.assertTrue(saved.get_json()['ok'])
        photo = self.upload(yu, 'product')
        draft = yu.get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('"q1": {"v": "yes"}', draft)
        self.assertIn(photo['url'], draft)
        self.assertEqual(yu.get(photo['url']).status_code, 200)
        # another inspector cannot see or delete it
        wang = self.client_for('wang')
        self.assertEqual(wang.get(photo['url']).status_code, 404)
        self.assertEqual(wang.post(f'/inspect/{JOB}/draft/files/{photo["id"]}/delete',
                                   headers={'X-CSRF-Token': 'tok'}).status_code, 404)
        # ... nor write a draft for a job that is not theirs
        self.assertEqual(wang.post(f'/inspect/{JOB}/draft', headers={'X-CSRF-Token': 'tok'},
                                   json={'answers': {}}).status_code, 403)
        # the owner can delete it
        self.assertTrue(yu.post(f'/inspect/{JOB}/draft/files/{photo["id"]}/delete',
                                headers={'X-CSRF-Token': 'tok'}).get_json()['ok'])
        self.assertNotIn(photo['url'], yu.get(f'/inspect/{JOB}').get_data(as_text=True))

    def test_incomplete_checklist_is_refused(self):
        yu = self.client_for('yu')
        self.upload(yu, 'product')
        response = self.submit(yu, {'q1': {'v': 'no'}, 'q2': {'v': 'no'}, 'q3': {'v': '320'}})   # q4 missing, q1 failed w/o photo
        self.assertEqual(response.status_code, 302)
        self.assertEqual(app.load_json(app.INSPECTIONS_CACHE, {}), {})
        flashes = yu.get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('检查清单还有 2 项没有完成', flashes)
        # posting without any checklist is refused too
        self.submit(yu, {}, checklist_template_id='')
        self.assertEqual(app.load_json(app.INSPECTIONS_CACHE, {}), {})

    def test_complete_submission(self):
        yu = self.client_for('yu')
        self.upload(yu, 'product')
        defect = self.upload(yu, 'q3', 'thin.jpg')
        answers = {'q1': {'v': 'yes'}, 'q2': {'v': 'no'}, 'q3': {'v': '280', 'occ': '2', 'sup': True, 'note': 'thin edge'},
                   'q4': {'v': 'na'}}
        self.assertEqual(self.submit(yu, answers).status_code, 302)
        record = app.load_json(app.INSPECTIONS_CACHE, {})[JOB][0]
        c = record['checklist']
        self.assertEqual(c['counts'], {'ok': 2, 'fail': 1, 'na': 1, 'total': 4})
        self.assertEqual(c['suggested_result'], 'Fail')
        self.assertEqual(c['failed'][0]['occurrences'], '2')
        self.assertIn('Coating thickness — 280 μm ×2 → Reject', record['defects'])
        self.assertEqual(c['photos']['q3'] and len(c['photos']['q3']), 1)
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM inspection_drafts').fetchone()[0], 0)
            att = conn.execute("SELECT * FROM inspection_attachments WHERE ref='q3'").fetchone()
        self.assertEqual((att['evidence_type'], att['original_name']), ('checklist_photo', defect['name']))
        # the report page shows the checklist summary with the defect photo; reviewers see it too
        page = self.client_for('murphy').get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('4 项：合格 2，不合格 1，不适用 1', page)
        self.assertIn(f'/attachments/{att["id"]}', page)

        # PDF: checklist section with every question, the failed one with its action
        pdf = self.client_for('murphy').get(f'/inspect/{JOB}/report.pdf')
        self.assertEqual(pdf.status_code, 200)
        try:
            from pypdf import PdfReader
        except ImportError:
            return
        text = ''.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.get_data())).pages)
        self.assertIn('Site checklist', text)
        self.assertIn('Coating thickness', text)
        self.assertIn('280 μm', text)
        self.assertIn('Spigot', text)                      # whole section N/A: one line
        self.assertIn('Checklist photos', text)

    def test_products_without_checklist_are_unchanged(self):
        with db_conn() as conn:
            conn.execute("UPDATE checklist_templates SET product_types=''")
        yu = self.client_for('yu')
        self.assertNotIn('checklist-config', yu.get(f'/inspect/{JOB}').get_data(as_text=True))
        response = self.submit(yu, {}, checklist_template_id='', checklist_version='', result='Pass')
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('checklist', app.load_json(app.INSPECTIONS_CACHE, {})[JOB][0])


if __name__ == '__main__':
    unittest.main()
