#!/usr/bin/env python3
"""
Portable (macOS + Linux) AGY SessionStart hook (Wave 3):
Injects a compact (<1800 char) immutable Turn-0 digest combining:
  - Cached preflight brief (when present)
  - Latest SIM lessons & failure count
  - Latest wm-dream consolidation digest (when <7d old)
Zero slow live probes; preserves Gemini 4 implicit prefix cache across turns.
"""

import json
import os
import sys
import time
from pathlib import Path

HOME = Path.home()
PREFLIGHT_PATH = Path(
    os.environ.get("WM_PREFLIGHT_PATH", str(HOME / "SharedWorkspace" / "PREFLIGHT_BRIEF.md"))
)
DATA_DIR = HOME / ".local" / "share" / "whitemagic"
SIM_LEDGER = Path(os.environ.get("WM_SIM_LEDGER", str(DATA_DIR / "sim_ledger.jsonl")))
DREAM_DIGEST = DATA_DIR / "dream_digest.md"


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
            parts.append(f"SIM Ledger ({len(recent_exps)} recent lessons, {recent_fails} recent failures; query with `wm-sim query`):")
            for e in reversed(recent_exps[-3:]):
                parts.append(
                    f"  - [{e.get('domain')}/{e.get('problem_group')}] ({e.get('status')}) {e.get('target')}: {e.get('observed_outcome','')[:115]}"
                )

    if DREAM_DIGEST.exists():
        try:
            age_days = (time.time() - DREAM_DIGEST.stat().st_mtime) / 86400.0
            if age_days <= 7.0:
                dtxt = DREAM_DIGEST.read_text(encoding="utf-8", errors="replace").strip()
                if dtxt:
                    parts.append(dtxt[:420])
        except Exception:
            pass

    if not parts:
        return ""
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
    out = {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": msg
                }
            }
        ]
    }
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
