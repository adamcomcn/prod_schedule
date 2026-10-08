"""QA BRT mismatch e-mail: Excel says "QA BRTs Sent? = YES" but no report exists."""
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
        ['DPL5', 'PO-5', 'XM', 'UMC0100', 'DN100 Coupling', '12', '2026/10/30', 'yes']],
    'DI FITTING': [NO_QA, ['DPL7', 'PO-7', 'DFT1008F', 'DN100 Tee', '20']]}
CURRENT = {
    'MELBOURNE': [H,
        ['DPL3', 'PO-3', 'JC', 'ACSVL', 'Sluice Valve Frame', '30', '2026/10/14', 'YES'],    # no report -> listed
        ['DPL4', 'PO-4', 'XM', 'GASK0080', 'DN80 Gasket', '1000', '2026/10/20', 'NO'],       # NO -> fine
        ['DPL5', 'PO-5', 'XM', 'UMC0100', 'DN100 Coupling', '12', '2026/10/30', 'yes'],      # report on FIJI -> fine
        ['DPL6', 'PO-6', 'LX', 'APSSSR0150', 'SS316 Strap', '150', '2026/11/15', ' Yes ']],  # new, no report -> listed
    'DI FITTING': [NO_QA, ['DPL7', 'PO-7', 'DFT1008F', 'DN100 Tee', '20']]}                # no QA column


class QaMismatchTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app.save_json(app.PREVIOUS_FILE, PREVIOUS)
        app.save_json(app.CURRENT_FILE, CURRENT)
        app.save_json(app.INSPECTIONS_CACHE, {
            'MELBOURNE|PO-2|ES0300': [{'result': 'Pass'}],
            'FIJI|PO-5|UMC0100': [{'result': 'Pass'}],          # same PO + item, another region: counts
        })
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            for name, role, email in (('boss', 'admin', 'boss@example.com'), ('murphy', 'lead', 'murphy@example.com'),
                                      ('yu', 'inspector', 'yu@example.com')):
                conn.execute('INSERT INTO users (username,password_hash,role,email) VALUES (?,?,?,?)',
                             (name, generate_password_hash('x' * 12), role, email))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        config = app.load_config()
        config['qa_mismatch_emails'] = 'supplier@example.com'
        app.save_json(app.CONFIG_FILE, config)
        self.sent = []

    def capture(self):
        def send(subject, body, recipients, attachments=(), html=None):
            self.sent.append({'subject': subject, 'body': body, 'to': recipients, 'html': html, 'files': attachments})
            return True, 'sent'
        return mock.patch.object(app, '_smtp_send', side_effect=send)

    def test_lines_match_the_schedule_badge(self):
        lines = app.qa_brt_mismatches()
        self.assertEqual([(l['item_code'], l['state']) for l in lines],
                         [('RSV0100', 'Shipped 已出货'), ('ACSVL', 'Not shipped 未出货'), ('APSSSR0150', 'New 新增')])
        self.assertEqual(lines[0]['supplier'], 'JS')

    def test_e_mail_to_supplier_and_lead(self):
        with self.capture(), app.app.test_request_context(base_url='https://qc.example.com'):
            self.assertEqual(app.send_qa_mismatch_email()[0], 3)
        mail = self.sent[0]
        self.assertEqual(mail['to'], ['supplier@example.com', 'murphy@example.com'])   # not admin / inspector
        self.assertIn('3 行 Excel 标 YES 但系统无检验报告', mail['subject'])
        self.assertIn('https://qc.example.com/inspect/MELBOURNE%7CPO-3%7CACSVL', mail['html'])
        self.assertIn('改为 NO', mail['body'])
        name, data, _ = mail['files'][0]
        self.assertTrue(name.startswith('QA BRT mismatch ') and name.endswith('.xlsx'))
        rows = list(openpyxl.load_workbook(io.BytesIO(data)).active.values)
        self.assertEqual(rows[1][:5], ('MELBOURNE', 'DPL1', 'PO-1', 'JS', 'RSV0100'))
        self.assertEqual(rows[1][-2:], ('YES', 'NO'))

    def test_nothing_to_send(self):
        app.save_json(app.INSPECTIONS_CACHE, {k: [{'result': 'Pass'}] for k in (
            'MELBOURNE|PO-1|RSV0100', 'MELBOURNE|PO-2|ES0300', 'MELBOURNE|PO-3|ACSVL',
            'FIJI|PO-5|UMC0100', 'MELBOURNE|PO-6|APSSSR0150')})
        with self.capture():
            self.assertEqual(app.send_qa_mismatch_email()[0], 0)
        self.assertEqual(self.sent, [])

    def test_after_upload_job_sends_both_e_mails(self):
        calls = []
        with mock.patch.object(app, 'send_purchasing_email', side_effect=lambda: calls.append('purchasing') or (True, 'ok')), \
             mock.patch.object(app, 'send_qa_mismatch_email', side_effect=lambda: calls.append('qa') or (1, 'ok')), \
             mock.patch.object(threading, 'Thread', side_effect=lambda target, daemon: mock.Mock(start=target)):
            app._after_upload_emails_in_background('https://qc.example.com/')
        self.assertEqual(calls, ['purchasing', 'qa'])

    def client(self, name):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[name]
            s['_csrf_token'] = 'tok'
        return c

    def test_settings_preview_and_permissions(self):
        boss = self.client('boss')
        page = boss.get('/settings/qa-mismatch-preview').get_data(as_text=True)
        self.assertIn('Preview only', page)
        self.assertIn('supplier@example.com, murphy@example.com', page)
        self.assertIn('ACSVL', page)
        self.assertEqual(boss.get('/settings/qa-mismatch-preview?format=xlsx').status_code, 200)
        for other in ('murphy', 'yu'):
            self.assertEqual(self.client(other).get('/settings/qa-mismatch-preview').status_code, 403)
            self.assertEqual(self.client(other).post('/settings/qa-mismatch-send', data={'_csrf_token': 'tok'}).status_code, 403)
        boss.post('/settings', data={'_csrf_token': 'tok', 'qa_mismatch_emails': 'a@example.com; nope'})
        self.assertEqual(app.load_config()['qa_mismatch_emails'], 'a@example.com')
        with self.capture():
            boss.post('/settings/qa-mismatch-send', data={'_csrf_token': 'tok'})
        self.assertEqual(self.sent[-1]['to'], ['a@example.com', 'murphy@example.com'])


if __name__ == '__main__':
    unittest.main()
