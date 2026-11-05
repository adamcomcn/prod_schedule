"""Dropbox off-site backup: PKCE sign-in, hourly upload of new files, daily data snapshot,
failure alerts. The Dropbox API is replaced by a fake (dropbox_backup.http)."""
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
import zipfile
from unittest import mock
from urllib.parse import parse_qs, urlparse

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import dropbox_backup
from db import db_conn
from werkzeug.security import generate_password_hash

REFRESH = 'rt-very-secret-refresh-token'


class FakeDropbox:
    def __init__(self, scope='account_info.read files.content.write', account_id='dbid:one'):
        self.scope, self.account_id = scope, account_id
        self.files, self.sessions, self.deleted, self.calls, self.moves = {}, {}, [], [], []
        self.fail, self.revoked, self.verifier = None, False, None

    @staticmethod
    def reply(status, data):
        return status, json.dumps(data).encode('utf-8')

    def __call__(self, url, data, headers, timeout=180):
        self.calls.append(url)
        if url == dropbox_backup.TOKEN_URL:
            form = {k: v[0] for k, v in parse_qs(data.decode('ascii')).items()}
            if form['grant_type'] == 'authorization_code':
                self.verifier = form['code_verifier']
                return self.reply(200, {'access_token': 'at', 'refresh_token': REFRESH, 'expires_in': 14400,
                                        'account_id': self.account_id, 'scope': self.scope})
            if form.get('refresh_token') != REFRESH:
                return self.reply(400, {'error': 'invalid_grant', 'error_description': 'refresh token is malformed'})
            return self.reply(200, {'access_token': 'at', 'expires_in': 14400})
        assert headers['Authorization'] == 'Bearer at'
        name = url.split('/2/', 1)[1]
        if 'Dropbox-API-Arg' in headers:
            arg = json.loads(headers['Dropbox-API-Arg'])
        else:
            arg = json.loads(data.decode('utf-8'))
        if self.fail and name.startswith('files/'):
            return self.reply(409, {'error_summary': self.fail})
        if name == 'files/upload':
            self.files[arg['path']] = bytes(data)
            return self.reply(200, {'path_display': arg['path']})
        if name == 'files/upload_session/start':
            sid = f's{len(self.sessions)}'
            self.sessions[sid] = bytearray(data)
            return self.reply(200, {'session_id': sid})
        if name in ('files/upload_session/append_v2', 'files/upload_session/finish'):
            buf = self.sessions[arg['cursor']['session_id']]
            assert arg['cursor']['offset'] == len(buf), 'offset mismatch'
            buf.extend(data)
            if name.endswith('finish'):
                self.files[arg['commit']['path']] = bytes(buf)
                return self.reply(200, {'path_display': arg['commit']['path']})
            return 200, b'null'
        if name == 'files/delete_v2':
            path = arg['path']
            self.deleted.append(path)
            gone = [k for k in self.files if k == path or k.startswith(path + '/')]
            if not gone:
                return self.reply(409, {'error_summary': 'path_lookup/not_found/'})
            for k in gone:
                del self.files[k]
            return self.reply(200, {})
        if name == 'files/move_v2':
            self.moves.append((arg['from_path'], arg['to_path']))
            if arg['from_path'] not in self.files:
                return self.reply(409, {'error_summary': 'from_lookup/not_found/'})
            if arg['to_path'] in self.files:
                return self.reply(409, {'error_summary': 'to/conflict/file/'})
            self.files[arg['to_path']] = self.files.pop(arg['from_path'])
            return self.reply(200, {})
        if name == 'users/get_current_account':
            return self.reply(200, {'account_id': self.account_id, 'email': 'backup@example.com',
                                    'name': {'display_name': 'QC Backup'}})
        if name == 'users/get_space_usage':
            return self.reply(200, {'used': 1024 ** 3, 'allocation': {'.tag': 'individual', 'allocated': 2 * 1024 ** 3}})
        if name == 'auth/token/revoke':
            self.revoked = True
            return 200, b'null'
        raise AssertionError(name)


class DropboxBackupTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        self.fake = FakeDropbox()
        base = os.path.join(app.APP_DATA_DIR, f'dbx-test-{uuid.uuid4().hex[:8]}')
        self.uploads, self.images = os.path.join(base, 'uploads'), os.path.join(base, 'product_images')
        os.makedirs(self.uploads)
        os.makedirs(self.images)
        self.base_rel = os.path.basename(base)
        for patcher in (mock.patch.object(dropbox_backup, 'http', self.fake),
                        mock.patch.object(app, 'DROPBOX_APP_KEY', 'test-app-key'),
                        mock.patch.object(app, 'UPLOAD_DIR', self.uploads),
                        mock.patch.object(app, 'PRODUCT_IMG_DIR', self.images)):
            patcher.start()
            self.addCleanup(patcher.stop)
        original = app.load_json(app.INSPECTIONS_CACHE, {})
        self.addCleanup(app.save_json, app.INSPECTIONS_CACHE, original)
        app.save_json(app.INSPECTIONS_CACHE, {})
        if os.path.exists(app.DROPBOX_TOKEN_FILE):
            os.remove(app.DROPBOX_TOKEN_FILE)
        config = app.load_config()
        for key in [k for k in config if k.startswith('dropbox_') or k.startswith('full_backup')]:
            config.pop(key)
        config['backup_emails'] = 'it@example.com'
        app.save_json(app.CONFIG_FILE, config)
        with db_conn() as conn:
            conn.execute('DELETE FROM dropbox_files')
            conn.execute('DELETE FROM users')
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('boss',?,'admin')",
                         (generate_password_hash('x' * 12),))
            conn.execute("INSERT INTO users (username,password_hash,role) VALUES ('qc1',?,'inspector')",
                         (generate_password_hash('x' * 12),))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}

    def client(self, user='boss'):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[user]
            s['_csrf_token'] = 'tok'
        return c

    def write(self, rel, content, age=600):
        full = os.path.join(self.uploads if not rel.startswith('img/') else self.images, rel.removeprefix('img/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as fh:
            fh.write(content)
        past = time.time() - age
        os.utime(full, (past, past))
        return f'/其他 Other/{self.base_rel}/' + ('product_images/' + rel[4:] if rel.startswith('img/') else 'uploads/' + rel)

    @staticmethod
    def sync():
        with app.app.test_request_context():
            return app.run_dropbox_sync()

    def connect(self, c=None):
        c = c or self.client()
        location = c.get('/settings/dropbox/connect').headers['Location']
        state = parse_qs(urlparse(location).query)['state'][0]
        return c, c.get(f'/settings/dropbox/callback?code=abc&state={state}')

    # ── sign-in ──
    def test_connect_sends_admin_to_dropbox_with_pkce(self):
        c = self.client()
        response = c.get('/settings/dropbox/connect')
        self.assertEqual(response.status_code, 302)
        url = urlparse(response.headers['Location'])
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.assertEqual(f'{url.scheme}://{url.netloc}{url.path}', dropbox_backup.AUTHORIZE_URL)
        self.assertEqual(query['client_id'], 'test-app-key')
        self.assertEqual(query['code_challenge_method'], 'S256')
        self.assertEqual(query['token_access_type'], 'offline')
        self.assertEqual(query['redirect_uri'], 'http://localhost/settings/dropbox/callback')
        with c.session_transaction() as s:
            self.assertEqual(s['dropbox_oauth']['state'], query['state'])

    def test_callback_saves_token_outside_every_backup(self):
        c, response = self.connect()
        self.assertTrue(response.headers['Location'].endswith('/settings#dropbox'))
        saved = app.dropbox_connection()
        self.assertEqual(saved['refresh_token'], REFRESH)
        self.assertEqual(saved['email'], 'backup@example.com')
        self.assertTrue(self.fake.verifier)
        self.assertNotIn(os.path.abspath(app.DATA_DIR), os.path.abspath(app.DROPBOX_TOKEN_FILE))
        page = c.get('/settings').get_data(as_text=True)
        self.assertIn('backup@example.com', page)
        self.assertIn('立即同步', page)
        self.assertNotIn(REFRESH, page)
        path = app.build_backup_zip(include_files=True)
        try:
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():
                    self.assertNotIn(REFRESH.encode(), zf.read(name), name)
        finally:
            os.remove(path)

    def test_callback_with_wrong_state_is_refused(self):
        c = self.client()
        c.get('/settings/dropbox/connect')
        c.get('/settings/dropbox/callback?code=abc&state=forged')
        self.assertEqual(app.dropbox_connection(), {})

    def test_callback_without_write_permission_is_refused(self):
        self.fake.scope = 'account_info.read'
        _c, response = self.connect()
        self.assertEqual(app.dropbox_connection(), {})
        self.assertTrue(self.fake.revoked)

    def test_only_admin(self):
        self.assertEqual(self.client('qc1').get('/settings/dropbox/connect').status_code, 403)
        self.assertEqual(self.client('qc1').post('/settings/dropbox/sync', data={'_csrf_token': 'tok'}).status_code, 403)

    def test_settings_explains_setup(self):
        page = self.client().get('/settings').get_data(as_text=True)
        self.assertIn('连接 Dropbox', page)
        self.assertIn('http://localhost/settings/dropbox/callback', page)
        with mock.patch.object(app, 'DROPBOX_APP_KEY', ''):
            page = self.client().get('/settings').get_data(as_text=True)
        self.assertIn('DROPBOX_APP_KEY', page)
        self.assertNotIn('/settings/dropbox/connect', page)

    # ── sync ──
    def test_sync_uploads_new_files_once_and_a_daily_snapshot(self):
        self.connect()
        report = self.write('job1/report.xlsx', b'excel-bytes')
        video = self.write('job1/drafts/video.mp4', b'v' * 25)           # > CHUNK: upload session
        photo = self.write('img/p1.jpg', b'jpeg')
        self.write('job1/just-now.jpg', b'still uploading', age=0)       # too new: next run
        with mock.patch.object(dropbox_backup, 'CHUNK', 10), \
                mock.patch.object(app, '_backup_day', return_value='2026-11-15'):
            ok, _msg = self.sync()
        self.assertTrue(ok)
        self.assertEqual(self.fake.files[report], b'excel-bytes')
        self.assertEqual(self.fake.files[video], b'v' * 25)
        self.assertEqual(self.fake.files[photo], b'jpeg')
        self.assertNotIn(f'/其他 Other/{self.base_rel}/uploads/job1/just-now.jpg', self.fake.files)
        snapshot = self.fake.files['/data/qc-backup-data-2026-11-15.zip']
        self.assertTrue(snapshot.startswith(b'PK'))
        self.assertIn('/data/qc-backup-data-2026-10-16.zip', self.fake.deleted)     # older than 30 days
        config = app.load_config()
        self.assertEqual(config['dropbox_last_error'], '')
        self.assertEqual(config['dropbox_last_ok'], config['dropbox_last_run'])
        self.assertEqual(config['dropbox_pending_count'], 1)
        self.assertEqual(config['dropbox_space_total'], 2 * 1024 ** 3)
        self.assertTrue(app.dropbox_healthy())

        # second run: nothing new, no second snapshot today
        uploads = sum(1 for u in self.fake.calls if u.startswith(dropbox_backup.CONTENT_URL))
        with mock.patch.object(app, '_backup_day', return_value='2026-11-15'):
            self.assertTrue(self.sync()[0])
        self.assertEqual(sum(1 for u in self.fake.calls if u.startswith(dropbox_backup.CONTENT_URL)), uploads)

        # a new file goes up with the next run
        later = self.write('job2/new.pdf', b'pdf')
        with mock.patch.object(app, '_backup_day', return_value='2026-11-15'):
            self.sync()
        self.assertEqual(self.fake.files[later], b'pdf')

        page = self.client().get('/settings').get_data(as_text=True)
        self.assertIn('Dropbox 自动备份正常', page)

    def test_month_start_snapshot_is_kept(self):
        self.connect()
        with mock.patch.object(app, '_backup_day', return_value='2026-10-31'):
            self.sync()
        self.assertNotIn('/data/qc-backup-data-2026-10-01.zip', self.fake.deleted)

    def test_full_dropbox_is_reported_and_emailed_once_a_day(self):
        self.connect()
        self.write('job1/a.jpg', b'a')
        self.fake.fail = 'path/insufficient_space/...'
        sent = []
        with mock.patch.object(app, '_smtp_send', side_effect=lambda *a: sent.append(a) or (True, 'ok')):
            ok, message = self.sync()
            self.assertFalse(ok)
            self.sync()
        self.assertIn('Dropbox 空间已满', message)
        self.assertEqual(len(sent), 1)
        self.assertIn('Dropbox', sent[0][0])
        self.assertEqual(sent[0][2], ['it@example.com'])
        self.assertIn('Dropbox 空间已满', app.load_config()['dropbox_last_error'])
        self.assertFalse(app.dropbox_healthy())
        page = self.client().get('/settings').get_data(as_text=True)
        self.assertIn('Dropbox 空间已满', page)

    def test_revoked_sign_in_asks_to_reconnect(self):
        self.connect()
        app.save_json(app.DROPBOX_TOKEN_FILE, dict(app.dropbox_connection(), refresh_token='revoked'))
        with mock.patch.object(app, '_smtp_send', return_value=(True, 'ok')):
            ok, message = self.sync()
        self.assertFalse(ok)
        self.assertIn('重新连接', message)

    def test_reconnecting_same_account_keeps_progress_other_account_restarts(self):
        self.connect()
        self.write('job1/a.jpg', b'a')
        self.sync()
        self.client().post('/settings/dropbox/disconnect', data={'_csrf_token': 'tok'})
        self.assertTrue(self.fake.revoked)
        self.assertEqual(app.dropbox_connection(), {})
        self.assertFalse(app.dropbox_ready())
        self.connect()
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM dropbox_files').fetchone()[0], 1)
        self.fake.account_id = 'dbid:two'
        self.connect()
        with db_conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM dropbox_files').fetchone()[0], 0)

    def test_sync_now_button(self):
        self.connect()
        self.write('job1/a.jpg', b'a')
        response = self.client().post('/settings/dropbox/sync', data={'_csrf_token': 'tok'})
        self.assertEqual(response.status_code, 302)
        self.assertIn(f'/其他 Other/{self.base_rel}/uploads/job1/a.jpg', self.fake.files)

    def test_full_backup_reminder_paused_while_dropbox_works(self):
        from collections import namedtuple
        Disk = namedtuple('Disk', 'total used free')
        self.connect()
        self.sync()
        app.update_config(full_backup_last_at='2026-01-01 00:00')
        sent = []
        with mock.patch.object(app.shutil, 'disk_usage', return_value=Disk(10, 1, 9)), \
                mock.patch.object(app, '_smtp_send', side_effect=lambda *a: sent.append(a) or (True, 'ok')),                 app.app.test_request_context():
            app.send_storage_alert()
        self.assertEqual(sent, [])

    # ── readable layout ──
    JOB = 'MELBOURNE|DPL2633|GPEFAD100125'
    FOLDER = '/检验报告 Inspections/MELBOURNE/DPL2633/GPEFAD100125/QC-20260920-DPL2633-GPEFAD100125-1'
    RECORD = {'region': 'MELBOURNE', 'order_number': 'DPL2633', 'item_code': 'GPEFAD100125',
              'item_description': 'Restraint Flange Adaptor', 'inspector': 'murphy',
              'inspection_date': '2026-09-20', 'submitted_at': '2026-09-20 05:47', 'result': 'Pass',
              'report_no': 'QC-20260920-DPL2633-GPEFAD100125-1'}

    def add_report(self, job_key=JOB):
        cache = app.load_json(app.INSPECTIONS_CACHE, {})
        cache[job_key] = [dict(self.RECORD)]
        app.save_json(app.INSPECTIONS_CACHE, cache)

    def attach(self, rel, original, job_key=JOB, content=b'x'):
        self.write(rel, content)
        full = os.path.join(self.uploads, rel)
        with db_conn() as conn:
            conn.execute('INSERT INTO inspection_attachments (job_key, insp_index, evidence_type, original_name, '
                         'saved_name, file_path) VALUES (?,?,?,?,?,?)',
                         (job_key, 0, 'other', original, os.path.basename(full), full))
        self.addCleanup(self._sql, 'DELETE FROM inspection_attachments WHERE job_key=?', job_key)

    @staticmethod
    def _sql(sql, *args):
        with db_conn() as conn:
            conn.execute(sql, args)

    def uploads_count(self):
        return sum(1 for u in self.fake.calls if u.endswith('files/upload'))

    def test_files_are_filed_by_report_under_their_original_names(self):
        self.connect()
        self.add_report()
        self.attach('h1/aaa.xlsx', 'DPL2633-PE material report.xlsx', content=b'first')
        self.attach('h1/bbb.xlsx', 'DPL2633-PE material report.xlsx', content=b'second')
        self.attach('h1/ccc.pdf', 'a/b:c?.pdf')
        self.write('vt/v1.pdf', b'vt')
        with db_conn() as conn:
            conn.execute('INSERT INTO vtrust_files (job_number, original_name, saved_name, file_path) VALUES (?,?,?,?)',
                         ('VT-001', 'V-Trust report.pdf', 'v1.pdf', os.path.join(self.uploads, 'vt', 'v1.pdf')))
            conn.execute("INSERT INTO products (item_code, name, image_path) VALUES ('ABC123', 'Gate Valve', 'p9.jpg')")
        self.addCleanup(self._sql, "DELETE FROM vtrust_files WHERE job_number='VT-001'")
        self.addCleanup(self._sql, "DELETE FROM products WHERE item_code='ABC123'")
        self.write('img/p9.jpg', b'jpeg')
        self.assertTrue(self.sync()[0])
        files = self.fake.files
        self.assertEqual(files[f'{self.FOLDER}/DPL2633-PE material report.xlsx'], b'first')
        self.assertEqual(files[f'{self.FOLDER}/DPL2633-PE material report (2).xlsx'], b'second')
        self.assertIn(f'{self.FOLDER}/a_b_c_.pdf', files)
        self.assertTrue(files[f'{self.FOLDER}/QC-20260920-DPL2633-GPEFAD100125-1.pdf'].startswith(b'%PDF'))
        self.assertEqual(files['/V-Trust/VT-001/V-Trust report.pdf'], b'vt')
        self.assertEqual(files['/产品图片 Product images/ABC123 Gate Valve.jpg'], b'jpeg')
        self.assertFalse([k for k in files if k.startswith('/files/')])

    def test_report_pdf_is_sent_again_when_the_report_changes(self):
        self.connect()
        self.add_report()
        pdf = f'{self.FOLDER}/QC-20260920-DPL2633-GPEFAD100125-1.pdf'
        self.sync()
        self.assertIn(pdf, self.fake.files)
        before = self.uploads_count()
        self.sync()
        self.assertEqual(self.uploads_count(), before)              # unchanged: not sent again
        with db_conn() as conn:
            conn.execute("INSERT INTO inspection_reviews (job_key, insp_index, status, reviewer) "
                         "VALUES (?, 0, 'approved', 'hq1')", (self.JOB,))
        self.addCleanup(self._sql, 'DELETE FROM inspection_reviews WHERE job_key=?', self.JOB)
        self.sync()
        self.assertEqual(self.uploads_count(), before + 1)

    def test_draft_file_moves_into_the_report_when_submitted(self):
        self.connect()
        self.write('h2/drafts/d1.jpg', b'photo')
        full = os.path.join(self.uploads, 'h2', 'drafts', 'd1.jpg')
        with db_conn() as conn:
            draft = conn.execute('INSERT INTO inspection_drafts (job_key, user_id) VALUES (?,?)',
                                 (self.JOB, self.ids['qc1'])).lastrowid
            conn.execute("INSERT INTO draft_files (draft_id, ref, original_name, saved_name, file_path) "
                         "VALUES (?, 'q1', 'IMG_0001.jpg', 'd1.jpg', ?)", (draft, full))
        self.addCleanup(self._sql, 'DELETE FROM inspection_drafts WHERE job_key=?', self.JOB)
        self.addCleanup(self._sql, 'DELETE FROM draft_files WHERE draft_id=?', draft)
        self.sync()
        draft_path = '/草稿 Drafts/MELBOURNE/DPL2633/GPEFAD100125/IMG_0001.jpg'
        self.assertEqual(self.fake.files[draft_path], b'photo')
        # submitted: the draft rows go, the same file becomes a report attachment
        self._sql('DELETE FROM draft_files WHERE draft_id=?', draft)
        self.add_report()
        with db_conn() as conn:
            conn.execute("INSERT INTO inspection_attachments (job_key, insp_index, evidence_type, original_name, "
                         "saved_name, file_path) VALUES (?, 0, 'checklist_photo', 'IMG_0001.jpg', 'd1.jpg', ?)",
                         (self.JOB, full))
        self.addCleanup(self._sql, 'DELETE FROM inspection_attachments WHERE job_key=?', self.JOB)
        before = self.uploads_count()
        self.sync()
        self.assertNotIn(draft_path, self.fake.files)
        self.assertEqual(self.fake.files[f'{self.FOLDER}/IMG_0001.jpg'], b'photo')
        self.assertIn((draft_path, f'{self.FOLDER}/IMG_0001.jpg'), self.fake.moves)
        self.assertEqual(self.uploads_count(), before + 1)             # only the report PDF

    def test_first_layout_copies_are_moved_and_files_folder_removed(self):
        self.connect()
        self.add_report()
        self.attach('h3/old.xlsx', 'Material report.xlsx', content=b'old')
        kept = f'{self.base_rel}/uploads/h3/old.xlsx'
        gone = 'uploads/deleted-here/gone.jpg'
        self.fake.files['/files/' + kept] = b'old'
        self.fake.files['/files/' + gone] = b'gone'
        with db_conn() as conn:
            conn.execute("INSERT INTO dropbox_files (path, size, synced_at) VALUES (?, 3, 'x')", (kept,))
            conn.execute("INSERT INTO dropbox_files (path, size, synced_at) VALUES (?, 4, 'x')", (gone,))
        self.assertTrue(self.sync()[0])
        self.assertEqual(self.fake.files[f'{self.FOLDER}/Material report.xlsx'], b'old')
        self.assertIn(('/files/' + kept, f'{self.FOLDER}/Material report.xlsx'), self.fake.moves)
        self.assertEqual(self.fake.files['/其他 Other/' + gone], b'gone')     # deleted here: kept in Dropbox
        self.assertIn('/files', self.fake.deleted)
        self.assertFalse([k for k in self.fake.files if k.startswith('/files/')])
        self.assertTrue(app.load_config()['dropbox_legacy_cleaned'])

    def test_names_dropbox_rejects_are_cleaned(self):
        self.assertEqual(app._dbx_part('a/b\\c:d*e?f"g<h>i|j. '), 'a_b_c_d_e_f_g_h_i_j')
        self.assertEqual(app._dbx_part(''), '_')
        long = app._dbx_part('x' * 300 + '.xlsx')
        self.assertEqual(len(long), 120)
        self.assertTrue(long.endswith('.xlsx'))


if __name__ == '__main__':
    unittest.main()
