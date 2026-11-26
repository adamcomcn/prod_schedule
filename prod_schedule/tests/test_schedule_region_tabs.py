"""Schedule page: everything under a region tab is about that region only
(shipped lines missing a QA BRT report, their history and counts)."""
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

H = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date']
SCHEDULE = {'MELBOURNE': [H, ['DPL1', 'PO-1', 'MEL-OPEN', 'Valve', '5', '2026-12-01']],
            'FIJI': [H, ['DPL2', 'PO-2', 'FJ-OPEN', 'Valve', '5', '2026-12-01']]}


class RegionTabTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app.save_json(app.CURRENT_FILE, SCHEDULE)
        app.save_json(app.PREVIOUS_FILE, SCHEDULE)
        app.save_json(app.INSPECTIONS_CACHE, {})
        with db_conn() as conn:
            for table in ('users', 'outstanding_jobs'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                         ('boss', generate_password_hash('x' * 12), 'admin'))
            self.uid = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            rows = [('MELBOURNE|PO-10|MEL-SHIP1', 'MELBOURNE', 0, 0), ('MELBOURNE|PO-11|MEL-SHIP2', 'MELBOURNE', 0, 0),
                    ('MELBOURNE|PO-12|MEL-DONE', 'MELBOURNE', 1, 0), ('FIJI|PO-20|FJ-SHIP', 'FIJI', 0, 0),
                    ('FIJI|PO-21|FJ-DONE', 'FIJI', 1, 0), ('OLDSHEET|PO-30|GONE-SHEET', 'OLDSHEET', 0, 0)]
            for key, sheet, completed, closed in rows:
                conn.execute('INSERT INTO outstanding_jobs (job_key, sheet, order_number, item_code, item_desc, '
                             'completed, closed, completed_at) VALUES (?,?,?,?,?,?,?,?)',
                             (key, sheet, key.split('|')[1], key.split('|')[2], 'Valve', completed, closed,
                              '2026-11-20' if completed else ''))

    def page(self, sheet):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.uid
            s['_csrf_token'] = 'tok'
        return c.get(f'/?sheet={sheet}').get_data(as_text=True)

    def test_each_tab_shows_its_own_region(self):
        mel = self.page('MELBOURNE')
        for code in ('MEL-SHIP1', 'MEL-SHIP2', 'MEL-DONE', 'GONE-SHEET'):
            self.assertIn(code, mel)
        for code in ('FJ-SHIP', 'FJ-DONE'):
            self.assertNotIn(code, mel)
        self.assertRegex(mel, r'MELBOURNE · (已全部出货但缺 QA BRT 报告|Fully Shipped Missing QA BRT Reports)')
        self.assertEqual(mel.count('class="out-check"'), 3)          # 2 MELBOURNE + 1 from a sheet no longer there
        fiji = self.page('FIJI')
        self.assertIn('FJ-SHIP', fiji)
        self.assertIn('FJ-DONE', fiji)
        self.assertNotIn('MEL-SHIP1', fiji)
        self.assertNotIn('MEL-DONE', fiji)
        self.assertEqual(fiji.count('class="out-check"'), 2)

    def test_tabs_count_open_lines_per_region(self):
        mel = self.page('MELBOURNE')
        tabs = mel.split('id="tabBar"')[1].split('</div>')[0]
        self.assertRegex(tabs, r'MELBOURNE<span class="tab-count"[^>]*>2</span>')
        self.assertRegex(tabs, r'FIJI<span class="tab-count"[^>]*>1</span>')
        # the tab bar comes before anything about the region
        self.assertLess(mel.index('id="tabBar"'), mel.index('id="outstanding"'))


if __name__ == '__main__':
    unittest.main()
