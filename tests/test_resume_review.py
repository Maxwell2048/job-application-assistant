import unittest
from local_llm import LLMError
from resume_review import review_projects

class Reviewer:
    def __init__(self, order): self.order = order
    def generate_json(self, *args, **kwargs):
        if self.order is None: raise LLMError("offline", error_type="service")
        return {"order": self.order, "reason": "Relevant projects first"}

class ResumeReviewTests(unittest.TestCase):
    def setUp(self):
        self.sections = [{"title": "Projects", "lines": [
            {"kind":"heading", "text":"A"}, {"kind":"bullet", "text":"Alpha", "basis":[{"quote":"source"}]},
            {"kind":"heading", "text":"B"}, {"kind":"bullet", "text":"Beta", "basis":[{"quote":"source2"}]}]}]
    def test_reorders_blocks_without_changing_evidence(self):
        out, notes = review_projects(Reviewer(["P2", "P1"]), self.sections, "Python")
        self.assertEqual([x["text"] for x in out[0]["lines"]], ["B", "Beta", "A", "Alpha"])
        self.assertEqual(out[0]["lines"][1]["basis"], [{"quote":"source2"}])
        self.assertEqual(self.sections[0]["lines"][0]["text"], "A")
    def test_invalid_or_failed_review_preserves_all_lines(self):
        for order in (["P2", "P2"], ["P1"], None):
            out, notes = review_projects(Reviewer(order), self.sections, "Python")
            self.assertEqual(out, self.sections)
            self.assertIn("original order retained", notes[0])
