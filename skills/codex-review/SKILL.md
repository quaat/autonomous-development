---
name: codex-review
description: Run a fresh independent read-only Codex review of all implementation changes against the baseline, accepted specification, plan, and recorded verification.
disable-model-invocation: true
effort: max
allowed-tools: Read Grep Glob Bash(git *) Bash(python3 *) Bash(codex *)
disallowed-tools: AskUserQuestion Edit Write
---

# Independent Codex review

1. Confirm accepted specification, accepted plan, and recorded verification exist.
2. Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" codex --phase review
```

3. Read the generated `.ai/autonomous-development/review-NN.codex.json`.
4. Summarize the verdict and findings by severity with exact file evidence.
5. Do not edit product files. Finding triage and repairs belong to `/autonomous-development:fix-findings`.
