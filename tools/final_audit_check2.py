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
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / 'data' / 'logistica_casa_do_campo.sqlite3'
PY = Path(r'C:\Users\wccto11ti1\AppData\Local\Programs\Python\Python312\python.exe')
ADMIN_PASSWORD_CANDIDATES = [
    p
    for p in [
        (os.environ.get("LOGISTICA_TEST_ADMIN_PASSWORD") or "").strip(),
        "CasaCampo@2026!",
        "admin123",
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
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_up(base, timeout=25):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(base + '/login', timeout=2):
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
        return ''

    def request(self, method, path, data=None):
        body = None
        if data is not None:
            payload = dict(data)
            if method.upper() == 'POST' and path != '/login' and '_csrf' not in payload:
                token = self.cookie('csrf_token')
                if token:
                    payload['_csrf'] = token
            data = payload
            body = urllib.parse.urlencode(data).encode('utf-8')
        req = urllib.request.Request(self.base + path, data=body, method=method)
        try:
            with self.opener.open(req, timeout=12) as r:
                return r.getcode(), r.read().decode('utf-8', errors='ignore'), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode('utf-8', errors='ignore'), dict(e.headers)

    def get(self, path):
        return self.request('GET', path)

    def post(self, path, data):
        return self.request('POST', path, data)


def push(results, name, ok, detail=''):
    results.append((name, bool(ok), detail))


def hidden_value(html, name):
    marker = f'name="{name}" value="'
    i = html.find(marker)
    if i < 0:
        return ''
    i += len(marker)
    j = html.find('"', i)
    return html[i:j] if j > i else ''


def login_admin(client):
    last = (401, "", {})
    for pwd in ADMIN_PASSWORD_CANDIDATES:
        last = client.post('/login', {'username': 'admin', 'password': pwd})
        if last[0] in (200, 302):
            if 'Troca de senha obrigatória' in (last[1] or ''):
                force_pwd = 'AdminAudit123'
                c2, b2, h2 = client.post('/force-password', {'new_password': force_pwd, 'confirm_password': force_pwd})
                if c2 not in (200, 302):
                    return c2, b2, h2
                return client.get('/dashboard')
            return last
    return last


def login_user_handling_force_change(client, username, current_password, target_password):
    c, b, h = client.post('/login', {'username': username, 'password': current_password})
    if c not in (200, 302):
        return c, b, h
    if 'Troca de senha obrigatória' in (b or ''):
        c2, b2, h2 = client.post('/force-password', {'new_password': target_password, 'confirm_password': target_password})
        if c2 not in (200, 302):
            return c2, b2, h2
        return client.get('/dashboard')
    return c, b, h


def main():
    if not SOURCE_DB.exists():
        print('FAIL source db missing')
        return 1

    tmpdir = Path(tempfile.mkdtemp(prefix='audit_final_'))
    db_path = tmpdir / 'audit.sqlite3'
    shutil.copy2(SOURCE_DB, db_path)
    ensure_admin_login(db_path, "admin123")

    port = free_port()
    base = f'http://127.0.0.1:{port}'

    env = os.environ.copy()
    env['APP_RUNTIME'] = 'legacy'
    env['APP_HOST'] = '127.0.0.1'
    env['APP_PORT'] = str(port)
    env['DATABASE_URL'] = f"sqlite:///{db_path.as_posix()}"
    env['LOGISTICA_DB_PATH'] = str(db_path)
    env['LOGISTICA_PORT'] = str(port)
    env['LOGISTICA_HOST'] = '127.0.0.1'

    proc = subprocess.Popen([str(PY), 'app.py'], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    results = []
    try:
        if not wait_up(base):
            print('FAIL server not up')
            return 1

        admin = Client(base)
        c, b, _ = login_admin(admin)
        push(results, 'login_admin', c in (200, 302), f'code={c}')

        t = str(int(time.time()))
        c, b, _ = admin.post('/settings/user', {'name': 'Fat User', 'username': f'fat_{t}', 'password': 'Senha1234', 'role': 'Faturamento'})
        push(results, 'create_faturamento_user', c in (200, 302), f'code={c}')
        c, b, _ = admin.post('/settings/user', {'name': 'Exp User', 'username': f'exp_{t}', 'password': 'Senha1234', 'role': 'Expedicao'})
        push(results, 'create_expedicao_user', c in (200, 302), f'code={c}')

        fat = Client(base)
        fat_default_password = f'fat_{t}123'
        c, b, _ = login_user_handling_force_change(fat, f'fat_{t}', fat_default_password, 'Senha1234')
        push(results, 'login_faturamento', c in (200, 302), f'code={c}')
        c, b, _ = fat.get('/faturamento')
        push(results, 'faturamento_can_open', c == 200, f'code={c}')
        c, b, _ = fat.get('/routes/new')
        push(results, 'faturamento_blocked_routes_new', c == 403, f'code={c}')
        c, b, _ = fat.post('/drivers', {'name': 'X'})
        push(results, 'faturamento_blocked_driver_write', c == 403, f'code={c}')

        c, b, _ = admin.post('/route-cities', {'route_name': 'Rota Teste', 'city': 'Cidade Teste', 'uf': 'SP', 'delivery_order': '1'})
        push(results, 'create_route_city', c in (200, 302), f'code={c}')
        c, b, _ = admin.post('/route-cities', {'route_name': 'Rota Teste', 'city': 'Cidade Teste', 'uf': 'SP', 'delivery_order': '1'})
        push(results, 'route_city_duplicate_blocked', c == 400 and 'cadastrada' in b.lower(), f'code={c}')

        c, b, _ = admin.post('/vehicles', {'name': 'Truck QA', 'plate': 'ABC1D23', 'type': 'Truck', 'capacity': '9000'})
        push(results, 'create_vehicle', c in (200, 302), f'code={c}')
        c, b, _ = admin.post('/vehicles', {'name': 'Truck QA2', 'plate': 'ABC1D23', 'type': 'Truck', 'capacity': '9000'})
        push(results, 'vehicle_plate_duplicate_blocked', c == 400 and 'placa duplicada' in b.lower(), f'code={c}')

        c, b, _ = admin.post('/drivers', {'name': 'Driver QA', 'phone': '(11) 99999-0000', 'document': f'DOC{t}', 'vehicle_default': 'Truck'})
        push(results, 'create_driver', c in (200, 302), f'code={c}')

        c, b, _ = admin.post('/clients', {'name': 'Cliente QA', 'phone': '(11) 98888-7777', 'city': 'Cidade Teste', 'route_name': 'Rota Teste'})
        push(results, 'create_client', c in (200, 302), f'code={c}')
        c, b, _ = admin.post('/clients', {'name': 'Cliente QA', 'phone': '(11) 98888-7777', 'city': 'Cidade Teste', 'route_name': 'Rota Teste'})
        push(results, 'client_duplicate_blocked', c == 400 and 'duplicado' in b.lower(), f'code={c}')

        order_no = f'QA-ORDER-{t}'
        c, b, _ = admin.post('/orders/new', {
            'order_number': order_no,
            'sale_date': time.strftime('%Y-%m-%d'),
            'payment_method': 'Pix',
            'weight_kg': '100',
            'total_value': '1200',
            'city': 'Cidade Teste',
            'route_name': 'Rota Teste',
            'client_name': 'Cliente Pedido QA',
            'delivery_address': 'Endereco 1'
        })
        push(results, 'create_order', c in (200, 302), f'code={c}')

        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            oid = int(db.execute('SELECT id FROM orders WHERE order_number=?', (order_no,)).fetchone()['id'])
            driver_id = int(db.execute('SELECT id FROM drivers WHERE active=1 ORDER BY id LIMIT 1').fetchone()['id'])
            vehicle_id = int(db.execute('SELECT id FROM vehicles WHERE active=1 ORDER BY id LIMIT 1').fetchone()['id'])

        c, b, _ = admin.post(f'/orders/{oid}/invoice', {'invoice_number': f'NF{t}', 'invoiced_at': time.strftime('%Y-%m-%d')})
        push(results, 'invoice_once', c in (200, 302), f'code={c}')
        c, b, _ = admin.post(f'/orders/{oid}/invoice', {'invoice_number': f'NF{t}A', 'invoiced_at': time.strftime('%Y-%m-%d')})
        push(results, 'invoice_twice_blocked', c == 400 and 'ja esta faturado' in b.lower().replace('á','a').replace('ã','a'), f'code={c}')

        c, b, _ = admin.post('/routes/new', {'name': 'Carga Sem', 'date': time.strftime('%Y-%m-%d'), 'route_name': 'Rota Teste', 'capacity': '9000', f'order_{oid}': 'on'})
        push(results, 'load_without_resources_blocked', c == 400, f'code={c}')

        c, b, _ = admin.post('/routes/new', {'name': 'Carga QA', 'date': time.strftime('%Y-%m-%d'), 'route_name': 'Rota Teste', 'driver_id': str(driver_id), 'vehicle_id': str(vehicle_id), 'capacity': '9000', f'order_{oid}': 'on'})
        push(results, 'create_load', c in (200, 302), f'code={c}')

        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            rid = int(db.execute("SELECT id FROM routes WHERE name='Carga QA' ORDER BY id DESC LIMIT 1").fetchone()['id'])

        c, b, _ = admin.post(f'/routes/{rid}/cancel', {'reason': 'Audit test'})
        push(results, 'cancel_load', c in (200, 302), f'code={c}')
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            route_status = db.execute('SELECT status FROM routes WHERE id=?', (rid,)).fetchone()['status']
            order_status = db.execute('SELECT status FROM orders WHERE id=?', (oid,)).fetchone()['status']
        push(results, 'cancel_load_status', route_status == 'Cancelada', f'status={route_status}')
        push(results, 'cancel_load_returns_order', order_status == 'Faturado', f'order_status={order_status}')

        c, edit_html, _ = admin.get(f'/orders/{oid}/edit')
        token = hidden_value(edit_html, 'updated_at')
        token_version = hidden_value(edit_html, 'version') or '1'
        time.sleep(1.2)
        c, b, _ = admin.post(f'/orders/{oid}/edit', {
            'updated_at': token,
            'version': token_version,
            'order_number': order_no,
            'status': 'Faturado',
            'sale_date': time.strftime('%Y-%m-%d'),
            'payment_method': 'Pix',
            'weight_kg': '101',
            'total_value': '1200',
            'city': 'Cidade Teste',
            'route_name': 'Rota Teste',
            'client_name': 'Cliente Pedido QA',
            'delivery_address': 'Endereco 1'
        })
        push(results, 'status_change_setup', c in (200, 302), f'code={c}')
        c, b, _ = admin.post(f'/orders/{oid}/edit', {
            'updated_at': token,
            'version': token_version,
            'order_number': order_no,
            'status': 'Faturado',
            'sale_date': time.strftime('%Y-%m-%d'),
            'payment_method': 'Pix',
            'weight_kg': '100',
            'total_value': '1200',
            'city': 'Cidade Teste',
            'route_name': 'Rota Teste',
            'client_name': 'Cliente Pedido QA',
            'delivery_address': 'Endereco 1'
        })
        push(results, 'stale_edit_blocked', c == 409 and 'Conflito de edicao' in b.replace('ç','c').replace('ã','a'), f'code={c}')

        exp = Client(base)
        exp_default_password = f'exp_{t}123'
        c, b, _ = login_user_handling_force_change(exp, f'exp_{t}', exp_default_password, 'Senha1234')
        push(results, 'login_expedicao', c in (200, 302), f'code={c}')
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            exp_id = int(db.execute('SELECT id FROM users WHERE username=?', (f'exp_{t}',)).fetchone()['id'])
        c, b, _ = admin.post(f'/settings/user/{exp_id}/update', {'name': 'Exp User', 'username': f'exp_{t}', 'role': 'Expedicao', 'active': '0', 'password': ''})
        push(results, 'inactivate_user', c in (200, 302), f'code={c}')
        c, b, h = exp.post('/clients', {'name': 'NoCreate'})
        push(results, 'inactive_session_blocked', c in (200, 302) and ('Acesso operacional' in b or '/login' in (h.get('Location') or '')), f'code={c},loc={h.get("Location")}')

        c, b, _ = admin.post('/backup/create', {})
        push(results, 'backup_create', c in (200, 302), f'code={c}')
        backup_files = sorted([x for x in os.listdir(ROOT / 'backups') if x.endswith('.sqlite3')], reverse=True)
        if backup_files:
            c, b, _ = admin.post('/backup/restore', {'backup_file': backup_files[0], 'confirm_text': 'RESTAURAR', 'reason': 'audit'})
            push(results, 'backup_restore', c in (200, 302), f'code={c}')
        else:
            push(results, 'backup_restore', False, 'no file')

        with sqlite3.connect(db_path) as db:
            now = time.strftime('%Y-%m-%d %H:%M:%S')
            day = time.strftime('%Y-%m-%d')
            client_id = int(db.execute('SELECT id FROM clients ORDER BY id LIMIT 1').fetchone()[0])
            seller_id = int(db.execute("SELECT id FROM users WHERE username='admin' LIMIT 1").fetchone()[0])
            for i in range(1000):
                ono = f'BULK-{t}-{i}'
                db.execute('''INSERT OR IGNORE INTO orders(order_number,client_id,seller_id,status,sale_date,expected_delivery_date,payment_method,total_value,weight_kg,delivery_address,route_name,city,created_at,updated_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (ono, client_id, seller_id, 'Venda', day, day, 'Pix', 10.0, 5.0, 'X', 'Rota Teste', 'Cidade Teste', now, now))
            db.commit()
        t0 = time.time()
        c, b, _ = admin.get('/dashboard')
        dt = time.time() - t0
        push(results, 'dashboard_1000_orders', c == 200 and dt < 5.5, f'code={c},time={dt:.2f}s')

        failed = [r for r in results if not r[1]]
        for name, ok, detail in results:
            print(('PASS' if ok else 'FAIL'), name, detail)
        print('TOTAL', len(results), 'FAILED', len(failed))
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass


if __name__ == '__main__':
    raise SystemExit(main())
