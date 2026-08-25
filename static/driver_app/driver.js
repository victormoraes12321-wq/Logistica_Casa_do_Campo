'use strict';

const DriverApp = {
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
  signatureDirty: false,
  drawing: false,
  confirmResolve: null,
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
    this.el('toastStack').appendChild(item);
    setTimeout(() => item.remove(), 4500);
  },
  showError(id, message = '') {
    const box = this.el(id);
    box.textContent = message;
    box.classList.toggle('hidden', !message);
  },
  ask(title, message, acceptLabel = 'Confirmar') {
    this.el('confirmTitle').textContent = title;
    this.el('confirmMessage').textContent = message;
    this.el('confirmAccept').textContent = acceptLabel;
    this.el('confirmDialog').showModal();
    return new Promise(resolve => { this.confirmResolve = resolve; });
  },

  async init() {
    this.bindEvents();
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
    setInterval(() => { if (navigator.onLine && this.session?.token) this.syncQueue(); }, 30000);
  },

  bindEvents() {
    this.el('loginForm').addEventListener('submit', event => { event.preventDefault(); this.login(); });
    this.el('passwordForm').addEventListener('submit', event => { event.preventDefault(); this.changePassword(); });
    this.el('refreshButton').addEventListener('click', () => this.loadRoutes());
    this.el('syncButton').addEventListener('click', () => this.syncQueue(true));
    this.el('navSync').addEventListener('click', () => this.syncQueue(true));
    this.el('navHome').addEventListener('click', () => { this.showScreen('dashboardScreen'); this.loadRoutes(); });
    this.el('navLogout').addEventListener('click', () => this.logout());
    this.el('backButton').addEventListener('click', () => { this.showScreen('dashboardScreen'); this.loadRoutes(); });
    this.el('startRouteButton').addEventListener('click', () => this.startRoute());
    this.el('orderSearch').addEventListener('input', () => this.filterOrders());
    this.el('closeDelivery').addEventListener('click', () => this.el('deliveryDialog').close());
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
    this.el('confirmDialog').close();
    if (this.confirmResolve) this.confirmResolve(value);
    this.confirmResolve = null;
  },
  updateConnectionStatus() {
    const node = this.el('connectionStatus');
    const online = navigator.onLine && this.serverReachable;
    node.classList.toggle('offline', !online);
    node.querySelector('span:last-child').textContent = online ? 'Online' : 'Offline';
  },
  showScreen(id) {
    ['loginScreen','passwordScreen','dashboardScreen','routeScreen'].forEach(screen => this.el(screen).classList.toggle('hidden', screen !== id));
    const authenticated = id === 'dashboardScreen' || id === 'routeScreen';
    this.el('bottomNav').classList.toggle('hidden', !authenticated);
    this.el('navHome').classList.toggle('active', id === 'dashboardScreen');
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
    const headers = {'Accept':'application/json', ...(options.headers || {})};
    if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    if (this.session?.token && options.auth !== false) headers.Authorization = `Bearer ${this.session.token}`;
    try {
      const response = await fetch(`${this.API_BASE}${path}`, {...options, headers, signal: controller.signal});
      this.serverReachable = true;
      this.updateConnectionStatus();
      let data = {};
      try { data = await response.json(); } catch (_) { data = {ok:false, message:'Resposta inválida do servidor.'}; }
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
    try {
      const data = await this.api('/api/v1/driver/all_drivers', {auth:false});
      select.innerHTML = '<option value="">Selecione seu nome</option>' + data.drivers.map(driver => `<option value="${Number(driver.id)}">${this.escape(driver.name)}</option>`).join('');
      if (!data.drivers.length) select.innerHTML = '<option value="">Nenhum motorista ativo</option>';
    } catch (_) {
      select.innerHTML = '<option value="">Servidor indisponível</option>';
      this.showError('loginError', 'Não foi possível carregar os motoristas. Verifique a conexão e tente novamente.');
    }
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
    const orders = route.orders || [];
    const done = orders.filter(order => ['Acertado','Problema'].includes(order.order_status) || ['Entregue','Com problema'].includes(order.route_order_status)).length;
    const pending = orders.length - done;
    const remainingWeight = orders.filter(order => !['Acertado','Problema'].includes(order.order_status)).reduce((sum, order) => sum + Number(order.weight_kg || 0), 0);
    const pct = orders.length ? Math.round(done * 100 / orders.length) : 0;
    this.el('routeTitle').textContent = route.name;
    this.el('routeSubtitle').textContent = `${route.vehicle_name || 'Veículo não informado'}${route.plate ? ` • ${route.plate}` : ''}`;
    this.el('routeStatus').textContent = route.status;
    this.el('routeStatus').className = `badge ${route.status === 'Planejada' ? 'badge-planned' : 'badge-route'}`;
    this.el('routePending').textContent = pending;
    this.el('routeWeight').textContent = `${remainingWeight.toFixed(1)} kg`;
    this.el('routeProgress').textContent = `${pct}%`;
    this.el('startRouteButton').classList.toggle('hidden', route.status !== 'Planejada');
    this.renderOrders(orders);
  },
  renderOrders(orders) {
    if (!orders.length) { this.el('ordersList').innerHTML = '<div class="card empty">Esta carga não possui pedidos.</div>'; return; }
    this.el('ordersList').innerHTML = orders.map(order => {
      const done = order.order_status === 'Acertado' || order.route_order_status === 'Entregue';
      const problem = order.order_status === 'Problema' || order.route_order_status === 'Com problema';
      const queued = this.queuedKeys.has(`${Number(this.currentRoute.id)}:${Number(order.order_id)}`);
      const fullAddress = order.delivery_address || order.client_full_address || `${order.farm_name || ''} ${order.city || ''}`.trim();
      const phone = String(order.client_phone || '').replace(/\D/g, '');
      let whatsapp = String(order.client_whatsapp || order.client_phone || '').replace(/\D/g, '');
      if (whatsapp && !whatsapp.startsWith('55')) whatsapp = `55${whatsapp}`;
      const mapsFallback = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(fullAddress)}`;
      const maps = this.safeHttpUrl(order.location_link, mapsFallback);
      const search = `${order.client_name || ''} ${order.farm_name || ''} ${order.order_number || ''} ${fullAddress}`.toLowerCase();
      const items = (order.items || []).map(item => `<li>${this.escape(item.product_name)} — ${Number(item.quantity || 0)} ${this.escape(item.unit || 'un')} (${Number(item.weight_kg || 0).toFixed(1)} kg)</li>`).join('');
      const badge = queued ? '<span class="badge badge-planned">Aguardando sincronização</span>' : problem ? '<span class="badge badge-problem">Com problema</span>' : done ? '<span class="badge badge-ok">Entregue</span>' : '<span class="badge badge-planned">Pendente</span>';
      return `<article class="card order-card ${done ? 'done' : ''} ${problem ? 'problem' : ''}" data-search="${this.escape(search)}"><div class="order-top"><div><h3>Pedido ${this.escape(order.order_number)}</h3><b>${this.escape(order.client_name || 'Cliente')}</b></div>${badge}</div><div class="address">📍 ${this.escape(fullAddress || 'Endereço não informado')}<br>${order.reference_point ? `Referência: ${this.escape(order.reference_point)}` : ''}</div><div class="contact-row">${phone ? `<a class="btn btn-secondary btn-small" href="tel:${phone}">📞 Ligar</a>` : ''}${whatsapp ? `<a class="btn btn-secondary btn-small" href="https://wa.me/${whatsapp}" target="_blank" rel="noopener">WhatsApp</a>` : ''}<a class="btn btn-secondary btn-small" href="${this.escape(maps)}" target="_blank" rel="noopener">🗺️ Abrir mapa</a></div>${items ? `<details class="items"><summary>Ver produtos (${order.items.length})</summary><ul>${items}</ul></details>` : ''}<div class="order-actions">${!queued && !done && !problem && this.currentRoute.status === 'Em rota' ? `<button class="btn btn-primary" data-order-id="${Number(order.order_id)}">Finalizar esta parada</button>` : ''}</div></article>`;
    }).join('');
    this.el('ordersList').querySelectorAll('[data-order-id]').forEach(button => button.addEventListener('click', () => this.openDelivery(Number(button.dataset.orderId))));
  },
  filterOrders() {
    const term = this.el('orderSearch').value.toLowerCase().trim();
    this.el('ordersList').querySelectorAll('[data-search]').forEach(card => { card.style.display = !term || card.dataset.search.includes(term) ? '' : 'none'; });
  },
  async startRoute() {
    if (!this.currentRoute || !(await this.ask('Registrar saída', 'A carga e seus pedidos passarão para “Em rota / Saiu para entrega”.', 'Registrar saída'))) return;
    const button = this.el('startRouteButton');
    this.setBusy(button, true, 'Registrando…');
    try {
      await this.api('/api/v1/driver/start_route', {method:'POST', body:JSON.stringify({route_id:this.currentRoute.id})});
      this.toast('Saída da carga registrada.', 'ok');
      await this.openRoute(this.currentRoute.id);
    } catch (error) { this.toast(error.message, 'bad'); }
    finally { this.setBusy(button, false); }
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
    this.el('deliveryDialog').showModal();
  },
  async processPhoto(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      this.removePhoto();
      return this.toast('Selecione uma imagem válida.', 'bad');
    }
    try {
      const source = await createImageBitmap(file);
      const max = 1600;
      const scale = Math.min(1, max / Math.max(source.width, source.height));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(source.width * scale));
      canvas.height = Math.max(1, Math.round(source.height * scale));
      canvas.getContext('2d').drawImage(source, 0, 0, canvas.width, canvas.height);
      source.close?.();
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

  openSignature() {
    this.el('signatureDialog').showModal();
    requestAnimationFrame(() => { this.resizeSignatureCanvas(false); this.bindCanvas(); });
  },
  bindCanvas() {
    const canvas = this.el('signatureCanvas');
    if (canvas.dataset.bound) return;
    canvas.dataset.bound = '1';
    const position = event => {
      const rect = canvas.getBoundingClientRect();
      return {x:(event.clientX - rect.left) * canvas.width / rect.width, y:(event.clientY - rect.top) * canvas.height / rect.height};
    };
    canvas.addEventListener('pointerdown', event => { this.drawing = true; canvas.setPointerCapture(event.pointerId); const p=position(event); const ctx=canvas.getContext('2d'); ctx.beginPath(); ctx.moveTo(p.x,p.y); event.preventDefault(); });
    canvas.addEventListener('pointermove', event => { if (!this.drawing) return; const p=position(event); const ctx=canvas.getContext('2d'); ctx.lineTo(p.x,p.y); ctx.stroke(); this.signatureDirty=true; event.preventDefault(); });
    ['pointerup','pointercancel','pointerleave'].forEach(name => canvas.addEventListener(name, () => { this.drawing=false; }));
  },
  resizeSignatureCanvas(preserve) {
    const canvas = this.el('signatureCanvas');
    const saved = preserve && this.signatureDirty && canvas.width ? canvas.toDataURL('image/png') : this.signatureData;
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.max(300, Math.round(rect.width * ratio));
    canvas.height = Math.max(180, Math.round(rect.height * ratio));
    const ctx = canvas.getContext('2d');
    ctx.lineWidth = 3 * ratio;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = '#111';
    if (saved) { const image = new Image(); image.onload = () => ctx.drawImage(image,0,0,canvas.width,canvas.height); image.src = saved; this.signatureDirty=true; }
  },
  clearSignature() {
    const canvas = this.el('signatureCanvas');
    canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);
    this.signatureDirty = false;
    this.signatureData = '';
  },
  closeSignature(save) {
    if (save) {
      if (!this.signatureDirty) return this.toast('Peça ao recebedor para assinar antes de continuar.', 'bad');
      this.signatureData = this.el('signatureCanvas').toDataURL('image/png');
      this.el('signatureStatus').textContent = '✅ Assinatura coletada e pronta para envio.';
    }
    this.el('signatureDialog').close();
  },

  async getGpsCoords() {
    if (!('geolocation' in navigator)) return { latitude: null, longitude: null };
    return new Promise(resolve => {
      let resolved = false;
      const timeoutTimer = setTimeout(() => {
        if (!resolved) {
          resolved = true;
          resolve({ latitude: null, longitude: null });
        }
      }, 5000);
      navigator.geolocation.getCurrentPosition(
        pos => {
          if (!resolved) {
            resolved = true;
            clearTimeout(timeoutTimer);
            resolve({
              latitude: pos.coords.latitude,
              longitude: pos.coords.longitude
            });
          }
        },
        err => {
          if (!resolved) {
            resolved = true;
            clearTimeout(timeoutTimer);
            resolve({ latitude: null, longitude: null });
          }
        },
        { enableHighAccuracy: true, timeout: 5000, maximumAge: 60000 }
      );
    });
  },

  buildPayload(isProblem, coords) {
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
    };
  },
  async submitOperation(isProblem) {
    if (!this.selectedOrder || !this.currentRoute) return;
    if (this.operationInProgress) return this.toast('Já existe uma confirmação em andamento. Aguarde.');
    if (!isProblem && !this.photoData && !this.signatureData) return this.toast('Inclua uma foto ou assinatura.', 'bad');
    if (isProblem && !this.el('problemNotes').value.trim()) return this.toast('Descreva o problema antes de registrar.', 'bad');
    const button = isProblem ? this.el('submitProblem') : this.el('submitDelivery');
    this.operationInProgress = true;
    try {
      if (await this.hasUnresolvedOperation(this.currentRoute.id, this.selectedOrder.order_id)) {
        return this.toast('Este pedido já possui um registro aguardando sincronização.', 'bad');
      }
      const title = isProblem ? 'Registrar problema' : 'Confirmar entrega';
      const message = isProblem ? 'O pedido ficará com problema. Confirma o registro?' : 'Confirma que a mercadoria foi entregue ao recebedor?';
      if (!(await this.ask(title, message, 'Confirmar'))) return;
      this.setBusy(button, true, 'Capturando GPS…');
      const coords = await this.getGpsCoords();
      const payload = this.buildPayload(isProblem, coords);
      this.setBusy(button, true, 'Processando…');
      this.el(isProblem ? 'submitDelivery' : 'submitProblem').disabled = true;
      if (!navigator.onLine) {
        await this.enqueue(payload);
        this.el('deliveryDialog').close();
        this.toast('Registro salvo no aparelho. Será enviado quando a conexão voltar.', 'ok');
        return;
      }
      try {
        const result = await this.api('/api/v1/driver/deliver', {method:'POST', headers:{'Idempotency-Key':payload.idempotency_key}, body:JSON.stringify(payload), timeout:30000});
        this.el('deliveryDialog').close();
        this.toast(result.message, 'ok');
        if (result.route_status === 'Acertada' || result.route_status === 'Com problema') { this.showScreen('dashboardScreen'); await this.loadRoutes(); }
        else await this.openRoute(this.currentRoute.id);
      } catch (error) {
        if (!error.status || error.status >= 500 || error.status === 429) {
          await this.enqueue(payload, error.message);
          this.el('deliveryDialog').close();
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
    if (await this.hasUnresolvedOperation(payload.route_id, payload.order_id)) return false;
    const record = {
      id: this.uuid(), idempotency_key: payload.idempotency_key,
      order_id: payload.order_id, route_id: payload.route_id,
      owner_driver_id: Number(this.session?.driver?.id || 0),
      created_at: new Date().toISOString(), attempts: 0,
      next_retry_at: Date.now(), status: 'pending', last_error: error, payload,
    };
    try { await this.dbRequest(this.tx('readwrite').add(record)); }
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
      ['pending','syncing','failed'].includes(row.status) &&
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
      records.filter(row => ['pending','syncing','failed'].includes(row.status))
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
        await this.api('/api/v1/driver/deliver', {method:'POST', headers:{'Idempotency-Key':record.idempotency_key}, body:JSON.stringify(record.payload), timeout:30000});
        record.status = 'sent';
        sentNow += 1;
        record.sent_at = new Date().toISOString();
        record.last_error = '';
        record.payload = null;
      } catch (error) {
        record.attempts = Number(record.attempts || 0) + 1;
        record.last_error = String(error.message || 'Falha de sincronização').slice(0,500);
        const code = String(error.data?.code || '');
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
};

window.addEventListener('DOMContentLoaded', () => DriverApp.init().catch(error => {
  console.error('Falha ao iniciar aplicativo', error);
  document.body.insertAdjacentHTML('beforeend', '<div class="error-box" style="position:fixed;inset:auto 12px 100px;z-index:99">Não foi possível iniciar o armazenamento offline. Reinicie o aplicativo.</div>');
  const boxes = document.querySelectorAll('.error-box');
  const box = boxes[boxes.length - 1];
  if (box && error?.name) box.textContent += ` (${error.name})`;
}));
