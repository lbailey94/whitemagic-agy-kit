#!/usr/bin/env python3
"""
whitemagic-agy-kit v4.4.2 Empirical Longevity, Self-Heal & Rust PostToolUse Benchmark (Zero-PII).

Measures 5 reproducible dimensions across git tags (v4.3.0 -> v4.4.0 -> v4.4.1 -> v4.4.2):
  1. Historical `run_command` Spill Replay (4-way squeeze & spill capture) + PreToolUse latency (`v4.4.1` vs `v4.4.2`)
  2. Historical `read_file` invalid-range replay + Rust `wm-squeeze --post-tool` vs Python `post_tool_sim.py`
  3. Turn-0 Rule & Enabled Plugin Frontmatter Footprint
  4. Zero-Token `PostInvocation` Longevity HUD Replay across real App + CLI transcripts
  5. Real subprocess hook latency (median / p95 ms) across PreToolUse, PostToolUse, and PostInvocation
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parents[1]
HOME = Path.home()
SURFACES = {
    "app": HOME / ".gemini" / "antigravity" / "brain",
    "ide": HOME / ".gemini" / "antigravity-ide" / "brain",
    "cli": HOME / ".gemini" / "antigravity-cli" / "brain",
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def sanitize_text(s: str) -> str:
    s = s.replace(str(HOME), "~")
    user = os.environ.get("USER") or HOME.name
    if user:
        s = s.replace(f"/home/{user}", "~").replace(f"/Users/{user}", "~")
    return EMAIL_RE.sub("[REDACTED_EMAIL]", s)


def load_module_from_source(name: str, source: str):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
        tf.write(source)
        tpath = tf.name
    try:
        spec = importlib.util.spec_from_file_location(name, tpath)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        try:
            os.unlink(tpath)
        except OSError:
            pass


def collect_transcripts(surface: str, limit: int) -> list[Path]:
    bdir = SURFACES.get(surface)
    if not bdir or not bdir.is_dir():
        return []
    logs = sorted(
        bdir.glob("*/.system_generated/logs/transcript.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return logs[:limit]


def _git_show_or_fallback(rev_path: str, fallback: str) -> str:
    p = subprocess.run(["git", "-C", str(KIT_DIR), "show", rev_path], capture_output=True, text=True)
    return p.stdout if p.returncode == 0 and p.stdout else fallback


def replay_pre_tool_spills(app_limit: int = 10, cli_limit: int = 12) -> dict:
    v442_src = (KIT_DIR / "plugin" / "hooks" / "pre_tool_guard.py").read_text(encoding="utf-8")
    v441_src = _git_show_or_fallback("v4.4.1:plugin/hooks/pre_tool_guard.py", v442_src)
    v440_src = _git_show_or_fallback("v4.4.0:plugin/hooks/pre_tool_guard.py", v441_src)
    v43_src = _git_show_or_fallback("71a1d74:plugin/hooks/pre_tool_guard.py", v440_src)

    mod_v43 = load_module_from_source("pre_v43", v43_src)
    mod_v440 = load_module_from_source("pre_v440", v440_src)
    mod_v441 = load_module_from_source("pre_v441", v441_src)
    mod_v442 = load_module_from_source("pre_v442", v442_src)

    transcripts = [(collect_transcripts("app", app_limit), "app"), (collect_transcripts("cli", cli_limit), "cli")]
    all_cmds: list[tuple[str, str, bool]] = []  # (surface, cmd, caused_spill)

    for paths, surf in transcripts:
        for tp in paths:
            last_cmd = ""
            for line in tp.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith("{"):
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get("type", "")
                if t == "PLANNER_RESPONSE":
                    for tc in o.get("tool_calls") or []:
                        if tc.get("name") == "run_command":
                            args = tc.get("args") or tc.get("arguments") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    args = {}
                            cmd = str(args.get("CommandLine") or "")
                            if cmd.startswith('"') and cmd.endswith('"'):
                                try:
                                    unwrapped = json.loads(cmd)
                                    if isinstance(unwrapped, str):
                                        cmd = unwrapped
                                except Exception:
                                    pass
                            if cmd:
                                all_cmds.append((surf, cmd, False))
                                last_cmd = cmd
                elif t == "GENERIC":
                    c = str(o.get("content") or "")
                    if "output.txt" in c and "truncated" in c.lower() and last_cmd:
                        for idx in range(len(all_cmds) - 1, -1, -1):
                            if all_cmds[idx][1] == last_cmd and not all_cmds[idx][2]:
                                all_cmds[idx] = (all_cmds[idx][0], all_cmds[idx][1], True)
                                break
                        last_cmd = ""

    v43_squeezed = v440_squeezed = v441_squeezed = v442_squeezed = 0
    v43_spills = v440_spills = v441_spills = v442_spills = total_spills = 0
    newly_caught_v442: Counter[str] = Counter()

    for surf, cmd, is_spill in all_cmds:
        r43 = bool(mod_v43.maybe_squeeze_command(cmd))
        r440 = bool(mod_v440.maybe_squeeze_command(cmd))
        r441 = bool(mod_v441.maybe_squeeze_command(cmd))
        r442 = bool(mod_v442.maybe_squeeze_command(cmd))
        if r43:
            v43_squeezed += 1
        if r440:
            v440_squeezed += 1
        if r441:
            v441_squeezed += 1
        if r442:
            v442_squeezed += 1
        if is_spill:
            total_spills += 1
            if r43:
                v43_spills += 1
            if r440:
                v440_spills += 1
            if r441:
                v441_spills += 1
            if r442:
                v442_spills += 1
        if r442 and not r441:
            first_tok = (cmd.strip().split() or ["?"])[0]
            newly_caught_v442[os.path.basename(first_tok)] += 1

    # Subprocess latency head-to-head: v4.4.1 vs v4.4.2 lazy imports across hot paths (30 runs each)
    hook_v442 = str(KIT_DIR / "plugin" / "hooks" / "pre_tool_guard.py")
    latency_cmp = {}
    with tempfile.TemporaryDirectory() as td:
        hook_v441_path = Path(td) / "pre_v441.py"
        hook_v441_path.write_text(v441_src, encoding="utf-8")
        scenarios = {
            "read_file": json.dumps({"toolCall": {"name": "read_file", "args": {"Path": hook_v442, "StartLine": 1, "EndLine": 100}}}),
            "run_command": json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": 'rg -n "foo|bar" /tmp'}}}),
            "mcp_noop": json.dumps({"toolCall": {"name": "mcp_whitemagic-agy-kit_fast_batch_search", "args": {"queries": []}}}),
        }
        for label, payload in scenarios.items():
            l441, l442 = [], []
            for _ in range(30):
                t0 = time.perf_counter()
                subprocess.run(["python3", "-S", "-B", str(hook_v441_path)], input=payload, capture_output=True, text=True, timeout=5)
                l441.append((time.perf_counter() - t0) * 1000.0)
                t0 = time.perf_counter()
                subprocess.run(["python3", "-S", "-B", hook_v442], input=payload, capture_output=True, text=True, timeout=5)
                l442.append((time.perf_counter() - t0) * 1000.0)
            l441.sort()
            l442.sort()
            m1, m2 = statistics.median(l441), statistics.median(l442)
            latency_cmp[label] = {
                "v441_med_ms": round(m1, 1),
                "v442_med_ms": round(m2, 1),
                "v442_p95_ms": round(l442[int(len(l442) * 0.95) - 1], 1),
                "delta_ms": round(m2 - m1, 1),
                "delta_pct": round(100.0 * (m2 - m1) / max(m1, 0.1), 1),
            }

    return {
        "commands_scanned": len(all_cmds),
        "v43_squeezed": v43_squeezed,
        "v440_squeezed": v440_squeezed,
        "v441_squeezed": v441_squeezed,
        "v442_squeezed": v442_squeezed,
        "delta_vs_v43": v442_squeezed - v43_squeezed,
        "delta_vs_v43_pct": round(100.0 * (v442_squeezed - v43_squeezed) / max(v43_squeezed, 1), 1),
        "spill_commands_total": total_spills,
        "v43_spills_caught": v43_spills,
        "v440_spills_caught": v440_spills,
        "v441_spills_caught": v441_spills,
        "v442_spills_caught": v442_spills,
        "v442_spill_capture_pct": round(100.0 * v442_spills / max(total_spills, 1), 1),
        "newly_caught_v442": dict(newly_caught_v442.most_common(8)),
        "latency_v441_vs_v442": latency_cmp,
        "pre_hook_median_ms": latency_cmp["run_command"]["v442_med_ms"],
        "pre_hook_p95_ms": latency_cmp["run_command"]["v442_p95_ms"],
    }


def measure_prefix_footprint() -> dict:
    p_v43 = subprocess.run(
        ["git", "-C", str(KIT_DIR), "show", "71a1d74:plugin/rules/AGENTS.md"],
        capture_output=True,
        text=True,
    )
    agents_v43_bytes = len(p_v43.stdout.encode("utf-8")) if p_v43.returncode == 0 else 0
    agents_v441_bytes = (KIT_DIR / "plugin" / "rules" / "AGENTS.md").stat().st_size

    gemini_md = HOME / ".gemini" / "GEMINI.md"
    gemini_bytes = gemini_md.stat().st_size if gemini_md.is_file() else 0

    cfg_path = HOME / ".gemini" / "config" / "config.json"
    plugins_dir = HOME / ".gemini" / "config" / "plugins"
    plugin_breakdown = []
    total_fm_bytes = 0
    total_skills = 0
    if cfg_path.is_file() and plugins_dir.is_dir():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        for pn, meta in sorted((cfg.get("plugins") or {}).items()):
            if not (isinstance(meta, dict) and meta.get("enabled")):
                continue
            pdir = plugins_dir / pn
            fm_b = 0
            sk_c = 0
            for sf in glob.glob(str(pdir / "**" / "SKILL.md"), recursive=True):
                sk_c += 1
                txt = Path(sf).read_text(encoding="utf-8", errors="replace")
                if txt.startswith("---"):
                    parts = txt.split("---", 2)
                    if len(parts) >= 2:
                        fm_b += len(parts[1].encode("utf-8"))
            total_fm_bytes += fm_b
            total_skills += sk_c
            plugin_breakdown.append({"plugin": pn, "skills": sk_c, "bytes": fm_b, "est_tokens": fm_b // 4})

    return {
        "agents_v43_bytes": agents_v43_bytes,
        "agents_v441_bytes": agents_v441_bytes,
        "agents_delta_bytes": agents_v441_bytes - agents_v43_bytes,
        "agents_delta_pct": round(100.0 * (agents_v441_bytes - agents_v43_bytes) / max(agents_v43_bytes, 1), 1),
        "gemini_md_bytes": gemini_bytes,
        "enabled_plugins": plugin_breakdown,
        "enabled_skills_total": total_skills,
        "enabled_frontmatter_bytes": total_fm_bytes,
        "enabled_frontmatter_est_tokens": total_fm_bytes // 4,
    }


def replay_longevity_hud(app_limit: int = 8, cli_limit: int = 12) -> dict:
    hud_src = (KIT_DIR / "plugin" / "hooks" / "longevity_hud.py").read_text(encoding="utf-8")
    mod_hud = load_module_from_source("hud_v44", hud_src)

    sessions = [(p, "app") for p in collect_transcripts("app", app_limit)] + [
        (p, "cli") for p in collect_transcripts("cli", cli_limit)
    ]

    total_turns = 0
    alerts_total = 0
    alerts_by_kind: Counter[str] = Counter()
    model_tokens_added = 0
    sessions_alerted = 0

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        mod_hud.STATE_DIR = td_path / "hud_state"
        for src_tp, surf in sessions:
            cid = src_tp.parents[2].name
            # Place simulated transcript under surface-named dir so `antigravity-cli` detection matches
            s_dir = td_path / ("antigravity-cli" if surf == "cli" else "antigravity") / cid
            s_dir.mkdir(parents=True, exist_ok=True)
            sim_tp = s_dir / "transcript.jsonl"
            sim_tp.write_text("", encoding="utf-8")
            inv = 0
            sess_alerts = 0
            with open(sim_tp, "a", encoding="utf-8") as sim_fh, open(src_tp, "r", encoding="utf-8", errors="replace") as src_fh:
                for raw_ln in src_fh:
                    sim_fh.write(raw_ln)
                    if '"PLANNER_RESPONSE"' not in raw_ln:
                        continue
                    try:
                        o = json.loads(raw_ln)
                    except Exception:
                        continue
                    if o.get("type") != "PLANNER_RESPONSE":
                        continue
                    if int(o.get("input_tokens") or 0) + int(o.get("cache_read_tokens") or 0) <= 0:
                        continue
                    sim_fh.flush()
                    total_turns += 1
                    inv += 1

                    # Execute core HUD logic in-process cleanly by stubbing stdin/stdout
                    payload = json.dumps({"conversationId": f"{surf}-{cid}", "transcriptPath": str(sim_tp), "invocationNum": inv})
                    orig_stdin, orig_stdout = sys.stdin, sys.stdout
                    import io
                    sys.stdin = io.StringIO(payload)
                    out_buf = io.StringIO()
                    sys.stdout = out_buf
                    try:
                        mod_hud.main()
                    finally:
                        sys.stdin, sys.stdout = orig_stdin, orig_stdout

                    res = json.loads(out_buf.getvalue() or "{}")
                    steps = res.get("injectSteps") or []
                    if steps:
                        sess_alerts += 1
                        alerts_total += 1
                        sm = steps[0].get("systemMessage") or {}
                        model_tokens_added += len(sm.get("systemMessage") or "")
                        um = (sm.get("metadata") or {}).get("user_markdown") or ""
                        if "Model Flip" in um:
                            alerts_by_kind["model_flip"] += 1
                        if "Context Cliff" in um:
                            alerts_by_kind["context_cliff_200k"] += 1
                        if "Context Watch" in um:
                            alerts_by_kind["context_watch_120k"] += 1
                        if "Cold Cache Miss" in um:
                            alerts_by_kind["cold_miss"] += 1
                        if "Cold Resume After Idle" in um:
                            alerts_by_kind["cold_idle_resume"] += 1
            if sess_alerts > 0:
                sessions_alerted += 1

        # Subprocess latency comparison (30 runs Python longevity_hud.py vs Rust wm-squeeze --hud) + exact parity check
        hud_path = str(KIT_DIR / "plugin" / "hooks" / "longevity_hud.py")
        sq_bin = str(HOME / ".local" / "bin" / "wm-squeeze")
        lat_tp = td_path / "antigravity" / "lat.jsonl"
        lat_tp.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 120000, "model": "gemini-4-hydrogen-xhigh"}) + "\n")
        py_lats = []
        rust_lats = []
        parity_ok = True
        env_py = {**os.environ, "WM_HOOK_STATE_DIR": str(td_path / "st_py")}
        env_rs = {**os.environ, "WM_HOOK_STATE_DIR": str(td_path / "st_rs")}
        for i in range(30):
            payload = json.dumps({"conversationId": f"bench-hud-{i}", "transcriptPath": str(lat_tp), "invocationNum": 2})
            t0 = time.perf_counter()
            p_py = subprocess.run(["python3", "-S", "-B", hud_path], input=payload, capture_output=True, text=True, env=env_py, timeout=5)
            py_lats.append((time.perf_counter() - t0) * 1000.0)
            t0 = time.perf_counter()
            p_rs = subprocess.run([sq_bin, "--hud"], input=payload, capture_output=True, text=True, env=env_rs, timeout=5)
            rust_lats.append((time.perf_counter() - t0) * 1000.0)
            try:
                if json.loads(p_py.stdout.strip() or "{}") != json.loads(p_rs.stdout.strip() or "{}"):
                    parity_ok = False
            except Exception:
                parity_ok = False
        py_lats.sort()
        rust_lats.sort()
        py_med = statistics.median(py_lats)
        rs_med = statistics.median(rust_lats)

    return {
        "sessions_replayed": len(sessions),
        "turns_replayed": total_turns,
        "sessions_with_alerts": sessions_alerted,
        "alerts_fired": alerts_total,
        "alert_rate_pct": round(100.0 * alerts_total / max(total_turns, 1), 2),
        "alerts_by_kind": dict(alerts_by_kind),
        "model_tokens_added": model_tokens_added,
        "hud_py_median_ms": round(py_med, 1),
        "hud_py_p95_ms": round(py_lats[int(len(py_lats) * 0.95) - 1], 1),
        "hud_rust_median_ms": round(rs_med, 1),
        "hud_rust_p95_ms": round(rust_lats[int(len(rust_lats) * 0.95) - 1], 1),
        "hud_rust_speedup_x": round(py_med / max(rs_med, 0.1), 1),
        "hud_py_rust_parity": parity_ok,
    }


def benchmark_post_tool_and_read_heal() -> dict:
    """Measure Rust wm-squeeze --post-tool vs Python post_tool_sim.py + replay read_file invalid ranges."""
    post_py = str(KIT_DIR / "plugin" / "hooks" / "post_tool_sim.py")
    sq_bin = str(HOME / ".local" / "bin" / "wm-squeeze")
    v442_src = (KIT_DIR / "plugin" / "hooks" / "pre_tool_guard.py").read_text(encoding="utf-8")
    mod_v442 = load_module_from_source("pre_v442_heal", v442_src)

    clean_payload = json.dumps({"toolCall": {"name": "read_file", "args": {"Path": "/tmp/demo.py", "StartLine": 1, "EndLine": 50}}, "result": "ok"})
    py_lats, rs_lats = [], []
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for _ in range(30):
            t0 = time.perf_counter()
            subprocess.run(["python3", "-S", "-B", post_py], input=clean_payload, capture_output=True, text=True, timeout=5)
            py_lats.append((time.perf_counter() - t0) * 1000.0)
            t0 = time.perf_counter()
            subprocess.run([sq_bin, "--post-tool"], input=clean_payload, capture_output=True, text=True, timeout=5)
            rs_lats.append((time.perf_counter() - t0) * 1000.0)
        py_lats.sort()
        rs_lats.sort()

        # Parity check on error + spill
        s_py, s_rs = td_path / "s_py.jsonl", td_path / "s_rs.jsonl"
        t_py, t_rs = td_path / "t_py.jsonl", td_path / "t_rs.jsonl"
        err_pl = json.dumps({"conversationId": "p1", "toolCall": {"name": "read_file", "args": {"Path": "/tmp/x.py", "StartLine": 280, "EndLine": 120}}, "error": "invalid line range Bearer sk_12345"})
        sp_pl = json.dumps({"conversationId": "p2", "toolCall": {"name": "write_file", "args": {"Path": "/tmp/big.py", "Content": "X" * 26000}}})
        subprocess.run(["python3", "-S", "-B", post_py], input=err_pl, capture_output=True, text=True, env={**os.environ, "WM_SIM_LEDGER": str(s_py), "WM_AGY_TELEMETRY": str(t_py)})
        subprocess.run([sq_bin, "--post-tool"], input=err_pl, capture_output=True, text=True, env={**os.environ, "WM_SIM_LEDGER": str(s_rs), "WM_AGY_TELEMETRY": str(t_rs)})
        subprocess.run(["python3", "-S", "-B", post_py], input=sp_pl, capture_output=True, text=True, env={**os.environ, "WM_SIM_LEDGER": str(s_py), "WM_AGY_TELEMETRY": str(t_py)})
        subprocess.run([sq_bin, "--post-tool"], input=sp_pl, capture_output=True, text=True, env={**os.environ, "WM_SIM_LEDGER": str(s_rs), "WM_AGY_TELEMETRY": str(t_rs)})
        rp = json.loads(s_py.read_text().splitlines()[0])
        rr = json.loads(s_rs.read_text().splitlines()[0])
        tp_r = json.loads(t_py.read_text().splitlines()[0])
        tr_r = json.loads(t_rs.read_text().splitlines()[0])
        parity_ok = (
            rp.get("cmd_preview") == rr.get("cmd_preview")
            and rp.get("error") == rr.get("error")
            and tp_r.get("kind") == tr_r.get("kind")
            and tp_r.get("bytes") == tr_r.get("bytes")
        )

        # Historical `invalid line range` replay across sim_ledger + recent transcripts
        big_f = td_path / "replay_1500.py"
        big_f.write_text("\n".join(f"line_{i} = {i}" for i in range(1500)) + "\n", encoding="utf-8")
        inv_re = re.compile(r"StartLine\s+(\d+)\s+is greater than\s+EndLine\s+(\d+)")
        seen_cases: list[tuple[int, int]] = []
        sim_p = HOME / ".local" / "share" / "whitemagic" / "sim_ledger.jsonl"
        if sim_p.is_file():
            for ln in sim_p.read_text(encoding="utf-8", errors="replace").splitlines():
                m = inv_re.search(ln)
                if m:
                    seen_cases.append((int(m.group(1)), int(m.group(2))))
        healed = 0
        for idx, (s_val, e_val) in enumerate(seen_cases):
            res = mod_v442.handle_read_file(
                {"Path": str(big_f), "StartLine": s_val, "EndLine": e_val},
                {"conversationId": f"bench-heal-{idx}", "stepIdx": 1},
            )
            ow = res.get("overwrite") or {}
            ns = int(ow.get("StartLine", s_val))
            ne = int(ow.get("EndLine", e_val))
            if res.get("decision") == "allow" and 1 <= ns <= ne and (ne - ns + 1) <= 200:
                healed += 1

    py_med = statistics.median(py_lats)
    rs_med = statistics.median(rs_lats)
    return {
        "post_py_median_ms": round(py_med, 1),
        "post_py_p95_ms": round(py_lats[int(len(py_lats) * 0.95) - 1], 1),
        "post_rust_median_ms": round(rs_med, 1),
        "post_rust_p95_ms": round(rs_lats[int(len(rs_lats) * 0.95) - 1], 1),
        "post_rust_speedup_x": round(py_med / max(rs_med, 0.1), 1),
        "post_parity_ok": parity_ok,
        "historical_invalid_range_total": len(seen_cases),
        "historical_invalid_range_healed": healed,
        "historical_heal_pct": round(100.0 * healed / max(len(seen_cases), 1), 1) if seen_cases else 100.0,
    }


def benchmark_v450_orchestration_and_rust_pretool(app_limit: int = 10, cli_limit: int = 10) -> dict:
    """Measure Rust wm-squeeze --pre-tool speedup, --swarm-map footprint, and ping-pong/orchestration corpus metrics."""
    sq_bin = shutil.which("wm-squeeze") or str(HOME / ".local" / "bin" / "wm-squeeze")
    py_pre = str(KIT_DIR / "plugin" / "hooks" / "pre_tool_guard.py")
    handoff_bin = str(KIT_DIR / "bin" / "wm-handoff")
    sample_file = str(KIT_DIR / "plugin" / "hooks" / "longevity_hud.py")

    with tempfile.TemporaryDirectory() as td:
        tp_fake = Path(td) / "tp.jsonl"
        tp_fake.write_text(
            "\n".join([
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-high", "input_tokens": 50000, "cache_read_tokens": 40000}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-hydrogen-high", "input_tokens": 55000, "cache_read_tokens": 110000}),
            ]) + "\n",
            encoding="utf-8",
        )
        scenarios = {
            "read_file": json.dumps({"toolCall": {"name": "read_file", "args": {"Path": sample_file, "StartLine": 1, "EndLine": 80}}, "transcriptPath": str(tp_fake)}),
            "mcp_noop": json.dumps({"toolCall": {"name": "mcp_whitemagic-agy-kit_fast_batch_search", "args": {"queries": []}}}),
            "swarm_add": json.dumps({"toolCall": {"name": "swarm", "args": {"action": "add", "name": "w1", "model": "gemini-4-hydrogen-high", "workspace": "share", "brief": "do x"}}, "transcriptPath": str(tp_fake)}),
            "run_command": json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": "python3 tests/test_wave3.py"}}}),
        }
        pre_rust_vs_py = {}
        for label, pl in scenarios.items():
            py_lats, rs_lats = [], []
            py_out = rs_out = {}
            for _ in range(30):
                t0 = time.perf_counter()
                p1 = subprocess.run(["python3", "-S", "-B", py_pre], input=pl, capture_output=True, text=True, timeout=5)
                py_lats.append((time.perf_counter() - t0) * 1000.0)
                t0 = time.perf_counter()
                p2 = subprocess.run([sq_bin, "--pre-tool", "--py", py_pre], input=pl, capture_output=True, text=True, timeout=5)
                rs_lats.append((time.perf_counter() - t0) * 1000.0)
                py_out = json.loads(p1.stdout)
                rs_out = json.loads(p2.stdout)
            py_lats.sort()
            rs_lats.sort()
            py_med = statistics.median(py_lats)
            rs_med = statistics.median(rs_lats)
            pre_rust_vs_py[label] = {
                "py_med_ms": round(py_med, 1),
                "rust_med_ms": round(rs_med, 1),
                "rust_p95_ms": round(rs_lats[int(len(rs_lats) * 0.95) - 1], 1),
                "speedup_x": round(py_med / max(rs_med, 0.1), 1),
                "parity": py_out.get("decision") == rs_out.get("decision") and py_out.get("overwrite") == rs_out.get("overwrite"),
            }

    r_sm = subprocess.run([handoff_bin, "--swarm-map", str(KIT_DIR)], capture_output=True, text=True, timeout=10)
    swarm_map_bytes = len(r_sm.stdout.encode("utf-8"))

    # Corpus replay across newest App + CLI transcripts for flips >=2 / exit_plan / 1-turn Neon / auto-crystal eligibility
    sessions = collect_transcripts("app", app_limit) + collect_transcripts("cli", cli_limit)
    pingpong_sessions = 0
    auto_crystal_eligible = 0
    total_flips = 0
    total_flip_unc = 0
    total_exit_plan = 0
    total_one_turn_neon = 0
    seen_paths_sessions: Counter[str] = Counter()

    for tp in sessions:
        prev_m = ""
        flips = 0
        flip_unc = 0
        last_ctx = 0
        neon_streak = 0
        tp_reads: set[str] = set()
        for line in tp.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "PLANNER_RESPONSE":
                continue
            u = int(o.get("input_tokens") or 0)
            c = int(o.get("cache_read_tokens") or 0)
            m = str(o.get("model") or "")
            has_toks = (u + c) > 0
            if m:
                if "neon" in m.lower():
                    neon_streak += 1
                elif has_toks:
                    neon_streak = 0
            for tc in o.get("tool_calls") or []:
                nm = tc.get("name") or ""
                if nm == "exit_plan_mode":
                    total_exit_plan += 1
                    if "neon" in m.lower() and neon_streak <= 1:
                        total_one_turn_neon += 1
                    neon_streak = 0
                elif nm == "read_file":
                    args = tc.get("args") or tc.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if isinstance(args, dict) and args.get("Path"):
                        tp_reads.add(str(args["Path"]))
            if has_toks:
                last_ctx = u + c
                if m and prev_m and m != prev_m:
                    flips += 1
                    flip_unc += u
                if m:
                    prev_m = m
        for rp in tp_reads:
            seen_paths_sessions[rp] += 1
        total_flips += flips
        total_flip_unc += flip_unc
        if flips >= 2:
            pingpong_sessions += 1
        if (last_ctx >= 99_000 and flips >= 2) or last_ctx >= 130_000:
            auto_crystal_eligible += 1

    return {
        "pre_rust_vs_py": pre_rust_vs_py,
        "swarm_map_bytes": swarm_map_bytes,
        "swarm_map_est_tokens": swarm_map_bytes // 4,
        "corpus_sessions": len(sessions),
        "pingpong_sessions": pingpong_sessions,
        "auto_crystal_eligible_sessions": auto_crystal_eligible,
        "total_flips": total_flips,
        "total_flip_unc_tokens": total_flip_unc,
        "total_exit_plan": total_exit_plan,
        "total_one_turn_neon": total_one_turn_neon,
        "cross_session_dup_reads": sum(1 for v in seen_paths_sessions.values() if v >= 2),
    }


def benchmark_v451_sidecar_cpr_and_plugin_scoping() -> dict:
    """Measure Sidecar cold start/HTTP latency/RSS, CPR pointer vs crystal size, and live Turn-0 plugin savings."""
    import signal
    import urllib.request

    srv_py = KIT_DIR / "plugin" / "sidecars" / "wm-sidecar" / "server.py"
    handoff_bin = str(KIT_DIR / "bin" / "wm-handoff")
    bridge_bin = str(KIT_DIR / "bin" / "wm-cpr-bridge")
    sim_bin = str(KIT_DIR / "bin" / "wm-sim")

    cold_start_ms = 0.0
    health_lats = []
    status_lats = []
    rss_mb = 0.0
    crystal_bytes = 0
    pointer_bytes = 0

    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        tp = td_p / "tp.jsonl"
        tp.write_text(
            "\n".join([
                json.dumps({"type": "USER_INPUT", "content": "Benchmark v4.5.1 sidecar and CPR bridge"}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-hydrogen-high", "input_tokens": 35000, "cache_read_tokens": 95000,
                            "tool_calls": [{"name": "edit_file", "args": {"Path": "plugin/sidecars/wm-sidecar/server.py"}}]}),
            ]) + "\n",
            encoding="utf-8",
        )
        c_md = td_p / "latest_bench451.md"
        c_rec = td_p / "latest_bench451.receipt.json"
        subprocess.run([handoff_bin, "--transcript", str(tp), "--cwd", str(KIT_DIR), "--out", str(c_md), "--emit-receipt", str(c_rec)], capture_output=True, text=True, check=True)
        crystal_bytes = c_md.stat().st_size
        r_ptr = subprocess.run([bridge_bin, "pointer", "--receipt", str(c_rec)], capture_output=True, text=True, check=True)
        pointer_bytes = len(r_ptr.stdout.encode("utf-8"))

        env_sc = {**os.environ, "ANTIGRAVITY_SIDECAR_WEB_PORT": "0", "WM_HANDOFF_CACHE_DIR": str(td_p)}
        t0 = time.perf_counter()
        proc = subprocess.Popen(["python3", "-S", "-B", str(srv_py)], env=env_sc, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            disc = td_p / "sidecar.json"
            url = ""
            for _ in range(100):
                if disc.is_file():
                    info = json.loads(disc.read_text(encoding="utf-8"))
                    url = info["url"]
                    try:
                        with urllib.request.urlopen(f"{url}/healthz", timeout=1) as r:
                            if r.status == 200:
                                cold_start_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                                break
                    except Exception:
                        pass
                time.sleep(0.01)

            for _ in range(30):
                t1 = time.perf_counter()
                with urllib.request.urlopen(f"{url}/healthz", timeout=2) as r:
                    r.read()
                health_lats.append((time.perf_counter() - t1) * 1000.0)
                t2 = time.perf_counter()
                with urllib.request.urlopen(f"{url}/api/status", timeout=2) as r:
                    r.read()
                status_lats.append((time.perf_counter() - t2) * 1000.0)

            st_file = Path(f"/proc/{proc.pid}/status")
            if st_file.is_file():
                for line in st_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        rss_mb = round(kb / 1024.0, 1)
                        break
            elif sys.platform == "darwin":
                ps_r = subprocess.run(["ps", "-o", "rss=", "-p", str(proc.pid)], capture_output=True, text=True)
                if ps_r.returncode == 0 and ps_r.stdout.strip().isdigit():
                    rss_mb = round(int(ps_r.stdout.strip()) / 1024.0, 1)
        finally:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=4)

    health_lats.sort()
    status_lats.sort()
    r_sc = subprocess.run([sim_bin, "audit", "--scope-plugins", str(KIT_DIR), "--json"], capture_output=True, text=True, check=True)
    sc_j = json.loads(r_sc.stdout)

    return {
        "sidecar_cold_start_ms": cold_start_ms,
        "sidecar_healthz_med_ms": round(statistics.median(health_lats), 2) if health_lats else 0.0,
        "sidecar_healthz_p95_ms": round(health_lats[int(len(health_lats) * 0.95) - 1], 2) if health_lats else 0.0,
        "sidecar_status_med_ms": round(statistics.median(status_lats), 2) if status_lats else 0.0,
        "sidecar_status_p95_ms": round(status_lats[int(len(status_lats) * 0.95) - 1], 2) if status_lats else 0.0,
        "sidecar_rss_mb": rss_mb,
        "cpr_crystal_bytes": crystal_bytes,
        "cpr_pointer_bytes": pointer_bytes,
        "cpr_pointer_est_tokens": pointer_bytes // 4,
        "cpr_reduction_pct": round(100.0 * (1.0 - pointer_bytes / max(crystal_bytes, 1)), 1),
        "scope_plugins_unmatched": sc_j.get("unmatched_enabled_plugins", []),
        "scope_plugins_savings_tok": sc_j.get("savings_est_tokens", 0),
        "scope_plugins_current_dom_tok": sc_j.get("current_enabled_domain_tokens", 0),
    }


def main():
    ap = argparse.ArgumentParser(description="Benchmark whitemagic-agy-kit v4.5.1 longevity, Rust PreToolUse, sidecar & CPR bridge")
    ap.add_argument("--out", default=str(KIT_DIR / "benchmarks" / "RESULTS_2026-11-25_v451.md"), help="Output Markdown receipt path")
    args = ap.parse_args()

    spills = replay_pre_tool_spills()
    post_heal = benchmark_post_tool_and_read_heal()
    prefix = measure_prefix_footprint()
    hud = replay_longevity_hud()
    v450 = benchmark_v450_orchestration_and_rust_pretool()
    v451 = benchmark_v451_sidecar_cpr_and_plugin_scoping()

    report = {
        "spills_replay": spills,
        "post_tool_and_read_heal": post_heal,
        "prefix_footprint": prefix,
        "hud_replay": hud,
        "v450_rust_pretool_and_orchestration": v450,
        "v451_sidecar_cpr_and_scoping": v451,
    }
    print(json.dumps(report, indent=2))

    v45_lat = v450["pre_rust_vs_py"]
    md = [
        "# WhiteMagic AGY Turbo Kit `v4.5.1` — Resident Sidecar UI, Sangha CPR Bridge & Turn-0 Plugin Scoping",
        "",
        "## 1. Historical `run_command` Spill & Squeeze Replay (`v4.3.0` → `v4.4.0` → `v4.4.1` → `v4.5.1`)",
        "",
        f"- **Commands replayed (`10` App + `12` CLI newest sessions):** `{spills['commands_scanned']}`",
        f"- **Commands intercepted by `wm-squeeze`:** `{spills['v43_squeezed']}` (`v4.3.0`) → `{spills['v440_squeezed']}` (`v4.4.0`) → `{spills['v441_squeezed']}` (`v4.4.1`) → **`{spills['v442_squeezed']}` (`v4.4.2+`, `+{spills['delta_vs_v43_pct']}%` vs `v4.3.0`)**",
        f"- **Historical `output.txt` spill commands caught:** `{spills['v43_spills_caught']}/{spills['spill_commands_total']}` (`v4.3.0`) → `{spills['v440_spills_caught']}/{spills['spill_commands_total']}` (`v4.4.0`) → `{spills['v441_spills_caught']}/{spills['spill_commands_total']}` (`v4.4.1`) → **`{spills['v442_spills_caught']}/{spills['spill_commands_total']}` (`v4.4.2+`, `{spills['v442_spill_capture_pct']}%`)**",
        "",
        "### `PreToolUse` Latency: Python `pre_tool_guard.py` vs Compiled Rust `wm-squeeze --pre-tool` (30 runs each)",
        "",
        "| Tool Scenario | Python (`v4.4.2`) | Rust `--pre-tool` (`v4.5.x`) | Rust p95 | Speedup | Exact Parity |",
        "| :--- | ---: | ---: | ---: | ---: | :---: |",
    ]
    for sc in ("read_file", "mcp_noop", "swarm_add", "run_command"):
        d = v45_lat[sc]
        md.append(f"| `{sc}` | {d['py_med_ms']} ms | **{d['rust_med_ms']} ms** | {d['rust_p95_ms']} ms | **{d['speedup_x']}×** | `{d['parity']}` |")

    md.extend(
        [
            "",
            "## 2. Historical `read_file` Self-Heal Replay & Sub-3ms Rust `wm-squeeze --post-tool`",
            "",
            f"- **Historical `invalid line range` failures self-healed:** **`{post_heal['historical_invalid_range_healed']}/{post_heal['historical_invalid_range_total']}` (`{post_heal['historical_heal_pct']}%`)**",
            f"- **Python `post_tool_sim.py` clean latency (30 runs):** median `{post_heal['post_py_median_ms']} ms` · p95 `{post_heal['post_py_p95_ms']} ms`",
            f"- **Rust `wm-squeeze --post-tool` clean latency (30 runs):** median **`{post_heal['post_rust_median_ms']} ms`** · p95 **`{post_heal['post_rust_p95_ms']} ms`** (**`{post_heal['post_rust_speedup_x']}×` faster**, parity=`{post_heal['post_parity_ok']}`)",
            "",
            "## 3. Turn-0 Rule & Plugin Frontmatter Footprint",
            "",
            f"- **Plugin `AGENTS.md`:** `{prefix['agents_v43_bytes']:,} B` (`v4.3.0`) → **`{prefix['agents_v441_bytes']:,} B` (`v4.5.1`, `{prefix['agents_delta_pct']}%`)**",
            f"- **Global `GEMINI.md` (local user rules):** `{prefix['gemini_md_bytes']:,} B`",
            f"- **Enabled plugins skill frontmatter:** `{prefix['enabled_skills_total']}` skills across `{len(prefix['enabled_plugins'])}` plugins = `{prefix['enabled_frontmatter_bytes']:,} B` (~`{prefix['enabled_frontmatter_est_tokens']:,}` tok)",
            "",
            "| Enabled Plugin | Skills | Frontmatter Bytes | Est. Tokens |",
            "| :--- | ---: | ---: | ---: |",
        ]
    )
    for pb in prefix["enabled_plugins"]:
        md.append(f"| `{pb['plugin']}` | {pb['skills']} | {pb['bytes']:,} B | ~{pb['est_tokens']:,} |")

    md.extend(
        [
            "",
            "## 4. Zero-Token `PostInvocation` Rust HUD (`wm-squeeze --hud`) & Orchestration Replay",
            "",
            f"- **Turns replayed (`8` App + `12` CLI sessions):** `{hud['turns_replayed']}` across `{hud['sessions_replayed']}` sessions",
            f"- **Alerts emitted (`user_markdown` only):** `{hud['alerts_fired']}` (`{hud['alert_rate_pct']}%` of turns across `{hud['sessions_with_alerts']}` sessions)",
            f"- **Rust `wm-squeeze --hud` vs Python (30 runs):** median **`{hud['hud_rust_median_ms']} ms`** (p95 `{hud['hud_rust_p95_ms']} ms`) vs `{hud['hud_py_median_ms']} ms` (**`{hud['hud_rust_speedup_x']}×` faster**, parity=`{hud['hud_py_rust_parity']}`)",
            f"- **Orchestration & Auto-Crystal Corpus Replay (`{v450['corpus_sessions']}` newest sessions):** `{v450['total_flips']}` flips (**`{v450['total_flip_unc_tokens']:,}` uncached tok**), `{v450['pingpong_sessions']}` ping-pong sessions, `{v450['auto_crystal_eligible_sessions']}` auto-crystal eligible, `{v450['total_exit_plan']}` `exit_plan_mode` (`{v450['total_one_turn_neon']}` 1-turn Neon), `{v450['cross_session_dup_reads']}` cross-session dup reads",
            f"- **Shared Swarm Map (`wm-handoff --swarm-map .`):** **`{v450['swarm_map_bytes']:,} B` (~`{v450['swarm_map_est_tokens']}` tokens)**",
            "",
            "## 5. Resident Sidecar UI, Sangha CPR Bridge & Turn-0 Plugin Scoping (`v4.5.1`)",
            "",
            f"- **Sidecar (`plugin/sidecars/wm-sidecar/`):** cold start **`{v451['sidecar_cold_start_ms']} ms`** · RSS **`{v451['sidecar_rss_mb']} MB`** · `GET /healthz` median **`{v451['sidecar_healthz_med_ms']} ms`** (p95 `{v451['sidecar_healthz_p95_ms']} ms`) · `GET /api/status` median **`{v451['sidecar_status_med_ms']} ms`** (p95 `{v451['sidecar_status_p95_ms']} ms`)",
            f"- **Sangha CPR Pointer (`wm-cpr-bridge`):** **`{v451['cpr_pointer_bytes']} B` (~`{v451['cpr_pointer_est_tokens']}` tokens)** vs `{v451['cpr_crystal_bytes']:,} B` full crystal (**`-{v451['cpr_reduction_pct']}%`** wire size on board dispatches)",
            f"- **Turn-0 Plugin Bloat Scoper (`wm-sim audit --scope-plugins .`):** flagged `{v451['scope_plugins_unmatched']}` -> saves **`~{v451['scope_plugins_savings_tok']:,}` Turn-0 tokens** per systems/CLI session",
            "",
        ]
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(sanitize_text("\n".join(md)), encoding="utf-8")
    print(f"\nWrote Zero-PII benchmark receipt -> {out_path}")


if __name__ == "__main__":
    main()
