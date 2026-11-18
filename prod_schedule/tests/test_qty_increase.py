"""A line whose quantity goes UP (12 -> 24) is flagged: on the schedule page, on the
inspection page when it was inspected for less, and in the purchasing export."""
import io
import os
import re
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
import app
from db import db_conn
from werkzeug.security import generate_password_hash

H = ['Order Number', 'Daemco Purchase Order', 'Supplier', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date']
NZ = 'NEW ZEALAND'
UP = f'{NZ}|PO-2644|DFT2508F'          # 12 -> 24 since last week, inspected at 12
SAME = f'{NZ}|PO-2650|DFT1008F'        # unchanged, inspected at 30
LESS = f'{NZ}|PO-2660|DFC30PF'         # 40 -> 10 (partly shipped)
OLDUP = f'{NZ}|PO-2670|UMC0100'        # 50 both weeks, but inspected when it was 40

PREVIOUS = {NZ: [H,
                 ['DPL2644', 'PO-2644', 'XM', 'DFT2508F', 'DN250 x 80 Tee', '12', '2026-11-20'],
                 ['DPL2650', 'PO-2650', 'XM', 'DFT1008F', 'DN100 Tee', '30', '2026-11-20'],
                 ['DPL2660', 'PO-2660', 'XM', 'DFC30PF', 'DN300 Connector', '40', '2026-11-20'],
                 ['DPL2670', 'PO-2670', 'XM', 'UMC0100', 'DN100 Coupling', '50', '2026-11-20']]}
CURRENT = {NZ: [H,
                ['DPL2644', 'PO-2644', 'XM', 'DFT2508F', 'DN250 x 80 Tee', '24', '2026-11-20'],
                ['DPL2650', 'PO-2650', 'XM', 'DFT1008F', 'DN100 Tee', '30', '2026-11-20'],
                ['DPL2660', 'PO-2660', 'XM', 'DFC30PF', 'DN300 Connector', '10', '2026-11-20'],
                ['DPL2670', 'PO-2670', 'XM', 'UMC0100', 'DN100 Coupling', '50', '2026-11-20']]}
REPORTS = {UP: [{'result': 'Pass', 'quantity_ordered': '12', 'inspector_name': 'Murphy'}],
           SAME: [{'result': 'Pass', 'quantity_ordered': '30'}],
           OLDUP: [{'result': 'Pass', 'quantity_ordered': '40'}]}


class QtyIncreaseTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app.save_json(app.PREVIOUS_FILE, PREVIOUS)
        app.save_json(app.CURRENT_FILE, CURRENT)
        app.save_json(app.INSPECTIONS_CACHE, REPORTS)
        with db_conn() as conn:
            for table in ('users', 'outstanding_jobs', 'inspection_tasks'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                         ('boss', generate_password_hash('x' * 12), 'admin'))
            self.uid = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]

    def client(self):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.uid
            s['_csrf_token'] = 'tok'
        return c

    def test_compare_finds_increases_but_keeps_the_status(self):
        info = {}
        statuses, _t, _s = app.compute_changes(PREVIOUS, CURRENT, moves_out=info)
        self.assertEqual(info['qty_up'], {UP: (12.0, 24.0)})
        self.assertEqual((statuses[UP], statuses[LESS], statuses[SAME]),
                         ('not_shipped', 'partially_shipped', 'not_shipped'))

    def test_inspected_for_less_than_now(self):
        found = app.inspected_qty_increases(CURRENT, app.SharedReports(REPORTS))
        self.assertEqual(found, {UP: ('12', '24'), OLDUP: ('40', '50')})
        # the latest report with a quantity decides
        self.assertIsNone(app.report_qty_increase([{'quantity_ordered': '12'}, {'quantity_ordered': '24'}], '24'))
        self.assertEqual(app.report_qty_increase([{'quantity_ordered': '12'}, {'result': 'Pass'}], '24.0'),
                         ('12', '24'))
        self.assertIsNone(app.report_qty_increase([], '24'))
        self.assertIsNone(app.report_qty_increase([{'quantity_ordered': '30'}], '10'))
        self.assertIsNone(app.report_qty_increase([{'quantity_ordered': ''}], '10'))

    def test_schedule_page_badge_note_and_filter(self):
        c = self.client()
        page = c.get(f'/?sheet={NZ}').get_data(as_text=True)
        self.assertIn('status=qty_up', page)
        self.assertRegex(page, r'(数量增加|QTY UP) 12 &rarr; 24')
        self.assertRegex(page, r'检验时数量 12，现在 24|Inspected at 12, now 24')
        self.assertRegex(page, r'检验时数量 40，现在 50|Inspected at 40, now 50')
        self.assertNotIn('30 &rarr;', page)
        filtered = c.get(f'/?sheet={NZ}&status=qty_up').get_data(as_text=True)
        keys = set(re.findall(r'data-key="([^"]+)"', filtered))
        self.assertEqual(keys, {UP, OLDUP})

    def test_inspection_page_warns(self):
        c = self.client()
        page = c.get(f'/inspect/{UP}').get_data(as_text=True)
        self.assertRegex(page, r'上次检验时订单数量是 12，现在排期上是 24|order qty was 12; the schedule now says 24')
        page = c.get(f'/inspect/{SAME}').get_data(as_text=True)
        self.assertNotIn('上次检验时订单数量是', page)
        self.assertNotIn('the schedule now says', page)

    def test_purchasing_export_lists_increases(self):
        ch = app.purchasing_changes(PREVIOUS, CURRENT)
        self.assertEqual(len(ch['qty_up']), 1)
        line = ch['qty_up'][0]
        self.assertEqual((line['dpl'], line['before'], line['after'], line['added'], line['has_report']),
                         ('DPL2644', 12.0, 24.0, 12.0, True))
        self.assertIn('Last inspection report was for 12', line['note'])
        self.assertEqual([l['dpl'] for l in ch['shipped']], ['DPL2660'])       # the decrease stays "shipped"
        wb = openpyxl.load_workbook(io.BytesIO(app.purchasing_workbook(ch, 'before', 'after')))
        rows = [list(r) for r in wb['Quantity Changes'].iter_rows(values_only=True)]
        self.assertEqual(rows[0][:10], ['Region', 'DPL', 'PO', 'Supplier', 'Item Code', 'Description',
                                        'Previous Qty', 'Current Qty', 'Added', 'QA Report'])
        self.assertEqual(rows[1][:10], [NZ, 'DPL2644', 'PO-2644', 'XM', 'DFT2508F', 'DN250 x 80 Tee',
                                        12, 24, '+12', 'Yes'])
        summary = {r[0]: r[1] for r in wb['Summary'].iter_rows(values_only=True) if r and r[0]}
        self.assertEqual(summary['Quantity increased'], 1)


if __name__ == '__main__':
    unittest.main()
