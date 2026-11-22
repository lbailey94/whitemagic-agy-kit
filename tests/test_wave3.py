#!/usr/bin/env python3
"""Comprehensive end-to-end verification suite for WhiteMagic AGY Turbo Kit v2.0.0 (Wave 3)."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
LOCAL_BIN = Path.home() / ".local" / "bin"
WM_SQUEEZE = LOCAL_BIN / "wm-squeeze"
_PLUGIN_HOOKS = Path.home() / ".gemini" / "config" / "plugins" / "whitemagic-agy-kit" / "hooks"
_GLOBAL_HOOKS = Path.home() / ".gemini" / "config" / "hooks"
_HOOKS_DIR = _PLUGIN_HOOKS if (_PLUGIN_HOOKS / "pre_tool_guard.py").is_file() else _GLOBAL_HOOKS
PRE_HOOK = _HOOKS_DIR / "pre_tool_guard.py"
STOP_HOOK = _HOOKS_DIR / "stop_telemetry.py"
START_HOOK = _HOOKS_DIR / "session_start_brief.py"
STATUSLINE = Path.home() / ".gemini" / "antigravity-cli" / "statusline.py"

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" :: {detail}" if detail else ""))


def run_hook(script, payload, env_extra=None):
    env = os.environ.copy()
    env["WM_SQUEEZE_NO_STATS"] = "1"
    if env_extra:
        env.update(env_extra)
    t0 = time.perf_counter()
    p = subprocess.run(
        ["python3", "-S", "-B", str(script)],
        input=json.dumps(payload) if isinstance(payload, dict) else str(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    ms = (time.perf_counter() - t0) * 1000
    return p.returncode, p.stdout.strip(), p.stderr.strip(), ms


def test_wm_squeeze():
    print("\n1. wm-squeeze Security, Compression & Latency:")
    check("binary exists & executable", WM_SQUEEZE.is_file() and os.access(WM_SQUEEZE, os.X_OK))

    # Security refusals (must ALL exit 2)
    unsafe_cases = [
        ["rm", "-rf", "/tmp/nonexistent"],
        ["git", "push", "origin", "main"],
        ["git", "-c", "core.sshCommand=id", "status"],
        ["rg", "--pre=id", "foo"],
        ["find", "/tmp", "-exec", "id", ";"],
        ["find", "/tmp", "-delete"],
        ["cargo", "publish"],
        ["python3", "-c", "print(1)"],
        ["sh", "-c", "id"],
        ["wm-gen3", "init"],
    ]
    env = {**os.environ, "WM_SQUEEZE_NO_STATS": "1"}
    refused_ok = True
    for args in unsafe_cases:
        p = subprocess.run([str(WM_SQUEEZE), "--safe", "--"] + args, capture_output=True, text=True, env=env)
        if p.returncode != 2:
            refused_ok = False
            check(f"refuse {' '.join(args)}", False, f"expected rc=2 got {p.returncode}")
    check(f"all {len(unsafe_cases)} unsafe commands refused (rc=2)", refused_ok)

    # Disallowed env var via --env must be refused (rc=2)
    p_bad_env = subprocess.run([str(WM_SQUEEZE), "--safe", "--env", "LD_PRELOAD=/tmp/x.so", "--", "ls"], capture_output=True, text=True, env=env)
    check("disallowed --env LD_PRELOAD refused (rc=2)", p_bad_env.returncode == 2)

    # Safe command execution & latency
    t0 = time.perf_counter()
    p = subprocess.run([str(WM_SQUEEZE), "--safe", "--cwd", str(KIT), "--", "git", "status", "--short"], capture_output=True, text=True, env=env)
    ms = (time.perf_counter() - t0) * 1000
    check("safe git status executes", p.returncode == 0, f"{ms:.1f}ms")

    # Cargo compression test (ANSI + 200 Compiling + 500 passed + 1 real panic)
    noisy_cargo = "\n".join(
        [f"\x1b[32m   Compiling\x1b[0m crate_{i} v0.1.0" for i in range(200)]
        + [f"test mod::test_{i} ... ok" for i in range(500)]
        + [
            "test core::critical_invariant ... FAILED",
            "thread 'core::critical_invariant' panicked at src/lib.rs:42:9:",
            "assertion `left == right` failed: left: 1, right: 2",
            "test result: FAILED. 500 passed; 1 failed; 0 ignored",
        ]
    )
    p = subprocess.run([str(WM_SQUEEZE), "--pipe", "--kind", "cargo"], input=noisy_cargo, capture_output=True, text=True, env=env)
    out = p.stdout
    red = 100.0 * (1.0 - len(out) / len(noisy_cargo))
    check(
        "cargo compression strips noise & preserves FAILED panic",
        p.returncode == 0
        and len(out.encode()) <= 8200
        and red >= 85.0
        and "\x1b[" not in out
        and "panicked at src/lib.rs:42:9" in out
        and "test result: FAILED" in out,
        f"{len(noisy_cargo)}B -> {len(out)}B (-{red:.1f}%)",
    )

    # Git diff lockfile compression
    lock_hunk = "diff --git a/package-lock.json b/package-lock.json\n" + "\n".join(f"+  \"pkg_{i}\": \"1.0.0\"," for i in range(1500))
    code_hunk = "\ndiff --git a/src/app.rs b/src/app.rs\n@@ -1,2 +1,2 @@\n-let x = 1;\n+let x = 2;\n"
    noisy_diff = lock_hunk + code_hunk
    p = subprocess.run([str(WM_SQUEEZE), "--pipe", "--kind", "git"], input=noisy_diff, capture_output=True, text=True, env=env)
    check(
        "git diff skips lockfile bloat & keeps source hunk",
        p.returncode == 0 and len(p.stdout.encode()) <= 8200 and "+let x = 2;" in p.stdout and "skipped" in p.stdout,
        f"{len(noisy_diff)}B -> {len(p.stdout)}B",
    )

    # Pytest -v multi-space right-aligned [  0%] collapsing
    noisy_pytest = "\n".join(
        [f"test_mod.py::test_case[{i}] PASSED                                        [{min(99, i):3d}%]" for i in range(300)]
        + ["test_mod.py::test_real_bug FAILED                                       [100%]", "AssertionError: expected 80 got 120"]
    )
    p = subprocess.run([str(WM_SQUEEZE), "--pipe", "--kind", "pytest"], input=noisy_pytest, capture_output=True, text=True, env=env)
    check(
        "pytest -v right-aligned PASSED lines collapsed & FAILED kept",
        p.returncode == 0 and "300 passed lines collapsed" in p.stdout and "AssertionError: expected 80 got 120" in p.stdout,
        f"{len(noisy_pytest)}B -> {len(p.stdout)}B",
    )

    # CoACT linter diagnostic grouping
    noisy_lint = "\n".join(f"src/file_{f}.ts({l},5): error TS2322: Type mismatch" for f in range(8) for l in range(1, 35))
    p = subprocess.run([str(WM_SQUEEZE), "--pipe", "--kind", "linter"], input=noisy_lint, capture_output=True, text=True, env=env)
    check(
        "CoACT linter grouping caps <=8.2KB & keeps file anchors",
        p.returncode == 0 and "linter-grouped" in p.stdout and len(p.stdout.encode()) <= 8200,
        f"{len(noisy_lint)}B -> {len(p.stdout)}B",
    )

    # Content-addressed cache hit on repeated safe git status
    env_cache = {**os.environ, "WM_SQUEEZE_NO_STATS": "1"}
    env_cache.pop("WM_SQUEEZE_NO_CACHE", None)
    subprocess.run([str(WM_SQUEEZE), "--safe", "--cwd", str(KIT), "--", "git", "log", "-n", "2", "--oneline"], capture_output=True, text=True, env=env_cache)
    p2 = subprocess.run([str(WM_SQUEEZE), "--safe", "--cwd", str(KIT), "--", "git", "log", "-n", "2", "--oneline"], capture_output=True, text=True, env=env_cache)
    check("wm-squeeze content-addressed cache hit on repeat cmd", p2.returncode == 0 and "cache-hit" in p2.stdout)


def test_pre_tool_hook():
    print("\n2. PreToolUse Guard & Deterministic Optimizer:")
    # Destructive guard (rm -rf, rm -fr, rm -r -f)
    rc, out, _, ms = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": "rm -rf /"}}})
    d = json.loads(out)
    check("destructive rm -rf / -> force_ask", rc == 0 and d.get("decision") == "force_ask", f"{ms:.1f}ms")

    for variant in ("rm -fr /", "rm -r -f /", "rm -f -r ~"):
        rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": variant}}})
        d = json.loads(out)
        check(f"destructive {variant} -> force_ask", rc == 0 and d.get("decision") == "force_ask")

    # Safe noisy run_command overwrite
    rc, out, _, ms = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": "git diff HEAD~1"}}})
    d = json.loads(out)
    ow = (d.get("overwrite") or {}).get("CommandLine", "")
    import shlex as _shlex
    ow_toks = _shlex.split(ow) if ow else []
    check("git diff HEAD~1 -> overwrite wm-squeeze --safe", rc == 0 and d.get("decision") == "allow" and "--safe" in ow_toks and ow_toks[-3:] == ["git", "diff", "HEAD~1"], f"{ms:.1f}ms -> {ow}")

    # Compound cd && cargo test overwrite
    rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": "cd /tmp && cargo test"}}})
    d = json.loads(out)
    ow = (d.get("overwrite") or {}).get("CommandLine", "")
    check("cd /tmp && cargo test -> --cwd /tmp", d.get("decision") == "allow" and "--cwd /tmp" in ow)

    # Safe env var prefix PYTHONPATH=. pytest -v -> wrapped with --env PYTHONPATH=.
    rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": "PYTHONPATH=. pytest -v tests/test_a.py"}}})
    d = json.loads(out)
    ow = (d.get("overwrite") or {}).get("CommandLine", "")
    check("PYTHONPATH=. pytest -v -> --env PYTHONPATH=.", d.get("decision") == "allow" and "--env PYTHONPATH=." in ow)

    # Piped/globbed/redirected commands left untouched
    for cmd in ("git diff | head -n 20", "ls *.py", "echo hi > /tmp/x", "python3 script.py"):
        rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
        d = json.loads(out)
        check(f"untouched: {cmd}", d.get("decision") == "allow" and "overwrite" not in d)

    with tempfile.TemporaryDirectory() as td:
        small = Path(td) / "small.py"
        small.write_text("\n".join(f"x = {i}" for i in range(40)), encoding="utf-8")
        big = Path(td) / "big.py"
        big.write_text("\n".join(f"line_{i} = 'padding text here for realistic line length'" for i in range(400)), encoding="utf-8")
        tr_hi = Path(td) / "tr_hi.jsonl"
        tr_hi.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 12000, "cache_read_tokens": 175000}) + "\n")
        tr_lo = Path(td) / "tr_lo.jsonl"
        tr_lo.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 8000, "cache_read_tokens": 40000}) + "\n")

        # Small file untouched
        _, out, _, _ = run_hook(PRE_HOOK, {"conversationId": "test-conv-1", "stepIdx": 1, "toolCall": {"name": "read_file", "args": {"Path": str(small)}}})
        d = json.loads(out)
        check("small file read untouched", d.get("decision") == "allow" and "overwrite" not in d)

        # Large unbounded file auto-clamped
        _, out, _, ms = run_hook(PRE_HOOK, {"conversationId": "test-conv-2", "stepIdx": 2, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big)}}})
        d = json.loads(out)
        check("large unbounded file -> clamped 1-200", d.get("decision") == "allow" and d.get("overwrite") == {"StartLine": 1, "EndLine": 200}, f"{ms:.1f}ms")

        # Exact duplicate unchanged read -> denied
        _, out, _, _ = run_hook(PRE_HOOK, {"conversationId": "test-conv-2", "stepIdx": 5, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big), "StartLine": 1, "EndLine": 200}}})
        d = json.loads(out)
        check("unchanged exact duplicate read -> denied", d.get("decision") == "deny" and "Read-Dedup" in d.get("reason", ""))

        # Next range on same file -> allowed
        _, out, _, _ = run_hook(PRE_HOOK, {"conversationId": "test-conv-2", "stepIdx": 6, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big), "StartLine": 201, "EndLine": 400}}})
        d = json.loads(out)
        check("different range (201-400) -> allowed", d.get("decision") == "allow")

        # Multimodal >=150k -> force_ask; <150k -> allow
        _, out, _, _ = run_hook(PRE_HOOK, {"transcriptPath": str(tr_hi), "toolCall": {"name": "read_file", "args": {"Path": "/tmp/shot.png"}}})
        d = json.loads(out)
        check("image at 187k ctx -> force_ask", d.get("decision") == "force_ask" and "Multimodal Cache Guard" in d.get("reason", ""))

        _, out, _, _ = run_hook(PRE_HOOK, {"transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": "/tmp/shot.png"}}})
        d = json.loads(out)
        check("image at 48k ctx -> allow", d.get("decision") == "allow")

    # WhiteMagic MCP auto-budget
    _, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "mcp_whitemagic_session_continuity", "args": {}}})
    d = json.loads(out)
    check("wm session_continuity -> token_budget=1200", d.get("overwrite") == {"token_budget": 1200})

    _, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "mcp_whitemagic_memory_recall", "args": {"query": "test", "limit": 12}}})
    d = json.loads(out)
    check("wm memory_recall limit=12 -> clamped 5 & max_content_bytes=900", d.get("overwrite") == {"limit": 5, "max_content_bytes": 900})

    # Official whitemagic-plugins Gen3 MCP auto-budget (schema-exact)
    _, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "mcp_whitemagic_whitemagic_gen3_session_continuity", "args": {}}})
    d = json.loads(out)
    check("wm_gen3 session_continuity -> token_budget=1200", d.get("overwrite") == {"token_budget": 1200})

    _, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "mcp_whitemagic_whitemagic_gen3_memory_recall", "args": {"query": "test", "limit": 12}}})
    d = json.loads(out)
    check("wm_gen3 memory_recall limit=12 -> clamped 5 only (no unsupported max_content_bytes)", d.get("overwrite") == {"limit": 5})

    # Fail-open on corrupt stdin
    rc, out, _, _ = run_hook(PRE_HOOK, "{broken-json")
    check("fail-open on invalid stdin", rc == 0 and json.loads(out).get("decision") == "allow")


def test_statusline_and_lifecycle():
    print("\n3. Statusline, Stop Telemetry, SessionStart & CLIs:")
    # Find newest live transcript
    brain = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    logs = sorted(brain.glob("*/.system_generated/logs/transcript.jsonl"), key=os.path.getmtime, reverse=True)
    tp = str(logs[0]) if logs else ""
    cid = logs[0].parents[2].name if logs else ""

    rc, out, _, ms = run_hook(
        STATUSLINE,
        {
            "model": {"display_name": "Gemini 4 Neon", "effort": "XHigh"},
            "context_window": {"total_input_tokens": 135000, "used_percentage": 13.5},
            "conversation_id": cid,
            "workspace": {"current_dir": str(KIT)},
        },
    )
    check("statusline renders hit% + warmth + uncached", rc == 0 and "hit:" in out and ("warm:" in out or "COOL:" in out or "COLD:" in out), f"{ms:.1f}ms")

    with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
        rc, out, _, ms = run_hook(
            STOP_HOOK,
            {"conversationId": cid, "transcriptPath": tp, "terminationReason": "NO_TOOL_CALL", "cwd": str(KIT)},
            env_extra={"WM_AGY_TELEMETRY": tf.name},
        )
        logged = Path(tf.name).read_text().strip()
        check("stop_telemetry logs & allows stop", rc == 0 and json.loads(out).get("decision") == "allow" and "last_hit_pct" in logged, f"{ms:.1f}ms")

    rc, out, _, ms = run_hook(START_HOOK, {"conversationId": cid})
    d = json.loads(out)
    msg = d.get("injectSteps", [{}])[0].get("systemMessage", {}).get("systemMessage", "")
    check("session_start_brief valid & <=1800B", rc == 0 and 0 < len(msg) <= 1800, f"{len(msg)}B in {ms:.1f}ms")

    # agy-mode dry-run
    p = subprocess.run([str(LOCAL_BIN / "agy-mode"), "longevity", "--dry-run"], capture_output=True, text=True)
    check("agy-mode longevity --dry-run", p.returncode == 0 and "Gemini 4 Hydrogen (XHigh)" in p.stdout)

    # wm-dream dry-run
    p = subprocess.run([str(LOCAL_BIN / "wm-dream"), "--dry-run"], capture_output=True, text=True)
    check("wm-dream --dry-run", p.returncode == 0 and "Dream Digest" in p.stdout)

    # Wave 4: wm-codegraph CLI + MCP + wm-handoff + wm-sim prefix-hash + VANILLA bypass
    cg = LOCAL_BIN / "wm-codegraph"
    p = subprocess.run([str(cg), "callers", "squeeze", "--root", str(KIT)], capture_output=True, text=True)
    check("wm-codegraph callers finds squeeze -> main", p.returncode == 0 and "main" in p.stdout)

    mcp_in = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'
        + json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "code_graph",
                    "arguments": {"mode": "callers", "target": "squeeze", "root": str(KIT), "depth": 1},
                },
            }
        )
        + "\n"
    )
    p = subprocess.run([str(cg), "--mcp"], input=mcp_in, capture_output=True, text=True)
    check("wm-codegraph --mcp responds to tools/list", p.returncode == 0 and "code_graph" in p.stdout)
    check("wm-codegraph --mcp honors depth=1 argument", p.returncode == 0 and "BFS depth <= 1" in p.stdout)

    p = subprocess.run([str(LOCAL_BIN / "wm-handoff"), "--cwd", str(KIT)], capture_output=True, text=True)
    check("wm-handoff emits verbatim crystal with sha256", p.returncode == 0 and "crystal_sha256=" in p.stdout)
    p_help = subprocess.run([str(LOCAL_BIN / "wm-handoff"), "--help"], capture_output=True, text=True)
    check("wm-handoff --help documents --save-wm3", p_help.returncode == 0 and "--save-wm3" in p_help.stdout)

    p = subprocess.run([str(LOCAL_BIN / "wm-sim"), "prefix-hash"], capture_output=True, text=True)
    check("wm-sim prefix-hash computes ContextCacheToken", p.returncode == 0 and "ContextCacheToken SHA-256" in p.stdout)

    rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": "git diff"}}}, env_extra={"WM_AGY_BENCH_VANILLA": "1"})
    d = json.loads(out)
    check("WM_AGY_BENCH_VANILLA=1 passes through untouched", rc == 0 and d == {"decision": "allow"})


def main():
    test_wm_squeeze()
    test_pre_tool_hook()
    test_statusline_and_lifecycle()
    print(f"\n=== Wave 3 Test Summary: {PASS} passed, {FAIL} failed ===")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
