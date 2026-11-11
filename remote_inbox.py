#!/usr/bin/env python3
"""Experimental desktop-session worker inbox: serves ONE relayed request under the supervisor, then exits.

The operator starts it by hand in the signed-in Windows session, where agy can read its sign-in.
Policy comes from this process's environment (approved task, worker roots), never from the request.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

import remote_supervisor

LIMIT = 1024 * 1024
LEAD_TASK_LIMIT = 1000  # short enough to read whole in the lead's approval prompt
REQUEST_WAIT_SECONDS = 2  # a relay writes the request before it claims, so this only covers a relay that died


def task_allowed(task, approved, mode):
    """Worker policy for the task text. The worker cannot see a human approval; `lead-approved` trusts the lead's."""
    if mode == 'exact':
        return bool(approved) and task == approved
    if mode == 'lead-approved':
        return isinstance(task, str) and bool(task.strip()) and len(task) <= LEAD_TASK_LIMIT
    return False  # unknown mode: refuse everything


def serve_one(spool, idle_seconds, approved, run=remote_supervisor.run_supervised, mode='exact'):
    """Wait for one request, answer it, return True. False when none arrived in time."""
    lock = spool / 'inbox.lock'
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))  # atomic: two inboxes starting together cannot both own the spool
    except FileExistsError:
        raise SystemExit('another inbox owns the spool; remove inbox.lock by hand if it is stale') from None
    try:
        return _serve_window(spool, idle_seconds, approved, run, mode)
    finally:
        lock.unlink(missing_ok=True)


def _serve_window(spool, idle_seconds, approved, run, mode):
    ready, closed = spool / 'ready', spool / 'closed'
    if ready.exists() or any(spool.glob('claimed.*')):
        raise SystemExit('another inbox or an unfinished claim is in the spool; remove the files by hand if it is stale')
    closed.unlink(missing_ok=True)
    # request.*, response.* and cancel.* leftovers stay: each carries a random id no new request reuses,
    # and deleting them here could take a file a relay is still writing or reading
    ready.touch()
    until = time.monotonic() + idle_seconds
    claimed = None
    while claimed is None:
        found = next(iter(spool.glob('claimed.*')), None)
        if found is not None:
            claimed = found
        elif time.monotonic() >= until:
            try:
                os.rename(ready, closed)  # one request only: nothing can claim a closed window
            except OSError:
                continue  # a relay claimed it in the same instant: serve that claim
            closed.unlink(missing_ok=True)
            return False
        else:
            time.sleep(0.1)
    request_id = claimed.name.split('.', 1)[1]
    request, response, cancel = (spool / f'{name}.{request_id}{ext}' for name, ext in (
        ('request', '.json'), ('response', '.json'), ('cancel', '')))
    try:
        result = _serve(spool, request, cancel, request_id, approved, run, mode)
        pending = spool / f'response.{request_id}.tmp'
        pending.write_text(json.dumps(result, ensure_ascii=True), encoding='ascii')
        os.replace(pending, response)
    finally:
        # the claim goes last: while it exists a second relay is told `busy`
        for leftover in (request, cancel, claimed):
            leftover.unlink(missing_ok=True)
    return True


def _serve(spool, request, cancel, request_id, approved, run, mode):
    """Read the claimed request and run it under policy; always returns the answer dict."""
    until = time.monotonic() + REQUEST_WAIT_SECONDS
    while not request.is_file() and time.monotonic() < until:
        time.sleep(0.05)
    try:
        if not re.fullmatch('[0-9a-f]{16}', request_id) or not request.is_file():
            raise ValueError('no request arrived for this claim')
        if request.stat().st_size > LIMIT:
            raise ValueError('request too large')
        data = request.read_bytes()
        request.unlink()
        parsed = json.loads(data)
        if isinstance(parsed, dict) and isinstance(parsed.get('task'), str):
            digest = hashlib.sha256(parsed['task'].encode('utf-8')).hexdigest()[:12]
            print(f'INBOX received a task: {len(parsed["task"])} chars, sha256 {digest}', flush=True)  # never the text
        if not isinstance(parsed, dict) or not task_allowed(parsed.get('task'), approved, mode):
            result = {'error': 'request refused by worker policy'}
        else:
            disconnected, finished = threading.Event(), threading.Event()

            def watch_cancel():
                while not finished.is_set():
                    if cancel.exists():
                        disconnected.set()
                        return
                    time.sleep(0.1)
            threading.Thread(target=watch_cancel, daemon=True).start()
            try:
                result = run(parsed, remote_supervisor.deadline_for(parsed), disconnected)
            finally:
                finished.set()
    except (ValueError, TypeError, RecursionError, OSError):
        result = {'error': 'invalid request'}
    return result


def main():
    spool = Path(os.environ.get('GOGOGOOGLE_WORKER_SPOOL', ''))
    if not spool.is_absolute() or not spool.is_dir():
        raise SystemExit('GOGOGOOGLE_WORKER_SPOOL must be an existing absolute folder')
    idle = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    mode = os.environ.get('GOGOGOOGLE_WORKER_TASK_MODE') or 'exact'
    roots = os.environ.get('GOGOGOOGLE_WORKER_ROOTS') or 'none'
    print(f'INBOX task mode: {mode}; worker roots: {roots}; do: off (review only)', flush=True)
    print(f'INBOX READY for one request, waiting up to {idle} seconds', flush=True)
    served = serve_one(spool, idle, os.environ.get('GOGOGOOGLE_WORKER_APPROVED_TASK', ''), mode=mode)
    print('INBOX DONE: one request answered' if served else 'INBOX DONE: no request arrived', flush=True)


if __name__ == '__main__':
    main()
