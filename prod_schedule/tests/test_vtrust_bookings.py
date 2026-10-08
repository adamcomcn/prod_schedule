"""V-Trust job numbers: booking valve lines with an inspection window,
reminders, "too early" reschedule alerts."""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import db
from db import db_conn
from werkzeug.security import generate_password_hash

TODAY = date(2026, 10, 9)
H = ['Order Number', 'Daemco Purchase Order', 'Supplier', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date']
A = 'MELBOURNE|PO-1|RSV020016FLFL'
B = 'MELBOURNE|PO-1|RSV030016FLFL'


def schedule(a_est='2026/10/19 ready to ship', b_est='2026/10/16'):
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

    def book(self, keys, job='JXW979734', start='2026-10-10', end='2026-10-11', client=None):
        return (client or self.client()).post('/vtrust/book', data={
            '_csrf_token': 'tok', 'job_key': keys, 'job_number': job, 'window_start': start, 'window_end': end,
            'note': 'Ms Li'})

    def statuses(self):
        return {l['code']: l['status'] for l in app.vtrust_lines()}

    def line(self, key):
        return next(l for l in app.vtrust_lines() if l['job_key'] == key)

    def test_statuses(self):
        self.assertEqual(self.statuses(), {'RSV020016FLFL': 'to_book', 'RSV030016FLFL': 'to_book',
                                           'RSVSO100ACC': 'later', 'RSV0080': 'overdue', 'RSV0100': 'no_date',
                                           'RSV0150': 'done'})
        self.book([A, B])                                   # inspection 10-10 ~ 10-11, ready 10-19 / 10-16
        self.assertEqual(self.statuses()['RSV020016FLFL'], 'booked')
        self.assertEqual(app.vtrust_due_lines(), [])
        self.book([A], start='2026-10-07', end='2026-10-07')  # inspection already happened
        self.assertEqual(self.statuses()['RSV020016FLFL'], 'inspected')

    def test_book_validation_and_unbook(self):
        self.assertEqual(self.book([A, B]).status_code, 302)
        bookings = app.vtrust_bookings()
        self.assertEqual({k: (b['job_number'], b['window_start'], b['window_end'], b['note'], b['booked_by'])
                          for k, b in bookings.items()},
                         {A: ('JXW979734', '2026-10-10', '2026-10-11', 'Ms Li', 'boss'),
                          B: ('JXW979734', '2026-10-10', '2026-10-11', 'Ms Li', 'boss')})
        self.assertEqual(bookings[A]['est_at_booking'], '2026/10/19 ready to ship')
        self.book([A], job='')                                                     # job number required
        self.book([A], job='X', start='', end='2026-10-11')                        # both dates required
        self.book([A], job='X', start='2026-10-10', end='')
        self.book([A], job='X', start='2026-10-12', end='2026-10-11')              # end before start
        self.assertEqual(app.vtrust_bookings()[A]['job_number'], 'JXW979734')
        self.assertEqual(app.booking_window_text(app.vtrust_bookings()[A]), '2026-10-10 ~ 2026-10-11')
        self.client().post('/vtrust/unbook', data={'_csrf_token': 'tok', 'job_key': [B]})
        self.assertEqual(list(app.vtrust_bookings()), [A])

    def test_only_admin(self):
        lead = self.client('murphy')
        self.assertEqual(lead.get('/vtrust').status_code, 403)
        self.assertEqual(self.book([A], client=lead).status_code, 403)
        self.book([A])
        page = self.client().get('/vtrust').get_data(as_text=True)
        self.assertIn('RSV020016FLFL', page)
        self.assertIn('2026-10-10 ~ 2026-10-11', page)
        self.assertIn('检验开始日', page)
        self.assertNotIn('ES0300', page)

    def test_reminder_lists_booked_lines_for_reference(self):
        self.book([B])
        with self.capture(), app.app.test_request_context(base_url='https://qc.example.com'):
            self.assertEqual(app.send_vtrust_reminders()[0], 1)
        mail = self.sent[0]
        self.assertIn('RSV020016FLFL', mail['body'].split('已预约')[0])
        self.assertIn('Already booked (1)', mail['html'])
        self.assertIn('JXW979734', mail['html'])
        self.assertIn('2026-10-10 ~ 2026-10-11', mail['html'])

    def test_too_early_only_when_completion_moves_far_past_a_coming_window(self):
        self.book([A])                                          # window ends 10-11, ready 10-19: 8 days
        self.assertEqual(self.line(A)['status'], 'booked')
        app.save_json(app.CURRENT_FILE, schedule(a_est='2026/10/25'))      # 14 days: still fine
        self.assertEqual(self.line(A)['status'], 'booked')
        app.save_json(app.CURRENT_FILE, schedule(a_est='2026/10/26'))      # 15 days: too early
        self.assertEqual((self.line(A)['status'], self.line(A)['late_by']), ('reschedule', 15))
        app.save_json(app.CURRENT_FILE, schedule(a_est='2026/10/01'))      # earlier: no alert
        self.assertEqual(self.line(A)['status'], 'booked')
        self.book([A], start='2026-10-05', end='2026-10-08')               # window over
        app.save_json(app.CURRENT_FILE, schedule(a_est='2026/12/01'))
        self.assertEqual(self.line(A)['status'], 'inspected')              # nothing to reschedule

    def test_reschedule_alert_once_per_new_date(self):
        self.book([A])
        with self.capture(), app.app.test_request_context(base_url='https://qc.example.com'):
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 0)
            app.save_json(app.CURRENT_FILE, schedule(a_est='2026/10/30 ready to ship'))
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 1)
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 0)               # same date: once
            app.save_json(app.CURRENT_FILE, schedule(a_est='2026/11/05'))
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 1)               # moved again
        mail = self.sent[0]
        self.assertEqual(mail['to'], ['boss@example.com'])
        self.assertIn('V-Trust 需改期', mail['subject'])
        self.assertIn('Job JXW979734', mail['body'])
        self.assertIn('检验时间 Inspection: 2026-10-10 ~ 2026-10-11', mail['body'])
        self.assertIn('比检验结束日晚 19 天', mail['body'])

    def test_failed_alert_is_retried(self):
        self.book([A])
        app.save_json(app.CURRENT_FILE, schedule(a_est='2026/10/30'))
        with self.capture(ok=False), app.app.test_request_context():
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 0)
        with self.capture(), app.app.test_request_context():
            self.assertEqual(app.send_vtrust_reschedule_alerts()[0], 1)

    def test_date_change_e_mail_mentions_the_booking(self):
        self.book([A])
        change = dict(job_key=A, region='MELBOURNE', order_number='DPL1', item_code='RSV020016FLFL', description='',
                      assigned_to=None, old_est='2026/10/19', new_est='2026/10/30', old_ship='', new_ship='')
        with self.capture(), app.app.test_request_context(), \
                mock.patch.object(app, '_task_recipients', return_value=['m@example.com']):
            app._send_date_change_email([change])
            app._send_date_change_email([dict(change, new_est='2026/10/21')])
        self.assertIn('⚠ V-Trust 已预约 Job JXW979734（检验时间 2026-10-10 ~ 2026-10-11），完成日推迟太多', self.sent[0]['body'])
        self.assertIn('V-Trust 已预约 Job JXW979734（检验时间 2026-10-10 ~ 2026-10-11）/', self.sent[1]['body'])
        self.assertNotIn('⚠ V-Trust', self.sent[1]['body'])

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
        self.assertEqual(app.load_json(app.INSPECTIONS_CACHE)[A][-1]['vtrust_job'], 'JXW979734')

    def test_inspection_page_report_and_pdf_show_the_job(self):
        self.book([A])
        with mock.patch.object(app, 'find_job', return_value={'job_key': A, 'Item Code': 'RSV020016FLFL',
                                                             'Item Description': 'DN200 Gate Valve'}):
            page = self.client().get(f'/inspect/{A}').get_data(as_text=True)
        self.assertIn('JXW979734', page)
        self.assertIn('2026-10-10 ~ 2026-10-11', page)
        app.save_json(app.INSPECTIONS_CACHE, {A: [{'result': 'Pass', 'vtrust_job': 'VT-OLD'}]})
        with mock.patch('pdf_report.build_inspection_pdf', return_value=b'%PDF') as build:
            app.build_report_pdf(A, 0)
        self.assertEqual(build.call_args[0][0]['V-Trust Job'], 'VT-OLD')


class OldBookingMigrationTests(unittest.TestCase):
    def test_single_date_becomes_the_inspection_window(self):
        path = os.path.join(tempfile.mkdtemp(), 'app.db')
        conn = sqlite3.connect(path)
        conn.execute('''CREATE TABLE vtrust_bookings (job_key TEXT PRIMARY KEY, job_number TEXT NOT NULL,
            planned_date TEXT DEFAULT '', est_at_booking TEXT DEFAULT '', note TEXT DEFAULT '',
            booked_by TEXT DEFAULT '', booked_at TEXT DEFAULT (datetime('now')), reschedule_alert TEXT DEFAULT '')''')
        conn.execute("INSERT INTO vtrust_bookings (job_key, job_number, planned_date, reschedule_alert) "
                     "VALUES ('K', 'JXW979734', '2026-10-07', '2026-10-19')")
        conn.commit()
        conn.close()
        with mock.patch.object(db, 'DB_PATH', path):
            db.init_db()
            db.init_db()                                                   # runs once only
        row = sqlite3.connect(path).execute(
            'SELECT window_start, window_end, reschedule_alert FROM vtrust_bookings').fetchone()
        self.assertEqual(row, ('2026-10-07', '2026-10-07', ''))


if __name__ == '__main__':
    unittest.main()
