# Software Factory: design

Status: built and in use on personal projects (callback, court-vision). This began as the design draft of
2026/11/06 and is kept as the reference for how the factory works. Section 12 says what was not built and what
has not been checked on a real run yet.

## 1. What this is

A layer above the harness. I point it at a project with an MVP document. It creates issues, and when I
tell it which issues to work on, it runs harnesses on them one at a time: build, test, review, open a PR,
merge. It only comes to me for decisions that are mine to make. It pauses when plan usage runs out and
resumes when it comes back.

It is for **personal projects**. Professional work keeps the existing harness rules (section 11).

Goals
- Maximum automation on personal projects, with the product vision staying mine.
- Modular: roles, harness recipes and the factory engine are separate and swappable.
- One place to edit anything: a role is one file, a recipe is one file.
- Never surprise me: every message to me is short, standardised and says what I need to decide.

Not goals (v1): see section 13.

## 2. Layers

```
factory        picks issues, runs harnesses, gates on usage, merges, talks to me
  harness      a recipe: which roles, in what order, with which gates and limits
    role       one agent file: the single source of truth for that job
guard          hook that decides who may commit, push and merge (section 11)
```

Rules that keep this modular
1. A role file holds the instructions for its job. A recipe never copies role instructions into itself.
   It only names roles.
2. A recipe holds order, gates, limits and sign-off rules. A role never encodes workflow.
3. The factory calls recipes by name. It never calls roles directly, except the factory-level roles
   in section 4.
4. All limits (review rounds, usage thresholds, retries) live in config, not code.

## 3. Repository layout

Everything lives in one repo, `software-factory` (renamed from `claude-harness` on 2026/11/06; the plugin name
stays `harness`, so `/ship` and the `harness:` agent prefix did not change).

```
agents/              one markdown file per role
skills/              /ship, /factory, /factory-plan, /factory-issue, /review-pr and so on
scripts/             harness.py (CLI, guard, supervisor), factory_loop.py (the loop engine), phone.py,
                     usage.py, statusline_capture.py, defaults.toml
docs/                this file, factory-allowlist.md, context-cost.md
tests/
```

Per-project files live in the **target repo**, not here:

```
<project>/.claude/harness.toml      project config; `[factory]` overrides the defaults in scripts/defaults.toml
<project>/.factory/log.jsonl        append-only factory log (state for resume, section 8)
<project>/.factory/docs.json        the MVP and vision documents recorded by /factory-plan
<project>/.factory/decisions.md     my answers and the factory's assumptions (section 4)
<project>/CLAUDE.md                 project conventions the agents read
```

One supervisor handles one project (`harness --root <project> factory ...`).

## 4. Roles

| Role | Job |
|---|---|
| `scout` | Maps relevant code for a task |
| `planner` | Turns an issue plus scout context into a task plan |
| `engineer` | Implements one task or fixes review findings |
| `claude-reviewer` | Fallback reviewer |
| `verifier` | Checks reviewer findings against the code |
| `mvp-reviewer` | Checks the MVP, vision and design docs are clear and complete enough to answer builders' questions |
| `factory-planner` | Drafts issues with acceptance criteria and dependencies from the clarified docs (`/factory-plan`) |
| `issue-selector` | Picks the next issue from the `todo` pool |
| `question-resolver` | Answers a question from the source docs, or decides it needs me (section 6) |
| `human-liaison` | Sits between the factory and me for every question and blocker (section 7) |
| `ci-fixer` | Diagnoses a failed CI run, attempts one fix and writes its diagnosis for the PR comment |
| `conflict-resolver` | Resolves a merge conflict using both sides' context |
| `factory-discoverer` | With auto-queue on and the queue empty, compares the MVP and vision docs with what is built and filed and drafts what is missing, or nothing (section 10a) |
| `harness-cleanup` | End-of-run cleanup. First job: re-check out-of-scope concerns and file the survivors |

Planning from the MVP document. The care goes into the documents, not into approving issues.
- `/factory-plan <mvp doc>` first has `mvp-reviewer` write an `understanding.md` (what it thinks the project is, in
  and out of scope, decisions the documents already make) and a `gaps.json`: it invents 12 to 20 questions a builder
  would raise, checks whether the documents settle each, and keeps the unsettled ones with a proposed answer, whether
  the question is product or implementation, and whether it cascades.
- I confirm or correct the understanding, then answer the gaps in batches (product questions and cascading ones
  first; I can accept low-impact proposals in bulk, recorded as assumed).
- Every answer is appended to `.factory/decisions.md` with `harness factory decide`; `question-resolver` and the
  planners treat that file as a source document, so the same question is never asked twice.
- Only then does `factory-planner` draft the issues and `harness factory issues-create` make them, with no approval
  gate, dependencies first, `Depends on #N` lines, existing titles skipped, each logged to `.factory/log.jsonl`. The
  checks on the draft (unique keys and titles, known dependencies, no cycles) run inside `issues-create`; `--dry-run`
  is the planner's self-check.
- Issues never get `todo`: I tell the manager which issues to work on and the run loop labels them.
  `harness factory labels` creates the section 8 state labels.

How decisions reach git. Inside a git repo `factory decide` never touches `decisions.md` (unless the project
gitignores it): the entry goes to `.factory/decisions.pending.md`, which git never sees and which every reader reads
alongside it. So an answer recorded while some issue's branch is checked out is visible at once, never rides along in
that issue's PR and never leaves the base branch dirty. The supervisor moves pending entries into `decisions.md` at
quiet points (when it starts and after each issue, on a clean base branch that matches origin) as one commit: pushed
directly when the base branch is unprotected, as a pull request when it is protected or the push is refused
(squash-merged under merge policy auto; otherwise, or if the merge is refused, left open with one
`decisions PR needs your merge` line). Until that lands the pending file stays. Branch creation ignores uncommitted
changes to `.factory/decisions.md`.

Git permissions by role: only the factory manager and the roles below may commit, push, create PRs or
merge, and only in factory mode (section 11). `scout`, `planner` and the reviewers stay read-only
for git. `engineer` may commit to its issue branch. `ci-fixer` and `conflict-resolver` may push to the
PR branch they were launched on. The factory manager creates and merges PRs.

## 5. Harness recipes

Not built as files. The ship pipeline lives in `skills/ship/SKILL.md` (and `skills/factory-issue/SKILL.md` for the
headless version), its limits in `[loop]` in `scripts/defaults.toml`, and the CI-fix and conflict steps in the
supervisor. The recipe idea is kept here as the intended shape if the pipeline is ever made data-driven. Sketch for
a `harnesses/ship.toml`:

```toml
name = "ship"
mode = "factory"                       # "factory" or "professional" (section 11)

[flow]
stages = ["scout", "plan", "implement", "review", "pr"]

[roles]
scout = "scout"
plan = "planner"
implement = "engineer"

[gates]                                # deterministic, run before any review
command = "${project.check_command}"   # same command CI runs

[review]
reviewers = ["claude-reviewer"]        # ordered list, swap freely
verifier = "verifier"
max_rounds = 3                         # matches the current harness setting
signoff = "all"                        # "all" | "majority" | "none"
on_disagreement = "block"              # reviewer vs verifier disagreement at the cap raises a blocker

[limits]
retries = 0                            # a failed or blocked run is never re-run; it is raised to me
```

Review stays modular on purpose: the reviewer list, the round cap and the sign-off rule are all in the
recipe, so reworking the review step is a recipe edit. As project CI matures, soften review by lowering
`max_rounds` or loosening `signoff`.

Other recipes
- `review-pr`: existing behaviour for PRs, unchanged.
- `ci-fix`: `ci-fixer` diagnoses, comments on the PR with diagnosis and attempted fix, pushes one fix.
  One attempt only. If CI fails again, the factory raises a blocker.
- `conflict-resolve`: `conflict-resolver` merges with both sides' intent in context, runs the gates, pushes.

CI vs review: the ship recipe runs only local gates. Remote CI runs after the PR is open and is the final
authority before merge. The local gate command and the CI workflow must call the same check command so
they cannot drift.

## 6. Questions: ask or decide

The rule: **I decide the product; the factory decides how to build it.** I want every product decision and almost
no implementation ones.

Every open question goes to `question-resolver` before it can reach me. It classifies on two axes:

- Category: **product** (what the product does, how it looks, feels or behaves for the people using it, who it is
  for, what is in scope), **implementation** (how it is built) or **process** (how the factory handles the issue:
  another review round, what counts as evidence, housekeeping).
- Impact, for implementation and process: **significant** or **minor**. Significant means any of: it changes what
  the finished product is or how it feels to use, or commits the project to one final experience; it would be
  expensive to undo (data model, public API shape, file format, a core dependency or architecture other issues build
  on); or its options trade off things only I can weigh (cost, scope, security, the quality bar) with no clearly
  better one. Minor means routine, standard or boilerplate, easy to change later, with a sensible default.

It returns one of three outcomes:

| Outcome | When | Recorded |
|---|---|---|
| answered | The source docs settle it. Quote the line. | On the issue |
| assumed | Implementation, low impact, reversible, high certainty. Nothing else. | On the issue and in the PR description |
| escalate | All product questions, anything significant, anything it is unsure about. It names the category and the impact. | Depends on who decides (below) |

Who decides an escalated question (issue #116). The session declares the category and impact on `factory block`, and
the harness applies the rule; nothing is inferred from the wording.

| Question | Who decides |
|---|---|
| product | Me, always. |
| implementation or process, significant | Me. |
| implementation or process, minor | The factory, when the category is in `[factory] auto_accept` (default: both). It takes the recommended option, records it as `assumed`, lists it in the PR, and tells me in one line that I can overrule. The issue is not blocked. With the category left out of `auto_accept`, me. |
| infrastructure (the factory's own tooling is in the way: dirty tree, refused command, CI, usage) | Nobody: there is nothing to choose. It is reported with the action it needs, never asked as a question. |

When unsure between assumed and escalate it escalates, and when unsure between minor and significant it says
significant. Implementation freedom is wide, product freedom is narrow: the vision stays mine.

Each project needs a vision source (a section in the MVP doc or a `VISION.md`). `question-resolver`
treats it as authoritative for product questions.

How it is wired: `/factory-issue` sends every question that /ship would ask the user to the
`question-resolver` agent (opus, read-only) first. `answered` continues with the quoted source; `assumed` is recorded
with `factory decide --by assumed` and listed under `## Assumptions` in the PR description; `escalate` becomes
`factory block --kind QUESTION --category ... --impact ...` with the resolver's options, which either blocks the
issue or prints `AUTO-ACCEPTED` and lets the session go on. A missing or malformed outcome counts as a product
escalation.

## 7. Talking to me

All messages from the factory to me pass through `human-liaison`: questions, blockers, usage
problems, completion notices. The goal is one standard shape that is short and decidable.

Message format
```
[QUESTION | BLOCKER | DONE]  <project> #<issue>
In short:           1-2 plain sentences: what it means for the product, what the recommendation does
What is going on:   at most 3 sentences
What I need:        one specific decision or action
Options:            2-3 choices, each "<short label>: <description>", one marked as recommended (when applicable)
Detail:             link to the raw CI log, agent output or issue comment
```

`In short` and the option labels are what I read first, on the phone and in `/factory` alike (issue #116). They are
written for someone who cannot open the repo: no file names, paths, line numbers, AC numbers or code formatting, and
a label is at most 35 characters and readable alone. `What is going on` and `Detail` carry the engineer's account.
`factory block` requires both and refuses a message that breaks the rules; `notify` checks them when given. The
supervisor's own blockers (CI failed twice, a session that ended without an outcome) have fixed plain wording and no
summary.

Blockers (raised to me, never retried)
- A question `question-resolver` escalates.
- A gate still failing after the review round cap.
- CI failing a second time after `ci-fixer`'s attempt.
- Reviewer and verifier disagreeing at the cap.
- Plan usage staying unreadable for `[factory] unknown_minutes` (default 30; section 9).
- A harness run failing or getting blocked.

Not blockers: merge conflicts (go to `conflict-resolver`), a single CI failure (goes to `ci-fixer`), hitting a
usage limit (pause and resume).

Delivery goes through one function, `notify(message)`, with channels behind it.
- `terminal`: default, always gets every message.
- `push`: the phone, through a Telegram bot (below).
- Adding a channel means adding one class. Nothing else changes.
- Built: `harness notify --kind question|blocker|done --issue N --situation ... [--need ...] [--option ...
  --recommend N] [--detail ...] [--channel terminal|push]`. It rejects a message that breaks the format (more than
  3 sentences, no decision for a QUESTION or BLOCKER, not 2 or 3 options with exactly one recommended). A failing
  channel never loses the message: the terminal always gets it. The `human-liaison` agent (haiku) builds the
  message from the raw material and calls this.

Phone access (decided 2026/11/14, issue #9)
- Claude's own push (Remote Control, `PushNotification`) was dropped: pushes never arrived (tested 2026/11/06), and
  each issue runs in a headless session that exits when it blocks, so there is no live session to
  attach to. Keeping one open per blocker would spend plan usage while waiting.
- QUESTION and BLOCKER messages go to a Telegram bot (`scripts/phone.py`), on top of the terminal and the issue
  comment. DONE stays on the terminal. Each option is a button, the recommended one starred.
- Only a remote run sends them (issue #122): by default blockers stay in the `/factory` manager session.
  `factory start --remote` or `[factory] remote = true` turns the phone on for a run, and `factory set remote on|off`
  switches a running one. The setting is read each time a blocker is raised, so a switch applies to the next blocker,
  including one from the issue in progress; earlier blockers are not re-sent. Asking for remote without the phone set
  up is refused. A supervisor started from a phone answer is remote, so the issue's next blocker reaches the phone too.
- What a phone message shows (issue #116): the project, the issue's number and title, the summary, what to decide,
  then each option's label with its description under the number its button carries. A button is `<n>. <label>`
  plus the star, 40 characters at most.
  A `Details` button returns the full standard message without a model. A blocker with no options is headed
  `stopped`; an infrastructure one says it needs me at the computer and that there is nothing to decide.
- The last line of a message says what else is going on in the project: how many more wait on me, and how many
  questions were decided for me and issues merged since I last tapped or wrote there.
- A question the factory settled itself (section 6) arrives as a silent note with no option buttons, and does not
  keep the Mac awake. Replying to it overrules it: the answer session records my decision and runs the issue again if
  it is still open and not being worked on, and otherwise says a follow-up is needed.
- Messages are sent as they happen, not held back to be grouped: a blocked issue is set aside on its branch and the
  factory moves to the next one, so the sooner I answer the sooner it can come back to it.
- When an answer is recorded, the original message is edited: the option buttons go, and it shows the choice, the
  time, and whether the issue was queued again and when it runs (after the current issue, or when the factory is
  started if it has stopped). A blocker picked up at the computer is marked the same way.
- One bot and one bridge serve every factory on the machine, whatever Claude or GitHub account each project uses.
  Setup is once per machine: create a bot with @BotFather, turn on topics for it there, message it, then run
  `harness phone setup --token <token>`. State lives in `~/.config/software-factory/phone`, outside the projects.
- Each project gets its own topic in the chat. Without topics, every message still names the project and a reply is
  matched by the message it replies to.
- The bridge is a small long-polling process (outgoing HTTPS only, so it works from any network). A remote
  `factory run` and every blocker sent start it if it isn't running; it exits after 30 minutes with no supervisor
  running and nothing waiting on me. While a blocker waits it holds `caffeinate -i`, because the supervisor stops
  (and lets the Mac sleep) once every queued issue is blocked; an idle Mac still sleeps, and a closed lid on battery
  still sleeps. `harness phone serve --forever` keeps it up (for a launchd agent).
- A tap or a reply goes to a short Sonnet session in that project, under the project's Claude account, allowed only
  `harness factory decide|queue|start|status` and file reads. It records the answer and re-queues the issue the way
  `/factory` does (starting the supervisor if it has stopped), or answers a follow-up question from the run's files.
  A fresh session is used because the issue session has exited and the `/factory` manager holds no context about
  the blocker. At most 4 replies per blocker; past that I finish at the computer.
- `status` (or `/status`) in a project's topic returns that project's `harness factory status`; outside the topics
  it covers every project. No model is used.
- A blocker answered at the computer (the issue is queued or started again) is closed out on the phone, on the
  message itself.
- Only my Telegram user, in the chat found at setup, is listened to.

A blocker stops only the blocked issue and anything that depends on it. The factory keeps working other
independent issues in my directed group. I am pinged less because fewer questions reach me (section 6), not because
they are held back.

## 8. State and labels

State lives in GitHub plus one log file. No database.

Labels
- `todo`: in the candidate pool. I add it, or the factory adds it when I name issues directly.
- `in-progress`: a harness is running on it.
- `awaiting-merge`: PR open, waiting on CI or on me.
- `blocked`: raised to me.
- `backlog`: created by `harness-cleanup`. Never worked until I change it to `todo`.

`.factory/log.jsonl` records every factory decision (issue picked, harness started and finished, usage pause
and resume, question outcome, merge). It is append-only. On start the factory replays it plus issue labels
to resume after a crash, pause or closed laptop.

Dependencies are written in each issue body as `Depends on #N`. The selector reads them.

## 9. The run loop

```
start(project, merge_policy)              # --merge auto|manual, default [factory] merge (auto)
loop:
  usage = usage.check()                   # scripts/usage.py
  if pause:   sleep until resume_at, then continue       # no prompt to me
  if unknown: retry with a growing wait; after unknown_minutes raise a blocker and stop
  pool = issues labelled todo, not blocked, dependencies merged
  if pool is empty: finish
  issue = issue-selector(pool)            # brief assessment; order is not label order
  run harness "ship" on issue             # sequential; also watches rate_limit_event during the run
  on success: gates passed, reviewers and verifier signed off -> open PR
     merge policy auto:   wait for CI if the repo has it -> squash merge
        CI fails -> ci-fix harness once -> CI passes -> merge; fails again -> blocker
        conflict -> conflict-resolve harness
     merge policy manual: label awaiting-merge and continue with an independent issue
  on failure or block: label blocked, send via human-liaison, continue with independent issues
  at end of group: harness-cleanup (section 10)
```

How it is built
- The loop is a small Python supervisor, `harness factory run [--issues 12,13] [--max-issues N] [--merge manual]`
  (engine in `scripts/factory_loop.py`, side effects injected). It spends no tokens: it checks usage, computes the
  pool, asks `issue-selector` for the order, and starts one fresh headless session per issue with
  `claude -p "/factory-issue N"` under its own factory marker. A long-lived manager session would grow its context
  across issues; this way each issue's context ends with the issue, and the interactive manager from section 7
  stays optional.
- All state is issue labels plus `.factory/log.jsonl`, so restarting the command resumes. An issue still labelled
  `in-progress` at start is an orphan from a crash: it becomes `blocked` and is raised to me, never guessed at.
- The pool is open issues labelled `todo` with every `Depends on #N` issue closed. I name the issues
  (`--issues` or `harness factory queue N...`); creation never queues anything. Merged PRs close their issues via
  `Closes #N`, so dependencies unlock on merge, not on PR open.
- `/factory-issue` follows `/ship` (the single source for the pipeline) with the human steps removed: no grill and
  no plan checkpoint, since the documents and `.factory/decisions.md` were settled earlier. It never asks. Where
  /ship asks me, it checks the decisions file, and otherwise runs `harness factory block N` (label `blocked`, comment
  on the issue, message through `notify`) and stops. When the review verdict is DONE it commits the diff, pushes
  the branch, opens the PR from the draft and runs `harness factory done N --pr URL` (label `awaiting-merge`).
  A session that ends in neither state is marked `blocked` by the supervisor.
- Questions are asynchronous: they arrive on the terminal and the phone, and I answer from either (section 7). At the
  computer that is `harness factory decide` and a relabel to `todo`, through `/factory`.
- The front door is `/factory` (skill), a thin conversation with the manager: "work on 12 and 13", "status",
  an answer to a blocked issue, "stop". It calls `harness factory start | status | stop | queue | decide` and
  never does the work. `start` launches `factory run` detached (own session, under `caffeinate`, lock in
  `.factory/supervisor.json`, output in `.factory/supervisor.log`), so the manager conversation can be cleared or
  closed at any time. `status` rebuilds the picture from labels and files: supervisor, usage snapshot, the current
  issue with a stage read from its run directory (scouting, implementing k/n, review round n) and the last lines
  of its session log, blocked and awaiting-merge issues, recent events. The supervisor appends one short line per
  event to `.factory/events.log`; the manager watches it with the Monitor tool and relays blockers and questions.
  `stop` finishes the current issue first; `stop --now` signals the supervisor, and the interrupted issue becomes
  `blocked` at the next start. Typing `harness factory run` directly still works but is not the intended path.
- Merge policy is `--merge auto|manual` (`factory start`/`run`; default `[factory] merge`, which is `auto`: using the
  factory means hands off; `/ship` on its own always leaves the merge to me). With `manual` the issue stops at
  `awaiting-merge` and I merge. With `auto` the supervisor itself (no tokens) lands each PR:
  - `factory done` records the PR URL in `.factory/prs.json`; the supervisor polls `gh pr view` (checks, mergeable)
    every `ci_poll` seconds up to `ci_timeout`; "no checks" counts as no CI only after `ci_grace` seconds.
  - Green or no CI: `gh pr merge --squash --delete-branch`, which closes the issue through `Closes #N` and unlocks
    dependants.
  - Failed CI: switch to the PR branch and start one headless `ci-fixer` session (marker role `ci-fixer`,
    `pr_branch`), which fixes, runs the checks, pushes and writes its diagnosis to `.factory/ci-fix-N.md`; the
    supervisor posts that as the PR comment, then waits for the new commit's checks (results of the old head commit
    are ignored).
  - A merge conflict goes the same way: one headless `conflict-resolver` session (marker `pr_branch` +
    `base_branch`) merges `origin/<base>`, keeps both sides' intent, runs the checks, pushes and writes
    `.factory/conflict-N.md` for the PR comment. The order is conflict, then CI failure, each tried once.
  - A second failure, a conflict that remains, a closed PR, a CI timeout or a refused merge labels the issue
    `blocked` and raises a blocker.
  - The loop only calls `land` for issues that ended `awaiting-merge`, so a manual-policy run is unchanged.
- Tunables are in `[factory]` in `scripts/defaults.toml` (usage limits, the unknown-reading limit, and the
  `claude_args` passed to each headless session; it defaults to `--permission-mode acceptEdits`).

Usage gate
- Uses `scripts/usage.py`. Defaults: pause at 85% of the 5-hour window and 90% of the weekly window.
  `factory start --until-limit` (opt-in, per run) raises both caps to 100%: it works until the plan limit is really
  hit, then pauses until the window resets. An issue cut off by the limit goes back to `todo` and is retried once
  after the reset.
- Checked before every harness launch, and again when an issue's session ends without an outcome (see below).
- Pausing is silent. The factory sleeps until `resume_at` and continues without prompting. It sleeps in 5-minute
  steps against the wall clock, because one long sleep does not count the time the Mac is asleep: a pause
  across a closed lid used to end hours late (court-vision 2026/11/12 and 11/13 never resumed by themselves).
- Unreadable usage retries with a growing wait (1, 2, 4, 8, then 15 minutes) and stops the factory with a
  blocker only after `[factory] unknown_minutes` (default 30) without a reading.
- An issue whose session ends without an outcome while usage is over the cap most likely ran into the limit.
  It goes back to `todo` (logged as `usage_requeued`) and runs again after the reset, once per run; a second
  failure blocks it as usual.
- Nighttime mode (`factory start --nighttime`, or `[factory] nighttime = true`) is for unattended runs: unreadable
  usage stops the factory only after `nighttime_unknown_hours` (default 6). A resume that came more than 10
  minutes after the reset logs `late_min` (the Mac was asleep), so the night can be checked in the morning.
- Run settings (`nighttime`, `auto-queue`, `until-limit`, `remote`) live in `.factory/run-settings.json` while a
  supervisor runs. `factory set <setting> on|off` changes them without a restart: the loop re-reads them before each
  issue and at each usage reading, and a usage wait ends early when they change (`remote` is read when a blocker is
  raised, section 7). `factory status` shows them.
- The factory runs as a long-lived process on my Mac under `caffeinate`. If the lid closes it pauses and
  resumes when the machine wakes. A supervisor lock whose pid is dead, or was reused by a process started
  after the lock was written (a reboot), is cleared on the next start.

Selector. `issue-selector` gets the `todo` pool in its prompt (each candidate's body and the open issues it
unblocks, plus the issues awaiting merge with the files their pull requests change), may look at the code with
read-only tools, and picks the next issue with a one-line reason logged. It is advisory about order only. It
cannot pick an issue with an unmerged dependency. A pick that was not a judgement (no usable answer, or the
selector says it lacked data) has a reason starting with `fallback:`, is logged with `fallback: true`, shows as
`picked #N (fallback)` in the events, and is counted in `factory status`.

## 10. Out-of-scope work

During a run, agents record out-of-scope concerns in the run ledger and never create issues mid-feature.
After PR creation or merge, `harness-cleanup` re-evaluates each concern shallowly: still real, still worth
doing, not a duplicate. Survivors become issues labelled `backlog`, with context linking back to the issue
that raised them. They are never worked until I change the label to `todo`. `harness-cleanup` is the home
for other end-of-run chores later.

How it is wired: `harness run concern "<text>" [--file F]` appends to `<run_dir>/concerns.jsonl`; the
engineer, reviewers, verifier, scout and planner use it for real out-of-scope problems (at most 3, never fixed).
After an issue's PR is open or merged, and only when that file exists, the supervisor starts one headless
`harness-cleanup` session (read-only; Bash limited to `gh issue list|view`, `git log|diff`). It drops concerns that are
gone, not worth doing or already covered, and writes at most 5 survivors to `backlog.json`. The supervisor creates
the issues (label `backlog`, "Raised while working on #N" link, skipping existing titles), so no agent creates an
issue. A failure here is logged and never stops the loop.

## 10a. Auto-queue

Off by default. `factory start --auto-queue` (or "let it loose" to `/factory`) lets one run queue its own work
instead of stopping when the queue is empty. It is per run and never persisted, so the next `factory start` is
back to working only what I named. It requires `--merge auto`: with manual merging the next issue would be built
on code that is not merged yet.

When the pool is empty the loop calls a refill step, in this order, and goes on as soon as one source yields work:
1. **Planned** issues: open, no state label, created by `factory issues-create` (read from `issue_created` in
   `.factory/log.jsonl`) and with every dependency closed. Issues a person opened are never taken.
2. **Backlog** issues from `harness-cleanup`, with dependencies closed. This is the one place the "never worked
   until I relabel it" rule of section 8 is waived, and only for a run I started with the flag.
3. **Discovery**: one headless `factory-discoverer` session (read-only; Bash limited to `gh issue list|view` and
   `git log|ls-files|grep`) reads the documents recorded by `factory docs` (written by `/factory-plan`), the
   decisions, every existing issue and the code, and writes issues only for what the documents call for that is
   neither built nor filed. The supervisor validates the list, cuts it to `auto_queue_per_round` (default 5, dropping
   issues whose dependency was cut), files it with the `todo` label and logs `discovered`. No documents recorded
   means no discovery.

Limits
- `auto_queue_rounds` discovery rounds per run (default 3). A round that finds nothing ends auto-queue for the run,
  which is the normal way a run finishes. `--max-issues` still caps the total; the usage gate and blockers are
  unchanged.
- An issue that is `todo` again after its session (a blocker answered and re-queued mid-run) is picked again before
  any refill, at most `max_runs_per_issue` times per run (default 3). Issues left over by that cap are logged
  (`issues_skipped`), named in the stop line, and hold back refill and discovery.

Links between issues. A drafted issue (discovered, planned or a cleanup backlog item) points at existing issues by
number in one of two fields: `depends_on_issues` becomes `Depends on #N` lines, so a follow-up waits for unfinished
work, and `related_issues` becomes `Related to #N` lines, which nothing waits for. Validation rejects a number that
is not an issue and any open issue the text names as `#N` without listing it in one of the fields. A rejected
discovery draft or cleanup list goes back to its agent once with the problems. A discovery draft still invalid after
that ends auto-queue with "discovery failed", not "nothing left to build"; cleanup items still wrong after that are
dropped and logged as `cleanup_rejected` (the issue the cleanup ran for needs no listing).

Every step is logged (`auto_queue_on`, `auto_queued` with its source, `discovered`, `auto_queue_exhausted`) and shown
in `factory status` events, but none wake the manager. Anything the documents leave open goes in the discoverer's
`notes`, not into an invented issue. A refill failure is logged and ends the run cleanly.

## 11. Personal and professional split

This boundary must always hold.

- Professional harnesses keep the current rules exactly: no commits by agents, no pushes, no GitHub writes,
  commits and pushes only by me through the main session as my personal account.
- Factory mode is enabled only by an explicit marker set by the factory launcher, and only for the project
  directory it was started on. The marker is an env var plus a file, and both must agree:
  the launcher exports `SOFTWARE_FACTORY_MARKER=<32-char nonce>` and writes
  `~/.software-factory/markers/<nonce>.json` with `{project, pid, started, role, pr_branch?, issue_branch?, base_branch?}`
  (mode 0600, deleted on exit). The guard accepts it only if the nonce is well-formed, the file is a regular
  file owned by the user and not group/world-writable, the pid is alive, `started` is under 24 hours old, and the
  realpath of the cwd (including `cd` and `git -C` targets) is inside `project`. Any error means no factory mode.
- The role of a subagent always comes from the hook payload's `agent_type`, never from the marker, because
  subagents inherit the env var. A call with no `agent_type` is the manager only if the file says
  `role = "manager"`, so an interactive session in the same terminal gets nothing.
- Factory mode only lifts the subagent denial, and only for: `engineer` add/commit on `issue_branch` (no
  `add -A`/`.`, no `--no-verify`); `ci-fixer` and `conflict-resolver` `git push [origin [<branch>]]` of
  `pr_branch` (no flags, refspecs or URL remotes). `conflict-resolver` also merges `origin/<base_branch>` (or
  `--abort`) into `pr_branch` and may add/commit there, so it never rebases or force-pushes. It never skips the
  account check (a work-account session is still denied), and never lifts force/delete pushes, protected branches, `--amend`, rebase, token/host overrides
  or gh account switching. The manager's own powers are unchanged: they already come from the main-session rules.
- The launcher is `harness factory launch [--role manager|engineer|ci-fixer|conflict-resolver] [--issue-branch B]
  [--pr-branch B] [--base-branch B] [-- <command...>]` (default command: `claude`). It refuses professional
  harnesses and roles missing their branch fields, writes the marker with this process's pid, runs the command in
  the project root with the nonce exported, forwards signals, and deletes the marker on exit. Every launch first
  prunes stale markers; `harness factory cleanup` does only that.
- Workers that need a branch (`engineer`, `ci-fixer`, `conflict-resolver`) run as their own sessions, launched by
  the manager with `harness factory launch --role <role> --<branch flags> -- claude -p ...`, so each gets its own
  marker with its branch fields; Task subagents can't carry per-worker markers. A session whose marker role is not
  `manager` is held to that role's rules like a subagent (it has no `agent_type`, so the guard uses the marker
  role), even outside the project. `ci-fixer` may also add and commit on its `pr_branch`, since its fix has to be
  committed before it pushes.
- Subagent detection uses `agent_id`, not `agent_type`. Checked against Claude Code 2.1.311: a main session has
  neither field, a Task subagent (including plugin ones like `harness:scout`) has both, and a main session started
  with `--agent` has `agent_type` only. Keying on `agent_type` would have treated a `--agent` manager as a subagent.
  Nested subagents were not tested.
- In factory mode the guard denies Bash, Write, Edit and notebook calls that mention or reach `~/.software-factory`.
  That is best effort against a process running as the same user. The real barrier is a manual one-time install
  in `~/.claude/settings.json` (user settings; the plugin can't ship it):
  `"permissions": {"deny": ["Edit(~/.software-factory/**)", "Write(~/.software-factory/**)",
  "NotebookEdit(~/.software-factory/**)", "Bash(*.software-factory*)"]}`. It also blocks harmless commands that
  mention the folder (like `ls`) in Claude sessions; run those from a separate terminal. The launcher is a separate
  process and is unaffected. Check with `/permissions`, then try a write into the folder.
- In factory mode a subagent may not use `git -c`, `--git-dir` or `--work-tree`, or write `git config`, since those
  can redirect a push to another repo or URL.
- The guard defaults to deny. Without the marker, or outside that directory, behaviour is today's.
- Factory-mode permissions are per role (section 4), not blanket. Merges and PR creation belong to the
  factory manager.
- Git writes in factory mode use the personal GitHub account, as the harness does today. Repos have no
  branch protection yet and little CI, so this is fine initially.
- A professional recipe has `mode = "professional"` and cannot be started by the factory.

The guard change is the riskiest part of this design. It gets its own tests, including: marker absent,
marker with the wrong directory, and a subagent without push rights.

## 12. Status

Everything in sections 4 to 11 is built and merged, with one exception: recipes as TOML files (section 5).

Not checked on a real run yet (unit tested only):
- A blocker answered from the phone, the plain-language phone messages (#116) and the `--remote` switch (#122).
- Nighttime mode over a real night: `usage_resume` with `late_min`, and `usage_requeued`.
- The fixes for re-queued issues, decisions outside the working tree and the selector's data (#113 to #115).
- Listed as unseen on 2026/11/09 and not re-checked for this document: the `ci-fixer` and `conflict-resolver`
  sessions, `question-resolver` on a real question, and `harness-cleanup` filing backlog issues.

Worth knowing when running it
- Updating the plugin is not enough: restart the `/factory` session and stop any old supervisor, because the
  supervisor runs the plugin build it was started from.
- Headless sessions can't prompt, so commands must be plain and one per call (no `&&`, `|`, `cd`, `git -C`) and
  anything repo-specific has to be allow-listed; see `docs/factory-allowlist.md`.
- `factory done` refuses a session that skipped the pipeline: it checks for a plan and a review.

## 13. Not in v1

- Parallel harnesses or swarms.
- Stacked branches (an issue building on an unmerged PR).
- Several projects in one factory process.
- A dashboard or web UI.
- Smart blocker summarising beyond the standard format.
- Retrying failed harness runs.

## 14. Open items

- `ci-fixer` can't comment on the PR itself (`gh pr comment` is blocked for worker sessions), so it writes its
  diagnosis to `.factory/ci-fix-N.md` and the supervisor posts it.
- Confirm what the usage probe reports when I am near or over a limit (the `status` field values are unseen).
- #7 (BM25 ranking for the scout) stays open.

