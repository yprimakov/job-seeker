"""Tests for pipeline/role_fit.py. Run: python -m unittest discover pipeline/tests"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import role_fit  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "role_fit"
CRITERIA = role_fit.load_criteria(role_fit.EXAMPLE_CRITERIA)


def score(name):
    return role_fit.score_posting((FIX / name).read_text(encoding="utf-8"), CRITERIA)


class WorkModeTests(unittest.TestCase):
    def test_modes(self):
        cases = {
            "This is a fully remote role.": ("remote", 0),
            "Hybrid, 2 days a week in the office.": ("hybrid", 2),
            "Expect three days per week on-site.": ("hybrid", 3),
            "You must be in the office 5 days a week.": ("onsite", 5),
            "This is not a remote position.": ("onsite", 5),
            "Hybrid schedule.": ("hybrid", None),
            "Great team, great snacks.": ("unknown", None),
        }
        for text, want in cases.items():
            self.assertEqual(role_fit.detect_work_mode(text), want, text)


class CompTests(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(role_fit.detect_comp("$170,000 - $210,000 base"), (170000, 210000))
        self.assertEqual(role_fit.detect_comp("Salary range: $85K-$95K."), (85000, 95000))
        self.assertEqual(role_fit.detect_comp("$60 - $80 per hour"), (60 * 2080, 80 * 2080))
        self.assertEqual(role_fit.detect_comp("$150k to $180k"), (150000, 180000))
        self.assertIsNone(role_fit.detect_comp("Pay is not listed. $500 home office stipend."))


class ScoreTests(unittest.TestCase):
    def test_strong_remote(self):
        r = score("strong_remote.txt")
        self.assertGreaterEqual(r.score, 85)
        self.assertEqual(r.verdict, "strong fit")
        self.assertEqual(r.red_flags, [])

    def test_onsite_five_days_flags(self):
        r = score("onsite_five_days.txt")
        self.assertLess(r.score, 50)
        self.assertEqual(r.verdict, "weak fit")
        self.assertIn("In office 5 days a week", r.red_flags)
        self.assertIn("Security clearance required", r.red_flags)
        self.assertTrue(any("mainframe" in f for f in r.red_flags))
        self.assertTrue(any("under your" in f for f in r.red_flags))

    def test_hybrid_over_max_days(self):
        r = score("hybrid_three_days.txt")
        self.assertTrue(any("3 office days" in f for f in r.red_flags))
        self.assertTrue(50 <= r.score < 75, r.score)

    def test_score_bounds_and_empty_criteria(self):
        r = role_fit.score_posting("", {})
        self.assertTrue(0 <= r.score <= 100)

    def test_ordering(self):
        a, b, c = score("strong_remote.txt"), score("hybrid_three_days.txt"), score("onsite_five_days.txt")
        self.assertGreater(a.score, b.score)
        self.assertGreater(b.score, c.score)

    def test_cli_json(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            role_fit.main(["--jd", str(FIX / "strong_remote.txt"), "--criteria", str(role_fit.EXAMPLE_CRITERIA), "--json"])
        out = json.loads(buf.getvalue())
        self.assertIn("score", out)
        self.assertIn("red_flags", out)


if __name__ == "__main__":
    unittest.main()
