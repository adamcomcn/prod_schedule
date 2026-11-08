"""Old (pre-launch) reports added afterwards from the original report, and
evidence files added to reports later (补录)."""
import io
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

JOB = 'MELBOURNE|DPL2607|RSV015016FLFL'
HEADERS = ['Order Number', 'Item Code', 'Item Description', 'Supplier', 'Quantity']
ROW = ['DPL2607', 'RSV015016FLFL', 'DN150 Resilient Seated Gate Valve', 'JS', '48']


def upload(name, content=b'%PDF-1.4 original'):
    return io.BytesIO(content), name


class BackfillTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True, SEND_EMAIL_SYNC=True)
        with db_conn() as conn:
            for table in ('users', 'outstanding_jobs', 'inspection_tasks', 'inspection_attachments',
                          'inspection_reviews', 'report_emails', 'kpi_exclusions'):
                conn.execute(f'DELETE FROM {table}')
            for name, role, flag in (('boss', 'admin', 0), ('intern', 'hq', 1), ('hq2', 'hq', 0),
                                     ('helper', 'hq', 1), ('murphy', 'inspector', 0)):
                conn.execute('INSERT INTO users (username,password_hash,role,can_backfill,display_name) '
                             'VALUES (?,?,?,?,?)', (name, generate_password_hash('x' * 12), role, flag,
                                                    'Murphy' if name == 'murphy' else ''))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
            conn.execute('INSERT INTO outstanding_jobs (job_key, sheet, headers_json, row_json, order_number, '
                         'item_code, item_desc, closed, close_reason) VALUES (?,?,?,?,?,?,?,1,?)',
                         (JOB, 'MELBOURNE', json.dumps(HEADERS), json.dumps(ROW), 'DPL2607', 'RSV015016FLFL',
                          ROW[2], 'pre_launch'))
        app.save_json(app.CURRENT_FILE, {})
        app.save_json(app.PREVIOUS_FILE, {})
        app.save_json(app.INSPECTIONS_CACHE, {})

    def client(self, user='intern'):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[user]
            s['_csrf_token'] = 'tok'
        return c

    def backfill(self, user='intern', **over):
        data = {'_csrf_token': 'tok', 'inspector_name': 'Murphy', 'inspection_date': '2026-05-20',
                'result': 'Pass', 'quantity_inspected': '8', 'quantity_passed': '8', 'notes': 'from the old Excel',
                'file_original': [upload('DPL2607 QC report.xlsx', b'old report')],
                'file_brt': [upload('BRT DPL2607.pdf', b'brt')]}
        data.update(over)
        return self.client(user).post(f'/inspect/{JOB}/backfill', data=data, content_type='multipart/form-data')

    def records(self):
        return app.load_json(app.INSPECTIONS_CACHE, {}).get(JOB, [])

    def attachments(self):
        with db_conn() as conn:
            return [dict(r) for r in conn.execute(
                'SELECT * FROM inspection_attachments WHERE job_key=? ORDER BY id', (JOB,))]

    def test_only_accounts_with_the_flag_can_add_old_reports(self):
        page = self.client('hq2').get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertNotIn('id="backfill"', page)
        self.assertEqual(self.backfill(user='hq2').status_code, 403)
        self.assertEqual(self.backfill(user='murphy').status_code, 403)
        self.assertEqual(self.records(), [])
        self.assertIn('id="backfill"', self.client('intern').get(f'/inspect/{JOB}').get_data(as_text=True))
        self.assertIn('id="backfill"', self.client('boss').get(f'/inspect/{JOB}').get_data(as_text=True))

    def test_old_report_is_saved_with_its_files_and_completes_the_job(self):
        self.assertEqual(self.backfill().status_code, 302)
        [rec] = self.records()
        self.assertTrue(rec['backfill'])
        self.assertEqual((rec['inspector_name'], rec['inspection_date'], rec['result'], rec['submitted_by']),
                         ('Murphy', '2026-05-20', 'Pass', 'intern'))
        self.assertEqual(rec['report_no'], 'QC-20260520-DPL2607-RSV015016FLFL-1')
        self.assertEqual(rec['evidence']['original']['files'], ['DPL2607 QC report.xlsx'])
        self.assertEqual(rec['evidence']['brt']['files'], ['BRT DPL2607.pdf'])
        self.assertNotIn('brt', rec['missing_evidence'])
        atts = self.attachments()
        self.assertEqual([(a['evidence_type'], a['original_name']) for a in atts],
                         [('original', 'DPL2607 QC report.xlsx'), ('brt', 'BRT DPL2607.pdf')])
        with open(atts[0]['file_path'], 'rb') as fh:
            self.assertEqual(fh.read(), b'old report')
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT completed FROM outstanding_jobs WHERE job_key=?',
                                          (JOB,)).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT status FROM inspection_tasks WHERE job_key=?',
                                          (JOB,)).fetchone()[0], 'Completed')
        page = self.client().get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('Old report added afterwards', page)
        self.assertNotIn('Pending review', page)
        [row] = [r for r in app.inspection_report_rows() if r['job_key'] == JOB]
        self.assertEqual((row['review'], row['reviewer']), ('backfill', 'intern'))
        self.assertIn('Old report added', self.client().get('/reports?review=backfill').get_data(as_text=True))
        pdf = self.client().get(f'/inspect/{JOB}/report.pdf')
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b'%PDF'))

    def test_only_shipped_or_closed_lines_take_old_reports(self):
        open_job = 'MELBOURNE|DPL2700|RSV015016FLFL'
        app.save_json(app.CURRENT_FILE, {'MELBOURNE': [HEADERS, ['DPL2700'] + ROW[1:]]})
        page = self.client().get(f'/inspect/{open_job}').get_data(as_text=True)
        self.assertNotIn('id="backfill"', page)
        self.client().post(f'/inspect/{open_job}/backfill', content_type='multipart/form-data', data={
            '_csrf_token': 'tok', 'inspector_name': 'Murphy', 'inspection_date': '2026-05-20', 'result': 'Pass',
            'file_original': [upload('report.xlsx')]})
        self.assertEqual(app.load_json(app.INSPECTIONS_CACHE, {}).get(open_job, []), [])
        # a shipped line still takes them
        self.assertIn('id="backfill"', self.client().get(f'/inspect/{JOB}').get_data(as_text=True))

    def test_a_failed_old_report_of_a_shipped_job_needs_no_follow_up(self):
        self.backfill(result='Fail')
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT status FROM inspection_tasks WHERE job_key=?',
                                          (JOB,)).fetchone()[0], 'Completed')

    def test_original_report_inspector_date_and_result_are_required(self):
        for over in ({'file_original': []}, {'inspector_name': ' '}, {'inspection_date': '2099-01-01'},
                     {'result': 'Maybe'}, {'file_brt': [upload('macro.exe')]}):
            self.backfill(**over)
            self.assertEqual(self.records(), [], over)
            self.assertEqual(self.attachments(), [], over)

    def test_old_reports_are_not_reviewed_e_mailed_or_rated(self):
        self.backfill()
        self.assertFalse([p for p in app.pending_reviews() if p['job_key'] == JOB])
        self.client('boss').post(f'/inspect/{JOB}/report/0/review',
                                 data={'_csrf_token': 'tok', 'action': 'approve'})
        self.client('boss').post(f'/inspect/{JOB}/report/0/email', data={'_csrf_token': 'tok'})
        with db_conn() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM inspection_reviews').fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM report_emails').fetchone())
            users = conn.execute('SELECT id, username, display_name, role FROM users').fetchall()
        stats = app.inspector_kpis(app.load_inspections(), {}, users, [])
        self.assertEqual(stats, [])
        [item] = app.inspection_status_rows()
        self.assertEqual((item['review_status'], item['passed']), ('backfill', True))

    def test_evidence_can_be_added_to_a_report_later_and_removed_again(self):
        app.save_json(app.INSPECTIONS_CACHE, {JOB: [{
            'order_number': 'DPL2607', 'item_code': 'RSV015016FLFL', 'item_description': ROW[2],
            'inspector_name': 'Murphy', 'inspection_date': '2026-10-01', 'result': 'Pass',
            'submitted_by': 'murphy', 'evidence': {}, 'missing_evidence': ['brt']}]})
        self.assertEqual(self.client('hq2').post(f'/inspect/{JOB}/report/0/evidence', data={
            '_csrf_token': 'tok', 'evidence_type': 'brt', 'files': [upload('BRT.pdf')]},
            content_type='multipart/form-data').status_code, 403)
        # the original-report type is only for old reports
        self.assertEqual(self.client().post(f'/inspect/{JOB}/report/0/evidence', data={
            '_csrf_token': 'tok', 'evidence_type': 'original', 'files': [upload('x.pdf')]},
            content_type='multipart/form-data').status_code, 400)
        self.client().post(f'/inspect/{JOB}/report/0/evidence', data={
            '_csrf_token': 'tok', 'evidence_type': 'brt', 'files': [upload('BRT 1.pdf'), upload('BRT 2.pdf')]},
            content_type='multipart/form-data')
        [rec] = self.records()
        self.assertEqual(rec['evidence']['brt']['files'], ['BRT 1.pdf', 'BRT 2.pdf'])
        self.assertNotIn('brt', rec['missing_evidence'])
        [entry] = rec['evidence_added']
        self.assertEqual((entry['type'], entry['files'], entry['by']), ('brt', ['BRT 1.pdf', 'BRT 2.pdf'], 'intern'))
        self.assertIn('BRT 1.pdf', self.client('boss').get(f'/inspect/{JOB}').get_data(as_text=True))
        first = self.attachments()[0]
        # someone else (not admin) cannot remove it; the one who added it can
        self.assertEqual(self.client('helper').post(
            f'/inspect/{JOB}/report/0/evidence/{first["id"]}/delete', data={'_csrf_token': 'tok'}).status_code, 403)
        self.client().post(f'/inspect/{JOB}/report/0/evidence/{first["id"]}/delete', data={'_csrf_token': 'tok'})
        second = self.attachments()[0]
        self.client('boss').post(f'/inspect/{JOB}/report/0/evidence/{second["id"]}/delete',
                                 data={'_csrf_token': 'tok'})
        [rec] = self.records()
        self.assertEqual(rec['evidence']['brt']['files'], [])
        self.assertIn('brt', rec['missing_evidence'])
        self.assertEqual(rec['evidence_added'], [])
        self.assertEqual(self.attachments(), [])
        self.assertFalse(os.path.exists(first['file_path']))

    def test_an_old_report_added_by_mistake_can_be_deleted(self):
        self.backfill()
        paths = [a['file_path'] for a in self.attachments()]
        self.assertEqual(self.client('helper').post(f'/inspect/{JOB}/report/0/backfill/delete',
                                                    data={'_csrf_token': 'tok'}).status_code, 403)
        self.client().post(f'/inspect/{JOB}/report/0/backfill/delete', data={'_csrf_token': 'tok'})
        self.assertEqual(self.records(), [])
        self.assertEqual(self.attachments(), [])
        self.assertFalse(any(os.path.exists(p) for p in paths))
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT completed FROM outstanding_jobs WHERE job_key=?',
                                          (JOB,)).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT status FROM inspection_tasks WHERE job_key=?',
                                          (JOB,)).fetchone()[0], 'Pending')

    def test_only_the_latest_report_can_be_deleted(self):
        self.backfill()
        self.backfill(user='boss', inspection_date='2026-06-01')
        self.assertEqual(len(self.records()), 2)
        self.client().post(f'/inspect/{JOB}/report/0/backfill/delete', data={'_csrf_token': 'tok'})
        self.assertEqual(len(self.records()), 2)

    def test_admin_grants_the_right_on_the_users_page(self):
        self.assertIn('name="can_backfill"', self.client('boss').get('/admin/users').get_data(as_text=True))
        self.client('boss').post(f'/admin/users/{self.ids["hq2"]}/update', data={
            '_csrf_token': 'tok', 'role': 'hq', 'can_backfill': '1', 'email': '', 'display_name': ''})
        self.client('boss').post(f'/admin/users/{self.ids["intern"]}/update', data={
            '_csrf_token': 'tok', 'role': 'hq', 'email': '', 'display_name': ''})
        with db_conn() as conn:
            flags = {r['username']: r['can_backfill'] for r in conn.execute('SELECT username, can_backfill FROM users')}
        self.assertEqual((flags['hq2'], flags['intern']), (1, 0))


if __name__ == '__main__':
    unittest.main()
