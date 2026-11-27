#!/usr/bin/env python3
"""Live Empirical Benchmark Suite for `cursor-fleet` v2 Wave Primitives:
  1. `map --reduce` (Estate portability & failure-mode sweep across 12 real CLIs + automatic reduce)
  2. `race` (Best-of-3 bwrap-confined worktree optimization tournament with --score-regex + --apply-winner)
  3. `council` (Blind 3-model-family security/architecture review + consensus judge)
  4. `pipeline` (3-stage verifiable synthetic secure-code factory: map -> deterministic exec gate -> reduce)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FLEET_CLI = str(REPO_ROOT / "bin" / "wm-grok-fleet")
OUT_ROOT = REPO_ROOT / "benchmarks" / "cursor_fleet_v2_run"
OUT_ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH = OUT_ROOT / "bench_fleet.db"
ART_DIR = OUT_ROOT / "artifacts"


def run_cli(args: list[str], cwd: Path | None = None, timeout: int = 420) -> tuple[int, str, str, float]:
    t0 = time.monotonic()
    p = subprocess.run(
        [sys.executable, "-B", FLEET_CLI] + args,
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return p.returncode, p.stdout, p.stderr, round(time.monotonic() - t0, 2)


def bench_1_map_reduce() -> dict[str, Any]:
    print("\n=== BENCH 1: map --reduce (12 real CLI scripts, N=4 + auto reduce) ===", flush=True)
    # Pick 12 compact Python/shell tools in bin/ (exclude huge >80KB scripts so N=4 stays crisp)
    candidates = sorted(
        p for p in (REPO_ROOT / "bin").iterdir()
        if p.is_file() and p.stat().st_size <= 55000
    )[:12]
    items_jsonl = OUT_ROOT / "b1_scripts.jsonl"
    lines = []
    for p in candidates:
        rel = str(p.relative_to(REPO_ROOT))
        raw = p.read_text(encoding="utf-8", errors="replace")
        snippet = raw[:12000] + (f"\n... [truncated {len(raw)} chars]" if len(raw) > 12000 else "")
        lines.append(json.dumps({"id": rel, "bytes": p.stat().st_size, "code": snippet}, ensure_ascii=False))
    items_jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

    out_json = OUT_ROOT / "b1_map_reduce.json"
    out_md = OUT_ROOT / "b1_map_reduce.md"
    red_txt = OUT_ROOT / "b1_reduced.md"

    rc, out, err, wall_s = run_cli(
        [
            "map",
            "--campaign", "bench2-b1-map-reduce",
            "--preset", "synthesis",
            "--items-jsonl", str(items_jsonl),
            "--batch-size", "4",
            "--max-usd", "1.00",
            "--parse-json",
            "--instruction",
            (
                "Audit each CLI script for CONCRETE reliability or cross-platform (macOS + Linux) issues: "
                "missing subprocess timeouts, Linux-only /proc assumptions, unclosed handles/locks, silent except:pass masking errors, or unsafe temp files. "
                "Return ONLY a JSON array: [{\"file\": \"...\", \"severity\": \"P0|P1|P2\", \"issue\": \"...\", \"line_hint\": \"...\", \"fix\": \"...\"}]. "
                "Include only concrete, code-evidenced findings."
            ),
            "--reduce-instruction",
            (
                "Deduplicate and rank ALL concrete findings from the 3 audit batches into a crisp Markdown executive report "
                "with sections: Top P0/P1 Risks (table with File, Issue, Fix), Quick Wins (P2), and Clean/Low-Risk Scripts. "
                "Keep it <= 60 lines and strictly faithful to the batch outputs."
            ),
            "--reduce-out", str(red_txt),
            "--out", str(out_json),
            "--report-md", str(out_md),
            "--db-path", str(DB_PATH),
            "--artifact-dir", str(ART_DIR),
        ]
    )
    res = json.loads(out) if out.strip().startswith("{") else {"ok": False, "raw": out[:400], "err": err[:400]}
    summary = res.get("summary") or {}
    speedup = round(float(summary.get("wall_s_sum") or 0) / max(wall_s, 0.01), 2)
    return {
        "bench": "b1_map_reduce",
        "ok": rc == 0 and bool(res.get("ok")),
        "files_scanned": len(candidates),
        "wall_s": wall_s,
        "wall_s_sum": summary.get("wall_s_sum"),
        "speedup_x": speedup,
        "counts": summary.get("counts"),
        "total_tool_calls": summary.get("total_tool_calls"),
        "tokens": summary.get("tokens"),
        "est_actual_usd": summary.get("est_actual_usd"),
        "est_cursor_quota_pct": summary.get("est_cursor_quota_pct"),
        "reduce_levels": (res.get("reduced") or {}).get("levels"),
        "reduce_ok": (res.get("reduced") or {}).get("ok"),
        "out_json": str(out_json),
        "reduce_out": str(red_txt),
    }


def _setup_race_repo(repo: Path) -> float:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "bench@whitemagic.dev"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "WM Bench"], cwd=repo, check=True)

    (repo / "analyzer.py").write_text(
        '''"""Sliding-window log-event deduplication and burst anomaly detector.

Given a list of events `[{"ts": int, "key": str, "weight": int}, ...]` (NOT guaranteed sorted by ts),
implement `analyze_events(events, window_sec=60, burst_threshold=5)` returning:
  {
    "unique_keys": int,                    # number of distinct keys
    "burst_keys": list[str],               # sorted distinct keys that ever reach sum(weight) >= burst_threshold
                                           # within any inclusive sliding window [t - window_sec, t]
    "peak_window_weight": int,             # max sum(weight) for any single key within any inclusive window [t - window_sec, t]
    "dedup_events": int,                   # total count after dropping exact (ts, key, weight) duplicates
  }
"""

from __future__ import annotations


def analyze_events(events: list[dict], window_sec: int = 60, burst_threshold: int = 5) -> dict:
    if not events:
        return {
            "unique_keys": 0,
            "burst_keys": [],
            "peak_window_weight": 0,
            "dedup_events": 0,
        }

    # Intentionally naive O(N^2) baseline implementation (duplicate scan + pairwise window scan)
    dedup: list[dict] = []
    for ev in events:
        tup = (ev["ts"], ev["key"], ev["weight"])
        found = False
        for prev in dedup:
            if (prev["ts"], prev["key"], prev["weight"]) == tup:
                found = True
                break
        if not found:
            dedup.append(ev)

    keys = sorted({ev["key"] for ev in dedup})
    burst_keys: list[str] = []
    peak = 0

    for k in keys:
        k_events = [ev for ev in dedup if ev["key"] == k]
        k_peak = 0
        for e1 in k_events:
            t = e1["ts"]
            w_sum = 0
            for e2 in k_events:
                if t - window_sec <= e2["ts"] <= t:
                    w_sum += e2["weight"]
            if w_sum > k_peak:
                k_peak = w_sum
        if k_peak > peak:
            peak = k_peak
        if k_peak >= burst_threshold:
            burst_keys.append(k)

    return {
        "unique_keys": len(keys),
        "burst_keys": burst_keys,
        "peak_window_weight": peak,
        "dedup_events": len(dedup),
    }
''',
        encoding="utf-8",
    )

    (repo / "verify_bench.py").write_text(
        '''#!/usr/bin/env python3
import random
import statistics
import time
from analyzer import analyze_events

# 1. Exact edge-case correctness suite
assert analyze_events([]) == {
    "unique_keys": 0, "burst_keys": [], "peak_window_weight": 0, "dedup_events": 0
}
# Exact duplicates dropped before windowing
r1 = analyze_events([
    {"ts": 10, "key": "a", "weight": 3},
    {"ts": 10, "key": "a", "weight": 3},
    {"ts": 70, "key": "a", "weight": 2},  # inclusive boundary 70 - 60 == 10
], window_sec=60, burst_threshold=5)
assert r1 == {"unique_keys": 1, "burst_keys": ["a"], "peak_window_weight": 5, "dedup_events": 2}, r1

# Boundary outside window (71 - 60 = 11 > 10)
r2 = analyze_events([
    {"ts": 10, "key": "a", "weight": 3},
    {"ts": 71, "key": "a", "weight": 2},
], window_sec=60, burst_threshold=5)
assert r2 == {"unique_keys": 1, "burst_keys": [], "peak_window_weight": 3, "dedup_events": 2}, r2

# Unsorted timestamps + same-ts different weights + negative/zero weights + unicode keys
r3 = analyze_events([
    {"ts": 200, "key": "β", "weight": 4},
    {"ts": 150, "key": "β", "weight": 2},
    {"ts": 150, "key": "β", "weight": 3},
    {"ts": 100, "key": "α", "weight": 5},
    {"ts": 161, "key": "α", "weight": 1},
    {"ts": 100, "key": "γ", "weight": 0},
], window_sec=60, burst_threshold=5)
assert r3 == {"unique_keys": 3, "burst_keys": ["α", "β"], "peak_window_weight": 9, "dedup_events": 6}, r3

# Cross-check against reference O(N^2) on randomized tricky dataset (N=250)
rng = random.Random(42)
small_events = [
    {"ts": rng.randint(0, 400), "key": f"k{rng.randint(0, 12)}", "weight": rng.randint(1, 4)}
    for _ in range(250)
]
# Duplicate 20 entries
small_events.extend(small_events[:20])
rng.shuffle(small_events)

def _ref(evs, w=60, th=12):
    seen = set()
    dd = []
    for e in evs:
        t = (e["ts"], e["key"], e["weight"])
        if t not in seen:
            seen.add(t)
            dd.append(e)
    ks = sorted({e["key"] for e in dd})
    bk, pk = [], 0
    for k in ks:
        ke = [e for e in dd if e["key"] == k]
        kp = max((sum(e2["weight"] for e2 in ke if e1["ts"] - w <= e2["ts"] <= e1["ts"]) for e1 in ke), default=0)
        pk = max(pk, kp)
        if kp >= th:
            bk.append(k)
    return {"unique_keys": len(ks), "burst_keys": bk, "peak_window_weight": pk, "dedup_events": len(dd)}

assert analyze_events(small_events, window_sec=60, burst_threshold=12) == _ref(small_events, 60, 12)

# 2. Deterministic performance benchmark (N=4000 events, 40 keys, ~10% dupes)
rng2 = random.Random(1337)
big_events = [
    {"ts": rng2.randint(0, 5000), "key": f"svc-{rng2.randint(0, 39)}", "weight": rng2.randint(1, 5)}
    for _ in range(3600)
]
big_events.extend(big_events[:400])
rng2.shuffle(big_events)

# Expected golden answer for N=4000
times_ms = []
out = None
for _ in range(5):
    t0 = time.perf_counter()
    out = analyze_events(big_events, window_sec=60, burst_threshold=25)
    times_ms.append((time.perf_counter() - t0) * 1000.0)

assert out["unique_keys"] == 40
assert out["dedup_events"] <= 4000 and out["dedup_events"] >= 3500
assert out["peak_window_weight"] >= 10
med = round(statistics.median(times_ms), 3)
print(f"BENCH_PASS median={med}ms unique={out['unique_keys']} dedup={out['dedup_events']} peak={out['peak_window_weight']} bursts={len(out['burst_keys'])}")
''',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline O(N^2) analyzer + verify_bench"], cwd=repo, check=True)

    # Measure baseline median
    bp = subprocess.run([sys.executable, "verify_bench.py"], cwd=repo, capture_output=True, text=True, check=True)
    m = re.search(r"median=([0-9.]+)ms", bp.stdout)
    return float(m.group(1)) if m else 0.0


def bench_2_race() -> dict[str, Any]:
    print("\n=== BENCH 2: race (Best-of-3 bwrap-confined worktree optimization tournament) ===", flush=True)
    repo = OUT_ROOT / "b2_race_repo"
    if repo.exists():
        import shutil
        shutil.rmtree(repo)
    baseline_ms = _setup_race_repo(repo)
    print(f"  Baseline O(N^2) median = {baseline_ms}ms", flush=True)

    out_json = OUT_ROOT / "b2_race_v2.json"
    out_md = OUT_ROOT / "b2_race_v2.md"
    rc, out, err, wall_s = run_cli(
        [
            "race",
            "--campaign", "bench2-b2-race-v2",
            "--cwd", str(repo),
            "--models", "grok-4.7-medium,composer-2.5,cursor-grok-4.6-high",
            "--confined",
            "--verify-cmd", "python3 verify_bench.py",
            "--score-regex", r"median=([0-9.]+)ms",
            "--score-goal", "min",
            "--apply-winner",
            "--max-usd", "1.50",
            "--prompt",
            (
                "Optimize `analyze_events` in `analyzer.py` (Python stdlib only) to minimize `median=...ms` in `verify_bench.py` "
                "while preserving 100% exact semantics (exact (ts, key, weight) dedup before windowing, "
                "inclusive [t - window_sec, t] sliding window, unsorted input, duplicate timestamps with different weights, and sorted burst_keys). "
                "Edit `analyzer.py` directly (e.g., set dedup + per-key sorted timestamps + two-pointer sliding window) and do NOT modify `verify_bench.py`."
            ),
            "--out", str(out_json),
            "--report-md", str(out_md),
            "--db-path", str(DB_PATH),
            "--artifact-dir", str(ART_DIR),
        ]
    )
    res = json.loads(out) if out.strip().startswith("{") else {"ok": False, "raw": out[:400], "err": err[:400]}
    summary = res.get("summary") or {}
    winner = res.get("winner") or {}
    win_ms = winner.get("score")
    opt_speedup = round(baseline_ms / win_ms, 2) if (baseline_ms and win_ms and win_ms > 0) else None
    post_apply_ok = False
    if res.get("applied_winner"):
        chk = subprocess.run([sys.executable, "verify_bench.py"], cwd=repo, capture_output=True, text=True)
        post_apply_ok = chk.returncode == 0
    return {
        "bench": "b2_race",
        "ok": rc == 0 and bool(res.get("ok")),
        "baseline_ms": baseline_ms,
        "winner_id": winner.get("id"),
        "winner_ms": win_ms,
        "algo_speedup_x": opt_speedup,
        "passed_candidates": (res.get("ranking") or {}).get("passed_count"),
        "total_candidates": (res.get("ranking") or {}).get("total_candidates"),
        "applied_winner": res.get("applied_winner"),
        "post_apply_verify_ok": post_apply_ok,
        "wall_s": wall_s,
        "wall_s_sum": summary.get("wall_s_sum"),
        "fleet_speedup_x": round(float(summary.get("wall_s_sum") or 0) / max(wall_s, 0.01), 2),
        "est_actual_usd": summary.get("est_actual_usd"),
        "est_cursor_quota_pct": summary.get("est_cursor_quota_pct"),
        "leaderboard": (res.get("ranking") or {}).get("leaderboard"),
        "report_md": str(out_md),
    }


def bench_3_council() -> dict[str, Any]:
    print("\n=== BENCH 3: council (Blind 3-Model-Family Review: Grok + Composer + Codex + Judge) ===", flush=True)
    # Extract a focused ~14KB slice of the new race + pipeline security/execution logic in bin/wm-grok-fleet
    fleet_lines = (REPO_ROOT / "bin" / "wm-grok-fleet").read_text(encoding="utf-8").splitlines()
    excerpt_p = OUT_ROOT / "b3_fleet_excerpt.py"
    # Extract verify_cmd branch + rank_race_candidates + cmd_pipeline filter/command execution
    slice_text = "\n".join(
        ["# Excerpt A: worktree verify_cmd execution (L1120-L1200)"]
        + fleet_lines[1119:1200]
        + ["", "# Excerpt B: cmd_pipeline filter & stage orchestration (L3512-L3770)"]
        + fleet_lines[3511:3770]
    )
    excerpt_p.write_text(slice_text, encoding="utf-8")

    out_json = OUT_ROOT / "b3_council_v3.json"
    out_md = OUT_ROOT / "b3_council_v3.md"
    rc, out, err, wall_s = run_cli(
        [
            "council",
            "--campaign", "bench2-b3-council-v3",
            "--cwd", str(OUT_ROOT),
            "--files", "b3_fleet_excerpt.py",
            "--models", "grok-4.7-medium,composer-2.5,cursor-grok-4.6-high",
            "--judge-model", "grok-4.7-high",
            "--min-panelists", "2",
            "--preset", "synthesis",
            "--max-usd", "1.50",
            "--prompt",
            (
                "Perform a rigorous security and reliability review of this Python fleet orchestrator excerpt "
                "(worktree verify_cmd execution and declarative pipeline filter/command orchestration). "
                "Focus on command/template handling, path boundaries, exception/timeout handling, "
                "partial-failure resume hazards, and cross-platform edge cases. "
                "Return up to 6 concise numbered findings with Severity (P0/P1/P2), exact symbol/line context, and concrete fix."
            ),
            "--out", str(out_json),
            "--report-md", str(out_md),
            "--db-path", str(DB_PATH),
            "--artifact-dir", str(ART_DIR),
        ]
    )
    res = json.loads(out) if out.strip().startswith("{") else {"ok": False, "raw": out[:400], "err": err[:400]}
    summary = res.get("summary") or {}
    return {
        "bench": "b3_council",
        "ok": rc == 0 and bool(res.get("ok")),
        "panel_done": res.get("panel_done"),
        "panel_total": res.get("panel_total"),
        "judge_ok": res.get("judge_ok"),
        "wall_s": wall_s,
        "wall_s_sum": summary.get("wall_s_sum"),
        "speedup_x": round(float(summary.get("wall_s_sum") or 0) / max(wall_s, 0.01), 2),
        "total_tool_calls": summary.get("total_tool_calls"),
        "est_actual_usd": summary.get("est_actual_usd"),
        "est_cursor_quota_pct": summary.get("est_cursor_quota_pct"),
        "out_json": str(out_json),
        "report_md": str(out_md),
    }


def bench_4_pipeline() -> dict[str, Any]:
    print("\n=== BENCH 4: pipeline (3-stage verifiable synthetic defensive regression-test factory) ===", flush=True)
    seeds_p = OUT_ROOT / "b4_seeds.jsonl"
    seeds = [
        {"id": "reg-01", "topic": "safe-path-containment", "hint": "check Path.is_relative_to instead of naive str.startswith('/safe/dir') which falsely accepts '/safe/dir_other/file'"},
        {"id": "reg-02", "topic": "archive-member-validation", "hint": "validate member paths before extraction so parent-dir segments '..' are rejected"},
        {"id": "reg-03", "topic": "parameterized-sqlite-query", "hint": "use bound '?' parameters in sqlite3 lookup instead of string interpolation"},
        {"id": "reg-04", "topic": "strict-hmac-alg-check", "hint": "require exact 'HS256' algorithm and hmac.compare_digest in a simple token verifier"},
        {"id": "reg-05", "topic": "ipv6-mapped-loopback-filter", "hint": "normalize IPv4-mapped IPv6 addresses via ipaddress.ip_address().ipv4_mapped in an internal-host filter"},
        {"id": "reg-06", "topic": "http-header-line-validator", "hint": "reject CR/LF characters when formatting HTTP header values"},
        {"id": "reg-07", "topic": "relative-redirect-normalizer", "hint": "ensure next-URL validator accepts '/dashboard' but rejects protocol-relative '//other.example' or backslash prefixes"},
        {"id": "reg-08", "topic": "deterministic-rate-bucket", "hint": "fix token-bucket refill math so negative elapsed clock skew never adds negative tokens or exceeds burst cap"},
    ]
    seeds_p.write_text("\n".join(json.dumps(s) for s in seeds) + "\n", encoding="utf-8")

    # Deterministic local Python verifier script for Stage 2 filter gate
    verifier_py = OUT_ROOT / "b4_exec_gate.py"
    verifier_py.write_text(
        '''#!/usr/bin/env python3
import json, pathlib, subprocess, sys, tempfile

inp, outp = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
kept = []
total = 0
for raw in inp.read_text(encoding="utf-8").splitlines():
    if not raw.strip():
        continue
    total += 1
    it = json.loads(raw)
    if not isinstance(it, dict) or not all(isinstance(it.get(k), str) and it[k].strip() for k in ("buggy_code", "fixed_code", "test_code")):
        continue
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td)
        bug_f = p / "run_buggy.py"
        fix_f = p / "run_fixed.py"
        bug_f.write_text(it["buggy_code"] + "\\n\\n" + it["test_code"] + "\\n", encoding="utf-8")
        fix_f.write_text(it["fixed_code"] + "\\n\\n" + it["test_code"] + "\\n", encoding="utf-8")
        try:
            r_bug = subprocess.run([sys.executable, str(bug_f)], cwd=td, capture_output=True, text=True, timeout=6)
            r_fix = subprocess.run([sys.executable, str(fix_f)], cwd=td, capture_output=True, text=True, timeout=6)
        except subprocess.TimeoutExpired:
            continue
        if r_bug.returncode != 0 and r_fix.returncode == 0:
            it["exec_verified"] = True
            it["buggy_rc"] = r_bug.returncode
            it["fixed_rc"] = r_fix.returncode
            kept.append(it)

outp.write_text("\\n".join(json.dumps(x, ensure_ascii=False) for x in kept) + ("\\n" if kept else ""), encoding="utf-8")
print(f"EXEC_GATE admitted={len(kept)}/{total}")
''',
        encoding="utf-8",
    )

    spec_p = OUT_ROOT / "b4_pipeline_spec.json"
    import shlex
    spec = {
        "id": "bench2-b4-synth-pipe-v2",
        "cwd": str(OUT_ROOT),
        "max_usd": 1.50,
        "stages": [
            {
                "name": "generate",
                "type": "map",
                "preset": "synthesis",
                "model": "medium",
                "batch_size": 4,
                "items_jsonl": str(seeds_p),
                "parse_json": True,
                "instruction": (
                    "For EVERY seed item below, write ONE clean, self-contained Python 3.12 stdlib-only defensive regression test case "
                    "(for a software quality benchmark). Return ONLY a valid JSON array with one object per seed: "
                    "[{\"id\": \"<seed id>\", \"topic\": \"...\", \"buggy_code\": \"<short Python function with the subtle validation bug>\", "
                    "\"fixed_code\": \"<corrected Python function with the exact same signature>\", "
                    "\"test_code\": \"<plain assert statements checking both normal valid inputs and the tricky boundary case; must raise AssertionError on buggy_code and pass with exit 0 on fixed_code>\"}]. "
                    "Pure Python stdlib only, no external files or network calls."
                ),
            },
            {
                "name": "exec-verify",
                "type": "filter",
                "command": f"python3 {shlex.quote(str(verifier_py))} {{in_jsonl}} {{out_jsonl}}",
            },
            {
                "name": "summarize",
                "type": "map",
                "preset": "synthesis",
                "model": "medium",
                "from_prev": True,
                "batch_size": 8,
                "parse_json": False,
                "instruction": "Briefly score each execution-verified defensive regression test (1-5 realism, 1-5 edge-case rigor) in a compact Markdown table.",
                "reduce_instruction": "Output the final compact Markdown quality table and 3-bullet summary of the execution-verified benchmark dataset.",
            },
        ],
    }
    spec_p.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    out_json = OUT_ROOT / "b4_pipeline_out.json"
    rc, out, err, wall_s = run_cli(
        [
            "pipeline", str(spec_p),
            "--out", str(out_json),
            "--db-path", str(DB_PATH),
            "--artifact-dir", str(ART_DIR),
        ]
    )
    res = json.loads(out) if out.strip().startswith("{") else {"ok": False, "raw": out[:400], "err": err[:400]}
    stages = res.get("stages") or []
    gen_n = stages[0].get("items_emitted", 0) if len(stages) >= 1 else 0
    adm_n = stages[1].get("kept_items", 0) if len(stages) >= 2 else 0
    tot_usd = float(res.get("total_est_usd") or 0.0)
    return {
        "bench": "b4_pipeline",
        "ok": rc == 0 and bool(res.get("ok")),
        "wall_s": wall_s,
        "seeds": len(seeds),
        "generated_items": gen_n,
        "exec_admitted": adm_n,
        "admission_rate_pct": round(100.0 * adm_n / max(len(seeds), 1), 1),
        "usd_per_admitted": round(tot_usd / max(adm_n, 1), 4) if adm_n else None,
        "total_est_usd": tot_usd,
        "est_cursor_quota_pct": res.get("est_cursor_quota_pct"),
        "stages": stages,
        "out_json": str(out_json),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="b1,b2,b3,b4", help="Comma-separated benches to run (e.g. b2,b3,b4)")
    ns = ap.parse_args()
    selected = {x.strip().lower() for x in ns.only.split(",") if x.strip()}

    prev_map: dict[str, dict[str, Any]] = {}
    final_p = OUT_ROOT / "benchmark_summary.json"
    if final_p.is_file():
        try:
            for item in json.loads(final_p.read_text(encoding="utf-8")).get("benchmarks") or []:
                prev_map[item.get("bench", "")] = item
        except Exception:
            pass

    t0 = time.monotonic()
    runners = [
        ("b1", "b1_map_reduce", bench_1_map_reduce),
        ("b2", "b2_race", bench_2_race),
        ("b3", "b3_council", bench_3_council),
        ("b4", "b4_pipeline", bench_4_pipeline),
    ]
    results = []
    for key, bname, fn in runners:
        if key not in selected and bname in prev_map:
            results.append(prev_map[bname])
            print(f"  -> {bname}: reused prior ok={prev_map[bname].get('ok')}", flush=True)
            continue
        try:
            r = fn()
        except Exception as exc:
            r = {"bench": bname, "ok": False, "error": str(exc)}
        results.append(r)
        print(f"  -> {r['bench']}: ok={r.get('ok')} wall={r.get('wall_s')}s usd={r.get('est_actual_usd', r.get('total_est_usd'))}", flush=True)

    tot_usd = round(sum(float(r.get("est_actual_usd") or r.get("total_est_usd") or 0.0) for r in results), 4)
    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_wall_s": round(time.monotonic() - t0, 2),
        "total_est_usd": tot_usd,
        "total_est_quota_pct": round(tot_usd / 0.96, 2),
        "all_ok": all(r.get("ok") for r in results),
        "benchmarks": results,
    }
    final_p.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n=== ALL 4 BENCHMARKS COMPLETE ===", flush=True)
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
