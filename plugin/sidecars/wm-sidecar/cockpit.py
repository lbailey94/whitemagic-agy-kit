#!/usr/bin/env python3
"""
WhiteMagic AGY Sidecar Live Cockpit Engine (v4.7.0, stdlib-only).
Provides:
  - Fast stat-cached live IDE/CLI/Swarm transcript tracker + Neon TTL warmth timer
  - Mutually-exclusive uncached burn decomposition (Flips, TTL Expiries, Warm Work, Partial/Compactions, Turn-0)
  - Lock-free pending_crystals.jsonl spool status
  - One-click loopback action backends: handoff crystal+pointer, Turn-0 audit, zero-LLM wm3 recall, spool flush
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SWARM_NAME_CID_RE = re.compile(r'"name"\s*:\s*"([^"]+)"[^}]*?"conversation_id"\s*:\s*"([0-9a-f-]{36})"')
SWARM_CID_NAME_RE = re.compile(r'"conversation_id"\s*:\s*"([0-9a-f-]{36})"[^}]*?"name"\s*:\s*"([^"]+)"')
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_CONV_SAN_RE = re.compile(r"[^a-zA-Z0-9_-]")
_RECALL_HDR_RE = re.compile(r"^\[(\d+)\]\s+Record\s+#(\S+)\s+\(score:\s*([0-9.]+)\)")

_TX_LOCK = threading.Lock()
_TX_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}


def redact_home(val: str) -> str:
    if not val:
        return ""
    home_str = str(Path.home())
    if val == home_str:
        return "~"
    if val.startswith(home_str + "/"):
        return "~" + val[len(home_str) :]
    return val


def surface_brains() -> dict[str, Path]:
    return {
        "app": Path(os.environ.get("WM_AGY_APP_BRAIN") or str(Path.home() / ".gemini" / "antigravity" / "brain")),
        "ide": Path(os.environ.get("WM_AGY_IDE_BRAIN") or str(Path.home() / ".gemini" / "antigravity-ide" / "brain")),
        "cli": Path(os.environ.get("WM_AGY_CLI_BRAIN") or str(Path.home() / ".gemini" / "antigravity-cli" / "brain")),
    }


def pending_crystals_path() -> Path:
    explicit = os.environ.get("WM_PENDING_CRYSTALS", "").strip()
    if explicit:
        return Path(explicit)
    telem = os.environ.get("WM_AGY_TELEMETRY", "").strip()
    if telem:
        return Path(telem).parent / "pending_crystals.jsonl"
    return Path.home() / ".local" / "share" / "whitemagic" / "pending_crystals.jsonl"


def spool_status() -> dict[str, Any]:
    sp = pending_crystals_path()
    flushed_p = sp.with_name("flushed_crystals.sha256")
    pending_shas: set[str] = set()
    flushed_shas: set[str] = set()
    if flushed_p.is_file():
        try:
            for ln in flushed_p.read_text(encoding="utf-8", errors="replace").splitlines():
                s = ln.strip()
                if s:
                    flushed_shas.add(s)
        except Exception:
            pass
    if sp.is_file():
        try:
            for ln in sp.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln.startswith("{"):
                    continue
                try:
                    obj = json.loads(ln)
                    sha = str(obj.get("crystal_sha256") or "")
                    if sha and sha not in flushed_shas:
                        pending_shas.add(sha)
                except Exception:
                    continue
        except Exception:
            pass
    return {
        "path": redact_home(str(sp)),
        "pending_count": len(pending_shas),
        "flushed_count": len(flushed_shas),
    }


def _unwrap_json_str(v: Any) -> Any:
    if isinstance(v, str) and len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        try:
            u = json.loads(v)
            if isinstance(u, str):
                return u
        except Exception:
            pass
    return v


def parse_transcript_metrics(tp: Path, surface: str) -> dict[str, Any]:
    st = tp.stat()
    key = str(tp)
    with _TX_LOCK:
        cached = _TX_CACHE.get(key)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return cached[2]

    turns = 0
    unc_sum = cac_sum = out_sum = 0
    last_ctx = 0
    last_unc = 0
    last_hit_pct: float | None = None
    last_model = ""
    last_ts_epoch: float | None = None
    prev_model = ""
    prev_ts: datetime | None = None
    prev_ctx = 0
    flips = 0
    compactions = 0
    saw_checkpoint = False
    latest_goal = ""
    swarm_workers: list[tuple[str, str]] = []
    seen_workers: set[str] = set()
    pending_names: list[str] = []

    buckets: dict[str, dict[str, int]] = {
        "flips": {"turns": 0, "tokens": 0},
        "ttl_expiries": {"turns": 0, "tokens": 0},
        "warm_work": {"turns": 0, "tokens": 0},
        "compactions_partial": {"turns": 0, "tokens": 0},
        "turn_0": {"turns": 0, "tokens": 0},
    }

    try:
        raw_lines = tp.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        raw_lines = []

    for line in raw_lines:
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        if t == "USER_INPUT":
            txt = " ".join(str(o.get("content") or "").split())
            if txt:
                latest_goal = txt[:200]
        elif t == "PLANNER_RESPONSE":
            u = int(o.get("input_tokens") or 0)
            c = int(o.get("cache_read_tokens") or 0)
            ot = int(o.get("output_tokens") or 0)
            m = str(o.get("model") or "")
            for tc in o.get("tool_calls") or []:
                if (tc.get("name") or "") == "swarm":
                    args = tc.get("args") or tc.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if isinstance(args, dict):
                        if _unwrap_json_str(args.get("action")) == "add":
                            wn = _unwrap_json_str(args.get("name"))
                            if wn:
                                pending_names.append(str(wn))
            if (u + c + ot) > 0:
                turns += 1
                ctx = u + c
                turn_hit = (100.0 * c / ctx) if ctx > 0 else 0.0
                unc_sum += u
                cac_sum += c
                out_sum += ot
                if ctx > 0:
                    last_ctx = ctx
                    last_unc = u
                    last_hit_pct = round(turn_hit, 1)
                if m:
                    last_model = m
                flipped = bool(m and prev_model and m != prev_model)
                if flipped:
                    flips += 1
                gap_s = 0.0
                ts_s = o.get("created_at") or o.get("timestamp")
                if ts_s:
                    try:
                        dt = datetime.fromisoformat(str(ts_s).replace("Z", "+00:00"))
                        last_ts_epoch = dt.timestamp()
                        if prev_ts is not None:
                            gap_s = max(0.0, (dt - prev_ts).total_seconds())
                        prev_ts = dt
                    except Exception:
                        pass

                if turns == 1:
                    bkey = "turn_0"
                elif flipped:
                    bkey = "flips"
                elif (
                    saw_checkpoint
                    or (prev_ctx >= 40_000 and ctx > 0 and ctx < int(prev_ctx * 0.85))
                    or (50.0 <= turn_hit < 90.0 and u >= 15_000 and gap_s < 60.0)
                ):
                    bkey = "compactions_partial"
                elif (
                    (gap_s >= 60.0 and turn_hit < 85.0)
                    or (gap_s >= 180.0 and u >= 25_000)
                    or (turn_hit < 50.0 and ctx >= 50_000)
                ):
                    bkey = "ttl_expiries"
                else:
                    bkey = "warm_work"
                buckets[bkey]["turns"] += 1
                buckets[bkey]["tokens"] += u
                saw_checkpoint = False
                if ctx > 0:
                    prev_ctx = ctx
                if m:
                    prev_model = m
        elif t == "CHECKPOINT":
            compactions += 1
            saw_checkpoint = True
        elif t == "GENERIC":
            content = str(o.get("content") or "")
            if "conversation_id" in content:
                for match in SWARM_NAME_CID_RE.finditer(content):
                    wn, wc = match.group(1), match.group(2)
                    if wc not in seen_workers:
                        seen_workers.add(wc)
                        swarm_workers.append((wn, wc))
                for match in SWARM_CID_NAME_RE.finditer(content):
                    wc, wn = match.group(1), match.group(2)
                    if wc not in seen_workers:
                        seen_workers.add(wc)
                        swarm_workers.append((wn, wc))
                if '"status":"added"' in content.replace(" ", ""):
                    for uid in UUID_RE.findall(content):
                        if uid not in seen_workers:
                            seen_workers.add(uid)
                            lbl = pending_names.pop(0) if pending_names else f"worker-{uid[:8]}"
                            swarm_workers.append((lbl, uid))

    conv_id = tp.parents[2].name if len(tp.parents) > 2 else tp.parent.name
    avg_hit = round(100.0 * cac_sum / max(unc_sum + cac_sum, 1), 1) if (unc_sum + cac_sum) > 0 else None
    breakdown = {}
    for bkey, bval in buckets.items():
        btoks = bval["tokens"]
        btrns = bval["turns"]
        breakdown[bkey] = {
            "turns": btrns,
            "tokens": btoks,
            "pct": round(100.0 * btoks / max(unc_sum, 1), 1) if unc_sum > 0 else 0.0,
            "avg_per_turn": round(btoks / btrns) if btrns > 0 else 0,
        }

    result = {
        "conv_id": conv_id,
        "surface": surface,
        "transcript_path": str(tp),
        "mtime": st.st_mtime,
        "model": last_model,
        "turns": turns,
        "last_ctx": last_ctx,
        "last_uncached_delta": last_unc,
        "last_hit_pct": last_hit_pct,
        "avg_hit_pct": avg_hit,
        "tot_uncached": unc_sum,
        "tot_cached": cac_sum,
        "tot_output": out_sum,
        "flips": flips,
        "compactions": compactions,
        "last_turn_epoch": last_ts_epoch or st.st_mtime,
        "latest_goal": latest_goal,
        "breakdown": breakdown,
        "swarm_worker_refs": swarm_workers,
    }
    with _TX_LOCK:
        if len(_TX_CACHE) > 96:
            _TX_CACHE.clear()
        _TX_CACHE[key] = (st.st_mtime_ns, st.st_size, result)
    return result


def _ttl_classification(idle_sec: int) -> tuple[str, float]:
    if idle_sec <= 15:
        return "warm", 97.8
    if idle_sec <= 30:
        return "warm", 96.1
    if idle_sec <= 60:
        return "cooling", 90.2
    if idle_sec <= 180:
        return "cold", 84.7
    return "cold", 51.8


def collect_live_cockpit(now: float | None = None) -> dict[str, Any]:
    now_ts = time.time() if now is None else now
    brains = surface_brains()
    candidates: list[tuple[float, Path, str]] = []
    by_cid: dict[str, tuple[Path, str]] = {}
    for surf in ("app", "ide", "cli"):
        bdir = brains.get(surf)
        if not bdir or not bdir.is_dir():
            continue
        try:
            for lp in bdir.glob("*/.system_generated/logs/transcript.jsonl"):
                try:
                    mt = lp.stat().st_mtime
                except Exception:
                    continue
                candidates.append((mt, lp, surf))
                cid = lp.parents[2].name
                if cid not in by_cid:
                    by_cid[cid] = (lp, surf)
        except Exception:
            continue

    candidates.sort(key=lambda x: x[0], reverse=True)
    if not candidates:
        return {
            "active": None,
            "workers": [],
            "spool": spool_status(),
        }

    _, active_tp, active_surf = candidates[0]
    act = parse_transcript_metrics(active_tp, active_surf)
    is_neon = "neon" in (act["model"] or "").lower()
    warn_tok = 100_000 if is_neon else 120_000
    rollover_tok = 120_000 if is_neon else 200_000
    cliff_tok = 200_000
    last_ctx = int(act["last_ctx"] or 0)
    if last_ctx >= cliff_tok:
        ctx_zone = "cliff"
    elif last_ctx >= 120_000:
        ctx_zone = "red"
    elif last_ctx >= 90_000:
        ctx_zone = "amber"
    else:
        ctx_zone = "green"

    idle_sec = max(0, int(now_ts - float(act["last_turn_epoch"] or now_ts)))
    ttl_state, ttl_ret = _ttl_classification(idle_sec)

    active_payload = {
        "conv_id": act["conv_id"],
        "conv_short": act["conv_id"][:12],
        "surface": act["surface"],
        "model": act["model"] or "unknown",
        "is_neon": is_neon,
        "turns": act["turns"],
        "last_ctx": last_ctx,
        "warn_tokens": warn_tok,
        "rollover_tokens": rollover_tok,
        "cliff_tokens": cliff_tok,
        "ctx_pct": round(min(100.0, 100.0 * last_ctx / max(rollover_tok, 1)), 1),
        "ctx_pct_200k": round(min(100.0, 100.0 * last_ctx / 200_000.0), 1),
        "ctx_zone": ctx_zone,
        "last_hit_pct": act["last_hit_pct"],
        "avg_hit_pct": act["avg_hit_pct"],
        "last_uncached_delta": act["last_uncached_delta"],
        "tot_uncached": act["tot_uncached"],
        "tot_cached": act["tot_cached"],
        "tot_output": act["tot_output"],
        "flips": act["flips"],
        "compactions": act["compactions"],
        "last_turn_epoch": act["last_turn_epoch"],
        "idle_sec": idle_sec,
        "ttl_state": ttl_state,
        "ttl_est_retention_pct": ttl_ret,
        "breakdown": act["breakdown"],
    }

    workers: list[dict[str, Any]] = []
    seen_worker_cids: set[str] = set()

    def _add_worker_row(name: str, role: str, mdata: dict[str, Any]) -> None:
        cid = mdata["conv_id"]
        if cid in seen_worker_cids:
            return
        seen_worker_cids.add(cid)
        w_idle = max(0, int(now_ts - float(mdata["last_turn_epoch"] or now_ts)))
        w_status = "active" if w_idle <= 60 else ("idle" if w_idle <= 600 else "cold")
        workers.append(
            {
                "name": name[:24],
                "role": role,
                "conv_id": cid[:12],
                "surface": mdata["surface"],
                "model": mdata["model"] or "?",
                "turns": mdata["turns"],
                "last_ctx": mdata["last_ctx"],
                "hit_pct": mdata["avg_hit_pct"],
                "tot_uncached": mdata["tot_uncached"],
                "flips": mdata["flips"],
                "idle_sec": w_idle,
                "status": w_status,
            }
        )

    _add_worker_row(f"active ({act['surface']})", "active", act)
    for wname, wcid in act.get("swarm_worker_refs") or []:
        pair = by_cid.get(wcid)
        if pair:
            _add_worker_row(wname, "swarm_worker", parse_transcript_metrics(pair[0], pair[1]))

    for mt, lp, surf in candidates[1:12]:
        if len(workers) >= 8:
            break
        if (now_ts - mt) > 1800 and len(workers) >= 4:
            break
        mdata = parse_transcript_metrics(lp, surf)
        _add_worker_row(f"{surf}:{mdata['conv_id'][:8]}", "recent", mdata)

    return {
        "active": active_payload,
        "workers": workers,
        "spool": spool_status(),
    }


def _resolve_bin(env_key: str, name: str) -> str | None:
    explicit = os.environ.get(env_key, "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    repo_bin = Path(__file__).resolve().parents[3] / "bin" / name
    if repo_bin.is_file():
        return str(repo_bin)
    local_bin = Path.home() / ".local" / "bin" / name
    if local_bin.is_file():
        return str(local_bin)
    return shutil.which(name)


def action_handoff(body: dict[str, Any], handoff_cache_dir: Path) -> tuple[int, dict[str, Any]]:
    handoff_bin = _resolve_bin("WM_HANDOFF_BIN", "wm-handoff")
    if not handoff_bin:
        return 500, {"ok": False, "error": "wm-handoff binary not found"}

    conv_req = str(body.get("conv_id") or "").strip()
    cwd_req = str(body.get("cwd") or "").strip()
    brains = surface_brains()
    target_tp: Path | None = None
    target_surf = "app"
    if conv_req:
        for surf in ("app", "ide", "cli"):
            bdir = brains.get(surf)
            if not bdir or not bdir.is_dir():
                continue
            cand = bdir / conv_req / ".system_generated" / "logs" / "transcript.jsonl"
            if cand.is_file():
                target_tp, target_surf = cand, surf
                break
            matches = sorted(
                bdir.glob(f"{conv_req}*/.system_generated/logs/transcript.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                target_tp, target_surf = matches[0], surf
                break
    if target_tp is None:
        cands = []
        for surf in ("app", "ide", "cli"):
            bdir = brains.get(surf)
            if bdir and bdir.is_dir():
                for lp in bdir.glob("*/.system_generated/logs/transcript.jsonl"):
                    try:
                        cands.append((lp.stat().st_mtime, lp, surf))
                    except Exception:
                        continue
        cands.sort(key=lambda x: x[0], reverse=True)
        if not cands:
            return 404, {"ok": False, "error": "No Antigravity transcript found"}
        _, target_tp, target_surf = cands[0]

    conv_id = target_tp.parents[2].name if len(target_tp.parents) > 2 else target_tp.parent.name
    san = _CONV_SAN_RE.sub("", conv_id)[:20] or "default"
    handoff_cache_dir.mkdir(parents=True, exist_ok=True)
    crystal_out = handoff_cache_dir / f"latest_{san}.md"
    receipt_out = handoff_cache_dir / f"latest_{san}.receipt.json"

    cmd = [
        sys.executable,
        "-S",
        handoff_bin,
        "--transcript",
        str(target_tp),
        "--out",
        str(crystal_out),
        "--emit-receipt",
        str(receipt_out),
        "--spool",
        "--spool-source",
        "wm-sidecar",
    ]
    if cwd_req and Path(cwd_req).is_dir():
        cmd.extend(["--cwd", cwd_req])
    env = os.environ.copy()
    env["WM_HANDOFF_CACHE_DIR"] = str(handoff_cache_dir)
    env["WM_PENDING_CRYSTALS"] = str(pending_crystals_path())
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=6)
    except Exception as exc:
        return 500, {"ok": False, "error": f"wm-handoff failed: {exc}"}
    if proc.returncode != 0 or not receipt_out.is_file():
        return 500, {"ok": False, "error": (proc.stderr or proc.stdout or "wm-handoff error").strip()[:300]}

    rec = json.loads(receipt_out.read_text(encoding="utf-8"))
    sha = str(rec.get("crystal_sha256") or "")
    model = str(rec.get("model") or "gemini-4-neon-xhigh")
    ctx_k = round(int(rec.get("last_ctx") or 0) / 1000.0, 1)
    mdata = parse_transcript_metrics(target_tp, target_surf)
    goal = (mdata.get("latest_goal") or "").strip()[:140]
    goal_line = f"\nContinue: {goal}" if goal else ""
    pointer_prompt = (
        f"Resume from {crystal_out} (sha256:{sha} | prior {conv_id[:8]} {model} {ctx_k}k ctx).\n"
        f"Stay pinned single-model (<120k ctx), read the crystal first, and verify state before editing.{goal_line}"
    )
    if len(pointer_prompt.encode("utf-8")) > 580:
        pointer_prompt = pointer_prompt[:560].rstrip() + "..."

    return 200, {
        "ok": True,
        "conv_id": conv_id[:16],
        "surface": target_surf,
        "model": model,
        "crystal_sha256": sha,
        "crystal_bytes": int(rec.get("crystal_bytes") or 0),
        "crystal_path": redact_home(str(crystal_out)),
        "receipt_path": redact_home(str(receipt_out)),
        "spooled": True,
        "spool": spool_status(),
        "pointer_prompt": pointer_prompt,
        "pointer_est_tokens": max(1, len(pointer_prompt) // 4),
    }


def action_audit_turn0(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    cfg_dir = Path(os.environ.get("WM_AGY_CONFIG_DIR") or str(Path.home() / ".gemini" / "config"))
    enabled_plugins: list[str] = []
    disabled_count = 0
    cfg_path = cfg_dir / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for pname, pmeta in sorted((cfg.get("plugins") or {}).items()):
                if isinstance(pmeta, dict) and pmeta.get("enabled") is True:
                    enabled_plugins.append(pname)
                else:
                    disabled_count += 1
        except Exception:
            pass

    eager_tools: list[str] = []
    readonly_eager_conflicts: list[str] = []
    mcp_path = cfg_dir / "mcp_config.json"
    if mcp_path.is_file():
        try:
            mcfg = json.loads(mcp_path.read_text(encoding="utf-8"))
            for sname, sdata in sorted((mcfg.get("mcpServers") or {}).items()):
                if not isinstance(sdata, dict) or sdata.get("disabled"):
                    continue
                args_list = [str(a) for a in (sdata.get("args") or [])]
                is_ro = "--readonly" in args_list
                for tname, tcfg in sorted((sdata.get("tools") or {}).items()):
                    if isinstance(tcfg, dict) and tcfg.get("eager") is True:
                        eager_tools.append(f"{sname}/{tname}")
                        if is_ro and tname in ("session_checkpoint", "memory_remember", "session_record"):
                            readonly_eager_conflicts.append(f"{sname}/{tname}")
        except Exception:
            pass

    sim_data: dict[str, Any] = {}
    sim_bin = _resolve_bin("WM_SIM_BIN", "wm-sim")
    repo_dir = str(body.get("repo_dir") or "").strip() or os.getcwd()
    if sim_bin:
        try:
            env = os.environ.copy()
            env["WM_AGY_CONFIG_DIR"] = str(cfg_dir)
            r = subprocess.run(
                [sys.executable, "-S", sim_bin, "audit", "--scope-plugins", repo_dir, "--json"],
                capture_output=True,
                text=True,
                env=env,
                timeout=4,
            )
            if r.returncode == 0 and r.stdout.strip().startswith("{"):
                sim_data = json.loads(r.stdout)
        except Exception:
            sim_data = {}

    recs: list[str] = []
    for c in readonly_eager_conflicts:
        recs.append(f"Demote readonly-refused eager tool: {c}")
    for p in sim_data.get("unmatched_enabled_plugins") or []:
        recs.append(f"Disable unused global domain plugin: {p}")
    if not recs:
        recs.append("Turn-0 plugin & eager MCP surface is lean.")

    return 200, {
        "ok": True,
        "enabled_plugins": enabled_plugins,
        "disabled_plugins_count": disabled_count,
        "eager_mcp_tools": eager_tools,
        "eager_tool_count": len(eager_tools),
        "readonly_eager_conflicts": readonly_eager_conflicts,
        "unmatched_enabled_plugins": sim_data.get("unmatched_enabled_plugins") or [],
        "savings_est_tokens": int(sim_data.get("savings_est_tokens") or 0),
        "recommendations": recs,
    }


def action_flush_spool(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    handoff_bin = _resolve_bin("WM_HANDOFF_BIN", "wm-handoff")
    if not handoff_bin:
        return 500, {"ok": False, "error": "wm-handoff binary not found"}
    cmd = [sys.executable, "-S", handoff_bin, "--flush-wm3", "--json"]
    if body.get("dry_run"):
        cmd.append("--dry-run")
    env = os.environ.copy()
    env["WM_PENDING_CRYSTALS"] = str(pending_crystals_path())
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
    except Exception as exc:
        return 500, {"ok": False, "error": f"flush failed: {exc}"}
    if proc.returncode != 0:
        return 500, {"ok": False, "error": (proc.stderr or proc.stdout or "flush error").strip()[:300]}
    try:
        data = json.loads(proc.stdout)
    except Exception:
        data = {"ok": True, "raw": proc.stdout.strip()[:400]}
    data["spool"] = spool_status()
    return 200, data


class _Wm3ReadOnlyClient:
    """Lazy persistent `wm3 serve --readonly --no-sweep --no-noise` JSON-RPC worker for <20ms warm recall."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._key: tuple[str, str] | None = None
        self._req_id = 0

    def close(self) -> None:
        with self._lock:
            self._kill_locked()

    def _kill_locked(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
            self._key = None

    def _ensure_started_locked(self, wm_bin: str, store: str) -> bool:
        if self._proc is not None and self._proc.poll() is None and self._key == (wm_bin, store):
            return True
        self._kill_locked()
        try:
            proc = subprocess.Popen(
                [wm_bin, "--store", store, "serve", "--readonly", "--no-sweep", "--no-noise"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            self._req_id += 1
            init_req = {
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "wm-sidecar", "version": "4.7.0"}},
            }
            assert proc.stdin and proc.stdout
            proc.stdin.write(json.dumps(init_req) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                proc.terminate()
                return False
            resp = json.loads(line)
            if "error" in resp:
                proc.terminate()
                return False
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            proc.stdin.flush()
            self._proc = proc
            self._key = (wm_bin, store)
            return True
        except Exception:
            self._kill_locked()
            return False

    def recall(self, wm_bin: str, store: str, query: str, limit: int) -> list[dict[str, Any]] | None:
        with self._lock:
            if not self._ensure_started_locked(wm_bin, store):
                return None
            assert self._proc and self._proc.stdin and self._proc.stdout
            self._req_id += 1
            rid = self._req_id
            req = {
                "jsonrpc": "2.0",
                "id": rid,
                "method": "tools/call",
                "params": {"name": "memory_recall", "arguments": {"query": query, "limit": limit, "max_content_bytes": 480}},
            }
            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
                for _ in range(10):
                    line = self._proc.stdout.readline()
                    if not line:
                        self._kill_locked()
                        return None
                    msg = json.loads(line)
                    if msg.get("id") != rid:
                        continue
                    if "error" in msg:
                        return None
                    content = (msg.get("result") or {}).get("content") or []
                    text_blob = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
                    parsed = json.loads(text_blob) if text_blob.strip().startswith("{") else {}
                    items = parsed.get("results") or parsed.get("memories") or []
                    out = []
                    for it in items[:limit]:
                        if not isinstance(it, dict):
                            continue
                        snip = str(it.get("content") or it.get("preview") or it.get("text") or "")[:360]
                        out.append(
                            {
                                "id": str(it.get("id") or it.get("record_id") or "?"),
                                "score": round(float(it.get("score") or 0.0), 4),
                                "source": str(it.get("source") or "gen3"),
                                "snippet": redact_home(" ".join(snip.split())),
                            }
                        )
                    return out
            except Exception:
                self._kill_locked()
                return None
        return None


_WM3_CLIENT = _Wm3ReadOnlyClient()


def shutdown_clients() -> None:
    _WM3_CLIENT.close()


def _local_crystal_search(query: str, handoff_cache_dir: Path, limit: int = 3) -> list[dict[str, Any]]:
    terms = [t.lower() for t in re.findall(r"[a-zA-Z0-9_-]{2,}", query)]
    if not terms:
        return []
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    sp = pending_crystals_path()
    if sp.is_file():
        try:
            for ln in reversed(sp.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]):
                if not ln.startswith("{"):
                    continue
                obj = json.loads(ln)
                sha = str(obj.get("crystal_sha256") or "")
                blob = f"{obj.get('summary', '')} {obj.get('content', '')}"
                low = blob.lower()
                matched = sum(1 for t in terms if t in low)
                if matched > 0 and sha not in seen:
                    seen.add(sha)
                    hits.append(
                        {
                            "id": f"spool:{sha[:10]}",
                            "score": round(matched / len(terms), 3),
                            "source": f"spool:{obj.get('source', 'ide')}",
                            "snippet": redact_home(" ".join(blob.split())[:300]),
                        }
                    )
                    if len(hits) >= limit:
                        return hits
        except Exception:
            pass
    if handoff_cache_dir.is_dir():
        for md in sorted(handoff_cache_dir.glob("latest_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:12]:
            try:
                txt = md.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            low = txt.lower()
            matched = sum(1 for t in terms if t in low)
            if matched > 0:
                hits.append(
                    {
                        "id": f"crystal:{md.stem}",
                        "score": round(matched / len(terms), 3),
                        "source": "crystal_cache",
                        "snippet": redact_home(" ".join(txt.split())[:300]),
                    }
                )
                if len(hits) >= limit:
                    break
    return hits


def _parse_wm3_cli_recall(stdout: str, limit: int) -> list[dict[str, Any]]:
    txt = stdout.strip()
    if txt.startswith("{") or txt.startswith("["):
        try:
            obj = json.loads(txt)
            arr = obj if isinstance(obj, list) else (obj.get("results") or obj.get("memories") or [])
            return [
                {
                    "id": str(it.get("id") or "?"),
                    "score": round(float(it.get("score") or 0.0), 4),
                    "source": str(it.get("source") or "gen3"),
                    "snippet": redact_home(" ".join(str(it.get("content") or it.get("snippet") or "").split())[:360]),
                }
                for it in arr[:limit]
                if isinstance(it, dict)
            ]
        except Exception:
            pass
    results: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in stdout.splitlines():
        m = _RECALL_HDR_RE.match(line.strip())
        if m:
            if cur:
                results.append(cur)
            cur = {"id": f"#{m.group(2)}", "score": float(m.group(3)), "source": "gen3", "snippet": ""}
            continue
        if cur is not None:
            s = line.strip()
            if s.startswith("Source:"):
                cur["source"] = s.split(":", 1)[1].strip()
            elif s.startswith("Content"):
                _, _, body = s.partition(":")
                cur["snippet"] = redact_home(body.strip()[:360])
            elif s and not cur["snippet"]:
                cur["snippet"] = redact_home(s[:360])
    if cur:
        results.append(cur)
    return results[:limit]


def action_recall(body: dict[str, Any], handoff_cache_dir: Path) -> tuple[int, dict[str, Any]]:
    query = str(body.get("query") or "").strip()
    if not query:
        return 400, {"ok": False, "error": "Missing non-empty 'query'"}
    try:
        limit = max(1, min(20, int(body.get("limit") or 6)))
    except Exception:
        limit = 6

    t0 = time.perf_counter()
    local_hits = _local_crystal_search(query, handoff_cache_dir, limit=min(3, limit))
    wm_bin = _resolve_bin("WM3_BIN", "wm3") or _resolve_bin("WM3_BIN", "wm-gen3")
    store = os.environ.get("WM3_STORE") or str(Path.home() / ".local" / "share" / "whitemagic" / "gen3")
    mode = "local_only"
    gen3_hits: list[dict[str, Any]] = []
    warn = ""

    if wm_bin and Path(store).exists():
        recall_mode = os.environ.get("WM3_RECALL_MODE", "auto").strip().lower()
        if recall_mode != "cli":
            res = _WM3_CLIENT.recall(wm_bin, store, query, limit)
            if res is not None:
                gen3_hits = res
                mode = "wm3_readonly_mcp"
        if mode == "local_only":
            timeout_s = float(os.environ.get("WM_RECALL_TIMEOUT_SEC") or "60")
            try:
                proc = subprocess.run(
                    [wm_bin, "--store", store, "recall", "--limit", str(limit), "--inspect-promise", "strict-zero-write", query],
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
                if proc.returncode == 0:
                    gen3_hits = _parse_wm3_cli_recall(proc.stdout, limit)
                    mode = "wm3_readonly_cli"
                else:
                    warn = (proc.stderr or proc.stdout or "wm3 recall failed").strip()[:200]
            except Exception as exc:
                warn = str(exc)[:200]

    combined = (gen3_hits + local_hits)[:limit]
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    payload: dict[str, Any] = {
        "ok": True,
        "query": query,
        "mode": mode,
        "zero_llm_tokens": True,
        "elapsed_ms": elapsed_ms,
        "results": combined,
    }
    if warn:
        payload["warning"] = warn
    return 200, payload
