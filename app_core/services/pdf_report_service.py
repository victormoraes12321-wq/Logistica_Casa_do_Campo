import os
import sys
import html
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape, A4
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, HRFlowable
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
        self.drawRightString(A4[1] - 1.5 * cm, 1.0 * cm, footer_text)
        self.drawString(1.5 * cm, 1.0 * cm, "CONFIDENCIAL — USO INTERNO")
        self.setStrokeColor(colors.HexColor("#cbd5e1"))
        self.setLineWidth(0.5)
        self.line(1.5 * cm, 1.4 * cm, A4[1] - 1.5 * cm, 1.4 * cm)
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
    Respeita as escolhas de checkboxes (status a incluir e colunas visíveis).
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
    if config.get("pdf_report_st_acertado", "1") == "1":
        included_statuses.extend(["Acertado", "Entregue"])
    if config.get("pdf_report_st_problema", "1") == "1":
        included_statuses.append("Problema")
    if config.get("pdf_report_st_cancelado", "0") == "1":
        included_statuses.append("Cancelado")

    if not included_statuses:
        included_statuses = ["Venda", "Faturado", "Saiu para entrega", "Acertado", "Entregue", "Problema", "Cancelado"]

    query = f"""
        SELECT o.*, c.name AS client_name, c.farm_name,
               (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id=o.id) AS item_count
        FROM orders o
        LEFT JOIN clients c ON c.id=o.client_id
        WHERE o.status IN ({','.join(['?']*len(included_statuses))})
        ORDER BY o.id DESC
    """
    rows = db.execute(query, tuple(included_statuses)).fetchall()

    # Opções de Exibição de Colunas (Checkboxes)
    show_financial = config.get("pdf_report_col_financial", "1") == "1"
    show_weight = config.get("pdf_report_col_weight", "1") == "1"
    show_dates = config.get("pdf_report_col_dates", "1") == "1"
    show_seller = config.get("pdf_report_col_seller", "1") == "1"
    show_receiver = config.get("pdf_report_col_receiver", "1") == "1"
    show_items = config.get("pdf_report_col_items", "0") == "1"

    # 2. Resumo
    total_orders = len(rows)
    total_weight = sum(float(r["weight_kg"] or 0) for r in rows)
    total_value = sum(float(r["total_value"] or 0) for r in rows)
    total_delivered = sum(1 for r in rows if str(r["status"]).strip() in ("Acertado", "Entregue"))
    total_problems = sum(1 for r in rows if str(r["status"]).strip() == "Problema")
    total_canceled = sum(1 for r in rows if str(r["status"]).strip() == "Cancelado")

    # 3. Documento PDF em formato Paisagem (Landscape A4)
    doc = SimpleDocTemplate(
        filepath,
        pagesize=landscape(A4),
        leftMargin=1.2 * cm,
        rightMargin=1.2 * cm,
        topMargin=1.2 * cm,
        bottomMargin=1.8 * cm,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "HeaderTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#ffffff"),
    )
    subtitle_style = ParagraphStyle(
        "HeaderSubTitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#e2e8f0"),
    )
    card_title_style = ParagraphStyle(
        "CardTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#475569"),
        alignment=1,
    )
    card_value_style = ParagraphStyle(
        "CardValue",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=14,
        textColor=colors.HexColor("#0f172a"),
        alignment=1,
    )
    th_style = ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#1e293b"),
    )
    td_style = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9.5,
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
            Paragraph(f"<b>Relatório Personalizado de Pedidos</b><br/>Emissão: {now_dt.strftime('%d/%m/%Y %H:%M')}", subtitle_style)
        ]
    ]
    header_table = Table(header_data, colWidths=[18 * cm, 9.3 * cm])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#174f2a")),
        ("PADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 10))

    # CARDS DE RESUMO
    cards_data = [
        [
            Paragraph("TOTAL PEDIDOS", card_title_style),
            Paragraph("PESO TOTAL", card_title_style),
            Paragraph("VALOR TOTAL", card_title_style),
            Paragraph("ENTREGUES", card_title_style),
            Paragraph("COM PROBLEMA", card_title_style),
            Paragraph("CANCELADOS", card_title_style),
        ],
        [
            Paragraph(str(total_orders), card_value_style),
            Paragraph(fmt_weight(total_weight), card_value_style),
            Paragraph(fmt_money(total_value), card_value_style),
            Paragraph(str(total_delivered), card_value_style),
            Paragraph(str(total_problems), card_value_style),
            Paragraph(str(total_canceled), card_value_style),
        ]
    ]
    col_w = (27.3 * cm) / 6
    summary_table = Table(cards_data, colWidths=[col_w] * 6)
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#ffffff")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 12))

    # MONTAGEM DINÂMICA DAS COLUNAS
    headers = [Paragraph("Nº Pedido", th_style), Paragraph("Cliente / Fazenda", th_style), Paragraph("Cidade / Rota", th_style)]
    widths = [2.3 * cm, 5.2 * cm, 4.0 * cm]

    if show_seller:
        headers.append(Paragraph("Vendedor", th_style))
        widths.append(3.0 * cm)

    if show_dates:
        headers.append(Paragraph("Data Prev.", th_style))
        widths.append(2.2 * cm)

    if show_weight:
        headers.append(Paragraph("Peso (kg)", th_style))
        widths.append(2.2 * cm)

    if show_financial:
        headers.append(Paragraph("Valor (R$)", th_style))
        widths.append(2.8 * cm)

    headers.append(Paragraph("Status", th_style))
    widths.append(2.6 * cm)

    if show_receiver:
        headers.append(Paragraph("Recebedor / Obs", th_style))
        widths.append(5.0 * cm)

    # Ajustar proporção de larguras para preencher 27.3 cm
    total_w = sum(widths)
    scale = (27.3 * cm) / total_w
    widths = [w * scale for w in widths]

    table_data = [headers]

    status_colors = {
        "Acertado": (colors.HexColor("#dff5e7"), colors.HexColor("#13733b")),
        "Entregue": (colors.HexColor("#dff5e7"), colors.HexColor("#13733b")),
        "Saiu para entrega": (colors.HexColor("#dbeafe"), colors.HexColor("#1e40af")),
        "Faturado": (colors.HexColor("#fef3c7"), colors.HexColor("#92400e")),
        "Venda": (colors.HexColor("#e0f2fe"), colors.HexColor("#0369a1")),
        "Problema": (colors.HexColor("#ffe0dc"), colors.HexColor("#99261d")),
        "Cancelado": (colors.HexColor("#f3f4f6"), colors.HexColor("#4b5563")),
    }

    table_styles = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("PADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]

    status_col_index = 3
    if show_seller: status_col_index += 1
    if show_dates: status_col_index += 1
    if show_weight: status_col_index += 1
    if show_financial: status_col_index += 1

    for i, r in enumerate(rows, start=1):
        st = str(r["status"] or "").strip()
        bg_col, txt_col = status_colors.get(st, (colors.HexColor("#f1f5f9"), colors.HexColor("#334155")))

        status_p_style = ParagraphStyle(
            f"Status_{i}",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=8,
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
            row_cells.append(Paragraph(fmt_br_date(r["expected_delivery_date"]), td_style))

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

        if i % 2 == 0:
            for c_idx in range(len(headers)):
                if c_idx != status_col_index:
                    table_styles.append(("BACKGROUND", (c_idx, i), (c_idx, i), colors.HexColor("#f8fafc")))

    orders_table = Table(table_data, colWidths=widths, repeatRows=1)
    orders_table.setStyle(TableStyle(table_styles))
    elements.append(orders_table)

    doc.build(elements, canvasmaker=NumberedCanvas)
    return filepath
