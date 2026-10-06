"""Bulk import of checklists from an import plan."""
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
from openpyxl import Workbook
from test_checklists import sample_workbook


def plan_workbook(rows, translations):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Plan'
    ws.append(['File', 'Use', 'Name', 'Product types', 'Item codes', 'Reference', 'Notes'])
    for row in rows:
        ws.append(row)
    tr = wb.create_sheet('Translations')
    tr.append(['English', '中文', 'Source'])
    for en, zh in translations:
        tr.append([en, zh, 'x'])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class BulkImportTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'checklist_templates', 'checklist_versions'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('murphy','x','admin')")
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('yu','x','inspector')")
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}

    def client_for(self, name):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.ids[name]
            session['_csrf_token'] = 'tok'
        return client

    def post(self, client, plan, files):
        return client.post('/checklists/bulk', data={
            '_csrf_token': 'tok', 'plan': (io.BytesIO(plan), 'plan.xlsx'),
            'files': [(io.BytesIO(data), name) for name, data in files]}, content_type='multipart/form-data')

    def test_import_translate_update_and_skip(self):
        plan = plan_workbook([
            ['a.xlsx', 'Y', 'Valve covers', '', 'ACSV*, ACHB', '', ''],
            ['b.xlsx', 'Y', 'Handwheels', 'handwheel, bogus', '', '', ''],
            ['old.xlsx', 'N', 'Old', '', '', '', 'superseded'],
            ['gone.xlsx', 'Y', 'Gone', '', '', '', ''],
        ], [('Rate the cleanliness of the part', '清洁度'), ('Rework', '返工')])
        english = sample_workbook(with_chinese=False)
        murphy = self.client_for('murphy')
        page = self.post(murphy, plan, [('A.xlsx', english), ('b.xlsx', english)]).get_data(as_text=True)
        self.assertIn('新建 2，更新 0，跳过 1，缺文件 1，出错 0', page)
        with db_conn() as conn:
            rows = {r['name']: dict(r) for r in conn.execute('SELECT * FROM checklist_templates')}
        self.assertEqual(set(rows), {'Valve covers', 'Handwheels'})
        self.assertEqual(rows['Valve covers']['item_codes'], 'ACSV*, ACHB')
        self.assertEqual(rows['Handwheels']['product_types'], 'handwheel')
        self.assertTrue(all(r['active'] == 0 for r in rows.values()))          # start inactive
        _tpl, _ver, data = app.checklist_version(rows['Valve covers']['id'])
        feet = data['sections'][1]['questions'][0]
        self.assertEqual(feet['action_zh'], '返工')                             # filled from Translations

        # importing again: same content -> no new version; changed codes are updated
        plan2 = plan_workbook([['a.xlsx', 'Y', 'Valve covers', '', 'ACSV*', '', '']],
                              [('Rate the cleanliness of the part', '清洁度'), ('Rework', '返工')])
        page = self.post(murphy, plan2, [('a.xlsx', english)]).get_data(as_text=True)
        self.assertIn('新建 0，更新 1', page)
        tpl, ver, _data = app.checklist_version(rows['Valve covers']['id'])
        self.assertEqual((ver['version'], tpl['item_codes']), (1, 'ACSV*'))

    def test_translation_key_and_inspectors(self):
        self.assertEqual(checklists.translation_key('Is the part coated?  '), checklists.translation_key('is the PART coated'))
        self.assertEqual(self.client_for('yu').get('/checklists/bulk').status_code, 403)
        self.assertIn('name="plan"', self.client_for('murphy').get('/checklists/bulk').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
