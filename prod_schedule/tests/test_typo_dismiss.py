"""Suspected typos (a new line that looks like one that left the schedule) are shown to
admins only, and an admin can dismiss them once checked (one by one or all at once)."""
import os
import re
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn
from werkzeug.security import generate_password_hash

H = ['Order Number', 'Daemco Purchase Order', 'Supplier', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date']
FJ = 'FIJI'
PREVIOUS = {FJ: [H,
                 ['D1', 'PO-4583', 'XM', 'DFC10PF', 'DN100 Connector', '10', '2026-12-01'],
                 ['D2', 'PO-4583', 'XM', 'DFC15PF', 'DN150 Connector', '10', '2026-12-01']]}
CURRENT = {FJ: [H,
                ['D3', 'PO-4584', 'XM', 'DFC10PF', 'DN100 Connector', '10', '2026-12-01'],
                ['D4', 'PO-4584', 'XM', 'DFC15PF', 'DN150 Connector', '10', '2026-12-01']]}
NEW10, NEW15 = f'{FJ}|PO-4584|DFC10PF', f'{FJ}|PO-4584|DFC15PF'
PAIR10 = f'{NEW10}>{FJ}|PO-4583|DFC10PF'
PAIR15 = f'{NEW15}>{FJ}|PO-4583|DFC15PF'


def esc(pair):
    return pair.replace('>', '&gt;')


class TypoDismissTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        app.save_json(app.PREVIOUS_FILE, PREVIOUS)
        app.save_json(app.CURRENT_FILE, CURRENT)
        app.save_json(app.INSPECTIONS_CACHE, {})
        app.update_config(typo_dismissed=[])
        with db_conn() as conn:
            for table in ('users', 'outstanding_jobs', 'inspection_tasks'):
                conn.execute(f'DELETE FROM {table}')
            for name, role in (('boss', 'admin'), ('qc1', 'inspector'), ('mel', 'hq')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (name, generate_password_hash('x' * 12), role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}

    def client(self, user='boss'):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[user]
            s['_csrf_token'] = 'tok'
        return c

    def page(self, user='boss'):
        return self.client(user).get(f'/?sheet={FJ}').get_data(as_text=True)

    def test_compare_skips_dismissed_pairs(self):
        info = {}
        statuses, flags, _ = app.compute_changes(PREVIOUS, CURRENT, moves_out=info, dismissed=set())
        self.assertEqual(sorted(f['pair'] for f in flags), [PAIR10, PAIR15])
        self.assertEqual((statuses[NEW10], info['typo_dismissed']), ('typo', []))
        statuses, flags, _ = app.compute_changes(PREVIOUS, CURRENT, moves_out=info, dismissed={PAIR10})
        self.assertEqual([f['pair'] for f in flags], [PAIR15])
        self.assertEqual((statuses[NEW10], statuses[NEW15]), ('new', 'typo'))
        self.assertEqual(info['typo_dismissed'], [PAIR10])

    def test_only_admins_see_the_warnings(self):
        admin = self.page()
        self.assertIn('typoPanel', admin)
        self.assertIn('class="badge badge-typo"', admin)
        for user in ('qc1', 'mel'):
            page = self.page(user)
            self.assertNotIn('typoPanel', page)
            self.assertNotIn('class="badge badge-typo"', page)
            self.assertRegex(page, r'badge-new">(新增|NEW)')
        self.assertEqual(self.client('qc1').post('/typos/dismiss', data={
            '_csrf_token': 'tok', 'pair': PAIR10}).status_code, 403)
        self.assertEqual(app.load_config().get('typo_dismissed'), [])

    def test_admin_dismisses_one_then_all_then_restores(self):
        c = self.client()
        res = c.post('/typos/dismiss', data={'_csrf_token': 'tok', 'sheet': FJ, 'pair': PAIR10})
        self.assertEqual(res.status_code, 302)
        self.assertIn('sheet=FIJI', res.headers['Location'])
        self.assertEqual(app.load_config()['typo_dismissed'], [PAIR10])
        page = self.page()
        self.assertEqual(re.findall(r'name="pair" value="([^"]+)"', page).count(esc(PAIR15)), 2)  # its own + "all"
        self.assertNotIn(f'value="{esc(PAIR10)}"', page)
        self.assertRegex(page, r'恢复已关闭的 1 条|Show 1 dismissed')

        c.post('/typos/dismiss', data={'_csrf_token': 'tok', 'sheet': FJ, 'pair': [PAIR15, PAIR10]})
        self.assertEqual(sorted(app.load_config()['typo_dismissed']), [PAIR10, PAIR15])
        page = self.page()
        self.assertNotIn('typoPanel', page)
        self.assertNotIn('class="badge badge-typo"', page)
        self.assertRegex(page, r'已关闭 2 条疑似笔误提示|2 typo warning\(s\) dismissed')

        c.post('/typos/dismiss', data={'_csrf_token': 'tok', 'sheet': FJ, 'restore': '1'})
        self.assertEqual(app.load_config()['typo_dismissed'], [])
        self.assertIn('typoPanel', self.page())


if __name__ == '__main__':
    unittest.main()
