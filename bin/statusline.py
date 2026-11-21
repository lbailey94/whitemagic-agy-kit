#!/usr/bin/env python3
"""
Portable (macOS + Linux) fast (<25ms, stdlib-only) custom AGY CLI statusLine renderer.
Shows: Model/Effort | Mode | Context Gauge (color-coded for 120k/180k TTFT cliff) |
Active Subagents | Git branch | Root Disk Free (warns <15 GiB).
"""
import json
import os
import shutil
import sys

RST = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"


def short_git_branch(cwd):
    if not cwd:
        return ""
    head = os.path.join(cwd, ".git", "HEAD")
    try:
        if os.path.isfile(head):
            raw = open(head, "r", encoding="utf-8", errors="replace").read().strip()
            if raw.startswith("ref: refs/heads/"):
                return raw[16:]
            return raw[:7]
    except Exception:
        pass
    return ""


def fmt_k(n):
    if not n:
        return "0"
    if n >= 1000:
        return f"{n/1000:.0f}k"
    return str(n)


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    parts = []
    model = data.get("model") or {}
    m_name = model.get("display_name") or model.get("id") or "agy"
    effort = model.get("effort")
    m_str = f"{CYAN}{BOLD}{m_name}{RST}"
    if effort and effort.lower() not in m_name.lower():
        m_str += f" {DIM}({effort}){RST}"
    parts.append(m_str)

    mode = data.get("cycle_mode")
    if mode:
        parts.append(f"{MAGENTA}[{mode}]{RST}")

    cw = data.get("context_window") or {}
    used_pct = cw.get("used_percentage")
    in_tok = cw.get("total_input_tokens") or 0
    over_200k = bool(data.get("exceeds_200k_tokens"))
    if used_pct is not None or in_tok:
        if over_200k or in_tok >= 180000:
            c_col = RED
            tip = " /compact!"
        elif in_tok >= 120000:
            c_col = YELLOW
            tip = " >120k"
        else:
            c_col = GREEN
            tip = ""
        pct_s = f" {used_pct:.0f}%" if isinstance(used_pct, (int, float)) else ""
        parts.append(f"{c_col}ctx:{fmt_k(in_tok)}{pct_s}{tip}{RST}")

    subs = data.get("subagents") or []
    running_subs = sum(1 for s in subs if isinstance(s, dict) and s.get("status") == "running")
    if running_subs:
        parts.append(f"{BLUE}agents:{running_subs}{RST}")

    ws = data.get("workspace") or {}
    cwd = ws.get("current_dir") or data.get("cwd") or ""
    branch = short_git_branch(cwd)
    if branch:
        parts.append(f"{DIM}git:{branch}{RST}")

    try:
        free_gb = shutil.disk_usage("/").free / (1024 ** 3)
        d_col = RED if free_gb < 15.0 else (YELLOW if free_gb < 30.0 else GREEN)
        parts.append(f"{d_col}disk:{free_gb:.1f}G{RST}")
    except Exception:
        pass

    sys.stdout.write(f" {DIM}|{RST} ".join(parts) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
