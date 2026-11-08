from __future__ import annotations

import datetime as dt
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..db import SessionLocal, User, get_global, utcnow
from ..security import flash, form_with_csrf, hash_password, verify_password
from .common import get_lang, render, tr

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_attempts: dict[str, list[float]] = {}


def _rate_limited(key: str, limit: int = 8, window: int = 600) -> bool:
    now = time.time()
    hits = [x for x in _attempts.get(key, []) if now - x < window]
    _attempts[key] = hits
    return len(hits) >= limit


@router.get("/register")
async def register_form(request: Request):
    return render(request, "register.html", form={})


@router.post("/register")
async def register(request: Request):
    f = await form_with_csrf(request)
    email = str(f.get("email", "")).strip().lower()
    name = str(f.get("name", "")).strip()[:120]
    phone = str(f.get("phone", "")).strip()[:32]
    pw, pw2 = str(f.get("password", "")), str(f.get("password2", ""))
    errors = []
    if not EMAIL_RE.match(email):
        errors.append(tr(request, "err_email"))
    if len(pw) < 8:
        errors.append(tr(request, "err_password_len"))
    if pw != pw2:
        errors.append(tr(request, "err_password_match"))
    if not f.get("terms"):
        errors.append(tr(request, "err_terms"))
    lang = get_lang(request)
    with SessionLocal() as s:
        if s.scalar(select(func.count(User.id)).where(User.email == email)):
            errors.append(tr(request, "err_email_taken"))
        if errors:
            for e in errors:
                flash(request, e, "error")
            return render(request, "register.html", form=dict(email=email, name=name, phone=phone), status_code=400)
        gs = get_global(s)
        first = not s.scalar(select(func.count(User.id)))
        u = User(email=email, name=name, phone=phone, password_hash=hash_password(pw), accepted_terms_at=utcnow(),
                 lang=lang, is_admin=first, status="active" if (first or not gs.require_approval) else "pending",
                 subscription_until=(utcnow() + dt.timedelta(days=gs.trial_days)) if gs.trial_days and not first else None)
        s.add(u)
        s.commit()
        request.session.clear()
        request.session["uid"] = u.id
        status = u.status
    flash(request, tr(request, "msg_registered") + " " +
          (tr(request, "msg_you_are_admin") if first else tr(request, "msg_pending") if status == "pending" else ""))
    return RedirectResponse("/dashboard", 303)


@router.get("/login")
async def login_form(request: Request):
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request):
    f = await form_with_csrf(request)
    email = str(f.get("email", "")).strip().lower()
    ip = request.client.host if request.client else "?"
    if _rate_limited(f"{ip}:{email}"):
        flash(request, tr(request, "err_too_many"), "error")
        return render(request, "login.html", status_code=429)
    with SessionLocal() as s:
        u = s.scalars(select(User).where(User.email == email)).first()
        if u is None or not verify_password(str(f.get("password", "")), u.password_hash):
            _attempts.setdefault(f"{ip}:{email}", []).append(time.time())
            flash(request, tr(request, "err_login"), "error")
            return render(request, "login.html", status_code=401)
        if u.status == "suspended":
            flash(request, tr(request, "err_suspended"), "error")
            return render(request, "login.html", status_code=403)
        u.last_login_at = utcnow()
        s.commit()
        uid = u.id
    request.session.clear()
    request.session["uid"] = uid
    return RedirectResponse("/dashboard", 303)


@router.post("/logout")
async def logout(request: Request):
    await form_with_csrf(request)
    request.session.clear()
    return RedirectResponse("/", 303)
