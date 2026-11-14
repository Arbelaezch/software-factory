# software-factory

## What this is
Harness: a Claude Code plugin for a multi-agent delivery pipeline (`/ship`, `/factory`, `/review-pr`,
`/harness-report`, `/harness-init`). The deterministic parts (run state, gates, review verdicts, cost
ledger, git guard, accounts, plugin installs, phone bridge) live in a stdlib-only Python CLI; the agentic
parts are markdown agent and skill files. This repo is both the plugin source and its marketplace
(`personal-harness`), and it is installed per project into other repos. See `README.md` for the full
picture and `docs/factory-design.md` for `/factory`.

## Layout
- `scripts/harness.py`: the CLI and every hook entry point (`hook guard-bash`, `subagent-stop`, ...).
  `factory_loop.py` (factory supervisor), `phone.py` (Telegram bridge), `usage.py` +
  `statusline_capture.py` (plan usage), `claude-accounts.sh` (shell wrapper, `harness-setup`).
- `scripts/defaults.toml`: plugin-wide defaults; a project's `.claude/harness.toml` overrides them.
- `agents/*.md`, `skills/*/SKILL.md`: subagents and slash commands (namespaced `harness:` when loaded).
- `hooks/hooks.json`, `.claude-plugin/plugin.json`: plugin wiring.
- `tests/`: unittest-style tests, run with pytest.

## Commands
- Tests: `python3 -m pytest -q` (the only gate).
- Check a project's setup: `python3 scripts/harness.py --root <project> doctor`.
- After merging plugin changes: `python3 scripts/harness.py plugin update`, then `plugin list`; open
  sessions need a restart to pick it up.

## Conventions and rules
- `scripts/` uses only the Python standard library (Python 3.11+); nothing to install.
- Tests must never touch real user state (`~/.claude*`, the phone setup, `gh auth`): point
  `SOFTWARE_FACTORY_HOME` / `FACTORY_PHONE_HOME` at temp dirs and mock env and subprocess calls.
- Git guard changes keep subagents unable to push or write to GitHub; test both the allowed and
  blocked case.
- Every new config key gets a default in `scripts/defaults.toml`.
- Agent and skill prose must match the `harness.py` subcommands and flags it calls; update both together.
- Hooks must be no-ops in projects without `.claude/harness.toml`.
