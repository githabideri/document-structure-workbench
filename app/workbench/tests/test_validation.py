"""Tests for the candidate validation pass (ADR 0003)."""
from unittest.mock import Mock, patch
from xml.dom import minidom  # noqa: F401  (import guard: keep XML deps explicit)

from django.test import TestCase

from workbench.validation import (
    LOW_CONFIDENCE_THRESHOLD,
    VALIDATION_VERSION,
    evaluate_candidate,
    line_confidences,
)


def htr_result(confidences):
    """Build a frozen-contract-shaped HTR result with per-line confidences."""
    lines = [
        {"label": f"line-{i}", "bbox": [0, 0, 1, 1], "text": f"line {i}",
         "candidates": ["line i"], "scores": [value], "confidence": value, "order": i}
        for i, value in enumerate(confidences, start=1)
    ]
    return {
        "id": "remote-1", "status": "succeeded", "pipeline": "test",
        "scope": "region", "text": "\n".join(line["text"] for line in lines),
        "regions": [{"label": "region-1", "bbox": [0, 0, 1, 1], "lines": lines}],
        "diagnostics": {"n_regions": 1, "n_lines": len(lines),
                        "mean_line_confidence": sum(confidences) / len(confidences)},
    }


class LineConfidenceExtractionTests(TestCase):
    def test_extracts_confidences_from_frozen_htr_shape(self):
        self.assertEqual(line_confidences(htr_result([0.9, 0.4])), [0.9, 0.4])

    def test_tolerates_non_contract_shapes(self):
        for raw in (None, [], "nope", {"regions": "no"}, {"regions": [{"lines": None}]},
                    {"regions": [{"lines": [{"confidence": None}]}]},
                    {"regions": [{"lines": [{"confidence": 1.7}]}]}):
            self.assertEqual(line_confidences(raw), [], msg=repr(raw))


class EvaluateCandidateTests(TestCase):
    def test_clean_confident_candidate_needs_no_review(self):
        verdict = evaluate_candidate("Ein gut lesbarer Satz.", htr_result([0.95, 0.9]))
        self.assertFalse(verdict["needs_review"])
        self.assertEqual(verdict["reasons"], [])
        self.assertEqual(verdict["version"], VALIDATION_VERSION)
        self.assertEqual(verdict["confidence"], {"lines": 2, "below_threshold": 0, "mean": 0.925})

    def test_empty_candidate_is_flagged(self):
        verdict = evaluate_candidate("   ")
        self.assertTrue(verdict["needs_review"])
        self.assertIn("empty_candidate", verdict["reasons"])
        self.assertEqual(verdict["counts"]["empty_candidate"], 1)

    def test_editorial_markers_are_counted_and_flagged(self):
        verdict = evaluate_candidate("dominus[?] et [illegible] d.[omi]ni")
        counts = verdict["counts"]
        self.assertEqual(counts["uncertain_marker"], 1)
        self.assertEqual(counts["illegible_marker"], 1)
        self.assertEqual(counts["abbreviation_marker"], 1)
        self.assertIn("uncertain_marker", verdict["reasons"])
        self.assertIn("illegible_marker", verdict["reasons"])
        self.assertNotIn("abbreviation_marker", verdict["reasons"])  # advisory only

    def test_control_and_replacement_characters_are_flagged(self):
        verdict = evaluate_candidate("bad\x01char and \ufffd here")
        self.assertIn("control_characters", verdict["reasons"])
        self.assertIn("replacement_characters", verdict["reasons"])

    def test_low_confidence_lines_flag_review(self):
        verdict = evaluate_candidate("text", htr_result([0.95, 0.2]))
        self.assertTrue(verdict["needs_review"])
        self.assertIn("low_confidence_lines", verdict["reasons"])
        self.assertEqual(verdict["confidence"]["below_threshold"], 1)

    def test_low_mean_confidence_flagged_once(self):
        # Every line low: both line-count and mean reasons appear, deduplicated.
        verdict = evaluate_candidate("text", htr_result([0.2, 0.3]))
        self.assertEqual(
            [r for r in verdict["reasons"] if r.startswith("low_")],
            ["low_confidence_lines", "low_mean_confidence"],
        )

    def test_provider_without_confidence_yields_null_confidence(self):
        verdict = evaluate_candidate("some text", {"response": "some text"})
        self.assertIsNone(verdict["confidence"])
        self.assertFalse(verdict["needs_review"])

    def test_threshold_boundary_is_inclusive(self):
        self.assertGreaterEqual(
            LOW_CONFIDENCE_THRESHOLD,
            max(
                c for c in line_confidences(htr_result([LOW_CONFIDENCE_THRESHOLD]))
            ),
        )


class WorkerPersistenceTests(TestCase):
    """The OCR worker persists the verdict on completion (ADR 0003)."""

    def test_completed_request_gets_validation_metadata(self):
        from workbench.management.commands.run_ocr_worker import Command

        request = Mock()
        request.provider = "openai-compatible"
        request.state = "processing"
        request.metadata = {"remote_id": "x"}
        request.candidate_text = "text with [?] marker"
        request.raw_response = {"response": "raw"}

        command = Command()
        with patch.object(command, "_process_vision"):
            # Simulate a successful provider run.
            request.state = "completed"
            command._process(request)

        self.assertIn("validation", request.metadata)
        self.assertTrue(request.metadata["validation"]["needs_review"])
        self.assertEqual(request.metadata["remote_id"], "x")  # prior metadata preserved
        request.save.assert_called_once()

    def test_failed_request_gets_no_validation_metadata(self):
        from workbench.management.commands.run_ocr_worker import Command

        request = Mock()
        request.provider = "openai-compatible"
        request.metadata = {}
        request.state = "processing"

        command = Command()
        with patch.object(command, "_process_vision", side_effect=RuntimeError("boom")):
            command._process(request)

        self.assertNotIn("validation", request.metadata)
        self.assertEqual(request.state, "failed")
