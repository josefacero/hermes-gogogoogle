"""Experimental Windows worker supervisor: runs one review under a deadline inside a Job Object (cleanup, not containment)."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

WORKER_DEADLINE_SECONDS = 150
VERIFY_EXTRA_SECONDS = 330  # the plugin gives each verifier model 5 minutes; plus margin


def deadline_for(request):
    """Worker-owned deadline: 150 s, or 480 s when the request asks for the verifier pass."""
    verify = isinstance(request, dict) and request.get('verify') is True
    return WORKER_DEADLINE_SECONDS + (VERIFY_EXTRA_SECONDS if verify else 0)
LIMIT = 1024 * 1024


class _BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimits), ('IoInfo', _IOCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


def _kernel32():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    api.IsProcessInJob.restype = wintypes.BOOL
    api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def _assign_job(api, job, process):
    if not api.AssignProcessToJobObject(job, int(process._handle)):
        raise ctypes.WinError(ctypes.get_last_error())
    assigned = wintypes.BOOL()
    if not api.IsProcessInJob(int(process._handle), job, ctypes.byref(assigned)) or not assigned.value:
        raise OSError('job membership could not be verified')


def _run_job(command, payload, deadline_seconds, disconnected=None):
    """Run an stdin-gated trusted child; no request is sent until assignment succeeds."""
    api = _kernel32()
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = None
    threads = []
    try:
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        _assign_job(api, job, process)
        output = bytearray()
        overflow = threading.Event()
        io_error = threading.Event()

        def read_output():
            try:
                while True:
                    chunk = process.stdout.read(min(65536, 1024 * 1024 + 1 - len(output)))
                    if not chunk:
                        return
                    output.extend(chunk)
                    if len(output) > 1024 * 1024:
                        overflow.set()
                        return
            except (OSError, ValueError):
                io_error.set()

        def write_input():
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (OSError, ValueError):
                io_error.set()

        threads = [threading.Thread(target=read_output, daemon=True),
                   threading.Thread(target=write_input, daemon=True)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + deadline_seconds
        while process.poll() is None or any(thread.is_alive() for thread in threads):
            if disconnected is not None and disconnected.is_set():
                return {'status': 'DISCONNECTED'}
            if overflow.is_set():
                return {'status': 'OUTPUT LIMIT'}
            if time.monotonic() >= deadline:
                return {'status': 'TIMED OUT'}
            time.sleep(0.01)
        if overflow.is_set():
            return {'status': 'OUTPUT LIMIT'}
        if io_error.is_set():
            return {'status': 'IO ERROR'}
        return {'status': 'DONE', 'returncode': process.returncode, 'output': bytes(output)}
    finally:
        terminated = api.TerminateJobObject(job, 1)
        terminate_error = ctypes.get_last_error() if not terminated else 0
        closed = api.CloseHandle(job)
        close_error = ctypes.get_last_error() if not closed else 0
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            process.wait(timeout=10)
            for thread in threads:
                thread.join(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed:
                    stream.close()
        if not closed:
            raise ctypes.WinError(close_error)
        if not terminated:
            raise ctypes.WinError(terminate_error)


def allowed_request(request, deadline_seconds):
    if (not isinstance(request, dict) or type(deadline_seconds) is not int
            or set(request) - {'task', 'workdir', 'role', 'minutes', 'verify', 'background'}):
        return False
    minutes = request.get('minutes')
    verify = request.get('verify')
    return (request.get('role') == 'review' and type(verify) is bool
            and request.get('background', False) is False and 'resume' not in request
            and type(minutes) is int and minutes >= 1
            and minutes * 60 + 60 + (300 if verify else 0) <= deadline_seconds)


def run_supervised(request, deadline_seconds, disconnected=None):
    if type(deadline_seconds) is not int or deadline_seconds <= 0:
        raise ValueError('deadline must be a positive integer')
    if not allowed_request(request, deadline_seconds):
        return {'error': 'request refused by worker policy'}
    try:
        if sys.prefix != sys.base_prefix or not os.path.samefile(sys.executable, sys._base_executable):
            return {'error': 'base Python interpreter required'}
        payload = json.dumps(request, allow_nan=False, ensure_ascii=True).encode('ascii')
        if len(payload) > 1024 * 1024:
            return {'error': 'request too large'}
        result = _run_job([sys.executable, str(Path(__file__).with_name('remote_runner.py'))],
                          payload, deadline_seconds, disconnected)
        if result['status'] == 'TIMED OUT':
            return {'status': 'TIMED OUT', 'error': 'worker deadline exceeded',
                    'note': 'Stopped at the worker deadline; part of the work may have run. Do not automatically retry.'}
        if result['status'] == 'DISCONNECTED':
            return {'status': 'UNKNOWN', 'error': 'connection lost'}
        if result['status'] != 'DONE':
            return {'status': 'UNKNOWN', 'error': 'worker output or communication failed'}
        if result['returncode'] != 0 or len(result['output']) > 1024 * 1024:
            return {'status': 'UNKNOWN', 'error': 'worker failed or output exceeded limit'}
        response = json.loads(result['output'])
        return response if isinstance(response, dict) else {'status': 'UNKNOWN', 'error': 'invalid response'}
    except (OSError, ValueError, TypeError, RecursionError, subprocess.TimeoutExpired):
        return {'status': 'UNKNOWN', 'error': 'worker supervision failed'}


def main():
    try:
        data = sys.stdin.buffer.readline(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('request too large')
        request = json.loads(data)
        approved = os.environ.get('GOGOGOOGLE_WORKER_APPROVED_TASK', '')
        if not approved or not isinstance(request, dict) or request.get('task') != approved:
            result = {'error': 'request refused by worker policy'}
        else:
            disconnected = threading.Event()
            def watch_connection():
                try:
                    os.read(0, 1)
                finally:
                    disconnected.set()
            threading.Thread(target=watch_connection, daemon=True).start()
            result = run_supervised(request, deadline_for(request), disconnected)
    except (ValueError, TypeError, RecursionError):
        result = {'error': 'invalid request'}
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + '\n')


if __name__ == '__main__':
    main()
