import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import remote_inbox
import remote_relay

REQUEST = {'task': 'approved task', 'workdir': 'C:/fixture', 'role': 'review',
           'minutes': 1, 'verify': False, 'background': False}


class RelayInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = Path(self.tmp.name)

    def start_inbox(self, run, approved='approved task'):
        served = []
        thread = threading.Thread(target=lambda: served.append(
            remote_inbox.serve_one(self.spool, 5, approved, run)))
        thread.start()
        until = time.monotonic() + 3
        while not (self.spool / 'ready').exists() and time.monotonic() < until:
            time.sleep(0.01)
        return thread, served

    def test_relay_refuses_and_queues_nothing_without_an_inbox(self):
        result = remote_relay.relay(b'{}', self.spool, threading.Event(), 1)
        self.assertEqual(result, {'error': 'worker inbox is not running'})
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_request_reaches_inbox_and_answer_comes_back_once(self):
        calls = []

        def run(request, deadline, disconnected):
            calls.append((request, deadline))
            return {'status': 'OK'}
        thread, served = self.start_inbox(run)
        data = json.dumps(REQUEST).encode()
        self.assertEqual(remote_relay.relay(data, self.spool, threading.Event(), 5), {'status': 'OK'})
        thread.join(3)
        self.assertEqual(served, [True])
        self.assertEqual(calls, [(REQUEST, remote_inbox.remote_supervisor.WORKER_DEADLINE_SECONDS)])
        # one request only: the inbox is gone, so a second call is refused
        self.assertEqual(remote_relay.relay(data, self.spool, threading.Event(), 1),
                         {'error': 'worker inbox is not running'})

    def test_unapproved_task_is_refused_without_running(self):
        calls = []
        thread, _ = self.start_inbox(lambda *a: calls.append(a) or {'status': 'OK'}, approved='another task')
        result = remote_relay.relay(json.dumps(REQUEST).encode(), self.spool, threading.Event(), 5)
        thread.join(3)
        self.assertEqual(result, {'error': 'request refused by worker policy'})
        self.assertEqual(calls, [])

    def test_client_disconnect_reaches_the_supervisor(self):
        seen = []

        def run(request, deadline, disconnected):
            seen.append(disconnected.wait(3))
            return {'status': 'UNKNOWN', 'error': 'connection lost'}
        thread, _ = self.start_inbox(run)
        gone = threading.Event()
        threading.Timer(0.3, gone.set).start()
        self.assertIsNone(remote_relay.relay(json.dumps(REQUEST).encode(), self.spool, gone, 5))
        thread.join(5)
        self.assertEqual(seen, [True])

    def test_silent_inbox_gives_unknown_and_cancels(self):
        (self.spool / 'ready').touch()
        result = remote_relay.relay(b'{}', self.spool, threading.Event(), 0.3)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertTrue((self.spool / 'cancel').exists())
        self.assertFalse((self.spool / 'request.json').exists())

    def test_relay_process_exits_cleanly_while_input_stays_open(self):
        (self.spool / 'ready').touch()
        (self.spool / 'response.json').write_text('{"status": "OK"}')
        relay = subprocess.Popen([sys.executable, str(Path(remote_relay.__file__))], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env={**__import__('os').environ, 'GOGOGOOGLE_WORKER_SPOOL': str(self.spool)})
        try:
            relay.stdin.write(b'{}\n')
            relay.stdin.flush()
            self.assertEqual(json.loads(relay.stdout.readline()), {'status': 'OK'})
            self.assertEqual(relay.wait(timeout=5), 0)
            self.assertEqual(relay.stderr.read(), b'')
        finally:
            if relay.poll() is None:
                relay.kill()
                relay.wait(timeout=5)
            for stream in (relay.stdin, relay.stdout, relay.stderr):
                stream.close()


if __name__ == '__main__':
    unittest.main()
