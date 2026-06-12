# Changelog

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
