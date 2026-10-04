from __future__ import annotations

import os
import unittest
from unittest import mock

from tests._helper_loader import helper
from tests.fixtures import TODAY, BridgeTestCase


def titles(items: list[dict]) -> list[str]:
    return sorted(item["title"] for item in items)


class PolicyFixture(BridgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        t = self.things
        self.private = t.tag("Private")
        self.medical = t.tag("Medical", parent=self.private)
        self.health = t.area("Health")
        self.work = t.area("Work")
        self.tagged_area = t.area("Journal", tags=[self.private])
        self.clinic = t.project("Clinic visits", area=self.health)
        self.launch = t.project("Launch", area=self.work)
        self.secret_project = t.project("Surprise party", tags=[self.private])
        self.checkup = t.task("Book checkup", project=self.clinic)
        self.ship = t.task("Ship it", project=self.launch)
        self.cake = t.task("Order cake", project=self.secret_project)
        self.pills = t.task("Refill pills", tags=[self.medical])
        self.diary = t.task("Write entry", area=self.tagged_area)
        self.loose = t.task("Buy milk")


class BlocklistTests(PolicyFixture):
    def test_blocked_scopes_disappear_everywhere(self) -> None:
        self.write_policy("blocked_scopes.txt", "# comment\narea:health\ntag:Private\n")
        view = self.view()
        visible = titles(helper.action_list({"list": "anytime"}, view)["items"])
        self.assertEqual(visible, ["Buy milk", "Launch", "Ship it"])
        # Child tag of a blocked tag, tag inherited from project, and area tag.
        for uid in (self.pills, self.cake, self.diary, self.checkup, self.clinic):
            with self.subTest(uid=uid), self.assertRaisesRegex(ValueError, "no visible item"):
                helper.action_get({"id": uid}, view)
        self.assertEqual(
            [a["title"] for a in helper.action_areas({}, view)["areas"]], ["Work"]
        )
        self.assertNotIn("Private", [t["title"] for t in helper.action_tags({}, view)["tags"]])
        self.assertNotIn("Medical", [t["title"] for t in helper.action_tags({}, view)["tags"]])
        self.assertEqual(helper.action_search({"term": "pills"}, view)["match_count"], 0)

    def test_blocking_by_id_and_project(self) -> None:
        self.write_policy("blocked_scopes.txt", f"project:{self.launch}\nunfiled\n")
        visible = titles(helper.action_list({"list": "anytime"}, self.view())["items"])
        self.assertNotIn("Ship it", visible)
        self.assertNotIn("Launch", visible)
        self.assertNotIn("Buy milk", visible)
        self.assertNotIn("Refill pills", visible)
        self.assertIn("Book checkup", visible)

    def test_untrusted_blocklist_fails_closed(self) -> None:
        self.write_policy("blocked_scopes.txt", "area:Health\n", mode=0o666)
        policy = helper.load_privacy_policy()
        self.assertEqual(policy.mode, "allowlist")
        self.assertEqual(policy.allowlist, ())
        self.assertEqual(helper.action_list({"list": "anytime"}, self.view())["count"], 0)

    def test_blocklist_wins_over_allowlist(self) -> None:
        self.write_policy("read_policy.txt", "allowlist\n")
        self.write_policy("allowed_scopes.txt", "area:Health\n")
        self.write_policy("blocked_scopes.txt", "project:Clinic visits\n")
        self.assertEqual(helper.action_list({"list": "anytime"}, self.view())["count"], 0)


class AllowlistTests(PolicyFixture):
    def test_allowlist_is_default_deny(self) -> None:
        self.write_policy("read_policy.txt", "allowlist\n")
        view = self.view()
        self.assertEqual(helper.action_list({"list": "anytime"}, view)["count"], 0)
        self.assertEqual(helper.action_areas({}, view)["count"], 0)
        self.assertEqual(helper.action_tags({}, view)["count"], 0)

    def test_allowlist_entries(self) -> None:
        self.write_policy("read_policy.txt", "allowlist\n")
        self.write_policy("allowed_scopes.txt", "area:Work\nunfiled\n")
        view = self.view()
        self.assertEqual(
            titles(helper.action_list({"list": "anytime"}, view)["items"]),
            ["Buy milk", "Launch", "Refill pills", "Ship it"],
        )
        self.assertEqual([a["title"] for a in helper.action_areas({}, view)["areas"]], ["Work"])

    def test_invalid_read_policy_fails_closed(self) -> None:
        self.write_policy("read_policy.txt", "everything\n")
        self.assertEqual(helper.load_privacy_policy().mode, "allowlist")

    def test_wrapper_override_and_root_requirement(self) -> None:
        self.write_policy("read_policy.txt", "blocklist\n")
        self.write_policy("allowed_scopes.txt", "area:Work\n")
        env = {"THINGS_READ_POLICY": "allowlist", "THINGS_REQUIRE_ROOT_POLICY": "1"}
        with mock.patch.dict(os.environ, env):
            policy = helper.load_privacy_policy()
        self.assertEqual(policy.mode, "allowlist")
        # A user-owned file is not trusted when root ownership is required.
        if os.getuid() != 0:
            self.assertEqual(policy.allowlist, ())


class WritePolicyTests(BridgeTestCase):
    def test_default_and_values(self) -> None:
        self.assertEqual(helper.load_write_policy(), "confirm")
        self.write_policy("write_policy.txt", "disabled\n")
        self.assertEqual(helper.load_write_policy(), "disabled")
        self.write_policy("write_policy.txt", "yolo\n")
        self.assertEqual(helper.load_write_policy(), "disabled")
        self.write_policy("write_policy.txt", "confirm\n", mode=0o666)
        self.assertEqual(helper.load_write_policy(), "disabled")


class ChangeScopeTests(PolicyFixture):
    def test_changes_outside_policy_are_refused(self) -> None:
        self.write_policy("blocked_scopes.txt", "area:Health\ntag:Private\n")
        view = self.view()
        with self.assertRaisesRegex(ValueError, "no visible item"):
            helper.normalize_change({"operation": "complete", "id": self.checkup}, view)
        with self.assertRaisesRegex(ValueError, "no visible project"):
            helper.normalize_change(
                {"operation": "add_todo", "title": "x", "project": self.clinic}, view
            )
        with self.assertRaisesRegex(ValueError, "outside the read policy"):
            helper.normalize_change({"operation": "add_todo", "title": "x", "tags": ["Medical"]}, view)
        with self.assertRaisesRegex(ValueError, "no visible area"):
            helper.normalize_change({"operation": "move", "id": self.loose, "area": self.health}, view)

    def test_allowlist_requires_unfiled_for_inbox_adds(self) -> None:
        self.write_policy("read_policy.txt", "allowlist\n")
        self.write_policy("allowed_scopes.txt", "area:Work\n")
        view = self.view()
        with self.assertRaisesRegex(ValueError, "outside the read policy"):
            helper.normalize_change({"operation": "add_todo", "title": "x"}, view)
        change = helper.normalize_change(
            {"operation": "add_todo", "title": "x", "project": self.launch}, view
        )
        self.assertEqual(change["project"], self.launch)


if __name__ == "__main__":
    unittest.main()
