"""Datas comerciais e entrega não se confundem; nenhum dado real é gravado."""
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from app_core import erp_connector
from app_core.domains import trip_registry_dispatch as trips
from app_core.services import trip_order_metadata as meta
from tests import test_trip_registry_lookup as lookup_tests


def commercial(number='99901', client='Cliente ERP'):
    return dict(order_number=number, invoice_number='77701', client_name=client, client_code='1305',
                sale_date='2026-09-23', invoiced_at='2026-09-28', expected_delivery_date='2026-10-08',
                city='CARLOS CHAGAS', uf='MG', route_name='ROTA 1', delivery_address='FAZENDA IBIRAPUERA II',
                seller_name='Vendedor ERP', weight_kg=900, total_value=4940.4,
                products_summary='30 SC Produto', details_complete=True, metadata_verified=True, source='ERP_MASTER',
                items=[dict(product_code='1495', product_name='Produto', quantity=30, unit='SC',
                            weight_kg=900, unit_price=164.68)])


class TripMetadataSaveTests(unittest.TestCase):
    def setUp(self):
        self.case = lookup_tests.TripLookupTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.db, self.handler = self.case.db, self.case.handler
        self.enterContext(patch.object(trips, '_get_oracle_cursor', return_value=(None, object())))

    def save(self, number='99901'):
        self.handler.post_data.return_value = dict(driver_id=1, vehicle_id=1, data_saida='2026-10-06',
            orders=[dict(order_number=number, delivery_date='2026-10-07', weight_kg=900, client_name='Cliente manual')])
        return trips.api_save_trip(self.handler, {'id': 1})

    def test_new_erp_order_saves_complete_metadata_and_real_products(self):
        with patch.object(meta, 'lookup_metadata', return_value=commercial()) as lookup:
            response, status = self.save()
        self.assertEqual(status, 200, response)
        lookup.assert_called_once()
        order = self.db.execute("SELECT * FROM orders WHERE order_number='99901'").fetchone()
        for key in ('sale_date','invoiced_at','expected_delivery_date','city','uf','route_name','delivery_address','seller_name'):
            self.assertEqual(order[key], commercial()[key])
        self.assertEqual(order['delivered_at'], '2026-10-07')
        item = self.db.execute('SELECT * FROM order_items WHERE order_id=?', (order['id'],)).fetchone()
        self.assertEqual(item['quantity'], 30)
        self.assertAlmostEqual(item['total_value'], 4940.4)
        self.assertEqual(item['status'], 'Entregue')

    def test_existing_order_is_enriched_without_replacing_actual_delivery(self):
        self.db.execute("UPDATE orders SET delivered_at='2026-10-01' WHERE id=1")
        with patch.object(meta, 'lookup_metadata', return_value=commercial('705335', 'Agropecuária Teste')):
            response, status = self.save('705335')
        self.assertEqual(status, 200, response)
        order = self.db.execute('SELECT * FROM orders WHERE id=1').fetchone()
        self.assertEqual(order['sale_date'], '2026-09-23')
        self.assertEqual(order['invoiced_at'], '2026-09-28')
        self.assertEqual(order['route_name'], 'ROTA 1')
        self.assertEqual(order['delivered_at'], '2026-10-01')
        self.assertEqual(order['weight_kg'], 825)

    def test_reference_of_another_customer_does_not_replace_manual_metadata(self):
        before = dict(self.db.execute('SELECT * FROM orders WHERE id=1').fetchone())
        with patch.object(meta, 'lookup_metadata', return_value=commercial('705335', 'CONSUMIDOR FINAL NFCE')):
            response, status = self.save('705335')
        self.assertEqual(status, 200, response)
        after = dict(self.db.execute('SELECT * FROM orders WHERE id=1').fetchone())
        for key in ('client_id','sale_date','invoiced_at','city','route_name','delivery_address','seller_name'):
            self.assertEqual(after[key], before[key])

    def test_unknown_manual_order_does_not_invent_sale_or_invoice_dates(self):
        with patch.object(meta, 'lookup_metadata', return_value=None):
            response, status = self.save()
        self.assertEqual(status, 200, response)
        order = self.db.execute("SELECT * FROM orders WHERE order_number='99901'").fetchone()
        self.assertIsNone(order['sale_date'])
        self.assertIsNone(order['invoiced_at'])
        self.assertIsNone(order['expected_delivery_date'])
        self.assertEqual(order['city'], '')
        self.assertEqual(order['invoice_number'], '')

    def test_erp_failure_does_not_create_route_or_settle_order(self):
        with patch.object(meta, 'lookup_metadata', side_effect=RuntimeError('ERP indisponível')):
            response, status = self.save('705335')
        self.assertEqual(status, 503)
        self.assertFalse(response['ok'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM routes').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT status FROM orders WHERE id=1').fetchone()[0], 'Faturado')


class TripMetadataReadTests(unittest.TestCase):
    def test_repeated_invoice_number_uses_latest_invoice_issue(self):
        old = dict(order_number=4032844,invoice_number=15928,invoice_issue_date=datetime(2020,1,8),sale_date=datetime(2026,10,20))
        recent = dict(order_number=50045358,invoice_number=15928,invoice_issue_date=datetime(2026,10,6))
        self.assertEqual(meta._invoice_candidates([old,recent],15928), [recent])

    def test_invoice_on_two_orders_in_same_issue_date_requires_order_number(self):
        rows = [dict(order_number=n,invoice_number=15928,invoice_issue_date=datetime(2026,10,6)) for n in (100,200)]
        with self.assertRaisesRegex(ValueError,'Informe o número do pedido'):
            meta._invoice_candidates(rows,15928)

    def test_fiscal_invoice_has_priority_over_an_unrelated_quote(self):
        fiscal = dict(order_number=100,invoice_number=15928,invoice_issue_date=datetime(2026,10,6))
        quote = dict(order_number=200,quote_number=15928,position='F',invoiced_at=datetime(2026,10,20))
        self.assertEqual(meta._invoice_candidates([quote,fiscal],15928), [fiscal])

    def test_pending_quote_is_not_a_fiscal_invoice(self):
        self.assertEqual(meta._invoice_candidates([dict(order_number=200,quote_number=15928,position='P')],15928), [])

    def test_missing_issue_dates_do_not_choose_an_arbitrary_order(self):
        rows = [dict(order_number=100,invoice_number=15928),
                dict(order_number=200,invoice_number=15928,invoice_issue_date=datetime(2026,10,6))]
        with self.assertRaises(ValueError):
            meta._invoice_candidates(rows,15928)

    def test_lookup_uses_master_sale_date_instead_of_invoice_date(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = [{'route_name':'ROTA 1','city':'Carlos Chagas'}]
        row = dict(order_number=700460,client_code=1305,client_name='Cliente',sale_date=datetime(2026,9,23,16,51),
                   invoiced_at=datetime(2026,9,28),invoice_issue_date=datetime(2026,9,28),
                   city='CARLOS CHAGAS',position='F',invoice_number=310376,weight_kg=900)
        cursor = MagicMock()
        cursor.description = [(key.upper(),) for key in row]
        cursor.fetchall.side_effect = [tuple([tuple(row.values())]), [(1495,'Produto','SC',30,164.68,900)]]
        cfg = MagicMock(is_ready=True,driver='oracle',schema='DONWMS',view_clientes='DONWMS.CLIENTES')
        with patch.object(erp_connector,'get_erp_config',return_value=cfg):
            actual = meta.lookup_metadata(db,'000700460',cursor)
        self.assertEqual(actual['sale_date'],'2026-09-23')
        self.assertEqual(actual['invoiced_at'],'2026-09-28')
        self.assertEqual(actual['expected_delivery_date'],'2026-10-08')
        self.assertEqual(actual['route_name'],'ROTA 1')
        self.assertIn('p.DATA AS SALE_DATE',cursor.execute.call_args_list[0].args[0])
        self.assertEqual(cursor.execute.call_args_list[0].args[1], {'ref':700460})

    def test_route_matching_is_exact_and_accent_insensitive(self):
        db = MagicMock()
        db.execute.return_value.fetchall.return_value = [{'city':'TEOFILO OTONI','route_name':'ROTA 4'}]
        self.assertEqual(meta.route_for_city(db,'Teófilo Otoni'),'ROTA 4')
        self.assertEqual(meta.route_for_city(db,'Otoni'),'')


if __name__ == '__main__':
    unittest.main()
