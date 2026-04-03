from collections.abc import AsyncGenerator
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

try:
    from .models import Base
except ImportError:  # pragma: no cover - fallback para ejecucion local directa
    from models import Base

__all__ = ["Base", "engine", "AsyncSessionLocal", "get_db"]

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+asyncpg://fraud:fraud123@localhost:5432/fraudshield"
)

engine = create_async_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
