"""Site icon (favicon, home-screen icons, web manifest). The admin can upload their own logo."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

from ..db import SessionLocal, get_global
from ..settings import DATA_DIR

router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / "static" / "icons"
TYPES = {"png": "image/png", "svg": "image/svg+xml", "ico": "image/x-icon", "webp": "image/webp"}
MAX_BYTES = 1_000_000
# an uploaded SVG is served as an image only: no scripts, no external loads
SVG_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'", "X-Content-Type-Options": "nosniff"}


def custom_icon() -> Path | None:
    for ext in TYPES:
        p = DATA_DIR / f"site-icon.{ext}"
        if p.exists():
            return p
    return None


def icon_version() -> str:
    p = custom_icon()
    return str(int(p.stat().st_mtime)) if p else "1"


def detect_type(data: bytes) -> str | None:
    head = data[:512].lstrip()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048]):
        return "svg"
    return None


def save_icon(data: bytes) -> str:
    """Validate and store an uploaded icon. Returns its type; raises ValueError if not acceptable."""
    if len(data) > MAX_BYTES:
        raise ValueError("icon must be smaller than 1 MB")
    kind = detect_type(data)
    if kind is None:
        raise ValueError("use a PNG, SVG, ICO or WEBP file")
    if kind == "svg" and (b"<script" in data.lower() or b"javascript:" in data.lower()):
        raise ValueError("SVG must not contain scripts")
    remove_icon()
    (DATA_DIR / f"site-icon.{kind}").write_bytes(data)
    return kind


def remove_icon() -> None:
    for ext in TYPES:
        (DATA_DIR / f"site-icon.{ext}").unlink(missing_ok=True)


def _serve(p: Path) -> Response:
    ext = p.suffix.lstrip(".")
    headers = {"Cache-Control": "public, max-age=86400", **(SVG_HEADERS if ext == "svg" else {})}
    return FileResponse(p, media_type=TYPES.get(ext, "application/octet-stream"), headers=headers)


@router.get("/site-icon")
async def site_icon():
    return _serve(custom_icon() or STATIC / "icon.svg")


@router.get("/favicon.ico")
async def favicon():
    c = custom_icon()
    return _serve(c if c and c.suffix in (".ico", ".png") else STATIC / "favicon.ico")


@router.get("/apple-touch-icon.png")
async def apple_touch_icon():
    c = custom_icon()
    return _serve(c if c and c.suffix == ".png" else STATIC / "icon-180.png")


@router.get("/manifest.webmanifest")
async def manifest():
    with SessionLocal() as s:
        gs = get_global(s)
    c = custom_icon()
    icons = ([{"src": f"/site-icon?v={icon_version()}", "sizes": "any", "type": TYPES[c.suffix.lstrip('.')]}] if c else
             [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
              {"src": "/static/icons/icon.svg", "sizes": "any", "type": "image/svg+xml"}])
    return JSONResponse({"name": gs.site_name, "short_name": gs.site_name[:12], "start_url": "/dashboard",
                         "display": "standalone", "background_color": "#0d0b1f", "theme_color": "#7c5cff", "icons": icons},
                        media_type="application/manifest+json")


@router.get("/icon/{kind}/{name}")
async def market_icon(kind: str, name: str):
    """Coin / exchange icon from our own cache (fetched once from CoinGecko), else a letter badge."""
    from ..engine import marketdata
    if kind not in ("coin", "ex"):
        return Response(status_code=404)
    name = name.split("/")[0].split(".")[0]
    if kind == "coin":
        name = name.upper().split("USDT")[0] if name.upper().endswith("USDT") and len(name) > 4 else name.upper()
    p = await marketdata.icon_file("coin" if kind == "coin" else "exchange", name)
    headers = {"Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff"}
    if p is None:
        return Response(marketdata.letter_badge(name), media_type="image/svg+xml",
                        headers={**SVG_HEADERS, "Cache-Control": "public, max-age=86400"})
    ctype = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}[p.suffix.lstrip(".")]
    if ctype == "image/svg+xml":
        headers.update(SVG_HEADERS)
    return FileResponse(p, media_type=ctype, headers=headers)
