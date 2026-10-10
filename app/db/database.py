"""
Database connection and session management
"""
import logging
from typing import AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.schema import CreateIndex
from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Create async engine (SQLite 不需要 pool 設定)
if settings.database_url.startswith("sqlite"):
    engine = create_async_engine(
        settings.database_url,
        echo=settings.app_debug,
        connect_args={"check_same_thread": False}
    )
else:
    # The collector gives every device its own session, so a cycle can hold
    # COLLECTOR_CONCURRENT connections at once. A pool smaller than that turns
    # the concurrency limit into a queue on the pool instead.
    _pool_size = max(10, settings.collector_concurrent + 5)
    engine = create_async_engine(
        settings.database_url,
        echo=settings.app_debug,
        pool_pre_ping=True,
        pool_size=_pool_size,
        max_overflow=20
    )

# Session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)


class Base(DeclarativeBase):
    """Base class for all database models"""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for getting database session.

    It yields, so the annotation is the generator, not the session: FastAPI
    decides how to handle a dependency by inspecting the function, but a
    plain "-> AsyncSession" is a lie that a type checker rightly rejects.
    """
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


def _sync_upgrade_schema(conn) -> None:
    """
    Bring an existing database up to the current model definition.

    create_all(checkfirst=True) only looks at whether a table exists. Once a
    table is there it is skipped whole, so columns and indexes added to a model
    later never reach a database that already has that table - silently, which
    is how a deployment ends up full-scanning raw_links forever. This closes
    that gap for the two changes that can be applied safely in place:

      * new nullable columns  -> ALTER TABLE ... ADD COLUMN
      * new indexes           -> CREATE INDEX IF NOT EXISTS

    Anything else (dropping or retyping a column, adding a NOT NULL column to a
    populated table) is logged and left alone - that needs a real migration.
    """
    inspector = inspect(conn)
    existing_tables = set(inspector.get_table_names())
    dialect = conn.dialect

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all() just made it, it is already current

        have_columns = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have_columns:
                continue
            if not column.nullable and column.server_default is None:
                logger.warning(
                    "Column %s.%s is missing and cannot be added automatically "
                    "(NOT NULL without a server default) - needs a migration",
                    table.name, column.name
                )
                continue
            col_type = column.type.compile(dialect)
            conn.execute(
                text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}')
            )
            logger.info("Added column %s.%s (%s)", table.name, column.name, col_type)

        have_indexes = {i["name"] for i in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in have_indexes:
                continue
            # CREATE INDEX IF NOT EXISTS: PostgreSQL >= 9.5, SQLite >= 3.8.
            # IF NOT EXISTS rather than the name check alone, because a
            # concurrently starting app/collector may be creating it too.
            ddl = str(CreateIndex(index).compile(dialect=dialect)).strip()
            ddl = ddl.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
            ddl = ddl.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
            conn.execute(text(ddl))
            logger.info("Created index %s on %s", index.name, table.name)


async def init_db():
    """Create missing tables, then upgrade tables that already exist"""
    from app.models import device, link, alert, profile, group, collector  # noqa

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sync_upgrade_schema)
