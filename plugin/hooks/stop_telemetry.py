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
import re
import shutil
import subprocess
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
HANDOFF_CACHE_DIR = Path(
    os.environ.get("WM_HANDOFF_CACHE_DIR") or str(Path.home() / ".cache" / "wm-handoff")
)
_CONV_ID_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")


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
    max_single_unc = 0
    model_flips = 0
    gaps_30s = 0
    compactions = 0
    spills = 0
    last_ctx = 0
    last_hit = None
    model = ""
    prev_model = ""
    prev_ts = None

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
            cur_m = str(o.get("model") or "")
            if ctx > 0:
                turns += 1
                unc_sum += unc
                cac_sum += cac
                out_sum += out
                last_ctx = ctx
                last_hit = round(100.0 * cac / ctx, 1)
                if unc > max_single_unc:
                    max_single_unc = unc
                if cur_m and prev_model and cur_m != prev_model:
                    model_flips += 1
                if cur_m:
                    prev_model = cur_m
                ts_s = o.get("created_at") or o.get("timestamp")
                if ts_s:
                    try:
                        dt = datetime.fromisoformat(str(ts_s).replace("Z", "+00:00"))
                        if prev_ts is not None and (dt - prev_ts).total_seconds() >= 30.0:
                            gaps_30s += 1
                        prev_ts = dt
                    except Exception:
                        pass
            if cur_m:
                model = cur_m
        elif t == "CHECKPOINT":
            compactions += 1
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
        "max_single_uncached": max_single_unc,
        "model_flips": model_flips,
        "gaps_30s": gaps_30s,
        "compactions": compactions,
        "spills": spills,
    }


def _resolve_handoff_bin() -> str | None:
    env_bin = os.environ.get("WM_HANDOFF_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    candidates = [
        Path(__file__).resolve().parents[2] / "bin" / "wm-handoff",
        Path.home() / ".local" / "bin" / "wm-handoff",
    ]
    which_bin = shutil.which("wm-handoff")
    if which_bin:
        candidates.append(Path(which_bin))
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _load_grimoire_tuning(host: str = "agy") -> dict:
    p = Path(
        os.environ.get("WM_GRIMOIRE_PROFILE")
        or str(Path.home() / ".local" / "share" / "whitemagic" / "grimoire_profile.json")
    )
    if not p.is_file():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        hp = obj.get("host_profiles") or {}
        if isinstance(hp, dict) and isinstance(hp.get(host), dict):
            return hp[host]
        mt = obj.get("model_tuning") or {}
        return mt if isinstance(mt, dict) else {}
    except Exception:
        return {}


def _maybe_auto_crystal(payload: dict, stats: dict, now: float, host: str = "agy") -> None:
    if os.environ.get("WM_AUTO_CRYSTAL_OFF") == "1":
        return
    if payload.get("fullyIdle") is False:
        return
    reason = str(payload.get("terminationReason") or "")
    if reason not in ("NO_TOOL_CALL", ""):
        return
    is_neon = "neon" in str(stats.get("model") or "").lower()
    tuning = _load_grimoire_tuning(host)
    if is_neon:
        warn_tok = min(100_000, int(tuning.get("neon_warn_tokens") or tuning.get("context_warn_tokens") or 100_000))
        cliff_tok = min(120_000, int(tuning.get("neon_rollover_tokens") or tuning.get("context_cliff_tokens") or 120_000))
    else:
        warn_tok = min(99_000, int(tuning.get("context_warn_tokens") or 99_000))
        cliff_tok = min(130_000, int(tuning.get("context_cliff_tokens") or 130_000))
    flip_guard = bool(tuning.get("enable_flip_guard", True))

    last_ctx = int(stats.get("last_ctx") or 0)
    flips = int(stats.get("model_flips") or 0)
    if flip_guard:
        should_fire = (last_ctx >= warn_tok and flips >= 2) or last_ctx >= cliff_tok
    else:
        should_fire = last_ctx >= cliff_tok or (last_ctx >= warn_tok and flips >= 1)
    if not should_fire:
        return
    tp = str(payload.get("transcriptPath") or "")
    if not tp or not Path(tp).is_file():
        return
    handoff_bin = _resolve_handoff_bin()
    if not handoff_bin:
        return

    raw_conv = str(payload.get("conversationId") or "default")
    san = _CONV_ID_SANITIZE_RE.sub("", raw_conv)[:20] or "default"
    cache_dir = Path(os.environ.get("WM_HANDOFF_CACHE_DIR") or str(HANDOFF_CACHE_DIR))
    crystal_out = cache_dir / f"latest_{san}.md"
    receipt_out = cache_dir / f"latest_{san}.receipt.json"
    tp_sz = Path(tp).stat().st_size

    if receipt_out.is_file() and crystal_out.is_file():
        try:
            prev_rec = json.loads(receipt_out.read_text(encoding="utf-8"))
            prev_mtime = receipt_out.stat().st_mtime
            prev_tp_sz = int(prev_rec.get("transcript_bytes") or 0)
            if (now - prev_mtime) < 120.0 and (tp_sz - prev_tp_sz) < 25_000:
                return
        except Exception:
            pass

    cmd = [
        sys.executable,
        "-S",
        handoff_bin,
        "--transcript",
        tp,
        "--out",
        str(crystal_out),
        "--emit-receipt",
        str(receipt_out),
        "--spool",
        "--spool-source",
        "stop_telemetry",
    ]
    cwd = str(payload.get("cwd") or "")
    if cwd and Path(cwd).is_dir():
        cmd.extend(["--cwd", cwd])
    env = os.environ.copy()
    if not env.get("WM_PENDING_CRYSTALS"):
        telem_override = env.get("WM_AGY_TELEMETRY", "").strip()
        if telem_override:
            env["WM_PENDING_CRYSTALS"] = str(Path(telem_override).parent / "pending_crystals.jsonl")
    subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1.5)


def main():
    host = "agy"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--host" and i + 1 < len(args):
            host = args[i + 1].strip().lower()
        elif a.startswith("--host="):
            host = a.split("=", 1)[1].strip().lower()

    if os.environ.get("WM_AGY_BENCH_VANILLA") == "1":
        sys.stdout.write("{}" if host == "cursor" else json.dumps({"decision": "allow"}))
        sys.stdout.flush()
        return

    if host == "cursor":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            now = time.time()
            roots = payload.get("workspace_roots")
            cwd = payload.get("cwd") or (roots[0] if isinstance(roots, list) and roots else "")
            conv_id = str(payload.get("conversation_id") or payload.get("session_id") or "")[:16]
            rec = {
                "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "ts_epoch": int(now),
                "host": "cursor",
                "conv_id": conv_id,
                "model": str(payload.get("model") or payload.get("model_id") or ""),
                "reason": str(payload.get("status") or ""),
                "loop_count": int(payload.get("loop_count") or 0),
                "cwd": str(cwd or ""),
            }
            TELEM_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(TELEM_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        sys.stdout.write("{}")
        sys.stdout.flush()
        return

    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tp = payload.get("transcriptPath") or ""
        stats = summarize_tail(tp) if tp else None
        if stats:
            now = time.time()
            is_neon = "neon" in str(stats.get("model") or "").lower()
            last_ctx = int(stats.get("last_ctx") or 0)
            rec = {
                "ts": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "ts_epoch": int(now),
                "conv_id": str(payload.get("conversationId") or "")[:16],
                "reason": str(payload.get("terminationReason") or ""),
                "cwd": str(payload.get("cwd") or ""),
                **stats,
            }
            if is_neon:
                rec["neon_warn"] = last_ctx >= 100_000
                rec["neon_rollover_recommended"] = last_ctx >= 120_000
            TELEM_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(TELEM_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            try:
                _maybe_auto_crystal(payload, stats, now)
            except Exception:
                pass
    except Exception:
        pass
    sys.stdout.write(json.dumps({"decision": "allow"}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
