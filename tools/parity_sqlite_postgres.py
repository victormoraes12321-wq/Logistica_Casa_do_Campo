from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app_core.config import load_config


TABLES = [
    "users",
    "clients",
    "drivers",
    "vehicles",
    "route_cities",
    "orders",
    "order_items",
    "routes",
    "route_orders",
    "order_history",
    "delivery_problems",
    "audit_logs",
    "settings",
    "role_permissions",
    "user_permissions",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Paridade funcional basica entre SQLite e PostgreSQL.")
    parser.add_argument("--sqlite-url", default="", help="URL sqlite:///... de referencia.")
    parser.add_argument("--postgres-url", default="", help="URL postgresql://... de destino.")
    parser.add_argument("--strict", action="store_true", help="Falha quando houver diferenca de contagem.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    sqlite_url = (args.sqlite_url or f"sqlite:///{Path(cfg.sqlite_db_path).resolve().as_posix()}").strip()
    postgres_url = (args.postgres_url or "").strip()
    if not postgres_url:
        print("SKIP: informe --postgres-url para validar paridade com PostgreSQL.")
        return 0

    sqlite_engine = create_engine(sqlite_url, future=True)
    pg_engine = create_engine(postgres_url, future=True, pool_pre_ping=True)
    sqlite_inspector = inspect(sqlite_engine)
    pg_inspector = inspect(pg_engine)

    s_tables = set(sqlite_inspector.get_table_names())
    p_tables = set(pg_inspector.get_table_names())
    missing_in_pg = sorted(set(TABLES) - p_tables)
    missing_in_sqlite = sorted(set(TABLES) - s_tables)
    if missing_in_pg:
        print("FALHA: tabelas ausentes no PostgreSQL:", ", ".join(missing_in_pg))
        return 1
    if missing_in_sqlite:
        print("FALHA: tabelas ausentes no SQLite:", ", ".join(missing_in_sqlite))
        return 1

    different = []
    with sqlite_engine.connect() as s_conn, pg_engine.connect() as p_conn:
        for table in TABLES:
            s_count = int(s_conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            p_count = int(p_conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            print(f"{table}: sqlite={s_count} postgres={p_count}")
            if s_count != p_count:
                different.append((table, s_count, p_count))

    if different and args.strict:
        print("FALHA: diferencas de contagem detectadas.")
        return 1
    if different:
        print("ATENCAO: diferencas de contagem detectadas (modo nao estrito).")
    else:
        print("OK: paridade de contagem validada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
