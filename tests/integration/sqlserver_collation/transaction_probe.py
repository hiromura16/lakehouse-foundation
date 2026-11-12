#!/usr/bin/env python3
"""Observe unwrapped transactions only on the disposable CI loopback server.

No fixture guard, CDC operation, permanent object or metadata write is used.
Each comparison uses a new sqlcmd connection and only an optional temp table.
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

PREFIX = "TRANSACTION_PROBE="


def source(variant, option, insert):
    """Build the minimal batch and capture context immediately in CATCH."""
    if variant not in {"inline", "dynamic", "scoped"}:
        raise ValueError("Fixed probe variant required")
    if option not in {"ON", "OFF"} or type(insert) is not bool:
        raise ValueError("Fixed probe options required")
    statement = "RAISERROR('Synthetic transaction probe.',16,1);"
    if variant == "dynamic":
        statement = (
            "EXEC sys.sp_executesql N'" + statement.replace("'", "''") + "';"
        )
    elif variant == "scoped":
        statement = "EXEC sys.sp_executesql N'SET XACT_ABORT OFF; " + (
            statement.replace("'", "''") + "';"
        )
    setup = "CREATE TABLE #sentinel (id int PRIMARY KEY);\n" if insert else ""
    write = "INSERT #sentinel VALUES (1);\n" if insert else ""
    sentinel = "(SELECT COUNT(*) FROM #sentinel)" if insert else "0"
    return f"""{setup}SET XACT_ABORT {option};
BEGIN TRANSACTION;
{write}DECLARE @Options int=@@OPTIONS,@Count int=@@TRANCOUNT;
DECLARE @Number int,@State int,@Line int,@Xact int,@AfterCount int,
 @Difference int,@Sentinel int;
BEGIN TRY
 {statement}
END TRY
BEGIN CATCH
 SELECT @Number=ERROR_NUMBER(),@State=ERROR_STATE(),@Line=ERROR_LINE(),
  @Xact=XACT_STATE(),@AfterCount=@@TRANCOUNT,
  @Difference=(@@OPTIONS ^ @Options),@Sentinel={sentinel};
END CATCH;
IF XACT_STATE()<>0 ROLLBACK TRANSACTION;
DECLARE @Result nvarchar(4000)=(SELECT @Number AS error_number,
 @State AS error_state,@Line AS error_line,@Xact AS transaction_state,
 @Count AS before_count,@AfterCount AS after_count,
 @Difference AS option_difference,@Sentinel AS sentinel_count
 FOR JSON PATH,INCLUDE_NULL_VALUES,WITHOUT_ARRAY_WRAPPER);
PRINT N'{PREFIX}'+@Result;
"""


def execute(sql):
    """Run a plain batch; never forward raw client output."""
    environment = {
        "PATH": os.environ["PATH"],
        "SQLCMDPASSWORD": os.environ["LAKEHOUSE_FIXTURE_PASSWORD"],
    }
    result = subprocess.run(
        [
            "sqlcmd",
            "-S",
            "tcp:127.0.0.1,14333",
            "-d",
            "tempdb",
            "-U",
            "sa",
            "-N",
            "mandatory",
            "-C",
            "-b",
            "-V",
            "17",
            "-l",
            "10",
            "-t",
            "30",
            "-Q",
            sql,
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=45,
        check=False,
    )
    if result.returncode or len(result.stdout) > 65536:
        raise RuntimeError("UNWRAPPED_PROBE_CLIENT_FAILED")
    rows = []
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", result.stdout.decode())
    for line in output.splitlines():
        position = line.find(PREFIX)
        if position >= 0:
            prefix_end = position + len(PREFIX)
            rows.append(json.loads(line[prefix_end:]))
    if len(rows) != 1:
        raise RuntimeError("UNWRAPPED_PROBE_RECEIPT_MISSING")
    return rows[0]


def main():
    """Record version and all comparisons; scoped candidate must preserve 1."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("DISPOSABLE_CI_ONLY")
    version = execute(f"""DECLARE @Result nvarchar(4000)=(SELECT
 CONVERT(nvarchar(128),SERVERPROPERTY('ProductVersion')) AS product_version,
 CONVERT(nvarchar(128),SERVERPROPERTY('ProductLevel')) AS product_level,
 CONVERT(nvarchar(128),SERVERPROPERTY('ProductUpdateLevel')) AS update_level,
 CONVERT(nvarchar(128),SERVERPROPERTY('Edition')) AS edition,
 CONVERT(int,SERVERPROPERTY('ProductMajorVersion')) AS major_version,
 @@VERSION AS runtime_version FOR JSON PATH,WITHOUT_ARRAY_WRAPPER);
PRINT N'{PREFIX}'+@Result;""")
    if version["major_version"] != 16 or "Developer" not in version["edition"]:
        raise RuntimeError("SQL2022_DEVELOPER_REQUIRED")
    print(json.dumps({"probe_runtime": version}), flush=True)
    failed = False
    for variant in ("inline", "dynamic", "scoped"):
        for option in ("ON", "OFF"):
            for insert in (False, True):
                observation = execute(source(variant, option, insert))
                print(
                    json.dumps(
                        {
                            "variant": variant,
                            "xact_abort": option,
                            "insert": insert,
                            "observation": observation,
                        }
                    ),
                    flush=True,
                )
                healthy = (
                    observation["error_number"] == 50000
                    and observation["error_state"] == 1
                    and observation["before_count"] == 1
                    and observation["after_count"] == 1
                    and observation["option_difference"] == 0
                    and observation["sentinel_count"] == int(insert)
                )
                if variant == "scoped":
                    healthy &= observation["transaction_state"] == 1
                failed |= not healthy
    print(
        json.dumps(
            {
                "scoped_candidate_passed": not failed,
                "source_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
            }
        )
    )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
