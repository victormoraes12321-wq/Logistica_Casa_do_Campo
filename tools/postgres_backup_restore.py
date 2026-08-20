from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime
from pathlib import Path

from app_core.config import load_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backup/restore PostgreSQL para operacao real.")
    parser.add_argument("--mode", choices=["backup", "restore"], required=True)
    parser.add_argument("--database-url", default="", help="DATABASE_URL PostgreSQL alvo.")
    parser.add_argument("--file", default="", help="Arquivo .dump para backup/restore.")
    parser.add_argument("--confirm-restore", default="", help="Digite RESTAURAR para confirmar restore.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def ensure_dump_file(path: Path) -> None:
    if path.suffix.lower() != ".dump":
        raise RuntimeError("Arquivo invalido: use extensao .dump")


def main() -> int:
    args = parse_args()
    cfg = load_config()
    db_url = (args.database_url or cfg.database_url).strip()
    if not db_url.lower().startswith(("postgresql://", "postgres://")):
        raise RuntimeError("DATABASE_URL deve ser PostgreSQL para este script.")

    backups_dir = Path(cfg.backup_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    pg_dump_bin = os.environ.get("PG_DUMP_BIN", "pg_dump")
    pg_restore_bin = os.environ.get("PG_RESTORE_BIN", "pg_restore")

    if args.mode == "backup":
        target = Path(args.file).resolve() if args.file else (backups_dir / f"pg_backup_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.dump")
        ensure_dump_file(target)
        cmd = [pg_dump_bin, "--format=custom", "--no-owner", "--file", str(target), db_url]
        if args.dry_run:
            print("DRY-RUN:", " ".join(cmd))
            return 0
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "pg_dump falhou")
        print(f"Backup PostgreSQL gerado: {target}")
        return 0

    source = Path(args.file).resolve() if args.file else None
    if not source or not source.exists():
        raise RuntimeError("Informe --file com dump existente para restore.")
    ensure_dump_file(source)
    if (args.confirm_restore or "").strip().upper() != "RESTAURAR":
        raise RuntimeError("Confirmacao invalida. Use --confirm-restore RESTAURAR.")
    cmd = [pg_restore_bin, "--clean", "--if-exists", "--no-owner", "--dbname", db_url, str(source)]
    if args.dry_run:
        print("DRY-RUN:", " ".join(cmd))
        return 0
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "pg_restore falhou")
    print(f"Restore PostgreSQL concluido: {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

