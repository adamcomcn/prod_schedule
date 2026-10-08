"""Schedule changes for purchasing: shipped, date changes, new lines, region moves."""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
import app
from db import db_conn
from werkzeug.security import generate_password_hash

H = ['Order Number', 'Daemco Purchase Order', 'Supplier', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date', 'Must Ship Date', 'unit price']
F = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date', 'Foundry']

BEFORE = {
    'MELBOURNE': [H,
        ['DPL1', 'PO-1', 'JS', 'RSV0100', 'DN100 Gate Valve', '10', '2026/10/1', '2026-10-20', '99'],     # fully shipped
        ['DPL2', 'PO-2', 'JS', 'ES0300', 'Extension Spindle 300', '50', '2026/10/10', '', '5'],           # partly: 50 -> 20
        ['DPL3', 'PO-3', 'JC', 'ACSVL', 'VIC Sluice Valve Frame and lid', '30', '2026/10/14 ready to ship', '', '1'],
        ['DPL3', 'PO-3', 'JC', 'ACSVL', 'VIC Sluice Valve Frame and lid', '6', '2026/10/14 ready to ship', '', '1'],
        ['DPL4', 'PO-4', 'XM', 'GASK0080TD', 'DN80 EPDM GASKET', '1000', '2026/10/20', '2026-11-01', '1'],  # earlier
        ['DPL5', 'PO-5', 'XM', 'UMC0100', 'DN100 Coupling', '12', '2026/10/30', '', '1'],                  # remark only
        ['DPL6', 'PO-6', 'XM', 'DFC30PF', 'DN300 Connector', '36', '2026/10/12', '', '1'],                 # moves to FIJI
    ],
    'DI FITTING': [F, ['DPL7', 'PO-7', 'DFT1008F', 'DN100 Tee', '20', '2026/10/09', 'RB']]}            # unchanged

AFTER = {
    'MELBOURNE': [H,
        ['DPL2', 'PO-2', 'JS', 'ES0300', 'Extension Spindle 300', '20', '2026/10/10', '', '5'],
        ['DPL3', 'PO-3', 'JC', 'ACSVL', 'VIC Sluice Valve Frame and lid', '36', '2026/10/20 ready to ship', '', '1'],
        ['DPL4', 'PO-4', 'XM', 'GASK0080TD', 'DN80 EPDM GASKET', '1000', '2026/10/17', '2026-11-05', '1'],
        ['DPL5', 'PO-5', 'XM', 'UMC0100', 'DN100 Coupling', '12', '2026/10/30 waiting for casting', '', '1'],
        ['DPL8', 'PO-8', 'LX', 'APSSSR0150', 'SS316 STRAP DN150', '150', '2026/11/15', '', '1'],            # new
    ],
    'FIJI': [H, ['DPL6', 'PO-6', 'XM', 'DFC30PF', 'DN300 Connector', '36', '2026/10/12', '', '1']],
    'DI FITTING': [F, ['DPL7', 'PO-7', 'DFT1008F', 'DN100 Tee', '20', '2026/10/09', 'RB']]}


class PurchasingExportTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        shutil.rmtree(app.HISTORY_DIR, ignore_errors=True)
        os.makedirs(os.path.join(app.HISTORY_DIR, '20261001-080000-000000'))
        first = os.path.join(app.HISTORY_DIR, '20261001-080000-000000')
        app.save_json(os.path.join(first, 'meta.json'), {'filename': 'Production Schedule 01.10.xlsx', 'applied_at': '2026-10-01 08:00'})
        second = os.path.join(app.HISTORY_DIR, '20261008-080000-000000')
        os.makedirs(second)
        app.save_json(os.path.join(second, 'replaced_schedule.json'), BEFORE)
        app.save_json(os.path.join(second, 'meta.json'), {'filename': 'Production Schedule 08.10.xlsx', 'applied_at': '2026-10-08 08:00'})
        app.save_json(app.CURRENT_FILE, AFTER)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            for name, role in (('boss', 'admin'), ('hq1', 'hq'), ('yu', 'inspector')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (name, generate_password_hash('x' * 12), role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}

    def sheets(self, data):
        wb = openpyxl.load_workbook(io.BytesIO(data))
        return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}

    def test_changes(self):
        ch = app.purchasing_changes(BEFORE, AFTER)
        shipped = {(l['item_code'], l['type']): l for l in ch['shipped']}
        self.assertEqual(set(shipped), {('RSV0100', 'Fully shipped'), ('ES0300', 'Partially shipped')})
        self.assertEqual((shipped['ES0300', 'Partially shipped']['before'],
                          shipped['ES0300', 'Partially shipped']['shipped']), (50, 30))
        dates = {l['item_code']: l for l in ch['date_changes']}
        self.assertEqual([l['item_code'] for l in ch['date_changes']], ['ACSVL', 'GASK0080TD', 'UMC0100'])
        self.assertEqual((dates['ACSVL']['est_days'], dates['ACSVL']['qty']), (6, 36))      # split lots added up
        self.assertEqual((dates['GASK0080TD']['est_days'], dates['GASK0080TD']['ship_days']), (-3, 4))
        self.assertEqual((dates['UMC0100']['est_days'], dates['UMC0100']['est_note']), (None, 'Remark changed'))
        self.assertEqual([l['item_code'] for l in ch['new']], ['APSSSR0150'])
        self.assertEqual([(m['item_code'], m['from'], m['to']) for m in ch['moves']],
                         [('DFC30PF', 'MELBOURNE 36', 'FIJI 36')])

    def test_workbook_is_readable_and_has_no_prices(self):
        data, filename, _ch, upload = app.purchasing_export()
        self.assertEqual(filename, 'schedule-changes-for-purchasing-2026-10-08.xlsx')
        s = self.sheets(data)
        self.assertEqual(list(s), ['Summary', 'Shipped', 'Date Changes', 'New Lines', 'Region Moves'])
        flat = str(s)
        self.assertNotIn('99', flat.replace('2026', ''))          # unit price never exported
        self.assertNotIn('unit price', flat.lower())
        summary = s['Summary']
        self.assertIn('Production Schedule 08.10.xlsx (applied 2026-10-08 08:00 UTC)', flat)
        self.assertIn('Production Schedule 01.10.xlsx (applied 2026-10-01 08:00 UTC)', flat)
        counts = {r[0]: r[1] for r in summary if r and r[1] is not None and isinstance(r[1], int)}
        self.assertEqual(counts['Fully shipped (line left the schedule)'], 1)
        self.assertEqual(counts['Partially shipped (quantity went down)'], 1)
        self.assertEqual(counts['Est. completion delayed'], 1)
        self.assertEqual(counts['Est. completion earlier'], 1)
        self.assertEqual(counts['New lines'], 1)
        self.assertEqual(s['Shipped'][1][:3], ['Fully shipped', 'MELBOURNE', 'DPL1'])
        self.assertEqual(s['Shipped'][2][7:10], [50, 20, 30])
        dc = s['Date Changes']
        self.assertEqual(dc[1][4], 'ACSVL')
        self.assertEqual((dc[1][9], dc[1][10]), ('+6', 'Delayed 6 d'))
        self.assertEqual((dc[2][9], dc[2][13]), ('-3', '+4'))
        self.assertEqual(s['Region Moves'][1][:3], ['DPL6', 'PO-6', 'XM'])

    def test_needs_two_uploads(self):
        shutil.rmtree(os.path.join(app.HISTORY_DIR, '20261008-080000-000000'))
        self.assertIsNone(app.purchasing_export())

    def client(self, name):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[name]
            s['_csrf_token'] = 'tok'
        return c

    def test_download_permissions_and_e_mail(self):
        r = self.client('hq1').get('/export/purchasing.xlsx')
        self.assertEqual(r.status_code, 200)
        self.assertIn('schedule-changes-for-purchasing', r.headers['Content-Disposition'])
        self.assertEqual(self.client('yu').get('/export/purchasing.xlsx').status_code, 403)
        self.assertEqual(self.client('boss').get('/export/purchasing.xlsx?upload=nope').status_code, 302)
        page = self.client('boss').get('/').get_data(as_text=True)
        self.assertIn('/export/purchasing.xlsx', page)

        boss = self.client('boss')
        boss.post('/settings', data={'_csrf_token': 'tok', 'purchasing_emails': 'buy@example.com, bad'})
        self.assertEqual(app.load_config()['purchasing_emails'], 'buy@example.com')
        sent = []
        with mock.patch.object(app, '_smtp_send', side_effect=lambda *a, **k: (sent.append((a, k)), (True, 'ok'))[1]):
            boss.post('/settings/purchasing-send', data={'_csrf_token': 'tok'})
        (subject, body, to), kw = sent[0]
        self.assertEqual(to, ['buy@example.com'])
        self.assertIn('2 shipped, 3 date changes', subject)
        self.assertIn('Fully shipped 全部出货: 1', body)
        self.assertTrue(kw['attachments'][0][0].endswith('.xlsx'))
        self.assertEqual(self.client('hq1').post('/settings/purchasing-send', data={'_csrf_token': 'tok'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
