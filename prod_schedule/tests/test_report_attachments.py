"""Original evidence files go to HQ with the report; HEIC photos are embedded."""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import pdf_report
from db import db_conn
from helpers import assign_job

JOB = 'MEL|PO-1|ITEM'
LINKS = {'inspect': 'https://x.test/inspect/a', 'pdf': 'https://x.test/inspect/a/report.pdf'}


class OriginalAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with db_conn() as conn:
            conn.execute('DELETE FROM inspection_attachments')

    def add(self, name, data=b'x', insp=0):
        path = os.path.join(self.dir, name.replace('/', '_') + str(id(data)))
        with open(path, 'wb') as fh:
            fh.write(data)
        with db_conn() as conn:
            cur = conn.execute(
                'INSERT INTO inspection_attachments (job_key, insp_index, evidence_type, original_name, saved_name, file_path) '
                'VALUES (?,?,?,?,?,?)', (JOB, insp, 'daq', name, name, path))
            return cur.lastrowid

    def test_spreadsheets_and_pdfs_attached_photos_and_videos_not(self):
        self.add('daq.xlsx', b'xlsx-bytes'); self.add('data.csv', b'a,b'); self.add('brt.pdf', b'%PDF-1')
        self.add('photo.jpg', b'jpg'); self.add('phone.heic', b'heic'); vid = self.add('test.mp4', b'mp4')
        sent, skipped = app._original_attachments(JOB, 0, LINKS)
        self.assertEqual(sent['names'], ['daq.xlsx', 'data.csv', 'brt.pdf'])
        self.assertEqual(sent['data'][0][2], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertEqual(skipped, [('test.mp4', f'https://x.test/attachments/{vid}')])

    def test_size_budget_and_duplicate_names(self):
        self.add('a.xlsx', b'1' * 10); self.add('a.xlsx', b'2' * 10); big = self.add('big.xlsx', b'3' * 50)
        with mock.patch.object(app, 'MAX_EMAIL_TOTAL', 40):
            sent, skipped = app._original_attachments(JOB, 0, LINKS, used=15)
        self.assertEqual(sent['names'], ['a.xlsx', 'a (2).xlsx'])
        self.assertEqual([n for n, _ in skipped], ['big.xlsx'])
        self.assertTrue(skipped[0][1].endswith(f'/attachments/{big}'))

    def test_other_inspections_files_are_not_included(self):
        self.add('mine.xlsx', insp=0); self.add('other.xlsx', insp=1)
        sent, _ = app._original_attachments(JOB, 0, LINKS)
        self.assertEqual(sent['names'], ['mine.xlsx'])


class HeicPhotoTests(unittest.TestCase):
    @unittest.skipUnless('.heic' in pdf_report.PHOTO_EXTENSIONS, 'pillow-heif not installed')
    def test_heic_photo_is_embedded_in_the_pdf(self):
        from PIL import Image
        path = os.path.join(tempfile.mkdtemp(), 'phone.heic')
        Image.new('RGB', (60, 40), (200, 30, 30)).save(path, format='HEIF')
        job = {'region': 'MEL', 'Order Number': 'D1', 'Item Code': 'I'}
        rec = {'inspector_name': 'a', 'result': 'Pass', 'submitted_at': '2026-10-01T10:00:00'}
        att = [{'evidence_type': 'daq', 'original_name': 'phone.heic', 'file_path': path}]
        pdf = pdf_report.build_inspection_pdf(job, rec, 'QC-1', attachments=att,
                                              evidence_labels={'daq': ('DAQ', 'DAQ')})
        self.assertIn(b'/Subtype /Image', pdf)          # the photo is an embedded image, not just a file name

    def test_a_non_photo_file_is_not_embedded(self):
        path = os.path.join(tempfile.mkdtemp(), 'daq.xlsx')
        with open(path, 'wb') as fh:
            fh.write(b'not an image')
        job = {'region': 'MEL', 'Order Number': 'D1', 'Item Code': 'I'}
        rec = {'inspector_name': 'a', 'result': 'Pass', 'submitted_at': '2026-10-01T10:00:00'}
        att = [{'evidence_type': 'daq', 'original_name': 'daq.xlsx', 'file_path': path}]
        pdf = pdf_report.build_inspection_pdf(job, rec, 'QC-1', attachments=att,
                                              evidence_labels={'daq': ('DAQ', 'DAQ')})
        self.assertNotIn(b'/Subtype /Image', pdf)


if __name__ == '__main__':
    unittest.main()


class EndToEndMailTests(unittest.TestCase):
    """Submit → approve → the HQ mail carries the Excel, lists the video as a link."""
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def ehlo(self): pass
        def starttls(self): pass
        def login(self, user, pwd): pass
        def sendmail(self, sender, recipients, raw):
            EndToEndMailTests.sent.append((list(recipients), raw))

    def test_hq_mail_has_excel_attached_and_video_link(self):
        import email
        from werkzeug.security import generate_password_hash
        app.app.config.update(TESTING=True, SEND_EMAIL_SYNC=True)
        EndToEndMailTests.sent = []
        with db_conn() as conn:
            for t in ('users', 'report_emails', 'inspection_attachments', 'inspection_reviews'):
                conn.execute(f'DELETE FROM {t}')
            for u, role in (('insp', 'inspector'), ('lead', 'lead')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (u, generate_password_hash(u * 6), role))
            ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        job = 'MELBOURNE|PO-7|UMC100'
        assign_job(job, 'insp')
        H = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity']
        sched = {'MELBOURNE': [H, ['DPL7', 'PO-7', 'UMC100', 'Coupling', '40']]}
        app.save_json(app.CURRENT_FILE, sched); app.save_json(app.PREVIOUS_FILE, sched)
        app.save_json(app.INSPECTIONS_CACHE, {})
        cfg = app.load_config(); cfg.update(hq_report_emails='hq@example.test', hq_report_mode='all',
                                            task_notify_emails='', modules={})
        app.save_json(app.CONFIG_FILE, cfg)

        def client(u):
            c = app.app.test_client()
            with c.session_transaction() as s:
                s['user_id'] = ids[u]; s['_csrf_token'] = 'tok'
            return c

        env = {'SMTP_HOST': 'smtp.example.test', 'SMTP_PORT': '587', 'SMTP_USERNAME': 'n@example.test', 'SMTP_PASSWORD': 'x'}
        with mock.patch.dict(os.environ, env), mock.patch('smtplib.SMTP', self.FakeSMTP):
            r = client('insp').post(f'/inspect/{job}/submit', data={
                # an RSV needs BRT + DAQ + V-Trust, so the DAQ file applies
                '_csrf_token': 'tok', 'region': 'MELBOURNE', 'order_number': 'DPL7', 'item_code': 'RSV0100FLFLCC',
                'item_description': 'DN100 Resilient Seated Gate Valve', 'inspector_name': 'Yu',
                'inspection_date': '2026-10-09',
                'quantity_inspected': '10', 'quantity_passed': '10', 'result': 'Pass',
                'ev_result_daq': 'Pass', 'ev_file_daq': [(io.BytesIO(b'PK-excel'), 'daq.xlsx'),
                                                         (io.BytesIO(b'video'), 'test.mp4')],
                # not required for an RSV: must not be attached
                'ev_result_xrf': 'Pass', 'ev_file_xrf': [(io.BytesIO(b'%PDF xrf'), 'xrf.pdf')],
            }, content_type='multipart/form-data')
            self.assertEqual(r.status_code, 302)
            client('lead').post(f'/inspect/{job}/report/0/review', data={'_csrf_token': 'tok', 'action': 'approve'})
        raw = EndToEndMailTests.sent[-1][1]
        msg = email.message_from_string(raw)
        names = [p.get_filename() for p in msg.walk() if p.get_content_disposition() == 'attachment']
        self.assertEqual(len(names), 2)  # the PDF report + daq.xlsx
        self.assertIn('daq.xlsx', names)
        self.assertNotIn('xrf.pdf', names)
        body = next(p for p in msg.walk() if p.get_content_type() == 'text/plain').get_payload(decode=True).decode()
        self.assertIn('daq.xlsx', body)
        self.assertIn('test.mp4', body)
        self.assertIn('/attachments/', body)
