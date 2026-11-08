from __future__ import annotations

import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..db import SessionLocal, User, get_global, utcnow
from ..security import flash, form_with_csrf, hash_password, verify_password
from .common import render

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_attempts: dict[str, list[float]] = {}


def _rate_limited(key: str, limit: int = 8, window: int = 600) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = hits
    return len(hits) >= limit


@router.get("/register")
async def register_form(request: Request):
    return render(request, "register.html")


@router.post("/register")
async def register(request: Request):
    f = await form_with_csrf(request)
    email = str(f.get("email", "")).strip().lower()
    name = str(f.get("name", "")).strip()[:120]
    phone = str(f.get("phone", "")).strip()[:32]
    pw, pw2 = str(f.get("password", "")), str(f.get("password2", ""))
    errors = []
    if not EMAIL_RE.match(email):
        errors.append("ایمیل معتبر نیست.")
    if len(pw) < 8:
        errors.append("رمز عبور باید حداقل ۸ کاراکتر باشد.")
    if pw != pw2:
        errors.append("تکرار رمز عبور یکسان نیست.")
    if not f.get("terms"):
        errors.append("پذیرش شرایط و ریسک‌ها لازم است.")
    with SessionLocal() as s:
        if s.scalar(select(func.count(User.id)).where(User.email == email)):
            errors.append("این ایمیل قبلاً ثبت شده است.")
        if errors:
            for e in errors:
                flash(request, e, "error")
            return render(request, "register.html", form=dict(email=email, name=name, phone=phone), status_code=400)
        gs = get_global(s)
        first_user = not s.scalar(select(func.count(User.id)))
        u = User(email=email, name=name, phone=phone, password_hash=hash_password(pw), accepted_terms_at=utcnow(),
                 is_admin=first_user, status="active" if (first_user or not gs.require_approval) else "pending")
        s.add(u)
        s.commit()
        request.session.clear()
        request.session["uid"] = u.id
    flash(request, "ثبت‌نام انجام شد." + (" حساب شما ادمین است." if first_user else
                                          " پس از تأیید ادمین معاملات شما فعال می‌شود." if u.status == "pending" else ""))
    return RedirectResponse("/", 303)


@router.get("/login")
async def login_form(request: Request):
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request):
    f = await form_with_csrf(request)
    email = str(f.get("email", "")).strip().lower()
    ip = request.client.host if request.client else "?"
    if _rate_limited(f"{ip}:{email}"):
        flash(request, "تلاش‌های زیاد. ۱۰ دقیقه دیگر امتحان کنید.", "error")
        return render(request, "login.html", status_code=429)
    with SessionLocal() as s:
        u = s.scalars(select(User).where(User.email == email)).first()
    if u is None or not verify_password(str(f.get("password", "")), u.password_hash):
        _attempts.setdefault(f"{ip}:{email}", []).append(time.time())
        flash(request, "ایمیل یا رمز عبور اشتباه است.", "error")
        return render(request, "login.html", status_code=401)
    if u.status == "suspended":
        flash(request, "حساب شما مسدود شده است.", "error")
        return render(request, "login.html", status_code=403)
    request.session.clear()
    request.session["uid"] = u.id
    return RedirectResponse("/", 303)


@router.post("/logout")
async def logout(request: Request):
    await form_with_csrf(request)
    request.session.clear()
    return RedirectResponse("/login", 303)
