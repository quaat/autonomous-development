#!/usr/bin/env python3
"""Skill-scoped Stop hook that keeps a bounded autonomous run moving."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import (
    TERMINAL_STATUSES,
    RunStateLock,
    StateError,
    find_active_runs,
    load_run_state,
    resolve_repository,
    resolve_state_home,
    save_run_state,
    detect_legacy_state,
    LEGACY_STATE_FILE_NAME,
)

MAX_GATE_BLOCKS = 3


def reason_for(state: dict, run_dir: Path) -> str:
    """Return a human-readable reason to continue the workflow."""
    if not (run_dir / "accepted-spec.md").exists():
        return (
            "Continue the autonomous workflow: reconcile the Codex proposal "
            "and create accepted-spec.md."
        )
    if not (run_dir / "accepted-plan.md").exists():
        return (
            "Continue the autonomous workflow: reconcile the Codex plan "
            "and create accepted-plan.md."
        )
    all_checks = state.get("verification", {}).get("checks", [])
    latest: dict[str, dict] = {}
    for check in all_checks:
        latest[str(check.get("name", "unnamed"))] = check
    checks = list(latest.values())
    if not checks:
        return "Continue the autonomous workflow: run and record relevant verification checks."
    if any(check.get("exit_code") != 0 for check in checks):
        return (
            "Continue the autonomous workflow: fix the failing verification checks "
            "and rerun them."
        )
    reviews = state.get("reviews", [])
    if not reviews:
        return (
            "Continue the autonomous workflow: run the independent Codex code review."
        )
    if reviews[-1].get("verdict") != "pass":
        return (
            "Continue the autonomous workflow: triage the latest Codex findings, "
            "fix valid issues, verify, and re-review."
        )
    if state.get("risk", {}).get("requires_adversarial_review"):
        adversarial = state.get("adversarial_reviews", [])
        if not adversarial or adversarial[-1].get("verdict") != "pass":
            return (
                "Continue the autonomous workflow: complete the required adversarial review "
                "and address valid risks."
            )
    return "Run the controller completion-gate evaluation and provide the final implementation report."


def _block_and_exit(run_dir: Path) -> int:
    """Atomically increment stop_gate_blocks and persist; print block JSON or exhaust. Return 0."""
    with RunStateLock(run_dir):
        try:
            state = load_run_state(run_dir, required=True)
        except Exception:
            return 0
        if state.get("status") in TERMINAL_STATUSES:
            return 0
        blocks = int(state.get("stop_gate_blocks", 0))
        if blocks >= MAX_GATE_BLOCKS:
            state["status"] = "blocked"
            state["phase"] = "stop-gate-budget-exhausted"
            state.setdefault("notes", []).append(
                "The bounded Stop hook retry budget was exhausted; inspect manually."
            )
            save_run_state(run_dir, state)
            return 0
        state["stop_gate_blocks"] = blocks + 1
        reason = reason_for(state, run_dir)
        save_run_state(run_dir, state)
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()

    # Step 1: resolve repository — if not in a git repo, nothing to do
    try:
        repo = resolve_repository(cwd)
    except StateError:
        return 0
    except Exception:
        return 0

    # Step 2: resolve state home (env or XDG default; no CLI arg available here)
    try:
        state_home = resolve_state_home(None)
    except Exception:
        return 0

    # Step 3: find active runs
    try:
        active = find_active_runs(state_home, repo.id)
    except Exception:
        return 0

    run_dir: Path
    state: dict

    if len(active) == 0:
        # Fall back to legacy state in repo root
        legacy_dir = detect_legacy_state(repo.canonical_root)
        if legacy_dir is None:
            return 0
        legacy_path = legacy_dir / LEGACY_STATE_FILE_NAME
        try:
            state = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        if not isinstance(state, dict):
            return 0
        run_dir = legacy_dir

    elif len(active) == 1:
        run_dir = active[0].run_dir
        state = active[0].state

    else:
        # Multiple active runs — ambiguous; fail safe without blocking
        ids = ", ".join(r.run_id for r in active)
        print(
            f"autonomous-development stop-gate: multiple active runs ({ids}); "
            "cannot auto-select — resolve manually.",
            file=sys.stderr,
        )
        return 0

    # Step 4: skip if run is already terminal
    if state.get("status") in TERMINAL_STATUSES:
        return 0

    # Step 5: enforce bounded block counter
    return _block_and_exit(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
