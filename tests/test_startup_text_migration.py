"""Reiniciar não deve reformatar novamente cadastros e entregas históricos."""
import sqlite3
import unittest
import app


class StartupTextMigrationTests(unittest.TestCase):
    def test_normalization_runs_once_and_preserves_later_manual_entries(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT);
            CREATE TABLE users(id INTEGER,name TEXT);
            CREATE TABLE orders(id INTEGER,seller_name TEXT,seller_id INTEGER,route_name TEXT,city TEXT);
            CREATE TABLE routes(id INTEGER,route_name TEXT);
            CREATE TABLE route_cities(id INTEGER,route_name TEXT,city TEXT);
            CREATE TABLE clients(id INTEGER,customer_code TEXT,name TEXT,document TEXT,phone TEXT,
                whatsapp TEXT,farm_name TEXT,address TEXT,reference_point TEXT,notes TEXT,
                updated_at TEXT,route_name TEXT,city TEXT);
            INSERT INTO orders(id,city) VALUES(1,'Carlos Chagas');
            INSERT INTO clients(id,name,city) VALUES(1,'Cliente legado','Carlos Chagas');
        ''')
        self.assertTrue(app.normalize_legacy_business_text_once(db))
        self.assertEqual(db.execute('SELECT city FROM orders').fetchone()[0], 'CARLOS CHAGAS')
        db.execute("INSERT INTO orders(id,city) VALUES(2,'Teófilo Otoni')")
        db.execute("INSERT INTO clients(id,name,city) VALUES(2,'JOSÉ LUIZ','Teófilo Otoni')")
        before_changes = db.total_changes
        self.assertFalse(app.normalize_legacy_business_text_once(db))
        self.assertEqual(db.total_changes, before_changes)
        order = db.execute('SELECT * FROM orders WHERE id=2').fetchone()
        client = db.execute('SELECT * FROM clients WHERE id=2').fetchone()
        self.assertEqual(order['city'], 'Teófilo Otoni')
        self.assertIsNone(order['seller_name'])
        self.assertIsNone(order['route_name'])
        self.assertIsNone(client['customer_code'])
        self.assertIsNone(client['document'])
        self.assertIsNone(client['updated_at'])


if __name__ == '__main__':
    unittest.main()
