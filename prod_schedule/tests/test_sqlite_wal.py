"""The live database runs in WAL mode; the backup copy is a single plain file."""
import os
import sqlite3
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from db import db_conn, init_db


class WalTests(unittest.TestCase):
    def test_live_database_uses_wal(self):
        init_db()
        with db_conn() as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_snapshot_is_not_wal(self):
        dest = os.path.join(tempfile.mkdtemp(), 'copy.db')
        app._sqlite_snapshot(dest)
        conn = sqlite3.connect(dest)
        try:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertTrue(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='users'").fetchone()[0])
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
