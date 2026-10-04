from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._helper_loader import REPO_ROOT, helper
from tests.fixtures import BridgeTestCase, realpath_tempdir


class BridgeRuntimeTests(BridgeTestCase):
    """End-to-end request processing through helper.main()."""

    def setUp(self) -> None:
        super().setUp()
        self.things.task("Secret title 4111 1111 1111 1111", start=0, notes="private notes")
        self.things.commit()
        self.requests = self.bridge / "control" / "requests"
        self.responses = self.bridge / "control" / "responses"
        helper.main()  # creates the private runtime directories

    def submit(self, stem: str, payload: object | bytes) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        tmp = self.requests / f".request-{stem}.json.tmp"
        tmp.write_bytes(raw)
        tmp.rename(self.requests / f"request-{stem}.json")

    def response(self, stem: str) -> dict:
        path = self.responses / f"response-{stem}.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        return json.loads(path.read_text())

    def test_round_trip_and_cleanup(self) -> None:
        self.submit("a1", {"id": "client-1", "action": "list", "params": {"list": "inbox"}})
        helper.main()
        result = self.response("a1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "client-1")
        self.assertEqual(result["items"][0]["title"], "Secret title [REDACTED-CARD]")
        self.assertEqual(list(self.requests.iterdir()), [])

    def test_status_reads_no_content(self) -> None:
        self.submit("s", {"id": "s", "action": "status", "params": {}})
        with mock.patch.object(helper, "snapshot_things_db", side_effect=AssertionError("no db")):
            helper.main()
        result = self.response("s")
        self.assertEqual(result["protocol_version"], helper.PROTOCOL_VERSION)
        self.assertEqual(result["write_policy"], "confirm")
        self.assertIn("things_db_readable", result["checks"])

    def test_malformed_requests_get_error_responses(self) -> None:
        cases = {
            "badjson": b"{not json",
            "array": [1, 2],
            "noaction": {"id": "x", "params": {}},
            "params": {"id": "x", "action": "list", "params": [1]},
            "unknown": {"id": "x", "action": "drop_tables", "params": {}},
            "badparam": {"id": "x", "action": "list", "params": {"list": "inbox", "evil": 1}},
            "large": b'{"id": "' + b"x" * (helper.MAX_REQUEST_BYTES + 10) + b'"}',
        }
        for stem, payload in cases.items():
            self.submit(stem, payload)
        helper.main()
        for stem in cases:
            with self.subTest(stem=stem):
                result = self.response(stem)
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
        self.assertIn("allowed_actions", self.response("unknown"))

    def test_json_id_is_never_used_as_a_path(self) -> None:
        self.submit("safe", {"id": "../../escape", "action": "status", "params": {}})
        helper.main()
        self.assertEqual(self.response("safe")["id"], "../../escape")
        self.assertFalse((self.bridge / "escape.json").exists())

    def test_symlinked_request_is_rejected(self) -> None:
        target = self.root / "outside.json"
        target.write_text(json.dumps({"id": "x", "action": "status"}))
        (self.requests / "request-link.json").symlink_to(target)
        helper.main()
        self.assertFalse(self.response("link")["ok"])
        self.assertTrue(target.exists())

    def test_logs_never_contain_content(self) -> None:
        self.submit("l", {"id": "l", "action": "search", "params": {"term": "Secret"}})
        helper.main()
        log = (self.bridge / "control" / "log.txt").read_text()
        self.assertIn("action=search", log)
        self.assertNotIn("Secret", log)
        self.assertNotIn("private notes", log)

    def test_unsafe_bridge_permissions_fail_closed(self) -> None:
        os.chmod(self.bridge, 0o755)
        self.addCleanup(os.chmod, self.bridge, 0o700)
        with self.assertRaises(helper.UnsafeRuntimePath):
            helper.main()

    def test_symlinked_runtime_directory_fails_closed(self) -> None:
        shutil.rmtree(self.responses)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        self.responses.symlink_to(elsewhere)
        with self.assertRaises(helper.UnsafeRuntimePath):
            helper.main()

    def test_stale_responses_are_reaped(self) -> None:
        stale = self.responses / "response-old.json"
        stale.write_text("{}")
        old = time.time() - helper.RESPONSE_TTL_S - 60
        os.utime(stale, (old, old))
        helper.main()
        self.assertFalse(stale.exists())

    def test_missing_database_is_a_clear_error(self) -> None:
        os.environ["THINGS_DB_PATH"] = str(self.root / "absent.sqlite")
        self.submit("m", {"id": "m", "action": "review", "params": {}})
        helper.main()
        self.assertIn("Full Disk Access", self.response("m")["error"])

    def test_snapshot_limit(self) -> None:
        with mock.patch.dict(os.environ, {"THINGS_SNAPSHOT_MAX_MB": "0"}):
            self.assertEqual(helper._get_snapshot_max_bytes(), helper.DEFAULT_SNAPSHOT_MAX_MB * 1024 * 1024)
        with mock.patch.object(helper, "_get_snapshot_max_bytes", return_value=10):
            with self.assertRaisesRegex(RuntimeError, "snapshot limit"):
                helper.snapshot_things_db()


class DatabaseDiscoveryTests(unittest.TestCase):
    def test_prefers_most_recent_thingsdata_directory(self) -> None:
        root = realpath_tempdir("things-container-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        paths = []
        for name in ("ThingsData-AAAAA", "ThingsData-BBBBB"):
            db = root / name / helper.THINGS_DB_RELATIVE
            db.parent.mkdir(parents=True)
            db.write_bytes(b"SQLite format 3\x00")
            paths.append(db)
        os.utime(paths[0], (time.time() - 1000, time.time() - 1000))
        env = {k: v for k, v in os.environ.items() if k != "THINGS_DB_PATH"}
        with mock.patch.object(helper, "THINGS_GROUP_CONTAINER", root), mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(helper.find_things_db(), paths[1])
            self.assertTrue(helper._probe_db_readable(paths[1]))


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("clang"), "requires macOS clang")
class WrapperTests(unittest.TestCase):
    """Build the FDA wrapper against a fake interpreter and observe what it executes."""

    def setUp(self) -> None:
        self.root = realpath_tempdir("things-wrapper-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.helper_py = self.root / "helper.py"
        self.gate_py = self.root / "write_gate.py"
        self.confirm = self.root / "confirm"
        self.capture = self.root / "capture.txt"
        self.python = self.root / "fake-python"
        for path in (self.helper_py, self.gate_py):
            path.write_text("# test\n")
            os.chmod(path, 0o500)
        self.confirm.write_text("#!/bin/sh\n")
        os.chmod(self.confirm, 0o700)
        self.python.write_text(f'#!/bin/sh\n{{ echo "ARGS:$*"; /usr/bin/env; }} > "{self.capture}"\n')
        os.chmod(self.python, 0o700)
        self.wrapper = self.root / "wrapper"
        subprocess.run(
            ["clang", "-Wall", "-Wextra", "-Werror", "-O2",
             f'-DHELPER_SCRIPT="{self.helper_py}"', f'-DWRITE_GATE_SCRIPT="{self.gate_py}"',
             f'-DCONFIRM_HELPER="{self.confirm}"', f'-DBRIDGE_ROOT="{self.root}"',
             f'-DPYTHON_INTERPRETER="{self.python}"',
             "-o", str(self.wrapper), str(REPO_ROOT / "bin" / "things_helper.c")],
            check=True, capture_output=True,
        )

    def run_wrapper(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.wrapper), *args], capture_output=True, text=True,
                              env=env if env is not None else {"PATH": "/usr/bin:/bin"})

    def test_executes_isolated_python_with_minimal_environment(self) -> None:
        result = self.run_wrapper(env={
            "PATH": "/tmp/evil:/usr/bin", "PYTHONPATH": "/tmp/evil", "PYTHONSTARTUP": "/tmp/evil/startup.py",
            "THINGS_DB_PATH": "/etc/passwd", "THINGS_SNAPSHOT_MAX_MB": "2048",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = self.capture.read_text()
        self.assertIn(f"ARGS:-I -B {self.helper_py}", captured)
        self.assertIn("PATH=/usr/bin:/bin\n", captured)
        self.assertIn(f"THINGS_BRIDGE_DIR={self.root}\n", captured)
        self.assertIn("THINGS_READ_POLICY=runtime\n", captured)
        self.assertIn("THINGS_SNAPSHOT_MAX_MB=2048\n", captured)
        for leaked in ("PYTHONPATH", "PYTHONSTARTUP", "THINGS_DB_PATH", "/tmp/evil"):
            self.assertNotIn(leaked, captured)

    def test_rejects_non_numeric_snapshot_limit(self) -> None:
        self.run_wrapper(env={"THINGS_SNAPSHOT_MAX_MB": "1; rm -rf /"})
        self.assertNotIn("THINGS_SNAPSHOT_MAX_MB", self.capture.read_text())

    def test_rejects_arguments(self) -> None:
        self.assertEqual(self.run_wrapper("--anything").returncode, 64)

    def test_rejects_writable_or_symlinked_code(self) -> None:
        os.chmod(self.gate_py, 0o522)
        self.assertEqual(self.run_wrapper().returncode, 5)
        os.chmod(self.gate_py, 0o500)
        self.helper_py.unlink()
        self.helper_py.symlink_to(self.gate_py)
        self.assertEqual(self.run_wrapper().returncode, 3)
        self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()
