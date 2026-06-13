# Specification phase

Goal: produce an accepted specification in the run directory.

- **rigorous mode:** run `controller.py codex --phase enhance`, read the Codex spec from the
  printed path (or `status --json` → `artifacts.enhance`), then reconcile it.
- **standard / lean mode:** skip Codex idea enhancement; reconcile directly from the user's idea
  and repository evidence.

Reconcile:
- accept grounded requirements; choose safe recommended defaults for non-blocking ambiguity;
- reject speculative scope expansion; preserve explicit non-goals;
- ensure each acceptance criterion is observable.

Register the result one of two ways:

```bash
# Legacy: write Markdown yourself, then register it
controller.py accept --kind spec --file <temporary-spec-file>

# Structured: emit only a reconciliation delta and let the controller materialize artifacts
controller.py accept --kind spec --source feature-spec.codex.json --decisions <delta.json>
```

The reconciliation delta is `{accept[], reject[{id,reason}], modify[{id,replacement}], add[]}`.
Items not mentioned are accepted verbatim, so the delta only records exceptions.

Completion condition: `accepted-spec.md` exists in the run directory.
