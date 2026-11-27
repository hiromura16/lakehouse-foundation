"""着地した原本と完了印を、保存先に依存せず読み取る。

矛盾した素材は返さない。S3、Bronze、完了台帳、Silver は呼出側の責務。
次の原本は要求されるまで読まないため、既存の表ごとの確定順も維持する。
"""

import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_ARTIFACTS = 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_BATCH_PATH = 1024
FILE_MANIFEST_VERSION = "source.file-manifest.v2"


class BatchValidationError(ValueError):
    """外部入力を診断する固定理由と、内部検証用の説明を保持する。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RawFile:
    """照合した原本 bytes、相対 path、内容 hash を同じ単位で渡す。"""

    path: Path
    relative: str
    data: bytes
    digest: str


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


def legacy_ready_files(directory):
    """完了印のパス順に、従来の照合を通過した原本だけを返す。"""
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
        yield RawFile(path, relative, data, digest)


def manifest_path_parts(path):
    """完成manifest内の原本パスはLanding相対で、通常の階層だけを許す。"""
    if (
        not isinstance(path, str)
        or not 0 < len(path) <= MAX_BATCH_PATH
        or any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
            for char in path
        )
        or "\\" in path
    ):
        raise BatchValidationError(
            "manifest_invalid", "Invalid completed batch artifact path."
        )
    parts = path.split("/")
    if (
        len(parts) < 2
        or parts[0] == "manifests"
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
        or parts[-1].endswith((".ready", ".ready.json", ".part"))
    ):
        raise BatchValidationError(
            "manifest_invalid", "Invalid completed batch artifact path."
        )
    return parts


@contextmanager
def open_relative(directory_fd, parts):
    """dirfdから各階層をたどり、symlink置換後も外のファイルを開かない。"""
    current = os.dup(directory_fd)
    try:
        for part in parts[:-1]:
            following = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = following
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=current,
        )
        with os.fdopen(descriptor, "rb") as stream:
            yield stream
    finally:
        os.close(current)


def bounded_bytes(stream, limit):
    """通常ファイルを上限付きで読み、同じinodeの書込み途中を拒否する。"""
    initial = os.fstat(stream.fileno())
    if not stat.S_ISREG(initial.st_mode) or initial.st_size > limit:
        raise BatchValidationError(
            "batch_limit", "Completed batch file exceeds its limit or type."
        )
    content = stream.read(limit + 1)
    final = os.fstat(stream.fileno())
    if len(content) != initial.st_size or (
        initial.st_size,
        initial.st_mtime_ns,
        initial.st_ctime_ns,
    ) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns):
        raise BatchValidationError(
            "artifact_mismatch",
            "Completed batch file changed during validation.",
        )
    return content


def unique_manifest_keys(pairs):
    """同じキーを重複させて検証対象を差し替えるJSONを拒否する。"""
    value = {}
    for key, item in pairs:
        if key in value:
            raise BatchValidationError(
                "manifest_invalid",
                "Completed batch manifest has duplicate keys.",
            )
        value[key] = item
    return value


def decode_batch_manifest(content):
    """JSONデコーダーだけの失敗を外部バッチの固定理由へ変換する。"""
    try:
        return json.loads(
            content.decode("utf-8"), object_pairs_hook=unique_manifest_keys
        )
    except BatchValidationError:
        raise
    except (ValueError, RecursionError) as error:
        raise BatchValidationError(
            "manifest_invalid", "Completed batch manifest cannot be read."
        ) from error


def read_batch_manifest(directory, directory_fd, marker_parts):
    """全原本のサイズとSHA-256を照合し、完全なバッチだけを返す。"""
    batch_root = marker_parts[:-2]
    try:
        with open_relative(directory_fd, marker_parts) as stream:
            content = bounded_bytes(stream, MAX_MANIFEST_BYTES)
        manifest = decode_batch_manifest(content)
        if not isinstance(manifest, dict) or (
            manifest.get("schema_version") != FILE_MANIFEST_VERSION
        ):
            raise BatchValidationError(
                "manifest_invalid",
                "Unsupported completed batch manifest version.",
            )
        items = manifest.get("artifacts")
        if not isinstance(items, list) or not (
            0 < len(items) <= MAX_MANIFEST_ARTIFACTS
        ):
            raise BatchValidationError(
                "manifest_invalid", "Invalid completed batch artifact count."
            )
        total = 0
        received = {}
        for item in items:
            if not isinstance(item, dict):
                raise BatchValidationError(
                    "manifest_invalid", "Invalid completed batch artifact."
                )
            relative = item.get("path")
            parts = manifest_path_parts(relative)
            expected = item.get("sha256")
            size = item.get("bytes")
            if (
                relative in received
                or type(size) is not int
                or not 0 <= size <= MAX_BYTES
                or not isinstance(expected, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected)
            ):
                raise BatchValidationError(
                    "manifest_invalid",
                    "Invalid completed batch artifact metadata.",
                )
            total += size
            if total > MAX_BATCH_BYTES:
                raise BatchValidationError(
                    "batch_limit", "Completed batch exceeds its size limit."
                )
            with open_relative(directory_fd, [*batch_root, *parts]) as stream:
                data = bounded_bytes(stream, size)
            digest = sha256(data).hexdigest()
            if len(data) != size or digest != expected:
                raise BatchValidationError(
                    "artifact_mismatch",
                    "Completed batch checksum or size mismatch.",
                )
            received[relative] = RawFile(
                directory.joinpath(*batch_root, *parts),
                "/".join([*batch_root, *parts]),
                data,
                digest,
            )
        return tuple(received.values())
    except OSError as error:
        raise BatchValidationError(
            "artifact_unavailable", "Completed batch is missing or unreadable."
        ) from error


def ready_files(directory, *, on_rejected_batch=None, validate_payload=None):
    """従来のreadyと外部の完成manifestを既存の受領台帳へ同じ形で渡す。"""
    yield from legacy_ready_files(directory)
    if not directory.is_dir():
        return
    directory_fd = os.open(
        directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        for marker in sorted(directory.rglob("*.ready.json")):
            parts = list(marker.relative_to(directory).parts)
            if len(parts) < 2 or parts[-2] != "manifests":
                continue
            # 外部アプリはrun/jobごとのrootにまとめて配置する。
            try:
                received = read_batch_manifest(directory, directory_fd, parts)
                if validate_payload is not None:
                    for raw in received:
                        validate_payload(raw)
            except BatchValidationError as error:
                if on_rejected_batch is None:
                    raise
                marker_id = sha256(os.fsencode(marker.relative_to(directory)))
                on_rejected_batch(marker_id.hexdigest()[:16], error.code)
                continue
            yield from received
    finally:
        os.close(directory_fd)
