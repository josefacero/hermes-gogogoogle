import json
import os
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
OTHER_ID = 'abcdef0123456789'


class RelayInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spool = Path(self.tmp.name)

    def start_inbox(self, run, approved='approved task', idle=5):
        served = []
        thread = threading.Thread(target=lambda: served.append(
            remote_inbox.serve_one(self.spool, idle, approved, run)))
        thread.start()
        until = time.monotonic() + 3
        while not (self.spool / 'ready').exists() and time.monotonic() < until:
            time.sleep(0.01)
        return thread, served

    def wait_for(self, pattern):
        until = time.monotonic() + 3
        while not list(self.spool.glob(pattern)) and time.monotonic() < until:
            time.sleep(0.01)
        return list(self.spool.glob(pattern))

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
        self.assertEqual(list(self.spool.iterdir()), [])  # every per-request file is gone
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
        self.assertTrue(list(self.spool.glob('cancel.*')))
        self.assertFalse(list(self.spool.glob('request.*.json')))

    def test_leftover_answer_for_another_request_is_not_returned(self):
        (self.spool / 'ready').touch()
        (self.spool / f'response.{OTHER_ID}.json').write_text('{"status": "OK", "stale": true}')
        result = remote_relay.relay(b'{}', self.spool, threading.Event(), 0.3)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertNotIn('stale', result)

    def test_second_relay_gets_busy_while_one_runs(self):
        release = threading.Event()
        thread, _ = self.start_inbox(lambda *a: release.wait(5) and {'status': 'OK'})
        first = []
        data = json.dumps(REQUEST).encode()
        worker = threading.Thread(target=lambda: first.append(
            remote_relay.relay(data, self.spool, threading.Event(), 5)))
        worker.start()
        self.wait_for('claimed.*')
        self.assertEqual(remote_relay.relay(data, self.spool, threading.Event(), 1),
                         {'error': 'worker inbox is busy'})
        release.set()
        worker.join(5)
        thread.join(5)
        self.assertEqual(first, [{'status': 'OK'}])

    def test_cancel_for_another_request_does_not_cancel_this_one(self):
        seen = []

        def run(request, deadline, disconnected):
            seen.append(disconnected.wait(0.6))
            return {'status': 'OK'}
        thread, _ = self.start_inbox(run)
        (self.spool / f'cancel.{OTHER_ID}').touch()
        result = remote_relay.relay(json.dumps(REQUEST).encode(), self.spool, threading.Event(), 5)
        thread.join(5)
        self.assertEqual(result, {'status': 'OK'})
        self.assertEqual(seen, [False])

    def test_second_inbox_refuses_to_start_and_leaves_the_files_alone(self):
        (self.spool / 'ready').touch()
        with self.assertRaises(SystemExit):
            remote_inbox.serve_one(self.spool, 1, 'approved task')
        self.assertTrue((self.spool / 'ready').exists())
        (self.spool / 'ready').unlink()
        (self.spool / f'claimed.{OTHER_ID}').touch()
        with self.assertRaises(SystemExit):
            remote_inbox.serve_one(self.spool, 1, 'approved task')
        self.assertTrue((self.spool / f'claimed.{OTHER_ID}').exists())

    def test_idle_inbox_closes_its_window_and_leaves_nothing(self):
        thread, served = self.start_inbox(lambda *a: {'status': 'OK'}, idle=0.3)
        thread.join(3)
        self.assertEqual(served, [False])
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_claim_without_request_answers_invalid_request(self):
        thread, served = self.start_inbox(lambda *a: {'status': 'OK'})
        os.rename(self.spool / 'ready', self.spool / f'claimed.{OTHER_ID}')
        thread.join(6)
        self.assertEqual(served, [True])
        self.assertEqual(json.loads((self.spool / f'response.{OTHER_ID}.json').read_text()),
                         {'error': 'invalid request'})
        self.assertFalse(list(self.spool.glob('claimed.*')))

    def test_relay_process_exits_cleanly_while_input_stays_open(self):
        (self.spool / 'ready').touch()

        def answer():
            for claim in self.wait_for('claimed.*'):
                done = self.spool / 'response.tmp'
                done.write_text('{"status": "OK"}')
                os.replace(done, self.spool / f'response.{claim.name.split(".", 1)[1]}.json')
        helper = threading.Thread(target=answer)
        helper.start()
        relay = subprocess.Popen([sys.executable, str(Path(remote_relay.__file__))], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env={**os.environ, 'GOGOGOOGLE_WORKER_SPOOL': str(self.spool)})
        try:
            relay.stdin.write(b'{}\n')
            relay.stdin.flush()
            self.assertEqual(json.loads(relay.stdout.readline()), {'status': 'OK'})
            self.assertEqual(relay.wait(timeout=5), 0)
            self.assertEqual(relay.stderr.read(), b'')
        finally:
            helper.join(5)
            if relay.poll() is None:
                relay.kill()
                relay.wait(timeout=5)
            for stream in (relay.stdin, relay.stdout, relay.stderr):
                stream.close()


if __name__ == '__main__':
    unittest.main()
