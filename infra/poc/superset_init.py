"""Migrate Superset and register Trino with existing Gold datasets."""

import json
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path

GOLD_TABLES = ["factory_daily_production", "sales_fulfillment"]


def cli(arguments):
    """Capture setup output so database credentials never reach public logs."""
    result = subprocess.run(
        ["/app/.venv/bin/superset", *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        # Keep detailed setup diagnostics in an owner-private container file.
        # Normal Docker logs expose only the failure type, never SQL URLs.
        error_file = Path("/tmp/superset-setup-error.log")
        error_file.write_text(result.stdout + "\n" + result.stderr)
        error_file.chmod(0o600)
        raise RuntimeError(
            "Superset setup command failed; review private DB state."
        )


def initialize():
    """Preserve existing users and GUI edits on repeated initialization."""
    password = os.environ.get("SUPERSET_ADMIN_PASSWORD", "")
    if len(password) < 16:
        raise ValueError("Generate a private Superset administrator password.")
    cli(["db", "upgrade"])
    cli(["init"])
    from sqlalchemy import inspect
    from superset import appbuilder, db
    from superset.app import create_app

    app = create_app()
    with app.app_context():
        from superset.connectors.sqla.models import SqlaTable
        from superset.models.core import Database

        manager = appbuilder.sm
        username = os.environ.get("SUPERSET_ADMIN_USERNAME", "admin")
        if not manager.find_user(username=username):
            user = manager.add_user(
                username=username,
                first_name="Lakehouse",
                last_name="Admin",
                email=os.environ.get(
                    "SUPERSET_ADMIN_EMAIL", "admin@example.invalid"
                ),
                role=manager.find_role("Admin"),
                password=password,
            )
            if not user:
                raise RuntimeError("Superset administrator creation failed.")
        database = (
            db.session.query(Database)
            .filter_by(database_name="Lakehouse Trino")
            .one_or_none()
        )
        if database is None:
            database = Database(
                database_name="Lakehouse Trino",
                sqlalchemy_uri="trino://superset@trino:8080/iceberg/gold",
                expose_in_sqllab=True,
                allow_run_async=False,
                allow_ctas=False,
                allow_cvas=False,
                allow_dml=False,
            )
            db.session.add(database)
            db.session.commit()
        registered = 0
        pending = 0
        # A fresh warehouse has no Gold tables until a medallion job succeeds.
        # Keep the BI GUI available and permit this init job to be rerun later.
        with database.get_sqla_engine() as engine:
            inspector = inspect(engine)
            for name in GOLD_TABLES:
                if not inspector.has_table(name, schema="gold"):
                    pending += 1
                    continue
                dataset = (
                    db.session.query(SqlaTable)
                    .filter_by(
                        database_id=database.id, schema="gold", table_name=name
                    )
                    .one_or_none()
                )
                if dataset is None:
                    dataset = SqlaTable(
                        database=database, schema="gold", table_name=name
                    )
                    db.session.add(dataset)
                    db.session.flush()
                    dataset.fetch_metadata()
                    db.session.commit()
                registered += 1
        return {"ready": True, "gold_datasets": registered, "pending": pending}


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    try:
        print(json.dumps(initialize()))
    except Exception as error:
        diagnostic = Path("/tmp/superset-setup-error.log")
        with diagnostic.open("a") as output:
            output.write(traceback.format_exc())
        diagnostic.chmod(0o600)
        print(json.dumps({"failed": True, "error_type": type(error).__name__}))
        sys.exit(1)
