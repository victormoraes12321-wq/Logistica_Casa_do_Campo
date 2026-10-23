"""Consulta de viagem deve preencher a linha sem executar sincronizações."""
import json
import unittest
from unittest.mock import MagicMock, patch

from app_core import erp_connector
from app_core.domains import trip_registry_dispatch as trips
from tests import test_trip_registry_baixa as trip_fixtures

configured_oracle_cursor = trips._get_oracle_cursor


class TripConnectionConfigTests(unittest.TestCase):
    def test_disabled_integration_does_not_open_a_connection(self):
        with patch.object(erp_connector, 'get_erp_config', return_value=MagicMock(is_ready=False)), \
                patch.object(erp_connector, '_build_connection') as build:
            self.assertEqual(configured_oracle_cursor(), (None, None))
            build.assert_not_called()

    def test_connection_uses_central_configuration(self):
        cfg = MagicMock(is_ready=True, driver='oracle', timeout=3)
        connection = MagicMock()
        with patch.object(erp_connector, 'get_erp_config', return_value=cfg), \
                patch.object(erp_connector, '_build_connection', return_value=connection) as build:
            actual, cursor = configured_oracle_cursor()
        self.assertIs(actual, connection)
        self.assertIs(cursor, connection.cursor.return_value)
        self.assertEqual(connection.call_timeout, 3000)
        build.assert_called_once_with(cfg)


class TripLookupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = trip_fixtures.TestTripRegistryBaixa()
        self.fixture.setUp()
        self.db = self.fixture.conn
        self.addCleanup(self.fixture.tearDown)
        self.db.executescript("""
            CREATE TABLE erp_cache_pedidos (numeropedido TEXT PRIMARY KEY, raw_json TEXT);
            CREATE TABLE erp_cache_clientes (codigocliente TEXT PRIMARY KEY, raw_json TEXT);
        """)
        self.db.execute("UPDATE orders SET payment_method='Pix', expected_delivery_date='2026-10-22' WHERE id=1")
        self.db.execute("INSERT INTO order_items(order_id,product_name,quantity,unit) VALUES(1,'Produto fracionado',1.5,'UN')")
        self.handler = MagicMock()
        self.handler.conn.return_value.__enter__.return_value = self.db
        self.handler.conn.return_value.__exit__.return_value = False
        self.handler.send_json.side_effect = lambda data, status=200: (data, status)
        self.db.commit()
        self.live = self.enterContext(patch.object(erp_connector, 'lookup_order', return_value=None))
        self.oracle = self.enterContext(patch.object(trips, '_get_oracle_cursor', return_value=(None, None)))
        self.sync = self.enterContext(patch.object(erp_connector, 'sync_erp_cache', side_effect=AssertionError('Consulta não pode sincronizar')))

    def search(self, value, field='order_number'):
        self.handler.path = f'/api/trips/search-item?q={value}&field={field}'
        before = self.db.total_changes
        result = trips.api_search_item(self.handler, {'id': 1})
        self.assertEqual(before, self.db.total_changes, 'Busca alterou dados do sistema')
        return result

    def test_local_lookup_fills_all_available_fields_without_connecting_to_erp(self):
        response, status = self.search('705335')
        self.assertEqual(status, 200)
        item = response['item']
        self.assertEqual(item['client_name'], 'Agropecuária Teste')
        self.assertEqual(item['invoice_number'], '311929')
        self.assertEqual(item['payment_method'], 'Pix')
        self.assertEqual(item['delivery_date'], '')
        self.assertEqual(item['expected_delivery_date'], '2026-10-22')
        self.assertEqual(item['weight_kg'], 825)
        self.assertIn('1.5 UN Produto fracionado', item['products_summary'])
        self.live.assert_not_called()
        self.oracle.assert_not_called()
        self.sync.assert_not_called()

    def test_order_and_invoice_with_same_number_are_not_confused(self):
        self.db.execute("INSERT INTO orders(order_number, invoice_number,client_id,status) VALUES('311929','888',1,'Faturado')")
        self.assertEqual(self.search('311929')[0]['item']['order_number'], '311929')
        self.assertEqual(self.search('311929', 'invoice_number')[0]['item']['order_number'], '705335')

    def test_leading_zeroes_are_accepted(self):
        self.assertEqual(self.search('000705335')[0]['item']['order_number'], '705335')

    def test_order_field_does_not_silently_match_an_invoice(self):
        with patch.object(trips, '_lookup_trip_erp_info', return_value=None):
            self.assertIsNone(self.search('311929')[0]['item'])

    def test_invoice_ambiguity_returns_explanation_instead_of_generic_failure(self):
        message = 'Esta NF tem mais de um pedido. Informe o número do pedido.'
        with patch.object(trips, '_lookup_trip_erp_info', side_effect=ValueError(message)):
            response, status = self.search('15928', 'invoice_number')
        self.assertEqual(status, 409)
        self.assertEqual(response['message'], message)
        self.assertFalse(response['ok'])

    def test_alphanumeric_reference_does_not_match_only_its_digits(self):
        with patch.object(trips, '_lookup_trip_erp_info', return_value=None):
            self.assertIsNone(self.search('PED-705335')[0]['item'])

    def test_cancelled_order_is_not_advertised_for_automatic_settlement(self):
        self.db.execute("UPDATE orders SET status='Cancelado' WHERE id=1")
        self.assertFalse(self.search('705335')[0]['item']['is_in_orders_list'])

    def test_cache_lookup_keeps_products_and_does_not_change_operational_orders(self):
        raw = {'pedido_': '99901', 'nota_': '77701', 'total_weight': 12,
               'razao_': 'Cliente do cache', 'formapagamento': 'Cartão',
               'data_entrega': '2026-10-23',
               '_itens': [{'codigoproduto': 'P1', 'nomeproduto': 'Produto cache', 'quantidade': 2, 'unidade': 'UN'}]}
        self.db.execute('INSERT INTO erp_cache_pedidos VALUES(?,?)', ('99901', json.dumps(raw)))
        response, _ = self.search('99901')
        self.assertEqual(response['item']['source'], 'ERP_CACHE')
        self.assertEqual(response['item']['weight_kg'], 12)
        self.assertIn('Produto cache', response['item']['products_summary'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 3)
        self.live.assert_not_called()
        self.oracle.assert_not_called()

    def test_invoice_lookup_can_resolve_a_cached_order(self):
        raw = {'pedido_': '99901', 'nota_': '77701', 'total_weight': 12,
               '_itens': [{'nomeproduto': 'Produto', 'quantidade': 1}]}
        self.db.execute('INSERT INTO erp_cache_pedidos VALUES(?,?)', ('99901', json.dumps(raw)))
        self.assertEqual(self.search('77701', 'invoice_number')[0]['item']['order_number'], '99901')

    def test_open_orders_supply_payment_and_delivery_date(self):
        data, _ = trips.api_open_orders(self.handler, {'id': 1})
        item = next(item for item in data['orders'] if item['order_number'] == '705335')
        self.assertEqual(item['payment_method'], 'Pix')
        self.assertEqual(item['delivery_date'], '')
        self.assertEqual(item['expected_delivery_date'], '2026-10-22')

    def test_erp_disabled_prevents_a_live_connection(self):
        cfg = MagicMock(is_ready=False)
        with patch.object(erp_connector, 'get_erp_config', return_value=cfg):
            self.assertIsNone(self.search('99999')[0]['item'])
        self.live.assert_not_called()
        self.oracle.assert_not_called()

    def test_verify_and_save_reject_invoice_belonging_to_another_order(self):
        payload = {'driver_id': 1, 'vehicle_id': 1, 'data_saida': '2026-10-21',
                   'orders': [{'order_number': '99999', 'invoice_number': '311929',
                               'weight_kg': 825, 'client_name': 'Agropecuária Teste'}]}
        self.handler.post_data.return_value = payload
        verification, _ = trips.api_verify_trip(self.handler, {'id': 1})
        self.assertEqual(verification['items'][0]['status'], 'DIVERGENCIA_PEDIDO')
        before = self.db.total_changes
        response, status = trips.api_save_trip(self.handler, {'id': 1})
        self.assertEqual(status, 409)
        self.assertFalse(response['ok'])
        self.assertEqual(self.db.total_changes, before)
        self.assertEqual(self.db.execute('SELECT status FROM orders WHERE id=1').fetchone()[0], 'Faturado')

    def test_verify_uses_same_cached_order_as_autofill(self):
        raw = {'pedido_': '99901', 'nota_': '77701', 'total_weight': 12,
               '_itens': [{'nomeproduto': 'Produto', 'quantidade': 1}]}
        self.db.execute('INSERT INTO erp_cache_pedidos VALUES(?,?)', ('99901', json.dumps(raw)))
        self.handler.post_data.return_value = {'orders': [{'order_number': '99901', 'invoice_number': '77701', 'weight_kg': 12}]}
        response, _ = trips.api_verify_trip(self.handler, {'id': 1})
        self.assertEqual(response['items'][0]['status'], 'OK_ERP_NOVO')


if __name__ == '__main__':
    unittest.main()
