import os
import sys
import unittest
from pathlib import Path

os.environ["MODEL_PROVIDER"] = "mock"

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ariadne.api.facade import AriadneAPI  # noqa: E402


class TestPI1Phase(unittest.TestCase):
    def setUp(self) -> None:
        self.api = AriadneAPI()

    def _generate(self):
        response = self.api.generate_courseware(
            {
                "topic": "Transformer",
                "keywords": ["attention", "encoder", "decoder"],
                "difficulty": "beginner",
                "style": "engineering",
                "template": "tutorial",
            }
        )
        self.assertEqual(response["code"], 0)
        return response["data"]["courseware_id"]

    # TC-PI1-001
    def test_empty_topic_should_fail(self):
        response = self.api.generate_courseware(
            {
                "topic": "",
                "keywords": [],
                "difficulty": "beginner",
                "style": "engineering",
                "template": "tutorial",
            }
        )
        self.assertEqual(response["code"], 10001)

    # TC-PI1-002 + TC-PI1-004
    def test_topic_length_two_should_generate_courseware_with_three_chunks(self):
        response = self.api.generate_courseware(
            {
                "topic": "AI",
                "keywords": ["模型"],
                "difficulty": "beginner",
                "style": "engineering",
                "template": "tutorial",
            }
        )
        self.assertEqual(response["code"], 0)
        courseware_id = response["data"]["courseware_id"]

        cw = self.api.get_courseware(courseware_id)
        self.assertEqual(cw["code"], 0)
        self.assertGreaterEqual(len(cw["data"]["chunks"]), 3)

    # TC-PI1-003
    def test_progress_events_are_visible_after_generate(self):
        courseware_id = self._generate()
        progress = self.api.get_progress(courseware_id)
        self.assertEqual(progress["code"], 0)
        self.assertGreater(len(progress["data"]["events"]), 0)
        phases = {ev["phase"] for ev in progress["data"]["events"]}
        self.assertIn("retrieving", phases)

    # TC-PI1-005
    def test_ask_chunk_should_link_current_chunk(self):
        courseware_id = self._generate()
        courseware = self.api.get_courseware(courseware_id)
        chunk_id = courseware["data"]["chunks"][0]["id"]

        answer = self.api.ask_chunk(
            chunk_id,
            {
                "question": "这个点我没懂",
                "page_id": "pg_001",
                "chunk_id": chunk_id,
                "selection": {"text": "attention"},
                "mode": "deep",
            },
        )
        self.assertEqual(answer["code"], 0)
        self.assertEqual(answer["data"]["linked_chunk_id"], chunk_id)
        self.assertIn(chunk_id, answer["data"]["answer"])

    # TC-PI1-006
    def test_upload_valid_pdf_should_be_ready(self):
        upload = self.api.upload_asset("notes.pdf", 1024)
        self.assertEqual(upload["code"], 0)

        status = self.api.get_asset_status(upload["data"]["asset_id"])
        self.assertEqual(status["code"], 0)
        self.assertEqual(status["data"]["status"], "ready")

    # TC-PI1-007
    def test_upload_invalid_file_type_should_fail(self):
        upload = self.api.upload_asset("virus.exe", 2048)
        self.assertEqual(upload["code"], 10006)

    # TC-PI1-008
    def test_export_html_should_be_openable_offline(self):
        courseware_id = self._generate()
        export = self.api.export_courseware(courseware_id, "html")
        self.assertEqual(export["code"], 0)
        html_payload = self.api.export_html_content(courseware_id)
        self.assertEqual(html_payload["code"], 0)
        html_text = html_payload["data"]["html"]
        self.assertTrue(html_text.startswith("<!doctype html>"))
        self.assertIn("<section", html_text)

    # TC-PI1-009
    def test_guest_mode_should_allow_core_flow(self):
        self.assertTrue(self.api.is_guest_mode_available())
        courseware_id = self._generate()
        progress = self.api.get_progress(courseware_id)
        self.assertEqual(progress["code"], 0)


if __name__ == "__main__":
    unittest.main()
