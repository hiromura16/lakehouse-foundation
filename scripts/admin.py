"""完成済み原本を取り込み、登録済みのメダリオンSQLを実行する。

読む順序は bootstrap → ingest → medallion → run → Handler/main。
ingestはreadyとhashを照合し、原本・Bronze・完了台帳の順に保存する。
medallionは最新版の採用・隔離・Gold集計を管理SQLへ委ねる。
各表のcommitは独立しており、実行全体のrollbackは提供しない。
GUI・CLI等の処理依頼は常駐workerの固定HTTP入口へ集約する。
取消の完了を確認できない場合は、新規処理を止めて管理者の確認を待つ。
"""

import argparse
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import boto3
import pyarrow as pa
import requests
from botocore.config import Config
from botocore.exceptions import ClientError
from PIL import Image
from pyiceberg.catalog import load_catalog
from pyiceberg.expressions import EqualTo

ROOT = Path("/opt/lakehouse")
HTTP = requests.Session()
S3 = boto3.client(
    "s3",
    endpoint_url="http://storage:8333",
    region_name="us-east-1",
    aws_access_key_id=os.environ["S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["S3_SECRET_KEY"],
    config=Config(
        s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=30
    ),
)
LOCK = threading.Lock()
STATE = {
    "ready": False,
    "blocked": False,
    "successful_runs": 0,
    "failed_runs": 0,
    "last_success": 0,
    "imported_files": 0,
}
CONTRACT = json.loads((ROOT / "medallion/sources.json").read_text())["sources"]
MAX_BYTES = 16 * 1024 * 1024


def request(method, url, payload=None, accepted=(200, 201, 204)):
    """カタログ管理APIへJSONを送り、許可したHTTP成功時の本文を返す。

    作成系の呼出しには副作用がある。異常時はstatusだけを例外へ含め、
    資格情報を含み得る要求・応答本文は利用者へ返さない。
    """
    response = HTTP.request(method, url, json=payload, timeout=30)
    if response.status_code not in accepted:
        raise RuntimeError(
            "Service returned HTTP " + str(response.status_code)
        )
    return response.json() if response.content else None


def catalog():
    """privateなS3資格情報で、共有のIceberg REST catalogを開く。

    表の操作先を返すだけで、ここでは表を作成しない。FileIOはclient側の
    鍵を使い、カタログ側の署名サービスへ暗黙に切り替えない。
    """
    return load_catalog(
        "lakehouse",
        type="rest",
        uri="http://catalog:8181/catalog",
        warehouse=os.environ["WAREHOUSE_NAME"],
        **{
            # 共通設定の鍵をFileIOへ渡し、catalog側署名へ暗黙移行しない。
            "header.X-Iceberg-Access-Delegation": "client-managed",
            "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
            "s3.endpoint": "http://storage:8333",
            "s3.region": "us-east-1",
            "s3.access-key-id": os.environ["S3_ACCESS_KEY"],
            "s3.secret-access-key": os.environ["S3_SECRET_KEY"],
            "s3.force-virtual-addressing": "false",
        },
    )


def bootstrap():
    """不足するbucket・warehouse・空の契約表を準備する。

    既存warehouseはbucketとendpointが一致する場合に再利用する。
    既存データや鍵を再生成せず、既存表のschema移行も行わない。
    """
    for bucket in [
        os.environ["S3_BUCKET_RAW"],
        os.environ["S3_BUCKET_WAREHOUSE"],
    ]:
        try:
            S3.head_bucket(Bucket=bucket)
        except ClientError as error:
            if str(error.response["Error"]["Code"]) not in {
                "404",
                "NoSuchBucket",
                "NotFound",
            }:
                raise
            S3.create_bucket(Bucket=bucket)
    info = request("GET", "http://catalog:8181/management/v1/info")
    if not info.get("bootstrapped"):
        request(
            "POST",
            "http://catalog:8181/management/v1/bootstrap",
            {"accept-terms-of-use": True},
        )
    warehouses = request(
        "GET", "http://catalog:8181/management/v1/warehouse"
    ).get("warehouses", [])
    matching = [
        r
        for r in warehouses
        if r.get("name", r.get("warehouse-name"))
        == os.environ["WAREHOUSE_NAME"]
    ]
    if not matching:
        request(
            "POST",
            "http://catalog:8181/management/v1/warehouse",
            {
                "warehouse-name": os.environ["WAREHOUSE_NAME"],
                "project-id": "00000000-0000-0000-0000-000000000000",
                "storage-profile": {
                    "type": "s3",
                    "bucket": os.environ["S3_BUCKET_WAREHOUSE"],
                    "key-prefix": "iceberg",
                    "endpoint": "http://storage:8333",
                    "region": "us-east-1",
                    "path-style-access": True,
                    "flavor": "s3-compat",
                    "sts-enabled": False,
                },
                "storage-credential": {
                    "type": "s3",
                    "credential-type": "access-key",
                    "access-key-id": os.environ["S3_ACCESS_KEY"],
                    "secret-access-key": os.environ["S3_SECRET_KEY"],
                },
            },
        )
    else:
        profile = matching[0].get("storage-profile", {})
        if (
            profile.get("bucket") != os.environ["S3_BUCKET_WAREHOUSE"]
            or str(profile.get("endpoint", "")).rstrip("/")
            != "http://storage:8333"
        ):
            raise RuntimeError(
                "Existing warehouse uses a different storage profile; "
                "refusing to reuse it."
            )
    ensure_tables()


def schema(prefix, layer):
    """許可した用途のprefixと層名を結び、namespace名を返す。

    通常処理、CDC、実行ごとの隔離検証だけを受け付ける。任意prefixを
    許してしまうと、検証の後片付けが通常表へ及ぶため入力を制限する。
    """
    if prefix not in {"", "cdc_", "sql_"} and not re.fullmatch(
        r"verify_[0-9a-f]+_", prefix
    ):
        raise ValueError("Invalid data namespace prefix.")
    return prefix + layer


def ensure_tables(prefix=""):
    """選択namespaceに不足する契約表と原本台帳を作成する。

    Bronzeは業務値と由来を文字列として保存する。既存表は保持するため、
    契約の列を変更しても既存schemaが自動で移行するわけではない。
    """
    cat = catalog()
    for layer in ["bronze", "silver", "gold", "quarantine"]:
        namespace = schema(prefix, layer)
        if not cat.namespace_exists(namespace):
            cat.create_namespace(namespace)
    for source in CONTRACT:
        identifier = schema(prefix, "bronze") + "." + source["table"]
        if not cat.table_exists(identifier):
            fields = source["columns"] + [
                "_source_file",
                "_source_hash",
                "_ingested_at",
            ]
            if prefix == "sql_":
                # SQL provenance extends only the isolated fixed-source tables.
                # Existing Landing/CDC schemas keep their current contracts.
                from scripts.sql_batch_worker import SQL_SOURCE_COLUMNS

                fields += list(SQL_SOURCE_COLUMNS)
            cat.create_table(
                identifier,
                schema=pa.schema([(name, pa.string()) for name in fields]),
                properties={"format-version": "2"},
            )
    for table, columns in [
        ("files", ["path", "sha256", "s3_uri", "mime", "bytes"]),
        (
            "images",
            ["path", "sha256", "s3_uri", "mime", "bytes", "width", "height"],
        ),
    ]:
        identifier = schema(prefix, "bronze") + "." + table
        if not cat.table_exists(identifier):
            cat.create_table(
                identifier,
                schema=pa.schema([(name, pa.string()) for name in columns]),
                properties={"format-version": "2"},
            )


def overwrite(table, rows, column, value):
    """指定列の値に一致する範囲を、渡された行集合へ置き換える。

    Arrowの型は対象Iceberg表へ合わせる。1表の置換を確定する操作であり、
    原本保存や別表の更新をまとめたtransactionではない。
    """
    arrow = pa.Table.from_pylist(rows, schema=table.schema().as_arrow())
    table.overwrite(arrow, overwrite_filter=EqualTo(column, value))


def completion_hash(marker):
    """改行付きのSHA-256完了markerを検査してhash文字列を返す。

    サイズ確認の後も読取量を制限し、読取前後のfile成長に備える。
    原本との一致は呼出元のingestが別に検査する。
    """
    if marker.stat().st_size > 66:
        raise ValueError("Invalid completion marker.")
    # stat後の成長にも備え、読取量そのものにも上限を設ける。
    with marker.open("rb") as stream:
        content = stream.read(67)
    if not re.fullmatch(rb"[a-f0-9]{64}\r?\n", content):
        raise ValueError("Invalid completion marker.")
    return content[:64].decode("ascii")


def resolve_source(relative):
    """相対pathから、固定6契約または正式なGUI増分名を照合する。

    一致した契約を返し、未知の名前はNoneを返す。未知ファイルは原本保存の
    対象にはなるが、任意の業務表として自動認識しない。
    """
    for source in CONTRACT:
        canonical = source["path"]
        if relative == canonical:
            return source
        stem, extension = canonical.rsplit(".", 1)
        pattern = (
            re.escape(stem)
            + r"__[a-f0-9]{12}_(?!0000)[0-9]{4}\."
            + re.escape(extension)
        )
        if re.fullmatch(pattern, relative):
            return source
    return None


def ingest(directory=Path("/landing"), prefix=""):
    """ready付き原本を照合し、原本・業務文字列・完了台帳を保存する。

    directoryは読取元、prefixは通常または隔離検証のnamespaceを選ぶ。
    同じ相対pathと内容hashが完了済みならskipし、件数を辞書で返す。
    更新素材はそのpath由来のBronze行だけを置換する。原本は内容hash別に
    保存し、landingの削除を分析表のDELETEとしては扱わない。
    途中失敗では一部の原本・表が残るため、完了台帳を最後に確定する。
    """
    ensure_tables(prefix)
    cat = catalog()
    files = cat.load_table(schema(prefix, "bronze") + ".files")
    imported = skipped = 0
    # 工程1: readyがある原本だけを対象にし、転送途中のfileを扱わない。
    for marker in sorted(directory.rglob("*.ready")):
        path = marker.with_name(marker.name[:-6])
        if marker.is_symlink() or path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Landing file exceeds the bounded import limit.")
        expected = completion_hash(marker)
        # 内容hashと読取前後の属性で、marker作成後や読取中の変更を拒否する。
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        digest = sha256(data).hexdigest()
        if digest != expected or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(
                "Landing file changed or does not match its completion marker."
            )
        previous = (
            files.scan(row_filter=EqualTo("path", relative))
            .to_arrow()
            .to_pylist()
        )
        # 工程2: 完了台帳のpath/hashが一致する時だけskipする。
        # 台帳未完了なら後段を再実行できるよう、台帳は最後に更新する。
        if previous and previous[0]["sha256"] == digest:
            skipped += 1
            continue
        source = resolve_source(relative)
        rows = []
        if source:
            # 業務契約へ一致する素材だけを解釈し、型の妥当性はSilverへ委ねる。
            # 未知のCSV等は任意表へせず、下段の原本保存と台帳登録だけを行う。
            if path.suffix == ".csv":
                rows = list(csv.DictReader(StringIO(data.decode("utf-8-sig"))))
            else:
                rows = [
                    json.loads(line)
                    for line in data.decode("utf-8-sig").splitlines()
                    if line.strip()
                ]
            if len(rows) > 100000 or not all(
                isinstance(row, dict) and set(row) == set(source["columns"])
                for row in rows
            ):
                raise ValueError(
                    "Input does not match its declared source "
                    "columns/row limit."
                )
            stamp = datetime.now(timezone.utc).isoformat()
            rows = [
                {
                    **{
                        key: None if value is None else str(value)
                        for key, value in row.items()
                    },
                    "_source_file": relative,
                    "_source_hash": digest,
                    "_ingested_at": stamp,
                }
                for row in rows
            ]
        bucket = os.environ["S3_BUCKET_RAW"]
        key = (
            ("verify/" + prefix if prefix else "raw/")
            + digest
            + "/"
            + sha256(relative.encode()).hexdigest()
        )
        uri = "s3://" + bucket + "/" + key
        mime = "application/octet-stream"
        image = None
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            # verify後のdecoderは再利用せず、寸法取得は別に開き直す。
            with Image.open(BytesIO(data)) as decoded:
                decoded.verify()
            with Image.open(BytesIO(data)) as decoded:
                image = (decoded.width, decoded.height)
                mime = Image.MIME.get(decoded.format, mime)
        # 工程3: 内容とpathのhashで原本を識別し、同名fileの旧版もS3へ残す。
        # 以降で失敗しても原本は残り得るが、完了台帳を先に進めない。
        S3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=mime)
        if source:
            table = cat.load_table(
                schema(prefix, "bronze") + "." + source["table"]
            )
            overwrite(table, rows, "_source_file", relative)
        meta = {
            "path": relative,
            "sha256": digest,
            "s3_uri": uri,
            "mime": mime,
            "bytes": str(len(data)),
        }
        if image:
            table = cat.load_table(schema(prefix, "bronze") + ".images")
            overwrite(
                table,
                [{**meta, "width": str(image[0]), "height": str(image[1])}],
                "path",
                relative,
            )
        # 工程4: 業務行/画像metadataのcommit後、最後にfile完了を確定する。
        # 1表ずつはatomicでも、S3と複数表をまとめたtransactionではない。
        overwrite(files, [meta], "path", relative)
        imported += 1
    STATE["imported_files"] += imported
    return {"imported": imported, "skipped": skipped}


class WorkerBlockedError(RuntimeError):
    """取消未確定のqueryが残り得るため、管理者確認まで新規処理を拒否する。"""


def internal_query_uri(uri):
    """継続・取消URLが許可した内部Trinoのoriginかを判定する。

    scheme・host・port・userinfo・fragmentを検査する。外部originへの
    redirectやtoken転送を許す判定ではなく、path別のallowlistでもない。
    """
    try:
        target = urlsplit(uri)
        return (
            target.scheme == "http"
            and target.hostname == "trino"
            and target.port == 8080
            and target.username is None
            and target.password is None
            and not target.fragment
        )
    except (TypeError, ValueError):
        return False


def cancel_query(next_uri):
    """既知の内部URLへ取消を送り、完了を確認できたかを真偽値で返す。

    204または404だけを取消済みと判断する。URL不明・通信失敗を成功へ
    読み替えず、下流例外の接続情報も呼出元へ返さない。
    """
    if not next_uri or not internal_query_uri(next_uri):
        return False
    try:
        response = HTTP.delete(next_uri, timeout=5, allow_redirects=False)
    except Exception:
        return False
    return response.status_code in {204, 404}


def query(sql):
    """1文のTrino SQLを完了まで追い、結果行を返す。

    HTTP 200は受付・途中結果の場合があるため、nextUriを最後まで追う。
    異常時は残存queryを取り消し、取消未確認ならSTATEをblockedにする。
    失敗時にSQL・接続先・応答本文を含む例外を利用者へ転送しない。
    """
    # HTTP protocolへは、CLI用の終端記号を除いた1文を送る。
    sql = sql.strip().removesuffix(";").rstrip()
    pending_uri = None
    terminal = False
    try:
        response = HTTP.post(
            "http://trino:8080/v1/statement",
            data=sql.encode(),
            headers={"X-Trino-User": "lakehouse-jobs"},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError("Trino request failed.")
        value = response.json()
        rows = value.get("data", [])
        deadline = time.monotonic() + 180
        while True:
            if value.get("error"):
                # 明示的なSQL失敗は終端なので、残存queryの取消は不要。
                terminal = True
                raise RuntimeError("Trino query failed.")
            # HTTP 200だけでは完了でない。nextUriがなくなるまで結果を追う。
            next_uri = value.get("nextUri")
            if not next_uri:
                terminal = True
                return rows
            if not internal_query_uri(next_uri):
                raise RuntimeError("Unexpected query result URL.")
            pending_uri = next_uri
            if time.monotonic() > deadline:
                raise TimeoutError("Query deadline exceeded.")
            time.sleep(0.1)
            response = HTTP.get(pending_uri, timeout=30, allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError("Trino request failed.")
            value = response.json()
            rows.extend(value.get("data", []))
    except Exception:
        # 最初のPOSTの応答不明でも、Trinoで実行中の可能性は残る。
        # 取消を確認できないまま次のwriterを開始しない。
        if not terminal and not cancel_query(pending_uri):
            STATE["blocked"] = True
        # 下流例外はSQL/接続先/応答本文を含み得るため、固定例外へ置き換える。
        raise RuntimeError("Trino query failed.") from None


def medallion(prefix=""):
    """管理されたSQLを依存順で実行し、完了した文数を返す。

    Silverで最新候補を選び、妥当性・親参照を判定してから隔離とGoldへ進む。
    prefixは同じSQLを通常・CDC・隔離検証の各層へ向けるために使う。
    1文ずつ表を置換するため、失敗時に全表を旧版へ巻き戻すことはない。
    """
    ensure_tables(prefix)
    count = 0
    # ファイル順で最新版採用・隔離・集計の依存を守る。業務規則の正本はSQL。
    # CREATE OR REPLACEは1表ずつcommitし、14文全体の一括commitではない。
    for path in sorted((ROOT / "medallion").glob("*.sql")):
        statements = "\n".join(
            line
            for line in path.read_text().splitlines()
            if not line.lstrip().startswith("--")
        )
        # 管理SQL用の単純な分割であり、任意SQLを受ける汎用parserではない。
        for statement in statements.split(";"):
            if not statement.strip():
                continue
            if prefix == "sql_":
                # Preserve SQL origin columns through each typed/final SELECT.
                # Keep the same qualifier when a source is selected via alias.
                from scripts.sql_batch_worker import SQL_SOURCE_COLUMNS

                def sql_provenance(match):
                    """Extend a projection, retaining its source alias."""
                    qualifier = match.group("qualifier") or ""
                    return (
                        ", ".join(
                            qualifier + column for column in SQL_SOURCE_COLUMNS
                        )
                        + ", "
                        + qualifier
                        + "_source_file,"
                    )

                statement = re.sub(
                    r"(?P<qualifier>[A-Za-z_]\w*\.)?_source_file,",
                    sql_provenance,
                    statement,
                )
            for layer in ["bronze", "silver", "gold", "quarantine"]:
                statement = statement.replace(
                    "iceberg." + layer + ".",
                    "iceberg." + schema(prefix, layer) + ".",
                )
            query(statement)
            count += 1
    return {"sql_statements": count}


def verify():
    """専用namespaceで固定合成素材・画像・再実行の期待値を照合する。

    取込→変換→独立期待値→同一素材の再取込→再変換の順に検査する。
    結果は成功した検査名と取込件数。通常の業務表は変更対象にしない。
    後片付けもこの実行の表・原本だけへ限定し、取消未確定なら保全する。
    """
    # 実行ごとの隔離先を使い、通常業務表を検証・削除の対象にしない。
    prefix = "verify_" + secrets.token_hex(5) + "_"
    cat = catalog()
    result = {"checks": []}
    try:
        with tempfile.TemporaryDirectory(
            prefix="lakehouse-fixtures-"
        ) as directory:
            command = [
                sys.executable,
                str(ROOT / "scripts" / "generate-demo.py"),
                "--output",
                directory,
                "--seed",
                "42",
                "--orders",
                "20",
                "--days",
                "7",
                "--scenario",
                "edge",
            ]
            subprocess.run(command, check=True)
            # 業務行の期待値とは別に、画像原本のバイト列保存も検査する。
            image_path = Path(directory) / "images" / "inspection.png"
            image_path.parent.mkdir()
            Image.new("RGB", (4, 4), color="gray").save(image_path)
            image_hash = sha256(image_path.read_bytes()).hexdigest()
            image_path.with_name(image_path.name + ".ready").write_text(
                image_hash + "\n"
            )
            first = ingest(Path(directory), prefix)
            medallion(prefix)
            image_table = cat.load_table(schema(prefix, "bronze") + ".images")
            image_record = image_table.scan().to_arrow().to_pylist()[0]
            image_uri = urlsplit(image_record["s3_uri"])
            original = S3.get_object(
                Bucket=image_uri.netloc,
                Key=image_uri.path.lstrip("/"),
            )["Body"].read()
            if sha256(original).hexdigest() != image_hash:
                raise AssertionError("Stored image bytes do not match.")
            result["checks"].append("image original bytes and hash preserved")
            checks = json.loads(
                (ROOT / "medallion/expected-demo.json").read_text()
            )["checks"]
            for check in checks:
                # 版管理した検査SQLと独立期待値を使い、生成件数で代用しない。
                sql = (
                    check.get("sql")
                    or (ROOT / "medallion" / check["sql_file"]).read_text()
                )
                for layer in ["bronze", "silver", "gold", "quarantine"]:
                    sql = sql.replace(
                        "iceberg." + layer + ".",
                        "iceberg." + schema(prefix, layer) + ".",
                    )
                actual = query(sql)
                if actual != check["expected"]:
                    raise AssertionError(
                        "MES/ERP check failed: " + check["name"]
                    )
                result["checks"].append(check["name"])
            second = ingest(Path(directory), prefix)
            if second["imported"]:
                raise AssertionError("Unchanged inputs imported twice.")
            medallion(prefix)
            for check in checks:
                # 再実行後にも同じ期待値を照合し、追記二重計上を検出する。
                sql = (
                    check.get("sql")
                    or (ROOT / "medallion" / check["sql_file"]).read_text()
                )
                for layer in ["bronze", "silver", "gold", "quarantine"]:
                    sql = sql.replace(
                        "iceberg." + layer + ".",
                        "iceberg." + schema(prefix, layer) + ".",
                    )
                if query(sql) != check["expected"]:
                    raise AssertionError("Medallion replay changed results.")
            result.update(
                first_import=first,
                replay_import=second,
                isolated_namespaces=True,
                ok=True,
            )
    finally:
        # 検査失敗でも自分の隔離先だけを掃除し、共有namespaceは消さない。
        # 取消未確定時はqueryが使用中かもしれないため、隔離先も保全する。
        if not STATE["blocked"]:
            cleanup_verification(cat, prefix)
    return result


def cleanup_verification(cat, prefix):
    """完了した1回の隔離検証に属する表・namespace・原本を削除する。

    専用prefixの形式を検査してからpurgeする破壊的な後片付けである。
    通常業務表や他の検証runを掃除する汎用処理として使わない。
    """
    if not re.fullmatch(r"verify_[0-9a-f]+_", prefix):
        raise ValueError("Cleanup requires a temporary verification prefix.")
    for layer in ["gold", "silver", "quarantine", "bronze"]:
        namespace = schema(prefix, layer)
        if cat.namespace_exists(namespace):
            for identifier in cat.list_tables(namespace):
                cat.purge_table(identifier)
            cat.drop_namespace(namespace)
    bucket = os.environ["S3_BUCKET_RAW"]
    for page in S3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix="verify/" + prefix
    ):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            S3.delete_objects(Bucket=bucket, Delete={"Objects": objects})


def sql_worker_context():
    """Expose only the existing worker capabilities needed by SQL batches."""
    return SimpleNamespace(
        pa=pa,
        CONTRACT=CONTRACT,
        catalog=catalog,
        ensure_tables=ensure_tables,
        medallion=medallion,
        S3=S3,
        os=os,
    )


def run(action, payload=None):
    """固定actionを同じworkerプロセス内で排他実行し、結果を返す。

    処理成功後にメモリー内の件数と最終成功時刻を更新する。blocked中は
    新規処理を拒否する。外部Sparkや別プロセスまでの分散lockではない。
    """
    if not LOCK.acquire(blocking=False):
        raise RuntimeError("Another ingestion/medallion job is running.")
    try:
        # lock解放後もblockedは維持する。query確認後の再起動まで解除しない。
        if STATE["blocked"]:
            raise WorkerBlockedError(
                "Confirm Trino query completion, then restart the worker."
            )
        if action == "bootstrap":
            bootstrap()
            result = {"ok": True}
        elif action == "ingest":
            result = ingest()
        elif action == "medallion":
            result = {**ingest(), **medallion()}
        elif action == "verify":
            result = verify()
        elif action == "sql-load":
            from scripts.sql_batch_worker import load

            result = load(
                sql_worker_context(),
                payload["batch_id"],
                payload["observed_at"],
            )
        else:
            raise ValueError("Unsupported action.")
        STATE["successful_runs"] += 1
        STATE["last_success"] = time.time()
        return result
    except Exception:
        STATE["failed_runs"] += 1
        raise
    finally:
        LOCK.release()


class Handler(BaseHTTPRequestHandler):
    """固定job・health・集計metricsを提供するworkerのHTTP入口。"""

    def log_message(self, *_):
        """接続情報を残さないよう、既定のaccess logを抑止する。"""
        pass

    def respond(self, status, value, content_type="application/json"):
        """HTTP statusと、JSONまたはmetrics本文を明示した長さで返す。"""
        body = (
            json.dumps(value).encode()
            if content_type == "application/json"
            else value.encode()
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """healthまたは集計metricsを返し、設定値や業務payloadは返さない。

        healthのreadyは初期化済みかつ取消確認待ちでないことを表す。
        metricsの値はプロセスメモリー上にあり、永続監査台帳ではない。
        """
        if self.path == "/health":
            self.respond(
                200 if STATE["ready"] and not STATE["blocked"] else 503,
                {
                    "ready": STATE["ready"] and not STATE["blocked"],
                    "blocked": STATE["blocked"],
                    "busy": LOCK.locked(),
                },
            )
        elif self.path == "/metrics":
            body = (
                "\n".join(
                    "lakehouse_"
                    + key
                    + " "
                    + str(int(value) if isinstance(value, bool) else value)
                    for key, value in STATE.items()
                )
                + "\n"
            )
            self.respond(200, body, "text/plain; version=0.0.4")
        else:
            self.respond(404, {"error": "not found"})

    def do_POST(self):
        """Bearer認証した固定actionを同期実行し、安全な結果だけを返す。

        要求bodyのサイズとactionを制限してからrunへ渡す。成功応答は処理の
        完了後であり、受付だけの202ではない。失敗ログは例外型と実装内の
        位置へ限定し、SQL・接続先・driverの生メッセージを出力しない。
        """
        if self.path != "/run":
            return self.respond(404, {"error": "not found"})
        expected = "Bearer " + os.environ["WORKER_TOKEN"]
        if not secrets.compare_digest(
            self.headers.get("Authorization", ""), expected
        ):
            return self.respond(401, {"error": "authentication required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                return self.respond(400, {"error": "invalid body size"})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                return self.respond(400, {"error": "invalid body"})
            action = payload.get("action")
            if action == "sql-load":
                if set(payload) != {"action", "batch_id", "observed_at"}:
                    return self.respond(400, {"error": "invalid SQL request"})
                from scripts.sql_batch_worker import apply_identifier

                try:
                    apply_identifier(
                        payload["batch_id"], payload["observed_at"]
                    )
                except ValueError:
                    return self.respond(400, {"error": "invalid SQL request"})
                return self.respond(200, run(action, payload))
            if action not in {"ingest", "medallion", "verify"}:
                return self.respond(400, {"error": "unknown action"})
            self.respond(200, run(action))
        except WorkerBlockedError:
            self.respond(
                409,
                {
                    "error": "WorkerBlockedError",
                    "message": (
                        "Confirm Trino query completion, "
                        "then restart the worker."
                    ),
                },
            )
        except Exception as error:
            # 公開可能と決めた文言だけを返し、driverの生例外を転送しない。
            message = "Job failed; check source contract and service health."
            controlled = ("Trino query failed:", "MES/ERP check failed:")
            if str(error).startswith(controlled):
                message = str(error)
            # 診断には実装内の関数/行を残し、業務値やSQL本文は記録しない。
            frames = [
                {"function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(error.__traceback__)
                if Path(frame.filename).parent == ROOT
            ]
            print(
                json.dumps(
                    {
                        "event": "job.failed",
                        "error": type(error).__name__,
                        "locations": frames,
                    }
                ),
                flush=True,
            )
            self.respond(
                500,
                {
                    "error": type(error).__name__,
                    "message": message,
                },
            )


def main():
    """常駐workerを起動するか、固定actionを既存workerへ依頼する。

    serveは基盤の準備完了後にHTTPを開始する。ingest等のCLIは同じ常駐
    プロセスの/runを使い、HTTP利用者と排他する。bootstrapだけは不足分の
    準備として直接実行する。CLIの待機切れはサーバー側の停止を意味しない。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["serve", "bootstrap", "ingest", "medallion", "verify"],
        help=(
            "serve=初期化後にworker APIを常駐。bootstrap=不足する基盤を準備。"
            "ingest=完成原本を取り込む。medallion=取込と派生表を更新。"
            "verify=隔離namespaceで合成期待値を検査"
        ),
    )
    args = parser.parse_args()
    if args.action == "serve":
        deadline = time.monotonic() + 180
        while True:
            try:
                bootstrap()
                break
            except Exception as error:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        "Bootstrap readiness deadline."
                    ) from None
                print(
                    "Waiting for core services: " + type(error).__name__,
                    flush=True,
                )
                time.sleep(3)
        STATE["ready"] = True
        ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
    elif args.action == "bootstrap":
        print(json.dumps(run(args.action), ensure_ascii=False), flush=True)
    else:
        # 別プロセスで直接取込せず、n8n等と同じ常駐workerのlockを使う。
        response = HTTP.post(
            "http://127.0.0.1:8000/run",
            headers={"Authorization": "Bearer " + os.environ["WORKER_TOKEN"]},
            json={"action": args.action},
            timeout=300,
        )
        if response.status_code != 200:
            raise SystemExit(
                "Worker job failed; inspect service health "
                "and source contracts."
            )
        print(json.dumps(response.json(), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
