"""Factory codes from Settings: names, and aliases counted as one factory."""
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

CODES = 'RB=瑞来宝荣宝\nYX=永欣\nCT=昌泰\nRAINBOW=RB\nChangtai=CT\nCANGZHOU=YX\nLOOP1=LOOP2\nLOOP2=LOOP1'


class FactoryCodeTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        config = app.load_config()
        config['factory_names'] = CODES
        config['modules'] = {}
        app.save_json(app.CONFIG_FILE, config)
        with db_conn() as conn:
            for t in ('users', 'inspection_tasks'):
                conn.execute(f'DELETE FROM {t}')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('boss',?,'admin')",
                         (generate_password_hash('x' * 12),))
            self.uid = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            for i, sup in enumerate(('RB', 'Rainbow', 'rainbow ', 'YX', 'Cangzhou', 'XM', '')):
                conn.execute("INSERT INTO inspection_tasks (job_key,order_number,region,item_code,supplier,status) "
                             "VALUES (?,?,?,?,?,'Pending')", (f'DI FITTING|PO-{i}|ITEM{i}', f'DPL{i}', 'DI FITTING', f'ITEM{i}', sup))
        app.save_json(app.INSPECTIONS_CACHE, {
            'DI FITTING|PO-0|ITEM0': [{'result': 'Pass', 'supplier': 'RB', 'inspection_date': '2026-10-01'}],
            'DI FITTING|PO-1|ITEM1': [{'result': 'Fail', 'supplier': 'Rainbow', 'inspection_date': '2026-10-02'}]})
        app.save_json(app.CURRENT_FILE, {})
        app.save_json(app.PREVIOUS_FILE, {})

    def client(self):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.uid
            s['_csrf_token'] = 'tok'
        return c

    def test_aliases_and_names(self):
        self.assertEqual(app.factory_names(), {'RB': '瑞来宝荣宝', 'YX': '永欣', 'CT': '昌泰'})
        self.assertEqual([app.canonical_factory(c) for c in ('Rainbow', 'RAINBOW', ' rainbow ', 'changtai', 'XM', 'xm', '')],
                         ['RB', 'RB', 'RB', 'CT', 'XM', 'xm', ''])
        self.assertEqual(app.factory_label('Cangzhou'), 'YX 永欣')
        self.assertEqual(app.factory_label('Unknown Foundry'), 'Unknown Foundry')
        self.assertIn(app.canonical_factory('LOOP1'), ('LOOP1', 'LOOP2'))          # a loop does not hang

    def test_tasks_count_aliases_as_one_factory(self):
        page = self.client().get('/tasks?scope=all').get_data(as_text=True)
        self.assertIn('RB 瑞来宝荣宝', page)
        self.assertNotIn('Rainbow', page)
        self.assertNotIn('Cangzhou', page)
        options = page.count('<option value="RB">')
        self.assertEqual(options, 1)                                              # one filter entry
        import io
        import openpyxl
        data = self.client().get('/tasks/export.xlsx?scope=all&supplier=RB').data
        rows = list(openpyxl.load_workbook(io.BytesIO(data)).active.values)[1:]
        self.assertEqual(len([r for r in rows if r and r[0]]), 3)                 # RB + 2 x Rainbow

    def test_reports_group_aliases(self):
        page = self.client().get('/reports').get_data(as_text=True)
        self.assertIn('RB 瑞来宝荣宝', page)
        self.assertIn('2 份', page.split('RB 瑞来宝荣宝')[1][:200])                # both reports under RB
        self.assertNotIn('Rainbow', page)


if __name__ == '__main__':
    unittest.main()
