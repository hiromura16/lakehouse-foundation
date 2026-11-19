#!/usr/bin/env python3
"""全YAML/shellを静的検査する。.git以外を省略せず実行はしない。"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_paths(root):
    """未追跡・隠しディレクトリも含め、symlinkは辿らず明示拒否する。"""

    def refuse_unreadable(error):
        """読めない階層を黙って省略して成功しない。"""
        raise error

    result = {"yaml": [], "shell": []}
    for directory, folders, files in os.walk(
        root, followlinks=False, onerror=refuse_unreadable
    ):
        folders[:] = sorted(name for name in folders if name != ".git")
        if any((Path(directory) / name).is_symlink() for name in folders):
            raise ValueError("SOURCE_DIRECTORY_SYMLINK")
        for name in sorted(files):
            path = Path(directory) / name
            kind = (
                "yaml"
                if path.suffix in {".yaml", ".yml"}
                else "shell"
                if path.suffix == ".sh"
                else None
            )
            if kind is not None:
                if path.is_symlink():
                    raise ValueError("SOURCE_FILE_SYMLINK")
                result[kind].append(path.relative_to(root).as_posix())
    return result


def check(root=ROOT):
    """argvで空白名を保持し、各検査の非ゼロ終了をそのまま失敗とする。"""
    sources = source_paths(root)
    commands = (
        ("yaml", ["yamllint", "--strict", "-c", ".yamllint.yml"]),
        ("shell", ["shellcheck"]),
    )
    for kind, command in commands:
        paths = sources[kind]
        if not paths:
            raise ValueError("SOURCE_COVERAGE_EMPTY")
        # 多数の追加ファイルでもOSのargv上限を超えないよう分割する。
        for offset in range(0, len(paths), 100):
            end = offset + 100
            subprocess.run(
                [*command, "--", *paths[offset:end]],
                cwd=root,
                check=True,
            )
    print(
        f"PASS: {len(sources['yaml'])} YAML and "
        f"{len(sources['shell'])} shell files checked."
    )


def main():
    """ツール欠落・探索拒否・lint失敗を成功に変換しない。"""
    try:
        check()
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("FAIL: YAML/ShellCheck source validation.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
