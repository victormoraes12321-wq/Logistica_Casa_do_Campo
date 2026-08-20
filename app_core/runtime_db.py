from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class RuntimeDatabaseTarget:
    backend: str
    database_url: str
    sqlite_path: str

    @property
    def is_sqlite(self) -> bool:
        return self.backend == "sqlite"

    @property
    def is_postgresql(self) -> bool:
        return self.backend == "postgresql"


def _normalize_sqlite_path(path_value: str, root_dir: Path) -> str:
    raw = str(path_value or "").strip()
    if not raw:
        return str((root_dir / "data" / "logistica_casa_do_campo.sqlite3").resolve())
    p = Path(raw)
    if not p.is_absolute():
        p = (root_dir / p).resolve()
    return str(p)


def _sqlite_url_from_path(path_value: str) -> str:
    normalized = Path(path_value).resolve().as_posix()
    return f"sqlite:///{normalized}"


def _target_from_database_url(database_url: str, root_dir: Path) -> RuntimeDatabaseTarget:
    raw = str(database_url or "").strip()
    if not raw:
        sqlite_path = _normalize_sqlite_path("", root_dir=root_dir)
        return RuntimeDatabaseTarget(backend="sqlite", database_url=_sqlite_url_from_path(sqlite_path), sqlite_path=sqlite_path)

    lowered = raw.lower()
    if lowered.startswith("sqlite:///"):
        parsed = urlparse(raw)
        sqlite_path = unquote(parsed.path or "")
        if os.name == "nt" and sqlite_path.startswith("/") and len(sqlite_path) >= 3 and sqlite_path[2] == ":":
            sqlite_path = sqlite_path[1:]
        elif os.name == "nt" and sqlite_path.startswith("/"):
            # On Windows, "sqlite:///data/app.sqlite3" should resolve relative to project root.
            # Keep true absolute paths (e.g. /C:/...) handled by the branch above.
            sqlite_path = sqlite_path.lstrip("/\\")
        sqlite_path = _normalize_sqlite_path(sqlite_path, root_dir=root_dir)
        return RuntimeDatabaseTarget(backend="sqlite", database_url=_sqlite_url_from_path(sqlite_path), sqlite_path=sqlite_path)

    if lowered.startswith("postgresql://") or lowered.startswith("postgres://"):
        return RuntimeDatabaseTarget(backend="postgresql", database_url=raw, sqlite_path="")

    raise ValueError("DATABASE_URL inválida. Use sqlite:///... ou postgresql://...")


def resolve_runtime_database(root_dir: Path, explicit_database_url: str | None = None, legacy_sqlite_path: str | None = None) -> RuntimeDatabaseTarget:
    if explicit_database_url and str(explicit_database_url).strip():
        return _target_from_database_url(str(explicit_database_url).strip(), root_dir=root_dir)
    sqlite_path = _normalize_sqlite_path(legacy_sqlite_path or "", root_dir=root_dir)
    return RuntimeDatabaseTarget(backend="sqlite", database_url=_sqlite_url_from_path(sqlite_path), sqlite_path=sqlite_path)


def create_runtime_connection(db_target: RuntimeDatabaseTarget, timeout_seconds: int = 20) -> sqlite3.Connection:
    if not db_target.is_sqlite:
        raise RuntimeError(
            "O runtime legado está ativo com SQL SQLite. "
            "Para PostgreSQL, use a trilha de migração SQLAlchemy/Alembic e a migração gradual de rotas."
        )
    if not db_target.sqlite_path:
        raise RuntimeError("Caminho SQLite não configurado.")
    conn = sqlite3.connect(db_target.sqlite_path, timeout=max(1, int(timeout_seconds or 20)))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=15000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    return conn


def backup_sqlite_database(source_db_path: str, destination_backup_path: str) -> None:
    src = str(source_db_path or "").strip()
    dst = str(destination_backup_path or "").strip()
    if not src or not os.path.isfile(src):
        raise FileNotFoundError("Arquivo do banco SQLite não encontrado para backup.")
    if not dst:
        raise ValueError("Caminho de destino para backup inválido.")
    os.makedirs(str(Path(dst).parent), exist_ok=True)
    with sqlite3.connect(src, timeout=20) as src_db, sqlite3.connect(dst) as dst_db:
        src_db.backup(dst_db)


def restore_sqlite_database(source_backup_path: str, destination_db_path: str) -> None:
    src = str(source_backup_path or "").strip()
    dst = str(destination_db_path or "").strip()
    if not src or not os.path.isfile(src):
        raise FileNotFoundError("Arquivo de backup não encontrado para restauração.")
    if not dst:
        raise ValueError("Destino do banco SQLite inválido para restauração.")
    with sqlite3.connect(src, timeout=20) as src_db, sqlite3.connect(dst) as dst_db:
        src_db.backup(dst_db)
