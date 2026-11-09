import importlib
import os
import subprocess
import sys
import threading
import tempfile
import unittest
from unittest.mock import patch


class Backend:
    def __init__(self):
        self.calls = []

    def delegate(self, args):
        self.calls.append(("delegate", args))
        return "delegated"

    def job(self, args):
        self.calls.append(("job", args))
        return "job result"

    def stats(self, args):
        self.calls.append(("stats", args))
        return "stats result"


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("gogogoogle_mcp")
        self.backend = Backend()
        self.adapter = self.module.Adapter(self.backend, trusted=True, host="local label", allowed_roots=[str(self.module.Path.cwd())])
        self.request = {"task": "  read this  ", "workdir": ".", "role": "review", "verify": False}

    def test_prepare_shows_exact_request_before_execution(self):
        prepared = self.adapter.prepare(self.request)
        self.assertEqual([], self.backend.calls)
        self.assertEqual("read this", prepared["request"]["task"])
        self.assertTrue(self.module.Path(prepared["request"]["workdir"]).is_absolute())
        self.assertEqual("local label", prepared["host_label"])
        self.assertFalse(prepared["skip_permissions"])
        self.assertIn("not human approval", prepared["notice"].lower())
        self.assertEqual("delegated", self.adapter.delegate(prepared["token"]))
        self.assertEqual(prepared["request"], self.backend.calls[0][1])

    def test_single_use_expired_and_mutated_tokens_fail(self):
        with patch.object(self.module.time, "monotonic", return_value=100):
            first = self.adapter.prepare(self.request)["token"]
            expired = self.adapter.prepare(self.request)["token"]
            mutated = self.adapter.prepare(self.request)["token"]
            with self.assertRaises(ValueError):
                self.adapter.delegate(mutated + "x")
            self.adapter.delegate(first)
            with self.assertRaises(ValueError):
                self.adapter.delegate(first)
        with patch.object(self.module.time, "monotonic", return_value=1000):
            with self.assertRaises(ValueError):
                self.adapter.delegate(expired)
        self.assertEqual(1, len(self.backend.calls))

    def test_caller_cannot_mutate_prepared_request(self):
        prepared = self.adapter.prepare(self.request)
        self.request["task"] = "changed"
        prepared["request"]["task"] = "also changed"
        self.adapter.delegate(prepared["token"])
        self.assertEqual("read this", self.backend.calls[0][1]["task"])

    def test_concurrent_consumption_executes_at_most_once(self):
        token = self.adapter.prepare(self.request)["token"]
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt():
            barrier.wait()
            try:
                outcomes.append(self.adapter.delegate(token))
            except ValueError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(["delegated", "rejected"], outcomes)
        self.assertEqual(1, len(self.backend.calls))

    def test_failed_backend_consumes_token(self):
        token = self.adapter.prepare(self.request)["token"]
        with patch.object(self.backend, "delegate", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                self.adapter.delegate(token)
        with self.assertRaises(ValueError):
            self.adapter.delegate(token)

    def test_untrusted_never_reaches_backend(self):
        for trust, host in [(False, "host"), ("true", "host"), (True, ""), (True, None)]:
            adapter = self.module.Adapter(self.backend, trusted=trust, host=host, allowed_roots=[str(self.module.Path.cwd())])
            for method, args in [(adapter.prepare, self.request), (adapter.delegate, "token"),
                                 (adapter.job, {"id": "x"}), (adapter.stats, {})]:
                with self.assertRaises(PermissionError):
                    method(args)
        self.assertEqual([], self.backend.calls)

    def test_invalid_requests_do_not_prepare_or_execute(self):
        for changes in ({"role": "other"}, {"verify": "false"}, {"minutes": 0},
                        {"background": 1}, {"unexpected": "x"}, {"workdir": ""}):
            with self.assertRaises(ValueError):
                self.adapter.prepare({**self.request, **changes})
        with self.assertRaises(ValueError):
            self.adapter.delegate("missing")
        self.assertEqual([], self.backend.calls)

    def test_defaults_and_other_tools(self):
        with self.assertRaises(PermissionError):
            self.adapter.prepare({"task": "work", "workdir": "."})
        self.adapter = self.module.Adapter(self.backend, trusted=True, host="local", allowed_roots=[str(self.module.Path.cwd())], allow_do=True)
        prepared = self.adapter.prepare({"task": "work", "workdir": "."})
        self.assertEqual("do", prepared["request"]["role"])
        self.assertTrue(prepared["request"]["verify"])
        self.assertTrue(prepared["skip_permissions"])
        self.assertEqual("job result", self.adapter.job({"id": "abc"}))
        self.assertEqual("stats result", self.adapter.stats({}))

    def test_invalid_root_configuration_fails_closed(self):
        for roots in (None, [], [""], ["."], [str(self.module.Path.cwd() / "missing-root")], "not-a-list"):
            with self.subTest(roots=roots):
                with self.assertRaises((ValueError, PermissionError)):
                    adapter = self.module.Adapter(self.backend, trusted=True, host="host", allowed_roots=roots)
                    adapter.prepare(self.request)
        self.assertEqual([], self.backend.calls)

    def test_outside_root_and_prefix_sibling_denied(self):
        with tempfile.TemporaryDirectory() as temp:
            base = self.module.Path(temp)
            root, sibling = base / "root", base / "root-other"
            root.mkdir()
            sibling.mkdir()
            adapter = self.module.Adapter(self.backend, trusted=True, host="host", allowed_roots=[str(root)])
            with self.assertRaises(PermissionError):
                adapter.prepare({**self.request, "workdir": str(sibling)})
            self.assertEqual([], self.backend.calls)

    def test_symlink_escape_denied(self):
        with tempfile.TemporaryDirectory() as temp:
            base = self.module.Path(temp)
            root, outside = base / "root", base / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "link").symlink_to(outside, target_is_directory=True)
            except OSError:
                if os.name != "nt" or subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)],
                                                      capture_output=True).returncode:
                    self.skipTest("directory symlinks and junctions unavailable")
            adapter = self.module.Adapter(self.backend, trusted=True, host="host", allowed_roots=[str(root)])
            try:
                with self.assertRaises(PermissionError):
                    adapter.prepare({**self.request, "workdir": str(root / "link")})
            finally:
                if os.name == "nt":
                    os.rmdir(root / "link")
            self.assertEqual([], self.backend.calls)

    def test_startup_rejects_missing_or_invalid_roots_before_sdk_import(self):
        for roots in (None, "", "[]", "not json", '["relative"]'):
            env = {"GOGOGOOGLE_MCP_TRUSTED": "1", "GOGOGOOGLE_MCP_HOST": "host"}
            if roots is not None:
                env["GOGOGOOGLE_MCP_ALLOWED_ROOTS"] = roots
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit):
                    self.module.main()

    def test_resolved_escape_and_changed_prepared_path_denied(self):
        with tempfile.TemporaryDirectory() as temp:
            base = self.module.Path(temp)
            root, outside = base / "root", base / "outside"
            root.mkdir()
            outside.mkdir()
            child = root / "child"
            child.mkdir()
            adapter = self.module.Adapter(self.backend, trusted=True, host="host", allowed_roots=[str(root)])
            prepared = adapter.prepare({**self.request, "workdir": str(child)})
            token = prepared["token"]
            prepared_child = self.module.Path(prepared["request"]["workdir"])
            original_resolve = self.module.Path.resolve
            redirected = []

            def resolve(path, *args, **kwargs):
                if path == child or path == prepared_child:
                    redirected.append(path)
                    return original_resolve(outside, *args, **kwargs)
                return original_resolve(path, *args, **kwargs)

            with patch.object(self.module.Path, "resolve", resolve):
                with self.assertRaises(PermissionError):
                    adapter.prepare({**self.request, "workdir": str(child)})
                with self.assertRaises(PermissionError):
                    adapter.delegate(token)
            self.assertEqual([child, prepared_child], redirected)
            self.assertEqual([], self.backend.calls)

    def test_do_requires_literal_boolean_configuration(self):
        for setting in (False, "1", 1, None):
            adapter = self.module.Adapter(self.backend, trusted=True, host="host", allowed_roots=[str(self.module.Path.cwd())], allow_do=setting)
            with self.assertRaises(PermissionError):
                adapter.prepare({**self.request, "role": "do"})
        self.assertEqual([], self.backend.calls)

    def test_import_does_not_import_mcp_sdk(self):
        self.assertFalse(any(name == "mcp" or name.startswith("mcp.") for name in sys.modules))


if __name__ == "__main__":
    unittest.main()
