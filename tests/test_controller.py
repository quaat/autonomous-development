from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts/controller.py"
STOP_GATE = ROOT / "scripts/stop_gate.py"

# Make state module importable for helpers
sys.path.insert(0, str(ROOT / "scripts"))
import argparse  # noqa: E402

import controller  # noqa: E402
from state import find_active_runs, resolve_repository  # noqa: E402


class ControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._tmpdirs:
            if d.exists():
                shutil.rmtree(str(d), ignore_errors=True)

    def make_repo(self) -> Path:
        temp = Path(tempfile.mkdtemp())
        self._tmpdirs.append(temp)
        subprocess.run(["git", "init", "-q", str(temp)], check=True)
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.name", "Test User"], check=True
        )
        (temp / "README.md").write_text("# Test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(temp), "commit", "-qm", "initial"], check=True)
        return temp

    def make_state_home(self) -> Path:
        """Create a temporary directory for state storage."""
        d = Path(tempfile.mkdtemp())
        self._tmpdirs.append(d)
        return d

    def run_controller(
        self, repo: Path, *args: str, state_home: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["python3", str(CONTROLLER), "--project-root", str(repo)]
        if state_home is not None:
            cmd += ["--state-dir", str(state_home)]
        cmd += list(args)
        return subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _find_state_path(self, repo: Path, state_home: Path) -> Path:
        """Locate run-state.json for the single active run in state_home."""
        repo_info = resolve_repository(repo)
        active = find_active_runs(state_home, repo_info.id)
        if not active:
            raise AssertionError(
                f"No active runs found in {state_home} for repo {repo_info.id}"
            )
        return active[0].run_dir / "run-state.json"

    def test_init_and_status(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()

        result = self.run_controller(
            repo, "init", "--feature", "Add a test feature", state_home=state_home
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        state_path = self._find_state_path(repo, state_home)
        self.assertTrue(state_path.exists(), f"State file not found at {state_path}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["feature"], "Add a test feature")

        status = self.run_controller(repo, "status", state_home=state_home)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("Phase: initialized", status.stdout)

    def test_record_passing_check(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()

        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Feature", state_home=state_home
            ).returncode,
            0,
        )
        result = self.run_controller(
            repo,
            "run-check",
            "--name",
            "truth",
            "--",
            "python3",
            "-c",
            'print("ok")',
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        state_path = self._find_state_path(repo, state_home)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["verification"]["passed"])

    def test_rerun_supersedes_failed_check(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()

        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Feature", state_home=state_home
            ).returncode,
            0,
        )
        failed = self.run_controller(
            repo,
            "run-check",
            "--name",
            "tests",
            "--",
            "python3",
            "-c",
            "raise SystemExit(1)",
            state_home=state_home,
        )
        self.assertEqual(failed.returncode, 1)
        passed = self.run_controller(
            repo,
            "run-check",
            "--name",
            "tests",
            "--",
            "python3",
            "-c",
            'print("fixed")',
            state_home=state_home,
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)

        state_path = self._find_state_path(repo, state_home)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["verification"]["passed"])

    def test_stop_gate_is_bounded(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()

        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Feature", state_home=state_home
            ).returncode,
            0,
        )

        # Capture state path while the run is still active (before budget exhaustion)
        state_path = self._find_state_path(repo, state_home)

        payload = json.dumps({"cwd": str(repo), "hook_event_name": "Stop"})
        env = {**os.environ, "CLAUDE_AUTONOMOUS_STATE_HOME": str(state_home)}

        for _ in range(3):
            result = subprocess.run(
                ["python3", str(STOP_GATE)],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn('"decision": "block"', result.stdout)

        final = subprocess.run(
            ["python3", str(STOP_GATE)],
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.assertEqual(final.returncode, 0)
        self.assertEqual(final.stdout, "")

        # After budget exhausted the run is terminal; read state directly by path
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "blocked")

    def test_reuse_ambiguous_multiple_runs_errors(self) -> None:
        """init --reuse with multiple active runs must error rather than silently pick one."""
        repo = self.make_repo()
        state_home = self.make_state_home()

        # Create two distinct active runs using --force
        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Run A", state_home=state_home
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_controller(
                repo, "init", "--feature", "Run B", "--force", state_home=state_home
            ).returncode,
            0,
        )

        result = self.run_controller(
            repo, "init", "--feature", "ignored", "--reuse", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Multiple active runs", result.stderr)

    def test_init_mode_auto_escalates_on_risk(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Add Stripe billing and payment migration",
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(
            self._find_state_path(repo, state_home).read_text(encoding="utf-8")
        )
        self.assertEqual(state["requested_mode"], "auto")
        self.assertEqual(state["effective_mode"], "rigorous")
        self.assertTrue(state["risk"]["requires_adversarial_review"])

    def test_init_mode_auto_standard_when_low_risk(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        result = self.run_controller(
            repo, "init", "--feature", "Rename a button label", state_home=state_home
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(
            self._find_state_path(repo, state_home).read_text(encoding="utf-8")
        )
        self.assertEqual(state["effective_mode"], "standard")
        self.assertFalse(state["risk"]["requires_adversarial_review"])

    def test_init_explicit_lean_not_escalated(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        result = self.run_controller(
            repo,
            "init",
            "--feature",
            "Add login auth flow",
            "--mode",
            "lean",
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(
            self._find_state_path(repo, state_home).read_text(encoding="utf-8")
        )
        self.assertEqual(state["effective_mode"], "lean")
        self.assertFalse(state["risk"]["requires_adversarial_review"])

    def test_set_risk_cannot_clear_required_adversarial_gate(self) -> None:
        """Once adversarial review is required, set-risk must not lower it: a
        high-risk run cannot be downgraded past the adversarial completion gate."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        # A rigorous-classified feature initializes with the gate required.
        self.run_controller(
            repo,
            "init",
            "--feature",
            "Add Stripe billing and payment migration",
            state_home=state_home,
        )
        state_path = self._find_state_path(repo, state_home)
        self.assertTrue(
            json.loads(state_path.read_text(encoding="utf-8"))["risk"][
                "requires_adversarial_review"
            ]
        )
        # Attempting to clear the gate fails closed and leaves it set.
        result = self.run_controller(
            repo, "set-risk", "--no-require-adversarial", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("monotonic-upward", result.stderr)
        self.assertTrue(
            json.loads(state_path.read_text(encoding="utf-8"))["risk"][
                "requires_adversarial_review"
            ]
        )

    def test_set_risk_can_escalate_low_to_required(self) -> None:
        """set-risk may raise the gate (the conservative direction)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(
            repo, "init", "--feature", "Rename a button label", state_home=state_home
        )
        state_path = self._find_state_path(repo, state_home)
        self.assertFalse(
            json.loads(state_path.read_text(encoding="utf-8"))["risk"][
                "requires_adversarial_review"
            ]
        )
        result = self.run_controller(
            repo,
            "set-risk",
            "--require-adversarial",
            "--reason",
            "touches auth after review",
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            json.loads(state_path.read_text(encoding="utf-8"))["risk"][
                "requires_adversarial_review"
            ]
        )

    def test_run_check_summary_output(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        result = self.run_controller(
            repo,
            "run-check",
            "--name",
            "unit-tests",
            "--",
            "python3",
            "-c",
            'print("noise" * 100)',
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("✓ unit-tests passed", result.stdout)
        self.assertIn("full log:", result.stdout)
        # Summary mode must NOT replay the command's stdout into context.
        self.assertNotIn("noisenoise", result.stdout)

    def test_run_check_full_output_replays_streams(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        result = self.run_controller(
            repo,
            "run-check",
            "--name",
            "unit-tests",
            "--output",
            "full",
            "--",
            "python3",
            "-c",
            'print("UNIQUEMARKER")',
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("UNIQUEMARKER", result.stdout)

    def test_run_check_failure_tail(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        script = (
            "import sys\n"
            "for i in range(200):\n"
            "    print('line', i)\n"
            "sys.exit(1)\n"
        )
        result = self.run_controller(
            repo,
            "run-check",
            "--name",
            "tests",
            "--failure-tail-lines",
            "10",
            "--",
            "python3",
            "-c",
            script,
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("✗ tests failed with exit code 1", result.stderr)
        self.assertIn("showing final 10 lines", result.stderr)
        self.assertIn("line 199", result.stderr)
        self.assertNotIn("line 150", result.stderr)

    def test_accept_structured_decisions_materializes(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        run_dir = self._find_state_path(repo, state_home).parent

        source = run_dir / "spec.codex.json"
        source.write_text(
            json.dumps(
                {
                    "title": "T",
                    "problem_statement": "P",
                    "functional_requirements": [
                        {"id": "FR-1", "requirement": "orig", "priority": "must"},
                        {"id": "FR-2", "requirement": "drop", "priority": "should"},
                    ],
                    "acceptance_criteria": [{"id": "AC-1", "criterion": "c"}],
                }
            ),
            encoding="utf-8",
        )
        decisions = Path(tempfile.mkdtemp())
        self._tmpdirs.append(decisions)
        decisions_file = decisions / "spec-decisions.json"
        decisions_file.write_text(
            json.dumps(
                {
                    "accept": ["FR-1"],
                    "reject": [{"id": "FR-2", "reason": "scope"}],
                    "modify": [{"id": "FR-1", "replacement": "newtext"}],
                    "add": [],
                }
            ),
            encoding="utf-8",
        )
        result = self.run_controller(
            repo,
            "accept",
            "--kind",
            "spec",
            "--source",
            str(source),
            "--decisions",
            str(decisions_file),
            state_home=state_home,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        accepted = json.loads(
            (run_dir / "accepted-spec.json").read_text(encoding="utf-8")
        )
        ids = [fr["id"] for fr in accepted["functional_requirements"]]
        self.assertEqual(ids, ["FR-1"])
        self.assertEqual(accepted["functional_requirements"][0]["requirement"], "newtext")
        md = (run_dir / "accepted-spec.md").read_text(encoding="utf-8")
        self.assertIn("newtext", md)

    def test_next_action_progression(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(
            repo,
            "init",
            "--feature",
            "Rename a label",
            "--mode",
            "standard",
            state_home=state_home,
        )
        first = self.run_controller(repo, "next-action", state_home=state_home)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["phase"], "specification")

        run_dir = self._find_state_path(repo, state_home).parent
        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        self.run_controller(
            repo,
            "set-phase",
            "--phase",
            "spec-accepted",
            state_home=state_home,
        )
        # accept --file to register the artifact key the way the workflow does.
        spec_src = run_dir / "src-spec.md"
        spec_src.write_text("spec", encoding="utf-8")
        self.run_controller(
            repo,
            "accept",
            "--kind",
            "spec",
            "--file",
            str(spec_src),
            state_home=state_home,
        )
        second = self.run_controller(repo, "next-action", state_home=state_home)
        self.assertEqual(json.loads(second.stdout)["phase"], "planning")

    def test_usage_report_empty_and_json(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        text = self.run_controller(repo, "usage-report", state_home=state_home)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("no Codex phases recorded", text.stdout)
        js = self.run_controller(
            repo, "usage-report", "--json", state_home=state_home
        )
        self.assertEqual(js.returncode, 0, js.stderr)
        self.assertEqual(json.loads(js.stdout), [])

    def test_evaluate_blocks_on_missing_accepted_artifact(self) -> None:
        """The completion gate checks accepted-artifact existence under the lock,
        so a missing accepted-spec/plan blocks completion (fail closed)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        # No accepted-spec.md / accepted-plan.md created.
        result = self.run_controller(repo, "evaluate", state_home=state_home)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Missing accepted-spec.md", result.stderr)
        state_path = self._find_state_path(repo, state_home)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotEqual(state.get("status"), "complete")

    def test_usage_report_works_after_run_is_terminal(self) -> None:
        """The usage report (FR-1) must remain viewable after the run reaches a
        terminal status, without requiring an explicit --run-id."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        # Drive the run to a terminal status and record a usage row.
        state["status"] = "complete"
        state["phase"] = "complete"
        state["codex_runs"] = [
            {
                "phase": "review-01",
                "prompt_characters": 100,
                "output_characters": 50,
                "duration_seconds": 1.0,
            }
        ]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        # No active run remains, and no --run-id is supplied.
        text = self.run_controller(repo, "usage-report", state_home=state_home)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("review-01", text.stdout)
        # status should likewise resolve the most-recent terminal run.
        st = self.run_controller(repo, "status", state_home=state_home)
        self.assertEqual(st.returncode, 0, st.stderr)

    def test_triage_merges_ledger(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        ledger_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(ledger_dir)
        ledger_file = ledger_dir / "triage.json"
        ledger_file.write_text(
            json.dumps(
                [
                    {
                        "fingerprint": "export.py:csv-style",
                        "status": "rejected",
                        "reason": "formatter enforces style",
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = self.run_controller(
            repo, "triage", "--file", str(ledger_file), state_home=state_home
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Recorded 1 triage finding", result.stdout)
        state = json.loads(
            self._find_state_path(repo, state_home).read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["review_ledger"][0]["fingerprint"], "export.py:csv-style"
        )

    def test_triage_rejects_unauditable_entry_without_fingerprint(self) -> None:
        """A triage entry lacking a fingerprint must fail closed: it would close a
        cumulative finding (unblocking the gate) without an audit-ledger record."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        # Seed a blocking severe cumulative finding.
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["cumulative_findings"] = [
            {"id": "F-1", "severity": "high", "status": "open"}
        ]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        ledger_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(ledger_dir)
        ledger_file = ledger_dir / "triage.json"
        # No fingerprint, but a finding_id that would close F-1 unaudited.
        ledger_file.write_text(
            json.dumps(
                [{"finding_id": "F-1", "status": "rejected", "reason": "nope"}]
            ),
            encoding="utf-8",
        )
        result = self.run_controller(
            repo, "triage", "--file", str(ledger_file), state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fingerprint", result.stderr)
        # The severe finding remains open (gate still blocked).
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["cumulative_findings"][0]["status"], "open")

    def test_source_path_prefers_run_dir_over_cwd_shadow(self) -> None:
        """A bare --source filename must bind to the run artifact, not a same-named
        file shadowing it from the current working directory."""
        run_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(run_dir)
        cwd_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(cwd_dir)
        (run_dir / "plan.codex.json").write_text("RUN", encoding="utf-8")
        (cwd_dir / "plan.codex.json").write_text("SHADOW", encoding="utf-8")

        prev = os.getcwd()
        os.chdir(cwd_dir)
        try:
            resolved = controller._resolve_source_path("plan.codex.json", run_dir)
        finally:
            os.chdir(prev)
        self.assertEqual(resolved, (run_dir / "plan.codex.json").resolve())
        self.assertEqual(resolved.read_text(encoding="utf-8"), "RUN")

    def test_source_path_absolute_outside_run_dir_still_resolves(self) -> None:
        """An absolute path (the form the skill uses for orchestrator-authored
        decisions/triage ledgers, e.g. /tmp/claude/...) must still bind to that
        literal file so the run-dir-first preference does not break the
        documented workflow."""
        run_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(run_dir)
        ext_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(ext_dir)
        external = ext_dir / "decisions.json"
        external.write_text("EXTERNAL", encoding="utf-8")

        resolved = controller._resolve_source_path(
            str(external), run_dir, label="Decisions file"
        )
        self.assertEqual(resolved, external.resolve())
        self.assertEqual(resolved.read_text(encoding="utf-8"), "EXTERNAL")

    def test_source_path_not_found_uses_label(self) -> None:
        run_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(run_dir)
        with self.assertRaises(controller.WorkflowError) as ctx:
            controller._resolve_source_path(
                "missing.json", run_dir, label="Triage ledger"
            )
        self.assertIn("Triage ledger not found", str(ctx.exception))

    def test_codex_review_records_usage_and_delta(self) -> None:
        """cmd_codex success path: --json + profile args, NDJSON artifact, usage
        record, and round-aware full-then-delta review selection."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        captured: list[list[str]] = []

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            captured.append(list(cmd))
            # Emulate codex writing the output-last-message file.
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            is_delta = "review-delta.schema.json" in " ".join(cmd)
            if is_delta:
                payload = {
                    "verdict": "pass",
                    "summary": "ok",
                    "resolved_findings": [],
                    "new_findings": [],
                    "regressions": [],
                    "affected_acceptance_criteria": [],
                    "confidence": 1.0,
                }
            else:
                payload = {
                    "verdict": "pass",
                    "summary": "ok",
                    "findings": [],
                    "verification_gaps": [],
                    "acceptance_criteria_assessment": [],
                    "confidence": 1.0,
                }
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            ndjson = (
                '{"msg": {"type": "session_configured", "model": "gpt-x-test"}}\n'
                '{"type": "token_count", "info": {"input_tokens": 10, '
                '"output_tokens": 5, "total_tokens": 15}}\n'
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=ndjson, stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            self.assertEqual(controller.cmd_codex(args), 0)
            self.assertEqual(controller.cmd_codex(args), 0)
        finally:
            controller.run_process = original

        # Both rounds applied --json and reasoning-effort overrides.
        for cmd in captured:
            self.assertIn("--json", cmd)
            self.assertTrue(
                any(c.startswith("model_reasoning_effort=") for c in cmd),
                cmd,
            )
        # Round 1 full schema, round 2 delta schema.
        self.assertIn("review.schema.json", " ".join(captured[0]))
        self.assertIn("review-delta.schema.json", " ".join(captured[1]))

        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(len(final["codex_runs"]), 2)
        rec = final["codex_runs"][0]
        self.assertEqual(rec["phase"], "review-01")
        self.assertEqual(rec["model"], "gpt-x-test")
        self.assertEqual(rec["reasoning_effort"], "high")
        self.assertIn("prompt_characters", rec)
        self.assertIn("output_characters", rec)
        self.assertIn("duration_seconds", rec)
        self.assertTrue((run_dir / "review-01.events.ndjson").exists())
        self.assertTrue((run_dir / "review-02.events.ndjson").exists())
        self.assertFalse(final["reviews"][0]["delta"])
        self.assertTrue(final["reviews"][1]["delta"])

    def test_review_round_mode_mismatch_fails_closed(self) -> None:
        """If a concurrent same-run invocation advances review_round between the
        pre-lock mode selection and the locked merge, cmd_codex must fail closed
        rather than merge a full-review payload under delta semantics."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            # This call begins as round 1 (full review). Simulate a concurrent
            # invocation completing round 1 first by advancing the persisted
            # round AND recording its full-review baseline before this
            # invocation acquires the lock (a real round-1 completion appends to
            # `reviews`, which is what makes round 2 select delta mode).
            mid = json.loads(state_path.read_text(encoding="utf-8"))
            mid["review_round"] = 1
            mid.setdefault("reviews", []).append(
                {"round": 1, "verdict": "pass", "delta": False}
            )
            state_path.write_text(json.dumps(mid), encoding="utf-8")
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            payload = {
                "verdict": "pass",
                "summary": "ok",
                "findings": [],
                "verification_gaps": [],
                "acceptance_criteria_assessment": [],
                "confidence": 1.0,
            }
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            with self.assertRaises(controller.WorkflowError) as ctx:
                controller.cmd_codex(args)
        finally:
            controller.run_process = original

        self.assertIn("round-mode mismatch", str(ctx.exception).lower())
        # Staged artifacts are cleaned up; the failing invocation's review was
        # NOT merged (only the simulated concurrent round-1 baseline remains).
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            final.get("reviews", []),
            [{"round": 1, "verdict": "pass", "delta": False}],
        )
        self.assertEqual(final.get("review_round"), 1)
        self.assertFalse(list(run_dir.glob(".staging-*")))

    def test_review_without_full_baseline_runs_full_not_delta(self) -> None:
        """A run whose review_round is already >= 1 but has NO recorded full
        review (e.g. migrated state, or a lost round-1 artifact) must run the
        next review as a FULL review, not a delta. Selecting delta purely from
        review_round would let a delta `pass` with no new findings clear the
        gate without any severe-findings baseline ever being established."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        # Simulate a migrated/legacy run: the round counter is advanced but no
        # full-review baseline was ever recorded and cumulative_findings is empty.
        state["review_round"] = 1
        state["reviews"] = []
        state["cumulative_findings"] = {}
        state_path.write_text(json.dumps(state), encoding="utf-8")

        captured: list[list[str]] = []

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            captured.append(list(cmd))
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            payload = {
                "verdict": "pass",
                "summary": "ok",
                "findings": [],
                "verification_gaps": [],
                "acceptance_criteria_assessment": [],
                "confidence": 1.0,
            }
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            self.assertEqual(controller.cmd_codex(args), 0)
        finally:
            controller.run_process = original

        # Despite review_round == 1 (next_round == 2), the absence of a recorded
        # full review forces a FULL review: full schema, not the delta schema.
        joined = " ".join(captured[0])
        self.assertIn("review.schema.json", joined)
        self.assertNotIn("review-delta.schema.json", joined)
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertFalse(final["reviews"][-1]["delta"])

    def test_codex_merge_aborts_if_run_made_terminal_during_exec(self) -> None:
        """A concurrent cancel/block while Codex runs drives the run to a
        terminal status; the post-exec locked merge must fail closed rather than
        append a review and resurrect the cancelled run."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            # Simulate a concurrent `cancel` completing while Codex runs.
            mid = json.loads(state_path.read_text(encoding="utf-8"))
            mid["status"] = "cancelled"
            mid["phase"] = "cancelled"
            state_path.write_text(json.dumps(mid), encoding="utf-8")
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            payload = {
                "verdict": "pass",
                "summary": "ok",
                "findings": [],
                "verification_gaps": [],
                "acceptance_criteria_assessment": [],
                "confidence": 1.0,
            }
            out_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            with self.assertRaises(controller.WorkflowError) as ctx:
                controller.cmd_codex(args)
        finally:
            controller.run_process = original

        self.assertIn("no longer active", str(ctx.exception).lower())
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final.get("status"), "cancelled")
        self.assertEqual(final.get("reviews", []), [])
        self.assertFalse(list(run_dir.glob(".staging-*")))

    def test_codex_staging_cleaned_on_events_write_failure(self) -> None:
        """If persisting the staged NDJSON event stream fails (e.g. disk full),
        the already-written Codex output must not be orphaned: no `.staging-*`
        files remain and no review is merged."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            out_path.write_text(json.dumps({"verdict": "pass"}), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="events", stderr="")

        real_write_text = Path.write_text

        def failing_write_text(self, *a, **k):
            if self.name.endswith(".events.ndjson"):
                raise OSError("disk full")
            return real_write_text(self, *a, **k)

        original = controller.run_process
        controller.run_process = fake_run_process
        Path.write_text = failing_write_text
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            with self.assertRaises(OSError):
                controller.cmd_codex(args)
        finally:
            Path.write_text = real_write_text
            controller.run_process = original

        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final.get("reviews", []), [])
        self.assertFalse(list(run_dir.glob(".staging-*")))

    def test_codex_staging_cleaned_on_validation_failure(self) -> None:
        """A schema-incomplete payload must fail closed AND leave no staging
        artifacts on disk (raw prompt response / NDJSON are not retained)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        self.run_controller(repo, "init", "--feature", "F", state_home=state_home)
        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "command": ["pytest"], "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(json.dumps(state), encoding="utf-8")

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            out_path = Path(cmd[cmd.index("--output-last-message") + 1])
            # Top-level-valid JSON object but missing the required `findings` key,
            # so _missing_required_fields fails closed after the staging write.
            out_path.write_text(
                json.dumps({"verdict": "pass", "summary": "ok"}), encoding="utf-8"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                phase="review",
            )
            with self.assertRaises(controller.WorkflowError):
                controller.cmd_codex(args)
        finally:
            controller.run_process = original

        self.assertFalse(
            list(run_dir.glob(".staging-*")),
            "staging artifacts must be removed on validation failure",
        )
        # No review was recorded.
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8")).get("reviews", []), []
        )

    def test_review_budget_exhausted_sets_blocked(self) -> None:
        """cmd_codex --phase review over budget must atomically set status=blocked."""
        import json as _json

        repo = self.make_repo()
        state_home = self.make_state_home()

        init = self.run_controller(
            repo, "init", "--feature", "Feature", state_home=state_home
        )
        self.assertEqual(init.returncode, 0, init.stderr)

        state_path = self._find_state_path(repo, state_home)
        run_dir = state_path.parent

        # Force review_round to the maximum so the next review attempt exceeds budget.
        state = _json.loads(state_path.read_text(encoding="utf-8"))
        state["review_round"] = state.get("max_review_rounds", 3)
        state_path.write_text(_json.dumps(state), encoding="utf-8")

        # Write required artifacts to pass pre-flight checks.
        (run_dir / "accepted-spec.md").write_text("spec", encoding="utf-8")
        (run_dir / "accepted-plan.md").write_text("plan", encoding="utf-8")
        state = _json.loads(state_path.read_text(encoding="utf-8"))
        state.setdefault("verification", {})["checks"] = [
            {"name": "t", "exit_code": 0, "passed": True}
        ]
        state["verification"]["passed"] = True
        state_path.write_text(_json.dumps(state), encoding="utf-8")

        result = self.run_controller(
            repo, "codex", "--phase", "review", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)

        final_state = _json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final_state["status"], "blocked")
        self.assertEqual(final_state["phase"], "review-budget-exhausted")


TERMINAL_STATUSES = ("complete", "blocked", "cancelled", "archived")


class TerminalStateIntegrityTests(unittest.TestCase):
    """P0 W1: terminal runs are immutable and cannot be resurrected.

    These tests assert the run-access contracts end-to-end through the CLI:
    a mutating command named with an explicit --run-id must refuse a terminal
    run, read-only inspection must keep working on terminal runs, lifecycle
    transitions must obey the transition table, and run-identity invariants
    must be enforced.
    """

    def setUp(self) -> None:
        self._tmpdirs: list[Path] = []

    def tearDown(self) -> None:
        for d in self._tmpdirs:
            if d.exists():
                shutil.rmtree(str(d), ignore_errors=True)

    # --- shared helpers (mirrors ControllerTests, kept local for isolation) ---

    def make_repo(self) -> Path:
        temp = Path(tempfile.mkdtemp())
        self._tmpdirs.append(temp)
        subprocess.run(["git", "init", "-q", str(temp)], check=True)
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(temp), "config", "user.name", "Test User"], check=True
        )
        (temp / "README.md").write_text("# Test\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(temp), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(temp), "commit", "-qm", "initial"], check=True)
        return temp

    def make_state_home(self) -> Path:
        d = Path(tempfile.mkdtemp())
        self._tmpdirs.append(d)
        return d

    def run_controller(
        self, repo: Path, *args: str, state_home: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["python3", str(CONTROLLER), "--project-root", str(repo)]
        if state_home is not None:
            cmd += ["--state-dir", str(state_home)]
        cmd += list(args)
        return subprocess.run(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def init_run(self, repo: Path, state_home: Path, feature: str = "F") -> Path:
        res = self.run_controller(
            repo, "init", "--feature", feature, state_home=state_home
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        return self._state_path(repo, state_home)

    def _state_path(self, repo: Path, state_home: Path) -> Path:
        repo_info = resolve_repository(repo)
        runs = state_home / "repositories" / repo_info.id / "runs"
        dirs = [d for d in runs.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1, f"expected exactly one run dir, got {dirs}")
        return dirs[0] / "run-state.json"

    def _set_status(self, state_path: Path, status: str) -> str:
        s = json.loads(state_path.read_text(encoding="utf-8"))
        s["status"] = status
        s["phase"] = status
        state_path.write_text(json.dumps(s), encoding="utf-8")
        return s["run_id"]

    def _mutation_commands(self) -> list[tuple[str, list[str]]]:
        ledger_dir = Path(tempfile.mkdtemp())
        self._tmpdirs.append(ledger_dir)
        ledger = ledger_dir / "triage.json"
        ledger.write_text(
            json.dumps(
                [{"fingerprint": "x.py:y", "status": "rejected", "reason": "r"}]
            ),
            encoding="utf-8",
        )
        spec = ledger_dir / "spec.md"
        spec.write_text("spec", encoding="utf-8")
        return [
            ("run-check", ["run-check", "--name", "t", "--", "python3", "-c", "print(1)"]),
            ("set-phase", ["set-phase", "--phase", "planning"]),
            ("set-risk", ["set-risk", "--require-adversarial", "--reason", "x"]),
            ("evaluate", ["evaluate"]),
            ("accept-drift", ["accept-drift"]),
            ("triage", ["triage", "--file", str(ledger)]),
            ("accept", ["accept", "--kind", "spec", "--file", str(spec)]),
            ("codex", ["codex", "--phase", "review"]),
        ]

    # --- tests ---

    def test_terminal_runs_reject_every_mutation(self) -> None:
        """Explicit-`--run-id` mutations must be refused for every terminal
        status, the error must name the run id + status + operation, and the
        persisted state bytes must be unchanged."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        commands = self._mutation_commands()

        for status in TERMINAL_STATUSES:
            run_id = self._set_status(state_path, status)
            before = state_path.read_bytes()
            for op, argv in commands:
                with self.subTest(status=status, op=op):
                    result = self.run_controller(
                        repo, "--run-id", run_id, *argv, state_home=state_home
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    err = result.stderr.lower()
                    self.assertIn("terminal", err)
                    self.assertIn(run_id, result.stderr)
                    self.assertIn(status, result.stderr)
                    # Immutability: the run-state file is byte-identical.
                    self.assertEqual(state_path.read_bytes(), before)

    def test_read_only_commands_inspect_every_terminal_status(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)

        for status in TERMINAL_STATUSES:
            run_id = self._set_status(state_path, status)
            before = state_path.read_bytes()
            inspections = [
                ["status", "--run-id", run_id],
                ["show-run", "--run-id", run_id],
                ["usage-report", "--run-id", run_id],
                ["next-action", "--run-id", run_id],
                ["list-runs", "--all"],
            ]
            for argv in inspections:
                with self.subTest(status=status, cmd=argv[0]):
                    # Global --run-id must precede the subcommand; show-run takes
                    # its run id as a subcommand option, the rest as global.
                    if argv[0] == "show-run":
                        result = self.run_controller(
                            repo, *argv, state_home=state_home
                        )
                    elif argv[0] == "list-runs":
                        result = self.run_controller(
                            repo, *argv, state_home=state_home
                        )
                    else:
                        result = self.run_controller(
                            repo,
                            "--run-id",
                            run_id,
                            argv[0],
                            state_home=state_home,
                        )
                    self.assertEqual(
                        result.returncode, 0, f"{argv[0]}: {result.stderr}"
                    )
                    self.assertEqual(state_path.read_bytes(), before)

    def test_evaluate_cannot_resurrect_completed_run(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_id = self._set_status(state_path, "complete")
        result = self.run_controller(
            repo, "--run-id", run_id, "evaluate", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["status"], "complete"
        )

    def test_cancel_and_block_cannot_rewrite_completed_run(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        for op in ("cancel", "block"):
            run_id = self._set_status(state_path, "complete")
            argv = [op] if op == "cancel" else [op, "--reason", "x"]
            result = self.run_controller(
                repo, "--run-id", run_id, *argv, state_home=state_home
            )
            with self.subTest(op=op):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("only allowed from", result.stderr)
                self.assertEqual(
                    json.loads(state_path.read_text(encoding="utf-8"))["status"],
                    "complete",
                )

    def test_active_run_cannot_be_archived(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_id = json.loads(state_path.read_text(encoding="utf-8"))["run_id"]
        result = self.run_controller(
            repo, "--run-id", run_id, "archive-run", state_home=state_home
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only allowed from", result.stderr)
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["status"], "active"
        )

    def test_archive_is_idempotent_and_preserves_data(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_id = self._set_status(state_path, "complete")
        first = self.run_controller(
            repo, "--run-id", run_id, "archive-run", state_home=state_home
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        archived_bytes = state_path.read_bytes()
        self.assertEqual(
            json.loads(archived_bytes)["status"], "archived"
        )
        # Re-archiving is a no-op that must not alter any data.
        second = self.run_controller(
            repo, "--run-id", run_id, "archive-run", state_home=state_home
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already archived", second.stdout)
        self.assertEqual(state_path.read_bytes(), archived_bytes)

    def test_init_cannot_overwrite_existing_terminal_run(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_id = self._set_status(state_path, "complete")
        before = state_path.read_bytes()
        # Even with --force, an existing run ID must not be clobbered.
        result = self.run_controller(
            repo,
            "--run-id",
            run_id,
            "init",
            "--feature",
            "overwrite attempt",
            "--force",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_state_run_id_directory_mismatch_is_rejected(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        s = json.loads(state_path.read_text(encoding="utf-8"))
        dir_name = state_path.parent.name
        s["run_id"] = "tampered-does-not-match-dir"
        state_path.write_text(json.dumps(s), encoding="utf-8")
        result = self.run_controller(
            repo, "--run-id", dir_name, "set-phase", "--phase", "planning",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match its run directory name", result.stderr)

    def test_state_repository_id_mismatch_is_rejected(self) -> None:
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        s = json.loads(state_path.read_text(encoding="utf-8"))
        run_id = s["run_id"]
        s["repository"]["id"] = "some-other-repo-id"
        state_path.write_text(json.dumps(s), encoding="utf-8")
        result = self.run_controller(
            repo, "--run-id", run_id, "set-phase", "--phase", "planning",
            state_home=state_home,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("records repository", result.stderr)

    def test_run_check_aborts_if_run_made_terminal_during_check(self) -> None:
        """A concurrent cancel while the verification command runs must prevent
        the check from being published (TOCTOU guard under the lock)."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        state_path = self.init_run(repo, state_home)
        run_dir = state_path.parent

        def fake_run_process(cmd, *, cwd, input_text=None, check=False, timeout=None):
            mid = json.loads(state_path.read_text(encoding="utf-8"))
            mid["status"] = "cancelled"
            mid["phase"] = "cancelled"
            state_path.write_text(json.dumps(mid), encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        original = controller.run_process
        controller.run_process = fake_run_process
        try:
            args = argparse.Namespace(
                project_root=str(repo),
                state_dir=str(state_home),
                run_id=None,
                name="unit-tests",
                command=["python3", "-c", "print(1)"],
                timeout=None,
                output="summary",
                failure_tail_lines=80,
            )
            with self.assertRaises((controller.WorkflowError, Exception)) as ctx:
                controller.cmd_run_check(args)
        finally:
            controller.run_process = original

        self.assertIn("terminal", str(ctx.exception).lower())
        final = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(final["status"], "cancelled")
        # The check was never published to state.
        self.assertEqual(final.get("verification", {}).get("checks", []), [])

    def test_concurrent_same_run_id_inits_cannot_both_create(self) -> None:
        """Two concurrent `init --run-id X` processes must not both materialize
        the same run: exactly one wins, the other fails closed without
        clobbering the survivor."""
        repo = self.make_repo()
        state_home = self.make_state_home()
        run_id = "fixed-shared-run-id"

        def spawn() -> subprocess.Popen[str]:
            cmd = [
                "python3", str(CONTROLLER),
                "--project-root", str(repo),
                "--state-dir", str(state_home),
                "--run-id", run_id,
                "init", "--feature", "concurrent",
            ]
            return subprocess.Popen(
                cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

        p1 = spawn()
        p2 = spawn()
        out1 = p1.communicate()
        out2 = p2.communicate()
        codes = sorted([p1.returncode, p2.returncode])
        # Exactly one wins (0); the loser fails closed (non-zero).
        self.assertEqual(codes[0], 0, (out1, out2))
        self.assertNotEqual(codes[1], 0, (out1, out2))
        loser_err = out1[1] if p1.returncode != 0 else out2[1]
        # The loser may lose at either guard depending on timing: the pre-init
        # active-run check ("already exist") or the run-id overwrite protection
        # ("already exists"). Both are correct fail-closed outcomes.
        self.assertIn("already exist", loser_err)

        # Exactly one run materialized and it is intact + active.
        repo_info = resolve_repository(repo)
        runs = state_home / "repositories" / repo_info.id / "runs"
        dirs = [d for d in runs.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1)
        state = json.loads(
            (dirs[0] / "run-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
