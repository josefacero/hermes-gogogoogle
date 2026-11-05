# hermes-gogogoogle

Hermes Agent plugin: delegates bulk work to Google Antigravity (`agy`) as a worker, then verifies it with a fresh model.
Public repo `josefacero/hermes-gogogoogle`. Sibling (private, different code): `C:\PROJECTS\gogogoogle\`.

## Layout
- `plugin.yaml` - manifest. `__init__.py` - the whole plugin (tools, agy runner, verifier chain, git guard, jobs, quota cooldown).
- `README.md` - user docs and the rule-13 disclosures. Keep the disclosures true when behaviour changes.

## Rules
- Public repo: no secrets, no law-folder names, no private paths. Stage files by name.
- Standard library only. Python 3.11: `"C:\Users\My PC\AppData\Local\Programs\Python\Python311\python.exe"`.
- Check before any push: `"C:\Users\My PC\AppData\Local\hermes\bin\hermes.exe" plugins validate .` (Victus: its own Hermes path).
- Catalog entry: PR `NousResearch/hermes-agent#149174` pins an exact commit. Any code change after the pin means a new commit,
  a new `sha` + `version` in `plugin-catalog/gogogoogle.yaml` on the PR branch (`josefacero/hermes-agent`, branch
  `add-gogogoogle-plugin`), and a bump of `version` in `plugin.yaml`.
- `agy` runs auto-approved: test in a throwaway git repo, never in a real project. Real runs use Antigravity quota.

## State
Current project state and next steps: vault `06 Handoffs\CURRENT\ai-orchestration.md` (private).
