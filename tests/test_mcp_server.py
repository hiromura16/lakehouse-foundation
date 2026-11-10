"""Exercise official MCP schemas, read/write boundaries and HTTP auth."""

import importlib.util
import json
import unittest
from pathlib import Path

# Only a genuinely absent optional package skips this test module. A broken
# installed package or any application import failure must remain an error.
try:
    from mcp import Client
except ModuleNotFoundError as error:
    if error.name != "mcp" or importlib.util.find_spec("mcp") is not None:
        raise
    raise unittest.SkipTest(
        "Optional tests need mcp. In a uv virtual environment, run "
        "uv pip sync --require-hashes requirements-dev.lock."
    ) from error

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "foundation_mcp_subject", ROOT / "infra/mcp/server.py"
)
SUBJECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUBJECT)


class FakeBackend:
    """Record fixed operations without any network or source credentials."""

    def __init__(self):
        self.calls = []

    def call(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return {"path": path, "ok": True}


class ToolTests(unittest.IsolatedAsyncioTestCase):
    """Use the real SDK client/server protocol in memory."""

    async def test_default_server_has_only_read_tools(self):
        backend = FakeBackend()
        async with Client(SUBJECT.create_server(backend)) as client:
            result = await client.list_tools()
            self.assertEqual(len(result.tools), 9)
            for tool in result.tools:
                annotation = tool.annotations.model_dump(by_alias=True)
                self.assertTrue(annotation["readOnlyHint"])
                self.assertIsNotNone(tool.output_schema)
            self.assertNotIn(
                "generate_sources", {tool.name for tool in result.tools}
            )
            resources = await client.list_resources()
            self.assertEqual(len(resources.resources), 2)
        self.assertEqual(backend.calls, [])

    async def test_context_is_typed_and_structured(self):
        backend = FakeBackend()
        async with Client(SUBJECT.create_server(backend)) as client:
            result = await client.call_tool(
                "gold_context", {"dataset": "sales", "limit": 20}
            )
            self.assertEqual(result.structured_content["path"], "/ai/context")
        self.assertEqual(
            backend.calls,
            [
                (
                    "/ai/context",
                    {"parameters": {"dataset": "sales", "limit": 20}},
                )
            ],
        )

    async def test_unknown_dataset_never_reaches_backend(self):
        backend = FakeBackend()
        async with Client(SUBJECT.create_server(backend)) as client:
            for arguments in [
                {"dataset": "sales; DELETE", "limit": 1},
                {"dataset": "sales", "limit": 501},
            ]:
                result = await client.call_tool("gold_context", arguments)
                self.assertTrue(result.is_error)
        self.assertEqual(backend.calls, [])

    async def test_paths_cannot_be_supplied_as_help_or_job_identifiers(self):
        backend = FakeBackend()
        async with Client(SUBJECT.create_server(backend)) as client:
            for name, arguments in [
                ("operator_help", {"topic": "../../private"}),
                ("job_status", {"job_id": "https://example.invalid"}),
            ]:
                result = await client.call_tool(name, arguments)
                self.assertTrue(result.is_error)
        self.assertEqual(backend.calls, [])

    async def test_mutations_need_opt_in_and_per_call_confirmation(self):
        backend = FakeBackend()
        async with Client(
            SUBJECT.create_server(backend, mutations=True)
        ) as client:
            result = await client.list_tools()
            names = {tool.name for tool in result.tools}
            self.assertTrue(
                {"generate_sources", "run_pipeline", "change_cdc_demo"}
                <= names
            )
            denied = await client.call_tool(
                "generate_sources", {"confirm": False}
            )
            self.assertTrue(denied.is_error)
            self.assertEqual(backend.calls, [])
            accepted = await client.call_tool(
                "generate_sources", {"confirm": True, "orders": 2}
            )
            self.assertFalse(accepted.is_error)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(backend.calls[0][0], "/jobs")
        self.assertFalse(backend.calls[0][1]["payload"]["allow_sql_write"])
        self.assertFalse(backend.calls[0][1]["payload"]["allow_file_faults"])


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    """Authenticate before any MCP routing while preserving ASGI lifespan."""

    async def invoke(self, path="/mcp", header=None, scope_type="http"):
        called = []
        messages = []

        async def inner(scope, _receive, send):
            called.append(scope["type"])
            if scope["type"] == "http":
                await send({"type": "http.response.start", "status": 204})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        token = "synthetic" * 4
        gateway = SUBJECT.BearerGate(inner, token, True)
        headers = [] if header is None else [(b"authorization", header)]
        await gateway(
            {
                "type": scope_type,
                "path": path,
                "method": "GET" if path == "/health" else "POST",
                "headers": headers,
            },
            receive,
            send,
        )
        return called, messages

    async def test_missing_or_wrong_token_never_reaches_sdk(self):
        for header in [None, b"Bearer invalid"]:
            called, messages = await self.invoke(header=header)
            self.assertEqual(called, [])
            self.assertEqual(messages[0]["status"], 401)
            self.assertNotIn("synthetic", str(messages))

    async def test_valid_token_and_lifespan_are_forwarded(self):
        called, messages = await self.invoke(
            header=("Bearer " + "synthetic" * 4).encode()
        )
        self.assertEqual(called, ["http"])
        self.assertEqual(messages[0]["status"], 204)
        called, _ = await self.invoke(scope_type="lifespan")
        self.assertEqual(called, ["lifespan"])

    async def test_health_has_no_authentication_material(self):
        called, messages = await self.invoke(path="/health")
        self.assertEqual(called, [])
        self.assertEqual(messages[0]["status"], 200)
        payload = json.loads(messages[1]["body"])
        self.assertEqual(payload, {"ready": True, "mutations_enabled": False})


if __name__ == "__main__":
    unittest.main()
