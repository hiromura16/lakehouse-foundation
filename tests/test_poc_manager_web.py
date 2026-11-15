"""Real temporary public-asset reads; no Docker, host state or credentials."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI, HTTPException

from infra.management.web import attach_web, read_public


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "assets").mkdir()
        (self.root / "index.html").write_text(
            "<html><head><title>Lakehouse Studio</title></head>"
            '<body><div id="root"></div></body></html>'
        )
        (self.root / "assets" / "index-abc.js").write_text("public")
        (self.root / "swagger-ui.css").write_text("public")
        self.app = FastAPI()

        async def authenticated(request):
            if request.headers.get("x-synthetic-auth") != "operator":
                raise HTTPException(401, "AUTH_REQUIRED")

        attach_web(self.app, authenticated)
        for name in ("DIST", "SWAGGER"):
            self.addCleanup(patch.stopall)
            patch("infra.management.web." + name, self.root).start()

    def get(self, path, **kwargs):
        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                return await client.get(path, **kwargs)

        return asyncio.run(run())

    def test_manager_meta_only_in_public_html(self):
        result = self.get("/")
        self.assertEqual(result.status_code, 200)
        self.assertIn('name="lakehouse-mode" content="manager"', result.text)
        self.assertIn("Lakehouse Control", result.text)
        self.assertNotIn(
            "lakehouse-mode", (self.root / "index.html").read_text()
        )
        self.assertIn(
            "script-src 'self'", result.headers["content-security-policy"]
        )
        self.assertEqual(result.headers["cache-control"], "no-store")

    def test_fixed_assets_and_no_api_spa_fallback(self):
        self.assertEqual(self.get("/assets/index-abc.js").status_code, 200)
        self.assertEqual(self.get("/api/private").status_code, 404)
        self.assertEqual(self.get("/internal/private").status_code, 404)
        self.assertEqual(self.get("/assets/site.env").status_code, 404)

    def test_swagger_requires_authentication(self):
        self.assertEqual(self.get("/swagger/swagger-ui.css").status_code, 401)
        self.assertEqual(
            self.get(
                "/swagger/swagger-ui.css",
                headers={"x-synthetic-auth": "operator"},
            ).status_code,
            200,
        )

    def test_file_and_ancestor_symlinks_rejected(self):
        (self.root / "link.js").symlink_to("index.html")
        (self.root / "other").symlink_to("assets", target_is_directory=True)
        for parts in (("link.js",), ("other", "index-abc.js")):
            with self.assertRaises(HTTPException):
                read_public(self.root, parts)

    def test_missing_and_traversal_and_oversized_files_fail_closed(self):
        for parts in (("missing.js",), ("..", "index.html")):
            with self.assertRaises(HTTPException):
                read_public(self.root, parts)
        with patch("infra.management.web.MAX_ASSET", 1):
            with self.assertRaises(HTTPException):
                read_public(self.root, ("index.html",))

    def test_duplicate_meta_is_not_injected_again(self):
        (self.root / "index.html").write_text(
            '<head><meta name="lakehouse-mode" content="manager"></head>'
        )
        self.assertEqual(self.get("/").status_code, 503)


if __name__ == "__main__":
    unittest.main()
