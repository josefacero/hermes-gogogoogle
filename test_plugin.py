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

    def test_numbered_research_coverage_is_only_a_tag_check(self):
        task = "1. First source\n2. Second source\n3. Third source"
        report = "1. DONE: checked\n2. UNVERIFIED: blocked"
        result = plugin._item_check(task, report)
        self.assertEqual(result["numbered_items"], 3)
        self.assertEqual(result["missing_status_tags"], [3])
        self.assertIn("do not prove", result["note"])
        self.assertIsNone(plugin._item_check("1. Single item", report))

    def test_delegate_rejects_invalid_inputs(self):
        for args in (None, {"task": 1, "workdir": "."}, {"task": "test", "workdir": 1},
                     {"task": "test", "workdir": ".", "resume": 1},
                     {"task": "test", "workdir": ".", "minutes": "bad"}):
            with self.subTest(args=args):
                self.assertIn("error", json.loads(plugin.delegate(args)))

    def test_approval_contains_exact_task_and_directory(self):
        args = {"task": "change only x", "workdir": "project", "background": True}
        directive = plugin._approval("antigravity_delegate", args)
        self.assertEqual(directive["action"], "approve")
        self.assertNotEqual(directive["rule_key"], plugin._approval("antigravity_delegate", args)["rule_key"])
        for text in (args["task"], args["workdir"], "background=True"):
            self.assertIn(text, directive["message"])
        self.assertIsNone(plugin._approval("antigravity_stats", args))

    def test_approval_puts_task_last_with_length_and_hash(self):
        task = "  " + "y" * 900 + "\n"
        message = plugin._approval("antigravity_delegate", {"task": task, "workdir": "proj"})["message"]
        sent = task.strip()
        self.assertTrue(message.endswith(sent))
        self.assertIn(f"Task ({len(sent)} chars, sha256 {plugin.hashlib.sha256(sent.encode()).hexdigest()[:12]})", message)
        self.assertLess(message.index("proj"), message.index("Task ("))

    def test_read_only_violation_never_passes(self):
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_snap", side_effect=["before", "after", "after"]), \
             patch.object(plugin, "_agy", return_value=("OK", "done", "", "c", {})), \
             patch.object(plugin, "_git", return_value=""), patch.object(plugin, "_log"), \
             patch.object(plugin, "_verify", return_value="VERIFY PASS (m)\nlooks fine"):
            result = plugin._work("look", d, "review", 1, True, "")
            self.assertIn("READ-ONLY VIOLATION", result["warning"])
            self.assertTrue(result["verify"].startswith("VERIFY FAIL"))

    def test_hard_timeout_keeps_streamed_conversation_id(self):
        class Proc:
            pid, calls = 4242, 0
            def communicate(self, *args, **kwargs):
                Proc.calls += 1
                if Proc.calls == 1:
                    raise plugin.subprocess.TimeoutExpired("agy", 1)
                return ('{"event":"init","conversation_id":"conv-123"}\n', "")
            def kill(self):
                pass
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()), \
             patch.object(plugin.subprocess, "run"):
            status, _, _, conv, _ = plugin._agy("task", ".", "model", 1)
        self.assertEqual((status, conv), ("TIMED OUT", "conv-123"))

    def test_stats_skips_broken_log_lines(self):
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_home", return_value=Path(d)):
            (Path(d) / "runs.jsonl").write_text('{"status":"OK","secs":4}\n{"status":"OK"\n[]\n', encoding="utf-8")
            result = json.loads(plugin.stats({}))
            self.assertEqual((result["runs"], result["avg_seconds"]), (1, 4))

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
        with patch.dict(plugin.os.environ, {"PATH": "path", "HOME": "home", "ProgramFiles": "pf", "OPENAI_API_KEY": "secret",
                                            "AGY_SECRET": "secret"}, clear=True):
            self.assertEqual({k.upper(): v for k, v in plugin._child_env().items()}, {"PATH": "path", "HOME": "home", "PROGRAMFILES": "pf"})

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

    def test_permission_error_steps_are_reported(self):
        # Shape captured from agy 1.5.1: the denial is an ERROR step; the result still says SUCCESS.
        stream = "\n".join(json.dumps(e) for e in (
            {"event": "init", "conversation_id": "c1"},
            {"event": "step_update", "step_update": {"state": "ERROR", "step_type": "tool", "tool_name": "write_file",
             "tool_info": {"name": "write_file", "error": {"type": "TOOL_ERROR", "message":
             "permission check failed for write_file \"x\": Permission denied for write_file(x). Matches user-configured deny rule."}}}},
            {"event": "step_update", "step_update": {"state": "ERROR", "step_type": "tool", "tool_name": "read_file",
             "tool_info": {"error": {"type": "TOOL_ERROR", "message": "file does not exist"}}}},
            {"event": "result", "result": {"conversation_id": "c1", "status": "SUCCESS", "response": "could not write"}}))
        class Proc:
            returncode = 0
            def communicate(self, *args, **kwargs):
                return (stream, "")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()):
            status, text, _, conv, _ = plugin._agy("task", ".", "model", 1)
        self.assertEqual((status, conv), ("OK", "c1"))
        self.assertTrue(text.startswith("could not write"))
        self.assertIn("blocked: write_file.", text)

    def test_agy_review_fixes(self):
        # Leading blank line before a valid verdict still counts.
        with patch.object(plugin, "_cooling", return_value={}), \
             patch.object(plugin, "_agy", return_value=("OK", "\nVERDICT: PASS\nok", "", "", {})) as agy:
            self.assertTrue(plugin._verify("t", "a" * 7000, ".").startswith("VERIFY PASS"))
            self.assertIn("Reply cut at 6,000 characters", agy.call_args.args[0])
        # A read-only violation fails even when the verifier was skipped.
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_snap", side_effect=["b", "a", "a"]), \
             patch.object(plugin, "_agy", return_value=("OK", "done", "", "c", {})), \
             patch.object(plugin, "_git", return_value=""), patch.object(plugin, "_log"), \
             patch.object(plugin, "_verify", return_value="VERIFY SKIPPED: no verifier model answered"):
            self.assertTrue(plugin._work("look", d, "research", 1, True, "")["verify"].startswith("VERIFY FAIL"))
        # agy's own print timeout without a result event keeps the init conversation id; error dicts do not crash.
        class Proc:
            returncode = 1
            def communicate(self, *args, **kwargs):
                return ('{"event":"init","conversation_id":"c9"}\n{"event":"result","result":{"error":{"type":"x"}}}',
                        "print timeout reached")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()):
            status, _, _, conv, _ = plugin._agy("task", ".", "model", 1)
        self.assertEqual((status, conv), ("TIMED OUT", "c9"))
        # Models often send null for optional arguments.
        with patch.object(plugin, "_refuse", return_value="stop"):
            self.assertIn("REFUSED", json.loads(plugin.delegate({"task": "t", "workdir": ".", "role": None, "resume": None}))["error"])

    def test_browser_snapshots_are_not_worker_edits(self):
        def run(cmd, **kwargs):
            return plugin.subprocess.CompletedProcess(cmd, 0, stdout="" if cmd[1] == "diff" else "notes.md\n" + self.extra)
        with patch.object(plugin.subprocess, "run", side_effect=run):
            self.extra = ""
            plain = plugin._snap(".")
            self.extra = ".playwright-mcp/page-1.yml\n.playwright-mcp/console-1.log\n"
            self.assertEqual(plugin._snap("."), plain)
            self.extra = "new_file.py\n"
            self.assertNotEqual(plugin._snap("."), plain)

    def test_denied_actions_given_as_dicts_are_named(self):
        stream = '{"event":"result","result":{"status":"SUCCESS","response":"","denied_actions":[{"action":"mcp","display_name":"CallMcpTool"}]}}'
        class Proc:
            returncode = 0
            def communicate(self, *args, **kwargs):
                return (stream, "")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()):
            status, text, *_ = plugin._agy("task", ".", "model", 1)
        self.assertEqual(status, "FAILED")
        self.assertIn("blocked: CallMcpTool.", text)

    def test_missing_executable_is_reported(self):
        with patch.object(plugin.shutil, "which", return_value="agy"), \
             patch.object(plugin.subprocess, "Popen", side_effect=OSError("missing")):
            self.assertEqual(plugin._agy("task", ".", "model", 1)[0], "FAILED")

    def _run_agy(self, stream, rc=0):
        class Proc:
            returncode = rc
            def communicate(self, *args, **kwargs):
                return (stream, "")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()):
            return plugin._agy("task", ".", "model", 1)

    def test_timeout_wait_is_bounded_when_pipes_stay_open(self):
        # A browser or shell that outlives agy can keep its output pipes open; the second wait must not be unbounded.
        class Proc:
            pid, killed, timeouts = 4242, False, []
            def communicate(self, *args, **kwargs):
                Proc.timeouts.append(kwargs.get("timeout"))
                raise plugin.subprocess.TimeoutExpired("agy", 1)
            def kill(self):
                Proc.killed = True
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()), \
             patch.object(plugin.subprocess, "run") as run:
            status, _, _, conv, _ = plugin._agy("task", ".", "model", 1, resume="old-1")
        self.assertEqual((status, conv, Proc.killed), ("TIMED OUT", "old-1", True))
        self.assertIsNotNone(Proc.timeouts[1])
        if plugin.os.name == "nt":
            self.assertEqual(run.call_args.args[0][:3], ["taskkill", "/F", "/T"])

    def test_git_hides_browser_folder_from_status_but_not_from_diff(self):
        with patch.object(plugin.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "?? .playwright-mcp/\n M real.py\n"
            self.assertEqual(plugin._git(".", "status", "--porcelain"), " M real.py\n")
            run.return_value.stdout = "diff --git a/.playwright-mcp/x b/.playwright-mcp/x\n"
            self.assertIn(".playwright-mcp/x", plugin._git(".", "diff", "HEAD"))

    def test_browser_folder_is_reported(self):
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_snap", return_value="same"), \
             patch.object(plugin, "_agy", return_value=("OK", "done", "", "c", {})), \
             patch.object(plugin, "_git", return_value=""), patch.object(plugin, "_log"), \
             patch.object(plugin, "_verify", return_value="VERIFY PASS (m)"):
            (Path(d) / ".playwright-mcp").mkdir()
            result = plugin._work("look", d, "research", 1, True, "")
        self.assertIn(".gitignore", result["browser_files"])
        self.assertTrue(result["verify"].startswith("VERIFY PASS"))

    def test_null_verify_still_verifies(self):
        args = {"task": "t", "workdir": ".", "verify": None}
        with patch.object(plugin, "_refuse", return_value=""), patch.object(plugin, "_work", return_value={}) as work:
            plugin.delegate(args)
            plugin.delegate({**args, "verify": False})
        self.assertEqual([c.args[4] for c in work.call_args_list], [True, False])
        self.assertIn("verify=True", plugin._approval("antigravity_delegate", args)["message"])

    def test_resume_must_look_like_a_conversation_id(self):
        with patch.object(plugin, "_refuse", return_value=""), patch.object(plugin, "_work", return_value={}) as work:
            for bad in ("--help", "a b", "x\nTask", "../x", "-c"):
                with self.subTest(resume=bad):
                    self.assertIn("error", json.loads(plugin.delegate({"task": "t", "workdir": ".", "resume": bad})))
            for good in ("def39ff1-5f7c-4d60-a05d-498a5dcf003b", "conv-123"):
                plugin.delegate({"task": "t", "workdir": ".", "resume": good})
        self.assertEqual(work.call_count, 2)

    def test_quota_reset_time_is_remembered(self):
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_home", return_value=Path(d)):
            now = plugin.time.time()
            plugin._cool("a", "Quota exhausted. Resets in 2h15m")
            plugin._cool("b", "Resets in 45m")
            plugin._cool("c", "Resets in ")  # no time given: nothing to remember
            cooling = plugin._cooling()
            self.assertEqual(sorted(cooling), ["a", "b"])
            self.assertAlmostEqual(cooling["a"] - now, 2 * 3600 + 15 * 60, delta=5)
            self.assertAlmostEqual(cooling["b"] - now, 45 * 60, delta=5)
            for content in (json.dumps({"old": now - 1, "bad": "x"}), "[]", "{broken"):
                (Path(d) / "cooldown.json").write_text(content, encoding="utf-8")
                self.assertEqual(plugin._cooling(), {})

    def test_busy_service_tries_the_next_verifier(self):
        busy = '{"event":"result","result":{"status":"ERROR","error":"API error (attempt 1): UNAVAILABLE (code 503): The service is currently unavailable."}}'
        self.assertEqual(self._run_agy(busy, rc=1)[2], "busy")
        replies = [("FAILED", "503 unavailable", "busy", "", {}), ("OK", "VERDICT: PASS\nok", "", "", {})]
        with patch.object(plugin, "_cooling", return_value={}), patch.object(plugin, "_agy", side_effect=replies):
            self.assertTrue(plugin._verify("t", "a", ".").startswith("VERIFY PASS"))

    def test_plain_not_found_is_not_a_model_error(self):
        texts = ["Error: workspace file not found", "command not found: git", "models/gemini-9 is not found for API version v1",
                 "unknown model foo", "429 quota exceeded"]
        kinds = [next((k for k, rx in plugin.FAIL_KINDS if rx.search(t)), "other") for t in texts]
        self.assertEqual(kinds, ["other", "other", "model", "model", "quota"])

    def test_blank_reply_and_nameless_denial(self):
        self.assertEqual(self._run_agy('{"event":"result","result":{"status":"SUCCESS","response":" \\n"}}')[:2], ("OK", "(empty reply)"))
        nameless = '{"event":"result","result":{"status":"SUCCESS","response":"","denied_actions":[{}]}}'
        self.assertIn("blocked: tool.", self._run_agy(nameless)[1])

    def test_approval_says_what_do_mode_can_do(self):
        ask = lambda role: plugin._approval("antigravity_delegate", {"task": "t", "workdir": "p", "role": role})["message"]
        self.assertIn("skips agy's permission prompts", ask("do"))
        self.assertIn("not a sandbox", ask("review"))
        self.assertNotIn("skips", ask("review"))

    def test_blocked_root_equal_inside_and_message(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "private"
            (root / "sub").mkdir(parents=True)
            with patch.object(plugin, "BLOCKED", [root]):
                for workdir in (root, root / "sub"):
                    with self.subTest(workdir=str(workdir)):
                        why = plugin._refuse(str(workdir))
                        self.assertIn("blocked root", why)
                        self.assertIn("blocked_roots", why)
                        self.assertIn("GOGOGOOGLE_BLOCKED_ROOTS", why)
                other = Path(d).resolve() / "other"
                other.mkdir()
                self.assertEqual(plugin._refuse(str(other)), "")

    def _register_with(self, roots):
        class Context:
            def get_config(self, key, default):
                return {"worker_model": "w", "verifier_models": ["v"], "blocked_roots": roots}.get(key, default)
            def register_hook(self, *args):
                pass
            def register_tool(self, **kwargs):
                pass
        plugin.register(Context())

    def test_register_blocked_roots_list_string_and_bad_type(self):
        with patch.object(plugin, "WORKER"), patch.object(plugin, "VERIFIERS"), patch.object(plugin, "BLOCKED"):
            with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
                self._register_with([a, b])
                self.assertEqual(plugin.BLOCKED, [Path(a).resolve(), Path(b).resolve()])
                self._register_with(a)  # a plain string is one path, not a crash
                self.assertEqual(plugin.BLOCKED, [Path(a).resolve()])
                self._register_with(a + plugin.os.pathsep + b)
                self.assertEqual(plugin.BLOCKED, [Path(a).resolve(), Path(b).resolve()])
            for bad in (5, {"x": 1}, [1], [""], ["ok", None]):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    self._register_with(bad)

    def test_every_tool_description_names_gogogoogle(self):
        for schema in (plugin.DELEGATE, plugin.JOB, plugin.STATS):
            self.assertTrue(schema["description"].startswith("gogogoogle:"), schema["name"])
        manifest = (Path(__file__).with_name("plugin.yaml")).read_text(encoding="utf-8")
        self.assertRegex(manifest, r'(?m)^description: "?gogogoogle:')

    def test_status_codes_need_word_boundaries(self):
        kind = lambda t: next((k for k, rx in plugin.FAIL_KINDS if rx.search(t)), "other")
        self.assertEqual([kind(t) for t in ("HTTP 429 too many", "HTTP 403 Forbidden", "HTTP 401", "id 14290 done", "port 14010 closed", "file 40312.txt")],
                         ["quota", "auth", "auth", "other", "other", "other"])


if __name__ == "__main__":
    unittest.main()
