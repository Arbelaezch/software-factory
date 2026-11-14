import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

_spec = importlib.util.spec_from_file_location("harness", Path(__file__).parent.parent / "scripts" / "harness.py")
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)

PLAN = """# Plan: add export
## Approach
why
## Contract changes
GET /export returns {rows}
## Tasks
### T1: endpoint  (repo: backend, model: sonnet)
- Files: a.py
- Do: lots of steps
- Done when: endpoint returns 200
"""


class HarnessTests(unittest.TestCase):
    def test_split_diff_files(self):
        d = "diff --git a/x b/x\n+1\ndiff --git a/y b/y\n+2\n"
        self.assertEqual(list(h.split_diff_files(d)), ["diff --git a/x b/x", "diff --git a/y b/y"])

    def test_plan_digest_keeps_criteria_drops_steps(self):
        out = h.plan_digest(PLAN)
        self.assertIn("Done when: endpoint returns 200", out)
        self.assertIn("GET /export", out)
        self.assertNotIn("lots of steps", out)

    def test_review_prompt_modes(self):
        self.assertIn('"questions"', h.review_system_prompt({"mode": "review"}, {}))
        self.assertNotIn('"questions"', h.review_system_prompt({"mode": "ship"}, {}))


REAL_CURRENT_BRANCH = h.current_branch
NONCE = "a" * 32
CFG = {"git": {"mode": "uncommitted", "protected_branches": ["main"]}, "github": {"write_accounts": ["me"]}}


class FactoryGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.project = os.path.join(self.home, "p", "app")
        os.makedirs(self.project)
        os.makedirs(os.path.join(self.home, "p", "app2"))
        self.branch = "feat/x"
        self.account = "writer"
        for patcher in (
            mock.patch.dict(os.environ, {"SOFTWARE_FACTORY_HOME": self.home, h.MARKER_ENV: NONCE}),
            mock.patch.object(h, "current_branch", lambda path: self.branch),
            mock.patch.object(h, "github_account_state", lambda cmd, cfg: (self.account, "stub")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def marker(self, **kw):
        m = {"project": self.project, "pid": os.getpid(), "started": time.time(), "role": "manager",
             "pr_branch": "feat/x", "issue_branch": "feat/x", **kw}
        d = h.marker_dir()
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{NONCE}.json"
        f.write_text(json.dumps(m))
        os.chmod(f, 0o600)
        return m

    def ctx(self, agent, cwd=None):
        return h.factory_context({"cwd": cwd or self.project, "agent_type": agent, "agent_id": "a1" if agent else None})

    def bash(self, cmd, agent="harness:ci-fixer", cwd=None):
        return h.check_bash(cmd, CFG, cwd or self.project, subagent=bool(agent), factory=self.ctx(agent, cwd))

    def test_no_marker_means_todays_behaviour(self):
        self.assertIsNone(self.ctx("harness:ci-fixer"))
        self.assertIn("blocked for subagents", self.bash("git push origin feat/x"))

    def test_env_without_file_and_file_without_env(self):
        self.assertIsNone(self.ctx(None))
        self.marker()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(h.MARKER_ENV)
            self.assertIsNone(self.ctx(None))

    def test_bad_marker_is_rejected(self):
        for kw in ({"pid": 2 ** 22 + 12345}, {"started": time.time() - 10 ** 6}, {"project": "/nope"}):
            self.marker(**kw)
            self.assertIsNone(h.load_marker(self.project), kw)
        self.marker()
        os.chmod(h.marker_dir() / f"{NONCE}.json", 0o666)
        self.assertIsNone(h.load_marker(self.project))
        with mock.patch.dict(os.environ, {h.MARKER_ENV: "../../etc/passwd"}):
            self.assertIsNone(h.load_marker(self.project))

    def test_marker_for_other_or_sibling_directory(self):
        self.marker()
        self.assertIsNotNone(h.load_marker(self.project))
        self.assertIsNone(h.load_marker(os.path.join(self.home, "p", "app2")))
        self.assertIsNone(h.load_marker(self.home))
        link = os.path.join(self.home, "link")
        os.symlink(os.path.join(self.home, "p", "app2"), link)
        self.assertIsNone(h.load_marker(link))

    def test_ci_fixer_pushes_only_its_pr_branch(self):
        self.marker()
        self.assertIsNone(self.bash("git push origin feat/x"))
        self.assertIsNone(self.bash("git push"))
        for cmd in ("git push origin other", "git push origin HEAD:other", "git push --force origin feat/x",
                    "git push origin feat/x --tags", "git push https://x/y.git feat/x", "git push --all"):
            self.assertIsNotNone(self.bash(cmd), cmd)
        self.branch = "other"
        self.assertIn("blocked for subagents", self.bash("git push"))

    def test_push_outside_project_via_cd_or_C(self):
        self.marker()
        out = os.path.join(self.home, "p", "app2")
        self.assertIsNotNone(self.bash(f"cd {out} && git push origin feat/x"))
        self.assertIsNotNone(self.bash(f"git -C {out} push origin feat/x"))

    def test_protected_branch_never_lifted(self):
        self.marker(pr_branch="main")
        self.branch = "main"
        self.assertIn("protected", self.bash("git push origin main"))

    def test_work_account_with_valid_marker_still_denied(self):
        self.marker()
        self.account = "other"
        self.assertIn("isn't in [github].write_accounts", self.bash("git push origin feat/x"))
        self.assertIn("isn't in [github].write_accounts", self.bash("git commit -m x", "harness:engineer"))

    def test_engineer_commits_on_issue_branch_only(self):
        self.marker()
        self.assertIsNone(self.bash("git add a.py && git commit -m x", "harness:engineer"))
        for cmd in ("git add -A", "git add .", "git commit --no-verify -m x", "git commit -nm x",
                    "git commit --amend -m x", "git push origin feat/x"):
            self.assertIsNotNone(self.bash(cmd, "harness:engineer"), cmd)
        self.branch = "main"
        self.assertIsNotNone(self.bash("git commit -m x", "harness:engineer"))

    def test_conflict_resolver_merges_base_into_pr_branch(self):
        self.marker(base_branch="develop")
        r = "harness:conflict-resolver"
        for cmd in ("git merge origin/develop", "git merge --no-edit origin/develop", "git merge --abort",
                    "git add a.py", "git commit --no-edit", "git push origin feat/x"):
            self.assertIsNone(self.bash(cmd, r), cmd)
        for cmd in ("git merge origin/other", "git merge --squash origin/develop", "git merge -X theirs origin/develop",
                    "git merge feat/y", "git rebase origin/develop", "git add -A", "git push --force origin feat/x"):
            self.assertIsNotNone(self.bash(cmd, r), cmd)
        self.branch = "other"
        self.assertIsNotNone(self.bash("git merge origin/develop", r))
        self.branch = "feat/x"
        self.assertIsNotNone(self.bash("git merge origin/develop", "harness:engineer"))
        self.account = "other"
        self.assertIn("isn't in [github].write_accounts", self.bash("git merge origin/develop", r))

    def test_other_roles_get_nothing(self):
        self.marker()
        for agent in ("harness:scout", "harness:planner", "harness:claude-reviewer", "harness:verifier", "harness:unknown"):
            for cmd in ("git push origin feat/x", "git commit -m x", "git add a.py"):
                self.assertIsNotNone(self.bash(cmd, agent), (agent, cmd))
        self.assertIsNotNone(self.bash("git push origin feat/x", "harness:engineer"))
        self.assertIsNotNone(self.bash("git merge origin/develop", "harness:ci-fixer"))

    def test_manager_needs_explicit_role(self):
        m = self.marker()
        self.assertTrue(h.factory_permission(m, None, self.project, "pr_merge"))
        self.assertFalse(h.factory_permission(m, None, self.project, "commit"))
        self.assertFalse(h.factory_permission({**m, "role": "worker"}, None, self.project, "push"))
        self.assertFalse(h.factory_permission(m, None, "/elsewhere", "push"))

    def test_gh_stays_blocked_for_subagents(self):
        self.marker()
        self.assertIn("blocked for subagents", self.bash("gh pr merge 3", "harness:ci-fixer"))

    def test_launcher_writes_and_removes_marker(self):
        probe = ("import os,sys,json;"
                 "p=os.environ['SOFTWARE_FACTORY_HOME']+'/.software-factory/markers/'+os.environ['SOFTWARE_FACTORY_MARKER']+'.json';"
                 "m=json.load(open(p));"
                 "sys.exit(7 if m['role']=='ci-fixer' and m['pr_branch']=='feat/x' and (os.stat(p).st_mode&0o777)==0o600 else 3)")
        os.environ.pop(h.MARKER_ENV)
        rc = h.run_factory(self.project, "ci-fixer", {"pr_branch": "feat/x", "issue_branch": None}, ["python3", "-c", probe])
        self.assertEqual(rc, 7)
        self.assertEqual(list(h.marker_dir().glob("*.json")), [])

    def test_launcher_marker_is_accepted_by_the_guard(self):
        nonce, path = h.write_marker(self.project, "engineer", {"issue_branch": "feat/x"})
        self.addCleanup(path.unlink, missing_ok=True)
        with mock.patch.dict(os.environ, {h.MARKER_ENV: nonce}):
            self.assertEqual(h.load_marker(self.project)["role"], "engineer")
            self.assertIsNone(self.bash("git commit -m x", "harness:engineer"))
            self.assertIsNone(h.load_marker(os.path.join(self.home, "p", "app2")))

    def test_launcher_removes_marker_when_command_fails(self):
        self.assertEqual(h.run_factory(self.project, "manager", {}, ["python3", "-c", "raise SystemExit(5)"]), 5)
        self.assertEqual(list(h.marker_dir().glob("*.json")), [])

    def test_prune_removes_only_stale_markers(self):
        live, lp = h.write_marker(self.project, "manager", {})
        old = {"project": self.project, "pid": os.getpid(), "started": time.time() - 10 ** 6, "role": "manager"}
        dead = {**old, "started": time.time(), "pid": 2 ** 22 + 12345}
        for name, body in (("b" * 32, old), ("c" * 32, dead)):
            (h.marker_dir() / f"{name}.json").write_text(json.dumps(body))
        (h.marker_dir() / ("d" * 32 + ".json")).write_text("not json")
        (h.marker_dir() / "notes.json").write_text("{}")
        self.assertEqual(h.prune_markers(), 3)
        self.assertEqual(sorted(f.name for f in h.marker_dir().glob("*.json")), sorted([lp.name, "notes.json"]))

    def test_launcher_needs_branch_fields_per_role(self):
        self.assertEqual(h.FACTORY_ROLE_FIELDS["conflict-resolver"], ("pr_branch", "base_branch"))
        self.assertEqual(set(h.FACTORY_ROLE_FIELDS), set(h.FACTORY_ROLES))

    def test_worker_session_is_held_to_its_role(self):
        """A `claude -p` worker launched with its own marker has no agent_type, but must not get main-session powers."""
        self.marker(role="ci-fixer", base_branch="develop")
        def deny(cmd, cwd=None):
            out = self.hook(h.hook_guard_bash, {"command": cmd}, None, cwd)
            return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"
        self.assertFalse(deny("git push origin feat/x"))
        self.assertFalse(deny("git add a.py && git commit -m fix"))
        for cmd in ("git push origin other", "git push --force origin feat/x", "gh pr merge 3", "gh pr create",
                    "git merge origin/develop", "git add -A"):
            self.assertTrue(deny(cmd), cmd)
        self.branch = "other"
        self.assertTrue(deny("git push"))
        self.assertTrue(deny("git commit -m x"))
        self.branch = "feat/x"
        self.assertTrue(deny("git push origin feat/x", os.path.join(self.home, "p", "app2")))  # outside the project

    def test_engineer_worker_session_cannot_push(self):
        self.marker(role="engineer")
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "git commit -m x"}))
        out = self.hook(h.hook_guard_github, {"command": "git push origin feat/x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_manager_session_keeps_main_session_powers(self):
        self.marker(role="manager")
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "git push origin feat/x"}))
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "gh pr merge 3"}))

    def test_subagent_is_decided_by_agent_id_not_agent_type(self):
        self.assertFalse(h.is_subagent({"agent_type": "probe"}, None))  # main session started with --agent
        self.assertFalse(h.is_subagent({}, None))
        self.assertTrue(h.is_subagent({"agent_type": "harness:scout", "agent_id": "a1"}, None))
        self.marker()
        for agent_type in ("probe", "harness:ci-fixer"):  # no agent_id: a main session, so the manager rules apply
            f = h.factory_context({"cwd": self.project, "agent_type": agent_type})
            self.assertFalse(h.is_subagent({"agent_type": agent_type}, f))
        f = h.factory_context({"cwd": self.project, "agent_type": "harness:ci-fixer", "agent_id": "a1"})
        self.assertTrue(h.is_subagent({}, f))

    def hook(self, fn, tool_input, agent=None, cwd=None):
        payload = json.dumps({"cwd": cwd or self.project, "agent_type": agent, "agent_id": "a1" if agent else None,
                              "tool_input": tool_input})
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(out), \
                mock.patch.object(h, "log_event", lambda e: None):
            fn(CFG)
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_marker_files_are_off_limits(self):
        self.marker()
        for tool_input in ({"command": f"echo x > {self.home}/.software-factory/markers/a.json"},
                           {"command": "cd ~/.software-factory && echo x > markers/a.json"},
                           {"file_path": str(h.marker_dir() / "a.json")},
                           {"notebook_path": str(h.marker_dir() / "a.ipynb")}):
            for fn in (h.hook_guard_bash, h.hook_guard_github):
                for agent in (None, "harness:engineer"):
                    out = self.hook(fn, tool_input, agent)
                    self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", (tool_input, fn, agent))
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "ls", "file_path": "a.py"}))

    def test_no_marker_leaves_dot_dir_alone(self):
        self.assertIsNone(self.hook(h.hook_guard_bash, {"command": "ls ~/.software-factory"}))

    def test_git_redirection_blocked_for_factory_subagents(self):
        self.marker()
        other = os.path.join(self.home, "p", "app2")
        for cmd in (f"git --git-dir={other}/.git push origin feat/x", f"git --git-dir {other}/.git push",
                    "git --work-tree=/x push origin feat/x", "git -c remote.origin.pushurl=u push origin feat/x",
                    "git config remote.origin.pushurl u", "git config url.u.insteadOf v"):
            self.assertIsNotNone(self.bash(cmd), cmd)
            self.assertIsNotNone(h.check_github_global(cmd, CFG, self.project, True, self.ctx("harness:ci-fixer")), cmd)
        self.assertIsNone(self.bash(f"git -C {self.project} push origin feat/x"))
        self.assertIsNone(self.bash("git config --get remote.origin.url"))

    def test_hooks_with_a_real_repo(self):
        self.marker()
        subprocess.run(["git", "init", "-q", "-b", "feat/x", self.project], check=True)
        with mock.patch.object(h, "current_branch", REAL_CURRENT_BRANCH):
            denied = lambda fn, cmd, agent: (self.hook(fn, {"command": cmd}, agent) or {}).get(
                "hookSpecificOutput", {}).get("permissionDecision") == "deny"
            for fn in (h.hook_guard_bash, h.hook_guard_github):
                self.assertFalse(denied(fn, "git push origin feat/x", "harness:ci-fixer"), fn)
                self.assertTrue(denied(fn, "git push origin feat/x", "harness:scout"), fn)
                self.assertTrue(denied(fn, "git push origin other", "harness:ci-fixer"), fn)
            subprocess.run(["git", "-C", self.project, "checkout", "-q", "-b", "other"], check=True)
            self.assertTrue(denied(h.hook_guard_bash, "git push", "harness:ci-fixer"))
            self.assertFalse(denied(h.hook_guard_bash, "git push", None))  # the main session is not a factory subagent


class ShellParsingTests(unittest.TestCase):
    """Text that only mentions a command is data; things that run are still caught (issues #3 and #18)."""

    def cmds(self, command):
        return [t[0] for t in h.split_segments(command)]

    def guard(self, command, **kw):
        with mock.patch.object(h, "github_account_state", lambda c, cfg: ("writer", "stub")), \
                mock.patch.object(h, "current_branch", lambda p: "feat/x"):
            return h.check_bash(command, CFG, "/p", **kw), h.check_github_global(command, CFG, "/p", **kw)

    def test_quoted_operators_do_not_split(self):
        self.assertEqual(self.cmds("grep -E 'git push|git add' f"), ["grep"])
        self.assertEqual(self.cmds('echo "a; git push && b"'), ["echo"])
        self.assertEqual(self.cmds("echo a\\; git push"), ["echo"])

    def test_heredoc_bodies_are_data(self):
        for cmd in ("cat > f.md <<'EOF'\nuse gh auth switch and GH_TOKEN=x\ngit push origin main\nEOF\nls",
                    "cat <<-EOF\n\tgit add .\n\tEOF\nls", 'cat <<"X"\ngit push origin main\nX'):
            self.assertEqual(self.guard(cmd), (None, None), cmd)
        self.assertEqual(self.cmds("cat <<EOF && ls\nbody\nEOF"), ["cat", "ls"])

    def test_things_that_run_are_still_checked(self):
        for cmd in ("echo ok && git push origin main", 'echo "$(git push origin main)"', "echo `git push origin main`",
                    'bash -c "git push origin main"', "sh -lc 'git push --force origin x'",
                    "eval 'git push origin main'", "cat <<EOF\n$(git push origin main)\nEOF",
                    "echo 'unterminated && git push origin main", "(git push origin main)", "git push origin main &"):
            self.assertIn("git", " ".join(sum(h.split_segments(cmd), [])), cmd)
            self.assertTrue(any(self.guard(cmd)), cmd)
        self.assertTrue(any(self.guard("gh auth switch")))
        self.assertTrue(any(self.guard("echo x; gh auth switch -u other")))

    def test_token_overrides_only_count_as_env_assignments(self):
        self.assertEqual(self.guard("echo 'GH_TOKEN is documented here'"), (None, None))
        self.assertEqual(self.guard("cat <<'EOF'\nGH_TOKEN=x gh pr create\nEOF"), (None, None))
        for cmd in ("GH_TOKEN=x gh pr create", "export GH_HOST=h; gh pr create", "env GITHUB_TOKEN=x gh pr create",
                    'bash -c "GH_TOKEN=x gh pr create"'):
            self.assertTrue(any(self.guard(cmd)), cmd)


class NotifyTests(unittest.TestCase):
    def msg(self, **kw):
        base = dict(kind="BLOCKER", project="Callback", issue=12, situation="CI failed twice on the PR. The fix did not help.",
                    need="Decide whether to retry or close the PR.", options=["Retry once more", "Close the PR"],
                    recommended=2, detail="https://example.test/log")
        return h.format_message(**{**base, **kw})

    def test_standard_shape(self):
        out = self.msg()
        self.assertEqual(out.splitlines(), [
            "[BLOCKER]  Callback #12",
            "What is going on:   CI failed twice on the PR. The fix did not help.",
            "What I need:        Decide whether to retry or close the PR.",
            "Options:", "  1. Retry once more", "  2. Close the PR  (recommended)",
            "Detail:             https://example.test/log"])

    def test_done_needs_no_decision(self):
        out = self.msg(kind="done", need="", options=None, recommended=None, detail="", issue="#7")
        self.assertIn("[DONE]  Callback #7", out)
        self.assertIn("Nothing, for your information.", out)
        self.assertNotIn("Options", out)

    def test_bad_messages_are_rejected(self):
        for kw in ({"kind": "FYI"}, {"situation": "One. Two. Three. Four."}, {"situation": " "}, {"need": ""},
                   {"need": "a\nb"}, {"options": ["only one"], "recommended": 1}, {"options": list("abcd"), "recommended": 1},
                   {"recommended": None}, {"recommended": 3}, {"kind": "DONE", "need": ""}):
            with self.assertRaises(ValueError, msg=kw):
                self.msg(**kw)
        with self.assertRaises(ValueError):
            self.msg(options=None, recommended=2)

    def test_sentence_count(self):
        self.assertEqual(h.count_sentences("One. Two! Three? Four."), 4)
        self.assertEqual(h.count_sentences("Version 1.2 failed in step 3."), 1)

    def test_notify_terminal_and_unknown_channels(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            h.notify("hello")
        self.assertIn("hello", out.getvalue())
        with self.assertRaises(ValueError):
            h.notify("x", ("pager",))

    def test_push_is_scaffolding_and_never_loses_the_message(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            h.notify("hello", ("push",))
        self.assertIn("hello", out.getvalue())
        self.assertIn("not built", err.getvalue())

    def test_a_new_channel_is_one_class(self):
        got = []

        class Memory(h.Channel):
            name = "memory"

            def send(self, text):
                got.append(text)

        with mock.patch.dict(h.CHANNELS, {"memory": Memory}), contextlib.redirect_stdout(io.StringIO()):
            h.notify("hi", ("memory",))
        self.assertEqual(got, ["hi"])


def issue(key, deps=(), **kw):
    return {"key": key, "title": f"Do {key}", "body": f"Body of {key}.", "acceptance": [f"{key} works"],
            "depends_on": list(deps), **kw}


class IssuePlanTests(unittest.TestCase):
    def test_valid_plan_and_order(self):
        plan = {"issues": [issue("c", ["b"]), issue("a"), issue("b", ["a"])]}
        self.assertEqual(h.validate_issue_plan(plan), [])
        self.assertEqual(h.issue_order(plan), ["a", "b", "c"])

    def test_problems_are_reported(self):
        def errs(*issues):
            return " | ".join(h.validate_issue_plan({"issues": list(issues)}))
        self.assertIn("at least one", errs())
        self.assertIn("duplicate key", errs(issue("a"), issue("a", title="Other")))
        self.assertIn("share a title", errs(issue("a", title="Same"), issue("b", title="Same")))
        self.assertIn("unknown key", errs(issue("a", ["zzz"])))
        self.assertIn("depends on itself", errs(issue("a", ["a"])))
        self.assertIn("cycle", errs(issue("a", ["b"]), issue("b", ["a"])))
        self.assertIn("acceptance", errs(issue("a", acceptance=[])))
        self.assertIn("acceptance", errs(issue("a", acceptance=["ok", " "])))
        self.assertIn("body", errs(issue("a", body=" ")))
        self.assertIn("title", errs(issue("a", title="x")))
        self.assertIn("key", errs(issue("Bad Key")))
        self.assertIn("labels", errs(issue("a", labels="enhancement")))
        self.assertIn("must be an object", errs("nope"))
        self.assertIn("at least one", " ".join(h.validate_issue_plan([])))

    def test_body_has_criteria_and_dependencies(self):
        body = h.render_issue_body(issue("b", ["a"]), {"a": 7})
        self.assertIn("## Acceptance criteria\n- [ ] b works", body)
        self.assertTrue(body.rstrip().endswith("Depends on #7"))
        self.assertNotIn("Depends on", h.render_issue_body(issue("a"), {}))

    def setUp(self):
        self.out = io.StringIO()

    def run_create(self, plan, existing=(), dry_run=False, have_labels=("enhancement", "bug")):
        calls = []

        def fake_gh(*args, input_text=None, **kw):
            calls.append((args, input_text))
            return f"https://github.com/o/r/issues/{100 + sum(1 for c, _ in calls if c[:2] == ('issue', 'create'))}\n"

        def fake_json(*args):
            if args[:2] == ("label", "list"):
                return [{"name": n} for n in have_labels]
            return [{"title": t, "number": n} for t, n in existing]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(h, "ROOT", Path(tmp)), \
                mock.patch.object(h, "gh", fake_gh), mock.patch.object(h, "gh_json", fake_json), \
                mock.patch.object(h, "slug_of", lambda cfg, r: "o/r"), mock.patch.object(h, "repos", lambda cfg: {"app": {}}), \
                mock.patch.object(h, "load_issue_plan", lambda f: plan), contextlib.redirect_stdout(self.out):
            a = mock.Mock(file="x", repo=None, dry_run=dry_run)
            h.cmd_factory_issues_create(a, {})
            log = (Path(tmp) / ".factory" / "log.jsonl")
            logged = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return calls, logged

    def test_create_makes_dependencies_first_and_links_numbers(self):
        plan = {"issues": [issue("b", ["a"]), issue("a")]}
        calls, logged = self.run_create(plan)
        creates = [(c, body) for c, body in calls if c[:2] == ("issue", "create")]
        self.assertEqual([c[c.index("--title") + 1] for c, _ in creates], ["Do a", "Do b"])
        self.assertIn("Depends on #101", creates[1][1])
        self.assertNotIn("todo", [c[i + 1] for c, _ in creates for i, x in enumerate(c) if x == "--label"])
        self.assertEqual([(e["key"], e["number"]) for e in logged], [("a", 101), ("b", 102)])

    def test_create_skips_existing_titles(self):
        plan = {"issues": [issue("b", ["a"]), issue("a")]}
        calls, logged = self.run_create(plan, existing=[("Do a", 5)])
        creates = [(c, body) for c, body in calls if c[:2] == ("issue", "create")]
        self.assertEqual(len(creates), 1)
        self.assertIn("Depends on #5", creates[0][1])
        self.assertNotIn("todo", creates[0][0])  # the user names the issues to run; creation never queues them

    def test_missing_non_factory_label_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.run_create({"issues": [issue("a", labels=["wizardry"])]})

    def test_dry_run_creates_nothing_and_prints_the_order(self):
        calls, logged = self.run_create({"issues": [issue("b", ["a"]), issue("a")]}, dry_run=True)
        self.assertEqual((calls, logged), ([], []))
        self.assertIn("2 issues would be created", self.out.getvalue())

    def test_problems_stop_creation_before_anything_is_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "i.json"
            f.write_text(json.dumps({"issues": [issue("a", ["b"]), issue("b", ["a"])]}))
            with self.assertRaises(SystemExit) as cm, mock.patch.object(h, "gh") as gh:
                h.load_issue_plan(str(f))
            self.assertIn("cycle", str(cm.exception))
            gh.assert_not_called()

    def test_decide_appends_to_the_decisions_file(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(h, "ROOT", Path(tmp)), \
                contextlib.redirect_stdout(io.StringIO()):
            for q, by in (("Who can delete a resume?", "user"), ("Max upload size?", "assumed")):
                h.cmd_factory_decide(mock.Mock(question=q, answer="Only the owner.\nAlways.", by=by, kind="product",
                                               why="vision doc", source="MVP.md:12"), {})
            text = (Path(tmp) / ".factory" / "decisions.md").read_text()
            log = (Path(tmp) / ".factory" / "log.jsonl").read_text().splitlines()
        self.assertEqual(text.count("# Decisions"), 1)
        self.assertIn("## Who can delete a resume?", text)
        self.assertIn("- Decision: Only the owner. Always.", text)
        self.assertIn("- Decided by: assumed on ", text)
        self.assertIn("- Source: MVP.md:12", text)
        self.assertEqual(len(log), 2)


class ResumeParkedBranchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], check=True, capture_output=True, text=True).stdout.strip()
        self.git("init", "-q", "-b", "main")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
        self.cfg = {"git": {"base_branch": "main"}, "repos": {"app": {"path": "."}}}

    def resume(self, n):
        with mock.patch.object(h, "ROOT", self.root):
            return h.resume_parked_branch(self.cfg, n)

    def test_switches_to_the_issues_parked_branch(self):
        self.git("branch", "fix/14-no-csrf")
        self.git("branch", "feat/15-other")
        self.assertEqual(self.resume(14), ["app:fix/14-no-csrf"])
        self.assertEqual(self.git("branch", "--show-current"), "fix/14-no-csrf")

    def test_no_or_several_matches_leave_the_tree_on_base(self):
        self.assertEqual(self.resume(14), [])
        self.git("branch", "fix/14-a")
        self.git("branch", "feat/14-b")
        self.assertEqual(self.resume(14), [])
        self.assertEqual(self.git("branch", "--show-current"), "main")

    def test_a_repo_not_on_base_is_left_alone(self):
        self.git("switch", "-q", "-c", "chore/1-x")
        self.git("branch", "fix/14-a")
        self.assertEqual(self.resume(14), [])


class TamperScanTests(unittest.TestCase):
    def scan(self, diff):
        return h.scan_diff_for_tampering("app", diff)

    def kinds(self, diff):
        return sorted(f["kind"] for f in self.scan(diff))

    def test_altered_and_removed_assertions_and_cases(self):
        diff = ("diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
                "@@ -3,3 +3,2 @@\n-    assert total == 5\n+    assert total == 6\n-def test_old():\n")
        found = self.scan(diff)
        self.assertEqual(sorted(f["kind"] for f in found), ["altered-assertion", "removed-test-case"])
        self.assertEqual(found[0]["file"], "tests/test_a.py")
        self.assertTrue(all(f["id"].startswith("T-") for f in found))

    def test_moved_or_reformatted_lines_are_not_flagged(self):
        diff = ("diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
                "@@ -1,1 +1,1 @@\n-    assert x   ==   1\n+    assert x == 1\n")
        self.assertEqual(self.scan(diff), [])

    def test_pure_additions_and_new_files_are_fine(self):
        added = ("diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
                 "@@ -1,0 +2,2 @@\n+def test_new():\n+    assert 1\n")
        new_file = ("diff --git a/tests/test_b.py b/tests/test_b.py\nnew file mode 100644\n--- /dev/null\n"
                    "+++ b/tests/test_b.py\n@@ -0,0 +1,1 @@\n+@pytest.mark.skip\n")
        self.assertEqual(self.scan(added + new_file), [])

    def test_skip_markers_and_deleted_test_files(self):
        skip = ("diff --git a/src/a.test.ts b/src/a.test.ts\n--- a/src/a.test.ts\n+++ b/src/a.test.ts\n"
                "@@ -1,1 +1,2 @@\n+it.skip('x', () => {})\n")
        gone = "diff --git a/tests/test_c.py b/tests/test_c.py\ndeleted file mode 100644\n--- a/tests/test_c.py\n+++ /dev/null\n"
        self.assertEqual(self.kinds(skip + gone), ["deleted-test-file", "skip-marker"])

    def test_content_lines_that_look_like_headers_are_scanned(self):
        diff = ("diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n"
                "@@ -1,2 +1,1 @@\n--- assert x == 1\n")
        self.assertEqual(self.kinds(diff), ["altered-assertion"])

    def test_non_test_files_are_ignored(self):
        diff = ("diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
                "@@ -1,1 +1,1 @@\n-    assert ok\n+    pass\n")
        self.assertEqual(self.scan(diff), [])

    def test_config_only_flags_risky_lines(self):
        risky = ("diff --git a/jest.config.js b/jest.config.js\n--- a/jest.config.js\n+++ b/jest.config.js\n"
                 "@@ -1,1 +1,1 @@\n-  coverageThreshold: 90,\n+  coverageThreshold: 10,\n")
        calm = ("diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n"
                "@@ -1,1 +1,1 @@\n-  \"left-pad\": \"1.0.0\",\n+  \"left-pad\": \"1.1.0\",\n")
        self.assertEqual(self.kinds(risky), ["test-config-changed", "test-config-changed"])
        self.assertEqual(self.scan(calm), [])

    def test_snapshot_loosening(self):
        diff = ("diff --git a/__snapshots__/a.snap b/__snapshots__/a.snap\n--- a/__snapshots__/a.snap\n"
                "+++ b/__snapshots__/a.snap\n@@ -1,2 +1,1 @@\n-exports[`a`] = `x`\n")
        self.assertEqual(self.kinds(diff), ["snapshot-loosened"])


class SubagentStopTests(unittest.TestCase):
    """hook_subagent_stop: only harness agents or agents inside a run are logged."""

    def run_hook(self, payload, rid=None, path=None):
        events = []
        with mock.patch.object(h, "current_run_id", return_value=rid), \
                mock.patch.object(h, "find_agent_transcript", return_value=path) as find, \
                mock.patch.object(h, "log_event", events.append), \
                mock.patch.object(h.time, "sleep"), \
                mock.patch.object(h.sys, "stdin", io.StringIO(json.dumps(payload))):
            h.hook_subagent_stop({})
        return events, find

    def test_non_harness_agent_outside_a_run_is_skipped(self):
        for agent_type in ("Explore", "general-purpose", None):
            events, find = self.run_hook({"agent_id": "a1", "agent_type": agent_type})
            self.assertEqual(events, [])
            find.assert_not_called()

    def test_harness_agent_with_missing_transcript_logs_an_error(self):
        events, _ = self.run_hook({"agent_id": "a1", "agent_type": "harness:scout"})
        self.assertEqual([(e["stage"], e["error"]) for e in events], [("scout", "transcript not found")])

    def test_missing_transcript_error_records_the_payload(self):
        events, _ = self.run_hook({"agent_id": "a1", "agent_type": "harness:scout", "cwd": "/x", "extra": 1})
        self.assertEqual(events[0]["payload"]["cwd"], "/x")
        self.assertNotIn("extra", events[0]["payload"])

    def test_transcript_is_found_under_another_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp) / "projects"
            mine = projects / "-mine"
            mine.mkdir(parents=True)
            other = projects / "-other" / "sess" / "subagents"
            other.mkdir(parents=True)
            (other / "agent-a9.jsonl").write_text("{}\n")
            found = h.find_agent_transcript({"agent_id": "a9", "session_id": "s", "transcript_path": str(mine / "s.jsonl")})
        self.assertEqual(found.name, "agent-a9.jsonl")

    def test_agent_inside_a_run_with_missing_transcript_logs_an_error(self):
        with mock.patch.object(h, "RUNS_DIR", Path(tempfile.gettempdir())):
            events, _ = self.run_hook({"agent_id": "a1", "agent_type": "Explore"}, rid="r1")
        self.assertEqual([(e["run_id"], e["stage"]) for e in events], [("r1", "Explore")])

    def test_normal_case_logs_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = Path(tmp) / "agent.jsonl"
            tr.write_text(json.dumps({"type": "assistant", "message": {
                "id": "m1", "model": "claude-sonnet-5-5", "content": [], "usage": {"input_tokens": 5, "output_tokens": 2}}}) + "\n")
            with mock.patch.object(h, "SEEN_DIR", Path(tmp) / "seen"):
                events, _ = self.run_hook({"agent_id": "a1", "agent_type": "harness:engineer"}, path=tr)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["stage"], "engineer")
        self.assertEqual(events[0]["gen_ai.usage.input_tokens"], 5)

    def test_unattributed_events_are_counted(self):
        evs = [{"kind": "agent", "stage": "unknown", "error": "transcript not found"},
               {"kind": "agent", "stage": "scout", "error": "transcript not found"},
               {"kind": "agent", "stage": "scout"}, {"kind": "gate"}]
        self.assertEqual(len(h.unattributed_agent_events(evs)), 2)

    def test_legacy_lost_events_are_split_out(self):
        old = {"kind": "agent", "stage": "unknown", "error": "transcript not found"}
        new = {**old, "payload": {"agent_type": None}}
        self.assertEqual(h.legacy_lost_agent_events([old, new]), [old])


def assistant_line(mid, inp, out=1, cache_read=0, cache_write=0):
    return json.dumps({"type": "assistant", "message": {"id": mid, "model": "claude-sonnet-5-5", "content": [], "usage": {
        "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write}}}) + "\n"


class RequestSizeTests(unittest.TestCase):
    def test_first_and_peak_request_count_all_input_tokens(self):
        entries = [json.loads(assistant_line("m1", 10, cache_write=5000)),
                   json.loads(assistant_line("m2", 20, cache_read=5000, cache_write=3000)),
                   json.loads(assistant_line("m3", 5, cache_read=8000))]
        r = h.summarize_entries(entries, {}, set())
        self.assertEqual((r["first_request_tokens"], r["peak_request_tokens"]), (5010, 8020))

    def test_no_requests(self):
        r = h.summarize_entries([], {}, set())
        self.assertEqual((r["first_request_tokens"], r["peak_request_tokens"]), (None, None))


def tool_entries(*calls):
    """Transcript entries: one assistant request per call (tool, target, result text), as Claude Code writes them."""
    out = []
    for i, (tool, target, result) in enumerate(calls, 1):
        key = {"Read": "file_path", "Bash": "command", "Grep": "pattern"}[tool]
        out.append({"type": "assistant", "message": {"id": f"m{i}", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": tool, "input": {key: target}}]}})
        out.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": result}]}})
        out.append({"type": "api-request-blob", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "dup"}]}})
    return out


class ToolCallTests(unittest.TestCase):
    def test_result_size_and_requests_that_carried_it(self):
        calls = h.tool_calls(tool_entries(("Read", "/a.py", "x" * 400), ("Bash", "ls", "y" * 40), ("Grep", "foo", "")))
        self.assertEqual([(c["tool"], c["target"], c["chars"], c["later_requests"]) for c in calls],
                         [("Read", "/a.py", 400, 2), ("Bash", "ls", 40, 1), ("Grep", "foo", 0, 0)])

    def test_streamed_blocks_of_one_request_count_once(self):
        e = tool_entries(("Read", "/a.py", "x" * 8))
        calls = h.tool_calls([e[0], e[0], e[1]])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["later_requests"], 0)

    def test_list_content_and_missing_result(self):
        e = tool_entries(("Read", "/a.py", ""), ("Bash", "ls", ""))
        e[1]["message"]["content"][0]["content"] = [{"type": "text", "text": "abc"}, {"type": "text", "text": "de"}]
        del e[4]  # the second call never got a result
        calls = h.tool_calls(e)
        self.assertEqual([(c["tool"], c["chars"]) for c in calls], [("Read", 5)])

    def test_report_totals_and_repeated_reads(self):
        sessions = [("engineer", "r1", tool_entries(("Read", "/p/plan.md", "x" * 400))),
                    ("scout", "r1", tool_entries(("Read", "/p/plan.md", "x" * 400), ("Bash", "ls", "y"))),
                    ("scout", "r2", tool_entries(("Read", "/p/plan.md", "x" * 400)))]
        buf = io.StringIO()
        with mock.patch.object(h, "transcripts_for", return_value=sessions), mock.patch.object(h.sys, "stdout", buf):
            h.print_tool_report([], 5)
        out = buf.getvalue()
        self.assertRegex(out, r"Read\s+3\s")
        self.assertIn("1 file(s) in total, 1 repeat read(s)", out)  # r1 read plan.md twice; r2 once

    def test_no_transcripts(self):
        buf = io.StringIO()
        with mock.patch.object(h, "transcripts_for", return_value=[]), mock.patch.object(h.sys, "stdout", buf):
            h.print_tool_report([], 5)
        self.assertIn("no transcripts found", buf.getvalue())

    def test_locate_transcript_finds_subagent_and_session_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "projects" / "-p" / "s1" / "subagents"
            sub.mkdir(parents=True)
            (sub / "agent-a1.jsonl").write_text("{}")
            (Path(tmp) / "projects" / "-p" / "sid9.jsonl").write_text("{}")
            with mock.patch.dict(h.os.environ, {"CLAUDE_CONFIG_DIR": tmp}):
                self.assertEqual(h.locate_transcript("a1").name, "agent-a1.jsonl")
                self.assertEqual(h.locate_transcript("sid9").name, "sid9.jsonl")
                self.assertIsNone(h.locate_transcript("nope"))


class ClaudeMdTemplateTests(unittest.TestCase):
    """Every agent pays for the generated CLAUDE.md on every request: it stays short and holds only project facts."""

    def render(self):
        return h.CLAUDE_MD_TEMPLATE.format(name="demo", description="A demo.", repo_lines="- `app` (app): node")

    def test_keeps_the_sections_agents_need(self):
        md = self.render()
        for heading in ("## What this is", "## Repos", "## Commands", "## Conventions and rules"):
            self.assertIn(heading, md)
        self.assertIn("A demo.", md)

    def test_stays_short_and_has_no_harness_boilerplate(self):
        md = self.render()
        self.assertLess(len(md), 800)  # about 200 tokens before the project fills it in
        self.assertNotIn("/ship", md)
        self.assertNotIn(".harness/", md)

    def test_init_skill_does_not_ask_for_a_harness_section(self):
        skill = (h.PLUGIN_DIR / "skills" / "harness-init" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn('"Harness" section', skill)


class SessionStartTests(unittest.TestCase):
    def entries(self):
        usage = lambda ctx: {"input_tokens": ctx, "output_tokens": 1}
        asst = lambda mid, ts, ctx: {"type": "assistant", "timestamp": ts, "message": {
            "id": mid, "model": "claude-sonnet-5-5", "content": [], "usage": usage(ctx)}}
        return [
            {"type": "queue-operation"},
            {"type": "attachment", "attachment": {"type": "skill_listing", "content": "s" * 400}},
            {"type": "attachment", "attachment": {"type": "skill_listing", "content": "s" * 400}},
            {"type": "attachment", "attachment": {"type": "prompt_snapshot", "systemPrompt": ["x" * 400]}},
            {"type": "user", "message": {"content": "p" * 100}},
            {"type": "api-request-shape", "shape": {"system": [{"text": "x" * 40}], "tools": [{"name": "t" * 80}]}},
            asst("m1", "2026-11-11T10:00:00Z", 26000),
            {"type": "attachment", "attachment": {"type": "date"}},  # after the first request: not startup
            asst("m1", "2026-11-11T10:00:01Z", 26000),  # a second block of the same request
            asst("m2", "2026-11-11T10:05:00Z", 30000),
            asst("m3", "2026-11-11T10:20:00Z", 40000),
            {"type": "assistant", "isSidechain": True, "timestamp": "2026-11-11T10:06:00Z", "message": {
                "id": "s1", "model": "claude-sonnet-5-5", "content": [], "usage": usage(9)}},
        ]

    def test_startup_parts_and_context_at_run_start(self):
        st = h.session_start(self.entries(), h.parse_ts("2026-11-11T10:10:00Z"))
        self.assertEqual(st["first_request_tokens"], 26000)
        self.assertEqual(st["requests_before_run"], 2)
        self.assertEqual(st["context_at_run_start"], 30000)
        self.assertEqual(sorted(st["parts"]), ["prompt", "skill_listing", "system prompt", "tool definitions"])
        self.assertGreater(st["parts"]["skill_listing"], 800)  # both attachments summed
        self.assertNotIn("date", st["parts"])
        self.assertNotIn("prompt_snapshot", st["parts"])

    def test_run_that_started_with_the_session(self):
        st = h.session_start(self.entries(), h.parse_ts("2026-11-11T09:00:00Z"))
        self.assertEqual((st["requests_before_run"], st["context_at_run_start"]), (0, None))

    def test_shape_without_text_adds_no_parts(self):
        e = self.entries()
        i = next(i for i, x in enumerate(e) if x["type"] == "api-request-shape")
        e[i] = {"type": "api-request-shape", "shape": {"system": "list[3] of dict", "tools": "list[12] of dict"}}
        self.assertNotIn("system prompt", h.session_start(e)["parts"])

    def test_no_requests(self):
        self.assertIsNone(h.session_start([{"type": "attachment", "attachment": {"type": "date"}}]))


class SessionUsageTests(unittest.TestCase):
    """log_session_usage: supervisor-launched `claude -p` sessions are metered from their transcript."""

    def log(self, transcript=None, issue=None, found=None):
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            if transcript:
                d = Path(tmp) / "projects" / "-proj"
                d.mkdir(parents=True)
                (d / "sid1.jsonl").write_text(transcript)
            with mock.patch.dict(h.os.environ, {"CLAUDE_CONFIG_DIR": tmp}), \
                    mock.patch.object(h, "issue_run", return_value=found), \
                    mock.patch.object(h, "log_event", events.append):
                h.log_session_usage("harness-cleanup", "sid1", {}, issue)
        return events

    def test_logs_usage_against_the_issue_and_its_run(self):
        events = self.log(assistant_line("m1", 5, 2), issue=7, found=(Path("x"), {"id": "run-7"}))
        self.assertEqual((events[0]["stage"], events[0]["issue"], events[0]["run_id"]), ("harness-cleanup", 7, "run-7"))
        self.assertEqual(events[0]["gen_ai.usage.input_tokens"], 5)

    def test_session_without_an_issue(self):
        events = self.log(assistant_line("m1", 5))
        self.assertNotIn("issue", events[0])
        self.assertIsNone(events[0]["run_id"])

    def test_missing_transcript_is_logged_as_an_error(self):
        events = self.log(None)
        self.assertEqual(events[0]["error"], "transcript not found")

    def test_metered_command_names_the_session(self):
        command, sid = h.metered_command("-p", "go")
        self.assertEqual(command[-2:], ["--session-id", sid])
        self.assertEqual(str(h.uuid.UUID(sid)), sid)


class IssueRollupTests(unittest.TestCase):
    def test_sums_every_run_and_supervisor_session_of_an_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rid, n in (("r1", 1), ("r2", 1), ("r3", 2)):
                h.write_json(Path(tmp) / rid / "run.json", {"issue": {"number": n}})
            ev = lambda **k: {"kind": "agent", "gen_ai.usage.input_tokens": 1000, **k}
            events = [ev(run_id="r1", stage="planner", cost_usd_est=1.0), ev(run_id="r2", stage="engineer", cost_usd_est=0.5),
                      ev(run_id=None, issue=1, stage="harness-cleanup", cost_usd_est=0.25),
                      ev(run_id="r3", stage="planner", cost_usd_est=2.0), ev(run_id="r3", stage="x", error="no"),
                      ev(run_id="nope", stage="scout", cost_usd_est=9.0),
                      {"kind": "run_end", "run_id": "r1", "status": "aborted"}, {"kind": "run_end", "run_id": "r2", "status": "done"}]
            with mock.patch.object(h, "RUNS_DIR", Path(tmp)):
                out = h.issue_rollup(events)
        self.assertEqual(sorted(out), [1, 2])
        self.assertEqual(round(out[1]["cost"], 2), 1.75)
        self.assertEqual(out[1]["runs"], {"r1": "aborted", "r2": "done"})
        self.assertEqual(out[1]["context"], 3000)
        self.assertEqual(out[2]["cost"], 2.0)


class ReviewRoundsTests(unittest.TestCase):
    def count(self, files, recorded=0):
        with tempfile.TemporaryDirectory() as tmp:
            for f in files:
                (Path(tmp) / f).write_text("{}")
            return h.count_review_rounds(Path(tmp), {"review_rounds": recorded})

    def test_counts_review_files_the_claude_reviewer_wrote(self):
        self.assertEqual(self.count(["review-1.json", "review-2.json", "verified-2.json", "plan.md"]), 2)

    def test_keeps_a_higher_recorded_count(self):
        self.assertEqual(self.count(["review-1.json"], recorded=3), 3)

    def test_no_reviews(self):
        self.assertEqual(self.count(["plan.md"]), 0)


class ValidateTests(unittest.TestCase):
    """harness validate: plan, review, verified and fix handoff files."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "app").mkdir()
        (self.root / "app" / "a.py").write_text("x = 1\n")
        self.cfg = {"repos": {"app": {"path": "app"}, "web": {"path": "web"}}}
        p = mock.patch.object(h, "ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.d = self.root / "run"
        self.d.mkdir()

    def task(self, **kw):
        return {"id": "T1", "title": "t", "repo": "app", "depends_on": [], "model": "sonnet",
                "files": ["a.py"], "acceptance": ["tests pass"], **kw}

    def plan(self, *tasks, repos=("app",)):
        return {"goal": "g", "repos": list(repos), "tasks": list(tasks) or [self.task()]}

    def finding(self, n=1, rnd=1, **kw):
        return {"id": f"R{rnd}-{n}", "severity": "blocking", "repo": "app", "file": "a.py", "line": 3,
                "title": "t", "detail": "d", **kw}

    def has(self, errors, text):
        self.assertTrue(any(text in e for e in errors), errors)

    def test_valid_plan(self):
        self.assertEqual(h.validate_plan(self.plan(self.task(), self.task(id="T2", depends_on=["T1"],
                                                                         files=["new.py (create)"])), self.cfg), [])

    def test_plan_failures(self):
        cases = [
            (self.plan(self.task(), self.task()), "duplicate id"),
            (self.plan(self.task(repo="nope")), "unknown repo 'nope'"),
            (self.plan(self.task(repo="web"), repos=("app",)), "not in the top-level"),
            (self.plan(self.task(), repos=("app", "ghost")), "unknown repo 'ghost'"),
            (self.plan(self.task(depends_on=["T9"])), "unknown task 'T9'"),
            (self.plan(self.task(depends_on=["T1"])), "depends on itself"),
            (self.plan(self.task(depends_on=["T2"]), self.task(id="T2", depends_on=["T1"])), "cycle"),
            (self.plan(self.task(model="gpt")), "model must be"),
            (self.plan(self.task(files=["missing.py"])), "does not exist"),
            (self.plan(self.task(acceptance=[])), "acceptance"),
            ({"tasks": []}, "at least one task"),
        ]
        for plan, text in cases:
            with self.subTest(text):
                self.has(h.validate_plan(plan, self.cfg), text)

    def test_created_files_need_no_marker_when_present_and_marker_is_stripped_for_gates(self):
        self.assertEqual(h.CREATE_MARK_RE.sub("", "new.py (create)"), "new.py")
        h.write_json(self.d / "tasks.json", self.plan(self.task(files=["a.py", "new.py (create)"])))
        self.assertEqual(h.planned_files(self.d, "app"), ["a.py", "new.py"])

    def test_valid_review(self):
        self.assertEqual(h.validate_review({"round": 1, "findings": [self.finding(1), self.finding(2, line=None)]}, 1), [])

    def test_review_failures(self):
        cases = [
            ([self.finding(1, severity="major")], "severity must be"),
            ([self.finding(1), self.finding(1)], "duplicate id"),
            ([self.finding(1, rnd=2)], "id must look like R1-"),
            ([self.finding(1, line="3")], "line must be an integer"),
            ([self.finding(1, file="")], "file is required"),
            ([self.finding(1, detail=None)], "detail is required"),
            ([self.finding(1, status="open")], "status must be"),
            (["nope"], "must be an object"),
        ]
        for findings, text in cases:
            with self.subTest(text):
                self.has(h.validate_review({"round": 1, "findings": findings}, 1), text)
        self.has(h.validate_review({"findings": "x"}, 1), "findings")
        self.has(h.validate_review({"round": 2, "findings": []}, 1), "expected 1")

    def test_verified(self):
        review = {"findings": [self.finding(1), self.finding(2)]}
        ok = {"findings": [{"id": "R1-1", "verdict": "confirmed", "severity": "nit"},
                           {"id": "R1-2", "verdict": "rejected"}],
              "additional": [{"severity": "should_fix", "file": "a.py", "line": 1, "title": "t", "detail": "d"}]}
        self.assertEqual(h.validate_verified(ok, 1, review), [])
        self.has(h.validate_verified({"findings": ok["findings"][:1]}, 1, review), "does not judge R1-2")
        self.has(h.validate_verified({"findings": [*ok["findings"], {"id": "R1-9", "verdict": "confirmed"}]}, 1, review),
                 "not in review-1.json")
        self.has(h.validate_verified({"findings": [{"id": "R1-1", "verdict": "maybe"}, ok["findings"][1]]}, 1, review),
                 "verdict must be")
        self.has(h.validate_verified({"findings": [*ok["findings"], ok["findings"][0]]}, 1, review), "judged twice")

    def test_fix(self):
        self.assertEqual(h.validate_fix({"round": 1, "findings": [self.finding(1), {**self.finding(1), "id": "V1-1"}]}, 1), [])
        self.has(h.validate_fix({"round": 1, "findings": []}, 1), "nothing to fix")

    def run_validate(self, what, rnd=None):
        out = io.StringIO()
        args = mock.Mock(what=what, round=rnd)
        with mock.patch.object(h, "load_run", return_value=(self.d, {})), contextlib.redirect_stdout(out):
            try:
                h.cmd_validate(args, self.cfg)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_command_reports_ok_missing_and_invalid(self):
        self.assertEqual(self.run_validate("plan")[0], 2)
        h.write_json(self.d / "tasks.json", self.plan())
        self.assertEqual(self.run_validate("plan"), (0, "OK: tasks.json\n"))
        h.write_json(self.d / "tasks.json", self.plan(self.task(acceptance=[])))
        code, out = self.run_validate("plan")
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("INVALID: tasks.json:"))
        (self.d / "review-1.json").write_text("{not json")
        self.assertIn("not valid JSON", self.run_validate("review", 1)[1])

    def test_verdict_stops_on_a_malformed_review(self):
        h.write_json(self.d / "review-1.json", {"round": 1, "findings": [self.finding(1, severity="major")]})
        h.write_json(self.d / "verified-1.json", {"findings": []})
        out = io.StringIO()
        with mock.patch.object(h, "load_run", return_value=(self.d, {"id": "r1"})), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                h.cmd_verdict(mock.Mock(round=1), {})
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("INVALID: review-1.json", out.getvalue())
        self.assertIn("claude-reviewer", out.getvalue())


class FallbackReviewerTests(ValidateTests):
    """No outside-model key: the claude-reviewer subagent writes review-<n>.json and the loop still works."""

    # Exactly the shape agents/claude-reviewer.md tells the subagent to write.
    FALLBACK_REVIEW = {"round": 1, "provider": "claude", "model": "claude-reviewer", "summary": "s",
                       "findings": [{"id": "R1-1", "severity": "blocking", "repo": "app", "file": "a.py", "line": 0,
                                     "title": "t", "evidence": "x = 1", "detail": "d", "suggestion": "s", "status": "new"},
                                    {"id": "R1-2", "severity": "nit", "repo": "app", "file": "a.py", "line": 7,
                                     "title": "t", "evidence": "e", "detail": "d", "suggestion": "s", "status": "unresolved"}]}

    def test_review_without_a_key_falls_back_and_writes_nothing(self):
        cfg = {"reviewer": {"provider": "deepseek"},
               "providers": {"deepseek": {"api_key_env": "HARNESS_TEST_NO_SUCH_KEY", "model": "m"}}}
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(h, "load_run", return_value=(self.d, {"id": "r1"})), contextlib.redirect_stdout(out):
            os.environ.pop("HARNESS_TEST_NO_SUCH_KEY", None)
            with self.assertRaises(SystemExit) as cm:
                h.cmd_review(mock.Mock(round=1, provider=None), cfg)
        self.assertEqual(cm.exception.code, 4)
        self.assertIn("FALLBACK", out.getvalue())
        self.assertFalse((self.d / "review-1.json").exists())

    def test_fallback_review_file_is_valid(self):
        self.assertEqual(h.validate_review(self.FALLBACK_REVIEW, 1), [])
        h.write_json(self.d / "review-1.json", self.FALLBACK_REVIEW)
        self.assertEqual(self.run_validate("review", 1), (0, "OK: review-1.json\n"))

    def run_verdict(self, review, verified):
        h.write_json(self.d / "review-1.json", review)
        h.write_json(self.d / "verified-1.json", verified)
        out = io.StringIO()
        code = 0
        with mock.patch.object(h, "load_run", return_value=(self.d, {"id": "r1", "mode": "full"})), \
                mock.patch.object(h, "tamper_scan", return_value=[]), \
                mock.patch.object(h, "log_event"), contextlib.redirect_stdout(out):
            try:
                h.cmd_verdict(mock.Mock(round=1), {})
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_verdict_runs_end_to_end_on_a_fallback_review(self):
        verified = {"findings": [{"id": "R1-1", "verdict": "confirmed"}, {"id": "R1-2", "verdict": "rejected"}]}
        code, out = self.run_verdict(self.FALLBACK_REVIEW, verified)
        self.assertEqual(code, 0)
        self.assertIn("DECISION: FIX", out)

    def test_invalid_fallback_review_is_resumed_not_respawned(self):
        bad = {**self.FALLBACK_REVIEW, "findings": [{**self.FALLBACK_REVIEW["findings"][0], "line": "12"}]}
        code, out = self.run_verdict(bad, {"findings": []})
        self.assertEqual(code, 2)
        self.assertIn("line must be an integer", out)
        self.assertIn("Resume the claude-reviewer", out)

    def test_invalid_outside_model_review_spawns_the_fallback(self):
        bad = {"round": 1, "provider": "deepseek", "findings": [{"id": "R1-1", "severity": "should_fix", "file": "a.py"}]}
        code, out = self.run_verdict(bad, {"findings": []})
        self.assertEqual(code, 2)
        self.assertIn("Spawn the harness:claude-reviewer", out)


class GateEnforcementTests(unittest.TestCase):
    """The engineer SubagentStop hook, the tamper scan in a real repo, and the verdict that uses it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "app"
        (self.repo / "tests").mkdir(parents=True)
        self.git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True, text=True).stdout.strip()
        self.git("init", "-q", "-b", "main")
        (self.repo / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n")
        (self.repo / "a.py").write_text("x = 1\n")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        self.d = self.root / "run"
        self.d.mkdir()
        self.run_ = {"id": "r1", "mode": "full", "repos": {"app": {"base_commit": self.git("rev-parse", "HEAD")}}}
        self.cfg = {"repos": {"app": {"path": "app", "gates": [{"name": "tests", "cmd": "true", "kind": "pytest"}]}}}
        self.transcript = self.root / "agent.jsonl"
        self.events = []
        self.patches = [mock.patch.object(h, "ROOT", self.root)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def edit_transcript(self, *paths):
        blocks = [{"type": "tool_use", "name": "Edit", "input": {"file_path": str(p)}} for p in paths]
        self.transcript.write_text(json.dumps({"type": "assistant", "message": {"content": blocks}}) + "\n")

    def problems(self, msg="GATES PASS"):
        return h.engineer_gate_problems({"last_assistant_message": msg}, self.cfg, self.d, self.run_, self.transcript)

    def record(self, ran=1, passed=True):
        h.record_gate_state(self.d, "app", h.repos(self.cfg)["app"], self.run_, ran, passed)

    def test_no_edits_means_nothing_to_enforce(self):
        self.transcript.write_text("")
        self.assertEqual(self.problems(), [])

    def test_blocked_until_gates_pass_on_the_current_tree(self):
        (self.repo / "a.py").write_text("x = 2\n")
        self.edit_transcript(self.repo / "a.py")
        self.assertIn("never ran the gates", self.problems()[0])
        self.record(passed=False)
        self.assertIn("last gates run failed", self.problems()[0])
        self.record(passed=True)
        self.assertEqual(self.problems(), [])
        (self.repo / "a.py").write_text("x = 3\n")  # edited after the passing run
        self.assertIn("changed after the last gates run", self.problems()[0])

    def test_untracked_new_files_count_toward_the_fingerprint(self):
        self.edit_transcript(self.repo / "new.py")
        (self.repo / "new.py").write_text("a = 1\n")
        self.record()
        (self.repo / "new.py").write_text("a = 2\n")
        self.assertIn("changed after", self.problems()[0])

    def test_escalations_and_honest_failures_may_stop(self):
        (self.repo / "a.py").write_text("x = 2\n")
        self.edit_transcript(self.repo / "a.py")
        self.assertEqual(self.problems("NEEDS USER: which db?"), [])
        self.assertEqual(self.problems("the plan is wrong: T2 is impossible"), [])
        self.assertIn("never ran", self.problems("GATES FAIL: flaky")[0])  # claiming failure needs a real run
        # the words only count when they open the reply or a line, not when a completion mentions them
        self.assertIn("never ran", self.problems("Done. Note the plan is wrong about X but I worked around it")[0])
        self.assertIn("never ran", self.problems("GATES PASS. No NEEDS USER items, no GATES FAIL.")[0])
        self.assertEqual(self.problems("GATES PASS\nNEEDS USER: which db?"), [])
        self.record(passed=False)
        self.assertEqual(self.problems("GATES FAIL: flaky"), [])

    def test_a_repo_with_no_applicable_gate_is_not_enforced(self):
        (self.repo / "a.py").write_text("x = 2\n")
        self.edit_transcript(self.repo / "a.py")
        self.record(ran=0)
        self.assertEqual(self.problems(), [])
        self.cfg["repos"]["app"]["gates"] = []
        self.assertEqual(h.engineer_gate_problems({}, self.cfg, self.d, self.run_, self.transcript), [])

    def test_edits_outside_any_repo_are_ignored(self):
        self.edit_transcript(self.root / "run" / "notes-T1.md")
        self.assertEqual(self.problems(), [])

    def run_hook(self, payload, agent="harness:engineer"):
        payload = {"agent_type": agent, "agent_transcript_path": str(self.transcript), **payload}
        (self.root / ".harness" / "runs").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(h, "current_run_id", lambda: "r1"), mock.patch.object(h, "RUNS_DIR", self.root), \
                mock.patch.object(h, "log_event", self.events.append), \
                mock.patch.object(h.sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            (self.root / "r1").mkdir(exist_ok=True)
            h.write_json(self.root / "r1" / "run.json", self.run_)
            for f in ("gate-state.json",):
                if (self.d / f).exists():
                    (self.root / "r1" / f).write_text((self.d / f).read_text())
            try:
                h.hook_engineer_gates(self.cfg)
            except SystemExit as e:
                return e.code, err.getvalue()
            return 0, err.getvalue()

    def test_hook_exits_2_with_a_reason_and_only_for_engineers(self):
        (self.repo / "a.py").write_text("x = 2\n")
        self.edit_transcript(self.repo / "a.py")
        code, err = self.run_hook({"last_assistant_message": "GATES PASS"})
        self.assertEqual(code, 2)
        self.assertIn("never ran the gates", err)
        self.assertEqual(self.run_hook({}, agent="harness:scout")[0], 0)

    def test_hook_stops_blocking_after_the_limit_and_flags_the_run(self):
        (self.repo / "a.py").write_text("x = 2\n")
        self.edit_transcript(self.repo / "a.py")
        results = [self.run_hook({"agent_id": "a1", "last_assistant_message": "GATES PASS"}) for _ in range(4)]
        self.assertEqual([c for c, _ in results], [2, 2, 2, 0])
        self.assertTrue(all("NEEDS USER: gates command denied" in e for c, e in results if c == 2))
        self.assertIn("Block 3 of 3", results[2][1])
        unenforced = json.loads((self.root / "r1" / "gate-unenforced.json").read_text())
        self.assertEqual(unenforced[0]["agent_id"], "a1")
        self.assertEqual([e["kind"] for e in self.events], ["gate_unenforced"])
        # another engineer starts with its own count
        self.assertEqual(self.run_hook({"agent_id": "a2", "last_assistant_message": "GATES PASS"})[0], 2)

    def test_summary_lists_unenforced_gates(self):
        h.write_json(self.d / "gate-unenforced.json", [{"agent_id": "a1", "problems": ["[app] never ran"], "blocks": 3}])
        run = {**self.run_, "task": "t"}
        with mock.patch.object(h, "load_run", lambda r=None: (self.d, run)), mock.patch.object(h, "read_events", lambda: []), \
                mock.patch.object(h, "run_totals", lambda rid: {"claude_cost_usd_est": 0, "external_cost_usd": 0}), \
                mock.patch.object(h, "write_pr_descriptions", lambda *a: []), contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_summary(mock.Mock(run=None), {**self.cfg, "git": {}})
        self.assertIn("Gates NOT enforced", out.getvalue())

    def test_hook_allows_a_review_run(self):
        (self.repo / "a.py").write_text("x = 2\n")
        self.edit_transcript(self.repo / "a.py")
        self.run_["mode"] = "review"
        self.assertEqual(self.run_hook({})[0], 0)

    def test_hook_is_registered_for_engineers_only_and_synchronously(self):
        hooks = json.loads((Path(__file__).parent.parent / "hooks" / "hooks.json").read_text())["hooks"]["SubagentStop"]
        gate = [g for g in hooks if "engineer-gates" in json.dumps(g)]
        self.assertEqual(len(gate), 1)
        self.assertEqual(gate[0]["matcher"], "^harness:engineer$")
        self.assertFalse(gate[0]["hooks"][0].get("async"))

    def test_tamper_scan_in_a_real_repo_with_allow_lists_and_clearing(self):
        (self.repo / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 2\n")
        found = h.tamper_scan(self.cfg, self.run_, self.d)
        self.assertEqual([f["kind"] for f in found], ["altered-assertion"])
        self.assertTrue((self.d / "tamper.json").exists())
        self.cfg["gates"] = {"tamper_allow": ["tests/*.py"]}
        self.assertEqual(h.tamper_scan(self.cfg, self.run_, self.d), [])
        del self.cfg["gates"]
        (self.d / "tasks.json").write_text(json.dumps({"tasks": [{"repo": "app", "tests_may_change": ["tests/test_a.py"]}]}))
        self.assertEqual(h.tamper_scan(self.cfg, self.run_, self.d), [])
        (self.d / "tasks.json").unlink()
        h.write_json(self.d / "tamper-cleared.json", {found[0]["id"]: {"by": "user", "reason": "intended"}})
        self.assertEqual(h.tamper_scan(self.cfg, self.run_, self.d), [])

    def test_scope_filter_does_not_shrink_tamper_json_and_duplicates_share_an_id(self):
        (self.repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
        found = h.tamper_scan(self.cfg, self.run_, self.d, only=["nope"])
        self.assertEqual(found, [])
        listed = json.loads((self.d / "tamper.json").read_text())["findings"]
        self.assertEqual(len(listed), len({f["id"] for f in listed}))
        self.assertTrue(listed)

    def test_binary_changes_move_the_fingerprint(self):
        blob = self.repo / "fx.bin"
        blob.write_bytes(b"\x00\x01")
        self.git("add", "fx.bin")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "bin")
        self.run_["repos"]["app"]["base_commit"] = self.git("rev-parse", "HEAD")
        blob.write_bytes(b"\x00\x02")
        before = h.tree_fingerprint("app", h.repos(self.cfg)["app"], self.run_)
        blob.write_bytes(b"\x00\x03")
        self.assertNotEqual(before, h.tree_fingerprint("app", h.repos(self.cfg)["app"], self.run_))

    def test_a_failing_scan_does_not_crash_verdict(self):
        with mock.patch.object(h, "tamper_scan", mock.Mock(side_effect=RuntimeError("bad base"))):
            code, out = self.verdict({})
        self.assertIn("WARNING: test-tampering scan failed", out)
        self.assertIn("DECISION: DONE", out)

    def test_subagents_cannot_clear_flags_or_edit_enforcement_state(self):
        def denied(tool_input, agent="harness:engineer", aid="a1"):
            payload = {"tool_input": tool_input, "agent_type": agent}
            if aid:
                payload["agent_id"] = aid
            return h.enforcement_state_denial(payload)
        self.assertTrue(denied({"command": "python3 /x/harness.py tamper clear T-abc123 --reason ok"}))
        self.assertTrue(denied({"command": "echo '{}' > .harness/runs/r/tamper-cleared.json"}))
        self.assertTrue(denied({"file_path": "/p/.harness/runs/r/gate-state.json"}))
        self.assertTrue(denied({"file_path": "/p/.harness/runs/r/tasks.json"}))
        self.assertTrue(denied({"file_path": "/p/.claude/harness.toml"}))
        self.assertFalse(denied({"file_path": "/p/.harness/runs/r/tasks.json"}, agent="harness:planner"))
        self.assertFalse(denied({"file_path": "/p/.harness/runs/r/notes-T1.md"}))
        self.assertFalse(denied({"command": "python3 /x/harness.py tamper clear T-abc123 --reason ok"}, aid=None))  # main session
        self.assertFalse(denied({"command": "python3 /x/harness.py tamper scan"}))

    def verdict(self, verified):
        h.write_json(self.d / "review-1.json", {"round": 1, "findings": []})
        h.write_json(self.d / "verified-1.json", {"round": 1, "findings": [], **verified})
        out = io.StringIO()
        with mock.patch.object(h, "load_run", lambda: (self.d, self.run_)), mock.patch.object(h, "log_event", lambda e: None), \
                contextlib.redirect_stdout(out):
            try:
                h.cmd_verdict(mock.Mock(round=1), self.cfg)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_verdict_needs_the_verifier_to_judge_tampering(self):
        (self.repo / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 2\n")
        tid = h.tamper_scan(self.cfg, self.run_, self.d)[0]["id"]
        code, out = self.verdict({})
        self.assertEqual(code, 2)
        self.assertIn(tid, out)
        code, out = self.verdict({"tamper": [{"id": tid, "verdict": "justified", "reason": "plan T1 changes the total"}]})
        self.assertIn("DECISION: DONE", out)
        code, out = self.verdict({"tamper": [{"id": tid, "verdict": "tampered", "reason": "weakened"}]})
        self.assertIn("DECISION: FIX", out)
        code, out = self.verdict({"tamper": [{"id": tid, "verdict": "justified"}]})  # no reason: not accepted
        self.assertNotIn("DECISION: DONE", out)

    def test_verdict_unchanged_without_tampering(self):
        code, out = self.verdict({})
        self.assertIn("DECISION: DONE", out)


if __name__ == "__main__":
    unittest.main()


class TierTests(unittest.TestCase):
    """Size tiers (#57): run start, run tier, reviewer model choice, and the per-tier report."""

    def test_quick_is_an_alias_for_tiny(self):
        self.assertEqual(h.resolve_tier(None, "quick"), ("tiny", "quick"))
        self.assertEqual(h.resolve_tier(None, "full"), ("standard", "full"))
        self.assertEqual(h.resolve_tier("large", "full"), ("large", "full"))
        self.assertEqual(h.resolve_tier("tiny", "full"), ("tiny", "quick"))

    def start(self, cfg, **kw):
        args = mock.Mock(task="fix typo", issue=None, repo=None, mode="full", tier=None, force=False)
        for k, v in kw.items():
            setattr(args, k, v)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(h, "new_run", return_value=(Path(tmp), {"id": "r1"})) as nr, \
                contextlib.redirect_stdout(out):
            h.cmd_run_start(args, cfg)
        return json.loads(out.getvalue()), nr

    def test_run_start_records_the_tier_and_sets_the_tiny_planner_model(self):
        out, nr = self.start({}, tier="tiny")
        self.assertEqual(out["tier"], "tiny")
        self.assertEqual(out["model_overrides"], {"planner": "sonnet"})
        self.assertEqual(nr.call_args.args[2], "quick")
        self.assertEqual(nr.call_args.args[4]["tier"], "tiny")

    def test_project_planner_override_wins_and_standard_has_none(self):
        out, _ = self.start({"models": {"planner": "opus"}}, mode="quick")
        self.assertEqual(out["model_overrides"], {"planner": "opus"})
        out, nr = self.start({})
        self.assertEqual((out["tier"], "model_overrides" in out), ("standard", False))
        self.assertEqual(nr.call_args.args[2], "full")

    def test_run_tier_back_to_standard_restores_the_review_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            h.write_json(d / "run.json", {"id": "r1", "mode": "quick", "tier": "tiny"})
            with mock.patch.object(h, "load_run", side_effect=lambda r=None: (d, h.read_json(d / "run.json"))), \
                    mock.patch.object(h, "log_event"), contextlib.redirect_stdout(io.StringIO()):
                h.cmd_run_tier(mock.Mock(tier="standard", reason="two repos", run=None), {})
            run = h.read_json(d / "run.json")
        self.assertEqual((run["tier"], run["mode"]), ("standard", "full"))
        self.assertEqual(run["tier_changes"][0]["reason"], "two repos")

    def test_reviewer_model_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            tiny, std = {"tier": "tiny", "id": "r1"}, {"tier": "standard", "id": "r1"}
            self.assertEqual(h.choose_reviewer_model(d, std, {}, 2)[0], "sonnet")
            self.assertEqual(h.choose_reviewer_model(d, std, {}, 1)[0], "opus")
            self.assertEqual(h.choose_reviewer_model(d, tiny, {}, 2)[0], "sonnet")
            # tiny with no repos configured: nothing risky in the diff
            self.assertEqual(h.choose_reviewer_model(d, tiny, {}, 1)[0], "sonnet")
            h.write_json(d / "tasks.json", {"tasks": [{"id": "T1", "model": "opus"}]})
            self.assertEqual(h.choose_reviewer_model(d, tiny, {}, 1)[0], "opus")

    def test_risk_marker_in_the_diff_keeps_opus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-q", "-b", "main")
            (root / "a.py").write_text("x = 1\n")
            git("add", ".")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
            base = git("rev-parse", "HEAD")
            cfg = {"repos": {"app": {"path": "."}}}
            run = {"tier": "tiny", "id": "r1", "repos": {"app": {"base_commit": base}}}
            with mock.patch.object(h, "ROOT", root), mock.patch.object(h, "run_untracked", return_value=[]):
                (root / "a.py").write_text("x = 2\n")
                self.assertEqual(h.choose_reviewer_model(root / "run", run, cfg, 1)[0], "sonnet")
                (root / "a.py").write_text("x = 2\ndef check_password(p): pass\n")
                model, why = h.choose_reviewer_model(root / "run", run, cfg, 1)
            self.assertEqual(model, "opus")
            self.assertIn("password", why)

    def test_fallback_records_the_reviewer_model_and_verdict_labels_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            run = {"id": "r1", "tier": "standard"}
            line = h.fallback_model_line(d, run, {}, 1)
            self.assertIn("model: opus", line)
            self.assertEqual(h.read_json(d / "run.json")["reviewer_models"], {"1": "opus"})
        self.assertEqual(h.reviewer_label({"provider": "claude", "model": "claude-reviewer"}, run, 1), "claude-reviewer:opus")
        self.assertEqual(h.reviewer_label({"provider": "deepseek", "model": "m"}, run, 1), "m")

    def test_tiny_run_is_capped_at_one_review_round(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        h.write_json(d / "review-1.json", FallbackReviewerTests.FALLBACK_REVIEW)
        h.write_json(d / "verified-1.json", {"findings": [{"id": "R1-1", "verdict": "confirmed"}, {"id": "R1-2", "verdict": "rejected"}]})
        out = io.StringIO()
        with mock.patch.object(h, "load_run", return_value=(d, {"id": "r1", "mode": "full", "tier": "tiny"})), \
                mock.patch.object(h, "tamper_scan", return_value=[]), mock.patch.object(h, "log_event"), \
                contextlib.redirect_stdout(out):
            h.cmd_verdict(mock.Mock(round=1), {})
        self.assertIn("DECISION: ESCALATE", out.getvalue())

    def test_report_groups_runs_by_tier(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            h.print_tier_rollup([{"tier": "tiny", "status": "done", "review_rounds": 1, "claude_cost_usd_est": 0.2, "duration_s": 600},
                                 {"mode": "quick", "status": "done", "review_rounds": 1, "claude_cost_usd_est": 0.4, "duration_s": 600},
                                 {"status": "escalated", "review_rounds": 3, "claude_cost_usd_est": 3.0, "duration_s": 1800}])
        text = out.getvalue()
        self.assertRegex(text, r"tiny\s+2\s+100%\s+1\.0\s+\$0\.30")
        self.assertRegex(text, r"standard\s+1\s+0%\s+3\.0\s+\$3\.00")


class ContractTests(unittest.TestCase):
    """[repos.<name>.contracts]: the contract map, the per-run snapshot gate, and the plan, review and verdict checks."""

    PLAN_NONE = "# Plan: x\n## Contract changes\nNone\n## Tasks\n### T1: x\n"
    PLAN_DECLARED = ("# Plan: x\n## Contract changes\nGET /api/x drops `name`; consumer web/x.js updated by T2\n"
                     "## Tasks\n### T1: x\n")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.gits = {}
        for name in ("api", "web"):
            repo = self.root / name
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "views.py").write_text("x = 1\n")
            (repo / "README.md").write_text("readme\n")
            g = lambda *a, repo=repo: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()
            g("init", "-q", "-b", "main")
            g("add", ".")
            g("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
            self.gits[name] = g
        # The "contract" is whatever src/contract.json says; a real snapshot_cmd introspects the API.
        (self.root / "api" / "contract.json").write_text('{"GET /api/x": ["id", "name"]}')
        self.contracts = {"map_cmd": "echo '# map' > {out}", "covers": ["src", "web:src"], "snapshot_cmd": "cp contract.json {out}"}
        self.cfg = {"repos": {"api": {"path": "api", "gates": [{"name": "tests", "cmd": "true"}],
                                      "contracts": self.contracts},
                              "web": {"path": "web", "gates": [{"name": "tests", "cmd": "true"}]}}}
        self.d = self.root / "run"
        self.d.mkdir()
        self.run_ = {"id": "r1", "mode": "full", "task": "t",
                     "repos": {n: {"base_commit": g("rev-parse", "HEAD")} for n, g in self.gits.items()}}
        for p in (mock.patch.object(h, "ROOT", self.root), mock.patch.object(h, "CONTRACTS_DIR", self.root / ".harness" / "contracts")):
            p.start()
            self.addCleanup(p.stop)

    def build(self, force=False):
        return h.build_contract_map("api", h.repos(self.cfg), self.cfg, force)[0]

    def baseline(self):
        with mock.patch.object(h, "load_run", return_value=(self.d, self.run_)), mock.patch.object(h, "log_event"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_baseline(mock.Mock(repos="api"), self.cfg)
        return out.getvalue()

    def gate(self):
        return h.contract_gate(self.d, "api", h.repos(self.cfg)["api"], self.cfg)

    def change_contract(self):
        (self.root / "api" / "contract.json").write_text('{"GET /api/x": ["id"]}')

    def test_projects_without_contracts_are_unchanged(self):
        cfg = {"repos": {"web": self.cfg["repos"]["web"]}}
        self.assertEqual(h.gates_for("web", h.repos(cfg)["web"], cfg), ([{"name": "tests", "cmd": "true"}], False))
        self.assertEqual(h.contract_snapshot_diffs(self.d), {})
        self.assertEqual(h.validate_contract_plan(None, {"repos": ["web"], "tasks": []}, cfg), [])
        self.assertEqual(h.undeclared_contract_findings(self.d), [])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_contracts(mock.Mock(action="map", repo=None, force=False), cfg)
        self.assertIn("No repo has [contracts]", out.getvalue())
        with mock.patch.object(h, "load_run", return_value=(self.d, self.run_)), contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_baseline(mock.Mock(repos="web"), cfg)
        self.assertNotIn("contracts", out.getvalue())

    def test_snapshot_cmd_runs_as_the_contracts_gate(self):
        gates, _ = h.gates_for("api", h.repos(self.cfg)["api"], self.cfg)
        self.assertEqual([g["name"] for g in gates], ["tests", "contracts"])
        self.assertTrue(gates[-1]["contracts"])

    def test_map_is_rebuilt_only_when_a_covered_file_changes(self):
        self.assertEqual(self.build(), "rebuilt")
        self.assertTrue(h.contract_map_path("api").exists())
        self.assertEqual(self.build(), "fresh")
        (self.root / "api" / "README.md").write_text("changed\n")  # not covered
        self.assertEqual(self.build(), "fresh")
        (self.root / "web" / "src" / "views.py").write_text("x = 2\n")  # covered, in the other repo, uncommitted
        self.assertEqual(h.contract_map_stale("api", h.repos(self.cfg)), "covered files in web changed (src/views.py)")
        self.assertEqual(self.build(), "rebuilt")
        self.gits["web"]("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "edit")
        self.assertEqual(self.build(), "fresh")
        self.assertEqual(self.build(force=True), "rebuilt")
        (self.root / "api" / "src" / "serializers.py").write_text("y = 1\n")  # a new, untracked covered file
        self.assertIn("src/serializers.py", h.contract_map_stale("api", h.repos(self.cfg)))
        self.contracts["map_cmd"] = "echo '# map v2' > {out}"
        self.assertEqual(h.contract_map_stale("api", h.repos(self.cfg)), "map_cmd changed")

    def test_a_failing_or_silent_map_cmd_is_reported(self):
        self.contracts["map_cmd"] = "echo boom; exit 3"
        state, detail = h.build_contract_map("api", h.repos(self.cfg), self.cfg)
        self.assertEqual(state, "failed")
        self.assertIn("boom", detail)
        self.contracts["map_cmd"] = "true"
        self.assertEqual(self.build(), "failed")

    def test_gate_needs_a_pre_edit_snapshot(self):
        ok, out = self.gate()
        self.assertFalse(ok)
        self.assertIn("harness baseline --repos api", out)
        self.assertIn("contracts: baseline snapshot recorded", self.baseline())
        self.assertEqual(self.gate(), (True, "API contract unchanged."))
        self.contracts["snapshot_cmd"] = "echo boom; exit 1"
        ok, out = self.gate()
        self.assertFalse(ok)
        self.assertIn("boom", out)

    def test_key_order_is_not_a_contract_change(self):
        self.baseline()
        (self.root / "api" / "contract.json").write_text('{\n  "GET /api/x": ["id", "name"]\n}\n')
        self.assertTrue(self.gate()[0])
        self.assertEqual(h.contract_snapshot_diffs(self.d), {})

    def test_undeclared_change_fails_the_gate_and_the_verdict(self):
        (self.d / "plan.md").write_text(self.PLAN_NONE)
        self.baseline()
        self.change_contract()
        ok, out = self.gate()
        self.assertFalse(ok)
        self.assertIn("declares none", out)
        self.assertIn('-"GET /api/x"[]: "name"', h.contract_snapshot_diffs(self.d)["api"])
        found = h.undeclared_contract_findings(self.d)
        self.assertEqual([(f["id"], f["severity"], f["verdict"]) for f in found], [("C-api", "blocking", "confirmed")])
        h.write_json(self.d / "review-1.json", {"round": 1, "provider": "claude", "findings": []})
        h.write_json(self.d / "verified-1.json", {"findings": []})
        with mock.patch.object(h, "load_run", return_value=(self.d, self.run_)), mock.patch.object(h, "log_event"), \
                mock.patch.object(h, "tamper_scan", return_value=[]), contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_verdict(mock.Mock(round=1), self.cfg)
        self.assertIn("DECISION: FIX", out.getvalue())
        self.assertEqual([f["id"] for f in json.loads((self.d / "fix-1.json").read_text())["findings"]], ["C-api"])
        # reverting the change clears the saved diff on the next gates run
        (self.root / "api" / "contract.json").write_text('{"GET /api/x": ["id", "name"]}')
        self.assertTrue(self.gate()[0])
        self.assertEqual(h.undeclared_contract_findings(self.d), [])

    def test_declared_change_passes_and_reviewers_see_the_diff(self):
        (self.d / "plan.md").write_text(self.PLAN_DECLARED)
        self.baseline()
        self.change_contract()
        ok, out = self.gate()
        self.assertTrue(ok)
        self.assertIn("changed as declared", out)
        self.assertEqual(h.undeclared_contract_findings(self.d), [])
        prompt, _ = h.build_review_prompt(self.run_, self.d, self.cfg, 1)
        self.assertIn("# API contract snapshot changes", prompt)
        self.assertLess(prompt.index("API contract snapshot"), prompt.index("# Changes"))
        with mock.patch.object(h, "load_run", return_value=(self.d, self.run_)), contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_diff(mock.Mock(round=1, stat=False), self.cfg)
        self.assertTrue(out.getvalue().startswith("=== API contract snapshot changes ==="))

    def test_plan_must_declare_contract_changes_with_a_consumer_task(self):
        tasks = {"repos": ["api", "web"], "tasks": [{"id": "T1"}, {"id": "T2"}]}
        self.assertIn("plan.md is missing", h.validate_contract_plan(None, tasks, self.cfg)[0])
        self.assertIn("Contract changes", h.validate_contract_plan("# Plan\n## Tasks\n", tasks, self.cfg)[0])
        self.assertEqual(h.validate_contract_plan(self.PLAN_NONE, tasks, self.cfg), [])
        self.assertEqual(h.validate_contract_plan(self.PLAN_NONE.replace("None", "No contract change."), tasks, self.cfg), [])
        self.assertEqual(h.validate_contract_plan(self.PLAN_DECLARED, tasks, self.cfg), [])
        no_task = self.PLAN_DECLARED.replace("updated by T2", "updated")
        self.assertIn("consumer task", h.validate_contract_plan(no_task, tasks, self.cfg)[0])
        self.assertEqual(h.validate_contract_plan(no_task, {"repos": ["web"], "tasks": []}, self.cfg), [])

    def test_gates_command_runs_the_contracts_gate_in_process(self):
        (self.d / "plan.md").write_text(self.PLAN_NONE)
        self.baseline()
        self.change_contract()
        (self.root / "api" / "src" / "views.py").write_text("x = 2\n")
        with mock.patch.object(h, "load_run", return_value=(self.d, self.run_)), mock.patch.object(h, "log_event"), \
                contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as cm:
            h.cmd_gates(mock.Mock(repo=["api"], task="T1", attempt=1), self.cfg)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[api] contracts: FAIL", out.getvalue())
        self.assertIn("declares none", out.getvalue())
