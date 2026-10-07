"""Country of an IP address, from the free DB-IP "IP to Country Lite" database
(CC BY 4.0, https://db-ip.com).

The database (~8 MB) is downloaded into APP_DATA_DIR/geoip on first use and
refreshed in the background once it is a month old. Lookups run on the server
only: no IP address is ever sent to another service.
"""
import gzip
import ipaddress
import logging
import os
import threading
import time
import urllib.request
from datetime import date, timedelta

import maxminddb

logger = logging.getLogger(__name__)

URL = 'https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz'
MAX_AGE_SECONDS = 35 * 24 * 3600
RETRY_SECONDS = 3600

_lock = threading.Lock()
_state = {'reader': None, 'mtime': None, 'downloading': False, 'last_try': 0.0}


def db_path(data_dir):
    return os.path.join(data_dir, 'geoip', 'dbip-country-lite.mmdb')


def download(path):
    """Fetch this month's database (or last month's, early in a month) and
    replace `path` atomically. Returns True on success."""
    first = date.today().replace(day=1)
    for month in (first, first - timedelta(days=1)):
        url = URL.format(month=month.strftime('%Y-%m'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.{os.getpid()}.tmp'
        try:
            # DB-IP refuses Python's default User-Agent (403)
            request = urllib.request.Request(url, headers={'User-Agent': 'prod-schedule-qc/1.0'})
            with urllib.request.urlopen(request, timeout=120) as resp:
                data = gzip.decompress(resp.read())
            with open(tmp, 'wb') as f:
                f.write(data)
            maxminddb.open_database(tmp, mode=maxminddb.MODE_MEMORY).close()   # must be a valid database
            os.replace(tmp, path)
        except Exception as exc:
            logger.info('GeoIP download %s failed: %s', url, exc)
            if os.path.exists(tmp):
                os.remove(tmp)
            continue
        logger.info('GeoIP database updated from %s', url)
        return True
    return False


def _refresh_in_background(path):
    def work():
        try:
            download(path)
        finally:
            _state['downloading'] = False
    _state['downloading'] = True
    _state['last_try'] = time.time()
    threading.Thread(target=work, daemon=True).start()


def _reader(data_dir, auto_download):
    path = db_path(data_dir)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    with _lock:
        stale = mtime is None or time.time() - mtime > MAX_AGE_SECONDS
        if (auto_download and stale and not _state['downloading']
                and time.time() - _state['last_try'] > RETRY_SECONDS):
            _refresh_in_background(path)
        if mtime is not None and mtime != _state['mtime']:
            try:
                # read into memory: the file can then be replaced while in use (Windows too)
                _state['reader'] = maxminddb.open_database(path, mode=maxminddb.MODE_MEMORY)
                _state['mtime'] = mtime
            except Exception:
                logger.exception('Unable to open GeoIP database')
        return _state['reader']


def lookup(ip, data_dir, auto_download=True):
    """('CN', {'en': 'China', 'zh-CN': '中国', ...}), ('LAN', {}) for private /
    local addresses, or None when unknown or the database is not available yet."""
    try:
        address = ipaddress.ip_address((ip or '').strip())
    except ValueError:
        return None
    if not address.is_global:
        return 'LAN', {}
    reader = _reader(data_dir, auto_download)
    if reader is None:
        return None
    try:
        country = (reader.get(str(address)) or {}).get('country') or {}
    except Exception:
        return None
    code = country.get('iso_code')
    return (code, country.get('names') or {}) if code else None
