# gogogoogle for Hermes

Hand bulk work to Google Antigravity's `agy` CLI as a worker, then have a fresh second model check the result.

## Tools

- `antigravity_delegate(task, workdir, role, minutes, verify, background, resume)` - roles `do`, `research`, `review`.
  - **Verify** (default on): a new agy conversation on a different model checks the claims against the real files **and the actual git diff**, and the reply ends with `VERIFY PASS` or `VERIFY FAIL`. If a verifier model is gone, out of quota or unauthorised, the next one in the chain runs.
  - **Git safety net:** the plugin fingerprints the working tree before and after. The reply says whether files changed. If a `research` or `review` run changed anything, it returns a `READ-ONLY VIOLATION` warning instead of trusting the prompt. A folder that is not a git repo is flagged as untracked.
  - **Background:** `background=true` returns a job id at once; poll with `antigravity_job`.
  - **Resume:** a timed-out run returns its conversation id; pass it as `resume` to continue.
- `antigravity_job(id)` - status or full result of a background run.
- `antigravity_stats` - runs, status counts, failures by reason (`quota`, `model`, `auth`, `other`), average time, tokens handled by agy instead of your lead model.

## Why not just switch the model or spawn a subagent?

- **Your lead model keeps the judgment.** Switching Hermes to a Gemini model replaces the thing deciding what to do. Here Hermes stays on its model and sends only the bulk step out.
- **Separate quota.** agy bills your Antigravity plan. A subagent on the same provider spends the same pool as the lead.
- **agy is a full agent**, with its own file tools and context. A subagent shares your provider's limits; agy does not.
- **Independent check.** The verifier is a fresh conversation on another model family, so the worker does not mark its own homework.
- **Visible failure reasons.** Dead model names (they change often) and empty quota are told apart from real errors.

If you already have a Gemini API key and only want another model, Hermes delegation does that natively. This plugin is for the Antigravity plan and the verify step.

## Disclosures

- **Runs shell commands:** launches `agy` with `--dangerously-skip-permissions`. agy can edit and delete files in `workdir`. Use git as the undo. `research` and `review` roles are only *told* not to edit; the plugin detects a change after the fact (git repos only) and warns, but does not revert it.
- **Sends data to a third party:** your task text, files agy reads and verifier excerpts go to Google through agy.
- **Reads outside its own data:** agy reads inside `workdir`. The plugin refuses a drive root, your home folder and any path under `GOGOGOOGLE_BLOCKED_ROOTS` (path-separator list). Put folders you never want sent to a cloud model there.
- **Runs `git`:** read-only `git diff`, `git status`, `git ls-files` in `workdir`. The diff (up to 6,000 characters) goes to the verifier model.
- **Writes:** `runs.jsonl` (time, role, model, status, seconds, token counts, failure class; no task text) and `jobs/<id>.json` (background results) in the plugin data directory. Background jobs end if Hermes restarts.
- **No credentials stored, no telemetry, no self-update.**

## Setup

Install Antigravity and sign in (`agy`). Optional environment: `ANTIGRAVITY_MODEL` (worker), `ANTIGRAVITY_VERIFIER_MODELS` (comma list), `GOGOGOOGLE_BLOCKED_ROOTS`. Model names come from `agy models`.
