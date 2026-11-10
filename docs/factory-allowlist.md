# Per-project allow list for factory sessions

Factory issue sessions run headless, so any Bash command that is not pre-approved is denied
(subagents cannot prompt). The factory pre-approves a generic set in `scripts/harness.py`
(`ISSUE_ALLOWED`). Anything specific to one repo belongs in **that repo**, not here:

```toml
# <repo>/.claude/harness.toml
[factory]
allowed_tools = [
  "Bash(scripts/check-tailoring.sh*)",
  "Bash(make verify-*)",
]
```

The repo's manager owns the list and keeps it in version control. The factory only enforces
the rules below; a rejected rule is dropped and logged as `allow_rule_rejected`.

## What may be added

- **Bash rules only**, as `Bash(<command> <args>)`.
- **Repo-owned entry points**: project scripts (`scripts/foo.sh`, `bin/foo`), `make <target>`,
  a project virtualenv binary (`env/bin/python manage.py <specific_command>`), test and
  verification commands. Prefer a name that does one thing over a general tool.
- **Narrowest pattern that works**: name the exact subcommand and let `*` cover only arguments
  (`manage.py check_tailoring*`, not `manage.py *`).
- **Read-only or repo-local effects** by default. A command that spends money (real LLM or
  cloud calls) or touches services is allowed only when an issue needs it. Comment why next to it.

## What is rejected

- Chaining, pipes, redirects, substitution (`; && | > < $( )` backticks). Put the steps in a script
  and allow the script. This also means `cd dir && cmd` rules are out; make the script `cd`.
- A bare wildcard, or a wildcard command name.
- First words: `git`, `gh` (the factory's git/GitHub write policy owns them), `sudo`, `su`,
  `rm`, `rmdir`, `mv`, `dd`, `chmod`, `chown`, `kill`, `pkill`, `killall`, `ssh`, `scp`, `rsync`,
  `curl`, `wget`, `nc`, shells (`bash`, `sh`, `zsh`), `eval`, `exec`, `env`, `xargs`, `docker`,
  `kubectl`, `terraform`, `pip`, `brew`, `npm`, `npx`, and bare interpreters (`python`, `python3`,
  `node`). An interpreter would run arbitrary code, so allow a script instead, or a specific
  project-venv path with a specific subcommand.
- Non-Bash tools (Edit, Write, MCP, WebFetch).

## What stays generic (do not repeat per repo)

Read-only git and `gh issue`, `npm run`/`npm test`, the harness gates and the localhost browser
checks are already allowed for every project. If several repos need the same new command, add it
to `ISSUE_ALLOWED` in the factory instead.

## When a blocker says a command was refused

The blocker names the command. Add a rule to the repo's `harness.toml` that follows the above, commit
it, and restart the issue. If the command cannot be expressed within the rules, wrap it in a script.
