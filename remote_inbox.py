#!/usr/bin/env python3
"""Experimental desktop-session worker inbox: serves ONE relayed request under the supervisor, then exits.

The operator starts it by hand in the signed-in Windows session, where agy can read its sign-in.
Policy comes from this process's environment (approved task, worker roots), never from the request.
"""
import json
import os
from pathlib import Path
import sys
import threading
import time

import remote_supervisor

LIMIT = 1024 * 1024


def serve_one(spool, idle_seconds, approved, run=remote_supervisor.run_supervised):
    """Wait for one request, answer it, return True. False when none arrived in time."""
    ready, request, response, cancel = (spool / name for name in ('ready', 'request.json', 'response.json', 'cancel'))
    for stale in (request, response, cancel):
        stale.unlink(missing_ok=True)
    ready.touch()
    try:
        until = time.monotonic() + idle_seconds
        while not request.is_file():
            if time.monotonic() >= until:
                return False
            time.sleep(0.1)
    finally:
        ready.unlink(missing_ok=True)  # one request only: a second relay call is refused
    try:
        if request.stat().st_size > LIMIT:
            raise ValueError('request too large')
        data = request.read_bytes()
        request.unlink()
        parsed = json.loads(data)
        if not approved or not isinstance(parsed, dict) or parsed.get('task') != approved:
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
                result = run(parsed, remote_supervisor.WORKER_DEADLINE_SECONDS, disconnected)
            finally:
                finished.set()
    except (ValueError, TypeError, RecursionError, OSError):
        result = {'error': 'invalid request'}
    pending = spool / 'response.tmp'
    pending.write_text(json.dumps(result, ensure_ascii=True), encoding='ascii')
    os.replace(pending, response)
    return True


def main():
    spool = Path(os.environ.get('GOGOGOOGLE_WORKER_SPOOL', ''))
    if not spool.is_absolute() or not spool.is_dir():
        raise SystemExit('GOGOGOOGLE_WORKER_SPOOL must be an existing absolute folder')
    idle = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    print(f'INBOX READY for one request, waiting up to {idle} seconds', flush=True)
    served = serve_one(spool, idle, os.environ.get('GOGOGOOGLE_WORKER_APPROVED_TASK', ''))
    print('INBOX DONE: one request answered' if served else 'INBOX DONE: no request arrived', flush=True)


if __name__ == '__main__':
    main()
