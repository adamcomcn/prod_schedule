"""Pressure test machine exports (Dalian): item codes in Settings get a
"machine data" evidence card as well as the pressure records; the uploaded
export is read, checked and added to the report and its PDF."""
import io
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl

import app
import pressure_machine as pm
from db import db_conn
from helpers import assign_job

JOB = 'MELBOURNE|PO-7|DFT1008F'
OTHER = 'MELBOURNE|PO-8|DFT1010F'
HEADERS = ['Order Number', 'Daemco Purchase Order', 'Item Code', 'Item Description', 'Quantity']

# Same layout as the machine's MCGS export (GB18030), made-up readings
SAMPLE_ROWS = [
    ('2026/06/16 07:38:47', 670, '60.000000', 'DN100*80', '合格', '壳体试验', '法兰三通', '2.493333'),
    ('2026/06/16 07:56:17', 942, '60.000000', 'DN100*80', '不合格', '壳体试验', '法兰三通', '2.400000'),
    ('2026/06/16 07:58:08', 700, '60.000000', 'DN100*80', '合格', '壳体试验', '法兰三通', '2.360000'),
    ('2026/06/16 08:00:47', 710, '45.000000', 'DN100*80', '合格', '壳体试验', '法兰三通', '2.583333'),
]


def sample_csv(rows=SAMPLE_ROWS):
    lines = ['"MCGS_TIME","MCGS_TIMEMS","C保压时间","C阀门型号","C试验结果","C试验内容","C产品编号","C试验压力"']
    for t, ms, hold, model, res, content, product, p in rows:
        lines.append(f'{t},{ms},{hold},"{model}","{res}","{content}","{product}",{p}')
    return ('\n'.join(lines) + '\n').encode('gb18030')


def xlsx(rows):
    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class ParserTests(unittest.TestCase):
    def test_reads_the_mcgs_csv(self):
        rows = pm.parse_machine_file(sample_csv(), '试压记录.csv')
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], {'time': '2026-06-16 07:38:47', 'hold': 60.0, 'model': 'DN100*80', 'ok': True,
                                   'content': '壳体试验', 'product': '法兰三通', 'pressure': 2.493333})
        self.assertIs(rows[1]['ok'], False)

    def test_summary_and_limits(self):
        rows = pm.parse_machine_file(sample_csv(), 'x.csv')
        plain = pm.summarise(rows)
        self.assertEqual((plain['tests'], plain['passed'], plain['failed']), (4, 3, 1))
        self.assertEqual((plain['low_pressure'], plain['short_hold']), (0, 0))
        self.assertTrue(plain['attention'])                       # one machine failure
        checked = pm.summarise(rows, min_pressure=2.4, min_hold=60, quantity=3)
        self.assertEqual((checked['low_pressure'], checked['short_hold']), (1, 1))
        self.assertFalse(checked['short_count'])                  # 3 passed for 3 ordered
        self.assertTrue(pm.summarise(rows, quantity=10)['short_count'])
        self.assertEqual(pm.row_flags(rows[2], 2.4, 60), ['pressure'])
        self.assertEqual(pm.row_flags(rows[3], 2.4, 60), ['hold'])
        lines = pm.summary_lines(pm.summarise(rows, 2.4, 60, 10))
        self.assertEqual(lines[0], ('共 4 次测试：合格 3，不合格 1', '4 tests: 3 passed, 1 failed'))
        self.assertIn(('合格件数 3 少于订单数量 10', '3 passed is less than the 10 ordered'), lines)
        good = pm.summarise([rows[0]], 2.4, 60, 1)
        self.assertFalse(good['attention'])

    def test_reads_xlsx_and_other_layouts(self):
        data = xlsx([('Report',), ('时间', '试验压力(MPa)', '保压时间(s)', '结果'),
                     ('2026-06-16 09:00:00', 2.5, 60, 'OK'), ('2026-06-16 09:05:00', 2.1, 60, 'NG'), (None, None)])
        rows = pm.parse_machine_file(data, 'export.xlsx')
        self.assertEqual([(r['pressure'], r['ok']) for r in rows], [(2.5, True), (2.1, False)])
        utf8 = 'time,result,pressure\n2026-06-16 10:00,PASS,2.45\n'.encode('utf-8-sig')
        self.assertEqual(pm.parse_machine_file(utf8, 'a.csv')[0]['pressure'], 2.45)

    def test_files_that_are_not_exports(self):
        with self.assertRaises(pm.MachineFileError):
            pm.parse_machine_file(b'a,b\n1,2\n', 'x.csv')
        with self.assertRaises(pm.MachineFileError):
            pm.parse_machine_file(sample_csv([]), 'x.csv')
        with self.assertRaises(pm.MachineFileError):
            pm.parse_machine_file(b'not a workbook', 'x.xlsx')
        self.assertFalse(pm.is_machine_file('photo.jpg'))

    def test_item_codes(self):
        data = xlsx([('NO', 'description', 'Product Code'), (1, 'Tee', 'dft1008f'), (2, 'Bend', 'DFB1006S'),
                     (3, 'Tee again', 'DFT1008F')])
        self.assertEqual(pm.codes_from_workbook(data), ['DFT1008F', 'DFB1006S'])
        self.assertEqual(pm.codes_from_text('dft1008f, DFB1006S\nDFT15*  '), ['DFT1008F', 'DFB1006S', 'DFT15*'])
        self.assertTrue(pm.code_matches('dft1508sf', ['DFT15*']))
        self.assertTrue(pm.code_matches('DFB1006S', ['DFB1006S']))
        self.assertFalse(pm.code_matches('DFB1006SX', ['DFB1006S']))
        self.assertFalse(pm.code_matches('', ['*']))


class MachineEvidenceTests(unittest.TestCase):
    def setUp(self):
        app.app.config.update(TESTING=True)
        with db_conn() as conn:
            for table in ('users', 'inspection_tasks', 'inspection_attachments', 'inspection_drafts',
                          'draft_files', 'checklist_templates', 'checklist_versions', 'product_reference'):
                conn.execute(f'DELETE FROM {table}')
            for name, role in (('yu', 'inspector'), ('boss', 'admin')):
                conn.execute('INSERT INTO users (username,password_hash,role) VALUES (?,?,?)', (name, 'x', role))
            self.ids = {r['username']: r['id'] for r in conn.execute('SELECT id, username FROM users')}
        schedule = {'MELBOURNE': [HEADERS, ['DPL7', 'PO-7', 'DFT1008F', 'DN100 x 80 FL TEE PN16 DI', '3'],
                                  ['DPL8', 'PO-8', 'DFT1010F', 'DN100 x 100 FL TEE PN16 DI', '5']]}
        app.save_json(app.CURRENT_FILE, schedule)
        app.save_json(app.PREVIOUS_FILE, schedule)
        app.save_json(app.INSPECTIONS_CACHE, {})
        config = app.load_config()
        config.update(hq_report_emails='', task_notify_emails='', machine_test_codes=['DFT1008F'],
                      machine_min_pressure=2.4, machine_min_hold=60)
        app.save_json(app.CONFIG_FILE, config)
        assign_job(JOB, 'yu')

    def client(self, name='yu'):
        c = app.app.test_client()
        with c.session_transaction() as s:
            s['user_id'] = self.ids[name]
            s['_csrf_token'] = 'tok'
        return c

    def upload(self, c, name='试压记录.csv', content=None):
        return c.post(f'/inspect/{JOB}/draft/files', headers={'X-CSRF-Token': 'tok'},
                      data={'ref': 'ev:machine', 'file': (io.BytesIO(content or sample_csv()), name)},
                      content_type='multipart/form-data')

    def records(self, job=JOB):
        return app.load_json(app.INSPECTIONS_CACHE, {}).get(job, [])

    def test_listed_codes_need_machine_data_as_well_as_pressure_records(self):
        types = [e['type'] for e in app.required_evidence('di_fitting', 'DFT1008F')]
        self.assertEqual(types, ['brt', 'pressure', 'machine'])
        self.assertEqual([e['type'] for e in app.required_evidence('di_fitting', 'DFT1010F')], ['brt', 'pressure'])
        self.assertEqual([e['type'] for e in app.required_evidence(None, 'DFT1008F')], ['machine'])
        c = self.client()
        with app.app.test_request_context():
            reqs = {r['type']: r for r in app.get_evidence_requirements('DFT1008F', 'DN100 x 80 FL TEE')}
        self.assertEqual(reqs['machine']['machine_limits'], {'pressure': 2.4, 'hold': 60})
        page = c.get(f'/inspect/{JOB}').get_data(as_text=True)
        self.assertIn('data-ev="machine"', page)
        self.assertIn('data-ev="pressure"', page)
        self.assertIn('试验压力 ≥ 2.4 MPa', page)

    def test_upload_shows_the_tests_that_need_a_look(self):
        res = self.upload(self.client())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        m = res.get_json()['file']['machine']
        self.assertEqual((m['tests'], m['passed'], m['flagged_total']), (4, 3, 3))
        self.assertTrue(m['attention'])
        self.assertEqual([r['flags'] for r in m['flagged']], [['fail'], ['pressure'], ['hold']])
        self.assertIn('共 4 次测试：合格 3，不合格 1', m['lines'])
        # the preview is there again when the page is reopened
        self.assertIn('"flagged_total": 3', self.client().get(f'/inspect/{JOB}').get_data(as_text=True))
        bad = self.upload(self.client(), 'notes.csv', b'a,b\n1,2\n').get_json()['file']['machine']
        self.assertIn('无法识别', bad['error'])
        photo = self.upload(self.client(), 'screen.jpg', b'\xff\xd8jpeg').get_json()['file']
        self.assertIsNone(photo['machine'])

    def submit(self, c):
        return c.post(f'/inspect/{JOB}/submit', data={
            '_csrf_token': 'tok', 'region': 'MELBOURNE', 'item_code': 'DFT1008F',
            'item_description': 'DN100 x 80 FL TEE PN16 DI', 'inspection_date': '2026-11-19',
            'quantity_inspected': '3', 'quantity_ordered': '3', 'result': 'Pass',
            'ev_result_machine': 'Pass', 'ev_check_machine_0': 'ok', 'ev_result_brt': 'N/A'},
            content_type='multipart/form-data')

    def test_report_keeps_the_tests_and_the_pdf_lists_them(self):
        c = self.client()
        self.upload(c)
        self.assertEqual(self.submit(c).status_code, 302)
        [rec] = self.records()
        ev = rec['evidence']['machine']
        self.assertEqual(ev['files'], ['试压记录.csv'])
        # the pressure test records are still needed next to the machine data
        self.assertEqual(rec['missing_evidence'], ['pressure'])
        [entry] = ev['machine_data']
        self.assertEqual(entry['file'], '试压记录.csv')
        self.assertEqual(len(entry['rows']), 4)
        s = entry['summary']
        self.assertEqual((s['passed'], s['failed'], s['low_pressure'], s['short_hold']), (3, 1, 1, 1))
        self.assertEqual((s['min_pressure'], s['min_hold'], s['quantity']), (2.4, 60, 3.0))
        self.assertEqual(ev['checks'][0]['state'], 'ok')
        pdf = c.get(f'/inspect/{JOB}/report.pdf')
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b'%PDF'))

    def test_pdf_with_many_tests(self):
        rows = [(f'2026/06/16 08:{i // 60:02d}:{i % 60:02d}', 1, '60', 'DN100*80', '合格', '壳体试验', '法兰三通', '2.5')
                for i in range(pm.PDF_MAX_ROWS + 25)]
        c = self.client()
        self.upload(c, content=sample_csv(rows))
        self.submit(c)
        self.assertEqual(len(self.records()[0]['evidence']['machine']['machine_data'][0]['rows']), len(rows))
        self.assertEqual(c.get(f'/inspect/{JOB}/report.pdf').status_code, 200)

    def test_export_added_to_a_report_later(self):
        app.save_json(app.INSPECTIONS_CACHE, {JOB: [{
            'order_number': 'PO-7', 'item_code': 'DFT1008F', 'item_description': 'DN100 x 80 FL TEE',
            'inspector_name': 'Yu', 'inspection_date': '2026-11-19', 'result': 'Pass', 'quantity_ordered': '3',
            'submitted_by': 'yu', 'evidence': {}, 'missing_evidence': ['brt', 'machine']}]})
        boss = self.client('boss')
        res = boss.post(f'/inspect/{JOB}/report/0/evidence', data={
            '_csrf_token': 'tok', 'evidence_type': 'machine', 'files': [(io.BytesIO(sample_csv()), 'later.csv')]},
            content_type='multipart/form-data')
        self.assertEqual(res.status_code, 302)
        [rec] = self.records()
        self.assertEqual(rec['evidence']['machine']['machine_data'][0]['summary']['tests'], 4)
        self.assertNotIn('machine', rec['missing_evidence'])
        with db_conn() as conn:
            aid = conn.execute("SELECT id FROM inspection_attachments WHERE evidence_type='machine'").fetchone()[0]
        boss.post(f'/inspect/{JOB}/report/0/evidence/{aid}/delete', data={'_csrf_token': 'tok'})
        [rec] = self.records()
        self.assertEqual(rec['evidence']['machine']['machine_data'], [])
        self.assertIn('machine', rec['missing_evidence'])

    def test_settings(self):
        self.assertIn(self.client().post('/settings/machine', data={'_csrf_token': 'tok', 'machine_test_codes': 'X'})
                      .status_code, (302, 403))
        self.assertEqual(app.load_config()['machine_test_codes'], ['DFT1008F'])     # inspectors cannot change it
        boss = self.client('boss')
        self.assertIn('id="machine"', boss.get('/settings').get_data(as_text=True))
        res = boss.post('/settings/machine', data={
            '_csrf_token': 'tok', 'machine_test_codes': 'dft1008f\nDFT15*', 'machine_min_pressure': '2.4',
            'machine_min_hold': '',
            'codes_file': (io.BytesIO(xlsx([('NO', 'description', 'Product Code'), (1, 'Bend', 'DFB1006S')])),
                           'shaped_fittings.xlsx')}, content_type='multipart/form-data')
        self.assertEqual(res.status_code, 302)
        config = app.load_config()
        self.assertEqual(config['machine_test_codes'], ['DFT1008F', 'DFT15*', 'DFB1006S'])
        self.assertEqual((config['machine_min_pressure'], config['machine_min_hold']), (2.4, None))
        self.assertTrue(app.needs_machine_test('DFT1508SF'))
        boss.post('/settings/machine', data={'_csrf_token': 'tok', 'machine_test_codes': 'A',
                                             'codes_file': (io.BytesIO(b'x'), 'codes.csv')},
                  content_type='multipart/form-data')
        self.assertEqual(app.load_config()['machine_test_codes'], ['DFT1008F', 'DFT15*', 'DFB1006S'])  # rejected


if __name__ == '__main__':
    unittest.main()
