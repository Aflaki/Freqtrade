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
from .engine import benchmarks, marketdata, markets
from .engine.botmonitor import monitor
from .engine.exchange import pool
from .security import EmailNotConfirmed, LoginRequired, decrypt
from .settings import SECRET_KEY
from .telegram import bot
from .web import account_security, admin, admin_bots, admin_pairs, admin_security, admin_servers, auth, guides, icons, public, push, user, watchdog, webhook

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # open trades are polled every 15 s: no line per request


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as s:
        token = decrypt(get_global(s).telegram_token_enc) or os.getenv("TELEGRAM_BOT_TOKEN", "")
    await bot.configure(token)
    jobs = [asyncio.create_task(bot.daily_jobs()), asyncio.create_task(monitor.loop()), asyncio.create_task(monitor.live_loop()), asyncio.create_task(markets.loop()), asyncio.create_task(marketdata.loop()),
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
for r in (webhook.router, watchdog.router, push.router, icons.router, public.router, guides.router, auth.router, account_security.router, user.router, admin_bots.router,
          admin_security.router, admin_servers.router, admin_pairs.router, admin.router):
    app.include_router(r)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", 303)


@app.exception_handler(EmailNotConfirmed)
async def _confirm_first(request: Request, exc: EmailNotConfirmed):
    return RedirectResponse("/verify-email", 303)


@app.get("/health")
async def health():
    return {"status": "ok", "telegram": bool(bot.username)}
