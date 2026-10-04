from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests._helper_loader import helper
from tests.fixtures import TODAY, BridgeTestCase, realpath_tempdir


class NormalizeTests(BridgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        t = self.things
        self.errands = t.tag("Errands")
        t.tag("a,b")
        self.area = t.area("Home")
        self.project = t.project("Garden", area=self.area)
        self.done_project = t.project("Old", status=3)
        self.todo = t.task("Weed", project=self.project, notes="beds 1-3")
        self.loose = t.task("Loose")
        self.closed = t.task("Closed", status=3)

    def norm(self, **params) -> dict:
        return helper.normalize_change(params, self.view())

    def test_add_todo_normalizes(self) -> None:
        change = self.norm(
            operation="add_todo", title="  Buy seeds ", notes="tomatoes\r\nbasil",
            when="tomorrow", deadline="2026-10-31", tags=["errands"], project=self.project,
        )
        self.assertEqual(change, {
            "operation": "add_todo", "title": "Buy seeds", "notes": "tomatoes\nbasil",
            "when": "2026-10-06", "deadline": "2026-10-31", "tags": ["Errands"],
            "project": self.project,
        })

    def test_add_validation(self) -> None:
        cases = [
            ({"title": ""}, "title"),
            ({"title": "x", "tags": ["Missing"]}, "does not exist"),
            ({"title": "x", "tags": ["a,b"]}, "comma"),
            ({"title": "x", "project": self.project, "area": self.area}, "not both"),
            ({"title": "x", "project": self.done_project}, "not open"),
            ({"title": "x", "project": self.project, "when": "inbox"}, "Inbox"),
            ({"title": "x", "checklist": ["a"]}, "unknown parameter"),
            ({"title": "x", "when": "evening"}, "evening"),
        ]
        for extra, message in cases:
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, message):
                self.norm(operation="add_todo", **extra)
        with self.assertRaisesRegex(ValueError, "Inbox"):
            self.norm(operation="add_project", title="x", when="inbox")

    def test_update(self) -> None:
        change = self.norm(operation="update", id=self.todo, deadline=None, append_notes="bed 4", tags=[])
        self.assertEqual(change, {
            "operation": "update", "id": self.todo, "deadline": None,
            "append_notes": "bed 4", "tags": [],
        })
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.norm(operation="update", id=self.todo)
        with self.assertRaisesRegex(ValueError, "not both"):
            self.norm(operation="update", id=self.todo, notes="a", append_notes="b")
        with self.assertRaisesRegex(ValueError, "reopen"):
            self.norm(operation="update", id=self.closed, title="x")
        with self.assertRaisesRegex(ValueError, "Inbox"):
            self.norm(operation="update", id=self.todo, when="inbox")

    def test_status_operations(self) -> None:
        self.assertEqual(self.norm(operation="complete", id=self.loose), {"operation": "complete", "id": self.loose})
        with self.assertRaisesRegex(ValueError, "already completed"):
            self.norm(operation="cancel", id=self.closed)
        with self.assertRaisesRegex(ValueError, "already open"):
            self.norm(operation="reopen", id=self.loose)
        self.assertEqual(self.norm(operation="reopen", id=self.closed)["operation"], "reopen")

    def test_move(self) -> None:
        self.assertEqual(self.norm(operation="move", id=self.loose, project=self.project)["project"], self.project)
        self.assertEqual(self.norm(operation="move", id=self.loose, area=self.area)["area"], self.area)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.norm(operation="move", id=self.loose)
        with self.assertRaisesRegex(ValueError, "already in that project"):
            self.norm(operation="move", id=self.todo, project=self.project)
        with self.assertRaisesRegex(ValueError, "out of a project"):
            self.norm(operation="move", id=self.todo, area=self.area)

    def test_unknown_operation(self) -> None:
        with self.assertRaisesRegex(ValueError, "operation must be"):
            self.norm(operation="delete", id=self.loose)

    def test_description_shows_exact_values(self) -> None:
        view = self.view()
        change = helper.normalize_change({"operation": "update", "id": self.todo, "notes": "new body"}, view)
        summary = helper.describe_change(change, view)
        self.assertIn("update a to-do", summary["headline"])
        self.assertIn(["Item", "Weed"], summary["rows"])
        self.assertIn(["In project", "Garden"], summary["rows"])
        self.assertEqual(summary["details"], "new body")


class ScriptTests(BridgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.things.project("Garden")
        self.todo = self.things.task('Say "hi"')
        self.lib = self.view().lib

    def test_add_script(self) -> None:
        script = helper.build_change_script({
            "operation": "add_todo", "title": 'Fix "door" \\ hinge', "notes": "a\nb",
            "when": "2026-12-24", "deadline": "2026-12-31", "tags": ["Home", "Tools"],
            "project": self.project,
        }, self.lib)
        self.assertIn('tell application id "com.culturedcode.ThingsMac"', script)
        self.assertIn('make new to do with properties {name:"Fix \\"door\\" \\\\ hinge", notes:"a\nb"}', script)
        self.assertIn(f'set project of theItem to project id "{self.project}"', script)
        self.assertIn('set tag names of theItem to "Home, Tools"', script)
        self.assertIn("set year of deadlineDate to 2026", script)
        self.assertIn("set month of whenDate to 12", script)
        self.assertIn("set day of whenDate to 24", script)
        self.assertIn("schedule theItem for whenDate", script)
        self.assertTrue(script.rstrip().endswith("end tell"))

    def test_list_moves_use_stable_ids(self) -> None:
        for when, list_id in helper.LIST_SOURCE_IDS.items():
            with self.subTest(when=when):
                script = helper.build_change_script(
                    {"operation": "update", "id": self.todo, "when": when}, self.lib
                )
                self.assertIn(f'move theItem to list id "{list_id}"', script)
                self.assertNotIn("whenDate", script)

    def test_status_and_clear_deadline(self) -> None:
        for operation, word in (("complete", "completed"), ("cancel", "canceled"), ("reopen", "open")):
            script = helper.build_change_script({"operation": operation, "id": self.todo}, self.lib)
            self.assertIn(f"set status of theItem to {word}", script)
            self.assertIn(f'set theItem to to do id "{self.todo}"', script)
        script = helper.build_change_script({"operation": "update", "id": self.project, "deadline": None}, self.lib)
        self.assertIn(f'set theItem to project id "{self.project}"', script)
        self.assertIn("set due date of theItem to missing value", script)

    @unittest.skipUnless(
        sys.platform == "darwin" and shutil.which("osacompile")
        and Path("/Applications/Things3.app").is_dir(),
        "requires macOS with Things 3 installed",
    )
    def test_generated_scripts_compile_against_the_things_dictionary(self) -> None:
        changes = [
            {"operation": "add_todo", "title": 'A "quoted" title', "notes": "x\ny", "when": "someday",
             "deadline": "2026-11-01", "tags": ["T"], "area": "AREA1"},
            {"operation": "add_project", "title": "P", "when": "2026-12-01"},
            {"operation": "update", "id": self.todo, "title": "t", "append_notes": "more",
             "deadline": None, "tags": [], "when": "today"},
            {"operation": "complete", "id": self.todo},
            {"operation": "cancel", "id": self.project},
            {"operation": "reopen", "id": self.todo},
            {"operation": "move", "id": self.todo, "project": self.project},
        ]
        out_dir = realpath_tempdir("things-osacompile-")
        self.addCleanup(shutil.rmtree, out_dir, ignore_errors=True)
        for number, change in enumerate(changes):
            script = helper.build_change_script(change, self.lib)
            result = subprocess.run(
                ["/usr/bin/osacompile", "-o", str(out_dir / f"{number}.scpt"), "-"],
                input=script, capture_output=True, text=True, timeout=30,
            )
            with self.subTest(operation=change["operation"]):
                self.assertEqual(result.returncode, 0, result.stderr)


class ChangeFlowTests(BridgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.todo = self.things.task("Water plants")
        self.osascript = mock.patch.object(helper, "_run_osascript", return_value=(0, self.todo, ""))
        self.run_osascript = self.osascript.start()
        self.addCleanup(self.osascript.stop)
        self.confirm = mock.patch.object(helper, "_run_confirmation", return_value=True)
        self.run_confirmation = self.confirm.start()
        self.addCleanup(self.confirm.stop)

    def preview(self, **params) -> dict:
        return helper.action_change_preview(params, self.view())

    def test_preview_then_change(self) -> None:
        preview = self.preview(operation="complete", id=self.todo)
        self.assertEqual(preview["change"], {"operation": "complete", "id": self.todo})
        self.assertEqual(preview["confirm_nonce_ttl_seconds"], helper.CONFIRM_NONCE_TTL)
        self.run_osascript.assert_not_called()
        result = helper.action_change(
            {"operation": "complete", "id": self.todo, "confirm_nonce": preview["confirm_nonce"]}, self.view()
        )
        self.assertEqual(result["applied"]["id"], self.todo)
        self.run_confirmation.assert_called_once()
        self.assertIn("set status of theItem to completed", self.run_osascript.call_args[0][0])

    def test_change_without_preview_or_with_mismatch_or_replay(self) -> None:
        with self.assertRaisesRegex(helper.WriteGateError, "missing confirm_nonce"):
            helper.action_change({"operation": "complete", "id": self.todo}, self.view())
        preview = self.preview(operation="update", id=self.todo, title="Water ferns")
        with self.assertRaisesRegex(helper.WriteGateError, "differs from preview"):
            helper.action_change(
                {"operation": "update", "id": self.todo, "title": "Water cacti",
                 "confirm_nonce": preview["confirm_nonce"]}, self.view())
        # A rejected nonce is burned, even with the original payload.
        with self.assertRaisesRegex(helper.WriteGateError, "not recognized"):
            helper.action_change(
                {"operation": "update", "id": self.todo, "title": "Water ferns",
                 "confirm_nonce": preview["confirm_nonce"]}, self.view())
        self.run_confirmation.assert_not_called()
        self.run_osascript.assert_not_called()

    def test_cancelled_dialog_does_not_apply(self) -> None:
        self.run_confirmation.return_value = False
        preview = self.preview(operation="complete", id=self.todo)
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            helper.action_change(
                {"operation": "complete", "id": self.todo, "confirm_nonce": preview["confirm_nonce"]}, self.view())
        self.run_osascript.assert_not_called()

    def test_applescript_failure_is_reported(self) -> None:
        self.run_osascript.return_value = (1, "", "Things got an error: Can't get to do id")
        preview = self.preview(operation="complete", id=self.todo)
        with self.assertRaisesRegex(RuntimeError, "AppleScript failed"):
            helper.action_change(
                {"operation": "complete", "id": self.todo, "confirm_nonce": preview["confirm_nonce"]}, self.view())

    def test_preview_policy_skips_the_dialog_but_keeps_the_nonce(self) -> None:
        self.write_policy("write_policy.txt", "preview\n")
        with self.assertRaisesRegex(helper.WriteGateError, "missing confirm_nonce"):
            helper.action_change({"operation": "complete", "id": self.todo}, self.view())
        preview = self.preview(operation="complete", id=self.todo)
        self.assertFalse(preview["dialog_required"])
        result = helper.action_change(
            {"operation": "complete", "id": self.todo, "confirm_nonce": preview["confirm_nonce"]}, self.view())
        self.assertEqual(result["applied"]["approval"], "preview")
        self.run_confirmation.assert_not_called()
        self.run_osascript.assert_called_once()

    def test_confirm_policy_reports_dialog_approval(self) -> None:
        preview = self.preview(operation="complete", id=self.todo)
        self.assertTrue(preview["dialog_required"])
        result = helper.action_change(
            {"operation": "complete", "id": self.todo, "confirm_nonce": preview["confirm_nonce"]}, self.view())
        self.assertEqual(result["applied"]["approval"], "dialog")

    def test_writes_disabled(self) -> None:
        self.write_policy("write_policy.txt", "disabled\n")
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.preview(operation="complete", id=self.todo)

    def test_preview_rejects_nonce_parameter(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not take"):
            self.preview(operation="complete", id=self.todo, confirm_nonce="x" * 20)


class ConfirmationHelperTests(unittest.TestCase):
    def test_exit_codes_map_to_decisions(self) -> None:
        description = {"headline": "h", "rows": [["a", "b"]], "details": ""}
        for code, expected in ((0, True), (1, False), (3, False)):
            completed = subprocess.CompletedProcess([], code, "", "")
            with self.subTest(code=code), mock.patch.object(helper.subprocess, "run", return_value=completed):
                self.assertIs(helper._run_confirmation(description), expected)
        broken = subprocess.CompletedProcess([], 2, "", "invalid JSON input")
        with mock.patch.object(helper.subprocess, "run", return_value=broken):
            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                helper._run_confirmation(description)
        timeout = subprocess.TimeoutExpired("x", 1)
        with mock.patch.object(helper.subprocess, "run", side_effect=timeout):
            self.assertFalse(helper._run_confirmation(description))


class WriteGateTests(BridgeTestCase):
    def test_nonce_lifecycle(self) -> None:
        change = {"operation": "complete", "id": "X"}
        nonce = helper.mint_confirm_nonce(change)
        record = self.bridge / "nonces" / f"{nonce}.json"
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.bridge / "nonces").stat().st_mode & 0o777, 0o700)
        # Key order does not matter; content does.
        helper.consume_confirm_nonce(nonce, {"id": "X", "operation": "complete"})
        self.assertFalse(record.exists())
        with self.assertRaises(helper.WriteGateError):
            helper.consume_confirm_nonce(nonce, change)

    def test_expired_and_malformed_nonces(self) -> None:
        change = {"operation": "complete", "id": "X"}
        nonce = helper.mint_confirm_nonce(change)
        future = time.time() + helper.CONFIRM_NONCE_TTL + 5
        with mock.patch.object(helper._write_gate.time, "time", return_value=future):
            with self.assertRaisesRegex(helper.WriteGateError, "expired"):
                helper.consume_confirm_nonce(nonce, change)
        for bad in ("../../etc/passwd", "short", "", None, 7):
            with self.subTest(bad=bad), self.assertRaises(helper.WriteGateError):
                helper.consume_confirm_nonce(bad, change)

    def test_reaper(self) -> None:
        change = {"operation": "complete", "id": "X"}
        fresh = helper.mint_confirm_nonce(change)
        stale = helper.mint_confirm_nonce(change)
        stale_path = self.bridge / "nonces" / f"{stale}.json"
        old = time.time() - 3600
        os.utime(stale_path, (old, old))
        stale_path.write_text('{"change_hash": "x", "expires_at": 1}')
        os.chmod(stale_path, 0o600)
        os.utime(stale_path, (old, old))
        helper.reap_expired_nonces()
        self.assertFalse(stale_path.exists())
        self.assertTrue((self.bridge / "nonces" / f"{fresh}.json").exists())


if __name__ == "__main__":
    unittest.main()
