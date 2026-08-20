from __future__ import annotations


def handle_get(handler, path: str, user) -> bool:
    if path == "/relatorios/export":
        if not handler.require_perm(user, "export_reports", "Sem permissão para exportar relatórios."):
            return True
        handler.export_csv(user)
        return True
    return False

