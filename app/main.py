"""Signal Relay: multi-user, multi-language panel + Freqtrade signal copier + Telegram."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .db import SessionLocal, get_global, init_db
from .engine import benchmarks
from .engine.botmonitor import monitor
from .engine.exchange import pool
from .security import LoginRequired, decrypt
from .settings import SECRET_KEY
from .telegram import bot
from .web import admin, admin_bots, auth, icons, public, user, webhook

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as s:
        token = decrypt(get_global(s).telegram_token_enc) or os.getenv("TELEGRAM_BOT_TOKEN", "")
    await bot.configure(token)
    jobs = [asyncio.create_task(bot.daily_jobs()), asyncio.create_task(monitor.loop()),
            asyncio.create_task(benchmarks.loop())]
    yield
    for j in jobs:
        j.cancel()
    await bot.stop()
    await pool.close_all()


app = FastAPI(title="Signal Relay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="relay_session",
                   max_age=7 * 24 * 3600, same_site="lax",
                   https_only=os.getenv("SECURE_COOKIES", "false").lower() in ("1", "true", "yes"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
for r in (webhook.router, icons.router, public.router, auth.router, user.router, admin_bots.router, admin.router):
    app.include_router(r)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", 303)


@app.get("/health")
async def health():
    return {"status": "ok", "telegram": bool(bot.username)}
