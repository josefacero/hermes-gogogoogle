"""gogogoogle: delegate bulk work to Antigravity (`agy`), verify it with a fresh model, log every run."""
import json
import os
import re
import shutil
import subprocess
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
        "verify": {"type": "boolean", "description": "Run a fresh verifier pass on the result. Default true."}},
        "required": ["task", "workdir"]}}
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


def _agy(prompt, cwd, model, minutes):
    """One agy pass. Returns (status, text, fail_kind). Prompt goes over stdin so long briefs fit Windows' command-line cap."""
    agy = shutil.which("agy")
    if not agy:
        return "FAILED", "`agy` is not on PATH; install the Antigravity CLI first.", "other"
    cmd = [agy, "--input-format", "stream-json", "--output-format", "stream-json", "--model", model,
           "--print-timeout", f"{minutes * 60}s", "--dangerously-skip-permissions"]
    msg = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
    try:
        out, err = p.communicate(msg, timeout=minutes * 60 + 60)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return "TIMED OUT", out[-2000:], "other"
    res = {}
    for line in reversed(out.splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("event") == "result":
            res = ev.get("result") or {}
            break
    if res.get("status") == "SUCCESS" and "print timeout" not in err:
        return "OK", res.get("response") or "(empty reply)", ""
    text = "\n".join(x for x in (res.get("error"), err) if x) or out[-1500:]
    status = "TIMED OUT" if "print timeout" in err else "FAILED"
    return status, text[-1500:], next((k for k, rx in FAIL_KINDS if rx.search(text)), "other")


def _verify(task, answer, cwd):
    brief = ("You are an independent verifier. Another agent was given this task and replied below. Check the claims against "
             "the real files in the working folder. Do not modify anything. First line of your reply must be "
             "'VERDICT: PASS' or 'VERDICT: FAIL', then the reasons.\n\nTASK:\n" + task + "\n\nREPLY:\n" + answer[:6000])
    for m in VERIFIERS:
        status, text, kind = _agy(brief, cwd, m, 5)
        if status == "OK":
            ok = re.search(r"VERDICT:\s*PASS", text, re.I) is not None
            return f"VERIFY {'PASS' if ok else 'FAIL'} ({m})\n{text[:1500]}"
        if kind not in ("quota", "model", "auth"):
            return f"VERIFY SKIPPED: {m} {status}: {text[:300]}"
    return "VERIFY SKIPPED: no verifier model answered"


def delegate(args, **kwargs):
    task, workdir = (args.get("task") or "").strip(), args.get("workdir") or ""
    role = args.get("role") or "do"
    if not task or role not in ROLES:
        return json.dumps({"error": "task is required and role must be one of " + ", ".join(ROLES)})
    why = _refuse(workdir)
    if why:
        return json.dumps({"error": "REFUSED: " + why})
    minutes = min(30, max(1, int(args.get("minutes") or 10)))
    t0 = time.time()
    status, text, kind = _agy(f"{ROLES[role]}\n\nTASK:\n{task}", workdir, WORKER, minutes)
    _log(ts=time.strftime("%Y-%m-%dT%H:%M:%S"), role=role, model=WORKER, status=status, secs=round(time.time() - t0),
         **({"fail": kind} if status == "FAILED" else {}))
    out = {"status": status, "model": WORKER, "seconds": round(time.time() - t0), "result": text}
    if status == "OK" and args.get("verify", True):
        out["verify"] = _verify(task, text, workdir)
    return json.dumps(out)


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
    return json.dumps({"runs": len(rows), "by_status": by, "failed_by_reason": fails, "avg_seconds": avg})


def register(ctx):
    ctx.register_tool(name="antigravity_delegate", toolset="gogogoogle", schema=DELEGATE, handler=delegate)
    ctx.register_tool(name="antigravity_stats", toolset="gogogoogle", schema=STATS, handler=stats)
