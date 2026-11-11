"""Observe probe processes by handle; no arbitrary process operations."""
import ctypes
import json
from pathlib import Path
import sys
import time

marker = Path(sys.argv[1])
until = time.monotonic() + 180
while not marker.exists() and time.monotonic() < until:
    time.sleep(0.05)
if not marker.exists():
    raise SystemExit('No probe marker appeared')
pids = json.loads(marker.read_text())
api = ctypes.WinDLL('kernel32', use_last_error=True)
api.OpenProcess.restype = ctypes.c_void_p
api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
api.CloseHandle.argtypes = [ctypes.c_void_p]
handles = []
for pid in pids:
    handle = api.OpenProcess(0x100000, False, pid)
    if not handle:
        print(f'Process handle unavailable; Windows error {ctypes.get_last_error()}', flush=True)
    handles.append(handle)
if not all(handles):
    for handle in handles:
        if handle:
            api.CloseHandle(handle)
    raise SystemExit('Could not hold handles to both probe processes')
print('MONITOR READY', flush=True)
start = time.monotonic()
try:
    while time.monotonic() - start < 30:
        if all(api.WaitForSingleObject(handle, 0) == 0 for handle in handles):
            print(f'BOTH EXITED after {time.monotonic() - start:.1f} seconds', flush=True)
            break
        time.sleep(0.1)
    else:
        print('PROBE PROCESSES STILL RUNNING', flush=True)
finally:
    for handle in handles:
        api.CloseHandle(handle)
