# prod_schedule — QC inspection platform

Flask app used by a China-based QC team to inspect supplier production against a weekly
production-schedule workbook, and by HQ (Australia) to review the reports.
Production: Railway (Nixpacks, `railway.toml`), auto-deploys on every push to `main`.

## Layout
- Git root is this folder; the app lives in `prod_schedule/`. Only the paths whitelisted in
  `.gitignore` are tracked. **The repo is public**: never commit business data (schedule
  workbooks, reference/pricing sheets, checklists source files), real e-mail addresses or secrets.
- `app.py` — all routes and most logic (~115 routes). Large; grep before adding helpers.
- `db.py` — SQLite schema + additive migrations (`ALTER TABLE ... ADD COLUMN` guarded by checks).
- `checklists.py` — checklist template parsing (several Excel layouts), evaluation, bulk import.
- `evidence_rules.py` — product-type classification (`classify`) and required evidence per type.
- `pdf_report.py` — inspection report PDF (reportlab, CJK font).
- `static/checklist.js` — checklist UI on the inspection page (drafts autosave, uploads via XHR).
- `templates/` — Jinja; `tests/` — pytest (run all of them, they are fast).
- Root-level `*.py` helpers in `prod_schedule/` (load_6_4, fix_previous, …) are old one-off scripts.

## Run / test (Windows)
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt pytest
cd prod_schedule
..\.venv\Scripts\python.exe -m pytest -q
```
Local server: set `APP_DATA_DIR` to a local folder (e.g. `..\.localdata`) and `SECRET_KEY`, then
`..\.venv\Scripts\python.exe app.py`. `BOOTSTRAP_ADMIN_USERNAME/PASSWORD` create the first admin.
CI (`.github/workflows/tests.yml`) runs pytest on PRs and on main.

## Data
- `APP_DATA_DIR` (`/data` on Railway, a mounted volume): `data/app.db`, JSON files
  (`current_week.json`, `previous_week.json`, `inspections_cache.json`, `config.json`), `uploads/`.
- JSON writes go through `save_json` (atomic tmp + `os.replace`, retry on Windows PermissionError).
- Inspection records live in `inspections_cache.json` keyed by job key `REGION|PO|ITEMCODE`.
  `SharedReports` falls back across regions by PO+item; region moves are detected on upload
  (`detect_region_moves` / `migrate_job_key`).

## Conventions
- Bilingual UI: `tr('中文', 'English')`. Language is fixed by role (`ROLE_LANGUAGE`:
  inspector/lead → zh, hq → en); only admin can switch (`g.can_switch_lang`).
- Roles: `admin`, `lead` (assigns/reviews), `inspector`, `hq`. Flags `g.is_admin`, `g.can_assign`,
  `g.can_review`. Checklist template management is admin-only.
- Times stored in UTC. Pages use the `localtime` filter (browser-local); e-mails/PDFs use
  `dual_zone_time` / `time_text` ("北京 / Melbourne"); business dates use `china_today()`.
- Excel exports: wrap user text with `_xl_safe` (formula-injection guard).
- E-mail: `_smtp_send` via `SMTP_HOST/PORT/USERNAME/PASSWORD/FROM[_NAME]`; recipients are
  entered in Settings, never hard-coded.
- Files use **CRLF** line endings. `sed -i` strips them — restore with `sed -i 's/$/\r/'`, or
  write patch scripts in Python that normalise newlines. Don't put `\b`/`\n` escapes in heredocs.
- New features need tests in `tests/` (see `tests/helpers.py` for client/login setup).

## Checklists
Template JSON: `sections → questions` with `id, text/text_zh, type (yes_no|rating|number|text),
fail_on, min/max/unit, action/_zh, hint/_zh, optional, photo (fail|always), only_if`.
Versions in `checklist_versions`; a submitted inspection stores the exact version used.
Matching: item-code patterns (`ACSV*`) first, then product type. Re-imports keep question ids
(`carry_ids`) so unchanged content doesn't create a new version.

## Delivery workflow (owner's rule)
1. New branch per change → full pytest → push the branch → report and **wait for "merge"**.
2. On "merge": fast-forward `main`, re-run tests, push `main` (deploys), delete local+remote
   branch, check `https://<railway-host>/healthz`.
Never push `main` without that explicit per-change "merge". `git pull` main first.

## Environment variables
`SECRET_KEY`, `APP_DATA_DIR`, `SMTP_*`, `SMTP_FROM_NAME`, `CRON_SECRET`, `EXCEL_PASSWORD`,
`BOOTSTRAP_ADMIN_*`, `MAX_UPLOAD_BYTES`, `PDF_FONT_PATH`, `SESSION_HOURS`, `TRUSTED_PROXY_HOPS`.
Secrets are set by the owner in Railway — never ask for or handle their values.
