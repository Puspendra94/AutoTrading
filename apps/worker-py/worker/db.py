"""Shared asyncpg connection pool. The pool sets `search_path` to the app schema so
every query targets `Algo_Trading` without qualifying each table."""
from __future__ import annotations

import asyncpg

from .config import config

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=config.dsn,
            min_size=1,
            max_size=8,
            server_settings={"search_path": f'"{config.db_schema}",public'},
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
