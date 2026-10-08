"""V-Trust booking reminder: valves due within 14 days are e-mailed to admins once."""
import io
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
import app
from db import db_conn
from werkzeug.security import generate_password_hash

TODAY = date(2026, 10, 8)
H = ['Order Number', 'Daemco Purchase Order', 'Supplier', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date']


def schedule():
    return {'MELBOURNE': [H,
        ['DPL2633', 'PO-4469', 'JS', 'RSV020016FLFL', 'DN200 Resilient Seated Gate Valve ACC Flange Flange', '60', '2026/10/14 ready to ship'],
        ['DPL2633', 'PO-4469', 'JS', 'RSV030016FLFL', 'DN300 Resilient Seated Gate Valve ACC Flange Flange', '30', '2026/10/10 ready to ship'],
        ['DPL2633', 'PO-4469', 'JS', 'RSVSO100ACC', 'DN100 Socket Gate Valve PN16 ACC', '90', '2026/10/30 ready to ship'],   # 22 days: not yet
        ['DPL2631', 'PO-4466', 'XM', 'RSVSO150ACC', 'DN150 Socket Gate Valve PN16 ACC', '48', '2026/10/20'],
        ['DPL2631', 'PO-4466', 'XM', 'RSVSO150ACC', 'DN150 Socket Gate Valve PN16 ACC', '12', '2026/10/18'],                # split lot
        ['DPL2620', 'PO-4400', 'XM', 'BFV100', 'DN100 Butterfly Valve', '5', '2026-10-03'],                                  # 5 days past, "valve"
        ['DPL2601', 'PO-4100', 'XM', 'RSV0200', 'DN200 Gate Valve', '5', '2026/6/15 ready for ship'],                         # months past: handled
        ['DPL2615', 'PO-4317', 'JC', 'ACCIHB', 'QLD Valve Trafficable Box COVER', '576', '2026/10/12'],                      # a cover: no V-Trust
        ['DPL2620', 'PO-4400', 'XM', 'RSV0100FLFLCC', 'DN100 Resilient Seated Gate Valve', '8', '2026/10/12'],               # V-Trust passed
        ['DPL2627', 'PO-4420', 'JC', 'ES0300', 'Extension Spindle 300', '400', '2026/10/12'],                                 # not a valve
        ['DPL2627', 'PO-4420', 'JC', 'RSV0150', 'DN150 Gate Valve', '10', 'TBC'],                                             # no date
    ]}


class VtrustReminderTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'vtrust_reminders', 'est_overrides'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute("INSERT INTO users (username,password_hash,role,email) VALUES ('boss',?,'admin','boss@example.com')",
                         (generate_password_hash('x' * 12),))
            conn.execute("INSERT INTO users (username,password_hash,role,email) VALUES ('murphy',?,'lead','m@example.com')",
                         (generate_password_hash('x' * 12),))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        app.save_json(app.CURRENT_FILE, schedule())
        app.save_json(app.INSPECTIONS_CACHE, {'MELBOURNE|PO-4400|RSV0100FLFLCC': [
            {'result': 'Pass', 'evidence': {'vtrust': {'result': 'Pass', 'files': ['v.mp4']}}}]})
        config = app.load_config()
        config.pop('vtrust_notify_emails', None)
        config.pop('vtrust_lead_days', None)
        app.save_json(app.CONFIG_FILE, config)
        patcher = mock.patch.object(app, 'china_today', return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = []

    def fake_send(self, ok=True):
        def send(subject, body, recipients, attachments=(), html=None):
            self.sent.append({'subject': subject, 'body': body, 'to': recipients, 'html': html, 'files': attachments})
            return ok, 'sent' if ok else 'failed'
        return mock.patch.object(app, '_smtp_send', side_effect=send)

    def send(self, **kw):
        with app.app.test_request_context(base_url='https://qc.example.com'):
            return app.send_vtrust_reminders(**kw)

    def test_which_lines_are_due(self):
        lines = app.vtrust_due_lines()
        self.assertEqual([(l['supplier'], l['code']) for l in lines],
                         [('JS', 'RSV030016FLFL'), ('JS', 'RSV020016FLFL'), ('XM', 'BFV100'), ('XM', 'RSVSO150ACC')])
        split = lines[-1]
        self.assertEqual((split['qty'], split['est'], split['days']), ('60', date(2026, 10, 18), 10))
        self.assertEqual(lines[2]['days'], -5)
        self.assertEqual(len(app.vtrust_due_lines(lead_days=30)), 5)          # + RSVSO100ACC (22 days)

    def test_sent_once_then_again_when_the_date_changes(self):
        with self.fake_send():
            self.assertEqual(self.send()[0], 4)
            self.assertEqual(self.send()[0], 0)                                # nothing new
            moved = schedule()
            moved['MELBOURNE'][1][6] = '2026/10/16 ready to ship'               # RSV020016FLFL moved
            app.save_json(app.CURRENT_FILE, moved)
            self.assertEqual(self.send()[0], 1)
        mail = self.sent[0]
        self.assertEqual(mail['to'], ['boss@example.com'])                    # admins only
        self.assertIn('4 个阀门订单行将在 14 天内完工', mail['subject'])
        self.assertIn('ESTIMATED COMPLETION TIME', mail['html'])
        self.assertIn('2026/10/14 ready to ship', mail['html'])
        self.assertIn('已过 5 天', mail['html'])
        self.assertIn('RSV030016FLFL', mail['body'])
        name, data, _ = mail['files'][0]
        self.assertEqual(name, 'V-Trust_2026-10-08.xlsx')
        rows = list(openpyxl.load_workbook(io.BytesIO(data)).active.values)
        self.assertEqual(rows[0][:6], ('DPL', 'Daemco purchase order number', 'PRODUCT CODE', 'DESCRIPTION',
                                       'QTY', 'ESTIMATED COMPLETION TIME'))
        self.assertEqual(rows[1][:5], ('DPL2633', 'PO-4469', 'RSV030016FLFL',
                                       'DN300 Resilient Seated Gate Valve ACC Flange Flange', '30'))
        self.assertEqual(len(self.sent), 2)                                   # the empty run sent nothing
        self.assertIn('RSV020016FLFL', self.sent[1]['body'])
        self.assertNotIn('RSV030016FLFL', self.sent[1]['body'])

    def test_failed_send_is_retried_and_settings_recipients_win(self):
        with self.fake_send(ok=False):
            self.assertEqual(self.send()[0], 0)
        config = app.load_config()
        config.update(vtrust_notify_emails='qa@example.com, vt@example.com', vtrust_lead_days=3)
        app.save_json(app.CONFIG_FILE, config)
        with self.fake_send():
            self.assertEqual(self.send()[0], 2)                                # BFV100 (past) + RSV030016 (2 days)
        self.assertEqual(self.sent[-1]['to'], ['qa@example.com', 'vt@example.com'])

    def test_no_recipient_keeps_lines_for_later(self):
        with db_conn() as conn:
            conn.execute("UPDATE users SET email=''")
        with self.fake_send():
            self.assertEqual(self.send()[0], 0)
            with db_conn() as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM vtrust_reminders').fetchone()[0], 0)

    def test_force_sends_full_list(self):
        with self.fake_send():
            self.send()
            self.assertEqual(self.send(force=True)[0], 4)

    def client(self, name):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[name]
            s['_csrf_token'] = 'tok'
        return c

    def test_preview_settings_and_permissions(self):
        admin = self.client('boss')
        page = admin.get('/settings/vtrust-preview').get_data(as_text=True)
        self.assertIn('Preview only', page)
        self.assertIn('boss@example.com', page)
        self.assertIn('RSV020016FLFL', page)
        xlsx = admin.get('/settings/vtrust-preview?format=xlsx')
        self.assertEqual(xlsx.status_code, 200)
        self.assertEqual(self.client('murphy').get('/settings/vtrust-preview').status_code, 403)
        self.assertEqual(self.client('murphy').post('/settings/vtrust-send', data={'_csrf_token': 'tok'}).status_code, 403)
        self.assertIn('V-Trust 预约提醒邮箱', admin.get('/settings').get_data(as_text=True))
        admin.post('/settings', data={'_csrf_token': 'tok', 'vtrust_notify_emails': 'a@example.com; bad',
                                      'vtrust_lead_days': '500'})
        config = app.load_config()
        self.assertEqual((config['vtrust_notify_emails'], config['vtrust_lead_days']), ('a@example.com', 14))
        with self.fake_send():
            admin.post('/settings/vtrust-send', data={'_csrf_token': 'tok'})
        self.assertEqual(self.sent[-1]['to'], ['a@example.com'])


if __name__ == '__main__':
    unittest.main()
