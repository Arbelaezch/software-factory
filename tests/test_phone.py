import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).parent.parent / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ph = load("phone")
OWNER = 42


class FakeTelegram:
    """Records every Bot API call; message ids count up."""

    def __init__(self, topics=True):
        self.calls, self.topics, self.next_id, self.next_thread = [], topics, 100, 7

    def __call__(self, method, http_timeout=30, **p):
        self.calls.append((method, p))
        if method == "createForumTopic":
            if not self.topics:
                raise ph.TelegramError("createForumTopic: the chat is not a forum")
            self.next_thread += 1
            return {"message_thread_id": self.next_thread}
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]


def note_id(tg, sent):
    """The message id FakeTelegram gave a sent message."""
    return 101 + tg.sent().index(sent)


def done(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class PhoneTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(ph, "HOME", self.tmp / "phone")
        patcher.start()
        self.addCleanup(patcher.stop)
        ph._write("config.json", {"token": "t", "chat_id": OWNER, "user_id": OWNER, "topics": True}, private=True)
        self.tg = FakeTelegram()
        self.bot = ph.Bot(ph.load_config(), self.tg)
        self.runs = []
        self.replies = []
        self.bridge = ph.Bridge(self.bot, run=self.fake_run, spawn=lambda fn: fn())

    def fake_run(self, cmd, **kw):
        self.runs.append((cmd, kw))
        return done(self.replies.pop(0) if self.replies else "OPEN\nok")

    def root(self, name):
        d = self.tmp / name
        (d / ".factory").mkdir(parents=True, exist_ok=True)
        return str(d)

    def block(self, name="alpha", issue=12, options=("Retry", "Skip"), config_dir="/acc/personal", **extra):
        root = self.root(name)
        return ph.send_blocker(root, name, issue, "BLOCKER", f"[BLOCKER]  {name} #{issue}\nWhat is going on: CI failed.",
                               "Retry or skip?", list(options), 1 if options else None, config_dir, self.bot, **extra), root

    def log(self, root, kind, issue=12, ts="2999-01-01T00:00:00Z"):
        with open(Path(root) / ".factory" / "log.jsonl", "a") as f:
            f.write(json.dumps({"ts": ts, "kind": kind, "issue": issue}) + "\n")

    def edits(self):
        return [p for m, p in self.tg.calls if m == "editMessageText"]

    def message(self, text, thread=None, reply_to=None, sender=OWNER):
        msg = {"message_id": 900, "from": {"id": sender}, "chat": {"id": OWNER}, "text": text}
        if thread:
            msg.update(message_thread_id=thread, is_topic_message=True)
        if reply_to:
            msg["reply_to_message"] = {"message_id": reply_to}
        self.bridge.handle({"update_id": 1, "message": msg})

    def tap(self, pid, i, sender=OWNER):
        self.bridge.handle({"update_id": 2, "callback_query": {"id": "q", "from": {"id": sender}, "data": f"a:{pid}:{i}"}})

    def details(self, pid):
        self.bridge.handle({"update_id": 3, "callback_query": {"id": "q", "from": {"id": OWNER}, "data": f"d:{pid}"}})


class SendTests(PhoneTestCase):
    def test_blocker_goes_to_the_projects_topic_with_buttons(self):
        pid, root = self.block()
        msg = self.tg.sent()[-1]
        self.assertEqual(msg["message_thread_id"], 8)
        self.assertTrue(msg["text"].startswith("⛔ alpha #12\n\nRetry or skip?\n1. Retry ★\n2. Skip"))
        labels = [row[0]["text"] for row in msg["reply_markup"]["inline_keyboard"]]
        self.assertEqual(labels, ["1. Retry ★", "2. Skip", "Details"])
        self.assertEqual(msg["reply_markup"]["inline_keyboard"][1][0]["callback_data"], f"a:{pid}:2")
        p = ph._read("pending.json", {})[pid]
        self.assertEqual((p["root"], p["issue"], p["state"], p["message_id"]), (root, 12, "open", 101))
        self.assertEqual(ph._read("projects.json", {})[root]["config_dir"], "/acc/personal")

    def test_each_project_gets_one_topic(self):
        self.block("alpha", 1)
        self.block("alpha", 2)
        self.block("beta", 3)
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "createForumTopic"]), 2)
        self.assertEqual([m["message_thread_id"] for m in self.tg.sent()], [8, 8, 9])

    def test_without_topics_it_still_sends_and_stops_trying(self):
        self.tg.topics = False
        self.block("alpha", 1)
        self.block("beta", 2)
        self.assertEqual(len([c for c in self.tg.calls if c[0] == "createForumTopic"]), 1)
        self.assertFalse(ph.load_config()["topics"])
        self.assertTrue(all("message_thread_id" not in m for m in self.tg.sent()))

    def test_long_option_labels_are_cut(self):
        self.block(options=("x" * 100, "Skip"))
        label = self.tg.sent()[-1]["reply_markup"]["inline_keyboard"][0][0]["text"]
        self.assertLessEqual(len(label), ph.LABEL_MAX)
        self.assertTrue(label.endswith("... ★"))

    def test_question_leads_with_the_summary_and_short_labels(self):
        # court-vision #33: the old button was the first 60 characters of an engineer's sentence.
        pid, _ = self.block(issue=33, options=("Accept unit tests asserting a non-color channel per element plus a PR table",
                                               "The planner picks data-driven team channels in teams.ts"),
                            summary="Teams are told apart by colour only. I can prove the fix with tests, and you check the screenshots.",
                            labels=["Tests + I check screenshots", "I pick the visual cue"], category="product",
                            situation="AC3 needs a non-color channel; see DESIGN_DOC.md:297.",
                            title="Tell teams apart without relying on colour")
        msg = self.tg.sent()[-1]
        self.assertEqual(msg["text"].splitlines()[:8], [
            "⛔ alpha #33 · Tell teams apart without relying on colour", "Teams are told apart by colour only. I can prove the fix with tests, and you check the screenshots.",
            "", "Retry or skip?", "1. Tests + I check screenshots ★",
            "   Accept unit tests asserting a non-color channel per element plus a PR table", "2. I pick the visual cue",
            "   The planner picks data-driven team channels in teams.ts"])
        self.assertNotIn("DESIGN_DOC", msg["text"])  # the engineer's account is behind Details
        self.assertEqual([row[0]["text"] for row in msg["reply_markup"]["inline_keyboard"]],
                         ["1. Tests + I check screenshots ★", "2. I pick the visual cue", "Details"])
        self.assertNotIn("disable_notification", msg)

    def test_details_sends_the_full_message_without_a_model(self):
        pid, _ = self.block(summary="Short.", situation="Long.")
        self.details(pid)
        self.assertTrue(self.tg.sent()[-1]["text"].startswith("[BLOCKER]  alpha #12"))
        self.assertEqual(self.tg.sent()[-1]["reply_parameters"]["message_id"], 101)
        self.assertEqual(self.runs, [])
        self.assertEqual(ph._read("pending.json", {})[pid]["state"], "open")

    def test_infrastructure_blocker_is_a_report_not_a_question(self):
        self.block(issue=85, options=(), category="infrastructure", situation="The base branch has uncommitted changes.",
                   title="Shot chart: " + "long " * 20)
        msg = self.tg.sent()[-1]
        head, rest = msg["text"].split("\n", 1)
        self.assertEqual(head, "⛔ Stopped · alpha #85 · Shot chart: long long long long long long long long long...")
        self.assertTrue(rest.startswith("The base branch has uncommitted changes.\n\nNeeds you at the computer: Retry or skip?"))
        self.assertIn("Nothing to decide here", msg["text"])
        self.assertEqual([row[0]["text"] for row in msg["reply_markup"]["inline_keyboard"]], ["Details"])

    def test_overview_counts_what_happened_since_the_user_last_looked(self):
        _, root = self.block(issue=1)
        self.assertNotIn("Also:", self.tg.sent()[-1]["text"])
        for kind, n in (("merged", 4), ("merged", 5), ("auto_accepted", 6), ("queued", 7)):
            self.log(root, kind, n)
        self.log(root, "merged", 2, ts="2000-01-01T00:00:00Z")  # before they last looked
        self.block(issue=2)
        self.assertIn("Also: 1 more waiting on you; 1 decided for you, 2 merged since you last looked.", self.tg.sent()[-1]["text"])
        with mock.patch.object(ph, "iso", return_value="2999-06-01T00:00:00Z"):
            self.message("status", thread=8)  # looking resets the counts
        self.block(issue=3)
        self.assertIn("Also: 2 more waiting on you.", self.tg.sent()[-1]["text"])


class AnswerTests(PhoneTestCase):
    def test_tap_runs_sonnet_in_the_project_with_its_account(self):
        pid, root = self.block()
        self.replies = ["RESOLVED\nRecorded: retry. #12 is queued again."]
        self.tap(pid, 1)
        cmd, kw = self.runs[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertIn("Option 1: Retry", cmd[2])
        self.assertEqual(kw["cwd"], root)
        self.assertEqual(kw["env"]["CLAUDE_CONFIG_DIR"], "/acc/personal")
        self.assertEqual(kw["env"]["FACTORY_REMOTE"], "1")  # a supervisor it starts keeps sending blockers to the phone
        allowed = cmd[cmd.index("--allowedTools") + 1:]
        self.assertIn(f"Bash(python3 {ph.HARNESS} --root {root} factory decide *)", allowed)
        self.assertFalse(any("git" in a for a in allowed))
        self.assertEqual(self.tg.sent()[-1]["text"], "✓ Recorded: retry. #12 is queued again.")
        self.assertEqual(ph._read("pending.json", {})[pid]["state"], "answered")
        edit = self.edits()[-1]  # the message itself now shows how it ended; only Details is left to tap
        self.assertEqual(edit["message_id"], 101)
        self.assertRegex(edit["text"], r"1\. Retry ★\n2\. Skip\n\n✓ Answered \d\d:\d\d: 1\. Retry\nRecorded\. Not queued again\.$")
        self.assertNotIn("Tap an option", edit["text"])
        self.assertEqual(edit["reply_markup"], {"inline_keyboard": [[{"text": "Details", "callback_data": f"d:{pid}"}]]})
        self.tap(pid, 2)
        self.assertEqual(len(self.runs), 1)
        self.assertIn("already settled", self.tg.sent()[-1]["text"])

    def test_answered_message_says_when_the_issue_runs_again(self):
        pid, root = self.block()

        def queues(cmd, **kw):  # the answer session queued the issue
            self.log(root, "queued")
            return done("RESOLVED\nRecorded.")

        self.bridge.run = queues
        with mock.patch.object(ph, "supervisor_running", return_value=True):
            self.message("2, but file an issue for the flaky test and keep the old endpoint until the next release is out", thread=8)
        text = self.edits()[-1]["text"]
        self.assertRegex(text, r"✓ Answered \d\d:\d\d: 2, but file an issue .{50,60}\.\.\.\nQueued again: the factory runs it after the issue it is on\.$")
        pid2, root2 = self.block("beta", 3)
        self.bridge.run = lambda cmd, **kw: (self.log(root2, "queued", 3), done("RESOLVED\nRecorded."))[1]
        self.tap(pid2, 2)
        self.assertTrue(self.edits()[-1]["text"].endswith("Queued again, but the factory is not running: it runs when the factory is started."))

    def test_a_supervisor_the_answer_just_started_counts_as_queued(self):
        # `factory start --issues N` returns once the supervisor is up, before it has logged queueing the issue.
        pid, root = self.block()
        (Path(root) / ".factory" / "supervisor.json").write_text(json.dumps({"pid": os.getpid(), "issues": [12]}))
        self.replies = ["RESOLVED\nRecorded."]
        self.tap(pid, 1)
        self.assertTrue(self.edits()[-1]["text"].endswith("Queued again: the factory runs it after the issue it is on."))

    def test_a_refused_edit_still_clears_the_buttons(self):
        pid, _ = self.block()
        def no_edit(method, http_timeout=30, **p):
            if method == "editMessageText":
                raise ph.TelegramError("editMessageText: message can't be edited")
            return self.tg(method, http_timeout, **p)

        self.bot.call = no_edit
        self.replies = ["RESOLVED\nRecorded."]
        self.tap(pid, 1)
        self.assertIn("editMessageReplyMarkup", [m for m, _ in self.tg.calls])

    def test_default_account_unsets_the_inherited_config_dir(self):
        pid, _ = self.block(config_dir=None)
        with mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": "/acc/other", "GH_TOKEN": "x"}):
            self.tap(pid, 1)
        env = self.runs[0][1]["env"]
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertNotIn("GH_TOKEN", env)

    def test_follow_up_question_keeps_it_open_and_carries_history(self):
        pid, _ = self.block()
        self.replies = ["OPEN\nThe test suite timed out twice.", "RESOLVED\nRecorded."]
        self.message("why did it fail?", thread=8)
        self.assertEqual(ph._read("pending.json", {})[pid]["state"], "open")
        followup = self.tg.next_id  # the bot's answer to the question
        self.message("2, but file an issue for the flake", reply_to=followup)
        prompt = self.runs[1][0][2]
        self.assertIn("why did it fail?", prompt)
        self.assertIn("The test suite timed out twice.", prompt)
        self.assertEqual(ph._read("pending.json", {})[pid]["state"], "answered")

    def test_turns_are_capped(self):
        pid, _ = self.block()
        for _ in range(ph.MAX_TURNS + 1):
            self.message("why?", thread=8)
        self.assertEqual(len(self.runs), ph.MAX_TURNS)
        self.assertIn("at the computer", self.tg.sent()[-1]["text"])

    def test_failed_session_reports_and_stays_open(self):
        pid, _ = self.block()
        self.bridge.run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "not logged in")
        self.tap(pid, 1)
        self.assertIn("not logged in", self.tg.sent()[-1]["text"])
        self.assertEqual(ph._read("pending.json", {})[pid]["state"], "open")

    def test_a_decision_taken_for_the_user_is_a_silent_note_a_reply_overrules(self):
        pid, root = self.block(issue=64, options=("Ship as it is", "Another review round"), decided=True, category="process",
                               summary="The reviewers disagree on one small naming point.", labels=["Ship it", "One more round"])
        note = self.tg.sent()[-1]
        self.assertTrue(note["disable_notification"])
        self.assertEqual(note["text"].splitlines(), [
            "ℹ️ Decided for you · alpha #64", "The reviewers disagree on one small naming point.", "", "Retry or skip?",
            "Took 1. Ship it: Ship as it is", "", "Reply to this message to overrule it."])
        self.assertEqual([row[0]["text"] for row in note["reply_markup"]["inline_keyboard"]], ["Details"])
        self.assertEqual(ph._read("pending.json", {})[pid]["state"], "auto")
        self.assertFalse(self.bridge.waiting())  # nothing waits on the user: the Mac may sleep
        self.message("what's the status of this", thread=8)  # not a reply to the note: it is not the target
        self.assertEqual(self.runs, [])
        self.tap(pid, 2)  # and it has no option to tap
        self.assertEqual(self.runs, [])
        self.replies = ["RESOLVED\nRecorded: one more round."]
        self.message("no, do another round", reply_to=note_id(self.tg, note))
        prompt = self.runs[0][0][2]
        self.assertIn("The factory decided this one itself (it took option 1)", prompt)
        self.assertIn("this QUESTION about issue #64", prompt)
        self.assertEqual(ph._read("pending.json", {})[pid]["state"], "answered")
        self.assertRegex(self.edits()[-1]["text"], r"✓ Overruled \d\d:\d\d: no, do another round\n")

    def test_a_normal_answer_carries_no_overrule_note(self):
        pid, _ = self.block()
        self.tap(pid, 1)
        self.assertNotIn("decided this one itself", self.runs[0][0][2])
        self.assertIn("--kind <product|implementation|process>", self.runs[0][0][2])

    def test_strangers_are_ignored(self):
        pid, _ = self.block()
        self.tap(pid, 1, sender=7)
        self.message("1", thread=8, sender=7)
        self.assertEqual(self.runs, [])


class RoutingTests(PhoneTestCase):
    def test_topic_picks_its_own_project(self):
        a, _ = self.block("alpha", 1)
        b, root_b = self.block("beta", 2)
        self.message("2", thread=9)
        self.assertEqual(self.runs[0][1]["cwd"], root_b)

    def test_several_waiting_without_a_reply_asks_which(self):
        self.block("alpha", 1)
        self.block("alpha", 2)
        self.message("2", thread=8)
        self.assertEqual(self.runs, [])
        self.assertIn("alpha #1; alpha #2", self.tg.sent()[-1]["text"])
        self.message("2", reply_to=102)
        self.assertIn("issue #2", self.runs[0][0][2])

    def test_nothing_waiting(self):
        self.message("hello")
        self.assertIn("Nothing is waiting", self.tg.sent()[-1]["text"])


class StatusTests(PhoneTestCase):
    def test_status_in_a_topic_covers_that_project(self):
        _, ra = self.block("alpha", 1)
        _, rb = self.block("beta", 2)
        self.replies = ["supervisor: running"]
        self.message("factory status", thread=9)
        cmd, kw = self.runs[0]
        self.assertEqual(cmd[2:6], ["--root", rb, "factory", "status"])
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.tg.sent()[-1]["text"], "supervisor: running")

    def test_status_outside_topics_covers_every_project(self):
        self.block("alpha", 1)
        self.block("beta", 2)
        self.replies = ["running a", "running b"]
        self.message("/status")
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(self.tg.sent()[-1]["text"], "alpha\nrunning a\n\nbeta\nrunning b")


class HousekeepingTests(PhoneTestCase):
    def test_answered_at_the_computer_is_closed_out(self):
        pid, root = self.block()
        self.log(root, "queued")
        before = len(self.tg.sent())
        self.bridge.prune()
        self.assertEqual(ph._read("pending.json", {})[pid]["state"], "settled")
        self.assertRegex(self.edits()[-1]["text"], r"✓ Picked up at the computer \d\d:\d\d\.$")
        self.assertEqual(len(self.tg.sent()), before)  # shown on the message itself, no extra ping

    def test_events_before_the_message_dont_count(self):
        pid, root = self.block()
        with open(Path(root) / ".factory" / "log.jsonl", "a") as f:
            f.write(json.dumps({"ts": "2000-01-01T00:00:00Z", "kind": "queued", "issue": 12}) + "\n")
        self.bridge.prune()
        self.assertEqual(ph._read("pending.json", {})[pid]["state"], "open")
        self.assertTrue(self.bridge.busy_projects())

    def test_keeps_the_mac_awake_only_while_something_waits(self):
        procs = []

        def start():
            procs.append(mock.Mock(**{"poll.return_value": None}))
            return procs[-1]

        awake = ph.KeepAwake(start)
        self.assertFalse(self.bridge.waiting())
        pid, _ = self.block()
        self.assertTrue(self.bridge.waiting())
        awake.set(self.bridge.waiting())
        awake.set(self.bridge.waiting())
        self.assertEqual(len(procs), 1)  # held, not restarted
        self.replies = ["RESOLVED\nRecorded."]
        self.tap(pid, 1)
        awake.set(self.bridge.waiting())
        procs[0].terminate.assert_called_once()
        self.assertIsNone(awake.proc)

    def test_a_dead_caffeinate_is_restarted(self):
        procs = [mock.Mock(**{"poll.return_value": 0}), mock.Mock(**{"poll.return_value": None})]
        awake = ph.KeepAwake(lambda: procs.pop(0))
        awake.set(True)
        awake.set(True)
        self.assertEqual(procs, [])

    def test_parse_reply(self):
        self.assertEqual(ph.parse_reply("RESOLVED\nRecorded."), (True, "Recorded."))
        self.assertEqual(ph.parse_reply("\nopen\nWhich test?"), (False, "Which test?"))
        self.assertEqual(ph.parse_reply("no header"), (False, "no header"))


class SetupTests(PhoneTestCase):
    def test_setup_finds_the_private_chat_and_saves_privately(self):
        (self.tmp / "phone" / "config.json").unlink()
        calls = []

        def call(method, http_timeout=30, **p):
            calls.append((method, p))
            if method == "getMe":
                return {"username": "factory_bot", "has_topics_enabled": True}
            if method == "getUpdates":
                return [{"update_id": 5, "message": {"chat": {"id": 55, "type": "private"}, "from": {"id": 55}}}]
            return {"message_id": 1}

        with contextlib.redirect_stdout(io.StringIO()):
            cfg = ph.setup("tok", call=call)
        self.assertEqual((cfg["chat_id"], cfg["user_id"], cfg["topics"]), (55, 55, True))
        self.assertEqual(ph.load_config()["token"], "tok")
        self.assertEqual((self.tmp / "phone" / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertIn(("getUpdates", {"timeout": 0, "offset": 6}), calls)


if __name__ == "__main__":
    unittest.main()
