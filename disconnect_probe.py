"""Disposable SSH disconnect probe; runs no agy or project commands."""
import json
import os
from pathlib import Path
import sys
import threading
import time

import remote_supervisor

marker = Path(__file__).with_name('disconnect-probe-pids.json')
if marker.exists():
    raise SystemExit('probe marker already exists')
sys.stdin.buffer.readline(1024)
disconnected = threading.Event()
def watch_connection():
    try:
        os.read(0, 1)
    finally:
        disconnected.set()
threading.Thread(target=watch_connection, daemon=True).start()
child = ('import sys,subprocess,time,os,pathlib,json; sys.stdin.buffer.read(); '
         'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); '
         'pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)')
outcome = []
thread = threading.Thread(target=lambda: outcome.append(remote_supervisor._run_job(
    [sys.executable, '-c', child, str(marker)], b'go', 20, disconnected)))
thread.start()
until = time.monotonic() + 8
while not marker.exists() and thread.is_alive() and time.monotonic() < until:
    time.sleep(0.05)
if not marker.exists():
    print('PROBE FAILED', flush=True)
else:
    print('PROBE READY', flush=True)
thread.join()
print(json.dumps({'probe_result': outcome[0]['status']}), flush=True)
