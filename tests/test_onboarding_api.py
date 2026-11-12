"""Exercise authenticated, bounded draft routes using synthetic metadata.

TestClient runs the real ASGI routes in-process. No HTTP listener, database,
Docker daemon, company configuration, or onboarding apply is involved.
"""

import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient
    from starlette.requests import Request
except ModuleNotFoundError as error:
    if (
        error.name != "fastapi"
        or importlib.util.find_spec("fastapi") is not None
    ):
        raise
    raise unittest.SkipTest("Install the locked source-quality dependencies.")

DEMO = Path(__file__).resolve().parents[1] / "infra/demo"
with mock.patch.object(sys, "path", [str(DEMO), *sys.path]):
    import onboarding_api as API


class OnboardingApiTests(unittest.TestCase):
    """Keep draft validation independent of runtime settings and jobs."""

    def setUp(self):
        """Create routing only; authentication uses a synthetic token."""
        app = FastAPI()

        def authorize(authorization: str = Header(default="")):
            if authorization != "Bearer onboarding-test":
                raise HTTPException(401, "Authentication required.")

        self.router = API.onboarding_router(authorize)
        app.include_router(self.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer onboarding-test"}
        self.discovery = API.builtin_discovery()

    def payload(self):
        """Select one real fixture table, keeping approval unverified."""
        table = self.discovery["tables"][0]
        return {
            "discovery": self.discovery,
            "source_id": "example-source",
            "owner_team": "example-team",
            "selections": [
                {
                    "schema_name": table["schema_name"],
                    "table_name": table["table_name"],
                    "included_columns": [c["name"] for c in table["columns"]],
                    "logical_name": "製品マスター",
                    "business_purpose": "検証用の集計に使用する",
                }
            ],
        }

    def post(self, path, payload):
        """Send JSON through the same routing and body limits as the UI."""
        return self.client.post(
            "/onboarding/" + path,
            headers=self.headers,
            json=payload,
        )

    def test_every_route_requires_existing_authentication(self):
        for path in ("example", "schema"):
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get("/onboarding/" + path).status_code, 401
                )
        for path in ("propose", "plan", "validate-discovery"):
            with self.subTest(path=path):
                payload = (
                    self.discovery
                    if path == "validate-discovery"
                    else self.payload()
                )
                self.assertEqual(
                    self.client.post(
                        "/onboarding/" + path, json=payload
                    ).status_code,
                    401,
                )

    def test_example_and_schema_are_offline_and_not_db_evidence(self):
        with mock.patch(
            "socket.create_connection", side_effect=AssertionError("network")
        ):
            response = self.client.get(
                "/onboarding/example", headers=self.headers
            )
            schema = self.client.get(
                "/onboarding/schema", headers=self.headers
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["discovery"]["tables"]), 6)
        self.assertFalse(response.json()["database_checked"])
        self.assertIn("proposal_schema", schema.json())

    def test_proposal_and_plan_can_never_apply_or_verify_approval(self):
        for path in ("propose", "plan"):
            with self.subTest(path=path):
                response = self.post(path, self.payload())
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["can_apply"])
                self.assertFalse(response.json()["approvals_verified"])
                self.assertFalse(response.json()["database_checked"])

    def test_metadata_validation_returns_normalized_content_and_hash(self):
        result = self.post("validate-discovery", self.discovery)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["metadata_sha256"]), 64)
        self.assertEqual(
            result.json()["discovery"],
            API.Discovery.model_validate(self.discovery)
            .model_copy(update={"origin": "imported"})
            .model_dump(),
        )
        self.assertFalse(result.json()["database_checked"])

    def test_unknown_credential_fields_are_rejected_without_echo(self):
        payload = self.payload()
        payload["password"] = "private-marker"
        response = self.post("propose", payload)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-marker", response.text)
        self.assertNotIn("password", response.text)

    def test_invalid_metadata_is_not_reflected_from_validation_errors(self):
        discovery = dict(self.discovery, source_ref="private host marker")
        result = self.post("validate-discovery", discovery)
        self.assertEqual(result.status_code, 422)
        self.assertNotIn("private host marker", result.text)
        self.assertNotIn("input", result.json())

    def test_duplicate_keys_nonfinite_and_invalid_utf8_are_rejected(self):
        for body in (
            b'{"source_id":"one","source_id":"two"}',
            b'{"source_id":NaN}',
            b'{"source_id":"\xff"}',
            b"[]",
        ):
            with self.subTest(body=body):
                result = self.client.post(
                    "/onboarding/propose",
                    content=body,
                    headers={
                        **self.headers,
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(result.status_code, 422)

    def test_oversize_stream_is_rejected_without_trusting_length(self):
        result = self.client.post(
            "/onboarding/propose",
            content=iter([b" " * API.MAX_BODY_BYTES, b"x"]),
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(result.status_code, 413)

    def test_deep_json_is_rejected_before_schema_validation(self):
        value = {}
        for _ in range(API.MAX_JSON_DEPTH + 1):
            value = {"nested": value}
        result = self.client.post(
            "/onboarding/propose",
            content=json.dumps(value),
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(result.status_code, 422)

    def test_non_json_content_and_apply_are_not_supported(self):
        response = self.client.post(
            "/onboarding/propose", content="source: example"
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.post("apply", self.payload()).status_code, 404)
        openapi = self.client.get("/openapi.json").json()
        self.assertIn("/onboarding/propose", openapi["paths"])
        self.assertNotIn("/onboarding/apply", openapi["paths"])
        self.assertIn("ProposalRequest", openapi["components"]["schemas"])

    def test_body_timeout_does_not_enter_proposal_handler(self):
        """Exercise the actual wrapper with a delayed synthetic ASGI body."""
        route = next(
            item
            for item in self.router.routes
            if getattr(item, "path", None) == "/onboarding/propose"
        )

        async def receive():
            await asyncio.sleep(1)
            return {"type": "http.request", "body": b"{}"}

        async def run():
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "headers": [(b"content-type", b"application/json")],
                },
                receive=receive,
            )
            return await route.get_route_handler()(request)

        with (
            mock.patch.object(API, "BODY_TIMEOUT_SECONDS", 0.01),
            mock.patch.object(API, "propose") as propose,
        ):
            result = asyncio.run(run())
        self.assertEqual(result.status_code, 408)
        propose.assert_not_called()


if __name__ == "__main__":
    unittest.main()
