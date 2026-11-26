#!/usr/bin/env python3
"""
WhiteMagic AGY Telemetry & Handoff Resident Sidecar (v4.5.1).
Stdlib-only loopback ThreadingHTTPServer providing:
  - GET /healthz
  - GET / (dark-mode Aux-Pane dashboard, Cache-Control: no-store, DOM textContent-safe)
  - GET /api/status
  - GET /api/crystals
  - GET /api/receipts
  - GET /api/webmcp & /.well-known/webmcp.json (Declarative/Imperative WebMCP manifest)
  - POST /api/receipt (validates wm-receipt/1.2 | wm-confinement-receipt/1.1 + optional HMAC-SHA256)
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
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "4.6.0"
MAX_RECEIPT_BYTES = 16384

WEBMCP_MANIFEST = {
    "schema": "webmcp/1.0",
    "server": "wm-sidecar",
    "version": VERSION,
    "tools": [
        {
            "name": "wm_sidecar_status",
            "description": "Refresh live telemetry, cache warmth, squeeze savings, and ready handoff crystals.",
            "method": "GET",
            "path": "/api/status",
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


def _redact_home(val: str) -> str:
    if not val:
        return ""
    home_str = str(Path.home())
    if val == home_str:
        return "~"
    if val.startswith(home_str + "/"):
        return "~" + val[len(home_str) :]
    return val


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

    return {
        "ok": True,
        "version": VERSION,
        "pid": os.getpid(),
        "hosts_detected": hosts_detected,
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
  :root { color-scheme: dark; --bg: #0e1116; --card: #161b22; --bd: #2a313c; --fg: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --ok: #3fb950; --warn: #d29922; }
  body { margin: 0; padding: 14px; font: 13px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); }
  h1 { margin: 0 0 10px; font-size: 15px; display: flex; justify-content: space-between; align-items: center; }
  .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--bd); color: var(--ok); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin-bottom: 12px; }
  .card { background: var(--card); border: 1px solid var(--bd); border-radius: 8px; padding: 10px; margin-bottom: 10px; }
  .kpi-lbl { font-size: 11px; color: var(--muted); }
  .kpi-val { font-size: 16px; font-weight: 600; margin-top: 2px; }
  h2 { margin: 0 0 8px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
  ul { list-style: none; padding: 0; margin: 0; }
  li { padding: 6px 0; border-top: 1px solid var(--bd); font-size: 12px; }
  li:first-child { border-top: 0; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; background: #0d1117; padding: 1px 4px; border-radius: 4px; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
  button { background: #1f6feb; color: #fff; border: 0; border-radius: 6px; padding: 5px 10px; font-size: 12px; cursor: pointer; }
  button:hover { background: #388bfd; }
  textarea { width: 100%; box-sizing: border-box; background: #0d1117; color: var(--fg); border: 1px solid var(--bd); border-radius: 6px; padding: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; margin-bottom: 6px; }
  label.chk { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 4px; }
  pre#verify-out { margin: 6px 0 0; font-size: 11px; color: var(--muted); white-space: pre-wrap; word-break: break-word; }
</style>
</head>
<body>
  <h1><span>WhiteMagic AGY Telemetry</span><span id="ver-pill" class="pill">v4.5.1</span></h1>
  <div class="grid">
    <div class="card"><div class="kpi-lbl">Avg Cache Hit</div><div class="kpi-val" id="kpi-hit">-</div></div>
    <div class="card"><div class="kpi-lbl">Recent Flips</div><div class="kpi-val" id="kpi-flips">-</div></div>
    <div class="card"><div class="kpi-lbl">Squeeze Saved</div><div class="kpi-val" id="kpi-sq">-</div></div>
    <div class="card"><div class="kpi-lbl">Ready Crystals</div><div class="kpi-val" id="kpi-crys">-</div></div>
  </div>
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
      <textarea id="receipt-input" name="receipt_json" rows="3" placeholder='{"schema":"wm-receipt/1.2",...}'></textarea>
      <div class="row">
        <label class="chk"><input id="verify-dry" type="checkbox" name="dry_run"> Dry-run verify only</label>
        <button id="btn-verify" type="submit">Verify Receipt</button>
      </div>
    </form>
    <pre id="verify-out"></pre>
  </div>
<script>
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
async function refresh() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (!res.ok) return;
    const d = await res.json();
    document.getElementById("ver-pill").textContent = "v" + (d.version || "4.5.1");
    document.getElementById("kpi-hit").textContent = d.telemetry.avg_hit_pct !== null ? d.telemetry.avg_hit_pct + "%" : "-";
    document.getElementById("kpi-flips").textContent = String(d.telemetry.recent_flips || 0);
    document.getElementById("kpi-sq").textContent = Math.round((d.wm_squeeze.saved_bytes || 0) / 1024) + " KB";
    document.getElementById("kpi-crys").textContent = String((d.crystals || []).length);
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
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
document.getElementById("form-status").addEventListener("submit", (e) => {
  e.preventDefault();
  refresh();
});
document.getElementById("form-verify").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = document.getElementById("verify-out");
  let parsed;
  try {
    parsed = JSON.parse(document.getElementById("receipt-input").value || "{}");
  } catch (err) {
    out.textContent = "Invalid JSON: " + String(err);
    return;
  }
  const suffix = document.getElementById("verify-dry").checked ? "?recorded=1" : "";
  try {
    const res = await fetch("/api/receipt" + suffix, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed),
    });
    const body = await res.json();
    out.textContent = JSON.stringify(body, null, 2);
    if (res.ok) refresh();
  } catch (err) {
    out.textContent = "Request failed: " + String(err);
  }
});
try {
  if (navigator.modelContext && typeof navigator.modelContext.registerTool === "function") {
    navigator.modelContext.registerTool({
      name: "wm_sidecar_status",
      description: "Read live telemetry, cache warmth, squeeze savings, and ready handoff crystals.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      execute: async () => (await fetch("/api/status", { cache: "no-store" })).json(),
    });
    navigator.modelContext.registerTool({
      name: "wm_sidecar_crystals",
      description: "List cached handoff crystals from wm-handoff.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      execute: async () => (await fetch("/api/crystals", { cache: "no-store" })).json(),
    });
    navigator.modelContext.registerTool({
      name: "wm_sidecar_receipts",
      description: "List recent verified CPR bridge ledger receipts.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      execute: async () => (await fetch("/api/receipts", { cache: "no-store" })).json(),
    });
    navigator.modelContext.registerTool({
      name: "wm_sidecar_verify_receipt",
      description: "Verify and optionally record a CPR wm-receipt/1.2 JSON payload.",
      inputSchema: {
        type: "object",
        properties: { receipt: { type: "object" }, dry_run: { type: "boolean" } },
        required: ["receipt"],
        additionalProperties: false,
      },
      execute: async (input) => {
        const suffix = input && input.dry_run ? "?recorded=1" : "";
        const res = await fetch("/api/receipt" + suffix, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify((input && input.receipt) || {}),
        });
        return res.json();
      },
    });
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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(200, {"ok": True, "version": VERSION, "pid": os.getpid()})
        elif path == "/" or path == "/index.html":
            self._send_html(200, DASHBOARD_HTML)
        elif path == "/api/status":
            self._send_json(200, _build_status_payload())
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
        if path != "/api/receipt":
            self._send_json(404, {"ok": False, "error": "Not found"})
            return
        try:
            clen = int(self.headers.get("Content-Length") or "0")
        except Exception:
            clen = -1
        if clen < 0 or clen > MAX_RECEIPT_BYTES:
            self._send_json(413, {"ok": False, "error": f"Payload too large (max {MAX_RECEIPT_BYTES} B)"})
            return
        body = self.rfile.read(clen)
        try:
            obj = json.loads(body.decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "Invalid JSON body"})
            return

        valid, digest, err = _verify_posted_receipt(obj)
        if not valid:
            self._send_json(400, {"ok": False, "error": err})
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
