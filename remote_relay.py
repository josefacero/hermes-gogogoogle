#!/usr/bin/env python3
"""Experimental SSH-side relay: hands one request to a worker inbox in the signed-in desktop session.

A key-authenticated Windows SSH session cannot read Credential Manager, where agy keeps its sign-in,
so agy cannot run here. This side only passes bytes across; policy, deadline and cleanup stay in the inbox.
"""
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time

LIMIT = 1024 * 1024
WAIT_SECONDS = 180  # the worker deadline (150) plus its cleanup margin
UNKNOWN_NOTE = 'Work may still run or have completed; do not automatically retry.'


def relay(data, spool, disconnected, wait_seconds=WAIT_SECONDS):
    """Queue one request and wait for its answer. None means the client went away."""
    request_id = secrets.token_hex(8)  # only an answer carrying this id is ours
    ready, claimed, request, response, cancel = (spool / name for name in (
        'ready', f'claimed.{request_id}', f'request.{request_id}.json',
        f'response.{request_id}.json', f'cancel.{request_id}'))
    pending = spool / f'request.{request_id}.tmp'
    try:
        pending.write_bytes(data)  # written before the claim, so the inbox never waits on a slow write
        os.rename(ready, claimed)  # atomic: one relay wins, the others find no `ready`
    except OSError:
        pending.unlink(missing_ok=True)
        busy = any(spool.glob('claimed.*'))
        return {'error': 'worker inbox is busy' if busy else 'worker inbox is not running'}
    try:
        os.rename(pending, request)
    except OSError:
        pending.unlink(missing_ok=True)
        return {'status': 'UNKNOWN', 'error': 'relay failed', 'note': UNKNOWN_NOTE}
    until = time.monotonic() + wait_seconds
    while time.monotonic() < until and not disconnected.is_set():
        if response.is_file():
            try:
                if response.stat().st_size > LIMIT:
                    raise ValueError('response too large')
                result = json.loads(response.read_bytes())
                response.unlink()
            except (OSError, ValueError):
                return {'status': 'UNKNOWN', 'error': 'invalid worker response', 'note': UNKNOWN_NOTE}
            return result if isinstance(result, dict) else {'status': 'UNKNOWN', 'error': 'invalid worker response'}
        time.sleep(0.1)
    try:
        cancel.touch()
        request.unlink(missing_ok=True)  # still there means the inbox never took it
    except OSError:
        pass
    if disconnected.is_set():
        return None
    return {'status': 'UNKNOWN', 'error': 'worker inbox did not answer', 'note': UNKNOWN_NOTE}


def main():
    try:
        data = sys.stdin.buffer.readline(LIMIT + 1)
        spool = Path(os.environ.get('GOGOGOOGLE_WORKER_SPOOL', ''))
        if len(data) > LIMIT or not data.strip():
            result = {'error': 'invalid request'}
        elif not spool.is_absolute() or not spool.is_dir():
            result = {'error': 'worker spool is not configured'}
        else:
            disconnected = threading.Event()

            def watch_connection():
                try:
                    os.read(0, 1)  # raw read: a buffered read here breaks interpreter shutdown while input stays open
                finally:
                    disconnected.set()
            threading.Thread(target=watch_connection, daemon=True).start()
            result = relay(data, spool, disconnected)
    except OSError:
        result = {'status': 'UNKNOWN', 'error': 'relay failed', 'note': UNKNOWN_NOTE}
    if result is not None:
        sys.stdout.write(json.dumps(result, ensure_ascii=True) + '\n')


if __name__ == '__main__':
    main()
