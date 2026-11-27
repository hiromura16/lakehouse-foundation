"""閉じた1表の合成 Phase 2B 副本から、read-only 証跡を2種類返す。

呼出側の署名済み所有権・運用の環境認証ではない。協調する合成fixtureの
作成者が別に算出した封印指紋を要求し、接続先や外部アダプターを受けない。
Consumer の最終イベント時刻/最終エラー、実 Kafka位置は測定しない。
ファイルの上限は符号化済みの読取りbyte数で、非信頼の圧縮入力の展開量を
保証しない。協調して閉じた合成ハーネスの封印済み副本にのみ使う。
"""

import json
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlsplit

from cdc_contract import ContractError, bounded_int, fields, require, validate
from cdc_observation import KINDS, MAX_AGE, MAX_ID, VERSION
from cdc_registration import (
    FIXTURE_ID,
    HEX_DIGEST,
    assessment,
    compile_spec,
    digest,
    match,
)

SEAL = "fixture-receipt.json"
MAX_FILES = 128
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_BYTES = 24 * 1024 * 1024
MAX_ROWS = 64
LAYERS = {"raw": "raw", "current": "current", "silver": "silver"}


def require_owned(info, *, directory=False):
    """0700の所有dir、または0600の単独ファイルだけを受け入れる。"""
    require(
        info.st_uid == os.getuid()
        and (
            stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
            if directory
            else stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
        ),
        "UNSAFE_FIXTURE_ARTIFACT",
    )
    return info


def contains_path(root, location):
    """Icebergのfile URIは封印倉庫の内側だけ。remote schemeには触れない。"""
    require(isinstance(location, str), "UNSAFE_ICEBERG_LOCATION")
    try:
        uri = urlsplit(location)
        path = Path(unquote(uri.path))
    except (ValueError, UnicodeError):
        require(False, "UNSAFE_ICEBERG_LOCATION")
    require(
        uri.scheme == "file"
        and not uri.netloc
        and not uri.query
        and not uri.fragment
        and path.is_absolute()
        and path.is_relative_to(root)
        and ".." not in path.parts,
        "UNSAFE_ICEBERG_LOCATION",
    )
    return path


@contextmanager
def open_directory(components, *, parent=None):
    """祖先も子もdirfd/O_NOFOLLOWで開き、linkを再解決しない。"""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open("/", flags) if parent is None else os.dup(parent)
    try:
        for name in components:
            require(name not in (".", "..", ""), "UNSAFE_FIXTURE_ARTIFACT")
            opened = os.open(name, flags, dir_fd=current)
            os.close(current)
            current = opened
            if parent is not None:
                require_owned(os.fstat(current), directory=True)
        require_owned(os.fstat(current), directory=True)
        yield current
    finally:
        os.close(current)


def enumerate_owned(root):
    """fd起点の実entryだけを有限個訪問し、swapされてもlinkを辿らない。"""
    require(root.is_absolute(), "UNSAFE_FIXTURE_DIRECTORY")
    expected = {SEAL, "state.db", "catalog.db", "warehouse"}
    result = {"state.db", "catalog.db"}
    directories = 0
    used = 0

    def walk(descriptor, relatives):
        nonlocal used, directories
        require(len(relatives) <= 6, "FIXTURE_ARTIFACT_LIMIT")
        directories += 1
        require(directories <= 32, "FIXTURE_ARTIFACT_LIMIT")
        with os.scandir(descriptor) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                child = (*relatives, entry.name)
                if stat.S_ISDIR(info.st_mode):
                    with open_directory(
                        (entry.name,), parent=descriptor
                    ) as sub:
                        walk(sub, child)
                    continue
                require(
                    Path(entry.name).suffix in (".json", ".avro", ".parquet"),
                    "UNSAFE_ICEBERG_ARTIFACT_TYPE",
                )
                used += require_owned(info).st_size
                require(used <= MAX_BYTES, "FIXTURE_ARTIFACT_LIMIT")
                result.add("/".join(child))
                require(len(result) <= MAX_FILES, "FIXTURE_ARTIFACT_LIMIT")

    with open_directory(root.parts[1:]) as owner:
        with os.scandir(owner) as entries:
            seen = set()
            for entry in entries:
                require(
                    entry.name in expected, "FIXTURE_ARTIFACT_SET_MISMATCH"
                )
                seen.add(entry.name)
                require_owned(
                    entry.stat(follow_symlinks=False),
                    directory=entry.name == "warehouse",
                )
        require(seen == expected, "FIXTURE_ARTIFACT_SET_MISMATCH")
        with open_directory(("warehouse",), parent=owner) as warehouse:
            walk(warehouse, ("warehouse",))
    return result


def read_owned(root, name, limit=MAX_FILE_BYTES):
    """パスの各段でlinkを拒否して、一つのbounded fileを採取する。"""
    supplied = Path(name)
    parts = supplied.parts
    require(
        root.is_absolute()
        and not supplied.is_absolute()
        and parts
        and ".." not in parts,
        "UNSAFE_FIXTURE_ARTIFACT",
    )
    with open_directory(root.parts[1:]) as owner:
        with open_directory(parts[:-1], parent=owner) as target:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(
                os.open(parts[-1], flags, dir_fd=target), "rb"
            ) as stream:
                info = require_owned(os.fstat(stream.fileno()))
                require(info.st_size <= limit, "FIXTURE_ARTIFACT_LIMIT")
                content = stream.read(info.st_size + 1)
            require(len(content) == info.st_size, "FIXTURE_ARTIFACT_CHANGED")
            return content


def sealed_contents(root, files):
    """その後のSDKに実ファイルを再openさせないため、固定バイトを保つ。"""
    total = 0
    result = {}
    for name in sorted(files):
        content = read_owned(
            root, name, min(MAX_FILE_BYTES, MAX_BYTES - total)
        )
        total += len(content)
        result[name] = content
    return result


def read_seal(root, fingerprint, expected):
    """外部のraw封印指紋が一致する合成snapshotのみ受け付ける。"""
    data = read_owned(root, SEAL, 256 * 1024)
    require(
        match(HEX_DIGEST, fingerprint)
        and sha256(data).hexdigest() == fingerprint,
        "FIXTURE_SEAL_MISMATCH",
    )

    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "INVALID_FIXTURE_SEAL")
            result[key] = value
        return result

    record = json.loads(data.decode(), object_pairs_hook=pairs)
    fields(
        record,
        set(expected) | {"observed_at", "files"},
        "INVALID_FIXTURE_SEAL",
    )
    require(
        all(
            type(record[key]) is type(value) and record[key] == value
            for key, value in expected.items()
        ),
        "FIXTURE_SCOPE_MISMATCH",
    )
    require(
        isinstance(record["files"], dict)
        and all(match(HEX_DIGEST, h) for h in record["files"].values()),
        "INVALID_FIXTURE_ARTIFACT_HASHES",
    )
    return record


def read_database(content):
    """sealedバイトをメモリ接続でdeserializeし、ファイルは開かない。"""
    header = content[:20]
    require(
        len(header) == 20
        and header[:16] == b"SQLite format 3\x00"
        and header[18:20] == b"\x01\x01",
        "INVALID_FIXTURE_DATABASE",
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(content)
        return connection
    except BaseException:
        connection.close()
        raise


def allow_only_tables(connection, expected):
    """実tableだけを読み、SQL view/virtual tableや外部アクセスを拒否。"""
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    markers = ",".join("?" for _ in expected)
    schema = connection.execute(
        "SELECT type,name,sql FROM sqlite_schema WHERE name IN ("
        + markers
        + ")",
        tuple(expected),
    ).fetchall()
    require(
        len(schema) == len(expected)
        and all(
            kind == "table"
            and isinstance(ddl, str)
            and ddl.upper().startswith("CREATE TABLE ")
            for kind, _, ddl in schema
        ),
        "UNSAFE_FIXTURE_DATABASE_SCHEMA",
    )

    def only_read(action, table, _column, schema_name, trigger):
        if trigger is not None:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT or (
            action == sqlite3.SQLITE_READ
            and schema_name == "main"
            and table in expected
        ):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    connection.set_authorizer(only_read)


def local_checkpoint(content, state_identity, topic):
    """他topicやpartialな受理順を、正しい次位置に見せない。"""
    with closing(read_database(content)) as db:
        allow_only_tables(
            db, {"contract_experiment", "checkpoints", "accepted"}
        )
        require(
            db.execute(
                "SELECT identity FROM contract_experiment LIMIT 2"
            ).fetchall()
            == [(state_identity,)],
            "STATE_CONTRACT_MISMATCH",
        )
        rows = db.execute(
            "SELECT topic,part,off FROM checkpoints LIMIT 2"
        ).fetchall()
        accepted = db.execute(
            "SELECT seq,topic,part,off FROM accepted ORDER BY seq LIMIT ?",
            (MAX_ROWS + 1,),
        ).fetchall()
        if not rows:
            require(not accepted, "STATE_CHECKPOINT_MISMATCH")
            return []
        offset = rows[0][2]
        require(
            bounded_int(offset, 0, MAX_ROWS - 1)
            and type(rows[0][1]) is int
            and rows == [(topic, 0, offset)]
            and all(
                type(seq) is int and type(part) is int and type(off) is int
                for seq, _, part, off in accepted
            )
            and accepted == [(i + 1, topic, 0, i) for i in range(offset + 1)],
            "STATE_CHECKPOINT_MISMATCH",
        )
        return [{"partition": 0, "local_next": offset + 1}]


def table_targets(root, contents, route):
    """catalog pointerとParquet footerを読み、現在snapshotの実数を得る。"""
    from pyarrow import BufferReader
    from pyarrow.parquet import ParquetFile
    from pyiceberg.catalog.noop import NoopCatalog
    from pyiceberg.io import FileIO, InputFile
    from pyiceberg.serializers import FromInputFile
    from pyiceberg.table import StaticTable

    warehouse = root / "warehouse"
    allowed = {
        root / name: content
        for name, content in contents.items()
        if name.startswith("warehouse/")
    }

    class ReadBuffer(InputFile):
        """入力は封印済みメモリだけ。URIは識別にしか使わない。"""

        def __init__(self, location, data):
            super().__init__(location)
            self.data = data

        def __len__(self):
            return len(self.data)

        def exists(self):
            return True

        def open(self, seekable=True):
            return BufferReader(self.data)

    class LocalIO(FileIO):
        """metadata propertiesやURIで任意のFileIOへ切り替えない。"""

        def __init__(self, lane):
            super().__init__({})
            self.lane = lane

        def new_input(self, location):
            path = contains_path(self.lane, location)
            require(path in allowed, "UNSEALED_ICEBERG_ARTIFACT")
            return ReadBuffer(path.as_uri(), allowed[path])

        def new_output(self, _location):
            require(False, "FIXTURE_ADAPTER_READ_ONLY")

        def delete(self, _location):
            require(False, "FIXTURE_ADAPTER_READ_ONLY")

    with closing(read_database(contents["catalog.db"])) as db:
        allow_only_tables(db, {"iceberg_tables"})
        rows = db.execute(
            "SELECT catalog_name,table_namespace,table_name,metadata_location,"
            "iceberg_type FROM iceberg_tables ORDER BY table_name LIMIT 4"
        ).fetchall()
    require(
        len(rows) == len(LAYERS)
        and {row[2] for row in rows} == set(LAYERS.values())
        and all(
            row[:2] == ("contract-proof", "proof") and row[4] == "TABLE"
            for row in rows
        ),
        "FIXTURE_CATALOG_IDENTITY",
    )
    targets = {}
    for _, namespace, name, pointer, _ in rows:
        require(
            isinstance(pointer, str) and pointer.endswith(".metadata.json"),
            "INVALID_ICEBERG_METADATA_POINTER",
        )
        lane = warehouse / namespace / name
        io = LocalIO(lane)
        metadata = FromInputFile.table_metadata(io.new_input(pointer))
        require(
            contains_path(lane, metadata.location) == lane,
            "ICEBERG_TABLE_PATH_MISMATCH",
        )
        table = StaticTable(
            (namespace, name),
            metadata,
            pointer,
            io,
            NoopCatalog("fixture-read-only"),
        )
        snapshot = table.current_snapshot()
        count = None
        if snapshot is not None:
            require(
                bounded_int(snapshot.snapshot_id, 1, MAX_ID),
                "INVALID_ICEBERG_SNAPSHOT",
            )
            count = 0
            seen = set()
            for task in table.scan().plan_files():
                file = task.file
                normalized = contains_path(lane, file.file_path)
                require(
                    not task.delete_files
                    and file.file_format.value == "PARQUET"
                    and normalized.suffix == ".parquet"
                    and normalized not in seen
                    and len(seen) < MAX_FILES,
                    "UNSUPPORTED_FIXTURE_ICEBERG_LAYOUT",
                )
                seen.add(normalized)
                with io.new_input(file.file_path).open() as handle:
                    actual_rows = ParquetFile(handle).metadata.num_rows
                require(
                    actual_rows == file.record_count
                    and bounded_int(actual_rows, 0, MAX_ROWS)
                    and count + actual_rows <= MAX_ROWS,
                    "ICEBERG_ROW_COUNT_MISMATCH_OR_LIMIT",
                )
                count += actual_rows
        targets[name] = {
            "identity_sha256": digest(
                {"kind": name, **route["destinations"][name]}
            ),
            "snapshot_id": snapshot.snapshot_id if snapshot else None,
            "row_count": count,
        }
    return targets


def collect_local(
    directory, seal, desired, registration, fixture, generation, now
):
    """読み取りだけでcollector V2へ渡せるConsumer/Iceberg証跡を返す。"""
    require(
        match(FIXTURE_ID, fixture)
        and match(HEX_DIGEST, generation)
        and bounded_int(now, 0, MAX_ID),
        "INVALID_FIXTURE_SCOPE",
    )
    try:
        root = Path(os.path.abspath(directory))
        require(root.name == fixture, "UNSAFE_FIXTURE_DIRECTORY")
        plan = assessment(desired, registration, fixture)
        groups = compile_spec(desired)["connectors"]
        require(
            len(groups) == 1 and len(groups[0]["routes"]) == 1,
            "UNSUPPORTED_FIXTURE_CONTRACT",
        )
        group = groups[0]
        route = group["routes"][0]
        require(
            all(
                route["destinations"][layer]["namespace"] == "proof"
                and route["destinations"][layer]["table"] == LAYERS[layer]
                for layer in KINDS
            ),
            "UNSUPPORTED_FIXTURE_DESTINATIONS",
        )
        topic = route["topics"]["canonical"]
        expected = {
            "version": VERSION,
            "environment": "synthetic",
            "fixture_id": fixture,
            "generation": generation,
            "route_sha256": digest(route),
            "topic_sha256": digest(topic),
            "group_sha256": digest([fixture, group["topic.prefix"]]),
            "registration_sha256": plan["observation_sha256"],
            "desired_contract_sha256": plan["desired_contract_sha256"],
        }
        record = read_seal(root, seal, expected)
        age = record["observed_at"]
        require(
            bounded_int(age, 0, MAX_ID) and 0 <= now - age <= MAX_AGE,
            "FIXTURE_SEAL_EXPIRED_OR_FUTURE",
        )
        names = enumerate_owned(root)
        contents = sealed_contents(root, names)
        fingerprints = {
            name: sha256(data).hexdigest() for name, data in contents.items()
        }
        require(
            fingerprints == record["files"], "FIXTURE_ARTIFACT_HASH_MISMATCH"
        )
        state = json.dumps(
            validate(desired),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        checkpoints = local_checkpoint(
            contents["state.db"], sha256(state.encode()).hexdigest(), topic
        )
        targets = table_targets(root, contents, route)
        # Do not present a mixed read if a closed-cooperative fixture changed.
        require(
            enumerate_owned(root) == names
            and sealed_contents(root, names) == contents
            and read_seal(root, seal, expected) == record,
            "FIXTURE_ARTIFACT_CHANGED",
        )
    except ContractError:
        raise
    except Exception:
        raise ContractError("LOCAL_FIXTURE_EVIDENCE_UNAVAILABLE") from None
    shared = {
        key: value
        for key, value in expected.items()
        if key not in ("route_sha256", "topic_sha256", "group_sha256")
    }
    shared["observed_at"] = age
    return {
        "consumer": {
            **shared,
            "domain": "consumer",
            "rows": [
                {
                    "binding_sha256": expected["route_sha256"],
                    "topic_sha256": expected["topic_sha256"],
                    "group_sha256": expected["group_sha256"],
                    "checkpoints": checkpoints,
                    "last_error": None,
                    "last_event_age_seconds": None,
                }
            ],
        },
        "iceberg": {
            **shared,
            "domain": "iceberg",
            "rows": [
                {
                    "binding_sha256": expected["route_sha256"],
                    "targets": targets,
                }
            ],
        },
    }
