import importlib
import importlib.util
import json
import unittest
from pathlib import Path

WORKERS = {"worker-one": {"host": "gg-worker", "launcher": "C:/work/launch.cmd", "workdir": "C:/work/fixture"}}


class Backend:
    def __init__(self):
        self.calls = []

    def delegate(self, args):
        self.calls.append(("delegate", args))
        return "delegated"

    def remote_review(self, args):
        self.calls.append(("remote_review", args))
        return '{"status": "OK"}'


class RemoteAdapterTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("gogogoogle_mcp")
        self.backend = Backend()

    def adapter(self, workers=WORKERS):
        return self.module.Adapter(self.backend, trusted=True, host="local", allowed_roots=[str(Path.cwd())],
                                   remote_workers=workers)

    def test_worker_without_configuration_is_refused(self):
        for workers in (None, {}):
            result = self.module.invoke(self.adapter(workers).prepare,
                                        {"task": "review it", "workdir": "", "role": "review", "worker": "worker-one"})
            self.assertEqual(result, {"error": "worker is not configured by the operator"})
        self.assertEqual(self.backend.calls, [])

    def test_unknown_worker_and_other_roles_are_refused(self):
        adapter = self.adapter()
        self.assertEqual(self.module.invoke(adapter.prepare, {"task": "t", "workdir": "", "role": "review", "worker": "nobody"}),
                         {"error": "worker is not configured by the operator"})
        for role in ("do", "research"):
            self.assertEqual(self.module.invoke(adapter.prepare, {"task": "t", "workdir": "", "role": role, "worker": "worker-one"}),
                             {"error": "a remote worker runs review only"})
        self.assertIn("error", self.module.invoke(adapter.prepare,
                                                  {"task": "x" * 1001, "workdir": "", "role": "review", "worker": "worker-one"}))

    def test_prepared_summary_names_the_worker_and_remote_folder(self):
        prepared = self.adapter().prepare({"task": " review calc.py ", "workdir": "C:/elsewhere", "role": "review",
                                           "worker": "worker-one", "minutes": 30, "verify": True})
        request = prepared["request"]
        self.assertEqual((request["worker"], request["remote_host"], request["remote_workdir"]),
                         ("worker-one", "gg-worker", "C:/work/fixture"))
        self.assertEqual((request["task"], request["role"], request["minutes"], request["verify"]),
                         ("review calc.py", "review", 1, False))
        self.assertNotIn("workdir", request)
        self.assertIn("not a sandbox", prepared["notice"])
        self.assertIn("Google", prepared["notice"])

    def test_delegate_calls_the_remote_function_once(self):
        adapter = self.adapter()
        token = adapter.prepare({"task": "review calc.py", "workdir": "", "role": "review", "worker": "worker-one"})["token"]
        self.assertEqual(adapter.delegate(token), '{"status": "OK"}')
        self.assertEqual(self.backend.calls, [("remote_review", {"task": "review calc.py", "worker": "worker-one"})])
        self.assertIn("error", self.module.invoke(adapter.delegate, token))  # single use
        self.assertEqual(len(self.backend.calls), 1)

    def test_local_requests_are_unchanged(self):
        adapter = self.adapter()
        self.assertEqual(self.module.invoke(adapter.prepare, {"task": "t", "workdir": "", "role": "review"}),
                         {"error": "task and workdir are required strings"})
        prepared = adapter.prepare({"task": "t", "workdir": ".", "role": "review"})
        self.assertNotIn("worker", prepared["request"])

    def test_configure_remote_loads_workers_and_transport_into_the_backend(self):
        spec = importlib.util.spec_from_file_location("gogogoogle_backend_test", Path(__file__).with_name("__init__.py"))
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        self.assertEqual(self.module.configure_remote(backend, ""), {})
        self.assertIsNone(backend._remote_transport)
        workers = self.module.configure_remote(backend, json.dumps(WORKERS))
        self.assertEqual(workers, WORKERS)
        self.assertEqual(backend.REMOTE_WORKERS, WORKERS)
        self.assertTrue(callable(backend._remote_transport.invoke))
        for bad in ("not json", '["list"]', json.dumps({"Bad Name": WORKERS["worker-one"]})):
            with self.assertRaises(ValueError):
                self.module.configure_remote(backend, bad)


if __name__ == "__main__":
    unittest.main()
