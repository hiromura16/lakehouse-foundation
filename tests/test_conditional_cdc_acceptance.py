"""Fail-closed tests for the approved pending-one acceptance policy."""

import copy
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import sqlfluff  # noqa: F401
except ModuleNotFoundError as error:
    if error.name != "sqlfluff":
        raise
    raise unittest.SkipTest("Install hashed requirements-dev.lock") from error

HERE = Path(__file__).resolve().parent / "integration/sqlserver_collation"
SPEC = importlib.util.spec_from_file_location(
    "conditional_cdc_acceptance", HERE / "conditional_acceptance.py"
)
ACCEPT = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(HERE))
try:
    SPEC.loader.exec_module(ACCEPT)
finally:
    sys.path.pop(0)


def evidence():
    hashes = ACCEPT.fixture.source_hashes()
    contract = {
        "evidence_kind": "synthetic_metadata_production_contract",
        "status": "passed",
        "source_commit": "a" * 40,
        "runtime": {"major_version": 16, "edition": "Developer Edition"},
        "source_sha256": {
            f: hashes["infra/sqlserver/" + f]
            for f in ACCEPT.health_contract.FILES
        },
        "engine_pending_one_coverage": "not measured here",
        "observations": [
            {
                "file": f,
                "variant": v,
                "cases": 24,
                "accepted": 2,
                "rejected": 22,
                "mismatches": 0 if v == "production" else 1,
            }
            for f in ACCEPT.health_contract.FILES
            for v in ACCEPT.MUTANTS | {"production"}
        ],
    }
    records = []
    for suffix in ("ci", "cs"):
        records.extend(
            {
                "stage": "init_" + case + "_passed",
                "status": "passed",
                "case": case + "_" + suffix,
            }
            for case in ACCEPT.INIT_CASES
        )
        records.append(
            {
                "stage": "corrupt_capture_preserved",
                "status": "passed",
                "case": "lifecycle_" + suffix,
            }
        )
        records.append(
            {
                "stage": "init_pending_drop_observation",
                "status": "observed",
                "case": "dropped_source_" + suffix,
                "drop_pending_one_observed": False,
                "before_scan_one_observed": False,
                "after_scan_one_observed": False,
                "before_scan_rows": 1,
                "after_scan_rows": 0,
            }
        )
        for ordinal in range(1, 7):
            for option in (0, 1):
                records.append(
                    {
                        "stage": "outer_transaction_observation",
                        "status": "observed",
                        "case": "lifecycle_" + suffix,
                        "script_ordinal": ordinal,
                        "inherited_xact_abort_enabled": option,
                        "outer_guard_caught": True,
                        "transaction_state": 1,
                        "transaction_count": 1,
                        "caller_transaction_count": 1,
                        "option_difference": 0,
                        "sentinel_present": True,
                    }
                )
    engine = {
        "coverage_gaps": ["pending_drop_one_ci", "pending_drop_one_cs"],
        "schema_version": 1,
        "suite": "cdc_initialization",
        "pending_drop_one_coverage": {
            "ci_observed": False,
            "cs_observed": False,
            "observation_count": 2,
        },
        "additional_lifecycle_failures": [],
        "status": "passed_with_coverage_gaps",
        "role_alias_coverage": {
            "same_principal_saved_alias_exercised": False,
            "distinct_principal_exercised": False,
            "observation_count": 0,
        },
        "confirmed_databases_created": 20,
        "candidate_synthetic_databases": [str(i) for i in range(20)],
        "fixture_owner_sha256": "b" * 64,
        "client_version": "go-sqlcmd-1.10.0",
        "source_commit": "a" * 40,
        "source_sha256": hashes,
        "records": records,
        "cleanup": "retained_intentionally",
    }
    return contract, engine, 3, "a" * 40, hashes


class ConditionalAcceptanceTests(unittest.TestCase):
    def test_accepts_only_named_remaining_risk(self):
        self.assertEqual(
            ACCEPT.verify(*evidence()),
            "conditionally_accepted_pending_one_unobserved",
        )

    def test_strict_success_still_requires_complete_evidence(self):
        contract, engine, _, commit, hashes = evidence()
        engine["status"] = "passed"
        engine["coverage_gaps"] = []
        for key in ("ci_observed", "cs_observed"):
            engine["pending_drop_one_coverage"][key] = True
        for row in engine["records"]:
            if row["stage"] == "init_pending_drop_observation":
                row["drop_pending_one_observed"] = True
                row["before_scan_one_observed"] = True
        self.assertEqual(
            ACCEPT.verify(contract, engine, 0, commit, hashes), "passed"
        )
        engine["records"].pop()
        with self.assertRaises(ValueError):
            ACCEPT.verify(contract, engine, 0, commit, hashes)

    def test_other_exit_codes_and_false_success_are_rejected(self):
        c, e, _, head, hashes = evidence()
        for exit_code in (-9, 0, 1, 2, 4, True, "3"):
            with self.subTest(code=exit_code), self.assertRaises(ValueError):
                ACCEPT.verify(c, e, exit_code, head, hashes)
        for status in ("passed", "failed_or_partial", "cancelled"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                ACCEPT.verify(c, {**e, "status": status}, 3, head, hashes)

    def test_every_missing_top_level_field_is_rejected(self):
        for target in (0, 1):
            for key in evidence()[target]:
                values = list(evidence())
                del values[target][key]
                with self.subTest(target=target, key=key):
                    with self.assertRaises((KeyError, ValueError)):
                        ACCEPT.verify(*values)

    def test_source_and_commit_mismatch_rejected(self):
        for target in (0, 1):
            values = list(evidence())
            values[target]["source_commit"] = "c" * 40
            with self.assertRaises(ValueError):
                ACCEPT.verify(*values)
            values = list(evidence())
            key = next(iter(values[target]["source_sha256"]))
            values[target]["source_sha256"] = {
                **values[target]["source_sha256"],
                key: "d" * 64,
            }
            with self.assertRaises(ValueError):
                ACCEPT.verify(*values)

    def test_missing_or_duplicate_receipts_and_failed_guard_rejected(self):
        for stage in (
            "init_zero_no_jobs_passed",
            "corrupt_capture_preserved",
            "outer_transaction_observation",
            "init_pending_drop_observation",
        ):
            for action in ("remove", "duplicate", "fail"):
                values = list(evidence())
                records = values[1]["records"]
                row = next(r for r in records if r["stage"] == stage)
                if action == "remove":
                    records.remove(row)
                elif action == "duplicate":
                    records.append(copy.deepcopy(row))
                else:
                    row["status"] = "failed"
                with self.subTest(stage=stage, action=action):
                    with self.assertRaises(ValueError):
                        ACCEPT.verify(*values)

    def test_unknown_gap_and_lifecycle_failure_rejected(self):
        changes = [
            {"unknown_coverage": False},
            {"coverage_gaps": ["pending_drop_one_ci", "unknown_gap"]},
            {"additional_lifecycle_failures": [{"case": "lifecycle_ci"}]},
            {"suite": "collation_full"},
            {"schema_version": True},
            {"confirmed_databases_created": "20"},
        ]
        for change in changes:
            values = list(evidence())
            values[1].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                ACCEPT.verify(*values)
        for change in (
            {"unknown_gap": False},
            {"ci_observed": 0},
            {"observation_count": "2"},
        ):
            values = list(evidence())
            values[1]["pending_drop_one_coverage"].update(change)
            with self.assertRaises(ValueError):
                ACCEPT.verify(*values)

    def test_contract_missing_mutant_and_false_success_rejected(self):
        for action in ("missing", "duplicate", "undetected", "bad_type"):
            values = list(evidence())
            rows = values[0]["observations"]
            if action == "missing":
                rows.pop()
            elif action == "duplicate":
                rows[-1] = copy.deepcopy(rows[0])
            elif action == "undetected":
                next(r for r in rows if r["variant"] != "production")[
                    "mismatches"
                ] = 0
            else:
                rows[0]["cases"] = "24"
            with self.subTest(action=action), self.assertRaises(ValueError):
                ACCEPT.verify(*values)

    def test_unknown_stage_case_field_and_pending_inconsistency(self):
        for mutation in ("stage", "case", "field", "before", "rows", "type"):
            values = list(evidence())
            rows = values[1]["records"]
            row = next(
                r
                for r in rows
                if r["stage"] == "init_pending_drop_observation"
            )
            if mutation == "stage":
                rows.append(
                    {
                        "stage": "unknown_coverage_gap",
                        "case": "unknown",
                        "status": "observed",
                        "failed": True,
                    }
                )
            elif mutation == "case":
                row["case"] = "unknown_ci"
            elif mutation == "field":
                row["failed"] = True
            elif mutation == "before":
                row["before_scan_one_observed"] = True
            elif mutation == "rows":
                row["before_scan_rows"] = -1
            else:
                row["before_scan_rows"] = True
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(ValueError),
            ):
                ACCEPT.verify(*values)

    def test_report_parser_rejects_partial_duplicate_and_extra_output(self):
        self.assertEqual(
            ACCEPT.parse_report('{"status":"passed"}'), {"status": "passed"}
        )
        for text in (
            "",
            "{",
            "{}\n{}",
            "prefix {}",
            "[]",
            '{"a":1,"a":2}',
            '{"a":NaN}',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                ACCEPT.parse_report(text)

    def test_contract_failure_stops_before_engine(self):
        with patch.dict(ACCEPT.os.environ, {"GITHUB_ACTIONS": "true"}):
            with patch.object(ACCEPT, "snapshot", return_value=("a", {})):
                with patch.object(
                    ACCEPT, "execute", return_value=(1, {})
                ) as run:
                    with self.assertRaises(ValueError):
                        ACCEPT.main()
                    run.assert_called_once_with("health_contract.py")

    def test_changed_checkout_stops_before_engine(self):
        with patch.dict(ACCEPT.os.environ, {"GITHUB_ACTIONS": "true"}):
            with patch.object(
                ACCEPT, "snapshot", side_effect=[("a", {}), ("b", {})]
            ):
                with patch.object(
                    ACCEPT, "execute", return_value=(0, {})
                ) as run:
                    with self.assertRaises(ValueError):
                        ACCEPT.main()
                    self.assertEqual(run.call_count, 1)

    def test_timeout_and_malformed_process_report_not_promoted(self):
        with patch.object(
            ACCEPT.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("test", 1500),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                ACCEPT.execute("run.py")
        with patch.object(
            ACCEPT.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 3, "{}\n{}", ""),
        ):
            with self.assertRaises(ValueError):
                ACCEPT.execute("run.py")


if __name__ == "__main__":
    unittest.main()
