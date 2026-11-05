"""Minimal Dropbox API client for the off-site backup (standard library only).

Sign-in uses OAuth with PKCE, so only the public App key is needed (no App secret):
an admin connects once in Settings and the long-lived refresh token it returns is
kept by app.py in a private file outside every backup zip. The app should be an
"App folder" app, so it can only write inside /Apps/<app name>/ in that Dropbox.
"""
import base64
import hashlib
import io
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_URL = 'https://www.dropbox.com/oauth2/authorize'
TOKEN_URL = 'https://api.dropboxapi.com/oauth2/token'
API_URL = 'https://api.dropboxapi.com/2/'
CONTENT_URL = 'https://content.dropboxapi.com/2/'
CHUNK = 8 * 1024 * 1024          # files above this go up in pieces (upload session)
WRITE_SCOPE = 'files.content.write'


class DropboxError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def http(url, data, headers, timeout=180):
    """POST `data` and return (status, body). Kept separate so tests can replace it."""
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _parse(body):
    try:
        data = json.loads(body.decode('utf-8')) if body else {}
    except (ValueError, UnicodeDecodeError):
        return {'error_summary': body[:300].decode('utf-8', 'replace')}
    return data if isinstance(data, dict) else {}


def _form(fields):
    return urllib.parse.urlencode(fields).encode('ascii')


FORM_HEADERS = {'Content-Type': 'application/x-www-form-urlencoded'}


def new_pkce():
    """(code_verifier, code_challenge) for one sign-in."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest())
    return verifier, challenge.rstrip(b'=').decode('ascii')


def authorize_url(app_key, challenge, redirect_uri, state):
    return AUTHORIZE_URL + '?' + urllib.parse.urlencode({
        'client_id': app_key, 'response_type': 'code', 'token_access_type': 'offline',
        'code_challenge': challenge, 'code_challenge_method': 'S256',
        'redirect_uri': redirect_uri, 'state': state})


def exchange_code(app_key, code, verifier, redirect_uri):
    """Trade the code from the sign-in page for tokens (refresh_token, scope, account_id ...)."""
    status, body = http(TOKEN_URL, _form({
        'grant_type': 'authorization_code', 'code': code, 'client_id': app_key,
        'code_verifier': verifier, 'redirect_uri': redirect_uri}), FORM_HEADERS)
    data = _parse(body)
    if status != 200 or not data.get('refresh_token'):
        raise DropboxError(data.get('error_description') or data.get('error') or f'HTTP {status}', status)
    return data


class Client:
    def __init__(self, app_key, refresh_token):
        self.app_key = app_key
        self.refresh_token = refresh_token
        self._token = None
        self._expires = 0.0

    def _access_token(self):
        if not self._token or time.time() > self._expires - 300:
            status, body = http(TOKEN_URL, _form({
                'grant_type': 'refresh_token', 'refresh_token': self.refresh_token,
                'client_id': self.app_key}), FORM_HEADERS)
            data = _parse(body)
            if status != 200 or not data.get('access_token'):
                raise DropboxError(f"{data.get('error') or f'HTTP {status}'}: "
                                   f"{data.get('error_description') or data.get('error_summary') or ''}", status)
            self._token = data['access_token']
            self._expires = time.time() + int(data.get('expires_in') or 14400)
        return self._token

    def _call(self, url, data, headers):
        for attempt in range(3):
            status, body = http(url, data, dict(headers, Authorization='Bearer ' + self._access_token()))
            if status == 200:
                return _parse(body)
            result = _parse(body)
            if status == 401 and attempt == 0 and 'expired_access_token' in json.dumps(result):
                self._token = None                       # refresh and try once more
                continue
            if status == 429 and attempt < 2:            # rate limited: wait as told, at most a minute
                error = result.get('error')
                time.sleep(min(60, int(error.get('retry_after', 5) if isinstance(error, dict) else 5)))
                continue
            raise DropboxError(result.get('error_summary') or result.get('error_description')
                               or f'HTTP {status}', status)
        raise DropboxError('retries exhausted')

    def rpc(self, name, arg=None):
        return self._call(API_URL + name, json.dumps(arg).encode('utf-8'), {'Content-Type': 'application/json'})

    def _content(self, name, arg, data):
        # Dropbox-API-Arg must be plain ASCII: json.dumps escapes everything else
        return self._call(CONTENT_URL + name, data, {'Content-Type': 'application/octet-stream',
                                                     'Dropbox-API-Arg': json.dumps(arg)})

    def account(self):
        return self.rpc('users/get_current_account')

    def space(self):
        return self.rpc('users/get_space_usage')

    def revoke(self):
        return self.rpc('auth/token/revoke')

    def delete(self, path):
        return self.rpc('files/delete_v2', {'path': path})

    def move(self, from_path, to_path):
        return self.rpc('files/move_v2', {'from_path': from_path, 'to_path': to_path, 'autorename': False})

    def upload(self, local_path, remote_path):
        """Upload one file (overwriting a file of the same name); big files in CHUNK pieces."""
        with open(local_path, 'rb') as fh:
            return self._upload(fh, os.path.getsize(local_path), remote_path)

    def upload_bytes(self, data, remote_path):
        return self._upload(io.BytesIO(data), len(data), remote_path)

    def _upload(self, fh, size, remote_path):
        commit = {'path': remote_path, 'mode': 'overwrite', 'mute': True}
        if size <= CHUNK:
            return self._content('files/upload', commit, fh.read())
        session_id = self._content('files/upload_session/start', {'close': False}, fh.read(CHUNK))['session_id']
        offset = CHUNK
        while True:
            piece = fh.read(CHUNK)
            cursor = {'session_id': session_id, 'offset': offset}
            if offset + len(piece) >= size or not piece:
                return self._content('files/upload_session/finish', {'cursor': cursor, 'commit': commit}, piece)
            self._content('files/upload_session/append_v2', {'cursor': cursor, 'close': False}, piece)
            offset += len(piece)
