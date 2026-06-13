#!/usr/bin/env python3
"""Stateful controller for the Claude + Codex autonomous-development plugin."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

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
    RepoInitLock,
    migrate_v1_to_v2,
    load_run_state,
    save_run_state,
    load_repo_metadata,
    save_repo_metadata,
    find_active_runs,
    find_all_runs,
    resolve_active_run,
    resolve_run_for_inspection,
    resolve_run_for_active_mutation,
    resolve_run_for_transition,
    require_active_run_state,
    assert_transition_allowed,
    detect_drift,
    repository_context,
    LEGACY_STATE_REL,
)
from schema_validation import SchemaValidationError, validate_payload

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

# Round-2+ code review uses a compact delta prompt/schema.
REVIEW_DELTA_PROMPT = "prompts/code-review-delta.md"
REVIEW_DELTA_SCHEMA = "schemas/review-delta.schema.json"

# Phase-specific Codex reasoning profiles. Installations may override these via the
# CLAUDE_AUTONOMOUS_PHASE_PROFILES env var (a JSON object keyed by phase) and select a
# per-phase model with CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>.
PHASE_PROFILES: dict[str, dict[str, str]] = {
    "enhance": {"reasoning": "medium", "verbosity": "low", "reasoning_summary": "none"},
    "plan": {"reasoning": "high", "verbosity": "low", "reasoning_summary": "none"},
    "review": {"reasoning": "high", "verbosity": "low", "reasoning_summary": "none"},
    "adversarial": {
        "reasoning": "xhigh",
        "verbosity": "low",
        "reasoning_summary": "none",
    },
}
_DEFAULT_PROFILE = {"reasoning": "high", "verbosity": "low", "reasoning_summary": "none"}

WORKFLOW_MODES = ("auto", "lean", "standard", "rigorous")

# Conservative risk categories used by `--mode auto` escalation. Matching any
# category escalates an `auto` run to rigorous.
MODE_RISK_PATTERNS: dict[str, list[str]] = {
    "auth/authz": [
        r"\bauth",
        r"authoriz",
        r"authentic",
        r"\blogin\b",
        r"permission",
        r"\brbac\b",
        r"\bacl\b",
        r"\bsession",
        r"credential",
    ],
    "persistence/migration": [
        r"migrat",
        r"\bschema\b",
        r"database",
        r"\bsql\b",
        r"persist",
        r"\borm\b",
    ],
    "personal/regulated data": [
        r"\bpii\b",
        r"personal data",
        r"regulated",
        r"\bgdpr\b",
        r"\bhipaa\b",
        r"\bpci\b",
        r"\bprivacy\b",
    ],
    "billing": [
        r"billing",
        r"payment",
        r"invoice",
        r"\bcharge",
        r"\bstripe\b",
        r"subscription",
    ],
    "concurrency/retries": [
        r"concurren",
        r"\brace\b",
        r"\bretr(y|ies|ied)\b",
        r"idempoten",
        r"\bmutex\b",
        r"\bthread",
    ],
    "public-API compatibility": [
        r"public api",
        r"public interface",
        r"backward compat",
        r"breaking change",
        r"api compatibility",
        r"\bcontract\b",
    ],
    "destructive/irreversible": [
        r"\bdelete\b",
        r"\bdestroy\b",
        r"\bdrop\b",
        r"irreversib",
        r"\bpurge\b",
        r"truncate",
        r"rm -rf",
    ],
    "broad architectural change": [
        r"architectur",
        r"\brewrite\b",
        r"redesign",
    ],
}

# Backward-compat alias
WorkflowError = StateError


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# Generous default so legitimately long Codex reviews/verification runs are not
# killed; `CLAUDE_AUTONOMOUS_PROCESS_TIMEOUT` overrides it (set to 0/empty to
# disable the timeout entirely for unusual environments).
DEFAULT_PROCESS_TIMEOUT_SECONDS = 3600.0
PROCESS_TIMEOUT_EXIT_CODE = 124


def _resolve_process_timeout() -> float | None:
    raw = os.environ.get("CLAUDE_AUTONOMOUS_PROCESS_TIMEOUT")
    if raw is None:
        return DEFAULT_PROCESS_TIMEOUT_SECONDS
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PROCESS_TIMEOUT_SECONDS
    return value if value > 0 else None


def run_process(
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    effective_timeout = timeout if timeout is not None else _resolve_process_timeout()
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
            timeout=effective_timeout,
        )
    except FileNotFoundError as exc:
        raise WorkflowError(f"Required executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        # subprocess.run terminates the child before raising. Surface the timeout
        # as a non-zero result (fail closed) with whatever partial output exists,
        # so verification checks block and Codex phases raise rather than hang.
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        marker = (
            f"\n[controller] command timed out after {effective_timeout}s "
            "and was terminated."
        )
        if check:
            raise WorkflowError(marker.strip()) from exc
        return subprocess.CompletedProcess(
            args, PROCESS_TIMEOUT_EXIT_CODE, stdout, stderr + marker
        )


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

    finding_ledger = render_finding_ledger(state)

    return {
        "FEATURE": state.get("feature", "(missing)"),
        "BASELINE": state.get("baseline", {}).get("commit", "(missing)"),
        "REPOSITORY_CONTEXT": artifact_text(
            "repository_context", "repository-context.txt"
        ),
        "CODEX_SPEC": artifact_text("enhance", "feature-spec.codex.json"),
        "ACCEPTED_SPEC": artifact_text("accepted_spec", "accepted-spec.md"),
        "ACCEPTED_PLAN": artifact_text("accepted_plan", "accepted-plan.md"),
        "VERIFICATION": json.dumps(compact_verification_view(state), indent=2),
        "PREVIOUS_REVIEW": finding_ledger,
        "LATEST_REVIEW": finding_ledger,
        "FINDING_LEDGER": finding_ledger,
        "OPEN_FINDINGS": render_open_findings(state),
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
# Phase profiles
# ---------------------------------------------------------------------------


def resolve_phase_profile(phase: str) -> dict[str, str]:
    """Resolve the effective Codex reasoning profile for a phase.

    Defaults come from PHASE_PROFILES; installations may override via the
    CLAUDE_AUTONOMOUS_PHASE_PROFILES env var (JSON object keyed by phase) and
    select a per-phase model via CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>.
    """
    profile = dict(PHASE_PROFILES.get(phase, _DEFAULT_PROFILE))
    override_raw = os.environ.get("CLAUDE_AUTONOMOUS_PHASE_PROFILES", "").strip()
    if override_raw:
        try:
            overrides = json.loads(override_raw)
        except json.JSONDecodeError:
            overrides = None
        if isinstance(overrides, dict):
            phase_override = overrides.get(phase)
            if isinstance(phase_override, dict):
                profile.update({k: str(v) for k, v in phase_override.items()})
    model_env = os.environ.get(
        f"CLAUDE_AUTONOMOUS_CODEX_MODEL_{phase.upper()}", ""
    ).strip()
    if model_env:
        profile["model"] = model_env
    return profile


def codex_profile_args(profile: dict[str, str]) -> list[str]:
    """Render a phase profile as Codex CLI arguments (`-c key=value` and `--model`)."""
    args: list[str] = []
    mapping = (
        ("reasoning", "model_reasoning_effort"),
        ("reasoning_summary", "model_reasoning_summary"),
        ("verbosity", "model_verbosity"),
    )
    for key, cfg in mapping:
        value = profile.get(key)
        if value:
            args += ["-c", f"{cfg}={value}"]
    model = profile.get("model")
    if model:
        args += ["--model", model]
    return args


# ---------------------------------------------------------------------------
# Codex usage telemetry
# ---------------------------------------------------------------------------


def parse_codex_usage(ndjson_text: str) -> dict[str, int]:
    """Best-effort extraction of token usage from Codex `--json` NDJSON events.

    Returns the last-seen input/output/total token counts when present. Unknown
    event shapes are ignored and absent token fields are acceptable (character
    counts serve as the fallback metric).
    """
    usage: dict[str, int] = {}
    aliases = (
        ("input_tokens", "input_tokens"),
        ("prompt_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
        ("total_token_usage", "total_tokens"),
    )
    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        candidates = [event]
        for key in ("usage", "info", "token_usage", "msg"):
            sub = event.get(key)
            if isinstance(sub, dict):
                candidates.append(sub)
        for cand in candidates:
            for src, dst in aliases:
                val = cand.get(src)
                if isinstance(val, int):
                    usage[dst] = val
    return usage


def parse_codex_model(ndjson_text: str) -> str | None:
    """Best-effort extraction of the concrete model id from Codex NDJSON events.

    Returns the first non-empty `model` string found (the session-configuration
    event reports the actually-selected model, including when it is inherited
    from global Codex config rather than an explicit phase profile).
    """
    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        candidates = [event]
        for key in ("msg", "info", "session", "config", "turn_context"):
            sub = event.get(key)
            if isinstance(sub, dict):
                candidates.append(sub)
        for cand in candidates:
            model = cand.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
    return None


# ---------------------------------------------------------------------------
# Workflow modes
# ---------------------------------------------------------------------------


def classify_feature_risk(feature: str) -> list[str]:
    """Return the conservative risk categories matched by the feature text."""
    text = feature.lower()
    matched: list[str] = []
    for category, patterns in MODE_RISK_PATTERNS.items():
        if any(re.search(pattern, text) for pattern in patterns):
            matched.append(category)
    return matched


def select_mode(requested: str, feature: str) -> tuple[str, list[str]]:
    """Resolve the effective workflow mode and the reasons for it.

    `auto` escalates conservatively to rigorous on any risk signal, otherwise
    standard. Explicit modes are respected verbatim; explicit rigorous is never
    downgraded.
    """
    if requested == "auto":
        risks = classify_feature_risk(feature)
        if risks:
            return "rigorous", [
                f"auto escalated to rigorous: detected {', '.join(risks)}"
            ]
        return "standard", ["auto selected standard: no high-risk signals detected"]
    return requested, [f"explicit mode requested: {requested}"]


# ---------------------------------------------------------------------------
# Compact prompt context helpers
# ---------------------------------------------------------------------------


def compact_verification_view(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Latest logical verification check per name as {name, command, exit_code}."""
    checks = latest_verification_checks(state.get("verification", {}).get("checks", []))
    return [
        {
            "name": check.get("name"),
            "command": check.get("command"),
            "exit_code": check.get("exit_code"),
        }
        for check in checks
    ]


def render_finding_ledger(state: dict[str, Any]) -> str:
    """Render the compact triage finding ledger for inclusion in review prompts."""
    ledger = state.get("review_ledger", [])
    compact: list[dict[str, Any]] = []
    for entry in ledger:
        if not isinstance(entry, dict):
            continue
        item: dict[str, Any] = {
            "fingerprint": entry.get("fingerprint"),
            "status": entry.get("status"),
        }
        if entry.get("resolution"):
            item["resolution"] = entry["resolution"]
        if entry.get("reason"):
            item["reason"] = entry["reason"]
        compact.append(item)
    if not compact:
        return "(none)"
    return json.dumps(compact, indent=2)


def render_open_findings(state: dict[str, Any]) -> str:
    """Render still-open cumulative findings with their `F-<n>` ids.

    Delta reviews must reference prior findings by `F-<n>` id in
    `resolved_findings`, but the triage ledger is keyed by fingerprint. Without
    the ids the reviewer cannot reliably resolve a prior finding, so a severe
    finding could remain open indefinitely. Surface the open findings (id +
    severity + status) so the delta reviewer can close them deterministically.
    """
    open_findings = [
        {
            "id": f.get("id"),
            "severity": f.get("severity"),
            "category": f.get("category"),
            "status": f.get("status"),
            "round": f.get("round"),
        }
        for f in state.get("cumulative_findings", [])
        if isinstance(f, dict) and f.get("status") == "open"
    ]
    if not open_findings:
        return "(none)"
    return json.dumps(open_findings, indent=2)


# ---------------------------------------------------------------------------
# Review ledger merge (full-then-delta)
# ---------------------------------------------------------------------------


_CANONICAL_FINDING_ID = re.compile(r"^F-(\d+)$")


def _index_findings(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    findings = state.get("cumulative_findings", [])
    return {f["id"]: f for f in findings if isinstance(f, dict) and "id" in f}


def _canonical_id_allocator(
    index: dict[str, dict[str, Any]], incoming_ids: list[str]
) -> Callable[[], str]:
    """Return an allocator that hands out fresh, collision-free canonical IDs.

    Duplicate finding IDs must be remapped to a real `F-<n>` id (not a synthetic
    `F-1#dup` key) so a triage entry — whose schema is `^F-[0-9]+$` — can still
    reference the remapped finding. Seed the counter past every canonical id in
    both the existing index and the incoming batch so a remapped id can never
    collide with an id that appears later in the same review.
    """
    max_n = 0
    for key in list(index) + list(incoming_ids):
        m = _CANONICAL_FINDING_ID.match(str(key))
        if m:
            max_n = max(max_n, int(m.group(1)))
    counter = {"n": max_n}

    def allocate() -> str:
        counter["n"] += 1
        candidate = f"F-{counter['n']}"
        while candidate in index:
            counter["n"] += 1
            candidate = f"F-{counter['n']}"
        return candidate

    return allocate


def migrate_cumulative_finding_ids(state: dict[str, Any]) -> None:
    """Remap legacy synthetic finding IDs (`F-1#dup..`/`F-1#r..`) to canonical IDs.

    Older runs recorded duplicate findings under unreferenceable synthetic keys.
    Rewrite each to the next free `F-<n>`, preserving the original under
    `legacy_id` and folding any `reused_id` into `source_id`. Never drop a
    finding. Idempotent: once all IDs are canonical this is a no-op.
    """
    findings = state.get("cumulative_findings")
    if not isinstance(findings, list):
        return
    max_n = 0
    has_legacy = False
    for f in findings:
        if not isinstance(f, dict):
            continue
        m = _CANONICAL_FINDING_ID.match(str(f.get("id", "")))
        if m:
            max_n = max(max_n, int(m.group(1)))
        elif str(f.get("id", "")).strip():
            has_legacy = True
    if not has_legacy:
        return
    for f in findings:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id", ""))
        if not fid.strip() or _CANONICAL_FINDING_ID.match(fid):
            continue
        max_n += 1
        f.setdefault("legacy_id", fid)
        if "reused_id" in f and "source_id" not in f:
            f["source_id"] = f.pop("reused_id")
        f["id"] = f"F-{max_n}"


def _require_finding_items(items: list[Any], context: str) -> None:
    """Fail closed if any review finding item is malformed.

    Top-level type checks cannot see inside list items, so a downgraded/partial
    Codex payload could include a `new_findings`/`findings` entry that is not a
    dict or lacks an `id`. Silently skipping such an entry would *drop* a
    potentially blocking finding (fail open). Raise instead so a malformed
    finding blocks the merge rather than vanishing.
    """
    for finding in items:
        if not isinstance(finding, dict) or not str(finding.get("id", "")).strip():
            raise WorkflowError(
                f"Codex review {context} contains a malformed finding entry "
                f"(not an object or missing id): {finding!r}; refusing to merge "
                "(fail closed)."
            )


def merge_full_review(state: dict[str, Any], parsed: dict[str, Any], round_num: int) -> None:
    """Seed the cumulative finding set from a round-1 full review."""
    migrate_cumulative_finding_ids(state)
    index = _index_findings(state)
    findings = list(parsed.get("findings", []))
    _require_finding_items(findings, "findings")
    allocate = _canonical_id_allocator(index, [str(f["id"]) for f in findings])
    for finding in findings:
        fid = finding["id"]
        # A full review can return two findings sharing one id: the schema
        # enforces id *format* (^F-[0-9]+$) but not uniqueness. Overwriting by
        # id would silently drop the first entry — potentially a severe finding —
        # from the seeded baseline (fail open). Remap the colliding entry to a
        # fresh canonical id (referenceable by triage) and record the model's
        # original id under `source_id`.
        if fid in index:
            new_id = allocate()
            index[new_id] = {
                "id": new_id,
                "severity": finding.get("severity"),
                "category": finding.get("category"),
                "status": "open",
                "round": round_num,
                "source_id": fid,
            }
            continue
        index[fid] = {
            "id": fid,
            "severity": finding.get("severity"),
            "category": finding.get("category"),
            "status": "open",
            "round": round_num,
        }
    state["cumulative_findings"] = list(index.values())


def merge_delta_review(
    state: dict[str, Any], parsed: dict[str, Any], round_num: int
) -> None:
    """Merge a round-2+ delta review into the cumulative finding set."""
    migrate_cumulative_finding_ids(state)
    index = _index_findings(state)
    new_items = list(parsed.get("new_findings", [])) + list(
        parsed.get("regressions", [])
    )
    # Fail closed on malformed nested items rather than dropping them silently.
    _require_finding_items(new_items, "new_findings/regressions")
    reintroduced_ids = {f["id"] for f in new_items}
    for fid in parsed.get("resolved_findings", []):
        # Fail closed: an id cannot be both resolved and (re)reported as a new
        # finding/regression in the same round. Leaving the original untouched
        # keeps a severe finding blocking rather than letting the contradictory
        # delta flip it to a lower-severity reintroduction and unblock the gate.
        if fid in index and fid not in reintroduced_ids:
            index[fid]["status"] = "resolved"
    allocate = _canonical_id_allocator(index, [str(f["id"]) for f in new_items])
    for finding in new_items:
        existing = index.get(finding["id"])
        # Never let a delta overwrite an existing finding (any status): doing so
        # could downgrade or drop an unresolved critical/high. Preserve the
        # original and remap the colliding report to a fresh canonical id so it
        # stays referenceable by triage, recording the model's id as `source_id`.
        if existing is not None:
            new_id = allocate()
            index[new_id] = {
                "id": new_id,
                "severity": finding.get("severity"),
                "category": finding.get("category"),
                "status": "open",
                "round": round_num,
                "source_id": finding["id"],
            }
            continue
        index[finding["id"]] = {
            "id": finding["id"],
            "severity": finding.get("severity"),
            "category": finding.get("category"),
            "status": "open",
            "round": round_num,
        }
    state["cumulative_findings"] = list(index.values())


# Triage dispositions that release a finding from blocking completion. A finding
# left `open` (or marked `requires_human_decision`) still blocks the gate.
NON_BLOCKING_TRIAGE_STATUSES = {
    "rejected",
    "rejected_with_evidence",
    "already_resolved",
    "out_of_scope_but_recorded",
    "resolved",
}

# Triage statuses that (re)assert blocking. A later triage round can escalate a
# previously closed finding back to blocking; transitions are bidirectional so a
# reclassification cannot leave a severe finding silently released.
BLOCKING_TRIAGE_STATUSES = {"open", "requires_human_decision"}


def _triage_rationale(entry: dict[str, Any]) -> str:
    """Return the recorded justification for a triage disposition, if any."""
    for key in ("reason", "evidence", "resolution", "justification"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def apply_triage_to_cumulative(
    state: dict[str, Any], entries: list[Any]
) -> None:
    """Close cumulative findings that triage dispositions release from blocking.

    A triage entry references the finding via `finding_id` (e.g. `F-1`). When its
    `status` is a non-blocking disposition, the matching cumulative finding's
    status is updated so a validly rejected high/critical finding does not keep
    `evaluate`/`next-action` looping until the review budget is exhausted.
    """
    index = _index_findings(state)
    if not index:
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        fid = entry.get("finding_id")
        status = entry.get("status")
        if fid not in index:
            continue
        finding = index[fid]
        # A blocking-intent triage status reopens a finding (the fail-safe
        # direction), so a later reclassification can re-block a previously
        # closed finding.
        if status in BLOCKING_TRIAGE_STATUSES:
            finding["status"] = "open"
            continue
        if status not in NON_BLOCKING_TRIAGE_STATUSES:
            continue
        # Closing a severe finding requires a recorded rationale so a critical
        # or high finding cannot be released from the gate by status metadata
        # alone. Non-severe findings may be closed without a written reason.
        severe = finding.get("severity") not in NON_SEVERE_SEVERITIES
        if severe and not _triage_rationale(entry):
            continue
        finding["status"] = status
    state["cumulative_findings"] = list(index.values())


# Severities the schemas treat as non-blocking. Anything else on an open
# finding (including a missing/unknown value) fails closed and keeps blocking,
# so malformed review output cannot slip an unreported issue past the gate.
NON_SEVERE_SEVERITIES = {"low", "medium"}


def cumulative_unresolved_severe(state: dict[str, Any]) -> list[dict[str, Any]]:
    # Fail closed in two directions:
    #   * a non-dict entry has no readable status/severity, so treat it as an
    #     unresolved severe finding rather than silently skipping it; and
    #   * a severe finding blocks unless it carries an explicitly-released status
    #     (NON_BLOCKING_TRIAGE_STATUSES, e.g. `resolved`/`rejected`). A missing or
    #     unknown status must NOT be read as "not open" — otherwise a malformed or
    #     migrated finding like {"id": "F-1", "severity": "critical"} (no status)
    #     would slip past the gate.
    severe: list[dict[str, Any]] = []
    for f in state.get("cumulative_findings", []):
        if not isinstance(f, dict):
            severe.append({"id": "(malformed)", "status": "open", "severity": "high"})
            continue
        is_severe = f.get("severity") not in NON_SEVERE_SEVERITIES
        released = f.get("status") in NON_BLOCKING_TRIAGE_STATUSES
        if is_severe and not released:
            severe.append(f)
    return severe


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

    # jsonschema is a declared runtime dependency: every Codex output, the
    # reconciliation source/decisions, and the triage ledger are validated
    # against the bundled schemas before they can affect run state. Without it
    # those structural gates cannot run, so surface a missing install here.
    try:
        import jsonschema  # noqa: F401

        jsonschema_version = getattr(jsonschema, "__version__", "unknown")
        print(f"jsonschema: {jsonschema_version}")
    except ImportError:
        print("jsonschema: not found")
        failures.append(
            "jsonschema is not installed; install the package dependencies "
            "(e.g. `pip install -e .` or `pip install 'jsonschema>=4.18'`)"
        )

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

    # Serialize the active-run check and run creation per repository. With
    # generated IDs two concurrent inits pick different run IDs, so the per-run
    # RunStateLock cannot serialize them; without this repo-level lock both could
    # observe "no active run" and each create one. The loser sees the winner's
    # active run and fails closed (unless --force authorizes an additional run).
    with RepoInitLock(state_home, repo.id):
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

        # Never overwrite an existing run, active or terminal. `--force` may create
        # an additional run while another is active (handled above); it must not
        # authorize clobbering an existing run ID's state.
        if (run_dir / "run-state.json").exists():
            raise WorkflowError(
                f"A run with ID {run_id!r} already exists at {run_dir}. Refusing to "
                "overwrite it. Use a different --run-id, `--reuse` to continue an "
                "active run, or `archive-run`/`list-runs` to manage existing runs."
            )

        with RunStateLock(run_dir):
            # Re-check under the lock to close the TOCTOU window against a concurrent
            # init creating the same run ID first.
            if (run_dir / "run-state.json").exists():
                raise WorkflowError(
                    f"A run with ID {run_id!r} already exists at {run_dir}. "
                    "Refusing to overwrite it."
                )
            run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

            ctx_text = repository_context(repo)
            (run_dir / "feature-request.md").write_text(
                feature + "\n", encoding="utf-8"
            )
            (run_dir / "repository-context.txt").write_text(
                ctx_text, encoding="utf-8"
            )

            dirty = git(
                repo.canonical_root, "status", "--short", check=False
            ).splitlines()

            requested_mode = getattr(args, "mode", "auto")
            effective_mode, mode_reasons = select_mode(requested_mode, feature)
            # `auto`/explicit rigorous runs are safety-sensitive: require adversarial.
            risk_reasons: list[str] = []
            requires_adversarial = effective_mode == "rigorous"
            if requires_adversarial:
                risk_reasons.append(
                    f"{effective_mode} mode selected (requested={requested_mode})"
                )

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
                "requested_mode": requested_mode,
                "effective_mode": effective_mode,
                "mode_reasons": mode_reasons,
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
                "cumulative_findings": [],
                "review_ledger": [],
                "codex_runs": [],
                "risk": {
                    "requires_adversarial_review": requires_adversarial,
                    "reasons": risk_reasons,
                },
                "notes": [],
            }
            save_run_state(run_dir, state)

        # Save repo metadata while still holding RepoInitLock. Two concurrent
        # `init --force` processes share this one metadata.json; updating it
        # outside the lock would let their read-modify-write cycles interleave
        # and lose one update (or observe a half-written file). Keeping it inside
        # the repository-level lock serializes the metadata mutation too.
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
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="codex"
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

    is_delta_review = False
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
        # Round 1 is a full review; rounds 2+ use the compact delta schema/prompt.
        # A delta review only carries forward findings relative to a recorded
        # full-review baseline (which seeds cumulative_findings). If no full
        # review has ever been recorded for this run (e.g. a run migrated from an
        # older state, or whose round-1 artifact was lost), a delta `pass` with
        # no new findings could clear the gate without any baseline of severe
        # findings ever being established. Require a recorded full review before
        # allowing delta mode; otherwise fall back to a full review that re-seeds
        # the cumulative ledger.
        has_full_review = any(
            isinstance(r, dict) and r.get("delta") is False
            for r in state.get("reviews", [])
        )
        is_delta_review = next_round >= 2 and has_full_review
        if is_delta_review:
            prompt_rel = REVIEW_DELTA_PROMPT
            schema_rel = REVIEW_DELTA_SCHEMA
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
    # Stage Codex output/events under invocation-unique names so a concurrent or
    # overlapping retry of the same phase cannot clobber this invocation's
    # artifacts; the canonical round files are published under the lock below.
    stage_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    output_path = run_dir / f".staging-{stage_id}.codex.json"

    profile = resolve_phase_profile(phase)
    command = [
        "codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--output-schema",
        str(PLUGIN_ROOT / schema_rel),
        "--output-last-message",
        str(output_path),
        *codex_profile_args(profile),
        "-",
    ]
    started_at = utc_now()
    started_monotonic = time.monotonic()
    result = run_process(
        command,
        cwd=repo.canonical_root,
        input_text=prompt,
        timeout=getattr(args, "timeout", None),
    )
    duration_seconds = round(time.monotonic() - started_monotonic, 1)

    # Canonical name (under the lock) for the raw NDJSON event stream; the
    # staging write happens inside the guarded block below so a write failure
    # (disk full / permission) triggers the same deterministic cleanup as any
    # later failure instead of orphaning the already-written Codex output.
    events_path = run_dir / f".staging-{stage_id}.events.ndjson"

    if result.returncode != 0:
        error_path = run_dir / f"{phase}.codex.stderr.log"
        error_path.write_text(result.stderr, encoding="utf-8")
        for staged in (output_path, events_path):
            staged.unlink(missing_ok=True)
        with RunStateLock(run_dir):
            err_state = load_run_state(run_dir)
            err_state.setdefault("notes", []).append(
                f"Codex {phase} failed; see {make_relative_path(error_path, run_dir)}"
            )
            save_run_state(run_dir, err_state)
        raise WorkflowError(result.stderr.strip() or f"Codex {phase} failed")

    # Any failure after this point (NDJSON staging, parse, schema validation,
    # locked merge) must not leave the invocation-unique staging files on disk:
    # they hold the raw prompt response / NDJSON event stream and would otherwise
    # accumulate and retain sensitive content across retries. Success publishes
    # them to canonical names under the lock (so `published` is set only once
    # that completes).
    staged_output, staged_events = output_path, events_path
    published = False
    try:
        events_path.write_text(result.stdout, encoding="utf-8")
        try:
            output_text = output_path.read_text(encoding="utf-8")
            parsed = json.loads(output_text)
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                f"Codex did not produce valid JSON at {output_path}: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise WorkflowError(f"Codex output must be an object: {output_path}")
        # Full Draft 2020-12 schema validation. Fail closed if a (e.g. downgraded)
        # Codex CLI returns syntactically valid but schema-violating output: a
        # delta review missing `new_findings`, a mistyped `new_findings: {}`, or a
        # finding with an out-of-enum severity must all be rejected before the
        # cumulative merge rather than silently mis-merged. This validates nested
        # finding/criterion items too, not just top-level keys.
        try:
            validate_payload(
                parsed, schema_rel, label=f"Codex {phase} output at {output_path}"
            )
        except SchemaValidationError as exc:
            raise WorkflowError(str(exc)) from exc
        # Defense in depth before publishing the canonical artifact under the lock:
        # the cumulative merge also fails closed on malformed finding entries, but
        # checking here keeps a malformed payload from being renamed to its
        # canonical name and left behind when the merge subsequently rejects it.
        if phase == "review":
            if is_delta_review:
                _require_finding_items(
                    list(parsed.get("new_findings", []))
                    + list(parsed.get("regressions", [])),
                    "new_findings/regressions",
                )
            else:
                _require_finding_items(list(parsed.get("findings", [])), "findings")

        token_usage = parse_codex_usage(result.stdout)
        # Prefer the concrete model reported by Codex; fall back to the explicit
        # profile model, then a placeholder when the model is inherited from config.
        recorded_model = (
            parse_codex_model(result.stdout) or profile.get("model") or "(default)"
        )

        # Recompute round/index from fresh state inside the lock to close the
        # TOCTOU gap between the pre-Codex snapshot check and the post-Codex write.
        final_path = output_path
        with RunStateLock(run_dir):
            state = load_run_state(run_dir)
            # Status was checked before the (long) Codex execution. A concurrent
            # `cancel`/`block` may have driven the run to a terminal state while
            # Codex ran; merging now would append reviews/findings and reset the
            # phase, effectively resurrecting a cancelled/blocked run. Re-check
            # under the lock and fail closed so terminal decisions stick.
            if state.get("status") != "active":
                raise WorkflowError(
                    "Run is no longer active "
                    f"(status={state.get('status')!r}); refusing to merge "
                    f"{phase} output produced before the status change."
                )
            phase_label = phase
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
                    raise WorkflowError(
                        f"Maximum review rounds exhausted ({maximum})"
                    )
                # The round mode (full vs delta) and the schema/prompt used to
                # drive Codex were chosen before acquiring the lock, from a
                # pre-lock round snapshot. If a concurrent same-run invocation
                # advanced the round in between, the locked round may no longer
                # match the mode this payload was produced under. Merging a full
                # review as a delta (or vice versa) silently corrupts the
                # cumulative ledger, so fail closed instead.
                has_full_review = any(
                    isinstance(r, dict) and r.get("delta") is False
                    for r in state.get("reviews", [])
                )
                expected_delta = next_round >= 2 and has_full_review
                if expected_delta != is_delta_review:
                    raise WorkflowError(
                        "Review round-mode mismatch (concurrent invocation?): "
                        f"payload was produced as a "
                        f"{'delta' if is_delta_review else 'full'} review but "
                        f"round {next_round} under the lock requires a "
                        f"{'delta' if expected_delta else 'full'} review; "
                        "refusing to merge with inconsistent semantics."
                    )
                canonical = run_dir / f"review-{next_round:02d}.codex.json"
                if output_path != canonical:
                    output_path.replace(canonical)
                final_path = canonical
                phase_label = f"review-{next_round:02d}"
            elif phase == "adversarial":
                index = len(state.get("adversarial_reviews", [])) + 1
                canonical = run_dir / f"adversarial-{index:02d}.codex.json"
                if output_path != canonical:
                    output_path.replace(canonical)
                final_path = canonical
                phase_label = f"adversarial-{index:02d}"
            else:
                # Static-name phases (enhance/plan): publish the staged output to
                # the fixed canonical name under the lock.
                canonical = run_dir / output_name
                if output_path != canonical:
                    output_path.replace(canonical)
                final_path = canonical
            # Keep the events artifact name aligned with the canonical round so the
            # recorded `events_artifact` cannot be misattributed if the round
            # number changed between the pre-Codex snapshot and this locked write.
            events_canonical = (
                run_dir / f"{final_path.stem.replace('.codex', '')}.events.ndjson"
            )
            if events_path != events_canonical and events_path.exists():
                events_path.replace(events_canonical)
                events_path = events_canonical
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
                        "delta": is_delta_review,
                    }
                )
                if is_delta_review:
                    merge_delta_review(state, parsed, next_round)
                else:
                    merge_full_review(state, parsed, next_round)
            elif phase == "adversarial":
                state["phase"] = "adversarially-reviewed"
                state.setdefault("adversarial_reviews", []).append(
                    {
                        "round": index,
                        "path": make_relative_path(final_path, run_dir),
                        "verdict": parsed.get("verdict"),
                    }
                )

            usage_record: dict[str, Any] = {
                "phase": phase_label,
                "prompt_characters": len(prompt),
                "output_characters": len(output_text),
                "duration_seconds": duration_seconds,
                "model": recorded_model,
                "reasoning_effort": profile.get("reasoning"),
                "verbosity": profile.get("verbosity"),
                "started_at": started_at,
                "events_artifact": make_relative_path(events_path, run_dir),
                "output_artifact": make_relative_path(final_path, run_dir),
            }
            if token_usage:
                usage_record["tokens"] = token_usage
            state.setdefault("codex_runs", []).append(usage_record)

            state["stop_gate_blocks"] = 0
            save_run_state(run_dir, state)
        published = True
    finally:
        # On any failure before the locked publish completes, remove the
        # invocation-unique staging files so partial/invalid prompt responses and
        # event streams are not retained on disk across retries.
        if not published:
            for staged in (staged_output, staged_events):
                staged.unlink(missing_ok=True)
    print(final_path)
    return 0


# ---------------------------------------------------------------------------
# cmd_accept
# ---------------------------------------------------------------------------


def _decision_maps(
    decisions: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    reject_map: dict[str, str] = {}
    for entry in decisions.get("reject", []):
        if isinstance(entry, dict) and "id" in entry:
            reject_map[str(entry["id"])] = str(entry.get("reason", ""))
    modify_map: dict[str, str] = {}
    for entry in decisions.get("modify", []):
        if isinstance(entry, dict) and "id" in entry:
            modify_map[str(entry["id"])] = str(entry.get("replacement", ""))
    return reject_map, modify_map


def _apply_decisions_to_items(
    items: list[Any],
    id_key: str,
    text_key: str,
    reject_map: dict[str, str],
    modify_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Keep each item unless explicitly rejected; apply text modifications.

    Items that are neither rejected nor modified are accepted verbatim. This
    keeps the accepted artifact complete by default, reducing accidental omission.
    """
    kept: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        iid = str(item.get(id_key, ""))
        if iid in reject_map:
            continue
        new_item = dict(item)
        if iid in modify_map:
            new_item[text_key] = modify_map[iid]
        kept.append(new_item)
    return kept


def _render_spec_markdown(a: dict[str, Any]) -> str:
    lines = [f"# Accepted specification — {a.get('title', '')}".rstrip(), ""]
    if a.get("problem_statement"):
        lines += ["## Problem statement", "", a["problem_statement"], ""]
    lines += ["## Functional requirements", ""]
    for fr in a["functional_requirements"]:
        priority = fr.get("priority")
        suffix = f" ({priority})" if priority else ""
        lines.append(f"- **{fr.get('id', '')}**{suffix}: {fr.get('requirement', '')}")
    lines += ["", "## Acceptance criteria", ""]
    for ac in a["acceptance_criteria"]:
        lines.append(f"- **{ac.get('id', '')}**: {ac.get('criterion', '')}")
    if a.get("non_functional_requirements"):
        lines += ["", "## Non-functional requirements", ""]
        lines += [f"- {n}" for n in a["non_functional_requirements"]]
    if a.get("non_goals"):
        lines += ["", "## Non-goals", ""]
        lines += [f"- {n}" for n in a["non_goals"]]
    if a.get("added"):
        lines += ["", "## Added during reconciliation", ""]
        lines += [f"- {json.dumps(item, sort_keys=True)}" for item in a["added"]]
    if a.get("rejected"):
        lines += ["", "## Rejected (with reasons)", ""]
        lines += [f"- {r['id']}: {r['reason']}" for r in a["rejected"]]
    return "\n".join(lines) + "\n"


def _render_plan_markdown(a: dict[str, Any]) -> str:
    lines = ["# Accepted implementation plan", ""]
    if a.get("summary"):
        lines += [a["summary"], ""]
    lines += ["## Steps", ""]
    for step in a["implementation_steps"]:
        files = step.get("files") or []
        files_str = f" (files: {', '.join(files)})" if files else ""
        lines.append(
            f"{step.get('order', '?')}. **{step.get('id', '')}** "
            f"{step.get('description', '')}{files_str}"
        )
    if a.get("added"):
        lines += ["", "## Added during reconciliation", ""]
        lines += [f"- {json.dumps(item, sort_keys=True)}" for item in a["added"]]
    if a.get("rejected"):
        lines += ["", "## Rejected (with reasons)", ""]
        lines += [f"- {r['id']}: {r['reason']}" for r in a["rejected"]]
    return "\n".join(lines) + "\n"


def _source_item_ids(kind: str, source: dict[str, Any]) -> set[str]:
    """Collect the ids a reconciliation delta may legitimately reference."""
    ids: set[str] = set()
    if kind == "spec":
        for key in ("functional_requirements", "acceptance_criteria"):
            for item in source.get(key, []):
                if isinstance(item, dict) and "id" in item:
                    ids.add(str(item["id"]))
    else:
        for step in source.get("implementation_steps", []):
            if isinstance(step, dict):
                ids.add(str(step.get("id", f"S{step.get('order', '?')}")))
    return ids


def _validate_decision_ids(
    kind: str, source: dict[str, Any], decisions: dict[str, Any]
) -> None:
    """Fail closed when accept/reject/modify target ids absent from the source.

    A silent typo (e.g. `AC-21` for `AC-12`) would otherwise leave the intended
    change unapplied while the materialized artifact looks complete.
    """
    valid = _source_item_ids(kind, source)
    referenced: list[str] = []
    for entry in decisions.get("accept", []):
        referenced.append(str(entry))
    for key in ("reject", "modify"):
        for entry in decisions.get(key, []):
            if isinstance(entry, dict) and "id" in entry:
                referenced.append(str(entry["id"]))
    unknown = sorted({rid for rid in referenced if rid not in valid})
    if unknown:
        raise WorkflowError(
            "Reconciliation decisions reference unknown source id(s): "
            f"{', '.join(unknown)}. Known ids: {', '.join(sorted(valid)) or '(none)'}"
        )


def _validate_decision_shape(decisions: dict[str, Any]) -> None:
    """Fail closed on malformed decision containers/entries.

    A directive supplied with the wrong shape (e.g. ``"reject": "FR-3"`` instead
    of a list of objects) would otherwise be silently skipped, leaving the
    intended change unapplied while the materialized artifact still looks
    complete.
    """
    for key in ("accept", "reject", "modify", "add"):
        if key in decisions and not isinstance(decisions[key], list):
            raise WorkflowError(
                f"Reconciliation decisions field '{key}' must be a list, got "
                f"{type(decisions[key]).__name__}."
            )
    for entry in decisions.get("accept", []):
        if not isinstance(entry, (str, int)):
            raise WorkflowError(
                "Each 'accept' entry must be an id scalar, got "
                f"{type(entry).__name__}."
            )
    required_field = {"reject": "reason", "modify": "replacement"}
    for key in ("reject", "modify"):
        field = required_field[key]
        for entry in decisions.get(key, []):
            if not isinstance(entry, dict) or "id" not in entry:
                raise WorkflowError(
                    f"Each '{key}' entry must be an object with an 'id'; got "
                    f"{json.dumps(entry)[:80]}."
                )
            value = entry.get(field)
            # Fail closed: a `modify` without a `replacement` would otherwise
            # blank the accepted item text; a `reject` without a `reason` would
            # leave an unauditable rejection.
            if not isinstance(value, str) or not value.strip():
                raise WorkflowError(
                    f"'{key}' entry for id {entry['id']!r} requires a non-empty "
                    f"'{field}'."
                )


def _validate_source_sections(kind: str, source: dict[str, Any]) -> None:
    """Fail closed when the reconciliation source is missing or malformed.

    Guards against pointing `accept --source` at the wrong/malformed JSON, which
    would otherwise materialize an empty accepted spec/plan and silently weaken
    downstream review against a blank contract. A non-empty section whose items
    are not objects is just as dangerous: _apply_decisions_to_items (and the
    plan-step filter) silently drop non-dict entries, so a section of bare
    strings would pass a length check yet materialize to nothing. Reject such
    items loudly rather than producing a blank contract.
    """
    primary = "functional_requirements" if kind == "spec" else "implementation_steps"
    section = source.get(primary)
    if not isinstance(section, list) or not section:
        raise WorkflowError(
            f"Reconciliation source for kind '{kind}' must contain a non-empty "
            f"'{primary}' section; refusing to materialize a blank accepted artifact."
        )
    # Validate item shape for every section that materialize_acceptance feeds
    # through the (silently-dropping) item filters. acceptance_criteria is
    # optional, so only validate it when present; the primary section is always
    # checked because it must be non-empty.
    item_sections = (
        ("functional_requirements", "acceptance_criteria")
        if kind == "spec"
        else ("implementation_steps",)
    )
    for key in item_sections:
        value = source.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise WorkflowError(
                f"Reconciliation source '{key}' must be a list when present."
            )
        for item in value:
            if not isinstance(item, dict):
                raise WorkflowError(
                    f"Each '{key}' entry must be an object; got "
                    f"{json.dumps(item)[:80]} — refusing to silently drop it from "
                    "the accepted artifact (fail closed)."
                )


def materialize_acceptance(
    kind: str, source: dict[str, Any], decisions: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Deterministically materialize an accepted spec/plan from a reconciliation delta."""
    _validate_source_sections(kind, source)
    # Structural validation against the bundled decision schema first (rejects
    # unknown keys / mistyped containers), then the semantic checks below which
    # enforce non-empty reasons/replacements with targeted messages.
    try:
        validate_payload(
            decisions,
            "schemas/accept-decisions.schema.json",
            label="Reconciliation decisions",
        )
    except SchemaValidationError as exc:
        raise WorkflowError(str(exc)) from exc
    _validate_decision_shape(decisions)
    _validate_decision_ids(kind, source, decisions)
    reject_map, modify_map = _decision_maps(decisions)
    rejected = [{"id": k, "reason": v} for k, v in sorted(reject_map.items())]
    added = list(decisions.get("add", []))

    if kind == "spec":
        frs = _apply_decisions_to_items(
            source.get("functional_requirements", []),
            "id",
            "requirement",
            reject_map,
            modify_map,
        )
        acs = _apply_decisions_to_items(
            source.get("acceptance_criteria", []),
            "id",
            "criterion",
            reject_map,
            modify_map,
        )
        accepted = {
            "kind": "spec",
            "title": source.get("title", ""),
            "problem_statement": source.get("problem_statement", ""),
            "functional_requirements": frs,
            "acceptance_criteria": acs,
            "non_functional_requirements": source.get(
                "non_functional_requirements", []
            ),
            "non_goals": source.get("non_goals", []),
            "added": added,
            "rejected": rejected,
            "decisions": decisions,
        }
        return accepted, _render_spec_markdown(accepted)

    steps_src: list[dict[str, Any]] = []
    for step in source.get("implementation_steps", []):
        if isinstance(step, dict):
            enriched = dict(step)
            enriched.setdefault("id", f"S{enriched.get('order', '?')}")
            steps_src.append(enriched)
    steps = _apply_decisions_to_items(
        steps_src, "id", "description", reject_map, modify_map
    )
    accepted = {
        "kind": "plan",
        "summary": source.get("summary", ""),
        "implementation_steps": steps,
        "added": added,
        "rejected": rejected,
        "decisions": decisions,
    }
    return accepted, _render_plan_markdown(accepted)


def _resolve_source_path(
    source_arg: str, run_dir: Path, label: str = "Source artifact"
) -> Path:
    # Resolve against the run directory first so a bare artifact filename (the
    # form the skill recommends, e.g. `implementation-plan.codex.json`) always
    # binds to this run's artifact rather than a same-named file shadowing it
    # from the current working directory / repository — which could otherwise
    # feed an unintended source into acceptance and suppress risk/adversarial
    # gating. An absolute path (the form the skill uses for orchestrator-authored
    # decisions/triage ledgers, e.g. /tmp/claude/...) still binds to that literal
    # path because `run_dir / "/abs"` yields the absolute path, so this run-dir
    # preference is shadow-safe without breaking the documented workflow.
    in_run = run_dir / source_arg
    if in_run.is_file():
        return in_run.resolve()
    candidate = Path(source_arg)
    if candidate.is_file():
        return candidate.resolve()
    raise WorkflowError(f"{label} not found: {source_arg}")


def cmd_accept(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="accept"
    )
    state = run_ref.state
    run_dir = run_ref.run_dir
    run_id = run_ref.run_id

    require_no_unsafe_drift(state, repo)

    kind = args.kind
    md_name = "accepted-spec.md" if kind == "spec" else "accepted-plan.md"
    json_name = "accepted-spec.json" if kind == "spec" else "accepted-plan.json"
    destination = run_dir / md_name

    # Build the artifact content in memory OUTSIDE the lock. Nothing is written to
    # a canonical path here, so a failure (bad input, cancellation) cannot leave a
    # half-written accepted-spec.md or a stale accepted-spec.json behind.
    md_text: str
    json_text: str | None = None
    if getattr(args, "decisions", None):
        if not getattr(args, "source", None):
            raise WorkflowError("--decisions requires --source <codex-json>")
        source_path = _resolve_source_path(args.source, run_dir)
        decisions_path = _resolve_source_path(
            args.decisions, run_dir, label="Decisions file"
        )
        try:
            source_obj = json.loads(source_path.read_text(encoding="utf-8"))
            decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowError(
                f"Cannot read structured acceptance inputs: {exc}"
            ) from exc
        if not isinstance(source_obj, dict) or not isinstance(decisions, dict):
            raise WorkflowError("Source and decisions must each be a JSON object")
        # Fully validate the structured acceptance source against its phase
        # schema before materializing: the accepted artifact becomes the contract
        # that downstream review and the completion gate are judged against, so a
        # malformed/wrong-shaped source (mistyped sections, bad FR-/AC- ids,
        # missing evidence) must be rejected, not silently degraded.
        source_schema = (
            "schemas/enhanced-idea.schema.json"
            if kind == "spec"
            else "schemas/implementation-plan.schema.json"
        )
        try:
            validate_payload(
                source_obj,
                source_schema,
                label=f"Reconciliation source for kind '{kind}'",
            )
        except SchemaValidationError as exc:
            raise WorkflowError(str(exc)) from exc
        accepted_obj, md_text = materialize_acceptance(kind, source_obj, decisions)
        json_text = json.dumps(accepted_obj, indent=2, sort_keys=True) + "\n"
    else:
        if not getattr(args, "file", None):
            raise WorkflowError("Provide either --file or --source with --decisions")
        source = Path(args.file).resolve()
        if not source.is_file():
            raise WorkflowError(f"Accepted artifact does not exist: {source}")
        md_text = source.read_text(encoding="utf-8")

    accepted_risks = classify_feature_risk(md_text)

    # Stage each artifact to an invocation-unique temp path on the same filesystem
    # so it can be published with an atomic os.replace under the lock.
    stage_token = uuid.uuid4().hex
    staged: list[tuple[Path, Path]] = []  # (temp, canonical)
    md_tmp = run_dir / f".{md_name}.{stage_token}.tmp"
    md_tmp.write_text(md_text, encoding="utf-8")
    staged.append((md_tmp, destination))
    if json_text is not None:
        json_tmp = run_dir / f".{json_name}.{stage_token}.tmp"
        json_tmp.write_text(json_text, encoding="utf-8")
        staged.append((json_tmp, run_dir / json_name))

    try:
        with RunStateLock(run_dir):
            state = load_run_state(run_dir)
            require_active_run_state(state, run_id, "accept")
            # Publish all artifacts and the state as one all-or-nothing unit. Each
            # canonical file that already exists is moved to an invocation-unique
            # backup before being overwritten; on ANY failure (a later
            # os.replace, the state save) every published artifact is rolled back
            # to its pre-accept bytes, so a partial publication can never leave the
            # canonical artifacts and run state inconsistent.
            published: list[tuple[Path, Path | None]] = []  # (canonical, backup|None)
            try:
                for tmp_path, canonical_path in staged:
                    backup: Path | None = None
                    if canonical_path.exists():
                        backup = canonical_path.with_name(
                            f".{canonical_path.name}.{stage_token}.bak"
                        )
                        os.replace(canonical_path, backup)
                    # Record before the publish replace so rollback can undo even
                    # if this replace itself fails after the backup move.
                    published.append((canonical_path, backup))
                    os.replace(tmp_path, canonical_path)
                state.setdefault("artifacts", {})[f"accepted_{kind}"] = md_name
                if json_text is not None:
                    state["artifacts"][f"accepted_{kind}_json"] = json_name
                state["phase"] = "spec-accepted" if kind == "spec" else "plan-accepted"
                # Risk is sticky upward: if the accepted artifact reveals high-risk
                # scope that the initial feature text did not, escalate the
                # adversarial gate. Never downgrade an already-required gate here.
                risk = state.setdefault("risk", {})
                if accepted_risks and not risk.get("requires_adversarial_review"):
                    risk["requires_adversarial_review"] = True
                    risk.setdefault("reasons", []).append(
                        f"accepted {kind} escalated to rigorous: detected "
                        f"{', '.join(accepted_risks)}"
                    )
                state["stop_gate_blocks"] = 0
                save_run_state(run_dir, state)
            except BaseException:
                # Roll back published artifacts to their pre-accept state. The run
                # state file is written atomically (temp+replace), so a failed save
                # leaves the prior state intact; restoring the artifacts therefore
                # restores full artifact/state consistency.
                for canonical_path, backup in reversed(published):
                    if backup is not None:
                        if backup.exists():
                            os.replace(backup, canonical_path)
                    else:
                        canonical_path.unlink(missing_ok=True)
                raise
            else:
                for _canonical, backup in published:
                    if backup is not None:
                        backup.unlink(missing_ok=True)
    finally:
        for tmp_path, _ in staged:
            tmp_path.unlink(missing_ok=True)
    print(destination)
    return 0


# ---------------------------------------------------------------------------
# cmd_run_check
# ---------------------------------------------------------------------------


def cmd_run_check(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="run-check"
    )
    run_dir = run_ref.run_dir
    run_id = run_ref.run_id

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
    started_monotonic = time.monotonic()
    result = run_process(
        command, cwd=repo.canonical_root, timeout=getattr(args, "timeout", None)
    )
    duration_seconds = round(time.monotonic() - started_monotonic, 1)
    completed = utc_now()

    # Acquire lock to compute a collision-free index and persist atomically.
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        # TOCTOU guard: a `cancel`/`block` may have driven the run terminal while
        # this (possibly long) check ran. Publishing now would resurrect it.
        require_active_run_state(state, run_id, "run-check")
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
            "duration_seconds": duration_seconds,
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

    output_mode = getattr(args, "output", "summary")
    command_str = " ".join(command)
    if output_mode == "full":
        # Full troubleshooting output: replay the complete streams.
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        print(f"\nVerification log: {log_path}", file=sys.stderr)
    elif result.returncode == 0:
        print(f"✓ {args.name} passed in {duration_seconds} s")
        print(f"  command: {command_str}")
        print(f"  full log: {log_path}")
    else:
        tail_n = max(0, int(getattr(args, "failure_tail_lines", 80)))
        combined_streams = f"{result.stdout}{result.stderr}"
        tail_lines = combined_streams.splitlines()[-tail_n:] if tail_n else []
        print(
            f"✗ {args.name} failed with exit code {result.returncode}",
            file=sys.stderr,
        )
        print(f"  command: {command_str}", file=sys.stderr)
        if tail_lines:
            print(f"  showing final {len(tail_lines)} lines", file=sys.stderr)
            for line in tail_lines:
                print(f"  {line}", file=sys.stderr)
        print(f"  full log: {log_path}", file=sys.stderr)
    return result.returncode


# ---------------------------------------------------------------------------
# cmd_set_phase
# ---------------------------------------------------------------------------


def cmd_set_phase(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="set-phase"
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        require_active_run_state(state, run_ref.run_id, "set-phase")
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
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="set-risk"
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        require_active_run_state(state, run_ref.run_id, "set-risk")
        require_no_unsafe_drift(state, repo)
        risk = state.setdefault("risk", {})
        currently_required = bool(risk.get("requires_adversarial_review"))
        # Monotonic-upward gate: once adversarial review is required (auto/rigorous
        # escalation or a prior explicit request), set-risk may raise the bar but
        # must not silently lower it. Otherwise a high-risk run could be downgraded
        # post-init and complete without the mandatory adversarial review.
        if currently_required and not args.require_adversarial:
            raise WorkflowError(
                "Refusing to clear requires_adversarial_review: the adversarial "
                "review gate is monotonic-upward once set, so a high-risk run "
                "cannot be downgraded past the adversarial completion gate."
            )
        risk["requires_adversarial_review"] = args.require_adversarial
        if args.reason:
            risk.setdefault("reasons", []).append(args.reason)
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)
    return 0


# ---------------------------------------------------------------------------
# cmd_evaluate
# ---------------------------------------------------------------------------


def cmd_evaluate(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="evaluate"
    )
    run_dir = run_ref.run_dir
    run_id = run_ref.run_id
    # Drift check uses snapshot; git state is outside our file lock anyway.
    require_no_unsafe_drift(run_ref.state, repo)

    # Rebuild all gate conditions from freshly-loaded state AND current
    # filesystem state inside the lock so that completion_gate_failures reflects
    # current reality. Checking artifact existence outside the lock would let a
    # deletion between the check and the commit mark a run complete with a stale
    # (fail-open) artifact result, so the existence check lives under the lock.
    reasons: list[str] = []
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        # A concurrent cancel/block may have made the run terminal after
        # resolution; evaluate must never flip a terminal run to complete/active.
        require_active_run_state(state, run_id, "evaluate")

        for artifact_key, filename in (
            ("accepted_spec", "accepted-spec.md"),
            ("accepted_plan", "accepted-plan.md"),
        ):
            rel = state.get("artifacts", {}).get(artifact_key, filename)
            try:
                path = resolve_artifact_path(str(rel), run_dir)
            except StateError:
                path = run_dir / filename
            if not path.exists():
                reasons.append(f"Missing {filename}")

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
            # Prefer the cumulative finding ledger (full-then-delta reviews); fall
            # back to the latest review object only when no ledger exists. A
            # truthy-but-malformed (non-list) ledger is scanned too — the scan
            # flags non-dict entries as severe — so a corrupted ledger fails
            # closed rather than being mistaken for "no findings".
            if state.get("cumulative_findings"):
                severe = cumulative_unresolved_severe(state)
            else:
                severe = unresolved_severe_findings(review)
            if severe:
                reasons.append(
                    f"{len(severe)} unresolved critical/high finding(s) in review ledger"
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
        run_ref = resolve_run_for_inspection(
            state_home, repo.id, repo.canonical_root, run_id_override
        )
        runs = [run_ref]
    else:
        active = find_active_runs(state_home, repo.id)
        if not active:
            # Read-only: fall back to the most recent run (terminal included) or
            # legacy state so `status` still works after a run completes.
            run_ref = resolve_run_for_inspection(
                state_home, repo.id, repo.canonical_root, None
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
    run_ref = resolve_run_for_transition(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        assert_transition_allowed(state.get("status"), "cancel", run_ref.run_id)
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
    run_ref = resolve_run_for_transition(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        assert_transition_allowed(state.get("status"), "block", run_ref.run_id)
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
    run_ref = resolve_run_for_inspection(
        state_home, repo.id, repo.canonical_root, run_id
    )

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
    run_ref = resolve_run_for_transition(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    run_id_str = run_ref.run_id
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        # Idempotent: re-archiving an archived run is a no-op that must not alter
        # any other data.
        if state.get("status") == "archived":
            print(f"Run {run_id_str!r} is already archived.")
            return 0
        assert_transition_allowed(state.get("status"), "archive-run", run_id_str)
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
    run_ref = resolve_run_for_active_mutation(
        state_home,
        repo.id,
        repo.canonical_root,
        run_id_override,
        operation="accept-drift",
    )
    run_dir = run_ref.run_dir
    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        require_active_run_state(state, run_ref.run_id, "accept-drift")
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
# cmd_usage_report
# ---------------------------------------------------------------------------


def cmd_usage_report(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    # Read-only: resolve the most recent run when none is active so the usage
    # report (FR-1) remains viewable after the run completes.
    run_ref = resolve_run_for_inspection(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    runs = run_ref.state.get("codex_runs", [])

    if getattr(args, "json", False):
        print(json.dumps(runs, indent=2))
        return 0

    header = (
        f"{'Phase':<16}{'Prompt chars':>14}{'Output chars':>14}{'Duration':>12}"
    )
    print(header)
    print("-" * len(header))
    for record in runs:
        phase = str(record.get("phase", ""))[:16]
        prompt_chars = f"{int(record.get('prompt_characters', 0)):,}"
        output_chars = f"{int(record.get('output_characters', 0)):,}"
        duration = record.get("duration_seconds")
        duration_str = f"{duration} s" if duration is not None else "-"
        print(f"{phase:<16}{prompt_chars:>14}{output_chars:>14}{duration_str:>12}")
    if not runs:
        print("(no Codex phases recorded yet)")
    return 0


# ---------------------------------------------------------------------------
# cmd_triage
# ---------------------------------------------------------------------------


def cmd_triage(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_active_mutation(
        state_home, repo.id, repo.canonical_root, run_id_override, operation="triage"
    )
    run_dir = run_ref.run_dir

    file_path = _resolve_source_path(args.file, run_dir, label="Triage ledger")
    try:
        entries = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"Cannot read triage ledger: {exc}") from exc
    # Validate the whole ledger BEFORE any disposition is applied: a malformed
    # entry (missing fingerprint, unknown status) must not partially close
    # cumulative findings or release the completion gate.
    try:
        validate_payload(entries, "schemas/triage.schema.json", label="Triage ledger")
    except SchemaValidationError as exc:
        raise WorkflowError(str(exc)) from exc

    with RunStateLock(run_dir):
        state = load_run_state(run_dir)
        require_active_run_state(state, run_ref.run_id, "triage")
        require_no_unsafe_drift(state, repo)
        ledger = state.setdefault("review_ledger", [])
        index = {
            e.get("fingerprint"): e for e in ledger if isinstance(e, dict)
        }
        merged = 0
        for entry in entries:
            # Fail closed on unauditable entries: an entry without a fingerprint
            # is not recorded in the review ledger, yet apply_triage_to_cumulative
            # would still close a cumulative finding by finding_id — closing a
            # severe finding (and unblocking the gate) with no audit trail. Reject
            # such entries so every gate-affecting closure is recorded.
            if not isinstance(entry, dict) or not str(
                entry.get("fingerprint", "")
            ).strip():
                raise WorkflowError(
                    "Every triage entry must carry a non-empty 'fingerprint' so a "
                    "gate-affecting closure is recorded in the audit ledger; "
                    f"refusing to apply an unauditable entry: {entry!r}"
                )
            index[entry["fingerprint"]] = entry
            merged += 1
        state["review_ledger"] = list(index.values())
        apply_triage_to_cumulative(state, entries)
        state["stop_gate_blocks"] = 0
        save_run_state(run_dir, state)
    print(f"Recorded {merged} triage finding(s) in the review ledger")
    return 0


# ---------------------------------------------------------------------------
# cmd_next_action
# ---------------------------------------------------------------------------


def _reference(name: str) -> str:
    return f"skills/autonomous-feature/references/{name}"


def compute_next_action(state: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Derive machine-readable phase guidance from current state and mode."""
    status = state.get("status")
    mode = state.get("effective_mode") or "standard"
    artifacts = state.get("artifacts", {})

    def have(key: str, fname: str) -> bool:
        rel = artifacts.get(key, fname)
        try:
            return resolve_artifact_path(str(rel), run_dir).exists()
        except StateError:
            return (run_dir / fname).exists()

    if status in {"complete", "blocked", "cancelled", "archived"}:
        return {
            "phase": status,
            "required_action": f"Run is {status}; no further action.",
            "completion_condition": "n/a",
            "references": [],
        }

    if not have("accepted_spec", "accepted-spec.md"):
        if mode == "rigorous" and "enhance" not in artifacts:
            return {
                "phase": "enhance",
                "required_action": "Run `codex --phase enhance`, then reconcile the "
                "output into an accepted spec.",
                "completion_condition": "accepted-spec.md exists (accept --kind spec).",
                "references": [_reference("specification.md")],
            }
        action = (
            "Inspect the repository and write a concise accepted spec."
            if mode == "lean"
            else "Reconcile requirements into an accepted spec."
        )
        return {
            "phase": "specification",
            "required_action": action,
            "completion_condition": "accepted-spec.md exists (accept --kind spec).",
            "references": [_reference("specification.md")],
        }

    if not have("accepted_plan", "accepted-plan.md"):
        action = (
            "Write a concise accepted implementation plan from repository inspection."
            if mode == "lean"
            else "Run `codex --phase plan`, then reconcile into an accepted plan."
        )
        return {
            "phase": "planning",
            "required_action": action,
            "completion_condition": "accepted-plan.md exists (accept --kind plan).",
            "references": [_reference("planning.md")],
        }

    checks = latest_verification_checks(
        state.get("verification", {}).get("checks", [])
    )
    verified = bool(checks) and all(c.get("exit_code") == 0 for c in checks)
    if not verified:
        return {
            "phase": "verification",
            "required_action": "Implement the plan and run repository checks via "
            "`run-check`.",
            "completion_condition": "All latest logical checks have exit_code 0.",
            "references": [
                _reference("implementation.md"),
                _reference("verification.md"),
            ],
        }

    reviews = state.get("reviews", [])
    latest_pass = bool(reviews) and reviews[-1].get("verdict") == "pass"
    if not latest_pass or cumulative_unresolved_severe(state):
        return {
            "phase": "review",
            "required_action": "Run `codex --phase review`, triage findings via "
            "`triage`, fix accepted ones, then re-review.",
            "completion_condition": "Latest review verdict is pass with no unresolved "
            "critical/high findings.",
            "references": [_reference("review.md")],
        }

    if state.get("risk", {}).get("requires_adversarial_review"):
        adversarial = state.get("adversarial_reviews", [])
        if not adversarial or adversarial[-1].get("verdict") != "pass":
            return {
                "phase": "adversarial",
                "required_action": "Run `codex --phase adversarial` and address any "
                "required actions.",
                "completion_condition": "Latest adversarial review verdict is pass.",
                "references": [_reference("review.md")],
            }

    return {
        "phase": "evaluate",
        "required_action": "Run `controller.py evaluate`.",
        "completion_condition": "All completion gates pass.",
        "references": [],
    }


def cmd_next_action(args: argparse.Namespace) -> int:
    repo, state_home, run_id_override = get_context(args)
    run_ref = resolve_run_for_inspection(
        state_home, repo.id, repo.canonical_root, run_id_override
    )
    info = compute_next_action(run_ref.state, run_ref.run_dir)
    print(json.dumps(info, indent=2))
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
    init.add_argument(
        "--mode",
        choices=WORKFLOW_MODES,
        default="auto",
        help="Workflow rigor mode; auto escalates conservatively by risk",
    )
    init.add_argument("--max-review-rounds", type=int, default=3, choices=range(1, 6))
    init.add_argument("--reuse", action="store_true")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    codex = sub.add_parser("codex", help="Run a structured, read-only Codex phase")
    codex.add_argument("--phase", required=True, choices=sorted(PHASE_OUTPUTS))
    codex.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Per-invocation timeout (seconds); overrides the global default",
    )
    codex.set_defaults(func=cmd_codex)

    accept = sub.add_parser(
        "accept", help="Record Claude-reconciled specification or plan"
    )
    accept.add_argument("--kind", required=True, choices=("spec", "plan"))
    accept.add_argument("--file", help="Accepted Markdown artifact (legacy mode)")
    accept.add_argument(
        "--source", help="Codex source JSON for structured decision-based acceptance"
    )
    accept.add_argument(
        "--decisions",
        help="Reconciliation delta JSON (accept/reject/modify/add) for structured mode",
    )
    accept.set_defaults(func=cmd_accept)

    run_check = sub.add_parser(
        "run-check", help="Execute and record one verification command"
    )
    run_check.add_argument("--name", required=True)
    run_check.add_argument(
        "--output",
        choices=("summary", "full"),
        default="summary",
        help="Terminal output policy; full replays complete stdout/stderr",
    )
    run_check.add_argument(
        "--failure-tail-lines",
        type=int,
        default=80,
        help="Number of trailing log lines to show on failure in summary mode",
    )
    run_check.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Per-command timeout (seconds); overrides the global default",
    )
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

    usage_report = sub.add_parser(
        "usage-report", help="Per-phase Codex usage regression table"
    )
    usage_report.add_argument("--json", action="store_true", help="Output JSON")
    usage_report.set_defaults(func=cmd_usage_report)

    next_action = sub.add_parser(
        "next-action", help="Machine-readable next-phase guidance"
    )
    next_action.add_argument(
        "--json", action="store_true", help="Output JSON (default format)"
    )
    next_action.set_defaults(func=cmd_next_action)

    triage = sub.add_parser(
        "triage", help="Merge triage finding-ledger entries into run state"
    )
    triage.add_argument(
        "--file", required=True, help="JSON array of {fingerprint, status, ...} entries"
    )
    triage.set_defaults(func=cmd_triage)

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
    except (WorkflowError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
