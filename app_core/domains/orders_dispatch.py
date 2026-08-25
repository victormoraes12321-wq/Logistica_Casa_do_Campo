from __future__ import annotations


def handle_get(handler, path: str, user) -> bool:
    if path == "/orders/erp-lookup":
        if not handler.require_perm(user, "create_orders", "Sem permissão para buscar pedidos no ERP."):
            return True
        handler.handle_erp_lookup(user)
        return True
    if path.startswith("/orders/") and path.endswith("/receipt-image"):
        if not handler.require_perm(user, "view_orders", "Sem permissão para visualizar comprovantes."):
            return True
        parts = path.split("/")
        oid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        handler.get_receipt_image(user, oid)
        return True
    if path.startswith("/orders/") and path.endswith("/signature-image"):
        if not handler.require_perm(user, "view_orders", "Sem permissão para visualizar assinaturas."):
            return True
        parts = path.split("/")
        oid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        handler.get_signature_image(user, oid)
        return True
    if path.startswith("/orders/") and path.endswith("/edit"):
        if not handler.require_perm(user, "edit_orders", "Sem permissão para editar pedidos."):
            return True
        handler.order_edit(user, handler.path_int(path, 2))
        return True
    if path.startswith("/orders/") and path.split("/")[-1].isdigit():
        if not handler.require_perm(user, "view_orders", "Sem permissão para visualizar pedidos."):
            return True
        handler.order_detail(user, handler.path_int(path, len(path.split("/")) - 1))
        return True
    return False


def handle_post(handler, path: str, user) -> bool:
    if path == "/orders/new":
        if not handler.require_perm(user, "create_orders", "Sem permissão para criar pedidos."):
            return True
        handler.post_order_new(user)
        return True
    if path.startswith("/orders/") and path.endswith("/edit"):
        if not handler.require_perm(user, "edit_orders", "Sem permissão para editar pedidos."):
            return True
        handler.post_order_edit(user, handler.path_int(path, 2))
        return True
    if path.startswith("/orders/") and path.endswith("/reopen"):
        if not handler.require_perm(user, "cancel_orders", "Sem permissão para reabrir/cancelar pedidos."):
            return True
        handler.post_order_reopen(user, handler.path_int(path, 2))
        return True
    if path.startswith("/orders/") and path.endswith("/status"):
        if not handler.require_perm(user, "edit_orders", "Sem permissão para alterar status de pedidos."):
            return True
        handler.post_status(user, handler.path_int(path, 2))
        return True
    if path.startswith("/orders/") and path.endswith("/invoice"):
        if not handler.require_perm(user, "invoice_orders", "Sem permissão para faturar pedidos."):
            return True
        handler.post_invoice(user, handler.path_int(path, 2))
        return True
    if path.startswith("/orders/") and path.endswith("/deliver"):
        if not handler.require_perm(user, "settle_routes", "Sem permissão para concluir entregas."):
            return True
        handler.post_deliver(user, handler.path_int(path, 2))
        return True
    if path.startswith("/orders/") and path.endswith("/problem"):
        if not handler.require_perm(user, "register_delivery_problem", "Sem permissão para registrar problema de entrega."):
            return True
        handler.post_problem(user, handler.path_int(path, 2))
        return True
    if path.startswith("/orders/") and path.endswith("/delete"):
        if not handler.require_perm(user, "cancel_orders", "Sem permissão para apagar pedidos."):
            return True
        handler.post_order_delete(user, handler.path_int(path, 2))
        return True
    return False

