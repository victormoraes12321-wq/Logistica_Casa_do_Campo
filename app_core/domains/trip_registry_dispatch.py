# -*- coding: utf-8 -*-
"""
Módulo de Domínio: Registro de Viagens (Digitalização da Folha Física).
Permite lançamento completo de viagens avulsas ou pendentes com cabeçalho de telemetria,
itens de entrega, despesas da viagem e validação interativa com modal de conferência
contra o banco de dados local SQLite e ERP Oracle em tempo real.
"""
from __future__ import annotations

import json
import re
import html
import io
import os
import sqlite3
import math
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import parse_qs, urlparse, quote
from app_core.services import trip_order_metadata
from app_core.services import trip_audit

# ReportLab para geração do Espelho da Viagem em PDF
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, Image
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _esc(val: any) -> str:
    return html.escape(str(val or ""))


def handle_get(handler, path: str, user, layout_func=None) -> bool:
    if path == "/registro-viagens":
        if not handler.require_perm(user, "create_routes", "Sem permissão para registrar viagens."):
            return True
        render_registro_viagens_page(handler, user, layout_func=layout_func)
        return True

    if path == "/api/trips/search-item":
        if not handler.require_perm(user, "view_orders", "Sem permissão."):
            return True
        api_search_item(handler, user)
        return True

    if path == "/api/trips/open-orders":
        if not handler.require_perm(user, "view_orders", "Sem permissão."):
            return True
        api_open_orders(handler, user)
        return True

    if path.startswith("/routes/") and path.endswith("/trip-sheet-pdf"):
        if not handler.require_perm(user, "view_routes", "Sem permissão."):
            return True
        route_id = handler.path_int(path, 2)
        generate_trip_sheet_pdf(handler, user, route_id)
        return True

    if path.startswith("/routes/") and "/odometer-photo/" in path:
        parts = path.split("/")
        if len(parts) >= 5 and parts[2].isdigit():
            route_id = int(parts[2])
            photo_type = parts[4].lower()
            return serve_odometer_photo(handler, user, route_id, photo_type)

    return False


def handle_post(handler, path: str, user) -> bool:
    if path == "/api/trips/verify":
        if not handler.require_perm(user, "create_routes", "Sem permissão para registrar viagens."):
            return True
        api_verify_trip(handler, user)
        return True

    if path == "/api/trips/save":
        if not handler.require_perm(user, "create_routes", "Sem permissão para registrar viagens."):
            return True
        api_save_trip(handler, user)
        return True

    return False


REGISTRO_VIAGENS_HTML_TEMPLATE = """
<style>
#tripForm .grid4, #tripForm .grid3 {
  display: grid;
  gap: 12px;
}
#tripForm .grid4 { grid-template-columns: repeat(4, minmax(0, 1fr)); }
#tripForm .grid3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
#tripForm .field { min-width: 0; }
#tripForm .field label { display: block; margin-bottom: 4px; }
#tripForm .field input, #tripForm .field select { min-width: 0; }
#tripForm > .panel > div:first-child { flex-wrap: wrap; gap: 12px; }
#tripForm > .panel > div:first-child > div:last-child { flex-wrap: wrap; }
#tripForm .table { min-width: 1250px; }
@media (max-width: 1100px) {
  #tripForm .grid4 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  #tripForm .grid3 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 700px) {
  #tripForm .grid4, #tripForm .grid3 { grid-template-columns: minmax(0, 1fr); }
  #tripForm .field { grid-column: auto !important; }
  #tripForm > div:last-child, #tripForm > div:last-child > div { flex-wrap: wrap; gap: 10px; }
}
.rv-ribbon {
  display: flex;
  gap: 16px;
  align-items: center;
  background: var(--surface, #ffffff);
  border: 1px solid var(--border, #d7ddd8);
  border-radius: var(--radius-sm, 8px);
  padding: 12px 18px;
  margin-bottom: 16px;
  box-shadow: var(--shadow-sm, 0 1px 2px rgba(0,0,0,0.05));
  flex-wrap: wrap;
}
.rv-ribbon-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 140px;
}
.rv-ribbon-item small {
  font-size: 11px;
  text-transform: uppercase;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--muted, #67756d);
}
.rv-ribbon-item strong {
  font-size: 20px;
  font-weight: 800;
  color: var(--green-strong, #185739);
}
.rv-ribbon-divider {
  width: 1px;
  height: 36px;
  background: var(--border, #d7ddd8);
}
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border-radius: 12px;
  font-size: 11.5px;
  font-weight: 700;
}
.status-ok { background: #dcfce7; color: #15803d; border: 1px solid #86efac; }
.status-warn { background: #fef3c7; color: #b45309; border: 1px solid #fde68a; }
.status-danger { background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; }

/* MODAL DE CONFERÊNCIA E SELEÇÃO */
#verifyModal, #openOrdersModal {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.65);
  backdrop-filter: blur(4px);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 99999;
  padding: 16px;
}
.modal-box {
  background: #ffffff;
  border-radius: 14px;
  max-width: 920px;
  width: 100%;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  box-shadow: 0 25px 50px rgba(0,0,0,0.25);
  border: 1px solid var(--border, #cbd5e1);
}
.modal-head {
  background: linear-gradient(135deg, #0f2e1b, var(--green-strong, #185739));
  color: #ffffff;
  padding: 16px 20px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.modal-head h3 { margin: 0; font-size: 17px; font-weight: 800; }
.modal-body {
  padding: 20px;
  overflow-y: auto;
  flex: 1;
}
.modal-foot {
  background: var(--surface-soft, #f8faf8);
  border-top: 1px solid var(--border, #d7ddd8);
  padding: 14px 20px;
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>

<!-- KPI / METRICS RIBBON -->
<div class="rv-ribbon">
  <div class="rv-ribbon-item">
    <small>Pedidos / NFs</small>
    <strong id="displayTotalOrders">0</strong>
  </div>
  <div class="rv-ribbon-divider"></div>
  <div class="rv-ribbon-item">
    <small>Peso Total Carregado</small>
    <strong id="displayTotalWeight">0 kg</strong>
  </div>
  <div class="rv-ribbon-divider"></div>
  <div class="rv-ribbon-item">
    <small>Despesas Totais Lançadas</small>
    <strong id="displayTotalExpenses">R$ 0,00</strong>
  </div>
  <div class="rv-ribbon-divider"></div>
  <div class="rv-ribbon-item">
    <small>KM Total Estimado</small>
    <strong id="displayTotalKm">0 km</strong>
  </div>
</div>

<form id="tripForm" data-realtime-refresh="off" onsubmit="event.preventDefault(); openVerifyModal();">
  <!-- PAINEL 1: IDENTIFICAÇÃO E TELEMETRIA -->
  <div class="panel">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; border-bottom:1px solid var(--border); padding-bottom:8px;">
      <h2 style="margin:0; font-size:16px;">🚚 Identificação do Veículo & Telemetria</h2>
      <span class="badge" style="background:#f0fdf4; color:#166534; border:1px solid #86efac; font-size:11.5px; font-weight:700;">Prancheta de Campo</span>
    </div>

    <div class="grid4">
      <div class="field">
        <label>Data Saída *</label>
        <input type="date" id="dataSaida" name="data_saida" value="__TODAY_ISO__" required>
      </div>
      <div class="field">
        <label>Hora Saída</label>
        <input type="time" id="horaSaida" name="hora_saida" value="__NOW_TIME__">
      </div>
      <div class="field">
        <label>KM Saída</label>
        <input type="number" step="any" id="kmStart" name="km_start" placeholder="Ex: 403200" oninput="calcKmTotal()">
      </div>
      <div class="field">
        <label>Destino Principal</label>
        <input type="text" id="destination" name="destination" placeholder="Ex: Região Norte / T.O.">
      </div>
    </div>

    <div class="grid4" style="margin-top:10px;">
      <div class="field">
        <label>Data Chegada</label>
        <input type="date" id="dataChegada" name="data_chegada" value="__TODAY_ISO__">
      </div>
      <div class="field">
        <label>Hora Chegada</label>
        <input type="time" id="horaChegada" name="hora_chegada" value="__NOW_TIME__">
      </div>
      <div class="field">
        <label>KM Chegada</label>
        <input type="number" step="any" id="kmEnd" name="km_end" placeholder="Ex: 403850" oninput="calcKmTotal()">
      </div>
      <div class="field">
        <label>KM Total Rodado</label>
        <input type="number" id="kmTotal" name="km_total" placeholder="0" readonly style="background:var(--surface-soft); font-weight:700; color:var(--green-strong);">
      </div>
    </div>

    <div class="grid3" style="margin-top:10px;">
      <div class="field">
        <label>Motorista *</label>
        <select id="driverId" name="driver_id" required>
          <option value="">Selecione o Motorista...</option>
          __DRIVER_OPTS__
        </select>
      </div>
      <div class="field">
        <label>Veículo *</label>
        <select id="vehicleId" name="vehicle_id" required>
          <option value="">Selecione o Veículo...</option>
          __VEHICLE_OPTS__
        </select>
      </div>
      <div class="field">
        <label>Ajudante de Carga</label>
        <input type="text" id="helperName" name="helper_name" placeholder="Nome do ajudante (opcional)">
      </div>
    </div>
  </div>

  <!-- PAINEL 2: PEDIDOS, NFS E ENTREGAS -->
  <div class="panel" style="margin-top:14px;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; border-bottom:1px solid var(--border); padding-bottom:8px;">
      <div>
        <h2 style="margin:0; font-size:16px;">📦 Pedidos e Entregas da Viagem (NFs)</h2>
        <small style="color:var(--muted)">Informe o pedido e a nota fiscal nos campos separados. Ao digitar um deles, o sistema tenta localizar os dados no ERP e na lista de Pedidos.</small>
      </div>
      <div style="display:flex; gap:8px; align-items:center;">
        <button type="button" class="btn btn-secondary small" onclick="openOpenOrdersModal()" style="font-weight:750; background:#f0fdf4; color:#166534; border:1px solid #86efac; cursor:pointer;">
          📋 Puxar Pedidos em Aberto da Lista
        </button>
        <button type="button" class="btn btn-secondary small" onclick="addOrderRow()" style="font-weight:700; cursor:pointer;">
          + Adicionar Linha
        </button>
      </div>
    </div>

    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th style="width:110px;">Nº Pedido *</th>
            <th style="width:110px;">Nota Fiscal</th>
            <th>Cliente</th>
            <th style="width:130px;">Forma Pgto</th>
            <th style="width:140px;">Data Entrega</th>
            <th style="width:140px;">KM / Hr Chegada</th>
            <th style="width:110px;">Peso (kg) *</th>
            <th>Produtos / Observações</th>
            <th style="width:40px; text-align:center;">✕</th>
          </tr>
        </thead>
        <tbody id="ordersTableBody">
          <tr id="row_1">
            <td><input type="text" name="order_number" placeholder="Nº do pedido" onblur="lookupItem(this, 1)"><input type="hidden" name="item_number" value=""></td>
            <td><input type="text" name="invoice_number" placeholder="Nº da NF" onblur="lookupItem(this, 1)"><div id="badge_baixa_1"></div></td>
            <td><input type="text" name="client_name" id="client_1" placeholder="Nome do cliente"></td>
            <td><select name="payment_type"><option value="Boleto">Boleto</option><option value="Promissória">Promissória</option><option value="À Vista">À Vista</option><option value="Cartão">Cartão</option><option value="Outro">Outro</option></select></td>
            <td><input type="date" name="delivery_date" value="__TODAY_ISO__"></td>
            <td><input type="text" name="km_hr_end" placeholder="Ex: 403751 / 15:50"></td>
            <td><input type="number" step="0.1" name="weight_kg" id="weight_1" placeholder="KG" required oninput="calcTotals()"></td>
            <td><input type="text" name="notes" id="notes_1" placeholder="Produtos / observações"></td>
            <td style="text-align:center;"><button type="button" onclick="removeRow(1)" aria-label="Remover pedido" style="background:none; border:none; color:#ef4444; font-weight:bold; cursor:pointer; font-size:1.1rem;">✕</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- PAINEL 3: DESPESAS DA VIAGEM -->
  <div class="panel" style="margin-top:14px;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; border-bottom:1px solid var(--border); padding-bottom:8px;">
      <h2 style="margin:0; font-size:16px;">💰 Despesas de Viagem (Acerto Financeiro)</h2>
      <span id="displayTotalExpensesBadge" class="badge" style="background:#f0fdf4; color:#166534; border:1px solid #86efac; font-size:12px; font-weight:800;">
        Total: R$ 0,00
      </span>
    </div>

    <div class="grid3">
      <div class="field">
        <label>Almoço</label>
        <div style="display:flex; gap:6px; align-items:center;">
          <input type="number" id="expLunchQty" placeholder="Qtd" min="0" step="1" aria-label="Quantidade de almoços" style="width:65px;" oninput="calcExpensesTotal()">
          <input type="text" inputmode="decimal" id="expLunchVal" placeholder="R$ Unit" aria-label="Valor unitário do almoço" oninput="calcExpensesTotal()" onblur="formatTripMoney(this)">
          <span id="expLunchTotal" style="min-width:65px; font-weight:700; font-size:12px; text-align:right;">R$ 0,00</span>
        </div>
      </div>
      <div class="field">
        <label>Diárias / Pernoite</label>
        <div style="display:flex; gap:6px; align-items:center;">
          <input type="number" id="expOvernightQty" placeholder="Qtd" min="0" step="1" aria-label="Quantidade de diárias" style="width:65px;" oninput="calcExpensesTotal()">
          <input type="text" inputmode="decimal" id="expOvernightVal" placeholder="R$ Unit" aria-label="Valor unitário da diária" oninput="calcExpensesTotal()" onblur="formatTripMoney(this)">
          <span id="expOvernightTotal" style="min-width:65px; font-weight:700; font-size:12px; text-align:right;">R$ 0,00</span>
        </div>
      </div>
      <div class="field">
        <label>Combustível</label>
        <div style="display:flex; gap:6px; align-items:center;">
          <input type="number" step="0.1" id="expFuelLitres" placeholder="Litros" min="0" style="width:75px;" oninput="calcExpensesTotal()">
          <input type="text" inputmode="decimal" id="expFuelVal" placeholder="R$ Total" aria-label="Valor total do combustível" oninput="calcExpensesTotal()" onblur="formatTripMoney(this)">
          <span id="expFuelTotal" style="min-width:65px; font-weight:700; font-size:12px; text-align:right;">R$ 0,00</span>
        </div>
      </div>
      <div class="field">
        <label>Pedágio (R$ Total)</label>
        <input type="text" inputmode="decimal" id="expTollVal" placeholder="R$ 0,00" oninput="calcExpensesTotal()" onblur="formatTripMoney(this)">
      </div>
      <div class="field" style="grid-column: span 2;">
        <label>Outros Gastos (Descrição e Valor Total)</label>
        <div style="display:flex; gap:6px;">
          <input type="text" id="expOtherDesc" placeholder="Descrição da despesa (ex: Borracharia, Manutenção rápida)">
          <input type="text" inputmode="decimal" id="expOtherVal" placeholder="R$ Total" style="width:130px;" oninput="calcExpensesTotal()" onblur="formatTripMoney(this)">
        </div>
      </div>
    </div>
  </div>

  <!-- BARRA DE AÇÕES -->
  <div style="display:flex; justify-content:space-between; align-items:center; margin-top:16px; margin-bottom:24px;">
    <a href="/routes" class="btn ghost">← Voltar para Cargas</a>
    <div style="display:flex; gap:10px;">
      <button type="button" class="btn ghost" onclick="window.print()">🖨️ Imprimir Folha</button>
      <button type="submit" class="btn btn-primary" style="font-size:14.5px; font-weight:750; padding:10px 24px;">
        🔍 Verificar e Conciliar com ERP / Gravar
      </button>
    </div>
  </div>
</form>

<!-- MODAL DE CONFERÊNCIA EM TEMPO REAL -->
<div id="verifyModal">
  <div class="modal-box">
    <div class="modal-head">
      <h3>🔍 Conferência em Tempo Real com ERP / Banco de Dados</h3>
      <button type="button" onclick="closeVerifyModal()" style="background:none; border:none; color:#fff; font-size:1.4rem; cursor:pointer;">&times;</button>
    </div>
    <div class="modal-body">
      <div id="verifyLoading" style="text-align:center; padding:30px;">
        <div style="font-size:1.5rem; margin-bottom:10px;">⏳</div>
        <b>Consultando pedidos no ERP Oracle e banco de dados...</b>
      </div>
      <div id="verifyContent" style="display:none;">
        <div id="verifySummaryBox" style="padding:12px 16px; border-radius:8px; margin-bottom:14px; font-size:0.9rem;"></div>
        
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>Item Digitado</th>
                <th>Doc ERP / Banco</th>
                <th>Cliente Encontrado</th>
                <th style="text-align:right;">Peso Digitado</th>
                <th style="text-align:right;">Peso ERP</th>
                <th style="text-align:center;">Resultado</th>
              </tr>
            </thead>
            <tbody id="verifyTableBody"></tbody>
          </table>
        </div>

        <div id="verifyWarningNote" style="margin-top:12px; font-size:12px; color:var(--muted);"></div>
      </div>
    </div>
    <div class="modal-foot">
      <button type="button" class="btn btn-secondary" onclick="closeVerifyModal()">Corrigir Dados na Tela</button>
      <button type="button" id="btnConfirmSave" class="btn btn-primary" style="font-weight:700;" onclick="executeSaveTrip()">
        Confirmar e Gravar no Sistema
      </button>
    </div>
  </div>
</div>

<!-- MODAL DE SELEÇÃO DE PEDIDOS EM ABERTO -->
<div id="openOrdersModal">
  <div class="modal-box" style="max-width:980px;">
    <div class="modal-head">
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="font-size:1.2rem;">📋</span>
        <h3 style="margin:0;">Pedidos em Aberto na Lista de Pedidos</h3>
      </div>
      <button type="button" onclick="closeOpenOrdersModal()" style="background:none; border:none; color:#fff; font-size:1.4rem; cursor:pointer;">&times;</button>
    </div>
    <div class="modal-body">
      <div style="display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:12px; flex-wrap:wrap;">
        <input type="text" id="openOrdersSearch" placeholder="🔍 Filtrar por Pedido, NF, Cliente ou Cidade..." oninput="filterOpenOrdersTable()" style="flex:1; min-width:240px; padding:8px 12px; border-radius:6px; border:1px solid var(--border);">
        <div style="font-size:12px; color:var(--muted);">
          Ao finalizar a viagem, os pedidos selecionados receberão <b>baixa automática</b> ('Acertado').
        </div>
      </div>
      <div id="openOrdersLoading" style="text-align:center; padding:30px;">
        <div style="font-size:1.5rem; margin-bottom:10px;">⏳</div>
        <b>Carregando pedidos em andamento...</b>
      </div>
      <div id="openOrdersTableContainer" style="display:none; max-height:450px; overflow-y:auto;" class="table-wrap">
        <table class="table" style="font-size:12.5px;">
          <thead>
            <tr>
              <th style="width:36px; text-align:center;">
                <input type="checkbox" id="selectAllOpenOrders" onchange="toggleSelectAllOpenOrders(this)">
              </th>
              <th style="width:130px;">Pedido / NF</th>
              <th>Cliente</th>
              <th>Cidade</th>
              <th style="width:115px; text-align:center;">Status Atual</th>
              <th style="width:100px; text-align:right;">Peso</th>
              <th style="width:110px; text-align:right;">Valor</th>
            </tr>
          </thead>
          <tbody id="openOrdersTableBody"></tbody>
        </table>
      </div>
    </div>
    <div class="modal-foot">
      <span id="openOrdersSelectedCount" style="margin-right:auto; font-weight:700; color:var(--green-strong); font-size:13px;">0 selecionado(s)</span>
      <button type="button" class="btn btn-secondary" onclick="closeOpenOrdersModal()">Cancelar</button>
      <button type="button" class="btn btn-primary" onclick="addSelectedOpenOrders()" style="font-weight:700; background:#166534; border-color:#166534;">
        ➕ Importar Selecionados para a Viagem
      </button>
    </div>
  </div>
</div>

<script>
let rowCounter = 1;
let verifiedTripData = null;
let verifiedTripPayload = null;

function calcKmTotal() {
    const start = parseFloat(document.getElementById('kmStart').value) || 0;
    const end = parseFloat(document.getElementById('kmEnd').value) || 0;
    const total = end > start ? (end - start) : 0;
    document.getElementById('kmTotal').value = total;
}

const tripMoneyFormatter = new Intl.NumberFormat('pt-BR', {style: 'currency', currency: 'BRL'});

function parseTripMoney(value) {
    let raw = String(value ?? '').replaceAll('R$', '').replaceAll(' ', '').replaceAll(' ', '').trim();
    if (!raw) return 0;
    if (raw.includes(',')) raw = raw.replaceAll('.', '').replace(',', '.');
    else if (/^-?[0-9]{1,3}(?:[.][0-9]{3})+$/.test(raw)) raw = raw.replaceAll('.', '');
    return /^-?[0-9]+(?:[.][0-9]+)?$/.test(raw) ? Number(raw) : NaN;
}

function tripExpenseNumber(id) {
    const field = document.getElementById(id);
    const value = field.type === 'number' ? Number(field.value || 0) : parseTripMoney(field.value);
    const valid = Number.isFinite(value) && value >= 0;
    field.setCustomValidity(valid ? '' : 'Informe um valor válido, maior ou igual a zero.');
    return valid ? (field.type === 'number' ? value : Math.round((value + Number.EPSILON) * 100) / 100) : 0;
}

function getTripExpenses() {
    const expenses = {
        lunch: {qty: tripExpenseNumber('expLunchQty'), unit_val: tripExpenseNumber('expLunchVal')},
        overnight: {qty: tripExpenseNumber('expOvernightQty'), unit_val: tripExpenseNumber('expOvernightVal')},
        fuel: {litres: tripExpenseNumber('expFuelLitres'), total_val: tripExpenseNumber('expFuelVal')},
        toll_val: tripExpenseNumber('expTollVal'),
        other: {desc: document.getElementById('expOtherDesc').value.trim(), val: tripExpenseNumber('expOtherVal')}
    };
    [['lunch', 'expLunchQty', 'expLunchVal'], ['overnight', 'expOvernightQty', 'expOvernightVal']].forEach(([key, qtyId, valueId]) => {
        if (expenses[key].unit_val > 0 && expenses[key].qty === 0) {
            document.getElementById(qtyId).setCustomValidity('Informe a quantidade para calcular esta despesa.');
        }
        if (expenses[key].qty > 0 && expenses[key].unit_val === 0) {
            document.getElementById(valueId).setCustomValidity('Informe o valor unitário para calcular esta despesa.');
        }
    });
    return expenses;
}

function formatTripMoney(field) {
    const value = parseTripMoney(field.value);
    if (field.value.trim() && Number.isFinite(value) && value >= 0) field.value = tripMoneyFormatter.format(value);
    calcExpensesTotal();
}

function calcExpensesTotal() {
    const expenses = getTripExpenses();
    const cents = value => Math.round((value + Number.EPSILON) * 100);
    const lunch = cents(expenses.lunch.qty * expenses.lunch.unit_val);
    const overnight = cents(expenses.overnight.qty * expenses.overnight.unit_val);
    const fuel = cents(expenses.fuel.total_val);
    const total = lunch + overnight + fuel + cents(expenses.toll_val) + cents(expenses.other.val);
    document.getElementById('expLunchTotal').textContent = tripMoneyFormatter.format(lunch / 100);
    document.getElementById('expOvernightTotal').textContent = tripMoneyFormatter.format(overnight / 100);
    document.getElementById('expFuelTotal').textContent = tripMoneyFormatter.format(fuel / 100);
    document.getElementById('displayTotalExpenses').textContent = tripMoneyFormatter.format(total / 100);
    document.getElementById('displayTotalExpensesBadge').textContent = 'Total: ' + tripMoneyFormatter.format(total / 100);
}

function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
}

let openOrdersCache = [];

function addOrderRow(data) {
    data = data || {};
    rowCounter++;
    const tbody = document.getElementById('ordersTableBody');
    const tr = document.createElement('tr');
    tr.id = 'row_' + rowCounter;

    const badgeHtml = data.status_badge
        ? `<div id="badge_baixa_${rowCounter}" style="margin-top:3px;"><span class="status-pill status-ok" style="font-size:10px;">${escapeHtml(data.status_badge)}</span></div>`
        : `<div id="badge_baixa_${rowCounter}"></div>`;

    tr.innerHTML = `
        <td>
            <input type="text" name="order_number" value="${escapeHtml(data.order_number || '')}" placeholder="Nº do pedido" onblur="lookupItem(this, ${rowCounter})">
            <input type="hidden" name="item_number" value="${escapeHtml(data.item_number || '')}">
        </td>
        <td>
            <input type="text" name="invoice_number" value="${escapeHtml(data.invoice_number || '')}" placeholder="Nº da NF" onblur="lookupItem(this, ${rowCounter})">
            ${badgeHtml}
        </td>
        <td>
            <input type="text" name="client_name" id="client_${rowCounter}" value="${escapeHtml(data.client_name || '')}" placeholder="Nome do cliente">
        </td>
        <td>
            <select name="payment_type">
                <option value="Boleto" ${data.payment_type === 'Boleto' ? 'selected' : ''}>Boleto</option>
                <option value="Promissória" ${data.payment_type === 'Promissória' ? 'selected' : ''}>Promissória</option>
                <option value="À Vista" ${data.payment_type === 'À Vista' ? 'selected' : ''}>À Vista</option>
                <option value="Cartão" ${data.payment_type === 'Cartão' ? 'selected' : ''}>Cartão</option>
                <option value="Outro" ${data.payment_type === 'Outro' ? 'selected' : ''}>Outro</option>
            </select>
        </td>
        <td>
            <input type="date" name="delivery_date" value="${escapeHtml(data.delivery_date || document.getElementById('dataSaida').value)}">
        </td>
        <td>
            <input type="text" name="km_hr_end" value="${escapeHtml(data.km_hr_end || '')}" placeholder="Ex: 403751 / 15:50">
        </td>
        <td>
            <input type="number" step="0.1" name="weight_kg" id="weight_${rowCounter}" value="${escapeHtml(data.weight_kg || '')}" placeholder="KG" required oninput="calcTotals()">
        </td>
        <td>
            <input type="text" name="notes" id="notes_${rowCounter}" value="${escapeHtml(data.notes || '')}" placeholder="Ex: 100 WINTE FOS RECILA 30 KG">
        </td>
        <td style="text-align:center;">
            <button type="button" onclick="removeRow(${rowCounter})" style="background:none; border:none; color:#ef4444; font-weight:bold; cursor:pointer; font-size:1.1rem;">✕</button>
        </td>
    `;

    tbody.appendChild(tr);
    if (Object.keys(data).length > 0) {
        tr.querySelectorAll('input[name], select[name]').forEach(field => {
            if (field.type === 'hidden' || !field.value || field.name === 'km_hr_end') return;
            field.dataset.tripAutoValue = field.value;
            field.dataset.tripAutoDefault = field.name === 'payment_type' ? 'Boleto'
                : field.name === 'delivery_date' ? document.getElementById('dataSaida').value : '';
        });
    }
    calcTotals();
}

function removeRow(id) {
    cancelTripLookup(id);
    const tr = document.getElementById('row_' + id);
    if (tr) tr.remove();
    calcTotals();
}

function calcTotals() {
    const rows = document.querySelectorAll('#ordersTableBody tr');
    let totalW = 0;
    rows.forEach(r => {
        const wInput = r.querySelector('input[name="weight_kg"]');
        if (wInput) {
            totalW += parseFloat(wInput.value) || 0;
        }
    });
    document.getElementById('displayTotalOrders').textContent = rows.length;
    document.getElementById('displayTotalWeight').textContent = totalW.toLocaleString('pt-BR') + ' kg';
}

const tripLookups = new Map();

function cancelTripLookup(rId) {
    const previous = tripLookups.get(rId);
    if (previous) {
        clearTimeout(previous.timer);
        clearTimeout(previous.timeout);
        previous.controller?.abort();
        tripLookups.delete(rId);
    }
}

function scheduleTripLookup(inputElem) {
    const row = inputElem.closest('#ordersTableBody tr');
    if (!row || !['order_number', 'invoice_number'].includes(inputElem.name)) return;
    const rId = Number(row.id.replace('row_', ''));
    cancelTripLookup(rId);
    // Remove somente valores gerados pela consulta anterior. Uma edição manual
    // do operador permanece preservada.
    row.querySelectorAll('[data-trip-auto-value]').forEach(field => {
        if (field !== inputElem && field.value === field.dataset.tripAutoValue) {
            field.value = field.dataset.tripAutoDefault || '';
        }
        delete field.dataset.tripAutoValue;
        delete field.dataset.tripAutoDefault;
    });
    delete row.dataset.tripLookupKey;
    const badge = document.getElementById('badge_baixa_' + rId);
    if (badge) badge.textContent = '';
    calcTotals();
    if (!inputElem.value.trim()) return;
    const state = {timer: setTimeout(() => lookupItem(inputElem, rId), 450)};
    tripLookups.set(rId, state);
}

function fillTripField(field, value, initialValue = '') {
    if (!field || value === undefined || value === null || value === '') return;
    if (field.dataset.tripManual === 'true') return;
    const canFill = !field.value || field.value === field.dataset.tripAutoValue || field.value === initialValue;
    if (!canFill) return;
    const text = String(value);
    if (field.tagName === 'SELECT' && !Array.from(field.options).some(option => option.value === text)) {
        field.add(new Option(text, text));
    }
    field.dataset.tripAutoDefault = initialValue;
    field.value = text;
    field.dataset.tripAutoValue = field.value;
}

async function lookupItem(inputElem, rId) {
    const val = inputElem.value.trim();
    const row = document.getElementById('row_' + rId);
    if (!row) return;
    const key = inputElem.name + ':' + val;
    const previous = tripLookups.get(rId);
    if (previous?.key === key || row.dataset.tripLookupKey === key) return;
    cancelTripLookup(rId);
    const badgeContainer = document.getElementById('badge_baixa_' + rId);
    if (!val) {
        if (badgeContainer) badgeContainer.innerHTML = '';
        return;
    }
    const state = {key, controller: new AbortController()};
    state.timeout = setTimeout(() => { state.timedOut = true; state.controller.abort(); }, 12000);
    tripLookups.set(rId, state);
    if (badgeContainer) badgeContainer.innerHTML = '<span class="status-pill">Consultando pedido…</span>';
    try {
        const resp = await fetch('/api/trips/search-item?q=' + encodeURIComponent(val) + '&field=' + encodeURIComponent(inputElem.name), {signal: state.controller.signal});
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.message || 'Não foi possível consultar o pedido. Tente novamente.');
        if (tripLookups.get(rId) !== state || !row.isConnected || inputElem.value.trim() !== val) return;
        row.dataset.tripLookupKey = key;
        if (data.ok && data.item) {
            const clientInput = document.getElementById('client_' + rId);
            const orderInput = row?.querySelector('input[name="order_number"]');
            const invoiceInput = row?.querySelector('input[name="invoice_number"]');
            const weightInput = document.getElementById('weight_' + rId);
            const notesInput = document.getElementById('notes_' + rId);

            if (orderInput !== inputElem) fillTripField(orderInput, data.item.order_number);
            if (invoiceInput !== inputElem) fillTripField(invoiceInput, data.item.invoice_number);
            fillTripField(clientInput, data.item.client_name);
            fillTripField(weightInput, data.item.weight_kg || '', '0');
            fillTripField(notesInput, data.item.products_summary);
            fillTripField(row.querySelector('select[name="payment_type"]'), data.item.payment_method, 'Boleto');
            // A data digitada é a entrega realizada; o prazo comercial vem separado.
            calcTotals();

            if (badgeContainer) {
                if (data.item.is_in_orders_list) {
                    badgeContainer.innerHTML = `<span class="status-pill status-ok" style="font-size:10px; margin-top:2px;">🟢 Na Lista (${escapeHtml(data.item.status)}) → Baixa Automática</span>`;
                } else if (data.item.status === 'Acertado') {
                    badgeContainer.innerHTML = `<span class="status-pill" style="background:#e0f2fe; color:#0369a1; border:1px solid #bae6fd; font-size:10px; margin-top:2px;">ℹ️ Já Acertado no Sistema</span>`;
                } else if (['ORACLE', 'ERP', 'ERP_CACHE', 'ERP_MASTER'].includes(data.item.source)) {
                    badgeContainer.innerHTML = `<span class="status-pill" style="background:#f3e8ff; color:#6b21a8; border:1px solid #d8b4fe; font-size:10px; margin-top:2px;">⚡ ERP (${escapeHtml(data.item.order_number)})</span>`;
                } else {
                    badgeContainer.innerHTML = '';
                }
                if (data.item.details_complete === false) {
                    badgeContainer.innerHTML += '<span class="status-pill status-warn" style="font-size:10px; margin-top:3px;">Dados parciais: confira peso e produtos.</span>';
                }
            }
        } else {
            if (badgeContainer) {
                badgeContainer.innerHTML = `<span class="status-pill status-warn" style="font-size:10px; margin-top:2px;">📝 Avulso / Manual</span>`;
            }
        }
    } catch (e) {
        if ((e.name === 'AbortError' && !state.timedOut) || tripLookups.get(rId) !== state || !row.isConnected) return;
        const message = state.timedOut ? 'A consulta demorou demais. Saia do campo para tentar novamente.'
            : e.message || 'Falha na consulta. Saia do campo para tentar novamente.';
        if (badgeContainer) badgeContainer.innerHTML = `<span class="status-pill status-warn">${escapeHtml(message)}</span>`;
        console.warn('Erro ao autocompletar item:', e);
    } finally {
        clearTimeout(state.timeout);
        if (tripLookups.get(rId) === state) tripLookups.delete(rId);
    }
}

async function openOpenOrdersModal() {
    const modal = document.getElementById('openOrdersModal');
    const loading = document.getElementById('openOrdersLoading');
    const container = document.getElementById('openOrdersTableContainer');
    modal.style.display = 'flex';
    loading.style.display = 'block';
    container.style.display = 'none';

    try {
        const resp = await fetch('/api/trips/open-orders');
        const data = await resp.json();
        openOrdersCache = (data && data.orders) ? data.orders : [];
        loading.style.display = 'none';
        container.style.display = 'block';
        renderOpenOrdersTable(openOrdersCache);
    } catch (e) {
        alert('Erro ao carregar lista de pedidos em aberto: ' + e.message);
        closeOpenOrdersModal();
    }
}

function closeOpenOrdersModal() {
    document.getElementById('openOrdersModal').style.display = 'none';
}

function renderOpenOrdersTable(orders) {
    const tbody = document.getElementById('openOrdersTableBody');
    tbody.innerHTML = '';
    const masterCb = document.getElementById('selectAllOpenOrders');
    if (masterCb) masterCb.checked = false;

    if (!orders || orders.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:20px; color:var(--muted);">Nenhum pedido em aberto encontrado.</td></tr>';
        updateSelectedOpenOrdersCount();
        return;
    }

    orders.forEach(o => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #e2e8f0';

        let statusClass = 'status-warn';
        if (o.status === 'Faturado') statusClass = 'status-ok';
        else if (o.status === 'Carga Montada') statusClass = 'status-warn';

        const docStr = o.invoice_number ? `Ped ${escapeHtml(o.order_number)} / NF ${escapeHtml(o.invoice_number)}` : `Ped ${escapeHtml(o.order_number)}`;
        const valStr = 'R$ ' + Number(o.total_value || 0).toLocaleString('pt-BR', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        const wStr = Number(o.weight_kg || 0).toLocaleString('pt-BR') + ' kg';
        const orderJson = encodeURIComponent(JSON.stringify(o));

        tr.innerHTML = `
            <td style="text-align:center; padding:8px;">
                <input type="checkbox" class="open-order-cb" data-order="${orderJson}" onchange="updateSelectedOpenOrdersCount()">
            </td>
            <td style="padding:8px;"><b>${docStr}</b></td>
            <td style="padding:8px;">${escapeHtml(o.client_name || 'Consumidor Final')}</td>
            <td style="padding:8px;">${escapeHtml(o.city || 'Teófilo Otoni')}</td>
            <td style="text-align:center; padding:8px;">
                <span class="status-pill ${statusClass}">${escapeHtml(o.status)}</span>
            </td>
            <td style="text-align:right; padding:8px;">${wStr}</td>
            <td style="text-align:right; padding:8px;">${valStr}</td>
        `;
        tbody.appendChild(tr);
    });

    updateSelectedOpenOrdersCount();
}

function filterOpenOrdersTable() {
    const term = (document.getElementById('openOrdersSearch').value || '').toLowerCase().trim();
    if (!term) {
        renderOpenOrdersTable(openOrdersCache);
        return;
    }
    const filtered = openOrdersCache.filter(o => {
        return (o.order_number && o.order_number.toLowerCase().includes(term)) ||
               (o.invoice_number && o.invoice_number.toLowerCase().includes(term)) ||
               (o.client_name && o.client_name.toLowerCase().includes(term)) ||
               (o.city && o.city.toLowerCase().includes(term)) ||
               (o.status && o.status.toLowerCase().includes(term));
    });
    renderOpenOrdersTable(filtered);
}

function toggleSelectAllOpenOrders(masterCb) {
    const checkboxes = document.querySelectorAll('.open-order-cb');
    checkboxes.forEach(cb => {
        cb.checked = masterCb.checked;
    });
    updateSelectedOpenOrdersCount();
}

function updateSelectedOpenOrdersCount() {
    const checked = document.querySelectorAll('.open-order-cb:checked');
    const counter = document.getElementById('openOrdersSelectedCount');
    if (counter) {
        counter.textContent = checked.length + ' selecionado(s)';
    }
}

function addSelectedOpenOrders() {
    const checked = document.querySelectorAll('.open-order-cb:checked');
    if (checked.length === 0) {
        alert('Por favor, selecione pelo menos um pedido da lista.');
        return;
    }

    // Se houver apenas 1 linha em branco no formulário, remove-a
    const existingRows = document.querySelectorAll('#ordersTableBody tr');
    if (existingRows.length === 1) {
        const orderInp = existingRows[0].querySelector('input[name="order_number"]');
        const invoiceInp = existingRows[0].querySelector('input[name="invoice_number"]');
        if ((!orderInp || !orderInp.value.trim()) && (!invoiceInp || !invoiceInp.value.trim())) {
            existingRows[0].remove();
        }
    }

    checked.forEach(cb => {
        try {
            const o = JSON.parse(decodeURIComponent(cb.getAttribute('data-order')));
            addOrderRow({
                order_number: o.order_number,
                invoice_number: o.invoice_number,
                client_name: o.client_name,
                payment_type: o.payment_method || 'Boleto',
                delivery_date: o.delivery_date || '',
                weight_kg: o.weight_kg,
                notes: o.notes || '',
                status_badge: `🟢 Na Lista (${o.status}) → Baixa Automática`
            });
        } catch (e) {
            console.error('Erro ao adicionar pedido selecionado:', e);
        }
    });

    closeOpenOrdersModal();
    calcTotals();
}

function getTripFormData() {
    const rows = [];
    document.querySelectorAll('#ordersTableBody tr').forEach(r => {
        rows.push({
            order_number: r.querySelector('input[name="order_number"]')?.value.trim() || '',
            invoice_number: r.querySelector('input[name="invoice_number"]')?.value.trim() || '',
            item_number: r.querySelector('input[name="order_number"]')?.value.trim() || r.querySelector('input[name="invoice_number"]')?.value.trim() || r.querySelector('input[name="item_number"]')?.value.trim() || '',
            client_name: r.querySelector('input[name="client_name"]')?.value.trim() || '',
            payment_type: r.querySelector('select[name="payment_type"]')?.value || 'Boleto',
            delivery_date: r.querySelector('input[name="delivery_date"]')?.value || '',
            km_hr_end: r.querySelector('input[name="km_hr_end"]')?.value.trim() || '',
            weight_kg: parseFloat(r.querySelector('input[name="weight_kg"]')?.value) || 0,
            notes: r.querySelector('input[name="notes"]')?.value.trim() || ''
        });
    });

    const kmStart = parseFloat(document.getElementById('kmStart').value) || 0;
    const kmEnd = parseFloat(document.getElementById('kmEnd').value) || 0;

    return {
        data_saida: document.getElementById('dataSaida').value,
        hora_saida: document.getElementById('horaSaida').value,
        data_chegada: document.getElementById('dataChegada').value,
        hora_chegada: document.getElementById('horaChegada').value,
        driver_id: parseInt(document.getElementById('driverId').value) || 0,
        helper_name: document.getElementById('helperName').value.trim(),
        vehicle_id: parseInt(document.getElementById('vehicleId').value) || 0,
        km_start: kmStart,
        km_end: kmEnd,
        km_total: kmEnd > kmStart ? (kmEnd - kmStart) : 0,
        destination: document.getElementById('destination').value.trim(),
        orders: rows,
        expenses: getTripExpenses()
    };
}

async function openVerifyModal() {
    if (tripLookups.size > 0) {
        alert('Aguarde a consulta dos pedidos terminar antes de conferir a viagem.');
        return;
    }
    const data = getTripFormData();
    if (!data.driver_id || !data.vehicle_id) {
        alert('Por favor, selecione o Motorista e o Veículo.');
        return;
    }
    if (data.orders.length === 0) {
        alert('Adicione pelo menos 1 NF ou Pedido para a viagem.');
        return;
    }
    if (data.orders.some(order => !order.order_number && !order.invoice_number && !order.item_number)) {
        alert('Informe o número do pedido ou da nota fiscal em cada linha.');
        return;
    }

    const modal = document.getElementById('verifyModal');
    const loading = document.getElementById('verifyLoading');
    const content = document.getElementById('verifyContent');
    modal.style.display = 'flex';
    loading.style.display = 'block';
    content.style.display = 'none';

    try {
        const resp = await fetch('/api/trips/verify', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        const res = await resp.json();
        verifiedTripData = res;
        verifiedTripPayload = JSON.stringify(data);

        loading.style.display = 'none';
        content.style.display = 'block';

        const summaryBox = document.getElementById('verifySummaryBox');
        let summaryHtml = '';
        if (res.all_matched) {
            summaryBox.style.background = '#dcfce7';
            summaryBox.style.border = '1px solid #86efac';
            summaryBox.style.color = '#15803d';
            summaryHtml = `<b>✓ 100% Conciliado!</b> Todos os ${res.items.length} pedidos foram validados no banco de dados e ERP.`;
        } else {
            summaryBox.style.background = '#fffbeb';
            summaryBox.style.border = '1px solid #fef3c7';
            summaryBox.style.color = '#92400e';
            summaryHtml = `<b>Atenção:</b> ${res.divergent_count} item(ns) possuem divergência de peso ou são lançamentos manuais não faturados.`;
        }

        if (res.orders_to_baixa_count > 0) {
            summaryHtml += `<div style="margin-top:6px; font-weight:750; color:#15803d; font-size:13px;">✅ ${res.orders_to_baixa_count} pedido(s) cadastrado(s) na Lista de Pedidos receberão baixa automática ('Acertado') ao confirmar.</div>`;
        }
        const hasInvoiceMismatch = (res.items || []).some(item => ['DIVERGENCIA_NF', 'DIVERGENCIA_PEDIDO'].includes(item.status));
        const confirmButton = document.getElementById('btnConfirmSave');
        if (confirmButton) {
            confirmButton.disabled = hasInvoiceMismatch;
            confirmButton.title = hasInvoiceMismatch ? 'Corrija a nota fiscal divergente antes de gravar.' : '';
        }
        summaryBox.innerHTML = summaryHtml;

        const tbody = document.getElementById('verifyTableBody');
        tbody.innerHTML = '';
        res.items.forEach(it => {
            let badgeHtml = '';
            if (it.status === 'OK_BAIXA') {
                badgeHtml = `<span class="status-pill status-ok">🟢 Na Lista (${it.current_status})<br/><small>Baixa Automática</small></span>`;
            } else if (it.status === 'OK_HISTORICO') {
                badgeHtml = '<span class="status-pill" style="background:#e0f2fe; color:#0369a1; border:1px solid #bae6fd;">✓ Já Acertado</span>';
            } else if (it.status === 'OK_ERP_NOVO') {
                badgeHtml = '<span class="status-pill" style="background:#f3e8ff; color:#6b21a8; border:1px solid #d8b4fe;">✓ Encontrado no ERP</span>';
            } else if (it.status === 'DIVERGENCIA_PESO') {
                badgeHtml = '<span class="status-pill status-warn">⚠️ Peso Divergente</span>';
            } else if (it.status === 'DIVERGENCIA_NF' || it.status === 'DIVERGENCIA_PEDIDO') {
                badgeHtml = '<span class="status-pill status-danger">⚠️ Pedido / NF divergentes</span>';
            } else {
                badgeHtml = '<span class="status-pill status-danger">✕ Manual / Não achou</span>';
            }

            const tr = document.createElement('tr');
            tr.style.borderBottom = '1px solid #e2e8f0';
            tr.innerHTML = `
                <td style="padding:8px;"><b>${escapeHtml(it.input)}</b></td>
                <td style="padding:8px;">${escapeHtml(it.erp_doc || '—')}</td>
                <td style="padding:8px;">${escapeHtml(it.erp_client || it.typed_client || 'Consumidor Final')}</td>
                <td style="padding:8px; text-align:right;">${Number(it.typed_weight || 0).toLocaleString('pt-BR')} kg</td>
                <td style="padding:8px; text-align:right;">${it.erp_weight ? Number(it.erp_weight).toLocaleString('pt-BR') + ' kg' : '—'}</td>
                <td style="padding:8px; text-align:center;">${badgeHtml}</td>
            `;
            tbody.appendChild(tr);
        });

        document.getElementById('verifyWarningNote').textContent = res.note || 'Ao confirmar, o sistema criará a carga oficial com status "Acertada", registrando a telemetria, pedidos e despesas.';
    } catch (e) {
        alert('Erro ao consultar banco/ERP: ' + e.message);
        closeVerifyModal();
    }
}

function closeVerifyModal() {
    document.getElementById('verifyModal').style.display = 'none';
}

async function executeSaveTrip() {
    const btn = document.getElementById('btnConfirmSave');
    const data = getTripFormData();
    if (!verifiedTripData || verifiedTripPayload !== JSON.stringify(data)) {
        alert('Os dados foram alterados desde a conferência. Confira novamente antes de gravar.');
        closeVerifyModal();
        openVerifyModal();
        return;
    }
    if ((verifiedTripData.items || []).some(item => ['DIVERGENCIA_NF', 'DIVERGENCIA_PEDIDO'].includes(item.status))) {
        alert('Corrija a nota fiscal divergente na linha indicada antes de gravar.');
        return;
    }
    btn.disabled = true;
    btn.textContent = 'Gravando Carga no Sistema...';

    try {
        const resp = await fetch('/api/trips/save', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        const res = await resp.json();
        if (res.ok) {
            let msg = 'Viagem gravada com sucesso! Carga #' + res.route_id + ' gerada.';
            if (res.orders_given_baixa_count > 0) {
                msg += '\\n\\n✅ ' + res.orders_given_baixa_count + ' pedido(s) da Lista de Pedidos tiveram baixa automática realizada com sucesso!';
            }
            alert(msg);
            window.location.href = '/routes/' + res.route_id;
        } else {
            alert('Erro ao gravar: ' + (res.message || 'Falha desconhecida'));
            btn.disabled = false;
            btn.textContent = 'Confirmar e Gravar no Sistema';
        }
    } catch (e) {
        alert('Falha na comunicação com o servidor: ' + e.message);
        btn.disabled = false;
        btn.textContent = 'Confirmar e Gravar no Sistema';
    }
}

function initializeTripPage() {
    const tbody = document.getElementById('ordersTableBody');
    tbody.addEventListener('input', event => {
        const field = event.target;
        if (['order_number', 'invoice_number'].includes(field.name)) scheduleTripLookup(field);
        else if (field.matches('input, select')) field.dataset.tripManual = 'true';
    });
    tbody.addEventListener('change', event => {
        if (event.target.matches('select')) event.target.dataset.tripManual = 'true';
    });
    calcTotals();
    calcExpensesTotal();
}
if (document.readyState === 'loading') {
    window.addEventListener('DOMContentLoaded', initializeTripPage, { once: true });
} else {
    initializeTripPage();
}
</script>
"""


def render_registro_viagens_page(handler, user, layout_func=None):
    """Renderiza a tela web espelho da folha de papel 'REGISTRO DE VIAGENS' integrada ao padrão visual do sistema."""
    today_iso = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("%H:%M")

    with handler.conn() as db:
        drivers = db.execute("SELECT id, name FROM drivers WHERE active=1 ORDER BY name").fetchall()
        vehicles = db.execute("SELECT id, name, plate, type, capacity_kg FROM vehicles WHERE active=1 ORDER BY name").fetchall()

    driver_opts = "".join(f"<option value='{d['id']}'>{_esc(d['name'])}</option>" for d in drivers)
    vehicle_opts = "".join(f"<option value='{v['id']}' data-capacity='{v['capacity_kg'] or 0}'>{_esc(v['name'])} - {_esc(v['plate'] or 'S/ Placa')} ({v['capacity_kg'] or 0:,.0f} kg)</option>" for v in vehicles)

    content = (
        REGISTRO_VIAGENS_HTML_TEMPLATE
        .replace("__TODAY_ISO__", today_iso)
        .replace("__NOW_TIME__", now_time)
        .replace("__DRIVER_OPTS__", driver_opts)
        .replace("__VEHICLE_OPTS__", vehicle_opts)
    )

    if layout_func is None:
        import sys
        for module_name in ("app", "__main__"):
            candidate = getattr(sys.modules.get(module_name), "layout", None)
            if callable(candidate):
                layout_func = candidate
                break

    if layout_func:
        page_html = layout_func(
            user,
            "Registro de Viagens",
            content,
            subtitle="Espelho Digital da Prancheta de Viagens com Conferência em Tempo Real",
            head_actions_extra='<a href="/routes" class="btn ghost small">← Voltar para Cargas</a>'
        )
    else:
        page_html = content

    handler.send_html(page_html)


def _get_candidates(raw_val: str) -> list[str]:
    raw_str = str(raw_val or "").strip()
    if not raw_str:
        return []
    candidates = []
    seen = set()

    def add(c):
        c = str(c or "").strip()
        if c and c not in seen:
            seen.add(c)
            candidates.append(c)

    add(raw_str)
    add(raw_str.lstrip("0"))
    digits = re.sub(r"\D", "", raw_str)
    if digits:
        add(digits)
        clean_d = digits.lstrip("0")
        if clean_d:
            add(clean_d)
            for length in (5, 6, 7, 8):
                add(clean_d.zfill(length))
    return candidates


def find_order_in_sqlite(db, query_str: str, query_field: str = "any"):
    """Localiza pedido no SQLite de forma tolerante (com/sem zeros, NF ou Pedido), priorizando em aberto."""
    cands = _get_candidates(query_str)
    if query_field != "any":
        raw = str(query_str or "").strip()
        cands = list(dict.fromkeys([raw, raw.lstrip("0") or "0"])) if raw else []
    if not cands:
        return None
    qmarks = ",".join("?" for _ in cands)
    fields = (query_field,) if query_field in ("order_number", "invoice_number") else ("order_number", "invoice_number")
    clauses = [f"o.{field} IN ({qmarks})" for field in fields]
    clauses += [f"LTRIM(o.{field}, '0') IN ({qmarks})" for field in fields]
    sql = f"""
        SELECT o.*, c.name as client_name
        FROM orders o
        LEFT JOIN clients c ON c.id = o.client_id
        WHERE {' OR '.join(clauses)}
        ORDER BY 
           CASE WHEN o.status NOT IN ('Acertado', 'Cancelado') THEN 0 ELSE 1 END,
           o.id DESC
        LIMIT 1
    """
    params = cands * len(clauses)
    return db.execute(sql, params).fetchone()


def _get_oracle_cursor():
    """Usa a configuração central do ERP, respeitando integração desativada."""
    connection = None
    try:
        from app_core import erp_connector
        cfg = erp_connector.get_erp_config()
        if not cfg.is_ready or cfg.driver != "oracle":
            return None, None
        connection = erp_connector._build_connection(cfg)
        connection.call_timeout = max(1000, int(cfg.timeout) * 1000)
        return connection, connection.cursor()
    except Exception:
        if connection:
            try:
                connection.close()
            except Exception:
                pass
        return None, None


def _calculate_oracle_weight_and_summary(rows):
    tw = 0.0
    for r in rows:
        desc = (r[4] or "").upper()
        q = float(r[5] or 1)
        uw = 1.0
        if "KG" in desc:
            m = re.search(r"(\d+)\s*KG", desc)
            if m: uw = float(m.group(1))
        elif "SC" in desc: uw = 40.0
        elif "TN" in desc: uw = 1000.0
        tw += (uw * q)
    summary = ", ".join(f"{r[5]}x {r[4]}" for r in rows[:3])
    return tw, summary


def find_order_in_db_or_oracle(db, query_str: str, ora_cur=None, query_field="any"):
    """
    Busca unificada e cruzada:
    1. Tenta achar direto no SQLite (por Pedido ou NF, tolerante a zeros e formatação).
    2. Se não achar no SQLite e houver Oracle, busca no ERP (PEDIDO_ ou NOTA_).
       - Se achar no ERP, com PEDIDO_ e NOTA_ em mãos, faz busca cruzada novamente no SQLite!
    Retorna tupla: (sqlite_row, source, erp_info)
    """
    row_sq = find_order_in_sqlite(db, query_str, query_field=query_field)
    if row_sq:
        return row_sq, "SQLITE", None

    if ora_cur:
        info = trip_order_metadata.lookup_metadata(db, query_str, ora_cur, query_field)
        if info:
            crossed = find_order_in_sqlite(db, info["order_number"], query_field="order_number")
            if crossed and not trip_order_metadata.matches_client(db, crossed, info):
                raise ValueError("A referência do ERP pertence a outro cliente. Confira o pedido antes de gravar.")
            return crossed, "ERP_MASTER", info

    cached = _cached_trip_raw(db, query_str, query_field)
    if cached:
        info = _trip_info_from_raw(cached, "ERP_CACHE")
        # O cache da view de vendas pode conter somente a última linha de um
        # pedido. Não usa essa linha isolada como total para validar/gravar.
        if cached.get("_itens") and info["weight_kg"] > 0:
            crossed = find_order_in_sqlite(db, info["order_number"], query_field="order_number")
            return crossed, "ERP_CACHE", info

    return None, "MANUAL", None


def _log_order_history(db, order_id: int, user_id: int | None, old_status: str, new_status: str, action: str, notes: str = ""):
    """Registra auditoria em order_history de forma segura."""
    try:
        db.execute("""
            INSERT INTO order_history (order_id, user_id, old_status, new_status, action, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """, (order_id, user_id, old_status, new_status, action, notes))
    except Exception:
        pass


def api_open_orders(handler, user):
    """Retorna todos os pedidos atualmente em aberto no sistema que aguardam entrega/baixa."""
    with handler.conn() as db:
        rows = db.execute("""
            SELECT o.id, o.order_number, o.invoice_number, o.status, o.weight_kg, o.total_value,
                   o.sale_date, o.expected_delivery_date, o.payment_method, o.city, c.name as client_name, o.notes
            FROM orders o
            LEFT JOIN clients c ON c.id = o.client_id
            WHERE o.status NOT IN ('Acertado', 'Problema', 'Cancelado')
            ORDER BY 
                CASE o.status 
                    WHEN 'Faturado' THEN 1 
                    WHEN 'Carga Montada' THEN 2 
                    WHEN 'Venda' THEN 3 
                    ELSE 4 
                END,
                o.id DESC
        """).fetchall()

        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "order_number": r["order_number"] or "",
                "invoice_number": r["invoice_number"] or "",
                "status": r["status"] or "",
                "client_name": r["client_name"] or "Consumidor Final",
                "city": r["city"] or "",
                "weight_kg": float(r["weight_kg"] or 0),
                "total_value": float(r["total_value"] or 0),
                "sale_date": r["sale_date"] or "",
                "payment_method": r["payment_method"] or "",
                "delivery_date": "",
                "expected_delivery_date": (r["expected_delivery_date"] or "")[:10],
                "notes": r["notes"] or ""
            })

    return handler.send_json({"ok": True, "orders": items, "count": len(items)})


def _trip_reference(value):
    value = str(value or "").strip()
    if re.fullmatch(r"\d+(?:\.0+)?", value):
        return str(int(value.split(".")[0]))
    return value


def _cached_trip_raw(db, query, query_field):
    """Lê o cache existente sem substituir dados nem iniciar sincronização."""
    try:
        if query_field == "invoice_number":
            columns = ("nota_", "nota_fiscal", "numeronf", "numero_nota_fiscal_saida")
            terms = [f"LTRIM(CAST(json_extract(raw_json, '$.{column}') AS TEXT), '0')=?" for column in columns]
            rows = db.execute(
                "SELECT numeropedido,raw_json FROM erp_cache_pedidos WHERE " + " OR ".join(terms),
                [_trip_reference(query)] * len(terms),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT numeropedido,raw_json FROM erp_cache_pedidos WHERE numeropedido=? OR LTRIM(numeropedido,'0')=?",
                (query, _trip_reference(query)),
            ).fetchall()
        for row in rows:
            raw = json.loads(row["raw_json"])
            canonical = raw.get("pedido_") or raw.get("numeropedido") or raw.get("numero_pedido") or raw.get("numero") or row["numeropedido"]
            if query_field != "invoice_number" and _trip_reference(canonical) != _trip_reference(query):
                continue
            raw["pedido_"] = str(canonical)
            code = raw.get("codcliente_") or raw.get("codigocliente") or raw.get("codigo_cliente")
            if code and not raw.get("_cliente"):
                client = db.execute("SELECT raw_json FROM erp_cache_clientes WHERE codigocliente=?", (str(code),)).fetchone()
                if client:
                    raw["_cliente"] = json.loads(client["raw_json"])
            return raw
    except (sqlite3.OperationalError, ValueError, TypeError):
        pass
    return None


def _trip_info_from_raw(raw, source):
    from app_core.erp_field_mapper import map_erp_to_logistica
    raw = dict(raw)
    canonical = raw.get("pedido_") or raw.get("numeropedido") or raw.get("numero_pedido") or raw.get("numero")
    raw["pedido_"] = canonical
    raw["numeropedido"] = canonical
    raw["total_weight"] = raw.get("total_weight") or raw.get("peso_total") or 0
    # A tabela mestre usa nomes diferentes dos usados pela view de vendas.
    raw["_itens"] = [dict(item, codigoproduto=item.get("codigoproduto") or item.get("codigo_produto"),
                           nomeproduto=item.get("nomeproduto") or item.get("descricao_produto"),
                           valorunitario=item.get("valorunitario") or item.get("preco_venda"))
                     for item in (raw.get("_itens") or []) if isinstance(item, dict)]
    mapped = map_erp_to_logistica(raw)
    items = mapped.get("items") or []
    return {
        "order_number": str(canonical or mapped.get("order_number") or ""),
        "invoice_number": mapped.get("invoice_number") or "",
        "client_name": mapped.get("client_name") or "",
        "city": mapped.get("city") or "",
        "weight_kg": float(mapped.get("weight_kg") or 0),
        "total_value": float(mapped.get("total_value") or 0),
        "payment_method": mapped.get("payment_method") or "",
        "delivery_date": "",
        "sale_date": trip_order_metadata.day(raw.get("data") or raw.get("sale_date")),
        "invoiced_at": trip_order_metadata.day(raw.get("data_faturamento_pedido") or raw.get("invoiced_at")),
        "expected_delivery_date": mapped.get("expected_delivery_date") or "",
        "uf": mapped.get("uf") or "",
        "route_name": mapped.get("route_name") or "",
        "delivery_address": mapped.get("delivery_address") or "",
        "seller_name": mapped.get("seller_name") or "",
        "client_code": mapped.get("client_code") or raw.get("codigo_cliente") or "",
        "items": items,
        "products_summary": ", ".join(f"{float(item.get('quantity') or 0):g} {item.get('unit') or ''} {item.get('product_name') or ''}" for item in items),
        "source": source,
        "details_complete": bool(raw.get("_itens")) and float(mapped.get("weight_kg") or 0) > 0,
    }


def _lookup_trip_erp_info(db, query, query_field):
    """Lê o mestre do ERP sem executar importação, faturamento ou baixa."""
    from app_core import erp_connector
    raw = _cached_trip_raw(db, query, query_field)
    cached_info = _trip_info_from_raw(raw, "ERP_CACHE") if raw else None
    cfg = erp_connector.get_erp_config()
    if not cfg.is_ready:
        return cached_info
    connection, cursor = _get_oracle_cursor()
    try:
        if cursor:
            info = trip_order_metadata.lookup_metadata(db, query, cursor, query_field)
            if info:
                info["payment_method"] = (cached_info or {}).get("payment_method") or ""
                return info
        return cached_info
    finally:
        if connection:
            connection.close()


def api_search_item(handler, user):
    """Consulta para preencher a linha; nunca importa, fatura ou baixa pedidos."""
    qs = parse_qs(urlparse(handler.path).query)
    query = (qs.get("q", [""])[0] or "").strip()
    query_field = (qs.get("field", ["any"])[0] or "any").strip()
    if query_field not in ("any", "order_number", "invoice_number"):
        return handler.send_json({"ok": False, "message": "Campo de consulta inválido."}, 400)
    if not query:
        return handler.send_json({"ok": False, "message": "Consulta vazia."}, 400)

    ora_conn = None
    try:
        with handler.conn() as db:
            # O pedido operacional é a primeira fonte. Não abre Oracle para
            # consultar um pedido já importado pelas sincronizações existentes.
            existing = find_order_in_sqlite(db, query, query_field=query_field)
            erp_info = None
            if not existing:
                erp_info = _lookup_trip_erp_info(db, query, query_field)
                if erp_info:
                    existing = find_order_in_sqlite(db, erp_info["order_number"], query_field="order_number")

            if existing:
                items = db.execute("SELECT product_name, quantity, unit FROM order_items WHERE order_id=? ORDER BY id", (existing["id"],)).fetchall()
                summary = ", ".join(f"{float(it['quantity'] or 0):g} {it['unit'] or ''} {it['product_name']}" for it in items)
                if not existing["payment_method"] or not summary:
                    cached_raw = _cached_trip_raw(db, existing["order_number"], "order_number")
                    if cached_raw:
                        erp_info = erp_info or _trip_info_from_raw(cached_raw, "ERP_CACHE")
                if not summary and erp_info:
                    summary = erp_info.get("products_summary", "")

                is_open = existing["status"] not in ("Acertado", "Cancelado")
                return handler.send_json({
                    "ok": True,
                    "item": {
                        "order_id": existing["id"],
                        "order_number": existing["order_number"],
                        "invoice_number": existing["invoice_number"] or (erp_info.get("invoice_number") if erp_info else ""),
                        "client_name": existing["client_name"],
                        "city": existing["city"] or "",
                        "weight_kg": float(existing["weight_kg"] or 0),
                        "total_value": float(existing["total_value"] or 0),
                        "payment_method": existing["payment_method"] or (erp_info.get("payment_method") if erp_info else "") or "",
                        "delivery_date": "",
                        "sale_date": existing["sale_date"] or "",
                        "invoiced_at": existing["invoiced_at"] or "",
                        "expected_delivery_date": existing["expected_delivery_date"] or "",
                        "route_name": existing["route_name"] or "",
                        "uf": existing["uf"] or "",
                        "delivery_address": existing["delivery_address"] or "",
                        "seller_name": existing["seller_name"] or "",
                        "status": existing["status"],
                        "is_in_orders_list": is_open,
                        "products_summary": summary,
                        "source": "SQLITE"
                    }
                })

            if erp_info:
                return handler.send_json({
                    "ok": True,
                    "item": {
                        "order_id": None,
                        "order_number": erp_info["order_number"],
                        "invoice_number": erp_info["invoice_number"],
                        "client_name": erp_info["client_name"],
                        "city": erp_info.get("city") or "",
                        "weight_kg": erp_info["weight_kg"],
                        "total_value": erp_info["total_value"],
                        "payment_method": erp_info.get("payment_method", ""),
                        "delivery_date": "",
                        **{key: erp_info.get(key) or "" for key in (
                            "sale_date", "invoiced_at", "expected_delivery_date", "route_name",
                            "uf", "delivery_address", "seller_name")},
                        "status": "ERP",
                        "is_in_orders_list": False,
                        "products_summary": erp_info["products_summary"],
                        "details_complete": erp_info.get("details_complete", False),
                        "source": erp_info.get("source", "ERP")
                    }
                })

        return handler.send_json({"ok": True, "item": None})
    except ValueError as exc:
        return handler.send_json({"ok": False, "message": str(exc)}, 409)
    except Exception:
        return handler.send_json({"ok": False, "message": "Não foi possível consultar os dados completos do pedido. Tente novamente."}, 503)
    finally:
        if ora_conn:
            try: ora_conn.close()
            except Exception: pass


def api_verify_trip(handler, user):
    """Confronta a viagem digitada com SQLite e ERP Oracle para alimentar o modal de confirmação."""
    try:
        if hasattr(handler, "post_data"):
            data = handler.post_data()
        elif hasattr(handler, "_raw_post_body") and handler._raw_post_body:
            data = json.loads(handler._raw_post_body)
        else:
            length = int(handler.headers.get("content-length", 0))
            raw_body = handler.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            data = json.loads(raw_body)
        if isinstance(data, str):
            data = json.loads(data)
    except Exception as e:
        return handler.send_json({"ok": False, "message": f"Corpo JSON inválido: {e}"}, 400)

    orders_input = data.get("orders") or data.get("items") or []
    if not orders_input:
        return handler.send_json({"ok": False, "message": "Nenhum pedido informado."}, 400)

    ora_conn, ora_cur = _get_oracle_cursor()
    results = []
    all_matched = True
    divergent_count = 0
    orders_to_baixa = 0

    try:
        with handler.conn() as db:
            for it in orders_input:
                order_num_input = (it.get("order_number") or "").strip()
                invoice_num_input = (it.get("invoice_number") or "").strip()
                raw_num = (order_num_input or invoice_num_input or it.get("item_number") or "").strip()
                typed_w = float(it.get("weight_kg") or 0)
                typed_c = it.get("client_name", "").strip()

                lookup_field = "order_number" if order_num_input else "invoice_number" if invoice_num_input else "any"
                existing, source, erp_info = find_order_in_db_or_oracle(db, raw_num, ora_cur=ora_cur, query_field=lookup_field)
                if not existing and not erp_info and invoice_num_input and order_num_input:
                    existing, source, erp_info = find_order_in_db_or_oracle(db, invoice_num_input, ora_cur=ora_cur, query_field="invoice_number")

                if existing:
                    is_open = existing["status"] not in ("Acertado", "Cancelado")
                    if is_open:
                        orders_to_baixa += 1

                    if ora_cur:
                        live_metadata = trip_order_metadata.lookup_metadata(db, existing["order_number"], ora_cur, "order_number")
                        if live_metadata and trip_order_metadata.matches_client(db, existing, live_metadata):
                            erp_info = live_metadata
                    erp_w = float(erp_info["weight_kg"] if erp_info and erp_info.get("metadata_verified") else existing["weight_kg"] or 0)
                    erp_c = existing["client_name"] or typed_c
                    w_diff = abs(erp_w - typed_w)
                    is_w_ok = w_diff < 5.0 or (typed_w > 0 and erp_w == 0)

                    invoice_mismatch = bool(
                        invoice_num_input and existing["invoice_number"] and
                        re.sub(r"\D", "", invoice_num_input).lstrip("0") !=
                        re.sub(r"\D", "", str(existing["invoice_number"])).lstrip("0")
                    )

                    order_mismatch = bool(order_num_input and _trip_reference(order_num_input) != _trip_reference(existing["order_number"]))
                    if order_mismatch:
                        status = "DIVERGENCIA_PEDIDO"
                        all_matched = False
                        divergent_count += 1
                    elif invoice_mismatch:
                        status = "DIVERGENCIA_NF"
                        all_matched = False
                        divergent_count += 1
                    elif not is_w_ok:
                        status = "DIVERGENCIA_PESO"
                        all_matched = False
                        divergent_count += 1
                    elif is_open:
                        status = "OK_BAIXA"
                    else:
                        status = "OK_HISTORICO"

                    doc_display = f"Ped {existing['order_number']}"
                    if existing["invoice_number"]:
                        doc_display += f" / NF {existing['invoice_number']}"

                    results.append({
                        "input": raw_num,
                        "order_id": existing["id"],
                        "erp_doc": doc_display,
                        "erp_client": erp_c,
                        "typed_client": typed_c,
                        "typed_weight": typed_w,
                        "erp_weight": erp_w,
                        "invoice_mismatch": invoice_mismatch,
                        "status": status,
                        "is_open_order": is_open,
                        "current_status": existing["status"],
                        "source": "ERP_MASTER" if erp_info and erp_info.get("metadata_verified") else "SQLITE" if source == "SQLITE" else "ORACLE_CRUZADO"
                    })
                    continue

                if erp_info:
                    tw = erp_info["weight_kg"]
                    w_diff = abs(tw - typed_w)
                    is_w_ok = w_diff < 5.0 or (typed_w > 0 and tw == 0)

                    invoice_mismatch = bool(
                        invoice_num_input and erp_info["invoice_number"] and
                        re.sub(r"\D", "", invoice_num_input).lstrip("0") !=
                        re.sub(r"\D", "", str(erp_info["invoice_number"])).lstrip("0")
                    )

                    order_mismatch = bool(order_num_input and _trip_reference(order_num_input) != _trip_reference(erp_info["order_number"]))
                    if order_mismatch:
                        status = "DIVERGENCIA_PEDIDO"
                        all_matched = False
                        divergent_count += 1
                    elif invoice_mismatch:
                        status = "DIVERGENCIA_NF"
                        all_matched = False
                        divergent_count += 1
                    elif not is_w_ok:
                        status = "DIVERGENCIA_PESO"
                        all_matched = False
                        divergent_count += 1
                    else:
                        status = "OK_ERP_NOVO"

                    doc_display = f"Ped {erp_info['order_number']}"
                    if erp_info["invoice_number"]:
                        doc_display += f" / NF {erp_info['invoice_number']}"

                    results.append({
                        "input": raw_num,
                        "order_id": None,
                        "erp_doc": doc_display,
                        "erp_client": erp_info["client_name"] or typed_c,
                        "typed_client": typed_c,
                        "typed_weight": typed_w,
                        "erp_weight": tw,
                        "invoice_mismatch": invoice_mismatch,
                        "status": status,
                        "is_open_order": False,
                        "current_status": "ERP",
                        "source": "ORACLE"
                    })
                    continue

                all_matched = False
                divergent_count += 1
                results.append({
                    "input": raw_num,
                    "order_id": None,
                    "erp_doc": "Não localizado no ERP/Base",
                    "erp_client": typed_c or "Consumidor Final",
                    "typed_client": typed_c,
                    "typed_weight": typed_w,
                    "erp_weight": 0.0,
                    "status": "NAO_LOCALIZADO",
                    "is_open_order": False,
                    "current_status": "Manual",
                    "source": "MANUAL"
                })
    except ValueError as exc:
        return handler.send_json({"ok": False, "message": str(exc)}, 409)
    except Exception:
        return handler.send_json({"ok": False, "message": "Não foi possível conferir os pedidos no ERP. Tente novamente."}, 503)
    finally:
        if ora_conn:
            try: ora_conn.close()
            except Exception: pass

    note = "A conferência foi realizada com sucesso. Revise as divergências acima antes de confirmar." if not all_matched else "Todos os itens bateram perfeitamente com os registros do ERP e banco local."
    if orders_to_baixa > 0:
        note += f" {orders_to_baixa} pedido(s) em andamento receberão baixa automática."

    return handler.send_json({
        "ok": True,
        "all_matched": all_matched,
        "divergent_count": divergent_count,
        "orders_to_baixa_count": orders_to_baixa,
        "items": results,
        "note": note
    })


def _expense_number(value, label, money=False):
    """Normaliza números JSON ou valores BR sem perder centavos."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        raise ValueError(f"Informe um valor válido para {label}.")
    raw = str(value).replace("R$", "").replace("\u00a0", "").replace(" ", "").strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif isinstance(value, str) and re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", raw):
        raw = raw.replace(".", "")
    try:
        number = Decimal(raw)
        if not number.is_finite() or number < 0:
            raise ValueError(f"{label} deve ser um valor válido, maior ou igual a zero.")
        if money:
            number = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        result = float(number)
        if not math.isfinite(result):
            raise ValueError(f"Informe um valor válido para {label}.")
        return result
    except (InvalidOperation, TypeError, OverflowError):
        raise ValueError(f"Informe um valor válido para {label}.") from None


def _normalize_trip_expenses(expenses):
    if not isinstance(expenses, dict):
        raise ValueError("Informe as despesas em formato válido.")
    result = {}
    for key, label in (("lunch", "almoço"), ("overnight", "diárias")):
        item = expenses.get(key) or {}
        if not isinstance(item, dict):
            raise ValueError(f"Informe os dados de {label} em formato válido.")
        qty = _expense_number(item.get("qty"), f"quantidade de {label}")
        unit = _expense_number(item.get("unit_val"), f"valor unitário de {label}", money=True)
        if (qty > 0) != (unit > 0):
            raise ValueError(f"Informe a quantidade e o valor unitário de {label} para calcular a despesa.")
        result[key] = {"qty": qty, "unit_val": unit}
    fuel, other = expenses.get("fuel") or {}, expenses.get("other") or {}
    if not isinstance(fuel, dict) or not isinstance(other, dict):
        raise ValueError("Informe combustível e outros gastos em formato válido.")
    result["fuel"] = {"litres": _expense_number(fuel.get("litres"), "litros de combustível"),
                      "total_val": _expense_number(fuel.get("total_val"), "combustível", money=True)}
    result["toll_val"] = _expense_number(expenses.get("toll_val"), "pedágio", money=True)
    result["other"] = {"desc": str(other.get("desc") or "").strip(),
                       "val": _expense_number(other.get("val"), "outros gastos", money=True)}
    return result


def _expense_product(qty, unit):
    return float((Decimal(str(qty)) * Decimal(str(unit))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def api_save_trip(handler, user):
    """Grava a viagem no banco SQLite criando rota, pedidos, itens de rota e despesas, dando baixa automática em pedidos existentes."""
    try:
        if hasattr(handler, "post_data"):
            data = handler.post_data()
        elif hasattr(handler, "_raw_post_body") and handler._raw_post_body:
            data = json.loads(handler._raw_post_body)
        else:
            length = int(handler.headers.get("content-length", 0))
            raw_body = handler.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            data = json.loads(raw_body)
        if isinstance(data, str):
            data = json.loads(data)
    except Exception as e:
        return handler.send_json({"ok": False, "message": f"Corpo JSON inválido: {e}"}, 400)

    driver_id = int(data.get("driver_id") or 0)
    vehicle_id = int(data.get("vehicle_id") or 0)
    data_saida = data.get("data_saida") or datetime.now().strftime("%Y-%m-%d")
    hora_saida = data.get("hora_saida") or "08:00"
    data_chegada = data.get("data_chegada") or data_saida
    hora_chegada = data.get("hora_chegada") or "18:00"
    km_start = float(data.get("km_start") or 0)
    km_end = float(data.get("km_end") or 0)
    km_total = float(data.get("km_total") or (km_end - km_start if km_end > km_start else 0))
    helper_name = data.get("helper_name") or ""
    destination = data.get("destination") or "Teófilo Otoni"
    orders_input = data.get("orders") or []
    try:
        expenses = _normalize_trip_expenses(data.get("expenses") or {})
    except ValueError as exc:
        return handler.send_json({"ok": False, "message": str(exc)}, 400)

    if not driver_id or not vehicle_id:
        return handler.send_json({"ok": False, "message": "Motorista e veículo são obrigatórios."}, 400)
    if not orders_input:
        return handler.send_json({"ok": False, "message": "Informe pelo menos um pedido ou nota fiscal."}, 400)
    for index, item in enumerate(orders_input, start=1):
        if not (str(item.get("order_number") or "").strip() or str(item.get("invoice_number") or "").strip() or str(item.get("item_number") or "").strip()):
            return handler.send_json({"ok": False, "message": f"Informe o número do pedido ou da NF na linha {index}."}, 400)
        try:
            if not math.isfinite(float(item.get("weight_kg") or 0)) or float(item.get("weight_kg") or 0) < 0:
                return handler.send_json({"ok": False, "message": f"O peso da linha {index} não pode ser negativo."}, 400)
        except (TypeError, ValueError):
            return handler.send_json({"ok": False, "message": f"Informe um peso válido na linha {index}."}, 400)

    ora_conn, ora_cur = _get_oracle_cursor()
    orders_given_baixa = []

    try:
        with handler.conn() as db:
            driver_row = db.execute("SELECT name FROM drivers WHERE id=?", (driver_id,)).fetchone()
            vehicle_row = db.execute("SELECT id FROM vehicles WHERE id=? AND active=1", (vehicle_id,)).fetchone()
            if not driver_row:
                return handler.send_json({"ok": False, "message": "O motorista selecionado não está ativo ou não existe."}, 400)
            if not vehicle_row:
                return handler.send_json({"ok": False, "message": "O veículo selecionado não está ativo ou não existe."}, 400)
            driver_name = driver_row["name"] if driver_row else "Motorista"

            # Valida os vínculos antes de criar a carga. Uma NF diferente da
            # cadastrada nunca pode sobrescrever ou baixar outro pedido.
            seen_order_ids = set()
            resolved_orders = []
            for index, item in enumerate(orders_input, start=1):
                order_num_input = (item.get("order_number") or "").strip()
                invoice_num_input = (item.get("invoice_number") or "").strip()
                lookup_num = order_num_input or invoice_num_input or (item.get("item_number") or "").strip()
                lookup_field = "order_number" if order_num_input else "invoice_number" if invoice_num_input else "any"
                existing, _, erp_info = find_order_in_db_or_oracle(db, lookup_num, ora_cur=ora_cur, query_field=lookup_field)
                comparison_status = "ERP confirmado" if erp_info and erp_info.get("metadata_verified") else "ERP não consultado" if ora_cur is None else "Não localizado no ERP"
                unmatched_candidate = None
                if not existing and not erp_info and order_num_input and invoice_num_input:
                    existing, _, erp_info = find_order_in_db_or_oracle(db, invoice_num_input, ora_cur=ora_cur, query_field="invoice_number")
                # A leitura é feita antes de qualquer escrita e reaproveitada na
                # transação. Campos ocultos enviados pelo navegador não são fonte.
                if existing and ora_cur:
                    metadata = trip_order_metadata.lookup_metadata(db, existing["order_number"], ora_cur, "order_number")
                    if metadata and trip_order_metadata.matches_client(db, existing, metadata):
                        erp_info = metadata
                        comparison_status = "ERP confirmado"
                    elif metadata:
                        unmatched_candidate = metadata
                        comparison_status = "Referência de outro cliente no ERP"
                if not existing and erp_info and not erp_info.get("metadata_verified"):
                    return handler.send_json({"ok": False, "message": f"Não foi possível confirmar os dados comerciais da linha {index} no ERP. Tente novamente antes de gravar."}, 503)
                if not existing and not erp_info and ora_cur is None:
                    from app_core import erp_connector
                    if erp_connector.get_erp_config().is_ready:
                        return handler.send_json({"ok": False, "message": "O ERP está indisponível. Aguarde a conexão para distinguir pedidos do ERP de lançamentos avulsos."}, 503)
                expected_order = (existing["order_number"] if existing else "") or (erp_info.get("order_number") if erp_info else "")
                if order_num_input and expected_order and _trip_reference(order_num_input) != _trip_reference(expected_order):
                    return handler.send_json({"ok": False, "message": f"O pedido e a NF da linha {index} pertencem a registros diferentes. Corrija antes de gravar."}, 409)
                expected_invoice = (existing["invoice_number"] if existing else "") or (erp_info.get("invoice_number") if erp_info else "")
                if invoice_num_input and expected_invoice:
                    entered = re.sub(r"\D", "", invoice_num_input).lstrip("0")
                    expected = re.sub(r"\D", "", str(expected_invoice)).lstrip("0")
                    if entered != expected:
                        return handler.send_json({"ok": False, "message": f"A NF informada na linha {index} não corresponde ao pedido localizado. Corrija a NF antes de gravar."}, 409)
                if existing:
                    if existing["id"] in seen_order_ids:
                        return handler.send_json({"ok": False, "message": f"O pedido {existing['order_number']} foi adicionado mais de uma vez."}, 409)
                    seen_order_ids.add(existing["id"])
                if erp_info and erp_info.get("metadata_verified"):
                    comparison_status = "ERP confirmado"
                resolved_orders.append((existing, erp_info, comparison_status, unmatched_candidate))

            display_date = datetime.strptime(data_saida, "%Y-%m-%d").strftime("%d/%m") if "-" in data_saida else data_saida
            route_name = f"Carga {display_date} - {driver_name}"

            total_weight = sum(float(o.get("weight_kg") or 0) for o in orders_input)

            # 1. Inserir ou atualizar a rota
            cur = db.execute("""
                INSERT INTO routes (
                    name, date, driver_id, vehicle_id, status, route_name, total_weight, declared_weight,
                    capacity, notes, km_start, km_end, km_total, departure_time, arrival_time,
                    helper_name, destination, expenses_json, created_at, updated_at, version
                ) VALUES (
                    ?, ?, ?, ?, 'Acertada', 'Mista', ?, ?,
                    12000.0, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, datetime('now'), datetime('now'), 1
                )
            """, (
                route_name, data_saida, driver_id, vehicle_id, total_weight, total_weight,
                f"Registro Web de Viagem - Destino: {destination}",
                km_start, km_end, km_total, f"{data_saida} {hora_saida}", f"{data_chegada} {hora_chegada}",
                helper_name, destination, json.dumps(expenses, ensure_ascii=False)
            ))
            route_id = cur.lastrowid

            # 2. Inserir despesas estruturadas em trip_expenses
            expense_items = []
            lunch = expenses.get("lunch") or {}
            if lunch.get("qty", 0) > 0 and lunch.get("unit_val", 0) > 0:
                expense_items.append(("Almoço", lunch["qty"], lunch["unit_val"], _expense_product(lunch["qty"], lunch["unit_val"]), "Refeição em viagem"))
            overnight = expenses.get("overnight") or {}
            if overnight.get("qty", 0) > 0 and overnight.get("unit_val", 0) > 0:
                expense_items.append(("Diária de Pernoite", overnight["qty"], overnight["unit_val"], _expense_product(overnight["qty"], overnight["unit_val"]), "Pernoite do motorista/ajudante"))
            fuel = expenses.get("fuel") or {}
            if fuel.get("total_val", 0) > 0:
                expense_items.append(("Combustível", fuel.get("litres", 0), 0, fuel["total_val"], f"{fuel.get('litres', 0)} litros abastecidos"))
            toll = float(expenses.get("toll_val") or 0)
            if toll > 0:
                expense_items.append(("Pedágio", 1, toll, toll, "Tarifas de pedágio"))
            other = expenses.get("other") or {}
            if other.get("val", 0) > 0:
                expense_items.append(("Outros", 1, other["val"], other["val"], other.get("desc") or "Despesas diversas"))

            for e_type, q, uv, tv, notes_exp in expense_items:
                db.execute("""
                    INSERT INTO trip_expenses (route_id, expense_type, quantity, unit_value, total_value, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """, (route_id, e_type, q, uv, tv, notes_exp))

            # 3. Vincular ou criar pedidos, dar baixa e associar em route_orders
            u_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
            seq = 1
            audit_lines = []
            for it, (existing, erp_info, comparison_status, unmatched_candidate) in zip(orders_input, resolved_orders):
                order_num_input = (it.get("order_number") or "").strip()
                invoice_num_input = (it.get("invoice_number") or "").strip()
                num = (order_num_input or invoice_num_input or it.get("item_number") or "").strip()
                w = float(it.get("weight_kg") or 0)
                c_name = it.get("client_name", "").strip() or "Consumidor Final"
                pm = it.get("payment_type") or "Boleto"
                deliv_d = it.get("delivery_date") or data_chegada
                notes = it.get("notes") or ""

                if existing:
                    oid = existing["id"]
                    old_status = existing["status"] or "Pendente"
                    is_open = old_status not in ("Acertado", "Cancelado")

                    inv_to_save = invoice_num_input or existing["invoice_number"] or (erp_info.get("invoice_number") if erp_info else "")

                    # 3.1. Dá baixa automática no pedido
                    db.execute("""
                        UPDATE orders
                        SET driver_id=?, vehicle_id=?, status='Acertado', delivered_at=COALESCE(delivered_at, ?),
                            payment_method=COALESCE(NULLIF(payment_method, ''), ?),
                            invoice_number=COALESCE(NULLIF(invoice_number, ''), ?),
                            final_notes=COALESCE(NULLIF(final_notes, ''), ?),
                            updated_at=datetime('now'), version=COALESCE(version, 1) + 1
                        WHERE id=?
                    """, (driver_id, vehicle_id, deliv_d, pm, inv_to_save, notes, oid))

                    if erp_info and erp_info.get("metadata_verified"):
                        _update_trip_commercial_metadata(db, oid, erp_info)

                    # 3.2. Histórico de auditoria
                    _log_order_history(
                        db,
                        order_id=oid,
                        user_id=u_id,
                        old_status=old_status,
                        new_status="Acertado",
                        action="Baixa por Registro de Viagens",
                        notes=f"Carga #{route_id} ({route_name}) - Motorista: {driver_name}"
                    )

                    # 3.3. Atualizar registros em aberto de rotas anteriores para não ficarem pendentes
                    db.execute("""
                        UPDATE route_orders
                        SET status='Entregue'
                        WHERE order_id=? AND route_id != ? AND status NOT IN ('Entregue', 'Cancelado')
                    """, (oid, route_id))

                    if is_open:
                        orders_given_baixa.append({
                            "order_id": oid,
                            "order_number": existing["order_number"],
                            "invoice_number": inv_to_save,
                            "client_name": existing["client_name"],
                            "old_status": old_status
                        })
                else:
                    if erp_info:
                        c_name = erp_info["client_name"] or c_name
                        ped_num = order_num_input or erp_info["order_number"]
                        nf_num = invoice_num_input or erp_info["invoice_number"] or ""
                        total_val = erp_info["total_value"]
                        if w == 0:
                            w = erp_info["weight_kg"]
                        if not notes:
                            notes = erp_info["products_summary"]
                    else:
                        ped_num = order_num_input or num
                        nf_num = invoice_num_input or (num if not order_num_input else "")
                        total_val = 0.0

                    metadata = erp_info or {}
                    c_row = None
                    if metadata.get("client_code"):
                        c_row = db.execute("SELECT id FROM clients WHERE customer_code=? AND name=? ORDER BY id LIMIT 1",
                                           (metadata["client_code"], c_name)).fetchone()
                    if not c_row:
                        c_row = db.execute("SELECT id FROM clients WHERE name=? AND (COALESCE(customer_code,'')='' OR customer_code=?) ORDER BY id LIMIT 1",
                                           (c_name, metadata.get("client_code") or "")).fetchone()
                    if c_row:
                        cid = c_row["id"]
                    else:
                        cur_c = db.execute("""INSERT INTO clients
                            (name,customer_code,city,address,neighborhood,phone,active,created_at,updated_at)
                            VALUES (?,?,?,?,?,?,1,datetime('now'),datetime('now'))""",
                            (c_name, metadata.get("client_code"), metadata.get("city") or "",
                             metadata.get("delivery_address") or "", metadata.get("neighborhood") or "", metadata.get("phone") or ""))
                        cid = cur_c.lastrowid

                    cur_o = db.execute("""
                        INSERT INTO orders (
                            order_number, invoice_number, client_id, status, sale_date, expected_delivery_date,
                            total_value, weight_kg, payment_method, delivery_address, city, uf, driver_id, vehicle_id,
                            invoiced_at, route_name, seller_name,
                            delivered_at, final_notes, notes, created_at, updated_at, version
                        ) VALUES (
                            ?, ?, ?, 'Acertado', ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?,
                            ?, ?, ?, datetime('now'), datetime('now'), 1
                        )
                    """, (ped_num, nf_num, cid, metadata.get("sale_date") or None,
                          metadata.get("expected_delivery_date") or None, total_val, w, pm,
                          metadata.get("delivery_address") or "", metadata.get("city") or "", metadata.get("uf") or "",
                          driver_id, vehicle_id, metadata.get("invoiced_at") or None,
                          metadata.get("route_name") or "", metadata.get("seller_name") or "", deliv_d, notes, notes))
                    oid = cur_o.lastrowid

                    _log_order_history(
                        db,
                        order_id=oid,
                        user_id=u_id,
                        old_status="Novo (Registro Viagens)",
                        new_status="Acertado",
                        action="Criação e Baixa por Registro de Viagens",
                        notes=f"Carga #{route_id} ({route_name}) - Motorista: {driver_name}"
                    )

                    if metadata.get("items"):
                        for product in metadata["items"]:
                            qty = float(product.get("quantity") or 0)
                            price = float(product.get("unit_price") or 0)
                            db.execute("""INSERT INTO order_items
                                (order_id,product_code,product_name,quantity,unit,weight_kg,unit_price,total_value,status,notes)
                                VALUES (?,?,?,?,?,?,?,?,'Entregue','ERP - Registro de Viagens')""",
                                (oid, product.get("product_code"), product.get("product_name") or "Produto", qty,
                                 product.get("unit") or "UN", product.get("weight_kg") or 0, price, round(qty * price, 2)))
                    elif notes:
                        db.execute("""
                            INSERT INTO order_items (order_id, product_code, product_name, quantity, unit, weight_kg, notes)
                            VALUES (?, '0', ?, 1, 'un', ?, ?)
                        """, (oid, notes[:100], w, "Lançamento Registro de Viagens"))

                ex_ro = db.execute("SELECT id FROM route_orders WHERE route_id=? AND order_id=?", (route_id, oid)).fetchone()
                if not ex_ro:
                    db.execute("""
                        INSERT INTO route_orders (route_id, order_id, delivery_order, status)
                        VALUES (?, ?, ?, 'Entregue')
                    """, (route_id, oid, seq))
                else:
                    db.execute("UPDATE route_orders SET status='Entregue', delivery_order=? WHERE id=?", (seq, ex_ro["id"]))
                audit_lines.append({"order_id": oid, "input": dict(it), "erp": erp_info,
                    "comparison_status": comparison_status, "unmatched_erp_candidate": unmatched_candidate,
                    "local_before": dict(existing) if existing else None})
                seq += 1

            audit_id = trip_audit.record_trip(db, route_id, user, data, expenses, audit_lines)
            db.commit()
    except ValueError as exc:
        return handler.send_json({"ok": False, "message": str(exc)}, 409)
    except Exception:
        return handler.send_json({"ok": False, "message": "Não foi possível concluir a viagem. Confira a conexão com o ERP e tente novamente; nenhuma carga foi gravada."}, 503)
    finally:
        if ora_conn:
            try: ora_conn.close()
            except Exception: pass

    baixa_count = len(orders_given_baixa)
    msg = f"Viagem registrada com sucesso na Carga #{route_id}!"
    if baixa_count > 0:
        msg += f" {baixa_count} pedido(s) da Lista de Pedidos receberam baixa automática ('Acertado')."

    return handler.send_json({
        "ok": True,
        "route_id": route_id,
        "audit_id": audit_id,
        "orders_given_baixa_count": baixa_count,
        "orders_given_baixa": orders_given_baixa,
        "message": msg
    })


def _update_trip_commercial_metadata(db, order_id, metadata):
    """Corrige dados comerciais confirmados sem tocar na entrega ou no acerto."""
    fields = ("sale_date", "invoiced_at", "expected_delivery_date", "city", "uf",
              "route_name", "delivery_address", "seller_name")
    values = {key: metadata[key] for key in fields if metadata.get(key)}
    if values:
        assignments = ",".join(f"{key}=?" for key in values)
        db.execute(f"UPDATE orders SET {assignments}, updated_at=datetime('now'), version=COALESCE(version,1)+1 WHERE id=?",
                   (*values.values(), order_id))


def generate_trip_sheet_pdf(handler, user, route_id: int):
    """Gera o espelho PDF oficial da folha de 'REGISTRO DE VIAGENS' para impressão e acerto."""
    with handler.conn() as db:
        r = db.execute("""
            SELECT r.*, d.name driver_name, v.name vehicle_name, v.plate vehicle_plate
            FROM routes r
            LEFT JOIN drivers d ON d.id=r.driver_id
            LEFT JOIN vehicles v ON v.id=r.vehicle_id
            WHERE r.id=?
        """, (route_id,)).fetchone()
        if not r:
            if hasattr(handler, "fail") and callable(getattr(handler, "fail", None)):
                return handler.fail(user, "Não encontrada", "Carga não encontrada.", 404)
            return handler.send_json({"ok": False, "message": "Carga não encontrada."}, 404)

        orders = db.execute("""
            SELECT o.*, c.name client_name, ro.delivery_order
            FROM route_orders ro
            JOIN orders o ON o.id=ro.order_id
            LEFT JOIN clients c ON c.id=o.client_id
            WHERE ro.route_id=?
            ORDER BY ro.delivery_order, o.id
        """, (route_id,)).fetchall()

        expenses = db.execute("""
            SELECT * FROM trip_expenses WHERE route_id=? ORDER BY id
        """, (route_id,)).fetchall()
        original_trip = trip_audit.load_trip(db, route_id)

    if original_trip:
        current_route = dict(r)
        current_route.update(original_trip["route"])
        current_route["driver_name"] = (original_trip.get("driver") or {}).get("name")
        current_route["vehicle_name"] = (original_trip.get("vehicle") or {}).get("name")
        current_route["vehicle_plate"] = (original_trip.get("vehicle") or {}).get("plate")
        r = current_route
        orders = []
        for entry in original_trip["entries"]:
            order = dict(entry["local_after"])
            typed = entry["input"]
            order["client_name"] = (entry.get("client") or {}).get("name") or typed.get("client_name")
            order["weight_kg"] = entry["entered_weight_kg"] if entry["entered_weight_kg"] is not None else entry["saved_weight_kg"]
            order["payment_method"] = typed.get("payment_type") or order.get("payment_method")
            order["delivered_at"] = typed.get("delivery_date") or order.get("delivered_at")
            order["notes"] = typed.get("notes") or order.get("notes")
            orders.append(order)
        expenses = original_trip["expenses"]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()

    primary_color = colors.HexColor("#166534")
    header_bg = colors.HexColor("#f0fdf4")
    border_color = colors.HexColor("#cbd5e1")

    title_style = ParagraphStyle('DocTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=16, leading=20, textColor=primary_color, alignment=1)
    subtitle_style = ParagraphStyle('DocSubtitle', parent=styles['Normal'], fontName='Helvetica', fontSize=9, leading=12, textColor=colors.HexColor("#475569"), alignment=1)
    cell_head = ParagraphStyle('CellHead', fontName='Helvetica-Bold', fontSize=8, leading=10, textColor=colors.white, alignment=1)
    cell_body = ParagraphStyle('CellBody', fontName='Helvetica', fontSize=7.5, leading=9.5, textColor=colors.HexColor("#1e293b"))
    cell_bold = ParagraphStyle('CellBold', parent=cell_body, fontName='Helvetica-Bold')
    cell_center = ParagraphStyle('CellCenter', parent=cell_body, alignment=1)

    elements = []
    elements.append(Paragraph("CASA DO CAMPO - LOGÍSTICA & DISTRIBUIÇÃO", title_style))
    elements.append(Paragraph(f"ESPELHO DE REGISTRO DE VIAGEM — CARGA #{r['id']} ({r['name']})", subtitle_style))
    elements.append(Spacer(1, 10))

    def weight_text(value):
        return "Não disponível" if value is None else f"{value:,.3f}".rstrip('0').rstrip('.').replace(',', 'X').replace('.', ',').replace('X', '.')
    def money_text(value):
        return f"R$ {float(value or 0):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

    dep_time = r['departure_time'] or (r['date'] or '')
    arr_time = r['arrival_time'] or (r['date'] or '')
    km_start_str = weight_text(r['km_start']) if r['km_start'] else "—"
    km_end_str = weight_text(r['km_end']) if r['km_end'] else "—"
    km_tot_str = weight_text(r['km_total']) if r['km_total'] else "—"

    head_data = [
        [
            Paragraph("<b>DATA/HORA SAÍDA:</b>", cell_bold), Paragraph(str(dep_time), cell_body),
            Paragraph("<b>DATA/HORA CHEGADA:</b>", cell_bold), Paragraph(str(arr_time), cell_body),
            Paragraph("<b>DESTINO:</b>", cell_bold), Paragraph(str(r['destination'] or 'T.O.'), cell_body)
        ],
        [
            Paragraph("<b>MOTORISTA:</b>", cell_bold), Paragraph(str(r['driver_name'] or '—'), cell_body),
            Paragraph("<b>AJUDANTE:</b>", cell_bold), Paragraph(str(r['helper_name'] or '—'), cell_body),
            Paragraph("<b>CAMINHÃO / PLACA:</b>", cell_bold), Paragraph(f"{r['vehicle_name'] or ''} {r['vehicle_plate'] or ''}", cell_body)
        ],
        [
            Paragraph("<b>KM INÍCIO:</b>", cell_bold), Paragraph(km_start_str, cell_body),
            Paragraph("<b>KM FINAL:</b>", cell_bold), Paragraph(km_end_str, cell_body),
            Paragraph("<b>KM RODADOS:</b>", cell_bold), Paragraph(f"<b>{km_tot_str} km</b>", cell_body)
        ]
    ]
    t_head = Table(head_data, colWidths=[doc.width*w/750 for w in [110, 140, 120, 140, 110, 130]])
    t_head.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), header_bg),
        ('BOX', (0,0), (-1,-1), 1, primary_color),
        ('INNERGRID', (0,0), (-1,-1), 0.5, border_color),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    elements.append(t_head)
    elements.append(Spacer(1, 10))

    elements.append(Paragraph("<b>ENTREGAS REALIZADAS NA VIAGEM:</b>", ParagraphStyle('H4', parent=styles['Normal'], fontSize=9.5, fontName='Helvetica-Bold', textColor=primary_color)))
    elements.append(Spacer(1, 4))

    table_orders = [
        [
            Paragraph("#", cell_head),
            Paragraph("NF / Pedido", cell_head),
            Paragraph("Cliente", cell_head),
            Paragraph("Pagamento", cell_head),
            Paragraph("Data Entrega", cell_head),
            Paragraph("Peso (KG)", cell_head),
            Paragraph("Observações / Produtos", cell_head),
            Paragraph("Status", cell_head)
        ]
    ]

    total_w = 0
    for idx, o in enumerate(orders, 1):
        w = float(o['weight_kg'] or 0)
        total_w += w
        doc_str = f"{o['invoice_number'] or o['order_number']}"
        if o['invoice_number'] and o['order_number'] and o['invoice_number'] != o['order_number']:
            doc_str += f" (Ped {o['order_number']})"
        table_orders.append([
            Paragraph(str(idx), cell_center),
            Paragraph(f"<b>{doc_str}</b>", cell_body),
            Paragraph(_esc(o['client_name'] or 'Consumidor'), cell_body),
            Paragraph(_esc(o['payment_method'] or 'Boleto'), cell_center),
            Paragraph(str(o['delivered_at'] or r['date'] or ''), cell_center),
            Paragraph(f"{weight_text(w)} kg", cell_center),
            Paragraph(_esc(o['notes'] or o['final_notes'] or '—'), cell_body),
            Paragraph("<font color='#166534'><b>Entregue</b></font>", cell_center)
        ])

    t_orders = Table(table_orders, colWidths=[doc.width*w/750 for w in [20, 100, 180, 80, 70, 70, 170, 60]], repeatRows=1)
    t_orders.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), primary_color),
        ('BOX', (0,0), (-1,-1), 1, primary_color),
        ('INNERGRID', (0,0), (-1,-1), 0.3, border_color),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))
    elements.append(t_orders)
    elements.append(Spacer(1, 8))

    if original_trip:
        elements.append(Paragraph("<b>CONFERÊNCIA DE PESO REGISTRADA NA GRAVAÇÃO</b>", cell_bold))
        comparison = [[Paragraph(label, cell_head) for label in (
            "Pedido", "Peso digitado (kg)", "Peso ERP (kg)", "Digitado - ERP (kg)", "Consulta")]]
        for entry in original_trip['entries']:
            comparison.append([Paragraph(_esc(value), cell_body) for value in (
                entry['order_number'], weight_text(entry['entered_weight_kg']),weight_text(entry['erp_weight_kg']),
                weight_text(entry['weight_difference_kg']), entry['comparison_status'])])
        comparison_table = Table(comparison, colWidths=[doc.width*w for w in (.15,.19,.19,.21,.26)], repeatRows=1)
        comparison_table.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),primary_color),('VALIGN',(0,0),(-1,-1),'TOP'),
            ('INNERGRID',(0,0),(-1,-1),.3,border_color),('BOX',(0,0),(-1,-1),.5,border_color),
            ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
        elements.append(comparison_table)
        elements.append(Paragraph("Histórico original preservado em "+_esc(original_trip['recorded_at'])+". Dados posteriores do cadastro ou ERP não substituem os valores acima.", cell_body))
        elements.append(Spacer(1,8))

    exp_text_parts = []
    total_exp = 0
    for exp in expenses:
        total_exp += float(exp['total_value'] or 0)
        exp_text_parts.append(f"• <b>{_esc(exp['expense_type'])}:</b> {money_text(exp['total_value'])} ({_esc(exp['notes'] or '')})")
    
    exp_summary_str = "<br/>".join(exp_text_parts) if exp_text_parts else "Nenhuma despesa registrada nesta viagem."

    sig_cell_elements = [
        Paragraph(f"<b>PESO TOTAL CARREGADO:</b><br/><font size=12><b>{weight_text(total_w)} kg</b></font><br/>", cell_center)
    ]
    if r['driver_signature'] and 'base64' in str(r['driver_signature']):
        try:
            import base64
            sig_s = str(r['driver_signature'])
            if ',' in sig_s:
                sig_s = sig_s.split(',', 1)[1]
            sig_bytes = base64.b64decode(sig_s)
            from reportlab.platypus import Image as RLImage
            sig_img = RLImage(io.BytesIO(sig_bytes), width=130, height=45)
            sig_cell_elements.append(sig_img)
        except Exception:
            sig_cell_elements.append(Paragraph("<br/>____________________________________________<br/>", cell_center))
    else:
        sig_cell_elements.append(Paragraph("<br/>____________________________________________<br/>", cell_center))

    sig_cell_elements.append(Paragraph(f"<b>Assinatura do Motorista ({r['driver_name'] or ''})</b>", cell_center))

    bot_table_data = [
        [
            Paragraph("<b>DESPESAS DA VIAGEM:</b><br/>" + exp_summary_str + f"<br/><b>TOTAL DESPESAS: {money_text(total_exp)}</b>", cell_body),
            sig_cell_elements
        ]
    ]
    t_bot = Table(bot_table_data, colWidths=[doc.width*400/750, doc.width*350/750])
    t_bot.setStyle(TableStyle([
        ('BOX', (0,0), (-1,-1), 1, primary_color),
        ('INNERGRID', (0,0), (-1,-1), 0.5, border_color),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
    ]))
    elements.append(t_bot)

    doc.build(elements)
    pdf_bytes = buf.getvalue()

    filename = f"Registro_Viagem_Carga_{route_id}.pdf"
    handler.send_response(200)
    handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Disposition", f"inline; filename=\"{filename}\"")
    handler.send_header("Content-Length", str(len(pdf_bytes)))
    handler.end_headers()
    handler.wfile.write(pdf_bytes)


def serve_odometer_photo(handler, user, route_id: int, photo_type: str) -> bool:
    """Serve a foto do odômetro inicial ou final para conferência no sistema e no app."""
    import base64
    with handler.conn() as db:
        row = db.execute(
            "SELECT image_data, mime_type FROM trip_photos WHERE route_id=? AND photo_type=? ORDER BY id DESC LIMIT 1",
            (route_id, photo_type)
        ).fetchone()

        data_val = None
        mime = "image/jpeg"
        if row and row["image_data"]:
            data_val = row["image_data"]
            if row["mime_type"]:
                mime = row["mime_type"]
        else:
            col = "odometer_start_photo" if photo_type == "start" else "odometer_end_photo"
            r = db.execute(f"SELECT {col} FROM routes WHERE id=?", (route_id,)).fetchone()
            if r and r[col] and not str(r[col]).startswith("/routes/"):
                data_val = r[col]

    if not data_val:
        handler.send_error(404, "Foto do odômetro não encontrada.")
        return True

    if isinstance(data_val, (bytes, bytearray)):
        content = bytes(data_val)
    else:
        s = str(data_val or "")
        if "," in s:
            header, s = s.split(",", 1)
            if "png" in header:
                mime = "image/png"
            elif "jpeg" in header or "jpg" in header:
                mime = "image/jpeg"
            elif "webp" in header:
                mime = "image/webp"
        try:
            content = base64.b64decode(s)
        except Exception:
            handler.send_error(404, "Formato de imagem inválido.")
            return True

    handler.send_response(200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header("Cache-Control", "private, max-age=86400")
    handler.end_headers()
    handler.wfile.write(content)
    return True

