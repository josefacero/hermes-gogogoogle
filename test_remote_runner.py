import tempfile
import unittest
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import remote_runner


class Backend:
    def __init__(self):
        self.calls = []

    def delegate(self, args):
        self.calls.append(args)
        return '{"status":"OK"}'


class RunnerTests(unittest.TestCase):
    def test_deep_json_fails_cleanly_without_traceback(self):
        runner = Path(__file__).with_name('remote_runner.py')
        result = subprocess.run([sys.executable, str(runner)], input='[' * 2000 + ']' * 2000,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(json.loads(result.stdout), {'error': 'invalid request'})
        self.assertNotIn('Traceback', result.stderr)

    def test_missing_backend_returns_sanitized_error(self):
        with tempfile.TemporaryDirectory() as root:
            runner = Path(root) / 'remote_runner.py'
            shutil.copyfile(Path(__file__).with_name('remote_runner.py'), runner)
            result = subprocess.run([sys.executable, str(runner)], input='{}',
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(json.loads(result.stdout), {'status': 'UNKNOWN', 'error': 'worker failed'})
            self.assertNotIn('Traceback', result.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_windows_launcher_passes_request_and_requires_python(self):
        launcher = Path(__file__).with_name('remote_runner.cmd')
        env = os.environ.copy()
        env['GOGOGOOGLE_WORKER_PYTHON'] = sys.executable
        result = subprocess.run(f'"{launcher}"', shell=True,
                                input='not json', capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'error': 'invalid request'})
        env.pop('GOGOGOOGLE_WORKER_PYTHON')
        result = subprocess.run(f'"{launcher}"', shell=True,
                                input='{}', capture_output=True, text=True, env=env, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher')
    def test_windows_launcher_requires_worker_approved_task(self):
        launcher = Path(__file__).with_name('remote_runner.cmd')
        env = os.environ.copy()
        env['GOGOGOOGLE_WORKER_PYTHON'] = sys.executable
        env.pop('GOGOGOOGLE_WORKER_APPROVED_TASK', None)
        request = {'task': 'review', 'workdir': 'C:/fixture', 'role': 'review',
                   'minutes': 1, 'verify': False, 'background': False}
        result = subprocess.run(f'"{launcher}"', shell=True, input=json.dumps(request),
                                capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(json.loads(result.stdout), {'error': 'request refused by worker policy'})

    def test_worker_validates_workspace_and_runs_with_worker_path(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / 'project'
            workspace.mkdir()
            backend = Backend()
            result = remote_runner.execute({'task': 'review', 'workdir': str(workspace), 'role': 'review'}, [root], False, backend)
            self.assertEqual(result['status'], 'OK')
            self.assertEqual(backend.calls[0]['workdir'], str(workspace.resolve()))
            self.assertFalse(backend.calls[0]['background'])

    def test_worker_verifier_chain_is_one_model(self):
        # one 5-minute verifier fits the 480 s worker deadline; a fallback chain could not
        with tempfile.TemporaryDirectory() as root:
            backend = Backend()
            backend.VERIFIERS = ['first-model', 'second-model', 'third-model']
            remote_runner.execute({'task': 'review', 'workdir': root, 'role': 'review', 'verify': True}, [root], False, backend)
            self.assertEqual(backend.VERIFIERS, ['first-model'])

    def test_outside_workspace_and_privileged_role_are_denied(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            backend = Backend()
            self.assertIn('error', remote_runner.execute({'task': 'x', 'workdir': outside, 'role': 'review'}, [root], False, backend))
            self.assertIn('error', remote_runner.execute({'task': 'x', 'workdir': root, 'role': 'do'}, [root], False, backend))
            self.assertEqual(backend.calls, [])

    def test_missing_roots_background_and_invalid_values_fail_closed(self):
        backend = Backend()
        for request in ({'task': 'x', 'workdir': '.', 'role': 'review'},
                        {'task': 'x', 'workdir': '.'},
                        {'task': 'x', 'workdir': '.', 'role': 'review', 'background': True},
                        {'task': 'x', 'workdir': '.', 'role': 'review', 'verify': 'false'}):
            self.assertIn('error', remote_runner.execute(request, [], False, backend))
        self.assertFalse(backend.calls)

    def test_do_requires_worker_operator_opt_in(self):
        with tempfile.TemporaryDirectory() as root:
            backend = Backend()
            result = remote_runner.execute({'task': 'x', 'workdir': root, 'role': 'do'}, [root], True, backend)
            self.assertEqual(result['status'], 'OK')
            self.assertEqual(backend.calls[0]['role'], 'do')

    def test_missing_role_is_refused_even_when_do_is_enabled(self):
        with tempfile.TemporaryDirectory() as root:
            backend = Backend()
            result = remote_runner.execute({'task': 'x', 'workdir': root}, [root], True, backend)
            self.assertIn('error', result)
            self.assertFalse(backend.calls)

    def test_symlink_escape_is_refused(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            link = Path(root) / 'escape'
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('directory symlinks unavailable to this user')
            backend = Backend()
            result = remote_runner.execute({'task': 'x', 'workdir': str(link), 'role': 'review'}, [root], False, backend)
            self.assertIn('error', result)
            self.assertFalse(backend.calls)


if __name__ == '__main__':
    unittest.main()
