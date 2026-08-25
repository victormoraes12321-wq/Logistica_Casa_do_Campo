from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app_core.config import load_config
from app_core.sqlalchemy_models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic pode ser chamado programaticamente depois que a aplicação já carregou
# outra DATABASE_URL. Recarregar evita migrar acidentalmente o alvo em cache.
cfg = load_config(force_reload=True)
config.set_main_option("sqlalchemy.url", cfg.database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    try:
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, compare_server_default=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        # Essencial no Windows: libera o arquivo SQLite para backup/limpeza.
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
