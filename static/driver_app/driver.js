/**
 * driver.js — Lógica do Aplicativo Android do Motorista
 * Câmera, Compressão de Fotos no Celular, Offline Store & Auto-Sync
 */

const DriverApp = {
    currentDriver: null,
    currentRoute: null,
    selectedOrder: null,
    compressedPhotoBase64: '',

    init: function() {
        this.bindNetworkEvents();
        this.loadDriverList();

        const savedUser = localStorage.getItem('driver_user');
        if (savedUser) {
            try {
                this.currentDriver = JSON.parse(savedUser);
                this.showScreen('screenRoutes');
                this.loadRoutes();
            } catch(e) {
                localStorage.removeItem('driver_user');
            }
        }
    },

    bindNetworkEvents: function() {
        const updateStatus = () => {
            const online = navigator.onLine;
            const badge = document.getElementById('netStatus');
            const banner = document.getElementById('offlineBanner');
            if (online) {
                badge.innerText = '🟢 Online';
                badge.style.background = 'rgba(40,167,69,0.3)';
                banner.style.display = 'none';
                this.syncOfflineQueue();
            } else {
                badge.innerText = '🔴 Offline';
                badge.style.background = 'rgba(220,53,69,0.4)';
                banner.style.display = 'block';
            }
        };
        window.addEventListener('online', updateStatus);
        window.addEventListener('offline', updateStatus);
        updateStatus();
    },

    showScreen: function(screenId) {
        ['screenLogin', 'screenRoutes', 'screenRouteDetail'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = (id === screenId) ? 'block' : 'none';
        });
    },

    loadDriverList: async function() {
        try {
            const res = await fetch('/api/v1/driver/routes');
            const data = await res.json();
            const select = document.getElementById('driverSelect');
            select.innerHTML = '<option value="">-- Selecione seu nome --</option>';
            
            if (data.routes && data.routes.length > 0) {
                const drivers = new Set();
                data.routes.forEach(r => {
                    if (r.driver_name) drivers.add(r.driver_name);
                });
                drivers.forEach(d => {
                    select.innerHTML += `<option value="${d}">${d}</option>`;
                });
            }
            // Se não houver cargas ativas com nome, lista genérica
            if (select.children.length <= 1) {
                select.innerHTML += '<option value="GOD Admin">GOD Admin</option>';
                select.innerHTML += '<option value="Motorista Padrao">Motorista Geral</option>';
            }
        } catch(e) {
            console.warn('Erro ao carregar lista de motoristas:', e);
        }
    },

    login: function() {
        const name = document.getElementById('driverSelect').value;
        const pin = document.getElementById('driverPin').value;
        if (!name) {
            alert('Por favor, selecione seu nome.');
            return;
        }
        this.currentDriver = { name: name, pin: pin };
        localStorage.setItem('driver_user', JSON.stringify(this.currentDriver));
        this.showScreen('screenRoutes');
        this.loadRoutes();
    },

    loadRoutes: async function() {
        const listEl = document.getElementById('routesList');
        listEl.innerHTML = '<p style="text-align:center; padding:10px;">Buscando cargas...</p>';
        try {
            const driverName = this.currentDriver ? this.currentDriver.name : '';
            const res = await fetch(`/api/v1/driver/routes?driver_name=${encodeURIComponent(driverName)}`);
            const data = await res.json();
            
            if (!data.routes || data.routes.length === 0) {
                listEl.innerHTML = '<p style="text-align:center; color:#6c757d; padding:20px;">Nenhuma carga pendente encontrada para você no momento.</p>';
                return;
            }

            listEl.innerHTML = '';
            data.routes.forEach(r => {
                listEl.innerHTML += `
                    <div class="card" style="border-left: 5px solid #1b4d3e; cursor:pointer;" onclick="DriverApp.openRoute(${r.id})">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <h4 style="color:#1b4d3e;">${r.name}</h4>
                            <span class="order-badge badge-pendente">${r.status}</span>
                        </div>
                        <p style="font-size:0.85rem; color:#495057; margin-top:4px;">
                            <strong>Motorista:</strong> ${r.driver_name || 'N/A'} | <strong>Veículo:</strong> ${r.vehicle_name || ''} (${r.plate || ''})<br>
                            <strong>Total de Entregas:</strong> ${r.total_orders || 0} pedido(s)
                        </p>
                        <button class="btn btn-primary" style="margin-top:8px; padding:8px;">Abrir Entregas desta Carga</button>
                    </div>
                `;
            });
        } catch(e) {
            listEl.innerHTML = '<p style="color:red; text-align:center;">Erro de conexão ao carregar cargas.</p>';
        }
    },

    openRoute: async function(routeId) {
        this.showScreen('screenRouteDetail');
        const ordersList = document.getElementById('ordersList');
        ordersList.innerHTML = '<p style="text-align:center; padding:10px;">Carregando entregas...</p>';
        
        try {
            const res = await fetch(`/api/v1/driver/route/${routeId}`);
            const data = await res.json();
            if (!data.ok || !data.route) {
                alert('Erro ao carregar detalhes da carga.');
                return;
            }

            this.currentRoute = data.route;
            document.getElementById('routeTitle').innerText = data.route.name;
            document.getElementById('routeSub').innerText = `Veículo: ${data.route.vehicle_name || ''} (${data.route.plate || ''}) | Motorista: ${data.route.driver_name || ''}`;

            ordersList.innerHTML = '';
            data.route.orders.forEach(o => {
                const isEntregue = (o.order_status === 'Acertado' || o.route_order_status === 'Entregue');
                const isProblema = (o.order_status === 'Problema' || o.route_order_status === 'Com problema');

                let statusClass = 'pendente';
                let statusLabel = 'Pendente';
                if (isEntregue) { statusClass = 'entregue'; statusLabel = 'Entregue 100%'; }
                if (isProblema) { statusClass = 'problema'; statusLabel = 'Problema'; }

                let receiptHtml = '';
                if (o.has_receipt_photo) {
                    receiptHtml = `<br><span style="color:#28a745; font-size:0.8rem;">📷 Canhoto Salvo no Banco!</span>`;
                }

                ordersList.innerHTML += `
                    <div class="order-item ${statusClass}">
                        <div class="order-header">
                            <span>Pedido #${o.order_number}</span>
                            <span class="order-badge badge-${statusClass}">${statusLabel}</span>
                        </div>
                        <div style="font-weight:bold; color:#1b4d3e;">${o.client_name || 'Cliente'}</div>
                        <div class="order-address">📍 ${o.delivery_address || 'Endereço não informado'}</div>
                        <div style="font-size:0.85rem; color:#495057;">
                            <strong>Fazenda/Local:</strong> ${o.farm_name || 'N/A'}<br>
                            <strong>Telefone:</strong> ${o.client_phone || 'N/A'}<br>
                            <strong>Valor:</strong> R$ ${(o.total_value || 0).toFixed(2)} | <strong>Peso:</strong> ${(o.weight_kg || 0).toFixed(1)} kg
                            ${receiptHtml}
                        </div>
                        ${!isEntregue ? `
                            <button class="btn btn-primary" style="margin-top:8px; padding:10px;" onclick="DriverApp.openDeliverModal(${o.order_id})">
                                📷 Fotografar Canhoto & Confirmar
                            </button>
                        ` : `
                            <button class="btn btn-outline" style="margin-top:8px; padding:6px; font-size:0.85rem;" onclick="DriverApp.openDeliverModal(${o.order_id})">
                                🔄 Atualizar Foto do Canhoto
                            </button>
                        `}
                    </div>
                `;
            });
        } catch(e) {
            ordersList.innerHTML = '<p style="color:red;">Erro ao carregar pedidos da carga.</p>';
        }
    },

    openDeliverModal: function(orderId) {
        if (!this.currentRoute || !this.currentRoute.orders) return;
        this.selectedOrder = this.currentRoute.orders.find(o => o.order_id === orderId);
        if (!this.selectedOrder) return;

        document.getElementById('modalOrderTitle').innerText = `Baixa do Pedido #${this.selectedOrder.order_number}`;
        document.getElementById('inputRecebedor').value = '';
        document.getElementById('inputDoc').value = '';
        document.getElementById('inputNotes').value = '';
        document.getElementById('cameraInput').value = '';
        document.getElementById('photoPreview').style.display = 'none';
        document.getElementById('problemSection').style.display = 'none';
        this.compressedPhotoBase64 = '';

        document.getElementById('modalDeliver').style.display = 'block';
    },

    closeModal: function() {
        document.getElementById('modalDeliver').style.display = 'none';
    },

    previewPhoto: function(event) {
        const file = event.target.files[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = (e) => {
            const img = new Image();
            img.onload = () => {
                // Compressão via HTML5 Canvas
                const canvas = document.createElement('canvas');
                const maxDim = 1280;
                let width = img.width;
                let height = img.height;

                if (width > height && width > maxDim) {
                    height = Math.round((height * maxDim) / width);
                    width = maxDim;
                } else if (height > maxDim) {
                    width = Math.round((width * maxDim) / height);
                    height = maxDim;
                }

                canvas.width = width;
                canvas.height = height;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0, width, height);

                // Converte para JPEG 75% qualidade (~120KB)
                this.compressedPhotoBase64 = canvas.toDataURL('image/jpeg', 0.75);

                const previewEl = document.getElementById('photoPreview');
                previewEl.src = this.compressedPhotoBase64;
                previewEl.style.display = 'block';
            };
            img.src = e.target.result;
        };
        reader.readAsDataURL(file);
    },

    toggleProblemForm: function() {
        const sec = document.getElementById('problemSection');
        sec.style.display = (sec.style.display === 'none') ? 'block' : 'none';
    },

    submitDelivery: async function(isProblem) {
        if (!this.selectedOrder) return;

        if (!isProblem && !this.compressedPhotoBase64) {
            alert('Por favor, tire a foto do canhoto/comprovante assinado antes de confirmar.');
            return;
        }

        const payload = {
            order_id: this.selectedOrder.order_id,
            route_id: this.currentRoute ? this.currentRoute.id : null,
            delivered_to: document.getElementById('inputRecebedor').value,
            delivered_document: document.getElementById('inputDoc').value,
            payment_method: document.getElementById('selectPayment').value,
            final_notes: document.getElementById('inputNotes').value,
            receipt_photo: this.compressedPhotoBase64,
            is_problem: isProblem,
            problem_type: document.getElementById('selectProblemType').value
        };

        if (!navigator.onLine) {
            this.saveToOfflineQueue(payload);
            alert('📱 Você está sem sinal de internet no momento. Sua entrega e a foto foram salvas no celular e serão sincronizadas automaticamente assim que você voltar à área de cobertura!');
            this.closeModal();
            return;
        }

        const btn = document.getElementById('btnConfirmDeliver');
        btn.disabled = true;
        btn.innerText = 'Enviando foto e salvando no banco...';

        try {
            const res = await fetch('/api/v1/driver/deliver', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            btn.disabled = false;
            btn.innerText = '✅ Confirmar Entrega 100%';

            if (!data.ok) {
                alert('Erro ao enviar baixa: ' + data.message);
                return;
            }

            this.closeModal();

            if (data.route_auto_settled) {
                alert('🎉 PARABÉNS! Todas as entregas desta carga foram concluídas 100%! O acerto de carga foi finalizado automaticamente pelo sistema!');
            } else {
                alert('✅ Entrega registrada com sucesso! Comprovante salvo no banco de dados.');
            }

            if (this.currentRoute) {
                this.openRoute(this.currentRoute.id);
            }
        } catch(e) {
            btn.disabled = false;
            btn.innerText = '✅ Confirmar Entrega 100%';
            this.saveToOfflineQueue(payload);
            alert('Sinal fraco ou indisponível. A entrega foi salva no aparelho para auto-envio.');
            this.closeModal();
        }
    },

    saveToOfflineQueue: function(payload) {
        const queue = JSON.parse(localStorage.getItem('offline_deliveries') || '[]');
        queue.push(payload);
        localStorage.setItem('offline_deliveries', JSON.stringify(queue));
    },

    syncOfflineQueue: async function() {
        const queue = JSON.parse(localStorage.getItem('offline_deliveries') || '[]');
        if (queue.length === 0) return;

        console.log(`Sincronizando ${queue.length} entregas offline pendentes...`);
        const remaining = [];

        for (let item of queue) {
            try {
                const res = await fetch('/api/v1/driver/deliver', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(item)
                });
                const data = await res.json();
                if (!data.ok) remaining.push(item);
            } catch(e) {
                remaining.push(item);
            }
        }

        localStorage.setItem('offline_deliveries', JSON.stringify(remaining));
        if (remaining.length === 0 && queue.length > 0) {
            alert('🟢 Todas as suas entregas offline pendentes foram sincronizadas com o banco de dados do sistema!');
            if (this.currentRoute) this.openRoute(this.currentRoute.id);
        }
    }
};

window.addEventListener('DOMContentLoaded', () => DriverApp.init());
