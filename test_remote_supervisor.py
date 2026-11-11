import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(os.name == 'nt', 'Windows supervisor')
class SupervisorTests(unittest.TestCase):
    def test_main_exits_cleanly_with_client_input_open(self):
        import json
        request = {'task': 'review', 'workdir': 'C:/fixture', 'role': 'review',
                   'minutes': 1, 'verify': False, 'background': False}
        code = ('import remote_supervisor as s; '
                's.run_supervised=lambda *args: {"status":"OK"}; s.main()')
        env = os.environ.copy()
        env['GOGOGOOGLE_WORKER_APPROVED_TASK'] = 'review'
        process = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            process.stdin.write((json.dumps(request) + '\n').encode())
            process.stdin.flush()
            self.assertEqual(process.wait(timeout=5), 0, process.stderr.read().decode())
            self.assertEqual(json.loads(process.stdout.read())['status'], 'OK')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_main_runs_real_runner_with_stub_backend(self):
        import json
        import shutil
        with tempfile.TemporaryDirectory() as root:
            for name in ('remote_supervisor.py', 'remote_runner.py'):
                shutil.copy(Path(__file__).with_name(name), root)
            (Path(root) / '__init__.py').write_text('def delegate(args):\n    return \'{"status":"OK"}\'\n')
            env = os.environ.copy()
            env.update(GOGOGOOGLE_WORKER_APPROVED_TASK='review', GOGOGOOGLE_WORKER_ROOTS=root)
            request = {'task': 'review', 'workdir': root, 'role': 'review', 'minutes': 1,
                       'verify': False, 'background': False}
            process = subprocess.Popen([sys.executable, str(Path(root) / 'remote_supervisor.py')],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            try:
                process.stdin.write((json.dumps(request) + '\n').encode())
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=10), 0, process.stderr.read().decode())
                self.assertEqual(json.loads(process.stdout.read())['status'], 'OK')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_disconnection_cancels_running_job(self):
        import remote_supervisor
        import threading
        disconnected = threading.Event()
        disconnected.set()
        code = 'import sys,time; sys.stdin.buffer.read(); time.sleep(60)'
        start = time.monotonic()
        result = remote_supervisor._run_job([sys.executable, '-c', code], b'go', 20, disconnected)
        self.assertEqual(result['status'], 'DISCONNECTED')
        self.assertLess(time.monotonic() - start, 5)

    def test_open_input_then_disconnect_cleans_process_tree(self):
        import ctypes
        import shutil
        with tempfile.TemporaryDirectory() as root:
            for name in ('disconnect_probe.py', 'remote_supervisor.py'):
                shutil.copy(Path(__file__).with_name(name), root)
            marker = Path(root) / 'disconnect-probe-pids.json'
            probe = subprocess.Popen([sys.executable, str(Path(root) / 'disconnect_probe.py')],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handles = []
            try:
                probe.stdin.write(b'go\n')
                probe.stdin.flush()
                self.assertEqual(probe.stdout.readline().strip(), b'PROBE READY')
                import json
                handles = [kernel.OpenProcess(0x100000, False, pid) for pid in json.loads(marker.read_text())]
                self.assertTrue(all(handles))
                start = time.monotonic()
                probe.stdin.close()
                self.assertEqual(probe.wait(timeout=5), 0)
                self.assertLess(time.monotonic() - start, 5)
                self.assertIn(b'DISCONNECTED', probe.stdout.read())
                self.assertTrue(all(kernel.WaitForSingleObject(h, 3000) == 0 for h in handles))
            finally:
                if probe.poll() is None:
                    probe.kill()
                    probe.wait(timeout=5)
                for stream in (probe.stdin, probe.stdout, probe.stderr):
                    if not stream.closed:
                        stream.close()
                for handle in handles:
                    if handle:
                        kernel.CloseHandle(handle)

    def test_supervisor_requires_positive_worker_owned_deadline(self):
        import remote_supervisor
        for deadline in (0, -1, True, None):
            with self.subTest(deadline=deadline):
                with self.assertRaises(ValueError):
                    remote_supervisor.run_supervised({}, deadline)

    def test_unapproved_request_never_spawns(self):
        import remote_supervisor
        with patch.object(remote_supervisor, '_run_job') as run:
            self.assertEqual(remote_supervisor.run_supervised({'role': 'do'}, 150),
                             {'error': 'request refused by worker policy'})
            run.assert_not_called()

    def test_redirecting_interpreter_is_refused(self):
        import remote_supervisor
        request = {'task': 'review', 'workdir': 'C:/fixture', 'role': 'review',
                   'minutes': 1, 'verify': False, 'background': False}
        with patch.object(remote_supervisor.os.path, 'samefile', return_value=False), \
                patch.object(remote_supervisor, '_run_job') as run:
            self.assertEqual(remote_supervisor.run_supervised(request, 150),
                             {'error': 'base Python interpreter required'})
            run.assert_not_called()

    def test_valid_request_passes_to_job_and_returns_json(self):
        import remote_supervisor
        request = {'task': 'review', 'workdir': 'C:/fixture', 'role': 'review',
                   'minutes': 1, 'verify': False, 'background': False}
        with patch.object(remote_supervisor, '_run_job', return_value={
                'status': 'DONE', 'returncode': 0, 'output': b'{"status":"OK"}'}) as run:
            self.assertEqual(remote_supervisor.run_supervised(request, 150), {'status': 'OK'})
            self.assertEqual(run.call_args.args[2], 150)

    def test_policy_refuses_do_verify_resume_and_missing_role(self):
        import remote_supervisor
        base = {'task': 'review', 'workdir': 'C:/fixture', 'role': 'review',
                'minutes': 1, 'verify': False, 'background': False}
        for changes in ({'role': 'do'}, {'verify': True}, {'resume': 'abc'},
                        {'resume': ''}, {'background': True}, {'role': None},
                        {'minutes': 30}, {'deadline_seconds': 99999}):
            with self.subTest(changes=changes):
                self.assertFalse(remote_supervisor.allowed_request({**base, **changes}, 120))
        self.assertTrue(remote_supervisor.allowed_request({**base, 'minutes': 1}, 120))

    def test_assignment_failure_does_not_send_request(self):
        import remote_supervisor
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'started'
            code = 'import sys,pathlib; d=sys.stdin.buffer.read(); pathlib.Path(sys.argv[1]).write_text("started") if d else None'
            with patch.object(remote_supervisor, '_assign_job', side_effect=OSError('test')):
                with self.assertRaises(OSError):
                    remote_supervisor._run_job([sys.executable, '-c', code, str(marker)], b'request', 2)
            time.sleep(0.2)
            self.assertFalse(marker.exists())

    def test_oversized_output_is_bounded_and_terminated(self):
        import remote_supervisor
        code = 'import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b"x"*2000000); sys.stdout.flush()'
        result = remote_supervisor._run_job([sys.executable, '-c', code], b'go', 5)
        self.assertEqual(result['status'], 'OUTPUT LIMIT')
        self.assertNotIn('output', result)

    def test_job_timeout_terminates_child_and_grandchild(self):
        import ctypes
        import remote_supervisor
        import threading
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'pids'
            code = ('import sys,subprocess,time,os,pathlib; sys.stdin.buffer.read(); '
                    'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                    'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())+" "+str(p.pid)); time.sleep(60)')
            result = []
            t = threading.Thread(target=lambda: result.append(remote_supervisor._run_job(
                [sys.executable, '-c', code, str(marker)], b'go', 2)))
            t.start()
            until = time.monotonic() + 1.5
            while not marker.exists() and time.monotonic() < until:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), 'test processes did not start')
            pids = [int(x) for x in marker.read_text().split()]
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handles = [kernel.OpenProcess(0x100000, False, pid) for pid in pids]
            try:
                self.assertTrue(all(handles))
                t.join(6)
                self.assertFalse(t.is_alive())
                self.assertEqual(result[0]['status'], 'TIMED OUT')
                self.assertTrue(all(kernel.WaitForSingleObject(h, 3000) == 0 for h in handles))
            finally:
                for handle in handles:
                    if handle:
                        kernel.CloseHandle(handle)

    def test_forced_supervisor_exit_terminates_tree(self):
        import ctypes
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'pids'
            child = ('import sys,subprocess,time,os,pathlib; sys.stdin.buffer.read(); '
                     'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                     'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())+" "+str(p.pid)); time.sleep(60)')
            parent = ('import remote_supervisor,sys; remote_supervisor._run_job('
                      '[sys.executable,"-c",sys.argv[1],sys.argv[2]],b"go",60)')
            supervisor = subprocess.Popen([sys.executable, '-c', parent, child, str(marker)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handles = []
            try:
                until = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < until:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), 'test processes did not start')
                handles = [kernel.OpenProcess(0x100000, False, int(pid)) for pid in marker.read_text().split()]
                self.assertTrue(all(handles))
                supervisor.kill()
                supervisor.wait(timeout=5)
                self.assertTrue(all(kernel.WaitForSingleObject(h, 3000) == 0 for h in handles))
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.wait(timeout=5)
                for handle in handles:
                    if handle:
                        kernel.CloseHandle(handle)

    def test_orphaned_grandchild_is_terminated_at_deadline(self):
        import ctypes
        import remote_supervisor
        import threading
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / 'pid'
            code = ('import sys,subprocess,time,pathlib; sys.stdin.buffer.read(); '
                    'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
                    'pathlib.Path(sys.argv[1]).write_text(str(p.pid))')
            result = []
            t = threading.Thread(target=lambda: result.append(remote_supervisor._run_job(
                [sys.executable, '-c', code, str(marker)], b'go', 2)))
            t.start()
            until = time.monotonic() + 1.5
            while not marker.exists() and time.monotonic() < until:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), 'grandchild did not start')
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x100000, False, int(marker.read_text()))
            try:
                self.assertTrue(handle)
                t.join(6)
                self.assertFalse(t.is_alive())
                self.assertEqual(result[0]['status'], 'TIMED OUT')
                self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0)
            finally:
                if handle:
                    kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
