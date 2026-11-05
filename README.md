# gogogoogle for Hermes

Hand bulk work to Google Antigravity's `agy` CLI as a worker, then have a fresh second model check the result.

## Tools

- `antigravity_delegate(task, workdir, role, minutes, verify, background, resume)` - roles `do`, `research`, `review`.
  - **Verify** (default on): a new agy conversation checks the report and a limited Git diff. The model family can match the worker. `VERIFY LIMITED` or `VERIFY INCOMPLETE` means manual review is needed; untracked and ignored file contents are not in the diff. If a verifier model is gone, out of quota or unauthorised, the next one in the chain runs.
  - **Git safety net:** the plugin fingerprints the working tree before and after. The reply says whether files changed. If a `research` or `review` run changed anything, it returns a `READ-ONLY VIOLATION` warning instead of trusting the prompt. A folder that is not a git repo is flagged as untracked.
  - **Background:** `background=true` returns a job id at once; poll with `antigravity_job`.
  - **Resume:** a timed-out run returns its conversation id; pass it as `resume` to continue.
- `antigravity_job(id)` - status or full result of a background run.
- `antigravity_stats` - runs, status counts, failures by reason (`quota`, `model`, `auth`, `other`), average time, tokens handled by agy instead of your lead model.
- Research prompts ask for sourced facts, label uncertain claims, and suggest FxTwitter for public X status links plus public pages for Instagram and TikTok. These are best-effort routes; research cannot run `yt-dlp` or other shell commands under the session policy. Never extract cookies or log in automatically.
- `jev_public_check(public_text, user_approved)` - currently disabled and sends no data. A model-supplied approval flag is not a privacy consent gate.

Hermes asks for approval before each delegation through its approval system. Each request uses a fresh key, so session or permanent approval of one request does not preapprove the next. YOLO, disabled approvals, and gateway or cron policies can bypass prompts. Do not enable automatic approval if a human must approve each call. Hermes can allow another plugin's pre-tool hook to change arguments after this plugin builds its approval message; use only trusted plugins and inspect their hooks. A failure in Hermes' outer hook dispatcher can also bypass hooks. Approval messages may appear in Hermes logs.

Token counters cover worker usage reported by agy; they do not measure verifier costs or prove a net saving.

## Why not just switch the model or spawn a subagent?

- **Your lead model keeps the judgment.** Switching Hermes to a Gemini model replaces the thing deciding what to do. Here Hermes stays on its model and sends only the bulk step out.
- **Separate quota.** agy bills your Antigravity plan. A subagent on the same provider spends the same pool as the lead.
- **agy is a full agent**, with its own file tools and context. A subagent shares your provider's limits; agy does not.
- **Fresh check.** The verifier is a fresh conversation; when fallback reaches the worker's model family, it may share its blind spots.
- **Visible failure reasons.** Dead model names (they change often) and empty quota are told apart from real errors.

If you already have a Gemini API key and only want another model, Hermes delegation does that natively. This plugin is for the Antigravity plan and the verify step.

## Disclosures

- **Runs shell commands:** `do` launches `agy` with `--dangerously-skip-permissions` after the Hermes approval gate; `research`, `review`, and verifier passes omit the flag and request session-only rules to allow project reads and deny edits and shell commands. Enforcement depends on agy's settings and version; these rules are not an OS sandbox. agy can access files outside `workdir`. Git detection can miss ignored files and modifications inside existing untracked files, and does not revert changes.
- **Sends data to a third party:** your task text, files agy reads and verifier excerpts go to Google through agy.
- **Jev:** disabled until a privacy consent mechanism is independently enforced; it sends no data.
- **Reads outside its own data:** agy can access files as the current OS user. The plugin refuses a drive root, your home folder and paths inside or containing `GOGOGOOGLE_BLOCKED_ROOTS` (path-separator list). Use OS isolation for a hard boundary.
- **Runs `git`:** `git diff`, `git status`, `git ls-files` in `workdir`. The diff (up to 6,000 characters) goes to the verifier model. agy and git receive a limited environment without inherited provider API keys, but can still access files permitted to the OS user.
- **Writes:** `runs.jsonl` (time, role, model, status, seconds, token counts, failure class; no task text) and `jobs/<id>.json` (background results) in the plugin data directory. After a restart, a running job's status is unknown; a subprocess may still be running.
- **No credentials stored, no telemetry, no self-update.**

## Install

```
hermes plugins install josefacero/hermes-gogogoogle
hermes plugins enable gogogoogle
```

When a model reports `Resets in 106h27m`, the plugin remembers that reset time and skips the model in later verifier chains until then. `antigravity_stats` shows `quota_cooldown_until`.

## Setup

Install Antigravity and sign in (`agy`). Optional plugin settings under `plugins.entries.gogogoogle.settings` are `worker_model` (string), `verifier_models` (list of strings), and `blocked_roots` (list of paths). Model names come from `agy models`. The previous environment variables remain fallback values when these settings are absent.
