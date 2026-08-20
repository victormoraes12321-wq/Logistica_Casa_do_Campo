# -*- coding: utf-8 -*-
"""Validação de sistema zerado + ciclo mínimo em banco temporário."""

import os
import hashlib
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "logistica_casa_do_campo.sqlite3"
PY = Path(r"C:\Users\wccto11ti1\AppData\Local\Programs\Python\Python312\python.exe")
BAD_PATTERNS = [
    re.compile(r"traceback", re.IGNORECASE),
    re.compile(r"sqlite3?\.(integrityerror|operationalerror|databaseerror|programmingerror|error)", re.IGNORECASE),
    re.compile(r"sqlite\s+error", re.IGNORECASE),
    re.compile(r"integrityerror", re.IGNORECASE),
    re.compile(r"operationalerror", re.IGNORECASE),
    re.compile(r"\bNone\b"),
    re.compile(r"\bNaN\b"),
]
ADMIN_PASSWORD_CANDIDATES = [
    p
    for p in [
        (os.environ.get("LOGISTICA_TEST_ADMIN_PASSWORD") or "").strip(),
        "CasaCampo@2026!",
        "admin123",
        "AdminAudit123",
    ]
    if p
]


def _legacy_v3_hash(password):
    return hashlib.sha256(("casa_do_campo_local_v3:" + str(password or "")).encode("utf-8")).hexdigest()


def ensure_admin_login(db_path, password="admin123"):
    now_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT id FROM users WHERE LOWER(username)=LOWER('admin') LIMIT 1").fetchone()
        hashed = _legacy_v3_hash(password)
        if row:
            db.execute(
                "UPDATE users SET password_hash=?, active=1, must_change_password=0 WHERE id=?",
                (hashed, int(row["id"])),
            )
        else:
            db.execute(
                "INSERT INTO users(name,username,password_hash,role,active,created_at,must_change_password) VALUES(?,?,?,?,?,?,0)",
                ("Administrador", "admin", hashed, "GOD", 1, now_ts),
            )
        db.commit()


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(base, timeout=25):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2):
                return True
        except Exception:
            time.sleep(0.2)
    return False


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def cookie(self, name):
        for c in self.jar:
            if c.name == name:
                return c.value
        return ""

    def request(self, method, path, data=None):
        body = None
        if data is not None:
            payload = dict(data)
            if method.upper() == "POST" and path != "/login" and "_csrf" not in payload:
                token = self.cookie("csrf_token")
                if token:
                    payload["_csrf"] = token
            body = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, method=method.upper())
        try:
            with self.opener.open(req, timeout=12) as r:
                return r.getcode(), r.read().decode("utf-8", errors="ignore"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="ignore"), dict(e.headers)

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, data):
        return self.request("POST", path, data)


def push(results, name, ok, detail=""):
    results.append((name, bool(ok), detail))


def parse_order_id(location):
    if not location or "/orders/" not in location:
        return 0
    tail = location.split("/orders/", 1)[1]
    try:
        return int(tail.split("?", 1)[0].strip("/"))
    except Exception:
        return 0


def has_bad_token(html):
    text = str(html or "")
    return any(p.search(text) for p in BAD_PATTERNS)


def login_admin(client):
    last = (401, "", {})
    for pwd in ADMIN_PASSWORD_CANDIDATES:
        last = client.post("/login", {"username": "admin", "password": pwd})
        if last[0] in (200, 302):
            if "Troca de senha obrigatória" in (last[1] or ""):
                force_pwd = "AdminAudit123"
                c2, b2, h2 = client.post("/force-password", {"new_password": force_pwd, "confirm_password": force_pwd})
                if c2 not in (200, 302):
                    return c2, b2, h2
                return client.get("/dashboard")
            return last
    return last


def main():
    if not SOURCE_DB.exists():
        print("FAIL source db missing")
        return 1

    tmpdir = Path(tempfile.mkdtemp(prefix="zero_state_"))
    db_path = tmpdir / "zero.sqlite3"
    shutil.copy2(SOURCE_DB, db_path)
    ensure_admin_login(db_path, "admin123")

    port = free_port()
    base = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["APP_RUNTIME"] = "legacy"
    env["APP_HOST"] = "127.0.0.1"
    env["APP_PORT"] = str(port)
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["LOGISTICA_DB_PATH"] = str(db_path)
    env["LOGISTICA_PORT"] = str(port)
    env["LOGISTICA_HOST"] = "127.0.0.1"

    proc = subprocess.Popen([str(PY), "app.py"], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    results = []
    try:
        if not wait_up(base):
            print("FAIL server not up")
            return 1

        admin = Client(base)
        c, b, _ = login_admin(admin)
        push(results, "login_admin", c in (200, 302), f"code={c}")

        # Estado zerado.
        for path, key in [
            ("/dashboard", "open_dashboard"),
            ("/orders", "open_orders"),
            ("/routes", "open_routes"),
            ("/load-settlement", "open_load_settlement"),
            ("/relatorios", "open_reports"),
        ]:
            c, html, _ = admin.get(path)
            push(results, key, c == 200, f"code={c}")
            push(results, f"{key}_no_technical_error", not has_bad_token(html), "no traceback/sqlite/None/NaN")

        c, orders_html, _ = admin.get("/orders")
        has_empty_message = "Nenhum pedido encontrado" in orders_html
        has_order_rows = bool(re.search(r"href=['\"]/orders/\d+", orders_html or ""))
        push(
            results,
            "orders_empty_or_listed",
            has_empty_message or has_order_rows,
            "empty state message or existing orders rendered",
        )

        # Ciclo mínimo.
        c, _, _ = admin.post("/route-cities", {"route_name": "Rota Produção 01", "city": "Cidade Produção 01", "uf": "MG", "delivery_order": "1"})
        push(results, "cycle_create_route_city", c in (200, 302), f"code={c}")

        c, _, _ = admin.post("/clients", {"name": "Cliente Produção 01", "phone": "(33) 98888-0001", "city": "Cidade Produção 01", "route_name": "Rota Produção 01"})
        push(results, "cycle_create_client", c in (200, 302), f"code={c}")

        c, _, _ = admin.post("/drivers", {"name": "Motorista Produção 01", "phone": "(33) 98888-0002", "document": "DOC-PROD-01", "vehicle_default": "Veículo Produção 01"})
        push(results, "cycle_create_driver", c in (200, 302), f"code={c}")

        c, _, _ = admin.post("/vehicles", {"name": "Veículo Produção 01", "plate": "PRD0A01", "type": "Caminhão", "capacity": "12000"})
        push(results, "cycle_create_vehicle", c in (200, 302), f"code={c}")

        order_no = f"PROD-CICLO-{int(time.time())}"
        c, _, h = admin.post(
            "/orders/new",
            {
                "order_number": order_no,
                "sale_date": time.strftime("%Y-%m-%d"),
                "payment_method": "Pix",
                "weight_kg": "450",
                "total_value": "2500",
                "city": "Cidade Produção 01",
                "route_name": "Rota Produção 01",
                "client_name": "Cliente Produção 01",
                "delivery_address": "Rua Produção 1",
            },
        )
        push(results, "cycle_create_order", c in (200, 302), f"code={c}")

        oid = parse_order_id(h.get("Location", ""))
        if not oid:
            with sqlite3.connect(db_path) as db:
                row = db.execute("SELECT id FROM orders WHERE order_number=?", (order_no,)).fetchone()
                oid = int(row[0]) if row else 0
        push(results, "cycle_order_id_resolved", oid > 0, f"oid={oid}")

        c, _, _ = admin.post(f"/orders/{oid}/invoice", {"invoice_number": f"NF-{int(time.time())}", "invoiced_at": time.strftime("%Y-%m-%d")})
        push(results, "cycle_invoice_order", c in (200, 302), f"code={c}")

        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            driver = db.execute("SELECT id FROM drivers WHERE name='Motorista Produção 01' ORDER BY id DESC LIMIT 1").fetchone()
            vehicle = db.execute("SELECT id FROM vehicles WHERE plate='PRD0A01' ORDER BY id DESC LIMIT 1").fetchone()
            driver_id = int(driver["id"]) if driver else 0
            vehicle_id = int(vehicle["id"]) if vehicle else 0

        load_name = f"Carga Produção {int(time.time())}"
        c, _, h = admin.post(
            "/routes/new",
            {
                "name": load_name,
                "date": time.strftime("%Y-%m-%d"),
                "route_name": "Rota Produção 01",
                "driver_id": str(driver_id),
                "vehicle_id": str(vehicle_id),
                "capacity": "12000",
                f"order_{oid}": "on",
            },
        )
        push(results, "cycle_create_load", c in (200, 302), f"code={c}")

        rid = 0
        loc = h.get("Location", "")
        if "/routes/" in loc:
            try:
                rid = int(loc.split("/routes/")[1].split("?")[0].strip("/"))
            except Exception:
                rid = 0
        if rid <= 0:
            with sqlite3.connect(db_path) as db:
                row = db.execute("SELECT id FROM routes WHERE name=? ORDER BY id DESC LIMIT 1", (load_name,)).fetchone()
                rid = int(row[0]) if row else 0
        push(results, "cycle_route_id_resolved", rid > 0, f"rid={rid}")

        c, _, _ = admin.post(f"/routes/{rid}/dispatch", {}) if rid > 0 else (404, "", {})
        push(results, "cycle_dispatch_load", c in (200, 302), f"code={c}")

        # Conclui acerto rápido da carga.
        c, html, _ = admin.get(f"/load-settlement?q={urllib.parse.quote('Carga Produção')}")
        push(results, "cycle_open_settlement", c == 200, f"code={c}")

        # Como o nome pode variar por timestamp, consulta diretamente no banco.
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            rr = db.execute("SELECT id,name FROM routes WHERE name=? ORDER BY id DESC LIMIT 1", (load_name,)).fetchone()
            rid2 = int(rr["id"]) if rr else 0
            rname = str(rr["name"]) if rr else ""
            ros = db.execute("SELECT order_id FROM route_orders WHERE route_id=?", (rid2,)).fetchall()
            order_ids = [int(x["order_id"]) for x in ros]

        payload = {"notes": "Acerto final de validação"}
        today = time.strftime("%Y-%m-%d")
        for order_id in order_ids:
            payload[f"ok_{order_id}"] = "on"
            payload[f"result_{order_id}"] = "entregue"
            payload[f"date_{order_id}"] = today
            payload[f"pay_{order_id}"] = "Pix"
            payload[f"obs_{order_id}"] = "Entrega concluída normalmente."

        c, _, _ = admin.post(f"/load-settlement/{rid2}/finish", payload)
        push(results, "cycle_finish_settlement", c in (200, 302), f"code={c}; route={rname}")

        c, _, _ = admin.post("/backup/create", {})
        push(results, "cycle_backup_create", c in (200, 302), f"code={c}")

        failed = [r for r in results if not r[1]]
        for name, ok, detail in results:
            print(("PASS" if ok else "FAIL"), name, detail)
        print("TOTAL", len(results), "FAILED", len(failed))
        return 0 if not failed else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
