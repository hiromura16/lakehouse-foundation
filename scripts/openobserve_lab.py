#!/usr/bin/env python3
"""Plan or explicitly probe the fixed local OpenObserve comparison lab."""

import argparse
import base64
import http.client
import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATH = "/api/default/ingestion-tokens"
TOKEN_NAME = "lakehouse-vector-lab"
SEARCH_PATH = "/api/default/_search"
MAX_RESPONSE = 1024 * 1024
MAX_TOKEN_FILE = 4096
REQUEST_SECONDS = 15
SOCKET_SECONDS = 3
TOKEN_PATTERN = re.compile(r"o2oi_[A-Za-z0-9]{32}")


class LabError(ValueError):
    """Carry only an operator-safe message, never server or credential text."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent root Basic credentials from following a redirect."""

    def redirect_request(self, request, stream, code, message, headers, url):
        """Refuse every redirected destination before forwarding headers."""
        raise LabError("Lab redirects are refused.")


def plan(values):
    """Describe an opt-in lab without connecting or reading credentials."""
    profiles = values.get("COMPOSE_PROFILES", "").split(",")
    if "openobserve-lab" not in profiles:
        return {"enabled": False, "network_requests": 0}
    return {
        "enabled": True,
        "organization": "default",
        "stream": "lakehouse_logs",
        "global_retention_days": 7,
        "metrics_duplication": False,
        "live_acceptance": "not_performed",
    }


def _port(values):
    """Allow only a bounded loopback port, never an arbitrary URL or host."""
    value = values.get("OPENOBSERVE_PORT", "5080")
    if not value.isdecimal() or not 1024 <= int(value) <= 65535:
        raise LabError("Set a valid OPENOBSERVE_PORT.")
    return int(value)


def _credentials(values):
    """Validate the bootstrap identity without opening an ingestion file."""
    email = values.get("OPENOBSERVE_ROOT_USER_EMAIL", "")
    password = values.get("OPENOBSERVE_ROOT_USER_PASSWORD", "")
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+", email):
        raise LabError("Configure a valid lab bootstrap email.")
    if not 32 <= len(password) <= 128 or any(c.isspace() for c in password):
        raise LabError("Configure a strong private lab bootstrap password.")
    return email, password


def _identity(metadata):
    """Pin identity and content metadata while excluding read-only atime."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _private_parent(filename):
    """Open a pinned parent outside checkout without following any link."""
    path = Path(filename)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.is_relative_to(ROOT)
        or len(path.parts) < 2
    ):
        raise LabError("Use an absolute private path outside the repository.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return path, parent
    except Exception:
        os.close(parent)
        raise


def _unique_fields(pairs):
    """Reject duplicate JSON keys rather than silently replacing a token."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise LabError("Check the private ingestion-token file.")
        result[key] = value
    return result


def _read_token(filename, expected_uid=None):
    """Read one bounded private file through no-follow directory handles."""
    failure = "Check the private ingestion-token file."
    parent = descriptor = None
    try:
        if expected_uid is None:
            expected_uid = os.getuid()
        if type(expected_uid) is not int or expected_uid <= 0:
            raise LabError(failure)
        path, parent = _private_parent(filename)
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != expected_uid
            or not 1 <= before.st_size <= MAX_TOKEN_FILE
        ):
            raise LabError(failure)
        content = bytearray()
        while len(content) <= MAX_TOKEN_FILE:
            block = os.read(descriptor, MAX_TOKEN_FILE + 1 - len(content))
            if not block:
                break
            content.extend(block)
        after = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (
            len(content) > MAX_TOKEN_FILE
            or _identity(before) != _identity(after)
            or _identity(after) != _identity(named)
        ):
            raise LabError(failure)
        document = json.loads(
            content.decode("utf-8"), object_pairs_hook=_unique_fields
        )
        token = document.get("token") if isinstance(document, dict) else None
        if (
            not isinstance(document, dict)
            or set(document) != {"token"}
            or not isinstance(token, str)
            or not TOKEN_PATTERN.fullmatch(token)
        ):
            raise LabError(failure)
        return token
    except (OSError, UnicodeError, ValueError, TypeError):
        raise LabError(failure) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def _validate_token(values, password, *, required):
    """Require a distinct ingestion token without returning it to callers."""
    filename = values.get("OPENOBSERVE_INGEST_TOKEN_FILE", "")
    if not filename:
        if required:
            raise LabError("Configure a private ingestion-token file.")
        return
    expected_uid = values.get("WORKER_UID")
    if expected_uid is not None:
        if (
            not isinstance(expected_uid, str)
            or not expected_uid.isdecimal()
            or not 1 <= int(expected_uid)
        ):
            raise LabError("Configure a nonroot WORKER_UID for the lab.")
        expected_uid = int(expected_uid)
    if _read_token(filename, expected_uid=expected_uid) == password:
        raise LabError("Use a distinct organization ingestion-only token.")


def validate_config(values, require_token=True):
    """Validate lab-on secrets offline; lab-off never opens private files."""
    profiles = values.get("COMPOSE_PROFILES", "").split(",")
    if "openobserve-lab" not in profiles:
        return
    _, password = _credentials(values)
    if require_token:
        _validate_token(values, password, required=True)


def _read_body(response, deadline):
    """Bound body reads and bytes with one shared monotonic budget.

    Initial header parsing happens inside urllib.open and has an idle socket
    timeout, not a strict wall-clock deadline. Do not report this body guard
    as a hard total-request guarantee against a slow-header server.
    """
    payload = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LabError("Lab response deadline exceeded.")
        # urllib's HTTPResponse exposes the buffered socket through fp.raw.
        # Reduce its per-read timeout to the remaining shared deadline.
        raw = getattr(getattr(response, "fp", None), "raw", None)
        connection = getattr(raw, "_sock", None)
        if connection is not None:
            connection.settimeout(min(SOCKET_SECONDS, remaining))
        chunk = response.read1(min(8192, MAX_RESPONSE + 1 - len(payload)))
        if time.monotonic() >= deadline:
            raise LabError("Lab response deadline exceeded.")
        if not isinstance(chunk, bytes):
            raise LabError("Lab returned an invalid response body.")
        if not chunk:
            return payload
        payload.extend(chunk)
        if len(payload) > MAX_RESPONSE:
            raise LabError("Lab response exceeds the local safety limit.")


def _request(values, path, *, body=None, authenticated=False, opener=None):
    """Call one fixed loopback route with bounded output and no proxy."""
    headers = {"Accept": "application/json"}
    if authenticated:
        email, password = _credentials(values)
        encoded = base64.b64encode((email + ":" + password).encode()).decode()
        headers["Authorization"] = "Basic " + encoded
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://127.0.0.1:{_port(values)}{path}",
        headers=headers,
        data=data,
    )
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
    deadline = time.monotonic() + REQUEST_SECONDS
    try:
        with opener.open(request, timeout=SOCKET_SECONDS) as response:
            if response.status != 200:
                raise LabError("Lab returned an unexpected HTTP status.")
            payload = _read_body(response, deadline)
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise LabError("Lab returned an invalid JSON response.")
        return result
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise LabError(
                "Lab authentication or authorization failed."
            ) from None
        raise LabError(
            "Lab HTTP request failed; inspect private logs."
        ) from None
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        http.client.HTTPException,
    ):
        raise LabError("Lab response is unavailable or invalid.") from None


def health(values, *, allow_lab=False, opener=None):
    """Check process reachability only after explicit operator opt-in."""
    if not allow_lab:
        raise LabError("An explicit --allow-lab is required for network I/O.")
    result = _request(values, "/healthz", opener=opener)
    if result.get("status") != "ok":
        raise LabError("Lab health endpoint is not ready.")
    return {"ok": True, "reachability_only": True}


def _reserve_output(output):
    """Exclusively reserve a private file outside source with no symlinks."""
    parent = None
    try:
        output, parent = _private_parent(output)
        metadata = os.fstat(parent)
        if (
            metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise LabError(
                "Use an owned, pre-existing 0700 private directory."
            )
        descriptor = os.open(
            output.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
    except (OSError, ValueError, TypeError):
        if parent is not None:
            os.close(parent)
        raise LabError(
            "Private token output cannot be created safely."
        ) from None
    return output, descriptor, parent


def create_token(
    values, output, *, allow_lab=False, allow_write=False, opener=None
):
    """Create a named ingestion token once and save only private JSON."""
    if not allow_lab or not allow_write:
        raise LabError(
            "Token creation needs --allow-lab --allow-token-create."
        )
    _credentials(values)
    output, descriptor, parent = _reserve_output(output)
    reserved = os.fstat(descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        result = _request(
            values,
            TOKEN_PATH,
            body={
                "name": TOKEN_NAME,
                "description": "Lakehouse lab Vector ingestion only",
                "splunk_token": False,
            },
            authenticated=True,
            opener=opener,
        )
        data = result.get("data")
        if not isinstance(data, dict):
            raise LabError("Lab returned invalid token metadata.")
        token = data.get("token", "")
        if data.get("name") != TOKEN_NAME or data.get("enabled") is not True:
            raise LabError("Lab did not confirm the named enabled token.")
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise LabError(
                "Lab did not return an organization ingestion token."
            )
        if token == values["OPENOBSERVE_ROOT_USER_PASSWORD"]:
            raise LabError("Lab token must differ from bootstrap credentials.")
        with os.fdopen(descriptor, "w") as stream:
            descriptor = None
            json.dump({"token": token}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        named = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (reserved.st_dev, reserved.st_ino):
            raise LabError("Private token output changed during creation.")
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        # Unlink only this invocation's inode in the same pinned directory.
        try:
            named = os.stat(output.name, dir_fd=parent, follow_symlinks=False)
            if (named.st_dev, named.st_ino) == (
                reserved.st_dev,
                reserved.st_ino,
            ):
                os.unlink(output.name, dir_fd=parent)
        except OSError:
            pass
        raise
    finally:
        os.close(parent)
    return {"ok": True, "saved_private_token": True, "site_env_updated": False}


def counts(values, *, allow_lab=False, window_seconds=300, opener=None):
    """Query fixed aggregate counts without returning log contents."""
    if not allow_lab:
        raise LabError("An explicit --allow-lab is required for network I/O.")
    if type(window_seconds) is not int or not 1 <= window_seconds <= 3600:
        raise LabError("Use a count window from 1 to 3600 seconds.")
    _, password = _credentials(values)
    _validate_token(values, password, required=False)
    end = time.time_ns() // 1000
    query = {
        "query": {
            "sql": (
                "SELECT COUNT(*) AS total_records, "
                "COUNT(DISTINCT source_event_id) AS distinct_events "
                'FROM "lakehouse_logs"'
            ),
            "start_time": end - window_seconds * 1000000,
            "end_time": end,
            "from": 0,
            "size": 1,
        }
    }
    started = time.monotonic()
    result = _request(
        values, SEARCH_PATH, body=query, authenticated=True, opener=opener
    )
    hits = result.get("hits")
    if (
        not isinstance(hits, list)
        or len(hits) != 1
        or not isinstance(hits[0], dict)
    ):
        raise LabError("Count query returned no validated aggregate result.")
    total = hits[0].get("total_records")
    distinct = hits[0].get("distinct_events")
    if (
        type(total) is not int
        or type(distinct) is not int
        or not 0 <= distinct <= total
    ):
        raise LabError("Count query returned invalid aggregate values.")
    return {
        "ok": True,
        "total_records": total,
        "distinct_source_events": distinct,
        "duplicate_excess": total - distinct,
        "window_seconds": window_seconds,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        "acceptance_benchmark": False,
    }


def main():
    """Keep plan offline; every active operation needs an explicit flag."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["plan", "health", "token-create", "counts"]
    )
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--allow-lab", action="store_true")
    parser.add_argument("--allow-token-create", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--window-seconds", type=int, default=300)
    args = parser.parse_args()
    from settings import load

    try:
        values = load(args.env_file)
        if args.action == "plan":
            result = plan(values)
        elif args.action == "health":
            result = health(values, allow_lab=args.allow_lab)
        elif args.action == "counts":
            result = counts(
                values,
                allow_lab=args.allow_lab,
                window_seconds=args.window_seconds,
            )
        else:
            if args.output is None:
                parser.error("token-create requires a private --output file.")
            result = create_token(
                values,
                args.output,
                allow_lab=args.allow_lab,
                allow_write=args.allow_token_create,
            )
    except (LabError, OSError, ValueError):
        parser.exit(
            1, "Lab operation failed; inspect private configuration.\n"
        )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
