#!/usr/bin/env python3
"""Lightweight validation that requires only the Python standard library."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(message: str) -> None:
    print(f'ERROR: {message}', file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    manifest = json.loads((ROOT / '.claude-plugin/plugin.json').read_text(encoding='utf-8'))
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', manifest.get('name', '')):
        fail('Plugin name must be kebab-case')

    for schema in sorted((ROOT / 'schemas').glob('*.json')):
        parsed = json.loads(schema.read_text(encoding='utf-8'))
        if parsed.get('type') != 'object':
            fail(f'{schema} must define an object schema')

    skills = sorted((ROOT / 'skills').glob('*/SKILL.md'))
    if not skills:
        fail('No skills found')
    for skill in skills:
        text = skill.read_text(encoding='utf-8')
        if not text.startswith('---\n'):
            fail(f'{skill} has no YAML frontmatter')
        if '\ndescription:' not in text:
            fail(f'{skill} has no description')

    required = [
        ROOT / 'scripts/controller.py',
        ROOT / 'scripts/stop_gate.py',
        ROOT / 'prompts/enhance-idea.md',
        ROOT / 'prompts/implementation-plan.md',
        ROOT / 'prompts/code-review.md',
    ]
    for path in required:
        if not path.exists():
            fail(f'Missing required file: {path}')

    print(f'Validated {len(skills)} skills and {len(list((ROOT / "schemas").glob("*.json")))} schemas.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
