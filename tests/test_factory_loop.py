import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).parent.parent / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fl = load("factory_loop")
h = load("harness")


def issue(n, labels=("todo",), state="OPEN", body="", title=None):
    return {"number": n, "title": title or f"Issue {n}", "body": body, "state": state,
            "labels": [{"name": x} for x in labels]}


class PoolTests(unittest.TestCase):
    def test_parse_depends(self):
        self.assertEqual(fl.parse_depends("Text\n\nDepends on #7\ndepends on #3\nSee #9 too"), [3, 7])
        self.assertEqual(fl.parse_depends(None), [])

    def test_pool_needs_todo_open_and_closed_dependencies(self):
        issues = [issue(1, state="CLOSED", labels=()), issue(2, body="Depends on #1"), issue(3, body="Depends on #2"),
                  issue(4, labels=()), issue(5, state="CLOSED"), issue(6, body="Depends on #99")]
        self.assertEqual([p["number"] for p in fl.compute_pool(issues)], [2])

    def test_pool_skips_other_states(self):
        for state in ("in-progress", "awaiting-merge", "blocked", "backlog"):
            self.assertEqual(fl.compute_pool([issue(1, labels=("todo", state))]), [], state)
        self.assertEqual(fl.compute_pool([issue(1, labels=("todo", "enhancement"))])[0]["labels"], ["enhancement"])

    def test_dependency_in_awaiting_merge_is_not_met(self):
        issues = [issue(1, labels=("awaiting-merge",)), issue(2, body="Depends on #1")]
        self.assertEqual(fl.compute_pool(issues), [])


class SelectorTests(unittest.TestCase):
    def test_parse_pick(self):
        self.assertEqual(fl.parse_pick("thinking\nPICK: 4 | unblocks two", {3, 4}), (4, "unblocks two"))
        self.assertEqual(fl.parse_pick("PICK: #3", {3}), (3, "no reason given"))
        self.assertIsNone(fl.parse_pick("PICK: 9 | nope", {3, 4}))
        self.assertIsNone(fl.parse_pick("no pick here", {3}))
        self.assertEqual(fl.parse_pick("PICK: 3 | a\nPICK: 4 | b", {3, 4}), (4, "b"))

    def test_choose(self):
        pool = [{"number": 3}, {"number": 8}]
        ask = mock.Mock()
        self.assertEqual(fl.choose(pool[:1], ask), (3, "only candidate"))
        ask.assert_not_called()
        self.assertEqual(fl.choose(pool, lambda p: "PICK: 8 | smaller")[0], 8)
        self.assertEqual(fl.choose(pool, lambda p: "garbage")[0], 3)
        self.assertEqual(fl.choose(pool, lambda p: 1 / 0)[0], 3)
        self.assertEqual(fl.choose(pool, lambda p: "PICK: 99 | x")[0], 3)


class GateTests(unittest.TestCase):
    def gate(self, readings, now=1000.0, **kw):
        it = iter(readings)
        sleeps, events = [], []
        out = fl.usage_gate(lambda: next(it), sleep=sleeps.append, now=lambda: now, log=events.append, **kw)
        return out, sleeps, events

    def test_go(self):
        self.assertEqual(self.gate([{"decision": "go"}])[0], "go")

    def test_pause_sleeps_until_reset_plus_buffer_then_continues(self):
        out, sleeps, events = self.gate([{"decision": "pause", "resume_at": 1600.0, "reasons": ["x"]}, {"decision": "go"}])
        self.assertEqual((out, sleeps), ("go", [660.0]))
        self.assertEqual([e["kind"] for e in events], ["usage_pause", "usage_resume"])

    def test_pause_without_reset_time_retries_later(self):
        self.assertEqual(self.gate([{"decision": "pause"}, {"decision": "go"}])[1], [60])

    def test_unknown_three_times_in_a_row_stops(self):
        out, sleeps, _ = self.gate([{"decision": "unknown"}] * 3)
        self.assertEqual((out, sleeps), ("stop", [60, 60]))

    def test_unknown_counter_resets(self):
        unknown = {"decision": "unknown"}
        out, _, _ = self.gate([unknown, unknown, {"decision": "pause", "resume_at": 1100.0}, unknown, unknown, {"decision": "go"}])
        self.assertEqual(out, "go")


class CiTests(unittest.TestCase):
    def test_ci_state(self):
        run = lambda status, conclusion=None: {"status": status, "conclusion": conclusion}  # noqa: E731
        self.assertEqual(fl.ci_state(None), "none")
        self.assertEqual(fl.ci_state([run("COMPLETED", "SUCCESS"), run("COMPLETED", "SKIPPED")]), "pass")
        self.assertEqual(fl.ci_state([run("COMPLETED", "SUCCESS"), run("IN_PROGRESS")]), "pending")
        self.assertEqual(fl.ci_state([run("COMPLETED", "FAILURE"), run("COMPLETED", "SUCCESS")]), "fail")
        self.assertEqual(fl.ci_state([run("COMPLETED", "FAILURE"), run("QUEUED")]), "pending")  # sees every failure
        self.assertEqual(fl.ci_state([{"state": "SUCCESS"}]), "pass")  # a commit status
        self.assertEqual(fl.ci_state([{"state": "PENDING"}]), "pending")
        self.assertEqual(fl.ci_state([{"state": "FAILURE"}]), "fail")

    def test_pr_state_puts_merged_closed_and_conflict_first(self):
        ok = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.assertEqual(fl.pr_state({"state": "MERGED", "statusCheckRollup": ok}), "merged")
        self.assertEqual(fl.pr_state({"state": "CLOSED"}), "closed")
        self.assertEqual(fl.pr_state({"state": "OPEN", "mergeable": "CONFLICTING", "statusCheckRollup": ok}), "conflict")
        self.assertEqual(fl.pr_state({"state": "OPEN", "mergeable": "UNKNOWN", "statusCheckRollup": ok}), "pass")

    def wait(self, states, **kw):
        it, clock = iter(states), [0.0]
        def sleep(sec): clock[0] += sec  # noqa: E306
        return fl.wait_ci(lambda: next(it), sleep=sleep, now=lambda: clock[0], poll=30, **kw), clock[0]

    def test_wait_polls_until_done(self):
        self.assertEqual(self.wait(["pending", "pending", "pass"]), ("pass", 60))

    def test_no_checks_counts_only_after_the_grace_period(self):
        self.assertEqual(self.wait(["none"] * 10, grace=90), ("none", 90))
        self.assertEqual(self.wait(["none", "pending", "pass"], grace=90)[0], "pass")  # workflows registered late

    def test_wait_times_out(self):
        self.assertEqual(self.wait(["pending"] * 50, timeout=100), ("timeout", 120))

    def land(self, waits, fix=True, merge=True, resolve=None):
        it, calls = iter(waits), {"fix": 0, "merge": 0, "resolve": 0, "blockers": [], "log": []}
        def do_fix(): calls["fix"] += 1; return fix  # noqa: E702, E306
        def do_merge(): calls["merge"] += 1; return merge  # noqa: E702, E306
        def do_resolve(): calls["resolve"] += 1; return resolve  # noqa: E702, E306
        out = fl.land(5, wait=lambda: next(it), fix=do_fix, merge=do_merge,
                      resolve=None if resolve is None else do_resolve,
                      raise_blocker=lambda text, n, need: calls["blockers"].append(text), log=calls["log"].append)
        return out, calls

    def test_a_conflict_is_resolved_once_then_ci_decides(self):
        out, c = self.land(["conflict", "pass"], resolve=True)
        self.assertEqual((out, c["resolve"], c["merge"]), ("merged", 1, 1))
        out, c = self.land(["conflict", "fail", "pass"], resolve=True)  # then CI fails on the merged code: fixer runs
        self.assertEqual((out, c["resolve"], c["fix"], c["merge"]), ("merged", 1, 1, 1))

    def test_a_conflict_that_remains_or_cannot_be_pushed_blocks(self):
        out, c = self.land(["conflict", "conflict"], resolve=True)
        self.assertEqual((out, c["resolve"], c["merge"], len(c["blockers"])), ("blocked", 1, 0, 1))
        out, c = self.land(["conflict"], resolve=False)
        self.assertEqual((out, c["merge"], len(c["blockers"])), ("blocked", 0, 1))

    def test_green_ci_merges(self):
        out, c = self.land(["pass"])
        self.assertEqual((out, c["fix"], c["merge"], c["blockers"]), ("merged", 0, 1, []))

    def test_no_ci_merges(self):
        self.assertEqual(self.land(["none"])[0], "merged")

    def test_one_failure_is_fixed_once_then_merged(self):
        out, c = self.land(["fail", "pass"])
        self.assertEqual((out, c["fix"], c["merge"]), ("merged", 1, 1))
        self.assertIn("ci_fix_pushed", [e["kind"] for e in c["log"]])

    def test_a_second_failure_blocks_without_merging(self):
        out, c = self.land(["fail", "fail"])
        self.assertEqual((out, c["fix"], c["merge"], len(c["blockers"])), ("blocked", 1, 0, 1))

    def test_a_fix_that_cannot_push_blocks(self):
        out, c = self.land(["fail"], fix=False)
        self.assertEqual((out, c["merge"], len(c["blockers"])), ("blocked", 0, 1))

    def test_conflict_closed_timeout_and_refused_merge_block(self):
        for waits, merge in ((["conflict"], True), (["closed"], True), (["timeout"], True), (["pass"], False)):
            out, c = self.land(waits, merge=merge)
            self.assertEqual((out, len(c["blockers"])), ("blocked", 1), waits)

    def test_already_merged_is_not_merged_again(self):
        out, c = self.land(["merged"])
        self.assertEqual((out, c["merge"]), ("merged", 0))


class CleanupTests(unittest.TestCase):
    def test_concerns_are_appended_to_the_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            a = mock.Mock(text="the  retry loop\nnever backs off", file="app/q.py", run=None)
            with mock.patch.object(h, "load_run", lambda r: (d, {"id": "r1"})), contextlib.redirect_stdout(io.StringIO()):
                h.cmd_run_concern(a, {})
                h.cmd_run_concern(mock.Mock(text="second", file=None, run=None), {})
            rows = [json.loads(x) for x in (d / "concerns.jsonl").read_text().splitlines()]
        self.assertEqual([r["text"] for r in rows], ["the retry loop never backs off", "second"])
        self.assertEqual(rows[0]["file"], "app/q.py")

    def test_file_backlog_creates_new_titles_only_with_the_backlog_label(self):
        made = []
        existing = [{"title": "Already filed"}]
        with mock.patch.object(h, "ensure_labels"), mock.patch.object(h, "gh_json", lambda *a: existing), \
                mock.patch.object(h, "gh", lambda *a, **k: made.append(a) or ""):
            n = h.file_backlog("o/r", 17, [{"title": "Add backoff"}, {"title": "already filed", "body": "x"},
                                           {"title": "  "}, "junk", {"title": "Add backoff", "body": "dup"}],
                               "https://x/pull/9")
        self.assertEqual(n, 1)
        args = made[0]
        self.assertEqual(args[:2], ("issue", "create"))
        self.assertIn("backlog", args)
        self.assertIn("Raised while working on #17 (https://x/pull/9)", args[args.index("--body") + 1])

    def test_nothing_is_filed_without_survivors(self):
        with mock.patch.object(h, "gh", side_effect=AssertionError("must not call gh")):
            self.assertEqual(h.file_backlog("o/r", 1, [], None), 0)


class LoopTests(unittest.TestCase):
    def make(self, issues, outcomes=None, gate=lambda: "go", select=None, **kw):
        labels = {i["number"]: {x["name"] for x in i["labels"]} for i in issues}
        calls = {"states": [], "ran": [], "blockers": [], "log": []}
        outcomes = outcomes or {}

        def set_state(n, st):
            calls["states"].append((n, st))
            labels[n] = (labels[n] - set(fl.STATE_LABELS)) | {st}

        def run_issue(n):
            calls["ran"].append(n)
            if n in outcomes:
                labels[n] = (labels[n] - set(fl.STATE_LABELS)) | {outcomes[n]}
            return 0

        def fetch():
            return [{**i, "labels": [{"name": x} for x in labels[i["number"]]],
                     "state": "CLOSED" if "closed" in labels[i["number"]] else i["state"]} for i in issues]

        result = fl.run_loop(gate=gate, fetch_issues=fetch, select=select or (lambda pool: (pool[0]["number"], "first")),
                             set_state=set_state, run_issue=run_issue, fetch_labels=lambda n: labels[n],
                             raise_blocker=lambda text, n, need: calls["blockers"].append((n, text)),
                             log=calls["log"].append, **kw)
        return result, calls, labels

    def test_runs_the_pool_in_selected_order_until_empty(self):
        issues = [issue(1), issue(2), issue(3, labels=())]
        result, calls, _ = self.make(issues, {1: "awaiting-merge", 2: "blocked"}, select=lambda pool: (pool[-1]["number"], "x"))
        self.assertEqual(calls["ran"], [2, 1])
        self.assertEqual(result["results"], [{"issue": 2, "outcome": "blocked"}, {"issue": 1, "outcome": "awaiting-merge"}])
        self.assertEqual(calls["blockers"], [])
        self.assertIn(("harness_started"), [e["kind"] for e in calls["log"]])

    def test_auto_merge_lands_awaiting_prs_and_blocks_the_ones_that_fail(self):
        landed = []
        result, calls, labels = self.make([issue(1), issue(2)], {1: "awaiting-merge", 2: "awaiting-merge"},
                                          land_pr=lambda n: landed.append(n) or ("merged" if n == 1 else "blocked"))
        self.assertEqual(landed, [1, 2])
        self.assertEqual(result["results"], [{"issue": 1, "outcome": "merged"}, {"issue": 2, "outcome": "blocked"}])
        self.assertIn((2, "blocked"), calls["states"])

    def test_cleanup_runs_after_a_pr_exists_and_never_stops_the_loop(self):
        seen = []
        def after(n):
            seen.append(n)
            raise RuntimeError("boom")
        result, calls, _ = self.make([issue(1), issue(2), issue(3)], {1: "awaiting-merge", 2: "blocked", 3: "awaiting-merge"},
                                     after_issue=after)
        self.assertEqual(seen, [1, 3])  # not for the blocked issue
        self.assertEqual(len(result["results"]), 3)
        self.assertEqual([e["kind"] for e in calls["log"]].count("after_issue_failed"), 2)

    def test_manual_policy_never_lands(self):
        result, _, _ = self.make([issue(1)], {1: "awaiting-merge"})
        self.assertEqual(result["results"], [{"issue": 1, "outcome": "awaiting-merge"}])

    def test_session_that_records_nothing_becomes_blocked(self):
        result, calls, labels = self.make([issue(1)])
        self.assertEqual(result["results"], [{"issue": 1, "outcome": "blocked"}])
        self.assertIn("blocked", labels[1])
        self.assertEqual(len(calls["blockers"]), 1)

    def test_an_issue_is_not_retried_in_the_same_run(self):
        result, calls, labels = self.make([issue(1)], {1: "todo"})
        self.assertEqual(calls["ran"], [1])

    def test_dependencies_unlock_during_the_run(self):
        issues = [issue(1), issue(2, body="Depends on #1")]
        result, calls, _ = self.make(issues, {1: "closed"})
        self.assertEqual(calls["ran"], [1, 2])

    def test_orphaned_in_progress_issues_are_handed_back(self):
        result, calls, labels = self.make([issue(1, labels=("in-progress",)), issue(2)], {2: "awaiting-merge"})
        self.assertEqual(calls["states"][0], (1, "blocked"))
        self.assertEqual(calls["blockers"][0][0], 1)
        self.assertEqual(calls["ran"], [2])

    def test_usage_stop_halts_and_raises_a_blocker(self):
        result, calls, _ = self.make([issue(1)], gate=lambda: "stop")
        self.assertEqual((calls["ran"], result["stopped"]), ([], "usage unknown"))
        self.assertEqual(calls["blockers"][0][0], None)

    def test_max_issues(self):
        issues = [issue(n) for n in (1, 2, 3)]
        result, calls, _ = self.make(issues, {n: "awaiting-merge" for n in (1, 2, 3)}, max_issues=2)
        self.assertEqual(calls["ran"], [1, 2])


class AutoQueueTests(unittest.TestCase):
    def test_planned_issues_come_first_and_only_the_factorys_own(self):
        issues = [issue(1, labels=()), issue(2, labels=()), issue(3, labels=("backlog",)),
                  issue(4, labels=("blocked",)), issue(5, labels=(), body="Depends on #4")]
        src, found = fl.auto_queue_candidates(issues, {2, 4, 5})
        self.assertEqual((src, [p["number"] for p in found]), ("planned", [2]))  # #1 was opened by a person

    def test_backlog_when_nothing_is_planned(self):
        issues = [issue(1, labels=("backlog",)), issue(2, labels=("backlog", "enhancement")),
                  issue(3, labels=("backlog",), body="Depends on #9"), issue(4, labels=("backlog", "blocked"))]
        src, found = fl.auto_queue_candidates(issues, set())
        self.assertEqual((src, [p["number"] for p in found]), ("backlog", [1, 2]))

    def refill(self, issues, discover, rounds=3):
        promoted, log = [], []
        r = fl.make_refill(lambda: issues, lambda: {i["number"] for i in issues}, promoted.append, discover, log.append, rounds)
        return r, promoted, log

    def test_refill_promotes_before_discovering(self):
        discover = mock.Mock(return_value=[9])
        r, promoted, log = self.refill([issue(1, labels=("backlog",))], discover)
        self.assertTrue(r())
        self.assertEqual(promoted, [1])
        discover.assert_not_called()
        self.assertEqual(log[0]["source"], "backlog")

    def test_discovery_runs_a_limited_number_of_rounds(self):
        discover = mock.Mock(return_value=[9])
        r, promoted, log = self.refill([], discover, rounds=2)
        self.assertEqual([r(), r(), r(), r()], [True, True, False, False])
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(log[-1]["kind"], "auto_queue_exhausted")

    def test_an_empty_discovery_round_ends_auto_queue(self):
        discover = mock.Mock(return_value=[])
        r, _, log = self.refill([], discover)
        self.assertFalse(r())
        self.assertFalse(r())
        self.assertEqual(discover.call_count, 1)
        self.assertIn("nothing left", log[-1]["why"])

    def test_loop_keeps_working_as_the_queue_refills_and_never_without_refill(self):
        labels = {1: {"todo"}}
        issues = [{"number": n, "title": f"I{n}", "body": "", "state": "OPEN"} for n in (1, 2)]
        order, made = [], []

        def fetch():
            return [{**i, "labels": [{"name": x} for x in labels.get(i["number"], ())]} for i in issues if i["number"] in labels]

        def run_issue(n):
            order.append(n)
            labels[n] = {"closed"}
            issues[n - 1]["state"] = "CLOSED"
            return 0

        def discover():
            if made:
                return []
            made.append(2)
            labels[2] = {"todo"}
            return [2]

        refill = fl.make_refill(fetch, set, lambda n: None, discover, lambda e: None)
        base = dict(gate=lambda: "go", fetch_issues=fetch, select=lambda pool: (pool[0]["number"], "x"),
                    set_state=lambda n, st: None, run_issue=run_issue, fetch_labels=lambda n: labels[n],
                    raise_blocker=lambda *a: None, log=lambda e: None)
        result = fl.run_loop(refill=refill, **base)
        self.assertEqual(order, [1, 2])
        self.assertEqual(len(result["results"]), 2)

    def test_a_failing_refill_ends_the_run_cleanly(self):
        log = []
        result = fl.run_loop(gate=lambda: "go", fetch_issues=lambda: [], select=None, set_state=None, run_issue=None,
                             fetch_labels=None, raise_blocker=None, log=log.append, refill=mock.Mock(side_effect=OSError("boom")))
        self.assertEqual(result, {"results": [], "stopped": None})
        self.assertEqual(log[-1]["kind"], "auto_queue_failed")


class AutoQueueWiringTests(unittest.TestCase):
    def test_trim_plan_keeps_build_order_and_drops_orphans(self):
        plan = {"issues": [{"key": "c", "depends_on": ["a"]}, {"key": "a"}, {"key": "b", "depends_on": ["c"]}, {"key": "d"}]}
        self.assertEqual([i["key"] for i in h.trim_plan(plan, 2)["issues"]], ["a", "d"])
        self.assertEqual([i["key"] for i in h.trim_plan(plan, 3)["issues"]], ["a", "d", "c"])

    def test_planned_numbers_come_from_the_log(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".factory").mkdir()
            (root / ".factory" / "log.jsonl").write_text(
                '{"kind": "issue_created", "number": 4}\nnot json\n{"kind": "queued", "issue": 5}\n{"kind": "issue_created", "number": 7}\n')
            with mock.patch.object(h, "ROOT", root):
                self.assertEqual(h.planned_issue_numbers(), {4, 7})

    def test_auto_queue_refuses_manual_merging(self):
        a = mock.Mock(merge="manual", issues=None, dry_run=False, max_issues=5, repo=None, auto_queue=True)
        with mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), mock.patch.object(h, "factory_modules", lambda: (fl, None)), \
                self.assertRaises(SystemExit) as cm:
            h.cmd_factory_run(a, {})
        self.assertIn("--merge auto", str(cm.exception))

    def test_empty_queue_runs_discovery_files_todo_issues_and_runs_them(self):
        import os
        import tempfile
        import types
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / ".factory").mkdir(parents=True)
            (root / ".factory" / "docs.json").write_text('{"mvp": "mvp.md"}')
            fake = Path(tmp) / "fake-claude.sh"
            plan = '{"notes": ["n1"], "issues": [{"key": "a", "title": "Add the court", "body": "From the MVP.", "acceptance": ["It renders"]}]}'
            fake.write_text(f"""#!/bin/sh
case "$*" in
  *factory-discoverer*)
    if [ -f "$SOFTWARE_FACTORY_HOME/round1" ]; then echo '{{"issues": []}}' > .factory/discovered.json
    else touch "$SOFTWARE_FACTORY_HOME/round1"; echo '{plan}' > .factory/discovered.json; fi;;
esac
echo "args: $@"
""")
            fake.chmod(0o755)
            issues, created = {}, []

            def fake_json(*args):
                if args[:2] == ("issue", "list"):
                    return [issue(n, labels=tuple(v)) for n, v in issues.items()]
                if args[:2] == ("issue", "view"):
                    return {"labels": [{"name": x} for x in issues[int(args[2])]]}
                return [{"name": x} for x in (*fl.STATE_LABELS, "enhancement")]

            def fake_gh(*args, **kw):
                if args[:2] == ("issue", "create"):
                    labels = {args[i + 1] for i, x in enumerate(args) if x == "--label"}
                    issues[7] = labels
                    created.append(labels)
                    return "https://github.com/o/r/issues/7\n"
                return ""

            def set_state(slug, n, st):
                issues[n] = (issues[n] - set(fl.STATE_LABELS)) | {st}

            usage = types.SimpleNamespace(check=lambda limits: {"decision": "go"})
            a = mock.Mock(merge="auto", issues=None, dry_run=False, max_issues=5, repo=None, auto_queue=True, until_limit=False)
            env = {"HARNESS_CLAUDE": str(fake), "SOFTWARE_FACTORY_HOME": tmp}
            with mock.patch.dict("os.environ", env), mock.patch.object(h, "ROOT", root), \
                    mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), \
                    mock.patch.object(h, "factory_modules", lambda: (fl, usage)), \
                    mock.patch.object(h, "gh_json", fake_json), mock.patch.object(h, "gh", fake_gh), \
                    mock.patch.object(h, "set_issue_state", set_state), mock.patch.object(h, "ensure_labels"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                h.cmd_factory_run(a, {"factory": {"claude_args": []}})
            events = [json.loads(x) for x in (root / ".factory" / "log.jsonl").read_text().splitlines()]
            discovers = [f.read_text() for f in (root / ".factory" / "sessions").glob("discover-*.log")]
            issue_runs = list((root / ".factory" / "sessions").glob("issue-7-*.log"))
        kinds = [e["kind"] for e in events]
        self.assertEqual(created, [{"enhancement", "todo"}])  # filed ready to run
        self.assertEqual(len(discovers), 2)  # one round that found an issue, one that found nothing
        self.assertTrue(all("--agent harness:factory-discoverer" in d for d in discovers))
        self.assertEqual(len(issue_runs), 1)  # the discovered issue was worked
        self.assertIn({"kind": "auto_queue_on"}, [{"kind": e["kind"]} for e in events])
        self.assertEqual([e for e in events if e["kind"] == "discovered"][0]["notes"], ["n1"])
        self.assertEqual([e["source"] for e in events if e["kind"] == "auto_queued"], ["discovered"])
        self.assertIn("auto_queue_exhausted", kinds)
        self.assertEqual(kinds.count("harness_started"), 1)

    def test_docs_are_recorded_for_discovery(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mvp.md").write_text("x")
            with mock.patch.object(h, "ROOT", root), contextlib.redirect_stdout(io.StringIO()):
                h.cmd_factory_docs(mock.Mock(mvp="mvp.md", vision=None), {})
                self.assertEqual(json.loads((root / ".factory" / "docs.json").read_text()), {"mvp": "mvp.md"})
                with self.assertRaises(SystemExit):
                    h.cmd_factory_docs(mock.Mock(mvp="missing.md", vision=None), {})


class HarnessWiringTests(unittest.TestCase):
    def test_set_issue_state_swaps_the_state_label(self):
        calls = []
        with mock.patch.object(h, "ensure_labels"), \
                mock.patch.object(h, "issue_labels", lambda slug, n: {"todo", "enhancement"}), \
                mock.patch.object(h, "gh", lambda *a, **k: calls.append(a)):
            h.set_issue_state("o/r", 5, "in-progress")
            h.set_issue_state("o/r", 5, "todo")  # already there: nothing to do
        self.assertEqual(calls, [("issue", "edit", "5", "--repo", "o/r", "--add-label", "in-progress", "--remove-label", "todo")])

    def test_block_requires_a_valid_message_and_labels_the_issue(self):
        a = mock.Mock(issue=7, kind="BLOCKER", situation="Gates keep failing.", need="Decide whether to skip.",
                      option=None, recommend=None, detail="/x/log", repo=None)
        states, comments = [], []
        with mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), \
                mock.patch.object(h, "set_issue_state", lambda s, n, st: states.append((n, st))), \
                mock.patch.object(h, "factory_comment", lambda s, n, t: comments.append(n)), \
                mock.patch.object(h, "factory_log"), contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_factory_block(a, {"project": {"name": "App"}})
        self.assertEqual((states, comments), ([(7, "blocked")], [7]))
        self.assertIn("[BLOCKER]  App #7", out.getvalue())
        a.need = ""
        with mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), self.assertRaises(SystemExit):
            h.cmd_factory_block(a, {})

    def test_done_labels_awaiting_merge(self):
        import tempfile
        a = mock.Mock(issue=7, pr="https://x/pull/9", note=None, repo=None)
        states = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(h, "ROOT", Path(tmp.name)), mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), \
                mock.patch.object(h, "set_issue_state", lambda s, n, st: states.append((n, st))), \
                mock.patch.object(h, "pipeline_gaps", lambda n: []), \
                mock.patch.object(h, "factory_comment"), mock.patch.object(h, "factory_log"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_factory_done(a, {})
        self.assertEqual(states, [(7, "awaiting-merge")])
        self.assertIn("[DONE]", out.getvalue())
        self.assertEqual(json.loads((Path(tmp.name) / ".factory" / "prs.json").read_text()), {"7": "https://x/pull/9"})

    def test_done_is_refused_when_the_run_skipped_plan_and_review(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            for name, issue, extra in (("20260101-a", 7, {}), ("20260102-b", 8, {})):
                d = runs / name
                d.mkdir()
                (d / "run.json").write_text(json.dumps({"issue": {"number": issue}, **extra}))
            with mock.patch.object(h, "RUNS_DIR", runs):
                self.assertEqual(len(h.pipeline_gaps(7)), 2)  # no tasks.json, no review round
                self.assertIn("no harness run", h.pipeline_gaps(9)[0])
                (runs / "20260101-a" / "tasks.json").write_text("{}")
                self.assertEqual(h.pipeline_gaps(7), ["no review round"])
                (runs / "20260101-a" / "review-1.raw.txt").write_text("x")  # not a review result
                self.assertEqual(h.pipeline_gaps(7), ["no review round"])
                (runs / "20260101-a" / "review-1.json").write_text("{}")  # the fallback reviewer's output
                self.assertEqual(h.pipeline_gaps(7), [])
                (runs / "20260101-a" / "review-1.json").unlink()
                (runs / "20260101-a" / "run.json").write_text(json.dumps({"issue": {"number": 7}, "review_rounds": 1}))
                self.assertEqual(h.pipeline_gaps(7), [])
                a = mock.Mock(issue=8, pr="x", note=None, repo=None)
                with self.assertRaises(SystemExit) as cm:
                    h.cmd_factory_done(a, {})
                self.assertIn("Refusing", str(cm.exception))

    def test_run_starts_a_fresh_marked_session_per_issue(self):
        import json
        import os
        import tempfile
        import types
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            fake = Path(tmp) / "fake-claude.sh"
            fake.write_text('#!/bin/sh\necho "args: $@"\necho "marker set: ${SOFTWARE_FACTORY_MARKER:+yes}"\n'
                            'ls "$SOFTWARE_FACTORY_HOME/.software-factory/markers" | wc -l\n')
            fake.chmod(0o755)
            labels = {4: {"todo"}}
            gh_calls = []

            def fake_json(*args):
                if args[:2] == ("issue", "list"):
                    return [issue(4, labels=tuple(labels[4]))]
                if args[:2] == ("issue", "view"):
                    return {"labels": [{"name": x} for x in labels[4]]}
                return [{"name": x} for x in fl.STATE_LABELS]

            def fake_gh(*args, **kw):
                gh_calls.append(args)
                return ""

            def set_state(slug, n, st):
                labels[n] = (labels[n] - set(fl.STATE_LABELS)) | {st}

            usage = types.SimpleNamespace(check=lambda limits: {"decision": "go"})
            a = mock.Mock(merge="manual", issues=None, dry_run=False, max_issues=5, repo=None, auto_queue=False, until_limit=False)
            env = {"HARNESS_CLAUDE": str(fake), "SOFTWARE_FACTORY_HOME": tmp}
            with mock.patch.dict("os.environ", env), mock.patch.object(h, "ROOT", root), \
                    mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), \
                    mock.patch.object(h, "factory_modules", lambda: (fl, usage)), \
                    mock.patch.object(h, "gh_json", fake_json), mock.patch.object(h, "gh", fake_gh), \
                    mock.patch.object(h, "set_issue_state", set_state), mock.patch.object(h, "ensure_labels"), \
                    contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                h.cmd_factory_run(a, {"factory": {"claude_args": ["--permission-mode", "acceptEdits"]}})
            logs = list((root / ".factory" / "sessions").glob("issue-4-*.log"))
            text = logs[0].read_text()
            events = [json.loads(x)["kind"] for x in (root / ".factory" / "log.jsonl").read_text().splitlines()]
            left = list((Path(tmp) / ".software-factory" / "markers").glob("*.json"))
        self.assertIn("args: -p /factory-issue 4 --permission-mode acceptEdits --add-dir", text)
        self.assertIn('--allowedTools Bash(git push origin *) Bash(gh pr create *) Bash(git add *) Bash(git commit -F *) Bash(git status) Bash(git status *) Bash(git diff *) Bash(git log *) Bash(git branch --show-current) Bash(gh issue list *) Bash(gh issue view *) Bash(gh pr checks *) Bash(python3 *harness.py *) Bash(npm run *) Bash(npm test *)', text)  # push is pre-approved for the issue session
        self.assertIn(str(SCRIPTS.parent), text)  # the plugin root is readable by the headless session
        self.assertIn("marker set: yes", text)
        self.assertEqual(text.splitlines()[-1].strip(), "1")  # exactly one marker while the session ran
        self.assertEqual(left, [])
        self.assertEqual(labels[4], {"blocked"})  # the fake session recorded nothing, so the issue is handed back
        self.assertIn("1 issue(s) run", out.getvalue())
        self.assertEqual([e for e in events if e in ("issue_picked", "harness_started", "harness_finished")],
                         ["issue_picked", "harness_started", "harness_finished"])



class FrontDoorTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(h, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_graceful_stop_is_checked_between_issues(self):
        ran = []
        stop = {"now": False}

        def run_issue(n):
            ran.append(n)
            stop["now"] = True
            return 0

        result = fl.run_loop(gate=lambda: "go", fetch_issues=lambda: [issue(1), issue(2)],
                             select=lambda pool: (pool[0]["number"], "x"), set_state=lambda n, s: None,
                             run_issue=run_issue, fetch_labels=lambda n: {"awaiting-merge"},
                             raise_blocker=lambda *a: None, log=lambda e: None, should_stop=lambda: stop["now"])
        self.assertEqual((ran, result["stopped"]), ([1], "stop requested"))

    def test_tree_is_parked_after_every_issue_even_a_blocked_one(self):
        parked = []
        result = fl.run_loop(gate=lambda: "go", fetch_issues=lambda: [issue(1), issue(2)],
                             select=lambda pool: (pool[0]["number"], "x"), set_state=lambda n, s: None,
                             run_issue=lambda n: 0, fetch_labels=lambda n: {"blocked"},
                             raise_blocker=lambda *a: None, log=lambda e: None, park=parked.append)
        self.assertEqual((parked, [r["issue"] for r in result["results"]]), ([1, 2], [1, 2]))

    def test_a_failing_park_does_not_stop_the_loop(self):
        def boom(n):
            raise RuntimeError("nope")
        events = []
        result = fl.run_loop(gate=lambda: "go", fetch_issues=lambda: [issue(1), issue(2)],
                             select=lambda pool: (pool[0]["number"], "x"), set_state=lambda n, s: None,
                             run_issue=lambda n: 0, fetch_labels=lambda n: {"blocked"},
                             raise_blocker=lambda *a: None, log=events.append, park=boom)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual([e["kind"] for e in events].count("park_failed"), 2)

    def test_events_log_gets_short_lines_for_the_events_that_matter(self):
        h.factory_log({"kind": "issue_picked", "issue": 4, "reason": "unblocks 2"})
        h.factory_log({"kind": "message", "message_kind": "BLOCKER", "issue": 4, "text": "Gates keep failing."})
        h.factory_log({"kind": "usage_pause", "resume_at": None})
        h.factory_log({"kind": "something_internal"})
        lines = (self.root / ".factory" / "events.log").read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].endswith("picked #4: unblocks 2"))
        self.assertTrue(lines[1].endswith("BLOCKER #4: Gates keep failing."))
        self.assertEqual(len((self.root / ".factory" / "log.jsonl").read_text().splitlines()), 4)

    def test_blocked_finish_line_is_dropped_when_a_blocker_line_was_already_sent(self):
        h.MESSAGED.clear()
        h.factory_log({"kind": "harness_started", "issue": 5})
        h.factory_log({"kind": "message", "message_kind": "BLOCKER", "issue": 5, "text": "Stuck."})
        h.factory_log({"kind": "harness_finished", "issue": 5, "outcome": "blocked"})
        h.factory_log({"kind": "harness_started", "issue": 6})
        h.factory_log({"kind": "harness_finished", "issue": 6, "outcome": "blocked"})
        lines = (self.root / ".factory" / "events.log").read_text().splitlines()
        self.assertEqual([l.split(" ", 1)[1] for l in lines],
                         ["started #5", "BLOCKER #5: Stuck.", "started #6", "#6 finished: blocked"])

    def test_second_watcher_exits_silently(self):
        import os
        d = self.root / ".factory"
        d.mkdir(exist_ok=True)
        (d / "watch.json").write_text(json.dumps({"pid": os.getpid()}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            h.cmd_factory_watch(None, {})  # would block forever if it did not see the live lock
        self.assertEqual(buf.getvalue(), "")

    def test_supervisor_lock(self):
        import json
        import os
        self.assertIsNone(h.supervisor_info())
        h.acquire_supervisor([3])
        self.assertEqual(h.supervisor_info()["pid"], os.getpid())
        with self.assertRaises(SystemExit):
            h.acquire_supervisor([])
        h.release_supervisor()
        self.assertIsNone(h.supervisor_info())
        (self.root / ".factory" / "supervisor.json").write_text(json.dumps({"pid": 2 ** 22 + 99, "started": 0, "issues": []}))
        self.assertIsNone(h.supervisor_info())  # a dead supervisor's lock is cleaned up
        self.assertFalse((self.root / ".factory" / "supervisor.json").exists())

    def test_stop_is_graceful_by_default_and_signals_with_now(self):
        import os
        with contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_factory_stop(mock.Mock(now=False), {})
            self.assertIn("no supervisor", out.getvalue())
            h.acquire_supervisor([])
            h.cmd_factory_stop(mock.Mock(now=False), {})
            self.assertTrue((self.root / ".factory" / "stop").exists())
            with mock.patch.object(h.os, "kill") as kill:
                h.cmd_factory_stop(mock.Mock(now=True), {})
        kill.assert_any_call(os.getpid(), h.signal.SIGTERM)
        h.release_supervisor()
        self.assertFalse((self.root / ".factory" / "stop").exists())

    def test_run_stage_reads_the_run_directory(self):
        import json
        rd = self.root / "run"
        self.assertEqual(h.run_stage(rd), "starting")
        rd.mkdir()
        self.assertEqual(h.run_stage(rd), "scouting")
        (rd / "context-app.md").write_text("x")
        self.assertEqual(h.run_stage(rd), "scouted, planning")
        (rd / "plan.md").write_text("x")
        (rd / "tasks.json").write_text(json.dumps({"tasks": [{}, {}, {}]}))
        (rd / "notes-T1.md").write_text("x")
        self.assertEqual(h.run_stage(rd), "implementing (1/3 tasks noted)")
        (rd / "review-1.json").write_text("{}")
        self.assertEqual(h.run_stage(rd), "review round 1 (reviewed)")
        (rd / "verified-1.json").write_text("{}")
        (rd / "final-1.json").write_text("{}")
        self.assertEqual(h.run_stage(rd), "review round 1 (decided)")
        (rd / "summary.md").write_text("x")
        self.assertEqual(h.run_stage(rd), "finishing (summary written)")

    def test_status_summarises_supervisor_queue_blocked_and_events(self):
        h.acquire_supervisor([])
        h.factory_log({"kind": "harness_started", "issue": 2})
        h.factory_log({"kind": "issue_picked", "issue": 2, "reason": "only candidate"})
        sessions = self.root / ".factory" / "sessions"
        sessions.mkdir()
        (sessions / "issue-2-1.log").write_text("line one\nline two\n")
        issues = [issue(2, labels=("in-progress",), title="Build it"), issue(3, labels=("blocked",), title="Stuck one"),
                  issue(4, labels=("awaiting-merge",)), issue(5, labels=("todo",)), issue(6, labels=("todo",), body="Depends on #5")]
        usage = types_ns(decide=lambda r, l: {"decision": "go", "reasons": []}, read_snapshot=lambda max_age=0: None)
        with mock.patch.object(h, "factory_modules", lambda: (fl, usage)), \
                mock.patch.object(h, "factory_repo", lambda a, c: "o/r"), mock.patch.object(h, "gh_json", lambda *a: issues), \
                mock.patch.object(h, "current_run_id", lambda: None), contextlib.redirect_stdout(io.StringIO()) as out:
            h.cmd_factory_status(mock.Mock(issue=None, events=5), {})
        text = out.getvalue()
        h.release_supervisor()
        self.assertIn("supervisor: running", text)
        self.assertIn("usage: go", text)
        self.assertIn("working on: #2 Build it - starting", text)
        self.assertIn("| line two", text)
        self.assertIn("blocked", text)
        self.assertIn("#3 Stuck one", text)
        self.assertIn("awaiting merge: #4", text)
        self.assertIn("could run now: #5", text)
        self.assertIn("picked #2: only candidate", text)


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


class ProjectAllowRulesTests(unittest.TestCase):
    def test_accepts_repo_entry_points(self):
        ok, bad = h.project_allow_rules({"allowed_tools": [
            "Bash(scripts/check.sh*)", "Bash(env/bin/python manage.py check_tailoring*)", "Bash(make verify-*)"]})
        self.assertEqual(len(ok), 3)
        self.assertEqual(bad, [])

    def test_rejects_unsafe_rules(self):
        rules = ["Read(*)", "Bash(*)", "Bash(git push *)", "Bash(gh pr merge *)", "Bash(python3 -c *)",
                 "Bash(/usr/bin/python3 x.py)", "Bash(a.sh && b.sh)", "Bash(a.sh | tee x)", "Bash(rm -rf *)"]
        ok, bad = h.project_allow_rules({"allowed_tools": rules})
        self.assertEqual(ok, [])
        self.assertEqual(len(bad), len(rules))

    def test_missing_key(self):
        self.assertEqual(h.project_allow_rules({}), ([], []))


if __name__ == "__main__":
    unittest.main()
