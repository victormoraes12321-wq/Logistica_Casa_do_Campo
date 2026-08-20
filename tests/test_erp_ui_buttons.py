# -*- coding: utf-8 -*-
import unittest
import json
import threading
import time
from unittest.mock import MagicMock, patch

import app_core.erp_connector as erp_connector


class TestErpUiButtons(unittest.TestCase):
    """Testa os handlers de API dos botões do painel Admin ERP."""

    def test_status_json_returns_valid_structure(self):
        status = erp_connector.get_sync_status()
        self.assertIn("status", status)
        self.assertIn("running", status)
        self.assertIn("progress_pct", status)
        self.assertIn("step", status)
        self.assertIn("pedidos_count", status)
        self.assertIn("clientes_count", status)
        self.assertIn("vendedores_count", status)
        self.assertIn("faturamento_count", status)

    def test_sync_progress_updates(self):
        erp_connector._update_sync_info(step="Test Step 1", progress_pct=45, pedidos_count=120)
        st = erp_connector.get_sync_status()
        self.assertEqual(st["step"], "Test Step 1")
        self.assertEqual(st["progress_pct"], 45)
        self.assertEqual(st["pedidos_count"], 120)

    @patch("app_core.erp_connector._erp_connection")
    def test_check_connectivity_structure(self, mock_conn):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.description = [("CODIGO",), ("NOME",)]
        mock_conn.return_value.__enter__.return_value.cursor.return_value = mock_cursor

        with patch.object(erp_connector, "get_erp_config") as mock_cfg:
            cfg = MagicMock()
            cfg.is_ready = True
            cfg.enabled = True
            cfg.driver = "oracle"
            cfg.host = "127.0.0.1"
            cfg.port = 1521
            cfg.schema = "TEST"
            cfg.view_pedidos = "VW_PEDIDOS"
            cfg.view_itens = "VW_ITENS"
            cfg.view_clientes = "VW_CLIENTES"
            cfg.view_vendedores = "VW_VENDEDORES"
            cfg.view_produtos = "VW_PRODUTOS"
            cfg.view_faturamento = "VW_FATURAMENTO"
            cfg.qualified = lambda v: f"TEST.{v}"
            mock_cfg.return_value = cfg

            res = erp_connector.check_connectivity()
            self.assertTrue(res["ok"])
            self.assertIn("views", res)
            self.assertIn("pedidos", res["views"])


if __name__ == "__main__":
    unittest.main()
