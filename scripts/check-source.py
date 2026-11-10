#!/usr/bin/env python3
"""Inspect public source and optional Git history without root or network I/O.

Private deny terms and credential values are read only from external inputs.
Reports contain rule names and source locations, never the matching contents.
"""

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.parsers import expat

ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_BYTES = 8 * 1024 * 1024
# The CA placeholder must stay empty in worktree, index and history blobs.
EMPTY_CA_SOURCE = "infra/proxy/empty-ca.pem"
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
FORBIDDEN_PARTS = {
    ".aws",
    ".codex",
    ".credentials",
    ".docker",
    ".secrets",
    ".ssh",
    ".venv",
    "__pycache__",
    "backups",
    "data",
    "landing",
    "runtime",
    "secrets",
    "venv",
    "volumes",
}
LOCAL_CACHE_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
FORBIDDEN_SUFFIXES = {
    ".bak",
    ".crt",
    ".db",
    ".key",
    ".log",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".sqlite",
    ".sqlite3",
}
CONTAINER_USERS = {"airflow", "curl_user", "jovyan", "node", "nonroot"}
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?")
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SVG_TAG = re.compile(rb"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")
SVG_ATTRIBUTE = re.compile(
    rb"([A-Za-z_:][\w:.-]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL
)
SVG_GEOMETRY = {
    b"d",
    b"points",
    b"viewBox",
    b"transform",
    b"gradientTransform",
    b"patternTransform",
    b"x",
    b"y",
    b"x1",
    b"x2",
    b"y1",
    b"y2",
    b"cx",
    b"cy",
    b"r",
    b"rx",
    b"ry",
    b"width",
    b"height",
    b"stroke-width",
    b"stroke-dasharray",
    b"stroke-dashoffset",
}
SVG_PATH_VALUE = re.compile(rb"[MmZzLlHhVvCcSsQqTtAaEe0-9+.,\s-]*")
SVG_NUMERIC_VALUE = re.compile(rb"[Ee0-9+.,\s-]*")
# Possessive repeats avoid backtracking over long malformed attributes.
SVG_TRANSFORM_VALUE = re.compile(
    rb"\s*+(?:(?:matrix|translate|scale|rotate|skewX|skewY)"
    rb"\s*+\([Ee0-9+.,\s-]++\)\s*+,?\s*+)*+"
)
HOME_PATH = re.compile(r"/home/([A-Za-z0-9_.-]+)(?=/|[\s\"':),]|$)")
SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|credential|api[_-]?key|access[_-]?key)"
)
RULES = {
    "private-key": re.compile(
        r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----"
    ),
    "github-token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    ),
    "api-token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "bearer-token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
    "literal-secret": re.compile(
        r"(?i)\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
        r"\b\s*[:=]\s*[\"'][A-Za-z0-9/+_.=-]{20,}[\"']"
    ),
}


@dataclass(frozen=True)
class Finding:
    """Identify a rule and source location without retaining secret text."""

    location: str
    line: int
    rule: str


def git(root, *arguments):
    """Run only read-only Git subcommands and retain stderr locally."""
    environment = dict(os.environ)
    # Partial-clone object reads must fail locally instead of fetching blobs.
    environment["GIT_NO_LAZY_FETCH"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    # Also forbid transport fallback in Git versions predating that guard.
    environment["GIT_ALLOW_PROTOCOL"] = ""
    result = subprocess.run(
        ["git", "--no-pager", "-C", str(root), *arguments],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise ValueError("Git inspection failed; command output was hidden.")
    return result.stdout


def is_repository(root):
    """Require this directory itself to be the checkout root."""
    try:
        top = git(root, "rev-parse", "--show-toplevel").decode().strip()
    except (OSError, ValueError):
        return False
    return Path(top).resolve() == root


def external_path(value, root, directory=False):
    """Reject private inputs located inside the publishable source tree."""
    path = Path(value).expanduser().resolve()
    if path.is_relative_to(root):
        raise ValueError("Private scan inputs must be outside the source.")
    if directory and not path.is_dir():
        raise ValueError("Private credential directory was not found.")
    if not directory and not path.is_file():
        raise ValueError("Private deny file was not found.")
    return path


def credential_values(value, key=""):
    """Extract only credential-like JSON leaves, excluding usernames/labels."""
    if isinstance(value, dict):
        for name, item in value.items():
            yield from credential_values(item, str(name))
    elif isinstance(value, list):
        for item in value:
            yield from credential_values(item, key)
    elif isinstance(value, str) and SENSITIVE_KEY.search(key):
        if len(value) >= 8:
            yield value


def private_inputs(args, root):
    """Load external deny terms and real secrets without displaying them."""
    terms = set()
    secrets = set()
    if args.deny_file:
        path = external_path(args.deny_file, root)
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                if len(line.strip()) < 3:
                    raise ValueError(
                        "Private deny terms need three characters."
                    )
                terms.add(line.strip().casefold())
    if args.secret_dir:
        parent = external_path(args.secret_dir, root, directory=True)
        for path in parent.rglob("*"):
            if path.is_symlink():
                raise ValueError(
                    "Private credential symlinks are unsupported."
                )
            if not path.is_file():
                continue
            if path.stat().st_size > MAX_TEXT_BYTES:
                raise ValueError("A private credential file is too large.")
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeError:
                continue
            try:
                secrets.update(credential_values(json.loads(text)))
            except ValueError:
                pass
            for line in text.splitlines():
                name, separator, value = line.partition("=")
                # Logs and scripts may contain comparisons such as !=.
                # Accept an env assignment only when its left side is a key.
                if (
                    separator
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                    and SENSITIVE_KEY.search(name)
                ):
                    value = value.strip().strip("\"'")
                    if len(value) >= 8:
                        secrets.add(value)
            # A one-line token file has no assignment or JSON structure.
            value = text.strip()
            if (
                "\n" not in value
                and len(value) >= 20
                and not any(char.isspace() for char in value)
                and not value.startswith(("{", "[", "#"))
            ):
                secrets.add(value)
    return terms, secrets


def path_rule(name):
    """Recognize private/runtime artifacts even when they were force-added."""
    path = PurePosixPath(name)
    if any(part.casefold() in FORBIDDEN_PARTS for part in path.parts):
        return "private-or-runtime-path"
    base = path.name.casefold()
    template = base.endswith((".env.example", ".env.template"))
    if not template and (
        base == ".env" or base.startswith(".env.") or base.endswith(".env")
    ):
        return "private-env-file"
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        return "private-or-runtime-file"
    if base in {".netrc", "id_rsa", "id_ed25519"}:
        return "private-credential-file"
    return None


def permitted_ip(value):
    """Allow loopback and standard network constants, never private hosts."""
    address_text = value.partition("/")[0]
    try:
        address = ipaddress.ip_address(address_text)
    except ValueError:
        return True  # A dotted version that is not an IPv4 address.
    if address.is_loopback or address == ipaddress.ip_address("0.0.0.0"):
        return True
    if "/" in value:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError:
            return False
        shared = ipaddress.ip_network("100.64.0.0/10")
        return network == shared or (
            network.prefixlen < 32
            and any(network.subnet_of(private) for private in PRIVATE_NETWORKS)
        )
    return False


def svg_ip_text(name, text):
    """Mask valid SVG geometry only for IP scanning, retaining line breaks."""
    if PurePosixPath(name).suffix.casefold() != ".svg":
        return text
    source = text.encode("utf-8")
    parser = expat.ParserCreate(namespace_separator="}")
    spans = []
    root = None

    def reject_doctype(*_):
        """Leave DTD/entity-bearing input fully subject to IP scanning."""
        raise ValueError("SVG geometry masking does not process DTDs.")

    def start_element(element, _):
        """Locate actual XML attributes, excluding comments and text."""
        nonlocal root
        if root is None:
            root = element
        if root not in {"svg", SVG_NAMESPACE + "}svg"}:
            return
        if "}" in element and not element.startswith(SVG_NAMESPACE + "}"):
            return
        tag = SVG_TAG.match(source, parser.CurrentByteIndex)
        if tag is None:
            return
        for attribute in SVG_ATTRIBUTE.finditer(tag.group()):
            key, _, value = attribute.groups()
            if key not in SVG_GEOMETRY:
                continue
            if key == b"d":
                grammar = SVG_PATH_VALUE
            elif key.endswith(b"Transform") or key == b"transform":
                grammar = SVG_TRANSFORM_VALUE
            else:
                grammar = SVG_NUMERIC_VALUE
            if grammar.fullmatch(value):
                spans.append(
                    (
                        tag.start() + attribute.start(3),
                        tag.start() + attribute.end(3),
                    )
                )

    parser.StartElementHandler = start_element
    parser.StartDoctypeDeclHandler = reject_doctype
    try:
        parser.Parse(source, True)
    except (expat.ExpatError, ValueError):
        return text
    masked = bytearray(source)
    for start, end in spans:
        masked[start:end] = bytes(
            char if char in (10, 13) else 32 for char in source[start:end]
        )
    return masked.decode("utf-8")


def scan_text(name, text, terms, secrets):
    """Report content policy matches without emitting lines or values."""
    findings = []
    ip_lines = svg_ip_text(name, text).splitlines()
    for number, line in enumerate(text.splitlines(), 1):
        for rule, expression in RULES.items():
            if expression.search(line):
                findings.append(Finding(name, number, rule))
        if any(term in line.casefold() for term in terms):
            findings.append(Finding(name, number, "private-deny-term"))
        if any(value in line for value in secrets):
            findings.append(Finding(name, number, "private-credential-value"))
        if any(
            not permitted_ip(hit.group())
            for hit in IPV4.finditer(ip_lines[number - 1])
        ):
            findings.append(Finding(name, number, "concrete-ipv4-address"))
        if any(
            hit.group(1) not in CONTAINER_USERS
            for hit in HOME_PATH.finditer(line)
        ):
            findings.append(Finding(name, number, "personal-home-path"))
    return findings


def scan_blob(name, content, allowed_png, terms, secrets):
    """Scan a text source or explicitly allowlisted synthetic PNG."""
    findings = []
    rule = path_rule(name)
    if rule and not (name == EMPTY_CA_SOURCE and content == b""):
        findings.append(Finding(name, 0, rule))
    for finding in scan_text(name, name, terms, secrets):
        findings.append(Finding(name, 0, finding.rule + "-in-path"))
    if name in allowed_png:
        if not name.endswith(".png") or not content.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            findings.append(Finding(name, 0, "invalid-allowed-png"))
        return findings
    if len(content) > MAX_TEXT_BYTES:
        return findings + [Finding(name, 0, "source-too-large-to-scan")]
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        return findings + [Finding(name, 0, "unapproved-binary-source")]
    if "\x00" in text:
        return findings + [Finding(name, 0, "unapproved-binary-source")]
    return findings + scan_text(name, text, terms, secrets)


def index_entries(root):
    """Read staged paths, modes and blob IDs from the Git index."""
    for record in git(root, "ls-files", "--stage", "-z").split(b"\x00"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, digest, stage = metadata.decode().split()
            if stage != "0":
                raise ValueError("Resolve index conflicts before publishing.")
            yield path.decode("utf-8"), mode, digest


def source_findings(root, repository, allowed_png, terms, secrets):
    """Inspect both staged blobs and their current working-tree versions."""
    findings = []
    count = 0
    if repository:
        entries = list(index_entries(root))
        if not entries:
            raise ValueError("No tracked sources; stage the intended files.")
        for name, mode, digest in entries:
            count += 1
            if mode not in {"100644", "100755"}:
                findings.append(Finding(name, 0, "symlink-or-submodule"))
                continue
            size = int(git(root, "cat-file", "-s", digest))
            if size > MAX_TEXT_BYTES:
                findings.append(Finding(name, 0, "source-too-large-to-scan"))
                continue
            staged = git(root, "cat-file", "blob", digest)
            findings.extend(
                scan_blob(name, staged, allowed_png, terms, secrets)
            )
            path = root / name
            if path.is_symlink():
                findings.append(Finding(name, 0, "working-tree-symlink"))
            elif path.is_file():
                with path.open("rb") as source:
                    current = source.read(MAX_TEXT_BYTES + 1)
                if current != staged:
                    findings.extend(
                        scan_blob(name, current, allowed_png, terms, secrets)
                    )
    else:
        # Pre-init inspection helps prepare the first clean public commit.
        for path in sorted(root.rglob("*")):
            name = path.relative_to(root).as_posix()
            if any(part in LOCAL_CACHE_PARTS for part in path.parts):
                continue
            if path.is_symlink():
                findings.append(Finding(name, 0, "working-tree-symlink"))
            elif path.is_file():
                count += 1
                with path.open("rb") as source:
                    content = source.read(MAX_TEXT_BYTES + 1)
                findings.extend(
                    scan_blob(name, content, allowed_png, terms, secrets)
                )
    return findings, count


def history_findings(root, allowed_png, terms, secrets):
    """Scan reachable trees and commit/tag metadata without repeating blobs."""
    findings = []
    seen = set()
    commits = git(root, "rev-list", "--all").decode().splitlines()
    for commit in commits:
        metadata = git(root, "cat-file", "commit", commit).decode()
        findings.extend(
            scan_text("commit:" + commit[:12], metadata, terms, secrets)
        )
        tree = git(root, "ls-tree", "-r", "-z", "--full-tree", commit)
        for record in tree.split(b"\x00"):
            if not record:
                continue
            metadata, raw_name = record.split(b"\t", 1)
            mode, kind, digest = metadata.decode().split()
            name = raw_name.decode("utf-8")
            label = "history:" + commit[:12] + ":" + name
            if mode not in {"100644", "100755"} or kind != "blob":
                findings.append(Finding(label, 0, "symlink-or-submodule"))
                continue
            if (name, digest) in seen:
                continue
            seen.add((name, digest))
            size = int(git(root, "cat-file", "-s", digest))
            if size > MAX_TEXT_BYTES:
                findings.append(Finding(label, 0, "source-too-large-to-scan"))
                continue
            for finding in scan_blob(
                name,
                git(root, "cat-file", "blob", digest),
                allowed_png,
                terms,
                secrets,
            ):
                findings.append(Finding(label, finding.line, finding.rule))
    tags = (
        git(
            root,
            "for-each-ref",
            "--format=%(objecttype) %(objectname)",
            "refs/tags",
        )
        .decode()
        .splitlines()
    )
    for tag in tags:
        kind, digest = tag.split()
        if kind == "tag":
            metadata = git(root, "cat-file", "tag", digest).decode()
            findings.extend(
                scan_text("tag:" + digest[:12], metadata, terms, secrets)
            )
    return findings, len(commits)


def main():
    """Run local-only publication checks and print value-free findings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--deny-file", type=Path)
    parser.add_argument("--secret-dir", type=Path)
    parser.add_argument("--allow-png", action="append", default=[])
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if not root.is_dir():
            raise ValueError("The source root is not a directory.")
        terms, secrets = private_inputs(args, root)
        repository = is_repository(root)
        if args.history and not repository:
            raise ValueError("History inspection needs this checkout's Git.")
        findings, count = source_findings(
            root, repository, set(args.allow_png), terms, secrets
        )
        if not count:
            raise ValueError("No source files were found.")
        commits = 0
        if args.history:
            old, commits = history_findings(
                root, set(args.allow_png), terms, secrets
            )
            findings.extend(old)
    except (OSError, UnicodeError, ValueError):
        print(
            "ERROR: source inspection could not complete; check input paths, "
            "permissions, UTF-8 and Git state. No input values were printed.",
            file=sys.stderr,
        )
        return 2
    for finding in sorted(
        set(findings), key=lambda item: (item.location, item.line, item.rule)
    ):
        location = finding.location
        if scan_text("location", location, terms, secrets):
            location = "(redacted source path)"
        print(f"FAIL {location}:{finding.line} [{finding.rule}]")
    if findings:
        print("Publication check failed; matching contents were not printed.")
        return 1
    print(f"PASS: {count} source files; {commits} historical commits checked.")
    if not repository:
        print("Pre-init scan only. Repeat --history after staging/committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
