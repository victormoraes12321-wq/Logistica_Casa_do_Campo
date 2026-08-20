from __future__ import annotations


def handle_post(handler, path: str, user) -> bool:
    if path == "/clients":
        if not handler.require_perm(user, "manage_clients", "Sem permissão para gerenciar clientes."):
            return True
        handler.post_client(user)
        return True
    if path.startswith("/clients/") and path.endswith("/update"):
        if not handler.require_perm(user, "manage_clients", "Sem permissão para gerenciar clientes."):
            return True
        handler.post_client_update(user, handler.path_int(path, 2))
        return True
    if path.startswith("/clients/") and path.endswith("/toggle"):
        if not handler.require_perm(user, "manage_clients", "Sem permissão para gerenciar clientes."):
            return True
        handler.post_client_toggle(user, handler.path_int(path, 2))
        return True
    if path.startswith("/clients/") and path.endswith("/delete"):
        if not handler.require_perm(user, "manage_clients", "Sem permissão para apagar clientes."):
            return True
        handler.post_client_delete(user, handler.path_int(path, 2))
        return True

    if path == "/drivers":
        if not handler.require_perm(user, "manage_drivers", "Sem permissão para gerenciar motoristas."):
            return True
        handler.post_driver(user)
        return True
    if path.startswith("/drivers/") and path.endswith("/update"):
        if not handler.require_perm(user, "manage_drivers", "Sem permissão para gerenciar motoristas."):
            return True
        handler.post_driver_update(user, handler.path_int(path, 2))
        return True
    if path.startswith("/drivers/") and path.endswith("/toggle"):
        if not handler.require_perm(user, "manage_drivers", "Sem permissão para gerenciar motoristas."):
            return True
        handler.post_driver_toggle(user, handler.path_int(path, 2))
        return True
    if path.startswith("/drivers/") and path.endswith("/delete"):
        if not handler.require_perm(user, "manage_drivers", "Sem permissão para apagar motoristas."):
            return True
        handler.post_driver_delete(user, handler.path_int(path, 2))
        return True

    if path == "/vehicles":
        if not handler.require_perm(user, "manage_vehicles", "Sem permissão para gerenciar veículos."):
            return True
        handler.post_vehicle(user)
        return True
    if path.startswith("/vehicles/") and path.endswith("/update"):
        if not handler.require_perm(user, "manage_vehicles", "Sem permissão para gerenciar veículos."):
            return True
        handler.post_vehicle_update(user, handler.path_int(path, 2))
        return True
    if path.startswith("/vehicles/") and path.endswith("/toggle"):
        if not handler.require_perm(user, "manage_vehicles", "Sem permissão para gerenciar veículos."):
            return True
        handler.post_vehicle_toggle(user, handler.path_int(path, 2))
        return True
    if path.startswith("/vehicles/") and path.endswith("/delete"):
        if not handler.require_perm(user, "manage_vehicles", "Sem permissão para apagar veículos."):
            return True
        handler.post_vehicle_delete(user, handler.path_int(path, 2))
        return True

    if path == "/route-cities":
        if not handler.require_perm(user, "manage_route_catalog", "Sem permissão para gerenciar cidades/rotas-base."):
            return True
        handler.post_route_city(user)
        return True
    if path.startswith("/route-cities/") and path.endswith("/update"):
        if not handler.require_perm(user, "manage_route_catalog", "Sem permissão para gerenciar cidades/rotas-base."):
            return True
        handler.post_route_city_update(user, handler.path_int(path, 2))
        return True
    if path.startswith("/route-cities/") and path.endswith("/toggle"):
        if not handler.require_perm(user, "manage_route_catalog", "Sem permissão para gerenciar cidades/rotas-base."):
            return True
        handler.post_route_city_toggle(user, handler.path_int(path, 2))
        return True
    if path.startswith("/route-cities/") and path.endswith("/delete"):
        if not handler.require_perm(user, "manage_route_catalog", "Sem permissão para apagar cidades/rotas-base."):
            return True
        handler.post_route_city_delete(user, handler.path_int(path, 2))
        return True

    return False

