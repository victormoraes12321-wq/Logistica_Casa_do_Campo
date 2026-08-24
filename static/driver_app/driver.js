/**
 * driver.js — Lógica do Aplicativo Android do Motorista 'Logística Casa do Campo'
 * Câmera, Compressão de Fotos no Celular, Cadastro de Motoristas, Offline Store & Auto-Sync
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
                const loggedEl = document.getElementById('loggedDriverName');
                if (loggedEl) loggedEl.innerText = this.currentDriver.name;
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
                badge.innerHTML = '<span>🟢</span> <span>Online</span>';
                badge.style.background = 'rgba(16,185,129,0.2)';
                banner.style.display = 'none';
                this.syncOfflineQueue();
            } else {
                badge.innerHTML = '<span>🔴</span> <span>Offline</span>';
                badge.style.background = 'rgba(239,68,68,0.3)';
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
        const select = document.getElementById('driverSelect');
        select.innerHTML = '<option value="">Buscando motoristas cadastrados...</option>';

        try {
            const res = await fetch('/api/v1/driver/all_drivers');
            const data = await res.json();
            
            select.innerHTML = '<option value="">-- Selecione seu nome --</option>';

            if (data.ok && data.drivers && data.drivers.length > 0) {
                data.drivers.forEach(d => {
                    select.innerHTML += `<option value="${d.name}">${d.name} ${d.vehicle_default ? ' (' + d.vehicle_default + ')' : ''}</option>`;
                });
            } else {
                select.innerHTML += '<option value="Motorista Padrao">Motorista Geral</option>';
            }
        } catch(e) {
            console.warn('Erro ao carregar lista de motoristas do banco:', e);
            select.innerHTML = '<option value="">-- Erro ao carregar (Cadastre abaixo) --</option>';
        }
    },

    openRegisterDriverModal: function() {
        document.getElementById('regDriverName').value = '';
        document.getElementById('regDriverPhone').value = '';
        document.getElementById('regDriverDoc').value = '';
        document.getElementById('regDriverVehicle').value = '';
        document.getElementById('modalRegisterDriver').style.display = 'block';
    },

    closeRegisterDriverModal: function() {
        document.getElementById('modalRegisterDriver').style.display = 'none';
    },

    submitRegisterDriver: async function() {
        const name = document.getElementById('regDriverName').value.trim();
        const phone = document.getElementById('regDriverPhone').value.trim();
        const doc = document.getElementById('regDriverDoc').value.trim();
        const vehicle = document.getElementById('regDriverVehicle').value.trim();

        if (!name) {
            alert('Por favor, informe seu nome completo.');
            return;
        }

        try {
            const res = await fetch('/api/v1/driver/register', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    phone: phone,
                    document: doc,
                    vehicle_default: vehicle
                })
            });
            const data = await res.json();

            if (!data.ok) {
                alert('Erro ao cadastrar motorista: ' + data.message);
                return;
            }

            alert('✅ ' + data.message);
            this.closeRegisterDriverModal();

            // Recarrega lista e auto-seleciona o novo motorista
            await this.loadDriverList();
            const select = document.getElementById('driverSelect');
            select.value = name;
        } catch(e) {
            alert('Erro de conexão ao cadastrar motorista: ' + e);
        }
    },

    login: function() {
        const name = document.getElementById('driverSelect').value;
        const pin = document.getElementById('driverPin').value;
        if (!name) {
            alert('Por favor, selecione seu nome na lista ou cadastre-se no botão acima.');
            return;
        }
        this.currentDriver = { name: name, pin: pin };
        localStorage.setItem('driver_user', JSON.stringify(this.currentDriver));
        
        const loggedEl = document.getElementById('loggedDriverName');
        if (loggedEl) loggedEl.innerText = this.currentDriver.name;

        this.showScreen('screenRoutes');
        this.loadRoutes();
    },

    loadRoutes: async function() {
        const listEl = document.getElementById('routesList');
        listEl.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">Buscando cargas ativas...</div>';
        try {
            const driverName = this.currentDriver ? this.currentDriver.name : '';
            const res = await fetch(`/api/v1/driver/routes?driver_name=${encodeURIComponent(driverName)}`);
            const data = await res.json();
            
            if (!data.routes || data.routes.length === 0) {
                listEl.innerHTML = `
                    <div class="card" style="text-align:center; padding:30px;">
                        <div style="font-size:2.5rem; margin-bottom:10px;">🚚</div>
                        <h4 style="color:var(--primary); font-weight:700;">Nenhuma carga pendente</h4>
                        <p style="font-size:0.85rem; color:var(--text-muted); margin-top:4px;">Não há cargas ativas designadas para você no momento.</p>
                    </div>
                `;
                return;
            }

            listEl.innerHTML = '';
            data.routes.forEach(r => {
                const total = r.total_orders || 0;
                const delivered = r.delivered_orders || 0;
                const pct = total > 0 ? Math.round((delivered / total) * 100) : 0;

                listEl.innerHTML += `
                    <div class="card" style="border-left: 6px solid var(--primary); cursor:pointer;" onclick="DriverApp.openRoute(${r.id})">
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                            <span style="font-weight:800; font-size:1.1rem; color:var(--primary);">${r.name}</span>
                            <span class="badge badge-pendente">${r.status}</span>
                        </div>
                        
                        <div style="font-size:0.86rem; color:var(--text-muted); margin-bottom:10px;">
                            <strong>Motorista:</strong> ${r.driver_name || 'N/A'}<br>
                            <strong>Veículo:</strong> ${r.vehicle_name || ''} ${r.plate ? '(' + r.plate + ')' : ''}
                        </div>

                        <div style="display:flex; justify-content:space-between; font-size:0.8rem; font-weight:600; color:var(--text-dark);">
                            <span>Entregas Concluídas: ${delivered} de ${total}</span>
                            <span>${pct}%</span>
                        </div>
                        <div class="progress-container">
                            <div class="progress-bar" style="width: ${pct}%;"></div>
                        </div>

                        <button class="btn btn-primary" style="margin-top:12px; font-size:0.9rem;">
                            📋 Abrir Carga e Ver Pedidos
                        </button>
                    </div>
                `;
            });
        } catch(e) {
            listEl.innerHTML = '<div class="card" style="color:var(--danger); text-align:center; padding:20px;">Erro de conexão ao carregar cargas.</div>';
        }
    },

    openRoute: async function(routeId) {
        this.showScreen('screenRouteDetail');
        const ordersList = document.getElementById('ordersList');
        ordersList.innerHTML = '<div style="text-align:center; padding:30px; color:var(--text-muted);">Carregando pedidos da carga...</div>';
        
        try {
            const res = await fetch(`/api/v1/driver/route/${routeId}`);
            const data = await res.json();
            if (!data.ok || !data.route) {
                alert('Erro ao carregar detalhes da carga.');
                return;
            }

            this.currentRoute = data.route;
            document.getElementById('routeTitle').innerText = data.route.name;
            document.getElementById('routeSub').innerText = `Veículo: ${data.route.vehicle_name || ''} ${data.route.plate ? '(' + data.route.plate + ')' : ''} | Motorista: ${data.route.driver_name || ''}`;

            const orders = data.route.orders || [];
            const totalOrders = orders.length;
            const deliveredOrders = orders.filter(o => o.order_status === 'Acertado' || o.route_order_status === 'Entregue').length;
            const pct = totalOrders > 0 ? Math.round((deliveredOrders / totalOrders) * 100) : 0;

            document.getElementById('routeProgressText').innerText = `${deliveredOrders}/${totalOrders} (${pct}%)`;
            document.getElementById('routeProgressBar').style.width = `${pct}%`;

            ordersList.innerHTML = '';
            orders.forEach(o => {
                const isEntregue = (o.order_status === 'Acertado' || o.route_order_status === 'Entregue');
                const isProblema = (o.order_status === 'Problema' || o.route_order_status === 'Com problema');

                let statusClass = 'pendente';
                let statusLabel = 'Pendente';
                if (isEntregue) { statusClass = 'entregue'; statusLabel = 'Entregue 100%'; }
                if (isProblema) { statusClass = 'problema'; statusLabel = 'Com Problema'; }

                let receiptHtml = '';
                if (o.has_receipt_photo) {
                    receiptHtml = `<div style="color:var(--success); font-weight:700; font-size:0.8rem; margin-top:4px;">📷 Canhoto Salvo no Banco de Dados!</div>`;
                }

                const cleanPhone = (o.client_phone || '').replace(/\D/g, '');
                let phoneButtons = '';
                if (cleanPhone) {
                    phoneButtons = `
                        <a href="tel:${cleanPhone}" class="btn btn-outline btn-sm" style="padding:4px 8px; text-decoration:none; color:var(--text-dark);">📞 Ligar</a>
                        <a href="https://wa.me/55${cleanPhone}" target="_blank" class="btn btn-outline btn-sm" style="padding:4px 8px; text-decoration:none; color:#25D366; border-color:#25D366;">💬 WhatsApp</a>
                    `;
                }

                ordersList.innerHTML += `
                    <div class="order-card ${statusClass}">
                        <div class="order-header-row">
                            <span class="order-num">Pedido #${o.order_number}</span>
                            <span class="badge badge-${statusClass}">${statusLabel}</span>
                        </div>
                        <div class="client-name">${o.client_name || 'Cliente Casa do Campo'}</div>
                        <div class="order-info-line">📍 ${o.delivery_address || 'Endereço não informado'}</div>
                        <div class="order-info-line">🏡 Fazenda/Local: ${o.farm_name || 'N/A'}</div>
                        
                        <div style="font-size:0.85rem; color:var(--text-dark); margin-top:8px; display:flex; justify-content:space-between; background:#f8fafc; padding:8px 12px; border-radius:8px;">
                            <span>Valor: <strong>R$ ${(o.total_value || 0).toFixed(2)}</strong></span>
                            <span>Peso: <strong>${(o.weight_kg || 0).toFixed(1)} kg</strong></span>
                        </div>

                        <div style="display:flex; align-items:center; justify-content:space-between; margin-top:8px;">
                            ${receiptHtml}
                            <div style="display:flex; gap:6px;">${phoneButtons}</div>
                        </div>

                        <div class="action-row">
                            ${!isEntregue ? `
                                <button class="btn btn-primary" onclick="DriverApp.openDeliverModal(${o.order_id})">
                                    📷 Fotografar Canhoto & Dar Baixa
                                </button>
                            ` : `
                                <button class="btn btn-outline" style="font-size:0.85rem; color:var(--success); border-color:var(--success);" onclick="DriverApp.openDeliverModal(${o.order_id})">
                                    🔄 Atualizar Comprovante / Foto
                                </button>
                            `}
                        </div>
                    </div>
                `;
            });
        } catch(e) {
            ordersList.innerHTML = '<div class="card" style="color:var(--danger); text-align:center;">Erro ao carregar pedidos da carga.</div>';
        }
    },

    openDeliverModal: function(orderId) {
        if (!this.currentRoute || !this.currentRoute.orders) return;
        this.selectedOrder = this.currentRoute.orders.find(o => o.order_id === orderId);
        if (!this.selectedOrder) return;

        document.getElementById('modalOrderTitle').innerText = `📷 Baixa do Pedido #${this.selectedOrder.order_number}`;
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
            alert('⚡ Modo Offline: Entrega e foto salvas no celular! Serão enviadas automaticamente quando o 4G voltar.');
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
            btn.innerText = '✅ Confirmar Entrega 100% OK';

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
            btn.innerText = '✅ Confirmar Entrega 100% OK';
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
