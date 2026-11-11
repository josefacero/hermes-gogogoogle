"""Experimental SSH JSON transport for the remote review tool; loaded only when a remote worker is configured."""

import json
import math
import os
import re
import subprocess
import threading
import time

_HOST = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.-]*@)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
_PATH = re.compile(r"(?:[A-Za-z]:)?/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\Z")
_STDOUT_LIMIT = 1024 * 1024
_STDERR_LIMIT = 64 * 1024
_REQUEST_LIMIT = 1024 * 1024


def _ssh_client():
    """Windows OpenSSH when present: an ssh found earlier on PATH (Hermes bundles Git's) cannot use the Windows ssh-agent."""
    if os.name == "nt":
        system = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "OpenSSH", "ssh.exe")
        if os.path.isfile(system):
            return system
    return "ssh"


def invoke(host, runner_path, request, timeout):
    """Return a JSON object; post-start failures have an unknown outcome."""
    if (not isinstance(host, str) or len(host) > 255 or not _HOST.fullmatch(host)
            or any(not part or len(part) > 63 or part.startswith('-') or part.endswith('-')
                   for part in host.split('@')[-1].split('.'))):
        return {"error": "invalid host"}
    if (not isinstance(runner_path, str) or not _PATH.fullmatch(runner_path)
            or any(part in ('.', '..') for part in runner_path.split('/'))):
        return {"error": "invalid runner path"}
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        return {"error": "invalid timeout"}
    if not isinstance(request, dict):
        return {"error": "invalid request"}
    try:
        payload = (json.dumps(request, allow_nan=False, ensure_ascii=True) + "\n").encode("ascii")
    except (ValueError, TypeError, RecursionError):
        return {"error": "invalid request"}
    if len(payload) > _REQUEST_LIMIT:
        return {"error": "request too large"}

    cmd = [_ssh_client(), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "ClearAllForwardings=yes", host, runner_path]
    try:
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        return {"error": "unable to start ssh"}

    def unknown(reason):
        return {"status": "UNKNOWN", "error": reason,
                "note": "Work may still run or have completed; do not automatically retry."}

    outputs = [bytearray(), bytearray()]
    overflow = threading.Event()
    io_error = threading.Event()

    def read_stream(stream, output, limit):
        try:
            while True:
                chunk = stream.read(min(65536, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    overflow.set()
                    break
        except (OSError, ValueError):
            io_error.set()
        finally:
            stream.close()

    def write_request():
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (OSError, ValueError):
            io_error.set()

    threads = [threading.Thread(target=write_request, daemon=True),
               threading.Thread(target=read_stream, args=(process.stdout, outputs[0], _STDOUT_LIMIT), daemon=True),
               threading.Thread(target=read_stream, args=(process.stderr, outputs[1], _STDERR_LIMIT), daemon=True)]
    deadline = time.monotonic() + timeout
    try:
        for thread in threads:
            thread.start()
        while any(thread.is_alive() for thread in threads) or process.poll() is None:
            if overflow.is_set():
                return unknown("output limit exceeded")
            if time.monotonic() >= deadline:
                return unknown("ssh timed out")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        if overflow.is_set():
            return unknown("output limit exceeded")
        if io_error.is_set() or process.returncode != 0:
            return unknown("ssh failed")
        try:
            result = json.loads(bytes(outputs[0]).decode("utf-8"),
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return unknown("invalid response")
        return result if isinstance(result, dict) else unknown("invalid response")
    except Exception:
        return unknown("ssh processing failed")
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait()
            if not process.stdin.closed:
                process.stdin.close()
        except OSError:
            return unknown("ssh cleanup failed")
