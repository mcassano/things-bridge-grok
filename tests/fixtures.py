"""Synthetic Things 3 databases and isolated bridges for tests.

The schema mirrors the columns of a real Things 3.2x database that the helper
reads. No real user data is involved.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import pathlib
import shutil
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator
from unittest import mock

from tests._helper_loader import helper

TODAY = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 12, 0, 0)

SCHEMA = """
CREATE TABLE TMTask (
    "uuid" TEXT PRIMARY KEY, "leavesTombstone" INTEGER, "creationDate" REAL,
    "userModificationDate" REAL, "type" INTEGER, "status" INTEGER, "stopDate" REAL,
    "trashed" INTEGER, "title" TEXT, "notes" TEXT, "notesSync" INTEGER,
    "cachedTags" BLOB, "start" INTEGER, "startDate" INTEGER, "startBucket" INTEGER,
    "reminderTime" INTEGER, "lastReminderInteractionDate" REAL, "deadline" INTEGER,
    "deadlineSuppressionDate" INTEGER, "t2_deadlineOffset" INTEGER, "index" INTEGER,
    "todayIndex" INTEGER, "todayIndexReferenceDate" INTEGER, "area" TEXT,
    "project" TEXT, "heading" TEXT, "contact" TEXT,
    "untrashedLeafActionsCount" INTEGER, "openUntrashedLeafActionsCount" INTEGER,
    "checklistItemsCount" INTEGER, "openChecklistItemsCount" INTEGER,
    "rt1_repeatingTemplate" TEXT, "rt1_recurrenceRule" BLOB,
    "rt1_instanceCreationStartDate" INTEGER, "rt1_instanceCreationPaused" INTEGER,
    "rt1_instanceCreationCount" INTEGER, "rt1_afterCompletionReferenceDate" INTEGER,
    "rt1_nextInstanceStartDate" INTEGER, "experimental" BLOB, "repeater" BLOB,
    "repeaterMigrationDate" REAL
);
CREATE TABLE TMArea ("uuid" TEXT PRIMARY KEY, "title" TEXT, "visible" INTEGER,
    "index" INTEGER, "cachedTags" BLOB, experimental BLOB);
CREATE TABLE TMTag ("uuid" TEXT PRIMARY KEY, "title" TEXT, "shortcut" TEXT,
    "usedDate" REAL, "parent" TEXT, "index" INTEGER, experimental BLOB);
CREATE TABLE TMTaskTag ("tasks" TEXT NOT NULL, "tags" TEXT NOT NULL);
CREATE TABLE TMAreaTag ("areas" TEXT NOT NULL, "tags" TEXT NOT NULL);
CREATE TABLE TMChecklistItem ("uuid" TEXT PRIMARY KEY, "userModificationDate" REAL,
    "creationDate" REAL, "title" TEXT, "status" INTEGER, "stopDate" REAL,
    "index" INTEGER, "task" TEXT, "leavesTombstone" INTEGER, experimental BLOB);
CREATE TABLE Meta ("key" TEXT PRIMARY KEY, "value" TEXT);
"""


def ts(value: datetime) -> float:
    return value.timestamp()


class ThingsFixture:
    """Builds a Things-shaped SQLite database row by row."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)
        self._ids = itertools.count(1)

    def _id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):04d}"

    def area(self, title: str, *, tags: Iterable[str] = (), index: int = 0) -> str:
        uid = self._id("AREA")
        self.conn.execute('INSERT INTO TMArea (uuid, title, visible, "index") VALUES (?, ?, 1, ?)',
                          (uid, title, index))
        for tag in tags:
            self.conn.execute("INSERT INTO TMAreaTag (areas, tags) VALUES (?, ?)", (uid, tag))
        return uid

    def tag(self, title: str, *, parent: str | None = None, index: int = 0) -> str:
        uid = self._id("TAG")
        self.conn.execute('INSERT INTO TMTag (uuid, title, parent, "index") VALUES (?, ?, ?, ?)',
                          (uid, title, parent, index))
        return uid

    def task(
        self,
        title: str,
        *,
        type: int = 0,
        status: int = 0,
        start: int = 1,
        start_date: date | None = None,
        evening: bool = False,
        deadline: date | None = None,
        area: str | None = None,
        project: str | None = None,
        heading: str | None = None,
        notes: str = "",
        trashed: bool = False,
        stopped: datetime | None = None,
        created: datetime | None = None,
        index: int = 0,
        today_index: int = 0,
        tags: Iterable[str] = (),
        checklist: Iterable[tuple[str, int]] = (),
        template: bool = False,
        instance_of: str | None = None,
    ) -> str:
        uid = self._id("TASK")
        checklist = list(checklist)
        created = created or NOW - timedelta(days=3)
        self.conn.execute(
            """INSERT INTO TMTask (uuid, creationDate, userModificationDate, type, status,
                   stopDate, trashed, title, notes, start, startDate, startBucket,
                   deadline, "index", todayIndex, area, project, heading,
                   checklistItemsCount, openChecklistItemsCount,
                   rt1_recurrenceRule, rt1_repeatingTemplate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uid, ts(created), ts(created), type, status,
                ts(stopped) if stopped else None, int(trashed), title, notes, start,
                helper.encode_things_date(start_date) if start_date else None,
                1 if evening else 0,
                helper.encode_things_date(deadline) if deadline else None,
                index, today_index, area, project, heading,
                len(checklist), sum(1 for _, s in checklist if s == 0),
                b"rule" if template else None, instance_of,
            ),
        )
        for position, (item_title, item_status) in enumerate(checklist):
            self.conn.execute(
                'INSERT INTO TMChecklistItem (uuid, title, status, "index", task) VALUES (?, ?, ?, ?, ?)',
                (self._id("CHK"), item_title, item_status, position, uid),
            )
        for tag in tags:
            self.conn.execute("INSERT INTO TMTaskTag (tasks, tags) VALUES (?, ?)", (uid, tag))
        return uid

    def project(self, title: str, **kwargs) -> str:
        return self.task(title, type=1, **kwargs)

    def heading(self, title: str, project: str, *, index: int = 0) -> str:
        return self.task(title, type=2, project=project, index=index)

    def commit(self) -> None:
        self.conn.commit()


def realpath_tempdir(prefix: str) -> pathlib.Path:
    # macOS temp dirs live under /var, a symlink to /private/var; the helper
    # opens bridge components with O_NOFOLLOW, so tests must use real paths.
    return pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix=prefix)))


class BridgeTestCase(unittest.TestCase):
    """Isolated bridge, policy directory, and Things database per test."""

    def setUp(self) -> None:
        self.root = realpath_tempdir("things-bridge-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bridge = self.root / "bridge"
        self.bridge.mkdir(mode=0o700)
        self.policy_dir = self.bridge / "policy"
        self.policy_dir.mkdir(mode=0o700)
        self.db_path = self.root / "main.sqlite"
        self.things = ThingsFixture(self.db_path)

        patches = {
            "BRIDGE_ROOT": self.bridge,
            "POLICY_ROOT": self.policy_dir,
            "REQUESTS_DIR": self.bridge / "control" / "requests",
            "RESPONSES_DIR": self.bridge / "control" / "responses",
            "LOG_PATH": self.bridge / "control" / "log.txt",
            "READ_POLICY_PATH": self.policy_dir / "read_policy.txt",
            "WRITE_POLICY_PATH": self.policy_dir / "write_policy.txt",
            "BLOCKLIST_PATH": self.policy_dir / "blocked_scopes.txt",
            "ALLOWLIST_PATH": self.policy_dir / "allowed_scopes.txt",
        }
        for name, value in patches.items():
            patcher = mock.patch.object(helper, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {
            "THINGS_BRIDGE_DIR": str(self.bridge),
            "THINGS_DB_PATH": str(self.db_path),
        })
        env.start()
        self.addCleanup(env.stop)
        for name in ("THINGS_READ_POLICY", "THINGS_REQUIRE_ROOT_POLICY"):
            os.environ.pop(name, None)

    def write_policy(self, name: str, content: str, mode: int = 0o600) -> pathlib.Path:
        path = self.policy_dir / name
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def view(self) -> "helper.View":
        self.things.commit()
        conn = sqlite3.connect(str(self.db_path))
        self.addCleanup(conn.close)
        return helper.View(helper.Library(conn, today=TODAY), helper.load_privacy_policy())

    @contextlib.contextmanager
    def frozen_today(self) -> Iterator[None]:
        real_library = helper.Library

        def library(conn, *, today=None):
            return real_library(conn, today=today or TODAY)

        with mock.patch.object(helper, "Library", library):
            yield
