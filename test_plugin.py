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
            plugin.jev_public_check({"public_text": "public", "user_approved": True})
            jev.assert_called_once_with("public")


if __name__ == "__main__":
    unittest.main()
