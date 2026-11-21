#!/usr/bin/env python3
"""
Portable (macOS + Linux) AGY Stop Telemetry Hook (Wave 3):
Fires ONCE when an agent turn completes (zero per-tool overhead).
Performs a fast O(1) tail read of transcriptPath and appends a compact rolling
cache/credit record to ~/.local/share/whitemagic/agy_telemetry.jsonl.
Always returns `{"decision": "allow"}` so it never blocks or extends execution.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TELEM_PATH = Path(
    os.environ.get(
        "WM_AGY_TELEMETRY",
        str(Path.home() / ".local" / "share" / "whitemagic" / "agy_telemetry.jsonl"),
    )
)


def summarize_tail(transcript_path):
    p = Path(transcript_path)
    if not p.is_file():
        return None
    sz = p.stat().st_size
    with open(p, "rb") as f:
        if sz > 131072:
            f.seek(sz - 131072)
        chunk = f.read().decode("utf-8", errors="replace")

    turns = 0
    unc_sum = 0
    cac_sum = 0
    out_sum = 0
    spills = 0
    last_ctx = 0
    last_hit = None
    model = ""

    for line in chunk.splitlines():
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type", "")
        if t == "PLANNER_RESPONSE":
            unc = int(o.get("input_tokens") or 0)
            cac = int(o.get("cache_read_tokens") or 0)
            out = int(o.get("output_tokens") or 0)
            ctx = unc + cac
            if ctx > 0:
                turns += 1
                unc_sum += unc
                cac_sum += cac
                out_sum += out
                last_ctx = ctx
                last_hit = round(100.0 * cac / ctx, 1)
            if o.get("model"):
                model = str(o["model"])
        elif t == "GENERIC":
            c = str(o.get("content") or "")
            if "output.txt" in c and "truncated" in c.lower():
                spills += 1

    if turns == 0:
        return None
    return {
        "model": model,
        "sampled_turns": turns,
        "last_ctx": last_ctx,
        "last_hit_pct": last_hit,
        "session_uncached": unc_sum,
        "session_cached": cac_sum,
        "session_out": out_sum,
        "spills": spills,
    }


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tp = payload.get("transcriptPath") or ""
        stats = summarize_tail(tp) if tp else None
        if stats:
            now = time.time()
            rec = {
                "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "ts_epoch": int(now),
                "conv_id": str(payload.get("conversationId") or "")[:16],
                "reason": str(payload.get("terminationReason") or ""),
                "cwd": str(payload.get("cwd") or ""),
                **stats,
            }
            TELEM_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(TELEM_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    sys.stdout.write(json.dumps({"decision": "allow"}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
