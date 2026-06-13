from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from schema_validation import (  # noqa: E402
    SchemaValidationError,
    validate_payload,
)

REVIEW = "schemas/review.schema.json"
REVIEW_DELTA = "schemas/review-delta.schema.json"
ADVERSARIAL = "schemas/adversarial-review.schema.json"


def _valid_review() -> dict:
    return {
        "verdict": "pass",
        "summary": "s",
        "findings": [],
        "verification_gaps": [],
        "acceptance_criteria_assessment": [],
        "confidence": 1.0,
    }


def _valid_delta() -> dict:
    return {
        "verdict": "pass",
        "summary": "s",
        "resolved_findings": [],
        "new_findings": [],
        "regressions": [],
        "affected_acceptance_criteria": [],
        "confidence": 1.0,
    }


def _valid_finding(**over) -> dict:
    base = {
        "id": "F-1",
        "severity": "high",
        "category": "security",
        "file": "a.py",
        "line_start": 3,
        "description": "d",
        "evidence": "e",
        "recommended_fix": "f",
    }
    base.update(over)
    return base


class ValidatorPositiveTests(unittest.TestCase):
    def test_minimal_review_valid(self) -> None:
        validate_payload(_valid_review(), REVIEW)

    def test_review_with_finding_valid(self) -> None:
        payload = _valid_review()
        payload["findings"] = [_valid_finding()]
        payload["acceptance_criteria_assessment"] = [
            {"id": "AC-1", "status": "satisfied", "evidence": "e"}
        ]
        validate_payload(payload, REVIEW)

    def test_minimal_delta_valid(self) -> None:
        validate_payload(_valid_delta(), REVIEW_DELTA)

    def test_null_file_and_line_allowed(self) -> None:
        payload = _valid_review()
        payload["findings"] = [_valid_finding(file=None, line_start=None)]
        validate_payload(payload, REVIEW)


class ValidatorNegativeTests(unittest.TestCase):
    def test_missing_required_top_level_key(self) -> None:
        payload = _valid_delta()
        del payload["new_findings"]
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW_DELTA)
        self.assertIn("new_findings", str(ctx.exception))

    def test_mistyped_array_as_object_rejected(self) -> None:
        payload = _valid_delta()
        payload["new_findings"] = {}
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW_DELTA)
        self.assertIn("new_findings", str(ctx.exception))

    def test_invalid_verdict_enum_rejected(self) -> None:
        payload = _valid_review()
        payload["verdict"] = "looks_fine"
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_invalid_severity_in_nested_finding_rejected(self) -> None:
        payload = _valid_review()
        payload["findings"] = [_valid_finding(severity="catastrophic")]
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW)
        # Error must point inside the nested finding.
        self.assertIn("/findings/0/severity", str(ctx.exception))

    def test_bad_finding_id_pattern_rejected(self) -> None:
        payload = _valid_review()
        payload["findings"] = [_valid_finding(id="bug-7")]
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_nested_finding_missing_required_field_rejected(self) -> None:
        bad = _valid_finding()
        del bad["evidence"]
        payload = _valid_review()
        payload["findings"] = [bad]
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW)
        self.assertIn("/findings/0", str(ctx.exception))

    def test_additional_property_rejected(self) -> None:
        payload = _valid_review()
        payload["unexpected"] = True
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_confidence_out_of_range_rejected(self) -> None:
        payload = _valid_review()
        payload["confidence"] = 1.5
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_bool_for_number_rejected(self) -> None:
        payload = _valid_review()
        payload["confidence"] = True
        with self.assertRaises(SchemaValidationError):
            validate_payload(payload, REVIEW)

    def test_unloadable_schema_fails_closed(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_payload({}, "schemas/does-not-exist.schema.json")

    def test_error_lists_all_violations_sorted(self) -> None:
        payload = _valid_review()
        payload["verdict"] = "nope"
        payload["confidence"] = 2
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_payload(payload, REVIEW)
        msg = str(ctx.exception)
        self.assertIn("/confidence", msg)
        self.assertIn("/verdict", msg)


if __name__ == "__main__":
    unittest.main()
