# -*- coding: utf-8 -*-
from __future__ import annotations
import unittest
import sqlite3
import os
import tempfile

import app


def conn():
    return app.conn()


def today():
    return app.today()

class DriverReportFixTests(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        self.old_db_target = app.DB_TARGET
        self.old_db_path = app.DB_PATH
        app.DB_PATH = self.db_path
        app.DB_TARGET = app.RuntimeDatabaseTarget(
            backend="sqlite",
            database_url=f"sqlite:///{self.db_path}",
            sqlite_path=self.db_path,
        )
        app.init_db()

    def tearDown(self):
        app.DB_TARGET = self.old_db_target
        app.DB_PATH = self.old_db_path
        os.close(self.db_fd)
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_driver_resolved_from_route_in_reports(self):
        # 1. Setup: Criar motorista, veículo, cliente, pedido e carga no banco de dados.
        with conn() as db:
            # Criar motorista de teste
            cur = db.execute("""INSERT INTO drivers(name, active, phone, updated_at) 
                                VALUES ('Motorista Teste Relatorio', 1, '123', '2026-06-30')""")
            driver_id = cur.lastrowid
            
            # Criar veículo de teste
            cur = db.execute("""INSERT INTO vehicles(name, plate, capacity, active, updated_at) 
                                VALUES ('Caminhao Teste', 'XYZ-9999', 5000, 1, '2026-06-30')""")
            vehicle_id = cur.lastrowid
            
            # Criar cliente de teste
            cur = db.execute("""INSERT INTO clients(name, farm_name, city, active, created_at, updated_at) 
                                VALUES ('Cliente Teste', 'Fazenda Teste', 'Cidade Teste', 1, '2026-06-30', '2026-06-30')""")
            client_id = cur.lastrowid
            
            # Criar pedido (sem driver_id direto na tabela orders)
            cur = db.execute("""INSERT INTO orders(order_number, client_id, status, sale_date, expected_delivery_date, weight_kg, total_value, created_at, updated_at)
                                VALUES ('PED-REP-TEST', ?, 'Faturado', ?, ?, 100, 1000, '2026-06-30', '2026-06-30')""",
                             (client_id, today(), today()))
            order_id = cur.lastrowid
            
            # Criar carga com o motorista de teste
            cur = db.execute("""INSERT INTO routes(name, date, driver_id, vehicle_id, status, route_name, total_weight, capacity, created_at, updated_at)
                                VALUES ('Carga Relatorio', ?, ?, ?, 'Planejada', 'Rota Teste', 100, 5000, '2026-06-30', '2026-06-30')""",
                             (today(), driver_id, vehicle_id))
            route_id = cur.lastrowid
            
            # Associar pedido à carga
            db.execute("INSERT INTO route_orders(route_id, order_id, delivery_order, status) VALUES (?, ?, 1, 'Pendente')", (route_id, order_id))
            db.commit()
            
        try:
            # 2. Executar consultas e fazer asserções usando exatamente a lógica implementada
            with conn() as db:
                # Testar lógica de listagem de relatório
                rows = db.execute("""SELECT COALESCE(d.name, od.name, 'Sem motorista') driver
                                     FROM orders o
                                     LEFT JOIN route_orders ro ON ro.order_id=o.id
                                     LEFT JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada'
                                     LEFT JOIN drivers d ON d.id=r.driver_id
                                     LEFT JOIN drivers od ON od.id=o.driver_id
                                     WHERE o.id=?""", (order_id,)).fetchall()
                
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['driver'], 'Motorista Teste Relatorio')
                
                # Testar lógica de agrupamento de motoristas (dr)
                dr_rows = db.execute("""SELECT COALESCE(d.name, od.name, 'Sem motorista') driver, COUNT(o.id) c
                                        FROM orders o
                                        LEFT JOIN route_orders ro ON ro.order_id=o.id
                                        LEFT JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada'
                                        LEFT JOIN drivers d ON d.id=r.driver_id
                                        LEFT JOIN drivers od ON od.id=o.driver_id
                                        WHERE o.id=?
                                        GROUP BY COALESCE(d.name, od.name, 'Sem motorista')""", (order_id,)).fetchall()
                self.assertEqual(len(dr_rows), 1)
                self.assertEqual(dr_rows[0]['driver'], 'Motorista Teste Relatorio')
                
        finally:
            # 3. Cleanup: Remover os registros criados para manter o banco de dados limpo
            with conn() as db:
                db.execute("DELETE FROM route_orders WHERE order_id=?", (order_id,))
                db.execute("DELETE FROM routes WHERE id=?", (route_id,))
                db.execute("DELETE FROM orders WHERE id=?", (order_id,))
                db.execute("DELETE FROM clients WHERE id=?", (client_id,))
                db.execute("DELETE FROM vehicles WHERE id=?", (vehicle_id,))
                db.execute("DELETE FROM drivers WHERE id=?", (driver_id,))
                db.commit()

if __name__ == '__main__':
    unittest.main()
