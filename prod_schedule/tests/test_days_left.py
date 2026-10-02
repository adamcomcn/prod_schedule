"""Task list shows the number of days until / past the completion date."""
import os
import re
import sys
import tempfile
import unittest
from datetime import date, timedelta

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn


class DaysLeftTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        today = app.china_today()
        self.cases = {
            'LATE45': ((today - timedelta(days=45)).isoformat(), 'Pending'),
            'LATE1': ((today - timedelta(days=1)).isoformat(), 'Pending'),
            'TODAY': (today.isoformat(), 'Pending'),
            'SOON5': ((today + timedelta(days=5)).isoformat(), 'Pending'),
            'LATER30': ((today + timedelta(days=30)).isoformat(), 'Pending'),
            'NODATE': ('TBC', 'Pending'),
            'DONE': ((today - timedelta(days=90)).isoformat(), 'Completed'),
        }
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute('DELETE FROM inspection_tasks')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('boss','x','admin')")
            self.uid = conn.execute("SELECT id FROM users WHERE username='boss'").fetchone()[0]
            for code, (est, status) in self.cases.items():
                conn.execute('INSERT INTO inspection_tasks (job_key, order_number, region, item_code, '
                             'est_completion, status) VALUES (?,?,?,?,?,?)',
                             (f'MELBOURNE|PO-{code}|{code}', code, 'MELBOURNE', code, est, status))
        app.save_json(app.CURRENT_FILE, {})
        app.save_json(app.PREVIOUS_FILE, {})
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.uid

    def badge(self, page, code):
        row = re.search(r'<tr[^>]*data-item="%s".*?</tr>' % code, page, re.S).group(0)
        return (re.search(r'class="days-badge[^"]*"[^>]*>([^<]*)<', row).group(1).strip(),
                re.search(r'data-days="([^"]*)"', row).group(1))

    def test_labels_show_days(self):
        self.client.get('/lang/zh')
        page = self.client.get('/tasks?scope=all').get_data(as_text=True)
        self.assertEqual(self.badge(page, 'LATE45'), ('逾期 45 天', '-45'))
        self.assertEqual(self.badge(page, 'TODAY'), ('今天到期', '0'))
        self.assertEqual(self.badge(page, 'SOON5'), ('还剩 5 天', '5'))
        self.assertEqual(self.badge(page, 'LATER30'), ('还剩 30 天', '30'))
        self.assertEqual(self.badge(page, 'NODATE'), ('无截止日', ''))
        self.assertEqual(self.badge(page, 'DONE'), ('—', ''))  # completed: sorts last

    def test_english_labels(self):
        self.client.get('/lang/en')
        page = self.client.get('/tasks?scope=all').get_data(as_text=True)
        self.assertEqual(self.badge(page, 'LATE45')[0], '45 days overdue')
        self.assertEqual(self.badge(page, 'LATE1')[0], '1 day overdue')
        self.assertEqual(self.badge(page, 'TODAY')[0], 'Due today')
        self.assertEqual(self.badge(page, 'SOON5')[0], '5 days left')


class SupplierColumnTests(DaysLeftTests):
    def test_supplier_column_and_filter(self):
        with db_conn() as conn:
            conn.execute("UPDATE inspection_tasks SET supplier='Rainbow' WHERE item_code IN ('LATE45', 'SOON5')")
            conn.execute("UPDATE inspection_tasks SET supplier='  Hebei Foundry ' WHERE item_code='TODAY'")
            conn.execute("UPDATE inspection_tasks SET supplier=NULL WHERE item_code='NODATE'")
        self.client.get('/lang/zh')
        page = self.client.get('/tasks?scope=all').get_data(as_text=True)
        self.assertIn('data-key="supplier"', page)
        options = re.findall(r'<option value="([^"]*)">', page.split('id="supplierFilter"')[1].split('</select>')[0])
        self.assertEqual(options, ['*', 'Hebei Foundry', 'Rainbow', ''])  # trimmed, sorted, no "None"
        self.assertIn('data-supplier="Rainbow"', page)
        self.assertIn('data-supplier="Hebei Foundry"', page)
        row = re.search(r'<tr[^>]*data-item="NODATE".*?</tr>', page, re.S).group(0)
        self.assertIn('data-supplier=""', row)


if __name__ == '__main__':
    unittest.main()
