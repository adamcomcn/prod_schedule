"""Inspection report PDF e-mailed to HQ on submit."""
import email
import io
import os
import sys
import tempfile
import unittest
from email.header import decode_header, make_header
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

SMTP_ENV = {'SMTP_HOST': 'smtp.example.test', 'SMTP_PORT': '587',
            'SMTP_USERNAME': 'noreply@example.test', 'SMTP_PASSWORD': 'x'}
HEADERS = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity']
JOB = 'MELBOURNE|PO-7|UMC100'


class FakeSMTP:
    sent = []
    fail = False

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self): pass
    def starttls(self): pass

    def login(self, user, pwd):
        if FakeSMTP.fail:
            raise OSError('connection refused')

    def sendmail(self, sender, recipients, raw):
        FakeSMTP.sent.append((list(recipients), raw))


@mock.patch.dict(os.environ, SMTP_ENV)
@mock.patch('smtplib.SMTP', FakeSMTP)
class HqReportEmailTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True, SEND_EMAIL_SYNC=True)
        FakeSMTP.sent, FakeSMTP.fail = [], False
        with db_conn() as conn:
            for table in ('users', 'report_emails', 'inspection_attachments'):
                conn.execute(f'DELETE FROM {table}')
            for name, role in (('insp', 'inspector'), ('lead', 'lead')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (name, generate_password_hash(name * 6), role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        schedule = {'MELBOURNE': [HEADERS, ['DPL7', 'PO-7', 'UMC100', 'Coupling', '40']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {})
        self.set_config('hq@example.test, qa@example.test', 'all')

    def tearDown(self):
        self.set_config('', 'all')
        app.app.config['SEND_EMAIL_SYNC'] = False

    def set_config(self, emails, mode):
        config = app.load_config()
        config.update(hq_report_emails=emails, hq_report_mode=mode, modules={})
        app.save_json(app.CONFIG_FILE, config)

    def client_for(self, name):
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.ids[name]
            session['_csrf_token'] = 'tok'
        return client

    def submit(self, result='Pass', client=None):
        client = client or self.client_for('insp')
        return client.post(f'/inspect/{JOB}/submit', data={
            '_csrf_token': 'tok', 'region': 'MELBOURNE', 'order_number': 'DPL7', 'item_code': 'UMC100',
            'item_description': 'Coupling', 'inspector_name': 'Yu', 'inspection_date': '2026-10-09',
            'quantity_inspected': '10', 'quantity_passed': '10', 'result': result,
            'ev_result_brt': 'Pass', 'ev_file_brt': (io.BytesIO(b'%PDF-1.4 brt'), 'brt.pdf'),
        }, content_type='multipart/form-data')

    def log_rows(self):
        with db_conn() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM report_emails ORDER BY id')]

    def test_submit_emails_pdf_to_hq(self):
        self.assertEqual(self.submit('Fail').status_code, 302)
        self.assertEqual(len(FakeSMTP.sent), 1)
        recipients, raw = FakeSMTP.sent[0]
        self.assertEqual(recipients, ['hq@example.test', 'qa@example.test'])
        msg = email.message_from_string(raw)
        subject = str(make_header(decode_header(msg['Subject'])))
        self.assertIn('DPL7', subject)
        self.assertIn('Fail', subject)
        parts = [p for p in msg.walk() if p.get_content_disposition() == 'attachment']
        self.assertEqual(len(parts), 1)
        self.assertTrue(parts[0].get_filename().endswith('.pdf'))
        self.assertTrue(parts[0].get_payload(decode=True).startswith(b'%PDF'))
        body = next(p for p in msg.walk() if p.get_content_type() == 'text/plain')
        self.assertIn('/inspect/MELBOURNE%7CPO-7%7CUMC100', body.get_payload(decode=True).decode('utf-8'))
        rows = self.log_rows()
        self.assertEqual((rows[0]['status'], rows[0]['insp_index']), ('sent', 0))
        page = self.client_for('insp').get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('已发送总部', page)

    def test_issues_only_mode_skips_pass(self):
        self.set_config('hq@example.test', 'issues')
        self.submit('Pass')
        self.assertEqual(FakeSMTP.sent, [])
        self.submit('Partial Pass')
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(self.log_rows()[0]['insp_index'], 1)

    def test_off_or_no_recipients_sends_nothing(self):
        self.set_config('hq@example.test', 'off')
        self.submit('Fail')
        self.set_config('', 'all')
        self.submit('Fail')
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual(self.log_rows(), [])

    def test_failure_is_recorded_and_lead_can_resend(self):
        FakeSMTP.fail = True
        self.assertEqual(self.submit('Pass').status_code, 302)  # inspection still saved
        self.assertEqual(self.log_rows()[0]['status'], 'failed')
        self.assertEqual(len(app.load_json(app.INSPECTIONS_CACHE)[JOB]), 1)
        page = self.client_for('lead').get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('发送总部失败', page)

        FakeSMTP.fail = False
        self.assertEqual(self.client_for('insp').post(
            f'/inspect/{JOB}/report/0/email', data={'_csrf_token': 'tok'}).status_code, 403)
        response = self.client_for('lead').post(f'/inspect/{JOB}/report/0/email', data={'_csrf_token': 'tok'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual([r['status'] for r in self.log_rows()], ['failed', 'sent'])
        self.assertEqual(self.client_for('lead').post(
            f'/inspect/{JOB}/report/9/email', data={'_csrf_token': 'tok'}).status_code, 404)

    def test_background_thread_mode(self):
        app.app.config['SEND_EMAIL_SYNC'] = False
        with mock.patch('threading.Thread') as thread:
            self.submit('Fail')
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs['daemon'])
        self.assertEqual(self.log_rows()[0]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
