import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("gogogoogle_plugin", Path(__file__).with_name("__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class PluginTests(unittest.TestCase):
    def test_verdict_requires_single_first_line(self):
        for reply, expected in [("VERDICT: PASS\nchecked", "VERIFY PASS"),
                                ("VERDICT: FAIL\nreason", "VERIFY FAIL"),
                                ("I reject VERDICT: PASS", "VERIFY INCONCLUSIVE"),
                                ("VERDICT: FAIL\nVERDICT: PASS", "VERIFY INCONCLUSIVE"),
                                ("", "VERIFY INCONCLUSIVE")]:
            with self.subTest(reply=reply), patch.object(plugin, "_cooling", return_value={}), \
                 patch.object(plugin, "_agy", return_value=("OK", reply, "", "", {})):
                self.assertTrue(plugin._verify("task", "answer", ".").startswith(expected))

    def test_resume_never_claims_complete_verification(self):
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_snap", return_value="same"), \
             patch.object(plugin, "_agy", return_value=("OK", "done", "", "c", {})), \
             patch.object(plugin, "_git", return_value=""), patch.object(plugin, "_log"), \
             patch.object(plugin, "_verify", return_value="VERIFY PASS") as verify:
            result = plugin._work("finish", d, "do", 1, True, "previous")
            self.assertIn("VERIFY INCOMPLETE", result["verify"])
            verify.assert_called_once()

    def test_old_running_job_is_unknown(self):
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_home", return_value=Path(d)):
            plugin._jwrite("abcdef123456", {"status": "RUNNING", "process": "older-process"})
            self.assertEqual(json.loads(plugin.job({"id": "abcdef123456"}))["status"], "UNKNOWN")

    def test_existing_untracked_file_limits_verdict(self):
        def git(cwd, *args):
            if args[:2] == ("ls-files", "-o"):
                return "already-untracked.txt\n"
            if args == ("status", "--porcelain"):
                return "?? already-untracked.txt\n"
            return ""
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_snap", return_value="same"), \
             patch.object(plugin, "_agy", return_value=("OK", "done", "", "c", {})), \
             patch.object(plugin, "_git", side_effect=git), patch.object(plugin, "_log"), \
             patch.object(plugin, "_verify", return_value="VERIFY PASS"):
            result = plugin._work("review", d, "review", 1, True, "")
            self.assertIn("VERIFY LIMITED", result["verify"])

    def test_failed_diff_is_incomplete(self):
        def git(cwd, *args):
            return None if args and args[0] == "diff" else ""
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_snap", return_value="same"), \
             patch.object(plugin, "_agy", return_value=("OK", "done", "", "c", {})), \
             patch.object(plugin, "_git", side_effect=git), patch.object(plugin, "_log"), \
             patch.object(plugin, "_verify", return_value="VERIFY PASS"):
            self.assertIn("VERIFY INCOMPLETE", plugin._work("task", d, "do", 1, True, "")["verify"])

    def test_verifier_mutation_detected_even_on_resume(self):
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_snap", side_effect=["before", "after", "verifier-write"]), \
             patch.object(plugin, "_agy", return_value=("OK", "done", "", "c", {})), \
             patch.object(plugin, "_git", return_value=""), patch.object(plugin, "_log"), \
             patch.object(plugin, "_verify", return_value="VERIFY PASS"):
            result = plugin._work("finish", d, "do", 1, True, "previous")
            self.assertIn("verifier_warning", result)
            self.assertIn("VERIFY INCOMPLETE", result["verify"])

    def test_binary_diff_is_limited(self):
        def git(cwd, *args):
            return "Binary files a/x and b/x differ\n" if args and args[0] == "diff" else ""
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_snap", side_effect=["before", "after", "after"]), \
             patch.object(plugin, "_agy", return_value=("OK", "done", "", "c", {})), \
             patch.object(plugin, "_git", side_effect=git), patch.object(plugin, "_log"), \
             patch.object(plugin, "_verify", return_value="VERIFY PASS"):
            self.assertIn("VERIFY LIMITED", plugin._work("task", d, "do", 1, True, "")["verify"])

    def test_ancestor_of_blocked_root_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            blocked = Path(d) / "private"
            blocked.mkdir()
            with patch.object(plugin, "BLOCKED", [blocked.resolve()]):
                self.assertTrue(plugin._refuse(d))

    def test_empty_workdir_is_refused(self):
        for workdir in ("", "  ", None):
            self.assertTrue(plugin._refuse(workdir))

    def test_delegate_rejects_invalid_inputs(self):
        for args in (None, {"task": 1, "workdir": "."}, {"task": "test", "workdir": 1},
                     {"task": "test", "workdir": ".", "resume": 1},
                     {"task": "test", "workdir": ".", "minutes": "bad"}):
            with self.subTest(args=args):
                self.assertIn("error", json.loads(plugin.delegate(args)))

    def test_jev_is_off_by_default_and_never_replaces_verifier(self):
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_snap", return_value="same"), \
             patch.object(plugin, "_agy", return_value=("OK", "public report", "", "c", {})), \
             patch.object(plugin, "_git", return_value=""), patch.object(plugin, "_log"), \
             patch.object(plugin, "_jev_public", return_value="Jev advisory flags: guessed") as jev, \
             patch.object(plugin, "_verify", return_value="VERIFY PASS") as verify:
            plugin._work("task", d, "research", 1, True, "")
            jev.assert_not_called()
            verify.assert_called_once()

    def test_jev_missing_key_fails_open(self):
        with patch.dict(plugin.os.environ, {}, clear=True), patch.object(plugin.urllib.request, "urlopen") as send:
            self.assertIn("unavailable", plugin._jev_public("public"))
            send.assert_not_called()

    def test_jev_only_sends_report_and_returns_flags(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b'{"answers":{"guessed":{"noul":0.8},"unproven":{"noul":0.1}}}'
        with patch.dict(plugin.os.environ, {"JEV_API_KEY": "test-key"}), \
             patch.object(plugin.urllib.request, "urlopen", return_value=Response()) as send:
            self.assertIn("guessed", plugin._jev_public("public report"))
            request = send.call_args.args[0]
            self.assertEqual(json.loads(request.data)["state"], "public report")

    def test_jev_requires_explicit_approval_and_excerpt(self):
        with patch.object(plugin, "_jev_public", return_value="Jev advisory flags: none") as jev:
            for args in ({"public_text": "public", "user_approved": False},
                         {"public_text": "", "user_approved": True},
                         {"public_text": "x" * 12001, "user_approved": True}):
                self.assertIn("error", json.loads(plugin.jev_public_check(args)))
            jev.assert_not_called()
            self.assertIn("disabled", json.loads(plugin.jev_public_check({"public_text": "public", "user_approved": True}))["error"])
            jev.assert_not_called()

    def test_approval_contains_exact_task_and_directory(self):
        args = {"task": "change only x", "workdir": "project", "background": True}
        directive = plugin._approval("antigravity_delegate", args)
        self.assertEqual(directive["action"], "approve")
        self.assertNotEqual(directive["rule_key"], plugin._approval("antigravity_delegate", args)["rule_key"])
        for text in (args["task"], args["workdir"], "background=True"):
            self.assertIn(text, directive["message"])
        self.assertIsNone(plugin._approval("antigravity_stats", args))

    def test_register_exposes_approval_hook(self):
        class Context:
            def __init__(self):
                self.hooks = {}
            def register_hook(self, name, handler):
                self.hooks[name] = handler
            def register_tool(self, **kwargs):
                pass
        ctx = Context()
        plugin.register(ctx)
        self.assertIs(ctx.hooks["pre_tool_call"], plugin._approval)

    def test_plugin_settings_override_legacy_defaults(self):
        class Context:
            def get_config(self, key, default):
                return {"worker_model": "worker", "verifier_models": ["verifier"], "blocked_roots": []}.get(key, default)
            def register_hook(self, *args):
                pass
            def register_tool(self, **kwargs):
                pass
        with patch.object(plugin, "WORKER"), patch.object(plugin, "VERIFIERS"), patch.object(plugin, "BLOCKED"):
            plugin.register(Context())
            self.assertEqual(plugin.WORKER, "worker")
            self.assertEqual(plugin.VERIFIERS, ["verifier"])
            self.assertEqual(plugin.BLOCKED, [])

    def test_child_environment_does_not_inherit_provider_keys(self):
        with patch.dict(plugin.os.environ, {"PATH": "path", "HOME": "home", "OPENAI_API_KEY": "secret", "AGY_SECRET": "secret"}, clear=True):
            self.assertEqual(plugin._child_env(), {"PATH": "path", "HOME": "home"})

    def test_worker_permissions_are_role_dependent(self):
        class Proc:
            returncode = 0
            def communicate(self, *args, **kwargs):
                return ('{"event":"result","result":{"status":"SUCCESS","response":"ok"}}', "")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()) as spawn:
            for allow in (False, True):
                plugin._agy("task", ".", "model", 1, allow_edits=allow)
                argv = spawn.call_args.args[0]
                self.assertEqual("--dangerously-skip-permissions" in argv, allow)
                self.assertEqual(spawn.call_args.kwargs["env"], plugin._child_env())

    def test_git_gets_minimal_environment(self):
        with patch.object(plugin.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            plugin._git(".", "status")
            self.assertEqual(run.call_args.kwargs["env"], plugin._child_env())

    def test_empty_response_with_denied_actions_is_failure(self):
        class Proc:
            returncode = 0
            def communicate(self, *args, **kwargs):
                return ('{"event":"result","result":{"status":"SUCCESS","response":"","denied_actions":["command"]}}', "")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()):
            self.assertEqual(plugin._agy("task", ".", "model", 1)[0], "FAILED")

    def test_missing_executable_is_reported(self):
        with patch.object(plugin.shutil, "which", return_value="agy"), \
             patch.object(plugin.subprocess, "Popen", side_effect=OSError("missing")):
            self.assertEqual(plugin._agy("task", ".", "model", 1)[0], "FAILED")


if __name__ == "__main__":
    unittest.main()
