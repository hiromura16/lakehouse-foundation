#!/usr/bin/env python3
"""Inspect private proxy settings and optionally probe fixed public targets."""

import argparse
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "proxy_diagnostic_adapter", ROOT / "infra/demo/proxy_checks.py"
)
DOCTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCTOR)


def read_values(path):
    """Read literal private settings without shell expansion or log output."""
    path = Path(path).expanduser()
    if (
        not path.is_absolute()
        or not path.is_file()
        or path.resolve().is_relative_to(ROOT)
        or any(part.is_symlink() for part in (path, *path.parents))
        or path.stat().st_mode & 0o077
        or path.stat().st_size > 128 * 1024
    ):
        raise ValueError("Use an external regular private env file (0600).")
    result = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or key in result
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
        ):
            raise ValueError("Invalid literal private settings.")
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        result[key] = value
    return result


def main():
    """Default to presence/routing inspection; outbound tests are explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    try:
        values = read_values(args.env_file)
        result = (
            DOCTOR.run_checks(values, args.check, args.timeout)
            if args.check
            else DOCTOR.configuration_report(values)
        )
    except (OSError, ValueError):
        raise SystemExit(
            "Proxy diagnosis failed; check private input/options."
        ) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
