"""One-shot backfill CLI, for the long initial full-history import.

    python -m worker.backfill                 # all configured intervals, full history
    python -m worker.backfill 1h              # just 1h
    python -m worker.backfill 1m 2024-01      # 1m, starting from 2024-01 (override start)
    python -m worker.backfill 1m --from-start # 1m, IGNORE the resume point and re-import all

--from-start is what you need after adding a column. Normally the run resumes from the newest
stored month, which means rows already present keep their NULLs forever no matter how often you
re-run it — the start override alone does NOT help, because the resume point wins over it.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from .binance_archive import backfill_symbol_interval, backfill_all
from .config import config
from .db import close_pool

logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s [%(name)s] %(message)s")


async def _run(argv: list[str]) -> None:
    try:
        if not argv:
            results = await backfill_all()
        else:
            args = [a for a in argv if a != "--from-start"]
            from_start = "--from-start" in argv
            interval = args[0]
            if len(args) > 1:
                # override start month for this run (e.g. a quick recent-window smoke test)
                object.__setattr__(config, "backfill_start", args[1])
            results = [await backfill_symbol_interval(
                config.backfill_symbol, interval, from_start=from_start)]
        print(results)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_run(sys.argv[1:]))
