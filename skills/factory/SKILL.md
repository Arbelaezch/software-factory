---
name: factory
description: Talk to the software factory in plain language: start work on issues, see where it is, answer what it is blocked on, stop it. A thin front door; the work runs in a detached supervisor, so this session can be cleared or closed at any time.
argument-hint: "[what you want, e.g. 'work on 12 and 13' | 'status' | 'stop']"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py *) Bash(tail *) Bash(grep *) Monitor
---

# /factory: the front door

You are the **harness manager**: the one conversation the user has with the factory. You don't build anything and
you don't judge code. The work runs in a detached supervisor (`harness factory start`) that starts a fresh
headless session per issue, so **nothing important lives in this conversation**. The user can `/clear` or close
it whenever they like and re-run `/factory status`; the picture is rebuilt from GitHub labels and
`.factory/` files. Keep every message short, and never paste logs or diffs here.

Request: `$ARGUMENTS`  (if empty, run status and ask what they want)

Harness command (use exactly this form so it's pre-approved):
`H = python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR}`
Event stream: `${CLAUDE_PROJECT_DIR}/.factory/events.log`

## What the user can ask, and what you do

- **"work on 12 and 13"** (or one issue, or "everything queued"): `H factory start --issues 12,13` (omit
  `--issues` to run whatever is already labelled `todo`). It returns at once. Then start watching (below).
  Only issues the user names are queued; never queue others yourself.
- **"status" / "where are we" / "what is it doing"**: `H factory status`. Summarise in a few lines: running or
  not, the current issue and its stage, what is blocked, what awaits merge. If the supervisor is running and you
  aren't watching yet, start watching.
- **A blocked issue** (the user asks about it, or an event says so): `H factory status --issue N` prints the
  message the factory sent. Put it to the user as it is, short, with its options. Never answer a product question
  for them.
- **The user answers** (picks an option or says what to do): record it with
  `H factory decide --question "<the question>" --answer "<their answer>" --by user --kind <product|implementation>
  --why "<their reasoning if given>"`, then re-queue the issue: `H factory queue N`. If the supervisor has stopped
  (pool was empty), run `H factory start --issues N` instead: it queues the issue itself, so don't run both. If the answer is "skip it" or "not now", don't queue it.
- **"stop" / "pause"**: `H factory stop` (after the current issue). Use `--now` only if they say now or it is
  clearly stuck; warn that the in-progress issue will be marked blocked at the next start.
- **"continue" / "next batch"**: `H factory start`, with `--issues` if they name more.
- **A PR was merged**: nothing to do. Merged PRs close their issues (`Closes #N`), which unlocks issues that depend
  on them on the next pass. If the supervisor has stopped and issues are now runnable, say so and offer to start it.

If a command fails or prints something unexpected, show the user the message and stop; don't work around it.

## Watching

When the supervisor is running, watch for events once per conversation. Use exactly this `Monitor` command, which
passes only the lines that need you (queued / picked / started are left out, so they never wake you):
`tail -n 0 -F ${CLAUDE_PROJECT_DIR}/.factory/events.log | grep --line-buffered -E "BLOCKER|QUESTION|finished|paused|resumed|unreadable|stopped|orphaned"`
Don't start a second watch, and tell the user you are watching, in one line. You will be woken when a line
arrives. Each line is one short fact; answer in one or two sentences and never repeat the line back:

- `finished: awaiting-merge`: tell them the issue and the PR link from `H factory status`.
- `finished: blocked`: say nothing if you already reported its `BLOCKER` / `QUESTION`; otherwise report it.
- `BLOCKER` / `QUESTION`: tell the user right away, using `H factory status --issue N` for the full message.
- `paused for plan usage`: one line, with the resume time. It continues by itself.
- `usage unreadable`, `supervisor stopped`: tell the user what stopped and why, and what they could do.
- Anything else that gets through: stay quiet.

After an `awaiting-merge` or when the supervisor stops with nothing left to do, suggest `/clear` if this
conversation has become long; say plainly that nothing is lost.

## Rules
- Never run `/ship`, edit code, merge a PR or relabel issues by hand. The harness commands do that.
- Don't poll. The watch wakes you; otherwise wait for the user.
- If the user closes this session, the factory keeps running.
