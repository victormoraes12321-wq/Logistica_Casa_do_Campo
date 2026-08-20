# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_core.services.backup_service import create_sqlite_backup, restore_sqlite_from_backup

DB_PATH = ROOT / "data" / "logistica_casa_do_campo.sqlite3"
BACKUP_DIR = ROOT / "backups"
OUT_DIR = ROOT / "docs" / "restore_simulacao"
REQUIRED_TABLES = {"users", "orders", "routes", "route_orders", "settings", "audit_logs"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Simulação real de restauração de desastre (SQLite) com tempo de recuperação.")
    p.add_argument("--backup-file", default="", help="Arquivo de backup .sqlite3. Se vazio, usa o mais recente.")
    return p.parse_args()


def pick_backup_file(explicit_name: str = "") -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if explicit_name:
        target = BACKUP_DIR / explicit_name
        if not target.exists():
            raise FileNotFoundError(f"Backup informado não existe: {target}")
        return explicit_name
    candidates = sorted(
        [p for p in BACKUP_DIR.glob("*.sqlite3") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0].name
    return create_sqlite_backup(str(DB_PATH), str(BACKUP_DIR))


def db_integrity(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path, timeout=20) as db:
        row = db.execute("PRAGMA integrity_check").fetchone()
        status = str(row[0] if row else "unknown").strip()
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        missing = REQUIRED_TABLES - tables
    return status, len(missing)


def main() -> int:
    args = parse_args()
    if not DB_PATH.exists():
        raise SystemExit(f"Banco não encontrado: {DB_PATH}")

    backup_name = pick_backup_file(args.backup_file)
    source_backup = BACKUP_DIR / backup_name
    if not source_backup.exists():
        raise SystemExit(f"Backup não encontrado: {source_backup}")

    temp_dir = Path(tempfile.mkdtemp(prefix="restore_disaster_"))
    try:
        temp_db = temp_dir / "simulacao_runtime.sqlite3"
        temp_backup_dir = temp_dir / "backups"
        temp_backup_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(DB_PATH, temp_db)
        shutil.copy2(source_backup, temp_backup_dir / backup_name)

        start = time.perf_counter()
        restored_name, safety_name = restore_sqlite_from_backup(
            str(temp_db),
            str(temp_backup_dir),
            backup_name,
        )
        elapsed = time.perf_counter() - start

        integrity, missing_count = db_integrity(temp_db)
        ok = integrity.lower() == "ok" and missing_count == 0

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report = OUT_DIR / f"simulacao_restore_{stamp}.md"
        report.write_text(
            "\n".join(
                [
                    "# Simulação de Restauração de Desastre",
                    "",
                    f"- Data/hora: **{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}**",
                    f"- Backup testado: **{restored_name}**",
                    f"- Backup de segurança gerado pelo processo: **{safety_name}**",
                    f"- Tempo de recuperação (restore): **{elapsed:.3f} s**",
                    f"- Integridade SQLite: **{integrity}**",
                    f"- Tabelas essenciais ausentes: **{missing_count}**",
                    f"- Resultado: **{'APROVADO' if ok else 'REPROVADO'}**",
                    "",
                    "## Observação",
                    "Teste executado em ambiente isolado temporário, usando o mesmo fluxo técnico de restauração do sistema.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    finally:
        # Em Windows, antivírus/indexador podem manter handle curto; ignore cleanup lock.
        shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"Simulação concluída: {'APROVADO' if ok else 'REPROVADO'}")
    print(f"Backup testado: {backup_name}")
    print(f"Tempo de recuperação: {elapsed:.3f}s")
    print(f"Relatório: {report}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
