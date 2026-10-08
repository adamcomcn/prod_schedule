import os, io, json, hashlib, tempfile, math, logging, traceback, secrets, hmac, time, base64, uuid, zipfile, re, unicodedata, shutil
from collections import Counter, defaultdict, deque
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
import geoip

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

_last_error = {'tb': '', 'time': ''}

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

LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
_login_failures = defaultdict(deque)

ADMIN_ENDPOINTS = {
    'debug_info', 'upload_excel', 'upload_preview', 'upload_confirm', 'upload_cancel', 'settings',
    'settings_modules', 'settings_reference', 'settings_backup', 'settings_backup_email',
    'settings_test_email', 'settings_weekly_summary', 'office_location_add',
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
    'user_update', 'user_logins', 'change_report_inspector', 'settings_vtrust_preview', 'settings_vtrust_send',
    'settings_purchasing_send', 'export_purchasing_excel', 'settings_qa_mismatch_preview', 'settings_qa_mismatch_send',
    'vtrust_page', 'vtrust_book', 'vtrust_unbook',
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
    # A unique temp name per write: background threads (e-mails) may save
    # the same file at the same moment.
    tmp_path = f'{path}.{uuid.uuid4().hex}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            # Windows only (local runs): the target is briefly open in another thread
            if attempt == 19:
                os.remove(tmp_path)
                raise
            time.sleep(0.05)

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

LOGIN_EVENTS = ('login', 'failed', 'blocked', 'logout')
LOGIN_EVENT_RETENTION_DAYS = 365

def _log_login_event(event, username='', user_id=None):
    """Sign-in history for admins (/admin/logins). Must never break logging in."""
    try:
        now = datetime.now(_tz.utc)
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO login_events (created_at, user_id, username, event, ip, user_agent) '
                'VALUES (?,?,?,?,?,?)',
                (now.strftime('%Y-%m-%d %H:%M:%S'), user_id, (username or '')[:64], event,
                 (request.remote_addr or '')[:64], request.headers.get('User-Agent', '')[:300]))
            if event == 'login':
                cutoff = now - timedelta(days=LOGIN_EVENT_RETENTION_DAYS)
                conn.execute('DELETE FROM login_events WHERE created_at < ?',
                             (cutoff.strftime('%Y-%m-%d %H:%M:%S'),))
    except Exception:
        logger.exception('Unable to record login event')

def device_label(user_agent):
    """'Android · 微信' style summary of a User-Agent string."""
    ua = user_agent or ''
    system = next((name for key, name in (
        ('iPhone', 'iPhone'), ('iPad', 'iPad'), ('Android', 'Android'), ('Windows', 'Windows'),
        ('Mac OS X', 'Mac'), ('CrOS', 'ChromeOS'), ('Linux', 'Linux')) if key in ua), '')
    browser = next((name for key, name in (
        ('MicroMessenger', tr('微信', 'WeChat')), ('DingTalk', tr('钉钉', 'DingTalk')),
        ('Edg/', 'Edge'), ('OPR/', 'Opera'), ('Firefox/', 'Firefox'), ('HuaweiBrowser', tr('华为浏览器', 'Huawei')),
        ('MiuiBrowser', tr('小米浏览器', 'Xiaomi')), ('UCBrowser', 'UC'), ('QQBrowser', 'QQ'),
        ('Chrome/', 'Chrome'), ('Safari/', 'Safari')) if key in ua), '')
    return ' · '.join(p for p in (system, browser) if p) or (ua[:40] if ua else '—')

app.jinja_env.filters['device_label'] = device_label

def ip_country(ip):
    """'CN 中国' / 'CN China' for an IP address (offline DB-IP Lite database)."""
    hit = geoip.lookup(ip, APP_DATA_DIR, auto_download=not app.config.get('TESTING'))
    if not hit:
        return ''
    code, names = hit
    if code == 'LAN':
        return tr('内网', 'Local')
    name = names.get('zh-CN') if current_lang() == 'zh' else names.get('en')
    return f'{code} {name}' if name else code

app.jinja_env.filters['ip_country'] = ip_country

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

# Fixed interface language per role: the China team works in Chinese, HQ in
# English. Only admins switch.
ROLE_LANGUAGE = {'inspector': 'zh', 'lead': 'zh', 'hq': 'en'}


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
    'shipped': '已出货', 'typo': '疑似笔误', 'moved_in': '转入', 'partially_moved': '部分转出',
    'Ready to Ship': '待出货', 'In Production': '生产中', 'Shipped': '已出货',
    'Casting arrived': '铸件已到', 'Raw Castings': '毛坯铸件', 'Conditional Pass': '有条件合格',
    'Approved': '已批准', 'Rejected': '已拒绝',
}
_STATUS_LABELS_EN = {
    'new': 'New', 'not_shipped': 'Not shipped', 'partially_shipped': 'Partially shipped',
    'shipped': 'Shipped', 'typo': 'Possible typo', 'moved_in': 'Moved in', 'partially_moved': 'Partly moved out',
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

# ── Time display ─────────────────────────────────────────────────────────────
# The server runs in UTC and stores naive UTC timestamps. Web pages show them
# in each viewer's own time zone (converted in the browser); e-mails and PDFs
# have no browser, so they show China and Melbourne time side by side.
from datetime import timezone as _tz
from zoneinfo import ZoneInfo
from markupsafe import Markup, escape as _escape

REPORT_ZONES = (('北京', 'Asia/Shanghai'), ('Melbourne', 'Australia/Melbourne'))
_TIME_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%d %b %Y %H:%M')


def parse_server_time(value):
    """A stored timestamp (ISO, 'YYYY-MM-DD HH:MM[:SS]' or 'DD Mon YYYY HH:MM')
    as an aware UTC datetime, or None (dates without a time are not converted)."""
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or '').strip()
        if len(text) < 16:
            return None
        dt = None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in _TIME_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    return dt.replace(tzinfo=_tz.utc) if dt.tzinfo is None else dt.astimezone(_tz.utc)


def local_time(value):
    """<time> element converted to the viewer's time zone by base.html."""
    dt = parse_server_time(value)
    if dt is None:
        return _escape(value or '')
    utc_text = dt.strftime('%Y-%m-%d %H:%M')
    return Markup(f'<time class="js-local" datetime="{dt.isoformat()}">{utc_text} UTC</time>')


def utc_iso(value):
    dt = parse_server_time(value)
    return dt.isoformat() if dt else ''


# Business dates ("today", due / overdue, default inspection date, attendance)
# follow the factories in China, not the server clock (UTC) or the viewer.
BUSINESS_TZ = ZoneInfo('Asia/Shanghai')


def china_now():
    """Current wall-clock time in China (naive)."""
    return datetime.now(BUSINESS_TZ).replace(tzinfo=None)


def china_today():
    return china_now().date()


def dual_zone_time(value=None):
    """'2026-10-02 08:52 北京 / 10:52 Melbourne' for e-mails and PDFs."""
    dt = parse_server_time(value) if value is not None else datetime.now(_tz.utc)
    if dt is None:
        return str(value or '')
    parts, first_day = [], None
    for label, zone in REPORT_ZONES:
        local = dt.astimezone(ZoneInfo(zone))
        day = local.strftime('%Y-%m-%d')
        parts.append(f"{local.strftime('%H:%M') if day == first_day else local.strftime('%Y-%m-%d %H:%M')} {label}")
        first_day = first_day or day
    return ' / '.join(parts)


app.jinja_env.filters['localtime'] = local_time
app.jinja_env.filters['utc_iso'] = utc_iso
app.jinja_env.filters['dual_zone_time'] = dual_zone_time


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
    'pressure': {'label': 'Pressure Test Records', 'label_zh': '压力测试记录',
                 'icon': '🧪', 'color': '#1e3a8a', 'bg': '#e0e7ff',
                 'accepts': '.pdf,.xlsx,.xls,.jpg,.jpeg,.png,.heic,.mp4,.mov,image/*,video/*'},
    'xrf':      {'label': 'XRF Report (Material Composition)', 'label_zh': 'XRF 材质成分报告',
                 'icon': '⚗️', 'color': '#065f46', 'bg': '#d1fae5',
                 'accepts': '.pdf,.xlsx,.xls,.jpg,.jpeg,.png,.heic,image/*'},
}

# Chinese version of each guidance line (lines are combined per product).
import evidence_rules
import checklists


def product_reference(code):
    """Row of the imported product reference table (reference.xlsx) or None."""
    code = (code or '').strip().upper()
    if not code:
        return None
    with db_conn() as conn:
        row = conn.execute('SELECT * FROM product_reference WHERE code=?', (code,)).fetchone()
    return dict(row) if row else None


def product_type_for(item_code, description=''):
    return evidence_rules.classify(item_code, description, product_reference(item_code))


def product_type_name(ptype):
    if not ptype:
        return ''
    name = evidence_rules.PRODUCT_TYPES[ptype]['name']
    return tr(name['zh'], name['en'])


def get_evidence_requirements(item_code, description=''):
    """Evidence cards required for a product, from the agreed rule matrix
    (evidence_rules.PRODUCT_TYPES). Each card lists its check items."""
    ptype = product_type_for(item_code, description)
    if not ptype:
        return []
    en = current_lang() == 'en'
    reqs = []
    for item in evidence_rules.PRODUCT_TYPES[ptype]['evidence']:
        meta = EVIDENCE_META[item['type']]
        reqs.append({
            **meta,
            'type': item['type'],
            'label': meta['label'] if en else (meta.get('label_zh') or meta['label']),
            'checks': [c['en'] if en else c['zh'] for c in item['checks']],
            'checks_bi': item['checks'],
            'daq_limits': ([(key, label['en'] if en else label['zh'], limit)
                            for key, label, limit in evidence_rules.DAQ_LIMITS]
                           if item['type'] == 'daq' else []),
        })
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


# ── Region moves ─────────────────────────────────────────────────────────────
# The supplier sometimes moves an order line (same PO + item code) to another
# region sheet, in full or in part. The job key includes the sheet, so without
# this the old row looked "shipped" and the new one "new". Inspection reports
# are shared across regions: the goods are one production batch wherever they
# are shipped.

def _job_tail(job_key):
    """'PO|ITEM' part of a 'SHEET|PO|ITEM' job key."""
    return job_key.split('|', 1)[1] if '|' in job_key else job_key


def detect_region_moves(prev_keys, curr_keys, prev_qty, curr_qty):
    """Order lines that appeared on a new sheet while leaving (or shrinking on)
    another one. also_shipped: the total quantity went down as well, so part
    of it shipped and the usual shipped / QA BRT rules still apply."""
    groups = defaultdict(lambda: ({}, {}))
    for k in prev_keys:
        groups[_job_tail(k)][0][k.split('|', 1)[0]] = prev_qty.get(k)
    for k in curr_keys:
        groups[_job_tail(k)][1][k.split('|', 1)[0]] = curr_qty.get(k)
    moves = []
    for tail, (before, after) in sorted(groups.items()):
        if not before or not after:
            continue
        to = sorted(s for s in after if s not in before)
        if not to:
            continue
        gone = sorted(s for s in before if s not in after)
        reduced = sorted(s for s in before if s in after and before[s] is not None
                         and after[s] is not None and after[s] < before[s])
        if not gone and not reduced:
            continue                       # extra quantity for a new region, nothing left the old one
        known = all(v is not None for v in list(before.values()) + list(after.values()))
        total_before = sum(v or 0 for v in before.values())
        total_after = sum(v or 0 for v in after.values())
        po, item = tail.split('|', 1)
        moves.append({
            'po': po, 'item': item,
            'kind': 'partial' if any(s in after for s in before) else 'full',
            'also_shipped': known and total_after < total_before - 1e-9,
            'from': [{'sheet': s, 'before': before[s], 'after': after.get(s)} for s in gone + reduced],
            'to': [{'sheet': s, 'qty': after[s]} for s in to],
            'gone_keys': [f'{s}|{tail}' for s in gone],
            'reduced_keys': [f'{s}|{tail}' for s in reduced],
            'to_keys': [f'{s}|{tail}' for s in to],
            'total_before': total_before, 'total_after': total_after,
        })
    return moves


def _qty_text(value):
    if value is None:
        return '?'
    return str(int(value)) if value == int(value) else str(value)


def move_text(m):
    """'DI FITTING 48 → FIJI 36 (DI FITTING keeps 15)' style description."""
    src = ', '.join(f"{f['sheet']} {_qty_text(f['before'])}" for f in m['from'])
    dst = ', '.join(f"{t['sheet']} {_qty_text(t['qty'])}" for t in m['to'])
    kept = [f"{f['sheet']} {_qty_text(f['after'])}" for f in m['from'] if f['after'] is not None]
    text = f'{src} → {dst}'
    if kept:
        text += tr(f"（{', '.join(kept)} 保留）", f" ({', '.join(kept)} kept)")
    if m.get('also_shipped'):
        text += tr('；总数减少，部分已出货', '; total went down, part shipped')
    return text


class SharedReports(dict):
    """Inspection reports by job key; a job without reports of its own falls
    back to the reports of the same PO + item in another region."""

    def __init__(self, data):
        super().__init__(data or {})
        self._by_tail = defaultdict(list)
        for key, records in self.items():
            if records:
                self._by_tail[_job_tail(key)].extend(records)

    def get(self, key, default=None):
        own = super().get(key)
        if own:
            return own
        shared = self._by_tail.get(_job_tail(key))
        if shared:
            return shared
        return own if own is not None else default


def migrate_job_key(conn, old, new):
    """Move a job — task, reports, attachments, reviews, e-mails, date
    corrections — to its new key after the whole order line moved region.
    Refused (False) when the new key already has reports of its own."""
    cache = load_json(INSPECTIONS_CACHE, {})
    if cache.get(new):
        return False
    sheet = new.split('|', 1)[0]
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for table in tables:
        cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')]
        if 'job_key' in cols:
            conn.execute(f'UPDATE OR IGNORE "{table}" SET job_key=? WHERE job_key=?', (new, old))
    conn.execute('UPDATE inspection_tasks SET region=? WHERE job_key=?', (sheet, new))
    if old in cache:
        records = cache.pop(old)
        for record in records:
            record['job_key'] = new
            record['region'] = sheet
        cache[new] = records
        save_json(INSPECTIONS_CACHE, cache)
    return True


def handle_region_moves(conn, moves, existing_keys, week_label):
    """Record the moves; a whole line that moved takes its task (and reports)
    along; a split-off part gets a new task for the same inspector.
    Returns (followed, inherit): followed = [(move, old_key, new_key)],
    inherit = {new_key: (source task row, move)}."""
    followed, inherit = [], {}
    for m in moves:
        for f in m['from']:
            for t in m['to']:
                conn.execute(
                    'INSERT INTO region_moves (po, item_code, from_sheet, to_sheet, from_before, '
                    'from_after, to_qty, kind, also_shipped, week_label) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (m['po'], m['item'], f['sheet'], t['sheet'], f['before'], f['after'], t['qty'],
                     m['kind'], 1 if m['also_shipped'] else 0, week_label))
        sources = [conn.execute('SELECT * FROM inspection_tasks WHERE job_key=?',
                                (f"{f['sheet']}|{m['po']}|{m['item']}",)).fetchone() for f in m['from']]
        source = next((t for t in sources if t and t['assigned_to']), None)
        targets = list(m['to_keys'])
        if m['kind'] == 'full' and not m['also_shipped'] and len(m['gone_keys']) == 1 and targets:
            old, new = m['gone_keys'][0], targets[0]
            if old in existing_keys and new not in existing_keys and migrate_job_key(conn, old, new):
                existing_keys.discard(old)
                existing_keys.add(new)
                followed.append((m, old, new))
                targets = targets[1:]
        if source:
            for key in targets:
                inherit[key] = (source, m)
    return followed, inherit


def _send_region_move_email(followed, inherited):
    """Tell the lead and the inspectors concerned which orders changed region."""
    tasks = [dict(task) for _m, task in followed] + [dict(t) for t in inherited]
    recipients = _task_recipients(tasks)
    if not recipients:
        return False, tr('未设置通知邮箱', 'No notification e-mail configured')
    lines = ['订单转区通知 Region transfer', '供应商把以下订单（同一 PO + 产品）转到了其他地区。'
             ' The supplier moved these order lines (same PO + item) to another region.', '']
    i = 0
    for m, task in followed:
        i += 1
        lines.append(f"{i}. {task['order_number']}  {m['po']}  {m['item']}")
        lines.append(f"    {move_text(m)}")
        lines.append(f"    整单转区：任务和检验报告已随订单转到 {task['region']}，负责人不变 ({task.get('assignee_name') or '—'})。"
                     f" Whole line moved: the task and its reports moved with it.")
        lines.append('    ' + url_for('inspect_form', job_key=task['job_key'], _external=True))
    for t in inherited:
        i += 1
        lines.append(f"{i}. {t['order_number']}  {t['item_code']}  → {t['region']}")
        lines.append(f"    {t['move_text']}")
        lines.append(f"    部分转区：已为 {t['region']} 新建任务，自动分配给 {t.get('assignee_name') or '—'}。"
                     f" Part moved: a new task was created for the same inspector.")
        lines.append('    ' + url_for('inspect_form', job_key=t['job_key'], _external=True))
    subject = f'【订单转区】{i} 个订单转到其他地区 / {i} order line(s) moved region'
    return _smtp_send(subject, '\n'.join(lines), recipients)


def compute_changes(previous, current, moves_out=None):
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

    # ── region moves: the same PO + item now (also) on another sheet ─────────
    moves = detect_region_moves(prev_keys, curr_keys, prev_qty, curr_qty)
    moved_in_keys = {k for m in moves for k in m['to_keys']}
    moved_out_keys = {k for m in moves if not m['also_shipped'] for k in m['gone_keys']}
    reduced_by_move = {k for m in moves if not m['also_shipped'] for k in m['reduced_keys']}
    genuine_new -= moved_in_keys
    genuine_shipped -= moved_out_keys
    if moves_out is not None:
        moves_out['moves'] = moves

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
        if k in moved_in_keys:
            statuses[k] = 'moved_in'
        elif k in typo_new_keys:
            statuses[k] = 'typo'
        elif k not in prev_keys:
            statuses[k] = 'new'
        else:
            pq = prev_qty.get(k)
            cq = curr_qty.get(k)
            if pq is not None and cq is not None and cq < pq:
                statuses[k] = 'partially_moved' if k in reduced_by_move else 'partially_shipped'
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
    inspections = SharedReports(load_json(INSPECTIONS_CACHE, {}))
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
    moves_info = {}
    if current and previous:
        statuses, typo_flags, shipped_rows = compute_changes(previous, current, moves_out=moves_info)
    else:
        statuses, typo_flags, shipped_rows = {}, [], {}
    move_notes = {}
    for m in moves_info.get('moves', []):
        for k in m['to_keys'] + m['reduced_keys']:
            move_notes[k] = move_text(m)
    if not g.can_see_prices:
        # Prices never reach inspectors (not even via search).
        current, shipped_rows = strip_admin_only_columns(current, shipped_rows)
    config = load_config()
    inspections = SharedReports(load_json(INSPECTIONS_CACHE, {}))

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
    today_str = china_today().isoformat()
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
                           purchasing_uploads=schedule_upload_history()[:30] if g.is_admin else [],
                           move_notes=move_notes,
                           est_overrides=est_overrides,
                           data=display_data,
                           statuses=statuses,
                           typo_flags=[t for t in typo_flags if t.get('sheet') == selected_sheet][:100],
                           shipped_rows=display_shipped,
                           inspections=inspections,
                           valve_keys=valve_keys,
                           upload_date=config.get('upload_date', ''),
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
    inspections = SharedReports(load_json(INSPECTIONS_CACHE, {}))
    statuses, _, shipped_rows = (
        compute_changes(previous, current)
        if current and previous else ({}, [], {}))
    if not g.can_see_prices:
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
                'typo': 'TYPO?', 'moved_in': 'MOVED IN', 'partially_moved': 'PART MOVED OUT',
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

# ── Schedule changes for purchasing ──────────────────────────────────────────
# One upload compared with the one before it: what shipped (fully / partly),
# which dates moved, new lines and region moves. Same rules as the upload
# preview (compute_changes); laid out for the purchasing team, no prices.

def _schedule_lines(data):
    """{job_key: line} for a saved schedule; split lots of one PO + item are
    added up and keep the first lot's dates."""
    lines = {}
    for sheet, rows in (data or {}).items():
        if not rows or len(rows) < 2:
            continue
        headers = rows[0]
        low = [str(h).strip().lower() for h in headers]
        qi = _qty_index(headers)
        for row in rows[1:]:
            key = make_job_key(sheet, row, headers)
            if not key.split('|')[1]:
                continue

            def col(*names):
                for name in names:
                    if name in low and low.index(name) < len(row) and row[low.index(name)] is not None:
                        return str(row[low.index(name)]).strip()
                return ''

            qty = _parse_qty(row[qi]) if qi is not None and qi < len(row) else None
            line = lines.get(key)
            if line:
                line['qty'] = None if line['qty'] is None or qty is None else line['qty'] + qty
                continue
            lines[key] = {'key': key, 'region': sheet, 'dpl': col('order number'),
                          'po': col('daemco purchase order', 'purchase order'),
                          'supplier': col('supplier', 'foundry'), 'item_code': col('item code'),
                          'description': col('item description'), 'qty': qty,
                          'est': col('estimated completion date'), 'must_ship': col('must ship date')}
    return lines


def _date_shift(old, new):
    """(days, note) for a changed schedule date cell."""
    (d1, n1), (d2, n2) = split_est(old), split_est(new)
    if d1 and d2 and d1 != d2:
        days = (d2 - d1).days
        return days, (f'Delayed {days} d' if days > 0 else f'Earlier {-days} d')
    if d1 and not d2:
        return None, 'Date removed'
    if d2 and not d1:
        return None, 'Date added'
    return None, 'Remark changed'


def purchasing_changes(before, after, inspections=None):
    """Changes between two saved schedules, as lists of rows for the export.
    Shipped lines carry has_report: is there an inspection report (QA BRT) now."""
    if inspections is None:
        inspections = SharedReports(load_json(INSPECTIONS_CACHE, {}))
    moves_info = {}
    statuses, typo_flags, _ = compute_changes(before, after, moves_out=moves_info)
    moves = moves_info.get('moves', [])
    old, new = _schedule_lines(before), _schedule_lines(after)
    region_order = {s: i for i, s in enumerate(list(after) + [s for s in before if s not in after])}

    def order(line):
        return (region_order.get(line['region'], 99), line['dpl'], line['po'], line['item_code'])

    moved_out = {k for m in moves if not m['also_shipped'] for k in m['gone_keys']}
    shipped = [dict(line, type='Fully shipped', before=line['qty'], after=0, shipped=line['qty'])
               for key, line in old.items() if key not in new and key not in moved_out]
    for key, status in statuses.items():
        if status == 'partially_shipped' and key in old and key in new:
            b, a = old[key]['qty'], new[key]['qty']
            shipped.append(dict(new[key], type='Partially shipped', before=b, after=a,
                                shipped=(b - a) if b is not None and a is not None else None))
    typo_of = {t['curr_key']: t for t in typo_flags}
    new_lines = []
    for key, status in statuses.items():
        if status in ('new', 'typo') and key in new:
            t = typo_of.get(key)
            note = (f"Possible typo of {t['prev_order']} / {t['prev_item']} (that line left the schedule)"
                    if t else '')
            new_lines.append(dict(new[key], note=note))
    date_changes = []
    for key, n in new.items():
        o = old.get(key)
        if not o:
            continue
        est_moved, ship_moved = est_changed(o['est'], n['est']), est_changed(o['must_ship'], n['must_ship'])
        if not est_moved and not ship_moved:
            continue
        est_days, est_note = _date_shift(o['est'], n['est']) if est_moved else (None, '')
        ship_days, ship_note = _date_shift(o['must_ship'], n['must_ship']) if ship_moved else (None, '')
        date_changes.append(dict(n, prev_est=o['est'], est_days=est_days, est_note=est_note,
                                 prev_must_ship=o['must_ship'], ship_days=ship_days, ship_note=ship_note))
    region_moves = []
    for m in moves:
        line = new.get(m['to_keys'][0]) or old.get((m['gone_keys'] or m['reduced_keys'] or [''])[0]) or {}
        src = ', '.join(f"{f['sheet']} {_qty_text(f['before'])}" for f in m['from'])
        dst = ', '.join(f"{t['sheet']} {_qty_text(t['qty'])}" for t in m['to'])
        kept = ', '.join(f"{f['sheet']} {_qty_text(f['after'])}" for f in m['from'] if f['after'] is not None)
        region_moves.append({'dpl': line.get('dpl', ''), 'po': m['po'], 'item_code': m['item'],
                             'supplier': line.get('supplier', ''), 'description': line.get('description', ''),
                             'from': src, 'to': dst, 'kept': kept,
                             'note': 'Total went down: part of it also shipped' if m['also_shipped'] else ''})
    for line in shipped:
        line['has_report'] = bool(inspections.get(line['key']))
    return {'shipped': sorted(shipped, key=lambda l: (l['type'] != 'Fully shipped', order(l))),
            'date_changes': sorted(date_changes, key=lambda l: (-(l['est_days'] if l['est_days'] is not None
                                                                  else l['ship_days'] or -10 ** 6), order(l))),
            'new': sorted(new_lines, key=order),
            'moves': sorted(region_moves, key=lambda m: (m['po'], m['item_code'])),
            'regions': list(region_order)}


def schedule_upload_history():
    """Applied uploads, newest first, each with the schedule before it
    ('before') and the schedule it produced ('after': the next upload's
    replaced schedule, or the current one for the latest upload)."""
    if not os.path.isdir(HISTORY_DIR):
        return []
    folders = sorted(f for f in os.listdir(HISTORY_DIR)
                     if os.path.isdir(os.path.join(HISTORY_DIR, f)) and re.fullmatch(r'[\d-]+', f))
    uploads = []
    for i, name in enumerate(folders):
        folder = os.path.join(HISTORY_DIR, name)
        meta = load_json(os.path.join(folder, 'meta.json'), {})
        after = (os.path.join(HISTORY_DIR, folders[i + 1], 'replaced_schedule.json')
                 if i + 1 < len(folders) else CURRENT_FILE)
        uploads.append({'id': name, 'filename': meta.get('filename') or name,
                        'applied_at': meta.get('applied_at', ''),
                        'before': os.path.join(folder, 'replaced_schedule.json'), 'after': after})
    for i, u in enumerate(uploads):
        u['previous'] = uploads[i - 1] if i else None
    return [u for u in reversed(uploads) if os.path.exists(u['before']) and os.path.exists(u['after'])]


def _upload_label(upload):
    if not upload:
        return 'previous schedule'
    return f"{upload['filename']} (applied {upload['applied_at']} UTC)" if upload['applied_at'] else upload['filename']


def purchasing_workbook(changes, before_label, after_label):
    """Readable workbook: Summary, Shipped, Date Changes, New Lines, Region Moves."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    navy, white = PatternFill('solid', fgColor='1A3A5C'), Font(color='FFFFFF', bold=True)
    thin = Side(style='thin', color='D0D5DD')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical='top')
    center = Alignment(horizontal='center', vertical='top')
    fills = {'Fully shipped': PatternFill('solid', fgColor='E5E7EB'),
             'Partially shipped': PatternFill('solid', fgColor='FEF3C7')}
    wb = openpyxl.Workbook()
    summary = wb.active
    summary.title = 'Summary'

    def qty(v):
        return '' if v is None else (int(v) if v == int(v) else v)

    def day(v):                       # '2026/6/15 ready to ship' -> '2026-06-15 (ready to ship)'
        return _est_label(v) if v else ''

    def table(title, columns, rows, empty='No changes'):
        ws = wb.create_sheet(title)
        ws.append([c[0] for c in columns])
        for c in ws[1]:
            c.fill, c.font, c.border = navy, white, border
            c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        ws.row_dimensions[1].height = 30
        for row in rows:
            ws.append([_xl_safe(v) for v in row])
        if not rows:
            ws.append([empty])
        for i, (_name, width, kind) in enumerate(columns, 1):
            ws.column_dimensions[get_column_letter(i)].width = width
            for (cell,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                cell.border = border
                cell.alignment = center if kind == 'c' else wrap
        ws.freeze_panes = 'A2'
        if rows:
            ws.auto_filter.ref = ws.dimensions
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = '1:1'
        return ws

    def delta(ws, col_idx):
        for (cell,) in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
            if isinstance(cell.value, int):
                cell.font = Font(bold=True, color='B91C1C' if cell.value > 0 else '047857')
                cell.value = f'+{cell.value}' if cell.value > 0 else str(cell.value)

    ws = table('Shipped', [('Type', 17, 'c'), ('Region', 13, 'c'), ('DPL', 10, 'c'), ('PO', 11, 'c'),
                           ('Supplier', 10, 'c'), ('Item Code', 18, 'l'), ('Description', 46, 'l'),
                           ('Previous Qty', 11, 'c'), ('Current Qty', 11, 'c'), ('Shipped Qty', 11, 'c'),
                           ('QA Report', 11, 'c'), ('Est. Completion (previous)', 22, 'c')],
               [(l['type'], l['region'], l['dpl'], l['po'], l['supplier'], l['item_code'], l['description'],
                 qty(l['before']), qty(l['after']), qty(l['shipped']), 'Yes' if l.get('has_report') else 'No',
                 day(l['est'])) for l in changes['shipped']])
    for (cell,) in ws.iter_rows(min_row=2, max_col=1):
        if cell.value in fills:
            cell.fill = fills[cell.value]
    for (cell,) in ws.iter_rows(min_row=2, min_col=10, max_col=10):
        cell.font = Font(bold=True)
    for (cell,) in ws.iter_rows(min_row=2, min_col=11, max_col=11):
        if cell.value == 'No':                     # shipped without an inspection report
            cell.font = Font(bold=True, color='B91C1C')
            cell.fill = PatternFill('solid', fgColor='FEE2E2')

    ws = table('Date Changes', [('Region', 13, 'c'), ('DPL', 10, 'c'), ('PO', 11, 'c'), ('Supplier', 10, 'c'),
                                ('Item Code', 18, 'l'), ('Description', 40, 'l'), ('Qty', 8, 'c'),
                                ('Est. Completion (previous)', 22, 'c'), ('Est. Completion (new)', 22, 'c'),
                                ('Days', 8, 'c'), ('Change', 15, 'c'),
                                ('Must Ship (previous)', 16, 'c'), ('Must Ship (new)', 16, 'c'),
                                ('Must Ship Days', 10, 'c')],
               [(l['region'], l['dpl'], l['po'], l['supplier'], l['item_code'], l['description'], qty(l['qty']),
                 day(l['prev_est']), day(l['est']), l['est_days'], l['est_note'],
                 day(l['prev_must_ship']) if l['ship_note'] else '', day(l['must_ship']) if l['ship_note'] else '',
                 l['ship_days'] if l['ship_days'] is not None else l['ship_note']) for l in changes['date_changes']])
    delta(ws, 10)
    delta(ws, 14)

    table('New Lines', [('Region', 13, 'c'), ('DPL', 10, 'c'), ('PO', 11, 'c'), ('Supplier', 10, 'c'),
                        ('Item Code', 18, 'l'), ('Description', 46, 'l'), ('Qty', 8, 'c'),
                        ('Est. Completion', 22, 'c'), ('Must Ship', 14, 'c'), ('Note', 36, 'l')],
          [(l['region'], l['dpl'], l['po'], l['supplier'], l['item_code'], l['description'], qty(l['qty']),
            day(l['est']), day(l['must_ship']), l['note']) for l in changes['new']])

    table('Region Moves', [('DPL', 10, 'c'), ('PO', 11, 'c'), ('Supplier', 10, 'c'), ('Item Code', 18, 'l'),
                           ('Description', 40, 'l'), ('From (qty before)', 24, 'l'), ('To (qty)', 22, 'l'),
                           ('Kept on the old region', 22, 'l'), ('Note', 30, 'l')],
          [(m['dpl'], m['po'], m['supplier'], m['item_code'], m['description'], m['from'], m['to'],
            m['kept'], m['note']) for m in changes['moves']])

    # Summary
    delayed = sum(1 for l in changes['date_changes'] if (l['est_days'] or 0) > 0)
    earlier = sum(1 for l in changes['date_changes'] if (l['est_days'] or 0) < 0)
    fully = [l for l in changes['shipped'] if l['type'] == 'Fully shipped']
    partly = [l for l in changes['shipped'] if l['type'] != 'Fully shipped']
    summary.column_dimensions['A'].width = 34
    for letter in 'BCDEFG':
        summary.column_dimensions[letter].width = 16
    summary.append(['Production Schedule Changes — for Purchasing'])
    summary['A1'].font = Font(bold=True, size=16, color='1A3A5C')
    summary.append([])
    summary.append(['Previous schedule', before_label])
    summary.append(['New schedule', after_label])
    summary.append(['Generated', dual_zone_time().replace('北京', 'Beijing')])
    for r in range(3, 6):
        summary.cell(r, 1).font = Font(bold=True)
    summary.append([])
    summary.append(['Change', 'Lines', 'Sheet'])
    head_row = summary.max_row
    for row in (('Fully shipped (line left the schedule)', len(fully), 'Shipped'),
                ('Partially shipped (quantity went down)', len(partly), 'Shipped'),
                ('Shipped without a QA report', sum(1 for l in changes['shipped'] if not l.get('has_report')),
                 'Shipped (QA Report = No)'),
                ('Est. completion delayed', delayed, 'Date Changes'),
                ('Est. completion earlier', earlier, 'Date Changes'),
                ('Other date / remark changes', len(changes['date_changes']) - delayed - earlier, 'Date Changes'),
                ('New lines', len(changes['new']), 'New Lines'),
                ('Moved to another region', len(changes['moves']), 'Region Moves')):
        summary.append(list(row))
    summary.append([])
    summary.append(['By region', 'Fully shipped', 'Partially shipped', 'Date changes', 'New lines'])
    region_head = summary.max_row
    for region in changes['regions']:
        counts = (sum(1 for l in fully if l['region'] == region), sum(1 for l in partly if l['region'] == region),
                  sum(1 for l in changes['date_changes'] if l['region'] == region),
                  sum(1 for l in changes['new'] if l['region'] == region))
        if any(counts):
            summary.append([region, *counts])
    for r in (head_row, region_head):
        for cell in summary[r]:
            if cell.value is not None:
                cell.fill, cell.font = navy, white
    for row in summary.iter_rows(min_row=head_row + 1, max_row=summary.max_row):
        for cell in row[1:]:
            cell.alignment = Alignment(horizontal='center')
    summary.append([])
    for note in ('How to read',
                 '• A line that is no longer in the new schedule has fully shipped.',
                 '• A lower quantity means part of the line has shipped.',
                 '• Days: + = later than before (delayed, red), − = earlier (green).',
                 '• A PO + item that moved to another region is listed under Region Moves, not as shipped.'):
        summary.append([note])
    summary.cell(summary.max_row - 4, 1).font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def purchasing_export(upload_id=None):
    """(xlsx bytes, filename, changes, upload) for one upload (default: latest), or None."""
    uploads = schedule_upload_history()
    upload = next((u for u in uploads if u['id'] == upload_id), None) if upload_id else (uploads[0] if uploads else None)
    if not upload:
        return None
    changes = purchasing_changes(load_schedule(upload['before']), load_schedule(upload['after']))
    data = purchasing_workbook(changes, _upload_label(upload['previous']), _upload_label(upload))
    # named by the day it is exported (China date, like other business dates)
    return data, f'Comparison Sheet {china_today().isoformat()}.xlsx', changes, upload


@app.route('/export/purchasing.xlsx')
def export_purchasing_excel():
    result = purchasing_export(request.args.get('upload') or None)
    if not result:
        flash(tr('至少要有两次排期上传才能对比', 'At least two schedule uploads are needed for a comparison'), 'warning')
        return redirect(url_for('index'))
    data, filename, _changes, _upload = result
    return send_file(io.BytesIO(data), as_attachment=True, download_name=filename,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def send_purchasing_email(upload_id=None):
    """E-mail the purchasing comparison to the purchasing addresses. Returns (ok, message)."""
    recipients = _email_list(load_config().get('purchasing_emails', ''))
    if not recipients:
        return False, tr('未设置采购邮箱', 'No purchasing e-mail configured')
    result = purchasing_export(upload_id)
    if not result:
        return False, tr('至少要有两次排期上传才能对比', 'At least two schedule uploads are needed for a comparison')
    data, filename, changes, upload = result
    fully = sum(1 for l in changes['shipped'] if l['type'] == 'Fully shipped')
    partly = len(changes['shipped']) - fully
    delayed = sum(1 for l in changes['date_changes'] if (l['est_days'] or 0) > 0)
    lines = ['Production schedule changes for purchasing', '',
             f"New schedule: {_upload_label(upload)}", f"Previous schedule: {_upload_label(upload['previous'])}", '',
             f'Fully shipped: {fully}', f'Partially shipped: {partly}',
             f"Date changes: {len(changes['date_changes'])} ({delayed} delayed)",
             f"New lines: {len(changes['new'])}", f"Region moves: {len(changes['moves'])}", '',
             'Details are in the attached Excel.']
    subject = (f"Schedule changes {(upload['applied_at'] or '')[:10]}: {fully + partly} shipped, "
               f"{len(changes['date_changes'])} date changes")
    return _smtp_send(subject, '\n'.join(lines), recipients, attachments=[
        (filename, data, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')])


@app.route('/settings/purchasing-send', methods=['POST'])
def settings_purchasing_send():
    ok, msg = send_purchasing_email()
    flash(msg, 'success' if ok else 'error')
    return redirect(url_for('settings') + '#email')


ONTIME_MIN_SAMPLE = 5   # below this the on-time rate is shown as indicative only
KPI_EXCLUDED_ROLES = ('admin', 'hq')   # they do not inspect: keep them out of the inspector KPIs


def inspector_kpis(inspections, est_map, users, weeks):
    """Per-inspector report counts, pass / on-time rates and a weekly series.

    inspections: {job_key: [report, ...]}; est_map: {job_key: estimated completion cell};
    users: rows with id, username, display_name, role; weeks: ['2026-W40', ...].
    A report belongs to the account whose display name or username equals its
    inspector name (case-insensitive); reports of admin / HQ accounts are left out.
    Names without an account are kept under the name as written."""
    by_name = {}
    for u in users:
        for key in (u['username'], u['display_name']):
            key = (key or '').strip().lower()
            if key:
                by_name.setdefault(key, u)
    raw = {}
    for job_key, records in inspections.items():
        est = _parse_date(est_map.get(job_key, ''))
        for rec in records:
            name = (rec.get('inspector_name') or '').strip()
            if not name:
                continue
            user = by_name.get(name.lower())
            if user and user['role'] in KPI_EXCLUDED_ROLES:
                continue
            key = ('user', user['id']) if user else ('name', name.lower())
            s = raw.setdefault(key, dict(
                name=(user['display_name'] or user['username']) if user else name,
                total=0, passed=0, failed=0, partial=0, on_time=0, late=0, no_est=0,
                last_date='', weekly=defaultdict(int)))
            s['total'] += 1
            result = (rec.get('result') or '').lower()
            if 'fail' in result:
                s['failed'] += 1
            elif 'partial' in result:
                s['partial'] += 1
            elif 'pass' in result:
                s['passed'] += 1
            insp_date = _parse_date(rec.get('inspection_date'))
            if insp_date:
                s['last_date'] = max(s['last_date'], insp_date.isoformat())
                year, week, _ = insp_date.isocalendar()
                s['weekly'][f'{year}-W{week:02d}'] += 1
            if est and insp_date:
                s['on_time' if insp_date <= est else 'late'] += 1
            else:
                s['no_est'] += 1
    stats = []
    for s in sorted(raw.values(), key=lambda s: (-s['total'], s['name'].lower())):
        s['weekly'] = {w: s['weekly'].get(w, 0) for w in weeks}
        s['max_weekly'] = max(s['weekly'].values(), default=0) or 1
        s['rated'] = s['on_time'] + s['late']
        s['ontime_pct'] = round(s['on_time'] / s['rated'] * 100) if s['rated'] else None
        s['small_sample'] = 0 < s['rated'] < ONTIME_MIN_SAMPLE
        stats.append((s['name'], s))
    return stats


@app.route('/dashboard')
def dashboard():
    current     = load_schedule(CURRENT_FILE)
    previous    = load_schedule(PREVIOUS_FILE)
    config      = load_config()
    inspections = SharedReports(load_json(INSPECTIONS_CACHE, {}))

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
                status = {'moved_in': 'new', 'partially_moved': 'not_shipped'}.get(status, status)
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
        users = conn.execute('SELECT id, username, display_name, role FROM users').fetchall()
    est_map = {r['job_key']: r['est_completion'] for r in _est_rows if r['job_key'] and r['est_completion']}

    # Last 8 ISO weeks (oldest → newest), by the factories' calendar
    _today = china_today()
    recent_weeks = []
    for _i in range(7, -1, -1):
        _yr, _wk, _ = (_today - timedelta(weeks=_i)).isocalendar()
        recent_weeks.append(f"{_yr}-W{_wk:02d}")
    inspector_stats = inspector_kpis(inspections, est_map, users, recent_weeks)

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
    summary = {'sheets': [], 'new': [], 'shipped': [], 'partial': [], 'typo': [], 'moves': [],
               'date_changes': [], 'warnings': [], 'first_upload': not current}
    if not current:
        for sheet, rows in new.items():
            summary['sheets'].append({'name': sheet, 'rows': max(0, len(rows) - 1),
                                      'new': max(0, len(rows) - 1), 'shipped': 0,
                                      'partial': 0, 'added': [], 'removed': []})
        return summary

    moves_info = {}
    statuses, typo_flags, _ = compute_changes(current, new, moves_out=moves_info)
    moves = moves_info.get('moves', [])
    moved_out = {k for m in moves if not m['also_shipped'] for k in m['gone_keys']}
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
    for m in moves:
        row = new_rows.get(m['to_keys'][0]) or {}
        summary.setdefault('moves', []).append({
            'order_number': row.get('order_number', ''), 'po': m['po'], 'item_code': m['item'],
            'description': row.get('description', ''), 'text': move_text(m),
            'kind': m['kind'], 'also_shipped': m['also_shipped']})
    for key, detail in old_rows.items():
        if key in moved_out:
            continue
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
    try:
        send_vtrust_reschedule_alerts()
    except Exception:
        logger.exception('V-Trust reschedule check failed')
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
    # a baseline re-sync compares with stale data: no follow-up e-mails then
    if not baseline and not app.config.get('TESTING'):
        _after_upload_emails_in_background(request.host_url)
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
    _snap_date  = china_today().isoformat()
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
        moves_info = {}
        if previous:
            statuses, _, newly_shipped = compute_changes(previous, current_data, moves_out=moves_info)
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
        moved_in_tasks = []
        date_changes = []
        tasks_updated = 0
        seen_this_upload = set()
        with db_conn() as conn:
            existing_keys = {r[0] for r in conn.execute(
                'SELECT job_key FROM inspection_tasks').fetchall()}
            followed, inherit = handle_region_moves(
                conn, moves_info.get('moves', []), existing_keys, config.get('upload_date', ''))
            followed = [(m, dict(conn.execute(
                "SELECT t.*, COALESCE(NULLIF(u.display_name, ''), u.username) AS assignee_name "
                "FROM inspection_tasks t LEFT JOIN users u ON u.id = t.assigned_to WHERE t.job_key=?",
                (new,)).fetchone())) for m, _old, new in followed]
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
                    if statuses.get(jk) in ('new', 'typo', 'moved_in'):
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
                        existing_keys.add(jk)
                        if jk in inherit:
                            # split-off part of a line that moved region: same inspector
                            source, move = inherit[jk]
                            from_sheets = ', '.join(f['sheet'] for f in move['from'])
                            note = f'从 {from_sheets} 转入 / moved from {from_sheets}'
                            conn.execute(
                                "UPDATE inspection_tasks SET assigned_to=?, assigned_by='system', "
                                "assigned_at=?, assign_note=? WHERE job_key=?",
                                (source['assigned_to'], datetime.now().strftime('%Y-%m-%d %H:%M'), note, jk))
                            assignee = conn.execute('SELECT * FROM users WHERE id=?',
                                                    (source['assigned_to'],)).fetchone()
                            moved_in_tasks.append(dict(
                                task, assigned_to=source['assigned_to'], move_text=move_text(move),
                                assignee_name=(assignee['display_name'] or assignee['username']) if assignee else ''))
                        else:
                            new_tasks_created.append(task)

        if followed or moved_in_tasks:
            note = tr(f'{len(followed) + len(moved_in_tasks)} 个订单转到了其他地区（任务已跟随 / 已分配给原检验员）',
                      f'{len(followed) + len(moved_in_tasks)} order line(s) moved region (tasks followed / went to the same inspector)')
            if not baseline:
                ok, msg = _send_region_move_email(followed, moved_in_tasks)
                note += tr('。邮件通知：', '. E-mail: ') + msg
            flash(note, 'info')
        if date_changes and not baseline:
            ok, msg = _send_date_change_email(date_changes)
            flash(tr(f'{len(date_changes)} 个任务的预计完成日/出货日有变动。邮件通知：{msg}',
                     f'{len(date_changes)} task(s) had date changes. E-mail: {msg}'),
                  'success' if ok else 'warning')
        if not baseline:
            run_daily_reminders()

        moved = assign_unscheduled_tasks_to_lead()
        if moved:
            flash(tr(f'{moved} 个订单已不在排期的未分配任务已自动分配给 Murphy',
                     f'{moved} unassigned task(s) whose order left the schedule were assigned to Murphy'),
                  'info')
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


def job_task(job_key):
    """The inspection task for a job (with the assignee's name) or None."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT t.*, COALESCE(NULLIF(u.display_name, ''), u.username) AS assignee_name "
            'FROM inspection_tasks t LEFT JOIN users u ON u.id = t.assigned_to '
            'WHERE t.job_key=?', (job_key,)).fetchone()


def inspect_permission(job_key):
    """(allowed, message): may the current user submit an inspection / checklist
    for this job? Lead and admin always; inspectors only for tasks assigned to
    them that are not closed. Anyone may still view the job and its reports."""
    if g.get('can_assign'):
        return True, ''
    if g.get('is_hq'):
        return False, tr('总部账号可以查看和审核检验报告，但不提交检验。',
                         'Head-office accounts can view and review reports but do not submit inspections.')
    task = job_task(job_key)
    if not task or not task['assigned_to']:
        return False, tr('此订单尚未分配检验任务，请联系检验主管分配后再检验。',
                         'This job has not been assigned yet. Ask the lead inspector to assign it.')
    if task['assigned_to'] != g.user_id:
        name = task['assignee_name'] or ''
        return False, tr(f'此任务已分配给 {name}，你只能查看。',
                         f'This task is assigned to {name}; you can only view it.')
    if task['status'] == 'Closed':
        return False, tr('此任务已被关闭，如需检验请联系检验主管。',
                         'This task has been closed. Ask the lead inspector if it still needs an inspection.')
    return True, ''


def my_open_job_keys():
    """Job keys the current user may inspect (None = every job: lead/admin)."""
    if g.get('can_assign'):
        return None
    with db_conn() as conn:
        return {r['job_key'] for r in conn.execute(
            "SELECT job_key FROM inspection_tasks WHERE assigned_to=? "
            "AND IFNULL(status, '') != 'Closed'", (g.user_id,))}


def inspector_display_name():
    return g.get('display_name') or g.get('username', '')


@app.route('/inspect/<path:job_key>/can-submit')
def inspect_can_submit(job_key):
    """Checked by the inspection form just before uploading, so a refused
    submit never throws away the inspector's photos."""
    allowed, message = inspect_permission(job_key)
    return jsonify(ok=allowed, message=message)


@app.route('/inspect/<path:job_key>/assign', methods=['POST'])
def inspect_assign(job_key):
    """Lead / admin assigns a job from its inspection page; creates the task
    first when the job has none (e.g. fully shipped jobs missing a QA BRT)."""
    if not g.can_assign:
        abort(403)
    back = redirect(url_for('inspect_form', job_key=job_key))
    note = request.form.get('note', '').strip()[:500]
    with db_conn() as conn:
        assignee = conn.execute(
            "SELECT id, username, display_name, email FROM users "
            "WHERE id=? AND active=1 AND role IN ('lead', 'inspector')",
            (request.form.get('assignee_id', ''),)).fetchone()
    if not assignee:
        flash(tr('请选择检验员', 'Choose an inspector'), 'error')
        return back
    task = job_task(job_key)
    if not task:
        job = find_job(job_key)
        if not job:
            abort(404)
        with db_conn() as conn:
            conn.execute(
                'INSERT INTO inspection_tasks (job_key, order_number, region, item_code, description, '
                'supplier, quantity, est_completion, must_ship, week_label, status) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (job_key, job.get('Order Number', ''), job.get('region', ''), job.get('Item Code', ''),
                 job.get('Item Description', ''), job.get('Supplier') or job.get('Foundry') or '',
                 str(job.get('Quantity', '')), job.get('Estimated Completion Date', ''),
                 job.get('Must Ship Date', ''), load_config().get('upload_date', ''), 'Pending'))
        task = job_task(job_key)
    with db_conn() as conn:
        conn.execute(
            "UPDATE inspection_tasks SET assigned_to=?, assigned_by=?, assigned_at=?, assign_note=?, "
            "status=CASE WHEN status='Closed' THEN 'Pending' ELSE status END WHERE id=?",
            (assignee['id'], g.username, datetime.now().strftime('%Y-%m-%d %H:%M'), note, task['id']))
    name = assignee['display_name'] or assignee['username']
    ok, msg = _send_assignment_email([dict(job_task(job_key))], assignee, note)
    flash(tr(f'已分配给 {name}。', f'Assigned to {name}. ') + msg, 'success' if ok else 'warning')
    return back


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
        job_info.get('Item Description', '')
    )
    product_kind = product_type_name(product_type_for(job_info.get('Item Code', ''),
                                                      job_info.get('Item Description', '')))

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

    draft = load_inspection_draft(job_key) if inspect_permission(job_key)[0] else None
    checklist = job_checklist(job_info.get('Item Code', ''), job_info.get('Item Description', ''),
                              draft and draft['template_id'], draft and draft['version'])
    if checklist:   # the digital checklist replaces the signed paper checklist upload
        evidence_reqs = [ev for ev in evidence_reqs if ev['type'] != 'checklist']
    with db_conn() as conn:
        checklist_photos = defaultdict(list)
        for a in conn.execute("SELECT id, insp_index, ref FROM inspection_attachments WHERE job_key=? "
                              "AND evidence_type IN ('checklist_photo', 'product_photo') ORDER BY id", (job_key,)):
            checklist_photos[(a['insp_index'], a['ref'])].append(a['id'])
    cache_all = load_json(INSPECTIONS_CACHE, {})
    related_reports = [
        {'job_key': k, 'region': k.split('|', 1)[0], 'index': i, 'record': r,
         'report_no': report_number(k, r, i)}
        for k, records in cache_all.items()
        if k != job_key and _job_tail(k) == _job_tail(job_key)
        for i, r in enumerate(records or [])]
    return render_template('inspect.html',
                           job=job_info,
                           checklist=checklist,
                           draft=draft,
                           checklist_photos=checklist_photos,
                           related_reports=related_reports,
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
                           product_kind=product_kind,
                           can_inspect=inspect_permission(job_key)[0],
                           inspect_block_message=inspect_permission(job_key)[1],
                           task=job_task(job_key),
                           assignees=_assignable() if g.can_assign else [],
                           vtrust_booking=vtrust_bookings().get(job_key),
                           ev_files={ref[len(EV_REF_PREFIX):]: files for ref, files in (draft or {}).get('files', {}).items()
                                     if ref.startswith(EV_REF_PREFIX)},
                           draft_file_max_bytes=DRAFT_FILE_MAX_BYTES,
                           now_date=china_today().isoformat())

INSPECTION_RESULTS = {'Pass', 'Fail', 'Partial Pass'}

@app.route('/inspect/<path:job_key>/submit', methods=['POST'])
def submit_inspection(job_key):
    form = request.form
    allowed, message = inspect_permission(job_key)
    if not allowed:
        flash(message, 'error')
        return redirect(url_for('inspect_form', job_key=job_key))
    # Inspectors always report under their own name; lead/admin may record
    # an inspection on someone else's behalf.
    inspector_name = (form.get('inspector_name', '').strip() if g.can_assign
                      else inspector_display_name())

    # ── Validate before anything is written ──────────────────────────────
    errors = []
    if form.get('result', '') not in INSPECTION_RESULTS:
        errors.append(tr('请选择总体检验结果', 'Please choose an overall result'))
    if not inspector_name:
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

    # ── Checklist: every question answered, photos where needed ──────────
    checklist = None
    with db_conn() as conn:
        draft = _draft(conn, job_key)
        draft_files = _draft_files(conn, draft['id']) if draft else []
    if form.get('checklist_template_id'):
        checklist = job_checklist('', '', form.get('checklist_template_id', type=int),
                                  form.get('checklist_version', type=int))
        if not checklist:
            flash(tr('找不到这份检查清单的版本，请刷新页面后重新提交（草稿已保存）',
                     'This checklist version was not found; reload the page and submit again (your draft is saved)'),
                  'error')
            return redirect(url_for('inspect_form', job_key=job_key))
    elif job_checklist(form.get('item_code', ''), form.get('item_description', '')):
        flash(tr('这个产品需要填写检查清单', 'This product needs its checklist filled in'), 'error')
        return redirect(url_for('inspect_form', job_key=job_key))
    checklist_answers = {}
    if checklist:
        try:
            checklist_answers = json.loads(form.get('checklist_json') or '{}')
        except json.JSONDecodeError:
            checklist_answers = {}
        checklist_answers = checklists.fill_not_applicable(checklist['data'], checklist_answers,
                                                           form.get('item_description', ''))
        photo_refs = {f['ref'] for f in draft_files}
        problems = checklists.answer_problems(checklist['data'], checklist_answers, photo_refs, 'product' in photo_refs)
        if problems:
            flash(tr(f'检查清单还有 {len(problems)} 项没有完成，草稿已保存：',
                     f'{len(problems)} checklist item(s) are not complete; your draft is saved: ')
                  + '；'.join(checklist_problem_messages(checklist, problems)), 'error')
            return redirect(url_for('inspect_form', job_key=job_key))

    inspection_data = {
        'job_key':           job_key,
        'region':            form.get('region', ''),
        'order_number':      form.get('order_number', ''),
        'item_code':         form.get('item_code', ''),
        'item_description':  form.get('item_description', ''),
        'supplier':          form.get('supplier', ''),
        'quantity_ordered':  form.get('quantity_ordered', ''),
        'inspector_name':    inspector_name,
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
    booking = vtrust_bookings().get(job_key)
    if booking:
        inspection_data['vtrust_job'] = booking['job_number']

    # ── Evidence uploads: one file-input per evidence type ───────────────
    evidence_results = {}
    all_file_links   = []
    all_file_names   = []

    # Determine which insp_index this will be
    cache = load_json(INSPECTIONS_CACHE, {})
    insp_index = len(cache.get(job_key, []))
    inspection_data['report_no'] = readable_report_number(inspection_data, insp_index)

    # Evidence files already uploaded into the draft (ref "ev:<type>")
    ev_draft_files = defaultdict(list)
    for f in draft_files:
        if f['ref'].startswith(EV_REF_PREFIX):
            ev_draft_files[f['ref'][len(EV_REF_PREFIX):]].append(f)

    # Evidence type names from form (ev_result_brt, ev_result_daq, …)
    ev_types = [k[10:] for k in form if k.startswith('ev_result_')]
    ev_types += [t for t in ev_draft_files if t not in ev_types]

    job_dir = os.path.join(UPLOAD_DIR, hashlib.sha256(job_key.encode('utf-8')).hexdigest())
    os.makedirs(job_dir, exist_ok=True)

    with db_conn() as conn:
        for etype in ev_types:
            ev_result = form.get(f'ev_result_{etype}', '')
            ev_notes  = form.get(f'ev_notes_{etype}', '')
            ev_files  = request.files.getlist(f'ev_file_{etype}')   # pages opened before draft uploads
            evidence_results[etype] = {'result': ev_result, 'notes': ev_notes, 'files': []}

            for f in ev_draft_files.get(etype, []):
                all_file_names.append(f['original_name'])
                all_file_links.append(f"[local] {f['saved_name']}")
                evidence_results[etype]['files'].append(f['original_name'])
                conn.execute(
                    'INSERT INTO inspection_attachments '
                    '(job_key, insp_index, evidence_type, original_name, saved_name, '
                    ' file_path, drive_link, result, notes) VALUES (?,?,?,?,?,?,?,?,?)',
                    (job_key, insp_index, etype, f['original_name'], f['saved_name'],
                     f['file_path'], '', ev_result, ev_notes))

            for uploaded_file in ev_files:
                if not uploaded_file.filename:
                    continue
                orig_name, saved_name = _save_uploaded_file(
                    uploaded_file, job_dir, EVIDENCE_EXTENSIONS)
                file_path = os.path.join(job_dir, saved_name)
                all_file_names.append(orig_name)

                all_file_links.append(f'[local] {saved_name}')
                evidence_results[etype]['files'].append(orig_name)

                conn.execute(
                    'INSERT INTO inspection_attachments '
                    '(job_key, insp_index, evidence_type, original_name, saved_name, '
                    ' file_path, drive_link, result, notes) VALUES (?,?,?,?,?,?,?,?,?)',
                    (job_key, insp_index, etype, orig_name, saved_name,
                     file_path, '', ev_result, ev_notes))

    # Check items, DAQ readings and missing evidence against the rule matrix
    item_code = form.get('item_code', '')
    item_desc = form.get('item_description', '')
    ptype = product_type_for(item_code, item_desc)
    inspection_data['product_type'] = ptype or ''
    missing = []
    for req in (evidence_rules.PRODUCT_TYPES[ptype]['evidence'] if ptype else []):
        etype = req['type']
        if etype == 'checklist' and checklist:
            continue                  # filled in digitally, nothing to upload
        ev = evidence_results.setdefault(etype, {'result': '', 'notes': '', 'files': []})
        if req['checks']:
            ev['checks'] = [{'zh': chk['zh'], 'en': chk['en'],
                             'state': form.get(f'ev_check_{etype}_{i}', '')}
                            for i, chk in enumerate(req['checks'])]
        if etype == 'daq':
            values = {}
            for key, _label, limit in evidence_rules.DAQ_LIMITS:
                try:
                    values[key] = float(form.get(f'daq_{key}', '').strip())
                except ValueError:
                    continue
            if values:
                ev['daq_values'] = values
                ev['daq_ok'] = all(values.get(k, 0) >= limit for k, _l, limit in evidence_rules.DAQ_LIMITS)
        if not ev['files'] and ev.get('result') != 'N/A':
            missing.append(etype)
    inspection_data['missing_evidence'] = missing

    inspection_data['evidence']    = evidence_results
    inspection_data['file_links']  = all_file_links
    inspection_data['file_names']  = all_file_names

    if checklist:
        # Draft photos become report attachments (same files, now permanent).
        photos = defaultdict(list)
        with db_conn() as conn:
            for f in draft_files:
                if f['ref'].startswith(EV_REF_PREFIX):
                    continue                  # already attached as required evidence above
                kind = 'product_photo' if f['ref'] == 'product' else 'checklist_photo'
                aid = conn.execute(
                    'INSERT INTO inspection_attachments (job_key, insp_index, evidence_type, original_name, '
                    'saved_name, file_path, ref) VALUES (?,?,?,?,?,?,?)',
                    (job_key, insp_index, kind, f['original_name'], f['saved_name'], f['file_path'], f['ref'])
                ).lastrowid
                photos[f['ref']].append(aid)
            if draft:
                conn.execute('DELETE FROM draft_files WHERE draft_id=?', (draft['id'],))
                conn.execute('DELETE FROM inspection_drafts WHERE id=?', (draft['id'],))
        counts, failed = checklists.summarise(checklist['data'], checklist_answers)
        inspection_data['checklist'] = {
            'template_id': checklist['template_id'], 'name': checklist['name'],
            'version': checklist['version'], 'data': checklist['data'],
            'answers': {qid: {k: a.get(k) for k in ('v', 'occ', 'sup', 'note')}
                        for qid, a in checklist_answers.items() if isinstance(a, dict)},
            'photos': dict(photos), 'counts': counts, 'failed': failed,
            'suggested_result': checklists.suggested_result(checklist['data'], checklist_answers)}
        if not inspection_data['defects'].strip() and failed:
            inspection_data['defects'] = '\n'.join(
                f"[{f['section']} {f['num']}] {f['text']} — {checklists.answer_label(f['value'])}"
                + (f" {f['unit']}" if f['unit'] else '')
                + (f" ×{f['occurrences']}" if f['occurrences'] else '') + f" → {f['action']}"
                for f in failed)
    else:
        with db_conn() as conn:
            if draft:
                conn.execute('DELETE FROM draft_files WHERE draft_id=?', (draft['id'],))
                conn.execute('DELETE FROM inspection_drafts WHERE id=?', (draft['id'],))

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
            'UPDATE outstanding_jobs SET completed=1, completed_at=? WHERE completed=0 '
            "AND substr(job_key, instr(job_key, '|') + 1)=?",
            (datetime.now().isoformat(), _job_tail(job_key)))

    flash(tr('检验报告已保存。', 'Inspection saved.'), 'success')

    return redirect(url_for('inspect_form', job_key=job_key))

def _report_part(value):
    """Order number / item code as a safe part of a report number."""
    return re.sub(r'[^A-Za-z0-9]+', '', str(value or '').upper())


def readable_report_number(record, index):
    """QC-<inspection date>-<order number>-<item code>-<n>, e.g.
    QC-20261001-DPL2627-ES0300-1. Stored on the report when it is submitted."""
    day = (record.get('inspection_date') or record.get('submitted_at') or '')[:10].replace('-', '')
    order = _report_part(record.get('order_number')) or _report_part(
        (record.get('job_key') or '').split('|')[1] if '|' in (record.get('job_key') or '') else '')
    parts = ['QC', day or 'NODATE', order or 'NOORDER', _report_part(record.get('item_code')) or 'NOITEM',
             str(index + 1)]
    return '-'.join(parts)


def report_number(job_key, record, index):
    """The report's number. Reports submitted since the readable format was
    introduced carry it ('report_no'); older reports keep their original
    QC-<date>-<job hash>-<n> number so references already sent stay valid."""
    if record.get('report_no'):
        return record['report_no']
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
    booking = vtrust_bookings().get(job_key)
    vtrust_job = record.get('vtrust_job') or (booking['job_number'] if booking else '')
    if vtrust_job:
        job = dict(job, **{'V-Trust Job': vtrust_job})
    pdf = build_inspection_pdf(
        job, record, report_no,
        attachments=attachments,
        defect_names=defect_names,
        evidence_labels={k: (v.get('label_zh') or v['label'], v['label']) for k, v in EVIDENCE_META.items()},
        checklist=_latest_checklist(job_key),
        logo_path=os.path.join(BASE_DIR, 'static', 'daemco_logo.png'),
        generated_by=generated_by, review=dict(review) if review else None,
        time_text=dual_zone_time)
    if record.get('report_no'):
        filename = f'{report_no}.pdf'          # already contains order and item
    else:
        safe = re.sub(r'[^A-Za-z0-9._-]+', '_', f"{record.get('order_number') or ''}_{record.get('item_code') or ''}")
        filename = f'{report_no}_{safe}.pdf'.replace('__', '_')
    return pdf, report_no, filename, record


@app.route('/inspect/<path:job_key>/report.pdf')
def inspection_report_pdf(job_key):
    cache = load_json(INSPECTIONS_CACHE, {})
    records = cache.get(job_key, [])
    if not records:
        other = next((k for k, recs in cache.items() if recs and _job_tail(k) == _job_tail(job_key)), None)
        if other:   # same PO + item inspected under another region
            return redirect(url_for('inspection_report_pdf', job_key=other))
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


EMAIL_ATTACH_TYPES = {
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.xls': 'application/vnd.ms-excel', '.csv': 'text/csv', '.pdf': 'application/pdf',
}
MAX_EMAIL_TOTAL = 15 * 1024 * 1024      # all attachments together (base64 adds a third)


def applicable_evidence_types(record):
    """Evidence types the product of this inspection requires (None = no rule,
    so every evidence file applies)."""
    ptype = record.get('product_type') or product_type_for(record.get('item_code', ''),
                                                           record.get('item_description', ''))
    if not ptype:
        return None
    return {item['type'] for item in evidence_rules.PRODUCT_TYPES[ptype]['evidence']}


def _original_attachments(job_key, index, links, used=0, allowed_types=None):
    """Original evidence files of one inspection for the HQ e-mail: spreadsheets
    and PDFs are attached while the size budget lasts; photos are already in
    the report and videos are too big, so those are not attached. Returns
    ({'data': [(name, bytes, mime)], 'names': [...]}, [(name, download_url)])."""
    with db_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            'SELECT id, original_name, saved_name, file_path, evidence_type FROM inspection_attachments '
            'WHERE job_key=? AND insp_index=? ORDER BY id', (job_key, index)).fetchall()]
    if allowed_types is not None:
        # only the evidence that applies to this product type
        rows = [r for r in rows if r['evidence_type'] in allowed_types]
    base = links['pdf'].split('/inspect/')[0]
    out, names, skipped, taken = [], [], [], set()
    for r in rows:
        name = r['original_name'] or r['saved_name'] or 'file'
        ext = os.path.splitext(name)[1].lower()
        if ext in ('.jpg', '.jpeg', '.png', '.webp', '.heic', '.heif'):
            continue                                    # photos live inside the PDF
        url = f"{base}/attachments/{r['id']}"
        path = r['file_path']
        if ext not in EMAIL_ATTACH_TYPES or not path or not os.path.exists(path):
            skipped.append((name, url))
            continue
        size = os.path.getsize(path)
        if used + size > MAX_EMAIL_TOTAL:
            skipped.append((name, url))
            continue
        base_name, n = name, 2
        while name in taken:                            # same file name twice in one e-mail
            stem, e = os.path.splitext(base_name)
            name, n = f'{stem} ({n}){e}', n + 1
        taken.add(name)
        with open(path, 'rb') as fh:
            out.append((name, fh.read(), EMAIL_ATTACH_TYPES[ext]))
        names.append(name)
        used += size
    return {'data': out, 'names': names}, skipped


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
            sent_files, skipped_files = _original_attachments(
                job_key, index, links, used=len(pdf) if attachments else 0,
                allowed_types=applicable_evidence_types(rec))
            attachments += sent_files['data']
            if sent_files['names']:
                lines += ['', '随邮件附上的原始文件 Original files attached:'] + [f"  • {n}" for n in sent_files['names']]
            if skipped_files:
                lines += ['', '以下文件未随邮件发送（视频或超过大小限制），请登录系统下载 Not attached (video or too large) — download in the system:']
                lines += [f"  • {n}: {u}" for n, u in skipped_files]
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
    """Lead / admin / HQ approves or returns a submitted inspection report."""
    if not g.can_review:
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


@app.route('/inspect/<path:job_key>/report/<int:index>/inspector', methods=['POST'])
def change_report_inspector(job_key, index):
    """Admin corrects who did a submitted inspection (e.g. entered under the
    wrong name). Only lead / inspector accounts can be chosen; every change is
    kept on the report."""
    with db_conn() as conn:
        user = conn.execute(
            "SELECT username, display_name FROM users WHERE id=? AND active=1 AND role IN ('lead', 'inspector')",
            (request.form.get('inspector_id', type=int),)).fetchone()
    if not user:
        flash(tr('请选择检验员', 'Please choose an inspector'), 'error')
        return redirect(url_for('inspect_form', job_key=job_key))
    cache = load_json(INSPECTIONS_CACHE, {})
    records = cache.get(job_key, [])
    if not 0 <= index < len(records):
        abort(404)
    record = records[index]
    old = record.get('inspector_name', '')
    new = user['display_name'] or user['username']
    if old == new:
        flash(tr('检验员没有变化', 'The inspector is unchanged'), 'info')
        return redirect(url_for('inspect_form', job_key=job_key))
    record['inspector_name'] = new
    record.setdefault('inspector_changes', []).append({
        'from': old, 'to': new, 'by': g.get('username', ''),
        'at': datetime.now(_tz.utc).strftime('%Y-%m-%d %H:%M')})
    save_json(INSPECTIONS_CACHE, cache)
    logger.info('Report %s #%s inspector changed from %r to %r by %s', job_key, index, old, new, g.get('username'))
    flash(tr(f'检验员已从 {old} 改为 {new}', f'Inspector changed from {old} to {new}'), 'success')
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



# ── Inspection report list and Excel export ──────────────────────────────────

REVIEW_STATES = ('approved', 'rejected', 'pending')
REPORT_FILTERS = ('date_from', 'date_to', 'supplier', 'inspector', 'result', 'review', 'q')


def inspection_report_rows():
    """Every submitted inspection report as a flat row, newest first."""
    cache = load_json(INSPECTIONS_CACHE, {})
    with db_conn() as conn:
        reviews = {(r['job_key'], r['insp_index']): dict(r)
                   for r in conn.execute('SELECT * FROM inspection_reviews')}
        suppliers = {r['job_key']: r['supplier'] for r in conn.execute(
            "SELECT job_key, supplier FROM inspection_tasks WHERE IFNULL(supplier, '') != ''")}
    rows = []
    for job_key, records in cache.items():
        for index, rec in enumerate(records or []):
            review = reviews.get((job_key, index)) or {}
            evidence = rec.get('evidence') or {}
            rows.append({
                'job_key': job_key,
                'index': index,
                'report_no': report_number(job_key, rec, index),
                'date': (rec.get('inspection_date') or rec.get('submitted_at') or '')[:10],
                'submitted_at': rec.get('submitted_at') or '',
                'region': rec.get('region') or (job_key.split('|')[0] if '|' in job_key else ''),
                'order_number': rec.get('order_number') or '',
                'item_code': rec.get('item_code') or '',
                'item_description': rec.get('item_description') or '',
                'supplier': (rec.get('supplier') or suppliers.get(job_key) or '').strip(),
                'product_type': product_type_name(rec.get('product_type')) if rec.get('product_type') else '',
                'inspector': rec.get('inspector_name') or rec.get('submitted_by') or '',
                'quantity_inspected': rec.get('quantity_inspected') or '',
                'quantity_passed': rec.get('quantity_passed') or '',
                'result': rec.get('result') or '',
                'defect_codes': ', '.join(rec.get('defect_codes') or []),
                'defects': rec.get('defects') or '',
                'evidence': '; '.join(f"{EVIDENCE_META.get(t, {}).get('label', t)}: {e.get('result') or '—'}"
                                      for t, e in evidence.items() if isinstance(e, dict)),
                'missing_evidence': ', '.join(EVIDENCE_META.get(t, {}).get('label', t)
                                              for t in rec.get('missing_evidence') or []),
                'notes': rec.get('notes') or '',
                'review': review.get('status') or 'pending',
                'reviewer': review.get('reviewer_name') or review.get('reviewer') or '',
                'reviewed_at': review.get('reviewed_at') or '',
                'review_comment': review.get('comment') or '',
            })
    rows.sort(key=lambda r: (r['date'], r['submitted_at']), reverse=True)
    return rows


def filter_report_rows(rows, args):
    f = {key: (args.get(key) or '').strip() for key in REPORT_FILTERS}
    q = f['q'].lower()
    out = []
    for r in rows:
        if f['date_from'] and r['date'] < f['date_from']:
            continue
        if f['date_to'] and r['date'] > f['date_to']:
            continue
        if f['supplier'] and r['supplier'] != f['supplier']:
            continue
        if f['inspector'] and r['inspector'] != f['inspector']:
            continue
        if f['result'] and r['result'] != f['result']:
            continue
        if f['review'] and r['review'] != f['review']:
            continue
        if q and q not in ' '.join((r['report_no'], r['order_number'], r['item_code'],
                                    r['item_description'])).lower():
            continue
        out.append(r)
    return out, f


def _xl_safe(value):
    """Stop spreadsheet formula injection from typed-in text."""
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value


def report_summary(rows):
    """Pass / fail counts per supplier (the dashboard of the export)."""
    by_supplier = {}
    for r in rows:
        s = by_supplier.setdefault(r['supplier'] or '—', {'total': 0, 'Pass': 0, 'Fail': 0, 'Partial Pass': 0})
        s['total'] += 1
        if r['result'] in s:
            s[r['result']] += 1
    out = []
    for name, s in sorted(by_supplier.items(), key=lambda kv: -kv[1]['total']):
        out.append({'supplier': name, **s,
                    'pass_rate': round(s['Pass'] / s['total'] * 100, 1) if s['total'] else 0})
    return out


@app.route('/reports')
def reports():
    all_rows = inspection_report_rows()
    rows, filters = filter_report_rows(all_rows, request.args)
    return render_template('reports.html', rows=rows[:500], total=len(rows), filters=filters,
                           summary=report_summary(rows),
                           suppliers=sorted({r['supplier'] for r in all_rows if r['supplier']}),
                           inspectors=sorted({r['inspector'] for r in all_rows if r['inspector']}),
                           export_args={k: v for k, v in filters.items() if v})


@app.route('/reports/export.xlsx')
def reports_export():
    rows, _filters = filter_report_rows(inspection_report_rows(), request.args)
    columns = [
        ('report_no', '报告编号 Report No.', 30), ('date', '检验日期 Date', 12),
        ('region', '区域 Region', 12), ('order_number', '订单号 Order', 14),
        ('item_code', '物料编码 Item code', 18), ('item_description', '描述 Description', 36),
        ('supplier', '供应商 Supplier', 20), ('product_type', '产品类别 Product type', 22),
        ('inspector', '检验员 Inspector', 14), ('quantity_inspected', '检验数量 Qty inspected', 12),
        ('quantity_passed', '合格数量 Qty passed', 12), ('result', '结果 Result', 12),
        ('defect_codes', '缺陷代码 Defect codes', 16), ('defects', '缺陷描述 Defects', 30),
        ('evidence', '证据结果 Evidence results', 40), ('missing_evidence', '缺少证据 Missing evidence', 24),
        ('review', '审核 Review', 11), ('reviewer', '审核人 Reviewer', 14),
        ('reviewed_at', '审核时间 Reviewed (UTC)', 17), ('review_comment', '审核意见 Review comment', 24),
        ('notes', '备注 Notes', 30), ('submitted_at', '提交时间 Submitted (UTC)', 17),
    ]
    header_fill = openpyxl.styles.PatternFill('solid', fgColor='1A3A5C')
    header_font = openpyxl.styles.Font(color='FFFFFF', bold=True)
    result_fills = {'Pass': 'D1FAE5', 'Fail': 'FEE2E2', 'Partial Pass': 'FEF3C7'}

    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = 'Inspections'
    ws.append([label for _key, label, _w in columns])
    for (_key, _label, width), cell in zip(columns, ws[1]):
        cell.fill, cell.font = header_fill, header_font
        cell.alignment = openpyxl.styles.Alignment(wrap_text=True, vertical='top')
        ws.column_dimensions[cell.column_letter].width = width
    result_col = [key for key, _l, _w in columns].index('result') + 1
    for r in rows:
        values = []
        for key, _label, _w in columns:
            value = r[key]
            if key in ('submitted_at', 'reviewed_at'):
                dt = parse_server_time(value)
                value = dt.strftime('%Y-%m-%d %H:%M') if dt else value
            values.append(_xl_safe(value))
        ws.append(values)
        fill = result_fills.get(r['result'])
        if fill:
            ws.cell(row=ws.max_row, column=result_col).fill = openpyxl.styles.PatternFill('solid', fgColor=fill)
    ws.freeze_panes = 'B2'
    ws.auto_filter.ref = ws.dimensions

    summary = workbook.create_sheet('Summary')
    summary.append(['供应商 Supplier', '报告数 Reports', '合格 Pass', '不合格 Fail',
                    '部分合格 Partial', '合格率 Pass rate %'])
    for cell in summary[1]:
        cell.fill, cell.font = header_fill, header_font
    for s in report_summary(rows):
        summary.append([_xl_safe(s['supplier']), s['total'], s['Pass'], s['Fail'], s['Partial Pass'], s['pass_rate']])
    for letter, width in zip('ABCDEF', (28, 12, 10, 12, 14, 16)):
        summary.column_dimensions[letter].width = width

    buf = io.BytesIO()
    workbook.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f'inspection-reports-{china_today().isoformat()}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    config = load_config()
    if request.method == 'POST':
        raw_prefixes = request.form.get('valve_prefixes', 'RSV')
        config['valve_prefixes'] = [p.strip().upper() for p in raw_prefixes.split(',') if p.strip()]
        config['task_notify_emails'] = ', '.join(_email_list(request.form.get('task_notify_emails', '')))
        config['hq_report_emails'] = ', '.join(_email_list(request.form.get('hq_report_emails', '')))
        config['backup_emails'] = ', '.join(_email_list(request.form.get('backup_emails', '')))
        config['weekly_summary'] = request.form.get('weekly_summary') == '1'
        config['vtrust_notify_emails'] = ', '.join(_email_list(request.form.get('vtrust_notify_emails', '')))
        config['purchasing_emails'] = ', '.join(_email_list(request.form.get('purchasing_emails', '')))
        config['qa_mismatch_emails'] = ', '.join(_email_list(request.form.get('qa_mismatch_emails', '')))
        lead = request.form.get('vtrust_lead_days', type=int)
        config['vtrust_lead_days'] = lead if lead and 1 <= lead <= 90 else VTRUST_LEAD_DAYS
        if 'schedule_hidden_columns' in request.form:
            config['schedule_hidden_columns'] = [
                ' '.join(c.split()).lower()
                for c in re.split(r'[,\n]', request.form['schedule_hidden_columns']) if c.strip()]
        mode = request.form.get('hq_report_mode', 'all')
        config['hq_report_mode'] = mode if mode in HQ_REPORT_MODES else 'all'
        for legacy_key in ('smtp_pass', 'smtp_user', 'smtp_host', 'smtp_port', 'sheet_id', 'drive_folder_id'):
            config.pop(legacy_key, None)
        save_json(CONFIG_FILE, config)

        flash(tr('设置已保存', 'Settings saved'), 'success')
        return redirect(url_for('settings'))

    return render_template('settings.html',
                           config=config,
                           modules=MODULES,
                           evidence_coverage=evidence_rule_coverage(),
                           smtp_configured=smtp_configured(),
                           pdf_font_embedded=_pdf_font_embedded(),
                           excel_password_configured=bool(EXCEL_PASSWORD),
                           my_email=_current_user_email())


def _current_user_email():
    with db_conn() as conn:
        row = conn.execute('SELECT email FROM users WHERE id=?', (g.user_id,)).fetchone()
    return (row['email'] if row else '') or ''


# ── Backups ──────────────────────────────────────────────────────────────────
# Everything lives on one Railway volume, so admins can download a copy and a
# daily copy of the core data (database + JSON records, no photos) is e-mailed
# to the backup addresses in Settings.

def _sqlite_snapshot(dest):
    """Consistent copy of the live SQLite database (safe while it is in use)."""
    import sqlite3
    from db import DB_PATH
    src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def build_backup_zip(include_files=False):
    """Write a backup zip to a temporary file and return its path (the caller
    deletes it). Core data: database snapshot + data/*.json + weekly history.
    include_files adds inspection attachments, invoices and product images."""
    fd, path = tempfile.mkstemp(prefix='qc-backup-', suffix='.zip')
    os.close(fd)
    snapshot = path + '.db'
    try:
        _sqlite_snapshot(snapshot)
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, 'data/app.db')
            for root, _dirs, files in os.walk(DATA_DIR):
                for name in files:
                    if name.startswith('app.db'):        # live database, -wal, -journal
                        continue
                    full = os.path.join(root, name)
                    zf.write(full, os.path.relpath(full, APP_DATA_DIR))
            if include_files:
                for base in (UPLOAD_DIR, PRODUCT_IMG_DIR):
                    for root, _dirs, files in os.walk(base):
                        for name in files:
                            full = os.path.join(root, name)
                            # photos / videos / PDFs are already compressed
                            zf.write(full, os.path.relpath(full, APP_DATA_DIR), compress_type=zipfile.ZIP_STORED)
    except Exception:
        _remove_quietly(path)
        raise
    finally:
        _remove_quietly(snapshot)
    return path


def _backup_day():
    return china_today().isoformat()


def send_backup_email(force=False):
    """E-mail the core-data backup to Settings → backup e-mails, once a day
    (force=True sends again). Returns (ok, message)."""
    config = load_config()
    recipients = _email_list(config.get('backup_emails', ''))
    if not recipients:
        return False, tr('未设置备份邮箱', 'No backup e-mail configured')
    day = _backup_day()
    if not force and config.get('backup_last_day') == day:
        return False, tr('今天已发送', 'Already sent today')
    path = build_backup_zip(include_files=False)
    try:
        size = os.path.getsize(path)
        name = f'qc-backup-data-{day}.zip'
        lines = [f'质检系统每日数据备份 / Daily QC data backup — {day}',
                 f'文件 File: {name} ({size / 1024 / 1024:.1f} MB)',
                 '内容：数据库（用户、任务、审核）、检验记录、排期历史；不含照片和附件。',
                 'Contents: database (users, tasks, reviews), inspection records and schedule history; '
                 'photos and attachments are not included.',
                 '',
                 '请妥善保管：备份中包含账号信息（密码已加密）。',
                 'Keep it safe: it contains account data (passwords are hashed).']
        attachments = []
        if size <= MAX_EMAIL_ATTACHMENT:
            with open(path, 'rb') as fh:
                attachments.append((name, fh.read(), 'application/zip'))
        else:
            lines += ['', '备份文件太大，无法作为附件发送，请在“设置 → 数据备份”中下载。',
                      'The backup is too large to attach; download it from Settings → Backups.']
        ok, msg = _smtp_send(f'【数据备份】QC data backup {day}', '\n'.join(lines), recipients, attachments)
    finally:
        _remove_quietly(path)
    config = load_config()
    config['backup_last_day'] = day
    config['backup_last_at'] = datetime.now(_tz.utc).strftime('%Y-%m-%d %H:%M')
    config['backup_last_status'] = 'ok' if ok else msg
    save_json(CONFIG_FILE, config)
    return ok, msg


@app.route('/settings/backup', methods=['POST'])
def settings_backup():
    full = request.form.get('scope') == 'full'
    path = build_backup_zip(include_files=full)
    stamp = datetime.now(_tz.utc).strftime('%Y%m%d-%H%M')
    response = send_file(path, mimetype='application/zip', as_attachment=True,
                         download_name=f"qc-backup-{'full' if full else 'data'}-{stamp}.zip")
    response.call_on_close(lambda: _remove_quietly(path))
    return response


@app.route('/settings/backup/email', methods=['POST'])
def settings_backup_email():
    ok, msg = send_backup_email(force=True)
    flash(msg if not ok else tr('备份邮件已发送', 'Backup e-mail sent'), 'success' if ok else 'error')
    return redirect(url_for('settings') + '#backup')


@app.route('/settings/test-email', methods=['POST'])
def settings_test_email():
    """Send a test message so admins can check the SMTP settings at once."""
    recipients = _email_list(request.form.get('to', ''))
    if not recipients:
        flash(tr('请填写有效的邮箱地址', 'Enter a valid e-mail address'), 'error')
        return redirect(url_for('settings') + '#email')
    sender = smtp_sender()[1]
    body = '\n'.join([
        '这是质检系统发出的测试邮件。收到说明邮件设置正确。',
        'This is a test e-mail from the QC system. If you received it, e-mail is set up correctly.',
        '',
        f'发件人 Sender: {sender}',
        f'时间 Time: {dual_zone_time()}',
        '',
        url_for('settings', _external=True),
    ])
    ok, msg = _smtp_send('【测试邮件】QC system test e-mail', body, recipients)
    flash(msg, 'success' if ok else 'error')
    return redirect(url_for('settings') + '#email')


@app.route('/settings/reference', methods=['POST'])
def settings_reference():
    """Import reference.xlsx (sheets 'ReferenceData' and 'Product Codes'),
    which classifies products for the required-evidence rules."""
    f = request.files.get('reference')
    if not f or _upload_extension(f.filename) != '.xlsx':
        flash(tr('请选择 .xlsx 产品对照表', 'Choose the .xlsx product reference file'), 'error')
        return redirect(url_for('settings'))
    try:
        items = evidence_rules.parse_reference_workbook(f.read())
    except Exception:
        logger.exception('Reference import failed')
        items = {}
    if not items:
        flash(tr('无法读取产品对照表（需要 ReferenceData 或 Product Codes 工作表）',
                 'Could not read the reference file (needs a ReferenceData or Product Codes sheet)'), 'error')
        return redirect(url_for('settings'))
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    with db_conn() as conn:
        conn.execute('DELETE FROM product_reference')
        conn.executemany(
            'INSERT INTO product_reference (code, description, category, sub_category, pc_category, updated_at) '
            'VALUES (?,?,?,?,?,?)',
            [(c, v.get('description', ''), v.get('category', ''), v.get('sub_category', ''),
              v.get('pc_category', ''), now) for c, v in items.items()])
    flash(tr(f'已导入 {len(items)} 个产品编码', f'Imported {len(items)} product codes'), 'success')
    return redirect(url_for('settings'))


def evidence_rule_coverage():
    """(reference count, updated, [unclassified (code, description)]) for
    the items on the current schedule."""
    with db_conn() as conn:
        row = conn.execute('SELECT COUNT(*) AS n, MAX(updated_at) AS t FROM product_reference').fetchone()
        refs = {r['code']: dict(r) for r in conn.execute('SELECT * FROM product_reference')}
    unknown, seen = [], set()
    for sheet, rows in load_schedule(CURRENT_FILE).items():
        if not rows:
            continue
        hl = [str(h).lower() for h in rows[0]]
        if 'item code' not in hl:
            continue
        ic = hl.index('item code')
        dc = hl.index('item description') if 'item description' in hl else None
        for r in rows[1:]:
            code = str(r[ic] if ic < len(r) else '').strip()
            if not code or code.upper() in seen:
                continue
            seen.add(code.upper())
            desc = str(r[dc]) if dc is not None and dc < len(r) else ''
            if not evidence_rules.classify(code, desc, refs.get(code.upper())):
                unknown.append((code, desc))
    return row['n'], row['t'], unknown


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


# Short link labels for the HTML version of e-mails. Corporate mail filters
# (e.g. Microsoft 365 Safe Links) rewrite every URL into a very long one, so
# links are shown as short labels instead of the address itself.
_EMAIL_LINK_LABELS = (
    ('/report.pdf', '下载 PDF 报告 / PDF report'),
    ('/attachments/', '下载附件 / Download attachment'),
    ('/inspect/', '打开检验页面 / Open inspection'),
    ('/tasks', '查看任务 / Open tasks'),
)
_URL_RE = re.compile(r'https?://[^\s<>"]+')


def _email_link_label(url):
    path = url.split('?', 1)[0]
    for marker, label in _EMAIL_LINK_LABELS:
        if marker in path:
            return label
    return '打开链接 / Open link'


def _email_inline(text):
    """Escape one line and turn its URLs into short labelled links."""
    import html as _html
    parts, last = [], 0
    for match in _URL_RE.finditer(text):
        parts.append(_html.escape(text[last:match.start()]))
        url = match.group(0)
        parts.append(f'<a href="{_html.escape(url, quote=True)}" style="color:#1a6fc4;font-weight:600;">'
                     f'{_html.escape(_email_link_label(url))}</a>')
        last = match.end()
    parts.append(_html.escape(text[last:]))
    return ''.join(parts)


_EMAIL_FONT = "font-family:-apple-system,'Segoe UI','Microsoft YaHei',Arial,sans-serif;"
_LABEL_RE = re.compile(r'^([^:：]{1,40}?[:：])\s*(.+)$')


def email_html(body):
    """HTML version of a plain-text e-mail body, laid out with real HTML
    elements (Outlook ignores CSS white-space, so line breaks must be tags):
    first line as a title, 'Label: value' pairs, numbered items in bold,
    several fields on one line split into separate lines, and lone URLs as
    buttons. Inline URLs become short labelled links."""
    import html as _html
    out = []
    first = True
    for raw in body.split('\n'):
        stripped = raw.strip()
        if not stripped:
            out.append('<div style="height:10px;line-height:10px;">&nbsp;</div>')
            continue
        indent = 18 if raw.startswith(' ') else 0
        if first:
            out.append(f'<p style="margin:0 0 10px;font-size:17px;font-weight:700;color:#1a3a5c;">'
                       f'{_email_inline(stripped)}</p>')
            first = False
            continue
        if _URL_RE.fullmatch(stripped):  # a link on its own line -> button
            url = _html.escape(stripped, quote=True)
            out.append(f'<p style="margin:6px 0 8px {indent}px;">'
                       f'<a href="{url}" style="display:inline-block;background:#1a3a5c;color:#ffffff;'
                       f'text-decoration:none;font-weight:600;padding:6px 14px;border-radius:6px;">'
                       f'{_html.escape(_email_link_label(stripped))}</a></p>')
            continue
        # "描述 Description: X    数量 Qty: 400" -> one field per line
        for field in re.split(r'\s{3,}', stripped):
            numbered = re.match(r'^(\d+\.)\s+(.*)$', field)
            label = None if _URL_RE.match(field) else _LABEL_RE.match(field)
            if numbered:
                html_line = (f'<span style="color:#1a3a5c;">{numbered.group(1)}</span> '
                             f'<strong>{_email_inline(numbered.group(2))}</strong>')
                style = f'margin:10px 0 2px {indent}px;'
            elif label and 'http' not in label.group(1).lower():
                html_line = (f'<span style="color:#6b7280;">{_html.escape(label.group(1))}</span> '
                             f'<strong>{_email_inline(label.group(2))}</strong>')
                style = f'margin:2px 0 2px {indent}px;'
            else:
                html_line = _email_inline(field)
                style = f'margin:2px 0 2px {indent}px;'
            out.append(f'<p style="{style}">{html_line}</p>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            '<tr><td style="padding:4px 0;">'
            f'<div style="{_EMAIL_FONT}font-size:14px;line-height:1.6;color:#1f2937;max-width:680px;">'
            + ''.join(out) + '</div></td></tr></table>')


# Ports that use implicit TLS from the first byte (465 standard, 994 NetEase).
SMTP_SSL_PORTS = (465, 994)


def smtp_sender():
    """(envelope address, From header). SMTP_FROM_NAME is the display name
    shown in the recipient's inbox, e.g. 'DAEMCO-QC <qc@example.com>'."""
    from email.utils import formataddr, parseaddr
    raw = os.environ.get('SMTP_FROM', '').strip() or os.environ.get('SMTP_USERNAME', '').strip()
    raw_name, address = parseaddr(raw)
    name = os.environ.get('SMTP_FROM_NAME', '').strip() or raw_name
    return address, formataddr((name, address), charset='utf-8') if name else address


def _smtp_send(subject, body, recipients, attachments=(), html=None):
    """Send a UTF-8 e-mail (plain text plus an HTML version with short link
    labels, or the given `html`). Returns (ok, message).
    attachments: [(filename, bytes, 'maintype/subtype'), ...]

    Port 465 uses implicit TLS (common for Chinese corporate mail such as
    Aliyun / Tencent Exmail); other ports use STARTTLS.
    """
    host = os.environ.get('SMTP_HOST', '').strip()
    port = int(os.environ.get('SMTP_PORT', '587'))
    user = os.environ.get('SMTP_USERNAME', '').strip()
    pwd  = os.environ.get('SMTP_PASSWORD', '')
    sender, from_header = smtp_sender()
    if not all([host, user, pwd]):
        return False, tr('邮件服务未配置', 'SMTP not configured')
    if not recipients:
        return False, tr('没有收件人', 'No recipients')

    import smtplib
    from email.header import Header
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    content = MIMEMultipart('alternative')
    content.attach(MIMEText(body, 'plain', 'utf-8'))
    content.attach(MIMEText(html or email_html(body), 'html', 'utf-8'))
    if attachments:
        msg = MIMEMultipart('mixed')
        msg.attach(content)
        for filename, data, mimetype in attachments:
            part = MIMEApplication(data, _subtype=mimetype.split('/', 1)[-1])
            part.add_header('Content-Disposition', 'attachment', filename=('utf-8', '', filename))
            msg.attach(part)
    else:
        msg = content
    msg['Subject'] = str(Header(subject, 'utf-8'))
    msg['From']    = from_header
    msg['To']      = ', '.join(recipients)
    # Steps are named so a failure says where it stopped (connect / login / send).
    stage = 'connect'
    try:
        if port in SMTP_SSL_PORTS:
            srv = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            srv = smtplib.SMTP(host, port, timeout=15)
            srv.ehlo()
            stage = 'starttls'
            srv.starttls()
        with srv:
            stage = 'login'
            srv.login(user, pwd)
            stage = 'send'
            srv.sendmail(sender, recipients, msg.as_string())
        return True, tr(f'邮件已发送至 {", ".join(recipients)}', f'E-mail sent to {", ".join(recipients)}')
    except Exception as exc:
        detail = ' '.join(str(exc).split())[:200]
        # one log line with everything except the password (Railway splits tracebacks)
        logger.error('E-mail delivery failed at %s: %s: %s (host=%s port=%s user=%s)',
                     stage, type(exc).__name__, detail, host, port, user)
        logger.debug('E-mail delivery traceback', exc_info=True)
        stage_text = {'connect': tr('连接服务器', 'connecting'), 'starttls': tr('加密握手', 'TLS handshake'),
                      'login': tr('登录', 'login'), 'send': tr('发送', 'sending')}[stage]
        return False, (tr('邮件发送失败', 'E-mail delivery failed') + f' [{stage_text}] ({type(exc).__name__}'
                       + (f': {detail}' if detail else '') + ')')


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

    today = china_today().isoformat()
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
    return (send_due_reminders(), send_review_reminders(), send_vtrust_reminders()[0],
            send_vtrust_reschedule_alerts()[0])


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


DATE_CHANGE_GROUPS = (('delayed', '⬆ 延后 / Delayed'), ('earlier', '⬇ 提前 / Earlier'),
                      ('added', '＋ 新增日期 / Date added'), ('removed', '✕ 日期被删除 / Date removed'),
                      ('remark', '✎ 只改了备注 / Remark only'))


def date_change_kind(old, new):
    """How a schedule date cell changed: (kind, days, label) with kind in
    delayed / earlier / added / removed / remark."""
    (d1, _n1), (d2, _n2) = split_est(old), split_est(new)
    if d1 and d2 and d1 != d2:
        days = (d2 - d1).days
        if days > 0:
            return 'delayed', days, f'⬆ 延后 {days} 天 / delayed {days} d'
        return 'earlier', -days, f'⬇ 提前 {-days} 天 / earlier by {-days} d'
    if d2 and not d1:
        return 'added', None, '＋ 新增日期 / date added'
    if d1 and not d2:
        return 'removed', None, '✕ 日期被删除 / date removed'
    return 'remark', None, '✎ 只改了备注 / remark only'


def _send_date_change_email(changes, edited_by=''):
    """Tell the lead and the assigned inspectors that completion / ship dates
    moved: grouped as delayed (most first), earlier, date added / removed and
    remark only, each date with its trend and a warning when the new
    completion date is already in the past."""
    recipients = _task_recipients(changes)
    if not recipients:
        return False, tr('未设置通知邮箱', 'No notification e-mail configured')
    today = china_today()
    ids = {c['assigned_to'] for c in changes if c.get('assigned_to')}
    names = {}
    if ids:
        with db_conn() as conn:
            names = {r['id']: r['display_name'] or r['username'] for r in conn.execute(
                f"SELECT id, username, display_name FROM users WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))}
    bookings = vtrust_bookings()
    items = []
    for c in changes:
        est = date_change_kind(c['old_est'], c['new_est']) if est_changed(c['old_est'], c['new_est']) else None
        ship = (date_change_kind(c.get('old_ship'), c.get('new_ship'))
                if est_changed(c.get('old_ship'), c.get('new_ship')) else None)
        if est or ship:
            # group by the completion date, unless only its remark changed and the ship date moved
            main = est if est and (est[0] != 'remark' or not ship) else ship
            items.append((c, est, ship, main[0], main[1] or 0))
    counts = {kind: sum(1 for item in items if item[3] == kind) for kind, _ in DATE_CHANGE_GROUPS}
    zh = {'delayed': '延后', 'earlier': '提前', 'added': '新增日期', 'removed': '日期被删除', 'remark': '只改备注'}
    en = {'delayed': 'delayed', 'earlier': 'earlier', 'added': 'date added', 'removed': 'date removed', 'remark': 'remark only'}
    summary_zh = ' · '.join(f'{zh[k]} {n}' for k, n in counts.items() if n)
    summary_en = ', '.join(f'{n} {en[k]}' for k, n in counts.items() if n)
    lines = [f"预计完成日变动 Completion date changes — {today.isoformat()}",
             f"{len(items)} 个未完成任务的日期有变动：{summary_zh}",
             f"{len(items)} open task(s) changed: {summary_en}.", '']
    if edited_by:
        lines.insert(3, f"由 {edited_by} 手动修改 / Edited manually by {edited_by}")
    n = 0
    for kind, heading in DATE_CHANGE_GROUPS:
        group = [item for item in items if item[3] == kind]
        if not group:
            continue
        if kind in ('delayed', 'earlier'):
            group.sort(key=lambda item: -item[4])                           # biggest move first
        lines.append(f"■ {heading} ({len(group)})")
        for c, est, ship, _kind, _days in group:
            n += 1
            lines.append(f"{n}. [{c['region']}]  {c['order_number']}  {c['item_code']}"
                         + (f"  {c['description']}" if c.get('description') else ''))
            if est:
                new_d, _ = split_est(c['new_est'])
                past = (f"   ⚠ 新日期已过 {(today - new_d).days} 天 / already {(today - new_d).days} d ago"
                        if new_d and new_d < today else '')
                lines.append(f"    预计完成 Est. completion: {_est_label(c['old_est'])}  →  "
                             f"{_est_label(c['new_est'])}   [{est[2]}]{past}")
            if ship:
                lines.append(f"    最迟出货 Must ship: {_est_label(c.get('old_ship'))}  →  "
                             f"{_est_label(c.get('new_ship'))}   [{ship[2]}]")
            booking = bookings.get(c['job_key'])
            if booking:
                check, new_d = booking_check_date(booking), split_est(c['new_est'])[0]
                when = booking.get('planned_date') or '—'
                if check and new_d and new_d > check:
                    lines.append(f"    ⚠ V-Trust 已预约 Job {booking['job_number']}（检验日 {when}），新完成日晚于预约，需要改期"
                                 f" / V-Trust job booked for {when}: reschedule")
                else:
                    lines.append(f"    V-Trust 已预约 Job {booking['job_number']}（检验日 {when}）/ V-Trust job booked for {when}")
            if c.get('assigned_to'):
                lines.append(f"    已分配 Assigned: {names.get(c['assigned_to'], '')}")
            lines.append('    ' + url_for('inspect_form', job_key=c['job_key'], _external=True))
            lines.append('')
    return _smtp_send(f"【日期变动】{len(items)} 项：{summary_zh} / {len(items)} inspection task(s): {summary_en}",
                      '\n'.join(lines), recipients)


REMINDER_DAYS = 14


def send_due_reminders():
    """Once per task and completion date: e-mail when an open task is within
    two weeks of its estimated completion date. A changed date re-arms it."""
    today = china_today()
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


# ── V-Trust booking reminder ─────────────────────────────────────────────────
# Valves need a third-party (V-Trust) inspection at the factory. Two weeks
# before a valve line is due, admins are told to book it.

VTRUST_LEAD_DAYS = 14
VTRUST_PAST_DAYS = 7     # lines further past their date are taken as already handled


def needs_vtrust(item_code, description, config=None):
    """Valves (code prefix / "valve" in the description) except product types
    whose evidence rules have no V-Trust, e.g. a "Valve Box COVER"."""
    if not is_valve(description, item_code, config):
        return False
    ptype = product_type_for(item_code, description)
    if not ptype:
        return True
    return any(e['type'] == 'vtrust' for e in evidence_rules.PRODUCT_TYPES[ptype]['evidence'])


def _qty_sum(a, b):
    try:
        total = float(a or 0) + float(b or 0)
        return str(int(total)) if total == int(total) else str(total)
    except ValueError:
        return ' + '.join(x for x in (a, b) if x)


VTRUST_STATUS = {
    'reschedule': ('需改期', 'Reschedule'), 'to_book': ('待预约', 'To book'), 'booked': ('已预约', 'Booked'),
    'later': ('以后再约', 'Later'), 'overdue': ('已过完成日', 'Past due'), 'no_date': ('无完成日', 'No date'),
    'done': ('已完成', 'Done'),
}


def vtrust_bookings():
    """{job_key: booking row} — V-Trust job numbers entered by admins."""
    with db_conn() as conn:
        return {r['job_key']: dict(r) for r in conn.execute('SELECT * FROM vtrust_bookings')}


def booking_check_date(booking):
    """The date a booking was made for: the planned inspection date, else the
    estimated completion date when it was booked."""
    return _parse_date(booking.get('planned_date')) or split_est(booking.get('est_at_booking'))[0]


def vtrust_lines(lead_days=None):
    """Every valve line in the current schedule with its V-Trust status:
    done (V-Trust passed), reschedule (booked, but completion now later than
    the booked date), booked, to_book (within the lead time or up to
    VTRUST_PAST_DAYS past), later, overdue (further past), no_date.
    Split lots of one PO + item are added together."""
    import copy
    config = load_config()
    lead = int(lead_days if lead_days is not None else config.get('vtrust_lead_days') or VTRUST_LEAD_DAYS)
    schedule, _ = _apply_est_overrides(copy.deepcopy(load_schedule(CURRENT_FILE)), commit=False)
    inspections = SharedReports(load_json(INSPECTIONS_CACHE, {}))
    bookings = vtrust_bookings()
    today = china_today()
    lines = {}
    for sheet, rows in schedule.items():
        if not rows or len(rows) < 2:
            continue
        headers = rows[0]
        low = [str(h).strip().lower() for h in headers]

        def col(row, *names):
            for name in names:
                if name in low:
                    i = low.index(name)
                    return str(row[i]).strip() if i < len(row) and row[i] is not None else ''
            return ''

        for row in rows[1:]:
            code, desc = col(row, 'item code'), col(row, 'item description')
            if not code or not needs_vtrust(code, desc, config):
                continue
            job_key = make_job_key(sheet, row, headers)
            if not job_key.split('|')[1]:
                continue
            est_text = col(row, 'estimated completion date')
            est, _ = split_est(est_text)
            line = lines.get(job_key)
            if line:                                   # another lot of the same line
                line['qty'] = _qty_sum(line['qty'], col(row, 'quantity'))
                if est and (not line['est'] or est < line['est']):
                    line.update(est=est, est_text=est_text)
                continue
            lines[job_key] = {'job_key': job_key, 'region': sheet, 'dpl': col(row, 'order number'),
                              'po': col(row, 'daemco purchase order', 'purchase order'), 'code': code,
                              'description': desc, 'qty': col(row, 'quantity'),
                              'supplier': col(row, 'supplier', 'foundry'), 'est': est, 'est_text': est_text}
    for line in lines.values():
        line['days'] = (line['est'] - today).days if line['est'] else None
        line['booking'] = booking = bookings.get(line['job_key'])
        check = booking_check_date(booking) if booking else None
        line['late_by'] = (line['est'] - check).days if booking and check and line['est'] and line['est'] > check else 0
        if get_vtrust_status(inspections.get(line['job_key'], [])).lower() == 'pass':
            line['status'] = 'done'
        elif booking:
            line['status'] = 'reschedule' if line['late_by'] else 'booked'
        elif line['days'] is None:
            line['status'] = 'no_date'
        elif line['days'] > lead:
            line['status'] = 'later'
        elif line['days'] < -VTRUST_PAST_DAYS:
            line['status'] = 'overdue'
        else:
            line['status'] = 'to_book'
    return sorted(lines.values(), key=lambda l: (l['supplier'] or '~', l['est'] or date.max, l['dpl'], l['code']))


def vtrust_due_lines(lead_days=None):
    """Valve lines to book now: within the lead time (or up to VTRUST_PAST_DAYS
    past), no V-Trust job number yet and no passed V-Trust inspection."""
    return [l for l in vtrust_lines(lead_days) if l['status'] == 'to_book']


def vtrust_recipients():
    """'V-Trust reminder e-mails' from Settings, else every active admin with an e-mail."""
    configured = _email_list(load_config().get('vtrust_notify_emails', ''))
    if configured:
        return configured
    with db_conn() as conn:
        return _email_list(', '.join(r['email'] for r in conn.execute(
            "SELECT email FROM users WHERE role='admin' AND active=1 AND IFNULL(email, '') != ''")))


def _vtrust_days_text(days):
    if days is None:
        return '—'
    if days > 0:
        return f'还有 {days} 天 / in {days} d'
    if days == 0:
        return '今天 / today'
    return f'已过 {-days} 天 / {-days} d past'


VTRUST_COLUMNS = ('DPL', 'Daemco purchase order number', 'PRODUCT CODE', 'DESCRIPTION', 'QTY',
                  'ESTIMATED COMPLETION TIME', 'SUPPLIER', 'REGION')


def vtrust_email(lines, lead, booked=()):
    """(subject, plain text, html, xlsx bytes) for valve lines to book; `booked`
    lines in the same window are listed after them for reference."""
    today = china_today().isoformat()
    subject = (f'【V-Trust 预约提醒】{len(lines)} 个阀门订单行将在 {lead} 天内完工 / '
               f'{len(lines)} valve line(s) ready within {lead} days — book V-Trust')
    intro_zh = f'以下阀门预计在 {lead} 天内完工（或刚过预计完成日不超过 {VTRUST_PAST_DAYS} 天），尚无 V-Trust 合格记录，请安排第三方来厂检验。'
    intro_en = (f'These valves are due within {lead} days (or at most {VTRUST_PAST_DAYS} days past their date) and have no passed V-Trust '
                f'inspection yet. Please book the third-party inspection at the factory.')
    text = [f'V-Trust 预约提醒 V-Trust booking reminder — {today}', intro_zh, intro_en,
            '预约后请在系统的 V-Trust 页面录入 job number。 After booking, enter the job number on the V-Trust page.', '']
    for i, l in enumerate(lines, 1):
        text.append(f"{i}. {l['dpl']}  {l['po']}  {l['code']}  {l['description']}  QTY {l['qty']}")
        text.append(f"    {l['est_text']}  ({_vtrust_days_text(l['days'])})  · {l['supplier'] or '—'} · {l['region']}")
    if booked:
        text += ['', f'已预约（供参考）Already booked ({len(booked)})']
        text += [f"  {b['dpl']}  {b['po']}  {b['code']}  · Job {b['booking']['job_number']}"
                 f"  · {b['booking']['planned_date'] or '—'}" for b in booked]
    text += ['', '附件为同样内容的 Excel，可直接转发给 V-Trust。', 'The attached Excel has the same list, ready to forward to V-Trust.']

    cell = 'border:1px solid #d0d5dd;padding:6px 10px;font-size:13px;'
    head = cell + 'background:#1a3a5c;color:#fff;font-weight:700;text-align:center;'
    rows_html, supplier = [], None
    for l in lines:
        if l['supplier'] != supplier:
            supplier = l['supplier']
            rows_html.append(f'<tr><td colspan="8" style="{cell}background:#eef2f7;font-weight:700;">'
                             f'{_escape(supplier or "—")}</td></tr>')
        late = (l['days'] or 0) < 0
        rows_html.append(
            '<tr>' + ''.join(f'<td style="{cell}{extra}">{_escape(v)}</td>' for v, extra in (
                (l['dpl'], 'text-align:center;'), (l['po'], 'text-align:center;'), (l['code'], ''),
                (l['description'], ''), (l['qty'], 'text-align:center;'), (l['est_text'], 'text-align:center;'),
                (_vtrust_days_text(l['days']), 'text-align:center;' + ('color:#b91c1c;font-weight:700;' if late else '')),
                (l['region'], 'text-align:center;'))) + '</tr>')
    headers = ('DPL', 'Daemco PO', 'PRODUCT CODE', 'DESCRIPTION', 'QTY', 'ESTIMATED COMPLETION TIME',
               '距完成 / Days', 'REGION')
    head_row = ''.join(f'<th style="{head}">{_escape(h)}</th>' for h in headers)
    booked_html = ''
    if booked:
        bhead = ''.join(f'<th style="{head}">{_escape(h)}</th>' for h in (
            'DPL', 'Daemco PO', 'PRODUCT CODE', 'ESTIMATED COMPLETION TIME', 'V-Trust Job', '预约检验日 / Planned'))
        brows = ''.join('<tr>' + ''.join(f'<td style="{cell}text-align:center;">{_escape(v)}</td>' for v in (
            b['dpl'], b['po'], b['code'], b['est_text'], b['booking']['job_number'], b['booking']['planned_date'] or '—'))
            + '</tr>' for b in booked)
        booked_html = (f'<p style="font-size:14px;margin:16px 0 6px;"><b>已预约（供参考）/ Already booked ({len(booked)})</b></p>'
                       f'<table style="border-collapse:collapse;"><tr>{bhead}</tr>{brows}</table>')
    font = "Arial,'Microsoft YaHei',sans-serif"
    html = (f'<div style="font-family:{font};color:#1a1a2e;">'
            f'<p style="font-size:14px;margin:0 0 4px;"><b>V-Trust 预约提醒 / V-Trust booking reminder — {today}</b></p>'
            f'<p style="font-size:13px;margin:0 0 2px;">{_escape(intro_zh)}</p>'
            f'<p style="font-size:13px;margin:0 0 2px;color:#4b5563;">{_escape(intro_en)}</p>'
            f'<p style="font-size:13px;margin:0 0 12px;">预约后请在系统的 <a href="{url_for("vtrust_page", _external=True)}">'
            f'V-Trust 页面</a>录入 job number。 After booking, enter the job number on the V-Trust page.</p>'
            f'<table style="border-collapse:collapse;">'
            f'<tr>{head_row}</tr>'
            + ''.join(rows_html) + '</table>' + booked_html +
            f'<p style="font-size:12px;color:#6b7280;margin-top:12px;">附件为同样内容的 Excel，可直接转发给 V-Trust。'
            f' The attached Excel has the same list, ready to forward to V-Trust.<br>'
            f'<a href="{url_for("index", _external=True)}">{_escape(url_for("index", _external=True))}</a></p></div>')

    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'V-Trust'
    ws.append(list(VTRUST_COLUMNS))
    for l in lines:
        ws.append([_xl_safe(v) for v in (l['dpl'], l['po'], l['code'], l['description'], l['qty'],
                                         l['est_text'], l['supplier'], l['region'])])
    for c in ws[1]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='1A3A5C')
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
    for letter, width in zip('ABCDEFGH', (12, 16, 18, 48, 8, 28, 12, 14)):
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = 'A2'
    buf = io.BytesIO()
    wb.save(buf)
    return subject, '\n'.join(text), html, buf.getvalue()


def _vtrust_booked_in_window(all_lines, lead):
    return [l for l in all_lines if l['status'] in ('booked', 'reschedule')
            and l['days'] is not None and -VTRUST_PAST_DAYS <= l['days'] <= lead]


def send_vtrust_reminders(force=False):
    """E-mail admins about valve lines entering the V-Trust booking window
    (lines with a job number are not "to book"). Each line is sent once per
    estimated completion date (a changed date sends it again); force=True
    sends the whole current list. Returns (count, message)."""
    config = load_config()
    lead = int(config.get('vtrust_lead_days') or VTRUST_LEAD_DAYS)
    all_lines = vtrust_lines(lead)
    lines = [l for l in all_lines if l['status'] == 'to_book']
    claimed = []
    if not force:
        with db_conn() as conn:
            for l in lines:
                if conn.execute('INSERT OR IGNORE INTO vtrust_reminders (job_key, est_date) VALUES (?,?)',
                                (l['job_key'], l['est'].isoformat())).rowcount:
                    claimed.append(l)
        lines = claimed
    if not lines:
        return 0, tr('没有需要提醒的阀门', 'No valves to remind about')

    def release():
        with db_conn() as conn:
            for l in claimed:
                conn.execute('DELETE FROM vtrust_reminders WHERE job_key=? AND est_date=?',
                             (l['job_key'], l['est'].isoformat()))

    recipients = vtrust_recipients()
    if not recipients:
        release()
        return 0, tr('未设置 V-Trust 提醒邮箱，管理员账号也没有邮箱', 'No V-Trust reminder e-mail and no admin e-mail')
    subject, text, html, xlsx = vtrust_email(lines, lead, _vtrust_booked_in_window(all_lines, lead))
    ok, msg = _smtp_send(subject, text, recipients, html=html, attachments=[
        (f'V-Trust_{china_today().isoformat()}.xlsx', xlsx,
         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')])
    if not ok:
        release()
        return 0, msg
    return len(lines), msg


def send_vtrust_reschedule_alerts():
    """Booked valve lines whose completion date is now later than the booked
    date: tell the V-Trust recipients to reschedule. Once per line and new
    completion date. Returns (count, message)."""
    lines = [l for l in vtrust_lines() if l['status'] == 'reschedule']
    claimed = []
    with db_conn() as conn:
        for l in lines:
            marker = l['est'].isoformat()
            if conn.execute("UPDATE vtrust_bookings SET reschedule_alert=? WHERE job_key=? "
                            "AND IFNULL(reschedule_alert, '') != ?", (marker, l['job_key'], marker)).rowcount:
                claimed.append(l)
    if not claimed:
        return 0, tr('没有需要改期的 V-Trust 预约', 'No V-Trust booking needs rescheduling')

    def release():
        with db_conn() as conn:
            for l in claimed:
                conn.execute("UPDATE vtrust_bookings SET reschedule_alert='' WHERE job_key=?", (l['job_key'],))

    recipients = vtrust_recipients()
    if not recipients:
        release()
        return 0, tr('未设置 V-Trust 提醒邮箱，管理员账号也没有邮箱', 'No V-Trust reminder e-mail and no admin e-mail')
    today = china_today().isoformat()
    lines_txt = [f'V-Trust 需改期 V-Trust reschedule — {today}',
                 f'以下 {len(claimed)} 个已预约 V-Trust 的阀门，预计完成日已推迟到预约日之后，请联系 V-Trust 改期，并在系统 V-Trust 页面更新预约日。',
                 f'{len(claimed)} booked valve line(s) will now be ready after the booked V-Trust date. '
                 f'Please reschedule with V-Trust and update the date on the V-Trust page.', '']
    for i, l in enumerate(sorted(claimed, key=lambda l: -l['late_by']), 1):
        b = l['booking']
        lines_txt.append(f"{i}. Job {b['job_number']}  ·  {l['dpl']}  {l['po']}  {l['code']}  {l['description']}  QTY {l['qty']}")
        lines_txt.append(f"    预约日 Booked for: {booking_check_date(b).isoformat()}"
                         f"{'' if b.get('planned_date') else '（预约时的完成日 / est. when booked）'}"
                         f"   →   新完成日 New est. completion: {_est_label(l['est_text'])}"
                         f"   [⬆ 晚 {l['late_by']} 天 / {l['late_by']} d later]")
        lines_txt.append('')
    lines_txt.append(url_for('vtrust_page', _external=True))
    ok, msg = _smtp_send(f'【V-Trust 需改期】{len(claimed)} 个阀门完工推迟到预约日之后 / '
                         f'{len(claimed)} V-Trust booking(s) need rescheduling', '\n'.join(lines_txt), recipients)
    if not ok:
        release()
        return 0, msg
    return len(claimed), msg


@app.route('/vtrust')
def vtrust_page():
    """Admin: valve lines and their V-Trust job numbers."""
    lead = int(load_config().get('vtrust_lead_days') or VTRUST_LEAD_DAYS)
    lines = vtrust_lines(lead)
    show = request.args.get('show', 'open')
    counts = Counter(l['status'] for l in lines)
    order = list(VTRUST_STATUS)
    if show == 'open':
        shown = [l for l in lines if l['status'] in ('reschedule', 'to_book', 'booked', 'overdue')]
    elif show in VTRUST_STATUS:
        shown = [l for l in lines if l['status'] == show]
    else:
        shown = lines
    shown.sort(key=lambda l: (order.index(l['status']), l['est'] or date.max, l['dpl'], l['code']))
    return render_template('vtrust.html', lines=shown, counts=counts, show=show, lead=lead,
                           statuses=VTRUST_STATUS, past_days=VTRUST_PAST_DAYS, today=china_today().isoformat())


@app.route('/vtrust/book', methods=['POST'])
def vtrust_book():
    keys = request.form.getlist('job_key')
    job_number = ' '.join(request.form.get('job_number', '').split())[:60]
    planned_raw = request.form.get('planned_date', '').strip()
    planned = _parse_date(planned_raw) if planned_raw else None
    note = ' '.join(request.form.get('note', '').split())[:300]
    if not keys:
        flash(tr('请先勾选阀门', 'Select at least one valve line'), 'error')
    elif not job_number:
        flash(tr('请填写 V-Trust job number', 'Enter the V-Trust job number'), 'error')
    elif planned_raw and not planned:
        flash(tr('预约检验日格式不对', 'The planned inspection date is not a valid date'), 'error')
    else:
        est = {l['job_key']: l['est_text'] for l in vtrust_lines()}
        with db_conn() as conn:
            for key in keys:
                conn.execute(
                    'INSERT INTO vtrust_bookings (job_key, job_number, planned_date, est_at_booking, note, booked_by, '
                    "booked_at, reschedule_alert) VALUES (?,?,?,?,?,?,datetime('now'),'') ON CONFLICT(job_key) DO UPDATE SET "
                    'job_number=excluded.job_number, planned_date=excluded.planned_date, '
                    'est_at_booking=excluded.est_at_booking, note=excluded.note, booked_by=excluded.booked_by, '
                    "booked_at=excluded.booked_at, reschedule_alert=''",
                    (key, job_number, planned.isoformat() if planned else '', est.get(key, ''), note,
                     g.get('username', '')))
        flash(tr(f'已为 {len(keys)} 行录入 V-Trust job {job_number}', f'V-Trust job {job_number} saved for {len(keys)} line(s)'),
              'success')
    return redirect(url_for('vtrust_page', show=request.form.get('show', 'open')))


@app.route('/vtrust/unbook', methods=['POST'])
def vtrust_unbook():
    keys = request.form.getlist('job_key')
    with db_conn() as conn:
        for key in keys:
            conn.execute('DELETE FROM vtrust_bookings WHERE job_key=?', (key,))
    flash(tr(f'已删除 {len(keys)} 行的 V-Trust 预约', f'V-Trust booking removed from {len(keys)} line(s)'), 'success')
    return redirect(url_for('vtrust_page', show=request.form.get('show', 'open')))


@app.route('/settings/vtrust-preview')
def settings_vtrust_preview():
    """The V-Trust reminder as it would look today (nothing is sent)."""
    lead = int(load_config().get('vtrust_lead_days') or VTRUST_LEAD_DAYS)
    all_lines = vtrust_lines(lead)
    lines = [l for l in all_lines if l['status'] == 'to_book']
    booked = _vtrust_booked_in_window(all_lines, lead)
    if request.args.get('format') == 'xlsx':
        return send_file(io.BytesIO(vtrust_email(lines, lead, booked)[3]), as_attachment=True,
                         download_name=f'V-Trust_{china_today().isoformat()}.xlsx')
    with db_conn() as conn:
        sent = {(r['job_key'], r['est_date']) for r in conn.execute('SELECT job_key, est_date FROM vtrust_reminders')}
    pending = sum(1 for l in lines if (l['job_key'], l['est'].isoformat()) not in sent)
    note = (f'<div style="font-family:Arial,sans-serif;font-size:13px;background:#fffbeb;border:1px solid #fcd34d;'
            f'padding:8px 12px;margin-bottom:14px;">预览，未发送 / Preview only — nothing was sent. '
            f'收件人 Recipients: {_escape(", ".join(vtrust_recipients()) or "—")} · '
            f'共 {len(lines)} 行，其中 {pending} 行尚未提醒过 / {len(lines)} line(s), {pending} not reminded yet · '
            f'<a href="?format=xlsx">Excel</a> · <a href="{url_for("settings")}#email">返回设置 / Back</a></div>')
    if not lines:
        return note + f'<p style="font-family:Arial,sans-serif;">{tr("目前没有需要提醒的阀门。", "No valves to remind about right now.")}</p>'
    subject, _text, html, _xlsx = vtrust_email(lines, lead, booked)
    return note + f'<p style="font-family:Arial,sans-serif;font-size:13px;"><b>{_escape(subject)}</b></p>' + html


@app.route('/settings/vtrust-send', methods=['POST'])
def settings_vtrust_send():
    count, msg = send_vtrust_reminders(force=True)
    flash(msg, 'success' if count else 'error')
    return redirect(url_for('settings') + '#email')


# ── QA BRT checks after each weekly upload ───────────────────────────────────
# Compare the Excel "QA BRTs Sent?" column with the reports in the platform:
#  · Excel YES but no report (orange badge on the schedule page) and
#  · a report exists but the Excel is not YES      → supplier + lead inspector
#  · lines that shipped with this upload, no report → HQ + lead + supplier

QA_STATE_LABELS = {'new': 'New 新增', 'typo': 'New 新增', 'not_shipped': 'Not shipped 未出货',
                   'partially_shipped': 'Partially shipped 部分出货', 'moved_in': 'Moved in 转入',
                   'partially_moved': 'Part moved 部分转出'}


def _qa_cell(row, headers):
    """The row's 'QA BRTs Sent?' text, or None when the sheet has no such column."""
    idx = next((i for i, h in enumerate(headers) if 'qa brt' in str(h).lower()), None)
    if idx is None:
        return None
    return str(row[idx]).strip() if idx < len(row) and row[idx] is not None else ''


def _qa_line(sheet, row, headers, key, **extra):
    low = [str(h).strip().lower() for h in headers]
    supplier = next((str(row[low.index(n)]).strip() for n in ('supplier', 'foundry')
                     if n in low and low.index(n) < len(row) and row[low.index(n)] is not None), '')
    cell = _qa_cell(row, headers)
    return dict(_row_details(sheet, row, headers), key=key, supplier=supplier,
                excel_qa='' if cell is None else (cell or '空 / blank'), **extra)


def _report_text(reports):
    last = reports[-1]
    return ' '.join(x for x in (last.get('result', ''), (last.get('inspection_date') or '')[:10]) if x)


def qa_brt_check():
    """{'yes_no_report', 'report_not_yes', 'shipped_no_report'}: lists of lines
    for the current schedule compared with the previous one."""
    current, previous = load_schedule(CURRENT_FILE), load_schedule(PREVIOUS_FILE)
    inspections = SharedReports(load_json(INSPECTIONS_CACHE, {}))
    statuses, _, shipped_rows = compute_changes(previous, current) if current and previous else ({}, [], {})
    old_qty = {k: l['qty'] for k, l in _schedule_lines(previous).items()}
    new_qty = {k: l['qty'] for k, l in _schedule_lines(current).items()}
    result = {'yes_no_report': [], 'report_not_yes': [], 'shipped_no_report': []}
    seen = set()
    for sheet, rows in current.items():
        if not rows:
            continue
        headers = rows[0]
        for row in rows[1:]:
            key = make_job_key(sheet, row, headers)
            if not key.split('|')[1] or key in seen:
                continue
            seen.add(key)
            reports = inspections.get(key, [])
            status = statuses.get(key)
            line = _qa_line(sheet, row, headers, key, state=QA_STATE_LABELS.get(status, 'In schedule 在排期中'))
            if status == 'partially_shipped' and not reports:
                b, a = old_qty.get(key), new_qty.get(key)
                result['shipped_no_report'].append(dict(line, shipment='Partially shipped 部分出货',
                                                        shipped_qty=_qty_text(b - a) if b is not None and a is not None else ''))
                continue                       # reported once, under "shipped without a report"
            cell = _qa_cell(row, headers)
            if cell is None:
                continue
            yes = cell.lower() in {'yes', 'y'}
            if yes and not reports:
                result['yes_no_report'].append(line)
            elif reports and not yes:
                result['report_not_yes'].append(dict(line, report=_report_text(reports)))
    for sheet, rows in shipped_rows.items():
        headers = (current.get(sheet) or previous.get(sheet) or [[]])[0]
        for row in rows:
            key = make_job_key(sheet, row, headers)
            if not key.split('|')[1] or inspections.get(key):
                continue
            q = old_qty.get(key)
            result['shipped_no_report'].append(_qa_line(sheet, row, headers, key, shipment='Fully shipped 全部出货',
                                                        shipped_qty=_qty_text(q) if q is not None else ''))
    order = lambda l: (l['sheet'], l['order_number'], l['item_code'])
    return {k: sorted(v, key=order) for k, v in result.items()}


def qa_brt_mismatches():
    """Lines still in the schedule whose Excel 'QA BRTs Sent?' is YES while no report exists."""
    return qa_brt_check()['yes_no_report']


def _lead_emails():
    with db_conn() as conn:
        return [r['email'] for r in conn.execute(
            "SELECT email FROM users WHERE role='lead' AND active=1 AND IFNULL(email, '') != ''")]


def qa_mismatch_recipients():
    """Supplier ('QA BRT e-mails (supplier)' in Settings) + every active lead inspector with an e-mail."""
    return _email_list(', '.join(_email_list(load_config().get('qa_mismatch_emails', '')) + _lead_emails()))


def shipped_no_report_recipients():
    """HQ ('HQ report e-mails') + lead inspectors + the supplier."""
    config = load_config()
    return _email_list(', '.join(_email_list(config.get('hq_report_emails', '')) + _lead_emails()
                                 + _email_list(config.get('qa_mismatch_emails', ''))))


_QA_BASE_COLUMNS = [('Region 区域', 'sheet', 'c'), ('DPL', 'order_number', 'c'), ('PO', 'po', 'c'),
                    ('Supplier 供应商', 'supplier', 'c'), ('Item Code 编码', 'item_code', 'l'),
                    ('Description 描述', 'description', 'l')]


def _qa_mail(title, subject, intro, sections):
    """(subject, plain text, html, xlsx) with one table / worksheet per section.
    sections: [(heading, sheet name, lines, columns, link label)]; columns: [(header, field, align)]."""
    today = china_today().isoformat()
    text = [f'{title} — {today}', *intro, '']
    cell = 'border:1px solid #d0d5dd;padding:6px 10px;font-size:13px;'
    head = cell + 'background:#1a3a5c;color:#fff;font-weight:700;text-align:center;'
    font = "Arial,'Microsoft YaHei',sans-serif"
    html = [f'<div style="font-family:{font};color:#1a1a2e;">',
            f'<p style="font-size:14px;margin:0 0 6px;"><b>{_escape(title)} — {today}</b></p>']
    html += [f'<p style="font-size:13px;margin:0 0 3px;">{_escape(t)}</p>' for t in intro]
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for heading, sheet_name, lines, columns, link_label in sections:
        if not lines:
            continue
        text += [f'■ {heading} ({len(lines)})']
        html.append(f'<p style="font-size:14px;margin:16px 0 6px;"><b>{_escape(heading)} ({len(lines)})</b></p>')
        html.append('<table style="border-collapse:collapse;"><tr>'
                    + ''.join(f'<th style="{head}">{_escape(c[0])}</th>' for c in columns)
                    + f'<th style="{head}">{_escape(link_label)}</th></tr>')
        ws = wb.create_sheet(sheet_name)
        ws.append([c[0] for c in columns])
        for i, l in enumerate(lines, 1):
            link = url_for('inspect_form', job_key=l['key'], _external=True)
            text.append(f'{i}. ' + '  '.join(str(l.get(c[1], '')) for c in columns if l.get(c[1])))
            text.append('    ' + link)
            html.append('<tr>' + ''.join(
                f'<td style="{cell}{"text-align:center;" if c[2] == "c" else ""}">{_escape(l.get(c[1], ""))}</td>'
                for c in columns) + f'<td style="{cell}text-align:center;"><a href="{_escape(link)}">'
                f'{_escape(link_label)}</a></td></tr>')
            ws.append([_xl_safe(l.get(c[1], '')) for c in columns])
        html.append('</table>')
        text.append('')
        for c in ws[1]:
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = PatternFill('solid', fgColor='1A3A5C')
            c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        for i, c in enumerate(columns, 1):
            ws.column_dimensions[get_column_letter(i)].width = 44 if c[1] == 'description' else 16
        ws.freeze_panes = 'A2'
    html.append('<p style="font-size:12px;color:#6b7280;margin-top:12px;">附件为同样内容的 Excel。 '
                'The attached Excel has the same list.</p></div>')
    if not wb.sheetnames:                      # nothing to list: a workbook needs one sheet
        wb.create_sheet('List').append(['No lines 没有需要提醒的行'])
    buf = io.BytesIO()
    wb.save(buf)
    return subject, '\n'.join(text), ''.join(html), buf.getvalue()


def qa_check_email(check):
    a, b = check['yes_no_report'], check['report_not_yes']
    subject = (f'【QA BRT 核对】Excel 与系统不一致 {len(a) + len(b)} 行 / '
               f'QA BRT check: {len(a) + len(b)} line(s) where the Excel and the platform disagree')
    intro = ['排期 Excel 的 "QA BRTs Sent?" 一列与质检平台的检验报告不一致，请核对：',
             '① Excel 标 YES 但平台没有报告：已检验的请检验员在平台提交报告；没检验的请把 Excel 改为 NO。',
             '② 平台已有报告但 Excel 不是 YES：请把 Excel 这一行改为 YES。',
             'The "QA BRTs Sent?" column of the schedule Excel and the reports in the QC platform disagree: '
             '(1) Excel YES but no report: submit the report, or change the Excel to NO; '
             '(2) a report exists but the Excel is not YES: please change the Excel to YES.']
    status = ('Status 状态', 'state', 'c')
    qty = ('QTY', 'quantity', 'c')
    return _qa_mail('QA BRT 核对 / QA BRT check', subject, intro, [
        ('① Excel 标 YES，平台无报告 / Excel YES, no report', 'Excel YES no report', a, _QA_BASE_COLUMNS + [qty, status], '提交 / Submit'),
        ('② 平台有报告，Excel 未标 YES / Report exists, Excel not YES', 'Report but Excel not YES', b,
         _QA_BASE_COLUMNS + [qty, status, ('Excel', 'excel_qa', 'c'), ('Report 报告', 'report', 'c')], '查看 / View')])


def shipped_no_report_email(lines):
    subject = (f'【出货缺 QA BRT】{len(lines)} 行已出货但系统无检验报告 / '
               f'{len(lines)} shipped line(s) without a QA BRT report')
    intro = ['以下订单行在本次上传的排期中已出货（全部或部分），但质检平台里没有检验报告：',
             '请 Murphy 确认是否检验过并补交报告；请供应商说明出货前是否完成检验。',
             'These lines shipped (fully or partly) according to this week\'s schedule, but the QC platform has '
             'no inspection report for them. Lead inspector: confirm and submit the report; supplier: confirm '
             'whether they were inspected before shipping.']
    return _qa_mail('出货缺 QA BRT / Shipped without a QA BRT report', subject, intro, [
        ('Shipped 出货', 'Shipped without report', lines, [('Shipment 出货', 'shipment', 'c')] + _QA_BASE_COLUMNS
         + [('Shipped QTY 出货数量', 'shipped_qty', 'c'), ('Excel QA BRTs Sent?', 'excel_qa', 'c')], '补交 / Submit')])


def send_qa_mismatch_email():
    """QA BRT check e-mail to the supplier + lead. Returns (count, message)."""
    check = qa_brt_check()
    count = len(check['yes_no_report']) + len(check['report_not_yes'])
    if not count:
        return 0, tr('Excel 与系统的 QA BRT 状态一致', 'The Excel and the platform agree on QA BRTs')
    recipients = qa_mismatch_recipients()
    if not recipients:
        return 0, tr('未设置供应商 QA BRT 邮箱，检验主管账号也没有邮箱', 'No supplier QA BRT e-mail and no lead inspector e-mail')
    subject, text, html, xlsx = qa_check_email(check)
    ok, msg = _smtp_send(subject, text, recipients, html=html, attachments=[
        (f'QA BRT check {china_today().isoformat()}.xlsx', xlsx,
         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')])
    return (count if ok else 0), msg


def send_shipped_no_report_email():
    """Shipped-without-report e-mail to HQ + lead + supplier. Returns (count, message)."""
    lines = qa_brt_check()['shipped_no_report']
    if not lines:
        return 0, tr('本次出货的行都有检验报告', 'Every shipped line has a report')
    recipients = shipped_no_report_recipients()
    if not recipients:
        return 0, tr('未设置总部 / 供应商邮箱，检验主管账号也没有邮箱', 'No HQ / supplier / lead e-mail')
    subject, text, html, xlsx = shipped_no_report_email(lines)
    ok, msg = _smtp_send(subject, text, recipients, html=html, attachments=[
        (f'Shipped without QA BRT {china_today().isoformat()}.xlsx', xlsx,
         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')])
    return (len(lines) if ok else 0), msg


AFTER_UPLOAD_EMAILS = (('Purchasing', 'send_purchasing_email'), ('QA BRT check', 'send_qa_mismatch_email'),
                       ('Shipped without report', 'send_shipped_no_report_email'))


def _after_upload_emails_in_background(host_url):
    """Purchasing comparison + QA BRT e-mails after a weekly upload."""
    def work():
        with app.test_request_context(base_url=host_url):
            for name, job in AFTER_UPLOAD_EMAILS:
                try:
                    result = globals()[job]()
                    if not result[0]:
                        logger.info('%s e-mail not sent: %s', name, result[1])
                except Exception:
                    logger.exception('%s e-mail failed', name)
    import threading
    threading.Thread(target=work, daemon=True).start()


def _qa_preview(kind):
    """(subject, html, xlsx, count, recipients) for 'check' or 'shipped'."""
    check = qa_brt_check()
    if kind == 'shipped':
        lines = check['shipped_no_report']
        subject, _t, html, xlsx = shipped_no_report_email(lines)
        return subject, html, xlsx, len(lines), shipped_no_report_recipients()
    count = len(check['yes_no_report']) + len(check['report_not_yes'])
    subject, _t, html, xlsx = qa_check_email(check)
    return subject, html, xlsx, count, qa_mismatch_recipients()


@app.route('/settings/qa-mismatch-preview')
def settings_qa_mismatch_preview():
    """A QA BRT e-mail as it would look now (nothing is sent). ?kind=check|shipped"""
    kind = 'shipped' if request.args.get('kind') == 'shipped' else 'check'
    subject, html, xlsx, count, recipients = _qa_preview(kind)
    if request.args.get('format') == 'xlsx':
        return send_file(io.BytesIO(xlsx), as_attachment=True,
                         download_name=f'QA BRT {kind} {china_today().isoformat()}.xlsx')
    note = (f'<div style="font-family:Arial,sans-serif;font-size:13px;background:#fffbeb;border:1px solid #fcd34d;'
            f'padding:8px 12px;margin-bottom:14px;">预览，未发送 / Preview only — nothing was sent. '
            f'收件人 Recipients: {_escape(", ".join(recipients) or "—")} · '
            f'共 {count} 行 / {count} line(s) · <a href="?kind={kind}&format=xlsx">Excel</a> · '
            f'<a href="{url_for("settings")}#email">返回设置 / Back</a></div>')
    if not count:
        return note + f'<p style="font-family:Arial,sans-serif;">{tr("目前没有需要提醒的行。", "Nothing to report right now.")}</p>'
    return note + f'<p style="font-family:Arial,sans-serif;font-size:13px;"><b>{_escape(subject)}</b></p>' + html


@app.route('/settings/qa-mismatch-send', methods=['POST'])
def settings_qa_mismatch_send():
    send = send_shipped_no_report_email if request.form.get('kind') == 'shipped' else send_qa_mismatch_email
    count, msg = send()
    flash(msg, 'success' if count else 'error')
    return redirect(url_for('settings') + '#email')


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
        f"分配人 Assigned by: {by}    时间 Time: {dual_zone_time()}",
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
    today = china_today()
    reconcile_tasks_with_inspections()
    every_task, all_tasks, rows, scope, status_filter, orphan_only = task_listing(
        {k: v for k, v in request.args.items() if k != 'supplier'})
    with db_conn() as conn:
        assignees = assignable_users(conn)

    scope_counts = {
        'mine': sum(1 for t in every_task if t['assigned_to'] == g.user_id
                    and t['status'] not in DONE_STATUSES),
        'unassigned': sum(1 for t in every_task if not t['assigned_to']
                          and t['status'] not in DONE_STATUSES),
        'all': len(every_task),
    }
    inspections = load_json(INSPECTIONS_CACHE, {})
    orphan_count = sum(1 for t in all_tasks if t['orphan'])

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

    review_queue = pending_reviews() if g.can_review else []
    return render_template('tasks.html', review_queue=review_queue, tasks=rows, inspections=inspections,
                           today=today, status_filter=status_filter, stats=stats,
                           orphan_only=orphan_only, orphan_count=orphan_count,
                           scope=scope, scope_counts=scope_counts, assignees=assignees)



def task_listing(args):
    """Tasks for the task page and its Excel export, using the page's filters
    (scope, status, orphan; supplier for the export).
    Returns (every_task, scope_tasks, rows, scope, status_filter, orphan_only)."""
    status_filter = args.get('status', '')
    # Inspectors land on their own tasks; the lead / admin on everything.
    scope = args.get('scope') or ('all' if g.can_assign or g.is_hq else 'mine')
    if scope not in ('mine', 'unassigned', 'all'):
        scope = 'all'
    with db_conn() as conn:
        every_task = [dict(r) for r in conn.execute(
            "SELECT t.*, COALESCE(NULLIF(u.display_name, ''), u.username) AS assignee_name "
            'FROM inspection_tasks t LEFT JOIN users u ON u.id = t.assigned_to '
            'ORDER BY t.est_completion ASC, t.created_at ASC'
        ).fetchall()]
    # Open tasks whose order is no longer in the current schedule (shipped or removed)
    in_schedule = set()
    for sheet, sheet_rows in load_schedule(CURRENT_FILE).items():
        for r in (sheet_rows or [])[1:]:
            in_schedule.add(make_job_key(sheet, r, sheet_rows[0]))
    for t in every_task:
        t['orphan'] = t['status'] not in DONE_STATUSES and t['job_key'] not in in_schedule
    if scope == 'mine':
        scope_tasks = [t for t in every_task if t['assigned_to'] == g.user_id]
    elif scope == 'unassigned':
        scope_tasks = [t for t in every_task if not t['assigned_to']]
    else:
        scope_tasks = every_task
    orphan_only = args.get('orphan') == '1'
    if orphan_only:
        rows = [t for t in scope_tasks if t['orphan']]
    elif status_filter:
        rows = [t for t in scope_tasks if t['status'] == status_filter]
    else:
        rows = scope_tasks
    supplier = args.get('supplier')
    if supplier is not None and supplier != '*':
        wanted = '' if supplier == '__none__' else supplier
        rows = [t for t in rows if (t['supplier'] or '').strip() == wanted]
    return every_task, scope_tasks, rows, scope, status_filter, orphan_only


@app.route('/tasks/export.xlsx')
def tasks_export():
    reconcile_tasks_with_inspections()
    _every, _scope_tasks, rows, scope, _status, _orphan = task_listing(request.args)
    inspections = load_json(INSPECTIONS_CACHE, {})
    today = china_today()
    columns = [
        ('区域 Region', 12), ('供应商 Supplier', 14), ('订单号 Order', 12), ('采购单号 PO', 12),
        ('产品编码 Item code', 18), ('描述 Description', 36), ('数量 Qty', 8),
        ('预计完工 Est. completion', 16), ('最迟出货 Must ship', 14), ('距今天数 Days left', 10),
        ('检验结果 Result', 12), ('报告编号 Report No.', 30), ('负责人 Assigned to', 14),
        ('分配时间 Assigned (UTC)', 17), ('分配备注 Note', 24), ('任务状态 Status', 12),
        ('备注 Remarks', 22),
    ]
    header_fill = openpyxl.styles.PatternFill('solid', fgColor='1A3A5C')
    header_font = openpyxl.styles.Font(color='FFFFFF', bold=True)
    fills = {'overdue': 'FEE2E2', 'week': 'FFEDD5', 'Pass': 'D1FAE5', 'Fail': 'FEE2E2',
             'Partial Pass': 'FEF3C7'}
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = 'Tasks'
    ws.append([label for label, _w in columns])
    for (_label, width), cell in zip(columns, ws[1]):
        cell.fill, cell.font = header_fill, header_font
        cell.alignment = openpyxl.styles.Alignment(wrap_text=True, vertical='top')
        ws.column_dimensions[cell.column_letter].width = width
    for t in rows:
        records = inspections.get(t['job_key']) or []
        last = records[-1] if records else {}
        done = t['status'] in DONE_STATUSES
        days = None if done else days_until(t['est_completion'], today)
        remarks = []
        if t['orphan']:
            remarks.append('订单已不在排期 / not in current schedule')
        ws.append([_xl_safe(v) for v in (
            t['region'], (t['supplier'] or '').strip(), t['order_number'],
            t['job_key'].split('|')[1] if t['job_key'].count('|') >= 2 else '',
            t['item_code'], t['description'], t['quantity'], t['est_completion'], t['must_ship'],
            days, last.get('result', ''),
            report_number(t['job_key'], last, len(records) - 1) if records else '',
            t['assignee_name'] or '', t['assigned_at'] or '', t['assign_note'] or '',
            status_label(t['status'] or 'Pending'), '; '.join(remarks))])
        line = ws.max_row
        urgency = 'overdue' if days is not None and days <= 0 else 'week' if days is not None and days <= 7 else ''
        if urgency:
            ws.cell(row=line, column=10).fill = openpyxl.styles.PatternFill('solid', fgColor=fills[urgency])
        if last.get('result') in fills:
            ws.cell(row=line, column=11).fill = openpyxl.styles.PatternFill('solid', fgColor=fills[last['result']])
    ws.freeze_panes = 'D2'
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    workbook.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f'inspection-tasks-{scope}-{today.isoformat()}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ── Monday summary for the lead inspector ────────────────────────────────────

def send_weekly_summary(force=False):
    """Monday (China date) e-mail to the task notification addresses: overdue
    tasks, tasks due in the next 14 days by inspector, unassigned tasks and
    reports waiting for review. Returns (ok, message)."""
    config = load_config()
    if not force and not config.get('weekly_summary', True):
        return False, tr('每周汇总已关闭', 'Weekly summary is off')
    recipients = _email_list(config.get('task_notify_emails', ''))
    if not recipients:
        return False, tr('未设置任务通知邮箱', 'No task notification e-mail configured')
    today = china_today()
    week = today.isoformat()
    if not force and (today.weekday() != 0 or config.get('weekly_summary_last') == week):
        return False, tr('今天不发送', 'Not due today')
    reconcile_tasks_with_inspections()
    with db_conn() as conn:
        tasks = [dict(r) for r in conn.execute(
            "SELECT t.*, COALESCE(NULLIF(u.display_name, ''), u.username) AS assignee_name "
            'FROM inspection_tasks t LEFT JOIN users u ON u.id = t.assigned_to '
            "WHERE IFNULL(t.status, '') NOT IN ('Completed', 'Closed')")]
    for t in tasks:
        t['days'] = days_until(t['est_completion'], today)
    overdue = sorted((t for t in tasks if t['days'] is not None and t['days'] < 0), key=lambda t: t['days'])
    soon = [t for t in tasks if t['days'] is not None and 0 <= t['days'] <= 14]
    unassigned = [t for t in tasks if not t['assigned_to']]
    reviews = pending_reviews()

    def line(t):
        when = (tr(f'逾期 {-t["days"]} 天', f'{-t["days"]} days overdue') if t['days'] < 0
                else tr('今天到期', 'due today') if t['days'] == 0
                else tr(f'还剩 {t["days"]} 天', f'{t["days"]} days left')) if t['days'] is not None else '—'
        return (f"  [{t['region']}] {t['order_number']} {t['item_code']} · {when} · "
                f"{t['assignee_name'] or '未分配 unassigned'}")

    lines = [f'每周检验任务汇总 Weekly inspection summary — {week}', '',
             f'逾期 Overdue: {len(overdue)}    14 天内到期 Due in 14 days: {len(soon)}    '
             f'未分配 Unassigned: {len(unassigned)}    待审核报告 Reports to review: {len(reviews)}', '']
    if overdue:
        lines.append(f'■ 逾期任务 Overdue ({len(overdue)})')
        lines += [line(t) for t in overdue[:30]]
        if len(overdue) > 30:
            lines.append(f'  … +{len(overdue) - 30}')
        lines.append('')
    if soon:
        lines.append(f'■ 14 天内到期（按负责人）Due in the next 14 days, by inspector ({len(soon)})')
        by_person = defaultdict(list)
        for t in sorted(soon, key=lambda t: t['days']):
            by_person[t['assignee_name'] or '未分配 Unassigned'].append(t)
        for person, items in sorted(by_person.items()):
            lines.append(f'  {person} ({len(items)})')
            lines += ['  ' + line(t) for t in items[:20]]
        lines.append('')
    if unassigned:
        lines.append(f'■ 未分配任务 Unassigned ({len(unassigned)})')
        lines += [line(t) for t in sorted(unassigned, key=lambda t: (t['days'] is None, t['days'] or 0))[:20]]
        lines.append('    ' + url_for('tasks', scope='unassigned', _external=True))
        lines.append('')
    if reviews:
        lines.append(f'■ 待审核报告 Reports waiting for review ({len(reviews)})')
        for q in reviews[:15]:
            rec = q['record']
            lines.append(f"  {rec.get('order_number', '')} {rec.get('item_code', '')} · {rec.get('result', '')} · "
                         f"{rec.get('inspector_name', '')} · {int(q['hours'])} h")
        lines.append('')
    lines.append(url_for('tasks', _external=True))
    ok, msg = _smtp_send(f'【每周汇总】检验任务 {week} / Weekly inspection summary', '\n'.join(lines), recipients)
    if ok and not force:
        config = load_config()
        config['weekly_summary_last'] = week
        save_json(CONFIG_FILE, config)
    return ok, msg


@app.route('/settings/weekly-summary', methods=['POST'])
def settings_weekly_summary():
    ok, msg = send_weekly_summary(force=True)
    flash(msg, 'success' if ok else 'error')
    return redirect(url_for('settings') + '#email')


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


def _assignable():
    with db_conn() as conn:
        return assignable_users(conn)


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
        task = conn.execute('SELECT assigned_to, status FROM inspection_tasks WHERE id=?',
                            (tid,)).fetchone()
        if not task:
            abort(404)
        if not g.can_assign:
            # Inspectors may only update tasks assigned to them, and closing
            # a task (or reopening a closed one) is the lead's / admin's call.
            if task['assigned_to'] != g.user_id:
                abort(403)
            if new_status not in INSPECTOR_TASK_STATUSES or task['status'] == 'Closed':
                abort(403)
        conn.execute('UPDATE inspection_tasks SET status=? WHERE id=?', (new_status, tid))
    return redirect(_safe_next_url(request.form.get('next')) or url_for('tasks'))


TASK_STATUSES = ('Pending', 'In Progress', 'Completed', 'On Hold', 'Closed')
# Statuses an inspector can pick on their own tasks ('Closed' is lead/admin only).
INSPECTOR_TASK_STATUSES = ('Pending', 'In Progress', 'Completed', 'On Hold')
app.jinja_env.globals['TASK_STATUSES'] = TASK_STATUSES
app.jinja_env.globals['my_open_job_keys'] = my_open_job_keys
app.jinja_env.globals['INSPECTOR_TASK_STATUSES'] = INSPECTOR_TASK_STATUSES
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



# ── Checklist on the inspection page: server drafts and per-question photos ──
# Inspectors answer the checklist and take photos on site (iPad), then finish
# at a PC: answers, form fields and photos live in a server draft per job and
# user until the report is submitted.

CHECKLIST_FILE_EXTENSIONS = IMAGE_EXTENSIONS | {'.heic', '.mp4', '.mov', '.avi', '.mkv', '.pdf', '.xlsx', '.xls', '.csv'}
# Required-evidence files (BRT, test videos, DAQ …) are uploaded one by one into
# the draft as soon as they are picked, under ref "ev:<type>", so a report
# started on an iPad can be finished on a PC.
EV_REF_PREFIX = 'ev:'
# One draft file per request, so videos may be larger than the overall limit.
DRAFT_FILE_MAX_BYTES = int(os.environ.get('DRAFT_FILE_MAX_BYTES', 300 * 1024 * 1024))

CHECKLIST_REASONS = {
    'product_photo': ('缺少产品照片（能看清编号 / 批号）', 'Product photo missing (serial / batch number visible)'),
    'unanswered': ('未回答', 'Not answered'),
    'na_not_allowed': ('此项不能选“不适用”', 'N/A is not allowed for this question'),
    'photo': ('需要照片', 'Photo required'),
}


def job_checklist(item_code, description, template_id=None, version=None):
    """The checklist for a job: the draft's template/version when given, else
    the active template for the product type. dict or None."""
    if not template_id:
        tpl = checklist_for_item(item_code, description)
        if not tpl:
            return None
        template_id, version = tpl['id'], None
    tpl, ver, data = checklist_version(template_id, version)
    if not data:
        return None
    return {'template_id': tpl['id'], 'name': tpl['name'], 'version': ver['version'], 'data': data}


def _draft(conn, job_key, user_id=None):
    return conn.execute('SELECT * FROM inspection_drafts WHERE job_key=? AND user_id=?',
                        (job_key, user_id or g.user_id)).fetchone()


def _draft_files(conn, draft_id):
    return conn.execute('SELECT * FROM draft_files WHERE draft_id=? ORDER BY id', (draft_id,)).fetchall()


def _draft_file_json(job_key, row):
    return {'id': row['id'], 'name': row['original_name'], 'ref': row['ref'],
            'video': _upload_extension(row['original_name']) in {'.mp4', '.mov', '.avi', '.mkv'},
            'doc': _upload_extension(row['original_name']) in {'.pdf', '.xlsx', '.xls', '.csv'},
            'url': url_for('draft_file', job_key=job_key, fid=row['id'])}


def load_inspection_draft(job_key):
    """{'fields', 'answers', 'files': {ref: [...]}, 'template_id', 'version', 'saved_at'} or None."""
    with db_conn() as conn:
        draft = _draft(conn, job_key)
        if not draft:
            return None
        files = _draft_files(conn, draft['id'])
    data = json.loads(draft['data_json'] or '{}')
    grouped = defaultdict(list)
    for row in files:
        grouped[row['ref']].append(_draft_file_json(job_key, row))
    return {'fields': data.get('fields') or {}, 'answers': data.get('answers') or {}, 'files': dict(grouped),
            'template_id': draft['template_id'], 'version': draft['version'], 'saved_at': draft['updated_at']}


def _ensure_draft(conn, job_key, template_id, version):
    draft = _draft(conn, job_key)
    if draft:
        return draft
    conn.execute('INSERT INTO inspection_drafts (job_key, user_id, template_id, version, data_json) '
                 'VALUES (?,?,?,?,?)', (job_key, g.user_id, template_id, version, '{}'))
    return _draft(conn, job_key)


@app.route('/inspect/<path:job_key>/draft', methods=['POST'])
def inspection_draft_save(job_key):
    allowed, message = inspect_permission(job_key)
    if not allowed:
        return jsonify(ok=False, message=message), 403
    payload = request.get_json(silent=True) or {}
    answers = payload.get('answers') if isinstance(payload.get('answers'), dict) else {}
    fields = payload.get('fields') if isinstance(payload.get('fields'), dict) else {}
    data = json.dumps({'answers': answers, 'fields': fields}, ensure_ascii=False)
    if len(data) > 500_000:
        return jsonify(ok=False, message='Draft too large'), 413
    with db_conn() as conn:
        draft = _ensure_draft(conn, job_key, payload.get('template_id'), payload.get('version'))
        conn.execute("UPDATE inspection_drafts SET data_json=?, updated_at=datetime('now') WHERE id=?",
                     (data, draft['id']))
        saved = conn.execute('SELECT updated_at FROM inspection_drafts WHERE id=?', (draft['id'],)).fetchone()[0]
    return jsonify(ok=True, saved_at=utc_iso(saved))


@app.route('/inspect/<path:job_key>/draft/files', methods=['POST'])
def inspection_draft_upload(job_key):
    allowed, message = inspect_permission(job_key)
    if not allowed:
        return jsonify(ok=False, message=message), 403
    upload = request.files.get('file')
    ref = (request.form.get('ref') or '').strip()[:40]
    if not upload or not upload.filename or not ref:
        return jsonify(ok=False, message=tr('没有文件', 'No file')), 400
    allowed = CHECKLIST_FILE_EXTENSIONS | (EVIDENCE_EXTENSIONS if ref.startswith(EV_REF_PREFIX) else set())
    if _upload_extension(upload.filename) not in allowed:
        return jsonify(ok=False, message=tr('只能上传照片、视频、PDF 或 Excel', 'Photos, videos, PDF or Excel only')), 400
    folder = os.path.join(UPLOAD_DIR, hashlib.sha256(job_key.encode('utf-8')).hexdigest(), 'drafts')
    os.makedirs(folder, exist_ok=True)
    original, saved = _save_uploaded_file(upload, folder, allowed)
    with db_conn() as conn:
        draft = _ensure_draft(conn, job_key, request.form.get('template_id', type=int),
                              request.form.get('version', type=int))
        fid = conn.execute('INSERT INTO draft_files (draft_id, ref, original_name, saved_name, file_path) '
                           'VALUES (?,?,?,?,?)', (draft['id'], ref, original, saved,
                                                  os.path.join(folder, saved))).lastrowid
        conn.execute("UPDATE inspection_drafts SET updated_at=datetime('now') WHERE id=?", (draft['id'],))
        row = conn.execute('SELECT * FROM draft_files WHERE id=?', (fid,)).fetchone()
    return jsonify(ok=True, file=_draft_file_json(job_key, row))


def _owned_draft_file(conn, job_key, fid):
    return conn.execute('SELECT f.*, d.user_id FROM draft_files f JOIN inspection_drafts d ON d.id=f.draft_id '
                        'WHERE f.id=? AND d.job_key=?', (fid, job_key)).fetchone()


@app.route('/inspect/<path:job_key>/draft/files/<int:fid>')
def draft_file(job_key, fid):
    with db_conn() as conn:
        row = _owned_draft_file(conn, job_key, fid)
    if not row or (row['user_id'] != g.user_id and not g.can_review) or not os.path.exists(row['file_path']):
        abort(404)
    return send_from_directory(os.path.dirname(row['file_path']), os.path.basename(row['file_path']),
                               download_name=row['original_name'])


@app.route('/inspect/<path:job_key>/draft/files/<int:fid>/delete', methods=['POST'])
def draft_file_delete(job_key, fid):
    with db_conn() as conn:
        row = _owned_draft_file(conn, job_key, fid)
        if not row or row['user_id'] != g.user_id:
            return jsonify(ok=False), 404
        conn.execute('DELETE FROM draft_files WHERE id=?', (fid,))
    _remove_quietly(row['file_path'])
    return jsonify(ok=True)


@app.route('/inspect/<path:job_key>/draft/discard', methods=['POST'])
def inspection_draft_discard(job_key):
    with db_conn() as conn:
        draft = _draft(conn, job_key)
        if draft:
            for row in _draft_files(conn, draft['id']):
                _remove_quietly(row['file_path'])
            conn.execute('DELETE FROM draft_files WHERE draft_id=?', (draft['id'],))
            conn.execute('DELETE FROM inspection_drafts WHERE id=?', (draft['id'],))
    flash(tr('草稿已清除', 'Draft discarded'), 'info')
    return redirect(url_for('inspect_form', job_key=job_key))


def checklist_problem_messages(checklist, problems, limit=6):
    """Readable lines for the first few blocking problems."""
    names = {}
    for s in checklist['data']['sections']:
        for n, q in enumerate(s['questions'], 1):
            names[q['id']] = f"{s.get('name_zh') or s['name'] if current_lang() == 'zh' else s['name']} {n}"
    lines = []
    for ref, reason in problems[:limit]:
        zh, en = CHECKLIST_REASONS[reason]
        lines.append(tr(zh, en) if ref == 'product' else f'{names.get(ref, ref)}: {tr(zh, en)}')
    if len(problems) > limit:
        lines.append(tr(f'还有 {len(problems) - limit} 项', f'{len(problems) - limit} more'))
    return lines


# ── Inspection checklists (templates per product type) ──────────────────────

def _require_checklist_editor():
    if not g.is_admin:            # checklist templates are maintained by admins only
        abort(403)


def checklist_version(template_id, version=None):
    """(template row, version row, template data) or (None, None, None)."""
    with db_conn() as conn:
        tpl = conn.execute('SELECT * FROM checklist_templates WHERE id=?', (template_id,)).fetchone()
        if not tpl:
            return None, None, None
        ver = conn.execute('SELECT * FROM checklist_versions WHERE template_id=? AND version=?',
                           (template_id, version or tpl['current_version'])).fetchone()
    if not ver:
        return tpl, None, None
    return tpl, ver, json.loads(ver['data_json'])


def save_checklist_version(conn, template_id, data, note=''):
    """Store `data` as the next version of a template; returns the version number."""
    last = conn.execute('SELECT MAX(version) FROM checklist_versions WHERE template_id=?',
                        (template_id,)).fetchone()[0] or 0
    conn.execute('INSERT INTO checklist_versions (template_id, version, data_json, note, created_by) '
                 'VALUES (?,?,?,?,?)', (template_id, last + 1, json.dumps(data, ensure_ascii=False),
                                       note, g.get('display_name') or g.get('username', '')))
    conn.execute("UPDATE checklist_templates SET current_version=?, updated_at=datetime('now') WHERE id=?",
                 (last + 1, template_id))
    return last + 1


def checklist_for_product_type(ptype):
    """The active checklist template for a product type, or None."""
    if not ptype:
        return None
    with db_conn() as conn:
        for tpl in conn.execute('SELECT * FROM checklist_templates WHERE active=1 ORDER BY id'):
            if ptype in (tpl['product_types'] or '').split(','):
                return tpl
    return None


def _code_patterns(text):
    """'ACLTYPESCFA, ACLTYPED*' -> ['ACLTYPESCFA', 'ACLTYPED*'] (upper case)."""
    return [p.strip().upper() for p in re.split(r'[,;\s]+', text or '') if p.strip()]


def _code_matches(code, pattern):
    code = (code or '').strip().upper()
    return code.startswith(pattern[:-1]) if pattern.endswith('*') else code == pattern


def checklist_for_item(item_code, description):
    """The active template for a job: one listing this item code first (exact
    or prefix*), else the one for its product type."""
    with db_conn() as conn:
        for tpl in conn.execute('SELECT * FROM checklist_templates WHERE active=1 ORDER BY id'):
            if any(_code_matches(item_code, p) for p in _code_patterns(tpl['item_codes'])):
                return tpl
    return checklist_for_product_type(product_type_for(item_code, description))


def _product_type_choices():
    return [(key, product_type_name(key)) for key in evidence_rules.PRODUCT_TYPES]


@app.route('/checklists')
def checklist_templates_page():
    _require_checklist_editor()
    with db_conn() as conn:
        templates = [dict(r) for r in conn.execute('SELECT * FROM checklist_templates ORDER BY active DESC, name')]
    for t in templates:
        _tpl, _ver, data = checklist_version(t['id'])
        t['questions'] = checklists.question_count(data) if data else 0
        t['type_names'] = [product_type_name(k) for k in (t['product_types'] or '').split(',')
                           if k in evidence_rules.PRODUCT_TYPES] + _code_patterns(t.get('item_codes'))
    return render_template('checklists.html', templates=templates)



@app.route('/checklists/bulk', methods=['GET', 'POST'])
def checklist_bulk_import():
    """Import many checklists at once from an import plan (Plan +
    Translations sheets) and the checklist files it lists. New checklists are
    created inactive; a checklist with the same name gets a new version."""
    _require_checklist_editor()
    if request.method == 'GET':
        return render_template('checklist_bulk.html', results=None)
    plan_file = request.files.get('plan')
    if not plan_file or _upload_extension(plan_file.filename) != '.xlsx':
        flash(tr('请选择导入计划（.xlsx）', 'Choose the import plan (.xlsx)'), 'error')
        return redirect(url_for('checklist_bulk_import'))
    try:
        plan, table = checklists.parse_import_plan(plan_file.read())
    except Exception as exc:
        flash(tr('无法读取导入计划：', 'The import plan could not be read: ') + str(exc)[:200], 'error')
        return redirect(url_for('checklist_bulk_import'))
    uploads = {os.path.basename(f.filename or '').lower(): f for f in request.files.getlist('files') if f.filename}
    results = []
    with db_conn() as conn:
        for row in plan:
            result = {'file': row['file'], 'name': row['name'] or row['file'], 'status': '', 'detail': ''}
            results.append(result)
            if row['use'] != 'Y':
                result['status'] = 'skipped'
                result['detail'] = row['note']
                continue
            upload = uploads.get(row['file'].lower())
            if not upload:
                result['status'] = 'missing'
                continue
            try:
                data = checklists.parse_checklist_workbook(upload.read())
            except Exception as exc:
                result['status'] = 'error'
                result['detail'] = str(exc)[:200]
                continue
            result['untranslated'] = checklists.apply_translations(data, table)
            result['questions'] = checklists.question_count(data)
            types = ','.join(t for t in row['types'] if t in evidence_rules.PRODUCT_TYPES)
            codes = ', '.join(dict.fromkeys(_code_patterns(row['codes'])))
            existing = conn.execute('SELECT id FROM checklist_templates WHERE name=?', (result['name'],)).fetchone()
            if existing:
                template_id = existing['id']
                conn.execute('UPDATE checklist_templates SET product_types=?, item_codes=? WHERE id=?',
                             (types, codes, template_id))
                result['status'] = 'updated'
            else:
                template_id = conn.execute(
                    'INSERT INTO checklist_templates (name, product_types, item_codes, active) VALUES (?,?,?,0)',
                    (result['name'], types, codes)).lastrowid
                result['status'] = 'created'
            current = conn.execute('SELECT data_json FROM checklist_versions WHERE template_id=? '
                                   'ORDER BY version DESC LIMIT 1', (template_id,)).fetchone()
            previous = json.loads(current['data_json']) if current else None
            data = checklists.carry_ids(data, previous)
            if previous == data:
                result['version'] = 'same'
            else:
                result['version'] = save_checklist_version(
                    conn, template_id, data, tr('批量导入：', 'Bulk import: ') + row['file'])
            result['template_id'] = template_id
    counts = Counter(r['status'] for r in results)
    flash(tr(f"新建 {counts.get('created', 0)}，更新 {counts.get('updated', 0)}，跳过 {counts.get('skipped', 0)}，"
             f"缺文件 {counts.get('missing', 0)}，出错 {counts.get('error', 0)}。新建的清单默认停用，检查后再启用。",
             f"Created {counts.get('created', 0)}, updated {counts.get('updated', 0)}, skipped {counts.get('skipped', 0)}, "
             f"missing {counts.get('missing', 0)}, errors {counts.get('error', 0)}. New checklists start inactive — "
             "check them, then activate."), 'success' if not counts.get('error') else 'warning')
    return render_template('checklist_bulk.html', results=results)


@app.route('/checklists/import', methods=['POST'])
def checklist_import():
    _require_checklist_editor()
    upload = request.files.get('file')
    if not upload or _upload_extension(upload.filename) != '.xlsx':
        flash(tr('请选择 .xlsx 检查清单文件', 'Choose a checklist .xlsx file'), 'error')
        return redirect(url_for('checklist_templates_page'))
    try:
        data = checklists.parse_checklist_workbook(upload.read())
    except Exception as exc:
        flash(tr('无法读取这个检查清单：', 'This checklist could not be read: ') + str(exc)[:200], 'error')
        return redirect(url_for('checklist_templates_page'))
    target = request.form.get('target', 'new')
    with db_conn() as conn:
        if target.isdigit() and conn.execute('SELECT 1 FROM checklist_templates WHERE id=?', (target,)).fetchone():
            template_id = int(target)
            current = conn.execute('SELECT data_json FROM checklist_versions WHERE template_id=? '
                                   'ORDER BY version DESC LIMIT 1', (template_id,)).fetchone()
            data = checklists.carry_ids(data, json.loads(current['data_json']) if current else None)
        else:
            name = request.form.get('name', '').strip() or data['title'] or _display_filename(upload.filename)
            template_id = conn.execute('INSERT INTO checklist_templates (name, product_types) VALUES (?, ?)',
                                       (name[:200], '')).lastrowid
        version = save_checklist_version(conn, template_id, data,
                                         tr('从 Excel 导入：', 'Imported from Excel: ') + _display_filename(upload.filename))
    flash(tr(f'已导入 {checklists.question_count(data)} 个检查项（第 {version} 版），请检查并设置适用的产品类别',
             f'Imported {checklists.question_count(data)} questions (version {version}). Check them and choose the product types.'),
          'success')
    return redirect(url_for('checklist_edit', template_id=template_id))


@app.route('/checklists/<int:template_id>/edit', methods=['GET', 'POST'])
def checklist_edit(template_id):
    _require_checklist_editor()
    tpl, ver, data = checklist_version(template_id)
    if not tpl:
        abort(404)
    if request.method == 'POST':
        try:
            new_data = checklists.normalise_template(json.loads(request.form.get('data_json') or '{}'))
        except (ValueError, json.JSONDecodeError) as exc:
            flash(tr('未保存：', 'Not saved: ') + str(exc), 'error')
            return redirect(url_for('checklist_edit', template_id=template_id))
        types = [t for t in request.form.getlist('product_types') if t in evidence_rules.PRODUCT_TYPES]
        codes = ', '.join(dict.fromkeys(_code_patterns(request.form.get('item_codes', ''))))
        with db_conn() as conn:
            conn.execute('UPDATE checklist_templates SET name=?, product_types=?, item_codes=?, active=? WHERE id=?',
                         ((request.form.get('name') or tpl['name']).strip()[:200], ','.join(types), codes[:2000],
                          1 if request.form.get('active') == '1' else 0, template_id))
            if new_data != data:
                version = save_checklist_version(conn, template_id, new_data, request.form.get('note', '').strip()[:300])
                flash(tr(f'已保存为第 {version} 版。已提交的报告保留原来的版本。',
                         f'Saved as version {version}. Submitted reports keep the version they used.'), 'success')
            else:
                flash(tr('设置已保存（检查项没有变化）', 'Settings saved (questions unchanged)'), 'success')
        return redirect(url_for('checklist_edit', template_id=template_id))
    with db_conn() as conn:
        versions = conn.execute('SELECT version, note, created_by, created_at FROM checklist_versions '
                                'WHERE template_id=? ORDER BY version DESC', (template_id,)).fetchall()
        others = {t: r['name'] for r in conn.execute(
            'SELECT id, name, product_types FROM checklist_templates WHERE active=1 AND id!=?', (template_id,))
            for t in (r['product_types'] or '').split(',') if t}
    return render_template('checklist_edit.html', tpl=tpl, ver=ver, data=data, versions=versions,
                           type_choices=_product_type_choices(), taken_types=others,
                           selected_types=(tpl['product_types'] or '').split(','))


@app.route('/checklists/<int:template_id>/preview')
def checklist_preview(template_id):
    _require_checklist_editor()
    version = request.args.get('v', type=int)
    tpl, ver, data = checklist_version(template_id, version)
    if not data:
        abort(404)
    return render_template('checklist_preview.html', tpl=tpl, ver=ver, data=data)


@app.route('/checklists/<int:template_id>/export.xlsx')
def checklist_export(template_id):
    """The current version as an Excel sheet in the import layout (with
    Chinese columns), for offline review or translation and re-import."""
    _require_checklist_editor()
    tpl, ver, data = checklist_version(template_id)
    if not data:
        abort(404)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Inspection Checklist'
    ws.append([data.get('title') or tpl['name']])
    ws.append([])
    ws.append(['PART', 'PART 中文', 'Q.', 'INSPECTION GUIDELINE', 'INSPECTION GUIDELINE 中文', 'IF',
               'WHAT TO DO?', 'WHAT TO DO? 中文', 'FREQUENCY', 'FREQUENCY 中文', 'PHOTO', 'ONLY IF'])
    for cell in ws[3]:
        cell.font = openpyxl.styles.Font(bold=True)
    for s in data['sections']:
        part = s['name'] + (' (IF APPLICABLE)' if s['optional'] else '')
        for i, q in enumerate(s['questions'], 1):
            text = q['text'] + (' (IF APPLICABLE)' if q['optional'] else '')
            if q['type'] == 'rating':
                text += ' [Good/ Fair/ Poor]'
                cond = 'If fair or poor' if q.get('fail_on') == 'fair' else 'If poor'
            elif q['type'] == 'number':
                cond = (f"If < {q['min']}{q.get('unit', '')}" if q.get('min') is not None
                        else f"If > {q['max']}{q.get('unit', '')}")
            elif q['type'] == 'text':
                cond = '-'
            else:
                cond = f"If {q.get('fail_on', 'no')}"
            # the condition, photo rule and keyword are ours, not typed text: no formula escaping
            ws.append([_xl_safe(v) for v in (part if i == 1 else '', s['name_zh'] if i == 1 else '', i, text,
                                              q['text_zh'])] + [cond] +
                      [_xl_safe(v) for v in (q['action'], q['action_zh'], q.get('hint', ''), q.get('hint_zh', ''))] +
                      [q.get('photo', 'fail'), q.get('only_if', '')])
    for letter, width in zip('ABCDEFGHIJKL', (22, 14, 5, 70, 50, 14, 20, 16, 22, 18, 8, 10)):
        ws.column_dimensions[letter].width = width
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe = re.sub(r'[^A-Za-z0-9._-]+', '-', tpl['name'])[:60]
    return send_file(buf, as_attachment=True, download_name=f'checklist-{safe}-v{ver["version"]}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


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
        allowed, message = inspect_permission(job_key)
        if not allowed:
            flash(message, 'error')
            return redirect(url_for('inspect_checklist', job_key=job_key, tpl=tpl_id))
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
                 f.get('dpl_number','').strip(),
                 f.get('inspector','').strip() if g.can_assign else inspector_display_name(),
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
                           can_inspect=inspect_permission(job_key)[0],
                           inspect_block_message=inspect_permission(job_key)[1],
                           now_date=china_today().isoformat())


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
    today = china_today().isoformat()
    month = request.args.get('month', china_now().strftime('%Y-%m'))
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
                           tab=tab, today=today, now_time=china_now().strftime('%H:%M'),
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

    work_date = f.get('work_date', china_today().isoformat())
    checkin_time = f.get('checkin_time', china_now().strftime('%H:%M'))
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

    work_date = f.get('work_date', china_today().isoformat())
    checkout_time = f.get('checkout_time', china_now().strftime('%H:%M'))
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

OLD_REPORTS_FIX_FLAG = 'fixed_old_shipped_reports_assignee'


def _lead_account(conn):
    """Murphy's account (username 'murphy'), else the only active lead."""
    lead = conn.execute(
        "SELECT id FROM users WHERE LOWER(username)='murphy' AND active=1").fetchone()
    if not lead:
        leads = conn.execute("SELECT id FROM users WHERE role='lead' AND active=1").fetchall()
        lead = leads[0] if len(leads) == 1 else None
    return lead


def assign_unscheduled_tasks_to_lead():
    """Business rule: an open, unassigned task whose order is no longer on the
    current schedule goes to Murphy, who decides whether to inspect or close
    it. Tasks already assigned to someone stay with them. No e-mail is sent.
    Runs at start-up and after every schedule upload. Returns the count."""
    current = load_schedule(CURRENT_FILE)
    if not current:
        return 0
    in_schedule = {make_job_key(sheet, r, rows[0])
                   for sheet, rows in current.items() for r in (rows or [])[1:]}
    with db_conn() as conn:
        lead = _lead_account(conn)
        if not lead:
            return 0
        rows = conn.execute(
            "SELECT id, job_key FROM inspection_tasks WHERE assigned_to IS NULL "
            "AND IFNULL(status, '') NOT IN ('Completed', 'Closed')").fetchall()
        ids = [r['id'] for r in rows if r['job_key'] not in in_schedule]
        for task_id in ids:
            conn.execute(
                'UPDATE inspection_tasks SET assigned_to=?, assigned_by=?, assigned_at=?, assign_note=? '
                'WHERE id=? AND assigned_to IS NULL',
                (lead['id'], 'system', datetime.now().strftime('%Y-%m-%d %H:%M'),
                 '订单已不在排期，自动分配给 Murphy / Order left the schedule, assigned to Murphy',
                 task_id))
    if ids:
        logger.info('Assigned %s unscheduled task(s) to Murphy', len(ids))
    return len(ids)


def reclassify_l_type_heads():
    """L-Type hydrant heads used to share the cover's product type; give
    reports saved before the split the head type. Idempotent."""
    cache = load_json(INSPECTIONS_CACHE, {})
    changed = 0
    for records in cache.values():
        for rec in records or []:
            if rec.get('product_type') == 'l_type' and \
                    product_type_for(rec.get('item_code', ''), rec.get('item_description', '')) == 'l_type_head':
                rec['product_type'] = 'l_type_head'
                changed += 1
    if changed:
        save_json(INSPECTIONS_CACHE, cache)
        logger.info('Re-labelled %d L-Type hydrant head report(s)', changed)
    return changed


def _assign_old_shipped_reports_to_lead():
    """One-time clean-up (agreed with the business): tasks that already have an
    inspection report but no assignee, and whose order has left the current
    schedule (fully shipped), are recorded as Murphy's. These reports were
    submitted before tasks had to be assigned first. Runs once; retried on
    the next start if Murphy's account does not exist yet."""
    config = load_config()
    if config.get(OLD_REPORTS_FIX_FLAG):
        return 0
    current = load_schedule(CURRENT_FILE)
    if not current:
        return 0  # no schedule yet: cannot tell what has shipped
    reconcile_tasks_with_inspections()  # make sure every report has its task
    in_schedule = {make_job_key(sheet, r, rows[0])
                   for sheet, rows in current.items() for r in (rows or [])[1:]}
    reported = {k for k, recs in load_json(INSPECTIONS_CACHE, {}).items() if recs}
    with db_conn() as conn:
        lead = _lead_account(conn)
        if not lead:
            logger.warning('Old shipped reports not re-assigned: no Murphy / single lead account yet')
            return 0
        rows = conn.execute(
            'SELECT id, job_key FROM inspection_tasks WHERE assigned_to IS NULL').fetchall()
        ids = [r['id'] for r in rows if r['job_key'] in reported and r['job_key'] not in in_schedule]
        for task_id in ids:
            conn.execute(
                'UPDATE inspection_tasks SET assigned_to=?, assigned_by=?, assigned_at=?, assign_note=? '
                'WHERE id=? AND assigned_to IS NULL',
                (lead['id'], 'system', datetime.now().strftime('%Y-%m-%d %H:%M'),
                 '历史检验记录，统一归属 Murphy（一次性整理） / Historical report, assigned to Murphy',
                 task_id))
    config = load_config()
    config[OLD_REPORTS_FIX_FLAG] = datetime.now().strftime('%Y-%m-%d %H:%M')
    save_json(CONFIG_FILE, config)
    logger.info('Assigned %s old shipped report task(s) to Murphy', len(ids))
    return len(ids)


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
    _assign_old_shipped_reports_to_lead()
except Exception:
    logger.exception('Unable to assign old shipped reports')

try:
    assign_unscheduled_tasks_to_lead()
except Exception:
    logger.exception('Unable to assign unscheduled tasks')

try:
    reclassify_l_type_heads()
except Exception:
    logger.exception('Unable to re-label L-Type hydrant head reports')

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
# hq = Melbourne head office: views everything (incl. dashboard and prices)
# and reviews reports, but does not inspect or assign.
ROLES = ('admin', 'lead', 'inspector', 'hq')

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
        last_logins = {row['user_id']: row for row in conn.execute(
            "SELECT user_id, MAX(created_at) AS created_at, ip FROM login_events "
            "WHERE event='login' AND user_id IS NOT NULL GROUP BY user_id")}
    return render_template('users.html', users=users, employees=employees, last_logins=last_logins)

LOGIN_HISTORY_LIMIT = 500

@app.route('/admin/logins')
def user_logins():
    """Who signed in, when and from which IP / device (newest first)."""
    user_filter = request.args.get('user', '').strip()
    event_filter = request.args.get('event', '').strip()
    where, params = [], []
    if user_filter:
        where.append('e.username = ? COLLATE NOCASE')
        params.append(user_filter)
    if event_filter in LOGIN_EVENTS:
        where.append('e.event = ?')
        params.append(event_filter)
    with db_conn() as conn:
        # new_ip: a successful login from an IP this account never logged in from before
        events = conn.execute(
            'SELECT e.*, u.display_name, '
            "  (e.event = 'login' AND e.user_id IS NOT NULL"
            "   AND EXISTS (SELECT 1 FROM login_events p WHERE p.user_id = e.user_id"
            "               AND p.event = 'login' AND p.id < e.id)"
            "   AND NOT EXISTS (SELECT 1 FROM login_events p WHERE p.user_id = e.user_id"
            "                   AND p.event = 'login' AND p.ip = e.ip AND p.id < e.id)) AS new_ip "
            'FROM login_events e LEFT JOIN users u ON u.id = e.user_id '
            + ('WHERE ' + ' AND '.join(where) + ' ' if where else '')
            + 'ORDER BY e.id DESC LIMIT ?', (*params, LOGIN_HISTORY_LIMIT)).fetchall()
        usernames = [row[0] for row in conn.execute('SELECT username FROM users ORDER BY username')]
    return render_template('login_history.html', events=events, usernames=usernames,
                           user_filter=user_filter, event_filter=event_filter,
                           limit=LOGIN_HISTORY_LIMIT, retention_days=LOGIN_EVENT_RETENTION_DAYS)

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
    if request.endpoint == 'inspection_draft_upload' and session.get('user_id'):
        request.max_content_length = DRAFT_FILE_MAX_BYTES   # signed-in users only; set before the body is read
    if request.method == 'POST' and request.endpoint != 'cron_reminders':
        submitted = request.headers.get('X-CSRF-Token') or request.form.get('_csrf_token')
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
    g.lang = ROLE_LANGUAGE.get(user['role'], g.lang)
    g.can_switch_lang = user['role'] not in ROLE_LANGUAGE
    g.user_id = user['id']
    g.username = user['username']
    g.role = user['role']
    g.employee_id = user['employee_id']
    g.is_admin = user['role'] == 'admin'
    g.is_hq = user['role'] == 'hq'
    g.can_assign = user['role'] in ('admin', 'lead')
    g.can_review = user['role'] in ('admin', 'lead', 'hq')
    g.can_see_prices = user['role'] in ('admin', 'hq')
    g.can_view_dashboard = user['role'] in ('admin', 'hq')
    if not app.config.get('TESTING') and _last_reminder_day['day'] != china_today():
        _last_reminder_day['day'] = china_today()
        _run_reminders_in_background(request.host_url)
    g.display_name = user['display_name'] or user['username']
    if request.endpoint == 'dashboard' and not g.can_view_dashboard:
        return tr('无权限访问此页面', 'Forbidden'), 403
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
        try:
            with app.test_request_context(base_url=base_url):
                send_backup_email()
        except Exception:
            logger.exception('Daily backup e-mail failed')
        try:
            with app.test_request_context(base_url=base_url):
                send_weekly_summary()
        except Exception:
            logger.exception('Weekly summary e-mail failed')
    import threading
    threading.Thread(target=work, daemon=True).start()


@app.route('/cron/reminders', methods=['GET', 'POST'])
def cron_reminders():
    """Token-protected hook for an external scheduler (Railway cron etc.)."""
    token = os.environ.get('CRON_SECRET', '')
    supplied = request.headers.get('X-Cron-Token', '') or request.args.get('token', '')
    if not token or not hmac.compare_digest(supplied, token):
        abort(404)
    return {'reminders_sent': send_due_reminders(), 'review_reminders_sent': send_review_reminders(),
            'vtrust_reminders_sent': send_vtrust_reminders()[0],
            'vtrust_reschedule_alerts': send_vtrust_reschedule_alerts()[0],
            'backup': send_backup_email()[1], 'weekly_summary': send_weekly_summary()[1]}


@app.route('/lang/<code>')
def set_language(code):
    """Switch UI language; remembered per account and per browser."""
    if code not in LANGUAGES:
        abort(404)
    user_id = session.get('user_id')
    if user_id:
        with db_conn() as conn:
            # roles with a fixed language (ROLE_LANGUAGE) keep it
            conn.execute('UPDATE users SET language=? WHERE id=? AND role NOT IN (%s)'
                         % ','.join('?' * len(ROLE_LANGUAGE)), (code, user_id, *ROLE_LANGUAGE))
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
            _log_login_event('blocked', username)
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
            _log_login_event('login', user['username'], user['id'])
            return redirect(_safe_next_url(request.args.get('next')) or url_for('index'))
        _record_login_failure(client_id)
        _log_login_event('failed', username, user['id'] if user else None)
        flash(tr('用户名或密码错误', 'Incorrect username or password'), 'error')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    if session.get('user_id'):
        _log_login_event('logout', g.get('username', ''), session['user_id'])
    session.clear()
    return redirect(url_for('login'))

@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(self)'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; img-src 'self' data: blob: https:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if IS_PRODUCTION:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

@app.after_request
def redirect_as_json_for_uploads(response):
    """The inspection form uploads with XMLHttpRequest (to show progress);
    hand it the redirect target as JSON so the browser navigates there itself
    and the flash message survives."""
    if request.headers.get('X-Upload') == '1' and response.status_code in (301, 302, 303):
        target = response.headers.get('Location', '')
        response.status_code = 200
        response.headers.pop('Location', None)
        response.set_data(json.dumps({'redirect': target}))
        response.mimetype = 'application/json'
    return response


@app.errorhandler(413)
def upload_too_large(_error):
    limit_mb = (request.max_content_length or 0) // (1024 * 1024)
    message = tr(f'上传文件过大（上限 {limit_mb} MB），请压缩后重试或分次提交。',
                 f'Upload too large (limit {limit_mb} MB). Compress the files or submit in parts.')
    if request.endpoint == 'inspection_draft_upload':
        return jsonify(ok=False, message=message), 413
    return message, 413

@app.errorhandler(500)
def internal_error(_error):
    logger.exception('Unhandled application error')
    return 'Internal server error', 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
