#!/usr/bin/env python3
"""公開Markdownの端末例を、指定されたBash/Zshで構文検査する。

Gitが列挙するMarkdownを読み、直前の明示ラベルから検査対象を決める。
共通例には限定的な非互換検査も行う。一般的なshell解析器や、手順の
実行試験ではない。shellへ渡すのは -n の標準入力だけで、例のsudo・
ネットワーク・DB操作やリダイレクトは実行しない。ファイルも生成しない。
"""

import argparse
import os
import re
import shlex
import shutil
import subprocess
from collections import deque
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
LANGUAGES = {"bash", "sh", "shell", "zsh"}
LABELS = {
    "**Bash / Zsh共通**": ("bash", "zsh"),
    "**Bash**": ("bash",),
    "**Zsh**": ("zsh",),
}
OPENING = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)$")
HEREDOC = re.compile(r"<<(-)?[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|([\w-]+))")
COMMAND = r"(?:^|[;\n|&({])[ \t]*(?:(?:if|while|until|then|do)[ \t]+)?"


class ShellExample(NamedTuple):
    """診断用の文書位置と、実行せずに検査する一つのコード例。"""

    path: str
    line: int
    language: str
    shells: tuple[str, ...]
    code: str


def markdown_paths(root):
    """cwdに依存せず、Git追跡済みと未ignoreの新規MDを列挙する。"""
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "*.md",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            "--rootにはGit管理されたリポジトリを指定してください。"
        )
    return sorted(
        set(os.fsdecode(item) for item in result.stdout.split(b"\0") if item)
    )


def extract_examples(content, path):
    """可変長の両fenceを読み、直前の空白以外の行をラベルとする。

    MarkdownやPython等のfence内にあるshell例は再帰抽出しない。
    言語名は対象の判別に使い、実際の検査shellは明示ラベルで決める。
    """
    lines = content.splitlines(keepends=True)
    examples = []
    previous = ""
    index = 0
    while index < len(lines):
        opening = OPENING.fullmatch(lines[index].rstrip("\r\n"))
        if opening and opening[1].startswith("`") and "`" in opening[2]:
            # backtick fenceのinfo文字列へbacktickは書けない。
            opening = None
        if not opening:
            if lines[index].strip():
                previous = lines[index].strip()
            index += 1
            continue
        markers, information = opening.groups()
        language = information.strip().split(maxsplit=1)
        language = language[0].lower() if language else ""
        closing = re.compile(
            rf" {{0,3}}{re.escape(markers[0])}{{{len(markers)},}}[ \t]*"
        )
        start = index
        index += 1
        body = []
        while index < len(lines) and not closing.fullmatch(
            lines[index].rstrip("\r\n")
        ):
            body.append(lines[index])
            index += 1
        if language in LANGUAGES:
            location = f"{path}:{start + 1}"
            if previous not in LABELS:
                raise ValueError(
                    location
                    + ": 直前に "
                    + " / ".join(LABELS)
                    + " を明記してください。"
                )
            if index == len(lines):
                raise ValueError(location + ": shellのfenceを閉じてください。")
            examples.append(
                ShellExample(
                    path, start + 1, language, LABELS[previous], "".join(body)
                )
            )
        # 閉じたfenceを飛び越えて、古いラベルを次の例へ持ち越さない。
        previous = ""
        index += 1
    return examples


def shell_regions(code):
    """限定検査向けに、引用・コメント・heredoc本文をマスクする。

    commandsは引用の外、expansionsは二重引用内の変数参照も保持する。
    単引用したPython -cやheredocのstatus=等をshellと誤認しない。
    これは現在の文書例を対象とし、完全なshell構文木は構築しない。
    """
    commands, expansions = [], []
    quote = None
    pending = deque()
    for line in code.splitlines(keepends=True):
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            if candidate == delimiter:
                pending.popleft()
            masked = "".join("\n" if char == "\n" else " " for char in line)
            commands.append(masked)
            expansions.append(masked)
            continue
        index = 0
        while index < len(line):
            char = line[index]
            blank = "\n" if char == "\n" else " "
            if quote == "'":
                commands.append(blank)
                expansions.append(blank)
                if char == "'":
                    quote = None
            elif char == "\\" and index + 1 < len(line):
                # エスケープした引用符や$は、引用・変数の開始と解釈しない。
                commands.append(" ")
                expansions.append(" ")
                index += 1
                escaped = "\n" if line[index] == "\n" else " "
                commands.append(escaped)
                expansions.append(escaped)
            elif quote == '"':
                commands.append(blank)
                expansions.append(char)
                if char == '"':
                    quote = None
            elif char in "'\"":
                quote = char
                commands.append(" ")
                expansions.append(" ")
            elif char == "#" and (
                index == 0
                or line[index - 1].isspace()
                or line[index - 1] in ";|&()"
            ):
                masked = "".join(
                    "\n" if item == "\n" else " " for item in line[index:]
                )
                commands.append(masked)
                expansions.append(masked)
                break
            elif line.startswith("<<<", index):
                commands.append("<<<")
                expansions.append("<<<")
                index += 2
            else:
                heredoc = HEREDOC.match(line, index)
                if heredoc:
                    strip_tabs, single, double, bare = heredoc.groups()
                    pending.append(
                        (single or double or bare, bool(strip_tabs))
                    )
                    masked = " " * len(heredoc.group())
                    commands.append(masked)
                    expansions.append(masked)
                    index = heredoc.end() - 1
                else:
                    commands.append(char)
                    expansions.append(char)
            index += 1
    return "".join(commands), "".join(expansions)


def common_incompatibilities(code):
    """-nだけでは検出しない、明白な共通例の非互換を返す。"""
    commands, expansions = shell_regions(code)
    errors = []
    read = re.compile(COMMAND + r"read\b(?P<arguments>[^\n;|&]*)")
    for match in read.finditer(commands):
        start, end = match.span("arguments")
        arguments = code[start:end]
        try:
            words = shlex.split(arguments)
        except ValueError:
            # 引用の構文不正は実shellの-nで、元の文書位置とともに示す。
            continue
        if any(re.fullmatch(r"-[A-Za-z]*p[A-Za-z]*", word) for word in words):
            errors.append("readの-pは共通にできません。Bash/Zsh版を分けます。")
        if any(re.match(r"^[A-Za-z_][A-Za-z_0-9]*\?", word) for word in words):
            errors.append("readの変数?質問はZsh版として明示してください。")
    for command in ("mapfile", "readarray", "setopt", "unsetopt", "autoload"):
        if re.search(COMMAND + re.escape(command) + r"\b", commands):
            errors.append(command + "を使う例は対応shellを明示してください。")
    if re.search(r"\$(?:BASH_SOURCE\b|\{BASH_SOURCE\b)", expansions):
        errors.append("BASH_SOURCEを使う例はBash版として明示してください。")
    if re.search(
        COMMAND + r"(?:(?:export|local|typeset|declare)[ \t]+)?"
        r"(?:status|path|fpath|pipestatus)=",
        commands,
    ):
        errors.append(
            "Zshの特別変数への代入を避け、用途固有の変数名にします。"
        )
    return errors


def shell_executables():
    """検査を省略せず、BashとZshの両方の存在を必須とする。"""
    executables = {name: shutil.which(name) for name in ("bash", "zsh")}
    missing = [
        name for name, executable in executables.items() if not executable
    ]
    if missing:
        raise ValueError(
            "検査用shellを導入してください: " + ", ".join(missing)
        )
    return executables


def syntax_error(example, shell, executable, root):
    """例を標準入力へ渡すだけで構文検査し、最初の診断行を返す。"""
    arguments = (
        [executable, "-f", "-n"] if shell == "zsh" else [executable, "-n"]
    )
    environment = dict(os.environ)
    # Bashの非対話起動hookへ文書以外の処理を混ぜない。
    environment.pop("BASH_ENV", None)
    environment.pop("ENV", None)
    environment["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            arguments,
            input=example.code,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=root,
            env=environment,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return shell + ": 構文検査を完了できませんでした。"
    if result.returncode:
        detail = result.stderr.splitlines()
        return shell + ": " + (detail[0] if detail else "構文エラーです。")
    return None


def check_examples(root):
    """全公開MDを読み、件数と診断を返す。出力先や書込み処理は持たない。"""
    root = root.resolve()
    executables = shell_executables()
    paths = markdown_paths(root)
    errors = []
    count = 0
    for path in paths:
        source = root / path
        if not source.resolve().is_relative_to(root) or not source.is_file():
            errors.append(path + ": リポジトリ内の通常ファイルを指定します。")
            continue
        try:
            examples = extract_examples(
                source.read_text(encoding="utf-8"), path
            )
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(str(error))
            continue
        for example in examples:
            count += 1
            location = f"{path}:{example.line}: "
            if len(example.shells) == 2:
                errors.extend(
                    location + item
                    for item in common_incompatibilities(example.code)
                )
            for shell in example.shells:
                error = syntax_error(example, shell, executables[shell], root)
                if error:
                    errors.append(location + error)
    return {"documents": len(paths), "examples": count}, errors


def main(argv=None):
    """既定と--checkの両方で、書込みなしの検査を実行するCLI。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="検査対象のGitルート。既定はこのスクリプトのリポジトリ。",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="検査のみを明示する。指定なしでも同じ動作で、例は実行しない。",
    )
    arguments = parser.parse_args(argv)
    try:
        statistics, errors = check_examples(arguments.root)
    except (OSError, ValueError) as error:
        print("ERROR: " + str(error))
        return 1
    if errors:
        for error in errors:
            print("ERROR: " + error)
        return 1
    print(
        f"PASS: {statistics['documents']} Markdown files; "
        f"{statistics['examples']} shell examples checked with Bash/Zsh."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
