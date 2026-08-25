# -*- coding: utf-8 -*-
"""Servidor descartável para auditoria manual/browser do app do motorista."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from app_core.services.driver_security import hash_driver_password


class AuditApp(app.App):
    delivery_delay_seconds = 0.0

    def do_POST(self):
        if urlparse(self.path).path == "/api/v1/driver/deliver" and self.delivery_delay_seconds > 0:
            time.sleep(self.delivery_delay_seconds)
        return super().do_POST()


def seed_database() -> None:
    app.init_db()
    now = app.now()
    with app.conn() as db:
        db.execute("DELETE FROM driver_delivery_operations")
        db.execute("DELETE FROM driver_sessions")
        db.execute("DELETE FROM delivery_receipts")
        db.execute("DELETE FROM delivery_problems")
        db.execute("DELETE FROM route_orders")
        db.execute("DELETE FROM order_items")
        db.execute("DELETE FROM routes")
        db.execute("DELETE FROM orders")
        db.execute("DELETE FROM clients")
        db.execute("DELETE FROM vehicles")
        db.execute("DELETE FROM drivers")
        db.execute(
            "INSERT INTO drivers(id,name,active,password_hash,must_change_password,updated_at,version) VALUES(501,'QA Motorista A',1,?,1,?,1)",
            (hash_driver_password("123"), now),
        )
        db.execute(
            "INSERT INTO drivers(id,name,active,password_hash,must_change_password,updated_at,version) VALUES(502,'QA Motorista B',1,?,0,?,1)",
            (hash_driver_password("senha-motorista-b"), now),
        )
        db.execute("INSERT INTO vehicles(id,name,plate,capacity,active,updated_at) VALUES(501,'Caminhão QA','QA-0501',5000,1,?)", (now,))
        db.execute("INSERT INTO vehicles(id,name,plate,capacity,active,updated_at) VALUES(502,'Caminhão B','QA-0502',5000,1,?)", (now,))
        for client_id in (5101, 5102, 5103, 5201, 5301):
            db.execute(
                "INSERT INTO clients(id,name,farm_name,city,address,phone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (client_id, f"Cliente QA {client_id}", f"Fazenda {client_id}", "Teófilo Otoni", "Rua de Teste, 100", "33999999999", now, now),
            )
            db.execute(
                "INSERT INTO orders(id,order_number,client_id,status,total_value,weight_kg,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (client_id, f"PED-QA-{client_id}", client_id, "Faturado", 100.0, 10.0, now, now),
            )
            db.execute(
                "INSERT INTO order_items(order_id,product_name,quantity,unit,weight_kg) VALUES(?,?,?,?,?)",
                (client_id, "Produto de auditoria", 1, "un", 10.0),
            )
        db.execute("INSERT INTO routes(id,name,driver_id,vehicle_id,status,created_at,updated_at,version) VALUES(510,'Carga QA Fluxo',501,501,'Planejada',?,?,1)", (now, now))
        db.execute("INSERT INTO routes(id,name,driver_id,vehicle_id,status,created_at,updated_at,version) VALUES(520,'Carga QA Acerto',501,501,'Planejada',?,?,1)", (now, now))
        db.execute("INSERT INTO routes(id,name,driver_id,vehicle_id,status,created_at,updated_at,version) VALUES(530,'Carga Motorista B',502,502,'Planejada',?,?,1)", (now, now))
        for sequence, order_id in enumerate((5101, 5102, 5103), start=1):
            db.execute("INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(510,?,?,'Pendente')", (order_id, sequence))
        db.execute("INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(520,5201,1,'Pendente')")
        db.execute("INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(530,5301,1,'Pendente')")
        db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--delivery-delay", type=float, default=0.0)
    args = parser.parse_args()
    db_path = args.db.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    app.DB_PATH = str(db_path)
    app.DB_TARGET = app.RuntimeDatabaseTarget(
        backend="sqlite",
        database_url=f"sqlite:///{db_path.as_posix()}",
        sqlite_path=str(db_path),
    )
    if args.seed:
        seed_database()
    AuditApp.delivery_delay_seconds = max(0.0, args.delivery_delay)
    server = app.SafeThreadingHTTPServer(("127.0.0.1", args.port), AuditApp)
    print(f"DRIVER_AUDIT_READY http://127.0.0.1:{args.port} DB={db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
