---
name: ship
description: Run the harness delivery pipeline for a GitHub issue or task (optional grill, scout, plan, approve, implement, cross-lab review loop, PR description drafts).
argument-hint: "[--tier tiny|standard|large | --quick] [--grill|--no-grill] <issue URL | repo#number | task description>"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py *) Bash(git status *) Bash(git diff *) Bash(git log *)
---

# /ship: delivery pipeline

You are the **manager**. You coordinate subagents and harness commands; you do not write or review
code yourself. Protect your own context: pass file paths between stages, never paste diffs, plans or
logs into this conversation, and don't read source files. Read only the short outputs of harness
commands and subagent replies.

Arguments: `$ARGUMENTS`
- `--tier tiny|standard|large`: force the size tier (see below). Without it, you pick one from the task.
- `--quick`: alias for `--tier tiny`.
- `--grill` / `--no-grill`: force the grill on or off. Otherwise ask.
- Everything else is the task: a GitHub issue (URL, or `<repo>#<n>` where repo is the harness name, the
  folder name or the GitHub name) optionally followed by extra instructions, or a plain task
  description. If it's empty, ask the user for the task and stop. In a single-repo project a bare
  `#<n>` works (pass `--repo <name>`); with several repos, ask which one.

## Size tiers
Choose the tier from the task before `run start`, and say which one in a single line:
- **tiny**: one repo, about 2 files or fewer, well specified (a typo, a config value, a small bug with a clear
  repro). No grill, no scouts (the planner reads the files itself), planner on Sonnet, a single task, one
  review round. If the task is vague, spans repos or touches auth, data, migrations or concurrency, it isn't tiny.
- **standard**: the full flow below. The default when unsure.
- **large**: the full flow; the phase splitting in step 3 applies above 6 tasks.
Every tier keeps the plan approval checkpoint (step 4) and the same guard rules. If a tiny run's planner says
the task is bigger than tiny (more than one task, more than 2 files, another repo), run
`H run tier standard --reason "<why>"`, scout the repos now, and resume the planner with the scout context.

Harness command (use exactly this form so it's pre-approved):
`H = python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR}`

Give every subagent, in its delegation message: the run directory, the harness command (the `harness`
value printed by `run start`), the `git_mode` printed by `run start`, and the specific job. If
`run start` prints `model_overrides` (a tiny run adds `planner: sonnet`), pass that model when spawning the matching agent.

## 0. Start the run
- Issue: `H run start --issue <ref> [--task "<extra instructions>"] [--tier <tier>]`. It reads the issue
  (title, body, labels, comments) into `task.md` via `gh`.
- Plain task: `H run start --task "<task>" [--tier <tier>]`.
Note `run_dir`, `git_mode` and, for issues, `suggested_branch_type` from the output. If it prints
`"profile": "legacy"`, the project is a legacy codebase: the legacy notes in steps 1, 2 and 4 apply, and the scouts,
planner, engineers and reviewers pick up `<run_dir>/profile.md` on their own. If another run is
active, ask the user whether to end it (`H run end --status aborted`) or stop. If `gh` fails, tell the
user to run `gh auth login`.

## 1. Grill (optional, interactive)
Skip when the tier is tiny or `--no-grill`. Otherwise, unless `--grill` was given, ask with AskUserQuestion:
"Grill this task before planning?" options: "Yes, grill me (Recommended for vague or risky tasks)",
"No, go straight to planning".

If yes: if a `grill-with-docs` or `grill-me` skill is available, invoke it on the task (prefer
`grill-with-docs`). Otherwise interview the user yourself: one question at a time, each with your
recommended answer, covering scope, users/roles affected, edge cases, error states, API/contract
shape, data/migrations, and what "done" looks like. Stop when the decisions are clear or the user
says enough.

**Legacy profile:** the grill always runs (skip it only for the tiny tier or `--no-grill`); don't ask whether to
grill, and interview the user yourself rather than with a grill skill. First read `profile_file`, then the decision
documents it lists, only the parts that bear on the task (look at headings, read the sections that apply). Ask only
what those documents and the task don't already answer, one question at a time, each with your recommended answer:
- product behaviour: who sees what, roles, edge cases, error states;
- important implementation trade-offs: data model shape, a migration vs. reusing an existing field, where logic lives
  (shared module vs. one view), contract changes and their consumers, backwards compatibility, performance vs.
  correctness, security and scoping choices.
Never ask about routine choices (naming, file layout, helpers, test structure): those follow the project's
conventions and a fixed bar of correct, secure (permissions and scoping explicit), tested and readable.

Then write `<run_dir>/brief.md`: decisions made, explicit non-goals, acceptance criteria. Even
without a grill, if the user gave details beyond the one-line task, record them in brief.md.

## 2. Scout
Using the workspace CLAUDE.md and the repos in `.claude/harness.toml`, decide which repos the task likely
touches (in a single-repo project, just that one). Spawn one
`harness:scout` subagent per relevant repo **in parallel**, each told to write `<run_dir>/context-<repo>.md`.
When unsure whether a repo is involved, include it; scouts are cheap. Legacy profile: tell each scout to read
`<run_dir>/profile.md`. **Tiny tier: skip the scouts** (the
planner reads the files itself).

API contracts (every tier): first run `H contracts map`. If it prints "No repo has [contracts]", skip this. Otherwise it
rebuilds a stale contract map and prints each map's path: give those paths to every scout and to the planner. If a map
`failed`, relay the reason to the user and continue without it (the snapshot gate still runs).

## 3. Plan
Spawn the `harness:planner` subagent with the run directory. It writes `plan.md` and `tasks.json`.
Then run `H validate plan`. If it prints `INVALID`, resume the planner once (SendMessage) with that message and
rerun; if it is still invalid, show the user the message and ask how to proceed.
If its reply says the job exceeds 6 tasks, show the user the proposed phases and ask (AskUserQuestion)
whether to plan phase 1 only (Recommended) or keep everything in one run.

## 4. Checkpoint (always)
Show the user the planner's short task list and any open questions (from its reply, not the file). Legacy profile:
also show each departure from local convention the planner listed (or say there are none), so the user approves them
with the plan.
Ask with AskUserQuestion: "Approve the plan?" options: "Approve", "Change something", "Abort".
- Change: collect the feedback, resume the planner (SendMessage) with it, then ask again. After two
  rounds of changes, spawn a fresh planner told to read the existing plan.md plus the accumulated
  feedback, instead of resuming (a resumed planner's context keeps growing).
- Abort: `H run end --status aborted` and stop.
The user can open `<run_dir>/plan.md` to read it in full; mention the path.

## 5. Branch
Read `repos` from `<run_dir>/tasks.json` (small file, fine to read). Run
`H branch --repos <comma-separated repos> [--type <type>]`. It fetches origin and creates the branch
from a fresh `origin/<base branch>` with the configured naming, e.g. `feat/193-add-export` (type
defaults to `suggested_branch_type`; use `--type fix`, `chore` or `hotfix` when the issue clearly calls for
it; the only allowed types are `feat`, `fix`, `chore`, `hotfix`). The user's own branches and their local base branch are never touched.
- If a repo has uncommitted tracked changes, tell the user and ask whether to carry them over
  (`--allow-dirty`) or stop so they can commit/stash first. Never stash or reset their work yourself.
- If it reports DEPENDENCIES CHANGED, ask the user to reinstall (e.g. `npm install`, `pip install -r
  requirements.txt`) and wait for them to confirm.
- If it couldn't fetch, relay the warning (it falls back to the last-fetched remote branch).

Then, **before any engineer edits anything**, run `H baseline --repos <same repos>`. It records the
failures that already exist (some suites aren't green today), so gates later fail only on new
failures, and takes the pre-edit API contract snapshot for repos with `[contracts]`. It can take a few minutes; tell the user it's running. Run it in the background if your
tools allow, but wait for it to finish before step 6.

## 6. Implement
For each task in `tasks.json`, in dependency order (independent tasks in different repos may run in
parallel): spawn a `harness:engineer` with the task id, passing `model: opus` when the task's `model` is
"opus". The engineer runs the gates itself.
- If it reports GATES FAIL after its retries: resume it once with SendMessage asking it to try a
  different approach. If it still fails, ask the user whether to continue, skip the task, or abort.
- If it reports the plan is wrong, resume the planner with the engineer's reason, then continue.
- If it reports `NEEDS USER: ...`, relay what and why to the user, ask how to proceed, and stop that
  task until they answer.
- Engineers run at the effort set in their agent file (fix rounds included); the `model: opus`
  override only changes the model.

After all tasks: run `H gates` (all changed repos). If anything fails, send the failing output's log
path to an engineer to fix, then rerun. After two such fix attempts, ask the user instead of looping.

## 7. Review loop
For round n = 1, 2, ...:
1. `H review --round n`. If it prints `FALLBACK` (also when the outside model's output was unparseable),
   spawn the `harness:claude-reviewer` subagent for round n with the `model` that the FALLBACK output names
   ("Spawn it with model: ..."). The harness picks it: Sonnet for rounds 2+ and for a tiny run whose diff shows
   no risk marker, Opus otherwise, and records it in `run.json` for `harness report`.
2. Spawn the `harness:verifier` subagent for round n. It writes `verified-<n>.json`.
3. `H verdict --round n`. If it prints `INVALID` about `review-<n>.json`, do what it says (an outside
   model's file: spawn the `harness:claude-reviewer` for round n; the fallback reviewer's own file: resume it once) and rerun. If it prints `INVALID` about `verified-<n>.json`, resume the verifier once (SendMessage) with that message,
   then rerun (the message may also ask for judgements on flagged test changes, listed in `tamper.json`). Otherwise
   it prints `DECISION: DONE | FIX | ESCALATE`. A flagged test change the verifier calls `tampered` goes to the engineer
   like any confirmed finding; if you, the user, accept a flagged change, record it with `H tamper clear <id> --reason "..."`.
   - DONE: go to step 8.
   - FIX: spawn a `harness:engineer` with `<run_dir>/fix-<n>.json`. Then `H gates`; continue with round n+1.
   - ESCALATE: tell the user how many confirmed and uncertain-blocking findings remain and where
     (`final-<n>.json`). Uncertain-blocking ones are unproven, so the engineer can't fix them; the
     verifier's reason says what would settle each. Ask:
     "Run another round", "Stop here; I'll handle the rest". Respect the answer.

## 8. Finish
1. `H summary` and show the user its output (it's short). It also writes a PR description draft per
   repo (`<run_dir>/pr-<repo>.md`, with `Closes #<issue>` and a "Related PRs" section when the
   change spans repos, which `/review-pr` uses to pair PRs). Point the user to them.
2. `H run end --status done` (or `escalated` if you stopped with open blocking findings).
3. Tell the user, in a few lines: which branches hold the changes, that nothing is committed (unless
   git mode is "commit"), open should_fix/nit items worth a look, and the run's cost line. On the personal
   account, offer to commit, push and open the PR once they've reviewed; don't do it unprompted.

## Rules
- Subagents never commit (unless git mode is "commit"), push, switch branches or stash, and never write to
  GitHub; the guard hook enforces it.
- You (the main session) may switch branches and stash. Don't reset or rewrite history.
- Commit only after the user has reviewed the changes and says to. On the personal GitHub account
  (`[github] write_accounts`), "commit" also means push. Open or edit a PR only when the user asks.
- On any other account (e.g. work) never commit or push; the guard blocks it. You may still create a PR or
  issue when asked.
- If a command is blocked, don't look for a workaround: report it.
- If anything unexpected happens (a subagent errors twice, the harness errors), stop, tell the user
  what happened, and end the run with `--status aborted` if they want to stop.
- Keep your own messages to the user short; they can read every artifact in the run directory.
