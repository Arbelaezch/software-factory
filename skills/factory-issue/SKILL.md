---
name: factory-issue
description: Build one GitHub issue end to end in a headless factory session (the /ship pipeline without human checkpoints), open the PR, and record the outcome. Started by `harness factory run`, one fresh session per issue.
argument-hint: "<issue number>"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py *) Bash(git status) Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git add *) Bash(git commit -F *) Bash(git push origin *) Bash(gh pr create *) Bash(gh issue view *)
---

# /factory-issue: one issue, unattended

You are the factory's **manager for exactly one issue**, running headless: nobody is watching and nobody can answer
a question. Your context ends with this issue, so don't carry anything forward and never start another issue.
Your job ends in exactly one of two ways: a pull request is open and recorded (`factory done`), or the issue is
recorded as blocked (`factory block`). A session that ends any other way is treated as a failure.

Arguments: `$ARGUMENTS`  (the issue number `N`)

Harness command (use exactly this form so it's pre-approved):
`H = python3 ${CLAUDE_PLUGIN_ROOT}/scripts/harness.py --root ${CLAUDE_PROJECT_DIR}`

## How to work

**Never write the code yourself, and never skip a stage.** You are the manager: the diff must come from the
engineer subagents, after scout, plan, gates, review and verify. There is no exception for a change that looks
small or obvious (a five-line change still gets the pipeline; use `--mode quick` if /ship allows it, which still
scouts, plans and reviews). `H factory done` refuses a run that has no plan or no review round.

Read `${CLAUDE_PLUGIN_ROOT}/skills/ship/SKILL.md` and follow it for issue `#N`, as if the user had run
`/ship #N --no-grill`, with these changes. The ship skill stays the single source for the pipeline; this file only
changes where humans were involved.

1. **No grill, no plan checkpoint.** Skip steps 1 and 4 of /ship: the documents were reviewed and answers recorded
   in `.factory/decisions.md` before the issue existed. Treat the plan as approved. Read that decisions file (if it exists; a project
   with no recorded decisions has none) and the issue's linked documents before planning; pass the relevant decisions to the planner and engineers.
2. **Nobody to ask.** Never use AskUserQuestion. Where /ship says to ask the user or relay `NEEDS USER`, send the
   question to the `question-resolver` agent first (give it the question, issue #N, what was being done and the
   paths of `.factory/decisions.md` and the issue's linked documents). It ends with one outcome line:
   - `ANSWERED: ... | SOURCE: ...`: use the answer and quote the source line where you act on it. Continue.
   - `ASSUMED: ... | WHY: ...`: record it with `H factory decide --question "<q>" --answer "<a>" --by assumed
     --kind implementation --why "<why>"`, give the answer to whoever asked, and add an `## Assumptions` section
     with the question and answer to the PR draft (`<run_dir>/pr-<repo>.md`) so it is visible in review. Continue.
   - `ESCALATE: ...`: run `H factory block N --kind QUESTION --situation "<at most 3 sentences>" --need "<the
     decision>" --option ... --option ... --recommend <k> --detail "<run dir path>"` using its options, then stop.
     (Use `--kind BLOCKER` when it is an action, not a decision.)
   If the resolver itself fails or gives no outcome line, treat it as ESCALATE.
   Do the same as ESCALATE, without trying a workaround, for: a dirty tree you didn't create, changed
   dependencies, gates still failing after the retries /ship allows, an ESCALATE verdict, two failed fix attempts,
   a command the guard blocked, or any error you can't fix by following /ship.
3. **Finish with a pull request.** When the verdict is DONE and the summary is written. Headless sessions can only
   run a command that matches a pre-approved pattern, and anything else is refused with no one to approve it. So
   **run every step below as its own Bash call: one plain command, no `&&`, `;` or `|`, no `cd`, no `git -C`, no
   `2>&1`, no multi-line `-m` message.** You already run in the project directory, so use plain `git status`,
   `git add <relative path>` and so on; a `git -C <dir> ...` or `cd <dir> && ...` form is refused. Use absolute
   paths only for files outside the project, such as the run directory. (A first attempt that chained add, commit and
   push in one call was refused for exactly this reason.)
   1. `git status`, then stage only the files in the diff: `git add <path> <path> ...` (never `-A` or `.`).
   2. Write the commit message to `<run_dir>/commit-msg.txt` with the Write tool: a first line that names the issue,
      e.g. `Add export endpoint (#N)`, then a blank line and whatever trailer lines the session instructions require.
      Then `git commit -F <run_dir>/commit-msg.txt`. Don't skip hooks or amend.
   3. `git push origin <branch>`. Never push the base or a protected branch.
   4. `gh pr create --base <base> --head <branch> --title "<title>" --body-file <run_dir>/pr-<repo>.md`.
      The draft already has `Closes #N`. Multi-repo issues get one PR per repo, linked as the draft says.
   5. `H run end --status done`, then `H factory done N --pr <PR url>`.
   Do not merge. Merging is the user's, or build step 6's.
4. **If anything in step 3 fails** (push rejected, PR creation fails): `H factory block N` with what happened.
5. **Rules that still apply:** subagents never commit or push; the guard enforces it. Keep your own output short.
