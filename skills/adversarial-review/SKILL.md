---
name: adversarial-review
description: Run a fresh read-only Codex challenge review for authentication, authorization, persistence, migration, concurrency, retry, data-loss, privacy, or external-service risks.
disable-model-invocation: true
effort: max
allowed-tools: Read Grep Glob Bash(git *) Bash(python3 *) Bash(codex *)
disallowed-tools: AskUserQuestion Edit Write
---

# Adversarial review

Use for high-risk changes after ordinary verification and code review.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" set-risk --require-adversarial --reason "$ARGUMENTS"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase adversarial
```

Read the generated `adversarial-NN.codex.json`. Distinguish concrete failure scenarios from speculation and identify the smallest evidence-backed mitigations. Do not edit product files in this skill.
