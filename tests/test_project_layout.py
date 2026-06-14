from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProjectLayoutTests(unittest.TestCase):
    def test_manifest_and_components(self) -> None:
        manifest = json.loads(
            (ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["name"], "autonomous-development")
        expected = {
            "autonomous-feature",
            "enhance-idea",
            "implementation-plan",
            "implement-plan",
            "verify-feature",
            "codex-review",
            "adversarial-review",
            "fix-findings",
            "autonomous-status",
        }
        actual = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
        self.assertEqual(actual, expected)

    def test_json_files_parse(self) -> None:
        for path in ROOT.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))

    def test_output_schemas_are_strict_required(self) -> None:
        """Codex --output-schema objects must list every property in `required`
        (OpenAI strict structured outputs reject any omission)."""

        def violations(node: object, path: str) -> list[tuple[str, list[str]]]:
            found: list[tuple[str, list[str]]] = []
            if isinstance(node, dict):
                props = node.get("properties")
                if node.get("type") == "object" and isinstance(props, dict):
                    missing = sorted(set(props) - set(node.get("required", [])))
                    if missing:
                        found.append((path, missing))
                for key, value in node.items():
                    found += violations(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    found += violations(value, f"{path}[{index}]")
            return found

        for name in (
            "enhanced-idea",
            "implementation-plan",
            "review",
            "review-delta",
            "adversarial-review",
        ):
            path = ROOT / "schemas" / f"{name}.schema.json"
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(violations(schema, "(root)"), [], f"{name}: {path}")

    def test_prompt_placeholders_are_known(self) -> None:
        known = {
            "FEATURE",
            "BASELINE",
            "REPOSITORY_CONTEXT",
            "CODEX_SPEC",
            "ACCEPTED_SPEC",
            "ACCEPTED_PLAN",
            "VERIFICATION",
            "PREVIOUS_REVIEW",
            "LATEST_REVIEW",
            "FINDING_LEDGER",
            "OPEN_FINDINGS",
            "ACCEPTANCE_CRITERIA",
        }
        import re

        for path in (ROOT / "prompts").glob("*.md"):
            placeholders = set(
                re.findall(r"\{\{([A-Z0-9_]+)\}\}", path.read_text(encoding="utf-8"))
            )
            self.assertTrue(placeholders <= known, f"{path}: {placeholders - known}")


if __name__ == "__main__":
    unittest.main()
