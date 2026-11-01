from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Base

engine = create_async_engine(settings.database_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# Lightweight additive migrations: (table, column, column definition).
# create_all handles brand-new tables; this only adds columns to existing tables.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("campaigns", "batch_id", "INTEGER"),
    ("batches", "scrape_profile", "TEXT DEFAULT 'safe'"),
]


async def _apply_column_migrations(conn) -> None:
    for table, column, definition in _COLUMN_MIGRATIONS:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {row[1] for row in result.fetchall()}
        if column not in existing:
            await conn.exec_driver_sql(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )


async def init_db() -> None:
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    settings.profiles_dir.mkdir(parents=True, exist_ok=True)
    (settings.project_root / "data").mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_column_migrations(conn)
        # Reduce "database is locked" stalls when UI polls while scraping writes.
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA busy_timeout=30000")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
