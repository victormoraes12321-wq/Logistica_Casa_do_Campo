"""Cálculo e persistência monetária das despesas, usando apenas banco em memória."""
import json
import unittest

from app_core.domains import trip_registry_dispatch as trips
from tests import test_trip_registry_lookup as lookup_tests


class TripExpenseTests(unittest.TestCase):
    def setUp(self):
        self.case = lookup_tests.TripLookupTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.db, self.handler = self.case.db, self.case.handler

    def save(self, expenses):
        self.handler.post_data.return_value = {
            'driver_id': 1, 'vehicle_id': 1, 'data_saida': '2026-10-22',
            'orders': [{'order_number': '705335', 'invoice_number': '311929', 'weight_kg': 825}],
            'expenses': expenses,
        }
        return trips.api_save_trip(self.handler, {'id': 1})

    def test_two_overnights_at_sixty_are_saved_as_120(self):
        response, status = self.save({'overnight': {'qty': 2, 'unit_val': 60}})
        self.assertEqual(status, 200)
        row = self.db.execute('SELECT quantity,unit_value,total_value FROM trip_expenses WHERE route_id=?', (response['route_id'],)).fetchone()
        self.assertEqual(tuple(row), (2, 60, 120))
        raw = self.db.execute('SELECT expenses_json FROM routes WHERE id=?', (response['route_id'],)).fetchone()[0]
        self.assertEqual(json.loads(raw)['overnight'], {'qty': 2, 'unit_val': 60})

    def test_brazilian_currency_is_saved_as_numbers_with_cents(self):
        response, status = self.save({'lunch': {'qty': '2', 'unit_val': 'R$ 30,25'},
                                      'overnight': {'qty': '2', 'unit_val': 'R$ 60,00'},
                                      'fuel': {'litres': 20, 'total_val': '1.234,56'},
                                      'toll_val': '12,34', 'other': {'val': '0,10', 'desc': 'Complemento'}})
        self.assertEqual(status, 200)
        totals = {r['expense_type']: r['total_value'] for r in self.db.execute('SELECT expense_type,total_value FROM trip_expenses WHERE route_id=?', (response['route_id'],))}
        self.assertEqual(totals['Almoço'], 60.50)
        self.assertEqual(totals['Diária de Pernoite'], 120)
        self.assertEqual(totals['Combustível'], 1234.56)
        self.assertAlmostEqual(sum(totals.values()), 1427.50)

    def test_invalid_expenses_are_rejected_before_writing(self):
        invalid = [
            {'overnight': {'qty': 2, 'unit_val': 0}},
            {'overnight': {'qty': 0, 'unit_val': 60}},
            {'overnight': {'qty': -2, 'unit_val': 60}},
            {'toll_val': '-1,00'}, {'toll_val': 'abc'},
            {'toll_val': 'NaN'}, {'toll_val': 'Infinity'},
        ]
        for expenses in invalid:
            with self.subTest(expenses=expenses):
                before = self.db.total_changes
                response, status = self.save(expenses)
                self.assertEqual(status, 400)
                self.assertFalse(response['ok'])
                self.assertEqual(self.db.total_changes, before)

    def test_products_round_to_cents(self):
        self.assertEqual(trips._expense_product(3, 0.1), 0.3)
        self.assertEqual(trips._expense_product(2, 60.50), 121)
        self.assertEqual(trips._expense_number('1.000', 'despesa', money=True), 1000)
        self.assertEqual(trips._expense_number('R$ 1.234,56', 'despesa', money=True), 1234.56)


if __name__ == '__main__':
    unittest.main()
