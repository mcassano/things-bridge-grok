from __future__ import annotations

import sqlite3
import unittest
from datetime import timedelta

from tests._helper_loader import helper
from tests.fixtures import NOW, TODAY, BridgeTestCase


def titles(items: list[dict]) -> list[str]:
    return [item["title"] for item in items]


class ListTests(BridgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        t = self.things
        self.work = t.area("Work")
        self.project = t.project("Launch", area=self.work)
        self.someday_project = t.project("Maybe later", start=2, area=self.work)
        self.done_project = t.project("Shipped", status=3, stopped=NOW - timedelta(days=1))
        t.task("Inbox item", start=0)
        t.task("Today second", start_date=TODAY, today_index=2)
        t.task("Today first", start_date=TODAY - timedelta(days=3), today_index=1)
        t.task("Tonight", start_date=TODAY, evening=True, today_index=0)
        t.task("Deadline today, unscheduled", deadline=TODAY)
        t.task("Next week", start_date=TODAY + timedelta(days=7))
        t.task("Far future", start_date=TODAY + timedelta(days=90))
        t.task("Anytime loose", index=1)
        t.task("In project", project=self.project, index=2)
        t.task("In someday project", project=self.someday_project)
        t.task("In done project", project=self.done_project)
        t.task("Someday item", start=2)
        t.task("Done yesterday", status=3, stopped=NOW - timedelta(days=1))
        t.task("Canceled today", status=2, stopped=NOW - timedelta(hours=1))
        t.task("Done long ago", status=3, stopped=NOW - timedelta(days=40))
        t.task("Trashed", trashed=True)
        t.task("Repeating template", template=True, start_date=TODAY)
        self.view_ = self.view()

    def list_titles(self, name: str, **params) -> list[str]:
        return titles(helper.action_list({"list": name, **params}, self.view_)["items"])

    def test_inbox(self) -> None:
        self.assertEqual(self.list_titles("inbox"), ["Inbox item"])

    def test_today_orders_evening_last_and_includes_due_unscheduled(self) -> None:
        self.assertEqual(
            self.list_titles("today"),
            ["Deadline today, unscheduled", "Today first", "Today second", "Tonight"],
        )
        tonight = next(i for i in helper.action_list({"list": "today"}, self.view_)["items"]
                       if i["title"] == "Tonight")
        self.assertEqual(tonight["when"], "evening")

    def test_upcoming_respects_window(self) -> None:
        self.assertEqual(self.list_titles("upcoming"), ["Next week"])
        self.assertEqual(self.list_titles("upcoming", days=120), ["Next week", "Far future"])

    def test_anytime_excludes_someday_projects_and_closed_projects(self) -> None:
        anytime = self.list_titles("anytime")
        self.assertIn("Anytime loose", anytime)
        self.assertIn("In project", anytime)
        self.assertIn("Launch", anytime)
        self.assertNotIn("In someday project", anytime)
        self.assertNotIn("In done project", anytime)
        self.assertNotIn("Trashed", anytime)
        self.assertNotIn("Repeating template", anytime)

    def test_someday(self) -> None:
        self.assertEqual(sorted(self.list_titles("someday")), ["Maybe later", "Someday item"])

    def test_logbook_window_and_order(self) -> None:
        logbook = self.list_titles("logbook")
        self.assertEqual(logbook[0], "Canceled today")
        self.assertEqual(sorted(logbook[1:]), ["Done yesterday", "Shipped"])
        self.assertIn("Done long ago", self.list_titles("logbook", days=60))

    def test_limit_and_truncation(self) -> None:
        result = helper.action_list({"list": "today", "limit": 2}, self.view_)
        self.assertEqual(result["count"], 4)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["items"]), 2)

    def test_scope_filters(self) -> None:
        self.assertEqual(self.list_titles("anytime", project="Launch"), ["Launch", "In project"])
        self.assertEqual(self.list_titles("anytime", area=self.work), ["Launch", "In project"])
        with self.assertRaises(ValueError):
            helper.action_list({"list": "anytime", "area": "Nope"}, self.view_)

    def test_rejects_unknown_lists_and_params(self) -> None:
        with self.assertRaises(ValueError):
            helper.action_list({"list": "trash"}, self.view_)
        with self.assertRaises(ValueError):
            helper.action_list({"list": "today", "sql": "1"}, self.view_)

    def test_items_carry_links_and_no_notes_by_default(self) -> None:
        item = helper.action_list({"list": "inbox"}, self.view_)["items"][0]
        self.assertTrue(item["url"].startswith("things:///show?id="))
        self.assertNotIn("notes", item)


class ReviewTests(BridgeTestCase):
    def test_buckets(self) -> None:
        t = self.things
        t.task("Late", deadline=TODAY - timedelta(days=2), start_date=TODAY - timedelta(days=5))
        t.task("Due Friday", deadline=TODAY + timedelta(days=4))
        t.task("Due next month", deadline=TODAY + timedelta(days=30))
        t.task("Plan", start_date=TODAY)
        t.task("Capture", start=0)
        result = helper.action_review({}, self.view())
        self.assertEqual(titles(result["overdue"]), ["Late"])
        self.assertTrue(result["overdue"][0]["overdue"])
        self.assertEqual(titles(result["due_soon"]), ["Due Friday"])
        self.assertEqual(sorted(titles(result["today"])), ["Late", "Plan"])
        self.assertEqual(titles(result["inbox"]), ["Capture"])
        self.assertEqual(result["counts"]["open_total"], 5)


class SearchTests(BridgeTestCase):
    def test_matches_title_notes_and_checklist(self) -> None:
        t = self.things
        t.task("Call plumber", notes="ask about the boiler")
        t.task("House chores", checklist=[("Fix BOILER valve", 0), ("Mop", 1)])
        t.task("Boiler service", status=3, stopped=NOW - timedelta(days=2))
        view = self.view()
        result = helper.action_search({"term": "boiler"}, view)
        self.assertEqual(result["match_count"], 2)
        matched = {item["title"]: item["matched_in"] for item in result["items"]}
        self.assertEqual(matched["Call plumber"], ["notes"])
        self.assertEqual(matched["House chores"], ["checklist"])
        everything = helper.action_search({"term": "boiler", "include_completed": True}, view)
        self.assertEqual(everything["match_count"], 3)
        self.assertEqual(everything["items"][-1]["title"], "Boiler service")

    def test_term_validation(self) -> None:
        with self.assertRaises(ValueError):
            helper.action_search({"term": "  "}, self.view())


class GetTests(BridgeTestCase):
    def test_todo_detail_has_full_notes_and_checklist(self) -> None:
        t = self.things
        long_notes = "line\n" * 200
        tag = t.tag("Errands")
        uid = t.task("Groceries", notes=long_notes, tags=[tag], checklist=[("Milk", 3), ("Eggs", 0)])
        item = helper.action_get({"id": uid}, self.view())["item"]
        self.assertEqual(item["notes"], long_notes)
        self.assertEqual(item["tags"], ["Errands"])
        self.assertEqual(item["checklist"], {"open": 1, "total": 2})
        self.assertEqual(
            item["checklist_items"],
            [{"title": "Milk", "completed": True, "canceled": False},
             {"title": "Eggs", "completed": False, "canceled": False}],
        )

    def test_project_detail_groups_by_heading(self) -> None:
        t = self.things
        project = t.project("Move house")
        later = t.heading("Later", project, index=2)
        first = t.heading("First", project, index=1)
        t.task("Loose", project=project)
        t.task("Pack", heading=first)
        t.task("Unpack", heading=later)
        t.task("Done step", project=project, status=3, stopped=NOW)
        result = helper.action_get({"id": project}, self.view())
        self.assertEqual([h["title"] for h in result["headings"]], ["First", "Later"])
        self.assertEqual(titles(result["todos"]), ["Loose", "Pack", "Unpack"])
        self.assertEqual(result["todos"][1]["heading"], "First")
        self.assertEqual(result["todos"][1]["project"]["title"], "Move house")
        self.assertEqual(result["item"]["progress"], {"open": 3, "completed": 1, "canceled": 0})
        with_done = helper.action_get({"id": project, "include_completed": True}, self.view())
        self.assertIn("Done step", titles(with_done["todos"]))

    def test_area_detail(self) -> None:
        t = self.things
        area = t.area("Home")
        t.project("Garden", area=area)
        t.task("Fix door", area=area)
        result = helper.action_get({"id": area}, self.view())
        self.assertEqual(result["item"]["type"], "area")
        self.assertEqual(titles(result["projects"]), ["Garden"])
        self.assertEqual(titles(result["todos"]), ["Fix door"])

    def test_missing_and_trashed_ids(self) -> None:
        uid = self.things.task("Gone", trashed=True)
        view = self.view()
        for ref in (uid, "NOPE"):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "no visible item"):
                helper.action_get({"id": ref}, view)


class CatalogTests(BridgeTestCase):
    def test_projects_areas_tags(self) -> None:
        t = self.things
        home = t.area("Home", index=1)
        work = t.area("Work", index=0)
        parent = t.tag("Places")
        child = t.tag("Store", parent=parent)
        garden = t.project("Garden", area=home, tags=[child])
        t.project("Someday plan", area=work, start=2)
        t.task("Weed", project=garden, tags=[child])
        t.task("Email", area=work)
        view = self.view()

        projects = helper.action_projects({}, view)
        self.assertEqual(titles(projects["projects"]), ["Someday plan", "Garden"])
        self.assertEqual(titles(helper.action_projects({"include_someday": False}, view)["projects"]), ["Garden"])

        areas = helper.action_areas({}, view)["areas"]
        self.assertEqual([(a["title"], a["open_projects"], a["open_todos"]) for a in areas],
                         [("Work", 1, 1), ("Home", 1, 1)])

        tags = {tag["title"]: tag for tag in helper.action_tags({}, view)["tags"]}
        self.assertEqual(tags["Store"]["parent"], "Places")
        self.assertEqual(tags["Store"]["open_items"], 2)

        by_parent_tag = helper.action_list({"list": "anytime", "tag": "Places"}, view)
        self.assertEqual(sorted(titles(by_parent_tag["items"])), ["Garden", "Weed"])


class StatsTests(BridgeTestCase):
    def test_completion_stats(self) -> None:
        t = self.things
        area = t.area("Work")
        t.task("A", area=area, status=3, created=NOW - timedelta(days=4), stopped=NOW - timedelta(days=2))
        t.task("B", status=3, created=NOW - timedelta(days=1), stopped=NOW)
        t.task("C", status=2, stopped=NOW)
        t.task("Old", status=3, created=NOW - timedelta(days=100), stopped=NOW - timedelta(days=60))
        t.task("New open", created=NOW)
        stats = helper.action_completion_stats({"days": 7}, self.view())
        self.assertEqual(stats["completed"], 2)
        self.assertEqual(stats["canceled"], 1)
        self.assertEqual(stats["lead_time_days"]["sample_size"], 2)
        self.assertEqual(stats["lead_time_days"]["median"], 1.5)
        self.assertEqual(len(stats["series"]), 7)
        self.assertEqual(sum(day["completed"] for day in stats["series"]), 2)
        self.assertEqual({a["title"] for a in stats["by_area"]}, {"Work", "(no area)"})


class SchemaTests(BridgeTestCase):
    def test_missing_required_column_is_reported(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            'CREATE TABLE TMTask (uuid TEXT, type INTEGER, title TEXT);'
            'CREATE TABLE TMArea (uuid TEXT, title TEXT);'
            'CREATE TABLE TMTag (uuid TEXT, title TEXT);'
            'CREATE TABLE TMTaskTag (tasks TEXT, tags TEXT);'
        )
        with self.assertRaisesRegex(helper.UnsupportedSchema, "TMTask lacks"):
            helper.Library(conn, today=TODAY)

    def test_optional_columns_and_tables_may_be_absent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            'CREATE TABLE TMTask (uuid TEXT, type INTEGER, status INTEGER, trashed INTEGER, '
            'title TEXT, notes TEXT, start INTEGER, startDate INTEGER, deadline INTEGER, '
            'stopDate REAL, creationDate REAL, area TEXT, project TEXT, heading TEXT, "index" INTEGER);'
            'CREATE TABLE TMArea (uuid TEXT, title TEXT);'
            'CREATE TABLE TMTag (uuid TEXT, title TEXT);'
            'CREATE TABLE TMTaskTag (tasks TEXT, tags TEXT);'
        )
        conn.execute("INSERT INTO TMTask (uuid, type, status, trashed, title, start) VALUES ('X1', 0, 0, 0, 'Hi', 0)")
        library = helper.Library(conn, today=TODAY)
        self.assertEqual(library.tasks["X1"].title, "Hi")
        self.assertEqual(library.checklist("X1"), [])


if __name__ == "__main__":
    unittest.main()
