"""Add OpnDoor to a phone's home screen: a web app manifest, icons and a minimal service worker.

The service worker caches nothing, so door states and private pages are always fetched fresh."""
import io
import json

from fastapi import APIRouter
from fastapi.responses import Response

from .admin import BRAND_DIR, brand

router = APIRouter()
_icons: dict = {}
TEAL = "#0f766e"


def _icon(size: int) -> bytes:
    from PIL import Image, ImageDraw
    b = brand()
    key = (size, b["logo"], b["accent"], b["nav"])
    if key in _icons:
        return _icons[key]
    bg = b["nav"] or "#0f1b33"
    img = Image.new("RGB", (size, size), bg)
    if b["logo"]:
        try:
            logo = Image.open(BRAND_DIR / b["logo"]).convert("RGBA")
            logo.thumbnail((int(size * .7), int(size * .7)))
            img.paste(logo, ((size - logo.width) // 2, (size - logo.height) // 2), logo)
        except OSError:
            pass
    else:  # a door, like the favicon
        d = ImageDraw.Draw(img)
        w, h = size * .38, size * .6
        x0, y0 = (size - w) / 2, (size - h) / 2
        d.rounded_rectangle([x0, y0, x0 + w, y0 + h], radius=size * .04, fill=b["accent"] or TEAL)
        r = size * .035
        cx, cy = x0 + w * .72, size / 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="white")
    out = io.BytesIO()
    img.save(out, "PNG")
    _icons.clear() if len(_icons) > 8 else None
    _icons[key] = out.getvalue()
    return _icons[key]


@router.get("/icon-{size}.png")
def icon(size: int):
    if size not in (180, 192, 512):
        return Response(status_code=404)
    return Response(_icon(size), media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/manifest.webmanifest")
def manifest():
    b = brand()
    name = b["name"] or "OpnDoor"
    body = {"name": name, "short_name": name[:12], "start_url": "/", "scope": "/", "display": "standalone",
            "background_color": b["nav"] or "#0f1b33", "theme_color": b["nav"] or "#0f1b33",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}
    return Response(json.dumps(body), media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


SW = """// OpnDoor: lets phones install the app. Nothing is cached, so doors are always shown as they are now.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""


@router.get("/sw.js")
def service_worker():
    return Response(SW, media_type="text/javascript", headers={"Cache-Control": "no-cache"})
