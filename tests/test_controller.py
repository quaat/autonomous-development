from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / 'scripts/controller.py'
STOP_GATE = ROOT / 'scripts/stop_gate.py'


class ControllerTests(unittest.TestCase):
    def make_repo(self) -> Path:
        temp = Path(tempfile.mkdtemp())
        subprocess.run(['git', 'init', '-q', str(temp)], check=True)
        subprocess.run(['git', '-C', str(temp), 'config', 'user.email', 'test@example.com'], check=True)
        subprocess.run(['git', '-C', str(temp), 'config', 'user.name', 'Test User'], check=True)
        (temp / 'README.md').write_text('# Test\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(temp), 'add', 'README.md'], check=True)
        subprocess.run(['git', '-C', str(temp), 'commit', '-qm', 'initial'], check=True)
        return temp

    def run_controller(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ['python3', str(CONTROLLER), '--project-root', str(repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_init_and_status(self) -> None:
        repo = self.make_repo()
        result = self.run_controller(repo, 'init', '--feature', 'Add a test feature')
        self.assertEqual(result.returncode, 0, result.stderr)
        state_path = repo / '.ai/autonomous-development/run-state.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        self.assertEqual(state['status'], 'active')
        self.assertEqual(state['feature'], 'Add a test feature')

        status = self.run_controller(repo, 'status')
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn('Phase: initialized', status.stdout)

    def test_record_passing_check(self) -> None:
        repo = self.make_repo()
        self.assertEqual(
            self.run_controller(repo, 'init', '--feature', 'Feature').returncode, 0
        )
        result = self.run_controller(
            repo, 'run-check', '--name', 'truth', '--', 'python3', '-c', 'print("ok")'
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(
            (repo / '.ai/autonomous-development/run-state.json').read_text(encoding='utf-8')
        )
        self.assertTrue(state['verification']['passed'])


    def test_rerun_supersedes_failed_check(self) -> None:
        repo = self.make_repo()
        self.assertEqual(
            self.run_controller(repo, 'init', '--feature', 'Feature').returncode, 0
        )
        failed = self.run_controller(
            repo, 'run-check', '--name', 'tests', '--', 'python3', '-c', 'raise SystemExit(1)'
        )
        self.assertEqual(failed.returncode, 1)
        passed = self.run_controller(
            repo, 'run-check', '--name', 'tests', '--', 'python3', '-c', 'print("fixed")'
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)
        state = json.loads(
            (repo / '.ai/autonomous-development/run-state.json').read_text(encoding='utf-8')
        )
        self.assertTrue(state['verification']['passed'])

    def test_stop_gate_is_bounded(self) -> None:
        repo = self.make_repo()
        self.assertEqual(
            self.run_controller(repo, 'init', '--feature', 'Feature').returncode, 0
        )
        payload = json.dumps({'cwd': str(repo), 'hook_event_name': 'Stop'})
        for _ in range(3):
            result = subprocess.run(
                ['python3', str(STOP_GATE)],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn('"decision": "block"', result.stdout)
        final = subprocess.run(
            ['python3', str(STOP_GATE)],
            input=payload,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(final.returncode, 0)
        self.assertEqual(final.stdout, '')
        state = json.loads(
            (repo / '.ai/autonomous-development/run-state.json').read_text(encoding='utf-8')
        )
        self.assertEqual(state['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
