---
name: gogogoogle
description: Hand a bounded task to Google Antigravity (`agy`) as a partner through the gogogoogle MCP server - roles do, research and review, each optionally checked by a fresh verifier pass, optionally in the background. Use when the user says "use gogogoogle", "send this to Antigravity" or "let Gemini do it", or when a task is bulk work (mass edits, file generation, broad reading, a first-draft report, a second-opinion review) that does not need your own judgment.
---

# gogogoogle - Antigravity as your partner

Tools (MCP server `gogogoogle`): `prepare`, `delegate`, `job`, `stats`.
Setup and the operator's policy: README section "Use from Claude Code or Codex". If a tool answers that the server is not trusted or the workspace is outside the allowed roots, tell the user what to change; do not try to work around it.

If you are Antigravity yourself, do the task directly. Delegating would only call you back.

## 1. Before you delegate
- Keep private or client-confidential material out. The task text, the files agy reads and the verifier input go to Google through agy.
- `workdir` is an absolute folder inside the operator's allowed roots, ideally a git repo (git is the only undo).
- Pick the role: `do` makes changes (only when the operator enabled it); `research` writes a cited report; `review` returns severity-ranked findings and edits nothing. Read-only roles are enforced by instruction and a git check, not by a sandbox.

## 2. Write the brief
The worker has none of your context. One self-contained brief: goal, exact files, constraints, how to check the result. Keep it to 3 to 8 numbered items and point at files instead of pasting them. Read-only roles cannot list a folder, so name the files.

## 3. Two steps, and the second needs the user
1. `prepare(task, workdir, role, minutes, verify, background)` returns the exact request (workspace, role, whether agy's permission prompts are skipped, host label) and a single-use token that expires in 2 minutes.
2. Show the user that request in plain words. Call `delegate(token)` only after they agree. The host's own tool prompt may also appear; an auto-approving host skips it, so your own check matters.

`background=true` returns a job id at once. Poll with `job(id)`; the job lives in the MCP server process and is lost if the host restarts it.

## 4. Check before you report
The reply is a claim, not proof.
- Read the `git` block: for `research` and `review`, `changed: true` means the worker edited files (a `READ-ONLY VIOLATION` warning). Inspect it before anything else.
- Read the verify line: `VERIFY PASS` is a second model's opinion from the same Google account family, not a guarantee. `LIMITED` or `INCOMPLETE` means the evidence was partial; review it yourself. `SKIPPED` means unverified.
- `TIMED OUT` is partial work: call `prepare` again with `resume` set to the conversation id from the result and a short follow-up.
- Re-run the tests or open the files yourself before telling the user it is done.

Use `stats` for run counts, failures and any model quota cooldown. Do not quote token savings or quota left; neither is measured here.

## Limits
Uses the user's own signed-in Antigravity account and its quota. Google may restrict third-party agent use of consumer plans; this is not an official Google integration.
