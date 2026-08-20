from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app_core.runtime_db import resolve_runtime_database


def _as_bool(value: str | None, default: bool) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "sim", "on"}


def _as_int(value: str | None, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(str(value or "").strip())
    except Exception:
        parsed = int(default)
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path
    data_dir: Path
    static_dir: Path
    backup_dir: Path
    log_dir: Path
    database_url: str
    db_backend: str
    sqlite_db_path: str
    host: str
    port: int
    flask_env: str
    debug: bool
    secret_key: str
    session_max_age_seconds: int
    session_cleanup_interval_seconds: int
    login_rate_window_seconds: int
    login_rate_max_failures: int
    login_rate_lock_seconds: int
    max_server_workers: int
    request_timeout_seconds: int
    stats_maintenance_interval_seconds: int
    secure_cookie: bool

    @property
    def is_production(self) -> bool:
        return str(self.flask_env).strip().lower() == "production"


_CONFIG_CACHE: AppConfig | None = None


def load_config(force_reload: bool = False) -> AppConfig:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)

    data_dir = root / "data"
    static_dir = root / "static"
    backup_dir = root / "backups"
    log_dir = root / "logs"
    for path in (data_dir, static_dir, backup_dir, log_dir):
        path.mkdir(parents=True, exist_ok=True)

    db_target = resolve_runtime_database(
        root_dir=root,
        explicit_database_url=os.environ.get("DATABASE_URL") or os.environ.get("LOGISTICA_DATABASE_URL"),
        legacy_sqlite_path=os.environ.get("LOGISTICA_DB_PATH"),
    )

    flask_env = (os.environ.get("FLASK_ENV") or "production").strip() or "production"
    debug = _as_bool(os.environ.get("DEBUG"), default=flask_env.lower() != "production")
    if flask_env.lower() == "production":
        debug = _as_bool(os.environ.get("DEBUG"), default=False) and _as_bool(
            os.environ.get("LOGISTICA_ALLOW_PROD_DEBUG"),
            default=False,
        )
    secret_key = (os.environ.get("SECRET_KEY") or "").strip()
    if not secret_key:
        if flask_env.lower() == "production" and not _as_bool(os.environ.get("LOGISTICA_ALLOW_EPHEMERAL_SECRET"), default=False):
            raise RuntimeError(
                "SECRET_KEY obrigatoria em producao. Defina no .env ou variavel de ambiente."
            )
        secret_key = secrets.token_urlsafe(32)

    cfg = AppConfig(
        root_dir=root,
        data_dir=data_dir,
        static_dir=static_dir,
        backup_dir=backup_dir,
        log_dir=log_dir,
        database_url=db_target.database_url,
        db_backend=db_target.backend,
        sqlite_db_path=db_target.sqlite_path,
        host=(os.environ.get("APP_HOST") or os.environ.get("LOGISTICA_HOST") or "127.0.0.1").strip() or "127.0.0.1",
        port=_as_int(os.environ.get("APP_PORT") or os.environ.get("LOGISTICA_PORT"), default=3000, minimum=1),
        flask_env=flask_env,
        debug=debug,
        secret_key=secret_key,
        session_max_age_seconds=_as_int(os.environ.get("LOGISTICA_SESSION_MAX_AGE"), default=28800, minimum=60),
        session_cleanup_interval_seconds=_as_int(os.environ.get("LOGISTICA_SESSION_CLEANUP_INTERVAL"), default=120, minimum=10),
        login_rate_window_seconds=_as_int(os.environ.get("LOGISTICA_LOGIN_RATE_WINDOW"), default=600, minimum=60),
        login_rate_max_failures=_as_int(os.environ.get("LOGISTICA_LOGIN_MAX_FAILURES"), default=6, minimum=1),
        login_rate_lock_seconds=_as_int(os.environ.get("LOGISTICA_LOGIN_LOCK_SECONDS"), default=300, minimum=10),
        max_server_workers=_as_int(os.environ.get("LOGISTICA_MAX_WORKERS"), default=40, minimum=4),
        request_timeout_seconds=_as_int(os.environ.get("LOGISTICA_REQUEST_TIMEOUT"), default=30, minimum=10),
        stats_maintenance_interval_seconds=_as_int(
            os.environ.get("LOGISTICA_STATS_MAINTENANCE_INTERVAL"),
            default=90,
            minimum=10,
        ),
        secure_cookie=_as_bool(os.environ.get("LOGISTICA_SECURE_COOKIE"), default=False),
    )
    _CONFIG_CACHE = cfg
    return cfg
