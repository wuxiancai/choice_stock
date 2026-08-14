from __future__ import annotations

import json
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import settings
from .database import initialize
from .services import dashboard, sync_latest

scheduler = BackgroundScheduler(timezone=settings.timezone)
templates = Jinja2Templates(directory="app/templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize()
    scheduler.add_job(sync_latest, "cron", hour=21, minute=0, id="nightly_sync", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="A股轻量选股", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {"request": request, "dashboard": dashboard(dict(request.query_params))})


@app.get("/api/dashboard")
def api_dashboard(request: Request):
    return dashboard(dict(request.query_params))


@app.post("/api/sync")
def api_sync():
    try:
        return sync_latest()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/healthz")
def healthz():
    return {"status": "ok", "scheduler_timezone": settings.timezone, "token_configured": bool(settings.tushare_token and not settings.tushare_token.startswith("replace-"))}
