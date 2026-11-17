"""Serve the independent React entry point and pinned local Swagger assets.

Only public build assets are read here. Static paths use descriptor-relative,
no-follow opens and bounded regular files. Neither private manager state nor
configuration is interpolated into HTML. Missing build output is unavailable,
not a fabricated successful page or a route into the managed PoC.
"""

import os
import re
import stat
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import Response

ROOT = Path(__file__).absolute().parents[2]
DIST = ROOT / "frontend" / "dist"
SWAGGER = ROOT / "infra" / "management" / "static"
MAX_ASSET = 8 * 1024 * 1024
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
MIME = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".html": "text/html",
}


def read_public(root, parts):
    """Reject symlinks, special files and concurrent file replacement."""
    if not root.is_absolute() or any(
        not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,159}", part)
        or part in {".", ".."}
        for part in parts
    ):
        raise HTTPException(404, "ASSET_UNAVAILABLE")
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in (*root.parts[1:], *parts[:-1]):
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            before = os.fstat(leaf)
            if not stat.S_ISREG(before.st_mode) or not (
                0 < before.st_size <= MAX_ASSET
            ):
                raise HTTPException(503, "ASSET_UNAVAILABLE")
            blocks = []
            remaining = before.st_size + 1
            while remaining:
                block = os.read(leaf, min(remaining, 65536))
                if not block:
                    break
                blocks.append(block)
                remaining -= len(block)
            content = b"".join(blocks)
            after = os.fstat(leaf)
            if len(content) != before.st_size or any(
                getattr(before, key) != getattr(after, key)
                for key in (
                    "st_dev",
                    "st_ino",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            ):
                raise HTTPException(503, "ASSET_UNAVAILABLE")
            return content
        finally:
            os.close(leaf)
    except OSError:
        raise HTTPException(503, "ASSET_UNAVAILABLE") from None
    finally:
        if fd is not None:
            os.close(fd)


def asset_response(root, parts, *, index=False):
    extension = Path(parts[-1]).suffix
    if extension not in MIME:
        raise HTTPException(404, "ASSET_UNAVAILABLE")
    content = read_public(root, parts)
    if index:
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            raise HTTPException(503, "ASSET_UNAVAILABLE") from None
        if text.count("</head>") != 1 or "lakehouse-mode" in text:
            raise HTTPException(503, "ASSET_UNAVAILABLE")
        text = text.replace(
            "</head>",
            '<meta name="lakehouse-mode" content="manager" /></head>',
        ).replace(
            "<title>Lakehouse Studio</title>",
            "<title>Lakehouse Control</title>",
        )
        # Vision用buildでも管理入口のtitleだけを置換し、本文は保つ。
        text = text.replace(
            "<title>Lakehouse Vision</title>",
            "<title>Lakehouse Control</title>",
        )
        content = text.encode("utf-8")
    return Response(
        content,
        media_type=MIME[extension],
        headers={
            "Content-Security-Policy": CSP,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


def attach_web(app, authenticated):
    """Register fixed assets; Swagger content requires an operator session."""

    @app.get("/", include_in_schema=False)
    async def index():
        return asset_response(DIST, ("index.html",), index=True)

    @app.get("/assets/{name}", include_in_schema=False)
    async def assets(name: str):
        return asset_response(DIST, ("assets", name))

    @app.get("/swagger/{name}", include_in_schema=False)
    async def swagger(name: str, request: Request):
        await authenticated(request)
        if name not in {"swagger-ui-bundle.js", "swagger-ui.css"}:
            raise HTTPException(404, "ASSET_UNAVAILABLE")
        return asset_response(SWAGGER, (name,))
