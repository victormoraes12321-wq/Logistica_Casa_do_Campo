from __future__ import annotations

import csv
import html
import io
import os
from datetime import datetime, date
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, KeepTogether
)


def _esc(val: Any) -> str:
    return html.escape(str(val or ""))


def _fmt_money(val: float) -> str:
    return f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_num(val: float, decimals: int = 0) -> str:
    if decimals == 0:
        return f"{val:,.0f}".replace(",", ".")
    return f"{val:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def get_driver_report_data(db: Any, start_date: str, end_date: str, driver_name: str | None = None) -> dict[str, Any]:
    params = [start_date, end_date]
    driver_filter = ""
    if driver_name and driver_name.strip():
        driver_filter = "AND LOWER(d.name) = LOWER(?)"
        params.append(driver_name.strip())

    trips_query = f"""
        SELECT r.id, r.date, r.name as route_name, r.driver_id, d.name as driver_name,
               r.vehicle_id, v.name as vehicle_name, v.plate,
               COALESCE(r.declared_weight, r.total_weight, 0) as weight_kg,
               COALESCE(r.km_start, 0) as km_start,
               COALESCE(r.km_end, 0) as km_end,
               COALESCE(r.km_total, 0) as km_total,
               r.status, r.destination, r.helper_name, r.departure_time, r.arrival_time,
               (SELECT COUNT(DISTINCT ro.order_id) FROM route_orders ro WHERE ro.route_id = r.id) as orders_count,
               (SELECT COUNT(DISTINCT ro.order_id) FROM route_orders ro 
                JOIN orders o ON o.id = ro.order_id 
                WHERE ro.route_id = r.id AND (ro.status IN ('Entregue', 'Acertado') OR o.status IN ('Entregue', 'Acertado'))) as delivered_count
        FROM routes r
        JOIN drivers d ON d.id = r.driver_id
        LEFT JOIN vehicles v ON v.id = r.vehicle_id
        WHERE COALESCE(DATE(r.departure_time), DATE(r.date), DATE(r.arrival_time), DATE(r.created_at)) >= ? 
          AND COALESCE(DATE(r.departure_time), DATE(r.date), DATE(r.arrival_time), DATE(r.created_at)) <= ? {driver_filter}
        ORDER BY COALESCE(r.arrival_time, r.departure_time, r.date, r.created_at) DESC, r.id DESC
    """
    trip_rows = db.execute(trips_query, tuple(params)).fetchall()

    route_ids = [t["id"] for t in trip_rows]
    expenses_by_route: dict[int, list[dict[str, Any]]] = {}
    if route_ids:
        placeholders = ",".join("?" for _ in route_ids)
        exp_rows = db.execute(
            f"SELECT * FROM trip_expenses WHERE route_id IN ({placeholders}) ORDER BY id",
            route_ids
        ).fetchall()
        for er in exp_rows:
            expenses_by_route.setdefault(er["route_id"], []).append(dict(er))

    # Process individual trips
    trips_list = []
    drivers_map: dict[str, dict[str, Any]] = {}

    for t in trip_rows:
        rid = t["id"]
        t_exps = expenses_by_route.get(rid, [])
        
        lunch_val = sum(float(e.get("total_value") or 0) for e in t_exps if "almoço" in (e.get("expense_type") or "").lower())
        overnight_qty = sum(float(e.get("quantity") or 0) for e in t_exps if "pernoite" in (e.get("expense_type") or "").lower() or "diária" in (e.get("expense_type") or "").lower())
        overnight_val = sum(float(e.get("total_value") or 0) for e in t_exps if "pernoite" in (e.get("expense_type") or "").lower() or "diária" in (e.get("expense_type") or "").lower())
        fuel_val = sum(float(e.get("total_value") or 0) for e in t_exps if "combustível" in (e.get("expense_type") or "").lower() or "combustivel" in (e.get("expense_type") or "").lower())
        toll_val = sum(float(e.get("total_value") or 0) for e in t_exps if "pedágio" in (e.get("expense_type") or "").lower() or "pedagio" in (e.get("expense_type") or "").lower())
        other_val = sum(float(e.get("total_value") or 0) for e in t_exps if not any(k in (e.get("expense_type") or "").lower() for k in ["almoço", "almoco", "pernoite", "diária", "diaria", "combustível", "combustivel", "pedágio", "pedagio"]))
        total_exp = sum(float(e.get("total_value") or 0) for e in t_exps)

        trip_dict = dict(t)
        trip_dict["expenses"] = t_exps
        trip_dict["lunch_val"] = lunch_val
        trip_dict["overnight_qty"] = overnight_qty
        trip_dict["overnight_val"] = overnight_val
        trip_dict["fuel_val"] = fuel_val
        trip_dict["toll_val"] = toll_val
        trip_dict["other_val"] = other_val
        trip_dict["total_exp"] = total_exp
        trips_list.append(trip_dict)

        dname = t["driver_name"] or "Não identificado"
        if dname not in drivers_map:
            drivers_map[dname] = {
                "driver_name": dname,
                "trips_count": 0,
                "delivered_count": 0,
                "weight_total": 0.0,
                "km_total": 0.0,
                "diarias_count": 0.0,
                "lunch_val": 0.0,
                "overnight_val": 0.0,
                "fuel_val": 0.0,
                "toll_val": 0.0,
                "other_val": 0.0,
                "expenses_total": 0.0,
            }

        dm = drivers_map[dname]
        # Multi-day trips count as 1 trip because 1 route record = 1 consolidated load
        dm["trips_count"] += 1
        dm["delivered_count"] += int(t["delivered_count"] or 0)
        dm["weight_total"] += float(t["weight_kg"] or 0)
        dm["km_total"] += float(t["km_total"] or 0)
        dm["diarias_count"] += overnight_qty
        dm["lunch_val"] += lunch_val
        dm["overnight_val"] += overnight_val
        dm["fuel_val"] += fuel_val
        dm["toll_val"] += toll_val
        dm["other_val"] += other_val
        dm["expenses_total"] += total_exp

    drivers_summary = sorted(drivers_map.values(), key=lambda x: x["driver_name"])

    kpi = {
        "total_trips": len(trips_list),
        "total_delivered": sum(d["delivered_count"] for d in drivers_summary),
        "total_weight": sum(d["weight_total"] for d in drivers_summary),
        "total_km": sum(d["km_total"] for d in drivers_summary),
        "total_diarias": sum(d["diarias_count"] for d in drivers_summary),
        "total_expenses": sum(d["expenses_total"] for d in drivers_summary),
    }

    return {
        "start_date": start_date,
        "end_date": end_date,
        "driver_filter": driver_name or "",
        "drivers_summary": drivers_summary,
        "trips": trips_list,
        "kpi": kpi
    }


def render_relatorio_motoristas_html(handler: Any, user: Any, qs: dict[str, list[str]], can_view_financial: bool) -> str:
    today_iso = date.today().isoformat()
    start_default = date.today().replace(day=1).isoformat()
    start = (qs.get("start", [start_default])[0] or start_default).strip()
    end = (qs.get("end", [today_iso])[0] or today_iso).strip()
    driver_param = (qs.get("driver", [""])[0] or "").strip()

    with handler.conn() as db:
        driver_names = [r["name"] for r in db.execute("SELECT name FROM drivers WHERE active=1 ORDER BY name")]
        data = get_driver_report_data(db, start, end, driver_param)

    kpi = data["kpi"]
    drivers_summary = data["drivers_summary"]
    trips = data["trips"]

    # Build driver options
    driver_options_parts = [f'<option value="" {"selected" if not driver_param else ""}>Todos os motoristas</option>']
    for dn in driver_names:
        sel = "selected" if dn.lower() == driver_param.lower() else ""
        driver_options_parts.append(f'<option value="{_esc(dn)}" {sel}>{_esc(dn)}</option>')
    driver_options_html = "".join(driver_options_parts)

    # Top nav bar with 3 tabs
    top_bar = f"""
    <div class="rep-top-bar">
      <div class="rep-nav-group">
        <a href="/relatorios?tab=operacional" class="rep-nav-item"><span>📊</span> Desempenho Operacional & SLA</a>
        <a href="/relatorios?tab=pedidos" class="rep-nav-item"><span>📦</span> Relatório de Pedidos</a>
        <a href="/relatorios?tab=motoristas" class="rep-nav-item active"><span>🚚</span> Relatório de Motoristas</a>
      </div>
      <div class="rep-top-actions">
        <button type="submit" form="driverReportForm" formaction="/relatorios/motoristas/export-pdf" class="rep-btn-cta-pdf" title="Baixar relatório formatado para impressão em PDF">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>
          <span>📥 Emitir Relatório PDF</span>
        </button>
        <button type="submit" form="driverReportForm" formaction="/relatorios/motoristas/export-csv" class="rep-btn-cta-csv" title="Exportar dados consolidados em planilha CSV">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
          <span>📊 Exportar Planilha (CSV)</span>
        </button>
      </div>
    </div>
    """

    # Filter section
    filter_card = f"""
    <section class="rep-card">
      <form id="driverReportForm" method="get" action="/relatorios">
        <input type="hidden" name="tab" value="motoristas">
        <div class="rep-filter-header">
          <div class="rep-presets">
            <span class="rep-presets-title">Período Rápido:</span>
            <button type="button" class="rep-preset-pill" onclick="setRepDates('today', 'driverReportForm')">Hoje</button>
            <button type="button" class="rep-preset-pill" onclick="setRepDates('7days', 'driverReportForm')">Últimos 7 dias</button>
            <button type="button" class="rep-preset-pill" onclick="setRepDates('this_month', 'driverReportForm')">Este Mês</button>
            <button type="button" class="rep-preset-pill" onclick="setRepDates('last_month', 'driverReportForm')">Mês Passado</button>
          </div>
          <div class="rep-active-badge">
            <span>📅 Período ativo: <b>{_esc(start)}</b> até <b>{_esc(end)}</b></span>
          </div>
        </div>

        <div class="rep-grid-filters" style="grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));">
          <div class="rep-input-group">
            <label style="font-size:12px; font-weight:700; color:#334155;">Data Inicial</label>
            <input type="date" name="start" value="{_esc(start)}" style="padding:8px 10px; border:1px solid #cbd5e1; border-radius:8px; font-size:13px;" required>
          </div>
          <div class="rep-input-group">
            <label style="font-size:12px; font-weight:700; color:#334155;">Data Final</label>
            <input type="date" name="end" value="{_esc(end)}" style="padding:8px 10px; border:1px solid #cbd5e1; border-radius:8px; font-size:13px;" required>
          </div>
          <div class="rep-input-group">
            <label style="font-size:12px; font-weight:700; color:#334155;">Motorista</label>
            <select name="driver" style="padding:8px 10px; border:1px solid #cbd5e1; border-radius:8px; font-size:13px;">
              {driver_options_html}
            </select>
          </div>
          <div class="rep-input-group" style="justify-content: flex-end;">
            <button type="submit" class="btn primary" style="padding:9px 18px; font-weight:700; font-size:13.5px; border-radius:8px;">🔍 Filtrar Relatório</button>
          </div>
        </div>
      </form>
    </section>
    """

    # KPI summary cards
    kpi_grid = f"""
    <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:14px; margin-bottom:16px;">
      <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <div style="font-size:11.5px; font-weight:700; color:#64748b; text-transform:uppercase;">Viagens Totais</div>
        <div style="font-size:26px; font-weight:800; color:#0f172a; margin:4px 0;">{_fmt_num(kpi['total_trips'])}</div>
        <small style="color:#64748b; font-size:11.5px;">Cargas consolidadas</small>
      </div>

      <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <div style="font-size:11.5px; font-weight:700; color:#64748b; text-transform:uppercase;">Entregas Efetuadas</div>
        <div style="font-size:26px; font-weight:800; color:#166534; margin:4px 0;">{_fmt_num(kpi['total_delivered'])}</div>
        <small style="color:#64748b; font-size:11.5px;">Pedidos entregues no período</small>
      </div>

      <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <div style="font-size:11.5px; font-weight:700; color:#64748b; text-transform:uppercase;">Peso Total Transportado</div>
        <div style="font-size:26px; font-weight:800; color:#0369a1; margin:4px 0;">{_fmt_num(kpi['total_weight'])} <span style="font-size:14px;">kg</span></div>
        <small style="color:#64748b; font-size:11.5px;">Carga declarada/realizada</small>
      </div>

      <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <div style="font-size:11.5px; font-weight:700; color:#64748b; text-transform:uppercase;">KM Total Rodados</div>
        <div style="font-size:26px; font-weight:800; color:#b45309; margin:4px 0;">{_fmt_num(kpi['total_km'])} <span style="font-size:14px;">km</span></div>
        <small style="color:#64748b; font-size:11.5px;">Odômetro confirmado</small>
      </div>

      <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <div style="font-size:11.5px; font-weight:700; color:#64748b; text-transform:uppercase;">Diárias / Pernoites</div>
        <div style="font-size:26px; font-weight:800; color:#7c3aed; margin:4px 0;">{_fmt_num(kpi['total_diarias'])}</div>
        <small style="color:#64748b; font-size:11.5px;">Diárias pagas no período</small>
      </div>

      <div style="background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; padding:16px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <div style="font-size:11.5px; font-weight:700; color:#64748b; text-transform:uppercase;">Total Despesas Operacionais</div>
        <div style="font-size:22px; font-weight:800; color:#dc2626; margin:4px 0;">{_fmt_money(kpi['total_expenses'])}</div>
        <small style="color:#64748b; font-size:11.5px;">Almoço, pernoite, combustível, pedágio</small>
      </div>
    </div>
    """

    # Table 1: Driver Consolidated Performance
    summary_trs = []
    for d in drivers_summary:
        summary_trs.append(f"""
        <tr>
          <td><b>{_esc(d['driver_name'])}</b></td>
          <td style="text-align:center;"><b>{_fmt_num(d['km_total'])} km</b></td>
          <td style="text-align:center;">{_fmt_num(d['diarias_count'])}</td>
          <td style="text-align:right;"><b>{_fmt_num(d['weight_total'])} kg</b></td>
          <td style="text-align:center;"><b>{_fmt_num(d['delivered_count'])}</b></td>
          <td style="text-align:center;"><span class="badge neutral" style="font-weight:700;">{_fmt_num(d['trips_count'])}</span></td>
          <td style="text-align:right;">{_fmt_money(d['lunch_val'])}</td>
          <td style="text-align:right;">{_fmt_money(d['overnight_val'])}</td>
          <td style="text-align:right;">{_fmt_money(d['fuel_val'])}</td>
          <td style="text-align:right;">{_fmt_money(d['toll_val'])}</td>
          <td style="text-align:right;">{_fmt_money(d['other_val'])}</td>
          <td style="text-align:right; font-weight:800; color:#1e3a8a;">{_fmt_money(d['expenses_total'])}</td>
        </tr>
        """)

    summary_table_html = f"""
    <section class="rep-card">
      <div class="rep-section-title" style="margin-bottom:12px; display:flex; justify-content:space-between; align-items:center;">
        <h3 style="margin:0; font-size:15px; color:#0f172a; font-weight:700;">
          <span>🚚</span> Desempenho e Acertos Consolidados por Motorista
        </h3>
        <span class="badge ok">{len(drivers_summary)} motorista(s) com movimento</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr style="background:#f8fafc; color:#475569; font-size:12px; text-transform:uppercase;">
              <th>Motorista</th>
              <th style="text-align:center;">KM Total</th>
              <th style="text-align:center;">Diárias</th>
              <th style="text-align:right;">Peso Total</th>
              <th style="text-align:center;">Entregas</th>
              <th style="text-align:center;">Viagens</th>
              <th style="text-align:right;">Almoço</th>
              <th style="text-align:right;">Pernoite</th>
              <th style="text-align:right;">Combustível</th>
              <th style="text-align:right;">Pedágio</th>
              <th style="text-align:right;">Outros</th>
              <th style="text-align:right;">Total Despesas</th>
            </tr>
          </thead>
          <tbody>
            {''.join(summary_trs) if summary_trs else "<tr><td colspan='12' class='empty'>Nenhuma viagem encontrada para os filtros selecionados.</td></tr>"}
          </tbody>
        </table>
      </div>
    </section>
    """

    # Table 2: Detailed Trips
    trip_trs = []
    for t in trips:
        v_str = f"{t['vehicle_name'] or ''} ({t['plate'] or ''})" if t['plate'] else (t['vehicle_name'] or '—')
        pdf_btn = f"<a class='btn small ghost' href='/routes/{t['id']}/trip-sheet-pdf' target='_blank' style='font-size:11px; padding:3px 7px; color:#166534; border-color:#166534; font-weight:700;'>📄 Ficha PDF</a>"
        
        # Format date as DD/MM/YYYY
        date_formatted = t['date']
        if date_formatted and len(date_formatted) >= 10:
            parts = date_formatted[:10].split("-")
            if len(parts) == 3:
                date_formatted = f"{parts[2]}/{parts[1]}/{parts[0]}"

        exp_desc_items = []
        if t['lunch_val'] > 0: exp_desc_items.append(f"Almoço: {_fmt_money(t['lunch_val'])}")
        if t['overnight_val'] > 0: exp_desc_items.append(f"Pernoite ({t['overnight_qty']:.0f}): {_fmt_money(t['overnight_val'])}")
        if t['fuel_val'] > 0: exp_desc_items.append(f"Comb: {_fmt_money(t['fuel_val'])}")
        if t['toll_val'] > 0: exp_desc_items.append(f"Pedágio: {_fmt_money(t['toll_val'])}")
        if t['other_val'] > 0: exp_desc_items.append(f"Outros: {_fmt_money(t['other_val'])}")
        exp_summary_str = " · ".join(exp_desc_items) if exp_desc_items else "Sem despesas"

        trip_trs.append(f"""
        <tr>
          <td><b>{_esc(date_formatted)}</b></td>
          <td><b>{_esc(t['route_name'])}</b><br><small class='muted'>Carga #{t['id']}</small></td>
          <td>{_esc(t['driver_name'])}</td>
          <td>{_esc(v_str)}</td>
          <td>{_esc(t['destination'] or 'T.O. / Padrão')}</td>
          <td style="text-align:center;">{_fmt_num(t['km_start']) if t['km_start'] else '—'}</td>
          <td style="text-align:center;">{_fmt_num(t['km_end']) if t['km_end'] else '—'}</td>
          <td style="text-align:center;"><b>{_fmt_num(t['km_total'])} km</b></td>
          <td style="text-align:center;"><b>{t['delivered_count']}</b> / {t['orders_count']}</td>
          <td style="text-align:right;"><b>{_fmt_num(t['weight_kg'])} kg</b></td>
          <td>
            <b>{_fmt_money(t['total_exp'])}</b>
            <br><small style="color:#64748b; font-size:11px;">{_esc(exp_summary_str)}</small>
          </td>
          <td><span class="badge {'ok' if t['status']=='Acertada' else 'neutral'}">{_esc(t['status'])}</span></td>
          <td>{pdf_btn}</td>
        </tr>
        """)

    detailed_trips_table_html = f"""
    <section class="rep-card">
      <div class="rep-section-title" style="margin-bottom:12px; display:flex; justify-content:space-between; align-items:center;">
        <h3 style="margin:0; font-size:15px; color:#0f172a; font-weight:700;">
          <span>📋</span> Detalhamento Analítico de Viagens e Despesas
        </h3>
        <span class="badge neutral">{len(trips)} viagem(ns) no período</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr style="background:#f8fafc; color:#475569; font-size:12px; text-transform:uppercase;">
              <th>Data</th>
              <th>Carga</th>
              <th>Motorista</th>
              <th>Veículo</th>
              <th>Destino</th>
              <th style="text-align:center;">KM Inic.</th>
              <th style="text-align:center;">KM Final</th>
              <th style="text-align:center;">KM Total</th>
              <th style="text-align:center;">Entregas</th>
              <th style="text-align:right;">Peso</th>
              <th>Despesas Detalhadas</th>
              <th>Status</th>
              <th>Ação</th>
            </tr>
          </thead>
          <tbody>
            {''.join(trip_trs) if trip_trs else "<tr><td colspan='13' class='empty'>Nenhuma viagem detalhada no período.</td></tr>"}
          </tbody>
        </table>
      </div>
    </section>
    """

    return f"""
    <div class="rep-shell">
      {top_bar}
      {filter_card}
      {kpi_grid}
      {summary_table_html}
      {detailed_trips_table_html}
    </div>
    """


def export_driver_report_csv(handler: Any, user: Any) -> None:
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(handler.path).query)
    today_iso = date.today().isoformat()
    start_default = date.today().replace(day=1).isoformat()
    start = (qs.get("start", [start_default])[0] or start_default).strip()
    end = (qs.get("end", [today_iso])[0] or today_iso).strip()
    driver_param = (qs.get("driver", [""])[0] or "").strip()

    with handler.conn() as db:
        data = get_driver_report_data(db, start, end, driver_param)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)

    # Header
    writer.writerow(["RELATÓRIO DE MOTORISTAS - LOGÍSTICA CASA DO CAMPO"])
    writer.writerow([f"Período: {start} até {end} | Filtro Motorista: {driver_param or 'Todos'}"])
    writer.writerow([])

    # Table 1: Consolidated
    writer.writerow(["CONSOLIDAÇÃO POR MOTORISTA"])
    writer.writerow(["Motorista", "KM Total Rodados", "Diárias Totais", "Peso Total (kg)", "Entregas Totais", "Viagens Totais", "Almoço (R$)", "Pernoite (R$)", "Combustível (R$)", "Pedágio (R$)", "Outros (R$)", "Total Despesas (R$)"])
    for d in data["drivers_summary"]:
        writer.writerow([
            d["driver_name"],
            f"{d['km_total']:.0f}",
            f"{d['diarias_count']:.0f}",
            f"{d['weight_total']:.2f}".replace(".", ","),
            d["delivered_count"],
            d["trips_count"],
            f"{d['lunch_val']:.2f}".replace(".", ","),
            f"{d['overnight_val']:.2f}".replace(".", ","),
            f"{d['fuel_val']:.2f}".replace(".", ","),
            f"{d['toll_val']:.2f}".replace(".", ","),
            f"{d['other_val']:.2f}".replace(".", ","),
            f"{d['expenses_total']:.2f}".replace(".", ",")
        ])

    writer.writerow([])
    writer.writerow(["DETALHAMENTO ANALÍTICO DE VIAGENS"])
    writer.writerow(["Data", "Carga", "Motorista", "Veículo", "Destino", "Ajudante", "KM Inicial", "KM Final", "KM Total Rodado", "Entregas Realizadas", "Pedidos Totais", "Peso Carga (kg)", "Almoço (R$)", "Pernoite (R$)", "Combustível (R$)", "Pedágio (R$)", "Outros (R$)", "Total Despesas (R$)", "Status"])
    for t in data["trips"]:
        v_str = f"{t['vehicle_name'] or ''} ({t['plate'] or ''})" if t['plate'] else (t['vehicle_name'] or '')
        writer.writerow([
            t["date"],
            t["route_name"],
            t["driver_name"],
            v_str,
            t["destination"] or "",
            t["helper_name"] or "",
            f"{t['km_start']:.0f}" if t["km_start"] else "",
            f"{t['km_end']:.0f}" if t["km_end"] else "",
            f"{t['km_total']:.0f}" if t["km_total"] else "",
            t["delivered_count"],
            t["orders_count"],
            f"{t['weight_kg']:.2f}".replace(".", ","),
            f"{t['lunch_val']:.2f}".replace(".", ","),
            f"{t['overnight_val']:.2f}".replace(".", ","),
            f"{t['fuel_val']:.2f}".replace(".", ","),
            f"{t['toll_val']:.2f}".replace(".", ","),
            f"{t['other_val']:.2f}".replace(".", ","),
            f"{t['total_exp']:.2f}".replace(".", ","),
            t["status"]
        ])

    csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
    filename = f"Relatorio_Motoristas_{start}_{end}.csv"
    handler.send_response(200)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition", f"attachment; filename=\"{filename}\"")
    handler.send_header("Content-Length", str(len(csv_bytes)))
    handler.end_headers()
    handler.wfile.write(csv_bytes)


def export_driver_report_pdf(handler: Any, user: Any) -> None:
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(handler.path).query)
    today_iso = date.today().isoformat()
    start_default = date.today().replace(day=1).isoformat()
    start = (qs.get("start", [start_default])[0] or start_default).strip()
    end = (qs.get("end", [today_iso])[0] or today_iso).strip()
    driver_param = (qs.get("driver", [""])[0] or "").strip()

    with handler.conn() as db:
        data = get_driver_report_data(db, start, end, driver_param)

    kpi = data["kpi"]
    drivers_summary = data["drivers_summary"]
    trips = data["trips"]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=25, rightMargin=25, topMargin=25, bottomMargin=25)
    styles = getSampleStyleSheet()

    primary_color = colors.HexColor("#166534")
    accent_color = colors.HexColor("#0f172a")
    border_color = colors.HexColor("#cbd5e1")
    light_bg = colors.HexColor("#f8fafc")

    title_style = ParagraphStyle('RepTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=16, leading=19, textColor=primary_color)
    subtitle_style = ParagraphStyle('RepSub', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=12, textColor=colors.HexColor("#475569"))
    th_style = ParagraphStyle('RepTH', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7.5, leading=9.5, textColor=colors.white, alignment=1)
    th_left = ParagraphStyle('RepTHLeft', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7.5, leading=9.5, textColor=colors.white, alignment=0)
    td_style = ParagraphStyle('RepTD', parent=styles['Normal'], fontName='Helvetica', fontSize=7, leading=9, textColor=colors.HexColor("#1e293b"))
    td_bold = ParagraphStyle('RepTDBold', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7, leading=9, textColor=colors.HexColor("#1e293b"))
    td_center = ParagraphStyle('RepTDCenter', parent=styles['Normal'], fontName='Helvetica', fontSize=7, leading=9, textColor=colors.HexColor("#1e293b"), alignment=1)
    td_right = ParagraphStyle('RepTDRight', parent=styles['Normal'], fontName='Helvetica', fontSize=7, leading=9, textColor=colors.HexColor("#1e293b"), alignment=2)
    sec_title = ParagraphStyle('RepSec', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10.5, leading=13, textColor=primary_color)

    elements = []

    # Header Table
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    driver_sub_str = f"Motorista: {driver_param}" if driver_param else "Todos os Motoristas"
    header_data = [
        [
            Paragraph("<b>LOGÍSTICA CASA DO CAMPO</b><br/><font size=12>Relatório Consolidado de Motoristas e Despesas</font>", title_style),
            Paragraph(f"<b>Período:</b> {start} até {end}<br/><b>Filtro:</b> {driver_sub_str}<br/><b>Emissão:</b> {now_str}", subtitle_style)
        ]
    ]
    t_hdr = Table(header_data, colWidths=[450, 300])
    t_hdr.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
    ]))
    elements.append(t_hdr)
    elements.append(Spacer(1, 6))

    # KPI summary box
    kpi_data = [
        [
            Paragraph(f"<b>VIAGENS TOTAIS</b><br/><font size=11><b>{kpi['total_trips']}</b></font>", td_center),
            Paragraph(f"<b>ENTREGAS</b><br/><font size=11><b>{kpi['total_delivered']}</b></font>", td_center),
            Paragraph(f"<b>PESO TOTAL</b><br/><font size=11><b>{kpi['total_weight']:,.0f} kg</b></font>", td_center),
            Paragraph(f"<b>KM TOTAL</b><br/><font size=11><b>{kpi['total_km']:,.0f} km</b></font>", td_center),
            Paragraph(f"<b>DIÁRIAS</b><br/><font size=11><b>{kpi['total_diarias']:.0f}</b></font>", td_center),
            Paragraph(f"<b>DESPESAS TOTAIS</b><br/><font size=11><b>R$ {kpi['total_expenses']:,.2f}</b></font>", td_center),
        ]
    ]
    t_kpi = Table(kpi_data, colWidths=[125, 125, 125, 125, 125, 125])
    t_kpi.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), light_bg),
        ('BOX', (0,0), (-1,-1), 1, primary_color),
        ('INNERGRID', (0,0), (-1,-1), 0.5, border_color),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    elements.append(t_kpi)
    elements.append(Spacer(1, 10))

    # Section 1: Consolidated Table
    elements.append(Paragraph("<b>1. CONSOLIDAÇÃO DE DESEMPENHO E ACERTOS POR MOTORISTA</b>", sec_title))
    elements.append(Spacer(1, 4))

    cons_rows = [
        [
            Paragraph("Motorista", th_left),
            Paragraph("KM Total", th_style),
            Paragraph("Diárias", th_style),
            Paragraph("Peso Total", th_style),
            Paragraph("Entregas", th_style),
            Paragraph("Viagens", th_style),
            Paragraph("Almoço", th_style),
            Paragraph("Pernoite", th_style),
            Paragraph("Combust.", th_style),
            Paragraph("Pedágio", th_style),
            Paragraph("Outros", th_style),
            Paragraph("Total Desp.", th_style),
        ]
    ]

    for d in drivers_summary:
        cons_rows.append([
            Paragraph(f"<b>{d['driver_name']}</b>", td_style),
            Paragraph(f"{d['km_total']:,.0f} km", td_center),
            Paragraph(f"{d['diarias_count']:.0f}", td_center),
            Paragraph(f"{d['weight_total']:,.0f} kg", td_right),
            Paragraph(f"{d['delivered_count']}", td_center),
            Paragraph(f"<b>{d['trips_count']}</b>", td_center),
            Paragraph(f"R$ {d['lunch_val']:,.2f}", td_right),
            Paragraph(f"R$ {d['overnight_val']:,.2f}", td_right),
            Paragraph(f"R$ {d['fuel_val']:,.2f}", td_right),
            Paragraph(f"R$ {d['toll_val']:,.2f}", td_right),
            Paragraph(f"R$ {d['other_val']:,.2f}", td_right),
            Paragraph(f"<b>R$ {d['expenses_total']:,.2f}</b>", td_right),
        ])

    t_cons = Table(cons_rows, colWidths=[110, 55, 45, 65, 45, 45, 60, 60, 65, 55, 55, 90], repeatRows=1)
    t_cons.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), primary_color),
        ('BOX', (0,0), (-1,-1), 1, primary_color),
        ('INNERGRID', (0,0), (-1,-1), 0.3, border_color),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))
    elements.append(t_cons)
    elements.append(Spacer(1, 10))

    # Section 2: Detailed Trips Table
    elements.append(Paragraph("<b>2. DETALHAMENTO ANALÍTICO DE VIAGENS E DESPESAS</b>", sec_title))
    elements.append(Spacer(1, 4))

    trip_table_rows = [
        [
            Paragraph("Data", th_style),
            Paragraph("Carga", th_left),
            Paragraph("Motorista", th_left),
            Paragraph("Veículo", th_left),
            Paragraph("Destino", th_left),
            Paragraph("KM Inic.", th_style),
            Paragraph("KM Fim", th_style),
            Paragraph("KM Tot.", th_style),
            Paragraph("Entr.", th_style),
            Paragraph("Peso", th_style),
            Paragraph("Despesas", th_style),
            Paragraph("Status", th_style),
        ]
    ]

    for t in trips:
        v_str = f"{t['vehicle_name'] or ''} ({t['plate'] or ''})" if t['plate'] else (t['vehicle_name'] or '')
        date_f = t['date']
        if date_f and len(date_f) >= 10:
            parts = date_f[:10].split("-")
            if len(parts) == 3:
                date_f = f"{parts[2]}/{parts[1]}"

        trip_table_rows.append([
            Paragraph(date_f, td_center),
            Paragraph(f"<b>{t['route_name']}</b>", td_style),
            Paragraph(str(t['driver_name'] or ''), td_style),
            Paragraph(v_str, td_style),
            Paragraph(str(t['destination'] or 'T.O.'), td_style),
            Paragraph(f"{t['km_start']:,.0f}" if t['km_start'] else "—", td_center),
            Paragraph(f"{t['km_end']:,.0f}" if t['km_end'] else "—", td_center),
            Paragraph(f"<b>{t['km_total']:,.0f}</b>" if t['km_total'] else "—", td_center),
            Paragraph(f"{t['delivered_count']}/{t['orders_count']}", td_center),
            Paragraph(f"{t['weight_kg']:,.0f} kg", td_right),
            Paragraph(f"R$ {t['total_exp']:,.2f}", td_right),
            Paragraph(f"<font color='#166534'><b>{t['status']}</b></font>", td_center),
        ])

    t_trips = Table(trip_table_rows, colWidths=[40, 95, 80, 80, 75, 45, 45, 45, 45, 65, 75, 60], repeatRows=1)
    t_trips.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), accent_color),
        ('BOX', (0,0), (-1,-1), 1, accent_color),
        ('INNERGRID', (0,0), (-1,-1), 0.3, border_color),
        ('TOPPADDING', (0,0), (-1,-1), 2.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
    ]))
    elements.append(t_trips)

    doc.build(elements)
    pdf_bytes = buf.getvalue()

    # Also save a copy on Desktop
    try:
        desktop_dir = r"C:\Users\Administrator\Desktop\Relatório Logística Casa do Campo"
        os.makedirs(desktop_dir, exist_ok=True)
        desktop_file = os.path.join(desktop_dir, f"Relatorio_Motoristas_{start}_{end}.pdf")
        with open(desktop_file, "wb") as f:
            f.write(pdf_bytes)
    except Exception:
        pass

    filename = f"Relatorio_Motoristas_{start}_{end}.pdf"
    handler.send_response(200)
    handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Disposition", f"inline; filename=\"{filename}\"")
    handler.send_header("Content-Length", str(len(pdf_bytes)))
    handler.end_headers()
    handler.wfile.write(pdf_bytes)
