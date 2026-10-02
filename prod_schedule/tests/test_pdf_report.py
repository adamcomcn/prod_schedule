"""PDF inspection report."""
import io
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from helpers import assign_job
from PIL import Image
from werkzeug.security import generate_password_hash

HEADERS = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description',
           'Quantity', 'Estimated Completion Date', 'QA BRTs Sent?']
JOB = 'MELBOURNE|PO-9|RSV0100FL'


def jpeg():
    buf = io.BytesIO()
    Image.new('RGB', (800, 600), (40, 90, 150)).save(buf, 'JPEG')
    buf.seek(0)
    return buf


class PdfReportTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute('DELETE FROM inspection_attachments')
            conn.execute(
                'INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                ('pdf-inspector', generate_password_hash('pdf-inspector-pw'), 'inspector'))
            uid = conn.execute("SELECT id FROM users WHERE username='pdf-inspector'").fetchone()[0]
        assign_job(JOB, 'pdf-inspector')
        schedule = {'MELBOURNE': [HEADERS, ['DPL9', 'PO-9', 'RSV0100FL', '阀门 DN100 Valve', '12',
                                            '2026-10-20', 'NO']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {})
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = uid
            session['_csrf_token'] = 'tok'

    def submit(self, result='Pass'):
        return self.client.post(f'/inspect/{JOB}/submit', data={
            '_csrf_token': 'tok', 'region': 'MELBOURNE', 'order_number': 'DPL9', 'item_code': 'RSV0100FL',
            'inspector_name': '于工', 'inspection_date': '2026-10-09', 'quantity_inspected': '12',
            'quantity_passed': '12', 'result': result, 'defect_codes': ['A01'],
            'notes': '外观良好 <b>not bold</b>',
            'ev_result_brt': 'Pass', 'ev_file_brt': (jpeg(), '现场照片.jpg'),
            'ev_result_vtrust': 'Pass', 'ev_file_vtrust': (io.BytesIO(b'video'), 'test.mp4'),
        }, content_type='multipart/form-data')

    def test_pdf_is_generated_with_photo(self):
        self.submit()
        response = self.client.get(f'/inspect/{JOB}/report.pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/pdf')
        self.assertTrue(response.data.startswith(b'%PDF'))
        self.assertIn('inline', response.headers['Content-Disposition'])
        self.assertIn('QC-20261009-', response.headers['Content-Disposition'])
        self.assertIn(b'/Subtype /Image', response.data)  # the photo is embedded
        download = self.client.get(f'/inspect/{JOB}/report.pdf?download=1')
        self.assertIn('attachment', download.headers['Content-Disposition'])

    def test_each_inspection_has_its_own_report(self):
        self.submit('Fail')
        self.submit('Pass')
        first = self.client.get(f'/inspect/{JOB}/report.pdf?i=0')
        second = self.client.get(f'/inspect/{JOB}/report.pdf?i=1')
        self.assertEqual(first.status_code, 200)
        self.assertIn('filename=QC-20261009-DPL9-RSV0100FL-1.pdf', first.headers['Content-Disposition'])
        self.assertIn('filename=QC-20261009-DPL9-RSV0100FL-2.pdf', second.headers['Content-Disposition'])
        records = app.load_json(app.INSPECTIONS_CACHE)[JOB]
        self.assertEqual([r['report_no'] for r in records],
                         ['QC-20261009-DPL9-RSV0100FL-1', 'QC-20261009-DPL9-RSV0100FL-2'])

    def test_old_reports_keep_their_original_number(self):
        old = {'job_key': JOB, 'order_number': 'DPL9', 'item_code': 'RSV0100FL', 'result': 'Pass',
               'inspector_name': 'x', 'inspection_date': '2026-09-01', 'submitted_at': '2026-09-01T10:00:00'}
        app.save_json(app.INSPECTIONS_CACHE, {JOB: [old]})   # submitted before report_no existed
        number = app.report_number(JOB, old, 0)
        self.assertRegex(number, r'^QC-20260901-[0-9A-F]{6}-1$')
        response = self.client.get(f'/inspect/{JOB}/report.pdf')
        self.assertIn(f'filename={number}_DPL9_RSV0100FL.pdf', response.headers['Content-Disposition'])

    def test_readable_number_is_safe(self):
        rec = {'inspection_date': '2026-10-01', 'order_number': 'DPL 2627/A', 'item_code': 'es-0300 '}
        self.assertEqual(app.readable_report_number(rec, 2), 'QC-20261001-DPL2627A-ES0300-3')

    def test_missing_report_is_404(self):
        self.assertEqual(self.client.get(f'/inspect/{JOB}/report.pdf').status_code, 404)
        self.submit()
        self.assertEqual(self.client.get(f'/inspect/{JOB}/report.pdf?i=5').status_code, 404)
        self.assertEqual(self.client.get(f'/inspect/{JOB}/report.pdf?i=x').status_code, 404)

    def test_pdf_link_on_inspection_and_schedule_pages(self):
        self.submit()
        self.assertIn('report.pdf', self.client.get(f'/inspect/{JOB}').get_data(as_text=True))
        self.assertIn('report.pdf', self.client.get('/').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
