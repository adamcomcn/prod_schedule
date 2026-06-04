"""
One-time script: parse Excel inspection templates from 检验单模板 and load into DB.
Run once:  python import_forms.py
"""
import openpyxl, os, sys, sqlite3, json

sys.stdout.reconfigure(encoding='utf-8')

TEMPLATE_DIR = r'C:\Users\JWan\Documents\检验单\检验单模板'
DB_PATH      = r'C:\Users\JWan\Documents\prod_schedule\data\app.db'

# Auto-generated keyword rules per template name
KEYWORD_MAP = {
    '199 Cover':                       '199 COVER, 199',
    '200 Cover':                       '200 COVER, 200',
    'CI Hydrant Cover':                'CI HYDRANT COVER, HYDRANT COVER',
    'CI Lilac Hydrant Cover':          'CI LILAC HYDRANT, LILAC HYDRANT',
    'CI Lilac Sluice Valve Cover':     'CI LILAC SLUICE, LILAC SLUICE',
    'CI QLD Hydrant Box':              'CI QLD HYDRANT, QLD HYDRANT BOX',
    'CI Sluice Valve Cover':           'CI SLUICE VALVE COVER, SLUICE VALVE COVER',
    'EPDM Gaskets':                    'EPDM, GASKET',
    'Handwheels':                      'HANDWHEEL',
    'L Type Hydrant Cover':            'L TYPE HYDRANT, L-TYPE HYDRANT',
    'QLD DI Lid Class D':              'QLD DI LID, CLASS D LID, DI LID',
    'Round CI SV Hinge Lid':           'ROUND CI SV, HINGE LID, SV HINGE',
    'Square CI Hydrant Hinge Lid':     'SQUARE CI HYDRANT HINGE, HYDRANT HINGE',
    'SS Straps':                       'SS STRAP, STAINLESS STRAP, SS BAND',
    'Valve Extension Spindles':        'SPINDLE, EXTENSION SPINDLE, VALVE SPINDLE',
}


def parse_template(path):
    """Parse an Excel checklist into {title, sections: [{name, questions:[…]}]}."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    def cell(row, idx):
        return str(row[idx]).strip() if idx < len(row) and row[idx] is not None else ''

    # ── Find header row (contains "Q.") ──────────────────────────────────────
    hdr_idx = col_q = None
    for i, row in enumerate(rows):
        for j, c in enumerate(row):
            if str(c).strip() == 'Q.':
                hdr_idx, col_q = i, j
                break
        if col_q is not None:
            break

    if col_q is None:
        return None

    col_part   = col_q - 1          # part / section name column
    col_sect   = col_q - 2 if col_q >= 2 else col_q - 1  # wider section column
    col_guide  = col_q + 1
    col_cond   = col_q + 2
    col_action = col_q + 3

    # ── Extract title ─────────────────────────────────────────────────────────
    title = ''
    for row in rows[:hdr_idx]:
        for c in row:
            s = str(c).strip() if c else ''
            if 'DAEMCO' in s.upper() or 'RELIABLE' in s.upper():
                title = s.replace('\n', ' ')
                break
        if title:
            break

    # ── Parse question rows ───────────────────────────────────────────────────
    sections   = []
    cur_sect   = ''
    cur_part   = ''
    cur_qs     = []

    SKIP = {'DATE', 'SIGNATURE', 'DPL:', 'DPL', ''}

    for row in rows[hdr_idx + 1:]:
        # Skip entirely empty rows
        if not any(c for c in row if c is not None):
            continue

        sect_val  = cell(row, col_sect)  if col_sect >= 0  else ''
        part_val  = cell(row, col_part)  if col_part >= 0  else ''
        q_val     = cell(row, col_q)
        guide_val = cell(row, col_guide)
        cond_val  = cell(row, col_cond)
        act_val   = cell(row, col_action)

        # Skip date/signature footer rows
        first = next((str(c).strip() for c in row if c), '')
        if first.upper() in {'DATE', 'SIGNATURE', 'DPL:', 'DPL'}:
            continue

        # Question row: Q. column has a value
        if q_val and (guide_val or part_val):
            # New section when section column changed
            if sect_val and sect_val.upper() not in SKIP and sect_val != cur_sect:
                if cur_qs:
                    sections.append({'name': cur_sect or cur_part, 'questions': cur_qs})
                    cur_qs = []
                cur_sect = sect_val
                cur_part = part_val or cur_part

            elif part_val and part_val != cur_part:
                # Same section, new part — start a new sub-group
                cur_part = part_val

            cur_qs.append({
                'part':      cur_part,
                'num':       q_val,
                'guideline': guide_val.replace('\n', ' '),
                'condition': cond_val,
                'action':    act_val,
            })

        elif (sect_val or part_val) and not q_val:
            name = sect_val or part_val
            if name.upper() in SKIP:
                continue
            # Section/part header without a question number
            if cur_qs:
                sections.append({'name': cur_sect or cur_part, 'questions': cur_qs})
                cur_qs = []
            cur_sect = name
            cur_part = ''

    if cur_qs:
        sections.append({'name': cur_sect or cur_part, 'questions': cur_qs})

    return {'title': title, 'sections': sections}


# ── Import to DB ──────────────────────────────────────────────────────────────
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.execute('PRAGMA foreign_keys = ON')

# Ensure tables exist
conn.executescript("""
CREATE TABLE IF NOT EXISTS form_templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    keywords      TEXT    DEFAULT '',
    sections_json TEXT    DEFAULT '[]',
    source_file   TEXT    DEFAULT '',
    created_at    TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS form_responses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key       TEXT    NOT NULL,
    template_id   INTEGER REFERENCES form_templates(id) ON DELETE SET NULL,
    template_name TEXT    DEFAULT '',
    dpl_number    TEXT    DEFAULT '',
    inspector     TEXT    DEFAULT '',
    insp_date     TEXT    DEFAULT '',
    answers       TEXT    DEFAULT '{}',
    overall       TEXT    DEFAULT '',
    summary       TEXT    DEFAULT '',
    submitted_at  TEXT    DEFAULT (datetime('now','localtime'))
);
""")
conn.commit()

imported = 0
for fname in sorted(os.listdir(TEMPLATE_DIR)):
    if not fname.endswith(('.xlsx', '.xls')):
        continue
    path = os.path.join(TEMPLATE_DIR, fname)

    # Template name = filename without version suffix and extension
    base = fname.replace(' - Reliable Inspection - V1.0.xlsx', '')
    base = base.replace(' - V1.0.xlsx', '').replace('.xlsx', '').strip()

    result = parse_template(path)
    if not result or not result['sections']:
        print(f'  SKIP (no questions parsed): {fname}')
        continue

    q_count = sum(len(s['questions']) for s in result['sections'])
    keywords = KEYWORD_MAP.get(base, base.upper())

    # Upsert: replace existing entry with same name
    existing = conn.execute('SELECT id FROM form_templates WHERE name=?', (base,)).fetchone()
    if existing:
        conn.execute(
            'UPDATE form_templates SET keywords=?, sections_json=?, source_file=? WHERE id=?',
            (keywords, json.dumps(result['sections'], ensure_ascii=False), fname, existing['id']))
        print(f'  UPDATED: {base} ({len(result["sections"])} sections, {q_count} questions)')
    else:
        conn.execute(
            'INSERT INTO form_templates (name, keywords, sections_json, source_file) VALUES (?,?,?,?)',
            (base, keywords, json.dumps(result['sections'], ensure_ascii=False), fname))
        print(f'  IMPORTED: {base} ({len(result["sections"])} sections, {q_count} questions)')

    imported += 1

conn.commit()
conn.close()
print(f'\nDone. {imported} templates imported.')
