# -*- coding: utf-8 -*-
"""
app_core/erp_connector.py
=========================
Conector READ-ONLY ao banco de dados do ERP.

Arquitetura de cache local:
  - Os dados ERP (últimos 30 dias) são sincronizados periodicamente para
    tabelas locais SQLite (erp_cache_*).
  - Lookups de pedido consultam SEMPRE o cache local — NUNCA o ERP diretamente
    durante uma requisição do usuário.
  - O ERP só é acessado pelo job de sync em background (a cada N minutos)
    e por solicitação manual do administrador.
  - Resultado: zero carga no ERP durante o dia a dia; resiliente a falhas de rede.

Configuração (em ordem de prioridade):
  1. Banco de dados local (tabela settings) — gerenciado pela tela Admin ERP
  2. Variáveis de ambiente (.env) — fallback / instalações sem UI
  3. Padrões internos

Drivers suportados:
  - oracle     → oracledb (thin mode, sem Oracle Client)
  - mysql      → pymysql
  - sqlserver  → pyodbc
  - postgresql → psycopg2

Regras de ouro:
  * NUNCA escreve nada no ERP.
  * NUNCA propaga exceção para o usuário — falha silenciosa + log.
  * Integração desativada por padrão (ERP_ENABLED=false).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("logistica.erp_connector")

# ---------------------------------------------------------------------------
# Configuração persistida em DB (injetada por app.py)
# ---------------------------------------------------------------------------

# Callback: app.py injeta uma função que lê da tabela settings
_DB_SETTINGS_READER: Callable[[str, str], str] | None = None
# Lock para escrita segura no cache
_CACHE_WRITE_LOCK = threading.Lock()
# Estado do último sync
_LAST_SYNC_INFO: dict[str, Any] = {
    "status": "idle",
    "step": "Aguardando início",
    "progress_pct": 0,
    "started_at": None,
    "finished_at": None,
    "records_synced": 0,
    "pedidos_count": 0,
    "clientes_count": 0,
    "vendedores_count": 0,
    "faturamento_count": 0,
    "error": None,
    "running": False,
}
_SYNC_STATE_LOCK = threading.Lock()


def register_db_reader(reader: Callable[[str, str], str]) -> None:
    """Chamado por app.py para registrar o leitor do banco de configurações."""
    global _DB_SETTINGS_READER
    _DB_SETTINGS_READER = reader


def _db_setting(key: str, default: str = "") -> str:
    """Lê configuração do banco local (tabela settings) via callback."""
    if _DB_SETTINGS_READER is not None:
        try:
            val = _DB_SETTINGS_READER(f"erp_{key}", "")
            if val:
                return val.strip()
        except Exception:
            pass
    return default


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "sim", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key))
    except (ValueError, TypeError):
        return default


def _setting(db_key: str, env_key: str, default: str) -> str:
    """Lê configuração: DB > env > default."""
    db_val = _db_setting(db_key)
    if db_val:
        return db_val
    env_val = _env(env_key)
    if env_val:
        return env_val
    return default


def _setting_bool(db_key: str, env_key: str, default: bool) -> bool:
    db_val = _db_setting(db_key)
    if db_val:
        return db_val.lower() in {"1", "true", "yes", "sim", "on"}
    return _env_bool(env_key, default)


def _setting_int(db_key: str, env_key: str, default: int) -> int:
    db_val = _db_setting(db_key)
    if db_val:
        try:
            return int(db_val)
        except ValueError:
            pass
    return _env_int(env_key, default)


# ---------------------------------------------------------------------------
# ErpConfig — lida com os valores de configuração
# ---------------------------------------------------------------------------

class ErpConfig:
    """Lê configuração ERP do banco local e/ou variáveis de ambiente."""

    def __init__(self) -> None:
        self.enabled: bool = _setting_bool("enabled", "ERP_ENABLED", False)
        self.driver: str = _setting("db_driver", "ERP_DB_DRIVER", "oracle").lower()
        self.host: str = _setting("db_host", "ERP_DB_HOST", "")
        default_port = {"oracle": 1521, "sqlserver": 1433, "mysql": 3306, "postgresql": 5432}.get(self.driver, 1521)
        self.port: int = _setting_int("db_port", "ERP_DB_PORT", default_port)
        self.database: str = _setting("db_name", "ERP_DB_NAME", "")
        self.schema: str = _setting("db_schema", "ERP_DB_SCHEMA", "")
        self.user: str = _setting("db_user", "ERP_DB_USER", "")
        self.password: str = _setting("db_password", "ERP_DB_PASSWORD", "")
        self.timeout: int = _setting_int("db_timeout", "ERP_DB_TIMEOUT", 5)
        self.sync_interval_min: int = _setting_int("sync_interval_min", "ERP_SYNC_INTERVAL_MIN", 30)
        self.sync_start_time: str = _setting("sync_start_time", "ERP_SYNC_START_TIME", "07:00")
        self.sync_end_time: str = _setting("sync_end_time", "ERP_SYNC_END_TIME", "19:00")
        self.sync_days: str = _setting("sync_days", "ERP_SYNC_DAYS", "seg_sab")
        self.sync_auto_enabled: bool = _setting_bool("sync_auto_enabled", "ERP_SYNC_AUTO_ENABLED", True)
        self.cache_days: int = _setting_int("cache_days", "ERP_CACHE_DAYS", 30)

        # Nomes das views
        self.view_pedidos: str = _setting("view_pedidos", "ERP_VIEW_PEDIDOS", "VW_PEDIDOS_CAD_LM")
        self.view_itens: str = _setting("view_itens", "ERP_VIEW_ITENS", "VW_ITENS_PEDIDO_CAD_LM")
        self.view_clientes: str = _setting("view_clientes", "ERP_VIEW_CLIENTES", "VW_CLIENTES_CAD_LM")
        self.view_vendedores: str = _setting("view_vendedores", "ERP_VIEW_VENDEDORES", "VW_VENDEDOR_CAD_LM")
        self.view_produtos: str = _setting("view_produtos", "ERP_VIEW_PRODUTOS", "VW_PRODUTOS_CAD_LM")
        self.view_faturamento: str = _setting("view_faturamento", "ERP_VIEW_FATURAMENTO", "VW_FATURAMENTO_CAD_LM")

        # Coluna de data de venda para filtro de 30 dias (pode variar por ERP)
        self.col_data_venda: str = _setting("col_data_venda", "ERP_COL_DATA_VENDA", "DATAVENDA")
        # Coluna do número do pedido nas views
        self.col_numero_pedido: str = _setting("col_numero_pedido", "ERP_COL_NUMERO_PEDIDO", "NUMEROPEDIDO")

    def qualified(self, view: str) -> str:
        """Retorna `schema.view` se schema definido, senão só `view`."""
        v = (view or "").strip()
        if self.driver == "oracle":
            v = v.upper()
            if self.schema:
                return f"{self.schema.strip().upper()}.{v}"
            return v
        if self.schema:
            return f"{self.schema.strip()}.{v}"
        return v

    @property
    def is_ready(self) -> bool:
        return self.enabled and bool(self.host) and bool(self.database) and bool(self.user)


# Instância global com refresh automático
_config_lock = threading.Lock()
_cfg: ErpConfig | None = None
_cfg_loaded_at: float = 0.0
_CFG_TTL_SECONDS = 30  # Recarrega config do DB a cada 30s


def get_erp_config() -> ErpConfig:
    """Retorna config ERP, recarregando do DB periodicamente."""
    global _cfg, _cfg_loaded_at
    now_ts = time.time()
    with _config_lock:
        if _cfg is None or (now_ts - _cfg_loaded_at) > _CFG_TTL_SECONDS:
            _cfg = ErpConfig()
            _cfg_loaded_at = now_ts
    return _cfg


def reload_erp_config() -> ErpConfig:
    """Força recarga imediata da configuração."""
    global _cfg, _cfg_loaded_at
    with _config_lock:
        _cfg_loaded_at = 0.0
        _cfg = ErpConfig()
        _cfg_loaded_at = time.time()
    return _cfg


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------

def _build_connection(cfg: ErpConfig):
    """Cria conexão com o banco ERP. Levanta exceção em caso de falha."""
    driver = cfg.driver

    if driver == "oracle":
        try:
            import oracledb  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "oracledb não instalado. Execute: pip install oracledb"
            ) from exc

        host = (cfg.host or "").strip()
        try:
            port = int(cfg.port or 1521)
        except (ValueError, TypeError):
            port = 1521
        db_name = (cfg.database or "").strip()
        user = (cfg.user or "").strip()
        password = cfg.password or ""

        # Padrão Lumina AI: passa host, port, user, password, sid/service_name diretamente
        if db_name:
            try:
                # Tenta via SID primeiro (ex: LOPES, XE)
                return oracledb.connect(host=host, port=port, user=user, password=password, sid=db_name)
            except Exception as sid_exc:
                try:
                    # Fallback via service_name (ex: ORCLPDB1)
                    return oracledb.connect(host=host, port=port, user=user, password=password, service_name=db_name)
                except Exception:
                    try:
                        # Fallback 3 via makedsn manual
                        dsn_sid = oracledb.makedsn(host, port, sid=db_name)
                        return oracledb.connect(user=user, password=password, dsn=dsn_sid)
                    except Exception:
                        raise sid_exc
        else:
            return oracledb.connect(host=host, port=port, user=user, password=password)

    if driver == "sqlserver":
        try:
            import pyodbc  # type: ignore
        except ImportError as exc:
            raise RuntimeError("pyodbc não instalado. Execute: pip install pyodbc") from exc
        odbc_drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
        odbc_driver = odbc_drivers[0] if odbc_drivers else "SQL Server"
        conn_str = (
            f"DRIVER={{{odbc_driver}}};"
            f"SERVER={cfg.host},{cfg.port};"
            f"DATABASE={cfg.database};"
            f"UID={cfg.user};"
            f"PWD={cfg.password};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={cfg.timeout};"
        )
        conn = pyodbc.connect(conn_str, timeout=cfg.timeout)
        conn.timeout = cfg.timeout
        return conn

    if driver == "mysql":
        try:
            import pymysql  # type: ignore
        except ImportError as exc:
            raise RuntimeError("pymysql não instalado. Execute: pip install pymysql") from exc
        return pymysql.connect(
            host=cfg.host, port=cfg.port or 3306, database=cfg.database,
            user=cfg.user, password=cfg.password, connect_timeout=cfg.timeout, charset="utf8mb4",
        )

    if driver == "postgresql":
        try:
            import psycopg2  # type: ignore
        except ImportError as exc:
            raise RuntimeError("psycopg2 não instalado. Execute: pip install psycopg2-binary") from exc
        return psycopg2.connect(
            host=cfg.host, port=cfg.port or 5432, dbname=cfg.database,
            user=cfg.user, password=cfg.password, connect_timeout=cfg.timeout,
        )

    raise ValueError(f"ERP_DB_DRIVER inválido: '{driver}'. Use: oracle, sqlserver, mysql, postgresql")


@contextmanager
def _erp_connection(cfg: ErpConfig):
    """Context manager que abre e fecha a conexão ERP com segurança."""
    conn = _build_connection(cfg)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers de cursor
# ---------------------------------------------------------------------------

def _row_to_dict(cursor, row) -> dict[str, Any]:
    """Converte row de cursor para dict com chaves em lowercase."""
    if row is None:
        return {}
    cols = [col[0].lower() for col in cursor.description]
    return dict(zip(cols, row))


def _rows_to_list(cursor, rows) -> list[dict[str, Any]]:
    cols = [col[0].lower() for col in cursor.description]
    return [dict(zip(cols, row)) for row in rows]


# ---------------------------------------------------------------------------
# Filtro de data por driver
# ---------------------------------------------------------------------------

def _date_filter_expr(driver: str, col: str, days: int) -> str:
    """Retorna a expressão SQL para filtrar os últimos N dias."""
    if driver == "oracle":
        return f"{col} >= TRUNC(SYSDATE) - {days}"
    if driver == "sqlserver":
        return f"{col} >= DATEADD(day, -{days}, GETDATE())"
    if driver == "mysql":
        return f"{col} >= DATE_SUB(CURDATE(), INTERVAL {days} DAY)"
    if driver == "postgresql":
        return f"{col} >= CURRENT_DATE - INTERVAL '{days} days'"
    return f"{col} >= CURRENT_DATE - {days}"


def _resolve_oracle_view(cur, view_name: str, schema: str = "", user: str = "") -> str:
    """Resolve o nome qualificado da view no Oracle, descobrindo o schema proprietário automaticamente se necessário."""
    v_upper = (view_name or "").strip().upper()
    if not v_upper:
        return ""

    # 1. Se schema foi informado explicitamente pelo operador, usa SCHEMA.VIEW
    if schema and schema.strip():
        return f"{schema.strip().upper()}.{v_upper}"

    # 2. Testa primeiro o nome sem qualificador
    try:
        cur.execute(f"SELECT * FROM {v_upper} WHERE ROWNUM < 1")
        return v_upper
    except Exception as exc1:
        if "ORA-00942" not in str(exc1).upper():
            return v_upper

    # 3. Auto-fallback: testa com o próprio usuário logado como schema (ex: CCAMPO.VW_PEDIDOS_CAD_LM)
    if user and user.strip():
        u_upper = user.strip().upper()
        try:
            target = f"{u_upper}.{v_upper}"
            cur.execute(f"SELECT * FROM {target} WHERE ROWNUM < 1")
            return target
        except Exception:
            pass

    # 4. Auto-descoberta: consulta o catálogo do Oracle (ALL_OBJECTS) para encontrar o dono da view
    try:
        cur.execute(
            "SELECT OWNER FROM ALL_OBJECTS WHERE OBJECT_NAME = :1 AND OBJECT_TYPE IN ('VIEW', 'TABLE') AND ROWNUM = 1",
            (v_upper,)
        )
        row = cur.fetchone()
        if row and row[0]:
            found_owner = str(row[0]).strip().upper()
            return f"{found_owner}.{v_upper}"
    except Exception:
        pass

    return v_upper


# ---------------------------------------------------------------------------
# Cache local (SQLite via tabelas do sistema)
# ---------------------------------------------------------------------------

# Callback: app.py injeta a função de conexão local
_LOCAL_DB_CONN: Callable | None = None


def register_local_db(conn_factory: Callable) -> None:
    """Registra a função de conexão ao banco SQLite local."""
    global _LOCAL_DB_CONN
    _LOCAL_DB_CONN = conn_factory


def _local_conn():
    """Retorna conexão ao banco local."""
    if _LOCAL_DB_CONN is None:
        raise RuntimeError("Cache local não registrado. Chame register_local_db() no startup.")
    return _LOCAL_DB_CONN()


def init_cache_tables() -> None:
    """Cria as tabelas de cache ERP no banco local (idempotente)."""
    try:
        with _local_conn() as db:
            db.executescript("""
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
            """)
        logger.debug("Tabelas de cache ERP verificadas/criadas.")
    except Exception as exc:
        logger.warning("Não foi possível criar tabelas de cache ERP: %s", exc)


def _cache_get_pedido(order_number: str) -> dict[str, Any] | None:
    """Busca pedido no cache local. Retorna None se não encontrado."""
    try:
        with _local_conn() as db:
            row = db.execute(
                "SELECT raw_json FROM erp_cache_pedidos WHERE numeropedido=?",
                (str(order_number).strip(),)
            ).fetchone()
            if row:
                return json.loads(row[0])
    except Exception as exc:
        logger.warning("Erro ao ler cache de pedido: %s", exc)
    return None


def _cache_get_faturamento(order_number: str) -> dict[str, Any] | None:
    """Busca status de faturamento no cache local."""
    try:
        with _local_conn() as db:
            row = db.execute(
                "SELECT raw_json FROM erp_cache_faturamento WHERE numeropedido=?",
                (str(order_number).strip(),)
            ).fetchone()
            if row:
                return json.loads(row[0])
    except Exception as exc:
        logger.warning("Erro ao ler cache de faturamento: %s", exc)
    return None


def _get_cache_meta(key: str, default: str = "") -> str:
    try:
        with _local_conn() as db:
            row = db.execute("SELECT value FROM erp_cache_meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else default
    except Exception:
        return default


def _set_cache_meta(key: str, value: str) -> None:
    try:
        with _local_conn() as db:
            db.execute(
                "INSERT OR REPLACE INTO erp_cache_meta(key,value,updated_at) VALUES(?,?,?)",
                (key, value, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            db.commit()
    except Exception as exc:
        logger.warning("Erro ao gravar meta de cache: %s", exc)


# ---------------------------------------------------------------------------
# Sync ERP → Cache local
# ---------------------------------------------------------------------------

def _json_safe(obj: Any) -> Any:
    """Torna um objeto serializável em JSON (converte datas, decimais, etc.)."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (datetime,)):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    try:
        import decimal
        if isinstance(obj, decimal.Decimal):
            return float(obj)
    except ImportError:
        pass
    return str(obj)


def _row_to_json(cursor, row) -> str:
    """Converte row de cursor para JSON string."""
    d = _row_to_dict(cursor, row)
    safe = {k: _json_safe(v) for k, v in d.items()}
    return json.dumps(safe, ensure_ascii=False)


def _update_sync_info(**kwargs) -> None:
    global _LAST_SYNC_INFO
    with _SYNC_STATE_LOCK:
        _LAST_SYNC_INFO.update(kwargs)


def sync_erp_cache() -> dict[str, Any]:
    """
    Sincroniza dados do ERP para o cache local (últimos N dias).
    Retorna dict com resultado: {'ok': bool, 'records': int, 'message': str}
    Seguro para chamar em background thread.
    """
    global _LAST_SYNC_INFO

    cfg = get_erp_config()
    if not cfg.is_ready:
        return {"ok": False, "records": 0, "message": "ERP não configurado ou desabilitado."}

    with _SYNC_STATE_LOCK:
        if _LAST_SYNC_INFO.get("running"):
            return {"ok": False, "records": 0, "message": "Sync já em andamento."}
        _LAST_SYNC_INFO.update({
            "status": "running",
            "step": "Iniciando conexão com o banco ERP...",
            "progress_pct": 5,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "records_synced": 0,
            "pedidos_count": 0,
            "clientes_count": 0,
            "vendedores_count": 0,
            "faturamento_count": 0,
            "error": None,
            "running": True,
        })

    total_records = 0
    error_msg = None

    try:
        days = cfg.cache_days
        col_data = cfg.col_data_venda.upper()
        col_num_ped = cfg.col_numero_pedido.upper()
        date_filter = _date_filter_expr(cfg.driver, col_data, days)

        _update_sync_info(step=f"Conectando ao banco {cfg.driver.upper()} ({cfg.host}:{cfg.port})...", progress_pct=10)

        with _erp_connection(cfg) as erp_conn:
            cur = erp_conn.cursor()

            # ---- 1. Sync VW_PEDIDOS ----
            v_ped = _resolve_oracle_view(cur, cfg.view_pedidos, cfg.schema, cfg.user) if cfg.driver == "oracle" else cfg.qualified(cfg.view_pedidos)
            _update_sync_info(step=f"[1/4] Lendo View de Pedidos ({v_ped})...", progress_pct=20)
            try:
                cur.execute(f"SELECT * FROM {v_ped} WHERE {date_filter}")
            except Exception as e_ped:
                logger.warning("Filtro de data na view de pedidos falhou (%s). Executando busca direta...", e_ped)
                if cfg.driver == "oracle":
                    cur.execute(f"SELECT * FROM {v_ped} WHERE ROWNUM <= 5000")
                elif cfg.driver == "sqlserver":
                    cur.execute(f"SELECT TOP 5000 * FROM {v_ped}")
                else:
                    cur.execute(f"SELECT * FROM {v_ped} LIMIT 5000")

            ped_rows = cur.fetchall()
            num_ped_col_idx = next(
                (i for i, col in enumerate(cur.description) if col[0].upper() in (col_num_ped, 'PEDIDO_', 'PEDIDO', 'NUMEROPEDIDO', 'NUM_PEDIDO', 'NOTA_')), 0
            )
            pedidos_data = []
            for row in ped_rows:
                num_ped = str(row[num_ped_col_idx] or "").strip()
                if not num_ped:
                    continue
                raw_json = _row_to_json(cur, row)
                pedidos_data.append((num_ped, raw_json))
            total_records += len(pedidos_data)
            _update_sync_info(step=f"[1/4] OK! Lidos {len(pedidos_data)} pedidos da view {v_ped}.", progress_pct=35, pedidos_count=len(pedidos_data))

            # ---- 2. Sync VW_CLIENTES ----
            v_cli = _resolve_oracle_view(cur, cfg.view_clientes, cfg.schema, cfg.user) if cfg.driver == "oracle" else cfg.qualified(cfg.view_clientes)
            _update_sync_info(step=f"[2/4] Lendo View de Clientes ({v_cli})...", progress_pct=40)
            try:
                if cfg.driver == "oracle":
                    cur.execute(f"SELECT * FROM {v_cli} WHERE ROWNUM <= 10000")
                elif cfg.driver == "sqlserver":
                    cur.execute(f"SELECT TOP 10000 * FROM {v_cli}")
                else:
                    cur.execute(f"SELECT * FROM {v_cli} LIMIT 10000")
            except Exception:
                cur.execute(f"SELECT * FROM {v_cli}")
            cli_rows = cur.fetchall()
            clientes_data = []
            for row in cli_rows:
                d = _row_to_dict(cur, row)
                cod = str(
                    d.get("codigocliente") or d.get("cod_cliente") or d.get("codcliente_") or d.get("codigo") or ""
                ).strip()
                if not cod:
                    continue
                safe = {k: _json_safe(v) for k, v in d.items()}
                clientes_data.append((cod, json.dumps(safe, ensure_ascii=False)))
            total_records += len(clientes_data)
            _update_sync_info(step=f"[2/4] OK! Lidos {len(clientes_data)} clientes da view {v_cli}.", progress_pct=55, clientes_count=len(clientes_data))

            # ---- 3. Sync VW_VENDEDORES ----
            v_vend = _resolve_oracle_view(cur, cfg.view_vendedores, cfg.schema, cfg.user) if cfg.driver == "oracle" else cfg.qualified(cfg.view_vendedores)
            _update_sync_info(step=f"[3/4] Lendo View de Vendedores ({v_vend})...", progress_pct=60)
            try:
                cur.execute(f"SELECT * FROM {v_vend}")
            except Exception:
                pass
            vend_rows = cur.fetchall()
            vendedores_data = []
            for row in vend_rows:
                d = _row_to_dict(cur, row)
                cod = str(
                    d.get("codigovendedor") or d.get("cod_vendedor") or d.get("codi_ved") or d.get("cod_ved") or d.get("codigo") or ""
                ).strip()
                if not cod:
                    continue
                safe = {k: _json_safe(v) for k, v in d.items()}
                vendedores_data.append((cod, json.dumps(safe, ensure_ascii=False)))
            total_records += len(vendedores_data)
            _update_sync_info(step=f"[3/4] OK! Lidos {len(vendedores_data)} vendedores da view {v_vend}.", progress_pct=70, vendedores_count=len(vendedores_data))

            # ---- 4. Sync VW_FATURAMENTO ----
            v_fat = _resolve_oracle_view(cur, cfg.view_faturamento, cfg.schema, cfg.user) if cfg.driver == "oracle" else cfg.qualified(cfg.view_faturamento)
            _update_sync_info(step=f"[4/4] Lendo View de Faturamento ({v_fat})...", progress_pct=75)
            try:
                cur.execute(f"SELECT * FROM {v_fat} WHERE {date_filter}")
            except Exception:
                if cfg.driver == "oracle":
                    cur.execute(f"SELECT * FROM {v_fat} WHERE ROWNUM <= 5000")
                else:
                    cur.execute(f"SELECT * FROM {v_fat}")
            fat_rows = cur.fetchall()
            fat_data = []
            for row in fat_rows:
                d = _row_to_dict(cur, row)
                num = str(
                    d.get("numeropedido") or d.get("nota_fiscal") or d.get("num_pedido") or d.get("pedido_") or d.get("nota_") or d.get("item") or ""
                ).strip()
                if not num:
                    continue
                safe = {k: _json_safe(v) for k, v in d.items()}
                fat_data.append((num, json.dumps(safe, ensure_ascii=False)))
            total_records += len(fat_data)
            _update_sync_info(step=f"[4/4] OK! Lidos {len(fat_data)} faturamentos da view {v_fat}.", progress_pct=85, faturamento_count=len(fat_data))

        # ---- 5. Grava no cache SQLite local ----
        _update_sync_info(step="Salvação: Gravando registros no banco SQLite local...", progress_pct=90)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _CACHE_WRITE_LOCK:
            with _local_conn() as db:
                if pedidos_data:
                    db.executemany(
                        "INSERT OR REPLACE INTO erp_cache_pedidos(numeropedido,raw_json,erp_synced_at) VALUES(?,?,?)",
                        [(n, j, now_str) for n, j in pedidos_data]
                    )
                if clientes_data:
                    db.executemany(
                        "INSERT OR REPLACE INTO erp_cache_clientes(codigocliente,raw_json,erp_synced_at) VALUES(?,?,?)",
                        [(n, j, now_str) for n, j in clientes_data]
                    )
                if vendedores_data:
                    db.executemany(
                        "INSERT OR REPLACE INTO erp_cache_vendedores(codigovendedor,raw_json,erp_synced_at) VALUES(?,?,?)",
                        [(n, j, now_str) for n, j in vendedores_data]
                    )
                if fat_data:
                    db.executemany(
                        "INSERT OR REPLACE INTO erp_cache_faturamento(numeropedido,raw_json,erp_synced_at) VALUES(?,?,?)",
                        [(n, j, now_str) for n, j in fat_data]
                    )
                cutoff = (datetime.now() - timedelta(days=cfg.cache_days + 5)).strftime("%Y-%m-%d %H:%M:%S")
                db.execute("DELETE FROM erp_cache_pedidos WHERE erp_synced_at < ?", (cutoff,))
                db.execute("DELETE FROM erp_cache_faturamento WHERE erp_synced_at < ?", (cutoff,))
                db.commit()

        _set_cache_meta("last_sync_at", now_str)
        _set_cache_meta("last_sync_records", str(total_records))
        _set_cache_meta("last_sync_status", "ok")

        _update_sync_info(
            step=f"✅ Sincronização concluída com sucesso! Total: {total_records} registros salvos no cache local.",
            progress_pct=100,
            status="ok",
            records_synced=total_records
        )
        result = {"ok": True, "records": total_records, "message": f"Sync concluído: {total_records} registros salvos no cache SQLite."}

    except Exception as exc:
        error_msg = str(exc)
        logger.error("Sync ERP falhou: %s", exc, exc_info=True)
        _set_cache_meta("last_sync_status", f"error: {error_msg[:200]}")
        _update_sync_info(step=f"❌ Erro durante o sync: {error_msg}", progress_pct=100, status="error", error=error_msg)
        result = {"ok": False, "records": total_records, "message": f"Erro no sync: {error_msg}"}

    finally:
        finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with _SYNC_STATE_LOCK:
            _LAST_SYNC_INFO["finished_at"] = finished
            _LAST_SYNC_INFO["running"] = False

    return result


def get_sync_status() -> dict[str, Any]:
    """Retorna o estado atual do sync (para a tela de configuração)."""
    with _SYNC_STATE_LOCK:
        if _LAST_SYNC_INFO.get("running") and _LAST_SYNC_INFO.get("started_at"):
            try:
                started_dt = datetime.strptime(_LAST_SYNC_INFO["started_at"], "%Y-%m-%d %H:%M:%S")
                if (datetime.now() - started_dt).total_seconds() > 180:
                    logger.warning("Sync ERP em 'running' há mais de 180s. Destravando estado automaticamente.")
                    _LAST_SYNC_INFO["running"] = False
                    _LAST_SYNC_INFO["status"] = "idle"
            except Exception:
                _LAST_SYNC_INFO["running"] = False
        info = dict(_LAST_SYNC_INFO)

    # Complementa com dados do cache meta
    info["last_sync_at"] = _get_cache_meta("last_sync_at", "Nunca")
    info["last_sync_records"] = _get_cache_meta("last_sync_records", "0")
    info["last_sync_db_status"] = _get_cache_meta("last_sync_status", "")

    try:
        with _local_conn() as db:
            info["cache_pedidos_count"] = db.execute("SELECT COUNT(*) FROM erp_cache_pedidos").fetchone()[0]
            info["cache_fat_count"] = db.execute("SELECT COUNT(*) FROM erp_cache_faturamento").fetchone()[0]
            info["cache_clientes_count"] = db.execute("SELECT COUNT(*) FROM erp_cache_clientes").fetchone()[0]
    except Exception:
        info["cache_pedidos_count"] = 0
        info["cache_fat_count"] = 0
        info["cache_clientes_count"] = 0

    return info


# ---------------------------------------------------------------------------
# Funções públicas de lookup (sempre via cache local)
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", "."))
    except Exception:
        return 0.0


def _fetch_live_order_from_erp(cfg: ErpConfig, order_number: str) -> dict[str, Any] | None:
    """Busca pedido diretamente no ERP quando não encontrado no cache local e salva no cache."""
    try:
        with _erp_connection(cfg) as erp_conn:
            cur = erp_conn.cursor()
            cur2 = erp_conn.cursor()

            # 1. No Oracle, tenta primeiro a tabela mestre DONWMS.DON_PEDIDO_VENDA (contém abertos 'L' e faturados 'F')
            if cfg.driver == "oracle":
                try:
                    cur.execute("SELECT * FROM DONWMS.DON_PEDIDO_VENDA WHERE TO_CHAR(NUMERO) = :1", (order_number,))
                    pv_row = cur.fetchone()
                    if pv_row:
                        ped_dict = {k: _json_safe(v) for k, v in _row_to_dict(cur, pv_row).items()}
                        # Mapeia colunas conhecidas para chaves padrão
                        ped_dict["posicao_pedido"] = ped_dict.get("posicao_pedido") or "L"
                        ped_dict["numero_nota_fiscal_saida"] = ped_dict.get("numero_nota_fiscal_saida") or 0
                        ped_dict["valortotal"] = ped_dict.get("valor_total") or 0.0
                        ped_dict["total_weight"] = ped_dict.get("peso_total") or 0.0
                        ped_dict["dtemissao_"] = ped_dict.get("data")
                        ped_dict["codcliente_"] = ped_dict.get("codigo_cliente")
                        ped_dict["rca_"] = ped_dict.get("codigo_rca")

                        # Busca itens em DONWMS.DON_PEDIDO_VENDA_ITEM
                        try:
                            cur2.execute("SELECT * FROM DONWMS.DON_PEDIDO_VENDA_ITEM WHERE TO_CHAR(NUMERO_PEDIDO_VENDA) = :1", (order_number,))
                            i_rows = cur2.fetchall()
                            if i_rows:
                                ped_dict["_itens"] = [{k: _json_safe(v) for k, v in _row_to_dict(cur2, ir).items()} for ir in i_rows]
                        except Exception:
                            ped_dict["_itens"] = []

                        # Cliente ao vivo
                        cod_cli = str(ped_dict.get("codigo_cliente") or "").strip()
                        if cod_cli:
                            try:
                                v_cli = _resolve_oracle_view(cur2, cfg.view_clientes, cfg.schema, cfg.user)
                                cur2.execute(f"SELECT * FROM {v_cli} WHERE TO_CHAR(CODIGO) = :1", (cod_cli,))
                                c_row = cur2.fetchone()
                                if c_row:
                                    ped_dict["_cliente"] = {k: _json_safe(v) for k, v in _row_to_dict(cur2, c_row).items()}
                            except Exception:
                                pass

                        # Vendedor ao vivo
                        cod_vend = str(ped_dict.get("codigo_rca") or "").strip()
                        if cod_vend:
                            try:
                                v_vend = _resolve_oracle_view(cur2, cfg.view_vendedores, cfg.schema, cfg.user)
                                cur2.execute(f"SELECT * FROM {v_vend} WHERE TO_CHAR(CODI_VED) = :1", (cod_vend,))
                                vd_row = cur2.fetchone()
                                if vd_row:
                                    ped_dict["_vendedor"] = {k: _json_safe(v) for k, v in _row_to_dict(cur2, vd_row).items()}
                            except Exception:
                                pass

                        raw_json = json.dumps(ped_dict, ensure_ascii=False)
                        synced_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        with _local_conn() as db:
                            db.execute(
                                "INSERT OR REPLACE INTO erp_cache_pedidos (numeropedido, raw_json, erp_synced_at) VALUES (?, ?, ?)",
                                (order_number, raw_json, synced_at)
                            )
                        return ped_dict
                except Exception as exc_pv:
                    logger.warning("Falha ao buscar DON_PEDIDO_VENDA para '%s': %s", order_number, exc_pv)

            # 2. Fallback para VW_VENDAS_LM / view configurada
            v_ped = _resolve_oracle_view(cur, cfg.view_pedidos, cfg.schema, cfg.user) if cfg.driver == "oracle" else cfg.qualified(cfg.view_pedidos)
            col_num_ped = cfg.col_numero_pedido.upper()
            
            if cfg.driver == "oracle":
                cur.execute(f"SELECT * FROM {v_ped} WHERE TO_CHAR({col_num_ped}) = :1 OR TO_CHAR(PEDIDO_) = :1 OR TO_CHAR(NOTA_) = :1", (order_number, order_number, order_number))
            elif cfg.driver == "sqlserver":
                cur.execute(f"SELECT TOP 10 * FROM {v_ped} WHERE CAST({col_num_ped} AS VARCHAR) = ?", (order_number,))
            else:
                cur.execute(f"SELECT * FROM {v_ped} WHERE CAST({col_num_ped} AS CHAR) = %s LIMIT 10", (order_number,))

            rows = cur.fetchall()
            if not rows:
                return None

            itens_list = []
            total_calc_weight = 0.0
            total_calc_val = 0.0

            v_prod = _resolve_oracle_view(cur2, cfg.view_produtos, cfg.schema, cfg.user) if cfg.driver == "oracle" else cfg.qualified(cfg.view_produtos)

            for row in rows:
                item_d = _row_to_dict(cur, row)
                itens_list.append(item_d)
                
                item_val = _safe_float(item_d.get("ptotal_") or item_d.get("valortotal") or 0)
                total_calc_val += item_val

                c_prod = str(item_d.get("codprod_") or item_d.get("codigoproduto") or item_d.get("cod_produto") or "").strip()
                item_qty = _safe_float(item_d.get("qtde_") or item_d.get("quantidade") or 1)
                peso_unit = 0.0

                if c_prod:
                    try:
                        cur2.execute(f"SELECT PESO_BRUTO FROM {v_prod} WHERE TO_NUMBER(CODIGO) = TO_NUMBER(:1)", (c_prod,))
                        p_row = cur2.fetchone()
                        if p_row and p_row[0]:
                            peso_unit = _safe_float(p_row[0])
                    except Exception:
                        pass
                
                total_calc_weight += round(peso_unit * item_qty, 4)

            ped_dict = {k: _json_safe(v) for k, v in _row_to_dict(cur, rows[0]).items()}
            ped_dict["_itens"] = [{k: _json_safe(v) for k, v in item_d.items()} for item_d in itens_list]
            ped_dict["total_weight"] = round(total_calc_weight, 2)
            if total_calc_val > 0 and not ped_dict.get("ptotal_"):
                ped_dict["ptotal_"] = total_calc_val

            # Enriquece com Cliente ao vivo se disponível
            cod_cli = str(ped_dict.get("codcliente_") or ped_dict.get("codigocliente") or ped_dict.get("cod_cliente") or "").strip()
            if cod_cli:
                try:
                    v_cli = _resolve_oracle_view(cur2, cfg.view_clientes, cfg.schema, cfg.user) if cfg.driver == "oracle" else cfg.qualified(cfg.view_clientes)
                    cur2.execute(f"SELECT * FROM {v_cli} WHERE TO_CHAR(CODIGO) = :1", (cod_cli,))
                    c_row = cur2.fetchone()
                    if c_row:
                        ped_dict["_cliente"] = {k: _json_safe(v) for k, v in _row_to_dict(cur2, c_row).items()}
                except Exception as exc_cli:
                    logger.warning("Falha ao buscar cliente '%s': %s", cod_cli, exc_cli)

            # Enriquece com Vendedor ao vivo se disponível
            cod_vend = str(ped_dict.get("rca_") or ped_dict.get("codigovendedor") or ped_dict.get("cod_vendedor") or "").strip()
            if cod_vend:
                try:
                    v_vend = _resolve_oracle_view(cur2, cfg.view_vendedores, cfg.schema, cfg.user) if cfg.driver == "oracle" else cfg.qualified(cfg.view_vendedores)
                    cur2.execute(f"SELECT * FROM {v_vend} WHERE TO_CHAR(CODI_VED) = :1", (cod_vend,))
                    vd_row = cur2.fetchone()
                    if vd_row:
                        ped_dict["_vendedor"] = {k: _json_safe(v) for k, v in _row_to_dict(cur2, vd_row).items()}
                except Exception as exc_vend:
                    logger.warning("Falha ao buscar vendedor '%s': %s", cod_vend, exc_vend)

            raw_json = json.dumps(ped_dict, ensure_ascii=False)

            try:
                synced_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with _local_conn() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO erp_cache_pedidos (numeropedido, raw_json, erp_synced_at) VALUES (?, ?, ?)",
                        (order_number, raw_json, synced_at)
                    )
                    if ped_dict.get("_cliente") and cod_cli:
                        db.execute(
                            "INSERT OR REPLACE INTO erp_cache_clientes (codigocliente, raw_json, erp_synced_at) VALUES (?, ?, ?)",
                            (cod_cli, json.dumps(ped_dict["_cliente"], ensure_ascii=False), synced_at)
                        )
                    if ped_dict.get("_vendedor") and cod_vend:
                        db.execute(
                            "INSERT OR REPLACE INTO erp_cache_vendedores (codigovendedor, raw_json, erp_synced_at) VALUES (?, ?, ?)",
                            (cod_vend, json.dumps(ped_dict["_vendedor"], ensure_ascii=False), synced_at)
                        )
                    db.commit()
            except Exception:
                pass

            return ped_dict
    except Exception as exc:
        logger.warning("Falha na busca ao vivo do pedido '%s' no ERP: %s", order_number, exc)
        return None


def lookup_order(order_number: str, force_live: bool = False) -> dict[str, Any] | None:
    """
    Busca pedido no cache local ERP pelo número do pedido.
    Se force_live=True ou caso não esteja no cache, realiza busca direta no ERP ao vivo.
    Retorna dict enriquecido ou None se não encontrado.
    """
    cfg = get_erp_config()
    if not cfg.is_ready:
        return None

    order_number = (order_number or "").strip()
    if not order_number:
        return None

    try:
        ped_data = None if force_live else _cache_get_pedido(order_number)
        if ped_data is None:
            ped_data = _fetch_live_order_from_erp(cfg, order_number)
            if ped_data is None and force_live:
                ped_data = _cache_get_pedido(order_number)

        if ped_data is None:
            logger.info("Cache/ERP: pedido '%s' não encontrado.", order_number)
            return None

        result = dict(ped_data)
        result["_erp_pedido_ok"] = True

        # Enriquece com dados do cliente (cache local se não veio no pedido)
        cod_cli = str(
            result.get("codigocliente") or result.get("cod_cliente") or result.get("codcliente_") or result.get("cliente_id") or ""
        ).strip()
        if cod_cli and not result.get("_cliente"):
            try:
                with _local_conn() as db:
                    row = db.execute(
                        "SELECT raw_json FROM erp_cache_clientes WHERE codigocliente=?", (cod_cli,)
                    ).fetchone()
                    if row:
                        result["_cliente"] = json.loads(row[0])
            except Exception:
                pass

        # Enriquece com dados do vendedor (cache local se não veio no pedido)
        cod_vend = str(
            result.get("codigovendedor") or result.get("cod_vendedor") or result.get("rca_") or result.get("codi_ved") or ""
        ).strip()
        if cod_vend and not result.get("_vendedor"):
            try:
                with _local_conn() as db:
                    row = db.execute(
                        "SELECT raw_json FROM erp_cache_vendedores WHERE codigovendedor=?", (cod_vend,)
                    ).fetchone()
                    if row:
                        result["_vendedor"] = json.loads(row[0])
            except Exception:
                pass

        # Itens: o peso vem do próprio pedido (VW_PEDIDOS_CAD_LM tem peso total)
        result["_itens"] = []  # Sem itens individuais — peso total vem do pedido

        logger.info("Cache ERP: pedido '%s' localizado.", order_number)
        return result

    except Exception as exc:
        logger.error("lookup_order('%s') falhou: %s", order_number, exc, exc_info=True)
        return None


def lookup_invoice_status(order_number: str, force_live: bool = False) -> dict[str, Any] | None:
    """
    Consulta status de faturamento no cache local (faturamento ou pedidos).
    Se force_live=True, força busca ao vivo no ERP caso o cache não tenha NF.
    Retorna dict com dados da NF, ou None se não faturado.
    """
    cfg = get_erp_config()
    if not cfg.is_ready:
        return None

    order_number = (order_number or "").strip()
    if not order_number:
        return None

    try:
        if not force_live:
            fat = _cache_get_faturamento(order_number)
            if fat:
                nf = (
                    fat.get("nota_fiscal")
                    or fat.get("numero_nota_fiscal")
                    or fat.get("numeronota")
                    or fat.get("nf")
                    or fat.get("num_nota")
                    or fat.get("numero_nota_fiscal_saida")
                    or fat.get("nota_")
                    or fat.get("num_nota_fiscal")
                    or fat.get("notafiscal")
                    or fat.get("numero_nfe")
                    or fat.get("nfe")
                )
                if nf and str(nf).strip() not in ("", "0", "0.0", "None", "null"):
                    return fat

        ped = lookup_order(order_number, force_live=force_live)
        if ped:
            nf = (
                ped.get("numeronota")
                or ped.get("nota_fiscal")
                or ped.get("numero_nota_fiscal")
                or ped.get("num_nota")
                or ped.get("nf")
                or ped.get("notafiscal")
                or ped.get("num_nota_fiscal")
                or ped.get("numero_nota_fiscal_saida")
                or ped.get("nota_")
                or ped.get("numero_nfe")
                or ped.get("nfe")
            )
            dt_fat = (
                ped.get("datafaturamento")
                or ped.get("data_faturamento")
                or ped.get("data_faturamento_pedido")
                or ped.get("datanota")
                or ped.get("data_nota")
                or ped.get("dtemissao_")
                or ped.get("data_venda")
            )
            st = str(ped.get("posicao_pedido") or ped.get("status") or ped.get("statuspedido") or ped.get("situacao") or "").upper()
            is_fat = (nf and str(nf).strip() not in ("", "0", "0.0", "None", "null")) or st in ("F", "FATURADO", "FAT", "E", "EMITIDO", "CONCLUIDO", "FECHADO", "ENCERRADO")
            if is_fat:
                return {
                    "numeropedido": order_number,
                    "nota_fiscal": str(nf).strip() if (nf and str(nf).strip() not in ("", "0", "0.0", "None", "null")) else "1",
                    "data_faturamento": dt_fat,
                    "numeronota": nf,
                    "datafaturamento": dt_fat,
                    "is_invoiced": True,
                    "invoice_number": str(nf).strip() if (nf and str(nf).strip() not in ("", "0", "0.0", "None", "null")) else "1",
                    "raw_json": json.dumps(ped)
                }

        if not force_live:
            return _cache_get_faturamento(order_number)
        return None
    except Exception as exc:
        logger.error("lookup_invoice_status('%s') falhou: %s", order_number, exc, exc_info=True)
        return None


def check_connectivity(config: ErpConfig | None = None) -> dict[str, Any]:
    """
    Testa conectividade direta com o ERP (usado apenas para diagnóstico na tela admin).
    Retorna dict com 'ok', 'message' e 'views'.
    """
    cfg = config or get_erp_config()
    if not cfg.enabled:
        return {"ok": False, "message": "Integração ERP desabilitada.", "views": {}}
    if not cfg.is_ready:
        return {"ok": False, "message": "Configuração ERP incompleta. Verifique Host, Banco e Usuário.", "views": {}}

    all_views = {
        "pedidos": cfg.view_pedidos,
        "itens": cfg.view_itens,
        "clientes": cfg.view_clientes,
        "vendedores": cfg.view_vendedores,
        "produtos": cfg.view_produtos,
        "faturamento": cfg.view_faturamento,
    }

    view_cols: dict[str, list[str]] = {}
    try:
        with _erp_connection(cfg) as erp_conn:
            cur = erp_conn.cursor()
            for alias, view in all_views.items():
                try:
                    if cfg.driver == "oracle":
                        qualified = _resolve_oracle_view(cur, view, cfg.schema, cfg.user)
                        cur.execute(f"SELECT * FROM {qualified} WHERE ROWNUM < 1")
                    elif cfg.driver == "sqlserver":
                        qualified = cfg.qualified(view)
                        cur.execute(f"SELECT TOP 0 * FROM {qualified}")
                    else:
                        qualified = cfg.qualified(view)
                        cur.execute(f"SELECT * FROM {qualified} LIMIT 0")
                    view_cols[alias] = [col[0] for col in (cur.description or [])]
                except Exception as view_exc:
                    view_cols[alias] = [f"ERRO: {view_exc}"]

        return {
            "ok": True,
            "message": f"Conexão ERP estabelecida com sucesso ({cfg.driver.upper()} @ {cfg.host}).",
            "views": view_cols,
        }
    except Exception as exc:
        return {"ok": False, "message": f"Falha ao conectar: {exc}", "views": view_cols}


def inspect_view(view_name: str) -> list[str]:
    """Retorna colunas de uma view ERP (diagnóstico)."""
    cfg = get_erp_config()
    if not cfg.is_ready:
        return []
    try:
        with _erp_connection(cfg) as erp_conn:
            cur = erp_conn.cursor()
            if cfg.driver == "oracle":
                q = _resolve_oracle_view(cur, view_name, cfg.schema, cfg.user)
                cur.execute(f"SELECT * FROM {q} WHERE ROWNUM < 1")
            elif cfg.driver == "sqlserver":
                q = cfg.qualified(view_name)
                cur.execute(f"SELECT TOP 0 * FROM {q}")
            else:
                q = cfg.qualified(view_name)
                cur.execute(f"SELECT * FROM {q} LIMIT 0")
            return [col[0] for col in (cur.description or [])]
    except Exception as exc:
        logger.error("inspect_view('%s') falhou: %s", view_name, exc)
        return []
