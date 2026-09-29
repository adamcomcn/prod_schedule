"""Tests for the two-step (preview → confirm) weekly schedule upload and for
parsing the real supplier workbook layout."""
import io
import os
import sys
import tempfile
import unittest
from datetime import datetime

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from openpyxl import Workbook
from werkzeug.security import generate_password_hash


def workbook_bytes(sheets):
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


MEL_HEADERS = ['Order Number', 'Daemco Purchase Order', 'Order Date', 'Item Code',
               'Item Description', 'Quantity', 'Estimated Completion Date', None,
               'Actual Completion Date', 'Must Ship Date', 'Crate QTY', 'QA BRTs Sent?']
FIJI_HEADERS = ['Order Number', 'Daemco Purchase Order', 'Order Date', 'Item Code',
                'Item Description', 'Quantity', 'Current Status',
                'Estimated Completion /\nReady to Ship Date', 'Actual Completion Date',
                None, 'Crate QTY', 'QA BRTs Sent?']


class ParseRealLayoutTests(unittest.TestCase):
    def test_serial_dates_aliases_and_spacer_columns(self):
        content = workbook_bytes({
            'MELBOURNE': [MEL_HEADERS,
                          ['DPL2607', 'PO-4266', 46106, 'ACTBW', 'DI Toby Box W', 294,
                           46152, None, datetime(2026, 5, 6), 46290, 1, 'YES']],
            'FIJI': [FIJI_HEADERS,
                     ['DPL2620', 'PO-4364', 46170, 'DFC30PF', 'Connector', 36,
                      'Ready to Ship', 46266, None, None, 0, None]],
            'PRETAPS': [['Order Number', 'Daemco Purchase Order', 'Item Code',
                         'Quantity', 'Must ship time', 'Foundry', 'Unit weight（kg）'],
                        ['DPL2553', 'PO-4137', 'wPB25', 500, datetime(2026, 9, 5),
                         'Rainbow', 0.33]],
        })
        data = app.parse_excel(content, '')
        mel_h, mel_r = data['MELBOURNE']
        self.assertNotIn('', mel_h)  # empty spacer column dropped
        row = dict(zip(mel_h, mel_r))
        self.assertEqual(row['Order Date'], '2026-03-25')
        self.assertEqual(row['Estimated Completion Date'], '2026-05-10')
        self.assertEqual(row['Actual Completion Date'], '2026-05-06')
        self.assertEqual(row['Quantity'], '294')

        fiji = dict(zip(*data['FIJI']))
        self.assertEqual(fiji['Estimated Completion Date'], '2026-09-01')
        self.assertEqual(fiji['Crate QTY'], '0')  # zero is kept, not blanked

        pretaps = dict(zip(*data['PRETAPS']))
        self.assertEqual(pretaps['Must Ship Date'], '2026-09-05')
        self.assertIn('Unit weight (kg)', data['PRETAPS'][0])


class ReferenceSheetTests(unittest.TestCase):
    """TOOLING / LEADTIMES are never shown, even in schedules saved before
    the parser started skipping them."""

    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('ref-admin','x','admin')")
            self.uid = conn.execute("SELECT id FROM users WHERE username='ref-admin'").fetchone()[0]
        headers = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Quantity']
        old_week = {
            'MELBOURNE': [headers, ['DPL1', 'PO-1', 'ITEM1', '5']],
            'TOOLING': [['Tool', 'Cost'], ['Mould A', '100']],
            'LEADTIMES': [['Item', 'Weeks'], ['Valve', '8']],
        }
        app.save_json(app.CURRENT_FILE, old_week)
        app.save_json(app.PREVIOUS_FILE, old_week)

    def test_saved_reference_sheets_are_hidden(self):
        self.assertEqual(list(app.load_schedule(app.CURRENT_FILE)), ['MELBOURNE'])
        client = app.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.uid
        page = client.get('/').get_data(as_text=True)
        self.assertIn('sheet=MELBOURNE', page)
        self.assertNotIn('sheet=TOOLING', page)
        self.assertNotIn('sheet=LEADTIMES', page)
        self.assertNotIn('Mould A', page)

    def test_purge_removes_old_reference_sheet_records(self):
        with db_conn() as conn:
            conn.execute("INSERT INTO outstanding_jobs (job_key, sheet) VALUES ('TOOLING|X|Y', 'TOOLING')")
            conn.execute("INSERT INTO inspection_tasks (job_key, region, status) VALUES ('LEADTIMES|A|B', 'LEADTIMES', 'Pending')")
            conn.execute("INSERT INTO inspection_tasks (job_key, region, status) VALUES ('MELBOURNE|PO-1|ITEM1', 'MELBOURNE', 'Pending')")
            conn.execute("INSERT INTO weekly_snapshots (week_label, week_date, region, total_orders) VALUES ('w', '2026-06-11', 'TOOLING', 2)")
        app._purge_ignored_sheet_records()
        with db_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outstanding_jobs WHERE sheet='TOOLING'").fetchone()[0], 0)
            regions = [r[0] for r in conn.execute('SELECT region FROM inspection_tasks')]
            self.assertIn('MELBOURNE', regions)
            self.assertNotIn('LEADTIMES', regions)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM weekly_snapshots WHERE region='TOOLING'").fetchone()[0], 0)
            conn.execute('DELETE FROM inspection_tasks')
            conn.execute('DELETE FROM weekly_snapshots')


class UploadFlowTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks', 'outstanding_jobs', 'schedule_uploads'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute(
                'INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                ('admin-upload', generate_password_hash('admin-upload-password'), 'admin'))
            user_id = conn.execute(
                "SELECT id FROM users WHERE username='admin-upload'").fetchone()[0]
        for path in (app.CURRENT_FILE, app.PREVIOUS_FILE, app.PENDING_UPLOAD_FILE):
            if os.path.exists(path):
                os.remove(path)
        app.save_json(app.INSPECTIONS_CACHE, {})
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = user_id
            session['_csrf_token'] = 'tok'

        headers = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description',
                   'Quantity', 'Estimated Completion Date', 'QA BRTs Sent?']
        self.headers = headers
        app.save_json(app.CURRENT_FILE, {'MELBOURNE': [
            headers,
            ['DPL1', 'PO-1', 'KEEP', 'Stays', '10', '2026-10-01', 'NO'],
            ['DPL1', 'PO-1', 'GONE', 'Ships', '5', '2026-09-01', 'NO'],
            ['DPL2', 'PO-2', 'PART', 'Partial', '20', '2026-10-05', 'NO'],
        ]})
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO inspection_tasks (job_key, order_number, region, item_code, '
                'est_completion, quantity, status) VALUES (?,?,?,?,?,?,?)',
                ('MELBOURNE|PO-1|KEEP', 'DPL1', 'MELBOURNE', 'KEEP', '2026-10-01', '10',
                 'Pending'))
        self.new_week = workbook_bytes({'MELBOURNE': [
            headers,
            ['DPL1', 'PO-1', 'KEEP', 'Stays', 10, '2026-10-20', 'NO'],
            ['DPL2', 'PO-2', 'PART', 'Partial', 8, '2026-10-05', 'NO'],
            ['DPL3', 'PO-3', 'NEW', 'Brand new', 4, '2026-11-01', ''],
        ]})

    def upload(self):
        return self.client.post('/upload', data={
            '_csrf_token': 'tok', 'file': (io.BytesIO(self.new_week), 'Production Schedule.xlsx'),
        }, content_type='multipart/form-data')

    def test_upload_waits_for_confirmation_then_applies(self):
        before = app.load_json(app.CURRENT_FILE)
        response = self.upload()
        self.assertEqual(response.status_code, 302)
        self.assertIn('/upload/preview', response.location)
        self.assertEqual(app.load_json(app.CURRENT_FILE), before)  # nothing applied yet

        preview = self.client.get('/upload/preview')
        self.assertEqual(preview.status_code, 200)
        summary = app.schedule_diff_summary(before, app.load_json(app.PENDING_UPLOAD_FILE)['data'])
        self.assertEqual([r['item_code'] for r in summary['shipped']], ['GONE'])
        self.assertEqual([r['item_code'] for r in summary['partial']], ['PART'])
        self.assertEqual([r['item_code'] for r in summary['new']], ['NEW'])
        self.assertEqual([r['item_code'] for r in summary['date_changes']], ['KEEP'])

        response = self.client.post('/upload/confirm', data={'_csrf_token': 'tok'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(os.path.exists(app.PENDING_UPLOAD_FILE))
        self.assertEqual(app.load_json(app.PREVIOUS_FILE), before)
        with db_conn() as conn:
            tasks = {r['job_key']: r for r in conn.execute('SELECT * FROM inspection_tasks')}
            outstanding = [r['job_key'] for r in conn.execute('SELECT job_key FROM outstanding_jobs')]
        self.assertEqual(tasks['MELBOURNE|PO-1|KEEP']['est_completion'], '2026-10-20')
        self.assertIn('MELBOURNE|PO-3|NEW', tasks)
        self.assertEqual(outstanding, ['MELBOURNE|PO-1|GONE'])

        history = os.listdir(app.HISTORY_DIR)
        applied = [h for h in history if not h.startswith('pending-')]
        self.assertTrue(applied)
        self.assertTrue(os.path.exists(os.path.join(app.HISTORY_DIR, applied[-1], 'source.xlsx')))

    def test_split_lot_rows_do_not_overwrite_task_quantity(self):
        self.new_week = workbook_bytes({'MELBOURNE': [
            self.headers,
            ['DPL1', 'PO-1', 'KEEP', 'Stays', 10, '2026-10-01', 'NO'],
            ['DPL9', 'PO-9', 'LOT', 'Lot A', 6, '2026-11-01', ''],
            ['DPL9', 'PO-9', 'LOT', 'Lot B', 4, '2026-11-01', ''],
        ]})
        self.upload()
        self.client.post('/upload/confirm', data={'_csrf_token': 'tok'})
        with db_conn() as conn:
            row = conn.execute(
                "SELECT quantity FROM inspection_tasks WHERE job_key='MELBOURNE|PO-9|LOT'").fetchone()
            keep = conn.execute(
                "SELECT quantity FROM inspection_tasks WHERE job_key='MELBOURNE|PO-1|KEEP'").fetchone()
        self.assertEqual(row['quantity'], '6')
        self.assertEqual(keep['quantity'], '10')

    def test_baseline_confirm_skips_shipped_alerts(self):
        self.upload()
        self.client.post('/upload/confirm', data={'_csrf_token': 'tok', 'baseline': '1'})
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outstanding_jobs').fetchone()[0], 0)
        # The startup backfill (runs on every deploy/restart) must respect it too.
        app._backfill_fully_shipped_history()
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM outstanding_jobs').fetchone()[0], 0)

    def test_cancel_discards_pending_upload(self):
        before = app.load_json(app.CURRENT_FILE)
        self.upload()
        self.client.post('/upload/cancel', data={'_csrf_token': 'tok'})
        self.assertFalse(os.path.exists(app.PENDING_UPLOAD_FILE))
        self.assertEqual(app.load_json(app.CURRENT_FILE), before)

    def test_renamed_key_column_raises_warning(self):
        current = app.load_json(app.CURRENT_FILE)
        rows = [self.headers] + [['D', f'PO-{i}', f'IT{i}', 'x', '1', '', ''] for i in range(12)]
        renamed = [['Order Number', 'PO Number', 'Item Code', 'Item Description', 'Quantity',
                    'Estimated Completion Date', 'QA BRTs Sent?']] + rows[1:]
        summary = app.schedule_diff_summary({'MELBOURNE': rows}, {'MELBOURNE': renamed})
        self.assertTrue(any('列名' in w for w in summary['warnings']))
        self.assertTrue(current)

    def test_inspector_cannot_confirm_upload(self):
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                ('insp-upload', generate_password_hash('insp-upload-password'), 'inspector'))
            uid = conn.execute("SELECT id FROM users WHERE username='insp-upload'").fetchone()[0]
        with self.client.session_transaction() as session:
            session['user_id'] = uid
        self.assertEqual(self.client.post('/upload/confirm', data={'_csrf_token': 'tok'}).status_code, 403)
        self.assertEqual(self.client.get('/upload/preview').status_code, 403)


if __name__ == '__main__':
    unittest.main()
