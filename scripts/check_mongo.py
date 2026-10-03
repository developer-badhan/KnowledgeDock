#!/usr/bin/env python3
"""Verify that MongoDB Atlas is reachable with the credentials in `.env`.

    uv run python scripts/check_mongo.py

Uses `pymongo`'s native async driver, the same driver the application uses, so
a pass here exercises the same code path the container will. Run it before
pushing so a network-access or credential mistake shows up locally instead of
as a 503 from `/health/ready` on Render.

Exit codes: 0 reachable, 1 connection failed, 2 configuration missing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from decouple import AutoConfig
from pymongo import AsyncMongoClient

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ("MONGODB_URI", "MONGODB_DB")


async def main() -> int:
    env = AutoConfig(search_path=REPO_ROOT)

    missing = [name for name in REQUIRED if not env(name, default="")]
    if missing:
        print(f"missing from .env: {', '.join(missing)}", file=sys.stderr)
        return 2

    client: AsyncMongoClient = AsyncMongoClient(
        env("MONGODB_URI"),
        serverSelectionTimeoutMS=8000,
        appname="knowledgedock-check",
    )
    try:
        print(await client.admin.command("ping"))
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
