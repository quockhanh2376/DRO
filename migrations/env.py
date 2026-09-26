from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.database import configure_sqlite_connection, database_url, sqlite_file_path
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    path = sqlite_file_path(database_url())
    if path:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if database_url().startswith("sqlite:"):
        from sqlalchemy import event
        event.listen(connectable, "connect", lambda connection, record: configure_sqlite_connection(connection, record, path))
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
