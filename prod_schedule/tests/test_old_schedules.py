"""Old weekly schedules uploaded only for comparisons: the current schedule is not touched."""
import io
import os
import shutil
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
import app
from db import db_conn
from werkzeug.security import generate_password_hash

H = ['Order Number', 'Daemco Purchase Order', 'Supplier', 'Item Code', 'Item Description', 'Quantity',
     'Estimated Completion Date']
BEFORE = {'MELBOURNE': [H, ['DPL1', 'PO-1', 'JS', 'RSV0100', 'DN100 Gate Valve', '10', '2026-10-01'],
                        ['DPL2', 'PO-2', 'JS', 'ES0300', 'Extension Spindle 300', '50', '2026-10-10']]}
AFTER = {'MELBOURNE': [H, ['DPL2', 'PO-2', 'JS', 'ES0300', 'Extension Spindle 300', '20', '2026-10-10']]}


def workbook_bytes(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'MELBOURNE'
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


OLD_ROWS = [H, ['DPL0', 'PO-0', 'JS', 'UMC0100', 'DN100 Coupling', 12, '2026-09-25'],
            ['DPL1', 'PO-1', 'JS', 'RSV0100', 'DN100 Gate Valve', 10, '2026-10-01'],
            ['DPL2', 'PO-2', 'JS', 'ES0300', 'Extension Spindle 300', 80, '2026-10-10']]


class OldScheduleTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        for d in (app.HISTORY_DIR, app.OLD_SCHEDULE_DIR):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(os.path.join(app.HISTORY_DIR, '20261001-080000-000000'))
        app.save_json(os.path.join(app.HISTORY_DIR, '20261001-080000-000000', 'meta.json'),
                      {'filename': 'Production Schedule 01.10.xlsx', 'applied_at': '2026-10-01 08:00'})
        second = os.path.join(app.HISTORY_DIR, '20261008-080000-000000')
        os.makedirs(second)
        app.save_json(os.path.join(second, 'replaced_schedule.json'), BEFORE)
        app.save_json(os.path.join(second, 'meta.json'),
                      {'filename': 'Production Schedule 08.10.xlsx', 'applied_at': '2026-10-08 08:00'})
        app.save_json(app.CURRENT_FILE, AFTER)
        app.save_json(app.PREVIOUS_FILE, BEFORE)
        with db_conn() as conn:
            conn.execute('DELETE FROM users')
            conn.execute('DELETE FROM schedule_uploads')
            for name, role in (('boss', 'admin'), ('hq1', 'hq'), ('lead1', 'lead')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)',
                             (name, generate_password_hash('x' * 12), role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}

    def client(self, name):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[name]
            s['_csrf_token'] = 'tok'
        return c

    def upload(self, client, day='2026-09-20', content=None, filename='Production Schedule 20.09.xlsx'):
        return client.post('/schedule/old', content_type='multipart/form-data', data={
            '_csrf_token': 'tok', 'schedule_date': day,
            'file': (io.BytesIO(content if content is not None else workbook_bytes(OLD_ROWS)), filename)})

    def test_upload_keeps_current_schedule_and_joins_the_comparison(self):
        boss = self.client('boss')
        with open(app.CURRENT_FILE, 'rb') as f:
            current_before = f.read()
        with open(app.PREVIOUS_FILE, 'rb') as f:
            previous_before = f.read()
        self.assertEqual(self.upload(boss).status_code, 302)
        # nothing about the live schedule changed
        with open(app.CURRENT_FILE, 'rb') as f:
            self.assertEqual(f.read(), current_before)
        with open(app.PREVIOUS_FILE, 'rb') as f:
            self.assertEqual(f.read(), previous_before)
        self.assertFalse(app.has_pending_upload())
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM schedule_uploads').fetchone()[0], 0)
        self.assertEqual([u['id'] for u in app.schedule_upload_history()], ['20261008-080000-000000'])

        old = app.old_schedules()
        self.assertEqual(len(old), 1)
        self.assertEqual((old[0]['schedule_date'], old[0]['filename'], old[0]['uploaded_by']),
                         ('2026-09-20', 'Production Schedule 20.09.xlsx', 'boss'))
        self.assertTrue(os.path.exists(os.path.join(app.OLD_SCHEDULE_DIR, old[0]['id'], 'source.xlsx')))
        # sorted by date with the real uploads: 08.10 (current), 01.10, then the old 20.09
        versions = app.schedule_versions()
        self.assertEqual([v['id'] for v in versions],
                         ['20261008-080000-000000', '20261001-080000-000000', old[0]['id']])

        # old 20.09 -> current: DPL0 and DPL1 left, DPL2 went 80 -> 20
        data, _n, ch, new = app.purchasing_export('20261008-080000-000000', old[0]['id'])
        self.assertEqual(new['id'], '20261008-080000-000000')
        self.assertEqual(sorted(l['dpl'] for l in ch['shipped'] if l['type'] == 'Fully shipped'), ['DPL0', 'DPL1'])
        part = [l for l in ch['shipped'] if l['type'] == 'Partially shipped']
        self.assertEqual([(l['dpl'], l['before'], l['after']) for l in part], [('DPL2', 80, 20)])
        summary = str([list(r) for r in openpyxl.load_workbook(io.BytesIO(data))['Summary'].iter_rows(values_only=True)])
        self.assertIn('Production Schedule 20.09.xlsx (schedule of 2026-09-20, added for comparison)', summary)
        # either order, and through the download
        _d, _n, swapped, _u = app.purchasing_export(old[0]['id'], '20261008-080000-000000')
        self.assertEqual(swapped['shipped'], ch['shipped'])
        r = boss.get(f'/export/purchasing.xlsx?base={old[0]["id"]}&upload=20261008-080000-000000')
        self.assertEqual(r.status_code, 200)

        page = boss.get('/').get_data(as_text=True)
        self.assertIn(f'<option value="{old[0]["id"]}">2026-09-20 · Production Schedule 20.09.xlsx', page)
        self.assertIn('/schedule/old', page)

    def test_two_old_schedules_compare_with_each_other(self):
        boss = self.client('boss')
        self.upload(boss, '2026-09-13', workbook_bytes(OLD_ROWS + [['DPL9', 'PO-9', 'JS', 'X1', 'Thing', 5, '2026-09-30']]))
        self.upload(boss, '2026-09-20')
        older, newer = sorted(app.old_schedules(), key=lambda v: v['schedule_date'])
        _d, _n, ch, new = app.purchasing_export(newer['id'], older['id'])
        self.assertEqual(new['id'], newer['id'])
        self.assertEqual([l['dpl'] for l in ch['shipped']], ['DPL9'])

    def test_validation_and_delete(self):
        boss = self.client('boss')
        self.upload(boss, day='2999-01-01')
        self.upload(boss, day='')
        self.upload(boss, filename='schedule.xls')
        self.upload(boss, content=b'not a workbook')
        self.assertEqual(app.old_schedules(), [])
        self.upload(boss)
        old_id = app.old_schedules()[0]['id']
        self.assertEqual(boss.post(f'/schedule/old/{old_id}/delete', data={'_csrf_token': 'tok'}).status_code, 302)
        self.assertEqual(app.old_schedules(), [])
        self.assertEqual(boss.post('/schedule/old/..%2Fhistory/delete', data={'_csrf_token': 'tok'}).status_code, 404)
        self.assertTrue(os.path.isdir(app.HISTORY_DIR))

    def test_admin_only(self):
        for name in ('hq1', 'lead1'):
            c = self.client(name)
            self.assertEqual(self.upload(c).status_code, 403)
            self.assertNotIn('/schedule/old', c.get('/').get_data(as_text=True))
        self.assertEqual(app.old_schedules(), [])
        self.upload(self.client('boss'))
        old_id = app.old_schedules()[0]['id']
        self.assertEqual(self.client('lead1').post(f'/schedule/old/{old_id}/delete',
                                                   data={'_csrf_token': 'tok'}).status_code, 403)
        self.assertEqual(len(app.old_schedules()), 1)


if __name__ == '__main__':
    unittest.main()
