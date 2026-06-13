# Autonomous Claude + Codex Development

A reusable Claude Code plugin for bounded autonomous feature development:

```text
feature idea
  -> Codex idea enhancement
  -> Codex implementation plan
  -> Claude reconciliation and implementation
  -> project verification
  -> independent Codex review
  -> Claude finding triage and fixes
  -> re-verification and re-review
```

The main entry point is:

```text
/autonomous-development:autonomous-feature "Add resumable file uploads"
```

The plugin deliberately keeps Claude as the implementation orchestrator and uses fresh, read-only Codex executions for independent planning and review. It does not push, merge, deploy, rotate credentials, alter remote infrastructure, or apply irreversible production migrations.

## Included skills

| Skill | Purpose |
|---|---|
| `/autonomous-development:autonomous-feature` | Run the complete workflow from a high-level idea |
| `/autonomous-development:enhance-idea` | Ask Codex to turn a rough feature idea into a structured proposal |
| `/autonomous-development:implementation-plan` | Ask a fresh Codex execution for a repository-grounded plan |
| `/autonomous-development:implement-plan` | Implement the accepted plan with Claude |
| `/autonomous-development:verify-feature` | Discover and run relevant repository checks |
| `/autonomous-development:codex-review` | Run an independent structured Codex review |
| `/autonomous-development:adversarial-review` | Challenge high-risk architecture and operational assumptions |
| `/autonomous-development:fix-findings` | Triage and fix validated review findings |
| `/autonomous-development:autonomous-status` | Show workflow state and remaining gates |

## Requirements

- Claude Code with plugin and skill support.
- Python 3.11 or later.
- Git.
- Codex CLI installed and authenticated.
- A Git repository for the target project.

Install Codex CLI when needed:

```bash
npm install -g @openai/codex
codex login
```

The official OpenAI Codex plugin for Claude Code is optional for this project because the workflow invokes `codex exec` directly to obtain schema-validated output. It remains useful for manual `/codex:*` commands:

```text
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
/codex:setup
```

## Run locally

From the parent directory of this plugin:

```bash
claude --plugin-dir ./claude-codex-autonomous-development
```

Then open a target repository and invoke:

```text
/autonomous-development:autonomous-feature "Add audit-log export as JSON and CSV"
```

For development validation:

```bash
make check
claude plugin validate . --strict
```

## Adaptive workflow modes

`init` accepts `--mode` to scale workflow depth to the change:

```bash
# auto (default): inspect the feature and escalate conservatively
controller.py init --feature "Add Stripe billing" --mode auto

# lean: clear, low-risk, localized work
# standard: normal feature work (skips independent idea enhancement)
# rigorous: full workflow with mandatory adversarial review
controller.py init --feature "Rename a button label" --mode standard
```

`auto` escalates to `rigorous` when it classifies the feature as touching auth/authz,
persistence/migrations, regulated data, billing, concurrency, public API compatibility, broad
architecture, or destructive behavior. It never downgrades an explicitly requested mode, and an
explicit `rigorous` (or escalated `auto`) run requires an adversarial review to complete.

The main skill is a state-machine driver: it repeatedly asks the controller for the next phase
and executes it.

```bash
controller.py next-action --json
# -> { "phase": "verification", "required_action": "...",
#      "completion_condition": "...", "references": [...] }
```

## Token efficiency

The controller minimizes the context that flows back into the model:

```bash
# Summary output (default): one line plus the on-disk log path
controller.py run-check --name unit-tests --output summary -- pytest -q
# ✓ unit-tests passed in 18.4 s
#   command: pytest -q
#   full log: .../verification/03-unit-tests.log

# Failures show a bounded tail; --output full replays complete streams
controller.py run-check --name unit-tests --failure-tail-lines 80 -- pytest -q
```

Codex phases run with `codex exec --json`, retain the NDJSON event stream, and record per-phase
usage. Inspect it with:

```bash
controller.py usage-report
# Phase             Prompt chars   Output chars    Duration
# enhance                 14,220          5,810        67 s
# plan                    23,840          9,120       104 s
# review-01               31,440          6,330       119 s
```

Reconciliation uses deltas rather than rewriting whole artifacts. Claude writes a decision file
(`accept`/`reject`/`modify`/`add`) and the controller materializes the accepted artifact:

```bash
controller.py accept --kind spec \
  --source feature-spec.codex.json \
  --decisions spec-reconciliation.json
```

Review triage is recorded as a finding ledger so later rounds never re-raise rejected findings:

```bash
controller.py triage --file triage-01.json
```

## State location

State is stored outside the target repository by default. The resolver uses the following
precedence:

```bash
# 1. Explicit state directory (highest priority)
controller.py --state-dir /path/to/state init --feature "..."

# 2. Environment variable
export CLAUDE_AUTONOMOUS_STATE_HOME=~/.local/state/claude-autonomous
controller.py init --feature "..."

# 3. XDG default (Linux)
# Automatically uses ~/.local/state/claude-autonomous/

# 4. Legacy fallback (existing .ai/autonomous-development/ detected automatically)
```

On macOS the default is `~/Library/Application Support/claude-autonomous/`.
On Windows the default is `%LOCALAPPDATA%\claude-autonomous\`.

## State and generated artifacts

Each run is stored in its own directory under the state home:

```text
~/.local/state/claude-autonomous/
├── repositories/
│   └── <repo-id>/
│       ├── metadata.json
│       └── runs/
│           └── <run-id>/
│               ├── run-state.json
│               ├── feature-request.md
│               ├── repository-context.txt
│               ├── accepted-spec.md
│               ├── accepted-plan.md
│               ├── feature-spec.codex.json
│               ├── implementation-plan.codex.json
│               ├── review-01.codex.json
│               └── verification/
```

The legacy `.ai/autonomous-development/` layout is still supported for backward compatibility
and is auto-detected when present. To suppress it, add `.ai/` to your `.gitignore` once you
have migrated (see "Migration from legacy state" below) or if you never want to commit the
planning evidence.

## Multiple runs and run IDs

Each `init` creates a new run with a collision-resistant run ID of the form
`<YYYYMMDDTHHMMSSZ>-<8-hex-chars>` (for example `20260612T134500Z-a1b2c3d4`).

```bash
# List all active runs for the current repository
controller.py list-runs

# Show details for a specific run
controller.py show-run --run-id 20260612T134500Z-a1b2c3d4

# Start a second concurrent run with an optional human-readable label
controller.py init --feature "New feature" --label "experiment"

# Run commands against a specific run when multiple are active
controller.py status --run-id 20260612T134500Z-a1b2c3d4
```

When exactly one active run exists, `--run-id` is optional and the run is selected
automatically. When multiple active runs exist, commands that mutate state require
`--run-id` to avoid ambiguity.

## Migration from legacy state

```bash
# Migrate existing .ai/autonomous-development/ state to the new external layout
controller.py migrate-legacy-state

# The original .ai/autonomous-development/ directory is preserved unchanged.
# To use the migrated state, either:
export CLAUDE_AUTONOMOUS_STATE_HOME=~/.local/state/claude-autonomous
# or add .ai/ to your .gitignore and continue; legacy state remains accessible.
```

Migration is non-destructive and idempotent. Run it again with `--force` to overwrite an
already-migrated run directory.

## Drift detection and recovery

The controller detects when the repository state diverges from the recorded baseline before
any mutating command. Two kinds of drift are distinguished:

- **EXPECTED**: HEAD has advanced on the same branch (commits were added). No action required.
- **UNSAFE**: Branch changed, worktree path changed, or repository identity changed. Mutating
  commands are blocked until the drift is acknowledged.

```bash
# If an unsafe drift is detected (e.g., branch changed), you will see:
# error: Unsafe repository drift detected: branch changed: main -> experiment
# Recovery: Run `accept-drift` to acknowledge and record the new baseline.

controller.py accept-drift
```

## Archiving runs

```bash
# Archive a completed run (removes it from the default list-runs output)
controller.py archive-run --run-id 20260612T134500Z-a1b2c3d4

# Show all runs including archived ones
controller.py list-runs --all
```

Archiving is a metadata flag; no files are deleted.

## Security and permissions

- State directories are created with mode `0o700` (owner-only) on POSIX systems.
- Review artifacts and Codex responses may contain sensitive code, prompts, or design details.
- Do not share state directories across users or store them on world-readable paths.
- Remote URLs are stored with credentials stripped (the `user:pass@` portion is removed).

## Worktree support

All commands work correctly from any linked worktree. The repository identity is derived from
the shared git object store so runs created in different worktrees belong to the same
repository and are visible to `list-runs`.

```bash
# Create a linked worktree and run the workflow there
git worktree add ../experiment feature-branch
cd ../experiment
controller.py init --feature "Experiment feature"
# State stored under the same repository ID, new run ID
```

## Completion rules

A run succeeds only when:

- an accepted specification and accepted plan exist;
- all recorded verification commands pass;
- the latest Codex review returns `pass`;
- no unresolved `critical` or `high` findings remain;
- an adversarial review passes when the change is classified as high risk.

A run stops as `blocked` when credentials or required services are unavailable, requirements materially conflict, verification cannot be performed, or the maximum review/fix rounds are exhausted.

## Security boundaries

The skill uses Codex with `--sandbox read-only`. Claude performs repository edits under Claude Code's normal permission system. The workflow explicitly prohibits:

- `danger-full-access`, `--yolo`, or bypassing sandbox controls;
- pushing, merging, publishing, or deploying;
- modifying production data or remote infrastructure;
- rotating or exposing credentials;
- deleting unrelated user changes;
- weakening tests or security controls to obtain a passing result.

Run autonomous development in a disposable branch or worktree. The orchestrator asks Claude Code to use an isolated worktree whenever supported.

## Customization

- Edit `prompts/*.md` to tailor Codex's planning and review behavior.
- Edit `schemas/*.schema.json` to add organization-specific output requirements.
- Extend `skills/verify-feature/references/check-discovery.md` with project-specific commands.
- Change `max_review_rounds` through the controller's `init --max-review-rounds` option.
- Map workflow phases to locally available Codex models and reasoning settings with
  `CLAUDE_AUTONOMOUS_PHASE_PROFILES` (JSON) and `CLAUDE_AUTONOMOUS_CODEX_MODEL_<PHASE>`.

## Compatibility note

Claude Code and Codex evolve quickly. The project uses documented plugin-root component layout, `SKILL.md` frontmatter, skill-scoped hooks, `${CLAUDE_PLUGIN_ROOT}`, and `codex exec --output-schema`. Run `make check` and `claude plugin validate . --strict` after upgrading either CLI.
