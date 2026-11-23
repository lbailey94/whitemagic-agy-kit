#!/usr/bin/env python3
"""
Portable (macOS + Linux) Unified AGY SessionStart Hook (v4.0.0):
Combines into ONE immutable Turn-0 <SYSTEM_MESSAGE> (<= 2550 bytes):
  1. Cached preflight brief (when present; zero volatile per-minute timestamps)
  2. Latest SIM lessons & failure count
  3. Latest wm-dream consolidation digest (when <7d old)
  4. Compact workspace snapshot (git status/log + manifests + 2-level layout),
     suppressing noisy $HOME listings when cwd == $HOME without .git.
Zero slow live probes; preserves Gemini 4 implicit prefix cache across turns.
"""

import json
import os
import subprocess
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

IGNORED_DIRS = {
    ".git", "node_modules", "target", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".turbo", ".cache", ".pytest_cache",
}


def build_brief():
    parts = []
    if PREFLIGHT_PATH.exists():
        lines = PREFLIGHT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        header = [l.strip() for l in lines[:4] if l.strip()]
        claims = [l.strip() for l in lines if "Claim:" in l or "Release:" in l][:3]
        parts.append(f"[SessionStart Cached Brief | source={PREFLIGHT_PATH}]")
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
                    parts.append(dtxt[:360])
        except Exception:
            pass

    if not parts:
        return ""
    text = "\n".join(parts)
    if len(text) > 1450:
        text = text[:1447] + "..."
    return text


def _run_git(cwd_path, args):
    try:
        res = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=0.8,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def build_workspace_snapshot(workspace_dir):
    if not workspace_dir:
        return ""
    try:
        ws = Path(workspace_dir).expanduser().resolve()
        if not ws.is_dir():
            return ""
    except Exception:
        return ""

    # Suppress noisy $HOME folder listing when launched from home directory without .git
    try:
        if ws == HOME.resolve() and not (ws / ".git").exists():
            return "[Workspace] ~ (home dir; pass explicit repo cwd to batch_search/code_graph)"
    except Exception:
        pass

    lines = [f"[Workspace Snapshot] {ws}"]

    # Git summary (fast, <= 0.8s)
    if (ws / ".git").exists() or _run_git(ws, ["rev-parse", "--is-inside-work-tree"]) == "true":
        status = _run_git(ws, ["status", "--short", "--branch"])
        if status:
            st_lines = status.splitlines()
            branch_line = st_lines[0]
            dirty = st_lines[1:6]
            more = len(st_lines) - 1 - len(dirty)
            lines.append(f"Git: {branch_line}")
            if dirty:
                lines.append("Dirty: " + "; ".join(d.strip() for d in dirty) + (f" (+{more} more)" if more > 0 else ""))
        log = _run_git(ws, ["log", "-n", "3", "--oneline"])
        if log:
            lines.append("Commits: " + " | ".join(l[:64] for l in log.splitlines()[:3]))

    # Manifests
    manifests = []
    pkg = ws / "package.json"
    if pkg.is_file():
        try:
            pdata = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            scripts = list((pdata.get("scripts") or {}).keys())[:6]
            manifests.append(f"package.json(scripts:{','.join(scripts)})" if scripts else "package.json")
        except Exception:
            manifests.append("package.json")
    for mname in ("Cargo.toml", "pyproject.toml", "go.mod", "Makefile"):
        if (ws / mname).is_file():
            manifests.append(mname)
    if manifests:
        lines.append("Manifests: " + ", ".join(manifests))

    # Compact 2-level layout
    try:
        entries = sorted(
            [e for e in ws.iterdir() if e.name not in IGNORED_DIRS and not e.name.startswith(".")],
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
        top_items = []
        for e in entries[:14]:
            if e.is_dir():
                try:
                    subs = sorted(
                        [s.name + ("/" if s.is_dir() else "") for s in e.iterdir() if s.name not in IGNORED_DIRS and not s.name.startswith(".")]
                    )[:4]
                    top_items.append(f"{e.name}/({','.join(subs)})" if subs else f"{e.name}/")
                except Exception:
                    top_items.append(f"{e.name}/")
            else:
                top_items.append(e.name)
        if len(entries) > 14:
            top_items.append(f"+{len(entries) - 14} more")
        if top_items:
            lines.append("Layout: " + ", ".join(top_items))
    except Exception:
        pass

    text = "\n".join(lines)
    if len(text) > 1000:
        text = text[:997] + "..."
    return text


def extract_workspace_dir(payload):
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    for key in ("workspacePaths", "workspace_paths", "workspaceRoots", "workspace_roots"):
        val = payload.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.strip():
                    return item.strip()
    return ""


def main():
    if os.environ.get("WM_AGY_BENCH_VANILLA") == "1":
        sys.stdout.write("{}")
        sys.stdout.flush()
        return
    payload = {}
    try:
        raw = sys.stdin.read()
        if raw and raw.strip():
            payload = json.loads(raw)
    except Exception:
        payload = {}

    brief = build_brief()
    snap = build_workspace_snapshot(extract_workspace_dir(payload))
    msg = "\n\n".join(p for p in (brief, snap) if p)

    if not msg:
        sys.stdout.write("{}")
        sys.stdout.flush()
        return

    raw_bytes = msg.encode("utf-8")
    if len(raw_bytes) > 2550:
        msg = raw_bytes[:2547].decode("utf-8", errors="ignore") + "..."

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
