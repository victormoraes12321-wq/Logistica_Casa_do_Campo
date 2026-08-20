from __future__ import annotations


def handle_get(handler, path: str, user) -> bool:
    if path.startswith("/routes/") and path.split("/")[-1].isdigit():
        if not handler.require_perm(user, "view_routes", "Sem permissÃ£o para visualizar cargas/rotas."):
            return True
        handler.route_detail(user, handler.path_int(path, len(path.split("/")) - 1))
        return True
    if path.startswith("/load-settlement/") and path.endswith("/print-report"):
        if not handler.require_perm(user, "settle_routes", "Sem permissÃ£o para visualizar relatÃ³rios de carga."):
            return True
        handler.load_settlement_print_report(user, handler.path_int(path, 2))
        return True
    return False


def handle_post(handler, path: str, user) -> bool:
    if path == "/routes/new":
        if not handler.require_perm(user, "create_routes", "Sem permissÃ£o para criar cargas."):
            return True
        handler.post_route(user)
        return True
    if path.startswith("/routes/") and path.endswith("/reopen"):
        if not handler.require_perm(user, "cancel_routes", "Sem permissÃ£o para reabrir/cancelar cargas."):
            return True
        handler.post_route_reopen(user, handler.path_int(path, 2))
        return True
    if path.startswith("/routes/") and path.endswith("/cancel"):
        if not handler.require_perm(user, "cancel_routes", "Sem permissÃ£o para reabrir/cancelar cargas."):
            return True
        handler.post_route_cancel(user, handler.path_int(path, 2))
        return True
    if path.startswith("/routes/") and path.endswith("/dispatch"):
        if not handler.require_perm(user, "edit_routes", "Sem permissÃ£o para editar cargas."):
            return True
        handler.post_route_dispatch(user, handler.path_int(path, 2))
        return True
    if path.startswith("/routes/") and path.endswith("/update"):
        if not handler.require_perm(user, "edit_routes", "Sem permissÃ£o para editar cargas."):
            return True
        handler.post_route_update(user, handler.path_int(path, 2))
        return True
    if path.startswith("/routes/") and path.endswith("/finish"):
        if not handler.require_perm(user, "settle_routes", "Sem permissÃ£o para concluir cargas."):
            return True
        handler.post_route_finish(user, handler.path_int(path, 2))
        return True
    if path.startswith("/routes/") and path.endswith("/sequence"):
        if not handler.require_perm(user, "edit_routes", "Sem permissÃ£o para editar cargas."):
            return True
        handler.post_route_sequence(user, handler.path_int(path, 2))
        return True
    if path.startswith("/routes/") and path.endswith("/add"):
        if not handler.require_perm(user, "edit_routes", "Sem permissÃ£o para editar cargas."):
            return True
        handler.post_route_add_order(user, handler.path_int(path, 2))
        return True
    if path.startswith("/routes/") and "/remove/" in path:
        if not handler.require_perm(user, "edit_routes", "Sem permissÃ£o para editar cargas."):
            return True
        handler.post_route_remove_order(user, handler.path_int(path, 2), handler.path_int(path, 4))
        return True
    if path.startswith("/routes/") and path.endswith("/delete"):
        if not handler.require_perm(user, "cancel_routes", "Sem permissÃ£o para apagar cargas."):
            return True
        handler.post_route_delete(user, handler.path_int(path, 2))
        return True
    if path.startswith("/load-settlement/") and path.endswith("/set-date"):
        if not handler.require_perm(user, "settle_routes", "Sem permissão para alterar data da carga."):
            return True
        handler.post_load_settlement_set_date(user, handler.path_int(path, 2))
        return True
    if path.startswith("/load-settlement/") and path.endswith("/finish"):
        if not handler.require_perm(user, "settle_routes", "Sem permissÃ£o para concluir acerto de carga."):
            return True
        handler.post_load_settlement_finish(user, handler.path_int(path, 2))
        return True
    return False
