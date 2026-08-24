# -*- coding: utf-8 -*-
import json
import base64
import tempfile
import os
import unittest
import app
from app_core.runtime_db import RuntimeDatabaseTarget


class DummyHandler:
    def __init__(self, data=None):
        self._data = data or {}
        self.result_data = None
        self.result_status = 200

    def json_data(self):
        return self._data

    def conn(self):
        return app.conn()

    def send_json(self, data, st=200):
        self.result_data = data
        self.result_status = st
        return True


class DriverApiTests(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp()
        self.orig_db_target = app.DB_TARGET
        self.orig_db_path = app.DB_PATH

        app.DB_PATH = self.db_path
        app.DB_TARGET = app.RuntimeDatabaseTarget(
            backend='sqlite',
            database_url=f'sqlite:///{self.db_path}',
            sqlite_path=self.db_path
        )
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.orig_db_path
        app.DB_TARGET = self.orig_db_target
        try:
            os.close(self.db_fd)
            os.remove(self.db_path)
        except Exception:
            pass

    def test_driver_list_all_and_register(self):
        from app_core.domains.driver_api_dispatch import handle_driver_api_request

        # 1. Cadastra novo motorista via API
        reg_handler = DummyHandler({
            "name": "Motorista Novo Teste",
            "phone": "(33) 99999-8888",
            "document": "12345678900",
            "vehicle_default": "Caminhão 1620"
        })
        handled = handle_driver_api_request(reg_handler, "/api/v1/driver/register", "POST")
        self.assertTrue(handled)
        self.assertTrue(reg_handler.result_data.get("ok"))
        self.assertIn("cadastrado com sucesso", reg_handler.result_data.get("message"))

        # 2. Lista todos os motoristas cadastrados
        list_handler = DummyHandler()
        handled_list = handle_driver_api_request(list_handler, "/api/v1/driver/all_drivers", "GET")
        self.assertTrue(handled_list)
        self.assertTrue(list_handler.result_data.get("ok"))
        drivers = list_handler.result_data.get("drivers", [])
        driver_names = [d["name"] for d in drivers]
        self.assertIn("Motorista Novo Teste", driver_names)

    def test_driver_start_route_and_order_items(self):
        from app_core.domains.driver_api_dispatch import handle_driver_api_request

        with app.conn() as db:
            db.execute("INSERT OR REPLACE INTO drivers(id, name, active) VALUES(88, 'Motorista Rota Teste', 1)")
            db.execute("INSERT OR REPLACE INTO vehicles(id, name, plate) VALUES(88, 'Caminhão 1620', 'XYZ-8888')")
            db.execute("INSERT OR REPLACE INTO clients(id, name, phone, whatsapp, farm_name, created_at) VALUES(88, 'Cliente Fazenda Boa Vista', '(33) 98888-7777', '(33) 98888-7777', 'Fazenda Boa Vista', '2026-08-24 10:00:00')")
            db.execute("""
                INSERT INTO orders(id, order_number, client_id, status, total_value, weight_kg, created_at, updated_at)
                VALUES(888, 'PED-ROTA-88', 88, 'Pendente', 1500.0, 300.0, '2026-08-24 10:00:00', '2026-08-24 10:00:00')
            """)
            db.execute("INSERT INTO order_items(order_id, product_name, quantity, unit, weight_kg) VALUES(888, 'Ração Bovinos 50kg', 6, 'un', 300.0)")
            db.execute("""
                INSERT INTO routes(id, name, driver_id, vehicle_id, status, created_at)
                VALUES(88, 'Rota Teófilo Otoni #88', 88, 88, 'Planejada', '2026-08-24 10:00:00')
            """)
            db.execute("INSERT INTO route_orders(route_id, order_id, delivery_order, status) VALUES(88, 888, 1, 'Pendente')")
            db.commit()

        # 1. Testa marcar saída da carga
        start_handler = DummyHandler({"route_id": 88})
        handled_start = handle_driver_api_request(start_handler, "/api/v1/driver/start_route", "POST")
        self.assertTrue(handled_start)
        self.assertTrue(start_handler.result_data.get("ok"))
        self.assertEqual(start_handler.result_data.get("status"), "Em rota")

        # 2. Testa detalhamento da rota com itens e contatos
        detail_handler = DummyHandler()
        handled_detail = handle_driver_api_request(detail_handler, "/api/v1/driver/route/88", "GET")
        self.assertTrue(handled_detail)
        route_data = detail_handler.result_data.get("route", {})
        self.assertEqual(route_data.get("status"), "Em rota")

        orders = route_data.get("orders", [])
        self.assertEqual(len(orders), 1)
        ord_items = orders[0].get("items", [])
        self.assertEqual(len(ord_items), 1)
        self.assertEqual(ord_items[0]["product_name"], "Ração Bovinos 50kg")
        self.assertEqual(orders[0]["client_phone"], "(33) 98888-7777")

    def test_driver_deliver_saves_photo_to_db_and_auto_settles(self):
        with app.conn() as db:
            # 1. Cria motorista, veículo, cliente, pedido e rota
            db.execute("INSERT OR REPLACE INTO drivers(id, name, active) VALUES(99, 'Motorista Teste API', 1)")
            db.execute("INSERT OR REPLACE INTO vehicles(id, name, plate) VALUES(99, 'Caminhão Teste API', 'ABC-9999')")
            db.execute("INSERT OR REPLACE INTO clients(id, name, created_at) VALUES(99, 'Cliente Teste API', '2026-08-24 10:00:00')")
            db.execute("""
                INSERT INTO orders(id, order_number, client_id, status, total_value, weight_kg, created_at, updated_at)
                VALUES(999, 'PED-TEST-API', 99, 'Saiu para entrega', 500.0, 100.0, '2026-08-24 10:00:00', '2026-08-24 10:00:00')
            """)
            db.execute("""
                INSERT INTO routes(id, name, driver_id, vehicle_id, status, created_at)
                VALUES(99, 'Rota Teste #99', 99, 99, 'Em rota', '2026-08-24 10:00:00')
            """)
            db.execute("INSERT INTO route_orders(route_id, order_id, delivery_order, status) VALUES(99, 999, 1, 'Pendente')")
            db.commit()

        # 2. Simula envio de comprovante (foto em base64) via API do Motorista
        sample_b64 = "data:image/jpeg;base64," + base64.b64encode(b"FAKE_PHOTO_DATA_BYTES").decode("utf-8")
        payload = {
            "order_id": 999,
            "route_id": 99,
            "delivered_to": "Recebedor Teste",
            "receipt_photo": sample_b64,
            "is_problem": False
        }

        handler = DummyHandler(payload)
        from app_core.domains.driver_api_dispatch import handle_driver_api_request
        handled = handle_driver_api_request(handler, "/api/v1/driver/deliver", "POST")

        self.assertTrue(handled)
        self.assertTrue(handler.result_data.get("ok"))
        self.assertTrue(handler.result_data.get("image_saved_in_db"))
        self.assertTrue(handler.result_data.get("route_auto_settled"))

        # 3. Verifica persistência no Banco de Dados
        with app.conn() as db:
            # Foto salva na tabela delivery_receipts
            rec = db.execute("SELECT * FROM delivery_receipts WHERE order_id=999").fetchone()
            self.assertIsNotNone(rec)
            self.assertIn("FAKE_PHOTO_DATA_BYTES", base64.b64decode(rec["image_data"]).decode("utf-8"))

            # Status do pedido atualizado para Acertado
            o_row = db.execute("SELECT status, receipt_photo_at FROM orders WHERE id=999").fetchone()
            self.assertEqual(o_row["status"], "Acertado")
            self.assertIsNotNone(o_row["receipt_photo_at"])

            # Rota finalizada automaticamente (Auto-Acerto)
            r_row = db.execute("SELECT status FROM routes WHERE id=99").fetchone()
            self.assertEqual(r_row["status"], "Acertada")


    def test_driver_change_password(self):
        from app_core.domains.driver_api_dispatch import handle_driver_api_request

        with app.conn() as db:
            try:
                db.execute("ALTER TABLE drivers ADD COLUMN pin TEXT DEFAULT ''")
            except Exception:
                pass
            db.execute("INSERT OR REPLACE INTO drivers(id, name, pin, active) VALUES(77, 'Motorista Troca Senha', '1234', 1)")
            db.commit()

        handler = DummyHandler({
            "driver_name": "Motorista Troca Senha",
            "new_pin": "5678"
        })
        handled = handle_driver_api_request(handler, "/api/v1/driver/change_password", "POST")
        self.assertTrue(handled)
        self.assertTrue(handler.result_data.get("ok"))
        self.assertIn("Senha / PIN alterado com sucesso", handler.result_data.get("message"))

        with app.conn() as db:
            d_row = db.execute("SELECT pin FROM drivers WHERE id=77").fetchone()
            self.assertEqual(d_row["pin"], "5678")


if __name__ == "__main__":
    unittest.main()

