import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tcf_monitor.monitor import (
    build_email,
    newly_open_sessions,
    parse_oncord_exam_table,
    split_addresses,
)

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_URL = "https://www.alliancefrancaise.ca/en/language/exams/tcf-canada/"


class ParserTests(unittest.TestCase):
    def parse_fixture(self, name: str):
        return parse_oncord_exam_table(
            (FIXTURES / name).read_text(encoding="utf-8"), PAGE_URL
        )

    def test_parses_full_session(self):
        session = self.parse_fixture("full.html")[0]
        self.assertEqual(session.title, "TCF-Canada October 2, 2026")
        self.assertEqual(session.status, "Full")
        self.assertFalse(session.is_open)
        self.assertEqual(session.booking_url, PAGE_URL)

    def test_parses_available_session_and_absolute_booking_link(self):
        session = self.parse_fixture("open.html")[0]
        self.assertTrue(session.is_open)
        self.assertEqual(
            session.booking_url,
            "https://www.alliancefrancaise.ca/commerce/book/123",
        )

    def test_parses_temporarily_held_session_as_unavailable(self):
        session = self.parse_fixture("held.html")[0]
        self.assertEqual(session.status, "Spots held")
        self.assertFalse(session.is_open)
        self.assertEqual(session.booking_url, PAGE_URL)

    def test_alerts_when_a_temporarily_held_session_becomes_available(self):
        held = self.parse_fixture("held.html")[0]
        available = self.parse_fixture("open.html")[0]
        previous = {"sessions": {held.key: {"is_open": False}}}
        self.assertEqual(newly_open_sessions([available], previous), [available])

    def test_rejects_page_without_exam_table(self):
        with self.assertRaisesRegex(ValueError, "exam table"):
            parse_oncord_exam_table("<html></html>", PAGE_URL)

    def test_alerts_on_closed_to_open_transition_only(self):
        full = self.parse_fixture("full.html")[0]
        available = self.parse_fixture("open.html")[0]
        previous = {"sessions": {full.key: {"is_open": False}}}
        self.assertEqual(newly_open_sessions([available], previous), [available])

        previous["sessions"][full.key]["is_open"] = True
        self.assertEqual(newly_open_sessions([available], previous), [])

    def test_new_open_session_is_an_alert(self):
        available = self.parse_fixture("open.html")[0]
        self.assertEqual(newly_open_sessions([available], {}), [available])


class EmailTests(unittest.TestCase):
    def test_splits_and_deduplicates_recipient_list(self):
        self.assertEqual(
            split_addresses("one@example.com, two@example.com\none@example.com"),
            ["one@example.com", "two@example.com"],
        )

    @patch.dict(
        os.environ,
        {
            "NOTIFY_EMAILS": "one@example.com,two@example.com",
            "EMAIL_FROM": "monitor@example.com",
        },
        clear=True,
    )
    def test_email_contains_booking_link_and_all_recipients(self):
        session = ParserTests().parse_fixture("open.html")[0]
        monitor = {"name": "Test monitor"}
        message = build_email([(monitor, session)])
        self.assertEqual(message["To"], "monitor@example.com")
        self.assertEqual(message["Bcc"], "one@example.com, two@example.com")
        self.assertIn(
            session.booking_url,
            message.get_body(preferencelist=("plain",)).get_content(),
        )


if __name__ == "__main__":
    unittest.main()
