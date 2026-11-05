"""gogogoogle: delegate bulk work to Antigravity (`agy`), verify it with a fresh model, log every run."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

WORKER = os.environ.get("ANTIGRAVITY_MODEL", "gemini-3.8-flash-high")
# Verifier chain: first model that answers wins. Override with a comma list; names come from `agy models`.
VERIFIERS = [m.strip() for m in os.environ.get(
    "ANTIGRAVITY_VERIFIER_MODELS", "claude-sonnet-5-5-low,gemini-4-argon-high,gemini-3.8-flash-high").split(",") if m.strip()]
BLOCKED = [Path(p).resolve() for p in os.environ.get("GOGOGOOGLE_BLOCKED_ROOTS", "").split(os.pathsep) if p.strip()]
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ROLES = {
    "do": "Do the task. Edit files in the working folder as needed. End with a list of every file you changed.",
    "research": "Research only. Do not modify any file. Reply with Summary, Findings (cite sources), Unverified.",
    "review": "Review only. Do not modify any file. Reply with problems ranked worst first, each with file and line.",
}
# Why a run failed, so a dead model or empty quota moves to the next model but a real error does not.
FAIL_KINDS = [("quota", re.compile(r"quota|rate.?limit|exhausted|429", re.I)),
              ("model", re.compile(r"unknown model|invalid model|not found|unsupported model", re.I)),
              ("auth", re.compile(r"auth|sign.?in|login|401|403", re.I))]

DELEGATE = {
    "name": "antigravity_delegate",
    "description": (
        "Hand a bulk task (mass edits, file generation, broad reading, first-draft research, second-opinion review) to the "
        "Antigravity `agy` CLI. It runs on the user's Antigravity quota, so it saves this session's tokens. "
        "WARNING: agy runs with all permissions auto-approved and can edit or delete files in workdir; use git as the undo. "
        "With verify=true a fresh model checks the result and the reply ends with VERIFY PASS or VERIFY FAIL."),
    "parameters": {"type": "object", "properties": {
        "task": {"type": "string", "description": "What to do. Be specific; agy has no memory of this chat."},
        "workdir": {"type": "string", "description": "Existing folder agy works in."},
        "role": {"type": "string", "enum": list(ROLES), "description": "do (may edit files), research or review (told not to edit)."},
        "minutes": {"type": "integer", "description": "Time limit, 1 to 30. Default 10."},
        "verify": {"type": "boolean", "description": "Run a fresh verifier pass on the result. Default true."},
        "background": {"type": "boolean", "description": "Return a job id at once; poll it with antigravity_job. Use for runs over a minute."},
        "resume": {"type": "string", "description": "A conversation id from an earlier run (for example after TIMED OUT); task is then the follow-up."}},
        "required": ["task", "workdir"]}}
JOB = {"name": "antigravity_job", "description": "Check a background antigravity_delegate job. Returns RUNNING, or the full result once done.",
       "parameters": {"type": "object", "properties": {"id": {"type": "string", "description": "Job id from antigravity_delegate."}},
                      "required": ["id"]}}
STATS = {"name": "antigravity_stats",
         "description": "Show how many delegated runs succeeded, failed (and why), and their average time.",
         "parameters": {"type": "object", "properties": {}}}


def _home():
    try:
        from plugins.plugin_storage import plugin_data_dir
        return Path(plugin_data_dir("gogogoogle"))
    except Exception:  # shortcut: fallback path when run outside Hermes (tests); fine until the API moves
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "plugin-data" / "gogogoogle"


def _log(**row):
    d = _home()
    d.mkdir(parents=True, exist_ok=True)
    with (d / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _refuse(workdir):
    p = Path(workdir).expanduser()
    if not p.is_dir():
        return f"workdir {workdir!r} is not an existing folder"
    p = p.resolve()
    if p == Path(p.anchor) or p == Path.home().resolve():
        return "workdir is a drive root or your home folder; pick a project folder"
    for b in BLOCKED:
        if p == b or b in p.parents:
            return f"workdir is inside a blocked root ({b}); set by GOGOGOOGLE_BLOCKED_ROOTS"
    return ""


def _cooling():
    """{model: epoch when its quota returns} for models still cooling down."""
    try:
        d = json.loads((_home() / "cooldown.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {m: t for m, t in d.items() if t > time.time()}


def _cool(model, text):
    """Remember a quota reset ('Resets in 106h27m') so later verifier chains skip the model instead of re-asking it."""
    mt = re.search(r"Resets in (?:(\d+)h)?\s*(?:(\d+)m)?", text)
    if not mt or not (mt.group(1) or mt.group(2)):
        return
    d = _cooling()
    d[model] = time.time() + int(mt.group(1) or 0) * 3600 + int(mt.group(2) or 0) * 60
    _home().mkdir(parents=True, exist_ok=True)
    (_home() / "cooldown.json").write_text(json.dumps(d), encoding="utf-8")


def _agy(prompt, cwd, model, minutes, resume=""):
    """One agy pass. Returns (status, text, fail_kind, conversation_id, usage). Prompt goes over stdin so long briefs fit Windows' command-line cap."""
    agy = shutil.which("agy")
    if not agy:
        return "FAILED", "`agy` is not on PATH; install the Antigravity CLI first.", "other", "", {}
    cmd = [agy, "--input-format", "stream-json", "--output-format", "stream-json", "--model", model,
           "--print-timeout", f"{minutes * 60}s", "--dangerously-skip-permissions"]
    if resume:
        cmd += ["--conversation", resume]
    msg = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
    try:
        out, err = p.communicate(msg, timeout=minutes * 60 + 60)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return "TIMED OUT", out[-2000:], "other", resume, {}
    res = {}
    for line in reversed(out.splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("event") == "result":
            res = ev.get("result") or {}
            break
    conv = res.get("conversation_id", "")
    if res.get("status") == "SUCCESS" and "print timeout" not in err:
        return "OK", res.get("response") or "(empty reply)", "", conv, res.get("usage") or {}
    text = "\n".join(x for x in (res.get("error"), err) if x) or out[-1500:]
    status = "TIMED OUT" if "print timeout" in err else "FAILED"
    kind = next((k for k, rx in FAIL_KINDS if rx.search(text)), "other")
    if kind == "quota":
        _cool(model, text)
    return status, text[-1500:], kind, conv, res.get("usage") or {}


def _git(cwd, *a):
    """stdout of a git command, or None when cwd is not a repo or git is missing."""
    try:
        r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=30, creationflags=NOWIN)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _snap(cwd):
    """Fingerprint of tracked changes plus the untracked file list. shortcut: edits inside an already-untracked file
    are not seen; upgrade by hashing untracked contents if that matters."""
    d, u = _git(cwd, "diff", "HEAD"), _git(cwd, "ls-files", "-o", "--exclude-standard")
    return None if d is None or u is None else hashlib.sha256((d + "\0" + u).encode("utf-8", "replace")).hexdigest()


def _verify(task, answer, cwd, diff=""):
    brief = ("You are an independent verifier. Another agent was given this task and replied below. Check the claims against "
             "the real files in the working folder. Do not modify anything. First line of your reply must be "
             "'VERDICT: PASS' or 'VERDICT: FAIL', then the reasons.\n\nTASK:\n" + task + "\n\nREPLY:\n" + answer[:6000]
             + ("\n\nACTUAL CHANGES (git diff, may be cut):\n" + diff[:6000] if diff else ""))
    for m in VERIFIERS:
        if m in _cooling():
            continue  # out of quota until its reset time
        status, text, kind, _, _ = _agy(brief, cwd, m, 5)
        if status == "OK":
            ok = re.search(r"VERDICT:\s*PASS", text, re.I) is not None
            return f"VERIFY {'PASS' if ok else 'FAIL'} ({m})\n{text[:1500]}"
        if kind not in ("quota", "model", "auth"):
            return f"VERIFY SKIPPED: {m} {status}: {text[:300]}"
    return "VERIFY SKIPPED: no verifier model answered"


def _work(task, workdir, role, minutes, verify, resume):
    t0 = time.time()
    # A resumed conversation already has its brief; the new text is just the follow-up.
    prompt = task if resume else f"{ROLES[role]}\n\nTASK:\n{task}"
    before = _snap(workdir)
    status, text, kind, conv, usage = _agy(prompt, workdir, WORKER, minutes, resume)
    changed = before is not None and _snap(workdir) != before
    _log(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), role=role, model=WORKER, status=status, secs=round(time.time() - t0),
         resumed=bool(resume), tin=usage.get("input_tokens", 0), tout=usage.get("output_tokens", 0),
         **({"fail": kind} if status == "FAILED" else {}))
    out = {"status": status, "model": WORKER, "seconds": round(time.time() - t0), "conversation": conv, "result": text}
    diff = ""
    if before is None:
        out["git"] = "workdir is not a git repo: file changes are not tracked or undoable. Run `git init` first for the safety net."
    else:
        out["git"] = {"changed": changed, "status": (_git(workdir, "status", "--porcelain") or "")[:1500]}
        diff = _git(workdir, "diff", "HEAD", "--stat", "-p") or ""
        if changed and role != "do":  # read-only roles are enforced by checking, not trusting
            out["warning"] = (f"READ-ONLY VIOLATION: role '{role}' must not edit files but the working tree changed. "
                              "Inspect with `git diff` and revert with `git restore .` if unwanted.")
    if status == "TIMED OUT" and conv:
        out["note"] = f"Stopped at the {minutes}-minute limit; call again with resume='{conv}' to continue."
    if status == "OK" and verify:
        out["verify"] = _verify(task, text, workdir, diff if changed else "")
    return out


def _jpath(jid):
    if not re.fullmatch(r"[0-9a-f]{12}", jid or ""):  # ids reach the filesystem
        raise ValueError("bad job id")
    return _home() / "jobs" / f"{jid}.json"


def _jwrite(jid, data):
    p = _jpath(jid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)  # atomic, so a poll never reads half a file


def delegate(args, **kwargs):
    task, workdir = (args.get("task") or "").strip(), args.get("workdir") or ""
    role, resume = args.get("role") or "do", (args.get("resume") or "").strip()
    if not task or role not in ROLES:
        return json.dumps({"error": "task is required and role must be one of " + ", ".join(ROLES)})
    why = _refuse(workdir)
    if why:
        return json.dumps({"error": "REFUSED: " + why})
    minutes = min(30, max(1, int(args.get("minutes") or 10)))
    verify = args.get("verify", True)
    if not args.get("background"):
        return json.dumps(_work(task, workdir, role, minutes, verify, resume))
    jid = os.urandom(6).hex()
    _jwrite(jid, {"status": "RUNNING", "started": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def run():
        try:
            _jwrite(jid, {**_work(task, workdir, role, minutes, verify, resume), "job_status": "DONE"})
        except Exception as e:  # a crashed job must still leave a readable record
            _jwrite(jid, {"status": "FAILED", "job_status": "DONE", "result": f"{type(e).__name__}: {e}"})
    threading.Thread(target=run, daemon=True).start()
    return json.dumps({"job": jid, "status": "RUNNING", "note": "Poll with antigravity_job."})


def job(args, **kwargs):
    try:
        p = _jpath((args.get("id") or "").strip())
        return p.read_text(encoding="utf-8") if p.exists() else json.dumps({"error": "no such job (a restart ends running jobs)"})
    except ValueError as e:
        return json.dumps({"error": str(e)})


def stats(args, **kwargs):
    f = _home() / "runs.jsonl"
    rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []
    by = {}
    for r in rows:
        by[r["status"]] = by.get(r["status"], 0) + 1
    fails = {}
    for r in rows:
        if r.get("fail"):
            fails[r["fail"]] = fails.get(r["fail"], 0) + 1
    avg = round(sum(r["secs"] for r in rows) / len(rows)) if rows else 0
    return json.dumps({"runs": len(rows), "by_status": by, "failed_by_reason": fails, "avg_seconds": avg,
                       "quota_cooldown_until": {m: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) for m, t in _cooling().items()},
                       "tokens_handled_by_agy": {"in": sum(r.get("tin", 0) for r in rows), "out": sum(r.get("tout", 0) for r in rows)}})


def register(ctx):
    ctx.register_tool(name="antigravity_delegate", toolset="gogogoogle", schema=DELEGATE, handler=delegate)
    ctx.register_tool(name="antigravity_job", toolset="gogogoogle", schema=JOB, handler=job)
    ctx.register_tool(name="antigravity_stats", toolset="gogogoogle", schema=STATS, handler=stats)
