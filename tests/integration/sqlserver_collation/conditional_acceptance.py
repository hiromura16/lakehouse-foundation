#!/usr/bin/env python3
"""Explicit CI-only acceptance of one approved residual pending=1 risk.

The strict runner is unchanged. Never use this as a generic exit-3 suppressor.
Both synthetic production-contract and real-engine suites run on this checkout.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import health_contract
import receipt_schema
import run as fixture

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
INIT_CASES = (
    "zero_no_jobs",
    "zero_with_jobs",
    "independent_null",
    "partial_three",
    "wrong_definition",
    "replace_four",
    "unknown_capture",
    "dropped_source",
    "partitioned",
)
REPORT_KEYS = {
    "coverage_gaps",
    "schema_version",
    "suite",
    "pending_drop_one_coverage",
    "additional_lifecycle_failures",
    "status",
    "role_alias_coverage",
    "confirmed_databases_created",
    "candidate_synthetic_databases",
    "fixture_owner_sha256",
    "client_version",
    "source_commit",
    "source_sha256",
    "records",
    "cleanup",
}
MUTANTS = {
    "pending_removed",
    "pending_inverted",
    "source_missing_removed",
    "source_tracking_removed",
    "ct_missing_removed",
    "pending_and_to_or",
    "outer_or_to_and",
}


def require(condition):
    if not condition:
        raise ValueError("CONDITIONAL_ACCEPTANCE_EVIDENCE_REJECTED")


def snapshot():
    """Bind both subprocesses to one commit and exact sources."""
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    hashes = fixture.source_hashes()
    for name in (
        "health_contract.py",
        "transaction_probe.py",
        "receipt_schema.py",
        "conditional_acceptance.py",
    ):
        path = HERE / name
        hashes[str(path.relative_to(ROOT))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return commit, hashes


def verify(contract, engine, exit_code, commit, hashes):
    """Only an otherwise complete initialization suite may accept this risk."""
    require(type(exit_code) is int)
    require(type(contract) is dict and type(engine) is dict)
    require(
        set(contract)
        == {
            "evidence_kind",
            "status",
            "runtime",
            "observations",
            "engine_pending_one_coverage",
            "source_commit",
            "source_sha256",
        }
    )
    require(type(contract["runtime"]["major_version"]) is int)
    require(
        contract["evidence_kind"] == "synthetic_metadata_production_contract"
    )
    require(contract["status"] == "passed")
    require(contract["source_commit"] == commit)
    require(contract["runtime"]["major_version"] == 16)
    require("Developer" in contract["runtime"]["edition"])
    require(set(contract["source_sha256"]) == set(health_contract.FILES))
    for name in health_contract.FILES:
        require(
            contract["source_sha256"][name]
            == hashes["infra/sqlserver/" + name]
        )
    observations = contract["observations"]
    require(type(observations) is list)
    for row in observations:
        require(
            set(row)
            == {
                "file",
                "variant",
                "mismatches",
                "accepted",
                "rejected",
                "cases",
            }
        )
        require(
            all(
                type(row[k]) is int and 0 <= row[k] <= 24
                for k in ("mismatches", "accepted", "rejected", "cases")
            )
        )
    expected = {
        (f, v) for f in health_contract.FILES for v in MUTANTS | {"production"}
    }
    require(len(observations) == len(expected))
    require({(r["file"], r["variant"]) for r in observations} == expected)
    for row in observations:
        require(row["cases"] == 24)
        require(row["accepted"] + row["rejected"] == 24)
        if row["variant"] == "production":
            require(
                (row["mismatches"], row["accepted"], row["rejected"])
                == (0, 2, 22)
            )
        else:
            require(0 < row["mismatches"] <= 24)
    # New report fields, gaps and suites require an explicit verifier update.
    require(set(engine) == REPORT_KEYS)
    require(type(engine["schema_version"]) is int)
    require(engine["schema_version"] == 1)
    require(type(engine["confirmed_databases_created"]) is int)
    require(type(engine["candidate_synthetic_databases"]) is list)
    require(
        all(type(x) is str for x in engine["candidate_synthetic_databases"])
    )
    require(engine["suite"] == "cdc_initialization")
    require(engine["source_commit"] == commit)
    fixture_keys = set(fixture.source_hashes())
    require(engine["source_sha256"] == {k: hashes[k] for k in fixture_keys})
    require(engine["additional_lifecycle_failures"] == [])
    require(engine["client_version"] == "go-sqlcmd-1.10.0")
    require(engine["confirmed_databases_created"] == 20)
    require(len(set(engine["candidate_synthetic_databases"])) == 20)
    # This suite does not run the separate role-alias/checker matrix. Its
    # unexecuted status is explicit, not reclassified as covered by this mode.
    require(
        engine["role_alias_coverage"]
        == {
            "same_principal_saved_alias_exercised": False,
            "distinct_principal_exercised": False,
            "observation_count": 0,
        }
    )
    require(type(engine["role_alias_coverage"]["observation_count"]) is int)
    require(
        all(
            type(engine["role_alias_coverage"][k]) is bool
            for k in (
                "same_principal_saved_alias_exercised",
                "distinct_principal_exercised",
            )
        )
    )
    records = engine["records"]
    receipt_schema.validate(records)
    require(type(records) is list)
    require(
        all(
            type(r) is dict
            and all(type(r[k]) is str for k in ("stage", "case", "status"))
            for r in records
        )
    )
    require(all(r["status"] in {"passed", "observed"} for r in records))
    for suffix in ("ci", "cs"):
        for case in INIT_CASES:
            require(
                sum(
                    r["stage"] == "init_" + case + "_passed"
                    and r["case"] == case + "_" + suffix
                    and r["status"] == "passed"
                    for r in records
                )
                == 1
            )
        require(
            sum(
                r["stage"] == "corrupt_capture_preserved"
                and r["case"] == "lifecycle_" + suffix
                and r["status"] == "passed"
                for r in records
            )
            == 1
        )
        caller = [
            r
            for r in records
            if r["stage"] == "outer_transaction_observation"
            and r["case"] == "lifecycle_" + suffix
        ]
        require(len(caller) == 12)
        require(
            all(
                type(r[k]) is int
                for r in caller
                for k in (
                    "script_ordinal",
                    "inherited_xact_abort_enabled",
                    "transaction_state",
                    "transaction_count",
                    "caller_transaction_count",
                    "option_difference",
                )
            )
        )
        observed = [
            r
            for r in records
            if r["stage"] == "init_pending_drop_observation"
            and r["case"] == "dropped_source_" + suffix
        ]
        require(len(observed) == 1 and observed[0]["status"] == "observed")
        require(type(observed[0]["drop_pending_one_observed"]) is bool)
        require(
            {
                (r["script_ordinal"], r["inherited_xact_abort_enabled"])
                for r in caller
            }
            == {(i, x) for i in range(1, 7) for x in (0, 1)}
        )
        require(
            all(
                r["outer_guard_caught"] is True
                and r["transaction_state"] == 1
                and r["transaction_count"] == 1
                and r["caller_transaction_count"] == 1
                and r["option_difference"] == 0
                and r["sentinel_present"] is True
                for r in caller
            )
        )
    pending = engine["pending_drop_one_coverage"]
    require(
        set(pending) == {"ci_observed", "cs_observed", "observation_count"}
    )
    require(type(pending["observation_count"]) is int)
    require(pending["observation_count"] == 2)
    require(
        all(type(pending[k]) is bool for k in ("ci_observed", "cs_observed"))
    )
    require(fixture.pending_drop_coverage(records) == pending)
    require(
        engine["coverage_gaps"]
        == [
            "pending_drop_one_" + suffix
            for suffix in ("ci", "cs")
            if not pending[suffix + "_observed"]
        ]
    )
    complete = pending["ci_observed"] and pending["cs_observed"]
    require(
        (exit_code, engine["status"])
        == ((0, "passed") if complete else (3, "passed_with_coverage_gaps"))
    )
    return (
        "passed"
        if complete
        else "conditionally_accepted_pending_one_unobserved"
    )


def parse_report(text):
    """Reject truncated, duplicate-key, multi-report and non-JSON output."""

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result)
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("NON_JSON_CONSTANT")

    report = json.loads(
        text, object_pairs_hook=unique, parse_constant=invalid_constant
    )
    require(type(report) is dict)
    return report


def execute(script, args=()):
    result = subprocess.run(
        [sys.executable, str(HERE / script), *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=1500,
        check=False,
    )
    # Parse exactly one report. Never promote missing/extra/malformed output.
    report = parse_report(result.stdout)
    print(json.dumps(report), flush=True)
    return result.returncode, report


def main():
    require(os.environ.get("GITHUB_ACTIONS") == "true")
    before = snapshot()
    contract_exit, contract = execute("health_contract.py")
    require(contract_exit == 0)
    require(snapshot() == before)
    engine_exit, engine = execute(
        "run.py",
        (
            "--run",
            "--initialization",
            "--consent",
            fixture.CONSENT,
            "--server",
            "tcp:127.0.0.1,14333",
            "--password-env",
            "LAKEHOUSE_FIXTURE_PASSWORD",
        ),
    )
    require(snapshot() == before)
    status = verify(contract, engine, engine_exit, *before)
    print(
        json.dumps(
            {
                "acceptance_policy": "approved_synthetic_pending_contract_v1",
                "status": status,
                "source_commit": before[0],
                "verified_source_sha256": before[1],
                "strict_engine_exit": engine_exit,
                "engine_pending_one_coverage": engine[
                    "pending_drop_one_coverage"
                ],
                "residual_risk": "Engine pending=1 remains unobserved"
                if engine_exit == 3
                else "none for this initialization coverage",
                "out_of_scope": [
                    "full checker/role-alias matrix",
                    "Windows client",
                    "connector E2E",
                    "production acceptance",
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
