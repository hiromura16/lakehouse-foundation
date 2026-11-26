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
    try:
        verify(runner)
    except Exception:
        # 氏名・資格情報・SQL・driver本文は出さず、固定stageと有限の番号のみ。
        summary = runner.failure or {}
        numbers = summary.get("error_numbers", [])
        details = sorted(
            {n for n in numbers if type(n) is int and 1 <= n <= 60000}
        )[:8]
        if details:
            print(
                "RETAINED_PROVISIONING_SQL_ERRORS="
                + ",".join(map(str, details)),
                file=sys.stderr,
                flush=True,
            )
        raise


def verify(runner):
    """処理段階を分け、失敗した場合は後続へ進まない。"""
    print("RETAINED_PROVISIONING_STAGE=virgin_setup", flush=True)
    database, selected = runner.fresh("virgin", "Latin1_General_CI_AS")
    print("RETAINED_PROVISIONING_STAGE=virgin_permissions", flush=True)
    runner.execute(
        database,
        fixture.render("003", selected)
        + fixture.successful_permissions()
        + fixture.record("ddl_003_retry_passed"),
    )
    print("RETAINED_PROVISIONING_STAGE=virgin_readonly_validation", flush=True)
    runner.validation(database, selected, 0, virgin=True)
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
