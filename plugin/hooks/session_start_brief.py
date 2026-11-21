#!/usr/bin/env python3
"""
Portable (macOS + Linux) AGY SessionStart hook:
Injects a compact (<1500 char) cached preflight brief (when present) and recent
Self-Improvement Memory (SIM) lessons at Turn 0 without slow live probes.
"""

import json
import os
import sys
import time
from pathlib import Path

HOME = Path.home()
PREFLIGHT_PATH = Path(os.environ.get("WM_PREFLIGHT_BRIEF", str(HOME / "SharedWorkspace" / "PREFLIGHT_BRIEF.md")))
SIM_LEDGER = Path(os.environ.get("WM_SIM_LEDGER", str(HOME / ".local" / "share" / "whitemagic" / "sim_ledger.jsonl")))


def build_brief():
    parts = []
    if PREFLIGHT_PATH.exists():
        age_min = int((time.time() - PREFLIGHT_PATH.stat().st_mtime) / 60)
        lines = PREFLIGHT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        header = [l.strip() for l in lines[:4] if l.strip()]
        claims = [l.strip() for l in lines if "Claim:" in l or "Release:" in l][:3]
        parts.append(f"[SessionStart Cached Brief | age={age_min}m | source={PREFLIGHT_PATH}]")
        parts.extend(header[:3])
        if claims:
            parts.append("Recent Sangha Claims/Releases:")
            parts.extend(f"  {c[:130]}" for c in claims)

    if SIM_LEDGER.exists():
        recent_exps = []
        recent_fails = 0
        for raw in SIM_LEDGER.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if e.get("event") == "experiment":
                recent_exps.append(e)
            elif e.get("event") == "tool_failure":
                recent_fails += 1
        if recent_exps or recent_fails:
            parts.append(
                f"SIM Ledger ({len(recent_exps)} recent lessons, {recent_fails} recent failures; query with `wm-sim query`):"
            )
            for e in reversed(recent_exps[-3:]):
                parts.append(
                    f"  - [{e.get('domain')}/{e.get('problem_group')}] ({e.get('status')}) "
                    f"{e.get('target')}: {e.get('observed_outcome', '')[:120]}"
                )

    text = "\n".join(parts)
    if len(text) > 1800:
        text = text[:1797] + "..."
    return text


def main():
    try:
        _ = sys.stdin.read()
    except Exception:
        pass
    msg = build_brief()
    if not msg:
        sys.stdout.write("{}")
        sys.stdout.flush()
        return
    out = {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
