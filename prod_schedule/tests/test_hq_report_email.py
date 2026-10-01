"""Inspection report PDF e-mailed to HQ on submit."""
import email
import io
import os
import sys
import tempfile
import unittest
from email.header import decode_header, make_header
from datetime import datetime, timedelta
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
            for table in ('users', 'report_emails', 'inspection_attachments', 'inspection_reviews'):
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
        config.update(hq_report_emails=emails, hq_report_mode=mode, modules={},
                      task_notify_emails='lead@example.test')
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

    def approve(self, index=0, client=None, comment=''):
        client = client or self.client_for('lead')
        return client.post(f'/inspect/{JOB}/report/{index}/review', data={
            '_csrf_token': 'tok', 'action': 'approve', 'comment': comment})

    def hq_mails(self):
        return [m for m in FakeSMTP.sent if 'lead@example.test' not in m[0]]

    def log_rows(self):
        with db_conn() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM report_emails ORDER BY id')]

    def test_submit_asks_for_review_and_hq_gets_pdf_after_approval(self):
        self.assertEqual(self.submit('Fail').status_code, 302)
        self.assertEqual(len(FakeSMTP.sent), 1)                 # only the review request
        self.assertEqual(FakeSMTP.sent[0][0], ['lead@example.test'])
        self.assertEqual(self.hq_mails(), [])
        self.assertEqual(self.client_for('insp').post(
            f'/inspect/{JOB}/report/0/review', data={'_csrf_token': 'tok', 'action': 'approve'}).status_code, 403)

        self.assertEqual(self.approve(comment='looks good').status_code, 302)
        recipients, raw = self.hq_mails()[0]
        self.assertEqual(recipients, ['hq@example.test', 'qa@example.test'])
        msg = email.message_from_string(raw)
        subject = str(make_header(decode_header(msg['Subject'])))
        self.assertIn('DPL7', subject)
        self.assertIn('Fail', subject)
        parts = [p for p in msg.walk() if p.get_content_disposition() == 'attachment']
        self.assertEqual(len(parts), 2)                       # the report + the uploaded brt.pdf
        self.assertTrue(parts[0].get_payload(decode=True).startswith(b'%PDF'))
        self.assertEqual(parts[1].get_filename(), 'brt.pdf')
        rows = self.log_rows()
        self.assertEqual((rows[0]['status'], rows[0]['insp_index']), ('sent', 0))
        page = self.client_for('insp').get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('已发送总部', page)
        self.assertIn('已审核通过', page)

    def test_pdf_shows_signature_and_review(self):
        self.submit('Pass')
        pdf, *_ = app.build_report_pdf(JOB, 0)
        with db_conn() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM inspection_reviews').fetchone())
        self.approve()                      # lead reviews a report submitted by 'insp'
        review = app._review_of(JOB, 0)
        self.assertEqual((review['status'], review['self_review']), ('approved', 0))
        pdf2, *_ = app.build_report_pdf(JOB, 0)
        self.assertTrue(pdf2.startswith(b'%PDF'))
        self.assertNotEqual(pdf, pdf2)

    def test_self_review_is_flagged(self):
        self.submit('Pass', client=self.client_for('lead'))
        self.approve()
        self.assertEqual(app._review_of(JOB, 0)['self_review'], 1)

    def test_return_requires_reason_and_blocks_hq_resend(self):
        self.submit('Pass')
        lead = self.client_for('lead')
        lead.post(f'/inspect/{JOB}/report/0/review', data={'_csrf_token': 'tok', 'action': 'reject'})
        self.assertIsNone(app._review_of(JOB, 0))
        lead.post(f'/inspect/{JOB}/report/0/review',
                  data={'_csrf_token': 'tok', 'action': 'reject', 'comment': '缺少 BRT'})
        self.assertEqual(app._review_of(JOB, 0)['status'], 'rejected')
        lead.post(f'/inspect/{JOB}/report/0/email', data={'_csrf_token': 'tok'})
        self.assertEqual(self.hq_mails(), [])

    def test_issues_only_mode_skips_pass(self):
        self.set_config('hq@example.test', 'issues')
        self.submit('Pass')
        self.approve(0)
        self.assertEqual(self.hq_mails(), [])
        self.submit('Partial Pass')
        self.approve(1)
        self.assertEqual(len(self.hq_mails()), 1)
        self.assertEqual(self.log_rows()[0]['insp_index'], 1)

    def test_off_or_no_recipients_sends_nothing(self):
        self.set_config('hq@example.test', 'off')
        self.submit('Fail')
        self.approve(0)
        self.set_config('', 'all')
        self.submit('Fail')
        self.approve(1)
        self.assertEqual(self.hq_mails(), [])
        self.assertEqual(self.log_rows(), [])

    def test_failure_is_recorded_and_lead_can_resend(self):
        self.assertEqual(self.submit('Pass').status_code, 302)
        FakeSMTP.fail = True
        self.approve(0)
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
        self.submit('Fail')
        app.app.config['SEND_EMAIL_SYNC'] = False
        with mock.patch('threading.Thread') as thread:
            self.approve(0)
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs['daemon'])
        self.assertEqual(self.log_rows()[0]['status'], 'pending')

    def test_overdue_review_reminded_once_fail_flagged(self):
        self.submit('Fail')
        FakeSMTP.sent = []
        with app.app.test_request_context():
            self.assertEqual(app.send_review_reminders(), 0)           # not 24 h yet
            cache = app.load_json(app.INSPECTIONS_CACHE)
            cache[JOB][0]['submitted_at'] = (datetime.now() - timedelta(hours=30)).isoformat()
            app.save_json(app.INSPECTIONS_CACHE, cache)
            self.assertEqual(app.send_review_reminders(), 1)
            self.assertEqual(app.send_review_reminders(), 0)           # only once
        recipients, raw = FakeSMTP.sent[0]
        self.assertEqual(recipients, ['lead@example.test'])
        subject = str(make_header(decode_header(email.message_from_string(raw)['Subject'])))
        self.assertIn('含 1 份不合格', subject)

    def test_reviewed_report_is_not_reminded_and_banner_shows(self):
        self.submit('Fail')
        page = self.client_for('lead').get('/tasks').get_data(as_text=True)
        self.assertIn('份检验报告待审核', page)
        self.approve(0)
        cache = app.load_json(app.INSPECTIONS_CACHE)
        cache[JOB][0]['submitted_at'] = (datetime.now() - timedelta(hours=40)).isoformat()
        app.save_json(app.INSPECTIONS_CACHE, cache)
        with app.app.test_request_context():
            self.assertEqual(app.send_review_reminders(), 0)
        self.assertNotIn('份检验报告待审核', self.client_for('lead').get('/tasks').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
