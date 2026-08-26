# -*- coding: utf-8 -*-
"""API autenticada usada pelo aplicativo do motorista.

Somente a lista pública de motoristas e o login dispensam bearer token. A
identidade operacional sempre vem da sessão, nunca do corpo/query string, e
entregas são atômicas e idempotentes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any

from app_core.services.driver_security import (
    hash_driver_password,
    hash_session_token,
    is_expired,
    new_session_token,
    session_expiry,
    utc_iso,
    verify_driver_password,
)

logger = logging.getLogger("logistica.driver_api")
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_SIGNATURE_BYTES = 2 * 1024 * 1024
TERMINAL_ORDER_STATUSES = {"Acertado", "Problema", "Cancelado"}
TERMINAL_ROUTE_ORDER_STATUSES = {"Entregue", "Com problema"}
_LOGIN_ATTEMPTS: dict[str, dict[str, float | int]] = {}
_LOGIN_ATTEMPTS_LOCK = threading.Lock()


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_json_data(handler: Any) -> dict[str, Any]:
    for attr in ("post_data", "json_data"):
        try:
            fn = getattr(handler, attr, None)
            result = fn() if callable(fn) else None
            if isinstance(result, dict):
                return result
        except Exception:
            continue
    return {}


def _header(handler: Any, name: str) -> str:
    try:
        headers = getattr(handler, "headers", None)
        if headers is not None:
            return str(headers.get(name) or headers.get(name.lower()) or "").strip()
    except Exception:
        pass
    return ""


def _bearer_token(handler: Any) -> str:
    value = _header(handler, "Authorization")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def _client_ip(handler: Any) -> str:
    try:
        fn = getattr(handler, "client_ip", None)
        return str(fn() if callable(fn) else "")[:128]
    except Exception:
        return ""


def _image_mime(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _decode_data_url(value: Any, *, max_bytes: int, label: str, allowed_mimes: set[str]) -> tuple[bytes, str]:
    encoded = str(value or "").strip()
    if not encoded:
        return b"", ""
    if "," in encoded:
        encoded = encoded.split(",", 1)[1]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} inválida.") from exc
    if len(raw) > max_bytes:
        raise ValueError(f"{label} excede o limite permitido.")
    mime = _image_mime(raw)
    if mime not in allowed_mimes:
        raise ValueError(f"{label} não contém uma imagem suportada.")
    return raw, mime


def _login_limits() -> tuple[int, int]:
    try:
        failures = max(2, int(os.environ.get("DRIVER_LOGIN_MAX_FAILURES", "5") or "5"))
    except ValueError:
        failures = 5
    try:
        lock_seconds = max(30, int(os.environ.get("DRIVER_LOGIN_LOCK_SECONDS", "300") or "300"))
    except ValueError:
        lock_seconds = 300
    return failures, lock_seconds


def _login_rate_key(handler: Any, driver_id: int, driver_name: str) -> str:
    identity = str(driver_id) if driver_id else driver_name.casefold()
    return f"{_client_ip(handler) or 'unknown'}|{identity}"[:512]


def _login_rate_status(key: str) -> int:
    now_mono = time.monotonic()
    with _LOGIN_ATTEMPTS_LOCK:
        for old_key, entry in list(_LOGIN_ATTEMPTS.items()):
            if now_mono - float(entry.get("updated_at", 0)) > 3600:
                _LOGIN_ATTEMPTS.pop(old_key, None)
        entry = _LOGIN_ATTEMPTS.get(key) or {}
        locked_until = float(entry.get("locked_until", 0))
        return max(0, int(locked_until - now_mono))


def _record_login_result(key: str, success: bool) -> None:
    now_mono = time.monotonic()
    with _LOGIN_ATTEMPTS_LOCK:
        if success:
            _LOGIN_ATTEMPTS.pop(key, None)
            return
        max_failures, lock_seconds = _login_limits()
        entry = _LOGIN_ATTEMPTS.setdefault(key, {"failures": 0, "locked_until": 0.0, "updated_at": now_mono})
        failures = int(entry.get("failures", 0)) + 1
        entry.update({"failures": failures, "updated_at": now_mono})
        if failures >= max_failures:
            entry["locked_until"] = now_mono + lock_seconds


def _request_fingerprint(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _authenticate_driver(handler: Any, *, allow_password_change: bool = False) -> dict[str, Any] | None:
    token = _bearer_token(handler)
    if not token:
        handler.send_json({"ok": False, "code": "authentication_required", "message": "Faça login novamente."}, 401)
        return None
    try:
        with handler.conn() as db:
            row = db.execute(
                """
                SELECT d.id,d.name,d.vehicle_default,d.active,d.must_change_password,
                       s.id AS session_id,s.expires_at,s.revoked_at
                FROM driver_sessions s JOIN drivers d ON d.id=s.driver_id
                WHERE s.token_hash=?
                """,
                (hash_session_token(token),),
            ).fetchone()
            if not row or int(row["active"] or 0) != 1 or row["revoked_at"]:
                handler.send_json({"ok": False, "code": "invalid_session", "message": "Sessão inválida. Faça login novamente."}, 401)
                return None
            if is_expired(str(row["expires_at"] or "")):
                db.execute("UPDATE driver_sessions SET revoked_at=? WHERE id=?", (utc_iso(), row["session_id"]))
                db.commit()
                handler.send_json({"ok": False, "code": "session_expired", "message": "Sua sessão expirou. Faça login novamente."}, 401)
                return None
            if int(row["must_change_password"] or 0) and not allow_password_change:
                handler.send_json({"ok": False, "code": "password_change_required", "message": "Altere a senha inicial antes de continuar."}, 403)
                return None
            db.execute("UPDATE driver_sessions SET last_seen_at=? WHERE id=?", (utc_iso(), row["session_id"]))
            db.commit()
            return dict(row)
    except Exception:
        logger.exception("Falha ao validar sessão do motorista")
        handler.send_json({"ok": False, "code": "authentication_unavailable", "message": "Não foi possível validar a sessão agora."}, 503)
        return None


def _audit(db: Any, driver: dict[str, Any], action: str, entity: str, notes: str = "") -> None:
    try:
        db.execute(
            "INSERT INTO audit_logs(user_id,user_name,action,module,entity,notes,created_at) VALUES(NULL,?,?,?,?,?,?)",
            (driver["name"], action, "App Motorista", entity, notes, _now_str()),
        )
    except Exception:
        logger.warning("Falha ao registrar auditoria do app", exc_info=True)


def _order_history(
    db: Any,
    driver: dict[str, Any],
    order_id: int,
    old_status: str,
    new_status: str,
    action: str,
    notes: str = "",
) -> None:
    db.execute(
        """INSERT INTO order_history(order_id,user_id,old_status,new_status,action,notes,created_at)
           VALUES(?,NULL,?,?,?,?,?)""",
        (order_id, old_status, new_status, action, f"Motorista: {driver['name']}. {notes}".strip(), _now_str()),
    )


def reconcile_route_status(db: Any, route_id: int, driver: dict[str, Any] | None = None) -> str | None:
    """Finaliza a carga somente quando todos os pedidos estão terminais."""
    route = db.execute("SELECT id,status FROM routes WHERE id=?", (route_id,)).fetchone()
    if not route or str(route["status"] or "") in {"Acertada", "Com problema", "Cancelada"}:
        return None
    rows = db.execute(
        "SELECT o.status order_status,ro.status route_order_status FROM route_orders ro JOIN orders o ON o.id=ro.order_id WHERE ro.route_id=?",
        (route_id,),
    ).fetchall()
    if not rows:
        return None
    all_terminal = all(
        str(row["order_status"] or "") in TERMINAL_ORDER_STATUSES
        or str(row["route_order_status"] or "") in TERMINAL_ROUTE_ORDER_STATUSES
        for row in rows
    )
    if not all_terminal:
        return None
    has_problem = any(
        str(row["order_status"] or "") == "Problema"
        or str(row["route_order_status"] or "") == "Com problema"
        for row in rows
    )
    new_status = "Com problema" if has_problem else "Acertada"
    db.execute(
        "UPDATE routes SET status=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",
        (new_status, _now_str(), route_id),
    )
    if driver:
        _audit(db, driver, "Finalização automática de carga", f"Carga #{route_id}", f"Status final: {new_status}")
    return new_status


def check_and_auto_settle_route(db: Any, route_id: int, user_info: dict[str, Any] | None = None) -> bool:
    """Compatibilidade com chamadas legadas; não efetua commit isolado."""
    return reconcile_route_status(db, route_id, user_info) == "Acertada"


def _login(handler: Any) -> bool:
    data = _get_json_data(handler)
    password = str(data.get("password") or data.get("pin") or "")
    try:
        driver_id = int(data.get("driver_id") or 0)
    except (TypeError, ValueError):
        driver_id = 0
    driver_name = str(data.get("driver_name") or data.get("name") or "").strip()
    if not driver_id and not driver_name:
        return handler.send_json({"ok": False, "message": "Selecione o motorista."}, 400)
    if not password:
        return handler.send_json({"ok": False, "message": "Informe a senha."}, 400)
    rate_key = _login_rate_key(handler, driver_id, driver_name)
    retry_after = _login_rate_status(rate_key)
    if retry_after:
        return handler.send_json({
            "ok": False,
            "code": "login_rate_limited",
            "message": "Muitas tentativas inválidas. Aguarde antes de tentar novamente.",
            "retry_after_seconds": retry_after,
        }, 429)
    try:
        with handler.conn() as db:
            if driver_id:
                driver = db.execute("SELECT * FROM drivers WHERE id=? AND active=1", (driver_id,)).fetchone()
            else:
                driver = db.execute("SELECT * FROM drivers WHERE LOWER(name)=LOWER(?) AND active=1", (driver_name,)).fetchone()
            if not driver or not verify_driver_password(password, str(driver["password_hash"] or "")):
                _record_login_result(rate_key, False)
                return handler.send_json({"ok": False, "code": "invalid_credentials", "message": "Motorista ou senha inválidos."}, 401)
            _record_login_result(rate_key, True)
            token = new_session_token()
            created_at = utc_iso()
            expires_at = session_expiry(int(os.environ.get("DRIVER_SESSION_HOURS", "24") or "24"))
            db.execute(
                "INSERT INTO driver_sessions(driver_id,token_hash,created_at,expires_at,last_seen_at,client_ip) VALUES(?,?,?,?,?,?)",
                (driver["id"], hash_session_token(token), created_at, expires_at, created_at, _client_ip(handler)),
            )
            db.commit()
            return handler.send_json({
                "ok": True, "token": token, "token_type": "Bearer", "expires_at": expires_at,
                "driver": {"id": driver["id"], "name": driver["name"]},
                "driver_id": driver["id"], "driver_name": driver["name"],
                "must_change_password": bool(driver["must_change_password"]),
            })
    except Exception:
        logger.exception("Erro no login do motorista")
        return handler.send_json({"ok": False, "message": "Não foi possível fazer login agora."}, 500)


def _deliver(handler: Any, driver: dict[str, Any]) -> bool:
    data = _get_json_data(handler)
    try:
        order_id = int(data.get("order_id") or 0)
        route_id = int(data.get("route_id") or 0)
    except (TypeError, ValueError):
        return handler.send_json({"ok": False, "message": "Pedido ou carga inválidos."}, 400)
    idempotency_key = str(data.get("idempotency_key") or _header(handler, "Idempotency-Key") or "").strip()
    if not order_id or not route_id:
        return handler.send_json({"ok": False, "message": "Pedido e carga são obrigatórios."}, 400)
    if not idempotency_key or len(idempotency_key) > 128:
        return handler.send_json({"ok": False, "code": "idempotency_key_required", "message": "Chave de idempotência inválida ou ausente."}, 422)
    is_problem = bool(data.get("is_problem", False))
    notes = str(data.get("final_notes") or "").strip()[:4000]
    problem_type = str(data.get("problem_type") or "Outro").strip()[:120]
    delivered_to = str(data.get("delivered_to") or "").strip()[:200]
    delivered_doc = str(data.get("delivered_document") or "").strip()[:80]
    delivered_doc_type = str(data.get("delivered_document_type") or "CPF").strip()[:30]
    lat_val = data.get("latitude") if data.get("latitude") is not None else data.get("lat")
    lng_val = data.get("longitude") if data.get("longitude") is not None else data.get("lng")
    latitude = None
    longitude = None
    location_link = ""
    try:
        if lat_val is not None and str(lat_val).strip() != "":
            latitude = float(lat_val)
        if lng_val is not None and str(lng_val).strip() != "":
            longitude = float(lng_val)
        if latitude is not None and longitude is not None and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            location_link = f"https://www.google.com/maps?q={latitude:.6f},{longitude:.6f}"
        else:
            latitude = None
            longitude = None
            location_link = ""
    except (ValueError, TypeError):
        latitude = None
        longitude = None
        location_link = ""
    if is_problem and not notes:
        return handler.send_json({"ok": False, "message": "Descreva o motivo do problema."}, 400)
    try:
        photo, photo_mime = _decode_data_url(
            data.get("receipt_photo"),
            max_bytes=MAX_PHOTO_BYTES,
            label="Foto",
            allowed_mimes={"image/jpeg", "image/png", "image/webp"},
        )
        signature, _ = _decode_data_url(
            data.get("digital_signature"),
            max_bytes=MAX_SIGNATURE_BYTES,
            label="Assinatura",
            allowed_mimes={"image/png"},
        )
    except ValueError as exc:
        return handler.send_json({"ok": False, "message": str(exc)}, 400)
    if not is_problem and not photo and not signature:
        return handler.send_json({"ok": False, "message": "Inclua a foto do comprovante ou a assinatura."}, 400)
    fingerprint = _request_fingerprint(data)
    now_ts = _now_str()
    try:
        with handler.conn() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT request_hash,status,response_json FROM driver_delivery_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous:
                if str(previous["request_hash"]) != fingerprint:
                    db.rollback()
                    return handler.send_json({"ok": False, "code": "idempotency_conflict", "message": "Esta chave já foi usada com dados diferentes."}, 409)
                if previous["status"] == "completed" and previous["response_json"]:
                    response = json.loads(previous["response_json"])
                    response["idempotent_replay"] = True
                    db.rollback()
                    return handler.send_json(response)
                db.rollback()
                return handler.send_json({"ok": False, "code": "operation_in_progress", "message": "A operação ainda está sendo processada."}, 409)
            route = db.execute("SELECT id,status,driver_id FROM routes WHERE id=?", (route_id,)).fetchone()
            if not route:
                db.rollback()
                return handler.send_json({"ok": False, "message": "Carga não encontrada."}, 404)
            if int(route["driver_id"] or 0) != int(driver["id"]):
                db.rollback()
                return handler.send_json({"ok": False, "code": "forbidden_route", "message": "Esta carga pertence a outro motorista."}, 403)
            if str(route["status"] or "") != "Em rota":
                db.rollback()
                return handler.send_json({"ok": False, "code": "route_not_in_progress", "message": "A carga precisa estar Em rota."}, 409)
            linked = db.execute(
                "SELECT ro.status route_order_status,o.status order_status FROM route_orders ro JOIN orders o ON o.id=ro.order_id WHERE ro.route_id=? AND ro.order_id=?",
                (route_id, order_id),
            ).fetchone()
            if not linked:
                db.rollback()
                return handler.send_json({"ok": False, "code": "order_not_in_route", "message": "O pedido não pertence a esta carga."}, 403)
            if str(linked["order_status"] or "") in TERMINAL_ORDER_STATUSES or str(linked["route_order_status"] or "") in TERMINAL_ROUTE_ORDER_STATUSES:
                db.rollback()
                return handler.send_json({"ok": False, "code": "order_already_finalized", "message": "Este pedido já foi finalizado."}, 409)
            operation_type = "problem" if is_problem else "delivery"
            cursor = db.execute(
                """INSERT INTO driver_delivery_operations(
                       idempotency_key,driver_id,route_id,order_id,operation_type,request_hash,status,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (idempotency_key, driver["id"], route_id, order_id, operation_type, fingerprint, "processing", utc_iso()),
            )
            operation_id = cursor.lastrowid
            if is_problem:
                db.execute(
                    """UPDATE orders SET status='Problema',final_notes=?,delivery_latitude=?,delivery_longitude=?,
                              delivery_location_link=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?""",
                    (notes, latitude, longitude, location_link, now_ts, order_id),
                )
                db.execute("UPDATE route_orders SET status='Com problema' WHERE route_id=? AND order_id=?", (route_id, order_id))
                db.execute("INSERT INTO delivery_problems(order_id,route_id,problem_type,description,created_at) VALUES(?,?,?,?,?)", (order_id, route_id, problem_type, notes, now_ts))
                status = "Problema"
                _order_history(
                    db, driver, order_id, str(linked["order_status"] or ""), status,
                    "Problema registrado pelo app", f"{problem_type}: {notes}",
                )
                _audit(db, driver, "Problema de entrega", f"Pedido #{order_id}", problem_type)
            else:
                db.execute(
                    """UPDATE orders SET status='Acertado',delivered_to=?,delivered_document=?,delivered_document_type=?,delivered_at=?,
                              final_notes=?,receipt_photo_at=?,delivery_latitude=?,delivery_longitude=?,
                              delivery_location_link=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?""",
                    (delivered_to, delivered_doc, delivered_doc_type, now_ts, notes, now_ts, latitude, longitude, location_link, now_ts, order_id),
                )
                db.execute("UPDATE route_orders SET status='Entregue' WHERE route_id=? AND order_id=?", (route_id, order_id))
                db.execute("DELETE FROM delivery_receipts WHERE route_id=? AND order_id=?", (route_id, order_id))
                db.execute(
                    """INSERT INTO delivery_receipts(order_id,route_id,image_data,mime_type,digital_signature,
                              delivered_to,delivered_document,delivered_document_type,notes,latitude,longitude,delivery_location_link,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (order_id, route_id, photo, photo_mime or "image/jpeg", signature, delivered_to, delivered_doc, delivered_doc_type, notes, latitude, longitude, location_link, now_ts),
                )
                status = "Acertado"
                _order_history(
                    db, driver, order_id, str(linked["order_status"] or ""), status,
                    "Entrega confirmada pelo app", notes,
                )
                _audit(db, driver, "Entrega concluída", f"Pedido #{order_id}")
            final_route_status = reconcile_route_status(db, route_id, driver)
            response = {
                "ok": True, "operation_id": operation_id, "idempotency_key": idempotency_key,
                "order_id": order_id, "route_id": route_id, "status": status,
                "image_saved_in_db": bool(photo or signature) and not is_problem,
                "route_status": final_route_status or "Em rota",
                "route_auto_settled": final_route_status == "Acertada",
                "message": "Problema registrado com segurança." if is_problem else "Entrega registrada com segurança.",
            }
            db.execute(
                "UPDATE driver_delivery_operations SET status='completed',response_json=?,completed_at=? WHERE id=?",
                (json.dumps(response, ensure_ascii=False), utc_iso(), operation_id),
            )
            db.commit()
            return handler.send_json(response)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        logger.info(
            "Cliente desconectou durante a resposta da operação do pedido %s na carga %s; "
            "a idempotência preserva o resultado persistido.",
            order_id,
            route_id,
        )
        return True
    except Exception:
        logger.exception("Falha transacional ao finalizar pedido %s da carga %s", order_id, route_id)
        return handler.send_json({"ok": False, "code": "delivery_failed", "message": "Não foi possível registrar a operação. Nenhuma alteração parcial foi mantida."}, 500)


def handle_driver_api_request(handler: Any, path: str, method: str) -> bool:
    if path in ("/api/v1/driver/all_drivers", "/api/v1/driver/drivers", "/api/v1/driver/list") and method == "GET":
        try:
            with handler.conn() as db:
                rows = db.execute("SELECT id,name FROM drivers WHERE active=1 ORDER BY name").fetchall()
                return handler.send_json({"ok": True, "drivers": [dict(row) for row in rows], "count": len(rows)})
        except Exception:
            logger.exception("Erro ao listar motoristas")
            return handler.send_json({"ok": False, "message": "Não foi possível listar os motoristas."}, 500)
    if path == "/api/v1/driver/login" and method == "POST":
        return _login(handler)
    if path == "/api/v1/driver/register" and method == "POST":
        return handler.send_json({"ok": False, "code": "registration_disabled", "message": "Cadastre motoristas na área administrativa."}, 403)
    if path == "/api/v1/driver/logout" and method == "POST":
        driver = _authenticate_driver(handler, allow_password_change=True)
        if not driver:
            return True
        with handler.conn() as db:
            db.execute("UPDATE driver_sessions SET revoked_at=? WHERE id=?", (utc_iso(), driver["session_id"]))
            db.commit()
        return handler.send_json({"ok": True, "message": "Sessão encerrada."})
    if path == "/api/v1/driver/change_password" and method == "POST":
        driver = _authenticate_driver(handler, allow_password_change=True)
        if not driver:
            return True
        data = _get_json_data(handler)
        new_password = str(data.get("new_password") or data.get("new_pin") or "")
        if len(new_password.strip()) < 8:
            return handler.send_json({"ok": False, "message": "A nova senha deve ter pelo menos 8 caracteres."}, 400)
        with handler.conn() as db:
            db.execute(
                "UPDATE drivers SET password_hash=?,must_change_password=0,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",
                (hash_driver_password(new_password), _now_str(), driver["id"]),
            )
            db.execute("UPDATE driver_sessions SET revoked_at=? WHERE driver_id=? AND id<>? AND revoked_at IS NULL", (utc_iso(), driver["id"], driver["session_id"]))
            db.commit()
        return handler.send_json({"ok": True, "must_change_password": False, "message": "Senha alterada com sucesso."})
    driver = _authenticate_driver(handler)
    if not driver:
        return True
    if path == "/api/v1/driver/me" and method == "GET":
        return handler.send_json({"ok": True, "driver": {"id": driver["id"], "name": driver["name"]}, "expires_at": driver["expires_at"]})
    if path == "/api/v1/driver/routes" and method == "GET":
        try:
            with handler.conn() as db:
                rows = db.execute(
                    """SELECT r.*,d.name driver_name,v.name vehicle_name,v.plate,COUNT(ro.id) total_orders,
                              SUM(CASE WHEN ro.status='Entregue' OR o.status='Acertado' THEN 1 ELSE 0 END) delivered_orders,
                              SUM(CASE WHEN ro.status='Com problema' OR o.status='Problema' THEN 1 ELSE 0 END) problem_orders
                       FROM routes r JOIN drivers d ON d.id=r.driver_id LEFT JOIN vehicles v ON v.id=r.vehicle_id
                       LEFT JOIN route_orders ro ON ro.route_id=r.id LEFT JOIN orders o ON o.id=ro.order_id
                       WHERE r.driver_id=? AND r.status IN ('Planejada','Em rota') GROUP BY r.id ORDER BY r.id DESC""",
                    (driver["id"],),
                ).fetchall()
                return handler.send_json({"ok": True, "routes": [dict(row) for row in rows], "count": len(rows)})
        except Exception:
            logger.exception("Erro ao listar cargas do motorista %s", driver["id"])
            return handler.send_json({"ok": False, "message": "Não foi possível carregar suas cargas."}, 500)
    if path == "/api/v1/driver/start_route" and method == "POST":
        data = _get_json_data(handler)
        try:
            route_id = int(data.get("route_id") or 0)
        except (TypeError, ValueError):
            route_id = 0
        if not route_id:
            return handler.send_json({"ok": False, "message": "Carga inválida."}, 400)
        try:
            with handler.conn() as db:
                db.execute("BEGIN IMMEDIATE")
                route = db.execute("SELECT id,status,driver_id FROM routes WHERE id=?", (route_id,)).fetchone()
                if not route:
                    db.rollback()
                    return handler.send_json({"ok": False, "message": "Carga não encontrada."}, 404)
                if int(route["driver_id"] or 0) != int(driver["id"]):
                    db.rollback()
                    return handler.send_json({"ok": False, "code": "forbidden_route", "message": "Esta carga pertence a outro motorista."}, 403)
                if route["status"] == "Em rota":
                    db.rollback()
                    return handler.send_json({"ok": True, "route_id": route_id, "status": "Em rota", "already_started": True})
                if route["status"] != "Planejada":
                    db.rollback()
                    return handler.send_json({"ok": False, "message": "Somente cargas Planejadas podem iniciar."}, 409)
                now_ts = _now_str()
                db.execute("UPDATE routes SET status='Em rota',updated_at=?,version=COALESCE(version,1)+1 WHERE id=?", (now_ts, route_id))
                db.execute("UPDATE route_orders SET status='Em rota' WHERE route_id=? AND status='Pendente'", (route_id,))
                db.execute(
                    """UPDATE orders SET status='Saiu para entrega',updated_at=?,version=COALESCE(version,1)+1
                       WHERE id IN (SELECT order_id FROM route_orders WHERE route_id=?)
                         AND status NOT IN ('Acertado','Problema','Cancelado')""",
                    (now_ts, route_id),
                )
                _audit(db, driver, "Saída da carga", f"Carga #{route_id}")
                db.commit()
                return handler.send_json({"ok": True, "route_id": route_id, "status": "Em rota", "message": "Saída registrada."})
        except Exception:
            logger.exception("Erro ao iniciar carga %s", route_id)
            return handler.send_json({"ok": False, "message": "Não foi possível iniciar a carga."}, 500)
    if path.startswith("/api/v1/driver/route/") and method == "GET":
        route_id_text = path.rsplit("/", 1)[-1]
        if not route_id_text.isdigit():
            return handler.send_json({"ok": False, "message": "Carga inválida."}, 400)
        route_id = int(route_id_text)
        try:
            with handler.conn() as db:
                route = db.execute(
                    """SELECT r.*,d.name driver_name,v.name vehicle_name,v.plate FROM routes r
                       JOIN drivers d ON d.id=r.driver_id LEFT JOIN vehicles v ON v.id=r.vehicle_id
                       WHERE r.id=? AND r.driver_id=?""",
                    (route_id, driver["id"]),
                ).fetchone()
                if not route:
                    return handler.send_json({"ok": False, "message": "Carga não encontrada para este motorista."}, 404)
                orders = db.execute(
                    """SELECT ro.id route_order_id,ro.delivery_order,ro.status route_order_status,
                              o.id order_id,o.order_number,o.status order_status,o.total_value,o.weight_kg,
                              o.expected_delivery_date,o.payment_method,o.notes order_notes,o.location_link,
                              c.id client_id,c.name client_name,c.phone client_phone,c.whatsapp client_whatsapp,
                              c.farm_name,c.city,c.address client_full_address,
                              COALESCE(o.delivery_address,c.address) delivery_address,c.reference_point
                       FROM route_orders ro JOIN orders o ON o.id=ro.order_id LEFT JOIN clients c ON c.id=o.client_id
                       WHERE ro.route_id=? ORDER BY ro.delivery_order,o.id""",
                    (route_id,),
                ).fetchall()
                result_orders = []
                for row in orders:
                    item = dict(row)
                    receipt = db.execute(
                        "SELECT id,created_at FROM delivery_receipts WHERE route_id=? AND order_id=?",
                        (route_id, item["order_id"]),
                    ).fetchone()
                    item["has_receipt_photo"] = bool(receipt)
                    item["receipt_created_at"] = receipt["created_at"] if receipt else None
                    items = db.execute("SELECT product_name,quantity,unit,weight_kg FROM order_items WHERE order_id=? ORDER BY id", (item["order_id"],)).fetchall()
                    item["items"] = [dict(product) for product in items]
                    result_orders.append(item)
                result = dict(route)
                result["orders"] = result_orders
                return handler.send_json({"ok": True, "route": result})
        except Exception:
            logger.exception("Erro ao carregar carga %s", route_id)
            return handler.send_json({"ok": False, "message": "Não foi possível carregar a carga."}, 500)
    if path == "/api/v1/driver/deliver" and method == "POST":
        return _deliver(handler, driver)
    return False
