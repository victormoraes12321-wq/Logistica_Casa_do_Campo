"""Os dados digitados e consultados devem sobreviver a mudanças de cadastro."""
import json
import io
import sqlite3
import unittest
from unittest.mock import patch
from app_core.domains import trip_registry_dispatch as trips
from app_core.services import trip_audit, trip_order_metadata
from tests import test_trip_registry_lookup as fixtures
from tests.test_trip_order_metadata import commercial


class TripAuditTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.TripLookupTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.db,self.handler=self.case.db,self.case.handler
        self.handler.conn.return_value=self.db
        self.payload=dict(driver_id=1,vehicle_id=1,data_saida='2026-10-06',data_chegada='2026-10-07',
            hora_saida='08:30',hora_chegada='19:00',km_start=1000,km_end=1200,km_total=200,
            helper_name='Diego',destination='Carlos Chagas',
            orders=[dict(order_number='705335',invoice_number='311929',weight_kg=800,
                         client_name='Agropecuária Teste',delivery_date='2026-10-07',
                         payment_type='Pix',km_hr_end='1190 / 18:00',notes='Conferir descarga')],
            expenses=dict(overnight=dict(qty=2,unit_val=60),toll_val='10,50'))
        self.metadata=commercial('705335','Agropecuária Teste')
        self.metadata['invoice_number']='311929'
        self.handler.post_data.return_value=self.payload

    def save(self,metadata=True):
        with patch.object(trips,'_get_oracle_cursor',return_value=(None,object() if metadata else None)), \
             patch.object(trip_order_metadata,'lookup_metadata',return_value=self.metadata):
            return trips.api_save_trip(self.handler,{'id':2,'name':'Operador'})

    def test_existing_order_preserves_typed_erp_and_saved_weights_separately(self):
        self.db.execute('ALTER TABLE drivers ADD COLUMN password_hash TEXT')
        self.db.execute("UPDATE drivers SET password_hash='credencial não deve ir ao histórico'")
        response,status=self.save()
        self.assertEqual(status,200,response)
        row=self.db.execute('SELECT * FROM trip_registry_audit_orders').fetchone()
        self.assertEqual((row['entered_weight_kg'],row['saved_weight_kg'],row['erp_weight_kg'],row['weight_difference_kg']),(800,825,900,-100))
        self.assertEqual(row['has_difference'],1)
        self.assertEqual(row['screen_weight_alert'],1)
        original=trip_audit.load_trip(self.db,response['route_id'])
        self.assertEqual(original['route']['helper_name'],'Diego')
        self.assertEqual(original['route']['km_total'],200)
        self.assertEqual(original['entries'][0]['input']['km_hr_end'],'1190 / 18:00')
        self.assertEqual(original['entries'][0]['local_before']['weight_kg'],825)
        self.assertEqual(original['raw_input'],self.payload)
        self.assertEqual(original['expense_total'],130.5)
        self.assertEqual(original['actor'],{'id':2,'name':'Operador'})
        self.assertNotIn('password_hash',original['driver'])

    def test_snapshot_is_unchanged_after_editing_order_client_and_route(self):
        response,status=self.save();self.assertEqual(status,200,response)
        original=trip_audit.load_trip(self.db,response['route_id'])
        self.db.execute("UPDATE orders SET weight_kg=9999,notes='Alterado depois' WHERE id=1")
        self.db.execute("UPDATE clients SET name='Outro nome' WHERE id=1")
        self.db.execute("UPDATE routes SET helper_name='Outro ajudante' WHERE id=?",(response['route_id'],))
        self.db.execute('DELETE FROM trip_expenses WHERE route_id=?',(response['route_id'],))
        self.db.commit()
        self.assertEqual(trip_audit.load_trip(self.db,response['route_id']),original)

    def test_original_history_cannot_be_updated_or_deleted(self):
        response,status=self.save();self.assertEqual(status,200,response)
        for sql in ["UPDATE trip_registry_audits SET actor_name='Outro'",'DELETE FROM trip_registry_audits',
                    'UPDATE trip_registry_audit_orders SET entered_weight_kg=1','DELETE FROM trip_registry_audit_orders']:
            with self.subTest(sql=sql),self.assertRaisesRegex(sqlite3.IntegrityError,'histórico original'):
                self.db.execute(sql)
            self.db.rollback()

    def test_audit_failure_rolls_back_the_entire_trip(self):
        self.db.commit()
        with patch.object(trip_audit,'record_trip',side_effect=sqlite3.OperationalError('Falha de gravação')):
            response,status=self.save()
        self.assertEqual(status,503)
        self.assertFalse(response['ok'])
        for table in ('routes','trip_expenses','trip_registry_audits','trip_registry_audit_orders'):
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT status FROM orders WHERE id=1').fetchone()[0],'Faturado')

    def test_missing_erp_is_not_recorded_as_zero_or_as_a_match(self):
        response,status=self.save(metadata=False);self.assertEqual(status,200,response)
        row=self.db.execute('SELECT * FROM trip_registry_audit_orders').fetchone()
        self.assertIsNone(row['erp_weight_kg'])
        self.assertIsNone(row['weight_difference_kg'])
        self.assertIsNone(row['has_difference'])
        self.assertEqual(row['comparison_status'],'ERP não consultado')

    def test_other_client_is_preserved_as_an_unmatched_candidate(self):
        self.metadata['client_name']='CONSUMIDOR FINAL NFCE'
        response,status=self.save();self.assertEqual(status,200,response)
        original=trip_audit.load_trip(self.db,response['route_id'])['entries'][0]
        self.assertIsNone(original['erp_weight_kg'])
        self.assertIsNone(original['erp'])
        self.assertEqual(original['comparison_status'],'Referência de outro cliente no ERP')
        self.assertEqual(original['unmatched_erp_candidate']['client_name'],'CONSUMIDOR FINAL NFCE')

    def test_verification_uses_current_erp_weight_even_for_local_order(self):
        with patch.object(trips,'_get_oracle_cursor',return_value=(None,object())), \
             patch.object(trip_order_metadata,'lookup_metadata',return_value=self.metadata):
            response,status=trips.api_verify_trip(self.handler,{'id':2})
        self.assertEqual(status,200)
        self.assertEqual(response['items'][0]['erp_weight'],900)
        self.assertEqual(response['items'][0]['typed_weight'],800)
        self.assertEqual(response['items'][0]['source'],'ERP_MASTER')

    def test_client_code_allows_abbreviated_name_but_not_fabricated_local_code(self):
        existing=trips.find_order_in_sqlite(self.db,'705335','order_number')
        self.metadata['client_name']='Nome completo no ERP';self.metadata['client_code']='6088'
        self.db.execute("UPDATE clients SET customer_code='6088' WHERE id=1")
        self.assertTrue(trip_order_metadata.matches_client(self.db,existing,self.metadata))
        self.db.execute("UPDATE clients SET customer_code='1' WHERE id=1")
        self.metadata['client_code']='1'
        self.assertFalse(trip_order_metadata.matches_client(self.db,existing,self.metadata))

    def test_print_uses_original_trip_and_original_erp_comparison(self):
        from pypdf import PdfReader
        response,status=self.save();self.assertEqual(status,200,response)
        self.db.execute('UPDATE orders SET weight_kg=9999 WHERE id=1')
        self.db.execute("UPDATE routes SET helper_name='Mudou depois' WHERE id=?",(response['route_id'],))
        self.db.commit()
        trips.generate_trip_sheet_pdf(self.handler,{'id':2},response['route_id'])
        pdf=self.handler.wfile.write.call_args.args[0]
        text='\n'.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)
        self.assertIn('Diego',text)
        self.assertIn('800',text)
        self.assertIn('900',text)
        self.assertIn('-100',text)
        self.assertNotIn('9,999',text)
        self.assertNotIn('Mudou depois',text)


if __name__=='__main__':unittest.main()
