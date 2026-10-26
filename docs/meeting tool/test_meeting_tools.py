import tempfile
import unittest
from pathlib import Path

from meeting_tools import (
    ask_meeting,
    export_meeting,
    get_meeting,
    list_meetings,
    save_meeting,
    search_meetings,
    summarize_meeting,
)


class MeetingToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "meetings.sqlite3"
        self.payload = {
            "title": "Project launch",
            "date": "2026-10-25",
            "transcript": (
                "The team approved the November launch. "
                "Mina will prepare the launch checklist."
            ),
            "decisions": ["Launch in November"],
            "action_items": ["Prepare launch checklist"],
            "participants": ["Mina", "Omar"],
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_list_get_search_and_export(self):
        saved = save_meeting(self.payload, db_path=self.db_path)

        self.assertEqual(saved["record"]["event_type"], "meeting.recorded")
        self.assertEqual(len(list_meetings(db_path=self.db_path)), 1)
        self.assertEqual(get_meeting(saved["id"], db_path=self.db_path)["id"], saved["id"])
        self.assertEqual(search_meetings("November launch", db_path=self.db_path)[0]["id"], saved["id"])
        self.assertEqual(export_meeting(saved["id"], db_path=self.db_path)["record"], saved["record"])

    def test_summary_and_question_retrieve_saved_meeting_facts(self):
        saved = save_meeting(self.payload, db_path=self.db_path)

        summary = summarize_meeting(saved["id"], db_path=self.db_path)
        answer = ask_meeting(
            saved["id"], "What did the team decide about the launch?",
            db_path=self.db_path,
        )

        self.assertEqual(summary["decisions"], ["Launch in November"])
        self.assertEqual(summary["action_items"], ["Prepare launch checklist"])
        self.assertIn("approved the November launch", answer["answer"])
        self.assertEqual(answer["answer_type"], "keyword_retrieval")

    def test_missing_meeting_is_explicit(self):
        with self.assertRaises(LookupError):
            get_meeting("missing", db_path=self.db_path)


if __name__ == "__main__":
    unittest.main()
