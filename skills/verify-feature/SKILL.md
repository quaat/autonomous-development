---
name: verify-feature
description: Discover repository-specific verification commands, run them, record evidence, and diagnose failures for an autonomous-development run.
disable-model-invocation: true
allowed-tools: Read Grep Glob LSP Bash(*) Bash(python3 *)
disallowed-tools: AskUserQuestion
---

# Verify the feature

Read [references/check-discovery.md](references/check-discovery.md).

1. Discover authoritative commands from repository instructions and CI configuration.
2. Run focused tests for changed behavior first.
3. Run applicable formatting checks, linters, type checks, unit tests, integration tests, builds, and runtime verification.
4. Record each command through:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/controller.py" run-check --name <name> -- <executable> <arguments...>
```

5. Diagnose and fix failures only when the failure is caused by the implementation. Do not hide, skip, delete, or weaken tests.
6. Report commands, exit status, and log paths.
