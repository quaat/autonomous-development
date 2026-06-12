---
name: implementation-plan
description: Ask a fresh read-only Codex execution for a detailed repository-grounded implementation plan after Claude has accepted the feature specification.
argument-hint: "[optional planning emphasis]"
disable-model-invocation: true
allowed-tools: Read Grep Glob Write Bash(git *) Bash(python3 *) Bash(codex *)
disallowed-tools: AskUserQuestion
---

# Create an implementation plan

1. Confirm an accepted spec exists. Use `controller.py status --json` and check
   `artifacts.accepted_spec`, or run `controller.py show-run` to view the full run state.
   When only the Codex proposal exists, reconcile it first and register it with
   `controller.py accept --kind spec`.
2. Inspect repository conventions and likely change locations independently.
3. Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase plan
```

4. Validate the plan output (path printed by the controller, or found via
   `controller.py status --json` under `artifacts.plan`) against actual files and interfaces.
5. Incorporate the optional emphasis: `$ARGUMENTS`.
6. Write a concise accepted Markdown plan and register it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" accept --kind plan --file <accepted-plan-file>
```

Do not modify product code in this skill.
