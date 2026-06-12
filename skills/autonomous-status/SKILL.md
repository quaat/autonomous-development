---
name: autonomous-status
description: Show the current autonomous-development run, generated artifacts, review budget, verification results, and remaining completion gates.
disable-model-invocation: true
allowed-tools: Read Bash(python3 *)
disallowed-tools: AskUserQuestion Edit Write
---

# Autonomous workflow status

Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" status
```

When more detail is needed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" status --json
```

When multiple runs exist for the repository, first list them:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" list-runs
```

Then inspect a specific run by ID:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" show-run --run-id <run-id>
```

Pass `--run-id <run-id>` to `status` as well when you need to target one run among several
active ones.

Explain the current phase, passing and failing checks, latest review verdict, remaining review
budget, high-risk review requirement, and the next concrete action. Do not modify product files.
