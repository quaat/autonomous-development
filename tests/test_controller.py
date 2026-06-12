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


if __name__ == "__main__":
    unittest.main()
