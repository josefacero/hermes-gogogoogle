import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
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

    def start_inbox(self, run, approved='approved task', idle=5, **options):
        served = []
        thread = threading.Thread(target=lambda: served.append(
            remote_inbox.serve_one(self.spool, idle, approved, run, **options)))
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

    def test_inbox_lock_keeps_a_second_inbox_out_and_is_released(self):
        (self.spool / 'inbox.lock').touch()  # another inbox owns the spool
        with self.assertRaises(SystemExit):
            remote_inbox.serve_one(self.spool, 1, 'approved task')
        self.assertTrue((self.spool / 'inbox.lock').exists())
        self.assertFalse((self.spool / 'ready').exists())

    def test_starting_inbox_deletes_no_leftover_however_old(self):
        # a relay may still be writing a request or reading the previous inbox's answer; age cannot prove otherwise
        fresh = self.spool / f'response.{OTHER_ID}.json'
        old = [self.spool / name for name in (
            'request.0123456789abcdef.tmp', 'response.0123456789abcdef.json', 'cancel.0123456789abcdef')]
        for path in (fresh, *old):
            path.write_text('{}')
        for path in old:
            os.utime(path, (0, 0))
        thread, served = self.start_inbox(lambda *a: {'status': 'OK'}, idle=0.3)
        thread.join(3)
        self.assertEqual(served, [False])
        for path in (fresh, *old):
            self.assertTrue(path.exists(), path.name)

    def test_inbox_holds_its_lock_while_it_runs_and_releases_it_when_it_ends(self):
        held = []  # checked from inside the served request, so no timer decides the outcome
        thread, served = self.start_inbox(lambda *a: held.append((self.spool / 'inbox.lock').exists()) or {'status': 'OK'})
        remote_relay.relay(json.dumps(REQUEST).encode(), self.spool, threading.Event(), 5)
        thread.join(3)
        self.assertEqual((held, served), ([True], [True]))
        self.assertFalse((self.spool / 'inbox.lock').exists())

    def test_request_cancelled_before_the_inbox_starts_it_never_runs(self):
        calls = []
        thread, served = self.start_inbox(lambda *a: calls.append(a) or {'status': 'OK'})
        (self.spool / f'request.{OTHER_ID}.json').write_text(json.dumps(REQUEST))
        (self.spool / f'cancel.{OTHER_ID}').touch()  # the relay gave up between its claim and the inbox reading it
        os.rename(self.spool / 'ready', self.spool / f'claimed.{OTHER_ID}')
        thread.join(3)
        self.assertEqual((calls, served), ([], [True]))

    def test_relay_removes_a_request_the_inbox_answered_without_reading(self):
        # an inbox that gave up waiting for a slow request answers and leaves; the late request file must not stay
        seen = []
        real_rename = os.rename

        def slow_rename(src, dst):
            real_rename(src, dst)
            if Path(dst).name.startswith('claimed.'):
                seen.append(self.wait_for('response.*.json'))  # hold the request back until the inbox has answered
        thread, served = self.start_inbox(lambda *a: {'status': 'OK'})
        with unittest.mock.patch.object(remote_relay.os, 'rename', slow_rename):
            result = remote_relay.relay(json.dumps(REQUEST).encode(), self.spool, threading.Event(), 5)
        thread.join(3)
        self.assertTrue(seen and seen[0])
        self.assertEqual(result, {'error': 'invalid request'})
        self.assertEqual(list(self.spool.iterdir()), [])

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

    def ask(self, task, mode=None, approved='approved task', run=None, **extra):
        """Send one request with `task` through an inbox in `mode`; return (answer, tasks the run saw)."""
        seen = []
        options = {} if mode is None else {'mode': mode}
        thread, _ = self.start_inbox(run or (lambda request, *a: seen.append(request['task']) or {'status': 'OK'}),
                                     approved=approved, **options)
        request = {**REQUEST, 'task': task, **extra}
        answer = remote_relay.relay(json.dumps(request).encode(), self.spool, threading.Event(), 5)
        thread.join(5)
        return answer, seen

    def test_default_mode_refuses_a_task_that_is_not_the_approved_one(self):
        answer, seen = self.ask('a different task')
        self.assertEqual(answer, {'error': 'request refused by worker policy'})
        self.assertEqual(seen, [])
        self.assertEqual(self.ask('a different task', mode='exact')[0], {'error': 'request refused by worker policy'})

    def test_lead_approved_mode_accepts_another_task_up_to_the_limit(self):
        answer, seen = self.ask('a different task', mode='lead-approved')
        self.assertEqual((answer, seen), ({'status': 'OK'}, ['a different task']))
        limit = remote_inbox.LEAD_TASK_LIMIT
        self.assertEqual(limit, 1000)
        self.assertEqual(self.ask('x' * limit, mode='lead-approved')[0], {'status': 'OK'})
        answer, seen = self.ask('x' * (limit + 1), mode='lead-approved')
        self.assertEqual((answer, seen), ({'error': 'request refused by worker policy'}, []))

    def test_lead_approved_mode_refuses_an_empty_or_non_text_task(self):
        for task in ('', '   ', None, 5):
            answer, seen = self.ask(task, mode='lead-approved')
            self.assertEqual((answer, seen), ({'error': 'request refused by worker policy'}, []), task)

    def test_unknown_mode_refuses_everything(self):
        answer, seen = self.ask('approved task', mode='anything')
        self.assertEqual((answer, seen), ({'error': 'request refused by worker policy'}, []))

    def test_verify_request_gets_the_longer_deadline(self):
        deadlines = []

        def run(request, deadline, disconnected):
            deadlines.append(deadline)
            return {'status': 'OK'}
        self.ask('approved task', mode='lead-approved', run=run, verify=True)
        self.ask('approved task', mode='lead-approved', run=run)
        self.assertEqual(deadlines, [480, 150])

    def test_supervisor_still_refuses_do_and_resume_in_both_modes(self):
        run = remote_inbox.remote_supervisor.run_supervised
        for mode in ('exact', 'lead-approved'):
            for extra in ({'role': 'do'}, {'resume': 'abc'}):
                answer, _ = self.ask('approved task', mode=mode, run=run, **extra)
                self.assertEqual(answer, {'error': 'request refused by worker policy'}, (mode, extra))

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
