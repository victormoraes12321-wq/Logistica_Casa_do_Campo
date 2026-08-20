from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from app_core.runtime_db import backup_sqlite_database, restore_sqlite_database


def backup_filename(prefix: str = "backup_logistica_casa_do_campo") -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.sqlite3"


def sanitize_backup_file_name(file_name: str) -> str:
    name = str(file_name or "").strip()
    if not name:
        raise ValueError("Arquivo de backup não informado.")
    if "/" in name or "\\" in name:
        raise ValueError("Arquivo de backup inválido.")
    if not name.lower().endswith(".sqlite3"):
        raise ValueError("Arquivo de backup inválido.")
    return name


def create_sqlite_backup(db_path: str, backup_dir: str | Path, prefix: str = "backup_logistica_casa_do_campo") -> str:
    target_dir = Path(str(backup_dir))
    target_dir.mkdir(parents=True, exist_ok=True)
    name = backup_filename(prefix=prefix)
    dest = str((target_dir / name).resolve())
    backup_sqlite_database(str(db_path), dest)
    return name


def list_backup_files(backup_dir: str | Path, extensions: tuple[str, ...] = (".sqlite3", ".dump")) -> list[str]:
    target_dir = Path(str(backup_dir))
    if not target_dir.exists():
        return []
    entries = [p for p in target_dir.iterdir() if p.is_file() and p.suffix.lower() in extensions]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in entries]


def prune_backup_files(backup_dir: str | Path, keep_latest: int = 7, extensions: tuple[str, ...] = (".sqlite3", ".dump")) -> int:
    keep_latest = max(1, int(keep_latest or 7))
    target_dir = Path(str(backup_dir))
    if not target_dir.exists():
        return 0
    entries = [p for p in target_dir.iterdir() if p.is_file() and p.suffix.lower() in extensions]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    to_remove = entries[keep_latest:]
    removed = 0
    for file_path in to_remove:
        try:
            file_path.unlink()
            removed += 1
        except Exception:
            continue
    return removed


def restore_sqlite_from_backup(db_path: str, backup_dir: str | Path, backup_file_name: str) -> tuple[str, str]:
    target_dir = Path(str(backup_dir))
    target_dir.mkdir(parents=True, exist_ok=True)
    backup_name = sanitize_backup_file_name(backup_file_name)
    src_path = str((target_dir / backup_name).resolve())
    if not os.path.isfile(src_path):
        raise ValueError("Backup selecionado não encontrado.")
    safety_name = backup_filename(prefix="pre_restore")
    safety_path = str((target_dir / safety_name).resolve())
    backup_sqlite_database(str(db_path), safety_path)
    restore_sqlite_database(src_path, str(db_path))
    return backup_name, safety_name
