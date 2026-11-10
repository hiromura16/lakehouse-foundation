#!/usr/bin/env python3
"""文書の目的・読む順序を生成し、公開Markdownの導線を検査する。

正本はdocs/documentation-manifest.json。通常実行は登録済みMarkdownの
最初のH1直後に目的欄を追加・更新し、目的別索引を再生成する。目的欄の
外側にある本文は保持する。--checkは一切書き込まず、登録の過不足、
生成内容、リポジトリ内リンクと見出しを検査する。通信・配置・秘密設定の
読取りは行わない。外部URLの到達性は検査対象に含まない。
"""

import argparse
import html
import json
import os
import re
import subprocess
import unicodedata
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "docs/documentation-manifest.json"
MANIFEST = "docs/DOCUMENTATION-MANIFEST.md"
START = "<!-- doc-purpose:start -->"
END = "<!-- doc-purpose:end -->"
STATUSES = {"active", "template", "reference"}
FIELDS = {
    "path",
    "title",
    "purpose",
    "audience",
    "when_to_use",
    "category",
    "prerequisites",
    "next",
    "status",
}
EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel", "data", "app", "plugin"}


def markdown_paths(root: Path = ROOT) -> list[str]:
    """Git管理対象と未追跡Markdownを、無視設定を尊重して列挙する。

    -CとNUL区切りにより呼出元cwdやファイル名の空白に依存しない。
    同じファイルがindexと作業木の両方に現れても1件として扱う。
    """
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
        check=True,
        capture_output=True,
    )
    return sorted(
        {name for name in result.stdout.decode("utf-8").split("\0") if name}
    )


def registered_path(value: str) -> bool:
    """登録名が正規化された相対Markdownパスか判定する。"""
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and path.as_posix() == value
        and path.suffix == ".md"
    )


def load_index(root: Path = ROOT) -> dict[str, dict]:
    """JSONの型・一意性・相互参照を検査してpath索引を返す。

    本文とGit一覧の照合はcheck_documentsが担う。例外メッセージには
    private設定や実行環境の絶対パスを含めない。
    """
    data = json.loads((root / REGISTRY).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported documentation schema_version.")
    documents = data.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("documents must be a nonempty list.")
    index = {}
    for number, document in enumerate(documents, 1):
        label = f"documents[{number}]"
        if not isinstance(document, dict) or set(document) != FIELDS:
            raise ValueError(f"{label}: unexpected or missing fields.")
        for field in (
            "path",
            "title",
            "purpose",
            "when_to_use",
            "category",
            "status",
        ):
            value = document[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label}: {field} must be nonempty text.")
            if "\n" in value or "\r" in value:
                raise ValueError(f"{label}: {field} must fit on one line.")
        path = document["path"]
        if not registered_path(path):
            raise ValueError(f"{label}: invalid Markdown path.")
        if path in index:
            raise ValueError(f"Duplicate document: {path}")
        if document["status"] not in STATUSES:
            raise ValueError(f"{path}: invalid status.")
        for field in ("audience", "prerequisites", "next"):
            values = document[field]
            if not isinstance(values, list) or any(
                not isinstance(value, str)
                or not value.strip()
                or "\n" in value
                or "\r" in value
                for value in values
            ):
                raise ValueError(f"{path}: {field} must contain text values.")
            if len(values) != len(set(values)):
                raise ValueError(f"{path}: duplicate {field} values.")
        if not document["audience"]:
            raise ValueError(f"{path}: audience must not be empty.")
        index[path] = document
    for path, document in index.items():
        for field in ("prerequisites", "next"):
            if any(value not in index for value in document[field]):
                raise ValueError(f"{path}: unregistered {field} document.")
    if MANIFEST not in index:
        raise ValueError("The documentation manifest must register itself.")
    return index


def relative_link(source: str, target: str, index: dict) -> str:
    """同一階層・異なる階層の文書を、題名付き相対リンクへ変換する。"""
    parent = PurePosixPath(source).parent.as_posix()
    relative = os.path.relpath(target, parent).replace(os.sep, "/")
    title = index[target]["title"].replace("[", "\\[").replace("]", "\\]")
    return f"[{title}](<{relative}>)"


def purpose_block(path: str, index=None) -> str:
    """文書冒頭へ挿入する可視目的欄を、両端マーカー込みで返す。

    pathはリポジトリ相対名。indexにはload_indexと同じpath→項目の辞書を
    渡せる。省略時は本スクリプトと同じcheckoutの公開JSONを読む。
    図キャプションなど別の生成器もこの関数を呼び、目的欄を統一する。
    ファイルは変更せず、戻り値の末尾には改行を付けない。
    """
    index = load_index() if index is None else index
    document = index[path]
    prerequisites = (
        " / ".join(
            relative_link(path, target, index)
            for target in document["prerequisites"]
        )
        or "なし。必要な用語は本文で確認します。"
    )
    following = (
        " / ".join(
            relative_link(path, target, index) for target in document["next"]
        )
        or "目的別一覧から必要な資料を選びます。"
    )
    return "\n".join(
        [
            START,
            f"> **役割:** {document['purpose']}",
            ">",
            f"> **対象読者:** {' / '.join(document['audience'])}",
            ">",
            f"> **読むタイミング:** {document['when_to_use']}",
            ">",
            f"> **前提となる資料:** {prerequisites}",
            ">",
            f"> **次に読む資料:** {following}",
            ">",
            f"> **文書一覧:** {relative_link(path, MANIFEST, index)}",
            END,
        ]
    )


def without_fences(text: str) -> str:
    """fenced codeを空行へ置換し、行番号と本文の改行位置を保つ。"""
    output = []
    fence = None
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence is None and match:
            fence = match.group(1)
            output.append("\n" if line.endswith("\n") else "")
        elif fence is not None:
            if re.match(
                r"^ {0,3}"
                + re.escape(fence[0])
                + "{"
                + str(len(fence))
                + r",}\s*$",
                line,
            ):
                fence = None
            output.append("\n" if line.endswith("\n") else "")
        else:
            output.append(line)
    return "".join(output)


def first_heading(text: str) -> tuple[str, int]:
    """コード例以外の初H1の題名と、その行末までの文字位置を返す。"""
    offset = 0
    for visible, original in zip(
        without_fences(text).splitlines(keepends=True),
        text.splitlines(keepends=True),
    ):
        offset += len(original)
        match = re.match(r"^ {0,3}#\s+(.+?)\s*#*\s*$", visible)
        if match:
            return match.group(1), offset
    raise ValueError("A level-one Markdown heading is required.")


def with_purpose(text: str, path: str, index: dict) -> str:
    """目的欄だけを更新し、その外側の文字列をそのまま維持する。

    既存マーカーが壊れている・複数ある場合は上書きせず拒否する。
    初回挿入時に限り、H1と目的欄の間へ空行を追加する。
    """
    _, heading_end = first_heading(text)
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise ValueError(f"{path}: invalid purpose markers.")
    block = purpose_block(path, index)
    if START in text:
        start, end = text.index(START), text.index(END) + len(END)
        if start < heading_end or end < start:
            raise ValueError(f"{path}: misplaced purpose markers.")
        if text[heading_end:start].strip():
            raise ValueError(
                f"{path}: purpose block must follow the first H1."
            )
        return text[:start] + block + text[end:]
    prefix = text[:heading_end]
    if not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + "\n" + block + "\n" + text[heading_end:]


def table_text(value: str) -> str:
    """生成する表の区切り文字と改行を安全な表示へ変換する。"""
    return value.replace("|", "\\|").replace("\n", " ")


def render_manifest(index: dict) -> str:
    """目的別・読者別の全件表と、正本の編集規則を生成する。"""
    lines = [
        f"# {index[MANIFEST]['title']}",
        "",
        purpose_block(MANIFEST, index),
        "",
        "この一覧は公開Markdown全件の道案内です。"
        "「前提」は先に確認する文書、「次」は読み終えた後の候補です。",
        "実際の操作はリンク先で成功条件まで確認します。"
        "templateは社内へコピーして記入する雛形、referenceは照合資料です。",
        "",
        "## 目的から探す",
        "",
    ]
    categories = sorted({item["category"] for item in index.values()})
    for category in categories:
        lines.extend(
            [
                f"### {category}",
                "",
                "| 文書 | 何のために読むか | 読むタイミング | 区分 |",
                "| --- | --- | --- | --- |",
            ]
        )
        documents = sorted(
            (item for item in index.values() if item["category"] == category),
            key=lambda item: item["path"],
        )
        for item in documents:
            link = relative_link(MANIFEST, item["path"], index)
            lines.append(
                f"| {link} | {table_text(item['purpose'])} | "
                f"{table_text(item['when_to_use'])} | {item['status']} |"
            )
        lines.append("")
    lines.extend(["## 読者から探す", ""])
    audiences = sorted(
        {audience for item in index.values() for audience in item["audience"]}
    )
    for audience in audiences:
        lines.extend(
            [
                f"### {audience}",
                "",
                "| 文書 | 目的 |",
                "| --- | --- |",
            ]
        )
        for item in sorted(index.values(), key=lambda item: item["path"]):
            if audience in item["audience"]:
                link = relative_link(MANIFEST, item["path"], index)
                lines.append(f"| {link} | {table_text(item['purpose'])} |")
        lines.append("")
    lines.extend(
        [
            "## 文書を追加・変更するとき",
            "",
            "1. 本文の初H1と役割を決め、"
            "[JSON正本](documentation-manifest.json)に1項目を追加・更新します。",
            "2. purpose・audience・when_to_useはその文書固有の説明にします。"
            "prerequisitesとnextには登録済みMarkdownの相対pathだけを指定します。",
            "3. リポジトリの端末で "
            "`python3 scripts/documentation.py` を実行し、"
            "全目的欄と本一覧を生成します。マーカーの外側の本文は保持されます。",
            "4. `python3 scripts/documentation.py --check` で登録・生成差分・"
            "ローカルリンク・見出しを検査します。外部URLの到達性は別確認です。",
            "5. 図の生成キャプションは共通のpurpose_block APIを使います。"
            "本文生成器の出力とこの検査を両方確認します。",
            "",
            "削除・改名時は相互参照と本文リンクも更新します。"
            "実環境の設定・証跡・記入済み報告書は登録せずGit外に保管します。",
            "Gitのignore対象は列挙されません。既に追跡済みのファイルは"
            "ignore設定だけでは対象外になりません。",
            "削除・改名は `git rm` / `git mv`、または対象を確認した "
            "`git add -u` でindexにも反映してから生成します。"
            "追跡済みの未stage削除は欠落として検出されます。",
            "",
            "## リンク検査の範囲と限界",
            "",
            "inline/reference形式のMarkdownリンクと画像、HTMLのhref/src、"
            "見出しIDと明示id/nameを検査します。コードフェンス内の例は対象外です。"
            "参照先の存在、重複見出しの連番、repository外への逸脱を確認します。",
            "外部URLへ通信しないため到達性は保証しません。"
            "独立した軽量検査であり、全CommonMark/GFM構文やGitHubの"
            "表示処理への完全適合を保証するものではありません。"
            "複雑な記法を追加した場合はGitHub上の表示とリンクも確認します。",
            "",
            "記法の一次資料: "
            "[GFMのコードフェンス](https://github.github.com/gfm/"
            "#fenced-code-blocks)、"
            "[GitHubの見出しリンク](https://docs.github.com/en/get-started/"
            "writing-on-github/"
            "getting-started-with-writing-and-formatting-on-github/"
            "basic-writing-and-formatting-syntax#section-links)、"
            "[GitHubの相対リンク](https://docs.github.com/en/get-started/"
            "writing-on-github/"
            "getting-started-with-writing-and-formatting-on-github/"
            "basic-writing-and-formatting-syntax#relative-links)。",
            "",
        ]
    )
    return "\n".join(lines)


class HtmlReferences(HTMLParser):
    """HTMLのローカル参照と明示id/nameを、表示行番号付きで収集する。"""

    def __init__(self):
        """1文書分の参照とanchorの格納先を初期化する。"""
        super().__init__(convert_charrefs=True)
        self.links = []
        self.anchors = set()

    def handle_starttag(self, tag, attrs):
        """href/srcをリンク、idとa要素nameを明示anchorとして扱う。"""
        for key, value in attrs:
            if value is None:
                continue
            if key in {"href", "src"}:
                self.links.append((self.getpos()[0], value))
            if key == "id" or (tag == "a" and key == "name"):
                self.anchors.add(value)


def heading_slug(text: str) -> str:
    """GFMの見出しID向けに装飾・HTML・句読点を除き、小文字化する。"""
    text = re.sub(r"!?\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = html.unescape(re.sub(r"<[^>]*>", "", text)).lower().strip()
    # 強調のunderscoreを除き、inline codeや識別子内のunderscoreは保つ。
    parts = re.split(r"(`+[^`]*`+)", text)
    text = "".join(
        part
        if position % 2
        else re.sub(
            r"(?<!\w)_{1,3}(.+?)_{1,3}(?!\w)",
            r"\1",
            part,
        )
        for position, part in enumerate(parts)
    )
    return "".join(
        "-" if char == " " else char
        for char in text
        if char in "_ -" or unicodedata.category(char)[0] in "LMN"
    )


def anchors(text: str, markdown: bool = True) -> set[str]:
    """明示anchorと見出しIDを収集し、重複見出しへ連番を付ける。"""
    visible = without_fences(text) if markdown else text
    parser = HtmlReferences()
    parser.feed(visible)
    result = set(parser.anchors)
    used = set()
    if not markdown:
        return result
    lines = visible.splitlines()
    for position, line in enumerate(lines):
        match = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        title = match.group(1) if match else None
        if (
            title is None
            and position
            and re.fullmatch(
                r" {0,3}(?:=+|-+)\s*",
                line,
            )
            and lines[position - 1].strip()
        ):
            title = lines[position - 1].strip()
        if title is None:
            continue
        base = heading_slug(title)
        slug, count = base, 0
        while slug in used:
            count += 1
            slug = f"{base}-{count}"
        used.add(slug)
        result.add(slug)
    return result


def markdown_links(text: str) -> list[tuple[int, str]]:
    """MarkdownとHTML参照を読み取り、コードfence内の例を除外する。

    Markdownのinlineリンク・画像、参照形式の定義/利用を扱う。
    destination内の対応する括弧と<path with spaces>も保持する。
    """
    visible = without_fences(text)
    parser = HtmlReferences()
    parser.feed(visible)
    links = list(parser.links)
    definitions = {}
    for match in re.finditer(
        r"(?m)^ {0,3}\[([^]]+)\]:\s*(<[^>]+>|\S+)",
        visible,
    ):
        definitions[match.group(1).casefold()] = match.group(2).strip("<>")
    for match in re.finditer(r"!?\[([^]\n]*)\]\(", visible):
        position = match.end()
        depth, quoted = 1, False
        destination = []
        while position < len(visible):
            char = visible[position]
            if char == "<" and not destination:
                quoted = True
            elif char == ">" and quoted:
                quoted = False
            elif not quoted and char == "(":
                depth += 1
            elif not quoted and char == ")":
                depth -= 1
                if not depth:
                    break
            if not quoted and char.isspace() and depth == 1:
                break
            destination.append(char)
            position += 1
        target = "".join(destination).strip("<>")
        if target:
            links.append((visible.count("\n", 0, match.start()) + 1, target))
    for match in re.finditer(r"!?\[([^]\n]+)\](?:\[([^]\n]*)\])?", visible):
        reference_end = match.end()
        if visible[reference_end:].startswith(("(", ":")):
            continue
        key = (match.group(2) or match.group(1)).casefold()
        if key in definitions:
            links.append(
                (
                    visible.count("\n", 0, match.start()) + 1,
                    definitions[key],
                )
            )
        elif match.group(2) is not None:
            links.append(
                (
                    visible.count("\n", 0, match.start()) + 1,
                    "unresolved-reference:" + key,
                )
            )
    return links


def check_links(root: Path, paths: list[str]) -> list[str]:
    """公開文書のローカルtargetとanchorを検査し、repo外参照を拒否する。

    外部http等へ接続しない。実path・秘密値を出力せず、診断は公開文書の
    相対名、行番号、エラー種別だけにする。
    """
    root = root.resolve()
    errors = []
    cache = {}
    for name in paths:
        source = root / name
        if not source.resolve().is_relative_to(root):
            errors.append(f"{name}: document escapes repository.")
            continue
        if not source.is_file():
            continue
        for line, target in markdown_links(source.read_text(encoding="utf-8")):
            label = f"{name}:{line}"
            try:
                parsed = urlsplit(html.unescape(target))
            except ValueError:
                errors.append(f"{label}: malformed link.")
                continue
            if parsed.scheme in EXTERNAL_SCHEMES or parsed.netloc:
                continue
            if parsed.scheme:
                errors.append(f"{label}: unsupported local link scheme.")
                continue
            decoded = unquote(parsed.path)
            if decoded.startswith("/"):
                destination = (root / decoded.lstrip("/")).resolve()
            else:
                destination = (
                    (source.parent / decoded).resolve()
                    if decoded
                    else source.resolve()
                )
            if not destination.is_relative_to(root):
                errors.append(f"{label}: local link escapes repository.")
                continue
            if not destination.exists():
                errors.append(f"{label}: local link target not found.")
                continue
            if parsed.fragment:
                if destination.is_dir():
                    candidates = [
                        destination / "README.md",
                        destination / "index.html",
                    ]
                    destination = next(
                        (item for item in candidates if item.is_file()),
                        destination,
                    ).resolve()
                    if not destination.is_relative_to(root):
                        errors.append(
                            f"{label}: local link escapes repository."
                        )
                        continue
                if not destination.is_file():
                    errors.append(f"{label}: anchor target is not a file.")
                    continue
                if destination not in cache:
                    try:
                        cache[destination] = anchors(
                            destination.read_text(encoding="utf-8"),
                            destination.suffix.lower() == ".md",
                        )
                    except (UnicodeError, OSError):
                        cache[destination] = set()
                if unquote(parsed.fragment) not in cache[destination]:
                    errors.append(f"{label}: local anchor not found.")
    return errors


def check_documents(root: Path, index: dict) -> list[str]:
    """登録全件と作業木を比較し、目的欄・生成索引・リンクを検査する。"""
    paths = markdown_paths(root)
    errors = []
    for path in sorted(set(paths) - set(index)):
        errors.append(f"Unregistered Markdown: {path}")
    for path in sorted(set(index) - set(paths)):
        errors.append(f"Registered Markdown is missing: {path}")
    for path in sorted(set(index) & set(paths)):
        destination = (root / path).resolve()
        if (
            not destination.is_relative_to(root.resolve())
            or not destination.is_file()
        ):
            errors.append(
                f"{path}: registered file is unavailable inside repository."
            )
            continue
        text = destination.read_text(encoding="utf-8")
        try:
            title, _ = first_heading(text)
            if title != index[path]["title"]:
                errors.append(
                    f"{path}: registered title differs from first H1."
                )
            if with_purpose(text, path, index) != text:
                errors.append(f"{path}: purpose block is missing or stale.")
        except ValueError as error:
            errors.append(f"{path}: {error}")
    manifest = root / MANIFEST
    if not manifest.is_file() or manifest.read_text(
        encoding="utf-8",
    ) != render_manifest(index):
        errors.append(f"{MANIFEST}: generated manifest is missing or stale.")
    errors.extend(check_links(root, paths))
    return errors


def update_documents(root: Path, index: dict) -> int:
    """登録を照合してから全目的欄と索引を更新し、変更ファイル数を返す。

    全入力を先に検査するため、登録漏れや不正マーカーで途中まで生成
    することはない。本文・題名の編集や未登録文書の自動登録はしない。
    """
    paths = markdown_paths(root)
    if set(paths) != set(index):
        raise ValueError(
            "Markdown coverage differs; update the registry first."
        )
    outputs = {}
    for path in paths:
        destination = (root / path).resolve()
        if (
            not destination.is_relative_to(root.resolve())
            or not destination.is_file()
        ):
            raise ValueError(f"{path}: registered file is unavailable.")
        text = destination.read_text(encoding="utf-8")
        title, _ = first_heading(text)
        if title != index[path]["title"]:
            raise ValueError(
                f"{path}: registered title differs from first H1."
            )
        outputs[path] = with_purpose(text, path, index)
    outputs[MANIFEST] = render_manifest(index)
    changed = 0
    for path, text in outputs.items():
        destination = root / path
        if destination.read_text(encoding="utf-8") != text:
            destination.write_text(text, encoding="utf-8")
            changed += 1
    return changed


def main(argv=None) -> int:
    """CLI引数を読み、生成または非書込検査の終了コードを返す。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="書き込まず登録・目的欄・生成索引・ローカルリンクを検査する。",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="対象checkoutのルート。既定は本スクリプトの所属リポジトリ。",
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    try:
        index = load_index(root)
        if arguments.check:
            errors = check_documents(root, index)
            if errors:
                print("\n".join(errors))
                return 1
            print(f"Documentation check passed: {len(index)} Markdown files.")
        else:
            changed = update_documents(root, index)
            print(f"Documentation updated: {changed} files.")
    except (OSError, ValueError, subprocess.CalledProcessError):
        # OS/JSON例外の生表示は絶対pathや本文を含むため避ける。
        print(
            "Documentation input is invalid; review registry and Git checkout."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
