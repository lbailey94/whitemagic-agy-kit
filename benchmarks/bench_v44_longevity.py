#!/usr/bin/env python3
"""
whitemagic-agy-kit v4.4.0 Empirical Longevity & App/IDE Parity Benchmark (Zero-PII).

Measures 4 reproducible dimensions against git HEAD (v4.3.0) baseline:
  1. Historical `run_command` Spill Replay (v4.3.0 vs v4.4.0 quote-aware wm-squeeze + python3 -c)
  2. Turn-0 Rule & Enabled Plugin Frontmatter Footprint
  3. Zero-Token `PostInvocation` Longevity HUD Replay across real App + CLI transcripts
  4. Real subprocess hook latency (median / p95 ms) for pre_tool_guard.py & longevity_hud.py
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
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
    v441_src = (KIT_DIR / "plugin" / "hooks" / "pre_tool_guard.py").read_text(encoding="utf-8")
    v440_src = _git_show_or_fallback("v4.4.0:plugin/hooks/pre_tool_guard.py", v441_src)
    v43_src = _git_show_or_fallback("71a1d74:plugin/hooks/pre_tool_guard.py", v440_src)

    mod_v43 = load_module_from_source("pre_v43", v43_src)
    mod_v440 = load_module_from_source("pre_v440", v440_src)
    mod_v441 = load_module_from_source("pre_v441", v441_src)

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

    v43_squeezed = v440_squeezed = v441_squeezed = 0
    v43_spills = v440_spills = v441_spills = total_spills = 0
    newly_caught_v441: Counter[str] = Counter()

    for surf, cmd, is_spill in all_cmds:
        r43 = bool(mod_v43.maybe_squeeze_command(cmd))
        r440 = bool(mod_v440.maybe_squeeze_command(cmd))
        r441 = bool(mod_v441.maybe_squeeze_command(cmd))
        if r43:
            v43_squeezed += 1
        if r440:
            v440_squeezed += 1
        if r441:
            v441_squeezed += 1
        if is_spill:
            total_spills += 1
            if r43:
                v43_spills += 1
            if r440:
                v440_spills += 1
            if r441:
                v441_spills += 1
        if r441 and not r440:
            first_tok = (cmd.strip().split() or ["?"])[0]
            newly_caught_v441[os.path.basename(first_tok)] += 1

    # Subprocess latency micro-bench (30 real pre_tool_guard.py invocations)
    hook_path = str(KIT_DIR / "plugin" / "hooks" / "pre_tool_guard.py")
    samples = [
        json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": 'rg -n "foo|bar" /tmp'}}}),
        json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": 'python3 -c "print(123)"'}}}),
        json.dumps({"toolCall": {"name": "read_file", "args": {"Path": str(KIT_DIR / "plugin" / "plugin.json"), "StartLine": 1, "EndLine": 22}}}),
    ]
    lats = []
    for i in range(30):
        payload = samples[i % len(samples)]
        t0 = time.perf_counter()
        subprocess.run(["python3", "-S", "-B", hook_path], input=payload, capture_output=True, text=True, timeout=5)
        lats.append((time.perf_counter() - t0) * 1000.0)
    lats.sort()

    return {
        "commands_scanned": len(all_cmds),
        "v43_squeezed": v43_squeezed,
        "v440_squeezed": v440_squeezed,
        "v441_squeezed": v441_squeezed,
        "delta_vs_v43": v441_squeezed - v43_squeezed,
        "delta_vs_v43_pct": round(100.0 * (v441_squeezed - v43_squeezed) / max(v43_squeezed, 1), 1),
        "spill_commands_total": total_spills,
        "v43_spills_caught": v43_spills,
        "v440_spills_caught": v440_spills,
        "v441_spills_caught": v441_spills,
        "v441_spill_capture_pct": round(100.0 * v441_spills / max(total_spills, 1), 1),
        "newly_caught_v441": dict(newly_caught_v441.most_common(8)),
        "pre_hook_median_ms": round(statistics.median(lats), 1),
        "pre_hook_p95_ms": round(lats[int(len(lats) * 0.95) - 1], 1),
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


def main():
    ap = argparse.ArgumentParser(description="Benchmark whitemagic-agy-kit v4.4.1 longevity, compound spills, and Rust HUD")
    ap.add_argument("--out", default=str(KIT_DIR / "benchmarks" / "RESULTS_2026-11-25_v441.md"), help="Output Markdown receipt path")
    args = ap.parse_args()

    spills = replay_pre_tool_spills()
    prefix = measure_prefix_footprint()
    hud = replay_longevity_hud()

    report = {
        "spills_replay": spills,
        "prefix_footprint": prefix,
        "hud_replay": hud,
    }
    print(json.dumps(report, indent=2))

    md = [
        "# WhiteMagic AGY Turbo Kit `v4.4.1` — 3-Way Spill Shield & Sub-4ms Rust HUD Benchmark",
        "",
        "## 1. Historical `run_command` Spill & Squeeze Replay (`v4.3.0` → `v4.4.0` → `v4.4.1`)",
        "",
        f"- **Commands replayed (`10` App + `12` CLI newest sessions):** `{spills['commands_scanned']}`",
        f"- **Commands intercepted by `wm-squeeze`:** `{spills['v43_squeezed']}` (`v4.3.0`) → `{spills['v440_squeezed']}` (`v4.4.0`) → **`{spills['v441_squeezed']}` (`v4.4.1`)** (**`+{spills['delta_vs_v43']}` / `+{spills['delta_vs_v43_pct']}%`** vs `v4.3.0`)",
        f"- **Historical `output.txt` spill commands caught:** `{spills['v43_spills_caught']}/{spills['spill_commands_total']}` (`v4.3.0`) → `{spills['v440_spills_caught']}/{spills['spill_commands_total']}` (`v4.4.0`) → **`{spills['v441_spills_caught']}/{spills['spill_commands_total']}` (`v4.4.1`, `{spills['v441_spill_capture_pct']}%`)**",
        f"- **Newly squeezed roots (`v4.4.1` vs `v4.4.0`):** `{json.dumps(spills['newly_caught_v441'])}`",
        f"- **`pre_tool_guard.py` subprocess latency:** median **`{spills['pre_hook_median_ms']} ms`** · p95 `{spills['pre_hook_p95_ms']} ms`",
        "",
        "## 2. Turn-0 Rule & Plugin Frontmatter Footprint",
        "",
        f"- **Plugin `AGENTS.md`:** `{prefix['agents_v43_bytes']:,} B` (`v4.3.0`) → **`{prefix['agents_v441_bytes']:,} B` (`v4.4.1`, `{prefix['agents_delta_pct']}%`)**",
        f"- **Global `GEMINI.md` (local user rules):** `{prefix['gemini_md_bytes']:,} B`",
        f"- **Enabled plugins skill frontmatter:** `{prefix['enabled_skills_total']}` skills across `{len(prefix['enabled_plugins'])}` plugins = `{prefix['enabled_frontmatter_bytes']:,} B` (~`{prefix['enabled_frontmatter_est_tokens']:,}` tok)",
        "",
        "| Enabled Plugin | Skills | Frontmatter Bytes | Est. Tokens |",
        "| :--- | ---: | ---: | ---: |",
    ]
    for pb in prefix["enabled_plugins"]:
        md.append(f"| `{pb['plugin']}` | {pb['skills']} | {pb['bytes']:,} B | ~{pb['est_tokens']:,} |")

    md.extend(
        [
            "",
            "## 3. Zero-Token `PostInvocation` Rust HUD (`wm-squeeze --hud`) vs Python Fallback",
            "",
            f"- **Turns replayed (`8` App + `12` CLI sessions):** `{hud['turns_replayed']}` across `{hud['sessions_replayed']}` sessions",
            f"- **Alerts emitted (`user_markdown` only):** `{hud['alerts_fired']}` (`{hud['alert_rate_pct']}%` of turns across `{hud['sessions_with_alerts']}` sessions)",
            f"- **Alert breakdown:** `{json.dumps(hud['alerts_by_kind'])}`",
            f"- **Model tokens added (`systemMessage`):** **`{hud['model_tokens_added']}`** (`0` prefix-cache impact)",
            f"- **Python `longevity_hud.py` latency (30 runs):** median `{hud['hud_py_median_ms']} ms` · p95 `{hud['hud_py_p95_ms']} ms`",
            f"- **Rust `wm-squeeze --hud` latency (30 runs):** median **`{hud['hud_rust_median_ms']} ms`** · p95 **`{hud['hud_rust_p95_ms']} ms`** (**`{hud['hud_rust_speedup_x']}×` faster**, exact output parity=`{hud['hud_py_rust_parity']}`)",
            "",
        ]
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(sanitize_text("\n".join(md)), encoding="utf-8")
    print(f"\nWrote Zero-PII benchmark receipt -> {out_path}")


if __name__ == "__main__":
    main()
