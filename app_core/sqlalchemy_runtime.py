from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app_core.config import load_config


def create_sqlalchemy_engine(echo: bool = False) -> Engine:
    cfg = load_config()
    connect_args = {}
    if cfg.db_backend == "sqlite":
        connect_args["timeout"] = 20
    return create_engine(cfg.database_url, future=True, pool_pre_ping=True, echo=bool(echo), connect_args=connect_args)

