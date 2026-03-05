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


class TestMarkdownFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.api = AriadneAPI()

    def test_generate_should_create_local_markdown_doc(self):
        res = self.api.generate_courseware(
            {
                "topic": "RAG",
                "keywords": ["retrieval", "embedding"],
                "difficulty": "beginner",
                "style": "engineering",
                "template": "tutorial",
            }
        )
        self.assertEqual(res["code"], 0)
        cw_id = res["data"]["courseware_id"]

        cw = self.api.get_courseware(cw_id)
        self.assertEqual(cw["code"], 0)
        path = Path(cw["data"]["knowledge_doc_path"])
        self.assertTrue(path.exists())
        self.assertIn("# RAG", path.read_text(encoding="utf-8"))

    def test_html_should_render_from_markdown(self):
        res = self.api.generate_courseware(
            {
                "topic": "Agent",
                "keywords": ["tool", "planning"],
                "difficulty": "beginner",
                "style": "engineering",
                "template": "tutorial",
            }
        )
        cw_id = res["data"]["courseware_id"]

        md = self.api.get_markdown(cw_id)
        self.assertEqual(md["code"], 0)
        updated_text = md["data"]["markdown"] + "\n## 自定义章节\n这是手工追加内容。\n"
        put_md = self.api.put_markdown(cw_id, {"markdown": updated_text})
        self.assertEqual(put_md["code"], 0)

        html_payload = self.api.export_html_content(cw_id)
        self.assertEqual(html_payload["code"], 0)
        html = html_payload["data"]["html"]
        self.assertIn("自定义章节", html)
        self.assertIn("这是手工追加内容", html)


if __name__ == "__main__":
    unittest.main()
