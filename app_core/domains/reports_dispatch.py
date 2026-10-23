from __future__ import annotations


def handle_get(handler, path: str, user) -> bool:
    if path == "/relatorios/export":
        if not handler.require_perm(user, "export_reports", "Sem permissão para exportar relatórios."):
            return True
        handler.export_csv(user)
        return True

    if path == "/relatorios/operacional/export-csv":
        if not handler.require_perm(user, "export_reports", "Sem permissão para exportar relatórios."):
            return True
        handler.export_operacional_csv(user)
        return True

    if path == "/relatorios/operacional/export-pdf":
        if not handler.require_perm(user, "export_reports", "Sem permissão para exportar relatórios."):
            return True
        handler.export_operacional_pdf(user)
        return True

    if path in ("/relatorios/pedidos/export-pdf", "/relatorios/agendador/generate-now", "/settings/reports/generate-now"):
        if not handler.require_perm(user, "export_reports", "Sem permissão para exportar relatórios."):
            return True
        handler.export_pedidos_pdf(user)
        return True

    if path == "/relatorios/motoristas/export-csv":
        if not handler.require_perm(user, "export_reports", "Sem permissão para exportar relatórios."):
            return True
        from app_core.domains.driver_reports_dispatch import export_driver_report_csv
        export_driver_report_csv(handler, user)
        return True

    if path == "/relatorios/motoristas/export-pdf":
        if not handler.require_perm(user, "export_reports", "Sem permissão para exportar relatórios."):
            return True
        from app_core.domains.driver_reports_dispatch import export_driver_report_pdf
        export_driver_report_pdf(handler, user)
        return True

    return False


def handle_post(handler, path: str, user) -> bool:
    if path in ("/relatorios/settings", "/relatorios/agendador/settings"):
        handler.post_report_settings(user)
        return True

    if path in ("/relatorios/agendador/generate-now", "/relatorios/settings/generate-now"):
        handler.generate_pdf_report_now(user)
        return True

    if path == "/relatorios/motoristas/export-csv":
        if not handler.require_perm(user, "export_reports", "Sem permissão para exportar relatórios."):
            return True
        from app_core.domains.driver_reports_dispatch import export_driver_report_csv
        export_driver_report_csv(handler, user)
        return True

    if path == "/relatorios/motoristas/export-pdf":
        if not handler.require_perm(user, "export_reports", "Sem permissão para exportar relatórios."):
            return True
        from app_core.domains.driver_reports_dispatch import export_driver_report_pdf
        export_driver_report_pdf(handler, user)
        return True

    return False



