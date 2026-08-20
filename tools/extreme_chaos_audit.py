# -*- coding: utf-8 -*-
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "logistica_casa_do_campo.sqlite3"
PY = Path(r"C:\Users\wccto11ti1\AppData\Local\Programs\Python\Python312\python.exe")
ADMIN_PASSWORD_CANDIDATES = [
    p
    for p in [
        (os.environ.get("LOGISTICA_TEST_ADMIN_PASSWORD") or "").strip(),
        "admin123",
        "AdminAudit123",
        "CasaCampo@2026!",
    ]
    if p
]
DISCOVERED_ADMIN_PASSWORD = ""

ORDER_STATUSES = {"Venda", "Faturado", "Saiu para entrega", "Acertado", "Problema", "Cancelado"}
ACTIVE_ROUTE_STATUSES = {"Planejada", "Em rota"}


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


def wait_up(base, timeout=30):
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

    def request(self, method, path, data=None, headers=None, add_csrf=True):
        payload = None
        if data is not None:
            send = dict(data)
            if method.upper() == "POST" and path != "/login" and add_csrf and "_csrf" not in send:
                token = self.cookie("csrf_token")
                if token:
                    send["_csrf"] = token
            payload = urllib.parse.urlencode(send).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=payload, method=method.upper())
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=15) as r:
                return r.getcode(), r.read().decode("utf-8", errors="ignore"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="ignore"), dict(e.headers)
        except Exception:
            return 599, "", {}

    def get(self, path, headers=None):
        return self.request("GET", path, None, headers=headers)

    def post(self, path, data, headers=None, add_csrf=True):
        return self.request("POST", path, data, headers=headers, add_csrf=add_csrf)


def hidden_input(html, name):
    marker = f'name="{name}" value="'
    i = html.find(marker)
    if i < 0:
        return ""
    i += len(marker)
    j = html.find('"', i)
    if j <= i:
        return ""
    return html[i:j]


def push(results, name, ok, detail=""):
    results.append((name, bool(ok), detail))


def parse_order_id_from_location(headers):
    loc = headers.get("Location", "")
    if "/orders/" not in loc:
        return 0
    try:
        return int(loc.split("/orders/")[1].split("?")[0].strip("/"))
    except Exception:
        return 0


def login_admin(client):
    global DISCOVERED_ADMIN_PASSWORD
    candidates = []
    if DISCOVERED_ADMIN_PASSWORD:
        candidates.append(DISCOVERED_ADMIN_PASSWORD)
    candidates.extend([c for c in ADMIN_PASSWORD_CANDIDATES if c not in candidates])
    last = (401, "", {})
    for pwd in candidates:
        last = client.post("/login", {"username": "admin", "password": pwd}, add_csrf=False)
        if last[0] in (200, 302):
            if "Troca de senha obrigatória" in (last[1] or ""):
                force_pwd = "AdminAudit123"
                c2, b2, h2 = client.post("/force-password", {"new_password": force_pwd, "confirm_password": force_pwd})
                if c2 not in (200, 302):
                    return c2, b2, h2
                DISCOVERED_ADMIN_PASSWORD = force_pwd
                return client.get("/dashboard")
            DISCOVERED_ADMIN_PASSWORD = pwd
            return last
    return last


def login_user_default(client, username, final_password="Senha1234"):
    default_password = f"{username}123"
    c, b, h = client.post("/login", {"username": username, "password": default_password}, add_csrf=False)
    if c not in (200, 302):
        # fallback para usuários já alterados anteriormente
        return client.post("/login", {"username": username, "password": final_password}, add_csrf=False)
    if "Troca de senha obrigatória" in (b or ""):
        c2, b2, h2 = client.post("/force-password", {"new_password": final_password, "confirm_password": final_password})
        if c2 not in (200, 302):
            return c2, b2, h2
        return client.get("/dashboard")
    return c, b, h


def create_order(client, order_no, city, route_name):
    code, body, headers = client.post(
        "/orders/new",
        {
            "order_number": order_no,
            "sale_date": time.strftime("%Y-%m-%d"),
            "payment_method": "Pix",
            "weight_kg": "100",
            "total_value": "1000",
            "city": city,
            "route_name": route_name,
            "client_name": "Cliente Caos",
            "delivery_address": "Endereco Caos",
        },
    )
    oid = parse_order_id_from_location(headers)
    if not oid:
        # fallback by opening orders list and searching by order number in db externally
        return code, body, 0
    return code, body, oid


def main():
    if not SOURCE_DB.exists():
        print("FAIL source db missing")
        return 1

    tmpdir = Path(tempfile.mkdtemp(prefix="chaos_audit_"))
    db_path = tmpdir / "chaos.sqlite3"
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

        anon = Client(base)
        c, b, h = anon.get("/healthz")
        push(results, "healthz_ok", c == 200 and '"status": "ok"' in b, f"code={c}")

        c, b, h = anon.get("/orders")
        push(results, "unauth_get_orders_redirect", c in (200, 302) and ("Acesso operacional" in b or "/login" in (h.get("Location") or "")), f"code={c}")

        c, b, h = anon.post("/clients", {"name": "X"}, add_csrf=False)
        push(results, "unauth_post_redirect", c in (200, 302) and ("Acesso operacional" in b or "/login" in (h.get("Location") or "")), f"code={c}")

        # Rate limit brute-force on same IP+username
        brute = Client(base)
        codes = []
        for _ in range(7):
            code, _, _ = brute.post("/login", {"username": "intruso_teste", "password": "errada"}, add_csrf=False)
            codes.append(code)
        push(results, "login_rate_limit_triggered", codes[-1] == 429 and all(c in (401, 429) for c in codes), f"codes={codes}")

        admin = Client(base)
        c, b, _ = login_admin(admin)
        push(results, "login_admin", c in (200, 302), f"code={c}")

        c, b, _ = admin.post("/clients", {"name": "SemCSRF"}, add_csrf=False)
        push(results, "csrf_missing_blocked", c == 403, f"code={c}")

        c, b, _ = admin.post("/clients", {"name": "OriginMalicioso"}, headers={"Origin": "http://evil.local"})
        push(results, "origin_header_blocked", c == 403, f"code={c}")

        # Baseline entities for operational tests
        t = str(int(time.time()))
        c, b, _ = admin.post("/route-cities", {"route_name": "Rota Caos", "city": "Cidade Caos", "uf": "SP", "delivery_order": "1"})
        push(results, "create_route_city", c in (200, 302), f"code={c}")
        c, b, _ = admin.post("/vehicles", {"name": "Veiculo Caos", "plate": f"QAZ{t[-4:]}1", "type": "Truck", "capacity": "9000"})
        push(results, "create_vehicle", c in (200, 302), f"code={c}")
        c, b, _ = admin.post("/drivers", {"name": "Motorista Caos", "phone": "(11) 99999-0000", "document": f"DOC-{t}", "vehicle_default": "Veiculo Caos"})
        push(results, "create_driver", c in (200, 302), f"code={c}")
        c, b, _ = admin.post("/clients", {"name": "Cliente Caos", "phone": "(11) 98888-7777", "city": "Cidade Caos", "route_name": "Rota Caos"})
        push(results, "create_client", c in (200, 302), f"code={c}")

        c, b, _ = admin.post("/clients", {"name": "Cliente Telefone Inv", "phone": "1234", "city": "Cidade Caos", "route_name": "Rota Caos"})
        push(results, "invalid_phone_blocked", c == 400, f"code={c}")

        c, b, _ = admin.post("/vehicles", {"name": "Veiculo Inv", "plate": f"INV{t[-4:]}2", "type": "Truck", "capacity": "-10"})
        push(results, "invalid_vehicle_capacity_blocked", c == 400, f"code={c}")

        c, b, _ = admin.post(
            "/orders/new",
            {
                "order_number": f"ORD-NEG-{t}",
                "sale_date": time.strftime("%Y-%m-%d"),
                "payment_method": "Pix",
                "weight_kg": "-1",
                "total_value": "1000",
                "city": "Cidade Caos",
                "route_name": "Rota Caos",
                "client_name": "Cliente Caos",
                "delivery_address": "X",
            },
        )
        push(results, "negative_weight_blocked", c == 400, f"code={c}")

        # Create order for concurrency invoice race
        order_no = f"ORD-RACE-{t}"
        c, b, oid = create_order(admin, order_no, "Cidade Caos", "Rota Caos")
        if oid <= 0:
            with sqlite3.connect(db_path) as db:
                row = db.execute("SELECT id FROM orders WHERE order_number=?", (order_no,)).fetchone()
                oid = int(row[0]) if row else 0
        push(results, "create_order_for_race", c in (200, 302) and oid > 0, f"code={c},oid={oid}")

        def invoice_worker(ix):
            cl = Client(base)
            login_admin(cl)
            code, body, _ = cl.post(f"/orders/{oid}/invoice", {"invoice_number": f"NF-RACE-{t}-{ix}", "invoiced_at": time.strftime("%Y-%m-%d")})
            if "Acesso operacional" in (body or ""):
                return 401
            return code

        invoice_codes = []
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(invoice_worker, i) for i in range(5)]
            for f in as_completed(futs):
                invoice_codes.append(f.result())
        with sqlite3.connect(db_path) as db:
            billed_hist = int(db.execute("SELECT COUNT(*) FROM order_history WHERE order_id=? AND action='Pedido faturado'", (oid,)).fetchone()[0])
        push(
            results,
            "concurrent_invoice_single_success",
            billed_hist == 1 and all(x in (200, 302, 400, 401, 409, 599) for x in invoice_codes),
            f"codes={sorted(invoice_codes)},hist={billed_hist}",
        )

        # Permission tests with Consulta
        consulta_user = f"consulta_{t}"
        c, b, _ = admin.post("/settings/user", {"name": "Consulta Caos", "username": consulta_user, "password": "Senha1234", "role": "Consulta"})
        push(results, "create_consulta_user", c in (200, 302), f"code={c}")
        consulta = Client(base)
        c, b, _ = login_user_default(consulta, consulta_user, "Senha1234")
        push(results, "login_consulta", c in (200, 302), f"code={c}")
        c, b, _ = consulta.get("/orders/new")
        push(results, "consulta_cannot_open_order_new", c == 403, f"code={c}")
        c, b, _ = consulta.post("/orders/new", {"order_number": "X"})
        push(results, "consulta_cannot_post_order_new", c == 403, f"code={c}")

        # Create multiple users and login all at once
        users = []
        for i in range(15):
            uname = f"op_{t}_{i}"
            users.append(uname)
            admin.post("/settings/user", {"name": f"Operador {i}", "username": uname, "password": "Senha1234", "role": "Operador"})

        def login_and_dashboard(uname):
            cuser = Client(base)
            code_login, _, _ = login_user_default(cuser, uname, "Senha1234")
            code_dash, _, _ = cuser.get("/dashboard")
            return code_login, code_dash

        login_results = []
        with ThreadPoolExecutor(max_workers=15) as ex:
            futs = [ex.submit(login_and_dashboard, u) for u in users]
            for f in as_completed(futs):
                login_results.append(f.result())
        ok_mass_login = sum(1 for a, b in login_results if a in (200, 302) and b == 200)
        push(results, "mass_login_dashboard", ok_mass_login >= 13, f"ok={ok_mass_login}/15")

        # Cross-route sequence tamper should be blocked
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            today = time.strftime("%Y-%m-%d")
            nowtxt = time.strftime("%Y-%m-%d %H:%M:%S")
            client_id = int(db.execute("SELECT id FROM clients ORDER BY id LIMIT 1").fetchone()["id"])
            seller_id = int(db.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()["id"])
            driver_id = int(db.execute("SELECT id FROM drivers WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"])
            vehicle_id = int(db.execute("SELECT id FROM vehicles WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"])
            db.execute("""INSERT INTO orders(order_number,client_id,seller_id,status,sale_date,expected_delivery_date,payment_method,total_value,weight_kg,delivery_address,route_name,city,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (f"XSEQA-{t}", client_id, seller_id, "Faturado", today, today, "Pix", 100, 10, "A", "Rota Caos", "Cidade Caos", nowtxt, nowtxt))
            o1 = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute("""INSERT INTO orders(order_number,client_id,seller_id,status,sale_date,expected_delivery_date,payment_method,total_value,weight_kg,delivery_address,route_name,city,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (f"XSEQB-{t}", client_id, seller_id, "Faturado", today, today, "Pix", 100, 10, "B", "Rota Caos", "Cidade Caos", nowtxt, nowtxt))
            o2 = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute("INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (f"RSEQ1-{t}", today, driver_id, vehicle_id, "Planejada", "Rota Caos", 10, 9000, "", nowtxt))
            r1 = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute("INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (f"RSEQ2-{t}", today, driver_id, vehicle_id, "Planejada", "Rota Caos", 10, 9000, "", nowtxt))
            r2 = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute("INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,?,?)", (r1, o1, 1, "Pendente"))
            ro1 = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute("INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,?,?)", (r2, o2, 1, "Pendente"))
            ro2 = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.commit()
        c, b, _ = admin.post(f"/routes/{r1}/sequence", {f"seq_{ro2}": "99"})
        with sqlite3.connect(db_path) as db:
            seq_after = int(db.execute("SELECT delivery_order FROM route_orders WHERE id=?", (ro2,)).fetchone()[0])
        push(results, "cross_route_sequence_tamper_blocked", c == 400 and seq_after == 1, f"code={c},seq={seq_after}")

        # Same order added to two active routes at same time
        order_same = f"ORD-SAME-{t}"
        c, b, oid_same = create_order(admin, order_same, "Cidade Caos", "Rota Caos")
        with sqlite3.connect(db_path) as db:
            if oid_same <= 0:
                row = db.execute("SELECT id FROM orders WHERE order_number=?", (order_same,)).fetchone()
                oid_same = int(row[0]) if row else 0
        admin.post(f"/orders/{oid_same}/invoice", {"invoice_number": f"NF-SAME-{t}", "invoiced_at": time.strftime("%Y-%m-%d")})

        # two routes for race
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            today = time.strftime("%Y-%m-%d")
            nowtxt = time.strftime("%Y-%m-%d %H:%M:%S")
            driver_id = int(db.execute("SELECT id FROM drivers WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"])
            vehicle_id = int(db.execute("SELECT id FROM vehicles WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"])
            db.execute("INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (f"RACEA-{t}", today, driver_id, vehicle_id, "Planejada", "Rota Caos", 0, 9000, "", nowtxt))
            rida = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.execute("INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (f"RACEB-{t}", today, driver_id, vehicle_id, "Planejada", "Rota Caos", 0, 9000, "", nowtxt))
            ridb = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.commit()

        def add_to_route(route_id):
            cuser = Client(base)
            login_admin(cuser)
            code, _, _ = cuser.post(f"/routes/{route_id}/add", {"order_id": str(oid_same)})
            return code

        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(add_to_route, rida), ex.submit(add_to_route, ridb)]
            add_codes = [f.result() for f in as_completed(futs)]
        with sqlite3.connect(db_path) as db:
            linked = db.execute("""SELECT COUNT(*) FROM route_orders ro
                                   JOIN routes r ON r.id=ro.route_id
                                   WHERE ro.order_id=? AND r.status IN ('Planejada','Em rota')""", (oid_same,)).fetchone()[0]
        push(results, "single_active_load_link_after_race", int(linked) <= 1, f"codes={add_codes},links={linked}")

        # Capacity protection when adding order to route
        ord_heavy = f"ORD-HEAVY-{t}"
        c, b, oid_heavy = create_order(admin, ord_heavy, "Cidade Caos", "Rota Caos")
        if oid_heavy <= 0:
            with sqlite3.connect(db_path) as db:
                row = db.execute("SELECT id FROM orders WHERE order_number=?", (ord_heavy,)).fetchone()
                oid_heavy = int(row[0]) if row else 0
        admin.post(f"/orders/{oid_heavy}/edit", {
            "order_number": ord_heavy,
            "status": "Venda",
            "sale_date": time.strftime("%Y-%m-%d"),
            "payment_method": "Pix",
            "weight_kg": "6000",
            "total_value": "1000",
            "city": "Cidade Caos",
            "route_name": "Rota Caos",
            "client_name": "Cliente Caos",
            "delivery_address": "Endereco pesado",
            "updated_at": "",
        })
        admin.post(f"/orders/{oid_heavy}/invoice", {"invoice_number": f"NF-HEAVY-{t}", "invoiced_at": time.strftime("%Y-%m-%d")})
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            today = time.strftime("%Y-%m-%d")
            nowtxt = time.strftime("%Y-%m-%d %H:%M:%S")
            driver_id = int(db.execute("SELECT id FROM drivers WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"])
            vehicle_id = int(db.execute("SELECT id FROM vehicles WHERE active=1 ORDER BY id LIMIT 1").fetchone()["id"])
            db.execute("INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (f"TINY-{t}", today, driver_id, vehicle_id, "Planejada", "Rota Caos", 0, 1000, "", nowtxt))
            tiny_route = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
            db.commit()
        c, b, _ = admin.post(f"/routes/{tiny_route}/add", {"order_id": str(oid_heavy)})
        push(results, "route_add_capacity_blocked", c == 400 and "Capacidade" in b, f"code={c}")

        # Inactivate logged-in user should block next action
        exp_user = f"exp_x_{t}"
        admin.post("/settings/user", {"name": "Exp X", "username": exp_user, "password": "Senha1234", "role": "Expedicao"})
        exp = Client(base)
        login_user_default(exp, exp_user, "Senha1234")
        with sqlite3.connect(db_path) as db:
            exp_id = int(db.execute("SELECT id FROM users WHERE username=?", (exp_user,)).fetchone()[0])
        admin.post(f"/settings/user/{exp_id}/update", {"name": "Exp X", "username": exp_user, "role": "Expedicao", "active": "0", "password": ""})
        c, b, h = exp.post("/clients", {"name": "NaoPode"})
        push(results, "inactive_user_session_blocked", c in (200, 302, 403) and ("Acesso operacional" in b or "/login" in (h.get("Location") or "")), f"code={c}")

        # Backup restore invalid attempts
        c, b, _ = admin.post("/backup/restore", {"backup_file": "../hack.sqlite3", "confirm_text": "RESTAURAR", "reason": "x"})
        push(results, "backup_restore_path_traversal_blocked", c == 400, f"code={c}")
        c, b, _ = admin.post("/backup/restore", {"backup_file": "nao_existe.sqlite3", "confirm_text": "ERRADO", "reason": "x"})
        push(results, "backup_restore_wrong_confirm_blocked", c == 400, f"code={c}")

        # Heavy mixed write load
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            city = str(db.execute("SELECT city FROM clients WHERE city IS NOT NULL AND TRIM(city)<>'' ORDER BY id LIMIT 1").fetchone()["city"])
            route_name = str(db.execute("SELECT route_name FROM clients WHERE route_name IS NOT NULL AND TRIM(route_name)<>'' ORDER BY id LIMIT 1").fetchone()["route_name"])

        def create_orders_worker(worker_idx):
            cuser = Client(base)
            login_admin(cuser)
            ok = 0
            blocked = 0
            for i in range(35):
                ono = f"EXT-{worker_idx}-{i}-{int(time.time() * 1000) % 1000000}"
                code, _, _ = cuser.post(
                    "/orders/new",
                    {
                        "order_number": ono,
                        "sale_date": time.strftime("%Y-%m-%d"),
                        "payment_method": "Pix",
                        "weight_kg": "12",
                        "total_value": "150",
                        "city": city,
                        "route_name": route_name,
                        "client_name": "Cliente Carga",
                        "delivery_address": "Endereco X",
                    },
                )
                if code in (200, 302):
                    ok += 1
                elif code in (400, 409):
                    blocked += 1
            return ok, blocked

        start_mass = time.time()
        mass = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(create_orders_worker, i) for i in range(8)]
            for f in as_completed(futs):
                mass.append(f.result())
        duration_mass = time.time() - start_mass
        total_ok = sum(x[0] for x in mass)
        total_block = sum(x[1] for x in mass)
        push(results, "mass_write_load_stable", total_ok >= 200 and duration_mass < 45, f"ok={total_ok},blocked={total_block},sec={duration_mass:.2f}")

        # Heavy concurrent reads
        def read_dash():
            cuser = Client(base)
            login_admin(cuser)
            t0 = time.time()
            code, _, _ = cuser.get("/dashboard")
            return code, time.time() - t0

        reads = []
        with ThreadPoolExecutor(max_workers=25) as ex:
            futs = [ex.submit(read_dash) for _ in range(40)]
            for f in as_completed(futs):
                reads.append(f.result())
        read_ok = sum(1 for ccode, _ in reads if ccode == 200)
        worst = max((dt for _, dt in reads), default=999)
        push(results, "concurrent_dashboard_reads", read_ok >= 35 and worst < 6.5, f"ok={read_ok}/40,worst={worst:.2f}s")

        # Database invariants
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            bad_status = db.execute("SELECT COUNT(*) c FROM orders WHERE status NOT IN ('Venda','Faturado','Saiu para entrega','Agendado','Acertado','Problema','Cancelado')").fetchone()["c"]
            neg_values = db.execute("SELECT COUNT(*) c FROM orders WHERE COALESCE(weight_kg,0) < 0 OR COALESCE(total_value,0) < 0").fetchone()["c"]
            multi_active = db.execute(
                """SELECT COUNT(*) c FROM (
                       SELECT ro.order_id, COUNT(*) n
                       FROM route_orders ro
                       JOIN routes r ON r.id=ro.route_id
                       WHERE r.status IN ('Planejada','Em rota')
                       GROUP BY ro.order_id
                       HAVING COUNT(*) > 1
                   ) x"""
            ).fetchone()["c"]
            active_god = db.execute("SELECT COUNT(*) c FROM users WHERE role='GOD' AND active=1").fetchone()["c"]
            dup_plate = db.execute(
                """SELECT COUNT(*) c FROM (
                      SELECT UPPER(TRIM(COALESCE(plate,''))) p, COUNT(*) n
                      FROM vehicles
                      WHERE active=1 AND TRIM(COALESCE(plate,''))<>''
                      GROUP BY UPPER(TRIM(COALESCE(plate,'')))
                      HAVING COUNT(*) > 1
                   ) d"""
            ).fetchone()["c"]
            dup_route_city = db.execute(
                """SELECT COUNT(*) c FROM (
                      SELECT LOWER(TRIM(COALESCE(route_name,''))) rk, LOWER(TRIM(COALESCE(city,''))) ck, COUNT(*) n
                      FROM route_cities
                      WHERE active=1
                      GROUP BY LOWER(TRIM(COALESCE(route_name,''))), LOWER(TRIM(COALESCE(city,'')))
                      HAVING COUNT(*) > 1
                   ) d"""
            ).fetchone()["c"]

        push(results, "invariant_valid_status", int(bad_status) == 0, f"count={bad_status}")
        push(results, "invariant_non_negative_values", int(neg_values) == 0, f"count={neg_values}")
        push(results, "invariant_single_active_load_per_order", int(multi_active) == 0, f"count={multi_active}")
        push(results, "invariant_active_god_exists", int(active_god) >= 1, f"count={active_god}")
        push(results, "invariant_no_duplicate_active_plate", int(dup_plate) == 0, f"count={dup_plate}")
        push(results, "invariant_no_duplicate_active_route_city", int(dup_route_city) == 0, f"count={dup_route_city}")

        failed = [r for r in results if not r[1]]
        for name, ok, detail in results:
            print(("PASS" if ok else "FAIL"), name, detail)
        print("TOTAL", len(results), "FAILED", len(failed))
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except Exception:
            proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
