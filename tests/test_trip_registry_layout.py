"""Regression coverage for rendering trips when run.py is the entry point."""
import sqlite3
import sys
import types
import unittest
from unittest.mock import Mock, patch

from app_core.domains import trip_registry_dispatch as trips


class TripRegistryLayoutTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE drivers (id INTEGER, name TEXT, active INTEGER);
            CREATE TABLE vehicles (
                id INTEGER, name TEXT, plate TEXT, type TEXT,
                capacity_kg REAL, active INTEGER
            );
        """)
        self.handler = Mock()
        self.handler.conn.return_value = self.db
        self.handler.require_perm.return_value = True
        self.user = {"name": "Operador", "role": "GOD"}

    @staticmethod
    def layout(user, title, content, **kwargs):
        return '<!doctype html><link rel="stylesheet" href="/static/style.css">' + content

    def assert_full_page(self):
        rendered = self.handler.send_html.call_args.args[0]
        self.assertTrue(rendered.startswith("<!doctype html>"))
        self.assertIn('href="/static/style.css"', rendered)
        self.assertIn('name="order_number"', rendered)
        self.assertIn('name="invoice_number"', rendered)
        self.assertIn('id="tripForm" data-realtime-refresh="off"', rendered)

    def test_dispatch_preserves_explicit_application_layout(self):
        handled = trips.handle_get(
            self.handler, "/registro-viagens", self.user, layout_func=self.layout
        )
        self.assertTrue(handled)
        self.assert_full_page()

    def test_run_entry_point_without_layout_uses_imported_app_layout(self):
        run_entry = types.ModuleType("__main__")
        app_module = types.ModuleType("app")
        app_module.layout = self.layout
        with patch.dict(sys.modules, {"__main__": run_entry, "app": app_module}):
            trips.render_registro_viagens_page(self.handler, self.user)
        self.assert_full_page()


if __name__ == "__main__":
    unittest.main()
