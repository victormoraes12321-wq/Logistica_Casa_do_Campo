from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app_core.config import load_config


def main() -> int:
    cfg = load_config()
    if cfg.db_backend != "sqlite":
        print("SKIP: auditoria FK local implementada para SQLite.")
        return 0
    db_path = Path(cfg.sqlite_db_path)
    if not db_path.exists():
        print("ERRO: banco SQLite nao encontrado.")
        return 1
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")

    checks = {
        "orders_without_client_ref": "SELECT COUNT(*) c FROM orders o WHERE o.client_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM clients c WHERE c.id=o.client_id)",
        "orders_without_driver_ref": "SELECT COUNT(*) c FROM orders o WHERE o.driver_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM drivers d WHERE d.id=o.driver_id)",
        "orders_without_vehicle_ref": "SELECT COUNT(*) c FROM orders o WHERE o.vehicle_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM vehicles v WHERE v.id=o.vehicle_id)",
        "route_orders_without_route": "SELECT COUNT(*) c FROM route_orders ro WHERE NOT EXISTS (SELECT 1 FROM routes r WHERE r.id=ro.route_id)",
        "route_orders_without_order": "SELECT COUNT(*) c FROM route_orders ro WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.id=ro.order_id)",
        "history_without_order": "SELECT COUNT(*) c FROM order_history h WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.id=h.order_id)",
        "history_without_user": "SELECT COUNT(*) c FROM order_history h WHERE h.user_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id=h.user_id)",
    }

    failed = 0
    for name, sql in checks.items():
        count = int(db.execute(sql).fetchone()["c"])
        print(f"{name}: {count}")
        if count > 0:
            failed += 1
    fk_issues = db.execute("PRAGMA foreign_key_check").fetchall()
    print(f"foreign_key_check_rows: {len(fk_issues)}")
    db.close()
    return 1 if failed or fk_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
