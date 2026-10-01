"""Shared test helpers."""
from db import db_conn


def assign_job(job_key, username, status='Pending'):
    """Give `username` an inspection task for `job_key` (inspectors may only
    submit inspections for jobs assigned to them)."""
    with db_conn() as conn:
        user_id = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()[0]
        conn.execute('DELETE FROM inspection_tasks WHERE job_key=?', (job_key,))
        conn.execute(
            'INSERT INTO inspection_tasks (job_key, region, order_number, item_code, status, assigned_to) '
            'VALUES (?,?,?,?,?,?)',
            (job_key, job_key.split('|')[0], job_key.split('|')[1], job_key.split('|')[-1],
             status, user_id))
    return user_id
