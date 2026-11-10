"""Hermes-independent delegation orchestration; execution services are injected."""
import re
import time
from pathlib import Path


def work(task, workdir, role, minutes, verify, resume, progress, api):
    _snap, _git, _agy, _log, _item_check, _verify, _role_prompt = (api[k] for k in ("_snap", "_git", "_agy", "_log", "_item_check", "_verify", "_role_prompt"))
    WORKER, REPLY_LIMIT, DIFF_LIMIT = api["WORKER"], api["REPLY_LIMIT"], api["DIFF_LIMIT"]
    t0 = time.time()
    elapsed_start = time.monotonic()
    if progress:
        progress("WORKER")
    # A resumed conversation already has its brief; the new text is just the follow-up.
    prompt = task if resume else f"{_role_prompt(role)}\n\nTASK:\n{task}"
    before = _snap(workdir)
    initial_status = _git(workdir, "status", "--porcelain")
    status, text, kind, conv, usage = _agy(prompt, workdir, WORKER, minutes, resume, allow_edits=(role == "do"))
    after = _snap(workdir)
    changed = before is not None and after is not None and after != before
    _log(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), role=role, model=WORKER, status=status, secs=round(time.time() - t0),
         resumed=bool(resume), tin=usage.get("input_tokens", 0), tout=usage.get("output_tokens", 0),
         **({"fail": kind} if status == "FAILED" else {}))
    out = {"status": status, "model": WORKER, "seconds": round(time.time() - t0), "conversation": conv, "result": text}
    if role == "research" and not resume:
        coverage = _item_check(task, text)
        if coverage:
            out["item_check"] = coverage
    diff = ""
    if before is None or after is None:
        out["git"] = "Git change detection unavailable; changes could not be checked."
    else:
        out["git"] = {"changed": changed, "status": (_git(workdir, "status", "--porcelain") or "")[:1500],
                      "scope": "Ignored files and content changes within existing untracked or binary files may be missed."}
        diff = _git(workdir, "diff", "HEAD", "--stat", "-p")
        if changed and role != "do":  # read-only roles are enforced by checking, not trusting
            out["warning"] = (f"READ-ONLY VIOLATION: role '{role}' must not edit files but the working tree changed. "
                              "Inspect the changes and decide how to restore only unwanted work.")
    if (Path(workdir) / ".playwright-mcp").exists():
        out["browser_files"] = ("agy's browser tool left a .playwright-mcp/ folder here. The change check ignores it. "
                                "Delete it or add it to .gitignore before `git add -A`.")
    if status == "TIMED OUT" and conv:
        out["note"] = f"Stopped at the {minutes}-minute limit; call again with resume='{conv}' to continue."
    if status == "OK" and verify:
        if progress:
            progress("VERIFYING")
        incomplete = (resume or len(text) > REPLY_LIMIT or diff is None or len(diff) > DIFF_LIMIT or before is None
                      or after is None or initial_status is None or "[gogogoogle] agy permission rules blocked:" in text
                      or "[Google sign-in URL redacted;" in text)
        out["verify"] = _verify(task, text, workdir, diff or "")
        verified_snapshot = _snap(workdir)
        if verified_snapshot is None or verified_snapshot != after:
            incomplete = True
            out["verifier_warning"] = "The working tree changed during verification or could not be checked."
        if incomplete:
            out["verify"] = out["verify"].replace("VERIFY PASS", "VERIFY INCOMPLETE", 1) + "\nResumed task, blocked action, truncated input, changed snapshot, or unavailable Git evidence requires manual review."
        else:
            untracked = _git(workdir, "ls-files", "-o", "--exclude-standard")
            if untracked is None:
                out["verify"] = out["verify"].replace("VERIFY PASS", "VERIFY INCOMPLETE", 1) + "\nUntracked file check failed."
            elif untracked:
                out["verify"] = out["verify"].replace("VERIFY PASS", "VERIFY LIMITED", 1) + "\nUntracked file contents are not in the git diff; inspect them separately."
            if re.search(r"(?m)^(Binary files .* differ|GIT binary patch)", diff or ""):
                out["verify"] = out["verify"].replace("VERIFY PASS", "VERIFY LIMITED", 1) + "\nBinary contents are not included in the diff."
            if initial_status:
                out["verify"] = out["verify"].replace("VERIFY PASS", "VERIFY LIMITED", 1) + "\nThe worktree already had changes; the diff may include prior work."
        if "warning" in out:  # a read-only role that edited files never passes
            out["verify"] = re.sub(r"^VERIFY [A-Z]+", "VERIFY FAIL", out["verify"], count=1) + "\nRead-only role changed the working tree."
    out["total_seconds"] = round(time.monotonic() - elapsed_start)
    if not verify:
        out["verify"] = "VERIFY SKIPPED: verification was disabled; result is unverified."
    return out
