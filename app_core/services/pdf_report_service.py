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

    # 1. Filtros de Status (Checkboxes)
    included_statuses = []
    if config.get("pdf_report_st_venda", "1") == "1":
        included_statuses.append("Venda")
    if config.get("pdf_report_st_faturado", "1") == "1":
        included_statuses.append("Faturado")
    if config.get("pdf_report_st_rota", "1") == "1":
        included_statuses.append("Saiu para entrega")
    if config.get("pdf_report_st_agendado", "1") == "1":
        included_statuses.append("Agendado")
    if config.get("pdf_report_st_acertado", "1") == "1":
        included_statuses.extend(["Acertado", "Entregue"])
    if config.get("pdf_report_st_problema", "1") == "1":
        included_statuses.append("Problema")
    if config.get("pdf_report_st_cancelado", "0") == "1":
        included_statuses.append("Cancelado")

    if not included_statuses:
        included_statuses = ["Venda", "Faturado", "Saiu para entrega", "Agendado", "Acertado", "Entregue", "Problema", "Cancelado"]

    query = f"""
        SELECT o.*, c.name AS client_name, c.farm_name,
               (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id=o.id) AS item_count
        FROM orders o
        LEFT JOIN clients c ON c.id=o.client_id
        WHERE o.status IN ({','.join(['?']*len(included_statuses))})
    """
    db_rows = db.execute(query, tuple(included_statuses)).fetchall()

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
    show_financial = config.get("pdf_report_col_financial", "1") == "1"
    show_weight = config.get("pdf_report_col_weight", "1") == "1"
    show_dates = config.get("pdf_report_col_dates", "1") == "1"
    show_seller = config.get("pdf_report_col_seller", "1") == "1"
    show_receiver = config.get("pdf_report_col_receiver", "1") == "1"

    # Opções de Cards do Cabeçalho (Checkboxes)
    hdr_orders = config.get("pdf_hdr_total_orders", "1") == "1"
    hdr_weight = config.get("pdf_hdr_total_weight", "1") == "1"
    # Valor Total só aparece no cabeçalho se AMBAS as opções (hdr e financial) estiverem marcadas
    hdr_value = (config.get("pdf_hdr_total_value", "1") == "1") and show_financial
    hdr_late = config.get("pdf_hdr_late", "1") == "1"
    hdr_risk = config.get("pdf_hdr_risk", "1") == "1"
    hdr_ontime = config.get("pdf_hdr_ontime", "1") == "1"
    hdr_scheduled = config.get("pdf_hdr_scheduled", "1") == "1"
    hdr_delivered = config.get("pdf_hdr_delivered", "1") == "1"
    hdr_problems = config.get("pdf_hdr_problems", "0") == "1"
    hdr_canceled = config.get("pdf_hdr_canceled", "0") == "1"

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
