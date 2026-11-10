#!/usr/bin/env python3
"""Export bounded Gold facts pinned to Iceberg snapshots for AI consumers.

Models receive these facts and their source metadata. They do not receive a
SQL executor, worker token, storage key, or a source-database connection.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from settings import ROOT, load

DATASETS = {
    "production": ("factory_daily_production", "factory_id, production_date"),
    "sales": ("sales_fulfillment", "sales_order_id"),
}


def collect(connection, dataset, limit=100):
    """Read a named table at its current main-branch snapshot."""
    if dataset not in DATASETS or not 1 <= limit <= 500:
        raise ValueError("Select a known dataset and a limit from 1 to 500.")
    table, ordering = DATASETS[dataset]
    cursor = connection.cursor()
    try:
        cursor.execute(
            'SELECT snapshot_id FROM iceberg.gold."'
            + table
            + "$refs\" WHERE name = 'main' LIMIT 1"
        )
        snapshot_rows = cursor.fetchall()
        if not snapshot_rows:
            raise ValueError("Run the medallion pipeline before exporting.")
        snapshot = int(snapshot_rows[0][0])
        # Every interpolated identifier comes from the fixed mapping above;
        # snapshot and limit are integers, never model-generated SQL text.
        cursor.execute(
            "SELECT * FROM iceberg.gold."
            + table
            + " FOR VERSION AS OF "
            + str(snapshot)
            + " ORDER BY "
            + ordering
            + " LIMIT "
            + str(limit)
        )
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return {
            "dataset": dataset,
            "table": "iceberg.gold." + table,
            "snapshot_id": str(snapshot),
            "rows": rows,
            "row_limit": limit,
            "complete_dataset": False,
            "order_by": ordering,
        }
    finally:
        cursor.close()


def export_context(values, datasets, limit=100, connect=None):
    """Use a distinct Trino identity and expose no connection secrets."""
    if connect is None:
        from trino.dbapi import connect
    connection = connect(
        host=values["BIND_IP"],
        port=int(values["TRINO_PORT"]),
        user="lakehouse-ai-reader",
        source="lakehouse-ai-context",
        request_timeout=30,
        max_attempts=1,
    )
    try:
        result = [collect(connection, name, limit) for name in datasets]
    finally:
        connection.close()
    return {
        "context_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # These describe this exporter's checkout, not the snapshot's build.
        "exporter_reference_sql_sha256": hashlib.sha256(
            (ROOT / "medallion/03_gold.sql").read_bytes()
        ).hexdigest(),
        "exporter_code_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "datasets": result,
        "notes": [
            "Each table is pinned independently; no global snapshot is used.",
            "Rows are limited and must not be treated as full-table totals.",
            "The model must cite the table and snapshot used for its answer.",
            "Exporter hashes do not identify the SQL that built a snapshot.",
        ],
    }


def write_private(output, context):
    """Write one new private artifact outside the public checkout."""
    output = Path(output).expanduser()
    if not output.is_absolute() or output.resolve().is_relative_to(ROOT):
        raise ValueError("Use an absolute output path outside the repository.")
    if any(part.is_symlink() for part in (output, *output.parents)):
        raise ValueError("Output paths must not use symlinks.")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.parent.stat().st_mode & 0o077:
        raise ValueError("The output parent must have mode 0700.")
    content = json.dumps(context, ensure_ascii=False, indent=2, default=str)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as target:
        target.write(content + "\n")


def main():
    """Export factual context without invoking any model or downstream job."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "固定Goldを表ごとのmain snapshotで参照し、"
            "根拠付きJSONを保存します。"
            "データは変更せず、モデル推論や外部AI送信も行いません。"
            "各表の上限付き抜粋であり、全件集計には使えません。"
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=os.environ.get("LAKEHOUSE_ENV_FILE") or None,
        help="Git外のsite.env。省略時はLAKEHOUSE_ENV_FILEを使う",
    )
    parser.add_argument(
        "--dataset",
        choices=[*DATASETS, "all"],
        default="all",
        help=(
            "production=工場日別生産、sales=受注別出荷。all=両方（既定: all）"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="各表の最大行数。1〜500（既定: 100）。all時も表ごとに適用する",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="新規JSONのGit外絶対パス。0600で保存し、既存ファイルは上書きしない",
    )
    args = parser.parse_args()
    if args.env_file is None:
        parser.error("Provide --env-file or set LAKEHOUSE_ENV_FILE.")
    try:
        values = load(args.env_file)
        names = list(DATASETS) if args.dataset == "all" else [args.dataset]
        context = export_context(values, names, args.limit)
        write_private(args.output, context)
    except Exception:
        raise SystemExit(
            "Context export failed; check Gold, private settings and output."
        ) from None
    print("Snapshot-pinned Gold context exported. No model was invoked.")


if __name__ == "__main__":
    main()
