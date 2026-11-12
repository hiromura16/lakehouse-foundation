"""Generate private onboarding drafts without database or deployment access.

The same pure contract engine serves the portal and offline preview. This
router accepts only bounded metadata and choices; it never receives source
credentials, a URL, SQL, or a filesystem output path. Existing API bearer
authentication protects every route. Review checkboxes are draft notes, not
verified approval receipts, and no endpoint applies a contract.
"""

import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from onboarding_contract import (
    Discovery,
    ProposalRequest,
    builtin_discovery,
    make_plan,
    metadata_hash,
    propose,
)
from pydantic import ValidationError

MAX_BODY_BYTES = 256 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 20_000
BODY_TIMEOUT_SECONDS = 5


def input_error(status=422, code="ONBOARDING_INPUT_INVALID"):
    """Return a fixed error without reflecting private metadata or input."""
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": "入力形式・件数・文字数を確認してください。",
        },
    )


def unique_object(pairs):
    """Reject ambiguous duplicate JSON keys before any model sees them."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def reject_constant(_):
    """Reject JavaScript non-finite numbers that JSON does not define."""
    raise ValueError("Non-finite JSON number.")


def bounded_json(data):
    """Decode one UTF-8 object and bound nesting before model validation."""
    if len(data) > MAX_BODY_BYTES:
        raise ValueError("Metadata exceeds transport limit.")
    result = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    if not isinstance(result, dict):
        raise ValueError("Metadata must be a JSON object.")
    pending = [(result, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if depth > MAX_JSON_DEPTH or visited > MAX_JSON_NODES:
            raise ValueError("Metadata complexity limit.")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return result


class BoundedDraftRoute(APIRoute):
    """Limit streamed JSON before FastAPI buffers/parses a metadata body."""

    def get_route_handler(self):
        """Wrap only this router; keep existing source/SQL routes unchanged."""
        handler = super().get_route_handler()

        async def bounded_handler(request: Request):
            if request.method == "POST":
                content_type = request.headers.get("content-type", "")
                if content_type.split(";", 1)[0].strip() != (
                    "application/json"
                ):
                    return input_error(415, "ONBOARDING_JSON_REQUIRED")
                chunks = []
                size = 0
                try:
                    async with asyncio.timeout(BODY_TIMEOUT_SECONDS):
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > MAX_BODY_BYTES:
                                return input_error(
                                    413, "ONBOARDING_INPUT_TOO_LARGE"
                                )
                            chunks.append(chunk)
                    body = b"".join(chunks)
                    parsed = bounded_json(body)
                except TimeoutError:
                    return input_error(408, "ONBOARDING_INPUT_TIMEOUT")
                except (ValueError, UnicodeError, RecursionError):
                    return input_error()
                # Starlette caches these attributes. Reuse only the bytes we
                # have bounded, never trust Content-Length or re-read a stream.
                request._body = body
                request._json = parsed
            try:
                return await handler(request)
            except (
                RequestValidationError,
                ValidationError,
                ValueError,
            ):
                # Pydantic errors can contain private input and validator
                # context. Standalone tests and the main API share this guard.
                return input_error()

        return bounded_handler


def onboarding_router(authorize):
    """Attach authenticated offline draft operations, never an apply route."""
    router = APIRouter(
        prefix="/onboarding",
        tags=["ソース登録の下書き（DB変更なし）"],
        dependencies=[Depends(authorize)],
        route_class=BoundedDraftRoute,
    )

    @router.get("/example")
    def example():
        """公開定義を基にした合成6表metadataを返す。DB検出結果ではない。"""
        discovery = builtin_discovery()
        return {
            "schema_version": 1,
            "discovery": discovery,
            "metadata_sha256": metadata_hash(discovery),
            "database_checked": False,
        }

    @router.get("/schema")
    def schemas():
        """AI/APIクライアント向けに、厳密な下書き入力schemaを返す。"""
        return {
            "schema_version": 1,
            "discovery_schema": Discovery.model_json_schema(),
            "proposal_schema": ProposalRequest.model_json_schema(),
        }

    @router.post("/validate-discovery")
    def validate_discovery(discovery: Discovery):
        """metadataの形式を検証する。実接続・権限・CDC稼働は検証しない。"""
        discovery = discovery.model_copy(update={"origin": "imported"})
        return {
            "schema_version": 1,
            "discovery": discovery.model_dump(mode="json"),
            "metadata_sha256": metadata_hash(discovery),
            "database_checked": False,
        }

    @router.post("/propose")
    def proposal(request: ProposalRequest):
        """GUIの表・列・業務方針から未承認contract案とWARN/BLOCKを返す。"""
        return propose(request)

    @router.post("/plan")
    def plan(request: ProposalRequest):
        """同じ入力を再検証し、実行能力を持たない非適用計画を返す。"""
        return make_plan(request)

    return router
