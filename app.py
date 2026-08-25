# -*- coding: utf-8 -*-
"""
Logística Casa do Campo
Sistema local profissional para logística interna: pedidos, faturamento,
expedição, cargas, rotas, entregas, gargalos, relatórios e backup.
Roda com Python padrão + SQLite.
Acesse: http://localhost:3000
"""
import os, html, sqlite3, hashlib, secrets, csv, json, re, time, hmac, traceback, threading, shutil, base64
import socket
from datetime import datetime, date, timedelta
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, quote, unquote, urlencode
from app_core.config import load_config
from app_core.domains import (
    admin_dispatch,
    backup_dispatch,
    catalog_dispatch,
    driver_api_dispatch,
    orders_dispatch,
    reports_dispatch,
    routes_dispatch,
)
try:
    from app_core.domains import erp_admin_dispatch as _erp_admin_dispatch
    _ERP_ADMIN_DISPATCH_OK = True
except Exception:
    _erp_admin_dispatch = None  # type: ignore
    _ERP_ADMIN_DISPATCH_OK = False
from app_core.runtime_db import RuntimeDatabaseTarget, create_runtime_connection
from app_core.route_registry import GET_ROUTE_HANDLERS, GET_ROUTE_PERMISSIONS
from app_core.repositories.user_repository import (
    find_active_user_by_username,
    find_user_by_id,
    update_user_last_login,
    update_user_password_hash,
)
from app_core.repositories.order_repository import find_order_by_id
from app_core.repositories.route_repository import find_route_by_id, touch_route, update_route_status
from app_core.services.audit_service import record_audit
from app_core.services.backup_service import (
    create_sqlite_backup,
    prune_backup_files,
    restore_sqlite_from_backup,
)
from app_core.services.permission_service import has_permission as permission_service_has_permission
from app_core.services.driver_security import DEFAULT_DRIVER_PASSWORD, hash_driver_password
import queue

# Integração ERP (importação condicional — nunca quebra o app se módulo ausente)
try:
    from app_core import erp_connector as _erp_connector
    from app_core import erp_field_mapper as _erp_mapper
    _ERP_AVAILABLE = True
except Exception:
    _erp_connector = None  # type: ignore
    _erp_mapper = None  # type: ignore
    _ERP_AVAILABLE = False

try:
    from app_core.services import GLOBAL_CACHE, ALERT_SERVICE
except Exception:
    GLOBAL_CACHE = None
    ALERT_SERVICE = None

_thread_local = threading.local()


class EventBroker:
    def __init__(self):
        self.subscribers = []
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def publish(self, event_type, data=None):
        event = {"type": event_type, "data": data or {}}
        if GLOBAL_CACHE:
            if event_type in ("orders_updated", "routes_updated"):
                GLOBAL_CACHE.invalidate_prefix("dashboard_")
                GLOBAL_CACHE.invalidate_prefix("stats_")
            elif event_type == "master_data_updated":
                GLOBAL_CACHE.clear()
        with self.lock:
            for q in self.subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

GLOBAL_BROKER = EventBroker()


_CFG = load_config()
BASE_DIR = str(_CFG.root_dir)
try:
    with open(os.path.join(BASE_DIR, 'VERSION'), 'r', encoding='utf-8') as _version_file:
        SYSTEM_VERSION = _version_file.read().strip() or 'unknown'
except OSError:
    SYSTEM_VERSION = 'unknown'
DATA_DIR = str(_CFG.data_dir)
STATIC_DIR = str(_CFG.static_dir)
BACKUP_DIR = str(_CFG.backup_dir)
LOG_DIR = str(_CFG.log_dir)
DB_TARGET = RuntimeDatabaseTarget(
    backend=str(_CFG.db_backend),
    database_url=str(_CFG.database_url),
    sqlite_path=str(_CFG.sqlite_db_path or ''),
)
DB_BACKEND = str(DB_TARGET.backend)
DATABASE_URL = str(DB_TARGET.database_url)
DB_PATH = str(DB_TARGET.sqlite_path or '')
PORT = int(_CFG.port)
HOST = str(_CFG.host)
for p in (DATA_DIR, STATIC_DIR, BACKUP_DIR, LOG_DIR):
    os.makedirs(p, exist_ok=True)
ERROR_LOG_PATH = os.path.join(LOG_DIR, 'server_errors.log')
ERROR_LOG_JSON_PATH = os.path.join(LOG_DIR, 'server_errors.jsonl')
AUTOMATION_STATUS_PATH = os.path.join(LOG_DIR, 'automation_status.json')
SESSION_MAX_AGE_SECONDS = int(_CFG.session_max_age_seconds)
SESSION_CLEANUP_INTERVAL_SECONDS = int(_CFG.session_cleanup_interval_seconds)
LOGIN_RATE_WINDOW_SECONDS = int(_CFG.login_rate_window_seconds)
LOGIN_RATE_MAX_FAILURES = int(_CFG.login_rate_max_failures)
LOGIN_RATE_LOCK_SECONDS = int(_CFG.login_rate_lock_seconds)
MAX_SERVER_WORKERS = max(4, int(_CFG.max_server_workers))
REQUEST_SOCKET_TIMEOUT_SECONDS = max(10, int(_CFG.request_timeout_seconds))
STATS_MAINTENANCE_INTERVAL_SECONDS = int(_CFG.stats_maintenance_interval_seconds)
SECURE_COOKIE_FLAG = bool(_CFG.secure_cookie)

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
LOGIN_ATTEMPTS = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
LAST_SESSION_CLEANUP_AT = 0.0
LAST_STATS_MAINTENANCE_AT = 0.0
STATS_MAINTENANCE_LOCK = threading.Lock()
REQUEST_LATENCIES_MS = []
REQUEST_LATENCIES_LOCK = threading.Lock()
SLA_LIMIT_DAYS = 15
SLA_RISK_DAYS = 5
STATUSES = ['Venda','Faturado','Saiu para entrega','Agendado','Acertado','Problema','Cancelado']
FLOW = ['Venda','Faturado','Saiu para entrega','Acertado']
ROLES = ['GOD','Admin','Gestor','Faturamento','Expedicao','Motorista','Operador','Consulta']
PROBLEM_TYPES = ['Cliente ausente','Endereço incorreto','Produto errado','Produto faltando','Cliente recusou','Local de difícil acesso','Chuva/estrada ruim','Veículo com problema','Outro motivo']
PAYMENT_METHODS = ['À vista','Boleto','Pix','Cartão','Prazo','Nota Promissória','Dinheiro','Pago antecipado','Sem cobrança na entrega','Outro']
TEMPLATES = {} # Placeholder para compatibilidade caso necessário
UNITS = ['unidade','saco','caixa','litro','kg','tonelada','metro','outros']
ROUTE_STATUSES = ['Planejada','Em rota','Acertada','Com problema','Cancelada']
ROUTE_ORDER_STATUSES = ['Pendente','Em rota','Entregue','Com problema','Cancelado']
PERMISSIONS = [
    ('view_dashboard', 'Visualizar dashboard'),
    ('view_orders', 'Visualizar pedidos'),
    ('create_orders', 'Criar pedidos'),
    ('edit_orders', 'Editar pedidos'),
    ('cancel_orders', 'Cancelar/reabrir pedidos'),
    ('invoice_orders', 'Faturar pedidos'),
    ('view_financial', 'Visualizar valores financeiros'),
    ('register_delivery_problem', 'Registrar problema de entrega'),
    ('view_clients', 'Visualizar clientes'),
    ('manage_clients', 'Criar/editar/inativar clientes'),
    ('view_drivers', 'Visualizar motoristas'),
    ('manage_drivers', 'Criar/editar/inativar motoristas'),
    ('view_vehicles', 'Visualizar veículos'),
    ('manage_vehicles', 'Criar/editar/inativar veículos'),
    ('view_route_catalog', 'Visualizar cidades/rotas-base'),
    ('manage_route_catalog', 'Gerenciar cidades/rotas-base'),
    ('view_routes', 'Visualizar cargas/rotas'),
    ('create_routes', 'Criar cargas'),
    ('edit_routes', 'Editar cargas e sequência'),
    ('cancel_routes', 'Cancelar/reabrir cargas'),
    ('settle_routes', 'Concluir acerto de carga'),
    ('view_sla', 'Visualizar SLA'),
    ('manage_sla', 'Gerenciar SLA/feriados'),
    ('view_reports', 'Visualizar relatórios'),
    ('export_reports', 'Exportar relatórios'),
    ('view_settings', 'Visualizar configurações'),
    ('manage_settings', 'Alterar configurações'),
    ('manage_users', 'Gerenciar usuários'),
    ('manage_permissions', 'Gerenciar permissões'),
    ('view_backup', 'Visualizar backup'),
    ('create_backup', 'Gerar backup'),
    ('restore_backup', 'Restaurar backup'),
]
PERMISSION_KEYS = [p[0] for p in PERMISSIONS]
PERMISSION_LABELS = {k: v for k, v in PERMISSIONS}
ROLE_DEFAULT_PERMISSIONS = {
    'GOD': set(PERMISSION_KEYS),
    'Admin': set(PERMISSION_KEYS) - {'manage_permissions', 'restore_backup'},
    'Gestor': {
        'view_dashboard', 'view_orders', 'create_orders', 'edit_orders', 'cancel_orders',
        'invoice_orders', 'register_delivery_problem', 'view_clients', 'manage_clients',
        'view_drivers', 'manage_drivers', 'view_vehicles', 'manage_vehicles',
        'view_route_catalog', 'manage_route_catalog', 'view_routes', 'create_routes',
        'edit_routes', 'cancel_routes', 'settle_routes', 'view_sla', 'view_reports',
        'export_reports', 'view_settings'
    },
    'Faturamento': {
        'view_dashboard', 'view_orders', 'edit_orders', 'invoice_orders',
        'view_clients', 'view_reports', 'view_sla'
    },
    'Expedicao': {
        'view_dashboard', 'view_orders', 'edit_orders', 'register_delivery_problem',
        'view_drivers', 'view_vehicles', 'view_route_catalog', 'view_routes',
        'create_routes', 'edit_routes', 'settle_routes', 'view_reports'
    },
    'Motorista': {
        'view_dashboard', 'view_orders', 'view_routes', 'settle_routes', 'register_delivery_problem'
    },
    'Operador': {
        'view_dashboard', 'view_orders', 'create_orders', 'edit_orders', 'cancel_orders',
        'invoice_orders', 'register_delivery_problem', 'view_clients', 'manage_clients',
        'view_drivers', 'view_vehicles', 'view_route_catalog', 'view_routes',
        'create_routes', 'edit_routes', 'settle_routes', 'view_reports', 'view_sla'
    },
    'Consulta': {
        'view_dashboard', 'view_orders', 'view_clients', 'view_drivers', 'view_vehicles',
        'view_route_catalog', 'view_routes', 'view_reports', 'view_sla',
        'view_settings'
    },
}
ORDER_STATUS_ALIASES = {
    'Venda criada': 'Venda',
    'Aguardando faturamento': 'Venda',
    'Venda': 'Venda',
    'Faturado': 'Faturado',
    'Em separação': 'Faturado',
    'Em separaÃ§Ã£o': 'Faturado',
    'Em separa??o': 'Faturado',
    'Pronto para entrega': 'Faturado',
    'Saiu para entrega': 'Saiu para entrega',
    'Agendado': 'Agendado',
    'Entrega agendada': 'Agendado',
    'Entrega agendada pelo cliente': 'Agendado',
    'Esperando agendamento': 'Agendado',
    'Entrega concluída': 'Acertado',
    'Entrega concluida': 'Acertado',
    'Entrega conclu?da': 'Acertado',
    'Entregue': 'Acertado',
    'Acertado': 'Acertado',
    'Entrega com problema': 'Problema',
    'Problema': 'Problema',
    'Cancelado': 'Cancelado',
}
ROUTE_STATUS_ALIASES = {
    'Saiu para entrega': 'Em rota',
    'Em rota': 'Em rota',
    'Concluída': 'Acertada',
    'Concluida': 'Acertada',
    'Conclu?da': 'Acertada',
    'Acertada': 'Acertada',
}
FINAL_ORDER_STATUSES = {'Acertado', 'Problema', 'Cancelado'}
FINAL_ROUTE_STATUSES = {'Acertada', 'Com problema', 'Cancelada'}
ACTIVE_ROUTE_STATUSES = ('Planejada', 'Em rota')
ORDER_ALLOWED_TRANSITIONS = {
    'Venda': {'Faturado', 'Problema', 'Cancelado', 'Agendado'},
    'Faturado': {'Saiu para entrega', 'Acertado', 'Problema', 'Cancelado', 'Agendado'},
    'Saiu para entrega': {'Faturado', 'Acertado', 'Problema', 'Cancelado', 'Agendado'},
    'Agendado': {'Venda', 'Faturado', 'Saiu para entrega', 'Acertado', 'Problema', 'Cancelado'},
    'Acertado': set(),
    'Problema': set(),
    'Cancelado': set(),
}
ROUTE_ALLOWED_TRANSITIONS = {
    'Planejada': {'Em rota', 'Cancelada'},
    'Em rota': {'Acertada', 'Com problema', 'Cancelada'},
    'Acertada': set(),
    'Com problema': set(),
    'Cancelada': set(),
}
ROLE_ALIASES = {
    'GOD': 'GOD',
    'Admin': 'Admin',
    'Administrador': 'Admin',
    'Gestor': 'Gestor',
    'Faturamento': 'Faturamento',
    'Expedição': 'Expedicao',
    'Expedicao': 'Expedicao',
    'Expediçao': 'Expedicao',
    'Expediã§ã£o': 'Expedicao',
    'Motorista': 'Motorista',
    'Motorista/Entrega': 'Motorista',
    'Operador': 'Operador',
    'Operacao': 'Operador',
    'Vendedor': 'Operador',
    'Consulta': 'Consulta',
}


def normalize_order_status(status):
    s = str(status or '').strip()
    s = ORDER_STATUS_ALIASES.get(s, s)
    return s if s in STATUSES else 'Venda'


def normalize_route_status(status):
    s = str(status or '').strip()
    s = ROUTE_STATUS_ALIASES.get(s, s)
    return s if s in ROUTE_STATUSES else 'Planejada'


def now(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def today(): return date.today().isoformat()
def esc(v): return html.escape('' if v is None else str(v), quote=True)
def money(v):
    try: return f"R$ {float(v or 0):,.2f}".replace(',', 'X').replace('.', ',').replace('X','.')
    except Exception: return 'R$ 0,00'


def money_visible(can_view_financial, v):
    return money(v) if can_view_financial else 'Oculto'


def client_display_name(customer_code, name):
    code = str(customer_code or '').strip()
    cname = str(name or '').strip()
    if code and cname:
        return f'{code} - {cname}'
    return cname or code or 'Sem nome'


def fmt_num(v, places=0):
    try: return f"{float(v or 0):,.{places}f}".replace(',', 'X').replace('.', ',').replace('X','.')
    except Exception: return '0'
def brdate(v):
    if not v: return '—'
    s = str(v)[:10]
    try:
        y,m,d = s.split('-')
        return f'{d}/{m}/{y}'
    except Exception:
        return esc(v)
def days_to(v):
    if not v: return None
    try:
        return (datetime.strptime(str(v)[:10], '%Y-%m-%d').date() - date.today()).days
    except Exception:
        return None
def date_add(days): return (date.today() + timedelta(days=days)).isoformat()


def parse_float(v, default=0.0):
    s = '' if v is None else str(v).strip()
    if not s:
        return float(default)
    s = s.replace('\xa0', ' ').replace('R$', '').replace('r$', '').strip()
    s = re.sub(r'[^0-9,.\-]', '', s)
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    if s in ('', '-', '.', '-.'):
        return float(default)
    try:
        return float(s)
    except Exception:
        return float(default)


def parse_int(v, default=0):
    s = '' if v is None else str(v).strip()
    if not s:
        return int(default)
    try:
        return int(s)
    except Exception:
        return int(default)


def upper_text(v):
    return str(v or '').strip().upper()


def friendly_db_error_message(err):
    txt = str(err or '').lower()
    if 'foreign key' in txt:
        return 'Não foi possível concluir a ação porque este registro está vinculado a outros dados.'
    if 'unique' in txt:
        return 'Não foi possível salvar porque já existe um cadastro com os mesmos dados.'
    if 'not null' in txt:
        return 'Não foi possível salvar porque existem campos obrigatórios não preenchidos.'
    if 'check constraint' in txt:
        return 'Não foi possível salvar porque um dos valores informados não é permitido.'
    if 'locked' in txt or 'busy' in txt:
        return 'Outro usuário está gravando neste momento. Aguarde alguns segundos e tente novamente.'
    if 'malformed' in txt or 'disk i/o' in txt or 'readonly' in txt:
        return 'Não foi possível acessar o banco de dados neste momento. Tente novamente e avise o administrador se persistir.'
    return 'Não foi possível concluir esta ação no banco de dados.'


def safe_user_error_message(message, fallback='Não foi possível concluir esta ação agora.'):
    msg = str(message or '').strip().replace('\n', ' ').replace('\r', ' ')
    msg = re.sub(r'\s+', ' ', msg).strip()
    if not msg:
        return fallback
    lowered = msg.lower()
    technical_markers = (
        'sqlite', 'traceback', 'stack', 'integrityerror', 'operationalerror',
        'syntaxerror', 'nameerror', 'typeerror', 'valueerror', 'keyerror',
        'foreign key constraint', 'database is locked', 'invalid literal for int',
        'no such table', 'no such column', 'pragma'
    )
    if any(m in lowered for m in technical_markers):
        return fallback
    if len(msg) > 240:
        return msg[:240].rstrip() + '...'
    return msg


def normalized_text_key(value):
    base = str(value or '').strip().lower()
    base = base.replace('ã', 'a').replace('á', 'a').replace('à', 'a').replace('â', 'a')
    base = base.replace('é', 'e').replace('ê', 'e').replace('í', 'i')
    base = base.replace('ó', 'o').replace('ô', 'o').replace('õ', 'o').replace('ú', 'u').replace('ç', 'c')
    base = re.sub(r'[^a-z0-9]+', ' ', base).strip()
    return re.sub(r'\s+', ' ', base)


def normalize_role(role):
    return ROLE_ALIASES.get(str(role or '').strip(), 'Operador')


def user_key(value):
    raw = str(value or '').strip().lower()
    raw = raw.replace(' ', '_').replace('-', '_')
    raw = re.sub(r'[^a-z0-9_]+', '', normalized_text_key(raw).replace(' ', '_'))
    return re.sub(r'_+', '_', raw).strip('_')


RESTRICTED_DATA_ENTRY_USERS = {'aline', 'ana_paula', 'leandro'}
FULL_ACCESS_EXTRA_USERS = {'gustavo'}


def user_field(user, field_name):
    if user is None:
        return ''
    if isinstance(user, dict):
        return user.get(field_name, '')
    try:
        return user[field_name]
    except Exception:
        return getattr(user, field_name, '')


def user_identity_keys(user):
    if not user:
        return set()
    return {
        user_key(user_field(user, 'username')),
        user_key(user_field(user, 'name')),
    }


def is_extra_full_access_user(user):
    return bool(user_identity_keys(user) & FULL_ACCESS_EXTRA_USERS)


def is_restricted_data_entry_user(user):
    return bool(user_identity_keys(user) & RESTRICTED_DATA_ENTRY_USERS)


def default_permissions_for_role(role):
    canon = normalize_role(role)
    return set(ROLE_DEFAULT_PERMISSIONS.get(canon, ROLE_DEFAULT_PERMISSIONS.get('Operador', set())))


def sanitize_permission_keys(keys):
    return [k for k in keys if k in PERMISSION_LABELS]


def validate_date_field(value, field_name, required=False):
    s = str(value or '').strip()
    if not s:
        if required:
            raise ValueError(f'Informe {field_name}.')
        return ''
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d').date().isoformat()
    except Exception:
        raise ValueError(f'Data inválida em {field_name}. Use o formato correto.')


def validate_password_strength(password):
    p = str(password or '')
    if len(p) < 8:
        raise ValueError('Senha fraca: use no mínimo 8 caracteres.')
    if not re.search(r'[A-Za-z]', p):
        raise ValueError('Senha fraca: inclua pelo menos uma letra.')
    if not re.search(r'\d', p):
        raise ValueError('Senha fraca: inclua pelo menos um número.')
    return True


def payment_method_options(selected=''):
    opts = ['<option value="">Selecione</option>']
    for method in PAYMENT_METHODS:
        sel = ' selected' if str(method) == str(selected or '') else ''
        opts.append(f'<option value="{esc(method)}"{sel}>{esc(method)}</option>')
    return ''.join(opts)


def validate_payment_method(value, required=False):
    s = str(value or '').strip()
    if not s:
        if required:
            raise ValueError('Informe o método de pagamento.')
        return ''
    if s not in PAYMENT_METHODS:
        raise ValueError('Método de pagamento inválido. Use uma opção cadastrada.')
    return s


def log_server_error(context, err):
    try:
        if ALERT_SERVICE:
            ALERT_SERVICE.record_error(str(context or "SERVER_ERROR"), err, context={"source": context})
        payload = {
            'timestamp': now(),
            'context': str(context or ''),
            'error_type': type(err).__name__,
            'error_message': str(err),
            'traceback': ''.join(traceback.format_exception(type(err), err, err.__traceback__)),
        }
        with open(ERROR_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f'\n[{payload["timestamp"]}] {payload["context"]}\n')
            f.write(payload['traceback'])
        with open(ERROR_LOG_JSON_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except Exception:
        pass


def holiday_dates():
    try:
        with conn() as db:
            return {r['date'] for r in db.execute('SELECT date FROM holidays')}
    except Exception:
        return set()

def add_business_days_from(start_date, days=SLA_LIMIT_DAYS, holidays=None):
    try:
        d = datetime.strptime(str(start_date or today())[:10], '%Y-%m-%d').date()
    except Exception:
        d = date.today()
    # Regra operacional atual: SLA em dias corridos.
    limit_days = parse_int(days or SLA_LIMIT_DAYS, SLA_LIMIT_DAYS)
    if limit_days < 0:
        limit_days = SLA_LIMIT_DAYS
    return (d + timedelta(days=limit_days)).isoformat()


def add_business_days(start_date, days=SLA_LIMIT_DAYS):
    return add_business_days_from(start_date, days, set())

def sla_state(sale_date, limit_date, status=''):
    if status in FINAL_ORDER_STATUSES:
        return 'Finalizado'
    d = days_to(limit_date)
    if d is None: return 'Sem prazo'
    if d < 0: return f'Fora do SLA há {abs(d)}d'
    if d <= SLA_RISK_DAYS: return f'Risco SLA: vence em {d}d'
    return f'Dentro do SLA: {d}d restantes'

def is_god(u): return u and normalize_role(u['role']) == 'GOD'
def is_admin(u): return u and normalize_role(u['role']) in ('GOD', 'Admin')
def is_readonly(u): return u and normalize_role(u['role']) == 'Consulta'
def can_manage_settings(u): return u and normalize_role(u['role']) == 'GOD'
def can_operate(u): return u and normalize_role(u['role']) in ('GOD', 'Admin', 'Gestor', 'Faturamento', 'Expedicao', 'Motorista', 'Operador')
def can_manage_reopen(u): return u and normalize_role(u['role']) in ('GOD', 'Admin')


def can_manage_catalog_deletions(u):
    return bool(is_admin(u) or is_extra_full_access_user(u))


def has_permission(db, user, perm):
    return permission_service_has_permission(
        db,
        user=user,
        permission_key=perm,
        valid_permissions=set(PERMISSION_LABELS.keys()),
        normalize_role=normalize_role,
        default_permissions_for_role=default_permissions_for_role,
    )


def user_can(user, perm):
    if not user:
        return False
    handler = getattr(_thread_local, 'current_handler', None)
    if handler:
        if not hasattr(handler, '_perm_cache'):
            handler._perm_cache = {}
        if perm not in handler._perm_cache:
            try:
                with conn() as db:
                    handler._perm_cache[perm] = has_permission(db, user, perm)
            except Exception:
                return False
        return handler._perm_cache[perm]
    try:
        with conn() as db:
            return has_permission(db, user, perm)
    except Exception:
        return False


def active_route_filter_sql(alias='r'):
    return f"{alias}.status IN ('Planejada','Em rota')"


def hash_password(p):
    pwd = str(p or '')
    salt = secrets.token_hex(16)
    iterations = 260000
    digest = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'), salt.encode('utf-8'), iterations).hex()
    return f'pbkdf2_sha256${iterations}${salt}${digest}'


def old_hash_password(p): return hashlib.sha256(('casa_do_campo_local_v2:'+str(p)).encode()).hexdigest()
def old_v3_hash_password(p): return hashlib.sha256(('casa_do_campo_local_v3:'+str(p)).encode()).hexdigest()


def verify_password(raw_password, stored_hash):
    raw = str(raw_password or '')
    stored = str(stored_hash or '')
    if stored.startswith('pbkdf2_sha256$'):
        try:
            _, rounds, salt, digest = stored.split('$', 3)
            rounds_i = int(rounds)
            calc = hashlib.pbkdf2_hmac('sha256', raw.encode('utf-8'), salt.encode('utf-8'), rounds_i).hex()
            return hmac.compare_digest(calc, digest), False
        except Exception:
            return False, False
    ok_legacy = stored in (old_v3_hash_password(raw), old_hash_password(raw))
    return ok_legacy, ok_legacy


def default_initial_password(username):
    return f'{str(username or "").strip()}123'


def human_bytes(value):
    try:
        size = float(value or 0)
    except Exception:
        size = 0.0
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    return f'{size:.1f} {units[idx]}'


def load_automation_status():
    if not os.path.isfile(AUTOMATION_STATUS_PATH):
        return {}
    try:
        with open(AUTOMATION_STATUS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record_request_latency_ms(elapsed_ms):
    try:
        ms = float(elapsed_ms or 0.0)
    except Exception:
        return
    if ms < 0:
        return
    with REQUEST_LATENCIES_LOCK:
        REQUEST_LATENCIES_MS.append(ms)
        if len(REQUEST_LATENCIES_MS) > 300:
            del REQUEST_LATENCIES_MS[:-300]


def request_latency_snapshot():
    with REQUEST_LATENCIES_LOCK:
        samples = list(REQUEST_LATENCIES_MS)
    if not samples:
        return {'samples': 0, 'avg_ms': 0.0, 'p95_ms': 0.0}
    samples.sort()
    avg_ms = sum(samples) / len(samples)
    p95_index = max(0, int(round((len(samples) - 1) * 0.95)))
    p95_ms = samples[p95_index]
    return {'samples': len(samples), 'avg_ms': avg_ms, 'p95_ms': p95_ms}


def revoke_user_sessions(user_id):
    try:
        uid = int(user_id or 0)
    except Exception:
        uid = 0
    if uid <= 0:
        return 0
    removed = 0
    with SESSIONS_LOCK:
        for sid, sess in list(SESSIONS.items()):
            if isinstance(sess, dict) and int(sess.get('uid') or 0) == uid:
                SESSIONS.pop(sid, None)
                removed += 1
    return removed


def conn():
    return create_runtime_connection(DB_TARGET, timeout_seconds=20)


def runtime_state_cleanup(force=False):
    global LAST_SESSION_CLEANUP_AT
    now_ts = time.time()
    if not force and (now_ts - LAST_SESSION_CLEANUP_AT) < SESSION_CLEANUP_INTERVAL_SECONDS:
        return
    LAST_SESSION_CLEANUP_AT = now_ts
    with SESSIONS_LOCK:
        expired = [sid for sid, sess in SESSIONS.items() if not isinstance(sess, dict) or float(sess.get('exp', 0)) < now_ts]
        for sid in expired:
            SESSIONS.pop(sid, None)
    with LOGIN_ATTEMPTS_LOCK:
        stale = []
        ttl = LOGIN_RATE_WINDOW_SECONDS + LOGIN_RATE_LOCK_SECONDS + 60
        for key, rec in LOGIN_ATTEMPTS.items():
            last = float(rec.get('last', 0))
            if (now_ts - last) > ttl and float(rec.get('lock_until', 0)) < now_ts:
                stale.append(key)
        for key in stale:
            LOGIN_ATTEMPTS.pop(key, None)


def login_attempt_key(client_ip, username):
    return f'{str(client_ip or "").strip()}::{str(username or "").strip().lower()}'


def login_lock_remaining(client_ip, username):
    key = login_attempt_key(client_ip, username)
    now_ts = time.time()
    with LOGIN_ATTEMPTS_LOCK:
        rec = LOGIN_ATTEMPTS.get(key)
        if not rec:
            return 0
        lock_until = float(rec.get('lock_until', 0))
        if lock_until > now_ts:
            return int(lock_until - now_ts) + 1
    return 0


def register_login_failure(client_ip, username):
    key = login_attempt_key(client_ip, username)
    now_ts = time.time()
    with LOGIN_ATTEMPTS_LOCK:
        rec = LOGIN_ATTEMPTS.get(key)
        if not rec or (now_ts - float(rec.get('first', 0))) > LOGIN_RATE_WINDOW_SECONDS:
            rec = {'first': now_ts, 'count': 0, 'lock_until': 0}
        rec['count'] = int(rec.get('count', 0)) + 1
        rec['last'] = now_ts
        if rec['count'] >= LOGIN_RATE_MAX_FAILURES:
            rec['lock_until'] = now_ts + LOGIN_RATE_LOCK_SECONDS
            rec['count'] = 0
            rec['first'] = now_ts
        LOGIN_ATTEMPTS[key] = rec


def clear_login_failures(client_ip, username):
    key = login_attempt_key(client_ip, username)
    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.pop(key, None)

def option(values, selected=None, blank=False, blank_label='Selecione'):
    out = f'<option value="">{esc(blank_label)}</option>' if blank else ''
    return out + ''.join(f'<option value="{esc(v)}" {"selected" if str(v)==str(selected) else ""}>{esc(v)}</option>' for v in values)

def row_options(rows, selected=None, label=lambda r: r['name'], blank=True, blank_label='Selecione'):
    out = f'<option value="">{esc(blank_label)}</option>' if blank else ''
    for r in rows:
        out += f'<option value="{r["id"]}" {"selected" if str(r["id"])==str(selected or "") else ""}>{esc(label(r))}</option>'
    return out


def unique_non_empty(values):
    seen = set()
    out = []
    for raw in values:
        txt = str(raw or '').strip()
        if not txt:
            continue
        key = normalized_text_key(txt)
        if key in seen:
            continue
        seen.add(key)
        out.append(txt)
    return sorted(out, key=lambda x: normalized_text_key(x))


def datalist_options(values):
    return ''.join(f'<option value="{esc(v)}"></option>' for v in unique_non_empty(values))

def slug(s):
    repl={'ç':'c','ã':'a','á':'a','à':'a','â':'a','é':'e','ê':'e','í':'i','ó':'o','ô':'o','õ':'o','ú':'u',' ':'-','/':'-'}
    t=str(s or '').lower()
    for a,b in repl.items(): t=t.replace(a,b)
    return ''.join(ch for ch in t if ch.isalnum() or ch=='-')

def badge(status): return f'<span class="badge st-{slug(status)}">{esc(status)}</span>'
def deadline_pill(v, status=''):
    d = days_to(v)
    if status in FINAL_ORDER_STATUSES:
        return f'<span class="deadline ok">Finalizado</span>'
    if status == 'Agendado':
        return f'<span class="deadline neutral">Agendado</span>'
    if d is None: return '<span class="deadline neutral">Sem prazo</span>'
    if d < 0: return f'<span class="deadline late">Atrasado {abs(d)}d</span>'
    if d <= SLA_RISK_DAYS: return f'<span class="deadline warn">Vence em {d}d</span>'
    return f'<span class="deadline ok">{d}d restantes</span>'


def order_sla_row_class(limit_date, status=''):
    if normalize_order_status(status) == 'Agendado':
        return 'sla-scheduled'
    if normalize_order_status(status) in FINAL_ORDER_STATUSES:
        return ''
    d = days_to(limit_date)
    if d is None:
        return ''
    if d < 0:
        return 'sla-late'
    if d <= SLA_RISK_DAYS:
        return 'sla-risk'
    return 'sla-ok'

def get_setting(key, default=''):
    try:
        with conn() as db:
            r = db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            return r['value'] if r else default
    except Exception:
        return default

def db_executescript(db):
    db.executescript('''
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS holidays(id INTEGER PRIMARY KEY AUTOINCREMENT,date TEXT UNIQUE NOT NULL,name TEXT,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL,active INTEGER DEFAULT 1,must_change_password INTEGER DEFAULT 0,last_login_at TEXT,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_code TEXT,name TEXT NOT NULL,document TEXT,phone TEXT,whatsapp TEXT,city TEXT,neighborhood TEXT,farm_name TEXT,address TEXT,reference_point TEXT,notes TEXT,route_name TEXT,active INTEGER DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT,version INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS drivers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,phone TEXT,document TEXT,vehicle_default TEXT,active INTEGER DEFAULT 1,updated_at TEXT,version INTEGER DEFAULT 1,password_hash TEXT,must_change_password INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS vehicles(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,plate TEXT,type TEXT,capacity TEXT,capacity_kg REAL,active INTEGER DEFAULT 1,updated_at TEXT,version INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,order_number TEXT UNIQUE NOT NULL,external_id TEXT,client_id INTEGER,seller_id INTEGER,seller_name TEXT,status TEXT NOT NULL,urgency TEXT DEFAULT 'Normal',sale_date TEXT,expected_delivery_date TEXT,invoice_limit_date TEXT,payment_method TEXT,total_value REAL DEFAULT 0,weight_kg REAL DEFAULT 0,delivery_address TEXT,location_link TEXT,route_name TEXT,city TEXT,uf TEXT,notes TEXT,invoice_number TEXT,invoice_file_path TEXT,invoiced_at TEXT,driver_id INTEGER,vehicle_id INTEGER,delivered_to TEXT,delivered_document TEXT,delivered_at TEXT,final_notes TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,version INTEGER DEFAULT 1,FOREIGN KEY(client_id) REFERENCES clients(id),FOREIGN KEY(seller_id) REFERENCES users(id),FOREIGN KEY(driver_id) REFERENCES drivers(id),FOREIGN KEY(vehicle_id) REFERENCES vehicles(id));
    CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL,product_code TEXT,product_name TEXT NOT NULL,category TEXT,quantity REAL DEFAULT 0,unit TEXT,weight_kg REAL DEFAULT 0,notes TEXT,FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS routes(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,date TEXT,driver_id INTEGER,vehicle_id INTEGER,status TEXT DEFAULT 'Planejada',route_name TEXT,total_weight REAL DEFAULT 0,capacity REAL DEFAULT 11000,notes TEXT,created_at TEXT NOT NULL,updated_at TEXT,version INTEGER DEFAULT 1,FOREIGN KEY(driver_id) REFERENCES drivers(id),FOREIGN KEY(vehicle_id) REFERENCES vehicles(id));
    CREATE TABLE IF NOT EXISTS route_orders(id INTEGER PRIMARY KEY AUTOINCREMENT,route_id INTEGER NOT NULL,order_id INTEGER NOT NULL,delivery_order INTEGER DEFAULT 1,status TEXT DEFAULT 'Pendente',FOREIGN KEY(route_id) REFERENCES routes(id) ON DELETE CASCADE,FOREIGN KEY(order_id) REFERENCES orders(id));
    CREATE TABLE IF NOT EXISTS route_cities(id INTEGER PRIMARY KEY AUTOINCREMENT,route_name TEXT,city TEXT,uf TEXT,delivery_order INTEGER,active INTEGER DEFAULT 1,notes TEXT,created_at TEXT,updated_at TEXT,version INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS order_history(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL,user_id INTEGER,old_status TEXT,new_status TEXT,action TEXT NOT NULL,notes TEXT,created_at TEXT NOT NULL,FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,FOREIGN KEY(user_id) REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS delivery_problems(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL,route_id INTEGER,problem_type TEXT,description TEXT,created_at TEXT NOT NULL,FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE,FOREIGN KEY(route_id) REFERENCES routes(id) ON DELETE SET NULL);
    CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER,file_path TEXT,file_type TEXT,description TEXT,created_at TEXT NOT NULL,FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT,user_id INTEGER,user_name TEXT,source_ip TEXT,action TEXT,module TEXT,entity TEXT,old_value TEXT,new_value TEXT,notes TEXT);
    CREATE TABLE IF NOT EXISTS driver_sessions(id INTEGER PRIMARY KEY AUTOINCREMENT,driver_id INTEGER NOT NULL,token_hash TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,last_seen_at TEXT,revoked_at TEXT,client_ip TEXT,FOREIGN KEY(driver_id) REFERENCES drivers(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS driver_delivery_operations(id INTEGER PRIMARY KEY AUTOINCREMENT,idempotency_key TEXT NOT NULL UNIQUE,driver_id INTEGER NOT NULL,route_id INTEGER NOT NULL,order_id INTEGER NOT NULL,operation_type TEXT NOT NULL,request_hash TEXT NOT NULL,status TEXT NOT NULL,response_json TEXT,created_at TEXT NOT NULL,completed_at TEXT,FOREIGN KEY(driver_id) REFERENCES drivers(id),FOREIGN KEY(route_id) REFERENCES routes(id),FOREIGN KEY(order_id) REFERENCES orders(id));
    CREATE TABLE IF NOT EXISTS role_permissions(role_name TEXT NOT NULL,perm TEXT NOT NULL,allowed INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(role_name,perm));
    CREATE TABLE IF NOT EXISTS user_permissions(user_id INTEGER NOT NULL,perm TEXT NOT NULL,allowed INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(user_id,perm),FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS erp_cache_pedidos (
        numeropedido TEXT PRIMARY KEY,
        raw_json TEXT NOT NULL,
        erp_synced_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS erp_cache_clientes (
        codigocliente TEXT PRIMARY KEY,
        raw_json TEXT NOT NULL,
        erp_synced_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS erp_cache_vendedores (
        codigovendedor TEXT PRIMARY KEY,
        raw_json TEXT NOT NULL,
        erp_synced_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS erp_cache_faturamento (
        numeropedido TEXT PRIMARY KEY,
        raw_json TEXT NOT NULL,
        erp_synced_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS erp_cache_meta (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_erp_cache_pedidos_sync ON erp_cache_pedidos(erp_synced_at);
    CREATE INDEX IF NOT EXISTS idx_erp_cache_fat_sync ON erp_cache_faturamento(erp_synced_at);
    ''')


def ensure_column(db, table, column_name, column_def):
    cols = {r['name'] for r in db.execute(f'PRAGMA table_info({table})').fetchall()}
    if column_name not in cols:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column_def}')


def ensure_schema_migrations(db):
    ensure_column(db, 'users', 'role', 'role TEXT NOT NULL DEFAULT "Operador"')
    ensure_column(db, 'users', 'active', 'active INTEGER DEFAULT 1')
    ensure_column(db, 'users', 'must_change_password', 'must_change_password INTEGER DEFAULT 0')
    ensure_column(db, 'users', 'password_hash', 'password_hash TEXT NOT NULL DEFAULT ""')
    ensure_column(db, 'users', 'created_at', 'created_at TEXT')
    ensure_column(db, 'users', 'last_login_at', 'last_login_at TEXT')
    ensure_column(db, 'orders', 'sale_date', 'sale_date TEXT')
    ensure_column(db, 'orders', 'expected_delivery_date', 'expected_delivery_date TEXT')
    ensure_column(db, 'orders', 'invoice_number', 'invoice_number TEXT')
    ensure_column(db, 'orders', 'invoiced_at', 'invoiced_at TEXT')
    ensure_column(db, 'orders', 'payment_method', 'payment_method TEXT')
    ensure_column(db, 'orders', 'weight_kg', 'weight_kg REAL DEFAULT 0')
    ensure_column(db, 'orders', 'total_value', 'total_value REAL DEFAULT 0')
    ensure_column(db, 'orders', 'route_name', 'route_name TEXT')
    ensure_column(db, 'orders', 'city', 'city TEXT')
    ensure_column(db, 'orders', 'driver_id', 'driver_id INTEGER')
    ensure_column(db, 'orders', 'vehicle_id', 'vehicle_id INTEGER')
    ensure_column(db, 'orders', 'seller_name', 'seller_name TEXT')
    ensure_column(db, 'orders', 'delivered_at', 'delivered_at TEXT')
    ensure_column(db, 'orders', 'final_notes', 'final_notes TEXT')
    ensure_column(db, 'orders', 'receipt_photo', 'receipt_photo TEXT')
    ensure_column(db, 'orders', 'receipt_photo_at', 'receipt_photo_at TEXT')
    ensure_column(db, 'orders', 'updated_at', 'updated_at TEXT')
    ensure_column(db, 'orders', 'version', 'version INTEGER DEFAULT 1')
    db.execute('''CREATE TABLE IF NOT EXISTS delivery_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        route_id INTEGER,
        image_data TEXT NOT NULL,
        mime_type TEXT DEFAULT 'image/jpeg',
        created_at TEXT NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_delivery_receipts_order ON delivery_receipts(order_id)')
    ensure_column(db, 'delivery_receipts', 'digital_signature', 'digital_signature TEXT')
    ensure_column(db, 'delivery_receipts', 'delivered_to', 'delivered_to TEXT')
    ensure_column(db, 'delivery_receipts', 'delivered_document', 'delivered_document TEXT')
    ensure_column(db, 'delivery_receipts', 'delivered_document_type', 'delivered_document_type TEXT')
    ensure_column(db, 'delivery_receipts', 'notes', 'notes TEXT')
    ensure_column(db, 'delivery_receipts', 'latitude', 'latitude REAL')
    ensure_column(db, 'delivery_receipts', 'longitude', 'longitude REAL')
    ensure_column(db, 'delivery_receipts', 'delivery_location_link', 'delivery_location_link TEXT')
    ensure_column(db, 'orders', 'delivery_latitude', 'delivery_latitude REAL')
    ensure_column(db, 'orders', 'delivery_longitude', 'delivery_longitude REAL')
    ensure_column(db, 'orders', 'delivery_location_link', 'delivery_location_link TEXT')
    ensure_column(db, 'orders', 'delivered_document_type', 'delivered_document_type TEXT')
    ensure_column(db, 'delivery_problems', 'route_id', 'route_id INTEGER')
    ensure_column(db, 'routes', 'status', 'status TEXT DEFAULT "Planejada"')
    ensure_column(db, 'routes', 'route_name', 'route_name TEXT')
    ensure_column(db, 'routes', 'total_weight', 'total_weight REAL DEFAULT 0')
    ensure_column(db, 'routes', 'capacity', 'capacity REAL DEFAULT 11000')
    ensure_column(db, 'routes', 'notes', 'notes TEXT')
    ensure_column(db, 'routes', 'date', 'date TEXT')
    ensure_column(db, 'routes', 'created_at', 'created_at TEXT')
    ensure_column(db, 'routes', 'updated_at', 'updated_at TEXT')
    ensure_column(db, 'routes', 'version', 'version INTEGER DEFAULT 1')
    ensure_column(db, 'route_orders', 'delivery_order', 'delivery_order INTEGER DEFAULT 1')
    ensure_column(db, 'route_orders', 'status', 'status TEXT DEFAULT "Pendente"')
    ensure_column(db, 'route_cities', 'created_at', 'created_at TEXT')
    ensure_column(db, 'route_cities', 'updated_at', 'updated_at TEXT')
    ensure_column(db, 'route_cities', 'version', 'version INTEGER DEFAULT 1')
    ensure_column(db, 'clients', 'updated_at', 'updated_at TEXT')
    ensure_column(db, 'clients', 'version', 'version INTEGER DEFAULT 1')
    ensure_column(db, 'clients', 'customer_code', 'customer_code TEXT')
    ensure_column(db, 'drivers', 'updated_at', 'updated_at TEXT')
    ensure_column(db, 'drivers', 'version', 'version INTEGER DEFAULT 1')
    ensure_column(db, 'drivers', 'password_hash', 'password_hash TEXT')
    ensure_column(db, 'drivers', 'must_change_password', 'must_change_password INTEGER DEFAULT 1')
    ensure_column(db, 'vehicles', 'capacity_kg', 'capacity_kg REAL')
    ensure_column(db, 'vehicles', 'updated_at', 'updated_at TEXT')
    ensure_column(db, 'vehicles', 'version', 'version INTEGER DEFAULT 1')
    ensure_column(db, 'holidays', 'name', 'name TEXT')
    ensure_column(db, 'holidays', 'created_at', 'created_at TEXT')
    ensure_column(db, 'audit_logs', 'user_id', 'user_id INTEGER')
    ensure_column(db, 'audit_logs', 'source_ip', 'source_ip TEXT')


def ensure_indexes(db):
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_status_expected ON orders(status, expected_delivery_date)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_expected_status ON orders(expected_delivery_date, status)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_sale_date ON orders(sale_date)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_route_city ON orders(route_name, city)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_invoice ON orders(invoice_number)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_order_number ON orders(order_number)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_updated_at ON orders(updated_at)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_status_updated ON orders(status, updated_at)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_version ON orders(version)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_route_orders_route_seq ON route_orders(route_id, delivery_order)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_route_orders_order ON route_orders(order_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_routes_status_date ON routes(status, date)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_routes_status_created ON routes(status, created_at)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_routes_updated_at ON routes(updated_at)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_route_cities_active_city ON route_cities(active, city)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_route_cities_active_route ON route_cities(active, route_name)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_route_cities_version ON route_cities(version)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_clients_customer_code ON clients(customer_code)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_vehicles_capacity_kg ON vehicles(capacity_kg)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_holidays_date ON holidays(date)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_history_order_date ON order_history(order_id, created_at)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_role_permissions_role ON role_permissions(role_name)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_user_permissions_user ON user_permissions(user_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_user ON audit_logs(user_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_driver ON orders(driver_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_vehicle ON orders(vehicle_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_orders_seller ON orders(seller_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_clients_active_name ON clients(active, name)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_drivers_active ON drivers(active)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_driver_sessions_driver ON driver_sessions(driver_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_driver_sessions_expires ON driver_sessions(expires_at)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_driver_operations_route_order ON driver_delivery_operations(route_id,order_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_vehicles_active ON vehicles(active)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')
    try:
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_invoice_number_unique ON orders(invoice_number) WHERE invoice_number IS NOT NULL AND TRIM(invoice_number) <> ""')
    except Exception:
        db.execute('CREATE INDEX IF NOT EXISTS idx_orders_invoice_number_unique ON orders(invoice_number)')


def apply_named_user_permission_overrides(db):
    base_data_entry_permissions = {
        'view_dashboard',
        'view_orders',
        'create_orders',
        'edit_orders',
        'invoice_orders',
        'register_delivery_problem',
        'view_clients',
        'manage_clients',
        'view_drivers',
        'manage_drivers',
        'view_vehicles',
        'manage_vehicles',
        'view_route_catalog',
        'manage_route_catalog',
        'view_routes',
        'create_routes',
        'edit_routes',
        'view_settings',
    }
    users = db.execute('SELECT id,name,username FROM users').fetchall()
    for row in users:
        uid = int(row['id'])
        keys = {
            user_key(row['username']),
            user_key(row['name']),
        }
        if keys & FULL_ACCESS_EXTRA_USERS:
            allowed_set = set(PERMISSION_KEYS)
        elif keys & RESTRICTED_DATA_ENTRY_USERS:
            allowed_set = set(base_data_entry_permissions)
            if 'leandro' in keys:
                allowed_set.add('settle_routes')
        else:
            continue
        for perm in PERMISSION_KEYS:
            db.execute(
                'INSERT OR REPLACE INTO user_permissions(user_id,perm,allowed,updated_at) VALUES(?,?,?,?)',
                (uid, perm, 1 if perm in allowed_set else 0, now()),
            )


def init_db():
    with conn() as db:
        db_executescript(db)
        ensure_schema_migrations(db)
        ensure_indexes(db)
        defaults={'system_name':'Logística Casa do Campo','company_name':'Casa do Campo','company_subtitle':'Operação logística interna','primary_color':'#d90429','secondary_color':'#ffbf1f','accent_color':'#174f2a','background_color':'#f6f7f2','sla_ideal_days':'15','sla_limit_days':'15','load_capacity_kg':'11000','logo_file':'/static/logo.png'}
        for k,v in defaults.items(): db.execute('INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)',(k,v,now()))
        initial_admin_password = os.environ.get('LOGISTICA_INITIAL_ADMIN_PASSWORD', default_initial_password('admin'))
        if not db.execute("SELECT id FROM users WHERE username='admin'").fetchone():
            db.execute('INSERT INTO users(name,username,password_hash,role,active,must_change_password,created_at) VALUES(?,?,?,?,1,1,?)',('Administrador GOD','admin',hash_password(initial_admin_password),'GOD',now()))
        else:
            db.execute("UPDATE users SET role='GOD', name=CASE WHEN name='Administrador' THEN 'Administrador GOD' ELSE name END WHERE username='admin'")
        # Normaliza perfis legados e garante hash válido.
        for row in db.execute('SELECT id, role, password_hash FROM users').fetchall():
            canon = normalize_role(row['role'])
            if canon != (row['role'] or ''):
                db.execute('UPDATE users SET role=? WHERE id=?', (canon, row['id']))
            if not str(row['password_hash'] or '').strip():
                db.execute('UPDATE users SET password_hash=? WHERE id=?', (hash_password(secrets.token_urlsafe(10)), row['id']))
        db.execute('UPDATE users SET must_change_password=0 WHERE must_change_password IS NULL')
        db.execute("UPDATE orders SET version=1 WHERE version IS NULL")
        db.execute("""UPDATE orders
                      SET seller_name=COALESCE(
                        NULLIF(TRIM(seller_name),''),
                        (SELECT u.name FROM users u WHERE u.id=orders.seller_id),
                        'Não informado'
                      )""")
        db.execute("UPDATE routes SET updated_at=COALESCE(updated_at,created_at) WHERE updated_at IS NULL")
        db.execute("UPDATE routes SET version=1 WHERE version IS NULL")
        db.execute("UPDATE route_cities SET version=1 WHERE version IS NULL")
        db.execute("UPDATE clients SET updated_at=COALESCE(updated_at,created_at) WHERE updated_at IS NULL")
        db.execute("UPDATE clients SET version=1 WHERE version IS NULL")
        # Normalização de rotas e cidades para caixa alta (unificação total de dados históricos)
        db.execute("UPDATE route_cities SET route_name = UPPER(TRIM(COALESCE(route_name, ''))), city = UPPER(TRIM(COALESCE(city, '')))")
        db.execute("UPDATE orders SET route_name = UPPER(TRIM(COALESCE(route_name, ''))), city = UPPER(TRIM(COALESCE(city, '')))")
        db.execute("UPDATE routes SET route_name = UPPER(TRIM(COALESCE(route_name, '')))")
        db.execute("UPDATE clients SET route_name = UPPER(TRIM(COALESCE(route_name, ''))), city = UPPER(TRIM(COALESCE(city, '')))")
        db.execute("UPDATE clients SET customer_code=CAST(id AS TEXT) WHERE customer_code IS NULL OR TRIM(customer_code)=''")
        for c in db.execute('SELECT id,customer_code,name,document,phone,whatsapp,farm_name,address,reference_point,notes FROM clients').fetchall():
            db.execute(
                """UPDATE clients
                   SET customer_code=?,
                       name=?,
                       document=?,
                       phone=?,
                       whatsapp=?,
                       farm_name=?,
                       address=?,
                       reference_point=?,
                       notes=?,
                       updated_at=?
                   WHERE id=?""",
                (
                    upper_text(c['customer_code']),
                    upper_text(c['name']),
                    upper_text(c['document']),
                    upper_text(c['phone']),
                    upper_text(c['whatsapp']),
                    upper_text(c['farm_name']),
                    upper_text(c['address']),
                    upper_text(c['reference_point']),
                    upper_text(c['notes']),
                    now(),
                    c['id'],
                ),
            )
        db.execute("UPDATE drivers SET updated_at=COALESCE(updated_at,?) WHERE updated_at IS NULL",(now(),))
        db.execute("UPDATE drivers SET version=1 WHERE version IS NULL")
        for driver_row in db.execute("SELECT id FROM drivers WHERE password_hash IS NULL OR TRIM(password_hash)='' ").fetchall():
            db.execute(
                "UPDATE drivers SET password_hash=?,must_change_password=1 WHERE id=?",
                (hash_driver_password(DEFAULT_DRIVER_PASSWORD), driver_row['id']),
            )
        db.execute("UPDATE drivers SET must_change_password=1 WHERE must_change_password IS NULL")
        if 'pin' in {column['name'] for column in db.execute("PRAGMA table_info(drivers)").fetchall()}:
            db.execute("UPDATE drivers SET pin=NULL WHERE pin IS NOT NULL")
        db.execute("UPDATE vehicles SET capacity_kg=CAST(COALESCE(NULLIF(capacity,''),'0') AS REAL) WHERE capacity_kg IS NULL")
        db.execute("UPDATE vehicles SET updated_at=COALESCE(updated_at,?) WHERE updated_at IS NULL",(now(),))
        db.execute("UPDATE vehicles SET version=1 WHERE version IS NULL")
        # Permissões padrão por perfil + saneamento de registros inválidos.
        for role in ROLES:
            role_default = default_permissions_for_role(role)
            for perm in PERMISSION_KEYS:
                db.execute(
                    'INSERT OR IGNORE INTO role_permissions(role_name,perm,allowed,updated_at) VALUES(?,?,?,?)',
                    (role, perm, 1 if perm in role_default else 0, now()),
                )
        role_params = ','.join('?' for _ in ROLES)
        perm_params = ','.join('?' for _ in PERMISSION_KEYS)
        db.execute(
            f'DELETE FROM role_permissions WHERE role_name NOT IN ({role_params}) OR perm NOT IN ({perm_params})',
            tuple(ROLES) + tuple(PERMISSION_KEYS),
        )
        db.execute(
            f'DELETE FROM user_permissions WHERE perm NOT IN ({perm_params}) OR user_id NOT IN (SELECT id FROM users)',
            tuple(PERMISSION_KEYS),
        )
        apply_named_user_permission_overrides(db)
        if not db.execute('SELECT id FROM drivers LIMIT 1').fetchone(): db.execute('INSERT INTO drivers(name,phone,document,vehicle_default,active,updated_at,version,password_hash,must_change_password) VALUES(?,?,?,?,1,?,1,?,1)',('Motorista padrão','','','',now(),hash_driver_password(DEFAULT_DRIVER_PASSWORD)))
        if not db.execute('SELECT id FROM vehicles LIMIT 1').fetchone(): db.execute('INSERT INTO vehicles(name,plate,type,capacity,capacity_kg,active,updated_at,version) VALUES(?,?,?,?,?,1,?,1)',('Veículo padrão','','Caminhão','11000',11000.0,now()))
        # Migrações seguras para fluxo simplificado sem apagar histórico.
        db.execute("""UPDATE orders
                      SET status=CASE
                          WHEN status IN ('Venda criada','Aguardando faturamento') OR status LIKE 'Venda criad%' THEN 'Venda'
                          WHEN status IN ('Em separação','Em separaÃ§Ã£o','Em separa??o','Pronto para entrega') OR status LIKE 'Em separa%' THEN 'Faturado'
                          WHEN status IN ('Entrega concluída','Entrega concluida','Entrega conclu?da') OR status LIKE 'Entrega conclu%' THEN 'Acertado'
                          WHEN status='Entrega com problema' OR status LIKE 'Entrega com problema%' THEN 'Problema'
                          ELSE status
                      END""")
        db.execute("UPDATE orders SET status='Venda' WHERE status IS NULL OR TRIM(status)=''")
        db.execute("UPDATE routes SET status='Em rota',updated_at=COALESCE(updated_at,created_at),version=COALESCE(version,1) WHERE status IN ('Saiu para entrega','Em rota')")
        db.execute("UPDATE routes SET status='Acertada',updated_at=COALESCE(updated_at,created_at),version=COALESCE(version,1) WHERE status IN ('Concluída','Concluida','Conclu?da') OR status LIKE 'Conclu%da'")
        db.execute("UPDATE route_orders SET status='Em rota' WHERE status='Pendente' AND route_id IN (SELECT id FROM routes WHERE status='Em rota')")
        db.execute("""UPDATE orders
                      SET delivered_at=COALESCE(NULLIF(delivered_at,''), substr(updated_at,1,10), substr(created_at,1,10), ?)
                      WHERE status IN ('Acertado','Problema') AND (delivered_at IS NULL OR delivered_at='')""", (today(),))
        db.commit()
        if _ERP_AVAILABLE:
            try:
                _erp_connector.register_db_reader(get_setting)
                _erp_connector.register_local_db(lambda: conn())
                _erp_connector.init_cache_tables()
            except Exception:
                pass

def add_hist(db, order_id, user_id, old, new, action, notes=''):
    db.execute('INSERT INTO order_history(order_id,user_id,old_status,new_status,action,notes,created_at) VALUES(?,?,?,?,?,?,?)',(order_id,user_id,old,new,action,notes,now()))

def audit(db, user, action, module, entity='', old='', new='', notes='', source_ip=''):
    user_id = None
    if user:
        try:
            user_id = int(user['id'])
        except Exception:
            user_id = None
    record_audit(
        db,
        created_at=now(),
        user_id=user_id,
        user_name=user['name'] if user else 'Sistema',
        action=action,
        module=module,
        entity=entity,
        old_value=old,
        new_value=new,
        notes=notes,
        source_ip=str(source_ip or '').strip(),
    )

def login_page(err=''):
    system=get_setting('system_name','Logística Casa do Campo')
    company=get_setting('company_name','Casa do Campo')
    return f'''<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Login · {esc(system)}</title><link rel="stylesheet" href="/static/style.css"><script src="/static/app.js" defer></script></head>
    <body class="login-bg"><div class="login-shell"><section class="login-hero"><div class="hero-chip">Central de logística · Casa do Campo</div><h1>{esc(system)}</h1><p>Controle profissional da venda até o acerto final da carga, com SLA de 15 dias corridos, faturamento, rotas, problemas e operação em tempo real.</p><div class="hero-metrics"><span>SLA 15 dias corridos</span><span>Rotas e cargas</span><span>Acesso GOD</span></div></section><section class="login-card"><img src="/static/logo.png" class="login-logo"><h2>Acesso operacional</h2><p class="login-hint">Entre com seu usuário interno autorizado.</p>{f'<div class="alert danger">{esc(err)}</div>' if err else ''}<form method="post" action="/login" autocomplete="off"><label>Usuário<input name="username" required autofocus autocomplete="off" value=""></label><label>Senha<input name="password" type="password" required autocomplete="off" value=""></label><button>Entrar no sistema</button></form></section></div></body></html>'''



def tutorial_for(title):
    t = str(title or '')
    rules = [
        ('Dashboard', 'Use os KPIs para puxar prioridade do dia: vendas abertas, faturados sem saída, em rota, acerto pendente, SLA e problemas.'),
        ('Pedidos', 'Filtre por status, cidade, rota, cliente, NF e prazo para agir no pedido certo sem perder tempo.'),
        ('Novo Pedido', 'Preencha venda, cliente e rota; o prazo de SLA é calculado automaticamente em 15 dias corridos.'),
        ('Editar Pedido', 'Ajuste dados do pedido e confirme status, cidade e rota antes de salvar.'),
        ('Faturamento', 'Fature pedidos em Venda: informe NF e data para liberar a carga.'),
        ('Cargas e Rotas', 'Primeiro veja faturados disponíveis, depois opere planejadas, em rota e histórico acertado.'),
        ('Nova Carga', 'Selecione pedidos faturados, confira capacidade e confirme a montagem.'),
        ('Acerto de carga', 'Busque a carga em rota, confira checklist por pedido e conclua com entregue ou problema.'),
        ('Operação da Carga', 'Organize sequência, despache e registre entregue ou problema por pedido.'),
        ('SLA', 'Monitore conformidade no ciclo venda → faturado → rota → acerto, com risco, atraso e impacto dos feriados.'),
        ('Clientes', 'Mantenha cadastro atualizado para reduzir erro de endereço e rota.'),
        ('Motoristas', 'Cadastre somente motoristas ativos para evitar escala inválida.'),
        ('Veículos', 'Capacidade correta no cadastro evita montar carga acima do limite.'),
        ('Relatórios', 'Filtre período, rota e status para extrair visão operacional e exportar CSV.'),
        ('Backup', 'Gere backup regular antes de mudanças operacionais importantes.'),
        ('Configurações', 'Apenas GOD altera parâmetros globais, permissões e auditoria.'),
    ]
    text = 'Siga o fluxo da tela, salve e confira o resultado na lista ou histórico.'
    for key, val in rules:
        if key.lower() in t.lower():
            text = val; break
    return f'<div class="mini-tutorial"><b>Passo a passo:</b> {esc(text)}</div>'


def contextual_help_topics(title):
    t = str(title or '').strip().lower()
    guides = [
        ('dashboard', [
            ('Vendas abertas', 'Pedidos em status Venda que ainda precisam de faturamento.'),
            ('Faturados aguardando saída', 'Pedidos faturados que ainda não foram colocados em carga/rota.'),
            ('Saiu para entrega', 'Pedidos vinculados em cargas com saída registrada e aguardando acerto.'),
            ('Fora do SLA', 'Pedidos com prazo limite vencido e ainda não finalizados.'),
        ]),
        ('pedido', [
            ('Status', 'Etapa atual do pedido: Venda, Faturado, Saiu para entrega, Acertado, Problema ou Cancelado.'),
            ('Prazo limite', 'Data de referência do SLA operacional para priorização da entrega.'),
            ('Rota', 'Rota planejada para agrupar pedidos na montagem de carga.'),
            ('NF', 'Número da nota fiscal vinculada ao faturamento do pedido.'),
        ]),
        ('cargas e rotas', [
            ('Planejada', 'Carga montada e pronta para sequência/ajustes antes da saída.'),
            ('Em rota', 'Carga já despachada, aguardando acerto final com checklist.'),
            ('Histórico', 'Cargas finalizadas (acertadas, com problema ou canceladas).'),
            ('Capacidade', 'Peso total da carga em relação ao limite do veículo selecionado.'),
        ]),
        ('nova carga', [
            ('Travar rota', 'Quando marcado, mostra somente pedidos da rota selecionada para facilitar a montagem.'),
            ('Pedidos elegíveis', 'Somente pedidos faturados ou em rota, com validações de consistência operacional.'),
            ('Capacidade kg', 'Limite de peso aceito para a carga; acima disso o sistema bloqueia confirmação.'),
        ]),
        ('operação da carga', [
            ('Sequência', 'Ordem planejada das entregas na rota.'),
            ('Marcar saída', 'Move a carga para Em rota e os pedidos para Saiu para entrega.'),
            ('Acerto da carga', 'Tela para concluir cada pedido como entregue ou problema.'),
        ]),
        ('acerto de carga', [
            ('Checklist conferido', 'Obrigatório por pedido para concluir o acerto da carga.'),
            ('Resultado', 'Define se o pedido foi entregue ou se houve problema operacional.'),
            ('Observação do acerto', 'Registro resumido do recebedor, divergência ou ocorrência.'),
        ]),
        ('sla', [
            ('Dentro do SLA', 'Pedidos entregues no prazo definido.'),
            ('Risco', 'Pedidos próximos do vencimento e que exigem atenção.'),
            ('Fora do SLA', 'Pedidos com prazo vencido para ação prioritária.'),
        ]),
        ('relatórios', [
            ('Filtro por período', 'Define intervalo de datas para consolidar os resultados.'),
            ('Relatório por status/rota/cidade', 'Mostra distribuição operacional por etapa e localização.'),
            ('Exportar CSV', 'Gera arquivo para conferência externa e análise administrativa.'),
        ]),
        ('clientes', [
            ('Cidade e rota', 'Dados usados para direcionar pedido na montagem de carga.'),
            ('Status ativo/inativo', 'Inativo impede uso em novos fluxos, preservando histórico existente.'),
        ]),
        ('motoristas', [
            ('Cadastro ativo', 'Somente motoristas ativos podem ser selecionados em cargas.'),
            ('Documento/telefone', 'Dados de contato e rastreabilidade operacional.'),
        ]),
        ('veículos', [
            ('Capacidade', 'Valor base para validação de peso da carga.'),
            ('Placa', 'Identificador único; duplicidade ativa é bloqueada pelo sistema.'),
        ]),
        ('configurações', [
            ('Minha conta', 'Atualização de nome, usuário e senha do usuário logado.'),
            ('Usuários', 'Criação, edição, reset de senha e ativação/inativação conforme permissão.'),
            ('Permissões', 'Matriz de acesso por perfil e por usuário (override).'),
            ('Auditoria', 'Rastro de ações críticas com usuário, IP e alterações antes/depois.'),
        ]),
        ('backup', [
            ('Gerar backup', 'Cria cópia do banco para recuperação operacional.'),
            ('Restaurar backup', 'Substitui base atual com confirmação forte e registro de motivo.'),
            ('Validação semanal', 'Indica se a conferência automatizada de restauração passou ou falhou.'),
        ]),
    ]
    for key, topics in guides:
        if key in t:
            return topics
    return [
        ('Objetivo da tela', 'Use os filtros e ações do topo para executar o fluxo operacional com segurança.'),
        ('Ações críticas', 'Ações de exclusão, cancelamento e restauração exigem confirmação.'),
        ('Histórico', 'Sempre que possível, prefira inativação para preservar rastreabilidade.'),
    ]


def contextual_help_widget(title):
    topics = contextual_help_topics(title)
    rows = ''.join(f'<li><b>{esc(k)}:</b> {esc(v)}</li>' for k, v in topics)
    return (
        "<details class='help-popover'>"
        "<summary class='help-trigger' title='Ajuda desta tela' aria-label='Ajuda desta tela'>?</summary>"
        f"<div class='help-content'><h3>Guia rápido</h3><ul>{rows}</ul></div>"
        "</details>"
    )

def layout(user,title,content,subtitle=None):
    system=get_setting('system_name','Logística Casa do Campo'); company=get_setting('company_name','Casa do Campo'); sub=get_setting('company_subtitle','Operação logística interna')
    primary=get_setting('primary_color','#d90429'); secondary=get_setting('secondary_color','#ffbf1f'); accent=get_setting('accent_color','#174f2a'); bg=get_setting('background_color','#f6f7f2'); logo=get_setting('logo_file','/static/logo.png')

    # --- Sidebar: 3 grupos contextuais ---
    op_items = []
    monitor_items = []
    config_items = []

    if user_can(user,'view_dashboard'):      op_items.append(('/dashboard','▦','Painel'))
    if user_can(user,'view_orders'):         op_items.append(('/orders','▤','Pedidos'))
    if user_can(user,'create_orders'):       op_items.append(('/orders/new','＋','Novo Pedido'))
    if user_can(user,'invoice_orders'):      op_items.append(('/faturamento','▣','Faturamento'))
    if user_can(user,'view_routes'):         op_items.append(('/routes','⇄','Cargas/Rotas'))
    if user_can(user,'settle_routes'):       op_items.append(('/load-settlement','☑','Acerto de Carga'))

    if user_can(user,'view_sla'):            monitor_items.append(('/sla','◷','SLA & Prazos'))
    if user_can(user,'view_reports'):        monitor_items.append(('/relatorios','▥','Relatórios'))

    if user_can(user,'view_clients'):        config_items.append(('/clients','◉','Clientes'))
    if user_can(user,'view_drivers'):        config_items.append(('/drivers','◈','Motoristas'))
    if user_can(user,'view_vehicles'):       config_items.append(('/vehicles','▰','Veículos'))
    if user_can(user,'view_route_catalog'):  config_items.append(('/route-cities','◎','Cidades/Rotas'))
    if user_can(user,'view_backup'):         config_items.append(('/backup','⤓','Backup'))
    if user_can(user,'view_settings'):       config_items.append(('/settings','⚙','Configurações'))
    # Link ERP Admin: apenas GOD
    if is_god(user):                          config_items.append(('/admin/erp','⚡','Integração ERP'))

    def render_group(label, items):
        if not items: return ''
        links = ''.join(f'<a href="{h}"><span>{i}</span>{l}</a>' for h,i,l in items)
        return f'<span class="nav-section-label">{label}</span>{links}'

    nav = render_group('Operação', op_items) + render_group('Monitoramento', monitor_items) + render_group('Configuração', config_items)

    # --- Topbar: chips de alerta em tempo real ---
    try:
        with conn() as db:
            n_late   = db.execute("SELECT COUNT(*) c FROM orders WHERE expected_delivery_date<? AND status NOT IN ('Acertado','Problema','Cancelado','Agendado')",(today(),)).fetchone()['c']
            n_risk   = db.execute("SELECT COUNT(*) c FROM orders WHERE expected_delivery_date BETWEEN ? AND ? AND status NOT IN ('Acertado','Problema','Cancelado','Agendado')",(today(),date_add(SLA_RISK_DAYS))).fetchone()['c']
            n_no_nf  = db.execute("SELECT COUNT(*) c FROM orders WHERE status='Venda' AND (invoice_number IS NULL OR TRIM(invoice_number)='')").fetchone()['c']
    except Exception:
        n_late = n_risk = n_no_nf = 0

    alert_chips = ''
    if n_late > 0:
        alert_chips += f'<a class="topbar-alert-chip critical" href="/orders?late=1" title="Pedidos com SLA vencido">🔴 {n_late} vencido{"s" if n_late>1 else ""}</a>'
    if n_risk > 0:
        alert_chips += f'<a class="topbar-alert-chip risk" href="/orders?near=1" title="Pedidos prestes a vencer SLA">🟡 {n_risk} em risco</a>'
    if n_no_nf > 0:
        alert_chips += f'<a class="topbar-alert-chip info" href="/orders?status=Venda" title="Pedidos em venda sem NF">📋 {n_no_nf} sem NF</a>'
    topbar_alerts = f'<div class="topbar-alerts">{alert_chips}</div>' if alert_chips else ''

    cssvars=f'--primary:{primary};--secondary:{secondary};--accent:{accent};--bg:{bg};'
    new_order_btn = '<a class="btn ghost" href="/orders/new">Novo pedido</a>' if user_can(user,'create_orders') else ''
    help_widget = contextual_help_widget(title)
    return f'''<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)} · {esc(system)}</title><link rel="stylesheet" href="/static/style.css"><script src="/static/app.js" defer></script></head><body style="{cssvars}"><aside class="sidebar"><div class="brand"><img src="{esc(logo)}" alt="Logo"><div><b>{esc(system)}</b><span>{esc(company)}</span></div></div><nav>{nav}</nav><div class="side-foot"><b>{esc(sub)}</b><small>Fluxo: venda → faturado → saiu para entrega → acerto</small></div></aside><main class="main"><header class="topbar"><form class="search" method="get" action="/orders"><input name="q" placeholder="Buscar pedido, NF, cliente, cidade... (Ctrl+K)"></form>{topbar_alerts}<div class="userbox"><div><b>{esc(user['name'])}</b><small>{esc(normalize_role(user['role']))}</small></div><a href="/logout">Sair</a></div></header><section class="content"><div class="page-head"><div><h1>{esc(title)}</h1><p>{esc(subtitle or (company+' · operação logística local'))}</p></div><div class="head-actions">{help_widget}{new_order_btn}<button id="btnPrintPage" class="btn print">Imprimir</button></div></div>{tutorial_for(title)}{content}</section></main>
    
    <!-- Drawer Lateral de Faturamento Inline -->
    <div id="invoiceDrawer" class="drawer-overlay">
      <div class="drawer">
        <div class="drawer-header">
          <h3>Faturar Pedido <span id="drawer_order_number"></span></h3>
          <button id="invoiceDrawerClose" class="drawer-close">&times;</button>
        </div>
        <form id="invoiceDrawerForm" class="drawer-body">
          <input type="hidden" name="order_id" id="drawer_order_id">
          <div class="drawer-section">
            <h4>Dados do Pedido</h4>
            <div class="drawer-grid">
              <div><small>Cliente</small><p id="drawer_client"></p></div>
              <div><small>Cidade/Rota</small><p id="drawer_city"></p></div>
              <div><small>Vendedor</small><p id="drawer_seller"></p></div>
              <div><small>Peso</small><p id="drawer_weight"></p></div>
              <div><small>Prazo Limite</small><p id="drawer_deadline"></p></div>
            </div>
          </div>
          <div class="drawer-section">
            <h4>Dados de Faturamento</h4>
            <label class="form-group">
              <span>Número da Nota Fiscal</span>
              <input name="invoice_number" id="drawer_invoice_number" placeholder="Número da NF" required autocomplete="off">
            </label>
            <label class="form-group">
              <span>Data de Faturamento</span>
              <input type="date" name="invoiced_at" id="drawer_invoiced_at" required>
            </label>
          </div>
          <div class="drawer-footer">
            <button type="button" id="btnCancelInvoice" class="btn ghost">Cancelar</button>
            <button type="submit" class="btn primary">Confirmar Faturamento</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Indicador de Sincronização em Tempo Real -->
    <div id="realtimeIndicator" class="realtime-indicator">
      <span class="realtime-dot"></span>
      <span id="realtimeText">Conectado em tempo real</span>
    </div>

    <!-- Universal Table Column Sorter -->
    <script>
    (function() {{
      function initGlobalTableSorter() {{
        var tables = document.querySelectorAll('table, .table, .data-table, .sortable-table');
        tables.forEach(function(table) {{
          if (table.dataset.sortableInitialized) return;
          table.dataset.sortableInitialized = 'true';

          var thead = table.querySelector('thead');
          var tbody = table.querySelector('tbody');
          if (!thead || !tbody) return;

          var headers = thead.querySelectorAll('th');
          headers.forEach(function(th, colIdx) {{
            var rawText = th.textContent.trim();
            if (!rawText || rawText.toLowerCase() === 'ações' || rawText.toLowerCase() === 'acoes' || th.querySelector('input, button, select')) {{
              return;
            }}

            th.style.cursor = 'pointer';
            th.style.userSelect = 'none';
            th.title = 'Clique para ordenar por ' + rawText;

            var icon = document.createElement('span');
            icon.className = 'table-sort-icon';
            icon.style.display = 'inline-block';
            icon.style.marginLeft = '5px';
            icon.style.fontSize = '0.75rem';
            icon.style.opacity = '0.35';
            icon.style.transition = 'transform 0.15s, opacity 0.15s';
            icon.innerHTML = '↕';
            th.appendChild(icon);

            th.addEventListener('click', function(e) {{
              var tgt = e.target;
              if (tgt.tagName === 'INPUT' || tgt.tagName === 'BUTTON' || tgt.tagName === 'SELECT' || tgt.tagName === 'A' || tgt.closest('a, button, input, select')) {{
                return;
              }}

              headers.forEach(function(otherTh) {{
                if (otherTh !== th) {{
                  otherTh.dataset.sortDir = '0';
                  var otherIcon = otherTh.querySelector('.table-sort-icon');
                  if (otherIcon) {{
                    otherIcon.innerHTML = '↕';
                    otherIcon.style.opacity = '0.35';
                    otherIcon.style.color = '';
                  }}
                  otherTh.style.backgroundColor = '';
                }}
              }});

              var currentDir = (th.dataset.sortDir === '1') ? -1 : 1;
              th.dataset.sortDir = String(currentDir);

              var sortIcon = th.querySelector('.table-sort-icon');
              if (sortIcon) {{
                sortIcon.innerHTML = currentDir === 1 ? '▲' : '▼';
                sortIcon.style.opacity = '1';
                sortIcon.style.color = '#166534';
              }}
              th.style.backgroundColor = 'rgba(22, 101, 52, 0.08)';

              var rows = Array.from(tbody.querySelectorAll('tr'));

              function extractVal(tr, idx) {{
                var cell = tr.children[idx];
                if (!cell) return '';
                var text = cell.textContent.trim();

                // 1. SLA / Datas BR (DD/MM/YYYY)
                var brDate = text.match(/(\\d{{2}})\\/(?:\\d{{2}})\\/(?:\\d{{4}})/);
                if (brDate) {{
                  var pts = text.split('/');
                  if (pts.length === 3) return pts[2] + pts[1] + pts[0];
                }}

                // 2. SLA Dias (Atrasado Xd -> -X, Vence em Xd -> X, Xd restantes -> X)
                var slaAtrasado = text.match(/atrasado\\s*(\\d+)d/i);
                if (slaAtrasado) return -parseInt(slaAtrasado[1], 10);
                var slaVence = text.match(/(?:vence em|restantes)?\\s*(\\d+)d/i);
                if (slaVence && text.toLowerCase().indexOf('d') !== -1) return parseInt(slaVence[1], 10);

                // 3. Peso (X kg) e Valor (R$ X)
                var numClean = text.replace(/R\\$\\s*/gi, '').replace(/\\s*kg/gi, '').replace(/\\./g, '').replace(',', '.').replace(/[^0-9.-]/gi, '');
                if (numClean && !isNaN(numClean) && text.match(/\\d/)) {{
                  return parseFloat(numClean);
                }}

                return text.toLowerCase();
              }}

              rows.sort(function(a, b) {{
                var vA = extractVal(a, colIdx);
                var vB = extractVal(b, colIdx);

                if (typeof vA === 'number' && typeof vB === 'number') {{
                  return currentDir === 1 ? vA - vB : vB - vA;
                }}
                return currentDir === 1 ? String(vA).localeCompare(String(vB), 'pt-BR') : String(vB).localeCompare(String(vA), 'pt-BR');
              }});

              rows.forEach(function(r) {{ tbody.appendChild(r); }});
            }});
          }});
        }});
      }}

      if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', initGlobalTableSorter);
      }} else {{
        initGlobalTableSorter();
      }}
      setInterval(initGlobalTableSorter, 2000);
    }})();
    </script>
    </body></html>'''


class App(BaseHTTPRequestHandler):
    def send_response(self, code, message=None):
        self._last_status_code = code
        super().send_response(code, message)
    def setup(self):
        super().setup()
        _thread_local.current_handler = self
        self._perm_cache = {}
        self._request_started_at = time.perf_counter()
        self._request_timing_recorded = False
        try:
            self.connection.settimeout(REQUEST_SOCKET_TIMEOUT_SECONDS)
        except Exception:
            pass

    def log_message(self,*a): return

    def mark_request_timing(self):
        if getattr(self, '_request_timing_recorded', False):
            return
        started = getattr(self, '_request_started_at', None)
        if started is None:
            return
        elapsed_ms = max(0.0, (time.perf_counter() - float(started)) * 1000.0)
        record_request_latency_ms(elapsed_ms)
        self._request_timing_recorded = True

    def _common_headers(self):
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Permitted-Cross-Domain-Policies', 'none')
        self.send_header('Permissions-Policy', 'geolocation=(), microphone=(), camera=(), payment=()')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        try:
            request_path = urlparse(self.path).path
        except Exception:
            request_path = ''
        if request_path.startswith('/static/driver_app/') or request_path.startswith('/api/v1/driver/'):
            csp = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        else:
            csp = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        self.send_header('Content-Security-Policy', csp)

    def send_error(self, code, message=None, explain=None):
        title_map = {
            400: 'Requisição inválida',
            401: 'Acesso não autorizado',
            403: 'Acesso negado',
            404: 'Não encontrado',
            409: 'Conflito de operação',
            500: 'Erro interno',
            503: 'Serviço temporariamente indisponível',
        }
        msg_map = {
            400: 'Não foi possível concluir sua solicitação com os dados informados.',
            401: 'Seu acesso não foi autorizado. Faça login novamente.',
            403: 'Seu usuário não possui permissão para esta ação.',
            404: 'A tela ou recurso solicitado não foi encontrado.',
            409: 'A ação não pôde ser concluída neste momento por conflito de atualização.',
            500: 'Não foi possível concluir a ação agora. Tente novamente em instantes.',
            503: 'O sistema está temporariamente indisponível. Tente novamente em instantes.',
        }
        title = title_map.get(int(code or 500), 'Erro')
        fallback = msg_map.get(int(code or 500), 'Não foi possível concluir esta ação agora.')
        msg = safe_user_error_message(message, fallback=fallback)
        try:
            req_path = urlparse(self.path).path
        except Exception:
            req_path = ''
        if req_path.startswith('/static/'):
            payload = f'{title}: {msg}'
            data = payload.encode('utf-8')
            self.send_response(int(code or 500))
            self._common_headers()
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            self.mark_request_timing()
            return
        try:
            u = self.user()
        except Exception:
            u = None
        if u:
            return self.fail(u, title, msg, int(code or 500))
        body = f"<!doctype html><html lang='pt-br'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>{esc(title)}</title><link rel='stylesheet' href='/static/style.css'></head><body class='login-bg'><div class='login-shell'><section class='login-card'><h2>{esc(title)}</h2><div class='alert danger'>{esc(msg)}</div><a class='btn ghost' href='/login'>Voltar para o login</a></section></div></body></html>"
        return self.send_html(body, int(code or 500))

    def send_html(self,s,st=200):
        b=s.encode('utf-8'); self.send_response(st); self._common_headers(); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); self.mark_request_timing()

    def send_json(self, payload, st=200):
        b = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(st)
        self._common_headers()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)
        self.mark_request_timing()

    def send_file(self,path,ctype='application/octet-stream'):
        if not os.path.exists(path): self.send_error(404); return
        data=open(path,'rb').read(); self.send_response(200); self._common_headers(); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); self.mark_request_timing()

    def send_response_bytes(self, b_data, ctype='image/jpeg', st=200):
        self.send_response(st)
        self._common_headers()
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.send_header('Content-Length', str(len(b_data)))
        self.end_headers()
        self.wfile.write(b_data)
        self.mark_request_timing()
    def redirect(self,p):
        self.send_response(302)
        self._common_headers()
        self.send_header('Location',p)
        self.end_headers()
        self.mark_request_timing()

    def post_data(self):
        if hasattr(self, '_cached_post_data'):
            return self._cached_post_data
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 10 * 1024 * 1024:  # Limite de 10 MB para evitar Denial of Service (OOM)
            raise ValueError('Tamanho da requisição excede o limite máximo permitido de 10MB.')
        raw = self.rfile.read(content_length).decode('utf-8', errors='ignore')
        self._raw_post_body = raw
        ctype = (self.headers.get('Content-Type') or '').lower()
        if 'application/json' in ctype or raw.strip().startswith('{') or raw.strip().startswith('['):
            try:
                parsed_json = json.loads(raw)
                if isinstance(parsed_json, dict):
                    self._cached_post_data = parsed_json
                    return self._cached_post_data
            except Exception:
                pass
        self._cached_post_data = {k: v[0] if v else '' for k, v in parse_qs(raw, keep_blank_values=True).items()}
        return self._cached_post_data

    def client_ip(self):
        # Apenas confia em X-Forwarded-For se explicitamente configurado no ambiente (ex: atrás de reverse proxy confiável)
        trust_xff = os.environ.get('TRUST_X_FORWARDED_FOR', '0') == '1'
        xff = (self.headers.get('X-Forwarded-For') or '').strip()
        if trust_xff and xff:
            return xff.split(',')[0].strip()
        return str(self.client_address[0] if self.client_address else '').strip()

    def is_https(self):
        proto = (self.headers.get('X-Forwarded-Proto') or '').strip().lower()
        return proto == 'https'

    def session_data(self):
        if hasattr(self, '_cached_session_data'):
            return self._cached_session_data
        c = cookies.SimpleCookie(self.headers.get('Cookie'))
        sid_cookie = c.get('sid')
        if not sid_cookie:
            self._cached_session_data = (None, None)
            return self._cached_session_data
        sid = sid_cookie.value
        with SESSIONS_LOCK:
            sess = SESSIONS.get(sid)
        if not isinstance(sess, dict):
            with SESSIONS_LOCK:
                SESSIONS.pop(sid, None)
            self._cached_session_data = (None, None)
            return self._cached_session_data
        self._cached_session_data = (sid, sess)
        return self._cached_session_data

    def same_origin_ok(self):
        origin = (self.headers.get('Origin') or '').strip().lower()
        referer = (self.headers.get('Referer') or '').strip().lower()
        forwarded_host = (self.headers.get('X-Forwarded-Host') or '').strip().lower()
        if ',' in forwarded_host:
            forwarded_host = forwarded_host.split(',', 1)[0].strip()
        host_header = forwarded_host or (self.headers.get('Host') or '').strip().lower()
        forwarded_port_raw = (self.headers.get('X-Forwarded-Port') or '').strip()

        # Alguns navegadores/proxies podem enviar Origin: null em contextos de privacidade.
        if origin == 'null':
            origin = ''
        if referer == 'null':
            referer = ''
        if not (origin or referer):
            return True

        def split_host_port(raw_host, default_port):
            value = str(raw_host or '').strip().lower()
            if not value:
                return '', int(default_port)
            if value.startswith('['):
                end = value.find(']')
                if end > 0:
                    h = value[1:end]
                    rest = value[end + 1:]
                    if rest.startswith(':') and rest[1:].isdigit():
                        return h, int(rest[1:])
                    return h, int(default_port)
            if ':' in value and value.count(':') == 1:
                h, p = value.rsplit(':', 1)
                if p.isdigit():
                    return h, int(p)
            return value, int(default_port)

        req_host, req_port = split_host_port(host_header, PORT)
        if forwarded_port_raw.isdigit():
            try:
                req_port = int(forwarded_port_raw)
            except Exception:
                req_port = int(req_port or PORT)
        allowed_pairs = {
            ('127.0.0.1', int(PORT)),
            ('localhost', int(PORT)),
        }
        if req_host:
            allowed_pairs.add((req_host, int(req_port or PORT)))

        extra_hosts = [h.strip().lower() for h in str(os.environ.get('LOGISTICA_ALLOWED_HOSTS') or '').split(',') if h.strip()]
        for h in extra_hosts:
            allowed_pairs.add((h, int(req_port or PORT)))
            allowed_pairs.add((h, int(PORT)))

        def candidate_pair(url_value):
            try:
                parsed = urlparse(str(url_value or ''))
                host = str(parsed.hostname or '').strip().lower()
                if not host:
                    return None
                port = parsed.port
                if port is None:
                    port = 443 if (parsed.scheme or '').lower() == 'https' else 80
                return (host, int(port))
            except Exception:
                return None

        if not str(origin or '').strip() and not str(referer or '').strip():
            # Alguns navegadores/rede local podem omitir Origin/Referer em POST interno.
            # Nesses casos, a validação CSRF por token continua obrigatória.
            return True

        for candidate in (origin, referer):
            if not candidate:
                continue
            pair = candidate_pair(candidate)
            if not pair:
                continue
            if pair in allowed_pairs:
                return True
        return False

    def conn(self):
        return conn()

    def validate_csrf(self):
        sid, sess = self.session_data()
        if not sid or not sess:
            return False
        c = cookies.SimpleCookie(self.headers.get('Cookie'))
        cookie_token = str((c.get('csrf_token').value if c.get('csrf_token') else '') or '')
        data = self.post_data()
        header_token = str(self.headers.get('X-CSRF-Token') or self.headers.get('x-csrf-token') or '').strip()
        form_token = str((data.get('_csrf') or header_token) or '').strip()
        session_token = str((sess.get('csrf') or '') or '')
        if not (cookie_token and form_token and session_token):
            return False
        if len(form_token) < 16:
            return False
        return hmac.compare_digest(cookie_token, form_token) and hmac.compare_digest(session_token, form_token)

    def user(self):
        runtime_state_cleanup()
        sid, sess = self.session_data()
        if not sid or not sess:
            return None
        now_ts = time.time()
        if float(sess.get('exp', 0)) < now_ts:
            with SESSIONS_LOCK:
                SESSIONS.pop(sid, None)
            return None
        with SESSIONS_LOCK:
            if sid in SESSIONS:
                SESSIONS[sid]['exp'] = now_ts + SESSION_MAX_AGE_SECONDS
        with conn() as db:
            user = db.execute('SELECT * FROM users WHERE id=? AND active=1',(sess.get('uid'),)).fetchone()
        if not user:
            with SESSIONS_LOCK:
                SESSIONS.pop(sid, None)
            return None
        return user

    def require(self):
        u=self.user()
        if not u: self.redirect('/login')
        return u

    def must_change_password(self, user):
        if not user:
            return False
        try:
            return int(user['must_change_password'] or 0) == 1
        except Exception:
            return False

    def force_password_page(self, u, err=''):
        msg = safe_user_error_message(err, 'Atualize sua senha para continuar.')
        _, sess = self.session_data()
        csrf_val = esc((sess or {}).get('csrf') or '')
        body = f"""<section class='panel'>
            <h2>Troca de senha obrigatória</h2>
            <p class='muted'>Seu acesso foi liberado com senha temporária. Defina uma nova senha para continuar usando o sistema.</p>
            {f'<div class="alert danger">{esc(msg)}</div>' if err else '<div class="alert info">Defina uma senha nova e confirme para liberar o uso do sistema.</div>'}
            <form method='post' action='/force-password' class='form compact'>
                <input type='hidden' name='_csrf' value='{csrf_val}'>
                <div class='grid3'>
                    <label>Nova senha<input type='password' name='new_password' required placeholder='Mínimo 8 caracteres'></label>
                    <label>Confirmar nova senha<input type='password' name='confirm_password' required placeholder='Repita a senha'></label>
                </div>
                <button>Salvar nova senha e continuar</button>
            </form>
        </section>"""
        return self.send_html(layout(u, 'Trocar senha', body, 'A atualização é obrigatória antes de continuar'))

    def fail(self, u, title, message, status=400):
        back = '<a class="btn ghost" href="javascript:history.back()">Voltar</a>'
        safe_message = safe_user_error_message(message, fallback='Não foi possível concluir esta ação agora.')
        if u:
            return self.send_html(layout(u,title,f'<div class="alert danger">{esc(safe_message)}</div>{back}'),status)
        return self.send_html(login_page(safe_message),status)

    def require_god(self, u, message='Sem permissão para esta ação.'):
        if is_god(u):
            return True
        self.fail(u,'Acesso negado',message,403)
        return False

    def has_perm(self, u, perm):
        with conn() as db:
            return has_permission(db, u, perm)

    def can_view_financial(self, u):
        return self.has_perm(u, 'view_financial')

    def require_perm(self, u, perm, message='Sem permissão para esta ação.'):
        if self.has_perm(u, perm):
            return True
        self.fail(u, 'Acesso negado', message, 403)
        return False

    def path_int(self, path, idx):
        parts = path.split('/')
        if idx >= len(parts) or not str(parts[idx]).isdigit():
            raise ValueError('ID inválido na rota.')
        return int(parts[idx])
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path=urlparse(self.path).path
        try:
            if path.startswith('/static/'):
                rel_path = path[len('/static/'):].lstrip('/')
                fp = os.path.abspath(os.path.join(STATIC_DIR, rel_path))
                if not fp.startswith(os.path.abspath(STATIC_DIR)):
                    self.send_error(403)
                    return
                ext = os.path.splitext(fp)[1].lower()
                c = {'.css':'text/css; charset=utf-8','.js':'application/javascript; charset=utf-8','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.svg':'image/svg+xml','.json':'application/json; charset=utf-8','.html':'text/html; charset=utf-8'}.get(ext,'application/octet-stream')
                return self.send_file(fp,c)
            if path == '/favicon.ico':
                icon_path = os.path.join(STATIC_DIR, 'logo.png')
                if os.path.exists(icon_path):
                    return self.send_file(icon_path, 'image/png')
                self.send_error(404)
                return
            if path in ('/driver-app', '/driver-app/'):
                return self.send_file(os.path.join(STATIC_DIR, 'driver_app', 'index.html'), 'text/html; charset=utf-8')
            if path == '/healthz':
                runtime_state_cleanup()
                with SESSIONS_LOCK:
                    active_sessions = len(SESSIONS)
                return self.send_json({
                    'ok': True,
                    'status': 'ok',
                    'service': 'logistica-casa-do-campo',
                    'api_version': 'v1',
                    'driver_api_version': 1,
                    'system_version': SYSTEM_VERSION,
                    'time': now(),
                    'active_sessions': active_sessions,
                })
            if path == '/events':
                u = self.user()
                if not u:
                    self.send_response(401)
                    self._common_headers()
                    self.end_headers()
                    return

                self.send_response(200)
                self._common_headers()
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                self.send_header('X-Accel-Buffering', 'no')
                self.end_headers()

                try:
                    self.wfile.write(b"data: {\"type\": \"connected\"}\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
                return

            if path=='/login': return self.send_html(login_page())
            if path.startswith('/api/v1/driver/'):
                if driver_api_dispatch.handle_driver_api_request(self, path, 'GET'):
                    return
            if path=='/logout':
                c=cookies.SimpleCookie(self.headers.get('Cookie')); sid=c.get('sid')
                logout_user=None
                if sid:
                    with SESSIONS_LOCK:
                        sess = SESSIONS.get(sid.value)
                if isinstance(sess, dict):
                    with conn() as db:
                        logout_user = find_user_by_id(db, int(sess.get('uid') or 0))
                if sid:
                    with SESSIONS_LOCK:
                        SESSIONS.pop(sid.value,None)
                if logout_user:
                    with conn() as db:
                        audit(db,logout_user,'Logout','Acesso',logout_user['username'],'','',f'IP: {self.client_ip()}', source_ip=self.client_ip())
                        db.commit()
                self.send_response(302)
                self._common_headers()
                self.send_header('Location','/login')
                self.send_header('Set-Cookie','sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax')
                self.send_header('Set-Cookie','csrf_token=; Path=/; Max-Age=0; SameSite=Lax')
                self.end_headers()
                return
            if path=='/force-password':
                u=self.require()
                if not u:
                    return
                if not self.must_change_password(u):
                    self.redirect('/dashboard')
                    return
                return self.force_password_page(u)
            u=self.require()
            if not u: return
            if self.must_change_password(u):
                self.redirect('/force-password')
                return
            if get_setting('maintenance_mode','off') == 'on' and not is_god(u):
                return self.fail(u,'Sistema em manutenção','O sistema está temporariamente em manutenção. Tente novamente em instantes.',503)
            if path == '/api/clients/duplicate-check':
                return self.get_client_duplicate_check(u)
            routes = GET_ROUTE_HANDLERS
            route_permissions = GET_ROUTE_PERMISSIONS
            if orders_dispatch.handle_get(self, path, u):
                return
            if routes_dispatch.handle_get(self, path, u):
                return
            if reports_dispatch.handle_get(self, path, u):
                return
            if _ERP_ADMIN_DISPATCH_OK and path.startswith('/admin/erp'):
                if not is_god(u):
                    return self.fail(u, 'Acesso negado', 'Esta área é exclusiva para administradores GOD.', 403)
                _erp_admin_dispatch.handle_get(self, path, u)
                return
            if path in routes:
                perm = route_permissions.get(path)
                if perm and not self.require_perm(u, perm):
                    return
                return getattr(self,routes[path])(u)
            return self.fail(u,'Não encontrado','Tela/rota não encontrada.',404)
        except ValueError as e:
            u=self.user()
            return self.fail(u,'URL inválida',safe_user_error_message(e, 'Não foi possível abrir esta tela com os dados informados.'),400)
        except sqlite3.OperationalError as e:
            u=self.user()
            return self.fail(u,'Banco indisponível',friendly_db_error_message(e),409)
        except sqlite3.IntegrityError as e:
            u=self.user()
            return self.fail(u,'Dados inválidos',friendly_db_error_message(e),400)
        except Exception as e:
            log_server_error(f'GET {path}', e)
            u=self.user()
            return self.fail(u,'Erro interno','Não foi possível carregar esta tela agora. Tente novamente em instantes.',500)

    def do_POST(self):
        path=urlparse(self.path).path
        if path=='/login':
            return self.post_login()
        if path.startswith('/api/v1/driver/'):
            if driver_api_dispatch.handle_driver_api_request(self, path, 'POST'):
                return
        if not self.same_origin_ok():
            u = self.user()
            if u:
                return self.fail(u,'Segurança','Origem da requisição não autorizada.',403)
            return self.send_html(login_page('Origem da requisição não autorizada.'),403)
        u=self.require()
        if not u: return
        if not self.validate_csrf() and not path.startswith('/admin/erp/'):
            return self.fail(u,'Segurança','Falha de segurança na submissão do formulário. Recarregue a página e tente novamente.',403)
        if path=='/force-password':
            return self.post_force_password(u)
        if self.must_change_password(u):
            self.redirect('/force-password')
            return
        if get_setting('maintenance_mode','off') == 'on' and not is_god(u):
            return self.fail(u,'Sistema em manutenção','O sistema está temporariamente em manutenção. Tente novamente em instantes.',503)
        try:
            if orders_dispatch.handle_post(self, path, u):
                return
            if routes_dispatch.handle_post(self, path, u):
                return
            if catalog_dispatch.handle_post(self, path, u):
                return
            if backup_dispatch.handle_post(self, path, u):
                return
            if _ERP_ADMIN_DISPATCH_OK and path.startswith('/admin/erp'):
                if not is_god(u):
                    return self.fail(u, 'Acesso negado', 'Esta área é exclusiva para administradores GOD.', 403)
                _erp_admin_dispatch.handle_post(self, path, u)
                return
            if admin_dispatch.handle_post(self, path, u):
                return
            if path=='/orders/new':
                if not self.require_perm(u,'create_orders','Sem permissão para criar pedidos.'): return
                return self.post_order_new(u)
            if path.startswith('/orders/') and path.endswith('/edit'):
                if not self.require_perm(u,'edit_orders','Sem permissão para editar pedidos.'): return
                return self.post_order_edit(u,self.path_int(path,2))
            if path.startswith('/orders/') and path.endswith('/reopen'):
                if not self.require_perm(u,'cancel_orders','Sem permissão para reabrir/cancelar pedidos.'): return
                return self.post_order_reopen(u,self.path_int(path,2))
            if path.startswith('/orders/') and path.endswith('/status'):
                if not self.require_perm(u,'edit_orders','Sem permissão para alterar status de pedidos.'): return
                return self.post_status(u,self.path_int(path,2))
            if path.startswith('/orders/') and path.endswith('/invoice'):
                if not self.require_perm(u,'invoice_orders','Sem permissão para faturar pedidos.'): return
                return self.post_invoice(u,self.path_int(path,2))
            if path.startswith('/orders/') and path.endswith('/deliver'):
                if not self.require_perm(u,'settle_routes','Sem permissão para concluir entregas.'): return
                return self.post_deliver(u,self.path_int(path,2))
            if path.startswith('/orders/') and path.endswith('/problem'):
                if not self.require_perm(u,'register_delivery_problem','Sem permissão para registrar problema de entrega.'): return
                return self.post_problem(u,self.path_int(path,2))
            if path.startswith('/orders/') and path.endswith('/delete'):
                if not self.require_perm(u,'cancel_orders','Sem permissão para apagar pedidos.'): return
                return self.post_order_delete(u,self.path_int(path,2))
            if path=='/clients':
                if not self.require_perm(u,'manage_clients','Sem permissão para gerenciar clientes.'): return
                return self.post_client(u)
            if path.startswith('/clients/') and path.endswith('/update'):
                if not self.require_perm(u,'manage_clients','Sem permissão para gerenciar clientes.'): return
                return self.post_client_update(u,self.path_int(path,2))
            if path.startswith('/clients/') and path.endswith('/toggle'):
                if not self.require_perm(u,'manage_clients','Sem permissão para gerenciar clientes.'): return
                return self.post_client_toggle(u,self.path_int(path,2))
            if path.startswith('/clients/') and path.endswith('/delete'):
                if not self.require_perm(u,'manage_clients','Sem permissão para apagar clientes.'): return
                return self.post_client_delete(u,self.path_int(path,2))
            if path=='/drivers':
                if not self.require_perm(u,'manage_drivers','Sem permissão para gerenciar motoristas.'): return
                return self.post_driver(u)
            if path.startswith('/drivers/') and path.endswith('/update'):
                if not self.require_perm(u,'manage_drivers','Sem permissão para gerenciar motoristas.'): return
                return self.post_driver_update(u,self.path_int(path,2))
            if path.startswith('/drivers/') and path.endswith('/toggle'):
                if not self.require_perm(u,'manage_drivers','Sem permissão para gerenciar motoristas.'): return
                return self.post_driver_toggle(u,self.path_int(path,2))
            if path.startswith('/drivers/') and path.endswith('/delete'):
                if not self.require_perm(u,'manage_drivers','Sem permissão para apagar motoristas.'): return
                return self.post_driver_delete(u,self.path_int(path,2))
            if path=='/vehicles':
                if not self.require_perm(u,'manage_vehicles','Sem permissão para gerenciar veículos.'): return
                return self.post_vehicle(u)
            if path.startswith('/vehicles/') and path.endswith('/update'):
                if not self.require_perm(u,'manage_vehicles','Sem permissão para gerenciar veículos.'): return
                return self.post_vehicle_update(u,self.path_int(path,2))
            if path.startswith('/vehicles/') and path.endswith('/toggle'):
                if not self.require_perm(u,'manage_vehicles','Sem permissão para gerenciar veículos.'): return
                return self.post_vehicle_toggle(u,self.path_int(path,2))
            if path.startswith('/vehicles/') and path.endswith('/delete'):
                if not self.require_perm(u,'manage_vehicles','Sem permissão para apagar veículos.'): return
                return self.post_vehicle_delete(u,self.path_int(path,2))
            if path=='/route-cities':
                if not self.require_perm(u,'manage_route_catalog','Sem permissão para gerenciar cidades/rotas-base.'): return
                return self.post_route_city(u)
            if path.startswith('/route-cities/') and path.endswith('/update'):
                if not self.require_perm(u,'manage_route_catalog','Sem permissão para gerenciar cidades/rotas-base.'): return
                return self.post_route_city_update(u,self.path_int(path,2))
            if path.startswith('/route-cities/') and path.endswith('/toggle'):
                if not self.require_perm(u,'manage_route_catalog','Sem permissão para gerenciar cidades/rotas-base.'): return
                return self.post_route_city_toggle(u,self.path_int(path,2))
            if path.startswith('/route-cities/') and path.endswith('/delete'):
                if not self.require_perm(u,'manage_route_catalog','Sem permissão para apagar cidades/rotas-base.'): return
                return self.post_route_city_delete(u,self.path_int(path,2))
            if path=='/routes/new':
                if not self.require_perm(u,'create_routes','Sem permissão para criar cargas.'): return
                return self.post_route(u)
            if path.startswith('/routes/') and path.endswith('/reopen'):
                if not self.require_perm(u,'cancel_routes','Sem permissão para reabrir/cancelar cargas.'): return
                return self.post_route_reopen(u,self.path_int(path,2))
            if path.startswith('/routes/') and path.endswith('/cancel'):
                if not self.require_perm(u,'cancel_routes','Sem permissão para reabrir/cancelar cargas.'): return
                return self.post_route_cancel(u,self.path_int(path,2))
            if path.startswith('/routes/') and path.endswith('/dispatch'):
                if not self.require_perm(u,'edit_routes','Sem permissão para editar cargas.'): return
                return self.post_route_dispatch(u,self.path_int(path,2))
            if path.startswith('/routes/') and path.endswith('/update'):
                if not self.require_perm(u,'edit_routes','Sem permissão para editar cargas.'): return
                return self.post_route_update(u,self.path_int(path,2))
            if path.startswith('/routes/') and path.endswith('/finish'):
                if not self.require_perm(u,'settle_routes','Sem permissão para concluir cargas.'): return
                return self.post_route_finish(u,self.path_int(path,2))
            if path.startswith('/routes/') and path.endswith('/sequence'):
                if not self.require_perm(u,'edit_routes','Sem permissão para editar cargas.'): return
                return self.post_route_sequence(u,self.path_int(path,2))
            if path.startswith('/routes/') and path.endswith('/add'):
                if not self.require_perm(u,'edit_routes','Sem permissão para editar cargas.'): return
                return self.post_route_add_order(u,self.path_int(path,2))
            if path.startswith('/routes/') and '/remove/' in path:
                if not self.require_perm(u,'edit_routes','Sem permissão para editar cargas.'): return
                return self.post_route_remove_order(u,self.path_int(path,2),self.path_int(path,4))
            if path.startswith('/routes/') and path.endswith('/delete'):
                if not self.require_perm(u,'cancel_routes','Sem permissão para apagar cargas.'): return
                return self.post_route_delete(u,self.path_int(path,2))
            if path.startswith('/load-settlement/') and path.endswith('/finish'):
                if not self.require_perm(u,'settle_routes','Sem permissão para concluir acerto de carga.'): return
                return self.post_load_settlement_finish(u,self.path_int(path,2))
            if path=='/backup/create':
                if not self.require_perm(u,'create_backup','Sem permissão para gerar backup.'): return
                return self.post_backup(u)
            if path=='/backup/restore':
                if not self.require_perm(u,'restore_backup','Sem permissão para restaurar backup.'): return
                return self.post_backup_restore(u)
            if path=='/settings':
                if not self.require_perm(u,'manage_settings','Sem permissão para alterar configurações.'): return
                return self.post_settings(u)
            if path=='/settings/user':
                if not self.require_perm(u,'manage_users','Sem permissão para gerenciar usuários.'): return
                return self.post_user(u)
            if path.startswith('/settings/user/') and path.endswith('/delete'):
                if not self.require_perm(u,'manage_users','Sem permissão para gerenciar usuários.'): return
                return self.post_user_delete(u,self.path_int(path,3))
            if path.startswith('/settings/user/') and path.endswith('/update'):
                if not self.require_perm(u,'manage_users','Sem permissão para gerenciar usuários.'): return
                return self.post_user_update(u,self.path_int(path,3))
            if path.startswith('/settings/user/') and path.endswith('/reset-password'):
                if not self.require_perm(u,'manage_users','Sem permissão para gerenciar usuários.'): return
                return self.post_user_reset_password(u,self.path_int(path,3))
            if path.startswith('/settings/user/') and path.endswith('/purge'):
                if not self.require_perm(u,'manage_users','Sem permissão para gerenciar usuários.'): return
                return self.post_user_purge(u,self.path_int(path,3))
            if path=='/settings/users/default-passwords':
                if not self.require_perm(u,'manage_users','Sem permissão para gerenciar usuários.'): return
                return self.post_users_default_passwords(u)
            if path=='/settings/permissions/role':
                if not self.require_perm(u,'manage_permissions','Sem permissão para gerenciar permissões.'): return
                return self.post_role_permissions(u)
            if path.startswith('/settings/permissions/user/') and path.endswith('/update'):
                if not self.require_perm(u,'manage_permissions','Sem permissão para gerenciar permissões.'): return
                return self.post_user_permissions(u,self.path_int(path,4))
            if path=='/settings/profile':
                if not self.require_perm(u,'view_settings','Sem permissão para alterar seu perfil.'): return
                return self.post_profile(u)
            if path=='/sla/holiday':
                if not self.require_perm(u,'manage_sla','Sem permissão para gerenciar feriados do SLA.'): return
                return self.post_holiday(u)
            if path.startswith('/sla/holiday/') and path.endswith('/delete'):
                if not self.require_perm(u,'manage_sla','Sem permissão para gerenciar feriados do SLA.'): return
                return self.post_holiday_delete(u,self.path_int(path,3))
            if path=='/sla/recalculate':
                if not self.require_perm(u,'manage_sla','Sem permissão para recalcular SLA.'): return
                return self.post_sla_recalculate(u)
        except sqlite3.IntegrityError as e:
            return self.fail(u,'Erro ao salvar',friendly_db_error_message(e),400)
        except sqlite3.OperationalError as e:
            return self.fail(u,'Banco indisponível',friendly_db_error_message(e),409)
        except ValueError as e:
            return self.fail(u,'Dados inválidos',safe_user_error_message(e, 'Revise os dados informados e tente novamente.'),400)
        except Exception as e:
            log_server_error(f'POST {path}', e)
            return self.fail(u,'Erro interno','Não foi possível concluir esta ação agora. Tente novamente em instantes.',500)
        finally:
            import sys
            if sys.exc_info()[0] is None and getattr(self, '_last_status_code', 200) in (200, 302):
                if path.startswith('/orders/'):
                    GLOBAL_BROKER.publish('orders_updated')
                elif path.startswith('/routes/') or path.startswith('/load-settlement/'):
                    GLOBAL_BROKER.publish('routes_updated')
                elif path.startswith('/clients/') or path.startswith('/drivers/') or path.startswith('/vehicles/') or path.startswith('/route-cities/'):
                    GLOBAL_BROKER.publish('master_data_updated')
                elif path.startswith('/sla/'):
                    GLOBAL_BROKER.publish('sla_updated')
        return self.fail(u,'Não encontrado','Ação não encontrada.',404)

    def post_login(self):
        d=self.post_data(); username=(d.get('username','') or '').strip(); pwd=(d.get('password','') or '')
        ip = self.client_ip()
        if not username or not pwd:
            return self.send_html(login_page('Usuário e senha são obrigatórios.'),400)
        wait_seconds = login_lock_remaining(ip, username)
        if wait_seconds > 0:
            return self.send_html(login_page(f'Acesso temporariamente bloqueado. Aguarde {wait_seconds}s e tente novamente.'),429)
        with conn() as db:
            user = find_active_user_by_username(db, username)
            ok=False; rehash=False
            if user:
                ok, rehash = verify_password(pwd, user['password_hash'])
            if ok and rehash:
                update_user_password_hash(db, int(user['id']), hash_password(pwd))
                db.commit()
            if ok:
                update_user_last_login(db, int(user['id']), now())
                db.commit()
        if not ok:
            register_login_failure(ip, username)
            return self.send_html(login_page('Usuário ou senha inválidos.'),401)
        clear_login_failures(ip, username)
        sid=secrets.token_urlsafe(32)
        csrf_token=secrets.token_urlsafe(32)
        with SESSIONS_LOCK:
            SESSIONS[sid] = {'uid': user['id'], 'exp': time.time() + SESSION_MAX_AGE_SECONDS, 'csrf': csrf_token}
        with conn() as db:
            audit(db,user,'Login','Acesso',user['username'],'','',f'IP: {ip}', source_ip=ip)
            db.commit()
        use_secure = SECURE_COOKIE_FLAG and self.is_https()
        secure_cookie = '; Secure' if use_secure else ''
        redirect_target = '/force-password' if self.must_change_password(user) else '/dashboard'
        self.send_response(302)
        self._common_headers()
        self.send_header('Location',redirect_target)
        self.send_header('Set-Cookie',f'sid={sid}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_MAX_AGE_SECONDS}{secure_cookie}')
        self.send_header('Set-Cookie',f'csrf_token={csrf_token}; Path=/; SameSite=Lax; Max-Age={SESSION_MAX_AGE_SECONDS}{secure_cookie}')
        self.end_headers()

    def post_force_password(self, u):
        d=self.post_data()
        new_password=(d.get('new_password') or '').strip()
        confirm_password=(d.get('confirm_password') or '').strip()
        if not new_password or not confirm_password:
            return self.force_password_page(u,'Informe e confirme a nova senha.')
        if new_password != confirm_password:
            return self.force_password_page(u,'As duas senhas não conferem.')
        validate_password_strength(new_password)
        with conn() as db:
            user_row=db.execute('SELECT id,name,username,password_hash,active,must_change_password FROM users WHERE id=?',(u['id'],)).fetchone()
            if not user_row or int(user_row['active'] or 0) != 1:
                return self.fail(u,'Acesso negado','Usuário inativo ou não encontrado para troca de senha.',403)
            same_pwd,_=verify_password(new_password,user_row['password_hash'])
            if same_pwd:
                return self.force_password_page(u,'A nova senha precisa ser diferente da senha atual.')
            db.execute('UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?',(hash_password(new_password),u['id']))
            audit(db,u,'Trocou senha obrigatória','Configurações',user_row['username'])
            db.commit()
        self.redirect('/dashboard')

    def maybe_run_stats_maintenance(self, db):
        global LAST_STATS_MAINTENANCE_AT
        now_ts = time.time()
        if (now_ts - LAST_STATS_MAINTENANCE_AT) < STATS_MAINTENANCE_INTERVAL_SECONDS:
            return False
        with STATS_MAINTENANCE_LOCK:
            now_ts = time.time()
            if (now_ts - LAST_STATS_MAINTENANCE_AT) < STATS_MAINTENANCE_INTERVAL_SECONDS:
                return False
            # Mantém contadores consistentes quando há legado com pedidos em rota sem vínculo.
            self.recalc_all_routes(db)
            self.ensure_in_route_loads(db)
            self.recalc_all_routes(db)
            LAST_STATS_MAINTENANCE_AT = now_ts
            return True

    def stats(self):
        with conn() as db:
            if self.maybe_run_stats_maintenance(db):
                db.commit()
            by={r['status']:r['c'] for r in db.execute('SELECT status,COUNT(*) c FROM orders GROUP BY status')}
            total=db.execute('SELECT COUNT(*) c, COALESCE(SUM(weight_kg),0) w, COALESCE(SUM(total_value),0) v FROM orders').fetchone()
            pending=db.execute("SELECT COUNT(*) c,COALESCE(SUM(weight_kg),0) w FROM orders WHERE status NOT IN ('Acertado','Problema','Cancelado')").fetchone()
            sales_open=db.execute("SELECT COUNT(*) c FROM orders WHERE status='Venda'").fetchone()['c']
            invoiced_waiting=db.execute("SELECT COUNT(*) c FROM orders WHERE status='Faturado'").fetchone()['c']
            out_for_delivery=db.execute("SELECT COUNT(*) c FROM orders WHERE status='Saiu para entrega'").fetchone()['c']
            pending_settlement_routes=db.execute("SELECT COUNT(*) c FROM routes WHERE status='Em rota'").fetchone()['c']
            settled_routes=db.execute("SELECT COUNT(*) c FROM routes WHERE status IN ('Acertada','Com problema')").fetchone()['c']
            delivered=db.execute("SELECT COUNT(*) c FROM orders WHERE status='Acertado'").fetchone()['c']
            problems=db.execute("SELECT COUNT(*) c FROM orders WHERE status='Problema'").fetchone()['c']
            late=db.execute("SELECT COUNT(*) c FROM orders WHERE expected_delivery_date<? AND status NOT IN ('Acertado','Problema','Cancelado','Agendado')",(today(),)).fetchone()['c']
            near=db.execute("SELECT COUNT(*) c FROM orders WHERE expected_delivery_date BETWEEN ? AND ? AND status NOT IN ('Acertado','Problema','Cancelado','Agendado')",(today(),date_add(SLA_RISK_DAYS))).fetchone()['c']
        return {'by':by,'total':total,'pending':pending,'sales_open':sales_open,'invoiced_waiting':invoiced_waiting,'out_for_delivery':out_for_delivery,'pending_settlement_routes':pending_settlement_routes,'settled_routes':settled_routes,'delivered':delivered,'late':late,'near':near,'problems':problems}

    def latest_backup_file_info(self):
        try:
            files = [x for x in os.listdir(BACKUP_DIR) if x.endswith('.sqlite3')]
        except Exception:
            files = []
        if not files:
            return {'name': '', 'mtime': ''}
        files.sort(key=lambda f: os.path.getmtime(os.path.join(BACKUP_DIR, f)), reverse=True)
        latest = files[0]
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(os.path.join(BACKUP_DIR, latest))).strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            mtime = ''
        return {'name': latest, 'mtime': mtime}

    def system_health_snapshot(self):
        health = {
            'db_ok': False,
            'db_error': '',
            'db_size': 0,
            'disk_total': 0,
            'disk_free': 0,
            'latency_samples': 0,
            'latency_avg_ms': 0.0,
            'latency_p95_ms': 0.0,
            'last_backup_at': '',
            'last_backup_file': '',
            'last_backup_verify_at': '',
            'last_backup_verify_ok': None,
        }
        try:
            with conn() as db:
                db.execute('SELECT 1').fetchone()
            health['db_ok'] = True
        except Exception as e:
            health['db_error'] = safe_user_error_message(e, 'Falha de leitura no banco de dados.')
        try:
            if DB_BACKEND == 'sqlite' and DB_PATH and os.path.isfile(DB_PATH):
                health['db_size'] = int(os.path.getsize(DB_PATH))
        except Exception:
            pass
        try:
            disk = shutil.disk_usage(BASE_DIR)
            health['disk_total'] = int(disk.total)
            health['disk_free'] = int(disk.free)
        except Exception:
            pass
        lat = request_latency_snapshot()
        health['latency_samples'] = lat['samples']
        health['latency_avg_ms'] = lat['avg_ms']
        health['latency_p95_ms'] = lat['p95_ms']
        auto = load_automation_status()
        latest = self.latest_backup_file_info()
        health['last_backup_at'] = str(auto.get('last_auto_backup_at') or latest['mtime'] or '')
        health['last_backup_file'] = str(auto.get('last_auto_backup_file') or latest['name'] or '')
        health['last_backup_verify_at'] = str(auto.get('last_auto_verify_at') or '')
        verify_ok = auto.get('last_auto_verify_ok')
        if verify_ok in (True, False):
            health['last_backup_verify_ok'] = bool(verify_ok)
        return health

    def health_panel_html(self):
        h = self.system_health_snapshot()
        db_state = "<span class='badge st-acertado'>OK</span>" if h['db_ok'] else "<span class='badge st-problema'>Falha</span>"
        verify_state = 'Sem validação ainda'
        if h['last_backup_verify_ok'] is True:
            verify_state = 'OK'
        elif h['last_backup_verify_ok'] is False:
            verify_state = 'Falhou'
        return f"""<section class='panel'>
            <h2>Saúde do Sistema</h2>
            <div class='info-grid'>
                <p><b>Banco de dados</b>{db_state}</p>
                <p><b>Tamanho do banco</b>{esc(human_bytes(h['db_size']))}</p>
                <p><b>Disco livre</b>{esc(human_bytes(h['disk_free']))} / {esc(human_bytes(h['disk_total']))}</p>
                <p><b>Latência média</b>{h['latency_avg_ms']:.1f} ms ({h['latency_samples']} req)</p>
                <p><b>Latência p95</b>{h['latency_p95_ms']:.1f} ms</p>
                <p><b>Último backup</b>{esc(h['last_backup_at'] or 'Não identificado')}</p>
                <p><b>Arquivo backup</b>{esc(h['last_backup_file'] or 'Nenhum')}</p>
                <p><b>Validação semanal backup</b>{esc(verify_state)} {f'({esc(h["last_backup_verify_at"])})' if h['last_backup_verify_at'] else ''}</p>
            </div>
            {f"<div class='alert danger'>{esc(h['db_error'])}</div>" if h['db_error'] else "<div class='alert info'>Monitoramento automático: inicialização, backup diário e validação semanal configuráveis em tools/install_windows_tasks.ps1.</div>"}
        </section>"""

    def order_route_info(self, db, oid):
        return db.execute("""SELECT ro.id route_order_id, ro.route_id, ro.status route_order_status, r.name route_name, r.status route_status
                             FROM route_orders ro JOIN routes r ON r.id=ro.route_id
                             WHERE ro.order_id=? ORDER BY ro.id DESC LIMIT 1""",(oid,)).fetchone()

    def route_status_info(self, db, rid):
        return db.execute('SELECT id,name,status FROM routes WHERE id=?',(rid,)).fetchone()

    def ensure_order_status(self, db, oid, new_status, notes='', allow_reopen=False):
        row = db.execute('SELECT id,status,order_number FROM orders WHERE id=?',(oid,)).fetchone()
        if not row:
            raise ValueError('Pedido não encontrado para mudança de status.')
        raw = str(new_status or '').strip()
        normalized = ORDER_STATUS_ALIASES.get(raw, raw)
        if raw and normalized not in STATUSES:
            raise ValueError('Status inválido para o pedido.')
        st = normalize_order_status(raw or 'Venda')
        old = normalize_order_status(row['status'])
        if st != old:
            allowed = ORDER_ALLOWED_TRANSITIONS.get(old, set())
            if st not in allowed:
                can_reopen_final = allow_reopen and old in FINAL_ORDER_STATUSES and st in ('Venda','Faturado','Saiu para entrega')
                if not can_reopen_final:
                    raise ValueError(f'Transição inválida de {old} para {st}.')
        if st == 'Saiu para entrega':
            route_info = self.order_route_info(db, oid)
            if not route_info or normalize_route_status(route_info['route_status']) != 'Em rota':
                raise ValueError('Pedido só pode ficar em "Saiu para entrega" quando estiver em uma carga com status "Em rota".')
        if st == 'Problema' and not str(notes or '').strip():
            raise ValueError('Informe uma observação para registrar o problema.')
        return st, old

    def ensure_route_status(self, db, rid, new_status, allow_reopen=False):
        route = self.route_status_info(db, rid)
        if not route:
            raise ValueError('Carga não encontrada para mudança de status.')
        old = normalize_route_status(route['status'])
        st = normalize_route_status(new_status)
        if st != old:
            allowed = ROUTE_ALLOWED_TRANSITIONS.get(old, set())
            if st not in allowed:
                can_reopen_final = allow_reopen and old in FINAL_ROUTE_STATUSES and st in ('Planejada','Em rota')
                if not can_reopen_final:
                    raise ValueError(f'Transição inválida de carga: {old} para {st}.')
        return st, old

    def ensure_unique_invoice(self, db, invoice_number, exclude_order_id=None):
        nf = (invoice_number or '').strip()
        if not nf:
            return
        if exclude_order_id:
            dup = db.execute('SELECT id,order_number FROM orders WHERE invoice_number=? AND id<>? LIMIT 1', (nf, exclude_order_id)).fetchone()
        else:
            dup = db.execute('SELECT id,order_number FROM orders WHERE invoice_number=? LIMIT 1', (nf,)).fetchone()
        if dup:
            raise ValueError(f'NF duplicada: a nota {nf} já está vinculada ao pedido {dup["order_number"]}.')

    def report_filter_values(self, qs=None):
        qs = qs or parse_qs(urlparse(self.path).query)
        default_start = today()[:8] + '01'
        default_end = today()
        try:
            start = validate_date_field(qs.get('start',[default_start])[0], 'a data inicial', required=True)
        except Exception:
            start = default_start
        try:
            end = validate_date_field(qs.get('end',[default_end])[0], 'a data final', required=True)
        except Exception:
            end = default_end
        route_filter = (qs.get('route',[''])[0] or '').strip()
        status_filter = (qs.get('status',[''])[0] or '').strip()
        return start, end, route_filter, status_filter

    def report_where(self, start, end, route_filter='', status_filter='', alias='o'):
        where = [f'{alias}.sale_date BETWEEN ? AND ?']
        params = [start, end]
        if route_filter:
            where.append(f'{alias}.route_name=?'); params.append(route_filter)
        if status_filter:
            where.append(f'{alias}.status=?'); params.append(status_filter)
        return ' WHERE ' + ' AND '.join(where), params

    def report_orders_rows(self, db, start, end, route_filter='', status_filter='', limit=80):
        where_sql, params = self.report_where(start, end, route_filter, status_filter, alias='o')
        limit_sql = f' LIMIT {int(limit)}' if limit else ''
        return db.execute(f"""SELECT
                                o.id,o.order_number,o.invoice_number,o.seller_name,o.payment_method,o.status,o.route_name,o.city,o.weight_kg,o.total_value,o.sale_date,o.delivered_at,o.expected_delivery_date,
                                c.name client,COALESCE(d.name, od.name, 'Sem motorista') driver,
                                CASE
                                  WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL
                                   AND NULLIF(o.sale_date,'') IS NOT NULL
                                  THEN CAST(julianday(substr(o.delivered_at,1,10)) - julianday(o.sale_date) AS INTEGER)
                                  ELSE NULL
                                END days_to_deliver
                               FROM orders o
                               LEFT JOIN clients c ON c.id=o.client_id
                               LEFT JOIN route_orders ro ON ro.order_id=o.id
                               LEFT JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada'
                               LEFT JOIN drivers d ON d.id=r.driver_id
                               LEFT JOIN drivers od ON od.id=o.driver_id
                               {where_sql}
                               ORDER BY o.id DESC{limit_sql}""",params).fetchall()

    def fetch_orders_for_table(self, db, where_sql='', params=(), limit=25):
        sql = """SELECT o.*,c.name client,c.farm_name,c.city client_city,
                        (SELECT r.name
                         FROM route_orders ro
                         JOIN routes r ON r.id=ro.route_id
                         WHERE ro.order_id=o.id
                         ORDER BY CASE WHEN r.status IN ('Planejada','Em rota') THEN 0 ELSE 1 END, ro.id DESC
                         LIMIT 1) load_name
                 FROM orders o
                 LEFT JOIN clients c ON c.id=o.client_id
                 WHERE 1=1"""
        sql += f' AND ({where_sql})' if where_sql else ''
        sql += " ORDER BY CASE WHEN o.expected_delivery_date<? AND o.status NOT IN ('Acertado','Problema','Cancelado','Agendado') THEN 0 ELSE 1 END, o.expected_delivery_date ASC, o.id DESC"
        query_params = list(params) + [today()]
        if limit:
            sql += f' LIMIT {int(limit)}'
        return db.execute(sql, query_params).fetchall()

    def master_data_quality(self, db):
        clients = db.execute('SELECT id,name,city,route_name,phone,whatsapp,farm_name FROM clients WHERE active=1 ORDER BY name').fetchall()
        drivers = db.execute('SELECT id,name,phone FROM drivers WHERE active=1 ORDER BY name').fetchall()
        vehicles = db.execute('SELECT id,name,capacity,capacity_kg FROM vehicles WHERE active=1 ORDER BY name').fetchall()
        invalid_phone_clients = []
        duplicate_map = {}
        for c in clients:
            digits = re.sub(r'\D', '', str(c['phone'] or c['whatsapp'] or ''))
            if digits and len(digits) not in (10, 11):
                invalid_phone_clients.append(c)
            key = normalized_text_key(c['name'])
            if key:
                duplicate_map.setdefault(key, []).append(c)
        duplicate_clients = [grp for grp in duplicate_map.values() if len(grp) > 1]
        invalid_phone_drivers = [d for d in drivers if re.sub(r'\D', '', str(d['phone'] or '')) and len(re.sub(r'\D', '', str(d['phone'] or ''))) not in (10, 11)]
        invalid_capacity_vehicles = [v for v in vehicles if parse_float((v['capacity_kg'] if v['capacity_kg'] is not None else v['capacity']) or 0) <= 0]
        open_incomplete_orders = db.execute("""SELECT id,order_number,status,city,route_name,payment_method,weight_kg,total_value
                                               FROM orders
                                               WHERE status NOT IN ('Acertado','Problema','Cancelado')
                                                 AND (
                                                      city IS NULL OR TRIM(city)=''
                                                      OR route_name IS NULL OR TRIM(route_name)=''
                                                      OR payment_method IS NULL OR TRIM(payment_method)=''
                                                      OR COALESCE(weight_kg,0)<=0
                                                     )
                                               ORDER BY id DESC LIMIT 30""").fetchall()
        return {
            'clients_missing_city': sum(1 for c in clients if not str(c['city'] or '').strip()),
            'clients_missing_route': sum(1 for c in clients if not str(c['route_name'] or '').strip()),
            'clients_invalid_phone': len(invalid_phone_clients),
            'drivers_invalid_phone': len(invalid_phone_drivers),
            'vehicles_invalid_capacity': len(invalid_capacity_vehicles),
            'duplicate_client_groups': duplicate_clients,
            'open_incomplete_orders': open_incomplete_orders,
        }

    def pendencias(self, u):
        with conn() as db:
            sem_nf = self.fetch_orders_for_table(db, "o.status='Venda' AND (o.invoice_number IS NULL OR TRIM(o.invoice_number)='')", limit=20)
            sem_carga = self.fetch_orders_for_table(db, """o.status='Faturado'
                AND NOT EXISTS (
                    SELECT 1
                    FROM route_orders ro
                    JOIN routes ar ON ar.id=ro.route_id
                    WHERE ro.order_id=o.id AND ar.status IN ('Planejada','Em rota')
                )""", limit=20)
            sem_acerto = self.fetch_orders_for_table(db, "o.status='Saiu para entrega'", limit=20)
            sla_critico = self.fetch_orders_for_table(db, "o.status NOT IN ('Acertado','Problema','Cancelado','Agendado') AND o.expected_delivery_date<=?", params=(date_add(SLA_RISK_DAYS),), limit=20)
            quality = self.master_data_quality(db)
        cards = f"""<div class='cards'>
            <div class='card danger'><small>Sem Faturar</small><strong>{len(sem_nf)}</strong><span>Pedidos em venda sem faturamento</span></div>
            <div class='card warn'><small>Sem carga</small><strong>{len(sem_carga)}</strong><span>Pedidos faturados sem roteirização</span></div>
            <div class='card warn'><small>Sem acerto</small><strong>{len(sem_acerto)}</strong><span>Pedidos em rota aguardando baixa</span></div>
            <div class='card danger'><small>SLA crítico</small><strong>{len(sla_critico)}</strong><span>Vencidos ou vencendo em até {SLA_RISK_DAYS} dias</span></div>
            <div class='card total'><small>Cadastros com risco</small><strong>{quality['clients_missing_city'] + quality['clients_missing_route'] + quality['clients_invalid_phone'] + quality['drivers_invalid_phone'] + quality['vehicles_invalid_capacity']}</strong><span>Qualidade de dados mestre</span></div>
        </div>"""
        dup_rows = ''.join(
            f"<li><b>{esc(g[0]['name'])}</b> ({len(g)} cadastros parecidos) · IDs: {', '.join(str(x['id']) for x in g[:6])}</li>"
            for g in quality['duplicate_client_groups'][:10]
        ) or '<li>Nenhuma duplicidade evidente por nome.</li>'
        incomplete_rows = ''.join(
            f"<tr><td><a class='btn small ghost' href='/orders/{r['id']}'>{esc(r['order_number'])}</a></td><td>{badge(r['status'])}</td><td>{esc(r['city'] or 'Sem cidade')}</td><td>{esc(r['route_name'] or 'Sem rota')}</td><td>{esc(r['payment_method'] or 'Sem pagamento')}</td><td>{fmt_num(r['weight_kg'],2)} kg</td></tr>"
            for r in quality['open_incomplete_orders']
        ) or "<tr><td colspan='6'>Nenhum pedido aberto com dados críticos faltando.</td></tr>"
        quality_panel = f"""<section class='panel'>
            <h2>Qualidade de Dados (pré-uso)</h2>
            <div class='info-grid'>
                <p><b>Clientes sem cidade</b>{quality['clients_missing_city']}</p>
                <p><b>Clientes sem rota</b>{quality['clients_missing_route']}</p>
                <p><b>Clientes com telefone inválido</b>{quality['clients_invalid_phone']}</p>
                <p><b>Motoristas com telefone inválido</b>{quality['drivers_invalid_phone']}</p>
                <p><b>Veículos com capacidade inválida</b>{quality['vehicles_invalid_capacity']}</p>
            </div>
            <h3>Possíveis clientes duplicados</h3>
            <ul>{dup_rows}</ul>
            <h3>Pedidos abertos com cadastro incompleto</h3>
            <div class='table-wrap'><table><thead><tr><th>Pedido</th><th>Status</th><th>Cidade</th><th>Rota</th><th>Pagamento</th><th>Peso</th></tr></thead><tbody>{incomplete_rows}</tbody></table></div>
        </section>"""
        assisted_links = []
        assisted_links.append("<a class='btn' href='/faturamento'>Ir para Faturamento</a>" if self.has_perm(u,'invoice_orders') else "<span class='muted'>Sem permissão para faturamento</span>")
        assisted_links.append("<a class='btn ghost' href='/routes/new'>Montar Carga</a>" if self.has_perm(u,'create_routes') else "<span class='muted'>Sem permissão para montar carga</span>")
        assisted_links.append("<a class='btn ghost' href='/load-settlement'>Baixar Acerto</a>" if self.has_perm(u,'settle_routes') else "<span class='muted'>Sem permissão para acerto</span>")
        assisted = f"""<section class='panel assisted-panel'><h2>Modo Assistido (1-2-3)</h2><div class='assisted-steps'><span>1. Fature o que está em venda</span><span>2. Coloque faturados em carga</span><span>3. Faça acerto das cargas em rota</span></div><div class='action-strip'>{''.join(assisted_links)}</div></section>"""
        content = cards + assisted + f"<section class='panel'><h2>Pedidos Sem Faturar</h2>{self.table_orders(sem_nf,u=u)}</section><section class='panel'><h2>Faturados sem carga</h2>{self.table_orders(sem_carga,u=u)}</section><section class='panel'><h2>Em rota sem acerto</h2>{self.table_orders(sem_acerto,u=u)}</section><section class='panel'><h2>SLA crítico</h2>{self.table_orders(sla_critico,u=u)}</section>" + quality_panel
        return self.send_html(layout(u,'Pendências Operacionais',content,'Central única para atacar o que trava operação e gera retrabalho'))

    def dashboard(self,u):
        s=self.stats(); by=s['by']; total=s['total']; pending=s['pending']

        # --- Central de Alertas ---
        with conn() as db:
            n_no_nf  = db.execute("SELECT COUNT(*) c FROM orders WHERE status='Venda' AND (invoice_number IS NULL OR TRIM(invoice_number)='')").fetchone()['c']
            n_no_load= db.execute("""SELECT COUNT(*) c FROM orders WHERE status='Faturado'
                AND NOT EXISTS (SELECT 1 FROM route_orders ro JOIN routes r ON r.id=ro.route_id
                WHERE ro.order_id=orders.id AND r.status IN ('Planejada','Em rota'))""").fetchone()['c']
            flow_rows=db.execute("SELECT status,COUNT(*) c FROM orders GROUP BY status").fetchall()

        def alert_chip_html(cls, icon, count, label, href):
            chip_cls = 'critical' if cls=='danger' else ('risk' if cls=='warn' else ('ok' if cls=='done' else 'pending'))
            return f'<a class="alert-chip {chip_cls}" href="{href}"><span class="alert-chip-icon">{icon}</span><span class="alert-chip-body"><b>{count}</b><small>{esc(label)}</small></span></a>'

        alert_central = '<div class="alert-central">'
        alert_central += alert_chip_html('danger','🔴',s['late'],'Fora do SLA','/orders?late=1')
        alert_central += alert_chip_html('warn','🟡',s['near'],'Risco de prazo','/orders?near=1')
        alert_central += alert_chip_html('warn','📋',n_no_nf,'Sem faturar','/orders?status=Venda')
        alert_central += alert_chip_html('warn','📦',n_no_load,'Faturados sem carga','/orders?status=Faturado')
        alert_central += alert_chip_html('warn','🚚',s['out_for_delivery'],'Em rota','/orders?status=Saiu+para+entrega')
        alert_central += alert_chip_html('done','✅',s['delivered'],'Acertados','/orders?status=Acertado')
        alert_central += '</div>'

        cards=[
            ('Total de pedidos',total['c'],'/orders','total'),
            ('Peso pendente',fmt_num(pending['w'])+' kg','/orders','route'),
            ('Cargas pendentes de acerto',s['pending_settlement_routes'],'/routes','warn'),
            ('Cargas acertadas',s['settled_routes'],'/routes','done'),
            ('Pedidos com problema',s['problems'],'/orders?status=Problema','danger'),
        ]
        html_cards='<div class="cards">'+''.join(f'<a class="card {cls}" href="{href}"><small>{esc(label)}</small><strong>{esc(str(value))}</strong></a>' for label,value,href,cls in cards)+'</div>'
        shortcut_links = []
        shortcut_links.append('<a class="btn" href="/orders/new">Novo pedido</a>' if self.has_perm(u,'create_orders') else '<span class="muted">Sem permissão para novo pedido</span>')
        shortcut_links.append('<a class="btn ghost" href="/faturamento">Faturar pedidos</a>' if self.has_perm(u,'invoice_orders') else '<span class="muted">Sem permissão para faturamento</span>')
        shortcut_links.append('<a class="btn ghost" href="/routes/new">Montar nova carga</a>' if self.has_perm(u,'create_routes') else '<span class="muted">Sem permissão para montar carga</span>')
        shortcut_links.append('<a class="btn ghost" href="/load-settlement">Baixar acerto de carga</a>' if self.has_perm(u,'settle_routes') else '<span class="muted">Sem permissão para acerto</span>')
        shortcut_links.append('<a class="btn ghost" href="/sla">Analisar SLA</a>' if self.has_perm(u,'view_sla') else '<span class="muted">Sem permissão para SLA</span>')
        shortcuts=f"""<section class="panel"><h2>Atalhos de operação</h2><div class="action-strip">{''.join(shortcut_links)}</div></section>"""
        bars=''.join(f'<div class="flow-row"><span>{badge(r["status"])}</span><b>{r["c"]}</b></div>' for r in flow_rows)
        health_panel = self.health_panel_html() if is_admin(u) else ''
        content=alert_central+html_cards+shortcuts+health_panel+f'<div class="dash-grid"><section class="panel"><h2>Distribuição por etapa</h2><div class="flow-list">{bars}</div><div class="alert info">Os números do painel seguem as mesmas regras das telas internas: faturamento, cargas, acerto e SLA.</div></section></div>'
        return self.send_html(layout(u,'Dashboard Operacional',content,'Visão de comando da operação logística'))


    def query_orders(self):
        qs=parse_qs(urlparse(self.path).query)
        history_mode = str(qs.get('history',[''])[0]).strip().lower() in ('1','true','sim','yes')
        status_filter = (qs.get('status',[''])[0] or '').strip()
        sql="""SELECT o.*,c.name client,c.farm_name,c.city client_city,u.name seller,
                      COALESCE(
                        (SELECT dr.name
                         FROM route_orders ro
                         JOIN routes r ON r.id=ro.route_id
                         JOIN drivers dr ON dr.id=r.driver_id
                         WHERE ro.order_id=o.id AND r.status <> 'Cancelada'
                         ORDER BY CASE WHEN r.status IN ('Planejada','Em rota') THEN 0 ELSE 1 END, ro.id DESC
                         LIMIT 1),
                        od.name
                      ) driver,
                      (SELECT r.name
                       FROM route_orders ro
                       JOIN routes r ON r.id=ro.route_id
                       WHERE ro.order_id=o.id
                       ORDER BY CASE WHEN r.status IN ('Planejada','Em rota') THEN 0 ELSE 1 END, ro.id DESC
                       LIMIT 1) load_name
               FROM orders o
               LEFT JOIN clients c ON c.id=o.client_id
               LEFT JOIN users u ON u.id=o.seller_id
               LEFT JOIN drivers od ON od.id=o.driver_id
               WHERE 1=1"""; p=[]
        if status_filter:
            sql += ' AND o.status=?'
            p.append(status_filter)
        elif history_mode:
            sql += " AND o.status IN ('Acertado','Problema','Cancelado')"
        else:
            sql += " AND o.status NOT IN ('Acertado','Problema','Cancelado')"
        for key,col in [('route','o.route_name'),('city','o.city'),('seller','o.seller_name')]:
            v=qs.get(key,[''])[0]
            if v: sql += f' AND {col}=?'; p.append(v)
        client_id=qs.get('client_id',[''])[0]
        if client_id and client_id.isdigit():
            sql += ' AND o.client_id=?'; p.append(int(client_id))
        nf=qs.get('nf',[''])[0].strip()
        if nf:
            sql += ' AND o.invoice_number LIKE ?'; p.append(f'%{nf}%')
        deadline_from=qs.get('deadline_from',[''])[0]
        deadline_to=qs.get('deadline_to',[''])[0]
        if deadline_from:
            sql += ' AND o.expected_delivery_date>=?'; p.append(deadline_from)
        if deadline_to:
            sql += ' AND o.expected_delivery_date<=?'; p.append(deadline_to)
        if qs.get('late',[''])[0]: sql += " AND o.expected_delivery_date<? AND o.status NOT IN ('Acertado','Problema','Cancelado','Agendado')"; p.append(today())
        if qs.get('near',[''])[0]: sql += " AND o.expected_delivery_date BETWEEN ? AND ? AND o.status NOT IN ('Acertado','Problema','Cancelado','Agendado')"; p += [today(),date_add(SLA_RISK_DAYS)]
        q=qs.get('q',[''])[0].strip()
        if q:
            like=f'%{q}%'; sql += ' AND (o.order_number LIKE ? OR o.invoice_number LIKE ? OR c.name LIKE ? OR c.farm_name LIKE ? OR o.city LIKE ? OR o.route_name LIKE ? OR o.seller_name LIKE ? OR o.notes LIKE ?)'; p += [like]*8
        if history_mode:
            # No histórico, o usuário precisa ver primeiro o que acabou de ser
            # finalizado; ordenar por SLA escondia registros recentes na paginação.
            sql += " ORDER BY COALESCE(o.delivered_at,o.updated_at,o.created_at) DESC,o.id DESC"
        else:
            sql += " ORDER BY CASE WHEN o.expected_delivery_date<? AND o.status NOT IN ('Acertado','Problema','Cancelado','Agendado') THEN 0 ELSE 1 END, o.expected_delivery_date ASC, o.id DESC"
            p.append(today())
        with conn() as db: return db.execute(sql,p).fetchall()

    def order_next_action(self, r, u=None):
        st=normalize_order_status(r['status'])
        if st == 'Agendado':
            keys = r.keys()
            has_nf = str(r['invoice_number'] or '').strip() != '' if 'invoice_number' in keys else False
            if not has_nf:
                st = 'Venda'
            else:
                st = 'Faturado'
                
        if st == 'Venda':
            if u and self.has_perm(u,'invoice_orders'):
                can_charge = "1" if self.has_perm(u,'create_routes') else "0"
                keys = r.keys()
                client_name = r['client'] if 'client' in keys else (r['client_name'] if 'client_name' in keys else '')
                city_name = r['city'] if 'city' in keys else (r['client_city'] if 'client_city' in keys else '')
                weight_val = r['weight_kg'] if 'weight_kg' in keys else 0
                seller_name = r['seller_name'] if 'seller_name' in keys else ''
                deadline_val = r['expected_delivery_date'] if 'expected_delivery_date' in keys else ''
                return f'<button type="button" class="btn small btn-action-invoice" data-order-id="{r["id"]}" data-order-number="{esc(r["order_number"])}" data-client="{esc(client_name)}" data-city="{esc(city_name)}" data-weight="{fmt_num(weight_val)}" data-seller="{esc(seller_name)}" data-deadline="{deadline_val}" data-can-charge="{can_charge}">Faturar</button>'
            return '<span class="muted">Sem permissão para faturar</span>'
        if st == 'Faturado':
            if u and self.has_perm(u,'create_routes'):
                return '<a class="btn small" href="/routes/new">Adicionar em carga</a>'
            return '<span class="muted">Sem permissão para carga</span>'
        if st == 'Saiu para entrega':
            if u and self.has_perm(u,'settle_routes'):
                return '<a class="btn small" href="/load-settlement">Fazer acerto</a>'
            return '<span class="muted">Sem permissão para acerto</span>'
        if st == 'Problema':
            return f'<a class="btn small ghost" href="/orders/{r["id"]}">Tratar problema</a>'
        return '<span class="muted">Sem ação pendente</span>'

    def order_assisted_block(self, row, route_info=None, u=None):
        st = normalize_order_status(row['status'])
        step = '4/4 Finalizado'
        action = '<span class="muted">Fluxo concluído para este pedido.</span>'
        hint = 'Pedido encerrado. Se houver divergência, use reabertura controlada.'
        if st == 'Venda':
            step = '1/4 Cadastro de venda'
            if u and self.has_perm(u,'invoice_orders'):
                action = "<a class='btn' href='/faturamento'>Ir para Faturamento</a>"
            else:
                action = "<span class='muted'>Aguardando faturamento por usuário autorizado.</span>"
            hint = 'Depois de faturar, o pedido entra na fila de montagem de carga.'
        elif st == 'Faturado':
            step = '2/4 Faturado'
            if u and self.has_perm(u,'create_routes'):
                action = "<a class='btn' href='/routes'>Ir para Cargas/Rotas</a>"
            else:
                action = "<span class='muted'>Aguardando planejamento de carga por usuário autorizado.</span>"
            hint = 'Monte a carga e defina sequência de entrega.'
        elif st == 'Saiu para entrega':
            step = '3/4 Em rota'
            route_name = str(route_info['route_name'] or '') if route_info else ''
            if u and self.has_perm(u,'settle_routes'):
                action = f"<a class='btn' href='/load-settlement?q={quote(route_name)}'>Ir para Acerto de Carga</a>" if route_name else "<a class='btn' href='/load-settlement'>Ir para Acerto de Carga</a>"
            else:
                action = "<span class='muted'>Aguardando acerto por usuário autorizado.</span>"
            hint = 'Quando a carga retornar, conclua checklist e baixa por pedido.'
        elif st == 'Agendado':
            keys = row.keys()
            has_nf = str(row['invoice_number'] or '').strip() != '' if 'invoice_number' in keys else False
            if not has_nf:
                step = 'Agendado / Aguardando faturamento'
                if u and self.has_perm(u,'invoice_orders'):
                    action = "<a class='btn' href='/faturamento'>Ir para Faturamento</a>"
                else:
                    action = "<span class='muted'>Aguardando faturamento por usuário autorizado.</span>"
                hint = 'Pedido agendado. Realize o faturamento um dia antes da data programada de entrega.'
            else:
                step = 'Agendado / Faturado'
                if u and self.has_perm(u,'create_routes'):
                    action = "<a class='btn' href='/routes'>Ir para Cargas/Rotas</a>"
                else:
                    action = "<span class='muted'>Aguardando planejamento de carga por usuário autorizado.</span>"
                hint = 'Pedido faturado e agendado. Adicione na carga correspondente na data de entrega.'
        return f"<section class='panel assisted-panel'><h2>Modo Assistido do Pedido</h2><div class='assisted-steps'><span>{esc(step)}</span><span>{esc(hint)}</span></div><div class='action-strip'>{action}</div></section>"

    def route_assisted_block(self, route, u=None):
        st = normalize_route_status(route['status'])
        step = '3/3 Carga finalizada'
        action = '<span class="muted">Fluxo concluído para esta carga.</span>'
        hint = 'Acerto já concluído. Reabra somente com motivo formal.'
        if st == 'Planejada':
            step = '1/3 Planejamento'
            action = f"<form method='post' action='/routes/{route['id']}/dispatch'><button>Próximo passo: marcar saída</button></form>"
            hint = 'Conferiu capacidade e sequência? então libere a saída.'
        elif st == 'Em rota':
            step = '2/3 Em rota'
            if u and self.has_perm(u,'settle_routes'):
                action = f"<a class='btn' href='/load-settlement?q={quote(route['name'])}'>Próximo passo: acerto de carga</a>"
            else:
                action = "<span class='muted'>Aguardando usuário com permissão de acerto.</span>"
            hint = 'Baixe todos os pedidos na volta da carga.'
        return f"<section class='panel assisted-panel'><h2>Modo Assistido da Carga</h2><div class='assisted-steps'><span>{esc(step)}</span><span>{esc(hint)}</span></div><div class='action-strip'>{action}</div></section>"

    def table_orders(self,rows,compact=False,u=None):
        if not rows: return '<div class="empty"><b>Nenhum pedido encontrado.</b><span>Use filtros diferentes ou cadastre um novo pedido.</span></div>'
        can_view_financial = self.can_view_financial(u) if u else False
        weight_col_title = 'Peso / Valor' if can_view_financial else 'Peso'
        head=f'<div class="table-wrap"><table class="orders-table"><thead><tr><th>Documento</th><th>Cliente / Fazenda</th><th>Rota / Cidade</th><th>Status</th><th>Prazo limite</th><th>{weight_col_title}</th><th>Ações</th></tr></thead><tbody>'
        body=''
        can_edit = self.has_perm(u,'edit_orders') if u else False
        can_delete = self.has_perm(u,'cancel_orders') if u else False
        for r in rows:
            row_class = order_sla_row_class(r['expected_delivery_date'], r['status'])
            next_action = self.order_next_action(r, u=u)
            edit_link = f'<a class="btn small ghost" href="/orders/{r["id"]}/edit">Editar</a>' if can_edit else ''
            delete_form = f"<form method='post' action='/orders/{r['id']}/delete' class='inline-mini needs-confirm' data-confirm-text='Confirma apagar este pedido definitivamente?'><button class='danger-btn small'>Apagar</button></form>" if can_delete else ''
            value_html = f'<br><small>{money_visible(can_view_financial, r["total_value"])}</small>' if can_view_financial else ''
            body += f'''<tr class="{row_class}"><td><b class="order-no">{esc(r['order_number'])}</b><br><small>Faturamento: {esc(r['invoice_number'] or 'pendente')} · Carga: {esc(r['load_name'] or 'sem carga')} · Vendedor: {esc(r['seller_name'] or 'Não informado')}</small></td><td>{esc(r['client'] or 'Cliente não vinculado')}<br><small>{esc(r['farm_name'] or r['delivery_address'] or '')}</small></td><td>{esc(r['route_name'] or 'Sem rota')}<br><small>{esc(r['city'] or r['client_city'] or 'Sem cidade')}</small></td><td>{badge(r['status'])}</td><td>{brdate(r['expected_delivery_date'])}<br>{deadline_pill(r['expected_delivery_date'], r['status'])}</td><td><b>{fmt_num(r['weight_kg'])} kg</b>{value_html}</td><td class="row-actions">{next_action}<a class="btn small ghost" href="/orders/{r['id']}">Abrir</a>{edit_link}{delete_form}</td></tr>'''
        return head+body+'</tbody></table></div>'

    def orders(self,u):
        rows=self.query_orders(); qs=parse_qs(urlparse(self.path).query)
        history_mode = str(qs.get('history',[''])[0]).strip().lower() in ('1','true','sim','yes')
        page=max(1,parse_int(qs.get('page',['1'])[0],1))
        page_size=40
        with conn() as db:
            cities=[r['city'] for r in db.execute('SELECT DISTINCT city FROM orders WHERE city IS NOT NULL AND city<>"" ORDER BY city')]
            routes=[r['route_name'] for r in db.execute('SELECT DISTINCT route_name FROM orders WHERE route_name IS NOT NULL AND route_name<>"" ORDER BY route_name')]
            clients=db.execute('SELECT id,customer_code,name FROM clients ORDER BY name').fetchall()
        client_opts='<option value="">Todos clientes</option>'+''.join(f'<option value="{c["id"]}" {"selected" if str(c["id"])==str(qs.get("client_id",[""])[0]) else ""}>{esc(client_display_name(c["customer_code"], c["name"]))}</option>' for c in clients)
        status_options = STATUSES if history_mode else [s for s in STATUSES if s not in FINAL_ORDER_STATUSES]
        nf=qs.get('nf',[''])[0]
        deadline_from=qs.get('deadline_from',[''])[0]
        deadline_to=qs.get('deadline_to',[''])[0]
        total_rows=len(rows)
        total_pages=max(1,(total_rows + page_size - 1)//page_size)
        if page > total_pages:
            page = total_pages
        start=(page-1)*page_size
        end=start+page_size
        page_rows=rows[start:end]
        pager_params = {k: list(v) for k, v in qs.items() if k != 'page'}
        if history_mode and 'history' not in pager_params:
            pager_params['history'] = ['1']
        prev_qs = urlencode({k: v[0] for k, v in pager_params.items()}, doseq=False)
        prev_prefix = f"&{prev_qs}" if prev_qs else ''
        prev_link = f"<a class='btn ghost small' href='/orders?page={page-1}{prev_prefix}'>Página anterior</a>" if page > 1 else "<span class='muted'>Página anterior</span>"
        next_link = f"<a class='btn ghost small' href='/orders?page={page+1}{prev_prefix}'>Próxima página</a>" if page < total_pages else "<span class='muted'>Fim da lista</span>"
        history_hidden = "<input type='hidden' name='history' value='1'>" if history_mode else ""
        new_order_link = '<a class="btn" href="/orders/new">+ Novo pedido</a>' if self.has_perm(u,'create_orders') else ''
        view_toggle = "<a class='btn ghost' href='/orders'>Pedidos em andamento</a>" if history_mode else "<a class='btn ghost' href='/orders?history=1'>Histórico de finalizados</a>"
        mode_label = 'Histórico de finalizados' if history_mode else 'Pedidos em andamento'
        filt=f'''<div class="toolbar"><div>{new_order_link}{view_toggle}<a class="btn ghost" href="/orders?late=1">Atrasados</a><a class="btn ghost" href="/orders?near=1">Próximos do limite</a></div><div class="muted"><b>{mode_label}</b> · <b>{total_rows}</b> pedido(s)</div></div><form class="filters">{history_hidden}<input name="q" placeholder="Busca rápida por pedido, cliente, cidade ou observação" value="{esc(qs.get('q',[''])[0])}"><select name="status">{option(status_options,qs.get('status',[''])[0],True,'Todos status')}</select><select name="city">{option(cities,qs.get('city',[''])[0],True,'Todas cidades')}</select><select name="route">{option(routes,qs.get('route',[''])[0],True,'Todas rotas')}</select><select name="client_id">{client_opts}</select><input name="nf" placeholder="NF" value="{esc(nf)}"><label>Prazo de<input type="date" name="deadline_from" value="{esc(deadline_from)}"></label><label>até<input type="date" name="deadline_to" value="{esc(deadline_to)}"></label><button>Filtrar</button><a class="btn ghost" href="/orders{'?history=1' if history_mode else ''}">Limpar</a></form>'''
        pager = f"<div class='action-strip'>{prev_link}<span class='muted'>Página {page} de {total_pages}</span>{next_link}</div>" if total_pages > 1 else ''
        subtitle = 'Somente pedidos abertos aparecem nesta visão.' if not history_mode else 'Pedidos finalizados (acertado, problema ou cancelado).'
        return self.send_html(layout(u,'Pedidos',filt+self.table_orders(page_rows,u=u)+pager,subtitle))

    def order_form(self,u,row=None):
        with conn() as db:
            clients=list(db.execute('SELECT id,customer_code,name,farm_name,city,phone,whatsapp,address,route_name FROM clients WHERE active=1 ORDER BY name').fetchall())
            if row and row['client_id']:
                extra_client=db.execute('SELECT id,customer_code,name,farm_name,city,phone,whatsapp,address,route_name FROM clients WHERE id=?',(row['client_id'],)).fetchone()
                if extra_client and all(int(c['id']) != int(extra_client['id']) for c in clients):
                    clients.append(extra_client)
            drivers=db.execute('SELECT id,name FROM drivers WHERE active=1 ORDER BY name').fetchall()
            vehicles=db.execute('SELECT id,name,plate FROM vehicles WHERE active=1 ORDER BY name').fetchall()
            city_rows=db.execute('SELECT DISTINCT city,route_name FROM route_cities WHERE active=1 AND city IS NOT NULL AND city<>"" ORDER BY city').fetchall()
            holidays=db.execute('SELECT date FROM holidays').fetchall()
            if not city_rows:
                city_rows=db.execute('SELECT DISTINCT COALESCE(city,"") city, COALESCE(route_name,"") route_name FROM clients WHERE city IS NOT NULL AND city<>"" UNION SELECT DISTINCT COALESCE(city,""), COALESCE(route_name,"") FROM orders WHERE city IS NOT NULL AND city<>"" ORDER BY city').fetchall()
        action='/orders/new' if not row else f'/orders/{row["id"]}/edit'
        status=normalize_order_status(row['status'] if row else 'Venda')
        sale=row['sale_date'] if row else today()
        limit=row['expected_delivery_date'] if row and row['expected_delivery_date'] else add_business_days(sale, get_setting('sla_limit_days','15'))
        cities=sorted({r['city'] for r in city_rows if r['city']})
        routes=sorted({r['route_name'] for r in city_rows if r['route_name']})
        current_city = (row['city'] if row else '') or ''
        current_route = (row['route_name'] if row else '') or ''
        if current_city and current_city not in cities:
            cities.insert(0, current_city)
        if current_route and current_route not in routes:
            routes.insert(0, current_route)
        route_pairs=';'.join(f"{esc(r['city'])}|{esc(r['route_name'])}" for r in city_rows if r['city'])
        city_opts=''.join(f'<option value="{esc(c)}" {"selected" if str(c)==str(current_city) else ""}>{esc(c)}</option>' for c in cities)
        route_opts=''.join(f'<option value="{esc(r)}" {"selected" if str(r)==str(current_route) else ""}>{esc(r)}</option>' for r in routes)
        if row:
            route_field = f'<label>Rota<select name="route_name" id="routeInput"><option value="">Selecione a rota</option>{route_opts}</select></label>'
        else:
            route_field = f'<label>Rota (automática)<select id="routeInput" disabled aria-disabled="true"><option value="">Selecione a cidade</option>{route_opts}</select><input type="hidden" name="route_name" id="routeInputHidden" value="{esc(current_route)}"><small>Definida automaticamente pela cidade selecionada.</small></label>'
        holiday_tags=''.join(f'<span data-holiday="{esc(h["date"])}" hidden></span>' for h in holidays)
        selected_client = None
        if row and row['client_id']:
            selected_client = next((c for c in clients if str(c['id']) == str(row['client_id'])), None)
        client_name_value = (selected_client['name'] if selected_client else '')
        client_phone_value = ((selected_client['phone'] or selected_client['whatsapp']) if selected_client else '')
        client_farm_value = (selected_client['farm_name'] if selected_client else '')
        client_code_value = (selected_client['customer_code'] if selected_client else '')
        client_search_value = (client_display_name(selected_client['customer_code'], selected_client['name']) if selected_client else '')
        client_opts = '<option value="">Novo cliente / preencher abaixo</option>' + ''.join(
            f'<option value="{c["id"]}" data-name="{esc(c["name"])}" data-code="{esc(c["customer_code"] or "")}" data-phone="{esc(c["phone"] or c["whatsapp"] or "")}" data-farm="{esc(c["farm_name"] or "")}" data-city="{esc(c["city"] or "")}" data-route="{esc(c["route_name"] or "")}" data-address="{esc(c["address"] or c["farm_name"] or "")}" {"selected" if row and str(c["id"])==str(row["client_id"] or "") else ""}>{esc(client_display_name(c["customer_code"], c["name"]))} · {esc(c["farm_name"] or c["city"] or "")}</option>' for c in clients)
        row_vehicle_id = row['vehicle_id'] if row else None
        vehicle_opts=row_options(vehicles,row_vehicle_id,lambda r:f"{r['name']} {r['plate'] or ''}",True,'Sem veículo')
        status_field = f'<label>Status<input id="statusDisplayInput" value="{esc(status)}" readonly><input type="hidden" name="status" id="statusHiddenInput" value="{esc(status)}"></label>'
        row_updated_at = row['updated_at'] if row else ''
        row_version = row['version'] if row else 1
        row_order_number = row['order_number'] if row else ''
        row_weight_kg = row['weight_kg'] if row else 0
        row_total_value = row['total_value'] if row else 0
        row_delivery_address = row['delivery_address'] if row else ''
        row_payment_method = row['payment_method'] if row else ''
        row_invoice_number = row['invoice_number'] if row else ''
        row_invoiced_date = (row['invoiced_at'] or '')[:10] if row else ''
        row_driver_id = row['driver_id'] if row else None
        row_location_link = row['location_link'] if row else ''
        row_notes = row['notes'] if row else ''
        row_seller_name = (row['seller_name'] if row else u['name']) or u['name']
        can_view_financial = self.can_view_financial(u)
        if can_view_financial or not row:
            total_value_field = f'<label>Valor total<input name="total_value" id="totalValueInput" type="text" inputmode="decimal" data-mask="currency" data-decimals="2" value="{esc(row_total_value)}" placeholder="R$ 0,00"></label>'
        else:
            total_value_field = f'<label>Valor total<input value="Oculto para este perfil" readonly><input type="hidden" name="total_value" value="{esc(row_total_value)}"><small>Você pode editar o pedido sem visualizar este valor.</small></label>'
        row_hidden_meta = f'<input type="hidden" name="updated_at" value="{esc(row_updated_at)}"><input type="hidden" name="version" value="{esc(row_version)}">' if row else ''

        faturamento_fieldset = f'<input type="hidden" name="invoice_number" id="hiddenInvoiceNumber" value=""><input type="hidden" name="invoiced_at" id="hiddenInvoicedAt" value="">' if not row else ''
        if row and status != 'Venda':
            faturamento_fieldset = f'''<fieldset>
              <legend>Faturamento e entrega</legend>
              <div class="grid3">
                <label>Nº NF<input name="invoice_number" value="{esc(row_invoice_number)}"></label>
                <label>Data faturamento<input type="date" name="invoiced_at" value="{esc(row_invoiced_date)}"></label>
                <label>Motorista<select name="driver_id">{row_options(drivers,row_driver_id,lambda r:r['name'],True,'Sem motorista')}</select></label>
                <label>Veículo<select name="vehicle_id">{vehicle_opts}</select></label>
                <label>Link localização<input name="location_link" value="{esc(row_location_link)}"></label>
              </div>
            </fieldset>'''

        # ERP Auto-Lookup: busca automaticamente no TAB, ENTER ou clique do botão
        erp_btn_html = ''
        erp_js_html = ''
        if not row and _ERP_AVAILABLE:
            erp_btn_html = f'''<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:0.85rem 1.1rem;margin-bottom:1.25rem;display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap;">
  <div>
    <h4 style="margin:0;font-size:0.95rem;color:#166534;font-weight:700;">⚡ Integração Direta com Banco Oracle ERP</h4>
    <small style="color:#15803d;">Puxe dados de um pedido específico pelo número ou execute a sincronização manual do banco de vendas ERP.</small>
  </div>
  <div style="display:flex;align-items:center;gap:.6rem;">
    <button type="button" onclick="triggerManualErpSyncModal()" class="btn" style="background:#166534;color:#fff;border:none;padding:.55rem 1rem;border-radius:6px;font-size:.88rem;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:.4rem;">
      🔄 Sincronizar Vendas do ERP Manualmente
    </button>
  </div>
</div>'''
            erp_js_html = '''
<!-- POPUP MODAL WINDOW (JANELA POPUP DE SINCRONIZAÇÃO ERP) -->
<div id="erpSyncOrderModalOverlay" style="display:none;position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(15,23,42,0.8);backdrop-filter:blur(5px);z-index:999999;align-items:center;justify-content:center;padding:1rem;">
    <div style="background:#ffffff;border-radius:12px;width:100%;max-width:720px;max-height:90vh;display:flex;flex-direction:column;box-shadow:0 25px 50px -12px rgba(0,0,0,0.4);overflow:hidden;border:1px solid #cbd5e1;animation:fadeInModal 0.25s ease;">
        <div style="background:#0f172a;color:#f8fafc;padding:1rem 1.4rem;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #334155;">
            <div style="display:flex;align-items:center;gap:.75rem;">
                <span id="orderModalTitle" style="font-weight:bold;font-size:1.05rem;color:#f8fafc;">🔄 Sincronização em Tempo Real com Banco Oracle ERP</span>
                <span id="orderModalTag" class="badge warning" style="font-size:0.78rem;">SINCRONIZANDO</span>
            </div>
            <button type="button" onclick="closeOrderSyncModal()" style="background:transparent;border:none;color:#94a3b8;font-size:1.4rem;cursor:pointer;line-height:1;padding:0 0.4rem;">&times;</button>
        </div>
        <div style="padding:1.4rem;overflow-y:auto;flex:1;background:#f8fafc;">
            <div id="orderModalProgressBox" style="margin-bottom:1rem;">
                <div style="display:flex;justify-content:space-between;font-size:0.85rem;color:#334155;margin-bottom:0.4rem;font-weight:600;">
                    <span id="orderProgressStep">Conectando ao banco Oracle ERP (10.253.7.2:1521)...</span>
                    <span id="orderProgressPercent">0%</span>
                </div>
                <div style="background:#e2e8f0;border-radius:8px;overflow:hidden;height:20px;position:relative;box-shadow:inset 0 1px 3px rgba(0,0,0,0.15);">
                    <div id="orderProgressBarFill" style="height:100%;background:linear-gradient(90deg, #2563eb, #10b981);width:0%;transition:width 0.3s ease;border-radius:8px;"></div>
                </div>
            </div>
            <div style="background:#0f172a;color:#e2e8f0;font-family:monospace;font-size:0.84rem;padding:1rem;border-radius:8px;max-height:260px;overflow-y:auto;border:1px solid #1e293b;box-shadow:inset 0 2px 4px rgba(0,0,0,0.3);">
                <div id="orderModalLogBody">
                    <div style="color:#60a5fa;">⏳ Conectando e processando... por favor aguarde.</div>
                </div>
            </div>
        </div>
        <div style="background:#ffffff;padding:1rem 1.4rem;display:flex;justify-content:space-between;align-items:center;border-top:1px solid #e2e8f0;">
            <span style="font-size:0.82rem;color:#64748b;">💡 Informações detalhadas da sincronização do banco Oracle</span>
            <button type="button" id="orderModalCloseBtn" onclick="closeOrderSyncModal()" class="btn ghost" style="min-width:130px;" disabled>⏳ Sincronizando...</button>
        </div>
    </div>
</div>

<script>
var lastErpSearched = '';
var orderPollTimer = null;

function triggerManualErpSyncModal() {
  document.getElementById('orderModalTitle').textContent = '🔄 Sincronização em Tempo Real com Banco Oracle ERP';
  document.getElementById('orderModalTag').className = 'badge warning';
  document.getElementById('orderModalTag').textContent = 'SINCRONIZANDO...';
  document.getElementById('orderProgressStep').textContent = 'Conectando ao banco Oracle ERP (10.253.7.2:1521)...';
  document.getElementById('orderProgressPercent').textContent = '10%';
  document.getElementById('orderProgressBarFill').style.width = '10%';
  document.getElementById('orderModalLogBody').innerHTML = '<div style="color:#60a5fa;">⏳ Conectando ao Banco Oracle ERP e iniciando thread de sincronização de vendas...</div>';

  var closeBtn = document.getElementById('orderModalCloseBtn');
  closeBtn.disabled = true;
  closeBtn.style.opacity = '0.5';
  closeBtn.textContent = '⏳ Sincronizando...';

  document.getElementById('erpSyncOrderModalOverlay').style.display = 'flex';

  fetch('/admin/erp/sync', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      startOrderPollingProgress();
    })
    .catch(function(err) {
      document.getElementById('orderModalTag').className = 'badge danger';
      document.getElementById('orderModalTag').textContent = 'ERRO';
      document.getElementById('orderModalLogBody').innerHTML = '<div style="color:#f87171;">❌ Falha na conexão com o servidor: ' + err + '</div>';
      closeBtn.disabled = false;
      closeBtn.style.opacity = '1';
      closeBtn.textContent = 'Fechar Janela';
    });
}

function startOrderPollingProgress() {
  if (orderPollTimer) clearInterval(orderPollTimer);
  var attempts = 0;
  orderPollTimer = setInterval(function() {
    attempts++;
    fetch('/admin/erp/status')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        var isRunning = d && d.running;
        var pCount = (d && d.cache_pedidos_count) || 0;
        var fCount = (d && d.cache_fat_count) || 0;
        var lastSync = (d && d.last_sync_at) || 'Agora';

        var pct = isRunning ? Math.min(90, 25 + attempts * 15) : 100;
        document.getElementById('orderProgressPercent').textContent = pct + '%';
        document.getElementById('orderProgressBarFill').style.width = pct + '%';

        var logBody = document.getElementById('orderModalLogBody');
        logBody.innerHTML = '<div style="color:#4ade80;font-weight:bold;margin-bottom:.5rem;">📦 Sincronizando Vendas & Faturamentos do Oracle ERP...</div>' +
          '<div style="color:#e2e8f0;line-height:1.6;">' +
          '• <b>Conexão Oracle:</b> OK (Host: 10.253.7.2:1521)<br>' +
          '• <b>Pedidos Importados para Cache:</b> <strong style="color:#4ade80;">' + pCount + '</strong> registros<br>' +
          '• <b>Faturamentos Detectados:</b> <strong style="color:#4ade80;">' + fCount + '</strong> registros<br>' +
          '• <b>Horário da Sincronização:</b> ' + lastSync +
          '</div>';

        if (!isRunning || attempts >= 8) {
          clearInterval(orderPollTimer);
          orderPollTimer = null;
          document.getElementById('orderModalTag').className = 'badge success';
          document.getElementById('orderModalTag').textContent = 'CONCLUÍDO';
          document.getElementById('orderProgressStep').textContent = '✅ Sincronização manual concluída com sucesso!';
          document.getElementById('orderProgressPercent').textContent = '100%';
          document.getElementById('orderProgressBarFill').style.width = '100%';

          var closeBtn = document.getElementById('orderModalCloseBtn');
          closeBtn.disabled = false;
          closeBtn.style.opacity = '1';
          closeBtn.textContent = 'Fechar Janela';

          var orderInp = document.querySelector('[name=order_number]');
          if (orderInp && orderInp.value.trim()) {
            autoErpLookup(true);
          }
        }
      })
      .catch(function() {
        if (attempts >= 5) {
          clearInterval(orderPollTimer);
          orderPollTimer = null;
          var closeBtn = document.getElementById('orderModalCloseBtn');
          closeBtn.disabled = false;
          closeBtn.style.opacity = '1';
          closeBtn.textContent = 'Fechar Janela';
        }
      });
  }, 1000);
}

function closeOrderSyncModal() {
  document.getElementById('erpSyncOrderModalOverlay').style.display = 'none';
  if (orderPollTimer) { clearInterval(orderPollTimer); orderPollTimer = null; }
}

function closeErpInvoicedModal() {
  var el = document.getElementById("erpInvoicedModal");
  if (el) el.remove();
}

function tryAutoSelectExistingClient(clientName, clientCode) {
  var sel = document.getElementById('clientSelect');
  var searchInp = document.getElementById('clientSearchInput');
  var dupWarn = document.querySelector('[data-client-dup-warning]');
  if (!sel) return false;

  var norm = function(s) {
    return String(s || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]/g, '');
  };

  var targetName = norm(clientName);
  var targetCode = norm(clientCode);
  if (!targetName && !targetCode) return false;

  var matchedOpt = null;
  for (var i = 0; i < sel.options.length; i++) {
    var opt = sel.options[i];
    if (!opt.value) continue;
    var optName = norm(opt.dataset.name || opt.text);
    var optCode = norm(opt.dataset.code || '');

    if ((targetCode && optCode && targetCode === optCode) ||
        (targetName && optName && (targetName === optName || optName.indexOf(targetName) !== -1 || targetName.indexOf(optName) !== -1))) {
      matchedOpt = opt;
      break;
    }
  }

  if (matchedOpt) {
    sel.value = matchedOpt.value;
    if (searchInp) {
      var codePrefix = (matchedOpt.dataset.code || '') ? (matchedOpt.dataset.code + ' - ') : '';
      searchInp.value = codePrefix + (matchedOpt.dataset.name || matchedOpt.text);
    }
    if (dupWarn) {
      dupWarn.hidden = false;
      dupWarn.style.display = 'block';
      dupWarn.className = 'alert info client-dup-warning';
      dupWarn.innerHTML = 'ℹ️ <b>Cliente local cadastrado:</b> ' + (matchedOpt.dataset.name || matchedOpt.text) + ' (ID: ' + matchedOpt.value + '). Os dados do pedido serão vinculados a este cliente existente.';
    }
    return true;
  } else {
    if (dupWarn) {
      dupWarn.hidden = true;
      dupWarn.style.display = 'none';
    }
  }
  return false;
}

function autoErpLookup(force) {
  var inp = document.querySelector('[name=order_number]');
  if (!inp) return;
  var num = inp.value.trim();
  if (!num) return;
  if (!force && num === lastErpSearched) return;

  var statusEl = document.getElementById('erpAutoLookupStatus');
  if (!statusEl) {
    statusEl = document.createElement('small');
    statusEl.id = 'erpAutoLookupStatus';
    statusEl.style.cssText = 'display:block;margin-top:.3rem;font-weight:600;font-size:.84rem;transition:all 0.2s ease;';
    inp.parentNode.parentNode ? inp.parentNode.parentNode.appendChild(statusEl) : inp.parentNode.appendChild(statusEl);
  }

  lastErpSearched = num;
  statusEl.style.color = '#2563eb';
  statusEl.textContent = '⏳ Buscando dados do pedido nº ' + num + ' no ERP...';

  fetch('/orders/erp-lookup?order_number=' + encodeURIComponent(num))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d.found) {
        statusEl.style.color = '#dc2626';
        statusEl.textContent = '⚠️ Pedido nº ' + num + ' não foi localizado no ERP. Verifique se o número está correto.';
        return;
      }
      var f = d.data;

      window._isAutoErpLookupFilling = true;
      function fill(sel, val) {
        if (!val) return;
        var el = document.querySelector(sel);
        if (el) {
          el.value = val;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }

      try {
        fill('[name=seller_name]', f.seller_name);
        fill('[name=sale_date]', f.sale_date);

        // Peso total formatado
        var wVal = (f.weight_kg > 0 ? f.weight_kg : (f.total_weight > 0 ? f.total_weight : 0));
        var wInp = document.querySelector('[name=weight_kg]');
        if (wInp) {
          wInp.value = String(wVal || 0).replace('.', ',');
          wInp.dispatchEvent(new Event('blur'));
        }

        // Valor total formatado como BRL (R$ 1.076,00) via máscara
        var tvVal = parseFloat(f.total_value || 0);
        var tvInp = document.querySelector('[name=total_value]');
        if (tvInp) {
          tvInp.value = tvVal > 0 ? String(tvVal).replace('.', ',') : '0';
          tvInp.dispatchEvent(new Event('blur'));
        }

        fill('[name=client_name]', f.client_name);
        fill('[name=customer_code]', f.customer_code || f.client_code);
        fill('#clientCodeInput', f.customer_code || f.client_code);
        fill('[name=farm_name]', f.farm_name || f.customer_farm);
        fill('#farmName', f.farm_name || f.customer_farm);
        fill('[name=client_phone]', f.phone);
        fill('#clientPhone', f.phone);
        fill('[name=delivery_address]', f.delivery_address);

        tryAutoSelectExistingClient(f.client_name, f.customer_code || f.client_code);

        var clientSearchInp = document.getElementById('clientSearchInput');
        if (clientSearchInp && f.client_name && !document.getElementById('clientSelect').value) {
          var codePrefix = (f.customer_code || f.client_code) ? (f.customer_code || f.client_code) + ' - ' : '';
          clientSearchInp.value = codePrefix + f.client_name;
        }

        var cityInput = document.getElementById('cityInput');
        if (cityInput && f.city) {
          function normCity(str) {
            return String(str || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]/g, '');
          }
          var targetCity = normCity(f.city);
          var matchedIdx = -1;
          for (var i = 0; i < cityInput.options.length; i++) {
            var optVal = normCity(cityInput.options[i].value);
            var optText = normCity(cityInput.options[i].text);
            if (optVal && (optVal === targetCity || optText === targetCity || optVal.indexOf(targetCity) !== -1 || optText.indexOf(targetCity) !== -1 || targetCity.indexOf(optVal) !== -1 || targetCity.indexOf(optText) !== -1)) {
              matchedIdx = i;
              break;
            }
          }
          if (matchedIdx >= 0) {
            cityInput.selectedIndex = matchedIdx;
            cityInput.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }

        var pmSel = document.querySelector('[name=payment_method]');
        if (pmSel && f.payment_method) {
          var pmTarget = f.payment_method.trim().toLowerCase();
          for (var j = 0; j < pmSel.options.length; j++) {
            var optVal = pmSel.options[j].value.trim().toLowerCase();
            var optText = pmSel.options[j].text.trim().toLowerCase();
            if (optVal === pmTarget || optText === pmTarget || optVal.indexOf(pmTarget) !== -1 || pmTarget.indexOf(optVal) !== -1) {
              pmSel.selectedIndex = j;
              break;
            }
          }
        }

        // Faturamento Automático + Popup Modal
        if (f.is_invoiced || f.invoiced || (f.invoice_number && String(f.invoice_number).trim() !== "0" && String(f.invoice_number).trim() !== "")) {
          var stInps = document.querySelectorAll('[name=status]');
          stInps.forEach(function(el) { el.value = 'Faturado'; });
          var stDisp = document.getElementById('statusDisplayInput');
          if (stDisp) stDisp.value = 'Faturado';

          fill('[name=invoice_number]', f.invoice_number);
          fill('#hiddenInvoiceNumber', f.invoice_number);
          fill('[name=invoiced_at]', f.invoiced_at || f.sale_date);
          fill('#hiddenInvoicedAt', f.invoiced_at || f.sale_date);

          var modalHtml = '<div id="erpInvoicedModal" style="position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(15,23,42,0.65);z-index:99999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px);padding:1rem;">' +
            '<div style="background:#fff;border-radius:12px;max-width:520px;width:100%;padding:1.75rem;box-shadow:0 20px 25px -5px rgba(0,0,0,0.1),0 8px 10px -6px rgba(0,0,0,0.1);border:1px solid #cbd5e1;animation:fadeIn 0.2s ease-out;">' +
            '<div style="display:flex;align-items:center;gap:.75rem;margin-bottom:1rem;">' +
            '<div style="width:42px;height:42px;border-radius:50%;background:#dcfce7;display:flex;align-items:center;justify-content:center;font-size:1.4rem;">🎉</div>' +
            '<div><h3 style="margin:0;font-size:1.15rem;color:#0f172a;font-weight:700;">Pedido JÁ Faturado no ERP!</h3>' +
            '<small style="color:#64748b;">Nº ' + num + '</small></div>' +
            '</div>' +
            '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:1rem;margin-bottom:1.25rem;font-size:.9rem;color:#334155;line-height:1.5;">' +
            '<p style="margin:0 0 .5rem 0;"><b>Nota Fiscal Nº:</b> <span style="color:#166534;font-weight:700;">' + (f.invoice_number || 'Sim') + '</span></p>' +
            '<p style="margin:0 0 .5rem 0;"><b>Data Faturamento:</b> ' + (f.invoiced_at || f.sale_date || '-') + '</p>' +
            '<p style="margin:0;color:#15803d;font-weight:600;">Este pedido JÁ CONSTA COMO FATURADO no ERP! Ao salvar este cadastro, o pedido será registrado automaticamente com o status "Faturado" no sistema de logística e ficará imediatamente disponível para a montagem de cargas e expedição.</p>' +
            '</div>' +
            '<button type="button" onclick="closeErpInvoicedModal()" style="width:100%;background:#166534;color:#fff;border:none;padding:.75rem 1rem;border-radius:8px;font-weight:600;font-size:.95rem;cursor:pointer;transition:background 0.2s;">Entendido, Prosseguir</button>' +
            '</div>' +
            '</div>';

          var oldModal = document.getElementById('erpInvoicedModal');
          if (oldModal) oldModal.remove();
          document.body.insertAdjacentHTML('beforeend', modalHtml);
        }
      } finally {
        window._isAutoErpLookupFilling = false;
      }

      statusEl.style.color = '#166534';
      statusEl.textContent = '✅ Pedido nº ' + num + ' localizado no ERP! Dados preenchidos automaticamente.';
    })
    .catch(function(){
      statusEl.style.color = '#dc2626';
      statusEl.textContent = '⚠️ Não foi possível consultar o ERP no momento. Preencha manualmente.';
    });
}

document.addEventListener('DOMContentLoaded', function(){
  var inp = document.querySelector('[name=order_number]');
  if (inp) {
    inp.addEventListener('blur', function(){ autoErpLookup(); });
    inp.addEventListener('change', function(){ autoErpLookup(); });
    inp.addEventListener('keydown', function(e){
      if (e.key === 'Enter') {
        e.preventDefault();
        autoErpLookup(true);
      }
    });
  }
  var cName = document.getElementById('clientName');
  var cCode = document.getElementById('clientCodeInput');
  var onClientChange = function() {
    var nameVal = cName ? cName.value : '';
    var codeVal = cCode ? cCode.value : '';
    tryAutoSelectExistingClient(nameVal, codeVal);
  };
  if (cName) {
    cName.addEventListener('blur', onClientChange);
    cName.addEventListener('change', onClientChange);
  }
  if (cCode) {
    cCode.addEventListener('blur', onClientChange);
    cCode.addEventListener('change', onClientChange);
  }
});
</script>'''

        order_num_field = f'<label>Número do pedido *<input name="order_number" id="orderNumberInput" required value="{esc(row_order_number)}"></label>' if row else f'''<label>Número do pedido *
  <div style="display:flex;gap:.4rem;align-items:center;">
    <input name="order_number" id="orderNumberInput" required value="{esc(row_order_number)}" placeholder="Digite o nº (ex: 50042752)">
    <button type="button" onclick="autoErpLookup(true)" title="Puxar dados do ERP Oracle" style="background:#166534;color:#fff;border:none;padding:.45rem .85rem;border-radius:6px;font-size:.85rem;font-weight:600;cursor:pointer;white-space:nowrap;display:inline-flex;align-items:center;gap:.3rem;">
      🔍 Puxar ERP
    </button>
  </div>
</label>'''

        return f'''<form method="post" action="{action}" class="form professional-form" data-route-pairs="{route_pairs}" data-client-dup-check="1" data-client-dup-endpoint="/api/clients/duplicate-check">
{holiday_tags}{row_hidden_meta}
<div class="status-ribbon"><span>Status atual</span>{badge(status)}<em>SLA oficial: 15 dias corridos após a venda</em></div>
{erp_btn_html}
<fieldset>
  <legend>Dados do pedido</legend>
  <div class="grid3">
    {order_num_field}
    {status_field}
    <label>Data da venda<input type="date" name="sale_date" id="saleDate" value="{esc(sale)}"></label>
    <label>Prazo limite<input type="date" name="expected_delivery_date" id="deadlineDate" value="{esc(limit)}" readonly><small>Calculado automaticamente com 15 dias corridos a partir da data de venda.</small></label>
    <label>Vendedor *<input name="seller_name" required value="{esc(row_seller_name)}" placeholder="Nome do vendedor responsável"></label>
    <label>Cadastrante<input value="{esc(u['name'])}" readonly><input type="hidden" name="seller_id" value="{u['id']}"><small>Preenchido automaticamente pelo usuário logado.</small></label>
    <label>Forma pagamento<select name="payment_method">{payment_method_options(row_payment_method)}</select></label>
    <label>Peso total kg<input name="weight_kg" id="weightKgInput" type="text" inputmode="decimal" data-mask="decimal" data-decimals="2" value="{esc(row_weight_kg)}" placeholder="0,00"></label>
    {total_value_field}
  </div>
</fieldset>
<fieldset>
  <legend>Cliente e endereço</legend>
  <div class="grid3">
    <label class="full client-search-wrap">Buscar cliente
      <input id="clientSearchInput" class="client-search-input" autocomplete="off" value="{esc(client_search_value)}" placeholder="Clique para listar ou digite código/nome do cliente">
      <small>Selecione um cliente existente ou escolha “Novo cliente / preencher abaixo”.</small>
      <div id="clientSearchResults" class="client-search-results" hidden></div>
    </label>
    <select name="client_id" id="clientSelect" class="client-select-hidden">{client_opts}</select>
    <label>Código do cliente<input name="customer_code" id="clientCodeInput" data-force-uppercase value="{esc(client_code_value)}" placeholder="Ex: 1254"><small>Para novo cliente, informe o código. Em cliente existente, é preenchido automaticamente.</small></label>
    <label>Nome do cliente<input name="client_name" id="clientName" data-force-uppercase value="{esc(client_name_value)}" placeholder="Usado se for novo cliente"></label>
    <div class="full"><div class="alert danger client-dup-warning" data-client-dup-warning hidden>Cliente duplicado.</div></div>
    <label>Telefone/WhatsApp<input name="client_phone" id="clientPhone" data-force-uppercase value="{esc(client_phone_value)}" placeholder="Contato"></label>
    <label>Fazenda/local<input name="farm_name" id="farmName" data-force-uppercase value="{esc(client_farm_value)}"></label>
    <label>Cidade<select name="city" id="cityInput" required><option value="">Selecione a cidade</option>{city_opts}</select></label>
    {route_field}
    <label class="full">Endereço/Fazenda<textarea name="delivery_address" id="addressInput" data-force-uppercase>{esc(row_delivery_address)}</textarea></label>
    <label class="full">Observações gerais do pedido<textarea name="notes">{esc(row_notes)}</textarea></label>
  </div>
</fieldset>
{faturamento_fieldset}
<div class="form-actions sticky-actions"><button>Salvar pedido e integrar banco</button><a class="btn ghost" href="/orders">Cancelar/voltar</a></div>
</form>{erp_js_html}'''

    def order_new(self,u): return self.send_html(layout(u,'Novo Pedido',self.order_form(u),'Cadastro estruturado por etapa operacional'))

    def handle_erp_lookup(self, u):
        """GET /orders/erp-lookup?order_number=X — retorna JSON com dados do ERP."""
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(self.path).query)
        order_number = (qs.get('order_number') or [''])[0].strip()
        if not order_number:
            return self.send_json({'found': False, 'message': 'Informe o número do pedido.'}, 400)
        if not _ERP_AVAILABLE:
            return self.send_json({'found': False, 'message': 'Módulo ERP não disponível no servidor.'}, 503)
        try:
            erp_cfg = _erp_connector.get_erp_config()
            if not erp_cfg.enabled:
                return self.send_json({'found': False, 'message': 'Integração ERP desabilitada.'}, 503)
            raw = _erp_connector.lookup_order(order_number)
            if not raw:
                with conn() as db:
                    audit(db, u, 'ERP_LOOKUP_NOT_FOUND', 'ERP', order_number, '', '', f'Pedido não localizado no ERP', source_ip=self.client_ip())
                    db.commit()
                return self.send_json({'found': False, 'message': f'Pedido "{order_number}" não encontrado no ERP.'})
            mapped = _erp_mapper.map_erp_to_logistica(raw)
            with conn() as db:
                audit(db, u, 'ERP_LOOKUP', 'ERP', order_number, '', '', f'Lookup ERP: {mapped.get("_erp_item_count", 0)} item(s)', source_ip=self.client_ip())
                db.commit()
            return self.send_json({'found': True, 'data': mapped})
        except Exception as e:
            import traceback as _tb
            log_server_error('ERP_LOOKUP', e)
            return self.send_json({'found': False, 'message': 'Erro interno ao consultar ERP.'}, 500)
    def order_edit(self,u,oid):
        with conn() as db: row=db.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
        if not row:
            return self.fail(u,'Não encontrado','Pedido não encontrado para edição.',404)
        return self.send_html(layout(u,'Editar Pedido',self.order_form(u,row),'Atualização de dados logísticos e comerciais'))

    def resolve_route_for_city(self, db, city, fallback_route=''):
        city_txt = str(city or '').strip()
        fallback = str(fallback_route or '').strip()
        if not city_txt:
            return fallback
        row = db.execute(
            """SELECT route_name
               FROM route_cities
               WHERE active=1
                 AND LOWER(TRIM(COALESCE(city,'')))=LOWER(TRIM(?))
                 AND route_name IS NOT NULL
                 AND TRIM(route_name)<>''
               ORDER BY COALESCE(delivery_order,9999), route_name, id
               LIMIT 1""",
            (city_txt,),
        ).fetchone()
        if row and str(row['route_name'] or '').strip():
            return str(row['route_name']).strip()
        return fallback

    def ensure_client_from_order(self, db, d):
        cid_raw = (d.get('client_id') or '').strip()
        cid = parse_int(cid_raw) if cid_raw.isdigit() else None
        customer_code = upper_text(d.get('customer_code'))
        name = upper_text(d.get('client_name'))
        city = upper_text(d.get('city'))
        route = self.resolve_route_for_city(db, city, upper_text(d.get('route_name')))
        address = upper_text(d.get('delivery_address'))
        phone = upper_text(d.get('client_phone'))
        farm = upper_text(d.get('farm_name'))
        if cid:
            exists = db.execute('SELECT id,name,city,customer_code FROM clients WHERE id=?',(cid,)).fetchone()
            if not exists:
                raise ValueError('Cliente selecionado não foi encontrado.')
            db.execute('UPDATE clients SET city=COALESCE(NULLIF(?,""),city), route_name=COALESCE(NULLIF(?,""),route_name), address=COALESCE(NULLIF(?,""),address), phone=COALESCE(NULLIF(?,""),phone), farm_name=COALESCE(NULLIF(?,""),farm_name),updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(city,route,address,phone,farm,now(),cid))
            return cid
        if name:
            dup = self.client_exists(db, name, exclude_id=None)
            if dup:
                if int(dup['active'] or 0) != 1:
                    raise ValueError(f'Cliente já cadastrado como inativo: "{dup["name"]}". Reative o cadastro para usar neste pedido.')
                dup_id = int(dup['id'])
                db.execute(
                    'UPDATE clients SET city=COALESCE(NULLIF(?,""),city), route_name=COALESCE(NULLIF(?,""),route_name), address=COALESCE(NULLIF(?,""),address), phone=COALESCE(NULLIF(?,""),phone), farm_name=COALESCE(NULLIF(?,""),farm_name),updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',
                    (city, route, address, phone, farm, now(), dup_id),
                )
                return dup_id
            if not customer_code:
                customer_code = str(db.execute('SELECT COALESCE(MAX(id),0)+1 n FROM clients').fetchone()['n'])
            safe_code = self.ensure_unique_customer_code(db, customer_code, exclude_id=None)
            cur=db.execute('INSERT INTO clients(customer_code,name,phone,whatsapp,city,farm_name,address,route_name,active,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,1,?,?,1)',(safe_code,name,phone,phone,city,farm,address,route,now(),now()))
            new_id = cur.lastrowid
            db.execute('UPDATE clients SET customer_code=COALESCE(NULLIF(customer_code,""),?) WHERE id=?', (str(new_id), new_id))
            return new_id
        return None

    def validate_order_payload(self, d, editing=False):
        order_no=(d.get('order_number') or '').strip()
        if not order_no:
            raise ValueError('Informe o número do pedido.')
        sale=validate_date_field(d.get('sale_date') or today(),'a data da venda',required=True)
        if d.get('invoiced_at'):
            validate_date_field(d.get('invoiced_at'),'a data de faturamento',required=False)
        weight=parse_float(d.get('weight_kg') or 0)
        value=parse_float(d.get('total_value') or 0)
        if weight < 0:
            raise ValueError('Peso não pode ser negativo.')
        if weight == 0:
            raise ValueError('Informe o peso total do pedido (kg).')
        if value < 0:
            raise ValueError('Valor total não pode ser negativo.')
        city=(d.get('city') or '').strip()
        if not city:
            raise ValueError('Informe a cidade do pedido.')
        seller_name = (d.get('seller_name') or '').strip()
        if not ((d.get('client_id') or '').strip() or (d.get('client_name') or '').strip()):
            raise ValueError('Pedido sem cliente. Selecione um cliente ou informe o nome.')
        raw_status=(d.get('status') or 'Venda').strip()
        normalized_status=ORDER_STATUS_ALIASES.get(raw_status, raw_status)
        if raw_status and normalized_status not in STATUSES:
            raise ValueError('Status inválido para o pedido.')
        status=normalize_order_status(raw_status)
        if not editing and not (status == 'Faturado' or (d.get('invoice_number') or '').strip()):
            status='Venda'
        notes=(d.get('notes') or '').strip()
        if status=='Problema' and not notes:
            raise ValueError('Informe observação para registrar pedido com problema.')
        location_link = (d.get('location_link') or '').strip()
        if location_link and not (location_link.lower().startswith('http://') or location_link.lower().startswith('https://')):
            raise ValueError('O link de localização do pedido deve começar com http:// ou https://')
        pay=validate_payment_method(d.get('payment_method') or '',required=True)
        deadline=add_business_days(sale, get_setting('sla_limit_days','15'))
        return {'order_no': order_no, 'sale': sale, 'deadline': deadline, 'weight': weight, 'value': value, 'status': status, 'notes': notes, 'payment_method': pay, 'seller_name': seller_name}

    def post_order_new(self,u):
        d=self.post_data()
        payload=self.validate_order_payload(d, editing=False)
        order_no=payload['order_no']; sale=payload['sale']; deadline=payload['deadline']; status=payload['status']; weight=payload['weight']; value=payload['value']; seller_name=(payload['seller_name'] or u['name']).strip()
        invoice_number=(d.get('invoice_number') or '').strip()
        invoiced_at_val = d.get('invoiced_at') or None

        # Auto-detecção de Faturamento ERP em Tempo Real
        if _ERP_AVAILABLE:
            try:
                raw_erp = _erp_connector.lookup_order(order_no)
                if raw_erp:
                    mapped_erp = _erp_mapper.map_erp_to_logistica(raw_erp)
                    if mapped_erp.get('is_invoiced') and mapped_erp.get('invoice_number'):
                        status = 'Faturado'
                        invoice_number = str(mapped_erp.get('invoice_number')).strip()
                        invoiced_at_val = mapped_erp.get('invoiced_at') or mapped_erp.get('sale_date') or today()
            except Exception as _e:
                log_server_error('POST_ORDER_NEW_ERP_CHECK', _e)

        with conn() as db:
            resolved_route_name = self.resolve_route_for_city(db, upper_text(d.get('city')), upper_text(d.get('route_name')))
            if not resolved_route_name:
                raise ValueError('Não foi possível definir a rota automaticamente para a cidade selecionada. Atualize Cidades e Rotas-base.')
            self.ensure_unique_invoice(db, invoice_number, exclude_order_id=None)
            cid=self.ensure_client_from_order(db,d)
            delivered_at = (d.get('delivered_at') or today()) if status in ('Acertado','Problema') else None
            cur=db.execute('''INSERT INTO orders(order_number,client_id,seller_id,seller_name,status,urgency,sale_date,expected_delivery_date,invoice_limit_date,payment_method,total_value,weight_kg,delivery_address,location_link,route_name,city,notes,invoice_number,invoiced_at,driver_id,vehicle_id,delivered_at,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(order_no,cid,u['id'],seller_name,status,'Normal',sale,deadline,'',payload['payment_method'],value,weight,(d.get('delivery_address') or '').strip(),(d.get('location_link') or '').strip(),resolved_route_name,upper_text(d.get('city')),payload['notes'],invoice_number,invoiced_at_val,d.get('driver_id') or None,d.get('vehicle_id') or None,delivered_at,now(),now(),1))
            oid=cur.lastrowid
            db.execute('INSERT INTO order_items(order_id,product_code,product_name,quantity,unit,weight_kg,notes) VALUES(?,?,?,?,?,?,?)',(oid,'','Carga/volume informado no pedido',1,'kg',weight,d.get('notes')))
            if status=='Problema':
                db.execute('INSERT INTO delivery_problems(order_id,problem_type,description,created_at) VALUES(?,?,?,?)',(oid,'Outro motivo',d.get('notes'),now()))
            add_hist(db,oid,u['id'],None,status,'Pedido criado',f'SLA limite calculado: {deadline}')
            audit(db,u,'Criou pedido','Pedidos',order_no,'',status)
            db.commit()
        self.redirect(f'/orders/{oid}')

    def post_order_edit(self,u,oid):
        d=self.post_data()
        payload=self.validate_order_payload(d, editing=True)
        order_no=payload['order_no']; sale=payload['sale']; deadline=payload['deadline']; weight=payload['weight']; value=payload['value']; seller_name=(payload['seller_name'] or u['name']).strip()
        invoice_number=(d.get('invoice_number') or '').strip()
        with conn() as db:
            resolved_route_name = self.resolve_route_for_city(db, upper_text(d.get('city')), upper_text(d.get('route_name')))
            if not resolved_route_name:
                raise ValueError('Não foi possível definir a rota automaticamente para a cidade selecionada. Atualize Cidades e Rotas-base.')
            oldrow=find_order_by_id(db, oid)
            if not oldrow:
                return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
            posted_updated_at=(d.get('updated_at') or '').strip()
            posted_version=parse_int(d.get('version') or 1,1)
            db_updated_at=str(oldrow['updated_at'] or '').strip()
            db_version=parse_int(oldrow['version'] or 1,1)
            if posted_updated_at and db_updated_at and posted_updated_at != db_updated_at:
                return self.fail(
                    u,
                    'Conflito de edição',
                    'Este pedido foi alterado por outro usuário enquanto você editava. Reabra o pedido para carregar os dados mais recentes e tente novamente.',
                    409,
                )
            if posted_version and db_version and posted_version != db_version:
                return self.fail(
                    u,
                    'Conflito de edição',
                    'Este pedido foi alterado por outro usuário enquanto você editava. Reabra o pedido para carregar os dados mais recentes e tente novamente.',
                    409,
                )
            old=normalize_order_status(oldrow['status'])
            self.ensure_unique_invoice(db, invoice_number, exclude_order_id=oid)
            cid=self.ensure_client_from_order(db,d)
            status,_=self.ensure_order_status(db,oid,d.get('status'),d.get('notes'))
            delivered_at = oldrow['delivered_at']
            if status in ('Acertado','Problema') and not delivered_at:
                delivered_at = today()
            db.execute('''UPDATE orders SET order_number=?,client_id=?,seller_id=?,seller_name=?,status=?,urgency=?,sale_date=?,expected_delivery_date=?,invoice_limit_date=?,payment_method=?,total_value=?,weight_kg=?,delivery_address=?,location_link=?,route_name=?,city=?,notes=?,invoice_number=?,invoiced_at=?,driver_id=?,vehicle_id=?,delivered_at=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?''',(order_no,cid,u['id'],seller_name,status,'Normal',sale,deadline,'',payload['payment_method'],value,weight,(d.get('delivery_address') or '').strip(),(d.get('location_link') or '').strip(),resolved_route_name,upper_text(d.get('city')),payload['notes'],invoice_number,d.get('invoiced_at'),d.get('driver_id') or None,d.get('vehicle_id') or None,delivered_at,now(),oid))
            item=db.execute('SELECT id FROM order_items WHERE order_id=? ORDER BY id LIMIT 1',(oid,)).fetchone()
            if item:
                db.execute('UPDATE order_items SET weight_kg=?, notes=? WHERE id=?',(weight,d.get('notes'),item['id']))
            else:
                db.execute('INSERT INTO order_items(order_id,product_code,product_name,quantity,unit,weight_kg,notes) VALUES(?,?,?,?,?,?,?)',(oid,'','Carga/volume informado no pedido',1,'kg',weight,d.get('notes')))
            if old != status:
                add_hist(db,oid,u['id'],old,status,'Status alterado em edição','')
            add_hist(db,oid,u['id'],old,status,'Pedido editado',f'Prazo limite recalculado: {deadline}')
            audit(db,u,'Editou pedido','Pedidos',oldrow['order_number'],old,status)
            db.commit()
        self.redirect(f'/orders/{oid}')

    def get_receipt_image(self, u, oid):
        with conn() as db:
            row = db.execute("SELECT image_data, mime_type FROM delivery_receipts WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
            if not row or not row['image_data']:
                order = db.execute("SELECT receipt_photo FROM orders WHERE id=?", (oid,)).fetchone()
                if order and order['receipt_photo']:
                    data_val = order['receipt_photo']
                    mime_val = 'image/jpeg'
                else:
                    self.send_error(404)
                    return
            else:
                data_val = row['image_data']
                mime_val = row['mime_type'] or 'image/jpeg'

        if isinstance(data_val, (bytes, bytearray)):
            content = bytes(data_val)
        else:
            s = str(data_val or '')
            if ',' in s:
                s = s.split(',', 1)[1]
            try:
                content = base64.b64decode(s)
            except Exception:
                self.send_error(404)
                return

        self.send_response(200)
        self.send_header('Content-Type', mime_val)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'private, max-age=86400')
        self.end_headers()
        self.wfile.write(content)

    def get_signature_image(self, u, oid):
        with conn() as db:
            row = db.execute("SELECT digital_signature FROM delivery_receipts WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
            if not row or not row['digital_signature']:
                self.send_error(404)
                return
            data_val = row['digital_signature']

        if isinstance(data_val, (bytes, bytearray)):
            content = bytes(data_val)
        else:
            s = str(data_val or '')
            if ',' in s:
                s = s.split(',', 1)[1]
            try:
                content = base64.b64decode(s)
            except Exception:
                self.send_error(404)
                return

        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'private, max-age=86400')
        self.end_headers()
        self.wfile.write(content)

    def order_detail(self,u,oid):
        with conn() as db:
            r=db.execute('''SELECT o.*,c.name client,c.document,c.phone,c.whatsapp,c.farm_name,c.address,c.reference_point,c.city client_city,u.name seller,
                                   COALESCE(
                                     (SELECT dr.name
                                      FROM route_orders ro
                                      JOIN routes r ON r.id=ro.route_id
                                      JOIN drivers dr ON dr.id=r.driver_id
                                      WHERE ro.order_id=o.id AND r.status <> 'Cancelada'
                                      ORDER BY CASE WHEN r.status IN ('Planejada','Em rota') THEN 0 ELSE 1 END, ro.id DESC
                                      LIMIT 1),
                                     d.name
                                   ) driver,
                                   COALESCE(
                                     (SELECT ve.name
                                      FROM route_orders ro
                                      JOIN routes r ON r.id=ro.route_id
                                      JOIN vehicles ve ON ve.id=r.vehicle_id
                                      WHERE ro.order_id=o.id AND r.status <> 'Cancelada'
                                      ORDER BY CASE WHEN r.status IN ('Planejada','Em rota') THEN 0 ELSE 1 END, ro.id DESC
                                      LIMIT 1),
                                     v.name
                                   ) vehicle,
                                   COALESCE(
                                     (SELECT ve.plate
                                      FROM route_orders ro
                                      JOIN routes r ON r.id=ro.route_id
                                      JOIN vehicles ve ON ve.id=r.vehicle_id
                                      WHERE ro.order_id=o.id AND r.status <> 'Cancelada'
                                      ORDER BY CASE WHEN r.status IN ('Planejada','Em rota') THEN 0 ELSE 1 END, ro.id DESC
                                      LIMIT 1),
                                     v.plate
                                   ) plate
                            FROM orders o
                            LEFT JOIN clients c ON c.id=o.client_id
                            LEFT JOIN users u ON u.id=o.seller_id
                            LEFT JOIN drivers d ON d.id=o.driver_id
                            LEFT JOIN vehicles v ON v.id=o.vehicle_id
                            WHERE o.id=?''',(oid,)).fetchone()
            if not r:
                return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
            items=db.execute('SELECT * FROM order_items WHERE order_id=?',(oid,)).fetchall(); hist=db.execute('SELECT h.*,u.name user FROM order_history h LEFT JOIN users u ON u.id=h.user_id WHERE h.order_id=? ORDER BY h.id ASC',(oid,)).fetchall(); probs=db.execute('SELECT * FROM delivery_problems WHERE order_id=? ORDER BY id DESC',(oid,)).fetchall()
            receipt=db.execute('SELECT * FROM delivery_receipts WHERE order_id=? ORDER BY id DESC LIMIT 1',(oid,)).fetchone()
            route_info=self.order_route_info(db,oid)

        receipt_panel = ''
        r_receipt_photo = r['receipt_photo'] if 'receipt_photo' in r.keys() else None
        r_delivered_to = r['delivered_to'] if 'delivered_to' in r.keys() else None
        r_delivered_doc = r['delivered_document'] if 'delivered_document' in r.keys() else None
        r_delivered_doc_type = r['delivered_document_type'] if 'delivered_document_type' in r.keys() else None
        r_delivered_at = r['delivered_at'] if 'delivered_at' in r.keys() else None
        r_final_notes = r['final_notes'] if 'final_notes' in r.keys() else None

        lat = receipt['latitude'] if (receipt and 'latitude' in receipt.keys() and receipt['latitude']) else (r['delivery_latitude'] if 'delivery_latitude' in r.keys() else None)
        lng = receipt['longitude'] if (receipt and 'longitude' in receipt.keys() and receipt['longitude']) else (r['delivery_longitude'] if 'delivery_longitude' in r.keys() else None)
        loc_link = receipt['delivery_location_link'] if (receipt and 'delivery_location_link' in receipt.keys() and receipt['delivery_location_link']) else (r['delivery_location_link'] if 'delivery_location_link' in r.keys() else None)
        if not loc_link and lat is not None and lng is not None:
            loc_link = f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}"

        has_receipt_photo = bool(receipt and receipt['image_data']) or bool(r_receipt_photo)
        has_signature = bool(receipt and receipt['digital_signature'])
        if has_receipt_photo or has_signature or r_delivered_to or loc_link:
            rec_to = esc(receipt['delivered_to'] if receipt and receipt['delivered_to'] else (r_delivered_to or '—'))
            rec_doc = esc(receipt['delivered_document'] if receipt and receipt['delivered_document'] else (r_delivered_doc or ''))
            rec_doc_type = esc(receipt['delivered_document_type'] if (receipt and 'delivered_document_type' in receipt.keys() and receipt['delivered_document_type']) else (r_delivered_doc_type or 'CPF'))
            rec_date = brdate(receipt['created_at']) if receipt and receipt['created_at'] else brdate(r_delivered_at)
            rec_notes = esc(receipt['notes'] if receipt and receipt['notes'] else (r_final_notes or '—'))

            photo_html = f'''<div style="flex:1; min-width:240px; background:#f8fafc; border:1px solid #cbd5e1; border-radius:8px; padding:12px; text-align:center;">
                <h4 style="margin:0 0 8px 0; font-size:0.9rem; color:#166534; font-weight:700;">📷 Foto do Canhoto / Comprovante</h4>
                <a href="/orders/{oid}/receipt-image" target="_blank" title="Clique para abrir imagem inteira">
                    <img src="/orders/{oid}/receipt-image" alt="Foto do Canhoto" style="max-width:100%; max-height:260px; border-radius:6px; border:2px solid #10b981; object-fit:contain;" onerror="this.parentNode.innerHTML='<span class=\\'muted\\'>Sem foto disponível</span>'">
                </a>
            </div>''' if has_receipt_photo else ''

            sig_html = f'''<div style="flex:1; min-width:240px; background:#f8fafc; border:1px solid #cbd5e1; border-radius:8px; padding:12px; text-align:center;">
                <h4 style="margin:0 0 8px 0; font-size:0.9rem; color:#1e3a8a; font-weight:700;">✍️ Assinatura Digital do Motorista</h4>
                <a href="/orders/{oid}/signature-image" target="_blank" title="Clique para abrir assinatura inteira">
                    <img src="/orders/{oid}/signature-image" alt="Assinatura Digital" style="max-width:100%; max-height:260px; border-radius:6px; border:2px solid #3b82f6; background:#fff; object-fit:contain;" onerror="this.parentNode.innerHTML='<span class=\\'muted\\'>Sem assinatura disponível</span>'">
                </a>
            </div>''' if has_signature else ''

            gps_html = f'''<p><b>Localização GPS</b><a href="{esc(loc_link)}" target="_blank" class="btn small ghost" style="color:#0369a1; border-color:#0369a1; font-weight:700; display:inline-flex; align-items:center; gap:4px;">🌐 Ver no Google Maps ({lat:.4f}, {lng:.4f})</a></p>''' if (loc_link and lat is not None and lng is not None) else (f'''<p><b>Localização GPS</b><a href="{esc(loc_link)}" target="_blank" class="btn small ghost" style="color:#0369a1; border-color:#0369a1; font-weight:700; display:inline-flex; align-items:center; gap:4px;">🌐 Abrir Mapa no Google Maps</a></p>''' if loc_link else '<p><b>Localização GPS</b><span class="muted">Não capturada</span></p>')

            receipt_panel = f'''<section class="panel" style="border-left:5px solid #10b981;">
                <h2 style="color:#065f46;">📷 Comprovante e Assinatura da Entrega (App do Motorista)</h2>
                <div class="info-grid">
                    <p><b>Recebido por</b>{rec_to} {f"({rec_doc_type}: {rec_doc})" if rec_doc else ""}</p>
                    <p><b>Data Registro</b>{rec_date}</p>
                    <p><b>Observação Motorista</b>{rec_notes}</p>
                    {gps_html}
                </div>
                <div style="display:flex; gap:16px; flex-wrap:wrap; margin-top:14px;">
                    {photo_html}
                    {sig_html}
                </div>
            </section>'''

        # --- Linha do tempo visual usando order_history real ---
        flow_steps = [
            ('Venda',           '📝', 'Pedido criado'),
            ('Faturado',        '🧾', 'Faturamento'),
            ('Saiu para entrega','🚚', 'Saiu em rota'),
            ('Acertado',        '✅', 'Acerto finalizado'),
        ]
        # Mapear status → data/hora real do histórico
        status_to_hist = {}
        for h in hist:
            ns = normalize_order_status(str(h['new_status'] or ''))
            if ns and ns not in status_to_hist:
                dt = str(h['created_at'] or '')
                status_to_hist[ns] = {'date': dt[:10], 'time': dt[11:16], 'user': h['user'] or 'Sistema'}
        # Status problemático usa data separada
        if normalize_order_status(r['status']) == 'Problema' and r_delivered_at:
            status_to_hist['Problema'] = {'date': str(r_delivered_at)[:10], 'time': '', 'user': ''}

        current_status = normalize_order_status(r['status'])
        flow_order = ['Venda','Faturado','Saiu para entrega','Acertado']
        current_idx = flow_order.index(current_status) if current_status in flow_order else -1
        is_late = r['expected_delivery_date'] and str(r['expected_delivery_date']) < today() and current_status not in ('Acertado','Problema','Cancelado')

        timeline_steps = ''
        for step_i, (st, icon, label) in enumerate(flow_steps):
            hist_info = status_to_hist.get(st, {})
            date_label = brdate(hist_info.get('date','')) if hist_info.get('date') else '—'
            time_label = hist_info.get('time','') or ''
            user_label = hist_info.get('user','') or ''
            if step_i == current_idx:
                cls = 'late' if is_late else 'active'
            elif hist_info.get('date'):
                cls = 'done'
            else:
                cls = 'future'
            timeline_steps += f'''<div class="timeline-step {cls}">
                <div class="timeline-dot">{icon}</div>
                <div class="timeline-label">{esc(label)}</div>
                <div class="timeline-date">{date_label}{f" {time_label}" if time_label else ""}{f"<br>{esc(user_label)}" if user_label else ""}</div>
            </div>'''

        # Manter histórico completo em lista (preservação de dados)
        hist_rows=''.join(f'<li><b>{esc(h["action"])}</b> · {esc(h["old_status"] or "—")} → {esc(h["new_status"] or "—")}<br><small>{brdate(h["created_at"])} {esc(str(h["created_at"])[11:16])} · {esc(h["user"] or "Sistema")} · {esc(h["notes"] or "")}</small></li>' for h in reversed(hist)) or '<li>Sem histórico.</li>'
        item_rows=''.join(f'<tr><td>{esc(it["product_code"] or "—")}</td><td>{esc(it["product_name"])}</td><td>{esc(it["quantity"])} {esc(it["unit"] or "")}</td><td>{fmt_num(it["weight_kg"])} kg</td><td>{esc(it["notes"] or "")}</td></tr>' for it in items) or '<tr><td colspan="5">Sem itens cadastrados.</td></tr>'
        prob_rows=''.join(f'<li><b>{esc(p["problem_type"])}</b><br><small>{esc(p["description"])} · {brdate(p["created_at"])}</small></li>' for p in probs) or '<li>Nenhum problema registrado.</li>'
        delete_action = f"""<form method='post' action='/orders/{oid}/delete' class='inline-form needs-confirm' data-confirm-text='Confirma apagar este pedido definitivamente?'>
            <button class='danger-btn'>Apagar pedido</button>
        </form>""" if self.has_perm(u,'cancel_orders') else ''
        edit_btn = f'<a class="btn" href="/orders/{oid}/edit">Editar pedido</a>' if self.has_perm(u,'edit_orders') else ''
        actions = f'''<div class="action-strip no-print">{edit_btn}{delete_action}</div>'''
        problem_form=f'''<form method="post" action="/orders/{oid}/problem" class="form compact no-print"><h3>Registrar problema de entrega</h3><div class="grid3"><label>Motivo<select name="problem_type">{option(PROBLEM_TYPES)}</select></label><label class="full">Descrição<textarea name="description" required></textarea></label></div><button class="danger-btn">Registrar problema</button></form>'''
        
        schedule_form = ''
        if self.has_perm(u,'edit_orders') and current_status not in FINAL_ORDER_STATUSES and current_status != 'Agendado':
            schedule_form = f'''<form method="post" action="/orders/{oid}/status" class="form compact no-print"><h3>Agendar Entrega</h3><input type="hidden" name="status" value="Agendado"><div class="grid3"><label class="full">Motivo / Nova data agendada *<textarea name="notes" required placeholder="Ex: Cliente solicitou entrega para DD/MM/AAAA por motivo X..."></textarea></label></div><button class="btn secondary">Agendar entrega</button></form>'''
        elif self.has_perm(u,'edit_orders') and current_status == 'Agendado':
            return_targets = ['Faturado', 'Venda']
            if route_info and normalize_route_status(route_info['route_status']) == 'Em rota':
                return_targets.insert(0, 'Saiu para entrega')
            schedule_form = f'''<section class="panel no-print"><h2>Retornar ao fluxo de entrega</h2><form method="post" action="/orders/{oid}/status" class="form compact"><div class="grid3"><label>Status de destino *<select name="status">{option(return_targets, 'Faturado')}</select></label><label class="full">Observação / Justificativa *<textarea name="notes" required placeholder="Descreva o motivo de retornar o pedido ao fluxo..."></textarea></label></div><button class="btn primary">Retornar ao fluxo</button></form></section>'''

        assisted=self.order_assisted_block(r, route_info, u=u)
        can_view_financial = self.can_view_financial(u)
        value_info = f'<p><b>Valor</b>{money(r["total_value"])}</p>' if can_view_financial else '<p><b>Valor</b>Oculto para este perfil</p>'
        reopen_form=''
        if can_manage_reopen(u) and normalize_order_status(r['status']) in FINAL_ORDER_STATUSES:
            reopen_targets = ['Faturado','Venda','Agendado']
            if route_info and normalize_route_status(route_info['route_status']) == 'Em rota':
                reopen_targets.insert(0,'Saiu para entrega')
            reopen_form = f"""<section class='panel no-print'><h2>Reabertura controlada</h2><form method='post' action='/orders/{oid}/reopen' class='form compact needs-confirm' data-confirm-text='Confirma reabrir este pedido finalizado?'><div class='grid3'><label>Destino<select name='target_status'>{option(reopen_targets,'Faturado')}</select></label><label class='full'>Motivo obrigatório<textarea name='reason' required placeholder='Descreva o motivo operacional da reabertura...'></textarea></label></div><button class='danger-btn'>Reabrir pedido</button></form></section>"""
        content=f'''<section class="order-hero"><div><span>{esc(r['order_number'])}</span><h2>{esc(r['client'] or 'Cliente não informado')}</h2><p>{esc(r['farm_name'] or r['delivery_address'] or '')}</p></div><div>{badge(r['status'])}{deadline_pill(r['expected_delivery_date'],r['status'])}</div></section>{assisted}{actions}{receipt_panel}
        <section class="panel"><h2>Linha do Tempo</h2><div class="order-timeline">{timeline_steps}</div></section>
        <div class="detail-grid"><section class="panel"><h2>Dados do pedido</h2><div class="info-grid"><p><b>Documento fiscal</b>{esc(r['invoice_number'] or 'Pendente')}</p><p><b>Venda</b>{brdate(r['sale_date'])}</p><p><b>Prazo limite</b>{brdate(r['expected_delivery_date'])}</p><p><b>Peso</b>{fmt_num(r['weight_kg'])} kg</p>{value_info}<p><b>Vendedor</b>{esc(r['seller_name'] or 'Não informado')}</p><p><b>Cadastrante</b>{esc(r['seller'] or '—')}</p></div></section><section class="panel"><h2>Entrega</h2><div class="info-grid"><p><b>Cidade</b>{esc(r['city'] or r['client_city'] or '—')}</p><p><b>Rota</b>{esc(r['route_name'] or '—')}</p><p><b>Motorista</b>{esc(r['driver'] or '—')}</p><p><b>Veículo</b>{esc((r['vehicle'] or '')+' '+(r['plate'] or ''))}</p><p><b>Recebido por</b>{esc(r['delivered_to'] or '—')}</p><p><b>Entrega em</b>{brdate(r['delivered_at'])}</p></div></section></div><section class="panel"><h2>Itens</h2><table><thead><tr><th>Código</th><th>Produto</th><th>Qtd</th><th>Peso</th><th>Obs.</th></tr></thead><tbody>{item_rows}</tbody></table></section><div class="detail-grid"><section class="panel"><h2>Histórico completo</h2><ul class="timeline-list">{hist_rows}</ul></section><section class="panel"><h2>Problemas registrados</h2><ul class="problems">{prob_rows}</ul></section></div>{problem_form}{schedule_form}{reopen_form}'''
        return self.send_html(layout(u,f'Pedido {r["order_number"]}',content,'Acompanhamento completo do pedido'))


    def post_status(self,u,oid):
        d=self.post_data(); notes=(d.get('notes') or '').strip()
        with conn() as db:
            row=db.execute('SELECT status,delivered_at FROM orders WHERE id=?',(oid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
            old=normalize_order_status(row['status'])
            new,old=self.ensure_order_status(db,oid,d.get('status'),notes)
            delivered_at = row['delivered_at']
            if new in ('Acertado','Problema') and not delivered_at:
                delivered_at = today()
            if new=='Problema':
                db.execute('INSERT INTO delivery_problems(order_id,problem_type,description,created_at) VALUES(?,?,?,?)',(oid,'Outro motivo',notes,now()))
                db.execute("UPDATE route_orders SET status='Com problema' WHERE order_id=?",(oid,))
            elif new=='Saiu para entrega':
                db.execute("UPDATE route_orders SET status='Em rota' WHERE order_id=?",(oid,))
            elif new in ('Venda','Faturado','Agendado'):
                db.execute("UPDATE route_orders SET status='Pendente' WHERE order_id=? AND status<>'Entregue'",(oid,))
            elif new=='Acertado':
                db.execute("UPDATE route_orders SET status='Entregue' WHERE order_id=?",(oid,))
            elif new=='Cancelado':
                db.execute("UPDATE route_orders SET status='Cancelado' WHERE order_id=?",(oid,))
            db.execute('UPDATE orders SET status=?,delivered_at=COALESCE(?,delivered_at),updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(new,delivered_at,now(),oid))
            add_hist(db,oid,u['id'],old,new,'Status alterado',notes)
            audit(db,u,'Alterou status','Pedidos',str(oid),old,new,notes)
            db.commit()
        self.redirect(f'/orders/{oid}')
    def post_invoice(self,u,oid):
        d=self.post_data(); nf=d.get('invoice_number','').strip()
        if not nf:
            raise ValueError('Informe o número da nota fiscal.')
        inv_date = validate_date_field(d.get('invoiced_at') or today(),'a data de faturamento',required=True)
        with conn() as db:
            row=db.execute('SELECT status,invoice_number FROM orders WHERE id=?',(oid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
            old_status = normalize_order_status(row['status'])
            if old_status == 'Faturado':
                return self.fail(u,'Operação bloqueada','Pedido já está faturado. Use editar pedido para ajustar dados, sem refaturar.',400)
            if old_status not in ('Venda', 'Agendado'):
                return self.fail(u,'Operação bloqueada',f'Pedido em status "{old_status}" não pode ser faturado diretamente.',400)
            self.ensure_unique_invoice(db, nf, exclude_order_id=oid)
            # Atualização atômica para impedir faturamento concorrente do mesmo pedido.
            upd = db.execute(
                "UPDATE orders SET invoice_number=?,invoiced_at=?,status='Faturado',updated_at=?,version=COALESCE(version,1)+1 WHERE id=? AND status IN ('Venda', 'Agendado')",
                (nf, inv_date, now(), oid),
            )
            if int(upd.rowcount or 0) != 1:
                current = db.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()
                current_status = normalize_order_status(current['status']) if current else 'indisponível'
                return self.fail(
                    u,
                    'Operação bloqueada',
                    f'Este pedido já mudou de status para "{current_status}" e não pode ser faturado novamente agora.',
                    409,
                )
            add_hist(db,oid,u['id'],'Venda','Faturado','Pedido faturado',f'NF {nf}')
            audit(db,u,'Faturou pedido','Faturamento',str(oid),'Venda','Faturado',nf)
            db.commit()
        if self.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in (self.headers.get('Accept') or ''):
            self.send_response(200)
            self._common_headers()
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write("OK".encode('utf-8'))
            return
        redirect_to = d.get('redirect', '').strip() or f'/orders/{oid}'
        self.redirect(redirect_to)
    def post_deliver(self,u,oid):
        d=self.post_data()
        delivered_date = validate_date_field(d.get('delivered_at'),'a data de entrega',required=True)
        with conn() as db:
            row=db.execute('SELECT status,sale_date FROM orders WHERE id=?',(oid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
            if row['sale_date'] and delivered_date < str(row['sale_date'])[:10]:
                raise ValueError('Data de entrega não pode ser anterior à data de venda.')
            _,old=self.ensure_order_status(db,oid,'Acertado',d.get('final_notes'))
            db.execute('UPDATE orders SET status=?,delivered_to=?,delivered_document=?,delivered_at=?,final_notes=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',('Acertado',d.get('delivered_to') or 'Não informado',d.get('delivered_document'),delivered_date,d.get('final_notes'),now(),oid))
            db.execute("UPDATE route_orders SET status='Entregue' WHERE order_id=?",(oid,))
            add_hist(db,oid,u['id'],old,'Acertado','Entrega acertada',d.get('final_notes'))
            audit(db,u,'Concluiu entrega','Entregas',str(oid),old,'Acertado')
            db.commit()
        self.redirect(f'/orders/{oid}')
    def post_problem(self,u,oid):
        d=self.post_data()
        ptype=(d.get('problem_type') or 'Outro motivo').strip()
        desc=(d.get('description') or '').strip()
        if not desc:
            raise ValueError('Descreva o problema da entrega.')
        with conn() as db:
            row=db.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
            _,old=self.ensure_order_status(db,oid,'Problema',desc)
            db.execute('INSERT INTO delivery_problems(order_id,problem_type,description,created_at) VALUES(?,?,?,?)',(oid,ptype,desc,now()))
            db.execute('UPDATE orders SET status=?,delivered_at=COALESCE(NULLIF(delivered_at,""),?),updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',('Problema',today(),now(),oid))
            db.execute("UPDATE route_orders SET status='Com problema' WHERE order_id=?",(oid,))
            add_hist(db,oid,u['id'],old,'Problema','Problema registrado',desc)
            audit(db,u,'Registrou problema','Entregas',str(oid),old,'Problema',ptype)
            db.commit()
        self.redirect(f'/orders/{oid}')

    def post_order_reopen(self, u, oid):
        if not can_manage_reopen(u):
            return self.fail(u,'Acesso negado','Somente GOD/Admin pode reabrir pedido finalizado.',403)
        d=self.post_data()
        reason=(d.get('reason') or '').strip()
        target=(d.get('target_status') or 'Faturado').strip()
        if not reason:
            raise ValueError('Informe o motivo da reabertura do pedido.')
        with conn() as db:
            row=db.execute('SELECT id,order_number,status FROM orders WHERE id=?',(oid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
            old=normalize_order_status(row['status'])
            if old not in FINAL_ORDER_STATUSES:
                return self.fail(u,'Operação inválida','Somente pedidos finalizados podem ser reabertos.',400)
            new,_=self.ensure_order_status(db,oid,target,reason,allow_reopen=True)
            if new in FINAL_ORDER_STATUSES:
                return self.fail(u,'Operação inválida','Destino da reabertura deve ser etapa operacional aberta.',400)
            db.execute("""UPDATE orders
                          SET status=?,delivered_at=NULL,updated_at=?,final_notes=COALESCE(final_notes,'') || ?,version=COALESCE(version,1)+1
                          WHERE id=?""",(new,now(),f' | Reaberto em {now()} por {u["name"]}: {reason}',oid))
            if new == 'Saiu para entrega':
                db.execute("UPDATE route_orders SET status='Em rota' WHERE order_id=?",(oid,))
            else:
                db.execute("UPDATE route_orders SET status='Pendente' WHERE order_id=? AND status<>'Entregue'",(oid,))
            add_hist(db,oid,u['id'],old,new,'Reabertura controlada',reason)
            audit(db,u,'Reabriu pedido','Pedidos',row['order_number'],old,new,reason)
            db.commit()
        self.redirect(f'/orders/{oid}')

    def post_order_delete(self, u, oid):
        with conn() as db:
            row=db.execute('SELECT id,order_number,status FROM orders WHERE id=?',(oid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
            order_status=normalize_order_status(row['status'])
            if order_status not in ('Venda','Cancelado'):
                return self.fail(
                    u,
                    'Exclusão bloqueada',
                    f'Pedido em status "{order_status}" não pode ser apagado. Cancele o pedido e remova vínculos antes de apagar.',
                    400,
                )
            active_route_link=db.execute("""SELECT COUNT(*) c
                                            FROM route_orders ro
                                            JOIN routes r ON r.id=ro.route_id
                                            WHERE ro.order_id=? AND r.status IN ('Planejada','Em rota')""",(oid,)).fetchone()['c']
            if active_route_link:
                return self.fail(
                    u,
                    'Exclusão bloqueada',
                    'Este pedido está vinculado a carga ativa e não pode ser apagado.',
                    400,
                )
            any_route_link=db.execute('SELECT COUNT(*) c FROM route_orders WHERE order_id=?',(oid,)).fetchone()['c']
            if any_route_link:
                return self.fail(
                    u,
                    'Exclusão bloqueada',
                    'Este pedido possui histórico de carga e não pode ser apagado. Mantenha-o no histórico para auditoria.',
                    400,
                )
            db.execute('DELETE FROM orders WHERE id=?',(oid,))
            audit(db,u,'Apagou pedido','Pedidos',row['order_number'],order_status,'')
            db.commit()
        self.redirect('/orders')

    def post_route_reopen(self, u, rid):
        if not can_manage_reopen(u):
            return self.fail(u,'Acesso negado','Somente GOD/Admin pode reabrir carga finalizada.',403)
        d=self.post_data()
        reason=(d.get('reason') or '').strip()
        target=(d.get('target_status') or 'Planejada').strip()
        if not reason:
            raise ValueError('Informe o motivo da reabertura da carga.')
        with conn() as db:
            route=find_route_by_id(db, rid)
            if not route:
                return self.fail(u,'Não encontrado','Carga não encontrada.',404)
            old_route=normalize_route_status(route['status'])
            if old_route not in FINAL_ROUTE_STATUSES:
                return self.fail(u,'Operação inválida','Somente cargas finalizadas podem ser reabertas.',400)
            new_route,_=self.ensure_route_status(db,rid,target,allow_reopen=True)
            if new_route in FINAL_ROUTE_STATUSES:
                return self.fail(u,'Operação inválida','Destino da reabertura deve ser Planejada ou Em rota.',400)
            db.execute("""UPDATE routes
                          SET status=?,notes=COALESCE(notes,'') || ?,updated_at=?,version=COALESCE(version,1)+1
                          WHERE id=?""",(new_route,f' | Reaberta em {now()} por {u["name"]}: {reason}',now(),rid))
            ros=db.execute("""SELECT ro.id route_order_id,o.id order_id,o.status,o.order_number
                              FROM route_orders ro
                              JOIN orders o ON o.id=ro.order_id
                              WHERE ro.route_id=?""",(rid,)).fetchall()
            target_order_status = 'Saiu para entrega' if new_route == 'Em rota' else 'Faturado'
            target_ro_status = 'Em rota' if new_route == 'Em rota' else 'Pendente'
            for ro in ros:
                old_order=normalize_order_status(ro['status'])
                new_order,_=self.ensure_order_status(db,ro['order_id'],target_order_status,reason,allow_reopen=True)
                db.execute("""UPDATE orders
                              SET status=?,delivered_at=NULL,updated_at=?,final_notes=COALESCE(final_notes,'') || ?,version=COALESCE(version,1)+1
                              WHERE id=?""",(new_order,now(),f' | Reaberto via carga {route["name"]}: {reason}',ro['order_id']))
                db.execute("UPDATE route_orders SET status=? WHERE id=?",(target_ro_status,ro['route_order_id']))
                if old_order != new_order:
                    add_hist(db,ro['order_id'],u['id'],old_order,new_order,'Reabertura controlada da carga',reason)
            audit(db,u,'Reabriu carga','Rotas',route['name'],old_route,new_route,reason)
            db.commit()
        self.redirect(f'/routes/{rid}')

    def post_route_delete(self, u, rid):
        with conn() as db:
            route=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            if not route:
                return self.fail(u,'Não encontrado','Carga não encontrada.',404)
            route_status=normalize_route_status(route['status'])
            if route_status in ('Em rota','Acertada','Com problema'):
                return self.fail(
                    u,
                    'Exclusão bloqueada',
                    f'Carga em status "{route_status}" não pode ser apagada. Use cancelar/reabertura conforme necessário.',
                    400,
                )
            ros=db.execute("""SELECT ro.order_id,o.order_number,o.status
                              FROM route_orders ro
                              JOIN orders o ON o.id=ro.order_id
                              WHERE ro.route_id=?""",(rid,)).fetchall()
            for ro in ros:
                old_order=normalize_order_status(ro['status'])
                if old_order in FINAL_ORDER_STATUSES:
                    return self.fail(
                        u,
                        'Exclusão bloqueada',
                        f'A carga possui pedido finalizado ({ro["order_number"]}) e não pode ser apagada.',
                        400,
                    )
            for ro in ros:
                old_order=normalize_order_status(ro['status'])
                if old_order == 'Saiu para entrega':
                    self.ensure_order_status(db,ro['order_id'],'Faturado')
                    db.execute("UPDATE orders SET status='Faturado',updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",(now(),ro['order_id']))
                    add_hist(db,ro['order_id'],u['id'],old_order,'Faturado','Carga apagada: pedido retornou para faturado',route['name'])
            db.execute('DELETE FROM routes WHERE id=?',(rid,))
            audit(db,u,'Apagou carga','Rotas',route['name'],route_status,'')
            db.commit()
        self.redirect('/routes')

    def post_route_cancel(self, u, rid):
        d=self.post_data()
        reason=(d.get('reason') or '').strip()
        if not reason:
            raise ValueError('Informe o motivo do cancelamento da carga.')
        with conn() as db:
            route=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            if not route:
                return self.fail(u,'Não encontrado','Carga não encontrada.',404)
            old_route=normalize_route_status(route['status'])
            if old_route in FINAL_ROUTE_STATUSES or old_route == 'Cancelada':
                return self.fail(u,'Operação inválida','Esta carga já está finalizada/cancelada.',400)
            rows=db.execute("""SELECT ro.id route_order_id, ro.order_id, o.order_number, o.status
                               FROM route_orders ro
                               JOIN orders o ON o.id=ro.order_id
                               WHERE ro.route_id=?""",(rid,)).fetchall()
            db.execute("""UPDATE routes
                          SET status='Cancelada',
                              notes=COALESCE(notes,'') || ?,
                              updated_at=?,
                              version=COALESCE(version,1)+1
                          WHERE id=?""",(f' | Cancelada em {now()} por {u["name"]}: {reason}',now(),rid))
            for ro in rows:
                old_order=normalize_order_status(ro['status'])
                db.execute("UPDATE route_orders SET status='Cancelado' WHERE id=?",(ro['route_order_id'],))
                if old_order not in FINAL_ORDER_STATUSES:
                    self.ensure_order_status(db,ro['order_id'],'Faturado')
                    db.execute("UPDATE orders SET status='Faturado',updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",(now(),ro['order_id']))
                    add_hist(db,ro['order_id'],u['id'],old_order,'Faturado','Carga cancelada: pedido retornou para faturado',reason)
            self.recalc_route_weight(db,rid)
            audit(db,u,'Cancelou carga','Rotas',route['name'],old_route,'Cancelada',reason)
            db.commit()
        self.redirect(f'/routes/{rid}')

    def faturamento(self,u):
        if _ERP_AVAILABLE:
            try:
                with conn() as db:
                    venda_rows = db.execute("SELECT id, order_number FROM orders WHERE status='Venda'").fetchall()
                    for vr in venda_rows:
                        ono = vr['order_number']
                        raw_erp = _erp_connector.lookup_order(ono)
                        if raw_erp:
                            mapped_erp = _erp_mapper.map_erp_to_logistica(raw_erp)
                            if mapped_erp.get('is_invoiced') and mapped_erp.get('invoice_number'):
                                inv_no = str(mapped_erp.get('invoice_number')).strip()
                                inv_dt = mapped_erp.get('invoiced_at') or mapped_erp.get('sale_date') or today()
                                db.execute("UPDATE orders SET status='Faturado', invoice_number=?, invoiced_at=?, updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (inv_no, inv_dt, now(), vr['id']))
                                add_hist(db, vr['id'], u['id'], 'Venda', 'Faturado', 'Faturamento automático detectado via Oracle ERP', f'NF Nº {inv_no}')
                    db.commit()
            except Exception as _e:
                log_server_error('FATURAMENTO_ERP_SYNC', _e)

        with conn() as db:
            rows=db.execute("""SELECT o.*,c.name client,c.farm_name,c.city client_city
                               FROM orders o
                               LEFT JOIN clients c ON c.id=o.client_id
                               WHERE o.status='Venda'
                               ORDER BY CASE WHEN o.expected_delivery_date<? THEN 0 ELSE 1 END,
                               o.expected_delivery_date, o.id DESC""",(today(),)).fetchall()
            late_count=db.execute("""SELECT COUNT(*) c
                                     FROM orders
                                     WHERE status='Venda'
                                     AND expected_delivery_date<?""",(today(),)).fetchone()['c']
        if rows:
            quick=''.join(f'''<form method="post" action="/orders/{r['id']}/invoice" class="invoice-card clean-row">
                <input type="hidden" name="redirect" value="/faturamento">
                <div class="invoice-main">
                    <div class="invoice-order-block"><span class="label">Pedido</span><b>{esc(r['order_number'])}</b>{badge(r['status'])}</div>
                    <div class="invoice-client-block"><span class="label">Cliente</span><b>{esc(r['client'] or 'Cliente não informado')}</b><small>Vendedor: {esc(r['seller_name'] or 'Não informado')} · {esc(r['city'] or r['client_city'] or 'Cidade não informada')} · {fmt_num(r['weight_kg'])} kg</small></div>
                    <div class="invoice-sla-block"><span class="label">SLA</span>{deadline_pill(r['expected_delivery_date'],r['status'])}<small>Prazo limite: {brdate(r['expected_delivery_date'])}</small></div>
                </div>
                <div class="invoice-action-grid">
                    <label class="inv-input"><span>Nota fiscal</span><input name="invoice_number" placeholder="Número da NF" required autocomplete="off"></label>
                    <label class="inv-input"><span>Data faturamento</span><input type="date" name="invoiced_at" value="{today()}" required></label>
                    <div class="inv-actions">
                        <button type="submit" class="btn primary small btn-faturar-proximo">Faturar e Próximo</button>
                        <button type="button" class="btn small ghost btn-proximo-pedido">Pular / Próximo</button>
                        <a class="btn small ghost" href="/orders/{r['id']}">Abrir</a>
                    </div>
                </div>
            </form>''' for r in rows)
            content=f'''<section class="panel priority-panel clean-panel"><div class="section-title"><div><h2>Fila de NF por prioridade</h2><p>Ordem automática: atrasados primeiro, depois prazo mais próximo.</p></div><span class="badge blue">{len(rows)} pendentes</span></div><div class="alert {'danger' if late_count else 'info'}">{late_count} pedido(s) desta fila já estão fora do SLA.</div><div class="invoice-list">{quick}</div></section>'''
        else:
            content='<section class="panel clean-panel"><h2>Fila de NF por prioridade</h2><div class="empty">Nenhum pedido aguardando faturamento.</div></section>'
        return self.send_html(layout(u,'Faturamento',content,'O que precisa virar NF primeiro'))

    def expedicao(self,u):
        with conn() as db:
            rows=db.execute("""SELECT o.*,c.name client,c.farm_name,c.city client_city FROM orders o LEFT JOIN clients c ON c.id=o.client_id WHERE o.status='Faturado' ORDER BY o.expected_delivery_date,o.id DESC""").fetchall()
        lanes=[]
        for st,next_st,label in [('Faturado','Faturado','Aguardando saída em carga')]:
            lane=''.join(f'''<article class="kanban-card"><div>{badge(r['status'])}</div><h3>{esc(r['order_number'])}</h3><p>{esc(r['client'] or '')}<br><small>Vendedor: {esc(r['seller_name'] or 'Não informado')} · {esc(r['city'] or r['client_city'] or '')} · {fmt_num(r['weight_kg'])} kg · {deadline_pill(r['expected_delivery_date'],r['status'])}</small></p><div class="row-actions"><a class="btn small ghost" href="/orders/{r['id']}">Abrir</a><a class="btn small" href="/routes/new">{label}</a></div></article>''' for r in rows if r['status']==st)
            lanes.append(f'<section class="kanban-lane"><h2>{esc(st)}</h2>{lane or "<div class=empty>Fila vazia</div>"}</section>')
        return self.send_html(layout(u,'Expedição / Separação','<div class="kanban">'+''.join(lanes)+'</div>','Painel visual de faturados aguardando saída em carga'))

    def validate_phone(self, value, field='telefone'):
        raw = (value or '').strip()
        if not raw:
            return ''
        digits = re.sub(r'\D', '', raw)
        if len(digits) < 8:
            raise ValueError(f'Informe um {field} válido com DDD.')
        return raw

    def client_exists(self, db, name, city='', farm='', exclude_id=None):
        key = normalized_text_key(name)
        if not key:
            return None
        rows = db.execute('SELECT id,name,city,farm_name,active,customer_code FROM clients').fetchall()
        for r in rows:
            if exclude_id and int(r['id']) == int(exclude_id):
                continue
            if normalized_text_key(r['name']) == key:
                return r
        return None

    def ensure_unique_customer_code(self, db, customer_code, exclude_id=None):
        code = upper_text(customer_code)
        if not code:
            raise ValueError('Informe o Código do Cliente.')
        if exclude_id:
            dup = db.execute('SELECT id,name FROM clients WHERE UPPER(COALESCE(customer_code,""))=? AND id<>? LIMIT 1',(code,exclude_id)).fetchone()
        else:
            dup = db.execute('SELECT id,name FROM clients WHERE UPPER(COALESCE(customer_code,""))=? LIMIT 1',(code,)).fetchone()
        if dup:
            raise ValueError(f'Código do Cliente duplicado: "{code}" já está em uso por {dup["name"]}.')
        return code

    def get_client_duplicate_check(self, u):
        if not (self.has_perm(u, 'manage_clients') or self.has_perm(u, 'create_orders')):
            return self.send_json({'ok': False, 'message': 'Sem permissão para validar clientes.'}, 403)
        qs = parse_qs(urlparse(self.path).query)
        customer_code = upper_text(qs.get('customer_code', [''])[0])
        name = upper_text(qs.get('name', [''])[0])
        exclude_id = parse_int(qs.get('exclude_id', [''])[0], 0)
        with conn() as db:
            code_dup = None
            name_dup = None
            if customer_code:
                if exclude_id:
                    code_dup = db.execute(
                        'SELECT id,name,active,customer_code FROM clients WHERE UPPER(COALESCE(customer_code,""))=? AND id<>? LIMIT 1',
                        (customer_code, exclude_id),
                    ).fetchone()
                else:
                    code_dup = db.execute(
                        'SELECT id,name,active,customer_code FROM clients WHERE UPPER(COALESCE(customer_code,""))=? LIMIT 1',
                        (customer_code,),
                    ).fetchone()
            if name:
                name_dup = self.client_exists(db, name, exclude_id=exclude_id or None)

        return self.send_json(
            {
                'ok': True,
                'code_exists': bool(code_dup),
                'name_exists': bool(name_dup),
                'code_owner': client_display_name(code_dup['customer_code'], code_dup['name']) if code_dup else '',
                'name_owner': client_display_name(name_dup['customer_code'], name_dup['name']) if name_dup else '',
            },
            200,
        )

    def clients(self,u):
        qs=parse_qs(urlparse(self.path).query); q=qs.get('q',[''])[0].strip(); edit_id=qs.get('edit',[''])[0].strip(); sql='SELECT c.*,COUNT(o.id) orders_count,COALESCE(SUM(o.total_value),0) total_value FROM clients c LEFT JOIN orders o ON o.client_id=c.id WHERE 1=1'; p=[]
        if q:
            like=f'%{q}%'; sql+=' AND (c.customer_code LIKE ? OR c.name LIKE ? OR c.farm_name LIKE ? OR c.city LIKE ? OR c.phone LIKE ? OR c.route_name LIKE ?)'; p=[like]*6
        sql+=' GROUP BY c.id ORDER BY c.active DESC,c.name'
        with conn() as db:
            rows=db.execute(sql,p).fetchall()
            edit_row=db.execute('SELECT * FROM clients WHERE id=?',(parse_int(edit_id),)).fetchone() if edit_id.isdigit() else None
            route_catalog=db.execute('SELECT DISTINCT route_name FROM route_cities WHERE route_name IS NOT NULL AND route_name<>"" ORDER BY route_name').fetchall()
            city_catalog=db.execute('SELECT DISTINCT city FROM route_cities WHERE city IS NOT NULL AND city<>"" ORDER BY city').fetchall()
        city_suggestions = datalist_options([r['city'] for r in rows] + [r['city'] for r in city_catalog])
        route_suggestions = datalist_options([r['route_name'] for r in rows] + [r['route_name'] for r in route_catalog])
        active_rows = [r for r in rows if int(r['active'] or 0) == 1]
        inactive_rows = [r for r in rows if int(r['active'] or 0) != 1]
        can_manage = self.has_perm(u,'manage_clients')
        can_delete_catalog = can_manage and can_manage_catalog_deletions(u)
        can_view_financial = self.can_view_financial(u)
        form=f'''<form class="filters"><input name="q" placeholder="Buscar código, nome, fazenda, cidade, telefone" value="{esc(q)}"><button>Buscar</button></form>'''
        if can_manage:
            form += f'''<form method="post" action="/clients" class="form compact" data-client-dup-check="1" data-client-dup-endpoint="/api/clients/duplicate-check"><h2>Novo cliente</h2><div class="grid3"><label>Código do Cliente<input name="customer_code" data-force-uppercase required placeholder="Ex: 1254"></label><label>Nome<input name="name" data-force-uppercase required></label><div class="full"><div class="alert danger client-dup-warning" data-client-dup-warning hidden>Cliente duplicado.</div></div><label>Telefone<input name="phone" data-force-uppercase></label><label>WhatsApp<input name="whatsapp" data-force-uppercase></label><label>Fazenda<input name="farm_name" data-force-uppercase></label><label>Cidade<input name="city" list="citySuggestions" placeholder="Selecione ou digite"></label><label>Rota<input name="route_name" list="routeSuggestions" placeholder="Selecione ou digite"></label><label class="full">Endereço<textarea name="address" data-force-uppercase></textarea></label></div><button>Adicionar cliente</button></form>'''
        edit_panel=''
        if can_manage and edit_row:
            edit_panel=f'''<section class="panel"><h2>Editar cliente</h2><form method="post" action="/clients/{edit_row["id"]}/update" class="form compact" data-client-dup-check="1" data-client-dup-endpoint="/api/clients/duplicate-check" data-client-dup-exclude-id="{edit_row["id"]}"><div class="grid3"><label>Código do Cliente<input name="customer_code" data-force-uppercase required value="{esc(edit_row["customer_code"] or "")}"></label><label>Nome<input name="name" data-force-uppercase required value="{esc(edit_row["name"])}"></label><div class="full"><div class="alert danger client-dup-warning" data-client-dup-warning hidden>Cliente duplicado.</div></div><label>Telefone<input name="phone" data-force-uppercase value="{esc(edit_row["phone"] or "")}"></label><label>WhatsApp<input name="whatsapp" data-force-uppercase value="{esc(edit_row["whatsapp"] or "")}"></label><label>Fazenda<input name="farm_name" data-force-uppercase value="{esc(edit_row["farm_name"] or "")}"></label><label>Cidade<input name="city" list="citySuggestions" value="{esc(edit_row["city"] or "")}"></label><label>Rota<input name="route_name" list="routeSuggestions" value="{esc(edit_row["route_name"] or "")}"></label><label class="full">Endereço<textarea name="address" data-force-uppercase>{esc(edit_row["address"] or "")}</textarea></label></div><button>Salvar alterações</button><a class="btn ghost" href="/clients">Cancelar edição</a></form></section>'''
        def render_rows(source_rows):
            body_rows=''
            for r in source_rows:
                if can_manage:
                    actions = f'''<a class="btn small ghost" href="/clients?edit={r["id"]}">Editar</a>'''
                    if can_delete_catalog:
                        toggle_label = 'Inativar' if int(r['active'] or 0) == 1 else 'Reativar'
                        toggle_class = 'danger-btn small' if int(r['active'] or 0) == 1 else 'small'
                        actions += f'''
                                  <form method="post" action="/clients/{r["id"]}/toggle" class="inline-mini needs-confirm" data-confirm-text="Confirma {toggle_label.lower()} este cliente?">
                                    <button class="{toggle_class}">{toggle_label}</button>
                                  </form>
                                  <form method="post" action="/clients/{r["id"]}/delete" class="inline-mini needs-confirm" data-confirm-text="Confirma apagar este cliente definitivamente?">
                                    <button class="danger-btn small">Apagar</button>
                                  </form>'''
                else:
                    actions = '<span class="muted">Somente visualização</span>'
                client_label = client_display_name(r['customer_code'], r['name'])
                value_col = money_visible(can_view_financial, r["total_value"])
                body_rows += f'<tr><td><b>{esc(client_label)}</b><br><small>{esc(r["document"] or "")}</small></td><td>{esc(r["farm_name"] or "—")}</td><td>{esc(r["city"] or "—")}</td><td>{esc(r["phone"] or r["whatsapp"] or "—")}</td><td>{esc(r["route_name"] or "—")}</td><td>{r["orders_count"]}</td><td>{value_col}</td><td>{"Ativo" if r["active"] else "Inativo"}</td><td>{actions}</td></tr>'
            return body_rows or '<tr><td colspan="9">Nenhum cliente encontrado.</td></tr>'
        body = render_rows(active_rows)
        inactive_body = render_rows(inactive_rows)
        inactive_panel = ''
        if inactive_rows:
            inactive_panel = f'''<details class="route-section route-archive"><summary><div><h2>Histórico de clientes inativos</h2><p>Cadastros antigos ou bloqueados para novos pedidos.</p></div><span class="badge neutral">{len(inactive_rows)} inativo(s)</span></summary><div class="table-wrap"><table><thead><tr><th>Cliente</th><th>Fazenda</th><th>Cidade</th><th>Contato</th><th>Rota</th><th>Pedidos</th><th>Valor histórico</th><th>Status</th><th>Ações</th></tr></thead><tbody>{inactive_body}</tbody></table></div></details>'''
        datalists = f'<datalist id="citySuggestions">{city_suggestions}</datalist><datalist id="routeSuggestions">{route_suggestions}</datalist>'
        return self.send_html(layout(u,'Clientes',form+edit_panel+datalists+f'<div class="table-wrap"><table><thead><tr><th>Cliente</th><th>Fazenda</th><th>Cidade</th><th>Contato</th><th>Rota</th><th>Pedidos</th><th>Valor histórico</th><th>Status</th><th>Ações</th></tr></thead><tbody>{body}</tbody></table></div>{inactive_panel}','Base de clientes e histórico logístico'))
    def post_client(self,u):
        d=self.post_data()
        customer_code=upper_text(d.get('customer_code'))
        name=upper_text(d.get('name'))
        if not name:
            raise ValueError('Informe o nome do cliente.')
        phone=upper_text(self.validate_phone(d.get('phone'),'telefone'))
        whatsapp=upper_text(self.validate_phone(d.get('whatsapp'),'WhatsApp'))
        city=upper_text(d.get('city'))
        farm=upper_text(d.get('farm_name'))
        document=upper_text(d.get('document'))
        address=upper_text(d.get('address'))
        route_name=upper_text(d.get('route_name'))
        with conn() as db:
            dup=self.client_exists(db,name,city,farm)
            if dup:
                status='ativo' if int(dup['active'] or 0) == 1 else 'inativo'
                raise ValueError(f'Nome de cliente duplicado: "{dup["name"]}" ({status}) já está cadastrado.')
            if not customer_code:
                customer_code = str(db.execute('SELECT COALESCE(MAX(id),0)+1 n FROM clients').fetchone()['n'])
            code=self.ensure_unique_customer_code(db, customer_code, exclude_id=None)
            db.execute('INSERT INTO clients(customer_code,name,document,phone,whatsapp,city,farm_name,address,route_name,active,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,1)',(code,name,document,phone,whatsapp,city,farm,address,route_name,now(),now())); audit(db,u,'Criou cliente','Clientes',client_display_name(code, name)); db.commit()
        self.redirect('/clients')
    def post_client_update(self,u,cid):
        d=self.post_data()
        customer_code=upper_text(d.get('customer_code'))
        name=upper_text(d.get('name'))
        if not name:
            raise ValueError('Informe o nome do cliente.')
        phone=upper_text(self.validate_phone(d.get('phone'),'telefone'))
        whatsapp=upper_text(self.validate_phone(d.get('whatsapp'),'WhatsApp'))
        city=upper_text(d.get('city'))
        farm=upper_text(d.get('farm_name'))
        address=upper_text(d.get('address'))
        route_name=upper_text(d.get('route_name'))
        with conn() as db:
            row=db.execute('SELECT * FROM clients WHERE id=?',(cid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Cliente não encontrado.',404)
            dup=self.client_exists(db,name,city,farm,exclude_id=cid)
            if dup:
                status='ativo' if int(dup['active'] or 0) == 1 else 'inativo'
                raise ValueError(f'Nome de cliente duplicado: "{dup["name"]}" ({status}) já está cadastrado.')
            if not customer_code:
                customer_code = str(row['customer_code'] or row['id'])
            code=self.ensure_unique_customer_code(db, customer_code, exclude_id=cid)
            db.execute('UPDATE clients SET customer_code=?,name=?,phone=?,whatsapp=?,city=?,farm_name=?,address=?,route_name=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(code,name,phone,whatsapp,city,farm,address,route_name,now(),cid))
            audit(db,u,'Editou cliente','Clientes',client_display_name(row['customer_code'], row['name']),client_display_name(row['customer_code'], row['name']),client_display_name(code, name))
            db.commit()
        self.redirect('/clients')
    def post_client_toggle(self,u,cid):
        if not can_manage_catalog_deletions(u):
            return self.fail(u,'Ação bloqueada','Seu perfil pode cadastrar e editar clientes, mas não pode inativar/reativar.',403)
        with conn() as db:
            row=db.execute('SELECT id,name,active FROM clients WHERE id=?',(cid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Cliente não encontrado.',404)
            target = 0 if int(row['active'] or 0) == 1 else 1
            if target == 0:
                open_orders=db.execute("""SELECT COUNT(*) c FROM orders
                                          WHERE client_id=? AND status NOT IN ('Acertado','Problema','Cancelado')""",(cid,)).fetchone()['c']
                if open_orders:
                    return self.fail(u,'Inativação bloqueada',f'Este cliente possui {open_orders} pedido(s) em andamento. Finalize/cancele os pedidos antes de inativar.',400)
            db.execute('UPDATE clients SET active=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(target,now(),cid))
            audit(db,u,'Alterou status de cliente','Clientes',row['name'],str(row['active']),str(target))
            db.commit()
        self.redirect('/clients')

    def post_client_delete(self,u,cid):
        if not can_manage_catalog_deletions(u):
            return self.fail(u,'Ação bloqueada','Seu perfil pode cadastrar e editar clientes, mas não pode excluir.',403)
        with conn() as db:
            row=db.execute('SELECT id,name FROM clients WHERE id=?',(cid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Cliente não encontrado.',404)
            linked_orders=db.execute('SELECT COUNT(*) c FROM orders WHERE client_id=?',(cid,)).fetchone()['c']
            if linked_orders:
                return self.fail(
                    u,
                    'Exclusão bloqueada',
                    f'Este cliente possui {linked_orders} pedido(s) vinculado(s) e não pode ser apagado. Use Inativar para bloquear novos usos.',
                    400,
                )
            db.execute('DELETE FROM clients WHERE id=?',(cid,))
            audit(db,u,'Apagou cliente','Clientes',row['name'])
            db.commit()
        self.redirect('/clients')

    def recalc_route_weight(self, db, rid):
        total=db.execute("SELECT COALESCE(SUM(o.weight_kg),0) total FROM route_orders ro JOIN orders o ON o.id=ro.order_id WHERE ro.route_id=?",(rid,)).fetchone()['total']
        db.execute('UPDATE routes SET total_weight=? WHERE id=?',(float(total or 0),rid))
        return float(total or 0)

    def recalc_all_routes(self, db):
        # 1. Atualiza o peso total de todas as rotas em uma única query
        db.execute("""
            UPDATE routes
            SET total_weight = COALESCE(
                (SELECT SUM(o.weight_kg)
                 FROM route_orders ro
                 JOIN orders o ON o.id = ro.order_id
                 WHERE ro.route_id = routes.id),
                0
            )
        """)
        # 2. Atualiza o status para 'Com problema' de todas as rotas que estão 'Em rota' mas não têm pedidos
        db.execute("""
            UPDATE routes
            SET status = 'Com problema',
                notes = COALESCE(notes, '') || ?,
                updated_at = ?,
                version = COALESCE(version, 1) + 1
            WHERE status = 'Em rota'
              AND NOT EXISTS (
                  SELECT 1
                  FROM route_orders ro
                  WHERE ro.route_id = routes.id
              )
        """, (' | Carga estava em rota sem pedidos vinculados.', now()))


    def ensure_in_route_loads(self, db):
        """Regulariza pedidos que já estão com status Saiu para entrega, mas não têm vínculo em route_orders.
        Sem esse vínculo, a tela Cargas/Rotas não consegue exibir o número da carga.
        A rotina cria uma carga operacional por rota/cidade e vincula os pedidos órfãos, sem mexer em pedidos já vinculados.
        """
        orphan_groups=db.execute("""
            SELECT COALESCE(NULLIF(route_name,''),'Rota sem nome') route_name,
                   COUNT(*) c,
                   COALESCE(SUM(weight_kg),0) total_weight,
                   MIN(expected_delivery_date) min_deadline
            FROM orders
            WHERE status='Saiu para entrega'
              AND NOT EXISTS (
                  SELECT 1
                  FROM route_orders ro
                  JOIN routes ar ON ar.id=ro.route_id
                  WHERE ro.order_id=orders.id
                  AND ar.status IN ('Planejada','Em rota')
              )
            GROUP BY COALESCE(NULLIF(route_name,''),'Rota sem nome')
            ORDER BY route_name
        """).fetchall()
        created=0
        for g in orphan_groups:
            seq=db.execute("SELECT COALESCE(MAX(id),0)+1 n FROM routes").fetchone()['n']
            safe_route=''.join(ch for ch in (g['route_name'] or 'ROTA') if ch.isalnum())[-6:] or 'ROTA'
            load_name=f"CG-EMROTA-{datetime.now().strftime('%Y%m%d')}-{seq:03d}-{safe_route}"
            cap=float(get_setting('load_capacity_kg','11000') or 11000)
            cur=db.execute("""
                INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """,(load_name,today(),None,None,'Em rota',g['route_name'],float(g['total_weight'] or 0),cap,
                 f"Carga regularizada automaticamente a partir de pedidos que já estavam em rota sem vínculo de carga. Rota base: {g['route_name']}",now()))
            rid=cur.lastrowid
            orders=db.execute("""
                SELECT id FROM orders
                WHERE status='Saiu para entrega'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM route_orders ro
                      JOIN routes ar ON ar.id=ro.route_id
                      WHERE ro.order_id=orders.id
                      AND ar.status IN ('Planejada','Em rota')
                  )
                  AND COALESCE(NULLIF(route_name,''),'Rota sem nome')=?
                ORDER BY expected_delivery_date,city,id
            """,(g['route_name'],)).fetchall()
            for idx,o in enumerate(orders,1):
                db.execute("INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,?,?)",(rid,o['id'],idx,'Em rota'))
            self.recalc_route_weight(db,rid)
            created+=1
        if created:
            db.commit()
        return created

    def routes(self,u):
        qs=parse_qs(urlparse(self.path).query)
        hist_page=max(1,parse_int(qs.get('hist_page',['1'])[0],1))
        hist_size=12
        hist_offset=(hist_page-1)*hist_size
        with conn() as db:
            self.recalc_all_routes(db)
            regularized=self.ensure_in_route_loads(db)
            self.recalc_all_routes(db)
            active_routes=db.execute("""SELECT r.*,d.name driver,v.name vehicle,v.plate,COUNT(ro.id) orders_count FROM routes r LEFT JOIN drivers d ON d.id=r.driver_id LEFT JOIN vehicles v ON v.id=r.vehicle_id LEFT JOIN route_orders ro ON ro.route_id=r.id WHERE r.status IN ('Planejada','Em rota') GROUP BY r.id ORDER BY CASE r.status WHEN 'Planejada' THEN 0 WHEN 'Em rota' THEN 1 ELSE 2 END,r.date DESC,r.id DESC""").fetchall()
            closed_routes=db.execute(f"""SELECT r.*,d.name driver,v.name vehicle,v.plate,COUNT(ro.id) orders_count FROM routes r LEFT JOIN drivers d ON d.id=r.driver_id LEFT JOIN vehicles v ON v.id=r.vehicle_id LEFT JOIN route_orders ro ON ro.route_id=r.id WHERE r.status IN ('Acertada','Com problema') GROUP BY r.id ORDER BY r.date DESC,r.id DESC LIMIT {hist_size} OFFSET {hist_offset}""").fetchall()
            closed_count=db.execute("SELECT COUNT(*) c FROM routes WHERE status IN ('Acertada','Com problema')").fetchone()['c']
            ready=db.execute("""SELECT COUNT(*) c,COALESCE(SUM(weight_kg),0) w
                                FROM orders o
                                WHERE o.status='Faturado'
                                AND NOT EXISTS (
                                    SELECT 1
                                    FROM route_orders ro
                                    JOIN routes ar ON ar.id=ro.route_id
                                    WHERE ro.order_id=o.id AND ar.status IN ('Planejada','Em rota')
                                )""").fetchone()
            inroute=db.execute("SELECT COUNT(*) c FROM routes WHERE status='Em rota'").fetchone()
            late_ready=db.execute("SELECT COUNT(*) c FROM orders WHERE status='Faturado' AND expected_delivery_date<?",(today(),)).fetchone()
            available_ready=db.execute("""SELECT o.id,o.order_number,o.seller_name,o.expected_delivery_date,o.weight_kg,o.city,o.route_name,c.name client
                                          FROM orders o
                                          LEFT JOIN clients c ON c.id=o.client_id
                                          WHERE o.status='Faturado'
                                          AND NOT EXISTS (
                                              SELECT 1
                                              FROM route_orders ro
                                              JOIN routes ar ON ar.id=ro.route_id
                                              WHERE ro.order_id=o.id AND ar.status IN ('Planejada','Em rota')
                                          )
                                          ORDER BY CASE WHEN o.expected_delivery_date<? THEN 0 ELSE 1 END,o.expected_delivery_date,o.id DESC
                                          LIMIT 40""",(today(),)).fetchall()
            db.commit()
        lanes={'Planejada':'','Em rota':'','Acertada':''}
        can_edit_routes = self.has_perm(u,'edit_routes')
        can_cancel_routes = self.has_perm(u,'cancel_routes')
        can_settle_routes = self.has_perm(u,'settle_routes')
        can_create_routes = self.has_perm(u,'create_routes')
        def route_card(r, closed=False):
            pct=(float(r['total_weight'] or 0)/float(r['capacity'] or 1))*100
            actions=f"<a class='btn small' href='/routes/{r['id']}'>Abrir operação</a>"
            if not closed and r['status']=='Planejada' and can_edit_routes:
                actions += f"<form method='post' action='/routes/{r['id']}/dispatch'><button class='small'>Marcar saída</button></form>"
            if not closed and r['status']=='Em rota' and can_settle_routes:
                actions += f"<a class='btn small ghost' href='/load-settlement?q={quote(r['name'])}'>Acerto</a>"
            if not closed and r['status'] in ('Planejada','Em rota') and can_cancel_routes:
                actions += f"<form method='post' action='/routes/{r['id']}/cancel' class='inline-mini needs-confirm' data-confirm-text='Confirma cancelar esta carga?'><input name='reason' placeholder='Motivo' required><button class='danger-btn small'>Cancelar</button></form>"
            if can_cancel_routes:
                actions += f"<form method='post' action='/routes/{r['id']}/delete' class='inline-mini needs-confirm' data-confirm-text='Confirma apagar esta carga definitivamente?'><button class='danger-btn small'>Apagar</button></form>"
            return f"""<article class='route-card ops-card {'closed-card' if closed else ''}'><div class='route-head'><div><h2>{esc(r['name'])}</h2><small>Carga #{r['id']} · {brdate(r['date'])} · {esc(r['route_name'] or 'Rota livre')}</small></div>{badge(r['status'])}</div><div class='capacity'><span style='width:{min(pct,100):.0f}%'></span></div><small>{fmt_num(r['total_weight'])} kg / {fmt_num(r['capacity'])} kg · {pct:.0f}% usado {'· acima da capacidade' if pct>100 else ''}</small><div class='route-meta'><span><b>Motorista:</b> {esc(r['driver'] or 'Definir motorista')}</span><span><b>Veículo:</b> {esc((r['vehicle'] or 'Definir veículo')+' '+(r['plate'] or ''))}</span><span><b>Pedidos:</b> {r['orders_count']}</span></div><div class='route-actions'>{actions}</div></article>"""
        for r in active_routes:
            lanes[r['status'] if r['status'] in lanes else 'Planejada'] += route_card(r)
        for r in closed_routes:
            lanes['Acertada'] += route_card(r, True)
        regularized_notice = f'<div class="alert info">{regularized} carga(s) em rota foram regularizadas automaticamente porque havia pedidos com status <b>Saiu para entrega</b> sem vínculo de carga.</div>' if regularized else ''
        ready_rows=''.join(f"<tr><td><b>{esc(o['order_number'])}</b><br><small>Vendedor: {esc(o['seller_name'] or 'Não informado')}</small></td><td>{esc(o['client'] or 'Cliente não informado')}</td><td>{esc(o['city'] or 'Sem cidade')} · {esc(o['route_name'] or 'Sem rota')}</td><td>{fmt_num(o['weight_kg'])} kg</td><td>{deadline_pill(o['expected_delivery_date'],'Faturado')}</td><td><a class='btn small ghost' href='/orders/{o['id']}'>Abrir</a></td></tr>" for o in available_ready) or "<tr><td colspan='6'>Nenhum pedido disponível para montar carga.</td></tr>"
        hist_prev=f"<a class='btn small ghost' href='/routes?hist_page={hist_page-1}'>Página anterior</a>" if hist_page>1 else "<span class='muted'>Página anterior</span>"
        has_next=(hist_page*hist_size)<closed_count
        hist_next=f"<a class='btn small ghost' href='/routes?hist_page={hist_page+1}'>Próxima página</a>" if has_next else "<span class='muted'>Fim do histórico</span>"
        create_route_cta = "<a class='btn route-cta' href='/routes/new'>+ Criar nova carga</a>" if can_create_routes else "<span class='muted'>Sem permissão para criar carga</span>"
        content=f"""{regularized_notice}<div class='route-command clean-metrics'><div class='route-kpi'><small>Faturados sem carga</small><b>{ready['c']}</b><span>{fmt_num(ready['w'])} kg aguardando planejamento</span></div><div class='route-kpi'><small>Cargas em rota</small><b>{inroute['c']}</b><span>Cargas já liberadas para entrega</span></div><div class='route-kpi danger'><small>Faturados atrasados</small><b>{late_ready['c']}</b><span>Entram primeiro na carga</span></div>{create_route_cta}</div><section class='panel route-flow clean-panel'><h2>Como operar cargas e rotas</h2><div class='route-steps vertical-friendly'><span>1 Conferir faturados</span><span>2 Criar carga</span><span>3 Escolher veículo</span><span>4 Ordenar entregas</span><span>5 Sair para entrega</span><span>6 Fazer acerto</span></div></section><div class='route-stack'><section class='route-section'><div class='section-title'><div><h2>1) Pedidos disponíveis para montar carga</h2><p>Pedidos faturados e sem vínculo de carga.</p></div><span class='badge blue'>{len(available_ready)} na lista</span></div><div class='table-wrap'><table><thead><tr><th>Pedido</th><th>Cliente</th><th>Cidade/Rota</th><th>Peso</th><th>SLA</th><th>Ação</th></tr></thead><tbody>{ready_rows}</tbody></table></div></section><section class='route-section'><div class='section-title'><div><h2>2) Cargas planejadas</h2><p>Cargas montadas, mas ainda não saíram para entrega.</p></div><span class='badge blue'>Planejamento</span></div><div class='route-card-grid'>{lanes['Planejada'] or '<div class="empty">Nenhuma carga planejada.</div>'}</div></section><section class='route-section'><div class='section-title'><div><h2>3) Cargas em rota</h2><p>Cargas que já saíram e precisam de baixa operacional.</p></div><span class='badge ok'>Operação ativa</span></div><div class='route-card-grid'>{lanes['Em rota'] or '<div class="empty">Nenhuma carga em rota.</div>'}</div></section><details class='route-section route-archive'><summary><div><h2>4) Histórico de cargas</h2><p>Cargas acertadas e com problema. Exibição paginada.</p></div><span class='badge neutral'>{closed_count} no histórico</span></summary><div class='route-card-grid'>{lanes['Acertada'] or '<div class="empty">Nenhuma carga finalizada.</div>'}</div><div class='action-strip'>{hist_prev}{hist_next}</div></details></div>"""
        return self.send_html(layout(u,'Cargas e Rotas',content,'Central logística para montar cargas, controlar capacidade e baixar entregas'))

    def route_new(self,u):
        with conn() as db:
            drivers=db.execute('SELECT * FROM drivers WHERE active=1 ORDER BY name').fetchall()
            vehicles=db.execute('SELECT * FROM vehicles WHERE active=1 ORDER BY name').fetchall()
            orders=db.execute("""
                SELECT o.*,c.name client,r.name current_load,r.status current_load_status
                FROM orders o
                LEFT JOIN clients c ON c.id=o.client_id
                LEFT JOIN route_orders ro ON ro.order_id=o.id
                LEFT JOIN routes r ON r.id=ro.route_id AND r.status NOT IN ('Acertada','Com problema','Cancelada')
                WHERE o.status IN ('Faturado','Saiu para entrega')
                GROUP BY o.id
                ORDER BY CASE WHEN o.expected_delivery_date<? THEN 0 ELSE 1 END,o.expected_delivery_date,o.route_name,o.city
            """,(today(),)).fetchall()
            route_names=[r['route_name'] for r in db.execute('SELECT DISTINCT route_name FROM orders WHERE route_name IS NOT NULL AND route_name<>"" ORDER BY route_name')]
            if 'Mista' not in route_names:
                route_names.insert(0, 'Mista')
        vehicle_options=''.join(f"<option value='{v['id']}' data-capacity='{esc(v['capacity_kg'] if v['capacity_kg'] is not None else (v['capacity'] or 0))}'>{esc(v['name'])} - {esc(v['plate'] or 'sem placa')} - {fmt_num(v['capacity_kg'] if v['capacity_kg'] is not None else v['capacity'])}kg</option>" for v in vehicles)
        checks=''
        for o in orders:
            move = f"<em class='load-current'>Já está em {esc(o['current_load'])}. Ao selecionar, será transferido para esta nova carga.</em>" if o['current_load'] else ''
            route_value = str(o['route_name'] or '')
            checks += f"""<label class='check load-check' data-route="{esc(route_value)}"><input type='checkbox' name='order_{o['id']}' data-route="{esc(route_value)}" data-weight='{float(o['weight_kg'] or 0)}'> <span><b>{esc(o['order_number'])}</b> - {esc(o['client'] or '')}<small>Vendedor: {esc(o['seller_name'] or 'Não informado')} · {esc(o['city'] or '')} · {esc(route_value or 'Sem rota')} · {deadline_pill(o['expected_delivery_date'],o['status'])} · {fmt_num(o['weight_kg'])} kg</small>{move}</span></label>"""
        if not route_names:
            route_names = ['Rota livre']
        route_select=''.join(f"<option value='{esc(r)}'>{esc(r)}</option>" for r in route_names)
        content=f"""<form method='post' action='/routes/new' class='form route-builder'><section class='builder-left'><fieldset><legend>1. Dados da carga</legend><div class='grid3'><label>Nome da carga<input name='name' required value='Carga {datetime.now().strftime('%d/%m %H:%M')}'></label><label>Data<input type='date' name='date' value='{today()}' required></label><label>Rota<select name='route_name' id='loadRouteSelect' required><option value=''>Selecione a rota</option>{route_select}</select><small>Selecione a rota principal desta carga.</small></label><label class='full route-lock-box'><span class='checkbox-label'><input type='checkbox' id='routeLockToggle' value='1'> Travar rota</span><small>Marcado: exibe só pedidos da rota selecionada. Desmarcado: exibe todos os pedidos elegíveis.</small></label><label>Motorista<select name='driver_id' required>{row_options(drivers,None,lambda r:r['name'])}</select></label><label>Veículo<select name='vehicle_id' id='vehicle_id' required><option value=''>Selecione veículo</option>{vehicle_options}</select></label><label>Capacidade kg<input id='capacity' name='capacity' type='text' inputmode='decimal' data-mask='decimal' data-decimals='2' value='{esc(get_setting('load_capacity_kg','11000'))}' required></label><label class='full'>Observações<textarea name='notes' placeholder='Instruções para entrega, restrições, ordem de prioridade...'></textarea></label></div></fieldset><div class='load-summary'><div><b>Pedidos selecionados: <span id='selectedCount'>0</span></b><small>Total previsto: <span id='selectedWeight'>0</span> kg</small></div><div class='load-capacity'><div class='capacity'><span id='capacityBar' style='width:0%'></span></div><small id='capacityAlert'>Capacidade dentro do limite</small></div></div><div class='alert info'>Resumo antes de salvar: confira quantidade de pedidos, peso total e capacidade do veículo.</div><button>Confirmar montagem da carga</button></section><section class='builder-right'><h2>2. Pedidos elegíveis para carga</h2><p class='muted'>Com <b>Travar rota</b> marcado, a lista mostra somente pedidos da rota escolhida. Desmarcado, exibe todos os pedidos elegíveis.</p><div class='checks'>{checks or '<div class=empty>Nenhum pedido faturado para carga. Conclua o faturamento antes de criar rota.</div>'}</div></section></form>"""
        return self.send_html(layout(u,'Nova Carga/Rota',content,'Montagem guiada de carga com capacidade e pedidos disponíveis'))

    def post_route(self,u):
        d=self.post_data(); total=0.0; selected=[]; old_routes=set()
        name=(d.get('name') or '').strip()
        if not name:
            raise ValueError('Informe o nome da carga.')
        route_name = upper_text(d.get('route_name'))
        route_key = normalized_text_key(route_name)
        if not route_name:
            raise ValueError('Carga sem rota: informe a rota antes de salvar.')
        route_date = validate_date_field(d.get('date') or today(),'a data da carga',required=True)
        driver_id = parse_int(d.get('driver_id') or 0)
        vehicle_id = parse_int(d.get('vehicle_id') or 0)
        if driver_id <= 0:
            raise ValueError('Carga sem motorista: selecione o motorista responsável.')
        if vehicle_id <= 0:
            raise ValueError('Carga sem veículo: selecione o veículo da carga.')
        cap = parse_float(d.get('capacity') or get_setting('load_capacity_kg','11000'))
        if cap <= 0:
            raise ValueError('Capacidade da carga deve ser maior que zero.')
        with conn() as db:
            driver_ok=db.execute('SELECT id,active FROM drivers WHERE id=?',(driver_id,)).fetchone()
            if not driver_ok or int(driver_ok['active'] or 0) != 1:
                raise ValueError('Motorista inválido ou inativo para a carga.')
            vehicle_ok=db.execute('SELECT id,active FROM vehicles WHERE id=?',(vehicle_id,)).fetchone()
            if not vehicle_ok or int(vehicle_ok['active'] or 0) != 1:
                raise ValueError('Veículo inválido ou inativo para a carga.')
            for k,v in d.items():
                if k.startswith('order_'):
                    oid=parse_int(k.split('_')[1])
                    order=db.execute("SELECT id,order_number,weight_kg,status,route_name FROM orders WHERE id=?",(oid,)).fetchone()
                    if not order:
                        raise ValueError('Pedido selecionado não foi encontrado. Atualize a tela e tente novamente.')
                    st=normalize_order_status(order['status'])
                    if st not in ('Faturado','Saiu para entrega'):
                        raise ValueError(f'Pedido {order["order_number"]} não está elegível para carga. Status atual: {st}.')
                    order_route = str(order['route_name'] or '').strip()
                    if not order_route:
                        raise ValueError(f'O pedido {order["order_number"]} está sem rota definida. Ajuste o pedido antes de montar a carga.')
                    if route_key != normalized_text_key("Mista") and normalized_text_key(order_route) != route_key:
                        raise ValueError(f'O pedido {order["order_number"]} pertence à rota "{order_route}" e não pode entrar em carga da rota "{route_name}".')
                    selected.append(oid)
                    total += float(order['weight_kg'] or 0)
            if not selected:
                raise ValueError('Carga sem pedido: selecione pelo menos um pedido para montar a carga.')
            if cap > 0 and total > cap:
                raise ValueError(f'A carga selecionada ultrapassa a capacidade do veículo ({fmt_num(total)} kg > {fmt_num(cap)} kg). Ajuste os pedidos ou a capacidade antes de confirmar.')
            cur=db.execute('INSERT INTO routes(name,date,driver_id,vehicle_id,status,route_name,total_weight,capacity,notes,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(name,route_date,driver_id,vehicle_id,'Planejada',route_name,total,cap,(d.get('notes') or '').strip(),now(),now(),1))
            rid=cur.lastrowid
            for i,oid in enumerate(selected,1):
                for rr in db.execute("""SELECT ro.route_id
                                       FROM route_orders ro
                                       JOIN routes ar ON ar.id=ro.route_id
                                       WHERE ro.order_id=? AND ar.status IN ('Planejada','Em rota')""",(oid,)).fetchall(): old_routes.add(rr['route_id'])
                db.execute("""DELETE FROM route_orders
                              WHERE id IN (
                                SELECT ro.id
                                FROM route_orders ro
                                JOIN routes ar ON ar.id=ro.route_id
                                WHERE ro.order_id=? AND ar.status IN ('Planejada','Em rota')
                              )""",(oid,))
                db.execute('INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,?,?)',(rid,oid,i,'Pendente'))
                old=normalize_order_status(db.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()['status'])
                if old != 'Faturado':
                    db.execute("UPDATE orders SET status='Faturado',updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",(now(),oid))
                    add_hist(db,oid,u['id'],old,'Faturado','Pedido transferido para nova carga',f'Carga {rid}')
            for old_rid in old_routes:
                if old_rid != rid: self.recalc_route_weight(db,old_rid)
            self.recalc_route_weight(db,rid)
            audit(db,u,'Criou carga','Rotas',name,'',str(total)); db.commit()
        self.redirect(f'/routes/{rid}')

    def route_detail(self,u,rid):
        with conn() as db:
            r=db.execute("SELECT r.*,d.name driver,v.name vehicle,v.plate FROM routes r LEFT JOIN drivers d ON d.id=r.driver_id LEFT JOIN vehicles v ON v.id=r.vehicle_id WHERE r.id=?",(rid,)).fetchone()
            target_route = str(r['route_name'] or '').strip() if r else ''
            ros=db.execute("""SELECT ro.*,o.order_number,o.seller_name,o.status,o.weight_kg,o.city,o.route_name,
                                     o.expected_delivery_date,o.receipt_photo,o.delivery_location_link,c.name client,
                                     (SELECT COUNT(*) FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.image_data IS NOT NULL) has_photo,
                                     (SELECT COUNT(*) FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.digital_signature IS NOT NULL) has_sig,
                                     (SELECT delivery_location_link FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.delivery_location_link IS NOT NULL AND dr.delivery_location_link!='' LIMIT 1) dr_loc_link
                              FROM route_orders ro JOIN orders o ON o.id=ro.order_id LEFT JOIN clients c ON c.id=o.client_id WHERE ro.route_id=? ORDER BY ro.delivery_order,ro.id""",(rid,)).fetchall()
            if target_route.lower().strip() == 'mista':
                available=db.execute("""SELECT o.id,o.order_number,o.seller_name,c.name client,o.city,o.route_name,o.weight_kg
                                        FROM orders o
                                        LEFT JOIN clients c ON c.id=o.client_id
                                        WHERE o.status IN ('Faturado','Saiu para entrega')
                                          AND NOT EXISTS (
                                              SELECT 1
                                              FROM route_orders ro2
                                              JOIN routes ar ON ar.id=ro2.route_id
                                              WHERE ro2.order_id=o.id
                                              AND ar.status IN ('Planejada','Em rota')
                                          )
                                        ORDER BY o.expected_delivery_date,o.city""").fetchall()
            else:
                available=db.execute("""SELECT o.id,o.order_number,o.seller_name,c.name client,o.city,o.route_name,o.weight_kg
                                        FROM orders o
                                        LEFT JOIN clients c ON c.id=o.client_id
                                        WHERE o.status IN ('Faturado','Saiu para entrega')
                                          AND LOWER(TRIM(COALESCE(o.route_name,'')))=LOWER(TRIM(?))
                                          AND NOT EXISTS (
                                              SELECT 1
                                              FROM route_orders ro2
                                              JOIN routes ar ON ar.id=ro2.route_id
                                              WHERE ro2.order_id=o.id
                                              AND ar.status IN ('Planejada','Em rota')
                                          )
                                        ORDER BY o.expected_delivery_date,o.city""",(target_route,)).fetchall()
            drivers=db.execute('SELECT id,name FROM drivers WHERE active=1 ORDER BY name').fetchall()
            vehicles=db.execute('SELECT id,name,plate,capacity,capacity_kg FROM vehicles WHERE active=1 ORDER BY name').fetchall()
        if not r:
            return self.fail(u,'Não encontrado','Carga não encontrada.',404)
        route_status = normalize_route_status(r['status'])
        can_edit = route_status in ('Planejada', 'Em rota')
        can_dispatch = route_status == 'Planejada'
        can_cancel = route_status in ('Planejada', 'Em rota') and self.has_perm(u,'cancel_routes')
        can_delete = self.has_perm(u,'cancel_routes')
        can_settle_routes = self.has_perm(u,'settle_routes')
        settlement_link = f"<a class='btn small ghost' href='/load-settlement?q={quote(r['name'])}'>Acerto da carga</a>" if can_settle_routes else "<span class='muted'>Sem permissão para acerto</span>"
        pct=(float(r['total_weight'] or 0)/float(r['capacity'] or 1))*100
        rows=''.join(f"""<tr><td><input form='seqForm' type='number' name='seq_{ro['id']}' value='{ro['delivery_order']}' class='seq' {'readonly' if not can_edit else ''}></td><td><a href='/orders/{ro['order_id']}'><b>{esc(ro['order_number'])}</b></a><br><small>{esc(ro['client'] or '')} · Vendedor: {esc(ro['seller_name'] or 'Não informado')}</small></td><td>{esc(ro['city'] or '')}<br><small>{esc(ro['route_name'] or '')}</small></td><td>{badge(ro['status'])}{f'<br><a href="/orders/{ro["order_id"]}/receipt-image" target="_blank" class="btn small ghost" style="color:#059669; border-color:#059669; padding:1px 4px; font-size:0.75rem; margin-top:3px; display:inline-block;">📷 Canhoto</a>' if (ro['has_photo'] or ro['receipt_photo']) else ''}{f'<a href="/orders/{ro["order_id"]}/signature-image" target="_blank" class="btn small ghost" style="color:#0284c7; border-color:#0284c7; padding:1px 4px; font-size:0.75rem; margin-top:3px; display:inline-block;">✍️ Assinatura</a>' if ro['has_sig'] else ''}{f'<br><a href="{esc(ro["dr_loc_link"] or ro["delivery_location_link"])}" target="_blank" class="btn small ghost" style="color:#0369a1; border-color:#0369a1; padding:1px 4px; font-size:0.75rem; margin-top:3px; display:inline-block;">📍 Mapa</a>' if (("dr_loc_link" in ro.keys() and ro["dr_loc_link"]) or ("delivery_location_link" in ro.keys() and ro["delivery_location_link"])) else ""}</td><td>{deadline_pill(ro['expected_delivery_date'],ro['status'])}</td><td>{fmt_num(ro['weight_kg'])} kg</td><td><div class='route-actions'>{f"<form method='post' action='/routes/{rid}/remove/{ro['id']}' class='inline-mini'><button class='danger-btn'>Remover</button></form>" if can_edit else "<span class='muted'>Carga finalizada</span>"}</div></td></tr>""" for ro in ros)
        add_opts=''.join(f"<option value='{a['id']}'>{esc(a['order_number'])} - {esc(a['client'] or '')} - Vendedor: {esc(a['seller_name'] or 'Não informado')} - {esc(a['city'] or '')} - {fmt_num(a['weight_kg'])}kg</option>" for a in available)
        lock_notice = '<div class="alert info">Carga finalizada: edição de pedidos e sequência está bloqueada para preservar o histórico.</div>' if not can_edit else ''
        dispatch_action = f"<form method='post' action='/routes/{rid}/dispatch'><button>1. Marcar saída</button></form>" if can_dispatch else "<span class='muted'>Saída já registrada</span>"
        cancel_action = f"""<form method='post' action='/routes/{rid}/cancel' class='inline-form needs-confirm' data-confirm-text='Confirma cancelar esta carga? Todos pedidos não finalizados voltarão para Faturado.'><input name='reason' placeholder='Motivo do cancelamento' required><button class='danger-btn'>Cancelar carga</button></form>""" if can_cancel else ""
        delete_action = f"""<form method='post' action='/routes/{rid}/delete' class='inline-form needs-confirm' data-confirm-text='Confirma apagar esta carga definitivamente?'><button class='danger-btn'>Apagar carga</button></form>""" if can_delete else ""
        add_panel = f"<section class='panel no-print'><h2>Adicionar pedido</h2><p class='muted'>Somente pedidos da rota <b>{esc(r['route_name'] or 'não definida')}</b> aparecem na lista.</p><form method='post' action='/routes/{rid}/add' class='inline-form'><select name='order_id' required><option value=''>Pedido faturado sem carga</option>{add_opts}</select><button>Adicionar</button></form></section>" if can_edit else "<section class='panel no-print'><h2>Adicionar pedido</h2><div class='alert info'>Carga finalizada: não é possível adicionar novos pedidos.</div></section>"
        save_seq_btn = "<button form='seqForm' class='btn ghost no-print'>Salvar sequência</button>" if can_edit else "<span class='muted'>Sequência bloqueada</span>"
        assisted=self.route_assisted_block(r, u=u)
        driver_opts=row_options(drivers,r['driver_id'],lambda x:x['name'],False)
        vehicle_opts=''.join(
            f"<option value='{v['id']}' data-capacity='{esc(v['capacity_kg'] if v['capacity_kg'] is not None else (v['capacity'] or 0))}' {'selected' if str(v['id'])==str(r['vehicle_id'] or '') else ''}>{esc(v['name'])} {esc(v['plate'] or '')}</option>"
            for v in vehicles
        )
        edit_meta_panel = f"""<section class='panel no-print'><h2>Editar dados da carga</h2><form method='post' action='/routes/{rid}/update' class='form compact'><input type='hidden' name='updated_at' value='{esc(r['updated_at'] or '')}'><input type='hidden' name='version' value='{esc(r['version'] or 1)}'><div class='grid3'><label>Nome da carga<input name='name' required value='{esc(r['name'] or '')}'></label><label>Data<input type='date' name='date' required value='{esc(r['date'] or today())}'></label><label>Rota<input name='route_name' required value='{esc(r['route_name'] or '')}'></label><label>Motorista<select name='driver_id' required>{driver_opts}</select></label><label>Veículo<select name='vehicle_id' id='vehicle_id' required>{vehicle_opts}</select></label><label>Capacidade kg<input name='capacity' id='capacity' type='text' inputmode='decimal' data-mask='decimal' data-decimals='2' value='{esc(r['capacity'] or 0)}' required></label><label class='full'>Observações<textarea name='notes'>{esc(r['notes'] or '')}</textarea></label></div><button>Salvar dados da carga</button></form></section>""" if can_edit else ""
        reopen_panel=''
        if can_manage_reopen(u) and route_status in FINAL_ROUTE_STATUSES:
            reopen_panel=f"""<section class='panel no-print'><h2>Reabertura controlada da carga</h2><form method='post' action='/routes/{rid}/reopen' class='form compact needs-confirm' data-confirm-text='Confirma reabrir esta carga finalizada?'><div class='grid3'><label>Destino<select name='target_status'>{option(['Planejada','Em rota'],'Planejada')}</select></label><label class='full'>Motivo obrigatório<textarea name='reason' required placeholder='Descreva o motivo da reabertura da carga...'></textarea></label></div><button class='danger-btn'>Reabrir carga</button></form></section>"""
        content=f"""{lock_notice}<section class='route-hero'><div><h2>{esc(r['name'])}</h2><p>{brdate(r['date'])} - {esc(r['route_name'] or 'Sem rota')} - {esc(r['driver'] or 'Sem motorista')} - {esc((r['vehicle'] or '')+' '+(r['plate'] or ''))}</p></div><div>{badge(r['status'])}<b>{fmt_num(r['total_weight'])} / {fmt_num(r['capacity'])} kg</b></div></section>{assisted}<div class='route-control-grid'><section class='panel'><h2>Capacidade</h2><div class='capacity big'><span style='width:{min(pct,100):.0f}%'></span></div><p><b>{pct:.0f}% usado</b> - {fmt_num(r['total_weight'])} kg carregados de {fmt_num(r['capacity'])} kg</p>{'<div class="alert danger">Carga acima da capacidade do veículo.</div>' if pct>100 else '<div class="alert success">Carga dentro da capacidade.</div>'}</section><section class='panel no-print'><h2>Ações principais</h2><div class='action-strip clean'>{dispatch_action}{delete_action}</div>{cancel_action}</section>{add_panel}</div>{edit_meta_panel}<form id='seqForm' method='post' action='/routes/{rid}/sequence'></form><section class='panel'><div class='section-title'><h2>Sequência de entrega</h2>{save_seq_btn}</div><div class='table-wrap'><table><thead><tr><th>Seq.</th><th>Pedido</th><th>Cidade/Rota</th><th>Status</th><th>Prazo limite</th><th>Peso</th><th>Ações</th></tr></thead><tbody>{rows or '<tr><td colspan=7>Nenhum pedido nesta carga.</td></tr>'}</tbody></table></div></section>{reopen_panel}"""
        return self.send_html(layout(u,'Operação da Carga',content,'Painel de saída, sequência, capacidade e baixa de entregas'))

    def post_route_dispatch(self,u,rid):
        with conn() as db:
            r=find_route_by_id(db, rid)
            if not r:
                return self.fail(u,'Não encontrado','Carga não encontrada.',404)
            new_route_status,old_route_status=self.ensure_route_status(db,rid,'Em rota')
            if old_route_status != 'Planejada':
                return self.fail(u,'Operação inválida','Somente cargas planejadas podem ser marcadas como saída.',400)
            orders=db.execute('SELECT id,order_id FROM route_orders WHERE route_id=?',(rid,)).fetchall()
            if not orders:
                return self.fail(u,'Carga inválida','Uma carga só pode sair para entrega quando tiver pedidos vinculados.',400)
            update_route_status(db, rid, new_route_status, now())
            for ro in orders:
                old=normalize_order_status(db.execute('SELECT status FROM orders WHERE id=?',(ro['order_id'],)).fetchone()['status'])
                db.execute("UPDATE route_orders SET status='Em rota' WHERE id=?",(ro['id'],))
                if old != 'Saiu para entrega':
                    self.ensure_order_status(db,ro['order_id'],'Saiu para entrega')
                    db.execute("UPDATE orders SET status='Saiu para entrega',updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",(now(),ro['order_id']))
                    add_hist(db,ro['order_id'],u['id'],old,'Saiu para entrega','Carga saiu para entrega',f'Carga {rid}')
            self.recalc_route_weight(db,rid)
            audit(db,u,'Despachou carga','Rotas',str(rid))
            db.commit()
        self.redirect(f'/routes/{rid}')
    def post_route_update(self,u,rid):
        d=self.post_data()
        name=(d.get('name') or '').strip()
        route_name=upper_text(d.get('route_name'))
        posted_updated_at=(d.get('updated_at') or '').strip()
        posted_version=parse_int(d.get('version') or 1,1)
        if not name:
            raise ValueError('Informe o nome da carga.')
        if not route_name:
            raise ValueError('Informe a rota da carga.')
        route_date=validate_date_field(d.get('date') or today(),'a data da carga',required=True)
        driver_id=parse_int(d.get('driver_id') or 0)
        vehicle_id=parse_int(d.get('vehicle_id') or 0)
        if driver_id <= 0:
            raise ValueError('Selecione um motorista ativo.')
        if vehicle_id <= 0:
            raise ValueError('Selecione um veículo ativo.')
        capacity=parse_float(d.get('capacity') or 0)
        if capacity <= 0:
            raise ValueError('Capacidade da carga deve ser maior que zero.')
        with conn() as db:
            route=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            if not route:
                return self.fail(u,'Não encontrado','Carga não encontrada.',404)
            if normalize_route_status(route['status']) in FINAL_ROUTE_STATUSES or normalize_route_status(route['status']) == 'Cancelada':
                return self.fail(u,'Operação bloqueada','Carga finalizada/cancelada não pode ter metadados editados.',400)
            db_updated_at=str(route['updated_at'] or '').strip()
            db_version=parse_int(route['version'] or 1,1)
            if posted_updated_at and db_updated_at and posted_updated_at != db_updated_at:
                return self.fail(
                    u,
                    'Conflito de edição',
                    'Esta carga foi alterada por outro usuário antes de salvar. Atualize a tela e tente novamente.',
                    409,
                )
            if posted_version and db_version and posted_version != db_version:
                return self.fail(
                    u,
                    'Conflito de edição',
                    'Esta carga foi alterada por outro usuário antes de salvar. Atualize a tela e tente novamente.',
                    409,
                )
            driver_ok=db.execute('SELECT id,active FROM drivers WHERE id=?',(driver_id,)).fetchone()
            if not driver_ok or int(driver_ok['active'] or 0) != 1:
                raise ValueError('Motorista inválido ou inativo.')
            vehicle_ok=db.execute('SELECT id,active FROM vehicles WHERE id=?',(vehicle_id,)).fetchone()
            if not vehicle_ok or int(vehicle_ok['active'] or 0) != 1:
                raise ValueError('Veículo inválido ou inativo.')
            total=float(route['total_weight'] or 0)
            if total > capacity:
                raise ValueError(f'A capacidade informada ({fmt_num(capacity)} kg) é menor que o peso atual da carga ({fmt_num(total)} kg).')
            db.execute("""UPDATE routes
                          SET name=?,date=?,driver_id=?,vehicle_id=?,route_name=?,capacity=?,notes=?,updated_at=?,version=COALESCE(version,1)+1
                          WHERE id=?""",(name,route_date,driver_id,vehicle_id,route_name,capacity,(d.get('notes') or '').strip(),now(),rid))
            audit(db,u,'Editou carga','Rotas',str(rid),route['name'],name)
            db.commit()
        self.redirect(f'/routes/{rid}')
    def post_route_finish(self,u,rid):
        with conn() as db:
            r=db.execute("SELECT * FROM routes WHERE id=?",(rid,)).fetchone()
            if not r:
                return self.fail(u,'Não encontrado','Carga não encontrada.',404)
        return self.fail(u,'Operação redirecionada','Conclua a carga pelo módulo de acerto para registrar entregue/problema por pedido.',400)
    def post_route_sequence(self,u,rid):
        d=self.post_data()
        with conn() as db:
            route=db.execute('SELECT status FROM routes WHERE id=?',(rid,)).fetchone()
            if not route:
                return self.fail(u,'Não encontrado','Carga não encontrada.',404)
            if normalize_route_status(route['status']) in ('Acertada','Com problema','Cancelada'):
                return self.fail(u,'Operação bloqueada','Carga finalizada não pode ter sequência alterada.',400)
            valid_ids = {int(r['id']) for r in db.execute('SELECT id FROM route_orders WHERE route_id=?',(rid,)).fetchall()}
            for k,v in d.items():
                if k.startswith('seq_'):
                    ro_id = parse_int(k.split('_')[1] or 0)
                    if ro_id not in valid_ids:
                        return self.fail(u,'Operação inválida','Tentativa de editar sequência de pedido fora desta carga.',400)
                    seq=max(1,parse_int(v or 1))
                    db.execute('UPDATE route_orders SET delivery_order=? WHERE id=? AND route_id=?',(seq,ro_id,rid))
            touch_route(db, rid, now())
            db.commit()
        self.redirect(f'/routes/{rid}')
    def post_route_add_order(self,u,rid):
        d=self.post_data(); oid=parse_int(d.get('order_id') or 0)
        if oid <= 0:
            return self.fail(u,'Dados inválidos','Selecione um pedido válido para adicionar na carga.',400)
        with conn() as db:
            route=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            if not route:
                return self.fail(u,'Não encontrado','Carga não encontrada.',404)
            if normalize_route_status(route['status']) in ('Acertada','Com problema','Cancelada'):
                return self.fail(u,'Operação bloqueada','Carga finalizada não pode receber novos pedidos.',400)
            if oid:
                order=db.execute("SELECT status,weight_kg,order_number,route_name FROM orders WHERE id=?",(oid,)).fetchone()
                if not order:
                    return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
                if normalize_order_status(order['status']) not in ('Faturado','Saiu para entrega'):
                    return self.fail(u,'Pedido inelegível','Somente pedidos faturados ou em rota podem ser adicionados à carga.',400)
                order_route = str(order['route_name'] or '').strip()
                route_name = str(route['route_name'] or '').strip()
                if not order_route:
                    return self.fail(u,'Pedido sem rota','Este pedido está sem rota definida. Ajuste o pedido antes de adicionar na carga.',400)
                if normalized_text_key(route_name) != normalized_text_key("Mista") and normalized_text_key(order_route) != normalized_text_key(route_name):
                    return self.fail(u,'Rota incompatível',f'Este pedido pertence à rota "{order_route}" e não pode ser adicionado na carga da rota "{route_name}".',400)
                old_routes=[r['route_id'] for r in db.execute("""SELECT ro.route_id
                                                                 FROM route_orders ro
                                                                 JOIN routes ar ON ar.id=ro.route_id
                                                                 WHERE ro.order_id=? AND ar.status IN ('Planejada','Em rota')""",(oid,)).fetchall()]
                order_weight=float(order['weight_kg'] or 0)
                current_total=float(route['total_weight'] or 0)
                capacity=float(route['capacity'] or 0)
                already_in_target = rid in old_routes
                projected_total = current_total if already_in_target else (current_total + order_weight)
                if capacity > 0 and projected_total > capacity:
                    return self.fail(
                        u,
                        'Capacidade excedida',
                        f'Não é possível adicionar o pedido {order["order_number"]}: capacidade da carga seria excedida ({fmt_num(projected_total)} kg > {fmt_num(capacity)} kg).',
                        400,
                    )
                db.execute("""DELETE FROM route_orders
                              WHERE id IN (
                                SELECT ro.id
                                FROM route_orders ro
                                JOIN routes ar ON ar.id=ro.route_id
                                WHERE ro.order_id=? AND ar.status IN ('Planejada','Em rota')
                              )""",(oid,))
                seq=db.execute('SELECT COALESCE(MAX(delivery_order),0)+1 n FROM route_orders WHERE route_id=?',(rid,)).fetchone()['n']
                ro_status='Em rota' if route['status']=='Em rota' else 'Pendente'
                db.execute('INSERT INTO route_orders(route_id,order_id,delivery_order,status) VALUES(?,?,?,?)',(rid,oid,seq,ro_status))
                new_order_status='Saiu para entrega' if route['status']=='Em rota' else 'Faturado'
                old_status=normalize_order_status(order['status'])
                if old_status != new_order_status:
                    self.ensure_order_status(db,oid,new_order_status)
                    db.execute('UPDATE orders SET status=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(new_order_status,now(),oid))
                    add_hist(db,oid,u['id'],old_status,new_order_status,'Pedido transferido de carga',f'Carga destino {rid}')
                for old_rid in old_routes:
                    if old_rid != rid: self.recalc_route_weight(db,old_rid)
                self.recalc_route_weight(db,rid)
                touch_route(db, rid, now())
                audit(db,u,'Adicionou/transferiu pedido à carga','Rotas',str(rid),'',str(oid))
                db.commit()
        self.redirect(f'/routes/{rid}')
    def post_route_remove_order(self,u,rid,roid):
        with conn() as db:
            route=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            if not route:
                return self.fail(u,'Não encontrado','Carga não encontrada.',404)
            if normalize_route_status(route['status']) in ('Acertada','Com problema','Cancelada'):
                return self.fail(u,'Operação bloqueada','Carga finalizada não pode ser editada.',400)
            ro=db.execute('SELECT * FROM route_orders WHERE id=? AND route_id=?',(roid,rid)).fetchone()
            if not ro:
                return self.fail(u,'Não encontrado','Vínculo de pedido não encontrado nesta carga.',404)
            if route['status']=='Em rota':
                c=db.execute('SELECT COUNT(*) c FROM route_orders WHERE route_id=?',(rid,)).fetchone()['c']
                if c <= 1:
                    return self.fail(u,'Operação bloqueada','A carga está em rota e não pode ficar sem pedidos. Transfira o pedido para outra carga.',400)
            order=db.execute('SELECT status FROM orders WHERE id=?',(ro['order_id'],)).fetchone()
            db.execute('DELETE FROM route_orders WHERE id=? AND route_id=?',(roid,rid))
            if order:
                old=normalize_order_status(order['status'])
                if old=='Saiu para entrega':
                    self.ensure_order_status(db,ro['order_id'],'Faturado')
                    db.execute("UPDATE orders SET status='Faturado',updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",(now(),ro['order_id']))
                    add_hist(db,ro['order_id'],u['id'],old,'Faturado','Removido da carga',f'Carga {rid}')
            self.recalc_route_weight(db,rid)
            touch_route(db, rid, now())
            audit(db,u,'Removeu pedido da carga','Rotas',str(rid),'',str(roid))
            db.commit()
        self.redirect(f'/routes/{rid}')

    def load_settlement(self,u):
        qs=parse_qs(urlparse(self.path).query); q=qs.get('q',[''])[0].strip()
        can_view_financial = self.can_view_financial(u)
        search_box=f"""<section class='panel settlement-search'><h2>Buscar carga</h2><form method='get' action='/load-settlement' class='inline-form'><input name='q' value='{esc(q)}' placeholder='Digite o número/nome da carga. Ex.: CG-20260515-002' autofocus><button>Buscar carga</button></form><p class='muted'>Use esta tela quando a carga voltou ou quando o responsável precisa fechar todos os pedidos da carga em uma única conferência.</p></section>"""
        if not q:
            with conn() as db:
                recent=db.execute("""SELECT r.*,COUNT(ro.id) orders_count FROM routes r LEFT JOIN route_orders ro ON ro.route_id=r.id WHERE r.status='Em rota' GROUP BY r.id ORDER BY r.date DESC,r.id DESC LIMIT 10""").fetchall()
            cards=''.join(f"<a class='settlement-card' href='/load-settlement?q={quote(r['name'])}'><b>{esc(r['name'])}</b><span>{badge(r['status'])}</span><small>{brdate(r['date'])} · {r['orders_count']} pedidos</small></a>" for r in recent) or '<div class="empty">Nenhuma carga ativa encontrada.</div>'
            return self.send_html(layout(u,'Acerto de carga',search_box+f"<section class='panel'><h2>Cargas ativas recentes</h2><div class='settlement-grid'>{cards}</div></section>",'Fechamento operacional da carga e baixa final das entregas'))
        with conn() as db:
            r=db.execute("""SELECT r.*,d.name driver,v.name vehicle,v.plate
                            FROM routes r
                            LEFT JOIN drivers d ON d.id=r.driver_id
                            LEFT JOIN vehicles v ON v.id=r.vehicle_id
                            WHERE CAST(r.id AS TEXT)=? OR r.name=? OR r.name LIKE ?
                            ORDER BY r.id DESC LIMIT 1""",(q,q,f'%{q}%')).fetchone()
            if not r:
                return self.send_html(layout(u,'Acerto de carga',search_box+'<div class="alert danger">Carga não encontrada. Confira o código/nome e tente novamente.</div>','Fechamento operacional da carga e baixa final das entregas'))
            rows=db.execute("""SELECT ro.*,o.id order_id,o.order_number,o.seller_name,o.status,o.weight_kg,o.total_value,
                                      o.payment_method,o.delivered_at,o.final_notes,o.city,o.route_name,o.receipt_photo,
                                      o.delivery_location_link,c.name client,c.farm_name,
                                      (SELECT COUNT(*) FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.image_data IS NOT NULL) has_photo,
                                      (SELECT COUNT(*) FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.digital_signature IS NOT NULL) has_sig,
                                      (SELECT delivery_location_link FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.delivery_location_link IS NOT NULL AND dr.delivery_location_link!='' LIMIT 1) dr_loc_link
                               FROM route_orders ro JOIN orders o ON o.id=ro.order_id LEFT JOIN clients c ON c.id=o.client_id WHERE ro.route_id=? ORDER BY ro.delivery_order,ro.id""",(r['id'],)).fetchall()
        route_status = normalize_route_status(r['status'])
        if not rows:
            body='<div class="empty">Esta carga não possui pedidos vinculados.</div>'
        elif route_status != 'Em rota':
            cards_list = []
            for ro in rows:
                photo_btn = f"<a class='btn small ghost' style='color:#059669; border-color:#059669; font-weight:700;' href='/orders/{ro['order_id']}/receipt-image' target='_blank'>📷 Ver Foto do Canhoto</a>" if (ro['has_photo'] or ro['receipt_photo']) else ""
                sig_btn = f"<a class='btn small ghost' style='color:#0284c7; border-color:#0284c7; font-weight:700;' href='/orders/{ro['order_id']}/signature-image' target='_blank'>✍️ Ver Assinatura Digital</a>" if ro['has_sig'] else ""
                loc_url = ro['dr_loc_link'] if ('dr_loc_link' in ro.keys() and ro['dr_loc_link']) else (ro['delivery_location_link'] if 'delivery_location_link' in ro.keys() else None)
                map_btn = f"<a class='btn small ghost' style='color:#0369a1; border-color:#0369a1; font-weight:700;' href='{esc(loc_url)}' target='_blank'>🌐 Ver Mapa no Google Maps</a>" if loc_url else ""
                btns_html = f"<div class='full' style='display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;'>{photo_btn}{sig_btn}{map_btn}</div>" if (photo_btn or sig_btn or map_btn) else ""
                cards_list.append(
                    f"<article class='settlement-order'><div class='settlement-main'><div><b>{esc(ro['order_number'])}</b><small>{esc(ro['client'] or '')} · {esc(ro['city'] or '')} · Vendedor: {esc(ro['seller_name'] or 'Não informado')} · {fmt_num(ro['weight_kg'])} kg · {money_visible(can_view_financial, ro['total_value'])}</small></div>{badge(ro['status'])}</div><div class='settlement-fields'><label>Método pagamento<input value='{esc(ro['payment_method'] or 'Não informado')}' readonly></label><label>Data entrega/ocorrência<input value='{esc(brdate(ro['delivered_at']))}' readonly></label><label class='full'>Observação final<textarea readonly>{esc(ro['final_notes'] or 'Sem observação')}</textarea></label>{btns_html}</div></article>"
                )
            cards = ''.join(cards_list)
            body=f"""<section class='panel'><div class='alert info'>Carga já finalizada em status <b>{esc(route_status)}</b>. Edição bloqueada para preservar o histórico.</div><section class='route-hero settlement-hero'><div><h2>{esc(r['name'])}</h2><p>Carga #{r['id']} · {brdate(r['date'])} · {esc(r['route_name'] or 'Rota livre')} · {esc(r['driver'] or 'Sem motorista')} · {esc((r['vehicle'] or '')+' '+(r['plate'] or ''))}</p></div><div>{badge(r['status'])}<b>{fmt_num(r['total_weight'])} kg</b></div></section><div class='settlement-list'>{cards}</div><div class='form-actions'><a class='btn ghost' href='/load-settlement/{r['id']}/print-report'>Relatório compacto (1 página)</a><a class='btn ghost' href='/routes/{r['id']}'>Voltar para operação da carga</a></div></section>"""
        else:
            trs=''
            for ro in rows:
                delivered_date=(ro['delivered_at'] or today())[:10]
                photo_btn = f"<a class='btn small ghost' style='color:#059669; border-color:#059669; font-weight:700;' href='/orders/{ro['order_id']}/receipt-image' target='_blank'>📷 Ver Foto do Canhoto</a>" if (ro['has_photo'] or ro['receipt_photo']) else ""
                sig_btn = f"<a class='btn small ghost' style='color:#0284c7; border-color:#0284c7; font-weight:700;' href='/orders/{ro['order_id']}/signature-image' target='_blank'>✍️ Ver Assinatura Digital</a>" if ro['has_sig'] else ""
                loc_url = ro['dr_loc_link'] if ('dr_loc_link' in ro.keys() and ro['dr_loc_link']) else (ro['delivery_location_link'] if 'delivery_location_link' in ro.keys() else None)
                map_btn = f"<a class='btn small ghost' style='color:#0369a1; border-color:#0369a1; font-weight:700;' href='{esc(loc_url)}' target='_blank'>🌐 Ver Mapa no Google Maps</a>" if loc_url else ""
                rec_btns = f"<div class='full' style='display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;'>{photo_btn}{sig_btn}{map_btn}</div>" if (photo_btn or sig_btn or map_btn) else ""
                trs += f"""<article class='settlement-order'><div class='settlement-main'><label class='settlement-check'><input type='checkbox' name='ok_{ro['order_id']}' class='settlement-ok' required> <span>Checklist conferido</span></label><div><b>{esc(ro['order_number'])}</b><small>{esc(ro['client'] or '')} · {esc(ro['city'] or '')} · Vendedor: {esc(ro['seller_name'] or 'Não informado')} · {fmt_num(ro['weight_kg'])} kg · {money_visible(can_view_financial, ro['total_value'])}</small></div>{badge(ro['status'])}</div><div class='settlement-fields'><label>Resultado<select name='result_{ro['order_id']}' class='settlement-result'><option value='entregue'>Entregue</option><option value='problema'>Registrar problema</option></select></label><label>Data entrega/ocorrência<input type='date' name='date_{ro['order_id']}' value='{esc(delivered_date)}' required></label><label>Método pagamento<select name='pay_{ro['order_id']}'>{payment_method_options(ro['payment_method'] or '')}</select></label><label>Tipo de problema<select name='ptype_{ro['order_id']}'>{option(PROBLEM_TYPES,'Outro motivo')}</select></label><label class='full'>Observação do acerto<textarea name='obs_{ro['order_id']}' placeholder='Recebedor, divergência, comprovante ou descrição do problema...'>{esc(ro['final_notes'] or '')}</textarea></label>{rec_btns}</div></article>"""
            body=f"""<form method='post' action='/load-settlement/{r['id']}/finish' class='settlement-form needs-confirm' data-confirm-text='Confirma concluir a carga? A baixa será aplicada em todos os pedidos conferidos.'><section class='route-hero settlement-hero'><div><h2>{esc(r['name'])}</h2><p>Carga #{r['id']} · {brdate(r['date'])} · {esc(r['route_name'] or 'Rota livre')} · {esc(r['driver'] or 'Sem motorista')} · {esc((r['vehicle'] or '')+' '+(r['plate'] or ''))}</p></div><div>{badge(r['status'])}<b>{fmt_num(r['total_weight'])} kg</b></div></section><div class='settlement-tools'><div style='display: flex; align-items: center; gap: 15px; flex-wrap: wrap;'><label class='settlement-check'><input type='checkbox' id='markAllDelivered'> <span>Marcar todos como entregues</span></label><button type='button' id='btnSetDateSelected' class='btn' style='font-size: 13px; padding: 6px 12px; height: auto;'>Definir Data para Selecionados</button></div><small class='muted'>Se houver problema em algum pedido, troque o resultado para <b>Registrar problema</b>.</small></div><div class='alert info'>Marque todos os checklists para liberar a conclusão. Ao concluir, cada pedido será baixado como <b>entregue</b> ou <b>problema</b> conforme selecionado.</div><div class='settlement-list'>{trs}</div><div class='form-actions sticky-actions'><button class='success-btn'>Concluir Carga</button><a class='btn ghost' href='/routes/{r['id']}'>Voltar para operação da carga</a></div></form>
            <div class='settlement-date-overlay' id='settlementDateOverlay'>
              <div class='settlement-date-modal' role='dialog' aria-modal='true' aria-labelledby='settlementDateTitle'>
                <button type='button' class='settlement-date-close' id='settlementDateClose' aria-label='Fechar'>&times;</button>
                <h3 id='settlementDateTitle'>Definir Data para Selecionados</h3>
                <form id='settlementDateForm' class='form'>
                  <div class='form-group' style='margin-bottom: 15px;'>
                    <label for='batch_delivery_date' style='display: block; margin-bottom: 5px; font-weight: 600;'>Escolha a Data de Entrega/Ocorrência</label>
                    <input type='date' id='batch_delivery_date' class='form-control' style='width: 100%; padding: 8px; border: 1px solid var(--border); border-radius: var(--radius-sm);' required>
                  </div>
                  <div class='alert info' style='margin-bottom: 15px; font-size: 13px; line-height: 1.4; padding: 10px; background: #eef9f2; border-left: 4px solid var(--accent); color: #174f2a; border-radius: var(--radius-sm);'>
                    A data escolhida será aplicada no banco de dados e no formulário para todos os pedidos marcados como <b>conferidos</b> no momento.
                  </div>
                  <div class='form-actions' style='display: flex; justify-content: flex-end; gap: 10px;'>
                    <button type='button' class='btn ghost' id='btnCancelSetDate' style='padding: 8px 16px;'>Cancelar</button>
                    <button type='submit' class='success-btn' style='padding: 8px 16px;'>Confirmar</button>
                  </div>
                </form>
              </div>
            </div>"""
        return self.send_html(layout(u,'Acerto de carga',search_box+body,'Fechamento operacional da carga e baixa final das entregas'))

    def post_load_settlement_set_date(self, u, rid):
        d = self.post_data()
        dt = validate_date_field(d.get('date'), 'a data da carga', required=True)
        order_ids_str = d.get('order_ids') or ''
        order_ids = [int(x.strip()) for x in order_ids_str.split(',') if x.strip().isdigit()]
        
        if not order_ids:
            raise ValueError("Nenhum pedido selecionado.")
            
        with conn() as db:
            r = db.execute('SELECT * FROM routes WHERE id=?', (rid,)).fetchone()
            if not r:
                return self.fail(u, 'Não encontrado', 'Carga não encontrada.', 404)
            if normalize_route_status(r['status']) != 'Em rota':
                return self.fail(u, 'Operação inválida', 'Somente cargas em rota podem ter as datas de pedidos alteradas.', 400)
                
            placeholders = ','.join('?' for _ in order_ids)
            ros = db.execute(
                f"SELECT ro.*, o.status, o.order_number, o.delivered_at, o.sale_date "
                f"FROM route_orders ro "
                f"JOIN orders o ON o.id=ro.order_id "
                f"WHERE ro.route_id=? AND ro.order_id IN ({placeholders})",
                [rid] + order_ids
            ).fetchall()
            
            valid_order_ids = {ro['order_id'] for ro in ros}
            if len(valid_order_ids) != len(order_ids):
                raise ValueError("Um ou mais pedidos selecionados não pertencem a esta carga.")
                
            for ro in ros:
                if ro['sale_date'] and dt < str(ro['sale_date'])[:10]:
                    raise ValueError(f"A data informada para o pedido {ro['order_number']} não pode ser anterior à data da venda ({brdate(ro['sale_date'])}).")
            
            updated_details = []
            for ro in ros:
                oid = ro['order_id']
                old_date = ro['delivered_at'] or ''
                
                if old_date == dt:
                    continue
                    
                db.execute(
                    "UPDATE orders SET delivered_at=?, updated_at=?, version=COALESCE(version,1)+1 WHERE id=?",
                    (dt, now(), oid)
                )
                
                add_hist(
                    db, 
                    oid, 
                    u['id'], 
                    normalize_order_status(ro['status']), 
                    normalize_order_status(ro['status']), 
                    'Alteração de data em lote', 
                    f"Data de entrega/ocorrência alterada de '{old_date}' para '{dt}' no acerto da carga {r['name']}."
                )
                updated_details.append(f"Pedido {ro['order_number']}: '{old_date}' -> '{dt}'")
            
            if updated_details:
                audit(
                    db, 
                    u, 
                    'Definiu data de carga em lote', 
                    'Acerto de carga', 
                    str(rid), 
                    '', 
                    f"Carga {r['name']} | Pedidos atualizados: {len(updated_details)} | Detalhes: {', '.join(updated_details)}"
                )
                db.commit()
                
        # Retorna resposta JSON ou de sucesso
        self.send_response(200)
        self._common_headers()
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write("OK".encode('utf-8'))

    def load_settlement_print_report(self,u,rid):
        can_view_financial = self.can_view_financial(u)
        with conn() as db:
            r=db.execute("""SELECT r.*,d.name driver,v.name vehicle,v.plate
                            FROM routes r
                            LEFT JOIN drivers d ON d.id=r.driver_id
                            LEFT JOIN vehicles v ON v.id=r.vehicle_id
                            WHERE r.id=?""",(rid,)).fetchone()
            rows=db.execute("""SELECT ro.delivery_order,o.order_number,o.seller_name,o.status,o.weight_kg,o.total_value,o.payment_method,o.delivered_at,o.city,c.name client
                               FROM route_orders ro
                               JOIN orders o ON o.id=ro.order_id
                               LEFT JOIN clients c ON c.id=o.client_id
                               WHERE ro.route_id=?
                               ORDER BY ro.delivery_order,ro.id""",(rid,)).fetchall()
        if not r:
            return self.fail(u,'Não encontrado','Carga não encontrada para relatório.',404)
        total_orders=len(rows)
        delivered=sum(1 for ro in rows if normalize_order_status(ro['status']) == 'Acertado')
        problems=sum(1 for ro in rows if normalize_order_status(ro['status']) == 'Problema')
        canceled=sum(1 for ro in rows if normalize_order_status(ro['status']) == 'Cancelado')
        pending=max(0,total_orders-delivered-problems-canceled)
        total_value=sum(float(ro['total_value'] or 0) for ro in rows)
        total_value_html = money_visible(can_view_financial, total_value)
        compact_rows=''.join(
            f"<tr><td>{parse_int(ro['delivery_order'] or 0)}</td><td>{esc(ro['order_number'])}</td><td>{esc(ro['seller_name'] or 'Não informado')}</td><td>{esc(ro['client'] or '—')}</td><td>{esc(ro['city'] or '—')}</td><td>{badge(ro['status'])}</td><td>{fmt_num(ro['weight_kg'])} kg</td><td>{esc(ro['payment_method'] or '—')}</td><td>{brdate(ro['delivered_at'])}</td></tr>"
            for ro in rows
        ) or "<tr><td colspan='9'>Sem pedidos nesta carga.</td></tr>"
        content=f"""<section class='panel settlement-print-page'><div class='alert info'>Relatório compacto para impressão operacional em página única.</div><section class='route-hero settlement-hero compact'><div><h2>{esc(r['name'])}</h2><p>Carga #{r['id']} · {brdate(r['date'])} · {esc(r['route_name'] or 'Rota livre')} · {esc(r['driver'] or 'Sem motorista')} · {esc((r['vehicle'] or '')+' '+(r['plate'] or ''))}</p></div><div>{badge(r['status'])}<b>{fmt_num(r['total_weight'])} kg</b></div></section><div class='cards compact-cards'><div class='card total'><small>Pedidos</small><strong>{total_orders}</strong></div><div class='card done'><small>Entregues</small><strong>{delivered}</strong></div><div class='card danger'><small>Problema</small><strong>{problems}</strong></div><div class='card warn'><small>Pendentes</small><strong>{pending}</strong></div><div class='card neutral'><small>Cancelados</small><strong>{canceled}</strong></div><div class='card route'><small>Valor total</small><strong>{total_value_html}</strong></div></div><div class='table-wrap compact-table'><table><thead><tr><th>Seq</th><th>Pedido</th><th>Vendedor</th><th>Cliente</th><th>Cidade</th><th>Status</th><th>Peso</th><th>Pagamento</th><th>Data</th></tr></thead><tbody>{compact_rows}</tbody></table></div><div class='form-actions'><a class='btn ghost' href='/load-settlement?q={quote(r["name"])}'>Voltar ao acerto</a></div></section>"""
        return self.send_html(layout(u,f'Relatório compacto da carga {r["id"]}',content,'Versão otimizada para impressão operacional'))

    def post_load_settlement_finish(self,u,rid):
        d=self.post_data()
        with conn() as db:
            r=db.execute('SELECT * FROM routes WHERE id=?',(rid,)).fetchone()
            if not r: return self.fail(u,'Não encontrado','Carga não encontrada.',404)
            if normalize_route_status(r['status']) != 'Em rota':
                return self.fail(u,'Operação inválida','Somente cargas em rota podem ser concluídas no acerto.',400)
            ros=db.execute('SELECT ro.*,o.status,o.order_number,o.sale_date FROM route_orders ro JOIN orders o ON o.id=ro.order_id WHERE ro.route_id=? ORDER BY ro.delivery_order,ro.id',(rid,)).fetchall()
            if not ros:
                return self.fail(u,'Carga vazia','Esta carga não possui pedidos para acerto.',400)
            missing=[ro['order_number'] for ro in ros if f'ok_{ro["order_id"]}' not in d]
            if missing:
                return self.send_html(layout(u,'Acerto incompleto',f'<div class="alert danger">Antes de concluir, marque o checklist de todos os pedidos. Pendentes: {esc(", ".join(missing))}</div><a class="btn ghost" href="/load-settlement?q={quote(r["name"])}">Voltar</a>'),400)
            problem_count=0
            for ro in ros:
                oid=ro['order_id']; old=normalize_order_status(ro['status']); dt=validate_date_field(d.get(f'date_{oid}'),'a data de entrega/ocorrência',required=True); pay=validate_payment_method(d.get(f'pay_{oid}') or '',required=False); obs=(d.get(f'obs_{oid}') or '').strip()
                if ro['sale_date'] and dt < str(ro['sale_date'])[:10]:
                    raise ValueError(f'Data de entrega/ocorrência do pedido {ro["order_number"]} não pode ser anterior à venda.')
                result=(d.get(f'result_{oid}') or 'entregue').strip().lower()
                if result not in ('entregue', 'problema'):
                    raise ValueError(f'Resultado inválido no pedido {ro["order_number"]}.')
                if result == 'problema':
                    if not obs:
                        raise ValueError(f'Informe observação do problema para o pedido {ro["order_number"]}.')
                    ptype=(d.get(f'ptype_{oid}') or 'Outro motivo').strip()
                    self.ensure_order_status(db,oid,'Problema',obs)
                    db.execute("""UPDATE orders
                                  SET status='Problema',
                                      delivered_at=COALESCE(NULLIF(delivered_at,''),?),
                                      payment_method=COALESCE(NULLIF(?,''),payment_method),
                                      final_notes=?,
                                      updated_at=?,
                                      version=COALESCE(version,1)+1
                                  WHERE id=?""",(dt,pay,obs,now(),oid))
                    db.execute("UPDATE route_orders SET status='Com problema' WHERE id=?",(ro['id'],))
                    db.execute('INSERT INTO delivery_problems(order_id,problem_type,description,created_at) VALUES(?,?,?,?)',(oid,ptype,obs,now()))
                    add_hist(db,oid,u['id'],old,'Problema','Problema registrado no acerto',f'Carga {r["name"]}. {ptype}: {obs}')
                    problem_count += 1
                else:
                    self.ensure_order_status(db,oid,'Acertado',obs)
                    db.execute("""UPDATE orders
                                  SET status='Acertado',
                                      delivered_at=?,
                                      payment_method=COALESCE(NULLIF(?,''),payment_method),
                                      final_notes=?,
                                      updated_at=?,
                                      version=COALESCE(version,1)+1
                                  WHERE id=?""",(dt,pay,obs,now(),oid))
                    db.execute("UPDATE route_orders SET status='Entregue' WHERE id=?",(ro['id'],))
                    add_hist(db,oid,u['id'],old,'Acertado','Acerto de carga concluído',f'Carga {r["name"]}. Pagamento: {pay}. {obs}')
            new_route_status='Com problema' if problem_count else 'Acertada'
            self.ensure_route_status(db,rid,new_route_status)
            update_route_status(db, rid, new_route_status, now())
            self.recalc_route_weight(db,rid)
            audit(db,u,'Concluiu acerto de carga','Acerto de carga',str(rid),'',f'{r["name"]} | problemas: {problem_count}'); db.commit()
        self.redirect(f'/load-settlement?q={quote(r["name"])}')

    def sla(self,u):
        qs=parse_qs(urlparse(self.path).query); start=qs.get('start',[today()[:8]+'01'])[0]; end=qs.get('end',[today()])[0]
        with conn() as db:
            rows=db.execute("SELECT o.*,c.name client,c.city client_city FROM orders o LEFT JOIN clients c ON c.id=o.client_id WHERE o.sale_date BETWEEN ? AND ? ORDER BY o.expected_delivery_date,o.id DESC",(start,end)).fetchall()
            holidays=db.execute('SELECT * FROM holidays ORDER BY date DESC LIMIT 90').fetchall()
            by_status=db.execute("SELECT status,COUNT(*) c FROM orders WHERE sale_date BETWEEN ? AND ? GROUP BY status ORDER BY c DESC",(start,end)).fetchall()
            by_route=db.execute("""SELECT COALESCE(route_name,'Sem rota') route_name,
                                          COUNT(*) c,
                                          SUM(CASE WHEN expected_delivery_date<? AND status NOT IN ('Acertado','Problema','Cancelado','Agendado') THEN 1 ELSE 0 END) late,
                                          SUM(CASE WHEN expected_delivery_date BETWEEN ? AND ? AND status NOT IN ('Acertado','Problema','Cancelado','Agendado') THEN 1 ELSE 0 END) risk
                                   FROM orders
                                   WHERE sale_date BETWEEN ? AND ?
                                   GROUP BY route_name
                                   ORDER BY late DESC,risk DESC,c DESC
                                   LIMIT 10""",(today(),today(),date_add(SLA_RISK_DAYS),start,end)).fetchall()
            by_city=db.execute("""SELECT COALESCE(city,'Sem cidade') city,
                                         COUNT(*) c,
                                         SUM(CASE WHEN expected_delivery_date<? AND status NOT IN ('Acertado','Problema','Cancelado','Agendado') THEN 1 ELSE 0 END) late,
                                         SUM(CASE WHEN expected_delivery_date BETWEEN ? AND ? AND status NOT IN ('Acertado','Problema','Cancelado','Agendado') THEN 1 ELSE 0 END) risk
                                  FROM orders
                                  WHERE sale_date BETWEEN ? AND ?
                                  GROUP BY city
                                  ORDER BY late DESC,risk DESC,c DESC
                                  LIMIT 10""",(today(),today(),date_add(SLA_RISK_DAYS),start,end)).fetchall()
        can_manage = self.has_perm(u,'manage_sla')
        total=len(rows); finished=[r for r in rows if r['status']=='Acertado']; open_rows=[r for r in rows if r['status'] not in ('Acertado','Problema','Cancelado')]
        out=[r for r in open_rows if r['status'] != 'Agendado' and r['expected_delivery_date'] and r['expected_delivery_date']<today()]
        risk=[r for r in open_rows if r['status'] != 'Agendado' and days_to(r['expected_delivery_date']) is not None and 0 <= days_to(r['expected_delivery_date']) <= SLA_RISK_DAYS]
        faturado_waiting=sum(1 for r in open_rows if r['status']=='Faturado')
        in_route_risk=sum(1 for r in open_rows if r['status']=='Saiu para entrega' and days_to(r['expected_delivery_date']) is not None and days_to(r['expected_delivery_date']) <= SLA_RISK_DAYS)
        problems_count=sum(1 for r in rows if r['status']=='Problema')
        delivered_ok=0; delivered_late=0; delivery_days=[]; open_age=[]
        for r in finished:
            delivered_date = (r['delivered_at'] or '')[:10]
            if delivered_date:
                if delivered_date <= (r['expected_delivery_date'] or '9999-99-99'):
                    delivered_ok+=1
                else:
                    delivered_late+=1
            try: delivery_days.append((datetime.strptime((r['delivered_at'] or '')[:10],'%Y-%m-%d').date()-datetime.strptime((r['sale_date'] or '')[:10],'%Y-%m-%d').date()).days)
            except Exception: pass
        for r in open_rows:
            try: open_age.append((date.today()-datetime.strptime((r['sale_date'] or '')[:10],'%Y-%m-%d').date()).days)
            except Exception: pass
        holidays_set={h['date'] for h in holidays}
        holiday_impact_orders=0; holiday_impact_days=0
        for r in rows:
            sale=(r['sale_date'] or '')[:10]
            if not sale:
                continue
            no_holiday=add_business_days_from(sale, get_setting('sla_limit_days','15'), set())
            with_holiday=add_business_days_from(sale, get_setting('sla_limit_days','15'), holidays_set)
            try:
                diff=(datetime.strptime(with_holiday,'%Y-%m-%d').date()-datetime.strptime(no_holiday,'%Y-%m-%d').date()).days
            except Exception:
                diff=0
            if diff > 0:
                holiday_impact_orders += 1
                holiday_impact_days += diff
        holiday_impact_avg=round(holiday_impact_days/max(holiday_impact_orders,1),1) if holiday_impact_orders else 0
        critical=sorted(
            [r for r in rows if r['status']=='Problema' or r in out or r in risk],
            key=lambda x: (
                0 if x in out else 1 if x in risk else 2,
                (x['expected_delivery_date'] or '9999-99-99'),
                x['id']
            )
        )[:25]
        pct=round((delivered_ok/max(len(finished),1))*100,1) if finished else 100
        avg_delivery=round(sum(delivery_days)/len(delivery_days),1) if delivery_days else 0
        avg_open=round(sum(open_age)/len(open_age),1) if open_age else 0

        # Metadados de Saúde do SLA para o Painel Executivo
        if pct >= 95:
            health_message = "Excelente. A operação está cumprindo a meta de conformidade do SLA."
            circle_color = "var(--success)"
        elif pct >= 85:
            health_message = "Atenção. A conformidade está ligeiramente abaixo da meta ideal de 95%."
            circle_color = "var(--secondary)"
        else:
            health_message = "Crítico. A operação está com baixa conformidade e alta taxa de violação do SLA."
            circle_color = "var(--danger)"

        out_pct = round((len(out) / max(total, 1)) * 100)
        risk_pct = round((len(risk) / max(total, 1)) * 100)
        ontime_count = total - len(out) - len(risk)
        ontime_pct = round((ontime_count / max(total, 1)) * 100)

        sla_health_panel = f"""
        <section class='panel sla-executive-summary'>
          <h2>Resumo Executivo do SLA</h2>
          <div class='sla-dashboard-grid'>
            <!-- Card 1: Saúde do SLA (Conformidade) -->
            <div class='sla-kpi-card large'>
              <div class='sla-progress-circle' style='--value: {pct}; --circle-color: {circle_color};'>
                <svg>
                  <circle cx='45' cy='45' r='40'></circle>
                  <circle cx='45' cy='45' r='40'></circle>
                </svg>
                <div class='percentage'>
                  <span>{pct}%</span>
                  <small>Conformidade</small>
                </div>
              </div>
              <div class='sla-kpi-info'>
                <h3>Saúde da Operação</h3>
                <p>{health_message}</p>
                <small>Meta estabelecida: 95%</small>
              </div>
            </div>

            <!-- Card 2: Distribuição de Pendências -->
            <div class='sla-kpi-card'>
              <h3>Distribuição por Status do Prazo</h3>
              <div class='sla-status-bars'>
                <div class='sla-bar-item late'>
                  <div class='bar-header'><span>Atrasados (SLA Violado)</span><b>{len(out)}</b></div>
                  <div class='bar-track'><div class='bar-fill' style='width: {out_pct}%'></div></div>
                </div>
                <div class='sla-bar-item risk'>
                  <div class='bar-header'><span>Em Risco (Prazo Crítico)</span><b>{len(risk)}</b></div>
                  <div class='bar-track'><div class='bar-fill' style='width: {risk_pct}%'></div></div>
                </div>
                <div class='sla-bar-item ontime'>
                  <div class='bar-header'><span>No Prazo / Concluídos</span><b>{ontime_count}</b></div>
                  <div class='bar-track'><div class='bar-fill' style='width: {ontime_pct}%'></div></div>
                </div>
              </div>
            </div>

            <!-- Card 3: Eficiência e Prazos -->
            <div class='sla-kpi-card stats-card'>
              <h3>Eficiência e Prazos</h3>
              <div class='sla-stats-list'>
                <div><small>Venda-Entrega</small><strong>{avg_delivery} dias</strong></div>
                <div><small>Idade Média Aberto</small><strong>{avg_open} dias</strong></div>
                <div><small>Sem Saída</small><strong>{faturado_waiting} ped.</strong></div>
                <div><small>Impacto Feriados</small><strong>+{holiday_impact_avg} dias</strong></div>
              </div>
            </div>
          </div>
        </section>
        """

        cards_main=f"""<div class='cards sla-cards'>
            <div class='card total'><small>Pedidos no período</small><strong>{total}</strong><span>Base analisada</span></div>
            <div class='card done'><small>Entregas dentro do prazo</small><strong>{delivered_ok}</strong><span>{pct}% de conformidade</span></div>
            <div class='card danger'><small>Pedidos atrasados</small><strong>{len(out)}</strong><span>Ação imediata</span></div>
            <div class='card warn'><small>Pedidos em risco</small><strong>{len(risk)}</strong><span>Vencem em até {SLA_RISK_DAYS} dias</span></div>
        </div>"""
        cards_secondary=f"""<details class='more-kpis-details'>
            <summary class='btn ghost'>Ver todos os 10 indicadores</summary>
            <div class='cards sla-cards more-kpis-grid'>
                <div class='card danger'><small>Entregas fora do prazo</small><strong>{delivered_late}</strong><span>Finalizados com SLA violado</span></div>
                <div class='card warn'><small>Em rota próximos SLA</small><strong>{in_route_risk}</strong><span>Exigem acerto rápido</span></div>
                <div class='card nf'><small>Faturados sem saída</small><strong>{faturado_waiting}</strong><span>Aguardando carga/saída</span></div>
                <div class='card danger'><small>Pedidos com problema</small><strong>{problems_count}</strong><span>Finalizados com ocorrência</span></div>
                <div class='card route'><small>Média venda-entrega</small><strong>{avg_delivery}d</strong><span>Pedidos finalizados</span></div>
                <div class='card total'><small>Impacto de feriados</small><strong>{holiday_impact_orders}</strong><span>Média +{holiday_impact_avg} dia(s) no prazo</span></div>
            </div>
        </details>"""
        cards = cards_main + cards_secondary

        trs_list = []
        for r in rows:
            is_late = r['expected_delivery_date'] and r['expected_delivery_date']<today() and r['status'] not in ('Acertado','Problema','Cancelado')
            is_risk = (not is_late) and r['status'] not in ('Acertado','Problema','Cancelado') and days_to(r['expected_delivery_date']) is not None and 0 <= days_to(r['expected_delivery_date']) <= SLA_RISK_DAYS
            row_type = 'sla-row-late' if is_late else ('sla-row-risk' if is_risk else 'sla-row-ontime')
            highlight_cls = 'late-row' if is_late else ('risk-row' if is_risk else '')
            trs_list.append(f"""<tr class='{row_type} {highlight_cls}'><td><b>{esc(r['order_number'])}</b><br><small>{esc(r['client'] or '')} · Vendedor: {esc(r['seller_name'] or 'Não informado')}</small></td><td>{brdate(r['sale_date'])}</td><td>{brdate(r['expected_delivery_date'])}</td><td>{badge(r['status'])}</td><td>{deadline_pill(r['expected_delivery_date'],r['status'])}</td><td>{esc(r['city'] or r['client_city'] or '')}</td><td><a class='btn small ghost' href='/orders/{r['id']}'>Abrir</a></td></tr>""")
        trs = ''.join(trs_list)

        critical_rows=''.join(f"""<tr><td><b>{esc(r['order_number'])}</b><br><small>{esc(r['client'] or '')} · Vendedor: {esc(r['seller_name'] or 'Não informado')}</small></td><td>{badge(r['status'])}</td><td>{brdate(r['expected_delivery_date'])}</td><td>{esc(r['route_name'] or 'Sem rota')} · {esc(r['city'] or r['client_city'] or 'Sem cidade')}</td><td><a class='btn small ghost' href='/orders/{r['id']}'>Abrir</a></td></tr>""" for r in critical) or '<tr><td colspan="5">Nenhum pedido crítico no período.</td></tr>'
        hrows=''.join(f"""<tr><td>{brdate(h['date'])}</td><td>{esc(h['name'] or 'Feriado/folga operacional')}</td><td>{f'<form method="post" action="/sla/holiday/{h["id"]}/delete"><button class="danger-btn small">Remover</button></form>' if can_manage else '<span class="muted">Somente GOD</span>'}</td></tr>""" for h in holidays) or '<tr><td colspan="3">Nenhum feriado cadastrado.</td></tr>'
        status_bars=''.join(f"""<div class='flow-row'><span>{badge(r['status'])}</span><b>{r['c']}</b></div>""" for r in by_status) or '<div class="empty">Sem status no período.</div>'
        route_bars=''.join(f"""<div class='sla-route-row'><div><b>{esc(r['route_name'])}</b><small>{r['c']} pedidos · {r['late'] or 0} atrasados · {r['risk'] or 0} em risco</small></div><div class='sla-meter'><span style='width:{min(100, (((r['late'] or 0)+(r['risk'] or 0))/max(r['c'],1))*100):.0f}%'></span></div></div>""" for r in by_route) or '<div class="empty">Sem rotas no período.</div>'
        city_bars=''.join(f"""<div class='sla-route-row'><div><b>{esc(r['city'])}</b><small>{r['c']} pedidos · {r['late'] or 0} atrasados · {r['risk'] or 0} em risco</small></div><div class='sla-meter'><span style='width:{min(100, (((r['late'] or 0)+(r['risk'] or 0))/max(r['c'],1))*100):.0f}%'></span></div></div>""" for r in by_city) or '<div class="empty">Sem cidades no período.</div>'
        recalc_form = f"""<form method='post' action='/sla/recalculate' class='inline-form'><input type='hidden' name='start' value='{esc(start)}'><input type='hidden' name='end' value='{esc(end)}'><button class='btn ghost'>Recalcular prazos do período</button></form>""" if can_manage else ''
        holiday_form = """<form method='post' action='/sla/holiday' class='inline-form'><input type='date' name='date' required><input name='name' placeholder='Nome do feriado ou motivo'><button>Adicionar feriado</button></form>""" if can_manage else '<div class="muted">Somente GOD pode cadastrar/remover feriados.</div>'

        filter_tabs = f"""
        <div class='sla-filter-tabs'>
          <button type='button' class='btn-sla-tab active' data-filter='all'>Todos ({total})</button>
          <button type='button' class='btn-sla-tab' data-filter='sla-row-late'>🔴 Atrasados ({len(out)})</button>
          <button type='button' class='btn-sla-tab' data-filter='sla-row-risk'>🟡 Em Risco ({len(risk)})</button>
          <button type='button' class='btn-sla-tab' data-filter='sla-row-ontime'>🟢 No Prazo ({ontime_count})</button>
        </div>
        """

        content=f"""<form class='filters'><input type='date' name='start' value='{esc(start)}'><input type='date' name='end' value='{esc(end)}'><button>Atualizar SLA</button></form>{recalc_form}{cards}{sla_health_panel}
        
        <section class='panel'>
          <h2>Pedidos críticos (Ação Imediata)</h2>
          <div class='table-wrap'>
            <table>
              <thead>
                <tr><th>Pedido</th><th>Status</th><th>Prazo</th><th>Rota/Cidade</th><th>Ação</th></tr>
              </thead>
              <tbody>{critical_rows}</tbody>
            </table>
          </div>
        </section>
        
        <details class='accordion-panel'>
          <summary>Distribuição e Análise de Gargalos (Status, Rotas e Cidades)</summary>
          <div class='accordion-content'>
            <div class='dash-grid'>
              <section class='panel'>
                <h2>Distribuição por status</h2>
                <div class='flow-list'>{status_bars}</div>
              </section>
              <section class='panel'>
                <h2>SLA por rota</h2>
                {route_bars}
                <h2>SLA por cidade</h2>
                {city_bars}
              </section>
            </div>
          </div>
        </details>
        
        <details class='accordion-panel'>
          <summary>SLA por Pedido (Lista Completa de {total} Pedidos)</summary>
          <div class='accordion-content'>
            {filter_tabs}
            <div class='table-wrap'>
              <table>
                <thead>
                  <tr><th>Pedido</th><th>Venda</th><th>Prazo limite</th><th>Status</th><th>SLA</th><th>Cidade</th><th>Ação</th></tr>
                </thead>
                <tbody>{trs or '<tr><td colspan=7>Nenhum pedido no período.</td></tr>'}</tbody>
              </table>
            </div>
          </div>
        </details>
        
        <details class='accordion-panel'>
          <summary>Feriados e Dias Sem Contagem ({len(holidays)})</summary>
          <div class='accordion-content'>
            <section class='panel'>
              {holiday_form}
              <p class='muted'>Feriados aumentam o prazo útil. No período atual, {holiday_impact_orders} pedido(s) tiveram prazo impactado por feriados cadastrados.</p>
              <div class='table-wrap'>
                <table>
                  <thead>
                    <tr><th>Data</th><th>Motivo</th><th>Ação</th></tr>
                  </thead>
                  <tbody>{hrows}</tbody>
                </table>
              </div>
            </section>
          </div>
        </details>"""
        return self.send_html(layout(u,'SLA Operacional',content,f'Service Level Agreement calculado por {SLA_LIMIT_DAYS} dias corridos'))

    def post_sla_recalculate(self,u):
        if not self.has_perm(u,'manage_sla'):
            return self.fail(u,'Acesso negado','Somente GOD pode recalcular SLA em lote.',403)
        d=self.post_data()
        start=validate_date_field(d.get('start') or (today()[:8]+'01'),'a data inicial',required=True)
        end=validate_date_field(d.get('end') or today(),'a data final',required=True)
        if start > end:
            raise ValueError('Período inválido: data inicial maior que a data final.')
        changed=0
        with conn() as db:
            rows=db.execute("SELECT id,sale_date,expected_delivery_date FROM orders WHERE sale_date BETWEEN ? AND ?",(start,end)).fetchall()
            for r in rows:
                if not r['sale_date']:
                    continue
                new_deadline=add_business_days(r['sale_date'], get_setting('sla_limit_days','15'))
                if (r['expected_delivery_date'] or '') != new_deadline:
                    db.execute('UPDATE orders SET expected_delivery_date=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(new_deadline,now(),r['id']))
                    changed += 1
            audit(db,u,'Recalculou SLA em lote','SLA',f'{start}..{end}','','Pedidos atualizados: '+str(changed))
            db.commit()
        self.redirect(f'/sla?start={quote(start)}&end={quote(end)}')

    def post_holiday(self,u):
        if not self.has_perm(u,'manage_sla'):
            return self.send_html(layout(u,'Acesso negado','<div class="alert danger">Seu usuário não pode cadastrar feriados do SLA.</div>'),403)
        d=self.post_data()
        hdate = validate_date_field(d.get('date'),'a data do feriado',required=True)
        hname = (d.get('name') or '').strip()
        with conn() as db:
            db.execute('INSERT OR REPLACE INTO holidays(date,name,created_at) VALUES(?,?,?)',(hdate,hname,now()))
            audit(db,u,'Alterou feriado','SLA',hdate,'',hname); db.commit()
        self.redirect('/sla')
    def post_holiday_delete(self,u,hid):
        if not self.has_perm(u,'manage_sla'):
            return self.send_html(layout(u,'Acesso negado','<div class="alert danger">Seu usuário não pode remover feriados do SLA.</div>'),403)
        with conn() as db:
            db.execute('DELETE FROM holidays WHERE id=?',(hid,)); audit(db,u,'Removeu feriado','SLA',str(hid)); db.commit()
        self.redirect('/sla')

    def drivers(self,u):
        qs=parse_qs(urlparse(self.path).query); edit_id=qs.get('edit',[''])[0].strip()
        with conn() as db:
            rows=db.execute('SELECT d.*,COUNT(o.id) deliveries FROM drivers d LEFT JOIN orders o ON o.driver_id=d.id AND o.status="Acertado" GROUP BY d.id ORDER BY d.active DESC,d.name').fetchall()
            edit_row=db.execute('SELECT * FROM drivers WHERE id=?',(parse_int(edit_id),)).fetchone() if edit_id.isdigit() else None
            vehicle_defaults=db.execute('SELECT DISTINCT name,plate FROM vehicles WHERE name IS NOT NULL AND name<>"" ORDER BY name').fetchall()
        active_rows = [r for r in rows if int(r['active'] or 0) == 1]
        inactive_rows = [r for r in rows if int(r['active'] or 0) != 1]
        vehicle_suggestions = datalist_options([f'{v["name"]} {v["plate"] or ""}'.strip() for v in vehicle_defaults] + [r['vehicle_default'] for r in rows])
        can_manage = self.has_perm(u,'manage_drivers')
        can_delete_catalog = can_manage and can_manage_catalog_deletions(u)
        form=''
        if can_manage:
            form='<form method="post" action="/drivers" class="inline-form"><input name="name" placeholder="Motorista" required><input name="phone" placeholder="Telefone"><input name="document" placeholder="Documento"><input name="vehicle_default" list="vehicleDefaultSuggestions" placeholder="Veículo padrão"><button>Adicionar</button></form>'
        edit_panel=''
        if can_manage and edit_row:
            edit_panel=f'''<section class="panel"><h2>Editar motorista</h2><form method="post" action="/drivers/{edit_row["id"]}/update" class="inline-form"><input name="name" required value="{esc(edit_row["name"])}"><input name="phone" value="{esc(edit_row["phone"] or "")}"><input name="document" value="{esc(edit_row["document"] or "")}"><input name="vehicle_default" list="vehicleDefaultSuggestions" value="{esc(edit_row["vehicle_default"] or "")}"><button>Salvar</button><a class="btn ghost" href="/drivers">Cancelar</a></form></section>'''
        def render_rows(source_rows):
            body_rows=''
            for r in source_rows:
                if can_manage:
                    actions=f'''<a class="btn small ghost" href="/drivers?edit={r["id"]}">Editar</a>
                                '''
                    if can_delete_catalog:
                        toggle_label='Inativar' if int(r['active'] or 0) == 1 else 'Reativar'
                        toggle_class='danger-btn small' if int(r['active'] or 0) == 1 else 'small'
                        actions += f'''<form method="post" action="/drivers/{r["id"]}/toggle" class="inline-mini needs-confirm" data-confirm-text="Confirma {toggle_label.lower()} este motorista?"><button class="{toggle_class}">{toggle_label}</button></form>
                                <form method="post" action="/drivers/{r["id"]}/delete" class="inline-mini needs-confirm" data-confirm-text="Confirma apagar este motorista definitivamente?"><button class="danger-btn small">Apagar</button></form>'''
                else:
                    actions='<span class="muted">Somente visualização</span>'
                body_rows += f'<tr><td><b>{esc(r["name"])}</b></td><td>{esc(r["phone"] or "—")}</td><td>{esc(r["document"] or "—")}</td><td>{esc(r["vehicle_default"] or "—")}</td><td>{r["deliveries"]}</td><td>{"Ativo" if r["active"] else "Inativo"}</td><td>{actions}</td></tr>'
            return body_rows or '<tr><td colspan="7">Nenhum motorista cadastrado.</td></tr>'
        body = render_rows(active_rows)
        inactive_body = render_rows(inactive_rows)
        inactive_panel = ''
        if inactive_rows:
            inactive_panel = f'''<details class="route-section route-archive"><summary><div><h2>Histórico de motoristas inativos</h2><p>Motoristas sem uso atual na operação.</p></div><span class="badge neutral">{len(inactive_rows)} inativo(s)</span></summary><div class="table-wrap"><table><thead><tr><th>Nome</th><th>Telefone</th><th>Documento</th><th>Veículo padrão</th><th>Entregas</th><th>Status</th><th>Ações</th></tr></thead><tbody>{inactive_body}</tbody></table></div></details>'''
        datalist = f'<datalist id="vehicleDefaultSuggestions">{vehicle_suggestions}</datalist>'
        return self.send_html(layout(u,'Motoristas',form+edit_panel+datalist+f'<div class="table-wrap"><table><thead><tr><th>Nome</th><th>Telefone</th><th>Documento</th><th>Veículo padrão</th><th>Entregas</th><th>Status</th><th>Ações</th></tr></thead><tbody>{body}</tbody></table></div>{inactive_panel}','Cadastro de equipe de entrega'))
    def post_driver(self,u):
        d=self.post_data(); name=(d.get('name') or '').strip()
        if not name:
            raise ValueError('Informe o nome do motorista.')
        phone=self.validate_phone(d.get('phone'),'telefone')
        doc=(d.get('document') or '').strip()
        with conn() as db:
            if doc:
                dup_doc=db.execute('SELECT id,name FROM drivers WHERE document=? LIMIT 1',(doc,)).fetchone()
                if dup_doc:
                    raise ValueError(f'Documento duplicado: já existe no motorista {dup_doc["name"]}.')
            db.execute('INSERT INTO drivers(name,phone,document,vehicle_default,active,updated_at,version,password_hash,must_change_password) VALUES(?,?,?,?,1,?,1,?,1)',(name,phone,doc,(d.get('vehicle_default') or '').strip(),now(),hash_driver_password(DEFAULT_DRIVER_PASSWORD))); audit(db,u,'Criou motorista','Motoristas',name,notes='Senha inicial temporária definida; troca obrigatória no primeiro acesso.'); db.commit()
        self.redirect('/drivers')
    def post_driver_update(self,u,did):
        d=self.post_data(); name=(d.get('name') or '').strip()
        if not name:
            raise ValueError('Informe o nome do motorista.')
        phone=self.validate_phone(d.get('phone'),'telefone')
        doc=(d.get('document') or '').strip()
        with conn() as db:
            row=db.execute('SELECT * FROM drivers WHERE id=?',(did,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Motorista não encontrado.',404)
            if doc:
                dup_doc=db.execute('SELECT id,name FROM drivers WHERE document=? AND id<>? LIMIT 1',(doc,did)).fetchone()
                if dup_doc:
                    raise ValueError(f'Documento duplicado: já existe no motorista {dup_doc["name"]}.')
            db.execute('UPDATE drivers SET name=?,phone=?,document=?,vehicle_default=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(name,phone,doc,(d.get('vehicle_default') or '').strip(),now(),did))
            audit(db,u,'Editou motorista','Motoristas',row['name'],row['name'],name)
            db.commit()
        self.redirect('/drivers')
    def post_driver_toggle(self,u,did):
        if not can_manage_catalog_deletions(u):
            return self.fail(u,'Ação bloqueada','Seu perfil pode cadastrar e editar motoristas, mas não pode inativar/reativar.',403)
        with conn() as db:
            row=db.execute('SELECT id,name,active FROM drivers WHERE id=?',(did,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Motorista não encontrado.',404)
            target = 0 if int(row['active'] or 0) == 1 else 1
            if target == 0:
                active_routes=db.execute("SELECT COUNT(*) c FROM routes WHERE driver_id=? AND status IN ('Planejada','Em rota')",(did,)).fetchone()['c']
                if active_routes:
                    return self.fail(u,'Inativação bloqueada',f'O motorista está vinculado a {active_routes} carga(s) ativa(s). Finalize/cancele as cargas antes de inativar.',400)
            db.execute('UPDATE drivers SET active=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(target,now(),did))
            audit(db,u,'Alterou status de motorista','Motoristas',row['name'],str(row['active']),str(target))
            db.commit()
        self.redirect('/drivers')

    def post_driver_delete(self,u,did):
        if not can_manage_catalog_deletions(u):
            return self.fail(u,'Ação bloqueada','Seu perfil pode cadastrar e editar motoristas, mas não pode excluir.',403)
        with conn() as db:
            row=db.execute('SELECT id,name FROM drivers WHERE id=?',(did,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Motorista não encontrado.',404)
            linked_routes=db.execute('SELECT COUNT(*) c FROM routes WHERE driver_id=?',(did,)).fetchone()['c']
            linked_orders=db.execute('SELECT COUNT(*) c FROM orders WHERE driver_id=?',(did,)).fetchone()['c']
            if linked_routes or linked_orders:
                return self.fail(
                    u,
                    'Exclusão bloqueada',
                    'Este motorista possui vínculo com pedidos/cargas e não pode ser apagado. Use Inativar para preservar o histórico.',
                    400,
                )
            db.execute('DELETE FROM drivers WHERE id=?',(did,))
            audit(db,u,'Apagou motorista','Motoristas',row['name'])
            db.commit()
        self.redirect('/drivers')
    def vehicles(self,u):
        qs=parse_qs(urlparse(self.path).query); edit_id=qs.get('edit',[''])[0].strip()
        with conn() as db:
            rows=db.execute('SELECT * FROM vehicles ORDER BY active DESC,name').fetchall()
            edit_row=db.execute('SELECT * FROM vehicles WHERE id=?',(parse_int(edit_id),)).fetchone() if edit_id.isdigit() else None
        active_rows = [r for r in rows if int(r['active'] or 0) == 1]
        inactive_rows = [r for r in rows if int(r['active'] or 0) != 1]
        type_suggestions = datalist_options([r['type'] for r in rows] + ['Baú', 'Truck', 'Toco', 'Carreta', 'VUC', 'Utilitário'])
        can_manage = self.has_perm(u,'manage_vehicles')
        can_delete_catalog = can_manage and can_manage_catalog_deletions(u)
        form=''
        if can_manage:
            form='<form method="post" action="/vehicles" class="inline-form"><input name="name" placeholder="Veículo" required><input name="plate" placeholder="Placa"><input name="type" list="vehicleTypeSuggestions" placeholder="Tipo"><input name="capacity" placeholder="Capacidade kg"><button>Adicionar</button></form>'
        edit_panel=''
        if can_manage and edit_row:
            edit_panel=f'''<section class="panel"><h2>Editar veículo</h2><form method="post" action="/vehicles/{edit_row["id"]}/update" class="inline-form"><input name="name" required value="{esc(edit_row["name"])}"><input name="plate" value="{esc(edit_row["plate"] or "")}"><input name="type" list="vehicleTypeSuggestions" value="{esc(edit_row["type"] or "")}"><input name="capacity" value="{esc(edit_row["capacity_kg"] if edit_row["capacity_kg"] is not None else (edit_row["capacity"] or ""))}"><button>Salvar</button><a class="btn ghost" href="/vehicles">Cancelar</a></form></section>'''
        def render_rows(source_rows):
            body_rows=''
            for r in source_rows:
                if can_manage:
                    actions=f'''<a class="btn small ghost" href="/vehicles?edit={r["id"]}">Editar</a>
                                '''
                    if can_delete_catalog:
                        toggle_label='Inativar' if int(r['active'] or 0) == 1 else 'Reativar'
                        toggle_class='danger-btn small' if int(r['active'] or 0) == 1 else 'small'
                        actions += f'''<form method="post" action="/vehicles/{r["id"]}/toggle" class="inline-mini needs-confirm" data-confirm-text="Confirma {toggle_label.lower()} este veículo?"><button class="{toggle_class}">{toggle_label}</button></form>
                                <form method="post" action="/vehicles/{r["id"]}/delete" class="inline-mini needs-confirm" data-confirm-text="Confirma apagar este veículo definitivamente?"><button class="danger-btn small">Apagar</button></form>'''
                else:
                    actions='<span class="muted">Somente visualização</span>'
                capacity_value = r["capacity_kg"] if r["capacity_kg"] is not None else r["capacity"]
                body_rows += f'<tr><td><b>{esc(r["name"])}</b></td><td>{esc(r["plate"] or "—")}</td><td>{esc(r["type"] or "—")}</td><td><div class="capacity mini"><span style="width:100%"></span></div>{fmt_num(capacity_value)} kg</td><td>{"Ativo" if r["active"] else "Inativo"}</td><td>{actions}</td></tr>'
            return body_rows or '<tr><td colspan="6">Nenhum veículo cadastrado.</td></tr>'
        body = render_rows(active_rows)
        inactive_body = render_rows(inactive_rows)
        inactive_panel = ''
        if inactive_rows:
            inactive_panel = f'''<details class="route-section route-archive"><summary><div><h2>Histórico de veículos inativos</h2><p>Veículos sem operação ativa.</p></div><span class="badge neutral">{len(inactive_rows)} inativo(s)</span></summary><div class="table-wrap"><table><thead><tr><th>Nome</th><th>Placa</th><th>Tipo</th><th>Capacidade</th><th>Status</th><th>Ações</th></tr></thead><tbody>{inactive_body}</tbody></table></div></details>'''
        datalist = f'<datalist id="vehicleTypeSuggestions">{type_suggestions}</datalist>'
        return self.send_html(layout(u,'Veículos',form+edit_panel+datalist+f'<div class="table-wrap"><table><thead><tr><th>Nome</th><th>Placa</th><th>Tipo</th><th>Capacidade</th><th>Status</th><th>Ações</th></tr></thead><tbody>{body}</tbody></table></div>{inactive_panel}','Controle de capacidade por veículo'))
    def post_vehicle(self,u):
        d=self.post_data();
        name=(d.get('name') or '').strip()
        if not name:
            raise ValueError('Informe o nome do veículo.')
        plate=(d.get('plate') or '').strip().upper()
        if not plate:
            raise ValueError('Informe a placa do veículo.')
        cap=parse_float(d.get('capacity') or 0)
        if cap < 0:
            raise ValueError('Capacidade do veículo não pode ser negativa.')
        with conn() as db:
            dup_plate=db.execute('SELECT id,name FROM vehicles WHERE UPPER(COALESCE(plate,""))=? LIMIT 1',(plate,)).fetchone()
            if dup_plate:
                raise ValueError(f'Placa duplicada: já existe no veículo {dup_plate["name"]}.')
            db.execute('INSERT INTO vehicles(name,plate,type,capacity,capacity_kg,active,updated_at,version) VALUES(?,?,?,?,?,1,?,1)',(name,plate,(d.get('type') or '').strip(),str(cap),cap,now())); audit(db,u,'Criou veículo','Veículos',name); db.commit()
        self.redirect('/vehicles')
    def post_vehicle_update(self,u,vid):
        d=self.post_data();
        name=(d.get('name') or '').strip()
        if not name:
            raise ValueError('Informe o nome do veículo.')
        plate=(d.get('plate') or '').strip().upper()
        if not plate:
            raise ValueError('Informe a placa do veículo.')
        cap=parse_float(d.get('capacity') or 0)
        if cap < 0:
            raise ValueError('Capacidade do veículo não pode ser negativa.')
        with conn() as db:
            row=db.execute('SELECT * FROM vehicles WHERE id=?',(vid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Veículo não encontrado.',404)
            dup_plate=db.execute('SELECT id,name FROM vehicles WHERE UPPER(COALESCE(plate,""))=? AND id<>? LIMIT 1',(plate,vid)).fetchone()
            if dup_plate:
                raise ValueError(f'Placa duplicada: já existe no veículo {dup_plate["name"]}.')
            db.execute('UPDATE vehicles SET name=?,plate=?,type=?,capacity=?,capacity_kg=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(name,plate,(d.get('type') or '').strip(),str(cap),cap,now(),vid))
            audit(db,u,'Editou veículo','Veículos',row['name'],row['name'],name)
            db.commit()
        self.redirect('/vehicles')
    def post_vehicle_toggle(self,u,vid):
        if not can_manage_catalog_deletions(u):
            return self.fail(u,'Ação bloqueada','Seu perfil pode cadastrar e editar veículos, mas não pode inativar/reativar.',403)
        with conn() as db:
            row=db.execute('SELECT id,name,active FROM vehicles WHERE id=?',(vid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Veículo não encontrado.',404)
            target = 0 if int(row['active'] or 0) == 1 else 1
            if target == 0:
                active_routes=db.execute("SELECT COUNT(*) c FROM routes WHERE vehicle_id=? AND status IN ('Planejada','Em rota')",(vid,)).fetchone()['c']
                if active_routes:
                    return self.fail(u,'Inativação bloqueada',f'O veículo está vinculado a {active_routes} carga(s) ativa(s). Finalize/cancele as cargas antes de inativar.',400)
            db.execute('UPDATE vehicles SET active=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(target,now(),vid))
            audit(db,u,'Alterou status de veículo','Veículos',row['name'],str(row['active']),str(target))
            db.commit()
        self.redirect('/vehicles')

    def post_vehicle_delete(self,u,vid):
        if not can_manage_catalog_deletions(u):
            return self.fail(u,'Ação bloqueada','Seu perfil pode cadastrar e editar veículos, mas não pode excluir.',403)
        with conn() as db:
            row=db.execute('SELECT id,name,plate FROM vehicles WHERE id=?',(vid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Veículo não encontrado.',404)
            linked_routes=db.execute('SELECT COUNT(*) c FROM routes WHERE vehicle_id=?',(vid,)).fetchone()['c']
            linked_orders=db.execute('SELECT COUNT(*) c FROM orders WHERE vehicle_id=?',(vid,)).fetchone()['c']
            if linked_routes or linked_orders:
                return self.fail(
                    u,
                    'Exclusão bloqueada',
                    'Este veículo possui vínculo com pedidos/cargas e não pode ser apagado. Use Inativar para preservar o histórico.',
                    400,
                )
            db.execute('DELETE FROM vehicles WHERE id=?',(vid,))
            audit(db,u,'Apagou veículo','Veículos',f'{row["name"]} {row["plate"] or ""}'.strip())
            db.commit()
        self.redirect('/vehicles')

    def route_city_exists(self, db, route_name, city, exclude_id=None):
        route_key = normalized_text_key(route_name)
        city_key = normalized_text_key(city)
        if not route_key or not city_key:
            return None
        for r in db.execute('SELECT id,route_name,city,active FROM route_cities').fetchall():
            if exclude_id and int(r['id']) == int(exclude_id):
                continue
            if normalized_text_key(r['route_name']) == route_key and normalized_text_key(r['city']) == city_key:
                return r
        return None

    def route_cities(self,u):
        qs=parse_qs(urlparse(self.path).query); q=qs.get('q',[''])[0].strip(); edit_id=qs.get('edit',[''])[0].strip()
        sql='SELECT * FROM route_cities WHERE 1=1'; p=[]
        if q:
            like=f'%{q}%'
            sql += ' AND (route_name LIKE ? OR city LIKE ? OR uf LIKE ? OR notes LIKE ?)'
            p += [like, like, like, like]
        sql += ' ORDER BY active DESC, route_name, delivery_order, city'
        with conn() as db:
            rows=db.execute(sql,p).fetchall()
            edit_row=db.execute('SELECT * FROM route_cities WHERE id=?',(parse_int(edit_id),)).fetchone() if edit_id.isdigit() else None
        route_suggestions = datalist_options([r['route_name'] for r in rows])
        city_suggestions = datalist_options([r['city'] for r in rows])
        active_rows = [r for r in rows if int(r['active'] or 0) == 1]
        inactive_rows = [r for r in rows if int(r['active'] or 0) != 1]
        can_manage = self.has_perm(u,'manage_route_catalog')
        can_delete_catalog = can_manage and can_manage_catalog_deletions(u)
        search=f'''<form class="filters"><input name="q" placeholder="Buscar rota, cidade, UF" value="{esc(q)}"><button>Buscar</button></form>'''
        add_form=''
        if can_manage:
            add_form=f'''<form method="post" action="/route-cities" class="form compact"><h2>Nova cidade/rota-base</h2><div class="grid3"><label>Rota *<input name="route_name" list="routeCatalogSuggestions" required placeholder="Selecione ou digite"></label><label>Cidade *<input name="city" list="cityCatalogSuggestions" required placeholder="Selecione ou digite"></label><label>UF<input name="uf" maxlength="2" placeholder="Ex: SP"></label><label>Ordem de entrega<input name="delivery_order" type="number" min="1" value="1"></label><label class="full">Observações<textarea name="notes"></textarea></label></div><button>Adicionar vínculo</button></form>'''
        edit_panel=''
        if can_manage and edit_row:
            edit_panel=f'''<section class="panel"><h2>Editar cidade/rota-base</h2><form method="post" action="/route-cities/{edit_row["id"]}/update" class="form compact"><div class="grid3"><label>Rota *<input name="route_name" list="routeCatalogSuggestions" required value="{esc(edit_row["route_name"] or "")}"></label><label>Cidade *<input name="city" list="cityCatalogSuggestions" required value="{esc(edit_row["city"] or "")}"></label><label>UF<input name="uf" maxlength="2" value="{esc(edit_row["uf"] or "")}"></label><label>Ordem de entrega<input name="delivery_order" type="number" min="1" value="{parse_int(edit_row["delivery_order"] or 1,1)}"></label><label class="full">Observações<textarea name="notes">{esc(edit_row["notes"] or "")}</textarea></label></div><button>Salvar</button><a class="btn ghost" href="/route-cities">Cancelar</a></form></section>'''
        def render_rows(source_rows):
            body_rows=''
            for r in source_rows:
                if can_manage:
                    actions=f'''<a class="btn small ghost" href="/route-cities?edit={r["id"]}">Editar</a>
                                '''
                    if can_delete_catalog:
                        toggle_label='Inativar' if int(r['active'] or 0) == 1 else 'Reativar'
                        toggle_class='danger-btn small' if int(r['active'] or 0) == 1 else 'small'
                        actions += f'''<form method="post" action="/route-cities/{r["id"]}/toggle" class="inline-mini needs-confirm" data-confirm-text="Confirma {toggle_label.lower()} este vínculo de cidade/rota?"><button class="{toggle_class}">{toggle_label}</button></form>
                                <form method="post" action="/route-cities/{r["id"]}/delete" class="inline-mini needs-confirm" data-confirm-text="Confirma apagar este vínculo definitivamente?"><button class="danger-btn small">Apagar</button></form>'''
                else:
                    actions='<span class="muted">Somente visualização</span>'
                body_rows += f'<tr><td>{esc(r["route_name"] or "—")}</td><td>{esc(r["city"] or "—")}</td><td>{esc((r["uf"] or "").upper())}</td><td>{parse_int(r["delivery_order"] or 0)}</td><td>{esc(r["notes"] or "")}</td><td>{"Ativo" if r["active"] else "Inativo"}</td><td>{actions}</td></tr>'
            return body_rows or '<tr><td colspan="7">Nenhum vínculo cadastrado.</td></tr>'
        body = render_rows(active_rows)
        inactive_body = render_rows(inactive_rows)
        inactive_panel = ''
        if inactive_rows:
            inactive_panel = f'''<details class="route-section route-archive"><summary><div><h2>Histórico de vínculos inativos</h2><p>Vínculos antigos removidos da operação atual.</p></div><span class="badge neutral">{len(inactive_rows)} inativo(s)</span></summary><div class="table-wrap"><table><thead><tr><th>Rota</th><th>Cidade</th><th>UF</th><th>Ordem</th><th>Observações</th><th>Status</th><th>Ações</th></tr></thead><tbody>{inactive_body}</tbody></table></div></details>'''
        datalists = f'<datalist id="routeCatalogSuggestions">{route_suggestions}</datalist><datalist id="cityCatalogSuggestions">{city_suggestions}</datalist>'
        return self.send_html(layout(u,'Cidades e Rotas-base',search+add_form+edit_panel+datalists+f'<div class="table-wrap"><table><thead><tr><th>Rota</th><th>Cidade</th><th>UF</th><th>Ordem</th><th>Observações</th><th>Status</th><th>Ações</th></tr></thead><tbody>{body}</tbody></table></div>{inactive_panel}','Cadastros operacionais para preencher rota/cidade em pedidos e cargas'))

    def post_route_city(self,u):
        d=self.post_data()
        route_name=upper_text(d.get('route_name'))
        city=upper_text(d.get('city'))
        if not route_name or not city:
            raise ValueError('Informe rota e cidade.')
        uf=(d.get('uf') or '').strip().upper()
        if uf and len(uf) != 2:
            raise ValueError('UF deve ter 2 caracteres.')
        seq=max(1,parse_int(d.get('delivery_order') or 1,1))
        with conn() as db:
            dup=self.route_city_exists(db,route_name,city)
            if dup:
                status='ativo' if int(dup['active'] or 0) == 1 else 'inativo'
                raise ValueError(f'Cidade/rota já cadastrada ({status}).')
            db.execute('INSERT INTO route_cities(route_name,city,uf,delivery_order,active,notes,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,1)',(route_name,city,uf,seq,1,(d.get('notes') or '').strip(),now(),now()))
            audit(db,u,'Criou cidade/rota-base','Cidades/Rotas',f'{route_name} | {city}')
            db.commit()
        self.redirect('/route-cities')

    def post_route_city_update(self,u,rcid):
        d=self.post_data()
        route_name=upper_text(d.get('route_name'))
        city=upper_text(d.get('city'))
        if not route_name or not city:
            raise ValueError('Informe rota e cidade.')
        uf=(d.get('uf') or '').strip().upper()
        if uf and len(uf) != 2:
            raise ValueError('UF deve ter 2 caracteres.')
        seq=max(1,parse_int(d.get('delivery_order') or 1,1))
        with conn() as db:
            row=db.execute('SELECT * FROM route_cities WHERE id=?',(rcid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Vínculo cidade/rota não encontrado.',404)
            dup=self.route_city_exists(db,route_name,city,exclude_id=rcid)
            if dup:
                raise ValueError('Já existe outro vínculo com a mesma rota e cidade.')
            db.execute('UPDATE route_cities SET route_name=?,city=?,uf=?,delivery_order=?,notes=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(route_name,city,uf,seq,(d.get('notes') or '').strip(),now(),rcid))
            audit(db,u,'Editou cidade/rota-base','Cidades/Rotas',f'{row["route_name"]} | {row["city"]}',f'{route_name} | {city}')
            db.commit()
        self.redirect('/route-cities')

    def post_route_city_toggle(self,u,rcid):
        if not can_manage_catalog_deletions(u):
            return self.fail(u,'Ação bloqueada','Seu perfil pode cadastrar e editar cidades/rotas-base, mas não pode inativar/reativar.',403)
        with conn() as db:
            row=db.execute('SELECT * FROM route_cities WHERE id=?',(rcid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Vínculo cidade/rota não encontrado.',404)
            target = 0 if int(row['active'] or 0) == 1 else 1
            if target == 0:
                open_orders=db.execute("""SELECT COUNT(*) c FROM orders
                                          WHERE COALESCE(route_name,'')=? AND COALESCE(city,'')=?
                                          AND status NOT IN ('Acertado','Problema','Cancelado')""",(row['route_name'] or '',row['city'] or '')).fetchone()['c']
                if open_orders:
                    return self.fail(u,'Inativação bloqueada',f'Existem {open_orders} pedido(s) em aberto usando esta rota/cidade.',400)
            db.execute('UPDATE route_cities SET active=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?',(target,now(),rcid))
            audit(db,u,'Alterou status de cidade/rota-base','Cidades/Rotas',f'{row["route_name"]} | {row["city"]}',str(row['active']),str(target))
            db.commit()
        self.redirect('/route-cities')

    def post_route_city_delete(self,u,rcid):
        if not can_manage_catalog_deletions(u):
            return self.fail(u,'Ação bloqueada','Seu perfil pode cadastrar e editar cidades/rotas-base, mas não pode excluir.',403)
        with conn() as db:
            row=db.execute('SELECT * FROM route_cities WHERE id=?',(rcid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Vínculo cidade/rota não encontrado.',404)
            route_name=(row['route_name'] or '').strip()
            city=(row['city'] or '').strip()
            linked_orders=db.execute("""SELECT COUNT(*) c
                                        FROM orders
                                        WHERE LOWER(TRIM(COALESCE(route_name,'')))=LOWER(TRIM(?))
                                          AND LOWER(TRIM(COALESCE(city,'')))=LOWER(TRIM(?))""",(route_name,city)).fetchone()['c']
            linked_routes=db.execute("""SELECT COUNT(*) c
                                        FROM routes
                                        WHERE LOWER(TRIM(COALESCE(route_name,'')))=LOWER(TRIM(?))""",(route_name,)).fetchone()['c']
            if linked_orders or linked_routes:
                return self.fail(
                    u,
                    'Exclusão bloqueada',
                    'Este vínculo rota/cidade já foi utilizado e não pode ser apagado. Use Inativar para impedir novos usos.',
                    400,
                )
            db.execute('DELETE FROM route_cities WHERE id=?',(rcid,))
            audit(db,u,'Apagou cidade/rota-base','Cidades/Rotas',f'{route_name} | {city}')
            db.commit()
        self.redirect('/route-cities')

    def relatorios(self,u):
        qs=parse_qs(urlparse(self.path).query)
        start,end,route_filter,status_filter=self.report_filter_values(qs)
        can_view_financial = self.can_view_financial(u)
        where_sql,p=self.report_where(start,end,route_filter,status_filter,alias='o')
        with conn() as db:
            route_names=[r['route_name'] for r in db.execute('SELECT DISTINCT route_name FROM orders WHERE route_name IS NOT NULL AND route_name<>"" ORDER BY route_name')]
            st=db.execute(f'SELECT o.status,COUNT(*) c,COALESCE(SUM(o.weight_kg),0) w,COALESCE(SUM(o.total_value),0) v FROM orders o {where_sql} GROUP BY o.status ORDER BY c DESC',p).fetchall()
            rt=db.execute(f'SELECT COALESCE(o.route_name,"Sem rota") route_name,COUNT(*) c,COALESCE(SUM(o.weight_kg),0) w,COALESCE(SUM(o.total_value),0) v FROM orders o {where_sql} GROUP BY o.route_name ORDER BY c DESC',p).fetchall()
            city=db.execute(f'SELECT COALESCE(o.city,"Sem cidade") city,COUNT(*) c,COALESCE(SUM(o.weight_kg),0) w,COALESCE(SUM(o.total_value),0) v FROM orders o {where_sql} GROUP BY o.city ORDER BY c DESC',p).fetchall()
            dr=db.execute(f"""SELECT
                                COALESCE(d.name, od.name, 'Sem motorista') driver,
                                COUNT(o.id) c,
                                COALESCE(SUM(o.weight_kg),0) w,
                                COALESCE(SUM(o.total_value),0) v,
                                SUM(CASE
                                        WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL
                                         AND NULLIF(o.sale_date,'') IS NOT NULL
                                        THEN 1 ELSE 0
                                    END) delivered_count,
                                ROUND(AVG(CASE
                                            WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL
                                             AND NULLIF(o.sale_date,'') IS NOT NULL
                                            THEN julianday(substr(o.delivered_at,1,10)) - julianday(o.sale_date)
                                          END),1) avg_days,
                                ROUND(COALESCE(SUM(CASE
                                            WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL
                                             AND NULLIF(o.sale_date,'') IS NOT NULL
                                            THEN julianday(substr(o.delivered_at,1,10)) - julianday(o.sale_date)
                                            ELSE 0
                                          END),0),1) total_days
                              FROM orders o
                              LEFT JOIN route_orders ro ON ro.order_id=o.id
                              LEFT JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada'
                              LEFT JOIN drivers d ON d.id=r.driver_id
                              LEFT JOIN drivers od ON od.id=o.driver_id
                              {where_sql}
                              GROUP BY COALESCE(d.name, od.name, 'Sem motorista')
                              ORDER BY c DESC""",p).fetchall()
            sla=db.execute(f"""SELECT
                                SUM(CASE WHEN o.status='Acertado' AND substr(COALESCE(o.delivered_at,''),1,10)<=o.expected_delivery_date THEN 1 ELSE 0 END) within_sla,
                                SUM(CASE WHEN o.status='Acertado' AND substr(COALESCE(o.delivered_at,''),1,10)>o.expected_delivery_date THEN 1 ELSE 0 END) outside_sla,
                                SUM(CASE WHEN o.status NOT IN ('Acertado','Problema','Cancelado') AND o.expected_delivery_date<? THEN 1 ELSE 0 END) open_late
                                FROM orders o {where_sql}""",[today()]+p).fetchone()
            problems=db.execute(f"""SELECT COALESCE(dp.problem_type,'Sem motivo') problem_type, COUNT(*) c
                                     FROM delivery_problems dp
                                     JOIN orders o ON o.id=dp.order_id
                                     {where_sql}
                                     GROUP BY dp.problem_type
                                     ORDER BY c DESC""",p).fetchall()
            deliveries=self.report_orders_rows(db,start,end,route_filter,status_filter,limit=80)
        srows=''.join(f'<tr><td>{badge(r["status"])}</td><td>{r["c"]}</td><td>{fmt_num(r["w"])} kg</td><td>{money_visible(can_view_financial, r["v"])}</td></tr>' for r in st) or '<tr><td colspan="4">Sem dados.</td></tr>'
        rrows=''.join(f'<tr><td>{esc(r["route_name"])}</td><td>{r["c"]}</td><td>{fmt_num(r["w"])} kg</td><td>{money_visible(can_view_financial, r["v"])}</td></tr>' for r in rt) or '<tr><td colspan="4">Sem dados.</td></tr>'
        city_rows=''.join(f'<tr><td>{esc(r["city"])}</td><td>{r["c"]}</td><td>{fmt_num(r["w"])} kg</td><td>{money_visible(can_view_financial, r["v"])}</td></tr>' for r in city) or '<tr><td colspan="4">Sem dados.</td></tr>'
        drows=''.join(
            f'<tr><td>{esc(r["driver"])}</td><td>{r["c"]}</td><td>{fmt_num(r["w"])} kg</td><td>{money_visible(can_view_financial, r["v"])}</td><td>{fmt_num(r["avg_days"],1) if r["avg_days"] is not None else "—"} dia(s)</td><td>{fmt_num(r["total_days"],1)} dia(s)</td></tr>'
            for r in dr
        ) or '<tr><td colspan="6">Sem dados.</td></tr>'
        prob_rows=''.join(f'<tr><td>{esc(r["problem_type"])}</td><td>{r["c"]}</td></tr>' for r in problems) or '<tr><td colspan="2">Sem problemas no filtro.</td></tr>'
        del_rows=''.join(
            f'<tr><td><a class="btn small ghost" href="/orders/{r["id"]}">{esc(r["order_number"])}</a></td><td>{esc(r["seller_name"] or "Não informado")}</td><td>{badge(r["status"])}</td><td>{esc(r["route_name"] or "Sem rota")} · {esc(r["city"] or "Sem cidade")}</td><td>{esc(r["driver"] or "Sem motorista")}</td><td>{fmt_num(r["weight_kg"])} kg</td><td>{money_visible(can_view_financial, r["total_value"])}</td><td>{brdate(r["delivered_at"])}</td><td>{(str(r["days_to_deliver"]) + " dia(s)") if r["days_to_deliver"] is not None else "—"}</td><td>{deadline_pill(r["expected_delivery_date"],r["status"])}</td></tr>'
            for r in deliveries
        ) or '<tr><td colspan="10">Sem entregas no filtro.</td></tr>'
        route_opts=option(route_names,route_filter,True,'Todas as rotas')
        status_opts=option(STATUSES,status_filter,True,'Todos os status')
        form=f'<form class="filters"><input type="date" name="start" value="{esc(start)}"><input type="date" name="end" value="{esc(end)}"><select name="route">{route_opts}</select><select name="status">{status_opts}</select><button>Filtrar</button><a class="btn ghost" href="/relatorios/export?start={esc(start)}&end={esc(end)}&route={quote(route_filter)}&status={quote(status_filter)}">Exportar CSV</a></form>'
        sla_panel=f"""<section class="panel"><h2>Relatório de SLA</h2><div class="cards"><div class="card done"><small>Dentro do prazo</small><strong>{sla['within_sla'] or 0}</strong></div><div class="card danger"><small>Fora do prazo</small><strong>{sla['outside_sla'] or 0}</strong></div><div class="card warn"><small>Abertos atrasados</small><strong>{sla['open_late'] or 0}</strong></div></div></section>"""
        
        accordions = f"""
        <details class='accordion-panel'>
          <summary>Relatório por Status</summary>
          <div class='accordion-content'>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Status</th><th>Qtd</th><th>Peso</th><th>Valor</th></tr></thead>
                <tbody>{srows}</tbody>
              </table>
            </div>
          </div>
        </details>

        <details class='accordion-panel'>
          <summary>Relatório por Rota e Cidade</summary>
          <div class='accordion-content'>
            <div class="dash-grid">
              <section class="panel" style="margin: 0; padding: 0; border: none; box-shadow: none;">
                <h2>Relatório por rota</h2>
                <div class="table-wrap">
                  <table>
                    <thead><tr><th>Rota</th><th>Qtd</th><th>Peso</th><th>Valor</th></tr></thead>
                    <tbody>{rrows}</tbody>
                  </table>
                </div>
              </section>
              <section class="panel" style="margin: 0; padding: 0; border: none; box-shadow: none;">
                <h2>Relatório por cidade</h2>
                <div class="table-wrap">
                  <table>
                    <thead><tr><th>Cidade</th><th>Qtd</th><th>Peso</th><th>Valor</th></tr></thead>
                    <tbody>{city_rows}</tbody>
                  </table>
                </div>
              </section>
            </div>
          </div>
        </details>

        <details class='accordion-panel'>
          <summary>Relatório por motorista</summary>
          <div class='accordion-content'>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Motorista</th><th>Pedidos</th><th>Peso</th><th>Valor</th><th>Média venda-entrega</th><th>Total dias</th></tr></thead>
                <tbody>{drows}</tbody>
              </table>
            </div>
          </div>
        </details>

        <details class='accordion-panel'>
          <summary>Relatório de Problemas e Ocorrências</summary>
          <div class='accordion-content'>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Problema</th><th>Qtd</th></tr></thead>
                <tbody>{prob_rows}</tbody>
              </table>
            </div>
          </div>
        </details>

        <details class='accordion-panel'>
          <summary>Relatório de entregas (Últimos 80 Pedidos)</summary>
          <div class='accordion-content'>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr><th>Pedido</th><th>Vendedor</th><th>Status</th><th>Rota/Cidade</th><th>Motorista</th><th>Peso</th><th>Valor</th><th>Entrega</th><th>Dias entrega</th><th>SLA</th></tr>
                </thead>
                <tbody>{del_rows}</tbody>
              </table>
            </div>
          </div>
        </details>
        """
        return self.send_html(layout(u,'Relatórios',form+sla_panel+accordions,'Relatórios operacionais com filtros e exportação CSV'))
    def export_csv(self,u):
        qs=parse_qs(urlparse(self.path).query)
        start,end,route_filter,status_filter=self.report_filter_values(qs)
        can_view_financial = self.can_view_financial(u)
        path=os.path.join(DATA_DIR,'relatorio_pedidos.csv')
        with conn() as db, open(path,'w',newline='',encoding='utf-8-sig') as f:
            w=csv.writer(f,delimiter=';'); w.writerow(['Pedido','Documento','Vendedor','Cliente','Cidade','Rota','Status','Peso_kg','Valor','Método_pagamento','Venda','Prazo_limite','Entrega','Dias_venda_entrega'])
            for r in self.report_orders_rows(db,start,end,route_filter,status_filter,limit=0):
                exported_value = r['total_value'] if can_view_financial else 'Oculto'
                w.writerow([r['order_number'],r['invoice_number'],r['seller_name'],r['client'],r['city'],r['route_name'],r['status'],r['weight_kg'],exported_value,r['payment_method'],r['sale_date'],r['expected_delivery_date'],r['delivered_at'],r['days_to_deliver'] if r['days_to_deliver'] is not None else ''])
        return self.send_file(path,'text/csv')
    def backup(self,u):
        if not self.has_perm(u,'view_backup'):
            return self.fail(u,'Acesso negado','Sem permissão para acessar backup.',403)
        os.makedirs(BACKUP_DIR, exist_ok=True)
        storage_label = 'SQLite' if DB_BACKEND == 'sqlite' else 'PostgreSQL'
        qs=parse_qs(urlparse(self.path).query)
        msg=''
        if qs.get('ok',[''])[0]:
            msg='<div class="alert success">Backup gerado com sucesso.</div>'
        if qs.get('err',[''])[0]:
            msg='<div class="alert danger">Falha ao gerar backup. Verifique permissões da pasta backups e tente novamente.</div>'
        if qs.get('restore_ok',[''])[0]:
            msg+='<div class="alert success">Backup restaurado com sucesso.</div>'
        if qs.get('restore_err',[''])[0]:
            msg+='<div class="alert danger">Falha ao restaurar backup. Confira o arquivo selecionado.</div>'
        auto = load_automation_status()
        latest = self.latest_backup_file_info()
        auto_backup_at = str(auto.get('last_auto_backup_at') or latest['mtime'] or '')
        auto_backup_file = str(auto.get('last_auto_backup_file') or latest['name'] or '')
        auto_verify_at = str(auto.get('last_auto_verify_at') or '')
        auto_verify_ok = auto.get('last_auto_verify_ok')
        auto_verify_text = 'Sem validação automática registrada'
        if auto_verify_ok is True:
            auto_verify_text = 'Validação semanal OK'
        elif auto_verify_ok is False:
            auto_verify_text = 'Validação semanal com falha'
        files=sorted([x for x in os.listdir(BACKUP_DIR) if x.endswith('.sqlite3')],reverse=True); lis=''.join(f'<li><b>{esc(x)}</b></li>' for x in files[:30]) or '<li>Nenhum backup ainda.</li>'
        guidance=f"""<div class='alert info'>
            Automação recomendada: executar <b>tools/install_windows_tasks.ps1</b> como administrador para ativar:
            inicialização automática, backup diário com retenção e teste semanal de restauração.
            <br>Último backup automático: <b>{esc(auto_backup_at or 'não identificado')}</b> · arquivo: <b>{esc(auto_backup_file or 'nenhum')}</b>.
            <br>{esc(auto_verify_text)} {f'({esc(auto_verify_at)})' if auto_verify_at else ''}.
            <br>Retenção ativa: o sistema mantém os <b>7 backups mais recentes</b> e remove os mais antigos automaticamente.
        </div>"""
        if DB_BACKEND != 'sqlite':
            guidance += """<div class='alert info'>
            Ambiente preparado para PostgreSQL: use rotina de <b>pg_dump</b> para backup externo até a migração total do runtime legado.
            </div>"""
        restore_form=''
        if self.has_perm(u,'restore_backup'):
            options=''.join(f'<option value="{esc(f)}">{esc(f)}</option>' for f in files[:100])
            restore_form=f"""<form method="post" action="/backup/restore" class="form compact needs-confirm" data-confirm-text="Confirma restaurar este backup? O banco atual será substituído após gerar um backup de segurança automático."><h2>Restaurar backup</h2><div class="grid3"><label>Arquivo de backup<select name="backup_file" required><option value="">Selecione</option>{options}</select></label><label>Confirmação forte<input name="confirm_text" placeholder="Digite RESTAURAR" required></label><label class="full">Motivo da restauração<input name="reason" placeholder="Ex: correção de falha operacional" required></label></div><button class="danger-btn">Restaurar agora</button></form>"""
        else:
            restore_form='<div class="alert info">Seu usuário não possui permissão de restauração de backup.</div>'
        return self.send_html(layout(u,'Backup',msg+f'<section class="panel"><h2>Backup do banco {esc(storage_label)}</h2><form method="post" action="/backup/create"><button>Gerar backup agora</button></form><p class="muted">O banco original foi preservado. Os backups ficam na pasta <b>backups</b>. Copie essa pasta para outro computador ou HD externo.</p>{guidance}<ul class="backup-list">{lis}</ul></section>{restore_form}','Proteção simples e local dos dados'))
    def post_backup(self,u):
        if not self.has_perm(u,'create_backup'):
            return self.fail(u,'Acesso negado','Sem permissão para gerar backup.',403)
        if DB_BACKEND != 'sqlite':
            return self.fail(u,'Operação não suportada','Backup interno automático desta tela está disponível no runtime SQLite. Para PostgreSQL use rotina pg_dump.',400)
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            name = create_sqlite_backup(DB_PATH, BACKUP_DIR)
            removed = prune_backup_files(BACKUP_DIR, keep_latest=7, extensions=('.sqlite3',))
            with conn() as db:
                audit(db,u,'Gerou backup','Backup',name,'',f'Retenção aplicada: {removed} antigo(s) removido(s)')
                db.commit()
            self.redirect('/backup?ok=1')
        except Exception as e:
            log_server_error('POST /backup/create', e)
            self.redirect('/backup?err=1')
    def post_backup_restore(self,u):
        if not self.has_perm(u,'restore_backup'):
            return self.fail(u,'Acesso negado','Sem permissão para restaurar backup.',403)
        if DB_BACKEND != 'sqlite':
            return self.fail(u,'Operação não suportada','Restauração interna desta tela está disponível no runtime SQLite. Para PostgreSQL use rotina de restore oficial.',400)
        d=self.post_data()
        backup_file=(d.get('backup_file') or '').strip()
        reason=(d.get('reason') or '').strip()
        confirm=(d.get('confirm_text') or '').strip().upper()
        if confirm != 'RESTAURAR':
            raise ValueError('Confirmação inválida. Digite RESTAURAR para prosseguir.')
        if not reason:
            raise ValueError('Informe o motivo da restauração.')
        if not backup_file or os.path.sep in backup_file or '/' in backup_file:
            raise ValueError('Arquivo de backup inválido.')
        src=os.path.join(BACKUP_DIR, backup_file)
        if not os.path.isfile(src) or not src.lower().endswith('.sqlite3'):
            raise ValueError('Backup selecionado não encontrado.')
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            _, safety_name = restore_sqlite_from_backup(DB_PATH, BACKUP_DIR, backup_file)
            prune_backup_files(BACKUP_DIR, keep_latest=7, extensions=('.sqlite3',))
            with conn() as db:
                audit(db,u,'Restaurou backup','Backup',backup_file,'',f'Motivo: {reason} | Segurança: {safety_name}')
                db.commit()
            self.redirect('/backup?restore_ok=1')
        except Exception as e:
            log_server_error('POST /backup/restore', e)
            self.redirect('/backup?restore_err=1')
    def settings(self,u):
        qs=parse_qs(urlparse(self.path).query)
        section=(qs.get('section',['profile'])[0] or 'profile').strip().lower()
        edit_uid=qs.get('edit_user',[''])[0]
        perm_role_raw=(qs.get('perm_role',['Operador'])[0] or 'Operador').strip()
        perm_role=normalize_role(perm_role_raw if perm_role_raw in ROLES else 'Operador')
        perm_uid=parse_int(qs.get('perm_user',['0'])[0],0)
        with conn() as db:
            settings={r['key']:r['value'] for r in db.execute('SELECT * FROM settings')}
            users=db.execute('SELECT * FROM users ORDER BY active DESC, role, name').fetchall()
            logs=db.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT 35').fetchall()
            edit_target=db.execute('SELECT * FROM users WHERE id=?',(int(edit_uid),)).fetchone() if str(edit_uid).isdigit() else None
            perm_user_target=db.execute('SELECT * FROM users WHERE id=?',(perm_uid,)).fetchone() if perm_uid > 0 else None
            role_perm_rows={r['perm']: int(r['allowed'] or 0) for r in db.execute('SELECT perm,allowed FROM role_permissions WHERE role_name=?',(perm_role,)).fetchall()}
            user_perm_rows={r['perm']: int(r['allowed'] or 0) for r in db.execute('SELECT perm,allowed FROM user_permissions WHERE user_id=?',(perm_uid,)).fetchall()} if perm_user_target else {}
        can_manage_config = self.has_perm(u,'manage_settings') and is_admin(u)
        can_manage_users = self.has_perm(u,'manage_users')
        can_manage_permissions = self.has_perm(u,'manage_permissions') and is_admin(u)
        can_view_audit = is_admin(u) and self.has_perm(u,'view_settings')
        allowed_sections = {'profile'}
        if can_manage_config:
            allowed_sections.add('system')
        if can_manage_users:
            allowed_sections.add('users')
        if can_manage_permissions:
            allowed_sections.add('permissions')
        if can_view_audit:
            allowed_sections.add('audit')
        if section not in allowed_sections:
            section = 'profile'
        tabs = [("profile", "Minha conta", "#settings-profile")]
        if can_manage_config:
            tabs.append(("system", "Parâmetros", "#settings-system"))
        if can_manage_users:
            tabs.append(("users", "Usuários", "#settings-users"))
        if can_manage_permissions:
            tabs.append(("permissions", "Permissões", "#settings-permissions"))
        if can_view_audit:
            tabs.append(("audit", "Auditoria", "#settings-audit"))
        tab_links = ''.join(
            f"""<a class='btn small {"ghost" if section!=key else ""}' href='/settings?section={key}{anchor}'>{label}</a>"""
            for key, label, anchor in tabs
        )
        settings_tabs=f"<section class='panel settings-tabs'>{tab_links}</section>"
        saved_msg = ''
        if qs.get('user_updated'):
            saved_msg = '<div class="alert success" style="margin-bottom:1rem;">✅ Usuário atualizado com sucesso!</div>'
        elif qs.get('profile_updated'):
            saved_msg = '<div class="alert success" style="margin-bottom:1rem;">✅ Dados da sua conta atualizados com sucesso!</div>'

        if is_restricted_data_entry_user(u):
            profile=f'''<section class="panel settings-panel" id="settings-profile"><h2>Minha conta</h2>{saved_msg if section=="profile" else ""}<form method="post" action="/settings/profile" class="inline-form"><input type="hidden" name="redirect_section" value="profile"><input type="hidden" name="name" value="{esc(u['name'])}"><input type="hidden" name="username" value="{esc(u['username'])}"><input name="password" type="password" placeholder="Nova senha" required><button>Alterar minha senha</button></form><p class="muted">Para seu perfil operacional, esta área permite somente troca de senha.</p><p class="muted">Último acesso: {brdate(u["last_login_at"])} {esc(str(u["last_login_at"] or "")[11:16]) if u["last_login_at"] else "—"}</p></section>'''
        else:
            profile=f'''<section class="panel settings-panel" id="settings-profile"><h2>Minha conta</h2>{saved_msg if section=="profile" else ""}<form method="post" action="/settings/profile" class="inline-form"><input type="hidden" name="redirect_section" value="profile"><input name="name" value="{esc(u['name'])}" placeholder="Nome" required><input name="username" value="{esc(u['username'])}" placeholder="Usuário" required><input name="password" type="password" placeholder="Nova senha opcional"><button>Atualizar minha conta</button></form><p class="muted">Último acesso: {brdate(u["last_login_at"])} {esc(str(u["last_login_at"] or "")[11:16]) if u["last_login_at"] else "—"}</p></section>'''
        content = settings_tabs + profile
        if not (can_manage_config or can_manage_users or can_manage_permissions or can_view_audit):
            return self.send_html(layout(u,'Configurações',content,'Ajustes da sua conta'))
        if can_manage_config:
            content += f'''<section class="panel settings-panel" id="settings-system"><h2>Parâmetros do sistema</h2><form method="post" action="/settings" class="form"><input type="hidden" name="redirect_section" value="system"><fieldset><legend>Identidade e operação</legend><div class="grid3"><label>Nome do sistema<input name="system_name" value="{esc(settings.get('system_name'))}"></label><label>Nome da empresa<input name="company_name" value="{esc(settings.get('company_name'))}"></label><label>Subtítulo<input name="company_subtitle" value="{esc(settings.get('company_subtitle'))}"></label><label>Cor principal<input type="color" name="primary_color" value="{esc(settings.get('primary_color'))}"></label><label>Cor secundária<input type="color" name="secondary_color" value="{esc(settings.get('secondary_color'))}"></label><label>Cor de apoio<input type="color" name="accent_color" value="{esc(settings.get('accent_color'))}"></label><label>Cor fundo<input type="color" name="background_color" value="{esc(settings.get('background_color','#f6f7f2'))}"></label><label>Prazo SLA oficial<input name="sla_limit_days" value="15" readonly><small>Travado em 15 dias corridos conforme regra operacional.</small></label><label>Capacidade padrão kg<input name="load_capacity_kg" value="{esc(settings.get('load_capacity_kg'))}"></label><label>Modo manutenção<select name="maintenance_mode"><option value="off" {'selected' if settings.get('maintenance_mode','off')!='on' else ''}>Desligado</option><option value="on" {'selected' if settings.get('maintenance_mode')=='on' else ''}>Ligado</option></select></label><label class="full">Observação interna<input name="god_note" value="{esc(settings.get('god_note',''))}" placeholder="Ex: revisão feita em..."></label></div></fieldset><button>Salvar configurações globais</button></form></section>'''
        if can_manage_users:
            userform=f'''<form method="post" action="/settings/user" class="inline-form"><input type="hidden" name="redirect_section" value="users"><input name="name" placeholder="Nome" required><input name="username" placeholder="Usuário" required><select name="role">{option(ROLES)}</select><button>Criar usuário</button></form>
            <div class="alert info">Ao criar usuário, a senha inicial será <b>usuario123</b> (ex.: joao = joao123) com troca obrigatória no primeiro login.</div>
            <form method="post" action="/settings/users/default-passwords" class="inline-form needs-confirm" data-confirm-text="Confirma aplicar senha padrão usuario123 para todos os usuários e exigir troca no próximo login?"><input type="hidden" name="redirect_section" value="users"><button class="danger-btn">Aplicar padrão em todos os usuários</button></form>'''
            edit_panel=''
            if edit_target:
                active_select = f'<select name="active"><option value="1" {"selected" if int(edit_target["active"] or 0)==1 else ""}>Ativo</option><option value="0" {"selected" if int(edit_target["active"] or 0)==0 else ""}>Inativo</option></select>'
                edit_panel=f'''<section class="panel" style="border:2px solid #2563eb;background:#f8fafc;margin-top:1rem;margin-bottom:1rem;"><h2>Editar usuário: {esc(edit_target["name"])} (ID: {edit_target["id"]})</h2><form method="post" action="/settings/user/{edit_target["id"]}/update" class="inline-form"><input type="hidden" name="redirect_section" value="users"><label style="font-weight:600;">Nome:<input name="name" value="{esc(edit_target["name"])}" required></label><label style="font-weight:600;">Login:<input name="username" value="{esc(edit_target["username"])}" required></label><label style="font-weight:600;">Perfil:<select name="role">{option(ROLES,edit_target["role"])}</select></label><label style="font-weight:600;">Status:{active_select}</label><label style="font-weight:600;">Nova senha:<input name="password" type="password" placeholder="Opcional"></label><button class="btn primary">💾 Salvar edição</button><a class="btn ghost" href="/settings?section=users#settings-users">Cancelar</a></form><small style="color:#64748b;display:block;margin-top:.4rem;">Não é permitido desativar o próprio usuário logado nem remover o último GOD ativo.</small></section>'''
            rows=''
            for x in users:
                is_self = (x['id'] == u['id'])
                edit_link = f'<a class="btn small ghost" href="/settings?section=users&edit_user={x["id"]}#settings-users">Editar</a>'
                if is_self:
                    action = f'''<div class="user-actions-stack">
                                 <div class="user-actions-top">
                                   {edit_link}
                                   <span class="muted small" style="font-size:0.8rem;align-self:center;">(Seu usuário)</span>
                                 </div>
                               </div>'''
                else:
                    action = f'''<div class="user-actions-stack">
                                 <div class="user-actions-top">
                                   {edit_link}
                                   <form method="post" action="/settings/user/{x["id"]}/delete" class="inline-mini needs-confirm" data-confirm-text="Confirma desativar este usuário?"><input type="hidden" name="redirect_section" value="users"><button class="danger-btn small">Desativar</button></form>
                                   <form method="post" action="/settings/user/{x["id"]}/purge" class="inline-mini needs-confirm" data-confirm-text="Confirma apagar este usuário definitivamente? Essa ação não pode ser desfeita."><input type="hidden" name="redirect_section" value="users"><button class="danger-btn small">Apagar definitivo</button></form>
                                 </div>
                                 <form method="post" action="/settings/user/{x["id"]}/reset-password" class="user-reset-form needs-confirm" data-confirm-text="Confirma resetar senha deste usuário?">
                                   <input type="hidden" name="redirect_section" value="users">
                                   <input name="new_password" placeholder="Nova senha" required>
                                   <label class="muted"><input type="checkbox" name="must_change_password" value="1" checked> Alterar senha no próximo login</label>
                                   <button class="small">Resetar senha</button>
                                 </form>
                               </div>'''
                pending_change = 0
                try:
                    pending_change = int(x['must_change_password'] or 0)
                except Exception:
                    pending_change = 0
                rows += f'<tr><td>{esc(x["name"])}</td><td>{esc(x["username"])}</td><td>{esc(normalize_role(x["role"]))}</td><td>{"Ativo" if x["active"] else "Inativo"}</td><td>{"Sim" if pending_change==1 else "Não"}</td><td>{brdate(x["last_login_at"])} {esc(str(x["last_login_at"] or "")[11:16]) if x["last_login_at"] else "—"}</td><td class="settings-user-actions">{action}</td></tr>'
            content += f'<section class="panel settings-panel" id="settings-users"><h2>Usuários</h2>{saved_msg if section=="users" else ""}{userform}{edit_panel}<div class="table-wrap"><table><thead><tr><th>Nome</th><th>Usuário</th><th>Perfil</th><th>Status</th><th>Troca senha pendente</th><th>Último acesso</th><th>Ações</th></tr></thead><tbody>{rows}</tbody></table></div></section>'

        if can_manage_permissions:
            role_links=' '.join(f'<a class="btn small {"ghost" if perm_role!=r else ""}" href="/settings?section=permissions&perm_role={quote(r)}#settings-permissions">{esc(r)}</a>' for r in ROLES)
            role_rows=''
            for perm_key, perm_label in PERMISSIONS:
                checked = 'checked' if int(role_perm_rows.get(perm_key, 1 if perm_key in default_permissions_for_role(perm_role) else 0)) == 1 else ''
                role_rows += f'<tr><td>{esc(perm_label)}</td><td><input type="checkbox" name="perm_{esc(perm_key)}" value="1" {checked}></td></tr>'
            role_panel=f'''<section class="panel"><h2>Permissões por perfil</h2><div class="action-strip">{role_links}</div><form method="post" action="/settings/permissions/role" class="form compact"><input type="hidden" name="redirect_section" value="permissions"><input type="hidden" name="role_name" value="{esc(perm_role)}"><div class="table-wrap"><table><thead><tr><th>Permissão</th><th>Liberado ({esc(perm_role)})</th></tr></thead><tbody>{role_rows}</tbody></table></div><button>Salvar permissões do perfil</button></form></section>'''
            user_links=' '.join(f'<a class="btn small {"ghost" if perm_uid!=x["id"] else ""}" href="/settings?section=permissions&perm_role={quote(perm_role)}&perm_user={x["id"]}#settings-permissions">{esc(x["username"])}</a>' for x in users[:20])
            user_panel=''
            if perm_user_target:
                user_rows=''
                for perm_key, perm_label in PERMISSIONS:
                    checked = 'checked' if int(user_perm_rows.get(perm_key, 0)) == 1 else ''
                    user_rows += f'<tr><td>{esc(perm_label)}</td><td><input type="checkbox" name="perm_{esc(perm_key)}" value="1" {checked}></td></tr>'
                user_panel=f'''<section class="panel"><h2>Permissões por usuário (override)</h2><p><b>{esc(perm_user_target["name"])}</b> · {esc(perm_user_target["username"])} · perfil {esc(normalize_role(perm_user_target["role"]))}</p><div class="action-strip">{user_links}</div><form method="post" action="/settings/permissions/user/{perm_user_target["id"]}/update" class="form compact"><input type="hidden" name="redirect_section" value="permissions"><input type="hidden" name="perm_role" value="{esc(perm_role)}"><div class="table-wrap"><table><thead><tr><th>Permissão</th><th>Override</th></tr></thead><tbody>{user_rows}</tbody></table></div><button>Salvar override do usuário</button></form><form method="post" action="/settings/permissions/user/{perm_user_target["id"]}/update" class="inline-form needs-confirm" data-confirm-text="Confirma limpar todos os overrides deste usuário?"><input type="hidden" name="redirect_section" value="permissions"><input type="hidden" name="perm_role" value="{esc(perm_role)}"><input type="hidden" name="reset_overrides" value="1"><button class="danger-btn">Restaurar padrão do perfil</button></form></section>'''
            else:
                user_panel=f'''<section class="panel"><h2>Permissões por usuário (override)</h2><div class="action-strip">{user_links}</div><p class="muted">Selecione um usuário para configurar override de permissões.</p></section>'''
            content += f'<section class="settings-stack" id="settings-permissions">{role_panel}{user_panel}</section>'
        if can_view_audit:
            logrows=''.join(
                f'<tr><td>{brdate(l["created_at"])} {esc(str(l["created_at"])[11:16])}</td><td>{esc(l["user_name"] or "Sistema")}<br><small>ID: {esc(l["user_id"] or "")}</small></td><td>{esc(l["module"])}</td><td>{esc(l["action"])}</td><td>{esc(l["entity"] or "")}</td><td>{esc(l["source_ip"] or "—")}</td><td><small>Antes: {esc(l["old_value"] or "—")}<br>Depois: {esc(l["new_value"] or "—")}<br>Obs: {esc(l["notes"] or "—")}</small></td></tr>'
                for l in logs
            ) or '<tr><td colspan="7">Sem auditoria ainda.</td></tr>'
            content += f'<section class="panel settings-panel" id="settings-audit"><h2>Auditoria recente</h2><div class="table-wrap"><table><thead><tr><th>Data</th><th>Usuário</th><th>Módulo</th><th>Ação</th><th>Registro</th><th>IP</th><th>Detalhes técnicos</th></tr></thead><tbody>{logrows}</tbody></table></div></section>'
        return self.send_html(layout(u,'Configurações',content,'Administração do sistema, usuários e permissões'))
    def post_settings(self,u):
        if not self.has_perm(u,'manage_settings') or not is_admin(u):
            return self.send_html(layout(u,'Acesso negado','<div class="alert danger">Somente perfis autorizados alteram configurações globais.</div>'),403)
        d=self.post_data(); keys=['system_name','company_name','company_subtitle','primary_color','secondary_color','accent_color','background_color','load_capacity_kg','maintenance_mode','god_note']
        redirect_section=(d.get('redirect_section') or 'system').strip().lower()
        load_capacity=parse_float(d.get('load_capacity_kg') or 0)
        if load_capacity <= 0:
            raise ValueError('Capacidade padrão deve ser maior que zero.')
        with conn() as db:
            for k in keys:
                val = d.get(k,'')
                if k == 'load_capacity_kg':
                    val = str(load_capacity)
                db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)',(k,val,now()))
            db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)',('sla_limit_days','15',now()))
            audit(db,u,'Alterou configurações','Configurações'); db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}#settings-{quote(redirect_section)}')
    def post_profile(self,u):
        d=self.post_data()
        redirect_section=(d.get('redirect_section') or 'profile').strip().lower()
        name=(d.get('name') or u['name']).strip()
        username=(d.get('username') or u['username']).strip()
        pwd=(d.get('password') or '').strip()
        if not name or not username:
            raise ValueError('Nome e usuário são obrigatórios.')
        if pwd:
            validate_password_strength(pwd)
        with conn() as db:
            dup=db.execute('SELECT id FROM users WHERE LOWER(username)=LOWER(?) AND id<>?',(username,u['id'])).fetchone()
            if dup:
                raise ValueError('Já existe outro usuário com este login.')
            if pwd: db.execute('UPDATE users SET name=?,username=?,password_hash=?,must_change_password=0 WHERE id=?',(name,username,hash_password(pwd),u['id']))
            else: db.execute('UPDATE users SET name=?,username=? WHERE id=?',(name,username,u['id']))
            audit(db,u,'Atualizou perfil','Configurações',username); db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}&profile_updated=1#settings-{quote(redirect_section)}')
    def post_user(self,u):
        if not self.has_perm(u,'manage_users'):
            return self.send_html(layout(u,'Acesso negado','<div class="alert danger">Sem permissão para criar usuários.</div>'),403)
        d=self.post_data(); role_raw=(d.get('role') or '').strip(); role=normalize_role(role_raw or 'Operador')
        redirect_section=(d.get('redirect_section') or 'users').strip().lower()
        name=(d.get('name') or '').strip(); username=(d.get('username') or '').strip()
        if not name or not username:
            raise ValueError('Nome e usuário são obrigatórios para criar usuário.')
        if role_raw and role_raw not in ROLES:
            raise ValueError('Perfil de usuário inválido.')
        if role == 'GOD' and not is_god(u):
            return self.fail(u,'Acesso negado','Somente usuário GOD pode criar outro usuário GOD.',403)
        default_pwd=default_initial_password(username)
        with conn() as db:
            dup=db.execute('SELECT id FROM users WHERE LOWER(username)=LOWER(?)',(username,)).fetchone()
            if dup:
                raise ValueError('Usuário duplicado: já existe um login com este nome.')
            db.execute('INSERT INTO users(name,username,password_hash,role,active,must_change_password,created_at) VALUES(?,?,?,?,1,1,?)',(name,username,hash_password(default_pwd),role,now()))
            audit(db,u,'Criou usuário','Configurações',username,'','',f'Senha padrão aplicada: {username}123 com troca obrigatória')
            db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}#settings-{quote(redirect_section)}')

    def post_user_update(self,u,uid):
        if not self.has_perm(u,'manage_users'):
            return self.fail(u,'Acesso negado','Sem permissão para editar usuários.',403)
        d=self.post_data()
        redirect_section=(d.get('redirect_section') or 'users').strip().lower()
        name=(d.get('name') or '').strip()
        username=(d.get('username') or '').strip()
        role_raw=(d.get('role') or '').strip()
        role=normalize_role(role_raw)
        active=1 if str(d.get('active','1'))=='1' else 0
        pwd=(d.get('password') or '').strip()
        if not name or not username:
            raise ValueError('Nome e usuário são obrigatórios.')
        if role_raw not in ROLES:
            raise ValueError('Perfil inválido.')
        if pwd:
            validate_password_strength(pwd)
        with conn() as db:
            target=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not target:
                return self.fail(u,'Não encontrado','Usuário não encontrado.',404)
            if normalize_role(target['role']) == 'GOD' and not is_god(u):
                return self.fail(u,'Acesso negado','Somente GOD pode editar usuários GOD.',403)
            if role == 'GOD' and not is_god(u):
                return self.fail(u,'Acesso negado','Somente GOD pode promover usuário para GOD.',403)
            dup=db.execute('SELECT id FROM users WHERE LOWER(username)=LOWER(?) AND id<>?',(username,uid)).fetchone()
            if dup:
                raise ValueError('Usuário duplicado: já existe um login com este nome.')
            if uid == u['id'] and active == 0:
                return self.fail(u,'Bloqueado','Você não pode desativar o próprio usuário logado.',400)
            if uid == u['id'] and normalize_role(u['role']) == 'GOD' and role != 'GOD':
                return self.fail(u,'Bloqueado','O usuário GOD logado não pode remover seu próprio perfil GOD.',400)
            if normalize_role(target['role'])=='GOD' and (role!='GOD' or active==0):
                gods=db.execute("SELECT COUNT(*) c FROM users WHERE role='GOD' AND active=1").fetchone()['c']
                if gods <= 1:
                    return self.fail(u,'Bloqueado','Não é permitido remover/desativar o último usuário GOD ativo.',400)
            if pwd:
                db.execute('UPDATE users SET name=?,username=?,role=?,active=?,password_hash=? WHERE id=?',(name,username,role,active,hash_password(pwd),uid))
            else:
                db.execute('UPDATE users SET name=?,username=?,role=?,active=? WHERE id=?',(name,username,role,active,uid))
            sessions_revoked = 0
            if active == 0:
                sessions_revoked = revoke_user_sessions(uid)
            audit(db,u,'Editou usuário','Configurações',target['username'],f'role={target["role"]}|active={target["active"]}',f'role={role}|active={active}')
            if sessions_revoked:
                audit(db,u,'Sessões encerradas por inativação','Configurações',target['username'],'',f'{sessions_revoked} sessão(ões) encerrada(s)')
            db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}&user_updated=1&edit_user={uid}#settings-{quote(redirect_section)}')


    def post_user_delete(self,u,uid):
        if not self.has_perm(u,'manage_users'):
            return self.send_html(layout(u,'Acesso negado','<div class="alert danger">Sem permissão para desativar usuários.</div>'),403)
        d=self.post_data()
        redirect_section=(d.get('redirect_section') or 'users').strip().lower()
        if uid == u['id']:
            return self.send_html(layout(u,'Bloqueado','<div class="alert danger">Você não pode excluir o próprio usuário logado.</div><a class="btn ghost" href="/settings">Voltar</a>'),400)
        with conn() as db:
            target=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not target:
                return self.send_html(layout(u,'Não encontrado','<div class="alert danger">Usuário não encontrado.</div>'),404)
            if normalize_role(target['role'])=='GOD':
                if not is_god(u):
                    return self.send_html(layout(u,'Acesso negado','<div class="alert danger">Somente GOD pode desativar usuário GOD.</div>'),403)
                gods=db.execute("SELECT COUNT(*) c FROM users WHERE role='GOD' AND active=1").fetchone()['c']
                if gods <= 1:
                    return self.send_html(layout(u,'Bloqueado','<div class="alert danger">Não é permitido remover o último usuário GOD ativo.</div><a class="btn ghost" href="/settings">Voltar</a>'),400)
            db.execute('UPDATE users SET active=0, username=username||"_excluido_"||id WHERE id=?',(uid,))
            sessions_revoked = revoke_user_sessions(uid)
            audit(db,u,'Excluiu/desativou usuário','Configurações',target['username'])
            if sessions_revoked:
                audit(db,u,'Sessões encerradas por desativação','Configurações',target['username'],'',f'{sessions_revoked} sessão(ões) encerrada(s)')
            db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}#settings-{quote(redirect_section)}')

    def post_user_purge(self,u,uid):
        if not self.has_perm(u,'manage_users'):
            return self.fail(u,'Acesso negado','Sem permissão para apagar usuários.',403)
        d=self.post_data()
        redirect_section=(d.get('redirect_section') or 'users').strip().lower()
        if uid == u['id']:
            return self.fail(u,'Bloqueado','Você não pode apagar o próprio usuário logado.',400)
        with conn() as db:
            target=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not target:
                return self.fail(u,'Não encontrado','Usuário não encontrado.',404)
            target_role=normalize_role(target['role'])
            if target_role=='GOD':
                if not is_god(u):
                    return self.fail(u,'Acesso negado','Somente GOD pode apagar usuário GOD.',403)
                gods=db.execute("SELECT COUNT(*) c FROM users WHERE role='GOD' AND active=1").fetchone()['c']
                if gods <= 1:
                    return self.fail(u,'Bloqueado','Não é permitido apagar o último usuário GOD ativo.',400)
            linked_orders=db.execute('SELECT COUNT(*) c FROM orders WHERE seller_id=?',(uid,)).fetchone()['c']
            linked_history=db.execute('SELECT COUNT(*) c FROM order_history WHERE user_id=?',(uid,)).fetchone()['c']
            if linked_orders or linked_history:
                return self.fail(
                    u,
                    'Exclusão bloqueada',
                    'Este usuário possui histórico operacional e não pode ser apagado definitivamente. Use Desativar para bloquear acesso.',
                    400,
                )
            db.execute('DELETE FROM user_permissions WHERE user_id=?',(uid,))
            db.execute('DELETE FROM users WHERE id=?',(uid,))
            sessions_revoked = revoke_user_sessions(uid)
            audit(db,u,'Apagou usuário','Configurações',target['username'])
            if sessions_revoked:
                audit(db,u,'Sessões encerradas por exclusão','Configurações',target['username'],'',f'{sessions_revoked} sessão(ões) encerrada(s)')
            db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}#settings-{quote(redirect_section)}')

    def post_users_default_passwords(self,u):
        if not self.has_perm(u,'manage_users'):
            return self.fail(u,'Acesso negado','Sem permissão para aplicar senha padrão.',403)
        d=self.post_data()
        redirect_section=(d.get('redirect_section') or 'users').strip().lower()
        changed=0
        revoked_total=0
        with conn() as db:
            users=db.execute('SELECT id,username FROM users').fetchall()
            for row in users:
                uname=(row['username'] or '').strip()
                if not uname:
                    continue
                default_pwd=default_initial_password(uname)
                db.execute(
                    'UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?',
                    (hash_password(default_pwd),row['id']),
                )
                if int(row['id']) != int(u['id']):
                    revoked_total += revoke_user_sessions(row['id'])
                changed += 1
            audit(
                db,
                u,
                'Aplicou senha padrão em massa',
                'Configurações',
                f'Usuários afetados: {changed}',
                '',
                '',
                f'Padrão: usuario123 e troca obrigatória no próximo login. Sessões encerradas: {revoked_total}',
            )
            db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}#settings-{quote(redirect_section)}')

    def post_user_reset_password(self,u,uid):
        if not self.has_perm(u,'manage_users'):
            return self.fail(u,'Acesso negado','Sem permissão para resetar senha de usuário.',403)
        d=self.post_data()
        redirect_section=(d.get('redirect_section') or 'users').strip().lower()
        new_password=(d.get('new_password') or '').strip()
        must_change_password = 1 if str(d.get('must_change_password') or '').strip().lower() in ('1','on','true','sim','yes') else 0
        if not new_password:
            raise ValueError('Informe a nova senha para reset.')
        validate_password_strength(new_password)
        with conn() as db:
            target=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not target:
                return self.fail(u,'Não encontrado','Usuário não encontrado.',404)
            if normalize_role(target['role']) == 'GOD' and not is_god(u):
                return self.fail(u,'Acesso negado','Somente GOD pode resetar senha de usuário GOD.',403)
            db.execute('UPDATE users SET password_hash=?,must_change_password=? WHERE id=?',(hash_password(new_password),must_change_password,uid))
            sessions_revoked = revoke_user_sessions(uid)
            audit(
                db,
                u,
                'Resetou senha',
                'Configurações',
                target['username'],
                '',
                '',
                'Troca obrigatória no próximo login: ' + ('sim' if must_change_password else 'não') + f'. Sessões encerradas: {sessions_revoked}',
            )
            db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}#settings-{quote(redirect_section)}')

    def post_role_permissions(self,u):
        if not self.has_perm(u,'manage_permissions') or not is_admin(u):
            return self.fail(u,'Acesso negado','Sem permissão para gerenciar permissões.',403)
        d=self.post_data()
        redirect_section=(d.get('redirect_section') or 'permissions').strip().lower()
        role_raw=(d.get('role_name') or '').strip()
        role=normalize_role(role_raw)
        if role not in ROLES:
            raise ValueError('Perfil inválido para atualização de permissões.')
        if role == 'GOD':
            return self.fail(u,'Operação bloqueada','Permissões do perfil GOD são totais e não podem ser reduzidas.',400)
        with conn() as db:
            for perm_key in PERMISSION_KEYS:
                allowed=1 if d.get(f'perm_{perm_key}') else 0
                db.execute('INSERT OR REPLACE INTO role_permissions(role_name,perm,allowed,updated_at) VALUES(?,?,?,?)',(role,perm_key,allowed,now()))
            audit(db,u,'Alterou permissões de perfil','Permissões',role)
            db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}&perm_role={quote(role)}#settings-permissions')

    def post_user_permissions(self,u,uid):
        if not self.has_perm(u,'manage_permissions') or not is_admin(u):
            return self.fail(u,'Acesso negado','Sem permissão para gerenciar permissões.',403)
        d=self.post_data()
        redirect_section=(d.get('redirect_section') or 'permissions').strip().lower()
        perm_role=normalize_role((d.get('perm_role') or 'Operador').strip() or 'Operador')
        with conn() as db:
            target=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not target:
                return self.fail(u,'Não encontrado','Usuário não encontrado.',404)
            if normalize_role(target['role']) == 'GOD' and not is_god(u):
                return self.fail(u,'Acesso negado','Somente GOD pode alterar permissões de usuário GOD.',403)
            if str(d.get('reset_overrides') or '') == '1':
                db.execute('DELETE FROM user_permissions WHERE user_id=?',(uid,))
                audit(db,u,'Limpou override de permissões','Permissões',target['username'])
                db.commit()
                return self.redirect(f'/settings?section={quote(redirect_section)}&perm_role={quote(perm_role)}&perm_user={uid}#settings-permissions')
            db.execute('DELETE FROM user_permissions WHERE user_id=?',(uid,))
            for perm_key in PERMISSION_KEYS:
                allowed=1 if d.get(f'perm_{perm_key}') else 0
                db.execute('INSERT INTO user_permissions(user_id,perm,allowed,updated_at) VALUES(?,?,?,?)',(uid,perm_key,allowed,now()))
            audit(db,u,'Alterou override de permissões','Permissões',target['username'])
            db.commit()
        self.redirect(f'/settings?section={quote(redirect_section)}&perm_role={quote(perm_role)}&perm_user={uid}#settings-permissions')

    # ---------------------------------------------------------------------------
    # INTEGRAÇÃO ERP - PAINEL ADMIN (Exclusivo GOD)
    # ---------------------------------------------------------------------------

    def get_erp_admin(self, u):
        if not is_god(u):
            return self.fail(u, 'Acesso negado', 'Esta tela é restrita ao Administrador GOD.', 403)

        cfg = _erp_connector.get_erp_config() if _ERP_AVAILABLE else None
        status_info = _erp_connector.get_sync_status() if _ERP_AVAILABLE else {}

        enabled = cfg.enabled if cfg else False
        driver = cfg.driver if cfg else 'oracle'
        host = cfg.host if cfg else ''
        port = cfg.port if cfg else 1521
        database = cfg.database if cfg else ''
        schema = cfg.schema if cfg else ''
        user_name = cfg.user if cfg else ''
        password = cfg.password if cfg else ''
        sync_min = cfg.sync_interval_min if cfg else 30
        sync_start_time = cfg.sync_start_time if cfg else '07:00'
        sync_end_time = cfg.sync_end_time if cfg else '19:00'
        sync_days = cfg.sync_days if cfg else 'seg_sab'
        sync_auto_enabled = cfg.sync_auto_enabled if cfg else True
        cache_days = cfg.cache_days if cfg else 30

        v_ped = cfg.view_pedidos if cfg else 'VW_PEDIDOS_CAD_LM'
        v_itens = cfg.view_itens if cfg else 'VW_ITENS_PEDIDO_CAD_LM'
        v_cli = cfg.view_clientes if cfg else 'VW_CLIENTES_CAD_LM'
        v_vend = cfg.view_vendedores if cfg else 'VW_VENDEDOR_CAD_LM'
        v_prod = cfg.view_produtos if cfg else 'VW_PRODUTOS_CAD_LM'
        v_fat = cfg.view_faturamento if cfg else 'VW_FATURAMENTO_CAD_LM'

        last_sync = status_info.get('last_sync_at', 'Nunca')
        cached_orders = status_info.get('cache_pedidos_count', 0)
        cached_fat = status_info.get('cache_fat_count', 0)
        cached_cli = status_info.get('cache_clientes_count', 0)
        sync_running = status_info.get('running', False)
        last_error = status_info.get('error', None)

        status_badge = '<span class="badge success" style="font-size:0.9rem;">✅ HABILITADO</span>' if enabled else '<span class="badge secondary" style="font-size:0.9rem;">⏸️ DESABILITADO</span>'
        _, sess = self.session_data()
        csrf_val = esc((sess or {}).get('csrf') or '')

        content = f'''
<style>
.tooltip-icon {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: #cbd5e1;
    color: #1e293b;
    font-size: 11px;
    font-weight: bold;
    cursor: help;
    margin-left: 5px;
    position: relative;
}}
.tooltip-icon:hover::after {{
    content: attr(data-tooltip);
    position: absolute;
    bottom: 130%;
    left: 50%;
    transform: translateX(-50%);
    background: #0f172a;
    color: #f8fafc;
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: normal;
    white-space: normal;
    width: 260px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.25);
    z-index: 9999;
    pointer-events: none;
    line-height: 1.4;
}}
.stat-card-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 1rem;
    margin-bottom: 1.5rem;
}}
.stat-card {{
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    padding: 1.2rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}}
.stat-card h4 {{
    font-size: 0.82rem;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 0 0 0.5rem 0;
}}
.stat-card .val {{
    font-size: 1.5rem;
    font-weight: 700;
    color: #0f172a;
}}

/* Barra de Progresso Real-time */
.progress-container {{
    margin-top: 1rem;
    background: #e2e8f0;
    border-radius: 8px;
    overflow: hidden;
    height: 22px;
    position: relative;
    box-shadow: inset 0 1px 3px rgba(0,0,0,0.15);
}}
.progress-bar-fill {{
    height: 100%;
    background: linear-gradient(90deg, #2563eb, #10b981);
    width: 0%;
    transition: width 0.3s ease;
    border-radius: 8px;
}}
.progress-text {{
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    font-size: 0.78rem;
    font-weight: bold;
    color: #0f172a;
    text-shadow: 0 0 2px rgba(255,255,255,0.8);
}}
</style>

<div class="panel settings-panel">
    <h2>⚡ Integração ERP (READ-ONLY) — Painel de Controle e Diagnóstico</h2>
    <p>Área exclusiva para administradores <b>GOD</b>. Configure a conexão com o ERP, monitore a sincronização com progresso em tempo real e consulte o cache SQLite local.</p>
</div>

<div class="stat-card-grid">
    <div class="stat-card">
        <h4>Status da Integração</h4>
        <div class="val">{status_badge}</div>
    </div>
    <div class="stat-card">
        <h4>Última Sincronização</h4>
        <div class="val" style="font-size:1.1rem;margin-top:.4rem;" id="lastSyncText">{esc(last_sync)}</div>
    </div>
    <div class="stat-card">
        <h4>Pedidos em Cache (30 dias)</h4>
        <div class="val" id="cardPedCount">{cached_orders}</div>
    </div>
    <div class="stat-card">
        <h4>Faturamentos / Clientes</h4>
        <div class="val" style="font-size:1.2rem;margin-top:.3rem;" id="cardFatCliCount">{cached_fat} NF / {cached_cli} Cli</div>
    </div>
</div>

<section class="panel" style="border: 2px solid #2563eb;margin-bottom:1.5rem;background:#f8fafc;">
    <h3 style="display:flex;align-items:center;justify-content:space-between;">
        <span>🧪 Central de Diagnósticos, Testes e Sincronização <span class="tooltip-icon" data-tooltip="Utilize os botões abaixo para testar a conexão com o ERP, verificar o banco local ou forçar a sincronização de 30 dias. O progresso em tempo real e o log das views serão exibidos no console abaixo.">❓</span></span>
        <span id="liveStatusTag" class="badge secondary">PRONTO</span>
    </h3>
    <p style="font-size:0.92rem;color:#475569;margin-bottom:1.2rem;">Execute testes em tempo real de conectividade com o banco do ERP e acompanhe o progresso linha por linha.</p>

    <!-- Botões de Ação de Teste -->
    <div style="display:flex;gap:.75rem;flex-wrap:wrap;align-items:center;">
        <button type="button" id="btnTestConn" class="btn" onclick="testErpConn()" style="background:#2563eb;color:#fff;">
            🔌 1. Testar Conexão Direta ERP <span class="tooltip-icon" data-tooltip="Conecta no banco do ERP (Oracle/SGBD) e inspeciona se as 6 views e suas colunas estão acessíveis.">❓</span>
        </button>
        
        <button type="button" id="btnCheckCache" class="btn ghost" onclick="checkCacheStatus()">
            📊 2. Testar Status do Cache Local <span class="tooltip-icon" data-tooltip="Consulta a base SQLite local do sistema e exibe a quantidade exata de linhas salvas em cache.">❓</span>
        </button>

        <button type="button" id="btnSyncNow" class="btn ghost" onclick="triggerManualSync()" {"disabled" if sync_running else ""}>
            🔄 3. Sincronizar ERP para Cache Agora <span class="tooltip-icon" data-tooltip="Inicia a sincronização dos últimos 30 dias do ERP para a base SQLite local com barra de carregamento e contagem de linhas em tempo real.">❓</span>
        </button>
    </div>
</section>

<!-- POPUP MODAL WINDOW (JANELA POPUP DE DIAGNÓSTICO E SINCRONIZAÇÃO) -->
<div id="erpModalOverlay" style="display:none;position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(15,23,42,0.8);backdrop-filter:blur(5px);z-index:999999;align-items:center;justify-content:center;padding:1rem;">
    <div style="background:#ffffff;border-radius:12px;width:100%;max-width:780px;max-height:90vh;display:flex;flex-direction:column;box-shadow:0 25px 50px -12px rgba(0,0,0,0.4);overflow:hidden;border:1px solid #cbd5e1;animation:fadeInModal 0.25s ease;">
        <!-- Header da Janela Popup -->
        <div style="background:#0f172a;color:#f8fafc;padding:1rem 1.4rem;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #334155;">
            <div style="display:flex;align-items:center;gap:.75rem;">
                <span id="modalTitle" style="font-weight:bold;font-size:1.05rem;color:#f8fafc;">🔌 Diagnóstico ERP</span>
                <span id="modalTag" class="badge secondary" style="font-size:0.78rem;">PROCESSANDO</span>
            </div>
            <button type="button" onclick="closeErpModal()" style="background:none;border:none;color:#94a3b8;font-size:1.4rem;cursor:pointer;line-height:1;padding:0 5px;" title="Fechar Janela Popup">✖</button>
        </div>

        <!-- Conteúdo Interno da Janela Popup -->
        <div style="padding:1.4rem;overflow-y:auto;flex:1;background:#f8fafc;">
            <!-- Barra de Progresso Real-time dentro do Popup -->
            <div id="modalProgressBox" style="display:none;margin-bottom:1.2rem;background:#ffffff;padding:1.1rem;border-radius:8px;border:1px solid #cbd5e1;box-shadow:0 1px 3px rgba(0,0,0,0.05);">
                <div style="display:flex;justify-content:space-between;align-items:center;font-size:0.9rem;font-weight:bold;color:#1e293b;margin-bottom:.4rem;">
                    <span id="modalProgressStep">⏳ Iniciando sincronização...</span>
                    <span id="modalProgressPct">0%</span>
                </div>
                <div class="progress-container">
                    <div id="modalProgressBarFill" class="progress-bar-fill"></div>
                    <div id="modalProgressOverlayText" class="progress-text">Aguardando resposta...</div>
                </div>
                <div style="margin-top:.7rem;font-size:0.85rem;color:#475569;display:flex;gap:1.2rem;flex-wrap:wrap;background:#f1f5f9;padding:.6rem;border-radius:6px;">
                    <span>📦 Pedidos: <strong id="mPedModal" style="color:#0f172a;">0</strong></span>
                    <span>👥 Clientes: <strong id="mCliModal" style="color:#0f172a;">0</strong></span>
                    <span>👔 Vendedores: <strong id="mVendModal" style="color:#0f172a;">0</strong></span>
                    <span>📄 Faturamento: <strong id="mFatModal" style="color:#0f172a;">0</strong></span>
                </div>
            </div>

            <!-- Log Terminal escuro dentro da Janela Popup -->
            <div id="modalLogBody" style="background:#0f172a;color:#f8fafc;padding:1.2rem;border-radius:8px;font-family:'Courier New',Consolas,monospace;font-size:0.88rem;line-height:1.6;min-height:220px;max-height:420px;overflow-y:auto;box-shadow:inset 0 2px 6px rgba(0,0,0,0.5);">
            </div>
        </div>

        <!-- Rodapé com Botão Fechar -->
        <div style="background:#ffffff;padding:1rem 1.4rem;display:flex;justify-content:space-between;align-items:center;border-top:1px solid #e2e8f0;">
            <span style="font-size:0.82rem;color:#64748b;">💡 Informações detalhadas do processo em execução</span>
            <button type="button" id="modalCloseBtn" onclick="closeErpModal()" class="btn ghost" style="min-width:130px;">Fechar Janela</button>
        </div>
    </div>
</div>

<form method="post" action="/admin/erp" class="form professional-form">
    <input type="hidden" name="_csrf" value="{csrf_val}">
    <input type="hidden" name="erp_db_driver" value="oracle">

    <fieldset>
        <legend>🔮 Conexão com Banco Oracle (Thin Mode 100% Nativo)</legend>
        <p style="font-size:0.88rem;color:#64748b;margin-bottom:1rem;">Configure a conexão física do banco de dados de produção onde as views do ERP estão localizadas.</p>

        <div class="grid2">
            <label>HOST (ENDEREÇO IP OU DOMÍNIO) * <span class="tooltip-icon" data-tooltip="Endereço IP ou Hostname do servidor de banco Oracle (ex: 192.168.1.10).">❓</span>
                <input name="erp_db_host" value="{esc(host)}" placeholder="Ex: 192.168.1.10 ou oracle.empresa.com">
            </label>

            <label>PORTA ORACLE * <span class="tooltip-icon" data-tooltip="Porta TCP do ouvinte (Listener) do Oracle. Padrão: 1521.">❓</span>
                <input name="erp_db_port" id="erpPortInput" type="number" value="{esc(port or '1521')}" placeholder="1521">
            </label>
        </div>

        <div class="grid2" style="margin-top:1rem;">
            <label>USUÁRIO DO BANCO (USER) * <span class="tooltip-icon" data-tooltip="Login do usuário do Oracle com permissão SELECT.">❓</span>
                <input name="erp_db_user" value="{esc(user_name)}" placeholder="Ex: ORACLE_USER ou usr_logistica">
            </label>

            <label>SENHA (PASSWORD) * <span class="tooltip-icon" data-tooltip="Senha de acesso do usuário no banco Oracle.">❓</span>
                <input type="password" name="erp_db_password" value="{esc(password)}" placeholder="••••••••">
            </label>
        </div>

        <div class="grid2" style="margin-top:1rem;">
            <label>ORACLE SYSTEM ID (SID) / SERVICE NAME * <span class="tooltip-icon" data-tooltip="Identificador da instância Oracle (ex: XE, ORCL ou orcl.suaempresa.com).">❓</span>
                <input name="erp_db_name" value="{esc(database)}" placeholder="Ex: XE ou ORCL ou prod.empresa.com">
                <small>Aceita tanto SID quanto Service Name (detectado automaticamente).</small>
            </label>

            <label>SCHEMA (DONO DAS VIEWS NO ORACLE) <span class="tooltip-icon" data-tooltip="Nome do usuário dono das views no Oracle (ex: PROTHEUS ou SYSTEM). Prevenir erro ORA-00942.">❓</span>
                <input name="erp_db_schema" value="{esc(schema)}" placeholder="Ex: PROTHEUS ou SYSTEM ou LOGISTICA">
                <small>Preenche o prefixo SCHEMA.VIEW nas consultas.</small>
            </label>
        </div>
    </fieldset>

    <fieldset>
        <legend>⏰ Horários & Frequência de Sincronização Automática de Vendas</legend>
        <p style="font-size:0.88rem;color:#64748b;margin-bottom:1rem;">Configure a janela de horários em que a sincronização automática de dados de vendas do ERP Oracle roda em segundo plano.</p>

        <div class="grid3">
            <label class="full" style="display:flex;align-items:center;gap:.5rem;background:#eff6ff;padding:.8rem;border-radius:6px;border:1px solid #bfdbfe;">
                <input type="checkbox" name="erp_enabled" value="1" {"checked" if enabled else ""}>
                <strong>Habilitar Módulo de Integração com ERP</strong>
                <span class="tooltip-icon" data-tooltip="Habilita/Desabilita as consultas ao ERP.">❓</span>
            </label>

            <label class="full" style="display:flex;align-items:center;gap:.5rem;background:#f0fdf4;padding:.8rem;border-radius:6px;border:1px solid #bbf7d0;">
                <input type="checkbox" name="erp_sync_auto_enabled" value="1" {"checked" if sync_auto_enabled else ""}>
                <strong>Habilitar Job Automático de Sincronização em Background</strong>
                <span class="tooltip-icon" data-tooltip="Sincroniza automaticamente vendas do ERP nos horários e dias configurados abaixo.">❓</span>
            </label>

            <label>Frequência entre Syncs (minutos) * <span class="tooltip-icon" data-tooltip="Frequência em minutos para rodar o sync de vendas (ex: 5, 10, 15, 30 min).">❓</span>
                <input name="erp_sync_interval_min" value="{esc(sync_min)}" min="5" max="1440" required>
                <small>Frequência (minutos)</small>
            </label>

            <label>Horário Inicial Expediente * <span class="tooltip-icon" data-tooltip="Horário do dia em que o sync automático começa a rodar (ex: 07:00).">❓</span>
                <input type="time" name="erp_sync_start_time" value="{esc(sync_start_time)}" required>
                <small>Início do expediente (HH:MM)</small>
            </label>

            <label>Horário Final Expediente * <span class="tooltip-icon" data-tooltip="Horário do dia em que o sync automático encerra (ex: 19:00).">❓</span>
                <input type="time" name="erp_sync_end_time" value="{esc(sync_end_time)}" required>
                <small>Fim do expediente (HH:MM)</small>
            </label>

            <label>Dias de Funcionamento <span class="tooltip-icon" data-tooltip="Dias da semana em que a sincronização automática de vendas executa.">❓</span>
                <select name="erp_sync_days">
                    <option value="seg_sex" {"selected" if sync_days=="seg_sex" else ""}>Segunda a Sexta-feira</option>
                    <option value="seg_sab" {"selected" if sync_days=="seg_sab" else ""}>Segunda a Sábado (Padrão)</option>
                    <option value="todos" {"selected" if sync_days=="todos" else ""}>Todos os dias (24/7)</option>
                </select>
                <small>Filtro por dia da semana</small>
            </label>

            <label>Histórico de Vendas (dias) <span class="tooltip-icon" data-tooltip="Período de vendas retroativas buscadas no ERP (padrão: 30 dias).">❓</span>
                <input name="erp_cache_days" value="{esc(cache_days)}" min="1" max="365">
                <small>Período retido no cache local</small>
            </label>
        </div>
    </fieldset>

    <fieldset>
        <legend>📋 Nomes das Views no ERP</legend>
        <div class="grid3">
            <label>View Pedidos <span class="tooltip-icon" data-tooltip="View do ERP contendo cabeçalho dos pedidos e valor/peso total.">❓</span>
                <input name="erp_view_pedidos" value="{esc(v_ped)}">
            </label>
            <label>View Itens do Pedido <span class="tooltip-icon" data-tooltip="View do ERP contendo itens individuais do pedido.">❓</span>
                <input name="erp_view_itens" value="{esc(v_itens)}">
            </label>
            <label>View Clientes <span class="tooltip-icon" data-tooltip="View do ERP contendo cadastro de clientes e endereços.">❓</span>
                <input name="erp_view_clientes" value="{esc(v_cli)}">
            </label>
            <label>View Vendedores <span class="tooltip-icon" data-tooltip="View do ERP contendo lista de vendedores.">❓</span>
                <input name="erp_view_vendedores" value="{esc(v_vend)}">
            </label>
            <label>View Produtos <span class="tooltip-icon" data-tooltip="View do ERP contendo cadastro de produtos.">❓</span>
                <input name="erp_view_produtos" value="{esc(v_prod)}">
            </label>
            <label>View Faturamento <span class="tooltip-icon" data-tooltip="View do ERP contendo notas fiscais emitidas.">❓</span>
                <input name="erp_view_faturamento" value="{esc(v_fat)}">
            </label>
        </div>
    </fieldset>

    <div class="form-actions sticky-actions">
        <button type="submit" class="btn">💾 Salvar Configurações do ERP</button>
        <a class="btn ghost" href="/settings">Voltar para Configurações</a>
    </div>
</form>

<script>
var pollTimer = null;
var CSRF_TOKEN = '{csrf_val}';

function adjustPortDefault() {{
    var drv = document.getElementById('erpDriverSelect').value;
    var portInp = document.getElementById('erpPortInput');
    if (drv === 'oracle' && (!portInp.value || portInp.value === '1433' || portInp.value === '3306' || portInp.value === '5432')) portInp.value = '1521';
    if (drv === 'sqlserver' && (!portInp.value || portInp.value === '1521')) portInp.value = '1433';
    if (drv === 'mysql' && (!portInp.value || portInp.value === '1521')) portInp.value = '3306';
    if (drv === 'postgresql' && (!portInp.value || portInp.value === '1521')) portInp.value = '5432';
}}

function openErpModal(title, tagText, tagClass, isSyncMode) {{
    document.getElementById('modalTitle').textContent = title;
    var tag = document.getElementById('modalTag');
    tag.textContent = tagText;
    tag.className = 'badge ' + tagClass;
    
    document.getElementById('modalProgressBox').style.display = isSyncMode ? 'block' : 'none';
    document.getElementById('modalLogBody').innerHTML = '<div style="color:#60a5fa;">⏳ Conectando e processando... por favor aguarde.</div>';
    
    var closeBtn = document.getElementById('modalCloseBtn');
    if (isSyncMode) {{
        closeBtn.disabled = true;
        closeBtn.style.opacity = '0.5';
        closeBtn.textContent = '⏳ Sincronizando...';
    }} else {{
        closeBtn.disabled = false;
        closeBtn.style.opacity = '1';
        closeBtn.textContent = 'Fechar Janela';
    }}

    document.getElementById('erpModalOverlay').style.display = 'flex';
}}

function closeErpModal() {{
    document.getElementById('erpModalOverlay').style.display = 'none';
    if (pollTimer) {{ clearInterval(pollTimer); pollTimer = null; }}
}}

function safeFetchJson(url, opts) {{
    opts = opts || {{}};
    opts.headers = opts.headers || {{}};
    opts.headers['X-CSRF-Token'] = CSRF_TOKEN;
    opts.headers['Accept'] = 'application/json';
    return fetch(url, opts).then(function(r) {{
        return r.json().catch(function() {{
            return fetch(url, {{ headers: {{ 'Accept': 'application/json' }} }}).then(function(r2) {{ return r2.json(); }});
        }});
    }});
}}

function testErpConn() {{
    var btn = document.getElementById('btnTestConn');
    btn.disabled = true;
    btn.innerHTML = '⏳ Testando Conexão...';
    
    openErpModal('🔌 Diagnóstico de Conexão Direta com ERP', 'TESTANDO ERP...', 'warning', false);

    safeFetchJson('/admin/erp/test', {{ method: 'POST' }})
        .then(function(d) {{
            btn.disabled = false;
            btn.innerHTML = '🔌 1. Testar Conexão Direta ERP';

            var modalTag = document.getElementById('modalTag');
            var logBody = document.getElementById('modalLogBody');

            if (d && d.ok) {{
                modalTag.className = 'badge success';
                modalTag.textContent = 'CONECTADO';
                
                var hasOra942 = false;
                var viewsHtml = '<div style="margin-top:.8rem;color:#e2e8f0;"><b>Views e Colunas Inspecionadas no Banco:</b><ul style="margin-top:.5rem;padding-left:1.2rem;">';
                for (var v in d.views) {{
                    var cols = d.views[v];
                    var isErr = cols.length > 0 && cols[0].indexOf('ERRO') === 0;
                    if (isErr) {{
                        if (cols[0].indexOf('ORA-00942') !== -1) hasOra942 = true;
                        viewsHtml += '<li style="color:#f87171;margin-bottom:.3rem;">❌ <b>' + v + ':</b> ' + cols[0] + '</li>';
                    }} else {{
                        viewsHtml += '<li style="color:#4ade80;margin-bottom:.3rem;">✅ <b>' + v + ' (' + cols.length + ' colunas):</b> ' + cols.slice(0, 8).join(', ') + (cols.length > 8 ? '...' : '') + '</li>';
                    }}
                }}
                viewsHtml += '</ul></div>';

                var adviceBox = '';
                if (hasOra942) {{
                    adviceBox = '<div style="margin-top:1rem;background:#1e293b;padding:.9rem;border-left:4px solid #f59e0b;border-radius:6px;color:#fef08a;font-size:0.88rem;line-height:1.5;">' +
                        '💡 <b>Como resolver o Erro ORA-00942 (Tabela ou view não existe):</b><br>' +
                        '1. Preencha o campo <b>SCHEMA (DONO DAS VIEWS NO ORACLE)</b> com o nome do usuário proprietário das views no Oracle (ex: <b>PROTHEUS</b>, <b>SYSTEM</b>, <b>WINTHOR</b>, etc.) e clique em <b>Salvar Configurações</b>.<br>' +
                        '2. Caso o usuário seja o próprio dono, confirme se as 6 views foram criadas com estes nomes e se possui permissão <b>GRANT SELECT</b> nelas.</div>';
                }}

                logBody.innerHTML = '<div style="color:#4ade80;font-weight:bold;font-size:1rem;">✅ ' + d.message + '</div>' + viewsHtml + adviceBox;
            }} else {{
                modalTag.className = 'badge danger';
                modalTag.textContent = 'ERRO CONEXÃO';
                
                var errMsg = (d && d.message) ? d.message : 'Erro desconhecido ao conectar no banco do ERP.';
                var helpAdvice = '';

                if (errMsg.indexOf('ORA-00942') !== -1) {{
                    helpAdvice = '<div style="margin-top:1rem;background:#1e293b;padding:.9rem;border-left:4px solid #f59e0b;border-radius:4px;color:#fef08a;">' +
                        '💡 <b>Dica de Solução para Erro ORA-00942 (Tabela ou View não existe):</b><br>' +
                        '1. Preencha o campo <b>Schema</b> com o nome do usuário dono das views no Oracle (ex: <b>PROTHEUS</b> ou <b>SYSTEM</b>).<br>' +
                        '2. Verifique se o usuário de leitura tem permissão GRANT SELECT nas views.</div>';
                }}

                logBody.innerHTML = '<div style="color:#f87171;font-weight:bold;font-size:1rem;">❌ FALHA NA CONEXÃO COM ERP</div>' +
                    '<div style="color:#f87171;margin-top:.4rem;">' + errMsg + '</div>' + helpAdvice;
            }}
        }})
        .catch(function(err) {{
            btn.disabled = false;
            btn.innerHTML = '🔌 1. Testar Conexão Direta ERP';
            document.getElementById('modalTag').className = 'badge danger';
            document.getElementById('modalTag').textContent = 'ERRO';
            document.getElementById('modalLogBody').innerHTML = '<div style="color:#f87171;">❌ Erro na comunicação interna do servidor: ' + err + '</div>';
        }});
}}

function checkCacheStatus() {{
    var btn = document.getElementById('btnCheckCache');
    btn.disabled = true;
    btn.innerHTML = '⏳ Verificando Cache...';

    openErpModal('📊 Diagnóstico do Cache Local (SQLite)', 'CONSULTANDO...', 'warning', false);

    safeFetchJson('/admin/erp/status', {{ method: 'GET' }})
        .then(function(d) {{
            btn.disabled = false;
            btn.innerHTML = '📊 2. Testar Status do Cache Local';

            var modalTag = document.getElementById('modalTag');
            var logBody = document.getElementById('modalLogBody');

            modalTag.className = 'badge success';
            modalTag.textContent = 'CACHE OK';

            var lastSync = (d && d.last_sync_at) || 'Nunca';
            var pedCount = (d && d.cache_pedidos_count) || 0;
            var fatCount = (d && d.cache_fat_count) || 0;
            var cliCount = (d && d.cache_clientes_count) || 0;
            var isRunning = (d && d.running) ? '<span style="color:#f59e0b;">EM ANDAMENTO</span>' : '<span style="color:#4ade80;">INATIVO (Aguardando intervalo)</span>';

            document.getElementById('lastSyncText').textContent = lastSync;
            document.getElementById('cardPedCount').textContent = pedCount;
            document.getElementById('cardFatCliCount').textContent = fatCount + ' NF / ' + cliCount + ' Cli';

            logBody.innerHTML = '<div style="color:#4ade80;font-weight:bold;font-size:1rem;margin-bottom:.8rem;">✅ ESTADO DO BANCO LOCAL SQLITE OPERACIONAL</div>' +
                '<div style="line-height:1.8;color:#f8fafc;">' +
                '• <b>Última Sincronização ERP → SQLite:</b> ' + lastSync + '<br>' +
                '• <b>Pedidos Armazenados em Cache:</b> <strong style="color:#4ade80;">' + pedCount + '</strong> registros<br>' +
                '• <b>Status de Faturamento Armazenados:</b> <strong style="color:#4ade80;">' + fatCount + '</strong> registros<br>' +
                '• <b>Clientes Armazenados:</b> <strong style="color:#4ade80;">' + cliCount + '</strong> registros<br>' +
                '• <b>Estado do Serviço em Background:</b> ' + isRunning +
                ((d && d.error) ? ('<br><br><span style="color:#f87171;"><b>Último Erro Registrado no Sync:</b> ' + d.error + '</span>') : '') +
                '</div>';
        }})
        .catch(function(err) {{
            btn.disabled = false;
            btn.innerHTML = '📊 2. Testar Status do Cache Local';
            document.getElementById('modalTag').className = 'badge danger';
            document.getElementById('modalTag').textContent = 'ERRO';
            document.getElementById('modalLogBody').innerHTML = '<div style="color:#f87171;">❌ Erro ao ler banco local: ' + err + '</div>';
        }});
}}

function triggerManualSync() {{
    var btn = document.getElementById('btnSyncNow');
    btn.disabled = true;
    btn.innerHTML = '⏳ Iniciando Sync...';

    openErpModal('🔄 Sincronização ERP para Cache Local SQLite', 'SINCRONIZANDO...', 'warning', true);
    document.getElementById('modalLogBody').innerHTML = '<div id="modalSyncLines" style="color:#94a3b8;">Iniciando thread de sincronização...</div>';

    safeFetchJson('/admin/erp/sync', {{ method: 'POST' }})
        .then(function(d) {{
            if (d && (d.ok || d.success)) {{
                startPollingProgress();
            }} else {{
                btn.disabled = false;
                btn.innerHTML = '🔄 3. Sincronizar ERP para Cache Agora';
                var closeBtn = document.getElementById('modalCloseBtn');
                if (closeBtn) {{
                    closeBtn.disabled = false;
                    closeBtn.style.opacity = '1';
                    closeBtn.textContent = 'Fechar Janela';
                }}
                var modalTag = document.getElementById('modalTag');
                if (modalTag) {{
                    modalTag.className = 'badge danger';
                    modalTag.textContent = 'FALHA SYNC';
                }}
                var errMsg = (d && d.message) ? d.message : 'Não foi possível iniciar a sincronização.';
                document.getElementById('modalLogBody').innerHTML = '<div style="color:#f87171;font-weight:bold;">⚠️ ' + errMsg + '</div>';
            }}
        }})
        .catch(function(err) {{
            btn.disabled = false;
            btn.innerHTML = '🔄 3. Sincronizar ERP para Cache Agora';
            var closeBtn = document.getElementById('modalCloseBtn');
            if (closeBtn) {{
                closeBtn.disabled = false;
                closeBtn.style.opacity = '1';
                closeBtn.textContent = 'Fechar Janela';
            }}
            var modalTag = document.getElementById('modalTag');
            if (modalTag) {{
                modalTag.className = 'badge danger';
                modalTag.textContent = 'ERRO SYNC';
            }}
            document.getElementById('modalLogBody').innerHTML = '<div style="color:#f87171;">❌ Falha ao iniciar sincronização: ' + err + '</div>';
        }});
}}

function startPollingProgress() {{
    if (pollTimer) clearInterval(pollTimer);
    
    pollTimer = setInterval(function() {{
        safeFetchJson('/admin/erp/status', {{ method: 'GET' }})
            .then(function(d) {{
                if (!d) return;
                var pct = d.progress_pct || 0;
                var step = d.step || 'Processando...';
                var isRunning = d.running;

                var bar = document.getElementById('modalProgressBarFill');
                if (bar) bar.style.width = pct + '%';
                var pctEl = document.getElementById('modalProgressPct');
                if (pctEl) pctEl.textContent = pct + '%';
                var stepEl = document.getElementById('modalProgressStep');
                if (stepEl) stepEl.textContent = step;
                var overlayEl = document.getElementById('modalProgressOverlayText');
                if (overlayEl) overlayEl.textContent = step + ' (' + pct + '%)';

                var pedModal = document.getElementById('mPedModal');
                if (pedModal) pedModal.textContent = d.pedidos_count || 0;
                var cliModal = document.getElementById('mCliModal');
                if (cliModal) cliModal.textContent = d.clientes_count || 0;
                var vendModal = document.getElementById('mVendModal');
                if (vendModal) vendModal.textContent = d.vendedores_count || 0;
                var fatModal = document.getElementById('mFatModal');
                if (fatModal) fatModal.textContent = d.faturamento_count || 0;

                var logDiv = document.getElementById('modalSyncLines');
                if (logDiv) {{
                    var line = '<div>[' + new Date().toLocaleTimeString() + '] ' + step + '</div>';
                    if (!logDiv.lastChild || logDiv.lastChild.textContent.indexOf(step) === -1) {{
                        logDiv.innerHTML += line;
                    }}
                }}

                if (!isRunning || pct >= 100) {{
                    clearInterval(pollTimer);
                    pollTimer = null;
                    var btnSync = document.getElementById('btnSyncNow');
                    if (btnSync) {{
                        btnSync.disabled = false;
                        btnSync.innerHTML = '🔄 3. Sincronizar ERP para Cache Agora';
                    }}

                    var closeBtn = document.getElementById('modalCloseBtn');
                    if (closeBtn) {{
                        closeBtn.disabled = false;
                        closeBtn.style.opacity = '1';
                        closeBtn.textContent = 'Fechar Janela';
                    }}

                    var modalTag = document.getElementById('modalTag');
                    if (modalTag) {{
                        if (d.status === 'error' || d.error) {{
                            modalTag.className = 'badge danger';
                            modalTag.textContent = 'ERRO SYNC';
                        }} else {{
                            modalTag.className = 'badge success';
                            modalTag.textContent = 'SYNC CONCLUÍDO';
                        }}
                    }}

                    var lastSyncEl = document.getElementById('lastSyncText');
                    if (lastSyncEl) lastSyncEl.textContent = d.last_sync_at || 'Agora';
                    var cardPed = document.getElementById('cardPedCount');
                    if (cardPed) cardPed.textContent = d.cache_pedidos_count || 0;
                    var cardFatCli = document.getElementById('cardFatCliCount');
                    if (cardFatCli) cardFatCli.textContent = (d.cache_fat_count || 0) + ' NF / ' + (d.cache_clientes_count || 0) + ' Cli';
                }}
            }})
            .catch(function(err) {{
                console.error("Polling error:", err);
            }});
    }}, 400);
}}

document.addEventListener('DOMContentLoaded', function() {{
    var b1 = document.getElementById('btnTestConn');
    if (b1) b1.addEventListener('click', function(e) {{ e.preventDefault(); testErpConn(); }});

    var b2 = document.getElementById('btnCheckCache');
    if (b2) b2.addEventListener('click', function(e) {{ e.preventDefault(); checkCacheStatus(); }});

    var b3 = document.getElementById('btnSyncNow');
    if (b3) b3.addEventListener('click', function(e) {{ e.preventDefault(); triggerManualSync(); }});

    var mc = document.getElementById('modalCloseBtn');
    if (mc) mc.addEventListener('click', function(e) {{ e.preventDefault(); closeErpModal(); }});
}});
</script>
'''
        return self.send_html(layout(u, 'Integração ERP', content, 'Configuração e diagnósticos da integração com o ERP'))

    def post_erp_admin(self, u):
        if not is_god(u):
            return self.fail(u, 'Acesso negado', 'Esta tela é restrita ao Administrador GOD.', 403)
        d = self.post_data()
        enabled = '1' if d.get('erp_enabled') else '0'
        driver = (d.get('erp_db_driver') or 'oracle').strip().lower()
        host = (d.get('erp_db_host') or '').strip()
        port = (d.get('erp_db_port') or '1521').strip()
        database = (d.get('erp_db_name') or '').strip()
        schema = (d.get('erp_db_schema') or '').strip()
        user_name = (d.get('erp_db_user') or '').strip()
        password = (d.get('erp_db_password') or '').strip()
        sync_min = (d.get('erp_sync_interval_min') or '30').strip()
        sync_start_time = (d.get('erp_sync_start_time') or '07:00').strip()
        sync_end_time = (d.get('erp_sync_end_time') or '19:00').strip()
        sync_days = (d.get('erp_sync_days') or 'seg_sab').strip()
        sync_auto_enabled = '1' if d.get('erp_sync_auto_enabled') else '0'
        cache_days = (d.get('erp_cache_days') or '30').strip()

        v_ped = (d.get('erp_view_pedidos') or 'VW_PEDIDOS_CAD_LM').strip()
        v_itens = (d.get('erp_view_itens') or 'VW_ITENS_PEDIDO_CAD_LM').strip()
        v_cli = (d.get('erp_view_clientes') or 'VW_CLIENTES_CAD_LM').strip()
        v_vend = (d.get('erp_view_vendedores') or 'VW_VENDEDOR_CAD_LM').strip()
        v_prod = (d.get('erp_view_produtos') or 'VW_PRODUTOS_CAD_LM').strip()
        v_fat = (d.get('erp_view_faturamento') or 'VW_FATURAMENTO_CAD_LM').strip()

        settings_to_update = {
            'erp_enabled': enabled,
            'erp_db_driver': driver,
            'erp_db_host': host,
            'erp_db_port': port,
            'erp_db_name': database,
            'erp_db_schema': schema,
            'erp_db_user': user_name,
            'erp_db_password': password,
            'erp_sync_interval_min': sync_min,
            'erp_sync_start_time': sync_start_time,
            'erp_sync_end_time': sync_end_time,
            'erp_sync_days': sync_days,
            'erp_sync_auto_enabled': sync_auto_enabled,
            'erp_cache_days': cache_days,
            'erp_view_pedidos': v_ped,
            'erp_view_itens': v_itens,
            'erp_view_clientes': v_cli,
            'erp_view_vendedores': v_vend,
            'erp_view_produtos': v_prod,
            'erp_view_faturamento': v_fat,
        }

        with conn() as db:
            for k, val in settings_to_update.items():
                db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', (k, val, now()))
            audit(db, u, 'Alterou configurações ERP', 'ERP')
            db.commit()

        if _ERP_AVAILABLE:
            _erp_connector.reload_erp_config()

        self.redirect('/admin/erp')

    def post_erp_force_sync(self, u):
        if not is_god(u):
            return self.send_json({'ok': False, 'message': 'Acesso negado'}, 403)
        if not _ERP_AVAILABLE:
            return self.send_json({'ok': False, 'message': 'Módulo ERP indisponível.'}, 503)

        status_info = _erp_connector.get_sync_status()
        if not status_info.get('running'):
            def _run_manual_sync():
                _erp_connector.sync_erp_cache()
                sync_pending_invoiced_orders_to_logistica()

            threading.Thread(target=_run_manual_sync, name='manual-sync-thread', daemon=True).start()
            with conn() as db:
                audit(db, u, 'Iniciou sync ERP manual', 'ERP')
                db.commit()

        return self.send_json({'ok': True, 'message': 'Sincronização iniciada em background.'})

    def post_erp_test_connection(self, u):
        if not is_god(u):
            return self.send_json({'ok': False, 'message': 'Acesso negado'}, 403)
        if not _ERP_AVAILABLE:
            return self.send_json({'ok': False, 'message': 'Módulo ERP não disponível'}, 503)
        res = _erp_connector.check_connectivity()
        return self.send_json(res)

    def get_erp_status_json(self, u):
        if not is_god(u):
            return self.send_json({'error': 'Acesso negado'}, 403)
        if not _ERP_AVAILABLE:
            return self.send_json({'error': 'Módulo ERP não disponível'}, 503)
        return self.send_json(_erp_connector.get_sync_status())


class SafeThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, server_address, handler_class):
        self._worker_limit = threading.BoundedSemaphore(MAX_SERVER_WORKERS)
        super().__init__(server_address, handler_class)

    def process_request_thread(self, request, client_address):
        acquired = self._worker_limit.acquire(timeout=REQUEST_SOCKET_TIMEOUT_SECONDS)
        if not acquired:
            try:
                request.close()
            except Exception:
                pass
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_limit.release()


def print_runtime_banner(host: str, port: int):
    if host == '0.0.0.0':
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            local_ip = 'SEU_IP_LOCAL'
        print(f'Logística Casa do Campo rodando em REDE na porta {port}')
        print(f'Local:   http://127.0.0.1:{port}')
        print(f'Rede:    http://{local_ip}:{port}')
    else:
        print(f'Logística Casa do Campo rodando em LOCAL: http://127.0.0.1:{port}')
    print(f'Banco ativo: {DB_BACKEND.upper()}')
    if DB_BACKEND == 'sqlite':
        print(f'Arquivo SQLite: {DB_PATH}')
    else:
        print(f'URL de banco: {DATABASE_URL}')
    print(f'Concorrência máxima de requisições simultâneas: {MAX_SERVER_WORKERS}')


def is_in_erp_sync_schedule(cfg=None):
    if not cfg:
        cfg = _erp_connector.get_erp_config() if _ERP_AVAILABLE else None
    if not cfg or not cfg.enabled or not cfg.sync_auto_enabled:
        return False, 'Sincronização automática desabilitada.'

    now_dt = datetime.now()
    weekday = now_dt.weekday()  # 0 = Segunda, 5 = Sábado, 6 = Domingo
    days_mode = (cfg.sync_days or 'seg_sab').strip().lower()
    if days_mode == 'seg_sex' and weekday > 4:
        return False, 'Fora dos dias ativos (Configurado: Segunda a Sexta).'
    if days_mode == 'seg_sab' and weekday == 6:
        return False, 'Fora dos dias ativos (Configurado: Segunda a Sábado).'

    try:
        start_h, start_m = map(int, (cfg.sync_start_time or '07:00').split(':'))
        end_h, end_m = map(int, (cfg.sync_end_time or '19:00').split(':'))
        curr_min = now_dt.hour * 60 + now_dt.minute
        start_min = start_h * 60 + start_m
        end_min = end_h * 60 + end_m

        if start_min <= end_min:
            if not (start_min <= curr_min <= end_min):
                return False, f'Fora do horário configurado ({cfg.sync_start_time} às {cfg.sync_end_time}).'
        else:
            if not (curr_min >= start_min or curr_min <= end_min):
                return False, f'Fora do horário configurado ({cfg.sync_start_time} às {cfg.sync_end_time}).'
    except Exception:
        pass

    return True, 'Dentro da janela de sincronização.'


def sync_pending_invoiced_orders_to_logistica():
    """
    Sincroniza pedidos que estavam como 'Venda' em Logística e agora estão faturados no ERP.
    Atualiza status para 'Faturado' no banco SQLite e publica atualização.
    """
    if not _ERP_AVAILABLE:
        return 0
    import logging as _logging
    _log = _logging.getLogger('logistica.erp_sync')

    with conn() as db:
        pending = db.execute(
            "SELECT id, order_number FROM orders WHERE status='Venda' AND order_number IS NOT NULL AND order_number<>''"
        ).fetchall()
    if not pending:
        return 0

    updated_count = 0
    _log.info('Sync ERP: verificando %d pedido(s) pendentes de faturamento.', len(pending))

    for row in pending:
        order_number = str(row['order_number'] or '').strip()
        if not order_number:
            continue
        try:
            # 1. Tenta buscar status de faturamento (no cache local)
            fat_raw = _erp_connector.lookup_invoice_status(order_number, force_live=False)
            
            # 2. Se o cache não indicou faturamento, força consulta ao vivo diretamente no ERP!
            if not fat_raw or not fat_raw.get('invoice_number'):
                live_ped = _erp_connector.lookup_order(order_number, force_live=True)
                if live_ped:
                    fat_raw = _erp_connector.lookup_invoice_status(order_number, force_live=False) or live_ped

            if not fat_raw:
                continue

            invoice_data = _erp_mapper.map_erp_invoice_to_logistica(fat_raw)
            if not invoice_data or not invoice_data.get('invoice_number'):
                mapped_ped = _erp_mapper.map_erp_to_logistica(fat_raw)
                if mapped_ped and (mapped_ped.get('is_invoiced') or mapped_ped.get('invoice_number')):
                    invoice_data = {
                        'invoice_number': mapped_ped.get('invoice_number') or '1',
                        'invoiced_at': mapped_ped.get('invoiced_at') or mapped_ped.get('sale_date') or now()[:10],
                    }

            if not invoice_data or not invoice_data.get('invoice_number'):
                continue

            nf = str(invoice_data['invoice_number']).strip()
            if not nf or nf in ('0', '0.0', 'None', 'null'):
                continue

            nf_date = invoice_data.get('invoiced_at') or now()[:10]

            with conn() as db:
                current = db.execute(
                    "SELECT id, status FROM orders WHERE id=? AND status='Venda'",
                    (row['id'],)
                ).fetchone()
                if not current:
                    continue
                db.execute(
                    "UPDATE orders SET invoice_number=?, invoiced_at=?, status='Faturado', updated_at=?, version=COALESCE(version,1)+1 WHERE id=? AND status='Venda'",
                    (nf, nf_date, now(), row['id'])
                )
                db.execute(
                    "INSERT INTO order_history(order_id,old_status,new_status,action,notes,created_at) VALUES(?,?,?,?,?,?)",
                    (row['id'], 'Venda', 'Faturado', 'ERP_SYNC', f'Faturamento detectado automaticamente via ERP. NF: {nf}', now())
                )
                db.commit()
                updated_count += 1
                _log.info('Pedido %s (ID %s) faturado automaticamente via ERP. NF: %s', order_number, row['id'], nf)
                GLOBAL_BROKER.publish('orders_updated')
        except Exception as item_exc:
            _log.error('Sync ERP: erro no pedido %s: %s', order_number, item_exc)

    return updated_count


def _erp_invoice_sync_worker():
    """
    Thread de sincronização de faturamento ERP → Logística.
    Roda continuamente em background a cada N segundos.
    Verifica os horários e dias configurados e executa o sync periodicamente.
    """
    import logging as _logging
    _log = _logging.getLogger('logistica.erp_sync')
    _log.info('Thread de sync ERP ativa em background.')

    last_full_sync_ts = 0.0
    last_pending_check_ts = 0.0

    while True:
        try:
            time.sleep(10)  # Checa a cada 10s se é hora de executar os ciclos
            if not _ERP_AVAILABLE:
                continue

            cfg = _erp_connector.get_erp_config()
            if not cfg.enabled or not cfg.sync_auto_enabled:
                continue

            in_sched, reason = is_in_erp_sync_schedule(cfg)
            if not in_sched:
                _log.debug('Sync ERP pausado no momento: %s', reason)
                continue

            now_ts = time.time()

            # 1. Checa pedidos pendentes de faturamento a cada 60 segundos
            if (now_ts - last_pending_check_ts) >= 60:
                last_pending_check_ts = now_ts
                updated_pending = sync_pending_invoiced_orders_to_logistica()
                if updated_pending > 0:
                    _log.info('Sync ERP: %d pedido(s) promovido(s) para Faturado em Logística.', updated_pending)

            # 2. Executa sync completo de cache nos intervalos configurados (ex: a cada 30 min)
            interval_min = max(1, cfg.sync_interval_min if cfg.sync_interval_min > 0 else 30)
            interval_sec = interval_min * 60

            if (now_ts - last_full_sync_ts) >= interval_sec:
                last_full_sync_ts = now_ts
                _log.info('Sync ERP: executando sincronização periódica de cache (%s)...', reason)
                sync_res = _erp_connector.sync_erp_cache()
                _log.info('Sync ERP resultado: %s', sync_res.get('message'))

                # Re-executa verificação de faturamento após o sync de cache
                sync_pending_invoiced_orders_to_logistica()

        except Exception as sync_exc:
            _log.error('Sync ERP: erro no ciclo de sync: %s', sync_exc)



def create_server(host: str = HOST, port: int = PORT) -> SafeThreadingHTTPServer:
    init_db()
    runtime_state_cleanup(force=True)
    if _ERP_AVAILABLE:
        try:
            _erp_connector.register_db_reader(get_setting)
            _erp_connector.register_local_db(conn)
            _erp_connector.init_cache_tables()
            _erp_sync_thread = threading.Thread(
                target=_erp_invoice_sync_worker,
                name='erp-invoice-sync',
                daemon=True,
            )
            _erp_sync_thread.start()
        except Exception as _e:
            pass  # Nunca impede o servidor de subir
    try:
        return SafeThreadingHTTPServer((host, int(port)), App)
    except OSError as e:
        log_server_error('SERVER_START', e)
        raise


def start_server(host: str = HOST, port: int = PORT):
    print_runtime_banner(host, int(port))
    server = create_server(host=host, port=int(port))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        except Exception:
            pass


if __name__=='__main__':
    start_server(host=HOST, port=PORT)

