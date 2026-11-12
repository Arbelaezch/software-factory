# Context cost and CLAUDE.md

Measured 2026/11/11 from `harness report --tools` on three projects with run data: court-vision (no
`CLAUDE.md`, 8 runs), callback (4.7 KB `CLAUDE.md`, 25 runs) and uasu (8.5 KB, 13 runs; only a few short harness
runs, so not typical). Token figures from transcripts are estimates (4 characters per token).

## What the audit found

- **Auto-load cost.** Subagents start at about 6.3k tokens without a `CLAUDE.md` (court-vision) and 8.9k with
  uasu's 8.5 KB one, so about 2.6k tokens per request. Over a whole project that is roughly 2-5% of context tokens
  in uasu and 4-8% in callback (estimated from requests x 1.2-2.6k tokens).
- **The manager pays it most.** A `/ship` manager session starts at 27-54k tokens, of which the project
  instructions are 1-2k plus 1.6k of nested files (uasu). Tool definitions (6-8k) and the skill listing (4-8k)
  come from the user's plugins and MCP servers, not from the harness.
- **Agents reading it again.** Seven agent prompts tell the agent to read `CLAUDE.md`. In the transcripts 10-50%
  of sessions did, at a carried cost well under 1% of context. Nested `CLAUDE.md` files are only loaded when
  the agent touches that folder, so the instruction still has a job there.
- **`omitClaudeMd: true` works on plugin subagents** (Claude Code 2.1.311). A probe agent without it started at
  5.8k tokens, with it at 2.2k, using uasu's `CLAUDE.md`: about 3.6k less per request.

## Decision per agent

No agent gets `omitClaudeMd` for now. The saving is under 1% of context for any single agent and a missed rule is
hard to notice.

| Agent | CLAUDE.md in context | Why |
|---|---|---|
| planner, engineer, claude-reviewer, ci-fixer, conflict-resolver | keep | They apply the project's rules. |
| scout | keep | Its report hands the conventions and test setup to the planner. |
| verifier | keep | It judges findings against the code and the project's rules. Best candidate if this is revisited. |
| factory-planner, factory-discoverer, mvp-reviewer | keep | They read the project documents and compare them with the code. |
| issue-selector, human-liaison, question-resolver, harness-cleanup | keep | Few calls; the saving is negligible. Unconfirmed that the field applies to `--agent` main sessions. |

Rules reaching engineers are unchanged: the git guard and gates are enforced by hooks, and the engineer prompt
still says to follow every `CLAUDE.md` rule that applies.

## What changed

- The `/harness-init` template drops its comment and the "Harness" section, which only helped humans, and says to
  keep to what the code doesn't show. The skill no longer asks for a "Harness" section in existing files.

## Where the larger costs are

`CLAUDE.md` is not the main cost. In court-vision, tool results were 21% of context tokens (almost all `Read` and
`Bash`) and the manager session was about 40%. Candidates for later work: the manager's wake-ups and startup size,
and putting the files agents re-read in the plan. Compaction at a lower threshold is unlikely to help; test it last.
