#!/usr/bin/env python3
"""Seed one editable Grafana dashboard without replacing GUI-owned state."""

import argparse
import base64
import copy
import ipaddress
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from settings import ROOT, load

DASHBOARD_UID = "lakehouse-operations"
DASHBOARDS_API = "/apis/dashboard.grafana.app/v1/namespaces/default/dashboards"
TEMPLATE = ROOT / "infra/monitoring/dashboards/foundation.json"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class GrafanaError(RuntimeError):
    """Describe a failed operation without exposing HTTP bodies or secrets."""


class GrafanaUnavailable(GrafanaError):
    """Indicate a transport failure that readiness checks may retry."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep administrator credentials on the explicitly selected endpoint."""

    def redirect_request(self, *_args, **_kwargs):
        """Reject redirects instead of forwarding the authorization header."""
        return None


class GrafanaAPI:
    """Call the selected private Grafana with Basic authentication."""

    def __init__(self, values):
        """Use validated private settings without embedding them into URLs."""
        try:
            address = ipaddress.ip_address(values.get("BIND_IP", "127.0.0.1"))
            port = int(values.get("GRAFANA_PORT", "3000"))
        except ValueError:
            raise GrafanaError("Invalid Grafana endpoint settings.") from None
        if (
            address.version != 4
            or address.is_global
            or address.is_unspecified
            or not 1 <= port <= 65535
        ):
            raise GrafanaError(
                "Use a specific local/private Grafana endpoint."
            )
        password = values.get("GRAFANA_ADMIN_PASSWORD")
        if not password:
            raise GrafanaError(
                "Set the private Grafana administrator password."
            )
        self.base_url = "http://" + str(address) + ":" + str(port)
        token = base64.b64encode(("admin:" + password).encode()).decode()
        self.authorization = "Basic " + token
        # This endpoint is private/local; never send its credentials via proxy.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(self, method, path, payload=None, timeout=10):
        """Return bounded JSON without exposing HTTP error bodies."""
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={
                "Authorization": self.authorization,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                status = response.status
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            # Codes are sufficient to distinguish absent/conflict/auth cases.
            return error.code, None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GrafanaUnavailable(
                "Grafana connection is not ready."
            ) from None
        if len(content) > MAX_RESPONSE_BYTES:
            raise GrafanaError("Grafana response exceeded the size limit.")
        try:
            result = json.loads(content)
        except (UnicodeError, ValueError):
            raise GrafanaError(
                "Grafana returned an invalid JSON response."
            ) from None
        return status, result


def wait_ready(api, timeout=60):
    """Wait for Grafana's database within the readiness deadline."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GrafanaError("Grafana readiness deadline exceeded.")
        try:
            status, payload = api.request(
                "GET", "/api/health", timeout=min(5, remaining)
            )
        except GrafanaUnavailable:
            status, payload = 0, None
        if (
            status == 200
            and isinstance(payload, dict)
            and payload.get("database") == "ok"
        ):
            return
        if status in {401, 403}:
            raise GrafanaError("Grafana rejected credentials or permissions.")
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def create_payload(template=TEMPLATE):
    """Wrap a classic editable model in the new dashboard API resource."""
    model = json.loads(template.read_text(encoding="utf-8"))
    if not isinstance(model, dict) or model.get("uid") != DASHBOARD_UID:
        raise GrafanaError("The dashboard template UID is invalid.")
    if not isinstance(model.get("panels"), list) or not model.get("title"):
        raise GrafanaError("The dashboard template model is invalid.")
    model = copy.deepcopy(model)
    for field in ("id", "uid", "version"):
        model.pop(field, None)
    model["editable"] = True
    return {
        "apiVersion": "dashboard.grafana.app/v1",
        "kind": "Dashboard",
        "metadata": {"name": DASHBOARD_UID},
        "spec": model,
    }


def setup(values):
    """Create only a missing UID; existing GUI dashboards are never updated."""
    api = GrafanaAPI(values)
    wait_ready(api)
    item_path = DASHBOARDS_API + "/" + DASHBOARD_UID
    status, _ = api.request("GET", item_path)
    if status == 200:
        return {"status": "exists", "uid": DASHBOARD_UID}
    if status != 404:
        raise GrafanaError("Grafana dashboard lookup failed; no write made.")
    status, _ = api.request("POST", DASHBOARDS_API, create_payload())
    if status in {200, 201}:
        return {"status": "created", "uid": DASHBOARD_UID}
    if status == 409:
        # Another initializer may have won the create race. Never use PUT.
        status, _ = api.request("GET", item_path)
        if status == 200:
            return {"status": "exists", "uid": DASHBOARD_UID}
    raise GrafanaError(
        "Grafana dashboard creation failed; no update attempted."
    )


def export_dashboard(values, output):
    """Export GUI state to one new private file outside the public checkout."""
    output = Path(output).expanduser()
    if not output.is_absolute() or output.resolve().is_relative_to(ROOT):
        raise GrafanaError(
            "Choose an absolute export path outside the source."
        )
    for part in (output, *output.parents):
        if part.is_symlink():
            raise GrafanaError("Export paths must not use symlinks.")
    if output.exists():
        raise GrafanaError(
            "The export file already exists; it was not changed."
        )
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.parent.stat().st_mode & 0o077:
        raise GrafanaError("The export directory must have mode 0700.")
    api = GrafanaAPI(values)
    wait_ready(api)
    status, resource = api.request("GET", DASHBOARDS_API + "/" + DASHBOARD_UID)
    if status != 200 or not isinstance(resource, dict):
        raise GrafanaError("The dashboard could not be read; no export made.")
    if not isinstance(resource.get("spec"), dict):
        raise GrafanaError("Grafana did not return a classic dashboard model.")
    model = copy.deepcopy(resource["spec"])
    model["uid"] = DASHBOARD_UID
    content = json.dumps(model, ensure_ascii=False, indent=2) + "\n"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(content)
    return {"status": "exported", "uid": DASHBOARD_UID}


def main():
    """Initialize or privately export using explicit or shell-selected env."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "対象は標準dashboardのUIDだけです。GUI編集を上書きする機能や、"
            "Grafana全体のbackup・利用者/権限のexportではありません。"
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=os.environ.get("LAKEHOUSE_ENV_FILE") or None,
        help=(
            "Grafana管理者のprivate site.env。省略時はLAKEHOUSE_ENV_FILEを使う"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="export時に必須のGit外JSON絶対パス。既存ファイルは上書きしない",
    )
    parser.add_argument(
        "action",
        choices=["init", "export"],
        help="init=標準dashboardがなければ一度だけ作成、export=現在の標準UIDを保存",
    )
    args = parser.parse_args()
    if args.env_file is None:
        parser.error("Provide --env-file or set LAKEHOUSE_ENV_FILE.")
    if args.action == "export" and args.output is None:
        parser.error("Export needs an external private --output path.")
    try:
        values = load(args.env_file)
        result = (
            setup(values)
            if args.action == "init"
            else export_dashboard(values, args.output)
        )
    except GrafanaError as error:
        raise SystemExit(str(error)) from None
    except (OSError, UnicodeError, ValueError):
        raise SystemExit(
            "Grafana operation failed; inspect private settings and health."
        ) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
