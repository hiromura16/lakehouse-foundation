#!/usr/bin/env python3
"""Compile済みReactと本物のBFFを、合成ASGI内で結合検査する。

先にfrontendでnpm run buildを実行する。公開distだけを一時領域へ複製し、
静的asset、認証cookie、固定backendへのproxyを実際のgatewayで確認する。
ブラウザー・実network・Docker・private設定・会社DBには接続しない。
JavaScriptのブラウザー実行や画面の描画成功を証明する検査ではない。
"""

import asyncio
import os
import re
import stat
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/demo"))
import portal_gateway  # noqa: E402


class Assets(HTMLParser):
    """同梱されたHTMLからscriptとstylesheetの固定参照だけを取り出す。"""

    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and "src" in values:
            self.paths.append(values["src"])
        if tag == "link" and values.get("rel") == "stylesheet":
            self.paths.append(values.get("href", ""))


def copy_dist(target):
    """生成物だけを上限付きで複製する。symlinkや未知拡張子は拒否する。"""
    source = ROOT / "frontend/dist"
    if source.is_symlink() or not source.is_dir():
        raise ValueError(
            "Build the frontend before its ASGI integration check."
        )
    count = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Build artifacts must not contain symlinks.")
        if path.is_dir():
            continue
        relative = path.relative_to(source)
        if path.suffix not in {".html", ".css", ".js", ".svg", ".woff2"}:
            raise ValueError("Unexpected build artifact type.")
        count += 1
        if count > 32:
            raise ValueError("Unexpected build artifact count.")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as output:
            metadata = os.fstat(output.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > portal_gateway.RESPONSE_LIMIT
            ):
                raise ValueError("Unexpected build artifact size or type.")
            data = output.read(portal_gateway.RESPONSE_LIMIT + 1)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    return count


async def verify(dist):
    """実際のHTML/JS/CSSと認証付きproxyを、外部接続なしで検査する。"""
    token = "s" * 48
    origin = "https://portal.example.invalid"
    calls = []

    async def backend(request):
        calls.append(request)
        return httpx.Response(200, json={"synthetic": True})

    app = portal_gateway.create_app(
        {
            "CONTROL_API_TOKEN": token,
            "PORTAL_DIST_DIR": str(dist),
            "PORTAL_LINK_MODE": "public",
            "PORTAL_PUBLIC_ORIGIN": origin,
            "PORTAL_PROJECT_NAME": "synthetic-build-check",
        },
        transport=httpx.MockTransport(backend),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=origin
    ) as client:
        response = await client.get("/_stcore/health")
        assert response.status_code == 200
        response = await client.get("/")
        assert response.status_code == 200
        assert "Lakehouse Studio" in response.text
        assert (
            "unsafe-inline" not in response.headers["content-security-policy"]
        )
        parser = Assets()
        parser.feed(response.text)
        assert len(parser.paths) >= 2
        for path in parser.paths:
            assert re.fullmatch(r"/assets/[A-Za-z0-9_.-]+\.(js|css)", path)
            asset = await client.get(path)
            assert asset.status_code == 200 and asset.content
            assert token not in asset.text
        assert (await client.get("/api/control/catalog")).status_code == 401
        response = await client.post(
            "/api/session",
            json={"token": token},
            headers={"Origin": origin, "X-Poc-Request": "1"},
        )
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        assert token not in response.text + response.headers["set-cookie"]
        response = await client.get("/api/control/catalog")
        assert response.status_code == 200
        assert response.json() == {"synthetic": True}
        assert len(calls) == 1
        assert str(calls[0].url) == "http://demo-api:8000/catalog"
        assert calls[0].headers["Authorization"] == "Bearer " + token
        assert "cookie" not in calls[0].headers


def main():
    """一時領域だけを使い、失敗時も元例外・資格情報を出力しない。"""
    try:
        with tempfile.TemporaryDirectory(prefix="react-build-asgi-") as name:
            dist = Path(name) / "dist"
            dist.mkdir(mode=0o700)
            count = copy_dist(dist)
            asyncio.run(verify(dist))
    except Exception:
        print("FAIL: React build/ASGI contract; no live service contacted.")
        return 1
    print(
        f"PASS: {count} compiled React assets, BFF and synthetic backend; "
        "browser/render acceptance remains separate."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
