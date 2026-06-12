#!/usr/bin/env python3
"""Skill-scoped Stop hook that keeps a bounded autonomous run moving."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

STATE_REL = Path('.ai/autonomous-development/run-state.json')
TERMINAL = {'complete', 'blocked', 'cancelled'}
MAX_GATE_BLOCKS = 3


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)


def reason_for(state: dict[str, Any], root: Path) -> str:
    directory = root / STATE_REL.parent
    if not (directory / 'accepted-spec.md').exists():
        return 'Continue the autonomous workflow: reconcile the Codex proposal and create accepted-spec.md.'
    if not (directory / 'accepted-plan.md').exists():
        return 'Continue the autonomous workflow: reconcile the Codex plan and create accepted-plan.md.'
    all_checks = state.get('verification', {}).get('checks', [])
    latest = {}
    for check in all_checks:
        latest[str(check.get('name', 'unnamed'))] = check
    checks = list(latest.values())
    if not checks:
        return 'Continue the autonomous workflow: run and record relevant verification checks.'
    if any(check.get('exit_code') != 0 for check in checks):
        return 'Continue the autonomous workflow: fix the failing verification checks and rerun them.'
    reviews = state.get('reviews', [])
    if not reviews:
        return 'Continue the autonomous workflow: run the independent Codex code review.'
    if reviews[-1].get('verdict') != 'pass':
        return 'Continue the autonomous workflow: triage the latest Codex findings, fix valid issues, verify, and re-review.'
    if state.get('risk', {}).get('requires_adversarial_review'):
        adversarial = state.get('adversarial_reviews', [])
        if not adversarial or adversarial[-1].get('verdict') != 'pass':
            return 'Continue the autonomous workflow: complete the required adversarial review and address valid risks.'
    return 'Run the controller completion-gate evaluation and provide the final implementation report.'


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    root = Path(payload.get('cwd') or os.getcwd()).resolve()
    path = root / STATE_REL
    if not path.exists():
        return 0
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return 0

    if state.get('status') in TERMINAL:
        return 0

    blocks = int(state.get('stop_gate_blocks', 0))
    if blocks >= MAX_GATE_BLOCKS:
        state['status'] = 'blocked'
        state['phase'] = 'stop-gate-budget-exhausted'
        state.setdefault('notes', []).append(
            'The bounded Stop hook retry budget was exhausted; inspect workflow status manually.'
        )
        atomic_write(path, state)
        return 0

    state['stop_gate_blocks'] = blocks + 1
    atomic_write(path, state)
    print(json.dumps({'decision': 'block', 'reason': reason_for(state, root)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
