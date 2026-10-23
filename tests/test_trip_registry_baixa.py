# -*- coding: utf-8 -*-
"""
Testes unitários e de integração para a funcionalidade de baixa automática de pedidos
via tela de Registro de Viagens (app_core.domains.trip_registry_dispatch).
"""
import unittest
import sqlite3
import json
from unittest.mock import MagicMock

class TestTripRegistryBaixa(unittest.TestCase):
    def setUp(self):
        # Cria banco de teste SQLite em memória com as tabelas necessárias
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        
        self.conn.executescript("""
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_code TEXT,
                name TEXT,
                document TEXT,
                phone TEXT,
                city TEXT,
                address TEXT,
                neighborhood TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT
            );
            
            CREATE TABLE drivers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                phone TEXT,
                active INTEGER DEFAULT 1
            );
            
            CREATE TABLE vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                plate TEXT,
                type TEXT,
                capacity_kg REAL,
                active INTEGER DEFAULT 1,
                updated_at TEXT
            );
            
            CREATE TABLE routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                date TEXT,
                driver_id INTEGER,
                vehicle_id INTEGER,
                status TEXT,
                route_name TEXT,
                total_weight REAL,
                declared_weight REAL,
                capacity REAL,
                notes TEXT,
                km_start REAL,
                km_end REAL,
                km_total REAL,
                departure_time TEXT,
                arrival_time TEXT,
                helper_name TEXT,
                destination TEXT,
                expenses_json TEXT,
                driver_signature TEXT,
                odometer_start_photo TEXT,
                odometer_end_photo TEXT,
                created_at TEXT,
                updated_at TEXT,
                version INTEGER DEFAULT 1
            );
            
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_number TEXT,
                external_id TEXT,
                client_id INTEGER,
                seller_id INTEGER,
                status TEXT,
                urgency TEXT,
                sale_date TEXT,
                expected_delivery_date TEXT,
                invoice_limit_date TEXT,
                payment_method TEXT,
                total_value REAL,
                weight_kg REAL,
                delivery_address TEXT,
                location_link TEXT,
                route_name TEXT,
                city TEXT,
                uf TEXT,
                notes TEXT,
                invoice_number TEXT,
                invoice_file_path TEXT,
                invoiced_at TEXT,
                driver_id INTEGER,
                vehicle_id INTEGER,
                delivered_to TEXT,
                delivered_document TEXT,
                delivered_at TEXT,
                final_notes TEXT,
                created_at TEXT,
                updated_at TEXT,
                version INTEGER DEFAULT 1,
                seller_name TEXT
            );
            
            CREATE TABLE order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                product_code TEXT,
                product_name TEXT,
                quantity REAL,
                unit TEXT,
                weight_kg REAL,
                unit_price REAL DEFAULT 0,
                total_value REAL DEFAULT 0,
                status TEXT DEFAULT 'Pendente',
                notes TEXT
            );
            
            CREATE TABLE route_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id INTEGER,
                order_id INTEGER,
                delivery_order INTEGER,
                status TEXT
            );
            
            CREATE TABLE order_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                user_id INTEGER,
                old_status TEXT,
                new_status TEXT,
                action TEXT NOT NULL,
                notes TEXT,
                created_at TEXT NOT NULL
            );
            
            CREATE TABLE trip_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id INTEGER NOT NULL,
                expense_type TEXT,
                quantity REAL,
                unit_value REAL,
                total_value REAL,
                notes TEXT,
                created_at TEXT
            );
        """)
        from app_core.services.trip_audit import ensure_schema
        ensure_schema(self.conn)
        
        # Insere dados de teste
        self.conn.execute("INSERT INTO drivers (name) VALUES ('Lucas')")
        self.conn.execute("INSERT INTO vehicles (name, plate, capacity_kg) VALUES ('Strada', 'SYP-5E02', 800.0)")
        self.conn.execute("INSERT INTO clients (name, city) VALUES ('Agropecuária Teste', 'Teófilo Otoni')")
        
        # Insere pedido em aberto (Status Faturado)
        self.conn.execute("""
            INSERT INTO orders (order_number, invoice_number, client_id, status, total_value, weight_kg, sale_date)
            VALUES ('705335', '311929', 1, 'Faturado', 4365.0, 825.0, '2026-10-15')
        """)
        
        # Insere pedido em aberto sem NF (Status Venda)
        self.conn.execute("""
            INSERT INTO orders (order_number, invoice_number, client_id, status, total_value, weight_kg, sale_date)
            VALUES ('50045478', '', 1, 'Venda', 5633.5, 1500.0, '2026-10-15')
        """)
        
        # Insere pedido que já estava em carga montada de rota anterior
        self.conn.execute("""
            INSERT INTO orders (order_number, invoice_number, client_id, status, total_value, weight_kg, sale_date)
            VALUES ('697045', '310343', 1, 'Carga Montada', 4996.0, 3250.0, '2026-10-15')
        """)
        self.conn.execute("""
            INSERT INTO route_orders (route_id, order_id, delivery_order, status)
            VALUES (99, 3, 1, 'Pendente')
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_candidate_generation(self):
        from app_core.domains.trip_registry_dispatch import _get_candidates
        cands = _get_candidates("0311929")
        self.assertIn("0311929", cands)
        self.assertIn("311929", cands)
        
        cands_formatted = _get_candidates("NF-311929")
        self.assertIn("311929", cands_formatted)

    def test_find_order_in_sqlite(self):
        from app_core.domains.trip_registry_dispatch import find_order_in_sqlite
        # Busca por Pedido
        res = find_order_in_sqlite(self.conn, "705335")
        self.assertIsNotNone(res)
        self.assertEqual(res["order_number"], "705335")
        self.assertEqual(res["invoice_number"], "311929")
        
        # Busca por NF com zero à esquerda
        res2 = find_order_in_sqlite(self.conn, "0311929")
        self.assertIsNotNone(res2)
        self.assertEqual(res2["id"], res["id"])
        
        # Busca por Pedido sem NF
        res3 = find_order_in_sqlite(self.conn, "050045478")
        self.assertIsNotNone(res3)
        self.assertEqual(res3["order_number"], "50045478")

    def test_save_trip_gives_baixa_to_open_orders(self):
        from app_core.domains.trip_registry_dispatch import api_save_trip
        
        # Mock Handler
        handler = MagicMock()
        handler.conn.return_value.__enter__.return_value = self.conn
        handler.conn.return_value.__exit__.return_value = False
        
        # Payload com pedido 705335 (digitando a NF 311929) e pedido 697045
        payload = {
            "driver_id": 1,
            "vehicle_id": 1,
            "data_saida": "2026-10-20",
            "hora_saida": "08:00",
            "data_chegada": "2026-10-20",
            "hora_chegada": "17:00",
            "km_start": 1000,
            "km_end": 1150,
            "destination": "Região Norte",
            "orders": [
                {
                    "item_number": "311929", # Digitou a NF
                    "client_name": "Agropecuária Teste",
                    "payment_type": "Boleto",
                    "weight_kg": 825.0,
                    "notes": "Entregue OK"
                },
                {
                    "item_number": "697045", # Digitou o Pedido
                    "client_name": "Agropecuária Teste",
                    "payment_type": "Boleto",
                    "weight_kg": 3250.0,
                    "notes": "Entregue OK"
                }
            ],
            "expenses": {}
        }
        handler.post_data.return_value = payload
        
        sent_response = {}
        def mock_send_json(data, status=200):
            sent_response.update(data)
            return True
        handler.send_json.side_effect = mock_send_json
        
        user = {"id": 1, "name": "Admin", "username": "admin"}
        api_save_trip(handler, user)
        
        self.assertTrue(sent_response.get("ok"))
        self.assertEqual(sent_response.get("orders_given_baixa_count"), 2)
        
        # Verifica se ambos os pedidos no banco agora estão com status 'Acertado'
        row1 = self.conn.execute("SELECT * FROM orders WHERE id=1").fetchone()
        self.assertEqual(row1["status"], "Acertado")
        self.assertEqual(row1["driver_id"], 1)
        self.assertEqual(row1["vehicle_id"], 1)
        self.assertEqual(row1["delivered_at"], "2026-10-20")
        
        row2 = self.conn.execute("SELECT * FROM orders WHERE id=3").fetchone()
        self.assertEqual(row2["status"], "Acertado")
        
        # Verifica se o pedido saiu da lista de pedidos em aberto!
        open_count = self.conn.execute("""
            SELECT COUNT(*) c FROM orders 
            WHERE id IN (1, 3) AND status NOT IN ('Acertado', 'Problema', 'Cancelado')
        """).fetchone()["c"]
        self.assertEqual(open_count, 0, "Pedidos que receberam baixa não podem mais aparecer na lista de pedidos em andamento!")
        
        # Verifica histórico de auditoria gravado
        hist_rows = self.conn.execute("SELECT * FROM order_history WHERE order_id=1").fetchall()
        self.assertEqual(len(hist_rows), 1)
        self.assertEqual(hist_rows[0]["old_status"], "Faturado")
        self.assertEqual(hist_rows[0]["new_status"], "Acertado")
        self.assertEqual(hist_rows[0]["action"], "Baixa por Registro de Viagens")
        
        # Verifica se rota anterior órfã em route_orders foi marcada como Entregue
        old_ro = self.conn.execute("SELECT status FROM route_orders WHERE route_id=99 AND order_id=3").fetchone()
        self.assertEqual(old_ro["status"], "Entregue")
        
        # Verifica se o vínculo com a nova rota foi criado como Entregue
        new_route_id = sent_response["route_id"]
        new_ro = self.conn.execute("SELECT status FROM route_orders WHERE route_id=? AND order_id=1", (new_route_id,)).fetchone()
        self.assertEqual(new_ro["status"], "Entregue")

    def test_api_open_orders(self):
        from app_core.domains.trip_registry_dispatch import api_open_orders
        handler = MagicMock()
        handler.conn.return_value.__enter__.return_value = self.conn
        handler.conn.return_value.__exit__.return_value = False
        
        sent = {}
        handler.send_json.side_effect = lambda d, s=200: sent.update(d)
        api_open_orders(handler, {"id": 1})
        
        self.assertTrue(sent.get("ok"))
        self.assertEqual(sent.get("count"), 3)
        order_numbers = [o["order_number"] for o in sent["orders"]]
        self.assertIn("705335", order_numbers)
        self.assertIn("50045478", order_numbers)
        self.assertIn("697045", order_numbers)

    def test_api_search_and_verify(self):
        from app_core.domains.trip_registry_dispatch import api_search_item, api_verify_trip
        handler = MagicMock()
        handler.conn.return_value.__enter__.return_value = self.conn
        handler.conn.return_value.__exit__.return_value = False
        
        # Teste de busca por item
        handler.path = "/api/trips/search-item?q=311929"
        sent_search = {}
        handler.send_json.side_effect = lambda d, s=200: sent_search.update(d)
        api_search_item(handler, {"id": 1})
        
        self.assertTrue(sent_search.get("ok"))
        self.assertIsNotNone(sent_search.get("item"))
        self.assertTrue(sent_search["item"]["is_in_orders_list"])
        self.assertEqual(sent_search["item"]["status"], "Faturado")
        
        # Teste de conferência
        handler.post_data.return_value = {
            "orders": [{"item_number": "311929", "weight_kg": 825.0, "client_name": "Agro"}]
        }
        sent_verify = {}
        handler.send_json.side_effect = lambda d, s=200: sent_verify.update(d)
        api_verify_trip(handler, {"id": 1})
        
        self.assertTrue(sent_verify.get("ok"))
        self.assertEqual(sent_verify.get("orders_to_baixa_count"), 1)
        self.assertEqual(sent_verify["items"][0]["status"], "OK_BAIXA")

if __name__ == "__main__":
    unittest.main()

