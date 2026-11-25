#!/usr/bin/env python3
"""
Portable (macOS + Linux) fast (<20ms, stdlib-only) custom AGY CLI statusLine renderer (Wave 3).
Shows:
  Model/Effort | Mode | Context Gauge (120k/180k cliff) |
  Live Cache Hit % + Warmth Countdown (<25s warm / 25-55s COOL / >=55s COLD) |
  Session Uncached Burn | Active Subagents | Git branch | Root Disk Free.
"""
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

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
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.0f}k"
    return str(n)


def fmt_age(sec):
    s = max(0, int(sec))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def resolve_transcript(data):
    tp = data.get("transcript_path") or data.get("transcriptPath")
    if tp and os.path.isfile(tp):
        return Path(tp)
    cid = data.get("conversation_id") or data.get("conversationId") or data.get("session_id")
    if cid:
        for surf in ("antigravity-cli", "antigravity", "antigravity-ide"):
            cand = Path.home() / ".gemini" / surf / "brain" / str(cid) / ".system_generated" / "logs" / "transcript.jsonl"
            if cand.is_file():
                return cand
    return None


def read_cache_telemetry(tp):
    """O(1) tail read (last 48 KB) of active transcript.jsonl (<3ms)."""
    try:
        sz = tp.stat().st_size
        with open(tp, "rb") as f:
            if sz > 49152:
                f.seek(sz - 49152)
            tail = f.read().decode("utf-8", errors="replace")
        last_hit = None
        last_ts = None
        recent_unc = 0
        for line in tail.splitlines():
            if '"PLANNER_RESPONSE"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "PLANNER_RESPONSE":
                continue
            unc = int(o.get("input_tokens") or 0)
            cac = int(o.get("cache_read_tokens") or 0)
            ctx = unc + cac
            if ctx > 0:
                recent_unc += unc
                last_hit = 100.0 * cac / ctx
            ts_raw = o.get("created_at")
            if ts_raw:
                try:
                    last_ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass
        return last_hit, last_ts, recent_unc
    except Exception:
        return None, None, 0


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

    # Live cache hit % + warmth countdown + uncached burn
    tp = resolve_transcript(data)
    if tp:
        hit, last_ts, rec_unc = read_cache_telemetry(tp)
        if hit is not None:
            h_col = GREEN if hit >= 85.0 else (YELLOW if hit >= 65.0 else RED)
            cache_seg = f"{h_col}hit:{hit:.0f}%{RST}"
            if last_ts is not None:
                age = max(0.0, time.time() - last_ts)
                if age < 25:
                    cache_seg += f" {GREEN}warm:{fmt_age(age)}{RST}"
                elif age < 55:
                    cache_seg += f" {YELLOW}COOL:{fmt_age(age)}!{RST}"
                else:
                    cache_seg += f" {RED}COLD:{fmt_age(age)}{RST}"
            if rec_unc > 0:
                cache_seg += f" {DIM}unc:{fmt_k(rec_unc)}{RST}"
            parts.append(cache_seg)

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
