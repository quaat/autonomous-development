"""Unit tests for token-efficiency and adaptive-rigor helpers (pure functions)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import controller  # noqa: E402
import state  # noqa: E402


class PhaseProfileTests(unittest.TestCase):
    def test_default_profiles(self) -> None:
        self.assertEqual(controller.resolve_phase_profile("plan")["reasoning"], "high")
        self.assertEqual(
            controller.resolve_phase_profile("adversarial")["reasoning"], "xhigh"
        )

    def test_env_override(self) -> None:
        os.environ["CLAUDE_AUTONOMOUS_PHASE_PROFILES"] = (
            '{"plan": {"reasoning": "low"}}'
        )
        os.environ["CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN"] = "local-model"
        try:
            profile = controller.resolve_phase_profile("plan")
        finally:
            del os.environ["CLAUDE_AUTONOMOUS_PHASE_PROFILES"]
            del os.environ["CLAUDE_AUTONOMOUS_CODEX_MODEL_PLAN"]
        self.assertEqual(profile["reasoning"], "low")
        self.assertEqual(profile["model"], "local-model")

    def test_profile_args(self) -> None:
        args = controller.codex_profile_args(
            {"reasoning": "high", "verbosity": "low", "reasoning_summary": "none"}
        )
        self.assertEqual(
            args,
            [
                "-c",
                "model_reasoning_effort=high",
                "-c",
                "model_reasoning_summary=none",
                "-c",
                "model_verbosity=low",
            ],
        )

    def test_profile_args_with_model(self) -> None:
        args = controller.codex_profile_args({"reasoning": "high", "model": "m"})
        self.assertIn("--model", args)
        self.assertIn("m", args)


class UsageParseTests(unittest.TestCase):
    def test_parse_tokens_from_info(self) -> None:
        ndjson = (
            '{"type": "item"}\n'
            "garbage line\n"
            '{"type": "token_count", "info": {"input_tokens": 100, '
            '"output_tokens": 50, "total_tokens": 150}}\n'
        )
        usage = controller.parse_codex_usage(ndjson)
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 50)
        self.assertEqual(usage["total_tokens"], 150)

    def test_parse_empty(self) -> None:
        self.assertEqual(controller.parse_codex_usage(""), {})

    def test_parse_model_from_session_event(self) -> None:
        ndjson = (
            '{"msg": {"type": "session_configured", "model": "gpt-x-test"}}\n'
            '{"type": "token_count", "info": {"input_tokens": 1}}\n'
        )
        self.assertEqual(controller.parse_codex_model(ndjson), "gpt-x-test")

    def test_parse_model_absent(self) -> None:
        self.assertIsNone(controller.parse_codex_model('{"type": "item"}\n'))


class ProcessTimeoutTests(unittest.TestCase):
    def test_resolve_timeout_default_and_overrides(self) -> None:
        os.environ.pop("CLAUDE_AUTONOMOUS_PROCESS_TIMEOUT", None)
        self.assertEqual(
            controller._resolve_process_timeout(),
            controller.DEFAULT_PROCESS_TIMEOUT_SECONDS,
        )
        os.environ["CLAUDE_AUTONOMOUS_PROCESS_TIMEOUT"] = "0"
        try:
            self.assertIsNone(controller._resolve_process_timeout())
            os.environ["CLAUDE_AUTONOMOUS_PROCESS_TIMEOUT"] = "12.5"
            self.assertEqual(controller._resolve_process_timeout(), 12.5)
        finally:
            del os.environ["CLAUDE_AUTONOMOUS_PROCESS_TIMEOUT"]

    def test_timeout_returns_failed_result(self) -> None:
        result = controller.run_process(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=ROOT,
            timeout=0.2,
        )
        self.assertEqual(result.returncode, controller.PROCESS_TIMEOUT_EXIT_CODE)
        self.assertIn("timed out", result.stderr)

    def test_timeout_with_check_raises(self) -> None:
        with self.assertRaises(controller.WorkflowError):
            controller.run_process(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                cwd=ROOT,
                timeout=0.2,
                check=True,
            )


class ModeTests(unittest.TestCase):
    def test_auto_escalates_on_risk(self) -> None:
        effective, reasons = controller.select_mode("auto", "Add Stripe billing payments")
        self.assertEqual(effective, "rigorous")
        self.assertTrue(reasons)

    def test_auto_standard_when_low_risk(self) -> None:
        effective, _ = controller.select_mode("auto", "Rename a button label")
        self.assertEqual(effective, "standard")

    def test_explicit_rigorous_respected(self) -> None:
        effective, _ = controller.select_mode("rigorous", "trivial")
        self.assertEqual(effective, "rigorous")

    def test_explicit_lean_not_escalated(self) -> None:
        # Explicit modes are respected verbatim even if risk words are present.
        effective, _ = controller.select_mode("lean", "auth login change")
        self.assertEqual(effective, "lean")

    def test_classify_risk_categories(self) -> None:
        self.assertIn(
            "persistence/migration",
            controller.classify_feature_risk("Migrate the database schema"),
        )
        self.assertIn("auth/authz", controller.classify_feature_risk("add login auth"))


class ReviewMergeTests(unittest.TestCase):
    def test_full_then_delta_merge(self) -> None:
        st: dict = {"cumulative_findings": []}
        full = {
            "findings": [
                {"id": "F-1", "severity": "high", "category": "correctness"},
                {"id": "F-2", "severity": "low", "category": "testing"},
            ]
        }
        controller.merge_full_review(st, full, 1)
        severe = controller.cumulative_unresolved_severe(st)
        self.assertEqual([f["id"] for f in severe], ["F-1"])

        delta = {
            "resolved_findings": ["F-1"],
            "new_findings": [
                {"id": "F-3", "severity": "critical", "category": "security"}
            ],
            "regressions": [],
        }
        controller.merge_delta_review(st, delta, 2)
        severe = controller.cumulative_unresolved_severe(st)
        self.assertEqual([f["id"] for f in severe], ["F-3"])

    def test_id_reuse_does_not_erase_open_finding(self) -> None:
        st: dict = {"cumulative_findings": []}
        controller.merge_full_review(
            st, {"findings": [{"id": "F-1", "severity": "critical"}]}, 1
        )
        # A later round reuses F-1 for a different low-severity note.
        controller.merge_delta_review(
            st,
            {
                "resolved_findings": [],
                "new_findings": [{"id": "F-1", "severity": "low"}],
                "regressions": [],
            },
            2,
        )
        severe = controller.cumulative_unresolved_severe(st)
        # The original open critical must still block completion.
        self.assertEqual([f["id"] for f in severe], ["F-1"])
        ids = {f["id"] for f in st["cumulative_findings"]}
        self.assertIn("F-1#r2-0", ids)

    def test_full_review_duplicate_id_does_not_drop_severe(self) -> None:
        # The review schema enforces id *format* but not uniqueness, so a
        # round-1 full review can return two findings sharing an id. Overwriting
        # by id would silently drop the first — here a critical — from the
        # seeded baseline (fail open). Both must be preserved so the severe one
        # still blocks completion.
        st: dict = {"cumulative_findings": []}
        full = {
            "findings": [
                {"id": "F-1", "severity": "critical", "category": "security"},
                {"id": "F-1", "severity": "low", "category": "style"},
            ]
        }
        controller.merge_full_review(st, full, 1)
        # Both findings survive (no silent overwrite).
        self.assertEqual(len(st["cumulative_findings"]), 2)
        # The critical is still present and still blocks the gate.
        severe = controller.cumulative_unresolved_severe(st)
        self.assertEqual(len(severe), 1)
        self.assertEqual(severe[0]["severity"], "critical")
        # The collision is recorded under a collision-safe id that preserves the
        # reused id for audit.
        collision = [
            f for f in st["cumulative_findings"] if f["id"] != "F-1"
        ]
        self.assertEqual(len(collision), 1)
        self.assertEqual(collision[0]["reused_id"], "F-1")
        self.assertTrue(collision[0]["id"].startswith("F-1#dup"))

    def test_full_review_triple_duplicate_id_all_preserved(self) -> None:
        # Three findings sharing one id must all survive with distinct ids so a
        # single overwrite cannot collapse multiple severe findings into one.
        st: dict = {"cumulative_findings": []}
        full = {
            "findings": [
                {"id": "F-1", "severity": "critical"},
                {"id": "F-1", "severity": "high"},
                {"id": "F-1", "severity": "high"},
            ]
        }
        controller.merge_full_review(st, full, 1)
        ids = [f["id"] for f in st["cumulative_findings"]]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        severe = controller.cumulative_unresolved_severe(st)
        self.assertEqual(len(severe), 3)

    def test_malformed_ledger_entry_fails_closed(self) -> None:
        # A non-dict ledger entry has no readable status/severity. The gate must
        # treat it as an unresolved severe finding rather than silently skipping
        # it, so a corrupted cumulative ledger cannot clear completion.
        st = {
            "cumulative_findings": [
                "not-a-dict",
                {"id": "F-1", "status": "open", "severity": "high"},
            ]
        }
        severe = controller.cumulative_unresolved_severe(st)
        self.assertEqual(len(severe), 2)
        self.assertTrue(any(f.get("id") == "(malformed)" for f in severe))


class DeltaContradictionTests(unittest.TestCase):
    def test_resolved_and_reintroduced_same_round_keeps_blocking(self) -> None:
        st: dict = {"cumulative_findings": []}
        controller.merge_full_review(
            st, {"findings": [{"id": "F-1", "severity": "high"}]}, 1
        )
        # Contradictory delta: F-1 is both resolved and reintroduced as low.
        controller.merge_delta_review(
            st,
            {
                "resolved_findings": ["F-1"],
                "new_findings": [{"id": "F-1", "severity": "low"}],
                "regressions": [],
            },
            2,
        )
        # The original high finding must remain open and blocking.
        severe = controller.cumulative_unresolved_severe(st)
        self.assertEqual([f["id"] for f in severe], ["F-1"])
        index = {f["id"]: f for f in st["cumulative_findings"]}
        self.assertEqual(index["F-1"]["severity"], "high")
        self.assertEqual(index["F-1"]["status"], "open")
        self.assertIn("F-1#r2-0", index)

    def test_multiple_reused_ids_one_round_do_not_overwrite(self) -> None:
        # Two delta entries reusing the same id in one round must each get a
        # distinct collision-safe key. A bare `#r<round>` key would make the
        # second overwrite the first — and if the first is a critical and the
        # second a low, the severe finding would vanish (fail open).
        st: dict = {"cumulative_findings": []}
        controller.merge_full_review(
            st, {"findings": [{"id": "F-1", "severity": "high"}]}, 1
        )
        controller.merge_delta_review(
            st,
            {
                "resolved_findings": ["F-1"],
                "new_findings": [
                    {"id": "F-1", "severity": "critical"},
                    {"id": "F-1", "severity": "low"},
                ],
                "regressions": [],
            },
            2,
        )
        # Both reused-id reports survive under distinct keys.
        ids = [f["id"] for f in st["cumulative_findings"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("F-1#r2-0", ids)
        self.assertIn("F-1#r2-1", ids)
        # The reintroduced critical still blocks the gate.
        severe = controller.cumulative_unresolved_severe(st)
        self.assertTrue(any(f["severity"] == "critical" for f in severe))


class SchemaTypeValidationTests(unittest.TestCase):
    def test_mistyped_array_field_detected(self) -> None:
        # new_findings as an object (not array) must be flagged.
        bad = {
            "verdict": "pass",
            "summary": "s",
            "resolved_findings": [],
            "new_findings": {},
            "regressions": [],
            "affected_acceptance_criteria": [],
            "confidence": 1.0,
        }
        violations = controller._schema_type_violations(
            bad, "schemas/review-delta.schema.json"
        )
        self.assertIn("new_findings", violations)

    def test_well_typed_payload_has_no_violations(self) -> None:
        good = {
            "verdict": "pass",
            "summary": "s",
            "resolved_findings": [],
            "new_findings": [],
            "regressions": [],
            "affected_acceptance_criteria": [],
            "confidence": 1.0,
        }
        self.assertEqual(
            controller._schema_type_violations(
                good, "schemas/review-delta.schema.json"
            ),
            [],
        )


class SourceSectionValidationTests(unittest.TestCase):
    def test_blank_spec_source_rejected(self) -> None:
        with self.assertRaises(controller.WorkflowError):
            controller.materialize_acceptance("spec", {"title": "x"}, {})

    def test_blank_plan_source_rejected(self) -> None:
        with self.assertRaises(controller.WorkflowError):
            controller.materialize_acceptance("plan", {"summary": "x"}, {})

    def test_nondict_fr_items_rejected_not_silently_dropped(self) -> None:
        # A non-empty functional_requirements list whose items are bare strings
        # passes the length check but _apply_decisions_to_items would silently
        # drop every entry, materializing an empty FR section and weakening
        # downstream review against a blank contract. Must fail closed.
        source = {
            "title": "x",
            "functional_requirements": ["bogus", "alsobogus"],
            "acceptance_criteria": [{"id": "AC-1", "criterion": "ok"}],
        }
        with self.assertRaises(controller.WorkflowError):
            controller.materialize_acceptance("spec", source, {})

    def test_nondict_ac_items_rejected_not_silently_dropped(self) -> None:
        source = {
            "title": "x",
            "functional_requirements": [{"id": "FR-1", "requirement": "ok"}],
            "acceptance_criteria": ["bogus"],
        }
        with self.assertRaises(controller.WorkflowError):
            controller.materialize_acceptance("spec", source, {})

    def test_nondict_plan_steps_rejected_not_silently_dropped(self) -> None:
        source = {"summary": "x", "implementation_steps": ["bogus"]}
        with self.assertRaises(controller.WorkflowError):
            controller.materialize_acceptance("plan", source, {})

    def test_wellformed_spec_with_optional_ac_absent_still_materializes(self) -> None:
        # Item-shape validation must not newly forbid a valid spec that omits the
        # optional acceptance_criteria section.
        source = {
            "title": "x",
            "functional_requirements": [{"id": "FR-1", "requirement": "do it"}],
        }
        obj, _ = controller.materialize_acceptance("spec", source, {})
        self.assertEqual(len(obj["functional_requirements"]), 1)


class CodexValidationTests(unittest.TestCase):
    def test_missing_required_fields_detected(self) -> None:
        missing = controller._missing_required_fields(
            {"verdict": "pass"}, "schemas/review-delta.schema.json"
        )
        self.assertIn("summary", missing)
        self.assertIn("new_findings", missing)

    def test_unreadable_schema_fails_closed(self) -> None:
        with self.assertRaises(controller.WorkflowError):
            controller._missing_required_fields(
                {"verdict": "pass"}, "schemas/does-not-exist.schema.json"
            )

    def test_complete_output_has_no_missing_fields(self) -> None:
        complete = {
            "verdict": "pass",
            "summary": "s",
            "resolved_findings": [],
            "new_findings": [],
            "regressions": [],
            "affected_acceptance_criteria": [],
            "confidence": 1.0,
        }
        self.assertEqual(
            controller._missing_required_fields(
                complete, "schemas/review-delta.schema.json"
            ),
            [],
        )


class DecisionValidationTests(unittest.TestCase):
    def test_unknown_decision_id_rejected(self) -> None:
        source = {
            "title": "T",
            "functional_requirements": [{"id": "FR-1", "requirement": "r"}],
            "acceptance_criteria": [{"id": "AC-1", "criterion": "c"}],
        }
        with self.assertRaises(controller.WorkflowError):
            controller.materialize_acceptance(
                "spec", source, {"modify": [{"id": "AC-21", "replacement": "x"}]}
            )

    def test_malformed_reject_directive_rejected(self) -> None:
        source = {
            "title": "T",
            "functional_requirements": [{"id": "FR-3", "requirement": "r"}],
            "acceptance_criteria": [],
        }
        # `reject` given as a bare string instead of a list of objects must fail
        # closed rather than being silently ignored.
        with self.assertRaises(controller.WorkflowError):
            controller.materialize_acceptance("spec", source, {"reject": "FR-3"})

    def test_modify_without_replacement_rejected(self) -> None:
        source = {
            "title": "T",
            "functional_requirements": [{"id": "FR-1", "requirement": "orig"}],
            "acceptance_criteria": [],
        }
        # A modify directive lacking `replacement` would blank the item text.
        with self.assertRaises(controller.WorkflowError):
            controller.materialize_acceptance(
                "spec", source, {"modify": [{"id": "FR-1"}]}
            )

    def test_known_decision_ids_accepted(self) -> None:
        source = {
            "title": "T",
            "functional_requirements": [{"id": "FR-1", "requirement": "r"}],
            "acceptance_criteria": [{"id": "AC-1", "criterion": "c"}],
        }
        obj, _ = controller.materialize_acceptance(
            "spec", source, {"reject": [{"id": "FR-1", "reason": "x"}]}
        )
        self.assertEqual(obj["functional_requirements"], [])


class TriageGateTests(unittest.TestCase):
    def test_rejected_finding_unblocks_cumulative_gate(self) -> None:
        st: dict = {"cumulative_findings": []}
        full = {
            "findings": [
                {"id": "F-1", "severity": "high", "category": "correctness"},
            ]
        }
        controller.merge_full_review(st, full, 1)
        self.assertEqual(
            [f["id"] for f in controller.cumulative_unresolved_severe(st)], ["F-1"]
        )
        # A validly rejected high finding (with recorded evidence) releases the
        # completion gate.
        controller.apply_triage_to_cumulative(
            st,
            [
                {
                    "fingerprint": "a:b:c",
                    "finding_id": "F-1",
                    "status": "rejected",
                    "reason": "Repository formatter enforces current style",
                }
            ],
        )
        self.assertEqual(controller.cumulative_unresolved_severe(st), [])

    def test_severe_rejection_without_rationale_still_blocks(self) -> None:
        st: dict = {"cumulative_findings": []}
        controller.merge_full_review(
            st, {"findings": [{"id": "F-1", "severity": "high"}]}, 1
        )
        # Rejecting a severe finding by status metadata alone must not release it.
        controller.apply_triage_to_cumulative(
            st, [{"finding_id": "F-1", "status": "rejected"}]
        )
        self.assertEqual(
            [f["id"] for f in controller.cumulative_unresolved_severe(st)], ["F-1"]
        )

    def test_reopen_transition_reblocks_closed_finding(self) -> None:
        st: dict = {"cumulative_findings": []}
        controller.merge_full_review(
            st, {"findings": [{"id": "F-1", "severity": "high"}]}, 1
        )
        controller.apply_triage_to_cumulative(
            st, [{"finding_id": "F-1", "status": "rejected", "reason": "style"}]
        )
        self.assertEqual(controller.cumulative_unresolved_severe(st), [])
        # A later reclassification must re-block the previously closed finding.
        controller.apply_triage_to_cumulative(
            st, [{"finding_id": "F-1", "status": "requires_human_decision"}]
        )
        self.assertEqual(
            [f["id"] for f in controller.cumulative_unresolved_severe(st)], ["F-1"]
        )

    def test_missing_severity_fails_closed(self) -> None:
        st: dict = {"cumulative_findings": []}
        # A delta finding that omits severity must be treated as blocking.
        controller.merge_delta_review(
            st,
            {
                "resolved_findings": [],
                "new_findings": [{"id": "F-9"}],
                "regressions": [],
            },
            2,
        )
        self.assertEqual(
            [f["id"] for f in controller.cumulative_unresolved_severe(st)], ["F-9"]
        )

    def test_requires_human_decision_still_blocks(self) -> None:
        st: dict = {"cumulative_findings": []}
        controller.merge_full_review(
            st, {"findings": [{"id": "F-1", "severity": "critical"}]}, 1
        )
        controller.apply_triage_to_cumulative(
            st,
            [{"finding_id": "F-1", "status": "requires_human_decision"}],
        )
        self.assertEqual(
            [f["id"] for f in controller.cumulative_unresolved_severe(st)], ["F-1"]
        )

    def test_malformed_new_finding_fails_closed(self) -> None:
        """A new_findings item missing an id must block the merge (fail closed)
        rather than be silently dropped (which would lose a blocking finding)."""
        st: dict = {"cumulative_findings": []}
        with self.assertRaises(controller.WorkflowError):
            controller.merge_delta_review(
                st,
                {
                    "resolved_findings": [],
                    "new_findings": [{"severity": "high"}],
                    "regressions": [],
                },
                2,
            )

    def test_malformed_full_finding_fails_closed(self) -> None:
        st: dict = {"cumulative_findings": []}
        with self.assertRaises(controller.WorkflowError):
            controller.merge_full_review(
                st, {"findings": [{"id": "F-1"}, "not-a-dict"]}, 1
            )

    def test_open_findings_render_exposes_ids(self) -> None:
        """The delta-review context must surface open findings' `F-<n>` ids so
        the reviewer can reference them in `resolved_findings`."""
        st: dict = {"cumulative_findings": []}
        controller.merge_full_review(
            st,
            {
                "findings": [
                    {"id": "F-1", "severity": "high"},
                    {"id": "F-2", "severity": "low"},
                ]
            },
            1,
        )
        controller.merge_delta_review(
            st, {"resolved_findings": ["F-2"], "new_findings": [], "regressions": []}, 2
        )
        rendered = controller.render_open_findings(st)
        self.assertIn("F-1", rendered)
        # F-2 was resolved, so it is no longer an open finding to reference.
        self.assertNotIn("F-2", rendered)
        self.assertEqual(controller.render_open_findings({}), "(none)")

    def test_open_findings_render_includes_identity_detail(self) -> None:
        """Open findings surfaced to the delta reviewer must carry enough
        identity (category + round) to map them reliably, not just the id."""
        st: dict = {"cumulative_findings": []}
        controller.merge_full_review(
            st,
            {"findings": [{"id": "F-1", "severity": "high", "category": "security"}]},
            1,
        )
        rendered = controller.render_open_findings(st)
        self.assertIn("security", rendered)
        self.assertIn("\"round\"", rendered)

    def test_missing_status_severe_finding_fails_closed(self) -> None:
        # A severe finding without an explicit released status (e.g. a migrated
        # entry missing "status") must keep blocking the gate, not slip through.
        st = {
            "cumulative_findings": [
                {"id": "F-1", "severity": "critical"},
                {"id": "F-2", "severity": "high", "status": "resolved"},
                {"id": "F-3", "severity": "high", "status": "rejected"},
            ]
        }
        severe = controller.cumulative_unresolved_severe(st)
        self.assertEqual([f["id"] for f in severe], ["F-1"])


class AcceptanceMaterializeTests(unittest.TestCase):
    def test_spec_materialization(self) -> None:
        source = {
            "title": "T",
            "problem_statement": "P",
            "functional_requirements": [
                {"id": "FR-1", "requirement": "orig", "priority": "must"},
                {"id": "FR-2", "requirement": "drop me", "priority": "should"},
            ],
            "acceptance_criteria": [{"id": "AC-1", "criterion": "c"}],
        }
        decisions = {
            "accept": ["FR-1"],
            "reject": [{"id": "FR-2", "reason": "scope"}],
            "modify": [{"id": "FR-1", "replacement": "newtext"}],
            "add": [],
        }
        obj, md = controller.materialize_acceptance("spec", source, decisions)
        ids = [fr["id"] for fr in obj["functional_requirements"]]
        self.assertEqual(ids, ["FR-1"])
        self.assertEqual(obj["functional_requirements"][0]["requirement"], "newtext")
        self.assertEqual(obj["rejected"], [{"id": "FR-2", "reason": "scope"}])
        self.assertIn("newtext", md)
        self.assertIn("AC-1", md)

    def test_plan_materialization(self) -> None:
        source = {
            "summary": "S",
            "implementation_steps": [
                {"order": 1, "description": "a"},
                {"order": 2, "description": "b"},
            ],
        }
        decisions = {"reject": [{"id": "S2", "reason": "later"}]}
        obj, md = controller.materialize_acceptance("plan", source, decisions)
        self.assertEqual(len(obj["implementation_steps"]), 1)
        self.assertEqual(obj["implementation_steps"][0]["id"], "S1")


class RepositoryManifestTests(unittest.TestCase):
    def test_manifest_sections(self) -> None:
        files = [
            "CLAUDE.md",
            "scripts/controller.py",
            "tests/test_x.py",
            "pyproject.toml",
            ".github/workflows/ci.yml",
            "src/app/main.py",
        ]
        manifest = state.build_repository_manifest(files)
        self.assertEqual(manifest["instructions"], ["CLAUDE.md"])
        self.assertEqual(manifest["build_manifests"], ["pyproject.toml"])
        self.assertEqual(manifest["primary_modules"], ["scripts", "src"])
        self.assertEqual(manifest["test_roots"], ["tests"])
        self.assertEqual(manifest["ci"], [".github/workflows/ci.yml"])


class CompactContextTests(unittest.TestCase):
    def test_compact_verification_view(self) -> None:
        st = {
            "verification": {
                "checks": [
                    {"name": "t", "command": ["pytest"], "exit_code": 1, "log": "a"},
                    {"name": "t", "command": ["pytest"], "exit_code": 0, "log": "b"},
                ]
            }
        }
        view = controller.compact_verification_view(st)
        self.assertEqual(view, [{"name": "t", "command": ["pytest"], "exit_code": 0}])

    def test_finding_ledger_render(self) -> None:
        st = {
            "review_ledger": [
                {"fingerprint": "a:b:c", "status": "rejected", "reason": "style"}
            ]
        }
        rendered = controller.render_finding_ledger(st)
        self.assertIn("a:b:c", rendered)
        self.assertIn("rejected", rendered)
        self.assertEqual(controller.render_finding_ledger({}), "(none)")


if __name__ == "__main__":
    unittest.main()
