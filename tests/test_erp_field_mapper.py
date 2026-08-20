# -*- coding: utf-8 -*-
"""
tests/test_erp_field_mapper.py
================================
Testes unitários para o módulo erp_field_mapper.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestMapErpToLogistica(unittest.TestCase):
    """Testa o mapeamento de dados brutos do ERP para campos logísticos."""

    def setUp(self):
        # Limpa variáveis de ambiente de mapeamento para testes
        for key in list(os.environ.keys()):
            if key.startswith("ERP_COL_"):
                del os.environ[key]

    def _make_erp_data(self, **kwargs):
        """Monta um dict simulando o retorno do ERP."""
        data = {
            "numeropedido": "54321",
            "datavenda": "2025-01-15",
            "valortotal": "2500.50",
            "formapagamento": "Boleto",
            "codigocliente": "CLI001",
            "codigovendedor": "VND001",
            "_erp_pedido_ok": True,
            "_itens": [],
            "_cliente": {
                "nomecliente": "FAZENDA BOA VISTA",
                "cidade": "UBERLANDIA",
                "uf": "MG",
                "bairro": "ZONA RURAL",
                "endereco": "ROD. BR-050 KM 22",
                "telefone": "(34) 99999-8888",
            },
            "_vendedor": {
                "nomevendedor": "JOAO DA SILVA",
            },
        }
        data.update(kwargs)
        return data

    def test_basic_mapping(self):
        """Campos básicos devem ser mapeados corretamente."""
        import app_core.erp_field_mapper as mod
        erp_data = self._make_erp_data()
        result = mod.map_erp_to_logistica(erp_data)
        self.assertEqual(result["order_number"], "54321")
        self.assertEqual(result["client_name"], "FAZENDA BOA VISTA")
        self.assertEqual(result["city"], "UBERLANDIA")
        self.assertEqual(result["uf"], "MG")
        self.assertEqual(result["seller_name"], "JOAO DA SILVA")
        self.assertEqual(result["sale_date"], "2025-01-15")
        self.assertAlmostEqual(result["total_value"], 2500.50, places=2)

    def test_delivery_address_assembled(self):
        """Endereço de entrega deve ser montado a partir de partes."""
        import app_core.erp_field_mapper as mod
        erp_data = self._make_erp_data()
        result = mod.map_erp_to_logistica(erp_data)
        addr = result["delivery_address"]
        self.assertIn("ROD. BR-050 KM 22", addr)
        self.assertIn("UBERLANDIA", addr)
        self.assertIn("MG", addr)

    def test_empty_data_returns_empty_dict(self):
        """Dados vazios devem retornar dict vazio."""
        import app_core.erp_field_mapper as mod
        result = mod.map_erp_to_logistica({})
        self.assertEqual(result, {})
        result2 = mod.map_erp_to_logistica(None)  # type: ignore
        self.assertEqual(result2, {})

    def test_erp_filled_flag(self):
        """_erp_filled deve ser True em dados válidos."""
        import app_core.erp_field_mapper as mod
        erp_data = self._make_erp_data()
        result = mod.map_erp_to_logistica(erp_data)
        self.assertTrue(result.get("_erp_filled"))

    def test_items_mapping(self):
        """Itens do pedido devem ser mapeados corretamente."""
        import app_core.erp_field_mapper as mod
        erp_data = self._make_erp_data(_itens=[
            {
                "codigoproduto": "P001",
                "nomeproduto": "SOJA SACARIA 60KG",
                "quantidade": 10.0,
                "unidade": "saco",
                "_produto": {"pesoproduto": 60.0},
            },
            {
                "codigoproduto": "P002",
                "nomeproduto": "MILHO GRANEL",
                "quantidade": 5.0,
                "unidade": "tonelada",
                "_produto": {"pesoproduto": 1000.0},
            },
        ])
        result = mod.map_erp_to_logistica(erp_data)
        self.assertEqual(result["_erp_item_count"], 2)
        items = result["items"]
        self.assertEqual(len(items), 2)
        # Primeiro item: 10 sacos * 60kg = 600kg
        self.assertEqual(items[0]["product_code"], "P001")
        self.assertAlmostEqual(items[0]["weight_kg"], 600.0, places=2)
        # Segundo item: 5 ton * 1000kg = 5000kg
        self.assertAlmostEqual(items[1]["weight_kg"], 5000.0, places=2)
        # Peso total: 5600kg
        self.assertAlmostEqual(result["weight_kg"], 5600.0, places=2)

    def test_no_items_gives_zero_weight(self):
        """Pedido sem itens deve ter peso 0."""
        import app_core.erp_field_mapper as mod
        erp_data = self._make_erp_data(_itens=[])
        result = mod.map_erp_to_logistica(erp_data)
        self.assertEqual(result["weight_kg"], 0.0)
        self.assertFalse(result.get("_erp_has_weight"))

    def test_payment_method_normalization(self):
        """Forma de pagamento deve ser normalizada."""
        import app_core.erp_field_mapper as mod
        cases = [
            ("boleto", "Boleto"),
            ("à vista", "À vista"),
            ("a vista", "À vista"),
            ("pix", "Pix"),
            ("CARTAO", "Cartão"),
        ]
        for raw, expected in cases:
            erp_data = self._make_erp_data(formapagamento=raw)
            result = mod.map_erp_to_logistica(erp_data)
            self.assertEqual(result["payment_method"], expected, f"Falhou para: {raw!r}")

    def test_date_parsing_br_format(self):
        """Datas no formato DD/MM/YYYY devem ser convertidas para YYYY-MM-DD."""
        import app_core.erp_field_mapper as mod
        erp_data = self._make_erp_data(datavenda="25/07/2025")
        result = mod.map_erp_to_logistica(erp_data)
        self.assertEqual(result["sale_date"], "2025-07-25")

    def test_decimal_with_comma(self):
        """Valores com vírgula decimal devem ser parseados corretamente."""
        import app_core.erp_field_mapper as mod
        erp_data = self._make_erp_data(valortotal="1.250,75")
        result = mod.map_erp_to_logistica(erp_data)
        self.assertAlmostEqual(result["total_value"], 1250.75, places=2)


class TestMapErpInvoice(unittest.TestCase):
    """Testa o mapeamento de dados de faturamento do ERP."""

    def test_invoice_mapping(self):
        """Dados de NF devem ser mapeados corretamente."""
        import app_core.erp_field_mapper as mod
        fat_data = {
            "numeronf": "NF-123456",
            "datafaturamento": "2025-07-30",
        }
        result = mod.map_erp_invoice_to_logistica(fat_data)
        self.assertIsNotNone(result)
        self.assertEqual(result["invoice_number"], "NF-123456")
        self.assertEqual(result["invoiced_at"], "2025-07-30")

    def test_no_nf_returns_none(self):
        """Ausência de número de NF deve retornar None."""
        import app_core.erp_field_mapper as mod
        fat_data = {"datafaturamento": "2025-07-30"}
        result = mod.map_erp_invoice_to_logistica(fat_data)
        self.assertIsNone(result)

    def test_empty_data_returns_none(self):
        """Dados vazios devem retornar None."""
        import app_core.erp_field_mapper as mod
        self.assertIsNone(mod.map_erp_invoice_to_logistica({}))
        self.assertIsNone(mod.map_erp_invoice_to_logistica(None))  # type: ignore

    def test_date_normalization_in_invoice(self):
        """Data de faturamento em formato DD/MM/YYYY deve ser convertida."""
        import app_core.erp_field_mapper as mod
        fat_data = {
            "numeronf": "NF-999",
            "datafaturamento": "30/07/2025",
        }
        result = mod.map_erp_invoice_to_logistica(fat_data)
        self.assertIsNotNone(result)
        self.assertEqual(result["invoiced_at"], "2025-07-30")


class TestParseHelpers(unittest.TestCase):
    """Testa os helpers internos de parsing."""

    def test_parse_float(self):
        """_parse_float deve lidar com diversos formatos."""
        import app_core.erp_field_mapper as mod
        self.assertAlmostEqual(mod._parse_float("1500.75"), 1500.75)
        self.assertAlmostEqual(mod._parse_float("1.500,75"), 1500.75)
        self.assertAlmostEqual(mod._parse_float(0), 0.0)
        self.assertAlmostEqual(mod._parse_float(None), 0.0)
        self.assertAlmostEqual(mod._parse_float(""), 0.0)

    def test_parse_date_formats(self):
        """_parse_date deve lidar com formatos variados."""
        import app_core.erp_field_mapper as mod
        self.assertEqual(mod._parse_date("2025-07-30"), "2025-07-30")
        self.assertEqual(mod._parse_date("30/07/2025"), "2025-07-30")
        self.assertEqual(mod._parse_date("30-07-2025"), "2025-07-30")
        self.assertEqual(mod._parse_date(None), "")
        self.assertEqual(mod._parse_date(""), "")

    def test_clean_str(self):
        """_clean_str deve remover espaços e converter para string."""
        import app_core.erp_field_mapper as mod
        self.assertEqual(mod._clean_str("  TESTE  "), "TESTE")
        self.assertEqual(mod._clean_str(None), "")
        self.assertEqual(mod._clean_str(123), "123")


if __name__ == "__main__":
    unittest.main(verbosity=2)
