# -*- coding: utf-8 -*-
import json
import base64
import tempfile
import os
import unittest
import app
from app_core.runtime_db import RuntimeDatabaseTarget


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

        class DummyHandler:
            def __init__(self):
                self._data = payload

            def json_data(self):
                return self._data

            def conn(self):
                return app.conn()

            def send_json(self, data, st=200):
                self.result_data = data
                self.result_status = st
                return True

        handler = DummyHandler()
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


if __name__ == "__main__":
    unittest.main()
