"""Serve the report on loopback. Each request reads the files fresh; nothing is written."""

from __future__ import annotations

import logging
import threading
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from tournament.report.config import REPO_ROOT, ReportConfig
from tournament.report.data import collect
from tournament.report.render import page

log = logging.getLogger(__name__)
CACHE_S = 3.0  # several open pages share one read of the files


def create_app(cfg: ReportConfig, root=REPO_ROOT) -> FastAPI:
    app = FastAPI(
        title="Options Tournament report", docs_url=None, redoc_url=None, openapi_url=None
    )
    lock = threading.Lock()
    cache: dict = {"at": 0.0, "data": None}

    def fresh() -> dict:
        with lock:
            if cache["data"] is None or time.monotonic() - cache["at"] > CACHE_S:
                cache["data"] = collect(cfg, root)
                cache["at"] = time.monotonic()
            return cache["data"]

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page(fresh(), poll=True)

    @app.get("/data.json")
    def data() -> JSONResponse:
        return JSONResponse(fresh(), headers={"Cache-Control": "no-store"})

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "config": cfg.version}

    return app
