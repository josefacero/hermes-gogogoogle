"""Experimental stdio MCP prototype; do not deploy as a supported interface.

Set GOGOGOOGLE_MCP_TRUSTED=1 and GOGOGOOGLE_MCP_HOST to an identifying label
and GOGOGOOGLE_MCP_ALLOWED_ROOTS to a JSON array of existing absolute local
directories. Set GOGOGOOGLE_MCP_ALLOW_DO=1 separately to enable do mode.
These settings grant all clients access; they are not human approval or a sandbox.
The optional MCP Python SDK is required only to start the server.
"""

import importlib.util
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path


class Adapter:
    def __init__(self, backend, *, trusted=False, host=None, allowed_roots=None, allow_do=False):
        self.backend = backend
        self.trusted = trusted is True and isinstance(host, str) and bool(host.strip())
        self.host = host
        if not isinstance(allowed_roots, list) or not allowed_roots:
            raise ValueError("at least one allowed workspace root is required")
        self.roots = []
        for root in allowed_roots:
            if not isinstance(root, str) or not root.strip() or not Path(root).is_absolute():
                raise ValueError("workspace roots must be absolute directory paths")
            try:
                path = Path(root).resolve(strict=True)
                if not path.is_dir():
                    raise ValueError("workspace root is not a directory")
            except (OSError, RuntimeError) as exc:
                raise ValueError("workspace root is unavailable") from exc
            self.roots.append(path)
        self.allow_do = allow_do is True
        self._pending = {}
        self._lock = threading.Lock()

    def _check(self):
        if not self.trusted:
            raise PermissionError("MCP server is not explicitly trusted and configured with a host label")

    def _workspace(self, value):
        try:
            workspace = Path(value).expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise PermissionError("workspace is not a directory")
            for root in self.roots:
                if root.resolve(strict=True) == root and root.is_dir() and workspace.is_relative_to(root):
                    return workspace
        except (OSError, RuntimeError) as exc:
            raise PermissionError("workspace or root is unavailable") from exc
        raise PermissionError("workspace is outside configured roots")

    def prepare(self, args):
        self._check()
        if not isinstance(args, dict) or set(args) - {"task", "workdir", "role", "minutes", "verify", "background", "resume"}:
            raise ValueError("invalid request fields")
        if not isinstance(args.get("task"), str) or not args["task"].strip() or not isinstance(args.get("workdir"), str) or not args["workdir"].strip():
            raise ValueError("task and workdir are required strings")
        role = args.get("role", "do")
        minutes = args.get("minutes", 10)
        verify = args.get("verify", True)
        background = args.get("background", False)
        resume = args.get("resume", "")
        if (role not in ("do", "research", "review") or type(minutes) is not int or not 1 <= minutes <= 30
                or type(verify) is not bool or type(background) is not bool or not isinstance(resume, str)
                or (resume and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", resume))):
            raise ValueError("invalid delegation options")
        if role == "do" and not self.allow_do:
            raise PermissionError("do mode is not enabled by the operator")
        workspace = self._workspace(args["workdir"])
        request = {"task": args["task"].strip(), "workdir": str(workspace), "role": role,
                   "minutes": minutes, "verify": verify, "background": background, "resume": resume}
        now = time.monotonic()
        with self._lock:
            self._pending = {key: value for key, value in self._pending.items() if value[0] > now}
            if len(self._pending) >= 100:
                raise ValueError("too many pending requests")
            token = secrets.token_urlsafe(32)
            self._pending[token] = (now + 120, request)
        return {"request": request.copy(), "host_label": self.host, "skip_permissions": role == "do",
                "expires_in_seconds": 120, "token": token,
                "notice": "Local execution under trusted configuration; this is not human approval."}

    def delegate(self, token):
        self._check()
        if not isinstance(token, str):
            raise ValueError("invalid token")
        with self._lock:
            entry = self._pending.pop(token, None)
        if entry is None or entry[0] <= time.monotonic():
            raise ValueError("invalid, expired, or already used token")
        if self._workspace(entry[1]["workdir"]) != Path(entry[1]["workdir"]):
            raise PermissionError("prepared workspace changed")
        return self.backend.delegate(entry[1])

    def job(self, args):
        self._check()
        return self.backend.job(args)

    def stats(self, args):
        self._check()
        return self.backend.stats(args)


def main():
    trusted = os.environ.get("GOGOGOOGLE_MCP_TRUSTED") == "1"
    host = os.environ.get("GOGOGOOGLE_MCP_HOST")
    if not trusted or not host or not host.strip():
        raise SystemExit("MCP server requires explicit trusted configuration and a host label")
    try:
        roots = json.loads(os.environ.get("GOGOGOOGLE_MCP_ALLOWED_ROOTS", ""))
        adapter = Adapter(None, trusted=trusted, host=host, allowed_roots=roots,
                          allow_do=os.environ.get("GOGOGOOGLE_MCP_ALLOW_DO") == "1")
    except (ValueError, TypeError):
        raise SystemExit("MCP server requires valid existing absolute workspace roots") from None
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        from mcp.server.mcpserver import MCPServer as FastMCP

    spec = importlib.util.spec_from_file_location("gogogoogle_backend", Path(__file__).with_name("__init__.py"))
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    backend.BROWSER_MODE = "off"
    adapter.backend = backend
    server = FastMCP("gogogoogle")

    def invoke(method, args):
        try:
            return method(args)
        except Exception:
            return {"error": "Request failed; inspect the backend separately."}

    @server.tool()
    def prepare(task: str, workdir: str, role: str = "do", minutes: int = 10,
                verify: bool = True, background: bool = False, resume: str = "") -> dict:
        """Show a local request and issue a single-use token; this is not human approval."""
        return invoke(adapter.prepare, {"task": task, "workdir": workdir, "role": role, "minutes": minutes,
                                        "verify": verify, "background": background, "resume": resume})

    @server.tool()
    def delegate(token: str) -> str | dict:
        """Execute the unchanged prepared request using its single-use token."""
        return invoke(adapter.delegate, token)

    @server.tool()
    def job(id: str) -> str | dict:
        """Get the status or result of a background job."""
        return invoke(adapter.job, {"id": id})

    @server.tool()
    def stats() -> str | dict:
        """Get aggregate delegation statistics."""
        return invoke(adapter.stats, {})

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
