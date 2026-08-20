from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app_core.config import load_config
from app_core.runtime_db import backup_sqlite_database

CFG = load_config()
BASE_DIR = str(CFG.root_dir)
DATA_DIR = str(CFG.data_dir)
BACKUP_DIR = str(CFG.backup_dir)
LOG_DIR = str(CFG.log_dir)
DB_PATH = str(CFG.sqlite_db_path or "")
DB_BACKEND = str(CFG.db_backend)
DATABASE_URL = str(CFG.database_url or "")
STATUS_PATH = os.path.join(LOG_DIR, "automation_status.json")
LOG_PATH = os.path.join(LOG_DIR, "backup_automation.log")
REQUIRED_TABLES = {"users", "orders", "routes", "route_orders", "settings"}


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_dirs() -> None:
    for path in (DATA_DIR, BACKUP_DIR, LOG_DIR):
        os.makedirs(path, exist_ok=True)


def log_line(message: str) -> None:
    ensure_dirs()
    stamp = now_iso()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")


def load_status() -> dict:
    if not os.path.isfile(STATUS_PATH):
        return {}
    try:
        with open(STATUS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_status(status: dict) -> None:
    ensure_dirs()
    tmp_path = STATUS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATUS_PATH)


def list_backups() -> list[str]:
    ensure_dirs()
    extensions = [".sqlite3"] if DB_BACKEND == "sqlite" else [".dump"]
    files = [x for x in os.listdir(BACKUP_DIR) if any(x.lower().endswith(ext) for ext in extensions)]
    files.sort(key=lambda n: os.path.getmtime(os.path.join(BACKUP_DIR, n)), reverse=True)
    return files


def create_backup() -> str:
    if DB_BACKEND == "sqlite":
        if not os.path.isfile(DB_PATH):
            raise RuntimeError("Banco de dados principal nao encontrado para backup.")
        name = f"auto_backup_logistica_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.sqlite3"
        dest = os.path.join(BACKUP_DIR, name)
        backup_sqlite_database(DB_PATH, dest)
        log_line(f"Backup automatico criado: {name}")
        return name
    if DB_BACKEND == "postgresql":
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL nao configurada para backup PostgreSQL.")
        name = f"auto_backup_logistica_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.dump"
        dest = os.path.join(BACKUP_DIR, name)
        pg_dump_bin = os.environ.get("PG_DUMP_BIN", "pg_dump")
        proc = subprocess.run(
            [pg_dump_bin, "--format=custom", "--no-owner", "--file", dest, DATABASE_URL],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump falhou: {proc.stderr.strip() or proc.stdout.strip() or 'erro desconhecido'}")
        log_line(f"Backup automatico PostgreSQL criado: {name}")
        return name
    raise RuntimeError("Backend de banco nao suportado para backup automatico.")


def prune_backups(keep_max: int = 7, keep_days: int = 0, keep_min: int = 1) -> int:
    keep_max = max(1, int(keep_max or 7))
    keep_days = max(0, int(keep_days or 0))
    keep_min = max(1, int(keep_min or 1))
    backups = list_backups()
    removed = 0
    # Regra principal: mantém somente os N backups mais recentes.
    for name in backups[keep_max:]:
        path = os.path.join(BACKUP_DIR, name)
        try:
            os.remove(path)
            removed += 1
        except Exception:
            continue
    # Regra opcional por idade (aplica somente nos arquivos que sobraram).
    if keep_days > 0:
        backups = list_backups()
        cutoff_ts = time.time() - (keep_days * 86400)
        for name in reversed(backups):
            if (len(backups) - removed) <= keep_min:
                break
            path = os.path.join(BACKUP_DIR, name)
            try:
                if os.path.getmtime(path) < cutoff_ts:
                    os.remove(path)
                    removed += 1
            except Exception:
                continue
    if removed:
        log_line(f"Retencao executada: {removed} arquivo(s) removido(s). Limite maximo: {keep_max}.")
    return removed


def enforce_backup_cap(keep_max: int = 7) -> int:
    keep_max = max(1, int(keep_max or 7))
    backups = list_backups()
    removed = 0
    for name in backups[keep_max:]:
        path = os.path.join(BACKUP_DIR, name)
        try:
            os.remove(path)
            removed += 1
        except Exception:
            continue
    if removed:
        log_line(f"Cap de backups aplicado: removidos {removed}; mantidos os {keep_max} mais recentes.")
    return removed


def run_backup_flow(status: dict, keep_days: int = 0, keep_min: int = 1, keep_max: int = 7) -> bool:
    try:
        backup_name = create_backup()
        removed = prune_backups(keep_max=keep_max, keep_days=keep_days, keep_min=keep_min)
        status["last_auto_backup_ok"] = True
        status["last_auto_backup_at"] = now_iso()
        status["last_auto_backup_file"] = backup_name
        status["last_auto_backup_error"] = ""
        status["last_auto_backup_removed"] = int(removed)
        status["last_auto_backup_keep_max"] = int(max(1, int(keep_max or 7)))
        return True
    except Exception as e:
        status["last_auto_backup_ok"] = False
        status["last_auto_backup_at"] = now_iso()
        status["last_auto_backup_error"] = str(e)
        log_line(f"ERRO backup automatico: {e}")
        return False


def run_verify_flow(status: dict, keep_max: int = 7) -> bool:
    enforce_backup_cap(keep_max=keep_max)
    backups = list_backups()
    if not backups:
        status["last_auto_verify_ok"] = False
        status["last_auto_verify_at"] = now_iso()
        status["last_auto_verify_file"] = ""
        status["last_auto_verify_error"] = "Nenhum arquivo de backup encontrado para validacao."
        log_line("ERRO validacao semanal: nenhum backup disponivel.")
        return False
    target = backups[0]
    try:
        verify_backup_file(target)
        status["last_auto_verify_ok"] = True
        status["last_auto_verify_at"] = now_iso()
        status["last_auto_verify_file"] = target
        status["last_auto_verify_error"] = ""
        status["last_auto_verify_keep_max"] = int(max(1, int(keep_max or 7)))
        return True
    except Exception as e:
        status["last_auto_verify_ok"] = False
        status["last_auto_verify_at"] = now_iso()
        status["last_auto_verify_file"] = target
        status["last_auto_verify_error"] = str(e)
        log_line(f"ERRO validacao semanal: {e}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup automatico com retencao e validacao semanal.")
    parser.add_argument("--mode", choices=["backup", "verify", "all"], default="backup")
    parser.add_argument("--keep-days", type=int, default=0, help="Opcional: remove backups mais antigos que N dias (0 = desativado).")
    parser.add_argument("--keep-min", type=int, default=1, help="Compatibilidade: quantidade minima ao aplicar regra por idade.")
    parser.add_argument("--keep-max", type=int, default=7, help="Limite maximo de backups mantidos (regra principal).")
    args = parser.parse_args()

    ensure_dirs()
    status = load_status()
    ok = True

    if args.mode in ("backup", "all"):
        ok = run_backup_flow(status, keep_days=args.keep_days, keep_min=args.keep_min, keep_max=args.keep_max) and ok
    if args.mode in ("verify", "all"):
        ok = run_verify_flow(status, keep_max=args.keep_max) and ok

    status["last_automation_run_at"] = now_iso()
    status["last_automation_mode"] = args.mode
    save_status(status)
    return 0 if ok else 1

def verify_backup_file(backup_name: str) -> None:
    if DB_BACKEND == "postgresql":
        pg_restore_bin = os.environ.get("PG_RESTORE_BIN", "pg_restore")
        backup_path = os.path.join(BACKUP_DIR, backup_name)
        if not os.path.isfile(backup_path):
            raise RuntimeError("Arquivo de backup para validacao nao encontrado.")
        proc = subprocess.run([pg_restore_bin, "--list", backup_path], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"pg_restore --list falhou: {proc.stderr.strip() or proc.stdout.strip() or 'erro desconhecido'}")
        if "TABLE" not in (proc.stdout or ""):
            raise RuntimeError("Validacao de backup PostgreSQL falhou: dump sem estrutura de tabelas.")
        log_line(f"Validacao semanal de backup PostgreSQL concluida com sucesso: {backup_name}")
        return
    if DB_BACKEND != "sqlite":
        raise RuntimeError("Validacao automatica nao suportada para este backend.")
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    if not os.path.isfile(backup_path):
        raise RuntimeError("Arquivo de backup para validacao nao encontrado.")
    temp_fd, temp_path = tempfile.mkstemp(prefix="restore_check_", suffix=".sqlite3")
    os.close(temp_fd)
    try:
        with sqlite3.connect(backup_path, timeout=20) as src_db, sqlite3.connect(temp_path, timeout=20) as dst_db:
            src_db.backup(dst_db)
        with sqlite3.connect(temp_path, timeout=20) as db:
            row = db.execute("PRAGMA integrity_check").fetchone()
            integrity = str(row[0] if row else "").strip().lower()
            if integrity != "ok":
                raise RuntimeError("Teste de restauracao falhou: integridade SQLite invalida.")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise RuntimeError("Teste de restauracao falhou: tabelas essenciais ausentes.")
        log_line(f"Validacao semanal de backup concluida com sucesso: {backup_name}")
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
