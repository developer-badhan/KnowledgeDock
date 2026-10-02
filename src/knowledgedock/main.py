"""ASGI entrypoint. Uvicorn is pointed at `knowledgedock.main:app`."""

from __future__ import annotations

from knowledgedock.app import create_app

app = create_app()
