#!/usr/bin/env python3
"""Plan or add one authenticated landing share to an existing Samba server."""

import argparse
import ipaddress
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

MAIN = Path("/etc/samba/smb.conf")
INCLUDE = Path("/etc/samba/smb.conf.d/lakehouse-foundation.conf")
MARKER = "# managed-by=lakehouse-foundation-samba"
BEGIN = "# BEGIN lakehouse-foundation-samba"
END = "# END lakehouse-foundation-samba"
BACKUPS = Path("/var/backups/lakehouse-foundation/samba")
SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*(?:[#;].*)?$", re.M)
INCLUDE_LINE = re.compile(r"^\s*include\s*=\s*(.*?)\s*$", re.M | re.I)


def fail(message):
    """Stop with a concise error without exposing command output."""
    raise SystemExit("ERROR: " + message)


def run(command):
    """Run a prerequisite check without forwarding sensitive diagnostics."""
    result = subprocess.run(
        command, capture_output=True, text=True, check=False
    )
    if result.returncode:
        fail(
            "Command failed: "
            + Path(command[0]).name
            + ". Review local service logs; no output was copied."
        )
    return result.stdout


def no_symlinks(path):
    """Keep managed writes from following redirected configuration paths."""
    for part in (path, *path.parents):
        if part.is_symlink():
            fail("Symlinks are not supported in managed paths.")


def strip_block(text):
    """Remove only the exact include block previously created by this tool."""
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        fail("Malformed or duplicate managed include block.")
    if BEGIN not in text:
        if str(INCLUDE) in text:
            fail(
                "Managed include pathname is already referenced "
                "outside the managed block."
            )
        return text
    before, block = text.split(BEGIN, 1)
    managed, after = block.split(END, 1)
    expected = "\n[global]\n    include = " + str(INCLUDE) + "\n"
    if managed != expected:
        fail(
            "Managed block differs from the expected include; "
            "review it manually."
        )
    remaining = before.rstrip("\n") + after
    if str(INCLUDE) in remaining:
        fail(
            "Managed include pathname is also referenced "
            "outside the managed block."
        )
    return remaining


def inspect_includes(path, desired_share, visited=None):
    """Reject ambiguous includes and conflicting share definitions."""
    if visited is None:
        visited = set()
    path = path.resolve()
    if path in visited or path == INCLUDE.resolve():
        return
    visited.add(path)
    if not path.exists():
        return  # Samba permits optional missing include files.
    if not path.is_file():
        fail("An include is not a regular file.")
    text = path.read_text()
    if any(
        name.casefold() == desired_share.casefold()
        for name in SECTION.findall(text)
    ):
        fail("A same-name share exists outside this tool's managed include.")
    if re.search(
        r"^\s*(?:config\s+backend\s*=\s*registry|"
        r"registry\s+shares\s*=\s*yes)\s*$",
        text,
        re.M | re.I,
    ):
        fail("Registry-based Samba configuration needs a manual review.")
    for raw in INCLUDE_LINE.findall(text):
        if raw == str(INCLUDE):
            if path != MAIN.resolve():
                fail(
                    "Managed include is also referenced "
                    "by another include file."
                )
            continue
        if "%" in raw or raw.casefold() == "registry":
            fail(
                "Dynamic/registry includes need a manual "
                "conflict review before use."
            )
        if any(char in raw for char in "*?[]"):
            fail("Wildcard includes need a manual conflict review before use.")
        child = Path(raw)
        if not child.is_absolute():
            child = path.parent / child
        inspect_includes(child, desired_share, visited)


def render(user, share, landing, allowed):
    """Render one authenticated share with an explicit client allowlist."""
    return f"""{MARKER}
[{share}]
    path = {landing}
    comment = CSV JSON image landing
    read only = no
    browseable = yes
    guest ok = no
    valid users = {user}
    force user = {user}
    create mask = 0660
    directory mask = 0770
    hosts allow = {" ".join(allowed)}
    hosts deny = ALL
"""


def atomic_write(path, text, mode, uid, gid):
    """Replace one file atomically while retaining its required ownership."""
    descriptor, temporary = tempfile.mkstemp(
        prefix=".lakehouse-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(text)
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    """Plan safely, or back up and apply one share with rollback on failure."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "既存Unix/Sambaユーザーの資格情報を保持し、専用共有を準備します。"
            "通常はplanだけで、applyは管理者がbackup・testparm・reloadを行います。"
            "認証付きSMBコピーの受入は別に確認してください。"
        ),
    )
    parser.add_argument(
        "--user",
        required=True,
        help="既存のUnix/Samba両方に登録済みのユーザー。passwordは変更しない",
    )
    parser.add_argument(
        "--allow",
        required=True,
        action="append",
        help="許可するIP/CIDR。複数指定・comma区切り可。全許可の/0は拒否",
    )
    parser.add_argument(
        "--share",
        default="Lakehouse-Landing",
        help=(
            "専用共有の名前。同名の既存共有は置換しない"
            "（既定: Lakehouse-Landing）"
        ),
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=Path("/srv/lakehouse/landing"),
        help="空白のない専用landing絶対パス。private設定のLANDING_DIRと揃える",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="管理者専用。backup・検証後に専用共有を配置し、smbdをreloadする",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="設定や共有を変更せず計画を表示する（既定の動作）",
    )
    args = parser.parse_args()
    if args.apply and args.plan:
        parser.error("Choose --plan or --apply.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", args.user):
        fail("Use a simple existing Unix username.")
    if not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.share
    ) or args.share.casefold() in {"global", "homes", "printers", "ipc$"}:
        fail("Use a simple dedicated share name.")
    if (
        not args.path.is_absolute()
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(args.path))
        or ".." in args.path.parts
        or args.path
        in {Path("/"), Path("/srv"), Path("/home"), Path("/etc"), Path("/var")}
    ):
        fail(
            "Use a dedicated absolute landing path "
            "without spaces or parent traversal."
        )
    allowed = []
    for item in args.allow:
        for token in item.split(","):
            try:
                network = ipaddress.ip_network(token.strip(), strict=False)
            except ValueError:
                fail("--allow accepts literal IP addresses or CIDRs only.")
            if network.prefixlen == 0:
                fail(
                    "An allow-all /0 is not accepted; "
                    "specify the intended client range."
                )
            value = (
                str(network.network_address)
                if network.num_addresses == 1
                else str(network)
            )
            if value not in allowed:
                allowed.append(value)
    try:
        account = pwd.getpwnam(args.user)
    except KeyError:
        fail("Unix user does not exist; create the intended user separately.")
    for path in (MAIN, INCLUDE, args.path):
        no_symlinks(path)
    if not MAIN.is_file():
        fail(
            "Samba is not configured. "
            "Install/configure the host Samba package first."
        )
    if not shutil.which("testparm"):
        fail("testparm is required. Install the Samba utilities first.")
    original = MAIN.read_text()
    unmanaged = strip_block(original)
    include_original = INCLUDE.read_text() if INCLUDE.exists() else None
    if include_original is not None and not include_original.startswith(
        MARKER + "\n"
    ):
        fail("An unmanaged destination include exists; it was not changed.")
    own_names = SECTION.findall(include_original or "")
    if include_original is not None and (
        len(own_names) != 1 or own_names[0].casefold() != args.share.casefold()
    ):
        fail(
            "Managed include has unexpected sections or a different share "
            "name; review it manually."
        )
    effective = run(["testparm", "-s", str(MAIN)])
    effective_names = [
        value.casefold() for value in SECTION.findall(effective)
    ]
    if args.share.casefold() in effective_names and (
        not own_names or own_names[0].casefold() != args.share.casefold()
    ):
        fail("A same-name effective share already exists.")
    inspect_includes(MAIN, args.share)
    share_text = render(args.user, args.share, args.path, allowed)
    block = f"\n{BEGIN}\n[global]\n    include = {INCLUDE}\n{END}\n"
    candidate = unmanaged.rstrip("\n") + "\n" + block
    # Validate a staged include graph before touching the installed files.
    with tempfile.TemporaryDirectory(prefix="lakehouse-samba-") as staging:
        temporary = Path(staging)
        staged_include = temporary / "landing.conf"
        staged_include.write_text(share_text)
        staged_main = temporary / "smb.conf"
        staged_main.write_text(
            candidate.replace(str(INCLUDE), str(staged_include))
        )
        run(["testparm", "-s", str(staged_main)])
    print("Plan: authenticated share " + args.share + " at " + str(args.path))
    print(
        "Plan: existing user "
        + args.user
        + "; explicit client allowlist; no password changes."
    )
    print(
        "Plan: add only "
        + str(INCLUDE)
        + " and one marked include block to smb.conf."
    )
    print(
        "Existing directories, other shares and their ownership are retained."
    )
    if not args.apply:
        print(
            "Read-only plan complete. Use sudo with --apply "
            "to install and reload smbd."
        )
        return
    if os.geteuid() != 0:
        fail("--apply requires root.")
    if not shutil.which("pdbedit") or not shutil.which("systemctl"):
        fail("pdbedit and systemctl are required for --apply.")
    listed_users = run(["pdbedit", "-L"])
    if not any(
        line.partition(":")[0] == args.user
        for line in listed_users.splitlines()
    ):
        fail(
            "Existing Samba account was not found. Set it up separately; "
            "this tool never changes passwords."
        )
    run(["systemctl", "is-active", "--quiet", "smbd.service"])
    main_info = MAIN.stat()
    include_info = INCLUDE.stat() if INCLUDE.exists() else None
    backup = BACKUPS / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        + uuid.uuid4().hex[:8]
    )
    backup.mkdir(parents=True, mode=0o700)
    backup.chmod(0o700)
    for name, content in [
        ("smb.conf", original),
        ("landing.conf", include_original),
    ]:
        if content is not None:
            file = backup / name
            file.write_text(content)
            file.chmod(0o600)
    metadata = backup / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "main_mode": stat.S_IMODE(main_info.st_mode),
                "main_uid": main_info.st_uid,
                "main_gid": main_info.st_gid,
                "include_existed": include_original is not None,
            },
            indent=2,
        )
    )
    metadata.chmod(0o600)
    if (
        MAIN.read_text() != original
        or (INCLUDE.read_text() if INCLUDE.exists() else None)
        != include_original
    ):
        fail(
            "Samba files changed during planning; "
            "rerun after reviewing concurrent changes."
        )
    created = []
    try:
        # Create only missing directories; never chmod/chown existing paths.
        missing = []
        current = args.path
        while not current.exists():
            missing.append(current)
            current = current.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o770 if directory == args.path else 0o755)
            created.append(directory)
            if directory == args.path:
                os.chown(directory, account.pw_uid, account.pw_gid)
        if not args.path.is_dir():
            fail("Landing path is not a directory.")
        for name in ("csv", "json", "images", "quarantine"):
            child = args.path / name
            no_symlinks(child)
            if not child.exists():
                child.mkdir(mode=0o770)
                created.append(child)
                os.chown(child, account.pw_uid, account.pw_gid)
            elif not child.is_dir():
                fail("A landing subdirectory name is occupied by a file.")
        INCLUDE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(INCLUDE, share_text, 0o644, 0, 0)
        atomic_write(
            MAIN,
            candidate,
            stat.S_IMODE(main_info.st_mode),
            main_info.st_uid,
            main_info.st_gid,
        )
        run(["testparm", "-s", str(MAIN)])
        run(["systemctl", "reload", "smbd.service"])
    except BaseException:
        # Restore both configuration files before requesting another reload.
        # Existing directories and account passwords are never rewritten.
        atomic_write(
            MAIN,
            original,
            stat.S_IMODE(main_info.st_mode),
            main_info.st_uid,
            main_info.st_gid,
        )
        if include_original is None:
            INCLUDE.unlink(missing_ok=True)
        else:
            atomic_write(
                INCLUDE,
                include_original,
                stat.S_IMODE(include_info.st_mode),
                include_info.st_uid,
                include_info.st_gid,
            )
        subprocess.run(
            ["systemctl", "reload", "smbd.service"],
            capture_output=True,
            check=False,
        )
        for directory in reversed(created):
            try:
                # Remove only directories created here that remain empty.
                directory.rmdir()
            except OSError:
                pass
        fail(
            "Apply failed; original Samba files were restored. Backup: "
            + str(backup)
        )
    print("Applied and reloaded smbd. Backup: " + str(backup))
    print(
        "Verify SMB authentication and write/read/delete "
        "from an allowed client."
    )


if __name__ == "__main__":
    main()
