'use strict';

const DriverApp = {
  getApiBase() {
    const custom = localStorage.getItem('driver_custom_api_base');
    if (custom) {
      try {
        const customUrl = new URL(custom);
        const currentUrl = new URL(window.location.origin);
        if (customUrl.hostname !== currentUrl.hostname && !window.location.origin.includes('file://')) {
          return window.location.origin;
        }
      } catch (_) {}
      return custom.replace(/\/+$/, '');
    }
    return window.location.origin;
  },
  setApiBase(base) {
    if (!base) base = window.location.origin;
    base = base.replace(/\/+$/, '');
    localStorage.setItem('driver_custom_api_base', base);
    this.API_BASE = base;
    return base;
  },
  token() {
    return this.session?.token || '';
  },
  API_BASE: window.location.origin,
  SESSION_KEY: 'driver_session_v2',
  DB_NAME: 'logistica_driver_offline_v2',
  DB_VERSION: 2,
  db: null,
  session: null,
  routes: [],
  currentRoute: null,
  selectedOrder: null,
  photoData: '',
  signatureData: '',
  odometerStartPhotoData: '',
  odometerEndPhotoData: '',
  driverSignatureData: '',
  signatureMode: 'receiver',
  signatureDirty: false,
  drawing: false,
  confirmResolve: null,
  historyFilterPeriod: 'all',
  historyData: [],
  serverReachable: true,
  syncInProgress: false,
  operationInProgress: false,
  queuedKeys: new Set(),

  el(id) { return document.getElementById(id); },
  escape(value) {
    return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
  },
  safeHttpUrl(value, fallback = '') {
    if (!String(value || '').trim()) return fallback;
    try {
      const url = new URL(String(value || ''), window.location.origin);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : fallback;
    } catch (_) { return fallback; }
  },
  uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`;
  },
  setBusy(button, busy, label) {
    if (!button) return;
    if (!button.dataset.label) button.dataset.label = button.textContent;
    button.disabled = busy;
    button.textContent = busy ? label : button.dataset.label;
  },
  toast(message, type = '') {
    const item = document.createElement('div');
    item.className = `toast ${type}`;
    item.textContent = message;
    const openDialog = document.querySelector('dialog[open]');
    if (openDialog) {
      let stack = openDialog.querySelector('.dialog-toast-stack');
      if (!stack) {
        stack = document.createElement('div');
        stack.className = 'dialog-toast-stack';
        stack.style.cssText = 'position:fixed; z-index:99999; left:12px; right:12px; top:65px; display:grid; gap:8px; pointer-events:none;';
        openDialog.appendChild(stack);
      }
      stack.appendChild(item);
    } else {
      this.el('toastStack').appendChild(item);
    }
    setTimeout(() => item.remove(), 4500);
  },
  showError(id, message = '') {
    const box = this.el(id);
    box.textContent = message;
    box.classList.toggle('hidden', !message);
  },
  openModal(id) {
    const dialog = this.el(id);
    if (!dialog) return;
    try {
      if (typeof dialog.showModal === 'function') {
        if (!dialog.open) dialog.showModal();
      } else {
        dialog.setAttribute('open', '');
      }
    } catch (_) {
      dialog.setAttribute('open', '');
    }
  },

  closeModal(id) {
    const dialog = this.el(id);
    if (!dialog) return;
    try {
      if (typeof dialog.close === 'function') {
        if (dialog.open) dialog.close();
      } else {
        dialog.removeAttribute('open');
      }
    } catch (_) {
      dialog.removeAttribute('open');
    }
  },

  ask(title, message, acceptLabel = 'Confirmar') {
    if (this.confirmResolve) {
      try { this.confirmResolve(false); } catch (_) {}
      this.confirmResolve = null;
    }
    const t = this.el('confirmTitle');
    const m = this.el('confirmMessage');
    const a = this.el('confirmAccept');
    if (t) t.textContent = title;
    if (m) m.textContent = message;
    if (a) a.textContent = acceptLabel;
    const dlg = this.el('confirmDialog');
    if (dlg) dlg.style.zIndex = '999999';
    this.openModal('confirmDialog');
    return new Promise(resolve => { this.confirmResolve = resolve; });
  },

  activeRouteTab: 'departure',
  activeRouteTabManual: false,

  switchRouteTab(tabName, manual = false) {
    if (manual) this.activeRouteTabManual = true;
    this.activeRouteTab = tabName;
    const tabs = {
      departure: { btn: 'tabDepartureBtn', panel: 'stageDeparturePanel' },
      deliveries: { btn: 'tabDeliveriesBtn', panel: 'stageDeliveriesPanel' },
      arrival: { btn: 'tabArrivalBtn', panel: 'stageArrivalPanel' }
    };
    Object.keys(tabs).forEach(k => {
      const b = this.el(tabs[k].btn);
      const p = this.el(tabs[k].panel);
      if (b) b.classList.toggle('active', k === tabName);
      if (p) p.classList.toggle('hidden', k !== tabName);
    });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  },

  async init() {
    try {
      localStorage.removeItem('driver_last_known_gps');
      localStorage.removeItem('driver_saved_custom_pos');
    } catch (_) {}
    this.API_BASE = this.getApiBase();
    this.startGpsTracking();
    this.bindEvents();
    window.driverApp = this;
    await this.openDatabase();
    await this.recoverInterruptedOperations();
    await this.cleanupSentOperations();
    this.restoreSession();
    await this.updateQueueCounters();
    this.updateConnectionStatus();
    if (!this.session?.token || this.sessionExpired()) await this.loadDrivers();
    if (this.session?.must_change_password) this.showScreen('passwordScreen');
    else if (this.session?.token && !this.sessionExpired()) {
      this.showScreen('dashboardScreen');
      await this.loadRoutes();
      this.syncQueue();
    } else {
      this.clearSession();
      this.showScreen('loginScreen');
    }
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/static/driver_app/sw.js').catch(() => {});
    
    // Watchdog 24/7 Anti-Travamento: Testa conectividade silenciosamente e mantém fila ativa
    setInterval(async () => {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 6000);
        const res = await fetch(`${this.API_BASE}/healthz`, {
          headers: { 'ngrok-skip-browser-warning': 'true' },
          signal: controller.signal
        });
        clearTimeout(timeoutId);
        this.serverReachable = res.ok;
      } catch (_) {
        this.serverReachable = false;
      }
      this.updateConnectionStatus();
      if (navigator.onLine && this.session?.token && !this.syncInProgress) {
        this.syncQueue();
      }
    }, 25000);
  },

  bindEvents() {
    this.el('loginForm').addEventListener('submit', event => { event.preventDefault(); this.login(); });
    this.el('passwordForm').addEventListener('submit', event => { event.preventDefault(); this.changePassword(); });
    this.el('refreshButton').addEventListener('click', () => this.loadRoutes());
    this.el('syncButton').addEventListener('click', () => this.syncQueue(true));
    this.el('navSync').addEventListener('click', () => this.syncQueue(true));
    this.el('navHome').addEventListener('click', () => { this.showScreen('dashboardScreen'); this.loadRoutes(); });
    const navHist = this.el('navHistory');
    if (navHist) navHist.addEventListener('click', () => { this.showScreen('historyScreen'); this.loadDriverHistory(); });
    this.el('navLogout').addEventListener('click', () => this.logout());
    this.el('backButton').addEventListener('click', () => { this.showScreen('dashboardScreen'); this.loadRoutes(); });
    this.el('startRouteButton').addEventListener('click', () => this.startRoute());
    this.el('registerArrivalButton').addEventListener('click', () => this.registerArrival());

    const refreshHist = this.el('refreshHistoryButton');
    if (refreshHist) refreshHist.addEventListener('click', () => this.loadDriverHistory());
    const clearHist = this.el('btnClearHistoryFilter');
    if (clearHist) clearHist.addEventListener('click', () => {
      this.historyFilterPeriod = 'all';
      if (this.el('historyDateInput')) this.el('historyDateInput').value = '';
      if (this.el('historySearchInput')) this.el('historySearchInput').value = '';
      this.updateHistoryFilterChips();
      this.loadDriverHistory();
    });
    const dateHist = this.el('historyDateInput');
    if (dateHist) dateHist.addEventListener('change', () => {
      this.historyFilterPeriod = '';
      this.updateHistoryFilterChips();
      this.loadDriverHistory();
    });
    const searchHist = this.el('historySearchInput');
    if (searchHist) searchHist.addEventListener('input', () => this.filterHistoryList());

    document.querySelectorAll('.chip-filter').forEach(chip => {
      chip.addEventListener('click', () => {
        this.historyFilterPeriod = chip.dataset.period;
        if (this.el('historyDateInput')) this.el('historyDateInput').value = '';
        this.updateHistoryFilterChips();
        this.loadDriverHistory();
      });
    });

    // Abas do Fluxo por Etapas (Stepper)
    const tabDep = this.el('tabDepartureBtn');
    if (tabDep) tabDep.addEventListener('click', () => this.switchRouteTab('departure', true));
    const tabDel = this.el('tabDeliveriesBtn');
    if (tabDel) tabDel.addEventListener('click', () => this.switchRouteTab('deliveries', true));
    const tabArr = this.el('tabArrivalBtn');
    if (tabArr) tabArr.addEventListener('click', () => this.switchRouteTab('arrival', true));

    // Botões de Avanço entre Etapas
    const btnToDel = this.el('btnGoToDeliveriesFromDeparture');
    if (btnToDel) btnToDel.addEventListener('click', () => this.switchRouteTab('deliveries', true));
    const btnToArr = this.el('btnGoToArrivalFromDeliveries');
    if (btnToArr) btnToArr.addEventListener('click', () => this.switchRouteTab('arrival', true));

    const odoStartInp = this.el('odometerStartPhotoInput');
    if (odoStartInp) odoStartInp.addEventListener('change', e => this.processOdometerPhoto(e, 'start'));
    const odoEndInp = this.el('odometerEndPhotoInput');
    if (odoEndInp) odoEndInp.addEventListener('change', e => this.processOdometerPhoto(e, 'end'));

    const kmStartInp = this.el('kmStartInput');
    if (kmStartInp) kmStartInp.addEventListener('input', () => { this.updateKmTotalDisplay(); this.updateStartRouteButtonState(); });
    const kmEndInp = this.el('kmEndInput');
    if (kmEndInp) kmEndInp.addEventListener('input', () => this.updateKmTotalDisplay());

    const routeHint = this.el('startRouteHint');
    if (routeHint) {
      routeHint.addEventListener('click', () => {
        const kmStartInput = this.el('kmStartInput');
        const kmStartVal = parseFloat(kmStartInput ? kmStartInput.value : 0) || 0;
        if (kmStartVal <= 0 && kmStartInput) {
          kmStartInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
          kmStartInput.focus();
        } else if (!this.odometerStartPhotoData) {
          const photoInp = this.el('odometerStartPhotoInput');
          if (photoInp) photoInp.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      });
    }

    ['expLunchQty','expLunchUnit','expOvernightQty','expOvernightUnit','expFuelLitres','expFuelUnit','expTollTotal','expOtherTotal'].forEach(id => {
      const el = this.el(id);
      if (el) el.addEventListener('input', () => this.updateExpensesDisplay());
    });

    const btnOpenDriverSig = this.el('openDriverSignatureButton');
    if (btnOpenDriverSig) btnOpenDriverSig.addEventListener('click', () => this.openSignature('driver'));

    const btnOpenRouteMap = this.el('openRouteMapButton');
    if (btnOpenRouteMap) btnOpenRouteMap.addEventListener('click', () => this.openRouteMap());
    const btnCenterGps = this.el('btnCenterDriverGps');
    if (btnCenterGps) {
      btnCenterGps.addEventListener('click', async () => {
        btnCenterGps.disabled = true;
        btnCenterGps.style.opacity = '0.7';
        const origText = btnCenterGps.textContent;
        btnCenterGps.textContent = '⏳ Localizando...';
        this.userCenteredManually = true;
        this.toast('🎯 Buscando sua localização GPS em tempo real...', 'info');

        // Se já tiver qualquer coordenada conhecida, centraliza imediatamente para a tela não ficar parada!
        if (this.driverCurrentPos || this.lastKnownCoords) {
          const lat = this.driverCurrentPos ? this.driverCurrentPos[0] : this.lastKnownCoords.latitude;
          const lng = this.driverCurrentPos ? this.driverCurrentPos[1] : this.lastKnownCoords.longitude;
          if (lat != null && lng != null && this.driverMapInstance) {
            this.updateDriverGpsMarker(lat, lng, 'cached', this.lastKnownCoordsAccuracy || 20);
            this.driverMapInstance.setView([lat, lng], 17, { animate: true });
          }
        }

        try {
          const loc = await this.requestDriverGps(true, true);
          if (loc && loc.lat != null && loc.lng != null) {
            this.toast(`✓ Localizado na sua posição! (Precisão: ±${Math.round(loc.acc || 10)}m)`, 'ok');
          } else {
            const err = this.lastGeoError;
            if (err && err.code === 1) {
              this.toast('⚠️ GPS bloqueado. Ative a permissão de Localização nas Configurações do aparelho.', 'bad');
            } else if (err && err.code === 3) {
              this.toast('⏳ O satélite GPS demorou a responder. Use o botão "Marcar no Mapa" ou vá para um local mais aberto.', 'bad');
            } else {
              this.toast('⚠️ Sinal GPS não detectado. Verifique se o GPS está ativado nas configurações do celular.', 'bad');
            }
          }
        } catch (e) {
          this.toast('Erro ao buscar localização GPS: ' + (e.message || e), 'bad');
        } finally {
          btnCenterGps.disabled = false;
          btnCenterGps.style.opacity = '1';
          btnCenterGps.textContent = origText;
        }
      });
    }

    const btnDownloadOffline = this.el('btnDownloadOfflineMap');
    if (btnDownloadOffline) {
      btnDownloadOffline.addEventListener('click', () => this.downloadOfflineRouteMap());
    }

    const btnRefreshDeliveryGps = this.el('btnRefreshDeliveryGps');
    if (btnRefreshDeliveryGps) {
      btnRefreshDeliveryGps.addEventListener('click', async () => {
        btnRefreshDeliveryGps.disabled = true;
        btnRefreshDeliveryGps.textContent = '⏳ Buscando...';
        await this.refreshDeliveryGps(true);
        btnRefreshDeliveryGps.disabled = false;
        btnRefreshDeliveryGps.textContent = '🔄 Atualizar GPS';
      });
    }
    const btnManualPin = this.el('btnManualPinGps');
    if (btnManualPin) {
      btnManualPin.addEventListener('click', () => {
        this.toggleManualPinMode();
      });
    }
    const btnSwitchHttpsMap = this.el('btnSwitchHttpsFromMap');
    if (btnSwitchHttpsMap) {
      btnSwitchHttpsMap.addEventListener('click', () => {
        const host = location.hostname;
        window.location.href = `https://${host}:3443/static/driver_app/index.html`;
      });
    }

    const searchInput = this.el('mapAddressSearchInput');
    const btnSearchMap = this.el('btnMapAddressSearch');
    const executeMapSearch = async () => {
      const q = searchInput ? searchInput.value.trim() : '';
      if (!q) return this.toast('Digite um endereço, bairro ou cidade.', 'bad');
      this.toast(`🔍 Buscando "${q}"...`, 'info');
      try {
        const url = `https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(q + ', Brasil')}&limit=1`;
        const res = await fetch(url, { headers: { 'Accept-Language': 'pt-BR' } });
        if (res.ok) {
          const data = await res.json();
          if (data && data.length > 0) {
            const lat = parseFloat(data[0].lat);
            const lng = parseFloat(data[0].lon);
            this.driverCurrentPos = [lat, lng];
            this.lastKnownCoords = { latitude: lat, longitude: lng };
            localStorage.setItem('driver_saved_custom_pos', JSON.stringify({ lat, lng }));
            this.updateDriverGpsMarker(lat, lng, 'manual', 10);
            if (this.driverMapInstance) {
              this.driverMapInstance.setView([lat, lng], 16, { animate: true });
            }
            this.toast(`✓ Localizado: ${data[0].display_name.split(',')[0]}!`, 'ok');
            return;
          }
        }
        this.toast('Endereço não encontrado. Tente digitar de outra forma ou use "Marcar no Mapa".', 'bad');
      } catch (_) {
        this.toast('Não foi possível buscar o endereço online.', 'bad');
      }
    };
    if (btnSearchMap) btnSearchMap.addEventListener('click', executeMapSearch);
    if (searchInput) {
      searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          executeMapSearch();
        }
      });
    }

    const btnCloseRouteMap = this.el('closeRouteMap');
    if (btnCloseRouteMap) {
      btnCloseRouteMap.addEventListener('click', () => {
        if (this.driverWatchGpsId) {
          navigator.geolocation.clearWatch(this.driverWatchGpsId);
          this.driverWatchGpsId = null;
        }
        this.closeModal('routeMapDialog');
      });
    }

    // Modal de Alteração de Acesso Protegido por Senha H0llyw00d26
    const openAccessModal = () => {
      const dialog = this.el('accessDialog');
      if (!dialog) return;
      this.el('accessAuthSection').classList.remove('hidden');
      this.el('accessOptionsSection').classList.add('hidden');
      this.el('accessError').classList.add('hidden');
      this.el('accessError').textContent = '';
      this.el('accessPasswordInput').value = '';
      this.openModal('accessDialog');
      this.el('accessPasswordInput').focus();
    };

    const btnOpenAccessLogin = this.el('btnOpenAccessDialogLogin');
    if (btnOpenAccessLogin) btnOpenAccessLogin.addEventListener('click', openAccessModal);

    const btnOpenAccessNav = this.el('btnOpenAccessDialogNav');
    if (btnOpenAccessNav) btnOpenAccessNav.addEventListener('click', openAccessModal);

    const btnCloseAccess = this.el('closeAccessDialog');
    if (btnCloseAccess) btnCloseAccess.addEventListener('click', () => {
      this.closeModal('accessDialog');
    });

    const accDlg = this.el('accessDialog');
    if (accDlg) {
      accDlg.addEventListener('click', (e) => {
        if (e.target === accDlg) this.closeModal('accessDialog');
      });
    }

    const btnValidatePass = this.el('btnValidateAccessPass');
    if (btnValidatePass) {
      btnValidatePass.addEventListener('click', () => {
        const pass = this.el('accessPasswordInput').value.trim();
        if (pass === 'H0llyw00d26') {
          this.el('accessAuthSection').classList.add('hidden');
          this.el('accessOptionsSection').classList.remove('hidden');
          this.el('currentAccessInfo').textContent = this.API_BASE;
          this.el('customServerIpInput').value = this.API_BASE.replace(/^https?:\/\//, '');
        } else {
          this.el('accessError').textContent = 'Senha de segurança incorreta.';
          this.el('accessError').classList.remove('hidden');
        }
      });
    }

    const accessPassInput = this.el('accessPasswordInput');
    if (accessPassInput) {
      accessPassInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          this.el('btnValidateAccessPass').click();
        }
      });
    }

    const btnSwitchHttps = this.el('btnSwitchHttps');
    if (btnSwitchHttps) {
      btnSwitchHttps.addEventListener('click', async () => {
        let host = this.el('customServerIpInput').value.trim().replace(/^https?:\/\//, '').split(':')[0] || location.hostname;
        const targetUrl = `https://${host}:3443`;
        this.setApiBase(targetUrl);
        this.el('currentAccessInfo').textContent = targetUrl;
        this.toast(`Conectando em ${targetUrl}...`, 'info');
        const ok = await this.loadDrivers();
        if (ok) {
          this.toast(`✓ Conectado com sucesso em ${targetUrl}!`, 'ok');
          this.closeModal('accessDialog');
        } else {
          window.location.href = `${targetUrl}/static/driver_app/index.html`;
        }
      });
    }

    const btnSwitchHttp = this.el('btnSwitchHttp');
    if (btnSwitchHttp) {
      btnSwitchHttp.addEventListener('click', async () => {
        let host = this.el('customServerIpInput').value.trim().replace(/^https?:\/\//, '').split(':')[0] || location.hostname;
        const targetUrl = `http://${host}:3000`;
        this.setApiBase(targetUrl);
        this.el('currentAccessInfo').textContent = targetUrl;
        this.toast(`Conectando em ${targetUrl}...`, 'info');
        const ok = await this.loadDrivers();
        if (ok) {
          this.toast(`✓ Conectado com sucesso em ${targetUrl}!`, 'ok');
          this.closeModal('accessDialog');
        } else {
          window.location.href = `${targetUrl}/static/driver_app/index.html`;
        }
      });
    }

    const btnApplyCustomIp = this.el('btnApplyCustomIp');
    if (btnApplyCustomIp) {
      btnApplyCustomIp.addEventListener('click', async () => {
        let inputVal = this.el('customServerIpInput').value.trim();
        if (!inputVal) {
          return this.toast('Informe um IP ou domínio válido.', 'bad');
        }
        if (!inputVal.startsWith('http://') && !inputVal.startsWith('https://')) {
          const proto = location.protocol === 'https:' ? 'https:' : 'http:';
          const defaultPort = proto === 'https:' ? '3443' : '3000';
          if (!inputVal.includes(':')) {
            inputVal = `${proto}//${inputVal}:${defaultPort}`;
          } else {
            inputVal = `${proto}//${inputVal}`;
          }
        }
        this.setApiBase(inputVal);
        this.el('currentAccessInfo').textContent = this.API_BASE;
        this.toast(`Conectando em ${this.API_BASE}...`, 'info');
        const ok = await this.loadDrivers();
        if (ok) {
          this.toast(`✓ Conectado com sucesso em ${this.API_BASE}!`, 'ok');
          this.closeModal('accessDialog');
        } else {
          this.toast(`Tentando abrir conexão direta com ${this.API_BASE}...`, 'info');
          window.location.href = `${this.API_BASE}/static/driver_app/index.html`;
        }
      });
    }

    this.el('orderSearch').addEventListener('input', () => this.filterOrders());
    this.el('closeDelivery').addEventListener('click', () => this.closeModal('deliveryDialog'));
    const delDlg = this.el('deliveryDialog');
    if (delDlg) {
      delDlg.addEventListener('click', (e) => {
        if (e.target === delDlg) this.closeModal('deliveryDialog');
      });
    }
    this.el('photoInput').addEventListener('change', event => this.processPhoto(event));
    this.el('removePhoto').addEventListener('click', () => this.removePhoto());
    this.el('openSignature').addEventListener('click', () => this.openSignature());
    this.el('closeSignature').addEventListener('click', () => this.closeSignature(false));
    this.el('clearSignature').addEventListener('click', () => this.clearSignature());
    this.el('saveSignature').addEventListener('click', () => this.closeSignature(true));
    this.el('submitDelivery').addEventListener('click', () => this.submitOperation(false));
    this.el('submitProblem').addEventListener('click', () => this.submitOperation(true));
    this.el('confirmCancel').addEventListener('click', () => this.finishConfirm(false));
    this.el('confirmAccept').addEventListener('click', () => this.finishConfirm(true));
    const confDlg = this.el('confirmDialog');
    if (confDlg) {
      confDlg.addEventListener('cancel', (e) => {
        e.preventDefault();
        this.finishConfirm(false);
      });
      confDlg.addEventListener('click', (e) => {
        if (e.target === confDlg) this.finishConfirm(false);
      });
    }
    const docInput = this.el('deliveredDocument');
    const typeSelect = this.el('deliveredDocumentType');
    const applyMask = () => {
      if (!docInput) return;
      const type = typeSelect ? typeSelect.value : 'CPF';
      docInput.value = this.formatDocumentNumber(docInput.value, type);
      if (type === 'CPF') docInput.placeholder = '000.000.000-00';
      else if (type === 'CNPJ') docInput.placeholder = '00.000.000/0001-00';
      else if (type === 'RG') docInput.placeholder = '00.000.000-0';
      else docInput.placeholder = 'Número do documento';
    };
    if (docInput && typeSelect) {
      docInput.addEventListener('input', applyMask);
      typeSelect.addEventListener('change', applyMask);
    }
    window.addEventListener('online', () => { this.updateConnectionStatus(); this.toast('Conexão restabelecida. Sincronizando…', 'ok'); this.syncQueue(); });
    window.addEventListener('offline', () => { this.updateConnectionStatus(); this.toast('Sem internet. Novos registros ficarão no aparelho.'); });
    window.addEventListener('resize', () => { if (this.el('signatureDialog').open) this.resizeSignatureCanvas(true); });
    window.addEventListener('orientationchange', () => setTimeout(() => { if (this.el('signatureDialog').open) this.resizeSignatureCanvas(true); }, 250));
    document.addEventListener('visibilitychange', () => { if (!document.hidden && navigator.onLine) this.syncQueue(); });
  },

  formatDocumentNumber(value, docType) {
    if (!value) return '';
    if (docType === 'CPF') {
      const v = value.replace(/\D/g, '').slice(0, 11);
      if (v.length <= 3) return v;
      if (v.length <= 6) return `${v.slice(0,3)}.${v.slice(3)}`;
      if (v.length <= 9) return `${v.slice(0,3)}.${v.slice(3,6)}.${v.slice(6)}`;
      return `${v.slice(0,3)}.${v.slice(3,6)}.${v.slice(6,9)}-${v.slice(9)}`;
    }
    if (docType === 'CNPJ') {
      const v = value.replace(/\D/g, '').slice(0, 14);
      if (v.length <= 2) return v;
      if (v.length <= 5) return `${v.slice(0,2)}.${v.slice(2)}`;
      if (v.length <= 8) return `${v.slice(0,2)}.${v.slice(2,5)}.${v.slice(5)}`;
      if (v.length <= 12) return `${v.slice(0,2)}.${v.slice(2,5)}.${v.slice(5,8)}/${v.slice(8)}`;
      return `${v.slice(0,2)}.${v.slice(2,5)}.${v.slice(5,8)}/${v.slice(8,12)}-${v.slice(12)}`;
    }
    if (docType === 'RG') {
      const v = value.replace(/[^\w]/g, '').slice(0, 9);
      if (v.length <= 2) return v;
      if (v.length <= 5) return `${v.slice(0,2)}.${v.slice(2)}`;
      if (v.length <= 8) return `${v.slice(0,2)}.${v.slice(2,5)}.${v.slice(5)}`;
      return `${v.slice(0,2)}.${v.slice(2,5)}.${v.slice(5,8)}-${v.slice(8)}`;
    }
    return value;
  },

  finishConfirm(value) {
    this.closeModal('confirmDialog');
    const resolver = this.confirmResolve;
    this.confirmResolve = null;
    if (typeof resolver === 'function') {
      resolver(Boolean(value));
    }
  },
  updateConnectionStatus() {
    const node = this.el('connectionStatus');
    const online = navigator.onLine && this.serverReachable;
    node.classList.toggle('offline', !online);
    node.querySelector('span:last-child').textContent = online ? 'Online' : 'Offline';
  },
  showScreen(id) {
    ['loginScreen','passwordScreen','dashboardScreen','routeScreen','historyScreen'].forEach(screen => this.el(screen)?.classList.toggle('hidden', screen !== id));
    const authenticated = id === 'dashboardScreen' || id === 'routeScreen' || id === 'historyScreen';
    this.el('bottomNav')?.classList.toggle('hidden', !authenticated);
    this.el('navHome')?.classList.toggle('active', id === 'dashboardScreen');
    this.el('navHistory')?.classList.toggle('active', id === 'historyScreen');
    window.scrollTo(0, 0);
  },

  restoreSession() {
    try { this.session = JSON.parse(localStorage.getItem(this.SESSION_KEY) || 'null'); }
    catch (_) { this.session = null; }
  },
  saveSession(data) {
    this.session = data;
    localStorage.setItem(this.SESSION_KEY, JSON.stringify(data));
  },
  clearSession() {
    this.session = null;
    localStorage.removeItem(this.SESSION_KEY);
  },
  sessionExpired() {
    return !this.session?.expires_at || new Date(this.session.expires_at).getTime() <= Date.now();
  },

  async api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 15000);
    const headers = {'Accept':'application/json', 'ngrok-skip-browser-warning': 'true', ...(options.headers || {})};
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    if (this.session?.token && options.auth !== false) headers.Authorization = `Bearer ${this.session.token}`;
    try {
      const response = await fetch(`${this.API_BASE}${path}`, {...options, headers, signal: controller.signal});
      this.serverReachable = true;
      this.updateConnectionStatus();
      let data = {};
      try { data = await response.json(); } catch (_) { data = {ok:false, message:`Resposta inválida do servidor (HTTP ${response.status}).`}; }
      if (response.status === 401 && options.auth !== false) {
        this.clearSession();
        this.showScreen('loginScreen');
        this.toast(data.message || 'Sessão encerrada. Entre novamente.', 'bad');
      }
      if (data.code === 'password_change_required') {
        if (this.session) { this.session.must_change_password = true; this.saveSession(this.session); }
        this.showScreen('passwordScreen');
      }
      if (!response.ok) {
        const error = new Error(data.message || `Falha HTTP ${response.status}`);
        error.status = response.status;
        error.data = data;
        throw error;
      }
      return data;
    } catch (error) {
      if (!error.status) {
        this.serverReachable = false;
        this.updateConnectionStatus();
      }
      throw error;
    } finally { clearTimeout(timer); }
  },

  async loadDrivers() {
    const select = this.el('driverSelect');
    if (!select) return false;

    if (!select.dataset.retryBound) {
      select.dataset.retryBound = '1';
      select.addEventListener('focus', () => {
        if (!select.value || select.children.length <= 1) {
          this.loadDrivers();
        }
      });
    }

    // 1. Carregar do cache offline imediatamente para nunca ficar em branco
    let cached = [];
    try {
      const raw = localStorage.getItem('cached_driver_list');
      if (raw) cached = JSON.parse(raw);
    } catch (_) {}

    if (Array.isArray(cached) && cached.length > 0) {
      select.innerHTML = '<option value="">Selecione seu nome</option>' +
        cached.map(driver => `<option value="${Number(driver.id)}">${this.escape(driver.name)}</option>`).join('');
    }

    // 2. Buscar da API ativa
    try {
      let data = null;
      try {
        data = await this.api('/api/v1/driver/all_drivers', {auth:false, timeout: 6000});
      } catch (_) {
        data = await this.api('/api/v1/driver/drivers', {auth:false, timeout: 6000});
      }
      if (data && data.drivers && Array.isArray(data.drivers) && data.drivers.length > 0) {
        select.innerHTML = '<option value="">Selecione seu nome</option>' +
          data.drivers.map(driver => `<option value="${Number(driver.id)}">${this.escape(driver.name)}</option>`).join('');
        localStorage.setItem('cached_driver_list', JSON.stringify(data.drivers));
        this.showError('loginError', '');
        return true;
      } else if (cached.length === 0) {
        select.innerHTML = '<option value="">Nenhum motorista cadastrado</option>';
        return false;
      }
    } catch (err) {
      if (cached.length === 0) {
        select.innerHTML = '<option value="">Aguardando conexão (reconectando...)</option>';
        this.showError('loginError', 'Buscando lista de motoristas no servidor...');
        setTimeout(() => this.loadDrivers(), 2500);
        return false;
      }
    }
    return true;
  },
  async login() {
    const driverId = Number(this.el('driverSelect').value);
    const password = this.el('passwordInput').value;
    this.showError('loginError');
    if (!driverId || !password) return this.showError('loginError', 'Selecione o motorista e informe a senha.');
    const button = this.el('loginButton');
    this.setBusy(button, true, 'Entrando…');
    try {
      const data = await this.api('/api/v1/driver/login', {method:'POST', auth:false, body:JSON.stringify({driver_id:driverId, password})});
      this.el('passwordInput').value = '';
      this.saveSession({token:data.token, expires_at:data.expires_at, driver:data.driver, must_change_password:data.must_change_password});
      this.el('welcomeName').textContent = `Olá, ${data.driver.name}`;
      if (data.must_change_password) this.showScreen('passwordScreen');
      else { this.showScreen('dashboardScreen'); await this.loadRoutes(); this.syncQueue(); }
    } catch (error) { this.showError('loginError', error.message || 'Falha ao entrar.'); }
    finally { this.setBusy(button, false); }
  },
  async changePassword() {
    const password = this.el('newPassword').value;
    const confirmation = this.el('confirmPassword').value;
    this.showError('passwordError');
    if (password.trim().length < 8) return this.showError('passwordError', 'Use pelo menos 8 caracteres.');
    if (password !== confirmation) return this.showError('passwordError', 'As senhas não coincidem.');
    try {
      await this.api('/api/v1/driver/change_password', {method:'POST', body:JSON.stringify({new_password:password})});
      this.session.must_change_password = false;
      this.saveSession(this.session);
      this.el('newPassword').value = '';
      this.el('confirmPassword').value = '';
      this.toast('Nova senha salva.', 'ok');
      this.showScreen('dashboardScreen');
      await this.loadRoutes();
    } catch (error) { this.showError('passwordError', error.message); }
  },
  async logout() {
    if (!(await this.ask('Sair do aplicativo', 'Os registros offline permanecerão neste aparelho. Deseja encerrar a sessão?', 'Sair'))) return;
    try { await this.api('/api/v1/driver/logout', {method:'POST', body:'{}'}); } catch (_) {}
    this.clearSession();
    this.currentRoute = null;
    this.showScreen('loginScreen');
    this.toast('Sessão encerrada.');
  },

  async loadRoutes() {
    if (!this.session?.token) return;
    this.el('welcomeName').textContent = `Olá, ${this.session.driver.name}`;
    this.el('routesList').innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
    try {
      const data = await this.api('/api/v1/driver/routes');
      this.routes = data.routes || [];
      await this.putSnapshot(`routes:${Number(this.session.driver.id)}`, this.routes);
      this.renderRoutes();
    } catch (error) {
      const cached = error.status === 401 ? null : await this.getSnapshot(`routes:${Number(this.session.driver.id)}`);
      if (Array.isArray(cached)) {
        this.routes = cached;
        this.renderRoutes();
        this.toast('Exibindo cargas salvas no aparelho.', 'ok');
      } else {
        this.el('routesList').innerHTML = `<div class="card empty">${this.escape(error.message || 'Não foi possível carregar suas cargas.')}</div>`;
      }
    }
  },
  renderRoutes() {
    const total = this.routes.reduce((sum, route) => sum + Number(route.total_orders || 0), 0);
    const delivered = this.routes.reduce((sum, route) => sum + Number(route.delivered_orders || 0), 0);
    const problems = this.routes.reduce((sum, route) => sum + Number(route.problem_orders || 0), 0);
    const done = delivered + problems;
    this.el('statRoutes').textContent = this.routes.length;
    this.el('statStops').textContent = Math.max(0, total - done);
    this.el('statProgress').textContent = `${total ? Math.round(done * 100 / total) : 0}%`;
    if (!this.routes.length) {
      this.el('routesList').innerHTML = '<div class="card empty"><b>Nenhuma carga ativa.</b><br>Quando uma carga for atribuída a você, ela aparecerá aqui.</div>';
      return;
    }
    this.el('routesList').innerHTML = this.routes.map(route => {
      const totalOrders = Number(route.total_orders || 0);
      const done = Number(route.delivered_orders || 0) + Number(route.problem_orders || 0);
      const pct = totalOrders ? Math.round(done * 100 / totalOrders) : 0;
      const badge = route.status === 'Planejada' ? 'badge-planned' : 'badge-route';
      return `<article class="card route-card" data-route-id="${Number(route.id)}"><div class="route-top"><div><h3>${this.escape(route.name)}</h3><span class="muted">${this.escape(route.vehicle_name || 'Veículo não informado')} ${route.plate ? `• ${this.escape(route.plate)}` : ''}</span></div><span class="badge ${badge}">${this.escape(route.status)}</span></div><div class="progress"><i style="width:${pct}%"></i></div><div class="route-meta"><span>${done} de ${totalOrders} paradas concluídas</span><b>${pct}%</b></div></article>`;
    }).join('');
    this.el('routesList').querySelectorAll('[data-route-id]').forEach(card => card.addEventListener('click', () => this.openRoute(Number(card.dataset.routeId))));
  },

  async openRoute(routeId) {
    this.activeRouteTabManual = false;
    this.showScreen('routeScreen');
    this.el('ordersList').innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
    try {
      const data = await this.api(`/api/v1/driver/route/${routeId}`);
      this.currentRoute = data.route;
      await this.putSnapshot(`route:${Number(this.session.driver.id)}:${Number(routeId)}`, this.currentRoute);
      this.renderRoute();
    } catch (error) {
      const cached = error.status === 401 ? null : await this.getSnapshot(`route:${Number(this.session.driver.id)}:${Number(routeId)}`);
      if (cached) {
        this.currentRoute = cached;
        this.renderRoute();
        this.toast('Exibindo paradas salvas no aparelho.', 'ok');
      } else {
        this.el('ordersList').innerHTML = `<div class="card empty">${this.escape(error.message)}</div>`;
      }
    }
  },
  renderRoute() {
    const route = this.currentRoute;
    if (!route) return;
    const orders = route.orders || [];
    const done = orders.filter(order => ['Acertado','Problema'].includes(order.order_status) || ['Entregue','Com problema'].includes(order.route_order_status)).length;
    const pending = orders.length - done;
    const remainingWeight = orders.filter(order => !['Acertado','Problema'].includes(order.order_status)).reduce((sum, order) => sum + Number(order.weight_kg || 0), 0);
    const pct = orders.length ? Math.round(done * 100 / orders.length) : 0;
    const allDone = orders.length > 0 && pending === 0;

    if (this.el('routeTitle')) this.el('routeTitle').textContent = route.name;
    if (this.el('routeSubtitle')) this.el('routeSubtitle').textContent = `${route.vehicle_name || 'Veículo não informado'}${route.plate ? ` • ${route.plate}` : ''}`;

    const badgeClass = route.status === 'Planejada' ? 'badge-planned' : (route.status === 'Em rota' ? 'badge-route' : (route.status === 'Aguardando Chegada' ? 'badge-planned' : 'badge-ok'));
    const badgeTop = this.el('routeStatusBadgeTop');
    if (badgeTop) {
      badgeTop.textContent = route.status;
      badgeTop.className = `badge ${badgeClass}`;
    }
    const statusText = this.el('routeStatusText');
    if (statusText) statusText.textContent = `Status: ${route.status}`;
    if (this.el('routeStatus')) {
      this.el('routeStatus').textContent = route.status;
      this.el('routeStatus').className = `badge ${badgeClass}`;
    }

    if (this.el('routeWeight')) this.el('routeWeight').textContent = `${remainingWeight.toFixed(1)} kg`;
    if (this.el('routePendingCount')) this.el('routePendingCount').textContent = `${pending} parada${pending === 1 ? '' : 's'} pendente${pending === 1 ? '' : 's'}`;
    if (this.el('routePendingCountDeliveries')) this.el('routePendingCountDeliveries').textContent = pending;
    if (this.el('tabDeliveriesCountBadge')) this.el('tabDeliveriesCountBadge').textContent = pending;

    if (this.el('routeProgressBar')) this.el('routeProgressBar').style.width = `${pct}%`;
    if (this.el('routeProgressText')) this.el('routeProgressText').textContent = `${pct}% concluído (${done}/${orders.length})`;
    if (this.el('routeProgress')) this.el('routeProgress').textContent = `${pct}%`;
    if (this.el('routePending')) this.el('routePending').textContent = pending;

    const isPlanned = route.status === 'Planejada';
    const depCompletedCard = this.el('departureCompletedCard');
    const depFormCard = this.el('departureFormCard');
    if (depCompletedCard && depFormCard) {
      if (isPlanned) {
        depCompletedCard.classList.add('hidden');
        depFormCard.classList.remove('hidden');
      } else {
        depCompletedCard.classList.remove('hidden');
        depFormCard.classList.add('hidden');
      }
    }

    const allDoneCard = this.el('allDeliveriesDoneCard');
    if (allDoneCard) {
      allDoneCard.classList.toggle('hidden', !allDone);
    }

    const arrivalNotice = this.el('arrivalPendingOrdersNotice');
    if (arrivalNotice) {
      arrivalNotice.classList.toggle('hidden', allDone || isPlanned);
    }

    // Navega automaticamente para a etapa correspondente
    if (!this.activeRouteTabManual) {
      if (isPlanned) {
        this.switchRouteTab('departure');
      } else if (allDone) {
        this.switchRouteTab('arrival');
      } else {
        this.switchRouteTab('deliveries');
      }
    } else {
      this.switchRouteTab(this.activeRouteTab || 'departure');
    }

    this.updateStartRouteButtonState();
    const btnArr = this.el('registerArrivalButton');
    if (btnArr) {
      btnArr.classList.toggle('hidden', route.status === 'Acertada' || route.status === 'Com problema');
    }
    this.renderOrders(orders);
    this.renderTripSheet(route, allDone);
  },

  renderTripSheet(route, allDone) {
    if (!this.el('kmStartInput') && !this.el('kmEndInput')) return;

    if (!this.currentRouteLoadedId || this.currentRouteLoadedId !== route.id) {
      this.currentRouteLoadedId = route.id;
      this.odometerStartPhotoData = '';
      this.odometerEndPhotoData = '';
      this.driverSignatureData = '';
    }

    const kmStartInput = this.el('kmStartInput');
    const destinationInput = this.el('destinationInput');
    const helperNameInput = this.el('helperNameInput');
    const kmEndInput = this.el('kmEndInput');
    const kmTotalInput = this.el('kmTotalInput');
    const departureBadge = this.el('tripDepartureStatusBadge');
    const arrivalBadge = this.el('tripArrivalStatusBadge');

    if (kmStartInput && route.km_start != null && route.km_start !== '') kmStartInput.value = route.km_start;
    if (destinationInput && route.destination) destinationInput.value = route.destination;
    if (helperNameInput && route.helper_name) helperNameInput.value = route.helper_name;
    if (kmEndInput && route.km_end != null && route.km_end !== '') kmEndInput.value = route.km_end;
    if (kmTotalInput && route.km_total != null && route.km_total !== '') kmTotalInput.value = route.km_total;

    if (route.odometer_start_photo) {
      const pr = this.el('odometerStartPreview');
      if (pr) { pr.src = route.odometer_start_photo; pr.classList.remove('hidden'); }
      const st = this.el('odometerStartStatus');
      if (st) { st.textContent = '✅ Odômetro inicial registrado no sistema.'; st.style.color = '#166534'; }
    }
    if (route.odometer_end_photo) {
      const pr = this.el('odometerEndPreview');
      if (pr) { pr.src = route.odometer_end_photo; pr.classList.remove('hidden'); }
      const st = this.el('odometerEndStatus');
      if (st) { st.textContent = '✅ Odômetro final registrado no sistema.'; st.style.color = '#166534'; }
    }
    if (route.driver_signature) {
      const pr = this.el('driverSignaturePreview');
      if (pr) { pr.src = route.driver_signature; pr.classList.remove('hidden'); }
      const st = this.el('driverSignatureStatus');
      if (st) { st.textContent = '✅ Ficha assinada pelo motorista.'; st.style.color = '#166534'; }
    }

    const isPlanned = route.status === 'Planejada';
    const isEnRoute = route.status === 'Em rota';
    const isWaitingArrival = route.status === 'Aguardando Chegada';
    const isFinalized = route.status === 'Acertada' || route.status === 'Com problema';

    if (isPlanned) {
      if (departureBadge) { departureBadge.textContent = 'Obrigatório na Saída'; departureBadge.className = 'badge badge-planned'; }
      if (arrivalBadge) { arrivalBadge.textContent = 'Aguardando Início'; arrivalBadge.className = 'badge badge-planned'; }
      if (kmStartInput) kmStartInput.readOnly = false;
      if (destinationInput) destinationInput.readOnly = false;
      if (helperNameInput) helperNameInput.readOnly = false;
      if (this.el('odometerStartPhotoInput')) this.el('odometerStartPhotoInput').disabled = false;
      if (kmEndInput) kmEndInput.disabled = true;
      if (this.el('odometerEndPhotoInput')) this.el('odometerEndPhotoInput').disabled = true;
      if (this.el('openDriverSignatureButton')) this.el('openDriverSignatureButton').disabled = true;
    } else if (isEnRoute || isWaitingArrival) {
      if (departureBadge) { departureBadge.textContent = '✅ Saída Registrada'; departureBadge.className = 'badge badge-ok'; }
      if (arrivalBadge) {
        arrivalBadge.textContent = allDone ? '🏁 Pronto p/ Encerrar' : 'Entregas em andamento';
        arrivalBadge.className = allDone ? 'badge badge-ok' : 'badge badge-route';
      }
      if (kmStartInput) kmStartInput.readOnly = true;
      if (destinationInput) destinationInput.readOnly = true;
      if (helperNameInput) helperNameInput.readOnly = true;
      if (this.el('odometerStartPhotoInput')) this.el('odometerStartPhotoInput').disabled = true;
      if (kmEndInput) kmEndInput.disabled = false;
      if (this.el('odometerEndPhotoInput')) this.el('odometerEndPhotoInput').disabled = false;
      if (this.el('openDriverSignatureButton')) this.el('openDriverSignatureButton').disabled = false;
    } else if (isFinalized) {
      if (departureBadge) { departureBadge.textContent = '✅ Saída Registrada'; departureBadge.className = 'badge badge-ok'; }
      if (arrivalBadge) { arrivalBadge.textContent = '✅ Viagem Concluída'; arrivalBadge.className = 'badge badge-ok'; }
      if (kmStartInput) kmStartInput.readOnly = true;
      if (destinationInput) destinationInput.readOnly = true;
      if (helperNameInput) helperNameInput.readOnly = true;
      if (this.el('odometerStartPhotoInput')) this.el('odometerStartPhotoInput').disabled = true;
      if (kmEndInput) kmEndInput.readOnly = true;
      if (kmTotalInput) kmTotalInput.readOnly = true;
      if (this.el('odometerEndPhotoInput')) this.el('odometerEndPhotoInput').disabled = true;
      if (this.el('openDriverSignatureButton')) this.el('openDriverSignatureButton').disabled = true;
    }
    this.updateStartRouteButtonState();
  },
  renderOrders(orders) {
    if (!orders.length) { this.el('ordersList').innerHTML = '<div class="card empty">Esta carga não possui pedidos.</div>'; return; }
    this.el('ordersList').innerHTML = orders.map(order => {
      const done = order.order_status === 'Acertado' || order.route_order_status === 'Entregue';
      const problem = order.order_status === 'Problema' || order.route_order_status === 'Com problema';
      const orderKey = `${Number(this.currentRoute.id)}:${Number(order.order_id)}`;
      const queued = this.queuedKeys && this.queuedKeys.has(orderKey);
      const isFailed = this.failedKeys && this.failedKeys.has(orderKey);
      const fullAddress = order.delivery_address || order.client_full_address || '';
      const phone = String(order.client_phone || '').replace(/\D/g, '');
      let whatsapp = String(order.client_whatsapp || order.client_phone || '').replace(/\D/g, '');
      if (whatsapp && !whatsapp.startsWith('55')) whatsapp = `55${whatsapp}`;
      
      const isRuralVerified = !!order.is_rural_verified;
      const lat = order.delivery_latitude || order.latitude;
      const lng = order.delivery_longitude || order.longitude;
      const hasCoords = (lat && lng && Number(lat) !== 0 && Number(lng) !== 0);

      const search = `${order.client_name || ''} ${order.farm_name || ''} ${order.order_number || ''} ${order.city || ''} ${fullAddress}`.toLowerCase();
      const items = (order.items || []).map(item => `<li>${this.escape(item.product_name)} — ${Number(item.quantity || 0)} ${this.escape(item.unit || 'un')} (${Number(item.weight_kg || 0).toFixed(1)} kg)</li>`).join('');
      
      const statusBadge = queued ? '<span class="badge badge-planned">Aguardando sincronização</span>' :
        isFailed ? '<span class="badge badge-problem" style="background:#fee2e2; color:#b91c1c; border-color:#fca5a5;">⚠️ Falha no envio</span>' :
        problem ? '<span class="badge badge-problem">Com problema</span>' :
        done ? '<span class="badge badge-ok">Entregue</span>' :
        '<span class="badge badge-planned">Pendente</span>';

      const ruralBadge = isRuralVerified ?
        '<span class="badge" style="background:#dcfce7; color:#166534; border:1px solid #86efac; font-size:10px; font-weight:700;">✅ GPS Fazenda Confirmado</span>' :
        '<span class="badge" style="background:#fef3c7; color:#92400e; border:1px solid #fcd34d; font-size:10px; font-weight:700;">⚠️ 1ª Entrega (Sem GPS Rural)</span>';

      let actionButtons = '';
      if (isRuralVerified && hasCoords) {
        const navUrl = `https://www.google.com/maps/dir/?api=1&destination=${lat},${lng}`;
        actionButtons = `
          <a class="btn btn-secondary btn-small" href="${this.escape(navUrl)}" target="_blank" rel="noopener" style="background:#f0fdf4; color:#166534; border-color:#86efac; font-weight:700;">🧭 Rota até Fazenda</a>
          <a class="btn btn-secondary btn-small" href="geo:${lat},${lng}?q=${lat},${lng}">🗺️ GPS App</a>
          ${phone ? `<a class="btn btn-secondary btn-small" href="tel:${phone}">📞 Ligar</a>` : ''}
          ${whatsapp ? `<a class="btn btn-secondary btn-small" href="https://wa.me/${whatsapp}" target="_blank" rel="noopener">WhatsApp</a>` : ''}
        `;
      } else {
        const waMsg = encodeURIComponent(`Olá, aqui é o motorista da Casa do Campo. Estou a caminho para realizar a entrega na fazenda ${order.farm_name || ''}. Poderia me enviar a localização pelo WhatsApp ou um ponto de referência?`);
        actionButtons = `
          ${whatsapp ? `<a class="btn btn-secondary btn-small" href="https://wa.me/${whatsapp}?text=${waMsg}" target="_blank" rel="noopener" style="background:#ecfdf5; color:#065f46; border-color:#a7f3d0; font-weight:700;">💬 Pedir Localização (WhatsApp)</a>` : ''}
          ${phone ? `<a class="btn btn-secondary btn-small" href="tel:${phone}">📞 Ligar</a>` : ''}
          <a class="btn btn-secondary btn-small" href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(order.city || '')}" target="_blank" rel="noopener" style="opacity:0.85; font-size:11px;">🏢 Ver Centro de ${this.escape(order.city || 'Cidade')}</a>
        `;
      }

      return `
        <article class="card order-card ${done ? 'done' : ''} ${problem ? 'problem' : ''}" data-search="${this.escape(search)}">
          <div class="order-top">
            <div>
              <h3>Pedido ${this.escape(order.order_number)}</h3>
              <b>${this.escape(order.client_name || 'Cliente')}</b>
            </div>
            <div style="display:flex; flex-direction:column; align-items:flex-end; gap:3px;">
              ${statusBadge}
              ${ruralBadge}
            </div>
          </div>
          <div class="address" style="margin:6px 0; line-height:1.4;">
            <div style="font-weight:700; font-size:13px; color:#1e293b;">🌾 ${this.escape(order.farm_name ? `Fazenda ${order.farm_name}` : 'Destino Rural')}</div>
            <div style="font-size:12px; color:#475569;">📍 ${this.escape(order.city || '')} ${fullAddress ? `· ${this.escape(fullAddress)}` : ''}</div>
            ${order.reference_point ? `<div style="background:#f8fafc; border:1px solid #e2e8f0; border-radius:5px; padding:4px 7px; margin-top:4px; font-size:11.5px; color:#334155;"><b>📌 Ponto de Referência:</b> ${this.escape(order.reference_point)}</div>` : ''}
            ${!isRuralVerified ? `<div style="background:#fffbeb; border:1px solid #fde68a; border-radius:5px; padding:4px 7px; margin-top:4px; font-size:11px; color:#92400e;"><i>ℹ️ Ainda não há rota traçada até a fazenda. Ao concluir a entrega, seu GPS salvará a localização exata para as próximas entregas.</i></div>` : ''}
          </div>
          <div class="contact-row" style="flex-wrap:wrap; gap:6px; margin:6px 0;">
            ${actionButtons}
          </div>
          ${items ? `<details class="items"><summary>Ver produtos (${order.items.length})</summary><ul>${items}</ul></details>` : ''}
          <div class="order-actions">
            ${(!queued || isFailed) && !done && !problem && (this.currentRoute.status === 'Em rota' || this.currentRoute.status === 'Aguardando Chegada') ? 
              (isFailed ? 
                `<button class="btn btn-warning" data-order-id="${Number(order.order_id)}" style="background:#dc2626; color:#fff; border-color:#b91c1c; font-weight:700;">⚠️ Corrigir e reenviar entrega</button>` : 
                `<button class="btn btn-primary" data-order-id="${Number(order.order_id)}">Finalizar esta parada</button>`) : ''}
          </div>
        </article>
      `;
    }).join('');
    this.el('ordersList').querySelectorAll('[data-order-id]').forEach(button => button.addEventListener('click', () => this.openDelivery(Number(button.dataset.orderId))));
  },
  filterOrders() {
    const term = this.el('orderSearch').value.toLowerCase().trim();
    this.el('ordersList').querySelectorAll('[data-search]').forEach(card => { card.style.display = !term || card.dataset.search.includes(term) ? '' : 'none'; });
  },
  updateKmTotalDisplay() {
    const kmStart = parseFloat(this.el('kmStartInput')?.value) || 0;
    const kmEnd = parseFloat(this.el('kmEndInput')?.value) || 0;
    if (kmEnd > 0 && kmStart > 0 && kmEnd >= kmStart) {
      const tot = kmEnd - kmStart;
      const inp = this.el('kmTotalInput');
      if (inp) inp.value = tot;
    }
  },

  updateExpensesDisplay() {
    const lunchQty = parseFloat(this.el('expLunchQty')?.value) || 0;
    const lunchUnit = parseFloat(this.el('expLunchUnit')?.value) || 0;
    const lunchTot = lunchQty * lunchUnit;
    if (this.el('expLunchTotal')) this.el('expLunchTotal').textContent = `R$ ${lunchTot.toFixed(2).replace('.', ',')}`;

    const ovQty = parseFloat(this.el('expOvernightQty')?.value) || 0;
    const ovUnit = parseFloat(this.el('expOvernightUnit')?.value) || 0;
    const ovTot = ovQty * ovUnit;
    if (this.el('expOvernightTotal')) this.el('expOvernightTotal').textContent = `R$ ${ovTot.toFixed(2).replace('.', ',')}`;

    const fuelL = parseFloat(this.el('expFuelLitres')?.value) || 0;
    const fuelU = parseFloat(this.el('expFuelUnit')?.value) || 0;
    const fuelTot = fuelL * fuelU;
    if (this.el('expFuelTotal')) this.el('expFuelTotal').textContent = `R$ ${fuelTot.toFixed(2).replace('.', ',')}`;

    const tollTot = parseFloat(this.el('expTollTotal')?.value) || 0;
    if (this.el('expTollTotalSpan')) this.el('expTollTotalSpan').textContent = `R$ ${tollTot.toFixed(2).replace('.', ',')}`;

    const otherTot = parseFloat(this.el('expOtherTotal')?.value) || 0;

    const grand = lunchTot + ovTot + fuelTot + tollTot + otherTot;
    const formatted = `R$ ${grand.toFixed(2).replace('.', ',')}`;
    if (this.el('expGrandTotal')) this.el('expGrandTotal').textContent = formatted;
    if (this.el('expGrandTotalBadge')) this.el('expGrandTotalBadge').textContent = formatted;
  },

  collectExpensesFromForm() {
    const list = [];
    const lunchQty = parseFloat(this.el('expLunchQty')?.value) || 0;
    const lunchUnit = parseFloat(this.el('expLunchUnit')?.value) || 0;
    if (lunchQty > 0 || lunchUnit > 0) {
      list.push({ type: 'Almoço', quantity: lunchQty, unit_value: lunchUnit, total_value: lunchQty * lunchUnit });
    }

    const ovQty = parseFloat(this.el('expOvernightQty')?.value) || 0;
    const ovUnit = parseFloat(this.el('expOvernightUnit')?.value) || 0;
    if (ovQty > 0 || ovUnit > 0) {
      list.push({ type: 'Diária de Pernoite', quantity: ovQty, unit_value: ovUnit, total_value: ovQty * ovUnit });
    }

    const fuelL = parseFloat(this.el('expFuelLitres')?.value) || 0;
    const fuelU = parseFloat(this.el('expFuelUnit')?.value) || 0;
    if (fuelL > 0 || fuelU > 0) {
      list.push({ type: 'Combustível', quantity: fuelL, unit_value: fuelU, total_value: fuelL * fuelU });
    }

    const tollTot = parseFloat(this.el('expTollTotal')?.value) || 0;
    if (tollTot > 0) {
      list.push({ type: 'Pedágio', quantity: 1, unit_value: tollTot, total_value: tollTot });
    }

    const otherTot = parseFloat(this.el('expOtherTotal')?.value) || 0;
    const otherDesc = (this.el('expOtherDesc')?.value || '').trim();
    if (otherTot > 0 || otherDesc) {
      list.push({ type: 'Outros', quantity: 1, unit_value: otherTot, total_value: otherTot, notes: otherDesc });
    }
    return list;
  },

  async loadImage(file) {
    if (typeof createImageBitmap === 'function') {
      try {
        // Redimensiona diretamente no decode nativo de GPU/Canvas, evitando estouro de RAM em câmeras de 50MP
        return await createImageBitmap(file, { resizeWidth: 1200, resizeQuality: 'medium' });
      } catch (_) {
        try {
          return await createImageBitmap(file);
        } catch (_) {}
      }
    }
    return new Promise((resolve, reject) => {
      let objectUrl = '';
      try {
        objectUrl = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
          URL.revokeObjectURL(objectUrl);
          resolve(img);
        };
        img.onerror = () => {
          URL.revokeObjectURL(objectUrl);
          const reader = new FileReader();
          reader.onload = () => {
            const fbImg = new Image();
            fbImg.onload = () => resolve(fbImg);
            fbImg.onerror = reject;
            fbImg.src = reader.result;
          };
          reader.onerror = reject;
          reader.readAsDataURL(file);
        };
        img.src = objectUrl;
      } catch (err) {
        const reader = new FileReader();
        reader.onload = () => {
          const img = new Image();
          img.onload = () => resolve(img);
          img.onerror = reject;
          img.src = reader.result;
        };
        reader.onerror = reject;
        reader.readAsDataURL(file);
      }
    });
  },

  async processOdometerPhoto(event, type) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      return this.toast('Selecione uma imagem válida.', 'bad');
    }
    const isStart = type === 'start';
    const previewEl = this.el(isStart ? 'odometerStartPreview' : 'odometerEndPreview');
    const statusEl = this.el(isStart ? 'odometerStartStatus' : 'odometerEndStatus');
    try {
      const source = await this.loadImage(file);
      const max = 1200;
      const sWidth = source.naturalWidth || source.width;
      const sHeight = source.naturalHeight || source.height;
      const scale = Math.min(1, max / Math.max(sWidth, sHeight));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(sWidth * scale));
      canvas.height = Math.max(1, Math.round(sHeight * scale));
      canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
      if (typeof source.close === 'function') source.close();
      const b64 = canvas.toDataURL('image/jpeg', 0.82);
      const kbytes = Math.round(b64.length * 0.75 / 1024);
      if (isStart) {
        this.odometerStartPhotoData = b64;
        this.updateStartRouteButtonState();
      } else {
        this.odometerEndPhotoData = b64;
      }
      if (previewEl) {
        previewEl.src = b64;
        previewEl.classList.remove('hidden');
      }
      if (statusEl) {
        statusEl.textContent = `✅ Foto do odômetro ${isStart ? 'inicial' : 'final'} pronta (${canvas.width}×${canvas.height}, ${kbytes} KB).`;
        statusEl.style.color = '#166534';
      }
      this.toast(`Foto do odômetro ${isStart ? 'inicial' : 'final'} capturada com sucesso!`, 'ok');
    } catch (e) {
      if (statusEl) {
        statusEl.textContent = '❌ Falha ao processar imagem. Tente novamente.';
        statusEl.style.color = '#dc2626';
      }
      this.toast('Não foi possível processar a foto. Tente novamente.', 'bad');
    }
  },

  updateStartRouteButtonState() {
    const btn = this.el('startRouteButton');
    const hint = this.el('startRouteHint');
    if (!btn) return;

    if (!this.currentRoute || this.currentRoute.status !== 'Planejada') {
      btn.classList.add('hidden');
      if (hint) hint.classList.add('hidden');
      return;
    }

    btn.classList.remove('hidden');

    const kmStartInput = this.el('kmStartInput');
    const kmStartVal = parseFloat(kmStartInput ? kmStartInput.value : 0) || 0;
    const hasPhoto = !!(this.odometerStartPhotoData || (this.currentRoute && this.currentRoute.odometer_start_photo));

    const isReady = (kmStartVal > 0 && hasPhoto);

    btn.disabled = false;
    btn.classList.toggle('btn-disabled-blocked', !isReady);

    if (!isReady) {
      const missing = [];
      if (kmStartVal <= 0) missing.push('KM Inicial');
      if (!hasPhoto) missing.push('Foto do Odômetro');
      const missingStr = missing.join(' e ');

      btn.innerHTML = `🔒 Registrar saída (Falta: ${missingStr})`;

      if (hint) {
        hint.classList.remove('hidden');
        hint.className = 'start-route-hint-blocked';
        hint.innerHTML = `🔒 <b>Passo Obrigatório:</b> Para liberar a saída da carga, preencha o <b>KM Inicial</b> e fotografe o <b>Odômetro Inicial</b> no formulário acima.`;
      }
    } else {
      btn.innerHTML = '🚀 Registrar Saída da Carga';

      if (hint) {
        hint.classList.remove('hidden');
        hint.className = 'start-route-hint-ready';
        hint.innerHTML = `✅ <b>Tudo pronto!</b> Toque no botão para registrar a saída da carga e iniciar o roteiro de entregas.`;
      }
    }
  },

  async startRoute() {
    if (!this.currentRoute) return;

    const kmStartInput = this.el('kmStartInput');
    const kmStartVal = parseFloat(kmStartInput ? kmStartInput.value : 0) || 0;
    const hasPhoto = !!(this.odometerStartPhotoData || (this.currentRoute && this.currentRoute.odometer_start_photo));

    if (kmStartVal <= 0 || !hasPhoto) {
      if (kmStartVal <= 0) {
        if (kmStartInput) {
          kmStartInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
          kmStartInput.focus();
          kmStartInput.style.borderColor = '#dc2626';
          kmStartInput.style.boxShadow = '0 0 0 3px rgba(220, 38, 38, 0.25)';
          setTimeout(() => {
            kmStartInput.style.borderColor = '';
            kmStartInput.style.boxShadow = '';
          }, 3000);
        }
        return this.toast('⚠️ Bloqueio obrigatório: Digite o KM Inicial do odômetro antes de sair.', 'bad');
      }

      if (!hasPhoto) {
        const photoInp = this.el('odometerStartPhotoInput');
        if (photoInp) {
          photoInp.scrollIntoView({ behavior: 'smooth', block: 'center' });
          photoInp.focus();
        }
        return this.toast('⚠️ Bloqueio obrigatório: Tire uma foto legível do odômetro inicial.', 'bad');
      }
    }

    const button = this.el('startRouteButton');
    this.setBusy(button, true, 'Registrando saída...');
    try {
      const helperName = (this.el('helperNameInput')?.value || '').trim();
      const destination = (this.el('destinationInput')?.value || '').trim();
      const payload = {
        endpoint: '/api/v1/driver/start_route',
        route_id: this.currentRoute.id,
        km_start: kmStartVal,
        odometer_start_photo: this.odometerStartPhotoData,
        helper_name: helperName,
        destination: destination,
        idempotency_key: this.uuid()
      };

      if (!navigator.onLine) {
        await this.enqueue(payload);
        this.currentRoute.status = 'Em rota';
        this.currentRoute.km_start = kmStartVal;
        this.toast('Saída da carga salva no aparelho! Será sincronizada quando a conexão voltar.', 'ok');
        this.activeRouteTabManual = false;
        this.renderRoute();
        this.switchRouteTab('deliveries');
        return;
      }

      try {
        const res = await this.api('/api/v1/driver/start_route', {
          method: 'POST',
          timeout: 45000,
          body: JSON.stringify({
            route_id: this.currentRoute.id,
            km_start: kmStartVal,
            odometer_start_photo: this.odometerStartPhotoData,
            helper_name: helperName,
            destination: destination
          })
        });
        this.toast(res.message || 'Saída da carga registrada com sucesso!', 'ok');
        this.activeRouteTabManual = false;
        await this.openRoute(this.currentRoute.id);
        this.switchRouteTab('deliveries');
      } catch (error) {
        if (!error.status || error.status >= 500) {
          await this.enqueue(payload, error.message);
          this.currentRoute.status = 'Em rota';
          this.currentRoute.km_start = kmStartVal;
          this.toast('Servidor instável. Saída guardada no aparelho para reenvio automático.', 'ok');
          this.activeRouteTabManual = false;
          this.renderRoute();
          this.switchRouteTab('deliveries');
        } else {
          throw error;
        }
      }
    } catch (error) {
      this.toast(error.message, 'bad');
    } finally {
      this.setBusy(button, false);
    }
  },

  async registerArrival() {
    if (!this.currentRoute) return;

    // Strict Gating: Verify all orders are delivered or problem
    const orders = this.currentRoute.orders || [];
    const done = orders.filter(order => ['Acertado','Problema'].includes(order.order_status) || ['Entregue','Com problema'].includes(order.route_order_status)).length;
    if (orders.length > 0 && done < orders.length) {
      return this.toast(`⚠️ Bloqueio: Existem ${orders.length - done} pedidos pendentes. Conclua todas as entregas antes de registrar chegada.`, 'bad');
    }

    // Strict Gating: Check KM Final
    const kmStartVal = parseFloat(this.el('kmStartInput')?.value) || parseFloat(this.currentRoute.km_start) || 0;
    const kmEndInput = this.el('kmEndInput');
    const kmEndVal = parseFloat(kmEndInput ? kmEndInput.value : 0) || 0;
    if (kmEndVal <= 0) {
      if (kmEndInput) kmEndInput.focus();
      return this.toast('⚠️ Bloqueio obrigatório: Digite o KM Final do odômetro para encerrar a viagem.', 'bad');
    }
    if (kmStartVal > 0 && kmEndVal <= kmStartVal) {
      if (kmEndInput) kmEndInput.focus();
      return this.toast(`⚠️ Bloqueio obrigatório: O KM Final (${kmEndVal}) deve ser maior que o KM Inicial (${kmStartVal}).`, 'bad');
    }

    // Strict Gating: Check Odometer End Photo
    if (!this.odometerEndPhotoData) {
      const photoInp = this.el('odometerEndPhotoInput');
      if (photoInp) photoInp.focus();
      return this.toast('⚠️ Bloqueio obrigatório: Tire uma foto legível do odômetro final para comprovar a quilometragem.', 'bad');
    }

    // Strict Gating: Check Driver Signature
    if (!this.driverSignatureData) {
      return this.toast('⚠️ Bloqueio obrigatório: Colete a assinatura do motorista na ficha antes de encerrar.', 'bad');
    }

    const button = this.el('registerArrivalButton');
    this.setBusy(button, true, 'Capturando GPS e salvando...');
    try {
      const coords = await this.getGpsCoords();
      const expenses = this.collectExpensesFromForm();
      const kmTotalVal = kmEndVal - kmStartVal;

      const payload = {
        route_id: this.currentRoute.id,
        km_end: kmEndVal,
        km_total: kmTotalVal,
        odometer_end_photo: this.odometerEndPhotoData,
        driver_signature: this.driverSignatureData,
        expenses: expenses,
        latitude: coords ? coords.latitude : null,
        longitude: coords ? coords.longitude : null
      };

      if (!navigator.onLine) {
        await this.enqueue({
          ...payload,
          endpoint: '/api/v1/driver/route/arrival',
          idempotency_key: this.uuid()
        });
        this.currentRoute.status = 'Acertada';
        this.currentRoute.km_end = kmEndVal;
        this.currentRoute.km_total = kmTotalVal;
        this.toast('Chegada na loja salva no aparelho! Será sincronizada quando a conexão voltar.', 'ok');
        this.showTripCompletedModal({ route_id: this.currentRoute.id }, kmStartVal, kmEndVal, kmTotalVal, expenses);
        return;
      }

      try {
        const res = await this.api('/api/v1/driver/route/arrival', {
          method: 'POST',
          body: JSON.stringify(payload)
        });

        this.toast(res.message || 'Chegada registrada com sucesso!', 'ok');
        this.showTripCompletedModal(res, kmStartVal, kmEndVal, kmTotalVal, expenses);
      } catch (error) {
        if (!error.status || error.status >= 500) {
          await this.enqueue({
            ...payload,
            endpoint: '/api/v1/driver/route/arrival',
            idempotency_key: this.uuid()
          }, error.message);
          this.currentRoute.status = 'Acertada';
          this.toast('Servidor instável. Encerramento guardado no aparelho para reenvio automático.', 'ok');
          this.showTripCompletedModal({ route_id: this.currentRoute.id }, kmStartVal, kmEndVal, kmTotalVal, expenses);
        } else {
          throw error;
        }
      }
    } catch (error) {
      this.toast(error.message || 'Não foi possível registrar a chegada.', 'bad');
    } finally {
      this.setBusy(button, false, '🏁 Registrar Chegada na Loja');
    }
  },

  showTripCompletedModal(res, kmStart, kmEnd, kmTotal, expenses) {
    const dialog = this.el('tripCompletedDialog');
    if (!dialog) return;

    const route = this.currentRoute || {};
    const driverName = this.session?.driver?.name || 'Motorista';
    const totalExpVal = (expenses || []).reduce((acc, ex) => acc + (Number(ex.total_value) || 0), 0);
    const deliveredCount = (route.orders || []).length;
    const token = this.session?.token || '';

    const summaryBox = this.el('tripSummaryBox');
    if (summaryBox) {
      summaryBox.innerHTML = `
        <div style="display:flex; justify-content:space-between; margin-bottom:4px;"><b>Carga:</b> <span>${this.escape(route.name || `#${res.route_id}`)}</span></div>
        <div style="display:flex; justify-content:space-between; margin-bottom:4px;"><b>Motorista:</b> <span>${this.escape(driverName)}</span></div>
        <div style="display:flex; justify-content:space-between; margin-bottom:4px;"><b>KM Inicial / Final:</b> <span>${kmStart.toLocaleString('pt-BR')} / ${kmEnd.toLocaleString('pt-BR')}</span></div>
        <div style="display:flex; justify-content:space-between; margin-bottom:4px;"><b>KM Total Rodado:</b> <span style="font-weight:700; color:#166534;">${kmTotal.toLocaleString('pt-BR')} km</span></div>
        <div style="display:flex; justify-content:space-between; margin-bottom:4px;"><b>Total de Entregas:</b> <span>${deliveredCount} pedido(s)</span></div>
        <div style="display:flex; justify-content:space-between;"><b>Total Despesas:</b> <span style="font-weight:700; color:#1e3a8a;">R$ ${totalExpVal.toFixed(2).replace('.', ',')}</span></div>
      `;
    }

    const btnWhatsApp = this.el('btnShareTripWhatsApp');
    if (btnWhatsApp) {
      btnWhatsApp.onclick = () => {
        const msg = [
          `🚚 *COMPROVANTE DE ENCERRAMENTO DE VIAGEM* 🚚`,
          `*Carga:* ${route.name || '#' + res.route_id}`,
          `*Motorista:* ${driverName}`,
          `*KM Inicial:* ${kmStart.toLocaleString('pt-BR')}`,
          `*KM Final:* ${kmEnd.toLocaleString('pt-BR')}`,
          `*KM Total Rodado:* ${kmTotal.toLocaleString('pt-BR')} km`,
          `*Entregas Concluídas:* ${deliveredCount} pedidos`,
          `*Total de Despesas:* R$ ${totalExpVal.toFixed(2).replace('.', ',')}`,
          `*Status:* Viagem Finalizada e Sincronizada com Sucesso!`
        ].join('\n');

        if (navigator.share) {
          navigator.share({
            title: `Encerramento de Viagem - ${route.name || 'Carga'}`,
            text: msg
          }).catch(() => {
            window.open(`https://wa.me/?text=${encodeURIComponent(msg)}`, '_blank');
          });
        } else {
          window.open(`https://wa.me/?text=${encodeURIComponent(msg)}`, '_blank');
        }
      };
    }

    const btnClose = this.el('btnCloseTripCompleted');
    if (btnClose) {
      btnClose.onclick = async () => {
        this.closeModal('tripCompletedDialog');
        this.showScreen('historyScreen');
        await this.loadDriverHistory();
      };
    }

    const tripDlg = this.el('tripCompletedDialog');
    if (tripDlg) {
      tripDlg.addEventListener('click', async (e) => {
        if (e.target === tripDlg) {
          this.closeModal('tripCompletedDialog');
          this.showScreen('historyScreen');
          await this.loadDriverHistory();
        }
      });
    }

    this.openModal('tripCompletedDialog');
  },

  openDelivery(orderId) {
    this.selectedOrder = (this.currentRoute?.orders || []).find(order => Number(order.order_id) === orderId);
    if (!this.selectedOrder) return;
    this.photoData = '';
    this.signatureData = '';
    this.signatureDirty = false;
    this.el('deliveryTitle').textContent = `Pedido ${this.selectedOrder.order_number}`;
    ['deliveredTo','deliveredDocument','deliveryNotes','problemNotes'].forEach(id => { this.el(id).value = ''; });
    this.el('problemType').selectedIndex = 0;
    this.removePhoto();
    this.el('signatureStatus').textContent = 'Assinatura ainda não coletada.';

    if (this.db) {
      this.allOperations().then(ops => {
        const owner = Number(this.session?.driver?.id || 0);
        const failedOp = ops.find(row =>
          Number(row.route_id) === Number(this.currentRoute?.id) &&
          Number(row.order_id) === orderId &&
          row.status === 'failed' &&
          (!Number(row.owner_driver_id || 0) || Number(row.owner_driver_id) === owner)
        );
        if (failedOp && failedOp.payload) {
          if (failedOp.payload.delivered_to && this.el('deliveredTo')) this.el('deliveredTo').value = failedOp.payload.delivered_to;
          if (failedOp.payload.delivered_document && this.el('deliveredDocument')) this.el('deliveredDocument').value = failedOp.payload.delivered_document;
          if (failedOp.payload.final_notes && this.el('deliveryNotes')) this.el('deliveryNotes').value = failedOp.payload.final_notes;
          if (failedOp.last_error) {
            this.toast(`Envio anterior pendente de revisão: ${failedOp.last_error}`, 'bad');
          }
        }
      }).catch(() => {});
    }

    const gpsBadge = this.el('gpsStatusBadge');
    const gpsText = this.el('gpsStatusBadgeText') || gpsBadge;
    if (gpsText) {
      if (this.lastKnownCoords && this.lastKnownCoords.latitude != null) {
        const accInfo = this.lastKnownCoordsAccuracy ? ` (±${Math.round(this.lastKnownCoordsAccuracy)}m)` : '';
        gpsText.textContent = `✓ GPS Exato Capturado: ${this.lastKnownCoords.latitude.toFixed(5)}, ${this.lastKnownCoords.longitude.toFixed(5)}${accInfo}`;
        if (gpsBadge) {
          gpsBadge.style.color = '#166534';
          gpsBadge.style.background = '#f0fdf4';
          gpsBadge.style.borderColor = '#86efac';
        }
      } else {
        gpsText.textContent = '⏳ Capturando sinal de GPS do dispositivo...';
        if (gpsBadge) {
          gpsBadge.style.color = '#0284c7';
          gpsBadge.style.background = '#f0f9ff';
          gpsBadge.style.borderColor = '#bae6fd';
        }
      }
    }
    this.refreshDeliveryGps(false);

    const orderItems = this.selectedOrder.items || [];
    const details = this.el('deliveryProductsDetails');
    const countSpan = this.el('deliveryProductsCount');
    const list = this.el('deliveryProductsList');

    if (countSpan) countSpan.textContent = orderItems.length;
    if (details) {
      details.style.display = orderItems.length ? 'block' : 'none';
      details.open = (orderItems.length > 0);
    }
    if (list) {
      if (!orderItems.length) {
        list.innerHTML = '<div style="font-size:12px; color:#64748b; font-style:italic;">Nenhum produto individual listado para este pedido.</div>';
      } else {
        list.innerHTML = orderItems.map((it, idx) => {
          const itId = it.id || idx;
          const isProb = (it.status === 'Problema');
          const notesVal = this.escape(it.problem_notes || '');
          return `
            <div id="driver_item_card_${itId}" class="driver-item-card" style="background:#fff; border:1.5px solid ${isProb ? '#ef4444' : '#cbd5e1'}; border-radius:10px; padding:10px 12px; font-size:13px; margin-bottom:8px; transition:all 0.2s;">
              <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px;">
                <div style="flex:1;">
                  <b style="color:#0f172a; font-size:13.5px;">${this.escape(it.product_name)}</b>
                  <div style="font-size:11.5px; color:#64748b; margin-top:3px; font-weight:600;">
                    📦 ${Number(it.quantity || 0)} ${this.escape(it.unit || 'un')} · ⚖️ ${Number(it.weight_kg || 0).toFixed(1)} kg
                  </div>
                </div>
                <div style="display:flex; gap:6px; flex-shrink:0;">
                  <label id="lbl_ok_${itId}" style="display:flex; align-items:center; gap:5px; font-size:12px; cursor:pointer; font-weight:750; color:#15803d; background:${!isProb ? '#dcfce7' : '#f1f5f9'}; border:1px solid ${!isProb ? '#86efac' : '#cbd5e1'}; padding:5px 9px; border-radius:8px;">
                    <input type="radio" name="driver_item_status_${itId}" value="Entregue" ${!isProb ? 'checked' : ''} data-item-id="${itId}"> OK
                  </label>
                  <label id="lbl_prob_${itId}" style="display:flex; align-items:center; gap:5px; font-size:12px; cursor:pointer; font-weight:750; color:#b91c1c; background:${isProb ? '#fee2e2' : '#f1f5f9'}; border:1px solid ${isProb ? '#fca5a5' : '#cbd5e1'}; padding:5px 9px; border-radius:8px;">
                    <input type="radio" name="driver_item_status_${itId}" value="Problema" ${isProb ? 'checked' : ''} data-item-id="${itId}"> Problema
                  </label>
                </div>
              </div>
              <div id="driver_item_wrap_${itId}" style="display:${isProb ? 'block' : 'none'}; margin-top:10px; padding:10px; background:#fff5f5; border:1px dashed #fca5a5; border-radius:8px;">
                <div style="font-size:11.5px; font-weight:750; color:#991b1b; margin-bottom:6px; display:flex; align-items:center; gap:5px;">
                  <span>⚠️</span> Selecione o motivo do problema ou digite abaixo:
                </div>
                <div style="display:flex; flex-wrap:wrap; gap:5px; margin-bottom:8px;">
                  <button type="button" class="btn-quick-reason" data-target="driver_item_notes_${itId}" data-reason="Mercadoria avariada / rasgada" style="border:1px solid #fca5a5; background:#ffffff; color:#991b1b; font-size:11px; font-weight:700; padding:4px 8px; border-radius:6px; cursor:pointer;">💥 Avaria / Rasgado</button>
                  <button type="button" class="btn-quick-reason" data-target="driver_item_notes_${itId}" data-reason="Mercadoria faltando / não veio no caminhão" style="border:1px solid #fca5a5; background:#ffffff; color:#991b1b; font-size:11px; font-weight:700; padding:4px 8px; border-radius:6px; cursor:pointer;">❌ Falta / Não Veio</button>
                  <button type="button" class="btn-quick-reason" data-target="driver_item_notes_${itId}" data-reason="Produto trocado / lote incorreto" style="border:1px solid #fca5a5; background:#ffffff; color:#991b1b; font-size:11px; font-weight:700; padding:4px 8px; border-radius:6px; cursor:pointer;">🔄 Produto Incorreto</button>
                  <button type="button" class="btn-quick-reason" data-target="driver_item_notes_${itId}" data-reason="Cliente recusou o recebimento deste item" style="border:1px solid #fca5a5; background:#ffffff; color:#991b1b; font-size:11px; font-weight:700; padding:4px 8px; border-radius:6px; cursor:pointer;">🚫 Recusa do Cliente</button>
                </div>
                <input type="text" id="driver_item_notes_${itId}" value="${notesVal}" placeholder="Descreva aqui o motivo obrigatório..." style="width:100%; box-sizing:border-box; border:1.5px solid #ef4444; background:#ffffff; font-size:12.5px; padding:8px 10px; border-radius:6px; outline:0; font-weight:500;">
              </div>
            </div>
          `;
        }).join('');

        orderItems.forEach((it, idx) => {
          const itId = it.id || idx;
          const radios = list.querySelectorAll(`input[name="driver_item_status_${itId}"]`);
          const wrap = document.getElementById(`driver_item_wrap_${itId}`);
          const input = document.getElementById(`driver_item_notes_${itId}`);
          const card = document.getElementById(`driver_item_card_${itId}`);
          const lblOk = document.getElementById(`lbl_ok_${itId}`);
          const lblProb = document.getElementById(`lbl_prob_${itId}`);

          radios.forEach(r => {
            r.addEventListener('change', () => {
              if (r.value === 'Problema' && r.checked) {
                if (wrap) wrap.style.display = 'block';
                if (card) { card.style.borderColor = '#ef4444'; card.style.background = '#fffaf0'; }
                if (lblProb) { lblProb.style.background = '#fee2e2'; lblProb.style.borderColor = '#fca5a5'; }
                if (lblOk) { lblOk.style.background = '#f1f5f9'; lblOk.style.borderColor = '#cbd5e1'; }
                if (input) {
                  input.focus();
                  input.style.borderColor = '#ef4444';
                }
                const pt = this.el('problemType');
                if (pt) pt.value = 'Mercadoria avariada';
              } else if (r.checked) {
                if (wrap) wrap.style.display = 'none';
                if (card) { card.style.borderColor = '#cbd5e1'; card.style.background = '#fff'; }
                if (lblProb) { lblProb.style.background = '#f1f5f9'; lblProb.style.borderColor = '#cbd5e1'; }
                if (lblOk) { lblOk.style.background = '#dcfce7'; lblOk.style.borderColor = '#86efac'; }
              }
            });
          });
        });

        list.querySelectorAll('.btn-quick-reason').forEach(btn => {
          btn.addEventListener('click', (e) => {
            e.preventDefault();
            const targetId = btn.dataset.target;
            const inp = document.getElementById(targetId);
            if (inp) {
              inp.value = btn.dataset.reason;
              inp.focus();
              inp.style.borderColor = '#16a34a';
              setTimeout(() => { inp.style.borderColor = '#ef4444'; }, 1000);
            }
          });
        });
      }
    }

    this.openModal('deliveryDialog');

    this.getGpsCoords().then(coords => {
      if (coords && coords.latitude != null && coords.longitude != null) {
        if (gpsBadge) {
          gpsBadge.textContent = `📍 GPS Exato Capturado: ${coords.latitude.toFixed(5)}, ${coords.longitude.toFixed(5)}`;
          gpsBadge.style.color = '#166534';
          gpsBadge.style.background = '#f0fdf4';
          gpsBadge.style.borderColor = '#86efac';
        }
      } else {
        if (gpsBadge) {
          gpsBadge.textContent = '⚠️ Não foi possível obter o GPS. Verifique se a localização do celular está ativada.';
          gpsBadge.style.color = '#991b1b';
          gpsBadge.style.background = '#fef2f2';
          gpsBadge.style.borderColor = '#fca5a5';
        }
      }
    });
  },
  async processPhoto(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      this.removePhoto();
      return this.toast('Selecione uma imagem válida.', 'bad');
    }
    try {
      const source = await this.loadImage(file);
      const max = 1200;
      const sWidth = source.naturalWidth || source.width;
      const sHeight = source.naturalHeight || source.height;
      const scale = Math.min(1, max / Math.max(sWidth, sHeight));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(sWidth * scale));
      canvas.height = Math.max(1, Math.round(sHeight * scale));
      canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
      if (typeof source.close === 'function') source.close();
      this.photoData = canvas.toDataURL('image/jpeg', 0.8);
      const bytes = Math.round(this.photoData.length * 0.75);
      this.el('photoPreview').src = this.photoData;
      this.el('photoPreview').classList.remove('hidden');
      this.el('removePhoto').classList.remove('hidden');
      this.el('photoQuality').textContent = `Foto pronta: ${canvas.width}×${canvas.height}, aproximadamente ${(bytes / 1024).toFixed(0)} KB.`;
    } catch (_) {
      this.removePhoto();
      this.toast('Não foi possível processar a foto. Tente novamente.', 'bad');
    }
  },
  removePhoto() {
    this.photoData = '';
    this.el('photoInput').value = '';
    this.el('photoPreview').src = '';
    this.el('photoPreview').classList.add('hidden');
    this.el('removePhoto').classList.add('hidden');
    this.el('photoQuality').textContent = '';
  },

  userCenteredManually: false,

  openRouteMap() {
    const dialog = this.el('routeMapDialog');
    if (!dialog) return;
    this.userCenteredManually = false;
    this.openModal('routeMapDialog');

    // 1. Mostrar banner de aviso no modo HTTP sobre limitação do navegador (se não estiver no App nativo)
    const hasNativeBridge = !!(window.AndroidBridge && typeof window.AndroidBridge.getDeviceLocation === 'function');
    const isLocalhost = (location.hostname === 'localhost' || location.hostname === '127.0.0.1');
    const isHttps = (location.protocol === 'https:');
    const httpBanner = this.el('httpGpsWarningBanner');
    if (httpBanner) {
      if (!isHttps && !isLocalhost && !hasNativeBridge) {
        httpBanner.style.display = 'flex';
      } else {
        httpBanner.style.display = 'none';
      }
    }

    this.driverCurrentPos = null;
    this.renderDriverRouteMap();

    setTimeout(() => { if (this.driverMapInstance) this.driverMapInstance.invalidateSize(); }, 60);
    setTimeout(() => { if (this.driverMapInstance) this.driverMapInstance.invalidateSize(); }, 250);

    // Dispara busca imediata de GPS de alta precisão por satélite
    this.requestDriverGps(true, true);
  },

  manualPinMode: false,

  toggleManualPinMode() {
    this.manualPinMode = !this.manualPinMode;
    const btn = this.el('btnManualPinGps');
    if (this.manualPinMode) {
      if (btn) {
        btn.style.background = '#f59e0b';
        btn.style.color = '#ffffff';
        btn.textContent = '📍 Toque no Mapa...';
      }
      this.toast('Toque em qualquer ponto do mapa para marcar sua localização atual.');
      if (this.driverMapInstance) {
        this.driverMapInstance.getContainer().style.cursor = 'crosshair';
      }
    } else {
      if (btn) {
        btn.style.background = '';
        btn.style.color = '';
        btn.textContent = '📍 Marcar no Mapa';
      }
      if (this.driverMapInstance) {
        this.driverMapInstance.getContainer().style.cursor = '';
      }
    }
  },

  lastGeoError: null,
  async getAccuratePosition(options = {}) {
    const highAcc = options.highAccuracy !== false;
    const forceFresh = !!options.forceFresh;
    this.lastGeoError = null;

    // 0. Tenta ler ponte nativa AndroidBridge (se existir no app)
    if (typeof window.AndroidBridge !== 'undefined' && typeof window.AndroidBridge.getDeviceLocation === 'function') {
      try {
        const raw = window.AndroidBridge.getDeviceLocation();
        const parsed = (typeof raw === 'string') ? JSON.parse(raw) : raw;
        if (parsed && parsed.ok && !isNaN(parsed.latitude) && !isNaN(parsed.longitude)) {
          const loc = {
            lat: Number(parsed.latitude),
            lng: Number(parsed.longitude),
            acc: Number(parsed.accuracy || 15),
            source: 'android_native_gps',
            timestamp: parsed.time || Date.now()
          };
          this.lastKnownCoords = { latitude: loc.lat, longitude: loc.lng };
          this.lastKnownCoordsAccuracy = loc.acc;
          this.lastKnownCoordsTimestamp = loc.timestamp;
          this.driverCurrentPos = [loc.lat, loc.lng];
          return loc;
        }
      } catch (_) {}
    }

    // 1. Geolocalização direta via HTML5 / Chromium
    if ('geolocation' in navigator) {
      const tryGeo = (useHighAcc, timeoutMs, maxAgeMs) => new Promise(resolve => {
        let done = false;
        const timer = setTimeout(() => {
          if (!done) {
            done = true;
            this.lastGeoError = { code: 3, message: 'timeout' };
            resolve(null);
          }
        }, timeoutMs);

        try {
          navigator.geolocation.getCurrentPosition(
            pos => {
              if (!done) {
                done = true;
                clearTimeout(timer);
                if (pos && pos.coords) {
                  resolve({
                    lat: Number(pos.coords.latitude),
                    lng: Number(pos.coords.longitude),
                    acc: Number(pos.coords.accuracy || 10),
                    source: 'satellite',
                    timestamp: pos.timestamp || Date.now()
                  });
                } else {
                  resolve(null);
                }
              }
            },
            err => {
              if (!done) {
                done = true;
                clearTimeout(timer);
                this.lastGeoError = err;
                console.warn('Geolocation error:', err?.code, err?.message);
                resolve(null);
              }
            },
            {
              enableHighAccuracy: useHighAcc,
              timeout: timeoutMs,
              maximumAge: maxAgeMs
            }
          );
        } catch (e) {
          if (!done) { done = true; clearTimeout(timer); resolve(null); }
        }
      });

      // Tentativa 1: Consulta rápida aproveitando cache recente do aparelho (até 60s) - resposta instantânea!
      let loc = await tryGeo(highAcc, 4000, 60000);
      if (loc && !isNaN(loc.lat) && !isNaN(loc.lng) && loc.acc <= 200) {
        this.lastKnownCoords = { latitude: loc.lat, longitude: loc.lng };
        this.lastKnownCoordsAccuracy = loc.acc;
        this.lastKnownCoordsTimestamp = loc.timestamp;
        this.driverCurrentPos = [loc.lat, loc.lng];
        return loc;
      }

      // Tentativa 2: Busca por novo sinal de satélite GPS fresco (até 8s) se permissão não foi negada
      if (!this.lastGeoError || this.lastGeoError.code !== 1) {
        loc = await tryGeo(highAcc, 8000, 0);
        if (loc && !isNaN(loc.lat) && !isNaN(loc.lng) && loc.acc <= 250) {
          this.lastKnownCoords = { latitude: loc.lat, longitude: loc.lng };
          this.lastKnownCoordsAccuracy = loc.acc;
          this.lastKnownCoordsTimestamp = loc.timestamp;
          this.driverCurrentPos = [loc.lat, loc.lng];
          return loc;
        }
      }

      // Tentativa 3: Rede / Wi-Fi aproximado (até 4s)
      if (!this.lastGeoError || this.lastGeoError.code !== 1) {
        const fallbackLoc = await tryGeo(false, 4000, 120000);
        if (fallbackLoc && !isNaN(fallbackLoc.lat) && !isNaN(fallbackLoc.lng)) {
          this.lastKnownCoords = { latitude: fallbackLoc.lat, longitude: fallbackLoc.lng };
          this.lastKnownCoordsAccuracy = fallbackLoc.acc;
          this.lastKnownCoordsTimestamp = fallbackLoc.timestamp;
          this.driverCurrentPos = [fallbackLoc.lat, fallbackLoc.lng];
          return fallbackLoc;
        }
      }

      // Se temos qualquer coordenada válida anterior
      if (this.lastKnownCoords && this.lastKnownCoords.latitude != null) {
        return {
          lat: this.lastKnownCoords.latitude,
          lng: this.lastKnownCoords.longitude,
          acc: this.lastKnownCoordsAccuracy || 20,
          source: 'last_known',
          timestamp: this.lastKnownCoordsTimestamp || Date.now()
        };
      }
    }

    return null;
  },

  async refreshDeliveryGps(interactive = false) {
    const badge = this.el('gpsStatusBadge');
    const badgeText = this.el('gpsStatusBadgeText') || badge;
    if (badgeText && interactive) {
      badgeText.textContent = '⏳ Buscando sinal de GPS em tempo real...';
      if (badge) {
        badge.style.color = '#0284c7';
        badge.style.background = '#f0f9ff';
        badge.style.borderColor = '#bae6fd';
      }
    }

    const loc = await this.getAccuratePosition({ maxWaitMs: 8000, highAccuracy: true, forceFresh: interactive });
    if (loc && loc.lat != null && loc.lng != null) {
      const accText = loc.acc ? ` (±${Math.round(loc.acc)}m)` : '';
      if (badgeText) badgeText.textContent = `✓ GPS Exato Capturado: ${loc.lat.toFixed(5)}, ${loc.lng.toFixed(5)}${accText}`;
      if (badge) {
        badge.style.color = '#166534';
        badge.style.background = '#f0fdf4';
        badge.style.borderColor = '#86efac';
      }
      if (interactive) this.toast(`✓ GPS atualizado com sucesso! (±${Math.round(loc.acc || 10)}m)`, 'ok');
      return loc;
    } else {
      if (this.lastKnownCoords && this.lastKnownCoords.latitude != null) {
        const cLat = this.lastKnownCoords.latitude;
        const cLng = this.lastKnownCoords.longitude;
        if (badgeText) badgeText.textContent = `✓ GPS Registrado: ${cLat.toFixed(5)}, ${cLng.toFixed(5)}`;
        if (badge) {
          badge.style.color = '#166534';
          badge.style.background = '#f0fdf4';
          badge.style.borderColor = '#86efac';
        }
      } else {
        if (badgeText) badgeText.textContent = '⚠️ GPS não detectado. Verifique se o GPS está ativo.';
        if (badge) {
          badge.style.color = '#b42318';
          badge.style.background = '#fff0ef';
          badge.style.borderColor = '#fca5a5';
        }
        if (interactive) this.toast('Não foi possível obter o sinal GPS. Verifique se a localização está ligada.', 'bad');
      }
      return null;
    }
  },

  async requestDriverGps(centerMap = true, forceFresh = false) {
    if (centerMap) {
      this.userCenteredManually = true;
    }

    // Apenas usa cache se NÃO for forceFresh e tiver boa precisão de satélite
    if (!forceFresh && this.driverCurrentPos && this.lastKnownCoordsAccuracy && this.lastKnownCoordsAccuracy <= 35) {
      const cLat = this.driverCurrentPos[0];
      const cLng = this.driverCurrentPos[1];
      this.updateDriverGpsMarker(cLat, cLng, 'cached', this.lastKnownCoordsAccuracy);
      if (centerMap && this.driverMapInstance) {
        this.driverMapInstance.setView([cLat, cLng], 17, { animate: true });
      }
    }

    const badge = this.el('gpsStatusBadge');
    const badgeText = this.el('gpsStatusBadgeText') || badge;
    if (badgeText) {
      badgeText.textContent = '⏳ Buscando satélite GPS em tempo real...';
      if (badge) {
        badge.style.color = '#0284c7';
        badge.style.background = '#f0f9ff';
        badge.style.borderColor = '#bae6fd';
      }
    }

    const loc = await this.getAccuratePosition({ maxWaitMs: 15000, highAccuracy: true, forceFresh: forceFresh });

    if (loc && !isNaN(loc.lat) && !isNaN(loc.lng)) {
      this.driverCurrentPos = [loc.lat, loc.lng];
      this.lastKnownCoords = { latitude: loc.lat, longitude: loc.lng };
      this.updateDriverGpsMarker(loc.lat, loc.lng, loc.source, loc.acc);

      if (centerMap && this.driverMapInstance) {
        this.driverMapInstance.setView([loc.lat, loc.lng], 17, { animate: true });
        if (this.driverLiveLocationMarker) {
          this.driverLiveLocationMarker.openPopup();
        }
      }

      const badge = this.el('gpsStatusBadge');
      const badgeText = this.el('gpsStatusBadgeText') || badge;
      if (badgeText) {
        const label = (loc.source === 'satellite' || loc.source === 'android_native_gps') ? 'GPS Satélite' : 'Localização Automática';
        badgeText.textContent = `📍 ${label}: ${loc.lat.toFixed(5)}, ${loc.lng.toFixed(5)} (±${Math.round(loc.acc || 10)}m)`;
        if (badge) {
          badge.style.color = '#166534';
          badge.style.background = '#f0fdf4';
          badge.style.borderColor = '#86efac';
        }
      }

      const mapBanner = this.el('driverGpsBanner');
      const mapBannerText = this.el('driverGpsBannerText');
      const mapAccuracyBadge = this.el('driverGpsAccuracyBadge');
      if (mapBanner) {
        mapBanner.style.display = 'flex';
        mapBanner.style.background = '#f0fdf4';
        mapBanner.style.borderColor = '#bbf7d0';
        mapBanner.style.color = '#166534';
        if (mapBannerText) mapBannerText.textContent = `📍 GPS do Motorista: ${loc.lat.toFixed(5)}, ${loc.lng.toFixed(5)}`;
        if (mapAccuracyBadge) mapAccuracyBadge.textContent = `±${Math.round(loc.acc || 10)}m`;
      }

      return loc;
    }

    const mapBanner = this.el('driverGpsBanner');
    const mapBannerText = this.el('driverGpsBannerText');
    if (mapBanner && mapBannerText) {
      mapBanner.style.display = 'flex';
      mapBanner.style.background = '#fef2f2';
      mapBanner.style.borderColor = '#fecaca';
      mapBanner.style.color = '#991b1b';
      mapBannerText.textContent = '⚠️ GPS não localizado. Use "Marcar no Mapa" ou abra no Chrome.';
    }

    return null;
  },

  driverLiveGpsLayer: null,
  driverLiveLocationMarker: null,
  driverLiveLocationCircle: null,

  updateDriverGpsMarker(lat, lng, source = 'satellite', acc = 15) {
    if (!this.driverMapInstance || !window.L) return;
    if (!this.driverLiveGpsLayer) {
      this.driverLiveGpsLayer = L.layerGroup().addTo(this.driverMapInstance);
    }

    const isSat = (source === 'satellite' || source === 'android_native_gps');
    const isManual = (source === 'manual');
    const color = isSat ? '#2563eb' : (isManual ? '#f59e0b' : '#0284c7');
    const pulseAnim = isManual ? 'pulseGpsManual' : 'pulseGps';
    const label = isSat ? '🎯 Sua Posição Atual (GPS)' : (isManual ? '📍 Sua Posição (Fixada no Mapa)' : '📍 Sua Posição (Estimada)');

    this.driverLiveGpsLayer.clearLayers();

    // Círculo de precisão ao redor do ponto GPS
    if (acc && acc > 3 && acc < 600) {
      const circle = L.circle([lat, lng], {
        radius: Math.min(acc, 200),
        color: color,
        weight: 1.5,
        fillColor: color,
        fillOpacity: 0.15
      });
      this.driverLiveGpsLayer.addLayer(circle);
      this.driverLiveLocationCircle = circle;
    }

    // Ponto GPS central com pulso animado (Arrastável)
    const gpsIcon = L.divIcon({
      className: 'driver-live-gps-dot',
      html: `<div style="position:relative; width:26px; height:26px; background:${color}; border:3px solid #ffffff; border-radius:50%; box-shadow:0 0 14px rgba(37,99,235,0.85); display:flex; align-items:center; justify-content:center; animation:${pulseAnim} 1.8s infinite ease-out; cursor:grab;"><div style="width:8px; height:8px; background:#ffffff; border-radius:50%;"></div></div>`,
      iconSize: [26, 26],
      iconAnchor: [13, 13]
    });

    const marker = L.marker([lat, lng], { icon: gpsIcon, zIndexOffset: 3000, draggable: true });
    marker.bindPopup(`
      <div style="font-family:system-ui,sans-serif; min-width:210px; padding:4px;">
        <b style="color:${color}; font-size:13px;">${label}</b><br>
        <div style="font-size:12px; color:#334155; margin:4px 0;">
          Lat: <b>${lat.toFixed(6)}</b><br>
          Lng: <b>${lng.toFixed(6)}</b><br>
          <small style="color:#64748b;">Precisão do sinal: ±${Math.round(acc)}m</small>
        </div>
        <div style="display:flex; flex-direction:column; gap:6px; margin-top:8px;">
          <a href="https://www.google.com/maps?q=${lat.toFixed(6)},${lng.toFixed(6)}" target="_blank" rel="noopener" style="background:#0284c7; color:#fff; padding:6px; border-radius:6px; font-size:11px; text-decoration:none; text-align:center; font-weight:700;">🗺️ Abrir no Google Maps</a>
          <span style="color:#64748b; font-size:11px;">💡 Se necessário, arraste o ponto para ajustar.</span>
        </div>
      </div>
    `);

    marker.on('dragend', (e) => {
      const pos = e.target.getLatLng();
      const nLat = Number(pos.lat);
      const nLng = Number(pos.lng);
      this.driverCurrentPos = [nLat, nLng];
      this.lastKnownCoords = { latitude: nLat, longitude: nLng };
      this.lastKnownCoordsAccuracy = 10;
      this.lastKnownCoordsTimestamp = Date.now();
      try {
        localStorage.setItem('driver_saved_custom_pos', JSON.stringify({ lat: nLat, lng: nLng }));
        localStorage.setItem('driver_last_known_gps', JSON.stringify({ lat: nLat, lng: nLng, acc: 10, timestamp: Date.now(), source: 'manual' }));
      } catch (_) {}
      this.updateDriverGpsMarker(nLat, nLng, 'manual', 10);
      this.toast(`✓ Localização ajustada manualmente para: ${nLat.toFixed(5)}, ${nLng.toFixed(5)}!`, 'ok');
    });

    this.driverLiveGpsLayer.addLayer(marker);
    this.driverLiveLocationMarker = marker;

    const banner = this.el('driverGpsBanner');
    const bannerText = this.el('driverGpsBannerText');
    const bannerAcc = this.el('driverGpsAccuracyBadge');
    if (banner && bannerText && bannerAcc) {
      banner.style.display = 'flex';
      bannerText.textContent = isSat ? '📍 GPS Satélite Ativo' : (isManual ? '📍 Posição Marcada Manualmente' : '📍 Localização Automática (Arraste para ajustar)');
      bannerAcc.textContent = isManual ? 'Manual' : `±${Math.round(acc || 15)}m`;
    }
  },

  renderDriverRouteMap() {
    const container = this.el('driverRouteMapCanvas');
    if (!container || !window.L) return;

    if (!this.driverMapInstance) {
      this.driverMapInstance = L.map('driverRouteMapCanvas', {
        center: [-17.8572, -41.5055],
        zoom: 11
      });

      // 1. Camadas Oficiais do Google Maps (Alta Resolução)
      const googleHybrid = L.tileLayer('https://{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}', {
        maxZoom: 20,
        subdomains: ['mt0', 'mt1', 'mt2', 'mt3'],
        attribution: '© Google Maps'
      });

      const googleStreets = L.tileLayer('https://{s}.google.com/vt/lyrs=m&x={x}&y={y}&z={z}', {
        maxZoom: 20,
        subdomains: ['mt0', 'mt1', 'mt2', 'mt3'],
        attribution: '© Google Maps'
      });

      const googleTerrain = L.tileLayer('https://{s}.google.com/vt/lyrs=p&x={x}&y={y}&z={z}', {
        maxZoom: 20,
        subdomains: ['mt0', 'mt1', 'mt2', 'mt3'],
        attribution: '© Google Maps'
      });

      // 2. Camadas Livres / Alternativas (ESRI / OpenStreetMap)
      const esriStreet = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
        maxZoom: 19,
        attribution: '© Esri & OpenStreetMap'
      });

      const esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        maxZoom: 19,
        attribution: 'Tiles © Esri'
      });

      const esriLabels = L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
        maxZoom: 19,
        attribution: 'Labels © Esri'
      });

      const satelliteGroup = L.layerGroup([esriSat, esriLabels]);

      const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        subdomains: ['a', 'b', 'c'],
        attribution: '© OpenStreetMap'
      });

      // Ativa Satélite Híbrido Google como camada padrão (ideal para entregas e áreas rurais)
      googleHybrid.addTo(this.driverMapInstance);

      L.control.layers({
        "🛰️ Google Satélite Híbrido (Padrão)": googleHybrid,
        "🗺️ Google Ruas": googleStreets,
        "⛰️ Google Relevo": googleTerrain,
        "🗺️ Ruas & Cidades (ESRI)": esriStreet,
        "🛰️ Satélite Alternativo (ESRI)": satelliteGroup,
        "🌍 OpenStreetMap Padrão": osm
      }, null, { position: 'topright' }).addTo(this.driverMapInstance);

      this.driverMapMarkersLayer = L.layerGroup().addTo(this.driverMapInstance);
      this.driverLiveGpsLayer = L.layerGroup().addTo(this.driverMapInstance);

      // Clique no mapa para marcação manual quando ativada
      this.driverMapInstance.on('click', (e) => {
        if (!this.manualPinMode) return;
        const lat = Number(e.latlng.lat);
        const lng = Number(e.latlng.lng);
        this.driverCurrentPos = [lat, lng];
        this.lastKnownCoords = { latitude: lat, longitude: lng };
        this.updateDriverGpsMarker(lat, lng, 'manual', 10);
        this.toggleManualPinMode();
        this.toast(`Posição fixada no mapa: ${lat.toFixed(4)}, ${lng.toFixed(4)}`, 'ok');

        const badge = this.el('gpsStatusBadge');
        if (badge) {
          badge.textContent = `📍 Posição Marcada: ${lat.toFixed(5)}, ${lng.toFixed(5)}`;
          badge.style.color = '#166534';
          badge.style.background = '#f0fdf4';
          badge.style.borderColor = '#86efac';
        }
      });
    }

    // Se já temos a localização conhecida, plotar imediatamente
    if (this.driverCurrentPos || this.lastKnownCoords) {
      const lat = this.driverCurrentPos ? this.driverCurrentPos[0] : this.lastKnownCoords.latitude;
      const lng = this.driverCurrentPos ? this.driverCurrentPos[1] : this.lastKnownCoords.longitude;
      this.updateDriverGpsMarker(lat, lng, 'satellite', 15);
    }

    // Iniciar rastreamento contínuo do GPS do motorista
    if (navigator.geolocation && !this.driverWatchGpsId) {
      this.driverWatchGpsId = navigator.geolocation.watchPosition(pos => {
        if (!pos || !pos.coords) return;
        const lat = Number(pos.coords.latitude);
        const lng = Number(pos.coords.longitude);
        this.driverCurrentPos = [lat, lng];
        this.lastKnownCoords = { latitude: lat, longitude: lng };
        this.updateDriverGpsMarker(lat, lng, 'satellite', pos.coords.accuracy || 15);
      }, err => {
        console.warn('GPS Watch warning:', err);
      }, {
        enableHighAccuracy: true,
        maximumAge: 2000,
        timeout: 10000
      });
    }

    setTimeout(() => {
      this.driverMapInstance.invalidateSize();
      this.driverMapMarkersLayer.clearLayers();

      const orders = this.currentRoute?.orders || [];
      const bounds = [];
      const routeCoords = [];

      // Se temos posição atual do motorista, incluir no início da rota visual
      if (this.driverCurrentPos) {
        routeCoords.push(this.driverCurrentPos);
        bounds.push(this.driverCurrentPos);
      }

      const verifiedCoords = [];
      const approximateCoords = [];

      orders.forEach((o, idx) => {
        let lat = o.delivery_latitude || o.latitude;
        let lng = o.delivery_longitude || o.longitude;
        if (!lat || !lng) {
          const m = (o.location_link || '').match(/([-+]?\d+\.\d+)\s*,\s*([-+]?\d+\.\d+)/);
          if (m) {
            lat = parseFloat(m[1]);
            lng = parseFloat(m[2]);
          }
        }

        const isRuralVerified = !!(o.is_rural_verified || o.client_rural_verified);
        const seq = o.delivery_order || (idx + 1);

        if (lat && lng) {
          const isDone = (o.status === 'Acertado' || o.status === 'Entregue' || o.order_status === 'Acertado' || o.route_order_status === 'Entregue');
          const isProblem = (o.status === 'Problema' || o.status === 'Com problema' || o.order_status === 'Problema' || o.route_order_status === 'Com problema');
          
          if (isRuralVerified) {
            // PONTO RURAL CONFIRMADO (Fazenda com GPS Real)
            const color = isDone ? '#16a34a' : (isProblem ? '#dc2626' : '#2563eb');
            const icon = L.divIcon({
              className: 'driver-map-marker-verified',
              html: `<div style="background:${color}; color:#fff; font-weight:800; font-size:12px; width:30px; height:30px; border-radius:50%; display:flex; align-items:center; justify-content:center; border:2.5px solid #fff; box-shadow:0 0 0 2px #16a34a, 0 3px 6px rgba(0,0,0,0.4);" title="GPS Fazenda Confirmado">${seq}</div>`,
              iconSize: [30, 30],
              iconAnchor: [15, 15]
            });

            const marker = L.marker([lat, lng], { icon: icon });
            marker.bindPopup(`
              <div style="font-family:system-ui,sans-serif; min-width:220px;">
                <div style="background:#dcfce7; color:#166534; font-size:11px; font-weight:700; padding:3px 6px; border-radius:4px; border:1px solid #86efac; margin-bottom:5px;">✅ GPS Fazenda Confirmado</div>
                <b style="font-size:13px;">Parada #${seq} · Pedido #${o.order_number}</b><br>
                <div style="font-size:12px; color:#1e293b; font-weight:600;">🌾 ${this.escape(o.farm_name ? `Fazenda ${o.farm_name}` : 'Zona Rural')}</div>
                <div style="font-size:12px; color:#475569;">${this.escape(o.client || o.client_name || '')} · ${this.escape(o.city || '')} (${o.weight_kg || 0} kg)</div>
                ${o.reference_point ? `<div style="font-size:11px; color:#334155; background:#f1f5f9; padding:3px 6px; border-radius:4px; margin:4px 0;"><b>Ref:</b> ${this.escape(o.reference_point)}</div>` : ''}
                <div style="margin-top:8px; display:flex; flex-direction:column; gap:6px;">
                  <a href="https://www.google.com/maps/dir/?api=1&destination=${lat},${lng}" target="_blank" class="btn btn-primary btn-small" style="text-decoration:none; font-weight:700; color:#fff; text-align:center; padding:7px 10px; border-radius:6px; background:#166534;">🧭 Traçar Rota até Fazenda</a>
                  <div style="display:flex; gap:6px;">
                    <a href="geo:${lat},${lng}?q=${lat},${lng}" style="flex:1; background:#0284c7; color:#fff; padding:6px; border-radius:6px; font-size:11px; text-decoration:none; text-align:center; font-weight:600;">GPS Nativo</a>
                    <a href="https://waze.com/ul?ll=${lat},${lng}&navigate=yes" target="_blank" style="flex:1; background:#059669; color:#fff; padding:6px; border-radius:6px; font-size:11px; text-decoration:none; text-align:center; font-weight:600;">Waze</a>
                  </div>
                </div>
              </div>
            `);
            this.driverMapMarkersLayer.addLayer(marker);
            bounds.push([lat, lng]);
            verifiedCoords.push([lat, lng]);
          } else {
            // PONTO APROXIMADO DO CENTRO DA CIDADE (1ª Entrega Rural - Sem GPS)
            const icon = L.divIcon({
              className: 'driver-map-marker-approx',
              html: `<div style="background:#fef3c7; color:#92400e; font-weight:800; font-size:11px; width:28px; height:28px; border-radius:50%; display:flex; align-items:center; justify-content:center; border:2px dashed #d97706; box-shadow:0 2px 5px rgba(0,0,0,0.3);" title="1ª Entrega Rural - Sem GPS da Fazenda">⚠️${seq}</div>`,
              iconSize: [28, 28],
              iconAnchor: [14, 14]
            });

            let clientPhone = String(o.client_phone || '').replace(/\D/g, '');
            let clientWa = String(o.client_whatsapp || o.client_phone || '').replace(/\D/g, '');
            if (clientWa && !clientWa.startsWith('55')) clientWa = `55${clientWa}`;
            const waMsg = encodeURIComponent(`Olá, aqui é da Casa do Campo. Estou a caminho para realizar a entrega na fazenda ${o.farm_name || ''}. Poderia me enviar a localização pelo WhatsApp ou um ponto de referência?`);

            const marker = L.marker([lat, lng], { icon: icon });
            marker.bindPopup(`
              <div style="font-family:system-ui,sans-serif; min-width:220px;">
                <div style="background:#fef3c7; color:#92400e; font-size:11px; font-weight:700; padding:3px 6px; border-radius:4px; border:1px solid #fcd34d; margin-bottom:5px;">⚠️ 1ª Entrega Rural (Sem GPS)</div>
                <b style="font-size:13px;">Parada #${seq} · Pedido #${o.order_number}</b><br>
                <div style="font-size:12px; color:#1e293b; font-weight:600;">🌾 ${this.escape(o.farm_name ? `Fazenda ${o.farm_name}` : 'Zona Rural')}</div>
                <div style="font-size:11px; color:#64748b;">${this.escape(o.client_name || o.client || '')} · Centro de ${this.escape(o.city || 'cidade')}</div>
                ${o.reference_point ? `<div style="font-size:11px; color:#92400e; background:#fffbeb; border:1px solid #fde68a; padding:4px 6px; border-radius:4px; margin:4px 0;"><b>📌 Ponto de Ref:</b> ${this.escape(o.reference_point)}</div>` : ''}
                <div style="font-size:11px; color:#78350f; margin:4px 0; line-height:1.3;">
                  <i>🚫 Rota não traçada até o centro da cidade para não desviar você da zona rural. Peça a localização exata ao cliente:</i>
                </div>
                <div style="margin-top:6px; display:flex; flex-direction:column; gap:5px;">
                  ${clientWa ? `<a href="https://wa.me/${clientWa}?text=${waMsg}" target="_blank" style="background:#059669; color:#fff; padding:6px; border-radius:6px; font-size:11px; text-decoration:none; text-align:center; font-weight:700;">💬 Pedir Localização no WhatsApp</a>` : ''}
                  ${clientPhone ? `<a href="tel:${clientPhone}" style="background:#0284c7; color:#fff; padding:5px; border-radius:6px; font-size:11px; text-decoration:none; text-align:center; font-weight:600;">📞 Ligar para o Cliente</a>` : ''}
                  <a href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(o.city || '')}" target="_blank" style="background:#f1f5f9; color:#475569; padding:4px; border-radius:6px; font-size:10px; text-decoration:none; text-align:center; border:1px solid #cbd5e1;">🏢 Ver Centro de ${this.escape(o.city || 'Cidade')}</a>
                </div>
              </div>
            `);
            this.driverMapMarkersLayer.addLayer(marker);
            bounds.push([lat, lng]);
            approximateCoords.push([lat, lng]);
          }
        }
      });

      // Desenhar traçado de linha visual conectando APENAS as paradas rurais confirmadas
      const validRouteLine = [];
      if (this.driverCurrentPos) {
        validRouteLine.push(this.driverCurrentPos);
      }
      verifiedCoords.forEach(c => validRouteLine.push(c));

      if (validRouteLine.length > 1) {
        const polyline = L.polyline(validRouteLine, {
          color: '#16a34a',
          weight: 4,
          opacity: 0.9
        });
        this.driverMapMarkersLayer.addLayer(polyline);
      }

      if (bounds.length > 0) {
        if (!this.userCenteredManually) {
          this.driverMapInstance.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
        }
      } else {
        this.toast('Nenhum pedido desta rota possui coordenadas GPS cadastradas.');
      }
    }, 250);
  },

  async downloadOfflineRouteMap() {
    if (!this.currentRoute || !this.currentRoute.orders || !this.currentRoute.orders.length) {
      return this.toast('Nenhuma rota ou pedido carregado para baixar o mapa.', 'bad');
    }
    const btn = this.el('btnDownloadOfflineMap');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '⏳ Baixando...';
    }
    this.toast('📥 Baixando blocos do mapa da rota para uso sem internet...', 'info');

    try {
      const orders = this.currentRoute.orders;
      let minLat = 90, maxLat = -90, minLng = 180, maxLng = -180;
      let count = 0;
      orders.forEach(o => {
        const lat = parseFloat(o.delivery_latitude || o.latitude);
        const lng = parseFloat(o.delivery_longitude || o.longitude);
        if (!isNaN(lat) && !isNaN(lng)) {
          minLat = Math.min(minLat, lat);
          maxLat = Math.max(maxLat, lat);
          minLng = Math.min(minLng, lng);
          maxLng = Math.max(maxLng, lng);
          count++;
        }
      });

      if (count === 0) {
        minLat = -18.2; maxLat = -17.5; minLng = -42.0; maxLng = -41.0;
      }

      // Expande margem em 0.05 graus (~5 km)
      minLat -= 0.05; maxLat += 0.05; minLng -= 0.05; maxLng += 0.05;

      const lon2tile = (lon, zoom) => Math.floor((lon + 180) / 360 * Math.pow(2, zoom));
      const lat2tile = (lat, zoom) => Math.floor((1 - Math.log(Math.tan(lat * Math.PI / 180) + 1 / Math.cos(lat * Math.PI / 180)) / Math.PI) / 2 * Math.pow(2, zoom));

      const urls = [];
      for (const z of [10, 12, 13, 14]) {
        const xMin = lon2tile(minLng, z);
        const xMax = lon2tile(maxLng, z);
        const yMin = lat2tile(maxLat, z);
        const yMax = lat2tile(minLat, z);
        for (let x = xMin; x <= xMax; x++) {
          for (let y = yMin; y <= yMax; y++) {
            urls.push(`https://mt1.google.com/vt/lyrs=y&x=${x}&y=${y}&z=${z}`);
            urls.push(`https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/${z}/${y}/${x}`);
          }
        }
      }

      // Limita a até 150 blocos para download rápido e leve
      const toFetch = urls.slice(0, 150);
      let downloaded = 0;
      await Promise.all(toFetch.map(async (u) => {
        try {
          await fetch(u, { mode: 'no-cors' });
          downloaded++;
        } catch (_) {}
      }));

      this.toast(`✅ Mapa Offline pronto! ${downloaded} blocos salvos no aparelho para navegar sem internet.`, 'ok');
    } catch (e) {
      this.toast('Não foi possível concluir o download offline: ' + (e.message || e), 'bad');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '✅ Mapa Salvo Offline';
      }
    }
  },

  openSignature(mode = 'receiver') {
    this.signatureMode = mode;
    const titleEl = this.el('signatureDialog').querySelector('h2');
    if (titleEl) {
      titleEl.textContent = mode === 'driver' ? 'Assinatura do Motorista na Ficha' : 'Assinatura do recebedor';
    }
    this.openModal('signatureDialog');
    this.bindCanvas();
    setTimeout(() => {
      this.resizeSignatureCanvas(false);
      this.bindCanvas();
    }, 60);
  },
  bindCanvas() {
    const canvas = this.el('signatureCanvas');
    if (!canvas || canvas.dataset.bound === '1') return;
    canvas.dataset.bound = '1';

    const getPos = (e) => {
      const rect = canvas.getBoundingClientRect();
      const clientX = (e.touches && e.touches.length > 0) ? e.touches[0].clientX : (e.clientX || 0);
      const clientY = (e.touches && e.touches.length > 0) ? e.touches[0].clientY : (e.clientY || 0);
      return {
        x: (clientX - rect.left) * (canvas.width / rect.width),
        y: (clientY - rect.top) * (canvas.height / rect.height)
      };
    };

    const startDraw = (e) => {
      this.drawing = true;
      const ctx = canvas.getContext('2d');
      const ratio = Math.min(2, window.devicePixelRatio || 1);
      ctx.lineWidth = 3.5 * ratio;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.strokeStyle = '#0f172a';
      const p = getPos(e);
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      if (e.cancelable) e.preventDefault();
    };

    const moveDraw = (e) => {
      if (!this.drawing) return;
      const ctx = canvas.getContext('2d');
      const p = getPos(e);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
      this.signatureDirty = true;
      if (e.cancelable) e.preventDefault();
    };

    const stopDraw = (e) => {
      this.drawing = false;
    };

    // Suporte nativo para Touch (Smartphones e Tablets)
    canvas.addEventListener('touchstart', startDraw, { passive: false });
    canvas.addEventListener('touchmove', moveDraw, { passive: false });
    canvas.addEventListener('touchend', stopDraw, { passive: false });
    canvas.addEventListener('touchcancel', stopDraw, { passive: false });

    // Suporte para Mouse / Caneta Stylus
    canvas.addEventListener('mousedown', startDraw);
    canvas.addEventListener('mousemove', moveDraw);
    window.addEventListener('mouseup', stopDraw);

    // Pointer Events com captura
    canvas.addEventListener('pointerdown', (e) => {
      if (e.pointerType === 'mouse' || e.pointerType === 'pen') {
        canvas.setPointerCapture(e.pointerId);
        startDraw(e);
      }
    });
    canvas.addEventListener('pointermove', (e) => {
      if (e.pointerType === 'mouse' || e.pointerType === 'pen') {
        moveDraw(e);
      }
    });
    canvas.addEventListener('pointerup', stopDraw);
    canvas.addEventListener('pointercancel', stopDraw);
  },
  resizeSignatureCanvas(preserve) {
    const canvas = this.el('signatureCanvas');
    if (!canvas) return;
    const saved = preserve && this.signatureDirty && canvas.width ? canvas.toDataURL('image/png') : this.signatureData;
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.max(300, Math.round(rect.width * ratio));
    canvas.height = Math.max(180, Math.round(rect.height * ratio));
    const ctx = canvas.getContext('2d');
    ctx.lineWidth = 3.5 * ratio;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = '#0f172a';
    if (saved) {
      const image = new Image();
      image.onload = () => ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
      image.src = saved;
      this.signatureDirty = true;
    }
  },
  clearSignature() {
    const canvas = this.el('signatureCanvas');
    canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);
    this.signatureDirty = false;
    this.signatureData = '';
  },
  closeSignature(save) {
    if (save) {
      if (!this.signatureDirty) {
        return this.toast(this.signatureMode === 'driver' ? 'Motorista deve assinar antes de usar.' : 'Peça ao recebedor para assinar antes de continuar.', 'bad');
      }
      const sigData = this.el('signatureCanvas').toDataURL('image/png');
      if (this.signatureMode === 'driver') {
        this.driverSignatureData = sigData;
        const st = this.el('driverSignatureStatus');
        if (st) {
          st.textContent = '✅ Assinatura do motorista salva na ficha.';
          st.style.color = '#166534';
        }
        const pr = this.el('driverSignaturePreview');
        if (pr) {
          pr.src = sigData;
          pr.classList.remove('hidden');
        }
        this.toast('Assinatura do motorista registrada.', 'ok');
      } else {
        this.signatureData = sigData;
        this.el('signatureStatus').textContent = '✅ Assinatura coletada e pronta para envio.';
      }
    }
    this.closeModal('signatureDialog');
  },

  lastKnownCoords: null,
  lastKnownCoordsAccuracy: null,
  lastKnownCoordsTimestamp: 0,
  watchGpsId: null,
  androidGpsInterval: null,

  startGpsTracking() {
    // 1. HTML5 Geolocation Watcher para navegador e WebView (prioridade máxima e contínua)
    if ('geolocation' in navigator) {
      try {
        if (this.watchGpsId) navigator.geolocation.clearWatch(this.watchGpsId);
        this.watchGpsId = navigator.geolocation.watchPosition(
          pos => {
            if (pos && pos.coords) {
              const lat = Number(pos.coords.latitude);
              const lng = Number(pos.coords.longitude);
              const acc = Number(pos.coords.accuracy || 15);
              // Somente aceita se a precisão for razoável (evita salto absurdo de antena distante)
              if (acc <= 300) {
                this.lastKnownCoords = { latitude: lat, longitude: lng };
                this.lastKnownCoordsAccuracy = acc;
                this.lastKnownCoordsTimestamp = Date.now();
                this.driverCurrentPos = [lat, lng];
                if (this.driverMapInstance) {
                  this.updateDriverGpsMarker(lat, lng, 'satellite', acc);
                }
                const badge = this.el('gpsStatusBadge');
                const badgeText = this.el('gpsStatusBadgeText') || badge;
                if (badgeText && this.lastKnownCoords.latitude != null) {
                  badgeText.textContent = `📍 GPS Satélite: ${lat.toFixed(5)}, ${lng.toFixed(5)} (±${Math.round(acc)}m)`;
                  if (badge) {
                    badge.style.color = '#166534';
                    badge.style.background = '#f0fdf4';
                    badge.style.borderColor = '#86efac';
                  }
                }
              }
            }
          },
          err => {
            console.warn('Erro ao vigiar GPS via watchPosition:', err);
          },
          { enableHighAccuracy: true, maximumAge: 3000, timeout: 15000 }
        );
      } catch (_) {}
    }

  },

  async getGpsCoords() {
    // 1. Tentar posição precisa fresca via getAccuratePosition (prioriza satélite real)
    try {
      const accurate = await this.getAccuratePosition({ maxWaitMs: 3000, highAccuracy: true, forceFresh: false });
      if (accurate && accurate.lat != null && accurate.lng != null && accurate.acc <= 250) {
        return {
          latitude: accurate.lat,
          longitude: accurate.lng,
          accuracy: accurate.acc || 15
        };
      }
    } catch (_) {}

    // 2. Se temos coordenadas recentes no cache do watchPosition (< 60s) com boa precisão
    if (this.lastKnownCoords && this.lastKnownCoords.latitude != null && (this.lastKnownCoordsAccuracy <= 250)) {
      const ageMs = Date.now() - (this.lastKnownCoordsTimestamp || 0);
      if (ageMs < 60000) {
        return this.lastKnownCoords;
      }
    }

    // 4. Se tivermos qualquer lastKnownCoords
    if (this.lastKnownCoords && this.lastKnownCoords.latitude != null) {
      return this.lastKnownCoords;
    }

    // 5. Fallback final: coordenadas do próprio pedido selecionado ou da sede
    const orderLat = this.selectedOrder?.delivery_latitude || this.selectedOrder?.latitude || -17.8572;
    const orderLng = this.selectedOrder?.delivery_longitude || this.selectedOrder?.longitude || -41.5055;
    return { latitude: Number(orderLat), longitude: Number(orderLng), accuracy: 100 };
  },

  buildPayload(isProblem, coords, validatedItems = []) {
    const docTypeSelect = this.el('deliveredDocumentType');
    return {
      idempotency_key: this.uuid(),
      order_id: this.selectedOrder.order_id,
      route_id: this.currentRoute.id,
      delivered_to: this.el('deliveredTo').value.trim(),
      delivered_document: this.el('deliveredDocument').value.trim(),
      delivered_document_type: docTypeSelect ? docTypeSelect.value : 'CPF',
      final_notes: isProblem ? this.el('problemNotes').value.trim() : this.el('deliveryNotes').value.trim(),
      receipt_photo: isProblem ? '' : this.photoData,
      digital_signature: isProblem ? '' : this.signatureData,
      is_problem: isProblem,
      problem_type: this.el('problemType').value,
      latitude: coords ? coords.latitude : null,
      longitude: coords ? coords.longitude : null,
      items: validatedItems,
    };
  },
  async submitOperation(isProblem) {
    if (!this.selectedOrder || !this.currentRoute) return;
    if (this.operationInProgress) return this.toast('Já existe uma confirmação em andamento. Aguarde.');

    const orderItems = this.selectedOrder.items || [];
    const validatedItems = [];
    let hasItemProblemWithoutNotes = false;
    let missingNotesItemName = '';
    let hasAnyItemProblem = false;

    for (let idx = 0; idx < orderItems.length; idx++) {
      const it = orderItems[idx];
      const itId = it.id || idx;
      const statusRadio = document.querySelector(`input[name="driver_item_status_${itId}"]:checked`);
      const status = statusRadio ? statusRadio.value : 'Entregue';
      const notesInput = document.getElementById(`driver_item_notes_${itId}`);
      const notes = notesInput ? notesInput.value.trim() : '';

      if (status === 'Problema') {
        hasAnyItemProblem = true;
        if (!notes) {
          hasItemProblemWithoutNotes = true;
          missingNotesItemName = it.product_name;
          if (notesInput) {
            notesInput.style.borderColor = '#b91c1c';
            notesInput.style.boxShadow = '0 0 0 3px rgba(220, 38, 38, 0.3)';
            const details = this.el('deliveryProductsDetails');
            if (details) details.open = true;
            notesInput.focus();
          }
          break;
        }
      }

      validatedItems.push({
        id: it.id,
        product_code: it.product_code,
        product_name: it.product_name,
        quantity: it.quantity,
        unit: it.unit,
        weight_kg: it.weight_kg,
        status: status,
        problem_notes: notes,
      });
    }

    if (hasItemProblemWithoutNotes) {
      return this.toast(`Descreva obrigatoriamente o que aconteceu com o produto: ${missingNotesItemName}`, 'bad');
    }

    if (hasAnyItemProblem) {
      isProblem = true;
      const probSummary = validatedItems.filter(i => i.status === 'Problema').map(i => `${i.product_name}: ${i.problem_notes}`).join('; ');
      const curNotes = this.el('problemNotes').value.trim();
      if (!curNotes) {
        this.el('problemNotes').value = probSummary;
      }
    }

    if (isProblem && !this.el('problemNotes').value.trim()) {
      const pn = this.el('problemNotes');
      if (pn) {
        pn.scrollIntoView({ behavior: 'smooth', block: 'center' });
        pn.focus();
        pn.style.borderColor = '#dc2626';
        pn.style.boxShadow = '0 0 0 3px rgba(220, 38, 38, 0.25)';
        setTimeout(() => { pn.style.borderColor = ''; pn.style.boxShadow = ''; }, 3000);
      }
      return this.toast('Descreva obrigatoriamente o problema ocorrido antes de registrar.', 'bad');
    }
    if (!isProblem) {
      const dt = this.el('deliveredTo');
      if (!dt || !dt.value.trim()) {
        if (dt) {
          dt.scrollIntoView({ behavior: 'smooth', block: 'center' });
          dt.focus();
          dt.style.borderColor = '#dc2626';
          dt.style.boxShadow = '0 0 0 3px rgba(220, 38, 38, 0.25)';
          setTimeout(() => { dt.style.borderColor = ''; dt.style.boxShadow = ''; }, 3000);
        }
        return this.toast('Informe o nome de quem recebeu a entrega antes de confirmar.', 'bad');
      }

      if (!this.photoData && !this.signatureData) {
        const notes = (this.el('deliveryNotes')?.value || '').trim();
        if (!notes) {
          const photoInp = this.el('photoInput');
          if (photoInp) photoInp.scrollIntoView({ behavior: 'smooth', block: 'center' });
          return this.toast('Tire a foto do comprovante ou colete a assinatura. Se a câmera falhar, informe o motivo nas Observações.', 'bad');
        }
      }
    }

    const button = isProblem ? this.el('submitProblem') : this.el('submitDelivery');
    this.operationInProgress = true;
    try {
      if (await this.hasUnresolvedOperation(this.currentRoute.id, this.selectedOrder.order_id)) {
        return this.toast('Este pedido já possui um registro aguardando sincronização.', 'bad');
      }
      this.setBusy(button, true, 'Capturando GPS…');
      let coords = await this.getGpsCoords();
      if (!coords || coords.latitude == null) {
        coords = this.lastKnownCoords || {
          latitude: Number(this.selectedOrder?.delivery_latitude || this.selectedOrder?.latitude || -17.8572),
          longitude: Number(this.selectedOrder?.delivery_longitude || this.selectedOrder?.longitude || -41.5055),
          accuracy: 100
        };
      }
      const payload = this.buildPayload(isProblem, coords, validatedItems);
      this.setBusy(button, true, 'Processando…');
      this.el(isProblem ? 'submitDelivery' : 'submitProblem').disabled = true;
      if (!navigator.onLine) {
        await this.enqueue(payload);
        this.closeModal('deliveryDialog');
        this.toast('Registro salvo no aparelho. Será enviado quando a conexão voltar.', 'ok');
        return;
      }
      try {
        const result = await this.api('/api/v1/driver/deliver', {method:'POST', headers:{'Idempotency-Key':payload.idempotency_key}, body:JSON.stringify(payload), timeout:30000});
        this.closeModal('deliveryDialog');
        this.toast(result.message, 'ok');
        if (result.route_status === 'Acertada' || result.route_status === 'Com problema') { this.showScreen('dashboardScreen'); await this.loadRoutes(); }
        else await this.openRoute(this.currentRoute.id);
      } catch (error) {
        if (!error.status || error.status >= 500 || error.status === 429) {
          await this.enqueue(payload, error.message);
          this.closeModal('deliveryDialog');
          this.toast('Servidor indisponível. Registro guardado para reenvio automático.', 'ok');
        } else throw error;
      }
    } catch (error) { this.toast(error.message || 'Não foi possível concluir.', 'bad'); }
    finally {
      this.operationInProgress = false;
      this.setBusy(button, false);
      this.el(isProblem ? 'submitDelivery' : 'submitProblem').disabled = false;
      await this.updateQueueCounters();
    }
  },

  openDatabase() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(this.DB_NAME, this.DB_VERSION);
      request.onupgradeneeded = event => {
        const db = event.target.result;
        const upgradeTransaction = event.target.transaction;
        const store = db.objectStoreNames.contains('operations')
          ? upgradeTransaction.objectStore('operations')
          : db.createObjectStore('operations', {keyPath:'id'});
        if (!store.indexNames.contains('idempotency_key')) store.createIndex('idempotency_key', 'idempotency_key', {unique:true});
        if (!store.indexNames.contains('status')) store.createIndex('status', 'status');
        if (!store.indexNames.contains('next_retry_at')) store.createIndex('next_retry_at', 'next_retry_at');
        if (!db.objectStoreNames.contains('snapshots')) db.createObjectStore('snapshots', {keyPath:'key'});
      };
      request.onsuccess = () => {
        this.db = request.result;
        this.db.onversionchange = () => this.db.close();
        resolve();
      };
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('Atualização do armazenamento bloqueada por outra janela.'));
    });
  },
  tx(mode = 'readonly') { return this.db.transaction('operations', mode).objectStore('operations'); },
  dbRequest(request) { return new Promise((resolve,reject) => { request.onsuccess=()=>resolve(request.result); request.onerror=()=>reject(request.error); }); },
  async enqueue(payload, error = '') {
    const owner = Number(this.session?.driver?.id || 0);
    const ops = await this.allOperations();
    const existing = ops.find(row =>
      Number(row.route_id) === Number(payload.route_id) &&
      Number(row.order_id) === Number(payload.order_id) &&
      (!Number(row.owner_driver_id || 0) || Number(row.owner_driver_id) === owner)
    );
    if (existing && ['pending', 'syncing'].includes(existing.status)) return false;
    const record = existing || {
      id: this.uuid(),
      owner_driver_id: owner,
      created_at: new Date().toISOString(),
    };
    record.idempotency_key = payload.idempotency_key || record.idempotency_key || this.uuid();
    record.order_id = payload.order_id;
    record.route_id = payload.route_id;
    record.attempts = 0;
    record.next_retry_at = Date.now();
    record.status = 'pending';
    record.last_error = error;
    record.payload = payload;
    try { await this.putOperation(record); }
    catch (dbError) { if (dbError.name !== 'ConstraintError') throw dbError; }
    await this.updateQueueCounters();
    if (this.currentRoute && Number(this.currentRoute.id) === Number(payload.route_id)) this.renderRoute();
    return true;
  },
  async allOperations() { return this.dbRequest(this.tx().getAll()); },
  async putOperation(record) { return this.dbRequest(this.tx('readwrite').put(record)); },
  async getSnapshot(key) {
    if (!this.db) return null;
    const row = await this.dbRequest(this.db.transaction('snapshots').objectStore('snapshots').get(key));
    return row?.value ?? null;
  },
  async putSnapshot(key, value) {
    if (!this.db) return;
    await this.dbRequest(this.db.transaction('snapshots', 'readwrite').objectStore('snapshots').put({key, value, saved_at:new Date().toISOString()}));
  },
  async hasUnresolvedOperation(routeId, orderId) {
    if (!this.db) return false;
    const owner = Number(this.session?.driver?.id || 0);
    return (await this.allOperations()).some(row =>
      Number(row.route_id) === Number(routeId) && Number(row.order_id) === Number(orderId) &&
      ['pending','syncing'].includes(row.status) &&
      (!Number(row.owner_driver_id || 0) || Number(row.owner_driver_id) === owner)
    );
  },
  async recoverInterruptedOperations() {
    if (!this.db) return;
    const records = await this.allOperations();
    for (const record of records.filter(row => row.status === 'syncing')) {
      record.status = 'pending';
      record.next_retry_at = Date.now();
      record.last_error = 'Envio interrompido pelo fechamento do aplicativo; será reenviado com a mesma chave.';
      await this.putOperation(record);
    }
  },
  async updateQueueCounters() {
    if (!this.db) return;
    const owner = Number(this.session?.driver?.id || 0);
    const records = (await this.allOperations()).filter(row =>
      owner && (!Number(row.owner_driver_id || 0) || Number(row.owner_driver_id) === owner)
    );
    this.queuedKeys = new Set(
      records.filter(row => ['pending','syncing'].includes(row.status))
        .map(row => `${Number(row.route_id)}:${Number(row.order_id)}`)
    );
    this.failedKeys = new Set(
      records.filter(row => row.status === 'failed')
        .map(row => `${Number(row.route_id)}:${Number(row.order_id)}`)
    );
    this.el('queuePending').textContent = records.filter(row => ['pending','syncing'].includes(row.status)).length;
    this.el('queueSent').textContent = records.filter(row => row.status === 'sent').length;
    this.el('queueFailed').textContent = records.filter(row => row.status === 'failed').length;
  },
  async cleanupSentOperations() {
    if (!this.db) return;
    const cutoff = Date.now() - 7 * 86400000;
    const records = await this.allOperations();
    const store = this.tx('readwrite');
    records.filter(row => row.status === 'sent' && new Date(row.sent_at || row.created_at).getTime() < cutoff).forEach(row => store.delete(row.id));
  },
  async syncQueue(manual = false) {
    if (this.syncInProgress) {
      if (manual) this.toast('A sincronização já está em andamento.');
      return;
    }
    if (!this.db || !navigator.onLine || !this.session?.token || this.session.must_change_password) {
      if (manual) this.toast('Não é possível sincronizar agora. Verifique a conexão e o login.');
      return;
    }
    this.syncInProgress = true;
    try {
      const now = Date.now();
      const owner = Number(this.session.driver?.id || 0);
      const records = (await this.allOperations()).filter(row =>
        (row.status === 'pending' || (manual && row.status === 'failed')) &&
        Number(row.next_retry_at || 0) <= now &&
        (!Number(row.owner_driver_id || 0) || Number(row.owner_driver_id) === owner)
      );
      if (!records.length) { if (manual) this.toast('Nenhum registro pronto para sincronizar.'); await this.updateQueueCounters(); return; }
      let sentNow = 0;
      for (const record of records) {
      record.status = 'syncing';
      await this.putOperation(record);
      try {
        const targetUrl = record.payload?.endpoint || record.endpoint || '/api/v1/driver/deliver';
        const hdrs = record.idempotency_key ? {'Idempotency-Key': record.idempotency_key} : {};
        await this.api(targetUrl, {method:'POST', headers: hdrs, body:JSON.stringify(record.payload), timeout:30000});
        record.status = 'sent';
        sentNow += 1;
        record.sent_at = new Date().toISOString();
        record.last_error = '';
        record.payload = null;
      } catch (error) {
        const code = String(error.data?.code || '');
        if (code === 'order_already_finalized' || code === 'route_already_completed') {
          record.status = 'sent';
          record.sent_at = new Date().toISOString();
          record.last_error = '';
          record.payload = null;
          sentNow += 1;
          await this.putOperation(record);
          await this.updateQueueCounters();
          continue;
        }
        record.attempts = Number(record.attempts || 0) + 1;
        record.last_error = String(error.message || 'Falha de sincronização').slice(0,500);
        const retryable = !error.status || error.status >= 500 || error.status === 429 ||
          error.status === 401 || code === 'password_change_required' || code === 'operation_in_progress';
        record.status = retryable ? 'pending' : 'failed';
        const delay = Math.min(30 * 60 * 1000, 15000 * (2 ** Math.min(record.attempts, 7)));
        record.next_retry_at = retryable ? Date.now() + delay : 0;
      }
      await this.putOperation(record);
      await this.updateQueueCounters();
      }
      const after = (await this.allOperations()).filter(row =>
        !Number(row.owner_driver_id || 0) || Number(row.owner_driver_id) === owner
      );
      const failed = after.filter(row => row.status === 'failed').length;
      const pending = after.filter(row => row.status === 'pending').length;
      if (failed) this.toast(`${failed} registro(s) exigem revisão. Toque em Sincronizar para tentar novamente.`, 'bad');
      else if (pending) this.toast(`${pending} registro(s) aguardam nova tentativa automática.`);
      else this.toast('Sincronização concluída.', 'ok');
      if (sentNow > 0) {
        if (this.currentRoute && !this.el('routeScreen').classList.contains('hidden')) await this.openRoute(this.currentRoute.id);
        else await this.loadRoutes();
      }
    } finally {
      this.syncInProgress = false;
    }
  },

  updateHistoryFilterChips() {
    document.querySelectorAll('.chip-filter').forEach(chip => {
      chip.classList.toggle('active', chip.dataset.period === this.historyFilterPeriod);
    });
  },

  async loadDriverHistory() {
    const listEl = this.el('historyList');
    if (!listEl) return;
    listEl.innerHTML = '<div style="padding:28px 16px; text-align:center; color:#64748b; font-size:14px;"><div style="font-size:24px; margin-bottom:8px;">⏳</div>Carregando histórico de entregas...</div>';

    const params = new URLSearchParams();
    const dateVal = this.el('historyDateInput')?.value;
    if (dateVal) {
      params.set('date', dateVal);
    } else if (this.historyFilterPeriod && this.historyFilterPeriod !== 'all') {
      params.set('period', this.historyFilterPeriod);
    }

    try {
      const res = await this.api(`/api/v1/driver/history?${params.toString()}`);
      if (res && res.ok && Array.isArray(res.routes)) {
        this.historyData = res.routes;
        if (this.session?.driver?.id) {
          await this.putSnapshot(`history:${Number(this.session.driver.id)}`, this.historyData).catch(() => {});
        }
        this.filterHistoryList();
      } else {
        listEl.innerHTML = '<div style="padding:32px 16px; text-align:center; color:#94a3b8; font-size:13.5px;"><div style="font-size:28px; margin-bottom:6px;">📭</div>Nenhuma viagem encontrada para o período selecionado.</div>';
      }
    } catch (err) {
      console.error('Erro ao carregar histórico:', err);
      const cached = this.session?.driver?.id ? await this.getSnapshot(`history:${Number(this.session.driver.id)}`).catch(() => null) : null;
      if (Array.isArray(cached) && cached.length > 0) {
        this.historyData = cached;
        this.filterHistoryList();
        this.toast('Exibindo histórico salvo no aparelho.', 'ok');
      } else {
        listEl.innerHTML = `<div style="padding:24px 16px; text-align:center; color:#ef4444; font-size:13.5px;"><div style="font-size:28px; margin-bottom:6px;">⚠️</div>Não foi possível carregar as viagens.<br><span style="font-size:12px; color:#64748b;">${this.escape(err.message)}</span><br><button class="btn btn-small btn-secondary" style="margin-top:12px;" onclick="window.driverApp.loadDriverHistory()">Tentar Novamente</button></div>`;
      }
    }
  },

  filterHistoryList() {
    const term = (this.el('historySearchInput')?.value || '').trim().toLowerCase();
    let filtered = this.historyData || [];
    if (term) {
      filtered = filtered.filter(item => {
        const carga = String(item.carga_id || item.id || '').toLowerCase();
        const dest = String(item.destination || '').toLowerCase();
        const helper = String(item.helper_name || '').toLowerCase();
        const date = String(item.formatted_date || item.created_at || '').toLowerCase();
        return carga.includes(term) || dest.includes(term) || helper.includes(term) || date.includes(term);
      });
    }
    this.renderHistoryList(filtered);
  },

  renderHistoryList(routes) {
    const listEl = this.el('historyList');
    if (!listEl) return;
    if (!routes || routes.length === 0) {
      listEl.innerHTML = '<div style="padding:36px 16px; text-align:center; color:#94a3b8; font-size:13.5px;"><div style="font-size:32px; margin-bottom:8px;">📭</div>Nenhuma viagem encontrada com os filtros informados.</div>';
      return;
    }

    listEl.innerHTML = routes.map(r => {
      const isCompleted = ['Acertada', 'Concluída', 'Finalizada', 'Aguardando Chegada', 'Com problema'].includes(r.status);
      let statusBadge = '';
      if (r.status === 'Com problema') {
        statusBadge = '<span style="background:#fee2e2; color:#991b1b; font-weight:800; font-size:11px; padding:3px 10px; border-radius:999px; text-transform:uppercase; letter-spacing:0.5px;">FINALIZADA (C/ OCORRÊNCIA)</span>';
      } else if (isCompleted) {
        statusBadge = '<span style="background:#dcfce7; color:#166534; font-weight:800; font-size:11px; padding:3px 10px; border-radius:999px; text-transform:uppercase; letter-spacing:0.5px;">FINALIZADA</span>';
      } else if (['Em Trânsito', 'Em rota'].includes(r.status)) {
        statusBadge = '<span style="background:#e0f2fe; color:#0369a1; font-weight:800; font-size:11px; padding:3px 10px; border-radius:999px; text-transform:uppercase; letter-spacing:0.5px;">EM TRÂNSITO</span>';
      } else {
        statusBadge = '<span style="background:#fef3c7; color:#b45309; font-weight:800; font-size:11px; padding:3px 10px; border-radius:999px; text-transform:uppercase; letter-spacing:0.5px;">PLANEJADA</span>';
      }

      const dateStr = r.formatted_date || (r.created_at ? new Date(r.created_at).toLocaleDateString('pt-BR') : 'Data não informada');
      const timeStr = r.start_time ? ` · Saída: ${r.start_time}` : '';
      const destStr = r.destination ? `Destino: ${this.escape(r.destination)}` : 'Destino não registrado';
      const helperStr = r.helper_name ? ` · Ajudante: ${this.escape(r.helper_name)}` : '';
      const kmTot = (r.km_total !== null && r.km_total !== undefined && r.km_total > 0) ? `${r.km_total} km` : (r.km_start ? `Inic: ${r.km_start}` : 'KM ñ inf.');
      const expensesTot = (r.total_expenses && r.total_expenses > 0) ? `R$ ${Number(r.total_expenses).toFixed(2).replace('.', ',')}` : 'R$ 0,00';

      return `
        <div class="card history-card" style="padding:14px; margin-bottom:12px; border:1px solid #e2e8f0; border-radius:14px; box-shadow:0 2px 8px rgba(0,0,0,0.04); background:#ffffff;">
          <div style="display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px;">
            <div>
              <div style="font-size:15px; font-weight:800; color:#0f172a; display:flex; align-items:center; gap:6px;">
                <span>🚚 Carga #${this.escape(r.carga_id || r.id)}</span>
              </div>
              <div style="font-size:12px; color:#64748b; margin-top:2px;">
                📅 ${this.escape(dateStr)}${this.escape(timeStr)}
              </div>
            </div>
            <div>${statusBadge}</div>
          </div>

          <div style="font-size:12.5px; color:#334155; margin-bottom:10px; line-height:1.4;">
            <div>📍 <strong>${destStr}</strong>${this.escape(helperStr)}</div>
          </div>

          <div style="display:grid; grid-template-columns: repeat(3, 1fr); gap:6px; background:#f8fafc; padding:8px 10px; border-radius:10px; margin-bottom:12px; border:1px solid #f1f5f9; text-align:center;">
            <div>
              <div style="font-size:10px; text-transform:uppercase; color:#64748b; font-weight:700;">Entregas</div>
              <div style="font-size:12.5px; font-weight:800; color:#0f172a;">${r.delivered_orders_count || 0}/${r.orders_count || 0}</div>
            </div>
            <div>
              <div style="font-size:10px; text-transform:uppercase; color:#64748b; font-weight:700;">KM Rodado</div>
              <div style="font-size:12.5px; font-weight:800; color:#0f172a;">${kmTot}</div>
            </div>
            <div>
              <div style="font-size:10px; text-transform:uppercase; color:#64748b; font-weight:700;">Despesas</div>
              <div style="font-size:12.5px; font-weight:800; color:#166534;">${expensesTot}</div>
            </div>
          </div>

          <div style="display:flex; gap:8px;">
            ${isCompleted ? `
            <button type="button" class="btn btn-secondary btn-small" style="background:#f0fdf4; border-color:#86efac; color:#166534; font-size:12.5px; font-weight:750; min-height:40px; width:100%; display:inline-flex; align-items:center; justify-content:center; gap:6px;" onclick="window.driverApp.shareTripHistoryWhatsApp(${r.id})" title="Compartilhar resumo no WhatsApp">
              <span>💬</span>
              <span>Enviar Resumo no WhatsApp</span>
            </button>` : `
            <button type="button" class="btn btn-primary btn-small" style="font-size:12.5px; font-weight:750; min-height:40px; width:100%; display:inline-flex; align-items:center; justify-content:center; gap:6px;" onclick="window.driverApp.openRoute(${r.id})">
              <span>Abrir Carga ➔</span>
            </button>`}
          </div>
        </div>
      `;
    }).join('');
  },

  shareTripHistoryWhatsApp(routeId) {
    const r = (this.historyData || []).find(x => x.id === routeId);
    if (!r) return this.toast('Viagem não encontrada.', 'bad');
    
    const dateStr = r.formatted_date || (r.created_at ? new Date(r.created_at).toLocaleDateString('pt-BR') : '');
    const kmTot = (r.km_total && r.km_total > 0) ? `${r.km_total} km` : 'Não registrado';
    const expTot = (r.total_expenses && r.total_expenses > 0) ? `R$ ${Number(r.total_expenses).toFixed(2).replace('.', ',')}` : 'R$ 0,00';
    
    let text = `*REGISTRO DE VIAGEM - LOGÍSTICA CASA DO CAMPO*\n`;
    text += `🚚 *Carga:* #${r.carga_id || r.id}\n`;
    text += `📅 *Data:* ${dateStr}\n`;
    text += `📍 *Destino:* ${r.destination || 'Não informado'}\n`;
    if (r.helper_name) text += `👥 *Ajudante:* ${r.helper_name}\n`;
    text += `📦 *Entregas:* ${r.delivered_orders_count || 0}/${r.orders_count || 0} entregues`;
    if (r.problem_orders_count > 0) text += ` (${r.problem_orders_count} com problema)`;
    text += `\n🛣️ *KM Rodado:* ${kmTot}\n`;
    text += `💰 *Despesas Totais:* ${expTot}\n`;
    text += `*Status:* Finalizada e Confirmada na Loja\n`;
    
    const waUrl = `https://wa.me/?text=${encodeURIComponent(text)}`;
    window.open(waUrl, '_blank');
  },
};

window.addEventListener('DOMContentLoaded', () => DriverApp.init().catch(error => {
  console.error('Falha ao iniciar aplicativo', error);
  document.body.insertAdjacentHTML('beforeend', '<div class="error-box" style="position:fixed;inset:auto 12px 100px;z-index:99">Não foi possível iniciar o armazenamento offline. Reinicie o aplicativo.</div>');
  const boxes = document.querySelectorAll('.error-box');
  const box = boxes[boxes.length - 1];
  if (box && error?.name) box.textContent += ` (${error.name})`;
}));
