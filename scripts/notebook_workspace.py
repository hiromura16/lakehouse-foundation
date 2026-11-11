#!/usr/bin/env python3
"""Inspect or explicitly prepare the single private Jupyter bind workspace.

The default is a mutation-free plan: it does not create directories, adjust
metadata, or test writes. Apply changes only the selected directory and at
most three missing parents, using pinned no-follow directory descriptors.
Existing ancestors and notebook contents are never chmod/chown targets.

Apply and the non-root write probe are operator-controlled host operations.
This module does not install Dokploy, start containers, or change security
policy. Its reports contain fixed codes and no environment values or paths.
"""

import argparse
import errno
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_MODE = 0o2770
PARENT_MODE = 0o750
MAX_MISSING_PARENTS = 3
PROBE_TIMEOUT = 5
PROTECTED_PREFIXES = (
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib32",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var/lib",
    "/var/cache",
    "/var/log",
    "/var/run",
    "/var/lib/docker",
    "/var/lib/containerd",
)
PROTECTED_LEAVES = {
    "/",
    "/home",
    "/opt",
    "/srv",
    "/tmp",
    "/var",
    "/var/lib",
    "/var/cache",
    "/var/tmp",
    "/mnt",
    "/media",
}


class WorkspaceError(ValueError):
    """Expose only an application-owned code, never a private OS message."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def selected(values, root):
    """Validate literal paths and identities without resolving symlinks."""
    if os.name != "posix" or not all(
        hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_PATH")
    ):
        raise WorkspaceError("NOFOLLOW_PLATFORM_UNAVAILABLE")
    raw = values.get("NOTEBOOKS_DIR", "")
    if not isinstance(raw, str) or any(c in raw for c in "\x00\r\n"):
        raise WorkspaceError("INVALID_WORKSPACE_PATH")
    path = Path(raw)
    if (
        not path.is_absolute()
        or str(path) != raw
        or ".." in raw.split("/")
        or "." in raw.split("/")
        or len(path.parts) < 3
    ):
        raise WorkspaceError("INVALID_WORKSPACE_PATH")
    if path.parent == Path("/home") or any(
        component
        in {
            ".ssh",
            ".aws",
            ".azure",
            ".docker",
            ".codex",
            ".git",
            ".config",
            ".kube",
            ".gnupg",
            ".password-store",
        }
        for component in path.parts
    ):
        raise WorkspaceError("PROTECTED_WORKSPACE_PATH")
    root = Path(root).absolute()
    if path == root or root in path.parents:
        raise WorkspaceError("WORKSPACE_INSIDE_CHECKOUT")
    if raw in PROTECTED_LEAVES or any(
        raw == prefix or raw.startswith(prefix + "/")
        for prefix in PROTECTED_PREFIXES
    ):
        raise WorkspaceError("PROTECTED_WORKSPACE_PATH")
    identities = []
    for key, minimum in (("WORKER_UID", 1), ("WORKER_GID", 0)):
        value = values.get(key, "")
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[0-9]{1,10}", value)
            or not minimum <= int(value) <= 2147483647
        ):
            raise WorkspaceError("INVALID_WORKSPACE_IDENTITY")
        identities.append(int(value))
    return path, *identities


def mode_access(metadata, uid, gid, bits):
    """Estimate classic mode access; do not mistake root os.access for proof.

    ACLs, labels, mounts and filesystem behavior can alter access. Only an
    explicit probe as the intended identity establishes write/read/delete.
    """
    shift = 6 if metadata.st_uid == uid else 3 if metadata.st_gid == gid else 0
    return ((stat.S_IMODE(metadata.st_mode) >> shift) & bits) == bits


def same(left, right):
    """Compare inode identity without following a pathname."""
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def parent_safe(metadata, path, uid):
    """Reject untrusted writable ancestors, with Linux's sticky tmp exception.

    A root-owned sticky /tmp or /var/tmp protects entries against other UIDs.
    Existing private parents must not permit group/other entry replacement.
    Descriptor checks additionally detect replacement by a trusted same UID.
    """
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid not in {0, os.geteuid(), uid}:
        return False
    if mode & 0o022:
        return (
            str(path) in {"/tmp", "/var/tmp"}
            and metadata.st_uid == 0
            and bool(mode & stat.S_ISVTX)
        )
    return True


class Walk:
    """Hold every existing directory inode until inspection/apply completes."""

    def __init__(self, path):
        self.path = path
        self.nodes = []
        self.missing = []

    def open(self):
        """Open components relative to their pinned parent, never a symlink."""
        flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open("/", flags)
        self.nodes.append(("/", fd, os.fstat(fd), Path("/")))
        for index, component in enumerate(self.path.parts[1:]):
            try:
                child = os.open(component, flags, dir_fd=self.nodes[-1][1])
            except FileNotFoundError:
                beginning = index + 1
                self.missing = list(self.path.parts[beginning:])
                break
            except OSError:
                raise WorkspaceError(
                    "UNSAFE_OR_INACCESSIBLE_COMPONENT"
                ) from None
            metadata = os.fstat(child)
            child_path = self.nodes[-1][3] / component
            self.nodes.append((component, child, metadata, child_path))
        self.verify()
        return self

    def verify(self):
        """Check each inode still has its inspected parent entry."""
        try:
            if not same(self.nodes[0][2], os.stat("/", follow_symlinks=False)):
                raise WorkspaceError("WORKSPACE_PATH_CHANGED")
            for index, (name, fd, recorded, _) in enumerate(self.nodes):
                if not same(recorded, os.fstat(fd)):
                    raise WorkspaceError("WORKSPACE_PATH_CHANGED")
                if index:
                    current = os.stat(
                        name,
                        dir_fd=self.nodes[index - 1][1],
                        follow_symlinks=False,
                    )
                    if not stat.S_ISDIR(current.st_mode) or not same(
                        recorded, current
                    ):
                        raise WorkspaceError("WORKSPACE_PATH_CHANGED")
        except OSError:
            raise WorkspaceError("WORKSPACE_PATH_CHANGED") from None

    def close(self):
        """Release every descriptor, including a partially opened traversal."""
        for _, fd, _, _ in reversed(self.nodes):
            os.close(fd)
        self.nodes = []


@contextmanager
def walk(path):
    """Keep traversal cleanup reliable even when a component is rejected."""
    handle = Walk(path)
    try:
        yield handle.open()
    finally:
        handle.close()


def base_report(enabled):
    """Use null for unobserved metadata and an explicitly untested probe."""
    return {
        "setting": "NOTEBOOKS_DIR",
        "enabled": enabled,
        "exists": None,
        "owner_match": None,
        "mode_match": None,
        "parents_safe": None,
        "access_estimate": "not_tested",
        "probe": "not_run",
        "action": "none" if not enabled else "blocked",
        "ready": not enabled,
        "applied": False,
        "created_parents": 0,
        "workspace_created": False,
        "metadata_changes": 0,
        "mutation_attempted": False,
        "apply_status": "not_requested",
        "reason": None,
    }


def mount_identity(fd):
    """Read this process's fixed descriptor metadata, never a selected path.

    Different mount IDs identify a mounted filesystem root even for bind
    mounts sharing the same device. Linux procfs must expose fdinfo; absence
    is unknown and therefore cannot authorize ownership changes.
    """
    try:
        path = Path("/proc/self/fdinfo") / str(fd)
        lines = path.read_text(encoding="ascii").splitlines()
        ids = [
            line.split(":", 1)[1].strip()
            for line in lines
            if line.startswith("mnt_id:")
        ]
        if len(ids) != 1 or not ids[0].isdigit():
            raise WorkspaceError("WORKSPACE_MOUNT_IDENTITY_UNAVAILABLE")
        return int(ids[0])
    except (OSError, UnicodeError):
        raise WorkspaceError("WORKSPACE_MOUNT_IDENTITY_UNAVAILABLE") from None


def assess(handle, uid, gid):
    """Inspect parent safety and selected root metadata without mutation."""
    handle.verify()
    exists = not handle.missing
    parents = handle.nodes[:-1] if exists else handle.nodes
    for _, fd, _, path in parents:
        metadata = os.fstat(fd)
        if not parent_safe(metadata, path, uid):
            raise WorkspaceError("UNSAFE_WORKSPACE_PARENT")
        if not mode_access(metadata, uid, gid, 1):
            raise WorkspaceError("WORKSPACE_PARENT_NOT_TRAVERSABLE")
    missing_parents = max(0, len(handle.missing) - 1)
    if missing_parents > MAX_MISSING_PARENTS:
        raise WorkspaceError("TOO_MANY_MISSING_PARENTS")
    if handle.missing and len(handle.nodes) == 1:
        raise WorkspaceError("ROOT_PARENT_CREATION_REFUSED")
    if exists and mount_identity(handle.nodes[-1][1]) != mount_identity(
        handle.nodes[-2][1]
    ):
        raise WorkspaceError("WORKSPACE_MOUNT_ROOT_REFUSED")
    metadata = os.fstat(handle.nodes[-1][1]) if exists else None
    owner = bool(metadata and (metadata.st_uid, metadata.st_gid) == (uid, gid))
    mode = bool(metadata and stat.S_IMODE(metadata.st_mode) == WORKSPACE_MODE)
    access = bool(metadata and mode_access(metadata, uid, gid, 7))
    return {
        "exists": exists,
        "owner_match": owner if exists else None,
        "mode_match": mode if exists else None,
        "parents_safe": True,
        "access_estimate": (
            "not_tested"
            if metadata is None
            else "estimated_allowed"
            if access
            else "estimated_denied"
        ),
        "action": "create"
        if not exists
        else "none"
        if owner and mode
        else "fix",
        "ready": exists and owner and mode and access,
    }


def enabled(values):
    """Profile-off means no workspace validation, inspection or requirement."""
    profiles = values.get("COMPOSE_PROFILES", "")
    return isinstance(profiles, str) and "notebooks" in profiles.split(",")


def inspect_workspace(values, *, root=ROOT):
    """Return a mutation-free plan with fixed codes and no private values."""
    result = base_report(enabled(values))
    if not result["enabled"]:
        return result
    try:
        path, uid, gid = selected(values, root)
        with walk(path) as handle:
            result.update(assess(handle, uid, gid))
    except WorkspaceError as error:
        result.update(action="blocked", ready=False, reason=error.code)
    except OSError:
        result.update(
            action="blocked", ready=False, reason="WORKSPACE_INSPECTION_FAILED"
        )
    return result


def writable_fd(handle, index):
    """Open a metadata descriptor for precisely the inspected inode."""
    handle.verify()
    name, _, expected, _ = handle.nodes[index]
    parent = handle.nodes[index - 1][1]
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except OSError:
        raise WorkspaceError("WORKSPACE_OPERATOR_PRIVILEGE_REQUIRED") from None
    if not same(expected, os.fstat(fd)):
        os.close(fd)
        raise WorkspaceError("WORKSPACE_PATH_CHANGED")
    try:
        handle.verify()
    except WorkspaceError:
        os.close(fd)
        raise
    return fd


def adjust(handle, index, uid, gid, mode, progress):
    """Change only a pinned workspace/new-parent inode, and only if needed."""
    fd = writable_fd(handle, index)
    changed = False
    try:
        handle.verify()
        metadata = os.fstat(fd)
        if (metadata.st_uid, metadata.st_gid) != (uid, gid):
            progress["mutation_attempted"] = True
            os.fchown(fd, uid, gid)
            progress["applied"] = True
            progress["metadata_changes"] += 1
            changed = True
        # chown can clear setgid; inspect again before deciding on chmod.
        handle.verify()
        if stat.S_IMODE(os.fstat(fd).st_mode) != mode:
            progress["mutation_attempted"] = True
            os.fchmod(fd, mode)
            progress["applied"] = True
            progress["metadata_changes"] += 1
            changed = True
        handle.verify()
    except OSError:
        raise WorkspaceError("WORKSPACE_METADATA_CHANGE_FAILED") from None
    finally:
        os.close(fd)
    return changed


def create_missing(handle, uid, gid, progress):
    """Create only the bounded missing suffix with private staging mode."""
    created = 0
    missing = list(handle.missing)
    for index, name in enumerate(missing):
        handle.verify()
        parent_fd = handle.nodes[-1][1]
        try:
            progress["mutation_attempted"] = True
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            progress["applied"] = True
            if index == len(missing) - 1:
                progress["workspace_created"] = True
            else:
                progress["created_parents"] += 1
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            fd = os.open(
                name,
                os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except OSError:
            raise WorkspaceError("WORKSPACE_CREATE_FAILED") from None
        current = os.fstat(fd)
        if not same(before, current) or current.st_uid != os.geteuid():
            os.close(fd)
            raise WorkspaceError("WORKSPACE_PATH_CHANGED")
        handle.nodes.append((name, fd, current, handle.nodes[-1][3] / name))
        handle.verify()
        mode = WORKSPACE_MODE if index == len(missing) - 1 else PARENT_MODE
        adjust(handle, len(handle.nodes) - 1, uid, gid, mode, progress)
        if mode == PARENT_MODE:
            created += 1
    handle.missing = []
    return created


def _probe_child(path, expected_fd, uid, gid):
    """Probe via an exclusive unnamed inode after credential drop.

    Linux O_TMPFILE|O_EXCL prevents naming/linking this probe. Closing the
    zero-link inode deletes it without a path-based unlink race in the shared
    workspace. Unsupported filesystems fail closed without a named fallback.
    Root children clear supplementary groups before dropping identity. A
    non-root child may retain only its already matching primary group.
    """
    result = {
        "probe": "failed",
        "reason": "WORKSPACE_PROBE_FAILED",
        "cleanup": "not_created",
    }
    try:
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
        if (
            os.geteuid() != uid
            or os.getegid() != gid
            or not set(os.getgroups()) <= {gid}
            or uid == 0
        ):
            raise WorkspaceError("WORKSPACE_PROBE_IDENTITY_UNAVAILABLE")
        if not hasattr(os, "O_TMPFILE"):
            return {
                "probe": "unsupported",
                "reason": "WORKSPACE_PROBE_UNSUPPORTED",
                "cleanup": "not_created",
            }
        with walk(Path(path)) as handle:
            if handle.missing or not same(
                os.fstat(expected_fd), os.fstat(handle.nodes[-1][1])
            ):
                raise WorkspaceError("WORKSPACE_PATH_CHANGED")
            directory_fd = handle.nodes[-1][1]
            fd = None
            try:
                payload = secrets.token_bytes(32)
                try:
                    fd = os.open(
                        ".",
                        os.O_TMPFILE | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                except OSError as error:
                    unsupported = error.errno in {
                        errno.EOPNOTSUPP,
                        errno.ENOSYS,
                        errno.EINVAL,
                        errno.EISDIR,
                    }
                    return {
                        "probe": "unsupported" if unsupported else "failed",
                        "reason": (
                            "WORKSPACE_PROBE_UNSUPPORTED"
                            if unsupported
                            else "WORKSPACE_PROBE_PERMISSION_FAILED"
                        ),
                        "cleanup": "not_created",
                    }
                metadata = os.fstat(fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 0
                ):
                    raise WorkspaceError("WORKSPACE_PROBE_INODE_INVALID")
                if os.write(fd, payload) != len(payload):
                    raise WorkspaceError("WORKSPACE_PROBE_SHORT_WRITE")
                os.lseek(fd, 0, os.SEEK_SET)
                if os.read(fd, 33) != payload:
                    raise WorkspaceError("WORKSPACE_PROBE_CONTENT_MISMATCH")
                result = {"probe": "passed", "reason": None}
            except (OSError, WorkspaceError):
                result = {
                    "probe": "failed",
                    "reason": "WORKSPACE_PROBE_PERMISSION_FAILED",
                }
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                        result["cleanup"] = "unlinked_inode_closed"
                    except OSError:
                        result = {
                            "probe": "cleanup_failed",
                            "reason": "WORKSPACE_PROBE_CLEANUP_FAILED",
                            "cleanup": "close_failed",
                        }
            handle.verify()
    except WorkspaceError as error:
        result = {
            "probe": "failed",
            "reason": error.code,
            "cleanup": result.get("cleanup", "not_created"),
        }
    except OSError:
        result = {
            "probe": "failed",
            "reason": "WORKSPACE_PROBE_PERMISSION_FAILED",
            "cleanup": result.get("cleanup", "not_created"),
        }
    return result


def run_probe(handle, uid, gid):
    """Use a bounded child without secrets or extra supplementary groups."""
    handle.verify()
    fd = handle.nodes[-1][1]
    command = [
        sys.executable,
        str(Path(__file__).absolute()),
        "--probe-child",
        str(fd),
        str(uid),
        str(gid),
        str(handle.path),
    ]
    try:
        reply = subprocess.run(
            command,
            pass_fds=(fd,),
            env={"PATH": os.defpath},
            cwd="/",
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
        if reply.returncode != 0 or len(reply.stdout) > 1024:
            raise WorkspaceError("WORKSPACE_PROBE_PROCESS_FAILED")
        result = json.loads(reply.stdout)
        if not isinstance(result, dict) or set(result) != {
            "probe",
            "reason",
            "cleanup",
        }:
            raise WorkspaceError("WORKSPACE_PROBE_PROCESS_FAILED")
        if result["probe"] not in {
            "passed",
            "failed",
            "cleanup_failed",
            "unsupported",
        } or result["cleanup"] not in {
            "not_created",
            "unlinked_inode_closed",
            "close_failed",
        }:
            raise WorkspaceError("WORKSPACE_PROBE_PROCESS_FAILED")
        reason = result["reason"]
        if reason is not None and (
            not isinstance(reason, str)
            or not re.fullmatch(r"[A-Z_]{1,64}", reason)
        ):
            raise WorkspaceError("WORKSPACE_PROBE_PROCESS_FAILED")
        if result["probe"] == "passed" and (
            reason is not None or result["cleanup"] != "unlinked_inode_closed"
        ):
            raise WorkspaceError("WORKSPACE_PROBE_PROCESS_FAILED")
    except (subprocess.SubprocessError, ValueError, OSError):
        result = {
            "probe": "failed",
            "reason": "WORKSPACE_PROBE_PROCESS_FAILED",
            "cleanup": "child_exit_closes_unlinked_inode",
        }
    handle.verify()
    return result


def prepare_workspace(values, *, apply=False, probe=False, root=ROOT):
    """Plan by default; require explicit apply/probe for host mutations."""
    if type(apply) is not bool or type(probe) is not bool:
        raise WorkspaceError("INVALID_WORKSPACE_OPERATION")
    if not apply and not probe:
        return inspect_workspace(values, root=root)
    result = base_report(enabled(values))
    if not result["enabled"]:
        return result
    metadata_complete = False
    try:
        path, uid, gid = selected(values, root)
        with walk(path) as handle:
            result.update(assess(handle, uid, gid))
            before = {
                key: result[key]
                for key in ("exists", "owner_match", "mode_match", "action")
            }
            if apply:
                result["apply_status"] = "unchanged"
                if handle.missing:
                    create_missing(handle, uid, gid, result)
                else:
                    adjust(
                        handle,
                        len(handle.nodes) - 1,
                        uid,
                        gid,
                        WORKSPACE_MODE,
                        result,
                    )
                if result["applied"]:
                    result["apply_status"] = "applied"
                result.update(assess(handle, uid, gid))
                result["before"] = before
                if not result["ready"]:
                    raise WorkspaceError("WORKSPACE_METADATA_NOT_READY")
                metadata_complete = True
            if probe:
                if not result["ready"]:
                    raise WorkspaceError("WORKSPACE_PROBE_REQUIRES_READY_ROOT")
                result.update(run_probe(handle, uid, gid))
                if result["probe"] != "passed":
                    result.update(action="blocked", ready=False)
    except WorkspaceError as error:
        result.update(action="blocked", ready=False, reason=error.code)
    except OSError:
        result.update(
            action="blocked", ready=False, reason="WORKSPACE_APPLY_FAILED"
        )
    if apply and result["action"] == "blocked":
        if result["mutation_attempted"] and not metadata_complete:
            result["apply_status"] = "partial_or_unknown"
            if result["applied"] is False:
                result["applied"] = None
        elif not result["mutation_attempted"]:
            result["apply_status"] = "blocked_before_mutation"
    return result


def main():
    """Emit one safe JSON record without invoking another installer."""
    if len(sys.argv) == 6 and sys.argv[1] == "--probe-child":
        try:
            fd, uid, gid = map(int, sys.argv[2:5])
            if fd < 0 or not 1 <= uid <= 2147483647:
                raise ValueError
            if not 0 <= gid <= 2147483647:
                raise ValueError
            result = _probe_child(sys.argv[5], fd, uid, gid)
        except (ValueError, OSError):
            result = {
                "probe": "failed",
                "reason": "WORKSPACE_PROBE_PROCESS_FAILED",
                "cleanup": "not_created",
            }
        print(json.dumps(result))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly approve narrow host metadata changes",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Explicitly approve a non-root create/read/delete test",
    )
    args = parser.parse_args()
    try:
        from settings import load

        result = prepare_workspace(
            load(args.env_file), apply=args.apply, probe=args.probe
        )
    except (ValueError, OSError):
        result = base_report(True)
        result.update(reason="WORKSPACE_SETTINGS_UNAVAILABLE")
    print(json.dumps(result, sort_keys=True))
    return 1 if result["action"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
