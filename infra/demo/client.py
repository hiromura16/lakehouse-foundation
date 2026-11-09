"""Call the internal control API from the Streamlit server only."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


class NoRedirects(HTTPRedirectHandler):
    """Keep API authentication on the configured internal origin."""

    def redirect_request(self, request, response, code, message, headers, url):
        """Reject redirects without repeating a credential-bearing request."""
        return None


def request(path, *, method="GET", payload=None, timeout=15):
    """Send a bounded JSON request and return only its parsed object."""
    base = os.environ.get("DEMO_API_URL", "http://demo-api:8000").rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
        or not parsed.hostname
        or not path.startswith("/")
        or path.startswith("//")
        or method not in {"GET", "POST", "PATCH"}
    ):
        raise ValueError("Invalid internal API request configuration.")
    token = os.environ.get("CONTROL_API_TOKEN", "")
    if not token:
        raise ValueError("The private API token has not been configured.")
    headers = {
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    call = Request(base + path, data=body, headers=headers, method=method)
    # Internal calls must not be routed through an outbound company proxy.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(call, timeout=timeout) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("The API response exceeds the display limit.")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("The API returned an unexpected response.")
        return result
    except HTTPError as error:
        raise ValueError(
            f"API request failed (HTTP {error.code}). "
            "Check the private API configuration and job parameters."
        ) from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise ValueError("The internal API is not available yet.") from None
