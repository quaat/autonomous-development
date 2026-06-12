#!/usr/bin/env python3
"""Stateful controller for the Claude + Codex autonomous-development plugin."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state import (
    StateError,
    RepoInfo,
    DriftKind,
    resolve_repository,
    resolve_state_home,
    detect_legacy_state,
    new_run_id,
    run_dir_path,
    make_relative_path,
    resolve_artifact_path,
    RunStateLock,
    migrate_v1_to_v2,
    load_run_state,
    save_run_state,
    load_repo_metadata,
    save_repo_metadata,
    find_active_runs,
    find_all_runs,
    resolve_active_run,
    detect_drift,
    repository_context,
    LEGACY_STATE_REL,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PHASE_OUTPUTS = {
    "enhance": (
        "prompts/enhance-idea.md",
        "schemas/enhanced-idea.schema.json",
        "feature-spec.codex.json",
    ),
    "plan": (
        "prompts/implementation-plan.md",
        "schemas/implementation-plan.schema.json",
        "implementation-plan.codex.json",
    ),
    "review": ("prompts/code-review.md", "schemas/review.schema.json", None),
    "adversarial": (
        "prompts/adversarial-review.md",
        "schemas/adversarial-review.schema.json",
        None,
    ),
}

# Backward-compat alias
WorkflowError = StateError


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_process(
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
    except FileNotFoundError as exc:
        raise WorkflowError(f"Required executable not found: {args[0]}") from exc


def git(root: Path, *args: str, check: bool = True) -> str:
    result = run_process(["git", *args], cwd=root)
    if check and result.returncode != 0:
        raise WorkflowError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def read_optional(path: Path) -> str:
    if not path.exists():
        return "(not available)"
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    return text or "(empty)"


def render(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    unresolved = sorted(set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", rendered)))
    if unresolved:
        raise WorkflowError(f"Unresolved prompt placeholders: {', '.join(unresolved)}")
    return rendered


def slug(value: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-").lower()
    return clean[:80] or "check"


# ---------------------------------------------------------------------------
# Shared context helpers
# ---------------------------------------------------------------------------


def get_context(args: argparse.Namespace) -> tuple[RepoInfo, Path, str | None]:
    """Return (repo, state_home, run_id_override) from parsed args."""
    start = Path(args.project_root).resolve() if args.project_root else None
    repo = resolve_repository(start)
    state_home = resolve_state_home(getattr(args, "state_dir", None))
    run_id = getattr(args, "run_id", None)
    return repo, state_home, run_id


def require_no_unsafe_drift(state: dict, repo: RepoInfo) -> None:
    """Raise WorkflowError if unsafe drift detected. Expected drift is allowed."""
    drift = detect_drift(state, repo)
    if drift.kind == DriftKind.UNSAFE:
        raise WorkflowError(
            f"Unsafe repository drift detected: {drift.message}\n"
            f"Recovery: {drift.recovery}\n"
            f"Use `accept-drift` to record the new baseline when safe."
        )


# ---------------------------------------------------------------------------
# Prompt values helper
# ---------------------------------------------------------------------------


def prompt_values(run_dir: Path, state: dict[str, Any]) -> dict[str, str]:
    """Compute template placeholder values for Codex prompts."""
    artifacts = state.get("artifacts", {})

    def artifact_text(key: str, fallback: str) -> str:
        rel = artifacts.get(key, "")
        if rel:
            try:
                path = resolve_artifact_path(str(rel), run_dir)
                return read_optional(path)
            except (StateError, OSError):
                pass
        # fallback path relative to run_dir
        fallback_path = run_dir / fallback
        return read_optional(fallback_path)

    previous_review = "(none)"
    reviews = state.get("reviews", [])
    if reviews:
        last_review = reviews[-1]
        rel_path = last_review.get("path", "")
        if rel_path:
            try:
                review_path = resolve_artifact_path(str(rel_path), run_dir)
                previous_review = read_optional(review_path)
                round_num = last_review.get("round", 0)
                triage_path = run_dir / f"triage-{round_num:02d}.md"
                if triage_path.exists():
                    previous_review += "\n\nTRIAGE\n" + read_optional(triage_path)
            except (StateError, OSError):
                pass

    return {
        "FEATURE": state.get("feature", "(missing)"),
        "BASELINE": state.get("baseline", {}).get("commit", "(missing)"),
        "REPOSITORY_CONTEXT": artifact_text(
            "repository_context", "repository-context.txt"
        ),
        "CODEX_SPEC": artifact_text("enhance", "feature-spec.codex.json"),
        "ACCEPTED_SPEC": artifact_text("accepted_spec", "accepted-spec.md"),
        "ACCEPTED_PLAN": artifact_text("accepted_plan", "accepted-plan.md"),
        "VERIFICATION": json.dumps(state.get("verification", {}), indent=2),
        "PREVIOUS_REVIEW": previous_review,
        "LATEST_REVIEW": previous_review,
    }


# ---------------------------------------------------------------------------
# Latest checks / finding helpers
# ---------------------------------------------------------------------------


def latest_verification_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the latest result for each logical verification check name."""
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for check in checks:
        name = str(check.get("name", "unnamed"))
        if name not in latest:
            order.append(name)
        latest[name] = check
    return [latest[name] for name in order]


def unresolved_severe_findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        finding
        for finding in review.get("findings", [])
        if isinstance(finding, dict) and finding.get("severity") in {"critical", "high"}
    ]


# ---------------------------------------------------------------------------
# cmd_doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    # Use project_root fallback for doctor; git check is optional
    start = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    failures: list[str] = []
    print(f"Python: {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        failures.append("Python 3.11 or later is required")

    for executable in ("git", "codex"):
        path = shutil.which(executable)
        print(f'{executable}: {path or "not found"}')
        if path is None:
            failures.append(f"{executable} is not installed or not on PATH")

    # Verify via resolve_repository as well as raw git check
    inside = git(start, "rev-parse", "--is-inside-work-tree", check=False)
    print(f'Git repository: {inside == "true"}')
    if inside != "true":
        failures.append(f"{start} is not inside a Git worktree")
    else:
        try:
            resolve_repository(start)
        except StateError as exc:
            failures.append(f"Repository resolver failed: {exc}")

    if shutil.which("codex"):
        version = run_process(["codex", "--version"], cwd=start)
        print(
            f'Codex version: {(version.stdout or version.stderr).strip() or "unknown"}'
        )
        auth = run_process(["codex", "login", "status"], cwd=start)
        print(
            f'Codex authentication: {"ready" if auth.returncode == 0 else "not ready"}'
        )
        if auth.returncode != 0:
            failures.append("Codex is not authenticated; run `codex login`")

    if failures:
        print("\nDoctor found problems:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("\nAll required local prerequisites are available.")
    return 0


# ---------------------------------------------------------------------------
# cmd_init
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)

    if not repo.head_commit:
        raise WorkflowError(
            "Git repository has no commits; cannot initialize a workflow run."
        )

    feature = args.feature.strip()
    if not feature:
        raise WorkflowError("Feature idea must not be empty")

    label = ""
    if getattr(args, "label", None):
        raw_label = args.label.strip()
        label = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_label).strip("-").lower()[:80]

    active_runs = find_active_runs(state_home, repo.id)

    if active_runs:
        if args.reuse:
            if len(active_runs) > 1:
                ids = ", ".join(r.run_id for r in active_runs)
                raise WorkflowError(
                    f"Multiple active runs exist: {ids}. "
                    "Use --run-id to select one explicitly."
                )
            run_ref = active_runs[0]
            run_dir = run_ref.run_dir
            state_path = run_dir / "run-state.json"
            print(state_path)
            return 0
        if not args.force:
            ids = ", ".join(r.run_id for r in active_runs)
            raise WorkflowError(
                f"Active workflow run(s) already exist: {ids}. "
                "Use `status`, `cancel`, `--reuse`, or `--force`."
            )

    run_id = run_id_override or new_run_id()
    run_dir = run_dir_path(state_home, repo.id, run_id)

    with RunStateLock(run_dir):
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        ctx_text = repository_context(repo)
        (run_dir / "feature-request.md").write_text(feature + "\n", encoding="utf-8")
        (run_dir / "repository-context.txt").write_text(ctx_text, encoding="utf-8")

        dirty = git(repo.canonical_root, "status", "--short", check=False).splitlines()

        state: dict[str, Any] = {
            "schema_version": 2,
            "run_id": run_id,
            "label": label,
            "feature": feature,
            "status": "active",
            "phase": "initialized",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "repository": {
                "id": repo.id,
                "canonical_root": str(repo.canonical_root),
                "git_common_dir": str(repo.git_common_dir),
                "worktree_path": str(repo.worktree_path),
                "display_name": repo.display_name,
                "remote_display": repo.remote_display,
            },
            "baseline": {
                "commit": repo.head_commit,
                "branch": repo.branch,
                "dirty_entries_at_init": dirty,
            },
            "max_review_rounds": args.max_review_rounds,
            "review_round": 0,
            "stop_gate_blocks": 0,
            "artifacts": {
                "feature_request": "feature-request.md",
                "repository_context": "repository-context.txt",
            },
            "verification": {"checks": [], "passed": False},
            "reviews": [],
            "adversarial_reviews": [],
            "risk": {"requires_adversarial_review": False, "reasons": []},
            "notes": [],
        }
        save_run_state(run_dir, state)

    # Save repo metadata
    meta = load_repo_metadata(state_home, repo.id)
    meta.update(
        {
            "id": repo.id,
            "display_name": repo.display_name,
            "canonical_root": str(repo.canonical_root),
            "remote_display": repo.remote_display,
            "last_run_id": run_id,
        }
    )
    save_repo_metadata(state_home, repo.id, meta)

    print(run_dir / "run-state.json")
    return 0


# ---------------------------------------------------------------------------
# cmd_codex
# ---------------------------------------------------------------------------


def cmd_codex(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    state = run_ref.state
    run_dir = run_ref.run_dir

    require_no_unsafe_drift(state, repo)

    if state.get("status") != "active":
        raise WorkflowError(f"Workflow is not active: {state.get('status')}")

    phase = args.phase
    prompt_rel, schema_rel, static_output = PHASE_OUTPUTS[phase]

    if phase == "plan":
        spec_path = resolve_artifact_path(
            state.get("artifacts", {}).get("accepted_spec", "accepted-spec.md"), run_dir
        )
        if not spec_path.exists():
            raise WorkflowError(
                "Create accepted-spec.md (in the run directory) before planning"
            )
    if phase in {"review", "adversarial"}:
        plan_path = resolve_artifact_path(
            state.get("artifacts", {}).get("accepted_plan", "accepted-plan.md"), run_dir
        )
        if not plan_path.exists():
            raise WorkflowError(
                "Create accepted-plan.md (in the run directory) before review"
            )
        if not state.get("verification", {}).get("checks"):
            raise WorkflowError("Record at least one verification check before review")

    if phase == "review":
        next_round = int(state.get("review_round", 0)) + 1
        maximum = int(state.get("max_review_rounds", 3))
        if next_round > maximum:
            with RunStateLock(run_dir):
                fresh = load_run_state(run_dir)
                fresh["status"] = "blocked"
                fresh["phase"] = "review-budget-exhausted"
                fresh.setdefault("notes", []).append(
                    f"Maximum review rounds exhausted ({maximum})"
                )
                save_run_state(run_dir, fresh)
            raise WorkflowError(f"Maximum review rounds exhausted ({maximum})")
        output_name = f"review-{next_round:02d}.codex.json"
    elif phase == "adversarial":
        index = len(state.get("adversarial_reviews", [])) + 1
        output_name = f"adversarial-{index:02d}.codex.json"
    else:
        output_name = static_output
        assert output_name is not None

    template = (PLUGIN_ROOT / prompt_rel).read_text(encoding="utf-8")
    prompt = render(template, prompt_values(run_dir, state))
    prompt_path = run_dir / f"{phase}.prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    output_path = run_dir / output_name

    command = [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(PLUGIN_ROOT / schema_rel),
        "--output-last-message",
        str(output_path),
        "-",
    ]
    result = run_process(command, cwd=repo.canonical_root, input_text=prompt)
    if result.returncode != 0:
        error_path = run_dir / f"{phase}.codex.stderr.log"
        error_path.write_text(result.stderr, encoding="utf-8")
        with RunStateLock(run_dir):
            err_state = load_run_state(run_dir)
            err_state.setdefault("notes", []).append(
                f"Codex {phase} failed; see {make_relative_path(error_path, run_dir)}"
            )
            save_run_state(run_dir, err_state)
        raise WorkflowError(result.stderr.strip() or f"Codex {phase} failed")

    try:
        parsed = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(
            f"Codex did not produce valid JSON at {output_path}: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise WorkflowError(f"Codex output must be an object: {output_path}")

    # Recompute round/index from fresh state inside the lock to close the TOCTOU
    # gap between the pre-Codex snapshot check and the post-Codex state write.
    final_path = output_path
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        if phase == "review":
            next_round = int(state.get("review_round", 0)) + 1
            maximum = int(state.get("max_review_rounds", 3))
            if next_round > maximum:
                state["status"] = "blocked"
                state["phase"] = "review-budget-exhausted"
                state.setdefault("notes", []).append(
                    f"Maximum review rounds exhausted ({maximum})"
                )
                save_run_state(run_dir, state)
                raise WorkflowError(f"Maximum review rounds exhausted ({maximum})")
            canonical = run_dir / f"review-{next_round:02d}.codex.json"
            if output_path != canonical:
                output_path.replace(canonical)
            final_path = canonical
        elif phase == "adversarial":
            index = len(state.get("adversarial_reviews", [])) + 1
            canonical = run_dir / f"adversarial-{index:02d}.codex.json"
            if output_path != canonical:
                output_path.replace(canonical)
            final_path = canonical
        state.setdefault("artifacts", {})[phase] = make_relative_path(
            final_path, run_dir
        )
        if phase == "enhance":
            state["phase"] = "idea-enhanced"
        elif phase == "plan":
            state["phase"] = "plan-proposed"
        elif phase == "review":
            state["review_round"] = next_round
            state["phase"] = "reviewed"
            state.setdefault("reviews", []).append(
                {
                    "round": next_round,
                    "path": make_relative_path(final_path, run_dir),
                    "verdict": parsed.get("verdict"),
                }
            )
        elif phase == "adversarial":
            state["phase"] = "adversarially-reviewed"
            state.setdefault("adversarial_reviews", []).append(
                {
                    "round": index,
                    "path": make_relative_path(final_path, run_dir),
                    "verdict": parsed.get("verdict"),
                }
            )
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)
    print(final_path)
    return 0


# ---------------------------------------------------------------------------
# cmd_accept
# ---------------------------------------------------------------------------


def cmd_accept(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    state = run_ref.state
    run_dir = run_ref.run_dir

    require_no_unsafe_drift(state, repo)

    source = Path(args.file).resolve()
    if not source.is_file():
        raise WorkflowError(f"Accepted artifact does not exist: {source}")
    destination_name = "accepted-spec.md" if args.kind == "spec" else "accepted-plan.md"
    destination = run_dir / destination_name
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        state.setdefault("artifacts", {})[f"accepted_{args.kind}"] = destination_name
        state["phase"] = "spec-accepted" if args.kind == "spec" else "plan-accepted"
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)
    print(destination)
    return 0


# ---------------------------------------------------------------------------
# cmd_run_check
# ---------------------------------------------------------------------------


def cmd_run_check(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    run_dir = run_ref.run_dir

    require_no_unsafe_drift(run_ref.state, repo)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise WorkflowError("Provide a verification command after `--`")

    verification_dir = run_dir / "verification"
    verification_dir.mkdir(parents=True, exist_ok=True)

    # Run the check outside the lock — may be long-running.
    started = utc_now()
    result = run_process(command, cwd=repo.canonical_root)
    completed = utc_now()

    # Acquire lock to compute a collision-free index and persist atomically.
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        index = len(state.get("verification", {}).get("checks", [])) + 1
        log_path = verification_dir / f"{index:02d}-{slug(args.name)}.log"
        combined = (
            f"COMMAND: {json.dumps(command)}\n"
            f"STARTED: {started}\n"
            f"EXIT CODE: {result.returncode}\n\n"
            f"STDOUT\n{result.stdout}\n\nSTDERR\n{result.stderr}\n"
        )
        log_path.write_text(combined, encoding="utf-8")
        check_record = {
            "name": args.name,
            "command": command,
            "exit_code": result.returncode,
            "log": make_relative_path(log_path, run_dir),
            "started_at": started,
            "completed_at": completed,
        }
        state.setdefault("verification", {}).setdefault("checks", []).append(
            check_record
        )
        checks = state["verification"]["checks"]
        effective_checks = latest_verification_checks(checks)
        state["verification"]["passed"] = bool(effective_checks) and all(
            c["exit_code"] == 0 for c in effective_checks
        )
        state["phase"] = (
            "verified" if state["verification"]["passed"] else "verification-failed"
        )
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)

    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    print(f"\nVerification log: {log_path}", file=sys.stderr)
    return result.returncode


# ---------------------------------------------------------------------------
# cmd_set_phase
# ---------------------------------------------------------------------------


def cmd_set_phase(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_dir = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    ).run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        require_no_unsafe_drift(state, repo)
        state["phase"] = args.phase
        if args.note:
            state.setdefault("notes", []).append(args.note)
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)
    print(args.phase)
    return 0


# ---------------------------------------------------------------------------
# cmd_set_risk
# ---------------------------------------------------------------------------


def cmd_set_risk(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_dir = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    ).run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        require_no_unsafe_drift(state, repo)
        state.setdefault("risk", {})[
            "requires_adversarial_review"
        ] = args.require_adversarial
        if args.reason:
            state["risk"].setdefault("reasons", []).append(args.reason)
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)
    return 0


# ---------------------------------------------------------------------------
# cmd_evaluate
# ---------------------------------------------------------------------------


def cmd_evaluate(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    run_dir = run_ref.run_dir
    # Drift check uses snapshot; git state is outside our file lock anyway.
    require_no_unsafe_drift(run_ref.state, repo)

    # Artifact existence is idempotent; check outside lock to avoid holding
    # the lock during filesystem traversal.
    artifact_reasons: list[str] = []
    for artifact_key, filename in (
        ("accepted_spec", "accepted-spec.md"),
        ("accepted_plan", "accepted-plan.md"),
    ):
        rel = run_ref.state.get("artifacts", {}).get(artifact_key, filename)
        try:
            path = resolve_artifact_path(str(rel), run_dir)
        except StateError:
            path = run_dir / filename
        if not path.exists():
            artifact_reasons.append(f"Missing {filename}")

    # Rebuild all state-dependent gate conditions from freshly-loaded state
    # inside the lock so that completion_gate_failures reflects current reality.
    reasons: list[str] = []
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        reasons = list(artifact_reasons)

        checks = latest_verification_checks(
            state.get("verification", {}).get("checks", [])
        )
        if not checks:
            reasons.append("No verification checks recorded")
        elif any(check.get("exit_code") != 0 for check in checks):
            reasons.append("One or more verification checks failed")

        reviews = state.get("reviews", [])
        if not reviews:
            reasons.append("No Codex code review recorded")
        else:
            last_review = reviews[-1]
            rel_path = last_review.get("path", "")
            try:
                review_path = resolve_artifact_path(str(rel_path), run_dir)
                review = json.loads(review_path.read_text(encoding="utf-8"))
            except (StateError, OSError, json.JSONDecodeError) as exc:
                reasons.append(f"Could not read latest review: {exc}")
                review = {}
            if review.get("verdict") != "pass":
                reasons.append(
                    f"Latest Codex review verdict is {review.get('verdict')}"
                )
            severe = unresolved_severe_findings(review)
            if severe:
                reasons.append(
                    f"Latest review contains {len(severe)} critical/high finding(s)"
                )

        requires_adversarial = bool(
            state.get("risk", {}).get("requires_adversarial_review")
        )
        if requires_adversarial:
            adversarial = state.get("adversarial_reviews", [])
            if not adversarial:
                reasons.append("High-risk change requires an adversarial review")
            elif adversarial[-1].get("verdict") != "pass":
                reasons.append(
                    f"Latest adversarial review verdict is "
                    f"{adversarial[-1].get('verdict')}"
                )

        if reasons:
            state["status"] = "active"
            state["phase"] = "completion-gates-failed"
            state["completion_gate_failures"] = reasons
        else:
            state["status"] = "complete"
            state["phase"] = "complete"
            state["completion_gate_failures"] = []
        save_run_state(run_dir, state)

    if reasons:
        for reason in reasons:
            print(f"- {reason}", file=sys.stderr)
        return 1
    print("Workflow complete")
    return 0


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)

    if run_id_override:
        run_ref = resolve_active_run(
            state_home, repo.id, repo.canonical_root, run_id_override
        )
        runs = [run_ref]
    else:
        active = find_active_runs(state_home, repo.id)
        if not active:
            # fall back to legacy or raise
            run_ref = resolve_active_run(
                state_home, repo.id, repo.canonical_root, None, allow_multiple=True
            )
            runs = [run_ref]
        elif len(active) > 1:
            if args.json:
                print(json.dumps([r.state for r in active], indent=2, sort_keys=True))
                return 0
            print(f"Multiple active runs ({len(active)}):")
            for r in active:
                lbl = r.state.get("label", "")
                print(
                    f'  {r.run_id}  label={lbl or "(none)"}  '
                    f'phase={r.state.get("phase")}  status={r.state.get("status")}'
                )
            print("Use --run-id to inspect a specific run.")
            return 0
        else:
            runs = active

    state = runs[0].state

    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    checks = latest_verification_checks(state.get("verification", {}).get("checks", []))
    passed = sum(1 for item in checks if item.get("exit_code") == 0)
    print(f"Run: {state.get('run_id')}")
    if state.get("label"):
        print(f"Label: {state['label']}")
    print(f"Status: {state.get('status')}")
    print(f"Phase: {state.get('phase')}")
    print(f"Feature: {state.get('feature')}")
    print(f"Baseline: {state.get('baseline', {}).get('commit')}")
    print(f"Verification: {passed}/{len(checks)} passing")
    print(
        f"Reviews: {state.get('review_round', 0)}/{state.get('max_review_rounds', 3)}"
    )
    if state.get("reviews"):
        print(f"Latest review: {state['reviews'][-1].get('verdict')}")
    if state.get("risk", {}).get("requires_adversarial_review"):
        verdict = (
            state.get("adversarial_reviews", [{}])[-1].get("verdict")
            if state.get("adversarial_reviews")
            else "missing"
        )
        print(f"Adversarial review required: {verdict}")
    failures = state.get("completion_gate_failures", [])
    if failures:
        print("Remaining gates:")
        for failure in failures:
            print(f"- {failure}")
    return 0


# ---------------------------------------------------------------------------
# cmd_cancel
# ---------------------------------------------------------------------------


def cmd_cancel(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_dir = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    ).run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        require_no_unsafe_drift(state, repo)
        state["status"] = "cancelled"
        state["phase"] = "cancelled"
        if args.reason:
            state.setdefault("notes", []).append(args.reason)
        save_run_state(run_dir, state)
    print("Workflow cancelled")
    return 0


# ---------------------------------------------------------------------------
# cmd_block
# ---------------------------------------------------------------------------


def cmd_block(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_dir = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    ).run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        require_no_unsafe_drift(state, repo)
        state["status"] = "blocked"
        state["phase"] = "blocked"
        state.setdefault("notes", []).append(args.reason)
        save_run_state(run_dir, state)
    print("Workflow blocked")
    return 0


# ---------------------------------------------------------------------------
# cmd_list_runs
# ---------------------------------------------------------------------------


def cmd_list_runs(args: argparse.Namespace) -> int:
    repo, state_home, _run_id_override = get_context(args)

    if getattr(args, "all", False):
        runs = find_all_runs(state_home, repo.id)
    else:
        # Default: active runs only (exclude archived and terminal)
        runs = [
            r
            for r in find_active_runs(state_home, repo.id)
            if r.state.get("status") != "archived"
        ]

    if args.json:
        print(json.dumps([r.state for r in runs], indent=2, sort_keys=True))
        return 0

    if not runs:
        print("No runs found.")
        return 0

    header = f"{'RUN_ID':<30}  {'LABEL':<20}  {'STATUS':<12}  {'PHASE':<28}  CREATED"
    print(header)
    print("-" * len(header))
    for r in runs:
        s = r.state
        run_id_str = (s.get("run_id") or r.run_id)[:30]
        label_str = (s.get("label") or "")[:20]
        status_str = (s.get("status") or "")[:12]
        phase_str = (s.get("phase") or "")[:28]
        created_str = (s.get("created_at") or "")[:25]
        print(
            f"{run_id_str:<30}  {label_str:<20}  {status_str:<12}  "
            f"{phase_str:<28}  {created_str}"
        )
    return 0


# ---------------------------------------------------------------------------
# cmd_show_run
# ---------------------------------------------------------------------------


def cmd_show_run(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)

    # run_id from --run-id arg on this subcommand, or global --run-id
    run_id = getattr(args, "show_run_id", None) or run_id_override
    run_ref = resolve_active_run(state_home, repo.id, repo.canonical_root, run_id)

    if args.json:
        print(json.dumps(run_ref.state, indent=2, sort_keys=True))
        return 0

    state = run_ref.state
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# cmd_migrate_legacy_state
# ---------------------------------------------------------------------------


def cmd_migrate_legacy_state(args: argparse.Namespace) -> int:
    repo, state_home, _run_id_override = get_context(args)

    legacy_dir = detect_legacy_state(repo.canonical_root)
    if legacy_dir is None:
        raise WorkflowError(
            f"No legacy run-state.json found under {repo.canonical_root / LEGACY_STATE_REL}. "
            "Nothing to migrate."
        )

    legacy_state_path = legacy_dir / "run-state.json"
    try:
        legacy_state = json.loads(legacy_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read legacy state: {exc}") from exc
    if not isinstance(legacy_state, dict):
        raise WorkflowError("Legacy state must be a JSON object.")

    # Determine run_id for the new layout
    legacy_run_id = legacy_state.get("run_id") or new_run_id()
    new_run_dir = run_dir_path(state_home, repo.id, legacy_run_id)

    # Idempotency check
    existing_state_path = new_run_dir / "run-state.json"
    if existing_state_path.exists() and not getattr(args, "force", False):
        try:
            existing = json.loads(existing_state_path.read_text(encoding="utf-8"))
            if existing.get("run_id") == legacy_run_id and existing.get(
                "migrated_from"
            ):
                print(
                    f"Already migrated: run {legacy_run_id!r} exists at {new_run_dir}"
                )
                return 0
        except (OSError, json.JSONDecodeError):
            pass
        raise WorkflowError(
            f"Run directory already exists: {new_run_dir}. " "Use --force to overwrite."
        )

    new_run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Copy all files from legacy dir preserving metadata
    for src in legacy_dir.iterdir():
        if src.is_file():
            dst = new_run_dir / src.name
            shutil.copy2(str(src), str(dst))
        elif src.is_dir():
            dst_dir = new_run_dir / src.name
            if dst_dir.exists() and getattr(args, "force", False):
                shutil.rmtree(str(dst_dir))
            shutil.copytree(str(src), str(dst_dir))

    # Convert to v2 format; pass legacy_dir so absolute paths under it are correctly relativized.
    migrated = migrate_v1_to_v2(legacy_state, new_run_dir, repo, legacy_dir=legacy_dir)
    migrated["run_id"] = legacy_run_id
    migrated["migrated_from"] = str(legacy_dir)
    migrated["migrated_at"] = utc_now()

    save_run_state(new_run_dir, migrated)

    # Save repo metadata
    meta = load_repo_metadata(state_home, repo.id)
    meta.update(
        {
            "id": repo.id,
            "display_name": repo.display_name,
            "canonical_root": str(repo.canonical_root),
            "remote_display": repo.remote_display,
            "last_run_id": legacy_run_id,
        }
    )
    save_repo_metadata(state_home, repo.id, meta)

    print(f"Migrated legacy state from {legacy_dir} to {new_run_dir}")
    print(f"Run ID: {legacy_run_id}")
    print("The original legacy directory has NOT been modified.")
    return 0


# ---------------------------------------------------------------------------
# cmd_archive_run
# ---------------------------------------------------------------------------


def cmd_archive_run(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    run_id_str = run_ref.run_id
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        require_no_unsafe_drift(state, repo)
        state["status"] = "archived"
        state.setdefault("notes", []).append(f"Archived at {utc_now()}")
        save_run_state(run_dir, state)
    print(f"Run {run_id_str!r} archived.")
    return 0


# ---------------------------------------------------------------------------
# cmd_accept_drift
# ---------------------------------------------------------------------------


def cmd_accept_drift(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_dir = resolve_active_run(
        state_home, repo.id, repo.canonical_root, run_id_override
    ).run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        old_baseline = dict(state.get("baseline", {}))
        old_repo_block = dict(state.get("repository", {}))

        state["repository"] = {
            "id": repo.id,
            "canonical_root": str(repo.canonical_root),
            "git_common_dir": str(repo.git_common_dir),
            "worktree_path": str(repo.worktree_path),
            "display_name": repo.display_name,
            "remote_display": repo.remote_display,
        }
        state["baseline"] = {
            "commit": repo.head_commit,
            "branch": repo.branch,
            "worktree_path": str(repo.worktree_path),
            "dirty_entries_at_init": old_baseline.get("dirty_entries_at_init", []),
        }
        state.setdefault("notes", []).append(
            f"drift_accepted_at={utc_now()} "
            f"drift_accepted_commit={repo.head_commit}"
        )
        save_run_state(run_dir, state)

    print("Drift accepted. Updated baseline:")
    old_commit = old_baseline.get("commit", "(unknown)")
    old_branch = old_baseline.get("branch", "(unknown)")
    old_worktree = old_baseline.get(
        "worktree_path", old_repo_block.get("worktree_path", "")
    )
    if old_commit != repo.head_commit:
        print(f"  commit: {old_commit} -> {repo.head_commit}")
    if old_branch != repo.branch:
        print(f"  branch: {old_branch} -> {repo.branch}")
    if old_worktree and old_worktree != str(repo.worktree_path):
        print(f"  worktree: {old_worktree} -> {repo.worktree_path}")
    if old_repo_block.get("id") and old_repo_block["id"] != repo.id:
        print(f'  repo_id: {old_repo_block["id"]} -> {repo.id}')
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root", help="Target repository; defaults to current directory"
    )
    parser.add_argument("--state-dir", help="Override state home directory")
    parser.add_argument("--run-id", help="Specify run ID for run-scoped commands")
    sub = parser.add_subparsers(dest="command_name", required=True)

    doctor = sub.add_parser(
        "doctor", help="Check Git, Python, Codex, and authentication"
    )
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser("init", help="Initialize a workflow run")
    init.add_argument("--feature", required=True)
    init.add_argument("--label", help="Human-readable label stored in state")
    init.add_argument("--max-review-rounds", type=int, default=3, choices=range(1, 6))
    init.add_argument("--reuse", action="store_true")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    codex = sub.add_parser("codex", help="Run a structured, read-only Codex phase")
    codex.add_argument("--phase", required=True, choices=sorted(PHASE_OUTPUTS))
    codex.set_defaults(func=cmd_codex)

    accept = sub.add_parser(
        "accept", help="Record Claude-reconciled specification or plan"
    )
    accept.add_argument("--kind", required=True, choices=("spec", "plan"))
    accept.add_argument("--file", required=True)
    accept.set_defaults(func=cmd_accept)

    run_check = sub.add_parser(
        "run-check", help="Execute and record one verification command"
    )
    run_check.add_argument("--name", required=True)
    run_check.add_argument("command", nargs=argparse.REMAINDER)
    run_check.set_defaults(func=cmd_run_check)

    phase = sub.add_parser("set-phase", help="Update phase and optional note")
    phase.add_argument("--phase", required=True)
    phase.add_argument("--note")
    phase.set_defaults(func=cmd_set_phase)

    risk = sub.add_parser("set-risk", help="Set whether adversarial review is required")
    risk.add_argument(
        "--require-adversarial", action=argparse.BooleanOptionalAction, default=True
    )
    risk.add_argument("--reason")
    risk.set_defaults(func=cmd_set_risk)

    evaluate = sub.add_parser("evaluate", help="Evaluate all completion gates")
    evaluate.set_defaults(func=cmd_evaluate)

    status = sub.add_parser("status", help="Show workflow state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    cancel = sub.add_parser("cancel", help="Cancel the active workflow")
    cancel.add_argument("--reason")
    cancel.set_defaults(func=cmd_cancel)

    block = sub.add_parser("block", help="Mark the workflow blocked")
    block.add_argument("--reason", required=True)
    block.set_defaults(func=cmd_block)

    list_runs = sub.add_parser(
        "list-runs", help="List workflow runs for this repository"
    )
    list_runs.add_argument("--json", action="store_true", help="Output JSON array")
    list_runs.add_argument(
        "--all", action="store_true", help="Include archived/terminal runs"
    )
    list_runs.set_defaults(func=cmd_list_runs)

    show_run = sub.add_parser("show-run", help="Show all fields of a specific run")
    show_run.add_argument("--run-id", dest="show_run_id", help="Run ID to display")
    show_run.add_argument("--json", action="store_true", help="Output JSON")
    show_run.set_defaults(func=cmd_show_run)

    migrate = sub.add_parser(
        "migrate-legacy-state",
        help="Migrate legacy .ai/autonomous-development state to new layout",
    )
    migrate.add_argument(
        "--force", action="store_true", help="Overwrite existing new-format run"
    )
    migrate.set_defaults(func=cmd_migrate_legacy_state)

    archive = sub.add_parser(
        "archive-run", help="Archive a run (exclude from default listing)"
    )
    archive.set_defaults(func=cmd_archive_run)

    accept_drift = sub.add_parser(
        "accept-drift", help="Accept current repository state as new drift baseline"
    )
    accept_drift.set_defaults(func=cmd_accept_drift)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
