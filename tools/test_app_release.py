# -*- coding: utf-8 -*-
"""
tools/test_app_release.py
==========================
Validador de Release e Integração E2E para o App 'Logística Casa do Campo'.
Executa a validação das APIs REST do Motorista, Persistência no Banco, Cadastro e Auto-Acerto.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def run_release_tests():
    print("=" * 70)
    print(" [RELEASE GATE] VALIDANDO INTEGRACAO DO APP 'LOGISTICA CASA DO CAMPO'")
    print("=" * 70)
    
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName("tests.test_driver_api")

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    if result.wasSuccessful():
        print("\n" + "=" * 70)
        print(" [OK] TODOS OS TESTES DE INTEGRACAO DO APP PASSARAM COM 100% SUCESSO!")
        print("    - Login por hash, troca inicial, sessão e logout: OK")
        print("    - Isolamento de cargas por driver_id: OK")
        print("    - Entrega/problema transacional e idempotente: OK")
        print("    - Comprovante, rollback e fechamento automático: OK")
        print("=" * 70)
        return True
    else:
        print("\n[ERRO] FALHA NOS TESTES DE INTEGRACAO!")
        return False


if __name__ == "__main__":
    success = run_release_tests()
    sys.exit(0 if success else 1)
