#!/usr/bin/env python3
"""Stateful controller for the Claude + Codex autonomous-development plugin."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR_NAME = Path('.ai/autonomous-development')
STATE_FILE_NAME = 'run-state.json'
TERMINAL_STATUSES = {'complete', 'blocked', 'cancelled'}
PHASE_OUTPUTS = {
    'enhance': ('prompts/enhance-idea.md', 'schemas/enhanced-idea.schema.json', 'feature-spec.codex.json'),
    'plan': ('prompts/implementation-plan.md', 'schemas/implementation-plan.schema.json', 'implementation-plan.codex.json'),
    'review': ('prompts/code-review.md', 'schemas/review.schema.json', None),
    'adversarial': ('prompts/adversarial-review.md', 'schemas/adversarial-review.schema.json', None),
}


class WorkflowError(RuntimeError):
    """A user-actionable workflow error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def run_process(
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )
    except FileNotFoundError as exc:
        raise WorkflowError(f'Required executable not found: {args[0]}') from exc


def git(root: Path, *args: str, check: bool = True) -> str:
    result = run_process(['git', *args], cwd=root)
    if check and result.returncode != 0:
        raise WorkflowError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def project_root(value: str | None) -> Path:
    root = Path(value or os.getcwd()).resolve()
    if not root.is_dir():
        raise WorkflowError(f'Project root is not a directory: {root}')
    return root


def state_dir(root: Path) -> Path:
    return root / STATE_DIR_NAME


def state_file(root: Path) -> Path:
    return state_dir(root) / STATE_FILE_NAME


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)


def load_state(root: Path, required: bool = True) -> dict[str, Any]:
    path = state_file(root)
    if not path.exists():
        if required:
            raise WorkflowError(f'No active workflow state at {path}')
        return {}
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f'Invalid workflow state: {path}: {exc}') from exc
    if not isinstance(state, dict):
        raise WorkflowError(f'Workflow state must be a JSON object: {path}')
    return state


def save_state(root: Path, state: dict[str, Any]) -> None:
    state['updated_at'] = utc_now()
    atomic_write_json(state_file(root), state)


def read_optional(path: Path) -> str:
    if not path.exists():
        return '(not available)'
    text = path.read_text(encoding='utf-8', errors='replace').strip()
    return text or '(empty)'


def render(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace('{{' + key + '}}', value)
    unresolved = sorted(set(re.findall(r'\{\{([A-Z0-9_]+)\}\}', rendered)))
    if unresolved:
        raise WorkflowError(f"Unresolved prompt placeholders: {', '.join(unresolved)}")
    return rendered


def repository_context(root: Path) -> str:
    tracked = git(root, 'ls-files', check=False).splitlines()
    top_files = '\n'.join(tracked[:250])
    status = git(root, 'status', '--short', check=False)
    remotes = git(root, 'remote', '-v', check=False)
    return (
        f"Repository: {root.name}\n"
        f"Branch: {git(root, 'branch', '--show-current', check=False) or '(detached)'}\n"
        f"HEAD: {git(root, 'rev-parse', 'HEAD', check=False) or '(unknown)'}\n"
        f"Working tree status:\n{status or '(clean)'}\n\n"
        f"Remotes (informational only; workflow must not push):\n{remotes or '(none)'}\n\n"
        f"First tracked files (maximum 250):\n{top_files or '(none)'}\n"
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    failures: list[str] = []
    print(f'Python: {sys.version.split()[0]}')
    if sys.version_info < (3, 11):
        failures.append('Python 3.11 or later is required')

    for executable in ('git', 'codex'):
        path = shutil.which(executable)
        print(f'{executable}: {path or "not found"}')
        if path is None:
            failures.append(f'{executable} is not installed or not on PATH')

    inside = git(root, 'rev-parse', '--is-inside-work-tree', check=False)
    print(f'Git repository: {inside == "true"}')
    if inside != 'true':
        failures.append(f'{root} is not inside a Git worktree')

    if shutil.which('codex'):
        version = run_process(['codex', '--version'], cwd=root)
        print(f'Codex version: {(version.stdout or version.stderr).strip() or "unknown"}')
        auth = run_process(['codex', 'login', 'status'], cwd=root)
        print(f'Codex authentication: {"ready" if auth.returncode == 0 else "not ready"}')
        if auth.returncode != 0:
            failures.append('Codex is not authenticated; run `codex login`')

    if failures:
        print('\nDoctor found problems:', file=sys.stderr)
        for failure in failures:
            print(f'- {failure}', file=sys.stderr)
        return 1
    print('\nAll required local prerequisites are available.')
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    if git(root, 'rev-parse', '--is-inside-work-tree', check=False) != 'true':
        raise WorkflowError('Run initialization inside a Git worktree')

    existing = load_state(root, required=False)
    if existing and existing.get('status') not in TERMINAL_STATUSES:
        if args.reuse:
            print(state_file(root))
            return 0
        if not args.force:
            raise WorkflowError(
                'An active workflow already exists. Use `status`, `cancel`, `--reuse`, or `--force`.'
            )

    feature = args.feature.strip()
    if not feature:
        raise WorkflowError('Feature idea must not be empty')

    directory = state_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    context = repository_context(root)
    (directory / 'feature-request.md').write_text(feature + '\n', encoding='utf-8')
    (directory / 'repository-context.txt').write_text(context, encoding='utf-8')

    baseline = git(root, 'rev-parse', 'HEAD')
    dirty = git(root, 'status', '--short', check=False).splitlines()
    state: dict[str, Any] = {
        'version': 1,
        'run_id': dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
        'feature': feature,
        'status': 'active',
        'phase': 'initialized',
        'created_at': utc_now(),
        'updated_at': utc_now(),
        'baseline': {
            'commit': baseline,
            'branch': git(root, 'branch', '--show-current', check=False),
            'dirty_entries_at_init': dirty,
        },
        'max_review_rounds': args.max_review_rounds,
        'review_round': 0,
        'stop_gate_blocks': 0,
        'artifacts': {
            'feature_request': str(directory / 'feature-request.md'),
            'repository_context': str(directory / 'repository-context.txt'),
        },
        'verification': {'checks': [], 'passed': False},
        'reviews': [],
        'adversarial_reviews': [],
        'risk': {'requires_adversarial_review': False, 'reasons': []},
        'notes': [],
    }
    save_state(root, state)
    print(state_file(root))
    return 0


def prompt_values(root: Path, state: dict[str, Any]) -> dict[str, str]:
    directory = state_dir(root)
    previous_review = '(none)'
    if state.get('reviews'):
        latest = Path(state['reviews'][-1]['path'])
        previous_review = read_optional(latest)
        triage = directory / f"triage-{state['reviews'][-1]['round']:02d}.md"
        if triage.exists():
            previous_review += '\n\nTRIAGE\n' + read_optional(triage)

    latest_review = previous_review
    return {
        'FEATURE': state.get('feature', '(missing)'),
        'BASELINE': state.get('baseline', {}).get('commit', '(missing)'),
        'REPOSITORY_CONTEXT': read_optional(directory / 'repository-context.txt'),
        'CODEX_SPEC': read_optional(directory / 'feature-spec.codex.json'),
        'ACCEPTED_SPEC': read_optional(directory / 'accepted-spec.md'),
        'ACCEPTED_PLAN': read_optional(directory / 'accepted-plan.md'),
        'VERIFICATION': json.dumps(state.get('verification', {}), indent=2),
        'PREVIOUS_REVIEW': previous_review,
        'LATEST_REVIEW': latest_review,
    }


def cmd_codex(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    state = load_state(root)
    if state.get('status') != 'active':
        raise WorkflowError(f"Workflow is not active: {state.get('status')}")

    phase = args.phase
    prompt_rel, schema_rel, static_output = PHASE_OUTPUTS[phase]
    directory = state_dir(root)

    if phase == 'plan' and not (directory / 'accepted-spec.md').exists():
        raise WorkflowError('Create `.ai/autonomous-development/accepted-spec.md` before planning')
    if phase in {'review', 'adversarial'}:
        if not (directory / 'accepted-plan.md').exists():
            raise WorkflowError('Create `.ai/autonomous-development/accepted-plan.md` before review')
        if not state.get('verification', {}).get('checks'):
            raise WorkflowError('Record at least one verification check before review')

    if phase == 'review':
        next_round = int(state.get('review_round', 0)) + 1
        maximum = int(state.get('max_review_rounds', 3))
        if next_round > maximum:
            state['status'] = 'blocked'
            state['phase'] = 'review-budget-exhausted'
            state['notes'].append(f'Maximum review rounds exhausted ({maximum})')
            save_state(root, state)
            raise WorkflowError(f'Maximum review rounds exhausted ({maximum})')
        output_name = f'review-{next_round:02d}.codex.json'
    elif phase == 'adversarial':
        index = len(state.get('adversarial_reviews', [])) + 1
        output_name = f'adversarial-{index:02d}.codex.json'
    else:
        output_name = static_output
        assert output_name is not None

    template = (PLUGIN_ROOT / prompt_rel).read_text(encoding='utf-8')
    prompt = render(template, prompt_values(root, state))
    prompt_path = directory / f'{phase}.prompt.md'
    prompt_path.write_text(prompt, encoding='utf-8')
    output_path = directory / output_name

    command = [
        'codex',
        'exec',
        '--sandbox',
        'read-only',
        '--output-schema',
        str(PLUGIN_ROOT / schema_rel),
        '--output-last-message',
        str(output_path),
        '-',
    ]
    result = run_process(command, cwd=root, input_text=prompt)
    if result.returncode != 0:
        error_path = directory / f'{phase}.codex.stderr.log'
        error_path.write_text(result.stderr, encoding='utf-8')
        state['notes'].append(f'Codex {phase} failed; see {error_path}')
        save_state(root, state)
        raise WorkflowError(result.stderr.strip() or f'Codex {phase} failed')

    try:
        parsed = json.loads(output_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f'Codex did not produce valid JSON at {output_path}: {exc}') from exc
    if not isinstance(parsed, dict):
        raise WorkflowError(f'Codex output must be an object: {output_path}')

    state['artifacts'][phase] = str(output_path)
    if phase == 'enhance':
        state['phase'] = 'idea-enhanced'
    elif phase == 'plan':
        state['phase'] = 'plan-proposed'
    elif phase == 'review':
        state['review_round'] = next_round
        state['phase'] = 'reviewed'
        state['reviews'].append(
            {'round': next_round, 'path': str(output_path), 'verdict': parsed.get('verdict')}
        )
    elif phase == 'adversarial':
        state['phase'] = 'adversarially-reviewed'
        state['adversarial_reviews'].append(
            {'round': index, 'path': str(output_path), 'verdict': parsed.get('verdict')}
        )
    state['stop_gate_blocks'] = 0
    save_state(root, state)
    print(output_path)
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    state = load_state(root)
    source = Path(args.file).resolve()
    if not source.is_file():
        raise WorkflowError(f'Accepted artifact does not exist: {source}')
    destination_name = 'accepted-spec.md' if args.kind == 'spec' else 'accepted-plan.md'
    destination = state_dir(root) / destination_name
    destination.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
    state['artifacts'][f'accepted_{args.kind}'] = str(destination)
    state['phase'] = 'spec-accepted' if args.kind == 'spec' else 'plan-accepted'
    state['stop_gate_blocks'] = 0
    save_state(root, state)
    print(destination)
    return 0


def slug(value: str) -> str:
    clean = re.sub(r'[^a-zA-Z0-9._-]+', '-', value.strip()).strip('-').lower()
    return clean[:80] or 'check'


def cmd_run_check(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    state = load_state(root)
    command = list(args.command)
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        raise WorkflowError('Provide a verification command after `--`')

    verification_dir = state_dir(root) / 'verification'
    verification_dir.mkdir(parents=True, exist_ok=True)
    index = len(state.get('verification', {}).get('checks', [])) + 1
    log_path = verification_dir / f'{index:02d}-{slug(args.name)}.log'
    started = utc_now()
    result = run_process(command, cwd=root)
    combined = (
        f"COMMAND: {json.dumps(command)}\n"
        f"STARTED: {started}\n"
        f"EXIT CODE: {result.returncode}\n\n"
        f"STDOUT\n{result.stdout}\n\nSTDERR\n{result.stderr}\n"
    )
    log_path.write_text(combined, encoding='utf-8')

    check_record = {
        'name': args.name,
        'command': command,
        'exit_code': result.returncode,
        'log': str(log_path),
        'started_at': started,
        'completed_at': utc_now(),
    }
    state.setdefault('verification', {}).setdefault('checks', []).append(check_record)
    checks = state['verification']['checks']
    effective_checks = latest_verification_checks(checks)
    state['verification']['passed'] = bool(effective_checks) and all(
        c['exit_code'] == 0 for c in effective_checks
    )
    state['phase'] = 'verified' if state['verification']['passed'] else 'verification-failed'
    state['stop_gate_blocks'] = 0
    save_state(root, state)

    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    print(f'\nVerification log: {log_path}', file=sys.stderr)
    return result.returncode


def cmd_set_phase(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    state = load_state(root)
    state['phase'] = args.phase
    if args.note:
        state.setdefault('notes', []).append(args.note)
    state['stop_gate_blocks'] = 0
    save_state(root, state)
    print(args.phase)
    return 0


def cmd_set_risk(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    state = load_state(root)
    state.setdefault('risk', {})['requires_adversarial_review'] = args.require_adversarial
    if args.reason:
        state['risk'].setdefault('reasons', []).append(args.reason)
    state['stop_gate_blocks'] = 0
    save_state(root, state)
    return 0


def latest_verification_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the latest result for each logical verification check name."""
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for check in checks:
        name = str(check.get('name', 'unnamed'))
        if name not in latest:
            order.append(name)
        latest[name] = check
    return [latest[name] for name in order]


def unresolved_severe_findings(review: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        finding
        for finding in review.get('findings', [])
        if isinstance(finding, dict) and finding.get('severity') in {'critical', 'high'}
    ]


def cmd_evaluate(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    state = load_state(root)
    directory = state_dir(root)
    reasons: list[str] = []

    for required in ('accepted-spec.md', 'accepted-plan.md'):
        if not (directory / required).exists():
            reasons.append(f'Missing {required}')

    checks = latest_verification_checks(state.get('verification', {}).get('checks', []))
    if not checks:
        reasons.append('No verification checks recorded')
    elif any(check.get('exit_code') != 0 for check in checks):
        reasons.append('One or more verification checks failed')

    reviews = state.get('reviews', [])
    if not reviews:
        reasons.append('No Codex code review recorded')
    else:
        latest_path = Path(reviews[-1]['path'])
        review = json.loads(latest_path.read_text(encoding='utf-8'))
        if review.get('verdict') != 'pass':
            reasons.append(f"Latest Codex review verdict is {review.get('verdict')}")
        severe = unresolved_severe_findings(review)
        if severe:
            reasons.append(f'Latest review contains {len(severe)} critical/high finding(s)')

    requires_adversarial = bool(state.get('risk', {}).get('requires_adversarial_review'))
    if requires_adversarial:
        adversarial = state.get('adversarial_reviews', [])
        if not adversarial:
            reasons.append('High-risk change requires an adversarial review')
        elif adversarial[-1].get('verdict') != 'pass':
            reasons.append(
                f"Latest adversarial review verdict is {adversarial[-1].get('verdict')}"
            )

    if reasons:
        state['status'] = 'active'
        state['phase'] = 'completion-gates-failed'
        state['completion_gate_failures'] = reasons
        save_state(root, state)
        for reason in reasons:
            print(f'- {reason}', file=sys.stderr)
        return 1

    state['status'] = 'complete'
    state['phase'] = 'complete'
    state['completion_gate_failures'] = []
    save_state(root, state)
    print('Workflow complete')
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    state = load_state(root)
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    checks = latest_verification_checks(state.get('verification', {}).get('checks', []))
    passed = sum(1 for item in checks if item.get('exit_code') == 0)
    print(f"Run: {state.get('run_id')}")
    print(f"Status: {state.get('status')}")
    print(f"Phase: {state.get('phase')}")
    print(f"Feature: {state.get('feature')}")
    print(f"Baseline: {state.get('baseline', {}).get('commit')}")
    print(f"Verification: {passed}/{len(checks)} passing")
    print(
        f"Reviews: {state.get('review_round', 0)}/{state.get('max_review_rounds', 3)}"
    )
    if state.get('reviews'):
        print(f"Latest review: {state['reviews'][-1].get('verdict')}")
    if state.get('risk', {}).get('requires_adversarial_review'):
        verdict = (
            state.get('adversarial_reviews', [{}])[-1].get('verdict')
            if state.get('adversarial_reviews')
            else 'missing'
        )
        print(f'Adversarial review required: {verdict}')
    failures = state.get('completion_gate_failures', [])
    if failures:
        print('Remaining gates:')
        for failure in failures:
            print(f'- {failure}')
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    state = load_state(root)
    state['status'] = 'cancelled'
    state['phase'] = 'cancelled'
    if args.reason:
        state.setdefault('notes', []).append(args.reason)
    save_state(root, state)
    print('Workflow cancelled')
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    root = project_root(args.project_root)
    state = load_state(root)
    state['status'] = 'blocked'
    state['phase'] = 'blocked'
    state.setdefault('notes', []).append(args.reason)
    save_state(root, state)
    print('Workflow blocked')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', help='Target repository; defaults to current directory')
    sub = parser.add_subparsers(dest='command_name', required=True)

    doctor = sub.add_parser('doctor', help='Check Git, Python, Codex, and authentication')
    doctor.set_defaults(func=cmd_doctor)

    init = sub.add_parser('init', help='Initialize a workflow run')
    init.add_argument('--feature', required=True)
    init.add_argument('--max-review-rounds', type=int, default=3, choices=range(1, 6))
    init.add_argument('--reuse', action='store_true')
    init.add_argument('--force', action='store_true')
    init.set_defaults(func=cmd_init)

    codex = sub.add_parser('codex', help='Run a structured, read-only Codex phase')
    codex.add_argument('--phase', required=True, choices=sorted(PHASE_OUTPUTS))
    codex.set_defaults(func=cmd_codex)

    accept = sub.add_parser('accept', help='Record Claude-reconciled specification or plan')
    accept.add_argument('--kind', required=True, choices=('spec', 'plan'))
    accept.add_argument('--file', required=True)
    accept.set_defaults(func=cmd_accept)

    run_check = sub.add_parser('run-check', help='Execute and record one verification command')
    run_check.add_argument('--name', required=True)
    run_check.add_argument('command', nargs=argparse.REMAINDER)
    run_check.set_defaults(func=cmd_run_check)

    phase = sub.add_parser('set-phase', help='Update phase and optional note')
    phase.add_argument('--phase', required=True)
    phase.add_argument('--note')
    phase.set_defaults(func=cmd_set_phase)

    risk = sub.add_parser('set-risk', help='Set whether adversarial review is required')
    risk.add_argument('--require-adversarial', action=argparse.BooleanOptionalAction, default=True)
    risk.add_argument('--reason')
    risk.set_defaults(func=cmd_set_risk)

    evaluate = sub.add_parser('evaluate', help='Evaluate all completion gates')
    evaluate.set_defaults(func=cmd_evaluate)

    status = sub.add_parser('status', help='Show workflow state')
    status.add_argument('--json', action='store_true')
    status.set_defaults(func=cmd_status)

    cancel = sub.add_parser('cancel', help='Cancel the active workflow')
    cancel.add_argument('--reason')
    cancel.set_defaults(func=cmd_cancel)

    block = sub.add_parser('block', help='Mark the workflow blocked')
    block.add_argument('--reason', required=True)
    block.set_defaults(func=cmd_block)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except WorkflowError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('error: interrupted', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
