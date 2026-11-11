import io
import json
import subprocess
import unittest
from unittest.mock import patch

import remote_transport as transport


class Input(io.BytesIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class Process:
    def __init__(self, stdout=b'{"ok": true}', stderr=b'', code=0):
        self.stdin = Input()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = code
        self.killed = False
        self.waited = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -1

    def wait(self):
        self.waited = True
        return self.returncode


class TransportTests(unittest.TestCase):
    def assert_unknown(self, result, reason):
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['error'], reason)
        self.assertIn('may still run', result['note'])
        self.assertIn('do not automatically retry', result['note'])

    def call(self, process, **kwargs):
        with patch.object(transport.subprocess, 'Popen', return_value=process) as popen:
            result = transport.invoke(kwargs.get('host', 'user@example.com'),
                                      kwargs.get('path', '/opt/bin/runner'),
                                      kwargs.get('request', {'message': 'hello'}),
                                      kwargs.get('timeout', 1))
        return result, popen

    def test_request_and_ssh_options(self):
        p = Process()
        result, popen = self.call(p, request={'message': 'hello; $(secret)'})
        self.assertEqual(result, {'ok': True})
        args = popen.call_args.args[0]
        self.assertEqual(args[-2:], ['user@example.com', '/opt/bin/runner'])
        self.assertIn('BatchMode=yes', args)
        self.assertIn('StrictHostKeyChecking=yes', args)
        self.assertEqual(json.loads(p.stdin.saved), {'message': 'hello; $(secret)'})
        self.assertTrue(p.waited)

    def test_windows_runner_path_and_shell_metacharacters(self):
        result, popen = self.call(Process(), path='C:/gogogoogle/runner.cmd')
        self.assertEqual(result, {'ok': True})
        self.assertEqual(popen.call_args.args[0][-1], 'C:/gogogoogle/runner.cmd')
        for path in ('C:/gogogoogle/../runner.cmd', 'C:/gogogoogle/a&whoami.cmd',
                     'C:/gogogoogle/a b.cmd', 'C:/gogogoogle/%TEMP%.cmd', 'C:runner.cmd'):
            result, popen = self.call(Process(), path=path)
            self.assertEqual(result, {'error': 'invalid runner path'})
            popen.assert_not_called()

    def test_invalid_inputs_never_start_ssh(self):
        for host in ('-oProxyCommand=x', 'a b', 'host;id', 'user@-host', 'host..com', 'ssh://host', ''):
            with self.subTest(host=host):
                result, popen = self.call(Process(), host=host)
                self.assertEqual(result, {'error': 'invalid host'})
                popen.assert_not_called()
        for path in ('runner', '/a/../b', '/a;id', '/a b', '/a\nwhoami', '/a//b', '/a/$(id)'):
            with self.subTest(path=path):
                result, popen = self.call(Process(), path=path)
                self.assertEqual(result, {'error': 'invalid runner path'})
                popen.assert_not_called()
        for timeout in (0, -1, float('inf'), float('nan'), True):
            result, popen = self.call(Process(), timeout=timeout)
            self.assertEqual(result, {'error': 'invalid timeout'})
            popen.assert_not_called()

    def test_failures_do_not_expose_stderr(self):
        for p, reason in ((Process(stderr=b'password secret', code=255), 'ssh failed'),
                          (Process(stdout=b'password secret'), 'invalid response')):
            result, _ = self.call(p)
            self.assertNotIn('secret', str(result))
            self.assert_unknown(result, reason)
        for response in (b'[]', b'{"x": NaN}', b'\xff', b'{"x":1} trailing'):
            self.assert_unknown(self.call(Process(stdout=response))[0], 'invalid response')

    def test_bounds_both_streams(self):
        for p in (Process(stdout=b'x' * (transport._STDOUT_LIMIT + 1)),
                  Process(stderr=b'x' * (transport._STDERR_LIMIT + 1))):
            self.assert_unknown(self.call(p)[0], 'output limit exceeded')
        result, popen = self.call(Process(), request={'x': 'a' * transport._REQUEST_LIMIT})
        self.assertEqual(result, {'error': 'request too large'})
        popen.assert_not_called()

    def test_timeout_kills_and_reaps(self):
        p = Process(code=None)
        result, _ = self.call(p, timeout=0.02)
        self.assert_unknown(result, 'ssh timed out')
        self.assertTrue(p.killed)
        self.assertTrue(p.waited)

    def test_start_failure_is_sanitized(self):
        with patch.object(transport.subprocess, 'Popen', side_effect=OSError('secret')):
            self.assertEqual(transport.invoke('host', '/runner', {}, 1), {'error': 'unable to start ssh'})

    def test_pipe_and_cleanup_failures_are_unknown(self):
        p = Process()
        p.stdin.close()
        self.assert_unknown(self.call(p)[0], 'ssh failed')
        p = Process()
        with patch.object(p, 'wait', side_effect=OSError('secret')):
            result, _ = self.call(p)
        self.assert_unknown(result, 'ssh cleanup failed')
        self.assertNotIn('secret', str(result))


if __name__ == '__main__':
    unittest.main()
