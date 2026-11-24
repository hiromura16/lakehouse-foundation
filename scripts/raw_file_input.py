"""着地した原本と完了印を、保存先に依存せず読み取る。

矛盾した素材は返さない。S3、Bronze、完了台帳、Silver は呼出側の責務。
次の原本は要求されるまで読まないため、既存の表ごとの確定順も維持する。
"""

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024


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


def ready_files(directory):
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
