#!/usr/bin/env python3
"""
WhiteMagic AGY Telemetry & Live Context Cockpit Resident Sidecar (v4.7.0).
Stdlib-only loopback ThreadingHTTPServer providing:
  - GET /healthz
  - GET / (dark-mode Cursor-style Aux-Pane cockpit, Cache-Control: no-store, DOM textContent-safe)
  - GET /api/status (telemetry + live context/TTL/breakdown/swarm workers + spool + grimoire + wm_fast)
  - GET /api/live (fast 2s-pollable live active IDE/CLI/swarm cockpit payload)
  - GET /api/crystals
  - GET /api/receipts
  - GET /api/webmcp & /.well-known/webmcp.json (Declarative/Imperative WebMCP manifest)
  - POST /api/receipt (validates wm-receipt/1.2 | wm-confinement-receipt/1.1 | mandala-cpr-pointer/0.5)
  - POST /api/actions/handoff (crystallize + spool + return <=150-tok fresh-chat handoff pointer)
  - POST /api/actions/audit-turn0 (audit enabled plugins + eager MCP tools + Turn-0 bloat)
  - POST /api/actions/recall (zero-LLM-token read-only WhiteMagic Gen3 + crystal spool recall)
  - POST /api/actions/flush-spool (single-writer-safe wm-handoff --flush-wm3)
"""
from __future__ import annotations

import glob
import hashlib
import hmac
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import cockpit  # noqa: E402

VERSION = "4.7.0"
MAX_RECEIPT_BYTES = 16384

WEBMCP_MANIFEST = {
    "schema": "webmcp/1.0",
    "server": "wm-sidecar",
    "version": VERSION,
    "tools": [
        {
            "name": "wm_sidecar_status",
            "description": "Refresh live telemetry, context meter, Neon TTL warmth, squeeze savings, and ready handoff crystals.",
            "method": "GET",
            "path": "/api/status",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "wm_sidecar_live",
            "description": "Read active IDE/CLI session context meter, Neon TTL warmth timer, uncached breakdown, and swarm workers.",
            "method": "GET",
            "path": "/api/live",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "wm_sidecar_crystals",
            "description": "List cached handoff crystals from wm-handoff.",
            "method": "GET",
            "path": "/api/crystals",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "wm_sidecar_receipts",
            "description": "List recent verified CPR bridge ledger receipts.",
            "method": "GET",
            "path": "/api/receipts",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "wm_sidecar_verify_receipt",
            "description": "Verify and optionally record a CPR wm-receipt/1.2 JSON payload.",
            "method": "POST",
            "path": "/api/receipt",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "receipt": {"type": "object", "description": "CPR receipt JSON object"},
                    "dry_run": {"type": "boolean", "description": "Verify without appending ledger"},
                },
                "required": ["receipt"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "wm_sidecar_handoff",
            "description": "Crystallize the active session into a CliffCompaction handoff, spool to pending_crystals.jsonl, and return a <=150-token pointer prompt.",
            "method": "POST",
            "path": "/api/actions/handoff",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conv_id": {"type": "string", "description": "Optional conversation ID prefix"},
                    "cwd": {"type": "string", "description": "Optional working directory for git state"},
                },
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "wm_sidecar_audit_turn0",
            "description": "Audit Turn-0 bloat across enabled AGY plugins and eager MCP tool declarations.",
            "method": "POST",
            "path": "/api/actions/audit-turn0",
            "inputSchema": {
                "type": "object",
                "properties": {"repo_dir": {"type": "string"}},
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "wm_sidecar_recall",
            "description": "Zero-LLM-token read-only WhiteMagic Gen3 and local crystal spool memory recall.",
            "method": "POST",
            "path": "/api/actions/recall",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (1-20)"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "idempotentHint": True},
        },
        {
            "name": "wm_sidecar_flush_spool",
            "description": "Flush pending_crystals.jsonl into WhiteMagic Gen3 LMDB safely.",
            "method": "POST",
            "path": "/api/actions/flush-spool",
            "inputSchema": {
                "type": "object",
                "properties": {"dry_run": {"type": "boolean"}},
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
    ],
}


def _handoff_cache_dir() -> Path:
    return Path(os.environ.get("WM_HANDOFF_CACHE_DIR") or str(Path.home() / ".cache" / "wm-handoff"))


def _telemetry_path() -> Path:
    return Path(
        os.environ.get(
            "WM_AGY_TELEMETRY",
            str(Path.home() / ".local" / "share" / "whitemagic" / "agy_telemetry.jsonl"),
        )
    )


def _squeeze_stats_path() -> Path:
    return Path(
        os.environ.get(
            "WM_SQUEEZE_STATS",
            str(Path.home() / ".local" / "share" / "whitemagic" / "wm_squeeze_stats.jsonl"),
        )
    )


def _bridge_ledger_path() -> Path:
    return Path(os.environ.get("WM_CPR_BRIDGE_LEDGER") or str(_handoff_cache_dir() / "cpr_bridge.jsonl"))


def _discovery_path() -> Path:
    return _handoff_cache_dir() / "sidecar.json"


def _grimoire_profile_path() -> Path:
    return Path(
        os.environ.get("WM_GRIMOIRE_PROFILE")
        or str(Path.home() / ".local" / "share" / "whitemagic" / "grimoire_profile.json")
    )


def _wm_fast_stats_path() -> Path:
    return Path(
        os.environ.get("WM_FAST_STATS")
        or str(Path.home() / ".local" / "share" / "whitemagic" / "wm_fast_stats.jsonl")
    )


def _redact_home(val: str) -> str:
    return cockpit.redact_home(val)


def _load_grimoire_summary() -> dict:
    p = _grimoire_profile_path()
    if not p.is_file():
        return {"present": False}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        conf = obj.get("confinement") or {}
        mt = obj.get("model_tuning") or {}
        hp = obj.get("host_profiles") or {}
        tel = obj.get("telemetry") or {}
        host_models = {}
        if isinstance(hp, dict):
            for k, v in hp.items():
                if isinstance(v, dict) and v.get("resolved_family"):
                    host_models[str(k)] = str(v["resolved_family"])
        return {
            "present": True,
            "schema": str(obj.get("schema") or "wm-grimoire-profile/1.0"),
            "hardware_tier": str(obj.get("hardware_tier") or "unknown"),
            "confinement_profile": str(conf.get("runner_profile") or "unknown"),
            "mandala_guard_mode": str(conf.get("mandala_guard_mode") or "advisory"),
            "model_family": str(mt.get("resolved_family") or "unknown"),
            "context_warn_tokens": int(mt.get("context_warn_tokens") or 120000),
            "context_cliff_tokens": int(mt.get("context_cliff_tokens") or 200000),
            "host_models": host_models,
            "recommendations_count": len(tel.get("recommendations") or []),
            "profile_path": _redact_home(str(p)),
        }
    except Exception:
        return {"present": False}


def _read_jsonl_tail(path: Path, limit: int) -> list[dict]:
    if not path.is_file():
        return []
    try:
        sz = path.stat().st_size
        with path.open("rb") as fh:
            if sz > 131072:
                fh.seek(sz - 131072)
            raw = fh.read().decode("utf-8", errors="replace")
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out[-limit:]
    except Exception:
        return []


def _list_crystals(limit: int = 20) -> list[dict]:
    cdir = _handoff_cache_dir()
    if not cdir.is_dir():
        return []
    items = []
    for rp in sorted(glob.glob(str(cdir / "latest_*.receipt.json")), key=os.path.getmtime, reverse=True)[:limit]:
        p = Path(rp)
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        conv = str(rec.get("conv_id") or p.stem.removeprefix("latest_").removesuffix(".receipt"))
        cpath = str(rec.get("crystal_path") or p.with_name(p.name.removesuffix(".receipt.json") + ".md"))
        items.append(
            {
                "conv_id": conv[:16],
                "surface": str(rec.get("surface") or ""),
                "model": str(rec.get("model") or ""),
                "last_ctx": int(rec.get("last_ctx") or 0),
                "last_hit_pct": rec.get("last_hit_pct"),
                "model_flips": int(rec.get("model_flips") or 0),
                "crystal_sha256": str(rec.get("crystal_sha256") or ""),
                "crystal_bytes": int(rec.get("crystal_bytes") or 0),
                "crystal_path": _redact_home(cpath),
                "receipt_path": _redact_home(str(p)),
                "created_at": str(rec.get("created_at") or ""),
                "reset_cmd": f"wm-reset --conv {conv[:12]}",
            }
        )
    return items


def _build_status_payload() -> dict:
    telem_rows = _read_jsonl_tail(_telemetry_path(), 30)
    stops = [r for r in telem_rows if r.get("event", "stop") == "stop" and ("last_hit_pct" in r or "last_ctx" in r)]
    avg_hit = (
        round(sum(float(r.get("last_hit_pct") or 0.0) for r in stops) / max(len(stops), 1), 1)
        if stops
        else None
    )
    total_flips = sum(int(r.get("model_flips") or 0) for r in stops)
    spills = sum(int(r.get("spills") or 0) for r in stops)

    sq_rows = _read_jsonl_tail(_squeeze_stats_path(), 200)
    sq_raw = sum(int(r.get("raw") or 0) for r in sq_rows)
    sq_saved = sum(int(r.get("saved") or 0) for r in sq_rows)

    crystals = _list_crystals(15)
    receipts = _read_jsonl_tail(_bridge_ledger_path(), 25)
    for r in receipts:
        if isinstance(r.get("crystal_path"), str):
            r["crystal_path"] = _redact_home(r["crystal_path"])
        if isinstance(r.get("receipt_path"), str):
            r["receipt_path"] = _redact_home(r["receipt_path"])

    recent_stops = []
    for r in reversed(stops[-10:]):
        recent_stops.append(
            {
                "ts": str(r.get("ts") or "")[:19],
                "conv_id": str(r.get("conv_id") or "")[:12],
                "model": str(r.get("model") or "?"),
                "last_ctx": int(r.get("last_ctx") or 0),
                "last_hit_pct": r.get("last_hit_pct"),
                "model_flips": int(r.get("model_flips") or 0),
                "spills": int(r.get("spills") or 0),
            }
        )

    hosts_detected = [
        h for h, p in (
            ("agy", Path.home() / ".gemini"),
            ("codex", Path.home() / ".codex"),
            ("cursor", Path.home() / ".cursor"),
        )
        if p.is_dir()
    ]

    fast_rows = _read_jsonl_tail(_wm_fast_stats_path(), 200)
    fast_calls = len(fast_rows)
    fast_errs = sum(1 for r in fast_rows if r.get("err"))
    fast_out = sum(int(r.get("out") or 0) for r in fast_rows)
    fast_ms = sum(int(r.get("wall_ms") or 0) for r in fast_rows)

    live_data = cockpit.collect_live_cockpit()

    return {
        "ok": True,
        "version": VERSION,
        "pid": os.getpid(),
        "hosts_detected": hosts_detected,
        "grimoire": _load_grimoire_summary(),
        "live": live_data,
        "spool": live_data.get("spool") or cockpit.spool_status(),
        "telemetry": {
            "sample_stops": len(stops),
            "avg_hit_pct": avg_hit,
            "recent_flips": total_flips,
            "recent_spills": spills,
            "recent_stops": recent_stops,
        },
        "wm_squeeze": {
            "sample_cmds": len(sq_rows),
            "raw_bytes": sq_raw,
            "saved_bytes": sq_saved,
            "est_tokens_saved": sq_saved // 4,
        },
        "wm_fast": {
            "calls": fast_calls,
            "errors": fast_errs,
            "total_bytes": fast_out,
            "avg_bytes": (fast_out // fast_calls) if fast_calls else 0,
            "avg_ms": (fast_ms // fast_calls) if fast_calls else 0,
        },
        "crystals": crystals,
        "cpr_ledger_tail": list(reversed(receipts)),
    }


def _verify_posted_receipt(obj: dict) -> tuple[bool, str, str]:
    if not isinstance(obj, dict):
        return False, "", "JSON body must be an object"
    schema = str(obj.get("schema") or "")
    if schema not in ("wm-receipt/1.2", "wm-confinement-receipt/1.1", "mandala-cpr-pointer/0.5"):
        return False, "", f"Unsupported receipt schema: {schema!r}"
    if schema == "mandala-cpr-pointer/0.5":
        digest = str(obj.get("bundle_sha256") or obj.get("stdout_hash") or "")
        if not digest:
            return False, "", "Missing bundle_sha256 or stdout_hash"
        hmac_raw = str(obj.get("hmac_sha256") or "")
        signing_key = (os.environ.get("WM_CPR_HMAC_KEY") or os.environ.get("WM_CONFINEMENT_SIGNING_KEY") or "").strip()
        if hmac_raw and signing_key:
            core = {k: v for k, v in obj.items() if k != "hmac_sha256"}
            canon = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            expected = hmac.new(signing_key.encode("utf-8"), canon, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(hmac_raw.removeprefix("hmac-sha256:"), expected):
                return False, digest, "Invalid hmac_sha256 signature"
        return True, digest, ""

    digest = str(obj.get("crystal_sha256") or obj.get("receipt_sha256") or "")
    if not digest:
        return False, "", "Missing crystal_sha256 or receipt_sha256"

    sig = obj.get("signature")
    signing_key = os.environ.get("WM_CONFINEMENT_SIGNING_KEY", "").strip()
    if isinstance(sig, dict) and sig.get("alg") == "hmac-sha256" and signing_key:
        core = {k: v for k, v in obj.items() if k != "signature"}
        canon = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(signing_key.encode("utf-8"), canon, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(sig.get("sig") or ""), expected):
            return False, digest, "Invalid hmac-sha256 signature"
    return True, digest, ""


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhiteMagic AGY Telemetry</title>
<style>
  :root { color-scheme: dark; --bg: #0e1116; --card: #161b22; --bd: #2a313c; --fg: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --ok: #3fb950; --warn: #d29922; --bad: #f85149; --purple: #bc8cff; }
  body { margin: 0; padding: 12px; font: 12.5px/1.42 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); }
  h1 { margin: 0 0 10px; font-size: 14.5px; display: flex; justify-content: space-between; align-items: center; gap: 6px; }
  .pill { font-size: 10.5px; padding: 2px 7px; border-radius: 999px; border: 1px solid var(--bd); color: var(--ok); font-weight: 600; }
  .pill.amber { color: var(--warn); border-color: var(--warn); }
  .pill.red { color: var(--bad); border-color: var(--bad); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(105px, 1fr)); gap: 7px; margin-bottom: 10px; }
  .card { background: var(--card); border: 1px solid var(--bd); border-radius: 8px; padding: 9px 10px; margin-bottom: 9px; }
  .kpi-lbl { font-size: 10.5px; color: var(--muted); }
  .kpi-val { font-size: 15px; font-weight: 600; margin-top: 2px; }
  .kpi-sub { font-size: 10.5px; color: var(--muted); margin-top: 1px; }
  h2 { margin: 0 0 7px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); display: flex; justify-content: space-between; align-items: center; }
  .meter-track { width: 100%; height: 8px; background: #0d1117; border: 1px solid var(--bd); border-radius: 999px; overflow: hidden; margin: 6px 0 4px; }
  .meter-fill { height: 100%; width: 0%; background: var(--ok); transition: width 0.25s ease, background 0.25s ease; }
  .meter-fill.amber { background: var(--warn); }
  .meter-fill.red, .meter-fill.cliff { background: var(--bad); }
  .stack-bar { display: flex; height: 7px; width: 100%; border-radius: 999px; overflow: hidden; background: #0d1117; border: 1px solid var(--bd); margin-bottom: 7px; }
  .seg-flips { background: var(--bad); }
  .seg-ttl { background: var(--warn); }
  .seg-warm { background: var(--ok); }
  .seg-cmp { background: var(--purple); }
  .seg-t0 { background: var(--accent); }
  table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
  th, td { text-align: left; padding: 4px 5px; border-top: 1px solid var(--bd); }
  th { color: var(--muted); font-weight: 600; border-top: 0; font-size: 10.5px; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  ul { list-style: none; padding: 0; margin: 0; }
  li { padding: 5px 0; border-top: 1px solid var(--bd); font-size: 11.5px; }
  li:first-child { border-top: 0; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10.5px; background: #0d1117; padding: 1px 4px; border-radius: 4px; }
  .row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
  button { background: #1f6feb; color: #fff; border: 0; border-radius: 6px; padding: 5px 9px; font-size: 11.5px; cursor: pointer; font-weight: 500; }
  button:hover { background: #388bfd; }
  button.sec { background: #21262d; color: var(--fg); border: 1px solid var(--bd); }
  button.sec:hover { background: #30363d; }
  input[type="text"], textarea { width: 100%; box-sizing: border-box; background: #0d1117; color: var(--fg); border: 1px solid var(--bd); border-radius: 6px; padding: 5px 7px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; margin-bottom: 6px; }
  label.chk { font-size: 11.5px; color: var(--muted); display: inline-flex; align-items: center; gap: 4px; }
  pre.out { margin: 5px 0 0; font-size: 11px; color: var(--muted); white-space: pre-wrap; word-break: break-word; max-height: 160px; overflow-y: auto; background: #0d1117; padding: 6px; border-radius: 6px; border: 1px solid var(--bd); display: none; }
  pre.out.show { display: block; }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 4px; background: var(--ok); }
  .dot.amber { background: var(--warn); }
  .dot.red { background: var(--bad); }
</style>
</head>
<body>
  <h1>
    <span>WhiteMagic AGY Telemetry</span>
    <span><span id="ttl-pill" class="pill">TTL -</span> <span id="ver-pill" class="pill">v4.7.0</span></span>
  </h1>

  <div class="card">
    <h2>
      <span>Live Context Meter (<span id="live-model">idle</span>)</span>
      <span id="live-zone-pill" class="pill">green</span>
    </h2>
    <div class="row" style="justify-content:space-between;margin-bottom:2px;">
      <strong id="live-ctx-main">0k / 120k tok</strong>
      <span id="live-meta" style="font-size:11px;color:var(--muted);">hit -% · +0k unc · 0 flips</span>
    </div>
    <div class="meter-track"><div id="live-meter" class="meter-fill"></div></div>
    <div class="row" style="justify-content:space-between;margin-bottom:0;font-size:11px;color:var(--muted);">
      <span id="live-conv">No active transcript</span>
      <span id="live-ttl-detail">Neon TTL: -</span>
    </div>
  </div>

  <div class="card">
    <h2><span>Quick Actions (Zero-LLM)</span><span id="spool-badge" style="color:var(--accent);">spool: 0 pending</span></h2>
    <div class="row">
      <button id="btn-handoff" type="button">Crystallize &amp; Copy Handoff (&lt;=150 tok)</button>
      <button id="btn-compact" class="sec" type="button">Copy /compact</button>
      <button id="btn-audit-t0" class="sec" type="button">Audit Turn-0 Bloat</button>
      <button id="btn-flush-spool" class="sec" type="button">Flush Spool</button>
    </div>
    <pre id="action-out" class="out"></pre>
  </div>

  <div class="card">
    <h2><span>WhiteMagic Memory Recall (Zero-LLM · Read-Only)</span><span id="recall-meta" style="color:var(--muted);">wm3 --readonly</span></h2>
    <form id="form-recall" class="row" style="margin-bottom:4px;" toolname="wm_sidecar_recall" tooldescription="Zero-LLM-token read-only WhiteMagic Gen3 and crystal spool memory recall.">
      <input id="recall-q" name="query" type="text" placeholder="Search Gen3 memories &amp; local crystal spool..." style="flex:1;margin-bottom:0;">
      <button id="btn-recall" type="submit">Recall</button>
    </form>
    <ul id="list-recall"></ul>
  </div>

  <div class="card">
    <h2><span>Uncached Burn Decomposition</span><span id="bd-total" style="color:var(--muted);">-</span></h2>
    <div id="bd-bar" class="stack-bar"></div>
    <table>
      <thead><tr><th>Bucket</th><th class="num">Turns</th><th class="num">Uncached</th><th class="num">Share</th><th class="num">Avg/Turn</th></tr></thead>
      <tbody id="tbl-breakdown"></tbody>
    </table>
  </div>

  <div class="card">
    <h2><span>Active Sessions &amp; Swarm Workers</span><span id="worker-count" style="color:var(--muted);">0</span></h2>
    <table>
      <thead><tr><th>Worker</th><th>Model</th><th class="num">Ctx</th><th class="num">Hit%</th><th class="num">Unc</th><th class="num">Idle</th></tr></thead>
      <tbody id="tbl-workers"></tbody>
    </table>
  </div>

  <div class="grid">
    <div class="card"><div class="kpi-lbl">Avg Cache Hit</div><div class="kpi-val" id="kpi-hit">-</div></div>
    <div class="card"><div class="kpi-lbl">Recent Flips</div><div class="kpi-val" id="kpi-flips">-</div></div>
    <div class="card"><div class="kpi-lbl">Squeeze Saved</div><div class="kpi-val" id="kpi-sq">-</div></div>
    <div class="card"><div class="kpi-lbl">Ready Crystals</div><div class="kpi-val" id="kpi-crys">-</div></div>
  </div>

  <div class="card"><h2>Adaptive Grimoire &amp; wm_fast</h2><div id="grimoire-line">Not detected</div></div>
  <div class="card"><h2>Ready Handoff Crystals (wm-reset)</h2><ul id="list-crystals"></ul></div>
  <div class="card"><h2>Recent CPR Ledger (wm-receipt/1.2)</h2><ul id="list-receipts"></ul></div>
  <div class="card"><h2>Recent Turn Telemetry</h2><ul id="list-stops"></ul></div>

  <div class="card">
    <h2>WebMCP Interactive Controls</h2>
    <div class="row">
      <form id="form-status" toolname="wm_sidecar_status" tooldescription="Refresh telemetry, cache warmth, squeeze savings, and ready handoff crystals." toolautosubmit>
        <button id="btn-refresh" type="submit">Refresh Telemetry</button>
      </form>
      <a href="/api/webmcp" style="font-size:11px;color:var(--accent);text-decoration:none;">/api/webmcp</a>
    </div>
    <form id="form-verify" method="post" action="/api/receipt" toolname="wm_sidecar_verify_receipt" tooldescription="Verify and optionally record a CPR wm-receipt/1.2 JSON payload.">
      <textarea id="receipt-input" name="receipt_json" rows="2" placeholder='{"schema":"wm-receipt/1.2",...}'></textarea>
      <div class="row">
        <label class="chk"><input id="verify-dry" type="checkbox" name="dry_run"> Dry-run verify only</label>
        <button id="btn-verify" type="submit">Verify Receipt</button>
      </div>
    </form>
    <pre id="verify-out" class="out"></pre>
  </div>
<script>
let lastTurnEpoch = null;

function fmtTok(n) {
  n = Number(n || 0);
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(Math.round(n));
}
function fmtAge(sec) {
  sec = Math.max(0, Math.floor(Number(sec || 0)));
  if (sec < 60) return sec + "s";
  if (sec < 3600) return Math.floor(sec / 60) + "m" + (sec % 60) + "s";
  return Math.floor(sec / 3600) + "h" + Math.floor((sec % 3600) / 60) + "m";
}
function setList(el, items, emptyText, renderFn) {
  while (el.firstChild) el.removeChild(el.firstChild);
  if (!items || items.length === 0) {
    const li = document.createElement("li");
    li.textContent = emptyText;
    el.appendChild(li);
    return;
  }
  for (const item of items) el.appendChild(renderFn(item));
}
function showActionOut(text) {
  const el = document.getElementById("action-out");
  el.textContent = text;
  el.classList.add("show");
}
async function copyText(txt) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(txt);
      return true;
    }
  } catch (e) {}
  return false;
}
function tickTtl() {
  if (!lastTurnEpoch) return;
  const idle = Math.max(0, Math.floor(Date.now() / 1000 - lastTurnEpoch));
  const pill = document.getElementById("ttl-pill");
  const det = document.getElementById("live-ttl-detail");
  let state = "warm", ret = "97.8%", cls = "pill";
  if (idle <= 15) { state = "warm"; ret = "97.8%"; cls = "pill"; }
  else if (idle <= 30) { state = "warm"; ret = "96.1%"; cls = "pill"; }
  else if (idle <= 60) { state = "cooling"; ret = "90.2%"; cls = "pill amber"; }
  else if (idle <= 180) { state = "cold"; ret = "84.7%"; cls = "pill red"; }
  else { state = "cold"; ret = "51.8%"; cls = "pill red"; }
  pill.className = cls;
  pill.textContent = "TTL " + fmtAge(idle) + " (" + state + ")";
  det.textContent = "Neon TTL: " + fmtAge(idle) + " · " + state + " (~" + ret + " keep)";
}
function renderBreakdown(active) {
  const bar = document.getElementById("bd-bar");
  const tbody = document.getElementById("tbl-breakdown");
  while (bar.firstChild) bar.removeChild(bar.firstChild);
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  if (!active || !active.breakdown) {
    document.getElementById("bd-total").textContent = "No turns yet";
    return;
  }
  const bd = active.breakdown;
  document.getElementById("bd-total").textContent = fmtTok(active.tot_uncached) + " unc / " + active.turns + " turns";
  const rows = [
    ["flips", "Flips", "seg-flips"],
    ["ttl_expiries", "TTL Expiries", "seg-ttl"],
    ["warm_work", "Warm Work", "seg-warm"],
    ["compactions_partial", "Partial/Compactions", "seg-cmp"],
    ["turn_0", "Turn-0", "seg-t0"],
  ];
  for (const [key, lbl, segCls] of rows) {
    const b = bd[key] || { turns: 0, tokens: 0, pct: 0, avg_per_turn: 0 };
    if (b.pct > 0) {
      const seg = document.createElement("div");
      seg.className = segCls;
      seg.style.width = b.pct + "%";
      seg.title = lbl + ": " + fmtTok(b.tokens) + " (" + b.pct + "%)";
      bar.appendChild(seg);
    }
    const tr = document.createElement("tr");
    for (const [val, isNum] of [[lbl, false], [String(b.turns), true], [fmtTok(b.tokens), true], [b.pct + "%", true], [fmtTok(b.avg_per_turn), true]]) {
      const td = document.createElement("td");
      if (isNum) td.className = "num";
      td.textContent = val;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}
function renderWorkers(workers) {
  const tbody = document.getElementById("tbl-workers");
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  document.getElementById("worker-count").textContent = String((workers || []).length);
  if (!workers || workers.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = "No active sessions or swarm workers detected.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (const w of workers) {
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    const dot = document.createElement("span");
    dot.className = "dot" + (w.status === "idle" ? " amber" : (w.status === "cold" ? " red" : ""));
    nameTd.appendChild(dot);
    nameTd.appendChild(document.createTextNode(w.name || w.conv_id || "?"));
    tr.appendChild(nameTd);
    const shortM = String(w.model || "?").replace("gemini-4-", "").replace("-xhigh", " xh");
    for (const [val, isNum] of [
      [shortM, false],
      [fmtTok(w.last_ctx), true],
      [w.hit_pct !== null && w.hit_pct !== undefined ? w.hit_pct + "%" : "-", true],
      [fmtTok(w.tot_uncached), true],
      [fmtAge(w.idle_sec), true],
    ]) {
      const td = document.createElement("td");
      if (isNum) td.className = "num";
      td.textContent = val;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}
async function refresh() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (!res.ok) return;
    const d = await res.json();
    document.getElementById("ver-pill").textContent = "v" + (d.version || "4.7.0");
    document.getElementById("kpi-hit").textContent = d.telemetry.avg_hit_pct !== null ? d.telemetry.avg_hit_pct + "%" : "-";
    document.getElementById("kpi-flips").textContent = String(d.telemetry.recent_flips || 0);
    document.getElementById("kpi-sq").textContent = Math.round((d.wm_squeeze.saved_bytes || 0) / 1024) + " KB";
    document.getElementById("kpi-crys").textContent = String((d.crystals || []).length);

    const sp = d.spool || {};
    document.getElementById("spool-badge").textContent = "spool: " + (sp.pending_count || 0) + " pending · " + (sp.flushed_count || 0) + " flushed";

    const live = d.live || {};
    const act = live.active;
    if (act) {
      lastTurnEpoch = act.last_turn_epoch || null;
      document.getElementById("live-model").textContent = act.model || "unknown";
      const zpill = document.getElementById("live-zone-pill");
      zpill.textContent = act.ctx_zone === "cliff" ? "200k CLIFF" : act.ctx_zone;
      zpill.className = "pill" + (act.ctx_zone === "amber" ? " amber" : (act.ctx_zone === "red" || act.ctx_zone === "cliff" ? " red" : ""));
      const targetLbl = act.is_neon ? "120k Neon" : "200k";
      document.getElementById("live-ctx-main").textContent = fmtTok(act.last_ctx) + " / " + targetLbl + " (" + act.ctx_pct + "%)";
      document.getElementById("live-meta").textContent = "hit " + (act.last_hit_pct ?? "-") + "% · +" + fmtTok(act.last_uncached_delta) + " unc · " + (act.flips || 0) + " flips";
      const meter = document.getElementById("live-meter");
      meter.style.width = Math.min(100, act.ctx_pct || 0) + "%";
      meter.className = "meter-fill " + (act.ctx_zone || "green");
      document.getElementById("live-conv").textContent = act.surface + ":" + act.conv_short + " · " + act.turns + " turns · avg " + (act.avg_hit_pct ?? "-") + "% hit";
      tickTtl();
    }
    renderBreakdown(act);
    renderWorkers(live.workers || []);

    const gr = d.grimoire || {};
    const wf = d.wm_fast || {};
    document.getElementById("grimoire-line").textContent = gr.present
      ? ("tier=" + gr.hardware_tier + " | conf=" + gr.confinement_profile + " (guard=" + (gr.mandala_guard_mode || "advisory") + ") | model=" + gr.model_family + " | warn/cliff=" + fmtTok(gr.context_warn_tokens) + "/" + fmtTok(gr.context_cliff_tokens) + " | wm_fast=" + (wf.calls || 0) + " (" + (wf.avg_ms || 0) + "ms)")
      : ("wm_fast=" + (wf.calls || 0) + " calls | Grimoire profile not seeded yet");
    setList(document.getElementById("list-crystals"), d.crystals, "No cached crystals yet.", (c) => {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = c.reset_cmd || "";
      li.appendChild(code);
      li.appendChild(document.createTextNode(" | " + (c.model || "?") + " | " + Math.round((c.last_ctx || 0) / 1000) + "k ctx | flips=" + (c.model_flips || 0)));
      return li;
    });
    setList(document.getElementById("list-receipts"), d.cpr_ledger_tail, "No CPR bridge entries yet.", (r) => {
      const li = document.createElement("li");
      li.textContent = (r.schema || "receipt") + " sha256=" + (r.digest || "?") + " sig=" + (r.sig_status || "?") + (r.sangha_posted ? " [Sangha]" : " [local]");
      return li;
    });
    setList(document.getElementById("list-stops"), (d.telemetry && d.telemetry.recent_stops) || [], "No Stop telemetry yet.", (s) => {
      const li = document.createElement("li");
      li.textContent = (s.conv_id || "?") + " | " + (s.model || "?") + " | " + Math.round((s.last_ctx || 0) / 1000) + "k | hit=" + (s.last_hit_pct ?? "-") + "% | flips=" + (s.model_flips || 0);
      return li;
    });
  } catch (e) {}
}
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 2000);
setInterval(() => { if (!document.hidden) tickTtl(); }, 1000);

document.getElementById("btn-handoff").addEventListener("click", async () => {
  showActionOut("Crystallizing active session & spooling...");
  try {
    const res = await fetch("/api/actions/handoff", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const j = await res.json();
    if (!j.ok) { showActionOut("Handoff error: " + (j.error || "unknown")); return; }
    const copied = await copyText(j.pointer_prompt || "");
    showActionOut((copied ? "[Copied <=150-tok Handoff to clipboard]\n" : "[Copy Handoff Prompt]\n") + (j.pointer_prompt || "") + "\nsha256=" + j.crystal_sha256 + " | spooled=" + j.spooled);
    refresh();
  } catch (err) { showActionOut("Handoff failed: " + String(err)); }
});
document.getElementById("btn-compact").addEventListener("click", async () => {
  const copied = await copyText("/compact");
  showActionOut(copied ? "Copied `/compact` to clipboard — paste into chat to compact current session." : "Type `/compact` in chat to compact current session.");
});
document.getElementById("btn-audit-t0").addEventListener("click", async () => {
  showActionOut("Auditing Turn-0 plugins & eager MCP tools...");
  try {
    const res = await fetch("/api/actions/audit-turn0", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const j = await res.json();
    showActionOut(
      "Enabled plugins (" + (j.enabled_plugins || []).length + "): " + (j.enabled_plugins || []).join(", ") + "\n" +
      "Eager MCP tools (" + (j.eager_tool_count || 0) + "): " + (j.eager_mcp_tools || []).join(", ") + "\n" +
      "Readonly conflicts: " + ((j.readonly_eager_conflicts || []).join(", ") || "none") + "\n" +
      "Recommendations:\n- " + (j.recommendations || []).join("\n- ")
    );
  } catch (err) { showActionOut("Audit failed: " + String(err)); }
});
document.getElementById("btn-flush-spool").addEventListener("click", async () => {
  showActionOut("Flushing pending_crystals.jsonl to Gen3 LMDB...");
  try {
    const res = await fetch("/api/actions/flush-spool", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const j = await res.json();
    showActionOut(JSON.stringify(j, null, 2));
    refresh();
  } catch (err) { showActionOut("Flush failed: " + String(err)); }
});
document.getElementById("form-recall").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = (document.getElementById("recall-q").value || "").trim();
  if (!q) return;
  document.getElementById("recall-meta").textContent = "searching...";
  try {
    const res = await fetch("/api/actions/recall", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query: q, limit: 6 }) });
    const j = await res.json();
    document.getElementById("recall-meta").textContent = (j.mode || "recall") + " · " + (j.elapsed_ms || 0) + "ms · 0 LLM tok";
    setList(document.getElementById("list-recall"), j.results || [], j.warning ? ("No hits (" + j.warning + ")") : "No matching memories found.", (r) => {
      const li = document.createElement("li");
      const code = document.createElement("code");
      code.textContent = (r.id || "?") + " [" + (r.source || "gen3") + "]";
      li.appendChild(code);
      li.appendChild(document.createTextNode(" " + (r.snippet || "")));
      return li;
    });
  } catch (err) {
    document.getElementById("recall-meta").textContent = "error";
  }
});
document.getElementById("form-status").addEventListener("submit", (e) => { e.preventDefault(); refresh(); });
document.getElementById("form-verify").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = document.getElementById("verify-out");
  out.classList.add("show");
  let parsed;
  try { parsed = JSON.parse(document.getElementById("receipt-input").value || "{}"); }
  catch (err) { out.textContent = "Invalid JSON: " + String(err); return; }
  const suffix = document.getElementById("verify-dry").checked ? "?recorded=1" : "";
  try {
    const res = await fetch("/api/receipt" + suffix, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(parsed) });
    const body = await res.json();
    out.textContent = JSON.stringify(body, null, 2);
    if (res.ok) refresh();
  } catch (err) { out.textContent = "Request failed: " + String(err); }
});
try {
  if (navigator.modelContext && typeof navigator.modelContext.registerTool === "function") {
    const reg = (name, description, inputSchema, execute) => navigator.modelContext.registerTool({ name, description, inputSchema, execute });
    reg("wm_sidecar_status", "Read live telemetry, context meter, squeeze savings, and ready handoff crystals.", { type: "object", properties: {}, additionalProperties: false }, async () => (await fetch("/api/status", { cache: "no-store" })).json());
    reg("wm_sidecar_live", "Read active IDE/CLI context meter, Neon TTL timer, uncached breakdown, and swarm workers.", { type: "object", properties: {}, additionalProperties: false }, async () => (await fetch("/api/live", { cache: "no-store" })).json());
    reg("wm_sidecar_crystals", "List cached handoff crystals from wm-handoff.", { type: "object", properties: {}, additionalProperties: false }, async () => (await fetch("/api/crystals", { cache: "no-store" })).json());
    reg("wm_sidecar_receipts", "List recent verified CPR bridge ledger receipts.", { type: "object", properties: {}, additionalProperties: false }, async () => (await fetch("/api/receipts", { cache: "no-store" })).json());
    reg("wm_sidecar_verify_receipt", "Verify and optionally record a CPR wm-receipt/1.2 JSON payload.", { type: "object", properties: { receipt: { type: "object" }, dry_run: { type: "boolean" } }, required: ["receipt"], additionalProperties: false }, async (input) => {
      const suffix = input && input.dry_run ? "?recorded=1" : "";
      return (await fetch("/api/receipt" + suffix, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify((input && input.receipt) || {}) })).json();
    });
    reg("wm_sidecar_handoff", "Crystallize active session, spool to pending_crystals.jsonl, and return <=150-tok handoff prompt.", { type: "object", properties: { conv_id: { type: "string" }, cwd: { type: "string" } }, additionalProperties: false }, async (input) => (await fetch("/api/actions/handoff", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input || {}) })).json());
    reg("wm_sidecar_audit_turn0", "Audit Turn-0 plugin and eager MCP tool overhead.", { type: "object", properties: { repo_dir: { type: "string" } }, additionalProperties: false }, async (input) => (await fetch("/api/actions/audit-turn0", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input || {}) })).json());
    reg("wm_sidecar_recall", "Zero-LLM-token read-only WhiteMagic memory recall.", { type: "object", properties: { query: { type: "string" }, limit: { type: "integer" } }, required: ["query"], additionalProperties: false }, async (input) => (await fetch("/api/actions/recall", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input || {}) })).json());
    reg("wm_sidecar_flush_spool", "Flush pending_crystals.jsonl into Gen3 LMDB safely.", { type: "object", properties: { dry_run: { type: "boolean" } }, additionalProperties: false }, async (input) => (await fetch("/api/actions/flush-spool", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input || {}) })).json());
  }
} catch (e) {}
</script>
</body>
</html>
"""

_LEDGER_LOCK = threading.Lock()


class SidecarHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def _send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, status: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> tuple[int, dict | None, str]:
        try:
            clen = int(self.headers.get("Content-Length") or "0")
        except Exception:
            clen = -1
        if clen < 0 or clen > MAX_RECEIPT_BYTES:
            return 413, None, f"Payload too large (max {MAX_RECEIPT_BYTES} B)"
        body = self.rfile.read(clen) if clen > 0 else b"{}"
        try:
            obj = json.loads(body.decode("utf-8"))
        except Exception:
            return 400, None, "Invalid JSON body"
        if not isinstance(obj, dict):
            return 400, None, "JSON body must be an object"
        return 200, obj, ""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(200, {"ok": True, "version": VERSION, "pid": os.getpid()})
        elif path == "/" or path == "/index.html":
            self._send_html(200, DASHBOARD_HTML)
        elif path == "/api/status":
            self._send_json(200, _build_status_payload())
        elif path == "/api/live":
            self._send_json(200, {"ok": True, **cockpit.collect_live_cockpit()})
        elif path == "/api/crystals":
            self._send_json(200, {"ok": True, "crystals": _list_crystals(30)})
        elif path == "/api/receipts":
            rows = _read_jsonl_tail(_bridge_ledger_path(), 30)
            self._send_json(200, {"ok": True, "receipts": list(reversed(rows))})
        elif path in ("/api/webmcp", "/.well-known/webmcp.json"):
            self._send_json(200, WEBMCP_MANIFEST)
        else:
            self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path not in (
            "/api/receipt",
            "/api/actions/handoff",
            "/api/actions/audit-turn0",
            "/api/actions/recall",
            "/api/actions/flush-spool",
        ):
            self._send_json(404, {"ok": False, "error": "Not found"})
            return

        code, obj, err = self._read_json_body()
        if code != 200 or obj is None:
            self._send_json(code, {"ok": False, "error": err})
            return

        if path == "/api/actions/handoff":
            st, payload = cockpit.action_handoff(obj, _handoff_cache_dir())
            self._send_json(st, payload)
            return
        if path == "/api/actions/audit-turn0":
            st, payload = cockpit.action_audit_turn0(obj)
            self._send_json(st, payload)
            return
        if path == "/api/actions/recall":
            st, payload = cockpit.action_recall(obj, _handoff_cache_dir())
            self._send_json(st, payload)
            return
        if path == "/api/actions/flush-spool":
            st, payload = cockpit.action_flush_spool(obj)
            self._send_json(st, payload)
            return

        valid, digest, verr = _verify_posted_receipt(obj)
        if not valid:
            self._send_json(400, {"ok": False, "error": verr})
            return

        sig = obj.get("signature")
        sig_status = "hmac-sha256" if isinstance(sig, dict) and sig.get("alg") else "unsigned"
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema": str(obj.get("schema") or ""),
            "digest": digest,
            "sig_status": sig_status,
            "conv_id": str(obj.get("conv_id") or "")[:16],
            "model": str(obj.get("model") or ""),
            "source": "sidecar_api",
            "sangha_posted": bool(obj.get("sangha_posted", False)),
        }
        if "recorded=1" not in query.split("&"):
            lp = _bridge_ledger_path()
            try:
                with _LEDGER_LOCK:
                    lp.parent.mkdir(parents=True, exist_ok=True)
                    with lp.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
                return

        self._send_json(201, {"ok": True, "schema": entry["schema"], "digest": digest})


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT") or os.environ.get("PORT") or "0")
    server = LoopbackHTTPServer(("127.0.0.1", port), SidecarHandler)
    actual_port = server.server_address[1]
    disc_path = _discovery_path()
    disc_payload = {
        "pid": os.getpid(),
        "port": actual_port,
        "url": f"http://127.0.0.1:{actual_port}",
        "version": VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    disc_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = disc_path.with_name(f".{disc_path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(disc_payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, disc_path)

    stop_event = threading.Event()

    def _handle_sig(_signum, _frame):
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        cockpit.shutdown_clients()
        try:
            server.server_close()
        except Exception:
            pass
        try:
            if disc_path.is_file():
                cur = json.loads(disc_path.read_text(encoding="utf-8"))
                if int(cur.get("pid") or -1) == os.getpid():
                    disc_path.unlink(missing_ok=True)
        except Exception:
            disc_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
