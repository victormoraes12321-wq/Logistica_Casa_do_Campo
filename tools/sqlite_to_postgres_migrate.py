"""Migração assistida de dados SQLite para PostgreSQL.

Atenção:
- Não remove dados do banco de origem.
- Cria backup SQLite antes da migração.
- Pode ser executado em modo dry-run para validar volume.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import MetaData, Table, create_engine, select, text

from app_core.config import load_config
from app_core.runtime_db import backup_sqlite_database
from app_core.sqlalchemy_models import Base


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migração segura de SQLite para PostgreSQL.")
    parser.add_argument("--source-sqlite", default="", help="Caminho do banco SQLite de origem. Padrão: runtime atual.")
    parser.add_argument("--target-url", default="", help="URL do PostgreSQL de destino.")
    parser.add_argument("--dry-run", action="store_true", help="Somente valida leitura e contagem, sem escrever no destino.")
    parser.add_argument("--truncate-target", action="store_true", help="Apaga dados atuais no destino antes de inserir.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    source_path = Path(args.source_sqlite or cfg.sqlite_db_path).resolve()
    target_url = (args.target_url or "").strip()
    if not target_url:
        raise SystemExit("Informe --target-url com o PostgreSQL de destino.")
    if not target_url.lower().startswith(("postgresql://", "postgres://")):
        raise SystemExit("O destino deve ser PostgreSQL.")
    if not source_path.is_file():
        raise SystemExit("Arquivo SQLite de origem não encontrado.")

    backup_name = f"pre_sqlite_to_postgres_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.sqlite3"
    backup_path = cfg.backup_dir / backup_name
    backup_sqlite_database(str(source_path), str(backup_path))
    print(f"Backup criado: {backup_path}")

    source_engine = create_engine(f"sqlite:///{source_path.as_posix()}", future=True)
    target_engine = create_engine(target_url, future=True, pool_pre_ping=True)
    Base.metadata.create_all(bind=target_engine, checkfirst=True)

    metadata = MetaData()
    metadata.reflect(bind=source_engine)
    copied = {}

    with source_engine.connect() as src_conn:
        with target_engine.begin() as dst_conn:
            if args.truncate_target and not args.dry_run:
                for table in reversed(Base.metadata.sorted_tables):
                    dst_conn.execute(text(f"TRUNCATE TABLE {table.name} RESTART IDENTITY CASCADE"))
            for table in Base.metadata.sorted_tables:
                src_table = Table(table.name, metadata, autoload_with=source_engine)
                rows = src_conn.execute(select(src_table)).mappings().all()
                copied[table.name] = len(rows)
                if rows and not args.dry_run:
                    dst_conn.execute(table.insert(), [dict(row) for row in rows])

    print("Relatório de migração:")
    for name in sorted(copied.keys()):
        print(f"- {name}: {copied[name]} registro(s)")
    if args.dry_run:
        print("Modo dry-run: nenhuma escrita foi realizada no PostgreSQL.")
    else:
        print("Migração finalizada com sucesso.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
