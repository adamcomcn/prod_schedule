"""V-Trust job numbers: booking valve lines, reminders, reschedule alerts."""
import os
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

TODAY = date(2026, 10, 9)
H = ['Order Number', 'Daemco Purchase Order', 'Supplier', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date']
A = 'MELBOURNE|PO-1|RSV020016FLFL'
B = 'MELBOURNE|PO-1|RSV030016FLFL'


def schedule(a_est='2026/10/14 ready to ship', b_est='2026/10/16'):
    return {'MELBOURNE': [H,
        ['DPL1', 'PO-1', 'JS', 'RSV020016FLFL', 'DN200 Gate Valve', '60', a_est],
        ['DPL1', 'PO-1', 'JS', 'RSV030016FLFL', 'DN300 Gate Valve', '30', b_est],
        ['DPL2', 'PO-2', 'JS', 'RSVSO100ACC', 'DN100 Socket Gate Valve', '90', '2026/11/30'],   # later
        ['DPL3', 'PO-3', 'XM', 'RSV0080', 'DN80 Gate Valve', '5', '2026/7/1'],                  # overdue
        ['DPL4', 'PO-4', 'XM', 'RSV0100', 'DN100 Gate Valve', '8', 'TBC'],                       # no date
        ['DPL5', 'PO-5', 'XM', 'RSV0150', 'DN150 Gate Valve', '8', '2026/10/12'],               # done
        ['DPL6', 'PO-6', 'XM', 'ES0300', 'Extension Spindle', '8', '2026/10/12']]}              # not a valve


class VtrustBookingTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'vtrust_reminders', 'vtrust_bookings', 'est_overrides', 'inspection_tasks'):
                conn.execute(f'DELETE FROM {table}')
            for name, role, email in (('boss', 'admin', 'boss@example.com'), ('murphy', 'lead', 'm@example.com')):
                conn.execute('INSERT INTO users (username,password_hash,role,email) VALUES (?,?,?,?)',
                             (name, generate_password_hash('x' * 12), role, email))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        app.save_json(app.CURRENT_FILE, schedule())
        app.save_json(app.INSPECTIONS_CACHE, {'MELBOURNE|PO-5|RSV0150': [
            {'result': 'Pass', 'evidence': {'vtrust': {'result': 'Pass', 'files': ['v.mp4']}}}]})
        config = app.load_config()
        config.pop('vtrust_notify_emails', None)
        config.pop('vtrust_lead_days', None)
        app.save_json(app.CONFIG_FILE, config)
        patcher = mock.patch.object(app, 'china_today', return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sent = []

    def capture(self, ok=True):
        def send(subject, body, recipients, attachments=(), html=None):
            self.sent.append({'subject': subject, 'body': body, 'to': recipients, 'html': html})
            return ok, 'sent' if ok else 'failed'
        return mock.patch.object(app, '_smtp_send', side_effect=send)

    def client(self, name='boss'):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[name]
            s['_csrf_token'] = 'tok'
        return c

    def book(self, keys, job='VT-2026-0815', planned='2026-10-15', client=None):
        return (client or self.client()).post('/vtrust/book', data={
            '_csrf_token': 'tok', 'job_key': keys, 'job_number': job, 'planned_date': planned, 'note': 'Ms Li'})

    def statuses(self):
        return {l['code']: l['status'] for l in app.vtrust_lines()}

    def test_statuses(self):
        self.assertEqual(self.statuses(), {'RSV020016FLFL': 'to_book', 'RSV030016FLFL': 'to_book',
                                           'RSVSO100ACC': 'later', 'RSV0080': 'overdue', 'RSV0100': 'no_date',
                                           'RSV0150': 'done'})
        self.book([A, B])
        self.assertEqual(self.statuses()['RSV020016FLFL'], 'booked')
        self.assertEqual([l['code'] for l in app.vtrust_due_lines()], [])          # booked: not "to book"

    def test_book_and_unbook_several_lines(self):
        r = self.book([A, B])
        self.assertEqual(r.status_code, 302)
        bookings = app.vtrust_bookings()
        self.assertEqual({k: (b['job_number'], b['planned_date'], b['note'], b['booked_by']) for k, b in bookings.items()},
                         {A: ('VT-2026-0815', '2026-10-15', 'Ms Li', 'boss'), B: ('VT-2026-0815', '2026-10-15', 'Ms Li', 'boss')})
        self.assertEqual(bookings[A]['est_at_booking'], '2026/10/14 ready to ship')
        self.book([A], job='', planned='')                                             # job number required
        self.book([A], planned='15/13/2026')                                           # bad date
        self.assertEqual(app.vtrust_bookings()[A]['job_number'], 'VT-2026-0815')
        self.book([A], job='VT-2', planned='')                                         # change, no date
        self.assertEqual((app.vtrust_bookings()[A]['job_number'], app.vtrust_bookings()[A]['planned_date']), ('VT-2', ''))
        self.client().post('/vtrust/unbook', data={'_csrf_token': 'tok', 'job_key': [B]})
        self.assertEqual(list(app.vtrust_bookings()), [A])

    def test_only_admin(self):
        lead = self.client('murphy')
        self.assertEqual(lead.get('/vtrust').status_code, 403)
        self.assertEqual(self.book([A], client=lead).status_code, 403)
        page = self.client().get('/vtrust').get_data(as_text=True)
        self.assertIn('RSV020016FLFL', page)
        self.assertNotIn('ES0300', page)
        self.assertIn('href="/vtrust"', self.client().get('/settings').get_data(as_text=True))

    def test_reminder_lists_booked_lines_for_reference(self):
        self.book([B])
        with self.capture(), app.app.test_request_context(base_url='https://qc.example.com'):
            self.assertEqual(app.send_vtrust_reminders()[0], 1)
        mail = self.sent[0]
        self.assertIn('RSV020016FLFL', mail['body'].split('已预约')[0])               # to book
        self.assertIn('Already booked (1)', mail['html'])
        self.assertIn('VT-2026-0815', mail['html'])
        self.assertIn('https://qc.example.com/vtrust', mail['html'])

    def test_reschedule_alert_once_per_new_date(self):
        self.book([A])                                                                 # planned 2026-10-15
        with self.capture(), app.app.test_request_context(base_url='https://qc.example.com'):
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 0)               # 10/14 ready: fine
            app.save_json(app.CURRENT_FILE, schedule(a_est='2026/10/20 ready to ship'))
            self.assertEqual(self.statuses()['RSV020016FLFL'], 'reschedule')
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 1)
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 0)               # same date: once
            app.save_json(app.CURRENT_FILE, schedule(a_est='2026/10/25'))
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 1)               # moved again
        mail = self.sent[0]
        self.assertEqual(mail['to'], ['boss@example.com'])
        self.assertIn('V-Trust 需改期', mail['subject'])
        self.assertIn('Job VT-2026-0815', mail['body'])
        self.assertIn('预约日 Booked for: 2026-10-15', mail['body'])
        self.assertIn('晚 5 天 / 5 d later', mail['body'])

    def test_reschedule_without_planned_date_uses_est_when_booked(self):
        self.book([A], planned='')
        app.save_json(app.CURRENT_FILE, schedule(a_est='2026/10/17'))
        line = next(l for l in app.vtrust_lines() if l['job_key'] == A)
        self.assertEqual((line['status'], line['late_by']), ('reschedule', 3))

    def test_failed_alert_is_retried(self):
        self.book([A])
        app.save_json(app.CURRENT_FILE, schedule(a_est='2026/10/20'))
        with self.capture(ok=False), app.app.test_request_context():
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 0)
        with self.capture(), app.app.test_request_context():
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 1)

    def test_date_change_e_mail_mentions_the_booking(self):
        self.book([A])
        change = dict(job_key=A, region='MELBOURNE', order_number='DPL1', item_code='RSV020016FLFL', description='',
                      assigned_to=None, old_est='2026/10/14', new_est='2026/10/20', old_ship='', new_ship='')
        with self.capture(), app.app.test_request_context(), \
                mock.patch.object(app, '_task_recipients', return_value=['m@example.com']):
            app._send_date_change_email([change])
        self.assertIn('⚠ V-Trust 已预约 Job VT-2026-0815（检验日 2026-10-15），新完成日晚于预约，需要改期', self.sent[0]['body'])

    def test_submitted_report_keeps_the_job_number(self):
        from helpers import assign_job
        with db_conn() as conn:
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('yu','x','inspector')")
            yu = conn.execute("SELECT id FROM users WHERE username='yu'").fetchone()[0]
        assign_job(A, 'yu')
        self.book([A])
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = yu
            s['_csrf_token'] = 'tok'
        r = c.post(f'/inspect/{A}/submit', data={'_csrf_token': 'tok', 'region': 'MELBOURNE', 'item_code': 'RSV020016FLFL',
                                                 'item_description': 'DN200 Resilient Seated Gate Valve',
                                                 'inspection_date': '2026-10-15', 'result': 'Pass'},
                   content_type='multipart/form-data')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(app.load_json(app.INSPECTIONS_CACHE)[A][-1]['vtrust_job'], 'VT-2026-0815')

    def test_inspection_page_report_and_pdf_show_the_job(self):
        self.book([A])
        with mock.patch.object(app, 'find_job', return_value={'job_key': A, 'Item Code': 'RSV020016FLFL',
                                                             'Item Description': 'DN200 Gate Valve'}):
            page = self.client().get(f'/inspect/{A}').get_data(as_text=True)
        self.assertIn('VT-2026-0815', page)
        self.assertIn('2026-10-15', page)
        app.save_json(app.INSPECTIONS_CACHE, {A: [{'result': 'Pass', 'vtrust_job': 'VT-OLD'}]})
        with mock.patch('pdf_report.build_inspection_pdf', return_value=b'%PDF') as build:
            app.build_report_pdf(A, 0)
        self.assertEqual(build.call_args[0][0]['V-Trust Job'], 'VT-OLD')            # the report's own job number


if __name__ == '__main__':
    unittest.main()
