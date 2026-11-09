"""Deterministic synthetic anomalies; no I/O or downstream execution."""

import copy
import csv
import io
import math
import random
import struct
import zlib
from datetime import datetime, timedelta
from hashlib import sha256

ROW_SCENARIOS = [
    (
        "missing_required",
        "必須キー欠損",
        "mes_events",
        "mes_events.event_idを空文字へ変えます。",
        "最新版のキー欠損をmissing_keyとして隔離します。",
    ),
    (
        "invalid_number",
        "数値の型不正",
        "mes_events",
        "mes_events.good_qtyをnot-a-numberへ変えます。",
        "数値へ変換できずinvalid_typeとして隔離します。",
    ),
    (
        "negative_quantity",
        "負の数量",
        "mes_events",
        "mes_events.good_qtyを-7へ変えます。",
        "負数をnegative_quantityとして隔離し、生産集計から除きます。",
    ),
    (
        "duplicate_row",
        "完全重複",
        "mes_events",
        "選択した製造実績の全列を同じ値のまま複製します。",
        "同じevent_idは1件採用され、Goldを二重計上しません。",
    ),
    (
        "conflicting_revision",
        "後着の有効改訂",
        "mes_events",
        "製造実績を複製し、good_qtyを10増、updated_atを400日後へ変えます。",
        "有効な元行では新しい改訂を採用し、Goldの良品数が10増えます。",
    ),
    (
        "invalid_latest",
        "最新版だけ不正",
        "mes_events",
        "同じevent_idを複製し、更新を400日後、good_qtyを型不正へ変えます。",
        "最新版をinvalid_typeで隔離し、古い有効版へは戻しません。",
    ),
    (
        "broken_reference",
        "指図への参照切れ",
        "mes_events",
        "mes_events.work_order_idの先頭にUNKNOWN-を付けます。",
        "参照先がなくunknown_work_orderとして隔離します。",
    ),
    (
        "invalid_datetime",
        "日時の型不正",
        "mes_events",
        "mes_events.event_timeをnot-a-timestampへ変えます。",
        "日時へ変換できずinvalid_timestampとして隔離します。",
    ),
    (
        "late_arrival",
        "過去イベントの遅延到着",
        "mes_events",
        "event_timeを30日前、updated_atを7日後へずらします。",
        "有効な遅延到着として過去日のGoldへ集計します。自動隔離はしません。",
    ),
    (
        "out_of_order",
        "ファイル内の順序逆転",
        "mes_events",
        "選択した製造実績の掲載順を逆転します。最低2行を対象にします。",
        "順序が変わっても主キーとupdated_atによる採用・Goldは変わりません。",
    ),
    (
        "clock_skew",
        "更新時計が1年進む",
        "mes_events",
        "mes_events.updated_atを365日後へずらします。",
        "将来時計も現行SQLでは受理します。自動隔離・自動警告はありません。",
    ),
    (
        "reject_spike",
        "不良数の急増",
        "mes_events",
        "mes_events.reject_qtyを500へ増やします。good_qtyは保持します。",
        "Goldの不良数が増え歩留まりが下がります。業務異常の自動警告はありません。",
    ),
    (
        "long_downtime",
        "12時間の設備停止",
        "mes_downtime",
        "mes_downtime.end_timeをstart_timeの12時間後へ変えます。",
        "停止時間を720分として開始日に集計します。自動隔離はしません。",
    ),
    (
        "overproduction",
        "計画を大幅に超える良品",
        "mes_events",
        "mes_events.good_qtyを100000へ変えます。",
        "Goldの良品数が増え、計画達成率が100%を超え得ます。自動隔離はしません。",
    ),
    (
        "zero_production",
        "良品・不良ともゼロ",
        "mes_events",
        "mes_events.good_qtyとreject_qtyをともに0へ変えます。",
        "Goldの生産量が減り、日全体で総数0なら歩留まりはNULLになります。",
    ),
    (
        "delayed_shipment",
        "30日遅れの出荷",
        "erp_shipments",
        "erp_shipments.shipped_atを30日後へ変えます。",
        "Goldの遅延出荷数・遅延数量へ計上します。自動隔離はしません。",
    ),
    (
        "missing_shipment",
        "受注の出荷が全て欠落",
        "erp_shipments",
        "選択した受注に属する出荷レコードを全て取り除きます。",
        "その受注のGoldは出荷0・未出荷数=受注数・履行率0%になります。",
    ),
    (
        "over_shipment",
        "受注を大幅に超える出荷",
        "erp_shipments",
        "erp_shipments.shipped_qtyを100000へ変えます。",
        "履行率が100%を超え、未出荷数は0で下限処理します。自動隔離はしません。",
    ),
    (
        "invalid_price",
        "単価の型不正",
        "erp_sales_orders",
        "erp_sales_orders.unit_priceをnot-a-priceへ変えます。",
        "受注をinvalid_typeで隔離し、その出荷も参照切れとして隔離します。",
    ),
]
FILE_SCENARIOS = [
    (
        "extra_column",
        "余分なCSV列",
        "csv/erp_products.csv",
        "製品CSVへunexpected_column列とsyntheticという値を追加します。",
        "readyのhashは正しくても、列契約不一致で取り込みを拒否します。SQLには影響しません。",
    ),
    (
        "missing_column",
        "CSV列の欠落",
        "csv/erp_products.csv",
        "製品CSVからproduct_name列をヘッダー・全行ごと除きます。",
        "readyのhashは正しくても、列契約不一致で取り込みを拒否します。SQLには影響しません。",
    ),
    (
        "missing_ready",
        "完了マーカー欠落",
        "csv/erp_products.csv",
        "製品CSVは完成させ、その.readyファイルだけを作りません。",
        "そのファイルは取り込み対象から無視されます。他ファイルは別に処理され得ます。",
    ),
    (
        "wrong_checksum",
        "完了ハッシュ不一致",
        "csv/erp_products.csv",
        "製品CSVの.readyを、実ファイルと異なる64桁のSHA-256へ変えます。",
        "完了hashの不一致で取り込みを拒否します。SQLには影響しません。",
    ),
    (
        "truncated_json",
        "途中で切れたJSON",
        "json/mes_events.jsonl",
        "製造実績JSONLの末尾を切り、最後のJSONを未完にします。",
        "内容のready hashは一致しますが、JSON解析で取り込みを拒否します。",
    ),
    (
        "invalid_encoding",
        "UTF-8以外のバイト",
        "json/mes_events.jsonl",
        "製造実績JSONLの先頭へUTF-8として不正なバイトを追加します。",
        "内容のready hashは一致しますが、UTF-8復号で取り込みを拒否します。",
    ),
    (
        "corrupt_image",
        "破損した検査画像",
        "images/inspection.png",
        "合成PNGの内容を画像として読めない固定バイトへ置き換えます。",
        "内容のready hashは一致しますが、画像検証で取り込みを拒否します。",
    ),
]
CONFLICTS = [
    {"extra_column", "missing_column"},
    {"missing_ready", "wrong_checksum"},
    {"truncated_json", "invalid_encoding"},
    {
        "invalid_number",
        "negative_quantity",
        "overproduction",
        "zero_production",
    },
    {"reject_spike", "zero_production"},
    {"invalid_datetime", "late_arrival"},
    {"clock_skew", "late_arrival"},
    {"missing_shipment", "delayed_shipment", "over_shipment"},
]


def catalog():
    """Describe real mutations and current downstream expectations."""
    result = []
    for category, definitions in [
        ("row", ROW_SCENARIOS),
        ("file", FILE_SCENARIOS),
    ]:
        for identifier, label, target, description, effect in definitions:
            result.append(
                {
                    "id": identifier,
                    "label": label,
                    "description": label
                    + "を選択した合成ソースへ注入します。",
                    "category": category,
                    "targets": [target],
                    "supported_destinations": (
                        ["landing", "both"]
                        if category == "file"
                        else ["landing", "sqlserver", "both"]
                    ),
                    "expected_effect": effect,
                    "requires_images": identifier == "corrupt_image",
                    "requires_file_consent": category == "file",
                }
            )
    return result


def presets():
    """Return compatible examples; GUI selections remain explicit."""
    definitions = [
        ("clean", "正常データ", "clean", []),
        ("legacy_edge", "従来のedgeデータ", "edge", []),
        (
            "quality",
            "型・必須・参照エラー",
            "clean",
            [
                "missing_required",
                "invalid_number",
                "broken_reference",
                "invalid_datetime",
            ],
        ),
        (
            "revisions",
            "重複・改訂・順序逆転",
            "clean",
            ["duplicate_row", "conflicting_revision", "out_of_order"],
        ),
        (
            "factory_losses",
            "不良増・長時間停止",
            "clean",
            ["reject_spike", "long_downtime"],
        ),
        ("shipping_delay", "出荷遅延", "clean", ["delayed_shipment"]),
        (
            "transfer_faults",
            "未完了・文字コード異常",
            "clean",
            ["missing_ready", "invalid_encoding"],
        ),
    ]
    return [
        {
            "id": identifier,
            "label": label,
            "base_scenario": base,
            "anomalies": selected,
        }
        for identifier, label, base, selected in definitions
    ]


def validate_selection(
    anomalies=(),
    destination="landing",
    images=False,
    allow_file_faults=False,
    anomaly_rate=10,
):
    """Reject unknown, repeated, incompatible or unconsented selections."""
    if not isinstance(anomalies, (list, tuple)) or not all(
        isinstance(item, str) for item in anomalies
    ):
        raise ValueError("anomalies must be a list of scenario IDs.")
    if len(set(anomalies)) != len(anomalies):
        raise ValueError("Duplicate anomaly IDs are not allowed.")
    if type(anomaly_rate) is not int or not 1 <= anomaly_rate <= 100:
        raise ValueError("anomaly_rate must be an integer in 1..100.")
    if type(images) is not bool or type(allow_file_faults) is not bool:
        raise ValueError("Scenario consent and image flags must be booleans.")
    if destination not in {"landing", "sqlserver", "both"}:
        raise ValueError("Unknown anomaly destination.")
    definitions = {item["id"]: item for item in catalog()}
    for identifier in anomalies:
        if identifier not in definitions:
            raise ValueError("Unknown anomaly ID.")
        item = definitions[identifier]
        if destination not in item["supported_destinations"]:
            raise ValueError("File faults require a landing destination.")
        if item["requires_images"] and not images:
            raise ValueError("corrupt_image requires synthetic images.")
        if item["requires_file_consent"] and not allow_file_faults:
            raise ValueError("Explicit file fault consent is required.")
    for conflict in CONFLICTS:
        if len(set(anomalies) & conflict) > 1:
            raise ValueError("Selected anomalies modify incompatible fields.")
    return tuple(sorted(anomalies))


def _chosen(rows, rate, seed, batch, identifier, minimum=1):
    """Pick stable row positions using an independent RNG per scenario."""
    if not rows:
        return []
    material = f"{seed}:{batch}:{identifier}".encode()
    rng = random.Random(int.from_bytes(sha256(material).digest(), "big"))
    count = min(len(rows), max(minimum, math.ceil(len(rows) * rate / 100)))
    return sorted(rng.sample(range(len(rows)), count))


def _shift(value, **delta):
    """Move a valid source timestamp while retaining ISO-8601 UTC output."""
    stamp = datetime.fromisoformat(value)
    return (stamp + timedelta(**delta)).isoformat().replace("+00:00", "Z")


def apply_rows(data, anomalies, anomaly_rate=10, seed=42, batch=1):
    """Copy raw rows, then apply selected transformations in sorted order."""
    result = copy.deepcopy(data)
    changes = []
    tables = {item[0]: item[2] for item in ROW_SCENARIOS}
    for identifier in sorted(anomalies):
        if identifier not in tables:
            continue
        table = tables[identifier]
        rows = result[table]
        indices = _chosen(rows, anomaly_rate, seed, batch, identifier)
        selected = [rows[index] for index in indices]
        affected = len(selected)
        if identifier == "missing_shipment":
            orders = result["erp_sales_orders"]
            chosen = _chosen(orders, anomaly_rate, seed, batch, identifier)
            keys = {orders[index]["sales_order_id"] for index in chosen}
            result[table] = [
                row for row in rows if row["sales_order_id"] not in keys
            ]
            affected = len(rows) - len(result[table])
        elif identifier == "out_of_order":
            indices = _chosen(rows, anomaly_rate, seed, batch, identifier, 2)
            values = [rows[index] for index in reversed(indices)]
            for index, row in zip(indices, values):
                rows[index] = row
            affected = len(indices)
        elif identifier in {
            "duplicate_row",
            "conflicting_revision",
            "invalid_latest",
        }:
            for original in selected:
                revision = dict(original)
                if identifier != "duplicate_row":
                    revision["updated_at"] = _shift(
                        revision["updated_at"], days=400
                    )
                    revision["good_qty"] = (
                        "not-a-number"
                        if identifier == "invalid_latest"
                        else str(int(revision["good_qty"]) + 10)
                        if revision["good_qty"].isdigit()
                        else "10"
                    )
                rows.append(revision)
        else:
            for row in selected:
                if identifier == "missing_required":
                    row["event_id"] = ""
                elif identifier == "invalid_number":
                    row["good_qty"] = "not-a-number"
                elif identifier == "negative_quantity":
                    row["good_qty"] = "-7"
                elif identifier == "broken_reference":
                    row["work_order_id"] = "UNKNOWN-" + row["work_order_id"]
                elif identifier == "invalid_datetime":
                    row["event_time"] = "not-a-timestamp"
                elif identifier == "late_arrival":
                    row["event_time"] = _shift(row["event_time"], days=-30)
                    row["updated_at"] = _shift(row["updated_at"], days=7)
                elif identifier == "clock_skew":
                    row["updated_at"] = _shift(row["updated_at"], days=365)
                elif identifier == "reject_spike":
                    row["reject_qty"] = "500"
                elif identifier == "long_downtime":
                    row["end_time"] = _shift(row["start_time"], hours=12)
                elif identifier == "overproduction":
                    row["good_qty"] = "100000"
                elif identifier == "zero_production":
                    row["good_qty"] = "0"
                    row["reject_qty"] = "0"
                elif identifier == "delayed_shipment":
                    row["shipped_at"] = _shift(row["shipped_at"], days=30)
                elif identifier == "over_shipment":
                    row["shipped_qty"] = "100000"
                elif identifier == "invalid_price":
                    row["unit_price"] = "not-a-price"
        changes.append(
            {"id": identifier, "table": table, "affected": affected}
        )
    return result, changes


def apply_files(payloads, anomalies):
    """Return immutable-publishable bytes and intentional marker policies."""
    result = dict(payloads)
    markers = {}
    faults = []
    definitions = {item[0]: item for item in FILE_SCENARIOS}
    for identifier in sorted(anomalies):
        if identifier not in definitions:
            continue
        _, _, path, _, effect = definitions[identifier]
        if path not in result:
            raise ValueError("The selected file anomaly has no payload.")
        if identifier in {"extra_column", "missing_column"}:
            reader = csv.DictReader(io.StringIO(result[path].decode()))
            columns = list(reader.fieldnames)
            rows = list(reader)
            if identifier == "extra_column":
                columns.append("unexpected_column")
                for row in rows:
                    row["unexpected_column"] = "synthetic"
            else:
                columns.remove("product_name")
                for row in rows:
                    row.pop("product_name")
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(
                stream, fieldnames=columns, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)
            result[path] = stream.getvalue().encode()
        elif identifier in {"missing_ready", "wrong_checksum"}:
            markers[path] = identifier
        elif identifier == "truncated_json":
            result[path] = result[path].rstrip(b"\n")[:-1]
        elif identifier == "invalid_encoding":
            result[path] = b"\xff" + result[path]
        elif identifier == "corrupt_image":
            result[path] = b"synthetic-corrupt-image"
        faults.append(
            {"id": identifier, "path": path, "expected_effect": effect}
        )
    return result, markers, faults


def ready_payload(payload, policy=None):
    """Return a truthful marker, an intentionally wrong one, or no marker."""
    if policy == "missing_ready":
        return None
    digest = sha256(payload).hexdigest()
    if policy == "wrong_checksum":
        digest = ("1" if digest[0] == "0" else "0") + digest[1:]
    return (digest + "\n").encode()


def synthetic_png(seed, size=96):
    """Produce the same artificial RGB fixture as the source GUI."""

    def chunk(kind, payload):
        """Encode one PNG chunk without an image-library dependency."""
        checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return (
            struct.pack("!I", len(payload))
            + kind
            + payload
            + struct.pack("!I", checksum)
        )

    pixels = io.BytesIO()
    for y in range(size):
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
