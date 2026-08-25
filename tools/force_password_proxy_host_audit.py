# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "logistica_casa_do_campo.sqlite3"
PY = Path(r"C:\Users\wccto11ti1\AppData\Local\Programs\Python\Python312\python.exe")


def legacy_v3_hash(password: str) -> str:
    return hashlib.sha256(("casa_do_campo_local_v3:" + str(password or "")).encode("utf-8")).hexdigest()


def ensure_admin_first_login(db_path: Path) -> None:
    now_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT id FROM users WHERE LOWER(username)=LOWER('admin') LIMIT 1").fetchone()
        pwd_hash = legacy_v3_hash("admin123")
        if row:
            db.execute(
                "UPDATE users SET password_hash=?, active=1, role='GOD', must_change_password=1 WHERE id=?",
                (pwd_hash, int(row["id"])),
            )
        else:
            db.execute(
                "INSERT INTO users(name,username,password_hash,role,active,must_change_password,created_at) VALUES(?,?,?,?,1,1,?)",
                ("Administrador GOD", "admin", pwd_hash, "GOD", now_ts),
            )
        db.commit()


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_up(port: int, timeout: float = 30.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            conn = HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            _ = resp.read()
            conn.close()
            if resp.status == 200:
                return True
        except Exception:
            time.sleep(0.2)
    return False


def cookie_header(cookies: dict[str, str]) -> str:
    if not cookies:
        return ""
    parts = [f"{k}={v}" for k, v in cookies.items()]
    return "; ".join(parts)


def merge_set_cookie(cookies: dict[str, str], response) -> None:
    for key, value in response.getheaders():
        if str(key).lower() != "set-cookie":
            continue
        jar = SimpleCookie()
        try:
            jar.load(value)
        except Exception:
            continue
        for morsel_key, morsel in jar.items():
            cookies[morsel_key] = morsel.value


def request(
    conn: HTTPConnection,
    method: str,
    path: str,
    host_header: str,
    cookies: dict[str, str],
    data: dict[str, str] | None = None,
    origin_base: str = "",
):
    headers = {"Host": host_header}
    body = None
    if data is not None:
        body = urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    ck = cookie_header(cookies)
    if ck:
        headers["Cookie"] = ck
    if origin_base:
        headers["Origin"] = origin_base
        headers["Referer"] = origin_base + ("/login" if path == "/login" else path)
    conn.request(method.upper(), path, body=body, headers=headers)
    resp = conn.getresponse()
    body_text = resp.read().decode("utf-8", errors="ignore")
    merge_set_cookie(cookies, resp)
    return resp.status, body_text, dict(resp.getheaders())


def main() -> int:
    if not SOURCE_DB.exists():
        print("FAIL source db missing")
        return 1

    tmpdir = Path(tempfile.mkdtemp(prefix="proxy_force_pwd_audit_"))
    db_path = tmpdir / "proxy_force_pwd.sqlite3"
    shutil.copy2(SOURCE_DB, db_path)
    ensure_admin_first_login(db_path)

    port = free_port()
    proxy_port = port + 1000
    public_host = f"logisticacasadocampo:{port}"
    origin_base = f"http://{public_host}"

    env = os.environ.copy()
    env["APP_RUNTIME"] = "flask"
    env["APP_HOST"] = "127.0.0.1"
    env["APP_PORT"] = str(port)
    env["LOGISTICA_LEGACY_PROXY_PORT"] = str(proxy_port)
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["LOGISTICA_DB_PATH"] = str(db_path)
    env["LOGISTICA_ALLOWED_HOSTS"] = "logisticacasadocampo,localhost,127.0.0.1"
    env["LOGISTICA_ALLOW_EPHEMERAL_SECRET"] = "1"
    env["SECRET_KEY"] = env.get("SECRET_KEY") or "audit_proxy_force_pwd_secret"

    proc = subprocess.Popen(
        [str(PY), "run.py"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

    try:
        if not wait_up(port):
            print("FAIL server not up")
            return 1

        cookies = {}
        conn = HTTPConnection("127.0.0.1", port, timeout=15)

        c, _, _ = request(conn, "GET", "/login", public_host, cookies, origin_base=origin_base)
        ok_login_page = c == 200
        print(f"PASS login_page code={c}" if ok_login_page else f"FAIL login_page code={c}")
        if not ok_login_page:
            return 1

        c, body, h = request(
            conn,
            "POST",
            "/login",
            public_host,
            cookies,
            data={"username": "admin", "password": "admin123"},
            origin_base=origin_base,
        )
        login_force = c in (200, 302) and (h.get("Location") == "/force-password" or "Troca de senha obrigatória" in body)
        print(f"PASS login_force_password code={c}" if login_force else f"FAIL login_force_password code={c}")
        if not login_force:
            return 1

        csrf = str(cookies.get("csrf_token") or "")
        if not csrf:
            print("FAIL csrf_cookie_missing")
            return 1

        c, _, _ = request(
            conn,
            "POST",
            "/force-password",
            public_host,
            cookies,
            data={
                "new_password": "AdminNovaSenha123",
                "confirm_password": "AdminNovaSenha123",
                "_csrf": csrf,
            },
            origin_base=origin_base,
        )
        ok_change = c in (200, 302)
        print(f"PASS force_password_submit code={c}" if ok_change else f"FAIL force_password_submit code={c}")
        conn.close()
        return 0 if ok_change else 1
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
