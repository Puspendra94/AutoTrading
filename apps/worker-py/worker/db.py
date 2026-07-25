"""Shared asyncpg connection pool. The pool sets `search_path` to the app schema so
every query targets `Algo_Trading` without qualifying each table, and registers a jsonb
codec so jsonb columns encode/decode as Python objects (dicts/lists) transparently."""
from __future__ import annotations

import json

import asyncpg

from .config import config

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    # Pass/receive jsonb as Python objects instead of raw JSON strings (used by the strategy
    # generator for parameters_json / backtest summaries / regime_tags).
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=config.dsn,
            min_size=1,
            max_size=8,
            server_settings={"search_path": f'"{config.db_schema}",public'},
            init=_init_conn,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
