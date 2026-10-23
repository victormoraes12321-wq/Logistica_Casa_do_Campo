"""Leitura pontual de dados comerciais para viagens, sem executar sincronizações."""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta


def day(value):
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def text_key(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


def route_for_city(db, city):
    key = text_key(city)
    if not key:
        return ""
    try:
        rows = db.execute("SELECT route_name,city FROM route_cities WHERE active=1 ORDER BY COALESCE(delivery_order,9999),id").fetchall()
        return next((r["route_name"] for r in rows if text_key(r["city"]) == key), "")
    except sqlite3.OperationalError:
        return ""


def matches_client(db, existing, metadata):
    """Não associa uma referência avulsa a uma venda de outro cliente."""
    if not existing or not metadata:
        return False
    client = db.execute("SELECT customer_code,name FROM clients WHERE id=?", (existing["client_id"],)).fetchone()
    if not client:
        return False
    # Alguns cadastros antigos receberam o próprio ID local como código do ERP;
    # por isso, o nome também precisa corresponder antes de corrigir históricos.
    same_name = bool(text_key(client["name"]) and text_key(client["name"]) == text_key(metadata.get("client_name")))
    local_code = str(client["customer_code"] or "").lstrip("0")
    remote_code = str(metadata.get("client_code") or "").lstrip("0")
    same_code = bool(local_code and remote_code and local_code == remote_code and local_code != str(existing["client_id"]))
    return same_name or same_code


def _invoice_candidates(rows, reference):
    """Números fiscais se repetem entre séries/anos; usa a emissão mais recente.

    Uma NF fiscal tem prioridade sobre um número de orçamento coincidente.
    Empates entre pedidos na mesma data exigem o número do pedido.
    """
    def equals(value):
        try:
            return int(value or 0) == int(reference)
        except (TypeError, ValueError):
            return False

    actual = [r for r in rows if equals(r.get("invoice_number"))]
    headers = [r for r in rows if equals(r.get("header_invoice"))]
    candidates = actual or headers or [r for r in rows if
        str(r.get("position") or "").upper() == "F" and equals(r.get("quote_number"))]
    if len({str(r["order_number"]) for r in candidates}) <= 1:
        return candidates
    dates = [day(r.get("invoice_issue_date")) or day(r.get("invoiced_at")) for r in candidates]
    if all(dates):
        latest = max(dates)
        candidates = [r for r, issued in zip(candidates, dates) if issued == latest]
    if len({str(r["order_number"]) for r in candidates}) > 1:
        raise ValueError("Esta NF corresponde a mais de um pedido na emissão mais recente ou sem data confirmada. Informe o número do pedido para localizar a entrega correta.")
    return candidates


def lookup_metadata(db, reference, cursor, field="order_number"):
    if cursor is None:
        return None
    raw_ref = str(reference or "").strip()
    if not raw_ref.isdigit():
        return None
    from app_core import erp_connector
    cfg = erp_connector.get_erp_config()
    if not cfg.is_ready or cfg.driver != "oracle":
        return None
    schema = f"{cfg.schema}." if cfg.schema else ""
    clients = cfg.view_clientes if "." in cfg.view_clientes else cfg.qualified(cfg.view_clientes)
    condition = "p.NUMERO=:ref" if field != "invoice_number" else "(n.NUMERO=:ref OR p.NUMERO_NOTA_FISCAL_SAIDA=:ref OR p.NUMERO_ORCAMENTO=:ref)"
    cursor.execute(f"""
        SELECT p.NUMERO AS ORDER_NUMBER, p.CODIGO_CLIENTE AS CLIENT_CODE,
               p.DATA AS SALE_DATE, p.DATA_FATURAMENTO_PEDIDO AS INVOICED_AT,
               p.DATA_ENTREGA AS ERP_DELIVERY_DATE, p.POSICAO_PEDIDO AS POSITION,
               p.NUMERO_NOTA_FISCAL_SAIDA AS HEADER_INVOICE,
               p.NUMERO_ORCAMENTO AS QUOTE_NUMBER, n.NUMERO AS INVOICE_NUMBER,
               n.DATA_EMISSAO AS INVOICE_ISSUE_DATE,
               p.PESO_TOTAL AS WEIGHT_KG, p.VALOR_TOTAL AS TOTAL_VALUE,
               c.NOME AS CLIENT_NAME, c.CNPJ_CPF AS DOCUMENT, c.FONE1 AS PHONE,
               c.ENDERECO AS ADDRESS, c.NUMERO AS ADDRESS_NUMBER,
               c.BAIRRO AS NEIGHBORHOOD, c.NOME_CIDADE AS CITY, c.UF AS UF,
               rca.NOME AS SELLER_NAME
        FROM {schema}DON_PEDIDO_VENDA p
        LEFT JOIN {schema}DON_NOTA_FISCAL_SAIDA n ON n.NUMERO_PEDIDO=p.NUMERO AND n.CANCELADA='N'
        LEFT JOIN {clients} c ON c.CODIGO=p.CODIGO_CLIENTE
        LEFT JOIN {schema}DON_RCA rca ON rca.CODIGO=p.CODIGO_RCA
        WHERE {condition}
    """, {"ref": int(raw_ref)})
    columns = [column[0].lower() for column in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    if not rows and field == "any":
        return lookup_metadata(db, reference, cursor, "invoice_number")
    if not rows:
        return None
    if field == "invoice_number":
        rows = _invoice_candidates(rows, int(raw_ref))
        if not rows:
            return None
    # Uma NF ou documento ambíguo não pode escolher arbitrariamente um pedido.
    if len({str(row["order_number"]) for row in rows}) != 1:
        raise ValueError("Documento vinculado a mais de um pedido no ERP. Informe o número do pedido.")
    row = rows[0]
    sale_date = day(row.get("sale_date"))
    if not sale_date or not row.get("client_name"):
        raise ValueError("O ERP não retornou a data de venda ou o cliente do pedido. Confira o cadastro antes de gravar.")
    invoiced_at = day(row.get("invoiced_at")) or day(row.get("invoice_issue_date"))
    invoice = row.get("invoice_number") or row.get("header_invoice")
    if not invoice and str(row.get("position") or "").upper() == "F":
        invoice = row.get("quote_number")
    invoice = str(invoice or "")
    if invoice in ("0", "0.0"):
        invoice = ""
    address = str(row.get("address") or "").strip()
    number = str(row.get("address_number") or "").strip()
    if number and number not in ("0", "S/N", "SN"):
        address = f"{address}, {number}".strip(", ")
    result = {
        "order_number": str(row["order_number"]), "invoice_number": invoice,
        "client_code": str(row.get("client_code") or ""), "client_name": row.get("client_name") or "",
        "document": row.get("document") or "", "phone": row.get("phone") or "",
        "sale_date": sale_date, "invoiced_at": invoiced_at,
        "expected_delivery_date": (date.fromisoformat(sale_date) + timedelta(days=15)).isoformat() if sale_date else "",
        "city": row.get("city") or "", "uf": row.get("uf") or "",
        "route_name": route_for_city(db, row.get("city")), "delivery_address": address,
        "neighborhood": row.get("neighborhood") or "", "seller_name": row.get("seller_name") or "",
        "weight_kg": float(row.get("weight_kg") or 0), "total_value": float(row.get("total_value") or 0),
        "source": "ERP_MASTER", "metadata_verified": True,
    }
    cursor.execute(f"""
        SELECT i.CODIGO_PRODUTO, COALESCE(i.DESCRICAO_PRODUTO,p.DESCRICAO,'Produto'),
               COALESCE(p.UNIDADE,'UN'), i.QUANTIDADE, i.PRECO_VENDA,
               i.QUANTIDADE * NVL(p.PESO_BRUTO,0)
        FROM {schema}DON_PEDIDO_VENDA_ITEM i
        LEFT JOIN {schema}DON_PRODUTO p ON p.CODIGO=i.CODIGO_PRODUTO
        WHERE i.NUMERO_PEDIDO_VENDA=:ref ORDER BY i.ITEM
    """, {"ref": int(result["order_number"])})
    items = [{"product_code": str(r[0] or ""), "product_name": r[1] or "Produto", "unit": r[2] or "UN",
              "quantity": float(r[3] or 0), "unit_price": float(r[4] or 0), "weight_kg": float(r[5] or 0)} for r in cursor.fetchall()]
    result["items"] = items
    result["products_summary"] = ", ".join(f"{i['quantity']:g} {i['unit']} {i['product_name']}" for i in items)
    result["details_complete"] = bool(items)
    if not result["weight_kg"]:
        result["weight_kg"] = sum(i["weight_kg"] for i in items)
    return result
