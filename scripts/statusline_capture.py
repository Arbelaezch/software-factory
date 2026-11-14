#!/usr/bin/env python3
"""Statusline command: snapshot the JSON Claude Code sends and print a short status line.

The snapshot lands in <config dir>/statusline-latest.json (written atomically) so other
tools can read the latest session and rate-limit data without a live session. The config dir is the
session's $CLAUDE_CONFIG_DIR (else ~/.claude), so each Claude account keeps its own snapshot.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

def snapshot_path():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "statusline-latest.json"


def write_snapshot(data):
    path = snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".statusline-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    write_snapshot(data)

    parts = [(data.get("model") or {}).get("display_name", "")]
    limits = data.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        used = (limits.get(key) or {}).get("used_percentage")
        if used is not None:
            parts.append(f"{label} {used:.0f}%")
    print(" | ".join(p for p in parts if p))


if __name__ == "__main__":
    main()
