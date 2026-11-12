#!/usr/bin/env python3
"""Production guard contract on synthetic TEMP metadata, not engine coverage.

Only the disposable SQL2022 CI endpoint is used. No CDC API or system metadata
write occurs. Passing this probe does not cover engine has_drop_pending=1.
"""

import hashlib
import itertools
import json
import os
import subprocess
from pathlib import Path

import transaction_probe
from sqlfluff.core import Linter

ROOT = Path(__file__).resolve().parents[3]
FILES = ("004_demo_cdc_enable.sql", "005_demo_cdc_disable.sql")
CASES = tuple(itertools.product((None, 0, 1), (0, 1), (0, 1), (0, 1)))


def code(segment):
    """Direct executable children only; comments cannot satisfy a contract."""
    return [s for s in segment.segments if s.is_code and not s.is_meta]


def tokens(segment):
    return tuple(s.raw for s in segment.raw_segments if s.is_code)


def require(condition):
    if not condition:
        raise ValueError("HEALTH_GUARD_AST_SHAPE_CHANGED")


def extract(source):
    """Extract a complete unique production IF, replacing relations only.

    WHERE is not reconstructed or evaluated in Python. AST boundaries and a
    restricted expression vocabulary prevent arbitrary SQL entering the probe.
    Predicate semantics are tested independently on SQL2022, including mutants.
    """
    parsed = Linter(dialect="tsql").parse_string(source)
    require(not parsed.violations and parsed.tree is not None)
    throws = [
        s
        for s in parsed.tree.recursive_crawl("throw_statement")
        if "51130" in tokens(s)
    ]
    require(len(throws) == 1)
    matches = [
        s
        for s in parsed.tree.recursive_crawl("if_then_statement")
        if any(t is throws[0] for t in s.recursive_crawl("throw_statement"))
    ]
    require(len(matches) == 1)
    block = matches[0]
    children = code(block)
    require(
        [s.get_type() for s in children]
        == ["if_clause", "statement", "statement_terminator"]
    )
    require(code(children[1]) == [throws[0]])
    require(
        tokens(throws[0])
        == (
            "THROW",
            "51130",
            ",",
            "'Pending or orphan CDC metadata requires DBA review.'",
            ",",
            "1",
        )
    )
    clause = code(children[0])
    require(len(clause) == 2 and clause[0].raw == "IF")
    expression = code(clause[1])
    require(len(expression) == 2 and expression[0].raw == "EXISTS")
    require(expression[1].is_type("bracketed"))
    bracket = code(expression[1])
    require(
        len(bracket) == 3
        and bracket[0].raw == "("
        and bracket[2].raw == ")"
        and bracket[1].is_type("select_statement")
    )
    select = code(bracket[1])
    require(
        [s.get_type() for s in select]
        == ["select_clause", "from_clause", "where_clause"]
    )
    require(tokens(select[0]) == ("SELECT", "1"))
    # This is the join structure, never an independently copied predicate.
    require(
        tokens(select[1])
        == tuple(
            "FROM cdc . change_tables ct LEFT JOIN sys . tables src ON "
            "src . object_id = ct . source_object_id LEFT JOIN sys . tables "
            "changed ON changed . object_id = ct . object_id".split()
        )
    )
    allowed = {
        "WHERE",
        "src",
        "ct",
        "changed",
        ".",
        "object_id",
        "source_object_id",
        "is_tracked_by_cdc",
        "has_drop_pending",
        "IS",
        "NOT",
        "NULL",
        "AND",
        "OR",
        "(",
        ")",
        "<",
        ">",
        "=",
        "0",
        "1",
    }
    require(set(tokens(select[2])) <= allowed)
    require(
        not list(select[2].recursive_crawl("function", "select_statement"))
    )
    references = list(block.recursive_crawl("table_reference"))
    require(
        [r.raw for r in references]
        == ["cdc.change_tables", "sys.tables", "sys.tables"]
    )
    start = block.pos_marker.source_slice.start
    end = block.pos_marker.source_slice.stop
    original = source[start:end]
    require(original == block.raw)
    mapped = original
    mapping = {"cdc.change_tables": "#capture", "sys.tables": "#objects"}
    for reference in reversed(references):
        span = reference.pos_marker.source_slice
        left, right = span.start - start, span.stop - start
        mapped = mapped[:left] + mapping[reference.raw] + mapped[right:]
    reparsed = Linter(dialect="tsql").parse_string(mapped)
    require(not reparsed.violations)
    require(
        [s.raw for s in reparsed.tree.recursive_crawl("table_reference")]
        == ["#capture", "#objects", "#objects"]
    )
    return original, mapped


def mutants(original):
    """Source defects which must violate the independent positive rule."""
    changes = {
        "pending_removed": (
            "OR (ct.has_drop_pending IS NOT NULL AND ct.has_drop_pending<>0)",
            "",
        ),
        "pending_inverted": (
            "ct.has_drop_pending<>0",
            "ct.has_drop_pending=0",
        ),
        "source_missing_removed": ("src.object_id IS NULL OR ", ""),
        "source_tracking_removed": ("OR src.is_tracked_by_cdc<>1", ""),
        "ct_missing_removed": ("OR changed.object_id IS NULL", ""),
        "pending_and_to_or": ("IS NOT NULL AND", "IS NOT NULL OR"),
        "outer_or_to_and": ("IS NULL OR src", "IS NULL AND src"),
    }
    result = {}
    for name, (old, new) in changes.items():
        require(original.count(old) == 1)
        result[name] = original.replace(old, new)
    return result


def matrix(mapped):
    """Absent sources are absent rows, producing actual LEFT JOIN NULLs."""
    sql = """SET NOCOUNT ON;
CREATE TABLE #capture
 (source_object_id int,object_id int,has_drop_pending bit);
CREATE TABLE #objects
 (object_id int PRIMARY KEY,is_tracked_by_cdc bit NOT NULL);
CREATE TABLE #sentinel (value int NOT NULL);
INSERT #sentinel VALUES (0);
DECLARE @Error int,@Mismatches int=0,@Rejected int=0,@Accepted int=0;
"""
    for pending, exists, tracked, changed in CASES:
        # Independent positive specification, not the production SQL predicate.
        healthy = pending in (None, 0) and bool(exists and tracked and changed)
        value = "NULL" if pending is None else str(pending)
        sql += f"""
DELETE #capture;
DELETE #objects;
UPDATE #sentinel SET value=0;
INSERT #capture VALUES (10,20,{value});
IF {exists}=1 INSERT #objects VALUES (10,{tracked});
IF {changed}=1 INSERT #objects VALUES (20,0);
IF (SELECT COUNT(src.object_id) FROM #capture ct
    LEFT JOIN #objects src ON src.object_id=ct.source_object_id)<>{exists}
    THROW 51990, 'Synthetic source join witness failed.', 1;
SET @Error=0;
BEGIN TRY
{mapped}
UPDATE #sentinel SET value=value+1;
END TRY
BEGIN CATCH
SET @Error=ERROR_NUMBER();
IF @Error<>51130 THROW;
END CATCH;
IF @Error=51130 AND (SELECT value FROM #sentinel)<>0
    THROW 51991, 'Rejected case reached mutation sentinel.', 1;
IF @Error=51130 SET @Rejected+=1;
IF @Error=0 SET @Accepted+=1;
IF @Error<>{0 if healthy else 51130}
    OR (SELECT value FROM #sentinel)<>{int(healthy)} SET @Mismatches+=1;
"""
    return (
        sql
        + f"""
DECLARE @Result nvarchar(4000)=(SELECT @Mismatches AS mismatches,
 @Accepted AS accepted,@Rejected AS rejected,24 AS cases
 FOR JSON PATH,WITHOUT_ARRAY_WRAPPER);
PRINT N'{transaction_probe.PREFIX}'+@Result;
"""
    )


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("DISPOSABLE_CI_ONLY")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    source_hashes = {
        f: hashlib.sha256(
            (ROOT / "infra/sqlserver" / f).read_bytes()
        ).hexdigest()
        for f in FILES
    }
    prepared = {}
    for filename in FILES:
        original, mapped = extract(
            (ROOT / "infra/sqlserver" / filename).read_text()
        )
        prepared[filename] = {"production": mapped}
        prepared[filename].update(
            {name: extract(sql)[1] for name, sql in mutants(original).items()}
        )
    version = transaction_probe.execute(f"""
DECLARE @Result nvarchar(4000)=(SELECT
 CONVERT(int,SERVERPROPERTY('ProductMajorVersion')) AS major_version,
 CONVERT(nvarchar(128),SERVERPROPERTY('ProductVersion')) AS product_version,
 CONVERT(nvarchar(128),SERVERPROPERTY('Edition')) AS edition
 FOR JSON PATH,WITHOUT_ARRAY_WRAPPER);
PRINT N'{transaction_probe.PREFIX}'+@Result;""")
    require(
        version["major_version"] == 16 and "Developer" in version["edition"]
    )
    evidence = []
    for filename, variants in prepared.items():
        for name, sql in variants.items():
            result = transaction_probe.execute(matrix(sql))
            require(result["cases"] == 24)
            if name == "production":
                require(
                    result
                    == {
                        "mismatches": 0,
                        "accepted": 2,
                        "rejected": 22,
                        "cases": 24,
                    }
                )
            else:
                require(result["mismatches"] > 0)
            evidence.append({"file": filename, "variant": name, **result})
    require(
        source_hashes
        == {
            f: hashlib.sha256(
                (ROOT / "infra/sqlserver" / f).read_bytes()
            ).hexdigest()
            for f in FILES
        }
    )
    require(
        commit
        == subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    )
    print(
        json.dumps(
            {
                "evidence_kind": "synthetic_metadata_production_contract",
                "status": "passed",
                "runtime": version,
                "observations": evidence,
                "engine_pending_one_coverage": "not measured here",
                "source_commit": commit,
                "source_sha256": source_hashes,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
