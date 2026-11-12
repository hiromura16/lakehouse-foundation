#!/usr/bin/env python3
"""Plan an opt-in SQL Server 2022 fixture; never connect by default.

The operator supplies a pre-existing disposable SQL Server 2022 Developer
instance and official sqlcmd. No image is downloaded, service is started,
server collation is changed, or database is dropped. Fresh databases are left
for owner-reviewed disposal. All code is synthetic, not a company runbook.
"""

import argparse
import getpass
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
CONSENT = "CREATE_SYNTHETIC_DATABASES_ON_DISPOSABLE_SQL2022"
PREFIX = "LAKEHOUSE_SQL_FIXTURE="
CHECKER = "lh_demo_ctl.check_write_safety_v1"
EXPECTED_CAPTURES = (
    "lh_demo_erp_products_v1",
    "lh_demo_erp_work_orders_v1",
    "lh_demo_erp_sales_orders_v1",
    "lh_demo_erp_shipments_v1",
    "lh_demo_mes_events_v1",
    "lh_demo_mes_downtime_v1",
)
CHECKER_CASES = (
    "own_exact",
    "unrelated",
    "execution",
    "quoted",
    "owner",
    "effective",
    "whitespace",
    "version",
    "ownership",
)
FIXED_STAGES = (
    {
        "mixed_collation",
        "batch_completed",
        "init_pending_drop_observation",
        "fresh_database_created",
        "ddl_001_passed",
        "ddl_002_passed",
        "legacy_prepared",
        "checker_creation_fault",
        "permission_grant_fault",
        "outer_sentinel_prepared",
        "outer_transaction_003_preserved",
        "outer_transaction_004_preserved",
        "outer_transaction_005_preserved",
        "outer_transaction_preflight_preserved",
        "before_explicit_rollback",
        "after_explicit_rollback",
        "ddl_003_retry_passed",
        "ddl_003_idempotent",
        "partial_enable",
        "one_fixed_capture_retained",
        "enable_resume",
        "enable_idempotent",
        "disable_without_consent_refused",
        "disable_refusal_preserved_captures",
        "partial_disable",
        "five_fixed_captures_remain",
        "disable_resume",
        "disable_idempotent",
        "own_checker_reused",
        "own_checker_reused_again",
        "untrusted_checker_preserved",
        "corrupt_capture_preflight_refused",
        "corrupt_capture_preserved",
        "role_alias_baseline",
        "role_alias_metadata",
        "role_alias_004_rejected",
        "role_alias_005_rejected",
        "role_alias_004_preserved",
        "role_alias_005_preserved",
        "distinct_role_004_passed",
        "distinct_role_005_passed",
    }
    | {
        "init_prepared",
        "init_state_preserved",
        "init_three_retained",
        "init_fixed_six",
        "init_replacement_partial",
    }
    | {
        "init_" + case + "_passed"
        for case in (
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
    }
    | {"validation_006_captures_" + str(n) for n in (0, 6)}
    | {
        "checker_" + case + suffix
        for case in CHECKER_CASES
        for suffix in ("_prepared", "_rejected")
    }
)
SCRIPTS = {
    number: ROOT / "infra/sqlserver" / filename
    for number, filename in (
        ("001", "001_demo_schemas.sql"),
        ("002", "002_demo_tables.sql"),
        ("003", "003_demo_permissions.sql"),
        ("004", "004_demo_cdc_enable.sql"),
        ("005", "005_demo_cdc_disable.sql"),
        ("006", "006_demo_validation.sql"),
        ("preflight", "diagnostics/003_permissions_preflight.sql"),
        ("cdc_preflight", "diagnostics/004_cdc_preflight.sql"),
    )
}


class FixtureError(RuntimeError):
    """Return a fixed diagnostic without SQL output, paths or credentials."""


def literal(text):
    """Quote a controlled SQL value, never a caller-selected identifier."""
    return "N'" + text.replace("'", "''") + "'"


def endpoint(value):
    """Allow only a literal local TCP test instance, with no DNS resolution."""
    found = re.fullmatch(r"tcp:127\.0\.0\.1,([0-9]{4,5})", value or "")
    if not found or not 1024 <= int(found[1]) <= 65535:
        raise FixtureError("LOCAL_DISPOSABLE_ENDPOINT_REQUIRED")
    return value


def client_environment(password=None):
    """Pass only process essentials; exclude inherited sqlcmd/proxy secrets."""
    result = {
        key: os.environ[key]
        for key in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TMP",
            "TEMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
        )
        if key in os.environ
    }
    if password is not None:
        result["SQLCMDPASSWORD"] = password
    return result


def verify_client(sqlcmd):
    """Gate writes on the exact source-verified Go client version only."""
    completed = subprocess.run(
        [str(sqlcmd), "--version"],
        env=client_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    if completed.returncode or len(completed.stdout) > 65536:
        raise FixtureError("GO_SQLCMD_1_10_0_REQUIRED")
    text = completed.stdout.decode("utf-8", errors="strict")
    if not re.search(
        r"(?im)^(?:Version:\s*|sqlcmd:?\s+(?:version\s+)?)"
        r"v?1\.10\.0\s*$",
        text,
    ):
        raise FixtureError("GO_SQLCMD_1_10_0_REQUIRED")
    return "go-sqlcmd-1.10.0"


def source_hashes():
    """Fingerprint public inputs, never read a private configuration file."""
    paths = [
        *SCRIPTS.values(),
        ROOT / "scripts/sqlserver_safety.sql",
        HERE / "run.py",
        HERE / "mixed_collation.sql",
        HERE / "role_alias.sql",
        ROOT / "scripts/prepare-sqlserver.py",
        ROOT / "infra/sqlserver/maintenance/replace_legacy_captures.sql",
    ]
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


def variables(database, nonce, version):
    """Make SQLCMD inputs from validated synthetic identifiers only."""
    if not re.fullmatch(r"lh_collation_[0-9a-f]{16}_[a-z_]{1,24}", database):
        raise FixtureError("SYNTHETIC_DATABASE_NAME_INVALID")
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise FixtureError("SYNTHETIC_OWNER_INVALID")
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise FixtureError("SOURCE_COMMIT_REQUIRED")
    return {
        "TargetDatabase": database,
        "InstallationId": nonce,
        "OwnershipHash": hashlib.sha256((nonce * 2).encode()).hexdigest(),
        "RepositoryVersion": version,
        "DemoWriterUser": "fixture_writer",
        "SourceReaderUser": "fixture_reader",
        "ControlWriterUser": "fixture_control",
        "ConfirmDisableCdc": "0",
    }


def render(number, selected, *, confirm=False):
    """Read current public DDL; do not patch its checker or safety gates."""
    if number not in SCRIPTS:
        raise FixtureError("FIXED_SCRIPT_REQUIRED")
    expected = variables(
        selected.get("TargetDatabase", ""),
        selected.get("InstallationId", ""),
        selected.get("RepositoryVersion", ""),
    )
    if selected != expected:
        raise FixtureError("SYNTHETIC_VARIABLES_CHANGED")
    values = dict(expected, ConfirmDisableCdc="1" if confirm else "0")
    source = SCRIPTS[number].read_text(encoding="utf-8")
    for key, value in values.items():
        source = source.replace("$(" + key + ")", value)
    if "$ (" in source or "$(" in source:
        raise FixtureError("UNRESOLVED_FIXTURE_VARIABLE")
    return source


def inject(source, stage):
    """Insert a test-only THROW in memory; production SQL stays unchanged."""
    expressions = {
        "checker": (
            r"(?m)^IF NOT EXISTS \(\s*\n\s*SELECT 1 FROM sys\.sql_modules",
            51990,
        ),
        "grant": (
            r"(?m)^GRANT SELECT, INSERT, UPDATE, DELETE ON OBJECT::"
            r"\[lh_demo_erp\]\.\[products\] TO \[lh_demo_writer\];$",
            51994,
        ),
        "enable": (r"(?m)^\s*@captured_column_list=@CapturedColumns;", 51991),
        "disable": (
            r"(?m)^\s*@source_schema=@Schema,@source_name=@Table,"
            r"@capture_instance=@Capture;",
            51993,
        ),
    }
    if stage not in expressions:
        raise FixtureError("FIXED_FAULT_STAGE_REQUIRED")
    pattern, error = expressions[stage]
    matches = list(re.finditer(pattern, source))
    if len(matches) != 1:
        raise FixtureError("FAULT_ANCHOR_CHANGED_REVIEW_REQUIRED")
    position = matches[0].start() if stage == "checker" else matches[0].end()
    fault = "\nTHROW " + str(error) + ", 'Synthetic injected failure.', 1;\n"
    return source[:position] + fault + source[position:]


def captures():
    """Use only the six exact source capture names, with binary matching."""
    return (
        "capture_instance COLLATE Latin1_General_100_BIN2 IN ("
        + ",".join(literal(name) for name in EXPECTED_CAPTURES)
        + ")"
    )


def guard(database, nonce):
    """Require the fresh database's fixture marker before every later batch."""
    return f"""SET NOCOUNT ON;
IF DB_NAME() COLLATE Latin1_General_100_BIN2
    <>{literal(database)} COLLATE Latin1_General_100_BIN2
    OR NOT EXISTS (SELECT 1 FROM sys.extended_properties
        WHERE class=0 AND name=N'LakehouseCollationFixtureOwner'
        AND CONVERT(nvarchar(128),value) COLLATE Latin1_General_100_BIN2
            ={literal(nonce)} COLLATE Latin1_General_100_BIN2)
    THROW 51970, 'The fresh synthetic fixture marker did not match.', 1;
"""


def record(stage, extras=""):
    """Print a small machine record; raw SQLCMD output is never forwarded."""
    if stage not in FIXED_STAGES:
        raise FixtureError("FIXED_STAGE_REQUIRED")
    return (
        "PRINT N'"
        + PREFIX
        + '{"stage":"'
        + stage
        + '","status":"passed"'
        + extras
        + "}';\n"
    )


def create_database(database, nonce, collation):
    """Create a never-adopted fresh database; no cleanup/drop is supplied."""
    variables(database, nonce, "0" * 40)
    if collation not in {"Latin1_General_CI_AS", "Latin1_General_CS_AS"}:
        raise FixtureError("FIXED_DATABASE_COLLATION_REQUIRED")
    setup = (
        "EXEC sys.sp_addextendedproperty "
        "@name=N'LakehouseCollationFixtureOwner',@value="
        + literal(nonce)
        + "; CREATE USER [fixture_writer] WITHOUT LOGIN;"
        " CREATE USER [fixture_reader] WITHOUT LOGIN;"
        " CREATE USER [fixture_control] WITHOUT LOGIN;"
        " CREATE USER [fixture_legacy_user] WITHOUT LOGIN;"
        " CREATE ROLE [fixture_legacy_reader] AUTHORIZATION [dbo];"
        " CREATE TABLE dbo.fixture_legacy_data (id int NOT NULL PRIMARY KEY);"
        " GRANT SELECT ON OBJECT::dbo.fixture_legacy_data"
        " TO [fixture_legacy_reader];"
        " ALTER ROLE [fixture_legacy_reader] ADD MEMBER [fixture_legacy_user];"
    )
    return f"""SET NOCOUNT ON;
IF CONVERT(int,SERVERPROPERTY('ProductMajorVersion'))<>16
    OR CONVERT(nvarchar(128),SERVERPROPERTY('Edition')) NOT LIKE N'%Developer%'
    THROW 51971, 'Use an approved disposable SQL2022 Developer instance.', 1;
IF DB_ID({literal(database)}) IS NOT NULL
    THROW 51972, 'Never adopt an existing database for this fixture.', 1;
BEGIN TRY
    EXEC sys.sp_executesql N'CREATE DATABASE [{database}] COLLATE {collation}';
END TRY
BEGIN CATCH
    -- A name collision, permission failure or other CREATE error must stop
    -- before any target batch can execute against an unrelated database.
    THROW;
END CATCH;
EXEC [{database}].sys.sp_executesql {literal(setup)};
""" + record("fresh_database_created")


def residual_snapshot(label):
    """Observe artifacts and permissions without assuming rollback."""
    if label not in {"before_explicit_rollback", "after_explicit_rollback"}:
        raise FixtureError("FIXED_RESIDUAL_STAGE_REQUIRED")
    return f"""DECLARE @Snapshot_{label} nvarchar(4000)=(
SELECT N'{label}' AS stage, N'observed' AS status,
    XACT_STATE() AS transaction_state, @@TRANCOUNT AS transaction_count,
    CONVERT(bit,CASE WHEN OBJECT_ID(N'{CHECKER}',N'P') IS NULL
        THEN 0 ELSE 1 END) AS checker_present,
    (SELECT COUNT_BIG(*) FROM sys.database_principals WHERE name IN (
        N'lh_demo_writer',N'lh_demo_reader',N'lh_demo_control_writer'))
        AS fixed_role_count,
    (SELECT COUNT_BIG(*) FROM sys.database_permissions p
        JOIN sys.database_principals r ON p.grantee_principal_id=r.principal_id
        WHERE r.name IN (N'lh_demo_writer',N'lh_demo_reader',
                         N'lh_demo_control_writer')) AS fixed_permission_count
FOR JSON PATH, WITHOUT_ARRAY_WRAPPER);
PRINT N'{PREFIX}'+@Snapshot_{label};
"""


def expect_failure(source, number, stage, *, inspect_residual=False):
    """Catch only the intended test failure at a lower compilation level."""
    if number not in {
        51003,
        51018,
        51019,
        51110,
        51112,
        51113,
        51130,
        51204,
        51992,
        51115,
        51123,
        51990,
        51991,
        51993,
        51994,
    }:
        raise FixtureError("FIXED_EXPECTED_ERROR_REQUIRED")
    before = (
        residual_snapshot("before_explicit_rollback")
        if inspect_residual
        else ""
    )
    after = (
        residual_snapshot("after_explicit_rollback")
        if inspect_residual
        else ""
    )
    return f"""BEGIN TRY
    EXEC sys.sp_executesql {literal(source)};
    THROW 51989, 'The negative fixture unexpectedly succeeded.', 1;
END TRY
BEGIN CATCH
    IF ERROR_NUMBER()<>{number} THROW;
{before}
    IF XACT_STATE()<>0 ROLLBACK TRANSACTION;
{after}
END CATCH;
""" + record(stage, ',"expected_error":' + str(number))


def outer_transaction_probe(source, stage, ordinal):
    """Assert rejection leaves the caller's own synthetic transaction intact.

    This deliberately does not use expect_failure: that helper rolls back
    before checking residuals, whereas this contract must inspect the still
    committable transaction first. Only this fixture rolls its sentinel back.
    """
    if stage not in {
        "outer_transaction_003_preserved",
        "outer_transaction_004_preserved",
        "outer_transaction_005_preserved",
        "outer_transaction_preflight_preserved",
    } or ordinal not in range(1, 5):
        raise FixtureError("FIXED_OUTER_TRANSACTION_STAGE_REQUIRED")
    return f"""SET XACT_ABORT ON;
BEGIN TRANSACTION;
INSERT fixture_ctl.outer_transaction_sentinel (sentinel_id) VALUES ({ordinal});
DECLARE @CallerCount int=@@TRANCOUNT,@CallerOptions int=@@OPTIONS;
DECLARE @BeforeObjects varbinary(32)=HASHBYTES('SHA2_256',
 CONVERT(nvarchar(max),(SELECT object_id,name,type,principal_id,schema_id
 FROM sys.objects ORDER BY object_id FOR JSON PATH)));
DECLARE @BeforeRoles varbinary(32)=HASHBYTES('SHA2_256',
 CONVERT(nvarchar(max),(SELECT principal_id,name,type,owning_principal_id
 FROM sys.database_principals ORDER BY principal_id FOR JSON PATH)));
DECLARE @BeforeGrants varbinary(32)=HASHBYTES('SHA2_256',
 CONVERT(nvarchar(max),(SELECT class,major_id,minor_id,grantee_principal_id,
 grantor_principal_id,permission_name,state FROM sys.database_permissions
 ORDER BY class,major_id,minor_id,grantee_principal_id,permission_name
 FOR JSON PATH)));
DECLARE @BeforeModules varbinary(32)=HASHBYTES('SHA2_256',
 CONVERT(nvarchar(max),(SELECT object_id,[definition],execute_as_principal_id,
 uses_ansi_nulls,uses_quoted_identifier FROM sys.sql_modules
 ORDER BY object_id FOR JSON PATH)));
DECLARE @CaughtOuterGuard bit=0;
BEGIN TRY
    EXEC sys.sp_executesql {literal(source)};
END TRY
BEGIN CATCH
    IF ERROR_NUMBER()<>50000 THROW;
    SET @CaughtOuterGuard=1;
END CATCH;
IF @CaughtOuterGuard<>1 OR XACT_STATE()<>1 OR @@TRANCOUNT<>@CallerCount
    OR @CallerCount<>1 OR @@OPTIONS<>@CallerOptions
    OR NOT EXISTS (SELECT 1 FROM fixture_ctl.outer_transaction_sentinel
                   WHERE sentinel_id={ordinal})
    THROW 51987, 'The caller transaction or synthetic sentinel changed.', 1;
IF @BeforeObjects<>HASHBYTES('SHA2_256',CONVERT(nvarchar(max),(
 SELECT object_id,name,type,principal_id,schema_id FROM sys.objects
 ORDER BY object_id FOR JSON PATH)))
 OR @BeforeRoles<>HASHBYTES('SHA2_256',CONVERT(nvarchar(max),(
 SELECT principal_id,name,type,owning_principal_id FROM sys.database_principals
 ORDER BY principal_id FOR JSON PATH)))
 OR @BeforeGrants<>HASHBYTES('SHA2_256',CONVERT(nvarchar(max),(
 SELECT class,major_id,minor_id,grantee_principal_id,grantor_principal_id,
 permission_name,state FROM sys.database_permissions
 ORDER BY class,major_id,minor_id,grantee_principal_id,permission_name
 FOR JSON PATH)))
 OR @BeforeModules<>HASHBYTES('SHA2_256',CONVERT(nvarchar(max),(
 SELECT object_id,[definition],execute_as_principal_id,uses_ansi_nulls,
 uses_quoted_identifier FROM sys.sql_modules
 ORDER BY object_id FOR JSON PATH)))
    THROW 51988, 'Rejected call changed synthetic fixture metadata.', 1;
-- Explicitly discard only the transaction started by this synthetic test.
ROLLBACK TRANSACTION;
IF EXISTS (SELECT 1 FROM fixture_ctl.outer_transaction_sentinel
           WHERE sentinel_id={ordinal})
    THROW 51987, 'The fixture sentinel was not discarded by its owner.', 1;
""" + record(stage, ',"expected_error":50000')


def assert_fixed_captures(count):
    """Check captures and tracked flags only for the exact six tables."""
    if count not in range(7):
        raise FixtureError("FIXED_CAPTURE_COUNT_REQUIRED")
    objects = ",".join(
        "OBJECT_ID(" + literal(name) + ",N'U')"
        for name in (
            "lh_demo_erp.products",
            "lh_demo_erp.work_orders",
            "lh_demo_erp.sales_orders",
            "lh_demo_erp.shipments",
            "lh_demo_mes.events",
            "lh_demo_mes.downtime",
        )
    )
    return (
        "IF (SELECT COUNT_BIG(*) FROM cdc.change_tables WHERE "
        + captures()
        + ")<>"
        + str(count)
        + " THROW 51974, 'Unexpected fixed capture count.', 1;\n"
        + "IF (SELECT COUNT_BIG(*) FROM sys.tables WHERE object_id IN ("
        + objects
        + ") AND is_tracked_by_cdc=1)<>"
        + str(count)
        + " THROW 51974, 'Unexpected fixed tracked-table count.', 1;\n"
    )


def successful_permissions():
    """Check three narrow memberships and reject broad roles."""
    return """IF (SELECT COUNT_BIG(*) FROM sys.database_role_members m
JOIN sys.database_principals r ON m.role_principal_id=r.principal_id
JOIN sys.database_principals u ON m.member_principal_id=u.principal_id
WHERE (r.name COLLATE Latin1_General_100_BIN2=N'lh_demo_writer'
       AND u.name COLLATE Latin1_General_100_BIN2=N'fixture_writer')
   OR (r.name COLLATE Latin1_General_100_BIN2=N'lh_demo_reader'
       AND u.name COLLATE Latin1_General_100_BIN2=N'fixture_reader')
   OR (r.name COLLATE Latin1_General_100_BIN2=N'lh_demo_control_writer'
       AND u.name COLLATE Latin1_General_100_BIN2=N'fixture_control'))<>3
    THROW 51975, 'Expected three narrow role memberships.', 1;
IF EXISTS (SELECT 1 FROM sys.database_role_members m
JOIN sys.database_principals r ON m.role_principal_id=r.principal_id
WHERE m.member_principal_id IN (USER_ID(N'fixture_writer'),
    USER_ID(N'fixture_reader'),USER_ID(N'fixture_control'))
  AND r.name IN (N'db_owner',N'db_datawriter',N'db_ddladmin',
                      N'db_securityadmin'))
    THROW 51976, 'A synthetic runtime user gained a broad role.', 1;
"""


def prepare_legacy_cdc():
    """Prepare CDC only inside a freshly marker-guarded synthetic database."""
    return """EXEC sys.sp_cdc_enable_db;
EXEC sys.sp_cdc_enable_table @source_schema=N'dbo',
    @source_name=N'fixture_legacy_data',
    @capture_instance=N'fixture_legacy_data_v1',@role_name=NULL,
    @supports_net_changes=0;
EXEC(N'CREATE SCHEMA fixture_ctl AUTHORIZATION dbo');
CREATE TABLE fixture_ctl.legacy_identity (
    role_id int NOT NULL,user_id int NOT NULL,object_id int NOT NULL,
    capture_object_id int NOT NULL,capture_job_id uniqueidentifier NOT NULL,
    cleanup_job_id uniqueidentifier NOT NULL);
INSERT fixture_ctl.legacy_identity
SELECT DATABASE_PRINCIPAL_ID(N'fixture_legacy_reader'),
    USER_ID(N'fixture_legacy_user'),OBJECT_ID(N'dbo.fixture_legacy_data'),
    (SELECT object_id FROM cdc.change_tables WHERE capture_instance
        COLLATE Latin1_General_100_BIN2=N'fixture_legacy_data_v1'),
    (SELECT job_id FROM msdb.dbo.cdc_jobs WHERE database_id=DB_ID()
        AND job_type=N'capture'),
    (SELECT job_id FROM msdb.dbo.cdc_jobs WHERE database_id=DB_ID()
        AND job_type=N'cleanup');
SELECT p.class,p.major_id,p.minor_id,p.permission_name,p.state
INTO fixture_ctl.legacy_permissions FROM sys.database_permissions p
WHERE p.grantee_principal_id=DATABASE_PRINCIPAL_ID(N'fixture_legacy_reader');
"""


def preserve_legacy():
    """Check identity, grants and the existing shared CDC job IDs both ways."""
    return """IF NOT EXISTS (SELECT 1 FROM fixture_ctl.legacy_identity saved
WHERE saved.role_id=DATABASE_PRINCIPAL_ID(N'fixture_legacy_reader')
  AND saved.user_id=USER_ID(N'fixture_legacy_user')
  AND saved.object_id=OBJECT_ID(N'dbo.fixture_legacy_data')
  AND saved.capture_object_id=(SELECT object_id FROM cdc.change_tables
      WHERE capture_instance COLLATE Latin1_General_100_BIN2
          =N'fixture_legacy_data_v1')
  AND saved.capture_job_id=(SELECT job_id FROM msdb.dbo.cdc_jobs
      WHERE database_id=DB_ID() AND job_type=N'capture')
  AND saved.cleanup_job_id=(SELECT job_id FROM msdb.dbo.cdc_jobs
      WHERE database_id=DB_ID() AND job_type=N'cleanup'))
    THROW 51977, 'Legacy fixture identity or shared job ID changed.', 1;
IF NOT EXISTS (SELECT 1 FROM sys.database_role_members
    WHERE role_principal_id=DATABASE_PRINCIPAL_ID(N'fixture_legacy_reader')
      AND member_principal_id=USER_ID(N'fixture_legacy_user'))
    THROW 51978, 'Legacy fixture role membership changed.', 1;
IF EXISTS (
 SELECT class,major_id,minor_id,
    permission_name COLLATE Latin1_General_100_BIN2,
    state COLLATE Latin1_General_100_BIN2 FROM fixture_ctl.legacy_permissions
 EXCEPT SELECT class,major_id,minor_id,
    permission_name COLLATE Latin1_General_100_BIN2,
    state COLLATE Latin1_General_100_BIN2 FROM sys.database_permissions
 WHERE grantee_principal_id=DATABASE_PRINCIPAL_ID(N'fixture_legacy_reader')
) OR EXISTS (
 SELECT class,major_id,minor_id,
    permission_name COLLATE Latin1_General_100_BIN2,
    state COLLATE Latin1_General_100_BIN2 FROM sys.database_permissions
 WHERE grantee_principal_id=DATABASE_PRINCIPAL_ID(N'fixture_legacy_reader')
 EXCEPT SELECT class,major_id,minor_id,
    permission_name COLLATE Latin1_General_100_BIN2,
    state COLLATE Latin1_General_100_BIN2 FROM fixture_ctl.legacy_permissions
)
    THROW 51979, 'Legacy fixture permissions changed.', 1;
"""


def role_alias_identity():
    """Observe actual saved spelling and native lookup, not a CI simulation."""
    return """DECLARE @SavedRole sysname,@SavedRoleId int,
 @FixedRoleId int=DATABASE_PRINCIPAL_ID(N'lh_demo_cdc_reader'),
 @AliasCapture int,@SamePrincipal bit,@SavedCaseDifferent bit;
SELECT @SavedRole=role_name,@AliasCapture=object_id FROM cdc.change_tables
WHERE capture_instance COLLATE Latin1_General_100_BIN2
    =N'fixture_role_alias_v1' COLLATE Latin1_General_100_BIN2;
SET @SavedRoleId=DATABASE_PRINCIPAL_ID(@SavedRole);
IF @AliasCapture IS NULL OR @SavedRole IS NULL OR @SavedRoleId IS NULL
 OR @FixedRoleId IS NULL
 OR DATABASE_PRINCIPAL_ID(N'LH_DEMO_CDC_READER') IS NULL
 OR @SavedRoleId<>DATABASE_PRINCIPAL_ID(N'LH_DEMO_CDC_READER')
 THROW 51984, 'Synthetic CDC role identity could not be established.', 1;
SET @SamePrincipal=CONVERT(bit,CASE
 WHEN @SavedRoleId=@FixedRoleId THEN 1 ELSE 0 END);
SET @SavedCaseDifferent=CONVERT(bit,CASE
 WHEN @SavedRole COLLATE Latin1_General_100_BIN2
     <>N'lh_demo_cdc_reader' COLLATE Latin1_General_100_BIN2
 THEN 1 ELSE 0 END);
"""


def role_alias_probe(source, number):
    """Assert reject-only identity discovery and preserve unrelated metadata.

    Same-principal sharing must raise 51115 before any mutation. When the
    catalog resolves a genuinely different principal, normal fixed lifecycle
    remains possible. The saved alias capture itself is always preserved.
    """
    if number not in {"004", "005"}:
        raise FixtureError("FIXED_ROLE_ALIAS_STAGE_REQUIRED")
    queries = {
        # SQL Agent can advance start/end LSNs or drop-pending progress while
        # these scripts are correctly refusing a change. Compare stable
        # identity/definition only; do not confuse progress with DDL mutation.
        "Captures": (
            "SELECT object_id,source_object_id,capture_instance,role_name,"
            "index_name,supports_net_changes FROM cdc.change_tables "
            "ORDER BY object_id"
        ),
        "Roles": (
            "SELECT principal_id,name,type,owning_principal_id "
            "FROM sys.database_principals ORDER BY principal_id"
        ),
        "Members": (
            "SELECT role_principal_id,member_principal_id "
            "FROM sys.database_role_members "
            "ORDER BY role_principal_id,member_principal_id"
        ),
        "Grants": (
            "SELECT class,major_id,minor_id,grantee_principal_id,"
            "grantor_principal_id,permission_name,state "
            "FROM sys.database_permissions "
            "ORDER BY class,major_id,minor_id,grantee_principal_id,"
            "permission_name"
        ),
    }
    hashes = {
        name: "HASHBYTES('SHA2_256',CONVERT(nvarchar(max),("
        + query
        + " FOR JSON PATH,INCLUDE_NULL_VALUES)))"
        for name, query in queries.items()
    }
    before = "".join(
        "DECLARE @Before" + name + " varbinary(32)=" + digest + ";\n"
        for name, digest in hashes.items()
    )
    unchanged = "\n OR ".join(
        "@Before" + name + "<>" + digest for name, digest in hashes.items()
    )
    return (
        role_alias_identity()
        + "IF @SamePrincipal=1\nBEGIN\n"
        + before
        + expect_failure(source, 51115, "role_alias_" + number + "_rejected")
        + "IF "
        + unchanged
        + "\n"
        + " THROW 51984, 'Rejected role sharing changed metadata.', 1;\n"
        + assert_fixed_captures(6)
        + record("role_alias_" + number + "_preserved")
        + "END\nELSE\nBEGIN\n"
        + "EXEC sys.sp_executesql "
        + literal(source)
        + ";\n"
        + assert_fixed_captures(6 if number == "004" else 0)
        + record("distinct_role_" + number + "_passed")
        + "END;\n"
        + """IF NOT EXISTS (SELECT 1 FROM cdc.change_tables
 WHERE object_id=@AliasCapture
 AND source_object_id=OBJECT_ID(N'dbo.fixture_role_alias_data')
 AND capture_instance COLLATE Latin1_General_100_BIN2
     =N'fixture_role_alias_v1' COLLATE Latin1_General_100_BIN2
 AND role_name COLLATE Latin1_General_100_BIN2
     =@SavedRole COLLATE Latin1_General_100_BIN2
 AND DATABASE_PRINCIPAL_ID(role_name)=@SavedRoleId)
 THROW 51984, 'Unrelated synthetic role-alias capture changed.', 1;
"""
        + preserve_legacy()
    )


def role_alias_coverage(records):
    """Do not call canonicalized spelling an exercised saved-alias case."""
    observations = [
        row for row in records if row["stage"] == "role_alias_metadata"
    ]
    return {
        "same_principal_saved_alias_exercised": any(
            row["same_principal"] and row["saved_case_different"]
            for row in observations
        ),
        "distinct_principal_exercised": any(
            not row["same_principal"] for row in observations
        ),
        "observation_count": len(observations),
    }


def replacement(selected):
    """Render public maintenance with four generic approved triples."""
    with_path = str(ROOT / "scripts")
    sys.path.insert(0, with_path)
    try:
        spec = importlib.util.spec_from_file_location(
            "fixture_prepare", ROOT / "scripts/prepare-sqlserver.py"
        )
        prepare = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prepare)
    finally:
        sys.path.remove(with_path)
    nonce = selected["InstallationId"]
    return prepare.replacement_script(
        {
            "DEMO_SQLSERVER_DATABASE": selected["TargetDatabase"],
            "DEMO_SQLSERVER_INSTALLATION_ID": nonce,
            "DEMO_SQLSERVER_OWNERSHIP_TOKEN": nonce * 2,
            "DEMO_SQLSERVER_USER": "fixture_writer",
            "SOURCE_SQLSERVER_USER": "fixture_reader",
            "SOURCE_SQLSERVER_CONTROL_USER": "fixture_control",
        },
        selected["RepositoryVersion"],
        [
            {
                "source_schema": "dbo",
                "source_table": "fixture_old_" + str(n),
                "capture_instance": "fixture_old_" + str(n) + "_v1",
            }
            for n in range(1, 5)
        ],
    )


def initialization_snapshot(variable, *, retained_only=False):
    """Hash actual metadata/history/offsets; never write CDC system metadata.

    Capture jobs are stopped before these comparisons, so start LSN and
    history must stay byte-for-byte stable. Partial enable compares only the
    three retained capture definitions and history, then checks all six.
    """
    if variable not in {"Before", "After"}:
        raise FixtureError("FIXED_SNAPSHOT_VARIABLE_REQUIRED")
    where = ""
    names = EXPECTED_CAPTURES
    if retained_only:
        names = EXPECTED_CAPTURES[:3]
        where = " WHERE capture_instance COLLATE Latin1_General_100_BIN2 IN ("
        where += ",".join(literal(name) for name in names) + ")"
    queries = [
        "SELECT * FROM cdc.change_tables" + where + " ORDER BY object_id",
        "SELECT cc.* FROM cdc.captured_columns cc JOIN cdc.change_tables ct "
        "ON cc.object_id=ct.object_id"
        + where
        + " ORDER BY cc.object_id,cc.column_ordinal",
    ]
    if not retained_only:
        queries += [
            "SELECT object_id,name,type,schema_id,principal_id FROM "
            "sys.objects ORDER BY object_id",
            "SELECT principal_id,name,type,owning_principal_id FROM "
            "sys.database_principals ORDER BY principal_id",
            "SELECT * FROM sys.database_role_members "
            "ORDER BY role_principal_id,member_principal_id",
            "SELECT * FROM sys.database_permissions "
            "ORDER BY class,major_id,minor_id,grantee_principal_id,"
            "grantor_principal_id,permission_name",
            "SELECT * FROM fixture_ctl.offset_sentinel ORDER BY id",
            "SELECT * FROM lh_demo_ctl.batch_watermarks ORDER BY source_table",
        ]
    job_queries = (
        [
            "SELECT * FROM msdb.dbo.cdc_jobs WHERE database_id=DB_ID() "
            "ORDER BY job_type",
            "SELECT j.* FROM msdb.dbo.sysjobs j JOIN msdb.dbo.cdc_jobs c "
            "ON j.job_id=c.job_id WHERE c.database_id=DB_ID() "
            "ORDER BY j.job_id",
            "SELECT s.* FROM msdb.dbo.sysjobsteps s JOIN msdb.dbo.cdc_jobs c "
            "ON s.job_id=c.job_id WHERE c.database_id=DB_ID() "
            "ORDER BY s.job_id,s.step_id",
        ]
        if not retained_only
        else []
    )
    target = "@Init" + variable
    sql = (
        "DECLARE "
        + target
        + " nvarchar(max)="
        + "+".join(
            "(" + query + " FOR JSON PATH,INCLUDE_NULL_VALUES)"
            for query in queries
        )
        + ";\n"
    )
    for query in job_queries:
        statement = "SELECT @h=@h+(" + query
        statement += " FOR JSON PATH,INCLUDE_NULL_VALUES);"
        sql += (
            "IF OBJECT_ID(N'msdb.dbo.cdc_jobs',N'U') IS NOT NULL\n"
            " EXEC sys.sp_executesql "
            + literal(statement)
            + ",N'@h nvarchar(max) OUTPUT',@h="
            + target
            + " OUTPUT;\n"
        )
    for name in names:
        query = (
            "SELECT @h=@h+(SELECT * FROM cdc.[" + name + "_CT] "
            "ORDER BY __$start_lsn,__$seqval,__$operation "
            "FOR JSON PATH,INCLUDE_NULL_VALUES);"
        )
        sql += (
            "IF OBJECT_ID(N'cdc." + name + "_CT',N'U') IS NOT NULL\n"
            " EXEC sys.sp_executesql "
            + literal(query)
            + ",N'@h nvarchar(max) OUTPUT',@h="
            + target
            + " OUTPUT;\n"
        )
    return sql


def initialization_unchanged(source, *, retained_only=False):
    """Assert a rejected call or exact rerun preserved the observed state."""
    return (
        initialization_snapshot("Before", retained_only=retained_only)
        + "EXEC sys.sp_executesql "
        + literal(source)
        + ";\n"
        + initialization_snapshot("After", retained_only=retained_only)
        + "IF HASHBYTES('SHA2_256',@InitBefore)"
        "<>HASHBYTES('SHA2_256',@InitAfter) "
        "THROW 51996, 'CDC initialization changed retained state.', 1;\n"
        + record("init_state_preserved")
    )


def stop_fixture_jobs():
    """Freeze only this fresh database's jobs for history/LSN comparisons."""
    sql = """
-- This fixture owns the fresh jobs. Disable scheduling before the
-- manual scan; the production scripts never change job enabled state.
DECLARE @FixtureJob uniqueidentifier;
DECLARE FixtureJobs CURSOR LOCAL FAST_FORWARD FOR
 SELECT j.job_id FROM msdb.dbo.cdc_jobs j JOIN msdb.dbo.sysjobs sj
 ON sj.job_id=j.job_id WHERE j.database_id=DB_ID() AND sj.enabled=1;
OPEN FixtureJobs;
FETCH NEXT FROM FixtureJobs INTO @FixtureJob;
WHILE @@FETCH_STATUS=0
BEGIN
 EXEC msdb.dbo.sp_update_job @job_id=@FixtureJob,@enabled=0;
 FETCH NEXT FROM FixtureJobs INTO @FixtureJob;
END;
CLOSE FixtureJobs;
DEALLOCATE FixtureJobs;
DECLARE @StopDeadline datetime2=DATEADD(second,30,SYSUTCDATETIME()),
 @QuietDeadline datetime2=DATEADD(second,2,SYSUTCDATETIME());
WHILE SYSUTCDATETIME()<@QuietDeadline
BEGIN
 IF SYSUTCDATETIME()>@StopDeadline
  THROW 51995, 'Synthetic CDC jobs did not stop.', 1;
"""
    for job_type in ("capture", "cleanup"):
        sql += (
            """ IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobactivity a
 JOIN msdb.dbo.cdc_jobs j ON a.job_id=j.job_id
 WHERE j.database_id=DB_ID() AND j.job_type=N'"""
            + job_type
            + """'
 AND a.run_requested_date IS NOT NULL AND a.stop_execution_date IS NULL
 AND a.session_id=(SELECT MAX(session_id) FROM msdb.dbo.syssessions))
 BEGIN
  SET @QuietDeadline=DATEADD(second,2,SYSUTCDATETIME());
  BEGIN TRY
   EXEC sys.sp_cdc_stop_job @job_type=N'"""
            + job_type
            + """';
  END TRY
  BEGIN CATCH
   -- A queued job can finish or start between observation and stop. Retry
   -- only SQL Agent's not-running race; every other error still fails.
   IF ERROR_NUMBER()<>22022 THROW;
  END CATCH;
 END;
"""
        )
    sql += " WAITFOR DELAY '00:00:00.200';\nEND;\n"
    return (
        "IF OBJECT_ID(N'msdb.dbo.cdc_jobs',N'U') IS NOT NULL\n"
        " EXEC sys.sp_executesql " + literal(sql) + ";\n"
    )


class Runner:
    """Execute only explicit synthetic stages; never forward raw SQL output."""

    def __init__(self, server, sqlcmd, password, nonce, version):
        self.server = endpoint(server)
        self.sqlcmd = str(sqlcmd)
        self.password = password
        self.nonce = nonce
        self.version = version
        self.records = []
        self.case = "initial"
        self.candidate_databases = []
        variables("lh_collation_" + nonce[:16] + "_probe", nonce, version)
        self.databases_created = 0
        self.failure = None

    def execute(
        self, database, source, *, guarded=True, allowed_sql_errors=()
    ):
        if guarded:
            source = guard(database, self.nonce) + source
        # Synthetic fixture DBA credentials exist only in this client process.
        # They never enter application runtime/site.env/argv/source or logs.
        environment = client_environment(self.password)
        with tempfile.TemporaryDirectory(
            prefix="lh-collation-input-"
        ) as temporary:
            path = Path(temporary) / "synthetic.sql"
            descriptor = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(
                    source + "\n" + record("batch_completed") + "GO\n"
                )
            completed = subprocess.run(
                [
                    self.sqlcmd,
                    "-S",
                    self.server,
                    "-d",
                    database,
                    "-U",
                    "sa",
                    "-N",
                    "mandatory",
                    "-C",
                    "-b",
                    # Let T-SQL TRY/CATCH finish expected severity-16 tests.
                    # Receipt and unexpected-error checks below remain strict.
                    "-V",
                    "17",
                    "-l",
                    "10",
                    "-t",
                    "120",
                    "-i",
                    str(path),
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=150,
                check=False,
            )
        output_text = re.sub(
            r"\x1b\[[0-?]*[ -/]*[@-~]",
            "",
            completed.stdout.decode("utf-8", errors="strict"),
        )
        if completed.returncode or len(completed.stdout) > 2 * 1024**2:
            self.failure = {
                "case": self.case,
                "batch_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "error_numbers": sorted(
                    {
                        int(number)
                        for number in re.findall(
                            r"Msg\s+([0-9]{1,6}),",
                            output_text[:65536],
                        )
                    }
                ),
                "known_error_numbers": sorted(
                    {
                        int(number)
                        for number in re.findall(
                            r"\b(5[12][0-9]{3})\b", output_text[:65536]
                        )
                    }
                ),
            }
            raise FixtureError("SYNTHETIC_SQLCMD_STAGE_FAILED")
        found = []
        for line in output_text.splitlines():
            line = line.lstrip("\ufeff").strip()
            # Go sqlcmd may decorate information messages. Parse only a
            # complete bounded receipt; never forward its surrounding output.
            position = line.find(PREFIX)
            if position < 0:
                continue
            prefix_end = position + len(PREFIX)
            value = json.loads(line[prefix_end:])
            if (
                not isinstance(value, dict)
                or value.get("stage") not in FIXED_STAGES
                or value.get("status") not in {"passed", "observed"}
            ):
                raise FixtureError("SYNTHETIC_RESULT_INVALID")
            allowed = {
                "stage",
                "status",
                "transaction_state",
                "transaction_count",
                "checker_present",
                "fixed_role_count",
                "fixed_permission_count",
                "expected_error",
                "observed_468",
                "forced_column_collations",
                "case_alias_detected",
                "same_principal",
                "saved_case_different",
                "drop_pending_one_observed",
            }
            if set(value) - allowed or any(
                type(v) not in {int, bool} or not -1 <= int(v) <= 1000000
                for key, v in value.items()
                if key not in {"stage", "status"}
            ):
                raise FixtureError("SYNTHETIC_RESULT_INVALID")
            if value["stage"] == "role_alias_metadata" and (
                set(value)
                != {
                    "stage",
                    "status",
                    "same_principal",
                    "saved_case_different",
                }
                or value["status"] != "observed"
                or type(value["same_principal"]) is not bool
                or type(value["saved_case_different"]) is not bool
            ):
                raise FixtureError("SYNTHETIC_RESULT_INVALID")
            if value["stage"] == "init_pending_drop_observation" and (
                set(value) != {"stage", "status", "drop_pending_one_observed"}
                or value["status"] != "observed"
                or type(value["drop_pending_one_observed"]) is not bool
            ):
                raise FixtureError("SYNTHETIC_RESULT_INVALID")
            value["case"] = self.case
            found.append(value)
        if not found:
            self.failure = {
                "case": self.case,
                "batch_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "output_bytes": len(completed.stdout),
                "prefix_count": output_text.count(PREFIX),
                "error_numbers": sorted(
                    {
                        int(number)
                        for number in re.findall(r"Msg\s+(\d+),", output_text)
                    }
                ),
            }
            raise FixtureError("SYNTHETIC_STAGE_EVIDENCE_MISSING")
        if found[-1]["stage"] != "batch_completed":
            self.failure = {
                "case": self.case,
                "error_numbers": sorted(
                    {
                        int(number)
                        for number in re.findall(r"Msg\s+(\d+),", output_text)
                    }
                ),
            }
            raise FixtureError("FINAL_BATCH_EVIDENCE_MISSING")
        found = found[:-1]
        expected_errors = set(allowed_sql_errors) | {
            row["expected_error"] for row in found if "expected_error" in row
        }
        actual_errors = {
            int(number)
            for number, severity in re.findall(
                r"Msg\s+(\d+),\s+Level\s+(\d+)", output_text
            )
            if int(severity) >= 11
        }
        if actual_errors - expected_errors:
            self.failure = {
                "case": self.case,
                "error_numbers": sorted(actual_errors - expected_errors),
            }
            raise FixtureError("UNEXPECTED_SQL_ERROR_WITH_RECEIPT")
        self.records.extend(found)
        return found

    def fresh(self, suffix, collation):
        self.case = suffix
        database = "lh_collation_" + self.nonce[:16] + "_" + suffix
        self.candidate_databases.append(database)
        self.execute(
            "master",
            create_database(database, self.nonce, collation),
            guarded=False,
        )
        self.databases_created += 1
        selected = variables(database, self.nonce, self.version)
        for number in ("001", "002"):
            self.execute(
                database,
                render(number, selected) + record("ddl_" + number + "_passed"),
            )
        return database, selected

    def lifecycle(self, suffix, collation):
        database, selected = self.fresh(suffix, collation)
        self.execute(
            database, prepare_legacy_cdc() + record("legacy_prepared")
        )
        self.outer_transaction_guards(database, selected)
        self.execute(database, (HERE / "mixed_collation.sql").read_text())
        permissions = render("003", selected)
        self.execute(
            database,
            expect_failure(
                inject(permissions, "checker"),
                51990,
                "checker_creation_fault",
                inspect_residual=True,
            ),
        )
        self.execute(
            database,
            expect_failure(
                inject(permissions, "grant"),
                51994,
                "permission_grant_fault",
                inspect_residual=True,
            ),
        )
        # The observation records above establish actual residuals. The test
        # does not infer rollback from XACT_ABORT or a successful parser.
        self.execute(
            database,
            permissions
            + successful_permissions()
            + preserve_legacy()
            + record("ddl_003_retry_passed"),
        )
        self.execute(
            database,
            permissions + preserve_legacy() + record("ddl_003_idempotent"),
        )
        self.validation(database, selected, 0)
        enabled = render("004", selected)
        self.execute(
            database,
            expect_failure(inject(enabled, "enable"), 51991, "partial_enable")
            + assert_fixed_captures(1)
            + preserve_legacy()
            + record("one_fixed_capture_retained"),
        )
        self.execute(
            database,
            enabled
            + assert_fixed_captures(6)
            + preserve_legacy()
            + record("enable_resume"),
        )
        self.execute(
            database,
            enabled
            + assert_fixed_captures(6)
            + preserve_legacy()
            + record("enable_idempotent"),
        )
        self.validation(database, selected, 6)
        self.execute(
            database,
            expect_failure(
                render("005", selected),
                51123,
                "disable_without_consent_refused",
            )
            + assert_fixed_captures(6)
            + preserve_legacy()
            + record("disable_refusal_preserved_captures"),
        )
        disabled = render("005", selected, confirm=True)
        self.execute(
            database,
            expect_failure(
                inject(disabled, "disable"), 51993, "partial_disable"
            )
            + assert_fixed_captures(5)
            + preserve_legacy()
            + record("five_fixed_captures_remain"),
        )
        self.execute(
            database,
            disabled
            + assert_fixed_captures(0)
            + preserve_legacy()
            + record("disable_resume"),
        )
        self.execute(
            database,
            disabled
            + assert_fixed_captures(0)
            + preserve_legacy()
            + record("disable_idempotent"),
        )
        self.validation(database, selected, 0)
        self.corrupt_capture_preflight(database, selected)

    def freeze_jobs(self, database):
        """Freeze owned jobs and tolerate only the idle SQL Agent stop race.

        The SQL loop must finish a quiet interval and emit its receipt. Job
        definitions are snapshotted after this explicit fixture preparation.
        Production 004/005 never call sp_update_job or stop shared jobs.
        """
        self.execute(
            database,
            stop_fixture_jobs() + record("init_prepared"),
            allowed_sql_errors=(22022,),
        )
        self.failure = None

    def initialization(self, suffix, collation):
        """Run each initialization case in a fresh owned SQL2022 database."""
        for case in plan()["initialization_cases"]:
            database, selected = self.fresh(case + "_" + suffix, collation)
            enabled = render("004", selected)
            self.execute(
                database,
                "EXEC sys.sp_cdc_enable_db;\n"
                "EXEC(N'CREATE SCHEMA fixture_ctl AUTHORIZATION dbo');\n"
                "CREATE TABLE fixture_ctl.offset_sentinel "
                "(id int PRIMARY KEY,offset_value bigint);\n"
                "INSERT fixture_ctl.offset_sentinel VALUES (1,123);\n"
                + render("003", selected)
                + record("init_prepared"),
            )
            self.execute(database, (HERE / "mixed_collation.sql").read_text())
            if case == "zero_no_jobs":
                setup = """IF EXISTS (SELECT 1 FROM cdc.change_tables)
 THROW 51995, 'Expected zero captures.', 1;
IF OBJECT_ID(N'msdb.dbo.cdc_jobs',N'U') IS NOT NULL
 EXEC sys.sp_executesql N'IF EXISTS (SELECT 1 FROM msdb.dbo.cdc_jobs
 WHERE database_id=DB_ID()) THROW 51995, ''Expected zero jobs.'', 1;';
"""
            elif case == "zero_with_jobs":
                setup = """EXEC sys.sp_cdc_add_job @job_type=N'capture';
EXEC sys.sp_cdc_add_job @job_type=N'cleanup';
"""
            elif case == "partial_three":
                # Fault after the third successful call, using only supported
                # CDC procedures; never patch a metadata value.
                fault = inject(enabled, "enable").replace(
                    "THROW 51991, 'Synthetic injected failure.', 1;",
                    "IF @Capture=N'lh_demo_erp_sales_orders_v1' "
                    "THROW 51991, 'Synthetic injected failure.', 1;",
                )
                setup = expect_failure(fault, 51991, "partial_enable")
                setup += assert_fixed_captures(3)
            elif case == "wrong_definition":
                setup = """EXEC sys.sp_cdc_enable_table
 @source_schema=N'lh_demo_erp',@source_name=N'products',
 @capture_instance=N'lh_demo_erp_products_v1',
 @role_name=N'lh_demo_cdc_reader',@supports_net_changes=0,
 @captured_column_list=N'[product_id]';
"""
            elif case in {"replace_four", "unknown_capture"}:
                setup = ""
                for n in range(1, 5):
                    table = "fixture_old_" + str(n)
                    setup += (
                        "CREATE TABLE dbo.["
                        + table
                        + "] (id int NOT NULL PRIMARY KEY);\n"
                        "EXEC sys.sp_cdc_enable_table @source_schema=N'dbo',"
                        "@source_name=N'"
                        + table
                        + "',@capture_instance=N'"
                        + table
                        + "_v1',@role_name=NULL,"
                        "@supports_net_changes=0;\n"
                    )
                if case == "unknown_capture":
                    setup += """EXEC sys.sp_cdc_enable_table
 @source_schema=N'dbo',@source_name=N'fixture_legacy_data',
 @capture_instance=N'fixture_unknown_v1',@role_name=NULL,
 @supports_net_changes=0;
"""
            elif case == "partitioned":
                setup = """CREATE PARTITION FUNCTION fixture_pf(nvarchar(160))
 AS RANGE RIGHT FOR VALUES (N'm');
CREATE PARTITION SCHEME fixture_ps AS PARTITION fixture_pf ALL TO ([PRIMARY]);
CREATE UNIQUE CLUSTERED INDEX PK_lh_demo_erp_products
 ON lh_demo_erp.products(product_id) WITH (DROP_EXISTING=ON)
 ON fixture_ps(product_id);
"""
            else:
                setup = """EXEC sys.sp_cdc_enable_table
 @source_schema=N'dbo',@source_name=N'fixture_legacy_data',
 @capture_instance=N'fixture_independent_v1',@role_name=NULL,
 @supports_net_changes=0;
IF NOT EXISTS (SELECT 1 FROM cdc.change_tables
 WHERE capture_instance=N'fixture_independent_v1' AND has_drop_pending IS NULL)
 THROW 51995, 'Expected real SQL2022 NULL drop-pending metadata.', 1;
"""
            self.execute(database, setup + record("init_prepared"))
            self.execute(
                database,
                "CREATE TABLE fixture_ctl.initial_jobs ("
                "job_id uniqueidentifier,job_type nvarchar(20),maxtrans int,"
                "maxscans int,continuous bit,pollinginterval bigint,"
                "retention bigint,threshold bigint);\n"
                "IF OBJECT_ID(N'msdb.dbo.cdc_jobs',N'U') IS NOT NULL "
                "EXEC sys.sp_executesql N'INSERT fixture_ctl.initial_jobs "
                "SELECT job_id,job_type,maxtrans,maxscans,continuous,"
                "pollinginterval,retention,threshold FROM msdb.dbo.cdc_jobs "
                "WHERE database_id=DB_ID();';\n"
                "SELECT * INTO fixture_ctl.independent_saved "
                "FROM cdc.change_tables WHERE capture_instance="
                "N'fixture_independent_v1';\n" + record("init_prepared"),
            )
            if case == "zero_no_jobs":
                self.execute(
                    database,
                    initialization_unchanged(
                        render("005", selected, confirm=True)
                    ),
                )
                self.execute(database, enabled + record("init_fixed_six"))
            self.freeze_jobs(database)
            self.execute(
                database,
                "DELETE FROM fixture_ctl.independent_saved;\n"
                "INSERT fixture_ctl.independent_saved SELECT * FROM "
                "cdc.change_tables WHERE capture_instance="
                "N'fixture_independent_v1';\n" + record("init_prepared"),
            )
            if case in {"wrong_definition", "unknown_capture", "partitioned"}:
                number = {
                    "wrong_definition": 51113,
                    "unknown_capture": 51204,
                    "partitioned": 51110,
                }[case]
                source = (
                    replacement(selected)
                    if case == "unknown_capture"
                    else enabled
                )
                failure = expect_failure(source, number, "init_prepared")
                self.execute(database, initialization_unchanged(failure))
            elif case == "dropped_source":
                self.execute(
                    database,
                    "DROP TABLE dbo.fixture_legacy_data;\n"
                    "IF NOT EXISTS (SELECT 1 FROM cdc.change_tables ct "
                    "LEFT JOIN sys.tables src "
                    "ON src.object_id=ct.source_object_id "
                    "WHERE src.object_id IS NULL) "
                    "THROW 51995, 'Expected retained orphan metadata.', 1;\n"
                    + record("init_prepared"),
                )
                failure = expect_failure(enabled, 51130, "init_prepared")
                self.execute(database, initialization_unchanged(failure))
                disabled_failure = expect_failure(
                    render("005", selected, confirm=True),
                    51130,
                    "init_prepared",
                )
                self.execute(
                    database, initialization_unchanged(disabled_failure)
                )
                # Observe the real state before/after supported scanning.
                # Some builds remove metadata without an observable pending=1.
                # Record a blocking coverage gap; never edit a system table.
                observations = self.execute(
                    database,
                    "IF NOT EXISTS (SELECT 1 FROM cdc.change_tables "
                    "WHERE has_drop_pending=1)\n"
                    " EXEC sys.sp_cdc_scan @maxtrans=500,@maxscans=10,"
                    "@continuous=0;\n"
                    "DECLARE @PendingObserved bit=CASE WHEN EXISTS "
                    "(SELECT 1 FROM cdc.change_tables "
                    "WHERE has_drop_pending=1) "
                    "THEN 1 ELSE 0 END;\n"
                    "DECLARE @PendingReceipt nvarchar(4000)=(SELECT "
                    "N'init_pending_drop_observation' AS stage,"
                    "N'observed' AS status,@PendingObserved "
                    "AS drop_pending_one_observed "
                    "FOR JSON PATH,WITHOUT_ARRAY_WRAPPER);\n"
                    "PRINT N'" + PREFIX + "'+@PendingReceipt;\n",
                )
                if observations[0]["drop_pending_one_observed"]:
                    self.execute(database, initialization_unchanged(failure))
                    self.execute(
                        database, initialization_unchanged(disabled_failure)
                    )
            elif case == "replace_four":
                maintenance = replacement(selected)
                fault = maintenance.replace(
                    "IF @LegacyResult<>0 OR EXISTS",
                    "THROW 51992, 'Synthetic legacy disable failure.', 1;\n"
                    "IF @LegacyResult<>0 OR EXISTS",
                )
                self.execute(
                    database,
                    expect_failure(fault, 51992, "init_replacement_partial")
                    + assert_fixed_captures(6)
                    + "IF (SELECT COUNT(*) FROM cdc.change_tables)<>9 "
                    "THROW 51995, 'Expected six fixed and three legacy.', 1;\n"
                    + record("init_prepared"),
                )
                self.execute(database, maintenance + record("init_fixed_six"))
                self.execute(database, initialization_unchanged(maintenance))
            elif case == "partial_three":
                self.execute(
                    database,
                    initialization_unchanged(enabled, retained_only=True)
                    + assert_fixed_captures(6)
                    + record("init_three_retained"),
                )
            elif case != "zero_no_jobs":
                self.execute(database, enabled + record("init_fixed_six"))
            if case not in {
                "wrong_definition",
                "unknown_capture",
                "dropped_source",
                "partitioned",
            }:
                self.execute(
                    database,
                    """IF EXISTS (
 SELECT * FROM fixture_ctl.independent_saved
 EXCEPT SELECT * FROM cdc.change_tables)
 THROW 51995, 'Independent capture metadata changed.', 1;
"""
                    + record("init_state_preserved"),
                )
                self.freeze_jobs(database)
                # Real change rows and a non-NULL start LSN must survive an
                # exact rerun, using supported scanning while jobs are stopped.
                self.execute(
                    database,
                    """INSERT lh_demo_erp.products
 (product_id,updated_at,created_at,run_id,original_values)
 VALUES (N'fixture_history',SYSUTCDATETIME(),SYSUTCDATETIME(),
         'fixture_run1',N'{}');
EXEC sys.sp_cdc_scan @maxtrans=500,@maxscans=10,@continuous=0;
IF NOT EXISTS (SELECT 1 FROM cdc.lh_demo_erp_products_v1_CT
 WHERE product_id=N'fixture_history')
 OR EXISTS (SELECT 1 FROM cdc.change_tables WHERE start_lsn IS NULL)
 THROW 51995, 'Expected captured history and established start LSN.', 1;
"""
                    + record("init_prepared"),
                )
                self.execute(database, initialization_unchanged(enabled))
                self.execute(
                    database,
                    initialization_unchanged(render("006", selected))
                    + assert_fixed_captures(6),
                )
                self.execute(
                    database,
                    initialization_unchanged(
                        render("cdc_preflight", selected)
                    ),
                )
            self.execute(
                database,
                """IF OBJECT_ID(N'msdb.dbo.cdc_jobs',N'U') IS NOT NULL
 EXEC sys.sp_executesql N'IF EXISTS (
 SELECT job_id,job_type COLLATE Latin1_General_100_BIN2,maxtrans,maxscans,
 continuous,pollinginterval,retention,threshold FROM fixture_ctl.initial_jobs
 EXCEPT SELECT job_id,job_type COLLATE Latin1_General_100_BIN2,maxtrans,
 maxscans,continuous,pollinginterval,retention,threshold
 FROM msdb.dbo.cdc_jobs WHERE database_id=DB_ID())
 THROW 51995, ''Existing shared jobs changed.'', 1;';
"""
                + record("init_" + case + "_passed"),
            )

    def outer_transaction_guards(self, database, selected):
        """Reject four child scripts without harming the caller transaction."""
        setup = """IF OBJECT_ID(
 N'fixture_ctl.outer_transaction_sentinel',N'U') IS NOT NULL
 THROW 51987, 'Refuse to adopt an existing fixture sentinel.', 1;
CREATE TABLE fixture_ctl.outer_transaction_sentinel (
 sentinel_id int NOT NULL PRIMARY KEY);
"""
        self.execute(database, setup + record("outer_sentinel_prepared"))
        for ordinal, number in enumerate(
            ("003", "004", "005", "preflight"), 1
        ):
            stage = "outer_transaction_" + number + "_preserved"
            self.execute(
                database,
                outer_transaction_probe(
                    render(number, selected), stage, ordinal
                ),
            )

    def corrupt_capture_preflight(self, database, selected):
        """Preserve a deliberately wrong owned-test capture on refusal."""
        setup = """EXEC sys.sp_cdc_enable_table
 @source_schema=N'lh_demo_erp',@source_name=N'products',
 @capture_instance=N'lh_demo_erp_products_v1',
 @role_name=N'lh_demo_cdc_reader',@supports_net_changes=0,
 @captured_column_list=N'[product_id]',@allow_partition_switch=0;
DECLARE @BadCapture int=(SELECT object_id FROM cdc.change_tables
 WHERE capture_instance COLLATE Latin1_General_100_BIN2
     =N'lh_demo_erp_products_v1');
"""
        setup += expect_failure(
            render("004", selected), 51113, "corrupt_capture_preflight_refused"
        )
        setup += """IF @BadCapture IS NULL OR NOT EXISTS (
 SELECT 1 FROM cdc.change_tables WHERE object_id=@BadCapture
 AND capture_instance COLLATE Latin1_General_100_BIN2
     =N'lh_demo_erp_products_v1') OR (
 SELECT COUNT_BIG(*) FROM cdc.captured_columns
 WHERE object_id=@BadCapture)<>1
 THROW 51986, 'The deliberately incorrect test capture changed.', 1;
"""
        self.execute(
            database,
            setup
            + assert_fixed_captures(1)
            + preserve_legacy()
            + record("corrupt_capture_preserved"),
        )

    def validation(self, database, selected, count):
        # Hash only fixture metadata; the real 006 is read-only and unchanged.
        before = """DECLARE @Before006 varbinary(32)=HASHBYTES('SHA2_256',
 CONVERT(nvarchar(max),(SELECT object_id,name,type FROM sys.objects
 ORDER BY object_id FOR JSON PATH)));
"""
        after = """IF @Before006<>HASHBYTES('SHA2_256',CONVERT(nvarchar(max),
 (SELECT object_id,name,type FROM sys.objects
  ORDER BY object_id FOR JSON PATH)))
 THROW 51984, 'Read-only validation changed fixture object metadata.', 1;
"""
        self.execute(
            database,
            before
            + "EXEC sys.sp_executesql "
            + literal(render("006", selected))
            + ";\n"
            + after
            + assert_fixed_captures(count)
            + preserve_legacy()
            + record("validation_006_captures_" + str(count)),
        )

    def shared_role_alias(self, suffix, collation):
        """Exercise aliases in a fresh DB without editing CDC metadata."""
        database, selected = self.fresh(suffix, collation)
        self.execute(
            database,
            prepare_legacy_cdc()
            + render("003", selected)
            + render("004", selected)
            + assert_fixed_captures(6)
            + record("role_alias_baseline"),
        )
        self.execute(
            database,
            (HERE / "role_alias.sql").read_text()
            + role_alias_identity()
            + """DECLARE @Evidence nvarchar(max)=N'LAKEHOUSE_SQL_FIXTURE=' + (
 SELECT N'role_alias_metadata' AS stage,N'observed' AS status,
 @SamePrincipal AS same_principal,
 @SavedCaseDifferent AS saved_case_different
 FOR JSON PATH,WITHOUT_ARRAY_WRAPPER);
PRINT @Evidence;
""",
        )
        for number in ("004", "005"):
            self.execute(
                database,
                role_alias_probe(
                    render(number, selected, confirm=(number == "005")),
                    number,
                ),
            )

    def checker_identity(self, case):
        database, selected = self.fresh(
            "checker_" + case, "Latin1_General_CI_AS"
        )
        canonical = (ROOT / "scripts/sqlserver_safety.sql").read_text().strip()
        setup = "SET ANSI_NULLS ON; SET QUOTED_IDENTIFIER ON;\n"
        code = 51019
        if case == "own_exact":
            definition = canonical
        elif case == "unrelated":
            definition = canonical.replace("THEN 1 ELSE 0", "THEN 0 ELSE 0")
        elif case == "execution":
            definition = canonical.replace(
                "EXECUTE AS 'dbo'", "EXECUTE AS OWNER"
            )
        elif case == "quoted":
            setup = "SET ANSI_NULLS ON; SET QUOTED_IDENTIFIER OFF;\n"
            definition = canonical
        elif case == "owner":
            definition = canonical
            code = 51018
        elif case in {"effective", "version", "ownership"}:
            definition = canonical
            if case in {"version", "ownership"}:
                code = 51003
        elif case == "whitespace":
            definition = canonical.replace(
                "SET NOCOUNT ON;", "SET  NOCOUNT ON;"
            )
        else:
            raise FixtureError("FIXED_CHECKER_CASE_REQUIRED")
        setup += "EXEC sys.sp_executesql " + literal(definition) + ";\n"
        if case == "owner":
            setup += (
                "CREATE USER fixture_other_owner WITHOUT LOGIN;\n"
                "ALTER AUTHORIZATION ON SCHEMA::lh_demo_ctl "
                "TO fixture_other_owner;\n"
            )
        if case == "effective":
            setup += (
                "CREATE USER fixture_other_owner WITHOUT LOGIN;\n"
                "ALTER AUTHORIZATION ON OBJECT::"
                "lh_demo_ctl.check_write_safety_v1 TO fixture_other_owner;\n"
            )
        if case == "version":
            setup += "UPDATE lh_demo_ctl.installation SET schema_version=2;\n"
        if case == "ownership":
            setup += (
                "UPDATE lh_demo_ctl.installation SET ownership_hash="
                "REPLICATE('0',64);\n"
            )
        self.execute(database, setup + record("checker_" + case + "_prepared"))
        if case == "own_exact":
            self.execute(
                database,
                render("003", selected)
                + successful_permissions()
                + record("own_checker_reused"),
            )
            self.execute(
                database,
                render("003", selected) + record("own_checker_reused_again"),
            )
        else:
            # Record a definition hash before the rejected attempt; do not
            # output its body or overwrite/drop the unrelated procedure.
            body = """DECLARE @BeforeDefinition varbinary(32)=
 HASHBYTES('SHA2_256',
 OBJECT_DEFINITION(OBJECT_ID(N'lh_demo_ctl.check_write_safety_v1')));
"""
            body += expect_failure(
                render("003", selected), code, "checker_" + case + "_rejected"
            )
            body += """IF OBJECT_ID(
 N'lh_demo_ctl.check_write_safety_v1',N'P') IS NULL
 OR @BeforeDefinition<>HASHBYTES('SHA2_256',
 OBJECT_DEFINITION(OBJECT_ID(N'lh_demo_ctl.check_write_safety_v1')))
 THROW 51985, 'A rejected checker was changed.', 1;
"""
            self.execute(
                database, body + record("untrusted_checker_preserved")
            )


def pending_drop_coverage(records):
    """Keep unobserved real pending metadata as an explicit acceptance gap."""
    observed = [
        row
        for row in records
        if row["stage"] == "init_pending_drop_observation"
    ]
    cases = {
        row["case"] for row in observed if row["drop_pending_one_observed"]
    }
    return {
        "ci_observed": "dropped_source_ci" in cases,
        "cs_observed": "dropped_source_cs" in cases,
        "observation_count": len(observed),
    }


def plan():
    """Make limitations and exact opt-in prerequisites machine-readable."""
    return {
        "schema_version": 1,
        "kind": "sqlserver-collation-fixture",
        "status": "not_run",
        "required_server": "SQL Server 2022 Developer",
        "required_client": "official Go sqlcmd exactly 1.10.0 (version-gated)",
        "transport": "explicit -N mandatory with disposable-cert trust -C",
        "endpoint_policy": "literal TCP loopback, no DNS or remote host",
        "operator_prerequisites": [
            "pre-existing isolated disposable instance; never company/prod",
            "SA fixture authorization and running SQL Server Agent for CDC",
            "no existing synthetic database name will be adopted",
            "no Docker/host/network changes or image downloads by this tool",
        ],
        "database_collations": [
            "Latin1_General_CI_AS",
            "Latin1_General_CS_AS",
        ],
        "mixed_collation_reproduction": (
            "forced column mismatch, not measured catalog"
        ),
        "checker_cases": list(CHECKER_CASES),
        "outer_transaction_cases": ["003", "004", "005", "preflight"],
        "role_alias_cases": (
            "supported CDC creation in fresh CI/CS databases; read back saved "
            "spelling/principal IDs; canonicalization is a coverage gap"
        ),
        "ansi_nulls_off_case": (
            "not forced; SQL2022 ANSI_NULLS behavior requires observation"
        ),
        "cleanup": "none; fresh marked databases retained for owner review",
        "initialization_cases": [
            "zero_no_jobs",
            "zero_with_jobs",
            "independent_null",
            "partial_three",
            "wrong_definition",
            "replace_four",
            "unknown_capture",
            "dropped_source",
            "partitioned",
        ],
        "runtime_proof": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--plan", action="store_true")
    parser.add_argument(
        "--initialization",
        action="store_true",
        help="Run the CDC initialization matrix on fresh disposable CI/CS DBs",
    )
    parser.add_argument(
        "--password-env",
        choices=("LAKEHOUSE_FIXTURE_PASSWORD",),
        help="Read the ephemeral CI credential instead of prompting",
    )
    parser.add_argument("--consent", default="")
    parser.add_argument("--server")
    parser.add_argument("--sqlcmd", type=Path)
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps(plan()))
        return 0
    if args.consent != CONSENT:
        raise FixtureError("EXPLICIT_DISPOSABLE_SERVER_CONSENT_REQUIRED")
    server = endpoint(args.server)
    sqlcmd = args.sqlcmd or shutil.which("sqlcmd")
    if not sqlcmd:
        raise FixtureError("OFFICIAL_SQLCMD_UNAVAILABLE")
    client_version = verify_client(sqlcmd)
    initial_hashes = source_hashes()
    nonce = uuid.uuid4().hex
    version = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        .stdout.decode()
        .strip()
    )
    password = (
        os.environ.get(args.password_env, "")
        if args.password_env
        else getpass.getpass("Disposable SQL2022 SA password (not saved): ")
    )
    if not password:
        raise FixtureError("SYNTHETIC_DBA_CREDENTIAL_REQUIRED")
    runner = Runner(server, sqlcmd, password, nonce, version)
    candidates = runner.candidate_databases
    try:
        if args.initialization:
            for suffix, collation in (
                ("ci", "Latin1_General_CI_AS"),
                ("cs", "Latin1_General_CS_AS"),
            ):
                runner.initialization(suffix, collation)
            runner.lifecycle("lifecycle_ci", "Latin1_General_CI_AS")
            runner.lifecycle("lifecycle_cs", "Latin1_General_CS_AS")
        else:
            runner.lifecycle("ci", "Latin1_General_CI_AS")
            runner.lifecycle("cs", "Latin1_General_CS_AS")
            runner.shared_role_alias("role_ci", "Latin1_General_CI_AS")
            runner.shared_role_alias("role_cs", "Latin1_General_CS_AS")
            for case in plan()["checker_cases"]:
                runner.checker_identity(case)
        if source_hashes() != initial_hashes:
            raise FixtureError("PUBLIC_FIXTURE_INPUTS_CHANGED")
    except (
        FixtureError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "failed_or_partial",
                    "code": str(error)
                    if isinstance(error, FixtureError)
                    else "FIXTURE_STAGE_UNAVAILABLE",
                    "confirmed_databases_created": runner.databases_created,
                    "candidate_synthetic_databases": candidates,
                    "fixture_owner_sha256": hashlib.sha256(
                        nonce.encode()
                    ).hexdigest(),
                    "client_version": client_version,
                    "source_commit": version,
                    "source_sha256": initial_hashes,
                    "records": runner.records,
                    "failure": runner.failure,
                    "databases_may_remain": True,
                    "cleanup": "none; inspect owned synthetic databases",
                }
            )
        )
        return 1
    role_coverage = role_alias_coverage(runner.records)
    role_coverage_complete = args.initialization or all(
        role_coverage[key]
        for key in (
            "same_principal_saved_alias_exercised",
            "distinct_principal_exercised",
        )
    )
    pending_coverage = pending_drop_coverage(runner.records)
    coverage_complete = role_coverage_complete and (
        not args.initialization
        or (
            pending_coverage["ci_observed"] and pending_coverage["cs_observed"]
        )
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "suite": "cdc_initialization"
                if args.initialization
                else "collation_full",
                "pending_drop_one_coverage": pending_coverage,
                "status": (
                    "passed"
                    if coverage_complete
                    else "passed_with_coverage_gaps"
                ),
                "role_alias_coverage": role_coverage,
                "confirmed_databases_created": runner.databases_created,
                "candidate_synthetic_databases": candidates,
                "fixture_owner_sha256": hashlib.sha256(
                    nonce.encode()
                ).hexdigest(),
                "client_version": client_version,
                "source_commit": version,
                "source_sha256": initial_hashes,
                "records": runner.records,
                "cleanup": "retained_intentionally",
            }
        )
    )
    return 0 if coverage_complete else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        FixtureError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        # Fixed messages only; no exception bodies containing client output.
        code = (
            str(error)
            if isinstance(error, FixtureError)
            else "FIXTURE_UNAVAILABLE"
        )
        print(json.dumps({"status": "blocked", "code": code}))
        raise SystemExit(2) from None
