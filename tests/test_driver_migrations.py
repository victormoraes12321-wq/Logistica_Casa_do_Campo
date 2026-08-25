# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEAD = "0004_driver_app_security_integrity"


class DriverMigrationTests(unittest.TestCase):
    def _upgrade(self, db_path: Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update({
            "DATABASE_URL": f"sqlite:///{db_path.resolve().as_posix()}",
            "LOGISTICA_IGNORE_DOTENV": "1",
            "FLASK_ENV": "development",
            "SECRET_KEY": "migration-test-only",
        })
        return subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )

    def test_clean_database_reaches_head_with_foreign_keys_valid(self):
        with tempfile.TemporaryDirectory(prefix="driver_migration_clean_") as tmp:
            db_path = Path(tmp) / "clean.sqlite3"
            result = self._upgrade(db_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with closing(sqlite3.connect(db_path)) as db:
                revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                self.assertEqual(revision, HEAD)
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({
                    "drivers", "orders", "routes", "route_orders", "delivery_receipts",
                    "delivery_problems", "driver_sessions", "driver_delivery_operations",
                }.issubset(tables))
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_legacy_revision_backfills_hash_and_clears_plaintext_pin(self):
        with tempfile.TemporaryDirectory(prefix="driver_migration_legacy_") as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            with closing(sqlite3.connect(db_path)) as db:
                db.executescript("""
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE drivers(
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        active INTEGER DEFAULT 1,
                        pin TEXT
                    );
                    CREATE TABLE orders(id INTEGER PRIMARY KEY);
                    CREATE TABLE routes(id INTEGER PRIMARY KEY);
                    CREATE TABLE delivery_problems(
                        id INTEGER PRIMARY KEY,
                        order_id INTEGER NOT NULL,
                        problem_type TEXT,
                        description TEXT,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
                    );
                    CREATE TABLE alembic_version(version_num VARCHAR(64) NOT NULL PRIMARY KEY);
                    INSERT INTO alembic_version(version_num) VALUES('0003_runtime_hardening_columns_indexes');
                    INSERT INTO drivers(id,name,active,pin) VALUES(1,'Motorista Legado',1,'123');
                """)
            result = self._upgrade(db_path)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with closing(sqlite3.connect(db_path)) as db:
                db.row_factory = sqlite3.Row
                driver = db.execute(
                    "SELECT password_hash,must_change_password,pin FROM drivers WHERE id=1"
                ).fetchone()
                self.assertTrue(driver["password_hash"].startswith("pbkdf2_sha256$"))
                self.assertEqual(driver["must_change_password"], 1)
                self.assertIsNone(driver["pin"])
                self.assertEqual(db.execute("SELECT version_num FROM alembic_version").fetchone()[0], HEAD)
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
