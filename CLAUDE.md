# hermes-gogogoogle

Hermes Agent plugin: delegates bulk work to Google Antigravity (`agy`) as a worker, then verifies it with a fresh model.
Public repo `josefacero/hermes-gogogoogle`. Trigger word: "Use gogogoogle to ...".

## Layout
- `plugin.yaml` - manifest. `__init__.py` - the whole plugin (tools, agy runner, verifier chain, git guard, jobs, quota cooldown).
- `README.md` - user docs and the rule-13 disclosures. Keep the disclosures true when behaviour changes.

## Rules
- Public repo: no secrets, no law-folder names, no private paths. Stage files by name.
- The Hermes plugin (`__init__.py`) is standard library only. Python 3.11+. `gogogoogle_mcp.py` (the optional MCP server for Claude Code and Codex) needs the optional `mcp` package; nothing in `__init__.py` may import it.
- Check before any push: `hermes plugins validate .` and `python -m unittest discover` (the MCP e2e tests skip without the `mcp` package, so say how many ran).
- Catalog entry: PR `NousResearch/hermes-agent#149174` pins an exact commit. Any code change after the pin means a new commit,
  a new `sha` + `version` in `plugin-catalog/gogogoogle.yaml` on the PR branch (`josefacero/hermes-agent`, branch
  `add-gogogoogle-plugin`), and a bump of `version` in `plugin.yaml`.
- `do` runs `agy` with permissions auto-approved after the Hermes approval gate; test in a throwaway git repo. Research, review and verifier omit the skip flag, but are not sandboxed. Real runs use Antigravity quota.

## State
Project state and next steps are kept by the maintainer outside this repo.
