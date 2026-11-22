"""FastAPI entry point for the preview-batch demo."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import media
from .config import BASE_DIR, Settings, settings as default_settings
from .scheduler import BatchManager

logger = logging.getLogger("preview")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.input_dir.mkdir(parents=True, exist_ok=True)
        settings.work_dir.mkdir(parents=True, exist_ok=True)
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            media.ensure_fixtures(settings)
        except media.MediaError as exc:
            logger.error("failed to create fixtures: %s", exc)
            raise
        manager = BatchManager(settings)
        app.state.manager = manager
        try:
            yield
        finally:
            manager.shutdown()

    app = FastAPI(title="预览片转码批次", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    def manager(request: Request) -> BatchManager:
        return request.app.state.manager

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        mgr = manager(request)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "clip_names": list(settings.clip_names),
                "slots_total": mgr.slots_total,
                "slow_after_seconds": settings.slow_after_seconds,
                "batches": mgr.list_batches(),
            },
        )

    @app.get("/api/config")
    def api_config(request: Request):
        mgr = manager(request)
        return {
            "clip_names": list(settings.clip_names),
            "slots_total": mgr.slots_total,
            "slots_used": mgr.slots_used,
            "slow_after_seconds": settings.slow_after_seconds,
            "poll_interval_ms": 400,
        }

    @app.get("/api/batches")
    def api_batches(request: Request):
        return {"batches": manager(request).list_batches()}

    @app.post("/api/batches", status_code=201)
    async def api_create_batch(request: Request):
        mgr = manager(request)
        payload: dict = {}
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                payload = await request.json()
            except Exception:
                raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
        else:
            form = await request.form()
            payload = dict(form)
        slow_clip = str(payload.get("slow_clip", "")).strip().upper()
        if slow_clip not in settings.clip_names:
            raise HTTPException(
                status_code=400,
                detail=f"slow_clip 必须是 {', '.join(settings.clip_names)} 之一",
            )
        try:
            return mgr.create_batch(slow_clip)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/batches/{batch_id}")
    def api_batch(batch_id: str, request: Request):
        batch = manager(request).get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="批次不存在")
        return batch

    @app.get("/batches/{batch_id}/download")
    def download(batch_id: str, request: Request):
        mgr = manager(request)
        path = mgr.get_batch_file(batch_id)
        batch = mgr.get_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="批次不存在")
        if path is None:
            detail = batch.get("error") or "成片尚未就绪"
            return JSONResponse(status_code=409, content={"detail": detail})
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=f"preview_{batch_id[:8]}.mp4",
        )

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "time": time.time()}

    return app


app = create_app()
