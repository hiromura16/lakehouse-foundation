#!/usr/bin/env python3
"""Write a SHA-256 completion marker after a landing file is fully copied."""

import argparse
from hashlib import sha256
from pathlib import Path

parser = argparse.ArgumentParser(
    description=__doc__,
    epilog=(
        "原本をコピーし終えた後に実行します。隣へ<原本名>.readyを作成・更新し、"
        "SHA-256と改行を保存します。原本の変更や取り込みの起動は行いません。"
    ),
)
parser.add_argument(
    "file",
    type=Path,
    help="コピー完了済みの通常ファイル。symlinkや.part/.ready自体は指定できない",
)
args = parser.parse_args()
path = args.file
if (
    not path.is_file()
    or path.is_symlink()
    or path.name.endswith((".part", ".ready"))
):
    raise SystemExit("Choose a completed regular landing file.")
# Hash only a completed copy; reject changes observed during hashing.
before = path.stat()
digest = sha256(path.read_bytes()).hexdigest()
after = path.stat()
if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
    raise SystemExit(
        "File changed during hashing; finish copying before marking it ready."
    )
# The worker checks this digest again before importing the source bytes.
marker = path.with_name(path.name + ".ready")
marker.write_text(digest + "\n")
print("Completion marker written. The ingestion job will verify its hash.")
