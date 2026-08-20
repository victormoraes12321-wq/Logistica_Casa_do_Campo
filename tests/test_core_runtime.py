from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app_core.runtime_db import resolve_runtime_database
from app_core.services.backup_service import backup_filename, sanitize_backup_file_name


class RuntimeDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="runtime_db_test_"))

    def test_resolve_sqlite_from_legacy_path(self):
        target = resolve_runtime_database(root_dir=self.root, explicit_database_url=None, legacy_sqlite_path="data/app.sqlite3")
        self.assertEqual(target.backend, "sqlite")
        self.assertTrue(target.sqlite_path.endswith(str(Path("data") / "app.sqlite3")))
        self.assertTrue(target.database_url.startswith("sqlite:///"))

    def test_resolve_postgresql_from_database_url(self):
        target = resolve_runtime_database(
            root_dir=self.root,
            explicit_database_url="postgresql://user:pass@localhost:5432/logistica",
            legacy_sqlite_path="",
        )
        self.assertEqual(target.backend, "postgresql")
        self.assertEqual(target.sqlite_path, "")

    def test_backup_name_and_sanitize(self):
        name = backup_filename(prefix="bk")
        self.assertTrue(name.startswith("bk_"))
        self.assertTrue(name.endswith(".sqlite3"))
        self.assertEqual(sanitize_backup_file_name("arquivo.sqlite3"), "arquivo.sqlite3")
        with self.assertRaises(ValueError):
            sanitize_backup_file_name("../arquivo.sqlite3")


if __name__ == "__main__":
    unittest.main()

