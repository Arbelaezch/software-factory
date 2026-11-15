#!/usr/bin/env python3
"""The phone channel (docs/factory-design.md section 7): factory blockers go to a Telegram bot, and the user answers
from the phone.

One bot and one bridge serve every factory on this machine, whatever Claude or GitHub account each project uses.
State lives outside the projects, in ~/.config/software-factory/phone (FACTORY_PHONE_HOME overrides it):
  config.json    bot token, chat and user ids, whether topics work (written by `harness phone setup`, mode 600)
  projects.json  project root -> name, Telegram topic, Claude config dir to answer with
  pending.json   blockers sent and not yet settled
  bridge.json    the running bridge (pid)
The bridge only makes outgoing HTTPS requests (long polling), so it works from any network.

Each project gets its own topic in the chat when the bot has topics turned on; otherwise every message carries the
project name and replies are matched by the message they reply to. A tap or a reply goes to a short Sonnet session in
the project (with the project's Claude account), which records the answer and re-queues the issue the way /factory
does, or answers a follow-up question. `status` and the Details button are answered without a model.

A message leads with the plain-language summary and short option labels the factory session wrote; the full standard
message is behind Details. A question the factory decided itself arrives as a silent one-line note that a reply
overrules, and a message that was answered is edited to show the choice, the time and whether the issue runs again.
Standard library only.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

HOME = Path(os.environ.get("FACTORY_PHONE_HOME") or "~/.config/software-factory/phone").expanduser()
HARNESS = Path(__file__).resolve().parent / "harness.py"
PLUGIN_DIR = Path(__file__).resolve().parent.parent
API = "https://api.telegram.org/bot{token}/{method}"
MAX_TURNS = 4            # user messages per blocker answered from the phone; past this, finish at the computer
IDLE_EXIT = 30 * 60      # the bridge exits after this long with no supervisor running and nothing pending
PRUNE_EVERY = 120        # seconds between checks for blockers settled at the computer
EXPIRE_DAYS = 7
ANSWER_TIMEOUT = 900
KIND_MARK = {"QUESTION": "❓", "BLOCKER": "⛔", "DECIDED": "ℹ️"}
LABEL_MAX = 40           # an option button, number and star included
TITLE_MAX = 60           # the issue's title next to its number
STATUS_RE = re.compile(r"^/?(?:factory\s+)?status(?:@\w+)?$", re.I)
HELP_RE = re.compile(r"^/?(?:help|start)(?:@\w+)?$", re.I)


def iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


# --------------------------------------------------------------------------- files
def _read(name: str, default):
    try:
        return json.loads((HOME / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(name: str, data, private: bool = False) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = HOME / f"{name}.tmp"
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if private:
        tmp.chmod(0o600)
    tmp.replace(HOME / name)


@contextmanager
def locked():
    """One writer at a time across processes (supervisors, issue sessions and the bridge all write here)."""
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_config() -> dict | None:
    cfg = _read("config.json", None)
    return cfg if cfg and cfg.get("token") and cfg.get("chat_id") else None


def configured() -> bool:
    return load_config() is not None


# --------------------------------------------------------------------------- Telegram
class TelegramError(RuntimeError):
    pass


def api(token: str, method: str, http_timeout: float = 30, **params):
    req = urllib.request.Request(API.format(token=token, method=method), data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except ValueError:
            raise TelegramError(f"{method}: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise TelegramError(f"{method}: {e}") from None
    if not body.get("ok"):
        raise TelegramError(f"{method}: {body.get('description', 'failed')}")
    return body.get("result")


class Bot:
    """The few Bot API calls the factory uses. Tests swap `call` for a fake."""

    def __init__(self, cfg: dict, call: Callable | None = None):
        self.cfg = cfg
        self.call = call or (lambda method, http_timeout=30, **p: api(cfg["token"], method, http_timeout, **p))

    def send(self, text: str, thread: int | None = None, buttons: list[list[dict]] | None = None,
             reply_to: int | None = None, silent: bool = False) -> int:
        p = {"chat_id": self.cfg["chat_id"], "text": text[:4000], "link_preview_options": {"is_disabled": True}}
        if silent:
            p["disable_notification"] = True
        if thread:
            p["message_thread_id"] = thread
        if buttons:
            p["reply_markup"] = {"inline_keyboard": buttons}
        if reply_to:
            p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.call("sendMessage", **p)["message_id"]

    def edit(self, message_id: int, text: str, buttons: list[list[dict]] | None = None) -> bool:
        """Replace a sent message's text and buttons. False when Telegram refuses (too old, deleted)."""
        try:
            self.call("editMessageText", chat_id=self.cfg["chat_id"], message_id=message_id, text=text[:4000],
                      link_preview_options={"is_disabled": True}, reply_markup={"inline_keyboard": buttons or []})
            return True
        except TelegramError:
            return False

    def clear_buttons(self, message_id: int) -> None:
        try:
            self.call("editMessageReplyMarkup", chat_id=self.cfg["chat_id"], message_id=message_id,
                      reply_markup={"inline_keyboard": []})
        except TelegramError:
            pass  # already cleared, or too old to edit

    def typing(self, thread: int | None) -> None:
        try:
            self.call("sendChatAction", chat_id=self.cfg["chat_id"], action="typing",
                      **({"message_thread_id": thread} if thread else {}))
        except TelegramError:
            pass


# --------------------------------------------------------------------------- sending (called from harness)
def register_project(root: str, name: str, config_dir: str | None, bot: Bot | None = None) -> dict:
    """Remember a project and give it a topic. Returns its entry. Topics that fail once are turned off for good
    (the bot doesn't have them enabled); messages then carry the project name instead."""
    cfg = load_config()
    bot = bot or Bot(cfg)
    with locked():
        projects = _read("projects.json", {})
        entry = projects.get(root) or {}
        entry.update({"name": name, "config_dir": config_dir or ""})
        entry.setdefault("seen", iso())
        cfg = _read("config.json", cfg)
        if cfg.get("topics", True) and not entry.get("thread"):
            try:
                entry["thread"] = bot.call("createForumTopic", chat_id=cfg["chat_id"], name=name[:128])["message_thread_id"]
            except (TelegramError, KeyError, TypeError) as e:
                cfg["topics"] = False
                cfg["topics_error"] = str(e)[:200]
                _write("config.json", cfg, private=True)
        projects[root] = entry
        _write("projects.json", projects)
    return entry


def option_label(p: dict, i: int) -> str:
    """The short name of option i (1-based): the label the session wrote, else the option itself."""
    labels = p.get("labels") or []
    return labels[i - 1] if len(labels) == len(p["options"]) else p["options"][i - 1]


def details_button(pid: str) -> list[dict]:
    return [{"text": "Details", "callback_data": f"d:{pid}"}]


def blocker_buttons(pid: str, p: dict) -> list[list[dict]]:
    """One button per option, each readable on its own, then Details (the full standard message)."""
    rows = []
    for i in range(1, len(p["options"]) + 1):
        star = " ★" if i == p.get("recommended") else ""
        label, room = f"{i}. {option_label(p, i)}", LABEL_MAX - len(star)
        rows.append([{"text": (label if len(label) <= room else label[:room - 3] + "...") + star,
                      "callback_data": f"a:{pid}:{i}"}])
    return rows + [details_button(pid)]


def log_events(root: str) -> list[dict]:
    """The project's recent factory log events, newest first."""
    try:
        lines = (Path(root) / ".factory" / "log.jsonl").read_text(encoding="utf-8").splitlines()[-2000:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def overview(root: str, pid: str | None = None) -> str:
    """What else is going on in the project, for the last line of a message: what waits on the user besides this
    one, and what was decided for them or merged since they last tapped or wrote here. Empty when nothing is."""
    seen = (_read("projects.json", {}).get(root) or {}).get("seen") or ""
    recent = [e.get("kind") for e in log_events(root) if str(e.get("ts", "")) > seen]
    more = len([k for k, v in _read("pending.json", {}).items() if v["root"] == root and v["state"] == "open" and k != pid])
    since = [f"{n} {what}" for n, what in ((recent.count("auto_accepted"), "decided for you"), (recent.count("merged"), "merged")) if n]
    parts = ([f"{more} more waiting on you"] if more else []) + ([", ".join(since) + " since you last looked"] if since else [])
    return ("Also: " + "; ".join(parts) + ".") if parts else ""


def blocker_text(p: dict) -> str:
    """What the phone shows for one pending message: the plain summary, then what to decide and the options under
    the numbers their buttons carry. File paths and the engineer's account stay behind Details."""
    ref = f" #{p['issue']}" if p.get("issue") not in (None, "") else ""
    title = " ".join((p.get("title") or "").split())
    name = f" · {title if len(title) <= TITLE_MAX else title[:TITLE_MAX - 3].rstrip() + '...'}" if title and ref else ""
    state = "Decided for you · " if p["kind"] == "DECIDED" else "" if p["options"] else "Stopped · "
    head = f"{KIND_MARK.get(p['kind'], '')} {state}{p['project']}{ref}{name}".strip()
    lead = [p.get("summary") or p.get("situation")] if p.get("summary") or p.get("situation") else []
    if p["kind"] == "DECIDED":
        k = p["recommended"]
        took = f"Took {k}. {option_label(p, k)}" + (f": {p['options'][k - 1]}" if p.get("labels") else "")
        return "\n".join([head, *lead, "", p["need"], took])
    if not p["options"]:
        action = "Needs you at the computer" if p.get("category") == "infrastructure" else "What I need"
        return "\n".join([head, *lead, "", f"{action}: {p['need']}"])
    lines = [head, *lead, "", p["need"]]
    for i, o in enumerate(p["options"], 1):
        star = " ★" if i == p.get("recommended") else ""
        lines += [f"{i}. {option_label(p, i)}{star}"] + ([f"   {o}"] if p.get("labels") else [])
    return "\n".join(lines)


def footer(p: dict) -> str:
    if p["kind"] == "DECIDED":
        return "Reply to this message to overrule it."
    if p["options"]:
        return "Tap an option, or reply to this message (\"2, but ...\", or a question)."
    if p.get("category") == "infrastructure":
        return "Nothing to decide here. Reply \"retry\" once it is fixed, or ask a question."
    return "Reply to this message with what to do, or ask a question."


def send_blocker(root: str, project: str, issue, kind: str, text: str, need: str, options: list[str] | None,
                 recommended: int | None, config_dir: str | None, bot: Bot | None = None, *, summary: str = "",
                 labels: list[str] | None = None, category: str = "", situation: str = "", title: str = "",
                 decided: bool = False) -> str:
    """Send one QUESTION or BLOCKER with a button per option, and remember it so the answer can find its way back
    to this project. `text` is the full standard message (behind Details, and what the answer session reads);
    `title` is the issue's title, shown next to its number.
    `decided`: the factory took the recommended option itself; the message is a silent note with no buttons that a
    reply overrules. Returns the pending id."""
    cfg = load_config()
    if not cfg:
        raise TelegramError("the phone is not set up (run `harness phone setup`)")
    bot = bot or Bot(cfg)
    entry = register_project(root, project, config_dir, bot)
    pid = secrets.token_hex(4)
    options = [o for o in (options or []) if o.strip()]
    p = {"root": root, "project": project, "issue": issue, "kind": "DECIDED" if decided else kind.upper(), "text": text,
         "need": need, "options": options, "recommended": recommended, "labels": list(labels or []),
         "summary": summary, "situation": situation, "category": category, "title": title, "thread": entry.get("thread"),
         "ts": iso(), "turns": [], "state": "auto" if decided else "open"}
    p["shown"] = blocker_text(p)
    also = overview(root)
    body = p["shown"] + "\n\n" + (also + "\n" if also else "") + footer(p)
    p["message_id"] = bot.send(body, p["thread"], [details_button(pid)] if decided or not options else blocker_buttons(pid, p),
                               silent=decided)
    with locked():
        pending = _read("pending.json", {})
        pending[pid] = p
        _write("pending.json", pending)
    return pid


def mark_seen(root: str) -> None:
    """The user tapped or wrote in this project: the "since you last looked" counts start again from now."""
    with locked():
        projects = _read("projects.json", {})
        if root in projects:
            projects[root]["seen"] = iso()
            _write("projects.json", projects)


def send_text(text: str, root: str | None = None) -> None:
    cfg = load_config()
    if not cfg:
        raise TelegramError("the phone is not set up (run `harness phone setup`)")
    thread = (_read("projects.json", {}).get(root) or {}).get("thread") if root else None
    Bot(cfg).send(text, thread)


# --------------------------------------------------------------------------- bridge lifecycle
def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def bridge_pid() -> int | None:
    info = _read("bridge.json", None)
    return int(info["pid"]) if info and pid_alive(info.get("pid")) else None


def ensure_bridge() -> None:
    """Start the bridge detached if it isn't running. A second one started at the same moment exits at once."""
    if not configured() or bridge_pid():
        return
    HOME.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("HARNESS_ROOT", "CLAUDE_PROJECT_DIR")}
    with open(HOME / "bridge.log", "a", encoding="utf-8") as out:
        subprocess.Popen([sys.executable, str(HARNESS), "phone", "serve"], stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, start_new_session=True, cwd=str(HOME), env=env)


def supervisor_info(root: str) -> dict | None:
    """The project's running supervisor ({pid, started, issues}), or None."""
    try:
        info = json.loads((Path(root) / ".factory" / "supervisor.json").read_text())
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and pid_alive(info.get("pid")) else None


def supervisor_running(root: str) -> bool:
    return supervisor_info(root) is not None


def settled_elsewhere(p: dict) -> bool:
    """The issue was queued or started again after the message went out: answered at the computer."""
    return queued_since(p, p["ts"], strict=True)


def queued_since(p: dict, since: str, strict: bool = False) -> bool:
    """Whether the issue was queued or started again after `since` (`strict`: not in that same second)."""
    for e in log_events(p["root"]):
        ts = str(e.get("ts", ""))
        if ts < since or (strict and ts == since):
            return False
        if e.get("issue") == p.get("issue") and e.get("kind") in ("queued", "harness_started"):
            return True
    return False


def outcome_line(p: dict, choice: str, since: str) -> str:
    """What an answered message shows under its text: the choice, the time, and whether the issue runs again."""
    # A supervisor the answer just started names the issue before it has logged queueing it.
    starting = p.get("issue") in ((supervisor_info(p["root"]) or {}).get("issues") or [])
    if not queued_since(p, since) and not starting:
        again = "Recorded. Not queued again."
    elif supervisor_running(p["root"]):
        again = "Queued again: the factory runs it after the issue it is on."
    else:
        again = "Queued again, but the factory is not running: it runs when the factory is started."
    choice = " ".join(choice.split())
    return f"✓ {'Overruled' if p['state'] == 'auto' else 'Answered'} {time.strftime('%H:%M')}: " \
           f"{choice if len(choice) <= 80 else choice[:77] + '...'}\n{again}"


# --------------------------------------------------------------------------- the bridge
def project_env(entry: dict) -> dict:
    """Environment for commands run in a project: that project's Claude account, nothing inherited that would
    point gh or the harness somewhere else."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CONFIG_DIR", "GH_TOKEN", "HARNESS_ROOT", "CLAUDE_PROJECT_DIR", "CLAUDECODE")}
    if entry.get("config_dir"):
        env["CLAUDE_CONFIG_DIR"] = entry["config_dir"]
    return env


def harness_cmd(root: str, *args: str) -> list[str]:
    return [sys.executable, str(HARNESS), "--root", root, *args]


ANSWER_PROMPT = """You answer the user's phone replies for the software factory in project "{project}". They replied
in Telegram to this {kind} about issue #{issue}:

{text}
{note}
Earlier on the phone about it:
{history}

The user's new message:
{message}

Decide which it is and act:
- An answer (an option, an option with a tweak, or their own instruction): record it, then resume the issue, exactly as
  /factory does:
  1. `{h} factory decide --question "<the question, from What I need>" --answer "<their answer, with the tweak>" --by user --kind <product|implementation|process> [--why "<their reasoning>"]`
  2. If the supervisor is running (`{h} factory status` says so): `{h} factory queue {issue}`. If it is not running:
     `{h} factory start --issues {issue}` (it queues the issue itself; don't run both).
  If the answer is "skip it", "not now" or leave it, record it and don't queue.
  "retry" or "fixed" for a blocker that had nothing to decide: there is nothing to record, only step 2.
- A question: answer it from the files. `{h} factory status --issue {issue}` shows the full message; the run
  directories are under .harness/runs/ (plan.md, review-*.json, notes-*.md, summary.md) and the issue session logs
  under .factory/sessions/. Don't act on it.
- Unclear: ask one short question back.
Never edit code, run /ship, merge, or touch git. If a command fails, say what failed in one line.

Reply for a phone screen: plain text, at most 6 short lines, no markdown. The first line of your reply must be exactly
RESOLVED (you recorded an answer) or OPEN (anything else), and the rest is what the user reads."""


AUTO_NOTE = """
The factory decided this one itself (it took option {k}) and went on with the issue. The user is now overruling it or
asking about it. To overrule: record their decision as in step 1 (`--by user`; it replaces the assumption). Then look
at `{h} factory status`: only if the issue is still open and is not being worked on right now, do step 2. Otherwise
don't queue; say the decision is recorded, that the work so far used the automatic choice, and that changing it
needs the issue run again or a follow-up issue.
"""


def history_text(turns: list[dict]) -> str:
    return "\n".join(f"- user: {t['user']}\n  you: {t['bot']}" for t in turns) or "(nothing yet)"


def answer_command(p: dict, message: str, harness_prefix: str) -> list[str]:
    note = AUTO_NOTE.format(k=p["recommended"], h=harness_prefix) if p["state"] == "auto" else ""
    prompt = ANSWER_PROMPT.format(project=p["project"], kind="QUESTION" if p["state"] == "auto" else p["kind"],
                                  issue=p["issue"], text=p["text"], note=note, history=history_text(p["turns"]),
                                  message=message, h=harness_prefix)
    allowed = [f"Bash({harness_prefix} factory {sub} *)" for sub in ("decide", "queue", "start", "status")]
    return [os.environ.get("HARNESS_CLAUDE", "claude"), "-p", prompt, "--model", "sonnet",
            "--add-dir", str(PLUGIN_DIR), "--allowedTools", *allowed, "Read", "Grep", "Glob"]


def parse_reply(out: str) -> tuple[bool, str]:
    lines = out.strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    first = lines[0].strip().upper() if lines else ""
    if first in ("RESOLVED", "OPEN"):
        return first == "RESOLVED", "\n".join(lines[1:]).strip() or ("Recorded." if first == "RESOLVED" else "...")
    return False, out.strip() or "(no reply)"


class Bridge:
    """Routes the user's taps and replies to the right project. Side effects are injected for tests."""

    def __init__(self, bot: Bot, run: Callable[..., subprocess.CompletedProcess] | None = None,
                 spawn: Callable[[Callable], None] | None = None):
        self.bot = bot
        self.run = run or (lambda cmd, **kw: subprocess.run(cmd, capture_output=True, text=True, **kw))
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.busy: set[str] = set()
        self.guard = threading.Lock()

    # ---- incoming
    def handle(self, update: dict) -> None:
        cfg = self.bot.cfg
        if "callback_query" in update:
            q = update["callback_query"]
            if str(q.get("from", {}).get("id")) != str(cfg.get("user_id")):
                return
            self.on_tap(q)
            return
        msg = update.get("message") or {}
        if str(msg.get("from", {}).get("id")) != str(cfg.get("user_id")) or str(msg.get("chat", {}).get("id")) != str(cfg["chat_id"]):
            return  # not the owner, or not the factory chat
        text = (msg.get("text") or "").strip()
        if not text:
            return
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        if HELP_RE.match(text):
            self.bot.send(HELP, thread)
            return
        if STATUS_RE.match(text):
            self.status(thread, msg["message_id"])
            return
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        pid, why = self.target(reply_to, thread)
        if pid:
            self.answer(pid, text, msg["message_id"], text)
        else:
            self.bot.send(why, thread, reply_to=msg["message_id"])

    def on_tap(self, q: dict) -> None:
        m = re.fullmatch(r"([ad]):([0-9a-f]+)(?::(\d))?", q.get("data") or "")
        try:
            self.bot.call("answerCallbackQuery", callback_query_id=q["id"])
        except TelegramError:
            pass
        if not m:
            return
        pid, p = m.group(2), _read("pending.json", {}).get(m.group(2))
        if m.group(1) == "d":  # Details: the full standard message, no model
            if p:
                mark_seen(p["root"])
            self.bot.send(p["text"] if p else "That one is no longer on file.", (p or {}).get("thread"),
                          reply_to=(p or {}).get("message_id"))
            return
        i = int(m.group(3) or 0)
        if not p or p["state"] != "open" or not 1 <= i <= len(p["options"]):
            self.bot.send("That one is already settled.", (p or {}).get("thread"))
            return
        label = option_label(p, i)
        self.answer(pid, f"Option {i}: {p['options'][i - 1]}" if label == p["options"][i - 1]
                    else f"Option {i} ({label}): {p['options'][i - 1]}", None, f"{i}. {label}")

    def target(self, reply_to: int | None, thread: int | None) -> tuple[str | None, str]:
        """(the pending id a message is for, "") or (None, why there isn't one, for the user)."""
        every = _read("pending.json", {})
        if reply_to:  # a reply also reaches a decision the factory took itself (to overrule it)
            for k, v in every.items():
                if v["state"] in ("open", "auto") and (v["message_id"] == reply_to or reply_to in v.get("replies", [])):
                    return k, ""
        pending = {k: v for k, v in every.items() if v["state"] == "open"}
        here = {k: v for k, v in pending.items() if thread is None or v.get("thread") == thread}
        if len(here) == 1:
            return next(iter(here)), ""
        if not here:
            return None, "Nothing is waiting on you here. Send \"status\" to see where things are."
        names = "; ".join(f"{v['project']} #{v['issue']}" for v in here.values())
        return None, f"Several things are waiting ({names}). Reply to the message you're answering."

    # ---- answering
    def answer(self, pid: str, message: str, user_msg: int | None, choice: str) -> None:
        """`choice` is what the message shows once this settles it: the option tapped, or the user's words."""
        with self.guard:
            if pid in self.busy:
                self.bot.send("Still working on your last reply to this one.", None, reply_to=user_msg)
                return
            self.busy.add(pid)
        p = _read("pending.json", {})[pid]
        mark_seen(p["root"])
        if len(p["turns"]) >= MAX_TURNS:
            self.busy.discard(pid)
            self.bot.send("That's as far as I go from the phone. Pick it up at the computer with /factory.",
                          p.get("thread"), reply_to=user_msg or p["message_id"])
            return
        self.bot.typing(p.get("thread"))
        self.spawn(lambda: self._answer(pid, p, message, user_msg, choice))

    def _answer(self, pid: str, p: dict, message: str, user_msg: int | None, choice: str) -> None:
        entry = _read("projects.json", {}).get(p["root"]) or {}
        since = iso()
        prefix = " ".join(["python3", str(HARNESS), "--root", p["root"]])
        try:
            r = self.run(answer_command(p, message, prefix), cwd=p["root"], env=project_env(entry), timeout=ANSWER_TIMEOUT)
            resolved, reply = parse_reply(r.stdout) if r.returncode == 0 else (False, f"The answer session failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
        except (OSError, subprocess.TimeoutExpired) as e:
            resolved, reply = False, f"The answer session failed: {e}"
        try:
            sent = self.bot.send(("✓ " if resolved else "") + reply, p.get("thread"), reply_to=user_msg or p["message_id"])
        except TelegramError:
            sent = None
        with locked():
            pending = _read("pending.json", {})
            cur = pending.get(pid)
            if cur:
                cur["turns"].append({"user": message, "bot": reply})
                if sent:
                    cur.setdefault("replies", []).append(sent)
                if resolved:
                    cur["state"] = "answered"
                _write("pending.json", pending)
        if resolved:
            self.settle(pid, p, outcome_line(p, choice, since))
        with self.guard:
            self.busy.discard(pid)

    def settle(self, pid: str, p: dict, line: str) -> bool:
        """Show on the message itself how it ended; only Details stays tappable."""
        if self.bot.edit(p["message_id"], f"{p.get('shown') or p['text']}\n\n{line}", [details_button(pid)]):
            return True
        self.bot.clear_buttons(p["message_id"])
        return False

    # ---- status
    def status(self, thread: int | None, user_msg: int) -> None:
        projects = _read("projects.json", {})
        roots = [r for r, e in projects.items() if thread is None or e.get("thread") == thread]
        for root in roots:
            mark_seen(root)
        if not roots:
            self.bot.send("No factory project has talked to this bot yet.", thread, reply_to=user_msg)
            return
        parts = []
        for root in roots:
            e = projects[root]
            if thread is None and len(roots) > 1 and not supervisor_running(root) and not self.open_for(root):
                parts.append(f"{e['name']}: idle")
                continue
            r = self.run(harness_cmd(root, "factory", "status", "--events", "4"), cwd=root, env=project_env(e), timeout=120)
            body = (r.stdout if r.returncode == 0 else r.stderr or r.stdout).strip()
            parts.append(f"{e['name']}\n{body}" if thread is None else body)
        self.bot.send("\n\n".join(parts) or "(nothing)", thread, reply_to=user_msg)

    def open_for(self, root: str) -> list[str]:
        return [k for k, v in _read("pending.json", {}).items() if v["root"] == root and v["state"] == "open"]

    # ---- housekeeping
    def prune(self) -> None:
        """Close out blockers answered at the computer, or too old to matter."""
        cutoff = iso(time.time() - EXPIRE_DAYS * 86400)
        done = []
        with locked():
            pending = _read("pending.json", {})
            for k, v in list(pending.items()):
                if v["state"] != "open":
                    if v["ts"] < cutoff:
                        del pending[k]
                    continue
                if settled_elsewhere(v):
                    v["state"] = "settled"
                    done.append((k, v))
                elif v["ts"] < cutoff:
                    v["state"] = "expired"
            _write("pending.json", pending)
        for k, v in done:
            if self.settle(k, v, f"✓ Picked up at the computer {time.strftime('%H:%M')}."):
                continue
            try:
                self.bot.send(f"#{v['issue']} was picked up at the computer.", v.get("thread"), reply_to=v["message_id"])
            except TelegramError:
                pass

    def waiting(self) -> bool:
        """Something is waiting on the user, or an answer is being worked on."""
        return bool(self.busy) or any(v["state"] == "open" for v in _read("pending.json", {}).values())

    def busy_projects(self) -> bool:
        return self.waiting() or any(supervisor_running(r) for r in _read("projects.json", {}))


class KeepAwake:
    """Holds `caffeinate -i` while something waits on the user. A supervisor keeps the Mac awake only while it runs,
    and it stops once every queued issue is blocked; without this the Mac would sleep on the answer. Released as soon
    as nothing waits, so an idle Mac still sleeps. `-w` ties it to the bridge, so it never outlives it."""

    def __init__(self, start: Callable[[], subprocess.Popen | None] | None = None):
        self.start = start or self._caffeinate
        self.proc = None

    @staticmethod
    def _caffeinate():
        if not shutil.which("caffeinate"):
            return None
        return subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def set(self, on: bool) -> None:
        if on and (self.proc is None or self.proc.poll() is not None):
            self.proc = self.start()
        elif not on and self.proc is not None:
            self.proc.terminate()
            self.proc = None


HELP = ("I pass the factory's questions and blockers to you.\n"
        "- Tap an option, or reply to the message: \"2\", \"2, but keep the old endpoint\", or a question.\n"
        "- Details: the full message, with the files and logs it points to.\n"
        "- A note that something was decided for you: reply to it to overrule.\n"
        "- \"status\": where the factory is (in a project's topic: that project; here: all of them).")


def serve(bot: Bot | None = None, forever: bool = False) -> None:
    """Long-poll Telegram and route each update. Exits after IDLE_EXIT with no supervisor running and nothing
    pending, unless `forever`. Only one bridge runs per machine."""
    cfg = load_config()
    if not cfg:
        sys.exit("the phone is not set up (run `harness phone setup`)")
    HOME.mkdir(parents=True, exist_ok=True)
    lock = open(HOME / "bridge.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another bridge has it
    _write("bridge.json", {"pid": os.getpid(), "started": iso()})
    bridge = Bridge(bot or Bot(cfg))
    state = _read("state.json", {})
    offset = state.get("offset")
    last_busy, last_prune = time.time(), 0.0
    awake = KeepAwake()
    print(f"{iso()} bridge started (pid {os.getpid()})", flush=True)
    try:
        while True:
            try:
                updates = bridge.bot.call("getUpdates", http_timeout=60, timeout=50,
                                          allowed_updates=["message", "callback_query"], **({"offset": offset} if offset else {}))
            except TelegramError as e:
                print(f"{iso()} {e}", flush=True)
                time.sleep(10)
                updates = []
            for u in updates or []:
                offset = u["update_id"] + 1
                _write("state.json", {"offset": offset})
                try:
                    bridge.handle(u)
                except Exception as e:  # one bad update must not stop the bridge
                    print(f"{iso()} update {u.get('update_id')}: {e!r}", flush=True)
            now = time.time()
            if now - last_prune > PRUNE_EVERY:
                last_prune = now
                bridge.prune()
                awake.set(bridge.waiting())
                if bridge.busy_projects():
                    last_busy = now
                elif not forever and now - last_busy > IDLE_EXIT:
                    print(f"{iso()} idle, exiting", flush=True)
                    return
    finally:
        awake.set(False)
        (HOME / "bridge.json").unlink(missing_ok=True)


# --------------------------------------------------------------------------- setup
def setup(token: str, wait: int = 120, call: Callable | None = None) -> dict:
    """Check the token, find the user's chat from a message they sent the bot, and save the config."""
    call = call or (lambda method, http_timeout=30, **p: api(token, method, http_timeout, **p))
    me = call("getMe")
    print(f"bot: @{me.get('username')}. Send it any message from your phone (open t.me/{me.get('username')} and press Start).")
    deadline = time.time() + wait
    offset = None
    while time.time() < deadline:
        updates = call("getUpdates", http_timeout=40, timeout=30, allowed_updates=["message"], **({"offset": offset} if offset else {}))
        for u in updates or []:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if msg.get("chat", {}).get("type") == "private":
                cfg = {"token": token, "chat_id": msg["chat"]["id"], "user_id": msg["from"]["id"],
                       "bot": me.get("username"), "topics": bool(me.get("has_topics_enabled"))}
                call("getUpdates", http_timeout=10, timeout=0, offset=offset)  # mark it read so the bridge doesn't answer it
                with locked():
                    _write("config.json", cfg, private=True)
                Bot(cfg, call).send(
                    "Connected. Factory questions and blockers will arrive here"
                    + (", one topic per project." if cfg["topics"] else ".\n(Turn on topics for this bot in @BotFather "
                       "to get one topic per project, then run `harness phone setup` again.)"))
                return cfg
    raise TelegramError(f"no message reached the bot within {wait}s; send it a message and run setup again")
