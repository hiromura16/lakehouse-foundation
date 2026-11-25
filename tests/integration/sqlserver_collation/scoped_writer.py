"""使い捨てSQL Serverの固定DML入口で、書込の許可と拒否を実行する。"""

import json
import os
import secrets
import sys
from pathlib import Path

from run import render, variables

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import sqlserver_demo as adapter  # noqa: E402

HOST = "127.0.0.1"
PORT = 14333
WRITER = "fixture_writer"
PRODUCTS = "[lh_demo_erp].[products]"
GUARD_FAILURE = "Fixed demo writer permissions exceed the allowed boundary"


def require(condition):
    """失敗時は呼出側の固定stageだけを表示する。"""
    if not condition:
        raise RuntimeError("SYNTHETIC_WRITER_EVIDENCE_FAILED")


def stage(name):
    """資格情報、識別子、SQL、のぞいたレコードは出力しない。"""
    print("SCOPED_WRITER_STAGE=" + name, flush=True)


def fixture_database(cursor, database, login, password, selected):
    """新規DBに公開DDLをそのまま適用し、無関係な別ユーザーも持つ。"""
    cursor.execute(
        "SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ProductMajorVersion')),"
        "CONVERT(nvarchar(128),SERVERPROPERTY('Edition'))"
    )
    version, edition = cursor.fetchone()
    require(str(version) == "16" and "Developer" in edition)
    stage("create_disposable_database")
    cursor.execute("CREATE DATABASE [" + database + "]")
    cursor.execute(
        "CREATE LOGIN [" + login + "] WITH PASSWORD=%s, CHECK_POLICY=OFF",
        (password,),
    )
    cursor.execute("USE [" + database + "]")
    cursor.execute("CREATE USER [" + WRITER + "] FOR LOGIN [" + login + "]")
    for user in ("fixture_reader", "fixture_control", "fixture_legacy"):
        cursor.execute("CREATE USER [" + user + "] WITHOUT LOGIN")
    for number in ("001", "002", "003"):
        stage("canonical_ddl_" + number)
        cursor.execute(render(number, selected))
        while cursor.nextset():
            pass
    stage("seed_disposable_legacy")
    cursor.execute("CREATE SCHEMA legacy AUTHORIZATION dbo")
    cursor.execute("CREATE TABLE legacy.unrelated (id int NOT NULL)")
    cursor.execute("INSERT INTO legacy.unrelated VALUES (42)")
    cursor.execute("CREATE ROLE fixture_legacy_role AUTHORIZATION dbo")
    cursor.execute(
        "GRANT SELECT ON OBJECT::legacy.unrelated TO fixture_legacy_role"
    )
    cursor.execute("ALTER ROLE fixture_legacy_role ADD MEMBER fixture_legacy")


def positive_permission_evidence(values):
    """実SQLで0件とOUTERの補完NULLを区別し、固定所有権も検証する。"""
    stage("normal_writer_login_and_role")
    with adapter.connection(values) as writer:
        cursor = writer.cursor()
        cursor.execute(
            "SELECT IS_MEMBER(N'lh_demo_writer'), "
            "IS_MEMBER(N'fixture_legacy_role')"
        )
        require(cursor.fetchone() == (1, 0))
        # 対象名を固定した一行で、権限TVFの実出力は0件、従来の外部
        # 結合はNULL行1件になる。別objectのカタログ可視性は仮定しない。
        stage("normal_writer_sql_apply_zero_rows")
        for apply, expected in (("CROSS APPLY", 0), ("OUTER APPLY", 1)):
            cursor.execute(
                "SELECT COUNT_BIG(*) FROM "
                "(VALUES(N'legacy.unrelated')) AS q(name) "
                + apply
                + " sys.fn_my_permissions(q.name,N'OBJECT') p "
                "WHERE p.permission_name IS NULL"
            )
            require(cursor.fetchone() == (expected,))
        stage("normal_writer_runtime_allow")
        require(adapter.permissions_safe(cursor))


def visible_legacy_without_grants(values, cursor, run):
    """追加の書込権なしでカタログに現れる0権限の行を実SQLで証明。"""
    cursor.execute("SELECT OBJECT_ID(N'legacy.unrelated',N'U')")
    (expected_id,) = cursor.fetchone()
    require(type(expected_id) is int and expected_id > 0)
    # SQL2022のメタデータ専用権限をこの合成DB内だけに一時付与する。
    # 開発/運用のwriter設定に追加する権限ではない。
    stage("temporary_metadata_grant")
    cursor.execute("GRANT VIEW SECURITY DEFINITION TO fixture_writer")
    try:
        stage("visible_legacy_catalog_query")
        with adapter.connection(values) as writer:
            limited = writer.cursor()
            limited.execute(
                "SELECT o.object_id, OBJECT_ID(N'legacy.unrelated',N'U'),"
                "(SELECT COUNT_BIG(*) FROM "
                "sys.fn_my_permissions(N'legacy.unrelated',N'OBJECT')) "
                "FROM sys.objects o JOIN sys.schemas s "
                "ON s.schema_id=o.schema_id "
                "WHERE s.name=N'legacy' AND o.name=N'unrelated'"
            )
            row = limited.fetchone()
            stage("visible_legacy_catalog_row")
            require(row is not None)
            stage("visible_legacy_catalog_identity")
            require(row[:2] == (expected_id, expected_id))
            stage("visible_legacy_effective_count")
            require(row[2:] == (0,))
            require(limited.fetchone() is None)
            stage("visible_legacy_strict_audit_refused")
            require(not adapter.permissions_safe(limited, full_audit=True))
            stage("visible_legacy_runtime_allow")
            require(adapter.permissions_safe(limited))
        stage("visible_legacy_dml_readback")
        require(
            adapter.mutate(values, "insert", run_id=run, consent=True)[
                "changed_rows"
            ]
            == 1
        )
        compare_product(cursor, run, "合成CDC投入")
        require(
            adapter.mutate(values, "delete", run_id=run, consent=True)[
                "changed_rows"
            ]
            == 1
        )
        compare_product(cursor, run, None)
    finally:
        cursor.execute("REVOKE VIEW SECURITY DEFINITION FROM fixture_writer")


def check_status(values):
    """通常の限定writerでもCDC captureを参照せず診断が通る。"""
    stage("normal_writer_status")
    result = adapter.diagnose(values)
    require(
        result["connected"]
        and result["ownership"]
        and result["permissions_safe"]
        and result["schema_version"] == 1
        and len(result["tables"]) == 6
        and all(
            table["exists"] and table["capture_instance"] is None
            for table in result["tables"].values()
        )
    )


def compare_product(cursor, run, name):
    """別接続で固定行と原文の更新、または不在を確認する。"""
    key = "CDC-" + run
    cursor.execute(
        "SELECT product_name, run_id, original_values FROM "
        + PRODUCTS
        + " WHERE product_id=%s",
        (key,),
    )
    row = cursor.fetchone()
    if name is None:
        require(row is None)
    else:
        require(row is not None and row[:2] == (name, run))
        payload = json.loads(row[2])
        require(
            payload["product_id"] == key and payload["product_name"] == name
        )
    require(cursor.fetchone() is None)
    return row


def compare_unrelated(cursor, sentinel, expected):
    """別runの原文を含む固定行と、無関係な表の内容を維持する。"""
    require(compare_product(cursor, sentinel, "合成CDC投入") == expected)
    cursor.execute("SELECT id FROM legacy.unrelated ORDER BY id")
    require(cursor.fetchall() == [(42,)])


def mutations(values, cursor, run, sentinel, expected):
    """固定DMLが実際にcommitし、拒否ケースは1行も残らないこと。"""
    stage("fixed_dml_positive")
    for action, name in (
        ("insert", "合成CDC投入"),
        ("update", "合成CDC更新"),
        ("delete", None),
    ):
        require(
            adapter.mutate(values, action, run_id=run, consent=True)
            == {
                "action": action,
                "changed_rows": 1,
                "run_id": run,
                "source_only": True,
            }
        )
        compare_product(cursor, run, name)
        compare_unrelated(cursor, sentinel, expected)

    cases = (
        (
            "required_rights",
            "DENY UPDATE ON OBJECT::lh_demo_erp.products TO fixture_writer",
            "REVOKE UPDATE ON OBJECT::lh_demo_erp.products "
            "FROM fixture_writer",
        ),
        (
            "outside_insert",
            "GRANT INSERT ON OBJECT::legacy.unrelated TO fixture_writer",
            "REVOKE INSERT ON OBJECT::legacy.unrelated FROM fixture_writer",
        ),
        (
            "outside_column_update",
            "GRANT UPDATE (id) ON legacy.unrelated TO fixture_writer",
            "REVOKE UPDATE (id) ON legacy.unrelated FROM fixture_writer",
        ),
        (
            "broad_role",
            "ALTER ROLE db_datawriter ADD MEMBER fixture_writer",
            "ALTER ROLE db_datawriter DROP MEMBER fixture_writer",
        ),
        (
            "hidden_inbound_fk",
            "CREATE TABLE legacy.child ( product_id nvarchar(160) NOT NULL "
            "REFERENCES lh_demo_erp.products(product_id) ON DELETE CASCADE )",
            "DROP TABLE legacy.child",
        ),
    )
    for label, change, restore in cases:
        stage(label)
        cursor.execute(change)
        try:
            if label == "hidden_inbound_fk":
                with adapter.connection(values) as writer:
                    limited = writer.cursor()
                    limited.execute("SELECT OBJECT_ID(N'legacy.child',N'U')")
                    require(limited.fetchone() == (None,))
                    require(not adapter.side_effects_safe(limited))
            try:
                adapter.mutate(values, "insert", run_id=run, consent=True)
            except adapter.SqlServerError as error:
                require(str(error).startswith(GUARD_FAILURE))
            else:
                raise RuntimeError("UNSAFE_DML_ACCEPTED")
            compare_product(cursor, run, None)
            compare_unrelated(cursor, sentinel, expected)
        finally:
            cursor.execute(restore)
    stage("fixed_writer_restored")
    result = adapter.mutate(values, "insert", run_id=run, consent=True)
    require(result["changed_rows"] == 1)
    compare_product(cursor, run, "合成CDC投入")
    compare_unrelated(cursor, sentinel, expected)


def main():
    """同一リポジトリの使い捨てrunner以外は接続前に終了する。"""
    require(
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("GITHUB_REPOSITORY")
        == "hiromura16/lakehouse-foundation"
        and os.environ.get("RUNNER_OS") == "Linux"
    )
    import pymssql

    suffix = secrets.token_hex(8)
    database = "lh_collation_" + suffix + "_writer"
    login = "writer_fixture_" + suffix
    password = secrets.token_hex(24) + "Aa1!"
    nonce = secrets.token_hex(16)
    selected = variables(database, nonce, os.environ["GITHUB_SHA"])
    values = {
        "DEMO_SQLSERVER_HOST": HOST,
        "DEMO_SQLSERVER_PORT": str(PORT),
        "DEMO_SQLSERVER_DATABASE": database,
        "DEMO_SQLSERVER_USER": login,
        "DEMO_SQLSERVER_PASSWORD": password,
        "DEMO_SQLSERVER_TRUST_SERVER_CERTIFICATE": "true",
        "DEMO_SQLSERVER_INSTALLATION_ID": nonce,
        "DEMO_SQLSERVER_OWNERSHIP_TOKEN": nonce * 2,
    }
    stage("prepare")
    with pymssql.connect(
        server=HOST,
        port=PORT,
        user="sa",
        password=os.environ["LAKEHOUSE_FIXTURE_PASSWORD"],
        database="master",
        autocommit=True,
        login_timeout=10,
        timeout=30,
    ) as admin:
        cursor = admin.cursor()
        fixture_database(cursor, database, login, password, selected)
        stage("zero_effective_permissions")
        positive_permission_evidence(values)
        check_status(values)
        run = secrets.token_hex(6)
        sentinel = secrets.token_hex(6)
        require(run != sentinel)
        require(
            adapter.mutate(values, "insert", run_id=sentinel, consent=True)[
                "changed_rows"
            ]
            == 1
        )
        expected = compare_product(cursor, sentinel, "合成CDC投入")
        compare_unrelated(cursor, sentinel, expected)
        stage("visible_legacy_zero_permissions")
        visible_legacy_without_grants(values, cursor, run)
        compare_unrelated(cursor, sentinel, expected)
        mutations(values, cursor, run, sentinel, expected)
    stage("passed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FAIL: SCOPED_WRITER_ACCEPTANCE", file=sys.stderr)
        raise SystemExit(1) from None
