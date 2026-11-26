#!/usr/bin/env python3
"""使い捨てCI限りで、writerなしの新規設置と旧設置の互換を実SQLで確認する。"""

import os
import shutil
import subprocess
import sys
import uuid

import run as fixture


def main():
    """既設・外部DBは使わず、このCIの固定loopbackと生成したDBだけを使う。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise fixture.FixtureError("DISPOSABLE_CI_ONLY")
    sqlcmd = shutil.which("sqlcmd")
    password = os.environ.get("LAKEHOUSE_FIXTURE_PASSWORD")
    if not sqlcmd or not password:
        raise fixture.FixtureError("SYNTHETIC_CLIENT_REQUIRED")
    fixture.verify_client(sqlcmd)
    version = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=fixture.ROOT,
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()
    runner = fixture.Runner(
        "tcp:127.0.0.1,14333", sqlcmd, password, uuid.uuid4().hex, version
    )
    print("RETAINED_PROVISIONING_STAGE=virgin", flush=True)
    database, selected = runner.fresh("virgin", "Latin1_General_CI_AS")
    runner.execute(
        database,
        fixture.render("003", selected)
        + fixture.successful_permissions()
        + fixture.record("ddl_003_retry_passed"),
    )
    runner.validation(database, selected, 0)
    print("RETAINED_PROVISIONING_STAGE=existing", flush=True)
    database, selected = runner.checker_identity("own_exact")
    print("RETAINED_PROVISIONING_STAGE=dormant_legacy_policy", flush=True)
    runner.dormant_v2_residue(database, selected)
    print("RETAINED_PROVISIONING=pass", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # DB名、接続情報・実SQLやドライバの原文は出力しない。
        print("RETAINED_PROVISIONING=fail", file=sys.stderr)
        sys.exit(1)
