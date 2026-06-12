# Verification-command discovery

Use repository evidence in this order:

1. `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, and project `README.md`.
2. CI workflows and required status-check configuration.
3. `Makefile`, `justfile`, `Taskfile.yml`, or repository scripts.
4. Language manifests:
   - Python: `pyproject.toml`, `tox.ini`, `noxfile.py`, `pytest.ini`;
   - Node.js: `package.json`, workspace configuration;
   - Rust: `Cargo.toml`;
   - Go: `go.mod`;
   - Java/Kotlin: Maven or Gradle files;
   - .NET: solution and project files.
5. Existing test-directory conventions.

Prefer commands already used in CI. Do not install new dependencies without repository evidence or a clear implementation need. For services requiring external infrastructure, use existing local fixtures or containers and record any verification gap honestly.
