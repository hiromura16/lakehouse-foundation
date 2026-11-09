"""Add bounded discovery, previews, offline help and network diagnostics."""

import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from poc_catalog import (
    HELP_ASSETS,
    HELP_DOCUMENTS,
    ROOT,
    capabilities,
    dataset_catalog,
)
from poc_quality import quality_counts
from poc_services import probe_services, service_cards
from proxy_checks import configuration_report, run_checks
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Capability(BaseModel):
    """Describe intent and mutation scope without giving an execution tool."""

    id: str
    method: Literal["GET", "POST", "PATCH"]
    path: str
    access: Literal["read", "write"]
    required_profiles: list[str]
    profile_match: Literal["any"] = "any"
    service: Literal["control_api", "mcp"] = "control_api"
    description: str
    input_notes: str


class Capabilities(BaseModel):
    """Expose the fixed API surface to people and AI clients."""

    version: Literal[1]
    capabilities: list[Capability]


class Column(BaseModel):
    """Describe a public contract column, never private source metadata."""

    name: str
    type: str
    description: str


class Dataset(BaseModel):
    """State grain, business key and tracked provenance explicitly."""

    id: str
    title: str
    layer: Literal["silver", "gold"]
    table: str
    columns: list[Column]
    grain: str
    primary_key: list[str]
    provenance: list[str]


class Catalog(BaseModel):
    """List the six Silver contracts and two fixed Gold views."""

    version: Literal[1]
    datasets: list[Dataset]


class ServiceCard(BaseModel):
    """Distinguish enablement, configuration and observed reachability."""

    id: str
    title: str
    profile: str
    profiles: list[str] = Field(default_factory=list)
    profile_match: Literal["any"] = "any"
    enabled: bool
    configured: bool
    state: Literal["not_enabled", "unconfigured", "available", "unreachable"]
    url: str
    description: str
    missing_settings: list[str]


class ServiceList(BaseModel):
    """Show applied configuration and whether saved changes await restart."""

    services: list[ServiceCard]
    pending_restart: bool
    configuration_scope: Literal["applied"] = "applied"


class HelpEntry(BaseModel):
    """Identify an allowlisted document bundled with the image."""

    slug: str
    title: str
    description: str
    available: bool


class AssetEntry(BaseModel):
    """Identify an offline diagram without exposing a filesystem path."""

    id: str
    title: str
    available: bool


class HelpList(BaseModel):
    """List only approved repository documentation and diagrams."""

    documents: list[HelpEntry]
    assets: list[AssetEntry]


class HelpDocument(BaseModel):
    """Return one bounded public Markdown document."""

    slug: str
    title: str
    markdown: str


class HelpAsset(BaseModel):
    """Return one bundled SVG through authenticated server-side transport."""

    id: str
    title: str
    media_type: Literal["image/svg+xml"] = "image/svg+xml"
    content: str


class QualityRow(BaseModel):
    """Keep quality output to public dataset IDs and nonnegative counts."""

    dataset: str
    bronze_rows: int = Field(ge=0)
    silver_rows: int = Field(ge=0)
    quarantine_rows: int = Field(ge=0)


class QualityView(BaseModel):
    """Report current per-table counts without claiming global consistency."""

    datasets: list[QualityRow] = Field(max_length=6)
    consistent_snapshot: Literal[False] = False
    description: str


class JobList(BaseModel):
    """Return bounded process-local history; restarting loses this history."""

    jobs: list[dict] = Field(max_length=20)
    persistent: Literal[False] = False
    history_limit: Literal[20] = 20


class NetworkCheck(BaseModel):
    """Allow only fixed target identifiers and an explicit settings scope."""

    model_config = ConfigDict(extra="forbid", strict=True)
    targets: list[Literal["pypi", "maven", "npm", "github"]] = Field(
        default_factory=lambda: ["pypi", "maven", "npm", "github"],
        min_length=1,
        max_length=4,
    )
    use_saved: bool = False

    @model_validator(mode="after")
    def distinct_targets(self):
        """Reject repeated checks before any outbound traffic is started."""
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("Duplicate diagnostic targets.")
        return self


def reject(status, code, message):
    """Send only application-owned, secret-free error messages."""
    raise HTTPException(status, detail={"code": code, "message": message})


def public_file(path: Path, maximum: int) -> str:
    """Read a known repository file with symlink and size checks."""
    if (
        not path.is_file()
        or path.is_symlink()
        or not path.resolve().is_relative_to((ROOT / "docs").resolve())
        or any(parent.is_symlink() for parent in path.parents)
        or path.stat().st_size > maximum
    ):
        raise ValueError("Bundled public document unavailable.")
    with path.open("rb") as source:
        content = source.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError("Bundled public document exceeds limit.")
    return content.decode()


class DiagnosticCaUnavailable(ValueError):
    """Report an unavailable private CA without returning its path."""


def diagnostic_inputs(original):
    """Select operator proxy inputs, excluding fixed runtime CA variables."""
    return {
        key: original[key]
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "CORPORATE_CA_FILE",
        )
        if key in original
    }


def network_values(store, use_saved):
    """Map only dedicated-private CA files, never a request-supplied path."""
    original = store.read() if use_saved else store.applied
    values = diagnostic_inputs(original)
    for key in ("CORPORATE_CA_FILE",):
        if not values.get(key):
            continue
        path = Path(values[key])
        host = Path(original.get("CONTROL_SETTINGS_DIR", ""))
        if path.is_absolute() and path.is_relative_to(host):
            path = store.path.parent / path.relative_to(host)
        if (
            not path.is_absolute()
            or not path.is_file()
            or not path.resolve().is_relative_to(store.path.parent.resolve())
            or any(item.is_symlink() for item in (path, *path.parents))
        ):
            raise DiagnosticCaUnavailable(
                "Private diagnostic CA is unavailable."
            )
        values[key] = str(path)
    return values


def portal_router(
    store,
    jobs,
    authorize,
    ai_exporter,
    quality_reader=None,
    service_probe=None,
    network_runner=None,
):
    """Share discovery APIs without Docker, shell or arbitrary SQL access."""
    quality_reader = quality_reader or quality_counts
    service_probe = service_probe or probe_services
    network_runner = network_runner or run_checks
    router = APIRouter(dependencies=[Depends(authorize)], tags=["PoCポータル"])

    @router.get("/capabilities", response_model=Capabilities)
    def get_capabilities():
        """Expose operation intent and explicit write classification."""
        return capabilities(store.applied)

    @router.get("/catalog", response_model=Catalog)
    def get_catalog():
        """Read tracked contract definitions without connecting to a DB."""
        return dataset_catalog()

    @router.get("/catalog/preview")
    def preview(
        dataset: Literal["all", "production", "sales"] = "all",
        limit: int = Query(default=20, ge=1, le=500),
    ):
        """Reuse the fixed snapshot-pinned exporter, never caller SQL."""
        selected = ["production", "sales"] if dataset == "all" else [dataset]
        try:
            return json.loads(
                json.dumps(ai_exporter(selected, limit), default=str)
            )
        except Exception:
            reject(
                409,
                "GOLD_UNAVAILABLE",
                "メダリオン実行後に再試行してください。",
            )

    @router.get("/quality", response_model=QualityView)
    def quality():
        """Read six fixed count rows with a deadline and cancellation."""
        try:
            return {
                "datasets": quality_reader(),
                "description": (
                    "各表の現在件数です。表間の同一時点は保証しません。"
                ),
            }
        except Exception:
            reject(
                409,
                "QUALITY_UNAVAILABLE",
                "メダリオンとTrinoの状態を確認します。",
            )

    @router.get("/services", response_model=ServiceList)
    def services():
        """Probe applied services without exposing credentials."""
        return {
            "services": service_cards(
                store.applied, service_probe(store.applied)
            ),
            "pending_restart": store.public()["pending_restart"],
        }

    @router.get("/jobs", response_model=JobList)
    def recent_jobs(limit: int = Query(default=20, ge=1, le=20)):
        """Review recent results without automatically retrying any job."""
        return {"jobs": jobs.history(limit)}

    @router.get("/help", response_model=HelpList)
    def help_list():
        """Return fixed slugs, never a directory listing or caller path."""
        return {
            "documents": [
                {
                    "slug": key,
                    "title": item[0],
                    "description": item[2],
                    "available": (ROOT / "docs" / item[1]).is_file(),
                }
                for key, item in HELP_DOCUMENTS.items()
            ],
            "assets": [
                {
                    "id": key,
                    "title": item[0],
                    "available": (ROOT / "docs/assets" / item[1]).is_file(),
                }
                for key, item in HELP_ASSETS.items()
            ],
        }

    @router.get("/help/assets/{identifier}", response_model=HelpAsset)
    def help_asset(identifier: str):
        """Serve only two approved offline diagrams through the API."""
        if identifier not in HELP_ASSETS:
            reject(404, "HELP_NOT_FOUND", "指定した図が見つかりません。")
        title, filename = HELP_ASSETS[identifier]
        try:
            content = public_file(ROOT / "docs/assets" / filename, 512 * 1024)
            return {"id": identifier, "title": title, "content": content}
        except (OSError, ValueError):
            reject(
                503,
                "HELP_UNAVAILABLE",
                "文書を同梱したimageへ更新してください。",
            )

    @router.get("/help/{slug}", response_model=HelpDocument)
    def help_document(slug: str):
        """Read bounded public Markdown from an explicit allowlist."""
        if slug not in HELP_DOCUMENTS:
            reject(404, "HELP_NOT_FOUND", "指定した文書が見つかりません。")
        title, filename, _ = HELP_DOCUMENTS[slug]
        try:
            content = public_file(ROOT / "docs" / filename, 256 * 1024)
            return {"slug": slug, "title": title, "markdown": content}
        except (OSError, ValueError):
            reject(
                503,
                "HELP_UNAVAILABLE",
                "文書を同梱したimageへ更新してください。",
            )

    @router.get("/network/config")
    def network_config(use_saved: bool = False):
        """Inspect proxy presence and exclusions without a connection."""
        values = store.read() if use_saved else store.applied
        return {
            "configuration": configuration_report(diagnostic_inputs(values)),
            "configuration_scope": "saved" if use_saved else "applied",
            "pending_restart": store.public()["pending_restart"],
        }

    @router.post("/network/checks")
    def network_checks(options: NetworkCheck):
        """Check selected fixed HTTPS targets with sanitized bounded output."""
        try:
            values = network_values(store, options.use_saved)
            result = network_runner(values, targets=options.targets, timeout=5)
            return {
                **result,
                "configuration_scope": "saved"
                if options.use_saved
                else "applied",
                "pending_restart": store.public()["pending_restart"],
            }
        except DiagnosticCaUnavailable:
            reject(
                422,
                "NETWORK_CA_UNAVAILABLE",
                "CAを専用private設定ディレクトリ配下へ置き、"
                "ファイルと権限を確認してください。",
            )
        except (OSError, ValueError):
            reject(
                422,
                "NETWORK_CONFIG_INVALID",
                "proxy・CAの設定を確認してください。",
            )

    return router
