# -*- coding: utf-8 -*-
"""
app_core/erp_field_mapper.py
============================
Mapeador de campos ERP → campos do sistema de logística.

Responsabilidade única: receber o dict bruto do ERP (chaves lowercase)
e retornar um dict com os campos prontos para preencher o formulário de pedido.

O mapeamento de colunas é configurável via variáveis de ambiente (prefixo ERP_COL_*),
permitindo adaptação a diferentes ERPs sem alterar código.

Variáveis de ambiente para mapeamento de colunas (.env):
  ERP_COL_NUMERO_PEDIDO     = numeropedido      (coluna do nº pedido na view pedidos)
  ERP_COL_DATA_VENDA        = datavenda
  ERP_COL_VALOR_TOTAL       = valortotal
  ERP_COL_FORMA_PAGAMENTO   = formapagamento
  ERP_COL_NOME_CLIENTE      = nomecliente        (coluna na view clientes ou pedidos)
  ERP_COL_CIDADE_CLIENTE    = cidade
  ERP_COL_UF_CLIENTE        = uf
  ERP_COL_BAIRRO_CLIENTE    = bairro
  ERP_COL_ENDERECO_CLIENTE  = endereco
  ERP_COL_TELEFONE_CLIENTE  = telefone
  ERP_COL_NOME_VENDEDOR     = nomevendedor       (coluna na view vendedores)
  ERP_COL_ITEM_CODIGO       = codigoproduto      (coluna na view itens)
  ERP_COL_ITEM_NOME         = nomeproduto
  ERP_COL_ITEM_QTD          = quantidade
  ERP_COL_ITEM_UNIDADE      = unidade
  ERP_COL_ITEM_PESO         = pesoproduto        (coluna na view produtos/_produto)
  ERP_COL_NF_NUMERO         = numeronf           (coluna na view faturamento)
  ERP_COL_NF_DATA           = datafaturamento
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _col(env_key: str, default: str) -> str:
    return (os.environ.get(env_key) or default).strip().lower()


def _get(d: dict, *keys: str, default: Any = None) -> Any:
    """Busca em um dict por múltiplas chaves possíveis (em ordem de preferência)."""
    for k in keys:
        v = d.get(k.lower())
        if v is not None:
            return v
    return default


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_date(value: Any) -> str:
    """Converte datas de vários formatos para YYYY-MM-DD."""
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    raw = str(value).strip()
    if not raw:
        return ""
    # Tenta formatos comuns (usa a string completa, não truncada por len(fmt))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    # Fallback: primeiros 10 caracteres se parecer com uma data
    if len(raw) >= 10 and (raw[4] in "-/" or raw[2] in "-/"):
        return raw[:10]
    return ""


def _parse_float(value: Any) -> float:
    """Converte valor para float, suportando formatos BR (1.250,75) e EN (1250.75)."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    raw = str(value).strip()
    if not raw:
        return 0.0
    try:
        # Detecta formato brasileiro: tem vírgula como separador decimal
        # Ex: "1.250,75" → tem vírgula após dígitos, vírgula é decimal
        # Ex: "2500,50" → vírgula é decimal
        if "," in raw:
            # Remove pontos (separadores de milhar) e troca vírgula por ponto
            cleaned = raw.replace(".", "").replace(",", ".")
            return round(float(cleaned), 4)
        # Formato inglês/ISO: ponto é decimal
        # Ex: "1500.75" ou "2500.50"
        return round(float(raw.replace(",", "")), 4)
    except Exception:
        return 0.0


def _normalize_payment(raw: str) -> str:
    """
    Tenta mapear a forma de pagamento do ERP para os valores aceitos pelo sistema.
    """
    if not raw:
        return ""
    r = raw.strip().lower()
    mapping = {
        "vista": "À vista",
        "á vista": "À vista",
        "a vista": "À vista",
        "dinheiro": "Dinheiro",
        "boleto": "Boleto",
        "pix": "Pix",
        "cartao": "Cartão",
        "cartão": "Cartão",
        "prazo": "Prazo",
        "promissoria": "Nota Promissória",
        "promissória": "Nota Promissória",
        "antecipado": "Pago antecipado",
        "pago antecipado": "Pago antecipado",
        "sem cobranca": "Sem cobrança na entrega",
        "sem cobrança": "Sem cobrança na entrega",
    }
    for key, label in mapping.items():
        if key in r:
            return label
    return raw.strip()  # Retorna o original se não mapeado


def _format_phone(value: Any) -> str:
    """Formata telefone no padrão (XX) XXXXX-XXXX ou (XX) XXXX-XXXX."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    if len(digits) == 9:
        return f"{digits[:5]}-{digits[5:]}"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:]}"
    return str(value).strip()


def _extract_farm(endereco: str, fantasia: str = "") -> str:
    """Extrai o nome da Fazenda/Local a partir do endereço ou nome fantasia."""
    end_upper = (endereco or "").strip().upper()
    fan_upper = (fantasia or "").strip().upper()
    keywords = ("FAZENDA", "SITIO", "SÍTIO", "CHACARA", "CHÁCARA", "RANCHO", "ESTANCIA", "ESTÂNCIA", "HARAS", "GLEBA", "POVOADO")
    for kw in keywords:
        if kw in end_upper:
            parts = [p.strip() for p in (endereco or "").split(",") if p.strip()]
            if parts:
                return parts[0]
            return (endereco or "").strip()
        if kw in fan_upper:
            parts = [p.strip() for p in (fantasia or "").split(",") if p.strip()]
            if parts:
                return parts[0]
            return (fantasia or "").strip()
    return ""


# ---------------------------------------------------------------------------
# Mapeamento principal: ERP bruto → campos logísticos
# ---------------------------------------------------------------------------

def map_erp_to_logistica(erp_data: dict[str, Any]) -> dict[str, Any]:
    """
    Converte o dict bruto do ERP (pedido + cliente + vendedor + itens)
    para o formato de dicionário aceito pela rota de criação de pedido do sistema.
    """
    if not erp_data:
        return {}

    cli = erp_data.get("_cliente") or {}
    vend = erp_data.get("_vendedor") or {}
    itens_raw = erp_data.get("_itens") or []

    # ---- Colunas configuráveis ----
    col_num_ped = _col("ERP_COL_NUMERO_PEDIDO", "numeropedido")
    col_data_venda = _col("ERP_COL_DATA_VENDA", "datavenda")
    col_valor_total = _col("ERP_COL_VALOR_TOTAL", "valortotal")
    col_forma_pag = _col("ERP_COL_FORMA_PAGAMENTO", "formapagamento")
    col_nome_cli = _col("ERP_COL_NOME_CLIENTE", "nomecliente")
    col_cidade = _col("ERP_COL_CIDADE_CLIENTE", "cidade")
    col_uf = _col("ERP_COL_UF_CLIENTE", "uf")
    col_bairro = _col("ERP_COL_BAIRRO_CLIENTE", "bairro")
    col_endereco = _col("ERP_COL_ENDERECO_CLIENTE", "endereco")
    col_tel = _col("ERP_COL_TELEFONE_CLIENTE", "telefone")
    col_nome_vend = _col("ERP_COL_NOME_VENDEDOR", "nomevendedor")
    col_item_cod = _col("ERP_COL_ITEM_CODIGO", "codigoproduto")
    col_item_nome = _col("ERP_COL_ITEM_NOME", "nomeproduto")
    col_item_qtd = _col("ERP_COL_ITEM_QTD", "quantidade")
    col_item_unid = _col("ERP_COL_ITEM_UNIDADE", "unidade")
    col_item_peso = _col("ERP_COL_ITEM_PESO", "pesoproduto")

    # ---- Campos da venda ----
    order_number = _clean_str(_get(erp_data, col_num_ped, "numeropedido", "num_pedido", "pedido", "pedido_", "nota_"))
    sale_date = _parse_date(_get(erp_data, col_data_venda, "datavenda", "data_venda", "datapedido", "dtemissao_", "dtemissao"))
    total_value = _parse_float(_get(erp_data, col_valor_total, "valortotal", "valor_total", "total", "ptotal_", "ptotal"))
    payment_raw = _clean_str(_get(erp_data, col_forma_pag, "formapagamento", "forma_pag", "pagamento", "plano_", "tpdescricao_"))
    payment_method = _normalize_payment(payment_raw)

    # ---- Campos do cliente ----
    customer_code = _clean_str(_get(cli, "codigo", "codigocliente") or _get(erp_data, "codcliente_", "codigocliente", "cod_cliente"))
    client_name = _clean_str(
        _get(cli, col_nome_cli, "nomecliente", "nome", "razaosocial", "razão_social", "fantasia")
        or _get(erp_data, col_nome_cli, "nomecliente", "nome_cliente", "cliente", "razao_", "razao")
    )
    cpf_cnpj = _clean_str(
        _get(cli, "cnpj_cpf", "cnpj", "cpf", "cpf_cnpj")
        or _get(erp_data, "cnpj_", "cnpj", "cpf", "cpf_cnpj")
    )
    city = _clean_str(
        _get(cli, col_cidade, "cidade", "municipio", "nome_cidade")
        or _get(erp_data, "cidade", "municipio", "nome_cidade")
    )
    uf = _clean_str(
        _get(cli, col_uf, "uf", "estado")
        or _get(erp_data, "uf", "estado")
    )
    bairro = _clean_str(_get(cli, col_bairro, "bairro") or _get(erp_data, "bairro"))
    raw_endereco = _clean_str(_get(cli, col_endereco, "endereco", "logradouro") or _get(erp_data, "endereco"))
    raw_fantasia = _clean_str(_get(cli, "fantasia"))
    phone_raw = _clean_str(_get(cli, col_tel, "telefone", "fone", "celular", "fone1", "fone3") or _get(erp_data, "telefone", "fone", "fone1"))
    phone = _format_phone(phone_raw)
    farm_name = _extract_farm(raw_endereco, raw_fantasia)

    # Monta endereço de entrega completo
    delivery_parts = [p for p in [raw_endereco, bairro, city, uf] if p]
    delivery_address = ", ".join(delivery_parts)

    # ---- Campos do vendedor ----
    seller_name = _clean_str(
        _get(vend, col_nome_vend, "nomevendedor", "nome_ved", "nome", "vendedor")
        or _get(erp_data, "nomevendedor", "nome_vendedor", "vendedor", "nome_", "nome")
    )

    # ---- Itens ----
    items = []
    total_weight = _parse_float(erp_data.get("total_weight") or 0)
    
    # Se os itens não vieram em _itens, mas o pedido contém codprod_ / descricao_, trata como item
    if not itens_raw and (_get(erp_data, "codprod_", "codprod", "codigoproduto")):
        itens_raw = [erp_data]

    calc_weight = 0.0
    calc_total_val = 0.0

    for item_raw in itens_raw:
        prod = item_raw.get("_produto") or {}

        code = _clean_str(_get(item_raw, col_item_cod, "codigoproduto", "cod_produto", "codigo", "codprod_", "codprod"))
        name = _clean_str(
            _get(item_raw, col_item_nome, "nomeproduto", "nome", "descricao", "descricaoproduto", "descricao_")
            or _get(prod, "nomeproduto", "nome", "descricao")
        )
        if not name:
            name = f"Produto {code}" if code else "Item s/ nome"

        qty = _parse_float(_get(item_raw, col_item_qtd, "quantidade", "qtd", "qtde", "qtde_"))
        unit_price = _parse_float(_get(item_raw, "p_unit_", "p_unit", "valorunitario", "valor_unitario", "preco"))
        item_total_val = _parse_float(_get(item_raw, "ptotal_", "ptotal", "valortotal", "valor_total")) or round(qty * unit_price, 2)
        calc_total_val += item_total_val

        unit = _clean_str(_get(item_raw, col_item_unid, "unidade", "un", "unid") or "unidade")

        # Peso: tenta no item, depois no produto
        peso_unit = _parse_float(
            _get(item_raw, col_item_peso, "pesoproduto", "peso", "pesounitario", "peso_bruto")
            or _get(prod, col_item_peso, "pesoproduto", "peso", "pesounitario", "peso_kg", "peso_bruto")
        )
        peso_total_item = round(peso_unit * qty, 4) if peso_unit and qty else 0.0
        calc_weight = round(calc_weight + peso_total_item, 4)

        items.append({
            "product_code": code,
            "product_name": name,
            "quantity": qty,
            "unit": unit,
            "unit_price": unit_price,
            "weight_kg": peso_total_item,
        })

    final_weight = total_weight if total_weight > 0 else calc_weight
    final_total_val = calc_total_val if calc_total_val > total_value else total_value

    # ---- Detecção de Faturamento ----
    posicao = _clean_str(_get(erp_data, "posicao_pedido", "posicao", "posicao_")).upper()
    raw_nf = _clean_str(_get(erp_data, "numero_nota_fiscal_saida", "nota_", "nota_fiscal", "numeronf", "numero_nfe", "nota"))
    if raw_nf in ("0", "0.0", "None", "NULL"):
        raw_nf = ""

    # Só considera Faturado se POSICAO_PEDIDO == 'F' E possuir número de Nota Fiscal > 0!
    is_invoiced = bool(raw_nf) and (posicao in ("F", "FATURADO", "") or not posicao)
    if posicao and posicao not in ("F", "FATURADO"):
        is_invoiced = False

    invoice_number = raw_nf if is_invoiced else ""
    invoiced_at = _parse_date(_get(erp_data, "data_faturamento_pedido", "data_faturamento")) if is_invoiced else ""
    if is_invoiced and not invoiced_at:
        invoiced_at = sale_date

    return {
        "order_number": order_number,
        "customer_code": customer_code,
        "client_code": customer_code,
        "client_name": client_name,
        "customer_name": client_name,
        "customer_cpf_cnpj": cpf_cnpj,
        "cpf_cnpj": cpf_cnpj,
        "city": city,
        "customer_city": city,
        "uf": uf,
        "customer_uf": uf,
        "delivery_address": delivery_address,
        "customer_address": delivery_address,
        "farm_name": farm_name,
        "customer_farm": farm_name,
        "phone": phone,
        "customer_phone": phone,
        "seller_name": seller_name,
        "seller": seller_name,
        "sale_date": sale_date,
        "order_date": sale_date,
        "total_value": round(final_total_val, 2),
        "payment_method": payment_method,
        "weight_kg": final_weight,
        "total_weight": final_weight,
        "is_invoiced": is_invoiced,
        "invoiced": is_invoiced,
        "invoice_number": invoice_number,
        "invoiced_at": invoiced_at,
        "items": items,
        # Flags de controle para o frontend
        "_erp_filled": True,
        "_erp_item_count": len(items),
        "_erp_has_weight": final_weight > 0,
    }


def map_erp_invoice_to_logistica(erp_fat_data: dict[str, Any]) -> dict[str, Any] | None:
    """
    Converte o dict bruto de faturamento do ERP para os campos de NF do sistema.

    Retorna dict com 'invoice_number' e 'invoiced_at', ou None se NF não encontrada.
    """
    if not erp_fat_data:
        return None

    col_nf_num = _col("ERP_COL_NF_NUMERO", "numeronf")
    col_nf_data = _col("ERP_COL_NF_DATA", "datafaturamento")

    nf = _clean_str(
        _get(erp_fat_data, col_nf_num, "numeronf", "nota_fiscal", "nf", "numernf", "numnf")
    )
    if not nf:
        return None

    nf_date = _parse_date(
        _get(erp_fat_data, col_nf_data, "datafaturamento", "data_faturamento", "data_nf", "datanf")
    )

    return {
        "invoice_number": nf,
        "invoiced_at": nf_date or datetime.now().strftime("%Y-%m-%d"),
    }
