"""The role fit hook in process_queue.py: off by default, skips low scores when on."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import process_queue  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "role_fit"


class FitHookTests(unittest.TestCase):
    def run_queue(self, env, jd_file, skip_below):
        items = [{"id": "1", "url": "https://example.com/job", "status": "pending", "title": ""}]
        crit = json.loads((process_queue.PIPELINE_DIR / "role_fit_criteria.example.json").read_text())
        crit["skip_below"] = skip_below
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(crit, f)
        env = {**env, "ROLE_FIT_CRITERIA": f.name}
        ran = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(process_queue, "read_queue", return_value=items), \
             mock.patch.object(process_queue, "write_queue"), \
             mock.patch.object(process_queue, "fetch_jd_http", return_value=(FIX / jd_file).read_text() * 2), \
             mock.patch.object(process_queue, "_find_newest_app_folder", return_value=None), \
             mock.patch.object(process_queue.subprocess, "run", ran), \
             mock.patch("builtins.print"):
            process_queue.main()
        os.unlink(f.name)
        return items[0], ran

    def test_off_by_default(self):
        os.environ.pop("ROLE_FIT_ENABLED", None)
        item, ran = self.run_queue({}, "onsite_five_days.txt", 50)
        self.assertNotIn("fit", item)
        self.assertTrue(ran.called)
        self.assertEqual(item["status"], "ready")

    def test_skips_below_threshold(self):
        item, ran = self.run_queue({"ROLE_FIT_ENABLED": "1"}, "onsite_five_days.txt", 50)
        self.assertFalse(ran.called)
        self.assertEqual(item["status"], "failed")
        self.assertTrue(item["fitSkipped"])
        self.assertIn("In office 5 days a week", item["fit"]["redFlags"])

    def test_scores_and_continues_above_threshold(self):
        item, ran = self.run_queue({"ROLE_FIT_ENABLED": "1"}, "strong_remote.txt", 50)
        self.assertTrue(ran.called)
        self.assertEqual(item["status"], "ready")
        self.assertGreaterEqual(item["fit"]["score"], 85)


if __name__ == "__main__":
    unittest.main()
