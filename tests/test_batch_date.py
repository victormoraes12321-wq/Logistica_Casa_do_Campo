from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import datetime

import app
from app_core.runtime_db import RuntimeDatabaseTarget
from app_core.sqlalchemy_models import Base
from sqlalchemy import create_engine


class MockApp(app.App):
    def __init__(self, post_data_dict=None, session_data_tuple=None):
        self._post_data = post_data_dict or {}
        self._session_data = session_data_tuple or (None, None)
        self.response_status = None
        self.response_headers = []
        self.response_body = b""

    def post_data(self):
        return self._post_data

    def session_data(self):
        return self._session_data

    def send_response(self, status):
        self.response_status = status

    def _common_headers(self):
        pass

    def send_header(self, keyword, value):
        self.response_headers.append((keyword, value))

    def end_headers(self):
        pass

    def wfile_write(self, data):
        self.response_body += data

    @property
    def wfile(self):
        class MockWfile:
            def __init__(self, outer):
                self._outer = outer
            def write(self, data):
                self._outer.wfile_write(data)
        return MockWfile(self)

    def fail(self, u, title, message, status=400):
        self.response_status = status
        self.response_body = str(message).encode('utf-8')
        return True

    def redirect(self, path):
        self.response_status = 302
        self.response_headers.append(('Location', path))


class BatchDateTests(unittest.TestCase):
    def setUp(self):
        # Criar banco de dados temporário
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_logistica.db"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)

        # Configurar conexão direta de teste
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON;")

        # Injetar banco de testes no app.py
        self.old_db_target = app.DB_TARGET
        app.DB_TARGET = RuntimeDatabaseTarget(
            backend="sqlite",
            database_url=f"sqlite:///{self.db_path}",
            sqlite_path=str(self.db_path)
        )

        # Inserir dados básicos de teste (Usuário, Carga, Pedidos)
        self.db.execute(
            "INSERT INTO users (id, name, username, password_hash, role, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "Test User", "testuser", "pbkdf2:sha256:...", "Admin", 1, "2026-01-01 00:00:00")
        )
        self.db.execute(
            "INSERT INTO routes (id, name, date, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (10, "CG-TEST-001", "2026-06-30", "Em rota", "2026-06-30 08:00:00")
        )
        self.db.execute(
            "INSERT INTO routes (id, name, date, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (11, "CG-TEST-PLAN", "2026-06-30", "Planejada", "2026-06-30 08:00:00")
        )
        self.db.execute(
            "INSERT INTO orders (id, order_number, status, sale_date, delivered_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (100, "PED-100", "Saiu para entrega", "2026-06-28", "2026-06-30", "2026-06-28 10:00:00", "2026-06-30 08:00:00")
        )
        self.db.execute(
            "INSERT INTO orders (id, order_number, status, sale_date, delivered_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (101, "PED-101", "Saiu para entrega", "2026-06-29", None, "2026-06-29 10:00:00", "2026-06-30 08:00:00")
        )
        self.db.execute(
            "INSERT INTO orders (id, order_number, status, sale_date, delivered_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (102, "PED-102", "Venda", "2026-06-29", None, "2026-06-29 10:00:00", "2026-06-29 10:00:00")
        )
        self.db.execute("INSERT INTO route_orders (route_id, order_id, delivery_order) VALUES (?, ?, ?)", (10, 100, 1))
        self.db.execute("INSERT INTO route_orders (route_id, order_id, delivery_order) VALUES (?, ?, ?)", (10, 101, 2))
        self.db.execute("INSERT INTO route_orders (route_id, order_id, delivery_order) VALUES (?, ?, ?)", (11, 102, 1))
        self.db.commit()

        self.user = {"id": 1, "name": "Test User", "username": "testuser", "role": "Admin"}
        self.session = {"uid": 1, "csrf": "my_csrf_token", "exp": datetime.now().timestamp() + 3600}

    def tearDown(self):
        # Restaurar banco original
        app.DB_TARGET = self.old_db_target
        self.db.close()
        # Remover diretório temporário
        try:
            import shutil
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass

    def test_batch_date_success(self):
        """Testa a atualização com sucesso de múltiplos pedidos de uma carga em rota."""
        post_data = {
            "date": "2026-07-01",
            "order_ids": "100,101",
            "_csrf": "my_csrf_token"
        }
        handler = MockApp(post_data_dict=post_data, session_data_tuple=("sid123", self.session))
        handler.post_load_settlement_set_date(self.user, 10)

        # Verificar se retornou 200 OK
        self.assertEqual(handler.response_status, 200)
        self.assertEqual(handler.response_body, b"OK")
        
        # Verificar no banco de dados se as datas foram atualizadas
        o100 = self.db.execute("SELECT * FROM orders WHERE id=100").fetchone()
        o101 = self.db.execute("SELECT * FROM orders WHERE id=101").fetchone()
        self.assertEqual(o100["delivered_at"], "2026-07-01")
        self.assertEqual(o101["delivered_at"], "2026-07-01")

        # Verificar histórico do pedido 100
        h100 = self.db.execute("SELECT * FROM order_history WHERE order_id=100 ORDER BY id DESC").fetchone()
        self.assertIsNotNone(h100)
        self.assertEqual(h100["action"], "Alteração de data em lote")
        self.assertIn("2026-06-30", h100["notes"])
        self.assertIn("2026-07-01", h100["notes"])

        # Verificar histórico do pedido 101
        h101 = self.db.execute("SELECT * FROM order_history WHERE order_id=101 ORDER BY id DESC").fetchone()
        self.assertIsNotNone(h101)
        self.assertEqual(h101["action"], "Alteração de data em lote")

        # Verificar logs de auditoria
        audit = self.db.execute("SELECT * FROM audit_logs ORDER BY id DESC").fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual(audit["action"], "Definiu data de carga em lote")
        self.assertEqual(audit["entity"], "10")
        self.assertIn("Pedidos atualizados: 2", audit["new_value"])

    def test_batch_date_before_sale_date(self):
        """Testa se a validação impede definir data de entrega anterior à data da venda."""
        post_data = {
            "date": "2026-06-27",  # PED-100 tem venda em 2026-06-28
            "order_ids": "100,101",
            "_csrf": "my_csrf_token"
        }
        handler = MockApp(post_data_dict=post_data, session_data_tuple=("sid123", self.session))
        
        with self.assertRaises(ValueError) as ctx:
            handler.post_load_settlement_set_date(self.user, 10)
        
        self.assertIn("não pode ser anterior à data da venda", str(ctx.exception))

        # Garantir que nenhuma alteração foi feita no banco (rollback)
        o100 = self.db.execute("SELECT * FROM orders WHERE id=100").fetchone()
        o101 = self.db.execute("SELECT * FROM orders WHERE id=101").fetchone()
        self.assertEqual(o100["delivered_at"], "2026-06-30")
        self.assertIsNone(o101["delivered_at"])

    def test_batch_date_route_not_in_route(self):
        """Testa se a validação impede alterar datas de pedidos de uma carga que não está 'Em rota'."""
        post_data = {
            "date": "2026-07-01",
            "order_ids": "102",
            "_csrf": "my_csrf_token"
        }
        handler = MockApp(post_data_dict=post_data, session_data_tuple=("sid123", self.session))
        handler.post_load_settlement_set_date(self.user, 11)  # Carga 11 está 'Planejada'

        self.assertEqual(handler.response_status, 400)
        self.assertIn(b"Somente cargas em rota", handler.response_body)

        # Garantir que a data não foi alterada
        o102 = self.db.execute("SELECT * FROM orders WHERE id=102").fetchone()
        self.assertIsNone(o102["delivered_at"])

    def test_batch_date_order_not_in_route(self):
        """Testa se a validação impede alterar pedidos que não pertencem à carga informada."""
        post_data = {
            "date": "2026-07-01",
            "order_ids": "100,102",  # PED-102 pertence à carga 11, não à carga 10
            "_csrf": "my_csrf_token"
        }
        handler = MockApp(post_data_dict=post_data, session_data_tuple=("sid123", self.session))
        
        with self.assertRaises(ValueError) as ctx:
            handler.post_load_settlement_set_date(self.user, 10)
            
        self.assertIn("não pertencem a esta carga", str(ctx.exception))

        # Garantir que a transação foi revertida (PED-100 não deve ter sido alterado)
        o100 = self.db.execute("SELECT * FROM orders WHERE id=100").fetchone()
        self.assertEqual(o100["delivered_at"], "2026-06-30")


if __name__ == "__main__":
    unittest.main()
