#!/usr/bin/env python3
"""Grok Bot Things helper

Runs on macOS, triggered by launchd when a new file lands in control/requests/.
Drains the request queue, dispatches each whitelisted action against an
in-memory snapshot of the Things 3 database, writes a response JSON into
control/responses/, and deletes the request.

Security posture:
  - Actions are strictly whitelisted (no eval/exec, no shell).
  - All SQL is static and parameterized; the Things database is opened
    read-only and copied into memory with SQLite's online backup API.
  - The read policy (area / project / tag scopes) is applied before any
    title or note is placed in a response.
  - Verification codes, card numbers, and SSN patterns are redacted.
  - Changes require a preview nonce bound to the exact normalized change,
    then explicit approval in a native macOS dialog, before a short static
    AppleScript drives Things.
  - Runtime files are opened relative to verified, no-follow directory
    descriptors; response writes are atomic (tmp + rename) and mode 600.

This script should be invoked only by the signed `grokbot-things-helper`
wrapper. Running it directly works, but without the environment hardening the
wrapper provides and without the wrapper's Full Disk Access grant.
"""

from __future__ import annotations

import fcntl
import glob
import html
import importlib.util
import json
import os
import plistlib
import re
import sqlite3
import stat
import statistics
import subprocess
import sys
import time
import traceback
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

# ---------------------------------------------------------------------------
# Identity and versions
# ---------------------------------------------------------------------------
HELPER_VERSION = "1.0.0"
PROTOCOL_VERSION = "1.0"
LAUNCHD_LABEL = "com.mcassano.grokbot-things"
HOST_DISPLAY_NAME = os.environ.get("THINGS_HOST_DISPLAY_NAME") or "Grok Bot"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CODE_ROOT = Path(__file__).resolve().parent.parent
if "THINGS_BRIDGE_DIR" in os.environ:
    _bridge_root_value = os.environ["THINGS_BRIDGE_DIR"]
    if not _bridge_root_value:
        raise RuntimeError("THINGS_BRIDGE_DIR must not be empty")
else:
    _bridge_root_value = str(CODE_ROOT)
    # The write gate locates the nonce store through the same variable.
    os.environ["THINGS_BRIDGE_DIR"] = _bridge_root_value
BRIDGE_ROOT = Path(os.path.abspath(os.path.expanduser(_bridge_root_value)))
POLICY_ROOT = BRIDGE_ROOT / "policy"
REQUESTS_DIR = BRIDGE_ROOT / "control" / "requests"
RESPONSES_DIR = BRIDGE_ROOT / "control" / "responses"
LOG_PATH = BRIDGE_ROOT / "control" / "log.txt"
READ_POLICY_PATH = POLICY_ROOT / "read_policy.txt"
WRITE_POLICY_PATH = POLICY_ROOT / "write_policy.txt"
BLOCKLIST_PATH = POLICY_ROOT / "blocked_scopes.txt"
ALLOWLIST_PATH = Path(
    os.path.abspath(
        os.path.expanduser(
            os.environ.get("THINGS_READ_ALLOWLIST") or str(POLICY_ROOT / "allowed_scopes.txt")
        )
    )
)
WRITE_GATE_PATH = CODE_ROOT / "bin" / "write_gate.py"
CONFIRM_HELPER_PATH = CODE_ROOT / "bin" / "grokbot-things-confirm"

THINGS_BUNDLE_ID = "com.culturedcode.ThingsMac"
THINGS_APP_PATHS = (
    Path("/Applications/Things3.app"),
    Path.home() / "Applications" / "Things3.app",
)
THINGS_GROUP_CONTAINER = (
    Path.home() / "Library" / "Group Containers" / "JLMPQHK86H.com.culturedcode.ThingsMac"
)
THINGS_DB_RELATIVE = Path("Things Database.thingsdatabase") / "main.sqlite"

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
MAX_REQUEST_BYTES = 64 * 1024
RESPONSE_TTL_S = 60 * 60
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 3
DEFAULT_SNAPSHOT_MAX_MB = 512

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
MAX_WINDOW_DAYS = 365
MAX_SEARCH_LEN = 200
MAX_REF_LEN = 200
LIST_NOTES_SNIPPET = 280
MAX_NOTES_RETURNED = 20_000

MAX_TITLE_LEN = 500
MAX_NOTES_LEN = 10_000
MAX_TAGS_PER_CHANGE = 20
MAX_SCHEDULE_YEARS_AHEAD = 10

OSASCRIPT_TIMEOUT_S = 20
CONFIRM_DIALOG_TIMEOUT_S = 75  # the dialog itself times out at 60s
WORKER_LOCK_TIMEOUT_S = CONFIRM_DIALOG_TIMEOUT_S + OSASCRIPT_TIMEOUT_S + 10
MAX_DRAIN_PASSES = 50


# ---------------------------------------------------------------------------
# Write gate (loaded by absolute path)
# ---------------------------------------------------------------------------
# The wrapper runs Python with -I (isolated mode), which deliberately leaves
# this file's directory off sys.path so a stray module dropped into bin/ cannot
# shadow an import. The trusted sibling is loaded by its absolute path instead.
def _load_write_gate():
    try:
        metadata = WRITE_GATE_PATH.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"write_gate not found: {WRITE_GATE_PATH}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"write_gate must be a regular file: {WRITE_GATE_PATH}")
    if metadata.st_uid not in (0, os.getuid()):
        raise RuntimeError(f"write_gate must be owned by root or the current user: {WRITE_GATE_PATH}")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RuntimeError(f"write_gate must not be group/world-writable: {WRITE_GATE_PATH}")
    spec = importlib.util.spec_from_file_location("things_write_gate", WRITE_GATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load write_gate from {WRITE_GATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_write_gate = _load_write_gate()
CONFIRM_NONCE_TTL = _write_gate.CONFIRM_NONCE_TTL
WriteGateError = _write_gate.WriteGateError
mint_confirm_nonce = _write_gate.mint_confirm_nonce
consume_confirm_nonce = _write_gate.consume_confirm_nonce
reap_expired_nonces = _write_gate.reap_expired_nonces


# ---------------------------------------------------------------------------
# Secure runtime filesystem access
# ---------------------------------------------------------------------------
_DIR_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_NOFOLLOW_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class UnsafeRuntimePath(RuntimeError):
    """Raised when user-owned bridge state is not a safe local file or directory."""


def _validate_private_directory(fd: int, label: str) -> None:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafeRuntimePath(f"{label} is not a directory")
    if metadata.st_uid != os.getuid():
        raise UnsafeRuntimePath(f"{label} is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UnsafeRuntimePath(f"{label} must not have group/world permissions")


def _open_bridge_root() -> int:
    """Open the absolute bridge path one component at a time without symlinks."""
    root = Path(os.path.abspath(str(BRIDGE_ROOT)))
    if not root.is_absolute() or root == Path("/"):
        raise UnsafeRuntimePath("bridge root must be a non-root absolute path")

    fd = os.open("/", _DIR_OPEN_FLAGS)
    try:
        for component in root.parts[1:]:
            next_fd = os.open(component, _DIR_OPEN_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _validate_private_directory(fd, f"bridge root {root}")
        return fd
    except UnsafeRuntimePath:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        raise UnsafeRuntimePath(f"unsafe bridge root {root}: {exc}") from exc


def _runtime_relative_parts(path: Path) -> tuple[str, ...]:
    root = Path(os.path.abspath(str(BRIDGE_ROOT)))
    candidate = Path(os.path.abspath(str(path)))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeRuntimePath(f"runtime path escapes bridge root: {candidate}") from exc
    if any(part in ("", ".", "..") or "/" in part for part in relative.parts):
        raise UnsafeRuntimePath(f"invalid runtime path: {candidate}")
    return relative.parts


@contextmanager
def _private_directory_fd(path: Path, *, create: bool = False) -> Iterator[int]:
    """Yield an anchored descriptor for a private directory below the bridge."""
    parts = _runtime_relative_parts(path)
    fd = _open_bridge_root()
    try:
        for component in parts:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(component, _DIR_OPEN_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise UnsafeRuntimePath(f"unsafe runtime directory {path}: {exc}") from exc
            os.close(fd)
            fd = next_fd
            _validate_private_directory(fd, str(path))
        yield fd
    finally:
        os.close(fd)


def _validate_regular_file(fd: int, label: str, *, private: bool = False) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeRuntimePath(f"{label} is not a regular file")
    if metadata.st_uid != os.getuid():
        raise UnsafeRuntimePath(f"{label} is not owned by the current user")
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UnsafeRuntimePath(f"{label} must not have group/world permissions")
    return metadata


def _stat_regular_at(directory_fd: int, name: str, *, private: bool = False) -> os.stat_result:
    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeRuntimePath(f"{name} is not a regular file")
    if metadata.st_uid != os.getuid():
        raise UnsafeRuntimePath(f"{name} is not owned by the current user")
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise UnsafeRuntimePath(f"{name} must not have group/world permissions")
    return metadata


# ---------------------------------------------------------------------------
# Logging (never logs titles, notes, or other Things content)
# ---------------------------------------------------------------------------
def _rotate_log(control_fd: int) -> None:
    try:
        current = _stat_regular_at(control_fd, LOG_PATH.name, private=True)
    except FileNotFoundError:
        return
    if current.st_size < LOG_MAX_BYTES:
        return

    oldest = f"{LOG_PATH.name}.{LOG_BACKUP_COUNT}"
    try:
        _stat_regular_at(control_fd, oldest, private=True)
    except FileNotFoundError:
        pass
    else:
        os.unlink(oldest, dir_fd=control_fd)

    for index in range(LOG_BACKUP_COUNT - 1, 0, -1):
        source = f"{LOG_PATH.name}.{index}"
        destination = f"{LOG_PATH.name}.{index + 1}"
        try:
            _stat_regular_at(control_fd, source, private=True)
        except FileNotFoundError:
            continue
        os.replace(source, destination, src_dir_fd=control_fd, dst_dir_fd=control_fd)

    os.replace(
        LOG_PATH.name,
        f"{LOG_PATH.name}.1",
        src_dir_fd=control_fd,
        dst_dir_fd=control_fd,
    )


def log(message: str) -> None:
    try:
        with _private_directory_fd(LOG_PATH.parent, create=True) as control_fd:
            _rotate_log(control_fd)
            fd = os.open(
                LOG_PATH.name,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | _FILE_NOFOLLOW_FLAGS,
                0o600,
                dir_fd=control_fd,
            )
            try:
                _validate_regular_file(fd, str(LOG_PATH), private=True)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    fd = -1
                    handle.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
            finally:
                if fd >= 0:
                    os.close(fd)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Things encodings
# ---------------------------------------------------------------------------
# Status (TMTask.status)
STATUS_OPEN = 0
STATUS_CANCELED = 2
STATUS_COMPLETED = 3
_STATUS_NAMES = {STATUS_OPEN: "open", STATUS_CANCELED: "canceled", STATUS_COMPLETED: "completed"}

# Item type (TMTask.type)
TYPE_TODO = 0
TYPE_PROJECT = 1
TYPE_HEADING = 2
_TYPE_NAMES = {TYPE_TODO: "to-do", TYPE_PROJECT: "project", TYPE_HEADING: "heading"}

# Start (TMTask.start)
START_INBOX = 0
START_ANYTIME = 1
START_SOMEDAY = 2

# startBucket: 1 places a Today item in "This Evening".
BUCKET_EVENING = 1

# Built-in list identifiers. These are stable across localizations, unlike the
# list names ("Today", "Heute", ...).
LIST_SOURCE_IDS = {
    "inbox": "TMInboxListSource",
    "today": "TMTodayListSource",
    "anytime": "TMNextListSource",
    "someday": "TMSomedayListSource",
}


def decode_things_date(value: Any) -> date | None:
    """Decode a Things packed date (startDate, deadline).

    Things 3.15+ stores calendar days as an integer with the bit layout
    ``YYYYYYYYYYYMMMMDDDDD0000000``: year << 16 | month << 12 | day << 7.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        packed = int(value)
    except (TypeError, ValueError):
        return None
    if packed <= 0:
        return None
    try:
        return date(packed >> 16, (packed >> 12) & 0xF, (packed >> 7) & 0x1F)
    except ValueError:
        return None


def encode_things_date(day: date) -> int:
    return (day.year << 16) | (day.month << 12) | (day.day << 7)


def decode_timestamp(value: Any) -> datetime | None:
    """Decode a Unix-epoch REAL timestamp (creationDate, stopDate, ...)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


_NOTE_XML_RE = re.compile(r"^\s*<note\b[^>]*>(.*)</note>\s*$", re.DOTALL)
_XML_TAG_RE = re.compile(r"<[^>]+>")


def normalize_notes(raw: Any) -> str:
    """Return plain-text notes.

    Current Things versions store Markdown-flavored plain text. Databases
    migrated from early Things 3 releases can still hold an XML ``<note>``
    wrapper, which is unwrapped here.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = str(raw)
    match = _NOTE_XML_RE.match(text)
    if match:
        text = html.unescape(_XML_TAG_RE.sub("", match.group(1)))
    return text


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _int(value: Any, default: int = 0) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
_CODE_NEAR_WORD = re.compile(
    r"(?:\b(?:code|verification|OTP|passcode|one[- ]time)\b[^0-9]{0,20}\b(\d{4,8})\b)"
    r"|(?:\b(\d{4,8})\b[^0-9]{0,20}\b(?:code|verification|OTP|passcode)\b)",
    re.IGNORECASE,
)
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def redact(text: str) -> str:
    """Mask common secrets. Best-effort; the read policy is the real boundary."""
    if not text:
        return text
    text = _CODE_NEAR_WORD.sub("[REDACTED-2FA]", text)
    text = _CARD_RE.sub(
        lambda m: "[REDACTED-CARD]" if len(re.sub(r"\D", "", m.group(0))) >= 13 else m.group(0),
        text,
    )
    return _SSN_RE.sub("[REDACTED-SSN]", text)


def _snippet(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Library: a read-only, in-memory model of one database snapshot
# ---------------------------------------------------------------------------
@dataclass
class Area:
    uuid: str
    title: str
    index: int
    tag_ids: list[str] = field(default_factory=list)


@dataclass
class Tag:
    uuid: str
    title: str
    parent: str | None
    shortcut: str
    index: int


@dataclass
class Task:
    uuid: str
    type: int
    title: str
    notes: str
    status: int
    trashed: bool
    start: int
    start_date: date | None
    start_bucket: int
    deadline: date | None
    deadline_suppressed: bool
    stop_date: datetime | None
    created: datetime | None
    modified: datetime | None
    area: str | None
    project: str | None
    heading: str | None
    index: int
    today_index: int
    checklist_total: int
    checklist_open: int
    is_template: bool
    is_repeating: bool
    tag_ids: list[str] = field(default_factory=list)


@dataclass
class ChecklistItem:
    uuid: str
    task: str
    title: str
    status: int
    index: int


# Columns the helper cannot work without. Optional columns that are absent in
# older or newer schemas are selected as NULL instead.
_REQUIRED_COLUMNS = {
    "TMTask": (
        "uuid", "type", "status", "trashed", "title", "notes", "start",
        "startDate", "deadline", "stopDate", "creationDate", "area", "project",
        "heading", "index",
    ),
    "TMArea": ("uuid", "title"),
    "TMTag": ("uuid", "title"),
    "TMTaskTag": ("tasks", "tags"),
}
_OPTIONAL_TASK_COLUMNS = (
    "startBucket", "deadlineSuppressionDate", "userModificationDate",
    "todayIndex", "checklistItemsCount", "openChecklistItemsCount",
    "rt1_recurrenceRule", "rt1_repeatingTemplate",
)


class UnsupportedSchema(RuntimeError):
    """Raised when the Things database lacks a column the helper relies on."""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # Table names come from the static allowlist above, never from requests.
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _column_expr(name: str, available: set[str]) -> str:
    return f'"{name}"' if name in available else f'NULL AS "{name}"'


class Library:
    """Areas, tags, and tasks from one snapshot, with Things list semantics."""

    def __init__(self, conn: sqlite3.Connection, *, today: date | None = None) -> None:
        self.conn = conn
        self.today = today or date.today()
        self.areas: dict[str, Area] = {}
        self.tags: dict[str, Tag] = {}
        self.tasks: dict[str, Task] = {}
        self._checklists: dict[str, list[ChecklistItem]] | None = None
        self._load()

    # -- loading ----------------------------------------------------------
    def _load(self) -> None:
        columns = {table: _table_columns(self.conn, table) for table in _REQUIRED_COLUMNS}
        for table, required in _REQUIRED_COLUMNS.items():
            missing = [name for name in required if name not in columns[table]]
            if missing:
                raise UnsupportedSchema(
                    f"unsupported Things database schema: {table} lacks {', '.join(missing)}"
                )

        area_cols = columns["TMArea"]
        for row in self.conn.execute(
            f'SELECT "uuid", "title", {_column_expr("index", area_cols)} FROM TMArea'
        ):
            self.areas[_text(row[0])] = Area(_text(row[0]), _text(row[1]), _int(row[2]))

        tag_cols = columns["TMTag"]
        for row in self.conn.execute(
            f'SELECT "uuid", "title", {_column_expr("parent", tag_cols)}, '
            f'{_column_expr("shortcut", tag_cols)}, {_column_expr("index", tag_cols)} FROM TMTag'
        ):
            self.tags[_text(row[0])] = Tag(
                _text(row[0]), _text(row[1]), _text(row[2]) or None, _text(row[3]), _int(row[4])
            )

        task_cols = columns["TMTask"]
        selected = list(_REQUIRED_COLUMNS["TMTask"]) + list(_OPTIONAL_TASK_COLUMNS)
        select_sql = ", ".join(_column_expr(name, task_cols) for name in selected)
        for row in self.conn.execute(f"SELECT {select_sql} FROM TMTask"):
            r = dict(zip(selected, row))
            task_type = _int(r["type"], -1)
            if task_type not in _TYPE_NAMES:
                continue
            self.tasks[_text(r["uuid"])] = Task(
                uuid=_text(r["uuid"]),
                type=task_type,
                title=_text(r["title"]),
                notes=normalize_notes(r["notes"]),
                status=_int(r["status"]),
                trashed=bool(_int(r["trashed"])),
                start=_int(r["start"], START_ANYTIME),
                start_date=decode_things_date(r["startDate"]),
                start_bucket=_int(r["startBucket"]),
                deadline=decode_things_date(r["deadline"]),
                deadline_suppressed=r["deadlineSuppressionDate"] is not None,
                stop_date=decode_timestamp(r["stopDate"]),
                created=decode_timestamp(r["creationDate"]),
                modified=decode_timestamp(r["userModificationDate"]),
                area=_text(r["area"]) or None,
                project=_text(r["project"]) or None,
                heading=_text(r["heading"]) or None,
                index=_int(r["index"]),
                today_index=_int(r["todayIndex"]),
                checklist_total=_int(r["checklistItemsCount"]),
                checklist_open=_int(r["openChecklistItemsCount"]),
                is_template=r["rt1_recurrenceRule"] is not None,
                is_repeating=bool(r["rt1_repeatingTemplate"]) or r["rt1_recurrenceRule"] is not None,
            )

        for task_id, tag_id in self.conn.execute("SELECT tasks, tags FROM TMTaskTag"):
            task = self.tasks.get(_text(task_id))
            if task is not None and _text(tag_id) in self.tags:
                task.tag_ids.append(_text(tag_id))

        if "TMAreaTag" in {
            row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }:
            for area_id, tag_id in self.conn.execute("SELECT areas, tags FROM TMAreaTag"):
                area = self.areas.get(_text(area_id))
                if area is not None and _text(tag_id) in self.tags:
                    area.tag_ids.append(_text(tag_id))

    def checklist(self, task_id: str) -> list[ChecklistItem]:
        if self._checklists is None:
            self._checklists = defaultdict(list)
            try:
                rows = self.conn.execute(
                    'SELECT "uuid", "task", "title", "status", "index" FROM TMChecklistItem'
                ).fetchall()
            except sqlite3.Error as exc:
                log(f"checklist items unavailable: {exc}")
                rows = []
            for row in rows:
                item = ChecklistItem(_text(row[0]), _text(row[1]), _text(row[2]), _int(row[3]), _int(row[4]))
                self._checklists[item.task].append(item)
            for items in self._checklists.values():
                items.sort(key=lambda item: item.index)
        return self._checklists.get(task_id, [])

    # -- relationships ----------------------------------------------------
    def project_of(self, task: Task) -> Task | None:
        """The project a to-do belongs to, directly or through a heading."""
        if task.type == TYPE_PROJECT:
            return None
        if task.project:
            candidate = self.tasks.get(task.project)
            if candidate is not None and candidate.type == TYPE_PROJECT:
                return candidate
        if task.heading:
            heading = self.tasks.get(task.heading)
            if heading is not None and heading.project:
                candidate = self.tasks.get(heading.project)
                if candidate is not None and candidate.type == TYPE_PROJECT:
                    return candidate
        return None

    def heading_of(self, task: Task) -> Task | None:
        if task.heading:
            heading = self.tasks.get(task.heading)
            if heading is not None and heading.type == TYPE_HEADING:
                return heading
        return None

    def area_of(self, task: Task) -> Area | None:
        if task.area and task.area in self.areas:
            return self.areas[task.area]
        project = self.project_of(task)
        if project is not None and project.area in self.areas:
            return self.areas[project.area]  # type: ignore[index]
        return None

    def tag_with_ancestors(self, tag_ids: Iterable[str]) -> list[Tag]:
        seen: dict[str, Tag] = {}
        for tag_id in tag_ids:
            current = self.tags.get(tag_id)
            depth = 0
            while current is not None and current.uuid not in seen and depth < 32:
                seen[current.uuid] = current
                current = self.tags.get(current.parent) if current.parent else None
                depth += 1
        return list(seen.values())

    def effective_tags(self, task: Task) -> list[Tag]:
        """Own tags plus tags inherited from the project and area, with ancestors."""
        tag_ids = list(task.tag_ids)
        project = self.project_of(task)
        if project is not None:
            tag_ids.extend(project.tag_ids)
        area = self.area_of(task)
        if area is not None:
            tag_ids.extend(area.tag_ids)
        return self.tag_with_ancestors(tag_ids)

    # -- state ------------------------------------------------------------
    def is_live(self, task: Task) -> bool:
        """Not in the Trash, directly or through its project or heading."""
        if task.trashed:
            return False
        heading = self.heading_of(task)
        if heading is not None and heading.trashed:
            return False
        project = self.project_of(task)
        return project is None or not project.trashed

    def is_actionable(self, task: Task) -> bool:
        """Open, live, not a repeating template, and inside an open project."""
        if task.status != STATUS_OPEN or task.is_template or not self.is_live(task):
            return False
        project = self.project_of(task)
        return project is None or project.status == STATUS_OPEN

    def in_today(self, task: Task) -> bool:
        if task.start_date is not None:
            return task.start in (START_ANYTIME, START_SOMEDAY) and task.start_date <= self.today
        # Things also surfaces unscheduled items whose deadline has arrived.
        return (
            task.start != START_INBOX
            and task.deadline is not None
            and task.deadline <= self.today
            and not task.deadline_suppressed
        )

    def when(self, task: Task) -> str:
        """Things' user-facing schedule: inbox, today, evening, anytime, someday, or a date."""
        if task.start == START_INBOX:
            return "inbox"
        if task.start_date is not None:
            if task.start_date <= self.today:
                return "evening" if task.start_bucket == BUCKET_EVENING else "today"
            return task.start_date.isoformat()
        return "someday" if task.start == START_SOMEDAY else "anytime"

    def scope(self, task: Task) -> "Scope":
        area = self.area_of(task)
        project = task if task.type == TYPE_PROJECT else self.project_of(task)
        return Scope(
            areas=((area.uuid, area.title),) if area else (),
            projects=((project.uuid, project.title),) if project else (),
            tags=tuple((tag.uuid, tag.title) for tag in self.effective_tags(task)),
            unfiled=area is None and project is None,
        )

    def area_scope(self, area: Area) -> "Scope":
        return Scope(
            areas=((area.uuid, area.title),),
            projects=(),
            tags=tuple((tag.uuid, tag.title) for tag in self.tag_with_ancestors(area.tag_ids)),
            unfiled=False,
        )

    # -- lookups ----------------------------------------------------------
    def find_area(self, ref: str) -> Area | None:
        if ref in self.areas:
            return self.areas[ref]
        folded = ref.casefold()
        matches = [area for area in self.areas.values() if area.title.casefold() == folded]
        if len(matches) > 1:
            raise ValueError(f"area {ref!r} is ambiguous; use its id")
        return matches[0] if matches else None

    def find_project(self, ref: str) -> Task | None:
        candidate = self.tasks.get(ref)
        if candidate is not None and candidate.type == TYPE_PROJECT:
            return candidate
        folded = ref.casefold()
        matches = [
            task
            for task in self.tasks.values()
            if task.type == TYPE_PROJECT and self.is_live(task) and task.title.casefold() == folded
        ]
        open_matches = [task for task in matches if task.status == STATUS_OPEN]
        if len(open_matches) == 1:
            return open_matches[0]
        if len(matches) > 1:
            raise ValueError(f"project {ref!r} is ambiguous; use its id")
        return matches[0] if matches else None

    def find_tag(self, ref: str) -> Tag | None:
        if ref in self.tags:
            return self.tags[ref]
        folded = ref.casefold()
        matches = [tag for tag in self.tags.values() if tag.title.casefold() == folded]
        if len(matches) > 1:
            raise ValueError(f"tag {ref!r} is ambiguous; use its id")
        return matches[0] if matches else None

    def tag_descendants(self, tag: Tag) -> set[str]:
        result = {tag.uuid}
        changed = True
        while changed:
            changed = False
            for candidate in self.tags.values():
                if candidate.parent in result and candidate.uuid not in result:
                    result.add(candidate.uuid)
                    changed = True
        return result


# ---------------------------------------------------------------------------
# Read policy
# ---------------------------------------------------------------------------
SCOPE_KINDS = ("area", "project", "tag")
UNFILED_ENTRY = "unfiled"


@dataclass(frozen=True)
class Scope:
    areas: tuple[tuple[str, str], ...]
    projects: tuple[tuple[str, str], ...]
    tags: tuple[tuple[str, str], ...]
    unfiled: bool


@dataclass(frozen=True)
class ScopeEntry:
    kind: str
    value: str  # casefolded id or title; empty for "unfiled"


@dataclass(frozen=True)
class PrivacyPolicy:
    mode: str
    blocklist: tuple[ScopeEntry, ...]
    allowlist: tuple[ScopeEntry, ...]


def parse_scope_entry(line: str) -> ScopeEntry:
    """Parse ``area:<name or id>``, ``project:...``, ``tag:...``, or ``unfiled``."""
    entry = line.strip()
    if not entry or len(entry) > MAX_REF_LEN + 10 or any(ord(ch) < 0x20 for ch in entry):
        raise ValueError("scope entry must be 1..210 printable characters")
    if entry.casefold() == UNFILED_ENTRY:
        return ScopeEntry(UNFILED_ENTRY, "")
    kind, sep, value = entry.partition(":")
    kind = kind.strip().casefold()
    value = value.strip()
    if not sep or kind not in SCOPE_KINDS or not value:
        raise ValueError(
            "scope entry must be area:<name>, project:<name>, tag:<name>, or unfiled"
        )
    return ScopeEntry(kind, value.casefold())


def _policy_file_is_trusted(path: Path, metadata: os.stat_result, *, require_root: bool) -> bool:
    if not stat.S_ISREG(metadata.st_mode):
        log(f"policy rejected: {path} must be a regular file")
        return False
    if require_root:
        if metadata.st_uid != 0:
            log(f"policy rejected: {path} must be root-owned")
            return False
        if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            log(f"policy rejected: {path} has group/world permissions")
            return False
        return True
    if metadata.st_uid not in (0, os.getuid()):
        log(f"policy rejected: {path} must be owned by the current user")
        return False
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        log(f"policy rejected: {path} must not be group/world-writable")
        return False
    return True


def _read_policy_lines(path: Path, *, require_root: bool = False) -> tuple[str, ...] | None:
    """Return meaningful lines, ``()`` when absent, or None when untrusted."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ()
    if not _policy_file_is_trusted(path, metadata, require_root=require_root):
        return None
    lines = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return tuple(lines)


def _load_scope_list(path: Path, *, require_root: bool = False) -> tuple[ScopeEntry, ...] | None:
    lines = _read_policy_lines(path, require_root=require_root)
    if lines is None:
        return None
    entries = []
    for number, line in enumerate(lines, 1):
        try:
            entries.append(parse_scope_entry(line))
        except ValueError as exc:
            log(f"policy {path.name}: ignoring invalid entry on content line {number}: {exc}")
    return tuple(entries)


def load_privacy_policy() -> PrivacyPolicy:
    override = os.environ.get("THINGS_READ_POLICY", "runtime")
    if override in ("allowlist", "blocklist"):
        mode = override
    else:
        lines = _read_policy_lines(READ_POLICY_PATH)
        if lines is None:
            mode = "allowlist"
        elif not lines:
            mode = "blocklist"
        else:
            mode = lines[0].strip().lower()
        if mode not in ("allowlist", "blocklist"):
            log(f"invalid read policy {mode!r}; failing closed in allowlist mode")
            mode = "allowlist"

    require_root = os.environ.get("THINGS_REQUIRE_ROOT_POLICY") == "1"
    blocklist = _load_scope_list(BLOCKLIST_PATH)
    allowlist = _load_scope_list(ALLOWLIST_PATH, require_root=require_root)
    if blocklist is None:
        # An untrusted blocklist cannot be honored, and ignoring it could
        # disclose what the user meant to hide. Fail closed.
        log("untrusted blocklist; failing closed with an empty allowlist")
        return PrivacyPolicy(mode="allowlist", blocklist=(), allowlist=())
    return PrivacyPolicy(mode=mode, blocklist=blocklist, allowlist=allowlist or ())


WRITE_POLICIES = ("confirm", "preview", "disabled")


def load_write_policy() -> str:
    """Return the effective write policy.

    ``confirm`` (default): preview nonce, chat approval, and the native dialog.
    ``preview``: preview nonce and chat approval only; no dialog.
    ``disabled``: no changes at all.

    Anything unexpected or untrusted disables writes. Hardened installs ignore
    ``preview``: the policy file is user-writable, and the dialog is the only
    layer that stops a same-user process from changing Things silently.
    """
    lines = _read_policy_lines(WRITE_POLICY_PATH)
    if lines is None:
        return "disabled"
    if not lines:
        return "confirm"
    value = lines[0].strip().lower()
    if value not in WRITE_POLICIES:
        log(f"invalid write policy {value!r}; writes disabled")
        return "disabled"
    if value == "preview" and os.environ.get("THINGS_REQUIRE_ROOT_POLICY") == "1":
        log("write policy 'preview' is not honored by hardened installs; using 'confirm'")
        return "confirm"
    return value


def _entry_matches(entry: ScopeEntry, scope: Scope) -> bool:
    if entry.kind == UNFILED_ENTRY:
        return scope.unfiled
    candidates = {"area": scope.areas, "project": scope.projects, "tag": scope.tags}[entry.kind]
    return any(entry.value in (uid.casefold(), title.casefold()) for uid, title in candidates)


def is_blocked(scope: Scope, policy: PrivacyPolicy) -> bool:
    return any(_entry_matches(entry, scope) for entry in policy.blocklist)


def is_allowed(scope: Scope, policy: PrivacyPolicy) -> bool:
    if is_blocked(scope, policy):
        return False
    if policy.mode == "allowlist":
        return any(_entry_matches(entry, scope) for entry in policy.allowlist)
    return True


class View:
    """A Library filtered through the read policy. All actions read through this."""

    def __init__(self, library: Library, policy: PrivacyPolicy) -> None:
        self.lib = library
        self.policy = policy
        self._visible: dict[str, bool] = {}

    @property
    def today(self) -> date:
        return self.lib.today

    def visible(self, task: Task) -> bool:
        cached = self._visible.get(task.uuid)
        if cached is None:
            cached = is_allowed(self.lib.scope(task), self.policy)
            self._visible[task.uuid] = cached
        return cached

    def area_visible(self, area: Area) -> bool:
        scope = self.lib.area_scope(area)
        if is_blocked(scope, self.policy):
            return False
        if is_allowed(scope, self.policy):
            return True
        # In allowlist mode, an area is listed when something inside it is visible.
        return any(
            self.lib.area_of(task) is area and self.visible(task)
            for task in self.lib.tasks.values()
            if task.type in (TYPE_TODO, TYPE_PROJECT)
        )

    def items(self, *, types: tuple[int, ...] = (TYPE_TODO, TYPE_PROJECT)) -> Iterator[Task]:
        for task in self.lib.tasks.values():
            if task.type in types and self.lib.is_live(task) and self.visible(task):
                yield task

    def actionable(self, *, types: tuple[int, ...] = (TYPE_TODO, TYPE_PROJECT)) -> Iterator[Task]:
        for task in self.items(types=types):
            if self.lib.is_actionable(task):
                yield task

    def tag_visible(self, tag: Tag) -> bool:
        scope = Scope((), (), tuple((t.uuid, t.title) for t in self.lib.tag_with_ancestors([tag.uuid])), False)
        return not is_blocked(scope, self.policy)

    def require_item(self, ref: str, *, types: tuple[int, ...] = (TYPE_TODO, TYPE_PROJECT)) -> Task:
        task = self.lib.tasks.get(ref)
        if task is None or task.type not in types or not self.lib.is_live(task) or not self.visible(task):
            # Hidden and missing items are indistinguishable by design.
            raise ValueError(f"no visible item with id {ref!r}")
        return task


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def _ref(uid: str, title: str) -> dict[str, str]:
    return {"id": uid, "title": redact(title)}


def serialize_item(view: View, task: Task, *, notes: str = "snippet") -> dict[str, Any]:
    """Compact, policy-safe representation of a to-do or project.

    ``notes`` is ``"none"``, ``"snippet"`` (first LIST_NOTES_SNIPPET chars), or
    ``"full"`` (capped at MAX_NOTES_RETURNED).
    """
    lib = view.lib
    item: dict[str, Any] = {
        "id": task.uuid,
        "type": _TYPE_NAMES[task.type],
        "title": redact(task.title),
        "status": _STATUS_NAMES.get(task.status, "unknown"),
        "when": lib.when(task),
        "start_date": _iso_date(task.start_date),
        "deadline": _iso_date(task.deadline),
        "tags": [redact(lib.tags[tag_id].title) for tag_id in task.tag_ids if tag_id in lib.tags],
        "url": f"things:///show?id={task.uuid}",
    }
    if task.deadline is not None and task.status == STATUS_OPEN:
        item["overdue"] = task.deadline < view.today
    project = lib.project_of(task)
    item["project"] = _ref(project.uuid, project.title) if project else None
    area = lib.area_of(task)
    item["area"] = _ref(area.uuid, area.title) if area else None
    heading = lib.heading_of(task)
    if heading is not None:
        item["heading"] = redact(heading.title)
    if task.type == TYPE_TODO and task.checklist_total:
        item["checklist"] = {"open": task.checklist_open, "total": task.checklist_total}
    if task.type == TYPE_PROJECT:
        children = [
            child for child in lib.tasks.values()
            if child.type == TYPE_TODO and lib.project_of(child) is task and lib.is_live(child)
            and not child.is_template and view.visible(child)
        ]
        item["progress"] = {
            "open": sum(1 for child in children if child.status == STATUS_OPEN),
            "completed": sum(1 for child in children if child.status == STATUS_COMPLETED),
            "canceled": sum(1 for child in children if child.status == STATUS_CANCELED),
        }
    if task.is_repeating:
        item["repeating"] = True
    if task.status != STATUS_OPEN:
        item["completed_at" if task.status == STATUS_COMPLETED else "canceled_at"] = _iso_datetime(task.stop_date)
    item["created_at"] = _iso_datetime(task.created)
    item["modified_at"] = _iso_datetime(task.modified)
    if notes != "none":
        text = redact(task.notes)
        if notes == "snippet":
            text = _snippet(text, LIST_NOTES_SNIPPET)
        else:
            if len(text) > MAX_NOTES_RETURNED:
                item["notes_truncated"] = True
            text = text[:MAX_NOTES_RETURNED]
        item["notes"] = text
    return item


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------
_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _reject_unknown(params: dict, allowed: Iterable[str]) -> None:
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise ValueError(f"unknown parameter(s): {', '.join(unknown)}")


def validate_int(value: Any, name: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{name} must be an integer")
        value = int(value)
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None
    if not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return value


def validate_limit(value: Any, default: int = DEFAULT_LIMIT) -> int:
    return validate_int(value, "limit", default=default, minimum=1, maximum=MAX_LIMIT)


def validate_bool(value: Any, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def validate_ref(value: Any, name: str) -> str:
    """A Things id or an exact (case-insensitive) title."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    if len(value) > MAX_REF_LEN:
        raise ValueError(f"{name} is too long")
    if any(ord(ch) < 0x20 for ch in value):
        raise ValueError(f"{name} contains control characters")
    return value.strip()


def validate_id(value: Any, name: str = "id") -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"{name} must be a Things id")
    return value


def validate_search(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("term is required")
    if len(value) > MAX_SEARCH_LEN:
        raise ValueError(f"term must be at most {MAX_SEARCH_LEN} characters")
    return value.strip()


def validate_title(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("title must be a string")
    title = value.strip()
    if not title:
        raise ValueError("title cannot be empty")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(f"title must be at most {MAX_TITLE_LEN} characters")
    for ch in title:
        if ord(ch) < 0x20 or ord(ch) == 0x7F or ch in "  ":
            raise ValueError("title cannot contain line breaks or control characters")
    return title


def validate_notes(value: Any, name: str = "notes") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_NOTES_LEN:
        raise ValueError(f"{name} must be at most {MAX_NOTES_LEN} characters")
    for ch in text:
        if (ord(ch) < 0x20 and ch not in "\n\t") or ord(ch) == 0x7F:
            raise ValueError(f"{name} contains disallowed control character U+{ord(ch):04X}")
    return text


def parse_iso_date(value: Any, name: str) -> date:
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        raise ValueError(f"{name} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} is not a valid calendar date") from None


def validate_deadline(value: Any) -> str:
    day = parse_iso_date(value, "deadline")
    if not date(2000, 1, 1) <= day <= date(2100, 12, 31):
        raise ValueError("deadline must be between 2000-01-01 and 2100-12-31")
    return day.isoformat()


def validate_when(value: Any, today: date, *, allow_inbox: bool) -> str:
    """Normalize a schedule to inbox, today, anytime, someday, or an ISO date.

    Relative values ("tomorrow") resolve to a concrete date here, so the
    confirmation dialog and the nonce both bind the exact day.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("when must be a string")
    normalized = value.strip().lower()
    if normalized == "evening":
        raise ValueError(
            "when=evening is not supported by the Things AppleScript interface; use today"
        )
    if normalized == "inbox":
        if not allow_inbox:
            raise ValueError("projects cannot be moved to the Inbox")
        return normalized
    if normalized in ("today", "anytime", "someday"):
        return normalized
    if normalized == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    day = parse_iso_date(normalized, "when")
    if day < today:
        raise ValueError("when cannot be in the past")
    if day == today:
        return "today"
    if day > today + timedelta(days=366 * MAX_SCHEDULE_YEARS_AHEAD):
        raise ValueError(f"when must be within {MAX_SCHEDULE_YEARS_AHEAD} years")
    return day.isoformat()


def resolve_tags(value: Any, lib: Library) -> list[Tag]:
    """Resolve tag titles or ids to existing tags, preserving order."""
    if not isinstance(value, list):
        raise ValueError("tags must be a list of tag names")
    if len(value) > MAX_TAGS_PER_CHANGE:
        raise ValueError(f"at most {MAX_TAGS_PER_CHANGE} tags")
    tags: list[Tag] = []
    for raw in value:
        ref = validate_ref(raw, "tag")
        tag = lib.find_tag(ref)
        if tag is None:
            raise ValueError(f"tag {ref!r} does not exist; create it in Things first")
        if "," in tag.title:
            raise ValueError(f"tag {tag.title!r} contains a comma and cannot be set by AppleScript")
        if tag not in tags:
            tags.append(tag)
    return tags


# ---------------------------------------------------------------------------
# Database snapshot
# ---------------------------------------------------------------------------
def find_things_db() -> Path | None:
    """Locate the live Things 3 database.

    Things 3.15+ keeps it under a ThingsData-XXXXX directory in the group
    container; older releases kept it directly in the container. When several
    candidates exist, the most recently modified wins. THINGS_DB_PATH is
    honored for tests and diagnostics; the wrapper never forwards it.
    """
    override = os.environ.get("THINGS_DB_PATH")
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() else None
    candidates = [
        Path(p)
        for p in glob.glob(str(THINGS_GROUP_CONTAINER / "ThingsData-*" / THINGS_DB_RELATIVE))
    ]
    legacy = THINGS_GROUP_CONTAINER / THINGS_DB_RELATIVE
    if legacy.is_file():
        candidates.append(legacy)
    existing = []
    for candidate in candidates:
        try:
            existing.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    if not existing:
        return None
    return max(existing)[1]


def _get_snapshot_max_bytes() -> int:
    value = os.environ.get("THINGS_SNAPSHOT_MAX_MB", "").strip()
    if value:
        try:
            megabytes = int(value)
            if megabytes > 0:
                return megabytes * 1024 * 1024
        except ValueError:
            pass
        log(f"THINGS_SNAPSHOT_MAX_MB={value!r} invalid; using default {DEFAULT_SNAPSHOT_MAX_MB} MB")
    return DEFAULT_SNAPSHOT_MAX_MB * 1024 * 1024


def snapshot_things_db() -> sqlite3.Connection:
    """Copy the Things database into memory with SQLite's online backup API.

    The source is opened read-only without ``immutable=1``: Things may be
    writing while we read, and the backup API is what yields a consistent
    snapshot that includes committed rows still in the WAL. Nothing is written
    to disk. The caller closes the returned connection.
    """
    db_path = find_things_db()
    if db_path is None:
        raise RuntimeError(
            "Things database not found; is Things 3 installed, and does the helper "
            "have Full Disk Access?"
        )
    try:
        size = db_path.stat().st_size
        wal = db_path.with_name(db_path.name + "-wal")
        size += wal.stat().st_size if wal.exists() else 0
    except OSError as exc:
        raise RuntimeError(f"cannot stat Things database: {exc}") from exc
    limit = _get_snapshot_max_bytes()
    if size > limit:
        raise RuntimeError(
            f"Things database + WAL ({size // (1024 * 1024)} MB) exceeds the snapshot limit "
            f"({limit // (1024 * 1024)} MB); raise THINGS_SNAPSHOT_MAX_MB"
        )

    source = None
    destination = None
    try:
        source = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro&cache=private", uri=True, timeout=5)
        destination = sqlite3.connect(":memory:")
        source.backup(destination)
        return destination
    except sqlite3.Error as exc:
        if destination is not None:
            destination.close()
        raise RuntimeError(
            f"cannot read the Things database ({exc}); grant Full Disk Access to the wrapper"
        ) from exc
    finally:
        if source is not None:
            source.close()


def things_app_info() -> dict[str, Any]:
    for path in THINGS_APP_PATHS:
        info_plist = path / "Contents" / "Info.plist"
        try:
            with info_plist.open("rb") as handle:
                info = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        return {
            "installed": True,
            "path": str(path),
            "version": info.get("CFBundleShortVersionString"),
            "bundle_id": info.get("CFBundleIdentifier"),
        }
    return {"installed": False, "path": None, "version": None, "bundle_id": None}


def _probe_db_readable(path: Path | None) -> bool:
    """True when this process can actually read the database (TCC-aware)."""
    if path is None:
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(16).startswith(b"SQLite format 3")
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Read actions
# ---------------------------------------------------------------------------
LISTS = ("inbox", "today", "upcoming", "anytime", "someday", "logbook")


def _list_sort_key(view: View, name: str) -> Callable[[Task], Any]:
    if name == "today":
        return lambda t: (t.start_bucket == BUCKET_EVENING, t.today_index, t.index)
    if name == "upcoming":
        return lambda t: (t.start_date or date.max, t.index)
    if name == "logbook":
        return lambda t: -(t.stop_date.timestamp() if t.stop_date else 0)
    return lambda t: (t.index, t.created or datetime.min)


def _in_list(view: View, task: Task, name: str, horizon: date, since: datetime) -> bool:
    lib = view.lib
    if name == "logbook":
        return (
            task.status in (STATUS_COMPLETED, STATUS_CANCELED)
            and task.stop_date is not None
            and task.stop_date >= since
        )
    if not lib.is_actionable(task):
        return False
    if name == "inbox":
        return task.type == TYPE_TODO and task.start == START_INBOX
    if name == "today":
        return lib.in_today(task)
    if name == "upcoming":
        return task.start_date is not None and view.today < task.start_date <= horizon
    if name == "anytime":
        if task.start != START_ANYTIME or (task.start_date and task.start_date > view.today):
            return False
        project = lib.project_of(task)
        if project is not None and (
            project.start == START_SOMEDAY
            or (project.start_date is not None and project.start_date > view.today)
        ):
            return False
        return True
    if name == "someday":
        return task.start == START_SOMEDAY and task.start_date is None
    return False


def _filter_scope(view: View, params: dict) -> Callable[[Task], bool]:
    lib = view.lib
    checks: list[Callable[[Task], bool]] = []
    if params.get("area") is not None:
        area = lib.find_area(validate_ref(params["area"], "area"))
        if area is None or not view.area_visible(area):
            raise ValueError("no visible area matches 'area'")
        checks.append(lambda t: lib.area_of(t) is area)
    if params.get("project") is not None:
        project = lib.find_project(validate_ref(params["project"], "project"))
        if project is None or not view.visible(project):
            raise ValueError("no visible project matches 'project'")
        checks.append(lambda t: t is project or lib.project_of(t) is project)
    if params.get("tag") is not None:
        tag = lib.find_tag(validate_ref(params["tag"], "tag"))
        if tag is None or not view.tag_visible(tag):
            raise ValueError("no visible tag matches 'tag'")
        wanted = lib.tag_descendants(tag)
        checks.append(lambda t: any(tag_id in wanted for tag_id in t.tag_ids))
    return lambda t: all(check(t) for check in checks)


def action_list(params: dict, view: View) -> dict:
    """Items in one of Things' built-in lists, optionally narrowed by scope."""
    _reject_unknown(params, ("list", "days", "limit", "area", "project", "tag", "include_notes"))
    name = params.get("list")
    if name not in LISTS:
        raise ValueError(f"list must be one of {', '.join(LISTS)}")
    default_days = 7 if name == "logbook" else 30
    days = validate_int(params.get("days"), "days", default=default_days, minimum=1, maximum=MAX_WINDOW_DAYS)
    limit = validate_limit(params.get("limit"))
    include_notes = validate_bool(params.get("include_notes"), "include_notes", False)
    scope_filter = _filter_scope(view, params)

    horizon = view.today + timedelta(days=days)
    since = datetime.combine(view.today - timedelta(days=days - 1), datetime.min.time())
    matches = [
        task for task in view.items()
        if not task.is_template and _in_list(view, task, name, horizon, since) and scope_filter(task)
    ]
    matches.sort(key=_list_sort_key(view, name))
    result: dict[str, Any] = {"list": name, "count": len(matches), "truncated": len(matches) > limit}
    if name in ("upcoming", "logbook"):
        result["days"] = days
    result["items"] = [
        serialize_item(view, task, notes="snippet" if include_notes else "none")
        for task in matches[:limit]
    ]
    return result


def action_review(params: dict, view: View) -> dict:
    """Triage: overdue, due soon, Today (incl. This Evening), and the Inbox."""
    _reject_unknown(params, ("days_ahead", "limit"))
    days_ahead = validate_int(params.get("days_ahead"), "days_ahead", default=7, minimum=0, maximum=90)
    limit = validate_limit(params.get("limit"), default=50)
    today = view.today
    soon = today + timedelta(days=days_ahead)

    overdue: list[Task] = []
    due_soon: list[Task] = []
    today_items: list[Task] = []
    inbox: list[Task] = []
    open_count = 0
    for task in view.actionable():
        open_count += 1
        if task.deadline is not None and task.deadline < today:
            overdue.append(task)
        elif task.deadline is not None and task.deadline <= soon:
            due_soon.append(task)
        if view.lib.in_today(task):
            today_items.append(task)
        if task.type == TYPE_TODO and task.start == START_INBOX:
            inbox.append(task)

    overdue.sort(key=lambda t: (t.deadline, t.index))
    due_soon.sort(key=lambda t: (t.deadline, t.index))
    today_items.sort(key=_list_sort_key(view, "today"))
    inbox.sort(key=_list_sort_key(view, "inbox"))

    def render(tasks: list[Task]) -> list[dict]:
        return [serialize_item(view, task, notes="snippet") for task in tasks[:limit]]

    return {
        "date": today.isoformat(),
        "days_ahead": days_ahead,
        "counts": {
            "overdue": len(overdue),
            "due_soon": len(due_soon),
            "today": len(today_items),
            "evening": sum(1 for t in today_items if view.lib.when(t) == "evening"),
            "inbox": len(inbox),
            "open_total": open_count,
        },
        "overdue": render(overdue),
        "due_soon": render(due_soon),
        "today": render(today_items),
        "inbox": render(inbox),
    }


def action_search(params: dict, view: View) -> dict:
    """Case-insensitive substring search over titles, notes, and checklist items."""
    _reject_unknown(params, ("term", "include_completed", "limit", "area", "project", "tag"))
    term = validate_search(params.get("term"))
    include_completed = validate_bool(params.get("include_completed"), "include_completed", False)
    limit = validate_limit(params.get("limit"), default=50)
    scope_filter = _filter_scope(view, params)
    needle = term.casefold()

    hits: list[tuple[Task, list[str]]] = []
    for task in view.items():
        if task.is_template:
            continue
        if not include_completed and not view.lib.is_actionable(task):
            continue
        if not scope_filter(task):
            continue
        fields = []
        if needle in task.title.casefold():
            fields.append("title")
        if needle in task.notes.casefold():
            fields.append("notes")
        if task.type == TYPE_TODO and task.checklist_total and any(
            needle in item.title.casefold() for item in view.lib.checklist(task.uuid)
        ):
            fields.append("checklist")
        if fields:
            hits.append((task, fields))

    hits.sort(key=lambda hit: (hit[0].status != STATUS_OPEN, -(hit[0].modified or hit[0].created or datetime.min).timestamp()))
    items = []
    for task, fields in hits[:limit]:
        item = serialize_item(view, task, notes="snippet")
        item["matched_in"] = fields
        items.append(item)
    return {
        "term": term,
        "include_completed": include_completed,
        "match_count": len(hits),
        "truncated": len(hits) > limit,
        "items": items,
    }


def action_get(params: dict, view: View) -> dict:
    """Full detail for one to-do, project, or area."""
    _reject_unknown(params, ("id", "include_completed"))
    ref = validate_id(params.get("id"))
    include_completed = validate_bool(params.get("include_completed"), "include_completed", False)
    lib = view.lib

    area = lib.areas.get(ref)
    if area is not None:
        if not view.area_visible(area):
            raise ValueError(f"no visible item with id {ref!r}")
        projects = [
            task for task in view.items(types=(TYPE_PROJECT,))
            if task.area == area.uuid and (include_completed or lib.is_actionable(task))
        ]
        loose = [
            task for task in view.items(types=(TYPE_TODO,))
            if task.area == area.uuid and lib.project_of(task) is None and not task.is_template
            and (include_completed or lib.is_actionable(task))
        ]
        projects.sort(key=lambda t: t.index)
        loose.sort(key=lambda t: t.index)
        return {
            "item": {
                "id": area.uuid,
                "type": "area",
                "title": redact(area.title),
                "tags": [redact(lib.tags[t].title) for t in area.tag_ids if t in lib.tags],
                "url": f"things:///show?id={area.uuid}",
            },
            "projects": [serialize_item(view, task, notes="none") for task in projects],
            "todos": [serialize_item(view, task, notes="snippet") for task in loose],
        }

    task = view.require_item(ref)
    result: dict[str, Any] = {"item": serialize_item(view, task, notes="full")}
    if task.type == TYPE_TODO:
        result["item"]["checklist_items"] = [
            {"title": redact(item.title), "completed": item.status == STATUS_COMPLETED,
             "canceled": item.status == STATUS_CANCELED}
            for item in lib.checklist(task.uuid)
        ]
        return result

    headings = sorted(
        (h for h in lib.tasks.values() if h.type == TYPE_HEADING and h.project == task.uuid and not h.trashed),
        key=lambda h: h.index,
    )
    heading_order = {h.uuid: position for position, h in enumerate(headings)}
    children = [
        child for child in view.items(types=(TYPE_TODO,))
        if lib.project_of(child) is task and not child.is_template
        and (include_completed or child.status == STATUS_OPEN)
    ]
    children.sort(key=lambda c: (heading_order.get(c.heading or "", -1), c.status != STATUS_OPEN, c.index))
    result["headings"] = [{"id": h.uuid, "title": redact(h.title)} for h in headings]
    result["todos"] = [serialize_item(view, child, notes="snippet") for child in children]
    return result


def action_projects(params: dict, view: View) -> dict:
    _reject_unknown(params, ("area", "include_someday", "include_completed", "limit"))
    include_someday = validate_bool(params.get("include_someday"), "include_someday", True)
    include_completed = validate_bool(params.get("include_completed"), "include_completed", False)
    limit = validate_limit(params.get("limit"), default=MAX_LIMIT)
    scope_filter = _filter_scope(view, {"area": params.get("area")})
    lib = view.lib
    projects = []
    for task in view.items(types=(TYPE_PROJECT,)):
        if task.is_template or not scope_filter(task):
            continue
        if not include_completed and task.status != STATUS_OPEN:
            continue
        if not include_someday and task.status == STATUS_OPEN and lib.when(task) == "someday":
            continue
        projects.append(task)
    def project_order(task: Task) -> tuple:
        area = lib.area_of(task)
        return (task.status != STATUS_OPEN, area.index if area else -1, task.index)

    projects.sort(key=project_order)
    return {
        "count": len(projects),
        "truncated": len(projects) > limit,
        "projects": [serialize_item(view, task, notes="snippet") for task in projects[:limit]],
    }


def action_areas(params: dict, view: View) -> dict:
    _reject_unknown(params, ())
    lib = view.lib
    open_projects: Counter[str] = Counter()
    open_todos: Counter[str] = Counter()
    for task in view.actionable():
        area = lib.area_of(task)
        if area is None:
            continue
        if task.type == TYPE_PROJECT:
            open_projects[area.uuid] += 1
        else:
            open_todos[area.uuid] += 1
    areas = [area for area in sorted(lib.areas.values(), key=lambda a: a.index) if view.area_visible(area)]
    return {
        "count": len(areas),
        "areas": [
            {
                "id": area.uuid,
                "title": redact(area.title),
                "tags": [redact(lib.tags[t].title) for t in area.tag_ids if t in lib.tags],
                "open_projects": open_projects[area.uuid],
                "open_todos": open_todos[area.uuid],
                "url": f"things:///show?id={area.uuid}",
            }
            for area in areas
        ],
    }


def action_tags(params: dict, view: View) -> dict:
    _reject_unknown(params, ())
    lib = view.lib
    usage: Counter[str] = Counter()
    for task in view.actionable():
        for tag_id in task.tag_ids:
            usage[tag_id] += 1
    tags = []
    for tag in sorted(lib.tags.values(), key=lambda t: t.index):
        if not view.tag_visible(tag):
            continue
        if view.policy.mode == "allowlist" and not usage[tag.uuid]:
            continue
        parent = lib.tags.get(tag.parent) if tag.parent else None
        tags.append({
            "id": tag.uuid,
            "title": redact(tag.title),
            "parent": redact(parent.title) if parent else None,
            "shortcut": tag.shortcut or None,
            "open_items": usage[tag.uuid],
        })
    return {"count": len(tags), "tags": tags}


def action_completion_stats(params: dict, view: View) -> dict:
    """Throughput over a window: completions per day, by area, and lead time."""
    _reject_unknown(params, ("days",))
    days = validate_int(params.get("days"), "days", default=30, minimum=1, maximum=MAX_WINDOW_DAYS)
    lib = view.lib
    start_day = view.today - timedelta(days=days - 1)
    since = datetime.combine(start_day, datetime.min.time())

    completed: list[Task] = []
    canceled = 0
    created = 0
    for task in view.items(types=(TYPE_TODO,)):
        if task.is_template:
            continue
        if task.created and task.created >= since:
            created += 1
        if task.stop_date is None or task.stop_date < since:
            continue
        if task.status == STATUS_COMPLETED:
            completed.append(task)
        elif task.status == STATUS_CANCELED:
            canceled += 1

    per_day = Counter(task.stop_date.date().isoformat() for task in completed)  # type: ignore[union-attr]
    by_area = Counter((lib.area_of(task).title if lib.area_of(task) else "(no area)") for task in completed)
    by_project = Counter(
        lib.project_of(task).title for task in completed if lib.project_of(task) is not None  # type: ignore[union-attr]
    )
    lead_days = [
        (task.stop_date - task.created).total_seconds() / 86400  # type: ignore[operator]
        for task in completed
        if task.created is not None and task.stop_date is not None and task.stop_date >= task.created
    ]
    series = [
        {"date": (start_day + timedelta(days=offset)).isoformat(),
         "completed": per_day.get((start_day + timedelta(days=offset)).isoformat(), 0)}
        for offset in range(days)
    ]
    return {
        "days": days,
        "from": start_day.isoformat(),
        "to": view.today.isoformat(),
        "completed": len(completed),
        "canceled": canceled,
        "created": created,
        "net_change": created - len(completed) - canceled,
        "average_per_day": round(len(completed) / days, 2),
        "lead_time_days": {
            "median": round(statistics.median(lead_days), 1) if lead_days else None,
            "mean": round(statistics.fmean(lead_days), 1) if lead_days else None,
            "sample_size": len(lead_days),
        },
        "by_area": [{"title": redact(title), "completed": n} for title, n in by_area.most_common()],
        "top_projects": [{"title": redact(title), "completed": n} for title, n in by_project.most_common(10)],
        "series": series,
    }


def action_status(params: dict, view: View | None) -> dict:
    """Versions, policy, and installation checks. Reads no Things content."""
    policy = load_privacy_policy()
    db_path = find_things_db()
    container_listable = False
    try:
        os.listdir(THINGS_GROUP_CONTAINER)
        container_listable = True
    except OSError:
        pass
    return {
        "helper_version": HELPER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "host_display_name": HOST_DISPLAY_NAME,
        "launchd_label": LAUNCHD_LABEL,
        "allowed_actions": sorted(ACTIONS),
        "code_root": str(CODE_ROOT),
        "bridge_root": str(BRIDGE_ROOT),
        "python_version": sys.version.split()[0],
        "things_app": things_app_info(),
        "read_policy": {
            "mode": policy.mode,
            "allowlist_entries": len(policy.allowlist),
            "blocklist_entries": len(policy.blocklist),
            "root_owned_required": os.environ.get("THINGS_REQUIRE_ROOT_POLICY") == "1",
        },
        "write_policy": load_write_policy(),
        "confirm_nonce_ttl_seconds": CONFIRM_NONCE_TTL,
        "checks": {
            "things_container_accessible": container_listable,
            "things_db_found": db_path is not None,
            "things_db_readable": _probe_db_readable(db_path),
            "confirmation_helper_executable": CONFIRM_HELPER_PATH.is_file()
            and os.access(CONFIRM_HELPER_PATH, os.X_OK),
            "requests_dir_exists": REQUESTS_DIR.is_dir(),
            "responses_dir_exists": RESPONSES_DIR.is_dir(),
        },
    }


action_status.needs_db = False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Changes: normalization, policy, description
# ---------------------------------------------------------------------------
OPERATIONS = ("add_todo", "add_project", "update", "complete", "cancel", "reopen", "move")
_OPERATION_FIELDS = {
    "add_todo": ("title", "notes", "when", "deadline", "tags", "project", "area"),
    "add_project": ("title", "notes", "when", "deadline", "tags", "area"),
    "update": ("id", "title", "notes", "append_notes", "when", "deadline", "tags"),
    "complete": ("id",),
    "cancel": ("id",),
    "reopen": ("id",),
    "move": ("id", "project", "area"),
}
_CHANGE_ENVELOPE = ("operation", "confirm_nonce")


def _require_writable_scope(view: View, scope: Scope, what: str) -> None:
    if not is_allowed(scope, view.policy):
        raise ValueError(f"{what} is outside the read policy; changes there are not permitted")


def _destination_project(view: View, ref: Any) -> Task:
    project = view.lib.tasks.get(validate_id(ref, "project"))
    if (
        project is None or project.type != TYPE_PROJECT or not view.lib.is_live(project)
        or not view.visible(project)
    ):
        raise ValueError(f"no visible project with id {ref!r}")
    if project.status != STATUS_OPEN:
        raise ValueError("the destination project is not open")
    return project


def _destination_area(view: View, ref: Any) -> Area:
    area = view.lib.areas.get(validate_id(ref, "area"))
    if area is None or not view.area_visible(area):
        raise ValueError(f"no visible area with id {ref!r}")
    _require_writable_scope(view, view.lib.area_scope(area), "that area")
    return area


def _new_item_scope(lib: Library, *, project: Task | None, area: Area | None, tags: list[Tag]) -> Scope:
    """The scope an item will have once created, for the write policy check."""
    effective_area = area or (lib.area_of(project) if project is not None else None)
    tag_ids = [tag.uuid for tag in tags]
    if project is not None:
        tag_ids += project.tag_ids
    if effective_area is not None:
        tag_ids += effective_area.tag_ids
    return Scope(
        areas=((effective_area.uuid, effective_area.title),) if effective_area else (),
        projects=((project.uuid, project.title),) if project else (),
        tags=tuple((tag.uuid, tag.title) for tag in lib.tag_with_ancestors(tag_ids)),
        unfiled=project is None and area is None,
    )


def normalize_change(params: dict, view: View) -> dict:
    """Validate a change request and return its canonical form.

    The canonical form is what the nonce binds and what AppleScript applies.
    Only fields the client supplied appear (``deadline: null`` is preserved
    as "clear the deadline").
    """
    operation = params.get("operation")
    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {', '.join(OPERATIONS)}")
    fields = _OPERATION_FIELDS[operation]
    _reject_unknown(params, fields + _CHANGE_ENVELOPE)
    lib = view.lib
    change: dict[str, Any] = {"operation": operation}

    if operation in ("add_todo", "add_project"):
        change["title"] = validate_title(params.get("title"))
        if params.get("notes") is not None:
            notes = validate_notes(params["notes"])
            if notes:
                change["notes"] = notes
        if params.get("when") is not None:
            change["when"] = validate_when(params["when"], view.today, allow_inbox=operation == "add_todo")
        if params.get("deadline") is not None:
            change["deadline"] = validate_deadline(params["deadline"])
        tags: list[Tag] = []
        if params.get("tags") is not None:
            tags = resolve_tags(params["tags"], lib)
            if tags:
                change["tags"] = [tag.title for tag in tags]
        project: Task | None = None
        area: Area | None = None
        if params.get("project") is not None:
            project = _destination_project(view, params["project"])
            change["project"] = project.uuid
        if params.get("area") is not None:
            area = _destination_area(view, params["area"])
            change["area"] = area.uuid
        if project is not None and area is not None:
            raise ValueError("specify project or area, not both")
        if project is not None and change.get("when") == "inbox":
            raise ValueError("a to-do in a project cannot be scheduled to the Inbox")
        new_scope = _new_item_scope(lib, project=project, area=area, tags=tags)
        _require_writable_scope(view, new_scope, "the new item")
        return change

    target = view.require_item(validate_id(params.get("id")))
    change["id"] = target.uuid

    if operation == "update":
        if params.get("title") is not None:
            change["title"] = validate_title(params["title"])
        if params.get("notes") is not None and params.get("append_notes") is not None:
            raise ValueError("specify notes or append_notes, not both")
        if params.get("notes") is not None:
            change["notes"] = validate_notes(params["notes"])
        if params.get("append_notes") is not None:
            appended = validate_notes(params["append_notes"], "append_notes")
            if not appended.strip():
                raise ValueError("append_notes cannot be empty")
            if len(target.notes) + 1 + len(appended) > MAX_NOTES_LEN:
                raise ValueError(f"notes would exceed {MAX_NOTES_LEN} characters")
            change["append_notes"] = appended
        if params.get("when") is not None:
            change["when"] = validate_when(params["when"], view.today, allow_inbox=target.type == TYPE_TODO)
            if change["when"] == "inbox" and lib.project_of(target) is not None:
                raise ValueError("a to-do in a project cannot be moved to the Inbox")
        if "deadline" in params:
            change["deadline"] = None if params["deadline"] is None else validate_deadline(params["deadline"])
        if params.get("tags") is not None:
            change["tags"] = [tag.title for tag in resolve_tags(params["tags"], lib)]
        if len(change) == 2:
            raise ValueError("update requires at least one field to change")
        if target.status != STATUS_OPEN:
            raise ValueError("only open items can be updated; reopen it first")
        return change

    if operation == "complete" or operation == "cancel":
        if target.status != STATUS_OPEN:
            raise ValueError(f"item is already {_STATUS_NAMES.get(target.status, 'closed')}")
        return change

    if operation == "reopen":
        if target.status == STATUS_OPEN:
            raise ValueError("item is already open")
        return change

    # move
    has_project = params.get("project") is not None
    has_area = params.get("area") is not None
    if has_project == has_area:
        raise ValueError("move requires exactly one of project or area")
    if target.status != STATUS_OPEN:
        raise ValueError("only open items can be moved")
    if has_project:
        if target.type != TYPE_TODO:
            raise ValueError("only to-dos can be moved into a project")
        project = _destination_project(view, params["project"])
        if lib.project_of(target) is project:
            raise ValueError("the to-do is already in that project")
        change["project"] = project.uuid
    else:
        area = _destination_area(view, params["area"])
        if lib.area_of(target) is area and lib.project_of(target) is None:
            raise ValueError("the item is already in that area")
        if target.type == TYPE_TODO and lib.project_of(target) is not None:
            raise ValueError(
                "moving a to-do out of a project is not supported by the Things AppleScript "
                "interface; move it in Things"
            )
        change["area"] = area.uuid
    return change


_OPERATION_HEADLINES = {
    "add_todo": "add a to-do",
    "add_project": "add a project",
    "update": "update {kind}",
    "complete": "complete {kind}",
    "cancel": "cancel {kind}",
    "reopen": "reopen {kind}",
    "move": "move {kind}",
}


def _describe_when(value: str) -> str:
    labels = {"inbox": "Inbox", "today": "Today", "anytime": "Anytime", "someday": "Someday"}
    return labels.get(value, value)


def describe_change(change: dict, view: View) -> dict:
    """Human-readable summary for the chat preview and the native dialog.

    Every value shown is unredacted on purpose: the human approving a change
    must see exactly what will be written.
    """
    lib = view.lib
    operation = change["operation"]
    target = lib.tasks.get(change.get("id", ""))
    kind = "a project" if target is not None and target.type == TYPE_PROJECT else "a to-do"
    headline = f"{HOST_DISPLAY_NAME} wants to " + _OPERATION_HEADLINES[operation].format(kind=kind)
    rows: list[list[str]] = []
    details: list[str] = []

    if target is not None:
        rows.append(["Item", target.title])
        project = lib.project_of(target)
        if project is not None:
            rows.append(["In project", project.title])
        area = lib.area_of(target)
        if area is not None:
            rows.append(["In area", area.title])
    if "title" in change:
        rows.append(["New title" if target is not None else "Title", change["title"]])
    if "project" in change:
        rows.append(["Project" if target is None else "Move to project", lib.tasks[change["project"]].title])
    if "area" in change:
        rows.append(["Area" if target is None else "Move to area", lib.areas[change["area"]].title])
    if "when" in change:
        rows.append(["When", _describe_when(change["when"])])
    if "deadline" in change:
        rows.append(["Deadline", change["deadline"] or "(remove deadline)"])
    if "tags" in change:
        rows.append(["Tags" if target is None else "Replace tags with", ", ".join(change["tags"]) or "(none)"])
    if "notes" in change:
        rows.append(["Notes", "replace" if target is not None else f"{len(change['notes'])} characters"])
        details.append(change["notes"] if change["notes"] else "(notes will be cleared)")
    if "append_notes" in change:
        rows.append(["Notes", "append"])
        details.append(change["append_notes"])
    return {"headline": headline, "rows": rows, "details": "\n\n".join(details)}


# ---------------------------------------------------------------------------
# AppleScript
# ---------------------------------------------------------------------------
def as_string(value: str) -> str:
    """Quote a validated Python string as an AppleScript string literal.

    Validation has already removed control characters other than newline and
    tab, so escaping backslash and double quote is sufficient. The script is
    fed to osascript on stdin, never on the command line.
    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _as_date_lines(var: str, iso: str) -> list[str]:
    """Build a date without locale-dependent parsing (day reset first to avoid overflow)."""
    day = date.fromisoformat(iso)
    return [
        f"set {var} to current date",
        f"set day of {var} to 1",
        f"set year of {var} to {day.year}",
        f"set month of {var} to {day.month}",
        f"set day of {var} to {day.day}",
        f"set time of {var} to 0",
    ]


def _as_item_ref(target: Task) -> str:
    kind = "project" if target.type == TYPE_PROJECT else "to do"
    return f"{kind} id {as_string(target.uuid)}"


def _as_schedule_lines(var: str, when: str) -> list[str]:
    if when in LIST_SOURCE_IDS:
        return [f"move {var} to list id {as_string(LIST_SOURCE_IDS[when])}"]
    return [f"schedule {var} for whenDate"]


def build_change_script(change: dict, lib: Library) -> str:
    """Translate a normalized change into a short, static-shape AppleScript.

    Every interpolated value is either a validated string quoted by as_string,
    an integer, or a constant chosen from a fixed table.
    """
    operation = change["operation"]
    prelude: list[str] = []
    body: list[str] = []
    if change.get("deadline"):
        prelude += _as_date_lines("deadlineDate", change["deadline"])
    when = change.get("when")
    if when is not None and when not in LIST_SOURCE_IDS:
        prelude += _as_date_lines("whenDate", when)

    if operation in ("add_todo", "add_project"):
        kind = "to do" if operation == "add_todo" else "project"
        properties = [f"name:{as_string(change['title'])}"]
        if change.get("notes"):
            properties.append(f"notes:{as_string(change['notes'])}")
        body.append(f"set theItem to make new {kind} with properties {{{', '.join(properties)}}}")
        if change.get("project"):
            body.append(f"set project of theItem to project id {as_string(change['project'])}")
        if change.get("area"):
            body.append(f"set area of theItem to area id {as_string(change['area'])}")
        if change.get("tags"):
            body.append(f"set tag names of theItem to {as_string(', '.join(change['tags']))}")
        if change.get("deadline"):
            body.append("set due date of theItem to deadlineDate")
        if when is not None:
            body += _as_schedule_lines("theItem", when)
        body.append("return id of theItem")
    else:
        target = lib.tasks[change["id"]]
        body.append(f"set theItem to {_as_item_ref(target)}")
        if operation == "update":
            if "title" in change:
                body.append(f"set name of theItem to {as_string(change['title'])}")
            if "notes" in change:
                body.append(f"set notes of theItem to {as_string(change['notes'])}")
            if "append_notes" in change:
                body += [
                    "set oldNotes to notes of theItem",
                    'if oldNotes is missing value then set oldNotes to ""',
                    'if oldNotes is "" then',
                    f"    set notes of theItem to {as_string(change['append_notes'])}",
                    "else",
                    f"    set notes of theItem to oldNotes & linefeed & {as_string(change['append_notes'])}",
                    "end if",
                ]
            if "deadline" in change:
                if change["deadline"]:
                    body.append("set due date of theItem to deadlineDate")
                else:
                    body += [
                        "try",
                        "    set due date of theItem to missing value",
                        "on error",
                        "    delete due date of theItem",
                        "end try",
                    ]
            if "tags" in change:
                body.append(f"set tag names of theItem to {as_string(', '.join(change['tags']))}")
            if when is not None:
                body += _as_schedule_lines("theItem", when)
        elif operation == "complete":
            body.append("set status of theItem to completed")
        elif operation == "cancel":
            body.append("set status of theItem to canceled")
        elif operation == "reopen":
            body.append("set status of theItem to open")
        elif operation == "move":
            if change.get("project"):
                body.append(f"set project of theItem to project id {as_string(change['project'])}")
            else:
                body.append(f"set area of theItem to area id {as_string(change['area'])}")
        body.append("return id of theItem")

    lines = prelude + [f"tell application id {as_string(THINGS_BUNDLE_ID)}"]
    lines += [f"    {line}" for line in body]
    lines.append("end tell")
    return "\n".join(lines) + "\n"


def _run_osascript(script: str, timeout: float = OSASCRIPT_TIMEOUT_S) -> tuple[int, str, str]:
    """Run ``script`` via osascript (fed on stdin). Separated for tests."""
    result = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()


def _run_confirmation(description: dict) -> bool:
    """Show the change in the native confirmation helper.

    Returns True only for the helper's explicit Apply exit status. Cancel and
    timeout return False; malformed input or helper failures raise.
    """
    payload = json.dumps(
        {
            "client_name": HOST_DISPLAY_NAME,
            "headline": description["headline"],
            "rows": description["rows"],
            "details": description["details"],
        },
        ensure_ascii=False,
    )
    try:
        result = subprocess.run(
            [str(CONFIRM_HELPER_PATH)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=CONFIRM_DIALOG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError as exc:
        raise RuntimeError(f"confirmation helper could not start: {exc}") from exc
    if result.returncode == 0:
        return True
    if result.returncode in (1, 3):
        return False
    detail = (result.stderr or result.stdout or "no output").strip()
    raise RuntimeError(f"confirmation helper failed (rc={result.returncode}): {detail}")


def _require_writes_enabled() -> str:
    policy = load_write_policy()
    if policy == "disabled":
        raise ValueError("changes are disabled by policy/write_policy.txt")
    return policy


def action_change_preview(params: dict, view: View) -> dict:
    """Validate a change and mint a single-use confirm nonce. Changes nothing."""
    policy = _require_writes_enabled()
    if "confirm_nonce" in params:
        raise ValueError("change_preview does not take confirm_nonce")
    change = normalize_change(params, view)
    return {
        "change": change,
        "summary": describe_change(change, view),
        "dialog_required": policy == "confirm",
        "confirm_nonce": mint_confirm_nonce(change),
        "confirm_nonce_ttl_seconds": CONFIRM_NONCE_TTL,
    }


def action_change(params: dict, view: View) -> dict:
    """Apply a previewed change, after native-dialog approval unless policy is ``preview``."""
    policy = _require_writes_enabled()
    change = normalize_change(params, view)
    consume_confirm_nonce(params.get("confirm_nonce"), change)
    if policy == "confirm" and not _run_confirmation(describe_change(change, view)):
        raise RuntimeError("change cancelled by user or timed out (60s dialog limit)")
    script = build_change_script(change, view.lib)
    rc, stdout, stderr = _run_osascript(script)
    if rc != 0:
        raise RuntimeError(f"Things AppleScript failed (rc={rc}): {stderr or stdout or 'no output'}")
    item_id = stdout if _ID_RE.fullmatch(stdout or "") else change.get("id")
    return {
        "applied": {
            "operation": change["operation"],
            "id": item_id,
            "url": f"things:///show?id={item_id}" if item_id else None,
            "applied_at": datetime.now().isoformat(timespec="seconds"),
            "approval": "dialog" if policy == "confirm" else "preview",
        }
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
ACTIONS: dict[str, Callable[..., dict]] = {
    "status": action_status,
    "review": action_review,
    "list": action_list,
    "search": action_search,
    "get": action_get,
    "projects": action_projects,
    "areas": action_areas,
    "tags": action_tags,
    "completion_stats": action_completion_stats,
    "change_preview": action_change_preview,
    "change": action_change,
}


def write_response(req_filename_stem: str, data: dict) -> None:
    """Write response JSON atomically, named from the request filename only."""
    if not req_filename_stem or "/" in req_filename_stem or req_filename_stem in (".", ".."):
        raise ValueError("invalid response filename stem")
    name = f"response-{req_filename_stem}.json"
    tmp = f".{name}.{uuid.uuid4().hex}.tmp"
    with _private_directory_fd(RESPONSES_DIR, create=True) as responses_fd:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_NOFOLLOW_FLAGS,
            0o600,
            dir_fd=responses_fd,
        )
        try:
            _validate_regular_file(fd, tmp, private=True)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, name, src_dir_fd=responses_fd, dst_dir_fd=responses_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp, dir_fd=responses_fd)
            except FileNotFoundError:
                pass


def reap_expired_responses() -> None:
    """Remove responses the host failed to consume within RESPONSE_TTL_S."""
    now = time.time()
    try:
        with _private_directory_fd(RESPONSES_DIR) as responses_fd:
            for name in os.listdir(responses_fd):
                if not (name.startswith("response-") and name.endswith(".json")):
                    continue
                try:
                    metadata = _stat_regular_at(responses_fd, name)
                    if now - metadata.st_mtime > RESPONSE_TTL_S:
                        os.unlink(name, dir_fd=responses_fd)
                except (OSError, UnsafeRuntimePath) as exc:
                    log(f"response reaper could not remove {name}: {exc}")
    except UnsafeRuntimePath as exc:
        if isinstance(exc.__cause__, FileNotFoundError):
            return
        raise


def _read_request_text(name: str, requests_fd: int) -> str:
    fd = os.open(name, os.O_RDONLY | _FILE_NOFOLLOW_FLAGS, dir_fd=requests_fd)
    try:
        metadata = _validate_regular_file(fd, name)
        if metadata.st_size > MAX_REQUEST_BYTES:
            raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} byte limit")
        chunks: list[bytes] = []
        remaining = MAX_REQUEST_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} byte limit")
        return raw.decode("utf-8")
    finally:
        os.close(fd)


def _error_response(req_stem: str, error: str, *, req_id: str | None = None, action: str | None = None) -> None:
    response: dict[str, Any] = {"ok": False, "error": error}
    if req_id is not None:
        response["id"] = req_id
    if action is not None:
        response["action"] = action
    write_response(req_stem, response)


def request_stem(name: str) -> str | None:
    """``request-<stem>.json`` -> ``<stem>``; None for anything else."""
    if not (name.startswith("request-") and name.endswith(".json")):
        return None
    stem = name[len("request-"):-len(".json")]
    if not stem or "/" in stem or stem.startswith("."):
        return None
    return stem


def process_request(name: str, requests_fd: int, *, today: date | None = None) -> None:
    req_stem = request_stem(name)
    if req_stem is None:
        return
    try:
        raw = _read_request_text(name, requests_fd)
    except Exception as exc:
        _error_response(req_stem, f"bad request file: {exc}")
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Tolerate a non-atomic client once; secure opens still reject symlinks.
        time.sleep(0.1)
        try:
            data = json.loads(_read_request_text(name, requests_fd))
        except Exception as exc:
            _error_response(req_stem, f"bad request JSON: {exc}")
            return

    if not isinstance(data, dict):
        _error_response(req_stem, "bad request: JSON root must be an object")
        return
    # The JSON id is echoed back but never used to build a filesystem path.
    req_id = str(data.get("id", req_stem))[:128]
    action = data.get("action")
    params = data.get("params", {})
    if not isinstance(action, str):
        _error_response(req_stem, "bad request: action must be a string", req_id=req_id)
        return
    if not isinstance(params, dict):
        _error_response(req_stem, "bad request: params must be an object", req_id=req_id, action=action)
        return
    if action not in ACTIONS:
        write_response(req_stem, {
            "id": req_id,
            "ok": False,
            "error": f"unknown action: {action!r}",
            "allowed_actions": sorted(ACTIONS),
        })
        return

    conn = None
    started = time.monotonic()
    try:
        handler = ACTIONS[action]
        if getattr(handler, "needs_db", True):
            conn = snapshot_things_db()
            view: View | None = View(Library(conn, today=today), load_privacy_policy())
        else:
            view = None
        result = handler(params, view)
        result.update({
            "id": req_id,
            "ok": True,
            "action": action,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        })
        write_response(req_stem, result)
        log(f"action={action} id={req_stem} ok in {time.monotonic() - started:.2f}s")
    except (ValueError, WriteGateError) as exc:
        log(f"action={action} id={req_stem} rejected: {type(exc).__name__}")
        _error_response(req_stem, str(exc), req_id=req_id, action=action)
    except Exception as exc:
        log(f"action={action} id={req_stem} error: {exc!r}")
        log(traceback.format_exc())
        _error_response(req_stem, str(exc), req_id=req_id, action=action)
    finally:
        if conn is not None:
            conn.close()


def _acquire_bridge_lock(control_fd: int, timeout_s: float = WORKER_LOCK_TIMEOUT_S) -> int:
    """Take the per-bridge worker lock, waiting up to ``timeout_s``.

    Returns the lock descriptor; closing it releases the lock.
    """
    try:
        lock_fd = os.open("lock", os.O_CREAT | os.O_RDWR | _FILE_NOFOLLOW_FLAGS, 0o600, dir_fd=control_fd)
    except OSError as exc:
        raise RuntimeError(f"could not open bridge lock file: {exc}") from exc
    try:
        _validate_regular_file(lock_fd, "control/lock", private=True)
    except Exception as exc:
        os.close(lock_fd)
        raise RuntimeError(f"unsafe bridge lock file: {exc}") from exc

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(lock_fd)
                raise RuntimeError(
                    f"could not acquire bridge lock within {timeout_s:.0f}s; "
                    "another worker is processing this bridge"
                ) from None
            time.sleep(0.05)


def _pending_requests(requests_fd: int) -> list[str]:
    return sorted(name for name in os.listdir(requests_fd) if request_stem(name) is not None)


def main() -> None:
    for path in (LOG_PATH.parent, REQUESTS_DIR, RESPONSES_DIR):
        with _private_directory_fd(path, create=True):
            pass
    reap_expired_responses()
    try:
        reap_expired_nonces()
    except Exception as exc:
        log(f"nonce reaper error: {exc!r}")

    try:
        with _private_directory_fd(LOG_PATH.parent) as control_fd:
            lock_fd = _acquire_bridge_lock(control_fd)
    except RuntimeError as exc:
        log(f"deferring drain: {exc}")
        return

    try:
        with _private_directory_fd(REQUESTS_DIR) as requests_fd:
            # Requests can arrive while a confirmation dialog is open and
            # launchd coalesces events, so keep draining until the queue is empty.
            for _ in range(MAX_DRAIN_PASSES):
                pending = _pending_requests(requests_fd)
                if not pending:
                    break
                for name in pending:
                    try:
                        process_request(name, requests_fd)
                    except Exception as exc:
                        log(f"request={name} unhandled error: {exc!r}")
                        stem = request_stem(name)
                        if stem is not None:
                            try:
                                _error_response(stem, f"request processing failed: {exc}")
                            except Exception as response_error:
                                log(f"request={name} could not write error response: {response_error!r}")
                    finally:
                        try:
                            os.unlink(name, dir_fd=requests_fd)
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            log(f"could not unlink {name}: {exc}")
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
