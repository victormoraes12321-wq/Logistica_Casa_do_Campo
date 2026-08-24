# -*- coding: utf-8 -*-
"""
app_core/domains/driver_api_dispatch.py
========================================
API REST dedicada para o Aplicativo Android do Motorista.
Gerencia autenticação do motorista, lista de rotas/pedidos em campo,
upload e persistência de fotos de comprovante/canhoto no BANCO DE DADOS,
e acerto automático de cargas quando 100% entregues sem pendências.
"""
from __future__ import annotations

import json
import base64
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("logistica.driver_api")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _get_qs_param(handler: Any, param_name: str) -> str:
    """Extrai parâmetro de Query String diretamente da URL da requisição (handler.path)."""
    try:
        from urllib.parse import parse_qs, urlparse
        raw_path = str(getattr(handler, "path", "") or "")
        if raw_path and "?" in raw_path:
            parsed = parse_qs(urlparse(raw_path).query)
            vals = parsed.get(param_name, [])
            if vals:
                return str(vals[0]).strip()
    except Exception:
        pass
    return ""



def check_and_auto_settle_route(db: Any, route_id: int, user_info: dict[str, Any] | None = None) -> bool:
    """
    Verifica se todos os pedidos de uma carga foram entregues 100% sem problemas abertos.
    Se sim, promove automaticamente o status da rota para 'Acertada' e registra histórico/auditoria.
    """
    try:
        route = db.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
        if not route:
            return False
        
        r_dict = dict(route)
        curr_status = str(r_dict.get("status") or "").strip()
        if curr_status in ("Acertada", "Com problema", "Cancelada"):
            return False

        # Busca todos os pedidos da rota
        r_orders = db.execute("""
            SELECT ro.id as ro_id, ro.status as ro_status, o.id as order_id, o.status as order_status, o.order_number
            FROM route_orders ro
            JOIN orders o ON o.id = ro.order_id
            WHERE ro.route_id = ?
        """, (route_id,)).fetchall()

        if not r_orders:
            return False

        total_count = len(r_orders)
        delivered_count = 0
        problem_count = 0

        for r_row in r_orders:
            st = str(r_row["order_status"] or "").strip()
            ro_st = str(r_row["ro_status"] or "").strip()
            if st in ("Acertado", "Entrega concluída", "Entregue") or ro_st == "Entregue":
                delivered_count += 1
            elif st in ("Problema", "Entrega com problema") or ro_st == "Com problema":
                problem_count += 1

        # Se TODOS os pedidos foram entregues e nenhum tem problema pendente
        if total_count > 0 and delivered_count == total_count and problem_count == 0:
            now_ts = _now_str()
            db.execute("UPDATE routes SET status='Acertada', updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (now_ts, route_id))
            db.execute("UPDATE route_orders SET status='Entregue' WHERE route_id=?", (route_id,))
            
            # Registra auditoria/histórico do acerto automático
            u_name = (user_info.get("name") if user_info else None) or "Sistema (App Motorista Auto-Acerto)"
            u_id = (user_info.get("id") if user_info else None) or 1
            
            try:
                db.execute("""
                    INSERT INTO audit_logs (user_id, user_name, action, module, entity, old_value, new_value, notes, created_at)
                    VALUES (?, ?, 'Auto-Acerto de Carga Concluído', 'App Motorista', ?, 'Em rota', 'Acertada', ?, ?)
                """, (u_id, u_name, f"Carga #{route_id}", f"100% dos {total_count} pedidos entregues pelo motorista no App", now_ts))
            except Exception:
                pass

            db.commit()
            logger.info("🎉 Carga #%d promovida automaticamente para 'Acertada' (100%% entregue via App Motorista).", route_id)
            return True

        return False
    except Exception as exc:
        logger.error("Erro ao verificar auto-acerto da carga #%d: %s", route_id, exc)
        return False


def handle_driver_api_request(handler: Any, path: str, method: str) -> bool:
    """
    Roteador de requisições da API REST do Motorista.
    Retorna True se tratou a requisição, False caso contrário.
    """
    # ---- 1. Identificação / Validação do Motorista ----
    if path == "/api/v1/driver/login" and method == "POST":
        try:
            data = handler.json_data() or {}
            driver_name = str(data.get("driver_name") or data.get("name") or "").strip()
            pin = str(data.get("pin") or "").strip()

            if not driver_name:
                return handler.send_json({"ok": False, "message": "Informe o nome do motorista."}, 400)

            with handler.conn() as db:
                driver = db.execute("SELECT * FROM drivers WHERE LOWER(name)=LOWER(?) AND active=1", (driver_name,)).fetchone()
                
                if not driver:
                    # Se não encontrou por busca exata, tenta busca aproximada
                    driver = db.execute("SELECT * FROM drivers WHERE LOWER(name) LIKE LOWER(?) AND active=1", (f"%{driver_name}%",)).fetchone()

                if not driver:
                    return handler.send_json({"ok": False, "message": f"Motorista '{driver_name}' não encontrado no cadastro."}, 404)

                d_dict = dict(driver)
                return handler.send_json({
                    "ok": True,
                    "driver_id": d_dict["id"],
                    "driver_name": d_dict["name"],
                    "vehicle_default": d_dict.get("vehicle_default") or "",
                    "message": "Autenticação realizada com sucesso!"
                })
        except Exception as exc:
            logger.error("Erro no login do motorista: %s", exc)
            return handler.send_json({"ok": False, "message": f"Erro interno: {exc}"}, 500)

    # ---- 1B. Listar Todos os Motoristas Cadastrados ----
    if path == "/api/v1/driver/all_drivers" and method == "GET":
        try:
            with handler.conn() as db:
                drivers = db.execute("SELECT id, name, phone, document, vehicle_default FROM drivers WHERE active=1 ORDER BY name ASC").fetchall()
                drivers_list = [dict(d) for d in drivers]
                return handler.send_json({"ok": True, "drivers": drivers_list, "count": len(drivers_list)})
        except Exception as exc:
            return handler.send_json({"ok": False, "message": str(exc)}, 500)

    # ---- 1C. Cadastrar Novo Motorista pelo App ----
    if path == "/api/v1/driver/register" and method == "POST":
        try:
            data = handler.json_data() or {}
            name = str(data.get("name") or "").strip()
            phone = str(data.get("phone") or "").strip()
            document = str(data.get("document") or "").strip()
            vehicle_default = str(data.get("vehicle_default") or "").strip()

            if not name:
                return handler.send_json({"ok": False, "message": "Informe o nome do motorista."}, 400)

            now_ts = _now_str()
            with handler.conn() as db:
                # Verifica se motorista já existe por nome
                existing = db.execute("SELECT id FROM drivers WHERE LOWER(name)=LOWER(?) AND active=1", (name,)).fetchone()
                if existing:
                    return handler.send_json({
                        "ok": True,
                        "driver_id": existing["id"],
                        "name": name,
                        "already_existed": True,
                        "message": f"Motorista '{name}' já estava cadastrado."
                    })

                cursor = db.execute("""
                    INSERT INTO drivers(name, phone, document, vehicle_default, active, updated_at, version)
                    VALUES(?, ?, ?, ?, 1, ?, 1)
                """, (name, phone, document, vehicle_default, now_ts))
                new_id = cursor.lastrowid
                db.commit()

                return handler.send_json({
                    "ok": True,
                    "driver_id": new_id,
                    "name": name,
                    "already_existed": False,
                    "message": f"Motorista '{name}' cadastrado com sucesso no banco de dados!"
                })
        except Exception as exc:
            logger.error("Erro no cadastro de motorista pelo app: %s", exc)
            return handler.send_json({"ok": False, "message": str(exc)}, 500)

    # ---- 1D. Alteração de Senha / PIN do Motorista ----
    if path == "/api/v1/driver/change_password" and method == "POST":
        try:
            data = handler.json_data() or {}
            driver_name = str(data.get("driver_name") or "").strip()
            new_pin = str(data.get("new_pin") or "").strip()

            if not driver_name:
                return handler.send_json({"ok": False, "message": "Informe o nome do motorista."}, 400)

            if not new_pin:
                return handler.send_json({"ok": False, "message": "Informe a nova senha / PIN."}, 400)

            now_ts = _now_str()
            with handler.conn() as db:
                try:
                    db.execute("ALTER TABLE drivers ADD COLUMN pin TEXT DEFAULT ''")
                except Exception:
                    pass

                driver = db.execute("SELECT id FROM drivers WHERE LOWER(name)=LOWER(?) AND active=1", (driver_name,)).fetchone()
                if not driver:
                    return handler.send_json({"ok": False, "message": f"Motorista '{driver_name}' não encontrado."}, 404)

                db.execute("UPDATE drivers SET pin=?, updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (new_pin, now_ts, driver["id"]))
                db.commit()

                return handler.send_json({"ok": True, "message": "🔑 Senha / PIN alterado com sucesso no banco de dados!"})
        except Exception as exc:
            logger.error("Erro ao alterar senha do motorista: %s", exc)
            return handler.send_json({"ok": False, "message": str(exc)}, 500)

    # ---- 2. Lista de Rotas Ativas do Motorista ----
    if path == "/api/v1/driver/routes" and method == "GET":
        try:
            driver_name = _get_qs_param(handler, "driver_name") or _get_qs_param(handler, "driver") or ""
            with handler.conn() as db:
                base_query = """
                    SELECT r.*, d.name as driver_name, v.name as vehicle_name, v.plate,
                           COUNT(ro.id) as total_orders,
                           SUM(CASE WHEN ro.status = 'Entregue' OR o.status = 'Acertado' THEN 1 ELSE 0 END) as delivered_orders
                    FROM routes r
                    LEFT JOIN drivers d ON d.id = r.driver_id
                    LEFT JOIN vehicles v ON v.id = r.vehicle_id
                    LEFT JOIN route_orders ro ON ro.route_id = r.id
                    LEFT JOIN orders o ON o.id = ro.order_id
                    WHERE r.status IN ('Em rota', 'Planejada', 'Aguardando', 'Criada')
                """
                params = []
                if driver_name:
                    query = base_query + " AND (LOWER(d.name) LIKE LOWER(?) OR LOWER(r.name) LIKE LOWER(?)) GROUP BY r.id ORDER BY r.id DESC"
                    params.extend([f"%{driver_name}%", f"%{driver_name}%"])
                    routes = [dict(r) for r in db.execute(query, params).fetchall()]
                    
                    # Se não encontrou cargas específicas pelo filtro, busca todas as cargas ativas para garantir sincronia
                    if not routes:
                        query_all = base_query + " GROUP BY r.id ORDER BY r.id DESC"
                        routes = [dict(r) for r in db.execute(query_all).fetchall()]
                else:
                    query = base_query + " GROUP BY r.id ORDER BY r.id DESC"
                    routes = [dict(r) for r in db.execute(query).fetchall()]

                return handler.send_json({"ok": True, "routes": routes, "count": len(routes)})
        except Exception as exc:
            return handler.send_json({"ok": False, "message": str(exc)}, 500)

    # ---- 2B. Marcar Saída da Carga (Promove status para 'Em rota') ----
    if path == "/api/v1/driver/start_route" and method == "POST":
        try:
            data = handler.json_data() or {}
            route_id = int(data.get("route_id") or 0)
            if not route_id:
                return handler.send_json({"ok": False, "message": "ID da carga não informado."}, 400)

            now_ts = _now_str()
            with handler.conn() as db:
                route = db.execute("SELECT id, name, status FROM routes WHERE id=?", (route_id,)).fetchone()
                if not route:
                    return handler.send_json({"ok": False, "message": "Carga não encontrada."}, 404)

                db.execute("UPDATE routes SET status='Em rota', updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (now_ts, route_id))
                db.execute("UPDATE route_orders SET status='Em rota' WHERE route_id=? AND status='Pendente'", (route_id,))
                db.execute("""
                    UPDATE orders SET status='Saiu para entrega', updated_at=?, version=COALESCE(version,1)+1
                    WHERE id IN (SELECT order_id FROM route_orders WHERE route_id=?) AND status NOT IN ('Acertado', 'Problema', 'Cancelado')
                """, (now_ts, route_id))

                db.commit()
                return handler.send_json({"ok": True, "route_id": route_id, "status": "Em rota", "message": f"Saída da carga #{route_id} marcada com sucesso! Status alterado para 'Em rota'."})
        except Exception as exc:
            return handler.send_json({"ok": False, "message": str(exc)}, 500)

    # ---- 3. Detalhes de uma Rota Específica e seus Pedidos ----
    if path.startswith("/api/v1/driver/route/") and method == "GET":
        try:
            route_id_str = path.replace("/api/v1/driver/route/", "").strip()
            if not route_id_str.isdigit():
                return handler.send_json({"ok": False, "message": "ID de rota inválido."}, 400)

            route_id = int(route_id_str)

            with handler.conn() as db:
                r_row = db.execute("""
                    SELECT r.*, d.name as driver_name, d.phone as driver_phone, v.name as vehicle_name, v.plate
                    FROM routes r
                    LEFT JOIN drivers d ON d.id = r.driver_id
                    LEFT JOIN vehicles v ON v.id = r.vehicle_id
                    WHERE r.id = ?
                """, (route_id,)).fetchone()

                if not r_row:
                    return handler.send_json({"ok": False, "message": "Carga não encontrada."}, 404)

                route_dict = dict(r_row)

                # Busca todos os pedidos vinculados a esta rota
                orders_rows = db.execute("""
                    SELECT 
                        ro.id as route_order_id, ro.delivery_order, ro.status as route_order_status,
                        o.id as order_id, o.order_number, o.status as order_status, o.total_value, o.weight_kg,
                        o.expected_delivery_date, o.payment_method, o.notes as order_notes,
                        c.id as client_id, c.name as client_name, c.phone as client_phone, c.whatsapp as client_whatsapp,
                        c.farm_name, c.city, c.address as client_full_address, c.address as delivery_address,
                        c.reference_point
                    FROM route_orders ro
                    JOIN orders o ON o.id = ro.order_id
                    LEFT JOIN clients c ON c.id = o.client_id
                    WHERE ro.route_id = ?
                    ORDER BY ro.delivery_order ASC, o.id ASC
                """, (route_id,)).fetchall()

                orders_list = []
                for o in orders_rows:
                    o_dict = dict(o)
                    
                    # Checa se já existe comprovante/canhoto em foto salvo no banco
                    receipt = db.execute("SELECT id, created_at FROM delivery_receipts WHERE order_id = ?", (o_dict["order_id"],)).fetchone()
                    o_dict["has_receipt_photo"] = bool(receipt)
                    if receipt:
                        o_dict["receipt_created_at"] = receipt["created_at"]

                    # Busca itens/produtos do pedido
                    items = db.execute("""
                        SELECT product_name, quantity, unit, weight_kg
                        FROM order_items
                        WHERE order_id = ?
                        ORDER BY id ASC
                    """, (o_dict["order_id"],)).fetchall()
                    o_dict["items"] = [dict(it) for it in items]

                    orders_list.append(o_dict)

                route_dict["orders"] = orders_list
                return handler.send_json({"ok": True, "route": route_dict})
        except Exception as exc:
            logger.error("Erro ao buscar detalhes da rota #%s: %s", path, exc)
            return handler.send_json({"ok": False, "message": str(exc)}, 500)

    # ---- 4. Registrar Entrega / Foto de Comprovante (Canhoto) / Problema ----
    if path == "/api/v1/driver/deliver" and method == "POST":
        try:
            data = handler.json_data() or {}
            order_id = int(data.get("order_id") or 0)
            route_id = int(data.get("route_id") or 0) if data.get("route_id") else None
            delivered_to = str(data.get("delivered_to") or "").strip()
            delivered_doc = str(data.get("delivered_document") or "").strip()
            payment_method = str(data.get("payment_method") or "").strip()
            notes = str(data.get("final_notes") or "").strip()
            photo_b64 = str(data.get("receipt_photo") or "").strip()
            signature_b64 = str(data.get("digital_signature") or "").strip()
            is_problem = bool(data.get("is_problem", False))
            problem_type = str(data.get("problem_type") or "Outro").strip()

            if not order_id:
                return handler.send_json({"ok": False, "message": "ID do pedido não informado."}, 400)

            now_ts = _now_str()

            with handler.conn() as db:
                # 1. Se for registro de PROBLEMA/RECUSA de entrega
                if is_problem:
                    db.execute("""
                        UPDATE orders 
                        SET status='Problema', updated_at=?, version=COALESCE(version,1)+1
                        WHERE id=?
                    """, (now_ts, order_id))

                    db.execute("""
                        UPDATE route_orders
                        SET status='Com problema'
                        WHERE order_id=?
                    """, (order_id,))

                    # Registra na tabela de problemas
                    db.execute("""
                        INSERT INTO delivery_problems (order_id, route_id, problem_type, notes, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (order_id, route_id, problem_type, notes, now_ts))

                    db.commit()
                    return handler.send_json({
                        "ok": True,
                        "order_id": order_id,
                        "status": "Problema",
                        "message": f"Problema '{problem_type}' registrado com sucesso no sistema!"
                    })

                # 2. Se for BAIXA DE ENTREGA NORMAL (Sucesso)
                db.execute("""
                    UPDATE orders 
                    SET status='Acertado', updated_at=?, version=COALESCE(version,1)+1
                    WHERE id=?
                """, (now_ts, order_id))

                db.execute("""
                    UPDATE route_orders
                    SET status='Entregue'
                    WHERE order_id=?
                """, (order_id,))

                # Salva FOTO DO CANHOTO e ASSINATURA DIGITAL no banco de dados se enviada
                saved_in_db = False
                if photo_b64 or signature_b64:
                    raw_bytes = b""
                    sig_bytes = b""
                    if photo_b64:
                        if "," in photo_b64:
                            photo_b64 = photo_b64.split(",", 1)[1]
                        try:
                            raw_bytes = base64.b64decode(photo_b64)
                        except Exception:
                            pass
                    
                    if signature_b64:
                        if "," in signature_b64:
                            signature_b64 = signature_b64.split(",", 1)[1]
                        try:
                            sig_bytes = base64.b64decode(signature_b64)
                        except Exception:
                            pass

                    # Garantir que as colunas necessárias existem na tabela delivery_receipts
                    for col_def in ["digital_signature BLOB", "delivered_to TEXT", "delivered_document TEXT", "notes TEXT"]:
                        try:
                            db.execute(f"ALTER TABLE delivery_receipts ADD COLUMN {col_def}")
                        except Exception:
                            pass

                    # Deleta comprovante anterior se existir para substituir
                    db.execute("DELETE FROM delivery_receipts WHERE order_id=?", (order_id,))
                    db.execute("""
                        INSERT INTO delivery_receipts (order_id, image_data, digital_signature, delivered_to, delivered_document, notes, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (order_id, raw_bytes, sig_bytes, delivered_to, delivered_doc, notes, now_ts))
                    
                    # Atualiza flag no pedido
                    db.execute("UPDATE orders SET receipt_photo_at=? WHERE id=?", (now_ts, order_id))
                    saved_in_db = True

                db.commit()

                # Verifica se a carga foi 100% concluída para realizar AUTO-ACERTO AUTOMÁTICO
                auto_settled = False
                if route_id:
                    auto_settled = check_and_auto_settle_route(db, route_id)

                return handler.send_json({
                    "ok": True,
                    "order_id": order_id,
                    "status": "Acertado",
                    "image_saved_in_db": saved_in_db,
                    "route_auto_settled": auto_settled,
                    "message": "Entrega registrada com sucesso! Comprovante gravado no banco de dados."
                })
        except Exception as exc:
            logger.error("Erro no registro de entrega do pedido #%s: %s", data.get("order_id"), exc)
            return handler.send_json({"ok": False, "message": f"Erro interno ao salvar entrega: {exc}"}, 500)

    # Rota não tratada por esta API
    return False
