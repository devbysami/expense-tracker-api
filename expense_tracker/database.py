"""
Async SQLAlchemy 2.0 setup.

Django comparison:
  - engine         ~ DATABASES setting / connection pool
  - AsyncSession   ~ the ORM "connection" you use for queries (no global, it's per request)
  - get_db()       ~ a dependency FastAPI injects into every view that asks for it
"""
import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from dotenv import load_dotenv
from sqlalchemy.orm import DeclarativeBase

load_dotenv()  # reads .env from the project root into os.environ (never overrides vars already set)

DATABASE_URL = os.environ["DATABASE_URL"]  # postgresql+asyncpg://... (async driver)

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)

# expire_on_commit=False -> objects stay usable after commit (otherwise accessing
# an attribute would trigger a lazy load, which is NOT allowed in async code).
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
