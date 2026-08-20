# -*- coding: utf-8 -*-
"""Auditoria focada em fluxo de selecao, historico e configuracoes."""

import os
import hashlib
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
        req = urllib.request.Request(self.base + path, data=body, method=method)
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


def is_success(code):
    return code in (200, 302)


def find_route_id(db_path, route_name):
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT id FROM routes WHERE name=? ORDER BY id DESC LIMIT 1",
            (route_name,),
        ).fetchone()
    return int(row["id"]) if row else None


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


def mk_order(admin, order_no, city, route_name):
    c, body, _ = admin.post(
        "/orders/new",
        {
            "order_number": order_no,
            "sale_date": time.strftime("%Y-%m-%d"),
            "payment_method": "Pix",
            "weight_kg": "150",
            "total_value": "1990",
            "city": city,
            "route_name": route_name,
            "client_name": f"Cliente {order_no}",
            "delivery_address": "Endereco teste",
        },
    )
    return c, body


def normalize(s):
    return (
        str(s or "")
        .lower()
        .replace("á", "a")
        .replace("à", "a")
        .replace("â", "a")
        .replace("ã", "a")
        .replace("é", "e")
        .replace("ê", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ô", "o")
        .replace("õ", "o")
        .replace("ú", "u")
    )


def main():
    if not SOURCE_DB.exists():
        print("FAIL source db missing")
        return 1

    tmpdir = Path(tempfile.mkdtemp(prefix="audit_ux_select_"))
    db_path = tmpdir / "audit.sqlite3"
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

        stamp = str(int(time.time()))

        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            driver_id = int(db.execute("SELECT id FROM drivers WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"])
            vehicle_id = int(db.execute("SELECT id FROM vehicles WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"])

        open_order = f"UX-OPEN-{stamp}"
        final_order = f"UX-FINAL-{stamp}"
        other_route_order = f"UX-RB-{stamp}"
        c, body = mk_order(admin, open_order, "Cidade Fluxo A", "Rota Fluxo A")
        push(results, "create_open_order", is_success(c), f"code={c}; msg={body[:140]}")
        c, body = mk_order(admin, final_order, "Cidade Fluxo A", "Rota Fluxo A")
        push(results, "create_final_order", is_success(c), f"code={c}; msg={body[:140]}")
        c, body = mk_order(admin, other_route_order, "Cidade Fluxo B", "Rota Fluxo B")
        push(results, "create_other_route_order", is_success(c), f"code={c}; msg={body[:140]}")

        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            open_id = int(db.execute("SELECT id FROM orders WHERE order_number=?", (open_order,)).fetchone()["id"])
            final_id = int(db.execute("SELECT id FROM orders WHERE order_number=?", (final_order,)).fetchone()["id"])
            rb_id = int(db.execute("SELECT id FROM orders WHERE order_number=?", (other_route_order,)).fetchone()["id"])

        c, body, _ = admin.post(
            f"/orders/{open_id}/invoice",
            {"invoice_number": f"NF-OPEN-{stamp}", "invoiced_at": time.strftime("%Y-%m-%d")},
        )
        push(results, "invoice_open_candidate", is_success(c), f"code={c}; msg={body[:120]}")
        c, body, _ = admin.post(
            f"/orders/{rb_id}/invoice",
            {"invoice_number": f"NF-RB-{stamp}", "invoiced_at": time.strftime("%Y-%m-%d")},
        )
        push(results, "invoice_other_route_candidate", is_success(c), f"code={c}; msg={body[:120]}")
        c, body, _ = admin.post(
            f"/orders/{final_id}/invoice",
            {"invoice_number": f"NF-FIN-{stamp}", "invoiced_at": time.strftime("%Y-%m-%d")},
        )
        push(results, "invoice_final_candidate", is_success(c), f"code={c}; msg={body[:120]}")

        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE orders SET status='Acertado', delivered_at=?, updated_at=? WHERE id=?",
                (time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-%d %H:%M:%S"), final_id),
            )
            db.commit()
        push(results, "mark_final_candidate_as_acertado", True, "status=Acertado")

        c, html_orders, _ = admin.get("/orders")
        push(results, "orders_open_screen", c == 200, f"code={c}")
        open_marker = f"<b class=\"order-no\">{open_order}</b>"
        final_marker = f"<b class=\"order-no\">{final_order}</b>"
        push(
            results,
            "orders_open_hides_final",
            (open_marker in html_orders) and (final_marker not in html_orders),
            "default screen should hide finalized",
        )

        c, html_hist, _ = admin.get("/orders?history=1")
        push(results, "orders_history_screen", c == 200, f"code={c}")
        push(
            results,
            "orders_history_shows_final",
            (final_marker in html_hist) and ("Histórico de finalizados" in html_hist),
            "history view should show finalized",
        )

        c, html_new_order, _ = admin.get("/orders/new")
        push(
            results,
            "order_form_has_client_data_attrs",
            ('data-farm="' in html_new_order)
            and ('id="clientSelect"' in html_new_order)
            and ('id="clientCodeInput"' in html_new_order)
            and ('id="clientSearchResults"' in html_new_order),
            "client search + code attrs",
        )

        c, html_route_new, _ = admin.get("/routes/new")
        push(results, "route_builder_has_filter_hooks", ('id=\'loadRouteSelect\'' in html_route_new) and ('data-route="' in html_route_new), "route filter hooks")

        invalid_name = f"Carga Invalida {stamp}"
        c, body, _ = admin.post(
            "/routes/new",
            {
                "name": invalid_name,
                "date": time.strftime("%Y-%m-%d"),
                "route_name": "Rota Fluxo A",
                "driver_id": str(driver_id),
                "vehicle_id": str(vehicle_id),
                "capacity": "9000",
                f"order_{rb_id}": "on",
            },
        )
        push(results, "route_new_blocks_mismatched_order_route", c == 400, f"code={c}; msg={body[:140]}")

        valid_name = f"Carga Valida {stamp}"
        c, body, _ = admin.post(
            "/routes/new",
            {
                "name": valid_name,
                "date": time.strftime("%Y-%m-%d"),
                "route_name": "Rota Fluxo A",
                "driver_id": str(driver_id),
                "vehicle_id": str(vehicle_id),
                "capacity": "9000",
                f"order_{open_id}": "on",
            },
        )
        push(results, "route_new_valid_create", is_success(c), f"code={c}; msg={body[:140]}")

        rid = find_route_id(db_path, valid_name)
        if rid is None:
            push(results, "valid_route_persisted", False, f"route_not_found name={valid_name}")
        else:
            push(results, "valid_route_persisted", True, f"rid={rid}")
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE routes SET status='Acertada' WHERE id=?", (rid,))
                db.commit()

            c, html_settle, _ = admin.get(f"/load-settlement?q={urllib.parse.quote(valid_name)}")
            push(results, "settlement_closed_has_compact_button", c == 200 and "/print-report" in html_settle, f"code={c}")

            c, html_print, _ = admin.get(f"/load-settlement/{rid}/print-report")
            push(results, "compact_report_route", c == 200 and "Relatório compacto" in html_print, f"code={c}")

        c, html_settings, _ = admin.get("/settings?section=permissions&perm_role=Gestor")
        push(results, "settings_permissions_anchor_links", c == 200 and "#settings-permissions" in html_settings and 'name="redirect_section" value="permissions"' in html_settings, f"code={c}")

        ok_count = sum(1 for _, ok, _ in results if ok)
        fail_count = len(results) - ok_count
        for name, ok, detail in results:
            print(("PASS" if ok else "FAIL"), name, detail)
        print("TOTAL", len(results), "PASS", ok_count, "FAIL", fail_count)
        return 0 if fail_count == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
