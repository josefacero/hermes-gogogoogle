import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("gogogoogle_plugin", Path(__file__).with_name("__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


REAL_MCP_SERVERS = plugin._mcp_servers


class PluginTests(unittest.TestCase):
    def setUp(self):  # never run the real `agy mcp list` in a test; the listing has its own tests below
        patcher = patch.object(plugin, "_mcp_servers", return_value=["playwright"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_verifier_receives_bounded_full_reply_and_denial_is_incomplete(self):
        answer = "é" * 7000 + "END-SENTINEL"
        with patch.object(plugin, "_cooling", return_value={}), \
             patch.object(plugin, "_agy", return_value=("OK", "VERDICT: PASS\nchecked", "", "", {})) as agy:
            plugin._verify("task", answer, ".")
            self.assertIn(answer, agy.call_args.args[0])
            plugin._verify("task", "a" * (plugin.REPLY_LIMIT + 1), ".")
            self.assertIn("Reply cut at 32,000 characters", agy.call_args.args[0])
        with patch.object(plugin, "_cooling", return_value={}), \
             patch.object(plugin, "_agy", return_value=("OK", "VERDICT: PASS\n[gogogoogle] agy permission rules blocked: read_file.", "", "", {})):
            self.assertTrue(plugin._verify("task", "reply", ".").startswith("VERIFY INCOMPLETE"))
        with patch.object(plugin, "_cooling", return_value={}), \
             patch.object(plugin, "_agy", return_value=("OK", "VERDICT: FAIL\n[gogogoogle] agy permission rules blocked: read_file.", "", "", {})):
            self.assertTrue(plugin._verify("task", "reply", ".").startswith("VERIFY FAIL"))

    def test_poll_tolerates_job_finishing_after_start_time_lookup(self):
        jid = "abcdef123456"
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_home", return_value=Path(d)):
            plugin._jwrite(jid, {"status": "RUNNING", "process": plugin.PROCESS_ID, "phase": "WORKER"})
            plugin._JOB_STARTS[jid] = time.monotonic()
            original = Path.read_text
            def finishes_during_read(path, *args, **kwargs):
                data = original(path, *args, **kwargs)
                plugin._JOB_STARTS.pop(jid, None)
                return data
            with patch.object(Path, "read_text", finishes_during_read):
                result = json.loads(plugin.job({"id": jid}))
            self.assertEqual(result["status"], "RUNNING")
            self.assertIn("elapsed_seconds", result)

    def test_truncated_reply_and_worker_denial_are_incomplete(self):
        for reply in ("a" * (plugin.REPLY_LIMIT + 1), "done\n\n[gogogoogle] agy permission rules blocked: tool.",
                      "[Google sign-in URL redacted; sign in through Antigravity]"):
            with self.subTest(reply=reply[:10]), tempfile.TemporaryDirectory() as d, \
                 patch.object(plugin, "_snap", return_value="same"), patch.object(plugin, "_git", return_value=""), \
                 patch.object(plugin, "_log"), patch.object(plugin, "_agy", return_value=("OK", reply, "", "c", {})), \
                 patch.object(plugin, "_verify", return_value="VERIFY PASS (m)"):
                self.assertTrue(plugin._work("t", d, "research", 1, True, "")["verify"].startswith("VERIFY INCOMPLETE"))

    def test_background_phases_and_elapsed(self):
        worker_entered, worker_release = threading.Event(), threading.Event()
        verifier_entered, verifier_release = threading.Event(), threading.Event()
        def agy(*args, **kwargs):
            worker_entered.set()
            if not worker_release.wait(5):
                raise RuntimeError("worker test timed out")
            return ("OK", "done", "", "c", {})
        def verify(*args):
            verifier_entered.set()
            if not verifier_release.wait(5):
                raise RuntimeError("verifier test timed out")
            return "VERIFY PASS (m)"
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_home", return_value=Path(d)), \
             patch.object(plugin, "_refuse", return_value=""), patch.object(plugin, "_snap", return_value="same"), \
             patch.object(plugin, "_git", return_value=""), patch.object(plugin, "_log"), \
             patch.object(plugin, "_agy", side_effect=agy), patch.object(plugin, "_verify", side_effect=verify):
            try:
                jid = json.loads(plugin.delegate({"task": "t", "workdir": d, "role": "research", "background": True}))["job"]
                self.assertTrue(worker_entered.wait(5))
                running = json.loads(plugin.job({"id": jid}))
                self.assertEqual((running["status"], running["phase"]), ("RUNNING", "WORKER"))
                self.assertIn("elapsed_seconds", running)
                worker_release.set()
                self.assertTrue(verifier_entered.wait(5))
                self.assertEqual(json.loads(plugin.job({"id": jid}))["phase"], "VERIFYING")
                verifier_release.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    done = json.loads(plugin.job({"id": jid}))
                    if done.get("job_status") == "DONE":
                        break
                    time.sleep(.01)
                self.assertEqual((done["status"], done["phase"]), ("OK", "DONE"))
                self.assertIn("seconds", done)
                self.assertGreaterEqual(done["total_seconds"], done["seconds"])
                self.assertIn("started", done)
            finally:
                worker_release.set()
                verifier_release.set()

    def test_background_thread_keeps_the_request_profile(self):
        # Hermes picks the active profile from a ContextVar. A plain thread loses it, so a job started in a second profile
        # wrote its progress and result to the default profile's folder and the poll stayed at STARTING for ever.
        import contextvars
        profile = contextvars.ContextVar("profile", default="default")
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_home", side_effect=lambda: Path(d) / profile.get()),              patch.object(plugin, "_refuse", return_value=""), patch.object(plugin, "_work", return_value={"status": "OK", "result": "x"}):
            profile.set("second")
            jid = json.loads(plugin.delegate({"task": "t", "workdir": d, "role": "research", "background": True}))["job"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                done = json.loads(plugin.job({"id": jid}))
                if done.get("job_status") == "DONE":
                    break
                time.sleep(.01)
            self.assertEqual(done.get("phase"), "DONE")
            self.assertFalse((Path(d) / "default" / "jobs" / f"{jid}.json").exists())

    def test_background_thread_start_failure_does_not_leave_running_job(self):
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_home", return_value=Path(d)), \
             patch.object(plugin, "_refuse", return_value=""), \
             patch.object(plugin.threading.Thread, "start", side_effect=RuntimeError("no threads")):
            result = json.loads(plugin.delegate({"task": "t", "workdir": d, "background": True}))
            self.assertEqual(result["status"], "FAILED")
            self.assertNotIn(result["job"], plugin._JOB_STARTS)
            self.assertEqual(json.loads(plugin.job({"id": result["job"]}))["status"], "FAILED")

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
                self.assertEqual(spawn.call_args.kwargs["env"], plugin._child_env(browser=True))

    def test_read_only_rules_deny_other_mcp_servers_and_unsafe_browser_tools(self):
        with patch.object(plugin, "BROWSER_MODE", "headless"):
            for servers, expect_in, expect_out in (
                    (["playwright", "github", "brave-search"], ["mcp(github/*)", "mcp(brave-search/*)", "mcp(playwright/browser_evaluate)",
                                                                "mcp(playwright/browser_run_code_unsafe)"], ["mcp(*)", "mcp(playwright/*)"]),
                    (["github"], ["mcp(github/*)"], ["mcp(*)", "mcp(playwright/browser_evaluate)"]),
                    ([], [], ["mcp(*)"]),
                    (None, ["mcp(*)"], [])):
                with self.subTest(servers=servers), patch.object(plugin, "_mcp_servers", return_value=servers):
                    perms = plugin._read_only_permissions(".")
                    rules = perms["deny"]
                    self.assertEqual(rules[:3], ["write_file(*)", "edit_file(*)", "command(*)"])
                    self.assertTrue(perms["allow"][0].startswith("read_file("))
                    # a safe browser tool needs an explicit allow (no rule = denied headless); an unsafe one is never allowed
                    self.assertEqual("mcp(playwright/browser_close)" in perms["allow"], servers is not None and "playwright" in servers)
                    self.assertFalse(set(perms["allow"]) & set(rules))
                    for r in expect_in:
                        self.assertIn(r, rules)
                    for r in expect_out:
                        self.assertNotIn(r, rules)
        with patch.object(plugin, "BROWSER_MODE", "off"), patch.object(plugin, "_mcp_servers", return_value=["playwright"]):
            perms = plugin._read_only_permissions(".")
            self.assertIn("mcp(*)", perms["deny"])  # off: no MCP at all for read-only roles
            self.assertEqual(len(perms["allow"]), 1)

    def test_mcp_server_listing_is_parsed_or_refused(self):
        table = "NAME            TYPE   COMMAND\nplaywright      stdio  npx -y @playwright/mcp\ngithub          stdio  npx\n"
        def listing(out, code=0):
            plugin._MCP_SERVERS.clear()
            with patch.object(plugin.shutil, "which", return_value="agy"), \
                 patch.object(plugin.subprocess, "run") as run:
                run.return_value.stdout, run.return_value.returncode = out, code
                return REAL_MCP_SERVERS()
        self.assertEqual(listing(table), ["playwright", "github"])
        self.assertIsNone(listing("Error: no config\n"))  # no NAME header
        self.assertIsNone(listing(table, 1))  # non-zero exit
        self.assertIsNone(listing("NAME TYPE\nbad;name stdio\n"))  # a name we would not trust inside a rule
        plugin._MCP_SERVERS.clear()
        with patch.object(plugin.shutil, "which", return_value=None):
            self.assertIsNone(REAL_MCP_SERVERS())
        plugin._MCP_SERVERS.clear()

    def test_browser_environment_modes(self):
        for mode, headless in (("headless", True), ("visible", False)):
            with self.subTest(mode=mode), patch.object(plugin, "BROWSER_MODE", mode):
                env = plugin._child_env(browser=True)
                self.assertEqual(env.get("PLAYWRIGHT_MCP_HEADLESS") == "1", headless)
                self.assertEqual(env["PLAYWRIGHT_MCP_ISOLATED"], "1")
                self.assertTrue(env["PLAYWRIGHT_MCP_OUTPUT_DIR"].endswith("gogogoogle-browser"))
                self.assertFalse({k for k in plugin._child_env() if k.startswith("PLAYWRIGHT_")})  # git gets none of it
        with patch.object(plugin, "BROWSER_MODE", "off"):
            self.assertFalse({k for k in plugin._child_env(browser=True) if k.startswith("PLAYWRIGHT_")})

    def test_credential_folders_are_always_refused(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d).resolve()
            (home / ".ssh" / "keys").mkdir(parents=True)
            (home / "project").mkdir()
            with patch.object(plugin.Path, "home", return_value=home), patch.object(plugin, "BLOCKED", []):
                for workdir in (home / ".ssh", home / ".ssh" / "keys"):
                    self.assertIn("credential folder", plugin._refuse(str(workdir)))
                self.assertEqual(plugin._refuse(str(home / "project")), "")

    def test_browser_mode_setting_is_validated_and_changes_the_research_prompt(self):
        class Context:
            def __init__(self, mode):
                self.mode = mode
            def get_config(self, key, default):
                return {"browser_mode": self.mode}.get(key, default)
            def register_hook(self, *args):
                pass
            def register_tool(self, **kwargs):
                pass
        with patch.object(plugin, "BROWSER_MODE", "headless"):
            plugin.register(Context("off"))
            self.assertEqual(plugin.BROWSER_MODE, "off")
            self.assertIn("Do not use browser or Playwright tools", plugin._role_prompt("research"))
            plugin.register(Context("visible"))
            self.assertIn("browser_navigate", plugin._role_prompt("research"))
            self.assertNotIn("Do not use browser", plugin._role_prompt("research"))
            self.assertEqual(plugin._role_prompt("review"), plugin.ROLES["review"])
            for bad in ("hidden", 1, None):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    plugin.register(Context(bad))

    def test_item_tags_at_either_end_of_the_item_line_count(self):
        task = "GOAL\n1. a\n2. b\n3. c\n"
        for report in ("1. **Item 1: a (DONE)**\n2. **Item 2: b (DONE)**\n3. **Item 3: c (UNVERIFIED)**",
                       "- **Item 1 (DONE):** a\n- **Item 2 (DONE):** b\n- **Item 3 (MISSING):** c",
                       "### 1. a - DONE\n### 2. b - DONE\n### 3. c - MISSING",
                       "### 1. a\nStatus: DONE\n\n### 2. b\nStatus: MISSING\n### 3. c\nStatus: UNVERIFIED",
                       "1. [DONE] a\n2. [MISSING] b\n3. [UNVERIFIED] c"):
            with self.subTest(report=report[:24]):
                self.assertEqual(plugin._item_check(task, report)["missing_status_tags"], [])
        self.assertEqual(plugin._item_check(task, "1. a (DONE)\n2. b was done well\n3. c is finished")["missing_status_tags"], [2, 3])
        self.assertEqual(plugin._item_check(task, "### 1. a\n### 2. b\nStatus: DONE\n### 3. c\nStatus: DONE")["missing_status_tags"], [1])  # no borrowing

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

    def test_authentication_url_is_not_returned_or_saved_in_result(self):
        class Proc:
            returncode = 1
            def communicate(self, *args, **kwargs):
                return ("", "Authentication required https://accounts.google.com/o/oauth2/auth?state=private-value")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()):
            status, text, kind, _, _ = plugin._agy("task", ".", "model", 1)
        self.assertEqual((status, kind), ("FAILED", "auth"))
        self.assertNotIn("private-value", text)
        self.assertIn("sign in through Antigravity", text)

    def test_auth_redaction_precedes_truncation_and_covers_success_and_timeout(self):
        secret_url = "https://accounts.google.com/o/oauth2/auth?state=" + "x" * 2500 + "ENDSECRET"
        for escaped in (secret_url.replace("/", r"\/"), secret_url.replace(":", r"\u003a").replace(".", r"\u002e")):
            self.assertNotIn("ENDSECRET", plugin._redact_auth(escaped))
        class Proc:
            returncode = 1
            calls = 0
            def communicate(self, *args, **kwargs):
                return (secret_url, "")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()):
            self.assertNotIn("ENDSECRET", plugin._agy("t", ".", "m", 1)[1])
        class Success(Proc):
            returncode = 0
            def communicate(self, *args, **kwargs):
                return (json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": secret_url}}), "")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Success()):
            self.assertNotIn("ENDSECRET", plugin._agy("t", ".", "m", 1)[1])
        class Timeout(Proc):
            def communicate(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise plugin.subprocess.TimeoutExpired("agy", 1)
                return (secret_url, "")
            def kill(self):
                pass
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Timeout()), \
             patch.object(plugin, "_kill_tree"):
            self.assertNotIn("ENDSECRET", plugin._agy("t", ".", "m", 1)[1])

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

    def test_denied_mcp_tool_is_named_and_verifier_is_told_what_it_may_use(self):
        stream = "\n".join(json.dumps(e) for e in (
            {"event": "step_update", "step_update": {"state": "ERROR", "step_type": "tool", "tool_name": "call_mcp_tool",
             "tool_info": {"parameters": {"ServerName": "playwright", "ToolName": "browser_evaluate", "Arguments": {}},
                           "error": {"message": "permission check failed: Permission denied for mcp(playwright/browser_evaluate)."}}}},
            {"event": "result", "result": {"status": "SUCCESS", "response": "partial"}}))
        class Proc:
            returncode = 0
            def communicate(self, *args, **kwargs):
                return (stream, "")
        with patch.object(plugin.shutil, "which", return_value="agy"), patch.object(plugin.subprocess, "Popen", return_value=Proc()):
            self.assertIn("blocked: playwright/browser_evaluate.", plugin._agy("task", ".", "model", 1)[1])
        with patch.object(plugin, "_cooling", return_value={}), patch.object(plugin, "BROWSER_MODE", "headless"), \
             patch.object(plugin, "_agy", return_value=("OK", "VERDICT: PASS\nok", "", "", {})) as agy:
            plugin._verify("task", "reply", ".")
            self.assertIn("browser_navigate", agy.call_args.args[0])
            self.assertIn("do not retry it", agy.call_args.args[0])
        with patch.object(plugin, "_cooling", return_value={}), patch.object(plugin, "BROWSER_MODE", "off"), \
             patch.object(plugin, "_agy", return_value=("OK", "VERDICT: PASS\nok", "", "", {})) as agy:
            plugin._verify("task", "reply", ".")
            self.assertNotIn("browser_navigate", agy.call_args.args[0])

    def test_agy_review_fixes(self):
        # Leading blank line before a valid verdict still counts.
        with patch.object(plugin, "_cooling", return_value={}), \
             patch.object(plugin, "_agy", return_value=("OK", "\nVERDICT: PASS\nok", "", "", {})) as agy:
            self.assertTrue(plugin._verify("t", "a" * 7000, ".").startswith("VERIFY PASS"))
            self.assertIn("a" * 7000, agy.call_args.args[0])
            self.assertNotIn("Reply cut", agy.call_args.args[0])
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
        self.assertEqual([kind(t) for t in ("Authentication failed", "token unauthorized", "Not authorised", "auth required",
                                            "see the author's file", "authoritative source missing")],
                         ["auth", "auth", "auth", "auth", "other", "other"])

    def test_research_prompt_tells_agy_not_to_bypass_a_denied_tool(self):
        self.assertIn("do not bypass the denial", plugin.ROLES["research"])
        self.assertIn("browser_navigate", plugin.ROLES["research"])  # the default mode lets read-only runs browse
        self.assertIn("denied", plugin.ROLES["research"])
        self.assertNotIn("mcp(", json.dumps(plugin.ROLES))

    def test_job_file_locked_for_a_moment_is_retried(self):
        real_replace, real_read, calls = plugin.os.replace, Path.read_text, {"replace": 0, "read": 0}
        def replace(a, b):
            calls["replace"] += 1
            if calls["replace"] < 3:
                raise PermissionError(13, "locked")
            return real_replace(a, b)
        def read(self, *a, **k):
            calls["read"] += 1
            if calls["read"] < 3:
                raise PermissionError(13, "locked")
            return real_read(self, *a, **k)
        with tempfile.TemporaryDirectory() as d, patch.object(plugin, "_home", return_value=Path(d)),              patch.object(plugin.os, "replace", side_effect=replace), patch.object(Path, "read_text", read):
            plugin._jwrite("0123456789ab", {"status": "DONE"})
            self.assertEqual(json.loads(plugin.job({"id": "0123456789ab"}))["status"], "DONE")
        self.assertEqual((calls["replace"], calls["read"]), (3, 3))


if __name__ == "__main__":
    unittest.main()
