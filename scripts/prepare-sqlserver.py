#!/usr/bin/env python3
"""Write private SQLCMD variables for a DBA; never connect to a SQL server."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from settings import ROOT, load


def prepared_values(values, version):
    """Validate every substituted literal before creating a private script.

    SQLCMD performs textual substitution. Reject quotes/newlines and narrow
    names instead of trying to escape arbitrary SQL or accept script input.
    The ownership token itself is never written; only its SHA-256 is used.
    """
    selected = {
        "TargetDatabase": values.get("DEMO_SQLSERVER_DATABASE", ""),
        "InstallationId": values.get("DEMO_SQLSERVER_INSTALLATION_ID", ""),
        "DemoWriterUser": values.get("DEMO_SQLSERVER_USER", ""),
        "SourceReaderUser": values.get("SOURCE_SQLSERVER_USER", ""),
        "ControlWriterUser": values.get("SOURCE_SQLSERVER_CONTROL_USER", ""),
    }
    if not all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", selected[key])
        for key in (
            "TargetDatabase",
            "DemoWriterUser",
            "SourceReaderUser",
            "ControlWriterUser",
        )
    ):
        raise ValueError("Configure approved SQL identifiers and three users.")
    if (
        len(
            {
                selected[key].casefold()
                for key in (
                    "DemoWriterUser",
                    "SourceReaderUser",
                    "ControlWriterUser",
                )
            }
        )
        != 3
    ):
        raise ValueError("Use three distinct existing database principals.")
    token = values.get("DEMO_SQLSERVER_OWNERSHIP_TOKEN", "")
    if not re.fullmatch(r"[0-9a-f]{64}", token) or not re.fullmatch(
        r"[0-9a-f]{32}", selected["InstallationId"]
    ):
        raise ValueError("Keep the generated installation identity and token.")
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("Cannot determine the reviewed repository commit.")
    selected["OwnershipHash"] = hashlib.sha256(token.encode()).hexdigest()
    selected["RepositoryVersion"] = version
    # Disabling fixed CDC is a separate DBA action; default input refuses it.
    selected["ConfirmDisableCdc"] = "0"
    return selected


def prepare(values, version):
    """Render the default private input; destructive maintenance stays off."""
    selected = prepared_values(values, version)
    return "-- Private DBA input. Not executed by this tool.\n" + "".join(
        ":setvar " + key + ' "' + value + '"\n'
        for key, value in selected.items()
    )


def replacement_script(values, version, allowlist):
    """Bundle reviewed 004 and four exact approved triples, never connect.

    The private JSON is a list of four objects with source_schema,
    source_table and capture_instance. Identifiers cannot contain wildcard,
    SQLCMD, quote or path syntax. No capture name is inferred from a prefix.
    """
    keys = {"source_schema", "source_table", "capture_instance"}
    if (
        not isinstance(allowlist, list)
        or len(allowlist) != 4
        or any(
            not isinstance(row, dict)
            or set(row) != keys
            or any(
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", value)
                for value in row.values()
            )
            or row["source_schema"].casefold().startswith(("lh_demo_", "cdc"))
            or row["capture_instance"].casefold().startswith("lh_demo_")
            or row["capture_instance"].casefold() == "all"
            for row in allowlist
        )
        or len({row["capture_instance"].casefold() for row in allowlist}) != 4
    ):
        raise ValueError("Configure exactly four distinct approved triples.")
    selected = prepared_values(values, version)
    directory = ROOT / "infra/sqlserver"
    enabled = (directory / "004_demo_cdc_enable.sql").read_text()
    for key, value in selected.items():
        enabled = enabled.replace("$(" + key + ")", value)
    source = (
        directory / "maintenance/replace_legacy_captures.sql"
    ).read_text()
    source = source.replace("$(TargetDatabase)", selected["TargetDatabase"])
    for ordinal, row in enumerate(allowlist, 1):
        for prefix, key in (
            ("LegacySchema", "source_schema"),
            ("LegacyTable", "source_table"),
            ("LegacyCapture", "capture_instance"),
        ):
            source = source.replace(
                "$(" + prefix + str(ordinal) + ")", row[key]
            )
    source = source.replace("$(FixedEnableSql)", enabled.replace("'", "''"))
    if "$(" in source:
        raise ValueError("Unresolved private maintenance input.")
    return source


def main():
    """Create one 0600 file outside Git, without displaying private values."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy-allowlist",
        type=Path,
        help="DBAが別途承認したGit外JSONの完全一致4組。指定時は置換SQLを生成",
    )
    parser.add_argument(
        "--confirm-replace-legacy",
        default="",
        help="置換SQLの生成には REPLACE_EXACT_APPROVED_FOUR を明示",
    )
    parser.add_argument(
        "--env-file",
        required=True,
        type=Path,
        help="生成済みのGit外private site.env",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Git外の新規SQLCMD変数file。既存fileは上書きしない",
    )
    args = parser.parse_args()
    path = args.output.expanduser().absolute()
    if path.resolve().is_relative_to(ROOT) or any(
        p.is_symlink() for p in (path, *path.parents)
    ):
        raise ValueError("Use a regular private output path outside Git.")
    version = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if args.legacy_allowlist:
        allow_path = args.legacy_allowlist.expanduser().absolute()
        if allow_path.resolve().is_relative_to(ROOT) or any(
            part.is_symlink() for part in (allow_path, *allow_path.parents)
        ):
            raise ValueError("Keep the approved allowlist outside Git.")
        if args.confirm_replace_legacy != "REPLACE_EXACT_APPROVED_FOUR":
            raise ValueError("Explicit legacy replacement consent required.")
        content = replacement_script(
            load(args.env_file), version, json.loads(allow_path.read_text())
        )
    else:
        if args.confirm_replace_legacy:
            raise ValueError("An explicit approved allowlist is required.")
        content = prepare(load(args.env_file), version)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("Use an output directory with mode 0700.")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
    print("Prepared private DBA variables (0600). No SQL was executed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError):
        raise SystemExit("DBA input preparation failed; values withheld.")
