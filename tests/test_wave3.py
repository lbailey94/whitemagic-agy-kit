#!/usr/bin/env python3
"""Comprehensive end-to-end verification suite for WhiteMagic AGY Turbo Kit v2.0.0 (Wave 3)."""
import json
import os
import shutil
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
POST_HOOK = _HOOKS_DIR / "post_tool_sim.py"
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


def test_tricameral_v31():
    print("\n4. Tricameral CyberBrain v3.1.0 (wm-models, MCP, Kekkai fail-open & portability):")
    p = subprocess.run([str(LOCAL_BIN / "wm-models"), "list", "--json"], capture_output=True, text=True)
    ok_models = False
    if p.returncode == 0:
        try:
            md = json.loads(p.stdout)
            ids = {m["id"] for m in md.get("models", [])}
            ok_models = {"huginn-1", "muninn-4", "valkyrie-m3"}.issubset(ids)
        except Exception:
            pass
    check("wm-models list --json returns all 3 Tricameral organs", ok_models)

    mcp_in = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"tricameral_dispatch","arguments":{"action":"status"}}}\n'
    )
    p = subprocess.run([str(LOCAL_BIN / "wm-tricameral-mcp")], input=mcp_in, capture_output=True, text=True)
    lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
    check("wm-tricameral-mcp responds to initialize + tools/list + status", p.returncode == 0 and len(lines) == 3 and "tricameral_dispatch" in lines[1] and "huginn-1" in lines[2])

    # PreToolUse Kekkai check must fail-open cleanly when disabled or socket missing
    rc, out, _, ms = run_hook(
        PRE_HOOK,
        {"toolCall": {"name": "run_command", "args": {"CommandLine": "cat ~/.ssh/id_ed25519"}}},
        env_extra={"WM_HUGINN_KEKKAI_OFF": "1"},
    )
    check("PreToolUse Kekkai respects WM_HUGINN_KEKKAI_OFF=1 fail-open", rc == 0 and json.loads(out).get("decision") == "allow", f"{ms:.1f}ms")

    rc, out, _, ms = run_hook(
        PRE_HOOK,
        {"toolCall": {"name": "run_command", "args": {"CommandLine": "cat ~/.ssh/id_ed25519"}}},
        env_extra={"WM_TRICAMERAL_SOCK_DIR": "/tmp/nonexistent-wm-sock-dir-test"},
    )
    check("PreToolUse Kekkai fails-open when daemon socket absent", rc == 0 and json.loads(out).get("decision") == "allow", f"{ms:.1f}ms")

    # Portability hygiene: zero machine-specific absolute user/media paths in bin/, plugin/, models/
    import re as _re
    personal_re = _re.compile(r"/(?:home|media|Users)/[A-Za-z0-9._-]+/(?:Desktop|SD_CARD|\.local|\.gemini)")
    leaked = []
    for sub in (KIT / "bin", KIT / "plugin", KIT / "models"):
        for f in sub.rglob("*"):
            if f.is_file() and f.suffix not in (".pyc",):
                txt = f.read_text(encoding="utf-8", errors="ignore")
                if personal_re.search(txt):
                    leaked.append(str(f.relative_to(KIT)))
    check("Zero hardcoded personal paths across bin/, plugin/, models/", len(leaked) == 0, f"leaked={leaked}")


def test_v4_fusion():
    print("\n5. Fused Turbo+Fast v4.0.0 (python3 -m, 2>&1, post_tool_sim, unified SessionStart, 5-tool MCP):")
    sq = LOCAL_BIN / "wm-squeeze"
    p_ok = subprocess.run([str(sq), "--safe", "--", "python3", "-m", "pytest", "--version"], capture_output=True, text=True)
    check("wm-squeeze --safe allows python3 -m pytest", p_ok.returncode == 0)
    p_bad = subprocess.run([str(sq), "--safe", "--", "python3", "-c", "print(1)"], capture_output=True, text=True)
    check("wm-squeeze --safe refuses python3 -c (rc=2)", p_bad.returncode == 2)

    for cmd in ("cargo test 2>&1", "go test ./...", "python3 -m pytest -q"):
        rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
        d = json.loads(out)
        rewritten = (d.get("overwrite") or {}).get("CommandLine", "")
        check(f"pre_tool_guard rewrites '{cmd}'", rc == 0 and "--safe" in rewritten)

    for tool_name in ("mcp_whitemagic-agy-kit_fast_run_checks", "mcp_whitemagic-agy-kit_fast_apply_edits"):
        rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": tool_name, "args": {"verify": "rm -rf /"}}})
        check(f"{tool_name} destructive guard -> force_ask", rc == 0 and json.loads(out).get("decision") == "force_ask")

    with tempfile.NamedTemporaryFile(suffix=".jsonl") as tf:
        rc, out, _, _ = run_hook(
            POST_HOOK,
            {"toolCall": {"name": "mcp_whitemagic-agy-kit_fast_apply_edits", "args": {"edits": [{"path": "foo.py"}]}}, "error": "boom"},
            env_extra={"WM_SIM_LEDGER": tf.name},
        )
        logged = Path(tf.name).read_text()
        check("post_tool_sim logs rich cmd_preview for apply_edits", rc == 0 and "apply_edits: foo.py" in logged)

    rc, out, _, _ = run_hook(START_HOOK, {"cwd": str(KIT)})
    msg_repo = json.loads(out).get("injectSteps", [{}])[0].get("systemMessage", {}).get("systemMessage", "")
    check("unified SessionStart repo snapshot <=2550B", rc == 0 and 0 < len(msg_repo.encode("utf-8")) <= 2550 and "[Workspace Snapshot]" in msg_repo and "Git:" in msg_repo)

    rc, out, _, _ = run_hook(START_HOOK, {"cwd": str(Path.home())})
    msg_home = json.loads(out).get("injectSteps", [{}])[0].get("systemMessage", {}).get("systemMessage", "")
    check("unified SessionStart suppresses $HOME noise", rc == 0 and "[Workspace] ~" in msg_home and "Music/" not in msg_home)

    node_bin = LOCAL_BIN / "node"
    if not node_bin.exists():
        node_bin = Path(shutil.which("node") or "node")
    mcp_js = KIT / "plugin" / "dist" / "mcp-server.mjs"
    with tempfile.TemporaryDirectory() as td:
        tfile = Path(td) / "demo.py"
        tfile.write_text("\n".join(f"x{i} = {i}" for i in range(20)) + "\ntarget_val = 1\n", encoding="utf-8")
        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "antigravity-cli", "version": "1.5.15"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "code_graph", "arguments": {"mode": "overview", "root": str(KIT / "tools" / "wm-squeeze")}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "apply_edits", "arguments": {"cwd": td, "edits": [{"path": "demo.py", "hunks": [{"target": "target_val = 1", "replacement": "target_val = 2", "startLine": 1, "endLine": 5}]}]}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "run_checks", "arguments": {"cwd": td, "outputMode": "compact", "commands": [{"name": "ls", "command": "ls"}]}}},
        ]
        p = subprocess.run([str(node_bin), str(mcp_js)], input="\n".join(json.dumps(r) for r in reqs) + "\n", capture_output=True, text=True)
        resps = {r["id"]: r for r in (json.loads(ln) for ln in p.stdout.splitlines() if ln.strip())}
        check("fused MCP initialize v4.0.0", resps[1]["result"]["serverInfo"]["name"] == "whitemagic-agy-fast" and resps[1]["result"]["serverInfo"]["version"] == "4.0.0")
        tnames = [t["name"] for t in resps[2]["result"]["tools"]]
        check("fused MCP tools/list has all 5 tools", tnames == ["batch_search", "apply_edits", "read_slices", "run_checks", "code_graph"])
        check("fused MCP code_graph overview", not resps[3]["result"]["isError"] and "main.rs" in resps[3]["result"]["content"][0]["text"])
        check("fused MCP apply_edits auto-relocate", not resps[4]["result"]["isError"] and "auto-relocated" in resps[4]["result"]["content"][0]["text"])
        check("fused MCP run_checks squeezed", not resps[5]["result"]["isError"] and "squeezed" in resps[5]["result"]["content"][0]["text"])

    # Phase 3C: manage_task anti-poll guard + wm-grok-swarm/mcp + wm-vps + wm-triage
    poll_conv = f"polltest-{int(time.time())}"
    for step_i, expected_dec in ((1, "allow"), (2, "allow"), (3, "deny")):
        rc, out, _, _ = run_hook(
            PRE_HOOK,
            {"conversationId": poll_conv, "stepIdx": step_i, "toolCall": {"name": "manage_task", "args": {"Action": "status", "TaskId": "t-1"}}},
        )
        check(f"manage_task(status) poll #{step_i} -> {expected_dec}", rc == 0 and json.loads(out).get("decision") == expected_dec)
    rc, out, _, _ = run_hook(
        PRE_HOOK,
        {"conversationId": poll_conv, "stepIdx": 4, "toolCall": {"name": "manage_task", "args": {"Action": "list"}}},
    )
    check("manage_task(list) not blocked by anti-poll guard", rc == 0 and json.loads(out).get("decision") == "allow")

    p_gs_help = subprocess.run([str(LOCAL_BIN / "wm-grok-swarm"), "--help"], capture_output=True, text=True)
    check("wm-grok-swarm --help", p_gs_help.returncode == 0 and "status" in p_gs_help.stdout)
    p_gs_bad = subprocess.run(
        [str(LOCAL_BIN / "wm-grok-swarm"), "run", "--json-stdin"],
        input=json.dumps({"action": "ask", "model": "claude-opus", "tasks": [{"prompt": "hi"}]}),
        capture_output=True,
        text=True,
    )
    check("wm-grok-swarm rejects non-grok model", p_gs_bad.returncode != 0 and "Unsupported Grok 4.7 model" in p_gs_bad.stdout)

    gmcp_in = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"grok_dispatch","arguments":{"action":"status"}}}\n'
    )
    p_gmcp = subprocess.run([str(LOCAL_BIN / "wm-grok-mcp")], input=gmcp_in, capture_output=True, text=True)
    glines = [json.loads(ln) for ln in p_gmcp.stdout.splitlines() if ln.strip()]
    check("wm-grok-mcp initialize + tools/list + status", p_gmcp.returncode == 0 and len(glines) == 3 and glines[1]["result"]["tools"][0]["name"] == "grok_dispatch")

    p_vps = subprocess.run([str(LOCAL_BIN / "wm-vps"), "--help"], capture_output=True, text=True)
    check("wm-vps --help", p_vps.returncode == 0 and "health" in p_vps.stdout)

    p_tri = subprocess.run([str(LOCAL_BIN / "wm-triage"), "--dry-run"], capture_output=True, text=True)
    check("wm-triage --dry-run <= 4500B", p_tri.returncode == 0 and 0 < len(p_tri.stdout.encode("utf-8")) <= 4500 and "WhiteMagic Estate Triage" in p_tri.stdout)


def test_bwrap_confinement():
    print("\n6. bwrap Confinement for wm-grok-swarm & wm-grok-mcp:")
    import importlib.machinery
    import importlib.util
    import socket

    gmcp_in = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'
    p_gmcp = subprocess.run([str(LOCAL_BIN / "wm-grok-mcp")], input=gmcp_in, capture_output=True, text=True)
    glines = [json.loads(ln) for ln in p_gmcp.stdout.splitlines() if ln.strip()]
    schema = glines[0]["result"]["tools"][0]["inputSchema"]
    top_props = schema.get("properties", {})
    task_props = top_props.get("tasks", {}).get("items", {}).get("properties", {})
    check(
        "wm-grok-mcp schema exposes confined + verify_cmd (top-level and per-task)",
        p_gmcp.returncode == 0
        and {"confined", "verify_cmd"}.issubset(top_props.keys())
        and {"confined", "verify_cmd"}.issubset(task_props.keys()),
    )

    swarm_path = str(KIT / "bin" / "wm-grok-swarm")
    loader = importlib.machinery.SourceFileLoader("wm_grok_swarm_mod", swarm_path)
    spec = importlib.util.spec_from_loader("wm_grok_swarm_mod", loader)
    swarm_mod = importlib.util.module_from_spec(spec)
    loader.exec_module(swarm_mod)

    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "wt"
        wt.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=str(wt), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(wt), check=True)
        (wt / "hello.txt").write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", "hello.txt"], cwd=str(wt), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(wt), check=True)

        # 1) Fail-closed when WM_BWRAP_BIN points to nonexistent binary
        old_bwrap_env = os.environ.get("WM_BWRAP_BIN")
        try:
            os.environ["WM_BWRAP_BIN"] = "/nonexistent/bwrap"
            fc_verify = swarm_mod.run_confined_verify(wt, "echo should-not-run", 15)
            fc_swarm = swarm_mod.run_swarm(
                {
                    "action": "worktree",
                    "confined": True,
                    "verify_cmd": "echo should-not-run",
                    "tasks": [{"name": "t1", "prompt": "noop", "cwd": str(wt)}],
                }
            )
            check(
                "fail-closed when WM_BWRAP_BIN missing (verify & run_swarm)",
                not fc_verify.get("ok")
                and fc_verify.get("confinement_receipt", {}).get("scope_confined") is False
                and not fc_swarm.get("ok")
                and fc_swarm.get("workers", [{}])[0].get("confinement_receipt", {}).get("scope_confined") is False,
            )
        finally:
            if old_bwrap_env is None:
                os.environ.pop("WM_BWRAP_BIN", None)
            else:
                os.environ["WM_BWRAP_BIN"] = old_bwrap_env

        # 2) Live bwrap confinement checks
        bwrap_bin = swarm_mod.find_bwrap()
        check("bwrap binary available for live confinement test", bool(bwrap_bin))
        if bwrap_bin:
            # 2a) Write inside wt succeeds & receipt has all 6 required keys
            res_in = swarm_mod.run_confined_verify(wt, "touch confined_inside.txt && test -f confined_inside.txt", 30)
            rcpt = res_in.get("confinement_receipt", {})
            req_keys = {
                "bwrap_bin",
                "network_isolated",
                "worktree_path",
                "worktree_diff_sha256",
                "verify_exit_code",
                "scope_confined",
            }
            check(
                "bwrap allows write inside wt_path & emits 6-key confinement_receipt",
                res_in.get("ok") is True
                and (wt / "confined_inside.txt").is_file()
                and req_keys.issubset(rcpt.keys())
                and rcpt.get("network_isolated") is True
                and rcpt.get("scope_confined") is True
                and rcpt.get("verify_exit_code") == 0
                and len(str(rcpt.get("worktree_diff_sha256") or "")) == 64,
            )

            # 2b) Write outside wt_path to a real host path outside /tmp fails read-only
            cache_dir = Path.home() / ".cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            outside_dir = Path(tempfile.mkdtemp(prefix="wm-bwrap-ro-test-", dir=str(cache_dir)))
            outside_file = outside_dir / "escape.txt"
            try:
                res_out = swarm_mod.run_confined_verify(wt, f"touch '{outside_file}'", 30)
                err_tail = (res_out.get("confinement_receipt", {}).get("verify_stderr_tail") or "").lower()
                check(
                    "bwrap blocks write outside wt_path (read-only host path)",
                    not res_out.get("ok")
                    and res_out.get("confinement_receipt", {}).get("verify_exit_code", 0) != 0
                    and not outside_file.exists()
                    and ("read-only" in err_tail or "permission denied" in err_tail),
                    detail=err_tail.strip().splitlines()[-1] if err_tail.strip() else "",
                )
            finally:
                shutil.rmtree(outside_dir, ignore_errors=True)

            # 2c) --unshare-net isolates network namespace from host loopback listener
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                srv.bind(("127.0.0.1", 0))
                srv.listen(1)
                port = srv.getsockname()[1]
                net_probe_cmd = (
                    "python3 -c 'import socket, sys; "
                    "s = socket.socket(); s.settimeout(1.0); "
                    f"sys.exit(0 if s.connect_ex((\"127.0.0.1\", {port})) != 0 else 1)'"
                )
                res_net = swarm_mod.run_confined_verify(wt, net_probe_cmd, 30)
                check(
                    "bwrap --unshare-net isolates network from host loopback",
                    res_net.get("ok") is True
                    and res_net.get("confinement_receipt", {}).get("network_isolated") is True,
                )
            finally:
                srv.close()

            # 2d) External worktree backed by a /tmp repo ro-binds /tmp/.git safely
            tmp_repo = Path(tempfile.mkdtemp(prefix="wm-bwrap-tmp-git-", dir="/tmp")).resolve()
            ext_parent = Path(tempfile.mkdtemp(prefix="wm-bwrap-ext-wt-", dir=str(cache_dir)))
            ext_wt = ext_parent / "wt1"
            try:
                subprocess.run(["git", "init", "-q"], cwd=str(tmp_repo), check=True)
                subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=str(tmp_repo), check=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_repo), check=True)
                (tmp_repo / "README.md").write_text("tmp-repo\n", encoding="utf-8")
                subprocess.run(["git", "add", "README.md"], cwd=str(tmp_repo), check=True)
                subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_repo), check=True)
                subprocess.run(["git", "-C", str(tmp_repo), "worktree", "add", "-q", str(ext_wt)], check=True)

                res_tmp_git = swarm_mod.run_confined_verify(
                    ext_wt,
                    "git status --short && git rev-parse --is-inside-work-tree",
                    30,
                )
                rcpt_tmp = res_tmp_git.get("confinement_receipt", {})
                tmp_git_dir = str((tmp_repo / ".git").resolve())
                bflags = rcpt_tmp.get("bwrap_flags", [])
                has_ro_bind = any(
                    bflags[i] == "--ro-bind" and bflags[i + 1] == tmp_git_dir
                    for i in range(len(bflags) - 1)
                )
                check(
                    "bwrap ro-binds external /tmp parent .git for git status in worktree",
                    res_tmp_git.get("ok") is True
                    and rcpt_tmp.get("verify_exit_code") == 0
                    and "true" in (rcpt_tmp.get("verify_stdout_tail") or "")
                    and has_ro_bind,
                    detail=rcpt_tmp.get("verify_stderr_tail", ""),
                )

                probe_escape = Path(tmp_git_dir) / "ro_escape_probe"
                res_ro_probe = swarm_mod.run_confined_verify(
                    ext_wt,
                    f"touch '{probe_escape}'",
                    30,
                )
                check(
                    "bwrap blocks writes to ro-bound /tmp parent .git directory",
                    res_ro_probe.get("ok") is False
                    and res_ro_probe.get("confinement_receipt", {}).get("verify_exit_code", 0) != 0
                    and not probe_escape.exists(),
                )
            finally:
                subprocess.run(
                    ["git", "-C", str(tmp_repo), "worktree", "remove", "--force", str(ext_wt)],
                    capture_output=True,
                )
                shutil.rmtree(ext_parent, ignore_errors=True)
                shutil.rmtree(tmp_repo, ignore_errors=True)


def test_signed_confinement_receipts():
    print("\n7. Signed Causal Provenance Receipts (wm-confinement-receipt/1.1):")
    import base64
    import importlib.machinery
    import importlib.util
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    # 7a) wm-grok-mcp schema exposes sign_receipt, sign_alg, receipt_log
    gmcp_in = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'
    p_gmcp = subprocess.run([str(LOCAL_BIN / "wm-grok-mcp")], input=gmcp_in, capture_output=True, text=True)
    glines = [json.loads(ln) for ln in p_gmcp.stdout.splitlines() if ln.strip()]
    schema = glines[0]["result"]["tools"][0]["inputSchema"]
    top_props = schema.get("properties", {})
    task_props = top_props.get("tasks", {}).get("items", {}).get("properties", {})
    check(
        "wm-grok-mcp schema exposes sign_receipt, sign_alg, receipt_log",
        p_gmcp.returncode == 0
        and {"sign_receipt", "sign_alg", "receipt_log"}.issubset(top_props.keys())
        and top_props["sign_alg"].get("enum") == ["auto", "hmac-sha256", "ed25519"]
        and {"sign_receipt", "sign_alg"}.issubset(task_props.keys()),
    )

    swarm_path = str(KIT / "bin" / "wm-grok-swarm")
    loader = importlib.machinery.SourceFileLoader("wm_grok_swarm_signed_mod", swarm_path)
    spec = importlib.util.spec_from_loader("wm_grok_swarm_signed_mod", loader)
    swarm_mod = importlib.util.module_from_spec(spec)
    loader.exec_module(swarm_mod)

    env_keys = (
        "WM_CONFINEMENT_HMAC_KEY",
        "WM_CONFINEMENT_HMAC_KEY_FILE",
        "WM_CONFINEMENT_ED25519_KEY_FILE",
        "WM_CONFINEMENT_ED25519_PUBKEY_FILE",
        "WM_CONFINEMENT_SIGN",
        "WM_CONFINEMENT_RECEIPT_LOG",
    )
    saved_env = {k: os.environ.get(k) for k in env_keys}

    def _clear_receipt_env():
        for k in env_keys:
            os.environ.pop(k, None)

    try:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            wt = td_path / "wt"
            wt.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(wt), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=str(wt), check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(wt), check=True)
            (wt / "app.py").write_text("print('ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=str(wt), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(wt), check=True)

            # 7b) Fail-closed when signing requested without key (run_confined_verify & run_swarm & ed25519)
            _clear_receipt_env()
            fc_no_key = swarm_mod.run_confined_verify(wt, "echo nope", 15, sign_receipt=True)
            os.environ["WM_CONFINEMENT_SIGN"] = "1"
            fc_env_no_key = swarm_mod.run_confined_verify(wt, "echo nope", 15)
            fc_swarm_no_key = swarm_mod.run_swarm(
                {
                    "action": "worktree",
                    "confined": True,
                    "sign_receipt": True,
                    "verify_cmd": "echo nope",
                    "tasks": [{"name": "w1", "prompt": "noop", "cwd": str(wt)}],
                }
            )
            _clear_receipt_env()
            os.environ["WM_CONFINEMENT_HMAC_KEY"] = "hmac-only-key"
            fc_ed_no_key = swarm_mod.run_confined_verify(wt, "echo nope", 15, sign_alg="ed25519")
            _clear_receipt_env()
            os.environ["WM_CONFINEMENT_HMAC_KEY_FILE"] = str(td_path / "missing.key")
            fc_bad_file = swarm_mod.run_confined_verify(wt, "echo nope", 15)
            check(
                "fail-closed when signing requested without key or invalid key file",
                not fc_no_key.get("ok")
                and fc_no_key.get("confinement_receipt", {}).get("receipt_version") == "wm-confinement-receipt/1.1"
                and fc_no_key.get("confinement_receipt", {}).get("signature_alg") is None
                and fc_no_key.get("confinement_receipt", {}).get("signature_b64") is None
                and not fc_env_no_key.get("ok")
                and not fc_swarm_no_key.get("ok")
                and fc_swarm_no_key.get("workers", [{}])[0].get("confinement_receipt", {}).get("signature_b64") is None
                and not fc_ed_no_key.get("ok")
                and not fc_bad_file.get("ok"),
            )

            # 7c) Canonical HMAC-SHA256 signing & tamper detection (raw, hex:, base64:)
            _clear_receipt_env()
            raw_secret = b"\x11\x22\x33\x44hmac-test-secret-key-32bytes!!"
            for label, env_val in (
                ("utf8", "test-utf8-hmac-secret"),
                ("hex", "hex:" + raw_secret.hex()),
                ("base64", "base64:" + base64.b64encode(raw_secret).decode("ascii")),
            ):
                os.environ["WM_CONFINEMENT_HMAC_KEY"] = env_val
                res_hmac = swarm_mod.run_confined_verify(wt, "echo signed-ok", 20)
                rcpt = res_hmac.get("confinement_receipt", {})
                v_ok, v_msg = swarm_mod.verify_confinement_receipt(rcpt)
                check(
                    f"HMAC-SHA256 sign + verify ({label}) with full v1.1 fields",
                    res_hmac.get("ok") is True
                    and v_ok is True
                    and rcpt.get("receipt_version") == "wm-confinement-receipt/1.1"
                    and str(rcpt.get("issued_at_utc") or "").endswith("Z")
                    and len(str(rcpt.get("verify_cmd_sha256") or "")) == 64
                    and isinstance(rcpt.get("bwrap_flags"), list)
                    and "--unshare-net" in rcpt.get("bwrap_flags", [])
                    and rcpt.get("signature_alg") == "hmac-sha256"
                    and bool(rcpt.get("signature_b64")),
                    detail=v_msg,
                )

            # Tamper detection across critical fields + algorithm downgrade
            os.environ["WM_CONFINEMENT_HMAC_KEY"] = "tamper-test-secret"
            res_t = swarm_mod.run_confined_verify(wt, "echo tamper-check", 20)
            base_rcpt = res_t["confinement_receipt"]
            tamper_cases = {
                "worktree_diff_sha256": "0" * 64,
                "verify_cmd_sha256": "f" * 64,
                "verify_exit_code": 99,
                "bwrap_flags": ["--ro-bind", "/", "/"],
                "network_isolated": False,
                "signature_alg": "ed25519",
            }
            all_tamper_rejected = True
            failed_field = ""
            for field, bad_val in tamper_cases.items():
                mut = dict(base_rcpt)
                mut[field] = bad_val
                tok, _ = swarm_mod.verify_confinement_receipt(mut, hmac_key="tamper-test-secret")
                if tok:
                    all_tamper_rejected = False
                    failed_field = field
                    break
            check("HMAC tamper detection rejects all mutated provenance fields", all_tamper_rejected, detail=failed_field)

            # 7d) Ed25519 signing (PEM PKCS8 + raw 32-byte), CLI verify-receipt & tamper detection
            _clear_receipt_env()
            priv = ed25519.Ed25519PrivateKey.generate()
            pub = priv.public_key()
            pem_priv_path = td_path / "ed25519_priv.pem"
            pem_pub_path = td_path / "ed25519_pub.pem"
            raw_priv_path = td_path / "ed25519_priv.bin"
            pem_priv_path.write_bytes(
                priv.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            pem_pub_path.write_bytes(
                pub.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            raw_priv_path.write_bytes(
                priv.private_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PrivateFormat.Raw,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )

            os.environ["WM_CONFINEMENT_ED25519_KEY_FILE"] = str(pem_priv_path)
            res_ed = swarm_mod.run_confined_verify(wt, "echo ed25519-pem", 20, sign_alg="ed25519")
            ed_rcpt = res_ed.get("confinement_receipt", {})
            ed_ok, ed_msg = swarm_mod.verify_confinement_receipt(ed_rcpt, ed25519_public_key=pem_pub_path)
            tampered_ed = dict(ed_rcpt)
            tampered_ed["verify_exit_code"] = 42
            ed_tamper_ok, _ = swarm_mod.verify_confinement_receipt(tampered_ed, ed25519_public_key=pem_pub_path)

            p_vr = subprocess.run(
                [str(LOCAL_BIN / "wm-grok-swarm"), "verify-receipt", "--file", "-", "--pubkey-file", str(pem_pub_path)],
                input=json.dumps(ed_rcpt),
                capture_output=True,
                text=True,
            )
            p_vr_bad = subprocess.run(
                [str(LOCAL_BIN / "wm-grok-swarm"), "verify-receipt", "--file", "-", "--pubkey-file", str(pem_pub_path)],
                input=json.dumps(tampered_ed),
                capture_output=True,
                text=True,
            )

            os.environ["WM_CONFINEMENT_ED25519_KEY_FILE"] = str(raw_priv_path)
            res_ed_raw = swarm_mod.run_confined_verify(wt, "echo ed25519-raw", 20)
            ed_raw_ok, _ = swarm_mod.verify_confinement_receipt(
                res_ed_raw.get("confinement_receipt", {}),
                ed25519_public_key=pub,
            )
            check(
                "Ed25519 PEM + raw-32B sign, verify-receipt CLI, and tamper rejection",
                res_ed.get("ok") is True
                and ed_rcpt.get("signature_alg") == "ed25519"
                and ed_ok is True
                and not ed_tamper_ok
                and p_vr.returncode == 0
                and json.loads(p_vr.stdout).get("ok") is True
                and p_vr_bad.returncode != 0
                and res_ed_raw.get("ok") is True
                and ed_raw_ok is True,
                detail=ed_msg,
            )

            # 7e) Append-only WM_CONFINEMENT_RECEIPT_LOG ledger writes & line-by-line verification
            _clear_receipt_env()
            ledger_file = td_path / "logs" / "receipts.jsonl"
            os.environ["WM_CONFINEMENT_HMAC_KEY"] = "ledger-hmac-secret"
            os.environ["WM_CONFINEMENT_RECEIPT_LOG"] = str(ledger_file)
            swarm_mod.run_confined_verify(wt, "echo entry-1", 20)
            swarm_mod.run_confined_verify(wt, "echo entry-2", 20)
            lines = [ln for ln in ledger_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            all_lines_ok = len(lines) == 2 and all(
                swarm_mod.verify_confinement_receipt(json.loads(ln))[0] for ln in lines
            )
            check(
                "WM_CONFINEMENT_RECEIPT_LOG appends locked JSONL records and verifies line-by-line",
                all_lines_ok,
                detail=f"{len(lines)} lines",
            )
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 7f) Portability scan: zero /home/lucas literals across git-tracked bin/, plugin/, models/
    tracked = subprocess.run(
        ["git", "-C", str(KIT), "ls-files", "bin", "plugin", "models"],
        capture_output=True,
        text=True,
        check=True,
    )
    leaked_paths = []
    for rel in tracked.stdout.splitlines():
        fp = KIT / rel
        if fp.is_file():
            try:
                if "/home/lucas" in fp.read_text(encoding="utf-8", errors="ignore"):
                    leaked_paths.append(rel)
            except OSError:
                pass
    check("Zero hardcoded /home/lucas paths in git-tracked bin/, plugin/, models/", not leaked_paths, detail=", ".join(leaked_paths))


def test_remote_confinement_attestation():
    print("\n=== Test 8: Remote RFC 9421 Confinement Attestation Loop ===")
    import base64
    import hashlib
    import hmac
    import http.server
    import importlib.machinery
    import importlib.util
    import re
    import socketserver
    import threading

    swarm_path = KIT / "bin" / "wm-grok-swarm"
    loader = importlib.machinery.SourceFileLoader("wm_grok_swarm_attest_test", str(swarm_path))
    spec = importlib.util.spec_from_loader("wm_grok_swarm_attest_test", loader)
    swarm_mod = importlib.util.module_from_spec(spec)
    loader.exec_module(swarm_mod)

    mcp_path = KIT / "bin" / "wm-grok-mcp"
    mcp_loader = importlib.machinery.SourceFileLoader("wm_grok_mcp_test", str(mcp_path))
    mcp_spec = importlib.util.spec_from_loader("wm_grok_mcp_test", mcp_loader)
    mcp_mod = importlib.util.module_from_spec(mcp_spec)
    mcp_loader.exec_module(mcp_mod)

    # 8a) Schema check on wm-grok-mcp (top-level & task-level)
    top_props = mcp_mod.TOOL_SCHEMA["inputSchema"]["properties"]
    task_props = top_props["tasks"]["items"]["properties"]
    check(
        "wm-grok-mcp schema exposes attest_remote & attest_url at top-level and task-level",
        top_props.get("attest_remote", {}).get("type") == "boolean"
        and top_props.get("attest_url", {}).get("type") == "string"
        and task_props.get("attest_remote", {}).get("type") == "boolean"
        and task_props.get("attest_url", {}).get("type") == "string",
    )

    tap_secret = b"mock-tap-rfc9421-hmac-secret-32b"
    tap_kid = "wm-mock-agent-1"
    receipt_hmac_secret = b"mock-receipt-hmac-secret-key-32b"
    state = {"mode": "valid", "seen": []}

    class _MockAttestHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw_body.decode("utf-8"))

            sig_input = self.headers.get("Signature-Input") or ""
            sig_hdr = self.headers.get("Signature") or ""
            trial_hdr = self.headers.get("X-WM-Trial") or ""
            host_hdr = self.headers.get("Host") or ""

            # Verify RFC 9421 signature against tap_secret with @path stripped of ?receipt=1
            m_in = re.match(r'^sig1=(\([^)]+\).*)$', sig_input.strip())
            m_sig = re.match(r'^sig1=:([^:]+):$', sig_hdr.strip())
            rfc_ok = False
            if m_in and m_sig:
                sig_params = m_in.group(1)
                pure_path = self.path.split("?", 1)[0] or "/"
                sig_base = (
                    f'"@method": POST\n'
                    f'"@path": {pure_path}\n'
                    f'"@authority": {host_hdr}\n'
                    f'"@signature-params": {sig_params}'
                ).encode("utf-8")
                expected = hmac.new(tap_secret, sig_base, hashlib.sha256).digest()
                got = base64.b64decode(m_sig.group(1), validate=True)
                rfc_ok = (
                    '"@method" "@path" "@authority"' in sig_params
                    and f'keyid="{tap_kid}"' in sig_params
                    and "?receipt=1" not in sig_base.decode("utf-8")
                    and hmac.compare_digest(expected, got)
                )

            state["seen"].append({
                "path": self.path,
                "trial_hdr": trial_hdr,
                "rfc_ok": rfc_ok,
                "has_receipt": isinstance(body.get("receipt"), dict),
            })

            mode = state["mode"]
            if mode == "http500":
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"internal error"}')
                return

            if mode == "invalid":
                resp = {
                    "valid": False,
                    "verdict": "UNTRUSTED",
                    "signature_verified": False,
                    "errors": ["mock signature rejected"],
                }
            else:
                resp = {
                    "valid": True,
                    "verdict": "TRUSTED",
                    "signature_verified": True,
                    "policy_ok": True,
                    "verification_receipt": {
                        "receipt_version": "cpr-0.5",
                        "verdict": "TRUSTED",
                    },
                }
            encoded = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if rfc_ok:
                self.send_header("X-WM-Agent-Verified", f"1; keyid={tap_kid}")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = _ThreadingHTTPServer(("127.0.0.1", 0), _MockAttestHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    saved_env = {
        k: os.environ.get(k)
        for k in (
            "WM_TRUSTED_AGENT_KEY_FILE",
            "WM_TRUSTED_AGENT_KEY_ID",
            "WM_CONFINEMENT_ATTEST_URL",
            "WM_CONFINEMENT_HMAC_KEY",
            "WM_CONFINEMENT_SIGN",
            "WM_CONFINEMENT_RECEIPT_LOG",
        )
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            key_json = td_path / "trusted_agent_key.json"
            key_json.write_text(
                json.dumps(
                    {
                        tap_kid: {
                            "alg": "hmac-sha256",
                            "secret": "hex:" + tap_secret.hex(),
                            "agent": "mock-agent",
                            "enabled": True,
                        }
                    }
                ),
                encoding="utf-8",
            )
            os.environ["WM_TRUSTED_AGENT_KEY_FILE"] = str(key_json)
            os.environ.pop("WM_TRUSTED_AGENT_KEY_ID", None)
            os.environ["WM_CONFINEMENT_HMAC_KEY"] = "hex:" + receipt_hmac_secret.hex()
            os.environ.pop("WM_CONFINEMENT_SIGN", None)

            rcpt = swarm_mod._build_confinement_receipt(
                bwrap_bin="/usr/bin/bwrap",
                network_isolated=True,
                worktree_path=str(td_path),
                worktree_diff_sha256="a" * 64,
                verify_exit_code=0,
                scope_confined=True,
                verify_cmd="echo ok",
                bwrap_flags=["--ro-bind", "/", "/", "--unshare-net", "--die-with-parent"],
            )
            signed_rcpt = swarm_mod.sign_confinement_receipt(rcpt, sign_alg="hmac-sha256")
            mock_url = f"http://127.0.0.1:{port}/verify-confinement?receipt=1"

            # 8b) Valid remote attestation with RFC 9421 + X-WM-Trial: 1
            state["mode"] = "valid"
            att_ok = swarm_mod.attest_confinement_receipt(
                signed_rcpt,
                attest_url=mock_url,
                hmac_key=receipt_hmac_secret,
            )
            last_seen = state["seen"][-1]
            check(
                "RFC 9421 headers (@path strips ?receipt=1) + X-WM-Trial: 1 + TRUSTED receipt attached",
                att_ok.get("ok") is True
                and att_ok.get("valid") is True
                and att_ok.get("verdict") == "TRUSTED"
                and att_ok.get("signature_verified") is True
                and att_ok.get("agent_verified") == f"1; keyid={tap_kid}"
                and isinstance(att_ok.get("verification_receipt"), dict)
                and last_seen["rfc_ok"] is True
                and last_seen["trial_hdr"] == "1"
                and last_seen["has_receipt"] is True,
                detail=str(att_ok.get("error") or att_ok.get("verdict")),
            )

            # 8c) Fail closed on HTTP 500 and valid=False
            state["mode"] = "http500"
            att_500 = swarm_mod.attest_confinement_receipt(
                signed_rcpt,
                attest_url=mock_url,
                hmac_key=receipt_hmac_secret,
            )
            state["mode"] = "invalid"
            att_inv = swarm_mod.attest_confinement_receipt(
                signed_rcpt,
                attest_url=mock_url,
                hmac_key=receipt_hmac_secret,
            )
            check(
                "Remote attestation fails closed on HTTP 500 and valid=False",
                att_500.get("ok") is False
                and att_500.get("http_status") == 500
                and att_inv.get("ok") is False
                and att_inv.get("valid") is False,
            )

            # 8d) Missing trusted_agent_key.json degrades cleanly to {} (X-WM-Trial: 1 still sent)
            hdrs_missing = swarm_mod._sign_rfc9421_hmac_headers(
                "POST",
                "/verify-confinement?receipt=1",
                f"127.0.0.1:{port}",
                key_file=td_path / "does_not_exist.json",
            )
            state["mode"] = "valid"
            att_no_tap = swarm_mod.attest_confinement_receipt(
                signed_rcpt,
                attest_url=mock_url,
                hmac_key=receipt_hmac_secret,
                key_file=td_path / "does_not_exist.json",
            )
            check(
                "Missing trusted_agent_key.json returns {} cleanly and falls back to X-WM-Trial: 1",
                hdrs_missing == {} and att_no_tap.get("ok") is True and att_no_tap.get("agent_verified") is None,
            )

            # 8e) CLI verify-receipt --attest-remote end-to-end (pass & fail-closed)
            state["mode"] = "valid"
            p_cli_ok = subprocess.run(
                [
                    str(LOCAL_BIN / "wm-grok-swarm"),
                    "verify-receipt",
                    "--file",
                    "-",
                    "--hmac-key",
                    "hex:" + receipt_hmac_secret.hex(),
                    "--attest-remote",
                    "--attest-url",
                    mock_url,
                ],
                input=json.dumps(signed_rcpt),
                capture_output=True,
                text=True,
            )
            cli_ok_json = json.loads(p_cli_ok.stdout)

            state["mode"] = "invalid"
            p_cli_fail = subprocess.run(
                [
                    str(LOCAL_BIN / "wm-grok-swarm"),
                    "verify-receipt",
                    "--file",
                    "-",
                    "--hmac-key",
                    "hex:" + receipt_hmac_secret.hex(),
                    "--attest-remote",
                    "--attest-url",
                    mock_url,
                ],
                input=json.dumps(signed_rcpt),
                capture_output=True,
                text=True,
            )
            cli_fail_json = json.loads(p_cli_fail.stdout)
            check(
                "CLI verify-receipt --attest-remote passes on valid mock and fails closed on rejection",
                p_cli_ok.returncode == 0
                and cli_ok_json.get("ok") is True
                and cli_ok_json.get("remote_attestation", {}).get("verdict") == "TRUSTED"
                and cli_ok_json.get("remote_attestation", {}).get("agent_verified") == f"1; keyid={tap_kid}"
                and p_cli_fail.returncode != 0
                and cli_fail_json.get("ok") is False,
            )
    finally:
        srv.shutdown()
        srv.server_close()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_v41_token_burn_shield():
    print("\n8. v4.1.0 Token-Burn Shield (Wide-Span Clamp, Heredoc Squeeze, Telemetry, Skill Dedup):")
    with tempfile.TemporaryDirectory() as td:
        big = Path(td) / "big400.py"
        big.write_text("\n".join(f"line_{i} = 'padding text here for line length test'" for i in range(420)), encoding="utf-8")
        tr_hi = Path(td) / "tr_130k.jsonl"
        tr_hi.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 15000, "cache_read_tokens": 115000, "model": "gemini-4-hydrogen-xhigh"}) + "\n")
        tr_lo = Path(td) / "tr_40k.jsonl"
        tr_lo.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 8000, "cache_read_tokens": 32000, "model": "gemini-4-hydrogen-xhigh"}) + "\n")

        # Wide explicit span (1..800) at <120k -> clamped to EndLine=200
        rc, out, _, _ = run_hook(
            PRE_HOOK,
            {"conversationId": "v41-wide-lo", "stepIdx": 1, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big), "StartLine": 1, "EndLine": 800}}},
        )
        d = json.loads(out)
        check("wide span 1..800 (<120k) -> EndLine=200", rc == 0 and d.get("overwrite") == {"EndLine": 200})

        # Wide explicit span (1..800) at >=120k -> clamped to EndLine=150
        rc, out, _, _ = run_hook(
            PRE_HOOK,
            {"conversationId": "v41-wide-hi", "stepIdx": 1, "transcriptPath": str(tr_hi), "toolCall": {"name": "read_file", "args": {"Path": str(big), "StartLine": 1, "EndLine": 800}}},
        )
        d = json.loads(out)
        check("wide span 1..800 (>=120k) -> EndLine=150", rc == 0 and d.get("overwrite") == {"EndLine": 150})

        # Unbounded tail (StartLine=50, EndLine=None) -> clamped to EndLine=249
        rc, out, _, _ = run_hook(
            PRE_HOOK,
            {"conversationId": "v41-tail", "stepIdx": 1, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big), "StartLine": 50}}},
        )
        d = json.loads(out)
        check("unbounded tail StartLine=50 -> EndLine=249", rc == 0 and d.get("overwrite") == {"EndLine": 249})

        # Normal 200-line slice untouched
        rc, out, _, _ = run_hook(
            PRE_HOOK,
            {"conversationId": "v41-norm", "stepIdx": 1, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big), "StartLine": 1, "EndLine": 200}}},
        )
        d = json.loads(out)
        check("normal 1..200 slice untouched", rc == 0 and "overwrite" not in d)

        # git ... 2>/dev/null -> squeezed via wm-squeeze --safe
        rc, out, _, _ = run_hook(
            PRE_HOOK,
            {"toolCall": {"name": "run_command", "args": {"CommandLine": f"git -C {td} status --short 2>/dev/null"}}},
        )
        d = json.loads(out)
        check("git status 2>/dev/null -> wm-squeeze --safe", rc == 0 and "--safe" in (d.get("overwrite") or {}).get("CommandLine", ""))

        # Multiline python3 heredoc -> rewritten with pipefail + wm-squeeze --pipe and executes cleanly
        heredoc_cmd = "python3 - <<'PY'\nfor i in range(400):\n    print(f'row {i}: ' + 'x' * 50)\nPY"
        rc, out, _, _ = run_hook(
            PRE_HOOK,
            {"toolCall": {"name": "run_command", "args": {"CommandLine": heredoc_cmd}}},
        )
        d = json.loads(out)
        rewritten = (d.get("overwrite") or {}).get("CommandLine", "")
        check("multiline python3 heredoc rewritten with pipefail + wm-squeeze", rc == 0 and "set -o pipefail;" in rewritten and "--pipe" in rewritten)
        p_exec = subprocess.run(["bash", "-c", rewritten], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"})
        check("rewritten heredoc caps stdout <=8.2KB & rc=0", p_exec.returncode == 0 and len(p_exec.stdout.encode("utf-8")) <= 8400 and "[wm-squeeze:" in p_exec.stdout)

        # Failing heredoc preserves non-zero exit code via pipefail
        fail_cmd = "python3 - <<'PY'\nimport sys; print('err'); sys.exit(3)\nPY"
        _, out_f, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": fail_cmd}}})
        rew_f = (json.loads(out_f).get("overwrite") or {}).get("CommandLine", "")
        p_fail = subprocess.run(["bash", "-c", rew_f], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"})
        check("rewritten failing heredoc preserves exit code 3", p_fail.returncode == 3)

        # Enhanced stop_telemetry metrics (model_flips, max_single_uncached, gaps_30s, compactions)
        tr_flip = Path(td) / "tr_flip.jsonl"
        tr_flip.write_text(
            "\n".join([
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 40000, "output_tokens": 200, "model": "gemini-4-neon-xhigh", "created_at": "2026-11-23T20:00:00Z"}),
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 150000, "cache_read_tokens": 0, "output_tokens": 300, "model": "gemini-4-hydrogen-xhigh", "created_at": "2026-11-23T20:01:10Z"}),
                json.dumps({"type": "CHECKPOINT", "content": "compaction"}),
            ]) + "\n",
            encoding="utf-8",
        )
        telem_out = Path(td) / "telem.jsonl"
        rc, out, _, _ = run_hook(
            STOP_HOOK,
            {"conversationId": "v41-stop", "transcriptPath": str(tr_flip), "terminationReason": "NO_TOOL_CALL", "cwd": td},
            env_extra={"WM_AGY_TELEMETRY": str(telem_out)},
        )
        rec = json.loads(telem_out.read_text().splitlines()[-1])
        check(
            "stop_telemetry emits model_flips/max_single_uncached/gaps_30s/compactions",
            rc == 0 and rec.get("model_flips") == 1 and rec.get("max_single_uncached") == 150000 and rec.get("gaps_30s") == 1 and rec.get("compactions") == 1,
        )

        # post_tool_sim spill capture
        spill_telem = Path(td) / "spill_telem.jsonl"
        rc, out, _, _ = run_hook(
            POST_HOOK,
            {"conversationId": "v41-spill", "toolCall": {"name": "run_command", "args": {"CommandLine": "cat big"}}, "result": "A" * 18000},
            env_extra={"WM_AGY_TELEMETRY": str(spill_telem)},
        )
        srec = json.loads(spill_telem.read_text().splitlines()[-1])
        check("post_tool_sim logs tool_spill >16KB", rc == 0 and srec.get("event") == "tool_spill" and srec.get("bytes") == 18000)

    # agy-token-audit --recent 3 --json
    p_audit = subprocess.run([str(LOCAL_BIN / "agy-token-audit"), "--recent", "3", "--json"], capture_output=True, text=True)
    ok_audit = False
    if p_audit.returncode == 0:
        try:
            aj = json.loads(p_audit.stdout)
            ok_audit = "aggregate" in aj and "sessions" in aj and len(aj["sessions"]) >= 1
        except Exception:
            pass
    check("agy-token-audit --recent 3 --json succeeds", ok_audit)

    # Zero duplicate skill names between ~/.gemini/config/skills/ and plugin/skills/
    user_sk = Path.home() / ".gemini" / "config" / "skills"
    plug_sk = Path.home() / ".gemini" / "config" / "plugins" / "whitemagic-agy-kit" / "skills"
    dups = set()
    if user_sk.is_dir() and plug_sk.is_dir():
        u_names = {p.name for p in user_sk.iterdir() if p.is_dir()}
        p_names = {p.name for p in plug_sk.iterdir() if p.is_dir()}
        dups = u_names & p_names
    check("zero duplicate skills between ~/.gemini/config/skills and plugin", len(dups) == 0, f"dups={sorted(dups)}")

    # Env-Write Guard replaces relative write_file(.env) sandbox rule portably
    rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "write_file", "args": {"Path": "/tmp/demo/.env", "Content": "SECRET=1"}}})
    check("write_file(.env) -> force_ask Env-Write Guard", rc == 0 and json.loads(out).get("decision") == "force_ask")
    rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "mcp_whitemagic-agy-kit_fast_apply_edits", "args": {"edits": [{"path": ".env.local", "hunks": []}]}}})
    check("apply_edits(.env.local) -> force_ask Env-Write Guard", rc == 0 and json.loads(out).get("decision") == "force_ask")
    rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "write_file", "args": {"Path": "/tmp/demo/.env.example", "Content": "KEY="}}})
    check("write_file(.env.example) -> allow", rc == 0 and json.loads(out).get("decision") == "allow")
    inst_txt = (KIT / "install.sh").read_text(encoding="utf-8")
    check("install.sh no longer appends invalid write_file(.env)", 'existing_deny.append("write_file(.env)")' not in inst_txt)


def test_v44_ide_longevity_shield():
    print("\n9. v4.4.0 App/IDE Parity, Quote-Aware Squeeze, Wide-Line Guard, Longevity HUD & Multi-Surface CLIs:")
    HUD_HOOK = _HOOKS_DIR / "longevity_hud.py"
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # 9a) Quote-aware PreToolUse wm-squeeze (quoted rg, find, git log --format ARE squeezed; $VAR/pipes/unquoted glob untouched)
        (td_path / "sample.py").write_text("foo = 1\nbar = 2\n", encoding="utf-8")
        for label, cmd in [
            ("quoted rg alternation", f'rg -n "foo|bar" {td}'),
            ("quoted find glob", f'find {td} -name "*.py"'),
            ("quoted git format", f'git -C {td} log --format="%h %s" -n 5'),
        ]:
            rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
            d = json.loads(out)
            ow = (d.get("overwrite") or {}).get("CommandLine", "")
            check(f"{label} -> rewritten with --safe", rc == 0 and d.get("decision") == "allow" and "--safe" in ow, ow)

        # Verify rewritten quoted rg & find actually execute cleanly
        _, out_rg, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": f'rg -n "foo|bar" {td}'}}})
        ow_rg = (json.loads(out_rg).get("overwrite") or {}).get("CommandLine", "")
        p_rg = subprocess.run(["bash", "-c", ow_rg], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1", "WM_SQUEEZE_NO_CACHE": "1"})
        check("rewritten quoted rg executes cleanly & finds matches", p_rg.returncode == 0 and "foo = 1" in p_rg.stdout and "bar = 2" in p_rg.stdout)

        for label, cmd in [
            ("double-quoted $VAR", f'rg -n "$VAR" {td}'),
            ("piped rg", f'rg -n foo {td} | head -n 5'),
            ("unquoted find glob", f'find {td} -name *.py'),
        ]:
            rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}})
            d = json.loads(out)
            check(f"untouched ({label})", rc == 0 and "overwrite" not in d)

        # 9b) Standalone python3 -c wrapping (large stdout capped, exit code preserved, piped untouched)
        py_big = 'python3 -c "for i in range(400): print(f\'row {i}: \' + \'x\' * 45)"'
        rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": py_big}}})
        rew_py = (json.loads(out).get("overwrite") or {}).get("CommandLine", "")
        check("python3 -c rewritten with pipefail + wm-squeeze --pipe", rc == 0 and "set -o pipefail;" in rew_py and "--pipe" in rew_py)
        p_py = subprocess.run(["bash", "-c", rew_py], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"})
        check("rewritten python3 -c caps stdout <=8.4KB & rc=0", p_py.returncode == 0 and len(p_py.stdout.encode("utf-8")) <= 8400 and "[wm-squeeze:" in p_py.stdout)

        py_fail = 'python3 -c "import sys; print(\'fail\'); sys.exit(4)"'
        _, out_pf, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": py_fail}}})
        rew_pf = (json.loads(out_pf).get("overwrite") or {}).get("CommandLine", "")
        p_pf = subprocess.run(["bash", "-c", rew_pf], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"})
        check("rewritten failing python3 -c preserves exit code 4", p_pf.returncode == 4)

        _, out_pipe, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": 'python3 -c "print(1)" | head -n 1'}}})
        check("piped python3 -c untouched", "overwrite" not in json.loads(out_pipe))

        # 9c) Wide-line JSONL file (>24KB, avg >400B/line) clamped to <=50L even on explicit 1..150 span
        wide_jsonl = td_path / "wide.jsonl"
        wide_jsonl.write_text("\n".join(json.dumps({"i": i, "blob": "A" * 600}) for i in range(80)) + "\n", encoding="utf-8")
        tr_lo = td_path / "tr_lo.jsonl"
        tr_lo.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 8000, "cache_read_tokens": 32000, "model": "gemini-4-hydrogen-xhigh"}) + "\n")
        rc, out, _, _ = run_hook(
            PRE_HOOK,
            {"conversationId": "v44-wide-jsonl", "stepIdx": 1, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(wide_jsonl), "StartLine": 1, "EndLine": 150}}},
        )
        check("wide-line JSONL StartLine=1..150 clamped to EndLine=50", rc == 0 and json.loads(out).get("overwrite") == {"EndLine": 50}, out)

        # 9d) Research Engine & Firecrawl MCP budgeters + hooks.json matcher coverage
        _, out_rh, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "mcp_wm-research-engine_research_hunt", "args": {"problem": "cache", "limit": 20}}})
        check("research_hunt limit=20 clamped to 8", json.loads(out_rh).get("overwrite") == {"limit": 8})
        _, out_fc, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "mcp_firecrawl_firecrawl_scrape", "args": {"url": "https://example.com"}}})
        check("firecrawl_scrape defaults markdown_only=True", json.loads(out_fc).get("overwrite") == {"markdown_only": True})
        _, out_fc_fmt, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "mcp_firecrawl_firecrawl_scrape", "args": {"url": "https://example.com", "format": "html"}}})
        check("firecrawl_scrape with explicit format untouched", "overwrite" not in json.loads(out_fc_fmt))
        hooks_cfg = json.loads((KIT / "plugin" / "hooks.json").read_text(encoding="utf-8"))
        pre_matcher = hooks_cfg["data-loss-and-token-guard"]["PreToolUse"][0]["matcher"]
        check(
            "hooks.json PreToolUse covers research_hunt & firecrawl_scrape + PostInvocation HUD",
            "mcp_wm-research-engine_research_hunt" in pre_matcher and "mcp_firecrawl_firecrawl_scrape" in pre_matcher and "agy-longevity-hud" in hooks_cfg,
        )

        # 9e) Zero-token PostInvocation Longevity HUD (longevity_hud.py)
        app_brain = td_path / ".gemini" / "antigravity" / "brain" / "v44-hud-app" / ".system_generated" / "logs"
        app_brain.mkdir(parents=True)
        tr_app = app_brain / "transcript.jsonl"
        tr_app.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 80000, "model": "gemini-4-hydrogen-xhigh", "step_index": 1}) + "\n")
        rc, out, _, ms = run_hook(HUD_HOOK, {"conversationId": "v44-hud-app-1", "transcriptPath": str(tr_app), "invocationNum": 1})
        check("HUD <120k -> silent {}", rc == 0 and json.loads(out) == {}, f"{ms:.1f}ms")

        tr_app.write_text(tr_app.read_text() + json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 115000, "model": "gemini-4-hydrogen-xhigh", "step_index": 2}) + "\n")
        rc, out, _, _ = run_hook(HUD_HOOK, {"conversationId": "v44-hud-app-1", "transcriptPath": str(tr_app), "invocationNum": 2})
        d = json.loads(out)
        sm = d.get("injectSteps", [{}])[0].get("systemMessage", {})
        check("HUD App 125k -> zero-token user_markdown Context Watch", rc == 0 and sm.get("systemMessage") == "" and "Context Watch" in sm.get("metadata", {}).get("user_markdown", ""))

        # Repeat at 128k (without compaction) -> silent {}
        tr_app.write_text(tr_app.read_text() + json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 3000, "cache_read_tokens": 125000, "model": "gemini-4-hydrogen-xhigh", "step_index": 3}) + "\n")
        rc, out, _, _ = run_hook(HUD_HOOK, {"conversationId": "v44-hud-app-1", "transcriptPath": str(tr_app), "invocationNum": 3})
        check("HUD 128k repeat -> silent {} (rate-limited)", rc == 0 and json.loads(out) == {})

        # Cross 205k -> Context Cliff alert
        tr_app.write_text(tr_app.read_text() + json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 8000, "cache_read_tokens": 197000, "model": "gemini-4-hydrogen-xhigh", "step_index": 4}) + "\n")
        rc, out, _, _ = run_hook(HUD_HOOK, {"conversationId": "v44-hud-app-1", "transcriptPath": str(tr_app), "invocationNum": 4})
        d = json.loads(out)
        um = d.get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", "")
        check("HUD 205k -> Context Cliff alert", rc == 0 and "Context Cliff" in um)

        # Post-compaction CHECKPOINT resets threshold flags
        tr_app.write_text(tr_app.read_text() + json.dumps({"type": "CHECKPOINT", "content": "compacted"}) + "\n" + json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 5000, "cache_read_tokens": 120000, "model": "gemini-4-hydrogen-xhigh", "step_index": 5}) + "\n")
        rc, out, _, _ = run_hook(HUD_HOOK, {"conversationId": "v44-hud-app-1", "transcriptPath": str(tr_app), "invocationNum": 5})
        um = json.loads(out).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", "")
        check("HUD post-CHECKPOINT epoch reset re-arms 120k watch", rc == 0 and "Context Watch" in um)

        # CLI Model flip >=50k uncached fires alert; VANILLA=1 stays silent
        cli_brain = td_path / ".gemini" / "antigravity-cli" / "brain" / "v44-hud-cli" / ".system_generated" / "logs"
        cli_brain.mkdir(parents=True)
        tr_cli = cli_brain / "transcript.jsonl"
        tr_cli.write_text(
            "\n".join([
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 90000, "model": "gemini-4-neon-xhigh", "step_index": 1}),
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 110000, "cache_read_tokens": 0, "model": "gemini-4-hydrogen-xhigh", "step_index": 2}),
            ]) + "\n"
        )
        rc, out, _, _ = run_hook(HUD_HOOK, {"conversationId": "v44-hud-cli-1", "transcriptPath": str(tr_cli), "invocationNum": 2})
        um_flip = json.loads(out).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", "")
        check("HUD CLI model flip unc=110k -> Model Flip alert", rc == 0 and "Model Flip Cache Eviction" in um_flip)

        rc, out_v, _, _ = run_hook(HUD_HOOK, {"conversationId": "v44-hud-cli-2", "transcriptPath": str(tr_cli), "invocationNum": 2}, env_extra={"WM_AGY_BENCH_VANILLA": "1"})
        check("HUD WM_AGY_BENCH_VANILLA=1 -> {}", rc == 0 and json.loads(out_v) == {})

        # Cold miss (<50% hit at >=80k without model flip)
        tr_cold = app_brain / "cold.jsonl"
        tr_cold.write_text(
            "\n".join([
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 120000, "model": "gemini-4-hydrogen-xhigh", "step_index": 1}),
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 140000, "cache_read_tokens": 0, "model": "gemini-4-hydrogen-xhigh", "step_index": 2}),
            ]) + "\n"
        )
        rc, out_c, _, _ = run_hook(HUD_HOOK, {"conversationId": "v44-hud-cold-1", "transcriptPath": str(tr_cold), "invocationNum": 6})
        um_cold = json.loads(out_c).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", "")
        check("HUD cold miss 140k (0% hit) -> Cold Cache Miss alert", rc == 0 and "Cold Cache Miss" in um_cold)

    # 9f) Multi-surface CLIs (wm-handoff, agy-token-audit, agy-mode, agy-bench, wm-sim)
    p_ho = subprocess.run([str(LOCAL_BIN / "wm-handoff"), "--surface", "app", "--cwd", str(KIT)], capture_output=True, text=True)
    check("wm-handoff --surface app emits crystal", p_ho.returncode == 0 and "surface=app" in p_ho.stdout and "crystal_sha256=" in p_ho.stdout)
    p_au = subprocess.run([str(LOCAL_BIN / "agy-token-audit"), "--surface", "app", "--recent", "3", "--json"], capture_output=True, text=True)
    ok_au = p_au.returncode == 0 and any(s.get("surface") == "app" for s in json.loads(p_au.stdout).get("sessions", []))
    check("agy-token-audit --surface app --recent 3 --json succeeds", ok_au)
    p_md = subprocess.run([str(LOCAL_BIN / "agy-mode"), "status", "--surface", "all"], capture_output=True, text=True)
    check("agy-mode status --surface all succeeds", p_md.returncode == 0 and "AGY Model Cache" in p_md.stdout)
    p_ab = subprocess.run([str(LOCAL_BIN / "agy-bench"), "--surface", "app", "--recent", "3", "--credits"], capture_output=True, text=True)
    check("agy-bench --surface app --credits succeeds", p_ab.returncode == 0 and "Overall cache hit rate" in p_ab.stdout)
    p_sim = subprocess.run([str(LOCAL_BIN / "wm-sim"), "audit"], capture_output=True, text=True)
    check("wm-sim audit confirms fast MCP VALID", p_sim.returncode == 0 and "whitemagic-agy-kit MCP: VALID" in p_sim.stdout)


def main():
    with tempfile.TemporaryDirectory(prefix="agy_hook_state_") as state_td:
        os.environ["WM_HOOK_STATE_DIR"] = state_td
        test_wm_squeeze()
        test_pre_tool_hook()
        test_statusline_and_lifecycle()
        test_tricameral_v31()
        test_v4_fusion()
        test_bwrap_confinement()
        test_signed_confinement_receipts()
        test_remote_confinement_attestation()
        test_v41_token_burn_shield()
        test_v44_ide_longevity_shield()
    print(f"\n=== Wave 3/4/v4.4 Test Summary: {PASS} passed, {FAIL} failed ===")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
