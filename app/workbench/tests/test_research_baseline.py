import json
import os

from django.test import SimpleTestCase

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "chat", "research_baseline_cases.json")

# Every case records these Phase-0 characterization dimensions so a given
# question can be re-run across retriever variants and compared.
REQUIRED_RECORD_FIELDS = {
    "question", "scope", "attached_documents", "retrieval_queries",
    "retrieval_result_count", "new_evidence_count", "evidence_markers",
    "provider_calls", "prompt_tokens", "run_duration_ms", "final_answer",
    "citations", "useful_by_inspection",
}
META_FIELDS = {"id", "category", "category_label"}


class ResearchBaselineSetTests(SimpleTestCase):
    def setUp(self):
        with open(FIXTURE, encoding="utf-8") as handle:
            self.data = json.load(handle)

    def test_fixture_is_a_non_empty_eval_set_of_8_to_15_cases(self):
        cases = self.data["cases"]
        self.assertGreaterEqual(len(cases), 8)
        self.assertLessEqual(len(cases), 15)

    def test_case_ids_are_unique_and_all_ten_categories_are_covered(self):
        cases = self.data["cases"]
        ids = [case["id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        categories = sorted(case["category"] for case in cases)
        self.assertEqual(categories, list(range(1, 11)))

    def test_every_case_has_a_question_and_a_scope_and_required_record_fields(self):
        for case in self.data["cases"]:
            self.assertTrue(str(case["question"]).strip())
            self.assertTrue(case["scope"])
            self.assertIsInstance(case["attached_documents"], list)
            expected = REQUIRED_RECORD_FIELDS | META_FIELDS
            self.assertSetEqual(set(case.keys()), expected, msg=case["id"])

    def test_every_case_declares_a_category_label(self):
        for case in self.data["cases"]:
            self.assertTrue(str(case["category_label"]).strip(), msg=case["id"])
