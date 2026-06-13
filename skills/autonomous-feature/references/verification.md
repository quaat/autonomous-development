# Verification phase

Discover relevant commands from repository evidence: `README.md`, `Makefile`, `pyproject.toml`,
`package.json`, CI workflows, and contributor instructions. Run the narrow checks first, then the
complete relevant suite.

Record every meaningful check through the controller. Output defaults to a one-line summary; full
logs are always written to `verification/NN-name.log`.

```bash
controller.py run-check --name unit-tests -- pytest -q
controller.py run-check --name typecheck -- npm run typecheck
# show full streams only when troubleshooting:
controller.py run-check --name unit-tests --output full -- pytest -q
# bound failure output:
controller.py run-check --name unit-tests --failure-tail-lines 80 -- pytest -q
```

For shell syntax, invoke the shell explicitly:

```bash
controller.py run-check --name combined -- bash -lc 'npm run lint && npm test'
```

Fix failures and rerun them. Never record a command as passing without actually executing it.

Completion condition: all latest logical checks have exit_code 0.
