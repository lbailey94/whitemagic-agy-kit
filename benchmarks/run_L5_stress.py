#!/usr/bin/env python3
"""
L5 Long-Horizon Multi-Phase Stress Benchmark (15–25+ LLM turns per arm in a single continuous session).
Tests a realistic 22-file Python + Rust monorepo across 5 sequential engineering phases using
`agy -p --conversation <id>`:
  Phase 1: Diagnose 3 bugs hidden in an 80 KB raw ANSI CI log + fix across 350L Python modules + pass 450 verbose pytest
  Phase 2: Diagnose & fix 3 Rust bugs across a multi-module crate with 250 noisy `cargo test` cases
  Phase 3: Cross-file feature propagation (`priority: int = 0` across 6 Python caller files + Rust crate) + new tests
  Phase 4: Audit a noisy `git diff` (containing a 50 KB Cargo.lock change + real patch), fix an edge-case regression,
           and re-verify with repeated `cargo check` / `pytest -v` (exercising wm-squeeze v3 content-addressed cache!)
  Phase 5: Final full verification gate (`pytest -v` + `cargo test` + `git status`) & concise summary

Also measures `wm-handoff` CliffCompaction crystal size vs. cold-resume peak context after Phase 5.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
BRAIN = HOME / ".gemini" / "antigravity-cli" / "brain"
LOCAL_BIN = HOME / ".local" / "bin"


def init_git(d: Path):
    subprocess.run(["git", "init", "-q"], cwd=d, check=True, timeout=30)
    subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=d, check=True, timeout=30)
    subprocess.run(["git", "config", "user.name", "Bench"], cwd=d, check=True, timeout=30)
    subprocess.run(["git", "add", "."], cwd=d, check=True, timeout=30)
    subprocess.run(["git", "commit", "-q", "-m", "initial monorepo"], cwd=d, check=True, timeout=30)


def build_monorepo(d: Path):
    # ── 1. Python service (14 files, 350L core files, 6-file call graph) ───
    py = d / "py_service"
    py.mkdir()
    (py / "__init__.py").write_text("")
    for m in range(6):
        (py / f"subsystem_{m}.py").write_text(
            "\n".join(f"def sub{m}_fn_{i}(v: int) -> int:\n    return (v * {(i%4)+1}) + {m}\n" for i in range(95))
        )

    (py / "cache_key.py").write_text(
        "import hashlib, time\n\n"
        + "\n".join(f"def _ck_pad_{i}(s: str) -> str:\n    return s.strip()\n" for i in range(95))
        + "\ndef build_prefix_key(rules: list[str], tools: list[str]) -> str:\n"
        + "    # BUG P1-1: embeds time.time() and leaves tools unsorted\n"
        + "    payload = '|'.join(rules) + '::' + ','.join(tools) + f'::{int(time.time())}'\n"
        + "    return hashlib.sha256(payload.encode()).hexdigest()[:16]\n"
    )
    (py / "chunker.py").write_text(
        "\n".join(f"def _ch_pad_{i}(n: int) -> int:\n    return n + {i}\n" for i in range(95))
        + "\ndef budget_truncate(text: str, max_bytes: int) -> str:\n"
        + "    # BUG P1-2: slices chars instead of UTF-8 bytes and exceeds max_bytes\n"
        + "    if len(text) <= max_bytes:\n"
        + "        return text\n"
        + "    return text[:max_bytes] + '...'\n"
    )
    (py / "pricing.py").write_text(
        "\n".join(f"def _pr_pad_{i}(n: float) -> float:\n    return round(n * 1.0{i%9}, 2)\n" for i in range(95))
        + "\ndef compute_cost(tokens: int, cached: bool = False) -> float:\n"
        + "    # BUG P1-3: cached tokens should get 90% discount (0.1x), not 1.0x\n"
        + "    rate = 1.0 if cached else 1.0\n"
        + "    return round((tokens / 1000.0) * rate, 4)\n"
    )
    # 5 caller files forming a 2-hop call graph over compute_cost
    (py / "checkout.py").write_text(
        "from py_service.pricing import compute_cost\n\n"
        "def quote_turn(uncached: int, cached: int) -> float:\n"
        "    return round(compute_cost(uncached, cached=False) + compute_cost(cached, cached=True), 4)\n"
    )
    (py / "billing.py").write_text(
        "from py_service.pricing import compute_cost\n\n"
        "def bill_batch(uncached: int, cached: int) -> float:\n"
        "    return round(compute_cost(uncached, False) + compute_cost(cached, True), 4)\n"
    )
    (py / "router.py").write_text(
        "from py_service.checkout import quote_turn\nfrom py_service.billing import bill_batch\n\n"
        "def estimate_session(uncached: int, cached: int) -> dict:\n"
        "    return {'turn': quote_turn(uncached, cached), 'batch': bill_batch(uncached, cached)}\n"
    )
    (py / "telemetry.py").write_text(
        "from py_service.router import estimate_session\n\n"
        "def session_report(uncached: int, cached: int) -> dict:\n"
        "    est = estimate_session(uncached, cached)\n"
        "    return {'est_turn': est['turn'], 'est_batch': est['batch']}\n"
    )
    (py / "cli_api.py").write_text(
        "from py_service.telemetry import session_report\n\n"
        "def run_summary(uncached: int, cached: int) -> dict:\n"
        "    return session_report(uncached, cached)\n"
    )

    # 80 KB noisy CI failure log hiding Phase 1's 3 real failure traces
    noise1 = [f"[2026-11-21 15:00:{i%60:02d}] \x1b[32mINFO\x1b[0m ci.py: py_service/test_sub_{i%6}::case_{i:04d} PASSED (0.00{i%9}s)" for i in range(450)]
    err1 = [
        "[2026-11-21 15:02:11] \x1b[31mFAIL\x1b[0m tests/test_py_service.py::test_prefix_key_determinism",
        "AssertionError: build_prefix_key(['r'], ['b','a']) != build_prefix_key(['r'], ['a','b']) or varied across seconds",
    ]
    noise2 = [f"[2026-11-21 15:03:{i%60:02d}] \x1b[32mINFO\x1b[0m ci.py: py_service/test_chunk_{i%5}::case_{i:04d} PASSED (ok)" for i in range(420)]
    err2 = [
        "[2026-11-21 15:05:30] \x1b[31mFAIL\x1b[0m tests/test_py_service.py::test_budget_truncate_utf8",
        "AssertionError: budget_truncate('🚀'*40, 36) returned 147 UTF-8 bytes (> 36 byte cap)",
    ]
    noise3 = [f"[2026-11-21 15:06:{i%60:02d}] \x1b[32mINFO\x1b[0m ci.py: py_service/test_price_{i%4}::case_{i:04d} PASSED (1.1ms)" for i in range(380)]
    err3 = [
        "[2026-11-21 15:08:12] \x1b[31mFAIL\x1b[0m tests/test_py_service.py::test_cached_discount",
        "AssertionError: compute_cost(10000, cached=True) == 10.0, expected 1.0 (90% cache discount)",
    ]
    (d / "ci_failure_dump.log").write_text("\n".join(noise1 + err1 + noise2 + err2 + noise3 + err3) + "\n")

    tests_dir = d / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_py_service.py").write_text(
        """import time
import pytest
from py_service.cache_key import build_prefix_key
from py_service.chunker import budget_truncate
from py_service.pricing import compute_cost
from py_service.cli_api import run_summary

@pytest.mark.parametrize("i", range(450))
def test_bulk_invariants(i):
    assert i >= 0

def test_prefix_key_determinism():
    k1 = build_prefix_key(["r1", "r2"], ["z", "a", "a"])
    k2 = build_prefix_key(["r1", "r2"], ["a", "z"])
    assert k1 == k2, f"tool order/dedup broke key: {k1} vs {k2}"
    time.sleep(1.05)
    k3 = build_prefix_key(["r1", "r2"], ["a", "z"])
    assert k1 == k3, "timestamp broke determinism"

def test_budget_truncate_utf8():
    out = budget_truncate("🚀" * 40, 36)
    assert len(out.encode("utf-8")) <= 36, f"got {len(out.encode('utf-8'))} bytes"
    assert budget_truncate("short", 36) == "short"

def test_cached_discount():
    assert compute_cost(10000, cached=True) == 1.0
    assert compute_cost(10000, cached=False) == 10.0
    s = run_summary(1000, 10000)
    assert s["est_turn"] == 2.0 and s["est_batch"] == 2.0
"""
    )

    # ── 2. Rust crate (6 files, 250 noisy unit tests, 3 bugs) ──────────────
    rs = d / "rs_engine"
    (rs / "src").mkdir(parents=True)
    (rs / "Cargo.toml").write_text('[package]\nname = "rs_engine"\nversion = "0.1.0"\nedition = "2021"\n')
    (rs / "Cargo.lock").write_text("# Auto-generated lockfile\nversion = 3\n\n[[package]]\nname = \"rs_engine\"\nversion = \"0.1.0\"\n")

    (rs / "src" / "prefix.rs").write_text(
        "\n".join(f"pub fn pad_p_{i}(x: i64) -> i64 {{ x + {i} }}" for i in range(90))
        + "\npub fn canonical_header(tools: &[&str]) -> String {\n"
        + "    // BUG P2-1: unsorted and keeps duplicates\n"
        + "    tools.join(\",\")\n"
        + "}\n"
    )
    (rs / "src" / "budget.rs").write_text(
        "\n".join(f"pub fn pad_b_{i}(x: usize) -> usize {{ x + {i} }}" for i in range(90))
        + "\npub fn cap_utf8(s: &str, max_bytes: usize) -> String {\n"
        + "    // BUG P2-2: char count instead of UTF-8 byte cap\n"
        + "    s.chars().take(max_bytes).collect()\n"
        + "}\n"
    )
    (rs / "src" / "ttl.rs").write_text(
        "\n".join(f"pub fn pad_t_{i}(x: u64) -> u64 {{ x * {(i%3)+1} }}" for i in range(90))
        + "\n/// Returns true if inter-turn gap (seconds) is inside the <30s warm window.\n"
        + "pub fn is_warm(gap_secs: u64) -> bool {\n"
        + "    // BUG P2-3: inverted comparison\n"
        + "    gap_secs >= 30\n"
        + "}\n"
    )
    (rs / "src" / "lib.rs").write_text(
        "pub mod prefix;\npub mod budget;\npub mod ttl;\n\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n"
        + "\n".join(f"    #[test] fn noisy_rs_pass_{i:03d}() {{ assert_eq!(prefix::pad_p_0({i}), {i}); }}" for i in range(250))
        + "\n    #[test] fn test_canonical_header() {\n"
        + "        assert_eq!(prefix::canonical_header(&[\"z\", \"a\", \"a\", \"m\"]), \"a,m,z\");\n"
        + "    }\n"
        + "    #[test] fn test_cap_utf8() {\n"
        + "        let c = budget::cap_utf8(&\"🚀\".repeat(15), 10);\n"
        + "        assert!(c.len() <= 10 && c == \"🚀🚀\");\n"
        + "    }\n"
        + "    #[test] fn test_is_warm() {\n"
        + "        assert!(ttl::is_warm(10));\n"
        + "        assert!(!ttl::is_warm(45));\n"
        + "    }\n"
        + "}\n"
    )

    init_git(d)


def inject_phase4_noisy_lockfile(d: Path):
    """Simulate a 50 KB Cargo.lock regeneration + 1 real edge-case requirement before Phase 4."""
    with (d / "rs_engine" / "Cargo.lock").open(encoding="utf-8") as _lf:
        lock = _lf.read()
    extra = "\n".join(
        f'[[package]]\nname = "transitive-dep-{i:04d}"\nversion = "1.{i%10}.0"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "deadbeef{i:08x}cafebabe{i:08x}"'
        for i in range(380)
    )
    (d / "rs_engine" / "Cargo.lock").write_text(lock + "\n" + extra + "\n")


def parse_transcript_full(tp: Path):
    turns = tools = peak = unc_sum = cac_sum = out_sum = spills = tool_bytes = 0
    per_turn_hits = []
    with tp.open(encoding="utf-8", errors="replace") as _tf:
        raw_lines = _tf.read().splitlines()
    for line in raw_lines:
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type", "")
        if t == "PLANNER_RESPONSE":
            unc = int(o.get("input_tokens") or 0)
            cac = int(o.get("cache_read_tokens") or 0)
            out = int(o.get("output_tokens") or 0)
            ctx = unc + cac
            if ctx > 0 or out > 0:
                turns += 1
                unc_sum += unc
                cac_sum += cac
                out_sum += out
                peak = max(peak, ctx)
                if turns > 1 and ctx > 0:
                    per_turn_hits.append(100.0 * cac / ctx)
            tools += len(o.get("tool_calls") or [])
        elif t == "GENERIC":
            c = str(o.get("content") or "")
            tool_bytes += len(c)
            if "output.txt" in c and "truncated" in c.lower():
                spills += 1
    tot_in = unc_sum + cac_sum
    hit_pct = round(100.0 * cac_sum / max(tot_in, 1), 2)
    steady_hit = round(sum(per_turn_hits) / max(len(per_turn_hits), 1), 2) if per_turn_hits else 0.0
    credit = round(unc_sum + 0.1 * cac_sum + 5.0 * out_sum, 1)
    return {
        "turns": turns,
        "tool_calls": tools,
        "peak_ctx": peak,
        "uncached_in": unc_sum,
        "cached_in": cac_sum,
        "out_tokens": out_sum,
        "cache_hit_pct": hit_pct,
        "steady_state_hit_pct": steady_hit,
        "spills": spills,
        "tool_bytes": tool_bytes,
        "credit_index": credit,
    }


def run_stress_arm(arm_name, model_name, vanilla=False):
    with tempfile.TemporaryDirectory(prefix=f"agy_L5_{arm_name}_") as td:
        repo = Path(td)
        build_monorepo(repo)

        prefix_ctl = (
            "IMPORTANT BENCHMARK CONTROL: Use ONLY built-in `run_command`, `read_file`, `edit_file`, `write_file` "
            "(do NOT use `agy-fast`, `wm-codegraph`, `wm-local-digest`, or `wm-squeeze`).\n"
            if vanilla
            else "Use your Wave 4 efficiency tools (`wm-local-digest` for large logs, `wm-codegraph` for call graphs, "
            "`agy-fast` batching/apply_edits+verify, and `PreToolUse` `wm-squeeze`) to stay fast and token-efficient.\n"
        )

        phases = [
            (
                "P1_py_diag_fix",
                prefix_ctl
                + "Phase 1: Diagnose all 3 failing Python tests from `ci_failure_dump.log`, fix the 3 root causes in `py_service/`, "
                "and verify `pytest -v tests/test_py_service.py` passes with 0 failures. Reply in 1 line.",
            ),
            (
                "P2_rust_fix",
                "Phase 2: Now run `cargo test` inside `rs_engine/`, diagnose all 3 failing Rust unit tests across `rs_engine/src/`, "
                "fix all 3 bugs, and verify `cargo test` inside `rs_engine/` passes completely. Reply in 1 line.",
            ),
            (
                "P3_cross_file_feature",
                "Phase 3: Add `priority: int = 0` support to `compute_cost(tokens, cached=False, priority=0)` in `py_service/pricing.py` "
                "(adding `+0.5 * priority` flat surcharge to the cost) AND propagate `priority: int = 0` through ALL 5 callers "
                "(`checkout.py`, `billing.py`, `router.py`, `telemetry.py`, `cli_api.py`, passing `priority=priority` to both `compute_cost` calls in `quote_turn` and `bill_batch`). "
                "Add a test `test_priority_surcharge` in `tests/test_py_service.py` verifying `run_summary(1000, 10000, priority=2)` returns `{'est_turn': 4.0, 'est_batch': 4.0}`, "
                "and run `pytest -v tests/test_py_service.py`. Reply in 1 line.",
            ),
            (
                "P4_lockfile_audit_edgecase",
                "Phase 4: Run `git diff` to inspect current repo changes (including the updated `rs_engine/Cargo.lock`), "
                "and fix one edge case in `py_service/chunker.py`: when `max_bytes <= 3`, `budget_truncate` must NEVER append `'...'` "
                "and must return `<= max_bytes` valid UTF-8 bytes. Add a test for `budget_truncate('🚀hello', 2)` and `budget_truncate('abcd', 3)` "
                "in `tests/test_py_service.py`, then re-run `pytest -v tests/test_py_service.py` and `cargo check` in `rs_engine/`. Reply in 1 line.",
            ),
            (
                "P5_final_gate",
                "Phase 5: Run final verification (`pytest -v tests/test_py_service.py`, `cargo test` in `rs_engine/`, and `git status --short`) "
                "to confirm everything is green with zero regressions. Reply with a 2-line final status.",
            ),
        ]

        env = os.environ.copy()
        if vanilla:
            env["WM_AGY_BENCH_VANILLA"] = "1"
        else:
            env.pop("WM_AGY_BENCH_VANILLA", None)

        conv_id = None
        total_wall = 0.0
        phase_walls = []

        for pname, prompt in phases:
            if pname == "P4_lockfile_audit_edgecase":
                inject_phase4_noisy_lockfile(repo)
            cmd = [
                str(LOCAL_BIN / "agy"),
                "-p", prompt,
                "--model", model_name,
                "--dangerously-skip-permissions",
                "--output-format", "json",
            ]
            if conv_id:
                cmd.extend(["--conversation", conv_id])
            t0 = time.perf_counter()
            p = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=300)
            w = round(time.perf_counter() - t0, 2)
            total_wall += w
            phase_walls.append((pname, w, p.returncode))
            try:
                j = json.loads(p.stdout)
                if not conv_id and j.get("conversation_id"):
                    conv_id = j["conversation_id"]
            except Exception:
                pass
            print(f"    [{arm_name}] {pname}: {w}s (rc={p.returncode}, conv={str(conv_id)[:8]})", flush=True)

        # Ground-truth external verification (pytest + cargo test + explicit Phase 3 & Phase 4 assertions!)
        gt_py = subprocess.run(["python3", "-m", "pytest", "-q", "tests/test_py_service.py"], cwd=repo, capture_output=True, text=True, timeout=60)
        gt_rs = subprocess.run(["cargo", "test", "--quiet"], cwd=repo / "rs_engine", capture_output=True, text=True, timeout=120)
        gt_spec = subprocess.run(
            [
                "python3", "-c",
                "from py_service.cli_api import run_summary; from py_service.chunker import budget_truncate; "
                "r = run_summary(1000, 10000, priority=2); assert r == {'est_turn': 4.0, 'est_batch': 4.0}, r; "
                "a = budget_truncate('🚀hello', 2); assert len(a.encode()) <= 2, a; "
                "b = budget_truncate('abcd', 3); assert len(b.encode()) <= 3, b; print('SPEC_OK')"
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=15,
        )
        passed = (gt_py.returncode == 0) and (gt_rs.returncode == 0) and ("SPEC_OK" in gt_spec.stdout)

        tp = BRAIN / conv_id / ".system_generated" / "logs" / "transcript.jsonl" if conv_id else None
        metrics = parse_transcript_full(tp) if (tp and tp.is_file()) else {}

        # Measure wm-handoff CliffCompaction crystal size vs cold-resume peak context
        handoff_tok = 0
        if conv_id and (LOCAL_BIN / "wm-handoff").exists():
            hp = subprocess.run([str(LOCAL_BIN / "wm-handoff"), "--conv", conv_id[:10], "--cwd", str(repo)], capture_output=True, text=True, timeout=15)
            handoff_tok = len(hp.stdout.encode("utf-8")) // 4

        return {
            "arm": arm_name,
            "model": model_name,
            "conv_id": str(conv_id)[:10] if conv_id else "",
            "passed": passed,
            "gt_detail": {"py": gt_py.returncode == 0, "rs": gt_rs.returncode == 0, "spec": "SPEC_OK" in gt_spec.stdout},
            "wall_s": round(total_wall, 2),
            "phase_walls": phase_walls,
            "handoff_crystal_tokens": handoff_tok,
            **metrics,
        }


def main():
    ap = argparse.ArgumentParser(description="L5 15+ Turn Multi-Phase Stress Benchmark")
    ap.add_argument("--out-json", default=str(Path(__file__).resolve().parent / "track2_L5_stress.json"))
    args = ap.parse_args()

    print("=== L5 LONG-HORIZON 5-PHASE STRESS BENCHMARK (15-25+ TURNS / ARM) ===", flush=True)
    arms = [
        ("vanilla_neon", "Gemini 4 Neon (High)", True),
        ("wave4_neon", "Gemini 4 Neon (High)", False),
        ("wave4_hydrogen", "Gemini 4 Hydrogen (High)", False),
    ]
    rows = []
    base_ci = None
    for aname, mdl, is_van in arms:
        print(f"\nRunning {aname} ({mdl})...", flush=True)
        res = run_stress_arm(aname, mdl, vanilla=is_van)
        if is_van and res.get("credit_index", 0) > 0:
            base_ci = res["credit_index"]
        if base_ci:
            res["credit_delta_pct"] = round(100.0 * (res["credit_index"] - base_ci) / base_ci, 1)
        rows.append(res)
        print(
            f"  SUMMARY {aname}: pass={res['passed']} ({res['gt_detail']}) wall={res['wall_s']}s "
            f"turns={res.get('turns')} tools={res.get('tool_calls')} peak={res.get('peak_ctx',0)//1000}k "
            f"unc={res.get('uncached_in',0)//1000}k hit={res.get('cache_hit_pct')}% (steady={res.get('steady_state_hit_pct')}%) "
            f"out={res.get('out_tokens',0)//1000}k spills={res.get('spills')} toolKB={res.get('tool_bytes',0)//1024}KB "
            f"credit={res.get('credit_index',0)/1000:.1f}k ({res.get('credit_delta_pct', 0.0):+.1f}%) "
            f"handoff={res.get('handoff_crystal_tokens')}tok",
            flush=True,
        )

    out = {"ts": datetime.now(timezone.utc).isoformat(), "benchmark": "L5_stress_5phase_monorepo", "arms": rows}
    Path(args.out_json).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nSaved L5 stress results -> {args.out_json}")


if __name__ == "__main__":
    main()
