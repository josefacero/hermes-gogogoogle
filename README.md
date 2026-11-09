# gogogoogle for Hermes

**Keep Hermes in charge. Give it an Antigravity partner.**

gogogoogle lets Hermes delegate research, coding, and other tasks to Google Antigravity using your Antigravity account and quota. With Hermes approvals enabled, you approve each delegation, and an optional fresh model reviews the result before it comes back to Hermes.

For example, ask Hermes to research a question, review specific files, or make an edit in a project. Hermes keeps its own model and brings the result back into your conversation. A fresh review is a second opinion, not a guarantee that the result is correct.

## Get started

You'll need Hermes, Git, and an installed, signed-in Antigravity CLI (`agy --version` should work). In a terminal, run:

```bash
hermes plugins install josefacero/hermes-gogogoogle
hermes plugins enable gogogoogle
```

Start a new Hermes session, then try:

> Use gogogoogle to research the WHO's physical activity recommendations for adults and cite the sources.

Review the approval prompt before allowing the run. For browser setup and security details, see [Install](#install-a-person-or-an-ai-agent-can-follow-this) and [Safety controls](#safety-controls) below. Research and review can read files the current OS user can access; their rules are not an OS sandbox.

## Use it

Name the plugin in your request: "Use gogogoogle to review the changes in this repo", "Use gogogoogle to research <question>", "Use gogogoogle to write the tests in <folder>". Hermes then calls the tools below and asks you to approve each delegation first.

## Tools

- `antigravity_delegate(task, workdir, role, minutes, verify, background, resume)` - roles `do`, `research`, `review`.
  - **Verify** (default on): a new agy conversation receives up to 32,000 characters of the reply and 6,000 characters of Git diff. Sending the text does not prove the model checked every claim. The model family can match the worker. `VERIFY LIMITED` or `VERIFY INCOMPLETE` means manual review is needed; untracked and ignored file contents are not in the diff. If a verifier model is gone, out of quota, unauthorised or the service answers 503, the next one in the chain runs.
  - **Git safety net:** the plugin fingerprints the working tree before and after. The reply says whether files changed. If a `research` or `review` run changed anything, it returns a `READ-ONLY VIOLATION` warning and the verdict becomes `VERIFY FAIL`, instead of trusting the prompt. The plugin points agy's browser snapshots at a temp folder; if an older setup still leaves a `.playwright-mcp/` folder in `workdir`, the check ignores it and the result tells you to delete it or add it to `.gitignore`. A folder that is not a git repo is flagged as untracked.
  - **Background:** `background=true` returns a job id at once; poll with `antigravity_job` for the plugin-observed worker or verifier phase and elapsed time. It does not notify the chat automatically. A completed run includes `total_seconds`; the older `seconds` and stats average measure the worker phase, not total time.
  - **Resume:** a timed-out run returns its conversation id when agy reported one; pass it as `resume` to continue. On Windows a timeout also stops agy's child processes (browser, shell).
- `antigravity_job(id)` - status or full result of a background run.
- `antigravity_stats` - runs, status counts, failures by reason (`quota`, `model`, `auth`, `busy`, `other`), average time, tokens handled by agy instead of your lead model.
- Research asks the worker to open primary sources, cite non-obvious claims, separate observations from inference, and list unsupported claims and inaccessible sources as unverified. It suggests FxTwitter for public X status links and public pages for Instagram and TikTok. These routes are best effort; research cannot run `yt-dlp` or other shell commands under the session policy. Never extract cookies or log in automatically. Web access depends on agy's own tools and settings; pages it cannot open belong in Unverified. The lead must check sources before acting.
- Consecutively numbered research requests get a local status-tag check: it reports items missing a `DONE`, `MISSING`, or `UNVERIFIED` line. This is only a coverage hint, not proof that the work was done or the citations are valid.

Hermes asks for approval before each delegation through its approval system. The prompt lists role, workdir and options first, then the task with its length and SHA-256 prefix. Hermes' CLI approval panel shows a limited number of description lines and marks the rest as truncated; if the task is cut, deny and ask for a shorter task. Each request uses a fresh key, so session or permanent approval of one request does not preapprove the next; choose once. YOLO, disabled approvals, and gateway or cron policies can bypass prompts. Do not enable automatic approval if a human must approve each call. Hermes can allow another plugin's pre-tool hook to change arguments after this plugin builds its approval message; use only trusted plugins and inspect their hooks. A failure in Hermes' outer hook dispatcher can also bypass hooks. Approval messages may appear in Hermes logs.

Token counters cover worker usage reported by agy; they do not measure verifier costs or prove a net saving.

## Safety controls

- **You approve every run.** Hermes shows the role, workdir and task and waits for your answer. Deny stops it before agy starts.
- **`research` and `review` are the cautious roles.** They run agy without `--dangerously-skip-permissions` and pass session-only rules that deny edits, shell commands, every MCP server in your agy config except `playwright`, and the Playwright tools that run code, type or upload files (`browser_run_code_unsafe`, `browser_evaluate`, `browser_file_upload`, `browser_fill_form`, `browser_type`, `browser_press_key`, `browser_drag`, `browser_drop`, `browser_select_option`, `browser_handle_dialog`). The verifier gets the same rules. These are rules, not a sandbox.
- **`--dangerously-skip-permissions` is used only by `do`.** In a `do` run agy approves its own file edits and shell commands without asking, so it can change or delete files in `workdir`. It is picked per request, after your Hermes approval, and there is no setting that turns it off for `do`; choose `research` or `review` when you do not want it. Keep the work in a git repo with a clean tree so git is the undo.
- **The browser (agy's Playwright server) is on for `research`, `review` and the verifier, headless by default.** Tested on agy 1.5.2 (Windows, a few runs): pages open with no window, and the browser's snapshot files go to a temp folder instead of `workdir`. The plugin also asks for an in-memory browser profile with no saved logins (`PLAYWRIGHT_MCP_ISOLATED=1`); that the profile really stays empty was not checked. Safe browser tools (navigate, snapshot, screenshot, click, tabs and similar) get explicit allow rules, because agy's headless mode refuses an MCP tool that has no rule and then often ends the run with no reply. Set `browser_mode` to `visible` to watch it (a screen recording, say) or `off` to give the read-only roles no MCP tools at all. Tools you have not listed as unsafe stay allowed, so a Playwright release that adds a tool can add a capability until the list is updated. Some sites block headless browsers; the run then lists those pages under Unverified. Page text is untrusted: a page can contain instructions aimed at the agent, which is why code-running and typing tools are denied.
- **`do` skips agy's permission checks,** so it can use any MCP tool, edit files and run commands. Use it only for work you would let agy do unattended.
- **Keep folders off limits with `blocked_roots`.** A request whose `workdir` is inside or contains a listed folder is refused, and a drive root or your home folder is always refused. In your Hermes `config.yaml`:

  ```yaml
  plugins:
    entries:
      gogogoogle:
        settings:
          blocked_roots:
            - D:/Private
            - /home/me/clients
  ```

  The `GOGOGOOGLE_BLOCKED_ROOTS` environment variable (path-separator list) works as a fallback. Credential folders in your home folder (`.ssh`, `.aws`, `.gnupg`, `.azure`, `.kube`, `.docker`, `.config/gcloud`, `.gemini`) are always refused, with or without this setting. This only stops the plugin from starting in those folders. It does not stop agy from reading a file by absolute path, so use OS permissions or a separate user for a hard boundary.

## Going faster (your risk)

- **Start long jobs with `background=true`** and poll with `antigravity_job`; one job per independent question.
- **Skip the verifier on mechanical work** (`verify=false`) such as renames or file generation you will diff yourself. Keep it on for research you will act on.
- **Keep tasks short and numbered** (3 to 8 items). Dropped items, not length, are the usual failure.
- **`do` already uses `--dangerously-skip-permissions`** after your Hermes approval, which is what lets it edit and run commands without stopping. The plugin does not add it to the read-only roles and has no setting for that: use `do` for work you would let agy do unattended, in a git repo with a clean tree, and keep personal and client folders in `blocked_roots`. Blocked folders stay refused even for `do`, but agy can still read other paths by absolute path.

## Why not just switch the model or spawn a subagent?

- **Your lead model keeps the judgment.** Switching Hermes to a Gemini model replaces the thing deciding what to do. Here Hermes stays on its model and sends only the bulk step out.
- **Separate quota.** agy bills your Antigravity plan. A subagent on the same provider spends the same pool as the lead.
- **agy is a full agent**, with its own file tools and context. A subagent shares your provider's limits; agy does not.
- **Fresh check.** The verifier is a fresh conversation; when fallback reaches the worker's model family, it may share its blind spots.
- **Visible failure reasons.** Dead model names (they change often) and empty quota are told apart from real errors.

If you already have a Gemini API key and only want another model, Hermes delegation does that natively. This plugin is for the Antigravity plan and the verify step.

## Disclosures

- **Runs shell commands:** `do` launches `agy` with `--dangerously-skip-permissions` after the Hermes approval gate; `research`, `review`, and verifier passes omit the flag and request session-only rules to allow project reads and deny edits and shell commands. Enforcement depends on agy's settings and version; these rules are not an OS sandbox. In testing the rules blocked writes and shell commands but not reads outside `workdir`. agy still reports success after a blocked action, so the plugin appends a note listing the blocked tools to the result. On agy 1.5.2 the read-only roles could call every MCP server in the user's agy config until the plugin added `mcp(<server>/*)` deny rules (one run each; read from `agy mcp list`; if that list cannot be read, all MCP tools are denied). agy's Playwright browser stays available to those roles except the tools listed above; its pages are fetched from the internet and from your own network (for example `localhost`) as the OS user, because no origin filter is set. agy can access files outside `workdir`. Git detection can miss ignored files and modifications inside existing untracked files, and does not revert changes.
- **Sends data to a third party:** your task text, files agy reads and up to 32,000 characters of the worker reply plus 6,000 characters of Git diff go to the verifier through Google agy.
- **Reads outside its own data:** agy can access files as the current OS user. The plugin refuses a drive root, your home folder and paths inside or containing a blocked root. Set roots with the `blocked_roots` plugin setting (a list of paths) or, as a fallback, the `GOGOGOOGLE_BLOCKED_ROOTS` environment variable (path-separator list). Use OS isolation for a hard boundary.
- **Runs `git`:** `git diff`, `git status`, `git ls-files` in `workdir`. The diff (up to 6,000 characters) goes to the verifier model. agy and git receive a limited environment without inherited provider API keys, but can still access files permitted to the OS user.
- **Writes:** `runs.jsonl` (time, role, model, status, seconds, token counts, failure class; no task text) and `jobs/<id>.json` (background results) in the plugin data directory. After a restart, a running job's status is unknown; a subprocess may still be running.
- **No credentials stored, no telemetry, no self-update.**

## Install (a person or an AI agent can follow this)

Run each step and check its result before the next one.

1. **Antigravity CLI.** `agy --version` prints a version (tested on 1.5.2). If the command is missing, install Antigravity and run `agy` once to sign in.
2. **Plugin.** `hermes plugins install josefacero/hermes-gogogoogle` then `hermes plugins enable gogogoogle`. Check: `hermes plugins list` shows `gogogoogle` enabled. Start a new Hermes session (restart the gateway if you use one).
3. **Browser, optional but recommended for research.** `agy mcp list` should show a server named exactly `playwright`. If it does not, add it (flags go before the name): `agy mcp add playwright npx -y @playwright/mcp@0.0.84`. It needs Node.js (`npx`) and Google Chrome. Without it, research still works through agy's web search and page-reading tools.
4. **Protect your folders.** Add anything private to `blocked_roots` (see below). Credential folders in your home folder are blocked already.
5. **Try it.** Ask Hermes: "Use gogogoogle to research <a question that needs a web page>". Hermes asks you to approve; allow once. The reply should end with a verifier line (`VERIFY PASS`, `LIMITED`, `INCOMPLETE` or `SKIPPED`) and no browser window should appear.

When a model reports `Resets in 106h27m`, the plugin remembers that reset time and skips the model in later verifier chains until then. `antigravity_stats` shows `quota_cooldown_until`.

## Use from Claude Code or Codex (experimental)

`gogogoogle_mcp.py` is a stdio MCP server over the same delegation engine, so a Claude Code or Codex session can hand work to `agy` without Hermes. It needs the optional `mcp` Python package (`pip install mcp`); the Hermes plugin itself stays standard-library only. The skill in `skills/gogogoogle/SKILL.md` tells the agent how to use it.

**The operator sets the policy, not the agent.** The server starts only with all of these environment variables, and the agent cannot change them:

| Variable | Meaning |
|---|---|
| `GOGOGOOGLE_MCP_TRUSTED=1` | You accept that every client of this server may delegate within the policy below. |
| `GOGOGOOGLE_MCP_HOST` | A label shown in each request (for example `my-laptop`). |
| `GOGOGOOGLE_MCP_ALLOWED_ROOTS` | A JSON array of existing absolute folders; work outside them is refused. |
| `GOGOGOOGLE_MCP_ALLOW_DO=1` | Optional. Without it only `research` and `review` run. With it, `do` runs agy with permission prompts skipped. |

Delegating takes two calls: `prepare` returns the exact request and a single-use token (valid 2 minutes), `delegate(token)` runs it. **This is not Hermes' approval.** Hermes asks a person before each call; here the person is whoever answers the host's tool prompt, and a host set to auto-approve skips that. Browser tools for agy are off on this path.

Claude Code (user scope):

```bash
claude mcp add gogogoogle --scope user -e GOGOGOOGLE_MCP_TRUSTED=1 -e GOGOGOOGLE_MCP_HOST=my-laptop -e 'GOGOGOOGLE_MCP_ALLOWED_ROOTS=["/home/me/projects"]' -- python /path/to/hermes-gogogoogle/gogogoogle_mcp.py
```

Codex (`~/.codex/config.toml`), not yet tested with a real Codex session:

```toml
[mcp_servers.gogogoogle]
command = "python"
args = ["/path/to/hermes-gogogoogle/gogogoogle_mcp.py"]

[mcp_servers.gogogoogle.env]
GOGOGOOGLE_MCP_TRUSTED = "1"
GOGOGOOGLE_MCP_HOST = "my-laptop"
GOGOGOOGLE_MCP_ALLOWED_ROOTS = '["/home/me/projects"]'
```

What has been tested: `test_mcp_e2e.py` drives the server with a real MCP client against a fake `agy` (no quota), and a Claude Code session ran `prepare` then `delegate` against it. What has not: Codex or other hosts, a real `agy` run through this path, Docker, and remote workers. The SSH files (`remote_runner.py`, `remote_transport.py`) are prototypes, not connected to Hermes or MCP.

## Setup

Optional plugin settings under `plugins.entries.gogogoogle.settings` are `worker_model` (string), `verifier_models` (list of strings), `blocked_roots` (list of paths) and `browser_mode` (`headless` by default, `visible` or `off`). Model names come from `agy models`. The environment variables `ANTIGRAVITY_MODEL`, `ANTIGRAVITY_VERIFIER_MODELS`, `GOGOGOOGLE_BLOCKED_ROOTS` and `GOGOGOOGLE_BROWSER` remain fallback values when these settings are absent.
