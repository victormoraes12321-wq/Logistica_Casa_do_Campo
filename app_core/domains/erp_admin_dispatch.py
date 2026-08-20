# -*- coding: utf-8 -*-
"""
app_core/domains/erp_admin_dispatch.py
======================================
Roteamento das rotas de administração da integração ERP.
Apenas usuários com perfil GOD podem acessar.
"""
from __future__ import annotations


def handle_get(handler, path: str, user) -> bool:
    if path == "/admin/erp":
        handler.get_erp_admin(user)
        return True
    if path == "/admin/erp/status":
        handler.get_erp_status_json(user)
        return True
    if path == "/admin/erp/test":
        handler.post_erp_test_connection(user)
        return True
    return False


def handle_post(handler, path: str, user) -> bool:
    if path == "/admin/erp":
        handler.post_erp_admin(user)
        return True
    if path == "/admin/erp/sync":
        handler.post_erp_force_sync(user)
        return True
    if path == "/admin/erp/test":
        handler.post_erp_test_connection(user)
        return True
    return False
