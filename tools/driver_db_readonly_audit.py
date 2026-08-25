from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_core.config import load_config


def main() -> int:
    config = load_config()
    if config.db_backend != "sqlite":
        print("SKIP: auditoria local de credenciais implementada para SQLite.")
        return 0

    path = Path(config.sqlite_db_path).resolve()
    if not path.is_file():
        print("FAIL: banco SQLite configurado não encontrado.")
        return 1

    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def columns(table: str) -> set[str]:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}

    def count(sql: str) -> int:
        return int(db.execute(sql).fetchone()[0])

    revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0] if "alembic_version" in tables else "missing"
    driver_columns = columns("drivers")
    invalid_hashes = count(
        "SELECT COUNT(*) FROM drivers WHERE COALESCE(password_hash,'') NOT LIKE 'pbkdf2_sha256$%'"
    )
    plaintext_pins = count("SELECT COUNT(*) FROM drivers WHERE pin IS NOT NULL") if "pin" in driver_columns else 0
    invalid_session_hashes = (
        count(
            "SELECT COUNT(*) FROM driver_sessions "
            "WHERE LENGTH(token_hash)<>64 OR token_hash GLOB '*[^0-9a-f]*'"
        )
        if "driver_sessions" in tables
        else -1
    )
    integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
    fk_issues = len(db.execute("PRAGMA foreign_key_check").fetchall())
    drivers = count("SELECT COUNT(*) FROM drivers")
    db.close()

    print(f"DB_FILE={path.name}")
    print(f"INTEGRITY={integrity}")
    print(f"FK_ISSUES={fk_issues}")
    print(f"ALEMBIC={revision}")
    print(f"DRIVERS={drivers}")
    print(f"DRIVER_HASH_INVALID={invalid_hashes}")
    print(f"PLAINTEXT_PIN_NON_NULL={plaintext_pins}")
    print(f"SESSION_TOKEN_HASH_INVALID={invalid_session_hashes}")
    return 1 if integrity.lower() != "ok" or fk_issues or invalid_hashes or plaintext_pins or invalid_session_hashes else 0


if __name__ == "__main__":
    raise SystemExit(main())
