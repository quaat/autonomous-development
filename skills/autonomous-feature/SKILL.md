---
name: autonomous-feature
description: Autonomously develop a repository feature from a high-level idea. Codex independently enhances the idea, proposes a detailed plan, and reviews the implementation while Claude reconciles requirements, implements, verifies, triages findings, and fixes valid issues. Use when the user delegates an end-to-end feature change.
argument-hint: "[feature idea]"
disable-model-invocation: true
effort: max
allowed-tools:
  - Read
  - Grep
  - Glob
  - Edit
  - Write
  - LSP
  - Agent
  - EnterWorktree
  - ExitWorktree
  - Bash(git *)
  - Bash(python3 *)
  - Bash(codex *)
disallowed-tools:
  - AskUserQuestion
hooks:
  Stop:
    - hooks:
        - type: command
          command: 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/stop_gate.py"'
          timeout: 10
---

# Autonomous feature development

Implement this feature idea:

> $ARGUMENTS

Use ultrathink for architecture, compatibility, and review triage.

## Non-negotiable boundaries

- Preserve unrelated user changes.
- Never push, merge, publish, deploy, rotate credentials, or modify remote infrastructure.
- Never use `danger-full-access`, `--yolo`, `bypassPermissions`, or equivalent unrestricted modes.
- Never apply an irreversible database migration or delete user data.
- Do not weaken authorization, validation, tests, or static checks to make the workflow pass.
- Codex planning and review executions must remain read-only.
- Use no more than the configured review-round budget.
- Treat every Codex finding as a proposal requiring evidence-based triage.

## Procedure

### 1. Isolate and inspect

1. Confirm this is a Git repository.
2. Inspect `CLAUDE.md`, repository instructions, architecture, status, tests, and relevant files.
3. Use `EnterWorktree` to create an isolated worktree whenever available. This is mandatory when the starting worktree contains uncommitted changes. Do not copy or delete unrelated changes.
4. Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" doctor
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" init --feature "$ARGUMENTS"
```

`init` prints the path to `run-state.json` and the run ID. When multiple concurrent runs are
active, pass `--run-id <run-id>` to all subsequent commands to target the correct run.

When `doctor` reports a missing external prerequisite, mark the run blocked with the controller
and report the exact missing prerequisite rather than bypassing it.

### 2. Enhance the idea with Codex

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase enhance
```

Read the Codex output path printed by the controller (or use
`controller.py status --json` to find `artifacts.enhance`). Reconcile it against repository
evidence and the user's actual idea:

- accept grounded requirements;
- choose safe recommended defaults for non-blocking ambiguity;
- reject speculative scope expansion;
- preserve explicit non-goals;
- ensure each acceptance criterion is observable.

Write the reconciled specification to a temporary Markdown file, then register it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" accept --kind spec --file <temporary-spec-file>
```

### 3. Obtain and reconcile the implementation plan

Run a fresh Codex execution:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase plan
```

Read the Codex output path printed by the controller (or use
`controller.py status --json` to find `artifacts.plan`). Verify all file paths, assumptions,
sequencing, migrations, public interfaces, and test commands against the repository. Produce a
concise accepted plan with explicit acceptance-criterion coverage, then register it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" accept --kind plan --file <temporary-plan-file>
```

If the change touches authentication, authorization, personal or regulated data, persistence schemas, destructive operations, concurrency, retries, external APIs, billing, or production-critical reliability, set the high-risk gate:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" set-risk --require-adversarial --reason "<specific risk>"
```

### 4. Implement

Implement the accepted plan incrementally:

- follow repository conventions;
- add or update tests alongside behavior;
- keep public interfaces backward compatible unless the accepted specification says otherwise;
- include migration and rollback support when applicable;
- update user-facing and operator documentation affected by the change;
- do not create commits unless the user explicitly requested them.

Record phase progress when useful:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" set-phase --phase implementing
```

### 5. Verify

Discover relevant commands from repository evidence such as `README.md`, `Makefile`, `pyproject.toml`, `package.json`, CI workflows, and existing contributor instructions. Run the narrow checks first, then the complete relevant suite.

Record every meaningful check through the controller, for example:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" run-check --name unit-tests -- pytest -q
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" run-check --name typecheck -- npm run typecheck
```

For shell syntax, explicitly invoke the shell:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" run-check --name combined -- bash -lc 'npm run lint && npm test'
```

Fix failures and rerun them. Never record a command as passing without actually executing it.

### 6. Independent Codex review

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase review
```

Read the generated `review-NN.codex.json`. For each finding, write `triage-NN.md` classifying it as:

- `accepted`;
- `rejected_with_evidence`;
- `already_resolved`;
- `out_of_scope_but_recorded`;
- `requires_human_decision`.

Give repository evidence for every rejection. Fix accepted findings, add regression tests, rerun all affected checks, and request a fresh review. Stop and mark the run blocked if the same critical/high issue recurs after a genuine fix attempt or if the review budget is exhausted.

### 7. Adversarial review for high-risk changes

When the high-risk gate is set, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase adversarial
```

Address valid required actions, verify again, and rerun the adversarial review when needed.

### 8. Completion gate

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" evaluate
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" status
```

Do not declare success unless `evaluate` succeeds. The final report must include:

- the implemented behavior;
- principal files changed;
- verification commands and results;
- Codex review rounds and disposition of findings;
- adversarial review result when required;
- remaining risks or explicit blocked reason;
- a suggested conventional commit message.
