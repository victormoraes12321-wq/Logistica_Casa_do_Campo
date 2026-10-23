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
    trip_registry_dispatch,
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
from app_core.services import pdf_report_service
from app_core.services.backup_service import (
    create_sqlite_backup,
    prune_backup_files,
    restore_sqlite_from_backup,
)
from app_core.services.permission_service import has_permission as permission_service_has_permission
from app_core.services.driver_security import DEFAULT_DRIVER_PASSWORD, hash_driver_password, utc_iso
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
STATUSES = ['Venda','Faturado','Carga Montada','Saiu para entrega','Agendado','Acertado','Problema','Cancelado']
FLOW = ['Venda','Faturado','Carga Montada','Saiu para entrega','Acertado']
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
    'Carga Montada': 'Carga Montada',
    'Carga montada': 'Carga Montada',
    'Aguardando Saída': 'Carga Montada',
    'Aguardando saída': 'Carga Montada',
    'Aguardando Saida': 'Carga Montada',
    'Aguardando saida': 'Carga Montada',
    'Carga Montada / Aguardando Saída': 'Carga Montada',
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
    'Faturado': {'Carga Montada', 'Saiu para entrega', 'Acertado', 'Problema', 'Cancelado', 'Agendado'},
    'Carga Montada': {'Faturado', 'Saiu para entrega', 'Acertado', 'Problema', 'Cancelado', 'Agendado'},
    'Saiu para entrega': {'Faturado', 'Carga Montada', 'Acertado', 'Problema', 'Cancelado', 'Agendado'},
    'Agendado': {'Venda', 'Faturado', 'Carga Montada', 'Saiu para entrega', 'Acertado', 'Problema', 'Cancelado'},
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


_SELLER_NAME_BY_CODE: dict[str, str] = {}

def get_seller_name_map(db=None) -> dict[str, str]:
    """Retorna dicionário mapeando código RCA (string) para nome completo do vendedor."""
    global _SELLER_NAME_BY_CODE
    if _SELLER_NAME_BY_CODE:
        return _SELLER_NAME_BY_CODE

    # 1. Carrega do banco SQLite local (erp_cache_vendedores)
    try:
        def _read_cache(conn_obj):
            for row in conn_obj.execute("SELECT codigovendedor, raw_json FROM erp_cache_vendedores").fetchall():
                c_code = str(row[0] or "").strip()
                if not c_code:
                    continue
                try:
                    d = json.loads(row[1]) if isinstance(row[1], str) else row[1]
                    name = (d.get("nome_ved") or d.get("nome") or d.get("nomevendedor") or d.get("nome_vendedor") or "").strip()
                    if name:
                        _SELLER_NAME_BY_CODE[c_code] = name
                except Exception:
                    pass
        if db is not None:
            _read_cache(db)
        else:
            with conn() as ldb:
                _read_cache(ldb)
    except Exception:
        pass

    return _SELLER_NAME_BY_CODE


def resolve_seller_name(code_or_str, db=None) -> str:
    """Resolve código RCA ou texto legado para o nome real completo do vendedor."""
    if not code_or_str:
        return "Não informado"
    val = str(code_or_str).strip()
    if not val:
        return "Não informado"

    # Se já é um nome completo de vendedor (não é "RCA XX", "#XX" nem número puro)
    m_rca = re.match(r'^(?:RCA\s*|\#\s*)?(\d+)$', val, re.IGNORECASE)
    if not m_rca:
        return val

    code = m_rca.group(1).strip()
    seller_map = get_seller_name_map(db)
    name = seller_map.get(code)
    if name:
        return name

    # Fallback buscando pontualmente no SQLite se ainda não carregado
    try:
        def _check_one(c_obj):
            r = c_obj.execute("SELECT raw_json FROM erp_cache_vendedores WHERE codigovendedor = ?", (code,)).fetchone()
            if r and r[0]:
                d = json.loads(r[0]) if isinstance(r[0], str) else r[0]
                return (d.get("nome_ved") or d.get("nome") or d.get("nomevendedor") or "").strip()
            return None
        found = _check_one(db) if db is not None else None
        if not found:
            with conn() as ldb:
                found = _check_one(ldb)
        if found:
            _SELLER_NAME_BY_CODE[code] = found
            return found
    except Exception:
        pass

    return f"Vendedor (RCA {code})"


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
def to_float(v, default=0.0):
    try: return float(v or 0)
    except Exception: return default
Number = to_float
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


def extract_lat_lng(val):
    if not val:
        return None, None
    if isinstance(val, (tuple, list)) and len(val) >= 2:
        try:
            return float(val[0]), float(val[1])
        except Exception:
            pass
    val_str = str(val).strip()
    m = re.search(r'([-+]?\d+\.\d+)\s*,\s*([-+]?\d+\.\d+)', val_str)
    if m:
        try:
            lat, lng = float(m.group(1)), float(m.group(2))
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return lat, lng
        except Exception:
            pass
    return None, None


def order_map_info(order_row, receipt_row=None):
    if not order_row:
        return ("", False, "")
    keys = order_row.keys()

    # 1. GPS do comprovante (coordenadas exatas do dispositivo)
    if receipt_row:
        r_keys = receipt_row.keys()
        if 'delivery_location_link' in r_keys and receipt_row['delivery_location_link']:
            return (receipt_row['delivery_location_link'], True, 'GPS Exato')
        if 'latitude' in r_keys and receipt_row['latitude'] is not None and 'longitude' in r_keys and receipt_row['longitude'] is not None:
            try:
                lat, lng = float(receipt_row['latitude']), float(receipt_row['longitude'])
                return (f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}", True, 'GPS Exato')
            except (ValueError, TypeError):
                pass

    # 2. GPS do pedido (capturado na confirmação da entrega mobile)
    if 'dr_loc_link' in keys and order_row['dr_loc_link']:
        return (order_row['dr_loc_link'], True, 'GPS Exato')
    if 'delivery_location_link' in keys and order_row['delivery_location_link']:
        return (order_row['delivery_location_link'], True, 'GPS Exato')
    if 'delivery_latitude' in keys and order_row['delivery_latitude'] is not None and 'delivery_longitude' in keys and order_row['delivery_longitude'] is not None:
        try:
            lat, lng = float(order_row['delivery_latitude']), float(order_row['delivery_longitude'])
            return (f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}", True, 'GPS Exato')
        except (ValueError, TypeError):
            pass

    # 3. Link de Mapa cadastrado no cliente
    if 'location_link' in keys and order_row['location_link']:
        return (order_row['location_link'], False, 'Cadastrado')

    # 3. Fallback de Endereço / Fazenda no Google Maps
    client = order_row['client'] if 'client' in keys else (order_row['client_name'] if 'client_name' in keys else '')
    farm = order_row['farm_name'] if 'farm_name' in keys else ''
    addr = order_row['delivery_address'] if 'delivery_address' in keys else ''
    city = order_row['city'] if 'city' in keys else (order_row['client_city'] if 'client_city' in keys else '')

    parts = [str(p).strip() for p in (farm, client, addr, city) if p and str(p).strip()]
    if parts:
        q_str = quote(" ".join(parts[:3]))
        return (f"https://www.google.com/maps/search/?api=1&query={q_str}", False, 'Endereço')

    return ("", False, "")


def get_setting(key, default=''):
    try:
        with conn() as db:
            r = db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            return r['value'] if r else default
    except Exception:
        return default


def get_google_maps_api_key():
    env_key = os.environ.get('GOOGLE_MAPS_API_KEY', '').strip()
    if env_key:
        return env_key
    return get_setting('google_maps_api_key', '').strip()


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
    CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL,product_code TEXT,product_name TEXT NOT NULL,category TEXT,quantity REAL DEFAULT 0,unit TEXT,weight_kg REAL DEFAULT 0,unit_price REAL DEFAULT 0,total_value REAL DEFAULT 0,status TEXT DEFAULT 'Pendente',problem_notes TEXT DEFAULT '',notes TEXT,FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
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
    CREATE TABLE IF NOT EXISTS deleted_orders (
        order_number TEXT PRIMARY KEY,
        deleted_at TEXT NOT NULL,
        deleted_by INTEGER,
        reason TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_erp_cache_pedidos_sync ON erp_cache_pedidos(erp_synced_at);
    CREATE INDEX IF NOT EXISTS idx_erp_cache_fat_sync ON erp_cache_faturamento(erp_synced_at);
    ''')


def ensure_column(db, table, column_name, column_def):
    cols = {r['name'] for r in db.execute(f'PRAGMA table_info({table})').fetchall()}
    if column_name not in cols:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column_def}')


def ensure_schema_migrations(db):
    from app_core.services.trip_audit import ensure_schema as ensure_trip_audit_schema
    ensure_trip_audit_schema(db)
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
    ensure_column(db, 'routes', 'status', 'status TEXT DEFAULT "Planejada"')
    ensure_column(db, 'routes', 'route_name', 'route_name TEXT')
    ensure_column(db, 'routes', 'total_weight', 'total_weight REAL DEFAULT 0')
    ensure_column(db, 'routes', 'declared_weight', 'declared_weight REAL DEFAULT 0')
    ensure_column(db, 'routes', 'capacity', 'capacity REAL DEFAULT 11000')
    ensure_column(db, 'routes', 'notes', 'notes TEXT')
    ensure_column(db, 'routes', 'date', 'date TEXT')
    ensure_column(db, 'routes', 'created_at', 'created_at TEXT')
    ensure_column(db, 'routes', 'km_start', 'km_start REAL')
    ensure_column(db, 'routes', 'km_end', 'km_end REAL')
    ensure_column(db, 'routes', 'km_total', 'km_total REAL')
    ensure_column(db, 'routes', 'departure_time', 'departure_time TEXT')
    ensure_column(db, 'routes', 'arrival_time', 'arrival_time TEXT')
    ensure_column(db, 'routes', 'helper_name', 'helper_name TEXT')
    ensure_column(db, 'routes', 'destination', 'destination TEXT')
    ensure_column(db, 'routes', 'odometer_start_photo', 'odometer_start_photo TEXT')
    ensure_column(db, 'routes', 'odometer_end_photo', 'odometer_end_photo TEXT')
    ensure_column(db, 'routes', 'driver_signature', 'driver_signature TEXT')
    ensure_column(db, 'routes', 'expenses_json', 'expenses_json TEXT')
    ensure_column(db, 'routes', 'arrival_latitude', 'arrival_latitude REAL')
    ensure_column(db, 'routes', 'arrival_longitude', 'arrival_longitude REAL')
    ensure_column(db, 'routes', 'arrival_location_link', 'arrival_location_link TEXT')
    ensure_column(db, 'routes', 'arrival_at', 'arrival_at TEXT')
    ensure_column(db, 'routes', 'updated_at', 'updated_at TEXT')
    ensure_column(db, 'routes', 'version', 'version INTEGER DEFAULT 1')
    db.execute('''CREATE TABLE IF NOT EXISTS trip_expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id INTEGER NOT NULL,
        expense_type TEXT NOT NULL,
        quantity REAL DEFAULT 1,
        unit_value REAL DEFAULT 0,
        total_value REAL DEFAULT 0,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(route_id) REFERENCES routes(id) ON DELETE CASCADE
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_trip_expenses_route ON trip_expenses(route_id)')
    db.execute('''CREATE TABLE IF NOT EXISTS trip_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        route_id INTEGER NOT NULL,
        photo_type TEXT NOT NULL,
        image_data TEXT NOT NULL,
        mime_type TEXT DEFAULT 'image/jpeg',
        created_at TEXT NOT NULL,
        FOREIGN KEY(route_id) REFERENCES routes(id) ON DELETE CASCADE
    )''')
    ensure_column(db, 'route_orders', 'delivery_order', 'delivery_order INTEGER DEFAULT 1')
    ensure_column(db, 'route_orders', 'status', 'status TEXT DEFAULT "Pendente"')
    ensure_column(db, 'route_cities', 'created_at', 'created_at TEXT')
    ensure_column(db, 'route_cities', 'updated_at', 'updated_at TEXT')
    ensure_column(db, 'route_cities', 'version', 'version INTEGER DEFAULT 1')
    ensure_column(db, 'clients', 'latitude', 'latitude REAL')
    ensure_column(db, 'clients', 'longitude', 'longitude REAL')
    ensure_column(db, 'clients', 'location_link', 'location_link TEXT')
    ensure_column(db, 'orders', 'latitude', 'latitude REAL')
    ensure_column(db, 'orders', 'longitude', 'longitude REAL')
    ensure_column(db, 'clients', 'updated_at', 'updated_at TEXT')
    ensure_column(db, 'clients', 'version', 'version INTEGER DEFAULT 1')
    ensure_column(db, 'clients', 'customer_code', 'customer_code TEXT')
    ensure_column(db, 'clients', 'is_rural_verified', 'is_rural_verified INTEGER DEFAULT 0')
    ensure_column(db, 'clients', 'gps_source', 'gps_source TEXT DEFAULT "pending"')
    ensure_column(db, 'orders', 'is_rural_verified', 'is_rural_verified INTEGER DEFAULT 0')
    ensure_column(db, 'orders', 'gps_source', 'gps_source TEXT DEFAULT "pending"')
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
    ensure_column(db, 'order_items', 'status', 'status TEXT DEFAULT "Pendente"')
    ensure_column(db, 'order_items', 'problem_notes', 'problem_notes TEXT DEFAULT ""')
    ensure_column(db, 'order_items', 'unit_price', 'unit_price REAL DEFAULT 0')
    ensure_column(db, 'order_items', 'total_value', 'total_value REAL DEFAULT 0')
    try:
        for r_rca in db.execute("SELECT id, seller_name FROM orders WHERE seller_name LIKE 'RCA %' OR seller_name GLOB '*[0-9]*'").fetchall():
            real_n = resolve_seller_name(r_rca['seller_name'], db)
            if real_n and not real_n.startswith('RCA ') and not real_n.startswith('Vendedor (RCA'):
                db.execute("UPDATE orders SET seller_name = ? WHERE id = ?", (real_n, r_rca['id']))
    except Exception:
        pass
    try:
        # Reconciliação automática: pedidos em cargas Planejadas devem ter status 'Carga Montada'
        db.execute("""
            UPDATE orders
            SET status='Carga Montada', updated_at=DATETIME('now','localtime')
            WHERE id IN (
                SELECT ro.order_id
                FROM route_orders ro
                JOIN routes r ON r.id=ro.route_id
                WHERE r.status='Planejada'
            )
            AND status IN ('Faturado', 'Venda')
        """)
    except Exception:
        pass


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


def normalize_legacy_business_text_once(db):
    """Migração única: reiniciar o serviço não deve regravar cadastros/históricos."""
    key = 'migration_business_text_normalization_v1'
    if db.execute("SELECT 1 FROM settings WHERE key=? AND value='done'", (key,)).fetchone():
        return False
    db.execute("""UPDATE orders
                  SET seller_name=COALESCE(
                    NULLIF(TRIM(seller_name),''),
                    (SELECT u.name FROM users u WHERE u.id=orders.seller_id),
                    'Não informado'
                  )""")
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
    db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,'done',?)", (key,now()))
    return True


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
        db.execute("UPDATE routes SET updated_at=COALESCE(updated_at,created_at) WHERE updated_at IS NULL")
        db.execute("UPDATE routes SET version=1 WHERE version IS NULL")
        db.execute("UPDATE route_cities SET version=1 WHERE version IS NULL")
        db.execute("UPDATE clients SET updated_at=COALESCE(updated_at,created_at) WHERE updated_at IS NULL")
        db.execute("UPDATE clients SET version=1 WHERE version IS NULL")
        normalize_legacy_business_text_once(db)
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

def login_page(err='', username=''):
    system=get_setting('system_name','Logística Casa do Campo')
    company=get_setting('company_name','Casa do Campo')
    is_maint = get_setting('maintenance_mode','off') == 'on'

    if is_maint:
        status_pill_html = '<div class="hero-status-pill maintenance"><span class="status-dot red"></span>Modo de Manutenção Ativo</div>'
        maint_banner_html = '<div class="login-alert-maintenance"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><span><b>Modo de Manutenção Ativo:</b> O sistema está em manutenção. Acesso restrito a administradores.</span></div>'
    else:
        status_pill_html = '<div class="hero-status-pill"><span class="status-dot"></span>Sistema Operacional On-line</div>'
        maint_banner_html = ''

    error_html = ''
    if err:
        error_html = f'''<div class="login-alert-error" style="background:#fef2f2; border:1px solid #f87171; border-left:4px solid #dc2626; color:#991b1b; padding:12px 14px; border-radius:8px; margin-bottom:16px; display:flex; align-items:flex-start; gap:10px; font-size:13px; line-height:1.45;">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#dc2626" stroke-width="2" style="flex:none; margin-top:1px;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            <div>
              <b style="color:#7f1d1d;">Não foi possível entrar:</b><br>
              <span>{esc(err)}</span>
              <div style="margin-top:6px; font-size:11px; color:#b91c1c;">💡 <b>Dica:</b> Confira se a tecla Caps Lock está ativada ou peça ao administrador para redefinir sua senha.</div>
            </div>
          </div>'''

    return f'''<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Login · {esc(system)}</title><link rel="stylesheet" href="/static/style.css"></head>
<body class="login-bg">
  <div class="login-split-container">
    <!-- LADO ESQUERDO: Dark Emerald Hero Section (Edge-to-Edge) -->
    <section class="login-hero-side">
      <div class="hero-bg-grid"></div>
      
      <!-- Topo com a Logo Oficial Destacada -->
      <div class="hero-top-bar">
        <div class="hero-logo-box">
          <img src="/static/logo.png" alt="{esc(company)}" class="hero-logo-img">
        </div>
        <div class="hero-system-info">
          <div class="system-title-row">
            <span class="system-name-tag">{esc(system)}</span>
            <span class="system-version-tag">V2.9.1 PRO</span>
          </div>
          <p class="system-desc-sub">Central Integrada de Expedição, Faturamento & Rotas</p>
        </div>
        {status_pill_html}
      </div>

      <!-- Meio com a Mensagem Principal -->
      <div class="hero-middle-content">
        <div class="hero-tagline-chip">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
          Tecnologia & Alta Eficiência Logística no Campo
        </div>

        <h1 class="hero-heading">
          Gestão Inteligente de <span class="gradient-accent">Rotas, Cargas e SLA</span> para o Agronegócio.
        </h1>

        <p class="hero-description">
          Controle centralizado da venda até o acerto final da carga, com faturamento automático, rastreamento mobile com GPS exato e monitoramento de SLA em tempo real.
        </p>

        <!-- 2x2 Feature Cards Grid -->
        <div class="hero-cards-grid">
          <div class="hero-feature-card">
            <div class="feat-icon green">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="3" width="15" height="13"/><polygon points="16 8 20 8 23 11 23 16 16 16 16 8"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/></svg>
            </div>
            <div>
              <h5>Expedição & Cargas</h5>
              <p>Montagem otimizada de rotas com limite de carga por veículo.</p>
            </div>
          </div>

          <div class="hero-feature-card">
            <div class="feat-icon blue">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>
            </div>
            <div>
              <h5>GPS Exato Mobile</h5>
              <p>Baixa em campo com canhoto digital e geolocalização exata.</p>
            </div>
          </div>

          <div class="hero-feature-card">
            <div class="feat-icon yellow">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            </div>
            <div>
              <h5>SLA em Horário Útil</h5>
              <p>Cálculo automático do prazo oficial de 15 dias corridos.</p>
            </div>
          </div>

          <div class="hero-feature-card">
            <div class="feat-icon purple">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
            </div>
            <div>
              <h5>Segurança & Auditoria</h5>
              <p>Histórico completo de ações com auditoria e controle de acesso.</p>
            </div>
          </div>
        </div>
      </div>

      <!-- Rodapé do Lado Esquerdo -->
      <div class="hero-bottom-strip">
        <span>{esc(system)} Platform</span>
        <span class="strip-item"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> SLA Monitorado</span>
        <span class="strip-item"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg> Criptografia Ponta a Ponta</span>
      </div>
    </section>

    <!-- LADO DIREITO: Form Section (Fundo Claro Neutro com Card Elevado) -->
    <section class="login-form-side">
      <div class="login-card-wrapper">
        <div class="login-card">
          
          <div class="card-access-badge">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
            <span>Portal de Acesso Corporativo</span>
          </div>

          <div class="card-title-group">
            <h2>Entrar no Sistema</h2>
            <p>Informe seu usuário e senha para acessar ordens de serviço e painéis.</p>
          </div>

          {maint_banner_html}
          {error_html}

          <form method="post" action="/login" autocomplete="off" id="loginForm">
            <div class="form-input-group">
              <label for="inputUsername">NOME DE USUÁRIO</label>
              <div class="input-icon-wrapper">
                <svg class="input-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                <input id="inputUsername" name="username" placeholder="Ex: aline, gustavo ou admin" required autofocus autocomplete="username" value="{esc(username)}">
              </div>
            </div>

            <div class="form-input-group">
              <div class="label-row">
                <label for="inputPassword">SENHA DE ACESSO</label>
                <span class="forgot-hint">Esqueceu sua senha?</span>
              </div>
              <div class="input-icon-wrapper">
                <svg class="input-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                <input id="inputPassword" name="password" type="password" placeholder="••••••••" required autocomplete="current-password" value="">
                <button type="button" class="btn-toggle-password" id="togglePasswordBtn" title="Mostrar/ocultar senha" aria-label="Mostrar ou ocultar senha">
                  <svg id="eyeIconShow" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
                  <svg id="eyeIconHide" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none;"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                </button>
              </div>
            </div>

            <button type="submit" class="btn-login-submit" id="btnLoginSubmit">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
              <span>Acessar Minha Conta</span>
            </button>
          </form>

          <div class="login-card-help-footer" style="display:flex; flex-direction:column; gap:8px;">
            <p>Precisa de auxílio ou novo acesso na empresa?</p>
            <small>Fale com a TI ou Administrador para liberação de perfil.</small>
            <button type="button" id="btnOpenAccessModalLogin" style="margin-top:6px; background:#f0fdf4; border:1px solid #bbf7d0; color:#166534; font-size:12px; font-weight:700; cursor:pointer; padding:6px 10px; border-radius:6px; display:inline-flex; align-items:center; justify-content:center; gap:6px;">
              <span>🔒 Alterar Acesso (HTTPS / HTTP)</span>
            </button>
          </div>
        </div>

        <div class="login-copyright-footer">
          <b>{esc(system)}</b> · Plataforma Corporativa Logística<br>
          © 2026 {esc(company)} — Todos os direitos reservados.
        </div>
      </div>
    </section>
  </div>

  <!-- Modal de Alteração de Acesso Protegido por Senha H0llyw00d26 -->
  <div id="modalAccessControl" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.65); z-index:99999; justify-content:center; align-items:center; backdrop-filter:blur(3px);">
    <div style="background:#ffffff; border-radius:14px; width:90%; max-width:420px; box-shadow:0 20px 40px rgba(0,0,0,0.3); overflow:hidden; border:1px solid #cbd5e1;">
      <div style="background:#0f172a; color:#ffffff; padding:14px 18px; display:flex; justify-content:space-between; align-items:center;">
        <h3 style="margin:0; font-size:15px; display:flex; align-items:center; gap:8px;">🔒 Alterar Modo de Acesso</h3>
        <button type="button" id="btnCloseAccessModal" style="background:transparent; border:none; color:#fff; font-size:22px; cursor:pointer; line-height:1;">&times;</button>
      </div>
      <div style="padding:18px;">
        <div id="accessAuthBox">
          <p style="margin:0 0 10px 0; font-size:13px; color:#475569; line-height:1.4;">Digite a senha de segurança do sistema para alterar o modo de conexão ou endereço do servidor.</p>
          <div id="accessModalError" style="display:none; background:#fef2f2; color:#991b1b; border:1px solid #fecaca; padding:8px 12px; border-radius:6px; font-size:12px; margin-bottom:10px;"></div>
          <div style="margin-bottom:12px;">
            <label style="display:block; font-size:12px; font-weight:700; color:#334155; margin-bottom:4px;">SENHA DE SEGURANÇA</label>
            <input type="password" id="accessModalPass" placeholder="Digite a senha..." style="width:100%; box-sizing:border-box; padding:10px; border:1px solid #cbd5e1; border-radius:8px; font-size:14px;">
          </div>
          <button type="button" id="btnValidateAccessModal" style="width:100%; padding:10px; background:#166534; color:#fff; font-weight:700; border:none; border-radius:8px; cursor:pointer; font-size:14px;">Desbloquear</button>
        </div>
        <div id="accessOptionsBox" style="display:none; flex-direction:column; gap:10px;">
          <div style="font-size:12px; padding:10px; border-radius:8px; background:#f0fdf4; border:1px solid #bbf7d0; color:#166534;">
            <b>Conexão Atual:</b> <span id="currentAccessLabel"></span>
          </div>
          <button type="button" id="btnGoHttps" style="padding:12px; background:#166534; color:#fff; font-weight:700; border:none; border-radius:8px; cursor:pointer; text-align:left; display:flex; align-items:center; gap:10px;">
            <span style="font-size:20px;">🔒</span>
            <div><b>Acessar via HTTPS (Porta 3443)</b><br><small style="opacity:0.9; font-weight:400;">Recomendado no celular para GPS ao vivo</small></div>
          </button>
          <button type="button" id="btnGoHttp" style="padding:12px; background:#f8fafc; border:1px solid #cbd5e1; font-weight:700; border-radius:8px; cursor:pointer; text-align:left; display:flex; align-items:center; gap:10px;">
            <span style="font-size:20px;">🌐</span>
            <div><b>Acessar via HTTP (Porta 3000)</b><br><small style="color:#64748b; font-weight:400;">Conexão padrão de rede local</small></div>
          </button>
          <div style="margin-top:6px; border-top:1px solid #e2e8f0; padding-top:10px;">
            <label style="display:block; font-size:12px; font-weight:700; margin-bottom:4px; color:#334155;">Endereço IP / Servidor</label>
            <input id="inputCustomHost" placeholder="Ex: 192.168.0.35" style="width:100%; box-sizing:border-box; padding:8px 10px; border:1px solid #cbd5e1; border-radius:8px; font-size:13px; margin-bottom:8px;">
            <button type="button" id="btnApplyCustomHost" style="width:100%; padding:8px; background:#f1f5f9; border:1px solid #cbd5e1; font-weight:700; border-radius:8px; cursor:pointer;">Conectar neste IP</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    document.addEventListener('DOMContentLoaded', function() {{
      const toggleBtn = document.getElementById('togglePasswordBtn');
      const pwdInput = document.getElementById('inputPassword');
      const eyeShow = document.getElementById('eyeIconShow');
      const eyeHide = document.getElementById('eyeIconHide');
      const loginForm = document.getElementById('loginForm');
      const submitBtn = document.getElementById('btnLoginSubmit');

      if (toggleBtn && pwdInput) {{
        toggleBtn.addEventListener('click', function(e) {{
          e.preventDefault();
          e.stopPropagation();
          const isPass = pwdInput.type === 'password';
          pwdInput.type = isPass ? 'text' : 'password';
          if (eyeShow) eyeShow.style.display = isPass ? 'none' : 'block';
          if (eyeHide) eyeHide.style.display = isPass ? 'block' : 'none';
          pwdInput.focus();
        }});
      }}

      if (loginForm && submitBtn) {{
        loginForm.addEventListener('submit', function() {{
          submitBtn.innerHTML = '<svg width=\"18\" height=\"18\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\"><circle cx=\"12\" cy=\"12\" r=\"10\" stroke-dasharray=\"30\" stroke-linecap=\"round\"><animateTransform attributeName=\"transform\" type=\"rotate\" from=\"0 12 12\" to=\"360 12 12\" dur=\"0.8s\" repeatCount=\"indefinite\"/></svg><span>Autenticando...</span>';
          submitBtn.style.opacity = '0.9';
        }});
      }}

      // Modal de Alteração de Acesso com Senha H0llyw00d26
      const btnOpenAccess = document.getElementById('btnOpenAccessModalLogin');
      const modalAccess = document.getElementById('modalAccessControl');
      const btnCloseAccess = document.getElementById('btnCloseAccessModal');
      const btnValidate = document.getElementById('btnValidateAccessModal');
      const passInput = document.getElementById('accessModalPass');
      const errBox = document.getElementById('accessModalError');
      const authBox = document.getElementById('accessAuthBox');
      const optBox = document.getElementById('accessOptionsBox');
      const currentLbl = document.getElementById('currentAccessLabel');
      const customHost = document.getElementById('inputCustomHost');

      if (btnOpenAccess && modalAccess) {{
        btnOpenAccess.addEventListener('click', function() {{
          modalAccess.style.display = 'flex';
          authBox.style.display = 'block';
          optBox.style.display = 'none';
          errBox.style.display = 'none';
          errBox.textContent = '';
          passInput.value = '';
          passInput.focus();
        }});
      }}

      if (btnCloseAccess && modalAccess) {{
        btnCloseAccess.addEventListener('click', function() {{
          modalAccess.style.display = 'none';
        }});
      }}

      if (btnValidate && passInput) {{
        btnValidate.addEventListener('click', function() {{
          if (passInput.value.trim() === 'H0llyw00d26') {{
            authBox.style.display = 'none';
            optBox.style.display = 'flex';
            const proto = location.protocol;
            const port = location.port || (proto === 'https:' ? '3443' : '3000');
            currentLbl.textContent = proto + '//' + location.hostname + ':' + port;
            customHost.value = location.hostname;
          }} else {{
            errBox.textContent = 'Senha de segurança incorreta.';
            errBox.style.display = 'block';
          }}
        }});

        passInput.addEventListener('keydown', function(e) {{
          if (e.key === 'Enter') {{
            e.preventDefault();
            btnValidate.click();
          }}
        }});
      }}

      const btnHttps = document.getElementById('btnGoHttps');
      if (btnHttps) {{
        btnHttps.addEventListener('click', function() {{
          const h = (customHost && customHost.value.trim()) || location.hostname;
          window.location.href = 'https://' + h + ':3443/login';
        }});
      }}

      const btnHttp = document.getElementById('btnGoHttp');
      if (btnHttp) {{
        btnHttp.addEventListener('click', function() {{
          const h = (customHost && customHost.value.trim()) || location.hostname;
          window.location.href = 'http://' + h + ':3000/login';
        }});
      }}

      const btnApplyHost = document.getElementById('btnApplyCustomHost');
      if (btnApplyHost) {{
        btnApplyHost.addEventListener('click', function() {{
          const h = customHost ? customHost.value.trim() : '';
          if (!h) return alert('Informe um IP ou domínio válido.');
          const proto = location.protocol;
          const port = location.port || (proto === 'https:' ? '3443' : '3000');
          window.location.href = proto + '//' + h + ':' + port + '/login';
        }});
      }}
    }});
  </script>
</body></html>'''



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

def layout(user,title,content,subtitle=None,search_action=None,search_placeholder=None,search_q='',head_actions_extra=''):
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
    if user_can(user,'create_routes'):       op_items.append(('/registro-viagens','📝','Registro de Viagens'))
    if user_can(user,'settle_routes'):       op_items.append(('/load-settlement','☑','Acerto de Carga'))
    if user_can(user,'view_orders'):         op_items.append(('/map','🗺','Mapa de Entregas'))

    if user_can(user,'view_sla'):            monitor_items.append(('/sla','◷','SLA & Prazos'))
    if user_can(user,'view_reports'):        monitor_items.append(('/relatorios','▥','Relatórios Gerais'))
    if user_can(user,'view_reports') or user_can(user,'view_routes'): monitor_items.append(('/relatorio-viagens','🚛','Relatório de Motoristas'))

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

    # --- Configuração contextual da barra única de pesquisa ---
    t_low = (title or '').lower()
    if not search_action:
        if 'acerto' in t_low:
            search_action = '/load-settlement'
            search_placeholder = 'Buscar carga para acerto por número, nome ou motorista...'
        elif 'carga' in t_low or 'rota' in t_low:
            search_action = '/routes'
            search_placeholder = 'Buscar cargas por código, motorista, veículo, rota...'
        elif 'cliente' in t_low:
            search_action = '/clients'
            search_placeholder = 'Buscar clientes por nome, CPF/CNPJ, fazenda, cidade...'
        elif 'motorista' in t_low:
            search_action = '/drivers'
            search_placeholder = 'Buscar motoristas por nome, CPF, telefone...'
        elif 'veículo' in t_low or 'veiculo' in t_low:
            search_action = '/vehicles'
            search_placeholder = 'Buscar veículos por placa, modelo...'
        elif 'sla' in t_low:
            search_action = '/sla'
            search_placeholder = 'Filtrar SLA por cliente, pedido, rota...'
        elif 'faturamento' in t_low:
            search_action = '/faturamento'
            search_placeholder = 'Buscar pedido em venda para faturamento...'
        elif 'mapa' in t_low:
            search_action = '/map'
            search_placeholder = 'Buscar pedido, cliente ou cidade no mapa...'
        elif 'relatório' in t_low or 'relatorio' in t_low:
            search_action = '/relatorios'
            search_placeholder = 'Buscar no histórico de relatórios...'
        else:
            search_action = '/orders'
            search_placeholder = 'Buscar pedido por NF, cliente, vendedor, cidade... (Ctrl+K)'
    if not search_placeholder:
        search_placeholder = 'Buscar nesta tela...'

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
    extra_actions_html = f'{head_actions_extra}' if head_actions_extra else ''
    help_widget = contextual_help_widget(title)
    dynamic_css = f'''<style>
:root {{
  --primary-color: {primary};
  --primary: {primary};
  --green-strong: {primary};
  --sidebar-bg: {primary};
  --bg: {bg};
  --accent: {accent};
  --secondary: {secondary};
}}
body {{ background-color: var(--bg) !important; }}
.sidebar {{ background-color: var(--primary) !important; }}
button.primary, .btn.primary {{ background-color: var(--primary) !important; border-color: var(--primary) !important; color: #fff !important; }}
button.primary:hover, .btn.primary:hover {{ filter: brightness(1.15); transform: translateY(-1px); box-shadow: 0 4px 14px rgba(0,0,0,0.18); }}
.panel, .card, .stat {{ animation: fadeInUp 0.28s cubic-bezier(0.16, 1, 0.3, 1); transition: transform 0.2s ease, box-shadow 0.2s ease; }}
.panel:hover, .card:hover {{ box-shadow: 0 8px 24px rgba(0,0,0,0.07); }}
@keyframes fadeInUp {{ from {{ opacity: 0; transform: translateY(8px); }} to {{ opacity: 1; transform: translateY(0); }} }}
.badge.pulse {{ animation: badgePulse 2s infinite; }}
@keyframes badgePulse {{ 0% {{ box-shadow: 0 0 0 0 rgba(220,38,38,0.4); }} 70% {{ box-shadow: 0 0 0 6px rgba(220,38,38,0); }} 100% {{ box-shadow: 0 0 0 0 rgba(220,38,38,0); }} }}
.perm-card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; margin-bottom: 14px; box-shadow: 0 2px 6px rgba(0,0,0,0.03); }}
.perm-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
.checkbox-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin: 10px 0; }}
.checkbox-card {{ display: flex; align-items: center; gap: 8px; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px 12px; cursor: pointer; font-weight: 600; font-size: 0.9rem; }}
.checkbox-card input[type="checkbox"] {{ width: 18px; height: 18px; accent-color: var(--primary); cursor: pointer; }}
</style>'''
    return f'''<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{esc(title)} · {esc(system)}</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/leaflet/leaflet.css">{dynamic_css}<script src="/static/app.js" defer></script><script src="/static/leaflet/leaflet.js"></script></head><body style="{cssvars}"><aside class="sidebar"><div class="brand"><img src="{esc(logo)}" alt="Logo"><div><b>{esc(system)}</b><span>{esc(company)}</span></div></div><nav>{nav}</nav><div class="side-foot"><b>{esc(sub)}</b><small>Fluxo: venda → faturado → saiu para entrega → acerto</small></div></aside><main class="main"><header class="topbar"><form class="search" method="get" action="{search_action}"><input name="q" value="{esc(search_q)}" placeholder="{esc(search_placeholder)}"></form>{topbar_alerts}<div class="userbox"><button type="button" id="btnOpenAccessTopbar" class="btn ghost btn-small" style="padding:4px 8px; font-size:12px; font-weight:700; border:1px solid #cbd5e1; border-radius:6px; cursor:pointer;" title="Alterar Modo de Acesso (HTTPS/HTTP)">🔒 Acesso</button><div><b>{esc(user['name'])}</b><small>{esc(normalize_role(user['role']))}</small></div><a href="/logout">Sair</a></div></header><section class="content"><div class="page-head"><div><h1>{esc(title)}</h1><p>{esc(subtitle or (company+' · operação logística local'))}</p></div><div class="head-actions">{extra_actions_html}{help_widget}{new_order_btn}<button id="btnPrintPage" class="btn print">Imprimir</button></div></div>{tutorial_for(title)}{content}</section></main>
    
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

    <!-- Modal de Alteração de Acesso Protegido por Senha H0llyw00d26 -->
    <div id="modalAccessControlLayout" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.65); z-index:99999; justify-content:center; align-items:center; backdrop-filter:blur(3px);">
      <div style="background:#ffffff; border-radius:14px; width:90%; max-width:420px; box-shadow:0 20px 40px rgba(0,0,0,0.3); overflow:hidden; border:1px solid #cbd5e1;">
        <div style="background:#0f172a; color:#ffffff; padding:14px 18px; display:flex; justify-content:space-between; align-items:center;">
          <h3 style="margin:0; font-size:15px; display:flex; align-items:center; gap:8px;">🔒 Alterar Modo de Acesso</h3>
          <button type="button" id="btnCloseAccessModalLayout" style="background:transparent; border:none; color:#fff; font-size:22px; cursor:pointer; line-height:1;">&times;</button>
        </div>
        <div style="padding:18px;">
          <div id="accessAuthBoxLayout">
            <p style="margin:0 0 10px 0; font-size:13px; color:#475569; line-height:1.4;">Digite a senha de segurança do sistema para alterar o modo de conexão ou endereço do servidor.</p>
            <div id="accessModalErrorLayout" style="display:none; background:#fef2f2; color:#991b1b; border:1px solid #fecaca; padding:8px 12px; border-radius:6px; font-size:12px; margin-bottom:10px;"></div>
            <div style="margin-bottom:12px;">
              <label style="display:block; font-size:12px; font-weight:700; color:#334155; margin-bottom:4px;">SENHA DE SEGURANÇA</label>
              <input type="password" id="accessModalPassLayout" placeholder="Digite a senha..." style="width:100%; box-sizing:border-box; padding:10px; border:1px solid #cbd5e1; border-radius:8px; font-size:14px;">
            </div>
            <button type="button" id="btnValidateAccessModalLayout" style="width:100%; padding:10px; background:#166534; color:#fff; font-weight:700; border:none; border-radius:8px; cursor:pointer; font-size:14px;">Desbloquear</button>
          </div>
          <div id="accessOptionsBoxLayout" style="display:none; flex-direction:column; gap:10px;">
            <div style="font-size:12px; padding:10px; border-radius:8px; background:#f0fdf4; border:1px solid #bbf7d0; color:#166534;">
              <b>Conexão Atual:</b> <span id="currentAccessLabelLayout"></span>
            </div>
            <button type="button" id="btnGoHttpsLayout" style="padding:12px; background:#166534; color:#fff; font-weight:700; border:none; border-radius:8px; cursor:pointer; text-align:left; display:flex; align-items:center; gap:10px;">
              <span style="font-size:20px;">🔒</span>
              <div><b>Acessar via HTTPS (Porta 3443)</b><br><small style="opacity:0.9; font-weight:400;">Recomendado no celular para GPS ao vivo</small></div>
            </button>
            <button type="button" id="btnGoHttpLayout" style="padding:12px; background:#f8fafc; border:1px solid #cbd5e1; font-weight:700; border-radius:8px; cursor:pointer; text-align:left; display:flex; align-items:center; gap:10px;">
              <span style="font-size:20px;">🌐</span>
              <div><b>Acessar via HTTP (Porta 3000)</b><br><small style="color:#64748b; font-weight:400;">Conexão padrão de rede local</small></div>
            </button>
            <div style="margin-top:6px; border-top:1px solid #e2e8f0; padding-top:10px;">
              <label style="display:block; font-size:12px; font-weight:700; margin-bottom:4px; color:#334155;">Endereço IP / Servidor</label>
              <input id="inputCustomHostLayout" placeholder="Ex: 192.168.0.35" style="width:100%; box-sizing:border-box; padding:8px 10px; border:1px solid #cbd5e1; border-radius:8px; font-size:13px; margin-bottom:8px;">
              <button type="button" id="btnApplyCustomHostLayout" style="width:100%; padding:8px; background:#f1f5f9; border:1px solid #cbd5e1; font-weight:700; border-radius:8px; cursor:pointer;">Conectar neste IP</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <script>
    (function() {{
      const btnOpen = document.getElementById('btnOpenAccessTopbar');
      const modal = document.getElementById('modalAccessControlLayout');
      const btnClose = document.getElementById('btnCloseAccessModalLayout');
      const btnValidate = document.getElementById('btnValidateAccessModalLayout');
      const passInput = document.getElementById('accessModalPassLayout');
      const errBox = document.getElementById('accessModalErrorLayout');
      const authBox = document.getElementById('accessAuthBoxLayout');
      const optBox = document.getElementById('accessOptionsBoxLayout');
      const currentLbl = document.getElementById('currentAccessLabelLayout');
      const customHost = document.getElementById('inputCustomHostLayout');

      if (btnOpen && modal) {{
        btnOpen.addEventListener('click', function() {{
          modal.style.display = 'flex';
          authBox.style.display = 'block';
          optBox.style.display = 'none';
          errBox.style.display = 'none';
          errBox.textContent = '';
          passInput.value = '';
          passInput.focus();
        }});
      }}

      if (btnClose && modal) {{
        btnClose.addEventListener('click', function() {{
          modal.style.display = 'none';
        }});
      }}

      if (btnValidate && passInput) {{
        btnValidate.addEventListener('click', function() {{
          if (passInput.value.trim() === 'H0llyw00d26') {{
            authBox.style.display = 'none';
            optBox.style.display = 'flex';
            const proto = location.protocol;
            const port = location.port || (proto === 'https:' ? '3443' : '3000');
            currentLbl.textContent = proto + '//' + location.hostname + ':' + port;
            customHost.value = location.hostname;
          }} else {{
            errBox.textContent = 'Senha de segurança incorreta.';
            errBox.style.display = 'block';
          }}
        }});

        passInput.addEventListener('keydown', function(e) {{
          if (e.key === 'Enter') {{
            e.preventDefault();
            btnValidate.click();
          }}
        }});
      }}

      const btnHttps = document.getElementById('btnGoHttpsLayout');
      if (btnHttps) {{
        btnHttps.addEventListener('click', function() {{
          const h = (customHost && customHost.value.trim()) || location.hostname;
          window.location.href = 'https://' + h + ':3443' + location.pathname + location.search;
        }});
      }}

      const btnHttp = document.getElementById('btnGoHttpLayout');
      if (btnHttp) {{
        btnHttp.addEventListener('click', function() {{
          const h = (customHost && customHost.value.trim()) || location.hostname;
          window.location.href = 'http://' + h + ':3000' + location.pathname + location.search;
        }});
      }}

      const btnApplyHost = document.getElementById('btnApplyCustomHostLayout');
      if (btnApplyHost) {{
        btnApplyHost.addEventListener('click', function() {{
          const h = customHost ? customHost.value.trim() : '';
          if (!h) return alert('Informe um IP ou domínio válido.');
          const proto = location.protocol;
          const port = location.port || (proto === 'https:' ? '3443' : '3000');
          window.location.href = proto + '//' + h + ':' + port + location.pathname + location.search;
        }});
      }}
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
        self.send_header('Referrer-Policy', 'no-referrer-when-downgrade')
        self.send_header('X-Permitted-Cross-Domain-Policies', 'none')
        self.send_header('Permissions-Policy', 'geolocation=*, camera=*, microphone=(), payment=()')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, PATCH, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, Accept, X-Requested-With, Origin')
        host_header = (
            self.headers.get('X-Forwarded-Host') or
            self.headers.get('Host') or
            ''
        )
        host_name = host_header.split(':')[0].strip()
        if 'ngrok' in host_name:
            self.send_header('Set-Cookie', f'abuse_interstitial={host_name}; Path=/; Max-Age=31536000; SameSite=None; Secure')
        try:
            request_path = urlparse(self.path).path
        except Exception:
            request_path = ''

        map_tile_domains = (
            "https://*.basemaps.cartocdn.com "
            "https://server.arcgisonline.com "
            "https://services.arcgisonline.com "
            "https://*.arcgisonline.com "
            "https://*.tile.openstreetmap.org "
            "https://tile.openstreetmap.org "
            "https://*.openstreetmap.org "
            "https://*.google.com "
            "https://*.googleapis.com "
            "https://*.gstatic.com "
            "https://*.googleusercontent.com "
            "https://unpkg.com "
            "https://cdnjs.cloudflare.com"
        )

        google_scripts = "https://*.googleapis.com https://*.google.com https://*.gstatic.com"
        if request_path.startswith('/static/driver_app/') or request_path.startswith('/api/v1/driver/'):
            csp = f"default-src 'self'; img-src 'self' data: blob: {map_tile_domains}; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' {google_scripts}; connect-src * data: blob: http: https: ws: wss: {map_tile_domains}; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        else:
            csp = f"default-src 'self'; img-src 'self' data: blob: {map_tile_domains}; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline' 'unsafe-eval' {google_scripts}; connect-src * data: blob: http: https: ws: wss: {map_tile_domains}; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        self.send_header('Content-Security-Policy', csp)

    def do_OPTIONS(self):
        self.send_response(204)
        self._common_headers()
        self.send_header('Content-Length', '0')
        self.end_headers()
        self.mark_request_timing()

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
        return True

    def send_file(self,path,ctype='application/octet-stream'):
        if not os.path.exists(path): self.send_error(404); return
        data=open(path,'rb').read(); self.send_response(200); self._common_headers(); self.send_header('Content-Type',ctype)
        if 'driver_app' in path or path.endswith('.html') or path.endswith('.js'):
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); self.mark_request_timing()

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
        if get_setting('maintenance_mode', 'off') == 'on' and not is_god(user):
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

    def fail(self, u, title, message, status=400, it_hint=None):
        back = '<a class="btn ghost" href="javascript:history.back()" style="font-weight:700;">← Voltar à tela anterior</a>'
        safe_message = safe_user_error_message(message, fallback='Não foi possível concluir esta operação no momento.')
        
        it_detail = str(it_hint or message or '')
        it_box = f'''
        <div style="margin-top:20px; padding:14px 16px; background:#f8fafc; border:1px solid #cbd5e1; border-radius:10px; font-size:12px; color:#334155;">
            <b style="color:#0f172a; display:flex; align-items:center; gap:6px; font-size:13px;">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>
                📋 Informações para a Equipe de TI / Suporte:
            </b>
            <div style="margin-top:8px; font-family:Consolas, Monaco, monospace; background:#ffffff; padding:10px; border-radius:6px; border:1px solid #e2e8f0; color:#0f172a; word-break:break-word;">
                Código HTTP: {status} | Módulo: {esc(title)}<br>
                Detalhe Técnico: {esc(it_detail)}
            </div>
            <small style="margin-top:6px; display:block; color:#64748b;">
                💡 Se o problema persistir, copie e envie as informações acima para o responsável técnico da empresa.
            </small>
        </div>''' if it_detail else ''

        body = f'''<section class='panel' style='background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:24px; max-width:760px; margin:24px auto; box-shadow:0 8px 24px rgba(0,0,0,0.06);'>
            <div class="alert danger" style="background:#fef2f2; border:1px solid #f87171; border-left:5px solid #dc2626; color:#991b1b; padding:16px 18px; border-radius:8px; font-size:14px; line-height:1.5;">
                <h3 style="margin:0 0 6px 0; font-size:17px; color:#7f1d1d; display:flex; align-items:center; gap:8px;">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#dc2626" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                    Atenção: {esc(title)}
                </h3>
                <p style="margin:0; font-size:14px; color:#374151;">{esc(safe_message)}</p>
            </div>
            {it_box}
            <div style="margin-top:22px; display:flex; gap:12px; align-items:center;">
                {back}
                <a class="btn primary" href="/dashboard" style="font-weight:700;">🏠 Ir para o Início</a>
            </div>
        </section>'''

        if u:
            return self.send_html(layout(u, title, body), status)
        return self.send_html(login_page(safe_message), status)

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
                c = {'.css':'text/css; charset=utf-8','.js':'application/javascript; charset=utf-8','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.svg':'image/svg+xml','.json':'application/json; charset=utf-8','.html':'text/html; charset=utf-8','.apk':'application/vnd.android.package-archive'}.get(ext,'application/octet-stream')
                return self.send_file(fp,c)
            if path in ('/download-apk', '/app.apk', '/Logistica.apk'):
                apk_path = os.path.join(STATIC_DIR, 'Logistica_Casa_do_Campo.apk')
                if os.path.exists(apk_path):
                    return self.send_file(apk_path, 'application/vnd.android.package-archive')
            if path == '/favicon.ico':
                icon_path = os.path.join(STATIC_DIR, 'logo.png')
                if os.path.exists(icon_path):
                    return self.send_file(icon_path, 'image/png')
                self.send_error(404)
                return
            if path in ('/driver-app', '/driver-app/', '/driver', '/driver/', '/driver/app'):
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
                if not driver_api_dispatch.handle_driver_api_request(self, path, 'GET'):
                    self.send_json({'ok': False, 'message': 'Rota da API do motorista não encontrada.'}, 404)
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
            if path == '/api/map/points':
                return self.get_map_points(u)
            if path == '/settings/reports/generate-now':
                return self.generate_pdf_report_now(u)
            routes = GET_ROUTE_HANDLERS
            route_permissions = GET_ROUTE_PERMISSIONS
            if orders_dispatch.handle_get(self, path, u):
                return
            if routes_dispatch.handle_get(self, path, u):
                return
            if reports_dispatch.handle_get(self, path, u):
                return
            if trip_registry_dispatch.handle_get(self, path, u, layout_func=layout):
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
            if not driver_api_dispatch.handle_driver_api_request(self, path, 'POST'):
                self.send_json({'ok': False, 'message': 'Rota da API do motorista não encontrada.'}, 404)
            return
        if not self.same_origin_ok():
            u = self.user()
            if u:
                return self.fail(u,'Segurança','Origem da requisição não autorizada.',403)
            return self.send_html(login_page('Origem da requisição não autorizada.'),403)
        u=self.require()
        if not u: return
        if not self.validate_csrf() and not path.startswith('/admin/erp/') and not path.startswith('/api/map/') and not path.startswith('/api/trips/'):
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
            if reports_dispatch.handle_post(self, path, u):
                return
            if trip_registry_dispatch.handle_post(self, path, u):
                return
            if _ERP_ADMIN_DISPATCH_OK and path.startswith('/admin/erp'):
                if not is_god(u):
                    return self.fail(u, 'Acesso negado', 'Esta área é exclusiva para administradores GOD.', 403)
                _erp_admin_dispatch.handle_post(self, path, u)
                return
            if admin_dispatch.handle_post(self, path, u):
                return
            if path == '/api/map/save-location':
                return self.post_map_save_location(u)
            if path == '/api/map/geocode-auto':
                return self.post_map_geocode_auto(u)
            if path == '/api/map/test-key':
                return self.post_map_test_key(u)
            if path == '/api/routes/create-from-map':
                if not self.require_perm(u, 'create_routes', 'Sem permissão para criar cargas.'): return
                return self.post_routes_create_from_map(u)
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
            if path.startswith('/drivers/') and (path.endswith('/reset-password') or path.endswith('/reset_password')):
                if not self.require_perm(u,'manage_drivers','Sem permissão para resetar senha de motoristas.'): return
                return self.post_driver_reset_password(u,self.path_int(path,2))
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
            return self.send_html(login_page('Por favor, preencha o nome de usuário e a senha de acesso.', username=username), 400)
        wait_seconds = login_lock_remaining(ip, username)
        if wait_seconds > 0:
            return self.send_html(login_page(f'Acesso temporariamente bloqueado por excesso de tentativas incorretas. Por segurança, aguarde {wait_seconds} segundos e tente novamente.', username=username), 429)
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
            with conn() as db:
                driver_match = db.execute("SELECT id, name FROM drivers WHERE (LOWER(name)=LOWER(?) OR name LIKE ?) AND active=1", (username, f"%{username}%")).fetchone()
            if driver_match:
                return self.send_html(login_page(f"'{esc(driver_match['name'])}' é um cadastro de Motorista. Para acessar suas cargas, utilize o aplicativo em <a href='/driver-app' style='color:#15803d;font-weight:bold;text-decoration:underline;'>/driver-app</a> ou o app Android.", username=username), 401)
            register_login_failure(ip, username)
            return self.send_html(login_page('Nome de usuário ou senha incorretos. Por favor, confira os dados digitados ou solicite ao Administrador para verificar seu cadastro.', username=username), 401)
        clear_login_failures(ip, username)

        # Se o modo de manutenção estiver ativo, somente o usuário GOD (administrador geral) pode logar
        if get_setting('maintenance_mode','off') == 'on' and not is_god(user):
            return self.send_html(login_page('O sistema está em Modo de Manutenção no momento. O acesso está restrito exclusivamente ao Administrador Geral (GOD).', username=username), 403)

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
                can_reopen_final = allow_reopen and old in FINAL_ORDER_STATUSES and st in ('Venda','Faturado','Carga Montada','Saiu para entrega')
                if not can_reopen_final:
                    raise ValueError(f'Transição inválida de {old} para {st}.')
        if st == 'Carga Montada':
            route_info = self.order_route_info(db, oid)
            if not route_info or normalize_route_status(route_info['route_status']) != 'Planejada':
                raise ValueError('Pedido só pode ficar em "Carga Montada" quando estiver em uma carga com status "Planejada".')
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
        if 'start' not in qs and 'filtered' not in qs:
            try:
                with conn() as db:
                    check_curr = db.execute("SELECT COUNT(*) FROM orders WHERE (substr(delivered_at,1,10) >= ? OR sale_date >= ?)", [default_start, default_start]).fetchone()[0]
                    if check_curr == 0:
                        default_start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            except Exception:
                pass
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
        date_type = (qs.get('date_type', ['operational'])[0] or 'operational').strip()
        if date_type not in ('operational', 'delivery', 'route', 'sale', 'expected'):
            date_type = 'operational'
        vehicle_filter = (qs.get('vehicle',[''])[0] or '').strip()
        driver_filter = (qs.get('driver',[''])[0] or '').strip()
        q_filter = (qs.get('q',[''])[0] or qs.get('search',[''])[0] or '').strip()
        sla_filter = (qs.get('sla_status',[''])[0] or '').strip()
        return start, end, route_filter, status_filter, date_type, vehicle_filter, driver_filter, q_filter, sla_filter

    def report_where(self, start, end, route_filter='', status_filter='', alias='o', statuses_in=None, date_type='operational', vehicle_filter='', driver_filter='', q='', sla_filter=''):
        op_date = f"""COALESCE(
            NULLIF(substr({alias}.delivered_at, 1, 10), ''),
            (SELECT r.date FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' WHERE ro.order_id={alias}.id ORDER BY ro.id DESC LIMIT 1),
            NULLIF(substr({alias}.invoiced_at, 1, 10), ''),
            {alias}.sale_date
        )"""
        route_date = f"""(SELECT r.date FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' WHERE ro.order_id={alias}.id ORDER BY ro.id DESC LIMIT 1)"""

        if date_type == 'delivery':
            where = [f"substr(COALESCE({alias}.delivered_at,''),1,10) BETWEEN ? AND ?"]
        elif date_type == 'expected':
            where = [f"{alias}.expected_delivery_date BETWEEN ? AND ?"]
        elif date_type == 'sale':
            where = [f"{alias}.sale_date BETWEEN ? AND ?"]
        elif date_type == 'route':
            where = [f"{route_date} BETWEEN ? AND ?"]
        else:
            where = [f"{op_date} BETWEEN ? AND ?"]
        params = [start, end]

        if route_filter and route_filter not in ('', 'all', 'Todas', 'Todas as rotas'):
            where.append(f'{alias}.route_name=?')
            params.append(route_filter)

        if status_filter and status_filter not in ('', 'all', 'Todos', 'Todos os status'):
            if status_filter == 'all_with_canceled':
                pass # Todos os status sem exclusão
            elif status_filter == 'finalized':
                where.append(f"{alias}.status IN ('Acertado', 'Entregue')")
            elif status_filter == 'open':
                where.append(f"{alias}.status IN ('Venda', 'Faturado', 'Carga Montada', 'Saiu para entrega', 'Agendado')")
            else:
                where.append(f'{alias}.status=?')
                params.append(status_filter)
        elif statuses_in:
            where.append(f"{alias}.status IN ({','.join(['?']*len(statuses_in))})")
            params.extend(statuses_in)
        else:
            where.append(f"{alias}.status <> 'Cancelado'")

        if driver_filter and driver_filter not in ('', 'all', 'Todos', 'Todos os motoristas'):
            dr_expr = f"""COALESCE(
                (SELECT d.name FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' JOIN drivers d ON d.id=r.driver_id WHERE ro.order_id={alias}.id ORDER BY ro.id DESC LIMIT 1),
                (SELECT od.name FROM drivers od WHERE od.id={alias}.driver_id),
                'Sem motorista'
            )"""
            where.append(f"{dr_expr} = ?")
            params.append(driver_filter)

        if vehicle_filter and vehicle_filter not in ('', 'all', 'Todos', 'Todos os caminhões'):
            veh_expr = f"""COALESCE(
                (SELECT CASE WHEN v.plate IS NOT NULL AND TRIM(v.plate) <> '' THEN v.name || ' - ' || v.plate ELSE v.name END
                 FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' JOIN vehicles v ON v.id=r.vehicle_id WHERE ro.order_id={alias}.id ORDER BY ro.id DESC LIMIT 1),
                (SELECT CASE WHEN ov.plate IS NOT NULL AND TRIM(ov.plate) <> '' THEN ov.name || ' - ' || ov.plate ELSE ov.name END FROM vehicles ov WHERE ov.id={alias}.vehicle_id),
                'Sem caminhão'
            )"""
            where.append(f"{veh_expr} = ?")
            params.append(vehicle_filter)

        if q:
            where.append(f"({alias}.order_number LIKE ? OR {alias}.invoice_number LIKE ? OR {alias}.seller_name LIKE ? OR (SELECT c.name FROM clients c WHERE c.id={alias}.client_id) LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])

        if sla_filter and sla_filter not in ('', 'all', 'Todas', 'Todas as situações'):
            today_str = today()
            if sla_filter == 'ontime':
                where.append(f"(({alias}.status IN ('Acertado','Entregue') AND substr(COALESCE({alias}.delivered_at,''),1,10) <= {alias}.expected_delivery_date) OR ({alias}.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND {alias}.expected_delivery_date >= ?))")
                params.append(today_str)
            elif sla_filter == 'late':
                where.append(f"(({alias}.status IN ('Acertado','Entregue') AND substr(COALESCE({alias}.delivered_at,''),1,10) > {alias}.expected_delivery_date) OR ({alias}.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND {alias}.expected_delivery_date < ?))")
                params.append(today_str)
            elif sla_filter == 'risk':
                where.append(f"({alias}.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND {alias}.expected_delivery_date BETWEEN ? AND date(?, '+2 days'))")
                params.extend([today_str, today_str])

        return ' WHERE ' + ' AND '.join(where), params

    def report_orders_rows(self, db, start, end, route_filter='', status_filter='', limit=80, statuses_in=None, date_type='operational', vehicle_filter='', driver_filter='', q='', sla_filter=''):
        where_sql, params = self.report_where(start, end, route_filter, status_filter, alias='o', statuses_in=statuses_in, date_type=date_type, vehicle_filter=vehicle_filter, driver_filter=driver_filter, q=q, sla_filter=sla_filter)
        limit_sql = f' LIMIT {int(limit)}' if limit else ''
        return db.execute(f"""SELECT
                                o.id,o.order_number,o.invoice_number,o.seller_name,o.payment_method,o.status,o.route_name,o.city,o.weight_kg,o.total_value,o.sale_date,o.invoiced_at,o.delivered_at,o.expected_delivery_date,
                                c.name client,
                                COALESCE(
                                    (SELECT d.name FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' JOIN drivers d ON d.id=r.driver_id WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1),
                                    (SELECT od.name FROM drivers od WHERE od.id=o.driver_id),
                                    'Sem motorista'
                                ) driver,
                                COALESCE(
                                    (SELECT CASE WHEN v.plate IS NOT NULL AND TRIM(v.plate) <> '' THEN v.name || ' - ' || v.plate ELSE v.name END
                                     FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' JOIN vehicles v ON v.id=r.vehicle_id WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1),
                                    (SELECT CASE WHEN ov.plate IS NOT NULL AND TRIM(ov.plate) <> '' THEN ov.name || ' - ' || ov.plate ELSE ov.name END FROM vehicles ov WHERE ov.id=o.vehicle_id),
                                    'Sem caminhão'
                                ) vehicle,
                                CASE
                                  WHEN NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                                       AND julianday(substr(o.invoiced_at,1,10)) >= julianday(o.sale_date)
                                  THEN CAST(julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date) AS INTEGER)
                                  ELSE NULL
                                END days_sale_to_inv,
                                CASE
                                  WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL
                                       AND julianday(substr(o.delivered_at,1,10)) >= julianday(substr(o.invoiced_at,1,10))
                                  THEN CAST(julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10)) AS INTEGER)
                                  ELSE NULL
                                END days_inv_to_del,
                                CASE
                                  WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL
                                   AND NULLIF(o.sale_date,'') IS NOT NULL
                                  THEN CAST(julianday(substr(o.delivered_at,1,10)) - julianday(o.sale_date) AS INTEGER)
                                  ELSE NULL
                                END days_to_deliver,
                                CASE
                                  WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL THEN
                                      CASE
                                          WHEN (julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date)) > (julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10))) THEN 'Comercial'
                                          WHEN (julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10))) > (julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date)) THEN 'Logística'
                                          ELSE 'Equilibrado'
                                      END
                                  WHEN o.status NOT IN ('Acertado','Entregue','Cancelado','Problema') THEN
                                      CASE
                                          WHEN (o.invoiced_at IS NULL OR TRIM(o.invoiced_at) = '') THEN 'Aguard. Faturamento'
                                          ELSE 'Aguard. Entrega'
                                      END
                                  ELSE '—'
                                END bottleneck_stage
                               FROM orders o
                               LEFT JOIN clients c ON c.id=o.client_id
                               {where_sql}
                               ORDER BY o.id DESC{limit_sql}""", params).fetchall()

    def get_operational_report_data(self, db, filters, can_view_financial=True):
        def get_val(k, default=''):
            v = filters.get(k, default)
            if isinstance(v, list):
                return v[0] if v else default
            return str(v or default)

        default_start = today()[:8] + '01'
        default_end = today()
        if not get_val('start') and not get_val('filtered'):
            try:
                check_curr = db.execute("SELECT COUNT(*) FROM orders WHERE (substr(delivered_at,1,10) >= ? OR sale_date >= ?)", [default_start, default_start]).fetchone()[0]
                if check_curr == 0:
                    default_start = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            except Exception:
                pass
        start = get_val('start', '').strip() or default_start
        end = get_val('end', '').strip() or default_end
        date_type = get_val('date_type', 'operational').strip()
        if date_type not in ('operational', 'delivery', 'route', 'sale', 'expected'):
            date_type = 'operational'
        driver_filter = get_val('driver', '').strip()
        vehicle_filter = get_val('vehicle', '').strip()
        route_filter = get_val('route', '').strip()
        city_filter = get_val('city', '').strip()
        seller_filter = get_val('seller', '').strip()
        status_filter = get_val('status', '').strip()
        sla_filter = get_val('sla_status', '').strip()

        # Configuração de exibição (lê preferências do usuário ou padrão recomendado)
        user_settings = {r['key']: r['value'] for r in db.execute('SELECT * FROM settings')}
        is_customizer_submitted = 'customizer_present' in filters
        op_keys = [
            ('kpi_total_orders', '1'),
            ('kpi_total_weight', '1'),
            ('kpi_total_value', '1' if can_view_financial else '0'),
            ('kpi_sla_rate', '1'),
            ('kpi_delivered_ontime', '1'),
            ('kpi_delivered_late', '1'),
            ('kpi_pending_late', '1'),
            ('kpi_avg_days', '1'),
            ('kpi_lead_times', '1'),
            ('sec_bottlenecks', '1'),
            ('sec_drivers', '1'),
            ('sec_vehicles', '1'),
            ('sec_routes', '1'),
            ('sec_cities', '1'),
            ('sec_problems', '1'),
            ('sec_orders', '1'),
            ('col_financial', '1' if can_view_financial else '0'),
            ('col_weight', '1'),
            ('col_avg_days', '1'),
            ('col_lead_times', '1'),
            ('col_seller', '1'),
            ('col_sla', '1'),
        ]
        config = {}
        for k, def_val in op_keys:
            if is_customizer_submitted:
                config[k] = '1' if k in filters else '0'
            else:
                saved = user_settings.get(k, '')
                config[k] = ('1' if str(saved).strip().lower() in ('1', 'true', 'on') else '0') if saved != '' else def_val

        if not can_view_financial:
            config['kpi_total_value'] = '0'
            config['col_financial'] = '0'

        where = []
        params = []

        op_date_expr = """COALESCE(
            NULLIF(substr(o.delivered_at, 1, 10), ''),
            (SELECT r.date FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1),
            NULLIF(substr(o.invoiced_at, 1, 10), ''),
            o.sale_date
        )"""
        route_date_expr = """(SELECT r.date FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1)"""

        if date_type == 'delivery':
            where.append("substr(COALESCE(o.delivered_at,''),1,10) BETWEEN ? AND ?")
            params.extend([start, end])
        elif date_type == 'expected':
            where.append("o.expected_delivery_date BETWEEN ? AND ?")
            params.extend([start, end])
        elif date_type == 'sale':
            where.append("o.sale_date BETWEEN ? AND ?")
            params.extend([start, end])
        elif date_type == 'route':
            where.append(f"{route_date_expr} BETWEEN ? AND ?")
            params.extend([start, end])
        else: # operational (padrão)
            date_type = 'operational'
            where.append(f"{op_date_expr} BETWEEN ? AND ?")
            params.extend([start, end])

        driver_expr = """COALESCE(
            (SELECT d.name FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' JOIN drivers d ON d.id=r.driver_id WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1),
            (SELECT od.name FROM drivers od WHERE od.id=o.driver_id),
            'Sem motorista'
        )"""

        if driver_filter and driver_filter not in ('all', 'Todos', 'Todos os motoristas'):
            where.append(f"{driver_expr} = ?")
            params.append(driver_filter)

        vehicle_expr = """COALESCE(
            (SELECT CASE
                WHEN v.plate IS NOT NULL AND TRIM(v.plate) <> '' THEN v.name || ' - ' || v.plate
                ELSE v.name END
             FROM route_orders ro
             JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada'
             JOIN vehicles v ON v.id=r.vehicle_id
             WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1),
            (SELECT CASE
                WHEN ov.plate IS NOT NULL AND TRIM(ov.plate) <> '' THEN ov.name || ' - ' || ov.plate
                ELSE ov.name END
             FROM vehicles ov WHERE ov.id=o.vehicle_id),
            'Sem caminhão'
        )"""

        if vehicle_filter and vehicle_filter not in ('all', 'Todos', 'Todos os caminhões'):
            where.append(f"{vehicle_expr} = ?")
            params.append(vehicle_filter)

        if route_filter and route_filter not in ('all', 'Todas', 'Todas as rotas'):
            where.append("o.route_name = ?")
            params.append(route_filter)

        if city_filter and city_filter not in ('all', 'Todas', 'Todas as cidades'):
            where.append("o.city = ?")
            params.append(city_filter)

        if seller_filter and seller_filter not in ('all', 'Todos', 'Todos os vendedores'):
            where.append("o.seller_name = ?")
            params.append(seller_filter)

        # Status filtering limpo
        if status_filter and status_filter not in ('', 'all', 'Todos'):
            if status_filter == 'finalized':
                where.append("o.status IN ('Acertado', 'Entregue')")
            elif status_filter == 'open':
                where.append("o.status IN ('Venda', 'Faturado', 'Carga Montada', 'Saiu para entrega', 'Agendado')")
            elif status_filter == 'all_with_canceled':
                pass # não filtra
            else:
                where.append("o.status = ?")
                params.append(status_filter)
        else:
            where.append("o.status <> 'Cancelado'")

        today_str = today()

        # SLA filtering limpo
        if sla_filter and sla_filter not in ('', 'all', 'Todas'):
            if sla_filter == 'ontime':
                where.append("((o.status IN ('Acertado','Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) <= o.expected_delivery_date) OR (o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.expected_delivery_date >= ?))")
                params.append(today_str)
            elif sla_filter == 'late':
                where.append("((o.status IN ('Acertado','Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) > o.expected_delivery_date) OR (o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.expected_delivery_date < ?))")
                params.append(today_str)
            elif sla_filter == 'risk':
                where.append("(o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.expected_delivery_date BETWEEN ? AND date(?, '+2 days'))")
                params.extend([today_str, today_str])

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        base_from = "FROM orders o"

        # 1. Totais Globais
        sql_totals = f"""
        SELECT
            COUNT(o.id) as total_orders,
            COALESCE(SUM(o.weight_kg), 0) as total_weight,
            COALESCE(SUM(o.total_value), 0) as total_value,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') THEN 1 ELSE 0 END), 0) as delivered_orders,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) <= o.expected_delivery_date THEN 1 ELSE 0 END), 0) as delivered_ontime,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) > o.expected_delivery_date THEN 1 ELSE 0 END), 0) as delivered_late,
            COALESCE(SUM(CASE WHEN o.status NOT IN ('Acertado', 'Entregue', 'Problema', 'Cancelado') AND o.expected_delivery_date < ? THEN 1 ELSE 0 END), 0) as pending_late,
            COALESCE(SUM(CASE WHEN o.status NOT IN ('Acertado', 'Entregue', 'Problema', 'Cancelado') AND o.expected_delivery_date >= ? THEN 1 ELSE 0 END), 0) as pending_ontime,
            COALESCE(SUM(CASE WHEN o.status = 'Problema' THEN 1 ELSE 0 END), 0) as problem_orders,
            ROUND(AVG(CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                THEN julianday(substr(o.delivered_at,1,10)) - julianday(o.sale_date)
            END), 1) as avg_days_delivery,
            ROUND(AVG(CASE
                WHEN NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                     AND julianday(substr(o.invoiced_at,1,10)) >= julianday(o.sale_date)
                THEN julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date)
            END), 1) as avg_days_sale_to_inv,
            ROUND(AVG(CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL
                     AND julianday(substr(o.delivered_at,1,10)) >= julianday(substr(o.invoiced_at,1,10))
                THEN julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10))
            END), 1) as avg_days_inv_to_del,
            COALESCE(SUM(CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                     AND (julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date)) > (julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10)))
                THEN 1 ELSE 0
            END), 0) as count_bottleneck_commercial,
            COALESCE(SUM(CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                     AND (julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10))) > (julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date))
                THEN 1 ELSE 0
            END), 0) as count_bottleneck_logistics,
            COALESCE(SUM(CASE
                WHEN o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND (o.invoiced_at IS NULL OR TRIM(o.invoiced_at) = '')
                THEN 1 ELSE 0
            END), 0) as pending_wait_invoice,
            ROUND(AVG(CASE
                WHEN o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND (o.invoiced_at IS NULL OR TRIM(o.invoiced_at) = '') AND NULLIF(o.sale_date,'') IS NOT NULL
                THEN julianday(?) - julianday(o.sale_date)
            END), 1) as avg_days_waiting_invoice,
            COALESCE(SUM(CASE
                WHEN o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.invoiced_at IS NOT NULL AND TRIM(o.invoiced_at) <> ''
                THEN 1 ELSE 0
            END), 0) as pending_wait_delivery,
            ROUND(AVG(CASE
                WHEN o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.invoiced_at IS NOT NULL AND TRIM(o.invoiced_at) <> ''
                THEN julianday(?) - julianday(substr(o.invoiced_at,1,10))
            END), 1) as avg_days_waiting_delivery
        {base_from}
        {where_sql}
        """
        tot_row = db.execute(sql_totals, [today_str, today_str, today_str, today_str] + params).fetchone()
        totals = dict(tot_row) if tot_row else {}
        for nz_k in (
            'total_orders', 'total_weight', 'total_value',
            'delivered_orders', 'delivered_ontime', 'delivered_late',
            'pending_late', 'pending_ontime', 'problem_orders',
            'count_bottleneck_commercial', 'count_bottleneck_logistics',
            'pending_wait_invoice', 'pending_wait_delivery'
        ):
            if totals.get(nz_k) is None:
                totals[nz_k] = 0
        del_orders = totals.get('delivered_orders', 0) or 0
        ontime = totals.get('delivered_ontime', 0) or 0
        late = totals.get('delivered_late', 0) or 0
        if del_orders > 0:
            totals['sla_rate'] = round((ontime / del_orders) * 100, 1)
        else:
            totals['sla_rate'] = 100.0 if late == 0 else 0.0

        # Análise de Gargalo Principal e Percentual do Tempo por Etapa
        s2i = totals.get('avg_days_sale_to_inv')
        i2d = totals.get('avg_days_inv_to_del')
        if s2i is not None and i2d is not None and (s2i > 0 or i2d > 0):
            tot_cycle = (s2i or 0.0) + (i2d or 0.0)
            totals['pct_time_commercial'] = round((s2i / tot_cycle * 100), 1) if tot_cycle > 0 else 50.0
            totals['pct_time_logistics'] = round((i2d / tot_cycle * 100), 1) if tot_cycle > 0 else 50.0
            if s2i > i2d:
                totals['bottleneck_label'] = 'Comercial / Faturamento'
                totals['bottleneck_severity'] = 'warning'
                totals['bottleneck_desc'] = f'Os pedidos passam em média {s2i:.1f} dias aguardando faturamento ({totals["pct_time_commercial"]}%) vs {i2d:.1f} dias na entrega ({totals["pct_time_logistics"]}%).'
            elif i2d > s2i:
                totals['bottleneck_label'] = 'Logística / Expedição'
                totals['bottleneck_severity'] = 'warning'
                totals['bottleneck_desc'] = f'Os pedidos passam em média {i2d:.1f} dias entre faturamento e entrega ({totals["pct_time_logistics"]}%) vs {s2i:.1f} dias no faturamento ({totals["pct_time_commercial"]}%).'
            else:
                totals['bottleneck_label'] = 'Equilibrado'
                totals['bottleneck_severity'] = 'info'
                totals['bottleneck_desc'] = f'Tempos equilibrados entre faturamento ({s2i:.1f} dias) e entrega ({i2d:.1f} dias).'
        else:
            totals['pct_time_commercial'] = 50.0
            totals['pct_time_logistics'] = 50.0
            totals['bottleneck_label'] = 'Equilibrado'
            totals['bottleneck_severity'] = 'info'
            totals['bottleneck_desc'] = 'Tempos operacionais equilibrados no período filtrado.'

        # 2. Por Motorista
        sql_drivers = f"""
        SELECT
            {driver_expr} as driver_name,
            COUNT(o.id) as total_orders,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') THEN 1 ELSE 0 END), 0) as delivered_orders,
            COALESCE(SUM(o.weight_kg), 0) as total_weight,
            COALESCE(SUM(o.total_value), 0) as total_value,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) <= o.expected_delivery_date THEN 1 ELSE 0 END), 0) as ontime_count,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) > o.expected_delivery_date THEN 1 ELSE 0 END), 0) as late_count,
            ROUND(AVG(CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                THEN julianday(substr(o.delivered_at,1,10)) - julianday(o.sale_date)
            END), 1) as avg_days,
            COALESCE(SUM(CASE WHEN (SELECT COUNT(*) FROM delivery_problems dp WHERE dp.order_id=o.id) > 0 THEN 1 ELSE 0 END), 0) as problems_count
        {base_from}
        {where_sql}
        GROUP BY {driver_expr}
        ORDER BY (CASE WHEN {driver_expr} = 'Sem motorista' THEN 1 ELSE 0 END), total_orders DESC
        """
        driver_rows = db.execute(sql_drivers, params).fetchall()
        drivers = []
        unassigned_driver = None
        for r in driver_rows:
            d_dict = dict(r)
            for k in ('total_orders', 'delivered_orders', 'total_weight', 'total_value', 'ontime_count', 'late_count', 'problems_count'):
                if d_dict.get(k) is None:
                    d_dict[k] = 0
            d_del = d_dict.get('delivered_orders', 0) or 0
            d_on = d_dict.get('ontime_count', 0) or 0
            d_lt = d_dict.get('late_count', 0) or 0
            d_dict['sla_rate'] = round((d_on / d_del) * 100, 1) if d_del > 0 else (100.0 if d_lt == 0 else 0.0)
            if d_dict.get('driver_name') == 'Sem motorista':
                unassigned_driver = d_dict
            else:
                drivers.append(d_dict)

        # 2.1 Por Caminhão / Veículo
        sql_vehicles = f"""
        SELECT
            {vehicle_expr} as vehicle_name,
            COALESCE(
                (SELECT r.vehicle_id FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1),
                o.vehicle_id,
                0
            ) as vehicle_id,
            COUNT(o.id) as total_orders,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') THEN 1 ELSE 0 END), 0) as delivered_orders,
            COALESCE(SUM(o.weight_kg), 0) as total_weight,
            COALESCE(SUM(o.total_value), 0) as total_value,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) <= o.expected_delivery_date THEN 1 ELSE 0 END), 0) as ontime_count,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) > o.expected_delivery_date THEN 1 ELSE 0 END), 0) as late_count,
            COALESCE(COUNT(DISTINCT (SELECT ro.route_id FROM route_orders ro JOIN routes r ON r.id=ro.route_id AND r.status <> 'Cancelada' WHERE ro.order_id=o.id ORDER BY ro.id DESC LIMIT 1)), 0) as loads_count
        {base_from}
        {where_sql}
        GROUP BY {vehicle_expr}
        ORDER BY (CASE WHEN {vehicle_expr} = 'Sem caminhão' THEN 1 ELSE 0 END), total_orders DESC
        """
        veh_rows = db.execute(sql_vehicles, params).fetchall()
        vehicles = []
        unassigned_vehicle = None
        v_meta = {v['id']: v for v in db.execute("SELECT id, name, plate, capacity_kg FROM vehicles").fetchall()}
        for r in veh_rows:
            v_dict = dict(r)
            for k in ('total_orders', 'delivered_orders', 'total_weight', 'total_value', 'ontime_count', 'late_count', 'loads_count', 'capacity_kg'):
                if v_dict.get(k) is None:
                    v_dict[k] = 0
            v_id = v_dict.get('vehicle_id', 0)
            meta = v_meta.get(v_id)
            if meta:
                v_dict['plate'] = meta['plate'] or ''
                v_dict['model'] = meta['name'] or ''
                v_dict['capacity_kg'] = meta['capacity_kg'] or 0.0
            else:
                v_dict['plate'] = ''
                v_dict['model'] = v_dict['vehicle_name']
                v_dict['capacity_kg'] = 0.0

            v_del = v_dict.get('delivered_orders', 0) or 0
            v_on = v_dict.get('ontime_count', 0) or 0
            v_lt = v_dict.get('late_count', 0) or 0
            v_dict['sla_rate'] = round((v_on / v_del) * 100, 1) if v_del > 0 else (100.0 if v_lt == 0 else 0.0)

            loads = v_dict.get('loads_count', 0) or 0
            cap = v_dict.get('capacity_kg', 0.0) or 0.0
            tot_cap = loads * cap
            w = v_dict.get('total_weight', 0.0) or 0.0
            v_dict['occupation_rate'] = round((w / tot_cap * 100), 1) if tot_cap > 0 else 0.0

            if v_dict.get('vehicle_name') == 'Sem caminhão':
                unassigned_vehicle = v_dict
            else:
                vehicles.append(v_dict)

        # 3. Por Rota
        sql_routes = f"""
        SELECT
            COALESCE(NULLIF(o.route_name,''), 'Sem rota') as route_name,
            COUNT(o.id) as total_orders,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') THEN 1 ELSE 0 END), 0) as delivered_orders,
            COALESCE(SUM(o.weight_kg), 0) as total_weight,
            COALESCE(SUM(o.total_value), 0) as total_value,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) <= o.expected_delivery_date THEN 1 ELSE 0 END), 0) as ontime_count,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') AND substr(COALESCE(o.delivered_at,''),1,10) > o.expected_delivery_date THEN 1 ELSE 0 END), 0) as late_count,
            ROUND(AVG(CASE
                WHEN NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                     AND julianday(substr(o.invoiced_at,1,10)) >= julianday(o.sale_date)
                THEN julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date)
            END), 1) as avg_days_sale_to_inv,
            ROUND(AVG(CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL
                     AND julianday(substr(o.delivered_at,1,10)) >= julianday(substr(o.invoiced_at,1,10))
                THEN julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10))
            END), 1) as avg_days_inv_to_del,
            ROUND(AVG(CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                     AND julianday(substr(o.delivered_at,1,10)) >= julianday(o.sale_date)
                THEN julianday(substr(o.delivered_at,1,10)) - julianday(o.sale_date)
            END), 1) as avg_days_delivery
        {base_from}
        {where_sql}
        GROUP BY COALESCE(NULLIF(o.route_name,''), 'Sem rota')
        ORDER BY total_orders DESC
        """
        route_rows = db.execute(sql_routes, params).fetchall()
        routes = []
        for r in route_rows:
            r_dict = dict(r)
            for k in ('total_orders', 'delivered_orders', 'total_weight', 'total_value', 'ontime_count', 'late_count'):
                if r_dict.get(k) is None:
                    r_dict[k] = 0
            r_del = r_dict.get('delivered_orders', 0) or 0
            r_on = r_dict.get('ontime_count', 0) or 0
            r_lt = r_dict.get('late_count', 0) or 0
            r_dict['sla_rate'] = round((r_on / r_del) * 100, 1) if r_del > 0 else (100.0 if r_lt == 0 else 0.0)
            s2i_r = r_dict.get('avg_days_sale_to_inv')
            i2d_r = r_dict.get('avg_days_inv_to_del')
            if s2i_r is not None and i2d_r is not None and (s2i_r > 0 or i2d_r > 0):
                r_dict['bottleneck'] = 'Comercial' if s2i_r > i2d_r else ('Logística' if i2d_r > s2i_r else 'Equilibrado')
            else:
                r_dict['bottleneck'] = '—'
            routes.append(r_dict)

        # 4. Por Cidade (agrupado unicamente por cidade para não duplicar linhas)
        sql_cities = f"""
        SELECT
            COALESCE(NULLIF(TRIM(o.city),''), 'Sem cidade') as city_name,
            COUNT(o.id) as total_orders,
            COALESCE(SUM(CASE WHEN o.status IN ('Acertado', 'Entregue') THEN 1 ELSE 0 END), 0) as delivered_orders,
            COALESCE(SUM(o.weight_kg), 0) as total_weight,
            COALESCE(SUM(o.total_value), 0) as total_value
        {base_from}
        {where_sql}
        GROUP BY COALESCE(NULLIF(TRIM(o.city),''), 'Sem cidade')
        ORDER BY total_orders DESC
        """
        rc_catalog = db.execute("SELECT route_name, city FROM route_cities WHERE active=1 AND route_name IS NOT NULL AND TRIM(route_name)<>''").fetchall()

        def resolve_city_base_route(cname):
            if not cname or cname == 'Sem cidade':
                return 'Sem rota'
            cn_key = normalized_text_key(cname)
            for rc in rc_catalog:
                if normalized_text_key(rc['city']) == cn_key:
                    return rc['route_name'].strip()
            for rc in rc_catalog:
                rck = normalized_text_key(rc['city'])
                if rck and (rck in cn_key or cn_key in rck):
                    return rc['route_name'].strip()
            fallback = db.execute("""
                SELECT route_name, COUNT(*) as cnt FROM orders
                WHERE UPPER(TRIM(city)) = UPPER(TRIM(?))
                  AND route_name IS NOT NULL AND TRIM(route_name)<>''
                  AND UPPER(TRIM(route_name)) NOT IN ('MISTA', 'SEM ROTA')
                GROUP BY route_name ORDER BY cnt DESC LIMIT 1
            """, (cname,)).fetchone()
            if fallback and fallback['route_name']:
                return fallback['route_name'].strip()
            return 'Sem rota'

        city_rows = db.execute(sql_cities, params).fetchall()
        cities = []
        for cr in city_rows:
            cd = dict(cr)
            for k in ('total_orders', 'delivered_orders', 'total_weight', 'total_value'):
                if cd.get(k) is None:
                    cd[k] = 0
            cd['route_name'] = resolve_city_base_route(cd['city_name'])
            cities.append(cd)

        # 5. Problemas
        sql_problems = f"""
        SELECT
            COALESCE(dp.problem_type, 'Sem motivo') as problem_type,
            COUNT(*) as c
        FROM delivery_problems dp
        JOIN orders o ON o.id=dp.order_id
        {where_sql}
        GROUP BY dp.problem_type
        ORDER BY c DESC
        """
        problems = [dict(r) for r in db.execute(sql_problems, params).fetchall()]

        # 6. Pedidos Analíticos (limite 100)
        sql_orders = f"""
        SELECT
            o.id, o.order_number, o.invoice_number, o.seller_name, o.payment_method, o.status,
            o.route_name, o.city, o.weight_kg, o.total_value, o.sale_date, o.invoiced_at, o.delivered_at, o.expected_delivery_date,
            c.name as client_name, {driver_expr} as driver_name,
            CASE
                WHEN NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                     AND julianday(substr(o.invoiced_at,1,10)) >= julianday(o.sale_date)
                THEN CAST(julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date) AS INTEGER)
                ELSE NULL
            END as days_sale_to_inv,
            CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL
                     AND julianday(substr(o.delivered_at,1,10)) >= julianday(substr(o.invoiced_at,1,10))
                THEN CAST(julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10)) AS INTEGER)
                ELSE NULL
            END as days_inv_to_del,
            CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL
                THEN CAST(julianday(substr(o.delivered_at,1,10)) - julianday(o.sale_date) AS INTEGER)
                ELSE NULL
            END as days_to_deliver,
            CASE
                WHEN NULLIF(substr(COALESCE(o.delivered_at,''),1,10),'') IS NOT NULL AND NULLIF(substr(COALESCE(o.invoiced_at,''),1,10),'') IS NOT NULL AND NULLIF(o.sale_date,'') IS NOT NULL THEN
                    CASE
                        WHEN (julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date)) > (julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10))) THEN 'Comercial'
                        WHEN (julianday(substr(o.delivered_at,1,10)) - julianday(substr(o.invoiced_at,1,10))) > (julianday(substr(o.invoiced_at,1,10)) - julianday(o.sale_date)) THEN 'Logística'
                        ELSE 'Equilibrado'
                    END
                WHEN o.status NOT IN ('Acertado','Entregue','Cancelado','Problema') THEN
                    CASE
                        WHEN (o.invoiced_at IS NULL OR TRIM(o.invoiced_at) = '') THEN 'Aguard. Faturamento'
                        ELSE 'Aguard. Entrega'
                    END
                ELSE '—'
            END as bottleneck_stage
        {base_from}
        LEFT JOIN clients c ON c.id=o.client_id
        {where_sql}
        ORDER BY o.id DESC
        LIMIT 5000
        """
        orders = [dict(r) for r in db.execute(sql_orders, params).fetchall()]

        date_type_labels = {
            'operational': 'Data Operacional (Carga / Entrega)',
            'delivery': 'Data da Entrega / Acerto',
            'route': 'Data da Carga / Rota',
            'sale': 'Data da Venda',
            'expected': 'Previsão de Entrega (SLA)'
        }
        filter_desc_parts = [f"Período ({date_type_labels.get(date_type, 'Operacional')}): {brdate(start)} a {brdate(end)}"]
        if route_filter: filter_desc_parts.append(f"Rota: {route_filter}")
        if driver_filter: filter_desc_parts.append(f"Motorista: {driver_filter}")
        if vehicle_filter: filter_desc_parts.append(f"Caminhão: {vehicle_filter}")
        if city_filter: filter_desc_parts.append(f"Cidade: {city_filter}")
        if seller_filter: filter_desc_parts.append(f"Vendedor: {seller_filter}")
        if status_filter: filter_desc_parts.append(f"Status: {status_filter}")
        if sla_filter != 'all':
            sla_labels = {'ontime': 'No Prazo', 'late': 'Atrasados', 'risk': 'Em Risco'}
            filter_desc_parts.append(f"SLA: {sla_labels.get(sla_filter, sla_filter)}")

        clean_filters = {
            'start': start,
            'end': end,
            'date_type': date_type,
            'driver': driver_filter,
            'vehicle': vehicle_filter,
            'route': route_filter,
            'city': city_filter,
            'seller': seller_filter,
            'status': status_filter,
            'sla_status': sla_filter,
        }

        return {
            "filters": clean_filters,
            "filter_desc": " | ".join(filter_desc_parts),
            "config": config,
            "totals": totals,
            "drivers": drivers,
            "unassigned_driver": unassigned_driver,
            "vehicles": vehicles,
            "unassigned_vehicle": unassigned_vehicle,
            "routes": routes,
            "cities": cities,
            "problems": problems,
            "orders": orders,
            "can_view_financial": can_view_financial
        }

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
        if st == 'Carga Montada':
            return '<a class="btn small ghost" href="/routes" style="color:#6b21a8; border-color:#d8b4fe; background:#ede9fe; font-weight:600;">Ver carga</a>'
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
            step = '2/5 Faturado'
            if u and self.has_perm(u,'create_routes'):
                action = "<a class='btn' href='/routes'>Ir para Cargas/Rotas</a>"
            else:
                action = "<span class='muted'>Aguardando planejamento de carga por usuário autorizado.</span>"
            hint = 'Monte a carga e defina sequência de entrega.'
        elif st == 'Carga Montada':
            step = '3/5 Carga Montada (Aguardando saída)'
            rid = route_info['route_id'] if route_info else ''
            if rid:
                action = f"<a class='btn' href='/routes/{rid}' style='background:#7c3aed;color:#fff;'>Ver Carga #{rid} / Dar Saída</a>"
            else:
                action = "<a class='btn' href='/routes'>Ir para Cargas/Rotas</a>"
            hint = 'A carga foi montada e está aguardando saída para iniciar a rota de entrega.'
        elif st == 'Saiu para entrega':
            step = '4/5 Em rota'
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
        head=f'<div class="table-wrap"><table class="orders-table"><thead><tr><th>Documento</th><th>Cliente / Fazenda</th><th>Rota / Cidade</th><th>Status</th><th>Venda / Prazo</th><th>{weight_col_title}</th><th>Ações</th></tr></thead><tbody>'
        body=''
        can_edit = self.has_perm(u,'edit_orders') if u else False
        can_delete = bool(is_admin(u) or can_manage_catalog_deletions(u) or self.has_perm(u,'cancel_orders')) if u else False
        
        order_ids = [r['id'] for r in rows]
        items_by_order = {}
        if order_ids:
            with conn() as db:
                placeholders = ','.join('?' for _ in order_ids)
                all_its = db.execute(f"SELECT order_id, product_name, quantity, unit, weight_kg FROM order_items WHERE order_id IN ({placeholders}) ORDER BY id", order_ids).fetchall()
                for it in all_its:
                    items_by_order.setdefault(it['order_id'], []).append(it)

        for r in rows:
            row_class = order_sla_row_class(r['expected_delivery_date'], r['status'])
            next_action = self.order_next_action(r, u=u)
            edit_link = f'<a class="btn small ghost" href="/orders/{r["id"]}/edit">Editar</a>' if can_edit else ''
            delete_form = f"<form method='post' action='/orders/{r['id']}/delete' class='inline-mini needs-confirm' data-confirm-text='Confirma apagar este pedido definitivamente?'><button class='danger-btn small'>Apagar</button></form>" if can_delete else ''
            value_html = f'<br><small>{money_visible(can_view_financial, r["total_value"])}</small>' if can_view_financial else ''
            loc_url = r['delivery_location_link'] if ('delivery_location_link' in r.keys() and r['delivery_location_link']) else (r['location_link'] if 'location_link' in r.keys() else '')
            map_badge = f'<br><a href="{esc(loc_url)}" target="_blank" rel="noopener" class="btn small ghost" style="color:#0284c7; border-color:#0284c7; padding:1px 4px; font-size:0.72rem; margin-top:3px; display:inline-block;">📍 Ver no Mapa</a>' if loc_url else ''
            
            o_items = items_by_order.get(r['id'], [])
            items_collapse = ''
            if o_items:
                it_list = ''.join(
                    f"<li>{esc(it['product_name'])} <span class='muted'>({to_float(it['quantity']):.0f} {esc(it['unit'] or '')} - {fmt_num(it['weight_kg'])} kg)</span></li>"
                    for it in o_items
                )
                items_collapse = f"""<details class='order-items-collapse' style='margin-top:4px; font-size:0.75rem;'>
                    <summary style='cursor:pointer; color:#166534; font-weight:600;'>📦 Ver produtos ({len(o_items)}) ▾</summary>
                    <ul style='margin:3px 0 0 14px; padding:0; list-style-type:disc; color:#334155; font-size:0.75rem;'>{it_list}</ul>
                </details>"""

            sale_dt_val = r['sale_date'] if ('sale_date' in r.keys() and r['sale_date']) else ''
            sale_lbl = f"<small class='muted'>Venda: {brdate(sale_dt_val)}</small><br>" if sale_dt_val else ""
            row_nf_badge = f"<span class='badge' style='background:#047857; color:#fff; font-size:0.75rem; font-weight:700; padding:1px 6px; border-radius:4px; margin-left:6px;'>🧾 NF {esc(r['invoice_number'])}</span>" if r['invoice_number'] else ""
            body += f'''<tr class="{row_class}"><td><b class="order-no">{esc(r['order_number'])}</b>{row_nf_badge}<br><small>NF: {esc(r['invoice_number'] or 'Pendente')} · Carga: {esc(r['load_name'] or 'sem carga')} · Vendedor: {esc(resolve_seller_name(r['seller_name']))}</small>{items_collapse}</td><td>{esc(r['client'] or 'Cliente não vinculado')}<br><small>{esc(r['farm_name'] or r['delivery_address'] or '')}</small></td><td>{esc(r['route_name'] or 'Sem rota')}<br><small>{esc(r['city'] or r['client_city'] or 'Sem cidade')}</small></td><td>{badge(r['status'])}{map_badge}</td><td>{sale_lbl}<b>Limite: {brdate(r['expected_delivery_date'])}</b><br>{deadline_pill(r['expected_delivery_date'], r['status'])}</td><td><b>{fmt_num(r['weight_kg'])} kg</b>{value_html}</td><td class="row-actions">{next_action}<a class="btn small ghost" href="/orders/{r['id']}">Abrir</a>{edit_link}{delete_form}</td></tr>'''
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
        sync_carga_btn = '<a class="btn ghost" href="/orders/sync-carga-erp" style="color:#166534; border-color:#86efac; background:#f0fdf4; font-weight:700;" title="Sincronizar pedidos com entrega ao cliente diretamente do ERP">🔄 Buscar Pedidos ERP (Caminhão)</a>' if self.has_perm(u,'create_orders') else ''
        view_toggle = "<a class='btn ghost' href='/orders'>Pedidos em andamento</a>" if history_mode else "<a class='btn ghost' href='/orders?history=1'>Histórico de finalizados</a>"
        mode_label = 'Histórico de finalizados' if history_mode else 'Pedidos em andamento'
        filt=f'''<div class="toolbar"><div>{new_order_link}{sync_carga_btn}{view_toggle}<a class="btn ghost" href="/orders?late=1">Atrasados</a><a class="btn ghost" href="/orders?near=1">Próximos do limite</a></div><div class="muted"><b>{mode_label}</b> · <b>{total_rows}</b> pedido(s)</div></div><form class="filters">{history_hidden}<input name="q" placeholder="Busca rápida por pedido, cliente, cidade ou observação" value="{esc(qs.get('q',[''])[0])}"><select name="status">{option(status_options,qs.get('status',[''])[0],True,'Todos status')}</select><select name="city">{option(cities,qs.get('city',[''])[0],True,'Todas cidades')}</select><select name="route">{option(routes,qs.get('route',[''])[0],True,'Todas rotas')}</select><select name="client_id">{client_opts}</select><input name="nf" placeholder="NF" value="{esc(nf)}"><label>Prazo de<input type="date" name="deadline_from" value="{esc(deadline_from)}"></label><label>até<input type="date" name="deadline_to" value="{esc(deadline_to)}"></label><button>Filtrar</button><a class="btn ghost" href="/orders{'?history=1' if history_mode else ''}">Limpar</a></form>'''
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

    def handle_orders_sync_carga_erp(self, u):
        """GET ou POST /orders/sync-carga-erp — sincroniza pedidos da view de carga do ERP."""
        from urllib.parse import parse_qs, urlparse
        res = sync_orders_from_carga_view()
        qs = parse_qs(urlparse(self.path).query)
        redirect_to = qs.get('redirect', ['/orders'])[0]
        with conn() as db:
            audit(db, u, 'Sincronizou pedidos da view de carga ERP', 'ERP', '', '', res.get('message', ''), source_ip=self.client_ip())
            db.commit()
        if 'application/json' in self.headers.get('Accept', '') or 'json' in qs:
            return self.send_json(res)
        return self.redirect(redirect_to)

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
        c_key = normalized_text_key(city_txt)
        rc_rows = db.execute("SELECT route_name, city FROM route_cities WHERE active=1 AND route_name IS NOT NULL AND TRIM(route_name)<>'' ORDER BY COALESCE(delivery_order,9999), route_name, id").fetchall()
        for r in rc_rows:
            if normalized_text_key(r['city']) == c_key:
                return str(r['route_name']).strip()
        for r in rc_rows:
            rck = normalized_text_key(r['city'])
            if rck and (rck in c_key or c_key in rck):
                return str(r['route_name']).strip()
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
            client_row = db.execute("SELECT latitude, longitude, location_link FROM clients WHERE id=?", (cid,)).fetchone() if cid else None
            order_loc_link = (d.get('location_link') or '').strip()
            order_lat, order_lng = extract_lat_lng(order_loc_link) if order_loc_link else (None, None)
            
            if not order_loc_link and client_row and client_row['location_link']:
                order_loc_link = client_row['location_link']
                order_lat = client_row['latitude']
                order_lng = client_row['longitude']
            elif order_loc_link and cid and (order_lat is not None and order_lng is not None):
                db.execute("UPDATE clients SET latitude=?, longitude=?, location_link=?, updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (order_lat, order_lng, order_loc_link, now(), cid))
                
            if order_lat is None and client_row and client_row['latitude']:
                order_lat = client_row['latitude']
                order_lng = client_row['longitude']

            delivered_at = (d.get('delivered_at') or today()) if status in ('Acertado','Problema') else None
            cur=db.execute('''INSERT INTO orders(order_number,client_id,seller_id,seller_name,status,urgency,sale_date,expected_delivery_date,invoice_limit_date,payment_method,total_value,weight_kg,delivery_address,location_link,latitude,longitude,route_name,city,notes,invoice_number,invoiced_at,driver_id,vehicle_id,delivered_at,created_at,updated_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(order_no,cid,u['id'],seller_name,status,'Normal',sale,deadline,'',payload['payment_method'],value,weight,(d.get('delivery_address') or '').strip(),order_loc_link,order_lat,order_lng,resolved_route_name,upper_text(d.get('city')),payload['notes'],invoice_number,invoiced_at_val,d.get('driver_id') or None,d.get('vehicle_id') or None,delivered_at,now(),now(),1))
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
                
            edit_loc_link = (d.get('location_link') or '').strip()
            edit_lat, edit_lng = extract_lat_lng(edit_loc_link) if edit_loc_link else (oldrow['latitude'] if 'latitude' in oldrow.keys() else None, oldrow['longitude'] if 'longitude' in oldrow.keys() else None)
            
            if edit_loc_link and cid and edit_lat is not None and edit_lng is not None:
                db.execute("UPDATE clients SET latitude=?, longitude=?, location_link=?, updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (edit_lat, edit_lng, edit_loc_link, now(), cid))
                
            db.execute('''UPDATE orders SET order_number=?,client_id=?,seller_id=?,seller_name=?,status=?,urgency=?,sale_date=?,expected_delivery_date=?,invoice_limit_date=?,payment_method=?,total_value=?,weight_kg=?,delivery_address=?,location_link=?,latitude=?,longitude=?,route_name=?,city=?,notes=?,invoice_number=?,invoiced_at=?,driver_id=?,vehicle_id=?,delivered_at=?,updated_at=?,version=COALESCE(version,1)+1 WHERE id=?''',(order_no,cid,u['id'],seller_name,status,'Normal',sale,deadline,'',payload['payment_method'],value,weight,(d.get('delivery_address') or '').strip(),edit_loc_link,edit_lat,edit_lng,resolved_route_name,upper_text(d.get('city')),payload['notes'],invoice_number,d.get('invoiced_at'),d.get('driver_id') or None,d.get('vehicle_id') or None,delivered_at,now(),oid))
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
            items=db.execute('SELECT * FROM order_items WHERE order_id=?',(oid,)).fetchall()
            if _ERP_AVAILABLE and (not items or any('Carga/volume' in (it['product_name'] or '') for it in items)):
                if enrich_order_items_from_erp(db, oid, r['order_number']):
                    items=db.execute('SELECT * FROM order_items WHERE order_id=?',(oid,)).fetchall()
            hist=db.execute('SELECT h.*,u.name user FROM order_history h LEFT JOIN users u ON u.id=h.user_id WHERE h.order_id=? ORDER BY h.id ASC',(oid,)).fetchall(); probs=db.execute('SELECT * FROM delivery_problems WHERE order_id=? ORDER BY id DESC',(oid,)).fetchall()
            receipt=db.execute('SELECT * FROM delivery_receipts WHERE order_id=? ORDER BY id DESC LIMIT 1',(oid,)).fetchone()
            route_info=self.order_route_info(db,oid)

        receipt_panel = ''
        r_receipt_photo = r['receipt_photo'] if 'receipt_photo' in r.keys() else None
        r_delivered_to = r['delivered_to'] if 'delivered_to' in r.keys() else None
        r_delivered_doc = r['delivered_document'] if 'delivered_document' in r.keys() else None
        r_delivered_doc_type = r['delivered_document_type'] if 'delivered_document_type' in r.keys() else None
        r_delivered_at = r['delivered_at'] if 'delivered_at' in r.keys() else None
        r_final_notes = r['final_notes'] if 'final_notes' in r.keys() else None

        loc_url, is_gps, loc_label = order_map_info(r, receipt)
        lat = receipt['latitude'] if (receipt and 'latitude' in receipt.keys() and receipt['latitude']) else (r['delivery_latitude'] if 'delivery_latitude' in r.keys() else None)
        lng = receipt['longitude'] if (receipt and 'longitude' in receipt.keys() and receipt['longitude']) else (r['delivery_longitude'] if 'delivery_longitude' in r.keys() else None)

        has_receipt_photo = bool(receipt and receipt['image_data']) or bool(r_receipt_photo)
        has_signature = bool(receipt and receipt['digital_signature'])
        if has_receipt_photo or has_signature or r_delivered_to or loc_url:
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

            if loc_url:
                if is_gps and lat is not None and lng is not None:
                    btn_text = f"📍 Ver Localização Exata no Google Maps ({lat:.4f}, {lng:.4f})"
                elif is_gps:
                    btn_text = "📍 Ver Localização Exata no Google Maps (GPS)"
                else:
                    btn_text = f"📍 Ver Localização no Google Maps ({loc_label})"
                gps_html = f'''<p><b>Localização no Mapa</b><a href="{esc(loc_url)}" target="_blank" rel="noopener" class="btn small primary" style="background:#0284c7; border-color:#0284c7; font-weight:700; display:inline-flex; align-items:center; gap:6px; color:#ffffff; padding:6px 12px; border-radius:6px; margin-top:4px;">{esc(btn_text)}</a></p>'''
            else:
                gps_html = '<p><b>Localização no Mapa</b><span class="muted">Não capturada</span></p>'

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

        # Análise de Lead Time & Gargalo do Pedido
        d_sale = None
        d_inv = None
        d_del = None
        try:
            if r['sale_date']:
                d_sale = datetime.strptime(str(r['sale_date'])[:10], '%Y-%m-%d').date()
        except Exception:
            pass
        try:
            if r['invoiced_at']:
                d_inv = datetime.strptime(str(r['invoiced_at'])[:10], '%Y-%m-%d').date()
        except Exception:
            pass
        try:
            if r['delivered_at']:
                d_del = datetime.strptime(str(r['delivered_at'])[:10], '%Y-%m-%d').date()
        except Exception:
            pass

        # Fallback de faturamento para pedidos já faturados sem invoiced_at preenchido
        if not d_inv and normalize_order_status(r['status']) in ('Faturado', 'Saiu para entrega', 'Acertado'):
            try:
                if r['updated_at']:
                    d_inv = datetime.strptime(str(r['updated_at'])[:10], '%Y-%m-%d').date()
                elif d_sale:
                    d_inv = d_sale
            except Exception:
                d_inv = d_sale

        days_s2i = (d_inv - d_sale).days if (d_sale and d_inv) else None
        days_i2d = (d_del - d_inv).days if (d_inv and d_del) else None
        days_tot = (d_del - d_sale).days if (d_sale and d_del) else None

        today_dt = date.today()
        if d_del:
            if days_s2i is not None and days_i2d is not None:
                if days_s2i > days_i2d:
                    bot_stage = "Comercial"
                    bot_desc = f"Faturamento demorou mais ({days_s2i}d no faturamento vs {days_i2d}d na entrega)"
                    bot_color = "#2563eb"
                elif days_i2d > days_s2i:
                    bot_stage = "Logística"
                    bot_desc = f"Expedição/entrega demorou mais ({days_i2d}d na logística vs {days_s2i}d no faturamento)"
                    bot_color = "#d97706"
                else:
                    bot_stage = "Equilibrado"
                    bot_desc = f"Tempos iguais nas duas etapas ({days_s2i}d em cada)"
                    bot_color = "#15803d"
            elif days_s2i is not None:
                bot_stage = "Comercial"
                bot_desc = f"Venda faturada em {days_s2i} dia(s)"
                bot_color = "#2563eb"
            elif days_i2d is not None:
                bot_stage = "Logística"
                bot_desc = f"Entregue em {days_i2d} dia(s) após faturar"
                bot_color = "#d97706"
            else:
                bot_stage = "Concluído"
                bot_desc = "Pedido entregue e concluído"
                bot_color = "#15803d"
        elif normalize_order_status(r['status']) == 'Cancelado':
            bot_stage = "Cancelado"
            bot_desc = "Pedido cancelado"
            bot_color = "#64748b"
        elif normalize_order_status(r['status']) == 'Venda' or not d_inv:
            wait_days = (today_dt - d_sale).days if d_sale else 0
            bot_stage = "Comercial"
            bot_desc = f"Aguardando faturamento há {wait_days} dia(s)"
            bot_color = "#b91c1c" if wait_days > 2 else "#2563eb"
        else:
            wait_days = (today_dt - d_inv).days
            bot_stage = "Logística"
            bot_desc = f"Aguardando carga/entrega há {wait_days} dia(s) após faturamento"
            bot_color = "#b91c1c" if wait_days > 3 else "#d97706"

        fat_label = brdate(r['invoiced_at']) if r['invoiced_at'] else ('Concluído' if normalize_order_status(r['status']) in ('Faturado', 'Saiu para entrega', 'Acertado') else 'Pendente')

        lead_time_panel = f'''<section class="panel" style="border-left:5px solid {bot_color};">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
                <h2 style="margin:0; font-size:1.05rem; color:#1e293b; display:flex; align-items:center; gap:8px;">
                    <span>⏱️</span> Análise de Lead Time &amp; Gargalo
                </h2>
                <span class="badge" style="background:{bot_color}; color:#fff; font-weight:700; font-size:0.85rem; padding:4px 10px; border-radius:12px;">
                    Gargalo: {esc(bot_stage)}
                </span>
            </div>
            <p style="margin:6px 0 12px 0; color:#475569; font-size:0.88rem;">{esc(bot_desc)}</p>
            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px;">
                <div style="background:#f1f5f9; padding:10px 14px; border-radius:8px; border:1px solid #e2e8f0;">
                    <div style="font-size:0.75rem; text-transform:uppercase; color:#64748b; font-weight:700;">1. Comercial (Venda ➔ Fat.)</div>
                    <div style="font-size:1.25rem; font-weight:800; color:#1e293b; margin-top:2px;">
                        {f"{days_s2i} dia(s)" if days_s2i is not None else ("Aguardando..." if normalize_order_status(r['status']) == 'Venda' else "0 dia(s)")}
                    </div>
                    <div style="font-size:0.75rem; color:#64748b; margin-top:2px;">Venda: {brdate(r['sale_date']) or '—'} ➔ Fat: {fat_label}</div>
                </div>
                <div style="background:#f1f5f9; padding:10px 14px; border-radius:8px; border:1px solid #e2e8f0;">
                    <div style="font-size:0.75rem; text-transform:uppercase; color:#64748b; font-weight:700;">2. Logística (Fat. ➔ Entrega)</div>
                    <div style="font-size:1.25rem; font-weight:800; color:#1e293b; margin-top:2px;">
                        {f"{days_i2d} dia(s)" if days_i2d is not None else ("Em andamento..." if d_inv and not d_del else "—")}
                    </div>
                    <div style="font-size:0.75rem; color:#64748b; margin-top:2px;">Fat: {brdate(r['invoiced_at']) or '—'} ➔ Entr: {brdate(r['delivered_at']) or 'Pendente'}</div>
                </div>
                <div style="background:#f1f5f9; padding:10px 14px; border-radius:8px; border:1px solid #e2e8f0;">
                    <div style="font-size:0.75rem; text-transform:uppercase; color:#64748b; font-weight:700;">Ciclo Total (Venda ➔ Entrega)</div>
                    <div style="font-size:1.25rem; font-weight:800; color:{'#15803d' if days_tot is not None else '#1e293b'}; margin-top:2px;">
                        {f"{days_tot} dia(s)" if days_tot is not None else "Em andamento"}
                    </div>
                    <div style="font-size:0.75rem; color:#64748b; margin-top:2px;">Limite acordado: {brdate(r['expected_delivery_date']) or '—'}</div>
                </div>
            </div>
        </section>'''

        # --- Linha do tempo visual usando order_history real ---
        flow_steps = [
            ('Venda',           '📝', 'Pedido criado'),
            ('Faturado',        '🧾', 'Faturamento'),
            ('Carga Montada',   '📦', 'Carga montada (Aguardando saída)'),
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
        flow_order = ['Venda','Faturado','Carga Montada','Saiu para entrega','Acertado']
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
        if is_admin(u) or can_manage_catalog_deletions(u):
            delete_action = f"""<form method='post' action='/orders/{oid}/delete' class='inline-form needs-confirm' data-confirm-text='⚠️ ATENÇÃO: Confirma apagar definitivamente este pedido #{esc(r["order_number"])} e remover todos os seus vínculos de faturamento e carga?'>
                <button class='danger-btn' style='background:#dc2626;border-color:#b91c1c;font-weight:800;'>⚡ Excluir Definitivamente (Controle Total)</button>
            </form>"""
        else:
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

        # Badge e campos detalhados de Nota Fiscal
        has_nf = bool(r['invoice_number'] and str(r['invoice_number']).strip() not in ('', '0', '0.0', 'None', 'null'))
        if has_nf:
            nf_hero_badge = f'''<span class="badge" style="background:#047857; color:#fff; font-size:0.95rem; font-weight:800; padding:6px 14px; border-radius:8px; display:inline-flex; align-items:center; gap:6px; box-shadow:0 1px 2px rgba(0,0,0,0.12);">🧾 Nota Fiscal: Nº {esc(r['invoice_number'])}</span>'''
            nf_field_html = f'''<p><b>Nota Fiscal (NF)</b><strong style="color:#047857; font-size:1.1rem; font-weight:800;">🧾 Nº {esc(r['invoice_number'])}</strong></p>'''
            fat_date_html = f'''<p><b>Data Faturamento</b><strong style="color:#1e293b;">{brdate(r['invoiced_at']) if r['invoiced_at'] else 'Confirmado no ERP'}</strong></p>'''
        else:
            is_fat_status = normalize_order_status(r['status']) in ('Faturado', 'Saiu para entrega', 'Acertado')
            if is_fat_status:
                nf_hero_badge = f'''<span class="badge" style="background:#fef3c7; color:#92400e; border:1px solid #fde68a; font-size:0.85rem; font-weight:700; padding:5px 12px; border-radius:8px;">⏳ Faturado (NF Pendente de sincronização)</span>'''
                nf_field_html = f'''<p><b>Nota Fiscal (NF)</b><span style="color:#b45309; font-weight:700;">Aguardando retorno do número de NF do ERP</span></p>'''
            else:
                nf_hero_badge = f'''<span class="badge" style="background:#f1f5f9; color:#475569; border:1px solid #cbd5e1; font-size:0.85rem; font-weight:700; padding:5px 12px; border-radius:8px;">📝 Pedido em Venda (Não Faturado)</span>'''
                nf_field_html = f'''<p><b>Nota Fiscal (NF)</b><span class="muted">Pendente (Aguardando faturamento no ERP)</span></p>'''
            fat_date_html = f'''<p><b>Data Faturamento</b><span class="muted">—</span></p>'''

        content=f'''<section class="order-hero"><div><span>{esc(r['order_number'])}</span><h2>{esc(r['client'] or 'Cliente não informado')}</h2><p>{esc(r['farm_name'] or r['delivery_address'] or '')}</p></div><div style="display:flex; flex-direction:column; align-items:flex-end; gap:6px;"><div>{badge(r['status'])}{deadline_pill(r['expected_delivery_date'],r['status'])}</div><div>{nf_hero_badge}</div></div></section>{assisted}{actions}{receipt_panel}{lead_time_panel}
        <section class="panel"><h2>Linha do Tempo</h2><div class="order-timeline">{timeline_steps}</div></section>
        <div class="detail-grid"><section class="panel"><h2>Dados do pedido</h2><div class="info-grid">{nf_field_html}{fat_date_html}<p><b>Venda</b>{brdate(r['sale_date'])}</p><p><b>Prazo limite</b>{brdate(r['expected_delivery_date'])}</p><p><b>Peso</b>{fmt_num(r['weight_kg'])} kg</p>{value_info}<p><b>Vendedor</b>{esc(resolve_seller_name(r['seller_name']))}</p><p><b>Cadastrante</b>{esc(r['seller'] or '—')}</p></div></section><section class="panel"><h2>Entrega</h2><div class="info-grid"><p><b>Cidade</b>{esc(r['city'] or r['client_city'] or '—')}</p><p><b>Rota</b>{esc(r['route_name'] or '—')}</p><p><b>Motorista</b>{esc(r['driver'] or '—')}</p><p><b>Veículo</b>{esc((r['vehicle'] or '')+' '+(r['plate'] or ''))}</p><p><b>Recebido por</b>{esc(r['delivered_to'] or '—')}</p><p><b>Entrega em</b>{brdate(r['delivered_at'])}</p></div></section></div><section class="panel"><h2>Itens</h2><table><thead><tr><th>Código</th><th>Produto</th><th>Qtd</th><th>Peso</th><th>Obs.</th></tr></thead><tbody>{item_rows}</tbody></table></section><div class="detail-grid"><section class="panel"><h2>Histórico completo</h2><ul class="timeline-list">{hist_rows}</ul></section><section class="panel"><h2>Problemas registrados</h2><ul class="problems">{prob_rows}</ul></section></div>{problem_form}{schedule_form}{reopen_form}'''
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
            elif new in ('Venda','Faturado','Carga Montada','Agendado'):
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
        if not (is_admin(u) or can_manage_catalog_deletions(u) or self.has_perm(u, 'cancel_orders')):
            return self.fail(u, 'Acesso negado', 'Seu usuário não tem permissão para apagar pedidos.', 403)
        with conn() as db:
            row=db.execute('SELECT id,order_number,status FROM orders WHERE id=?',(oid,)).fetchone()
            if not row:
                return self.fail(u,'Não encontrado','Pedido não encontrado.',404)
            order_status=normalize_order_status(row['status'])

            # Controle Total: GOD, Admin e usuários com permissão total de gerenciamento
            has_total_control = bool(is_admin(u) or can_manage_catalog_deletions(u) or is_god(u))

            if not has_total_control:
                if order_status not in ('Venda','Cancelado'):
                    return self.fail(
                        u,
                        'Exclusão bloqueada',
                        f'Pedido em status "{order_status}" não pode ser apagado por seu perfil. Cancele o pedido antes de apagar ou solicite a um administrador.',
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
                        'Este pedido está vinculado a uma carga ativa (Planejada/Em rota) e não pode ser apagado.',
                        400,
                    )

            # Limpeza completa em cascata de todas as tabelas vinculadas antes de excluir o pedido
            db.execute('DELETE FROM driver_delivery_operations WHERE order_id=?', (oid,))
            db.execute('DELETE FROM route_orders WHERE order_id=?', (oid,))
            db.execute('DELETE FROM delivery_receipts WHERE order_id=?', (oid,))
            db.execute('DELETE FROM delivery_problems WHERE order_id=?', (oid,))
            db.execute('DELETE FROM attachments WHERE order_id=?', (oid,))
            db.execute('DELETE FROM order_items WHERE order_id=?', (oid,))
            db.execute('DELETE FROM order_history WHERE order_id=?', (oid,))
            db.execute('DELETE FROM orders WHERE id=?', (oid,))
            db.execute('INSERT OR REPLACE INTO deleted_orders (order_number, deleted_at, deleted_by, reason) VALUES (?, ?, ?, ?)', (row['order_number'], now(), u['id'], 'Exclusão pelo usuário'))

            audit_detail = 'Exclusão com controle total (GOD/Admin Override)' if has_total_control else 'Exclusão padrão'
            audit(db, u, 'Apagou pedido', 'Pedidos', row['order_number'], order_status, audit_detail)
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
            target_order_status = 'Saiu para entrega' if new_route == 'Em rota' else 'Carga Montada'
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

            # GOD/Admin OVERRIDE: GOD e Admin podem apagar qualquer carga em qualquer status
            if is_god(u) or is_admin(u) or can_manage_catalog_deletions(u):
                ros=db.execute("SELECT ro.order_id, o.status FROM route_orders ro JOIN orders o ON o.id=ro.order_id WHERE ro.route_id=?", (rid,)).fetchall()
                for ro in ros:
                    old_order=normalize_order_status(ro['status'])
                    if old_order in ('Saiu para entrega', 'Carga Montada'):
                        self.ensure_order_status(db, ro['order_id'], 'Faturado')
                        db.execute("UPDATE orders SET status='Faturado',updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",(now(),ro['order_id']))
                        add_hist(db,ro['order_id'],u['id'],old_order,'Faturado','Carga apagada: pedido retornou para faturado',route['name'])
                db.execute("DELETE FROM driver_delivery_operations WHERE route_id=?", (rid,))
                db.execute("DELETE FROM route_orders WHERE route_id=?", (rid,))
                db.execute("DELETE FROM delivery_receipts WHERE route_id=?", (rid,))
                db.execute("DELETE FROM delivery_problems WHERE route_id=?", (rid,))
                db.execute("DELETE FROM routes WHERE id=?", (rid,))
                audit(db, u, 'Apagou carga (Controle Total)', 'Rotas', route['route_name'] or f"#{rid}", route_status, 'Exclusão forçada com controle total')
                db.commit()
                return self.redirect('/routes')

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
                if old_order in ('Saiu para entrega', 'Carga Montada'):
                    self.ensure_order_status(db,ro['order_id'],'Faturado')
                    db.execute("UPDATE orders SET status='Faturado',updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",(now(),ro['order_id']))
                    add_hist(db,ro['order_id'],u['id'],old_order,'Faturado','Carga apagada: pedido retornou para faturado',route['name'])
            db.execute("DELETE FROM driver_delivery_operations WHERE route_id=?", (rid,))
            db.execute("DELETE FROM route_orders WHERE route_id=?", (rid,))
            db.execute("DELETE FROM delivery_receipts WHERE route_id=?", (rid,))
            db.execute("DELETE FROM delivery_problems WHERE route_id=?", (rid,))
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
                    <div class="invoice-client-block"><span class="label">Cliente</span><b>{esc(r['client'] or 'Cliente não informado')}</b><small>Vendedor: {esc(resolve_seller_name(r['seller_name']))} · {esc(r['city'] or r['client_city'] or 'Cidade não informada')} · {fmt_num(r['weight_kg'])} kg</small></div>
                    <div class="invoice-sla-block"><span class="label">Venda / SLA</span><small class="muted">Venda: {brdate(r['sale_date'])}</small><small>Prazo limite: {brdate(r['expected_delivery_date'])}</small>{deadline_pill(r['expected_delivery_date'],r['status'])}</div>
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
            rows=db.execute("""SELECT o.*,c.name client,c.farm_name,c.city client_city,
                                      r.id route_id, r.name route_name_load
                               FROM orders o
                               LEFT JOIN clients c ON c.id=o.client_id
                               LEFT JOIN route_orders ro ON ro.order_id=o.id
                               LEFT JOIN routes r ON r.id=ro.route_id AND r.status IN ('Planejada','Em rota')
                               WHERE o.status IN ('Faturado', 'Carga Montada')
                               ORDER BY o.expected_delivery_date,o.id DESC""").fetchall()
        lanes=[]
        configs = [
            ('Faturado', 'Aguardando montagem de carga', '/routes/new', 'Montar Carga'),
            ('Carga Montada', 'Carga montada (Aguardando saída)', '/routes', 'Ver Carga'),
        ]
        for st, lane_title, default_url, default_btn_text in configs:
            lane_cards = []
            for r in rows:
                if r['status'] == st:
                    card_url = f"/routes/{r['route_id']}" if (st == 'Carga Montada' and r['route_id']) else default_url
                    card_btn_text = f"Carga #{r['route_id']}" if (st == 'Carga Montada' and r['route_id']) else default_btn_text
                    load_info = f"<br><small style='color:#6b21a8; font-weight:600;'>📦 {esc(r['route_name_load'] or f'Carga #{r['route_id']}')}</small>" if (st == 'Carga Montada' and r['route_id']) else ""
                    lane_cards.append(f'''<article class="kanban-card"><div>{badge(r['status'])}</div><h3>{esc(r['order_number'])}</h3><p>{esc(r['client'] or '')}{load_info}<br><small>Vendedor: {esc(resolve_seller_name(r['seller_name']))} · Venda: {brdate(r['sale_date'])} · Limite: {brdate(r['expected_delivery_date'])} · {esc(r['city'] or r['client_city'] or '')} · {fmt_num(r['weight_kg'])} kg · {deadline_pill(r['expected_delivery_date'],r['status'])}</small></p><div class="row-actions"><a class="btn small ghost" href="/orders/{r['id']}">Abrir</a><a class="btn small" href="{card_url}">{card_btn_text}</a></div></article>''')
            lanes.append(f'<section class="kanban-lane"><h2>{esc(lane_title)} ({len(lane_cards)})</h2>{"".join(lane_cards) or "<div class=empty>Fila vazia</div>"}</section>')
        return self.send_html(layout(u,'Expedição / Separação','<div class="kanban">'+''.join(lanes)+'</div>','Painel visual de faturados e cargas aguardando saída'))

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
        q=qs.get('q',[''])[0].strip()
        hist_page=max(1,parse_int(qs.get('hist_page',['1'])[0],1))
        hist_size=12
        hist_offset=(hist_page-1)*hist_size
        with conn() as db:
            self.recalc_all_routes(db)
            regularized=self.ensure_in_route_loads(db)
            self.recalc_all_routes(db)
            
            # Filtro por busca unificada 'q' se preenchido
            q_like = f"%{q}%" if q else None
            
            if q:
                active_routes=db.execute("""
                    SELECT r.*,d.name driver,v.name vehicle,v.plate,COUNT(ro.id) orders_count 
                    FROM routes r 
                    LEFT JOIN drivers d ON d.id=r.driver_id 
                    LEFT JOIN vehicles v ON v.id=r.vehicle_id 
                    LEFT JOIN route_orders ro ON ro.route_id=r.id 
                    WHERE r.status IN ('Planejada','Em rota') 
                      AND (r.name LIKE ? OR r.route_name LIKE ? OR d.name LIKE ? OR v.plate LIKE ? OR CAST(r.id AS TEXT)=?)
                    GROUP BY r.id 
                    ORDER BY CASE r.status WHEN 'Planejada' THEN 0 WHEN 'Em rota' THEN 1 ELSE 2 END,r.date DESC,r.id DESC
                """,(q_like, q_like, q_like, q_like, q)).fetchall()
                
                closed_routes=db.execute(f"""
                    SELECT r.*,d.name driver,v.name vehicle,v.plate,COUNT(ro.id) orders_count 
                    FROM routes r 
                    LEFT JOIN drivers d ON d.id=r.driver_id 
                    LEFT JOIN vehicles v ON v.id=r.vehicle_id 
                    LEFT JOIN route_orders ro ON ro.route_id=r.id 
                    WHERE r.status IN ('Acertada','Com problema')
                      AND (r.name LIKE ? OR r.route_name LIKE ? OR d.name LIKE ? OR v.plate LIKE ? OR CAST(r.id AS TEXT)=?)
                    GROUP BY r.id 
                    ORDER BY r.date DESC,r.id DESC LIMIT {hist_size} OFFSET {hist_offset}
                """,(q_like, q_like, q_like, q_like, q)).fetchall()
                
                closed_count=db.execute("""
                    SELECT COUNT(DISTINCT r.id) c 
                    FROM routes r 
                    LEFT JOIN drivers d ON d.id=r.driver_id 
                    LEFT JOIN vehicles v ON v.id=r.vehicle_id 
                    WHERE r.status IN ('Acertada','Com problema')
                      AND (r.name LIKE ? OR r.route_name LIKE ? OR d.name LIKE ? OR v.plate LIKE ? OR CAST(r.id AS TEXT)=?)
                """,(q_like, q_like, q_like, q_like, q)).fetchone()['c']
            else:
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
            
            if q:
                available_ready=db.execute("""
                    SELECT o.id,o.order_number,o.invoice_number,o.seller_name,o.sale_date,o.expected_delivery_date,o.weight_kg,o.city,o.route_name,c.name client
                    FROM orders o
                    LEFT JOIN clients c ON c.id=o.client_id
                    WHERE o.status='Faturado'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM route_orders ro
                        JOIN routes ar ON ar.id=ro.route_id
                        WHERE ro.order_id=o.id AND ar.status IN ('Planejada','Em rota')
                    )
                    AND (o.order_number LIKE ? OR c.name LIKE ? OR o.city LIKE ? OR o.route_name LIKE ?)
                    ORDER BY CASE WHEN o.expected_delivery_date<? THEN 0 ELSE 1 END,o.expected_delivery_date,o.id DESC
                    LIMIT 40
                """,(q_like, q_like, q_like, q_like, today())).fetchall()
            else:
                available_ready=db.execute("""SELECT o.id,o.order_number,o.invoice_number,o.seller_name,o.sale_date,o.expected_delivery_date,o.weight_kg,o.city,o.route_name,c.name client
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

        can_edit_routes = self.has_perm(u,'edit_routes')
        can_cancel_routes = self.has_perm(u,'cancel_routes')
        can_settle_routes = self.has_perm(u,'settle_routes')
        can_create_routes = self.has_perm(u,'create_routes')

        def render_route_table(r_list, is_closed=False):
            if not r_list:
                return f"<div class='empty'>Nenhuma carga {'finalizada' if is_closed else 'nesta etapa'}.</div>"
            trs = []
            for r in r_list:
                pct = (float(r['total_weight'] or 0)/float(r['capacity'] or 1))*100
                cap_text = f"{fmt_num(r['total_weight'])} / {fmt_num(r['capacity'])} kg ({pct:.0f}%)"
                cap_badge = f"<span class='badge {'danger' if pct>100 else 'ok'}' style='font-size:0.8rem;'>{cap_text}</span>"
                
                actions = f"<a class='btn small ghost' href='/routes/{r['id']}'>Abrir</a>"
                if not is_closed and r['status']=='Planejada' and can_edit_routes:
                    actions += f" <form method='post' action='/routes/{r['id']}/dispatch' style='display:inline;'><button class='btn small primary'>Marcar saída</button></form>"
                if not is_closed and r['status']=='Em rota' and can_settle_routes:
                    actions += f" <a class='btn small' style='background:#15803d;color:#fff;' href='/load-settlement?q={quote(r['name'])}'>Acerto</a>"
                if not is_closed and r['status'] in ('Planejada','Em rota') and can_cancel_routes:
                    actions += f" <form method='post' action='/routes/{r['id']}/cancel' class='inline-mini needs-confirm' data-confirm-text='Confirma cancelar esta carga?' style='display:inline;'><input name='reason' placeholder='Motivo' required style='width:90px;'><button class='danger-btn small'>Cancelar</button></form>"
                if can_cancel_routes:
                    actions += f" <form method='post' action='/routes/{r['id']}/delete' class='inline-mini needs-confirm' data-confirm-text='Confirma apagar esta carga definitivamente?' style='display:inline;'><button class='danger-btn small'>Apagar</button></form>"
                
                trs.append(
                    f"<tr>"
                    f"<td><b>{esc(r['name'])}</b><br><small class='muted'>Carga #{r['id']}</small></td>"
                    f"<td>{brdate(r['date'])}</td>"
                    f"<td><b>{esc(r['route_name'] or 'Rota livre')}</b></td>"
                    f"<td>{esc(r['driver'] or 'Definir motorista')}</td>"
                    f"<td>{esc((r['vehicle'] or 'Definir veículo')+' '+(r['plate'] or ''))}</td>"
                    f"<td><span class='badge neutral'>{r['orders_count']} pedidos</span></td>"
                    f"<td>{cap_badge}</td>"
                    f"<td>{badge(r['status'])}</td>"
                    f"<td style='white-space:nowrap;'>{actions}</td>"
                    f"</tr>"
                )
            return f"<div class='table-wrap'><table><thead><tr><th>Carga</th><th>Data</th><th>Rota</th><th>Motorista</th><th>Veículo</th><th>Qtd Pedidos</th><th>Peso / Capacidade</th><th>Status</th><th>Ações</th></tr></thead><tbody>{''.join(trs)}</tbody></table></div>"

        planejadas = [r for r in active_routes if r['status'] == 'Planejada']
        em_rota = [r for r in active_routes if r['status'] == 'Em rota']

        regularized_notice = f'<div class="alert info">{regularized} carga(s) em rota foram regularizadas automaticamente porque havia pedidos com status <b>Saiu para entrega</b> sem vínculo de carga.</div>' if regularized else ''
        ready_tr_list = []
        for o in available_ready:
            o_sale_dt = o['sale_date'] if ('sale_date' in o.keys() and o['sale_date']) else ''
            o_sale = f"<small class='muted'>Venda: {brdate(o_sale_dt)}</small><br>" if o_sale_dt else ""
            o_nf_pill = f"<span class='badge' style='background:#047857; color:#fff; font-size:0.75rem; font-weight:700; padding:1px 5px; border-radius:4px; margin-left:4px;'>🧾 NF {esc(o['invoice_number'])}</span>" if o['invoice_number'] else ""
            ready_tr_list.append(
                f"<tr><td><b>{esc(o['order_number'])}</b>{o_nf_pill}<br><small class='muted'>Vendedor: {esc(resolve_seller_name(o['seller_name']))}</small></td>"
                f"<td>{esc(o['client'] or 'Cliente não informado')}</td>"
                f"<td>{esc(o['city'] or 'Sem cidade')} · <b>{esc(o['route_name'] or 'Sem rota')}</b></td>"
                f"<td>{fmt_num(o['weight_kg'])} kg</td>"
                f"<td>{o_sale}<b>Limite: {brdate(o['expected_delivery_date'])}</b><br>{deadline_pill(o['expected_delivery_date'],'Faturado')}</td>"
                f"<td><a class='btn small ghost' href='/orders/{o['id']}'>Abrir</a></td></tr>"
            )
        ready_rows = ''.join(ready_tr_list) or "<tr><td colspan='6' class='empty'>Nenhum pedido disponível para montar carga.</td></tr>"
        hist_prev=f"<a class='btn small ghost' href='/routes?hist_page={hist_page-1}&q={quote(q)}'>Página anterior</a>" if hist_page>1 else "<span class='muted'>Página anterior</span>"
        has_next=(hist_page*hist_size)<closed_count
        hist_next=f"<a class='btn small ghost' href='/routes?hist_page={hist_page+1}&q={quote(q)}'>Próxima página</a>" if has_next else "<span class='muted'>Fim do histórico</span>"
        create_route_btn = f"<a class='btn primary' href='/routes/new' style='font-weight:700;'>＋ Criar Nova Carga</a>" if can_create_routes else ""

        # Ribbon horizontal de métricas super compacto (altura reduzida)
        metrics_ribbon = f"""
        <div class='metrics-ribbon' style='display:flex; gap:12px; align-items:center; background:#ffffff; border:1px solid var(--border); border-radius:var(--radius-sm); padding:10px 16px; margin-bottom:14px; box-shadow:var(--shadow-sm); flex-wrap:wrap;'>
          <div style='display:flex; align-items:center; gap:8px;'>
            <span style='color:var(--muted); font-size:0.85rem; font-weight:600;'>Faturados sem carga:</span>
            <strong style='font-size:1.1rem; color:var(--text);'>{ready['c']}</strong>
            <span style='font-size:0.85rem; color:var(--muted);'>({fmt_num(ready['w'])} kg aguardando)</span>
          </div>
          <div style='width:1px; height:20px; background:var(--border);'></div>
          <div style='display:flex; align-items:center; gap:8px;'>
            <span style='color:var(--muted); font-size:0.85rem; font-weight:600;'>Cargas em rota:</span>
            <strong style='font-size:1.1rem; color:#166534;'>{inroute['c']}</strong>
            <span style='font-size:0.85rem; color:var(--muted);'>(Em trânsito)</span>
          </div>
          <div style='width:1px; height:20px; background:var(--border);'></div>
          <div style='display:flex; align-items:center; gap:8px;'>
            <span style='color:{'#dc2626' if late_ready['c']>0 else 'var(--muted)'}; font-size:0.85rem; font-weight:600;'>Faturados atrasados:</span>
            <strong style='font-size:1.1rem; color:{'#dc2626' if late_ready['c']>0 else 'var(--text)'};'>{late_ready['c']}</strong>
            <span style='font-size:0.85rem; color:var(--muted);'>(Prioridade na carga)</span>
          </div>
        </div>
        """

        content=f"""
        {regularized_notice}
        {metrics_ribbon}
        <div class='route-stack'>
          <section class='route-section' style='background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:16px; margin-bottom:16px;'>
            <div class='section-title' style='display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;'>
              <div>
                <h2 style='font-size:1.05rem; margin:0;'>1) Pedidos disponíveis para montar carga</h2>
                <small class='muted'>Pedidos faturados aguardando inclusão em carga.</small>
              </div>
              <span class='badge blue'>{len(available_ready)} pedidos</span>
            </div>
            <div class='table-wrap'>
              <table>
                <thead><tr><th>Pedido</th><th>Cliente</th><th>Cidade / Rota</th><th>Peso</th><th>Venda / Prazo</th><th>Ação</th></tr></thead>
                <tbody>{ready_rows}</tbody>
              </table>
            </div>
          </section>

          <section class='route-section' style='background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:16px; margin-bottom:16px;'>
            <div class='section-title' style='display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;'>
              <div>
                <h2 style='font-size:1.05rem; margin:0;'>2) Cargas planejadas</h2>
                <small class='muted'>Cargas montadas aguardando saída para entrega.</small>
              </div>
              <span class='badge blue'>{len(planejadas)} planejadas</span>
            </div>
            {render_route_table(planejadas, is_closed=False)}
          </section>

          <section class='route-section' style='background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:16px; margin-bottom:16px;'>
            <div class='section-title' style='display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;'>
              <div>
                <h2 style='font-size:1.05rem; margin:0;'>3) Cargas em rota</h2>
                <small class='muted'>Cargas em trânsito com motoristas.</small>
              </div>
              <span class='badge ok'>{len(em_rota)} em rota</span>
            </div>
            {render_route_table(em_rota, is_closed=False)}
          </section>

          <details class='route-section route-archive' style='background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:16px;'>
            <summary style='cursor:pointer; display:flex; justify-content:space-between; align-items:center;'>
              <div>
                <h2 style='font-size:1.05rem; margin:0; display:inline-block;'>4) Histórico de cargas finalizadas</h2>
                <p class='muted' style='margin:2px 0 0;'>Cargas acertadas e finalizadas.</p>
              </div>
              <span class='badge neutral'>{closed_count} no histórico</span>
            </summary>
            <div style='margin-top:14px;'>
              {render_route_table(closed_routes, is_closed=True)}
            </div>
          </details>
        </div>
        """
        return self.send_html(layout(u,'Cargas e Rotas',content,'Central logística para montar cargas, controlar capacidade e baixar entregas',search_action='/routes',search_placeholder='Buscar cargas por código, motorista, veículo, rota...',search_q=q,head_actions_extra=create_route_btn))

    def registro_viagens(self, u):
        from app_core.domains.trip_registry_dispatch import render_registro_viagens_page
        render_registro_viagens_page(self, u, layout_func=layout)

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
                WHERE o.status IN ('Faturado','Carga Montada','Saiu para entrega')
                GROUP BY o.id
                ORDER BY CASE WHEN o.expected_delivery_date<? THEN 0 ELSE 1 END,o.expected_delivery_date,o.route_name,o.city
            """,(today(),)).fetchall()
            route_names=[r['route_name'] for r in db.execute('SELECT DISTINCT route_name FROM orders WHERE route_name IS NOT NULL AND route_name<>"" ORDER BY route_name')]
            if 'Mista' not in route_names:
                route_names.insert(0, 'Mista')
        vehicle_options=''.join(f"<option value='{v['id']}' data-capacity='{esc(v['capacity_kg'] if v['capacity_kg'] is not None else (v['capacity'] or 0))}'>{esc(v['name'])} - {esc(v['plate'] or 'sem placa')} - {fmt_num(v['capacity_kg'] if v['capacity_kg'] is not None else v['capacity'])}kg</option>" for v in vehicles)
        
        order_ids = [o['id'] for o in orders]
        items_by_order = {}
        if order_ids:
            with conn() as db:
                placeholders = ','.join('?' for _ in order_ids)
                all_its = db.execute(f"SELECT order_id, product_name, quantity, unit, weight_kg FROM order_items WHERE order_id IN ({placeholders}) ORDER BY id", order_ids).fetchall()
                for it in all_its:
                    items_by_order.setdefault(it['order_id'], []).append(it)

        checks=''
        for o in orders:
            move = f"<em class='load-current'>Já está em {esc(o['current_load'])}. Ao selecionar, será transferido para esta nova carga.</em>" if o['current_load'] else ''
            route_value = str(o['route_name'] or '')
            o_items = items_by_order.get(o['id'], [])
            items_details = ''
            if o_items:
                it_rows = ''.join(
                    f"<tr><td style='padding:2px 4px;'>{esc(it['product_name'])}</td><td style='padding:2px 4px;text-align:right;'>{to_float(it['quantity']):.0f} {esc(it['unit'] or '')}</td><td style='padding:2px 4px;text-align:right;'>{fmt_num(it['weight_kg'])} kg</td></tr>"
                    for it in o_items
                )
                items_details = f"""<details class='order-items-collapse' style='margin-top:4px; font-size:0.8rem; background:rgba(0,0,0,0.02); border:1px solid #e2e8f0; border-radius:5px; padding:3px 6px;' onclick='event.stopPropagation();'>
                    <summary style='cursor:pointer; color:#166534; font-weight:600;'>📦 Ver produtos ({len(o_items)} itens) ▾</summary>
                    <table style='width:100%; margin-top:4px; font-size:0.75rem; border-collapse:collapse;'>
                        <thead><tr style='border-bottom:1px solid #cbd5e1; color:#64748b;'><th style='text-align:left;'>Produto</th><th style='text-align:right;'>Qtd</th><th style='text-align:right;'>Peso</th></tr></thead>
                        <tbody>{it_rows}</tbody>
                    </table>
                </details>"""

            o_sale_dt = o['sale_date'] if ('sale_date' in o.keys() and o['sale_date']) else ''
            sale_str = f"Venda: {brdate(o_sale_dt)} · " if o_sale_dt else ""
            o_exp_dt = o['expected_delivery_date'] if ('expected_delivery_date' in o.keys() and o['expected_delivery_date']) else ''
            deadline_str = f"Limite: {brdate(o_exp_dt)} · " if o_exp_dt else ""
            o_has_nf = bool(o['invoice_number'] and str(o['invoice_number']).strip() not in ('', '0', '0.0', 'None', 'null'))
            o_nf_pill = f"<span class='badge' style='background:#047857; color:#fff; font-size:0.75rem; font-weight:700; padding:2px 6px; border-radius:4px; margin-left:6px;'>🧾 NF {esc(o['invoice_number'])}</span>" if o_has_nf else "<span class='badge' style='background:#fef3c7; color:#92400e; border:1px solid #fde68a; font-size:0.72rem; padding:1px 5px; border-radius:4px; margin-left:6px;'>Sem NF</span>"
            o_nf_text = f"NF: {esc(o['invoice_number'])} · " if o_has_nf else "Sem NF · "
            checks += f"""<label class='check load-check' data-route="{esc(route_value)}"><input type='checkbox' name='order_{o['id']}' data-route="{esc(route_value)}" data-weight='{float(o['weight_kg'] or 0)}'> <span><b>{esc(o['order_number'])}</b>{o_nf_pill} - {esc(o['client'] or '')}<small>{o_nf_text}Vendedor: {esc(resolve_seller_name(o['seller_name']))} · {sale_str}{deadline_str}{esc(o['city'] or '')} · {esc(route_value or 'Sem rota')} · {deadline_pill(o['expected_delivery_date'],o['status'])} · {fmt_num(o['weight_kg'])} kg</small>{move}{items_details}</span></label>"""
        if not route_names:
            route_names = ['Rota livre']
        route_select=''.join(f"<option value='{esc(r)}'>{esc(r)}</option>" for r in route_names)
        content=f"""<form method='post' action='/routes/new' class='form route-builder'><section class='builder-left'><fieldset><legend>1. Dados da carga</legend><div class='grid3'><label>Nome da carga<input name='name' required value='Carga {datetime.now().strftime('%d/%m %H:%M')}'></label><label>Data<input type='date' name='date' value='{today()}' required></label><label>Rota<select name='route_name' id='loadRouteSelect' required><option value=''>Selecione a rota</option>{route_select}</select><small>Selecione a rota principal desta carga.</small></label><label class='full route-lock-box'><span class='checkbox-label'><input type='checkbox' id='routeLockToggle' value='1'> Travar rota</span><small>Marcado: exibe só pedidos da rota selecionada. Desmarcado: exibe todos os pedidos elegíveis.</small></label><label>Motorista<select name='driver_id' required>{row_options(drivers,None,lambda r:r['name'])}</select></label><label>Veículo<select name='vehicle_id' id='vehicle_id' required><option value=''>Selecione veículo</option>{vehicle_options}</select></label><label>Capacidade kg<input id='capacity' name='capacity' type='text' inputmode='decimal' data-mask='decimal' data-decimals='2' value='{esc(get_setting('load_capacity_kg','11000'))}' required></label><label class='full'>Observações<textarea name='notes' placeholder='Instruções para entrega, restrições, ordem de prioridade...'></textarea></label></div></fieldset><div class='load-summary'><div><b>Pedidos selecionados: <span id='selectedCount'>0</span></b><small>Total previsto: <span id='selectedWeight'>0</span> kg</small></div><div class='load-capacity'><div class='capacity'><span id='capacityBar' style='width:0%'></span></div><small id='capacityAlert'>Capacidade dentro do limite</small></div></div><div class='alert info'>Resumo antes de salvar: confira quantidade de pedidos, peso total e capacidade do veículo.</div><button>Confirmar montagem da carga</button></section><section class='builder-right'><div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;'><h2 style='margin:0;'>2. Pedidos elegíveis para carga</h2><a class='btn small ghost' href='/orders/sync-carga-erp?redirect=/routes/new' style='color:#166534; border-color:#86efac; background:#f0fdf4; font-weight:700;' title='Puxar pedidos novos do ERP agora'>🔄 Atualizar ERP</a></div><p class='muted'>Com <b>Travar rota</b> marcado, a lista mostra somente pedidos da rota escolhida. Desmarcado, exibe todos os pedidos elegíveis.</p><div class='checks'>{checks or '<div class=empty>Nenhum pedido faturado para carga. Conclua o faturamento antes de criar rota.</div>'}</div></section></form>"""
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
                    if st not in ('Faturado','Carga Montada','Saiu para entrega'):
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
                if old != 'Carga Montada':
                    self.ensure_order_status(db,oid,'Carga Montada')
                    db.execute("UPDATE orders SET status='Carga Montada',updated_at=?,version=COALESCE(version,1)+1 WHERE id=?",(now(),oid))
                    add_hist(db,oid,u['id'],old,'Carga Montada','Carga montada (Aguardando saída)',f'Carga {rid}')
            for old_rid in old_routes:
                if old_rid != rid: self.recalc_route_weight(db,old_rid)
            self.recalc_route_weight(db,rid)
            audit(db,u,'Criou carga','Rotas',name,'',str(total)); db.commit()
        self.redirect(f'/routes/{rid}')

    def route_detail(self,u,rid):
        with conn() as db:
            r=db.execute("SELECT r.*,d.name driver,v.name vehicle,v.plate FROM routes r LEFT JOIN drivers d ON d.id=r.driver_id LEFT JOIN vehicles v ON v.id=r.vehicle_id WHERE r.id=?",(rid,)).fetchone()
            target_route = str(r['route_name'] or '').strip() if r else ''
            ros=db.execute("""SELECT ro.*,o.order_number,o.invoice_number,o.seller_name,o.status AS order_status,o.weight_kg,o.city,o.route_name,o.delivery_address,
                                     o.expected_delivery_date,o.sale_date,o.receipt_photo,o.delivery_location_link,c.name client,c.farm_name,
                                     (SELECT COUNT(*) FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.image_data IS NOT NULL) has_photo,
                                     (SELECT COUNT(*) FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.digital_signature IS NOT NULL) has_sig,
                                     (SELECT delivery_location_link FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.delivery_location_link IS NOT NULL AND dr.delivery_location_link!='' LIMIT 1) dr_loc_link
                              FROM route_orders ro JOIN orders o ON o.id=ro.order_id LEFT JOIN clients c ON c.id=o.client_id WHERE ro.route_id=? ORDER BY ro.delivery_order,ro.id""",(rid,)).fetchall()
            if target_route.lower().strip() == 'mista':
                available=db.execute("""SELECT o.id,o.order_number,o.invoice_number,o.seller_name,c.name client,o.city,o.route_name,o.weight_kg
                                        FROM orders o
                                        LEFT JOIN clients c ON c.id=o.client_id
                                        WHERE o.status IN ('Faturado','Carga Montada','Saiu para entrega')
                                          AND NOT EXISTS (
                                              SELECT 1
                                              FROM route_orders ro2
                                              JOIN routes ar ON ar.id=ro2.route_id
                                              WHERE ro2.order_id=o.id
                                              AND ar.status IN ('Planejada','Em rota')
                                          )
                                        ORDER BY o.expected_delivery_date,o.city""").fetchall()
            else:
                available=db.execute("""SELECT o.id,o.order_number,o.invoice_number,o.seller_name,c.name client,o.city,o.route_name,o.weight_kg
                                        FROM orders o
                                        LEFT JOIN clients c ON c.id=o.client_id
                                        WHERE o.status IN ('Faturado','Carga Montada','Saiu para entrega')
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
        
        order_ids = [ro['order_id'] for ro in ros]
        items_by_order = {}
        if order_ids:
            with conn() as db:
                placeholders = ','.join('?' for _ in order_ids)
                all_its = db.execute(f"SELECT order_id, product_name, quantity, unit, weight_kg, status, problem_notes FROM order_items WHERE order_id IN ({placeholders}) ORDER BY id", order_ids).fetchall()
                for it in all_its:
                    items_by_order.setdefault(it['order_id'], []).append(it)

        def _render_route_order(ro):
            loc_url, is_gps, loc_type = order_map_info(ro)
            map_html = f'<br><a href="{esc(loc_url)}" target="_blank" rel="noopener" class="btn small ghost" style="color:#0369a1; border-color:#0369a1; padding:1px 4px; font-size:0.75rem; margin-top:3px; display:inline-block;">📍 Mapa ({esc(loc_type)})</a>' if loc_url else ''
            photo_btn = f'<br><a href="/orders/{ro["order_id"]}/receipt-image" target="_blank" class="btn small ghost" style="color:#059669; border-color:#059669; padding:1px 4px; font-size:0.75rem; margin-top:3px; display:inline-block;">📷 Canhoto</a>' if (ro['has_photo'] or ro['receipt_photo']) else ''
            sig_btn = f'<a href="/orders/{ro["order_id"]}/signature-image" target="_blank" class="btn small ghost" style="color:#0284c7; border-color:#0284c7; padding:1px 4px; font-size:0.75rem; margin-top:3px; display:inline-block;">✍️ Assinatura</a>' if ro['has_sig'] else ''
            
            o_items = items_by_order.get(ro['order_id'], [])
            items_collapse = ''
            if o_items:
                it_list_parts = []
                for it in o_items:
                    is_prob = (it['status'] == 'Problema') if ('status' in it.keys() and it['status']) else False
                    prob_tag = f" <b style='color:#dc2626;'>[{esc(it['status'])}: {esc(it['problem_notes'])}]</b>" if is_prob else ""
                    qty_num = to_float(it['quantity'])
                    it_list_parts.append(
                        f"<li>{esc(it['product_name'])} <span class='muted'>({qty_num:.0f} {esc(it['unit'] or '')} - {fmt_num(it['weight_kg'])} kg)</span>{prob_tag}</li>"
                    )
                it_list = ''.join(it_list_parts)
                items_collapse = f"""<details class='order-items-collapse' style='margin-top:3px; font-size:0.75rem;'>
                    <summary style='cursor:pointer; color:#166534; font-weight:600;'>📦 Ver produtos ({len(o_items)}) ▾</summary>
                    <ul style='margin:3px 0 0 14px; padding:0; list-style-type:disc; color:#334155; font-size:0.75rem;'>{it_list}</ul>
                </details>"""

            ro_sale_dt = ro['sale_date'] if ('sale_date' in ro.keys() and ro['sale_date']) else ''
            sale_lbl = f"<small class='muted'>Venda: {brdate(ro_sale_dt)}</small><br>" if ro_sale_dt else ""
            ro_has_nf = bool(ro['invoice_number'] and str(ro['invoice_number']).strip() not in ('', '0', '0.0', 'None', 'null'))
            ro_nf_pill = f"<span class='badge' style='background:#047857; color:#fff; font-size:0.75rem; font-weight:700; padding:1px 6px; border-radius:4px; margin-left:4px;'>🧾 NF {esc(ro['invoice_number'])}</span>" if ro_has_nf else "<span class='badge' style='background:#fef3c7; color:#92400e; border:1px solid #fde68a; font-size:0.72rem; padding:1px 5px; border-radius:4px; margin-left:4px;'>Sem NF</span>"
            return f"""<tr><td><input form='seqForm' type='number' name='seq_{ro['id']}' value='{ro['delivery_order']}' class='seq' {'readonly' if not can_edit else ''}></td><td><a href='/orders/{ro['order_id']}'><b>{esc(ro['order_number'])}</b></a>{ro_nf_pill}<br><small>{esc(ro['client'] or '')} · Vendedor: {esc(resolve_seller_name(ro['seller_name']))}</small>{items_collapse}</td><td>{esc(ro['city'] or '')}<br><small>{esc(ro['route_name'] or '')}</small></td><td>{badge(ro['status'])}{photo_btn}{sig_btn}{map_html}</td><td>{sale_lbl}<b>Limite: {brdate(ro['expected_delivery_date'])}</b><br>{deadline_pill(ro['expected_delivery_date'],ro['order_status'])}</td><td>{fmt_num(ro['weight_kg'])} kg</td><td><div class='route-actions'>{f"<form method='post' action='/routes/{rid}/remove/{ro['id']}' class='inline-mini'><button class='danger-btn'>Remover</button></form>" if can_edit else "<span class='muted'>Carga finalizada</span>"}</div></td></tr>"""
        rows=''.join(_render_route_order(ro) for ro in ros)
        add_opts=''.join(f"<option value='{a['id']}'>{esc(a['order_number'])} (NF: {esc(a['invoice_number'] or 'Sem NF')}) - {esc(a['client'] or '')} - Vendedor: {esc(resolve_seller_name(a['seller_name']))} - {esc(a['city'] or '')} - {fmt_num(a['weight_kg'])}kg</option>" for a in available)
        lock_notice = '<div class="alert info">Carga finalizada: edição de pedidos e sequência está bloqueada para preservar o histórico.</div>' if not can_edit else ''
        dispatch_action = f"<form method='post' action='/routes/{rid}/dispatch'><button>1. Marcar saída</button></form>" if can_dispatch else "<span class='muted'>Saída já registrada</span>"
        cancel_action = f"""<form method='post' action='/routes/{rid}/cancel' class='inline-form needs-confirm' data-confirm-text='Confirma cancelar esta carga? Todos pedidos não finalizados voltarão para Faturado.'><input name='reason' placeholder='Motivo do cancelamento' required><button class='danger-btn'>Cancelar carga</button></form>""" if can_cancel else ""
        if is_god(u):
            delete_action = f"""<form method='post' action='/routes/{rid}/delete' class='inline-form needs-confirm' data-confirm-text='⚠️ ATENÇÃO GOD: Confirma apagar definitivamente esta carga e desvincular seus pedidos?'>
                <button class='danger-btn' style='background:#dc2626;border-color:#b91c1c;font-weight:800;'>⚡ Excluir Definitivamente (GOD Override)</button>
            </form>"""
        else:
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
        arrival_info = ''
        if r['arrival_location_link'] or r['arrival_at']:
            arr_date = brdate(r['arrival_at']) if r['arrival_at'] else '—'
            arr_time = str(r['arrival_at'])[11:16] if r['arrival_at'] and len(str(r['arrival_at'])) >= 16 else ''
            arr_maps = f'<a href="{esc(r["arrival_location_link"])}" target="_blank" rel="noopener" class="btn small primary" style="background:#166534; border-color:#166534; color:#ffffff; font-weight:700; padding:4px 10px; border-radius:6px; margin-left:8px; display:inline-flex; align-items:center; gap:4px;">🏁 Ver Local da Chegada no Google Maps</a>' if r['arrival_location_link'] else ''
            arrival_info = f'<div class="alert success" style="margin-top:10px; background:#f0fdf4; border-color:#86efac; color:#166534;">🏁 <b>Chegada na Loja Registrada:</b> {arr_date} {arr_time} {arr_maps}</div>'
        content=f"""{lock_notice}<section class='route-hero'><div><h2>{esc(r['name'])}</h2><p>{brdate(r['date'])} - {esc(r['route_name'] or 'Sem rota')} - {esc(r['driver'] or 'Sem motorista')} - {esc((r['vehicle'] or '')+' '+(r['plate'] or ''))}</p>{arrival_info}</div><div>{badge(r['status'])}<b>{fmt_num(r['total_weight'])} / {fmt_num(r['capacity'])} kg</b></div></section>{assisted}<div class='route-control-grid'><section class='panel'><h2>Capacidade</h2><div class='capacity big'><span style='width:{min(pct,100):.0f}%'></span></div><p><b>{pct:.0f}% usado</b> - {fmt_num(r['total_weight'])} kg carregados de {fmt_num(r['capacity'])} kg</p>{'<div class="alert danger">Carga acima da capacidade do veículo.</div>' if pct>100 else '<div class="alert success">Carga dentro da capacidade.</div>'}</section><section class='panel no-print'><h2>Ações principais</h2><div class='action-strip clean'>{dispatch_action}{delete_action}</div>{cancel_action}</section>{add_panel}</div>{edit_meta_panel}<form id='seqForm' method='post' action='/routes/{rid}/sequence'></form><section class='panel'><div class='section-title'><h2>Sequência de entrega</h2>{save_seq_btn}</div><div class='table-wrap'><table><thead><tr><th>Seq.</th><th>Pedido</th><th>Cidade/Rota</th><th>Status</th><th>Venda / Prazo</th><th>Peso</th><th>Ações</th></tr></thead><tbody>{rows or '<tr><td colspan=7>Nenhum pedido nesta carga.</td></tr>'}</tbody></table></div></section>{reopen_panel}"""
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
                if normalize_order_status(order['status']) not in ('Faturado','Carga Montada','Saiu para entrega'):
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
                new_order_status='Saiu para entrega' if route['status']=='Em rota' else 'Carga Montada'
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
                if old in ('Saiu para entrega', 'Carga Montada'):
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
        
        if not q:
            with conn() as db:
                recent=db.execute("""
                    SELECT r.*, d.name driver, v.name vehicle, v.plate, COUNT(ro.id) orders_count, COALESCE(SUM(o.weight_kg), 0) total_kg
                    FROM routes r 
                    LEFT JOIN drivers d ON d.id=r.driver_id
                    LEFT JOIN vehicles v ON v.id=r.vehicle_id
                    LEFT JOIN route_orders ro ON ro.route_id=r.id 
                    LEFT JOIN orders o ON o.id=ro.order_id
                    WHERE r.status IN ('Em rota','Aguardando Chegada') 
                    GROUP BY r.id 
                    ORDER BY CASE r.status WHEN 'Aguardando Chegada' THEN 0 ELSE 1 END, r.date DESC, r.id DESC 
                    LIMIT 40
                """).fetchall()
            
            trs = []
            for r in recent:
                w = r['total_weight'] or r['total_kg'] or 0
                trs.append(
                    f"<tr>"
                    f"<td><b>{esc(r['name'])}</b><br><small class='muted'>Carga #{r['id']}</small></td>"
                    f"<td>{brdate(r['date'])}</td>"
                    f"<td><b>{esc(r['route_name'] or 'Rota livre')}</b></td>"
                    f"<td>{esc(r['driver'] or 'Não atribuído')}</td>"
                    f"<td>{esc((r['vehicle'] or '')+' '+(r['plate'] or '')) or 'Não informado'}</td>"
                    f"<td><span class='badge neutral'>{r['orders_count']} pedidos</span></td>"
                    f"<td><b>{fmt_num(w)} kg</b></td>"
                    f"<td>{badge(r['status'])}</td>"
                    f"<td><a class='btn small primary' href='/load-settlement?q={quote(r['name'])}' style='font-weight:700;'>Conferir / Fazer Acerto</a></td>"
                    f"</tr>"
                )
            
            table_html = f"""
            <div class='table-wrap'>
              <table>
                <thead>
                  <tr>
                    <th>Carga</th>
                    <th>Data</th>
                    <th>Rota</th>
                    <th>Motorista</th>
                    <th>Veículo</th>
                    <th>Qtd Pedidos</th>
                    <th>Peso Total</th>
                    <th>Status</th>
                    <th>Ação</th>
                  </tr>
                </thead>
                <tbody>
                  {''.join(trs) if trs else "<tr><td colspan='9' class='empty'>Nenhuma carga ativa ou aguardando chegada no momento.</td></tr>"}
                </tbody>
              </table>
            </div>
            """
            
            content = f"""
            <section class='panel clean-panel' style='background:#fff; border:1px solid var(--border); border-radius:var(--radius); padding:16px;'>
              <div class='section-title' style='display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;'>
                <div>
                  <h2 style='font-size:1.1rem; margin:0;'>Cargas ativas e aguardando chegada</h2>
                  <small class='muted'>Selecione uma carga abaixo ou pesquise pelo código na barra superior para realizar a conferência e baixa.</small>
                </div>
                <span class='badge ok'>{len(recent)} cargas ativas</span>
              </div>
              {table_html}
            </section>
            """
            return self.send_html(layout(u,'Acerto de Carga',content,'Fechamento operacional da carga e baixa final das entregas',search_action='/load-settlement',search_placeholder='Buscar carga para acerto por número, nome ou motorista...',search_q=q))
        
        with conn() as db:
            r=db.execute("""SELECT r.*,d.name driver,v.name vehicle,v.plate
                            FROM routes r
                            LEFT JOIN drivers d ON d.id=r.driver_id
                            LEFT JOIN vehicles v ON v.id=r.vehicle_id
                            WHERE CAST(r.id AS TEXT)=? OR r.name=? OR r.name LIKE ?
                            ORDER BY r.id DESC LIMIT 1""",(q,q,f'%{q}%')).fetchone()
            if not r:
                return self.send_html(layout(u,'Acerto de Carga',f'<div class="alert danger">Carga "{esc(q)}" não encontrada. Confira o código/nome e tente novamente.</div><div style="margin-top:14px;"><a class="btn ghost" href="/load-settlement">← Ver todas as cargas ativas</a></div>','Fechamento operacional da carga e baixa final das entregas',search_action='/load-settlement',search_placeholder='Buscar carga para acerto por número, nome ou motorista...',search_q=q))
            rows=db.execute("""SELECT ro.*,o.id order_id,o.order_number,o.seller_name,o.status,o.weight_kg,o.total_value,
                                      o.payment_method,o.delivered_at,o.final_notes,o.city,o.route_name,o.receipt_photo,
                                      o.delivery_location_link,c.name client,c.farm_name,
                                      (SELECT COUNT(*) FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.image_data IS NOT NULL) has_photo,
                                      (SELECT COUNT(*) FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.digital_signature IS NOT NULL) has_sig,
                                      (SELECT delivery_location_link FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.delivery_location_link IS NOT NULL AND dr.delivery_location_link!='' LIMIT 1) dr_loc_link
                               FROM route_orders ro JOIN orders o ON o.id=ro.order_id LEFT JOIN clients c ON c.id=o.client_id WHERE ro.route_id=? ORDER BY ro.delivery_order,ro.id""",(r['id'],)).fetchall()
        route_status = normalize_route_status(r['status'])
        
        order_ids = [ro['order_id'] for ro in rows]
        items_by_order = {}
        if order_ids:
            with conn() as db:
                placeholders = ','.join('?' for _ in order_ids)
                all_its = db.execute(f"SELECT * FROM order_items WHERE order_id IN ({placeholders}) ORDER BY id", order_ids).fetchall()
                for it in all_its:
                    items_by_order.setdefault(it['order_id'], []).append(it)

        with conn() as db:
            expenses_rows = db.execute("SELECT * FROM trip_expenses WHERE route_id=? ORDER BY id", (r['id'],)).fetchall()
            has_start_photo = bool(r['odometer_start_photo'] or db.execute("SELECT 1 FROM trip_photos WHERE route_id=? AND photo_type='start' LIMIT 1", (r['id'],)).fetchone())
            has_end_photo = bool(r['odometer_end_photo'] or db.execute("SELECT 1 FROM trip_photos WHERE route_id=? AND photo_type='end' LIMIT 1", (r['id'],)).fetchone())

        exp_badges = []
        exp_total_sum = 0
        for ex in expenses_rows:
            tot = float(ex['total_value'] or 0)
            exp_total_sum += tot
            note_str = f" ({esc(ex['notes'])})" if ex['notes'] else ""
            exp_badges.append(f"<span style='background:#f1f5f9; border:1px solid #cbd5e1; border-radius:6px; padding:3px 8px; font-size:0.8rem;'><b>{esc(ex['expense_type'])}:</b> R$ {tot:,.2f}{note_str}</span>")
        
        exp_html = f"<div style='display:flex; flex-wrap:wrap; gap:6px; margin-top:6px;'>{''.join(exp_badges)}</div><div style='font-size:0.85rem; font-weight:700; color:#1e3a8a; margin-top:6px;'>Total Despesas: R$ {exp_total_sum:,.2f}</div>" if expenses_rows else "<span class='muted'>Nenhuma despesa lançada nesta viagem.</span>"

        has_driver_sig = bool(r['driver_signature'])
        start_photo_btn = f"<a href='/routes/{r['id']}/odometer-photo/start' target='_blank' class='btn small ghost' style='color:#0369a1; border-color:#0369a1; font-weight:700;'>📸 Odômetro Inicial</a>" if has_start_photo else "<span class='muted'>Sem foto</span>"
        end_photo_btn = f"<a href='/routes/{r['id']}/odometer-photo/end' target='_blank' class='btn small ghost' style='color:#166534; border-color:#166534; font-weight:700;'>📸 Odômetro Final</a>" if has_end_photo else "<span class='muted'>Sem foto</span>"
        sig_badge = f"<span style='color:#166534; font-weight:700;'>✅ Assinada</span>" if has_driver_sig else "<span class='muted'>Pendente</span>"

        km_start_display = f"{float(r['km_start']):,.0f} km" if r['km_start'] else "Não informado"
        km_end_display = f"{float(r['km_end']):,.0f} km" if r['km_end'] else "Não informado"
        km_tot_display = f"{float(r['km_total']):,.0f} km" if r['km_total'] else "—"

        telemetry_card_html = f"""
        <section class='panel' style='background:#f8fafc; border:1px solid #cbd5e1; border-radius:10px; padding:16px; margin:16px 0;'>
          <div style='display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:12px; border-bottom:1px solid #e2e8f0; padding-bottom:8px;'>
            <div>
              <h3 style='margin:0; font-size:1.05rem; color:#1e3a8a; display:flex; align-items:center; gap:8px;'>
                <span>📋</span> Ficha de Telemetria e Fechamento da Viagem
              </h3>
              <small class='muted'>Dados operacionais de odômetro, fotos comprobatórias, despesas de rota e assinatura do motorista.</small>
            </div>
            <div style='display:flex; gap:8px;'>
              <a href='/routes/{r['id']}/trip-sheet-pdf' target='_blank' class='btn small primary' style='font-weight:700; background:#166534; border-color:#166534; display:flex; align-items:center; gap:6px;'>
                <span>📄</span> Imprimir Ficha de Viagem Oficial (PDF)
              </a>
            </div>
          </div>

          <div style='display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:12px;'>
            <div style='background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:10px;'>
              <span style='font-size:0.75rem; font-weight:700; color:#64748b; text-transform:uppercase;'>KM Inicial & Foto</span>
              <div style='font-size:1.1rem; font-weight:800; color:#0f172a; margin:4px 0;'>{km_start_display}</div>
              <div>{start_photo_btn}</div>
              <small class='muted'>Saída: {esc(r['departure_time'] or '—')}</small>
            </div>

            <div style='background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:10px;'>
              <span style='font-size:0.75rem; font-weight:700; color:#64748b; text-transform:uppercase;'>KM Final & Foto</span>
              <div style='font-size:1.1rem; font-weight:800; color:#0f172a; margin:4px 0;'>{km_end_display}</div>
              <div>{end_photo_btn}</div>
              <small class='muted'>Chegada: {esc(r['arrival_time'] or r['arrival_at'] or '—')}</small>
            </div>

            <div style='background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:10px;'>
              <span style='font-size:0.75rem; font-weight:700; color:#64748b; text-transform:uppercase;'>Total Percorrido</span>
              <div style='font-size:1.3rem; font-weight:800; color:#166534; margin:4px 0;'>{km_tot_display}</div>
              <small class='muted'>Destino: {esc(r['destination'] or 'T.O. / Operação')}</small>
            </div>

            <div style='background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:10px;'>
              <span style='font-size:0.75rem; font-weight:700; color:#64748b; text-transform:uppercase;'>Equipe & Assinatura</span>
              <div style='font-size:0.88rem; font-weight:700; margin:4px 0;'>Ajudante: {esc(r['helper_name'] or 'Sem ajudante')}</div>
              <div style='font-size:0.85rem;'>Ficha: {sig_badge}</div>
            </div>
          </div>

          <div style='background:#fff; border:1px solid #e2e8f0; border-radius:8px; padding:12px;'>
            <b style='font-size:0.85rem; color:#334155; text-transform:uppercase;'>💰 Despesas Declaradas da Viagem</b>
            {exp_html}
          </div>
        </section>
        """

        if not rows:
            body='<div class="empty">Esta carga não possui pedidos vinculados.</div>'
        elif route_status not in ('Em rota', 'Aguardando Chegada'):
            cards_list = []
            for ro in rows:
                photo_btn = f"<a class='btn small ghost' style='color:#059669; border-color:#059669; font-weight:700;' href='/orders/{ro['order_id']}/receipt-image' target='_blank'>📷 Ver Foto do Canhoto</a>" if (ro['has_photo'] or ro['receipt_photo']) else ""
                sig_btn = f"<a class='btn small ghost' style='color:#0284c7; border-color:#0284c7; font-weight:700;' href='/orders/{ro['order_id']}/signature-image' target='_blank'>✍️ Ver Assinatura Digital</a>" if ro['has_sig'] else ""
                loc_url = ro['dr_loc_link'] if ('dr_loc_link' in ro.keys() and ro['dr_loc_link']) else (ro['delivery_location_link'] if 'delivery_location_link' in ro.keys() else None)
                map_btn = f"<a class='btn small ghost' style='color:#0369a1; border-color:#0369a1; font-weight:700;' href='{esc(loc_url)}' target='_blank'>🌐 Ver Mapa no Google Maps</a>" if loc_url else ""
                btns_html = f"<div class='full' style='display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;'>{photo_btn}{sig_btn}{map_btn}</div>" if (photo_btn or sig_btn or map_btn) else ""
                
                o_items = items_by_order.get(ro['order_id'], [])
                items_block = ''
                if o_items:
                    it_rows = ''.join(
                        f"<tr><td style='padding:4px 6px;'>{esc(it['product_name'])}</td><td style='padding:4px 6px; text-align:center;'>{to_float(it['quantity']):.0f} {esc(it['unit'] or '')}</td><td style='padding:4px 6px; text-align:center;'>{badge(it['status'] or 'Entregue')}</td><td style='padding:4px 6px;'>{esc(it['problem_notes'] or '—')}</td></tr>"
                        for it in o_items
                    )
                    items_block = f"""<details class='settlement-items-collapse' style='margin-top:8px; border:1px solid #e2e8f0; border-radius:6px; background:#f8fafc; padding:6px 10px;'>
                        <summary style='cursor:pointer; font-weight:700; color:#166534; font-size:0.83rem;'>📦 Produtos da entrega ({len(o_items)} itens) ▾</summary>
                        <table style='width:100%; border-collapse:collapse; margin-top:6px; font-size:0.8rem;'>
                            <thead><tr style='border-bottom:1px solid #cbd5e1; color:#64748b;'><th style='text-align:left; padding:3px 6px;'>Produto</th><th style='text-align:center; padding:3px 6px;'>Qtd</th><th style='text-align:center; padding:3px 6px;'>Status</th><th style='text-align:left; padding:3px 6px;'>Ocorrência</th></tr></thead>
                            <tbody>{it_rows}</tbody>
                        </table>
                    </details>"""

                cards_list.append(
                    f"<article class='settlement-order'><div class='settlement-main'><div><b>{esc(ro['order_number'])}</b><small>{esc(ro['client'] or '')} · {esc(ro['city'] or '')} · Vendedor: {esc(ro['seller_name'] or 'Não informado')} · {fmt_num(ro['weight_kg'])} kg · {money_visible(can_view_financial, ro['total_value'])}</small></div>{badge(ro['status'])}</div><div class='settlement-fields'><label>Método pagamento<input value='{esc(ro['payment_method'] or 'Não informado')}' readonly></label><label>Data entrega/ocorrência<input value='{esc(brdate(ro['delivered_at']))}' readonly></label><label class='full'>Observação final<textarea readonly>{esc(ro['final_notes'] or 'Sem observação')}</textarea></label>{btns_html}</div>{items_block}</article>"
                )
            cards = ''.join(cards_list)
            body=f"""<section class='panel'><div class='alert info'>Carga já finalizada em status <b>{esc(route_status)}</b>. Edição bloqueada para preservar o histórico.</div><section class='route-hero settlement-hero'><div><h2>{esc(r['name'])}</h2><p>Carga #{r['id']} · {brdate(r['date'])} · {esc(r['route_name'] or 'Rota livre')} · {esc(r['driver'] or 'Sem motorista')} · {esc((r['vehicle'] or '')+' '+(r['plate'] or ''))}</p></div><div>{badge(r['status'])}<b>{fmt_num(r['total_weight'])} kg</b></div></section>{telemetry_card_html}<div class='settlement-list'>{cards}</div><div class='form-actions'><a class='btn ghost' href='/load-settlement/{r['id']}/print-report'>Relatório compacto (1 página)</a><a class='btn ghost' href='/routes/{r['id']}'>Voltar para operação da carga</a></div></section>"""
        else:
            trs=''
            arrival_notice = '<div class="alert warning" style="background:#fef3c7; border-color:#fde047; color:#92400e; margin-bottom:1rem; padding:12px 16px; border-radius:8px; font-size:0.95rem;">🏁 <b>Carga Aguardando Registro de Chegada:</b> As entregas desta carga foram concluídas pelo motorista no aplicativo mobile. O acerto e conferência dos pedidos pode ser realizado abaixo.</div>' if route_status == 'Aguardando Chegada' else ''
            for ro in rows:
                delivered_date=(ro['delivered_at'] or today())[:10]
                photo_btn = f"<a class='btn small ghost' style='color:#059669; border-color:#059669; font-weight:700;' href='/orders/{ro['order_id']}/receipt-image' target='_blank'>📷 Ver Foto do Canhoto</a>" if (ro['has_photo'] or ro['receipt_photo']) else ""
                sig_btn = f"<a class='btn small ghost' style='color:#0284c7; border-color:#0284c7; font-weight:700;' href='/orders/{ro['order_id']}/signature-image' target='_blank'>✍️ Ver Assinatura Digital</a>" if ro['has_sig'] else ""
                loc_url = ro['dr_loc_link'] if ('dr_loc_link' in ro.keys() and ro['dr_loc_link']) else (ro['delivery_location_link'] if 'delivery_location_link' in ro.keys() else None)
                map_btn = f"<a class='btn small ghost' style='color:#0369a1; border-color:#0369a1; font-weight:700;' href='{esc(loc_url)}' target='_blank'>🌐 Ver Mapa no Google Maps</a>" if loc_url else ""
                rec_btns = f"<div class='full' style='display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;'>{photo_btn}{sig_btn}{map_btn}</div>" if (photo_btn or sig_btn or map_btn) else ""
                
                o_items = items_by_order.get(ro['order_id'], [])
                items_block = ''
                if o_items:
                    it_rows = ''
                    for it in o_items:
                        it_id = it['id']
                        is_prob = (it['status'] == 'Problema')
                        it_prob_text = it['problem_notes'] or ''
                        sel_ok = 'selected' if not is_prob else ''
                        sel_prob = 'selected' if is_prob else ''
                        prob_style = 'block' if is_prob else 'none'
                        prob_req = 'required' if is_prob else ''

                        it_rows += f"""<tr style='border-bottom:1px solid #e2e8f0;'>
                            <td style='padding:6px 8px; vertical-align:middle;'>
                                <b>{esc(it['product_name'])}</b>
                                <br><small class='muted'>{esc(it['product_code'] or '')} {f"· {esc(it['notes'])}" if it['notes'] else ""}</small>
                            </td>
                            <td style='padding:6px 8px; text-align:center; vertical-align:middle;'>
                                <b>{to_float(it['quantity']):.0f}</b> {esc(it['unit'] or '')}
                                <br><small class='muted'>{fmt_num(it['weight_kg'])} kg</small>
                            </td>
                            <td style='padding:6px 8px; text-align:center; vertical-align:middle;'>
                                <select name='item_status_{it_id}' class='form-control item-status-selector' data-item-id='{it_id}' data-order-id='{ro['order_id']}' style='padding:4px 8px; font-size:0.82rem; border-radius:4px;'>
                                    <option value='Entregue' {sel_ok}>✅ Entregue</option>
                                    <option value='Problema' {sel_prob}>❌ Problema</option>
                                </select>
                            </td>
                            <td style='padding:6px 8px; vertical-align:middle;'>
                                <div id='item_prob_wrap_{it_id}' style='display:{prob_style};'>
                                    <input type='text' name='item_problem_{it_id}' id='item_problem_{it_id}' class='form-control item-problem-input' value='{esc(it_prob_text)}' placeholder='⚠️ Descreva obrigatoriamente o que aconteceu com este produto...' style='width:100%; border:1px solid #ef4444; background:#fef2f2; font-size:0.82rem; padding:5px 8px; border-radius:4px;' {prob_req}>
                                </div>
                                <span id='item_ok_badge_{it_id}' class='muted' style='display:{'none' if is_prob else 'inline'}; font-size:0.75rem;'>Item sem ocorrência</span>
                            </td>
                        </tr>"""

                    items_block = f"""<details class='settlement-items-collapse' open style='margin-top:10px; border:1px solid #cbd5e1; border-radius:6px; background:#f8fafc; padding:8px 10px;'>
                        <summary style='cursor:pointer; font-weight:700; color:#166534; font-size:0.85rem;'>📦 Conferência por Produto ({len(o_items)} itens) ▾</summary>
                        <div style='overflow-x:auto; margin-top:6px;'>
                            <table style='width:100%; border-collapse:collapse; background:#ffffff; border-radius:4px; font-size:0.82rem;'>
                                <thead>
                                    <tr style='background:#f1f5f9; color:#475569; text-align:left; font-size:0.75rem; text-transform:uppercase;'>
                                        <th style='padding:6px 8px;'>Produto</th>
                                        <th style='padding:6px 8px; text-align:center;'>Qtd</th>
                                        <th style='padding:6px 8px; text-align:center;'>Status</th>
                                        <th style='padding:6px 8px;'>Ocorrência no Produto</th>
                                    </tr>
                                </thead>
                                <tbody>{it_rows}</tbody>
                            </table>
                        </div>
                    </details>"""

                trs += f"""<article class='settlement-order'><div class='settlement-main'><label class='settlement-check'><input type='checkbox' name='ok_{ro['order_id']}' class='settlement-ok' required> <span>Checklist conferido</span></label><div><b>{esc(ro['order_number'])}</b><small>{esc(ro['client'] or '')} · {esc(ro['city'] or '')} · Vendedor: {esc(ro['seller_name'] or 'Não informado')} · {fmt_num(ro['weight_kg'])} kg · {money_visible(can_view_financial, ro['total_value'])}</small></div>{badge(ro['status'])}</div><div class='settlement-fields'><label>Resultado<select name='result_{ro['order_id']}' class='settlement-result'><option value='entregue'>Entregue</option><option value='problema'>Registrar problema</option></select></label><label>Data entrega/ocorrência<input type='date' name='date_{ro['order_id']}' value='{esc(delivered_date)}' required></label><label>Método pagamento<select name='pay_{ro['order_id']}'>{payment_method_options(ro['payment_method'] or '')}</select></label><label>Tipo de problema<select name='ptype_{ro['order_id']}'>{option(PROBLEM_TYPES,'Outro motivo')}</select></label><label class='full'>Observação do acerto<textarea name='obs_{ro['order_id']}' placeholder='Recebedor, divergência, comprovante ou descrição do problema...'>{esc(ro['final_notes'] or '')}</textarea></label>{rec_btns}</div>{items_block}</article>"""
            
            settlement_js = """<script>
            document.addEventListener('DOMContentLoaded', function() {
                document.querySelectorAll('.item-status-selector').forEach(function(sel) {
                    sel.addEventListener('change', function() {
                        var itemId = this.dataset.itemId;
                        var orderId = this.dataset.orderId;
                        var wrap = document.getElementById('item_prob_wrap_' + itemId);
                        var badge = document.getElementById('item_ok_badge_' + itemId);
                        var input = document.getElementById('item_problem_' + itemId);
                        if (this.value === 'Problema') {
                            if (wrap) wrap.style.display = 'block';
                            if (badge) badge.style.display = 'none';
                            if (input) {
                                input.required = true;
                                input.focus();
                            }
                            var orderResult = document.querySelector('select[name="result_' + orderId + '"]');
                            if (orderResult) {
                                orderResult.value = 'problema';
                            }
                        } else {
                            if (wrap) wrap.style.display = 'none';
                            if (badge) badge.style.display = 'inline';
                            if (input) {
                                input.required = false;
                            }
                        }
                    });
                });

                var form = document.querySelector('.settlement-form');
                if (form) {
                    form.addEventListener('submit', function(e) {
                        var invalidInput = null;
                        document.querySelectorAll('.item-status-selector').forEach(function(sel) {
                            if (sel.value === 'Problema') {
                                var itemId = sel.dataset.itemId;
                                var input = document.getElementById('item_problem_' + itemId);
                                if (input && !input.value.trim()) {
                                    invalidInput = input;
                                    var details = input.closest('details');
                                    if (details) details.open = true;
                                }
                            }
                        });
                        if (invalidInput) {
                            e.preventDefault();
                            alert('Atenção: É obrigatório descrever o que aconteceu com todos os produtos marcados com problema!');
                            invalidInput.focus();
                            invalidInput.style.borderColor = '#b91c1c';
                            invalidInput.style.boxShadow = '0 0 0 3px rgba(220, 38, 38, 0.3)';
                            return false;
                        }
                    });
                }
            });
            </script>"""

            body=f"""<form method='post' action='/load-settlement/{r['id']}/finish' class='settlement-form needs-confirm' data-confirm-text='Confirma concluir a carga? A baixa será aplicada em todos os pedidos conferidos.'>{arrival_notice}<section class='route-hero settlement-hero'><div><h2>{esc(r['name'])}</h2><p>Carga #{r['id']} · {brdate(r['date'])} · {esc(r['route_name'] or 'Rota livre')} · {esc(r['driver'] or 'Sem motorista')} · {esc((r['vehicle'] or '')+' '+(r['plate'] or ''))}</p></div><div>{badge(r['status'])}<b>{fmt_num(r['total_weight'])} kg</b></div></section>{telemetry_card_html}<div class='settlement-tools'><div style='display: flex; align-items: center; gap: 15px; flex-wrap: wrap;'><label class='settlement-check'><input type='checkbox' id='markAllDelivered'> <span>Marcar todos como entregues</span></label><button type='button' id='btnSetDateSelected' class='btn' style='font-size: 13px; padding: 6px 12px; height: auto;'>Definir Data para Selecionados</button></div><small class='muted'>Se houver problema em algum pedido, troque o resultado para <b>Registrar problema</b>.</small></div><div class='alert info'>Marque todos os checklists para liberar a conclusão. Ao concluir, cada pedido será baixado como <b>entregue</b> ou <b>problema</b> conforme selecionado.</div><div class='settlement-list'>{trs}</div><div class='form-actions sticky-actions'><button class='success-btn'>Concluir Carga</button><a class='btn ghost' href='/routes/{r['id']}'>Voltar para operação da carga</a></div></form>{settlement_js}
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
        return self.send_html(layout(u,'Acerto de Carga',body,'Fechamento operacional da carga e baixa final das entregas',search_action='/load-settlement',search_placeholder='Buscar carga para acerto por número, nome ou motorista...',search_q=q))

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
            if normalize_route_status(r['status']) not in ('Em rota', 'Aguardando Chegada'):
                return self.fail(u, 'Operação inválida', 'Somente cargas em rota ou aguardando chegada podem ter as datas de pedidos alteradas.', 400)
                
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

    def map_view(self, u):
        can_view_financial = self.can_view_financial(u)
        with conn() as db:
            routes = db.execute("SELECT DISTINCT route_name FROM orders WHERE route_name IS NOT NULL AND route_name!='' AND status NOT IN ('Cancelado', 'Acertado', 'Entregue') ORDER BY route_name").fetchall()
            cities = db.execute("SELECT DISTINCT city FROM orders WHERE city IS NOT NULL AND city!='' AND status NOT IN ('Cancelado', 'Acertado', 'Entregue') ORDER BY city").fetchall()
            drivers = db.execute("SELECT id, name FROM drivers WHERE active=1 ORDER BY name").fetchall()
            vehicles = db.execute("SELECT id, name, plate, capacity_kg, capacity FROM vehicles WHERE active=1 ORDER BY name").fetchall()

        route_names = [r['route_name'] for r in routes]
        if 'Mista' not in route_names:
            route_names.insert(0, 'Mista')

        route_options = ''.join(f"<option value='{esc(r)}'>{esc(r)}</option>" for r in route_names)
        city_options = ''.join(f"<option value='{esc(c['city'])}'>{esc(c['city'])}</option>" for c in cities)
        driver_options = '<option value="">Selecione o motorista</option>' + ''.join(f"<option value='{d['id']}'>{esc(d['name'])}</option>" for d in drivers)
        vehicle_options = '<option value="">Selecione o veículo</option>' + ''.join(
            f"<option value='{v['id']}' data-capacity='{esc(v['capacity_kg'] if v['capacity_kg'] is not None else (v['capacity'] or 11000))}'>{esc(v['name'])} ({esc(v['plate'] or 'sem placa')}) - {fmt_num(v['capacity_kg'] if v['capacity_kg'] is not None else (v['capacity'] or 0))} kg</option>"
            for v in vehicles
        )
        
        default_load_name = f"Carga {datetime.now().strftime('%d/%m %H:%M')}"
        default_capacity = esc(get_setting('load_capacity_kg', '11000'))

        content = f"""
        <div id='mapPageContainer' class='map-page-container' style='display:grid; grid-template-columns: 360px 1fr; gap:12px; height:calc(100vh - 130px); min-height:550px; position:relative;'>
          
          <!-- Painel Lateral / Gaveta de Pedidos e Montagem de Cargas -->
          <div id='mapSidebarPanel' class='map-sidebar-panel' style='background:#ffffff; border:1px solid var(--border); border-radius:var(--radius); padding:12px; display:flex; flex-direction:column; gap:10px; height:100%; box-shadow:var(--shadow-sm); overflow:hidden; transition:all 0.2s ease-in-out;'>
            
            <!-- Barra Superior de Alternância de Abas e Recolhimento -->
            <div style='display:flex; align-items:center; justify-content:space-between; gap:6px; padding-bottom:4px; border-bottom:1px solid #f1f5f9;'>
              <div style='display:flex; gap:6px; flex:1;'>
                <button type='button' id='tabBtnOrders' class='map-tab-btn active'>
                  <span>📋 Pedidos & SLA</span>
                  <span id='mapTotalBadge' class='badge' style='background:#e2e8f0; color:#1e293b; padding:2px 6px; font-size:11px;'>0</span>
                </button>
                <button type='button' id='tabBtnDraft' class='map-tab-btn'>
                  <span>🚚 Montar Carga</span>
                  <span id='draftBadge' class='badge' style='background:#f59e0b; color:#ffffff; padding:2px 6px; font-size:11px;'>0</span>
                </button>
              </div>
              <button type='button' id='btnToggleSidebar' class='btn small ghost' title='Ocultar / Expandir Painel' style='padding:6px 9px; font-size:12px; border-radius:6px;'>◀</button>
            </div>

            <!-- ================= ABA 1: PEDIDOS & SLA ================= -->
            <div id='tabContentOrders' style='display:flex; flex-direction:column; gap:8px; flex:1; min-height:0;'>
              <!-- Chips Compactos de SLA -->
              <div style='display:flex; gap:4px; overflow-x:auto; padding-bottom:2px;'>
                <div class='map-sla-pill' data-filter='all' style='flex:1; min-width:55px; background:#f8fafc; border:1px solid var(--border); border-radius:6px; padding:4px 6px; text-align:center; cursor:pointer;'>
                  <small style='color:#64748b; font-size:10px; font-weight:700; display:block;'>TODOS</small>
                  <strong id='cntAll' style='font-size:0.95rem; color:#0f172a;'>0</strong>
                </div>
                <div class='map-sla-pill' data-filter='late' style='flex:1; min-width:55px; background:#fef2f2; border:1px solid #fecaca; border-radius:6px; padding:4px 6px; text-align:center; cursor:pointer;'>
                  <small style='color:#991b1b; font-size:10px; font-weight:700; display:block;'>🔴 VENCIDOS</small>
                  <strong id='cntLate' style='font-size:0.95rem; color:#dc2626;'>0</strong>
                </div>
                <div class='map-sla-pill' data-filter='risk' style='flex:1; min-width:55px; background:#fefce8; border:1px solid #fef08a; border-radius:6px; padding:4px 6px; text-align:center; cursor:pointer;'>
                  <small style='color:#854d0e; font-size:10px; font-weight:700; display:block;'>🟡 RISCO</small>
                  <strong id='cntRisk' style='font-size:0.95rem; color:#d97706;'>0</strong>
                </div>
                <div class='map-sla-pill' data-filter='ok' style='flex:1; min-width:55px; background:#f0fdf4; border:1px solid #bbf7d0; border-radius:6px; padding:4px 6px; text-align:center; cursor:pointer;'>
                  <small style='color:#166534; font-size:10px; font-weight:700; display:block;'>🟢 NO PRAZO</small>
                  <strong id='cntOk' style='font-size:0.95rem; color:#16a34a;'>0</strong>
                </div>
                <div class='map-sla-pill' data-filter='in_route' style='flex:1; min-width:55px; background:#faf5ff; border:1px solid #e9d5ff; border-radius:6px; padding:4px 6px; text-align:center; cursor:pointer;'>
                  <small style='color:#6b21a8; font-size:10px; font-weight:700; display:block;'>🟣 EM ROTA</small>
                  <strong id='cntInRoute' style='font-size:0.95rem; color:#9333ea;'>0</strong>
                </div>
              </div>

              <!-- Indicadores de GPS Rural vs Ponto Aproximado -->
              <div style='display:flex; gap:6px; font-size:11px; margin:2px 0;'>
                <div style='flex:1; background:#f0fdf4; border:1px solid #bbf7d0; border-radius:6px; padding:4px 6px; color:#166534; display:flex; align-items:center; justify-content:space-between; font-weight:600;' title='Pedidos com localização exata da fazenda confirmada por motorista ou mapa'>
                  <span>🌾 Rural Confirmado:</span>
                  <b id='cntVerifiedRural' style='font-size:12px;'>0</b>
                </div>
                <div style='flex:1; background:#fefce8; border:1px solid #fef08a; border-radius:6px; padding:4px 6px; color:#854d0e; display:flex; align-items:center; justify-content:space-between; font-weight:600;' title='Pedidos com ponto provisório no centro da cidade (1ª Entrega Rural)'>
                  <span>⚠️ Centro Cidade:</span>
                  <b id='cntCityApprox' style='font-size:12px;'>0</b>
                </div>
              </div>

              <!-- Filtros Compactos de Busca, Rota, Cidade e Status -->
              <div style='display:flex; flex-direction:column; gap:5px;'>
                <input type='text' id='mapSearchInput' class='form-control' placeholder='🔍 Buscar cliente, pedido, cidade...' style='width:100%; padding:6px 10px; border:1px solid var(--border); border-radius:var(--radius-sm); font-size:0.82rem;'>
                <div style='display:grid; grid-template-columns: 1fr 1fr; gap:5px;'>
                  <select id='mapRouteSelect' class='form-control' style='padding:5px 6px; border:1px solid var(--border); border-radius:var(--radius-sm); font-size:0.78rem;'>
                    <option value=''>Todas as Rotas</option>
                    {route_options}
                  </select>
                  <select id='mapCitySelect' class='form-control' style='padding:5px 6px; border:1px solid var(--border); border-radius:var(--radius-sm); font-size:0.78rem;'>
                    <option value=''>Todas as Cidades</option>
                    {city_options}
                  </select>
                </div>
                <div style='display:grid; grid-template-columns: 1fr auto; gap:5px;'>
                  <select id='mapStatusSelect' class='form-control' style='padding:5px 6px; border:1px solid var(--border); border-radius:var(--radius-sm); font-size:0.78rem;'>
                    <option value=''>Todos Status Ativos</option>
                    <option value='Venda'>Venda (Aguardando Faturamento)</option>
                    <option value='Faturado'>Faturado (Pronto p/ Carga)</option>
                    <option value='Carga Montada'>Carga Montada (Aguardando Saída)</option>
                    <option value='Saiu para entrega'>Saiu para entrega (Em Rota)</option>
                    <option value='Agendado'>Agendado</option>
                    <option value='Problema'>Com problema / Ocorrência</option>
                  </select>
                  <button type='button' id='btnResetFilters' class='btn small ghost' title='Limpar filtros' style='padding:5px 8px; font-size:11px;'>↺</button>
                </div>
                <div style='display:flex; gap:6px; margin-top:2px;'>
                  <button type='button' id='btnGeocodeAll' class='btn small' style='flex:1; background:#0284c7; border-color:#0284c7; color:#fff; font-size:0.75rem; font-weight:700; padding:5px 8px; display:flex; align-items:center; justify-content:center; gap:4px;' title='Localizar pedidos sem GPS automaticamente usando a API do Google Maps'>
                    ⚡ Geocodificar sem GPS (Google)
                  </button>
                  <button type='button' id='btnSidebarLocateMe' class='btn small ghost' style='background:#f0f9ff; border:1px solid #bae6fd; color:#0369a1; font-size:0.75rem; font-weight:700; padding:5px 8px; display:flex; align-items:center; justify-content:center; gap:4px;' title='Centralizar no meu GPS atual'>
                    🎯 Minha Posição
                  </button>
                </div>
                <div id='geocodeFeedbackBox' style='display:none; font-size:11px; padding:6px 8px; border-radius:4px; font-weight:600;'></div>
              </div>

              <!-- Banner de Ação de Ponto GPS / Fixação -->
              <div id='selectedOrderBanner' class='alert info' style='display:none; padding:7px 9px; font-size:0.78rem; margin:0; background:#eff6ff; border-color:#bfdbfe; color:#1e40af;'>
                <div id='selectedOrderText'>Selecione um pedido abaixo para marcar a localização.</div>
                <div style='display:flex; gap:6px; margin-top:5px;'>
                  <button type='button' id='btnSavePinAction' class='btn small' style='flex:1; font-weight:700; background:#0284c7; color:#ffffff; padding:4px 8px; font-size:11px;' disabled>📍 Fixar Ponto</button>
                  <button type='button' id='btnCancelPinAction' class='btn small ghost' style='padding:4px 6px; font-size:11px;'>Cancelar</button>
                </div>
              </div>

              <!-- Lista Rolável de Pedidos com Checkbox e Detalhes -->
              <div id='mapOrdersList' style='flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:5px; padding-right:2px; min-height:100px;'>
                <div class='empty' style='padding:16px 0; font-size:0.85rem;'>Carregando pedidos em andamento...</div>
              </div>
            </div>

            <!-- ================= ABA 2: MONTAGEM DE CARGA ================= -->
            <div id='tabContentDraft' style='display:none; flex-direction:column; gap:8px; flex:1; min-height:0;'>
              <!-- Resumo da Carga em Montagem -->
              <div style='background:#fffbeb; border:1px solid #fde68a; border-radius:8px; padding:8px 10px;'>
                <div style='display:flex; justify-content:space-between; align-items:center;'>
                  <span style='font-size:0.82rem; font-weight:700; color:#92400e;'>🚚 Resumo da Nova Carga</span>
                  <span id='draftOrderCountLabel' class='badge ok' style='background:#f59e0b; color:#fff; font-size:11px;'>0 pedidos</span>
                </div>
                
                <div style='display:flex; justify-content:space-between; align-items:baseline; margin:4px 0 2px;'>
                  <small style='color:#78350f; font-weight:600;'>Peso Acumulado:</small>
                  <span style='font-size:0.88rem;'><strong id='draftTotalWeightText' style='color:#b45309;'>0</strong> / <span id='draftCapacityMaxText'>{default_capacity}</span> kg</span>
                </div>

                <div class='progress' style='height:8px; margin:4px 0 3px; background:#fef3c7;'>
                  <i id='draftCapacityBar' style='background:#f59e0b; width:0%; transition:width 0.3s ease;'></i>
                </div>
                <small id='draftCapacityAlert' style='font-size:11px; color:#92400e; font-weight:600;'>Selecione pedidos no mapa ou lista para preencher a carga.</small>
              </div>

              <!-- Dados e Formulário Rápido de Criação -->
              <div style='display:flex; flex-direction:column; gap:6px;'>
                <div style='display:grid; grid-template-columns: 1fr 1fr; gap:5px;'>
                  <div>
                    <label style='font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:2px;'>Nome da Carga</label>
                    <input type='text' id='draftName' class='form-control' value='{default_load_name}' style='padding:5px 8px; font-size:0.80rem; border:1px solid var(--border); border-radius:var(--radius-sm);'>
                  </div>
                  <div>
                    <label style='font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:2px;'>Data</label>
                    <input type='date' id='draftDate' class='form-control' value='{today()}' style='padding:5px 8px; font-size:0.80rem; border:1px solid var(--border); border-radius:var(--radius-sm);'>
                  </div>
                </div>

                <div style='display:grid; grid-template-columns: 1fr 1fr; gap:5px;'>
                  <div>
                    <label style='font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:2px;'>Motorista</label>
                    <select id='draftDriverSelect' class='form-control' style='padding:5px 8px; font-size:0.80rem; border:1px solid var(--border); border-radius:var(--radius-sm);'>
                      {driver_options}
                    </select>
                  </div>
                  <div>
                    <label style='font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:2px;'>Veículo</label>
                    <select id='draftVehicleSelect' class='form-control' style='padding:5px 8px; font-size:0.80rem; border:1px solid var(--border); border-radius:var(--radius-sm);'>
                      {vehicle_options}
                    </select>
                  </div>
                </div>

                <div style='display:grid; grid-template-columns: 1fr 1fr; gap:5px;'>
                  <div>
                    <label style='font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:2px;'>Rota</label>
                    <select id='draftRouteSelect' class='form-control' style='padding:5px 8px; font-size:0.80rem; border:1px solid var(--border); border-radius:var(--radius-sm);'>
                      {route_options}
                    </select>
                  </div>
                  <div>
                    <label style='font-size:11px; font-weight:700; color:#475569; display:block; margin-bottom:2px;'>Capacidade (kg)</label>
                    <input type='number' id='draftCapacityInput' class='form-control' value='{default_capacity}' style='padding:5px 8px; font-size:0.80rem; border:1px solid var(--border); border-radius:var(--radius-sm);'>
                  </div>
                </div>
              </div>

              <!-- Lista de Paradas Selecionadas na Carga -->
              <div style='display:flex; justify-content:space-between; align-items:center; margin-top:2px;'>
                <span style='font-size:11px; font-weight:700; color:#64748b;'>Paradas da Carga (<span id='draftStopCount'>0</span>):</span>
                <button type='button' id='btnClearDraft' class='btn small ghost' style='font-size:11px; padding:2px 6px; color:#dc2626;'>🗑️ Limpar Seleção</button>
              </div>

              <div id='draftStopsList' style='flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:4px; padding-right:2px; min-height:80px;'>
                <div class='empty' style='padding:16px 0; font-size:0.82rem;'>Nenhum pedido adicionado à carga.<br><small>Clique nos pedidos ou marcadores do mapa para incluir.</small></div>
              </div>

              <!-- Botão de Ação: Criar Carga -->
              <button type='button' id='btnSubmitDraftLoad' class='btn primary' style='width:100%; font-weight:800; padding:10px; background:#166534; border-color:#166534; color:#fff; font-size:0.88rem; box-shadow:0 3px 8px rgba(22,101,52,0.3);' disabled>
                🚚 Confirmar e Gerar Carga
              </button>
            </div>

          </div>

          <!-- Canvas Interativo Leaflet com Botão Flutuante de Maximizar -->
          <div id='mapViewportPanel' class='map-viewport-panel' style='position:relative; background:#e2e8f0; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; box-shadow:var(--shadow-sm); height:100%;'>
            
            <!-- Botão Flutuante de Alternar Painel Lateral -->
            <button type='button' id='btnFloatToggleSidebar' class='btn small ghost' style='position:absolute; top:12px; left:52px; z-index:1000; background:rgba(255,255,255,0.94); backdrop-filter:blur(4px); box-shadow:0 2px 6px rgba(0,0,0,0.15); border:1px solid var(--border); font-size:12px; font-weight:700; padding:6px 10px;'>
              ☰ Painel Lateral
            </button>

            <!-- Botão Flutuante: Onde estou agora (GPS em tempo real) -->
            <button type='button' id='btnLocateMe' class='btn small' style='position:absolute; top:12px; left:165px; z-index:1000; background:#0284c7; color:#ffffff; border:none; box-shadow:0 2px 8px rgba(2,132,199,0.4); font-size:12px; font-weight:700; padding:6px 12px; border-radius:6px; display:flex; align-items:center; gap:6px; cursor:pointer;' title='Buscar localização GPS do seu dispositivo e centralizar o mapa'>
              <span id='locateIcon' style='font-size:14px;'>🎯</span>
              <span id='locateText'>Onde estou agora</span>
            </button>

            <!-- Banner Informativo para Redirecionamento HTTPS quando acessado via IP em HTTP -->
            <div id='httpsGpsBanner' style='display:none; position:absolute; top:48px; left:52px; right:52px; z-index:1001; background:#fffbeb; border:1px solid #fde68a; border-radius:8px; padding:8px 12px; font-size:12.5px; color:#92400e; box-shadow:0 3px 10px rgba(0,0,0,0.12); align-items:center; justify-content:space-between; gap:10px;'>
              <div style='display:flex; align-items:center; gap:8px;'>
                <span style='font-size:18px;'>🔒</span>
                <span><b>Conexão HTTP (Porta 3000):</b> O Chrome bloqueia a pergunta de permissão de GPS em rede local HTTP. Para solicitar permissão diretamente, use HTTPS.</span>
              </div>
              <button type='button' id='btnBannerGoHttps' class='btn small' style='background:#166534; color:#fff; font-weight:700; border:none; padding:6px 12px; border-radius:6px; white-space:nowrap; cursor:pointer;'>
                Mudar para HTTPS (Porta 3443) e Ativar GPS
              </button>
            </div>

            <!-- Canvas do Mapa -->
            <div id='interactiveMap' style='width:100%; height:100%; z-index:1;'></div>
            
            <!-- Barra Flutuante de Legenda (Compacta) -->
            <div class='map-legend-bar' style='position:absolute; bottom:12px; left:12px; z-index:1000; background:rgba(255,255,255,0.94); backdrop-filter:blur(4px); border:1px solid var(--border); border-radius:var(--radius-sm); padding:4px 10px; font-size:0.74rem; box-shadow:0 2px 8px rgba(0,0,0,0.12); display:flex; gap:8px; align-items:center; flex-wrap:wrap;'>
              <span><b>Legenda:</b></span>
              <span><i style='display:inline-block; width:8px; height:8px; border-radius:50%; background:#dc2626;'></i> Vencido</span>
              <span><i style='display:inline-block; width:8px; height:8px; border-radius:50%; background:#d97706;'></i> Risco</span>
              <span><i style='display:inline-block; width:8px; height:8px; border-radius:50%; background:#16a34a;'></i> No Prazo</span>
              <span><i style='display:inline-block; width:8px; height:8px; border-radius:50%; background:#2563eb;'></i> Agendado</span>
              <span><i style='display:inline-block; width:8px; height:8px; border-radius:50%; background:#9333ea;'></i> Em Rota</span>
              <span><i style='display:inline-block; width:8px; height:8px; border-radius:50%; background:#f59e0b;'></i> Na Carga</span>
            </div>
          </div>
        <style>
        @keyframes pulseGeo {{
          0% {{ transform: scale(0.6); opacity: 0.95; }}
          100% {{ transform: scale(2.2); opacity: 0; }}
        }}
        .my-location-pulse-marker {{
          background: transparent !important;
          border: none !important;
        }}
        </style>

        <script>
        function initDeliveryMapPage() {{
          var mapElement = document.getElementById('interactiveMap');
          if (!mapElement) return;
          if (!window.L) {{
            setTimeout(initDeliveryMapPage, 100);
            return;
          }}
          if (mapElement._leaflet_id) return;

          // 1. Inicializar Mapa centrado na região operacional
          var map = L.map('interactiveMap', {{
            center: [-17.8572, -41.5055],
            zoom: 9,
            zoomControl: true
          }});

          // 2. Camadas Oficiais do Google Maps (Alta Resolução)
          var googleHybrid = L.tileLayer('https://{{s}}.google.com/vt/lyrs=y&x={{x}}&y={{y}}&z={{z}}', {{
            maxZoom: 20,
            subdomains: ['mt0', 'mt1', 'mt2', 'mt3'],
            attribution: '© Google Maps'
          }});

          var googleStreets = L.tileLayer('https://{{s}}.google.com/vt/lyrs=m&x={{x}}&y={{y}}&z={{z}}', {{
            maxZoom: 20,
            subdomains: ['mt0', 'mt1', 'mt2', 'mt3'],
            attribution: '© Google Maps'
          }});

          var googleTerrain = L.tileLayer('https://{{s}}.google.com/vt/lyrs=p&x={{x}}&y={{y}}&z={{z}}', {{
            maxZoom: 20,
            subdomains: ['mt0', 'mt1', 'mt2', 'mt3'],
            attribution: '© Google Maps'
          }});

          // Camadas Livres / Alternativas (ESRI / OpenStreetMap)
          var streetLayer = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
            maxZoom: 19,
            attribution: '© Esri & OpenStreetMap'
          }});

          var esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
            maxZoom: 19,
            attribution: 'Tiles © Esri'
          }});
          var esriLabels = L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{{z}}/{{y}}/{{x}}', {{
            maxZoom: 19,
            attribution: 'Labels © Esri'
          }});
          var satelliteLayerGroup = L.layerGroup([esriSat, esriLabels]);

          var osmLayer = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            maxZoom: 19,
            subdomains: ['a', 'b', 'c'],
            attribution: '© OpenStreetMap'
          }});

          // Ativa Satélite Híbrido Google como camada padrão (ideal para o agro e entregas rurais)
          googleHybrid.addTo(map);

          L.control.layers({{
            "🛰️ Google Satélite Híbrido (Vias e Nomes)": googleHybrid,
            "🗺️ Google Ruas": googleStreets,
            "⛰️ Google Terreno": googleTerrain,
            "🗺️ Ruas & Cidades (ESRI)": streetLayer,
            "🛰️ Satélite Alternativo (ESRI)": satelliteLayerGroup,
            "🌍 OpenStreetMap Padrão": osmLayer
          }}, null, {{ position: 'topright', collapsed: false }}).addTo(map);

          setTimeout(function() {{ map.invalidateSize(); }}, 200);
          setTimeout(function() {{ map.invalidateSize(); }}, 600);
          window.addEventListener('resize', function() {{ map.invalidateSize(); }});

          // Camadas de elementos interativos
          var markersLayer = L.layerGroup().addTo(map);
          var draftRoutePolyline = null;
          var tempPinMarker = null;
          
          var allPoints = [];
          var currentFilter = 'all';
          var activeSelectedOrder = null;
          var tempClickedLatLng = null;

          // Estado da Carga em Montagem (Draft Load)
          var draftOrderIds = [];

          // 3. Alternância entre Abas e Recolhimento da Barra Lateral
          var tabBtnOrders = document.getElementById('tabBtnOrders');
          var tabBtnDraft = document.getElementById('tabBtnDraft');
          var tabContentOrders = document.getElementById('tabContentOrders');
          var tabContentDraft = document.getElementById('tabContentDraft');
          var sidebar = document.getElementById('mapSidebarPanel');
          var pageContainer = document.getElementById('mapPageContainer');
          var btnToggleSidebar = document.getElementById('btnToggleSidebar');
          var btnFloatToggleSidebar = document.getElementById('btnFloatToggleSidebar');

          function showTab(tabName) {{
            if (tabName === 'orders') {{
              tabBtnOrders.classList.add('active');
              tabBtnDraft.classList.remove('active');
              tabContentOrders.style.display = 'flex';
              tabContentDraft.style.display = 'none';
            }} else {{
              tabBtnDraft.classList.add('active');
              tabBtnOrders.classList.remove('active');
              tabContentDraft.style.display = 'flex';
              tabContentOrders.style.display = 'none';
            }}
          }}

          tabBtnOrders.addEventListener('click', function() {{ showTab('orders'); }});
          tabBtnDraft.addEventListener('click', function() {{ showTab('draft'); }});

          function toggleSidebar() {{
            var isHidden = (sidebar.style.display === 'none');
            if (isHidden) {{
              sidebar.style.display = 'flex';
              pageContainer.style.gridTemplateColumns = '360px 1fr';
              btnToggleSidebar.textContent = '◀';
            }} else {{
              sidebar.style.display = 'none';
              pageContainer.style.gridTemplateColumns = '1fr';
              btnToggleSidebar.textContent = '▶';
            }}
            setTimeout(function() {{ map.invalidateSize(); }}, 250);
          }}

          btnToggleSidebar.addEventListener('click', toggleSidebar);
          btnFloatToggleSidebar.addEventListener('click', toggleSidebar);

          // 4. Ícones do Mapa
          function getMarkerIcon(color, seqNumber, isDraft, isRuralVerified) {{
            if (isDraft) {{
              var ring = isRuralVerified ? 'border:2px solid #ffffff; box-shadow:0 0 0 3px rgba(22,163,74,0.7), 0 3px 6px rgba(0,0,0,0.35);' : 'border:2px dashed #b45309; box-shadow:0 0 0 3px rgba(245,158,11,0.5), 0 3px 6px rgba(0,0,0,0.35);';
              var bg = isRuralVerified ? '#16a34a' : '#f59e0b';
              return L.divIcon({{
                className: 'draft-load-marker',
                html: '<div style="background-color:' + bg + '; color:#ffffff; font-weight:800; font-size:11px; width:26px; height:26px; border-radius:50%; ' + ring + ' display:flex; align-items:center; justify-content:center;">' + (seqNumber || '🚚') + '</div>',
                iconSize: [26, 26],
                iconAnchor: [13, 13]
              }});
            }}

            if (isRuralVerified) {{
              return L.divIcon({{
                className: 'custom-map-marker verified-rural-marker',
                html: '<div style="background-color:' + color + '; width:20px; height:20px; border-radius:50%; border:2px solid #ffffff; box-shadow:0 0 0 2px #16a34a, 0 2px 5px rgba(0,0,0,0.35); display:flex; align-items:center; justify-content:center; color:#fff; font-size:9px; font-weight:800;" title="GPS Rural Confirmado (Fazenda)">✓</div>',
                iconSize: [22, 22],
                iconAnchor: [11, 11]
              }});
            }}

            return L.divIcon({{
              className: 'custom-map-marker approx-city-marker',
              html: '<div style="background-color:#fef3c7; color:#b45309; width:22px; height:22px; border-radius:50%; border:2px dashed #d97706; box-shadow:0 2px 5px rgba(0,0,0,0.3); display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:900;" title="Ponto Aproximado (Centro da Cidade - 1ª Entrega Rural)">⚠️</div>',
              iconSize: [22, 22],
              iconAnchor: [11, 11]
            }});
          }}

          function getTempPinIcon() {{
            return L.divIcon({{
              className: 'temp-pin-marker',
              html: '<div style="font-size:28px; line-height:1; filter:drop-shadow(0 3px 6px rgba(0,0,0,0.4)); animation:bouncePin 0.8s infinite alternate ease-in-out;">📍</div>',
              iconSize: [30, 30],
              iconAnchor: [15, 28]
            }});
          }}

          // 5. Carregamento dos Pontos da API
          function loadMapPoints() {{
            fetch('/api/map/points')
              .then(function(res) {{ return res.json(); }})
              .then(function(data) {{
                if (!data.ok) return;
                allPoints = data.points || [];
                updateSlaCounts(data.counts || {{}});
                renderPoints();
                renderDraftLoad();
              }})
              .catch(function(err) {{
                console.error('Erro ao carregar dados do mapa:', err);
              }});
          }}

          function updateSlaCounts(counts) {{
            document.getElementById('cntAll').textContent = allPoints.length;
            document.getElementById('cntLate').textContent = counts.late || 0;
            document.getElementById('cntRisk').textContent = counts.risk || 0;
            document.getElementById('cntOk').textContent = counts.ok || 0;
            document.getElementById('cntInRoute').textContent = counts.in_route || 0;
            document.getElementById('mapTotalBadge').textContent = allPoints.length;
            var ruralEl = document.getElementById('cntVerifiedRural');
            if (ruralEl) ruralEl.textContent = counts.verified_rural || 0;
            var approxEl = document.getElementById('cntCityApprox');
            if (approxEl) approxEl.textContent = counts.approx_city || 0;
          }}

          // 6. Gerenciamento da Carga em Montagem (Draft Load)
          window.toggleDraftOrder = function(orderId) {{
            orderId = parseInt(orderId);
            var idx = draftOrderIds.indexOf(orderId);
            if (idx >= 0) {{
              draftOrderIds.splice(idx, 1);
            }} else {{
              draftOrderIds.push(orderId);
            }}
            renderDraftLoad();
            renderPoints();
          }};

          function renderDraftLoad() {{
            var badge = document.getElementById('draftBadge');
            var countLabel = document.getElementById('draftOrderCountLabel');
            var stopCount = document.getElementById('draftStopCount');
            var weightText = document.getElementById('draftTotalWeightText');
            var capBar = document.getElementById('draftCapacityBar');
            var alertText = document.getElementById('draftCapacityAlert');
            var stopsList = document.getElementById('draftStopsList');
            var btnSubmit = document.getElementById('btnSubmitDraftLoad');
            var capInput = document.getElementById('draftCapacityInput');
            var capMax = parseFloat(capInput.value || 11000) || 11000;

            var draftOrders = allPoints.filter(function(p) {{ return draftOrderIds.indexOf(p.order_id) >= 0; }});
            
            badge.textContent = draftOrders.length;
            countLabel.textContent = draftOrders.length + ' pedidos';
            stopCount.textContent = draftOrders.length;

            var totalWeight = draftOrders.reduce(function(acc, o) {{ return acc + (parseFloat(o.weight_kg) || 0); }}, 0);
            weightText.textContent = totalWeight.toLocaleString('pt-BR', {{ minimumFractionDigits: 1, maximumFractionDigits: 1 }});

            var pct = Math.min(100, Math.round((totalWeight / capMax) * 100));
            capBar.style.width = pct + '%';

            if (totalWeight > capMax) {{
              capBar.style.background = '#dc2626';
              alertText.innerHTML = '<span style="color:#dc2626;">⚠️ Excesso de peso: +' + (totalWeight - capMax).toFixed(1) + ' kg além da capacidade!</span>';
              btnSubmit.disabled = true;
            }} else if (draftOrders.length > 0) {{
              capBar.style.background = '#16a34a';
              alertText.innerHTML = '<span style="color:#166534;">✓ Capacidade ocupada: ' + pct + '% (' + (capMax - totalWeight).toFixed(1) + ' kg disponíveis)</span>';
              btnSubmit.disabled = false;
            }} else {{
              capBar.style.background = '#f59e0b';
              alertText.textContent = 'Selecione pedidos no mapa ou lista para preencher a carga.';
              btnSubmit.disabled = true;
            }}

            // Renderizar paradas na aba 2
            if (draftOrders.length === 0) {{
              stopsList.innerHTML = '<div class="empty" style="padding:16px 0; font-size:0.82rem;">Nenhum pedido adicionado à carga.<br><small>Clique nos pedidos ou marcadores do mapa para incluir.</small></div>';
            }} else {{
              stopsList.innerHTML = '';
              draftOrders.forEach(function(o, seq) {{
                var item = document.createElement('div');
                item.className = 'draft-load-item';
                item.style.cssText = 'background:#f8fafc; border:1px solid #e2e8f0; border-radius:6px; padding:6px 8px; display:flex; justify-content:space-between; align-items:center; gap:6px; font-size:0.80rem;';
                item.innerHTML = '<div style="display:flex; align-items:center; gap:6px; min-width:0;">' +
                                 '<span style="background:#f59e0b; color:#fff; font-weight:800; font-size:10px; width:18px; height:18px; border-radius:50%; display:flex; align-items:center; justify-content:center; flex:none;">' + (seq + 1) + '</span>' +
                                 '<div style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">' +
                                 '<b>#' + o.order_number + '</b> · ' + o.client_name + '<br><small style="color:#64748b;">' + o.city + ' · ' + (o.weight_kg || 0) + ' kg</small>' +
                                 '</div></div>' +
                                 '<button type="button" onclick="toggleDraftOrder(' + o.order_id + ')" class="btn small ghost" style="padding:3px 6px; color:#dc2626; font-size:12px;" title="Remover da carga">✕</button>';
                stopsList.appendChild(item);
              }});
            }}

            // Atualizar traçado da linha polilinha da carga em montagem no mapa
            if (draftRoutePolyline) {{
              map.removeLayer(draftRoutePolyline);
              draftRoutePolyline = null;
            }}

            var draftCoords = [];
            draftOrders.forEach(function(o) {{
              if (o.has_gps && o.lat && o.lng) {{
                draftCoords.push([o.lat, o.lng]);
              }}
            }});

            if (draftCoords.length > 1) {{
              draftRoutePolyline = L.polyline(draftCoords, {{
                color: '#f59e0b',
                weight: 4,
                opacity: 0.9,
                dashArray: '8, 6'
              }}).addTo(map);
            }}
          }}

          // Atualizar capacidade ao mudar veículo
          document.getElementById('draftVehicleSelect').addEventListener('change', function(e) {{
            var opt = e.target.selectedOptions[0];
            if (opt && opt.dataset.capacity) {{
              var capVal = parseFloat(opt.dataset.capacity) || 11000;
              document.getElementById('draftCapacityInput').value = capVal;
              document.getElementById('draftCapacityMaxText').textContent = capVal.toLocaleString('pt-BR');
              renderDraftLoad();
            }}
          }});

          document.getElementById('draftCapacityInput').addEventListener('input', function() {{
            var capVal = parseFloat(this.value) || 11000;
            document.getElementById('draftCapacityMaxText').textContent = capVal.toLocaleString('pt-BR');
            renderDraftLoad();
          }});

          document.getElementById('btnClearDraft').addEventListener('click', function() {{
            draftOrderIds = [];
            renderDraftLoad();
            renderPoints();
          }});

          // 7. Submeter Criação de Carga
          document.getElementById('btnSubmitDraftLoad').addEventListener('click', function() {{
            if (draftOrderIds.length === 0) return;
            var driverId = document.getElementById('draftDriverSelect').value;
            var vehicleId = document.getElementById('draftVehicleSelect').value;
            var loadName = document.getElementById('draftName').value.trim();
            var loadDate = document.getElementById('draftDate').value;
            var routeName = document.getElementById('draftRouteSelect').value;
            var cap = document.getElementById('draftCapacityInput').value;

            if (!driverId) {{
              alert('Por favor, selecione o motorista responsável.');
              return;
            }}
            if (!vehicleId) {{
              alert('Por favor, selecione o veículo da carga.');
              return;
            }}

            var payload = {{
              name: loadName,
              date: loadDate,
              driver_id: driverId,
              vehicle_id: vehicleId,
              route_name: routeName,
              capacity: cap,
              order_ids: draftOrderIds
            }};

            function getCookie(name) {{
              var key = name + "=";
              var ca = document.cookie.split(';');
              for (var i = 0; i < ca.length; i++) {{
                var c = ca[i].trim();
                if (c.indexOf(key) === 0) return decodeURIComponent(c.substring(key.length, c.length));
              }}
              return '';
            }}

            var btn = document.getElementById('btnSubmitDraftLoad');
            btn.disabled = true;
            btn.textContent = '⏳ Criando Carga...';

            fetch('/api/routes/create-from-map', {{
              method: 'POST',
              headers: {{
                'Content-Type': 'application/json',
                'X-CSRF-Token': getCookie('csrf_token')
              }},
              body: JSON.stringify(payload)
            }})
            .then(function(res) {{ return res.json(); }})
            .then(function(data) {{
              if (data.ok) {{
                alert(data.message || 'Carga criada com sucesso!');
                if (data.redirect_url) {{
                  window.location.href = data.redirect_url;
                }} else {{
                  location.reload();
                }}
              }} else {{
                alert(data.message || 'Erro ao criar carga.');
                btn.disabled = false;
                btn.textContent = '🚚 Confirmar e Gerar Carga';
              }}
            }})
            .catch(function(err) {{
              alert('Erro de conexão ao criar carga. Tente novamente.');
              btn.disabled = false;
              btn.textContent = '🚚 Confirmar e Gerar Carga';
            }});
          }});

          // 8. Renderização dos Pontos e Pedidos
          function renderPoints() {{
            markersLayer.clearLayers();
            var searchQ = (document.getElementById('mapSearchInput').value || '').toLowerCase().trim();
            var routeQ = document.getElementById('mapRouteSelect').value;
            var cityQ = document.getElementById('mapCitySelect').value;
            var statusQ = document.getElementById('mapStatusSelect').value;
            var listContainer = document.getElementById('mapOrdersList');
            listContainer.innerHTML = '';

            var filtered = allPoints.filter(function(p) {{
              if (currentFilter !== 'all' && p.sla_category !== currentFilter) return false;
              if (routeQ && p.route_name !== routeQ) return false;
              if (cityQ && p.city !== cityQ) return false;
              if (statusQ && p.status !== statusQ) return false;
              if (searchQ) {{
                var hay = (p.order_number + ' ' + p.client_name + ' ' + (p.farm_name || '') + ' ' + p.city + ' ' + (p.delivery_address || '')).toLowerCase();
                if (hay.indexOf(searchQ) === -1) return false;
              }}
              return true;
            }});

            if (filtered.length === 0) {{
              listContainer.innerHTML = '<div class="empty" style="padding:16px 0; font-size:0.82rem;">Nenhum pedido encontrado com os filtros aplicados.</div>';
              return;
            }}

            var bounds = [];

            filtered.forEach(function(p) {{
              var inDraft = (draftOrderIds.indexOf(p.order_id) >= 0);
              var draftSeq = inDraft ? (draftOrderIds.indexOf(p.order_id) + 1) : null;

              // Card na Lista Lateral
              var card = document.createElement('div');
              card.className = 'map-order-item' + (inDraft ? ' selected-in-draft' : '');
              card.dataset.orderId = p.order_id;
              card.style.cssText = 'background:#ffffff; border:1px solid ' + (inDraft ? '#f59e0b' : '#e2e8f0') + '; border-radius:8px; padding:8px 10px; display:flex; flex-direction:column; gap:4px; font-size:0.80rem;';

              var gpsBadge = p.is_rural_verified ? 
                '<span style="color:#16a34a; font-weight:700; font-size:10px; background:#dcfce7; padding:2px 5px; border-radius:4px; border:1px solid #86efac;">✅ GPS Rural</span>' : 
                (p.has_gps ? 
                  '<span style="color:#d97706; font-weight:700; font-size:10px; background:#fef3c7; padding:2px 5px; border-radius:4px; border:1px solid #fcd34d;">⚠️ Centro Cidade</span>' : 
                  '<span style="color:#dc2626; font-weight:700; font-size:10px; background:#fee2e2; padding:2px 5px; border-radius:4px; border:1px solid #fca5a5;">❌ Sem GPS</span>');

              card.innerHTML = 
                '<div style="display:flex; justify-content:space-between; align-items:flex-start; gap:6px;">' +
                  '<label style="display:flex; align-items:center; gap:6px; cursor:pointer; min-width:0; flex:1;">' +
                    '<input type="checkbox" ' + (inDraft ? 'checked' : '') + ' onchange="toggleDraftOrder(' + p.order_id + ')" style="width:16px; height:16px; cursor:pointer; accent-color:#f59e0b;">' +
                    '<div style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">' +
                      '<b>#' + p.order_number + '</b> · ' + p.client_name +
                    '</div>' +
                  '</label>' +
                  '<span class="badge" style="background:' + p.color + '20; color:' + p.color + '; border:1px solid ' + p.color + '40; font-size:10px; padding:2px 5px; flex:none;">' + p.sla_label + '</span>' +
                '</div>' +
                (p.farm_name ? '<div style="font-size:11px; color:#1e293b; font-weight:600;">🌾 ' + p.farm_name + '</div>' : '') +
                '<div style="display:flex; justify-content:space-between; color:#64748b; font-size:11px;">' +
                  '<span>' + (p.city || 'Sem cidade') + (p.route_name ? ' (' + p.route_name + ')' : '') + '</span>' +
                  '<span><b>' + (p.weight_kg || 0) + ' kg</b> · ' + gpsBadge + '</span>' +
                '</div>' +
                (p.reference_point ? '<div style="font-size:10.5px; color:#64748b; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">Ref: ' + p.reference_point + '</div>' : '') +
                '<div style="display:flex; gap:6px; margin-top:2px;">' +
                  '<button type="button" class="btn small ghost btn-fix-pin" style="flex:1; padding:3px 6px; font-size:11px;" title="Marcar ou mover localização exata no mapa">📍 ' + (p.is_rural_verified ? 'Ajustar GPS' : 'Posicionar Fazenda') + '</button>' +
                  '<button type="button" class="btn small ghost btn-toggle-draft" style="flex:1; padding:3px 6px; font-size:11px; ' + (inDraft ? 'color:#dc2626;' : 'color:#166534; font-weight:700;') + '">' + (inDraft ? '➖ Remover Carga' : '🚚 + Carga') + '</button>' +
                '</div>';

              card.querySelector('.btn-fix-pin').addEventListener('click', function(e) {{
                e.stopPropagation();
                selectOrderForPinning(p);
              }});

              card.querySelector('.btn-toggle-draft').addEventListener('click', function(e) {{
                e.stopPropagation();
                toggleDraftOrder(p.order_id);
              }});

              card.addEventListener('click', function() {{
                if (p.has_gps && p.lat && p.lng) {{
                  map.flyTo([p.lat, p.lng], 15, {{ duration: 1.0 }});
                }} else {{
                  selectOrderForPinning(p);
                }}
              }});

              listContainer.appendChild(card);

              // Adicionar Marcador no Mapa (se tiver GPS)
              if (p.has_gps && p.lat && p.lng) {{
                var markerIcon = getMarkerIcon(p.color, draftSeq, inDraft, p.is_rural_verified);
                var marker = L.marker([p.lat, p.lng], {{ icon: markerIcon }}).addTo(markersLayer);
                
                var draftBtnHtml = inDraft ?
                  '<button onclick="toggleDraftOrder(' + p.order_id + ')" class="btn small ghost" style="width:100%; color:#dc2626; border-color:#fca5a5; font-size:11px; font-weight:700; padding:5px;">➖ Remover da Carga</button>' :
                  '<button onclick="toggleDraftOrder(' + p.order_id + ')" class="btn small" style="width:100%; background:#f59e0b; color:#fff; border-color:#f59e0b; font-size:11px; font-weight:700; padding:5px;">🚚 + Adicionar à Carga</button>';

                var ruralBadge = p.is_rural_verified ?
                  '<div style="background:#dcfce7; color:#166534; border:1px solid #86efac; border-radius:4px; padding:3px 6px; font-size:10.5px; font-weight:700; margin-bottom:5px;">✅ GPS Fazenda Confirmado</div>' :
                  '<div style="background:#fef3c7; color:#92400e; border:1px solid #fcd34d; border-radius:4px; padding:3px 6px; font-size:10.5px; font-weight:700; margin-bottom:5px;">⚠️ Centro da Cidade (1ª Entrega Rural)</div>';

                var farmLine = p.farm_name ? '<div style="font-size:12px; color:#1e293b; font-weight:600;">🌾 ' + p.farm_name + '</div>' : '';
                var refLine = p.reference_point ? '<div style="font-size:11px; color:#475569; background:#f8fafc; padding:3px 6px; border-radius:4px; margin:3px 0;"><b>Ponto de Referência:</b> ' + p.reference_point + '</div>' : '';

                var popupContent = 
                  '<div style="font-family:system-ui,sans-serif; min-width:220px;">' +
                    '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:3px;">' +
                      '<b style="font-size:13px;">Pedido #' + p.order_number + '</b>' +
                      '<span class="badge" style="background:' + p.color + '20; color:' + p.color + '; font-size:10px; padding:1px 5px;">' + p.sla_label + '</span>' +
                    '</div>' +
                    ruralBadge +
                    '<div style="font-size:12px; color:#166534; font-weight:600;">' + p.client_name + '</div>' +
                    farmLine +
                    '<div style="font-size:11px; color:#64748b; margin:2px 0 4px;">' + (p.city || '') + ' · ' + (p.weight_kg || 0) + ' kg</div>' +
                    refLine +
                    '<div style="display:flex; flex-direction:column; gap:5px; margin-top:8px;">' +
                      draftBtnHtml +
                      '<button onclick="triggerPinOrder(' + p.order_id + ')" class="btn small ghost" style="width:100%; font-size:11px; padding:5px; font-weight:600;">📍 ' + (p.is_rural_verified ? 'Ajustar Localização GPS' : 'Posicionar Fazenda no Mapa') + '</button>' +
                    '</div>' +
                  '</div>';

                marker.bindPopup(popupContent);
                bounds.push([p.lat, p.lng]);
              }}
            }});

            if (bounds.length > 0 && !activeSelectedOrder) {{
              map.fitBounds(bounds, {{ padding: [30, 30], maxZoom: 14 }});
            }}
          }}

          // 9. Fluxo de Fixação de Localização no Mapa
          window.triggerPinOrder = function(orderId) {{
            var order = allPoints.find(function(p) {{ return p.order_id == orderId; }});
            if (order) selectOrderForPinning(order);
          }};

          function selectOrderForPinning(order) {{
            showTab('orders');
            activeSelectedOrder = order;
            tempClickedLatLng = null;

            if (tempPinMarker) {{
              map.removeLayer(tempPinMarker);
              tempPinMarker = null;
            }}

            var banner = document.getElementById('selectedOrderBanner');
            var bannerTxt = document.getElementById('selectedOrderText');
            var btnSave = document.getElementById('btnSavePinAction');
            banner.style.display = 'block';

            bannerTxt.innerHTML = '👉 <b>Pedido #' + order.order_number + '</b> (' + order.client_name + ') selecionado.<br>Clique no local exato do mapa para fixar a entrega.';
            btnSave.disabled = true;
            btnSave.textContent = '📍 Clique no Mapa';
            btnSave.style.background = '#0284c7';

            if (order.has_gps && order.lat && order.lng) {{
              map.flyTo([order.lat, order.lng], 15, {{ duration: 1.0 }});
            }}
          }}

          document.getElementById('btnCancelPinAction').addEventListener('click', function() {{
            activeSelectedOrder = null;
            tempClickedLatLng = null;
            if (tempPinMarker) {{
              map.removeLayer(tempPinMarker);
              tempPinMarker = null;
            }}
            document.getElementById('selectedOrderBanner').style.display = 'none';
          }});

          // Clique no mapa para posicionar marcador temporário de fixação
          map.on('click', function(e) {{
            if (!activeSelectedOrder) return;

            var lat = e.latlng.lat;
            var lng = e.latlng.lng;
            tempClickedLatLng = {{ lat: lat, lng: lng }};

            if (tempPinMarker) {{
              tempPinMarker.setLatLng([lat, lng]);
            }} else {{
              tempPinMarker = L.marker([lat, lng], {{
                icon: getTempPinIcon(),
                draggable: true
              }}).addTo(map);

              tempPinMarker.on('dragend', function(ev) {{
                var pos = ev.target.getLatLng();
                tempClickedLatLng = {{ lat: pos.lat, lng: pos.lng }};
              }});
            }}

            var btnSave = document.getElementById('btnSavePinAction');
            btnSave.disabled = false;
            btnSave.textContent = '✓ Salvar Ponto no Pedido #' + activeSelectedOrder.order_number;
            btnSave.style.background = '#16a34a';
          }});

          document.getElementById('btnSavePinAction').addEventListener('click', function() {{
            if (!activeSelectedOrder || !tempClickedLatLng) return;
            saveLocation(activeSelectedOrder.order_id, activeSelectedOrder.client_id, tempClickedLatLng.lat, tempClickedLatLng.lng, activeSelectedOrder.order_number);
          }});

          function saveLocation(orderId, clientId, lat, lng, orderNumber) {{
            function getCookie(name) {{
              var key = name + "=";
              var ca = document.cookie.split(';');
              for (var i = 0; i < ca.length; i++) {{
                var c = ca[i].trim();
                if (c.indexOf(key) === 0) return decodeURIComponent(c.substring(key.length, c.length));
              }}
              return '';
            }}

            var payload = {{
              order_id: orderId,
              client_id: clientId,
              lat: lat,
              lng: lng
            }};

            fetch('/api/map/save-location', {{
              method: 'POST',
              headers: {{
                'Content-Type': 'application/json',
                'X-CSRF-Token': getCookie('csrf_token')
              }},
              body: JSON.stringify(payload)
            }})
            .then(function(res) {{ return res.json(); }})
            .then(function(data) {{
              if (data.ok) {{
                if (tempPinMarker) {{
                  map.removeLayer(tempPinMarker);
                  tempPinMarker = null;
                }}
                activeSelectedOrder = null;
                tempClickedLatLng = null;
                document.getElementById('selectedOrderBanner').style.display = 'none';
                loadMapPoints();
              }} else {{
                alert(data.message || 'Erro ao salvar localização.');
              }}
            }})
            .catch(function(err) {{
              alert('Erro ao salvar localização.');
            }});
          }}

          // 10. Filtros de SLA e Busca
          document.querySelectorAll('.map-sla-pill').forEach(function(pill) {{
            pill.addEventListener('click', function() {{
              document.querySelectorAll('.map-sla-pill').forEach(function(p) {{ p.classList.remove('active-filter'); }});
              pill.classList.add('active-filter');
              currentFilter = pill.dataset.filter || 'all';
              renderPoints();
            }});
          }});

          document.getElementById('mapSearchInput').addEventListener('input', renderPoints);
          document.getElementById('mapRouteSelect').addEventListener('change', renderPoints);
          document.getElementById('mapCitySelect').addEventListener('change', renderPoints);
          document.getElementById('mapStatusSelect').addEventListener('change', renderPoints);
          document.getElementById('btnResetFilters').addEventListener('click', function() {{
            document.getElementById('mapSearchInput').value = '';
            document.getElementById('mapRouteSelect').value = '';
            document.getElementById('mapCitySelect').value = '';
            document.getElementById('mapStatusSelect').value = '';
            currentFilter = 'all';
            document.querySelectorAll('.map-sla-pill').forEach(function(p) {{ p.style.outline = 'none'; }});
            renderPoints();
          }});

          var btnGeocodeAll = document.getElementById('btnGeocodeAll');
          var geocodeFeedback = document.getElementById('geocodeFeedbackBox');

          if (btnGeocodeAll) {{
            btnGeocodeAll.addEventListener('click', function() {{
              btnGeocodeAll.disabled = true;
              btnGeocodeAll.textContent = '⏳ Localizando...';
              if (geocodeFeedback) {{
                geocodeFeedback.style.display = 'block';
                geocodeFeedback.style.background = '#eff6ff';
                geocodeFeedback.style.color = '#1e40af';
                geocodeFeedback.style.border = '1px solid #bfdbfe';
                geocodeFeedback.textContent = 'Consultando Google Geocoding API para pedidos sem GPS...';
              }}

              fetch('/api/map/geocode-auto', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{}})
              }})
              .then(function(res) {{ return res.json(); }})
              .then(function(data) {{
                btnGeocodeAll.disabled = false;
                btnGeocodeAll.textContent = '⚡ Geocodificar sem GPS (Google)';
                if (data.ok) {{
                  if (geocodeFeedback) {{
                    geocodeFeedback.style.background = '#dcfce7';
                    geocodeFeedback.style.color = '#166534';
                    geocodeFeedback.style.border = '1px solid #86efac';
                    geocodeFeedback.textContent = data.message || 'Geocodificação concluída com sucesso!';
                  }}
                  loadMapPoints();
                }} else {{
                  if (geocodeFeedback) {{
                    geocodeFeedback.style.background = '#fee2e2';
                    geocodeFeedback.style.color = '#991b1b';
                    geocodeFeedback.style.border = '1px solid #fca5a5';
                    geocodeFeedback.textContent = data.message || 'Falha ao geocodificar.';
                  }}
                }}
              }})
              .catch(function(err) {{
                btnGeocodeAll.disabled = false;
                btnGeocodeAll.textContent = '⚡ Geocodificar sem GPS (Google)';
                if (geocodeFeedback) {{
                  geocodeFeedback.style.background = '#fee2e2';
                  geocodeFeedback.style.color = '#991b1b';
                  geocodeFeedback.style.border = '1px solid #fca5a5';
                  geocodeFeedback.textContent = 'Erro ao conectar: ' + err.message;
                }}
              }});
            }});
          }}

          // 11. Localização Atual do Usuário (GPS ao vivo)
          var btnLocateMe = document.getElementById('btnLocateMe');
          var btnSidebarLocateMe = document.getElementById('btnSidebarLocateMe');
          var locateText = document.getElementById('locateText');
          var locateIcon = document.getElementById('locateIcon');
          var userLocationMarker = null;
          var userLocationCircle = null;

          function locateUserNow() {{
            // 1. Se estiver em HTTP na rede local (não localhost), navegadores modernos bloqueiam o prompt de permissão
            if (!window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {{
              var goHttps = confirm([
                '🔒 Para que o navegador solicite a sua permissão de GPS diretamente, é necessário acessar via HTTPS (Porta 3443).',
                '',
                'No modo HTTP comum (Porta 3000), o Google Chrome e o Edge bloqueiam a pergunta de permissão por segurança.',
                '',
                'Deseja alternar agora para https://' + location.hostname + ':3443/map para ativar o GPS?'
              ].join('\\n'));
              if (goHttps) {{
                window.location.href = 'https://' + location.hostname + ':3443' + location.pathname + (location.search ? location.search + '&locate=1' : '?locate=1');
              }}
              return;
            }}

            if (!navigator.geolocation) {{
              alert('A geolocalização não é suportada ou está desativada no seu navegador.');
              return;
            }}

            if (navigator.permissions && navigator.permissions.query) {{
              navigator.permissions.query({{ name: 'geolocation' }}).then(function(perm) {{
                if (perm.state === 'denied') {{
                  alert([
                    '⚠️ A permissão de localização foi bloqueada nas configurações do navegador.',
                    '',
                    'Para reativar a pergunta de permissão:',
                    '1. Clique no ícone de opções/cadeado ao lado de ' + location.host + ' na barra de endereços;',
                    '2. Redefina a permissão de "Localização" para "Permitir" ou "Perguntar";',
                    '3. Recarregue a página.'
                  ].join('\\n'));
                }}
              }}).catch(function() {{}});
            }}

            if (btnLocateMe) btnLocateMe.disabled = true;
            if (btnSidebarLocateMe) btnSidebarLocateMe.disabled = true;
            if (locateText) locateText.textContent = 'Obtendo GPS...';
            if (locateIcon) locateIcon.textContent = '⏳';

            function applyUserPosition(lat, lng, acc, sourceLabel) {{
              map.flyTo([lat, lng], 17, {{ duration: 1.2 }});

              if (userLocationMarker) {{
                map.removeLayer(userLocationMarker);
              }}
              if (userLocationCircle) {{
                map.removeLayer(userLocationCircle);
              }}

              userLocationCircle = L.circle([lat, lng], {{
                radius: Math.min(acc || 25, 300),
                color: '#0284c7',
                fillColor: '#38bdf8',
                fillOpacity: 0.18,
                weight: 1.5
              }}).addTo(map);

              var pulseIcon = L.divIcon({{
                className: 'my-location-pulse-marker',
                html: '<div style="position:relative; width:22px; height:22px; background:#0284c7; border:3px solid #ffffff; border-radius:50%; box-shadow:0 0 14px rgba(2,132,199,0.9); display:flex; align-items:center; justify-content:center;"><div style="width:6px; height:6px; background:#ffffff; border-radius:50%;"></div><div style="position:absolute; width:44px; height:44px; border-radius:50%; border:2px solid #0284c7; animation:pulseGeo 1.6s infinite ease-out; opacity:0.8; pointer-events:none; top:-14px; left:-14px;"></div></div>',
                iconSize: [22, 22],
                iconAnchor: [11, 11]
              }});

              userLocationMarker = L.marker([lat, lng], {{ icon: pulseIcon }}).addTo(map);
              userLocationMarker.bindPopup(
                '<div style="min-width:190px; padding:2px;">' +
                '<b style="color:#0284c7; font-size:13px;">🎯 Sua Posição Atual (' + (sourceLabel || 'GPS') + ')</b><br>' +
                '<div style="font-size:12px; color:#334155; margin:4px 0;">' +
                'Lat: <b>' + lat.toFixed(6) + '</b><br>' +
                'Lng: <b>' + lng.toFixed(6) + '</b><br>' +
                '<small style="color:#64748b;">Precisão do sinal: ±' + Math.round(acc) + ' metros</small>' +
                '</div>' +
                '<a href="https://www.google.com/maps?q=' + lat.toFixed(6) + ',' + lng.toFixed(6) + '" target="_blank" rel="noopener" style="display:inline-block; font-size:11px; font-weight:700; color:#0284c7; text-decoration:none;">🗺️ Abrir no Google Maps ↗</a>' +
                '</div>'
              ).openPopup();

              if (locateText) locateText.textContent = 'Você está aqui!';
              if (locateIcon) locateIcon.textContent = '🎯';
              if (btnLocateMe) btnLocateMe.disabled = false;
              if (btnSidebarLocateMe) btnSidebarLocateMe.disabled = false;

              setTimeout(function() {{
                if (locateText) locateText.textContent = 'Onde estou agora';
              }}, 4500);
            }}

            function handleGeoError(err) {{
              if (btnLocateMe) btnLocateMe.disabled = false;
              if (btnSidebarLocateMe) btnSidebarLocateMe.disabled = false;
              if (locateText) locateText.textContent = 'Onde estou agora';
              if (locateIcon) locateIcon.textContent = '🎯';
              var msg = 'Não foi possível obter a sua localização GPS.';
              if (err && err.code === 1) {{
                if (!window.isSecureContext) {{
                  msg = 'O navegador bloqueou a permissão de GPS porque a conexão atual é HTTP (Porta 3000). Use HTTPS na porta 3443 para solicitar a permissão nativa.';
                }} else {{
                  msg = 'Permissão de localização negada pelo navegador. Permita o acesso clicando no ícone de configurações ao lado de ' + location.host + ' na barra de endereços.';
                }}
              }} else if (err && err.code === 2) {{
                msg = 'Sinal de GPS/localização indisponível no dispositivo. Ative o GPS/Localização no Windows ou no aparelho.';
              }} else if (err && err.code === 3) {{
                msg = 'Tempo limite esgotado ao buscar localização GPS.';
              }}
              alert('📍 ' + msg);
            }}

            function fallbackLowAccuracy() {{
              navigator.geolocation.getCurrentPosition(
                function(pos) {{
                  applyUserPosition(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy || 50, 'Rede/Wi-Fi');
                }},
                function(err) {{
                  fetch('/api/v1/driver/auto-location')
                    .then(function(r) {{ return r.json(); }})
                    .then(function(d) {{
                      if (d && d.ok && d.latitude && d.longitude) {{
                        applyUserPosition(parseFloat(d.latitude), parseFloat(d.longitude), 150, 'Estimada (Rede)');
                      }} else {{
                        handleGeoError(err);
                      }}
                    }})
                    .catch(function() {{
                      handleGeoError(err);
                    }});
                }},
                {{ enableHighAccuracy: false, timeout: 15000, maximumAge: 300000 }}
              );
            }}

            navigator.geolocation.getCurrentPosition(
              function(pos) {{
                applyUserPosition(pos.coords.latitude, pos.coords.longitude, pos.coords.accuracy || 20, 'GPS');
              }},
              function(err) {{
                if (err && err.code === 1) {{
                  handleGeoError(err);
                }} else {{
                  fallbackLowAccuracy();
                }}
              }},
              {{
                enableHighAccuracy: true,
                timeout: 25000,
                maximumAge: 5000
              }}
            );
          }}

          if (btnLocateMe) btnLocateMe.addEventListener('click', locateUserNow);
          if (btnSidebarLocateMe) btnSidebarLocateMe.addEventListener('click', locateUserNow);

          // Verificar banner de HTTP na rede local
          if (!window.isSecureContext && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {{
            var b = document.getElementById('httpsGpsBanner');
            if (b) b.style.display = 'flex';
          }}
          var btnBannerGoHttps = document.getElementById('btnBannerGoHttps');
          if (btnBannerGoHttps) {{
            btnBannerGoHttps.addEventListener('click', function() {{
              window.location.href = 'https://' + location.hostname + ':3443' + location.pathname + (location.search ? location.search + '&locate=1' : '?locate=1');
            }});
          }}

          // Se a URL contém ?locate=1 (ex: redirecionado para ativar GPS no HTTPS), acionar automaticamente
          if (location.search && location.search.indexOf('locate=1') !== -1) {{
            setTimeout(function() {{
              locateUserNow();
            }}, 700);
          }}

          loadMapPoints();
        }}

        if (document.readyState === 'loading') {{
          document.addEventListener('DOMContentLoaded', initDeliveryMapPage);
        }} else {{
          initDeliveryMapPage();
        }}
        </script>
        """
        return self.send_html(layout(u, 'Mapa de Entregas', content, 'Controle geográfico dos pedidos em tempo real por status de SLA e roteirização', search_action='/map', search_placeholder='Buscar pedido, cliente ou cidade no mapa...'))

    def get_map_points(self, u):
        with conn() as db:
            rows = db.execute("""
                SELECT o.id order_id, o.order_number, o.status, o.urgency, o.sale_date, o.expected_delivery_date,
                       o.weight_kg, o.total_value, o.delivery_address, o.city, o.route_name,
                       o.location_link, o.latitude, o.longitude,
                       o.delivery_location_link, o.delivery_latitude, o.delivery_longitude,
                       o.is_rural_verified, o.gps_source,
                       c.id client_id, c.name client_name, c.farm_name, c.phone client_phone, c.whatsapp client_whatsapp,
                       c.location_link client_location_link, c.latitude client_latitude, c.longitude client_longitude,
                       c.is_rural_verified client_rural_verified, c.gps_source client_gps_source, c.reference_point,
                       (SELECT delivery_location_link FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.delivery_location_link IS NOT NULL AND dr.delivery_location_link!='' LIMIT 1) dr_loc_link,
                       (SELECT latitude FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.latitude IS NOT NULL LIMIT 1) dr_lat,
                       (SELECT longitude FROM delivery_receipts dr WHERE dr.order_id=o.id AND dr.longitude IS NOT NULL LIMIT 1) dr_lng
                FROM orders o
                LEFT JOIN clients c ON c.id=o.client_id
                WHERE o.status NOT IN ('Cancelado', 'Acertado', 'Entregue')
                ORDER BY CASE WHEN o.expected_delivery_date < date('now') THEN 0 ELSE 1 END, o.expected_delivery_date ASC
            """).fetchall()

        points = []
        counts = {'late': 0, 'risk': 0, 'ok': 0, 'in_route': 0, 'scheduled': 0, 'problem': 0, 'verified_rural': 0, 'approx_city': 0, 'no_gps': 0}
        
        for r in rows:
            # Determinar coordenadas
            lat = r['dr_lat'] or r['delivery_latitude'] or r['latitude'] or r['client_latitude']
            lng = r['dr_lng'] or r['delivery_longitude'] or r['longitude'] or r['client_longitude']
            
            # Tentar extrair do link se ainda for nulo
            if lat is None or lng is None:
                link = r['dr_loc_link'] or r['delivery_location_link'] or r['location_link'] or r['client_location_link']
                if link:
                    lat, lng = extract_lat_lng(link)

            has_gps = (lat is not None and lng is not None and float(lat) != 0 and float(lng) != 0)
            
            is_rural = bool(
                r['dr_lat'] or
                r['is_rural_verified'] or
                r['client_rural_verified'] or
                (r['gps_source'] in ('driver_confirmed', 'manual_pin')) or
                (r['client_gps_source'] in ('driver_confirmed', 'manual_pin'))
            )
            gps_src = 'driver_confirmed' if r['dr_lat'] else (r['gps_source'] or r['client_gps_source'] or ('driver_confirmed' if is_rural else 'city_approximate'))

            if not has_gps:
                counts['no_gps'] += 1
            elif is_rural:
                counts['verified_rural'] += 1
            else:
                counts['approx_city'] += 1

            st = normalize_order_status(r['status'])
            deadline = r['expected_delivery_date'] or ''
            
            # Classificação de SLA
            if st == 'Agendado':
                sla_cat = 'scheduled'
                color = '#2563eb'
                sla_label = 'Agendado'
                counts['scheduled'] += 1
            elif st == 'Saiu para entrega':
                sla_cat = 'in_route'
                color = '#9333ea'
                sla_label = 'Em Rota'
                counts['in_route'] += 1
            elif st in ('Problema', 'Com problema'):
                sla_cat = 'problem'
                color = '#dc2626'
                sla_label = 'Com Problema'
                counts['problem'] += 1
            else:
                if deadline and deadline < today():
                    sla_cat = 'late'
                    color = '#dc2626'
                    sla_label = 'Vencido'
                    counts['late'] += 1
                elif deadline and deadline <= date_add(SLA_RISK_DAYS):
                    sla_cat = 'risk'
                    color = '#d97706'
                    sla_label = 'Em Risco'
                    counts['risk'] += 1
                else:
                    sla_cat = 'ok'
                    color = '#16a34a'
                    sla_label = 'No Prazo'
                    counts['ok'] += 1

            points.append({
                'order_id': r['order_id'],
                'order_number': r['order_number'],
                'client_id': r['client_id'],
                'client_name': r['client_name'] or 'Cliente não informado',
                'farm_name': r['farm_name'] or '',
                'city': r['city'] or '',
                'route_name': r['route_name'] or 'Sem rota',
                'weight_kg': r['weight_kg'] or 0,
                'total_value': r['total_value'] or 0,
                'delivery_address': r['delivery_address'] or '',
                'status': st,
                'sla_category': sla_cat,
                'sla_label': sla_label,
                'color': color,
                'has_gps': has_gps,
                'is_rural_verified': is_rural,
                'gps_source': gps_src,
                'reference_point': r['reference_point'] or '',
                'client_phone': r['client_phone'] or '',
                'client_whatsapp': r['client_whatsapp'] or '',
                'lat': float(lat) if has_gps else None,
                'lng': float(lng) if has_gps else None
            })

        return self.send_json({
            'ok': True,
            'counts': counts,
            'points': points
        })

    def post_map_save_location(self, u):
        data = self.post_data()
        order_id = parse_int(data.get('order_id'))
        client_id = parse_int(data.get('client_id'))
        lat = parse_float(data.get('lat'))
        lng = parse_float(data.get('lng'))

        if lat is None or lng is None or lat < -90 or lat > 90 or lng < -180 or lng > 180:
            return self.send_json({'ok': False, 'message': 'Coordenadas inválidas informadas.'}, 400)

        maps_link = f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}"

        with conn() as db:
            if order_id:
                db.execute("""
                    UPDATE orders 
                    SET latitude=?, longitude=?, location_link=?, is_rural_verified=1, gps_source='manual_pin', updated_at=?, version=COALESCE(version,1)+1 
                    WHERE id=? OR order_number=?
                """, (lat, lng, maps_link, now(), order_id, str(order_id)))
                
                if not client_id:
                    o = db.execute("SELECT client_id FROM orders WHERE id=? OR order_number=?", (order_id, str(order_id))).fetchone()
                    if o: client_id = o['client_id']

            if client_id:
                db.execute("""
                    UPDATE clients 
                    SET latitude=?, longitude=?, location_link=?, is_rural_verified=1, gps_source='manual_pin', updated_at=?, version=COALESCE(version,1)+1 
                    WHERE id=?
                """, (lat, lng, maps_link, now(), client_id))
                
                db.execute("""
                    UPDATE orders 
                    SET latitude=?, longitude=?, location_link=?, is_rural_verified=1, gps_source='manual_pin', updated_at=?, version=COALESCE(version,1)+1 
                    WHERE client_id=? AND (delivery_latitude IS NULL OR delivery_latitude=0)
                """, (lat, lng, maps_link, now(), client_id))

            audit(db, u, 'Marcou localização no mapa', 'Mapa/Clientes', f"Cliente {client_id} / Pedido {order_id}", '', f"{lat:.6f}, {lng:.6f} (Ponto Manual Confirmado)")
            db.commit()

        return self.send_json({
            'ok': True,
            'message': 'Localização salva e sincronizada com o perfil do cliente!'
        })

    def post_map_test_key(self, u):
        data = self.post_data()
        key = (data.get('api_key') or '').strip() or get_google_maps_api_key()
        if not key:
            return self.send_json({'ok': False, 'message': 'Nenhuma chave informada ou cadastrada no sistema.'}, 400)

        try:
            import urllib.request
            import urllib.parse
            url = f"https://maps.googleapis.com/maps/api/geocode/json?address=Teofilo+Otoni+MG&key={urllib.parse.quote(key)}"
            req = urllib.request.Request(url, headers={'User-Agent': 'LogisticaCasaDoCampo/1.0'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                res_data = json.loads(resp.read().decode('utf-8'))
                status = res_data.get('status')
                if status == 'OK':
                    return self.send_json({
                        'ok': True,
                        'message': '✅ Chave validada com sucesso no Google Cloud! Maps e Geocoding estão ativos e respondendo.'
                    })
                elif status == 'REQUEST_DENIED':
                    err_msg = res_data.get('error_message', 'Chave inválida ou permissão de Geocoding API não ativada.')
                    return self.send_json({'ok': False, 'message': f'❌ Rejeitado pelo Google: {err_msg}'}, 400)
                else:
                    return self.send_json({'ok': False, 'message': f'Resposta do Google: {status}'}, 400)
        except Exception as exc:
            return self.send_json({'ok': False, 'message': f'Erro ao conectar com Google Cloud: {str(exc)}'}, 500)

    def post_map_geocode_auto(self, u):
        api_key = get_google_maps_api_key()
        if not api_key:
            return self.send_json({
                'ok': False,
                'message': 'Chave da API do Google Maps não configurada. Configure-a em Configurações > Parâmetros ou no .env.'
            }, 400)

        data = self.post_data()
        target_order_id = parse_int(data.get('order_id'))

        import urllib.request
        import urllib.parse

        with conn() as db:
            if target_order_id:
                orders_to_geocode = db.execute("""
                    SELECT o.id, o.order_number, o.delivery_address, o.city, o.uf,
                           c.id as client_id, c.name as client_name, c.address as client_address, c.city as client_city, c.farm_name, c.neighborhood
                    FROM orders o
                    LEFT JOIN clients c ON c.id = o.client_id
                    WHERE o.id = ?
                """, (target_order_id,)).fetchall()
            else:
                orders_to_geocode = db.execute("""
                    SELECT o.id, o.order_number, o.delivery_address, o.city, o.uf,
                           c.id as client_id, c.name as client_name, c.address as client_address, c.city as client_city, c.farm_name, c.neighborhood
                    FROM orders o
                    LEFT JOIN clients c ON c.id = o.client_id
                    WHERE o.status NOT IN ('Cancelado', 'Acertado', 'Entregue')
                      AND (o.latitude IS NULL OR o.latitude = 0)
                    LIMIT 30
                """).fetchall()

            if not orders_to_geocode:
                return self.send_json({
                    'ok': True,
                    'geocoded_count': 0,
                    'message': 'Todos os pedidos em aberto já possuem localização GPS no mapa!'
                })

            geocoded_count = 0
            details = []

            for row in orders_to_geocode:
                order_id = row['id']
                client_id = row['client_id']
                city = row['city'] or row['client_city'] or 'Teófilo Otoni'
                uf = row['uf'] or 'MG'
                addr = row['delivery_address'] or row['client_address'] or ''
                farm = row['farm_name'] or ''
                bairro = row['neighborhood'] or ''

                query_parts = []
                if addr:
                    query_parts.append(addr)
                elif farm:
                    query_parts.append(farm)
                if bairro and bairro not in addr:
                    query_parts.append(bairro)
                query_parts.append(city)
                query_parts.append(uf)
                query_parts.append('Brasil')

                full_query = ', '.join([p.strip() for p in query_parts if p.strip()])
                if not full_query:
                    continue

                try:
                    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={urllib.parse.quote(full_query)}&components=country:BR&key={urllib.parse.quote(api_key)}"
                    req = urllib.request.Request(url, headers={'User-Agent': 'LogisticaCasaDoCampo/1.0'})
                    with urllib.request.urlopen(req, timeout=6) as resp:
                        res_data = json.loads(resp.read().decode('utf-8'))
                        if res_data.get('status') == 'OK' and res_data.get('results'):
                            loc = res_data['results'][0]['geometry']['location']
                            lat = float(loc['lat'])
                            lng = float(loc['lng'])
                            maps_link = f"https://www.google.com/maps?q={lat:.6f},{lng:.6f}"

                            db.execute("""
                                UPDATE orders
                                SET latitude=?, longitude=?, location_link=?, updated_at=?, version=COALESCE(version,1)+1
                                WHERE id=?
                            """, (lat, lng, maps_link, now(), order_id))

                            if client_id:
                                db.execute("""
                                    UPDATE clients
                                    SET latitude=COALESCE(latitude, ?), longitude=COALESCE(longitude, ?), location_link=COALESCE(location_link, ?), updated_at=?, version=COALESCE(version,1)+1
                                    WHERE id=? AND (latitude IS NULL OR latitude=0)
                                """, (lat, lng, maps_link, now(), client_id))

                            geocoded_count += 1
                            details.append(f"Pedido {row['order_number']}")
                except Exception as exc:
                    log_server_error(f'GEOCODE_ORDER_{order_id}', exc)

            if geocoded_count > 0:
                audit(db, u, f'Geocodificou {geocoded_count} pedidos via Google Maps', 'Mapa', ', '.join(details[:6]))
                db.commit()

        return self.send_json({
            'ok': True,
            'geocoded_count': geocoded_count,
            'message': f'Sucesso! {geocoded_count} pedido(s) localizados e fixados no mapa via Google Maps.'
        })

    def post_routes_create_from_map(self, u):
        data = self.post_data()
        name = (data.get('name') or '').strip()
        if not name:
            name = f"Carga {datetime.now().strftime('%d/%m %H:%M')}"
        route_date = validate_date_field(data.get('date') or today(), 'a data da carga', required=True)
        driver_id = parse_int(data.get('driver_id') or 0)
        vehicle_id = parse_int(data.get('vehicle_id') or 0)
        route_name = upper_text(data.get('route_name') or 'Mista')
        notes = (data.get('notes') or '').strip()

        raw_order_ids = data.get('order_ids') or []
        if isinstance(raw_order_ids, str):
            try:
                raw_order_ids = json.loads(raw_order_ids)
            except Exception:
                raw_order_ids = [int(x.strip()) for x in raw_order_ids.split(',') if x.strip().isdigit()]

        order_ids = [parse_int(x) for x in raw_order_ids if parse_int(x)]
        if not order_ids:
            return self.send_json({'ok': False, 'message': 'Nenhum pedido foi selecionado para montar a carga.'}, 400)

        if driver_id <= 0:
            return self.send_json({'ok': False, 'message': 'Selecione o motorista responsável pela carga.'}, 400)
        if vehicle_id <= 0:
            return self.send_json({'ok': False, 'message': 'Selecione o veículo da carga.'}, 400)

        with conn() as db:
            driver_ok = db.execute('SELECT id, active FROM drivers WHERE id=?', (driver_id,)).fetchone()
            if not driver_ok or int(driver_ok['active'] or 0) != 1:
                return self.send_json({'ok': False, 'message': 'Motorista selecionado é inválido ou inativo.'}, 400)

            vehicle_ok = db.execute('SELECT id, active, capacity_kg, capacity FROM vehicles WHERE id=?', (vehicle_id,)).fetchone()
            if not vehicle_ok or int(vehicle_ok['active'] or 0) != 1:
                return self.send_json({'ok': False, 'message': 'Veículo selecionado é inválido ou inativo.'}, 400)

            default_cap = float(vehicle_ok['capacity_kg'] if vehicle_ok['capacity_kg'] is not None else (vehicle_ok['capacity'] or 11000))
            cap = parse_float(data.get('capacity') or default_cap)
            if cap <= 0:
                cap = default_cap or 11000.0

            total_weight = 0.0
            valid_orders = []
            old_routes = set()

            for oid in order_ids:
                order = db.execute("SELECT id, order_number, weight_kg, status, route_name FROM orders WHERE id=?", (oid,)).fetchone()
                if not order:
                    continue
                st = normalize_order_status(order['status'])
                if st in ('Cancelado', 'Acertado', 'Entregue'):
                    continue
                valid_orders.append(order)
                total_weight += float(order['weight_kg'] or 0)

            if not valid_orders:
                return self.send_json({'ok': False, 'message': 'Nenhum dos pedidos selecionados está elegível para carga.'}, 400)

            if cap > 0 and total_weight > cap:
                return self.send_json({
                    'ok': False, 
                    'message': f'Peso total ({fmt_num(total_weight)} kg) excede a capacidade do veículo ({fmt_num(cap)} kg). Reduza os pedidos ou ajuste a capacidade.'
                }, 400)

            cur = db.execute("""
                INSERT INTO routes(name, date, driver_id, vehicle_id, status, route_name, total_weight, capacity, notes, created_at, updated_at, version)
                VALUES(?, ?, ?, ?, 'Planejada', ?, ?, ?, ?, ?, ?, 1)
            """, (name, route_date, driver_id, vehicle_id, route_name, total_weight, cap, notes, now(), now()))
            rid = cur.lastrowid

            for idx, ord_item in enumerate(valid_orders, 1):
                oid = ord_item['id']
                for rr in db.execute("""SELECT ro.route_id FROM route_orders ro JOIN routes ar ON ar.id=ro.route_id WHERE ro.order_id=? AND ar.status IN ('Planejada','Em rota')""", (oid,)).fetchall():
                    old_routes.add(rr['route_id'])

                db.execute("""DELETE FROM route_orders WHERE id IN (SELECT ro.id FROM route_orders ro JOIN routes ar ON ar.id=ro.route_id WHERE ro.order_id=? AND ar.status IN ('Planejada','Em rota'))""", (oid,))
                db.execute('INSERT INTO route_orders(route_id, order_id, delivery_order, status) VALUES(?, ?, ?, ?)', (rid, oid, idx, 'Pendente'))
                
                old_st = normalize_order_status(ord_item['status'])
                if old_st != 'Carga Montada':
                    self.ensure_order_status(db, oid, 'Carga Montada')
                    db.execute("UPDATE orders SET status='Carga Montada', updated_at=?, version=COALESCE(version,1)+1 WHERE id=?", (now(), oid))
                    add_hist(db, oid, u['id'], old_st, 'Carga Montada', 'Carga montada (Aguardando saída)', f'Carga #{rid}')

            for old_rid in old_routes:
                if old_rid != rid:
                    self.recalc_route_weight(db, old_rid)
            self.recalc_route_weight(db, rid)
            audit(db, u, 'Criou carga via Mapa', 'Rotas/Mapa', name, '', f"{total_weight:.2f} kg, {len(valid_orders)} pedidos")
            db.commit()

        return self.send_json({
            'ok': True,
            'route_id': rid,
            'redirect_url': f'/routes/{rid}',
            'message': f'Carga "{name}" criada com sucesso contendo {len(valid_orders)} pedidos ({total_weight:.1f} kg)!'
        })

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
            if normalize_route_status(r['status']) not in ('Em rota', 'Aguardando Chegada'):
                return self.fail(u,'Operação inválida','Somente cargas em rota ou aguardando chegada podem ser concluídas no acerto.',400)
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
                o_items = db.execute("SELECT id, product_name, status, problem_notes FROM order_items WHERE order_id=? ORDER BY id", (oid,)).fetchall()
                item_problems = []
                for it in o_items:
                    it_id = it['id']
                    it_status = (d.get(f'item_status_{it_id}') or 'Entregue').strip()
                    it_prob = (d.get(f'item_problem_{it_id}') or '').strip()
                    if it_status == 'Problema':
                        if not it_prob:
                            raise ValueError(f'É obrigatório descrever o problema do produto "{it["product_name"]}" no pedido {ro["order_number"]}.')
                        db.execute("UPDATE order_items SET status='Problema', problem_notes=? WHERE id=?", (it_prob, it_id))
                        item_problems.append(f'{it["product_name"]}: {it_prob}')
                    else:
                        db.execute("UPDATE order_items SET status='Entregue', problem_notes=NULL WHERE id=?", (it_id,))

                if item_problems:
                    result = 'problema'
                    prob_desc_items = "Problema nos itens: " + "; ".join(item_problems)
                    if obs:
                        obs = f"{obs} | {prob_desc_items}"
                    else:
                        obs = prob_desc_items

                if result == 'problema':
                    if not obs:
                        raise ValueError(f'Informe observação do problema para o pedido {ro["order_number"]}.')
                    ptype=(d.get(f'ptype_{oid}') or ('Mercadoria avariada' if item_problems else 'Outro motivo')).strip()
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
                    db.execute('INSERT INTO delivery_problems(order_id,route_id,problem_type,description,created_at) VALUES(?,?,?,?,?)',(oid,rid,ptype,obs,now()))
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
        faturado_waiting=sum(1 for r in open_rows if r['status'] in ('Faturado', 'Carga Montada'))
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
            trs_list.append(f"""<tr class='{row_type} {highlight_cls}'><td><b>{esc(r['order_number'])}</b><br><small>{esc(r['client'] or '')} · Vendedor: {esc(resolve_seller_name(r['seller_name']))}</small></td><td>{brdate(r['sale_date'])}</td><td>{brdate(r['expected_delivery_date'])}</td><td>{badge(r['status'])}</td><td>{deadline_pill(r['expected_delivery_date'],r['status'])}</td><td>{esc(r['city'] or r['client_city'] or '')}</td><td><a class='btn small ghost' href='/orders/{r['id']}'>Abrir</a></td></tr>""")
        trs = ''.join(trs_list)

        critical_rows=''.join(f"""<tr><td><b>{esc(r['order_number'])}</b><br><small>{esc(r['client'] or '')} · Vendedor: {esc(resolve_seller_name(r['seller_name']))}</small></td><td>{badge(r['status'])}</td><td>{brdate(r['expected_delivery_date'])}</td><td>{esc(r['route_name'] or 'Sem rota')} · {esc(r['city'] or r['client_city'] or 'Sem cidade')}</td><td><a class='btn small ghost' href='/orders/{r['id']}'>Abrir</a></td></tr>""" for r in critical) or '<tr><td colspan="5">Nenhum pedido crítico no período.</td></tr>'
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
            edit_panel=f'''<section class="panel"><h2>Editar motorista</h2><form method="post" action="/drivers/{edit_row["id"]}/update" class="inline-form"><input name="name" required value="{esc(edit_row["name"])}"><input name="phone" value="{esc(edit_row["phone"] or "")}"><input name="document" value="{esc(edit_row["document"] or "")}"><input name="vehicle_default" list="vehicleDefaultSuggestions" value="{esc(edit_row["vehicle_default"] or "")}"><button>Salvar</button><a class="btn ghost" href="/drivers">Cancelar</a></form><form method="post" action="/drivers/{edit_row["id"]}/reset-password" class="inline-form" style="margin-top:12px; padding-top:12px; border-top:1px solid #cbd5e1;"><input type="hidden" name="redirect_to" value="/drivers"><input name="new_password" type="password" placeholder="Nova senha manual" style="max-width:220px;" required><button class="btn small primary" style="background:#0284c7; border-color:#0284c7;">🔑 Trocar Senha Manualmente</button></form></section>'''
        def render_rows(source_rows):
            body_rows=''
            for r in source_rows:
                if can_manage:
                    reset_btn=f'''<form method="post" action="/drivers/{r["id"]}/reset-password" class="inline-mini needs-confirm" data-confirm-text="Confirma resetar a senha de {esc(r["name"])} para \'123\' (exigirá nova senha no próximo acesso)?"><input type="hidden" name="reset_to_default" value="1"><button class="btn small ghost" style="color:#0284c7; border-color:#0284c7;">⚡ Resetar (123)</button></form>'''
                    actions=f'''<a class="btn small ghost" href="/drivers?edit={r["id"]}">Editar</a>{reset_btn}
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

    def post_driver_reset_password(self, u, did):
        d = self.post_data()
        redirect_to = (d.get('redirect_to') or '/drivers').strip()
        custom_password = (d.get('new_password') or '').strip()
        reset_to_default = str(d.get('reset_to_default') or '').strip().lower() in ('1', 'on', 'true', 'sim', 'yes')

        with conn() as db:
            driver_row = db.execute('SELECT * FROM drivers WHERE id=?', (did,)).fetchone()
            if not driver_row:
                return self.fail(u, 'Não encontrado', 'Motorista não encontrado.', 404)

            if reset_to_default or not custom_password:
                pwd_hash = hash_driver_password(DEFAULT_DRIVER_PASSWORD)
                must_change = 1
                msg_audit = "Resetou senha do motorista para '123' (troca obrigatória no 1º acesso)"
            else:
                if len(custom_password) < 4:
                    raise ValueError("A nova senha do motorista deve ter no mínimo 4 caracteres.")
                pwd_hash = hash_driver_password(custom_password)
                must_change = 0
                msg_audit = f"Definiu nova senha manual para o motorista {driver_row['name']}"

            db.execute(
                'UPDATE drivers SET password_hash=?, must_change_password=?, updated_at=?, version=COALESCE(version,1)+1 WHERE id=?',
                (pwd_hash, must_change, now(), did),
            )
            db.execute("UPDATE driver_sessions SET revoked_at=? WHERE driver_id=? AND revoked_at IS NULL", (utc_iso(), did))
            audit(db, u, 'Resetou senha de motorista', 'Motoristas', driver_row['name'], notes=msg_audit)
            db.commit()

        self.redirect(redirect_to)
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

    def relatorio_viagens(self, u):
        if not (self.has_perm(u, 'view_reports') or self.has_perm(u, 'view_routes')):
            return self.fail(u, 'Acesso negado', 'Sem permissão para visualizar relatórios de motoristas.', 403)
        qs = parse_qs(urlparse(self.path).query)
        can_view_financial = self.can_view_financial(u)
        from app_core.domains.driver_reports_dispatch import render_relatorio_motoristas_html
        content = render_relatorio_motoristas_html(self, u, qs, can_view_financial)
        rep_styles = """<style>
.rep-shell { display: flex; flex-direction: column; gap: 16px; width: 100%; max-width: 100%; box-sizing: border-box; }
.rep-top-bar {
  display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 14px;
  background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px 18px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.rep-nav-group { display: flex; align-items: center; gap: 6px; background: #f1f5f9; padding: 4px; border-radius: 10px; }
.rep-nav-item {
  padding: 8px 18px; font-weight: 700; font-size: 13.5px; border-radius: 8px; color: #475569;
  text-decoration: none; display: inline-flex; align-items: center; gap: 8px; transition: all 0.15s ease;
}
.rep-nav-item:hover { color: #0f172a; background: rgba(255,255,255,0.7); }
.rep-nav-item.active { background: #ffffff; color: #15803d; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
.rep-top-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.rep-btn-cta-pdf {
  display: inline-flex; align-items: center; gap: 8px; background: #166534; color: #ffffff;
  padding: 9px 16px; border-radius: 8px; font-size: 13px; font-weight: 750; border: none; cursor: pointer;
  text-decoration: none; transition: background 0.15s ease; box-shadow: 0 1px 3px rgba(22,101,52,0.25);
}
.rep-btn-cta-pdf:hover { background: #15803d; }
.rep-btn-cta-csv {
  display: inline-flex; align-items: center; gap: 8px; background: #ffffff; color: #0f172a;
  padding: 9px 16px; border-radius: 8px; font-size: 13px; font-weight: 750; border: 1px solid #cbd5e1;
  cursor: pointer; text-decoration: none; transition: all 0.15s ease;
}
.rep-btn-cta-csv:hover { background: #f8fafc; border-color: #94a3b8; }
.rep-card { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
.rep-filter-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 14px; }
.rep-presets { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.rep-presets-title { font-size: 12.5px; font-weight: 700; color: #475569; margin-right: 2px; }
.rep-preset-pill { background: #f1f5f9; color: #334155; font-size: 12px; font-weight: 700; padding: 5px 12px; border-radius: 999px; border: 1px solid #e2e8f0; cursor: pointer; transition: all 0.15s; }
.rep-preset-pill:hover { background: #e2e8f0; color: #0f172a; }
.rep-active-badge { font-size: 12.5px; color: #166534; background: #f0fdf4; border: 1px solid #bbf7d0; padding: 4px 10px; border-radius: 8px; font-weight: 600; }
.rep-grid-filters { display: grid; gap: 14px; margin-bottom: 14px; }
.rep-input-group { display: flex; flex-direction: column; gap: 4px; }
.rep-filter-actions { display: flex; justify-content: flex-end; gap: 10px; border-top: 1px solid #f1f5f9; padding-top: 12px; }
</style>
<script>
function setRepDates(preset, formId) {
  const form = document.getElementById(formId);
  if (!form) return;
  const today = new Date();
  const pad = n => String(n).padStart(2, '0');
  const fmt = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  let start = fmt(today);
  let end = fmt(today);
  if (preset === 'today') {
    start = fmt(today); end = fmt(today);
  } else if (preset === '7days') {
    const d = new Date(today); d.setDate(today.getDate() - 7);
    start = fmt(d); end = fmt(today);
  } else if (preset === 'this_month') {
    const d = new Date(today.getFullYear(), today.getMonth(), 1);
    start = fmt(d); end = fmt(today);
  } else if (preset === 'last_month') {
    const d1 = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const d2 = new Date(today.getFullYear(), today.getMonth(), 0);
    start = fmt(d1); end = fmt(d2);
  }
  if (form.start) form.start.value = start;
  if (form.end) form.end.value = end;
  form.submit();
}
</script>"""
        subtitle = 'Consolidação de desempenho, quilometragem, diárias, despesas e emissão de PDFs por motorista'
        return self.send_html(layout(u, 'Relatório de Motoristas', rep_styles + content, subtitle))

    def relatorios(self, u):
        qs = parse_qs(urlparse(self.path).query)
        tab = (qs.get('tab', ['operacional'])[0] or 'operacional').strip().lower()
        if tab not in ('operacional', 'pedidos', 'motoristas'):
            tab = 'operacional'

        can_view_financial = self.can_view_financial(u)

        rep_styles = """<style>
/* RELATÓRIOS - DESIGN SYSTEM MODERNO, LIMPO E INTUITIVO */
.rep-shell { display: flex; flex-direction: column; gap: 16px; width: 100%; max-width: 100%; box-sizing: border-box; }
.rep-top-bar {
  display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 14px;
  background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px 18px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.rep-nav-group { display: flex; align-items: center; gap: 6px; background: #f1f5f9; padding: 4px; border-radius: 10px; }
.rep-nav-item {
  padding: 8px 18px; font-weight: 700; font-size: 13.5px; border-radius: 8px; color: #475569;
  text-decoration: none; display: inline-flex; align-items: center; gap: 8px; transition: all 0.15s ease;
}
.rep-nav-item:hover { color: #0f172a; background: rgba(255,255,255,0.7); }
.rep-nav-item.active { background: #ffffff; color: #15803d; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }

.rep-top-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.rep-btn-cta-pdf {
  background: linear-gradient(135deg, #15803d 0%, #166534 100%) !important;
  color: #ffffff !important; font-size: 14px !important; font-weight: 700 !important;
  padding: 10px 20px !important; border-radius: 8px !important; border: 1px solid #14532d !important;
  box-shadow: 0 3px 10px rgba(22, 101, 52, 0.28) !important; cursor: pointer;
  display: inline-flex; align-items: center; gap: 8px; transition: all 0.15s ease;
}
.rep-btn-cta-pdf:hover {
  filter: brightness(1.1); transform: translateY(-1px); box-shadow: 0 5px 14px rgba(22, 101, 52, 0.38) !important;
}
.rep-btn-cta-csv {
  background: #ffffff !important; color: #1e293b !important; font-size: 13.5px !important; font-weight: 600 !important;
  padding: 10px 18px !important; border-radius: 8px !important; border: 1px solid #cbd5e1 !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important; cursor: pointer;
  display: inline-flex; align-items: center; gap: 8px; transition: all 0.15s ease;
}
.rep-btn-cta-csv:hover { background: #f8fafc !important; border-color: #94a3b8 !important; }

.rep-card {
  background: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 18px 20px;
  box-shadow: 0 2px 6px rgba(0,0,0,0.02); box-sizing: border-box; margin-bottom: 14px;
}
.rep-filter-header {
  display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;
  margin-bottom: 14px; padding-bottom: 12px; border-bottom: 1px solid #f1f5f9;
}
.rep-presets { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.rep-presets-title { font-size: 11.5px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: 0.3px; margin-right: 2px; }
.rep-preset-pill {
  background: #f8fafc; border: 1px solid #e2e8f0; color: #334155; font-size: 12px; font-weight: 600;
  padding: 5px 12px; border-radius: 6px; cursor: pointer; transition: all 0.15s ease;
}
.rep-preset-pill:hover { background: #e2e8f0; color: #0f172a; }
.rep-preset-pill.active { background: #dcfce7; border-color: #86efac; color: #15803d; font-weight: 700; }
.rep-active-badge {
  font-size: 12px; color: #475569; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px;
  padding: 5px 12px; display: inline-flex; align-items: center; gap: 6px;
}

.rep-grid-filters {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: 12px; align-items: end;
}
.rep-input-group { display: flex; flex-direction: column; gap: 4px; }
.rep-input-group label {
  font-size: 11px; font-weight: 700; color: #475569; text-transform: uppercase; letter-spacing: 0.3px;
}
.rep-input-group input, .rep-input-group select {
  height: 38px; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 10px; font-size: 13px;
  background: #ffffff; color: #0f172a; width: 100%; box-sizing: border-box; transition: border-color 0.15s, box-shadow 0.15s;
}
.rep-input-group input:focus, .rep-input-group select:focus {
  border-color: #15803d; outline: none; box-shadow: 0 0 0 3px rgba(21, 128, 61, 0.12);
}

.rep-kpis-container {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px;
}
.rep-kpi {
  background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 14px 16px;
  border-left: 4px solid #94a3b8; display: flex; flex-direction: column; justify-content: space-between;
  box-shadow: 0 1px 3px rgba(0,0,0,0.03); transition: transform 0.15s, box-shadow 0.15s;
}
.rep-kpi:hover { transform: translateY(-2px); box-shadow: 0 4px 10px rgba(0,0,0,0.06); }
.rep-kpi.green { border-left-color: #16a34a; }
.rep-kpi.amber { border-left-color: #d97706; }
.rep-kpi.red { border-left-color: #dc2626; }
.rep-kpi.blue { border-left-color: #2563eb; }
.rep-kpi-lbl { font-size: 12px; font-weight: 600; color: #64748b; display: flex; align-items: center; gap: 6px; }
.rep-kpi-val { font-size: 21px; font-weight: 800; color: #0f172a; margin-top: 4px; line-height: 1.2; }
.rep-kpi-sub { font-size: 11px; color: #94a3b8; margin-top: 2px; }

.rep-section-title {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;
}
.rep-section-title h3 {
  margin: 0; font-size: 15px; font-weight: 700; color: #0f172a; display: flex; align-items: center; gap: 8px;
}
.rep-count-badge {
  background: #f1f5f9; color: #475569; font-size: 11.5px; font-weight: 700; padding: 3px 10px; border-radius: 12px;
}

.rep-progress-track {
  background: #e2e8f0; border-radius: 6px; height: 7px; overflow: hidden; width: 100%; min-width: 60px; margin-top: 4px;
}
.rep-progress-fill { height: 100%; border-radius: 6px; }
.rep-fill-green { background: #16a34a; }
.rep-fill-amber { background: #f59e0b; }
.rep-fill-red { background: #ef4444; }

.rep-schedule-details {
  background: #f8fafc; border: 1px dashed #cbd5e1; border-radius: 10px; padding: 12px 16px; margin-top: 14px;
}
.rep-schedule-details summary {
  font-size: 13px; font-weight: 700; color: #475569; cursor: pointer; outline: none;
}
.rep-split-bar {
  display: flex; height: 16px; border-radius: 8px; overflow: hidden; background: #e2e8f0; width: 100%; margin: 8px 0;
}
.rep-split-seg-comm {
  background: #3b82f6; transition: width 0.3s;
}
.rep-split-seg-log {
  background: #10b981; transition: width 0.3s;
}
.rep-lead-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 14px;
}
.rep-lead-card {
  background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 10px; padding: 14px;
}
.rep-lead-title {
  font-size: 12px; font-weight: 700; color: #475569; text-transform: uppercase; letter-spacing: 0.3px; display: flex; align-items: center; gap: 6px;
}
.rep-lead-val {
  font-size: 24px; font-weight: 800; color: #0f172a; margin: 6px 0 2px 0;
}
.rep-lead-sub {
  font-size: 11.5px; color: #64748b;
}

/* PAINEL MODULAR DE CUSTOMIZAÇÃO DE RELATÓRIO */
.rep-customizer-panel {
  background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 10px; margin-top: 14px; overflow: hidden;
  transition: all 0.2s ease;
}
.rep-customizer-panel[open] {
  background: #ffffff; border-color: #94a3b8; box-shadow: 0 4px 12px rgba(0,0,0,0.05);
}
.rep-customizer-summary {
  display: flex; justify-content: space-between; align-items: center; padding: 12px 16px;
  cursor: pointer; user-select: none; background: #f1f5f9; font-size: 13.5px; font-weight: 700; color: #1e293b;
}
.rep-customizer-summary:hover { background: #e2e8f0; }
.rep-customizer-summary-left { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rep-customizer-badge {
  font-size: 11px; font-weight: 700; background: #dbeafe; color: #1e40af; padding: 2px 8px; border-radius: 12px;
}
.rep-customizer-hint { font-size: 11.5px; color: #64748b; font-weight: normal; }

.rep-customizer-content { padding: 16px 18px; border-top: 1px solid #e2e8f0; }
.rep-customizer-toolbar {
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 14px;
  padding-bottom: 12px; border-bottom: 1px solid #f1f5f9;
}
.rep-tool-btn {
  background: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 12px;
  font-size: 12px; font-weight: 600; color: #334155; cursor: pointer; transition: all 0.15s ease;
  display: inline-flex; align-items: center; gap: 4px;
}
.rep-tool-btn:hover { background: #f1f5f9; border-color: #94a3b8; color: #0f172a; }
.rep-tool-btn.save {
  background: #f0fdf4; border-color: #86efac; color: #166534; font-weight: 700; margin-left: auto;
}
.rep-tool-btn.save:hover { background: #dcfce7; border-color: #4ade80; }

.rep-customizer-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px;
}
.rep-cust-card {
  background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px 14px;
}
.rep-cust-card-title {
  font-size: 12px; font-weight: 700; text-transform: uppercase; color: #475569; letter-spacing: 0.3px;
  margin-bottom: 10px; display: flex; align-items: center; gap: 6px; padding-bottom: 6px; border-bottom: 1px solid #e2e8f0;
}
.rep-cust-card-body { display: flex; flex-direction: column; gap: 8px; }
.rep-chk-item {
  display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: #1e293b; cursor: pointer;
}
.rep-chk-item input[type="checkbox"] {
  width: 16px; height: 16px; cursor: pointer; accent-color: #15803d;
}
</style>
<script>
function setRepDates(preset, fId) {
  var f = document.getElementById(fId);
  if (!f) return;
  var sInp = f.querySelector('input[name="start"]');
  var eInp = f.querySelector('input[name="end"]');
  var now = new Date();
  var pad = function(n){ return n < 10 ? '0' + n : n; };
  var fmt = function(d){ return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()); };
  if (preset === 'today') {
    sInp.value = fmt(now);
    eInp.value = fmt(now);
  } else if (preset === '7days') {
    var past = new Date(); past.setDate(now.getDate() - 7);
    sInp.value = fmt(past);
    eInp.value = fmt(now);
  } else if (preset === 'this_month') {
    var d1 = new Date(now.getFullYear(), now.getMonth(), 1);
    sInp.value = fmt(d1);
    eInp.value = fmt(now);
  } else if (preset === 'last_month') {
    var d1 = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    var d2 = new Date(now.getFullYear(), now.getMonth(), 0);
    sInp.value = fmt(d1);
    eInp.value = fmt(d2);
  } else if (preset === 'open_orders') {
    var d1 = new Date(now.getFullYear(), 0, 1);
    sInp.value = fmt(d1);
    eInp.value = fmt(now);
    var stSel = f.querySelector('select[name="status"]');
    if (stSel) stSel.value = 'open';
  }
  var pills = f.querySelectorAll('.rep-preset-pill');
  pills.forEach(function(p){ p.classList.remove('active'); });
  if (window.event && window.event.target) {
    window.event.target.classList.add('active');
  }
}

function setAllCheckboxes(formId, checked) {
  var f = document.getElementById(formId);
  if (!f) return;
  var chks = f.querySelectorAll('.rep-customizer-grid input[type="checkbox"]');
  chks.forEach(function(c) {
    if (!c.disabled) c.checked = checked;
  });
}

function resetDefaultCheckboxes(formId) {
  var f = document.getElementById(formId);
  if (!f) return;
  var chks = f.querySelectorAll('.rep-customizer-grid input[type="checkbox"]');
  chks.forEach(function(c) {
    var n = c.name;
    if (n === 'pdf_hdr_problems' || n === 'pdf_hdr_canceled' || n === 'pdf_report_st_cancelado' || n === 'sec_orders') {
      c.checked = false;
    } else {
      c.checked = true;
    }
  });
}

function saveRepPreferences(formId, tabName) {
  var f = document.getElementById(formId);
  if (!f) return;
  var postForm = document.createElement('form');
  postForm.method = 'POST';
  postForm.action = '/relatorios/settings';
  
  var tabInp = document.createElement('input');
  tabInp.type = 'hidden';
  tabInp.name = 'origin_tab';
  tabInp.value = tabName;
  postForm.appendChild(tabInp);

  var chks = f.querySelectorAll('.rep-customizer-grid input[type="checkbox"]');
  chks.forEach(function(c) {
    var inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = c.name;
    inp.value = c.checked ? '1' : '0';
    postForm.appendChild(inp);
  });

  var csrfInp = f.querySelector('input[name="_csrf"]');
  if (csrfInp) {
    var ci = document.createElement('input');
    ci.type = 'hidden';
    ci.name = '_csrf';
    ci.value = csrfInp.value;
    postForm.appendChild(ci);
  }

  document.body.appendChild(postForm);
  postForm.submit();
}
</script>"""

        if tab == 'pedidos':
            content = self.render_relatorio_pedidos(u, qs, can_view_financial)
            subtitle = 'Relatório analítico e auditoria de pedidos com busca, filtros e exportação'
        elif tab == 'motoristas':
            from app_core.domains.driver_reports_dispatch import render_relatorio_motoristas_html
            content = render_relatorio_motoristas_html(self, u, qs, can_view_financial)
            subtitle = 'Consolidação de desempenho, quilometragem, diárias e despesas por motorista'
        else:
            tab = 'operacional'
            content = self.render_relatorio_operacional(u, qs, can_view_financial)
            subtitle = 'Indicadores executivos, desempenho de motoristas, caminhões, rotas e pontualidade SLA'

        return self.send_html(layout(u, 'Relatórios', rep_styles + content, subtitle))

    def render_relatorio_operacional(self, u, qs, can_view_financial):
        with conn() as db:
            route_names = [r['route_name'] for r in db.execute('SELECT DISTINCT route_name FROM orders WHERE route_name IS NOT NULL AND route_name<>"" ORDER BY route_name')]
            city_names = [r['city'] for r in db.execute('SELECT DISTINCT city FROM orders WHERE city IS NOT NULL AND city<>"" ORDER BY city')]
            driver_names = [r['name'] for r in db.execute('SELECT name FROM drivers WHERE active=1 ORDER BY name')]
            vehicle_rows = db.execute("SELECT id, name, plate FROM vehicles WHERE active=1 ORDER BY name").fetchall()
            vehicle_names = [f"{r['name']} - {r['plate']}" if (r['plate'] and r['plate'].strip()) else r['name'] for r in vehicle_rows]
            seller_names = [r['seller_name'] for r in db.execute('SELECT DISTINCT seller_name FROM orders WHERE seller_name IS NOT NULL AND seller_name<>"" ORDER BY seller_name')]
            settings = {r['key']: r['value'] for r in db.execute('SELECT * FROM settings')}

            op_data = self.get_operational_report_data(db, qs, can_view_financial)

        _, sess = self.session_data()
        csrf_val = esc((sess or {}).get('csrf') or '')

        f = op_data['filters']
        cfg = op_data.get('config', {})
        totals = op_data['totals']
        drivers = op_data['drivers']
        vehicles = op_data.get('vehicles', [])
        unassigned_vehicle = op_data.get('unassigned_vehicle')
        routes = op_data['routes']
        cities = op_data['cities']
        problems = op_data['problems']
        orders = op_data['orders']

        def chk(name, def_val='1'):
            return 'checked' if cfg.get(name, def_val) == '1' else ''

        alert_msg = ''
        if qs.get('saved'):
            alert_msg = '<div class="alert success" style="margin-bottom:1rem;">✅ Preferências salvas com sucesso!</div>'
        elif qs.get('report_generated'):
            fn = esc(qs.get('filename', ['relatório'])[0])
            alert_msg = f'<div class="alert success" style="margin-bottom:1rem;">⚡ <b>Relatório gerado com sucesso!</b><br>Arquivo salvo em <b>Desktop\\Relatório Logística Casa do Campo\\{fn}</b></div>'

        route_opts = option(route_names, f['route'], True, 'Todas as rotas')
        city_opts = option(city_names, f['city'], True, 'Todas as cidades')
        driver_opts = option(driver_names, f['driver'], True, 'Todos os motoristas')
        vehicle_opts = option(vehicle_names, f.get('vehicle', ''), True, 'Todos os caminhões')
        seller_opts = option(seller_names, f['seller'], True, 'Todos os vendedores')

        date_type_opts = ''.join([
            f'<option value="operational" {"selected" if f["date_type"]=="operational" else ""}>Data Operacional (Carga / Entrega / Expedição) - Padrão</option>',
            f'<option value="delivery" {"selected" if f["date_type"]=="delivery" else ""}>Data da Entrega / Acerto (Apenas finalizadas)</option>',
            f'<option value="route" {"selected" if f["date_type"]=="route" else ""}>Data da Carga / Rota</option>',
            f'<option value="sale" {"selected" if f["date_type"]=="sale" else ""}>Data da Venda (Comercial / ERP)</option>',
            f'<option value="expected" {"selected" if f["date_type"]=="expected" else ""}>Previsão de Entrega (SLA)</option>',
        ])

        def chk_op(name, def_val='1'):
            return 'checked' if cfg.get(name, def_val) == '1' else ''

        active_op_cnt = sum(1 for k in cfg if cfg.get(k) == '1' and not k.startswith('sec_'))

        val_op_kpi_chk = f'<label class="rep-chk-item"><input type="checkbox" name="kpi_total_value" value="1" {chk_op("kpi_total_value")}> <span>💰 Faturamento Total</span></label>' if can_view_financial else ''
        val_op_col_chk = f'<label class="rep-chk-item"><input type="checkbox" name="col_financial" value="1" {chk_op("col_financial")}> <span>💰 Faturamento / Valor</span></label>' if can_view_financial else ''

        customizer_op_html = f"""
        <details class="rep-customizer-panel">
          <summary class="rep-customizer-summary">
            <div class="rep-customizer-summary-left">
              <span>⚙️</span>
              <span>Personalizar Exibição do Relatório Operacional (Cards, Seções e Colunas)</span>
              <span class="rep-customizer-badge">{active_op_cnt} opções ativas</span>
            </div>
            <span class="rep-customizer-hint">Clique para expandir/recolher opções de exportação e tela</span>
          </summary>
          <div class="rep-customizer-content">
            <div class="rep-customizer-toolbar">
              <button type="button" class="rep-tool-btn" onclick="setAllCheckboxes('opReportForm', true)">☑️ Marcar Tudo</button>
              <button type="button" class="rep-tool-btn" onclick="setAllCheckboxes('opReportForm', false)">⬜ Desmarcar Tudo</button>
              <button type="button" class="rep-tool-btn" onclick="resetDefaultCheckboxes('opReportForm')">🔄 Padrão Recomendado</button>
              <button type="button" class="rep-tool-btn save" onclick="saveRepPreferences('opReportForm', 'operacional')">💾 Salvar Minhas Preferências</button>
            </div>
            <div class="rep-customizer-grid">
              <div class="rep-cust-card">
                <div class="rep-cust-card-title"><span>🎯</span> Cards de Resumo (KPIs)</div>
                <div class="rep-cust-card-body">
                  <label class="rep-chk-item"><input type="checkbox" name="kpi_total_orders" value="1" {chk_op('kpi_total_orders')}> <span>📦 Total de Pedidos</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="kpi_total_weight" value="1" {chk_op('kpi_total_weight')}> <span>⚖️ Peso Total</span></label>
                  {val_op_kpi_chk}
                  <label class="rep-chk-item"><input type="checkbox" name="kpi_sla_rate" value="1" {chk_op('kpi_sla_rate')}> <span>🎯 Pontualidade SLA (%)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="kpi_delivered_ontime" value="1" {chk_op('kpi_delivered_ontime')}> <span>🟢 Entregues no Prazo</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="kpi_delivered_late" value="1" {chk_op('kpi_delivered_late')}> <span>🔴 Entregues Fora Prazo</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="kpi_pending_late" value="1" {chk_op('kpi_pending_late')}> <span>⚠️ Abertos Atrasados</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="kpi_avg_days" value="1" {chk_op('kpi_avg_days')}> <span>⏱️ Média Dias Entrega</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="kpi_lead_times" value="1" {chk_op('kpi_lead_times')}> <span>⏳ Lead Times & Gargalos</span></label>
                </div>
              </div>
              <div class="rep-cust-card">
                <div class="rep-cust-card-title"><span>📑</span> Seções do Relatório</div>
                <div class="rep-cust-card-body">
                  <label class="rep-chk-item"><input type="checkbox" name="sec_bottlenecks" value="1" {chk_op('sec_bottlenecks')}> <span>🎯 Diagnóstico de Gargalos (Comercial vs Logística)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="sec_drivers" value="1" {chk_op('sec_drivers')}> <span>👤 1. Desempenho por Motorista</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="sec_vehicles" value="1" {chk_op('sec_vehicles')}> <span>🚚 2. Desempenho por Caminhão</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="sec_routes" value="1" {chk_op('sec_routes')}> <span>🗺️ 3. Desempenho por Rota</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="sec_cities" value="1" {chk_op('sec_cities')}> <span>🏙️ 4. Desempenho por Cidade</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="sec_problems" value="1" {chk_op('sec_problems')}> <span>⚠️ 5. Ocorrências & Problemas</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="sec_orders" value="1" {chk_op('sec_orders', '0')}> <span>📋 6. Listagem Operacional</span></label>
                </div>
              </div>
              <div class="rep-cust-card">
                <div class="rep-cust-card-title"><span>📋</span> Colunas Analíticas</div>
                <div class="rep-cust-card-body">
                  {val_op_col_chk}
                  <label class="rep-chk-item"><input type="checkbox" name="col_weight" value="1" {chk_op('col_weight')}> <span>⚖️ Peso (kg)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="col_avg_days" value="1" {chk_op('col_avg_days')}> <span>⏱️ Média Dias</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="col_lead_times" value="1" {chk_op('col_lead_times')}> <span>⏳ Tempos por Etapa (Lead Times)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="col_sla" value="1" {chk_op('col_sla')}> <span>🎯 Pontualidade SLA</span></label>
                </div>
              </div>
            </div>
          </div>
        </details>
        """

        filter_form = f"""
        <div class="rep-top-bar">
          <div class="rep-nav-group">
            <a href="/relatorios?tab=operacional" class="rep-nav-item active"><span>📊</span> Desempenho Operacional & SLA</a>
            <a href="/relatorios?tab=pedidos" class="rep-nav-item"><span>📦</span> Relatório de Pedidos</a>
            <a href="/relatorios?tab=motoristas" class="rep-nav-item"><span>🚚</span> Relatório de Motoristas</a>
          </div>
          <div class="rep-top-actions">
            <button type="submit" form="opReportForm" formaction="/relatorios/operacional/export-pdf" class="rep-btn-cta-pdf" title="Baixar relatório formatado para impressão em PDF">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>
              <span>📄 Emitir Relatório PDF</span>
            </button>
            <button type="submit" form="opReportForm" formaction="/relatorios/operacional/export-csv" class="rep-btn-cta-csv" title="Exportar dados consolidados em planilha CSV">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
              <span>📊 Exportar Planilha (CSV)</span>
            </button>
          </div>
        </div>

        <div class="rep-card">
          <form id="opReportForm" method="get" action="/relatorios">
            <input type="hidden" name="tab" value="operacional">
            <input type="hidden" name="filtered" value="1">
            <input type="hidden" name="customizer_present" value="1">
            <input type="hidden" name="_csrf" value="{csrf_val}">
            
            <div class="rep-filter-header">
              <div class="rep-presets">
                <span class="rep-presets-title">Atalhos Rápidos:</span>
                <button type="button" class="rep-preset-pill" onclick="setRepDates('today', 'opReportForm')">Hoje</button>
                <button type="button" class="rep-preset-pill" onclick="setRepDates('7days', 'opReportForm')">Últimos 7 dias</button>
                <button type="button" class="rep-preset-pill active" onclick="setRepDates('this_month', 'opReportForm')">Este Mês</button>
                <button type="button" class="rep-preset-pill" onclick="setRepDates('last_month', 'opReportForm')">Mês Anterior</button>
                <button type="button" class="rep-preset-pill" onclick="setRepDates('open_orders', 'opReportForm')">📦 Todos em Aberto (Ativos)</button>
              </div>
              <div class="rep-active-badge">
                <span>Filtro ativo: <b>{esc(op_data['filter_desc'])}</b></span>
              </div>
            </div>

            <div class="rep-grid-filters">
              <div class="rep-input-group">
                <label>Data Inicial</label>
                <input type="date" name="start" value="{esc(f['start'])}" required>
              </div>
              <div class="rep-input-group">
                <label>Data Final</label>
                <input type="date" name="end" value="{esc(f['end'])}" required>
              </div>
              <div class="rep-input-group">
                <label>Filtrar período por</label>
                <select name="date_type">{date_type_opts}</select>
              </div>
              <div class="rep-input-group">
                <label>Motorista</label>
                <select name="driver">{driver_opts}</select>
              </div>
              <div class="rep-input-group">
                <label>Caminhão / Veículo</label>
                <select name="vehicle">{vehicle_opts}</select>
              </div>
              <div class="rep-input-group">
                <label>Rota</label>
                <select name="route">{route_opts}</select>
              </div>
              <div class="rep-input-group">
                <label>Status Operacional</label>
                <select name="status">
                  <option value="" {"selected" if not f.get('status') else ""}>Todas as cargas ativas</option>
                  <option value="finalized" {"selected" if f.get('status')=='finalized' else ""}>Apenas Finalizadas (Entregues)</option>
                  <option value="open" {"selected" if f.get('status')=='open' else ""}>Em Aberto / Em Rota</option>
                  <option value="all_with_canceled" {"selected" if f.get('status')=='all_with_canceled' else ""}>Incluir Canceladas</option>
                </select>
              </div>
            </div>

            {customizer_op_html}

            <div style="display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap;">
              <button type="submit" class="btn primary" style="font-weight:700;padding:8px 22px;font-size:13.5px;display:inline-flex;align-items:center;gap:6px;">
                🔍 Aplicar Filtros
              </button>
              <a href="/relatorios?tab=operacional" class="btn ghost" style="padding:8px 16px;font-size:13px;display:inline-flex;align-items:center;">
                ↺ Limpar Filtros
              </a>
            </div>
          </form>
        </div>
        """

        # Opção expansiva de Agendamento Automático
        r_enabled = settings.get('pdf_report_enabled', '1')
        r_days = settings.get('pdf_report_days', 'Mon,Wed,Fri')
        r_time = settings.get('pdf_report_time', '09:00')

        agendador_accordion = f"""
        <details class='rep-schedule-details'>
          <summary>⚙️ Agendamento Automático deste Relatório na Área de Trabalho (Opcional)</summary>
          <div style="padding-top:12px;">
            <form method="post" action="/relatorios/settings" class="form" style="margin:0;">
              <input type="hidden" name="_csrf" value="{csrf_val}">
              <input type="hidden" name="origin_tab" value="operacional">
              <div class="grid3">
                <label>Status do Agendador
                  <select name="pdf_report_enabled">
                    <option value="1" {'selected' if r_enabled=='1' else ''}>Ativo (Gerar nos horários programados)</option>
                    <option value="0" {'selected' if r_enabled=='0' else ''}>Inativo</option>
                  </select>
                </label>
                <label>Dias de Execução
                  <select name="pdf_report_days">
                    <option value="Mon,Wed,Fri" {'selected' if r_days=='Mon,Wed,Fri' else ''}>Segunda, Quarta e Sexta</option>
                    <option value="ALL" {'selected' if r_days=='ALL' else ''}>Todos os dias (Diário)</option>
                    <option value="Mon" {'selected' if r_days=='Mon' else ''}>Apenas Segunda-feira</option>
                    <option value="Fri" {'selected' if r_days=='Fri' else ''}>Apenas Sexta-feira</option>
                  </select>
                </label>
                <label>Horário de Emissão (24h)
                  <input type="time" name="pdf_report_time" value="{esc(r_time)}" required>
                </label>
              </div>
              <p class="muted" style="margin:8px 0;">Os relatórios gerados automaticamente serão salvos na pasta <b>Desktop\\Relatório Logística Casa do Campo</b> neste computador.</p>
              <div style="margin-top:10px;">
                <button type="submit" class="btn primary" style="padding:6px 16px;font-size:13px;">💾 Salvar Agendamento</button>
              </div>
            </form>
          </div>
        </details>
        """

        sla_rate = totals.get('sla_rate') if totals.get('sla_rate') is not None else 100.0
        tot_weight_kg = totals.get('total_weight') or 0.0
        tot_value = totals.get('total_value') or 0.0
        weight_display = f"{fmt_num(tot_weight_kg)} kg" + (f" ({tot_weight_kg/1000:.2f} t)" if tot_weight_kg >= 1000 else "")
        sla_kpi_cls = 'green' if sla_rate >= 90 else ('amber' if sla_rate >= 75 else 'red')

        # Montagem dinâmica dos Cards KPI conforme preferências do usuário
        op_kpi_cards = []
        if cfg.get('kpi_total_orders', '1') == '1':
            op_kpi_cards.append(f"""
              <div class="rep-kpi blue">
                <div class="rep-kpi-lbl"><span>📦</span> Total de Pedidos</div>
                <div class="rep-kpi-val">{totals.get("total_orders", 0) or 0}</div>
                <div class="rep-kpi-sub">pedidos no período</div>
              </div>
            """)
        if cfg.get('kpi_total_weight', '1') == '1':
            op_kpi_cards.append(f"""
              <div class="rep-kpi blue">
                <div class="rep-kpi-lbl"><span>⚖️</span> Peso Transportado</div>
                <div class="rep-kpi-val">{weight_display}</div>
                <div class="rep-kpi-sub">volume total movimentado</div>
              </div>
            """)
        if can_view_financial and cfg.get('kpi_total_value', '1') == '1':
            op_kpi_cards.append(f"""
              <div class="rep-kpi blue">
                <div class="rep-kpi-lbl"><span>💰</span> Faturamento Total</div>
                <div class="rep-kpi-val">{money_visible(can_view_financial, totals.get("total_value", 0) or 0)}</div>
                <div class="rep-kpi-sub">valor financeiro global</div>
              </div>
            """)
        if cfg.get('kpi_sla_rate', '1') == '1':
            op_kpi_cards.append(f"""
              <div class="rep-kpi {sla_kpi_cls}">
                <div class="rep-kpi-lbl"><span>🎯</span> Pontualidade SLA</div>
                <div class="rep-kpi-val" style="color:{'#15803d' if sla_rate>=90 else ('#b45309' if sla_rate>=75 else '#b91c1c')};">{sla_rate:.1f}%</div>
                <div class="rep-kpi-sub">{totals.get("delivered_ontime", 0) or 0} no prazo / {totals.get("delivered_late", 0) or 0} atraso</div>
              </div>
            """)
        if cfg.get('kpi_delivered_ontime', '1') == '1':
            op_kpi_cards.append(f"""
              <div class="rep-kpi green">
                <div class="rep-kpi-lbl"><span>🟢</span> Entregues no Prazo</div>
                <div class="rep-kpi-val">{totals.get("delivered_ontime", 0) or 0}</div>
                <div class="rep-kpi-sub">entregas pontuais</div>
              </div>
            """)
        if cfg.get('kpi_delivered_late', '1') == '1':
            op_kpi_cards.append(f"""
              <div class="rep-kpi {'red' if (totals.get('delivered_late') or 0)>0 else 'green'}">
                <div class="rep-kpi-lbl"><span>🔴</span> Entregues Fora do Prazo</div>
                <div class="rep-kpi-val">{totals.get("delivered_late", 0) or 0}</div>
                <div class="rep-kpi-sub">entregas atrasadas</div>
              </div>
            """)
        if cfg.get('kpi_pending_late', '1') == '1':
            op_kpi_cards.append(f"""
              <div class="rep-kpi {'red' if (totals.get('pending_late') or 0)>0 else 'amber'}">
                <div class="rep-kpi-lbl"><span>⚠️</span> Abertos Atrasados</div>
                <div class="rep-kpi-val">{totals.get("pending_late", 0) or 0}</div>
                <div class="rep-kpi-sub">em rota / armazém atrasados</div>
              </div>
            """)
        if cfg.get('kpi_avg_days', '1') == '1':
            op_kpi_cards.append(f"""
              <div class="rep-kpi blue">
                <div class="rep-kpi-lbl"><span>⏱️</span> Média Venda-Entrega</div>
                <div class="rep-kpi-val">{fmt_num(totals.get("avg_days_delivery", 0), 1) if totals.get("avg_days_delivery") is not None else "—"} dia(s)</div>
                <div class="rep-kpi-sub">tempo médio de ciclo</div>
              </div>
            """)
        if cfg.get('kpi_lead_times', '1') == '1':
            s2i_val = f"{totals.get('avg_days_sale_to_inv', 0):.1f} dia(s)" if totals.get('avg_days_sale_to_inv') is not None else "—"
            i2d_val = f"{totals.get('avg_days_inv_to_del', 0):.1f} dia(s)" if totals.get('avg_days_inv_to_del') is not None else "—"
            bot_lbl = totals.get('bottleneck_label', 'Equilibrado')
            bot_col = '#dc2626' if bot_lbl == 'Logística / Expedição' else ('#d97706' if bot_lbl == 'Comercial / Faturamento' else '#16a34a')
            op_kpi_cards.append(f"""
              <div class="rep-kpi blue">
                <div class="rep-kpi-lbl"><span>📝➔🧾</span> Venda ➔ Faturamento</div>
                <div class="rep-kpi-val">{s2i_val}</div>
                <div class="rep-kpi-sub">lead time comercial ({totals.get('pct_time_commercial', 50):.0f}% do ciclo)</div>
              </div>
            """)
            op_kpi_cards.append(f"""
              <div class="rep-kpi green">
                <div class="rep-kpi-lbl"><span>🧾➔🚚</span> Faturamento ➔ Entrega</div>
                <div class="rep-kpi-val">{i2d_val}</div>
                <div class="rep-kpi-sub">lead time logística ({totals.get('pct_time_logistics', 50):.0f}% do ciclo)</div>
              </div>
            """)
            op_kpi_cards.append(f"""
              <div class="rep-kpi {'red' if bot_lbl != 'Equilibrado' else 'green'}">
                <div class="rep-kpi-lbl"><span>🔍</span> Gargalo Predominante</div>
                <div class="rep-kpi-val" style="font-size:16px;color:{bot_col};">{esc(bot_lbl)}</div>
                <div class="rep-kpi-sub">maior tempo de retenção</div>
              </div>
            """)

        kpi_section = ""
        if op_kpi_cards:
            kpi_section = f'<div class="rep-kpis-container">{"".join(op_kpi_cards)}</div>'
        if (totals.get('total_orders') or 0) == 0:
            kpi_section += f"""
            <div class="alert info" style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:12px 16px;margin:12px 0;display:flex;align-items:center;gap:12px;color:#1e40af;">
              <span style="font-size:1.3rem;">ℹ️</span>
              <span><b>Nenhum pedido encontrado para o período selecionado ({esc(op_data.get('filter_desc', ''))}).</b><br>
              <small style="color:#2563eb;">💡 Dica: No início do mês ou para auditar períodos anteriores, utilize os botões rápidos no topo: <b>"Últimos 7 dias"</b>, <b>"Mês Anterior"</b> ou <b>"📦 Todos em Aberto"</b>.</small></span>
            </div>
            """

        dr_rows = []
        tot_dr_orders = sum((d.get('total_orders') or 0) for d in drivers)
        tot_dr_del = sum((d.get('delivered_orders') or 0) for d in drivers)
        tot_dr_w = sum((d.get('total_weight') or 0) for d in drivers)
        tot_dr_v = sum((d.get('total_value') or 0) for d in drivers)
        tot_dr_on = sum((d.get('ontime_count') or 0) for d in drivers)
        tot_dr_prob = sum((d.get('problems_count') or 0) for d in drivers)
        overall_dr_sla = (tot_dr_on / (tot_dr_del or 1) * 100) if tot_dr_del > 0 else 100.0

        for d in drivers:
            dr_rate = d.get('sla_rate', 100)
            dr_badge_cls = 'st-entregue' if dr_rate >= 90 else ('st-rota' if dr_rate >= 75 else 'st-problema')
            dr_sla_badge = f'<span class="badge {dr_badge_cls}" style="font-weight:700;">{dr_rate:.1f}%</span>'
            dr_rows.append(
                f'<tr><td><b>{esc(d["driver_name"])}</b></td>'
                f'<td>{d["total_orders"]}</td>'
                f'<td>{d["delivered_orders"]}</td>'
                f'<td>{fmt_num(d["total_weight"])} kg</td>'
                f'<td>{money_visible(can_view_financial, d["total_value"])}</td>'
                f'<td>{(str(d["avg_days"]) + " dia(s)") if d["avg_days"] is not None else "—"}</td>'
                f'<td>{dr_sla_badge} <small class="muted" style="margin-left:4px;">({d["ontime_count"]} ok / {d["late_count"]} atraso)</small></td>'
                f'<td>{d["problems_count"]}</td></tr>'
            )

        unassigned_driver = op_data.get('unassigned_driver')
        unassigned_note = ""
        if unassigned_driver and (unassigned_driver.get('total_orders') or 0) > 0:
            unassigned_note = f"""
            <div style="background:#fffbeb;border:1px solid #fef3c7;border-radius:8px;padding:10px 14px;margin-top:10px;font-size:0.88rem;color:#92400e;display:flex;align-items:center;gap:8px;">
              <span>📦</span>
              <span><b>Pedidos aguardando montagem de carga / motorista:</b> {unassigned_driver['total_orders']} pedido(s) · {fmt_num(unassigned_driver['total_weight'])} kg · {money_visible(can_view_financial, unassigned_driver['total_value'])}</span>
            </div>
            """

        dr_tbody = ''.join(dr_rows) or '<tr><td colspan="8" class="empty">Sem dados de motorista no período.</td></tr>'

        vh_rows = []
        tot_vh_loads = sum((v.get('loads_count') or 0) for v in vehicles)
        tot_vh_orders = sum((v.get('total_orders') or 0) for v in vehicles)
        tot_vh_del = sum((v.get('delivered_orders') or 0) for v in vehicles)
        tot_vh_w = sum((v.get('total_weight') or 0) for v in vehicles)
        tot_vh_v = sum((v.get('total_value') or 0) for v in vehicles)
        tot_vh_on = sum((v.get('ontime_count') or 0) for v in vehicles)
        overall_vh_sla = (tot_vh_on / (tot_vh_del or 1) * 100) if tot_vh_del > 0 else 100.0

        tot_vh_cap = sum(((v.get('loads_count') or 0) * (v.get('capacity_kg') or 0.0)) for v in vehicles)
        overall_vh_occ = (tot_vh_w / tot_vh_cap * 100) if tot_vh_cap > 0 else 0.0

        for v in vehicles:
            v_rate = v.get('sla_rate', 100)
            v_badge_cls = 'st-entregue' if v_rate >= 90 else ('st-rota' if v_rate >= 75 else 'st-problema')
            v_sla_badge = f'<span class="badge {v_badge_cls}" style="font-weight:700;">{v_rate:.1f}%</span>'
            v_occ = v.get('occupation_rate', 0.0)
            v_occ_fill = 'rep-fill-green' if v_occ >= 70 else ('rep-fill-amber' if v_occ >= 40 else 'rep-fill-red')
            v_occ_color = '#15803d' if v_occ >= 70 else ('#b45309' if v_occ >= 40 else '#64748b')
            vh_rows.append(
                f'<tr><td><b>{esc(v.get("model", v["vehicle_name"]))}</b></td>'
                f'<td><span class="badge neutral" style="font-weight:700;">{esc(v.get("plate", "—"))}</span></td>'
                f'<td>{fmt_num(v.get("capacity_kg", 0))} kg</td>'
                f'<td>{v.get("loads_count", 0)}</td>'
                f'<td>{v["total_orders"]}</td>'
                f'<td>{v["delivered_orders"]}</td>'
                f'<td>{fmt_num(v["total_weight"])} kg</td>'
                f'<td><div style="min-width:90px;"><b style="color:{v_occ_color}">{v_occ:.1f}%</b><div class="rep-progress-track"><div class="rep-progress-fill {v_occ_fill}" style="width:{min(v_occ, 100.0):.1f}%;"></div></div></div></td>'
                f'<td>{money_visible(can_view_financial, v["total_value"])}</td>'
                f'<td>{v_sla_badge} <small class="muted">({v["ontime_count"]} ok / {v["late_count"]} atraso)</small></td></tr>'
            )

        unassigned_vh_note = ""
        if unassigned_vehicle and (unassigned_vehicle.get('total_orders') or 0) > 0:
            unassigned_vh_note = f"""
            <div style="background:#fffbeb;border:1px solid #fef3c7;border-radius:8px;padding:10px 14px;margin-top:10px;font-size:0.88rem;color:#92400e;display:flex;align-items:center;gap:8px;">
              <span>📦</span>
              <span><b>Pedidos aguardando alocação de caminhão / carga:</b> {unassigned_vehicle['total_orders']} pedido(s) · {fmt_num(unassigned_vehicle['total_weight'])} kg · {money_visible(can_view_financial, unassigned_vehicle['total_value'])}</span>
            </div>
            """

        vh_tbody = ''.join(vh_rows) or '<tr><td colspan="10" class="empty">Sem dados de caminhão no período.</td></tr>'

        rt_rows = []
        tot_rt_orders = sum((r.get('total_orders') or 0) for r in routes)
        tot_rt_del = sum((r.get('delivered_orders') or 0) for r in routes)
        tot_rt_w = sum((r.get('total_weight') or 0) for r in routes) or 1
        tot_rt_v = sum((r.get('total_value') or 0) for r in routes)
        for r in routes:
            pct_w = ((r.get('total_weight') or 0) / tot_rt_w * 100)
            r_rate = r.get('sla_rate', 100)
            rt_badge_cls = 'st-entregue' if r_rate >= 90 else ('st-rota' if r_rate >= 75 else 'st-problema')
            rt_sla_badge = f'<span class="badge {rt_badge_cls}" style="font-weight:700;">{r_rate:.1f}%</span>'
            s2i_r_str = f"{r['avg_days_sale_to_inv']:.1f}d" if r.get('avg_days_sale_to_inv') is not None else "—"
            i2d_r_str = f"{r['avg_days_inv_to_del']:.1f}d" if r.get('avg_days_inv_to_del') is not None else "—"
            bot_r = r.get('bottleneck', '—')
            bot_r_badge = f'<span class="badge {"warning" if bot_r=="Comercial" else ("danger" if bot_r=="Logística" else "neutral")}">{esc(bot_r)}</span>' if bot_r != '—' else '<span class="muted">—</span>'
            rt_rows.append(
                f'<tr><td><b>{esc(r["route_name"])}</b></td><td>{r["total_orders"]}</td><td>{r["delivered_orders"]}</td><td>{fmt_num(r["total_weight"])} kg</td><td>{s2i_r_str}</td><td>{i2d_r_str}</td><td>{bot_r_badge}</td><td>{rt_sla_badge}</td></tr>'
            )
        rt_tbody = ''.join(rt_rows) or '<tr><td colspan="8" class="empty">Sem dados de rotas no período.</td></tr>'

        ct_rows = []
        tot_ct_orders = sum((c.get('total_orders') or 0) for c in cities)
        tot_ct_del = sum((c.get('delivered_orders') or 0) for c in cities)
        tot_ct_w = sum((c.get('total_weight') or 0) for c in cities)
        tot_ct_v = sum((c.get('total_value') or 0) for c in cities)
        for c in cities:
            ct_rows.append(
                f'<tr><td><b>{esc(c["city_name"])}</b></td><td>{esc(c.get("route_name","—"))}</td><td>{c["total_orders"]}</td><td>{c["delivered_orders"]}</td><td>{fmt_num(c["total_weight"])} kg</td><td>{money_visible(can_view_financial, c["total_value"])}</td></tr>'
            )
        ct_tbody = ''.join(ct_rows) or '<tr><td colspan="6" class="empty">Sem dados de cidades no período.</td></tr>'

        tot_problems = sum((p.get('c') or 0) for p in problems)
        pb_rows = []
        for p in problems:
            pct_pb = ((p.get('c') or 0) / (tot_problems or 1) * 100)
            pb_rows.append(
                f'<tr><td><b>{esc(p["problem_type"])}</b></td><td>{p["c"]}</td><td>{pct_pb:.1f}%</td></tr>'
            )
        
        problems_html = ""
        if problems:
            problems_html = f"""
            <div class="rep-card">
              <div class="rep-section-title">
                <h3><span>⚠️</span> Ocorrências e Motivos de Problema na Entrega</h3>
                <span class="rep-count-badge" style="background:#fee2e2;color:#b91c1c;">{tot_problems} ocorrência(s)</span>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Motivo da Ocorrência</th><th>Quantidade</th><th>% do Total</th></tr></thead>
                  <tbody>{''.join(pb_rows)}</tbody>
                </table>
              </div>
            </div>
            """
        else:
            problems_html = f"""
            <div class="rep-card" style="background:#f0fdf4;border-color:#bbf7d0;">
              <div style="display:flex;align-items:center;gap:10px;color:#15803d;font-weight:600;">
                <span style="font-size:1.2rem;">✅</span>
                <span><b>Nenhuma ocorrência ou devolução registrada no período selecionado!</b> Operação fluindo perfeitamente.</span>
              </div>
            </div>
            """

        del_rows = []
        for o in orders:
            s2i_str = f"{o['days_sale_to_inv']} d" if o.get('days_sale_to_inv') is not None else "—"
            i2d_str = f"{o['days_inv_to_del']} d" if o.get('days_inv_to_del') is not None else "—"
            tot_d_str = f"{o['days_to_deliver']} d" if o.get('days_to_deliver') is not None else "—"
            bot = o.get('bottleneck_stage', '—')
            if bot == 'Comercial':
                bot_badge = '<span class="badge" style="background:#fef3c7;color:#92400e;border:1px solid #fde68a;">Comercial</span>'
            elif bot == 'Logística':
                bot_badge = '<span class="badge" style="background:#fee2e2;color:#991b1b;border:1px solid #fecaca;">Logística</span>'
            elif bot == 'Equilibrado':
                bot_badge = '<span class="badge" style="background:#dcfce7;color:#166534;border:1px solid #bbf7d0;">Equilibrado</span>'
            elif 'Aguard' in bot:
                bot_badge = f'<span class="badge" style="background:#f1f5f9;color:#475569;border:1px solid #cbd5e1;">{esc(bot)}</span>'
            else:
                bot_badge = '<span class="muted">—</span>'

            del_rows.append(
                f'<tr><td><a class="btn small ghost" href="/orders/{o["id"]}"><b>{esc(o["order_number"])}</b></a></td>'
                f'<td>{esc(o["seller_name"] or "Não informado")}</td>'
                f'<td>{badge(o["status"])}</td>'
                f'<td>{esc(o["route_name"] or "Sem rota")} · {esc(o["city"] or "Sem cidade")}</td>'
                f'<td>{esc(o["driver_name"] or "Sem motorista")}</td>'
                f'<td>{fmt_num(o["weight_kg"])} kg</td>'
                f'<td>{money_visible(can_view_financial, o["total_value"])}</td>'
                f'<td><small>Venda: {brdate(o["sale_date"])}<br>Fat: {brdate(o.get("invoiced_at"))}<br>Entr: {brdate(o["delivered_at"])}</small></td>'
                f'<td><div style="font-size:12px;line-height:1.4;">'
                f'<span>📝➔🧾: <b>{s2i_str}</b></span><br>'
                f'<span>🧾➔🚚: <b>{i2d_str}</b></span><br>'
                f'<span>🏁 Total: <b>{tot_d_str}</b></span></div></td>'
                f'<td>{bot_badge}</td>'
                f'<td>{deadline_pill(o["expected_delivery_date"], o["status"])}</td></tr>'
            )
        del_tbody = ''.join(del_rows) or '<tr><td colspan="11" class="empty">Nenhum pedido no período selecionado.</td></tr>'

        sections_list = []

        # SEÇÃO: DIAGNÓSTICO DE GARGALOS & LEAD TIMES
        if cfg.get('sec_bottlenecks', '1') == '1':
            s2i = totals.get('avg_days_sale_to_inv')
            i2d = totals.get('avg_days_inv_to_del')
            tot_cycle = totals.get('avg_days_delivery')
            pct_comm = totals.get('pct_time_commercial', 50.0)
            pct_log = totals.get('pct_time_logistics', 50.0)
            bot_lbl = totals.get('bottleneck_label', 'Equilibrado')
            bot_desc = totals.get('bottleneck_desc', '')

            p_inv = totals.get('pending_wait_invoice', 0)
            p_inv_days = totals.get('avg_days_waiting_invoice', 0)
            p_del = totals.get('pending_wait_delivery', 0)
            p_del_days = totals.get('avg_days_waiting_delivery', 0)

            pend_alerts = []
            if p_inv > 0:
                pend_alerts.append(f"""
                <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:10px 14px;display:flex;align-items:center;gap:10px;color:#92400e;font-size:12.5px;">
                  <span style="font-size:18px;">⚠️</span>
                  <div><b>Comercial / Faturamento:</b> <b>{p_inv} pedido(s)</b> aguardando emissão de nota (média de <b>{fmt_num(p_inv_days, 1)} dia(s)</b> desde a venda).</div>
                </div>
                """)
            if p_del > 0:
                pend_alerts.append(f"""
                <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:10px 14px;display:flex;align-items:center;gap:10px;color:#1e40af;font-size:12.5px;">
                  <span style="font-size:18px;">🚚</span>
                  <div><b>Logística / Expedição:</b> <b>{p_del} pedido(s)</b> faturados aguardando carga/entrega (média de <b>{fmt_num(p_del_days, 1)} dia(s)</b> desde o faturamento).</div>
                </div>
                """)
            pend_alerts_html = f'<div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(280px, 1fr));gap:10px;margin-top:12px;">{"".join(pend_alerts)}</div>' if pend_alerts else ''

            sections_list.append(f"""
            <div class="rep-card" style="border-left:4px solid #3b82f6;">
              <div class="rep-section-title">
                <h3><span>🎯</span> Diagnóstico de Gargalos & Lead Times (Comercial vs Logística)</h3>
                <span class="badge {'danger' if bot_lbl != 'Equilibrado' else 'success'}" style="font-size:12px;font-weight:700;">Gargalo: {esc(bot_lbl)}</span>
              </div>
              <p style="font-size:13px;color:#475569;margin-top:-6px;margin-bottom:14px;">
                Identificação precisa de onde os pedidos passam mais tempo retidos: na <b>etapa comercial/faturamento</b> ou na <b>etapa logística/expedição/transporte</b>.
              </p>
              
              <div class="rep-lead-grid">
                <div class="rep-lead-card" style="border-left:3px solid #3b82f6;">
                  <div class="rep-lead-title"><span style="color:#3b82f6;">📝➔🧾</span> Etapa 1: Venda ➔ Faturamento</div>
                  <div class="rep-lead-val" style="color:#1d4ed8;">{f"{s2i:.1f} dia(s)" if s2i is not None else "—"}</div>
                  <div class="rep-lead-sub">Representa <b>{pct_comm:.1f}%</b> do ciclo total do pedido</div>
                  <small class="muted" style="display:block;margin-top:4px;">Emissão de nota fiscal e liberação comercial/financeira.</small>
                </div>
                <div class="rep-lead-card" style="border-left:3px solid #10b981;">
                  <div class="rep-lead-title"><span style="color:#10b981;">🧾➔🚚</span> Etapa 2: Faturamento ➔ Entrega</div>
                  <div class="rep-lead-val" style="color:#047857;">{f"{i2d:.1f} dia(s)" if i2d is not None else "—"}</div>
                  <div class="rep-lead-sub">Representa <b>{pct_log:.1f}%</b> do ciclo total do pedido</div>
                  <small class="muted" style="display:block;margin-top:4px;">Montagem de carga, expedição no armazém e transporte até a fazenda.</small>
                </div>
                <div class="rep-lead-card" style="border-left:3px solid #6366f1;">
                  <div class="rep-lead-title"><span style="color:#6366f1;">⏳</span> Ciclo Total: Venda ➔ Entrega</div>
                  <div class="rep-lead-val" style="color:#4338ca;">{f"{tot_cycle:.1f} dia(s)" if tot_cycle is not None else "—"}</div>
                  <div class="rep-lead-sub">Tempo médio total da experiência do cliente</div>
                  <small class="muted" style="display:block;margin-top:4px;">Prazo total transcorrido desde o fechamento da venda.</small>
                </div>
              </div>

              <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:14px;margin-top:8px;">
                <div style="display:flex;justify-content:space-between;align-items:center;font-size:12px;font-weight:700;color:#334155;margin-bottom:4px;">
                  <span>Proporção do Tempo Gasto por Etapa:</span>
                  <span>Comercial: {pct_comm:.1f}% | Logística: {pct_log:.1f}%</span>
                </div>
                <div class="rep-split-bar">
                  <div class="rep-split-seg-comm" style="width:{pct_comm}%;" title="Comercial / Faturamento: {pct_comm:.1f}%"></div>
                  <div class="rep-split-seg-log" style="width:{pct_log}%;" title="Logística / Expedição: {pct_log:.1f}%"></div>
                </div>
                <div style="display:flex;justify-content:space-between;font-size:11px;color:#64748b;margin-top:2px;">
                  <span>🟦 1. Comercial / Faturamento ({f"{s2i:.1f}d" if s2i is not None else "—"})</span>
                  <span>🟩 2. Logística / Transporte ({f"{i2d:.1f}d" if i2d is not None else "—"})</span>
                </div>
                <div style="margin-top:10px;padding:10px 14px;border-radius:6px;font-size:12.5px;background:#f8fafc;border-left:3px solid {'#dc2626' if bot_lbl != 'Equilibrado' else '#16a34a'};">
                  <b>Conclusão do Diagnóstico:</b> {esc(bot_desc)}
                </div>
              </div>
              {pend_alerts_html}
            </div>
            """)

        # SEÇÃO 1: MOTORISTAS
        if cfg.get('sec_drivers', '1') == '1':
            sections_list.append(f"""
            <div class="rep-card">
              <div class="rep-section-title">
                <h3><span>🚛</span> Desempenho por Motorista</h3>
                <span class="rep-count-badge">{len(drivers)} motoristas</span>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Motorista</th><th>Pedidos</th><th>Entregues</th><th>Peso Transportado</th><th>Faturamento</th><th>Média Venda-Entrega</th><th>Pontualidade SLA</th><th>Ocorrências</th></tr></thead>
                  <tbody>{dr_tbody}</tbody>
                  <tfoot>
                    <tr style="font-weight:bold;background:#f8fafc;">
                      <td>Total ({len(drivers)} motoristas)</td>
                      <td>{tot_dr_orders}</td>
                      <td>{tot_dr_del}</td>
                      <td>{fmt_num(tot_dr_w)} kg</td>
                      <td>{money_visible(can_view_financial, tot_dr_v)}</td>
                      <td>—</td>
                      <td>{overall_dr_sla:.1f}%</td>
                      <td>{tot_dr_prob}</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
              {unassigned_note}
            </div>
            """)

        # SEÇÃO 2: CAMINHÕES
        if cfg.get('sec_vehicles', '1') == '1':
            sections_list.append(f"""
            <div class="rep-card">
              <div class="rep-section-title">
                <h3><span>🚚</span> Desempenho por Caminhão / Veículo</h3>
                <span class="rep-count-badge">{len(vehicles)} veículos</span>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Caminhão / Modelo</th><th>Placa</th><th>Capacidade</th><th>Cargas</th><th>Pedidos</th><th>Entregues</th><th>Peso Transportado</th><th>% Ocupação Nominal</th><th>Faturamento</th><th>Pontualidade SLA</th></tr></thead>
                  <tbody>{vh_tbody}</tbody>
                  <tfoot>
                    <tr style="font-weight:bold;background:#f8fafc;">
                      <td colspan="2">Total ({len(vehicles)} veículos)</td>
                      <td>—</td>
                      <td>{tot_vh_loads}</td>
                      <td>{tot_vh_orders}</td>
                      <td>{tot_vh_del}</td>
                      <td>{fmt_num(tot_vh_w)} kg</td>
                      <td>{overall_vh_occ:.1f}%</td>
                      <td>{money_visible(can_view_financial, tot_vh_v)}</td>
                      <td>{overall_vh_sla:.1f}%</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
              {unassigned_vh_note}
            </div>
            """)

        # SEÇÃO 3: ROTAS E CIDADES
        show_rt = cfg.get('sec_routes', '1') == '1'
        show_ct = cfg.get('sec_cities', '1') == '1'
        if show_rt or show_ct:
            rt_sec_html = f"""
            <section class="panel" style="margin: 0; padding: 0; border: none; box-shadow: none;">
              <h4 style="margin: 0 0 10px; color:#334155;">Rotas Atendidas ({len(routes)})</h4>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Rota</th><th>Pedidos</th><th>Entregues</th><th>Peso</th><th>Venda➔Fat</th><th>Fat➔Entr</th><th>Gargalo</th><th>SLA</th></tr></thead>
                  <tbody>{rt_tbody}</tbody>
                  <tfoot>
                    <tr style="font-weight:bold;background:#f8fafc;">
                      <td>Total Rotas</td>
                      <td>{tot_rt_orders}</td>
                      <td>{tot_rt_del}</td>
                      <td>{fmt_num(tot_rt_w)} kg</td>
                      <td>—</td>
                      <td>—</td>
                      <td>—</td>
                      <td>—</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            </section>
            """ if show_rt else ""

            ct_sec_html = f"""
            <section class="panel" style="margin: 0; padding: 0; border: none; box-shadow: none;">
              <h4 style="margin: 0 0 10px; color:#334155;">Cidades Atendidas ({len(cities)})</h4>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Cidade</th><th>Rota Base</th><th>Pedidos</th><th>Entregues</th><th>Peso</th><th>Valor</th></tr></thead>
                  <tbody>{ct_tbody}</tbody>
                  <tfoot>
                    <tr style="font-weight:bold;background:#f8fafc;">
                      <td>Total Cidades</td>
                      <td>—</td>
                      <td>{tot_ct_orders}</td>
                      <td>{tot_ct_del}</td>
                      <td>{fmt_num(tot_ct_w)} kg</td>
                      <td>{money_visible(can_view_financial, tot_ct_v)}</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            </section>
            """ if show_ct else ""

            dash_style = "display:grid;grid-template-columns:repeat(auto-fit, minmax(340px, 1fr));gap:14px;" if (show_rt and show_ct) else ""

            sections_list.append(f"""
            <div class="rep-card">
              <div class="rep-section-title">
                <h3><span>🗺️</span> Desempenho por Rota e Cidade</h3>
              </div>
              <div style="{dash_style}">
                {rt_sec_html}
                {ct_sec_html}
              </div>
            </div>
            """)

        # SEÇÃO 4: OCORRÊNCIAS
        if cfg.get('sec_problems', '1') == '1':
            sections_list.append(problems_html)

        # SEÇÃO 5: PEDIDOS DO PERÍODO
        if cfg.get('sec_orders', '0') == '1':
            sections_list.append(f"""
            <div class="rep-card">
              <div class="rep-section-title">
                <h3><span>📋</span> Listagem Operacional de Pedidos ({len(orders)} exibidos)</h3>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr><th>Pedido</th><th>Vendedor</th><th>Status</th><th>Rota/Cidade</th><th>Motorista</th><th>Peso</th><th>Valor</th><th>Datas (Venda/Fat/Entr)</th><th>Lead Time por Etapa</th><th>Gargalo</th><th>SLA</th></tr>
                  </thead>
                  <tbody>{del_tbody}</tbody>
                </table>
              </div>
            </div>
            """)

        sections_html = "".join(sections_list)

        return f'<div class="rep-shell">{alert_msg}{filter_form}{kpi_section}{sections_html}{agendador_accordion}</div>'

    def render_relatorio_pedidos(self, u, qs, can_view_financial):
        start, end, route_filter, status_filter, date_type, vehicle_filter, driver_filter, q_filter, sla_filter = self.report_filter_values(qs)
        is_customizer_submitted = ('customizer_present' in qs)

        with conn() as db:
            settings = {r['key']: r['value'] for r in db.execute('SELECT * FROM settings')}
            route_names = [r['route_name'] for r in db.execute('SELECT DISTINCT route_name FROM orders WHERE route_name IS NOT NULL AND route_name<>"" ORDER BY route_name')]
            city_names = [r['city'] for r in db.execute('SELECT DISTINCT city FROM orders WHERE city IS NOT NULL AND city<>"" ORDER BY city')]
            seller_names = [r['seller_name'] for r in db.execute('SELECT DISTINCT seller_name FROM orders WHERE seller_name IS NOT NULL AND seller_name<>"" ORDER BY seller_name')]
            driver_names = [r['name'] for r in db.execute('SELECT name FROM drivers WHERE active=1 ORDER BY name')]
            vehicle_rows = db.execute("SELECT id, name, plate FROM vehicles WHERE active=1 ORDER BY name").fetchall()
            vehicle_names = [f"{r['name']} - {r['plate']}" if (r['plate'] and r['plate'].strip()) else r['name'] for r in vehicle_rows]

            ped_report_keys = [
                ('pdf_hdr_total_orders', '1'),
                ('pdf_hdr_total_weight', '1'),
                ('pdf_hdr_total_value', '1' if can_view_financial else '0'),
                ('pdf_hdr_ontime', '1'),
                ('pdf_hdr_late', '1'),
                ('pdf_hdr_risk', '1'),
                ('pdf_hdr_scheduled', '1'),
                ('pdf_hdr_delivered', '1'),
                ('pdf_hdr_problems', '0'),
                ('pdf_hdr_canceled', '0'),
                ('pdf_report_st_venda', '1'),
                ('pdf_report_st_faturado', '1'),
                ('pdf_report_st_rota', '1'),
                ('pdf_report_st_agendado', '1'),
                ('pdf_report_st_acertado', '1'),
                ('pdf_report_st_problema', '1'),
                ('pdf_report_st_cancelado', '0'),
                ('pdf_report_col_financial', '1' if can_view_financial else '0'),
                ('pdf_report_col_weight', '1'),
                ('pdf_report_col_dates', '1'),
                ('pdf_report_col_seller', '1'),
                ('pdf_report_col_receiver', '1'),
                ('sec_ped_kpis', '1'),
                ('sec_ped_breakdown', '1'),
                ('sec_ped_orders', '1'),
            ]
            cfg = {}
            for k, def_v in ped_report_keys:
                if is_customizer_submitted:
                    cfg[k] = '1' if k in qs else '0'
                else:
                    v = settings.get(k)
                    if v is None or v == '':
                        cfg[k] = def_v
                    else:
                        cfg[k] = '1' if str(v).strip().lower() in ('1', 'true', 'on') else '0'

            if not can_view_financial:
                cfg['pdf_report_col_financial'] = '0'
                cfg['pdf_hdr_total_value'] = '0'

            included_statuses = []
            if cfg.get('pdf_report_st_venda') == '1': included_statuses.append('Venda')
            if cfg.get('pdf_report_st_faturado') == '1': included_statuses.append('Faturado')
            if cfg.get('pdf_report_st_carga_montada', '1') == '1': included_statuses.append('Carga Montada')
            if cfg.get('pdf_report_st_rota') == '1': included_statuses.append('Saiu para entrega')
            if cfg.get('pdf_report_st_agendado') == '1': included_statuses.append('Agendado')
            if cfg.get('pdf_report_st_acertado') == '1': included_statuses.extend(['Acertado', 'Entregue'])
            if cfg.get('pdf_report_st_problema') == '1': included_statuses.append('Problema')
            if cfg.get('pdf_report_st_cancelado') == '1': included_statuses.append('Cancelado')

            where_sql, p = self.report_where(start, end, route_filter=route_filter, status_filter=status_filter, alias='o', statuses_in=included_statuses, date_type=date_type, vehicle_filter=vehicle_filter, driver_filter=driver_filter, q=q_filter, sla_filter=sla_filter)

            sla = db.execute(f"""SELECT
                                SUM(CASE WHEN o.status IN ('Acertado','Entregue') AND substr(COALESCE(o.delivered_at,''),1,10)<=o.expected_delivery_date THEN 1 ELSE 0 END) within_sla,
                                SUM(CASE WHEN o.status IN ('Acertado','Entregue') AND substr(COALESCE(o.delivered_at,''),1,10)>o.expected_delivery_date THEN 1 ELSE 0 END) outside_sla,
                                SUM(CASE WHEN o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.expected_delivery_date<? THEN 1 ELSE 0 END) open_late,
                                SUM(CASE WHEN o.status NOT IN ('Acertado','Entregue','Problema','Cancelado') AND o.expected_delivery_date>=? AND julianday(o.expected_delivery_date)-julianday(?)<=2 THEN 1 ELSE 0 END) at_risk,
                                COUNT(*) total_orders,
                                COALESCE(SUM(o.weight_kg),0) total_weight,
                                COALESCE(SUM(o.total_value),0) total_value,
                                SUM(CASE WHEN o.status='Agendado' THEN 1 ELSE 0 END) sched_count,
                                SUM(CASE WHEN o.status IN ('Acertado','Entregue') THEN 1 ELSE 0 END) deliv_count,
                                SUM(CASE WHEN o.status='Problema' THEN 1 ELSE 0 END) prob_count,
                                SUM(CASE WHEN o.status='Cancelado' THEN 1 ELSE 0 END) canc_count
                                FROM orders o {where_sql}""", [today(), today(), today()] + p).fetchone()

            st = db.execute(f'SELECT o.status, COUNT(*) c, COALESCE(SUM(o.weight_kg),0) w, COALESCE(SUM(o.total_value),0) v FROM orders o {where_sql} GROUP BY o.status ORDER BY c DESC', p).fetchall()
            rt = db.execute(f'SELECT COALESCE(o.route_name,"Sem rota") route_name, COUNT(*) c, COALESCE(SUM(o.weight_kg),0) w, COALESCE(SUM(o.total_value),0) v FROM orders o {where_sql} GROUP BY o.route_name ORDER BY c DESC LIMIT 10', p).fetchall()
            orders = self.report_orders_rows(db, start, end, route_filter=route_filter, status_filter=status_filter, limit=150, statuses_in=included_statuses, date_type=date_type, vehicle_filter=vehicle_filter, driver_filter=driver_filter, q=q_filter, sla_filter=sla_filter)

        _, sess = self.session_data()
        csrf_val = esc((sess or {}).get('csrf') or '')

        alert_msg = ''
        if qs.get('saved'):
            alert_msg = '<div class="alert success" style="margin-bottom:1rem;">✅ Preferências salvas com sucesso!</div>'
        elif qs.get('report_generated'):
            gen_file = esc(qs.get('filename', ['relatório'])[0])
            alert_msg = f'<div class="alert success" style="margin-bottom:1rem;">⚡ <b>Relatório PDF gerado com sucesso!</b><br>Arquivo salvo em <b>Desktop\\Relatório Logística Casa do Campo\\{gen_file}</b></div>'

        route_opts = option(route_names, route_filter, True, 'Todas as rotas')
        vehicle_opts = option(vehicle_names, vehicle_filter, True, 'Todos os caminhões')
        driver_opts = option(driver_names, driver_filter, True, 'Todos os motoristas')

        status_opts_list = ['<option value="" ' + ('selected' if not status_filter else '') + '>Todos os status</option>']
        for st_val in STATUSES:
            status_opts_list.append(f'<option value="{st_val}" {"selected" if status_filter==st_val else ""}>{st_val}</option>')
        status_opts = ''.join(status_opts_list)

        sla_status_opts = ''.join([
            f'<option value="" {"selected" if not sla_filter else ""}>Todas as situações de prazo</option>',
            f'<option value="ontime" {"selected" if sla_filter=="ontime" else ""}>🟢 No Prazo / Concluído</option>',
            f'<option value="late" {"selected" if sla_filter=="late" else ""}>🔴 Atrasados (SLA Violado)</option>',
            f'<option value="risk" {"selected" if sla_filter=="risk" else ""}>🟡 Em Risco (≤ 2 dias)</option>',
        ])

        ped_date_type_opts = ''.join([
            f'<option value="operational" {"selected" if date_type=="operational" else ""}>Data Operacional (Carga / Entrega) - Padrão</option>',
            f'<option value="delivery" {"selected" if date_type=="delivery" else ""}>Data da Entrega / Acerto (Apenas finalizadas)</option>',
            f'<option value="route" {"selected" if date_type=="route" else ""}>Data da Carga / Rota</option>',
            f'<option value="sale" {"selected" if date_type=="sale" else ""}>Data da Venda (Comercial / ERP)</option>',
            f'<option value="expected" {"selected" if date_type=="expected" else ""}>Previsão de Entrega (SLA)</option>',
        ])

        filter_desc_parts = [f"{brdate(start)} até {brdate(end)}"]
        if q_filter: filter_desc_parts.append(f'Busca: "{q_filter}"')
        if status_filter: filter_desc_parts.append(f'Status: {status_filter}')
        if sla_filter:
            sla_labels = {'ontime': 'No Prazo', 'late': 'Atrasados', 'risk': 'Em Risco'}
            filter_desc_parts.append(f'SLA: {sla_labels.get(sla_filter, sla_filter)}')
        if driver_filter: filter_desc_parts.append(f'Motorista: {driver_filter}')
        if vehicle_filter: filter_desc_parts.append(f'Caminhão: {vehicle_filter}')
        if route_filter: filter_desc_parts.append(f'Rota: {route_filter}')
        active_filter_text = ' · '.join(filter_desc_parts)

        def chk_ped(name):
            return 'checked' if cfg.get(name) == '1' else ''

        active_ped_cnt = sum(1 for k, _ in ped_report_keys if cfg.get(k) == '1' and not k.startswith('sec_'))

        val_ped_kpi_chk = f'<label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_total_value" value="1" {chk_ped("pdf_hdr_total_value")}> <span>💰 Faturamento Total</span></label>' if can_view_financial else ''
        val_ped_col_chk = f'<label class="rep-chk-item"><input type="checkbox" name="pdf_report_col_financial" value="1" {chk_ped("pdf_report_col_financial")}> <span>💰 Faturamento / Valor</span></label>' if can_view_financial else ''

        customizer_ped_html = f"""
        <details class="rep-customizer-panel">
          <summary class="rep-customizer-summary">
            <div class="rep-customizer-summary-left">
              <span>⚙️</span>
              <span>Personalizar Opções do Relatório de Pedidos (Cards, Status, Colunas e Seções)</span>
              <span class="rep-customizer-badge">{active_ped_cnt} opções ativas</span>
            </div>
            <span class="rep-customizer-hint">Clique para expandir/recolher opções de exportação e tela</span>
          </summary>
          <div class="rep-customizer-content">
            <div class="rep-customizer-toolbar">
              <button type="button" class="rep-tool-btn" onclick="setAllCheckboxes('pedReportForm', true)">☑️ Marcar Tudo</button>
              <button type="button" class="rep-tool-btn" onclick="setAllCheckboxes('pedReportForm', false)">⬜ Desmarcar Tudo</button>
              <button type="button" class="rep-tool-btn" onclick="resetDefaultCheckboxes('pedReportForm')">🔄 Padrão Recomendado</button>
              <button type="button" class="rep-tool-btn save" onclick="saveRepPreferences('pedReportForm', 'pedidos')">💾 Salvar Minhas Preferências</button>
            </div>
            <div class="rep-customizer-grid">
              <div class="rep-cust-card">
                <div class="rep-cust-card-title"><span>🎯</span> Cards de Resumo (KPIs)</div>
                <div class="rep-cust-card-body">
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_total_orders" value="1" {chk_ped('pdf_hdr_total_orders')}> <span>📦 Total de Pedidos</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_total_weight" value="1" {chk_ped('pdf_hdr_total_weight')}> <span>⚖️ Peso Total</span></label>
                  {val_ped_kpi_chk}
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_ontime" value="1" {chk_ped('pdf_hdr_ontime')}> <span>🟢 Entregues no Prazo</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_late" value="1" {chk_ped('pdf_hdr_late')}> <span>🔴 Fora Prazo / Atrasados</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_risk" value="1" {chk_ped('pdf_hdr_risk')}> <span>🟡 Em Risco (≤ 2 dias)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_scheduled" value="1" {chk_ped('pdf_hdr_scheduled')}> <span>🔵 Agendados</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_delivered" value="1" {chk_ped('pdf_hdr_delivered')}> <span>✅ Finalizados</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_problems" value="1" {chk_ped('pdf_hdr_problems')}> <span>⚠️ Ocorrências / Problemas</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_hdr_canceled" value="1" {chk_ped('pdf_hdr_canceled')}> <span>❌ Cancelados</span></label>
                </div>
              </div>
              <div class="rep-cust-card">
                <div class="rep-cust-card-title"><span>🚦</span> Status dos Pedidos a Exibir</div>
                <div class="rep-cust-card-body">
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_st_venda" value="1" {chk_ped('pdf_report_st_venda')}> <span>Venda (Comercial)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_st_faturado" value="1" {chk_ped('pdf_report_st_faturado')}> <span>Faturado (Pátio / Expedição)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_st_rota" value="1" {chk_ped('pdf_report_st_rota')}> <span>Saiu para Entrega (Em Rota)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_st_agendado" value="1" {chk_ped('pdf_report_st_agendado')}> <span>Agendado</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_st_acertado" value="1" {chk_ped('pdf_report_st_acertado')}> <span>Entregue / Acertado</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_st_problema" value="1" {chk_ped('pdf_report_st_problema')}> <span>Com Problema</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_st_cancelado" value="1" {chk_ped('pdf_report_st_cancelado')}> <span>Cancelados</span></label>
                </div>
              </div>
              <div class="rep-cust-card">
                <div class="rep-cust-card-title"><span>📋</span> Colunas da Tabela & PDF</div>
                <div class="rep-cust-card-body">
                  {val_ped_col_chk}
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_col_weight" value="1" {chk_ped('pdf_report_col_weight')}> <span>⚖️ Peso (kg)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_col_dates" value="1" {chk_ped('pdf_report_col_dates')}> <span>📅 Datas (Venda/Prev/Entrega)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_col_seller" value="1" {chk_ped('pdf_report_col_seller')}> <span>👤 Vendedor</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="pdf_report_col_receiver" value="1" {chk_ped('pdf_report_col_receiver')}> <span>🏢 Cliente / Fazenda</span></label>
                </div>
              </div>
              <div class="rep-cust-card">
                <div class="rep-cust-card-title"><span>📑</span> Seções do Relatório</div>
                <div class="rep-cust-card-body">
                  <label class="rep-chk-item"><input type="checkbox" name="sec_ped_kpis" value="1" {chk_ped('sec_ped_kpis')}> <span>🎯 1. Cards de Resumo (KPIs)</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="sec_ped_breakdown" value="1" {chk_ped('sec_ped_breakdown')}> <span>📊 2. Resumos por Status e Rota</span></label>
                  <label class="rep-chk-item"><input type="checkbox" name="sec_ped_orders" value="1" {chk_ped('sec_ped_orders')}> <span>📑 3. Listagem Detalhada</span></label>
                </div>
              </div>
            </div>
          </div>
        </details>
        """

        top_bar = f"""
        <div class="rep-top-bar">
          <div class="rep-nav-group">
            <a href="/relatorios?tab=operacional" class="rep-nav-item"><span>📊</span> Desempenho Operacional & SLA</a>
            <a href="/relatorios?tab=pedidos" class="rep-nav-item active"><span>📦</span> Relatório de Pedidos</a>
            <a href="/relatorios?tab=motoristas" class="rep-nav-item"><span>🚚</span> Relatório de Motoristas</a>
          </div>
          <div class="rep-top-actions">
            <button type="submit" form="pedReportForm" formaction="/relatorios/pedidos/export-pdf" class="rep-btn-cta-pdf" title="Baixar relatório de pedidos formatado para impressão em PDF">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>
              <span>📄 Emitir Relatório de Pedidos (PDF)</span>
            </button>
            <button type="submit" form="pedReportForm" formaction="/relatorios/export" class="rep-btn-cta-csv" title="Exportar dados analíticos de pedidos em planilha CSV">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
              <span>📊 Exportar Planilha (CSV)</span>
            </button>
          </div>
        </div>

        <div class="rep-card">
          <form id="pedReportForm" method="get" action="/relatorios">
            <input type="hidden" name="tab" value="pedidos">
            <input type="hidden" name="filtered" value="1">
            <input type="hidden" name="customizer_present" value="1">
            <input type="hidden" name="_csrf" value="{csrf_val}">

            <div class="rep-filter-header">
              <div class="rep-presets">
                <span class="rep-presets-title">Atalhos Rápidos:</span>
                <button type="button" class="rep-preset-pill" onclick="setRepDates('today', 'pedReportForm')">Hoje</button>
                <button type="button" class="rep-preset-pill" onclick="setRepDates('7days', 'pedReportForm')">Últimos 7 dias</button>
                <button type="button" class="rep-preset-pill active" onclick="setRepDates('this_month', 'pedReportForm')">Este Mês</button>
                <button type="button" class="rep-preset-pill" onclick="setRepDates('last_month', 'pedReportForm')">Mês Anterior</button>
                <button type="button" class="rep-preset-pill" onclick="setRepDates('open_orders', 'pedReportForm')">📦 Todos em Aberto (Ativos)</button>
              </div>
              <div class="rep-active-badge">
                <span>Filtro ativo: <b>{esc(active_filter_text)}</b></span>
              </div>
            </div>

            <div style="margin-bottom:14px;">
              <div class="rep-input-group">
                <label style="font-weight:700;color:#1e293b;">🔍 Busca Rápida de Pedidos</label>
                <input type="text" name="q" value="{esc(q_filter)}" placeholder="Buscar por Nº do Pedido, Nota Fiscal (NF), Cliente ou Vendedor..." style="width:100%;height:40px;font-size:14px;padding:8px 12px;border:1px solid #cbd5e1;border-radius:8px;box-sizing:border-box;">
              </div>
            </div>

            <div class="rep-grid-filters">
              <div class="rep-input-group">
                <label>Data Inicial</label>
                <input type="date" name="start" value="{esc(start)}" required>
              </div>
              <div class="rep-input-group">
                <label>Data Final</label>
                <input type="date" name="end" value="{esc(end)}" required>
              </div>
              <div class="rep-input-group">
                <label>Filtrar período por</label>
                <select name="date_type">{ped_date_type_opts}</select>
              </div>
              <div class="rep-input-group">
                <label>Status do Pedido</label>
                <select name="status">{status_opts}</select>
              </div>
              <div class="rep-input-group">
                <label>Situação SLA / Prazo</label>
                <select name="sla_status">{sla_status_opts}</select>
              </div>
              <div class="rep-input-group">
                <label>Motorista</label>
                <select name="driver">{driver_opts}</select>
              </div>
              <div class="rep-input-group">
                <label>Caminhão / Veículo</label>
                <select name="vehicle">{vehicle_opts}</select>
              </div>
              <div class="rep-input-group">
                <label>Rota</label>
                <select name="route">{route_opts}</select>
              </div>
            </div>

            {customizer_ped_html}

            <div style="display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap;">
              <button type="submit" class="btn primary" style="font-weight:700;padding:8px 22px;font-size:13.5px;display:inline-flex;align-items:center;gap:6px;">
                🔍 Aplicar Filtros
              </button>
              <a href="/relatorios?tab=pedidos" class="btn ghost" style="padding:8px 16px;font-size:13px;display:inline-flex;align-items:center;">
                ↺ Limpar Filtros
              </a>
            </div>
          </form>
        </div>
        """

        total_orders = (sla['total_orders'] or 0) if sla else 0
        total_weight = (sla['total_weight'] or 0) if sla else 0
        weight_display = f"{fmt_num(total_weight)} kg" + (f" ({total_weight/1000:.2f} t)" if total_weight >= 1000 else "")
        total_value = (sla['total_value'] or 0) if sla else 0
        ontime_ok = (sla['within_sla'] or 0) if sla else 0
        late_total = ((sla['outside_sla'] or 0) + (sla['open_late'] or 0)) if sla else 0
        at_risk = (sla['at_risk'] or 0) if sla else 0
        deliv_count = (sla['deliv_count'] or 0) if sla else 0
        sched_count = (sla['sched_count'] or 0) if sla else 0
        prob_count = (sla['prob_count'] or 0) if sla else 0
        canc_count = (sla['canc_count'] or 0) if sla else 0

        kpi_cards = []
        if cfg.get('pdf_hdr_total_orders') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi blue">
              <div class="rep-kpi-lbl"><span>📦</span> Total de Pedidos</div>
              <div class="rep-kpi-val">{total_orders}</div>
              <div class="rep-kpi-sub">pedidos localizados</div>
            </div>
            """)
        if cfg.get('pdf_hdr_total_weight') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi blue">
              <div class="rep-kpi-lbl"><span>⚖️</span> Peso Total</div>
              <div class="rep-kpi-val">{weight_display}</div>
              <div class="rep-kpi-sub">volume filtrado</div>
            </div>
            """)
        if can_view_financial and cfg.get('pdf_hdr_total_value') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi blue">
              <div class="rep-kpi-lbl"><span>💰</span> Valor Total</div>
              <div class="rep-kpi-val">{money_visible(can_view_financial, total_value)}</div>
              <div class="rep-kpi-sub">faturamento no filtro</div>
            </div>
            """)
        if cfg.get('pdf_hdr_ontime') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi green">
              <div class="rep-kpi-lbl"><span>🟢</span> Entregues no Prazo</div>
              <div class="rep-kpi-val">{ontime_ok}</div>
              <div class="rep-kpi-sub">SLA cumprido</div>
            </div>
            """)
        if cfg.get('pdf_hdr_late') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi {'red' if late_total > 0 else 'green'}">
              <div class="rep-kpi-lbl"><span>🔴</span> Fora Prazo / Atrasados</div>
              <div class="rep-kpi-val">{late_total}</div>
              <div class="rep-kpi-sub">entregues tarde ou abertos</div>
            </div>
            """)
        if cfg.get('pdf_hdr_risk') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi {'amber' if at_risk > 0 else 'blue'}">
              <div class="rep-kpi-lbl"><span>🟡</span> Em Risco (≤ 2 dias)</div>
              <div class="rep-kpi-val">{at_risk}</div>
              <div class="rep-kpi-sub">próximos do vencimento</div>
            </div>
            """)
        if cfg.get('pdf_hdr_scheduled') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi blue">
              <div class="rep-kpi-lbl"><span>🔵</span> Agendados</div>
              <div class="rep-kpi-val">{sched_count}</div>
              <div class="rep-kpi-sub">com agendamento fixado</div>
            </div>
            """)
        if cfg.get('pdf_hdr_delivered') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi green">
              <div class="rep-kpi-lbl"><span>✅</span> Finalizados</div>
              <div class="rep-kpi-val">{deliv_count}</div>
              <div class="rep-kpi-sub">entregues / acertados</div>
            </div>
            """)
        if cfg.get('pdf_hdr_problems') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi {'red' if prob_count > 0 else 'blue'}">
              <div class="rep-kpi-lbl"><span>⚠️</span> Ocorrências / Problemas</div>
              <div class="rep-kpi-val">{prob_count}</div>
              <div class="rep-kpi-sub">com ocorrência registrada</div>
            </div>
            """)
        if cfg.get('pdf_hdr_canceled') == '1':
            kpi_cards.append(f"""
            <div class="rep-kpi {'red' if canc_count > 0 else 'blue'}">
              <div class="rep-kpi-lbl"><span>❌</span> Cancelados</div>
              <div class="rep-kpi-val">{canc_count}</div>
              <div class="rep-kpi-sub">pedidos cancelados</div>
            </div>
            """)

        kpi_section = f'<div class="rep-kpis-container">{"".join(kpi_cards)}</div>' if (cfg.get('sec_ped_kpis', '1') == '1' and kpi_cards) else ""

        show_breakdown_w = (cfg.get('pdf_report_col_weight') == '1')
        show_breakdown_v = (can_view_financial and cfg.get('pdf_report_col_financial') == '1')

        status_rows_list = []
        for r in st:
            row_tds = [f"<td>{badge(r['status'])}</td>", f"<td><b>{r['c']}</b></td>"]
            if show_breakdown_w:
                row_tds.append(f"<td>{fmt_num(r['w'])} kg</td>")
            if show_breakdown_v:
                row_tds.append(f"<td>{money_visible(can_view_financial, r['v'])}</td>")
            status_rows_list.append(f"<tr>{''.join(row_tds)}</tr>")
        bd_cols_count = 2 + (1 if show_breakdown_w else 0) + (1 if show_breakdown_v else 0)
        status_rows_html = ''.join(status_rows_list) or f'<tr><td colspan="{bd_cols_count}" class="empty">Sem dados para os filtros.</td></tr>'

        route_rows_list = []
        for r in rt:
            row_tds = [f"<td><b>{esc(r['route_name'])}</b></td>", f"<td><b>{r['c']}</b></td>"]
            if show_breakdown_w:
                row_tds.append(f"<td>{fmt_num(r['w'])} kg</td>")
            if show_breakdown_v:
                row_tds.append(f"<td>{money_visible(can_view_financial, r['v'])}</td>")
            route_rows_list.append(f"<tr>{''.join(row_tds)}</tr>")
        route_rows_html = ''.join(route_rows_list) or f'<tr><td colspan="{bd_cols_count}" class="empty">Sem dados para os filtros.</td></tr>'

        breakdown_section = f"""
        <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(340px, 1fr));gap:14px;margin-bottom:14px;">
          <div class="rep-card" style="margin-bottom:0;">
            <div class="rep-section-title">
              <h3><span>📊</span> Resumo por Status</h3>
              <span class="rep-count-badge">{len(st)} status</span>
            </div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Status</th><th>Pedidos</th>{f'<th>Peso (kg)</th>' if show_breakdown_w else ''}{f'<th>Valor</th>' if show_breakdown_v else ''}</tr></thead>
                <tbody>{status_rows_html}</tbody>
              </table>
            </div>
          </div>
          <div class="rep-card" style="margin-bottom:0;">
            <div class="rep-section-title">
              <h3><span>🗺️</span> Resumo por Rota (Top 10)</h3>
              <span class="rep-count-badge">{len(rt)} rotas</span>
            </div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Rota</th><th>Pedidos</th>{f'<th>Peso (kg)</th>' if show_breakdown_w else ''}{f'<th>Valor</th>' if show_breakdown_v else ''}</tr></thead>
                <tbody>{route_rows_html}</tbody>
              </table>
            </div>
          </div>
        </div>
        """ if cfg.get('sec_ped_breakdown', '1') == '1' else ""

        show_rec = (cfg.get('pdf_report_col_receiver') == '1')
        show_sel = (cfg.get('pdf_report_col_seller') == '1')
        show_wt = (cfg.get('pdf_report_col_weight') == '1')
        show_val = (can_view_financial and cfg.get('pdf_report_col_financial') == '1')
        show_dt = (cfg.get('pdf_report_col_dates') == '1')

        del_rows_list = []
        for r in orders:
            is_finished = r['status'] in ('Acertado', 'Entregue')
            deliv_date = (r['delivered_at'] or '')[:10]
            exp_date = r['expected_delivery_date'] or ''
            if is_finished:
                if exp_date and deliv_date:
                    if deliv_date <= exp_date:
                        sla_pill = '<span class="deadline ok">🟢 No Prazo</span>'
                    else:
                        sla_pill = f'<span class="deadline late">🔴 Fora ({brdate(deliv_date)})</span>'
                else:
                    sla_pill = '<span class="deadline ok">Finalizado</span>'
            else:
                sla_pill = deadline_pill(exp_date, r['status'])

            date_col = f"<b>{brdate(r['delivered_at'])}</b>" if is_finished else (f"Prev: {brdate(r['expected_delivery_date'])}" if r['expected_delivery_date'] else "—")
            seller_str = esc(r['seller_name'] or '—')
            client_str = esc(r['client'] or '—')
            nf_str = esc(r['invoice_number'] or '—')
            route_city_str = f"<b>{esc(r['route_name'] or 'Sem rota')}</b><br><small class='muted'>{esc(r['city'] or 'Sem cidade')}</small>"
            driver_veh_str = f"<b>{esc(r['driver'] or 'Sem motorista')}</b><br><small class='muted'>{esc(r['vehicle'] or 'Sem caminhão')}</small>"

            tds = [
                f"<td><a class='btn small ghost' href='/orders/{r['id']}' style='font-weight:700;color:#1e3a8a;'>{esc(r['order_number'])}</a></td>",
                f"<td>{nf_str}</td>"
            ]
            if show_rec:
                tds.append(f"<td><b>{client_str}</b></td>")
            if show_sel:
                tds.append(f"<td>{seller_str}</td>")
            tds.append(f"<td>{route_city_str}</td>")
            tds.append(f"<td>{driver_veh_str}</td>")
            tds.append(f"<td>{badge(r['status'])}</td>")
            if show_wt:
                tds.append(f"<td><b>{fmt_num(r['weight_kg'])} kg</b></td>")
            if show_val:
                tds.append(f"<td>{money_visible(can_view_financial, r['total_value'])}</td>")
            if show_dt:
                tds.append(f"<td>{date_col}</td>")
            tds.append(f"<td>{sla_pill}</td>")

            del_rows_list.append(f"<tr>{''.join(tds)}</tr>")

        th_cols = ["<th>Pedido</th>", "<th>NF</th>"]
        if show_rec: th_cols.append("<th>Cliente</th>")
        if show_sel: th_cols.append("<th>Vendedor</th>")
        th_cols.append("<th>Rota / Cidade</th>")
        th_cols.append("<th>Motorista / Caminhão</th>")
        th_cols.append("<th>Status</th>")
        if show_wt: th_cols.append("<th>Peso</th>")
        if show_val: th_cols.append("<th>Valor</th>")
        if show_dt: th_cols.append("<th>Data Ref.</th>")
        th_cols.append("<th>Situação SLA</th>")
        table_head_html = "".join(th_cols)

        del_rows = ''.join(del_rows_list) or f'<tr><td colspan="{len(th_cols)}" class="empty">Nenhum pedido encontrado para os filtros selecionados.</td></tr>'

        orders_card = f"""
        <div class="rep-card">
          <div class="rep-section-title">
            <h3><span>📑</span> Listagem Detalhada de Pedidos</h3>
            <span class="rep-count-badge">Mostrando {len(orders)} de {total_orders} pedidos</span>
          </div>
          {f'<p class="muted" style="margin: -6px 0 12px 0; font-size: 12.5px;">Mostrando os primeiros {len(orders)} pedidos na tela. Para a relação completa de todos os {total_orders} pedidos, clique em <b>Emitir Relatório de Pedidos (PDF)</b> ou <b>Exportar Planilha (CSV)</b> no topo.</p>' if total_orders > len(orders) else ''}
          <div class="table-wrap">
            <table>
              <thead>
                <tr>{table_head_html}</tr>
              </thead>
              <tbody>
                {del_rows}
              </tbody>
            </table>
          </div>
        </div>
        """ if cfg.get('sec_ped_orders', '1') == '1' else ""

        r_enabled = settings.get('pdf_report_enabled', '1')
        r_days = settings.get('pdf_report_days', 'Mon,Wed,Fri')
        r_time = settings.get('pdf_report_time', '09:00')

        agendador_accordion = f"""
        <details class='rep-schedule-details'>
          <summary>⚙️ Agendamento Automático deste Relatório na Área de Trabalho (Opcional)</summary>
          <div style="padding-top:12px;">
            <form method="post" action="/relatorios/settings" class="form" style="margin:0;">
              <input type="hidden" name="_csrf" value="{csrf_val}">
              <input type="hidden" name="origin_tab" value="pedidos">
              <div class="grid3">
                <label>Status do Agendador
                  <select name="pdf_report_enabled">
                    <option value="1" {'selected' if r_enabled=='1' else ''}>Ativo (Gerar nos horários programados)</option>
                    <option value="0" {'selected' if r_enabled=='0' else ''}>Inativo</option>
                  </select>
                </label>
                <label>Dias de Execução
                  <select name="pdf_report_days">
                    <option value="Mon,Wed,Fri" {'selected' if r_days=='Mon,Wed,Fri' else ''}>Segunda, Quarta e Sexta</option>
                    <option value="ALL" {'selected' if r_days=='ALL' else ''}>Todos os dias (Diário)</option>
                    <option value="Mon" {'selected' if r_days=='Mon' else ''}>Apenas Segunda-feira</option>
                    <option value="Fri" {'selected' if r_days=='Fri' else ''}>Apenas Sexta-feira</option>
                  </select>
                </label>
                <label>Horário de Emissão (24h)
                  <input type="time" name="pdf_report_time" value="{esc(r_time)}" required>
                </label>
              </div>
              <p class="muted" style="margin:8px 0;">Os relatórios gerados automaticamente serão salvos na pasta <b>Desktop\\Relatório Logística Casa do Campo</b> neste computador.</p>
              <div style="margin-top:10px;">
                <button type="submit" class="btn primary" style="padding:6px 16px;font-size:13px;">💾 Salvar Agendamento</button>
              </div>
            </form>
          </div>
        </details>
        """
        return f'<div class="rep-shell">{alert_msg}{top_bar}{kpi_section}{breakdown_section}{orders_card}{agendador_accordion}</div>'

    def render_relatorio_agendador(self, u, qs):
        return self.redirect('/relatorios?tab=operacional')

    def export_csv(self, u):
        qs = parse_qs(urlparse(self.path).query)
        start, end, route_filter, status_filter, date_type, vehicle_filter, driver_filter, q_filter, sla_filter = self.report_filter_values(qs)
        can_view_financial = self.can_view_financial(u)

        path = os.path.join(DATA_DIR, 'relatorio_pedidos.csv')
        with conn() as db, open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(['Pedido', 'Documento', 'Vendedor', 'Cliente', 'Cidade', 'Rota', 'Motorista', 'Caminhão', 'Status', 'Peso_kg', 'Valor', 'Método_pagamento', 'Venda', 'Prazo_limite', 'Entrega', 'Dias_venda_entrega'])
            for r in self.report_orders_rows(db, start, end, route_filter, status_filter, limit=0, date_type=date_type, vehicle_filter=vehicle_filter, driver_filter=driver_filter, q=q_filter, sla_filter=sla_filter):
                veh_val = r['vehicle'] or 'Sem caminhão'
                exported_value = r['total_value'] if can_view_financial else 'Oculto'
                w.writerow([r['order_number'], r['invoice_number'], r['seller_name'], r['client'], r['city'], r['route_name'], r['driver'], veh_val, r['status'], r['weight_kg'], exported_value, r['payment_method'], r['sale_date'], r['expected_delivery_date'], r['delivered_at'], r['days_to_deliver'] if r['days_to_deliver'] is not None else ''])
        return self.send_file(path, 'text/csv')

    def export_operacional_csv(self, u):
        qs = parse_qs(urlparse(self.path).query)
        can_view_financial = self.can_view_financial(u)
        with conn() as db:
            op_data = self.get_operational_report_data(db, qs, can_view_financial)

        totals = op_data['totals']
        drivers = op_data['drivers']
        vehicles = op_data.get('vehicles', [])
        routes = op_data['routes']
        cities = op_data['cities']
        problems = op_data['problems']
        orders = op_data['orders']

        path = os.path.join(DATA_DIR, 'relatorio_operacional_sla.csv')
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(['RELATÓRIO OPERACIONAL & DESEMPENHO SLA - CASA DO CAMPO'])
            w.writerow([f"Filtros: {op_data['filter_desc']}"])
            w.writerow([f"Emissão: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"])
            w.writerow([])

            w.writerow(['--- 1. RESUMO GERAL & SLA ---'])
            w.writerow(['Total de Pedidos', totals.get('total_orders', 0)])
            w.writerow(['Peso Total (kg)', totals.get('total_weight', 0)])
            w.writerow(['Faturamento Total (R$)', totals.get('total_value', 0) if can_view_financial else 'Oculto'])
            w.writerow(['Pontualidade SLA (%)', f"{totals.get('sla_rate', 100):.1f}%"])
            w.writerow(['Entregues no Prazo', totals.get('delivered_ontime', 0)])
            w.writerow(['Entregues Fora do Prazo', totals.get('delivered_late', 0)])
            w.writerow(['Abertos Atrasados', totals.get('pending_late', 0)])
            w.writerow(['Média Venda ➔ Faturamento (dias)', totals.get('avg_days_sale_to_inv', '')])
            w.writerow(['Média Faturamento ➔ Entrega (dias)', totals.get('avg_days_inv_to_del', '')])
            w.writerow(['Média Venda ➔ Entrega (dias)', totals.get('avg_days_delivery', 0)])
            w.writerow(['Gargalo Predominante', totals.get('bottleneck_label', 'Equilibrado')])
            w.writerow(['Diagnóstico do Gargalo', totals.get('bottleneck_desc', '')])
            w.writerow([])

            w.writerow(['--- 2. DESEMPENHO POR MOTORISTA ---'])
            w.writerow(['Motorista', 'Total_Pedidos', 'Entregues', 'Peso_kg', 'Valor_Total', 'Media_Dias_Entrega', 'Pontualidade_SLA_Pct', 'No_Prazo', 'Fora_Prazo', 'Problemas'])
            for d in drivers:
                w.writerow([
                    d['driver_name'],
                    d['total_orders'],
                    d['delivered_orders'],
                    d['total_weight'],
                    d['total_value'] if can_view_financial else 'Oculto',
                    d['avg_days'] if d['avg_days'] is not None else '',
                    f"{d.get('sla_rate', 100):.1f}%",
                    d['ontime_count'],
                    d['late_count'],
                    d['problems_count']
                ])
            unassigned_driver = op_data.get('unassigned_driver')
            if unassigned_driver and unassigned_driver.get('total_orders', 0) > 0:
                w.writerow([
                    'Aguardando Carga / Pátio',
                    unassigned_driver['total_orders'],
                    unassigned_driver['delivered_orders'],
                    unassigned_driver['total_weight'],
                    unassigned_driver['total_value'] if can_view_financial else 'Oculto',
                    '',
                    '—',
                    0,
                    0,
                    0
                ])
            w.writerow([])

            w.writerow(['--- 2.1. DESEMPENHO POR CAMINHÃO / VEÍCULO ---'])
            w.writerow(['Caminhao_Modelo', 'Placa', 'Capacidade_kg', 'Cargas_Realizadas', 'Total_Pedidos', 'Entregues', 'Peso_Transportado_kg', 'Ocupacao_Capacidade_Pct', 'Valor_Total', 'Pontualidade_SLA_Pct', 'No_Prazo', 'Fora_Prazo'])
            for v in vehicles:
                w.writerow([
                    v.get('model', v.get('vehicle_name', '')),
                    v.get('plate', ''),
                    v.get('capacity_kg', 0),
                    v.get('loads_count', 0),
                    v.get('total_orders', 0),
                    v.get('delivered_orders', 0),
                    v.get('total_weight', 0),
                    f"{v.get('occupation_rate', 0):.1f}%",
                    v.get('total_value', 0) if can_view_financial else 'Oculto',
                    f"{v.get('sla_rate', 100):.1f}%",
                    v.get('ontime_count', 0),
                    v.get('late_count', 0),
                ])
            unassigned_vh = op_data.get('unassigned_vehicle')
            if unassigned_vh and unassigned_vh.get('total_orders', 0) > 0:
                w.writerow([
                    'Aguardando Caminhão / Pátio',
                    '',
                    0,
                    0,
                    unassigned_vh.get('total_orders', 0),
                    unassigned_vh.get('delivered_orders', 0),
                    unassigned_vh.get('total_weight', 0),
                    '—',
                    unassigned_vh.get('total_value', 0) if can_view_financial else 'Oculto',
                    '—',
                    0,
                    0
                ])
            w.writerow([])

            w.writerow(['--- 3. DESEMPENHO POR ROTA ---'])
            w.writerow(['Rota', 'Total_Pedidos', 'Entregues', 'Peso_kg', 'Pct_Peso', 'Valor_Total', 'Media_Dias_Venda_Fat', 'Media_Dias_Fat_Entr', 'Gargalo_Rota', 'Pontualidade_SLA_Pct'])
            tot_w = totals.get('total_weight', 0) or 1
            for r in routes:
                pct_w = (r.get('total_weight', 0) / tot_w * 100)
                w.writerow([
                    r['route_name'],
                    r['total_orders'],
                    r['delivered_orders'],
                    r['total_weight'],
                    f"{pct_w:.1f}%",
                    r['total_value'] if can_view_financial else 'Oculto',
                    r.get('avg_days_sale_to_inv', ''),
                    r.get('avg_days_inv_to_del', ''),
                    r.get('bottleneck', '—'),
                    f"{r.get('sla_rate', 100):.1f}%"
                ])
            w.writerow([])

            w.writerow(['--- 4. DESEMPENHO POR CIDADE ---'])
            w.writerow(['Cidade', 'Rota_Base', 'Total_Pedidos', 'Entregues', 'Peso_kg', 'Valor_Total'])
            for c in cities:
                w.writerow([
                    c['city_name'],
                    c.get('route_name', ''),
                    c['total_orders'],
                    c['delivered_orders'],
                    c['total_weight'],
                    c['total_value'] if can_view_financial else 'Oculto'
                ])
            w.writerow([])

            if problems:
                w.writerow(['--- 5. OCORRÊNCIAS & PROBLEMAS ---'])
                w.writerow(['Motivo', 'Quantidade'])
                for p in problems:
                    w.writerow([p['problem_type'], p['c']])
                w.writerow([])

            w.writerow(['--- 6. LISTAGEM ANALÍTICA DOS PEDIDOS (AMOSTRA FILTRADA) ---'])
            w.writerow(['Pedido', 'Documento', 'Vendedor', 'Cliente', 'Cidade', 'Rota', 'Motorista', 'Status', 'Peso_kg', 'Valor', 'Metodo_Pagamento', 'Venda', 'Faturamento', 'Prazo_SLA', 'Entrega', 'Dias_Venda_Fat', 'Dias_Fat_Entr', 'Dias_Venda_Entrega_Total', 'Gargalo_Pedido'])
            for o in orders:
                w.writerow([
                    o['order_number'],
                    o['invoice_number'],
                    o['seller_name'],
                    o['client_name'] if 'client_name' in o.keys() else (o['client'] if 'client' in o.keys() else ''),
                    o['city'],
                    o['route_name'],
                    o['driver_name'],
                    o['status'],
                    o['weight_kg'],
                    o['total_value'] if can_view_financial else 'Oculto',
                    o['payment_method'],
                    o['sale_date'],
                    o.get('invoiced_at', ''),
                    o['expected_delivery_date'],
                    o['delivered_at'],
                    o.get('days_sale_to_inv', ''),
                    o.get('days_inv_to_del', ''),
                    o['days_to_deliver'] if o['days_to_deliver'] is not None else '',
                    o.get('bottleneck_stage', '—')
                ])

        return self.send_file(path, 'text/csv')

    def export_operacional_pdf(self, u):
        qs = parse_qs(urlparse(self.path).query)
        can_view_financial = self.can_view_financial(u)
        with conn() as db:
            op_data = self.get_operational_report_data(db, qs, can_view_financial)
            filepath = pdf_report_service.generate_operational_sla_pdf_report(op_data)
            filename = os.path.basename(filepath)
            audit(db, u, 'Exportou relatório operacional PDF', 'Relatórios', filename, '', f'Salvo em {filepath}')
            db.commit()

        try:
            with open(filepath, 'rb') as f:
                pdf_bytes = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Length', str(len(pdf_bytes)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(pdf_bytes)
            self.mark_request_timing()
        except Exception as exc:
            log_server_error('PDF_OPERATIONAL_DOWNLOAD', exc)
            self.redirect(f'/relatorios?tab=operacional&report_generated=1&filename={quote(filename)}')

    def export_pedidos_pdf(self, u):
        if not self.require_perm(u, 'export_reports', 'Sem permissão para exportar relatórios.'):
            return
        qs = parse_qs(urlparse(self.path).query)
        can_view_financial = self.can_view_financial(u)
        is_customizer_submitted = ('customizer_present' in qs)
        with conn() as db:
            settings = {r['key']: r['value'] for r in db.execute('SELECT * FROM settings')}
            cfg = {}
            cfg['start'] = (qs.get('start', [''])[0] or '').strip()
            cfg['end'] = (qs.get('end', [''])[0] or '').strip()
            cfg['route'] = (qs.get('route', [''])[0] or '').strip()
            cfg['status'] = (qs.get('status', [''])[0] or '').strip()
            cfg['date_type'] = (qs.get('date_type', ['operational'])[0] or 'operational').strip()
            cfg['driver'] = (qs.get('driver', [''])[0] or '').strip()
            cfg['vehicle'] = (qs.get('vehicle', [''])[0] or '').strip()
            cfg['q'] = (qs.get('q', [''])[0] or qs.get('search', [''])[0] or '').strip()
            cfg['sla_status'] = (qs.get('sla_status', [''])[0] or '').strip()

            ped_keys = [
                ('pdf_hdr_total_orders', '1'),
                ('pdf_hdr_total_weight', '1'),
                ('pdf_hdr_total_value', '1' if can_view_financial else '0'),
                ('pdf_hdr_ontime', '1'),
                ('pdf_hdr_late', '1'),
                ('pdf_hdr_risk', '1'),
                ('pdf_hdr_scheduled', '1'),
                ('pdf_hdr_delivered', '1'),
                ('pdf_hdr_problems', '0'),
                ('pdf_hdr_canceled', '0'),
                ('pdf_report_st_venda', '1'),
                ('pdf_report_st_faturado', '1'),
                ('pdf_report_st_rota', '1'),
                ('pdf_report_st_agendado', '1'),
                ('pdf_report_st_acertado', '1'),
                ('pdf_report_st_problema', '1'),
                ('pdf_report_st_cancelado', '0'),
                ('pdf_report_col_financial', '1' if can_view_financial else '0'),
                ('pdf_report_col_weight', '1'),
                ('pdf_report_col_dates', '1'),
                ('pdf_report_col_seller', '1'),
                ('pdf_report_col_receiver', '1'),
            ]
            for k, def_v in ped_keys:
                if is_customizer_submitted:
                    cfg[k] = '1' if k in qs else '0'
                else:
                    v = settings.get(k)
                    if v is None or v == '':
                        cfg[k] = def_v
                    else:
                        cfg[k] = '1' if str(v).strip().lower() in ('1', 'true', 'on') else '0'

            if not can_view_financial:
                cfg['pdf_report_col_financial'] = '0'
                cfg['pdf_hdr_total_value'] = '0'

            filepath = pdf_report_service.generate_orders_pdf_report(db, config=cfg)
            filename = os.path.basename(filepath)
            audit(db, u, 'Exportou relatório de pedidos PDF', 'Relatórios', filename, '', f'Salvo em {filepath}')
            db.commit()

        try:
            with open(filepath, 'rb') as f:
                pdf_bytes = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Length', str(len(pdf_bytes)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(pdf_bytes)
            self.mark_request_timing()
        except Exception as exc:
            log_server_error('PDF_PEDIDOS_DOWNLOAD', exc)
            self.redirect(f'/relatorios?tab=pedidos&report_generated=1&filename={quote(filename)}')

    def post_report_settings(self, u):
        if not (self.has_perm(u, 'manage_settings') or is_admin(u)):
            return self.send_html(layout(u, 'Acesso negado', '<div class="alert danger">Somente perfis autorizados alteram preferências de relatórios.</div>'), 403)
        d = self.post_data()
        origin_tab = (d.get('origin_tab', 'operacional') or 'operacional').strip().lower()
        if origin_tab not in ('operacional', 'pedidos'):
            origin_tab = 'operacional'

        with conn() as db:
            if origin_tab == 'pedidos':
                ped_bool_keys = [
                    'pdf_hdr_total_orders', 'pdf_hdr_total_weight', 'pdf_hdr_total_value',
                    'pdf_hdr_ontime', 'pdf_hdr_late', 'pdf_hdr_risk', 'pdf_hdr_scheduled',
                    'pdf_hdr_delivered', 'pdf_hdr_problems', 'pdf_hdr_canceled',
                    'pdf_report_st_venda', 'pdf_report_st_faturado', 'pdf_report_st_rota',
                    'pdf_report_st_agendado', 'pdf_report_st_acertado', 'pdf_report_st_problema', 'pdf_report_st_cancelado',
                    'pdf_report_col_financial', 'pdf_report_col_weight', 'pdf_report_col_dates',
                    'pdf_report_col_seller', 'pdf_report_col_receiver',
                    'sec_ped_kpis', 'sec_ped_breakdown', 'sec_ped_orders'
                ]
                for k in ped_bool_keys:
                    val = '1' if d.get(k) in ('1', 'on', 'true') else '0'
                    db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', (k, val, now()))
            else:
                op_bool_keys = [
                    'kpi_total_orders', 'kpi_total_weight', 'kpi_total_value', 'kpi_sla_rate',
                    'kpi_delivered_ontime', 'kpi_delivered_late', 'kpi_pending_late', 'kpi_avg_days',
                    'sec_drivers', 'sec_vehicles', 'sec_routes', 'sec_cities', 'sec_problems', 'sec_orders',
                    'col_financial', 'col_weight', 'col_avg_days', 'col_sla'
                ]
                for k in op_bool_keys:
                    val = '1' if d.get(k) in ('1', 'on', 'true') else '0'
                    db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', (k, val, now()))

            if 'pdf_report_enabled' in d:
                db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', ('pdf_report_enabled', '1' if d.get('pdf_report_enabled') == '1' else '0', now()))
            if 'pdf_report_days' in d:
                db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', ('pdf_report_days', d.get('pdf_report_days', 'Mon,Wed,Fri'), now()))
            if 'pdf_report_time' in d:
                db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)', ('pdf_report_time', d.get('pdf_report_time', '09:00'), now()))

            audit(db, u, f'Alterou configurações de relatórios ({origin_tab})', 'Relatórios')
            db.commit()
        return self.redirect(f'/relatorios?tab={quote(origin_tab)}&saved=1')

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
            drivers=db.execute('SELECT * FROM drivers WHERE active=1 ORDER BY name').fetchall()
            edit_target=db.execute('SELECT * FROM users WHERE id=?',(int(edit_uid),)).fetchone() if str(edit_uid).isdigit() else None
            perm_user_target=db.execute('SELECT * FROM users WHERE id=?',(perm_uid,)).fetchone() if perm_uid > 0 else None
            role_perm_rows={r['perm']: int(r['allowed'] or 0) for r in db.execute('SELECT perm,allowed FROM role_permissions WHERE role_name=?',(perm_role,)).fetchall()}
            user_perm_rows={r['perm']: int(r['allowed'] or 0) for r in db.execute('SELECT perm,allowed FROM user_permissions WHERE user_id=?',(perm_uid,)).fetchall()} if perm_user_target else {}
        can_manage_config = self.has_perm(u,'manage_settings') and is_admin(u)
        can_manage_users = self.has_perm(u,'manage_users')
        can_manage_permissions = self.has_perm(u,'manage_permissions') and is_admin(u)
        can_view_audit = is_admin(u) and self.has_perm(u,'view_settings')
        can_manage_reports = self.has_perm(u,'manage_settings') or is_admin(u)
        can_manage_app_config = is_god(u)

        if section == 'reports':
            return self.redirect('/relatorios?tab=agendador')

        allowed_sections = {'profile'}
        if can_manage_config:
            allowed_sections.add('system')
        if can_manage_app_config:
            allowed_sections.add('app_config')
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
        if can_manage_app_config:
            tabs.append(("app_config", "App Mobile (GOD)", "#settings-app_config"))
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
        elif qs.get('report_generated'):
            gen_file = esc(qs.get('filename',['relatório'])[0])
            saved_msg = f'<div class="alert success" style="margin-bottom:1rem;">⚡ <b>Relatório PDF gerado com sucesso!</b><br>Arquivo salvo em <b>Desktop\\Relatório Logística Casa do Campo\\{gen_file}</b></div>'

        # Renderização do painel da aba ativa (APENAS A SEÇÃO SELECIONADA)
        content = settings_tabs

        if section == 'profile':
            if is_restricted_data_entry_user(u):
                content += f'''<section class="panel settings-panel" id="settings-profile"><h2>Minha conta</h2>{saved_msg}<form method="post" action="/settings/profile" class="inline-form"><input type="hidden" name="redirect_section" value="profile"><input type="hidden" name="name" value="{esc(u['name'])}"><input type="hidden" name="username" value="{esc(u['username'])}"><input name="password" type="password" placeholder="Nova senha" required><button>Alterar minha senha</button></form><p class="muted">Para seu perfil operacional, esta área permite somente troca de senha.</p><p class="muted">Último acesso: {brdate(u["last_login_at"])} {esc(str(u["last_login_at"] or "")[11:16]) if u["last_login_at"] else "—"}</p></section>'''
            else:
                content += f'''<section class="panel settings-panel" id="settings-profile"><h2>Minha conta</h2>{saved_msg}<form method="post" action="/settings/profile" class="inline-form"><input type="hidden" name="redirect_section" value="profile"><input name="name" value="{esc(u['name'])}" placeholder="Nome" required><input name="username" value="{esc(u['username'])}" placeholder="Usuário" required><input name="password" type="password" placeholder="Nova senha opcional"><button>Atualizar minha conta</button></form><p class="muted">Último acesso: {brdate(u["last_login_at"])} {esc(str(u["last_login_at"] or "")[11:16]) if u["last_login_at"] else "—"}</p></section>'''

        elif section == 'system' and can_manage_config:
            active_gmap_key = get_google_maps_api_key()
            is_env_key = bool(os.environ.get('GOOGLE_MAPS_API_KEY'))
            gmap_status_text = '🟢 Chave Configurada via .env' if is_env_key else ('🟢 Chave Configurada no Banco' if active_gmap_key else '⚪ Nenhuma Chave Configurada (Usando ESRI/OpenStreetMap)')
            gmap_status_color = '#16a34a' if active_gmap_key else '#64748b'
            gmap_status_bg = '#dcfce7' if active_gmap_key else '#f1f5f9'

            content += f'''<section class="panel settings-panel" id="settings-system"><h2>Parâmetros do sistema</h2><p class="muted" style="margin-bottom:1rem;">📋 O agendamento e personalização de relatórios PDF foram movidos para a aba <a href="/relatorios?tab=agendador" style="color:var(--primary);font-weight:700;">Relatórios → Agendamento PDF</a>.</p><form method="post" action="/settings" class="form"><input type="hidden" name="redirect_section" value="system"><fieldset><legend>Identidade e operação</legend><div class="grid3"><label>Nome do sistema<input name="system_name" value="{esc(settings.get('system_name'))}"></label><label>Nome da empresa<input name="company_name" value="{esc(settings.get('company_name'))}"></label><label>Subtítulo<input name="company_subtitle" value="{esc(settings.get('company_subtitle'))}"></label><label>Cor principal<input type="color" name="primary_color" value="{esc(settings.get('primary_color'))}"></label><label>Cor secundária<input type="color" name="secondary_color" value="{esc(settings.get('secondary_color'))}"></label><label>Cor de apoio<input type="color" name="accent_color" value="{esc(settings.get('accent_color'))}"></label><label>Cor fundo<input type="color" name="background_color" value="{esc(settings.get('background_color','#f6f7f2'))}"></label><label>Prazo SLA oficial<input name="sla_limit_days" value="15" readonly><small>Travado em 15 dias corridos conforme regra operacional.</small></label><label>Capacidade padrão kg<input name="load_capacity_kg" value="{esc(settings.get('load_capacity_kg'))}"></label><label>Modo manutenção<select name="maintenance_mode"><option value="off" {'selected' if settings.get('maintenance_mode','off')!='on' else ''}>Desligado</option><option value="on" {'selected' if settings.get('maintenance_mode')=='on' else ''}>Ligado</option></select></label><label class="full">Observação interna<input name="god_note" value="{esc(settings.get('god_note',''))}" placeholder="Ex: revisão feita em..."></label></div></fieldset>

<fieldset style="margin-top:1.5rem; border:1px solid #93c5fd; background:#f0f9ff; padding:16px; border-radius:8px;">
<legend style="color:#0369a1; font-weight:800; font-size:14px; padding:0 8px;">🗺️ Integração Google Maps (Google Cloud Platform)</legend>
<div style="display:flex; flex-direction:column; gap:12px;">
  <p style="margin:0; font-size:13px; color:#334155;">
    Habilita mapas em alta resolução (Satélite Híbrido com nomes de vias/cidades, Ruas e Terreno) e <b>Geocodificação Automática</b> de endereços rurais/urbanos para fixar pinos GPS no mapa interativo e facilitar a navegação do motorista.
  </p>
  <div>
    <label style="font-weight:700; font-size:13px; display:block; margin-bottom:4px; color:#1e293b;">Chave de API do Google Maps (Google Cloud API Key)</label>
    <div style="display:flex; gap:8px;">
      <input type="password" id="inputGoogleMapsKey" name="google_maps_api_key" value="{esc(settings.get('google_maps_api_key', ''))}" placeholder="AIzaSy..." style="flex:1; font-family:monospace; padding:8px 10px; border:1px solid #cbd5e1; border-radius:6px; font-size:13px;">
      <button type="button" id="btnToggleKeyVis" class="btn ghost small" style="padding:6px 12px; font-size:12px;">👁️ Ver</button>
    </div>
    <small style="color:#64748b; margin-top:3px; display:block;">
      {f'🔒 Chave ativa detectada no arquivo <code>.env</code> (GOOGLE_MAPS_API_KEY).' if is_env_key else 'Pode ser preenchida aqui ou no arquivo <code>.env</code>. Requer as APIs Maps JavaScript API e Geocoding API ativadas no Google Cloud.'}
    </small>
  </div>
  <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; padding-top:10px; border-top:1px solid #bae6fd;">
    <div style="display:flex; align-items:center; gap:8px;">
      <span id="badgeGMapStatus" style="font-size:12px; font-weight:700; padding:4px 10px; border-radius:12px; background:{gmap_status_bg}; color:{gmap_status_color};">
        {gmap_status_text}
      </span>
    </div>
    <button type="button" id="btnTestGoogleMaps" class="btn small" style="background:#0284c7; border-color:#0284c7; color:#fff; font-weight:700; font-size:12px; padding:6px 12px;">
      🔍 Testar Chave no Google Cloud
    </button>
  </div>
  <div id="boxGoogleMapsTestResult" style="display:none; font-size:13px; font-weight:600; padding:10px 14px; border-radius:6px; margin-top:4px;"></div>
</div>
</fieldset>

<script>
(function() {{
  var btnToggle = document.getElementById('btnToggleKeyVis');
  var inputKey = document.getElementById('inputGoogleMapsKey');
  var btnTest = document.getElementById('btnTestGoogleMaps');
  var resBox = document.getElementById('boxGoogleMapsTestResult');

  if (btnToggle && inputKey) {{
    btnToggle.addEventListener('click', function() {{
      if (inputKey.type === 'password') {{
        inputKey.type = 'text';
        btnToggle.textContent = '🔒 Ocultar';
      }} else {{
        inputKey.type = 'password';
        btnToggle.textContent = '👁️ Ver';
      }}
    }});
  }}

  if (btnTest && resBox) {{
    btnTest.addEventListener('click', function() {{
      var keyVal = inputKey ? inputKey.value.trim() : '';
      btnTest.disabled = true;
      btnTest.textContent = '⏳ Testando conexão...';
      resBox.style.display = 'block';
      resBox.style.background = '#f8fafc';
      resBox.style.color = '#334155';
      resBox.style.border = '1px solid #cbd5e1';
      resBox.textContent = 'Conectando à Google Geocoding API...';

      fetch('/api/map/test-key', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ api_key: keyVal }})
      }})
      .then(function(res) {{ return res.json(); }})
      .then(function(data) {{
        btnTest.disabled = false;
        btnTest.textContent = '🔍 Testar Chave no Google Cloud';
        if (data.ok) {{
          resBox.style.background = '#dcfce7';
          resBox.style.color = '#166534';
          resBox.style.border = '1px solid #86efac';
          resBox.textContent = data.message || 'Chave testada e funcionando com sucesso!';
        }} else {{
          resBox.style.background = '#fee2e2';
          resBox.style.color = '#991b1b';
          resBox.style.border = '1px solid #fca5a5';
          resBox.textContent = data.message || 'Falha ao validar chave no Google Cloud.';
        }}
      }})
      .catch(function(err) {{
        btnTest.disabled = false;
        btnTest.textContent = '🔍 Testar Chave no Google Cloud';
        resBox.style.background = '#fee2e2';
        resBox.style.color = '#991b1b';
        resBox.style.border = '1px solid #fca5a5';
        resBox.textContent = 'Erro ao se comunicar com o servidor: ' + err.message;
      }});
    }});
  }}
}})();
</script>
<button class="btn primary" style="margin-top:1rem;">💾 Salvar configurações globais</button></form></section>'''

        elif section == 'app_config' and can_manage_app_config:
            app_title = settings.get('driver_app_title', 'Entregas Casa do Campo')
            app_req_photo = settings.get('driver_app_require_photo', '0')
            app_req_sig = settings.get('driver_app_require_signature', '0')
            app_gps = settings.get('driver_app_gps_mode', 'auto')
            app_sync = settings.get('driver_app_sync_interval_sec', '30')
            app_force_pass = settings.get('driver_app_force_password_change', '1')
            
            drivers_rows = ''.join(f'<tr><td>{esc(d["id"])}</td><td><b>{esc(d["name"])}</b></td><td>{esc(d["phone"] or "—")}</td><td>{esc(d["document"] or "—")}</td><td><a class="btn small ghost" href="/drivers?edit={d["id"]}">Editar Motorista</a></td></tr>' for d in drivers)
            empty_drivers_tr = '<tr><td colspan="5">Nenhum motorista cadastrado.</td></tr>'
            drivers_quick_table = f'<div class="table-wrap"><table><thead><tr><th>ID</th><th>Nome</th><th>Telefone</th><th>CPF</th><th>Ação</th></tr></thead><tbody>{drivers_rows or empty_drivers_tr}</tbody></table></div>'

            content += f'''<section class="panel settings-panel" id="settings-app_config"><h2>📱 Parâmetros Globais e Gerenciamento do Aplicativo Mobile (GOD)</h2><div class="alert info">Acesso exclusivo para perfis <b>GOD</b>. Altera diretamente o comportamento do App do Motorista.</div><form method="post" action="/settings" class="form"><input type="hidden" name="redirect_section" value="app_config"><fieldset><legend>Identidade e Políticas da Entrega Mobile</legend><div class="grid3"><label>Título do aplicativo mobile<input name="driver_app_title" value="{esc(app_title)}"></label><label>Obrigatoriedade de foto do canhoto<select name="driver_app_require_photo"><option value="0" {"selected" if app_req_photo=="0" else ""}>Foto ou Assinatura (Flexível)</option><option value="1" {"selected" if app_req_photo=="1" else ""}>Foto do Canhoto Obrigatória</option></select></label><label>Obrigatoriedade de assinatura digital<select name="driver_app_require_signature"><option value="0" {"selected" if app_req_sig=="0" else ""}>Opcional (Foto ou Assinatura)</option><option value="1" {"selected" if app_req_sig=="1" else ""}>Assinatura Obrigatória</option></select></label><label>Captura de Geolocalização GPS<select name="driver_app_gps_mode"><option value="auto" {"selected" if app_gps=="auto" else ""}>Automática na Confirmação (Recomendado)</option><option value="disabled" {"selected" if app_gps=="disabled" else ""}>Desativada</option></select></label><label>Troca de Senha no 1º Acesso<select name="driver_app_force_password_change"><option value="1" {"selected" if app_force_pass=="1" else ""}>Sim (Exigir alteração no 1º login)</option><option value="0" {"selected" if app_force_pass=="0" else ""}>Não</option></select></label><label>Sincronização em segundo plano (segundos)<input type="number" name="driver_app_sync_interval_sec" value="{esc(app_sync)}" min="5" max="300"></label></div></fieldset><button class="btn primary" style="margin-top:1rem;">💾 Salvar configurações do App Mobile</button></form><div style="margin-top:2rem;border-top:2px solid #e2e8f0;padding-top:1.5rem;"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;"><h3 style="margin:0;color:#1e293b;">🚛 Gerenciamento Rápido de Motoristas</h3><a class="btn primary small" href="/drivers">Gerenciar Motoristas Completo →</a></div>{drivers_quick_table}</div></section>'''

        elif section == 'users' and can_manage_users:
            userform=f'''<form method="post" action="/settings/user" class="inline-form"><input type="hidden" name="redirect_section" value="users"><input name="name" placeholder="Nome" required><input name="username" placeholder="Usuário" required><select name="role">{option(ROLES)}</select><button class="btn primary">Criar usuário</button></form><div class="alert info">Ao criar usuário, a senha inicial será <b>usuario123</b> (ex.: joao = joao123) com troca obrigatória no primeiro login.</div>'''
            edit_panel=''
            if edit_target:
                active_select = f'<select name="active"><option value="1" {"selected" if int(edit_target["active"] or 0)==1 else ""}>Ativo</option><option value="0" {"selected" if int(edit_target["active"] or 0)==0 else ""}>Inativo</option></select>'
                edit_panel=f'''<section class="panel" style="border:2px solid #2563eb;background:#f8fafc;margin-top:1rem;margin-bottom:1rem;"><h2>Editar usuário: {esc(edit_target["name"])} (ID: {edit_target["id"]})</h2><form method="post" action="/settings/user/{edit_target["id"]}/update" class="inline-form"><input type="hidden" name="redirect_section" value="users"><label style="font-weight:600;">Nome:<input name="name" value="{esc(edit_target["name"])}" required></label><label style="font-weight:600;">Login:<input name="username" value="{esc(edit_target["username"])}" required></label><label style="font-weight:600;">Perfil:<select name="role">{option(ROLES,edit_target["role"])}</select></label><label style="font-weight:600;">Status:{active_select}</label><label style="font-weight:600;">Nova senha:<input name="password" type="password" placeholder="Opcional"></label><button class="btn primary">💾 Salvar edição</button><a class="btn ghost" href="/settings?section=users#settings-users">Cancelar</a></form></section>'''
            rows=''
            for x in users:
                is_self = (x['id'] == u['id'])
                edit_link = f'<a class="btn small ghost" href="/settings?section=users&edit_user={x["id"]}#settings-users">Editar</a>'
                if is_self:
                    action = f'<span class="muted small">(Seu usuário)</span>'
                else:
                    action = f'''<div style="display:flex;gap:6px;">{edit_link}<form method="post" action="/settings/user/{x["id"]}/delete" class="inline-mini needs-confirm" data-confirm-text="Confirma desativar este usuário?"><input type="hidden" name="redirect_section" value="users"><button class="danger-btn small">Desativar</button></form></div>'''
                rows += f'<tr><td><b>{esc(x["name"])}</b></td><td>{esc(x["username"])}</td><td>{esc(normalize_role(x["role"]))}</td><td>{"Ativo" if x["active"] else "Inativo"}</td><td>{brdate(x["last_login_at"])}</td><td class="settings-user-actions">{action}</td></tr>'
            content += f'<section class="panel settings-panel" id="settings-users"><h2>Usuários</h2>{saved_msg}{userform}{edit_panel}<div class="table-wrap"><table><thead><tr><th>Nome</th><th>Usuário</th><th>Perfil</th><th>Status</th><th>Último acesso</th><th>Ações</th></tr></thead><tbody>{rows}</tbody></table></div></section>'

        elif section == 'permissions' and can_manage_permissions:
            role_links=' '.join(f'<a class="btn small {"ghost" if perm_role!=r else ""}" href="/settings?section=permissions&perm_role={quote(r)}#settings-permissions">{esc(r)}</a>' for r in ROLES)
            
            # Agrupamento de permissões por módulo em Cards
            modules = {
                "📦 Módulo de Pedidos & Faturamento": ['view_orders', 'create_orders', 'edit_orders', 'cancel_orders', 'invoice_orders'],
                "🚛 Módulo de Cargas & Entregas": ['view_routes', 'create_routes', 'edit_routes', 'settle_routes', 'cancel_routes'],
                "📋 Módulo de Cadastros & Auditoria": ['view_clients', 'view_drivers', 'view_vehicles', 'view_route_catalog', 'view_dashboard', 'view_reports', 'view_sla', 'view_backup', 'view_settings'],
                "⚙️ Módulo de Administração & Sistema": ['manage_settings', 'manage_users', 'manage_permissions']
            }
            perm_dict = dict(PERMISSIONS)
            module_cards = ""
            for mod_title, mod_perms in modules.items():
                mod_rows = ""
                for p_key in mod_perms:
                    if p_key in perm_dict:
                        checked = 'checked' if int(role_perm_rows.get(p_key, 1 if p_key in default_permissions_for_role(perm_role) else 0)) == 1 else ''
                        mod_rows += f'<label class="checkbox-card"><input type="checkbox" name="perm_{esc(p_key)}" value="1" {checked}> {esc(perm_dict[p_key])}</label>'
                module_cards += f'<div class="perm-card"><h4 style="margin:0 0 10px 0;color:#1e293b;border-bottom:1px solid #e2e8f0;padding-bottom:6px;">{mod_title}</h4><div class="checkbox-grid">{mod_rows}</div></div>'

            role_panel=f'''<section class="panel"><h2>Permissões do Perfil: <span style="color:var(--primary);">{esc(perm_role)}</span></h2><div class="action-strip" style="margin-bottom:1rem;">{role_links}</div><form method="post" action="/settings/permissions/role" class="form"><input type="hidden" name="redirect_section" value="permissions"><input type="hidden" name="role_name" value="{esc(perm_role)}"><div class="perm-grid">{module_cards}</div><button class="btn primary" style="margin-top:1.2rem;">💾 Salvar permissões do perfil ({esc(perm_role)})</button></form></section>'''
            content += f'<section class="settings-stack" id="settings-permissions">{role_panel}</section>'

        elif section == 'audit' and can_view_audit:
            logrows=''.join(
                f'<tr><td>{brdate(l["created_at"])} {esc(str(l["created_at"])[11:16])}</td><td><b>{esc(l["user_name"] or "Sistema")}</b></td><td>{esc(l["module"])}</td><td>{esc(l["action"])}</td><td>{esc(l["entity"] or "")}</td><td><small>{esc(l["notes"] or "—")}</small></td></tr>'
                for l in logs
            ) or '<tr><td colspan="6">Sem auditoria ainda.</td></tr>'
            content += f'<section class="panel settings-panel" id="settings-audit"><h2>Auditoria recente</h2><div class="table-wrap"><table><thead><tr><th>Data</th><th>Usuário</th><th>Módulo</th><th>Ação</th><th>Registro</th><th>Detalhes</th></tr></thead><tbody>{logrows}</tbody></table></div></section>'

        return self.send_html(layout(u,'Configurações',content,'Administração do sistema, usuários e permissões'))

    def generate_pdf_report_now(self, u):
        if not (self.has_perm(u, 'manage_settings') or is_admin(u)):
            return self.fail(u, 'Sem permissão', 'Você não tem permissão para gerar relatórios.', 403)
        with conn() as db:
            settings_map = {r['key']: r['value'] for r in db.execute('SELECT * FROM settings')}
            filepath = pdf_report_service.generate_orders_pdf_report(db, config=settings_map)
            filename = os.path.basename(filepath)
            audit(db, u, 'Gerou relatório PDF manual', 'Relatórios', filename, '', f'Salvo em {filepath}')
            db.commit()

        # Envia arquivo diretamente para download no navegador do usuário
        try:
            with open(filepath, 'rb') as f:
                pdf_bytes = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('Content-Length', str(len(pdf_bytes)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(pdf_bytes)
            self.mark_request_timing()
        except Exception as exc:
            log_server_error('PDF_DOWNLOAD', exc)
            self.redirect(f'/relatorios?tab=agendador&report_generated=1&filename={quote(filename)}')

    def post_settings(self,u):
        d=self.post_data()
        redirect_section=(d.get('redirect_section') or 'system').strip().lower()

        if redirect_section == 'app_config':
            if not is_god(u):
                return self.send_html(layout(u,'Acesso negado','<div class="alert danger">Somente perfis GOD alteram configurações do App Mobile.</div>'),403)
            app_keys=['driver_app_title','driver_app_require_photo','driver_app_require_signature','driver_app_gps_mode','driver_app_sync_interval_sec','driver_app_force_password_change']
            with conn() as db:
                for k in app_keys:
                    db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)',(k,d.get(k,''),now()))
                audit(db,u,'Alterou configurações do App Mobile','Configurações'); db.commit()
            return self.redirect('/settings?section=app_config#settings-app_config')

        if redirect_section == 'reports':
            if not (self.has_perm(u,'manage_settings') or is_admin(u)):
                return self.send_html(layout(u,'Acesso negado','<div class="alert danger">Somente perfis autorizados alteram preferências de relatórios.</div>'),403)
            report_keys=[
                'pdf_report_enabled','pdf_report_days','pdf_report_time',
                'pdf_report_st_venda','pdf_report_st_faturado','pdf_report_st_rota','pdf_report_st_agendado','pdf_report_st_acertado','pdf_report_st_problema','pdf_report_st_cancelado',
                'pdf_report_col_financial','pdf_report_col_weight','pdf_report_col_dates','pdf_report_col_seller','pdf_report_col_receiver',
                'pdf_hdr_total_orders','pdf_hdr_total_weight','pdf_hdr_total_value','pdf_hdr_late','pdf_hdr_risk','pdf_hdr_ontime','pdf_hdr_scheduled','pdf_hdr_delivered','pdf_hdr_problems','pdf_hdr_canceled'
            ]
            with conn() as db:
                for k in report_keys:
                    db.execute('INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,?)',(k,d.get(k,''),now()))
                audit(db,u,'Alterou configurações de relatórios PDF','Configurações'); db.commit()
            return self.redirect('/relatorios?tab=agendador&saved=1')

        if not self.has_perm(u,'manage_settings') or not is_admin(u):
            return self.send_html(layout(u,'Acesso negado','<div class="alert danger">Somente perfis autorizados alteram configurações globais.</div>'),403)
        keys=['system_name','company_name','company_subtitle','primary_color','secondary_color','accent_color','background_color','load_capacity_kg','maintenance_mode','god_note','google_maps_api_key']
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
            if d.get('maintenance_mode') == 'on':
                with SESSIONS_LOCK:
                    for s_id, s_data in list(SESSIONS.items()):
                        uid = s_data.get('uid')
                        if uid != u['id']:
                            SESSIONS.pop(s_id, None)
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

        with conn() as db:
            db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES('erp_enabled','1',?)", (now(),))
            db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES('erp_sync_auto_enabled','1',?)", (now(),))
            db.commit()

        if _ERP_AVAILABLE:
            _erp_connector.reload_erp_config()

        status_info = _erp_connector.get_sync_status()
        if not status_info.get('running'):
            def _run_manual_sync():
                _erp_connector.sync_erp_cache(force=True)
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
                        'invoice_number': mapped_ped.get('invoice_number') or order_number,
                        'invoiced_at': mapped_ped.get('invoiced_at') or mapped_ped.get('sale_date') or now()[:10],
                    }

            if not invoice_data or not invoice_data.get('invoice_number'):
                continue

            nf = str(invoice_data.get('invoice_number') or '').strip()
            if not nf or nf in ('0', '0.0', 'None', 'null', 'NULL'):
                continue

            nf_date = invoice_data.get('invoiced_at') or now()[:10]

            with conn() as db:
                current = db.execute(
                    "SELECT id, status FROM orders WHERE id=? AND status='Venda'",
                    (row['id'],)
                ).fetchone()
                if not current:
                    continue

                # Evita falha por duplicidade na constraint UNIQUE de invoice_number
                dup = db.execute(
                    "SELECT id, order_number FROM orders WHERE invoice_number=? AND id<>?",
                    (nf, row['id'])
                ).fetchone()
                if dup:
                    nf = f"{nf}-{order_number}"

                db.execute(
                    "UPDATE orders SET invoice_number=?, invoiced_at=?, status='Faturado', updated_at=?, version=COALESCE(version,1)+1 WHERE id=? AND status='Venda'",
                    (nf, nf_date, now(), row['id'])
                )
                db.execute(
                    "INSERT INTO order_history(order_id,old_status,new_status,action,notes,created_at) VALUES(?,?,?,?,?,?)",
                    (row['id'], 'Venda', 'Faturado', 'ERP_SYNC', f'Faturamento detectado automaticamente via ERP. NF/Doc: {nf}', now())
                )
                db.commit()
                updated_count += 1
                _log.info('Pedido %s (ID %s) faturado automaticamente via ERP. NF/Doc: %s', order_number, row['id'], nf)
                GLOBAL_BROKER.publish('orders_updated')
        except Exception as item_exc:
            _log.error('Sync ERP: erro no pedido %s: %s', order_number, item_exc)

    return updated_count


def _resolve_route_name_for_city(db, city):
    if not city:
        return ''
    c_key = normalized_text_key(city)
    rc_rows = db.execute("SELECT route_name, city FROM route_cities WHERE active=1 AND route_name IS NOT NULL AND TRIM(route_name)<>'' ORDER BY COALESCE(delivery_order,9999), route_name, id").fetchall()
    for r in rc_rows:
        if normalized_text_key(r['city']) == c_key:
            return str(r['route_name']).strip()
    for r in rc_rows:
        rck = normalized_text_key(r['city'])
        if rck and (rck in c_key or c_key in rck):
            return str(r['route_name']).strip()
    return ''


def _get_or_create_client_from_erp(db, client_code, client_name, doc, phone, city, address, neighborhood, uf):
    c = None
    doc_clean = str(doc or '').strip()
    if doc_clean and doc_clean not in ('00000000000', '00000000000000', '0'):
        c = db.execute("SELECT id FROM clients WHERE document = ? LIMIT 1", (doc_clean,)).fetchone()
    if not c and client_code:
        c = db.execute("SELECT id FROM clients WHERE customer_code = ? LIMIT 1", (str(client_code).strip(),)).fetchone()
    if not c and client_name:
        c = db.execute("SELECT id FROM clients WHERE UPPER(TRIM(name)) = UPPER(TRIM(?)) LIMIT 1", (str(client_name).strip(),)).fetchone()

    city_val = str(city or 'Teófilo Otoni').strip()
    addr_val = str(address or '').strip()
    neigh_val = str(neighborhood or '').strip()
    phone_val = str(phone or '').strip()
    now_ts = now()

    if c:
        cid = c['id']
        db.execute("""
            UPDATE clients 
            SET phone = COALESCE(NULLIF(phone,''), ?),
                city = COALESCE(NULLIF(city,''), ?),
                address = COALESCE(NULLIF(address,''), ?),
                neighborhood = COALESCE(NULLIF(neighborhood,''), ?),
                updated_at = ?
            WHERE id = ?
        """, (phone_val, city_val, addr_val, neigh_val, now_ts, cid))
        return cid

    cur = db.execute("""
        INSERT INTO clients (customer_code, name, document, phone, city, address, neighborhood, active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
    """, (str(client_code or '').strip(), str(client_name or 'Cliente ERP').strip(), doc_clean, phone_val, city_val, addr_val, neigh_val, now_ts, now_ts))
    return cur.lastrowid


def enrich_order_items_from_erp(db, order_id, order_number):
    """
    Busca os itens reais do pedido na tabela don_pedido_venda_item do Oracle
    e substitui itens genéricos ('Carga/volume...') pelos produtos detalhados.
    """
    if not _ERP_AVAILABLE:
        return False
    import logging as _logging
    _log = _logging.getLogger('logistica.items_enrich')
    cfg = _erp_connector.get_erp_config()
    if not cfg.enabled:
        return False
    try:
        with _erp_connector._erp_connection(cfg) as ora_conn:
            cur = ora_conn.cursor()
            cur.execute("""
                SELECT 
                    pvi.item,
                    pvi.codigo_produto,
                    COALESCE(pvi.descricao_produto, pro.descricao, 'Produto') as descricao,
                    COALESCE(pro.unidade, 'UN') as unidade,
                    NVL(pvi.embalagem_venda, '') as embalagem,
                    pvi.quantidade,
                    pvi.preco_venda,
                    (pvi.quantidade * pvi.preco_venda) as total_valor,
                    NVL(pro.peso_bruto, 0) as peso_unit,
                    (pvi.quantidade * NVL(pro.peso_bruto, 0)) as peso_total
                FROM don_pedido_venda_item pvi
                LEFT JOIN don_produto pro ON pro.codigo = pvi.codigo_produto
                WHERE pvi.numero_pedido_venda = :ped
                ORDER BY pvi.item
            """, ped=str(order_number).strip())
            items = cur.fetchall()
            if not items:
                return False

            existing = db.execute("SELECT id, status FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
            has_settled = any(it['status'] in ('Entregue', 'Problema') for it in existing)
            if has_settled:
                return False

            db.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
            calc_w = 0.0
            calc_v = 0.0
            for it in items:
                p_code = str(it[1] or '').strip()
                p_name = (it[2] or 'Produto').strip()
                p_unit = (it[3] or 'UN').strip()
                p_pkg = (it[4] or '').strip()
                p_qty = float(it[5] or 0)
                p_price = float(it[6] or 0)
                p_val = float(it[7] or (p_qty * p_price))
                p_w = float(it[9] or 0)
                calc_w += p_w
                calc_v += p_val
                notes_str = f"Emb: {p_pkg} | R$ {p_price:.2f}" if p_pkg else f"R$ {p_price:.2f}"
                db.execute("""
                    INSERT INTO order_items (
                        order_id, product_code, product_name, quantity, unit, weight_kg, unit_price, total_value, status, problem_notes, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pendente', '', ?)
                """, (order_id, p_code, p_name, p_qty, p_unit, p_w, p_price, p_val, notes_str))

            row_o = db.execute("SELECT weight_kg, total_value FROM orders WHERE id=?", (order_id,)).fetchone()
            if row_o:
                if (not row_o['weight_kg'] or row_o['weight_kg'] == 0) and calc_w > 0:
                    db.execute("UPDATE orders SET weight_kg = ? WHERE id = ?", (calc_w, order_id))
                if (not row_o['total_value'] or row_o['total_value'] == 0) and calc_v > 0:
                    db.execute("UPDATE orders SET total_value = ? WHERE id = ?", (calc_v, order_id))
            db.commit()
            return True
    except Exception as exc:
        _log.warning('Falha ao enriquecer itens do pedido %s: %s', order_number, exc)
        return False


def sync_orders_from_carga_view():
    """
    Sincroniza pedidos e produtos da view de carga do ERP (VW_PEDIDO_CARGA_CC).
    Esta view filtra apenas os pedidos com 'Entrega ao cliente' (transporte rodoviário/caminhão).
    Insere novos pedidos com status 'Faturado' e seus produtos em 'order_items'.
    Atualiza pedidos existentes caso ainda não tenham saído para entrega.
    """
    if not _ERP_AVAILABLE:
        return {'ok': False, 'message': 'Módulo ERP não disponível.', 'imported': 0, 'updated': 0}

    import logging as _logging
    _log = _logging.getLogger('logistica.carga_sync')
    cfg = _erp_connector.get_erp_config()
    if not cfg.enabled:
        return {'ok': False, 'message': 'Integração ERP desabilitada.', 'imported': 0, 'updated': 0}

    view_carga = getattr(cfg, 'view_carga', None) or get_setting('erp_view_carga', 'VW_PEDIDO_CARGA_CC')
    view_cli = getattr(cfg, 'view_clientes', None) or get_setting('erp_view_clientes', 'VW_CLIENTES_CAD_LM')

    try:
        with _erp_connector._erp_connection(cfg) as ora_conn:
            cur = ora_conn.cursor()
            v_carga_full = f"{cfg.schema}.{view_carga}" if cfg.schema and '.' not in view_carga else view_carga
            v_cli_full = f"{cfg.schema}.{view_cli}" if cfg.schema and '.' not in view_cli else view_cli
            
            sql = f"""
                SELECT 
                    p.NUMERO_PEDIDO,
                    p.CODIGO_CLIENTE,
                    p.CLIENTE,
                    p.CNPJ_CLIENTE,
                    p.CODIGO_RCA,
                    p.DATA_EMISSAO_PEDIDO,
                    p.DATA_FATURAMENTO_PEDIDO,
                    p.NUM_NOTA_ORIGINAL,
                    p.POSICAO_PEDIDO,
                    p.CODIGO_PRODUTO,
                    p.DESCRICAO_PRODUTO,
                    p.EMBALAGEM_PRODUTO,
                    p.UNIDADE_PRODUTO,
                    p.QUANTIDADE_PRODUTO,
                    p.PRECO_UNITARIO_PRODUTO,
                    p.VALOR_TOTAL_PRODUTO,
                    p.PESO_BRUTO_PRODUTO,
                    p.PESO_LIQUIDO_PRODUTO,
                    p.ITEM,
                    c.ENDERECO,
                    c.NUMERO,
                    c.BAIRRO,
                    c.NOME_CIDADE,
                    c.UF,
                    c.FONE1
                FROM {v_carga_full} p
                LEFT JOIN {v_cli_full} c ON c.CODIGO = p.CODIGO_CLIENTE
                ORDER BY p.NUMERO_PEDIDO, p.ITEM
            """
            cur.execute(sql)
            cols = [d[0].lower() for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            # Consulta faturamento real de cada pedido em lote
            unique_peds = list({int(r['numero_pedido']) for r in rows if str(r.get('numero_pedido') or '').strip().isdigit()})
            ped_billing_map = {}
            if unique_peds:
                try:
                    schema_pfx = f"{cfg.schema}." if cfg.schema else ""
                    for i in range(0, len(unique_peds), 500):
                        batch = unique_peds[i:i+500]
                        in_clause = ','.join(str(x) for x in batch)
                        cur.execute(f"""
                            SELECT 
                                p.NUMERO AS numero_pedido,
                                p.POSICAO_PEDIDO AS posicao_pedido,
                                COALESCE(NULLIF(n.NUMERO, 0), NULLIF(p.NUMERO_NOTA_FISCAL_SAIDA, 0)) AS nota_fiscal,
                                p.DATA_FATURAMENTO_PEDIDO AS data_faturamento,
                                p.HORA_FATURAMENTO_PEDIDO AS hora_faturamento,
                                p.MINUTO_FATURAMENTO_PEDIDO AS minuto_faturamento
                            FROM {schema_pfx}don_pedido_venda p
                            LEFT JOIN {schema_pfx}DON_NOTA_FISCAL_SAIDA n ON n.NUMERO_PEDIDO = p.NUMERO AND n.CANCELADA = 'N'
                            WHERE p.NUMERO IN ({in_clause})
                        """)
                        for brow in cur.fetchall():
                            b_ped = str(brow[0])
                            b_pos = str(brow[1] or '').upper()
                            b_nf = str(brow[2] or '').strip()
                            if b_nf in ('0', '0.0', 'None', 'null', ''):
                                b_nf = ''
                            b_dt = brow[3]
                            b_h = brow[4] or 0
                            b_m = brow[5] or 0
                            b_fat_ts = None
                            if b_dt:
                                d_s = b_dt.strftime("%Y-%m-%d") if hasattr(b_dt, "strftime") else str(b_dt)[:10]
                                b_fat_ts = f"{d_s} {b_h:02d}:{b_m:02d}:00" if (b_h or b_m) else d_s
                            is_b_fat = (b_pos == 'F') or bool(b_nf)
                            ped_billing_map[b_ped] = {
                                'is_invoiced': is_b_fat,
                                'posicao': b_pos,
                                'invoice_number': b_nf,
                                'invoiced_at': b_fat_ts
                            }

                    # Carrega catálogo de vendedores/RCAs do ERP para resolver o nome real
                    rca_name_map = {}
                    try:
                        cur.execute(f"SELECT CODIGO, NOME FROM {schema_pfx}DON_RCA WHERE NOME IS NOT NULL")
                        for rca_row in cur.fetchall():
                            if rca_row[0] is not None and rca_row[1]:
                                rca_name_map[str(rca_row[0]).strip()] = str(rca_row[1]).strip()
                    except Exception as rca_exc:
                        _log.warning('Erro ao buscar DON_RCA na carga: %s', rca_exc)
                    try:
                        cur.execute(f"SELECT CODI_VED, NOME_VED FROM {schema_pfx}VW_VENDEDOR_CAD_LM WHERE NOME_VED IS NOT NULL")
                        for rca_row in cur.fetchall():
                            if rca_row[0] is not None and rca_row[1]:
                                c_k = str(rca_row[0]).strip()
                                if c_k not in rca_name_map:
                                    rca_name_map[c_k] = str(rca_row[1]).strip()
                    except Exception as rca_exc2:
                        _log.warning('Erro ao buscar VW_VENDEDOR_CAD_LM na carga: %s', rca_exc2)
                    if rca_name_map:
                        _SELLER_NAME_BY_CODE.update(rca_name_map)
                except Exception as b_exc:
                    _log.warning('Erro ao verificar faturamento/vendedores em lote na carga ERP: %s', b_exc)
    except Exception as exc:
        _log.error('Erro ao consultar view de carga no ERP (%s): %s', view_carga, exc)
        return {'ok': False, 'message': f'Erro ao ler view do ERP: {exc}', 'imported': 0, 'updated': 0}

    if not rows:
        return {'ok': True, 'message': 'Nenhum pedido de carga pendente na view do ERP.', 'imported': 0, 'updated': 0, 'total': 0}

    orders_map = {}
    for r in rows:
        ped = str(r.get('numero_pedido') or '').strip()
        if not ped:
            continue
        if ped not in orders_map:
            sale_dt = str(r.get('data_emissao_pedido') or '')[:10]
            if not sale_dt or len(sale_dt) < 10:
                sale_dt = today()
            inv_dt = str(r.get('data_faturamento_pedido') or '')[:10] if r.get('data_faturamento_pedido') else None
            num_nota = str(r.get('num_nota_original') or '').strip()
            if num_nota in ('0', 'None', 'null', ''):
                num_nota = ''
            
            b_info = ped_billing_map.get(ped, {})
            is_invoiced = b_info.get('is_invoiced', False)
            real_nf = b_info.get('invoice_number') or num_nota
            real_fat_date = b_info.get('invoiced_at') or inv_dt

            addr_parts = [str(r.get('endereco') or '').strip()]
            num_str = str(r.get('numero') or '').strip()
            if num_str and num_str not in ('0', 'S/N', 'SN'):
                addr_parts.append(num_str)
            full_addr = ' '.join(p for p in addr_parts if p).strip()

            s_code = str(r.get('codigo_rca') or '').strip()
            s_name = rca_name_map.get(s_code) or resolve_seller_name(s_code) or 'Vendedor ERP'

            orders_map[ped] = {
                'order_number': ped,
                'client_code': r.get('codigo_cliente'),
                'client_name': (r.get('cliente') or 'Cliente ERP').strip(),
                'document': (r.get('cnpj_cliente') or '').strip(),
                'seller_code': s_code,
                'seller_name': s_name,
                'sale_date': sale_dt,
                'invoice_date': real_fat_date if is_invoiced else None,
                'invoice_number': real_nf if is_invoiced else '',
                'is_invoiced': is_invoiced,
                'position': b_info.get('posicao') or r.get('posicao_pedido'),
                'address': full_addr,
                'neighborhood': (r.get('bairro') or '').strip(),
                'city': (r.get('nome_cidade') or 'Teófilo Otoni').strip(),
                'uf': (r.get('uf') or 'MG').strip(),
                'phone': (r.get('fone1') or '').strip(),
                'total_value': 0.0,
                'total_weight': 0.0,
                'items': []
            }

        qty = float(r.get('quantidade_produto') or 1)
        val = float(r.get('valor_total_produto') or 0)
        w = float(r.get('peso_bruto_produto') or 0)
        orders_map[ped]['total_value'] += val
        orders_map[ped]['total_weight'] += w
        orders_map[ped]['items'].append({
            'item_seq': r.get('item'),
            'product_code': str(r.get('codigo_produto') or '').strip(),
            'product_name': (r.get('descricao_produto') or 'Produto').strip(),
            'unit': (r.get('unidade_produto') or 'UN').strip(),
            'package': (r.get('embalagem_produto') or '').strip(),
            'quantity': qty,
            'unit_price': float(r.get('preco_unitario_produto') or 0),
            'total_value': val,
            'weight_kg': w
        })

    imported_count = 0
    updated_count = 0
    now_ts = now()

    with conn() as db:
        if rca_name_map:
            for c_cod, c_nom in rca_name_map.items():
                db.execute(
                    "INSERT OR REPLACE INTO erp_cache_vendedores (codigovendedor, raw_json, erp_synced_at) VALUES (?, ?, ?)",
                    (c_cod, json.dumps({'codi_ved': c_cod, 'nome_ved': c_nom, 'nome': c_nom}, ensure_ascii=False), now_ts)
                )

        deleted_peds = {r['order_number'] for r in db.execute("SELECT order_number FROM deleted_orders").fetchall()}
        for ped, o_data in orders_map.items():
            if ped in deleted_peds:
                continue
            client_id = _get_or_create_client_from_erp(
                db, o_data['client_code'], o_data['client_name'], o_data['document'],
                o_data['phone'], o_data['city'], o_data['address'], o_data['neighborhood'], o_data['uf']
            )
            route_name = _resolve_route_name_for_city(db, o_data['city'])

            try:
                d_sale = datetime.strptime(o_data['sale_date'], '%Y-%m-%d').date()
            except Exception:
                d_sale = datetime.now().date()
            deadline = (d_sale + timedelta(days=15)).strftime('%Y-%m-%d')

            existing = db.execute(
                "SELECT id, status, total_value, weight_kg, invoice_number, invoiced_at, route_name FROM orders WHERE order_number = ?",
                (ped,)
            ).fetchone()

            seller_label = o_data.get('seller_name') or resolve_seller_name(o_data.get('seller_code'), db) or 'Vendedor ERP'
            status_to_insert = 'Faturado' if o_data['is_invoiced'] else 'Venda'

            if not existing:
                cur_ins = db.execute("""
                    INSERT INTO orders (
                        order_number, client_id, seller_name, status, urgency,
                        sale_date, expected_delivery_date, invoice_limit_date,
                        total_value, weight_kg, delivery_address, city, uf, route_name,
                        invoice_number, invoiced_at, created_at, updated_at, version
                    ) VALUES (
                        ?, ?, ?, ?, 'Normal',
                        ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, 1
                    )
                """, (
                    ped, client_id, seller_label, status_to_insert,
                    o_data['sale_date'], deadline, deadline,
                    o_data['total_value'], o_data['total_weight'], o_data['address'], o_data['city'], o_data['uf'], route_name,
                    o_data['invoice_number'], o_data['invoice_date'],
                    now_ts, now_ts
                ))
                new_oid = cur_ins.lastrowid

                for it in o_data['items']:
                    notes_str = f"Emb: {it['package']} | R$ {it['unit_price']:.2f}" if it['package'] else f"R$ {it['unit_price']:.2f}"
                    db.execute("""
                        INSERT INTO order_items (
                            order_id, product_code, product_name, quantity, unit, weight_kg, unit_price, total_value, status, problem_notes, notes
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pendente', '', ?)
                    """, (
                        new_oid, it['product_code'], it['product_name'], it['quantity'], it['unit'],
                        it['weight_kg'], it['unit_price'], it['total_value'], notes_str
                    ))

                if status_to_insert == 'Faturado':
                    add_hist(db, new_oid, 1, 'ERP', 'Faturado', 'Pedido faturado importado do ERP', f'NF: {o_data["invoice_number"]} | View {view_carga} | Rota {route_name}')
                else:
                    add_hist(db, new_oid, 1, 'ERP', 'Venda', 'Pedido em venda importado do ERP', f'Aguardando Faturamento | View {view_carga} | Rota {route_name}')
                imported_count += 1
            else:
                oid = existing['id']
                current_st = normalize_order_status(existing['status'])
                if current_st in ('Venda', 'Faturado', 'Carga Montada'):
                    if current_st == 'Carga Montada':
                        update_st = 'Carga Montada'
                    else:
                        update_st = 'Faturado' if o_data['is_invoiced'] else 'Venda'
                    upd_nf = o_data['invoice_number'] if o_data['is_invoiced'] else ''
                    upd_fat_date = o_data['invoice_date'] if o_data['is_invoiced'] else None
                    db.execute("""
                        UPDATE orders 
                        SET status = ?,
                            seller_name = CASE WHEN (seller_name LIKE 'RCA %' OR seller_name IS NULL OR seller_name = '' OR seller_name GLOB '*[0-9]*') AND ? != '' THEN ? ELSE seller_name END,
                            total_value = ?, weight_kg = ?,
                            route_name = CASE WHEN route_name IS NULL OR route_name='' OR route_name='Sem rota' OR route_name='Mista' THEN ? ELSE route_name END,
                            delivery_address = COALESCE(NULLIF(delivery_address,''), ?),
                            city = COALESCE(NULLIF(city,''), ?),
                            uf = COALESCE(NULLIF(uf,''), ?),
                            invoice_number = ?,
                            invoiced_at = ?,
                            updated_at = ?,
                            version = COALESCE(version, 1) + 1
                        WHERE id = ?
                    """, (
                        update_st, seller_label, seller_label, o_data['total_value'], o_data['total_weight'], route_name,
                        o_data['address'], o_data['city'], o_data['uf'], upd_nf,
                        upd_fat_date, now_ts, oid
                    ))

                    existing_items = db.execute("SELECT id, status FROM order_items WHERE order_id = ?", (oid,)).fetchall()
                    has_settled_items = any(it['status'] in ('Entregue', 'Problema') for it in existing_items)
                    if not has_settled_items:
                        db.execute("DELETE FROM order_items WHERE order_id = ?", (oid,))
                        for it in o_data['items']:
                            notes_str = f"Emb: {it['package']} | R$ {it['unit_price']:.2f}" if it['package'] else f"R$ {it['unit_price']:.2f}"
                            db.execute("""
                                INSERT INTO order_items (
                                    order_id, product_code, product_name, quantity, unit, weight_kg, unit_price, total_value, status, problem_notes, notes
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pendente', '', ?)
                            """, (
                                oid, it['product_code'], it['product_name'], it['quantity'], it['unit'],
                                it['weight_kg'], it['unit_price'], it['total_value'], notes_str
                            ))

                    if current_st == 'Venda' and update_st == 'Faturado':
                        add_hist(db, oid, 1, 'Venda', 'Faturado', 'Pedido promovido pelo ERP', f'NF: {upd_nf}')
                    elif current_st == 'Faturado' and update_st == 'Venda':
                        add_hist(db, oid, 1, 'Faturado', 'Venda', 'Ajuste de status ERP (Aguardando faturamento)', '')
                    updated_count += 1
                else:
                    if o_data['invoice_number'] and not existing['invoice_number']:
                        db.execute("UPDATE orders SET invoice_number=?, invoiced_at=COALESCE(?, invoiced_at), updated_at=? WHERE id=?", (o_data['invoice_number'], o_data['invoice_date'], now_ts, oid))

        try:
            open_placeholders = db.execute("""
                SELECT DISTINCT o.id, o.order_number 
                FROM orders o 
                JOIN order_items oi ON oi.order_id = o.id 
                WHERE (oi.product_name LIKE '%Carga/volume%' OR oi.product_name LIKE '%volume%')
                  AND o.status NOT IN ('Acertado', 'Cancelado')
            """).fetchall()
            for po in open_placeholders:
                enrich_order_items_from_erp(db, po['id'], po['order_number'])
        except Exception as _e:
            _log.warning('Falha ao enriquecer itens pendentes: %s', _e)

        db.commit()

    if imported_count > 0 or updated_count > 0:
        GLOBAL_BROKER.publish('orders_updated')
        GLOBAL_BROKER.publish('routes_updated')

    return {
        'ok': True,
        'imported': imported_count,
        'updated': updated_count,
        'total': len(orders_map),
        'message': f'{imported_count} novo(s) pedido(s) importado(s), {updated_count} atualizado(s) da view de carga.'
    }



def start_pdf_report_scheduler():
    def pdf_loop():
        while True:
            try:
                time.sleep(30)
                now_dt = datetime.now()
                current_time = now_dt.strftime("%H:%M")
                weekday = now_dt.strftime("%a")
                today_str = now_dt.strftime("%Y-%m-%d")

                with conn() as db:
                    settings_map = {r['key']: r['value'] for r in db.execute('SELECT * FROM settings')}
                    enabled = settings_map.get('pdf_report_enabled', '1') == '1'
                    target_time = settings_map.get('pdf_report_time', '09:00').strip()
                    sched_days_raw = settings_map.get('pdf_report_days', 'Mon,Wed,Fri')
                    last_run = settings_map.get('pdf_report_last_run', '')

                    if not enabled:
                        continue

                    day_map = {
                        'seg': 'Mon', 'mon': 'Mon', 'segunda': 'Mon',
                        'qua': 'Wed', 'wed': 'Wed', 'quarta': 'Wed',
                        'sex': 'Fri', 'fri': 'Fri', 'sexta': 'Fri',
                        'todos': 'ALL', 'all': 'ALL'
                    }
                    sched_days = [d.strip() for d in sched_days_raw.split(',') if d.strip()]
                    normalized_days = set()
                    for d in sched_days:
                        d_low = d.lower()
                        normalized_days.add(day_map.get(d_low, d))

                    is_scheduled_day = ('ALL' in normalized_days) or (weekday in normalized_days)
                    is_scheduled_time = (current_time == target_time)
                    already_ran_today = last_run.startswith(today_str)

                    if is_scheduled_day and is_scheduled_time and not already_ran_today:
                        filepath = pdf_report_service.generate_orders_pdf_report(db, config=settings_map)
                        filename = os.path.basename(filepath)
                        now_ts = now()
                        db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES('pdf_report_last_run',?,?)", (f"{today_str} {current_time}", now_ts))
                        audit(db, {'id': 1, 'username': 'sistema', 'name': 'Agendador Automático', 'role': 'god'}, 'Gerou relatório PDF agendado', 'Relatórios', filename, '', f'Salvo em {filepath}')
                        db.commit()
            except Exception as e:
                log_server_error("pdf_report_scheduler_loop", e)

    t = threading.Thread(target=pdf_loop, name="pdf-report-scheduler", daemon=True)
    t.start()


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
    last_carga_sync_ts = 0.0

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

            # 2. Puxa novos pedidos de caminhão da view de carga (VW_PEDIDO_CARGA_CC) a cada 60 segundos
            if (now_ts - last_carga_sync_ts) >= 60:
                last_carga_sync_ts = now_ts
                try:
                    res_carga = sync_orders_from_carga_view()
                    if res_carga.get('imported', 0) > 0 or res_carga.get('updated', 0) > 0:
                        _log.info('Sync Carga ERP: %s', res_carga.get('message'))
                except Exception as c_exc:
                    _log.error('Sync Carga ERP: erro ao sincronizar view de carga: %s', c_exc)

            # 3. Executa sync completo de cache nos intervalos configurados (ex: a cada 30 min)
            interval_min = max(1, cfg.sync_interval_min if cfg.sync_interval_min > 0 else 30)
            interval_sec = interval_min * 60

            if (now_ts - last_full_sync_ts) >= interval_sec:
                last_full_sync_ts = now_ts
                _log.info('Sync ERP: executando sincronização periódica de cache (%s)...', reason)
                sync_res = _erp_connector.sync_erp_cache()
                _log.info('Sync ERP resultado: %s', sync_res.get('message'))

                # Re-executa verificação de faturamento e carga após o sync de cache
                sync_pending_invoiced_orders_to_logistica()
                sync_orders_from_carga_view()

        except Exception as sync_exc:
            _log.error('Sync ERP: erro no ciclo de sync: %s', sync_exc)



def create_server(host: str = HOST, port: int = PORT) -> SafeThreadingHTTPServer:
    init_db()
    runtime_state_cleanup(force=True)
    try:
        start_pdf_report_scheduler()
    except Exception as _e:
        pass
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

    # Inicia servidor HTTPS na porta 3443 para liberar GPS e recursos de segurança no celular
    https_port = int(os.environ.get('LOGISTICA_SSL_PORT', '3443'))
    https_server = None
    try:
        from app_core.ssl_cert import get_ssl_context
        ctx = get_ssl_context()
        if ctx:
            https_server = SafeThreadingHTTPServer((host, https_port), App)
            https_server.socket = ctx.wrap_socket(https_server.socket, server_side=True)
            t_https = threading.Thread(
                target=https_server.serve_forever,
                kwargs={'poll_interval': 0.5},
                daemon=True,
                name='https-listener'
            )
            t_https.start()
            print(f"🔒 Servidor HTTPS (GPS Ativo) escutando em https://{host}:{https_port}")
    except Exception as ssl_err:
        print(f"[AVISO] Servidor HTTPS não pôde ser iniciado: {ssl_err}")

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        except Exception:
            pass
        if https_server:
            try:
                https_server.server_close()
            except Exception:
                pass


if __name__=='__main__':
    start_server(host=HOST, port=PORT)

