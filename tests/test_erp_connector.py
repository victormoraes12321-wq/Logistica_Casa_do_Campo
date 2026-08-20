# -*- coding: utf-8 -*-
"""
tests/test_erp_connector.py
============================
Testes unitários para o módulo erp_connector.
Todos os testes usam mock — nenhuma conexão real com ERP é feita.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# Garante que o módulo app_core seja encontrado
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestErpConfig(unittest.TestCase):
    """Testa a leitura de configuração do ERP via variáveis de ambiente."""

    def setUp(self):
        # Limpa o cache de configuração entre testes
        import app_core.erp_connector as mod
        with mod._config_lock:
            mod._cfg = None

    def test_disabled_by_default(self):
        """ERP_ENABLED deve ser False por padrão (seguro para deploy)."""
        import app_core.erp_connector as mod
        env = {"ERP_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=False):
            with mod._config_lock:
                mod._cfg = None
            cfg = mod.get_erp_config()
            self.assertFalse(cfg.enabled)

    def test_enabled_when_set(self):
        """ERP_ENABLED=true deve habilitar a integração."""
        import app_core.erp_connector as mod
        env = {
            "ERP_ENABLED": "true",
            "ERP_DB_HOST": "192.168.1.10",
            "ERP_DB_NAME": "TESTDB",
            "ERP_DB_USER": "user_ro",
            "ERP_DB_PASSWORD": "pass",
        }
        with patch.dict(os.environ, env, clear=False):
            with mod._config_lock:
                mod._cfg = None
            cfg = mod.get_erp_config()
            self.assertTrue(cfg.enabled)
            self.assertTrue(cfg.is_ready)

    def test_not_ready_without_host(self):
        """is_ready deve ser False se ERP_DB_HOST não estiver definido."""
        import app_core.erp_connector as mod
        env = {
            "ERP_ENABLED": "true",
            "ERP_DB_HOST": "",
            "ERP_DB_NAME": "TESTDB",
            "ERP_DB_USER": "user_ro",
        }
        with patch.dict(os.environ, env, clear=False):
            with mod._config_lock:
                mod._cfg = None
            cfg = mod.get_erp_config()
            self.assertFalse(cfg.is_ready)

    def test_qualified_view_name(self):
        """qualified() deve retornar schema.view quando schema definido."""
        import app_core.erp_connector as mod
        env = {
            "ERP_ENABLED": "true",
            "ERP_DB_DRIVER": "sqlserver",
            "ERP_DB_HOST": "localhost",
            "ERP_DB_NAME": "ERP",
            "ERP_DB_USER": "ro",
            "ERP_DB_SCHEMA": "dbo",
        }
        with patch.dict(os.environ, env, clear=False):
            with mod._config_lock:
                mod._cfg = None
            cfg = mod.get_erp_config()
            self.assertEqual(cfg.qualified("VW_PEDIDOS_CAD_LM"), "dbo.VW_PEDIDOS_CAD_LM")

        env_oracle = {
            "ERP_ENABLED": "true",
            "ERP_DB_DRIVER": "oracle",
            "ERP_DB_HOST": "localhost",
            "ERP_DB_NAME": "ERP",
            "ERP_DB_USER": "ro",
            "ERP_DB_SCHEMA": "dbo",
        }
        with patch.dict(os.environ, env_oracle, clear=False):
            with mod._config_lock:
                mod._cfg = None
            cfg = mod.get_erp_config()
            self.assertEqual(cfg.qualified("VW_PEDIDOS_CAD_LM"), "DBO.VW_PEDIDOS_CAD_LM")

    def test_view_names_configurable(self):
        """Nomes das views devem ser configuráveis via ERP_VIEW_*."""
        import app_core.erp_connector as mod
        env = {
            "ERP_ENABLED": "true",
            "ERP_DB_HOST": "localhost",
            "ERP_DB_NAME": "ERP",
            "ERP_DB_USER": "ro",
            "ERP_VIEW_PEDIDOS": "MINHA_VIEW_PEDIDOS",
        }
        with patch.dict(os.environ, env, clear=False):
            with mod._config_lock:
                mod._cfg = None
            cfg = mod.get_erp_config()
            self.assertEqual(cfg.view_pedidos, "MINHA_VIEW_PEDIDOS")

    def test_default_ports(self):
        """Porta padrão do Oracle deve ser 1521 e do SQL Server 1433."""
        import app_core.erp_connector as mod
        env_oracle = {"ERP_ENABLED": "false", "ERP_DB_DRIVER": "oracle"}
        with patch.dict(os.environ, env_oracle, clear=False):
            with mod._config_lock:
                mod._cfg = None
            cfg = mod.get_erp_config()
            self.assertEqual(cfg.port, 1521)

        env_sql = {"ERP_ENABLED": "false", "ERP_DB_DRIVER": "sqlserver"}
        with patch.dict(os.environ, env_sql, clear=False):
            with mod._config_lock:
                mod._cfg = None
            cfg = mod.get_erp_config()
            # Se ERP_DB_PORT não foi informado no env e driver é sqlserver, default
            self.assertEqual(cfg.port, 1433)


class TestLookupOrderDisabled(unittest.TestCase):
    """Testa que lookup retorna None quando ERP está desabilitado."""

    def test_lookup_returns_none_when_disabled(self):
        """lookup_order deve retornar None se ERP_ENABLED=false."""
        import app_core.erp_connector as mod
        env = {"ERP_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=False):
            with mod._config_lock:
                mod._cfg = None
            result = mod.lookup_order("12345")
            self.assertIsNone(result)

    def test_lookup_returns_none_for_empty_number(self):
        """lookup_order deve retornar None para número de pedido vazio."""
        import app_core.erp_connector as mod
        result = mod.lookup_order("")
        self.assertIsNone(result)
        result2 = mod.lookup_order(None)  # type: ignore
        self.assertIsNone(result2)

    def test_invoice_status_returns_none_when_disabled(self):
        """lookup_invoice_status deve retornar None se ERP desabilitado."""
        import app_core.erp_connector as mod
        env = {"ERP_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=False):
            with mod._config_lock:
                mod._cfg = None
            result = mod.lookup_invoice_status("12345")
            self.assertIsNone(result)


class TestCheckConnectivityDisabled(unittest.TestCase):
    """Testa check_connectivity quando ERP está desabilitado."""

    def test_returns_not_ok_when_disabled(self):
        """check_connectivity deve retornar ok=False quando desabilitado."""
        import app_core.erp_connector as mod
        env = {"ERP_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=False):
            with mod._config_lock:
                mod._cfg = None
            result = mod.check_connectivity()
            self.assertFalse(result["ok"])
            self.assertIn("desabilitada", result["message"].lower())

    def test_returns_not_ok_when_incomplete_config(self):
        """check_connectivity deve retornar ok=False quando configuração incompleta."""
        import app_core.erp_connector as mod
        env = {
            "ERP_ENABLED": "true",
            "ERP_DB_HOST": "",
            "ERP_DB_NAME": "",
            "ERP_DB_USER": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with mod._config_lock:
                mod._cfg = None
            result = mod.check_connectivity()
            self.assertFalse(result["ok"])


class TestRowToDict(unittest.TestCase):
    """Testa a função de conversão de row para dict."""

    def test_converts_row_to_lowercase_dict(self):
        """_row_to_dict deve converter chaves para lowercase."""
        import app_core.erp_connector as mod
        cursor = MagicMock()
        cursor.description = [("NomeProduto",), ("Quantidade",), ("Preco",)]
        row = ("Soja Saco", 100, 50.75)
        result = mod._row_to_dict(cursor, row)
        self.assertEqual(result["nomeproduto"], "Soja Saco")
        self.assertEqual(result["quantidade"], 100)
        self.assertEqual(result["preco"], 50.75)

    def test_returns_empty_dict_for_none_row(self):
        """_row_to_dict deve retornar dict vazio para row None."""
        import app_core.erp_connector as mod
        cursor = MagicMock()
        cursor.description = [("col1",)]
        result = mod._row_to_dict(cursor, None)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
