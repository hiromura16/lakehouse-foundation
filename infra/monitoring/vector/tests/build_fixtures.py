#!/usr/bin/env python3
"""Generate native Vector assertions, never a Python severity implementation.

Run with --check to verify the committed native fixture file. Only the official
pinned Vector test command executes VRL and establishes transform outcomes.
Synthetic expected values are test specifications, not observed runtime data.
"""

import argparse
import hashlib
import json
from pathlib import Path

import yaml

DIRECTORY = Path(__file__).resolve().parent


def base(message, service="worker", stream="stdout"):
    """Provide only bounded invented metadata to isolated transform inputs."""
    return {
        "timestamp": "2026-01-01T00:00:00Z",
        "source_timestamp": "2026-01-01T00:00:00Z",
        "compose_project": "synthetic",
        "compose_service": service,
        "host_alias": "lab",
        "container_alias": service + "-1",
        "source_identity": "a" * 64,
        "source_position": hashlib.sha256(message.encode()).hexdigest(),
        "stream": stream,
        "message": message,
        "line_count": 1,
        "capture_truncated": 0,
    }


def vrl_literal(value):
    """Encode the fixture's JSON-shaped values using VRL 0.38 syntax.

    Vector 0.61 pins VRL 0.38. Its lexer requires ``\\u{HEX}``, rather than
    JSON's ``\\uXXXX`` or surrogate pairs. Literal braces are escaped too:
    fixture text must never become VRL template interpolation. This only
    serializes trusted synthetic values; it does not evaluate VRL or prove
    that the generated native test compiles with the official binary.

    Reference: vectordotdev/vrl v0.38.0 src/parser/lex.rs, escape_code and
    unicode_escape. Unsupported values and invalid Unicode fail generation.
    """
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int and -(2**63) <= value < 2**63:
        return str(value)
    if isinstance(value, str):
        escaped = []
        special = {'"': '\\"', "\\": "\\\\", "{": "\\{", "}": "\\}"}
        for character in value:
            codepoint = ord(character)
            if 0xD800 <= codepoint <= 0xDFFF:
                raise ValueError("Synthetic VRL text contains a surrogate.")
            if character in special:
                escaped.append(special[character])
            elif not 0x20 <= codepoint <= 0x7E:
                escaped.append("\\u{" + format(codepoint, "x") + "}")
            else:
                escaped.append(character)
        return '"' + "".join(escaped) + '"'
    if isinstance(value, list):
        return "[" + ", ".join(vrl_literal(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return (
            "{"
            + ", ".join(
                vrl_literal(key) + ": " + vrl_literal(item)
                for key, item in value.items()
            )
            + "}"
        )
    raise ValueError("Synthetic VRL literal type or range is unsupported.")


def assertions(expected):
    """Encode literal native VRL assertions without evaluating their result."""
    return "\n".join(
        "assert_eq!(." + key + ", " + vrl_literal(value) + ")"
        for key, value in expected.items()
    )


def build():
    """Produce native transform tests from the inspectable synthetic cases."""
    cases = json.loads((DIRECTORY / "cases.json").read_text())["cases"]
    tests = []
    for case in cases:
        if case["kind"] == "severity":
            stage = "severity"
            event = base(case["message"], case["service"], case["stream"])
            expected = case["expected"]
        else:
            stage = "redacted"
            event = {
                **base(case["message"]),
                "level_raw": "INFO",
                "level_normalized": "info",
                "level_source": "level",
                "level_parser": "json",
                "level_parse_status": "parsed",
                "logger": case.get("logger", "unit.logger"),
                "http_status": None,
                "http_method": None,
                "http_route": None,
                "log_kind": "application",
            }
            expected = {
                "message": case["expected_message"],
                "level": "info",
                "level_normalized": "info",
                "truncated": case.get("expected_truncated", 0),
                "logger": case.get("expected_logger", "unit.logger"),
            }
        input_record = {"insert_at": stage, "type": "log", "log_fields": event}
        if case.get("logger_pattern") == "aws_synthetic":
            # Assemble a synthetic known-provider pattern only inside the
            # test runtime; no credential-looking literal enters fixtures.
            input_record = {
                "insert_at": stage,
                "type": "vrl",
                "source": ". = " + vrl_literal(event) + "\n.logger = "
                '"AKIA" + "AAAA" + "AAAA" + "AAAA" + "AAAA"',
            }
        tests.append(
            {
                "name": case["name"],
                "inputs": [input_record],
                "outputs": [
                    {
                        "extract_from": stage,
                        "conditions": [
                            {"type": "vrl", "source": assertions(expected)}
                        ],
                    }
                ],
            }
        )
    envelope = {
        **base("\x1b[31mINFO: synthetic ERROR word\x1b[0m", "demo-api"),
        "capture_version": 1,
        "truncated": False,
    }
    tests.append(
        {
            "name": "envelope-ansi-before-severity",
            "inputs": [
                {
                    "insert_at": "envelope",
                    "type": "log",
                    "log_fields": {"message": json.dumps(envelope)},
                }
            ],
            "outputs": [
                {
                    "extract_from": "severity",
                    "conditions": [
                        {
                            "type": "vrl",
                            "source": assertions(
                                {
                                    "level_normalized": "info",
                                    "level_raw": "INFO",
                                    "message": "synthetic ERROR word",
                                }
                            ),
                        }
                    ],
                }
            ],
        }
    )
    for name, service, parent, final in (
        (
            "python-parent-inheritance",
            "worker",
            "ERROR: synthetic parent",
            "ValueError: synthetic final ERROR word",
        ),
        (
            "java-parent-inheritance",
            "cdc-connect",
            "2026-01-01 00:00:00.000 ERROR synthetic parent",
            "java.lang.IllegalStateException: synthetic final",
        ),
    ):
        tests.append(
            {
                "name": name,
                "inputs": [
                    {
                        "insert_at": "severity",
                        "type": "log",
                        "log_fields": base(text, service, "stderr"),
                    }
                    for text in (parent, "    synthetic frame", final)
                ],
                "outputs": [
                    {
                        "extract_from": "redacted",
                        "conditions": [
                            {
                                "type": "vrl",
                                "source": assertions(
                                    {
                                        "level": "error",
                                        "level_normalized": "error",
                                        "line_count": 3,
                                        "stream": "stderr",
                                        "compose_service": service,
                                    }
                                ),
                            }
                        ],
                    }
                ],
            }
        )
    return (
        "# Generated by build_fixtures.py; execute with official Vector.\n"
        + (yaml.safe_dump({"tests": tests}, sort_keys=False, width=78))
    )


def main():
    """Write or check generated fixtures without reading private settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    target = DIRECTORY / "native-tests.yaml"
    rendered = build()
    if options.check:
        return 0 if target.read_text() == rendered else 1
    target.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
