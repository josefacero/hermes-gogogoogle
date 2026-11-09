import tempfile
import unittest
from pathlib import Path

import remote_runner


class Backend:
    def __init__(self):
        self.calls = []

    def delegate(self, args):
        self.calls.append(args)
        return '{"status":"OK"}'


class RunnerTests(unittest.TestCase):
    def test_worker_validates_workspace_and_runs_with_worker_path(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / 'project'
            workspace.mkdir()
            backend = Backend()
            result = remote_runner.execute({'task': 'review', 'workdir': str(workspace), 'role': 'review'}, [root], False, backend)
            self.assertEqual(result['status'], 'OK')
            self.assertEqual(backend.calls[0]['workdir'], str(workspace.resolve()))
            self.assertFalse(backend.calls[0]['background'])

    def test_outside_workspace_and_privileged_role_are_denied(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            backend = Backend()
            self.assertIn('error', remote_runner.execute({'task': 'x', 'workdir': outside, 'role': 'review'}, [root], False, backend))
            self.assertIn('error', remote_runner.execute({'task': 'x', 'workdir': root, 'role': 'do'}, [root], False, backend))
            self.assertEqual(backend.calls, [])

    def test_missing_roots_background_and_invalid_values_fail_closed(self):
        backend = Backend()
        for request in ({'task': 'x', 'workdir': '.', 'role': 'review'},
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
