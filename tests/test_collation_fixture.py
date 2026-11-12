"""Source and mocked fixture contracts; no SQL Server is contacted."""

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

# Skip only an absent optional package; keep broken installs as errors.
try:
    from sqlfluff.core import Linter
except ModuleNotFoundError as error:
    if (
        error.name != "sqlfluff"
        or importlib.util.find_spec("sqlfluff") is not None
    ):
        raise
    raise unittest.SkipTest(
        "Optional tests need sqlfluff. In a uv virtual environment, run "
        "uv pip sync --require-hashes requirements-dev.lock."
    ) from error

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collation_fixture", ROOT / "tests/integration/sqlserver_collation/run.py"
)
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)
PROBE_SPEC = importlib.util.spec_from_file_location(
    "transaction_probe", FIXTURE.HERE / "transaction_probe.py"
)
PROBE = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(PROBE)
NONCE = "a" * 32
DATABASE = "lh_collation_" + NONCE[:16] + "_ci"
VERSION = "b" * 40


def selected():
    """Use only fixed-format synthetic values, never site settings."""
    return FIXTURE.variables(DATABASE, NONCE, VERSION)


def output_with_terminal(text):
    """Simulate complete client output, including the final batch receipt."""
    terminal = FIXTURE.PREFIX + json.dumps(
        {"stage": "batch_completed", "status": "passed"}
    )
    return (text + "\n" + terminal + "\n").encode()


class FixtureConsentTests(unittest.TestCase):
    """A repository test can never contact a DB without explicit opt-in."""

    def test_minimal_probe_has_no_fixture_wrapper_or_permanent_writes(self):
        for variant in ("inline", "dynamic", "scoped"):
            for option in ("ON", "OFF"):
                for insert in (False, True):
                    body = PROBE.source(variant, option, insert)
                    self.assertNotIn("fixture_ctl", body)
                    self.assertNotIn("LakehouseCollationFixtureOwner", body)
                    self.assertEqual("INSERT #sentinel" in body, insert)
                    self.assertEqual("CREATE TABLE #sentinel" in body, insert)
                    self.assertEqual(
                        Linter(dialect="tsql").parse_string(body).violations,
                        [],
                    )
                    self.assertLess(
                        body.index("@Xact=XACT_STATE()"),
                        body.index("ROLLBACK TRANSACTION"),
                    )

    def test_minimal_probe_requires_ci_before_opening_a_connection(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(PROBE, "execute") as execute:
                with self.assertRaisesRegex(RuntimeError, "DISPOSABLE_CI"):
                    PROBE.main()
                execute.assert_not_called()

    def test_default_plan_has_no_subprocess_or_password_prompt(self):
        output = io.StringIO()
        with (
            mock.patch.object(FIXTURE.subprocess, "run") as process,
            mock.patch.object(FIXTURE.getpass, "getpass") as password,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(FIXTURE.main([]), 0)
        process.assert_not_called()
        password.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "not_run")
        self.assertFalse(result["runtime_proof"])
        self.assertIn("forced column", result["mixed_collation_reproduction"])
        self.assertTrue(result["cleanup"].startswith("none"))

    def test_run_requires_exact_disposable_consent_before_any_connection(self):
        for consent in ("", "yes", "CREATE_SYNTHETIC_DATABASES"):
            with self.subTest(consent=consent):
                with (
                    mock.patch.object(FIXTURE.subprocess, "run") as process,
                    mock.patch.object(FIXTURE.getpass, "getpass") as password,
                    self.assertRaises(FIXTURE.FixtureError),
                ):
                    FIXTURE.main(["--run", "--consent", consent])
                process.assert_not_called()
                password.assert_not_called()

    def test_endpoint_rejects_hosts_named_instances_forward_options(self):
        for value in (
            None,
            "sql.example.invalid,1433",
            "localhost,1433",
            "tcp:" + ".".join(("192", "0", "2", "1")) + ",1433",
            "tcp:127.0.0.1,80",
            "tcp:127.0.0.1,65536",
            "tcp:127.0.0.1,1433 -P x",
            "tcp:127.0.0.1\\INSTANCE,1433",
        ):
            with self.subTest(value=value):
                with self.assertRaises(FIXTURE.FixtureError):
                    FIXTURE.endpoint(value)
        self.assertEqual(
            FIXTURE.endpoint("tcp:127.0.0.1,1433"), "tcp:127.0.0.1,1433"
        )

    def test_missing_client_is_blocked_without_prompt_or_git_process(self):
        with (
            mock.patch.object(FIXTURE.shutil, "which", return_value=None),
            mock.patch.object(FIXTURE.subprocess, "run") as process,
            mock.patch.object(FIXTURE.getpass, "getpass") as password,
            self.assertRaisesRegex(FIXTURE.FixtureError, "SQLCMD_UNAVAILABLE"),
        ):
            FIXTURE.main(
                [
                    "--run",
                    "--consent",
                    FIXTURE.CONSENT,
                    "--server",
                    "tcp:127.0.0.1,1433",
                ]
            )
        process.assert_not_called()
        password.assert_not_called()

    def test_synthetic_names_and_variables_are_not_arbitrary_inputs(self):
        for database, nonce, version in (
            ("master", NONCE, VERSION),
            (DATABASE + "';--", NONCE, VERSION),
            (DATABASE, "not-a-nonce", VERSION),
            (DATABASE, NONCE, "main"),
        ):
            with self.assertRaises(FIXTURE.FixtureError):
                FIXTURE.variables(database, nonce, version)
        for changes in (
            {"DemoWriterUser": "other_user"},
            {"TargetDatabase": "master"},
            {"ConfirmDisableCdc": "1"},
        ):
            with self.assertRaises(FIXTURE.FixtureError):
                FIXTURE.render("003", dict(selected(), **changes))

    def test_go_version_gate_is_exact_and_does_not_forward_version_body(self):
        for output, success in (
            (b"Version: 1.10.0\n", True),
            (b"sqlcmd v1.10.0\n", True),
            (b"Version: 1.10.1\n", False),
            (b"ODBC sqlcmd 18.0\n", False),
            (b"synthetic-private-output\n", False),
        ):
            with self.subTest(success=success):
                completed = subprocess.CompletedProcess([], 0, output)
                with mock.patch.object(
                    FIXTURE.subprocess, "run", return_value=completed
                ) as process:
                    if success:
                        self.assertEqual(
                            FIXTURE.verify_client("sqlcmd"), "go-sqlcmd-1.10.0"
                        )
                    else:
                        with self.assertRaisesRegex(
                            FIXTURE.FixtureError, "GO_SQLCMD_1_10_0_REQUIRED"
                        ):
                            FIXTURE.verify_client("sqlcmd")
                self.assertEqual(
                    process.call_args.args[0], ["sqlcmd", "--version"]
                )
                self.assertNotIn(
                    "SQLCMDPASSWORD", process.call_args.kwargs["env"]
                )

    def test_wrong_client_version_stops_before_password_or_database(self):
        with (
            mock.patch.object(FIXTURE.shutil, "which", return_value="sqlcmd"),
            mock.patch.object(
                FIXTURE.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, b"Version: 1.10.1\n"
                ),
            ) as process,
            mock.patch.object(FIXTURE.getpass, "getpass") as password,
            self.assertRaises(FIXTURE.FixtureError),
        ):
            FIXTURE.main(
                [
                    "--run",
                    "--consent",
                    FIXTURE.CONSENT,
                    "--server",
                    "tcp:127.0.0.1,1433",
                ]
            )
        password.assert_not_called()
        self.assertEqual(process.call_count, 1)
        self.assertEqual(process.call_args.args[0], ["sqlcmd", "--version"])


class FaultInjectionContractTests(unittest.TestCase):
    """Verify injected copies and observations without executing any SQL."""

    def test_fault_copies_leave_repository_ddl_unchanged(self):
        for number, stage, error in (
            ("003", "checker", 51990),
            ("004", "enable", 51991),
            ("003", "grant", 51994),
            ("005", "disable", 51993),
        ):
            with self.subTest(stage=stage):
                before = FIXTURE.SCRIPTS[number].read_bytes()
                body = FIXTURE.render(number, selected(), confirm=True)
                injected = FIXTURE.inject(body, stage)
                self.assertIn("THROW " + str(error), injected)
                self.assertEqual(FIXTURE.SCRIPTS[number].read_bytes(), before)
                self.assertEqual(
                    injected.count("Synthetic injected failure"), 1
                )

    def test_checker_fault_precedes_identity_validation_after_creation(self):
        injected = FIXTURE.inject(FIXTURE.render("003", selected()), "checker")
        self.assertLess(
            injected.index("EXEC sys.sp_executesql @SafetyDefinition"),
            injected.index("THROW 51990"),
        )
        self.assertLess(
            injected.index("THROW 51990"),
            injected.index("SELECT 1 FROM sys.sql_modules"),
        )

    def test_unknown_or_ambiguous_fault_anchor_fails_closed(self):
        for body in (
            "SELECT 1;",
            "@captured_column_list=@CapturedColumns;\n" * 2,
        ):
            with self.assertRaises(FIXTURE.FixtureError):
                FIXTURE.inject(body, "enable")
        with self.assertRaises(FIXTURE.FixtureError):
            FIXTURE.inject("SELECT 1", "unknown")

    def test_residual_observation_is_on_both_sides_of_explicit_rollback(self):
        body = FIXTURE.expect_failure(
            "THROW 51990,'Synthetic',1;",
            51990,
            "checker_creation_fault",
            inspect_residual=True,
        )
        self.assertLess(
            body.index("before_explicit_rollback"),
            body.index("ROLLBACK TRANSACTION"),
        )
        self.assertLess(
            body.index("ROLLBACK TRANSACTION"),
            body.index("after_explicit_rollback"),
        )
        for column in (
            "checker_present",
            "fixed_role_count",
            "fixed_permission_count",
            "transaction_state",
        ):
            self.assertIn(column, body)
        self.assertIn("IF ERROR_NUMBER()<>51990 THROW", body)
        self.assertNotIn("DROP PROCEDURE", body)

    def test_legacy_assertions_cover_grants_identity_and_shared_job_ids(self):
        body = FIXTURE.preserve_legacy()
        for term in (
            "capture_object_id",
            "capture_job_id",
            "cleanup_job_id",
            "legacy_permissions",
            "database_role_members",
        ):
            self.assertIn(term, body)
        self.assertEqual(body.count("EXCEPT"), 2)
        self.assertNotIn("DROP", body)
        self.assertNotIn("sp_cdc_disable", body)

    def test_sources_never_drop_database_or_repair_unrelated_checker(self):
        source = (FIXTURE.HERE / "run.py").read_text()
        self.assertNotRegex(source, r"(?i)DROP\s+(DATABASE|PROCEDURE)")
        self.assertNotRegex(source, r"(?i)ALTER\s+(SERVER|DATABASE).*COLLATE")
        self.assertNotIn("CREATE OR ALTER", source)
        self.assertIn("Never adopt an existing database", source)
        self.assertIn("FAUL" + "T_ANCHOR_CHANGED_REVIEW_REQUIRED", source)

    def test_rendered_ddl_keeps_exact_definition_and_confirmation(self):
        body = FIXTURE.render("003", selected())
        canonical = (ROOT / "scripts/sqlserver_safety.sql").read_text().strip()
        self.assertIn(canonical.replace("'", "''"), body)
        self.assertIn(
            "ConfirmDisableCdc", (FIXTURE.SCRIPTS["005"]).read_text()
        )
        self.assertIn("=N'0'", FIXTURE.render("005", selected()))
        self.assertIn("=N'1'", FIXTURE.render("005", selected(), confirm=True))

    def test_owned_db_guard_precedes_each_executed_stage(self):
        body = FIXTURE.guard(DATABASE, NONCE)
        self.assertIn("LakehouseCollationFixtureOwner", body)
        self.assertIn("DB_NAME() COLLATE Latin1_General_100_BIN2", body)
        self.assertIn("THROW 51970", body)
        self.assertNotIn("sp_addextendedproperty", body)

    def test_generated_sql_parses_without_claiming_engine_success(self):
        linter = Linter(dialect="tsql")
        samples = [
            (FIXTURE.HERE / "mixed_collation.sql").read_text(),
            FIXTURE.create_database(DATABASE, NONCE, "Latin1_General_CI_AS"),
            FIXTURE.guard(DATABASE, NONCE),
            FIXTURE.prepare_legacy_cdc(),
            FIXTURE.preserve_legacy(),
            FIXTURE.successful_permissions(),
            (FIXTURE.HERE / "role_alias.sql").read_text(),
            FIXTURE.role_alias_identity(),
            FIXTURE.initialization_snapshot("Before"),
            FIXTURE.initialization_snapshot("After", retained_only=True),
            FIXTURE.stop_fixture_jobs(),
            FIXTURE.role_alias_probe("SELECT 1;", "004"),
            FIXTURE.role_alias_probe("SELECT 1;", "005"),
            FIXTURE.expect_failure(
                "THROW 51990,'Synthetic',1;",
                51990,
                "checker_creation_fault",
                inspect_residual=True,
            ),
        ]
        for index, body in enumerate(samples):
            with self.subTest(index=index):
                self.assertEqual(linter.parse_string(body).violations, [])

    def test_initialization_matrix_is_explicit_and_never_edits_system_rows(
        self,
    ):
        expected = {
            "zero_no_jobs",
            "zero_with_jobs",
            "independent_null",
            "partial_three",
            "wrong_definition",
            "replace_four",
            "unknown_capture",
            "dropped_source",
            "partitioned",
        }
        self.assertEqual(set(FIXTURE.plan()["initialization_cases"]), expected)
        body = FIXTURE.replacement(selected())
        self.assertNotIn("$(", body)
        self.assertIn("fixture_old_4_v1", body)
        self.assertIn("THROW 51204", body)
        self.assertNotRegex(body, r"(?i)UPDATE\s+(?:cdc|msdb)\.")
        source = (FIXTURE.HERE / "run.py").read_text()
        self.assertIn("WHERE has_drop_pending=1", source)
        self.assertIn("DROP TABLE dbo.fixture_legacy_data", source)
        self.assertNotRegex(source, r"(?i)UPDATE\s+cdc\.")

    def test_outer_transaction_probe_asserts_before_owner_rollback(self):
        body = FIXTURE.outer_transaction_probe(
            "RAISERROR('Synthetic outer-transaction guard.',16,1); RETURN;",
            "outer_transaction_003_preserved",
            1,
        )
        for required in (
            "SET XACT_ABORT ON",
            "BEGIN TRANSACTION",
            "XACT_STATE()<>1",
            "@@TRANCOUNT<>@CallerCount",
            "@@OPTIONS<>@CallerOptions",
            "ERROR_NUMBER()<>50000",
            "@BeforeObjects",
            "@BeforeRoles",
            "@BeforeGrants",
        ):
            self.assertIn(required, body)
        self.assertLess(
            body.index("XACT_STATE()<>1"), body.index("ROLLBACK TRANSACTION")
        )
        self.assertLess(
            body.index("@BeforeGrants<>"), body.index("ROLLBACK TRANSACTION")
        )
        self.assertEqual(body.count("ROLLBACK TRANSACTION"), 1)
        self.assertNotIn("COMMIT TRANSACTION", body)
        self.assertEqual(
            Linter(dialect="tsql").parse_string(body).violations, []
        )

    def test_all_five_outer_transaction_guards_have_explicit_fixture_cases(
        self,
    ):
        runner = FIXTURE.Runner(
            "tcp:127.0.0.1,1433", "sqlcmd", "synthetic", NONCE, VERSION
        )
        calls = []
        with mock.patch.object(
            runner, "execute", side_effect=lambda *a, **k: calls.append(a[1])
        ):
            runner.outer_transaction_guards(DATABASE, selected())
        self.assertEqual(len(calls), 12)
        self.assertIn(
            "CREATE TABLE fixture_ctl.outer_transaction_sentinel", calls[0]
        )
        for index, number in enumerate(
            ("003", "004", "005", "preflight", "cdc_preflight"), 7
        ):
            self.assertIn(
                "outer_transaction_" + number + "_preserved", calls[index]
            )
            self.assertIn("WHERE sentinel_id=" + str(index - 6), calls[index])
            self.assertIn("IF ERROR_NUMBER()<>50000 THROW", calls[index])

    def test_transaction_experiment_observes_catch_before_owner_rollback(self):
        for variant in ("inline", "dynamic_raise", "full_003"):
            for option in ("ON", "OFF"):
                with self.subTest(variant=variant, option=option):
                    body = FIXTURE.transaction_doom_experiment(
                        "RAISERROR('Synthetic full source.',16,1); RETURN;",
                        variant,
                        option,
                    )
                    self.assertIn("SET XACT_ABORT " + option, body)
                    for expression in (
                        "ERROR_NUMBER()",
                        "ERROR_STATE()",
                        "ERROR_LINE()",
                        "@CaughtState=XACT_STATE()",
                    ):
                        self.assertLess(
                            body.index(expression),
                            body.index("ROLLBACK TRANSACTION"),
                        )
                    self.assertNotIn("COMMIT TRANSACTION", body)
                    self.assertEqual(
                        Linter(dialect="tsql").parse_string(body).violations,
                        [],
                    )

    def test_cdc_diagnostic_rejects_outer_transaction_before_options(self):
        body = FIXTURE.SCRIPTS["cdc_preflight"].read_text()
        guard = body.index("IF @@TRANCOUNT<>0")
        refusal = body.index("RAISERROR(")
        returned = body.index("RETURN;")
        first_option = body.index("SET IMPLICIT_TRANSACTIONS OFF")
        self.assertLess(guard, refusal)
        self.assertLess(refusal, returned)
        self.assertLess(returned, first_option)
        self.assertLess(first_option, body.index("BEGIN TRANSACTION"))


class RoleAliasFixtureTests(unittest.TestCase):
    """Check source/mock contracts, not actual CI/CS catalog behavior."""

    def test_alias_setup_uses_supported_cdc_and_observes_actual_saved_identity(
        self,
    ):
        setup = (FIXTURE.HERE / "role_alias.sql").read_text()
        self.assertIn("EXEC sys.sp_cdc_enable_table", setup)
        self.assertIn("@role_name=N'LH_DEMO_CDC_READER'", setup)
        observation = FIXTURE.role_alias_identity()
        self.assertIn("@SavedRole=role_name", observation)
        self.assertIn(
            "@SavedRoleId=DATABASE_PRINCIPAL_ID(@SavedRole)", observation
        )
        self.assertIn("WHEN @SavedRoleId=@FixedRoleId", observation)
        self.assertIn(
            "@SavedRole COLLATE Latin1_General_100_BIN2", observation
        )
        self.assertIn("@SavedRoleId IS NULL", observation)
        self.assertNotRegex(setup + observation, r"(?i)UPDATE\s+cdc\.")
        self.assertIn(
            "tests/integration/sqlserver_collation/role_alias.sql",
            FIXTURE.source_hashes(),
        )

    def test_probes_refuse_shared_identity_before_asserting_unchanged_metadata(
        self,
    ):
        for number in ("004", "005"):
            with self.subTest(number=number):
                body = FIXTURE.role_alias_probe("SELECT 1;", number)
                self.assertIn("IF @SamePrincipal=1", body)
                self.assertIn("IF ERROR_NUMBER()<>51115 THROW", body)
                for part in ("Captures", "Roles", "Members", "Grants"):
                    self.assertIn("DECLARE @Before" + part, body)
                    self.assertIn("@Before" + part + "<>", body)
                self.assertLess(
                    body.index("IF ERROR_NUMBER()<>51115 THROW"),
                    body.index("IF @BeforeCaptures<>"),
                )
                self.assertIn("role_alias_" + number + "_preserved", body)
                self.assertIn("distinct_role_" + number + "_passed", body)
                self.assertIn("WHERE object_id=@AliasCapture", body)
                self.assertIn(
                    "DATABASE_PRINCIPAL_ID(role_name)=@SavedRoleId", body
                )
                self.assertIn("Legacy fixture permissions changed", body)
                self.assertNotIn("SELECT * FROM cdc.change_tables", body)
                for asynchronous in (
                    "start_lsn",
                    "end_lsn",
                    "has_drop_pending",
                ):
                    self.assertNotIn(asynchronous, body)
                self.assertNotIn("UPPER(", body)
                self.assertNotRegex(body, r"(?i)UPDATE\s+cdc\.")
        with self.assertRaises(FIXTURE.FixtureError):
            FIXTURE.role_alias_probe("SELECT 1;", "003")

    def test_canonicalization_or_missing_observations_remain_coverage_gaps(
        self,
    ):
        def observed(same, different):
            return {
                "stage": "role_alias_metadata",
                "same_principal": same,
                "saved_case_different": different,
            }

        for records, expected in (
            ([], (False, False, 0)),
            ([observed(True, False)], (False, False, 1)),
            ([observed(True, True)], (True, False, 1)),
            ([observed(False, True)], (False, True, 1)),
            ([observed(True, True), observed(False, True)], (True, True, 2)),
        ):
            with self.subTest(expected=expected):
                result = FIXTURE.role_alias_coverage(records)
                self.assertEqual(tuple(result.values()), expected)

    def test_real_pending_drop_absence_remains_a_coverage_gap(self):
        rows = [
            {
                "stage": "init_pending_drop_observation",
                "case": case,
                "drop_pending_one_observed": observed,
            }
            for case, observed in (
                ("dropped_source_ci", True),
                ("dropped_source_cs", False),
            )
        ]
        coverage = FIXTURE.pending_drop_coverage(rows)
        self.assertTrue(coverage["ci_observed"])
        self.assertFalse(coverage["cs_observed"])
        self.assertEqual(coverage["observation_count"], 2)
        missing = FIXTURE.pending_drop_coverage([])
        self.assertFalse(missing["ci_observed"] or missing["cs_observed"])

    def test_runner_isolated_role_case_runs_both_scripts_with_explicit_consent(
        self,
    ):
        runner = FIXTURE.Runner(
            "tcp:127.0.0.1,1433", "sqlcmd", "synthetic", NONCE, VERSION
        )
        calls = []
        with mock.patch.object(
            runner, "execute", side_effect=lambda *a, **k: calls.append(a[1])
        ):
            runner.shared_role_alias("role_ci", "Latin1_General_CI_AS")
        self.assertEqual(len(calls), 7)
        self.assertIn("CREATE DATABASE", calls[0])
        self.assertIn("role_alias_metadata", calls[4])
        self.assertIn("role_alias_004_rejected", calls[5])
        self.assertIn("role_alias_005_rejected", calls[6])
        self.assertIn("ConfirmDisableCdc nvarchar(4000)=N''1''", calls[6])
        self.assertEqual(runner.databases_created, 1)
        self.assertNotIn("DROP DATABASE", "\n".join(calls))

    def test_observation_receipt_requires_boolean_evidence_and_omits_raw_names(
        self,
    ):
        runner = FIXTURE.Runner(
            "tcp:127.0.0.1,1433", "sqlcmd", "synthetic", NONCE, VERSION
        )
        baseline = {
            "stage": "role_alias_metadata",
            "status": "observed",
            "same_principal": True,
            "saved_case_different": False,
        }
        for changes in (
            {},
            {"same_principal": 1},
            {"same_principal": "true"},
            {"saved_role": "LH_DEMO_CDC_READER"},
            {"status": "passed"},
        ):
            receipt = dict(baseline, **changes)
            completed = subprocess.CompletedProcess(
                [],
                0,
                output_with_terminal(FIXTURE.PREFIX + json.dumps(receipt)),
            )
            with mock.patch.object(
                FIXTURE.subprocess, "run", return_value=completed
            ):
                if not changes:
                    self.assertEqual(
                        runner.execute(DATABASE, "SELECT 1;")[0][
                            "saved_case_different"
                        ],
                        False,
                    )
                else:
                    with self.assertRaises(FIXTURE.FixtureError):
                        runner.execute(DATABASE, "SELECT 1;")


class SqlcmdBoundaryTests(unittest.TestCase):
    """Exercise bounded synthetic client invocation with a fake subprocess."""

    def runner(self):
        return FIXTURE.Runner(
            "tcp:127.0.0.1,1433",
            "official-sqlcmd",
            "synthetic-" + "fixture-credential",
            NONCE,
            VERSION,
        )

    def test_client_decorations_do_not_hide_bounded_receipts(self):
        receipt = FIXTURE.PREFIX + json.dumps(
            {"stage": "fresh_database_created", "status": "passed"}
        )
        for output in (
            receipt,
            "mssql: " + receipt,
            "\x1b[33m" + receipt + "\x1b[0m",
        ):
            completed = subprocess.CompletedProcess(
                [], 0, output_with_terminal(output)
            )
            with mock.patch.object(
                FIXTURE.subprocess, "run", return_value=completed
            ):
                result = self.runner().execute(DATABASE, "SELECT 1;")
                self.assertEqual(result[0]["stage"], "fresh_database_created")

    def test_expected_caught_errors_finish_but_unexpected_errors_still_fail(
        self,
    ):
        for number, expected in ((51991, True), (208, False)):
            receipt = {
                "stage": "partial_enable",
                "status": "passed",
                "expected_error": 51991,
            }
            output = (
                f"Msg {number}, Level 16, State 1\n"
                + FIXTURE.PREFIX
                + json.dumps(receipt)
            )
            completed = subprocess.CompletedProcess(
                [], 0, output_with_terminal(output)
            )
            with mock.patch.object(
                FIXTURE.subprocess, "run", return_value=completed
            ):
                if expected:
                    self.runner().execute(DATABASE, "SELECT 1;")
                else:
                    with self.assertRaises(FIXTURE.FixtureError):
                        self.runner().execute(DATABASE, "SELECT 1;")

    def test_password_is_only_in_child_env_input_is_private_and_removed(self):
        runner = self.runner()
        seen = []

        def fake(command, **kwargs):
            self.assertNotIn(runner.password, str(command))
            self.assertNotIn("-P", command)
            self.assertEqual(command[command.index("-N") + 1], "mandatory")
            self.assertEqual(command[command.index("-V") + 1], "17")
            self.assertEqual(kwargs["env"]["SQLCMDPASSWORD"], runner.password)
            self.assertNotIn("HTTP_PROXY", kwargs["env"])
            self.assertNotIn("SQLCMDUSER", kwargs["env"])
            path = Path(command[command.index("-i") + 1])
            seen.append(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(path.read_text().startswith("SET NOCOUNT ON"))
            self.assertTrue(path.read_text().endswith("\nGO\n"))
            value = {"stage": "ddl_003_retry_passed", "status": "passed"}
            return subprocess.CompletedProcess(
                command,
                0,
                output_with_terminal(
                    "noisy progress\n"
                    + FIXTURE.PREFIX
                    + json.dumps(value)
                    + "\n"
                ),
            )

        with (
            mock.patch.object(FIXTURE.subprocess, "run", side_effect=fake),
            mock.patch.dict(
                os.environ,
                {"HTTP_PROXY": "synthetic-private", "SQLCMDUSER": "inherited"},
            ),
        ):
            result = runner.execute(DATABASE, "SELECT 1;")
        self.assertEqual(result[0]["stage"], "ddl_003_retry_passed")
        self.assertTrue(all(not path.exists() for path in seen))
        self.assertNotIn(runner.password, json.dumps(result))

    def test_raw_errors_missing_records_and_secret_fields_cannot_escape(self):
        for output, code in (
            (b"raw SQL private diagnostic", 1),
            (b"no fixture records", 0),
            (
                (
                    FIXTURE.PREFIX
                    + json.dumps(
                        {"stage": "ddl_003_retry_passed", "status": "passed"}
                    )
                ).encode(),
                0,
            ),
            (
                (
                    FIXTURE.PREFIX
                    + json.dumps(
                        {
                            "stage": "ddl_003_retry_passed",
                            "status": "passed",
                            "password": "synthetic",
                        }
                    )
                ).encode(),
                0,
            ),
            (
                (
                    FIXTURE.PREFIX
                    + json.dumps({"stage": "unexpected", "status": "passed"})
                ).encode(),
                0,
            ),
            (
                (
                    FIXTURE.PREFIX
                    + json.dumps(
                        {
                            "stage": "ddl_003_retry_passed",
                            "status": "passed",
                            "checker_present": "synthetic",
                        }
                    )
                ).encode(),
                0,
            ),
        ):
            with self.subTest(code=code, size=len(output)):
                completed = subprocess.CompletedProcess([], code, output)
                with (
                    mock.patch.object(
                        FIXTURE.subprocess, "run", return_value=completed
                    ),
                    self.assertRaises(FIXTURE.FixtureError) as raised,
                ):
                    self.runner().execute(DATABASE, "SELECT 1;")
                self.assertNotIn("private diagnostic", str(raised.exception))
                self.assertNotIn("synthetic", str(raised.exception))

    def test_lifecycle_sequences_are_fixed_and_no_sources_are_mutated(self):
        runner = self.runner()
        calls = []
        with mock.patch.object(
            runner, "execute", side_effect=lambda *a, **k: calls.append((a, k))
        ):
            runner.lifecycle("ci", "Latin1_General_CI_AS")
        all_sql = "\n".join(args[1] for args, _ in calls)
        for error in ("51990", "51991", "51993", "51994", "51123"):
            self.assertIn(error, all_sql)
        self.assertIn("fixed_role_count", all_sql)
        self.assertIn("before_explicit_rollback", all_sql)
        self.assertIn("fixed_capture_count", all_sql)
        self.assertEqual(runner.databases_created, 1)
        self.assertFalse(any("DROP DATABASE" in args[1] for args, _ in calls))

    def test_identity_cases_preserve_mismatch_without_any_drop_or_alter_module(
        self,
    ):
        for case in FIXTURE.CHECKER_CASES:
            runner = self.runner()
            calls = []
            with mock.patch.object(
                runner,
                "execute",
                side_effect=lambda *a, **k: calls.append(a[1]),
            ):
                runner.checker_identity(case)
            body = "\n".join(calls)
            self.assertNotIn("DROP PROCEDURE", body)
            self.assertNotIn("ALTER PROCEDURE", body)
            if case == "own_exact":
                self.assertIn("own_checker_reused_again", body)
            else:
                self.assertIn("untrusted_checker_preserved", body)
                self.assertIn("IF ERROR_NUMBER()<>", body)
