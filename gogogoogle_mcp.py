"""Experimental stdio MCP server for Claude Code and Codex; a preview, tested on Windows only.

Set GOGOGOOGLE_MCP_TRUSTED=1 and GOGOGOOGLE_MCP_HOST to an identifying label
and GOGOGOOGLE_MCP_ALLOWED_ROOTS to a JSON array (or an os.pathsep list) of existing
absolute local directories. Set GOGOGOOGLE_MCP_ALLOW_DO=1 separately to enable do mode.
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


CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: a calling agent may retype the code
CODE_LENGTH = 12
CODE_SECONDS = 300


class Refused(ValueError):
    """The adapter itself refused a request. Its text is a fixed message, safe to show the calling agent."""


class Denied(PermissionError):
    """The adapter's policy refused a request. Fixed message, safe to show the calling agent."""


def parse_roots(value):
    """A JSON array of folders, or an os.pathsep list (shells such as PowerShell 5.1 strip the JSON quotes)."""
    try:
        roots = json.loads(value or "")
    except json.JSONDecodeError:
        roots = [part.strip() for part in (value or "").split(os.pathsep) if part.strip()]
    if not isinstance(roots, list) or not roots:
        raise ValueError("workspace roots must be a non-empty list")
    return roots


def invoke(method, args):
    try:
        return method(args)
    except (Refused, Denied) as exc:  # a bad token or a refused workspace must not look like a backend failure
        return {"error": str(exc)}
    except Exception:  # anything else may carry paths or backend detail, so it stays generic
        return {"error": "Request failed; inspect the backend separately."}


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
                home = Path.home().resolve()
            except (OSError, RuntimeError) as exc:
                raise ValueError("workspace root is unavailable") from exc
            if path == Path(path.anchor) or path == home:
                raise ValueError("workspace root is a drive root or the home folder")
            self.roots.append(path)
        self.allow_do = allow_do is True
        self._pending = {}
        self._lock = threading.Lock()

    def _check(self):
        if not self.trusted:
            raise Denied("MCP server is not explicitly trusted and configured with a host label")

    def _workspace(self, value):
        try:
            workspace = Path(value).expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise Denied("workspace is not a directory")
            for root in self.roots:
                if root.resolve(strict=True) == root and root.is_dir() and workspace.is_relative_to(root):
                    return workspace
        except (OSError, RuntimeError) as exc:
            raise Denied("workspace or root is unavailable") from exc
        raise Denied("workspace is outside configured roots")

    def prepare(self, args):
        self._check()
        if not isinstance(args, dict) or set(args) - {"task", "workdir", "role", "minutes", "verify", "background", "resume"}:
            raise Refused("invalid request fields")
        if not isinstance(args.get("task"), str) or not args["task"].strip() or not isinstance(args.get("workdir"), str) or not args["workdir"].strip():
            raise Refused("task and workdir are required strings")
        role = args.get("role")  # required: no silent default to the privileged mode
        minutes = args.get("minutes", 10)
        verify = args.get("verify", True)
        background = args.get("background", False)
        resume = args.get("resume", "")
        if (role not in ("do", "research", "review") or type(minutes) is not int or not 1 <= minutes <= 30
                or type(verify) is not bool or type(background) is not bool or not isinstance(resume, str)
                or (resume and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", resume))):
            raise Refused("invalid delegation options")
        if role == "do" and not self.allow_do:
            raise Denied("do mode is not enabled by the operator")
        workspace = self._workspace(args["workdir"])
        request = {"task": args["task"].strip(), "workdir": str(workspace), "role": role,
                   "minutes": minutes, "verify": verify, "background": background, "resume": resume}
        now = time.monotonic()
        with self._lock:
            self._pending = {key: value for key, value in self._pending.items() if value[0] > now}
            if len(self._pending) >= 100:
                raise Refused("too many pending requests")
            token = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            self._pending[token] = (now + CODE_SECONDS, request)
        return {"request": request.copy(), "host_label": self.host, "skip_permissions": role == "do",
                "expires_in_seconds": CODE_SECONDS, "token": token,
                "notice": "Local execution under trusted configuration; this is not human approval."}

    def delegate(self, token):
        self._check()
        if not isinstance(token, str):
            raise Refused("invalid token")
        token = token.strip().strip("'\"").strip().lower()  # agents add quotes, newlines or capitals
        with self._lock:
            entry = self._pending.pop(token, None)
        if entry is None or entry[0] <= time.monotonic():
            raise Refused("invalid, expired, or already used token")
        if self._workspace(entry[1]["workdir"]) != Path(entry[1]["workdir"]):
            raise Denied("prepared workspace changed")
        result = self.backend.delegate(entry[1])
        # the shared engine names the Hermes tool; on this server the tool is `job`
        return result.replace("antigravity_job", "the job tool") if isinstance(result, str) else result

    def job(self, args):
        self._check()
        return self.backend.job(args)

    def stats(self, args):
        self._check()
        return self.backend.stats(args)


def main():
    trusted = os.environ.get("GOGOGOOGLE_MCP_TRUSTED") == "1"
    host = os.environ.get("GOGOGOOGLE_MCP_HOST")
    if not trusted:
        raise SystemExit("gogogoogle MCP server not started: set GOGOGOOGLE_MCP_TRUSTED=1 to accept that every "
                         "client of this server may delegate within your policy (the server must be explicitly trusted).")
    if not host or not host.strip():
        raise SystemExit("gogogoogle MCP server not started: set GOGOGOOGLE_MCP_HOST to a label for this machine, such as my-laptop.")
    try:
        adapter = Adapter(None, trusted=trusted, host=host, allowed_roots=parse_roots(os.environ.get("GOGOGOOGLE_MCP_ALLOWED_ROOTS")),
                          allow_do=os.environ.get("GOGOGOOGLE_MCP_ALLOW_DO") == "1")
    except (ValueError, TypeError) as exc:  # Adapter's messages are fixed text and carry no path
        raise SystemExit(f"gogogoogle MCP server not started: GOGOGOOGLE_MCP_ALLOWED_ROOTS is not usable ({exc}). Set it to existing "
                         f"absolute project folders, as a JSON array or separated by '{os.pathsep}'. "
                         "A drive root or your home folder is refused.") from None
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        try:
            from mcp.server.mcpserver import MCPServer as FastMCP
        except ImportError:
            raise SystemExit("gogogoogle MCP server not started: the optional 'mcp' package is missing. "
                             "Install it for the interpreter that runs this file: <python> -m pip install mcp") from None

    spec = importlib.util.spec_from_file_location("gogogoogle_backend", Path(__file__).with_name("__init__.py"))
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    backend.BROWSER_MODE = "off"
    adapter.backend = backend
    server = FastMCP("gogogoogle")

    @server.tool()
    def prepare(task: str, workdir: str, role: str, minutes: int = 10,
                verify: bool = True, background: bool = False, resume: str = "") -> dict:
        """Show a local request and issue a single-use code; this is not human approval. role: do, research or review."""
        return invoke(adapter.prepare, {"task": task, "workdir": workdir, "role": role, "minutes": minutes,
                                        "verify": verify, "background": background, "resume": resume})

    @server.tool()
    def delegate(token: str) -> str | dict:
        """Execute the unchanged prepared request. Pass the token from the prepare result as is."""
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
