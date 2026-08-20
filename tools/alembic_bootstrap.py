"""Bootstrap seguro do versionamento Alembic para banco já existente.

Uso:
    python tools/alembic_bootstrap.py

Comportamento:
- cria backup SQLite antes de qualquer ação (quando runtime for SQLite);
- executa `alembic stamp 0001_baseline_schema` para registrar baseline sem
  tentar recriar tabelas existentes.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app_core.config import load_config
from app_core.runtime_db import backup_sqlite_database


def main() -> int:
    cfg = load_config()
    root = cfg.root_dir
    if cfg.db_backend == "sqlite":
        backup_name = f"pre_alembic_bootstrap_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.sqlite3"
        backup_path = cfg.backup_dir / backup_name
        backup_sqlite_database(cfg.sqlite_db_path, str(backup_path))
        print(f"Backup criado: {backup_path}")
    else:
        print("Runtime atual não é SQLite. Siga o processo de backup do PostgreSQL antes de prosseguir.")

    stamp_cmd = [sys.executable, "-m", "alembic", "stamp", "0001_baseline_schema"]
    print("Executando:", " ".join(stamp_cmd))
    stamp_result = subprocess.run(stamp_cmd, cwd=str(root))
    if int(stamp_result.returncode) != 0:
        return int(stamp_result.returncode)

    upgrade_cmd = [sys.executable, "-m", "alembic", "upgrade", "head"]
    print("Executando:", " ".join(upgrade_cmd))
    upgrade_result = subprocess.run(upgrade_cmd, cwd=str(root))
    return int(upgrade_result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
