from __future__ import annotations

import unittest
from datetime import date

from tests._helper_loader import helper
from tests.fixtures import TODAY


class ThingsDateTests(unittest.TestCase):
    def test_decodes_packed_dates_from_a_real_database(self) -> None:
        # Value observed in a Things 3.23 database.
        self.assertEqual(helper.decode_things_date(132191104), date(2017, 1, 7))

    def test_round_trip(self) -> None:
        for day in (date(2026, 1, 1), date(2026, 12, 31), date(2028, 2, 29), date(2099, 7, 15)):
            with self.subTest(day=day):
                self.assertEqual(helper.decode_things_date(helper.encode_things_date(day)), day)

    def test_invalid_values_decode_to_none(self) -> None:
        for value in (None, 0, -5, True, "x", (2026 << 16) | (13 << 12) | (1 << 7)):
            with self.subTest(value=value):
                self.assertIsNone(helper.decode_things_date(value))

    def test_timestamps(self) -> None:
        self.assertIsNone(helper.decode_timestamp(None))
        self.assertIsNone(helper.decode_timestamp(0))
        self.assertEqual(helper.decode_timestamp(1_700_000_000).year, 2023)


class NotesTests(unittest.TestCase):
    def test_plain_notes_pass_through(self) -> None:
        self.assertEqual(helper.normalize_notes("- [ ] milk\n**bold**"), "- [ ] milk\n**bold**")

    def test_legacy_xml_notes_are_unwrapped(self) -> None:
        raw = '<note xml:space="preserve">Call &amp; confirm<br/>please</note>'
        self.assertEqual(helper.normalize_notes(raw), "Call & confirmplease")

    def test_missing_notes(self) -> None:
        self.assertEqual(helper.normalize_notes(None), "")


class RedactionTests(unittest.TestCase):
    def test_masks_codes_cards_and_ssns(self) -> None:
        text = "Your verification code is 482913. Card 4111 1111 1111 1111. SSN 123-45-6789."
        redacted = helper.redact(text)
        self.assertNotIn("482913", redacted)
        self.assertNotIn("4111 1111", redacted)
        self.assertNotIn("123-45-6789", redacted)
        self.assertIn("[REDACTED-2FA]", redacted)
        self.assertIn("[REDACTED-CARD]", redacted)
        self.assertIn("[REDACTED-SSN]", redacted)

    def test_leaves_ordinary_numbers(self) -> None:
        self.assertEqual(helper.redact("Buy 12 eggs by 2026-10-05"), "Buy 12 eggs by 2026-10-05")


class ValidationTests(unittest.TestCase):
    def test_title(self) -> None:
        self.assertEqual(helper.validate_title("  Buy milk  "), "Buy milk")
        for bad in ("", "   ", "a\nb", "a\tb", "a b", "x" * (helper.MAX_TITLE_LEN + 1), 5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                helper.validate_title(bad)

    def test_notes(self) -> None:
        self.assertEqual(helper.validate_notes("a\r\nb\rc\td"), "a\nb\nc\td")
        self.assertEqual(helper.validate_notes(""), "")
        for bad in ("a\x00b", "a\x1bb", "x" * (helper.MAX_NOTES_LEN + 1), 3):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                helper.validate_notes(bad)

    def test_when(self) -> None:
        cases = {
            "today": "today",
            " Anytime ": "anytime",
            "someday": "someday",
            "inbox": "inbox",
            "tomorrow": "2026-10-06",
            "2026-10-05": "today",
            "2026-12-24": "2026-12-24",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(helper.validate_when(value, TODAY, allow_inbox=True), expected)

    def test_when_rejections(self) -> None:
        for value in ("evening", "2026-10-04", "2026-02-30", "next week", "", "2040-01-01", 7):
            with self.subTest(value=value), self.assertRaises(ValueError):
                helper.validate_when(value, TODAY, allow_inbox=True)
        with self.assertRaises(ValueError):
            helper.validate_when("inbox", TODAY, allow_inbox=False)

    def test_deadline(self) -> None:
        self.assertEqual(helper.validate_deadline("2026-11-01"), "2026-11-01")
        for bad in ("2026-11-31", "11/01/2026", "1999-12-31", "2101-01-01", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                helper.validate_deadline(bad)

    def test_int_parameters(self) -> None:
        self.assertEqual(helper.validate_limit(None), helper.DEFAULT_LIMIT)
        self.assertEqual(helper.validate_limit(5.0), 5)
        self.assertEqual(helper.validate_limit("7"), 7)
        for bad in (True, 0, 1.5, "x", helper.MAX_LIMIT + 1, [1]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                helper.validate_limit(bad)

    def test_ids(self) -> None:
        self.assertEqual(helper.validate_id("Ab3-9xYz"), "Ab3-9xYz")
        for bad in ("", "../x", "a b", "a/b", "x" * 65, None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                helper.validate_id(bad)


class ScopeEntryTests(unittest.TestCase):
    def test_parses_entries(self) -> None:
        self.assertEqual(helper.parse_scope_entry("Area: Health "), helper.ScopeEntry("area", "health"))
        self.assertEqual(helper.parse_scope_entry("tag:Private"), helper.ScopeEntry("tag", "private"))
        self.assertEqual(helper.parse_scope_entry("UNFILED"), helper.ScopeEntry("unfiled", ""))
        self.assertEqual(
            helper.parse_scope_entry("project:Taxes: 2026"), helper.ScopeEntry("project", "taxes: 2026")
        )

    def test_rejects_malformed_entries(self) -> None:
        for bad in ("Health", "folder:Work", "area:", "tag:\x07x", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                helper.parse_scope_entry(bad)


class AppleScriptQuotingTests(unittest.TestCase):
    def test_quotes_and_backslashes_are_escaped(self) -> None:
        self.assertEqual(helper.as_string('say "hi" \\ bye'), '"say \\"hi\\" \\\\ bye"')

    def test_injection_attempt_stays_inside_the_literal(self) -> None:
        literal = helper.as_string('x" & (do shell script "id") & "')
        # The literal starts and ends with the only unescaped quotes.
        inner = literal[1:-1]
        self.assertNotIn('"', inner.replace('\\"', ""))


if __name__ == "__main__":
    unittest.main()
