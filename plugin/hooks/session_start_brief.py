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
import re
import shutil
import socket
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
DREAM_DIGEST = Path(os.environ.get("WM_DREAM_DIGEST", str(DATA_DIR / "dream_digest.md")))

IGNORED_DIRS = {
    ".git", "node_modules", "target", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".turbo", ".cache", ".pytest_cache",
}


def _trunc_word(text: str, limit: int) -> str:
    s = str(text or "").rstrip()
    if len(s) <= limit:
        return s
    if limit <= 4:
        return s[:limit]
    cut = s[: limit - 3]
    sp = cut.rfind(" ")
    if sp >= int((limit - 3) * 0.55):
        cut = cut[:sp]
    cut = cut.rstrip(" ,;:-")
    return cut + "..."


def _fit_utf8_lines(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    lines = text.splitlines()
    kept = []
    for ln in lines:
        cand = "\n".join(kept + [ln])
        if len(cand.encode("utf-8")) <= max_bytes:
            kept.append(ln)
        else:
            break
    if kept:
        return "\n".join(kept)
    return _trunc_word(text, max_bytes - 4)


def _resolve_local_wm3_store():
    explicit = os.environ.get("WM3_STORE", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_dir() else None
    for cand in (HOME / ".local" / "share" / "whitemagic" / "gen3", Path("/Users/Shared/ai/wm-store-gen3")):
        if cand.is_dir():
            return cand
    return None


def _probe_local_gen3_records(store_path):
    if not store_path or os.environ.get("WM_SESSION_START_SKIP_GEN3") == "1":
        return None
    wm_bin = (
        os.environ.get("WM3_BIN")
        or shutil.which("wm-gen3")
        or shutil.which("wm3")
        or next(
            (str(p) for p in (HOME / ".local" / "bin" / "wm-gen3", HOME / ".local" / "bin" / "wm3") if p.is_file()),
            None,
        )
    )
    if wm_bin:
        try:
            res = subprocess.run(
                [wm_bin, "status", "--store", str(store_path)],
                capture_output=True,
                text=True,
                timeout=0.5,
            )
            if res.returncode == 0:
                m = re.search(r"Records Count:\s*([\d,]+)", res.stdout)
                if m:
                    return int(m.group(1).replace(",", ""))
        except Exception:
            pass
    uidx = store_path / "uuid_index.jsonl"
    if uidx.is_file():
        try:
            with uidx.open("rb") as fh:
                return sum(1 for _ in fh)
        except Exception:
            pass
    return None


def _local_host_telemetry():
    """Fast (<50ms) local disk & Gen3 store line rounded for Turn-0 prefix stability."""
    if os.environ.get("WM_LOCAL_TELEMETRY_OFF") == "1":
        return "", None, None
    host = socket.gethostname().split(".")[0] or "localhost"
    free_gb = None
    disk_part = ""
    try:
        du = shutil.disk_usage(str(HOME))
        free_gb = du.free / (1024 ** 3)
        used_pct = int(round((du.used / du.total) * 100)) if du.total else 0
        warn = " [LOW DISK]" if (free_gb < 15.0 or used_pct >= 90) else ""
        disk_part = f"Disk: {free_gb:.1f} GiB free ({used_pct}% used){warn}"
    except Exception:
        pass
    store = _resolve_local_wm3_store()
    gen3_cnt = _probe_local_gen3_records(store) if store else None
    gen3_part = ""
    if store is not None and gen3_cnt is not None:
        gen3_part = f"Gen3: {gen3_cnt:,} records ({store})"
    elif store is not None:
        gen3_part = f"Gen3: {store}"
    parts = [p for p in (host, disk_part, gen3_part) if p]
    return (f"[Local Host | {' | '.join(parts)}]" if parts else ""), free_gb, gen3_cnt


def _is_remote_preflight(raw_text, local_free_gb, local_gen3_cnt):
    flag = os.environ.get("WM_PREFLIGHT_IS_LOCAL", "").strip()
    if flag == "1":
        return False
    if flag == "0":
        return True
    # Detect synced brief authored on another OS/user host (e.g. ThinkPad Linux -> macOS Syncthing)
    if sys.platform == "darwin" and ("/home/" in raw_text or "home-lucas-" in raw_text):
        return True
    if "home-" in raw_text and HOME.name and f"home-{HOME.name}-" not in raw_text and "home-lucas-" in raw_text:
        return True
    head_block = "\n".join(raw_text.splitlines()[:6])
    if local_free_gb is not None:
        m_disk = re.search(r"Disk:\s*\*{0,2}([\d.]+)\s*GiB free", head_block)
        if m_disk:
            try:
                if abs(float(m_disk.group(1)) - local_free_gb) >= 5.0:
                    return True
            except ValueError:
                pass
    if local_gen3_cnt is not None:
        m_rec = re.search(r"Gen3 Primary Store:\s*\*{0,2}([\d,]+)\s*records", head_block)
        if m_rec:
            try:
                remote_cnt = int(m_rec.group(1).replace(",", ""))
                if abs(remote_cnt - local_gen3_cnt) > max(500, int(local_gen3_cnt * 0.25)):
                    return True
            except ValueError:
                pass
    return False


def build_brief():
    preflight_lines = []
    local_line, local_free_gb, local_gen3_cnt = _local_host_telemetry()
    if local_line:
        preflight_lines.append(local_line)

    if PREFLIGHT_PATH.exists():
        raw_text = PREFLIGHT_PATH.read_text(encoding="utf-8", errors="replace")
        lines = raw_text.splitlines()
        is_remote = _is_remote_preflight(raw_text, local_free_gb, local_gen3_cnt)
        if not (is_remote and os.environ.get("WM_PREFLIGHT_INCLUDE_REMOTE") == "0"):
            header = [l.strip() for l in lines[:4] if l.strip()]
            claims = [l.strip() for l in lines if "Claim:" in l or "Release:" in l][:2]
            if is_remote:
                preflight_lines.append(f"[Remote Estate Brief (Syncthing) | source={PREFLIGHT_PATH}]")
                # Filter out remote host's Disk / Gen3 Primary Store line so it is never confused with localhost
                filtered_hdr = [
                    h for h in header
                    if not ("Disk:" in h and ("Gen3 Primary Store:" in h or "Compiled in" in h))
                ]
                preflight_lines.extend(_trunc_word(h, 180) for h in filtered_hdr[:2])
            else:
                preflight_lines.append(f"[SessionStart Cached Brief | source={PREFLIGHT_PATH}]")
                preflight_lines.extend(_trunc_word(h, 180) for h in header[:3])
            if claims:
                preflight_lines.append("Recent Sangha Claims/Releases:")
                preflight_lines.extend(f"  {_trunc_word(c, 115)}" for c in claims)

    sim_header = ""
    sim_lines = []
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
            sim_header = f"SIM Ledger ({len(recent_exps)} recent lessons, {recent_fails} recent failures; query with `wm-sim query`):"
            for e in reversed(recent_exps[-3:]):
                outcome = _trunc_word(e.get("observed_outcome", ""), 95)
                sim_lines.append(
                    f"  - [{e.get('domain')}/{e.get('problem_group')}] ({e.get('status')}) {e.get('target')}: {outcome}"
                )

    dream_lines = []
    if DREAM_DIGEST.exists():
        try:
            age_days = (time.time() - DREAM_DIGEST.stat().st_mtime) / 86400.0
            if age_days <= 7.0:
                dtxt = DREAM_DIGEST.read_text(encoding="utf-8", errors="replace").strip()
                if dtxt:
                    dream_lines = [_trunc_word(ln.rstrip(), 180) for ln in dtxt.splitlines() if ln.strip()]
        except Exception:
            pass

    if not preflight_lines and not sim_header and not dream_lines:
        return ""

    # Assemble within 1450-byte budget cleanly on whole-line boundaries (Dream Digest preserved intact)
    out_lines = list(preflight_lines)
    dream_block = "\n".join(dream_lines)
    reserve_dream = len(dream_block.encode("utf-8")) + (1 if dream_block else 0)
    if sim_header:
        cand_h = "\n".join(out_lines + [sim_header])
        if len(cand_h.encode("utf-8")) + reserve_dream <= 1450:
            out_lines.append(sim_header)
            for sl in sim_lines:
                cand_s = "\n".join(out_lines + [sl])
                if len(cand_s.encode("utf-8")) + reserve_dream <= 1450:
                    out_lines.append(sl)

    for dl in dream_lines:
        cand_d = "\n".join(out_lines + [dl])
        if len(cand_d.encode("utf-8")) <= 1450:
            out_lines.append(dl)

    text = "\n".join(out_lines)
    return _fit_utf8_lines(text, 1450)


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
    host = "agy"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--host" and i + 1 < len(args):
            host = args[i + 1].strip().lower()
        elif a.startswith("--host="):
            host = a.split("=", 1)[1].strip().lower()

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

    if len(msg.encode("utf-8")) > 2550:
        msg = _fit_utf8_lines(msg, 2550)

    if host == "cursor":
        out = {"additional_context": msg}
    else:
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
