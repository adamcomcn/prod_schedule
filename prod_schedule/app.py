import os, io, json, hashlib, tempfile, math, logging, traceback, secrets, hmac, time, base64, uuid, zipfile, re, unicodedata, shutil
from collections import defaultdict, deque
from difflib import SequenceMatcher
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session, send_from_directory, send_file, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
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
# Railway (and most PaaS hosts) sit behind a reverse proxy; trust one hop of
# X-Forwarded-* so request.remote_addr is the real client, not the proxy.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=int(os.environ.get('TRUSTED_PROXY_HOPS', '1')),
                        x_proto=1, x_host=1)
IS_PRODUCTION = bool(
    os.environ.get('RAILWAY_ENVIRONMENT_ID')
    or os.environ.get('RAILWAY_ENVIRONMENT_NAME')
    or os.environ.get('RENDER')
)
SECRET_KEY = os.environ.get('SECRET_KEY')
if IS_PRODUCTION and not SECRET_KEY:
    raise RuntimeError('SECRET_KEY is required in production')
app.secret_key = SECRET_KEY or 'local-development-only'
app.config.update(
    MAX_CONTENT_LENGTH=int(os.environ.get('MAX_UPLOAD_BYTES', 50 * 1024 * 1024)),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=int(os.environ.get('SESSION_HOURS', '8'))),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
)
app.jinja_env.globals['enumerate'] = enumerate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DATA_DIR = os.path.abspath(os.environ.get('APP_DATA_DIR', BASE_DIR))
DATA_DIR = os.path.join(APP_DATA_DIR, 'data')
UPLOAD_DIR = os.path.join(APP_DATA_DIR, 'uploads')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
CURRENT_FILE = os.path.join(DATA_DIR, 'current_week.json')
PREVIOUS_FILE = os.path.join(DATA_DIR, 'previous_week.json')
INSPECTIONS_CACHE = os.path.join(DATA_DIR, 'inspections_cache.json')
PENDING_UPLOAD_FILE = os.path.join(DATA_DIR, 'pending_upload.json')
HISTORY_DIR = os.path.join(DATA_DIR, 'history')

EXCEL_PASSWORD   = os.environ.get('EXCEL_PASSWORD', '')
PRODUCT_IMG_DIR  = os.path.join(APP_DATA_DIR, 'product_images')

EVIDENCE_EXTENSIONS = {'.pdf', '.xlsx', '.xls', '.csv', '.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif',
                       '.mp4', '.mov', '.avi', '.mkv'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
INVOICE_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png'}

SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive',
]

LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
_login_failures = defaultdict(deque)

ADMIN_ENDPOINTS = {
    'dashboard', 'debug_info', 'upload_excel', 'upload_preview', 'upload_confirm', 'upload_cancel', 'settings',
    'settings_modules', 'office_location_add',
    'office_location_delete', 'supplier_new', 'supplier_edit', 'suppliers_import',
    'supplier_delete', 'category_new', 'category_delete', 'inspector_add',
    'inspector_delete', 'product_new', 'product_edit', 'product_delete',
    'order_new', 'order_edit', 'order_delete', 'employees', 'employee_new',
    'employee_edit', 'employee_delete', 'employee_work_info_save',
    'form_keywords_update', 'form_template_delete', 'kb_article_new',
    'kb_article_edit', 'kb_article_delete', 'kb_category_new',
    'kb_category_delete', 'question_new', 'question_delete', 'exam_new',
    'exam_delete', 'plan_new', 'plan_assign', 'plan_delete', 'region_new',
    'region_edit', 'region_delete', 'leave_approve', 'leave_reject',
    'leave_delete', 'expense_approve', 'expense_reject', 'expense_delete',
    'users_admin', 'user_create', 'user_toggle', 'user_reset_password',
    'user_update',
}

# ── helpers ──────────────────────────────────────────────────────────────────

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, data):
    # Write to a temp file then atomically replace, so a crash mid-write
    # never leaves a truncated JSON file behind.
    tmp_path = f'{path}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)

def schedule_fingerprint(data):
    canonical = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()

def remember_schedule_upload(data, upload_date=''):
    if not data:
        return
    with db_conn() as conn:
        conn.execute(
            'INSERT OR IGNORE INTO schedule_uploads (fingerprint, upload_date) '
            'VALUES (?,?)', (schedule_fingerprint(data), upload_date))

def csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token

app.jinja_env.globals['csrf_token'] = csrf_token

def _login_rate_limited(client_id):
    attempts = _login_failures[client_id]
    cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return len(attempts) >= LOGIN_MAX_FAILURES

def _record_login_failure(client_id):
    _login_failures[client_id].append(time.monotonic())

def _safe_next_url(value):
    if not value or not value.startswith('/') or value.startswith('//') or '\\' in value:
        return None
    return value

# ── Language (Chinese default, English for HQ) ───────────────────────────────
LANGUAGES = ('zh', 'en')
DEFAULT_LANGUAGE = 'zh'

def _lang_from_cookie():
    lang = request.cookies.get('lang', '')
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE

def current_lang():
    try:
        return g.get('lang') or DEFAULT_LANGUAGE
    except RuntimeError:  # outside a request (scripts, e-mail jobs)
        return DEFAULT_LANGUAGE

def tr(zh, en):
    """Pick the Chinese or English text for the current user."""
    return en if current_lang() == 'en' else zh

app.jinja_env.globals['tr'] = tr
app.jinja_env.globals['current_lang'] = current_lang

_STATUS_LABELS = {
    'Pending': '待检验', 'In Progress': '进行中', 'Completed': '已完成', 'On Hold': '暂停', 'Closed': '已关闭',
    'Pass': '合格', 'Fail': '不合格', 'Partial Pass': '部分合格', 'N/A': '不适用',
    'new': '新增', 'not_shipped': '未出货', 'partially_shipped': '部分出货',
    'shipped': '已出货', 'typo': '疑似笔误',
    'Ready to Ship': '待出货', 'In Production': '生产中', 'Shipped': '已出货',
    'Casting arrived': '铸件已到', 'Raw Castings': '毛坯铸件', 'Conditional Pass': '有条件合格',
    'Approved': '已批准', 'Rejected': '已拒绝',
}
_STATUS_LABELS_EN = {
    'new': 'New', 'not_shipped': 'Not shipped', 'partially_shipped': 'Partially shipped',
    'shipped': 'Shipped', 'typo': 'Possible typo',
}

def status_label(value):
    """Display label for stored status values (stored values stay English)."""
    text = '' if value is None else str(value)
    if current_lang() == 'en':
        return _STATUS_LABELS_EN.get(text, text)
    return _STATUS_LABELS.get(text, text)

app.jinja_env.filters['status_label'] = status_label

# Chinese labels for the supplier workbook's column headers.
_HEADER_LABELS_ZH = {
    'order number': '订单号', 'daemco purchase order': '采购单号 (PO)',
    'purchase order': '采购单号 (PO)', 'order date': '下单日期',
    'item code': '物料编码', 'item description': '物料描述', 'quantity': '数量',
    'estimated completion date': '预计完成日', 'actual completion date': '实际完成日',
    'must ship date': '最迟出货日', 'crate qty': '箱数', 'qa brts sent?': 'QA BRT 已发送?',
    'current status': '当前状态', 'pieces per crate': '每箱件数', 'full crates': '整箱数',
    'casting arrived date': '铸件到货日', 'foundry': '铸造厂', 'supplier': '供应商',
    'unit weight (kg)': '单重 (kg)', 'total weight (kg)': '总重 (kg)',
    'gross weight (kg)': '毛重 (kg)', 'reliable code': 'Reliable 编码',
    'chinese description': '中文描述', 'updated price': '更新单价', 'total price': '总价',
    'unit price': '单价', 'price': '价格',
}

# Schedule columns hidden by default (inspectors can show them from the
# "Columns" menu; admins can change the list in Settings).
DEFAULT_HIDDEN_COLUMNS = [
    'order date', 'reliable code', 'pieces per crate', 'full crates',
    'actual completion date', 'must ship date', 'must ship time',
    'unit weight (kg)', 'total weight (kg)', 'gross weight (kg)',
    'updated price', 'total price',
]


# Commercial columns are for admins only: they are removed on the server
# before schedule data is rendered or exported for inspectors / leads.
ADMIN_ONLY_COLUMN_WORDS = ('price', 'cost', 'amount', '单价', '价格', '金额')


def is_admin_only_column(header):
    text = str(header or '').lower()
    return any(word in text for word in ADMIN_ONLY_COLUMN_WORDS)


def strip_admin_only_columns(sheets, shipped=None):
    """Return copies of {sheet: [headers, *rows]} (and {sheet: [rows]} of
    shipped rows laid out with the same headers) without admin-only columns."""
    shipped = shipped or {}
    out_sheets, out_shipped = {}, {}
    for sheet, rows in sheets.items():
        if not rows:
            out_sheets[sheet] = rows
            continue
        keep = [i for i, h in enumerate(rows[0]) if not is_admin_only_column(h)]

        def cut(row):
            return [row[i] if i < len(row) else '' for i in keep]

        out_sheets[sheet] = [cut(r) for r in rows]
        if sheet in shipped:
            out_shipped[sheet] = [cut(r) for r in shipped[sheet]]
    for sheet, rows in shipped.items():
        out_shipped.setdefault(sheet, rows)
    return out_sheets, out_shipped


def hidden_schedule_columns():
    configured = load_config().get('schedule_hidden_columns')
    columns = DEFAULT_HIDDEN_COLUMNS if configured is None else configured
    return [c.strip().lower() for c in columns if c and c.strip()]

app.jinja_env.globals['hidden_schedule_columns'] = hidden_schedule_columns

def header_label(header):
    text = '' if header is None else str(header)
    if current_lang() == 'en':
        return text
    return _HEADER_LABELS_ZH.get(text.lower(), text)

app.jinja_env.filters['header_label'] = header_label

# ── Optional modules (hidden during the pilot, can be enabled in Settings) ──
MODULES = {
    'hr':        {'zh': '人事（考勤/请假/报销）', 'en': 'HR (attendance / leave / expenses)',
                  'paths': ('/hr',)},
    'training':  {'zh': '培训与考试', 'en': 'Training & exams', 'paths': ('/training',)},
    'knowledge': {'zh': '知识库', 'en': 'Knowledge base', 'paths': ('/knowledge',)},
    'orders':    {'zh': '订单（手动录入）', 'en': 'Orders (manual entry)', 'paths': ('/orders',)},
    'suppliers': {'zh': '供应商', 'en': 'Suppliers', 'paths': ('/suppliers',)},
    'forms':     {'zh': '检验模板（数字检验清单）', 'en': 'Inspection form templates', 'paths': ('/forms',)},
    'regions':   {'zh': '区域（层级维护）', 'en': 'Regions (hierarchy)', 'paths': ('/regions',)},
    'employees': {'zh': '员工档案（人事/培训需要）', 'en': 'Employee records (needed by HR / Training)',
                  'paths': ('/employees',)},
    'products':  {'zh': '产品（类别 / 自动分配检验员）', 'en': 'Products (categories / auto inspectors)', 'paths': ('/products',)},
}

def module_enabled(name):
    modules = load_config().get('modules', {})
    if name == 'employees':
        # HR and Training are built on the employee records
        return any(bool(modules.get(m, False)) for m in ('employees', 'hr', 'training'))
    return bool(modules.get(name, False))

def _disabled_module_for_path(path):
    for name, info in MODULES.items():
        if any(path == p or path.startswith(p + '/') for p in info['paths']):
            if not module_enabled(name):
                return name
    return None

app.jinja_env.globals['module_enabled'] = module_enabled

def _requested_employee_id(form):
    if g.role == 'admin':
        return form.get('employee_id')
    return str(g.employee_id) if g.employee_id else None

def google_credentials_configured():
    return bool(os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON_B64'))

def _display_filename(filename):
    """Keep the user's original file name (including Chinese characters) for
    display only; the file itself is always saved under a random name."""
    name = os.path.basename((filename or '').replace('\\', '/'))
    name = ''.join(ch for ch in name if ch.isprintable()).strip()
    return name[:200]

def _upload_extension(filename):
    return os.path.splitext(_display_filename(filename))[1].lower()

def _save_uploaded_file(file_obj, directory, allowed_extensions):
    # secure_filename() strips non-ASCII characters, so "检验报告.pdf" would
    # become "pdf" with no extension; take the extension from the raw name.
    original = _display_filename(file_obj.filename) or secure_filename(file_obj.filename or '')
    extension = _upload_extension(file_obj.filename)
    if not original or extension not in allowed_extensions:
        raise ValueError('Unsupported file type')
    os.makedirs(directory, exist_ok=True)
    saved_name = f'{uuid.uuid4().hex}{extension}'
    file_obj.save(os.path.join(directory, saved_name))
    return original, saved_name

_DATE_IN_TEXT = re.compile(r'(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)')

def _parse_date(value):
    """Parse a schedule date cell ('2026-10-05', '2026/6/7', '05/10/2026',
    '2026-10-05 00:00:00', '2026/6/15 ready for ship'); return None for
    blanks or text without a date such as 'TBC'."""
    text = str(value or '').strip()
    match = _DATE_IN_TEXT.search(text)
    if match:
        try:
            return date(*(int(g) for g in match.groups()))
        except ValueError:
            return None
    for fmt in ('%d/%m/%Y', '%d.%m.%Y'):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None

def split_est(value):
    """Split an estimated-completion cell into (date or None, remark text),
    e.g. '2026/6/15 ready for ship' -> (2026-06-15, 'ready for ship')."""
    text = str(value or '').strip()
    parsed = _parse_date(text)
    if not parsed:
        return None, ' '.join(text.split())
    match = _DATE_IN_TEXT.search(text)
    rest = (text[:match.start()] + ' ' + text[match.end():]) if match else ''
    rest = re.sub(r'\d{1,2}:\d{2}(:\d{2})?', '', rest)
    return parsed, ' '.join(rest.split()).strip(' -,;')

def est_changed(old, new):
    """True when the date or the remark text really differs (ignores
    formatting such as '2026-05-15' vs '2026/5/15 00:00:00')."""
    (d1, n1), (d2, n2) = split_est(old), split_est(new)
    return d1 != d2 or n1.lower() != n2.lower()

def days_until(value, today):
    parsed = _parse_date(value)
    return (parsed - today).days if parsed else None

app.jinja_env.globals['days_until'] = days_until

def est_iso(value):
    """ISO date of a schedule date cell ('2026/6/15 ready for ship' -> '2026-06-15'), '' if none."""
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else ''

app.jinja_env.globals['est_iso'] = est_iso

def load_config():
    return load_json(CONFIG_FILE, {
        'sheet_id': '',
        'drive_folder_id': '',
        'upload_date': '',
        'valve_prefixes': ['RSV'],
        'office_locations': [],
    })

def parse_json_list(value):
    try:
        parsed = json.loads(value or '[]')
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

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
    'brt':      {'label': 'BRT (Batch Release Test)', 'label_zh': 'BRT 批次放行报告',
                 'icon': '📋', 'color': '#1e40af', 'bg': '#dbeafe',
                 'accepts': '.pdf,.xlsx,.xls,.jpg,.jpeg,.png,.heic,image/*'},
    'checklist':{'label': 'Inspection Checklist', 'label_zh': '检验清单',
                 'icon': '✅', 'color': '#065f46', 'bg': '#d1fae5',
                 'accepts': '.pdf,.jpg,.jpeg,.png,.heic,image/*'},
    'material': {'label': 'Material Report', 'label_zh': '材质报告',
                 'icon': '🔬', 'color': '#92400e', 'bg': '#fef3c7',
                 'accepts': '.pdf,.xlsx,.xls,.jpg,.jpeg,.png,.heic,image/*'},
    'daq':      {'label': 'DAQ Data (Pressure Test)', 'label_zh': 'DAQ 压力测试数据',
                 'icon': '📊', 'color': '#5b21b6', 'bg': '#ede9fe',
                 'accepts': '.pdf,.xlsx,.xls,.csv'},
    'vtrust':   {'label': 'V-Trust Pressure Test Video', 'label_zh': 'V-Trust 压力测试视频',
                 'icon': '🎥', 'color': '#9a3412', 'bg': '#fff7ed',
                 'accepts': '.mp4,.mov,.avi,.mkv,video/*'},
    'spark':    {'label': 'Spark / Holiday Test Video', 'label_zh': '电火花测试视频',
                 'icon': '⚡', 'color': '#991b1b', 'bg': '#fee2e2',
                 'accepts': '.mp4,.mov,.avi,.mkv,video/*'},
    'xrf':      {'label': 'XRF Report (Material Composition)', 'label_zh': 'XRF 材质成分报告',
                 'icon': '⚗️', 'color': '#065f46', 'bg': '#d1fae5',
                 'accepts': '.pdf,.xlsx,.xls,.jpg,.jpeg,.png,.heic,image/*'},
}

# Chinese version of each guidance line (lines are combined per product).
GUIDANCE_ZH = {
    'Inspection checklist must be signed and dated by inspector.':
        '检验清单须由检验员签名并注明日期。',
    'Check Material sheet: chemical and mechanical properties within limits':
        '检查材质表：化学成分和力学性能在限值内',
    'Check Material sheet: chemical and mechanical properties within limits (Spec 500-7)':
        '检查材质表：化学成分和力学性能在限值内（Spec 500-7）',
    'Check Checking Report: C1–C7 & A1–A9 must be OK (refer to SPEC sheet)':
        '检查检验报告：C1–C7 和 A1–A9 必须全部 OK（参照 SPEC 表）',
    'Note: DN375 has no DAQ — verify pressure test via Cells Z–AG and V-Trust video only':
        '注意：DN375 没有 DAQ，只通过 Z–AG 单元格和 V-Trust 视频核对压力测试',
    'Check DAQ section (Cells AB–BH):':
        '检查 DAQ 部分（AB–BH 单元格）：',
    '  • T1 Average [AL] (Gate test 1) ≥ 1.76 MPa':
        '  • T1 平均值 [AL]（闸板测试 1）≥ 1.76 MPa',
    '  • T2 Average [AW] (Gate test 2) ≥ 1.76 MPa':
        '  • T2 平均值 [AW]（闸板测试 2）≥ 1.76 MPa',
    '  • T3 Average [BH] (Body test)   ≥ 2.40 MPa':
        '  • T3 平均值 [BH]（阀体测试）≥ 2.40 MPa',
    'Upload pressure test machine exported data (Excel or PDF).':
        '上传压力测试机导出的数据（Excel 或 PDF）。',
    'Verify: T1 Average ≥ 1.76 MPa, T2 Average ≥ 1.76 MPa, T3 Average ≥ 2.40 MPa':
        '核对：T1 平均值 ≥ 1.76 MPa，T2 平均值 ≥ 1.76 MPa，T3 平均值 ≥ 2.40 MPa',
    'Upload V-Trust pressure test video(s) (MP4 / MOV).':
        '上传 V-Trust 压力测试视频（MP4 / MOV）。',
    'Ensure all videos are received and show acceptable test results.':
        '确认所有视频已收到，且测试结果合格。',
    '电火花 Holiday / Spark test video required for DN200 and above.':
        'DN200 及以上需要电火花（Holiday / Spark）测试视频。',
    'Ensure all spark test videos are received (MP4 / MOV).':
        '确认所有电火花测试视频已收到（MP4 / MOV）。',
    'Check 316 SS.jpg — confirm material is 316 stainless steel':
        '检查 316 SS.jpg — 确认材质为 316 不锈钢',
    'Check Assembly Report — all criteria acceptable':
        '检查装配报告 — 所有项目合格',
    'Check Bolt 316.jpg — confirm bolt material is 316 SS':
        '检查 Bolt 316.jpg — 确认螺栓材质为 316 不锈钢',
    'Check Dimension Report — compare to Daemco design drawings':
        '检查尺寸报告 — 与 Daemco 设计图纸比对',
    'For GAL variant: ensure bolts are Steel (Q235B) material':
        'GAL 型号：确认螺栓为钢制（Q235B）',
    'Check Assembly Folder — all checkboxes acceptable':
        '检查装配文件夹 — 所有勾选项合格',
    'Check Dimension Report — compare to Daemco REPAIR CLAMP 2023.11.7 drawing':
        '检查尺寸报告 — 与 Daemco REPAIR CLAMP 2023.11.7 图纸比对',
    'Check Material Folder — verify 316 stainless steel':
        '检查材质文件夹 — 确认为 316 不锈钢',
    'Upload XRF Excel / PDF report.':
        '上传 XRF 报告（Excel / PDF）。',
    'Verify material is 316 stainless steel. Confirm Pass or Fail.':
        '确认材质为 316 不锈钢，并标记合格或不合格。',
    'Review BRT document for acceptability.':
        '审核 BRT 文件是否合格。',
    'Check material report: chemical and mechanical properties within limits.':
        '检查材质报告：化学成分和力学性能在限值内。',
    'Check DPL Fitting — Casting Inspection Report: all criteria acceptable':
        '检查 DPL 管件铸件检验报告：所有项目合格',
    'Check DPL Fitting — Final Inspection Report: all criteria acceptable':
        '检查 DPL 管件最终检验报告：所有项目合格',
    'Check material report: chemical and mechanical properties within limits.':
        '检查材质报告：化学成分和力学性能在限值内。',
    'For CI/DI products: compare to Dandong Foundry acceptable limits':
        '灰铁/球铁产品：与丹东铸造厂的合格限值比对',
    'for cast iron / ductile iron (chemical composition & mechanical properties).':
        '（灰铁/球铁的化学成分和力学性能）。',
}

def localize_evidence(meta, guidance):
    if current_lang() == 'en':
        return meta['label'], guidance
    lines = [GUIDANCE_ZH.get(line, line) for line in guidance.split('\n')]
    return meta.get('label_zh') or meta['label'], '\n'.join(lines)

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
        label, text = localize_evidence(m, guidance)
        return {**m, 'type': etype, 'label': label, 'guidance': text}

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
    """Return the V-Trust result from the most recent inspection that has one.

    Submissions store it under evidence['vtrust']['result']; older records may
    carry a top-level 'vtrust_result'.
    """
    for record in reversed(inspections_list or []):
        evidence = record.get('evidence') or {}
        r = ((evidence.get('vtrust') or {}).get('result') or
             record.get('vtrust_result') or '').strip()
        if r:
            return r
    return ''

app.jinja_env.globals['vtrust_status'] = get_vtrust_status

def qa_brt_missing(status, reports):
    """A shipped / partially shipped job without an inspection report in the
    system. The Excel 'QA BRTs Sent?' cell is not trusted for this."""
    return status in {'shipped', 'partially_shipped'} and not reports

def qa_excel_mismatch(row, headers, reports):
    """Excel says QA BRTs were sent (YES) but the system holds no report."""
    qa_idx = next((i for i, h in enumerate(headers) if 'qa brt' in str(h).lower()), None)
    if qa_idx is None or qa_idx >= len(row) or reports:
        return False
    return str(row[qa_idx]).strip().lower() in {'yes', 'y'}

app.jinja_env.globals['qa_excel_mismatch'] = qa_excel_mismatch

def fmt_date(val):
    if val is None:
        return ''
    if '00:00:00' in str(val):
        return str(val).replace(' 00:00:00', '')
    return str(val)

def count_unique_jobs(sheet, rows):
    """Orders in a sheet, counted the way the dashboard counts them: one per
    order+item (split lots merge), rows without an order number ignored."""
    if not rows or len(rows) < 2:
        return 0
    keys = set()
    for row in rows[1:]:
        jk = make_job_key(sheet, row, rows[0])
        if jk.split('|')[1]:
            keys.add(jk)
    return len(keys)


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

class ExcelUploadError(ValueError):
    pass

class ExcelPasswordRequired(ExcelUploadError):
    pass

class ExcelPasswordIncorrect(ExcelUploadError):
    pass

class InvalidExcelFile(ExcelUploadError):
    pass

def decrypt_excel(file_bytes, password):
    if zipfile.is_zipfile(io.BytesIO(file_bytes)):
        return io.BytesIO(file_bytes)
    if file_bytes.startswith(bytes.fromhex('D0CF11E0A1B11AE1')) and not password:
        raise ExcelPasswordRequired(
            'This workbook is encrypted. Configure EXCEL_PASSWORD in Railway '
            'or upload an unencrypted .xlsx file.')

    enc = io.BytesIO(file_bytes)
    try:
        office_file = msoffcrypto.OfficeFile(enc)
    except Exception as exc:
        raise InvalidExcelFile('The uploaded file is not a valid .xlsx workbook.') from exc

    if not office_file.is_encrypted():
        raise InvalidExcelFile('The uploaded file is not a valid .xlsx workbook.')
    if not password:
        raise ExcelPasswordRequired(
            'This workbook is encrypted. Configure EXCEL_PASSWORD in Railway '
            'or upload an unencrypted .xlsx file.')

    try:
        office_file.load_key(password=password, verify_password=True)
        dec = io.BytesIO()
        office_file.decrypt(dec)
        dec.seek(0)
    except Exception as exc:
        raise ExcelPasswordIncorrect(
            'The configured EXCEL_PASSWORD could not decrypt this workbook. '
            'Update it in Railway and try again.') from exc

    if not zipfile.is_zipfile(dec):
        raise ExcelPasswordIncorrect(
            'The configured EXCEL_PASSWORD could not decrypt this workbook. '
            'Update it in Railway and try again.')
    dec.seek(0)
    return dec

# Supplier workbooks label the same column differently per sheet; map the
# variants onto one canonical header so every sheet is read the same way.
HEADER_ALIASES = {
    'estimated completion / ready to ship date': 'Estimated Completion Date',
    'estimated completion/ready to ship date':   'Estimated Completion Date',
    'estimated ready to ship date':              'Estimated Completion Date',
    'must ship time':                            'Must Ship Date',
}

# Reference sheets in the supplier workbook that are not order lines; they are
# never imported, compared or shown.
IGNORED_SHEETS = {'TOOLING', 'LEADTIMES', 'LEAD TIMES'}


def is_ignored_sheet(name):
    return str(name).strip().upper() in IGNORED_SHEETS


def load_schedule(path):
    """Read a saved week (current / previous), leaving out reference sheets.
    Schedules saved before these sheets were skipped still contain them."""
    data = load_json(path, {})
    return {sheet: rows for sheet, rows in data.items() if not is_ignored_sheet(sheet)}

def normalize_header(value):
    text = '' if value is None else str(value)
    # NFKC turns full-width punctuation such as "（kg）" into "(kg)".
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'(\w)\(', r'\1 (', text)
    text = ' '.join(text.split())
    return HEADER_ALIASES.get(text.lower(), text)

def _is_date_header(header):
    h = header.lower()
    return 'date' in h or h.endswith(' time')

def _format_cell(value, is_date_col):
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d') if not (value.hour or value.minute or value.second)             else value.strftime('%Y-%m-%d %H:%M')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        # Many date cells are stored as plain serial numbers with "General"
        # formatting (e.g. 46106 = 2026-03-24).
        if is_date_col and 20000 <= value <= 80000:
            return (datetime(1899, 12, 30) + timedelta(days=int(value))).strftime('%Y-%m-%d')
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return fmt_date(value)

def parse_excel(file_bytes, password):
    dec = decrypt_excel(file_bytes, password)
    try:
        wb = openpyxl.load_workbook(dec, data_only=True, read_only=True)
    except (zipfile.BadZipFile, openpyxl.utils.exceptions.InvalidFileException) as exc:
        raise InvalidExcelFile('The uploaded file is not a valid .xlsx workbook.') from exc
    result = {}
    for sheet_name in wb.sheetnames:
        if is_ignored_sheet(sheet_name):
            continue
        ws = wb[sheet_name]
        raw_rows =[list(r) for r in ws.iter_rows(values_only=True)
                    if any(c is not None and str(c).strip() for c in r)]
        if not raw_rows:
            result[sheet_name] = []
            continue
        width = max(len(r) for r in raw_rows)
        headers = [normalize_header(h) for h in raw_rows[0]] + [''] * (width - len(raw_rows[0]))
        # Drop spacer columns: no header and no data in any row.
        keep = [i for i in range(width)
                if headers[i] or any(i < len(r) and r[i] is not None and str(r[i]).strip()
                                     for r in raw_rows[1:])]
        date_cols = {i for i in keep if _is_date_header(headers[i])}
        rows = [[headers[i] for i in keep]]
        for r in raw_rows[1:]:
            rows.append([_format_cell(r[i] if i < len(r) else None, i in date_cols)
                         for i in keep])
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
            else:
                # Both PO and item differing is a different order line, not a
                # typo (e.g. PO-4507/DFCT151000F vs PO-4257/DFCT151200F).
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
    # Typo matches are advisory only: a row that disappeared is still treated
    # as fully shipped (the supplier removes rows once everything has shipped).
    fully_shipped_keys = genuine_shipped
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

def persist_fully_shipped_jobs(previous, current, shipped_rows, week_label):
    """Persist every fully shipped job; only jobs with QA BRT evidence complete."""
    inspections = load_json(INSPECTIONS_CACHE, {})
    persisted = 0
    pending = 0
    completed_at = datetime.now().isoformat()

    with db_conn() as conn:
        for sheet, rows in shipped_rows.items():
            source = current.get(sheet) or previous.get(sheet) or []
            if not source:
                continue
            headers = list(source[0])
            headers_lower = [str(header).lower() for header in headers]

            def get_col(name, row):
                try:
                    idx = headers_lower.index(name.lower())
                    return str(row[idx]) if idx < len(row) else ''
                except ValueError:
                    return ''

            for row in rows:
                job_key = make_job_key(sheet, row, headers)
                if not job_key.split('|')[1]:
                    continue
                has_report = bool(inspections.get(job_key))
                complete_value = 1 if has_report else 0
                complete_time = completed_at if has_report else None

                conn.execute(
                    'INSERT INTO outstanding_jobs '
                    '(job_key,sheet,headers_json,row_json,week_label,order_number,'
                    ' item_code,item_desc,supplier,quantity,est_completion,must_ship,'
                    ' completed,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(job_key) DO UPDATE SET '
                    'sheet=excluded.sheet, headers_json=excluded.headers_json, '
                    'row_json=excluded.row_json, order_number=excluded.order_number, '
                    'item_code=excluded.item_code, item_desc=excluded.item_desc, '
                    'supplier=excluded.supplier, quantity=excluded.quantity, '
                    'est_completion=excluded.est_completion, must_ship=excluded.must_ship, '
                    'completed=MAX(outstanding_jobs.completed, excluded.completed), '
                    'completed_at=CASE WHEN outstanding_jobs.completed=1 '
                    'THEN outstanding_jobs.completed_at ELSE excluded.completed_at END',
                    (job_key, sheet, json.dumps(headers), json.dumps(list(row)),
                     week_label,
                     get_col('order number', row)
                     or get_col('daemco purchase order', row),
                     get_col('item code', row),
                     get_col('item description', row),
                     get_col('supplier', row) or get_col('foundry', row),
                     get_col('quantity', row),
                     get_col('estimated completion date', row),
                     get_col('must ship date', row),
                     complete_value, complete_time))
                persisted += 1
                if not has_report:
                    pending += 1

    return persisted, pending

# ── Google API ────────────────────────────────────────────────────────────────

def get_google_services():
    encoded = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON_B64', '')
    if not GOOGLE_AVAILABLE or not encoded:
        return None, None
    info = json.loads(base64.b64decode(encoded).decode('utf-8'))
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
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
    current = load_schedule(CURRENT_FILE)
    previous = load_schedule(PREVIOUS_FILE)
    if current and previous:
        statuses, typo_flags, shipped_rows = compute_changes(previous, current)
    else:
        statuses, typo_flags, shipped_rows = {}, [], {}
    if not g.is_admin:
        # Prices never reach inspectors (not even via search).
        current, shipped_rows = strip_admin_only_columns(current, shipped_rows)
    config = load_config()
    inspections = load_json(INSPECTIONS_CACHE, {})

    sheet_names = list(current.keys())
    selected_sheet = request.args.get('sheet', '')
    if selected_sheet not in current:
        selected_sheet = sheet_names[0] if sheet_names else ''
    search = request.args.get('q', '').strip()
    view = request.args.get('view', 'active')
    if view not in {'active', 'shipped', 'all'}:
        view = 'active'
    status_filter = request.args.get('status', '')
    if status_filter not in {
            'new', 'not_shipped', 'partially_shipped', 'shipped', 'vtrust',
            'no_qa_brt'}:
        status_filter = ''
    try:
        page = max(1, int(request.args.get('page', '1')))
    except ValueError:
        page = 1
    try:
        per_page = min(200, max(1, int(request.args.get('per_page', '100'))))
    except ValueError:
        per_page = 100

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
            'SELECT * FROM outstanding_jobs WHERE completed=1 ORDER BY completed_at DESC LIMIT 100'
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

    display_data = {}
    display_shipped = {}
    total_results = 0
    total_pages = 1
    if selected_sheet and current.get(selected_sheet):
        headers = current[selected_sheet][0]
        active_rows = current[selected_sheet][1:]
        selected_shipped = shipped_rows.get(selected_sheet, [])
        entries = []
        if view in {'active', 'all'}:
            entries.extend(('active', row) for row in active_rows)
        if view in {'shipped', 'all'}:
            entries.extend(('shipped', row) for row in selected_shipped)
        if search:
            needle = search.casefold()
            entries = [
                entry for entry in entries
                if any(needle in str(cell).casefold() for cell in entry[1])
            ]
        if status_filter:
            def matches_status(entry):
                kind, row = entry
                job_key = make_job_key(selected_sheet, row, headers)
                status = 'shipped' if kind == 'shipped' else statuses.get(job_key, '')
                if status_filter == 'no_qa_brt':
                    return qa_brt_missing(status, inspections.get(job_key, []))
                if status_filter != 'vtrust':
                    return status == status_filter
                records = inspections.get(job_key, [])
                vtrust_result = get_vtrust_status(records).lower()
                return (
                    job_key in valve_keys
                    and status in {'shipped', 'partially_shipped'}
                    and vtrust_result != 'pass'
                )
            entries = [entry for entry in entries if matches_status(entry)]
        total_results = len(entries)
        total_pages = max(1, math.ceil(total_results / per_page))
        page = min(page, total_pages)
        page_entries = entries[(page - 1) * per_page:page * per_page]
        display_data[selected_sheet] = [headers] + [
            row for kind, row in page_entries if kind == 'active'
        ]
        display_shipped[selected_sheet] = [
            row for kind, row in page_entries if kind == 'shipped'
        ]

    with db_conn() as _c:
        est_overrides = {r['job_key']: dict(r) for r in _c.execute('SELECT * FROM est_overrides')}
    return render_template('index.html',
                           est_overrides=est_overrides,
                           data=display_data,
                           statuses=statuses,
                           typo_flags=[t for t in typo_flags if t.get('sheet') == selected_sheet][:100],
                           shipped_rows=display_shipped,
                           inspections=inspections,
                           valve_keys=valve_keys,
                           upload_date=config.get('upload_date', ''),
                           google_configured=bool(config.get('sheet_id') and google_credentials_configured()),
                           outstanding_jobs=outstanding_jobs,
                           outstanding_by_sheet=outstanding_by_sheet,
                           completed_jobs=completed_jobs,
                           kpi_total=kpi_total,
                           kpi_done=kpi_done,
                           kpi_overdue=kpi_overdue,
                           today_str=today_str,
                           sheet_names=sheet_names,
                           selected_sheet=selected_sheet,
                           search=search,
                           view=view,
                           status_filter=status_filter,
                           page=page,
                           per_page=per_page,
                           total_results=total_results,
                           total_pages=total_pages)

@app.route('/export/comparison.xlsx')
def export_comparison_excel():
    current = load_schedule(CURRENT_FILE)
    previous = load_schedule(PREVIOUS_FILE)
    inspections = load_json(INSPECTIONS_CACHE, {})
    statuses, _, shipped_rows = (
        compute_changes(previous, current)
        if current and previous else ({}, [], {}))
    if not g.is_admin:
        current, shipped_rows = strip_admin_only_columns(current, shipped_rows)

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    header_fill = openpyxl.styles.PatternFill('solid', fgColor='1A3A5C')
    header_font = openpyxl.styles.Font(color='FFFFFF', bold=True)
    status_fills = {
        'NEW': 'D1FAE5', 'NOT SHIPPED': 'FEE2E2',
        'PARTIAL': 'FEF3C7', 'SHIPPED': 'E5E7EB',
    }

    for sheet_name, rows in current.items():
        if not rows:
            continue
        headers = list(rows[0])
        worksheet = workbook.create_sheet(title=str(sheet_name)[:31])
        worksheet.append([
            'Comparison Status', 'QA BRT Alert', 'Inspection Status',
            'V-Trust Result', *headers])
        for cell in worksheet[1]:
            cell.fill = header_fill
            cell.font = header_font

        entries = [('active', row) for row in rows[1:]]
        entries.extend(('shipped', row) for row in shipped_rows.get(sheet_name, []))
        for kind, row in entries:
            job_key = make_job_key(sheet_name, row, headers)
            status = 'shipped' if kind == 'shipped' else statuses.get(job_key, '')
            status_label = {
                'new': 'NEW', 'not_shipped': 'NOT SHIPPED',
                'partially_shipped': 'PARTIAL', 'shipped': 'SHIPPED',
                'typo': 'TYPO?',
            }.get(status, status.upper())
            reports = inspections.get(job_key, [])
            worksheet.append([
                status_label,
                'No QA BRT' if qa_brt_missing(status, reports) else '',
                reports[-1].get('result', '') if reports else 'Pending',
                get_vtrust_status(reports),
                *list(row),
            ])
            row_number = worksheet.max_row
            if status_label in status_fills:
                worksheet.cell(row_number, 1).fill = openpyxl.styles.PatternFill(
                    'solid', fgColor=status_fills[status_label])
            if worksheet.cell(row_number, 2).value:
                worksheet.cell(row_number, 2).fill = openpyxl.styles.PatternFill(
                    'solid', fgColor='FEE2E2')
                worksheet.cell(row_number, 2).font = openpyxl.styles.Font(
                    color='DC2626', bold=True)

        _format_export_sheet(worksheet)

    history = workbook.create_sheet(title='Fully Shipped History')
    history.append([
        'Status', 'QA BRT Alert', 'Region', 'Order Number', 'Item Code',
        'Description', 'Supplier', 'Quantity', 'Shipped Week', 'Completed',
        'Completed At'])
    for cell in history[1]:
        cell.fill = header_fill
        cell.font = header_font
    with db_conn() as conn:
        history_rows = conn.execute(
            'SELECT * FROM outstanding_jobs ORDER BY shipped_at DESC').fetchall()
    for job in history_rows:
        missing = not bool(job['completed'])
        history.append([
            'SHIPPED', 'No QA BRT' if missing else '', job['sheet'],
            job['order_number'], job['item_code'], job['item_desc'],
            job['supplier'], job['quantity'], job['week_label'],
            'YES' if job['completed'] else 'NO', job['completed_at'] or '',
        ])
        if missing:
            history.cell(history.max_row, 2).fill = openpyxl.styles.PatternFill(
                'solid', fgColor='FEE2E2')
            history.cell(history.max_row, 2).font = openpyxl.styles.Font(
                color='DC2626', bold=True)
    _format_export_sheet(history)

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name=f'production-schedule-comparison-{datetime.now():%Y-%m-%d}.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

def _format_export_sheet(worksheet):
    worksheet.freeze_panes = 'A2'
    worksheet.auto_filter.ref = worksheet.dimensions
    for column in worksheet.columns:
        width = min(
            45, max(12, max(len(str(cell.value or '')) for cell in column) + 2))
        worksheet.column_dimensions[column[0].column_letter].width = width

ONTIME_MIN_SAMPLE = 5   # below this the on-time rate is shown as indicative only


@app.route('/dashboard')
def dashboard():
    current     = load_schedule(CURRENT_FILE)
    previous    = load_schedule(PREVIOUS_FILE)
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
    seen_all = set()      # every order counted on this page
    for sheet in current.keys():
        rows      = current.get(sheet, [])
        prev_rows = previous.get(sheet, [])
        stats = dict(total=0, new=0, not_shipped=0, partially_shipped=0,
                     shipped=0, typo=0, qa_violations=0, vtrust_violations=0,
                     insp_pass=0, insp_fail=0, insp_partial=0, insp_pending=0)

        if rows and len(rows) >= 2:
            headers = rows[0]
            seen_jk = set()   # deduplicate split rows – count each order+item once
            for row in rows[1:]:
                jk = make_job_key(sheet, row, headers)
                if not jk.split('|')[1] or jk in seen_jk:
                    continue
                seen_jk.add(jk)
                status = statuses.get(jk, 'new')
                seen_all.add(jk)
                if status == 'typo':          # a suspected-typo row is still a new order
                    stats['new'] += 1
                    stats['typo'] += 1
                else:
                    stats[status] += 1
                stats['total'] += 1
                if status in ('partially_shipped',):
                    if not inspections.get(jk):
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
            seen_all.add(jk)
            if not inspections.get(jk):
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
        sc['rated'] = rated
        sc['small_sample'] = 0 < rated < ONTIME_MIN_SAMPLE
        inspector_stats.append((name, sc))

    outside = {k: v for k, v in inspections.items() if v and k not in seen_all}
    inspections_outside = {'jobs': len(outside), 'reports': sum(len(v) for v in outside.values())}
    inspections_all = {'jobs': sum(1 for v in inspections.values() if v),
                       'reports': sum(len(v) for v in inspections.values())}

    prev_total       = totals['not_shipped'] + totals['partially_shipped'] + totals['shipped']
    curr_in_schedule = totals['new']         + totals['not_shipped']       + totals['partially_shipped']

    # ── Weekly region trend data for chart ───────────────────────────────
    with db_conn() as conn:
        _snap_rows = conn.execute(
            'SELECT rowid AS rid, week_label, week_date, region, total_orders '
            'FROM weekly_snapshots ORDER BY rid').fetchall()

    # One point per ISO week = the last upload of that week (several uploads in
    # a week, e.g. corrections, must not look like several weeks).
    _by_week = {}
    for r in _snap_rows:
        try:
            y, w, _ = datetime.strptime(r['week_date'], '%Y-%m-%d').isocalendar()
        except (TypeError, ValueError):
            continue
        _by_week.setdefault((y, w), {})[r['region']] = (r['week_date'], r['total_orders'])
    _weeks = sorted(_by_week)
    _chart_labels = [f"{y}-W{w:02d}" for y, w in _weeks]
    _chart_dates = [max(d for d, _ in _by_week[k].values()) for k in _weeks]
    _all_regions = sorted({reg for v in _by_week.values() for reg in v})
    _series = {reg: [(_by_week[k].get(reg) or (None, None))[1] for k in _weeks] for reg in _all_regions}
    # the newest point is the schedule on screen: use the same numbers as the cards
    if _weeks:
        _live = {sheet: s['new'] + s['not_shipped'] + s['partially_shipped'] for sheet, s in region_stats.items()}
        for reg in _all_regions:
            if reg in _live:
                _series[reg][-1] = _live[reg]
            elif reg not in current:
                _series[reg][-1] = None
    _chart_datasets = [{'region': reg, 'data': _series[reg]} for reg in _all_regions
                       if any(v for v in _series[reg])]

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
                           chart_dates=_chart_dates,
                           chart_datasets=_chart_datasets,
                           ontime_min=ONTIME_MIN_SAMPLE,
                           inspections_all=inspections_all,
                           inspections_outside=inspections_outside)


def _upload_block_reason(data):
    """Return an error message if this parsed schedule must not be applied."""
    previous_data = load_schedule(PREVIOUS_FILE)
    if previous_data and data == previous_data:
        return tr('已阻止上传：该文件与已保存的上周排期相同，上传会颠倒新增/已出货状态，破坏当前对比。',
                  'Upload blocked: this file matches the saved previous-week '
                  'schedule. Uploading it would reverse NEW/SHIPPED statuses and '
                  'corrupt the current comparison.')
    with db_conn() as conn:
        prior_upload = conn.execute(
            'SELECT upload_date FROM schedule_uploads WHERE fingerprint=?',
            (schedule_fingerprint(data),)
        ).fetchone()
    if prior_upload:
        label = prior_upload['upload_date'] or tr('更早的一次上传', 'an earlier upload')
        return tr(f'已阻止上传：该排期之前已上传过（{label}）。请只上传最新一周的排期。',
                  f'Upload blocked: this schedule was already uploaded ({label}). '
                  'Only upload the latest weekly schedule.')
    return None


def _row_details(sheet, row, headers):
    hlow = [str(h).lower() for h in headers]

    def col(name):
        return str(row[hlow.index(name)]) if name in hlow and hlow.index(name) < len(row) else ''

    return {
        'sheet': sheet,
        'order_number': col('order number'),
        'po': col('daemco purchase order') or col('purchase order'),
        'item_code': col('item code'),
        'description': col('item description'),
        'quantity': col('quantity'),
        'est_completion': col('estimated completion date'),
    }


def schedule_diff_summary(current, new):
    """Describe what applying `new` on top of `current` would change."""
    summary = {'sheets': [], 'new': [], 'shipped': [], 'partial': [], 'typo': [],
               'date_changes': [], 'warnings': [], 'first_upload': not current}
    if not current:
        for sheet, rows in new.items():
            summary['sheets'].append({'name': sheet, 'rows': max(0, len(rows) - 1),
                                      'new': max(0, len(rows) - 1), 'shipped': 0,
                                      'partial': 0, 'added': [], 'removed': []})
        return summary

    statuses, typo_flags, _ = compute_changes(current, new)
    old_rows, new_rows = {}, {}
    for source, target in ((current, old_rows), (new, new_rows)):
        for sheet, rows in source.items():
            if not rows:
                continue
            for row in rows[1:]:
                key = make_job_key(sheet, row, rows[0])
                target.setdefault(key, _row_details(sheet, row, rows[0]))

    per_sheet = defaultdict(lambda: {'new': 0, 'shipped': 0, 'partial': 0})
    for key, status in statuses.items():
        sheet = key.split('|', 1)[0]
        if status in ('new', 'typo'):
            per_sheet[sheet]['new'] += 1
            summary['new'].append(new_rows.get(key, {'sheet': sheet, 'item_code': key}))
        elif status == 'partially_shipped':
            per_sheet[sheet]['partial'] += 1
            detail = dict(new_rows.get(key, {'sheet': sheet, 'item_code': key}))
            detail['previous_quantity'] = old_rows.get(key, {}).get('quantity', '')
            summary['partial'].append(detail)
    summary['typo'] = typo_flags
    # A row that disappeared entirely means the job fully shipped.
    for key, detail in old_rows.items():
        if key not in new_rows and key.split('|')[1]:
            per_sheet[detail['sheet']]['shipped'] += 1
            summary['shipped'].append(detail)

    for key, detail in new_rows.items():
        old = old_rows.get(key)
        if old and est_changed(old['est_completion'], detail['est_completion']):
            summary['date_changes'].append(dict(detail, previous_est=old['est_completion']))

    for sheet in list(new.keys()) + [s for s in current if s not in new]:
        new_sheet_rows = new.get(sheet) or []
        old_sheet_rows = current.get(sheet) or []
        new_headers = list(new_sheet_rows[0]) if new_sheet_rows else []
        old_headers = list(old_sheet_rows[0]) if old_sheet_rows else []
        info = {
            'name': sheet,
            'rows': max(0, len(new_sheet_rows) - 1),
            'previous_rows': max(0, len(old_sheet_rows) - 1),
            'added': [h for h in new_headers if h and h not in old_headers] if old_headers else [],
            'removed': [h for h in old_headers if h and h not in new_headers] if new_headers else [],
            **per_sheet[sheet],
        }
        summary['sheets'].append(info)
        if sheet not in current:
            summary['warnings'].append(tr(f'新 sheet「{sheet}」', f'New sheet "{sheet}"'))
        if sheet not in new and info['shipped']:
            summary['warnings'].append(tr(
                f'sheet「{sheet}」在新文件中不存在，其中 {info["shipped"]} 行将全部视为已出货',
                f'Sheet "{sheet}" is missing: all its rows would be treated as shipped'))
        lower = [str(h).lower() for h in new_headers]
        if new_sheet_rows and len(new_sheet_rows) > 1 and 'item code' not in lower:
            summary['warnings'].append(tr(
                f'sheet「{sheet}」缺少 Item Code 列', f'Sheet "{sheet}" has no Item Code column'))
        if info['previous_rows'] >= 10 and info['rows'] >= 10 \
                and info['shipped'] >= 0.8 * info['previous_rows'] \
                and info['new'] >= 0.8 * info['rows']:
            summary['warnings'].append(tr(
                f'sheet「{sheet}」几乎所有行都变成"新增+已出货"，可能是列名或单号格式变了',
                f'Sheet "{sheet}": almost every row looks new AND shipped – '
                'a key column was probably renamed'))
    return summary


def has_pending_upload():
    return os.path.exists(PENDING_UPLOAD_FILE)

app.jinja_env.globals['has_pending_upload'] = has_pending_upload


def _discard_pending_upload():
    pending = load_json(PENDING_UPLOAD_FILE, {})
    raw = pending.get('raw_file')
    if raw:
        try:
            os.remove(os.path.join(HISTORY_DIR, raw))
        except OSError:
            pass
    if os.path.exists(PENDING_UPLOAD_FILE):
        os.remove(PENDING_UPLOAD_FILE)


@app.route('/upload', methods=['POST'])
def upload_excel():
    if 'file' not in request.files:
        flash(tr('请选择文件', 'No file selected'), 'error')
        return redirect(url_for('index'))

    f = request.files['file']
    if _upload_extension(f.filename) != '.xlsx':
        flash(tr('只支持 .xlsx 格式的排期文件', 'Only .xlsx schedule files are supported'), 'error')
        return redirect(url_for('index'))

    try:
        file_bytes = f.read()
        data = parse_excel(file_bytes, EXCEL_PASSWORD)
        blocked = _upload_block_reason(data)
        if blocked:
            flash(blocked, 'error')
            return redirect(url_for('index'))

        _discard_pending_upload()
        os.makedirs(HISTORY_DIR, exist_ok=True)
        raw_name = f"pending-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.xlsx"
        with open(os.path.join(HISTORY_DIR, raw_name), 'wb') as out:
            out.write(file_bytes)
        save_json(PENDING_UPLOAD_FILE, {
            'data': data,
            'filename': _display_filename(f.filename),
            'raw_file': raw_name,
            'uploaded_by': g.get('username', ''),
            'uploaded_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        })
    except ExcelUploadError as exc:
        logger.warning('Schedule upload rejected: %s', exc)
        flash(str(exc), 'error')
        return redirect(url_for('index'))
    except Exception:
        tb = traceback.format_exc()
        logger.error('Upload failed:\n%s', tb)
        _last_error['tb'] = tb
        _last_error['time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        flash(tr('无法读取上传的排期，请检查文件格式和加密密码。', 'Unable to read the uploaded schedule. Check the file format and encryption password.'), 'error')
        return redirect(url_for('index'))

    return redirect(url_for('upload_preview'))


def _est_col(headers):
    low = [str(h).lower() for h in headers]
    return low.index('estimated completion date') if 'estimated completion date' in low else -1


def _apply_est_overrides(data, commit=True):
    """Keep manual completion-date corrections on top of a fresh upload.
    While the supplier's cell still equals the value that was corrected, the
    corrected value stays; once the supplier changes it, the correction is
    dropped (commit=True) and the supplier's new value wins.
    Returns (data, applied_count)."""
    with db_conn() as conn:
        overrides = {r['job_key']: dict(r) for r in conn.execute('SELECT * FROM est_overrides')}
    if not overrides:
        return data, 0
    applied, stale = 0, set()
    for sheet, rows in data.items():
        if not rows or len(rows) < 2:
            continue
        idx = _est_col(rows[0])
        if idx < 0:
            continue
        for row in rows[1:]:
            if idx >= len(row):
                continue
            ov = overrides.get(make_job_key(sheet, row, rows[0]))
            if not ov:
                continue
            if not est_changed(row[idx], ov['original']):
                row[idx] = ov['corrected']
                applied += 1
            else:
                stale.add(ov['job_key'])
    if commit and stale:
        with db_conn() as conn:
            for jk in stale:
                conn.execute('DELETE FROM est_overrides WHERE job_key=?', (jk,))
    return data, applied


@app.route('/schedule/est', methods=['POST'])
def schedule_est_edit():
    """Lead inspector / admin corrects a (typo) estimated completion date.
    Notifies the task notification e-mails and the assigned inspector."""
    if not g.can_assign:
        return tr('无权限访问此页面', 'Forbidden'), 403
    job_key = request.form.get('job_key', '')
    new_est = ' '.join(request.form.get('est', '').split())
    back = request.form.get('next', '')
    if not back.startswith('/') or back.startswith('//'):
        back = url_for('index')
    if not new_est or len(new_est) > 100:
        flash(tr('预计完成日不能为空（最多 100 字符）', 'Completion date is required (max 100 characters)'), 'error')
        return redirect(back)

    data = load_json(CURRENT_FILE, {})
    old_est, hit, row_info = None, False, {}
    for sheet, rows in data.items():
        if not rows or len(rows) < 2 or is_ignored_sheet(sheet):
            continue
        idx = _est_col(rows[0])
        if idx < 0:
            continue
        headers_low = [str(h).lower() for h in rows[0]]
        for row in rows[1:]:
            if idx < len(row) and make_job_key(sheet, row, rows[0]) == job_key:
                if old_est is None:
                    old_est = str(row[idx])
                    def cell(name, row=row, hl=headers_low):
                        return str(row[hl.index(name)]) if name in hl and hl.index(name) < len(row) else ''
                    row_info = dict(region=sheet, order_number=cell('order number'),
                                    item_code=cell('item code'), must_ship=cell('must ship date'))
                row[idx] = new_est
                hit = True
    if not hit:
        flash(tr('在当前排期中找不到该订单', 'Job not found in the current schedule'), 'error')
        return redirect(back)
    if old_est == new_est:
        return redirect(back)

    save_json(CURRENT_FILE, data)
    config = load_config()
    by = g.get('display_name') or g.get('username', '')
    with db_conn() as conn:
        prev = conn.execute('SELECT original FROM est_overrides WHERE job_key=?', (job_key,)).fetchone()
        original = prev['original'] if prev else old_est
        if est_changed(original, new_est):
            conn.execute(
                'INSERT INTO est_overrides (job_key, original, corrected, edited_by) VALUES (?,?,?,?) '
                'ON CONFLICT(job_key) DO UPDATE SET corrected=excluded.corrected, '
                'edited_by=excluded.edited_by, edited_at=datetime(\'now\',\'localtime\')',
                (job_key, original, new_est, g.get('username', '')))
        else:
            conn.execute('DELETE FROM est_overrides WHERE job_key=?', (job_key,))
        conn.execute('UPDATE inspection_tasks SET est_completion=? WHERE job_key=?', (new_est, job_key))
        conn.execute('UPDATE outstanding_jobs SET est_completion=? WHERE job_key=?', (new_est, job_key))
        conn.execute(
            'INSERT INTO task_date_changes (job_key,old_est,new_est,old_ship,new_ship,week_label) '
            'VALUES (?,?,?,?,?,?)',
            (job_key, old_est, new_est, row_info['must_ship'], row_info['must_ship'],
             config.get('upload_date', '')))
        task = conn.execute('SELECT * FROM inspection_tasks WHERE job_key=?', (job_key,)).fetchone()
    change = dict(task) if task else dict(job_key=job_key, assigned_to=None, **{
        k: row_info[k] for k in ('region', 'order_number', 'item_code')})
    change.update(old_est=old_est, new_est=new_est,
                  old_ship=row_info['must_ship'], new_ship=row_info['must_ship'])
    ok, msg = _send_date_change_email([change], edited_by=by)
    flash(tr(f'预计完成日已更新为 {new_est}。邮件通知：{msg}',
             f'Completion date updated to {new_est}. E-mail: {msg}'), 'success' if ok else 'warning')
    return redirect(back)


@app.route('/upload/preview')
def upload_preview():
    pending = load_json(PENDING_UPLOAD_FILE, {})
    if not pending.get('data'):
        flash(tr('没有待确认的排期上传', 'No schedule upload is waiting for confirmation'), 'warning')
        return redirect(url_for('index'))
    import copy
    shown, _ = _apply_est_overrides(copy.deepcopy(pending['data']), commit=False)
    summary = schedule_diff_summary(load_schedule(CURRENT_FILE), shown)
    return render_template('upload_preview.html', pending=pending, summary=summary,
                           config=load_config())


@app.route('/upload/cancel', methods=['POST'])
def upload_cancel():
    _discard_pending_upload()
    flash(tr('已取消本次上传，网站数据未改变', 'Upload cancelled – nothing was changed'), 'info')
    return redirect(url_for('index'))


@app.route('/upload/confirm', methods=['POST'])
def upload_confirm():
    pending = load_json(PENDING_UPLOAD_FILE, {})
    data = pending.get('data')
    if not data:
        flash(tr('没有待确认的排期上传', 'No schedule upload is waiting for confirmation'), 'warning')
        return redirect(url_for('index'))
    blocked = _upload_block_reason(data)
    if blocked:
        _discard_pending_upload()
        flash(blocked, 'error')
        return redirect(url_for('index'))

    baseline = request.form.get('baseline') == '1'
    try:
        _archive_upload(pending)
        _apply_schedule(data, baseline=baseline)
    except Exception:
        tb = traceback.format_exc()
        logger.error('Applying schedule failed:\n%s', tb)
        _last_error['tb'] = tb
        _last_error['time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        flash(tr('应用排期时出错，请联系管理员', 'Applying the schedule failed'), 'error')
        return redirect(url_for('index'))
    if os.path.exists(PENDING_UPLOAD_FILE):
        os.remove(PENDING_UPLOAD_FILE)
    return redirect(url_for('index'))


def _archive_upload(pending):
    """Keep every applied upload: the original workbook plus the schedule it
    replaced, so any week can be inspected or restored later."""
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    folder = os.path.join(HISTORY_DIR, stamp)
    os.makedirs(folder, exist_ok=True)
    raw = pending.get('raw_file')
    if raw and os.path.exists(os.path.join(HISTORY_DIR, raw)):
        os.replace(os.path.join(HISTORY_DIR, raw), os.path.join(folder, 'source.xlsx'))
    save_json(os.path.join(folder, 'schedule.json'), pending['data'])
    if os.path.exists(CURRENT_FILE):
        shutil.copy(CURRENT_FILE, os.path.join(folder, 'replaced_schedule.json'))
    save_json(os.path.join(folder, 'meta.json'), {
        'filename': pending.get('filename', ''),
        'uploaded_by': pending.get('uploaded_by', ''),
        'uploaded_at': pending.get('uploaded_at', ''),
        'applied_by': g.get('username', ''),
        'applied_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    })


def _apply_schedule(data, baseline=False):
    """Make `data` the current week. With baseline=True (first sync after a
    long gap) rows that disappeared are not turned into QA BRT alerts and no
    task e-mail is sent."""
    if os.path.exists(CURRENT_FILE):
        shutil.copy(CURRENT_FILE, PREVIOUS_FILE)

    data, kept_corrections = _apply_est_overrides(data)
    if kept_corrections:
        flash(tr(f'{kept_corrections} 个预计完成日沿用了主管的手动修正（供应商表格里仍是原值）',
                 f'{kept_corrections} completion date(s) kept the lead\'s manual correction (supplier file unchanged)'),
              'info')
    save_json(CURRENT_FILE, data)
    remember_schedule_upload(data, datetime.now().strftime('%d %b %Y %H:%M'))

    # ── Detect header changes vs previous week (poka-yoke) ───────────
    prev_for_check = load_schedule(PREVIOUS_FILE)
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
            if added:   parts.append(tr('新增列: ', 'Added: ') + ', '.join(f'"{h}"' for h in added))
            if removed: parts.append(tr('移除列: ', 'Removed: ') + ', '.join(f'"{h}"' for h in removed))
            header_warnings.append(f'[{sheet}] ' + '; '.join(parts))
    if header_warnings:
        flash(tr('⚠ 列结构与上次不同，请核实列名变更是否符合预期：',
                 '⚠ Columns changed since the last upload — please check: ')
              + ' | '.join(header_warnings), 'warning')

    config = load_config()
    config['upload_date'] = datetime.now().strftime('%d %b %Y %H:%M')
    config['last_upload_baseline'] = bool(baseline)
    save_json(CONFIG_FILE, config)

    # ── Save weekly snapshot for trend chart ─────────────────────────
    _snap_label = config['upload_date']
    _snap_date  = datetime.now().strftime('%Y-%m-%d')
    with db_conn() as conn:
        for _sheet, _rows in data.items():
            _count = count_unique_jobs(_sheet, _rows)
            if _count > 0:
                conn.execute(
                    'INSERT OR REPLACE INTO weekly_snapshots '
                    '(week_label, week_date, region, total_orders) VALUES (?,?,?,?)',
                    (_snap_label, _snap_date, _sheet, _count))

    # ── Auto-create inspection tasks + persist outstanding jobs ──────
    previous = load_schedule(PREVIOUS_FILE)
    current_data = load_schedule(CURRENT_FILE)
    if current_data:
        if previous:
            statuses, _, newly_shipped = compute_changes(previous, current_data)
        else:
            # First upload: every row is new.
            statuses, newly_shipped = {}, {}
            for sheet, rows in current_data.items():
                for row in (rows or [])[1:]:
                    statuses[make_job_key(sheet, row, rows[0])] = 'new'

        # Keep all fully shipped jobs in history. Missing QA BRT reports
        # remain outstanding until an inspection is submitted.
        if not baseline:
            _, pending_shipped = persist_fully_shipped_jobs(
                previous, current_data, newly_shipped, config.get('upload_date', ''))
            if pending_shipped:
                flash(tr(f'{pending_shipped} 个已全部出货的订单缺少 QA BRT 检验报告',
                         f'{pending_shipped} fully shipped job(s) require a QA BRT inspection report'),
                      'warning')
        new_tasks_created = []
        date_changes = []
        tasks_updated = 0
        seen_this_upload = set()
        with db_conn() as conn:
            existing_keys = {r[0] for r in conn.execute(
                'SELECT job_key FROM inspection_tasks').fetchall()}
            for sheet, rows in current_data.items():
                if not rows or len(rows) < 2:
                    continue
                headers = rows[0]
                hlow = [x.lower() for x in headers]
                def gcol(name, row):
                    try:
                        return str(row[hlow.index(name.lower())])
                    except (ValueError, IndexError):
                        return ''
                for row in rows[1:]:
                    jk = make_job_key(sheet, row, headers)
                    if jk in seen_this_upload:
                        continue  # split lot: same PO + item on several rows
                    seen_this_upload.add(jk)
                    if jk in existing_keys:
                        # Suppliers move completion / ship dates week to week;
                        # keep open tasks in step with the latest schedule and
                        # remember what changed so the inspectors are told.
                        est, ship, qty = (gcol('estimated completion date', row),
                                          gcol('must ship date', row), gcol('quantity', row))
                        sup = gcol('supplier', row) or gcol('foundry', row)
                        if sup:   # fill a missing supplier on any task, finished ones included
                            conn.execute("UPDATE inspection_tasks SET supplier=? "
                                         "WHERE job_key=? AND IFNULL(supplier, '')=''", (sup, jk))
                        before = conn.execute(
                            "SELECT * FROM inspection_tasks WHERE job_key=? "
                            "AND IFNULL(status, '') NOT IN ('Completed', 'Closed')", (jk,)).fetchone()
                        if not before:
                            continue
                        # Supplier: correct from the sheet, never blank it out
                        new_sup = sup if sup and sup != (before['supplier'] or '') else None
                        if (before['est_completion'] or '') == est and (before['must_ship'] or '') == ship \
                                and (before['quantity'] or '') == qty and new_sup is None:
                            continue
                        conn.execute(
                            "UPDATE inspection_tasks SET est_completion=?, must_ship=?, quantity=?, "
                            "supplier=COALESCE(?, supplier) WHERE job_key=?",
                            (est, ship, qty, new_sup, jk))
                        tasks_updated += 1
                        if est_changed(before['est_completion'], est) or \
                                est_changed(before['must_ship'], ship):
                            conn.execute(
                                'INSERT INTO task_date_changes (job_key,old_est,new_est,old_ship,'
                                'new_ship,week_label) VALUES (?,?,?,?,?,?)',
                                (jk, before['est_completion'], est, before['must_ship'], ship,
                                 config.get('upload_date', '')))
                            date_changes.append(dict(
                                dict(before), old_est=before['est_completion'], new_est=est,
                                old_ship=before['must_ship'], new_ship=ship))
                        continue
                    if statuses.get(jk) in ('new', 'typo'):
                        task = dict(
                            job_key=jk, order_number=gcol('order number', row),
                            region=sheet, item_code=gcol('item code', row),
                            description=gcol('item description', row),
                            supplier=gcol('supplier', row) or gcol('foundry', row),
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

        if date_changes and not baseline:
            ok, msg = _send_date_change_email(date_changes)
            flash(tr(f'{len(date_changes)} 个任务的预计完成日/出货日有变动。邮件通知：{msg}',
                     f'{len(date_changes)} task(s) had date changes. E-mail: {msg}'),
                  'success' if ok else 'warning')
        if not baseline:
            run_daily_reminders()

        updated_note = f'，{tasks_updated} 个任务日期/数量已同步' if tasks_updated else ''
        updated_note_en = f', {tasks_updated} task(s) updated' if tasks_updated else ''
        if new_tasks_created and not baseline:
            ok, msg = _send_task_email(new_tasks_created)
            if ok:
                flash(tr(f'排期已更新，{len(new_tasks_created)} 个新任务已创建{updated_note}，{msg}', f'Schedule updated: {len(new_tasks_created)} new task(s){updated_note_en}. {msg}'), 'success')
            else:
                flash(tr(f'排期已更新，{len(new_tasks_created)} 个新任务已创建{updated_note}。邮件通知：{msg}', f'Schedule updated: {len(new_tasks_created)} new task(s){updated_note_en}. E-mail: {msg}'), 'warning')
        else:
            flash(tr(f'排期已更新，{len(new_tasks_created)} 个新任务已创建{updated_note}',
                     f'Schedule updated: {len(new_tasks_created)} new task(s){updated_note_en}'), 'success')
    else:
        flash(tr('排期已更新', 'Schedule updated'), 'success')


@app.route('/debug')
def debug_info():
    if IS_PRODUCTION:
        abort(404)
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
        info['db_error'] = 'database check failed'
    return f'<pre style="white-space:pre-wrap;font-size:13px">{json.dumps(info, indent=2, ensure_ascii=False)}</pre>'

def find_job(job_key):
    """Job details for a job_key: current schedule, manual orders, or a
    fully shipped job kept in outstanding_jobs. Returns a dict or None."""
    current = load_schedule(CURRENT_FILE)
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

            # Fully shipped jobs remain inspectable after they leave both
            # current and previous schedule files.
            if not job_info:
                shipped_job = conn.execute(
                    'SELECT sheet, headers_json, row_json FROM outstanding_jobs '
                    'WHERE job_key=?', (job_key,)
                ).fetchone()
                if shipped_job:
                    try:
                        headers = json.loads(shipped_job['headers_json'] or '[]')
                        row = json.loads(shipped_job['row_json'] or '[]')
                    except (TypeError, json.JSONDecodeError):
                        headers, row = [], []
                    job_info = dict(zip(headers, row))
                    job_info['region'] = shipped_job['sheet']
                    job_info['job_key'] = job_key

    return job_info


@app.route('/inspect/<path:job_key>')
def inspect_form(job_key):
    job_info = find_job(job_key)
    if not job_info:
        flash(tr('找不到该订单', 'Job not found'), 'error')
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
                           report_emails=report_email_status(job_key),
                           reviews=review_status(job_key),
                           is_valve=valve_flag,
                           defect_groups=defect_groups,
                           matched_tpl=matched_tpl,
                           auto_inspectors=auto_inspectors,
                           matched_category=matched_category,
                           evidence_reqs=evidence_reqs,
                           now_date=datetime.now().strftime('%Y-%m-%d'),
                           google_configured=bool(config.get('sheet_id') and google_credentials_configured()))

INSPECTION_RESULTS = {'Pass', 'Fail', 'Partial Pass'}

@app.route('/inspect/<path:job_key>/submit', methods=['POST'])
def submit_inspection(job_key):
    form = request.form

    # ── Validate before anything is written ──────────────────────────────
    errors = []
    if form.get('result', '') not in INSPECTION_RESULTS:
        errors.append(tr('请选择总体检验结果', 'Please choose an overall result'))
    if not form.get('inspector_name', '').strip():
        errors.append(tr('请填写检验员', 'Inspector name is required'))
    if not form.get('inspection_date', '').strip():
        errors.append(tr('请填写检验日期', 'Inspection date is required'))
    bad_files = [
        f.filename for key in request.files if key.startswith('ev_file_')
        for f in request.files.getlist(key)
        if f.filename and _upload_extension(f.filename) not in EVIDENCE_EXTENSIONS
    ]
    if bad_files:
        errors.append(tr('不支持的文件类型：', 'Unsupported file type: ') + ', '.join(bad_files))
    if errors:
        for error in errors:
            flash(error, 'error')
        return redirect(url_for('inspect_form', job_key=job_key))

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
        'quantity_passed':   form.get('quantity_passed', ''),
        'result':            form.get('result', ''),
        'defect_codes':      request.form.getlist('defect_codes'),
        'defects':           form.get('defects', ''),
        'packing_condition': form.get('packing_condition', ''),
        'marking':           form.get('marking', ''),
        'notes':             form.get('notes', ''),
        'submitted_by':      g.get('username', ''),
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

    job_dir = os.path.join(UPLOAD_DIR, hashlib.sha256(job_key.encode('utf-8')).hexdigest())
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
                orig_name, saved_name = _save_uploaded_file(
                    uploaded_file, job_dir, EVIDENCE_EXTENSIONS)
                file_path = os.path.join(job_dir, saved_name)
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
    current_sched = load_schedule(CURRENT_FILE)
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

    _update_task_after_inspection(job_key, inspection_data['result'])
    # HQ only receives the report once a reviewer has approved it.
    _send_review_request_email(job_key, len(cache[job_key]) - 1, inspection_data)

    # Mark outstanding_jobs entry as completed
    with db_conn() as conn:
        conn.execute(
            'UPDATE outstanding_jobs SET completed=1, completed_at=? WHERE job_key=? AND completed=0',
            (datetime.now().isoformat(), job_key))

    # Try Google Sheets
    ok, msg = append_inspection_to_sheet(inspection_data, all_file_links)

    if ok:
        flash(tr('检验报告已保存并同步到 Google Sheets', 'Inspection saved to Google Sheets'), 'success')
    else:
        flash(tr('检验报告已保存。', 'Inspection saved.') + (tr('（未同步 Google：', ' (Google Sheets not synced: ') + msg + tr('）', ')') if g.is_admin else ''), 'success')

    return redirect(url_for('inspect_form', job_key=job_key))

def report_number(job_key, record, index):
    day = (record.get('inspection_date') or record.get('submitted_at') or '')[:10].replace('-', '')
    return f"QC-{day or 'NODATE'}-{hashlib.sha1(job_key.encode('utf-8')).hexdigest()[:6].upper()}-{index + 1}"


def _latest_checklist(job_key):
    """Latest digital checklist for the job, flattened for the PDF."""
    with db_conn() as conn:
        resp = conn.execute(
            'SELECT * FROM form_responses WHERE job_key=? ORDER BY submitted_at DESC, id DESC LIMIT 1',
            (job_key,)).fetchone()
        if not resp:
            return None
        tpl = conn.execute('SELECT sections_json FROM form_templates WHERE id=?',
                           (resp['template_id'],)).fetchone()
    try:
        sections = json.loads(tpl['sections_json'] or '[]') if tpl else []
        answers = json.loads(resp['answers'] or '{}')
    except (TypeError, json.JSONDecodeError):
        sections, answers = [], {}
    rows = []
    for si, sect in enumerate(sections):
        for qi, q in enumerate(sect.get('questions', [])):
            ans = answers.get(f'{si}_{qi}', {})
            label = ' '.join(str(x) for x in (q.get('part'), q.get('num')) if x)
            rows.append((sect.get('name', '') + (f' · {label}' if label else ''),
                         q.get('guideline', ''), ans.get('result', ''), ans.get('notes', '')))
    return {'name': resp['template_name'], 'inspector': resp['inspector'], 'date': resp['insp_date'],
            'overall': resp['overall'], 'summary': resp['summary'], 'rows': rows}


def _pdf_font_embedded():
    try:
        from pdf_report import font_is_embedded
        return font_is_embedded()
    except Exception:
        logger.exception('PDF font check failed')
        return False


def build_report_pdf(job_key, index, generated_by=''):
    """Return (pdf_bytes, report_no, filename, record) or None."""
    records = load_json(INSPECTIONS_CACHE, {}).get(job_key, [])
    if not 0 <= index < len(records):
        return None
    record = records[index]
    job = find_job(job_key) or {
        'region': record.get('region'), 'Order Number': record.get('order_number'),
        'Item Code': record.get('item_code'), 'Item Description': record.get('item_description'),
        'Supplier': record.get('supplier'), 'Quantity': record.get('quantity_ordered')}

    with db_conn() as conn:
        attachments = [dict(a) for a in conn.execute(
            'SELECT * FROM inspection_attachments WHERE job_key=? AND insp_index=? ORDER BY id',
            (job_key, index)).fetchall()]
        defect_names = {r['code']: (r['name'], r['name_cn'] or '') for r in conn.execute(
            'SELECT code, name, name_cn FROM defect_codes').fetchall()}
        review = conn.execute('SELECT * FROM inspection_reviews WHERE job_key=? AND insp_index=?',
                              (job_key, index)).fetchone()

    from pdf_report import build_inspection_pdf
    report_no = report_number(job_key, record, index)
    pdf = build_inspection_pdf(
        job, record, report_no,
        attachments=attachments,
        defect_names=defect_names,
        evidence_labels={k: (v.get('label_zh') or v['label'], v['label']) for k, v in EVIDENCE_META.items()},
        checklist=_latest_checklist(job_key),
        logo_path=os.path.join(BASE_DIR, 'static', 'daemco_logo.png'),
        generated_by=generated_by, review=dict(review) if review else None)
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', f"{record.get('order_number') or ''}_{record.get('item_code') or ''}")
    return pdf, report_no, f'{report_no}_{safe}.pdf'.replace('__', '_'), record


@app.route('/inspect/<path:job_key>/report.pdf')
def inspection_report_pdf(job_key):
    records = load_json(INSPECTIONS_CACHE, {}).get(job_key, [])
    if not records:
        abort(404)
    try:
        index = int(request.args.get('i', len(records) - 1))
    except ValueError:
        abort(404)
    built = build_report_pdf(job_key, index, g.get('display_name') or g.get('username', ''))
    if not built:
        abort(404)
    pdf, _report_no, filename, _record = built
    response = send_file(io.BytesIO(pdf), mimetype='application/pdf',
                         as_attachment=request.args.get('download') == '1',
                         download_name=filename)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


HQ_REPORT_MODES = ('all', 'issues', 'off')
MAX_EMAIL_ATTACHMENT = 15 * 1024 * 1024


def hq_report_wanted(result):
    config = load_config()
    mode = config.get('hq_report_mode', 'all')
    if mode == 'off' or not _email_list(config.get('hq_report_emails', '')):
        return False
    return mode == 'all' or result in ('Fail', 'Partial Pass')


def _send_report_email(log_id, job_key, index, sent_by, links):
    """Build the PDF and e-mail it to HQ; record the outcome in report_emails.
    Runs in a background thread (no request context)."""
    status, detail = 'failed', ''
    try:
        recipients = _email_list(load_config().get('hq_report_emails', ''))
        built = build_report_pdf(job_key, index, sent_by)
        if not built:
            detail = 'inspection not found'
        else:
            pdf, report_no, filename, rec = built
            result = rec.get('result', '')
            result_zh = {'Pass': '合格', 'Fail': '不合格', 'Partial Pass': '部分合格'}.get(result, '')
            subject = (f"【检验报告 Inspection Report】{rec.get('order_number', '')} "
                       f"{rec.get('item_code', '')} — {result_zh} {result} ({report_no})")
            lines = [
                f"报告编号 Report No.: {report_no}",
                f"区域 Region: {rec.get('region', '')}",
                f"订单 Order: {rec.get('order_number', '')}    物料 Item: {rec.get('item_code', '')}",
                f"描述 Description: {rec.get('item_description', '')}",
                f"检验员 Inspector: {rec.get('inspector_name', '')}    日期 Date: {rec.get('inspection_date', '')}",
                f"抽检 / 合格 Inspected / passed: {rec.get('quantity_inspected', '')} / {rec.get('quantity_passed', '') or '—'}",
                f"结果 Result: {result_zh} {result}",
            ]
            if rec.get('defect_codes'):
                lines.append(f"缺陷代码 Defect codes: {', '.join(rec['defect_codes'])}")
            if rec.get('notes'):
                lines += ['', f"备注 Notes: {rec['notes']}"]
            lines += ['', f"在线查看 View online: {links['inspect']}"]
            attachments = []
            if len(pdf) <= MAX_EMAIL_ATTACHMENT:
                attachments.append((filename, pdf, 'application/pdf'))
            else:
                lines.append(f"PDF 过大未附上，请在线下载 PDF too large to attach — download: {links['pdf']}")
            lines += ['', '— Daemco QC 系统自动发送 / sent automatically by the Daemco QC system']
            ok, detail = _smtp_send(subject, '\n'.join(lines), recipients, attachments)
            status = 'sent' if ok else 'failed'
            detail = detail if not ok else ', '.join(recipients)
    except Exception as exc:  # never let a background send crash silently
        logger.exception('HQ report e-mail failed')
        detail = type(exc).__name__
    with db_conn() as conn:
        conn.execute('UPDATE report_emails SET status=?, detail=? WHERE id=?', (status, detail[:500], log_id))


def queue_report_email(job_key, index):
    """Log and send the HQ e-mail for one inspection (background thread)."""
    recipients = _email_list(load_config().get('hq_report_emails', ''))
    sent_by = g.get('display_name') or g.get('username', '')
    links = {'inspect': url_for('inspect_form', job_key=job_key, _external=True),
             'pdf': url_for('inspection_report_pdf', job_key=job_key, i=index, _external=True)}
    with db_conn() as conn:
        cur = conn.execute(
            'INSERT INTO report_emails (job_key, insp_index, recipients, status, created_by) '
            'VALUES (?,?,?,?,?)', (job_key, index, ', '.join(recipients), 'pending', g.get('username', '')))
        log_id = cur.lastrowid
    args = (log_id, job_key, index, sent_by, links)
    if app.config.get('SEND_EMAIL_SYNC'):
        _send_report_email(*args)
    else:
        import threading
        threading.Thread(target=_send_report_email, args=args, daemon=True).start()


def review_status(job_key):
    with db_conn() as conn:
        rows = conn.execute('SELECT * FROM inspection_reviews WHERE job_key=?', (job_key,)).fetchall()
    return {r['insp_index']: r for r in rows}


def report_email_status(job_key):
    """{insp_index: latest report_emails row}"""
    with db_conn() as conn:
        rows = conn.execute('SELECT * FROM report_emails WHERE job_key=? ORDER BY id', (job_key,)).fetchall()
    return {r['insp_index']: r for r in rows}


@app.route('/inspect/<path:job_key>/report/<int:index>/email', methods=['POST'])
def resend_report_email(job_key, index):
    if not g.can_assign:
        abort(403)
    if not 0 <= index < len(load_json(INSPECTIONS_CACHE, {}).get(job_key, [])):
        abort(404)
    if not _email_list(load_config().get('hq_report_emails', '')):
        flash(tr('请先在设置中填写总部报告邮箱', 'Set the HQ report e-mails in Settings first'), 'error')
    elif _review_of(job_key, index) is None or _review_of(job_key, index)['status'] != 'approved':
        flash(tr('报告需先审核通过才能发送给总部', 'The report must be approved before it is sent to HQ'), 'error')
    else:
        queue_report_email(job_key, index)
        flash(tr('正在发送检验报告给总部…', 'Sending the inspection report to HQ…'), 'success')
    return redirect(url_for('inspect_form', job_key=job_key))


def _review_of(job_key, index):
    with db_conn() as conn:
        return conn.execute('SELECT * FROM inspection_reviews WHERE job_key=? AND insp_index=?',
                            (job_key, index)).fetchone()


def _send_review_request_email(job_key, index, record):
    """Ask the lead (task notification e-mails) to review a new report."""
    recipients = _email_list(load_config().get('task_notify_emails', ''))
    if not recipients:
        return
    link = url_for('inspect_form', job_key=job_key, _external=True)
    result = record.get('result', '')
    lines = [f"有检验报告待审核 An inspection report is waiting for review",
             f"订单 Order: {record.get('order_number', '')}    物料 Item: {record.get('item_code', '')}",
             f"检验员 Inspector: {record.get('inspector_name', '')}    结果 Result: {result}",
             '', link]
    subject = (f"【待审核】{record.get('order_number', '')} {record.get('item_code', '')} — "
               f"{result} / report awaiting review")
    body = '\n'.join(lines)
    if app.config.get('SEND_EMAIL_SYNC'):
        _smtp_send(subject, body, recipients)
    else:
        import threading
        threading.Thread(target=_smtp_send, args=(subject, body, recipients), daemon=True).start()


@app.route('/inspect/<path:job_key>/report/<int:index>/review', methods=['POST'])
def review_inspection(job_key, index):
    """Lead / admin approves or returns a submitted inspection report."""
    if not g.can_assign:
        abort(403)
    records = load_json(INSPECTIONS_CACHE, {}).get(job_key, [])
    if not 0 <= index < len(records):
        abort(404)
    record = records[index]
    action = request.form.get('action', '')
    comment = ' '.join(request.form.get('comment', '').split())[:500]
    if action not in ('approve', 'reject'):
        abort(400)
    if action == 'reject' and not comment:
        flash(tr('退回时请填写原因', 'Please give a reason when returning a report'), 'error')
        return redirect(url_for('inspect_form', job_key=job_key))
    status = 'approved' if action == 'approve' else 'rejected'
    me = g.get('username', '')
    with db_conn() as conn:
        conn.execute(
            'INSERT INTO inspection_reviews (job_key, insp_index, status, reviewer, reviewer_name, '
            'reviewed_at, comment, self_review) VALUES (?,?,?,?,?,?,?,?) '
            'ON CONFLICT(job_key, insp_index) DO UPDATE SET status=excluded.status, '
            'reviewer=excluded.reviewer, reviewer_name=excluded.reviewer_name, '
            'reviewed_at=excluded.reviewed_at, comment=excluded.comment, self_review=excluded.self_review',
            (job_key, index, status, me, g.get('display_name') or me,
             datetime.now().strftime('%Y-%m-%d %H:%M'), comment,
             1 if me and me == record.get('submitted_by') else 0))
    if status == 'approved':
        if hq_report_wanted(record.get('result', '')):
            queue_report_email(job_key, index)
            flash(tr('已审核通过，正在发送检验报告给总部…', 'Approved. Sending the report to HQ…'), 'success')
        else:
            flash(tr('已审核通过', 'Approved'), 'success')
    else:
        _notify_report_returned(job_key, record, comment)
        flash(tr('已退回，已通知检验员', 'Returned to the inspector'), 'success')
    return redirect(url_for('inspect_form', job_key=job_key))


def _notify_report_returned(job_key, record, comment):
    with db_conn() as conn:
        u = conn.execute("SELECT email FROM users WHERE username=? AND active=1 AND email != ''",
                         (record.get('submitted_by', ''),)).fetchone()
    recipients = _email_list(', '.join(_email_list(load_config().get('task_notify_emails', ''))
                                       + ([u['email']] if u else [])))
    if not recipients:
        return
    by = g.get('display_name') or g.get('username', '')
    _smtp_send(f"【报告被退回】{record.get('order_number', '')} {record.get('item_code', '')} / report returned",
               '\n'.join([f"检验报告被 {by} 退回 / Returned by {by}", f"原因 Reason: {comment}", '',
                          url_for('inspect_form', job_key=job_key, _external=True)]), recipients)


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    config = load_config()
    if request.method == 'POST':
        config['sheet_id'] = request.form.get('sheet_id', '').strip()
        config['drive_folder_id'] = request.form.get('drive_folder_id', '').strip()
        raw_prefixes = request.form.get('valve_prefixes', 'RSV')
        config['valve_prefixes'] = [p.strip().upper() for p in raw_prefixes.split(',') if p.strip()]
        config['task_notify_emails'] = ', '.join(_email_list(request.form.get('task_notify_emails', '')))
        config['hq_report_emails'] = ', '.join(_email_list(request.form.get('hq_report_emails', '')))
        if 'schedule_hidden_columns' in request.form:
            config['schedule_hidden_columns'] = [
                ' '.join(c.split()).lower()
                for c in re.split(r'[,\n]', request.form['schedule_hidden_columns']) if c.strip()]
        mode = request.form.get('hq_report_mode', 'all')
        config['hq_report_mode'] = mode if mode in HQ_REPORT_MODES else 'all'
        for legacy_secret in ('smtp_pass', 'smtp_user', 'smtp_host', 'smtp_port'):
            config.pop(legacy_secret, None)
        save_json(CONFIG_FILE, config)

        flash(tr('设置已保存', 'Settings saved'), 'success')
        return redirect(url_for('settings'))

    return render_template('settings.html',
                           config=config,
                           modules=MODULES,
                           credentials_exist=google_credentials_configured(),
                           smtp_configured=smtp_configured(),
                           pdf_font_embedded=_pdf_font_embedded(),
                           excel_password_configured=bool(EXCEL_PASSWORD))

@app.route('/settings/modules', methods=['POST'])
def settings_modules():
    config = load_config()
    config['modules'] = {name: request.form.get(f'module_{name}') == '1' for name in MODULES}
    save_json(CONFIG_FILE, config)
    flash(tr('功能模块设置已保存', 'Module settings saved'), 'success')
    return redirect(url_for('settings'))

@app.route('/settings/office-locations/add', methods=['POST'])
def office_location_add():
    f = request.form
    name = f.get('loc_name', '').strip()
    try:
        lat = float(f.get('lat', ''))
        lng = float(f.get('lng', ''))
        radius = int(f.get('radius', 500) or 500)
    except (ValueError, TypeError):
        flash(tr('经纬度格式错误', 'Invalid latitude / longitude'), 'error')
        return redirect(url_for('settings'))
    if not name:
        flash(tr('地点名称不能为空', 'Location name is required'), 'error')
        return redirect(url_for('settings'))
    config = load_config()
    config.setdefault('office_locations', []).append(
        {'name': name, 'lat': lat, 'lng': lng, 'radius': radius})
    save_json(CONFIG_FILE, config)
    flash(tr(f'打卡地点「{name}」已添加', f'Location "{name}" added'), 'success')
    return redirect(url_for('settings'))

@app.route('/settings/office-locations/<int:idx>/delete', methods=['POST'])
def office_location_delete(idx):
    config = load_config()
    locs = config.get('office_locations', [])
    if 0 <= idx < len(locs):
        removed = locs.pop(idx)
        save_json(CONFIG_FILE, config)
        flash(tr(f'地点「{removed["name"]}」已删除', f'Location "{removed["name"]}" deleted'), 'success')
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
        flash(tr('供应商已添加', 'Supplier added'), 'success')
        return redirect(url_for('suppliers'))
    return render_template('supplier_form.html', supplier=None, title=tr('新增供应商', 'New supplier'))

@app.route('/suppliers/<int:sid>/edit', methods=['GET', 'POST'])
def supplier_edit(sid):
    with db_conn() as conn:
        supplier = conn.execute('SELECT * FROM suppliers WHERE id=?', (sid,)).fetchone()
        if not supplier:
            flash(tr('找不到该供应商', 'Supplier not found'), 'error')
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
            flash(tr('供应商已更新', 'Supplier updated'), 'success')
            return redirect(url_for('suppliers'))
    return render_template('supplier_form.html', supplier=supplier, title=tr('编辑供应商', 'Edit supplier'))

@app.route('/suppliers/import-from-schedule', methods=['POST'])
def suppliers_import():
    current = load_schedule(CURRENT_FILE)
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
        flash(tr(f'从排期导入 {added} 个新供应商', f'Imported {added} new suppliers from schedule'), 'success')
    else:
        flash(tr('排期中的供应商都已在列表中', 'All suppliers from the schedule are already in the list.'), 'warning')
    return redirect(url_for('suppliers'))


@app.route('/suppliers/<int:sid>/delete', methods=['POST'])
def supplier_delete(sid):
    with db_conn() as conn:
        conn.execute('DELETE FROM suppliers WHERE id=?', (sid,))
    flash(tr('供应商已删除', 'Supplier deleted'), 'success')
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
        flash(tr(f'类别「{name}」已添加', f'Category "{name}" added'), 'success')
    return redirect(url_for('products', tab='categories'))

@app.route('/products/categories/<int:cid>/delete', methods=['POST'])
def category_delete(cid):
    with db_conn() as conn:
        conn.execute('DELETE FROM product_categories WHERE id=?', (cid,))
    flash(tr('类别已删除', 'Category deleted'), 'success')
    return redirect(url_for('products', tab='categories'))

@app.route('/products/categories/<int:cid>/inspector/add', methods=['POST'])
def inspector_add(cid):
    name = request.form.get('inspector_name', '').strip()
    if name:
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO category_inspectors (category_id,inspector_name) VALUES (?,?)',
                (cid, name))
        flash(tr('检验员已添加', 'Inspector added'), 'success')
    return redirect(url_for('products', tab='inspector_map'))

@app.route('/products/categories/inspector/<int:iid>/delete', methods=['POST'])
def inspector_delete(iid):
    with db_conn() as conn:
        conn.execute('DELETE FROM category_inspectors WHERE id=?', (iid,))
    flash(tr('检验员已移除', 'Inspector removed'), 'success')
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
        flash(tr('产品已添加', 'Product added'), 'success')
        return redirect(url_for('products', tab='archive'))
    with db_conn() as conn:
        cats      = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
        sup_opts  = conn.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
    return render_template('product_form.html', product=None, cats=cats,
                           sup_opts=sup_opts, title=tr('新增产品', 'New product'))

@app.route('/products/items/<int:pid>/edit', methods=['GET', 'POST'])
def product_edit(pid):
    with db_conn() as conn:
        product = conn.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    if not product:
        flash(tr('找不到该产品', 'Product not found'), 'error')
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
        flash(tr('产品已更新', 'Product updated'), 'success')
        return redirect(url_for('products', tab='archive'))
    with db_conn() as conn:
        cats     = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
        sup_opts = conn.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
    return render_template('product_form.html', product=product, cats=cats,
                           sup_opts=sup_opts, title=tr('编辑产品', 'Edit product'))

@app.route('/products/items/<int:pid>/delete', methods=['POST'])
def product_delete(pid):
    with db_conn() as conn:
        conn.execute('DELETE FROM products WHERE id=?', (pid,))
    flash(tr('产品已删除', 'Product deleted'), 'success')
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
        flash(tr('订单已添加', 'Order added'), 'success')
        return redirect(url_for('orders'))
    with db_conn() as conn:
        suppliers = conn.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
        cats      = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
    return render_template('order_form.html', order=None, suppliers=suppliers,
                           cats=cats, title=tr('新增订单', 'New order'))

@app.route('/orders/<int:oid>/edit', methods=['GET', 'POST'])
def order_edit(oid):
    with db_conn() as conn:
        order = conn.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
    if not order:
        flash(tr('找不到该订单', 'Order not found'), 'error')
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
        flash(tr('订单已更新', 'Order updated'), 'success')
        return redirect(url_for('orders'))
    with db_conn() as conn:
        suppliers = conn.execute('SELECT id,name FROM suppliers ORDER BY name').fetchall()
        cats      = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
    return render_template('order_form.html', order=order, suppliers=suppliers,
                           cats=cats, title=tr('编辑订单', 'Edit order'))

@app.route('/orders/<int:oid>/delete', methods=['POST'])
def order_delete(oid):
    with db_conn() as conn:
        conn.execute('DELETE FROM orders WHERE id=?', (oid,))
    flash(tr('订单已删除', 'Order deleted'), 'success')
    return redirect(url_for('orders'))

@app.route('/static/product_images/<path:filename>')
def product_image(filename):
    return send_from_directory(PRODUCT_IMG_DIR, filename)

def smtp_configured():
    return bool(os.environ.get('SMTP_HOST') and os.environ.get('SMTP_USERNAME')
                and os.environ.get('SMTP_PASSWORD'))


def _smtp_send(subject, body, recipients, attachments=()):
    """Send a plain-text UTF-8 e-mail. Returns (ok, message).
    attachments: [(filename, bytes, 'maintype/subtype'), ...]

    Port 465 uses implicit TLS (common for Chinese corporate mail such as
    Aliyun / Tencent Exmail); other ports use STARTTLS.
    """
    host = os.environ.get('SMTP_HOST', '').strip()
    port = int(os.environ.get('SMTP_PORT', '587'))
    user = os.environ.get('SMTP_USERNAME', '').strip()
    pwd  = os.environ.get('SMTP_PASSWORD', '')
    sender = os.environ.get('SMTP_FROM', user).strip()
    if not all([host, user, pwd]):
        return False, tr('邮件服务未配置', 'SMTP not configured')
    if not recipients:
        return False, tr('没有收件人', 'No recipients')

    import smtplib
    from email.header import Header
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        for filename, data, mimetype in attachments:
            part = MIMEApplication(data, _subtype=mimetype.split('/', 1)[-1])
            part.add_header('Content-Disposition', 'attachment', filename=('utf-8', '', filename))
            msg.attach(part)
    else:
        msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = str(Header(subject, 'utf-8'))
    msg['From']    = sender
    msg['To']      = ', '.join(recipients)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as srv:
                srv.login(user, pwd)
                srv.sendmail(sender, recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as srv:
                srv.ehlo(); srv.starttls(); srv.login(user, pwd)
                srv.sendmail(sender, recipients, msg.as_string())
        return True, tr(f'邮件已发送至 {", ".join(recipients)}', f'E-mail sent to {", ".join(recipients)}')
    except Exception as exc:
        logger.exception('E-mail delivery failed')
        return False, tr('邮件发送失败', 'E-mail delivery failed') + f' ({type(exc).__name__})'


def _task_lines(t):
    lines = [f"[{t['region']}]  {t['order_number']}  {t['item_code']}"]
    lines.append(f"    描述 Description: {t.get('description') or '—'}    数量 Qty: {t.get('quantity') or '—'}")
    if t.get('est_completion') or t.get('must_ship'):
        lines.append(f"    预计完工 Est. completion: {t.get('est_completion') or '—'}    "
                     f"最迟出货 Must ship: {t.get('must_ship') or '—'}")
    return lines


def _send_task_email(new_tasks):
    """Tell the lead inspector (Settings → task notification e-mails) that new
    jobs arrived and need assigning. Falls back to all active employees."""
    recipients = _email_list(load_config().get('task_notify_emails', ''))
    if not recipients:
        with db_conn() as conn:
            recipients = [e['email'] for e in conn.execute(
                "SELECT email FROM employees WHERE active=1 AND email != ''").fetchall()]
    if not recipients:
        return False, tr('未设置通知邮箱', 'No notification e-mail configured')

    today = datetime.now().strftime('%Y-%m-%d')
    lines = [
        f"生产排期已更新 Production schedule updated — {today}",
        f"本次新增 {len(new_tasks)} 个检验任务，请登录系统分配检验员。",
        f"{len(new_tasks)} new inspection task(s) — please log in and assign them.",
        '',
    ]
    for i, t in enumerate(new_tasks, 1):
        task_lines = _task_lines(t)
        lines.append(f"{i}. {task_lines[0]}")
        lines.extend(task_lines[1:])
        lines.append('')
    lines.append(url_for('tasks', scope='unassigned', _external=True))
    return _smtp_send(f"【新检验任务 {len(new_tasks)} 项 / {len(new_tasks)} new inspection tasks】{today}",
                      '\n'.join(lines), recipients)


REVIEW_REMINDER_HOURS = 24


def pending_reviews():
    """Submitted reports nobody has reviewed yet, oldest first:
    [{job_key, index, record, hours}]"""
    now = datetime.now()
    with db_conn() as conn:
        done = {(r['job_key'], r['insp_index']) for r in conn.execute(
            'SELECT job_key, insp_index FROM inspection_reviews')}
    out = []
    for job_key, records in load_json(INSPECTIONS_CACHE, {}).items():
        for index, rec in enumerate(records):
            if (job_key, index) in done:
                continue
            try:
                hours = (now - datetime.fromisoformat(rec.get('submitted_at', ''))).total_seconds() / 3600
            except (TypeError, ValueError):
                hours = 0
            out.append(dict(job_key=job_key, index=index, record=rec, hours=hours))
    out.sort(key=lambda x: x['hours'], reverse=True)
    return out


def send_review_reminders():
    """Once per report: remind the lead when a report has waited 24 h+ for review."""
    overdue = [p for p in pending_reviews() if p['hours'] >= REVIEW_REMINDER_HOURS]
    claimed = []
    with db_conn() as conn:
        for p in overdue:
            if conn.execute('INSERT OR IGNORE INTO review_reminders (job_key, insp_index) VALUES (?,?)',
                            (p['job_key'], p['index'])).rowcount:
                claimed.append(p)

    def release():
        with db_conn() as conn:
            for p in claimed:
                conn.execute('DELETE FROM review_reminders WHERE job_key=? AND insp_index=?',
                             (p['job_key'], p['index']))

    if not claimed:
        return 0
    recipients = _email_list(load_config().get('task_notify_emails', ''))
    if not recipients:
        release()
        return 0
    claimed.sort(key=lambda p: (p['record'].get('result') == 'Pass', -p['hours']))   # Fail first
    lines = [f"以下 {len(claimed)} 份检验报告已超过 {REVIEW_REMINDER_HOURS} 小时未审核，总部尚未收到。",
             f"{len(claimed)} inspection report(s) have waited over {REVIEW_REMINDER_HOURS} h for review; HQ has not received them.", '']
    for i, p in enumerate(claimed, 1):
        r = p['record']
        flag = '⚠ ' if r.get('result') != 'Pass' else ''
        lines.append(f"{i}. {flag}{r.get('order_number', '')}  {r.get('item_code', '')}  — {r.get('result', '')}  "
                     f"({r.get('inspector_name', '')}, 已等待 {int(p['hours'])} 小时 / waiting {int(p['hours'])} h)")
        lines.append('    ' + url_for('inspect_form', job_key=p['job_key'], _external=True))
        lines.append('')
    n_bad = sum(1 for p in claimed if p['record'].get('result') != 'Pass')
    ok, _ = _smtp_send(
        f"【审核超时】{len(claimed)} 份检验报告待审核" + (f"（含 {n_bad} 份不合格）" if n_bad else '')
        + f" / {len(claimed)} report(s) overdue for review", '\n'.join(lines), recipients)
    if not ok:
        release()
        return 0
    return len(claimed)


def run_daily_reminders():
    return send_due_reminders(), send_review_reminders()


def _est_label(value):
    d, note = split_est(value)
    if not d:
        return (note or '—')
    return d.isoformat() + (f' ({note})' if note else '')


def _task_recipients(tasks):
    """Task notification e-mails + the assigned inspector of each task."""
    recipients = _email_list(load_config().get('task_notify_emails', ''))
    ids = {t['assigned_to'] for t in tasks if t.get('assigned_to')}
    if ids:
        with db_conn() as conn:
            marks = ','.join('?' * len(ids))
            mails = [r['email'] for r in conn.execute(
                f"SELECT email FROM users WHERE id IN ({marks}) AND active=1 AND email != ''",
                tuple(ids)).fetchall()]
        recipients = _email_list(', '.join(recipients + mails))
    return recipients


def _send_date_change_email(changes, edited_by=''):
    """Tell the lead and the assigned inspectors that completion / ship dates
    moved (earlier or later) or the remark text next to the date changed."""
    recipients = _task_recipients(changes)
    if not recipients:
        return False, tr('未设置通知邮箱', 'No notification e-mail configured')
    today = datetime.now().strftime('%Y-%m-%d')
    lines = [f"预计完成日变动 Completion date changes — {today}",
             f"{len(changes)} 个未完成任务的日期有变动 / {len(changes)} open task(s) changed.", '']
    if edited_by:
        lines.insert(2, f"由 {edited_by} 手动修改 / Edited manually by {edited_by}")
    for i, c in enumerate(changes, 1):
        lines.append(f"{i}. [{c['region']}]  {c['order_number']}  {c['item_code']}")
        old_d, _ = split_est(c['old_est'])
        new_d, _ = split_est(c['new_est'])
        if est_changed(c['old_est'], c['new_est']):
            if old_d and new_d and old_d != new_d:
                delta = (new_d - old_d).days
                trend = (f"延后 {delta} 天 / delayed {delta} d" if delta > 0
                         else f"提前 {-delta} 天 / earlier by {-delta} d")
            else:
                trend = '备注变化 / remark changed'
            lines.append(f"    预计完成 Est. completion: {_est_label(c['old_est'])}  →  "
                         f"{_est_label(c['new_est'])}   [{trend}]")
        if est_changed(c['old_ship'], c['new_ship']):
            lines.append(f"    最迟出货 Must ship: {_est_label(c['old_ship'])}  →  {_est_label(c['new_ship'])}")
        if c.get('assigned_to'):
            lines.append("    已分配 Assigned")
        lines.append('    ' + url_for('inspect_form', job_key=c['job_key'], _external=True))
        lines.append('')
    return _smtp_send(f"【日期变动】{len(changes)} 项检验任务 / {len(changes)} inspection task(s) with date changes",
                      '\n'.join(lines), recipients)


REMINDER_DAYS = 14


def send_due_reminders():
    """Once per task and completion date: e-mail when an open task is within
    two weeks of its estimated completion date. A changed date re-arms it."""
    today = date.today()
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM inspection_tasks WHERE IFNULL(status,'') NOT IN ('Completed', 'Closed')").fetchall()
        due = []
        for t in rows:
            d, _ = split_est(t['est_completion'])
            if not d or (d - today).days > REMINDER_DAYS:
                continue
            marker = d.isoformat()
            # atomic claim so concurrent workers do not both send
            cur = conn.execute(
                "UPDATE inspection_tasks SET reminder_est=? WHERE id=? AND IFNULL(reminder_est,'') != ?",
                (marker, t['id'], marker))
            if cur.rowcount:
                due.append(dict(t))
    if not due:
        return 0

    def release():
        # sending failed: re-arm so the next run retries
        with db_conn() as conn:
            for t in due:
                conn.execute("UPDATE inspection_tasks SET reminder_est='' WHERE id=?", (t['id'],))

    due.sort(key=lambda t: split_est(t['est_completion'])[0])
    recipients = _task_recipients(due)
    if not recipients:
        release()
        return 0
    lines = [f"预计完成日提醒 Completion reminder — {today.isoformat()}",
             f"以下 {len(due)} 个任务距预计完成日不足 {REMINDER_DAYS} 天（或已过期），请安排检验。",
             f"{len(due)} task(s) are within {REMINDER_DAYS} days of the estimated completion date.", '']
    for i, t in enumerate(due, 1):
        d, _ = split_est(t['est_completion'])
        left = (d - today).days
        when = f"还有 {left} 天 / in {left} d" if left >= 0 else f"已逾期 {-left} 天 / {-left} d overdue"
        lines.append(f"{i}. [{t['region']}]  {t['order_number']}  {t['item_code']}")
        lines.append(f"    预计完成 Est. completion: {_est_label(t['est_completion'])}  ({when})")
        lines.append('    ' + url_for('inspect_form', job_key=t['job_key'], _external=True))
        lines.append('')
    ok, _ = _smtp_send(f"【完成日提醒】{len(due)} 项检验任务 / {len(due)} inspection task(s) due within {REMINDER_DAYS} days",
                       '\n'.join(lines), recipients)
    if not ok:
        release()
        return 0
    return len(due)


def _send_assignment_email(tasks, assignee, note=''):
    """Notify the lead (task notification e-mails) and the assignee that
    inspection tasks were assigned."""
    recipients = _email_list(load_config().get('task_notify_emails', ''))
    if assignee and assignee['email']:
        recipients = _email_list(', '.join(recipients + [assignee['email']]))
    if not recipients:
        return False, tr('未设置通知邮箱', 'No notification e-mail configured')

    name = (assignee['display_name'] or assignee['username']) if assignee else tr('未分配', 'Unassigned')
    by = g.get('display_name') or g.get('username', '')
    lines = [
        f"检验任务分配通知 Inspection task assignment",
        f"负责人 Assigned to: {name}",
        f"分配人 Assigned by: {by}    时间 Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]
    if note:
        lines.append(f"备注 Note: {note}")
    lines.append('')
    for i, t in enumerate(tasks, 1):
        task_lines = _task_lines(t)
        lines.append(f"{i}. {task_lines[0]}")
        lines.extend(task_lines[1:])
        lines.append('    ' + url_for('inspect_form', job_key=t['job_key'], _external=True))
        lines.append('')
    lines.append(tr('请登录系统查看“我的任务”。', 'Log in to see "My tasks".') + ' '
                 + url_for('tasks', scope='mine', _external=True))
    subject = f"【任务分配】{len(tasks)} 项检验任务 → {name} / {len(tasks)} inspection task(s) assigned to {name}"
    return _smtp_send(subject, '\n'.join(lines), recipients)


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
        flash(tr('员工已添加', 'Employee added'), 'success')
        return redirect(url_for('employees'))
    with db_conn() as conn:
        cats = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
    return render_template('employee_form.html', employee=None, work_info=None,
                           all_employees=[], cats=cats, title=tr('新增员工', 'New employee'))

@app.route('/employees/<int:eid>/edit', methods=['GET', 'POST'])
def employee_edit(eid):
    with db_conn() as conn:
        emp = conn.execute('SELECT * FROM employees WHERE id=?', (eid,)).fetchone()
    if not emp:
        flash(tr('找不到该员工', 'Employee not found'), 'error')
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
        flash(tr('员工信息已更新', 'Employee updated'), 'success')
        return redirect(url_for('employees'))
    with db_conn() as conn:
        work_info = conn.execute('SELECT * FROM employee_work_info WHERE employee_id=?', (eid,)).fetchone()
        all_employees = conn.execute('SELECT id,name FROM employees WHERE active=1 AND id!=? ORDER BY name', (eid,)).fetchall()
        cats = conn.execute('SELECT id,name FROM product_categories ORDER BY name').fetchall()
    return render_template('employee_form.html', employee=emp, work_info=work_info,
                           all_employees=all_employees, cats=cats, title=tr('编辑员工', 'Edit employee'))

@app.route('/employees/<int:eid>/delete', methods=['POST'])
def employee_delete(eid):
    with db_conn() as conn:
        conn.execute('DELETE FROM employees WHERE id=?', (eid,))
    flash(tr('员工已删除', 'Employee deleted'), 'success')
    return redirect(url_for('employees'))


# ── Task routes ───────────────────────────────────────────────────────────────

@app.route('/tasks')
def tasks():
    from collections import defaultdict
    from datetime import timedelta
    status_filter = request.args.get('status', '')
    # Inspectors land on their own tasks; the lead / admin on everything.
    scope = request.args.get('scope') or ('all' if g.can_assign else 'mine')
    if scope not in ('mine', 'unassigned', 'all'):
        scope = 'all'
    today = datetime.now().date()
    reconcile_tasks_with_inspections()

    with db_conn() as conn:
        every_task = [dict(r) for r in conn.execute(
            "SELECT t.*, COALESCE(NULLIF(u.display_name, ''), u.username) AS assignee_name "
            'FROM inspection_tasks t LEFT JOIN users u ON u.id = t.assigned_to '
            'ORDER BY t.est_completion ASC, t.created_at ASC'
        ).fetchall()]
        assignees = assignable_users(conn)

    # Open tasks whose order is no longer in the current schedule (shipped or removed)
    in_schedule = set()
    for sheet, sheet_rows in load_schedule(CURRENT_FILE).items():
        for r in (sheet_rows or [])[1:]:
            in_schedule.add(make_job_key(sheet, r, sheet_rows[0]))
    for t in every_task:
        t['orphan'] = t['status'] not in DONE_STATUSES and t['job_key'] not in in_schedule

    scope_counts = {
        'mine': sum(1 for t in every_task if t['assigned_to'] == g.user_id
                    and t['status'] not in DONE_STATUSES),
        'unassigned': sum(1 for t in every_task if not t['assigned_to']
                          and t['status'] not in DONE_STATUSES),
        'all': len(every_task),
    }
    if scope == 'mine':
        all_tasks = [t for t in every_task if t['assigned_to'] == g.user_id]
    elif scope == 'unassigned':
        all_tasks = [t for t in every_task if not t['assigned_to']]
    else:
        all_tasks = every_task

    inspections = load_json(INSPECTIONS_CACHE, {})

    # Filtered view for the table
    orphan_only = request.args.get('orphan') == '1'
    orphan_count = sum(1 for t in all_tasks if t['orphan'])
    if orphan_only:
        rows = [t for t in all_tasks if t['orphan']]
    elif status_filter:
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
        if t['status'] in DONE_STATUSES:
            continue
        est_date = _parse_date(t['est_completion'])
        if not est_date:
            urg['no_date'] += 1
        elif est_date < today:
            urg['overdue'] += 1
        else:
            delta = (est_date - today).days
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
        est_date = _parse_date(t['est_completion'])
        if est_date and est_date < today and t['status'] not in DONE_STATUSES:
            region_data[r]['overdue'] += 1
    region_stats = sorted(region_data.items(), key=lambda x: x[1]['total'], reverse=True)
    max_region = max((v['total'] for _, v in region_stats), default=1)

    # By supplier (outstanding only, top 8)
    sup_data = defaultdict(lambda: {'outstanding': 0, 'completed': 0})
    for t in all_tasks:
        s = (t['supplier'] or 'Unknown').strip() or 'Unknown'
        if t['status'] == 'Completed':
            sup_data[s]['completed'] += 1
        elif t['status'] != 'Closed':
            sup_data[s]['outstanding'] += 1
    # every supplier with open tasks; the page shows the first 8 and can expand
    top_suppliers = sorted(((k, v) for k, v in sup_data.items() if v['outstanding'] > 0),
                           key=lambda x: x[1]['outstanding'], reverse=True)
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

    review_queue = pending_reviews() if g.can_assign else []
    return render_template('tasks.html', review_queue=review_queue, tasks=rows, inspections=inspections,
                           today=today, status_filter=status_filter, stats=stats,
                           orphan_only=orphan_only, orphan_count=orphan_count,
                           scope=scope, scope_counts=scope_counts, assignees=assignees)


@app.route('/tasks/close', methods=['POST'])
def task_close():
    """Lead / admin closes tasks that no longer need an inspection."""
    if not g.can_assign:
        abort(403)
    ids = [int(i) for i in request.form.getlist('task_ids') if i.isdigit()]
    back = redirect(_safe_next_url(request.form.get('next')) or url_for('tasks'))
    if not ids:
        flash(tr('请先勾选要关闭的任务', 'Select at least one task first'), 'error')
        return back
    marks = ','.join('?' * len(ids))
    with db_conn() as conn:
        cur = conn.execute(
            f"UPDATE inspection_tasks SET status='Closed' WHERE id IN ({marks}) "
            "AND IFNULL(status,'') != 'Completed'", ids)
    flash(tr(f'已关闭 {cur.rowcount} 个任务', f'Closed {cur.rowcount} task(s)'), 'success')
    return back


def assignable_users(conn):
    return conn.execute(
        "SELECT id, username, display_name, email, role FROM users "
        "WHERE active=1 AND role IN ('lead', 'inspector') "
        "ORDER BY CASE role WHEN 'lead' THEN 0 ELSE 1 END, "
        "COALESCE(NULLIF(display_name, ''), username)"
    ).fetchall()


@app.route('/tasks/assign', methods=['POST'])
def task_assign():
    if not g.can_assign:
        abort(403)
    ids = [int(i) for i in request.form.getlist('task_ids') if i.isdigit()]
    raw_assignee = request.form.get('assignee_id', '')
    note = request.form.get('note', '').strip()[:500]
    back = redirect(_safe_next_url(request.form.get('next')) or url_for('tasks'))
    if not ids:
        flash(tr('请先勾选要分配的任务', 'Select at least one task first'), 'error')
        return back

    with db_conn() as conn:
        assignee = None
        if raw_assignee:
            assignee = conn.execute(
                "SELECT id, username, display_name, email FROM users "
                "WHERE id=? AND active=1 AND role IN ('lead', 'inspector')",
                (raw_assignee,)).fetchone()
            if not assignee:
                flash(tr('无效的检验员', 'Invalid inspector'), 'error')
                return back
        placeholders = ','.join('?' * len(ids))
        tasks = conn.execute(
            f'SELECT * FROM inspection_tasks WHERE id IN ({placeholders})', ids).fetchall()
        conn.execute(
            f'UPDATE inspection_tasks SET assigned_to=?, assigned_by=?, assigned_at=?, '
            f'assign_note=? WHERE id IN ({placeholders})',
            [assignee['id'] if assignee else None, g.username,
             datetime.now().strftime('%Y-%m-%d %H:%M'), note, *ids])

    if not assignee:
        flash(tr(f'已取消 {len(tasks)} 个任务的分配', f'Unassigned {len(tasks)} task(s)'), 'success')
        return back

    name = assignee['display_name'] or assignee['username']
    ok, msg = _send_assignment_email([dict(t) for t in tasks], assignee, note)
    flash(tr(f'已将 {len(tasks)} 个任务分配给 {name}。', f'Assigned {len(tasks)} task(s) to {name}. ')
          + msg, 'success' if ok else 'warning')
    return back

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
    if new_status not in TASK_STATUSES:
        abort(400)
    with db_conn() as conn:
        task = conn.execute('SELECT assigned_to FROM inspection_tasks WHERE id=?', (tid,)).fetchone()
        if not task:
            abort(404)
        # Inspectors may only update tasks assigned to them.
        if not g.can_assign and task['assigned_to'] != g.user_id:
            abort(403)
        conn.execute('UPDATE inspection_tasks SET status=? WHERE id=?', (new_status, tid))
    return redirect(_safe_next_url(request.form.get('next')) or url_for('tasks'))


TASK_STATUSES = ('Pending', 'In Progress', 'Completed', 'On Hold', 'Closed')
# 'Closed' = closed by the lead without an inspection (order gone / not needed)
DONE_STATUSES = ('Completed', 'Closed')


def reconcile_tasks_with_inspections():
    """Keep the task list in step with the submitted inspection reports: a
    still-Pending task takes the status of its latest report, and a report
    whose job has no task gets one. Returns (updated, created)."""
    updated = created = 0
    with db_conn() as conn:
        existing = {r['job_key']: r['status'] for r in conn.execute(
            'SELECT job_key, status FROM inspection_tasks')}
        for job_key, records in load_json(INSPECTIONS_CACHE, {}).items():
            if not records:
                continue
            rec = records[-1]
            status = 'Completed' if rec.get('result') == 'Pass' else 'In Progress'
            if job_key in existing:
                if (existing[job_key] or 'Pending') == 'Pending':
                    conn.execute('UPDATE inspection_tasks SET status=? WHERE job_key=?', (status, job_key))
                    updated += 1
            else:
                conn.execute(
                    'INSERT OR IGNORE INTO inspection_tasks (job_key,order_number,region,item_code,'
                    'description,supplier,quantity,status) VALUES (?,?,?,?,?,?,?,?)',
                    (job_key, rec.get('order_number', ''), rec.get('region', ''), rec.get('item_code', ''),
                     rec.get('item_description', ''), rec.get('supplier', ''),
                     rec.get('quantity_ordered', ''), status))
                created += 1
    if updated or created:
        logger.info('Reconciled tasks with inspections: %s updated, %s created', updated, created)
    return updated, created


def _update_task_after_inspection(job_key, result):
    """An inspection report drives the task status: Pass -> Completed,
    anything else -> In Progress (needs follow-up)."""
    status = 'Completed' if result == 'Pass' else 'In Progress'
    with db_conn() as conn:
        conn.execute('UPDATE inspection_tasks SET status=? WHERE job_key=?', (status, job_key))


def _save_product_image(file_obj):
    if not file_obj or not file_obj.filename:
        return ''
    _, saved_name = _save_uploaded_file(file_obj, PRODUCT_IMG_DIR, IMAGE_EXTENSIONS)
    return saved_name


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
    flash(tr('关键词已更新', 'Keywords updated'), 'success')
    return redirect(url_for('form_templates_page'))

@app.route('/forms/<int:fid>/delete', methods=['POST'])
def form_template_delete(fid):
    with db_conn() as conn:
        conn.execute('DELETE FROM form_templates WHERE id=?', (fid,))
    flash(tr('模板已删除', 'Template deleted'), 'success')
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
    job_info = find_job(job_key)

    if not job_info:
        flash(tr('找不到该订单', 'Job not found'), 'error'); return redirect(url_for('index'))

    if not tpl:
        flash(tr('没有匹配的检验表单', 'No matching inspection form'), 'error'); return redirect(url_for('inspect_form', job_key=job_key))

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

        flash(tr('检验单已提交', 'Checklist submitted'), 'success')
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
        imgs = parse_json_list(a['images'])
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
        flash(tr('找不到该文章', 'Article not found'), 'error'); return redirect(url_for('knowledge'))
    images = parse_json_list(article['images'])
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
        flash(tr('文章已添加', 'Article added'), 'success'); return redirect(url_for('knowledge'))
    return render_template('kb_form.html', article=None, cats=cats, title='新增知识库文章')

@app.route('/knowledge/article/<int:aid>/edit', methods=['GET', 'POST'])
def kb_article_edit(aid):
    with db_conn() as conn:
        article = conn.execute('SELECT * FROM kb_articles WHERE id=?', (aid,)).fetchone()
        cats    = conn.execute('SELECT * FROM kb_categories ORDER BY name').fetchall()
    if not article:
        flash(tr('找不到该文章', 'Article not found'), 'error'); return redirect(url_for('knowledge'))
    if request.method == 'POST':
        f = request.form
        with db_conn() as conn:
            conn.execute(
                'UPDATE kb_articles SET category_id=?,title=?,content=?,tags=?,'
                'updated_at=datetime("now","localtime") WHERE id=?',
                (f.get('category_id') or None, f.get('title','').strip(),
                 f.get('content','').strip(), f.get('tags','').strip(), aid))
        flash(tr('文章已更新', 'Article updated'), 'success'); return redirect(url_for('kb_article', aid=aid))
    return render_template('kb_form.html', article=article, cats=cats, title='编辑文章')

@app.route('/knowledge/article/<int:aid>/delete', methods=['POST'])
def kb_article_delete(aid):
    with db_conn() as conn:
        conn.execute('DELETE FROM kb_articles WHERE id=?', (aid,))
    flash(tr('文章已删除', 'Article deleted'), 'success'); return redirect(url_for('knowledge'))

@app.route('/knowledge/categories/new', methods=['POST'])
def kb_category_new():
    name = request.form.get('name','').strip()
    if name:
        with db_conn() as conn:
            conn.execute('INSERT INTO kb_categories (name,description) VALUES (?,?)',
                         (name, request.form.get('description','').strip()))
        flash(tr(f'类别「{name}」已添加', f'Category "{name}" added'), 'success')
    return redirect(url_for('knowledge'))

@app.route('/knowledge/categories/<int:cid>/delete', methods=['POST'])
def kb_category_delete(cid):
    with db_conn() as conn:
        conn.execute('DELETE FROM kb_categories WHERE id=?', (cid,))
    flash(tr('类别已删除', 'Category deleted'), 'success'); return redirect(url_for('knowledge'))


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
    flash(tr('题目已添加', 'Question added'), 'success'); return redirect(url_for('training', tab='questions'))

@app.route('/training/questions/<int:qid>/delete', methods=['POST'])
def question_delete(qid):
    with db_conn() as conn:
        conn.execute('DELETE FROM questions WHERE id=?', (qid,))
    flash(tr('题目已删除', 'Question deleted'), 'success'); return redirect(url_for('training', tab='questions'))

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
    flash(tr('试卷已创建', 'Exam created'), 'success'); return redirect(url_for('training', tab='exams'))

@app.route('/training/exams/<int:eid>/delete', methods=['POST'])
def exam_delete(eid):
    with db_conn() as conn:
        conn.execute('DELETE FROM exams WHERE id=?', (eid,))
    flash(tr('试卷已删除', 'Exam deleted'), 'success'); return redirect(url_for('training', tab='exams'))

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
    flash(tr('培训计划已创建', 'Training plan created'), 'success'); return redirect(url_for('training_plan', pid=pid))

@app.route('/training/plans/<int:pid>')
def training_plan(pid):
    with db_conn() as conn:
        plan = conn.execute(
            'SELECT tp.*, e.title AS exam_title, e.pass_score FROM training_plans tp '
            'LEFT JOIN exams e ON tp.exam_id=e.id WHERE tp.id=?', (pid,)).fetchone()
        if not plan:
            flash(tr('培训计划不存在', 'Training plan not found'), 'error'); return redirect(url_for('training'))
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
    flash(tr('分配已更新', 'Assignments updated'), 'success'); return redirect(url_for('training_plan', pid=pid))

@app.route('/training/plans/<int:pid>/delete', methods=['POST'])
def plan_delete(pid):
    with db_conn() as conn:
        conn.execute('DELETE FROM training_plans WHERE id=?', (pid,))
    flash(tr('培训计划已删除', 'Training plan deleted'), 'success'); return redirect(url_for('training'))

@app.route('/training/plans/<int:pid>/take', methods=['GET', 'POST'])
def exam_take(pid):
    with db_conn() as conn:
        plan = conn.execute(
            'SELECT tp.*, e.title AS exam_title, e.pass_score FROM training_plans tp '
            'LEFT JOIN exams e ON tp.exam_id=e.id WHERE tp.id=?', (pid,)).fetchone()
        if not plan or not plan['exam_id']:
            flash(tr('该培训计划暂无试卷', 'This training plan has no exam'), 'error'); return redirect(url_for('training'))
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
        flash(tr('名称不能为空', 'Name is required'), 'error')
        return redirect(url_for('regions'))
    with db_conn() as conn:
        conn.execute('INSERT INTO regions (name,level,parent_id,code) VALUES (?,?,?,?)',
                     (name, level, parent_id, f.get('code', '').strip()))
    flash(tr(f'已添加「{name}」', f'Added "{name}"'), 'success')
    return redirect(url_for('regions'))

@app.route('/regions/<int:rid>/edit', methods=['POST'])
def region_edit(rid):
    f = request.form
    with db_conn() as conn:
        conn.execute('UPDATE regions SET name=?,code=? WHERE id=?',
                     (f.get('name', '').strip(), f.get('code', '').strip(), rid))
    flash(tr('已更新', 'Updated'), 'success')
    return redirect(url_for('regions'))

@app.route('/regions/<int:rid>/delete', methods=['POST'])
def region_delete(rid):
    with db_conn() as conn:
        conn.execute('DELETE FROM regions WHERE id=?', (rid,))
    flash(tr('已删除', 'Deleted'), 'success')
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

    if not g.is_admin:
        employee_id = g.employee_id
        emp_list = [r for r in emp_list if r['id'] == employee_id]
        today_records = [r for r in today_records if r['employee_id'] == employee_id]
        stats_rows = [r for r in stats_rows if r['id'] == employee_id]
        leaves = [r for r in leaves if r['employee_id'] == employee_id]
        expenses = [r for r in expenses if r['employee_id'] == employee_id]

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
    emp_id = _requested_employee_id(f)
    if not emp_id:
        flash(tr('请选择员工', 'Please choose an employee'), 'error')
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
            flash(tr('入厂打卡成功', 'Checked in'), 'success')
        except Exception:
            conn.execute(
                'UPDATE attendance_records SET checkin_time=?,checkin_location=?,status=?,notes=?,checkin_lat=?,checkin_lng=?,gps_verified=? '
                'WHERE employee_id=? AND work_date=?',
                (checkin_time, location, status, notes, lat, lng, verified, emp_id, work_date))
            flash(tr('入厂记录已更新', 'Check-in updated'), 'success')
    return redirect(url_for('hr_portal', tab='attendance'))

@app.route('/hr/attendance/checkout', methods=['POST'])
def hr_checkout():
    f = request.form
    emp_id = _requested_employee_id(f)
    if not emp_id:
        flash(tr('请选择员工', 'Please choose an employee'), 'error')
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
    flash(tr('出厂打卡成功', 'Checked out'), 'success')
    return redirect(url_for('hr_portal', tab='attendance'))

@app.route('/hr/leaves/new', methods=['POST'])
def leave_new():
    f = request.form
    emp_id = _requested_employee_id(f)
    if not emp_id:
        flash(tr('请选择员工', 'Please choose an employee'), 'error')
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
    flash(tr('请假申请已提交', 'Leave request submitted'), 'success')
    return redirect(url_for('hr_portal', tab='leave'))

@app.route('/hr/leaves/<int:lid>/approve', methods=['POST'])
def leave_approve(lid):
    with db_conn() as conn:
        conn.execute("UPDATE leave_requests SET status='Approved',approver_notes=? WHERE id=?",
                     (request.form.get('notes', ''), lid))
    flash(tr('已批准', 'Approved'), 'success')
    return redirect(url_for('hr_portal', tab='leave'))

@app.route('/hr/leaves/<int:lid>/reject', methods=['POST'])
def leave_reject(lid):
    with db_conn() as conn:
        conn.execute("UPDATE leave_requests SET status='Rejected',approver_notes=? WHERE id=?",
                     (request.form.get('notes', ''), lid))
    flash(tr('已拒绝', 'Rejected'), 'warning')
    return redirect(url_for('hr_portal', tab='leave'))

@app.route('/hr/leaves/<int:lid>/delete', methods=['POST'])
def leave_delete(lid):
    with db_conn() as conn:
        conn.execute('DELETE FROM leave_requests WHERE id=?', (lid,))
    flash(tr('已删除', 'Deleted'), 'success')
    return redirect(url_for('hr_portal', tab='leave'))

@app.route('/hr/expenses/new', methods=['POST'])
def expense_new():
    f = request.form
    emp_id = _requested_employee_id(f)
    if not emp_id:
        flash(tr('请选择员工', 'Please choose an employee'), 'error')
        return redirect(url_for('hr_portal', tab='expense'))
    invoice_path = ''
    inv = request.files.get('invoice')
    if inv and inv.filename:
        _, invoice_path = _save_uploaded_file(inv, UPLOAD_DIR, INVOICE_EXTENSIONS)
    with db_conn() as conn:
        conn.execute(
            'INSERT INTO expense_claims (employee_id,claim_date,claim_type,amount,description,invoice_path) VALUES (?,?,?,?,?,?)',
            (emp_id, f.get('claim_date', ''), f.get('claim_type', 'Transport'),
             float(f.get('amount', 0) or 0), f.get('description', '').strip(), invoice_path))
    flash(tr('报销申请已提交', 'Expense claim submitted'), 'success')
    return redirect(url_for('hr_portal', tab='expense'))

@app.route('/hr/expenses/<int:eid>/approve', methods=['POST'])
def expense_approve(eid):
    with db_conn() as conn:
        conn.execute("UPDATE expense_claims SET status='Approved',approver_notes=? WHERE id=?",
                     (request.form.get('notes', ''), eid))
    flash(tr('已批准', 'Approved'), 'success')
    return redirect(url_for('hr_portal', tab='expense'))

@app.route('/hr/expenses/<int:eid>/reject', methods=['POST'])
def expense_reject(eid):
    with db_conn() as conn:
        conn.execute("UPDATE expense_claims SET status='Rejected',approver_notes=? WHERE id=?",
                     (request.form.get('notes', ''), eid))
    flash(tr('已拒绝', 'Rejected'), 'warning')
    return redirect(url_for('hr_portal', tab='expense'))

@app.route('/hr/expenses/<int:eid>/delete', methods=['POST'])
def expense_delete(eid):
    with db_conn() as conn:
        conn.execute('DELETE FROM expense_claims WHERE id=?', (eid,))
    flash(tr('已删除', 'Deleted'), 'success')
    return redirect(url_for('hr_portal', tab='expense'))

@app.route('/hr/expenses/<int:eid>/invoice')
def expense_invoice(eid):
    with db_conn() as conn:
        row = conn.execute(
            'SELECT invoice_path, employee_id FROM expense_claims WHERE id=?', (eid,)
        ).fetchone()
    if row and not g.is_admin and row['employee_id'] != g.employee_id:
        return 'Forbidden', 403
    if not row or not row['invoice_path']:
        flash(tr('无附件', 'No attachment'), 'error')
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
    flash(tr('工作信息已保存', 'Work info saved'), 'success')
    return redirect(url_for('employee_edit', eid=eid))


os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
init_db()

for _known_schedule in (load_schedule(PREVIOUS_FILE), load_schedule(CURRENT_FILE)):
    remember_schedule_upload(_known_schedule)

def _backfill_fully_shipped_history():
    previous = load_schedule(PREVIOUS_FILE)
    current = load_schedule(CURRENT_FILE)
    if not previous or not current:
        return
    if load_config().get('last_upload_baseline'):
        # The admin applied the current week as a baseline re-sync: rows
        # that vanished since the stale data must not become QA BRT alerts.
        return
    _, _, shipped_rows = compute_changes(previous, current)
    persisted, pending = persist_fully_shipped_jobs(
        previous, current, shipped_rows, load_config().get('upload_date', ''))
    if persisted:
        logger.info(
            'Backfilled %s fully shipped job(s); %s require QA BRT reports',
            persisted, pending)

def _purge_ignored_sheet_records():
    """Remove alerts / open tasks / chart points created from reference sheets
    (TOOLING, LEADTIMES) by uploads made before those sheets were skipped.
    Tasks that already have an inspection are kept."""
    names = sorted(IGNORED_SHEETS)
    marks = ','.join('?' * len(names))
    inspected = set(load_json(INSPECTIONS_CACHE, {}).keys())
    with db_conn() as conn:
        jobs = conn.execute(
            f'DELETE FROM outstanding_jobs WHERE UPPER(TRIM(sheet)) IN ({marks})', names).rowcount
        task_rows = conn.execute(
            f'SELECT id, job_key FROM inspection_tasks WHERE UPPER(TRIM(region)) IN ({marks})',
            names).fetchall()
        stale = [r['id'] for r in task_rows if r['job_key'] not in inspected]
        for task_id in stale:
            conn.execute('DELETE FROM inspection_tasks WHERE id=?', (task_id,))
        snaps = conn.execute(
            f'DELETE FROM weekly_snapshots WHERE UPPER(TRIM(region)) IN ({marks})', names).rowcount
    if jobs or stale or snaps:
        logger.info('Removed reference-sheet records: %s shipped jobs, %s tasks, %s snapshots',
                    jobs, len(stale), snaps)

try:
    _purge_ignored_sheet_records()
except Exception:
    logger.exception('Unable to purge reference-sheet records')

try:
    _backfill_fully_shipped_history()
except Exception:
    logger.exception('Unable to backfill fully shipped history')

def _bootstrap_admin():
    username = os.environ.get('BOOTSTRAP_ADMIN_USERNAME', '').strip()
    password = os.environ.get('BOOTSTRAP_ADMIN_PASSWORD', '')
    with db_conn() as conn:
        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
            return
        if not username or len(password) < 12:
            logger.warning(
                'No users exist. Set BOOTSTRAP_ADMIN_USERNAME and a '
                'BOOTSTRAP_ADMIN_PASSWORD of at least 12 characters.'
            )
            return
        conn.execute(
            'INSERT INTO users (username, password_hash, role) VALUES (?,?,?)',
            (username, generate_password_hash(password), 'admin'))
        logger.info('Bootstrap administrator created; remove BOOTSTRAP_ADMIN_PASSWORD')

_bootstrap_admin()

def _startup_safety_checks():
    """Log deployment risks and disable legacy accounts whose password equals
    the username (early versions seeded admin/admin, qc1/qc1, qc2/qc2)."""
    if IS_PRODUCTION and not os.environ.get('APP_DATA_DIR'):
        logger.error(
            'APP_DATA_DIR is not set: the database, schedules and uploads are '
            'stored inside the container and will be LOST on the next deploy. '
            'Mount a Railway Volume at /data and set APP_DATA_DIR=/data.')
    with db_conn() as conn:
        users = conn.execute(
            'SELECT id, username, password_hash, role FROM users WHERE active=1'
        ).fetchall()
        active_admins = sum(1 for u in users if u['role'] == 'admin')
        for user in users:
            if not check_password_hash(user['password_hash'], user['username']):
                continue
            if user['role'] == 'admin' and active_admins <= 1:
                logger.error('Account %r uses its username as password and is the only '
                             'admin; change its password immediately.', user['username'])
                continue
            conn.execute('UPDATE users SET active=0 WHERE id=?', (user['id'],))
            if user['role'] == 'admin':
                active_admins -= 1
            logger.warning('Disabled account %r: password equals username.', user['username'])

_startup_safety_checks()

# admin: everything; lead: inspector who also assigns tasks; inspector: own tasks
ROLES = ('admin', 'lead', 'inspector')

def _valid_email(value):
    return bool(re.fullmatch(r'[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+', value or ''))

def _email_list(value):
    """Split 'a@x.com, b@y.com; c@z.com' into valid addresses (deduplicated)."""
    seen, out = set(), []
    for part in re.split(r'[,;\s]+', value or ''):
        if _valid_email(part) and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out

@app.route('/admin/users')
def users_admin():
    with db_conn() as conn:
        users = conn.execute(
            'SELECT u.*, e.name AS employee_name FROM users u '
            'LEFT JOIN employees e ON u.employee_id=e.id ORDER BY u.username'
        ).fetchall()
        employees = conn.execute(
            'SELECT id, name FROM employees WHERE active=1 ORDER BY name'
        ).fetchall()
    return render_template('users.html', users=users, employees=employees)

@app.route('/admin/users/create', methods=['POST'])
def user_create():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'inspector')
    employee_id = request.form.get('employee_id') or None
    email = request.form.get('email', '').strip()
    display_name = request.form.get('display_name', '').strip()
    if not username or not username.replace('_', '').replace('-', '').isalnum():
        flash(tr('用户名只能包含字母、数字、下划线和连字符', 'Username may only contain letters, digits, underscores and hyphens'), 'error')
    elif len(password) < 12:
        flash(tr('密码至少需要 12 个字符', 'Password must be at least 12 characters'), 'error')
    elif role not in ROLES:
        flash(tr('无效角色', 'Invalid role'), 'error')
    elif email and not _valid_email(email):
        flash(tr('邮箱格式不正确', 'Invalid e-mail address'), 'error')
    else:
        try:
            with db_conn() as conn:
                conn.execute(
                    'INSERT INTO users (username,password_hash,role,employee_id,email,display_name) '
                    'VALUES (?,?,?,?,?,?)',
                    (username, generate_password_hash(password), role, employee_id,
                     email, display_name))
            flash(tr('账号已创建', 'Account created'), 'success')
        except Exception:
            flash(tr('用户名已存在或员工关联无效', 'Username already exists or the employee link is invalid'), 'error')
    return redirect(url_for('users_admin'))

@app.route('/admin/users/<int:uid>/toggle', methods=['POST'])
def user_toggle(uid):
    if uid == g.user_id:
        flash(tr('不能停用当前登录账号', 'You cannot disable the account you are logged in with'), 'error')
        return redirect(url_for('users_admin'))
    with db_conn() as conn:
        user = conn.execute('SELECT active,role FROM users WHERE id=?', (uid,)).fetchone()
        if user:
            if user['active'] and user['role'] == 'admin':
                active_admins = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
                ).fetchone()[0]
                if active_admins <= 1:
                    flash(tr('至少需要保留一个启用的管理员', 'At least one active admin is required'), 'error')
                    return redirect(url_for('users_admin'))
            conn.execute('UPDATE users SET active=? WHERE id=?', (0 if user['active'] else 1, uid))
            flash(tr('账号状态已更新', 'Account status updated'), 'success')
    return redirect(url_for('users_admin'))

@app.route('/admin/users/<int:uid>/reset-password', methods=['POST'])
def user_reset_password(uid):
    password = request.form.get('password', '')
    if len(password) < 12:
        flash(tr('密码至少需要 12 个字符', 'Password must be at least 12 characters'), 'error')
    else:
        with db_conn() as conn:
            conn.execute(
                'UPDATE users SET password_hash=? WHERE id=?',
                (generate_password_hash(password), uid))
        flash(tr('密码已重置', 'Password reset'), 'success')
    return redirect(url_for('users_admin'))

@app.route('/admin/users/<int:uid>/update', methods=['POST'])
def user_update(uid):
    role = request.form.get('role', 'inspector')
    employee_id = request.form.get('employee_id') or None
    email = request.form.get('email', '').strip()
    display_name = request.form.get('display_name', '').strip()
    if role not in ROLES:
        flash(tr('无效角色', 'Invalid role'), 'error')
        return redirect(url_for('users_admin'))
    if email and not _valid_email(email):
        flash(tr('邮箱格式不正确', 'Invalid e-mail address'), 'error')
        return redirect(url_for('users_admin'))
    with db_conn() as conn:
        user = conn.execute('SELECT role,active FROM users WHERE id=?', (uid,)).fetchone()
        if user and user['active'] and user['role'] == 'admin' and role != 'admin':
            active_admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
            ).fetchone()[0]
            if active_admins <= 1:
                flash(tr('至少需要保留一个启用的管理员', 'At least one active admin is required'), 'error')
                return redirect(url_for('users_admin'))
        conn.execute(
            'UPDATE users SET role=?,employee_id=?,email=?,display_name=? WHERE id=?',
            (role, employee_id, email, display_name, uid))
    flash(tr('账号资料已更新', 'Account updated'), 'success')
    return redirect(url_for('users_admin'))

@app.before_request
def _auth_check():
    if request.method == 'POST' and request.endpoint != 'cron_reminders':
        submitted = request.form.get('_csrf_token') or request.headers.get('X-CSRF-Token')
        expected = session.get('_csrf_token', '')
        if not submitted or not expected or not hmac.compare_digest(submitted, expected):
            return 'Invalid CSRF token', 400

    g.lang = _lang_from_cookie()
    public = {'healthz', 'login', 'static', 'set_language', 'cron_reminders'}
    if request.endpoint in public or request.endpoint is None:
        return None

    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login', next=request.full_path.rstrip('?')))
    with db_conn() as conn:
        user = conn.execute(
            'SELECT id, username, role, employee_id, language, display_name, email '
            'FROM users WHERE id=? AND active=1',
            (user_id,)
        ).fetchone()
    if not user:
        session.clear()
        return redirect(url_for('login'))
    if user['language'] in LANGUAGES:
        g.lang = user['language']
    g.user_id = user['id']
    g.username = user['username']
    g.role = user['role']
    g.employee_id = user['employee_id']
    g.is_admin = user['role'] == 'admin'
    g.can_assign = user['role'] in ('admin', 'lead')
    if not app.config.get('TESTING') and _last_reminder_day['day'] != date.today():
        _last_reminder_day['day'] = date.today()
        _run_reminders_in_background(request.host_url)
    g.display_name = user['display_name'] or user['username']
    if request.endpoint in ADMIN_ENDPOINTS and not g.is_admin:
        return tr('无权限访问此页面', 'Forbidden'), 403
    disabled = _disabled_module_for_path(request.path)
    if disabled:
        abort(404)

_last_reminder_day = {'day': None}


def _run_reminders_in_background(base_url):
    def work():
        try:
            with app.test_request_context(base_url=base_url):
                run_daily_reminders()
        except Exception:
            logger.exception('Due-date reminder run failed')
    import threading
    threading.Thread(target=work, daemon=True).start()


@app.route('/cron/reminders', methods=['GET', 'POST'])
def cron_reminders():
    """Token-protected hook for an external scheduler (Railway cron etc.)."""
    token = os.environ.get('CRON_SECRET', '')
    supplied = request.headers.get('X-Cron-Token', '') or request.args.get('token', '')
    if not token or not hmac.compare_digest(supplied, token):
        abort(404)
    return {'reminders_sent': send_due_reminders(), 'review_reminders_sent': send_review_reminders()}


@app.route('/lang/<code>')
def set_language(code):
    """Switch UI language; remembered per account and per browser."""
    if code not in LANGUAGES:
        abort(404)
    user_id = session.get('user_id')
    if user_id:
        with db_conn() as conn:
            conn.execute('UPDATE users SET language=? WHERE id=?', (code, user_id))
    response = redirect(_safe_next_url(request.args.get('next')) or url_for('index'))
    response.set_cookie('lang', code, max_age=365 * 24 * 3600, samesite='Lax',
                        secure=IS_PRODUCTION, httponly=True)
    return response

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        # Count failures per username+IP so one person's typos (or everyone
        # sharing an office IP) cannot lock out the whole team.
        client_id = f"{username.lower()}|{request.remote_addr or 'unknown'}"
        if _login_rate_limited(client_id):
            flash(tr('登录尝试次数过多，请稍后再试', 'Too many login attempts — please try again later'), 'error')
            return render_template('login.html'), 429
        password = request.form.get('password', '')
        with db_conn() as conn:
            user = conn.execute(
                'SELECT * FROM users WHERE username=? AND active=1', (username,)
            ).fetchone()
        if user and check_password_hash(user['password_hash'], password):
            _login_failures.pop(client_id, None)
            session.clear()
            session.permanent = True
            session['user_id'] = user['id']
            csrf_token()
            return redirect(_safe_next_url(request.args.get('next')) or url_for('index'))
        _record_login_failure(client_id)
        flash(tr('用户名或密码错误', 'Incorrect username or password'), 'error')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(self)'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if IS_PRODUCTION:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

@app.errorhandler(413)
def upload_too_large(_error):
    limit_mb = (app.config.get('MAX_CONTENT_LENGTH') or 0) // (1024 * 1024)
    return tr(f'上传文件过大（上限 {limit_mb} MB），请压缩后重试或分次提交。',
              f'Upload too large (limit {limit_mb} MB). Compress the files or submit in parts.'), 413

@app.errorhandler(500)
def internal_error(_error):
    logger.exception('Unhandled application error')
    return 'Internal server error', 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
