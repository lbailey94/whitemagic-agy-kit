#!/usr/bin/env python3
"""
Portable (macOS + Linux) AGY PostInvocation Zero-Token Longevity HUD (v4.4.0):
Renders inline `metadata.user_markdown` alerts with `"systemMessage": ""` (0 model
tokens, 0 prefix-cache impact) when a session hits key cache/context milestones:
  - Model-flip cache eviction (>=50k uncached burn across all surfaces: App, IDE, CLI)
  - 120k context watch & 200k context cliff (App/IDE, where statusLine isn't visible)
  - Cold resume cache miss (<50% hit at >=80k ctx, >=50k uncached)

Fast stat-cached tail seek (<15ms, stdlib-only). 100% fail-open to `{}`.
"""

import json
import os
import re
import stat as stat_mod
import sys
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("WM_HOOK_STATE_DIR", "/tmp/agy-hook-state"))
_CONV_ID_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def _write_json_atomic(path: Path, payload_obj) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload_obj, fh)
    os.replace(tmp, path)


def analyze_tail(transcript_path: str):
    p = Path(transcript_path)
    st = p.stat()
    if not stat_mod.S_ISREG(st.st_mode):
        return None
    return p, st.st_size, st.st_mtime_ns


def _parse_iso_ts(val) -> float | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_tail_metrics(p: Path, sz: int):
    with open(p, "rb") as f:
        if sz > 49152:
            f.seek(sz - 49152)
        chunk = f.read().decode("utf-8", errors="replace")

    checkpoints = 0
    responses = []
    for line in chunk.splitlines():
        if not line.startswith("{"):
            continue
        if '"CHECKPOINT"' in line:
            try:
                if json.loads(line).get("type") == "CHECKPOINT":
                    checkpoints += 1
            except Exception:
                pass
            continue
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
        if unc + cac <= 0:
            continue
        responses.append(
            {
                "unc": unc,
                "cac": cac,
                "ctx": unc + cac,
                "model": str(o.get("model") or ""),
                "step": o.get("step_index"),
                "ts": _parse_iso_ts(o.get("created_at") or o.get("timestamp")),
            }
        )
    return checkpoints, responses[-2:]


def main():
    if os.environ.get("WM_AGY_BENCH_VANILLA") == "1" or os.environ.get("WM_LONGEVITY_HUD_OFF") == "1":
        emit({})
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        emit({})
        return

    tp = str(payload.get("transcriptPath") or "")
    conv_id = str(payload.get("conversationId") or "default")
    if not tp:
        emit({})
        return

    try:
        info = analyze_tail(tp)
        if not info:
            emit({})
            return
        p, sz, mtime_ns = info

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        sf = STATE_DIR / f"hud_{_CONV_ID_SANITIZE_RE.sub('', conv_id)[:20] or 'default'}.json"
        state = {}
        if sf.is_file():
            try:
                state = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                state = {}

        if state.get("path") == str(p) and state.get("sz") == sz and state.get("mtime_ns") == mtime_ns:
            emit({})
            return

        checkpoints, last_two = parse_tail_metrics(p, sz)
        state["path"] = str(p)
        state["sz"] = sz
        state["mtime_ns"] = mtime_ns

        if not last_two:
            _write_json_atomic(sf, state)
            emit({})
            return

        last_pr = last_two[-1]
        prev_pr = last_two[-2] if len(last_two) == 2 else None
        unc = last_pr["unc"]
        cac = last_pr["cac"]
        cur_ctx = last_pr["ctx"]
        hit_pct = 100.0 * cac / max(cur_ctx, 1)
        cur_model = last_pr["model"]
        prev_model = prev_pr["model"] if prev_pr else ""
        cur_inv = int(payload.get("invocationNum") or 0)
        raw_step = last_pr.get("step")
        step_key = str(raw_step) if raw_step is not None else f"inv:{cur_inv}:{sz}"
        t_prev = prev_pr.get("ts") if prev_pr else None
        t_last = last_pr.get("ts")
        idle_secs = int(t_last - t_prev) if (t_prev is not None and t_last is not None and t_last > t_prev) else 0

        # Reset context-threshold flags after compaction or context shrink
        prev_epoch = int(state.get("epoch") or 0)
        if checkpoints > prev_epoch:
            state["warned_120k"] = False
            state["warned_200k"] = False
            state["flip_count"] = 0
            state["flip_unc_total"] = 0
        elif cur_ctx < 90_000:
            state["warned_120k"] = False
            state["warned_200k"] = False
        state["epoch"] = max(prev_epoch, checkpoints)

        is_cli = "antigravity-cli" in tp
        hud_cli_full = os.environ.get("WM_LONGEVITY_HUD_CLI") == "1"
        flipped = bool(prev_model and cur_model and prev_model != cur_model)
        alerts = []

        handoff_dir = Path(os.environ.get("WM_HANDOFF_CACHE_DIR") or str(Path.home() / ".cache" / "wm-handoff"))
        san_id = _CONV_ID_SANITIZE_RE.sub("", conv_id)[:20] or "default"
        crystal_path = handoff_dir / f"latest_{san_id}.md"
        has_crystal = crystal_path.is_file()
        crystal_hint = f" Crystal ready: `{crystal_path}` (`wm-reset`)." if has_crystal else ""
        crystal_short = " Crystal ready (`wm-reset`)." if has_crystal else ""

        # 1. Model-flip cache eviction / Orchestrator ping-pong alert (all surfaces)
        if flipped and unc >= 50_000 and step_key != state.get("last_flip_step"):
            flip_count = int(state.get("flip_count") or 0) + 1
            flip_unc_total = int(state.get("flip_unc_total") or 0) + unc
            state["flip_count"] = flip_count
            state["flip_unc_total"] = flip_unc_total
            if flip_count >= 2 and (cur_ctx >= 99_000 or flip_unc_total >= 300_000):
                alerts.append(
                    f"⚡ **Orchestrator Ping-Pong Alert** ({flip_count} flips · ~{flip_unc_total / 1000:.0f}k uncached burned): "
                    f"Pin single-model Hydrogen, run `/compact`, or reset via `wm-reset`.{crystal_hint}"
                )
            else:
                alerts.append(
                    f"⚡ **Model Flip Cache Eviction** (`{prev_model}` → `{cur_model}`): "
                    f"burned **~{unc / 1000:.0f}k uncached tok** ({hit_pct:.0f}% hit). "
                    "Pin a single model (`Gemini 4 Hydrogen`) on >80k sessions."
                )
            state["last_flip_step"] = step_key

        # 2. App/IDE context thresholds & cold-resume misses (CLI already renders statusLine)
        if (not is_cli) or hud_cli_full:
            if cur_ctx >= 200_000 and not state.get("warned_200k"):
                alerts.append(
                    f"🔴 **Context Cliff (~{cur_ctx / 1000:.0f}k tok · {hit_pct:.0f}% cache hit):** "
                    f"High re-prefill cost on any idle gap. Run `/compact` or `wm-handoff` soon.{crystal_short}"
                )
                state["warned_200k"] = True
                state["warned_120k"] = True
            elif cur_ctx >= 120_000 and not state.get("warned_120k"):
                alerts.append(
                    f"🟡 **Context Watch (~{cur_ctx / 1000:.0f}k tok · {hit_pct:.0f}% cache hit):** "
                    f"Run `/compact` or `wm-handoff` before stepping away >5m.{crystal_short}"
                )
                state["warned_120k"] = True

            last_cold_inv = int(state.get("last_cold_inv", -99))
            if (
                not flipped
                and cur_ctx >= 80_000
                and hit_pct < 50.0
                and unc >= 50_000
                and (cur_inv - last_cold_inv) >= 5
            ):
                if idle_secs >= 300:
                    alerts.append(
                        f"❄️ **Cold Resume After Idle (~{cur_ctx / 1000:.0f}k tok · {hit_pct:.0f}% hit · idle {idle_secs // 60}m):** "
                        f"Re-prefilled ~{unc / 1000:.0f}k uncached tok. Use `wm-handoff` before resuming idle sessions.{crystal_short}"
                    )
                else:
                    alerts.append(
                        f"❄️ **Cold Cache Miss (~{cur_ctx / 1000:.0f}k tok · {hit_pct:.0f}% hit):** "
                        f"Re-prefilled ~{unc / 1000:.0f}k uncached tok. Use `wm-handoff` before resuming idle sessions.{crystal_short}"
                    )
                state["last_cold_inv"] = cur_inv

        _write_json_atomic(sf, state)

        if alerts:
            emit(
                {
                    "injectSteps": [
                        {
                            "systemMessage": {
                                "systemMessage": "",
                                "metadata": {
                                    "user_markdown": "> " + "\n> ".join(alerts),
                                },
                            }
                        }
                    ],
                    "terminationBehavior": "",
                }
            )
            return
    except Exception:
        pass
    emit({})


if __name__ == "__main__":
    main()
