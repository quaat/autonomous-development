# Security policy

This plugin coordinates two local coding agents. Treat every repository as untrusted until reviewed.

- Review the plugin and target repository before accepting Claude Code workspace trust.
- Never store API keys or Codex authentication files in a repository.
- Keep Codex planning and review executions in the read-only sandbox.
- Do not run autonomous implementation against production-mounted filesystems.
- Do not enable `danger-full-access`, `bypassPermissions`, or equivalent unrestricted modes.
- Review generated migrations, authorization changes, and destructive commands manually before applying them outside a disposable development environment.

Report security problems privately to the repository owner rather than opening a public issue containing exploit details or credentials.
