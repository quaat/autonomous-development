# Changelog

## Unreleased

### Added
- Token instrumentation: `codex` phases run with `codex exec --json`, retain the NDJSON
  event stream (`*.events.ndjson`), and record a per-phase usage block
  (`prompt_characters`, `output_characters`, `duration_seconds`, `model`,
  `reasoning_effort`, `verbosity`) in `codex_runs`
- `usage-report` command: per-phase prompt/output/duration table (`--json` for raw records)
- `next-action` command: machine-readable phase guidance (`phase`, `required_action`,
  `completion_condition`, `references`) so the main skill drives a state machine
- `triage` command: merge a JSON finding ledger (`fingerprint`/`status`/`reason`) so later
  review rounds do not re-raise rejected findings
- `init --mode {auto,lean,standard,rigorous}`: adaptive workflow depth. `auto` escalates
  conservatively to `rigorous` on risk classification and never downgrades an explicit mode
- `run-check --output {summary,full}` and `--failure-tail-lines N`: summary mode prints a
  one-line result and log path (success) or a bounded failure tail instead of replaying
  full streams into context; the complete log is always retained on disk
- `accept --source <codex-json> --decisions <delta-json>`: deterministically materialize
  `accepted-spec.json`/`.md` (and plan equivalents) from a reconciliation delta
  (`accept`/`reject`/`modify`/`add`) instead of rewriting full artifacts
- Phase-specific Codex reasoning profiles (`PHASE_PROFILES`) applied via `-c` overrides,
  configurable per installation through `CLAUDE_AUTONOMOUS_PHASE_PROFILES` and
  `CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>`
- Full-then-delta reviews: round 1 uses the full review schema, rounds 2+ use a compact
  `review-delta` schema merged into a cumulative finding ledger
- `prompts/code-review-delta.md` and `schemas/review-delta.schema.json`
- `schemas/accept-decisions.schema.json`
- `skills/autonomous-feature/references/`: per-phase guidance loaded only when needed

### Changed
- Codex context is compacted: repository manifest (instructions/build manifests/primary
  modules/test roots/CI) instead of the first 250 tracked file names; latest-only
  verification checks; finding ledger instead of full prior review/triage prose
- `skills/autonomous-feature/SKILL.md` slimmed to a state-machine driver (`effort: high`)

## 0.2.0 - 2026-06-12

### Added
- `scripts/state.py`: shared module for git-root discovery, state-home resolution,
  repository identity, run selection, schema migration, atomic writes, and file locking
- `--state-dir` global option: override state home directory
- `--run-id` global option: select a specific run for run-scoped commands
- `list-runs` command: show active runs for the current repository
- `show-run` command: show full details for one run
- `migrate-legacy-state` command: non-destructive import of legacy `.ai/` state
- `archive-run` command: mark a run archived
- `accept-drift` command: acknowledge and record a new git baseline
- XDG-based external state storage: `~/.local/state/claude-autonomous/`
- Multiple concurrent runs per repository with collision-resistant run IDs
- Drift detection: blocks unsafe changes (branch/identity/worktree changes)
- File locking (`fcntl.flock`) for concurrent controller/stop-gate safety
- Schema version 2 with validation and backward-compatible v1 loading

### Changed
- Controller and stop gate now discover repository root via `git rev-parse --show-toplevel`
- Stop gate uses shared state resolver (no more hardcoded CWD-relative path)
- Run state stores artifact paths relative to run directory
- Legacy `.ai/autonomous-development/` layout auto-detected as fallback

### Backward compatible
- All existing commands (`init`, `codex`, `accept`, `run-check`, `status`, etc.) work unchanged
- Single-run workflows require no new flags
- Legacy state automatically detected and usable without migration

## 0.1.0 - 2026-06-12

- Initial plugin boilerplate.
- Added end-to-end autonomous feature workflow.
- Added modular idea, plan, implementation, verification, review, fix, adversarial-review, and status skills.
- Added schema-validated Codex execution controller and bounded stop gate.
- Added tests and project validation utilities.
