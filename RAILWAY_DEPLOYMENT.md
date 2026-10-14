# Railway production deployment

## Required before deployment

1. Back up the current production `app.db`, JSON schedule/config files, and uploaded files.
2. Create a Railway Volume mounted at `/data`.
3. Configure the variables below. Never place their real values in Git.
4. Deploy the branch and verify `/healthz`, login, role permissions, and data persistence.

The Railway service Root Directory must remain `prod_schedule`; the start command assumes Railway
starts inside that directory.

## Build and service settings

Railway stops reading `railway.toml` / `railway.json` on 2026-12-01, so the repo no longer has them
and nothing about the deploy depends on them:

- **Build:** Railpack, configured by `prod_schedule/railpack.json` (Python 3.11 as in
  `.python-version`; runtime apt packages `...` = Railpack's defaults plus `fonts-wqy-microhei`,
  the Chinese font embedded in the PDF reports).
- **Stored on the Railway service** (Settings, or `railway config apply`):
  - Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 8 --timeout 600`
  - Health check path `/healthz`, timeout 300 s
  - Build command `pip install -r requirements.txt`, Root Directory `prod_schedule`, Volume at `/data`

If these service settings are ever changed with Railway Infrastructure as Code, start from
`railway config pull` (it keeps every variable as `preserve()`): a file listing only some settings
plans to delete the variables, the GitHub source and the volume attachment.

## Environment variables

Required:

- `APP_DATA_DIR=/data`
- `SECRET_KEY`: a strong random value
- `BOOTSTRAP_ADMIN_USERNAME`: required only for the first deployment with an empty database
- `BOOTSTRAP_ADMIN_PASSWORD`: at least 12 characters; remove immediately after the first administrator is created

Optional integrations:

- `EXCEL_PASSWORD`: required only for encrypted schedule workbooks; rotate the previously exposed password
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `GOOGLE_SERVICE_ACCOUNT_JSON_B64`: base64-encoded service account JSON
- `MAX_UPLOAD_BYTES`: defaults to 50 MB (one request: report submit, schedule upload)
- `DRAFT_FILE_MAX_BYTES`: defaults to 300 MB (one evidence / checklist file, e.g. a video)
- `SESSION_HOURS`: defaults to 8 hours

## Volume layout

The application stores runtime state under `APP_DATA_DIR`:

- `/data/data/app.db`
- `/data/data/current_week.json`
- `/data/data/previous_week.json`
- `/data/data/inspections_cache.json`
- `/data/data/config.json`
- `/data/uploads/`
- `/data/product_images/`

Copy the backed-up production files into these locations before directing users to the new deployment.

## Post-deployment checks

- `/healthz` returns HTTP 200 without authentication.
- All business routes redirect unauthenticated users to `/login`.
- Inspector accounts receive HTTP 403 for settings, uploads, approvals, deletes, and account management.
- Restart the Railway service and confirm accounts, schedules, and uploaded files still exist.
- Remove `BOOTSTRAP_ADMIN_PASSWORD` after the first administrator exists.
