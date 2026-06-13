# Planning phase

Goal: produce an accepted implementation plan in the run directory.

- **rigorous / standard mode:** run `controller.py codex --phase plan`, read the plan from the
  printed path (or `status --json` → `artifacts.plan`), then reconcile it.
- **lean mode:** write a concise plan from repository inspection without a separate Codex planning
  pass.

Verify every file path, assumption, sequencing decision, migration, public interface, and test
command against the actual repository before accepting. Produce a plan with explicit
acceptance-criterion coverage.

Register one of two ways:

```bash
controller.py accept --kind plan --file <temporary-plan-file>
controller.py accept --kind plan --source implementation-plan.codex.json --decisions <delta.json>
```

If the change touches authentication, authorization, personal or regulated data, persistence
schemas, destructive operations, concurrency, retries, external APIs, billing, or
production-critical reliability, set the high-risk gate (auto mode sets it automatically when it
escalates to rigorous):

```bash
controller.py set-risk --require-adversarial --reason "<specific risk>"
```

Completion condition: `accepted-plan.md` exists in the run directory.
