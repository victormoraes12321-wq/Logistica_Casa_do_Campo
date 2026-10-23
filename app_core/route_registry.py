from __future__ import annotations

# Mapa principal de rotas estáveis (sem alteração de URL pública).
GET_ROUTE_HANDLERS = {
    "/": "dashboard",
    "/dashboard": "dashboard",
    "/orders": "orders",
    "/orders/new": "order_new",
    "/clients": "clients",
    "/faturamento": "faturamento",
    "/expedicao": "expedicao",
    "/routes": "routes",
    "/routes/new": "route_new",
    "/registro-viagens": "registro_viagens",
    "/load-settlement": "load_settlement",
    "/sla": "sla",
    "/drivers": "drivers",
    "/vehicles": "vehicles",
    "/route-cities": "route_cities",
    "/relatorios": "relatorios",
    "/relatorio-viagens": "relatorio_viagens",
    "/map": "map_view",
    "/backup": "backup",
    "/settings": "settings",
}


GET_ROUTE_PERMISSIONS = {
    "/": "view_dashboard",
    "/dashboard": "view_dashboard",
    "/orders": "view_orders",
    "/orders/new": "create_orders",
    "/clients": "view_clients",
    "/faturamento": "invoice_orders",
    "/expedicao": "view_orders",
    "/routes": "view_routes",
    "/routes/new": "create_routes",
    "/registro-viagens": "create_routes",
    "/load-settlement": "settle_routes",
    "/sla": "view_sla",
    "/drivers": "view_drivers",
    "/vehicles": "view_vehicles",
    "/route-cities": "view_route_catalog",
    "/relatorios": "view_reports",
    "/map": "view_orders",
    "/backup": "view_backup",
    "/settings": "view_settings",
}
