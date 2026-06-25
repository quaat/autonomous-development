# Example invocations

```text
/autonomous-development:autonomous-feature "Add resumable multipart uploads with checksum validation and backward-compatible API behavior"
```

```text
/autonomous-development:enhance-idea "Add organization-level API usage limits"
/autonomous-development:implementation-plan "Prioritize backward compatibility and migration safety"
/autonomous-development:implement-plan
/autonomous-development:verify-feature
/autonomous-development:codex-review
/autonomous-development:fix-findings
/autonomous-development:codex-review
/autonomous-development:autonomous-status
```

For a high-risk feature:

```text
/autonomous-development:adversarial-review "The change modifies authorization and persistent access-token storage"
```

## Controller modes and reporting

`auto` mode (the default) scales the workflow to the change and escalates conservatively:

```bash
# Low-risk, localized work runs lean/standard; high-risk work escalates to rigorous.
controller.py init --feature "Rename a button label" --mode auto

# Force the full workflow with mandatory adversarial review.
controller.py init --feature "Add tenant-scoped billing" --mode rigorous

# Use your manually created feature branch instead of a disposable worktree.
git switch -c experiment
controller.py init --feature "Experiment feature" --mode auto --worktree-mode current
```

Drive phases and inspect token usage:

```bash
controller.py next-action --json
controller.py run-check --name unit-tests --output summary -- pytest -q
controller.py usage-report
```
