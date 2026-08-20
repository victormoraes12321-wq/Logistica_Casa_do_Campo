# -*- coding: utf-8 -*-
from __future__ import annotations
import unittest
import sqlite3
from app import (
    conn,
    today,
    STATUSES,
    ORDER_STATUS_ALIASES,
    ORDER_ALLOWED_TRANSITIONS,
    order_sla_row_class,
    deadline_pill,
    normalize_order_status
)

class ScheduledStatusTests(unittest.TestCase):
    def test_scheduled_status_constants(self):
        # Validar se 'Agendado' está registrado nos status válidos
        self.assertIn('Agendado', STATUSES)
        
        # Validar aliases de status
        self.assertEqual(ORDER_STATUS_ALIASES.get('Agendado'), 'Agendado')
        self.assertEqual(ORDER_STATUS_ALIASES.get('Entrega agendada'), 'Agendado')
        self.assertEqual(ORDER_STATUS_ALIASES.get('Entrega agendada pelo cliente'), 'Agendado')
        self.assertEqual(ORDER_STATUS_ALIASES.get('Esperando agendamento'), 'Agendado')

    def test_scheduled_status_transitions(self):
        # Validar transições permitidas a partir dos status principais
        self.assertIn('Agendado', ORDER_ALLOWED_TRANSITIONS['Venda'])
        self.assertIn('Agendado', ORDER_ALLOWED_TRANSITIONS['Faturado'])
        self.assertIn('Agendado', ORDER_ALLOWED_TRANSITIONS['Saiu para entrega'])
        
        # Validar transições a partir de 'Agendado'
        self.assertEqual(
            ORDER_ALLOWED_TRANSITIONS['Agendado'],
            {'Venda', 'Faturado', 'Saiu para entrega', 'Acertado', 'Problema', 'Cancelado'}
        )

    def test_sla_exclusion_for_scheduled_orders(self):
        # Testar se a classe de linha de SLA para 'Agendado' é 'sla-scheduled'
        past_date = "2026-01-01"
        row_class = order_sla_row_class(past_date, 'Agendado')
        self.assertEqual(row_class, 'sla-scheduled')
        
        # Testar se a pílula de prazo de entrega exibe "Agendado" de forma neutra
        pill_html = deadline_pill(past_date, 'Agendado')
        self.assertIn('Agendado', pill_html)
        self.assertIn('class="deadline neutral"', pill_html)

if __name__ == "__main__":
    unittest.main()
