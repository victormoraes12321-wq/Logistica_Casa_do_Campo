import os
import sys
import html
from datetime import datetime, date

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas


class NumberedCanvas(canvas.Canvas):
    """Canvas de duas passagens para numeração de páginas no rodapé: Página X de Y."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def draw_page_number(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b"))
        now_str = datetime.now().strftime("%d/%m/%Y às %H:%M:%S")
        footer_text = f"Relatório Logística Casa do Campo — Gerado em {now_str} — Página {self._pageNumber} de {page_count}"
        self.drawRightString(A4[1] - 1.2 * cm, 0.8 * cm, footer_text)
        self.drawString(1.2 * cm, 0.8 * cm, "CONFIDENCIAL — USO OPERACIONAL INTERNO")
        self.setStrokeColor(colors.HexColor("#cbd5e1"))
        self.setLineWidth(0.5)
        self.line(1.2 * cm, 1.2 * cm, A4[1] - 1.2 * cm, 1.2 * cm)
        self.restoreState()


def get_desktop_report_folder():
    """Retorna o caminho da pasta de relatórios na Área de Trabalho do Windows."""
    user_home = os.path.expanduser("~")
    desktop = os.path.join(user_home, "Desktop")
    folder = os.path.join(desktop, "Relatório Logística Casa do Campo")
    os.makedirs(folder, exist_ok=True)
    return folder


def esc(val):
    if val is None:
        return ""
    return html.escape(str(val))


def fmt_br_date(val):
    if not val:
        return "—"
    val_str = str(val).strip()
    if len(val_str) >= 10 and val_str[4] == "-" and val_str[7] == "-":
        return f"{val_str[8:10]}/{val_str[5:7]}/{val_str[:4]}"
    return esc(val_str)


def parse_date(val):
    if not val:
        return None
    val_str = str(val).strip()[:10]
    try:
        return datetime.strptime(val_str, "%Y-%m-%d").date()
    except Exception:
        return None


def is_cfg_true(config, key, default=True):
    if not config:
        return default
    val = config.get(key)
    if val is None or val == "":
        return default
    return str(val).strip().lower() in ("1", "true", "on", "yes")


def calculate_sla_info(expected_date_str, status_str):
    """
    Calcula a prioridade de ordenação, texto do pill e cor do SLA.
    Retorna: (priority_int, pill_text, text_color_hex, bg_color_hex, row_border_color_hex)
    1 = Atrasado (Vermelho)
    2 = Em Risco (Amarelo)
    3 = No Prazo / OK (Verde)
    4 = Agendado (Azul)
    5 = Finalizado / Outros (Cinza)
    """
    st = str(status_str or "").strip()
    exp_date = parse_date(expected_date_str)
    today_dt = date.today()

    if st == "Agendado":
        return (4, "Agendado", "#1e40af", "#dbeafe", "#3b82f6")

    if st in ("Acertado", "Entregue"):
        return (5, "Entregue / Acertado", "#15803d", "#dcfce7", "#22c55e")

    if st == "Cancelado":
        return (6, "Cancelado", "#475569", "#f1f5f9", "#94a3b8")

    if st == "Problema":
        return (5, "Com Problema", "#b91c1c", "#fee2e2", "#ef4444")

    if not exp_date:
        return (3, "Sem data limite", "#475569", "#f1f5f9", "#94a3b8")

    diff_days = (exp_date - today_dt).days

    if diff_days < 0:
        days_late = abs(diff_days)
        return (1, f"Atrasado há {days_late} dia{'s' if days_late > 1 else ''}", "#b91c1c", "#fee2e2", "#ef4444")

    if diff_days == 0:
        return (2, "Vence hoje", "#b45309", "#fef3c7", "#f59e0b")

    if diff_days == 1:
        return (2, "Vence amanhã", "#b45309", "#fef3c7", "#f59e0b")

    if diff_days == 2:
        return (2, "Vence em 2 dias", "#b45309", "#fef3c7", "#f59e0b")

    return (3, f"{diff_days} dias restantes", "#15803d", "#dcfce7", "#22c55e")


def fmt_money(val):
    try:
        num = float(val or 0)
        return f"R$ {num:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"


def fmt_weight(val):
    try:
        num = float(val or 0)
        return f"{num:,.1f} kg".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "0,0 kg"


def generate_orders_pdf_report(db, config=None, target_dir=None, filename_prefix="Relatorio_Pedidos"):
    """
    Gera um relatório profissional dos pedidos em PDF e o salva na pasta da Área de Trabalho.
    Respeita as escolhas de checkboxes (status a incluir, colunas visíveis e cards do cabeçalho).
    """
    if config is None:
        config = {}

    target_folder = target_dir or get_desktop_report_folder()
    os.makedirs(target_folder, exist_ok=True)

    now_dt = datetime.now()
    stamp = now_dt.strftime("%Y-%m-%d_%H%M%S")
    filename = f"{filename_prefix}_{stamp}.pdf"
    filepath = os.path.join(target_folder, filename)

    where_parts = []
    params = []

    status_param = (config.get("status") or "").strip()
    if status_param and status_param not in ("all", "Todos"):
        where_parts.append("o.status = ?")
        params.append(status_param)
    else:
        # Verifica se checkboxes de status foram explicitamente configuradas
        included_statuses = []
        if is_cfg_true(config, "pdf_report_st_venda", True): included_statuses.append("Venda")
        if is_cfg_true(config, "pdf_report_st_faturado", True): included_statuses.append("Faturado")
        if is_cfg_true(config, "pdf_report_st_carga_montada", True): included_statuses.append("Carga Montada")
        if is_cfg_true(config, "pdf_report_st_rota", True): included_statuses.append("Saiu para entrega")
        if is_cfg_true(config, "pdf_report_st_agendado", True): included_statuses.append("Agendado")
        if is_cfg_true(config, "pdf_report_st_acertado", True): included_statuses.extend(["Acertado", "Entregue"])
        if is_cfg_true(config, "pdf_report_st_problema", True): included_statuses.append("Problema")
        if is_cfg_true(config, "pdf_report_st_cancelado", False): included_statuses.append("Cancelado")
        if included_statuses and len(included_statuses) < 8:
            where_parts.append(f"o.status IN ({','.join(['?']*len(included_statuses))})")
            params.extend(included_statuses)
        elif not is_cfg_true(config, "pdf_report_st_cancelado", False):
            where_parts.append("o.status <> 'Cancelado'")

    q_param = (config.get("q") or config.get("search") or "").strip()
    if q_param:
        where_parts.append("(o.order_number LIKE ? OR o.invoice_number LIKE ? OR c.name LIKE ? OR o.seller_name LIKE ?)")
        params.extend([f"%{q_param}%", f"%{q_param}%", f"%{q_param}%", f"%{q_param}%"])

    st_param = (config.get("start") or "").strip()
    end_param = (config.get("end") or "").strip()
    rt_param = (config.get("route") or "").strip()
    driver_param = (config.get("driver") or "").strip()
    vehicle_param = (config.get("vehicle") or "").strip()
    seller_param = (config.get("seller") or "").strip()
    sla_param = (config.get("sla_status") or "").strip()
    date_type = (config.get("date_type") or "operational").strip()

    op_date_expr = """COALESCE(
        NULLIF(substr(o.delivered_at, 1, 10), ''),
        (SELECT r.date FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1),
        NULLIF(substr(o.invoiced_at, 1, 10), ''),
        o.sale_date
    )"""
    route_date_expr = """(SELECT r.date FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1)"""

    if st_param and end_param:
        if date_type == "delivery":
            where_parts.append("substr(COALESCE(o.delivered_at,''), 1, 10) BETWEEN ? AND ?")
        elif date_type == "expected":
            where_parts.append("o.expected_delivery_date BETWEEN ? AND ?")
        elif date_type == "sale":
            where_parts.append("substr(COALESCE(o.sale_date, o.created_at), 1, 10) BETWEEN ? AND ?")
        elif date_type == "route":
            where_parts.append(f"{route_date_expr} BETWEEN ? AND ?")
        else: # operational
            where_parts.append(f"{op_date_expr} BETWEEN ? AND ?")
        params.extend([st_param, end_param])
    elif st_param:
        if date_type == "delivery":
            where_parts.append("substr(COALESCE(o.delivered_at,''), 1, 10) >= ?")
        elif date_type == "expected":
            where_parts.append("o.expected_delivery_date >= ?")
        elif date_type == "sale":
            where_parts.append("substr(COALESCE(o.sale_date, o.created_at), 1, 10) >= ?")
        elif date_type == "route":
            where_parts.append(f"{route_date_expr} >= ?")
        else:
            where_parts.append(f"{op_date_expr} >= ?")
        params.append(st_param)
    elif end_param:
        if date_type == "delivery":
            where_parts.append("substr(COALESCE(o.delivered_at,''), 1, 10) <= ?")
        elif date_type == "expected":
            where_parts.append("o.expected_delivery_date <= ?")
        elif date_type == "sale":
            where_parts.append("substr(COALESCE(o.sale_date, o.created_at), 1, 10) <= ?")
        elif date_type == "route":
            where_parts.append(f"{route_date_expr} <= ?")
        else:
            where_parts.append(f"{op_date_expr} <= ?")
        params.append(end_param)

    if rt_param and rt_param not in ("all", "Todas"):
        where_parts.append("o.route_name = ?")
        params.append(rt_param)
    if seller_param and seller_param not in ("all", "Todos"):
        where_parts.append("o.seller_name = ?")
        params.append(seller_param)
    if driver_param and driver_param not in ("all", "Todos"):
        dr_expr = """COALESCE(
            (SELECT d.name FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' JOIN drivers d ON d.id=r.driver_id WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1),
            (SELECT od.name FROM drivers od WHERE od.id=o.driver_id),
            'Sem motorista'
        )"""
        where_parts.append(f"{dr_expr} = ?")
        params.append(driver_param)
    if vehicle_param and vehicle_param not in ("all", "Todos"):
        vh_expr = """COALESCE(
            (SELECT CASE
                WHEN v.plate IS NOT NULL AND TRIM(v.plate) <> '' THEN v.name || ' - ' || v.plate
                ELSE v.name END
             FROM route_orders ro
             JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada'
             JOIN vehicles v ON v.id=r.vehicle_id
             WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1),
            (SELECT CASE
                WHEN ov.plate IS NOT NULL AND TRIM(ov.plate) <> '' THEN ov.name || ' - ' || ov.plate
                ELSE ov.name END
             FROM vehicles ov WHERE ov.id=o.vehicle_id),
            'Sem caminhão'
        )"""
        where_parts.append(f"{vh_expr} = ?")
        params.append(vehicle_param)

    if sla_param:
        today_str = date.today().strftime("%Y-%m-%d")
        if sla_param == "ontime":
            where_parts.append("((o.status IN ('Acertado','Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) <= o.expected_delivery_date) OR (o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.expected_delivery_date >= ?))")
            params.append(today_str)
        elif sla_param == "late":
            where_parts.append("((o.status IN ('Acertado','Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) > o.expected_delivery_date) OR (o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.expected_delivery_date < ?))")
            params.append(today_str)
        elif sla_param == "risk":
            where_parts.append("(o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.expected_delivery_date BETWEEN ? AND date(?, '+2 days'))")
            params.extend([today_str, today_str])

    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    query = f"""
        SELECT o.*, c.name AS client_name, c.farm_name,
               (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id=o.id) AS item_count
        FROM orders o
        LEFT JOIN clients c ON c.id=o.client_id
        {where_clause}
    """
    db_rows = db.execute(query, tuple(params)).fetchall()

    # Processar SLA e Ordenar por Prioridade de Urgência:
    # 1º Atrasados (Vermelho) -> 2º Em Risco (Amarelo) -> 3º No Prazo (Verde) -> 4º Agendados (Azul) -> 5º Entregues / Outros
    annotated_rows = []
    for r in db_rows:
        sla_prio, sla_text, sla_text_col, sla_bg_col, sla_border_col = calculate_sla_info(
            r["expected_delivery_date"], r["status"]
        )
        exp_d = str(r["expected_delivery_date"] or "9999-99-99")
        annotated_rows.append({
            "raw": r,
            "sla_prio": sla_prio,
            "sla_text": sla_text,
            "sla_text_col": sla_text_col,
            "sla_bg_col": sla_bg_col,
            "sla_border_col": sla_border_col,
            "exp_date_str": exp_d,
            "id": r["id"]
        })

    # Ordenação estrita por prioridade de SLA, depois data prevista, depois ID desc
    annotated_rows.sort(key=lambda x: (x["sla_prio"], x["exp_date_str"], -x["id"]))

    # Opções de Exibição de Colunas (Checkboxes)
    show_financial = is_cfg_true(config, "pdf_report_col_financial", True)
    show_weight = is_cfg_true(config, "pdf_report_col_weight", True)
    show_dates = is_cfg_true(config, "pdf_report_col_dates", True)
    show_seller = is_cfg_true(config, "pdf_report_col_seller", True)
    show_receiver = is_cfg_true(config, "pdf_report_col_receiver", True)

    # Opções de Cards do Cabeçalho (Checkboxes)
    hdr_orders = is_cfg_true(config, "pdf_hdr_total_orders", True)
    hdr_weight = is_cfg_true(config, "pdf_hdr_total_weight", True)
    # Valor Total só aparece no cabeçalho se AMBAS as opções (hdr e financial) estiverem marcadas
    hdr_value = is_cfg_true(config, "pdf_hdr_total_value", True) and show_financial
    hdr_late = is_cfg_true(config, "pdf_hdr_late", True)
    hdr_risk = is_cfg_true(config, "pdf_hdr_risk", True)
    hdr_ontime = is_cfg_true(config, "pdf_hdr_ontime", True)
    hdr_scheduled = is_cfg_true(config, "pdf_hdr_scheduled", True)
    hdr_delivered = is_cfg_true(config, "pdf_hdr_delivered", True)
    hdr_problems = is_cfg_true(config, "pdf_hdr_problems", False)
    hdr_canceled = is_cfg_true(config, "pdf_hdr_canceled", False)

    # 2. Resumo de Métricas
    total_orders = len(annotated_rows)
    total_weight = sum(float(x["raw"]["weight_kg"] or 0) for x in annotated_rows)
    total_value = sum(float(x["raw"]["total_value"] or 0) for x in annotated_rows)

    count_late = sum(1 for x in annotated_rows if x["sla_prio"] == 1)
    count_risk = sum(1 for x in annotated_rows if x["sla_prio"] == 2)
    count_ontime = sum(1 for x in annotated_rows if x["sla_prio"] == 3 and str(x["raw"]["status"]).strip() not in ("Acertado", "Entregue"))
    count_scheduled = sum(1 for x in annotated_rows if x["sla_prio"] == 4)
    count_delivered = sum(1 for x in annotated_rows if str(x["raw"]["status"]).strip() in ("Acertado", "Entregue"))
    count_problems = sum(1 for x in annotated_rows if str(x["raw"]["status"]).strip() == "Problema")
    count_canceled = sum(1 for x in annotated_rows if str(x["raw"]["status"]).strip() == "Cancelado")

    # 3. Documento PDF em formato Paisagem (Landscape A4)
    doc = SimpleDocTemplate(
        filepath,
        pagesize=landscape(A4),
        leftMargin=1.2 * cm,
        rightMargin=1.2 * cm,
        topMargin=1.0 * cm,
        bottomMargin=1.6 * cm,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "HeaderTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=colors.HexColor("#ffffff"),
    )
    subtitle_style = ParagraphStyle(
        "HeaderSubTitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#e2e8f0"),
    )
    card_title_style = ParagraphStyle(
        "CardTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#475569"),
        alignment=1,
    )
    card_value_style = ParagraphStyle(
        "CardValue",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=13,
        textColor=colors.HexColor("#0f172a"),
        alignment=1,
    )
    th_style = ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#0f172a"),
    )
    td_style = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.0,
        leading=9.0,
        textColor=colors.HexColor("#1e293b"),
    )
    td_bold = ParagraphStyle(
        "TableCellBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#0f172a"),
    )

    elements = []

    # BANNER DE CABEÇALHO
    header_data = [
        [
            Paragraph("<b>LOGÍSTICA CASA DO CAMPO</b>", title_style),
            Paragraph(f"<b>Relatório Operacional de Pedidos & SLA</b><br/>Emissão: {now_dt.strftime('%d/%m/%Y às %H:%M')}", subtitle_style)
        ]
    ]
    header_table = Table(header_data, colWidths=[18 * cm, 9.3 * cm])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#174f2a")),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 8))

    # CARDS DE CABEÇALHO MODULARES (DINÂMICOS CONFORME PREFERÊNCIAS)
    header_cards = []
    if hdr_orders:
        header_cards.append(("PEDIDOS", str(total_orders), "#0f172a"))
    if hdr_weight:
        header_cards.append(("PESO TOTAL", fmt_weight(total_weight), "#0f172a"))
    if hdr_value:
        header_cards.append(("VALOR TOTAL", fmt_money(total_value), "#0f172a"))
    if hdr_late:
        header_cards.append(("🔴 ATRASADOS", str(count_late), "#b91c1c"))
    if hdr_risk:
        header_cards.append(("🟡 EM RISCO", str(count_risk), "#b45309"))
    if hdr_ontime:
        header_cards.append(("🟢 NO PRAZO", str(count_ontime), "#15803d"))
    if hdr_scheduled:
        header_cards.append(("🔵 AGENDADOS", str(count_scheduled), "#1e40af"))
    if hdr_delivered:
        header_cards.append(("ENTREGUES", str(count_delivered), "#15803d"))
    if hdr_problems:
        header_cards.append(("PROBLEMAS", str(count_problems), "#b91c1c"))
    if hdr_canceled:
        header_cards.append(("CANCELADOS", str(count_canceled), "#64748b"))

    if header_cards:
        # Se houver mais de 6 cards, divide em 2 linhas balanceadas
        card_chunks = []
        if len(header_cards) > 6:
            mid = (len(header_cards) + 1) // 2
            card_chunks.append(header_cards[:mid])
            card_chunks.append(header_cards[mid:])
        else:
            card_chunks.append(header_cards)

        for chunk in card_chunks:
            row_titles = []
            row_values = []
            for title_c, val_c, col_c in chunk:
                row_titles.append(Paragraph(f"<b>{title_c}</b>", card_title_style))
                v_style = ParagraphStyle(
                    f"CV_{title_c}", parent=card_value_style, textColor=colors.HexColor(col_c)
                )
                row_values.append(Paragraph(f"<b>{val_c}</b>", v_style))

            num_cols = len(chunk)
            w_per_col = (27.3 * cm) / num_cols
            chunk_table = Table([row_titles, row_values], colWidths=[w_per_col] * num_cols)
            chunk_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#ffffff")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ("PADDING", (0, 0), (-1, -1), 4),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            elements.append(chunk_table)
            elements.append(Spacer(1, 4))
        elements.append(Spacer(1, 4))

    # MONTAGEM DINÂMICA DAS COLUNAS DA TABELA
    headers = [
        Paragraph("Nº Pedido", th_style),
        Paragraph("Cliente / Fazenda", th_style),
        Paragraph("Cidade / Rota", th_style)
    ]
    widths = [2.2 * cm, 5.0 * cm, 3.8 * cm]

    if show_seller:
        headers.append(Paragraph("Vendedor", th_style))
        widths.append(2.8 * cm)

    if show_dates:
        headers.append(Paragraph("Previsão & SLA", th_style))
        widths.append(3.6 * cm)

    if show_weight:
        headers.append(Paragraph("Peso (kg)", th_style))
        widths.append(2.0 * cm)

    if show_financial:
        headers.append(Paragraph("Valor (R$)", th_style))
        widths.append(2.6 * cm)

    headers.append(Paragraph("Status", th_style))
    widths.append(2.5 * cm)

    if show_receiver:
        headers.append(Paragraph("Recebedor / Obs", th_style))
        widths.append(5.0 * cm)

    # Ajustar proporção de larguras para preencher 27.3 cm
    total_w = sum(widths)
    scale = (27.3 * cm) / total_w
    widths = [w * scale for w in widths]

    table_data = [headers]

    status_colors = {
        "Acertado": (colors.HexColor("#dcfce7"), colors.HexColor("#15803d")),
        "Entregue": (colors.HexColor("#dcfce7"), colors.HexColor("#15803d")),
        "Saiu para entrega": (colors.HexColor("#dbeafe"), colors.HexColor("#1e40af")),
        "Carga Montada": (colors.HexColor("#ede9fe"), colors.HexColor("#5b21b6")),
        "Aguardando Saída": (colors.HexColor("#ede9fe"), colors.HexColor("#5b21b6")),
        "Faturado": (colors.HexColor("#fef3c7"), colors.HexColor("#b45309")),
        "Venda": (colors.HexColor("#e0f2fe"), colors.HexColor("#0369a1")),
        "Agendado": (colors.HexColor("#ede9fe"), colors.HexColor("#6b21a8")),
        "Problema": (colors.HexColor("#fee2e2"), colors.HexColor("#b91c1c")),
        "Cancelado": (colors.HexColor("#f1f5f9"), colors.HexColor("#475569")),
    }

    table_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 3.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]

    status_col_index = 3
    if show_seller: status_col_index += 1
    if show_dates: status_col_index += 1
    if show_weight: status_col_index += 1
    if show_financial: status_col_index += 1

    date_col_index = 3
    if show_seller: date_col_index += 1

    for i, item in enumerate(annotated_rows, start=1):
        r = item["raw"]
        st = str(r["status"] or "").strip()
        bg_col, txt_col = status_colors.get(st, (colors.HexColor("#f1f5f9"), colors.HexColor("#334155")))

        status_p_style = ParagraphStyle(
            f"St_{i}",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=6.5,
            leading=8.0,
            textColor=txt_col,
            alignment=1,
        )
        status_cell = Paragraph(f"<b>{esc(st)}</b>", status_p_style)

        client_text = f"<b>{esc(r['client_name'] or 'Cliente não informado')}</b>"
        if r["farm_name"]:
            client_text += f"<br/><font color='#64748b'>Fazenda: {esc(r['farm_name'])}</font>"

        city_route = f"{esc(r['city'] or '—')}"
        if r["route_name"]:
            city_route += f"<br/><font color='#64748b'>Rota: {esc(r['route_name'])}</font>"

        row_cells = [
            Paragraph(f"<b>{esc(r['order_number'])}</b>", td_bold),
            Paragraph(client_text, td_style),
            Paragraph(city_route, td_style),
        ]

        if show_seller:
            row_cells.append(Paragraph(esc(r["seller_name"] or "—"), td_style))

        if show_dates:
            dt_fmt = fmt_br_date(r["expected_delivery_date"])
            sla_tag = f"<br/><font color='{item['sla_text_col']}'><b>{esc(item['sla_text'])}</b></font>"
            row_cells.append(Paragraph(f"<b>{dt_fmt}</b>{sla_tag}", td_style))

        if show_weight:
            row_cells.append(Paragraph(fmt_weight(r["weight_kg"]), td_style))

        if show_financial:
            row_cells.append(Paragraph(fmt_money(r["total_value"]), td_style))

        row_cells.append(status_cell)

        if show_receiver:
            receiver_obs = ""
            if r["delivered_to"]:
                doc_type = esc(r["delivered_document_type"] or "Doc")
                doc_info = f" ({doc_type}: {esc(r['delivered_document'])})" if r["delivered_document"] else ""
                receiver_obs += f"<b>Recebeu:</b> {esc(r['delivered_to'])}{doc_info}"
            if r["final_notes"]:
                if receiver_obs:
                    receiver_obs += "<br/>"
                receiver_obs += f"<font color='#64748b'>Obs: {esc(r['final_notes'][:60])}</font>"
            if not receiver_obs:
                receiver_obs = "—"
            row_cells.append(Paragraph(receiver_obs, td_style))

        table_data.append(row_cells)
        table_styles.append(("BACKGROUND", (status_col_index, i), (status_col_index, i), bg_col))

        # Destaque de linha para pedidos atrasados
        if item["sla_prio"] == 1:
            table_styles.append(("BACKGROUND", (0, i), (0, i), colors.HexColor("#fee2e2")))
        elif item["sla_prio"] == 2:
            table_styles.append(("BACKGROUND", (0, i), (0, i), colors.HexColor("#fef3c7")))
        elif i % 2 == 0:
            for c_idx in range(len(headers)):
                if c_idx != status_col_index and c_idx != 0:
                    table_styles.append(("BACKGROUND", (c_idx, i), (c_idx, i), colors.HexColor("#f8fafc")))

    orders_table = Table(table_data, colWidths=widths, repeatRows=1)
    orders_table.setStyle(TableStyle(table_styles))
    elements.append(orders_table)

    doc.build(elements, canvasmaker=NumberedCanvas)
    return filepath


def generate_operational_sla_pdf_report(data, target_dir=None, filename_prefix="Relatorio_Operacional_SLA", config=None):
    """
    Gera o Relatório Executivo Operacional & SLA em PDF.
    Apresenta KPIs consolidados, desempenho por motorista, por rota, por cidade e ocorrências.
    """
    target_folder = target_dir or get_desktop_report_folder()
    os.makedirs(target_folder, exist_ok=True)

    cfg = config or data.get("config") or {}
    now_dt = datetime.now()
    stamp = now_dt.strftime("%Y-%m-%d_%H%M%S")
    filename = f"{filename_prefix}_{stamp}.pdf"
    filepath = os.path.join(target_folder, filename)

    doc = SimpleDocTemplate(
        filepath,
        pagesize=landscape(A4),
        leftMargin=1.2 * cm,
        rightMargin=1.2 * cm,
        topMargin=1.0 * cm,
        bottomMargin=1.6 * cm,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "OpHeaderTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=18,
        textColor=colors.HexColor("#ffffff"),
    )
    subtitle_style = ParagraphStyle(
        "OpHeaderSubTitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#e2e8f0"),
    )
    sec_title_style = ParagraphStyle(
        "OpSecTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#1e293b"),
        spaceAfter=3,
    )
    card_title_style = ParagraphStyle(
        "OpCardTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#475569"),
        alignment=1,
    )
    card_val_style = ParagraphStyle(
        "OpCardVal",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=13,
        alignment=1,
    )
    th_style = ParagraphStyle(
        "OpTH",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#0f172a"),
    )
    td_style = ParagraphStyle(
        "OpTD",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.0,
        leading=9.0,
        textColor=colors.HexColor("#1e293b"),
    )
    td_bold = ParagraphStyle(
        "OpTDBold",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=7.0,
        leading=9.0,
        textColor=colors.HexColor("#0f172a"),
    )

    elements = []

    # BANNER
    filter_desc = data.get("filter_desc", "Período: Completo")
    header_data = [
        [
            Paragraph("<b>LOGÍSTICA CASA DO CAMPO</b><br/><font size='10'>Relatório de Desempenho Operacional & SLA</font>", title_style),
            Paragraph(f"<b>Filtros:</b> {esc(filter_desc)}<br/><b>Emissão:</b> {now_dt.strftime('%d/%m/%Y às %H:%M')}", subtitle_style)
        ]
    ]
    header_table = Table(header_data, colWidths=[17.5 * cm, 9.8 * cm])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#174f2a")),
        ("PADDING", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 6))

    # CARDS KPI (Filtrados por preferência)
    totals = data.get("totals", {})
    can_view_fin = data.get("can_view_financial", False)
    show_fin = can_view_fin and is_cfg_true(cfg, "col_financial", True)

    all_kpi_cards = [
        ("kpi_total_orders", "TOTAL PEDIDOS", str(totals.get("total_orders", 0)), "#0f172a"),
        ("kpi_total_weight", "PESO TOTAL", fmt_weight(totals.get("total_weight", 0)), "#0f172a"),
        ("kpi_total_value", "VALOR TOTAL", fmt_money(totals.get("total_value", 0)) if show_fin else "Oculto", "#0f172a"),
        ("kpi_sla_rate", "ÍNDICE SLA", f"{totals.get('sla_rate', 100):.1f}%", "#15803d" if totals.get('sla_rate', 100) >= 90 else "#b91c1c"),
        ("kpi_delivered_ontime", "ENTREGUES NO PRAZO", str(totals.get("delivered_ontime", 0)), "#15803d"),
        ("kpi_delivered_late", "ENTREGUES FORA PRAZO", str(totals.get("delivered_late", 0)), "#b91c1c"),
        ("kpi_pending_late", "ABERTOS ATRASADOS", str(totals.get("pending_late", 0)), "#b91c1c" if totals.get("pending_late", 0) > 0 else "#64748b"),
        ("kpi_avg_days", "MÉDIA DIAS ENTREGA", f"{totals.get('avg_days_delivery', 0.0):.1f} d" if totals.get('avg_days_delivery') is not None else "—", "#0f172a"),
    ]
    if is_cfg_true(cfg, "kpi_lead_times", True):
        sale_to_inv = totals.get('avg_days_sale_to_inv')
        inv_to_del = totals.get('avg_days_inv_to_del')
        bot_label = totals.get('bottleneck_label', 'Equilibrado')
        bot_col = "#b91c1c" if bot_label in ("Logística", "Comercial") else "#15803d"
        all_kpi_cards.extend([
            ("kpi_lead_times", "VENDA ➔ FATURAMENTO", f"{sale_to_inv:.1f} d" if sale_to_inv is not None else "—", "#2563eb"),
            ("kpi_lead_times", "FATURAMENTO ➔ ENTREGA", f"{inv_to_del:.1f} d" if inv_to_del is not None else "—", "#d97706"),
            ("kpi_lead_times", "GARGALO PRINCIPAL", bot_label, bot_col),
        ])

    cards = [c[1:] for c in all_kpi_cards if is_cfg_true(cfg, c[0], True)]
    if not cards:
        cards = [c[1:] for c in all_kpi_cards]

    if len(cards) > 6:
        mid = (len(cards) + 1) // 2
        card_chunks = [cards[:mid], cards[mid:]]
    else:
        card_chunks = [cards]

    for chunk in card_chunks:
        card_titles = [Paragraph(f"<b>{c[0]}</b>", card_title_style) for c in chunk]
        card_vals = [
            Paragraph(f"<b>{c[1]}</b>", ParagraphStyle(f"CV_{c[0]}", parent=card_val_style, textColor=colors.HexColor(c[2])))
            for c in chunk
        ]
        w_card = (27.3 * cm) / len(chunk)
        cards_table = Table([card_titles, card_vals], colWidths=[w_card] * len(chunk))
        cards_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#ffffff")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 3),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(cards_table)
        elements.append(Spacer(1, 4))
    elements.append(Spacer(1, 4))

    show_bottlenecks = is_cfg_true(cfg, "sec_bottlenecks", True)
    if show_bottlenecks:
        elements.append(Paragraph("<b>🎯 Diagnóstico de Gargalos & Lead Times (Comercial vs Logística)</b>", sec_title_style))
        avg_s2i = totals.get("avg_days_sale_to_inv", 0.0) or 0.0
        avg_i2d = totals.get("avg_days_inv_to_del", 0.0) or 0.0
        avg_tot = totals.get("avg_days_delivery", 0.0) or 0.0
        pct_comm = totals.get("pct_time_commercial", 0.0) or 0.0
        pct_log = totals.get("pct_time_logistics", 0.0) or 0.0
        b_label = totals.get("bottleneck_label", "Equilibrado")
        b_desc = totals.get("bottleneck_desc", "")

        diag_text = (
            f"<b>Diagnóstico do Fluxo:</b> <font color='{'#b91c1c' if b_label in ('Logística', 'Comercial') else '#15803d'}'><b>{b_label}</b></font> — {esc(b_desc)}<br/>"
            f"• <b>Comercial (Venda ➔ Faturamento):</b> média de <b>{avg_s2i:.1f} dias</b> ({pct_comm:.1f}% do tempo de espera total)<br/>"
            f"• <b>Logística (Faturamento ➔ Entrega):</b> média de <b>{avg_i2d:.1f} dias</b> ({pct_log:.1f}% do tempo de espera total)<br/>"
            f"• <b>Ciclo Total Médio (Venda ➔ Entrega):</b> <b>{avg_tot:.1f} dias</b>"
        )

        diag_data = [
            [
                Paragraph(diag_text, td_style)
            ]
        ]
        diag_table = Table(diag_data, colWidths=[27.3 * cm])
        diag_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#f8fafc")),
            ("BOX", (0, 0), (0, 0), 0.5, colors.HexColor("#cbd5e1")),
            ("PADDING", (0, 0), (-1, -1), 5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        elements.append(diag_table)
        elements.append(Spacer(1, 8))

    show_drivers = is_cfg_true(cfg, "sec_drivers", True)
    show_vehicles = is_cfg_true(cfg, "sec_vehicles", True)
    show_routes = is_cfg_true(cfg, "sec_routes", True)
    show_cities = is_cfg_true(cfg, "sec_cities", True)
    show_problems = is_cfg_true(cfg, "sec_problems", True)
    show_orders = is_cfg_true(cfg, "sec_orders", False)

    # SEÇÃO 1: MOTORISTAS
    if show_drivers:
        elements.append(Paragraph("<b>1. Desempenho por Motorista</b>", sec_title_style))
        drivers = data.get("drivers", [])
        dr_headers = [
            Paragraph("Motorista", th_style),
            Paragraph("Pedidos", th_style),
            Paragraph("Entregues", th_style),
            Paragraph("Peso (kg)", th_style),
            Paragraph("Valor (R$)", th_style),
            Paragraph("Média Dias", th_style),
            Paragraph("Pontualidade SLA", th_style),
            Paragraph("Problemas", th_style),
        ]
        dr_widths = [5.5 * cm, 2.3 * cm, 2.5 * cm, 3.2 * cm, 3.5 * cm, 2.8 * cm, 4.0 * cm, 3.5 * cm]
        dr_data = [dr_headers]
        for d in drivers:
            dr_data.append([
                Paragraph(f"<b>{esc(d.get('driver_name','Sem motorista'))}</b>", td_style),
                Paragraph(str(d.get("total_orders", 0)), td_style),
                Paragraph(str(d.get("delivered_orders", 0)), td_style),
                Paragraph(fmt_weight(d.get("total_weight", 0)), td_style),
                Paragraph(fmt_money(d.get("total_value", 0)) if show_fin else "Oculto", td_style),
                Paragraph(f"{d.get('avg_days', 0.0):.1f} dia(s)" if d.get('avg_days') is not None else "—", td_style),
                Paragraph(f"<b>{d.get('sla_rate', 100):.1f}%</b> ({d.get('ontime_count',0)} no prazo / {d.get('late_count',0)} atraso)", td_style),
                Paragraph(str(d.get("problems_count", 0)), td_style),
            ])
        unassigned = data.get("unassigned_driver")
        if unassigned and unassigned.get("total_orders", 0) > 0:
            dr_data.append([
                Paragraph("<b>📦 Aguardando Carga</b>", td_style),
                Paragraph(str(unassigned.get("total_orders", 0)), td_style),
                Paragraph(str(unassigned.get("delivered_orders", 0)), td_style),
                Paragraph(fmt_weight(unassigned.get("total_weight", 0)), td_style),
                Paragraph(fmt_money(unassigned.get("total_value", 0)) if show_fin else "Oculto", td_style),
                Paragraph("—", td_style),
                Paragraph("—", td_style),
                Paragraph("0", td_style),
            ])

        tot_dr_orders = sum(d.get('total_orders', 0) for d in drivers)
        tot_dr_del = sum(d.get('delivered_orders', 0) for d in drivers)
        tot_dr_w = sum(d.get('total_weight', 0) for d in drivers)
        tot_dr_v = sum(d.get('total_value', 0) for d in drivers)
        tot_dr_on = sum(d.get('ontime_count', 0) for d in drivers)
        tot_dr_prob = sum(d.get('problems_count', 0) for d in drivers)
        overall_sla = (tot_dr_on / (tot_dr_del or 1) * 100) if tot_dr_del > 0 else 100.0

        if drivers:
            dr_data.append([
                Paragraph(f"<b>Total ({len(drivers)} motoristas)</b>", td_style),
                Paragraph(f"<b>{tot_dr_orders}</b>", td_style),
                Paragraph(f"<b>{tot_dr_del}</b>", td_style),
                Paragraph(f"<b>{fmt_weight(tot_dr_w)}</b>", td_style),
                Paragraph(f"<b>{fmt_money(tot_dr_v) if show_fin else 'Oculto'}</b>", td_style),
                Paragraph("—", td_style),
                Paragraph(f"<b>{overall_sla:.1f}%</b>", td_style),
                Paragraph(f"<b>{tot_dr_prob}</b>", td_style),
            ])

        if not drivers and not (unassigned and unassigned.get("total_orders", 0) > 0):
            dr_data.append([Paragraph("Nenhum dado de motorista para o período filtrado.", td_style)] + [Paragraph("", td_style)] * 7)

        dr_table = Table(dr_data, colWidths=dr_widths, repeatRows=1)
        dr_table_styles = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 3),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        if drivers:
            dr_table_styles.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f8fafc")))
        dr_table.setStyle(TableStyle(dr_table_styles))
        elements.append(dr_table)
        elements.append(Spacer(1, 8))

    # SEÇÃO: CAMINHÕES / VEÍCULOS
    if show_vehicles:
        elements.append(Paragraph("<b>Desempenho por Caminhão / Veículo</b>", sec_title_style))
        vehicles = data.get("vehicles", [])
        vh_headers = [
            Paragraph("Caminhão / Modelo", th_style),
            Paragraph("Placa", th_style),
            Paragraph("Capacidade", th_style),
            Paragraph("Cargas", th_style),
            Paragraph("Pedidos", th_style),
            Paragraph("Entregues", th_style),
            Paragraph("Peso (kg)", th_style),
            Paragraph("% Ocupação", th_style),
            Paragraph("Valor (R$)", th_style),
            Paragraph("Pontualidade SLA", th_style),
        ]
        vh_widths = [4.2 * cm, 2.2 * cm, 2.3 * cm, 1.8 * cm, 1.8 * cm, 1.8 * cm, 2.7 * cm, 2.2 * cm, 3.5 * cm, 4.8 * cm]
        vh_data = [vh_headers]
        for v in vehicles:
            v_occ = v.get("occupation_rate", 0.0)
            vh_data.append([
                Paragraph(f"<b>{esc(v.get('model', v.get('vehicle_name', '')))}</b>", td_style),
                Paragraph(esc(v.get("plate", "—")), td_style),
                Paragraph(fmt_weight(v.get("capacity_kg", 0)), td_style),
                Paragraph(str(v.get("loads_count", 0)), td_style),
                Paragraph(str(v.get("total_orders", 0)), td_style),
                Paragraph(str(v.get("delivered_orders", 0)), td_style),
                Paragraph(fmt_weight(v.get("total_weight", 0)), td_style),
                Paragraph(f"<b>{v_occ:.1f}%</b>", td_style),
                Paragraph(fmt_money(v.get("total_value", 0)) if show_fin else "Oculto", td_style),
                Paragraph(f"<b>{v.get('sla_rate', 100):.1f}%</b> ({v.get('ontime_count',0)} no prazo / {v.get('late_count',0)} atraso)", td_style),
            ])
        unassigned_v = data.get("unassigned_vehicle")
        if unassigned_v and unassigned_v.get("total_orders", 0) > 0:
            vh_data.append([
                Paragraph("<b>📦 Aguardando Caminhão</b>", td_style),
                Paragraph("—", td_style),
                Paragraph("—", td_style),
                Paragraph("0", td_style),
                Paragraph(str(unassigned_v.get("total_orders", 0)), td_style),
                Paragraph(str(unassigned_v.get("delivered_orders", 0)), td_style),
                Paragraph(fmt_weight(unassigned_v.get("total_weight", 0)), td_style),
                Paragraph("—", td_style),
                Paragraph(fmt_money(unassigned_v.get("total_value", 0)) if show_fin else "Oculto", td_style),
                Paragraph("—", td_style),
            ])

        tot_vh_loads = sum(v.get('loads_count', 0) for v in vehicles)
        tot_vh_orders = sum(v.get('total_orders', 0) for v in vehicles)
        tot_vh_del = sum(v.get('delivered_orders', 0) for v in vehicles)
        tot_vh_w = sum(v.get('total_weight', 0) for v in vehicles)
        tot_vh_v = sum(v.get('total_value', 0) for v in vehicles)
        tot_vh_on = sum(v.get('ontime_count', 0) for v in vehicles)
        overall_vh_sla = (tot_vh_on / (tot_vh_del or 1) * 100) if tot_vh_del > 0 else 100.0

        tot_vh_cap = sum((v.get('loads_count', 0) * v.get('capacity_kg', 0.0)) for v in vehicles)
        overall_vh_occ = (tot_vh_w / tot_vh_cap * 100) if tot_vh_cap > 0 else 0.0

        if vehicles:
            vh_data.append([
                Paragraph(f"<b>Total ({len(vehicles)} veículos)</b>", td_style),
                Paragraph("—", td_style),
                Paragraph("—", td_style),
                Paragraph(f"<b>{tot_vh_loads}</b>", td_style),
                Paragraph(f"<b>{tot_vh_orders}</b>", td_style),
                Paragraph(f"<b>{tot_vh_del}</b>", td_style),
                Paragraph(f"<b>{fmt_weight(tot_vh_w)}</b>", td_style),
                Paragraph(f"<b>{overall_vh_occ:.1f}%</b>", td_style),
                Paragraph(f"<b>{fmt_money(tot_vh_v) if show_fin else 'Oculto'}</b>", td_style),
                Paragraph(f"<b>{overall_vh_sla:.1f}%</b>", td_style),
            ])

        if not vehicles and not (unassigned_v and unassigned_v.get("total_orders", 0) > 0):
            vh_data.append([Paragraph("Nenhum dado de caminhão para o período filtrado.", td_style)] + [Paragraph("", td_style)] * 9)

        vh_table = Table(vh_data, colWidths=vh_widths, repeatRows=1)
        vh_table_styles = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 3),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        if vehicles:
            vh_table_styles.append(("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f8fafc")))
        vh_table.setStyle(TableStyle(vh_table_styles))
        elements.append(vh_table)
        elements.append(Spacer(1, 8))

    # SEÇÃO 2: ROTAS
    if show_routes:
        elements.append(Paragraph("<b>2. Desempenho por Rota</b>", sec_title_style))
        routes = data.get("routes", [])
        show_lead = is_cfg_true(cfg, "col_lead_times", True)
        rt_headers = [
            Paragraph("Rota", th_style),
            Paragraph("Total Pedidos", th_style),
            Paragraph("Entregues", th_style),
            Paragraph("Peso Total (kg)", th_style),
            Paragraph("% do Peso", th_style),
            Paragraph("Valor Total (R$)", th_style),
            Paragraph("Pontualidade SLA", th_style),
        ]
        if show_lead:
            rt_headers.extend([
                Paragraph("Venda➔Fat", th_style),
                Paragraph("Fat➔Entr", th_style),
                Paragraph("Gargalo", th_style),
            ])
            rt_widths = [4.5 * cm, 2.0 * cm, 2.0 * cm, 3.0 * cm, 1.8 * cm, 3.2 * cm, 4.8 * cm, 2.0 * cm, 2.0 * cm, 2.0 * cm]
        else:
            rt_widths = [5.5 * cm, 2.5 * cm, 2.5 * cm, 3.8 * cm, 2.5 * cm, 4.0 * cm, 6.5 * cm]
        rt_data = [rt_headers]
        tot_weight = totals.get("total_weight", 0) or 1
        for r in routes:
            pct_w = (r.get("total_weight", 0) / tot_weight * 100) if tot_weight else 0
            row_r = [
                Paragraph(f"<b>{esc(r.get('route_name','Sem rota'))}</b>", td_style),
                Paragraph(str(r.get("total_orders", 0)), td_style),
                Paragraph(str(r.get("delivered_orders", 0)), td_style),
                Paragraph(fmt_weight(r.get("total_weight", 0)), td_style),
                Paragraph(f"{pct_w:.1f}%", td_style),
                Paragraph(fmt_money(r.get("total_value", 0)) if show_fin else "Oculto", td_style),
                Paragraph(f"<b>{r.get('sla_rate', 100):.1f}%</b> ({r.get('ontime_count',0)} no prazo / {r.get('late_count',0)} atraso)", td_style),
            ]
            if show_lead:
                s2i = r.get("avg_days_sale_to_inv")
                i2d = r.get("avg_days_inv_to_del")
                b_st = r.get("bottleneck_stage", "—")
                row_r.extend([
                    Paragraph(f"{s2i:.1f} d" if s2i is not None else "—", td_style),
                    Paragraph(f"{i2d:.1f} d" if i2d is not None else "—", td_style),
                    Paragraph(f"<b>{esc(b_st)}</b>", td_style),
                ])
            rt_data.append(row_r)
        if not routes:
            empty_cols = 9 if show_lead else 6
            rt_data.append([Paragraph("Nenhum dado de rota para o período filtrado.", td_style)] + [Paragraph("", td_style)] * empty_cols)

        rt_table = Table(rt_data, colWidths=rt_widths, repeatRows=1)
        rt_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 3),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(rt_table)
        elements.append(Spacer(1, 8))

    # SEÇÃO 3: CIDADES
    if show_cities:
        elements.append(Paragraph("<b>3. Desempenho por Cidade</b>", sec_title_style))
        cities = data.get("cities", [])
        ct_headers = [
            Paragraph("Cidade", th_style),
            Paragraph("Rota Base", th_style),
            Paragraph("Total Pedidos", th_style),
            Paragraph("Entregues", th_style),
            Paragraph("Peso Total (kg)", th_style),
            Paragraph("Valor Total (R$)", th_style),
        ]
        ct_widths = [6.5 * cm, 5.0 * cm, 3.0 * cm, 3.0 * cm, 4.5 * cm, 5.3 * cm]
        ct_data = [ct_headers]
        for c in cities:
            ct_data.append([
                Paragraph(f"<b>{esc(c.get('city_name','Sem cidade'))}</b>", td_style),
                Paragraph(esc(c.get("route_name", "—")), td_style),
                Paragraph(str(c.get("total_orders", 0)), td_style),
                Paragraph(str(c.get("delivered_orders", 0)), td_style),
                Paragraph(fmt_weight(c.get("total_weight", 0)), td_style),
                Paragraph(fmt_money(c.get("total_value", 0)) if show_fin else "Oculto", td_style),
            ])
        if not cities:
            ct_data.append([Paragraph("Nenhum dado de cidade para o período filtrado.", td_style)] + [Paragraph("", td_style)] * 5)

        ct_table = Table(ct_data, colWidths=ct_widths, repeatRows=1)
        ct_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 3),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(ct_table)

    # SEÇÃO 4: OCORRÊNCIAS (se habilitado e houver dados)
    problems = data.get("problems", [])
    if show_problems and problems:
        elements.append(Spacer(1, 8))
        elements.append(Paragraph("<b>4. Ocorrências & Problemas de Entrega</b>", sec_title_style))
        pb_headers = [
            Paragraph("Motivo da Ocorrência", th_style),
            Paragraph("Quantidade", th_style),
            Paragraph("% do Total", th_style),
        ]
        pb_widths = [14.0 * cm, 6.5 * cm, 6.8 * cm]
        pb_data = [pb_headers]
        tot_prob = sum(p.get("c", 0) for p in problems) or 1
        for p in problems:
            pct_p = (p.get("c", 0) / tot_prob * 100) if tot_prob else 0
            pb_data.append([
                Paragraph(f"<b>{esc(p.get('problem_type','Sem motivo'))}</b>", td_style),
                Paragraph(str(p.get("c", 0)), td_style),
                Paragraph(f"{pct_p:.1f}%", td_style),
            ])
        pb_table = Table(pb_data, colWidths=pb_widths, repeatRows=1)
        pb_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#fee2e2")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#ef4444")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#fecaca")),
            ("PADDING", (0, 0), (-1, -1), 3),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(pb_table)

    # SEÇÃO 5: LISTAGEM ANALÍTICA DE PEDIDOS (se habilitado)
    orders = data.get("orders", [])
    if show_orders and orders:
        elements.append(Spacer(1, 8))
        elements.append(Paragraph(f"<b>5. Listagem de Pedidos ({len(orders)} pedidos)</b>", sec_title_style))
        show_lead_ord = is_cfg_true(cfg, "col_lead_times", True)
        ord_headers = [
            Paragraph("Pedido", th_style),
            Paragraph("Vendedor", th_style),
            Paragraph("Status", th_style),
            Paragraph("Rota/Cidade", th_style),
            Paragraph("Motorista", th_style),
            Paragraph("Peso (kg)", th_style),
            Paragraph("Valor (R$)", th_style),
            Paragraph("Entrega", th_style),
            Paragraph("SLA", th_style),
        ]
        if show_lead_ord:
            ord_headers.extend([
                Paragraph("Venda➔Fat", th_style),
                Paragraph("Fat➔Entr", th_style),
                Paragraph("Gargalo", th_style),
            ])
            ord_widths = [2.2 * cm, 3.0 * cm, 2.2 * cm, 4.0 * cm, 3.5 * cm, 2.0 * cm, 2.4 * cm, 2.2 * cm, 2.0 * cm, 1.3 * cm, 1.3 * cm, 1.2 * cm]
        else:
            ord_widths = [2.5 * cm, 3.5 * cm, 2.5 * cm, 5.0 * cm, 4.0 * cm, 2.3 * cm, 2.7 * cm, 2.5 * cm, 2.3 * cm]
        ord_data = [ord_headers]
        for o in orders:
            row_o = [
                Paragraph(f"<b>{esc(o.get('order_number',''))}</b>", td_style),
                Paragraph(esc(o.get("seller_name", "—")), td_style),
                Paragraph(esc(o.get("status", "")), td_style),
                Paragraph(f"{esc(o.get('route_name',''))} / {esc(o.get('city',''))}", td_style),
                Paragraph(esc(o.get("driver_name", "—")), td_style),
                Paragraph(fmt_weight(o.get("weight_kg", 0)), td_style),
                Paragraph(fmt_money(o.get("total_value", 0)) if show_fin else "Oculto", td_style),
                Paragraph(fmt_br_date(o.get("delivered_at")), td_style),
                Paragraph(esc(o.get("expected_delivery_date", "—")), td_style),
            ]
            if show_lead_ord:
                s2i = o.get("days_sale_to_inv")
                i2d = o.get("days_inv_to_del")
                b_st = o.get("bottleneck_stage", "—")
                row_o.extend([
                    Paragraph(f"{s2i} d" if s2i is not None else "—", td_style),
                    Paragraph(f"{i2d} d" if i2d is not None else "—", td_style),
                    Paragraph(f"<b>{esc(b_st)}</b>", td_style),
                ])
            ord_data.append(row_o)
        ord_table = Table(ord_data, colWidths=ord_widths, repeatRows=1)
        ord_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("PADDING", (0, 0), (-1, -1), 2.5),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(ord_table)

    doc.build(elements, canvasmaker=NumberedCanvas)
    return filepath

