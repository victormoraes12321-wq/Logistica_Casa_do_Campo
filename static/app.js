document.addEventListener('DOMContentLoaded', () => {
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const getCookie = name => {
    const key = `${name}=`;
    for (const raw of document.cookie.split(';')) {
      const item = raw.trim();
      if (item.startsWith(key)) return decodeURIComponent(item.slice(key.length));
    }
    return '';
  };
  const csrfToken = getCookie('csrf_token');
  const ensureCsrfInput = form => {
    if (!form) return;
    const method = (form.getAttribute('method') || '').toLowerCase();
    if (method !== 'post' || !csrfToken) return;
    let input = form.querySelector('input[name="_csrf"]');
    if (!input) {
      input = document.createElement('input');
      input.type = 'hidden';
      input.name = '_csrf';
      form.prepend(input);
    }
    input.value = csrfToken;
  };

  // Active sidebar state
  const path = window.location.pathname.replace(/\/$/, '') || '/dashboard';
  
  // Botão de impressão (substitui onclick inline)
  const printBtn = $('#btnPrintPage');
  if (printBtn) {
    printBtn.addEventListener('click', () => window.print());
  }

  $$('.sidebar nav a').forEach(a => {
    const href = (a.getAttribute('href') || '').replace(/\/$/, '') || '/dashboard';
    if (href === path || (href !== '/dashboard' && path.startsWith(href + '/'))) a.classList.add('active');
  });
  const sidebar = $('.sidebar');
  if (sidebar) {
    const sideKey = 'logistica_sidebar_scroll_top';
    const savedRaw = sessionStorage.getItem(sideKey);
    const saved = savedRaw === null ? null : Number(savedRaw);
    if (saved !== null && Number.isFinite(saved)) sidebar.scrollTop = saved;
    const rememberSidebar = () => sessionStorage.setItem(sideKey, String(sidebar.scrollTop));
    sidebar.addEventListener('scroll', rememberSidebar);
    window.addEventListener('beforeunload', rememberSidebar);
    $$('.sidebar nav a').forEach(link => link.addEventListener('click', rememberSidebar));
    if ((saved === null || !Number.isFinite(saved)) && $('.sidebar nav a.active')) {
      $('.sidebar nav a.active').scrollIntoView({ block: 'nearest', behavior: 'auto' });
    }
  }

  // Help popover: close when clicking outside
  document.addEventListener('click', e => {
    $$('details.help-popover[open]').forEach(pop => {
      if (!pop.contains(e.target)) pop.removeAttribute('open');
    });
  });

  // Inline help per data card/rectangle
  const normalizeHelpKey = value => String(value || '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9\s/-]/g, ' ')
    .replace(/\d+/g, '') // remove numbers/digits to handle top alert-chips like "2 vencidos", "23 sem NF"
    .replace(/\s+/g, ' ')
    .trim();

  const CARD_HELP_TEXT = {
    'pedidos no periodo': 'Total de pedidos considerados no filtro atual.',
    'entregas dentro do prazo': 'Quantidade de pedidos finalizados sem estourar o SLA.',
    'entregas fora do prazo': 'Quantidade de pedidos finalizados com SLA violado.',
    'pedidos em risco': 'Pedidos perto de vencer o prazo e que exigem atencao imediata.',
    'pedidos atrasados': 'Pedidos com prazo vencido e sem finalizacao.',
    'em rota proximos sla': 'Pedidos em rota que estao perto do limite do SLA.',
    'faturados sem saida': 'Pedidos faturados que ainda nao foram despachados em carga.',
    'pedidos com problema': 'Pedidos finalizados com ocorrencia/problema registrado.',
    'media venda-entrega': 'Tempo medio entre data de venda e data de entrega final.',
    'impacto de feriados': 'Dias adicionais causados por feriados no calculo de prazo.',
    'vendas abertas': 'Pedidos em status Venda aguardando faturamento.',
    'faturados aguardando saida': 'Pedidos faturados ainda nao enviados para entrega.',
    'saiu para entrega': 'Pedidos ja despachados e aguardando acerto final.',
    'pedidos entregues': 'Pedidos finalizados como Acertado.',
    'fora do sla': 'Pedidos abertos que ultrapassaram o prazo operacional.',
    'risco de sla': 'Pedidos proximos do vencimento do prazo.',
    'sem nf': 'Pedidos sem numero de nota fiscal preenchido.',
    'sem carga': 'Pedidos faturados que ainda nao entraram in carga.',
    'sem acerto': 'Pedidos em rota que ainda nao tiveram baixa final.',
    'sla critico': 'Pedidos vencidos ou vencendo em ate 5 dias.',
    'cadastros com risco': 'Cadastros com dados faltando ou inconsistentes.',
    'vencidos': 'Pedidos com prazo vencido e sem finalizacao.',
    'vencido': 'Pedidos com prazo vencido e sem finalizacao.',
    'em risco': 'Pedidos perto de vencer o prazo e que exigem atencao imediata.',
    'risco de prazo': 'Pedidos abertos que estao proximos do limite do SLA e precisam de atencao.',
    'sem faturar': 'Pedidos em status Venda aguardando faturamento e emissao de nota fiscal.',
    'faturados sem carga': 'Pedidos ja faturados mas que ainda nao foram vinculados a nenhuma carga/rota.',
    'em rota': 'Pedidos despachados em cargas ativas que estao em transito para entrega.',
    'acertados': 'Pedidos cujas entregas foram concluidas e confirmadas financeiramente.'
  };

  const CARD_HELP_SELECTORS = [
    '.cards .card',
    '.route-kpi',
    '.route-card',
    '.closed-card',
    '.invoice-card',
    '.settlement-card',
    '.sla-health-grid > div',
    '.kanban-card',
    '.kanban-lane',
    '.alert-central .alert-chip'
  ];

  const cleanText = value => String(value || '').replace(/\s+/g, ' ').trim();
  const escapeHtml = value => String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

  function getFirstTextBySelectors(root, selectors) {
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      const txt = cleanText(node?.textContent || '');
      if (txt && txt !== '?') return txt;
    }
    return '';
  }

  function getCardTitle(card) {
    const title = getFirstTextBySelectors(card, [
      '.metric-title',
      '.kpi-title',
      'small',
      'h2',
      'h3',
      'h4',
      'legend'
    ]);
    if (title) return title;
    const alt = getFirstTextBySelectors(card, ['b', 'strong', 'span', 'p']);
    if (alt) return alt.length <= 80 ? alt : alt.slice(0, 80);
    const selfText = cleanText(card.textContent || '');
    if (selfText && selfText !== '?') return selfText.length <= 80 ? selfText : selfText.slice(0, 80);
    return '';
  }

  function getCardValue(card) {
    return getFirstTextBySelectors(card, [
      'strong',
      '.kpi-value',
      '.metric-value',
      'b'
    ]);
  }

  function getCardDetail(card, title, value) {
    const nodes = card.querySelectorAll('span, p, small, .muted');
    for (const node of nodes) {
      const txt = cleanText(node.textContent || '');
      if (!txt) continue;
      if (txt === title || txt === value || txt === '?') continue;
      if (txt.length < 4) continue;
      return txt;
    }
    return '';
  }

  function buildCardHelpHtml(title, value, detail) {
    const key = normalizeHelpKey(title);
    const safeTitle = escapeHtml(title);
    const safeValue = escapeHtml(value);
    const safeDetail = escapeHtml(detail);
    const mapped = CARD_HELP_TEXT[key] || `Este indicador resume: ${title}.`;
    const safeMapped = escapeHtml(mapped);
    const valueLine = safeValue ? `<p><b>Valor atual:</b> ${safeValue}</p>` : '';
    const detailLine = safeDetail ? `<p><b>Leitura:</b> ${safeDetail}</p>` : '';
    return `<div class="help-content metric-help-content"><h3>${safeTitle}</h3><p>${safeMapped}</p>${valueLine}${detailLine}</div>`;
  }

  function placeMetricHelp(details) {
    if (!details || !details.open) return;
    const content = details.querySelector('.metric-help-content');
    if (!content) return;

    details.classList.remove('metric-help-left');
    const main = $('.main');
    const safeLeft = main ? Math.max(8, Math.round(main.getBoundingClientRect().left) + 8) : 8;
    const viewportRight = Math.max(16, window.innerWidth - 12);

    let rect = content.getBoundingClientRect();
    if (rect.left < safeLeft) {
      details.classList.add('metric-help-left');
      rect = content.getBoundingClientRect();
      if (rect.right > viewportRight) details.classList.remove('metric-help-left');
    }
  }

  function ensureMetricHelp(card) {
    if (!card || card.dataset.metricHelpReady === '1') return;
    if (card.closest('.help-content')) return;

    const title = getCardTitle(card);
    if (!title) return;
    const value = getCardValue(card);
    const detail = getCardDetail(card, title, value);

    const details = document.createElement('details');
    details.className = 'help-popover metric-help-popover';

    const summary = document.createElement('summary');
    summary.className = 'help-trigger metric-help-trigger';
    summary.title = `Ajuda de: ${title}`;
    summary.setAttribute('aria-label', `Ajuda de: ${title}`);
    summary.textContent = '?';
    details.appendChild(summary);

    details.insertAdjacentHTML('beforeend', buildCardHelpHtml(title, value, detail));

    if (card.tagName === 'A') {
      let wrap = card.parentElement;
      if (!wrap || !wrap.classList.contains('metric-help-wrap')) {
        wrap = document.createElement('div');
        wrap.className = 'metric-help-wrap';
        card.parentNode.insertBefore(wrap, card);
        wrap.appendChild(card);
      }
      wrap.classList.add('metric-help-target');
      card.classList.add('has-inline-help');
      wrap.appendChild(details);
    } else {
      card.classList.add('metric-help-target', 'has-inline-help');
      card.prepend(details);
    }

    details.addEventListener('toggle', () => {
      if (details.open) placeMetricHelp(details);
    });

    card.dataset.metricHelpReady = '1';
  }

  function applyMetricHelp() {
    CARD_HELP_SELECTORS.forEach(selector => {
      $$(selector).forEach(card => ensureMetricHelp(card));
    });
  }
  applyMetricHelp();

  window.addEventListener('resize', () => {
    $$('.metric-help-popover[open]').forEach(pop => placeMetricHelp(pop));
  });

  // Mobile menu button without changing backend templates
  const topbar = $('.topbar');
  if (topbar) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn ghost small mobile-menu-btn';
    btn.textContent = '☰ Menu';
    btn.style.display = 'none';
    btn.addEventListener('click', () => document.body.classList.toggle('menu-open'));
    topbar.prepend(btn);
  }

  // Global quick search
  const quickSearch = $('.search input');
  document.addEventListener('keydown', e => {
    if (e.ctrlKey && e.key.toLowerCase() === 'k') { e.preventDefault(); quickSearch?.focus(); }
    if (e.key === 'Escape') document.body.classList.remove('menu-open');
  });

  // Confirm risky actions
  $$('form').forEach(form => {
    ensureCsrfInput(form);
    form.addEventListener('submit', e => {
      ensureCsrfInput(form);
      let alreadyConfirmed = false;
      if (form.classList.contains('needs-confirm')) {
        const msg = form.dataset.confirmText || 'Confirma esta ação?';
        if (!confirm(msg)) { e.preventDefault(); return; }
        alreadyConfirmed = true;
      }
      const danger = form.querySelector('.danger-btn');
      const deleting = /cancelar|excluir|remover|problema|desativar/i.test(form.textContent || '');
      if (!alreadyConfirmed && (danger || deleting) && !confirm('Confirma esta ação?')) e.preventDefault();
    });
  });

  // SLA preview em dias corridos. Backend continua como fonte da verdade.
  const holidays = new Set();
  $$('[data-holiday]').forEach(x => holidays.add(x.dataset.holiday));
  function addCalendarDays(dateStr, days = 15) {
    if (!dateStr) return '';
    const [y, m, d] = dateStr.split('-').map(Number);
    const dt = new Date(y, m - 1, d);
    dt.setDate(dt.getDate() + Number(days || 15));
    return dt.toISOString().slice(0, 10);
  }
  const saleDate = $('#saleDate');
  const deadlineDate = $('#deadlineDate');
  saleDate?.addEventListener('change', () => { if (deadlineDate) deadlineDate.value = addCalendarDays(saleDate.value, 15); });

  function parseLocaleNumber(raw) {
    const clean = String(raw || '')
      .replace(/\u00a0/g, ' ')
      .replace(/R\$/gi, '')
      .replace(/\s+/g, '')
      .replace(/[^\d,.\-]/g, '');
    if (!clean) return 0;
    let value = clean;
    if (value.includes(',') && value.includes('.')) value = value.replace(/\./g, '').replace(',', '.');
    else if (value.includes(',')) value = value.replace(',', '.');
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function formatDecimalBR(value, decimals = 2) {
    return Number(value || 0).toLocaleString('pt-BR', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
  }

  function formatCurrencyBR(value) {
    return `R$ ${formatDecimalBR(value, 2)}`;
  }

  // City -> route autofill support already provided by backend data attributes.
  const form = $('.professional-form');
  const routeMap = new Map();
  const norm = (v = '') => v.toString().trim().toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  if (form?.dataset.routePairs) {
    form.dataset.routePairs.split(';').forEach(pair => {
      const [city, route] = pair.split('|');
      const key = norm(city);
      if (key && route && !routeMap.has(key)) routeMap.set(key, route);
    });
  }
  const cityInput = $('#cityInput');
  const routeInput = $('#routeInput');
  const routeInputHidden = $('#routeInputHidden');
  const farmInput = $('#farmName');
  const clientNameInput = $('#clientName');
  const clientPhoneInput = $('#clientPhone');
  const addressInput = $('#addressInput');
  const setRouteValue = (value = '') => {
    setValue(routeInput, value);
    setValue(routeInputHidden, value);
  };
  const autoFillRoute = () => {
    const route = routeMap.get(norm(cityInput.value));
    setRouteValue(route || '');
  };
  cityInput?.addEventListener('input', autoFillRoute);
  cityInput?.addEventListener('change', autoFillRoute);

  const clientSelect = $('#clientSelect');
  const clientSearchInput = $('#clientSearchInput');
  const clientSearchResults = $('#clientSearchResults');
  const clientCodeInput = $('#clientCodeInput');
  const clientOptionsCache = clientSelect
    ? [...clientSelect.options].map(opt => ({
        value: String(opt.value || ''),
        name: String(opt.dataset.name || ''),
        code: String(opt.dataset.code || ''),
        phone: String(opt.dataset.phone || ''),
        farm: String(opt.dataset.farm || ''),
        city: String(opt.dataset.city || ''),
        route: String(opt.dataset.route || ''),
        address: String(opt.dataset.address || ''),
        label: String(opt.textContent || '').trim(),
        isNew: !String(opt.value || '').trim(),
        search: norm(`${opt.dataset.code || ''} ${opt.dataset.name || ''} ${opt.dataset.farm || ''} ${opt.dataset.city || ''} ${opt.textContent || ''}`)
      }))
    : [];
  let clientDropdownOpen = false;
  let clientHighlightIndex = -1;

  const setValue = (field, value = '') => {
    if (field) field.value = value;
  };
  const setClientCodeMode = isExisting => {
    if (!clientCodeInput) return;
    clientCodeInput.readOnly = !!isExisting;
    clientCodeInput.classList.toggle('readonly-input', !!isExisting);
    clientCodeInput.title = isExisting ? 'Código preenchido automaticamente para cliente já cadastrado.' : '';
  };
  const clearClientFields = () => {
    setValue(clientNameInput, '');
    setValue(clientPhoneInput, '');
    setValue(farmInput, '');
    setValue(cityInput, '');
    setRouteValue('');
    setValue(addressInput, '');
    setValue(clientCodeInput, '');
    setClientCodeMode(false);
  };
  const clientPrimaryLabel = item => {
    if (!item) return '';
    if (item.isNew) return 'Novo cliente / preencher abaixo';
    const code = item.code ? `${item.code} - ` : '';
    return `${code}${item.name || item.label}`.trim();
  };
  const escapeClientHtml = value => String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
  const getFilteredClients = (query = '') => {
    const normalized = norm(query);
    const matches = clientOptionsCache.filter(item => item.isNew || !normalized || item.search.includes(normalized));
    const newItem = matches.find(item => item.isNew) || {
      value: '',
      isNew: true,
      name: '',
      code: '',
      farm: '',
      city: '',
      route: '',
      address: '',
      phone: '',
      label: 'Novo cliente / preencher abaixo',
      search: ''
    };
    const existing = matches.filter(item => !item.isNew).sort((a, b) => a.label.localeCompare(b.label, 'pt-BR'));
    return [newItem, ...existing];
  };
  const closeClientDropdown = () => {
    if (!clientSearchResults) return;
    clientSearchResults.hidden = true;
    clientSearchResults.classList.remove('show');
    clientDropdownOpen = false;
    clientHighlightIndex = -1;
  };
  const renderClientDropdown = (query = '', keepHighlight = false) => {
    if (!clientSearchResults || !clientSelect) return;
    const rows = getFilteredClients(query);
    const selected = String(clientSelect.value || '');
    const visibleRows = rows.slice(0, 120);
    const listHtml = visibleRows.map((item, idx) => {
      const selectedClass = item.value === selected ? ' selected' : '';
      const code = item.code ? `<span class="client-result-code">${escapeClientHtml(item.code)}</span>` : '';
      const subtitleSource = item.isNew ? 'Criar cliente novo com os campos abaixo.' : [item.farm, item.city].filter(Boolean).join(' · ');
      return `<button type="button" class="client-result-item${selectedClass}" data-client-index="${idx}" data-client-value="${escapeClientHtml(item.value)}"><span class="client-result-main">${code}<b>${escapeClientHtml(clientPrimaryLabel(item))}</b></span><small class="client-result-sub">${escapeClientHtml(subtitleSource || 'Sem complemento cadastrado')}</small></button>`;
    }).join('');
    clientSearchResults.innerHTML = listHtml || '<div class="client-result-empty">Nenhum cliente encontrado para esta busca.</div>';
    clientSearchResults.hidden = false;
    clientSearchResults.classList.add('show');
    clientDropdownOpen = true;
    if (!keepHighlight) clientHighlightIndex = -1;
    if (!visibleRows.length) return;
    const selectedIndex = visibleRows.findIndex(item => item.value === selected);
    if (selectedIndex >= 0 && clientHighlightIndex < 0) clientHighlightIndex = selectedIndex;
    const buttons = [...clientSearchResults.querySelectorAll('.client-result-item')];
    buttons.forEach((btn, idx) => btn.classList.toggle('active', idx === clientHighlightIndex));
  };
  const applyClientSelection = clientId => {
    if (!clientSelect) return;
    const next = String(clientId || '');
    const found = [...clientSelect.options].some(opt => String(opt.value || '') === next);
    clientSelect.value = found ? next : '';
    clientSelect.dispatchEvent(new Event('change'));
  };
  clientSelect?.addEventListener('change', () => {
    if (window._isAutoErpLookupFilling) return;
    const opt = clientSelect.selectedOptions[0];
    const selectedId = String(clientSelect.value || '');
    if (!opt || !selectedId) {
      if (clientSearchInput) clientSearchInput.value = '';
      clearClientFields();
      return;
    }
    setValue(clientNameInput, opt.dataset.name || '');
    setValue(clientPhoneInput, opt.dataset.phone || '');
    setValue(farmInput, opt.dataset.farm || '');
    setValue(cityInput, opt.dataset.city || '');
    setRouteValue(opt.dataset.route || '');
    setValue(addressInput, opt.dataset.address || '');
    setValue(clientCodeInput, opt.dataset.code || '');
    setClientCodeMode(true);
    if (clientSearchInput) {
      const cacheItem = clientOptionsCache.find(item => String(item.value || '') === selectedId);
      clientSearchInput.value = clientPrimaryLabel(cacheItem || {
        isNew: false,
        code: opt.dataset.code || '',
        name: opt.dataset.name || '',
        label: opt.textContent || ''
      });
    }
    cityInput?.dispatchEvent(new Event('change'));
  });
  clientSearchInput?.addEventListener('focus', () => {
    renderClientDropdown(clientSearchInput.value || '');
  });
  clientSearchInput?.addEventListener('input', () => {
    renderClientDropdown(clientSearchInput.value || '');
  });
  clientSearchInput?.addEventListener('keydown', e => {
    if (!clientDropdownOpen && ['ArrowDown', 'ArrowUp', 'Enter'].includes(e.key)) {
      renderClientDropdown(clientSearchInput.value || '');
    }
    const buttons = [...(clientSearchResults?.querySelectorAll('.client-result-item') || [])];
    if (!buttons.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      clientHighlightIndex = Math.min(buttons.length - 1, clientHighlightIndex + 1);
      buttons.forEach((btn, idx) => btn.classList.toggle('active', idx === clientHighlightIndex));
      buttons[clientHighlightIndex]?.scrollIntoView({ block: 'nearest' });
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      clientHighlightIndex = Math.max(0, clientHighlightIndex - 1);
      buttons.forEach((btn, idx) => btn.classList.toggle('active', idx === clientHighlightIndex));
      buttons[clientHighlightIndex]?.scrollIntoView({ block: 'nearest' });
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      const targetIndex = clientHighlightIndex >= 0 ? clientHighlightIndex : 0;
      const chosen = buttons[targetIndex];
      if (!chosen) return;
      applyClientSelection(chosen.dataset.clientValue || '');
      closeClientDropdown();
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeClientDropdown();
    }
  });
  clientSearchResults?.addEventListener('mousedown', e => {
    const btn = e.target.closest('.client-result-item');
    if (!btn) return;
    e.preventDefault();
    applyClientSelection(btn.dataset.clientValue || '');
    closeClientDropdown();
  });
  document.addEventListener('click', e => {
    if (!clientSearchResults || !clientSearchInput) return;
    const searchWrap = clientSearchInput.closest('.client-search-wrap');
    if (!searchWrap) return;
    if (!searchWrap.contains(e.target)) closeClientDropdown();
  });
  if (clientSelect?.value) {
    clientSelect.dispatchEvent(new Event('change'));
  } else {
    clearClientFields();
  }

  // Duplicate guard for client code/name (pre-submit + backend hard validation).
  function initClientDuplicateGuard(formEl) {
    if (!formEl) return;
    const endpoint = formEl.dataset.clientDupEndpoint || '/api/clients/duplicate-check';
    const excludeId = String(formEl.dataset.clientDupExcludeId || '').trim();
    const codeField = formEl.querySelector('input[name="customer_code"]');
    const nameField = formEl.querySelector('input[name="name"], input[name="client_name"]');
    const selectedClientField = formEl.querySelector('select[name="client_id"]');
    const warningBox = formEl.querySelector('[data-client-dup-warning]');
    const submitButtons = [...formEl.querySelectorAll('button[type="submit"], button:not([type])')]
      .filter(btn => !btn.closest('a'));
    if (!codeField && !nameField) return;

    const setBlocked = blocked => {
      submitButtons.forEach(btn => { btn.disabled = !!blocked; });
    };
    const setWarning = (message = '') => {
      if (!warningBox) return;
      warningBox.hidden = !message;
      warningBox.textContent = message || '';
      codeField?.classList.toggle('dup-invalid', !!message);
      nameField?.classList.toggle('dup-invalid', !!message);
    };
    const shouldCheck = () => {
      if (!selectedClientField) return true;
      return !String(selectedClientField.value || '').trim();
    };

    let debounceHandle = null;
    let requestSeq = 0;
    const runCheck = async () => {
      if (!shouldCheck()) {
        setWarning('');
        setBlocked(false);
        return;
      }
      const code = String(codeField?.value || '').trim();
      const name = String(nameField?.value || '').trim();
      if (!code && !name) {
        setWarning('');
        setBlocked(false);
        return;
      }
      const seq = ++requestSeq;
      const params = new URLSearchParams();
      if (code) params.set('customer_code', code);
      if (name) params.set('name', name);
      if (excludeId) params.set('exclude_id', excludeId);
      try {
        const resp = await fetch(`${endpoint}?${params.toString()}`, {
          method: 'GET',
          credentials: 'same-origin',
          headers: { 'X-Requested-With': 'XMLHttpRequest' }
        });
        const data = await resp.json().catch(() => null);
        if (seq !== requestSeq) return;
        if (!resp.ok || !data || !data.ok) {
          setWarning('Não foi possível validar duplicidade agora. Tente novamente.');
          setBlocked(true);
          return;
        }
        const msgs = [];
        if (data.code_exists) msgs.push(`Código do Cliente já cadastrado (${data.code_owner || 'registro existente'}).`);
        if (data.name_exists) msgs.push(`Nome do cliente já cadastrado (${data.name_owner || 'registro existente'}).`);
        if (msgs.length) {
          setWarning(`Cadastro bloqueado: ${msgs.join(' ')}`);
          setBlocked(true);
          return;
        }
        setWarning('');
        setBlocked(false);
      } catch (_) {
        if (seq !== requestSeq) return;
        setWarning('Não foi possível validar duplicidade agora. Tente novamente.');
        setBlocked(true);
      }
    };
    const queueCheck = () => {
      clearTimeout(debounceHandle);
      debounceHandle = setTimeout(runCheck, 220);
    };

    codeField?.addEventListener('input', queueCheck);
    nameField?.addEventListener('input', queueCheck);
    codeField?.addEventListener('blur', queueCheck);
    nameField?.addEventListener('blur', queueCheck);
    selectedClientField?.addEventListener('change', queueCheck);
    formEl.addEventListener('submit', e => {
      if ([...submitButtons].some(btn => btn.disabled)) e.preventDefault();
    });
    queueCheck();
  }

  $$('form[data-client-dup-check="1"]').forEach(initClientDuplicateGuard);

  // Numeric masks for peso, capacidade e valor monetário.
  function attachNumberMask(input, mode = 'decimal') {
    if (!input) return;
    const decimals = Number(input.dataset.decimals || '2');
    input.addEventListener('input', () => {
      input.value = input.value.replace(/[^\d,.\-]/g, '');
    });
    input.addEventListener('focus', () => {
      if (mode === 'currency') input.value = input.value.replace(/^R\$\s*/i, '');
    });
    input.addEventListener('blur', () => {
      if (!input.value.trim()) {
        input.value = mode === 'currency' ? 'R$ 0,00' : formatDecimalBR(0, decimals);
        return;
      }
      const n = parseLocaleNumber(input.value);
      input.value = mode === 'currency' ? formatCurrencyBR(n) : formatDecimalBR(n, decimals);
    });
    if (input.value && String(input.value).trim()) {
      const n = parseLocaleNumber(input.value);
      input.value = mode === 'currency' ? formatCurrencyBR(n) : formatDecimalBR(n, decimals);
    }
  }
  $$('input[data-mask="decimal"]').forEach(i => attachNumberMask(i, 'decimal'));
  $$('input[data-mask="currency"]').forEach(i => attachNumberMask(i, 'currency'));

  // Force uppercase in selected typed fields (without depending on CapsLock/Shift).
  function attachUppercaseTransform(field) {
    if (!field) return;
    let composing = false;
    field.addEventListener('compositionstart', () => { composing = true; });
    field.addEventListener('compositionend', () => {
      composing = false;
      field.value = String(field.value || '').toUpperCase();
    });
    field.addEventListener('input', () => {
      if (composing) return;
      field.value = String(field.value || '').toUpperCase();
    });
    if (field.value) field.value = String(field.value).toUpperCase();
  }
  $$('input[data-force-uppercase], textarea[data-force-uppercase]').forEach(attachUppercaseTransform);

  // Load builder capacity calculation
  const weightInputs = $$('.load-check input[type="checkbox"]');
  const selectedWeight = $('#selectedWeight');
  const selectedCount = $('#selectedCount');
  const capacityInput = $('#capacity');
  const capacityAlert = $('#capacityAlert');
  const capacityBar = $('#capacityBar');
  const routeBuilderSubmit = $('.route-builder button[type="submit"], .route-builder button');
  const routeBuilderSelect = $('.route-builder select[name="route_name"]');
  const routeLockToggle = $('#routeLockToggle');
  const loadChecks = $$('.load-check');
  const checksContainer = $('.route-builder .checks');
  let checksEmptyState = null;
  function calcLoad() {
    if (!selectedWeight) return;
    let total = 0;
    let count = 0;
    weightInputs.forEach(i => {
      if (i.checked) {
        count += 1;
        total += Number(i.dataset.weight || 0);
      }
    });
    selectedWeight.textContent = total.toLocaleString('pt-BR', { maximumFractionDigits: 0 });
    if (selectedCount) selectedCount.textContent = String(count);
    const cap = parseLocaleNumber(capacityInput?.value || '0');
    const pct = cap > 0 ? (total / cap) * 100 : 0;
    if (capacityBar) capacityBar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
    if (capacityAlert) {
      if (cap && total > cap) {
        capacityAlert.textContent = '⚠ Acima da capacidade do veículo';
        capacityAlert.className = 'deadline late';
        if (routeBuilderSubmit) routeBuilderSubmit.disabled = true;
      } else if (cap && total > cap * 0.9) {
        capacityAlert.textContent = 'Carga próxima do limite';
        capacityAlert.className = 'deadline warn';
        if (routeBuilderSubmit) routeBuilderSubmit.disabled = false;
      } else {
        capacityAlert.textContent = 'Capacidade dentro do limite';
        capacityAlert.className = 'deadline ok';
        if (routeBuilderSubmit) routeBuilderSubmit.disabled = false;
      }
    }
  }
  function ensureChecksEmptyState() {
    if (!checksContainer) return null;
    if (!checksEmptyState) {
      checksEmptyState = document.createElement('div');
      checksEmptyState.className = 'empty checks-empty';
      checksEmptyState.textContent = 'Nenhum pedido da rota selecionada está disponível para esta carga.';
      checksContainer.appendChild(checksEmptyState);
    }
    return checksEmptyState;
  }
  function applyRouteOrderFilter() {
    if (!routeBuilderSelect || !loadChecks.length) return;
    const lockByRoute = !!(routeLockToggle && routeLockToggle.checked);
    const selectedRoute = lockByRoute ? norm(routeBuilderSelect.value || '') : '';
    let visibleCount = 0;
    loadChecks.forEach(label => {
      const chk = label.querySelector('input[type="checkbox"]');
      if (!chk) return;
      const orderRoute = norm(chk.dataset.route || '');
      const visible = !selectedRoute || selectedRoute === 'mista' || orderRoute === selectedRoute;
      label.style.display = visible ? 'flex' : 'none';
      if (!visible && chk.checked) chk.checked = false;
      if (visible) visibleCount += 1;
    });
    const emptyEl = ensureChecksEmptyState();
    if (emptyEl) {
      if (lockByRoute && selectedRoute) {
        emptyEl.textContent = 'Nenhum pedido da rota selecionada está disponível para esta carga.';
      } else {
        emptyEl.textContent = 'Nenhum pedido elegível está disponível para esta carga.';
      }
      emptyEl.style.display = (visibleCount > 0) ? 'none' : 'block';
    }
    calcLoad();
  }
  weightInputs.forEach(i => i.addEventListener('change', calcLoad));
  capacityInput?.addEventListener('input', calcLoad);
  routeBuilderSelect?.addEventListener('change', applyRouteOrderFilter);
  routeLockToggle?.addEventListener('change', applyRouteOrderFilter);

  const vehicleSelect = $('#vehicle_id');
  vehicleSelect?.addEventListener('change', () => {
    const opt = vehicleSelect.selectedOptions[0];
    const cap = opt?.dataset?.capacity;
    if (cap && capacityInput) { capacityInput.value = formatDecimalBR(parseLocaleNumber(cap), 2); calcLoad(); }
  });
  applyRouteOrderFilter();
  calcLoad();

  // Settlement helpers
  const markAllDelivered = $('#markAllDelivered');
  const settlementChecks = $$('.settlement-ok');
  const settlementResults = $$('.settlement-result');
  markAllDelivered?.addEventListener('change', () => {
    settlementChecks.forEach(chk => { chk.checked = markAllDelivered.checked; });
    if (markAllDelivered.checked) settlementResults.forEach(sel => { sel.value = 'entregue'; });
  });
  settlementResults.forEach(sel => {
    sel.addEventListener('change', () => {
      if (sel.value === 'problema' && markAllDelivered) markAllDelivered.checked = false;
    });
  });

  // Phone mask
  function formatPhoneBR(value) {
    const digits = String(value || '').replace(/\D/g, '').slice(0, 11);
    if (!digits) return '';
    if (digits.length <= 2) return `(${digits}`;
    const ddd = digits.slice(0, 2);
    const rest = digits.slice(2);
    if (digits.length <= 10) {
      if (rest.length <= 4) return `(${ddd}) ${rest}`;
      return `(${ddd}) ${rest.slice(0, 4)}-${rest.slice(4)}`;
    }
    if (rest.length <= 5) return `(${ddd}) ${rest}`;
    return `(${ddd}) ${rest.slice(0, 5)}-${rest.slice(5)}`;
  }
  $$('input[name="phone"], input[name="whatsapp"], input[name="client_phone"]').forEach(input => {
    input.addEventListener('input', () => {
      input.value = formatPhoneBR(input.value);
    });
    if (input.value) input.value = formatPhoneBR(input.value);
  });

  const settingsSection = new URLSearchParams(window.location.search).get('section');
  if (settingsSection && window.location.pathname === '/settings') {
    const target = document.getElementById(`settings-${settingsSection}`);
    target?.scrollIntoView({ block: 'start', behavior: 'auto' });
  }

  // Interface intentionally keeps motion minimal for operational readability.

  // ============================================================
  // MELHORIAS UX/UI — AUDITORIA 2026
  // ============================================================

  // --- 1. Filtros colapsáveis com contagem de ativos ---
  (function initCollapsibleFilters() {
    const filtersWrap = $('.filters');
    if (!filtersWrap) return;
    const storageKey = 'logistica_filters_collapsed';
    const collapsed = sessionStorage.getItem(storageKey) === '1';

    // Contar filtros ativos (campos com valor)
    function countActiveFilters() {
      let count = 0;
      filtersWrap.querySelectorAll('input[type="text"], input:not([type]), select').forEach(el => {
        const v = (el.value || '').trim();
        if (v && v !== '' && el.name && el.name !== '_csrf') count++;
      });
      return count;
    }

    // Criar header colapsável
    const header = document.createElement('div');
    header.className = 'filters-header';
    const activeCount = countActiveFilters();
    const badge = activeCount > 0 ? `<span class="filters-active-badge">${activeCount} ativo${activeCount > 1 ? 's' : ''}</span>` : '';
    header.innerHTML = `<b>🔍 Filtros de busca ${badge}</b><span style="font-size:13px;color:var(--muted)">${collapsed ? '▼ Expandir' : '▲ Recolher'}</span>`;

    const collapsibleWrap = document.createElement('div');
    collapsibleWrap.className = 'filters-collapsible' + (collapsed ? ' collapsed' : '');

    filtersWrap.parentNode.insertBefore(header, filtersWrap);
    filtersWrap.parentNode.insertBefore(collapsibleWrap, filtersWrap);
    collapsibleWrap.appendChild(filtersWrap);

    header.addEventListener('click', () => {
      const isCollapsed = collapsibleWrap.classList.toggle('collapsed');
      sessionStorage.setItem(storageKey, isCollapsed ? '1' : '0');
      header.querySelector('span').textContent = isCollapsed ? '▼ Expandir' : '▲ Recolher';
    });
  })();

  // --- 2. Densidade variável da tabela ---
  (function initTableDensity() {
    const table = $('.orders-table');
    if (!table) return;
    const storageKey = 'logistica_table_density';
    const saved = localStorage.getItem(storageKey) || 'normal';
    if (saved !== 'normal') table.classList.add(`density-${saved}`);

    const toggle = document.createElement('div');
    toggle.className = 'density-toggle';
    toggle.innerHTML = `
      <button type="button" data-density="compact" title="Compacto">≡</button>
      <button type="button" data-density="normal"  title="Normal">☰</button>
    `;
    toggle.querySelectorAll('button').forEach(btn => {
      if (btn.dataset.density === saved) btn.classList.add('active');
      btn.addEventListener('click', () => {
        const d = btn.dataset.density;
        table.className = table.className.replace(/density-\S+/g, '').trim();
        if (d !== 'normal') table.classList.add(`density-${d}`);
        toggle.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
        localStorage.setItem(storageKey, d);
      });
    });

    // Inserir toggle antes do table-wrap
    const tableWrap = table.closest('.table-wrap');
    if (tableWrap) tableWrap.parentNode.insertBefore(toggle, tableWrap);
  })();

  // --- 3. Barras de progresso SLA nas linhas da tabela ---
  (function initSlaProgressBars() {
    const SLA_DAYS = 15;
    const RISK_DAYS = 5;
    $$('tbody tr').forEach(row => {
      // Encontrar célula de prazo (5ª coluna padrão)
      const cells = row.querySelectorAll('td');
      if (cells.length < 5) return;
      const deadlineCell = cells[4];
      const deadlineText = deadlineCell.textContent || '';
      // Tentar extrair data no formato dd/mm/yyyy
      const match = deadlineText.match(/(\d{2})\/(\d{2})\/(\d{4})/);
      if (!match) return;
      const deadline = new Date(`${match[3]}-${match[2]}-${match[1]}`);
      const today = new Date(); today.setHours(0,0,0,0);
      const diffDays = Math.round((deadline - today) / 86400000);
      const startDays = -SLA_DAYS; // pior caso = SLA_DAYS atrás
      const pct = Math.max(0, Math.min(100, ((diffDays - startDays) / (SLA_DAYS - startDays)) * 100));
      const cls = diffDays < 0 ? 'late' : diffDays <= RISK_DAYS ? 'warn' : 'ok';
      const bar = document.createElement('div');
      bar.className = 'sla-progress';
      bar.innerHTML = `<div class="sla-progress-fill ${cls}" style="width:${pct.toFixed(1)}%"></div>`;
      deadlineCell.appendChild(bar);
    });
  })();

  // --- 4. Modal de adição rápida de cidade/rota (inline, sem sair do form) ---
  (function initQuickAddCity() {
    // Achar o select de cidade do pedido
    const citySelect = $('#cityInput');
    // Achar os inputs de cidade de cliente (podem ser vários, ex: criar e editar)
    const clientCityInputs = $$('input[name="city"][list="citySuggestions"]');

    if (!citySelect && !clientCityInputs.length) return;

    // Colecionar rotas existentes do routeMap
    const existingRoutes = Array.from(new Set(routeMap.values())).sort();
    let routeOptionsHtml = '<option value="">Selecione a rota</option>';
    existingRoutes.forEach(r => {
      if (r) {
        routeOptionsHtml += `<option value="${r.toUpperCase()}">${r.toUpperCase()}</option>`;
      }
    });
    routeOptionsHtml += '<option value="__new__">＋ Cadastrar nova rota...</option>';

    // Criar overlay e modal
    const overlay = document.createElement('div');
    overlay.className = 'quick-add-overlay';
    overlay.id = 'quickAddCityOverlay';
    overlay.innerHTML = `
      <div class="quick-add-modal" role="dialog" aria-modal="true" aria-labelledby="quickAddTitle">
        <button type="button" class="quick-add-close" id="quickAddClose" title="Fechar">✕</button>
        <h3 id="quickAddTitle">➕ Adicionar Cidade/Rota</h3>
        <p style="color:var(--muted);font-size:13px;margin:0 0 14px">Adicione a cidade sem perder os dados deste formulário.</p>
        <div id="quickAddStatus"></div>
        <div class="form" style="display:grid;gap:10px">
          <label>Cidade *<input id="qaCity" required placeholder="Nome da cidade" style="text-transform:uppercase"></label>
          <label>Rota *
            <select id="qaRouteSelect" required style="width:100%">
              ${routeOptionsHtml}
            </select>
            <input id="qaRouteInput" placeholder="Digite a nova rota (Ex: ROTA-MG-1)" style="text-transform:uppercase; display:none; margin-top:6px; width:100%">
          </label>
          <label>UF<input id="qaUF" maxlength="2" placeholder="MG" style="text-transform:uppercase;max-width:80px"></label>
          <label>Ordem de entrega<input id="qaOrder" type="number" min="1" placeholder="1"></label>
          <div style="display:flex;gap:8px;margin-top:4px">
            <button type="button" id="qaSubmit">Salvar cidade</button>
            <button type="button" id="qaCancel" class="btn ghost">Cancelar</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const qaRouteSelect = overlay.querySelector('#qaRouteSelect');
    const qaRouteInput = overlay.querySelector('#qaRouteInput');
    qaRouteSelect.addEventListener('change', () => {
      if (qaRouteSelect.value === '__new__') {
        qaRouteInput.style.display = 'block';
        qaRouteInput.required = true;
        qaRouteInput.focus();
      } else {
        qaRouteInput.style.display = 'none';
        qaRouteInput.required = false;
        qaRouteInput.value = '';
      }
    });

    const closeModal = () => {
      overlay.classList.remove('open');
      document.getElementById('quickAddStatus').innerHTML = '';
      document.getElementById('qaCity').value = '';
      document.getElementById('qaRouteSelect').value = '';
      document.getElementById('qaRouteInput').value = '';
      document.getElementById('qaRouteInput').style.display = 'none';
      document.getElementById('qaRouteInput').required = false;
      document.getElementById('qaUF').value = '';
      document.getElementById('qaOrder').value = '';
    };

    document.getElementById('quickAddClose').addEventListener('click', closeModal);
    document.getElementById('qaCancel').addEventListener('click', closeModal);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && overlay.classList.contains('open')) closeModal(); });

    // Função para anexar botão trigger a um elemento de input/select de cidade
    function attachTrigger(element, isSelect) {
      const trigger = document.createElement('button');
      trigger.type = 'button';
      trigger.className = 'quick-add-trigger';
      trigger.textContent = '＋ Adicionar cidade/rota';
      trigger.title = 'Adicionar nova cidade sem sair deste formulário';
      element.parentNode.appendChild(trigger);

      trigger.addEventListener('click', () => {
        overlay.dataset.targetIsSelect = isSelect ? '1' : '0';
        overlay.classList.add('open');
        setTimeout(() => document.getElementById('qaCity')?.focus(), 100);
      });
    }

    if (citySelect) attachTrigger(citySelect, true);
    clientCityInputs.forEach(el => attachTrigger(el, false));

    // Submeter via fetch (sem perder o formulário aberto)
    document.getElementById('qaSubmit').addEventListener('click', async () => {
      const city = (document.getElementById('qaCity').value || '').trim().toUpperCase();
      const rSel = document.getElementById('qaRouteSelect').value;
      const rVal = rSel === '__new__' ? document.getElementById('qaRouteInput').value : rSel;
      const route = (rVal || '').trim().toUpperCase();
      const uf = (document.getElementById('qaUF').value || '').trim().toUpperCase();
      const order = (document.getElementById('qaOrder').value || '').trim();
      const statusEl = document.getElementById('quickAddStatus');

      if (!city || !route) {
        statusEl.innerHTML = `<div class="alert danger">Cidade e Rota são obrigatórios.</div>`;
        return;
      }

      const submitBtn = document.getElementById('qaSubmit');
      submitBtn.disabled = true;
      submitBtn.textContent = 'Salvando...';

      const formData = new URLSearchParams();
      formData.set('city', city);
      formData.set('route_name', route);
      formData.set('uf', uf);
      formData.set('delivery_order', order || '1');
      // CSRF token
      const csrfCookie = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('csrf_token='));
      const csrf = csrfCookie ? decodeURIComponent(csrfCookie.split('=')[1]) : '';
      formData.set('_csrf', csrf);

      try {
        const resp = await fetch('/route-cities', {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest' },
          credentials: 'same-origin',
          body: formData.toString()
        });

        if (resp.ok || resp.status === 302) {
          const isSelect = overlay.dataset.targetIsSelect === '1';

          // Atualizar select de rotas do modal
          const qaRouteSelect = document.getElementById('qaRouteSelect');
          if (rSel === '__new__' && route && qaRouteSelect) {
            const exists = Array.from(qaRouteSelect.options).some(o => o.value === route);
            if (!exists) {
              const newRouteOpt = document.createElement('option');
              newRouteOpt.value = route;
              newRouteOpt.textContent = route;
              qaRouteSelect.insertBefore(newRouteOpt, qaRouteSelect.lastElementChild);
            }
          }

          if (isSelect && citySelect) {
            // Adicionar nova opção ao select de cidade sem recarregar
            const newOpt = document.createElement('option');
            newOpt.value = city;
            newOpt.textContent = city;
            newOpt.selected = true;
            citySelect.appendChild(newOpt);
            citySelect.value = city;
            citySelect.dispatchEvent(new Event('change'));

            // Atualizar route map se existir
            const form = citySelect.closest('form');
            if (form && form.dataset.routePairs !== undefined) {
              form.dataset.routePairs += `;${city}|${route}`;
            }
            if (routeMap && typeof norm === 'function') {
              routeMap.set(norm(city), route);
            }
            // Forçar rota
            const routeHidden = $('#routeInputHidden');
            const routeInput = $('#routeInput');
            if (routeHidden) routeHidden.value = route;
            if (routeInput) routeInput.value = route;
          } else {
            // É um input de cliente
            // Achar qual formulário está ativo para preencher os campos corretos
            const activeForm = document.activeElement?.closest('form') || $('form[action="/clients"]') || $('form[action*="/clients"]');
            const activeCity = activeForm?.querySelector('input[name="city"]');
            const activeRoute = activeForm?.querySelector('input[name="route_name"]');

            if (activeCity) {
              activeCity.value = city;
              activeCity.dispatchEvent(new Event('change'));
            }
            if (activeRoute) {
              activeRoute.value = route;
              activeRoute.dispatchEvent(new Event('change'));
            }

            // Atualizar datalists de sugestões
            const cityDatalist = $('#citySuggestions');
            const routeDatalist = $('#routeSuggestions');
            if (cityDatalist && ![...cityDatalist.options].some(o => o.value === city)) {
              const opt = document.createElement('option');
              opt.value = city;
              cityDatalist.appendChild(opt);
            }
            if (routeDatalist && ![...routeDatalist.options].some(o => o.value === route)) {
              const opt = document.createElement('option');
              opt.value = route;
              routeDatalist.appendChild(opt);
            }
          }

          statusEl.innerHTML = `<div class="alert success">✅ Cidade "${escapeClientHtml(city)}" adicionada com sucesso!</div>`;
          setTimeout(closeModal, 1200);
        } else {
          statusEl.innerHTML = `<div class="alert danger">Não foi possível salvar. Verifique se a cidade já existe ou tente novamente.</div>`;
        }
      } catch (err) {
        statusEl.innerHTML = `<div class="alert danger">Erro de conexão ao salvar. Tente novamente.</div>`;
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Salvar cidade';
      }
    });
  })();

  // Direct Billing Modal logic
  (() => {
    document.addEventListener('click', e => {
      const btn = e.target.closest('.faturar-btn');
      if (btn) {
        e.preventDefault();
        const orderId = btn.dataset.orderId;
        const orderNumber = btn.dataset.orderNumber;
        const client = btn.dataset.client;
        const city = btn.dataset.city;

        const overlay = document.getElementById('billingModalOverlay');
        if (overlay) {
          const idInput = document.getElementById('billingOrderId');
          const numberInput = document.getElementById('billingOrderNumber');
          const clientInput = document.getElementById('billingClientName');
          const invoiceInput = document.getElementById('billingInvoiceNumber');
          const dateInput = document.getElementById('billingInvoiceDate');

          if (idInput) idInput.value = orderId || '';
          if (numberInput) numberInput.value = orderNumber || '';
          if (clientInput) clientInput.value = (client || '') + (city ? ` (${city})` : '');
          if (invoiceInput) {
            invoiceInput.value = '';
            setTimeout(() => invoiceInput.focus(), 50);
          }

          if (dateInput) {
            const today = new Date();
            const yyyy = today.getFullYear();
            let mm = today.getMonth() + 1;
            let dd = today.getDate();
            if (dd < 10) dd = '0' + dd;
            if (mm < 10) mm = '0' + mm;
            dateInput.value = `${yyyy}-${mm}-${dd}`;
          }

          const form = document.getElementById('billingForm');
          if (form) {
            form.action = `/orders/${orderId}/invoice`;
            ensureCsrfInput(form);
          }

          overlay.classList.add('open');
          overlay.style.display = 'flex';
        }
      }
    });

    window.closeBillingModal = () => {
      const overlay = document.getElementById('billingModalOverlay');
      if (overlay) {
        overlay.classList.remove('open');
        overlay.style.display = 'none';
      }
    };

    const billingOverlay = document.getElementById('billingModalOverlay');
    if (billingOverlay) {
      billingOverlay.addEventListener('click', e => {
        if (e.target === billingOverlay) {
          closeBillingModal();
        }
      });
    }
  })();

  // --- 5. Batch Cargo Date Modal (Definir Data para Selecionados) ---
  (() => {
    const btnSetDateSelected = $('#btnSetDateSelected');
    const overlay = $('#settlementDateOverlay');
    if (!btnSetDateSelected || !overlay) return;

    const closeBtn = $('#settlementDateClose');
    const cancelBtn = $('#btnCancelSetDate');
    const form = $('#settlementDateForm');
    const batchDateInput = $('#batch_delivery_date');

    const openModal = () => {
      const selectedChecks = $$('.settlement-ok').filter(chk => chk.checked);
      if (selectedChecks.length === 0) {
        alert('Nenhum pedido selecionado. Selecione pelo menos um pedido no checklist para definir a data.');
        return;
      }
      
      const today = new Date().toISOString().slice(0, 10);
      batchDateInput.value = today;

      overlay.classList.add('open');
      overlay.style.display = 'flex';
    };

    const closeModal = () => {
      overlay.classList.remove('open');
      overlay.style.display = 'none';
    };

    btnSetDateSelected.addEventListener('click', openModal);
    closeBtn?.addEventListener('click', closeModal);
    cancelBtn?.addEventListener('click', closeModal);
    overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(); });

    form.addEventListener('submit', e => {
      e.preventDefault();

      const chosenDate = batchDateInput.value;
      if (!chosenDate) {
        alert('Por favor, selecione uma data válida.');
        return;
      }

      const selectedChecks = $$('.settlement-ok').filter(chk => chk.checked);
      const selectedIds = selectedChecks.map(chk => chk.name.replace('ok_', ''));
      
      const settlementForm = $('.settlement-form');
      if (!settlementForm) {
        alert('Erro: Formulário de acerto não encontrado.');
        return;
      }
      const action = settlementForm.getAttribute('action');
      const routeIdMatch = action.match(/\/load-settlement\/(\d+)\/finish/);
      if (!routeIdMatch) {
        alert('Erro: ID da carga não identificada.');
        return;
      }
      const routeId = routeIdMatch[1];

      let hasDifferentDate = false;
      selectedIds.forEach(id => {
        const orderDateInput = $(`input[name="date_${id}"]`);
        if (orderDateInput && orderDateInput.value && orderDateInput.value !== chosenDate) {
          hasDifferentDate = true;
        }
      });

      let confirmMsg = `Confirma definir a data de entrega/ocorrência dos ${selectedIds.length} pedidos selecionados para ${chosenDate.split('-').reverse().join('/')}?`;
      if (hasDifferentDate) {
        confirmMsg = `Alguns dos pedidos selecionados já possuem datas preenchidas diferentes de ${chosenDate.split('-').reverse().join('/')}.\n\nDeseja realmente sobrescrever a data de todos os pedidos selecionados?`;
      }

      if (!confirm(confirmMsg)) {
        return;
      }

      const params = new URLSearchParams();
      params.append('_csrf', csrfToken);
      params.append('date', chosenDate);
      params.append('order_ids', selectedIds.join(','));

      LoadingBar.start();

      fetch(`/load-settlement/${routeId}/set-date`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded'
        },
        body: params.toString()
      })
      .then(response => {
        LoadingBar.finish();
        if (response.ok) {
          selectedIds.forEach(id => {
            const orderDateInput = $(`input[name="date_${id}"]`);
            if (orderDateInput) {
              orderDateInput.value = chosenDate;
            }
          });
          closeModal();
          showToast('Data atualizada com sucesso para os pedidos selecionados!', 'success');
        } else {
          response.text().then(text => {
            showToast('Erro ao atualizar datas no servidor: ' + text, 'error');
          });
        }
      })
      .catch(err => {
        LoadingBar.finish();
        showToast('Erro de rede ao atualizar datas: ' + err, 'error');
      });
    });
  })();

  // --- 6. Collapsible KPI Cards (SLA) Toggle Text ---
  (() => {
    const moreKpisDetails = $('.more-kpis-details');
    if (!moreKpisDetails) return;
    const summary = moreKpisDetails.querySelector('summary');
    if (!summary) return;
    const originalText = summary.textContent;
    moreKpisDetails.addEventListener('toggle', () => {
      if (moreKpisDetails.open) {
        summary.textContent = 'Ocultar indicadores secundários';
        summary.classList.add('active');
      } else {
        summary.textContent = originalText;
        summary.classList.remove('active');
      }
    });
  })();

  // --- 7. LoadingBar & Toast System (SaaS Redesign) ---
  const LoadingBar = {
    el: null,
    init() {
      if (document.getElementById('top-loading-bar')) {
        this.el = document.getElementById('top-loading-bar');
        return;
      }
      this.el = document.createElement('div');
      this.el.id = 'top-loading-bar';
      document.body.appendChild(this.el);
    },
    start() {
      this.init();
      this.el.classList.remove('finished');
      this.el.classList.add('active');
      this.el.style.width = '0%';
      this.el.offsetWidth; // Force reflow
      this.el.style.width = '90%';
    },
    finish() {
      if (!this.el) return;
      this.el.classList.remove('active');
      this.el.classList.add('finished');
      this.el.style.width = '100%';
    }
  };

  const Toast = {
    container: null,
    init() {
      if (document.getElementById('toast-container')) {
        this.container = document.getElementById('toast-container');
        return;
      }
      this.container = document.createElement('div');
      this.container.id = 'toast-container';
      document.body.appendChild(this.container);
    },
    show(message, type = 'success') {
      this.init();
      const toast = document.createElement('div');
      toast.className = `toast ${type}`;
      
      let icon = '✓';
      let iconClass = 'toast-success-icon';
      if (type === 'error') {
        icon = '✕';
        iconClass = 'toast-error-icon';
      } else if (type === 'info') {
        icon = 'ℹ';
        iconClass = 'toast-info-icon';
      }

      toast.innerHTML = `
        <span class="toast-icon ${iconClass}">${icon}</span>
        <div class="toast-content"></div>
      `;
      toast.querySelector('.toast-content').textContent = message;

      this.container.appendChild(toast);
      
      // Animate in
      toast.offsetWidth; // Force reflow
      toast.classList.add('show');

      // Auto remove after 4 seconds
      setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => {
          toast.remove();
        }, 300);
      }, 4000);
    }
  };

  // Expor globalmente
  window.showToast = (msg, type) => Toast.show(msg, type);
  window.LoadingBar = LoadingBar;

  // Interceptar envio de formulários para exibir a barra de progresso no topo
  document.querySelectorAll('form').forEach(form => {
    if (form.getAttribute('target')) return;
    form.addEventListener('submit', () => {
      LoadingBar.start();
    });
  });

  // Interceptar cliques em links de navegação para exibir a barra de progresso
  document.querySelectorAll('a:not([href^="#"]):not([target="_blank"])').forEach(link => {
    link.addEventListener('click', e => {
      // Se não for clique com botão direito ou teclas modificadoras
      if (e.button === 0 && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) {
        LoadingBar.start();
      }
    });
  });

  // --- 8. Inline Invoicing & Drawer System ---
  (() => {
    const drawerOverlay = $('#invoiceDrawer');
    if (!drawerOverlay) return;

    const drawerForm = $('#invoiceDrawerForm');
    const closeBtn = $('#invoiceDrawerClose');
    const cancelBtn = $('#btnCancelInvoice');

    const openDrawer = (btn) => {
      $('#drawer_order_id').value = btn.dataset.orderId;
      $('#drawer_order_number').textContent = btn.dataset.orderNumber;
      $('#drawer_client').textContent = btn.dataset.client || '—';
      $('#drawer_city').textContent = btn.dataset.city || '—';
      $('#drawer_weight').textContent = (btn.dataset.weight || '0') + ' kg';
      $('#drawer_seller').textContent = btn.dataset.seller || '—';
      $('#drawer_deadline').textContent = btn.dataset.deadline ? btn.dataset.deadline.split('-').reverse().join('/') : '—';
      
      $('#drawer_invoice_number').value = '';
      $('#drawer_invoiced_at').value = new Date().toISOString().slice(0, 10);

      drawerOverlay.classList.add('open');
      drawerOverlay.style.display = 'flex';
      setTimeout(() => {
        $('#drawer_invoice_number').focus();
      }, 200);
    };

    const closeDrawer = () => {
      drawerOverlay.classList.remove('open');
      setTimeout(() => {
        drawerOverlay.style.display = 'none';
      }, 300);
    };

    // Escutar cliques em qualquer botão de faturamento nas tabelas (usando delegação para suportar paginação/carregamento dinâmico)
    document.body.addEventListener('click', e => {
      const btn = e.target.closest('.btn-action-invoice');
      if (btn) {
        e.preventDefault();
        openDrawer(btn);
      }
    });

    closeBtn?.addEventListener('click', closeDrawer);
    cancelBtn?.addEventListener('click', closeDrawer);
    drawerOverlay.addEventListener('click', e => {
      if (e.target === drawerOverlay) closeDrawer();
    });

    // Submissão do Drawer
    drawerForm.addEventListener('submit', e => {
      e.preventDefault();

      const orderId = $('#drawer_order_id').value;
      const nf = $('#drawer_invoice_number').value.strip ? $('#drawer_invoice_number').value.strip() : $('#drawer_invoice_number').value.trim();
      const date = $('#drawer_invoiced_at').value;

      if (!nf) {
        showToast('Por favor, informe o número da Nota Fiscal.', 'error');
        return;
      }

      const params = new URLSearchParams();
      params.append('_csrf', csrfToken);
      params.append('invoice_number', nf);
      params.append('invoiced_at', date);

      LoadingBar.start();

      fetch(`/orders/${orderId}/invoice`, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'Content-Type': 'application/x-www-form-urlencoded'
        },
        body: params.toString()
      })
      .then(response => {
        LoadingBar.finish();
        if (response.ok) {
          closeDrawer();
          showToast(`Pedido faturado com sucesso! NF: ${nf}`, 'success');

          // Atualizar o DOM da linha na tabela correspondente
          const btn = document.querySelector(`.btn-action-invoice[data-order-id="${orderId}"]`);
          if (btn) {
            const row = btn.closest('tr') || btn.parentElement;
            if (row) {
              // Atualizar badge de status
              const badgeEl = row.querySelector('.badge');
              if (badgeEl) {
                badgeEl.textContent = 'Faturado';
                badgeEl.className = 'badge st-faturado';
              }
              // Substituir o botão de ação pelo próximo passo
              if (btn.dataset.canCharge === '1') {
                btn.outerHTML = '<a class="btn small" href="/routes/new">Adicionar em carga</a>';
              } else {
                btn.outerHTML = '<span class="muted">Sem permissão para carga</span>';
              }
            }
          }
        } else {
          response.text().then(text => {
            showToast('Erro ao faturar pedido: ' + text, 'error');
          });
        }
      })
      .catch(err => {
        LoadingBar.finish();
        showToast('Erro de rede ao faturar: ' + err, 'error');
      });
    });

    // Interceptar formulários de faturamento na fila (/faturamento)
    document.body.addEventListener('submit', e => {
      const form = e.target.closest('.invoice-card');
      if (!form) return;

      e.preventDefault();

      const action = form.getAttribute('action');
      const nfInput = form.querySelector('input[name="invoice_number"]');
      const dateInput = form.querySelector('input[name="invoiced_at"]');
      const nf = nfInput ? nfInput.value.trim() : '';
      const date = dateInput ? dateInput.value : '';

      if (!nf) {
        showToast('Por favor, informe a Nota Fiscal.', 'error');
        return;
      }

      const params = new URLSearchParams();
      params.append('_csrf', csrfToken);
      params.append('invoice_number', nf);
      params.append('invoiced_at', date);

      LoadingBar.start();

      fetch(action, {
        method: 'POST',
        headers: {
          'X-Requested-With': 'XMLHttpRequest',
          'Content-Type': 'application/x-www-form-urlencoded'
        },
        body: params.toString()
      })
      .then(response => {
        LoadingBar.finish();
        if (response.ok) {
          showToast(`Pedido faturado com sucesso! NF: ${nf}`, 'success');

          // Animação de fade-out e remoção do card
          form.classList.add('fade-out');
          updateActiveInvoiceCard();

          setTimeout(() => {
            const list = form.parentElement;
            form.remove();

            // Atualizar contadores na tela de faturamento
            if (list) {
              const remaining = list.querySelectorAll('.invoice-card').length;
              const countBadge = document.querySelector('.section-title .badge.blue');
              if (countBadge) {
                countBadge.textContent = `${remaining} pendente${remaining !== 1 ? 's' : ''}`;
              }

              // Se a fila esvaziou, exibir estado vazio
              if (remaining === 0) {
                const panel = list.closest('.panel');
                if (panel) {
                  panel.innerHTML = '<h2>Fila de NF por prioridade</h2><div class="empty">Nenhum pedido aguardando faturamento.</div>';
                }
              }
            }
          }, 300);
        } else {
          response.text().then(text => {
            showToast('Erro ao faturar: ' + text, 'error');
          });
        }
      })
      .catch(err => {
        LoadingBar.finish();
        showToast('Erro de rede ao faturar: ' + err, 'error');
      });
    });

    const updateActiveInvoiceCard = () => {
      if (window.location.pathname !== '/faturamento') return;

      document.querySelectorAll('.invoice-card').forEach(card => {
        card.classList.remove('active-invoice-card');
      });

      const firstCard = document.querySelector('.invoice-card:not(.fade-out)');
      if (firstCard) {
        firstCard.classList.add('active-invoice-card');
        const nfInput = firstCard.querySelector('input[name="invoice_number"]');
        if (nfInput && document.activeElement !== nfInput) {
          nfInput.focus();
        }
      }
    };

    window.updateActiveInvoiceCard = updateActiveInvoiceCard;

    // Escutar cliques em "Pular / Próximo"
    document.body.addEventListener('click', e => {
      const btn = e.target.closest('.btn-proximo-pedido');
      if (btn) {
        e.preventDefault();
        const currentCard = btn.closest('.invoice-card');
        if (currentCard) {
          currentCard.classList.remove('active-invoice-card');
          
          const visibleCards = Array.from(document.querySelectorAll('.invoice-card:not(.fade-out)'));
          const currentIndex = visibleCards.indexOf(currentCard);
          let nextCard = null;
          
          if (currentIndex !== -1 && currentIndex < visibleCards.length - 1) {
            nextCard = visibleCards[currentIndex + 1];
          } else if (visibleCards.length > 0) {
            nextCard = visibleCards[0];
          }
          
          if (nextCard) {
            nextCard.classList.add('active-invoice-card');
            const nfInput = nextCard.querySelector('input[name="invoice_number"]');
            if (nfInput) {
              nfInput.focus();
            }
          }
        }
      }
    });

    // Foco inicial
    updateActiveInvoiceCard();
  })();

  // --- 9. Real-Time Synchronization via Server-Sent Events (SSE) ---
  (() => {
    const indicator = document.getElementById('realtimeIndicator');
    if (!indicator) return;

    const indicatorText = document.getElementById('realtimeText');
    const dot = indicator.querySelector('.realtime-dot');
    let sse = null;

    const showIndicator = (text, isError = false) => {
      indicatorText.textContent = text;
      if (isError) {
        dot.classList.add('disconnected');
      } else {
        dot.classList.remove('disconnected');
      }
      indicator.classList.add('show');
    };

    const hideIndicator = () => {
      indicator.classList.remove('show');
    };

    let pollInterval = null;
    const connectSSE = () => {
      if (pollInterval) {
        clearInterval(pollInterval);
      }

      console.log('Real-time polling active');
      showIndicator('Atualização automática ativa');
      setTimeout(hideIndicator, 3000);

      pollInterval = setInterval(() => {
        handleRealtimeEvent('poll');
      }, 30000);
    };

    const handleRealtimeEvent = (eventType) => {
      // Não atualizar automaticamente em telas de criação ou edição ativa para evitar perda de dados digitados
      const path = window.location.pathname;
      if (path.includes('/routes/new') || path.includes('/edit') || path.includes('/new') || path.includes('/load-settlement/')) {
        return;
      }

      // Executar busca dos dados frescos do servidor mantendo a URL com filtros ativos
      fetch(window.location.href)
        .then(response => {
          if (!response.ok) throw new Error('Failed to fetch fresh data');
          return response.text();
        })
        .then(html => {
          const parser = new DOMParser();
          const doc = parser.parseFromString(html, 'text/html');

          // Seletores de áreas de dados que devem ser atualizadas
          const selectors = [
            '.table-wrap',
            '.invoice-list',
            '.kanban',
            '.sla-meter',
            '.sla-list',
            '.topbar-alerts',
            '.dashboard-cards',
            '.route-hero',
            '.timeline'
          ];

          let swappedAny = false;

          selectors.forEach(selector => {
            const currentEl = document.querySelector(selector);
            const parsedEl = doc.querySelector(selector);

            if (currentEl && parsedEl) {
              // Apenas substitui se o usuário não estiver com foco (digitando) dentro daquele container
              if (!currentEl.contains(document.activeElement)) {
                currentEl.outerHTML = parsedEl.outerHTML;
                swappedAny = true;
              }
            }
          });

          if (swappedAny) {
            console.log('DOM atualizado em tempo real para o evento:', eventType);
            // Re-vincular os listeners específicos de toggles de KPI caso estejamos na tela de SLA
            if (window.location.pathname.startsWith('/sla')) {
              initSlaToggleText();
            }
             if (window.updateActiveInvoiceCard) {
               window.updateActiveInvoiceCard();
             }
             if (window.applyCurrentSlaFilter) {
               window.applyCurrentSlaFilter();
             }
           }
         })
        .catch(err => console.error('Erro na atualização em tempo real do DOM:', err));
    };

    const initSlaToggleText = () => {
      const moreKpisDetails = document.querySelector('.more-kpis-details');
      if (!moreKpisDetails) return;
      const summary = moreKpisDetails.querySelector('summary');
      if (!summary) return;
      const originalText = summary.textContent;
      moreKpisDetails.addEventListener('toggle', () => {
        if (moreKpisDetails.open) {
          summary.textContent = 'Ocultar indicadores secundários';
          summary.classList.add('active');
        } else {
          summary.textContent = originalText;
          summary.classList.remove('active');
        }
      });
    };

    // --- SLA Tab Filtering ---
    const applyCurrentSlaFilter = () => {
      if (window.location.pathname !== '/sla') return;
      const filter = window.currentSlaFilter || 'all';
      
      // Atualiza estado ativo dos botões de abas
      document.querySelectorAll('.btn-sla-tab').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-filter') === filter);
      });
      
      // Filtra as linhas da tabela
      const rows = document.querySelectorAll('.accordion-panel table tbody tr');
      rows.forEach(row => {
        if (filter === 'all') {
          row.style.display = '';
        } else {
          row.style.display = row.classList.contains(filter) ? '' : 'none';
        }
      });
    };

    window.applyCurrentSlaFilter = applyCurrentSlaFilter;

    document.body.addEventListener('click', e => {
      const tab = e.target.closest('.btn-sla-tab');
      if (tab) {
        e.preventDefault();
        window.currentSlaFilter = tab.getAttribute('data-filter');
        applyCurrentSlaFilter();
      }
    });

    // Inicializar conexão e filtros
    applyCurrentSlaFilter();
    connectSSE();
  })();

});
