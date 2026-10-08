"""QA BRT checks after a weekly upload: Excel "QA BRTs Sent?" vs the reports in
the platform, and shipped lines without a report."""
import io
import os
import sys
import tempfile
import threading
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
     'Estimated Completion Date', 'QA BRTs Sent?']
NO_QA = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity']

PREVIOUS = {
    'MELBOURNE': [H,
        ['DPL1', 'PO-1', 'JS', 'RSV0100', 'DN100 Gate Valve', '10', '2026/10/1', 'YES'],     # shipped, no report
        ['DPL2', 'PO-2', 'JS', 'ES0300', 'Spindle 300', '50', '2026/10/10', 'YES'],          # shipped, has report
        ['DPL3', 'PO-3', 'JC', 'ACSVL', 'Sluice Valve Frame', '30', '2026/10/14', 'YES'],
        ['DPL4', 'PO-4', 'XM', 'GASK0080', 'DN80 Gasket', '1000', '2026/10/20', 'NO'],
        ['DPL5', 'PO-5', 'XM', 'UMC0100', 'DN100 Coupling', '12', '2026/10/30', 'yes'],
        ['DPL9', 'PO-9', 'XM', 'DFBF0100', 'DI Blank Flange', '100', '2026/10/05', '']],
    'DI FITTING': [NO_QA, ['DPL7', 'PO-7', 'DFT1008F', 'DN100 Tee', '20']]}
CURRENT = {
    'MELBOURNE': [H,
        ['DPL3', 'PO-3', 'JC', 'ACSVL', 'Sluice Valve Frame', '30', '2026/10/14', 'YES'],    # YES, no report
        ['DPL4', 'PO-4', 'XM', 'GASK0080', 'DN80 Gasket', '1000', '2026/10/20', 'NO'],       # report, Excel NO
        ['DPL5', 'PO-5', 'XM', 'UMC0100', 'DN100 Coupling', '12', '2026/10/30', 'yes'],      # report on FIJI: fine
        ['DPL6', 'PO-6', 'LX', 'APSSSR0150', 'SS316 Strap', '150', '2026/11/15', ' Yes '],   # new, YES, no report
        ['DPL9', 'PO-9', 'XM', 'DFBF0100', 'DI Blank Flange', '60', '2026/10/05', '']],      # 40 shipped, no report
    'DI FITTING': [NO_QA, ['DPL7', 'PO-7', 'DFT1008F', 'DN100 Tee', '20']]}                # no QA column


class QaBrtCheckTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app.save_json(app.PREVIOUS_FILE, PREVIOUS)
        app.save_json(app.CURRENT_FILE, CURRENT)
        app.save_json(app.INSPECTIONS_CACHE, {
            'MELBOURNE|PO-2|ES0300': [{'result': 'Pass'}],
            'MELBOURNE|PO-4|GASK0080': [{'result': 'Pass', 'inspection_date': '2026-10-02'}],
            'FIJI|PO-5|UMC0100': [{'result': 'Pass'}],          # same PO + item, another region: counts
        })
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            for name, role, email in (('boss', 'admin', 'boss@example.com'), ('murphy', 'lead', 'murphy@example.com'),
                                      ('yu', 'inspector', 'yu@example.com'), ('hq1', 'hq', 'hq1@example.com')):
                conn.execute('INSERT INTO users (username,password_hash,role,email) VALUES (?,?,?,?)',
                             (name, generate_password_hash('x' * 12), role, email))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        config = app.load_config()
        config.update(qa_mismatch_emails='supplier@example.com', hq_report_emails='qa-hq@example.com')
        app.save_json(app.CONFIG_FILE, config)
        self.sent = []

    def capture(self):
        def send(subject, body, recipients, attachments=(), html=None):
            self.sent.append({'subject': subject, 'body': body, 'to': recipients, 'html': html, 'files': attachments})
            return True, 'sent'
        return mock.patch.object(app, '_smtp_send', side_effect=send)

    def send(self, fn):
        with self.capture(), app.app.test_request_context(base_url='https://qc.example.com'):
            return fn()

    def test_the_three_lists(self):
        check = app.qa_brt_check()
        self.assertEqual([(l['item_code'], l['state']) for l in check['yes_no_report']],
                         [('ACSVL', 'Not shipped 未出货'), ('APSSSR0150', 'New 新增')])
        self.assertEqual([(l['item_code'], l['excel_qa'], l['report']) for l in check['report_not_yes']],
                         [('GASK0080', 'NO', 'Pass 2026-10-02')])
        self.assertEqual([(l['item_code'], l['shipment'], l['shipped_qty'], l['excel_qa'])
                          for l in check['shipped_no_report']],
                         [('RSV0100', 'Fully shipped 全部出货', '10', 'YES'),
                          ('DFBF0100', 'Partially shipped 部分出货', '40', '空 / blank')])
        self.assertEqual(app.qa_brt_mismatches(), check['yes_no_report'])     # same rule as the orange badge

    def test_check_e_mail_to_supplier_and_lead(self):
        self.assertEqual(self.send(app.send_qa_mismatch_email)[0], 3)
        mail = self.sent[0]
        self.assertEqual(mail['to'], ['supplier@example.com', 'murphy@example.com'])
        self.assertIn('Excel 与系统不一致 3 行', mail['subject'])
        self.assertIn('Excel 标 YES，平台无报告 / Excel YES, no report (2)', mail['html'])
        self.assertIn('平台有报告，Excel 未标 YES / Report exists, Excel not YES (1)', mail['html'])
        self.assertIn('https://qc.example.com/inspect/MELBOURNE%7CPO-3%7CACSVL', mail['html'])
        self.assertIn('Pass 2026-10-02', mail['html'])
        self.assertNotIn('RSV0100', mail['html'])                             # shipped: other e-mail
        wb = openpyxl.load_workbook(io.BytesIO(mail['files'][0][1]))
        self.assertEqual(wb.sheetnames, ['Excel YES no report', 'Report but Excel not YES'])
        self.assertEqual(list(wb['Report but Excel not YES'].values)[1][4], 'GASK0080')

    def test_shipped_without_report_e_mail_to_hq_lead_and_supplier(self):
        self.assertEqual(self.send(app.send_shipped_no_report_email)[0], 2)
        mail = self.sent[0]
        self.assertEqual(mail['to'], ['qa-hq@example.com', 'murphy@example.com', 'supplier@example.com'])
        self.assertIn('2 行已出货但系统无检验报告', mail['subject'])
        self.assertIn('Partially shipped 部分出货', mail['html'])
        rows = list(openpyxl.load_workbook(io.BytesIO(mail['files'][0][1])).active.values)
        self.assertEqual(rows[1][:2], ('Fully shipped 全部出货', 'MELBOURNE'))
        self.assertEqual(rows[2][-2:], ('40', '空 / blank'))

    def test_nothing_to_send(self):
        app.save_json(app.INSPECTIONS_CACHE, {})
        app.save_json(app.PREVIOUS_FILE, CURRENT)
        app.save_json(app.CURRENT_FILE, {'MELBOURNE': [H, ['DPL4', 'PO-4', 'XM', 'GASK0080', 'Gasket', '1', '', 'NO']]})
        app.save_json(app.PREVIOUS_FILE, app.load_json(app.CURRENT_FILE))
        self.assertEqual(self.send(app.send_qa_mismatch_email)[0], 0)
        self.assertEqual(self.send(app.send_shipped_no_report_email)[0], 0)
        self.assertEqual(self.sent, [])
        boss = self.client('boss')                                     # empty previews still work
        for kind in ('check', 'shipped'):
            self.assertIn('没有需要提醒', boss.get(f'/settings/qa-mismatch-preview?kind={kind}').get_data(as_text=True))
            self.assertEqual(boss.get(f'/settings/qa-mismatch-preview?kind={kind}&format=xlsx').status_code, 200)

    def test_after_upload_job_sends_all_three(self):
        calls = []
        with mock.patch.object(app, 'send_purchasing_email', side_effect=lambda: calls.append('purchasing') or (True, 'ok')), \
             mock.patch.object(app, 'send_qa_mismatch_email', side_effect=lambda: calls.append('check') or (1, 'ok')), \
             mock.patch.object(app, 'send_shipped_no_report_email', side_effect=lambda: calls.append('shipped') or (0, 'none')), \
             mock.patch.object(threading, 'Thread', side_effect=lambda target, daemon: mock.Mock(start=target)):
            app._after_upload_emails_in_background('https://qc.example.com/')
        self.assertEqual(calls, ['purchasing', 'check', 'shipped'])

    def client(self, name):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[name]
            s['_csrf_token'] = 'tok'
        return c

    def test_settings_previews_and_permissions(self):
        boss = self.client('boss')
        page = boss.get('/settings/qa-mismatch-preview').get_data(as_text=True)
        self.assertIn('Preview only', page)
        self.assertIn('supplier@example.com, murphy@example.com', page)
        self.assertIn('GASK0080', page)
        page = boss.get('/settings/qa-mismatch-preview?kind=shipped').get_data(as_text=True)
        self.assertIn('qa-hq@example.com, murphy@example.com, supplier@example.com', page)
        self.assertIn('DFBF0100', page)
        self.assertEqual(boss.get('/settings/qa-mismatch-preview?kind=shipped&format=xlsx').status_code, 200)
        for other in ('murphy', 'yu', 'hq1'):
            self.assertEqual(self.client(other).get('/settings/qa-mismatch-preview').status_code, 403)
            self.assertEqual(self.client(other).post('/settings/qa-mismatch-send', data={'_csrf_token': 'tok'}).status_code, 403)
        boss.post('/settings', data={'_csrf_token': 'tok', 'qa_mismatch_emails': 'a@example.com; nope',
                                     'hq_report_emails': 'qa-hq@example.com'})
        self.assertEqual(app.load_config()['qa_mismatch_emails'], 'a@example.com')
        with self.capture():
            boss.post('/settings/qa-mismatch-send', data={'_csrf_token': 'tok', 'kind': 'shipped'})
        self.assertEqual(self.sent[-1]['to'], ['qa-hq@example.com', 'murphy@example.com', 'a@example.com'])
        self.assertIn('出货缺 QA BRT', self.sent[-1]['subject'])


if __name__ == '__main__':
    unittest.main()
