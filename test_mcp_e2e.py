"""End to end: a real MCP client talks stdio to gogogoogle_mcp.py, which runs a fake `agy` (no quota, no Google account).

Skipped when the optional `mcp` package is missing. Proves the wiring and the operator policy, not the quality of agy's work.
"""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

# The SDK is imported inside _session, not here: test_mcp_adapter asserts the plugin never imports it in-process.
HAVE_MCP = importlib.util.find_spec("mcp") is not None

HERE = Path(__file__).resolve().parent
FAKE = '''import json, sys
from pathlib import Path
log = Path(__file__).with_name("argv.log")
with log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:2] == ["mcp"]:
    print("NAME"); sys.exit(0)
sys.stdin.readline()
print(json.dumps({"event": "result", "conversation_id": "fake-1", "result": {
    "status": "SUCCESS", "response": "FAKE AGY REPLY", "conversation_id": "fake-1",
    "usage": {"input_tokens": 10, "output_tokens": 2}}}))
'''


def _make_fake_agy(folder):
    (folder / "fake_agy.py").write_text(FAKE, encoding="utf-8")
    if os.name == "nt":
        (folder / "agy.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{folder / "fake_agy.py"}" %*\r\n', encoding="utf-8")
    else:
        shim = folder / "agy"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{folder / "fake_agy.py"}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)


def _make_repo(folder):
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=folder, check=True)
    (folder / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=folder, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=folder, check=True)


def _payload(result):
    data = result.structured_content or json.loads(result.content[0].text)
    inner = data.get("result", data)
    return json.loads(inner) if isinstance(inner, str) else inner


@unittest.skipUnless(HAVE_MCP, "optional `mcp` package not installed")
class McpEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.work = base / "work"
        self.work.mkdir()
        self.home = base / "home"
        _make_fake_agy(self.bin)
        _make_repo(self.work)

    def tearDown(self):
        self._tmp.cleanup()

    @asynccontextmanager
    async def _session(self, allow_do=False, roots=None):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env =dict(os.environ, GOGOGOOGLE_MCP_TRUSTED="1", GOGOGOOGLE_MCP_HOST="test-host", HERMES_HOME=str(self.home),
                   GOGOGOOGLE_MCP_ALLOWED_ROOTS=json.dumps(roots or [str(self.work)]),
                   PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        if allow_do:
            env["GOGOGOOGLE_MCP_ALLOW_DO"] = "1"
        params = StdioServerParameters(command=sys.executable, args=[str(HERE / "gogogoogle_mcp.py")], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                yield s

    def _argv_log(self):
        log = self.bin / "argv.log"
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def test_lists_exactly_the_four_tools(self):
        async def go():
            async with self._session() as s:
                return sorted(t.name for t in (await s.list_tools()).tools)
        self.assertEqual(asyncio.run(go()), ["delegate", "job", "prepare", "stats"])

    def test_review_runs_without_skipping_permissions_and_token_is_single_use(self):
        async def go():
            async with self._session() as s:
                prep = _payload(await s.call_tool("prepare", {"task": "look", "workdir": str(self.work), "role": "review", "verify": False}))
                first = await s.call_tool("delegate", {"token": prep["token"]})
                again = await s.call_tool("delegate", {"token": prep["token"]})
                return prep, _payload(first), _payload(again)
        prep, first, again = asyncio.run(go())
        self.assertFalse(prep["skip_permissions"])
        self.assertEqual(first["status"], "OK")
        self.assertIn("FAKE AGY REPLY", first["result"])
        self.assertEqual(first["git"]["changed"], False)
        self.assertIn("error", again)
        self.assertTrue(self._argv_log())
        self.assertNotIn("--dangerously-skip-permissions", sum(self._argv_log(), []))

    def test_do_is_refused_unless_the_operator_enables_it(self):
        async def go():
            async with self._session() as s:
                return _payload(await s.call_tool("prepare", {"task": "x", "workdir": str(self.work), "role": "do"}))
        self.assertIn("error", asyncio.run(go()))
        self.assertEqual(self._argv_log(), [])

    def test_do_skips_agy_permissions_only_when_enabled(self):
        async def go():
            async with self._session(allow_do=True) as s:
                prep = _payload(await s.call_tool("prepare", {"task": "x", "workdir": str(self.work), "role": "do", "verify": False}))
                res = _payload(await s.call_tool("delegate", {"token": prep["token"]}))
                return prep, res
        prep, res = asyncio.run(go())
        self.assertTrue(prep["skip_permissions"])
        self.assertEqual(res["status"], "OK")
        self.assertIn("--dangerously-skip-permissions", sum(self._argv_log(), []))

    def test_workspace_outside_the_roots_never_reaches_agy(self):
        async def go():
            async with self._session() as s:
                return _payload(await s.call_tool("prepare", {"task": "x", "workdir": tempfile.gettempdir(), "role": "review"}))
        self.assertIn("error", asyncio.run(go()))
        self.assertEqual(self._argv_log(), [])

    def test_background_returns_a_job_that_finishes(self):
        async def go():
            async with self._session() as s:
                prep = _payload(await s.call_tool("prepare", {"task": "look", "workdir": str(self.work), "role": "review",
                                                              "verify": False, "background": True}))
                started = _payload(await s.call_tool("delegate", {"token": prep["token"]}))
                for _ in range(100):  # the fake agy answers at once; 20 s is a generous cap
                    record = _payload(await s.call_tool("job", {"id": started["job"]}))
                    if record.get("status") != "RUNNING":
                        break
                    await asyncio.sleep(0.2)
                return started, record
        started, record = asyncio.run(go())
        self.assertRegex(started["job"], r"^[0-9a-f]{12}$")
        self.assertEqual(started["status"], "RUNNING")
        self.assertEqual(started["note"], "Poll with the job tool.")  # the real engine's note, renamed for this server
        self.assertEqual(record["status"], "OK")
        self.assertIn("FAKE AGY REPLY", record["result"])

    def test_job_started_by_another_server_process_reads_unknown(self):
        jobs = self.home / "plugin-data" / "gogogoogle" / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "0123456789ab.json").write_text(json.dumps({"status": "RUNNING", "process": "another", "phase": "WORKER"}), encoding="utf-8")

        async def go():
            async with self._session() as s:
                return (_payload(await s.call_tool("job", {"id": "0123456789ab"})),
                        _payload(await s.call_tool("job", {"id": "../../x"})))
        lost, bad = asyncio.run(go())
        self.assertEqual(lost["status"], "UNKNOWN")
        self.assertIn("error", bad)

    def test_server_refuses_to_start_untrusted(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GOGOGOOGLE_")}
        r = subprocess.run([sys.executable, str(HERE / "gogogoogle_mcp.py")], capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("trusted", r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main()
