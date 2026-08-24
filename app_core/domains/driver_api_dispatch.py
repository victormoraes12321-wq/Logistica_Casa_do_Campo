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
            if st in ("Acertado", "Entregue") or ro_st == "Entregue":
                delivered_count += 1
            elif st in ("Problema", "Cancelado") or ro_st in ("Com problema", "Cancelado"):
                problem_count += 1

        now_ts = _now_str()
        u_id = user_info.get("id") if user_info else None
        u_name = user_info.get("name", "App Motorista Auto-Acerto") if user_info else "App Motorista Auto-Acerto"

        if problem_count > 0:
            db.execute("UPDATE routes SET status='Com problema', updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (now_ts, route_id))
            db.execute("""
                INSERT INTO audit_logs(created_at, user_id, user_name, source_ip, action, module, entity, notes)
                VALUES(?, ?, ?, '127.0.0.1', 'Status Carga Alterado', 'Motorista App', ?, 'Carga com entrega em problema detectada pelo App do Motorista')
            """, (now_ts, u_id, u_name, str(route_id)))
            db.commit()
            return False

        if delivered_count == total_count and total_count > 0:
            # 100% entregue sem problemas — Executa Auto-Acerto!
            db.execute("UPDATE routes SET status='Acertada', updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (now_ts, route_id))
            
            # Atualiza histórico de cada pedido
            for r_row in r_orders:
                oid = r_row["order_id"]
                db.execute("""
                    INSERT INTO order_history(order_id, user_id, old_status, new_status, action, notes, created_at)
                    VALUES(?, ?, 'Saiu para entrega', 'Acertado', 'AUTO_SETTLE', 'Acerto de carga finalizado automaticamente via App do Motorista', ?)
                """, (oid, u_id, now_ts))

            db.execute("""
                INSERT INTO audit_logs(created_at, user_id, user_name, source_ip, action, module, entity, notes)
                VALUES(?, ?, ?, '127.0.0.1', 'Auto-Acerto Concluído', 'Motorista App', ?, 'Carga 100% entregue e finalizada automaticamente via App do Motorista')
            """, (now_ts, u_id, u_name, str(route_id)))
            db.commit()
            logger.info("Auto-Acerto: Rota ID %d (%s) finalizada automaticamente com sucesso!", route_id, r_dict.get("name"))
            return True

    except Exception as exc:
        logger.error("Erro no auto-acerto da rota %s: %s", route_id, exc, exc_info=True)

    return False


def handle_driver_api_request(handler, path: str, method: str) -> bool:
    """
    Roteador de requisições da API REST do Motorista.
    """
    if not path.startswith("/api/v1/driver/"):
        return False

    # ---- 1. Login / Autenticação do Motorista ----
    if path == "/api/v1/driver/login" and method == "POST":
        try:
            data = handler.json_data() or {}
            username = str(data.get("username") or data.get("driver_name") or "").strip()
            password = str(data.get("password") or data.get("pin") or "").strip()

            if not username:
                return handler.send_json({"ok": False, "message": "Informe o nome ou login do motorista."}, 400)

            with handler.conn() as db:
                # Tenta buscar em usuarios
                u_row = db.execute("SELECT * FROM users WHERE LOWER(username)=LOWER(?) AND active=1", (username,)).fetchone()
                if not u_row:
                    # Tenta buscar em motoristas por nome ou documento
                    d_row = db.execute("SELECT * FROM drivers WHERE LOWER(name)=LOWER(?) AND active=1", (username,)).fetchone()
                    if d_row:
                        d_dict = dict(d_row)
                        return handler.send_json({
                            "ok": True,
                            "user": {
                                "id": d_dict["id"],
                                "name": d_dict["name"],
                                "role": "Motorista",
                                "is_driver": True
                            },
                            "message": "Autenticado com sucesso"
                        })
                    return handler.send_json({"ok": False, "message": "Motorista não encontrado ou inativo."}, 404)

                u_dict = dict(u_row)
                valid, _ = handler.verify_password(password, u_dict["password_hash"]) if password else (True, False)
                if not valid and password:
                    return handler.send_json({"ok": False, "message": "Senha incorreta."}, 401)

                return handler.send_json({
                    "ok": True,
                    "user": {
                        "id": u_dict["id"],
                        "name": u_dict["name"],
                        "role": u_dict["role"],
                        "is_driver": True
                    },
                    "message": "Autenticado com sucesso"
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

    # ---- 2. Lista de Rotas Ativas do Motorista ----
    if path == "/api/v1/driver/routes" and method == "GET":
        try:
            driver_name = handler.qs_get("driver_name") or handler.qs_get("driver") or ""
            with handler.conn() as db:
                query = """
                    SELECT r.*, d.name as driver_name, v.name as vehicle_name, v.plate,
                           COUNT(ro.id) as total_orders
                    FROM routes r
                    LEFT JOIN drivers d ON d.id = r.driver_id
                    LEFT JOIN vehicles v ON v.id = r.vehicle_id
                    LEFT JOIN route_orders ro ON ro.route_id = r.id
                    WHERE r.status IN ('Em rota', 'Planejada')
                """
                params = []
                if driver_name:
                    query += " AND (LOWER(d.name) LIKE LOWER(?) OR LOWER(r.name) LIKE LOWER(?))"
                    params.extend([f"%{driver_name}%", f"%{driver_name}%"])

                query += " GROUP BY r.id ORDER BY r.id DESC"
                routes = [dict(r) for r in db.execute(query, params).fetchall()]

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
                route = db.execute("SELECT * FROM routes WHERE id=?", (route_id,)).fetchone()
                if not route:
                    return handler.send_json({"ok": False, "message": "Carga não encontrada."}, 404)

                r_dict = dict(route)
                curr_status = str(r_dict.get("status") or "").strip()

                if curr_status == "Em rota":
                    return handler.send_json({"ok": True, "message": "Esta carga já está em rota de entrega!"})

                db.execute("UPDATE routes SET status='Em rota', updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (now_ts, route_id))

                # Atualiza também o status dos pedidos associados para 'Saiu para entrega'
                db.execute("""
                    UPDATE orders SET status='Saiu para entrega', updated_at=?, version=COALESCE(version,1)+1
                    WHERE id IN (SELECT order_id FROM route_orders WHERE route_id=?) AND status NOT IN ('Acertado', 'Entregue')
                """, (now_ts, route_id))

                db.execute("""
                    INSERT INTO audit_logs(created_at, source_ip, action, module, entity, notes)
                    VALUES(?, '127.0.0.1', 'Saída de Carga Iniciada', 'Motorista App', ?, 'Saída da carga iniciada diretamente pelo App do Motorista')
                """, (now_ts, str(route_id)))
                db.commit()

                # Notifica WebSocket/EventSource
                try:
                    from app_core.services.broker import GLOBAL_BROKER
                    GLOBAL_BROKER.publish("routes_updated", {"route_id": route_id, "status": "Em rota"})
                except Exception:
                    pass

                return handler.send_json({
                    "ok": True,
                    "route_id": route_id,
                    "status": "Em rota",
                    "message": "🚀 Saída da carga registrada com sucesso! Boa viagem."
                })
        except Exception as exc:
            logger.error("Erro ao marcar saída da carga: %s", exc)
            return handler.send_json({"ok": False, "message": str(exc)}, 500)

    # ---- 3. Detalhes de uma Rota / Paradas ----
    if path.startswith("/api/v1/driver/route/") and method == "GET":
        try:
            parts = path.split("/")
            route_id = int(parts[-1])
            with handler.conn() as db:
                route = db.execute("""
                    SELECT r.*, d.name as driver_name, d.phone as driver_phone,
                           v.name as vehicle_name, v.plate
                    FROM routes r
                    LEFT JOIN drivers d ON d.id = r.driver_id
                    LEFT JOIN vehicles v ON v.id = r.vehicle_id
                    WHERE r.id = ?
                """, (route_id,)).fetchone()

                if not route:
                    return handler.send_json({"ok": False, "message": "Rota não encontrada."}, 404)

                r_dict = dict(route)
                orders_rows = db.execute("""
                    SELECT ro.delivery_order, ro.status as route_order_status,
                           o.id as order_id, o.order_number, o.status as order_status,
                           o.client_id, o.total_value, o.weight_kg, o.delivery_address,
                           o.location_link, o.city, o.uf, o.notes, o.payment_method,
                           o.delivered_to, o.delivered_at, o.receipt_photo_at,
                           c.name as client_name, c.phone as client_phone, c.whatsapp as client_whatsapp,
                           c.farm_name, c.neighborhood, c.reference_point, c.address as client_full_address
                    FROM route_orders ro
                    JOIN orders o ON o.id = ro.order_id
                    LEFT JOIN clients c ON c.id = o.client_id
                    WHERE ro.route_id = ?
                    ORDER BY ro.delivery_order ASC, o.id ASC
                """, (route_id,)).fetchall()

                orders_list = []
                for o_row in orders_rows:
                    od = dict(o_row)
                    # Busca os produtos/itens do pedido
                    items_rows = db.execute("""
                        SELECT id, product_code, product_name, category, quantity, unit, weight_kg, notes
                        FROM order_items WHERE order_id=? ORDER BY id ASC
                    """, (od["order_id"],)).fetchall()
                    od["items"] = [dict(i) for i in items_rows]

                    # Verifica se possui foto de comprovante salva no banco
                    has_receipt = bool(od.get("receipt_photo_at"))
                    if not has_receipt:
                        rc_count = db.execute("SELECT COUNT(*) FROM delivery_receipts WHERE order_id=?", (od["order_id"],)).fetchone()[0]
                        has_receipt = rc_count > 0
                    od["has_receipt_photo"] = has_receipt
                    od["receipt_url"] = f"/api/v1/driver/receipt/{od['order_id']}" if has_receipt else None
                    orders_list.append(od)

                r_dict["orders"] = orders_list
                return handler.send_json({"ok": True, "route": r_dict})
        except Exception as exc:
            return handler.send_json({"ok": False, "message": str(exc)}, 500)

    # ---- 4. Baixa de Entrega / Comprovante (Foto salva no Banco de Dados) ----
    if path == "/api/v1/driver/deliver" and method == "POST":
        try:
            data = handler.json_data() or {}
            order_id = int(data.get("order_id") or 0)
            route_id = int(data.get("route_id") or 0)
            delivered_to = str(data.get("delivered_to") or "").strip()
            delivered_doc = str(data.get("delivered_document") or "").strip()
            payment_method = str(data.get("payment_method") or "").strip()
            final_notes = str(data.get("final_notes") or "").strip()
            photo_base64 = str(data.get("receipt_photo") or data.get("receipt_photo_base64") or "").strip()
            is_problem = bool(data.get("is_problem"))
            problem_type = str(data.get("problem_type") or "Outro motivo").strip()

            if not order_id:
                return handler.send_json({"ok": False, "message": "ID do pedido não informado."}, 400)

            now_ts = _now_str()

            with handler.conn() as db:
                order_row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
                if not order_row:
                    return handler.send_json({"ok": False, "message": "Pedido não encontrado."}, 404)

                o_dict = dict(order_row)
                old_status = o_dict.get("status", "Saiu para entrega")

                # Se veio foto em Base64, salva no banco de dados SQLite na tabela delivery_receipts
                image_saved = False
                if photo_base64:
                    # Remove cabeçalho data:image/...;base64, se presente
                    raw_b64 = photo_base64
                    if "," in raw_b64:
                        raw_b64 = raw_b64.split(",", 1)[1]

                    mime_type = "image/jpeg"
                    if photo_base64.startswith("data:image/png"):
                        mime_type = "image/png"

                    # Grava no banco de dados na tabela delivery_receipts
                    db.execute("""
                        INSERT INTO delivery_receipts(order_id, route_id, image_data, mime_type, created_at)
                        VALUES(?, ?, ?, ?, ?)
                    """, (order_id, route_id or None, raw_b64, mime_type, now_ts))

                    # Atualiza coluna na tabela orders
                    db.execute("""
                        UPDATE orders
                        SET receipt_photo=?, receipt_photo_at=?
                        WHERE id=?
                    """, (raw_b64[:200] + "...(salvo_no_banco)", now_ts, order_id))
                    image_saved = True

                if is_problem:
                    # Registra problema de entrega
                    new_status = "Problema"
                    db.execute("""
                        UPDATE orders
                        SET status='Problema', delivered_at=?, final_notes=?, updated_at=?, version=COALESCE(version,1)+1
                        WHERE id=?
                    """, (now_ts, f"PROBLEMA: {problem_type} - {final_notes}", now_ts, order_id))

                    if route_id:
                        db.execute("UPDATE route_orders SET status='Com problema' WHERE route_id=? AND order_id=?", (route_id, order_id))

                    db.execute("""
                        INSERT INTO delivery_problems(order_id, problem_type, description, created_at)
                        VALUES(?, ?, ?, ?)
                    """, (order_id, problem_type, final_notes or "Registrado via App Motorista", now_ts))

                    db.execute("""
                        INSERT INTO order_history(order_id, old_status, new_status, action, notes, created_at)
                        VALUES(?, ?, 'Problema', 'DRIVER_APP', ?, ?)
                    """, (order_id, old_status, f"Problema em campo: {problem_type}. {final_notes}", now_ts))
                else:
                    # Registra Entrega 100% Concluída
                    new_status = "Acertado"
                    db.execute("""
                        UPDATE orders
                        SET status='Acertado', delivered_to=?, delivered_document=?, delivered_at=?,
                            payment_method=COALESCE(NULLIF(?,''), payment_method),
                            final_notes=?, updated_at=?, version=COALESCE(version,1)+1
                        WHERE id=?
                    """, (delivered_to, delivered_doc, now_ts, payment_method, final_notes or "Entregue via App Motorista", now_ts, order_id))

                    if route_id:
                        db.execute("UPDATE route_orders SET status='Entregue' WHERE route_id=? AND order_id=?", (route_id, order_id))

                    db.execute("""
                        INSERT INTO order_history(order_id, old_status, new_status, action, notes, created_at)
                        VALUES(?, ?, 'Acertado', 'DRIVER_APP', ?, ?)
                    """, (order_id, old_status, f"Entrega concluída via App. Recebedor: {delivered_to}. Canhoto salvo no banco.", now_ts))

                db.commit()

                # Notifica canal de tempo real
                if hasattr(handler, "GLOBAL_BROKER") and handler.GLOBAL_BROKER:
                    handler.GLOBAL_BROKER.publish("orders_updated")
                    handler.GLOBAL_BROKER.publish("routes_updated")

                # Verifica se a carga inteira deve ser finalizada automaticamente (Auto-Acerto)
                auto_settled = False
                if route_id:
                    auto_settled = check_and_auto_settle_route(db, route_id)

                return handler.send_json({
                    "ok": True,
                    "message": "Baixa de entrega registrada com sucesso! Comprovante salvo no banco de dados.",
                    "order_id": order_id,
                    "status": new_status,
                    "image_saved_in_db": image_saved,
                    "route_auto_settled": auto_settled
                })
        except Exception as exc:
            logger.error("Erro na baixa de entrega pelo aplicativo: %s", exc, exc_info=True)
            return handler.send_json({"ok": False, "message": f"Erro no processamento: {exc}"}, 500)

    # ---- 5. Visualizar Foto do Comprovante Salva no Banco ----
    if path.startswith("/api/v1/driver/receipt/") and method == "GET":
        try:
            parts = path.split("/")
            order_id = int(parts[-1])
            with handler.conn() as db:
                rec = db.execute("""
                    SELECT image_data, mime_type FROM delivery_receipts
                    WHERE order_id = ?
                    ORDER BY id DESC LIMIT 1
                """, (order_id,)).fetchone()

                if not rec:
                    # Fallback para coluna em orders
                    o_row = db.execute("SELECT receipt_photo FROM orders WHERE id=?", (order_id,)).fetchone()
                    if o_row and o_row["receipt_photo"] and len(o_row["receipt_photo"]) > 100:
                        raw_b64 = o_row["receipt_photo"]
                        if "," in raw_b64:
                            raw_b64 = raw_b64.split(",", 1)[1]
                        img_bytes = base64.b64decode(raw_b64)
                        return handler.send_response_bytes(img_bytes, "image/jpeg")

                    return handler.send_json({"ok": False, "message": "Comprovante não encontrado para este pedido."}, 404)

                r_dict = dict(rec)
                raw_b64 = r_dict["image_data"]
                if "," in raw_b64:
                    raw_b64 = raw_b64.split(",", 1)[1]

                img_bytes = base64.b64decode(raw_b64)
                mime = r_dict.get("mime_type") or "image/jpeg"
                return handler.send_response_bytes(img_bytes, mime)
        except Exception as exc:
            return handler.send_json({"ok": False, "message": str(exc)}, 500)

    return False
