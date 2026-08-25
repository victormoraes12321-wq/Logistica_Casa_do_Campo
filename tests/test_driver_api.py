# -*- coding: utf-8 -*-
import base64
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import app
import app_core.domains.driver_api_dispatch as driver_api
from app_core.domains.driver_api_dispatch import handle_driver_api_request
from app_core.services.driver_security import hash_driver_password, hash_session_token


class DummyHandler:
    def __init__(self, data=None, token=""):
        self._data = data or {}
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.result_data = None
        self.result_status = 200

    def json_data(self):
        return self._data

    def conn(self):
        return app.conn()

    def client_ip(self):
        return "127.0.0.1"

    def send_json(self, data, st=200):
        self.result_data = data
        self.result_status = st
        return True


class DriverApiTests(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        self.orig_db_target = app.DB_TARGET
        self.orig_db_path = app.DB_PATH
        app.DB_PATH = self.db_path
        app.DB_TARGET = app.RuntimeDatabaseTarget(
            backend="sqlite", database_url=f"sqlite:///{self.db_path}", sqlite_path=self.db_path
        )
        app.init_db()
        with driver_api._LOGIN_ATTEMPTS_LOCK:
            driver_api._LOGIN_ATTEMPTS.clear()

    def tearDown(self):
        app.DB_PATH = self.orig_db_path
        app.DB_TARGET = self.orig_db_target
        try:
            os.close(self.db_fd)
        except OSError:
            pass
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _create_driver(self, driver_id, name, password="senha-segura", must_change=0):
        with app.conn() as db:
            db.execute(
                """INSERT OR REPLACE INTO drivers(
                       id,name,active,password_hash,must_change_password,updated_at,version
                   ) VALUES(?,?,1,?,?,?,1)""",
                (driver_id, name, hash_driver_password(password), must_change, "2026-08-25 10:00:00"),
            )
            db.commit()

    def _login(self, driver_id, password="senha-segura"):
        handler = DummyHandler({"driver_id": driver_id, "password": password})
        self.assertTrue(handle_driver_api_request(handler, "/api/v1/driver/login", "POST"))
        self.assertEqual(handler.result_status, 200, handler.result_data)
        self.assertTrue(handler.result_data["ok"])
        return handler.result_data["token"]

    def _create_route_order(self, driver_id, route_id, order_id, route_status="Em rota"):
        with app.conn() as db:
            db.execute("INSERT OR REPLACE INTO vehicles(id,name,plate) VALUES(?,?,?)", (route_id, "Caminhão", f"ABC-{route_id:04d}"))
            db.execute("INSERT OR REPLACE INTO clients(id,name,created_at) VALUES(?,?,?)", (order_id, "Cliente Teste", "2026-08-25 10:00:00"))
            db.execute(
                """INSERT INTO orders(id,order_number,client_id,status,total_value,weight_kg,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (order_id, f"PED-{order_id}", order_id, "Saiu para entrega", 500.0, 100.0, "2026-08-25 10:00:00", "2026-08-25 10:00:00"),
            )
            db.execute(
                "INSERT INTO routes(id,name,driver_id,vehicle_id,status,created_at) VALUES(?,?,?,?,?,?)",
                (route_id, f"Carga {route_id}", driver_id, route_id, route_status, "2026-08-25 10:00:00"),
            )
            db.execute("INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,1,'Em rota')", (route_id, order_id))
            db.execute("INSERT INTO order_items(order_id,product_name,quantity,unit,weight_kg) VALUES(?,?,?,?,?)", (order_id, "Ração", 2, "un", 100.0))
            db.commit()

    def test_public_driver_list_exposes_only_identity_and_registration_is_disabled(self):
        self._create_driver(11, "Motorista Lista")
        list_handler = DummyHandler()
        handle_driver_api_request(list_handler, "/api/v1/driver/all_drivers", "GET")
        selected = next(item for item in list_handler.result_data["drivers"] if item["id"] == 11)
        self.assertEqual(set(selected), {"id", "name"})

        register = DummyHandler({"name": "Cadastro Público"})
        handle_driver_api_request(register, "/api/v1/driver/register", "POST")
        self.assertEqual(register.result_status, 403)
        self.assertEqual(register.result_data["code"], "registration_disabled")

    def test_login_checks_hash_and_forces_first_password_change(self):
        self._create_driver(12, "Motorista Primeiro Acesso", password="123", must_change=1)
        invalid = DummyHandler({"driver_id": 12, "password": "errada"})
        handle_driver_api_request(invalid, "/api/v1/driver/login", "POST")
        self.assertEqual(invalid.result_status, 401)

        token = self._login(12, "123")
        blocked = DummyHandler(token=token)
        handle_driver_api_request(blocked, "/api/v1/driver/routes", "GET")
        self.assertEqual(blocked.result_status, 403)
        self.assertEqual(blocked.result_data["code"], "password_change_required")

        change = DummyHandler({"new_password": "minha nova senha"}, token=token)
        handle_driver_api_request(change, "/api/v1/driver/change_password", "POST")
        self.assertEqual(change.result_status, 200)
        with app.conn() as db:
            row = db.execute("SELECT password_hash,must_change_password FROM drivers WHERE id=12").fetchone()
            self.assertNotEqual(row["password_hash"], "minha nova senha")
            self.assertTrue(row["password_hash"].startswith("pbkdf2_sha256$"))
            self.assertEqual(row["must_change_password"], 0)
        self._login(12, "minha nova senha")

    def test_routes_are_scoped_to_authenticated_driver_without_fallback(self):
        self._create_driver(21, "Motorista Um")
        self._create_driver(22, "Motorista Dois")
        self._create_route_order(21, 211, 2111, route_status="Planejada")
        self._create_route_order(22, 222, 2222, route_status="Planejada")
        token = self._login(21)

        listing = DummyHandler(token=token)
        handle_driver_api_request(listing, "/api/v1/driver/routes", "GET")
        self.assertEqual([row["id"] for row in listing.result_data["routes"]], [211])

        foreign_detail = DummyHandler(token=token)
        handle_driver_api_request(foreign_detail, "/api/v1/driver/route/222", "GET")
        self.assertEqual(foreign_detail.result_status, 404)

        foreign_start = DummyHandler({"route_id": 222}, token=token)
        handle_driver_api_request(foreign_start, "/api/v1/driver/start_route", "POST")
        self.assertEqual(foreign_start.result_status, 403)

    def test_delivery_is_atomic_idempotent_and_auto_settles(self):
        self._create_driver(31, "Motorista Entrega")
        self._create_route_order(31, 311, 3111)
        token = self._login(31)
        photo = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0PHOTO_DATA_BYTES").decode("ascii")
        payload = {
            "idempotency_key": "delivery-3111-v1",
            "order_id": 3111,
            "route_id": 311,
            "delivered_to": "Recebedor",
            "receipt_photo": photo,
            "is_problem": False,
            "latitude": -20.3155,
            "longitude": -40.3128,
        }
        first = DummyHandler(payload, token)
        handle_driver_api_request(first, "/api/v1/driver/deliver", "POST")
        self.assertEqual(first.result_status, 200, first.result_data)
        self.assertTrue(first.result_data["route_auto_settled"])

        replay = DummyHandler(payload, token)
        handle_driver_api_request(replay, "/api/v1/driver/deliver", "POST")
        self.assertEqual(replay.result_status, 200)
        self.assertTrue(replay.result_data["idempotent_replay"])
        with app.conn() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM delivery_receipts WHERE order_id=3111").fetchone()[0], 1)
            rec = db.execute("SELECT latitude,longitude,delivery_location_link FROM delivery_receipts WHERE order_id=3111").fetchone()
            self.assertEqual(rec["latitude"], -20.3155)
            self.assertEqual(rec["longitude"], -40.3128)
            self.assertIn("https://www.google.com/maps?q=-20.3155", rec["delivery_location_link"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM driver_delivery_operations WHERE order_id=3111").fetchone()[0], 1)
            history = db.execute("SELECT old_status,new_status,action FROM order_history WHERE order_id=3111").fetchone()
            self.assertEqual((history["old_status"], history["new_status"]), ("Saiu para entrega", "Acertado"))
            self.assertIn("app", history["action"])
            self.assertEqual(db.execute("SELECT status FROM orders WHERE id=3111").fetchone()[0], "Acertado")
            self.assertEqual(db.execute("SELECT status FROM routes WHERE id=311").fetchone()[0], "Acertada")

    def test_client_disconnect_after_commit_does_not_report_transaction_failure(self):
        self._create_driver(32, "Motorista Conexão")
        self._create_route_order(32, 321, 3211)
        token = self._login(32)
        handler = DummyHandler({
            "idempotency_key": "delivery-disconnect-3211",
            "order_id": 3211,
            "route_id": 321,
            "delivered_to": "Recebedor",
            "receipt_photo": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffphoto").decode("ascii"),
            "is_problem": False,
        }, token)
        handler.send_json = Mock(side_effect=ConnectionAbortedError("cliente encerrou a conexão"))

        self.assertTrue(handle_driver_api_request(handler, "/api/v1/driver/deliver", "POST"))
        self.assertEqual(handler.send_json.call_count, 1)
        with app.conn() as db:
            self.assertEqual(db.execute("SELECT status FROM orders WHERE id=3211").fetchone()[0], "Acertado")
            self.assertEqual(
                db.execute("SELECT status FROM driver_delivery_operations WHERE order_id=3211").fetchone()[0],
                "completed",
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM delivery_receipts WHERE order_id=3211").fetchone()[0], 1)

    def test_problem_uses_aligned_schema_and_marks_route_after_last_stop(self):
        self._create_driver(41, "Motorista Problema")
        self._create_route_order(41, 411, 4111)
        token = self._login(41)
        handler = DummyHandler({
            "idempotency_key": "problem-4111-v1",
            "order_id": 4111,
            "route_id": 411,
            "is_problem": True,
            "problem_type": "Cliente ausente",
            "final_notes": "Portão fechado após duas tentativas.",
        }, token)
        handle_driver_api_request(handler, "/api/v1/driver/deliver", "POST")
        self.assertEqual(handler.result_status, 200, handler.result_data)
        self.assertEqual(handler.result_data["route_status"], "Com problema")
        with app.conn() as db:
            problem = db.execute("SELECT route_id,problem_type,description FROM delivery_problems WHERE order_id=4111").fetchone()
            self.assertEqual(problem["route_id"], 411)
            self.assertIn("Portão fechado", problem["description"])
            history = db.execute("SELECT new_status,notes FROM order_history WHERE order_id=4111").fetchone()
            self.assertEqual(history["new_status"], "Problema")
            self.assertIn("Cliente ausente", history["notes"])
            self.assertEqual(db.execute("SELECT status FROM routes WHERE id=411").fetchone()[0], "Com problema")

    def test_failed_receipt_insert_rolls_back_all_business_state(self):
        self._create_driver(51, "Motorista Rollback")
        self._create_route_order(51, 511, 5111)
        token = self._login(51)
        with app.conn() as db:
            db.execute("DROP TABLE delivery_receipts")
            db.commit()
        handler = DummyHandler({
            "idempotency_key": "rollback-5111-v1",
            "order_id": 5111,
            "route_id": 511,
            "receipt_photo": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffphoto").decode("ascii"),
            "is_problem": False,
        }, token)
        handle_driver_api_request(handler, "/api/v1/driver/deliver", "POST")
        self.assertEqual(handler.result_status, 500)
        with app.conn() as db:
            self.assertEqual(db.execute("SELECT status FROM orders WHERE id=5111").fetchone()[0], "Saiu para entrega")
            self.assertEqual(db.execute("SELECT status FROM route_orders WHERE order_id=5111").fetchone()[0], "Em rota")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM driver_delivery_operations WHERE order_id=5111").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM order_history WHERE order_id=5111").fetchone()[0], 0)

    def test_logout_revokes_only_hashed_token(self):
        self._create_driver(61, "Motorista Logout")
        token = self._login(61)
        logout = DummyHandler(token=token)
        handle_driver_api_request(logout, "/api/v1/driver/logout", "POST")
        self.assertEqual(logout.result_status, 200)
        with app.conn() as db:
            session = db.execute("SELECT token_hash,revoked_at FROM driver_sessions WHERE driver_id=61").fetchone()
            self.assertEqual(session["token_hash"], hash_session_token(token))
            self.assertNotEqual(session["token_hash"], token)
            self.assertTrue(session["revoked_at"])
        after = DummyHandler(token=token)
        handle_driver_api_request(after, "/api/v1/driver/routes", "GET")
        self.assertEqual(after.result_status, 401)

    def test_mutating_and_route_endpoints_require_authentication(self):
        cases = [
            ("/api/v1/driver/routes", "GET", {}),
            ("/api/v1/driver/route/1", "GET", {}),
            ("/api/v1/driver/start_route", "POST", {"route_id": 1}),
            ("/api/v1/driver/deliver", "POST", {"route_id": 1, "order_id": 1}),
            ("/api/v1/driver/change_password", "POST", {"new_password": "senha nova segura"}),
        ]
        for path, method, payload in cases:
            with self.subTest(path=path):
                handler = DummyHandler(payload)
                self.assertTrue(handle_driver_api_request(handler, path, method))
                self.assertEqual(handler.result_status, 401)
                self.assertEqual(handler.result_data["code"], "authentication_required")

    def test_expired_and_tampered_tokens_are_rejected(self):
        self._create_driver(71, "Motorista Sessão")
        token = self._login(71)
        tampered = DummyHandler(token=token + "adulterado")
        handle_driver_api_request(tampered, "/api/v1/driver/routes", "GET")
        self.assertEqual(tampered.result_status, 401)
        self.assertEqual(tampered.result_data["code"], "invalid_session")

        with app.conn() as db:
            db.execute("UPDATE driver_sessions SET expires_at='2000-01-01T00:00:00Z' WHERE driver_id=71")
            db.commit()
        expired = DummyHandler(token=token)
        handle_driver_api_request(expired, "/api/v1/driver/routes", "GET")
        self.assertEqual(expired.result_status, 401)
        self.assertEqual(expired.result_data["code"], "session_expired")

    def test_driver_cannot_deliver_another_drivers_order(self):
        self._create_driver(81, "Motorista A")
        self._create_driver(82, "Motorista B")
        self._create_route_order(82, 822, 8222)
        token = self._login(81)
        handler = DummyHandler({
            "idempotency_key": "foreign-delivery-8222",
            "route_id": 822,
            "order_id": 8222,
            "receipt_photo": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffphoto").decode("ascii"),
        }, token)
        handle_driver_api_request(handler, "/api/v1/driver/deliver", "POST")
        self.assertEqual(handler.result_status, 403)
        self.assertEqual(handler.result_data["code"], "forbidden_route")
        with app.conn() as db:
            self.assertEqual(db.execute("SELECT status FROM orders WHERE id=8222").fetchone()[0], "Saiu para entrega")

    def test_invalid_and_oversized_images_are_rejected_without_state_change(self):
        self._create_driver(91, "Motorista Imagem")
        self._create_route_order(91, 911, 9111)
        token = self._login(91)
        base_payload = {"route_id": 911, "order_id": 9111, "is_problem": False}

        invalid = DummyHandler({
            **base_payload,
            "idempotency_key": "invalid-image",
            "receipt_photo": "data:image/jpeg;base64," + base64.b64encode(b"not-an-image").decode("ascii"),
        }, token)
        handle_driver_api_request(invalid, "/api/v1/driver/deliver", "POST")
        self.assertEqual(invalid.result_status, 400)
        self.assertIn("imagem suportada", invalid.result_data["message"])

        oversized = DummyHandler({
            **base_payload,
            "idempotency_key": "oversized-image",
            "receipt_photo": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xfftoo-large").decode("ascii"),
        }, token)
        with patch.object(driver_api, "MAX_PHOTO_BYTES", 4):
            handle_driver_api_request(oversized, "/api/v1/driver/deliver", "POST")
        self.assertEqual(oversized.result_status, 400)
        self.assertIn("limite", oversized.result_data["message"])
        with app.conn() as db:
            self.assertEqual(db.execute("SELECT status FROM orders WHERE id=9111").fetchone()[0], "Saiu para entrega")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM driver_delivery_operations").fetchone()[0], 0)

    def test_login_rate_limit_and_password_minimum(self):
        self._create_driver(101, "Motorista Limite")
        with patch.dict(os.environ, {"DRIVER_LOGIN_MAX_FAILURES": "2", "DRIVER_LOGIN_LOCK_SECONDS": "30"}):
            for _ in range(2):
                wrong = DummyHandler({"driver_id": 101, "password": "errada"})
                handle_driver_api_request(wrong, "/api/v1/driver/login", "POST")
                self.assertEqual(wrong.result_status, 401)
            blocked = DummyHandler({"driver_id": 101, "password": "senha-segura"})
            handle_driver_api_request(blocked, "/api/v1/driver/login", "POST")
            self.assertEqual(blocked.result_status, 429)
            self.assertEqual(blocked.result_data["code"], "login_rate_limited")

        self._create_driver(102, "Motorista Senha Curta", must_change=1)
        token = self._login(102)
        short = DummyHandler({"new_password": "1234567"}, token)
        handle_driver_api_request(short, "/api/v1/driver/change_password", "POST")
        self.assertEqual(short.result_status, 400)
        with app.conn() as db:
            self.assertEqual(db.execute("SELECT must_change_password FROM drivers WHERE id=102").fetchone()[0], 1)

    def test_receipts_are_scoped_by_route_and_order(self):
        self._create_driver(111, "Motorista Histórico")
        self._create_route_order(111, 1111, 11111)
        with app.conn() as db:
            db.execute("INSERT INTO routes(id,name,driver_id,status,created_at) VALUES(1110,'Carga antiga',111,'Cancelada',?)", (app.now(),))
            db.execute(
                "INSERT INTO delivery_receipts(order_id,route_id,image_data,mime_type,created_at) VALUES(11111,1110,?,'image/jpeg',?)",
                (b"\xff\xd8\xffold", app.now()),
            )
            db.commit()
        token = self._login(111)
        detail = DummyHandler(token=token)
        handle_driver_api_request(detail, "/api/v1/driver/route/1111", "GET")
        self.assertFalse(detail.result_data["route"]["orders"][0]["has_receipt_photo"])

        delivery = DummyHandler({
            "idempotency_key": "scoped-receipt-11111",
            "route_id": 1111,
            "order_id": 11111,
            "receipt_photo": "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffnew").decode("ascii"),
        }, token)
        handle_driver_api_request(delivery, "/api/v1/driver/deliver", "POST")
        self.assertEqual(delivery.result_status, 200, delivery.result_data)
        with app.conn() as db:
            routes = [row[0] for row in db.execute("SELECT route_id FROM delivery_receipts WHERE order_id=11111 ORDER BY route_id")]
            self.assertEqual(routes, [1110, 1111])


if __name__ == "__main__":
    unittest.main()
