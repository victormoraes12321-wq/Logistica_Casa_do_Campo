import os
import hashlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "logistica_casa_do_campo.sqlite3"
ADMIN_PASSWORD_CANDIDATES = [
    p
    for p in [
        (os.environ.get("LOGISTICA_TEST_ADMIN_PASSWORD") or "").strip(),
        "CasaCampo@2026!",
        "admin123",
        "AdminSmoke123",
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_server(base_url, timeout_seconds=25):
    started_at = time.time()
    while time.time() - started_at < timeout_seconds:
        try:
            with urllib.request.urlopen(base_url + "/login", timeout=1.5):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def setup_test_data(db_path):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    today = date.today().isoformat()
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        user = db.execute("SELECT id FROM users WHERE LOWER(username)=LOWER('admin') LIMIT 1").fetchone()
        seller_id = int(user["id"]) if user else None
        client = db.execute("SELECT id,name,city,route_name FROM clients ORDER BY id LIMIT 1").fetchone()
        if not client:
            cur = db.execute(
                "INSERT INTO clients(name,phone,whatsapp,city,farm_name,address,route_name,active,created_at) VALUES(?,?,?,?,?,?,?,1,?)",
                ("Cliente Smoke", "", "", "Cidade Smoke", "Fazenda Smoke", "Endereço Smoke", "Rota Smoke", now),
            )
            client_id = int(cur.lastrowid)
            city = "Cidade Smoke"
            route_name = "Rota Smoke"
        else:
            client_id = int(client["id"])
            city = str(client["city"] or "Cidade Smoke")
            route_name = str(client["route_name"] or "Rota Smoke")

        driver = db.execute("SELECT id FROM drivers WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        if not driver:
            cur = db.execute(
                "INSERT INTO drivers(name,phone,document,vehicle_default,active) VALUES(?,?,?,?,1)",
                ("Motorista Smoke", "", "", ""),
            )
            driver_id = int(cur.lastrowid)
        else:
            driver_id = int(driver["id"])

        vehicle = db.execute("SELECT id FROM vehicles WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        if not vehicle:
            cur = db.execute(
                "INSERT INTO vehicles(name,plate,type,capacity,active) VALUES(?,?,?,?,1)",
                ("Veículo Smoke", "", "Caminhão", "11000"),
            )
            vehicle_id = int(cur.lastrowid)
        else:
            vehicle_id = int(vehicle["id"])

        seed = {
            "client_id": client_id,
            "seller_id": seller_id,
            "driver_id": driver_id,
            "vehicle_id": vehicle_id,
            "city": city,
            "route_name": route_name,
            "today": today,
            "now": now,
        }

        final_order_no = f"SMK-FINAL-{int(time.time())}"
        final_order_id = db.execute(
            """INSERT INTO orders(
                order_number,client_id,seller_id,status,sale_date,expected_delivery_date,payment_method,total_value,weight_kg,
                delivery_address,route_name,city,invoice_number,invoiced_at,driver_id,vehicle_id,delivered_at,final_notes,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                final_order_no,
                client_id,
                seller_id,
                "Acertado",
                today,
                today,
                "Pix",
                100.0,
                10.0,
                "Endereço Smoke",
                route_name,
                city,
                "NF-SMOKE-FINAL",
                today,
                driver_id,
                vehicle_id,
                today,
                "pedido finalizado para teste",
                now,
                now,
            ),
        ).lastrowid

        final_route_name = f"SMK-ROTA-FINAL-{int(time.time())}"
        final_route_id = db.execute(
            """INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (final_route_name, today, driver_id, vehicle_id, "Acertada", route_name, 10.0, 11000.0, "smoke", now),
        ).lastrowid
        db.execute(
            "INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,?,?)",
            (final_route_id, final_order_id, 1, "Entregue"),
        )

        emrota_order_no = f"SMK-EMROTA-{int(time.time())}"
        emrota_order_id = db.execute(
            """INSERT INTO orders(
                order_number,client_id,seller_id,status,sale_date,expected_delivery_date,payment_method,total_value,weight_kg,
                delivery_address,route_name,city,invoice_number,invoiced_at,driver_id,vehicle_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                emrota_order_no,
                client_id,
                seller_id,
                "Saiu para entrega",
                today,
                today,
                "Boleto",
                250.0,
                20.0,
                "Endereço Smoke",
                route_name,
                city,
                "NF-SMOKE-EMROTA",
                today,
                driver_id,
                vehicle_id,
                now,
                now,
            ),
        ).lastrowid

        emrota_route_name = f"SMK-ROTA-EMROTA-{int(time.time())}"
        emrota_route_id = db.execute(
            """INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (emrota_route_name, today, driver_id, vehicle_id, "Em rota", route_name, 20.0, 11000.0, "smoke", now),
        ).lastrowid
        db.execute(
            "INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,?,?)",
            (emrota_route_id, emrota_order_id, 1, "Em rota"),
        )

        db.commit()
        seed["final_order_id"] = int(final_order_id)
        seed["final_route_id"] = int(final_route_id)
        seed["final_route_name"] = final_route_name
        seed["emrota_route_id"] = int(emrota_route_id)
        seed["emrota_route_name"] = emrota_route_name
    return seed


def login_admin(client):
    last = (401, "", {})
    for pwd in ADMIN_PASSWORD_CANDIDATES:
        last = client.post('/login', {'username': 'admin', 'password': pwd})
        if last[0] in (200, 302):
            return last
    return last


def main():
    if not SOURCE_DB.exists():
        print("FAIL banco base não encontrado:", str(SOURCE_DB))
        return 1

    tmpdir = Path(tempfile.mkdtemp(prefix="logistica_smoke_"))
    test_db = tmpdir / "smoke.sqlite3"
    proc = None
    code = 1
    try:
        shutil.copy2(SOURCE_DB, test_db)
        ensure_admin_login(test_db, "admin123")
        seed = setup_test_data(test_db)

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = os.environ.copy()
        env["APP_RUNTIME"] = "legacy"
        env["APP_HOST"] = "127.0.0.1"
        env["APP_PORT"] = str(port)
        env["DATABASE_URL"] = f"sqlite:///{test_db.as_posix()}"
        env["LOGISTICA_DB_PATH"] = str(test_db)
        env["LOGISTICA_PORT"] = str(port)
        env["LOGISTICA_HOST"] = "127.0.0.1"
        env["LOGISTICA_SECURE_COOKIE"] = "0"

        proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

        results = []
        if not wait_server(base):
            print("FAIL servidor não iniciou em até 25s")
            return 1

        cookie_jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))

        def cookie(name):
            for c in cookie_jar:
                if c.name == name:
                    return c.value
            return ''

        def get(path):
            req = urllib.request.Request(base + path, method="GET")
            try:
                with opener.open(req, timeout=10) as response:
                    return response.getcode(), response.read().decode("utf-8", errors="ignore")
            except urllib.error.HTTPError as http_error:
                return http_error.code, http_error.read().decode("utf-8", errors="ignore")

        def post(path, data):
            payload = dict(data)
            if path != "/login" and "_csrf" not in payload:
                token = cookie("csrf_token")
                if token:
                    payload["_csrf"] = token
            encoded = urllib.parse.urlencode(payload).encode("utf-8")
            req = urllib.request.Request(base + path, data=encoded, method="POST")
            try:
                with opener.open(req, timeout=10) as response:
                    return response.getcode(), response.read().decode("utf-8", errors="ignore")
            except urllib.error.HTTPError as http_error:
                return http_error.code, http_error.read().decode("utf-8", errors="ignore")

        login_code, body = 401, ""
        for pwd in ADMIN_PASSWORD_CANDIDATES:
            login_code, body = post("/login", {"username": "admin", "password": pwd})
            if login_code in (200, 302):
                if "Troca de senha obrigatória" in (body or ""):
                    login_code, body = post("/force-password", {"new_password": "AdminSmoke123", "confirm_password": "AdminSmoke123"})
                    if login_code in (200, 302):
                        login_code, body = get("/dashboard")
                break
        results.append(("login_admin", login_code in (200, 302) and "Dashboard" in body))

        pages = ["/dashboard", "/orders", "/orders/new", "/routes", "/load-settlement", "/relatorios"]
        for page in pages:
            page_code, page_body = get(page)
            results.append((f"open_{page}", page_code == 200 and len(page_body) > 1000))

        form_code, order_form = get("/orders/new")
        has_nota_promissoria = "Nota Promissória" in order_form
        has_convenio = ("Convênio" in order_form) or ("Convenio" in order_form)
        idx_dados = order_form.find("Dados do pedido")
        idx_peso = order_form.find("Peso total kg")
        idx_cliente = order_form.find("Cliente e endereço")
        results.append(("order_form_weight_in_step1", form_code == 200 and idx_dados != -1 and idx_peso != -1 and idx_cliente != -1 and idx_dados < idx_peso < idx_cliente and "Faturamento e entrega" not in order_form))
        results.append(("order_form_currency_mask", 'data-mask="currency"' in order_form and 'data-mask="decimal"' in order_form))
        results.append(("payment_nota_promissoria", has_nota_promissoria and not has_convenio))
        has_city_select = '<select name="city"' in order_form
        has_legacy_route_select = '<select name="route_name"' in order_form
        has_locked_route_flow = (
            'id="routeInput"' in order_form
            and 'name="route_name"' in order_form
            and 'id="routeInputHidden"' in order_form
        )
        results.append(("city_and_route_selects", has_city_select and (has_legacy_route_select or has_locked_route_flow)))

        invalid_code, invalid_body = post(
            "/orders/new",
            {
                "order_number": " ",
                "sale_date": "",
                "city": "",
                "route_name": "",
                "payment_method": "",
                "client_name": "",
                "weight_kg": "0",
                "total_value": "R$ abc",
            },
        )
        results.append(("invalid_order_payload_blocked", invalid_code == 400 and "Dados inválidos" in invalid_body))

        pay_code, pay_body = post(
            "/orders/new",
            {
                "order_number": f"SMK-INVALID-PAY-{int(time.time())}",
                "status": "Venda",
                "sale_date": seed["today"],
                "expected_delivery_date": seed["today"],
                "client_id": str(seed["client_id"]),
                "city": seed["city"],
                "route_name": seed["route_name"],
                "payment_method": "Convênio",
                "weight_kg": "10",
                "total_value": "100",
                "seller_id": str(seed["seller_id"] or ""),
            },
        )
        results.append(("invalid_payment_blocked", pay_code == 400 and "Método de pagamento inválido" in pay_body))

        localized_order_no = f"SMK-LOCALE-{int(time.time())}"
        create_code, _ = post(
            "/orders/new",
            {
                "order_number": localized_order_no,
                "status": "Venda",
                "sale_date": seed["today"],
                "expected_delivery_date": seed["today"],
                "client_id": str(seed["client_id"]),
                "city": seed["city"],
                "route_name": seed["route_name"],
                "payment_method": "Pix",
                "weight_kg": "1.234,50",
                "total_value": "R$ 9.876,54",
                "seller_id": str(seed["seller_id"] or ""),
            },
        )
        created_ok = create_code in (200, 302)
        weight_ok = False
        value_ok = False
        if created_ok:
            with sqlite3.connect(test_db) as db:
                db.row_factory = sqlite3.Row
                row = db.execute(
                    "SELECT weight_kg,total_value FROM orders WHERE order_number=? LIMIT 1",
                    (localized_order_no,),
                ).fetchone()
                if row:
                    weight_ok = abs(float(row["weight_kg"] or 0) - 1234.5) < 0.01
                    value_ok = abs(float(row["total_value"] or 0) - 9876.54) < 0.01
        results.append(("locale_numbers_parsed_weight", created_ok and weight_ok))
        results.append(("locale_numbers_parsed_value", created_ok and value_ok))

        status_code, status_body = post(f"/orders/{seed['final_order_id']}/status", {"status": "HACKED", "notes": "x"})
        results.append(("invalid_status_blocked", status_code == 400 and "Status inválido" in status_body))

        reopen_fail_code, _ = post(f"/orders/{seed['final_order_id']}/reopen", {"target_status": "Faturado", "reason": ""})
        results.append(("reopen_order_requires_reason", reopen_fail_code == 400))

        reopen_ok_code, _ = post(
            f"/orders/{seed['final_order_id']}/reopen",
            {"target_status": "Faturado", "reason": "Ajuste operacional de teste"},
        )
        order_reopen_ok = reopen_ok_code in (200, 302)
        with sqlite3.connect(test_db) as db:
            row = db.execute("SELECT status FROM orders WHERE id=?", (seed["final_order_id"],)).fetchone()
            order_reopen_ok = order_reopen_ok and row is not None and row[0] == "Faturado"
        results.append(("reopen_order_success", order_reopen_ok))

        rid = seed["final_route_id"]
        code_seq, _ = post(f"/routes/{rid}/sequence", {"seq_1": "1"})
        code_add, _ = post(f"/routes/{rid}/add", {"order_id": "1"})
        code_rm, _ = post(f"/routes/{rid}/remove/1", {})
        results.append(("final_route_sequence_locked", code_seq == 400))
        results.append(("final_route_add_locked", code_add == 400))
        results.append(("final_route_remove_locked", code_rm == 400))

        readonly_code, readonly_body = get("/load-settlement?q=" + urllib.parse.quote(str(seed["final_route_name"])))
        results.append(("final_route_settlement_readonly", readonly_code == 200 and "Edição bloqueada" in readonly_body))

        route_reopen_code, _ = post(f"/routes/{seed['final_route_id']}/reopen", {"target_status": "Planejada", "reason": "Teste de reabertura"})
        route_reopen_ok = route_reopen_code in (200, 302)
        with sqlite3.connect(test_db) as db:
            row = db.execute("SELECT status FROM routes WHERE id=?", (seed["final_route_id"],)).fetchone()
            route_reopen_ok = route_reopen_ok and row is not None and row[0] == "Planejada"
        results.append(("reopen_route_success", route_reopen_ok))

        settle_code, settle_body = post(f"/load-settlement/{seed['emrota_route_id']}/finish", {})
        results.append(("settlement_requires_checklist", settle_code == 400 and "Acerto incompleto" in settle_body))


        report_code, report_body = get("/relatorios")
        reports_ok = (
            report_code == 200
            and "Relatório por motorista" in report_body
            and "Média venda-entrega" in report_body
            and "Total dias" in report_body
            and "Relatório de entregas" in report_body
            and "Peso" in report_body
        )
        results.append(("reports_complete_sections", reports_ok))

        csv_code, csv_body = get("/relatorios/export")
        csv_ok = (
            csv_code == 200
            and "Método_pagamento" in csv_body
            and "Dias_venda_entrega" in csv_body
        )
        results.append(("csv_has_new_columns", csv_ok))

        failed = [name for name, ok in results if not ok]
        for name, ok in results:
            print(("PASS" if ok else "FAIL"), name)
        if failed:
            print("Falhas:", ", ".join(failed))
            code = 1
        else:
            code = 0
        return code
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except Exception:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
        for _ in range(20):
            try:
                shutil.rmtree(tmpdir)
                break
            except PermissionError:
                time.sleep(0.2)
            except FileNotFoundError:
                break


if __name__ == "__main__":
    raise SystemExit(main())


