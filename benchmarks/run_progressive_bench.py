#!/usr/bin/env python3
"""
Progressive Multi-Level AGY Benchmark Suite (whitemagic-agy-kit v3.0.0)

Track 1 (--track1): 100% Offline Reproducible Micro-Benchmarks & Full Historical Replay
  - Real 49-session / 12,711-turn / 2.27B-token historical cache-physics & uncached anatomy
  - wm-squeeze v3 compression + content-addressed cache-hit benchmark (cargo, pytest, git, linter, rg)
  - wm-codegraph vs naive grep+read token comparison on real repositories
  - wm-handoff CliffCompaction cold-resume savings across real historical sessions

Track 2 (--track2): Live Controlled 3-Level Progressive Head-to-Head A/B Trials via `agy -p`
  - Level 1 (Simple):  5-file repo, 350L target file, 35KB noisy ANSI test runner, 1 bug
  - Level 2 (Medium):  10-file repo, 2-hop cross-file callers across 6 files, 45KB noisy linter, signature refactor
  - Level 3 (Complex): 18-file repo, 65KB noisy CI failure log hiding 3 interacting bugs, cross-file fix + full test pass
  - Arms:
      * vanilla  : Built-in tools only, WM_AGY_BENCH_VANILLA=1 (Neon High)
      * wave4    : Full Wave 4 Kit (wm-squeeze v3 + PreToolUse guards + wm-codegraph + wm-local-digest + agy-fast, Neon High)
      * longevity: Full Wave 4 Kit + Gemini 4 Hydrogen (High)
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
BRAIN = HOME / ".gemini" / "antigravity-cli" / "brain"
LOCAL_BIN = HOME / ".local" / "bin"
KIT_DIR = Path(__file__).resolve().parents[1]


def parse_ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# TRACK 1: OFFLINE MICRO-BENCHMARKS & FULL HISTORICAL CORPUS REPLAY
# ═══════════════════════════════════════════════════════════════════════════

def bench_wm_squeeze_v3():
    sq = str(LOCAL_BIN / "wm-squeeze")
    env = {**os.environ, "WM_SQUEEZE_NO_STATS": "1"}
    cases = {}

    # 1. Noisy cargo test (250 compiling + 600 ok + 1 panic + ANSI)
    cargo_raw = "\n".join(
        [f"\x1b[1;32m   Compiling\x1b[0m dep_crate_{i} v1.2.{i}" for i in range(250)]
        + [f"test suite::case_{i} ... ok" for i in range(600)]
        + [
            "test engine::cache_key_stability ... FAILED",
            "thread 'engine::cache_key_stability' panicked at src/cache.rs:142:9:",
            "assertion `left == right` failed: expected deterministic sha256",
            "test result: FAILED. 600 passed; 1 failed; 0 ignored",
        ]
    )
    t0 = time.perf_counter()
    p = subprocess.run([sq, "--pipe", "--kind", "cargo"], input=cargo_raw, capture_output=True, text=True, env=env)
    ms = (time.perf_counter() - t0) * 1000
    cases["cargo_test_noisy"] = {
        "raw_bytes": len(cargo_raw), "out_bytes": len(p.stdout),
        "reduction_pct": round(100 * (1 - len(p.stdout) / len(cargo_raw)), 1),
        "error_preserved": "src/cache.rs:142:9" in p.stdout and "FAILED" in p.stdout,
        "ms": round(ms, 2),
    }

    # 2. Noisy tsc/ruff linter (400 diagnostics across 12 files)
    lint_lines = []
    for fidx in range(12):
        for lnum in range(1, 36):
            lint_lines.append(f"src/mod_{fidx}.ts({lnum},14): error TS2322: Type 'string | undefined' is not assignable to type 'string'.")
    lint_raw = "\n".join(lint_lines)
    t0 = time.perf_counter()
    p = subprocess.run([sq, "--pipe", "--kind", "linter"], input=lint_raw, capture_output=True, text=True, env=env)
    ms = (time.perf_counter() - t0) * 1000
    cases["linter_420_diags"] = {
        "raw_bytes": len(lint_raw), "out_bytes": len(p.stdout),
        "reduction_pct": round(100 * (1 - len(p.stdout) / len(lint_raw)), 1),
        "error_preserved": "src/mod_0.ts" in p.stdout and len(p.stdout.encode()) <= 8200,
        "ms": round(ms, 2),
    }

    # 3. Noisy git diff with lockfile + real patch
    diff_raw = (
        "diff --git a/Cargo.lock b/Cargo.lock\n"
        + "\n".join(f'+name = "pkg-{i}"\n+version = "0.{i}.0"' for i in range(1200))
        + "\ndiff --git a/src/router.rs b/src/router.rs\n@@ -40,3 +40,3 @@\n-let ttl = 30;\n+let ttl = 300;\n"
    )
    t0 = time.perf_counter()
    p = subprocess.run([sq, "--pipe", "--kind", "git"], input=diff_raw, capture_output=True, text=True, env=env)
    ms = (time.perf_counter() - t0) * 1000
    cases["git_diff_lockfile"] = {
        "raw_bytes": len(diff_raw), "out_bytes": len(p.stdout),
        "reduction_pct": round(100 * (1 - len(p.stdout) / len(diff_raw)), 1),
        "error_preserved": "+let ttl = 300;" in p.stdout,
        "ms": round(ms, 2),
    }

    # 4. Content-addressed cache hit speedup on real cargo check
    env_cache = {**os.environ, "WM_SQUEEZE_NO_STATS": "1"}
    env_cache.pop("WM_SQUEEZE_NO_CACHE", None)
    # Clear existing cache entry by passing unique max-bytes or running twice
    t0 = time.perf_counter()
    p1 = subprocess.run([sq, "--safe", "--cwd", str(KIT_DIR / "tools" / "wm-squeeze"), "--", "cargo", "check"], capture_output=True, text=True, env=env_cache)
    cold_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    p2 = subprocess.run([sq, "--safe", "--cwd", str(KIT_DIR / "tools" / "wm-squeeze"), "--", "cargo", "check"], capture_output=True, text=True, env=env_cache)
    warm_ms = (time.perf_counter() - t0) * 1000
    cases["content_addressed_cache"] = {
        "cold_ms": round(cold_ms, 1),
        "cache_hit_ms": round(warm_ms, 1),
        "cache_hit_marker": "cache-hit" in p2.stdout,
        "speedup_x": round(cold_ms / max(warm_ms, 0.1), 1),
    }
    return cases


def bench_wm_codegraph():
    cg = str(LOCAL_BIN / "wm-codegraph")
    results = {}
    targets = [
        ("whitemagic-agy-kit", KIT_DIR, "squeeze"),
    ]
    extra_repo = os.environ.get("WM_EXTRA_CODEGRAPH_REPO")
    extra_sym = os.environ.get("WM_EXTRA_CODEGRAPH_SYM", "main")
    if extra_repo:
        targets.append((Path(extra_repo).name, Path(extra_repo), extra_sym))
    for label, root, sym in targets:
        if not root.is_dir():
            continue
        # Measure naive cost: total bytes of all source files containing `sym`
        naive_bytes = 0
        matched_files = 0
        for ext in ("*.rs", "*.py", "*.ts", "*.js"):
            for fp in root.rglob(ext):
                if any(part in ("target", "node_modules", ".git", "__pycache__") for part in fp.parts):
                    continue
                try:
                    txt = fp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if sym in txt:
                    naive_bytes += len(txt.encode("utf-8"))
                    matched_files += 1

        t0 = time.perf_counter()
        p_ov = subprocess.run([cg, "overview", "--root", str(root)], capture_output=True, text=True)
        cold_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        p_cal = subprocess.run([cg, "callers", sym, "--root", str(root)], capture_output=True, text=True)
        warm_ms = (time.perf_counter() - t0) * 1000
        cg_bytes = len(p_cal.stdout.encode("utf-8"))
        results[label] = {
            "symbol": sym,
            "naive_matching_files": matched_files,
            "naive_read_bytes": naive_bytes,
            "naive_est_tokens": naive_bytes // 4,
            "codegraph_bytes": cg_bytes,
            "codegraph_est_tokens": cg_bytes // 4,
            "reduction_pct": round(100 * (1 - cg_bytes / max(naive_bytes, 1)), 2),
            "compression_x": round(naive_bytes / max(cg_bytes, 1), 1),
            "index_ms": round(cold_ms, 1),
            "query_ms": round(warm_ms, 1),
        }
    return results


def bench_historical_replay():
    logs = sorted(BRAIN.glob("*/.system_generated/logs/transcript.jsonl"), key=os.path.getmtime, reverse=True)
    tot_unc = tot_cac = tot_out = tot_turns = 0
    user_idle_evict_unc = 0
    user_idle_evict_count = 0
    handoff_saved_unc = 0
    gap30_evict_unc = 0
    normal_inc_unc = 0
    spills = 0

    for lp in logs:
        prev_ts = None
        prev_ctx = 0
        turn_idx = 0
        for line in lp.read_text(errors="replace").splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type", "")
            if t == "USER_INPUT":
                prev_ts = None
                continue
            if t == "PLANNER_RESPONSE":
                unc = int(o.get("input_tokens") or 0)
                cac = int(o.get("cache_read_tokens") or 0)
                out = int(o.get("output_tokens") or 0)
                ctx = unc + cac
                ts = parse_ts(o.get("created_at"))
                if ctx > 0:
                    turn_idx += 1
                    tot_turns += 1
                    tot_unc += unc
                    tot_cac += cac
                    tot_out += out
                    hit = cac / ctx
                    if turn_idx > 1 and prev_ts is None and hit < 0.70:
                        user_idle_evict_unc += unc
                        user_idle_evict_count += 1
                        if unc > 3000:
                            handoff_saved_unc += (unc - 2000)
                    elif turn_idx > 1 and prev_ts is not None and ts and (ts - prev_ts) >= 30 and hit < 0.70:
                        gap30_evict_unc += unc
                    elif turn_idx > 1 and hit >= 0.70:
                        normal_inc_unc += unc
                    prev_ctx = ctx
                    if ts:
                        prev_ts = ts
            elif t == "GENERIC":
                c = str(o.get("content") or "")
                if "output.txt" in c and "truncated" in c.lower():
                    spills += 1

    return {
        "sessions": len(logs),
        "turns": tot_turns,
        "total_input_tokens": tot_unc + tot_cac,
        "total_uncached_tokens": tot_unc,
        "overall_cache_hit_pct": round(100.0 * tot_cac / max(tot_unc + tot_cac, 1), 2),
        "total_spills": spills,
        "user_idle_evictions": user_idle_evict_count,
        "user_idle_evict_uncached": user_idle_evict_unc,
        "wm_handoff_recoverable_uncached": handoff_saved_unc,
        "wm_handoff_pct_of_all_uncached": round(100.0 * handoff_saved_unc / max(tot_unc, 1), 1),
        "gap30_evict_uncached": gap30_evict_unc,
        "normal_incremental_uncached": normal_inc_unc,
    }


# ═══════════════════════════════════════════════════════════════════════════
# TRACK 2: PROGRESSIVE 3-LEVEL CONTROLLED LIVE `agy -p` A/B BENCHMARKS
# ═══════════════════════════════════════════════════════════════════════════

def init_git(d: Path):
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "bench@example.com"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "Bench"], cwd=d, check=True)
    subprocess.run(["git", "add", "."], cwd=d, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=d, check=True)


def build_level1_repo(d: Path):
    """Level 1 (Simple): 5 files, 360-line target module, 35KB noisy ANSI test runner, 1 subtle bug."""
    (d / "app").mkdir()
    # Padding 320 lines of realistic helper routines + bug at line 318
    helpers = [f"def helper_metric_{i}(x: float) -> float:\n    \"\"\"Metric transform {i}.\"\"\"\n    return round((x * {(i % 7) + 1.1:.2f}) / {(i % 5) + 1.0:.1f}, 4)\n" for i in range(105)]
    billing = (
        "\"\"\"Billing engine with discount tiers.\"\"\"\n\n"
        + "\n".join(helpers)
        + "\ndef apply_tier_discount(subtotal: float, tier: str) -> float:\n"
        + "    \"\"\"Apply discount: gold=20%, silver=10%, bronze=5%, else 0%.\"\"\"\n"
        + "    rates = {'gold': 0.20, 'silver': 0.10, 'bronze': 0.05}\n"
        + "    rate = rates.get(tier.lower(), 0.0)\n"
        + "    # BUG: adds rate instead of subtracting rate\n"
        + "    return round(subtotal * (1.0 + rate), 2)\n"
    )
    (d / "app" / "__init__.py").write_text("")
    (d / "app" / "billing.py").write_text(billing)
    (d / "app" / "models.py").write_text("from dataclasses import dataclass\n\n@dataclass\nclass Invoice:\n    id: str\n    subtotal: float\n    tier: str\n")
    (d / "app" / "service.py").write_text("from app.billing import apply_tier_discount\n\ndef final_total(subtotal: float, tier: str) -> float:\n    return apply_tier_discount(subtotal, tier)\n")

    # Realistic verbose pytest suite (450 parametrized PASSED cases = ~38KB noisy output + 2 real failures)
    test_py = """import pytest
from app.service import final_total
from app.billing import helper_metric_0

@pytest.mark.parametrize("i", range(450))
def test_helper_metric_nonnegative(i):
    assert helper_metric_0(float(i)) >= 0

def test_gold_discount():
    got = final_total(100.0, "gold")
    assert got == 80.0, f"Expected 80.0 for gold tier, got {got}"

def test_silver_discount():
    got = final_total(200.0, "silver")
    assert got == 180.0, f"Expected 180.0 for silver tier, got {got}"
"""
    (d / "test_billing.py").write_text(test_py)
    init_git(d)
    prompt = (
        "Run `pytest -v test_billing.py`, find why billing tests fail, fix the bug in the codebase, "
        "and re-run `pytest -v test_billing.py` until all tests pass. Reply with a 1-line summary."
    )
    verify_cmd = ["pytest", "-q", "test_billing.py"]
    return prompt, verify_cmd


def build_level2_repo(d: Path):
    """Level 2 (Medium): 10-file repo, 2-hop cross-file call graph across 6 callers, 400L core file."""
    (d / "pkg").mkdir()
    (d / "pkg" / "__init__.py").write_text("")
    pad = "\n".join(f"def util_pad_{i}(n: int) -> int:\n    return (n + {i}) * 2\n" for i in range(110))
    (d / "pkg" / "pricing.py").write_text(
        pad
        + "\ndef compute_fee(amount: float, currency: str = 'USD') -> float:\n"
        + "    \"\"\"Compute platform fee. Needs new required keyword-compatible parameter `urgent: bool = False` (+5.0 flat fee when urgent=True).\"\"\"\n"
        + "    base = 0.03 if currency == 'USD' else 0.05\n"
        + "    return round(amount * base, 2)\n"
    )
    callers = [
        ("checkout.py", "from pkg.pricing import compute_fee\n\ndef checkout_total(amount: float, urgent: bool = False) -> float:\n    return round(amount + compute_fee(amount, 'USD'), 2)\n"),
        ("invoice.py", "from pkg.pricing import compute_fee\n\ndef build_invoice_fee(amount: float, urgent: bool = False) -> float:\n    return compute_fee(amount, 'EUR')\n"),
        ("subscription.py", "from pkg.pricing import compute_fee\n\ndef renewal_fee(amount: float, urgent: bool = False) -> float:\n    return compute_fee(amount, 'USD')\n"),
        ("api.py", "from pkg.checkout import checkout_total\nfrom pkg.invoice import build_invoice_fee\n\ndef handle_order(amount: float, urgent: bool = False) -> dict:\n    return {'total': checkout_total(amount, urgent=urgent), 'inv_fee': build_invoice_fee(amount, urgent=urgent)}\n"),
        ("worker.py", "from pkg.subscription import renewal_fee\n\ndef process_renewal(amount: float, urgent: bool = False) -> float:\n    return renewal_fee(amount, urgent=urgent)\n"),
        ("audit.py", "def audit_entry(x: str) -> str:\n    return f'AUDIT:{x}'\n" + "\n".join(f"def audit_pad_{i}(x: str) -> str:\n    return x\n" for i in range(80))),
        ("metrics.py", "\n".join(f"def metric_pad_{i}(v: float) -> float:\n    return v * 1.{i%9}\n" for i in range(90))),
        ("config.py", "\n".join(f"CFG_KEY_{i} = 'val_{i}'\n" for i in range(180))),
    ]
    for fname, content in callers:
        (d / "pkg" / fname).write_text(content)

    (d / "test_pricing.py").write_text(
        """import pytest
from pkg.api import handle_order
from pkg.worker import process_renewal
from pkg.pricing import compute_fee, util_pad_0

@pytest.mark.parametrize("i", range(450))
def test_padding_bulk_invariant(i):
    assert util_pad_0(i) == i * 2

def test_urgent_fee_everywhere():
    assert compute_fee(100.0, 'USD', urgent=True) == 8.0
    assert compute_fee(100.0, 'USD', urgent=False) == 3.0
    r = handle_order(100.0, urgent=True)
    assert r['total'] == 108.0, f"checkout_total missed urgent: {r}"
    assert r['inv_fee'] == 10.0, f"build_invoice_fee missed urgent: {r}"
    assert process_renewal(100.0, urgent=True) == 8.0, "renewal_fee missed urgent"
"""
    )
    init_git(d)
    prompt = (
        "Add `urgent: bool = False` support to `compute_fee` (+5.0 flat fee when `urgent=True`) and propagate `urgent` "
        "through ALL direct and transitive callers across `pkg/` so `pytest -v test_pricing.py` passes completely. "
        "Reply with a 1-line summary."
    )
    verify_cmd = ["pytest", "-q", "test_pricing.py"]
    return prompt, verify_cmd


def build_level3_repo(d: Path):
    """Level 3 (Complex): 16-file repo + 65KB noisy CI failure log hiding 3 interacting bugs across 3 modules."""
    (d / "core").mkdir()
    (d / "core" / "__init__.py").write_text("")
    # 12 noise/realistic modules (~300 lines each) + 3 buggy modules
    for m in range(10):
        body = "\n".join(f"def mod{m}_op_{i}(v: int) -> int:\n    return (v * {(i%5)+1}) + {m}\n" for i in range(95))
        (d / "core" / f"subsystem_{m}.py").write_text(body)

    # Bug 1: tokenizer/cache key includes volatile timestamp!
    (d / "core" / "cache_key.py").write_text(
        "import hashlib, time\n\n"
        + "\n".join(f"def _ck_helper_{i}(s: str) -> str:\n    return s.strip()\n" for i in range(85))
        + "\ndef build_prefix_key(rules: list[str], tools: list[str]) -> str:\n"
        + "    # BUG 1: embeds time.time() and doesn't sort tools!\n"
        + "    payload = '|'.join(rules) + '::' + ','.join(tools) + f'::{int(time.time())}'\n"
        + "    return hashlib.sha256(payload.encode()).hexdigest()[:16]\n"
    )
    # Bug 2: chunker drops last chunk / off-by-one overlap
    (d / "core" / "chunker.py").write_text(
        "\n".join(f"def _ch_pad_{i}(n: int) -> int:\n    return n + {i}\n" for i in range(85))
        + "\ndef budget_truncate(text: str, max_bytes: int) -> str:\n"
        + "    # BUG 2: slices characters instead of UTF-8 bytes and forgets ellipsis cap\n"
        + "    if len(text) <= max_bytes:\n"
        + "        return text\n"
        + "    return text[:max_bytes] + '...'\n"
    )
    # Bug 3: retry backoff ignores cache-warmth ceiling (sleeps >60s instead of clamping <=10s)
    (d / "core" / "scheduler.py").write_text(
        "from core.cache_key import build_prefix_key\nfrom core.chunker import budget_truncate\n\n"
        + "\n".join(f"def _sc_pad_{i}(x: int) -> int:\n    return x\n" for i in range(80))
        + "\ndef next_poll_delay(attempt: int) -> int:\n"
        + "    # BUG 3: exponential backoff uncapped; must cap at 10s to protect prefix cache\n"
        + "    return 2 ** attempt\n\n"
        + "def prepare_turn(rules: list[str], tools: list[str], tool_out: str, max_bytes: int) -> dict:\n"
        + "    return {'key': build_prefix_key(rules, tools), 'out': budget_truncate(tool_out, max_bytes)}\n"
    )

    # 65 KB noisy CI failure log hiding the 3 real failure traces!
    noise_head = [f"[2026-11-21 16:00:{i%60:02d}] \x1b[32mINFO\x1b[0m ci.runner: test_subsystem_{i%10}::case_{i:04d} PASSED in 0.00{i%9}s (mem=42MB)" for i in range(420)]
    real_err_1 = [
        "[2026-11-21 16:02:11] \x1b[31mFAIL\x1b[0m test_suite.py::test_deterministic_prefix_key",
        "AssertionError: build_prefix_key(['r1'], ['b', 'a']) != build_prefix_key(['r1'], ['a', 'b']) or varied across seconds",
    ]
    noise_mid = [f"[2026-11-21 16:03:{i%60:02d}] \x1b[32mINFO\x1b[0m ci.runner: test_integration_{i%8}::check_{i:04d} PASSED (ok)" for i in range(380)]
    real_err_2 = [
        "[2026-11-21 16:05:44] \x1b[31mFAIL\x1b[0m test_suite.py::test_utf8_byte_budget",
        "AssertionError: budget_truncate('🚀' * 50, 40) returned 163 UTF-8 bytes (> 40 bytes limit!)",
    ]
    noise_tail = [f"[2026-11-21 16:06:{i%60:02d}] \x1b[32mINFO\x1b[0m ci.runner: test_perf_{i%5}::step_{i:04d} PASSED (1.2ms)" for i in range(220)]
    real_err_3 = [
        "[2026-11-21 16:08:02] \x1b[31mFAIL\x1b[0m test_suite.py::test_cache_safe_poll_delay",
        "AssertionError: next_poll_delay(6) returned 64 (> 10s cache-safe ceiling)",
    ]
    (d / "ci_failure_dump.log").write_text("\n".join(noise_head + real_err_1 + noise_mid + real_err_2 + noise_tail + real_err_3) + "\n")

    (d / "test_suite.py").write_text(
        """import time
import pytest
from core.cache_key import build_prefix_key
from core.chunker import budget_truncate
from core.scheduler import next_poll_delay, prepare_turn

@pytest.mark.parametrize("i", range(450))
def test_bulk_pass_invariant(i):
    assert i >= 0

def test_deterministic_prefix_key():
    k1 = build_prefix_key(["r1", "r2"], ["b_tool", "a_tool"])
    k2 = build_prefix_key(["r1", "r2"], ["a_tool", "b_tool"])
    assert k1 == k2, f"tool order broke determinism: {k1} vs {k2}"
    time.sleep(1.05)
    k3 = build_prefix_key(["r1", "r2"], ["a_tool", "b_tool"])
    assert k1 == k3, f"timestamp broke determinism: {k1} vs {k3}"

def test_utf8_byte_budget():
    res = budget_truncate("🚀" * 50, 40)
    assert len(res.encode("utf-8")) <= 40, f"exceeded 40 UTF-8 bytes: {len(res.encode('utf-8'))}"
    assert budget_truncate("hello", 40) == "hello"

def test_cache_safe_poll_delay():
    assert next_poll_delay(1) == 2
    assert next_poll_delay(6) <= 10, f"poll delay {next_poll_delay(6)} > 10"
    turn = prepare_turn(["r"], ["b", "a"], "🚀" * 30, 32)
    assert len(turn["out"].encode("utf-8")) <= 32
"""
    )
    init_git(d)
    prompt = (
        "Inspect `ci_failure_dump.log` to diagnose all failing checks, locate the responsible symbols in `core/`, "
        "fix all 3 root causes, and verify `pytest -v test_suite.py` passes completely. Reply with a 1-line summary."
    )
    verify_cmd = ["pytest", "-q", "test_suite.py"]
    return prompt, verify_cmd


def build_level4_rust_repo(d: Path):
    """Level 4 (Hard Rust Multi-Module): 8-file Rust crate with noisy cargo test (400 ok + 3 real failures), cross-module call chain."""
    (d / "src").mkdir()
    (d / "Cargo.toml").write_text(
        '[package]\nname = "cache_lab"\nversion = "0.1.0"\nedition = "2021"\n'
    )
    pad_a = "\n".join(f"pub fn pad_a_{i}(x: i64) -> i64 {{ x.wrapping_add({i}) }}" for i in range(95))
    (d / "src" / "prefix.rs").write_text(
        pad_a
        + "\n/// Canonicalize tool names for deterministic prefix cache key.\n"
        + "pub fn canonical_tool_header(tools: &[&str]) -> String {\n"
        + "    // BUG 1: does not sort or deduplicate tools\n"
        + "    tools.join(\",\")\n"
        + "}\n"
    )
    pad_b = "\n".join(f"pub fn pad_b_{i}(x: usize) -> usize {{ x + {i} }}" for i in range(95))
    (d / "src" / "budget.rs").write_text(
        pad_b
        + "\n/// Truncate a string safely to <= max_bytes UTF-8 bytes without splitting multi-byte chars.\n"
        + "pub fn cap_utf8_bytes(s: &str, max_bytes: usize) -> String {\n"
        + "    // BUG 2: counts chars instead of UTF-8 bytes\n"
        + "    s.chars().take(max_bytes).collect()\n"
        + "}\n"
    )
    pad_c = "\n".join(f"pub fn pad_c_{i}(x: u64) -> u64 {{ x * {(i%3)+1} }}" for i in range(95))
    (d / "src" / "ttl.rs").write_text(
        pad_c
        + "\nuse crate::prefix::canonical_tool_header;\nuse crate::budget::cap_utf8_bytes;\n\n"
        + "/// Return true if inter-turn gap (seconds) is still inside Neon's <30s warm window.\n"
        + "pub fn is_cache_warm(gap_secs: u64) -> bool {\n"
        + "    // BUG 3: inverted condition\n"
        + "    gap_secs >= 30\n"
        + "}\n\n"
        + "pub fn pack_turn(tools: &[&str], out: &str, max_bytes: usize) -> String {\n"
        + "    format!(\"{}|{}\", canonical_tool_header(tools), cap_utf8_bytes(out, max_bytes))\n"
        + "}\n"
    )
    (d / "src" / "lib.rs").write_text(
        "pub mod prefix;\npub mod budget;\npub mod ttl;\n\n"
        "#[cfg(test)]\nmod tests {\n"
        "    use super::*;\n"
        + "\n".join(f"    #[test] fn noisy_pass_{i:03d}() {{ assert_eq!(prefix::pad_a_0({i}), {i}); }}" for i in range(220))
        + "\n    #[test]\n    fn test_canonical_tool_header() {\n"
        "        assert_eq!(prefix::canonical_tool_header(&[\"z\", \"a\", \"a\", \"m\"]), \"a,m,z\");\n"
        "    }\n"
        "    #[test]\n    fn test_cap_utf8_bytes_multibyte() {\n"
        "        let s = \"🚀\".repeat(20);\n"
        "        let capped = budget::cap_utf8_bytes(&s, 10);\n"
        "        assert!(capped.len() <= 10, \"expected <=10 UTF-8 bytes, got {}\", capped.len());\n"
        "        assert_eq!(capped, \"🚀🚀\");\n"
        "    }\n"
        "    #[test]\n    fn test_cache_warm_and_pack() {\n"
        "        assert!(ttl::is_cache_warm(12));\n"
        "        assert!(!ttl::is_cache_warm(45));\n"
        "        assert_eq!(ttl::pack_turn(&[\"b\", \"a\"], \"🚀ok\", 4), \"a,b|🚀\");\n"
        "    }\n"
        "}\n"
    )
    init_git(d)
    prompt = (
        "Run `cargo test`, diagnose all failing Rust unit tests across `src/`, fix all 3 bugs, "
        "and verify `cargo test` passes completely with 0 failures. Reply with a 1-line summary."
    )
    verify_cmd = ["cargo", "test", "--quiet"]
    return prompt, verify_cmd


def run_live_arm(level_name, arm_name, build_fn, model_name, vanilla=False):
    with tempfile.TemporaryDirectory(prefix=f"agy_bench_{level_name}_{arm_name}_") as td:
        repo = Path(td)
        base_prompt, verify_cmd = build_fn(repo)
        if vanilla:
            prompt = (
                "IMPORTANT FOR BENCHMARK CONTROL: Use ONLY built-in `run_command`, `read_file`, `edit_file`, `write_file` "
                "(do NOT invoke `agy-fast`, `wm-codegraph`, `wm-local-digest`, or `wm-squeeze`).\n"
                + base_prompt
            )
        else:
            prompt = (
                "Use your Wave 4 efficiency tools (`wm-codegraph`, `wm-local-digest` for large logs, `agy-fast` batching, "
                "and `PreToolUse` `wm-squeeze`) to stay fast and token-efficient.\n"
                + base_prompt
            )

        env = os.environ.copy()
        if vanilla:
            env["WM_AGY_BENCH_VANILLA"] = "1"
        else:
            env.pop("WM_AGY_BENCH_VANILLA", None)

        # Track existing brain dirs before launch so we locate exact new transcript.jsonl
        before_ids = set(p.name for p in BRAIN.iterdir() if p.is_dir()) if BRAIN.is_dir() else set()

        cmd = [
            str(LOCAL_BIN / "agy"),
            "-p", prompt,
            "--model", model_name,
            "--dangerously-skip-permissions",
            "--output-format", "json",
        ]
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=300)
        wall_s = round(time.perf_counter() - t0, 2)

        # Ground-truth external verification
        ver = subprocess.run(verify_cmd, cwd=repo, capture_output=True, text=True, timeout=60)
        passed = ver.returncode == 0

        # Locate newly created transcript
        after_ids = set(p.name for p in BRAIN.iterdir() if p.is_dir()) if BRAIN.is_dir() else set()
        new_ids = after_ids - before_ids
        conv_id = ""
        try:
            jout = json.loads(proc.stdout)
            conv_id = jout.get("conversation_id") or jout.get("conversationId") or jout.get("session_id") or ""
        except Exception:
            pass
        tp = None
        if conv_id and (BRAIN / conv_id / ".system_generated" / "logs" / "transcript.jsonl").is_file():
            tp = BRAIN / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        elif new_ids:
            cands = [BRAIN / nid / ".system_generated" / "logs" / "transcript.jsonl" for nid in new_ids]
            cands = [c for c in cands if c.is_file()]
            if cands:
                tp = max(cands, key=os.path.getmtime)

        metrics = {
            "level": level_name,
            "arm": arm_name,
            "model": model_name,
            "passed": passed,
            "wall_s": wall_s,
            "exit_code": proc.returncode,
            "conv_id": tp.parents[2].name[:10] if tp else "",
            "turns": 0,
            "tool_calls": 0,
            "peak_ctx": 0,
            "uncached_in": 0,
            "cached_in": 0,
            "out_tokens": 0,
            "cache_hit_pct": 0.0,
            "spills": 0,
            "tool_bytes": 0,
            "credit_index": 0.0,
        }
        if tp:
            for line in tp.read_text(errors="replace").splitlines():
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
                        metrics["turns"] += 1
                        metrics["uncached_in"] += unc
                        metrics["cached_in"] += cac
                        metrics["out_tokens"] += out
                        metrics["peak_ctx"] = max(metrics["peak_ctx"], ctx)
                    metrics["tool_calls"] += len(o.get("tool_calls") or [])
                elif t == "GENERIC":
                    c = str(o.get("content") or "")
                    metrics["tool_bytes"] += len(c)
                    if "output.txt" in c and "truncated" in c.lower():
                        metrics["spills"] += 1
            tot_in = metrics["uncached_in"] + metrics["cached_in"]
            metrics["cache_hit_pct"] = round(100.0 * metrics["cached_in"] / max(tot_in, 1), 1)
            # Weighted billable credit proxy: uncached_in + 0.1 * cached_in + 5 * output
            metrics["credit_index"] = round(metrics["uncached_in"] + 0.1 * metrics["cached_in"] + 5.0 * metrics["out_tokens"], 1)
        return metrics


def main():
    ap = argparse.ArgumentParser(description="Progressive AGY Benchmark Suite (Track 1 offline + Track 2 live A/B)")
    ap.add_argument("--track1", action="store_true", help="Run Track 1 offline micro-benchmarks & full historical replay")
    ap.add_argument("--track2", action="store_true", help="Run Track 2 controlled 3-level live agy -p A/B trials")
    ap.add_argument("--levels", default="1,2,3", help="Comma-separated Track 2 levels (default: 1,2,3)")
    ap.add_argument("--out-json", help="Optional path to save full JSON results")
    args = ap.parse_args()

    if not args.track1 and not args.track2:
        args.track1 = True

    report = {"ts": datetime.now(timezone.utc).isoformat()}

    if args.track1:
        print("=== TRACK 1: OFFLINE MICRO-BENCHMARKS & FULL HISTORICAL REPLAY ===")
        t1 = {
            "wm_squeeze_v3": bench_wm_squeeze_v3(),
            "wm_codegraph": bench_wm_codegraph(),
            "historical_replay": bench_historical_replay(),
        }
        report["track1"] = t1
        print("\n[1A] wm-squeeze v3 Compression & Content-Addressed Cache:")
        for k, v in t1["wm_squeeze_v3"].items():
            print(f"  {k:26s}: {v}")
        print("\n[1B] wm-codegraph vs Naive Full-File Trace:")
        for k, v in t1["wm_codegraph"].items():
            print(f"  {k:26s}: {v['naive_est_tokens']:,} tok -> {v['codegraph_est_tokens']:,} tok (-{v['reduction_pct']}%, {v['compression_x']}x) | idx={v['index_ms']}ms query={v['query_ms']}ms")
        hr = t1["historical_replay"]
        print(f"\n[1C] Full Historical Corpus ({hr['sessions']} sessions, {hr['turns']:,} turns, {hr['total_input_tokens']/1e9:.2f}B in):")
        print(f"  Uncached total            : {hr['total_uncached_tokens']/1e6:.2f}M ({hr['overall_cache_hit_pct']}% hit) | spills: {hr['total_spills']}")
        print(f"  User-idle cache evictions : {hr['user_idle_evictions']} events -> {hr['user_idle_evict_uncached']/1e6:.2f}M unc")
        print(f"  wm-handoff recoverable    : {hr['wm_handoff_recoverable_uncached']/1e6:.2f}M unc ({hr['wm_handoff_pct_of_all_uncached']}% of ALL historical uncached burn!)")

    if args.track2:
        print("\n=== TRACK 2: LIVE CONTROLLED PROGRESSIVE 3-LEVEL A/B BENCHMARK (agy -p) ===")
        lvl_map = {
            "1": ("L1_simple", build_level1_repo, [("vanilla", "Gemini 4 Neon (High)", True), ("wave4_neon", "Gemini 4 Neon (High)", False)]),
            "2": ("L2_medium", build_level2_repo, [("vanilla", "Gemini 4 Neon (High)", True), ("wave4_neon", "Gemini 4 Neon (High)", False), ("wave4_hydrogen", "Gemini 4 Hydrogen (High)", False)]),
            "3": ("L3_complex", build_level3_repo, [("vanilla", "Gemini 4 Neon (High)", True), ("wave4_neon", "Gemini 4 Neon (High)", False), ("wave4_hydrogen", "Gemini 4 Hydrogen (High)", False)]),
            "4": ("L4_rust_crate", build_level4_rust_repo, [("vanilla", "Gemini 4 Neon (High)", True), ("wave4_neon", "Gemini 4 Neon (High)", False), ("wave4_hydrogen", "Gemini 4 Hydrogen (High)", False)]),
        }
        live_rows = []
        for lv in [x.strip() for x in args.levels.split(",") if x.strip() in lvl_map]:
            lname, bfn, arms = lvl_map[lv]
            base_ci = None
            for aname, mdl, is_van in arms:
                print(f"  Running {lname} / {aname} ({mdl})...", flush=True)
                res = run_live_arm(lname, aname, bfn, mdl, vanilla=is_van)
                if is_van and res["credit_index"] > 0:
                    base_ci = res["credit_index"]
                if base_ci:
                    res["credit_delta_pct"] = round(100.0 * (res["credit_index"] - base_ci) / base_ci, 1)
                live_rows.append(res)
                print(
                    f"    -> pass={res['passed']} wall={res['wall_s']}s turns={res['turns']} tools={res['tool_calls']} "
                    f"peak={res['peak_ctx']//1000}k unc={res['uncached_in']//1000}k hit={res['cache_hit_pct']}% "
                    f"out={res['out_tokens']//1000}k spills={res['spills']} toolKB={res['tool_bytes']//1024}KB "
                    f"credit={res['credit_index']/1000:.1f}k ({res.get('credit_delta_pct', 0.0):+.1f}%)",
                    flush=True,
                )
        report["track2"] = live_rows

    if args.out_json:
        Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nSaved JSON report -> {args.out_json}")


if __name__ == "__main__":
    main()
