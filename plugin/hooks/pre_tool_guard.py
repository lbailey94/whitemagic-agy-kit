#!/usr/bin/env python3
"""
Portable (macOS + Linux) AGY PreToolUse data-loss guard:
Covers built-in run_command AND agy-fast execution tools (run_checks / apply_edits.verify).
Forces explicit operator confirmation ('force_ask') on irreversible destructive commands;
returns explicit '{"decision": "allow"}' on safe commands.
"""

import json
import re
import sys

DESTRUCTIVE_PATTERNS = [
    (
        r"(?:^|[;&|]\s*)rm\s+-[^\s]*r[^\s]*f[^\s]*\s+(?:/\s*$|/\*|\~|\$HOME(?:\s|$)|/(?:home|Users)/[^/\s]+(?:\s|$))",
        "Recursive force-delete targeting root or home directory",
    ),
    (r"\bdrop\s+(?:table|database|schema|view)\b", "SQL DROP statement detected"),
    (r"\btruncate\s+table\b", "SQL TRUNCATE TABLE detected"),
    (r"\bgcloud\s+projects\s+delete\b", "Destructive gcloud project deletion detected"),
    (r"\b(?:gsutil|gcloud\s+storage)\s+rm\b", "Cloud Storage deletion command detected"),
    (r"\bgit\s+push\s+(?:--force|-f)\b.*\b(?:main|master)\b", "Force-push to main/master branch detected"),
    (
        r"\bmkfs\.[a-z0-9]+\b|\bdd\s+if=.*\bof=/dev/|\bdiskutil\s+(?:eraseDisk|eraseVolume|partitionDisk)\b",
        "Raw disk/block device formatting or overwrite detected",
    ),
]


def extract_commands(args):
    cmds = []
    for key in ("CommandLine", "command", "verify"):
        val = args.get(key)
        if val:
            cmds.append(str(val))
    raw_cmds = args.get("commands")
    if isinstance(raw_cmds, list):
        for item in raw_cmds:
            if isinstance(item, dict) and item.get("command"):
                cmds.append(str(item["command"]))
            elif isinstance(item, str):
                cmds.append(item)
    return cmds


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.stdout.write(json.dumps({"decision": "allow"}))
        sys.stdout.flush()
        return

    tool_call = payload.get("toolCall") or {}
    args = tool_call.get("args") or tool_call.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"command": args}

    for cmd in extract_commands(args):
        for pattern, reason in DESTRUCTIVE_PATTERNS:
            if re.search(pattern, cmd, flags=re.IGNORECASE):
                sys.stdout.write(
                    json.dumps(
                        {
                            "decision": "force_ask",
                            "reason": f"[Data-Loss Guard] {reason}. Confirm explicitly before proceeding.",
                        }
                    )
                )
                sys.stdout.flush()
                return

    sys.stdout.write(json.dumps({"decision": "allow"}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
