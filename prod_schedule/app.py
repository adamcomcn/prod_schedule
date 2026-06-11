import os, io, json, hashlib, tempfile, math, logging, traceback
from difflib import SequenceMatcher
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session, send_from_directory, g
from werkzeug.security import generate_password_hash, check_password_hash
import msoffcrypto
import openpyxl
from db import db_conn, init_db

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

_last_error = {'tb': '', 'time': ''}

# Google API imports (graceful fallback if not configured)
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False

app = Flask(__name__)
IS_PRODUCTION = bool(
    os.environ.get('RAILWAY_ENVIRONMENT_ID')
    or os.environ.get('RAILWAY_ENVIRONMENT_NAME')
    or os.environ.get('RENDER')
)
SECRET_KEY = os.environ.get('SECRET_KEY')
if IS_PRODUCTION and not SECRET_KEY:
    raise RuntimeError('SECRET_KEY is required in production')
app.secret_key = SECRET_KEY or 'local-development-only'
app.jinja_env.globals['enumerate'] = enumerate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DATA_DIR = os.path.abspath(os.environ.get('APP_DATA_DIR', BASE_DIR))
DATA_DIR = os.path.join(APP_DATA_DIR, 'data')
UPLOAD_DIR = os.path.join(APP_DATA_DIR, 'uploads')
CREDENTIALS_FILE = os.path.join(APP_DATA_DIR, 'credentials.json')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
CURRENT_FILE = os.path.join(DATA_DIR, 'current_week.json')
PREVIOUS_FILE = os.path.join(DATA_DIR, 'previous_week.json')
INSPECTIONS_CACHE = os.path.join(DATA_DIR, 'inspections_cache.json')

EXCEL_PASSWORD   = 'castings1'
PRODUCT_IMG_DIR  = os.path.join(BASE_DIR, 'static', 'product_images')

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

# ── helpers ──────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_config():
    return load_json(CONFIG_FILE, {
        'sheet_id': '',
        'drive_folder_id': '',
        'upload_date': '',
        'valve_prefixes': ['RSV'],
        'office_locations': [],
    })

def haversine(lat1, lon1, lat2, lon2):
    """Distance in metres between two GPS coordinates."""
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def is_valve(item_description, item_code, config=None):
    """Return True if the item is a valve (requires V-Trust inspection)."""
    if 'valve' in str(item_description).lower():
        return True
    prefixes = (config or {}).get('valve_prefixes', ['RSV'])
    code = str(item_code).upper()
    return any(code.startswith(p.upper()) for p in prefixes if p)

# ── Evidence requirement system ───────────────────────────────────────────────

EVIDENCE_META = {
    'brt':      {'label': 'BRT (Batch Release Test)',      'icon': '📋', 'color': '#1e40af', 'bg': '#dbeafe', 'accepts': '.pdf,.xlsx,.xls,.jpg,.jpeg,.png'},
    'checklist':{'label': 'Inspection Checklist',           'icon': '✅', 'color': '#065f46', 'bg': '#d1fae5', 'accepts': '.pdf,.jpg,.jpeg,.png'},
    'material': {'label': 'Material Report',                'icon': '🔬', 'color': '#92400e', 'bg': '#fef3c7', 'accepts': '.pdf,.xlsx,.xls,.jpg,.jpeg,.png'},
    'daq':      {'label': 'DAQ Data (Pressure Test)',       'icon': '📊', 'color': '#5b21b6', 'bg': '#ede9fe', 'accepts': '.pdf,.xlsx,.xls,.csv'},
    'vtrust':   {'label': 'V-Trust Pressure Test Video',    'icon': '🎥', 'color': '#9a3412', 'bg': '#fff7ed', 'accepts': '.mp4,.mov,.avi,.mkv'},
    'spark':    {'label': 'Spark / Holiday Test Video',     'icon': '⚡', 'color': '#991b1b', 'bg': '#fee2e2', 'accepts': '.mp4,.mov,.avi,.mkv'},
    'xrf':      {'label': 'XRF Report (Material Composition)', 'icon': '⚗️', 'color': '#065f46', 'bg': '#d1fae5', 'accepts': '.pdf,.xlsx,.xls,.jpg,.jpeg,.png'},
}

def extract_dn(item_code):
    """Return DN size (int) from an RSV item code, or None if not parseable."""
    import re
    code = (item_code or '').upper().strip()
    # RSV0 + 3 digits:  RSV0080… → DN80,  RSV0250… → DN250
    m = re.match(r'^RSV0(\d{3})', code)
    if m:
        return int(m.group(1))
    # RSVPE/SO/SP/CAP + digits:  RSVPE125 → DN125
    m = re.match(r'^RSV(?:PE|SO|SP|CAP)(\d{1,3})', code)
    if m:
        return int(m.group(1))
    return None

def get_evidence_requirements(item_code, category_name=''):
    """Return list of evidence dicts required for this product.
    Each dict: {type, label, icon, color, bg, accepts, guidance}

    Matching priority:
      1. Item code prefix (reliable, code-driven)
      2. Product category name (fallback when code prefix unknown)
    """
    import re
    code = (item_code or '').upper().strip()
    cat  = (category_name or '').lower()

    def ev(etype, guidance):
        m = EVIDENCE_META[etype]
        return {**m, 'type': etype, 'guidance': guidance}

    reqs = []

    # ── 1. RSVCAP — Valve Caps (Checklist only, NOT gate valves) ───────
    # Must be checked before the generic RSV block
    if code.startswith('RSVCAP'):
        reqs.append(ev('checklist', 'Inspection checklist must be signed and dated by inspector.'))
        return reqs

    # ── 2. All RSV Gate Valves (FL / SO / SP / PE and any other suffix) ─
    # Rules are size-driven for ALL sub-types (FL, SO, SP, PE, etc.)
    # Spreadsheet rows 3 & 4 explicitly list FL, SO, SP, PE together
    if code.startswith('RSV'):
        dn = extract_dn(code)

        # BRT guidance depends only on DN (not sub-type)
        if dn == 375:
            brt_guide = (
                'Check Material sheet: chemical and mechanical properties within limits\n'
                'Check Checking Report: C1–C7 & A1–A9 must be OK (refer to SPEC sheet)\n'
                'Note: DN375 has no DAQ — verify pressure test via Cells Z–AG and V-Trust video only')
        else:
            brt_guide = (
                'Check Material sheet: chemical and mechanical properties within limits (Spec 500-7)\n'
                'Check Checking Report: C1–C7 & A1–A9 must be OK (refer to SPEC sheet)\n'
                'Check DAQ section (Cells AB–BH):\n'
                '  • T1 Average [AL] (Gate test 1) ≥ 1.76 MPa\n'
                '  • T2 Average [AW] (Gate test 2) ≥ 1.76 MPa\n'
                '  • T3 Average [BH] (Body test)   ≥ 2.40 MPa')
        reqs.append(ev('brt', brt_guide))

        # DAQ — all RSVs except DN375
        if dn != 375:
            reqs.append(ev('daq',
                'Upload pressure test machine exported data (Excel or PDF).\n'
                'Verify: T1 Average ≥ 1.76 MPa, T2 Average ≥ 1.76 MPa, T3 Average ≥ 2.40 MPa'))

        # V-Trust — all RSVs
        reqs.append(ev('vtrust',
            'Upload V-Trust pressure test video(s) (MP4 / MOV).\n'
            'Ensure all videos are received and show acceptable test results.'))

        # Spark / Holiday test — DN200 and above only
        if dn is not None and dn >= 200:
            reqs.append(ev('spark',
                '电火花 Holiday / Spark test video required for DN200 and above.\n'
                'Ensure all spark test videos are received (MP4 / MOV).'))

        return reqs

    # ── 3. UMC Couplings (item code starts with UMC) ───────────────────
    if code.startswith('UMC'):
        is_gal = 'GAL' in code
        guide = ('Check 316 SS.jpg — confirm material is 316 stainless steel\n'
                 'Check Assembly Report — all criteria acceptable\n'
                 'Check Bolt 316.jpg — confirm bolt material is 316 SS\n'
                 'Check Dimension Report — compare to Daemco design drawings')
        if is_gal:
            guide += '\nFor GAL variant: ensure bolts are Steel (Q235B) material'
        reqs.append(ev('brt', guide))
        return reqs

    # ── 4. Category-based matching (for products without a known code prefix) ─
    # ── Repair Clamps ──────────────────────────────────────────────────
    if 'repair' in cat or 'repair clamp' in cat:
        is_gal = 'GAL' in code
        guide = ('Check Assembly Folder — all checkboxes acceptable\n'
                 'Check Dimension Report — compare to Daemco REPAIR CLAMP 2023.11.7 drawing\n'
                 'Check Material Folder — verify 316 stainless steel')
        if is_gal:
            guide += '\nFor GAL variant: ensure bolts are Steel (Q235B) material'
        reqs.append(ev('brt', guide))
        reqs.append(ev('xrf',
            'Upload XRF Excel / PDF report.\n'
            'Verify material is 316 stainless steel. Confirm Pass or Fail.'))
        return reqs

    # ── Couplings (Gibault and other non-UMC couplings) ────────────────
    if 'coupling' in cat:
        reqs.append(ev('brt', 'Review BRT document for acceptability.'))
        reqs.append(ev('material',
            'Check material report: chemical and mechanical properties within limits.'))
        return reqs

    # ── DI Fittings ────────────────────────────────────────────────────
    if 'di fitting' in cat or ('fitting' in cat and 'di' in cat):
        reqs.append(ev('brt',
            'Check DPL Fitting — Casting Inspection Report: all criteria acceptable\n'
            'Check DPL Fitting — Final Inspection Report: all criteria acceptable'))
        return reqs

    # ── Blank Flanges / Tapped Flanges ─────────────────────────────────
    if 'flange' in cat:
        reqs.append(ev('brt', 'Review BRT document for acceptability.'))
        reqs.append(ev('material',
            'Check material report: chemical and mechanical properties within limits.'))
        return reqs

    # ── Extension Spindles ─────────────────────────────────────────────
    if 'spindle' in cat:
        reqs.append(ev('checklist', 'Inspection checklist must be signed and dated by inspector.'))
        reqs.append(ev('material',
            'Check material report: chemical and mechanical properties within limits.'))
        return reqs

    # ── Stainless Steel Straps ─────────────────────────────────────────
    if 'strap' in cat:
        reqs.append(ev('checklist', 'Inspection checklist must be signed and dated by inspector.'))
        reqs.append(ev('material',
            'Check material report: chemical and mechanical properties within limits.'))
        return reqs

    # ── Gaskets ────────────────────────────────────────────────────────
    if 'gasket' in cat:
        reqs.append(ev('checklist', 'Inspection checklist must be signed and dated by inspector.'))
        return reqs

    # ── Handwheels ─────────────────────────────────────────────────────
    if 'handwheel' in cat:
        reqs.append(ev('checklist', 'Inspection checklist must be signed and dated by inspector.'))
        return reqs

    # ── Covers & Lids (all CI/DI cover and lid variants) ───────────────
    if 'cover' in cat or 'lid' in cat:
        reqs.append(ev('checklist', 'Inspection checklist must be signed and dated by inspector.'))
        reqs.append(ev('material',
            'Check material report: chemical and mechanical properties within limits.\n'
            'For CI/DI products: compare to Dandong Foundry acceptable limits\n'
            'for cast iron / ductile iron (chemical composition & mechanical properties).'))
        return reqs

    return reqs


def get_vtrust_status(inspections_list):
    """Return the V-Trust result from the most recent inspection that has one."""
    for record in reversed(inspections_list):
        r = record.get('vtrust_result', '').strip()
        if r:
            return r
    return ''

def fmt_date(val):
    if val and '00:00:00' in str(val):
        return str(val).replace(' 00:00:00', '')
    return str(val) if val else ''

def make_job_key(sheet_name, row, headers):
    def col(name):
        try:
            idx = [h.lower() for h in headers].index(name.lower())
            return str(row[idx]) if idx < len(row) else ''
        except ValueError:
            return ''
    # Use Purchase Order number as the primary key — it uniquely identifies
    # a supplier order line, unlike the DPL order number which groups many lines.
    po = col('daemco purchase order')
    if not po:
        po = col('purchase order')
    if not po:
        po = col('order number')   # fallback for sheets without a PO column
    item = col('item code')
    return f"{sheet_name}|{po}|{item}"

def decrypt_excel(file_bytes, password):
    enc = io.BytesIO(file_bytes)
    try:
        office_file = msoffcrypto.OfficeFile(enc)
        if office_file.is_encrypted():
            office_file.load_key(password=password)
            dec = io.BytesIO()
            office_file.decrypt(dec)
            dec.seek(0)
            return dec
    except Exception:
        pass
    return io.BytesIO(file_bytes)

def parse_excel(file_bytes, password):
    dec = decrypt_excel(file_bytes, password)
    wb = openpyxl.load_workbook(dec, data_only=True, read_only=True)
    result = {}
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            r = [fmt_date(c) for c in row]
            if any(v.strip() for v in r):
                rows.append(r)
        result[sheet_name] = rows
    wb.close()
    return result

def _sim(a, b):
    """String similarity ratio 0–1 using SequenceMatcher."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.upper(), b.upper()).ratio()

def _qty_index(headers):
    """Return index of the Quantity column, or None."""
    for i, h in enumerate(headers):
        if h.lower() == 'quantity':
            return i
    return None

def _parse_qty(val):
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None

def compute_changes(previous, current):
    """Compare two week datasets.

    Same order+item can appear as multiple rows (split production lots) or
    as a single merged row across different weeks.  We aggregate quantities
    per job_key so splits/merges don't create false new/shipped signals.

    Returns:
        statuses   – dict  keyed by job_key → 'new' | 'not_shipped' |
                                               'partially_shipped' | 'shipped' | 'typo'
        typo_flags – list of dicts describing suspected typo pairs
        shipped_rows – dict  sheet_name → list of rows (one per unique key,
                       with qty updated to the aggregated total)
    """
    prev_keys = set()
    curr_keys = set()
    prev_qty  = {}   # job_key → aggregated float qty
    curr_qty  = {}

    # job_key → (sheet, representative_row, headers, qi)
    # Used to build shipped_rows with correct aggregated quantities.
    _prev_rep = {}

    for sheet, rows in previous.items():
        if not rows:
            continue
        headers = rows[0]
        qi = _qty_index(headers)
        for row in rows[1:]:
            k = make_job_key(sheet, row, headers)
            if not k.split('|')[1]:
                continue
            prev_keys.add(k)
            # Sum quantities across split rows
            if qi is not None and qi < len(row):
                q = _parse_qty(row[qi])
                if q is not None:
                    prev_qty[k] = (prev_qty.get(k) or 0) + q
            # Keep first row as the representative for display
            if k not in _prev_rep:
                _prev_rep[k] = (sheet, list(row), headers, qi)

    for sheet, rows in current.items():
        if not rows:
            continue
        headers = rows[0]
        qi = _qty_index(headers)
        for row in rows[1:]:
            k = make_job_key(sheet, row, headers)
            if not k.split('|')[1]:
                continue
            curr_keys.add(k)
            # Sum quantities across split rows
            if qi is not None and qi < len(row):
                q = _parse_qty(row[qi])
                if q is not None:
                    curr_qty[k] = (curr_qty.get(k) or 0) + q

    genuine_new     = curr_keys - prev_keys
    genuine_shipped = prev_keys - curr_keys

    # ── typo detection ────────────────────────────────────────────────────────
    THRESHOLD = 0.75
    typo_flags        = []
    typo_new_keys     = set()
    typo_shipped_keys = set()

    for nk in sorted(genuine_new):
        n_sheet, n_order, n_item = nk.split('|', 2)
        best, best_score = None, 0.0

        for sk in genuine_shipped:
            s_sheet, s_order, s_item = sk.split('|', 2)
            if n_sheet != s_sheet:
                continue

            order_sim = _sim(n_order, s_order)
            item_sim  = _sim(n_item,  s_item)

            if n_order == s_order and THRESHOLD <= item_sim < 1.0:
                score, reason, field = item_sim,  'Item code may have a typo',             'item_code'
            elif n_item == s_item and THRESHOLD <= order_sim < 1.0:
                score, reason, field = order_sim, 'Order number may have a typo',          'order_number'
            elif order_sim >= THRESHOLD and item_sim >= THRESHOLD:
                score, reason, field = (order_sim + item_sim) / 2, \
                                        'Order number or item code may have a typo', 'both'
            else:
                continue

            if score > best_score:
                best_score = score
                best = dict(curr_key=nk, prev_key=sk, sheet=n_sheet,
                            curr_order=n_order, curr_item=n_item,
                            prev_order=s_order, prev_item=s_item,
                            reason=reason, field=field, score=round(score * 100))

        if best:
            typo_flags.append(best)
            typo_new_keys.add(nk)
            typo_shipped_keys.add(best['prev_key'])

    # ── statuses for current-week rows ───────────────────────────────────────
    statuses = {}
    for k in curr_keys:
        if k in typo_new_keys:
            statuses[k] = 'typo'
        elif k not in prev_keys:
            statuses[k] = 'new'
        else:
            pq = prev_qty.get(k)
            cq = curr_qty.get(k)
            if pq is not None and cq is not None and cq < pq:
                statuses[k] = 'partially_shipped'
            else:
                statuses[k] = 'not_shipped'

    # ── collect shipped rows from previous week ───────────────────────────────
    # One representative row per unique key (split rows are deduplicated).
    # The quantity cell is updated to reflect the aggregated total so the
    # display and outstanding_jobs records show the correct combined quantity.
    fully_shipped_keys = genuine_shipped - typo_shipped_keys
    shipped_rows = {}   # sheet → list of row lists

    for k in sorted(fully_shipped_keys):   # sorted for determinism
        if k not in _prev_rep:
            continue
        sheet, rep_row, headers, qi = _prev_rep[k]
        # Replace qty cell with the aggregated total
        if qi is not None and qi < len(rep_row) and prev_qty.get(k) is not None:
            rep_row = list(rep_row)          # don't mutate the original
            rep_row[qi] = str(int(prev_qty[k]) if prev_qty[k] == int(prev_qty[k])
                               else prev_qty[k])
        if sheet not in shipped_rows:
            shipped_rows[sheet] = []
        shipped_rows[sheet].append(rep_row)

    # Normalise shipped rows to match current week's column structure.
    # Previous week may have different/extra columns; align each row to the
    # current headers so the template renders them in the correct columns.
    for sheet in list(shipped_rows.keys()):
        if sheet not in current or not current[sheet]:
            continue
        curr_headers = list(current[sheet][0])
        prev_headers = list(previous[sheet][0]) if previous.get(sheet) else []
        if curr_headers == prev_headers:
            continue  # identical structure, nothing to do
        prev_idx = {h: i for i, h in enumerate(prev_headers)}
        normalised = []
        for row in shipped_rows[sheet]:
            new_row = []
            for h in curr_headers:
                idx = prev_idx.get(h)
                new_row.append(row[idx] if idx is not None and idx < len(row) else '')
            normalised.append(new_row)
        shipped_rows[sheet] = normalised

    return statuses, typo_flags, shipped_rows

# ── Google API ────────────────────────────────────────────────────────────────

def get_google_services():
    if not GOOGLE_AVAILABLE or not os.path.exists(CREDENTIALS_FILE):
        return None, None
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE, scopes=SCOPES)
    sheets = build('sheets', 'v4', credentials=creds)
    drive = build('drive', 'v3', credentials=creds)
    return sheets, drive

def append_inspection_to_sheet(inspection_data, file_links):
    config = load_config()
    sheet_id = config.get('sheet_id', '')
    if not sheet_id:
        return False, 'Google Sheet ID not configured'
    sheets, _ = get_google_services()
    if not sheets:
        return False, 'Google credentials not configured'

    row = [
        inspection_data.get('job_key', ''),
        inspection_data.get('region', ''),
        inspection_data.get('order_number', ''),
        inspection_data.get('item_code', ''),
        inspection_data.get('item_description', ''),
        inspection_data.get('supplier', ''),
        inspection_data.get('quantity_ordered', ''),
        inspection_data.get('inspector_name', ''),
        inspection_data.get('inspection_date', ''),
        inspection_data.get('quantity_inspected', ''),
        inspection_data.get('result', ''),
        inspection_data.get('defects', ''),
        inspection_data.get('notes', ''),
        ', '.join(file_links),
        datetime.now().isoformat(),
    ]
    body = {'values': [row]}
    sheets.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range='Inspections!A:O',
        valueInputOption='USER_ENTERED',
        body=body,
    ).execute()
    return True, 'Saved to Google Sheets'

def upload_file_to_drive(local_path, filename, job_key):
    config = load_config()
    folder_id = config.get('drive_folder_id', '')
    _, drive = get_google_services()
    if not drive:
        return None

    # Create sub-folder for this job if needed
    safe_key = job_key.replace('|', '_').replace('/', '-')
    query = f"name='{safe_key}' and '{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    res = drive.files().list(q=query, fields='files(id,name)').execute()
    if res.get('files'):
        job_folder_id = res['files'][0]['id']
    else:
        meta = {'name': safe_key, 'mimeType': 'application/vnd.google-apps.folder',
                'parents': [folder_id]}
        job_folder_id = drive.files().create(body=meta, fields='id').execute()['id']

    file_meta = {'name': filename, 'parents': [job_folder_id]}
    media = MediaFileUpload(local_path, resumable=True)
    uploaded = drive.files().create(body=file_meta, media_body=media, fields='id,webViewLink').execute()
    # Make it viewable by anyone with link
    drive.permissions().create(
        fileId=uploaded['id'],
        body={'type': 'anyone', 'role': 'reader'},
    ).execute()
    return uploaded.get('webViewLink', '')

# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/healthz')
def healthz():
    try:
        with db_conn() as conn:
            conn.execute('SELECT 1').fetchone()
        return jsonify(status='ok'), 200
    except Exception:
        logger.exception('Health check failed')
        return jsonify(status='unhealthy'), 503


@app.route('/')
def index():
    current = load_json(CURRENT_FILE, {})
    previous = load_json(PREVIOUS_FILE, {})
    if current and previous:
        statuses, typo_flags, shipped_rows = compute_changes(previous, current)
    else:
        statuses, typo_flags, shipped_rows = {}, [], {}
    config = load_config()
    inspections = load_json(INSPECTIONS_CACHE, {})

    # Build set of job_keys that are valves (require V-Trust)
    valve_keys = set()
    all_rows_to_check = list(current.items()) + [
        (sheet, rows_list)
        for sheet, rows_list in {
            s: [list(current[s][0])] + srows
            for s, srows in shipped_rows.items()
            if s in current and current[s]
        }.items()
    ]
    for sheet_name, rows in all_rows_to_check:
        if not rows or len(rows) < 2:
            continue
        headers = rows[0]
        desc_idx = next((i for i, h in enumerate(headers) if h.lower() == 'item description'), -1)
        code_idx = next((i for i, h in enumerate(headers) if h.lower() == 'item code'), -1)
        for row in rows[1:]:
            desc = row[desc_idx] if desc_idx >= 0 and desc_idx < len(row) else ''
            code = row[code_idx] if code_idx >= 0 and code_idx < len(row) else ''
            if is_valve(desc, code, config):
                valve_keys.add(make_job_key(sheet_name, row, headers))

    # Load outstanding jobs (shipped but inspection incomplete) + completed history
    today_str = datetime.now().strftime('%Y-%m-%d')
    with db_conn() as conn:
        outstanding_jobs = conn.execute(
            'SELECT * FROM outstanding_jobs WHERE completed=0 ORDER BY est_completion ASC, shipped_at ASC'
        ).fetchall()
        completed_jobs = conn.execute(
            'SELECT * FROM outstanding_jobs WHERE completed=1 ORDER BY completed_at DESC'
        ).fetchall()
        kpi_total     = conn.execute('SELECT COUNT(*) FROM outstanding_jobs').fetchone()[0]
        kpi_done      = conn.execute('SELECT COUNT(*) FROM outstanding_jobs WHERE completed=1').fetchone()[0]
        kpi_overdue   = conn.execute(
            "SELECT COUNT(*) FROM outstanding_jobs WHERE completed=0 AND est_completion!='' AND est_completion<?",
            (today_str,)).fetchone()[0]

    # Group outstanding by sheet for display
    from collections import defaultdict as _dd
    outstanding_by_sheet = _dd(list)
    for job in outstanding_jobs:
        outstanding_by_sheet[job['sheet']].append(job)

    return render_template('index.html',
                           data=current,
                           statuses=statuses,
                           typo_flags=typo_flags,
                           shipped_rows=shipped_rows,
                           inspections=inspections,
                           valve_keys=valve_keys,
                           upload_date=config.get('upload_date', ''),
                           google_configured=bool(config.get('sheet_id') and os.path.exists(CREDENTIALS_FILE)),
                           outstanding_jobs=outstanding_jobs,
                           outstanding_by_sheet=outstanding_by_sheet,
                           completed_jobs=completed_jobs,
                           kpi_total=kpi_total,
                           kpi_done=kpi_done,
                           kpi_overdue=kpi_overdue,
                           today_str=today_str)

@app.route('/dashboard')
def dashboard():
    current     = load_json(CURRENT_FILE, {})
    previous    = load_json(PREVIOUS_FILE, {})
    config      = load_config()
    inspections = load_json(INSPECTIONS_CACHE, {})

    if current and previous:
        statuses, typo_flags, shipped_rows = compute_changes(previous, current)
    else:
        statuses, typo_flags, shipped_rows = {}, [], {}

    # Build valve_keys for current + shipped rows
    valve_keys = set()
    for sheet_name, rows in current.items():
        if not rows or len(rows) < 2:
            continue
        headers  = rows[0]
        desc_idx = next((i for i, h in enumerate(headers) if h.lower() == 'item description'), -1)
        code_idx = next((i for i, h in enumerate(headers) if h.lower() == 'item code'), -1)
        for row in rows[1:]:
            desc = row[desc_idx] if 0 <= desc_idx < len(row) else ''
            code = row[code_idx] if 0 <= code_idx < len(row) else ''
            if is_valve(desc, code, config):
                valve_keys.add(make_job_key(sheet_name, row, headers))
    for sheet_name, srows in shipped_rows.items():
        prev_rows = previous.get(sheet_name, [])
        if not prev_rows:
            continue
        headers  = prev_rows[0]
        desc_idx = next((i for i, h in enumerate(headers) if h.lower() == 'item description'), -1)
        code_idx = next((i for i, h in enumerate(headers) if h.lower() == 'item code'), -1)
        for row in srows:
            desc = row[desc_idx] if 0 <= desc_idx < len(row) else ''
            code = row[code_idx] if 0 <= code_idx < len(row) else ''
            if is_valve(desc, code, config):
                valve_keys.add(make_job_key(sheet_name, row, headers))

    def insp_result(jk):
        recs = inspections.get(jk, [])
        if not recs:
            return 'pending'
        r = recs[-1].get('result', '').lower()
        if 'fail' in r:
            return 'fail'
        if 'partial' in r:
            return 'partial'
        if 'pass' in r:
            return 'pass'
        return 'pending'

    region_stats = {}
    for sheet in current.keys():
        rows      = current.get(sheet, [])
        prev_rows = previous.get(sheet, [])
        stats = dict(total=0, new=0, not_shipped=0, partially_shipped=0,
                     shipped=0, typo=0, qa_violations=0, vtrust_violations=0,
                     insp_pass=0, insp_fail=0, insp_partial=0, insp_pending=0)

        if rows and len(rows) >= 2:
            headers = rows[0]
            qa_idx  = next((i for i, h in enumerate(headers) if 'qa brt' in h.lower()), -1)
            seen_jk = set()   # deduplicate split rows – count each order+item once
            for row in rows[1:]:
                jk = make_job_key(sheet, row, headers)
                if not jk.split('|')[1] or jk in seen_jk:
                    continue
                seen_jk.add(jk)
                status = statuses.get(jk, 'new')
                stats[status] += 1
                stats['total'] += 1
                if status in ('partially_shipped',):
                    if qa_idx >= 0 and qa_idx < len(row):
                        if str(row[qa_idx]).strip().lower() not in ('yes', 'y'):
                            stats['qa_violations'] += 1
                    if jk in valve_keys:
                        if get_vtrust_status(inspections.get(jk, [])).lower() != 'pass':
                            stats['vtrust_violations'] += 1
                stats[f'insp_{insp_result(jk)}'] += 1

        for row in shipped_rows.get(sheet, []):
            ph = prev_rows[0] if prev_rows else []
            if not ph:
                continue
            jk = make_job_key(sheet, row, ph)
            if not jk.split('|')[1]:
                continue
            stats['shipped'] += 1
            stats['total']   += 1
            qa_idx = next((i for i, h in enumerate(ph) if 'qa brt' in h.lower()), -1)
            if qa_idx >= 0 and qa_idx < len(row):
                if str(row[qa_idx]).strip().lower() not in ('yes', 'y'):
                    stats['qa_violations'] += 1
            if jk in valve_keys:
                if get_vtrust_status(inspections.get(jk, [])).lower() != 'pass':
                    stats['vtrust_violations'] += 1
            stats[f'insp_{insp_result(jk)}'] += 1

        if stats['total'] > 0:
            region_stats[sheet] = stats

    stat_keys = ['total', 'new', 'not_shipped', 'partially_shipped', 'shipped', 'typo',
                 'qa_violations', 'vtrust_violations', 'insp_pass', 'insp_fail',
                 'insp_partial', 'insp_pending']
    totals = {k: sum(r.get(k, 0) for r in region_stats.values()) for k in stat_keys}

    # Build est_completion lookup: job_key → est_completion date
    with db_conn() as conn:
        _est_rows = (
            conn.execute("SELECT job_key, est_completion FROM outstanding_jobs  WHERE est_completion!=''").fetchall() +
            conn.execute("SELECT job_key, est_completion FROM inspection_tasks WHERE est_completion!=''").fetchall()
        )
    est_map = {r['job_key']: r['est_completion'][:10] for r in _est_rows if r['job_key'] and r['est_completion']}

    # Last 8 ISO weeks (oldest → newest)
    from datetime import datetime as _dt, timedelta as _td
    from collections import defaultdict
    _today = _dt.now()
    recent_weeks = []
    for _i in range(7, -1, -1):
        _d = _today - _td(weeks=_i)
        _yr, _wk, _ = _d.isocalendar()
        recent_weeks.append(f"{_yr}-W{_wk:02d}")

    # Inspector statistics with on-time rate + weekly breakdown
    _insp_raw = defaultdict(lambda: dict(
        total=0, passed=0, failed=0, partial=0,
        on_time=0, late=0, no_est=0, last_date='',
        weekly=defaultdict(int)
    ))
    for job_key, records in inspections.items():
        est = est_map.get(job_key, '')
        for rec in records:
            name = rec.get('inspector_name', '').strip()
            if not name:
                continue
            s = _insp_raw[name]
            s['total'] += 1
            r = rec.get('result', '').lower()
            if 'fail' in r:    s['failed'] += 1
            elif 'partial' in r: s['partial'] += 1
            elif 'pass' in r:  s['passed'] += 1
            insp_date = (rec.get('inspection_date') or '')[:10]
            if insp_date and insp_date > s['last_date']:
                s['last_date'] = insp_date
            if insp_date:
                try:
                    _d2 = _dt.strptime(insp_date, '%Y-%m-%d')
                    _yr2, _wk2, _ = _d2.isocalendar()
                    s['weekly'][f"{_yr2}-W{_wk2:02d}"] += 1
                except Exception:
                    pass
            if est and insp_date:
                (s['on_time'] if insp_date <= est else s['late']).__class__  # dummy
                if insp_date <= est:
                    s['on_time'] += 1
                else:
                    s['late'] += 1
            else:
                s['no_est'] += 1

    # Serialise for template (convert inner defaultdicts)
    inspector_stats = []
    for name, s in sorted(_insp_raw.items(), key=lambda x: -x[1]['total']):
        sc = dict(s)
        sc['weekly'] = {w: s['weekly'].get(w, 0) for w in recent_weeks}
        sc['max_weekly'] = max(sc['weekly'].values()) if any(sc['weekly'].values()) else 1
        rated = sc['on_time'] + sc['late']
        sc['ontime_pct'] = round(sc['on_time'] / rated * 100) if rated else None
        inspector_stats.append((name, sc))

    prev_total       = totals['not_shipped'] + totals['partially_shipped'] + totals['shipped']
    curr_in_schedule = totals['new']         + totals['not_shipped']       + totals['partially_shipped']

    # ── Weekly region trend data for chart ───────────────────────────────
    with db_conn() as conn:
        _snap_rows = conn.execute(
            'SELECT week_label, week_date, region, total_orders '
            'FROM weekly_snapshots ORDER BY week_date, region'
        ).fetchall()

    _all_regions = sorted({r['region'] for r in _snap_rows})
    _week_order  = sorted({(r['week_date'], r['week_label']) for r in _snap_rows})
    _chart_labels   = [lbl for _, lbl in _week_order]
    _chart_date_map = {lbl: dt for dt, lbl in _week_order}

    _region_series = {}
    for region in _all_regions:
        _region_series[region] = {r['week_label']: r['total_orders']
                                   for r in _snap_rows if r['region'] == region}

    _chart_datasets = [
        {'region': r, 'data': [_region_series[r].get(lbl, None) for lbl in _chart_labels]}
        for r in _all_regions
    ]

    return render_template('dashboard.html',
                           region_stats=region_stats,
                           totals=totals,
                           typo_count=len(typo_flags),
                           upload_date=config.get('upload_date', ''),
                           inspector_stats=inspector_stats,
                           recent_weeks=recent_weeks,
                           prev_total=prev_total,
                           curr_in_schedule=curr_in_schedule,
                           chart_labels=_chart_labels,
                           chart_datasets=_chart_datasets)


@app.route('/upload', methods=['POST'])
def upload_excel():
    if 'file' not in request.files:
        flash('No file selected', 'error')
        return redirect(url_for('index'))

    f = request.files['file']
    password = request.form.get('password', EXCEL_PASSWORD)

    try:
        file_bytes = f.read()
        data = parse_excel(file_bytes, password)

        # Rotate: current → previous
        if os.path.exists(CURRENT_FILE):
            import shutil
            shutil.copy(CURRENT_FILE, PREVIOUS_FILE)

        save_json(CURRENT_FILE, data)

        # ── Detect header changes vs previous week (poka-yoke) ───────────
        prev_for_check = load_json(PREVIOUS_FILE, {})
        header_warnings = []
        for sheet, rows in data.items():
            if not rows:
                continue
            curr_hdrs = list(rows[0])
            prev_rows = prev_for_check.get(sheet)
            if not prev_rows:
                continue
            prev_hdrs = list(prev_rows[0])
            added   = [h for h in curr_hdrs if h not in prev_hdrs]
            removed = [h for h in prev_hdrs if h not in curr_hdrs]
            if added or removed:
                parts = []
                if added:   parts.append('新增列: ' + ', '.join(f'「{h}」' for h in added))
                if removed: parts.append('移除列: ' + ', '.join(f'「{h}」' for h in removed))
                header_warnings.append(f'[{sheet}] ' + '；'.join(parts))
        if header_warnings:
            flash('⚠ 列结构与上周不同（Shipped 行已自动对齐，请核实列名变更是否符合预期）：'
                  + ' | '.join(header_warnings), 'warning')

        config = load_config()
        config['upload_date'] = datetime.now().strftime('%d %b %Y %H:%M')
        save_json(CONFIG_FILE, config)

        # ── Save weekly snapshot for trend chart ─────────────────────────
        _snap_label = config['upload_date']
        _snap_date  = datetime.now().strftime('%Y-%m-%d')
        with db_conn() as conn:
            for _sheet, _rows in data.items():
                _count = max(0, len(_rows) - 1)  # subtract header row
                if _count > 0:
                    conn.execute(
                        'INSERT OR REPLACE INTO weekly_snapshots '
                        '(week_label, week_date, region, total_orders) VALUES (?,?,?,?)',
                        (_snap_label, _snap_date, _sheet, _count))

        # ── Auto-create inspection tasks + persist outstanding jobs ──────
        previous = load_json(PREVIOUS_FILE, {})
        current_data = load_json(CURRENT_FILE, {})
        if previous and current_data:
            statuses, _, newly_shipped = compute_changes(previous, current_data)

            # Persist shipped-but-incomplete rows into outstanding_jobs
            with db_conn() as conn:
                for sheet, srows in newly_shipped.items():
                    if sheet not in current_data or not current_data[sheet]:
                        continue
                    curr_hdrs = list(current_data[sheet][0])
                    hlow2 = [str(h).lower() for h in curr_hdrs]
                    qa_idx2 = next((i for i, h in enumerate(curr_hdrs)
                                    if 'qa brt' in str(h).lower()), None)
                    def _gcol2(name, row):
                        try: return str(row[hlow2.index(name.lower())])
                        except (ValueError, IndexError): return ''
                    for row in srows:
                        qa_val = str(row[qa_idx2]).strip().lower() \
                            if qa_idx2 is not None and qa_idx2 < len(row) else ''
                        if qa_val in ('yes', 'y'):
                            continue  # already done, skip
                        jk = make_job_key(sheet, row, curr_hdrs)
                        try:
                            conn.execute(
                                'INSERT OR IGNORE INTO outstanding_jobs '
                                '(job_key,sheet,headers_json,row_json,week_label,'
                                'order_number,item_code,item_desc,supplier,quantity,'
                                'est_completion,must_ship) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                (jk, sheet,
                                 json.dumps(curr_hdrs), json.dumps(list(row)),
                                 config.get('upload_date', ''),
                                 _gcol2('order number', row) or _gcol2('daemco purchase order', row),
                                 _gcol2('item code', row),
                                 _gcol2('item description', row),
                                 _gcol2('supplier', row),
                                 _gcol2('quantity', row),
                                 _gcol2('estimated completion date', row),
                                 _gcol2('must ship date', row)))
                        except Exception:
                            pass
            new_tasks_created = []
            with db_conn() as conn:
                existing_keys = {r[0] for r in conn.execute(
                    'SELECT job_key FROM inspection_tasks').fetchall()}
                for sheet, rows in current_data.items():
                    if not rows or len(rows) < 2:
                        continue
                    headers = rows[0]
                    def _col(h, row):
                        try:
                            return str(row[[x.lower() for x in h].index(h_lower)]) \
                                   if (h_lower := h.lower()) in [x.lower() for x in headers] else ''
                        except Exception:
                            return ''
                    hlow = [x.lower() for x in headers]
                    def gcol(name, row):
                        try:
                            return str(row[hlow.index(name.lower())])
                        except (ValueError, IndexError):
                            return ''
                    for row in rows[1:]:
                        jk = make_job_key(sheet, row, headers)
                        if statuses.get(jk) == 'new' and jk not in existing_keys:
                            task = dict(
                                job_key=jk, order_number=gcol('order number', row),
                                region=sheet, item_code=gcol('item code', row),
                                description=gcol('item description', row),
                                supplier=gcol('supplier', row),
                                quantity=gcol('quantity', row),
                                est_completion=gcol('estimated completion date', row),
                                must_ship=gcol('must ship date', row),
                                week_label=config.get('upload_date', ''))
                            conn.execute(
                                'INSERT OR IGNORE INTO inspection_tasks '
                                '(job_key,order_number,region,item_code,description,'
                                'supplier,quantity,est_completion,must_ship,week_label)'
                                ' VALUES (?,?,?,?,?,?,?,?,?,?)',
                                (task['job_key'], task['order_number'], task['region'],
                                 task['item_code'], task['description'], task['supplier'],
                                 task['quantity'], task['est_completion'],
                                 task['must_ship'], task['week_label']))
                            new_tasks_created.append(task)
                            existing_keys.add(jk)

            if new_tasks_created:
                ok, msg = _send_task_email(new_tasks_created)
                if ok:
                    flash(f'排期已更新，{len(new_tasks_created)} 个新任务已创建，{msg}', 'success')
                else:
                    flash(f'排期已更新，{len(new_tasks_created)} 个新任务已创建。邮件通知：{msg}', 'warning')
            else:
                flash('Schedule updated successfully!', 'success')
        else:
            flash('Schedule updated successfully!', 'success')
    except Exception as e:
        tb = traceback.format_exc()
        logger.error('Upload failed:\n%s', tb)
        _last_error['tb'] = tb
        _last_error['time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        flash(f'Error reading file: {e}', 'error')

    return redirect(url_for('index'))


@app.route('/debug')
def debug_info():
    import sys, sqlite3
    info = {
        'python': sys.version,
        'data_dir_exists': os.path.exists(DATA_DIR),
        'current_week_exists': os.path.exists(CURRENT_FILE),
        'previous_week_exists': os.path.exists(PREVIOUS_FILE),
        'db_exists': os.path.exists(os.path.join(DATA_DIR, 'app.db')),
        'last_error_time': _last_error['time'],
        'last_error': _last_error['tb'] or 'none',
    }
    try:
        with db_conn() as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            info['db_tables'] = tables
    except Exception as e:
        info['db_error'] = str(e)
    return f'<pre style="white-space:pre-wrap;font-size:13px">{json.dumps(info, indent=2, ensure_ascii=False)}</pre>'

@app.route('/inspect/<path:job_key>')
def inspect_form(job_key):
    current = load_json(CURRENT_FILE, {})
    job_info = None

    # Search current week Excel data
    for sheet_name, rows in current.items():
        if not rows:
            continue
        headers = rows[0]
        for row in rows[1:]:
            k = make_job_key(sheet_name, row, headers)
            if k == job_key:
                job_info = dict(zip(headers, row))
                job_info['region'] = sheet_name
                job_info['job_key'] = job_key
                break
        if job_info:
            break

    # Fallback: search manual orders in SQLite
    if not job_info:
        with db_conn() as conn:
            o = conn.execute(
                'SELECT o.*, s.name AS supplier_name FROM orders o '
                'LEFT JOIN suppliers s ON o.supplier_id = s.id WHERE o.id=?',
                (job_key.split('|')[0] if '|' not in job_key else -1,)
            ).fetchone()
            # Try matching by order_number + item_code
            parts = job_key.split('|')
            if len(parts) == 3:
                o = conn.execute(
                    'SELECT o.*, s.name AS supplier_name FROM orders o '
                    'LEFT JOIN suppliers s ON o.supplier_id = s.id '
                    'WHERE o.region=? AND o.order_number=? AND o.item_code=?',
                    (parts[0], parts[1], parts[2])
                ).fetchone()
            if o:
                job_info = {
                    'Order Number':   o['order_number'],
                    'Item Code':      o['item_code'],
                    'Item Description': o['description'],
                    'Supplier':       o['supplier_name'] or '',
                    'Quantity':       o['quantity'],
                    'Order Date':     o['order_date'],
                    'region':         o['region'],
                    'job_key':        job_key,
                }

    if not job_info:
        flash('Job not found', 'error')
        return redirect(url_for('index'))

    cache = load_json(INSPECTIONS_CACHE, {})
    past  = cache.get(job_key, [])
    config = load_config()
    valve_flag = is_valve(
        job_info.get('Item Description', ''),
        job_info.get('Item Code', ''),
        config
    )

    # Load defect codes grouped by category
    with db_conn() as conn:
        dc_rows = conn.execute(
            'SELECT code, name, name_cn, category FROM defect_codes ORDER BY code'
        ).fetchall()
    from collections import defaultdict
    defect_cats = defaultdict(list)
    for r in dc_rows:
        defect_cats[r['category']].append(r)
    cat_order = ['Assembly', 'Casting', 'Coating & Surface',
                 'Machining & Drilling', 'Marking & Packaging', 'Components & Materials']
    defect_groups = [(c, defect_cats[c]) for c in cat_order if c in defect_cats]

    # Match a form template by item description / code keywords
    # Also look up auto-assigned inspectors via product category config
    matched_tpl = None
    auto_inspectors = []
    matched_category = None
    desc_up = job_info.get('Item Description', '').upper()
    code_up = job_info.get('Item Code', '').strip()
    with db_conn() as conn:
        tpls = conn.execute('SELECT id, name, keywords FROM form_templates ORDER BY id').fetchall()

        # Inspector auto-assignment: item_code → product → category → category_inspectors
        if code_up:
            prod_row = conn.execute(
                'SELECT p.category_id, pc.name AS cat_name '
                'FROM products p '
                'LEFT JOIN product_categories pc ON p.category_id = pc.id '
                'WHERE UPPER(p.item_code) = UPPER(?)',
                (code_up,)
            ).fetchone()
            if prod_row and prod_row['category_id']:
                matched_category = prod_row['cat_name']
                insp_rows = conn.execute(
                    'SELECT inspector_name FROM category_inspectors WHERE category_id = ?',
                    (prod_row['category_id'],)
                ).fetchall()
                auto_inspectors = [r['inspector_name'] for r in insp_rows]

    for tpl in tpls:
        for kw in (tpl['keywords'] or '').split(','):
            kw = kw.strip().upper()
            if kw and (kw in desc_up or kw in code_up.upper()):
                matched_tpl = tpl
                break
        if matched_tpl:
            break

    evidence_reqs = get_evidence_requirements(
        job_info.get('Item Code', ''),
        matched_category or ''
    )

    # Load existing attachments for past inspections
    with db_conn() as conn:
        att_rows = conn.execute(
            'SELECT * FROM inspection_attachments WHERE job_key=? ORDER BY insp_index, id',
            (job_key,)
        ).fetchall()
    from collections import defaultdict
    past_attachments = defaultdict(list)
    for a in att_rows:
        past_attachments[a['insp_index']].append(a)

    return render_template('inspect.html',
                           job=job_info,
                           past_inspections=past,
                           past_attachments=dict(past_attachments),
                           is_valve=valve_flag,
                           defect_groups=defect_groups,
                           matched_tpl=matched_tpl,
                           auto_inspectors=auto_inspectors,
                           matched_category=matched_category,
                           evidence_reqs=evidence_reqs,
                           now_date=datetime.now().strftime('%Y-%m-%d'),
                           google_configured=bool(config.get('sheet_id') and os.path.exists(CREDENTIALS_FILE)))

@app.route('/inspect/<path:job_key>/submit', methods=['POST'])
def submit_inspection(job_key):
    form = request.form
    now_ts = datetime.now().strftime('%Y%m%d%H%M%S')

    inspection_data = {
        'job_key':           job_key,
        'region':            form.get('region', ''),
        'order_number':      form.get('order_number', ''),
        'item_code':         form.get('item_code', ''),
        'item_description':  form.get('item_description', ''),
        'supplier':          form.get('supplier', ''),
        'quantity_ordered':  form.get('quantity_ordered', ''),
        'inspector_name':    form.get('inspector_name', ''),
        'inspection_date':   form.get('inspection_date', ''),
        'quantity_inspected':form.get('quantity_inspected', ''),
        'result':            form.get('result', ''),
        'defect_codes':      request.form.getlist('defect_codes'),
        'defects':           form.get('defects', ''),
        'notes':             form.get('notes', ''),
        'submitted_at':      datetime.now().isoformat(),
    }

    # ── Evidence uploads: one file-input per evidence type ───────────────
    evidence_results = {}
    all_file_links   = []
    all_file_names   = []

    # Determine which insp_index this will be
    cache = load_json(INSPECTIONS_CACHE, {})
    insp_index = len(cache.get(job_key, []))

    # Evidence type names from form (ev_result_brt, ev_result_daq, …)
    ev_types = [k[10:] for k in form if k.startswith('ev_result_')]

    job_dir = os.path.join(UPLOAD_DIR, job_key.replace('|', '_').replace('/', '_'))
    os.makedirs(job_dir, exist_ok=True)

    with db_conn() as conn:
        for etype in ev_types:
            ev_result = form.get(f'ev_result_{etype}', '')
            ev_notes  = form.get(f'ev_notes_{etype}', '')
            ev_files  = request.files.getlist(f'ev_file_{etype}')
            evidence_results[etype] = {'result': ev_result, 'notes': ev_notes, 'files': []}

            for uploaded_file in ev_files:
                if not uploaded_file.filename:
                    continue
                orig_name  = uploaded_file.filename
                safe_name  = orig_name.replace(' ', '_')
                saved_name = f"{etype}_{now_ts}_{safe_name}"
                file_path  = os.path.join(job_dir, saved_name)
                uploaded_file.save(file_path)
                all_file_names.append(orig_name)

                drive_link = upload_file_to_drive(file_path, saved_name, job_key)
                link = drive_link or f'[local] {saved_name}'
                all_file_links.append(link)
                evidence_results[etype]['files'].append(orig_name)

                conn.execute(
                    'INSERT INTO inspection_attachments '
                    '(job_key, insp_index, evidence_type, original_name, saved_name, '
                    ' file_path, drive_link, result, notes) VALUES (?,?,?,?,?,?,?,?,?)',
                    (job_key, insp_index, etype, orig_name, saved_name,
                     file_path, drive_link or '', ev_result, ev_notes))

    inspection_data['evidence']    = evidence_results
    inspection_data['file_links']  = all_file_links
    inspection_data['file_names']  = all_file_names

    # Save to local cache
    if job_key not in cache:
        cache[job_key] = []
    cache[job_key].append(inspection_data)
    save_json(INSPECTIONS_CACHE, cache)

    # Auto-update QA BRTs Sent? → YES in current schedule
    current_sched = load_json(CURRENT_FILE, {})
    qa_updated = False
    for sheet, rows in current_sched.items():
        if not rows or len(rows) < 2:
            continue
        headers = rows[0]
        qa_idx = next((i for i, h in enumerate(headers)
                       if 'qa brt' in str(h).lower()), None)
        if qa_idx is None:
            continue
        for row in rows[1:]:
            if make_job_key(sheet, row, headers) == job_key:
                row[qa_idx] = 'YES'
                qa_updated = True
                break
        if qa_updated:
            break
    if qa_updated:
        save_json(CURRENT_FILE, current_sched)

    # Mark outstanding_jobs entry as completed
    with db_conn() as conn:
        conn.execute(
            'UPDATE outstanding_jobs SET completed=1, completed_at=? WHERE job_key=? AND completed=0',
            (datetime.now().isoformat(), job_key))

    # Try Google Sheets
    ok, msg = append_inspection_to_sheet(inspection_data, file_links)

    if ok:
        flash('Inspection saved to Google Sheets!', 'success')
    else:
        flash(f'Inspection saved locally. Google Sheets: {msg}', 'warning')

    return redirect(url_for('inspect_form', job_key=job_key))

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    config = load_config()
    if request.method == 'POST':
        config['sheet_id'] = request.form.get('sheet_id', '').strip()
        config['drive_folder_id'] = request.form.get('drive_folder_id', '').strip()
        raw_prefixes = request.form.get('valve_prefixes', 'RSV')
        config['valve_prefixes'] = [p.strip().upper() for p in raw_prefixes.split(',') if p.strip()]
        config['smtp_host'] = request.form.get('smtp_host', '').strip()
        config['smtp_port'] = request.form.get('smtp_port', '587').strip()
        config['smtp_user'] = request.form.get('smtp_user', '').strip()
        if request.form.get('smtp_pass', '').strip():
            config['smtp_pass'] = request.form.get('smtp_pass', '').strip()
        save_json(CONFIG_FILE, config)

        cred_file = request.files.get('credentials')
        if cred_file and cred_file.filename:
            cred_file.save(CREDENTIALS_FILE)

        flash('Settings saved!', 'success')
        return redirect(url_for('settings'))

    return render_template('settings.html',
                           config=config,
                           credentials_exist=os.path.exists(CREDENTIALS_FILE))

@app.route('/settings/office-locations/add', methods=['POST'])
def office_location_add():
    f = request.form
    name = f.get('loc_name', '').strip()
    try:
        lat = float(f.get('lat', ''))
        lng = float(f.get('lng', ''))
        radius = int(f.get('radius', 500) or 500)
    except (ValueError, TypeError):
        flash('经纬度格式错误', 'error')
        return redirect(url_for('settings'))
    if not name:
        flash('地点名称不能为空', 'error')
        return redirect(url_for('settings'))
    config = load_config()
    config.setdefault('office_locations', []).append(
        {'name': name, 'lat': lat, 'lng': lng, 'radius': radius})
    save_json(CONFIG_FILE, config)
    flash(f'打卡地点「{name}」已添加', 'success')
    return redirect(url_for('settings'))

@app.route('/settings/office-locations/<int:idx>/delete', methods=['POST'])
def office_location_delete(idx):
    config = load_config()
    locs = config.get('office_locations', [])
    if 0 <= idx < len(locs):
        removed = locs.pop(idx)
        save_json(CONFIG_FILE, config)
        flash(f'地点「{removed["name"]}」已删除', 'success')
    return redirect(url_for('settings'))

@app.route('/api/inspections/<path:job_key>')
def api_inspections(job_key):
    cache = load_json(INSPECTIONS_CACHE, {})
    return jsonify(cache.get(job_key, []))


# ── Supplier routes ───────────────────────────────────────────────────────────

@app.route('/suppliers')
def suppliers():
    with db_conn() as conn:
        rows = conn.execute('SELECT * FROM suppliers ORDER BY name').fetchall()
    return render_template('suppliers.html', suppliers=rows)

@app.route('/suppliers/new', methods=['GET', 'POST'])
def supplier_new():
    if request.method == 'POST':
        f = request.form
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO suppliers (name,contact_person,phone,email,address,country,notes)'
                ' VALUES (?,?,?,?,?,?,?)',
                (f.get('name','').strip(), f.get('contact_person','').strip(),
                 f.get('phone','').strip(), f.get('email','').strip(),
                 f.get('address','').strip(), f.get('country','China').strip(),
                 f.get('notes','').strip()))
        flash('供应商已添加', 'success')
        return redirect(url_for('suppliers'))
    return render_template('supplier_form.html', supplier=None, title='新增供应商')

@app.route('/suppliers/<int:sid>/edit', methods=['GET', 'POST'])
def supplier_edit(sid):
    with db_conn() as conn:
        supplier = conn.execute('SELECT * FROM suppliers WHERE id=?', (sid,)).fetchone()
        if not supplier:
            flash('找不到该供应商', 'error')
            return redirect(url_for('suppliers'))
        if request.method == 'POST':
            f = request.form
            conn.execute(
                'UPDATE suppliers SET name=?,contact_person=?,phone=?,email=?,'
                'address=?,country=?,notes=? WHERE id=?',
                (f.get('name','').strip(), f.get('contact_person','').strip(),
                 f.get('phone','').strip(), f.get('email','').strip(),
                 f.get('address','').strip(), f.get('country','China').strip(),
                 f.get('notes','').strip(), sid))
            flash('供应商已更新', 'success')
            return redirect(url_for('suppliers'))
    return render_template('supplier_form.html', supplier=supplier, title='编辑供应商')

@app.route('/suppliers/import-from-schedule', methods=['POST'])
def suppliers_import():
    current = load_json(CURRENT_FILE, {})
    found = set()
    for sheet, rows in current.items():
        if not rows or len(rows) < 2:
            continue
        hlow = [str(h).strip().lower() for h in rows[0]]
        try:
            sidx = hlow.index('supplier')
        except ValueError:
            continue
        for row in rows[1:]:
            v = str(row[sidx]).strip() if sidx < len(row) else ''
            if v and v.lower() not in ('none', '', 'nan'):
                found.add(v)

    added = 0
    with db_conn() as conn:
        existing = {r[0].upper() for r in conn.execute('SELECT name FROM suppliers').fetchall()}
        for name in sorted(found):
            if name.upper() not in existing:
                conn.execute('INSERT INTO suppliers (name) VALUES (?)', (name,))
                added += 1

    if added:
        flash(f'从排期导入 {added} 个新供应商 / Imported {added} new suppliers from schedule.', 'success')
    else:
        flash('All suppliers from the schedule are already in the list.', 'warning')
    return redirect(url_for('suppliers'))


@app.route('/suppliers/<int:sid>/delete', methods=['POST'])
def supplier_delete(sid):
    with db_conn() as conn:
        conn.execute('DELETE FROM suppliers WHERE id=?', (sid,))
    flash('供应商已删除', 'success')
    return redirect(url_for('suppliers'))


# ── Product routes ────────────────────────────────────────────────────────────

@app.route('/products')
def products():
    tab    = request.args.get('tab', 'categories')
    search = request.args.get('q', '').strip()
    cat_id = request.args.get('cat_id', '').strip()
    with db_conn() as conn:
        cats     = conn.execute('SELECT * FROM product_categories ORDER BY name').fetchall()
        insp_map = {
            c['id']: conn.execute(
                'SELECT * FROM category_inspectors WHERE category_id=?', (c['id'],)
            ).fetchall()
            for c in cats
        }
        where_parts = []
        params = []
        if search:
            where_parts.append('(UPPER(p.item_code) LIKE ? OR UPPER(p.name) LIKE ?)')
            like = '%' + search.upper() + '%'
            params += [like, like]
        if cat_id:
            where_parts.append('p.category_id = ?')
            params.append(cat_id)
        where_clause = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''
        items = conn.execute(f'''
            SELECT p.*, pc.name AS category_name, s.name AS supplier_name
            FROM products p
            LEFT JOIN product_categories pc ON p.category_id = pc.id
            LEFT JOIN suppliers           s  ON p.supplier_id = s.id
            {where_clause}
            ORDER BY p.category_id, p.item_code
        ''', params).fetchall()
        supplier_opts = conn.execute('SELECT id, name FROM suppliers ORDER BY name').fetchall()
        all_employees = conn.execute(
            "SELECT id, name, role FROM employees WHERE active=1 ORDER BY name"
        ).fetchall()
    return render_template('products.html', tab=tab, cats=cats, insp_map=insp_map,
                           items=items, supplier_opts=supplier_opts,
                           search=search, cat_id=cat_id,
                           all_employees=all_employees)

@app.route('/products/categories/new', methods=['POST'])
def category_new():
    f = request.form
    name = f.get('name', '').strip()
    if name:
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO product_categories (code,name,description) VALUES (?,?,?)',
                (f.get('code','').strip().upper(), name, f.get('description','').strip()))
        flash(f'类别「{name}」已添加', 'success')
    return redirect(url_for('products', tab='categories'))

@app.route('/products/categories/<int:cid>/delete', methods=['POST'])
def category_delete(cid):
    with db_conn() as conn:
        conn.execute('DELETE FROM product_categories WHERE id=?', (cid,))
    flash('类别已删除', 'success')
    return redirect(url_for('products', tab='categories'))

@app.route('/products/categories/<int:cid>/inspector/add', methods=['POST'])
def inspector_add(cid):
    name = request.form.get('inspector_name', '').strip()
    if name:
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO category_inspectors (category_id,inspector_name) VALUES (?,?)',
                (cid, name))
        flash('检验员已添加', 'success')
    return redirect(url_for('products', tab='inspector_map'))

@app.route('/products/categories/inspector/<int:iid>/delete', methods=['POST'])
def inspector_delete(iid):
    with db_conn() as conn:
        conn.execute('DELETE FROM category_inspectors WHERE id=?', (iid,))
    flash('检验员已移除', 'success')
    return redirect(url_for('products', tab='inspector_map'))

@app.route('/products/items/new', methods=['GET', 'POST'])
def product_new():
    if request.method == 'POST':
        image_path = _save_product_image(request.files.get('image'))
        f = request.form
        def _num(key):
            v = f.get(key, '').strip()
            try: return float(v) if v else None
            except ValueError: return None
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO products (category_id,supplier_id,item_code,name,description,image_path,'
                'sub_category,crate_qty,inspect_pcs,inspect_mins)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?)',
                (f.get('category_id') or None, f.get('supplier_id') or None,
                 f.get('item_code','').strip(), f.get('name','').strip(),
                 f.get('description','').strip(), image_path,
                 f.get('sub_category','').strip(),
                 int(_num('crate_qty')) if _num('crate_qty') is not None else None,
                 f.get('inspect_pcs','').strip(),
                 _num('inspect_mins')))
        flash('产品已添加', 'success')
        return redirect(url_for('products', tab='archive'))
    with db_conn() as conn:
        cats      = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
        sup_opts  = conn.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
    return render_template('product_form.html', product=None, cats=cats,
                           sup_opts=sup_opts, title='新增产品')

@app.route('/products/items/<int:pid>/edit', methods=['GET', 'POST'])
def product_edit(pid):
    with db_conn() as conn:
        product = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    if not product:
        flash('找不到该产品', 'error')
        return redirect(url_for('products', tab='archive'))
    if request.method == 'POST':
        img = request.files.get('image')
        image_path = _save_product_image(img) if (img and img.filename) else product['image_path']
        f = request.form
        def _num(key):
            v = f.get(key, '').strip()
            try: return float(v) if v else None
            except ValueError: return None
        with db_conn() as conn:
            conn.execute(
                'UPDATE products SET category_id=?,supplier_id=?,item_code=?,'
                'name=?,description=?,image_path=?,'
                'sub_category=?,crate_qty=?,inspect_pcs=?,inspect_mins=? WHERE id=?',
                (f.get('category_id') or None, f.get('supplier_id') or None,
                 f.get('item_code','').strip(), f.get('name','').strip(),
                 f.get('description','').strip(), image_path,
                 f.get('sub_category','').strip(),
                 int(_num('crate_qty')) if _num('crate_qty') is not None else None,
                 f.get('inspect_pcs','').strip(),
                 _num('inspect_mins'), pid))
        flash('产品已更新', 'success')
        return redirect(url_for('products', tab='archive'))
    with db_conn() as conn:
        cats     = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
        sup_opts = conn.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
    return render_template('product_form.html', product=product, cats=cats,
                           sup_opts=sup_opts, title='编辑产品')

@app.route('/products/items/<int:pid>/delete', methods=['POST'])
def product_delete(pid):
    with db_conn() as conn:
        conn.execute('DELETE FROM products WHERE id=?', (pid,))
    flash('产品已删除', 'success')
    return redirect(url_for('products', tab='archive'))


# ── Order routes ──────────────────────────────────────────────────────────────

@app.route('/orders')
def orders():
    status_filter = request.args.get('status', '')
    with db_conn() as conn:
        q = '''SELECT o.*, s.name AS supplier_name, pc.name AS category_name
               FROM orders o
               LEFT JOIN suppliers s ON o.supplier_id = s.id
               LEFT JOIN product_categories pc ON o.category_id = pc.id'''
        if status_filter:
            rows = conn.execute(q + ' WHERE o.status=? ORDER BY o.created_at DESC',
                                (status_filter,)).fetchall()
        else:
            rows = conn.execute(q + ' ORDER BY o.created_at DESC').fetchall()
    inspections = load_json(INSPECTIONS_CACHE, {})
    return render_template('orders.html', orders=rows, inspections=inspections,
                           status_filter=status_filter)

@app.route('/orders/new', methods=['GET', 'POST'])
def order_new():
    if request.method == 'POST':
        f = request.form
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO orders (order_number,region,customer,supplier_id,category_id,'
                'item_code,description,quantity,status,order_date,eta,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (f.get('order_number','').strip(), f.get('region','').strip(),
                 f.get('customer','').strip(), f.get('supplier_id') or None,
                 f.get('category_id') or None, f.get('item_code','').strip(),
                 f.get('description','').strip(), f.get('quantity') or 0,
                 f.get('status','Pending'), f.get('order_date','').strip(),
                 f.get('eta','').strip(), f.get('notes','').strip()))
        flash('订单已添加', 'success')
        return redirect(url_for('orders'))
    with db_conn() as conn:
        suppliers = conn.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
        cats      = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
    return render_template('order_form.html', order=None, suppliers=suppliers,
                           cats=cats, title='新增订单')

@app.route('/orders/<int:oid>/edit', methods=['GET', 'POST'])
def order_edit(oid):
    with db_conn() as conn:
        order = conn.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if not order:
        flash('找不到该订单', 'error')
        return redirect(url_for('orders'))
    if request.method == 'POST':
        f = request.form
        with db_conn() as conn:
            conn.execute(
                'UPDATE orders SET order_number=?,region=?,customer=?,supplier_id=?,category_id=?,'
                'item_code=?,description=?,quantity=?,status=?,order_date=?,eta=?,notes=? WHERE id=?',
                (f.get('order_number','').strip(), f.get('region','').strip(),
                 f.get('customer','').strip(), f.get('supplier_id') or None,
                 f.get('category_id') or None, f.get('item_code','').strip(),
                 f.get('description','').strip(), f.get('quantity') or 0,
                 f.get('status','Pending'), f.get('order_date','').strip(),
                 f.get('eta','').strip(), f.get('notes','').strip(), oid))
        flash('订单已更新', 'success')
        return redirect(url_for('orders'))
    with db_conn() as conn:
        suppliers = conn.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
        cats      = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
    return render_template('order_form.html', order=order, suppliers=suppliers,
                           cats=cats, title='编辑订单')

@app.route('/orders/<int:oid>/delete', methods=['POST'])
def order_delete(oid):
    with db_conn() as conn:
        conn.execute('DELETE FROM orders WHERE id=?', (oid,))
    flash('订单已删除', 'success')
    return redirect(url_for('orders'))

@app.route('/static/product_images/<path:filename>')
def product_image(filename):
    return send_from_directory(PRODUCT_IMG_DIR, filename)

def _send_task_email(new_tasks):
    """Send inspection task notification email to all active employees."""
    config = load_config()
    host = config.get('smtp_host', '').strip()
    port = int(config.get('smtp_port', 587) or 587)
    user = config.get('smtp_user', '').strip()
    pwd  = config.get('smtp_pass', '').strip()
    if not all([host, user, pwd]):
        return False, 'SMTP not configured'

    with db_conn() as conn:
        employees = conn.execute(
            "SELECT name, email FROM employees WHERE active=1 AND email != ''"
        ).fetchall()
    if not employees:
        return False, 'No active employees with email addresses'

    recipients = [e['email'] for e in employees]
    lines = [
        f"生产排期已更新 — {datetime.now().strftime('%Y-%m-%d')}",
        f"本次新增 {len(new_tasks)} 个检验任务，请尽快安排检验：",
        '',
    ]
    for i, t in enumerate(new_tasks, 1):
        lines.append(f"{i}. [{t['region']}]  {t['order_number']}  {t['item_code']}")
        lines.append(f"   描述: {t['description']}    数量: {t['quantity']}")
        if t.get('est_completion'):
            lines.append(f"   预计完工: {t['est_completion']}")
        lines.append('')
    lines.append('请登录检验系统查看任务详情。')

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    msg = MIMEMultipart()
    msg['Subject'] = f"【新检验任务 {len(new_tasks)} 项】{datetime.now().strftime('%Y-%m-%d')}"
    msg['From']    = user
    msg['To']      = ', '.join(recipients)
    msg.attach(MIMEText('\n'.join(lines), 'plain', 'utf-8'))
    try:
        with smtplib.SMTP(host, port, timeout=10) as srv:
            srv.ehlo(); srv.starttls(); srv.login(user, pwd)
            srv.sendmail(user, recipients, msg.as_string())
        return True, f'邮件已发送给 {len(recipients)} 名员工'
    except Exception as e:
        return False, str(e)


# ── Employee routes ───────────────────────────────────────────────────────────

@app.route('/employees')
def employees():
    with db_conn() as conn:
        rows = conn.execute('SELECT * FROM employees ORDER BY name').fetchall()
    return render_template('employees.html', employees=rows)

@app.route('/employees/new', methods=['GET', 'POST'])
def employee_new():
    if request.method == 'POST':
        f = request.form
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO employees (name,gender,mobile,email,phone,department,role,stationed_at,is_stationed,active) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (f.get('name','').strip(), f.get('gender','').strip(),
                 f.get('mobile','').strip(), f.get('email','').strip(),
                 f.get('phone','').strip(), f.get('department','').strip(),
                 f.get('role','Inspector').strip(), f.get('stationed_at','').strip(),
                 1 if f.get('is_stationed') else 0, 1 if f.get('active') else 0))
        flash('员工已添加', 'success')
        return redirect(url_for('employees'))
    with db_conn() as conn:
        cats = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
    return render_template('employee_form.html', employee=None, work_info=None,
                           all_employees=[], cats=cats, title='新增员工')

@app.route('/employees/<int:eid>/edit', methods=['GET', 'POST'])
def employee_edit(eid):
    with db_conn() as conn:
        emp = conn.execute('SELECT * FROM employees WHERE id=?', (eid,)).fetchone()
    if not emp:
        flash('找不到该员工', 'error')
        return redirect(url_for('employees'))
    if request.method == 'POST':
        f = request.form
        with db_conn() as conn:
            conn.execute(
                'UPDATE employees SET name=?,gender=?,mobile=?,email=?,phone=?,department=?,role=?,stationed_at=?,is_stationed=?,active=? WHERE id=?',
                (f.get('name','').strip(), f.get('gender','').strip(),
                 f.get('mobile','').strip(), f.get('email','').strip(),
                 f.get('phone','').strip(), f.get('department','').strip(),
                 f.get('role','Inspector').strip(), f.get('stationed_at','').strip(),
                 1 if f.get('is_stationed') else 0, 1 if f.get('active') else 0, eid))
        flash('员工信息已更新', 'success')
        return redirect(url_for('employees'))
    with db_conn() as conn:
        work_info = conn.execute('SELECT * FROM employee_work_info WHERE employee_id=?', (eid,)).fetchone()
        all_employees = conn.execute('SELECT id,name FROM employees WHERE active=1 AND id!=? ORDER BY name', (eid,)).fetchall()
        cats = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
    return render_template('employee_form.html', employee=emp, work_info=work_info,
                           all_employees=all_employees, cats=cats, title='编辑员工')

@app.route('/employees/<int:eid>/delete', methods=['POST'])
def employee_delete(eid):
    with db_conn() as conn:
        conn.execute('DELETE FROM employees WHERE id=?', (eid,))
    flash('员工已删除', 'success')
    return redirect(url_for('employees'))


# ── Task routes ───────────────────────────────────────────────────────────────

@app.route('/tasks')
def tasks():
    from collections import defaultdict
    from datetime import timedelta
    status_filter = request.args.get('status', '')
    today = datetime.now().date()
    today_str = today.isoformat()

    with db_conn() as conn:
        all_tasks = conn.execute(
            'SELECT * FROM inspection_tasks ORDER BY est_completion ASC, created_at ASC'
        ).fetchall()

    inspections = load_json(INSPECTIONS_CACHE, {})

    # Filtered view for the table
    if status_filter:
        rows = [t for t in all_tasks if t['status'] == status_filter]
    else:
        rows = all_tasks

    # ── Stats (always over ALL tasks) ─────────────────────────────────────
    total = len(all_tasks)

    # Status counts
    status_counts = defaultdict(int)
    for t in all_tasks:
        status_counts[t['status'] or 'Pending'] += 1

    # Urgency counts (non-completed only)
    urg = defaultdict(int)
    for t in all_tasks:
        if t['status'] == 'Completed':
            continue
        est = (t['est_completion'] or '')[:10]
        if not est:
            urg['no_date'] += 1
        elif est < today_str:
            urg['overdue'] += 1
        else:
            delta = (datetime.strptime(est, '%Y-%m-%d').date() - today).days
            if delta <= 7:
                urg['week'] += 1
            elif delta <= 14:
                urg['fortnight'] += 1
            else:
                urg['ok'] += 1

    # By region
    region_data = defaultdict(lambda: {'total': 0, 'completed': 0, 'overdue': 0})
    for t in all_tasks:
        r = (t['region'] or 'Unknown').strip() or 'Unknown'
        region_data[r]['total'] += 1
        if t['status'] == 'Completed':
            region_data[r]['completed'] += 1
        est = (t['est_completion'] or '')[:10]
        if est and est < today_str and t['status'] != 'Completed':
            region_data[r]['overdue'] += 1
    region_stats = sorted(region_data.items(), key=lambda x: x[1]['total'], reverse=True)
    max_region = max((v['total'] for _, v in region_stats), default=1)

    # By supplier (outstanding only, top 8)
    sup_data = defaultdict(lambda: {'outstanding': 0, 'completed': 0})
    for t in all_tasks:
        s = (t['supplier'] or 'Unknown').strip() or 'Unknown'
        if t['status'] == 'Completed':
            sup_data[s]['completed'] += 1
        else:
            sup_data[s]['outstanding'] += 1
    top_suppliers = sorted(sup_data.items(), key=lambda x: x[1]['outstanding'], reverse=True)[:8]
    max_sup = max((v['outstanding'] for _, v in top_suppliers), default=1)

    # Inspection pass/fail
    pass_ct = fail_ct = 0
    for insp_list in inspections.values():
        if insp_list:
            r = (insp_list[-1].get('result') or '')
            if r == 'Pass':
                pass_ct += 1
            elif r == 'Fail':
                fail_ct += 1

    stats = dict(
        total=total,
        status_counts=dict(status_counts),
        urg=dict(urg),
        region_stats=region_stats,
        max_region=max_region,
        top_suppliers=top_suppliers,
        max_sup=max_sup,
        pass_ct=pass_ct,
        fail_ct=fail_ct,
    )

    return render_template('tasks.html', tasks=rows, inspections=inspections,
                           today=today, status_filter=status_filter, stats=stats)

@app.route('/attachments/<int:aid>')
def serve_attachment(aid):
    with db_conn() as conn:
        row = conn.execute(
            'SELECT * FROM inspection_attachments WHERE id=?', (aid,)
        ).fetchone()
    if not row or not os.path.exists(row['file_path']):
        return 'File not found', 404
    return send_from_directory(
        os.path.dirname(row['file_path']),
        os.path.basename(row['file_path']),
        as_attachment=False,
        download_name=row['original_name']
    )


@app.route('/tasks/<int:tid>/status', methods=['POST'])
def task_status_update(tid):
    new_status = request.form.get('status', 'Pending')
    with db_conn() as conn:
        conn.execute('UPDATE inspection_tasks SET status=? WHERE id=?', (new_status, tid))
    return redirect(request.referrer or url_for('tasks'))


def _save_product_image(file_obj):
    if not file_obj or not file_obj.filename:
        return ''
    os.makedirs(PRODUCT_IMG_DIR, exist_ok=True)
    safe = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{file_obj.filename.replace(' ', '_')}"
    file_obj.save(os.path.join(PRODUCT_IMG_DIR, safe))
    return safe


# ── Form template management ─────────────────────────────────────────────────

@app.route('/forms')
def form_templates_page():
    with db_conn() as conn:
        tpls = conn.execute('SELECT * FROM form_templates ORDER BY name').fetchall()
    return render_template('forms.html', templates=tpls)

@app.route('/forms/<int:fid>/keywords', methods=['POST'])
def form_keywords_update(fid):
    kw = request.form.get('keywords', '').strip()
    with db_conn() as conn:
        conn.execute('UPDATE form_templates SET keywords=? WHERE id=?', (kw, fid))
    flash('关键词已更新', 'success')
    return redirect(url_for('form_templates_page'))

@app.route('/forms/<int:fid>/delete', methods=['POST'])
def form_template_delete(fid):
    with db_conn() as conn:
        conn.execute('DELETE FROM form_templates WHERE id=?', (fid,))
    flash('模板已删除', 'success')
    return redirect(url_for('form_templates_page'))


# ── Checklist inspection ──────────────────────────────────────────────────────

@app.route('/inspect/<path:job_key>/checklist', methods=['GET', 'POST'])
def inspect_checklist(job_key):
    tpl_id = request.args.get('tpl') or request.form.get('tpl_id')

    with db_conn() as conn:
        tpl = conn.execute('SELECT * FROM form_templates WHERE id=?', (tpl_id,)).fetchone() if tpl_id else None
        past_responses = conn.execute(
            'SELECT * FROM form_responses WHERE job_key=? ORDER BY submitted_at DESC', (job_key,)
        ).fetchall()

    # Load job info (same logic as inspect_form)
    current  = load_json(CURRENT_FILE, {})
    job_info = None
    for sheet_name, rows in current.items():
        if not rows: continue
        headers = rows[0]
        for row in rows[1:]:
            k = make_job_key(sheet_name, row, headers)
            if k == job_key:
                job_info = dict(zip(headers, row))
                job_info['region']  = sheet_name
                job_info['job_key'] = job_key
                break
        if job_info: break

    if not job_info:
        # Try SQLite orders
        parts = job_key.split('|')
        if len(parts) == 3:
            with db_conn() as conn:
                o = conn.execute(
                    'SELECT o.*, s.name AS supplier_name FROM orders o '
                    'LEFT JOIN suppliers s ON o.supplier_id=s.id '
                    'WHERE o.region=? AND o.order_number=? AND o.item_code=?',
                    (parts[0], parts[1], parts[2])).fetchone()
            if o:
                job_info = {'Order Number': o['order_number'], 'Item Code': o['item_code'],
                            'Item Description': o['description'], 'Supplier': o['supplier_name'] or '',
                            'Quantity': o['quantity'], 'region': o['region'], 'job_key': job_key}

    if not job_info:
        flash('Job not found', 'error'); return redirect(url_for('index'))

    if not tpl:
        flash('没有匹配的检验表单', 'error'); return redirect(url_for('inspect_form', job_key=job_key))

    sections = json.loads(tpl['sections_json'] or '[]')

    if request.method == 'POST':
        f = request.form
        answers = {}
        for sect_idx, sect in enumerate(sections):
            for q_idx, q in enumerate(sect['questions']):
                key = f'{sect_idx}_{q_idx}'
                answers[key] = {
                    'result': f.get(f'ans_{key}', ''),
                    'notes':  f.get(f'note_{key}', '').strip(),
                }

        overall = f.get('overall', '')
        summary = f.get('summary', '').strip()

        with db_conn() as conn:
            conn.execute(
                'INSERT INTO form_responses '
                '(job_key,template_id,template_name,dpl_number,inspector,insp_date,answers,overall,summary)'
                ' VALUES (?,?,?,?,?,?,?,?,?)',
                (job_key, tpl['id'], tpl['name'],
                 f.get('dpl_number','').strip(), f.get('inspector','').strip(),
                 f.get('insp_date','').strip(),
                 json.dumps(answers, ensure_ascii=False), overall, summary))

            # Sync to inspection_tasks
            conn.execute(
                "UPDATE inspection_tasks SET status=? WHERE job_key=?",
                ('Completed' if overall == 'Pass' else 'In Progress', job_key))

        flash('检验单已提交', 'success')
        return redirect(url_for('inspect_checklist', job_key=job_key, tpl=tpl_id))

    return render_template('form_checklist.html',
                           job=job_info, tpl=tpl, sections=sections,
                           past_responses=past_responses,
                           now_date=datetime.now().strftime('%Y-%m-%d'))


# ── Knowledge Base routes ─────────────────────────────────────────────────────

@app.route('/knowledge')
def knowledge():
    cat_filter = request.args.get('cat', '')
    search     = request.args.get('q', '').strip()
    with db_conn() as conn:
        cats = conn.execute('SELECT * FROM kb_categories ORDER BY name').fetchall()
        q = 'SELECT a.*, kc.name AS cat_name FROM kb_articles a LEFT JOIN kb_categories kc ON a.category_id=kc.id'
        params = []
        where  = []
        if cat_filter:
            where.append('a.category_id=?'); params.append(cat_filter)
        if search:
            where.append('(a.title LIKE ? OR a.content LIKE ? OR a.tags LIKE ?)')
            params += [f'%{search}%', f'%{search}%', f'%{search}%']
        if where:
            q += ' WHERE ' + ' AND '.join(where)
        q += ' ORDER BY a.tags, a.title'
        articles = conn.execute(q, params).fetchall()
    # Parse images JSON for each article so templates can use them directly
    articles_data = []
    for a in articles:
        imgs = json.loads(a['images'] or '[]') if a['images'] else []
        articles_data.append({'row': a, 'first_img': imgs[0] if imgs else '', 'img_count': len(imgs)})
    return render_template('knowledge.html', cats=cats, articles=articles_data,
                           cat_filter=cat_filter, search=search)

@app.route('/knowledge/article/<int:aid>')
def kb_article(aid):
    with db_conn() as conn:
        article = conn.execute(
            'SELECT a.*, kc.name AS cat_name FROM kb_articles a '
            'LEFT JOIN kb_categories kc ON a.category_id=kc.id WHERE a.id=?', (aid,)
        ).fetchone()
        related_qs = conn.execute(
            'SELECT * FROM questions WHERE article_id=?', (aid,)
        ).fetchall()
    if not article:
        flash('找不到该文章', 'error'); return redirect(url_for('knowledge'))
    images = json.loads(article['images'] or '[]') if article['images'] else []
    return render_template('kb_article.html', article=article, related_qs=related_qs, images=images)

@app.route('/knowledge/article/new', methods=['GET', 'POST'])
def kb_article_new():
    with db_conn() as conn:
        cats = conn.execute('SELECT * FROM kb_categories ORDER BY name').fetchall()
    if request.method == 'POST':
        f = request.form
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO kb_articles (category_id,title,content,tags) VALUES (?,?,?,?)',
                (f.get('category_id') or None, f.get('title','').strip(),
                 f.get('content','').strip(), f.get('tags','').strip()))
        flash('文章已添加', 'success'); return redirect(url_for('knowledge'))
    return render_template('kb_form.html', article=None, cats=cats, title='新增知识库文章')

@app.route('/knowledge/article/<int:aid>/edit', methods=['GET', 'POST'])
def kb_article_edit(aid):
    with db_conn() as conn:
        article = conn.execute('SELECT * FROM kb_articles WHERE id=?', (aid,)).fetchone()
        cats    = conn.execute('SELECT * FROM kb_categories ORDER BY name').fetchall()
    if not article:
        flash('找不到该文章', 'error'); return redirect(url_for('knowledge'))
    if request.method == 'POST':
        f = request.form
        with db_conn() as conn:
            conn.execute(
                'UPDATE kb_articles SET category_id=?,title=?,content=?,tags=?,'
                'updated_at=datetime("now","localtime") WHERE id=?',
                (f.get('category_id') or None, f.get('title','').strip(),
                 f.get('content','').strip(), f.get('tags','').strip(), aid))
        flash('文章已更新', 'success'); return redirect(url_for('kb_article', aid=aid))
    return render_template('kb_form.html', article=article, cats=cats, title='编辑文章')

@app.route('/knowledge/article/<int:aid>/delete', methods=['POST'])
def kb_article_delete(aid):
    with db_conn() as conn:
        conn.execute('DELETE FROM kb_articles WHERE id=?', (aid,))
    flash('文章已删除', 'success'); return redirect(url_for('knowledge'))

@app.route('/knowledge/categories/new', methods=['POST'])
def kb_category_new():
    name = request.form.get('name','').strip()
    if name:
        with db_conn() as conn:
            conn.execute('INSERT INTO kb_categories (name,description) VALUES (?,?)',
                         (name, request.form.get('description','').strip()))
        flash(f'类别「{name}」已添加', 'success')
    return redirect(url_for('knowledge'))

@app.route('/knowledge/categories/<int:cid>/delete', methods=['POST'])
def kb_category_delete(cid):
    with db_conn() as conn:
        conn.execute('DELETE FROM kb_categories WHERE id=?', (cid,))
    flash('类别已删除', 'success'); return redirect(url_for('knowledge'))


# ── Training (questions / exams / plans) ──────────────────────────────────────

@app.route('/training')
def training():
    tab = request.args.get('tab', 'plans')
    with db_conn() as conn:
        plans = conn.execute(
            'SELECT tp.*, e.title AS exam_title FROM training_plans tp '
            'LEFT JOIN exams e ON tp.exam_id=e.id ORDER BY tp.created_at DESC').fetchall()
        questions = conn.execute(
            'SELECT q.*, kc.name AS cat_name FROM questions q '
            'LEFT JOIN kb_categories kc ON q.category_id=kc.id ORDER BY q.category_id, q.id').fetchall()
        exams = conn.execute('SELECT * FROM exams ORDER BY created_at DESC').fetchall()
        cats  = conn.execute('SELECT * FROM kb_categories ORDER BY name').fetchall()
        exam_q_counts = {
            e['id']: conn.execute('SELECT COUNT(*) FROM exam_questions WHERE exam_id=?', (e['id'],)).fetchone()[0]
            for e in exams}
    return render_template('training.html', tab=tab, plans=plans, questions=questions,
                           exams=exams, cats=cats, exam_q_counts=exam_q_counts)

@app.route('/training/questions/new', methods=['POST'])
def question_new():
    f = request.form
    with db_conn() as conn:
        conn.execute(
            'INSERT INTO questions (category_id,article_id,question,option_a,option_b,'
            'option_c,option_d,answer,explanation,difficulty) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (f.get('category_id') or None, f.get('article_id') or None,
             f.get('question','').strip(), f.get('option_a','').strip(),
             f.get('option_b','').strip(), f.get('option_c','').strip(),
             f.get('option_d','').strip(), f.get('answer','A').upper(),
             f.get('explanation','').strip(), f.get('difficulty','Medium')))
    flash('题目已添加', 'success'); return redirect(url_for('training', tab='questions'))

@app.route('/training/questions/<int:qid>/delete', methods=['POST'])
def question_delete(qid):
    with db_conn() as conn:
        conn.execute('DELETE FROM questions WHERE id=?', (qid,))
    flash('题目已删除', 'success'); return redirect(url_for('training', tab='questions'))

@app.route('/training/exams/new', methods=['POST'])
def exam_new():
    f = request.form
    q_ids = request.form.getlist('question_ids')
    with db_conn() as conn:
        cur = conn.execute(
            'INSERT INTO exams (title,description,pass_score) VALUES (?,?,?)',
            (f.get('title','').strip(), f.get('description','').strip(),
             int(f.get('pass_score',60) or 60)))
        eid = cur.lastrowid
        for i, qid in enumerate(q_ids):
            conn.execute('INSERT OR IGNORE INTO exam_questions (exam_id,question_id,order_num) VALUES (?,?,?)',
                         (eid, int(qid), i))
    flash('试卷已创建', 'success'); return redirect(url_for('training', tab='exams'))

@app.route('/training/exams/<int:eid>/delete', methods=['POST'])
def exam_delete(eid):
    with db_conn() as conn:
        conn.execute('DELETE FROM exams WHERE id=?', (eid,))
    flash('试卷已删除', 'success'); return redirect(url_for('training', tab='exams'))

@app.route('/training/plans/new', methods=['POST'])
def plan_new():
    f = request.form
    emp_ids = request.form.getlist('employee_ids')
    with db_conn() as conn:
        cur = conn.execute(
            'INSERT INTO training_plans (title,exam_id,due_date,notes) VALUES (?,?,?,?)',
            (f.get('title','').strip(), f.get('exam_id') or None,
             f.get('due_date','').strip(), f.get('notes','').strip()))
        pid = cur.lastrowid
        for eid in emp_ids:
            conn.execute(
                'INSERT OR IGNORE INTO training_assignments (plan_id,employee_id) VALUES (?,?)',
                (pid, int(eid)))
    flash('培训计划已创建', 'success'); return redirect(url_for('training_plan', pid=pid))

@app.route('/training/plans/<int:pid>')
def training_plan(pid):
    with db_conn() as conn:
        plan = conn.execute(
            'SELECT tp.*, e.title AS exam_title, e.pass_score FROM training_plans tp '
            'LEFT JOIN exams e ON tp.exam_id=e.id WHERE tp.id=?', (pid,)).fetchone()
        if not plan:
            flash('培训计划不存在', 'error'); return redirect(url_for('training'))
        assignments = conn.execute(
            'SELECT ta.*, emp.name AS emp_name, emp.email FROM training_assignments ta '
            'JOIN employees emp ON ta.employee_id=emp.id WHERE ta.plan_id=?', (pid,)).fetchall()
        exam_questions = []
        if plan['exam_id']:
            exam_questions = conn.execute(
                'SELECT q.* FROM questions q JOIN exam_questions eq ON q.id=eq.question_id '
                'WHERE eq.exam_id=? ORDER BY eq.order_num', (plan['exam_id'],)).fetchall()
        all_employees = conn.execute('SELECT * FROM employees WHERE active=1').fetchall()
        assigned_ids  = {a['employee_id'] for a in assignments}
    return render_template('training_plan.html', plan=plan, assignments=assignments,
                           exam_questions=exam_questions, all_employees=all_employees,
                           assigned_ids=assigned_ids)

@app.route('/training/plans/<int:pid>/assign', methods=['POST'])
def plan_assign(pid):
    emp_ids = request.form.getlist('employee_ids')
    with db_conn() as conn:
        conn.execute('DELETE FROM training_assignments WHERE plan_id=?', (pid,))
        for eid in emp_ids:
            conn.execute('INSERT OR IGNORE INTO training_assignments (plan_id,employee_id) VALUES (?,?)',
                         (pid, int(eid)))
    flash('分配已更新', 'success'); return redirect(url_for('training_plan', pid=pid))

@app.route('/training/plans/<int:pid>/delete', methods=['POST'])
def plan_delete(pid):
    with db_conn() as conn:
        conn.execute('DELETE FROM training_plans WHERE id=?', (pid,))
    flash('培训计划已删除', 'success'); return redirect(url_for('training'))

@app.route('/training/plans/<int:pid>/take', methods=['GET', 'POST'])
def exam_take(pid):
    with db_conn() as conn:
        plan = conn.execute(
            'SELECT tp.*, e.title AS exam_title, e.pass_score FROM training_plans tp '
            'LEFT JOIN exams e ON tp.exam_id=e.id WHERE tp.id=?', (pid,)).fetchone()
        if not plan or not plan['exam_id']:
            flash('该培训计划暂无试卷', 'error'); return redirect(url_for('training'))
        questions = conn.execute(
            'SELECT q.* FROM questions q JOIN exam_questions eq ON q.id=eq.question_id '
            'WHERE eq.exam_id=? ORDER BY eq.order_num', (plan['exam_id'],)).fetchall()
        employees = conn.execute(
            'SELECT emp.* FROM training_assignments ta JOIN employees emp ON ta.employee_id=emp.id '
            'WHERE ta.plan_id=?', (pid,)).fetchall()

    if request.method == 'POST':
        f = request.form
        emp_id = int(f.get('employee_id', 0))
        answers = {str(q['id']): f.get(f'q_{q["id"]}', '').upper() for q in questions}
        correct = sum(1 for q in questions if answers.get(str(q['id'])) == q['answer'])
        total   = len(questions)
        score   = round(correct / total * 100) if total else 0
        passed  = 1 if score >= (plan['pass_score'] or 60) else 0
        with db_conn() as conn:
            conn.execute(
                'UPDATE training_assignments SET status=?,score=?,passed=?,answers=?,completed_at=datetime("now","localtime")'
                ' WHERE plan_id=? AND employee_id=?',
                ('Completed', score, passed, json.dumps(answers), pid, emp_id))
        return render_template('exam_result.html', plan=plan, questions=questions,
                               answers=answers, score=score, passed=passed,
                               correct=correct, total=total)

    return render_template('exam_take.html', plan=plan, questions=questions, employees=employees)


# ── Region routes ─────────────────────────────────────────────────────────────

def build_region_tree(flat_list):
    nodes = {r['id']: dict(r) | {'children': []} for r in flat_list}
    roots = []
    for node in nodes.values():
        pid = node['parent_id']
        if pid and pid in nodes:
            nodes[pid]['children'].append(node)
        elif not pid:
            roots.append(node)
    def _sort(lst):
        lst.sort(key=lambda n: (n.get('sort_order', 0), n.get('name', '')))
        for n in lst: _sort(n['children'])
    _sort(roots)
    return roots

LEVEL_NAMES = {1: '省份', 2: '城市', 3: '厂区', 4: '车间'}

@app.route('/regions')
def regions():
    with db_conn() as conn:
        flat = conn.execute('SELECT * FROM regions ORDER BY level, sort_order, name').fetchall()
    return render_template('regions.html', tree=build_region_tree(flat),
                           flat=flat, level_names=LEVEL_NAMES)

@app.route('/regions/new', methods=['POST'])
def region_new():
    f = request.form
    name = f.get('name', '').strip()
    level = int(f.get('level', 1) or 1)
    parent_id = f.get('parent_id') or None
    if not name:
        flash('名称不能为空', 'error')
        return redirect(url_for('regions'))
    with db_conn() as conn:
        conn.execute('INSERT INTO regions (name,level,parent_id,code) VALUES (?,?,?,?)',
                     (name, level, parent_id, f.get('code', '').strip()))
    flash(f'已添加「{name}」', 'success')
    return redirect(url_for('regions'))

@app.route('/regions/<int:rid>/edit', methods=['POST'])
def region_edit(rid):
    f = request.form
    with db_conn() as conn:
        conn.execute('UPDATE regions SET name=?,code=? WHERE id=?',
                     (f.get('name', '').strip(), f.get('code', '').strip(), rid))
    flash('已更新', 'success')
    return redirect(url_for('regions'))

@app.route('/regions/<int:rid>/delete', methods=['POST'])
def region_delete(rid):
    with db_conn() as conn:
        conn.execute('DELETE FROM regions WHERE id=?', (rid,))
    flash('已删除', 'success')
    return redirect(url_for('regions'))


# ── HR Portal routes ───────────────────────────────────────────────────────────

@app.route('/hr')
def hr_portal():
    tab = request.args.get('tab', 'attendance')
    today = datetime.now().strftime('%Y-%m-%d')
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    leave_filter = request.args.get('leave_filter', 'Pending')
    expense_filter = request.args.get('expense_filter', 'Pending')

    view_date = request.args.get('view_date', today)

    with db_conn() as conn:
        emp_list = conn.execute(
            'SELECT id,name,department FROM employees WHERE active=1 ORDER BY name'
        ).fetchall()
        today_records = conn.execute('''
            SELECT ar.*, e.name AS emp_name FROM attendance_records ar
            JOIN employees e ON ar.employee_id=e.id
            WHERE ar.work_date=? ORDER BY ar.checkin_time DESC
        ''', (view_date,)).fetchall()
        stats_rows = conn.execute('''
            SELECT e.id, e.name, e.department,
                   COUNT(ar.id) AS days_present,
                   SUM(CASE WHEN ar.status='Late' THEN 1 ELSE 0 END) AS days_late,
                   SUM(COALESCE(ar.overtime_mins,0)) AS total_overtime_mins
            FROM employees e
            LEFT JOIN attendance_records ar
                ON ar.employee_id=e.id AND ar.work_date LIKE ?
            WHERE e.active=1 GROUP BY e.id ORDER BY e.name
        ''', (month + '%',)).fetchall()
        if leave_filter == 'all':
            leaves = conn.execute('''
                SELECT lr.*, e.name AS emp_name FROM leave_requests lr
                JOIN employees e ON lr.employee_id=e.id
                ORDER BY lr.created_at DESC LIMIT 200
            ''').fetchall()
        else:
            leaves = conn.execute('''
                SELECT lr.*, e.name AS emp_name FROM leave_requests lr
                JOIN employees e ON lr.employee_id=e.id
                WHERE lr.status=? ORDER BY lr.created_at DESC
            ''', (leave_filter,)).fetchall()
        if expense_filter == 'all':
            expenses = conn.execute('''
                SELECT ec.*, e.name AS emp_name FROM expense_claims ec
                JOIN employees e ON ec.employee_id=e.id
                ORDER BY ec.created_at DESC LIMIT 200
            ''').fetchall()
        else:
            expenses = conn.execute('''
                SELECT ec.*, e.name AS emp_name FROM expense_claims ec
                JOIN employees e ON ec.employee_id=e.id
                WHERE ec.status=? ORDER BY ec.created_at DESC
            ''', (expense_filter,)).fetchall()

    return render_template('hr.html',
                           tab=tab, today=today, now_time=datetime.now().strftime('%H:%M'),
                           emp_list=emp_list, today_records=today_records,
                           view_date=view_date,
                           stats_rows=stats_rows, month=month,
                           leaves=leaves, leave_filter=leave_filter,
                           expenses=expenses, expense_filter=expense_filter)

def _verify_gps(lat_str, lng_str):
    """Check GPS coordinates against configured office locations.
    Returns (ok: bool, message: str, lat: float|None, lng: float|None, verified: int)"""
    config = load_config()
    locs = config.get('office_locations', [])
    lat = lng = None
    try:
        lat = float(lat_str)
        lng = float(lng_str)
    except (TypeError, ValueError):
        pass

    if not locs:
        return True, '', lat, lng, 0

    if lat is None or lng is None:
        return False, '打卡地点验证失败：无法获取您的位置，请允许浏览器定位权限后重试', None, None, 0

    best_name, best_dist = '', float('inf')
    for loc in locs:
        d = haversine(lat, lng, loc['lat'], loc['lng'])
        if d < best_dist:
            best_dist, best_name = d, loc['name']
        if d <= loc['radius']:
            return True, '', lat, lng, 1

    return (False,
            f'打卡地点不在允许范围内（最近地点：{best_name}，距离 {int(best_dist)} 米）',
            lat, lng, 0)

@app.route('/hr/attendance/checkin', methods=['POST'])
def hr_checkin():
    f = request.form
    emp_id = f.get('employee_id')
    if not emp_id:
        flash('请选择员工', 'error')
        return redirect(url_for('hr_portal', tab='attendance'))

    ok, msg, lat, lng, verified = _verify_gps(f.get('lat'), f.get('lng'))
    if not ok:
        flash(msg, 'error')
        return redirect(url_for('hr_portal', tab='attendance'))

    work_date = f.get('work_date', datetime.now().strftime('%Y-%m-%d'))
    checkin_time = f.get('checkin_time', datetime.now().strftime('%H:%M'))
    location = f.get('checkin_location', '').strip()
    notes = f.get('notes', '').strip()
    status = 'Normal'
    try:
        h, m = map(int, checkin_time.split(':'))
        if h > 9 or (h == 9 and m > 15):
            status = 'Late'
    except Exception:
        pass
    with db_conn() as conn:
        try:
            conn.execute(
                'INSERT INTO attendance_records '
                '(employee_id,work_date,checkin_time,checkin_location,status,notes,checkin_lat,checkin_lng,gps_verified) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (emp_id, work_date, checkin_time, location, status, notes, lat, lng, verified))
            flash('入厂打卡成功', 'success')
        except Exception:
            conn.execute(
                'UPDATE attendance_records SET checkin_time=?,checkin_location=?,status=?,notes=?,checkin_lat=?,checkin_lng=?,gps_verified=? '
                'WHERE employee_id=? AND work_date=?',
                (checkin_time, location, status, notes, lat, lng, verified, emp_id, work_date))
            flash('入厂记录已更新', 'success')
    return redirect(url_for('hr_portal', tab='attendance'))

@app.route('/hr/attendance/checkout', methods=['POST'])
def hr_checkout():
    f = request.form
    emp_id = f.get('employee_id')
    if not emp_id:
        flash('请选择员工', 'error')
        return redirect(url_for('hr_portal', tab='attendance'))

    ok, msg, lat, lng, verified = _verify_gps(f.get('lat'), f.get('lng'))
    if not ok:
        flash(msg, 'error')
        return redirect(url_for('hr_portal', tab='attendance'))

    work_date = f.get('work_date', datetime.now().strftime('%Y-%m-%d'))
    checkout_time = f.get('checkout_time', datetime.now().strftime('%H:%M'))
    location = f.get('checkout_location', '').strip()
    overtime_mins = int(f.get('overtime_mins', 0) or 0)
    with db_conn() as conn:
        if conn.execute('SELECT id FROM attendance_records WHERE employee_id=? AND work_date=?',
                        (emp_id, work_date)).fetchone():
            conn.execute(
                'UPDATE attendance_records SET checkout_time=?,checkout_location=?,overtime_mins=?,checkout_lat=?,checkout_lng=? '
                'WHERE employee_id=? AND work_date=?',
                (checkout_time, location, overtime_mins, lat, lng, emp_id, work_date))
        else:
            conn.execute(
                'INSERT INTO attendance_records (employee_id,work_date,checkout_time,checkout_location,overtime_mins,checkout_lat,checkout_lng) '
                'VALUES (?,?,?,?,?,?,?)',
                (emp_id, work_date, checkout_time, location, overtime_mins, lat, lng))
    flash('出厂打卡成功', 'success')
    return redirect(url_for('hr_portal', tab='attendance'))

@app.route('/hr/leaves/new', methods=['POST'])
def leave_new():
    f = request.form
    emp_id = f.get('employee_id')
    if not emp_id:
        flash('请选择员工', 'error')
        return redirect(url_for('hr_portal', tab='leave'))
    start, end = f.get('start_date', ''), f.get('end_date', '')
    try:
        days = max(1, (datetime.strptime(end, '%Y-%m-%d') - datetime.strptime(start, '%Y-%m-%d')).days + 1)
    except Exception:
        days = float(f.get('days', 1) or 1)
    with db_conn() as conn:
        conn.execute(
            'INSERT INTO leave_requests (employee_id,leave_type,start_date,end_date,days,reason) VALUES (?,?,?,?,?,?)',
            (emp_id, f.get('leave_type', 'Annual'), start, end, days, f.get('reason', '').strip()))
    flash('请假申请已提交', 'success')
    return redirect(url_for('hr_portal', tab='leave'))

@app.route('/hr/leaves/<int:lid>/approve', methods=['POST'])
def leave_approve(lid):
    with db_conn() as conn:
        conn.execute("UPDATE leave_requests SET status='Approved',approver_notes=? WHERE id=?",
                     (request.form.get('notes', ''), lid))
    flash('已批准', 'success')
    return redirect(url_for('hr_portal', tab='leave'))

@app.route('/hr/leaves/<int:lid>/reject', methods=['POST'])
def leave_reject(lid):
    with db_conn() as conn:
        conn.execute("UPDATE leave_requests SET status='Rejected',approver_notes=? WHERE id=?",
                     (request.form.get('notes', ''), lid))
    flash('已拒绝', 'warning')
    return redirect(url_for('hr_portal', tab='leave'))

@app.route('/hr/leaves/<int:lid>/delete', methods=['POST'])
def leave_delete(lid):
    with db_conn() as conn:
        conn.execute('DELETE FROM leave_requests WHERE id=?', (lid,))
    flash('已删除', 'success')
    return redirect(url_for('hr_portal', tab='leave'))

@app.route('/hr/expenses/new', methods=['POST'])
def expense_new():
    f = request.form
    emp_id = f.get('employee_id')
    if not emp_id:
        flash('请选择员工', 'error')
        return redirect(url_for('hr_portal', tab='expense'))
    invoice_path = ''
    inv = request.files.get('invoice')
    if inv and inv.filename:
        safe = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{inv.filename.replace(' ', '_')}"
        invoice_path = safe
        inv.save(os.path.join(UPLOAD_DIR, safe))
    with db_conn() as conn:
        conn.execute(
            'INSERT INTO expense_claims (employee_id,claim_date,claim_type,amount,description,invoice_path) VALUES (?,?,?,?,?,?)',
            (emp_id, f.get('claim_date', ''), f.get('claim_type', 'Transport'),
             float(f.get('amount', 0) or 0), f.get('description', '').strip(), invoice_path))
    flash('报销申请已提交', 'success')
    return redirect(url_for('hr_portal', tab='expense'))

@app.route('/hr/expenses/<int:eid>/approve', methods=['POST'])
def expense_approve(eid):
    with db_conn() as conn:
        conn.execute("UPDATE expense_claims SET status='Approved',approver_notes=? WHERE id=?",
                     (request.form.get('notes', ''), eid))
    flash('已批准', 'success')
    return redirect(url_for('hr_portal', tab='expense'))

@app.route('/hr/expenses/<int:eid>/reject', methods=['POST'])
def expense_reject(eid):
    with db_conn() as conn:
        conn.execute("UPDATE expense_claims SET status='Rejected',approver_notes=? WHERE id=?",
                     (request.form.get('notes', ''), eid))
    flash('已拒绝', 'warning')
    return redirect(url_for('hr_portal', tab='expense'))

@app.route('/hr/expenses/<int:eid>/delete', methods=['POST'])
def expense_delete(eid):
    with db_conn() as conn:
        conn.execute('DELETE FROM expense_claims WHERE id=?', (eid,))
    flash('已删除', 'success')
    return redirect(url_for('hr_portal', tab='expense'))

@app.route('/hr/expenses/<int:eid>/invoice')
def expense_invoice(eid):
    with db_conn() as conn:
        row = conn.execute('SELECT invoice_path FROM expense_claims WHERE id=?', (eid,)).fetchone()
    if not row or not row['invoice_path']:
        flash('无附件', 'error')
        return redirect(url_for('hr_portal', tab='expense'))
    return send_from_directory(UPLOAD_DIR, row['invoice_path'])

@app.route('/employees/<int:eid>/work-info', methods=['POST'])
def employee_work_info_save(eid):
    f = request.form
    supervisor_id = f.get('supervisor_id') or None
    product_cats = ','.join(request.form.getlist('product_category_ids'))
    with db_conn() as conn:
        if conn.execute('SELECT id FROM employee_work_info WHERE employee_id=?', (eid,)).fetchone():
            conn.execute(
                'UPDATE employee_work_info SET supervisor_id=?,skill_level=?,product_categories=?,travel_status=?,overtime_notes=?,updated_at=datetime("now","localtime") WHERE employee_id=?',
                (supervisor_id, f.get('skill_level', ''), product_cats,
                 f.get('travel_status', ''), f.get('overtime_notes', '').strip(), eid))
        else:
            conn.execute(
                'INSERT INTO employee_work_info (employee_id,supervisor_id,skill_level,product_categories,travel_status,overtime_notes) VALUES (?,?,?,?,?,?)',
                (eid, supervisor_id, f.get('skill_level', ''), product_cats,
                 f.get('travel_status', ''), f.get('overtime_notes', '').strip()))
    flash('工作信息已保存', 'success')
    return redirect(url_for('employee_edit', eid=eid))


os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
init_db()

def _seed_users():
    _defaults = [
        ('admin', 'admin', 'admin'),
        ('qc1',   'qc1',   'inspector'),
        ('qc2',   'qc2',   'inspector'),
    ]
    with db_conn() as conn:
        existing = {r[0] for r in conn.execute('SELECT username FROM users').fetchall()}
        for username, password, role in _defaults:
            if username not in existing:
                conn.execute(
                    'INSERT INTO users (username, password_hash, role) VALUES (?,?,?)',
                    (username, generate_password_hash(password), role))

_seed_users()

@app.before_request
def _auth_check():
    public = {'healthz', 'login', 'logout', 'debug_info', 'static'}
    if request.endpoint and request.endpoint not in public and 'user_id' not in session:
        return redirect(url_for('login'))
    g.username = session.get('username', '')
    g.role = session.get('role', '')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        with db_conn() as conn:
            user = conn.execute(
                'SELECT * FROM users WHERE username=? AND active=1', (username,)
            ).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            return redirect(url_for('index'))
        flash('用户名或密码错误', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
