"""Fixed receipt schemas for the approved initialization suite only.

These public synthetic schemas are pinned independently of run.py parsing.
Adding a stage, case, field or changing a field type requires explicit review.
"""

SCHEMAS = {
    "after_explicit_rollback": (
        "observed",
        ("lifecycle",),
        {
            "checker_present": bool,
            "fixed_permission_count": int,
            "fixed_role_count": int,
            "transaction_count": int,
            "transaction_state": int,
        },
    ),
    "before_explicit_rollback": (
        "observed",
        ("lifecycle",),
        {
            "checker_present": bool,
            "fixed_permission_count": int,
            "fixed_role_count": int,
            "transaction_count": int,
            "transaction_state": int,
        },
    ),
    "checker_creation_fault": (
        "passed",
        ("lifecycle",),
        {"expected_error": int},
    ),
    "corrupt_capture_preflight_refused": (
        "passed",
        ("lifecycle",),
        {"expected_error": int},
    ),
    "corrupt_capture_preserved": ("passed", ("lifecycle",), {}),
    "ddl_001_passed": (
        "passed",
        (
            "dropped_source",
            "independent_null",
            "lifecycle",
            "partial_three",
            "partitioned",
            "replace_four",
            "unknown_capture",
            "wrong_definition",
            "zero_no_jobs",
            "zero_with_jobs",
        ),
        {},
    ),
    "ddl_002_passed": (
        "passed",
        (
            "dropped_source",
            "independent_null",
            "lifecycle",
            "partial_three",
            "partitioned",
            "replace_four",
            "unknown_capture",
            "wrong_definition",
            "zero_no_jobs",
            "zero_with_jobs",
        ),
        {},
    ),
    "ddl_003_idempotent": ("passed", ("lifecycle",), {}),
    "ddl_003_retry_passed": ("passed", ("lifecycle",), {}),
    "disable_idempotent": ("passed", ("lifecycle",), {}),
    "disable_refusal_preserved_captures": ("passed", ("lifecycle",), {}),
    "disable_resume": ("passed", ("lifecycle",), {}),
    "disable_without_consent_refused": (
        "passed",
        ("independent_null", "lifecycle"),
        {"expected_error": int},
    ),
    "enable_idempotent": ("passed", ("lifecycle",), {}),
    "enable_resume": ("passed", ("lifecycle",), {}),
    "five_fixed_captures_remain": ("passed", ("lifecycle",), {}),
    "fresh_database_created": (
        "passed",
        (
            "dropped_source",
            "independent_null",
            "lifecycle",
            "partial_three",
            "partitioned",
            "replace_four",
            "unknown_capture",
            "wrong_definition",
            "zero_no_jobs",
            "zero_with_jobs",
        ),
        {},
    ),
    "init_disable_retry_passed": ("passed", ("independent_null",), {}),
    "init_dropped_source_passed": ("passed", ("dropped_source",), {}),
    "init_fixed_six": (
        "passed",
        ("independent_null", "replace_four", "zero_no_jobs", "zero_with_jobs"),
        {},
    ),
    "init_independent_null_passed": ("passed", ("independent_null",), {}),
    "init_partial_three_passed": ("passed", ("partial_three",), {}),
    "init_partitioned_passed": ("passed", ("partitioned",), {}),
    "init_pending_drop_observation": (
        "observed",
        ("dropped_source",),
        {
            "after_scan_one_observed": bool,
            "after_scan_rows": int,
            "before_scan_one_observed": bool,
            "before_scan_rows": int,
            "drop_pending_one_observed": bool,
        },
    ),
    "init_prepared": (
        "passed",
        (
            "dropped_source",
            "independent_null",
            "partial_three",
            "partitioned",
            "replace_four",
            "unknown_capture",
            "wrong_definition",
            "zero_no_jobs",
            "zero_with_jobs",
        ),
        {},
    ),
    "init_replace_four_passed": ("passed", ("replace_four",), {}),
    "init_replacement_partial": (
        "passed",
        ("replace_four",),
        {"expected_error": int},
    ),
    "init_state_preserved": (
        "passed",
        (
            "dropped_source",
            "independent_null",
            "partial_three",
            "partitioned",
            "replace_four",
            "unknown_capture",
            "wrong_definition",
            "zero_no_jobs",
            "zero_with_jobs",
        ),
        {},
    ),
    "init_three_retained": ("passed", ("partial_three",), {}),
    "init_unknown_capture_passed": ("passed", ("unknown_capture",), {}),
    "init_wrong_definition_passed": ("passed", ("wrong_definition",), {}),
    "init_zero_no_jobs_passed": ("passed", ("zero_no_jobs",), {}),
    "init_zero_with_jobs_passed": ("passed", ("zero_with_jobs",), {}),
    "legacy_prepared": ("passed", ("lifecycle",), {}),
    "mixed_collation": (
        "passed",
        (
            "dropped_source",
            "independent_null",
            "lifecycle",
            "partial_three",
            "partitioned",
            "replace_four",
            "unknown_capture",
            "wrong_definition",
            "zero_no_jobs",
            "zero_with_jobs",
        ),
        {
            "case_alias_detected": bool,
            "forced_column_collations": bool,
            "observed_468": bool,
        },
    ),
    "one_fixed_capture_retained": ("passed", ("lifecycle",), {}),
    "outer_sentinel_prepared": ("passed", ("lifecycle",), {}),
    "outer_transaction_003_preserved": (
        "passed",
        ("lifecycle",),
        {"expected_error": int},
    ),
    "outer_transaction_004_preserved": (
        "passed",
        ("lifecycle",),
        {"expected_error": int},
    ),
    "outer_transaction_005_preserved": (
        "passed",
        ("lifecycle",),
        {"expected_error": int},
    ),
    "outer_transaction_cdc_preflight_preserved": (
        "passed",
        ("lifecycle",),
        {"expected_error": int},
    ),
    "outer_transaction_maintenance_preserved": (
        "passed",
        ("lifecycle",),
        {"expected_error": int},
    ),
    "outer_transaction_observation": (
        "observed",
        ("lifecycle",),
        {
            "caller_transaction_count": int,
            "inherited_xact_abort_enabled": int,
            "option_difference": int,
            "outer_guard_caught": bool,
            "script_ordinal": int,
            "sentinel_present": bool,
            "transaction_count": int,
            "transaction_state": int,
        },
    ),
    "outer_transaction_preflight_preserved": (
        "passed",
        ("lifecycle",),
        {"expected_error": int},
    ),
    "partial_disable": (
        "passed",
        ("independent_null", "lifecycle"),
        {"expected_error": int},
    ),
    "partial_enable": (
        "passed",
        ("lifecycle", "partial_three"),
        {"expected_error": int},
    ),
    "permission_grant_fault": (
        "passed",
        ("lifecycle",),
        {"expected_error": int},
    ),
    "transaction_doom_experiment": (
        "observed",
        ("lifecycle",),
        {
            "catch_transaction_count": int,
            "catch_transaction_state": int,
            "error_line": int,
            "error_number": int,
            "error_state": int,
            "expected_error": int,
            "variant_ordinal": int,
            "xact_abort_enabled": int,
        },
    ),
    "validation_006_captures_0": ("passed", ("lifecycle",), {}),
    "validation_006_captures_6": ("passed", ("lifecycle",), {}),
}


def validate(records):
    """Reject unknown stages/cases/fields and inconsistent pending details."""
    if type(records) is not list:
        raise ValueError("RECEIPT_SCHEMA_INVALID")
    for row in records:
        if type(row) is not dict or row.get("stage") not in SCHEMAS:
            raise ValueError("RECEIPT_SCHEMA_INVALID")
        status, cases, fields = SCHEMAS[row["stage"]]
        # The preparation receipt is also emitted after expected refusals.
        if row["stage"] == "init_prepared" and "expected_error" in row:
            fields = {"expected_error": int}
        if (
            set(row) != {"stage", "status", "case"} | set(fields)
            or row["status"] != status
            or row["case"]
            not in {
                case + "_" + suffix
                for case in cases
                for suffix in ("ci", "cs")
            }
            or any(type(row[k]) is not kind for k, kind in fields.items())
        ):
            raise ValueError("RECEIPT_SCHEMA_INVALID")
        if row["stage"] == "init_pending_drop_observation":
            before = row["before_scan_one_observed"]
            after = row["after_scan_one_observed"]
            if (
                row["drop_pending_one_observed"] != (before or after)
                or row["before_scan_rows"] < 0
                or row["after_scan_rows"] < 0
                or (before and row["before_scan_rows"] == 0)
                or (after and row["after_scan_rows"] == 0)
            ):
                raise ValueError("PENDING_OBSERVATION_INCONSISTENT")
