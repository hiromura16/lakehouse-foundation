"""Publish bounded validation evidence into an existing private directory.

Only an operator-selected external directory is used. No directory, ancestor
or existing result is changed. Records are unique, mode 0600 and linked into
place after fsync; retention is a refusal threshold, never automatic deletion.
"""

import fcntl
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from settings import ROOT

MAX_RESULTS = 50
MAX_ENTRIES = 1000
MAX_BYTES = 65536
NAME = re.compile(r"spark-check-[0-9a-f]{32}\.json")


class EvidenceError(ValueError):
    """Expose no path, file contents or operating-system exception text."""


@contextmanager
def private_directory(path):
    """Walk with no-follow directory descriptors and retain identity checks."""
    path = Path(path)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path == Path("/")
        or path.is_relative_to(ROOT)
    ):
        raise EvidenceError(
            "Use an existing external private result directory."
        )
    descriptors = []
    identities = []
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            descriptors.append(child)
            metadata = os.fstat(child)
            identities.append((fd, part, metadata.st_dev, metadata.st_ino))
            # A root-owned sticky directory such as /tmp is traversable, but
            # untrusted writable parents cannot safely hold private evidence.
            if metadata.st_mode & 0o022 and not (
                metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
            ):
                raise EvidenceError("Unsafe result parent permissions.")
            fd = child
        metadata = os.fstat(fd)
        if (
            metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise EvidenceError("Result directory requires private ownership.")

        def verify():
            """Refuse renamed or replaced components before publication."""
            for parent, name, device, inode in identities:
                actual = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (actual.st_dev, actual.st_ino) != (device, inode):
                    raise EvidenceError("Result directory identity changed.")

        verify()
        yield fd, verify
    except (OSError, ValueError):
        raise EvidenceError("Private result directory unavailable.") from None
    finally:
        failed = False
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                failed = True
        if failed:
            raise EvidenceError("Result descriptor cleanup is uncertain.")


def check_capacity(fd):
    """Bound our record count without reading or deleting existing content."""
    count = 0
    with os.scandir(fd) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_ENTRIES:
                raise EvidenceError("Result directory listing limit reached.")
            if NAME.fullmatch(entry.name):
                metadata = entry.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise EvidenceError("Existing result metadata differs.")
                count += 1
    if count >= MAX_RESULTS:
        raise EvidenceError("Archive prior results before another validation.")


def validate_destination(path):
    """Read-only preflight: no write, access probe or metadata correction."""
    with private_directory(path) as (fd, verify):
        check_capacity(fd)
        verify()


def write_result(path, result):
    """Publish once; an existing result, even an equal one, is preserved."""
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", run_id
    ):
        raise EvidenceError("Invalid result identity.")
    payload = json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n"
    payload = payload.encode()
    if len(payload) > MAX_BYTES:
        raise EvidenceError("Validation result is too large.")
    final = "spark-check-" + run_id + ".json"
    temporary = ".spark-check-" + secrets.token_hex(16) + ".tmp"
    with private_directory(path) as (fd, verify):
        # Serialize this writer's capacity check without creating a lock file.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_capacity(fd)
        descriptor = None
        created = False
        identity = None
        try:
            verify()
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
            created = True
            os.fchmod(
                descriptor, 0o600
            )  # New file only; defeat restrictive umask.
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            verify()
            metadata = os.stat(temporary, dir_fd=fd, follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) != identity:
                raise EvidenceError("Result temporary identity changed.")
            os.link(
                temporary,
                final,
                src_dir_fd=fd,
                dst_dir_fd=fd,
                follow_symlinks=False,
            )
            metadata = os.stat(final, dir_fd=fd, follow_symlinks=False)
            if (
                (metadata.st_dev, metadata.st_ino) != identity
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise EvidenceError("Published result identity changed.")
            os.unlink(temporary, dir_fd=fd)
            created = False
            os.fsync(fd)
        except (OSError, ValueError):
            raise EvidenceError(
                "Result publication failed or is uncertain."
            ) from None
        finally:
            failed = False
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    failed = True
            if created:
                try:
                    metadata = os.stat(
                        temporary, dir_fd=fd, follow_symlinks=False
                    )
                    if identity != (metadata.st_dev, metadata.st_ino):
                        raise EvidenceError("Temporary result was replaced.")
                    os.unlink(temporary, dir_fd=fd)
                except (OSError, EvidenceError):
                    failed = True
            if failed:
                raise EvidenceError("Result cleanup failed or is uncertain.")
    return final
