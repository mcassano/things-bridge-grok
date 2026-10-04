"""Helper-side confirmation gate for Things changes.

``change_preview`` mints a single-use nonce bound to the SHA-256 of the
normalized change. ``change`` must echo the nonce back, and the change it
carries must normalize to exactly the same payload. Nonces expire after
CONFIRM_NONCE_TTL seconds and are deleted on use, so they cannot be replayed.

This enforces "preview, then confirm" inside the helper: a process that writes
a ``change`` request directly into the bridge without a matching preview is
rejected before the native confirmation dialog is ever shown.

Design note: launchd starts the helper once per batch of requests, so it exits
between ``change_preview`` and ``change``. Nonces are therefore persisted as
per-nonce files under ``<bridge>/nonces/`` rather than kept in memory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import stat
import time
from contextlib import contextmanager
from typing import Any, Iterator


CONFIRM_NONCE_TTL = 120  # seconds

# URL-safe base64 alphabet used by secrets.token_urlsafe.
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_NOFOLLOW_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_MAX_NONCE_RECORD_BYTES = 4096
_REAP_GRACE_SECONDS = 5


class WriteGateError(Exception):
    """Raised when a change request fails to clear the preview/confirm gate."""


def _bridge_dir() -> pathlib.Path:
    override = os.environ.get("THINGS_BRIDGE_DIR")
    if not override:
        raise RuntimeError("THINGS_BRIDGE_DIR is required")
    return pathlib.Path(os.path.abspath(os.path.expanduser(override)))


def _validate_private_directory(fd: int, label: str) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError(f"{label} is not a directory")
    if metadata.st_uid != os.getuid():
        raise RuntimeError(f"{label} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(f"{label} must not have group/world permissions")


def _open_bridge_root() -> int:
    root = _bridge_dir()
    if not root.is_absolute() or root == pathlib.Path("/"):
        raise RuntimeError("bridge root must be a non-root absolute path")
    fd = os.open("/", _DIR_OPEN_FLAGS)
    try:
        for component in root.parts[1:]:
            next_fd = os.open(component, _DIR_OPEN_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _validate_private_directory(fd, f"bridge root {root}")
        return fd
    except RuntimeError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(f"unsafe bridge root {root}: {exc}") from exc


@contextmanager
def _nonce_dir_fd() -> Iterator[int]:
    fd = _open_bridge_root()
    try:
        try:
            os.mkdir("nonces", mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        try:
            nonce_fd = os.open("nonces", _DIR_OPEN_FLAGS, dir_fd=fd)
        except OSError as exc:
            raise RuntimeError(f"unsafe nonce directory: {exc}") from exc
        os.close(fd)
        fd = nonce_fd
        _validate_private_directory(fd, "nonce directory")
        yield fd
    finally:
        os.close(fd)


def _validate_nonce_file(fd: int, label: str) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} is not a regular file")
    if metadata.st_uid != os.getuid():
        raise RuntimeError(f"{label} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RuntimeError(f"{label} must have mode 600")
    if metadata.st_size > _MAX_NONCE_RECORD_BYTES:
        raise RuntimeError(f"{label} is too large")


def _read_nonce_record(directory_fd: int, name: str) -> dict:
    fd = os.open(name, os.O_RDONLY | _FILE_NOFOLLOW_FLAGS, dir_fd=directory_fd)
    try:
        _validate_nonce_file(fd, name)
        chunks = []
        remaining = _MAX_NONCE_RECORD_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_NONCE_RECORD_BYTES:
            raise RuntimeError(f"{name} is too large")
        record = json.loads(raw.decode("utf-8"))
        if not isinstance(record, dict):
            raise RuntimeError(f"{name} must contain a JSON object")
        return record
    finally:
        os.close(fd)


def _unlink_at(directory_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _valid_expiry(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def change_hash(change: dict) -> str:
    """Bind a nonce to an exact normalized change.

    Canonical JSON (sorted keys, no insignificant whitespace, UTF-8) makes the
    digest independent of key order in the client's request.
    """
    canonical = json.dumps(
        change, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mint_confirm_nonce(change: dict) -> str:
    """Persist a nonce record for ``change`` and return the nonce string.

    The record is written to a temporary name and renamed into place so the
    reaper never observes a half-written file.
    """
    nonce = secrets.token_urlsafe(24)
    record = {
        "change_hash": change_hash(change),
        "expires_at": int(time.time()) + CONFIRM_NONCE_TTL,
    }
    name = f"{nonce}.json"
    tmp = f".{nonce}.json.tmp"
    with _nonce_dir_fd() as nonce_fd:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW_FLAGS,
            0o600,
            dir_fd=nonce_fd,
        )
        try:
            _validate_nonce_file(fd, tmp)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(record, handle)
            os.replace(tmp, name, src_dir_fd=nonce_fd, dst_dir_fd=nonce_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            _unlink_at(nonce_fd, tmp)
    return nonce


def consume_confirm_nonce(nonce: Any, change: dict) -> None:
    """Validate and consume ``nonce`` for ``change``.

    Raises WriteGateError on any failure. The nonce file is atomically renamed
    to a claimed name first, so two concurrent ``change`` requests cannot both
    pass. The claimed record is always deleted, whether validation succeeds or
    fails, so a rejected nonce cannot be retried with a corrected payload.
    """
    if not isinstance(nonce, str) or not nonce:
        raise WriteGateError("missing confirm_nonce; call change_preview first")
    if not _NONCE_RE.match(nonce):
        # Defense in depth against path traversal through a crafted nonce.
        raise WriteGateError("invalid confirm_nonce format")

    name = f"{nonce}.json"
    claimed = f"{nonce}.claimed"
    with _nonce_dir_fd() as nonce_fd:
        try:
            os.rename(name, claimed, src_dir_fd=nonce_fd, dst_dir_fd=nonce_fd)
        except FileNotFoundError:
            raise WriteGateError(
                "confirm_nonce not recognized; change_preview must precede change"
            ) from None
        except OSError as exc:
            raise WriteGateError(f"failed to claim confirm_nonce: {exc}") from exc

        try:
            try:
                record = _read_nonce_record(nonce_fd, claimed)
            except Exception as exc:
                raise WriteGateError(f"malformed nonce record: {exc}") from exc

            expires_at = record.get("expires_at", 0)
            if not _valid_expiry(expires_at):
                raise WriteGateError(
                    "malformed nonce record: expires_at must be a finite number"
                )
            if int(time.time()) > expires_at:
                raise WriteGateError(
                    f"confirm_nonce expired (TTL {CONFIRM_NONCE_TTL}s); "
                    "call change_preview again"
                )
            if change_hash(change) != record.get("change_hash"):
                raise WriteGateError(
                    "change differs from preview; call change_preview again"
                )
        finally:
            _unlink_at(nonce_fd, claimed)


def reap_expired_nonces() -> None:
    """Remove nonce records from previews that never received a ``change``.

    Safe to call at helper startup. Files younger than a short grace period are
    left alone so an in-flight mint is never deleted. Orphaned ``.claimed``
    files from an interrupted ``change`` are removed after the TTL.
    """
    now = int(time.time())
    with _nonce_dir_fd() as nonce_fd:
        for name in os.listdir(nonce_fd):
            if name.startswith("."):
                continue
            if not (name.endswith(".json") or name.endswith(".claimed")):
                continue
            try:
                metadata = os.stat(name, dir_fd=nonce_fd, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                    continue
                if stat.S_IMODE(metadata.st_mode) & 0o077:
                    continue
                age = now - metadata.st_mtime
                if name.endswith(".claimed"):
                    if age > CONFIRM_NONCE_TTL:
                        _unlink_at(nonce_fd, name)
                    continue
                if age < _REAP_GRACE_SECONDS:
                    continue
                try:
                    record = _read_nonce_record(nonce_fd, name)
                    expires_at = record.get("expires_at", 0)
                    if not _valid_expiry(expires_at) or now > expires_at:
                        _unlink_at(nonce_fd, name)
                except Exception:
                    _unlink_at(nonce_fd, name)
            except Exception:
                continue
