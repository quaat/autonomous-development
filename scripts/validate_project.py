#!/usr/bin/env python3
"""Lightweight validation that requires only the Python standard library."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def _strict_required_violations(node: object, path: str) -> list[tuple[str, list[str]]]:
    """Return (location, missing_keys) for objects whose `required` omits a property."""
    problems: list[tuple[str, list[str]]] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and isinstance(node.get("properties"), dict):
            missing = sorted(set(node["properties"]) - set(node.get("required", [])))
            if missing:
                problems.append((path, missing))
        for key, value in node.items():
            problems.extend(_strict_required_violations(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            problems.extend(_strict_required_violations(value, f"{path}[{index}]"))
    return problems


def main() -> int:
    manifest = json.loads(
        (ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
    )
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", manifest.get("name", "")):
        fail("Plugin name must be kebab-case")

    for schema in sorted((ROOT / "schemas").glob("*.json")):
        parsed = json.loads(schema.read_text(encoding="utf-8"))
        if parsed.get("type") != "object":
            fail(f"{schema} must define an object schema")

    # Codex `--output-schema` runs under OpenAI strict structured outputs, which
    # require every object's `required` array to list every key in `properties`.
    # Validate this recursively so a missing entry is caught here rather than at
    # Codex runtime.
    output_schema_names = (
        "enhanced-idea",
        "implementation-plan",
        "review",
        "review-delta",
        "adversarial-review",
    )
    for name in output_schema_names:
        path = ROOT / "schemas" / f"{name}.schema.json"
        if not path.exists():
            fail(f"Missing required output schema: {path}")
        parsed = json.loads(path.read_text(encoding="utf-8"))
        for location, missing in _strict_required_violations(parsed, "(root)"):
            fail(
                f"{path}: object at {location} omits from `required`: "
                f"{', '.join(missing)}"
            )

    skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
    if not skills:
        fail("No skills found")
    for skill in skills:
        text = skill.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            fail(f"{skill} has no YAML frontmatter")
        if "\ndescription:" not in text:
            fail(f"{skill} has no description")

    required = [
        ROOT / "scripts/controller.py",
        ROOT / "scripts/stop_gate.py",
        ROOT / "prompts/enhance-idea.md",
        ROOT / "prompts/implementation-plan.md",
        ROOT / "prompts/code-review.md",
        ROOT / "prompts/code-review-delta.md",
        ROOT / "schemas/review-delta.schema.json",
        ROOT / "schemas/accept-decisions.schema.json",
        ROOT / "skills/autonomous-feature/references/specification.md",
        ROOT / "skills/autonomous-feature/references/planning.md",
        ROOT / "skills/autonomous-feature/references/implementation.md",
        ROOT / "skills/autonomous-feature/references/verification.md",
        ROOT / "skills/autonomous-feature/references/review.md",
    ]
    for path in required:
        if not path.exists():
            fail(f"Missing required file: {path}")

    print(
        f'Validated {len(skills)} skills and {len(list((ROOT / "schemas").glob("*.json")))} schemas.'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
