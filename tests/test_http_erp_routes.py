# -*- coding: utf-8 -*-
import unittest
import json
import urllib.request
import urllib.error
import http.cookiejar
import threading
import time
import os

import app


class TestHttpErpRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = 8991
        cls.server = app.SafeThreadingHTTPServer(('127.0.0.1', cls.port), app.App)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.5)

        # Prepara CookieJar para autenticação
        cls.cj = http.cookiejar.CookieJar()
        cls.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cls.cj))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        try:
            with app.conn() as db:
                db.execute("DELETE FROM users WHERE id=999")
                db.commit()
        except Exception:
            pass

    def _login_god(self):
        url = f"http://127.0.0.1:{self.port}/login"
        pwd_hash = app.hash_password('123')
        with app.conn() as db:
            db.execute("INSERT OR REPLACE INTO users(id,username,name,role,active,password_hash,created_at) VALUES(999,'test_god','Test GOD','GOD',1,?, '2026-01-01 00:00:00')", (pwd_hash,))
            db.commit()

        data = urllib.parse.urlencode({"username": "test_god", "password": "123"}).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            resp = self.opener.open(req)
            return resp
        except urllib.error.HTTPError as e:
            return e

    def test_e2e_erp_admin_routes(self):
        # 1. Login
        self._login_god()

        base_url = f"http://127.0.0.1:{self.port}"

        try:
            req_admin = urllib.request.Request(f"{base_url}/admin/erp")
            resp_admin = self.opener.open(req_admin)
            html_content = resp_admin.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            print("HTTPError on /admin/erp:", e.code, e.read().decode('utf-8'))
            raise
        self.assertEqual(resp_admin.status, 200)
        self.assertIn("Integração ERP", html_content)
        self.assertIn("btnTestConn", html_content)
        self.assertIn("btnCheckCache", html_content)
        self.assertIn("btnSyncNow", html_content)

        # 3. GET /admin/erp/status -> deve retornar JSON 200
        req_status = urllib.request.Request(f"{base_url}/admin/erp/status")
        resp_status = self.opener.open(req_status)
        self.assertEqual(resp_status.status, 200)
        json_status = json.loads(resp_status.read().decode("utf-8"))
        self.assertIn("running", json_status)
        self.assertIn("progress_pct", json_status)
        self.assertIn("cache_pedidos_count", json_status)

        # 4. POST /admin/erp/test -> deve retornar JSON 200
        req_test = urllib.request.Request(f"{base_url}/admin/erp/test", data=b"", method="POST")
        resp_test = self.opener.open(req_test)
        self.assertEqual(resp_test.status, 200)
        json_test = json.loads(resp_test.read().decode("utf-8"))
        self.assertIn("ok", json_test)

        # 5. POST /admin/erp/sync -> deve retornar JSON 200
        req_sync = urllib.request.Request(f"{base_url}/admin/erp/sync", data=b"", method="POST")
        resp_sync = self.opener.open(req_sync)
        self.assertEqual(resp_sync.status, 200)
        json_sync = json.loads(resp_sync.read().decode("utf-8"))
        self.assertTrue(json_sync.get("ok"))


if __name__ == "__main__":
    unittest.main()
