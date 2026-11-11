"""Check Vector source/config contracts without pretending to execute VRL.

Native test assertions are delivered for the official pinned binary. YAML and
fixture consistency here establishes source contracts only; live formatting,
fan-out, saturation, checkpoint/restart and UI evidence remain separate gates.
"""

import fnmatch
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "infra/monitoring/vector"
SPEC = importlib.util.spec_from_file_location(
    "vector_fixture_builder", DIRECTORY / "tests/build_fixtures.py"
)
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


def configuration(name):
    """Read only public checked-in YAML, never a private settings file."""
    return yaml.safe_load((DIRECTORY / name).read_text())


class VrlLiteralTests(unittest.TestCase):
    """Check selected lexer syntax without claiming VRL execution."""

    def test_unicode_scalars_use_vrl_not_json_or_surrogate_escapes(self):
        self.assertEqual(
            BUILDER.vrl_literal("日本💥\U0010ffff"),
            '"\\u{65e5}\\u{672c}\\u{1f4a5}\\u{10ffff}"',
        )
        for codepoint in (
            0,
            0x1F,
            0x7F,
            0x9F,
            0xD7FF,
            0xE000,
            0xFFFF,
            0x10000,
            0x10FFFF,
        ):
            with self.subTest(codepoint=codepoint):
                literal = BUILDER.vrl_literal(chr(codepoint))
                self.assertEqual(literal, '"\\u{' + f"{codepoint:x}" + '}"')
                self.assertTrue(literal.isascii())

    def test_controls_quotes_backslashes_and_braces_are_literal_text(self):
        self.assertEqual(
            BUILDER.vrl_literal("\x00\b\f\n\r\t\x1b\x7f\u2028\u2029"),
            '"\\u{0}\\u{8}\\u{c}\\u{a}\\u{d}\\u{9}\\u{1b}\\u{7f}'
            '\\u{2028}\\u{2029}"',
        )
        # Escape both template delimiters rather than evaluate fixture text.
        self.assertEqual(
            BUILDER.vrl_literal('"\\{{ synthetic }}'),
            '"\\"\\\\\\{\\{ synthetic \\}\\}"',
        )
        for codepoint in (*range(0x20), *range(0x7F, 0xA0)):
            with self.subTest(control=codepoint):
                self.assertEqual(
                    BUILDER.vrl_literal(chr(codepoint)),
                    '"\\u{' + f"{codepoint:x}" + '}"',
                )

    def test_generated_objects_and_assertions_use_the_same_safe_literals(self):
        self.assertEqual(
            BUILDER.vrl_literal(
                {"text": "💥", "state": [None, False, True, 1, -2]}
            ),
            '{"text": "\\u{1f4a5}", "state": [null, false, true, 1, -2]}',
        )
        self.assertEqual(
            BUILDER.assertions({"message": "\b💥{{ text }}"}),
            'assert_eq!(.message, "\\u{8}\\u{1f4a5}\\{\\{ text \\}\\}")',
        )
        native = yaml.safe_load(BUILDER.build())["tests"]
        for case in native:
            sources = [record.get("source", "") for record in case["inputs"]]
            sources.extend(
                condition.get("source", "")
                for output in case["outputs"]
                for condition in output["conditions"]
            )
            for source in sources:
                with self.subTest(case=case["name"]):
                    self.assertTrue(source.isascii())
                    self.assertNotRegex(source, r"(?<!\\)\\u[0-9a-fA-F]{4}")

    def test_surrogates_and_unsupported_literal_types_fail_generation(self):
        for value in (
            "\ud800",
            "\udfff",
            "\ud83d\udca5",
            {"\ud800": "synthetic"},
            2**63,
            -(2**63) - 1,
            1.5,
            float("nan"),
            ("tuple",),
            {1: "non-string key"},
        ):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError):
                    BUILDER.vrl_literal(value)


class VectorConfigTests(unittest.TestCase):
    """Review static safety/topology invariants independently of runtime."""

    def test_native_fixtures_are_current_and_expectations_are_not_results(
        self,
    ):
        self.assertEqual(
            (DIRECTORY / "tests/native-tests.yaml").read_text(),
            BUILDER.build(),
        )
        document = json.loads((DIRECTORY / "tests/cases.json").read_text())
        self.assertIn("pending", document["provenance"])
        cases = document["cases"]
        names = {row["name"] for row in cases}
        self.assertEqual(len(names), len(cases))
        for name in (
            "uvicorn-info-error-logger-stderr",
            "grafana-warn-status-200",
            "grafana-info-error-word",
            "prometheus-info-failed-word",
            "trino-info-error-word",
            "plain-stderr-unknown",
            "malformed-json",
            "python-final-exception",
            "java-final-exception",
            "redaction-multiline-auth",
            "redaction-camel-secretAccessKey",
            "flat-log-level",
        ):
            self.assertIn(name, names)
        known = [
            row
            for row in cases
            if row["kind"] == "severity"
            and row["expected"]["level_parse_status"] == "parsed"
        ]
        self.assertGreaterEqual(len(known), 40)
        # These are expected denominators, not fabricated observed success %.
        self.assertFalse(any("observed" in row for row in cases))

    def test_file_source_is_bounded_curated_and_checkpointed(self):
        config = configuration("common.yaml")
        self.assertEqual(config["data_dir"], "/var/lib/vector")
        self.assertFalse(config["api"]["enabled"])
        self.assertFalse(config["proxy"]["enabled"])
        self.assertFalse(config["acknowledgements"]["enabled"])
        source = config["sources"]["curated"]
        self.assertEqual(source["type"], "file")
        self.assertEqual(
            source["include"], ["/var/lib/lakehouse-log-spool/*.jsonl"]
        )
        self.assertFalse(source["ignore_checkpoints"])
        self.assertEqual(source["max_line_bytes"], 131072)
        self.assertEqual(source["fingerprint"]["strategy"], "device_and_inode")
        text = (DIRECTORY / "common.yaml").read_text()
        self.assertNotIn("docker_logs", text)
        self.assertNotIn("docker.sock", text)

    def test_multiline_preserves_first_parent_metadata_and_bounds(self):
        config = configuration("common.yaml")
        reduce = config["transforms"]["multiline"]
        self.assertEqual(
            reduce["group_by"], ["source_identity", "stream", "process_key"]
        )
        self.assertEqual(reduce["max_events"], 32)
        self.assertEqual(reduce["expire_after_ms"], 1000)
        merges = reduce["merge_strategies"]
        for field in (
            "source_identity",
            "source_position",
            "compose_project",
            "compose_service",
            "host_alias",
            "container_alias",
            "stream",
            "timestamp",
            "level_raw",
            "level_normalized",
            "level_source",
            "level_parser",
            "level_parse_status",
            "logger",
            "http_status",
        ):
            self.assertEqual(merges[field], "discard")
        self.assertEqual(merges["message"], "concat_newline")
        self.assertEqual(merges["capture_truncated"], "max")
        self.assertEqual(merges["line_count"], "sum")

    def test_single_redaction_before_both_bounded_nonblocking_sink_branches(
        self,
    ):
        loki = configuration("loki.yaml")["sinks"]["loki"]
        laboratory = configuration("openobserve.yaml")
        sink = laboratory["sinks"]["openobserve"]
        self.assertEqual(loki["inputs"], ["redacted"])
        transform = laboratory["transforms"]["openobserve_timestamp"]
        self.assertEqual(transform["inputs"], ["redacted"])
        self.assertEqual(sink["inputs"], ["openobserve_timestamp"])
        for branch in (loki, sink):
            self.assertFalse(branch["acknowledgements"]["enabled"])
            self.assertEqual(branch["buffer"]["type"], "disk")
            self.assertEqual(branch["buffer"]["max_size"], 536870912)
            self.assertEqual(branch["buffer"]["when_full"], "drop_newest")
            self.assertEqual(branch["request"]["retry_attempts"], 10)
        self.assertEqual(
            set(loki["labels"]),
            {
                "job",
                "host",
                "compose_project",
                "compose_service",
                "container_name",
                "stream",
                "level",
            },
        )
        self.assertNotIn("source_event_id", json.dumps(loki["labels"]))
        self.assertEqual(sink["auth"]["strategy"], "basic")
        self.assertEqual(sink["auth"]["password"], "SECRET[openobserve.token]")
        self.assertEqual(sink["payload_prefix"], "[")
        self.assertEqual(sink["payload_suffix"], "]")
        self.assertFalse(sink["healthcheck"]["enabled"])
        self.assertEqual(
            "\n".join(
                line
                for line in (DIRECTORY / "transforms/openobserve.vrl")
                .read_text()
                .splitlines()
                if not line.startswith("#")
            ).strip(),
            "._timestamp = .timestamp",
        )

    def test_profile_selector_never_uses_general_environment_interpolation(
        self,
    ):
        text = (DIRECTORY / "entrypoint.sh").read_text()
        self.assertIn('case ",${COMPOSE_PROFILES:-}," in', text)
        self.assertIn("*,openobserve-lab,*)", text)
        normal = text.split("  *)", 1)[1]
        self.assertNotIn("openobserve.yaml", normal)
        self.assertNotIn("SECRET", (DIRECTORY / "common.yaml").read_text())
        for file in DIRECTORY.glob("*.yaml"):
            self.assertNotIn("dangerously_allow", file.read_text())
        subprocess.run(
            ["sh", "-n", str(DIRECTORY / "entrypoint.sh")], check=True
        )

    def test_optional_host_intent_is_one_bounded_namespaced_static_gauge(
        self,
    ):
        """Check public topology, not emitted bytes or live exporter health."""
        host = configuration("host-expected.yaml")
        self.assertEqual(set(host), {"sources"})
        self.assertEqual(set(host["sources"]), {"internal_host_expected"})
        source = host["sources"]["internal_host_expected"]
        self.assertEqual(
            source,
            {
                "type": "static_metrics",
                "namespace": "lakehouse",
                "interval_secs": 15,
                "metrics": [
                    {
                        "name": "host_collection_expected",
                        "kind": "absolute",
                        "tags": {"host_alias": "local"},
                        "value": {"gauge": {"value": 1.0}},
                    }
                ],
            },
        )
        common = configuration("common.yaml")
        inputs = common["sinks"]["metrics"]["inputs"]
        normal = {"internal", "parser_metrics"}
        for components in (normal, normal | {"internal_host_expected"}):
            matches = set()
            for pattern in inputs:
                matched = {
                    name
                    for name in components
                    if fnmatch.fnmatchcase(name, pattern)
                }
                self.assertTrue(matched)
                matches.update(matched)
            self.assertEqual(matches, components)
        self.assertNotIn("internal_host_expected", common["sources"])

    def test_redaction_and_parser_source_contracts_are_bounded(self):
        severity = (DIRECTORY / "transforms/severity.vrl").read_text()
        redaction = (DIRECTORY / "transforms/redact.vrl").read_text()
        envelope = (DIRECTORY / "transforms/envelope.vrl").read_text()
        self.assertIn('.process_key = "default"', severity)
        self.assertIn('parsed."log.level"', severity)
        for alias in (
            "secret[_-]?access[_-]?key",
            "access[_-]?key[_-]?id",
            "session[_-]?token",
            "cookie",
            "password",
            "bearer",
            "basic",
        ):
            self.assertIn(alias, redaction)
        self.assertIn("limit: 4096", redaction)
        self.assertIn('"source_event_id": event_id', redaction)
        self.assertIn('"level": .level_normalized', redaction)
        self.assertIn('"sail-server", "sail-benchmark"', envelope)
        self.assertNotRegex(envelope, r'"(?:sail|sail-check)"')
        self.assertNotIn('"source_identity":', redaction)
        matrix = configuration("log_parsers.yaml")
        self.assertEqual(matrix["vector_version"], "0.61.0")
        self.assertEqual(
            set(matrix["level_contract"]),
            {"trace", "debug", "info", "warn", "error", "fatal", "unknown"},
        )
        for parser in matrix["parsers"]:
            self.assertTrue(re.match(r"https://", parser["reference"]))
            self.assertIn("sample", parser)
            self.assertIn("fallback", parser)

    def test_logger_assignments_are_redacted_in_native_fixture_expectations(
        self,
    ):
        """Inspect metadata leak regressions; this does not execute VRL."""
        redaction = (DIRECTORY / "transforms/redact.vrl").read_text()
        self.assertIn("logger_sensitive = match(logger,", redaction)
        self.assertIn("if logger_sensitive ||", redaction)
        cases = json.loads((DIRECTORY / "tests/cases.json").read_text())
        selected = [
            case
            for case in cases["cases"]
            if case["name"].startswith("logger-assignment-")
        ]
        self.assertEqual(len(selected), 8)
        for case in selected:
            self.assertEqual(case["expected_logger"], "redacted")
            self.assertEqual(case["expected_message"], "synthetic message")
        safe = [
            case
            for case in cases["cases"]
            if case["name"].startswith("logger-safe-")
        ]
        self.assertEqual(len(safe), 2)
        for case in safe:
            self.assertEqual(case["logger"], case["expected_logger"])


@unittest.skipUnless(os.geteuid() != 0, "Requires an unprivileged test UID.")
class EntryPointTests(unittest.TestCase):
    """Use disposable paths and a stub; never run Vector or host apply."""

    def setUp(self):
        fixture = tempfile.TemporaryDirectory(prefix="vector-entry-unit-")
        self.addCleanup(fixture.cleanup)
        self.directory = Path(fixture.name)
        self.state = self.directory / "state"
        self.state.mkdir(mode=0o700)
        self.secret = self.directory / "secret.json"
        self.binary = self.directory / "stub-vector"
        self.binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        self.binary.chmod(0o700)
        script = (DIRECTORY / "entrypoint.sh").read_text()
        script = script.replace("/var/lib/vector", str(self.state))
        script = script.replace(
            "/run/secrets/openobserve-vector.json", str(self.secret)
        )
        script = script.replace("/usr/bin/vector", str(self.binary))
        self.script = self.directory / "entrypoint.sh"
        self.script.write_text(script)

    def run_selected(self, profiles):
        """Keep profile selection observable without running a daemon."""
        return subprocess.run(
            ["sh", str(self.script)],
            env={"PATH": os.defpath, "COMPOSE_PROFILES": profiles},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def test_normal_off_needs_no_secret_and_exact_profile_selects_lab(self):
        for profiles in (
            "monitoring",
            "openobserve-laboratory,monitoring",
            "",
        ):
            result = self.run_selected(profiles)
            self.assertEqual(result.returncode, 0)
            self.assertIn("common.yaml", result.stdout)
            self.assertNotIn("openobserve.yaml", result.stdout)
        missing = self.run_selected("monitoring,openobserve-lab")
        self.assertEqual(missing.returncode, 78)
        self.assertNotIn(str(self.secret), missing.stderr)
        self.secret.write_text('{"token":"o2oi_' + "s" * 32 + '"}')
        self.secret.chmod(0o600)
        included = self.run_selected("monitoring,openobserve-lab")
        self.assertEqual(included.returncode, 0)
        self.assertIn("openobserve.yaml", included.stdout)
        self.assertNotIn("synthetic", included.stdout)

    def test_existing_bad_mode_or_symlink_is_never_repaired(self):
        self.state.chmod(0o750)
        result = self.run_selected("monitoring")
        self.assertEqual(result.returncode, 78)
        self.assertEqual(self.state.stat().st_mode & 0o7777, 0o750)
        self.state.rmdir()
        self.state.symlink_to(self.directory, target_is_directory=True)
        result = self.run_selected("monitoring")
        self.assertEqual(result.returncode, 78)
        self.assertTrue(self.state.is_symlink())

    def test_host_intent_config_is_selected_only_by_the_exact_host_profile(
        self,
    ):
        for profiles in ("", "monitoring", "monitoring,monitoring-hosting"):
            result = self.run_selected(profiles)
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("host-expected.yaml", result.stdout)
        for profiles in (
            "monitoring,monitoring-host",
            "monitoring-host,monitoring",
        ):
            result = self.run_selected(profiles)
            self.assertEqual(result.returncode, 0)
            self.assertIn("host-expected.yaml", result.stdout)
            self.assertNotIn("openobserve.yaml", result.stdout)
        self.secret.write_text('{"token":"o2oi_' + "s" * 32 + '"}')
        self.secret.chmod(0o600)
        result = self.run_selected(
            "monitoring,monitoring-host,openobserve-lab"
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("host-expected.yaml", result.stdout)
        self.assertIn("openobserve.yaml", result.stdout)
