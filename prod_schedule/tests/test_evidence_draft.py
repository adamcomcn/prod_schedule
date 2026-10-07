"""Required-evidence files go into the server draft when picked, so an
inspection started on an iPad / phone can be finished and submitted on a PC."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import checklists
from db import db_conn
from helpers import assign_job

VALVE = 'MELBOURNE|PO-1|RSV0250FLFLCC'          # no checklist: BRT, Spark, DAQ, V-Trust evidence
FITTING = 'MELBOURNE|PO-7|DFT1008F'             # has a checklist: BRT + pressure evidence
HEADERS = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity']
TEMPLATE = {'title': 'DI fittings', 'product_photo': False, 'sections': [
    {'id': 's1', 'name': 'Marking', 'optional': False, 'questions': [
        {'id': 'q1', 'text': 'Is the DN correct?', 'type': 'yes_no', 'fail_on': 'no', 'action': 'Reject'}]}]}


class EvidenceDraftTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks', 'inspection_attachments', 'inspection_drafts', 'draft_files',
                          'checklist_templates', 'checklist_versions', 'product_reference'):
                conn.execute(f'DELETE FROM {table}')
            for name, role in (('yu', 'inspector'), ('wang', 'inspector')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)', (name, 'x', role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
            self.tid = conn.execute("INSERT INTO checklist_templates (name, product_types) VALUES ('DI', 'di_fitting')").lastrowid
            conn.execute('INSERT INTO checklist_versions (template_id, version, data_json) VALUES (?, 1, ?)',
                         (self.tid, json.dumps(checklists.normalise_template(TEMPLATE))))
            conn.execute('UPDATE checklist_templates SET current_version=1 WHERE id=?', (self.tid,))
        schedule = {'MELBOURNE': [HEADERS,
                                  ['DPL1', 'PO-1', 'RSV0250FLFLCC', 'DN250 Resilient Seated Gate Valve CC Flange Flange', '4'],
                                  ['DPL7', 'PO-7', 'DFT1008F', 'DN100 x 80 Flange Tee DI', '20']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {})
        config = app.load_config()
        config.update(hq_report_emails='', task_notify_emails='')
        app.save_json(app.CONFIG_FILE, config)
        assign_job(VALVE, 'yu')
        assign_job(FITTING, 'yu')

    def device(self, name='yu'):
        """A separate browser (iPad, PC …) signed in as `name`."""
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.ids[name]
            session['_csrf_token'] = 'tok'
        return client

    def upload(self, client, job, ref, name, content=b'video-bytes', **extra):
        return client.post(f'/inspect/{job}/draft/files', headers={'X-CSRF-Token': 'tok'},
                           data={'ref': ref, 'file': (io.BytesIO(content), name), **extra},
                           content_type='multipart/form-data')

    def submit(self, client, job, code, desc, **extra):
        data = {'_csrf_token': 'tok', 'region': 'MELBOURNE', 'item_code': code, 'item_description': desc,
                'inspection_date': '2026-10-08', 'quantity_inspected': '4', 'result': 'Pass', **extra}
        return client.post(f'/inspect/{job}/submit', data=data, content_type='multipart/form-data')

    def records(self, job):
        return app.load_json(app.INSPECTIONS_CACHE, {}).get(job, [])

    def test_ipad_uploads_and_pc_submits(self):
        ipad, pc = self.device(), self.device()
        video = self.upload(ipad, VALVE, 'ev:vtrust', 'pressure.MOV')
        self.assertEqual(video.status_code, 200, video.get_data(as_text=True))
        video = video.get_json()['file']
        self.assertTrue(video['video'])
        brt = self.upload(ipad, VALVE, 'ev:brt', 'brt.pdf').get_json()['file']
        # form fields of a product without a checklist are kept on the server too
        ipad.post(f'/inspect/{VALVE}/draft', headers={'X-CSRF-Token': 'tok'},
                  json={'template_id': None, 'version': None, 'answers': {},
                        'fields': {'quantity_inspected': '4', 'notes': 'from the iPad'}})

        page = pc.get(f'/inspect/{VALVE}').get_data(as_text=True)
        self.assertIn(video['url'], page)
        self.assertIn('"vtrust": [', page)
        self.assertIn('from the iPad', page)
        self.assertNotIn('name="ev_file_', page)         # files are no longer posted with the form
        self.assertEqual(pc.get(video['url']).status_code, 200)

        response = self.submit(pc, VALVE, 'RSV0250FLFLCC', 'DN250 Resilient Seated Gate Valve CC Flange Flange',
                               ev_result_vtrust='Pass', ev_result_brt='Pass', ev_result_spark='N/A', ev_result_daq='N/A')
        self.assertEqual(response.status_code, 302)
        [record] = self.records(VALVE)
        self.assertEqual(record['evidence']['vtrust']['files'], ['pressure.MOV'])
        self.assertEqual(record['evidence']['brt']['files'], ['brt.pdf'])
        self.assertEqual(record['missing_evidence'], [])
        with db_conn() as conn:
            atts = {r['evidence_type']: r for r in conn.execute('SELECT * FROM inspection_attachments WHERE job_key=?', (VALVE,))}
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM draft_files').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM inspection_drafts').fetchone()[0], 0)
        self.assertEqual(set(atts), {'vtrust', 'brt'})
        self.assertTrue(os.path.exists(atts['vtrust']['file_path']))
        self.assertEqual(atts['vtrust']['result'], 'Pass')
        self.assertNotEqual(brt['id'], None)

    def test_evidence_files_are_not_mixed_with_checklist_photos(self):
        yu = self.device()
        self.upload(yu, FITTING, 'ev:pressure', 'test.mp4', template_id=self.tid, version=1)
        self.upload(yu, FITTING, 'q1', 'dn.jpg', template_id=self.tid, version=1)
        response = self.submit(yu, FITTING, 'DFT1008F', 'DN100 x 80 Flange Tee DI', checklist_template_id=self.tid,
                               checklist_version=1, checklist_json=json.dumps({'q1': {'v': 'yes'}}),
                               ev_result_pressure='Pass', ev_result_brt='N/A')
        self.assertEqual(response.status_code, 302)
        [record] = self.records(FITTING)
        self.assertEqual(record['evidence']['pressure']['files'], ['test.mp4'])
        self.assertEqual(list(record['checklist']['photos']), ['q1'])
        with db_conn() as conn:
            kinds = sorted(r[0] for r in conn.execute('SELECT evidence_type FROM inspection_attachments'))
        self.assertEqual(kinds, ['checklist_photo', 'pressure'])

    def test_other_users_cannot_use_my_draft_files(self):
        mine = self.upload(self.device(), VALVE, 'ev:brt', 'brt.pdf').get_json()['file']
        wang = self.device('wang')
        self.assertEqual(wang.get(mine['url']).status_code, 404)
        self.assertEqual(self.upload(wang, VALVE, 'ev:brt', 'x.pdf').status_code, 403)   # not his job
        self.assertNotIn(mine['url'], wang.get(f'/inspect/{VALVE}').get_data(as_text=True))

    def test_file_types_and_size_limit(self):
        yu = self.device()
        self.assertEqual(self.upload(yu, VALVE, 'ev:brt', 'run.exe').status_code, 400)
        self.assertEqual(self.upload(yu, VALVE, 'ev:brt', 'scan.heif').status_code, 200)   # evidence accepts HEIF
        with mock.patch.object(app, 'DRAFT_FILE_MAX_BYTES', 1000):
            too_big = self.upload(yu, VALVE, 'ev:vtrust', 'long.mov', content=b'x' * 5000)
        self.assertEqual(too_big.status_code, 413)
        self.assertIn('上限', too_big.get_json()['message'])

    def test_large_draft_file_allowed_only_when_signed_in(self):
        big = b'x' * 3000
        with mock.patch.dict(app.app.config, {'MAX_CONTENT_LENGTH': 1000}):
            self.assertEqual(self.upload(self.device(), VALVE, 'ev:vtrust', 'clip.mov', content=big).status_code, 200)
            anonymous = app.app.test_client()
            response = anonymous.post(f'/inspect/{VALVE}/draft/files', headers={'X-CSRF-Token': 'tok'},
                                      data={'ref': 'ev:vtrust', 'file': (io.BytesIO(big), 'clip.mov')},
                                      content_type='multipart/form-data')
            self.assertIn(response.status_code, (400, 413))


if __name__ == '__main__':
    unittest.main()
