"""Dispatch modular por dominio para o runtime legado.

Mantem rotas e layout existentes, reduzindo acoplamento no app.py.
"""
from app_core.domains import (
    admin_dispatch,
    backup_dispatch,
    catalog_dispatch,
    driver_api_dispatch,
    erp_admin_dispatch,
    orders_dispatch,
    reports_dispatch,
    routes_dispatch,
)

__all__ = [
    "admin_dispatch",
    "backup_dispatch",
    "catalog_dispatch",
    "driver_api_dispatch",
    "erp_admin_dispatch",
    "orders_dispatch",
    "reports_dispatch",
    "routes_dispatch",
]
