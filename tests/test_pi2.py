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


class TestPI2Phase(unittest.TestCase):
    def setUp(self) -> None:
        self.api = AriadneAPI()
        gen = self.api.generate_courseware(
            {
                "topic": "Transformer",
                "keywords": ["attention", "encoder", "decoder"],
                "difficulty": "beginner",
                "style": "engineering",
                "template": "tutorial",
            }
        )
        self.assertEqual(gen["code"], 0)
        self.courseware_id = gen["data"]["courseware_id"]
        cw = self.api.get_courseware(self.courseware_id)
        self.chunk_id = cw["data"]["chunks"][0]["id"]

    def test_update_chunk_state(self):
        res = self.api.update_chunk_state(
            self.chunk_id,
            {"understand_state": "understood", "is_favorite": True, "collapsed": True},
        )
        self.assertEqual(res["code"], 0)
        self.assertEqual(res["data"]["understand_state"], "understood")

    def test_append_accept_and_reject(self):
        ask = self.api.ask_chunk(
            self.chunk_id,
            {
                "question": "解释一下",
                "page_id": "pg_001",
                "selection": {"text": "attention"},
                "mode": "deep",
            },
        )
        self.assertEqual(ask["code"], 0)
        answer_id = ask["data"]["answer_id"]

        reject = self.api.append_chunk(self.chunk_id, {"answer_id": answer_id, "action": "reject"})
        self.assertEqual(reject["code"], 0)
        self.assertFalse(reject["data"]["applied"])

        accept = self.api.append_chunk(self.chunk_id, {"answer_id": answer_id, "action": "accept"})
        self.assertEqual(accept["code"], 0)
        self.assertTrue(accept["data"]["applied"])

    def test_chat_session_and_message(self):
        session = self.api.create_chat_session(
            {
                "courseware_id": self.courseware_id,
                "page_id": "pg_001",
                "chunk_id": self.chunk_id,
            }
        )
        self.assertEqual(session["code"], 0)
        session_id = session["data"]["id"]

        msg = self.api.send_chat_message({"session_id": session_id, "message": "继续展开"})
        self.assertEqual(msg["code"], 0)
        self.assertIn("reply", msg["data"])

    def test_rewrite_apply_and_undo(self):
        draft = self.api.rewrite_draft(
            "pg_001",
            {"chunk_id": self.chunk_id, "instruction": "改成更直观"},
        )
        self.assertEqual(draft["code"], 0)

        cw = self.api.get_courseware(self.courseware_id)
        version = cw["data"]["current_version"]
        applied = self.api.apply_draft("pg_001", {"draft_id": draft["data"]["draft_id"], "expected_version": version})
        self.assertEqual(applied["code"], 0)

        undone = self.api.undo("pg_001", {"expected_version": applied["data"]["version"]})
        self.assertEqual(undone["code"], 0)

    def test_retrieval_settings_and_profile(self):
        settings = self.api.put_retrieval_settings(
            {
                "web_enabled": True,
                "source_weight": {"doc": 2, "blog": 1, "paper": 1},
                "domain_whitelist": ["arxiv.org"],
                "domain_blacklist": ["bad.example"],
            }
        )
        self.assertEqual(settings["code"], 0)
        self.assertAlmostEqual(settings["data"]["source_weight"]["doc"], 0.5, places=3)

        profile = self.api.put_profile(
            {
                "goal": "三周学会",
                "background": "python",
                "analogy_preference": "technical",
                "mastered_topics": ["softmax"],
                "local_only": True,
            }
        )
        self.assertEqual(profile["code"], 0)
        self.assertTrue(profile["data"]["local_only"])

    def test_logs_and_metrics(self):
        logs = self.api.get_logs()
        self.assertEqual(logs["code"], 0)
        self.assertGreaterEqual(len(logs["data"]), 1)

        metrics = self.api.get_metrics()
        self.assertEqual(metrics["code"], 0)
        self.assertIn("generation", metrics["data"])


if __name__ == "__main__":
    unittest.main()
