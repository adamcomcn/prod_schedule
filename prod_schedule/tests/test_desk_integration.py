"""Production Desk integration: forwarded weekly schedules become the pending upload an admin
confirms, and inspection status is readable with the shared token only."""
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
from tests.test_schedule_upload import MEL_HEADERS, workbook_bytes

TOKEN = 'integration-test-token'


class DeskIntegrationTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        os.environ['DESK_INTEGRATION_TOKEN'] = TOKEN
        app._discard_pending_upload()
        app.save_json(app.CURRENT_FILE, {})
        app.save_json(app.PREVIOUS_FILE, {})
        app.save_json(app.INSPECTIONS_CACHE, {})
        with db_conn() as conn:
            conn.execute('DELETE FROM schedule_uploads')
            conn.execute('DELETE FROM inspection_reviews')
        self.client = app.app.test_client()

    def tearDown(self):
        os.environ.pop('DESK_INTEGRATION_TOKEN', None)
        app._discard_pending_upload()

    def forward(self, token=TOKEN, content=None):
        content = content or workbook_bytes({'MELBOURNE': [
            MEL_HEADERS, ['DPL1', 'PO-1', 46106, 'ITEM1', 'Valve', 10, None, None, None, None, 1, None]]})
        return self.client.post('/api/integration/schedule',
                                headers={'X-Integration-Token': token} if token else {},
                                data={'file': (io.BytesIO(content), 'Production Schedule 10.08.xlsx'),
                                      'uploaded_by': 'Wang'},
                                content_type='multipart/form-data')

    def test_forwarded_schedule_waits_for_admin_confirmation(self):
        response = self.forward()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json(), {'ok': True, 'pending': True})
        pending = app.load_json(app.PENDING_UPLOAD_FILE, {})
        self.assertEqual(pending['uploaded_by'], 'Wang (Production Desk)')
        self.assertIn('MELBOURNE', pending['data'])
        self.assertEqual(app.load_schedule(app.CURRENT_FILE), {}, 'nothing is applied before confirmation')

    def test_token_is_required(self):
        self.assertEqual(self.forward(token='wrong').status_code, 404)
        self.assertEqual(self.forward(token=None).status_code, 404)
        os.environ.pop('DESK_INTEGRATION_TOKEN')
        self.assertEqual(self.forward().status_code, 404, 'disabled when no token is configured')
        self.assertFalse(app.has_pending_upload())

    def test_unreadable_file_is_rejected(self):
        response = self.forward(content=b'not a workbook')
        self.assertIn(response.status_code, (400, 422))
        self.assertFalse(response.get_json()['ok'])
        self.assertFalse(app.has_pending_upload())

    def test_inspection_status_uses_latest_result_and_review(self):
        app.save_json(app.INSPECTIONS_CACHE, {
            'MELBOURNE|PO-1|ITEM1': [{'result': 'Fail'}, {'result': 'Pass', 'submitted_at': '2026-10-01T10:00:00'}],
            'QLD|PO-2|ITEM2': [{'result': 'Pass'}],
        })
        with db_conn() as conn:
            conn.execute("INSERT INTO inspection_reviews (job_key, insp_index, status) VALUES ('MELBOURNE|PO-1|ITEM1', 1, 'approved')")
        self.assertEqual(self.client.get('/api/integration/inspections').status_code, 404)
        response = self.client.get('/api/integration/inspections', headers={'X-Integration-Token': TOKEN})
        items = {row['job_key']: row for row in response.get_json()['items']}
        first = items['MELBOURNE|PO-1|ITEM1']
        self.assertEqual((first['purchase_order'], first['item_code'], first['result']), ('PO-1', 'ITEM1', 'Pass'))
        self.assertTrue(first['passed'])
        self.assertEqual(first['inspection_count'], 2)
        self.assertFalse(items['QLD|PO-2|ITEM2']['passed'], 'a Pass without an approved review is not passed')
        self.assertEqual(items['QLD|PO-2|ITEM2']['review_status'], 'pending')


if __name__ == '__main__':
    unittest.main()
