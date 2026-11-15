"""Keep the human workspace key separate from the server API credential.

Only a salted scrypt hash is persisted, beside the API's private settings.
The gateway never mounts that directory. Revision checks invalidate all old
browser sessions after a change without rotating SQL, MCP or runner secrets.
"""

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import deque
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from lifecycle_store import open_directory, private_bytes
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class CredentialError(ValueError):
    """Carry a fixed reason only; never stringify credentials or OS errors."""


def unique_record(pairs):
    """Treat ambiguous private JSON as damage, never as reset permission."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise CredentialError("credential_state_unavailable")
        value[key] = item
    return value


class CredentialStore:
    """Serialize rotations and reject damaged or foreign state."""

    def __init__(self, directory, bootstrap):
        self.directory = directory / ".portal-auth"
        self.bootstrap = bootstrap
        self.lock = threading.RLock()
        self.attempts = deque(maxlen=10)

    def revision(self):
        """Fingerprint the high-entropy bootstrap with domain separation."""
        return hmac.new(
            self.bootstrap.encode(), b"portal-login-revision-v1", "sha256"
        ).hexdigest()

    @staticmethod
    def read(descriptor):
        """Never fall back to bootstrap when stored state is damaged."""
        data = json.loads(
            private_bytes(descriptor, "credential.json", maximum=4096),
            object_pairs_hook=unique_record,
        )
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "salt", "digest", "revision"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or not all(
                isinstance(data[key], str) and re.fullmatch(pattern, data[key])
                for key, pattern in (
                    ("salt", r"[a-f0-9]{32}"),
                    ("digest", r"[a-f0-9]{64}"),
                    ("revision", r"[a-f0-9]{64}"),
                )
            )
        ):
            raise CredentialError("credential_state_unavailable")
        return data

    def _read(self):
        """Allow absent initial state and reject incomplete existing state."""
        try:
            descriptor = open_directory(self.directory)
        except FileNotFoundError:
            return None
        try:
            return self.read(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def hashed(value, salt):
        """Bound memory/CPU independently of fields in persisted state."""
        return hashlib.scrypt(
            value.encode("ascii"),
            salt=bytes.fromhex(salt),
            # OWASP's32MiB alternative trades CPU for bounded process memory.
            # Parameters are code-owned, never taken from a stored record.
            n=32768,
            r=8,
            p=3,
            dklen=32,
            maxmem=64 * 1024 * 1024,
        ).hex()

    def allowed(self):
        """Limit expensive verification globally in this single API process."""
        now = time.monotonic()
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            raise CredentialError("credential_rate_limited")
        self.attempts.append(now)

    def matches(self, supplied, data):
        if not isinstance(supplied, str) or not re.fullmatch(
            r"[!-~]{1,256}", supplied
        ):
            return False
        expected = self.bootstrap if data is None else data["digest"]
        actual = (
            supplied if data is None else self.hashed(supplied, data["salt"])
        )
        return bool(expected) and secrets.compare_digest(actual, expected)

    def status(self):
        with self.lock:
            data = self._read()
            return {
                "configured": data is not None,
                "revision": self.revision()
                if data is None
                else data["revision"],
            }

    def verify(self, supplied):
        with self.lock:
            self.allowed()
            data = self._read()
            return {
                "valid": self.matches(supplied, data),
                "revision": self.revision()
                if data is None
                else data["revision"],
            }

    @contextlib.contextmanager
    def writer(self):
        """Use a no-follow owned lock inside the protected leaf directory."""
        descriptor = open_directory(self.directory, create=True)
        handle = None
        try:
            handle = os.open(
                "rotation.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=descriptor,
            )
            info = os.fstat(handle)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise CredentialError("credential_state_unavailable")
            # A crashed/slow second process must not hold an API thread
            # indefinitely. The owner can retry after resolving contention.
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield descriptor
        finally:
            if handle is not None:
                os.close(handle)
            os.close(descriptor)

    def change(self, current, new):
        """Authenticate before creating state; replace only this owned hash."""
        if not re.fullmatch(r"[!-~]{24,128}", new):
            raise CredentialError("invalid_new_credential")
        with self.lock:
            self.allowed()
            previous = self._read()
            if not self.matches(current, previous):
                raise CredentialError("current_credential_invalid")
            if secrets.compare_digest(current, new):
                raise CredentialError("new_credential_unchanged")
            with self.writer() as descriptor:
                # A concurrent cooperating process may have rotated after
                # the proof above. Recheck under the cross-process lock.
                try:
                    previous = self.read(descriptor)
                except FileNotFoundError:
                    previous = None
                if not self.matches(current, previous):
                    raise CredentialError("current_credential_invalid")
                salt = secrets.token_hex(16)
                data = {
                    "version": 1,
                    "salt": salt,
                    "digest": self.hashed(new, salt),
                    "revision": secrets.token_hex(32),
                }
                temporary = ".credential-" + secrets.token_hex(16)
                handle = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=descriptor,
                )
                try:
                    with os.fdopen(handle, "w") as output:
                        json.dump(data, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(
                        temporary,
                        "credential.json",
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                    )
                    os.fsync(descriptor)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
                return {"changed": True, "revision": data["revision"]}


class Proof(BaseModel):
    """SecretStr keeps a validated request out of accidental repr output."""

    model_config = ConfigDict(extra="forbid", strict=True)
    token: Annotated[SecretStr, Field(min_length=1, max_length=256)]


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    current_token: Annotated[SecretStr, Field(min_length=1, max_length=256)]
    new_token: Annotated[SecretStr, Field(min_length=24, max_length=128)]


def credential_router(store, authorize, bootstrap):
    """Internal endpoints blocked by the generic browser proxy."""
    router = APIRouter(
        prefix="/portal-auth", dependencies=[Depends(authorize)]
    )
    credentials = CredentialStore(store.path.parent, bootstrap)

    def checked(function):
        try:
            return function()
        except CredentialError as error:
            code = str(error)
            status = {
                "current_credential_invalid": 401,
                "invalid_new_credential": 422,
                "new_credential_unchanged": 422,
                "credential_rate_limited": 429,
            }.get(code, 503)
            raise HTTPException(status, detail={"code": code}) from None
        except (OSError, ValueError, TypeError, KeyError):
            raise HTTPException(
                503, detail={"code": "credential_state_unavailable"}
            ) from None

    @router.get("/status", include_in_schema=False)
    def status():
        return checked(credentials.status)

    @router.post("/verify", include_in_schema=False)
    def verify(proof: Proof):
        return checked(
            lambda: credentials.verify(proof.token.get_secret_value())
        )

    @router.post("/change", include_in_schema=False)
    def change(proof: Change):
        # SSO identity is managed by Access; there is no weak fallback key.
        if store.applied.get("PORTAL_AUTH_MODE", "token") != "token":
            raise HTTPException(
                403, detail={"code": "credential_change_disabled"}
            )
        return checked(
            lambda: credentials.change(
                proof.current_token.get_secret_value(),
                proof.new_token.get_secret_value(),
            )
        )

    return router
