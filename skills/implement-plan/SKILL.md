---
name: implement-plan
description: Implement the currently accepted autonomous-development plan with tests and documentation while preserving unrelated changes and safety boundaries.
disable-model-invocation: true
effort: max
allowed-tools: Read Grep Glob Edit Write LSP Agent Bash(git *) Bash(python3 *)
disallowed-tools: AskUserQuestion
---

# Implement the accepted plan

Read:

- `.ai/autonomous-development/accepted-spec.md`;
- `.ai/autonomous-development/accepted-plan.md`;
- repository instructions and relevant source/tests.

Then:

1. Confirm the workflow is active with `controller.py status`.
2. Implement only accepted scope, incrementally.
3. Preserve unrelated local modifications.
4. Add tests for every changed behavior and regression.
5. Keep interfaces backward compatible unless the accepted specification explicitly requires a break.
6. Include safe migration and rollback paths where applicable.
7. Update affected documentation and examples.
8. Set the phase to `implemented` when implementation is complete.

Never push, merge, deploy, access production, rotate credentials, or apply irreversible migrations.
