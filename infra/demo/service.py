"""GUI/APIから、検証用のMES・ERP素材だけを生成・公開する。

読む順序は generate_batch → _publish_batch → publish_source。
値の生成・SQLの検証はCLIと共通化し、各run/batchの業務キーを分離する。
出力はCSV/JSON/人工画像、必要時の専用SQL schema、秘密を含まないmanifest。
Iceberg取込・メダリオン・workflowはここから呼ばず、別操作に分ける。
ファイル公開とSQL commitをまとめるtransactionは提供しない。
"""

import fcntl
import importlib.util
import io
import json
import os
import re
import struct
import tempfile
import zlib
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "demo_source_generator", ROOT / "scripts/generate-demo.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)
SCENARIOS = GENERATOR.SCENARIOS
ID_COLUMNS = {
    "product_id",
    "work_order_id",
    "sales_order_id",
    "shipment_id",
    "event_id",
    "downtime_id",
}
SQL_KEYS = {
    "DEMO_SQLSERVER_" + suffix
    for suffix in [
        "HOST",
        "PORT",
        "DATABASE",
        "USER",
        "PASSWORD",
        "CA_FILE",
        "HOSTNAME_IN_CERTIFICATE",
    ]
} | {"SOURCE_SQLSERVER_DATABASE"}


def validate_options(run_id, batch, seed, orders, days, scenario, destination):
    """run/batchの識別子、件数、期間、出力先を副作用なしで検査する。

    正式な増分名を作れる12桁IDと4桁batchに限定する。boolを整数として
    通さず、GUI外から呼ばれても同じ件数境界を維持する。
    """
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[0-9a-f]{12}", run_id
    ):
        raise ValueError(
            "run_id must contain 12 lowercase hexadecimal digits."
        )
    for name, value, lower, upper in [
        ("batch", batch, 1, 9999),
        ("seed", seed, 0, 2147483647),
        ("orders", orders, 1, 1000),
        ("days", days, 1, 366),
    ]:
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} must be an integer in {lower}..{upper}.")
    if scenario not in {"clean", "edge"}:
        raise ValueError("scenario must be clean or edge.")
    if destination not in {"landing", "sqlserver", "both"}:
        raise ValueError("Unknown source destination.")


def namespaced_data(run_id, batch, seed, orders, days, scenario):
    """生成済み6契約の主キー・参照をrun/batch単位で分離して返す。

    同じprefixを親子のIDへ付け、改訂順と異常な参照も保持する。
    業務日の意味は変えず、更新時刻へbatch差分だけを付ける。
    """
    data = GENERATOR.build_data(seed, orders, days, scenario)
    namespace = f"G{run_id}B{batch:04d}-"
    # 同じID変換を主キーと参照へ適用し、別run/batchとの偶発的な結合を防ぐ。
    # 過去の業務日や改訂順は保持し、追加生成を同じ業務行の更新にしない。
    offset = timedelta(seconds=batch - 1)
    for rows in data.values():
        for row in rows:
            for key in ID_COLUMNS & row.keys():
                row[key] = namespace + row[key]
            stamp = datetime.fromisoformat(row["updated_at"])
            row["updated_at"] = (
                (stamp + offset).isoformat().replace("+00:00", "Z")
            )
    return data


def synthetic_png(seed, size=96):
    """seedから再現できる人工検査模様のRGB PNGバイト列を返す。

    実写真や画像理解の結果は使わず、標準ライブラリーだけで組み立てる。
    入力画像の読込み・ネットワーク接続・ファイル保存は行わない。
    """

    def chunk(kind, payload):
        """PNG chunkの長さ・種類・payload・CRCを規格の順に並べて返す。"""
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return (
            struct.pack("!I", len(payload))
            + kind
            + payload
            + struct.pack("!I", checksum)
        )

    pixels = io.BytesIO()
    for y in range(size):
        # PNGの各走査線へ「フィルターなし」のbyteを付け、RGBを続ける。
        pixels.write(b"\x00")
        for x in range(size):
            tile = (x // 12 + y // 12 + seed) % 2
            pixels.write(bytes((40 + 140 * tile, 90 + seed % 80, 180)))
    header = struct.pack("!2I5B", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels.getvalue()))
        + chunk(b"IEND", b"")
    )


def sql_settings(settings):
    """DEMO接続値だけを選び、CLIと同じprivate設定検査を通す。

    一時的な0600ファイルを介してliteral parserを再利用し、処理後は削除する。
    返す辞書はSQL実行内部用であり、APIやmanifestへ返さない。
    """
    values = {
        key: str(value)
        for key, value in (
            settings if settings is not None else os.environ
        ).items()
        if key in SQL_KEYS
    }
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("SQL settings must be single-line values.")
    with tempfile.TemporaryDirectory(prefix="demo-source-settings-") as name:
        path = Path(name) / "site.env"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(
                "\n".join(key + "=" + value for key, value in values.items())
                + "\n"
            )
        return GENERATOR.private_demo_settings(path)


def publish_source(path, payload, policy=None):
    """原本を新規公開し、選択policyのreadyを公開して内容hashを返す。

    標準policyでは正しいSHA-256 markerを使う。意図したfile faultの試験では
    marker欠落や不一致もあり得るため、素材成功と後続取込成功を区別する。
    """
    GENERATOR.publish_file(path, payload)
    digest = sha256(payload).hexdigest()
    marker = SCENARIOS.ready_payload(payload, policy)
    if marker is not None:
        GENERATOR.publish_file(path.with_name(path.name + ".ready"), marker)
    return digest


def generate_batch(
    output,
    *,
    run_id,
    batch=1,
    seed=42,
    orders=20,
    days=7,
    scenario="edge",
    destination="landing",
    images=False,
    allow_sql_write=False,
    settings=None,
    anomalies=(),
    anomaly_rate=10,
    allow_file_faults=False,
):
    """1batchの入力を検査し、同じ出力dirのsource writerを排他する。

    SQL指定には明示同意と専用接続の検査が必要。検査後に出力dirを準備し、
    file lockを取って公開する。返すmanifestは件数・hash等の非秘密情報だけ。
    lockはこのdirを使う素材生成間のもので、全分析writerの排他ではない。
    """
    validate_options(run_id, batch, seed, orders, days, scenario, destination)
    anomalies = SCENARIOS.validate_selection(
        anomalies, destination, images, allow_file_faults, anomaly_rate
    )
    if type(images) is not bool or type(allow_sql_write) is not bool:
        raise ValueError("images and allow_sql_write must be booleans.")
    sql = destination in {"sqlserver", "both"}
    if sql and not allow_sql_write:
        raise ValueError("Explicit SQL write consent is required.")
    if destination == "sqlserver" and images:
        raise ValueError("Synthetic images require a landing destination.")
    # 工程1: 出力先を作る前に入力/同意/専用SQL設定を検査する。
    values = sql_settings(settings) if sql else None
    output = GENERATOR.output_directory(Path(output))
    for name in ["csv", "json", "images", "manifests"]:
        child = output / name
        if child.is_symlink() or (child.exists() and not child.is_dir()):
            raise ValueError("Source output paths must be real directories.")
    output.mkdir(parents=True, exist_ok=True)
    # 工程2: sessionを跨ぐfile lockで、同じlandingの素材公開を直列化する。
    lock_path = output / ".demo-gui.lock"
    descriptor = os.open(
        lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another source batch is running.") from None
        return _publish_batch(
            output,
            run_id,
            batch,
            seed,
            orders,
            days,
            scenario,
            destination,
            images,
            values,
            anomalies,
            anomaly_rate,
        )
    finally:
        os.close(descriptor)


def _publish_batch(
    output,
    run_id,
    batch,
    seed,
    orders,
    days,
    scenario,
    destination,
    images,
    values,
    anomalies,
    anomaly_rate,
):
    """入力検査・lock取得済みの1batchを生成し、manifestを最後に公開する。

    生成→行異常→serialize→file異常→名前の重複検査→SQL→fileの順に進む。
    既存runの上書きはしない。bothではSQL commit後のfile公開失敗があり得て、
    manifestがないことだけからSQLも未実行だったとは判断できない。
    """
    suffix = f"__{run_id}_{batch:04d}"
    manifest_path = output / "manifests" / ("demo" + suffix + ".json")
    if manifest_path.exists() or manifest_path.is_symlink():
        raise ValueError("This batch has already been published.")
    data = namespaced_data(run_id, batch, seed, orders, days, scenario)
    changes = []
    if anomalies:
        # 行の異常はserialize前、転送/file異常はserialize後に適用する。
        data, changes = SCENARIOS.apply_rows(
            data, anomalies, anomaly_rate, seed, batch
        )
    payloads = GENERATOR.encoded_sources(data)
    if images:
        payloads["images/inspection.png"] = synthetic_png(seed + batch)
    payloads, policies, faults = SCENARIOS.apply_files(payloads, anomalies)
    sources = []
    targets = []
    paths = {}
    for source in GENERATOR.contracts():
        original = Path(source["path"])
        relative = original.with_name(original.stem + suffix + original.suffix)
        payload = payloads[source["path"]]
        paths[source["path"]] = relative.as_posix()
        sources.append(
            {
                "table": source["table"],
                "path": relative.as_posix(),
                "rows": len(data[source["table"]]),
                "sha256": sha256(payload).hexdigest(),
            }
        )
        targets.append(
            (output / relative, payload, policies.get(source["path"]))
        )
    image_sources = []
    if images:
        relative = Path("images") / ("inspection" + suffix + ".png")
        payload = payloads["images/inspection.png"]
        paths["images/inspection.png"] = relative.as_posix()
        image_sources.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256(payload).hexdigest(),
            }
        )
        targets.append(
            (output / relative, payload, policies.get("images/inspection.png"))
        )
    # 工程3: SQL commitの前に同名出力を拒否し、既存runを上書きしない。
    if destination != "sqlserver" and any(
        path.exists() or path.with_name(path.name + ".ready").exists()
        for path, _, _ in targets
    ):
        raise ValueError("Batch filenames already exist; choose a new run.")
    schema = GENERATOR.write_sql(data, values) if values is not None else None
    if destination != "sqlserver":
        # SQLとfileは別の確定点。bothの片側失敗を一括rollbackとは扱わない。
        for path, payload, policy in targets:
            publish_source(path, payload, policy)
    result = {
        "format_version": 1,
        "run_id": run_id,
        "batch": batch,
        "seed": seed,
        "orders": orders,
        "days": days,
        "scenario": scenario,
        "destination": destination,
        "sources": sources,
        "images": image_sources,
        "sql_schema": schema,
        "manifest": manifest_path.relative_to(output).as_posix(),
    }
    if anomalies:
        result["diagnostics"] = {
            "anomalies": list(anomalies),
            "anomaly_rate": anomaly_rate,
            "row_changes": changes,
            "file_faults": [
                {**fault, "path": paths[fault["path"]]} for fault in faults
            ],
            "intentional_file_faults": bool(faults),
        }
    # 工程4: 全対象を公開できた後にだけ、非秘密の最終manifestを公開する。
    # source-onlyなので、ここからworkerやschedulerを呼び出さない。
    GENERATOR.publish_file(
        manifest_path, (json.dumps(result, indent=2) + "\n").encode()
    )
    return result
