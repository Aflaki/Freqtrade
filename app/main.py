"""Signal Relay: multi-user web panel + Freqtrade signal copier + Telegram notifications."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .db import init_db
from .engine.exchange import pool
from .security import LoginRequired
from .settings import config
from .telegram import bot
from .web import admin, auth, user, webhook

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await bot.start()
    yield
    await bot.stop()
    await pool.close_all()


app = FastAPI(title="Signal Relay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.secret_key, session_cookie="relay_session",
                   max_age=7 * 24 * 3600, same_site="lax", https_only=config.secure_cookies)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(webhook.router)
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(admin.router)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", 303)


@app.get("/health")
async def health():
    return {"status": "ok", "telegram": bool(bot.username)}
