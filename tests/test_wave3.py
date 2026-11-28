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
    check(
        "wm-squeeze --safe allows python3 -m pytest",
        p_ok.returncode == 0
        or (p_ok.returncode == 1 and "No module named pytest" in (p_ok.stdout + p_ok.stderr) and "[wm-squeeze]" not in p_ok.stderr),
    )
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
                    and ("read-only" in err_tail or "permission denied" in err_tail or "operation not permitted" in err_tail),
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
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:
        serialization = None
        ed25519 = None

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
            pem_priv_path = td_path / "ed25519_priv.pem"
            pem_pub_path = td_path / "ed25519_pub.pem"
            raw_priv_path = td_path / "ed25519_priv.bin"
            if ed25519 is not None and serialization is not None:
                priv = ed25519.Ed25519PrivateKey.generate()
                pub = priv.public_key()
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
            else:
                raw_seed = os.urandom(32)
                raw_priv_path.write_bytes(raw_seed)
                pem_priv_bytes = swarm_mod._der_to_pem(swarm_mod._ED25519_PKCS8_PREFIX + raw_seed, "PRIVATE KEY")
                pem_priv_path.write_bytes(pem_priv_bytes)
                openssl_bin = swarm_mod._find_openssl_ed25519()
                priv = swarm_mod._OpensslEd25519PrivateKey(pem_priv_bytes, openssl_bin)
                pub = priv.public_key()
                pem_pub_path.write_bytes(pub.to_pem().encode("utf-8"))

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
    app_brain = Path.home() / ".gemini" / "antigravity" / "brain"
    has_real_app = bool(list(app_brain.glob("*/.system_generated/logs/transcript.jsonl"))) if app_brain.is_dir() else False
    with tempfile.TemporaryDirectory(prefix="wm-app-surf-") as app_td:
        surf_env = dict(os.environ)
        if not has_real_app:
            synth_logs = Path(app_td) / ".gemini" / "antigravity" / "brain" / "conv-app-synth-1" / ".system_generated" / "logs"
            synth_logs.mkdir(parents=True, exist_ok=True)
            (synth_logs / "transcript.jsonl").write_text(
                json.dumps({"type": "USER_INPUT", "content": "check app surface", "created_at": "2026-11-25T12:00:00Z"}) + "\n"
                + json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-hydrogen-high", "input_tokens": 4000, "cache_read_tokens": 36000, "output_tokens": 300, "content": "App surface verified.", "created_at": "2026-11-25T12:00:05Z"}) + "\n",
                encoding="utf-8",
            )
            surf_env["HOME"] = app_td
        p_ho = subprocess.run([str(LOCAL_BIN / "wm-handoff"), "--surface", "app", "--cwd", str(KIT)], capture_output=True, text=True, env=surf_env)
        check("wm-handoff --surface app emits crystal", p_ho.returncode == 0 and "surface=app" in p_ho.stdout and "crystal_sha256=" in p_ho.stdout)
        p_au = subprocess.run([str(LOCAL_BIN / "agy-token-audit"), "--surface", "app", "--recent", "3", "--json"], capture_output=True, text=True, env=surf_env)
        ok_au = p_au.returncode == 0 and any(s.get("surface") == "app" for s in json.loads(p_au.stdout).get("sessions", []))
        check("agy-token-audit --surface app --recent 3 --json succeeds", ok_au)
        p_ab = subprocess.run([str(LOCAL_BIN / "agy-bench"), "--surface", "app", "--recent", "3", "--credits"], capture_output=True, text=True, env=surf_env)
        check("agy-bench --surface app --credits succeeds", p_ab.returncode == 0 and "Overall cache hit rate" in p_ab.stdout)
    p_md = subprocess.run([str(LOCAL_BIN / "agy-mode"), "status", "--surface", "all"], capture_output=True, text=True)
    check("agy-mode status --surface all succeeds", p_md.returncode == 0 and "AGY Model Cache" in p_md.stdout)
    p_sim = subprocess.run([str(LOCAL_BIN / "wm-sim"), "audit"], capture_output=True, text=True)
    check("wm-sim audit confirms fast MCP VALID", p_sim.returncode == 0 and "whitemagic-agy-kit MCP: VALID" in p_sim.stdout)


def test_v441_spill_cap_and_rust_hud():
    print("\n10. v4.4.1 Compound Spill Shield, Sub-4ms Rust HUD & CLI Hygiene:")
    SQ_BIN = LOCAL_BIN / "wm-squeeze"
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "f1.py").write_text("foo = 1\nbar = 2\n", encoding="utf-8")

        # 10a) Multiline read-only inspection wrapped & executes cleanly
        cmd_ml = f'ls -la {td}\necho "==="\nrg -n foo {td}'
        rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd_ml}}})
        rew_ml = (json.loads(out).get("overwrite") or {}).get("CommandLine", "")
        check("multiline read-only rewritten with set +o pipefail + wm-squeeze --pipe", rc == 0 and "(set +o pipefail" in rew_ml and "--pipe" in rew_ml)
        p_ml = subprocess.run(["bash", "-c", rew_ml], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"})
        check("rewritten multiline executes rc=0 & keeps output", p_ml.returncode == 0 and "===" in p_ml.stdout and "foo = 1" in p_ml.stdout)

        # 10b) Multiline with inner `| head -n 2` exits 0 (zero false SIGPIPE 141)
        cmd_sig = 'python3 -c "for i in range(4000): print(i)" | head -n 2\necho SIGPIPE_OK'
        rc, out, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd_sig}}})
        rew_sig = (json.loads(out).get("overwrite") or {}).get("CommandLine", "")
        p_sig = subprocess.run(["bash", "-c", rew_sig], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"})
        check("multiline with inner | head exits rc=0 (no SIGPIPE 141)", p_sig.returncode == 0 and "SIGPIPE_OK" in p_sig.stdout)

        # 10c) Failing command inside wrapped compound block preserves exact non-zero exit code
        cmd_fail = f'ls {td} && python3 -c "import sys; print(\'boom\'); sys.exit(7)"'
        _, out_cf, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd_fail}}})
        rew_cf = (json.loads(out_cf).get("overwrite") or {}).get("CommandLine", "")
        p_cf = subprocess.run(["bash", "-c", rew_cf], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"})
        check("wrapped failing compound cmd preserves exit code 7", p_cf.returncode == 7)

        # 10d) Complex heredoc (`cd <dir> && python3 - <<'PY'`) wrapped & capped <=8.4KB
        cmd_hd = f"cd {td} && python3 - <<'PY'\nfor i in range(400):\n    print(f'row {{i}}: ' + 'x'*55)\nPY"
        _, out_hd, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd_hd}}})
        rew_hd = (json.loads(out_hd).get("overwrite") or {}).get("CommandLine", "")
        p_hd = subprocess.run(["bash", "-c", rew_hd], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"})
        check("cd && python3 heredoc wrapped & capped <=8.4KB", "--pipe" in rew_hd and p_hd.returncode == 0 and len(p_hd.stdout.encode("utf-8")) <= 8400)

        # 10e) Uncapped pipeline wrapped (`rg ... | grep ...`)
        cmd_pipe = f'rg -n "foo|bar" {td} | grep foo'
        _, out_p, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd_pipe}}})
        check("uncapped pipeline rg | grep wrapped", "--pipe" in (json.loads(out_p).get("overwrite") or {}).get("CommandLine", ""))

        # 10f) Safety & bounded boundaries UNTOUCHED
        for label, safe_cmd in [
            ("bounded git diff | head", "git diff | head -n 20"),
            ("unquoted glob", "ls *.py"),
            ("output redirect", "echo hi > /tmp/x"),
            ("arbitrary python script", "python3 script.py"),
            ("mutating rm &&", "rm -rf /tmp/v441_nonexistent && echo done"),
            ("compound redirect", "cat a > /tmp/b && echo done"),
            ("background &", "sleep 1 &"),
            ("sed -i &&", "sed -i 's/a/b/' /tmp/f && echo ok"),
        ]:
            _, out_u, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": safe_cmd}}})
            check(f"v4.4.1 untouched ({label})", "overwrite" not in json.loads(out_u))

        # 10g) Rust `wm-squeeze --hud` full parity + idle-gap attribution + sub-25ms speed
        def run_rust_hud(payload, env_extra=None):
            env = os.environ.copy()
            if env_extra:
                env.update(env_extra)
            t0 = time.perf_counter()
            p = subprocess.run([str(SQ_BIN), "--hud"], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=5)
            return p.returncode, p.stdout.strip(), (time.perf_counter() - t0) * 1000.0

        app_dir = td_path / ".gemini" / "antigravity" / "brain" / "r-hud" / ".system_generated" / "logs"
        app_dir.mkdir(parents=True)
        tr_r = app_dir / "transcript.jsonl"
        tr_r.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 80000, "model": "gemini-4-hydrogen-xhigh", "step_index": 1}) + "\n")
        rc, out_r, _ = run_rust_hud({"conversationId": "rust-hud-1", "transcriptPath": str(tr_r), "invocationNum": 1})
        check("Rust HUD <120k -> {}", rc == 0 and json.loads(out_r) == {})

        tr_r.write_text(tr_r.read_text() + json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 115000, "model": "gemini-4-hydrogen-xhigh", "step_index": 2}) + "\n")
        rc, out_r, _ = run_rust_hud({"conversationId": "rust-hud-1", "transcriptPath": str(tr_r), "invocationNum": 2})
        sm_r = json.loads(out_r).get("injectSteps", [{}])[0].get("systemMessage", {})
        check("Rust HUD App 125k -> zero-token Context Watch", rc == 0 and sm_r.get("systemMessage") == "" and "Context Watch" in sm_r.get("metadata", {}).get("user_markdown", ""))

        tr_r.write_text(tr_r.read_text() + json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 3000, "cache_read_tokens": 125000, "model": "gemini-4-hydrogen-xhigh", "step_index": 3}) + "\n")
        rc, out_r, _ = run_rust_hud({"conversationId": "rust-hud-1", "transcriptPath": str(tr_r), "invocationNum": 3})
        check("Rust HUD 128k repeat -> {}", rc == 0 and json.loads(out_r) == {})

        tr_r.write_text(tr_r.read_text() + json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 8000, "cache_read_tokens": 197000, "model": "gemini-4-hydrogen-xhigh", "step_index": 4}) + "\n")
        rc, out_r, _ = run_rust_hud({"conversationId": "rust-hud-1", "transcriptPath": str(tr_r), "invocationNum": 4})
        check("Rust HUD 205k -> Context Cliff", rc == 0 and "Context Cliff" in json.loads(out_r).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", ""))

        tr_r.write_text(tr_r.read_text() + json.dumps({"type": "CHECKPOINT", "content": "ok"}) + "\n" + json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 5000, "cache_read_tokens": 120000, "model": "gemini-4-hydrogen-xhigh", "step_index": 5}) + "\n")
        rc, out_r, _ = run_rust_hud({"conversationId": "rust-hud-1", "transcriptPath": str(tr_r), "invocationNum": 5})
        check("Rust HUD post-CHECKPOINT re-arms 120k watch", rc == 0 and "Context Watch" in json.loads(out_r).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", ""))

        cli_dir = td_path / ".gemini" / "antigravity-cli" / "brain" / "r-cli" / ".system_generated" / "logs"
        cli_dir.mkdir(parents=True)
        tr_rc = cli_dir / "transcript.jsonl"
        tr_rc.write_text(
            "\n".join([
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 90000, "model": "gemini-4-neon-xhigh", "step_index": 1}),
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 110000, "cache_read_tokens": 0, "model": "gemini-4-hydrogen-xhigh", "step_index": 2}),
            ]) + "\n"
        )
        rc, out_rf, _ = run_rust_hud({"conversationId": "rust-hud-cli", "transcriptPath": str(tr_rc), "invocationNum": 2})
        check("Rust HUD CLI model flip -> Model Flip alert", rc == 0 and "Model Flip Cache Eviction" in json.loads(out_rf).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", ""))
        rc, out_rv, _ = run_rust_hud({"conversationId": "rust-hud-cli2", "transcriptPath": str(tr_rc), "invocationNum": 2}, env_extra={"WM_AGY_BENCH_VANILLA": "1"})
        check("Rust HUD VANILLA=1 -> {}", rc == 0 and json.loads(out_rv) == {})

        # Idle-gap attribution (12m gap)
        tr_idle = app_dir / "idle.jsonl"
        tr_idle.write_text(
            "\n".join([
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 10000, "cache_read_tokens": 120000, "model": "gemini-4-hydrogen-xhigh", "created_at": "2026-11-25T15:00:00Z", "step_index": 1}),
                json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 140000, "cache_read_tokens": 0, "model": "gemini-4-hydrogen-xhigh", "created_at": "2026-11-25T15:12:00Z", "step_index": 2}),
            ]) + "\n"
        )
        rc, out_ri, _ = run_rust_hud({"conversationId": "rust-hud-idle", "transcriptPath": str(tr_idle), "invocationNum": 6})
        um_ri = json.loads(out_ri).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", "")
        check("Rust HUD 12m idle resume -> Cold Resume After Idle (idle 12m)", rc == 0 and "Cold Resume After Idle" in um_ri and "idle 12m" in um_ri, um_ri)

        # Rust HUD latency (<25ms median)
        hud_lats = sorted(run_rust_hud({"conversationId": f"rust-lat-{i}", "transcriptPath": str(tr_r), "invocationNum": 5})[2] for i in range(10))
        med_ms = hud_lats[len(hud_lats) // 2]
        check(f"Rust wm-squeeze --hud median latency < 25ms ({med_ms:.1f}ms)", med_ms < 25.0)

    # 10h) wm-sim audit skips disabled agy-fast rule file
    p_sim_audit = subprocess.run([str(LOCAL_BIN / "wm-sim"), "audit"], capture_output=True, text=True)
    check("wm-sim audit skips disabled agy-fast rule file", p_sim_audit.returncode == 0 and "plugins/agy-fast/rules/AGENTS.md" not in p_sim_audit.stdout)


def test_v442_self_heal_and_rust_post_tool():
    print("\n11. v4.4.2 Self-Healing read_file, Path Hints, Rust --post-tool, Dream & Brief Hygiene:")
    SQ_BIN = LOCAL_BIN / "wm-squeeze"
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        # 11a) read_file self-healing inverted/invalid line ranges
        big500 = td_path / "big500.py"
        big500.write_text("\n".join(f"row_{i} = 'payload text {i}'" for i in range(500)) + "\n", encoding="utf-8")
        wide_jl = td_path / "wide500.jsonl"
        wide_jl.write_text("\n".join(json.dumps({"i": i, "d": "X" * 520}) for i in range(100)) + "\n", encoding="utf-8")
        tr_lo = td_path / "tr_lo.jsonl"
        tr_lo.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 5000, "cache_read_tokens": 35000}) + "\n")

        rc, out, _, _ = run_hook(PRE_HOOK, {"conversationId": "v442-heal-1", "stepIdx": 1, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big500), "StartLine": 280, "EndLine": 120}}})
        check("read_file StartLine=280,EndLine=120 -> healed EndLine=399", rc == 0 and json.loads(out).get("overwrite") == {"EndLine": 399}, out)

        rc, out, _, _ = run_hook(PRE_HOOK, {"conversationId": "v442-heal-2", "stepIdx": 1, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big500), "StartLine": 101, "EndLine": 100}}})
        check("read_file StartLine=101,EndLine=100 -> healed EndLine=200", rc == 0 and json.loads(out).get("overwrite") == {"EndLine": 200}, out)

        rc, out, _, _ = run_hook(PRE_HOOK, {"conversationId": "v442-heal-3", "stepIdx": 1, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big500), "StartLine": 450, "EndLine": 350}}})
        check("read_file swapped 450..350 -> StartLine=350,EndLine=450", rc == 0 and json.loads(out).get("overwrite") == {"StartLine": 350, "EndLine": 450}, out)

        rc, out, _, _ = run_hook(PRE_HOOK, {"conversationId": "v442-heal-4", "stepIdx": 1, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(big500), "StartLine": 0, "EndLine": 50}}})
        check("read_file StartLine=0,EndLine=50 -> clamped StartLine=1", rc == 0 and json.loads(out).get("overwrite") == {"StartLine": 1}, out)

        rc, out, _, _ = run_hook(PRE_HOOK, {"conversationId": "v442-heal-5", "stepIdx": 1, "transcriptPath": str(tr_lo), "toolCall": {"name": "read_file", "args": {"Path": str(wide_jl), "StartLine": 280, "EndLine": 120}}})
        check("read_file wide JSONL 280..120 -> capped EndLine=329 (50L)", rc == 0 and json.loads(out).get("overwrite") == {"EndLine": 329}, out)

        # 11b) Missing-path & directory hints
        proj = td_path / "proj"
        sub = proj / "hooks"
        sub.mkdir(parents=True)
        (proj / "hooks.json").write_text("{}\n")
        (sub / "pre_tool_guard.py").write_text("# ok\n")
        (sub / "post_tool_sim.py").write_text("# ok\n")

        _, out_miss, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "read_file", "args": {"Path": str(sub / "pre_tool_gard.py")}}})
        dm = json.loads(out_miss)
        check("read_file typo -> deny [Path Hint] closest sibling", dm.get("decision") == "deny" and "[Path Hint]" in dm.get("reason", "") and "pre_tool_guard.py" in dm.get("reason", ""), out_miss)

        _, out_gp, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "read_file", "args": {"Path": str(sub / "hooks.json")}}})
        dg = json.loads(out_gp)
        check("read_file wrong subdir -> parent-dir hint finds hooks.json", dg.get("decision") == "deny" and "hooks.json" in dg.get("reason", "") and "parent dir" in dg.get("reason", ""), out_gp)

        _, out_dir, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "read_file", "args": {"Path": str(sub)}}})
        dd = json.loads(out_dir)
        check("read_file directory -> deny [Path Hint] entries", dd.get("decision") == "deny" and "is a directory" in dd.get("reason", "") and "pre_tool_guard.py" in dd.get("reason", ""), out_dir)

        _, out_off, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "read_file", "args": {"Path": str(sub / "missing.py")}}}, env_extra={"WM_READ_HINT_OFF": "1"})
        check("WM_READ_HINT_OFF=1 disables path hint -> allow", json.loads(out_off).get("decision") == "allow")

        # 11c) Spill-shield 3 edge cases (cd with spaces heredoc, backslash-nl continuation, safe xargs)
        sp_dir = td_path / "dir with spaces"
        sp_dir.mkdir()
        (sp_dir / "alpha.py").write_text("target_needle = 1\n")

        cmd_hd_sp = f'cd "{sp_dir}" && python3 - <<\'PY\'\nprint("SPACES_HEREDOC_OK")\nPY'
        _, out_hs, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd_hd_sp}}})
        rew_hs = (json.loads(out_hs).get("overwrite") or {}).get("CommandLine", "")
        p_hs = subprocess.run(["bash", "-c", rew_hs], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"}) if rew_hs else None
        check("cd with spaces + python3 heredoc wrapped & executes rc=0", "--pipe" in rew_hs and p_hs is not None and p_hs.returncode == 0 and "SPACES_HEREDOC_OK" in p_hs.stdout, rew_hs)

        cmd_cont = f'cd "{sp_dir}" && \\\nls -la'
        _, out_ct, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd_cont}}})
        rew_ct = (json.loads(out_ct).get("overwrite") or {}).get("CommandLine", "")
        p_ct = subprocess.run(["bash", "-c", rew_ct], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1", "WM_SQUEEZE_NO_CACHE": "1"}) if rew_ct else None
        check("cd with spaces + backslash-newline continuation squeezed & rc=0", "--safe" in rew_ct and p_ct is not None and p_ct.returncode == 0 and "alpha.py" in p_ct.stdout, rew_ct)

        (td_path / "clean.py").write_text("target_needle = 1\n")
        cmd_xg = f'rg -l target_needle "{td_path / "clean.py"}" | xargs -r ls -lh'
        _, out_xg, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd_xg}}})
        rew_xg = (json.loads(out_xg).get("overwrite") or {}).get("CommandLine", "")
        p_xg = subprocess.run(["bash", "-c", rew_xg], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"}) if rew_xg else None
        check("rg -l | xargs -r ls wrapped & executes rc=0", "--pipe" in rew_xg and p_xg is not None and p_xg.returncode == 0 and "clean.py" in p_xg.stdout, rew_xg)

        cmd_xg0 = f'rg -0 -l target_needle "{sp_dir}" | xargs -0 -r ls -lh'
        _, out_xg0, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd_xg0}}})
        rew_xg0 = (json.loads(out_xg0).get("overwrite") or {}).get("CommandLine", "")
        p_xg0 = subprocess.run(["bash", "-c", rew_xg0], capture_output=True, text=True, env={**os.environ, "WM_SQUEEZE_NO_STATS": "1"}) if rew_xg0 else None
        check("rg -0 -l | xargs -0 -r ls handles spaces rc=0", "--pipe" in rew_xg0 and p_xg0 is not None and p_xg0.returncode == 0 and "alpha.py" in p_xg0.stdout, rew_xg0)

        for bad_xg in (f'rg -l foo "{sp_dir}" | xargs -r rm -f', f'rg -l foo "{sp_dir}" | xargs sed -i "s/a/b/"'):
            _, out_bx, _, _ = run_hook(PRE_HOOK, {"toolCall": {"name": "run_command", "args": {"CommandLine": bad_xg}}})
            check(f"unsafe xargs untouched ({bad_xg.split('|')[1].strip()})", "overwrite" not in json.loads(out_bx))

        # 11d) Rust wm-squeeze --post-tool parity + speed vs Python post_tool_sim.py
        sim_py = td_path / "sim_py.jsonl"
        sim_rs = td_path / "sim_rs.jsonl"
        tel_py = td_path / "tel_py.jsonl"
        tel_rs = td_path / "tel_rs.jsonl"

        test_err_cases = [
            {"conversationId": "c1234567890abcdef", "cwd": str(td_path), "modelName": "hydrogen", "toolCall": {"name": "read_file", "args": {"Path": "/tmp/a.py", "StartLine": 280, "EndLine": 120}}, "error": "invalid line range Bearer sk_live_999999"},
            {"conversationId": "c1", "toolCall": {"name": "write_file", "args": {"Path": "/tmp/w.py"}}, "error": "api_key=supersecret1234"},
            {"conversationId": "c1", "toolCall": {"name": "edit_file", "args": {"Path": "/tmp/e.py"}}, "error": "target not found"},
            {"conversationId": "c1", "toolCall": {"name": "mcp_whitemagic-agy-kit_fast_apply_edits", "args": {"edits": [{"path": "x.rs"}, {"path": "y.rs"}]}}, "error": "aborted"},
        ]
        for pl in test_err_cases:
            run_hook(POST_HOOK, pl, env_extra={"WM_SIM_LEDGER": str(sim_py), "WM_AGY_TELEMETRY": str(tel_py)})
            subprocess.run([str(SQ_BIN), "--post-tool"], input=json.dumps(pl), capture_output=True, text=True, env={**os.environ, "WM_SIM_LEDGER": str(sim_rs), "WM_AGY_TELEMETRY": str(tel_rs)}, timeout=5)

        py_rows = [json.loads(l) for l in sim_py.read_text().splitlines() if l.strip()]
        rs_rows = [json.loads(l) for l in sim_rs.read_text().splitlines() if l.strip()]
        parity_errs = len(py_rows) == 4 and len(rs_rows) == 4 and all(
            p.get("tool") == r.get("tool") and p.get("cmd_preview") == r.get("cmd_preview") and p.get("error") == r.get("error")
            for p, r in zip(py_rows, rs_rows)
        )
        check("Rust --post-tool failure parity + rich read/write/edit preview + redaction", parity_errs and "read_file: /tmp/a.py [L280-120]" in rs_rows[0]["cmd_preview"] and "[REDACTED]" in rs_rows[0]["error"] and "[REDACTED]" in rs_rows[1]["error"])

        for spl in (
            {"conversationId": "sp1", "toolCall": {"name": "run_command", "args": {"CommandLine": "cat big"}}, "result": "Z" * 18000},
            {"conversationId": "sp2", "toolCall": {"name": "write_file", "args": {"Path": "/tmp/huge.py", "Content": "Y" * 26000}}},
        ):
            run_hook(POST_HOOK, spl, env_extra={"WM_SIM_LEDGER": str(sim_py), "WM_AGY_TELEMETRY": str(tel_py)})
            subprocess.run([str(SQ_BIN), "--post-tool"], input=json.dumps(spl), capture_output=True, text=True, env={**os.environ, "WM_SIM_LEDGER": str(sim_rs), "WM_AGY_TELEMETRY": str(tel_rs)}, timeout=5)
        py_sp = [json.loads(l) for l in tel_py.read_text().splitlines() if l.strip()]
        rs_sp = [json.loads(l) for l in tel_rs.read_text().splitlines() if l.strip()]
        check("Rust --post-tool spill parity (output_spill + large_write_file)", len(py_sp) == 2 and len(rs_sp) == 2 and all(p.get("kind") == r.get("kind") and p.get("bytes") == r.get("bytes") for p, r in zip(py_sp, rs_sp)))

        clean_pl = json.dumps({"toolCall": {"name": "read_file", "args": {"Path": "/tmp/a.py"}}, "result": "ok"})
        pt_lats = []
        for _ in range(15):
            t0 = time.perf_counter()
            subprocess.run([str(SQ_BIN), "--post-tool"], input=clean_pl, capture_output=True, text=True, timeout=5)
            pt_lats.append((time.perf_counter() - t0) * 1000.0)
        pt_lats.sort()
        pt_med = pt_lats[len(pt_lats) // 2]
        check(f"Rust wm-squeeze --post-tool clean latency < 15ms ({pt_med:.1f}ms)", pt_med < 15.0)

        # 11e) wm-dream windowing & retired legacy exclusion + session_start_brief word-boundary safety
        from datetime import datetime, timedelta, timezone
        now_utc = datetime.now(timezone.utc)
        d_sim = td_path / "dream_sim.jsonl"
        d_sim.write_text(
            "\n".join([
                json.dumps({"event": "tool_failure", "ts": (now_utc - timedelta(days=12)).isoformat(), "tool": "read_file", "error": "stale 12d old"}),
                json.dumps({"event": "tool_failure", "ts": (now_utc - timedelta(days=1)).isoformat(), "tool": "mcp_agy-fast_fast_apply_edits", "error": "Atomic edit aborted:"}),
                json.dumps({"event": "tool_failure", "ts": (now_utc - timedelta(days=1)).isoformat(), "tool": "run_command", "error": "exit status 2:"}),
            ]) + "\n",
            encoding="utf-8",
        )
        d_tel = td_path / "dream_tel.jsonl"
        d_tel.write_text(
            "\n".join([
                json.dumps({"event": "stop", "ts_epoch": time.time(), "session_uncached": 10000, "session_cached": 90000, "last_hit_pct": 90.0}),
                json.dumps({"event": "stop", "ts_epoch": time.time(), "session_uncached": 20000, "session_cached": 80000, "last_hit_pct": 80.0}),
                json.dumps({"event": "tool_spill", "ts": now_utc.isoformat(), "kind": "large_write_file", "bytes": 30000}),
            ]) + "\n",
            encoding="utf-8",
        )
        d_sq = td_path / "dream_sq.jsonl"
        d_sq.write_text(json.dumps({"ts": time.time(), "raw": 10000, "out": 2000, "saved": 8000}) + "\n", encoding="utf-8")
        d_out = td_path / "dream_digest.md"
        p_dr = subprocess.run(
            [str(LOCAL_BIN / "wm-dream"), "--window-days", "7"],
            capture_output=True,
            text=True,
            env={**os.environ, "WM_SIM_LEDGER": str(d_sim), "WM_AGY_TELEMETRY": str(d_tel), "WM_SQUEEZE_STATS": str(d_sq), "WM_DREAM_DIGEST": str(d_out)},
        )
        d_txt = d_out.read_text(encoding="utf-8") if d_out.is_file() else ""
        check(
            "wm-dream excludes 12d/legacy failures, counts stops!=spills, <=460B",
            p_dr.returncode == 0 and "AGY: 2 stops" in d_txt and "Spills (1): 1x large_write_file" in d_txt and "run_command: exit status 2" in d_txt and "stale 12d old" not in d_txt and "mcp_agy-fast_" not in d_txt and len(d_txt.strip()) <= 460,
            d_txt,
        )

        # session_start_brief word-boundary truncation
        pf_f = td_path / "PREFLIGHT.md"
        pf_f.write_text("# Header 1\n> Summary line\n> Release: whitemagic-agy-kit v4.4.2 " + ("word " * 30) + "\n", encoding="utf-8")
        ss_sim = td_path / "ss_sim.jsonl"
        ss_sim.write_text(json.dumps({"event": "experiment", "domain": "agy-kit", "problem_group": "v442", "status": "kept", "target": "suite", "observed_outcome": "alpha beta gamma delta epsilon " * 8 + "validate --strict"}) + "\n", encoding="utf-8")
        _, out_ss, _, _ = run_hook(
            START_HOOK,
            {"conversationId": "ss-word"},
            env_extra={"WM_PREFLIGHT_PATH": str(pf_f), "WM_SIM_LEDGER": str(ss_sim), "WM_DREAM_DIGEST": str(d_out)},
        )
        ss_msg = json.loads(out_ss).get("injectSteps", [{}])[0].get("systemMessage", {}).get("systemMessage", "")
        check(
            "session_start_brief <=1450B & no mid-word Dream Digest truncation",
            len(ss_msg.encode("utf-8")) <= 1450 and "run_command: exit status 2" in ss_msg and not ss_msg.rstrip().endswith("f..."),
            f"{len(ss_msg)}B",
        )

        # session_start_brief separates remote Syncthing ThinkPad brief from local host disk/Gen3
        pf_remote = td_path / "PREFLIGHT_REMOTE.md"
        pf_remote.write_text(
            "# ⚡ WhiteMagic Fleet & Estate Pre-Flight Brief (2026-11-27T15:30:51+00:00)\n"
            "> Compiled in **12439.1 ms** | Disk: **40.77 GiB free** (77.4% used) | Gen3 Primary Store: **460,620 records**\n"
            "> **Hosted & VPS Health:** api: `up` · mcp: `up`\n"
            "home-lucas-SharedWorkspace-grok-fleet\n",
            encoding="utf-8",
        )
        _, out_ss_rem, _, _ = run_hook(
            START_HOOK,
            {"conversationId": "ss-remote"},
            env_extra={"WM_PREFLIGHT_PATH": str(pf_remote), "WM_SIM_LEDGER": str(ss_sim), "WM_DREAM_DIGEST": str(d_out), "WM_PREFLIGHT_IS_LOCAL": "0"},
        )
        ss_rem_msg = json.loads(out_ss_rem).get("injectSteps", [{}])[0].get("systemMessage", {}).get("systemMessage", "")
        check(
            "session_start_brief emits Local Host first & strips remote Disk/Gen3 from ThinkPad brief",
            "[Local Host |" in ss_rem_msg
            and "[Remote Estate Brief (Syncthing)" in ss_rem_msg
            and "40.77 GiB free" not in ss_rem_msg
            and "460,620 records" not in ss_rem_msg
            and "Hosted & VPS Health" in ss_rem_msg
            and len(ss_rem_msg.encode("utf-8")) <= 1450,
            ss_rem_msg[:220],
        )


def test_v450_rust_pretool_crystal_and_swarm_guards():
    print("\n12. v4.5.0 Rust --pre-tool, Ping-Pong Auto-Crystal, wm-reset & Orchestration:")
    hud_hook = _HOOKS_DIR / "longevity_hud.py"
    handoff_bin = LOCAL_BIN / "wm-handoff"
    reset_bin = LOCAL_BIN / "wm-reset"
    audit_bin = LOCAL_BIN / "agy-token-audit"

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        tp = td_path / "conv_v450_pingpong" / ".system_generated" / "logs" / "transcript.jsonl"
        tp.parent.mkdir(parents=True)
        rows = [
            {"type": "USER_INPUT", "content": "Harden v4.5.0 pipeline"},
            {"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-high", "input_tokens": 45000, "cache_read_tokens": 15000, "step_index": 1},
            {"type": "PLANNER_RESPONSE", "model": "gemini-4-hydrogen-high", "input_tokens": 90000, "cache_read_tokens": 20000, "step_index": 2},
            {"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-high", "input_tokens": 115000, "cache_read_tokens": 50000, "step_index": 3,
             "tool_calls": [{"name": "edit_file", "args": {"Path": str(td_path / "edited.py")}}]},
        ]
        tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        big400 = td_path / "big400.py"
        big400.write_text("\n".join(f"line_{i} = {i}" for i in range(400)) + "\n", encoding="utf-8")

        # 12a) Rust --pre-tool parity vs Python + >=160k <=100L clamp + swarm/subagent/for/test guards
        parity_cases = [
            ("read_165k", {"toolCall": {"name": "read_file", "args": {"Path": str(big400)}}, "transcriptPath": str(tp), "conversationId": "v450-r1"}),
            ("write_env", {"toolCall": {"name": "write_file", "args": {"Path": str(td_path / ".env"), "Content": "KEY=1"}}, "conversationId": "v450-w1"}),
            ("swarm_fork", {"toolCall": {"name": "swarm", "args": {"action": "add", "name": "w1", "fork": "coordinator", "model": "gemini-4-hydrogen-high", "brief": "do x"}}, "transcriptPath": str(tp), "conversationId": "v450-s1"}),
            ("swarm_nomodel", {"toolCall": {"name": "swarm", "args": {"action": "add", "name": "w2", "workspace": "share", "brief": "do y"}}, "transcriptPath": str(tp), "conversationId": "v450-s2"}),
            ("swarm_bloat", {"toolCall": {"name": "swarm", "args": {"action": "add", "name": "w3", "model": "gemini-4-hydrogen-high", "brief": "Z" * 6200}}, "transcriptPath": str(tp), "conversationId": "v450-s3"}),
            ("post_bloat", {"toolCall": {"name": "post_message", "args": {"to": "w1", "body": "B" * 4200}}, "conversationId": "v450-p1"}),
            ("subagent_res", {"toolCall": {"name": "invoke_subagent", "args": {"TypeName": "research", "Model": "inherit", "Prompt": "scan"}}, "transcriptPath": str(tp), "conversationId": "v450-i1"}),
            ("cmd_test", {"toolCall": {"name": "run_command", "args": {"CommandLine": "python3 tests/test_wave3.py"}}, "conversationId": "v450-c1"}),
            ("cmd_bench", {"toolCall": {"name": "run_command", "args": {"CommandLine": "python3 benchmarks/bench_v44_longevity.py"}}, "conversationId": "v450-c2"}),
            ("cmd_other", {"toolCall": {"name": "run_command", "args": {"CommandLine": "python3 scratch/custom.py"}}, "conversationId": "v450-c3"}),
            ("cmd_for_ro", {"toolCall": {"name": "run_command", "args": {"CommandLine": 'for f in *.md; do head -n 5 "$f"; done'}}, "conversationId": "v450-c4"}),
            ("cmd_for_rm", {"toolCall": {"name": "run_command", "args": {"CommandLine": 'for f in *; do rm "$f"; done'}}, "conversationId": "v450-c5"}),
            ("cmd_for_redir", {"toolCall": {"name": "run_command", "args": {"CommandLine": 'for f in *.md; do head -n 5 "$f" > out.txt; done'}}, "conversationId": "v450-c6"}),
            ("wm_recall", {"toolCall": {"name": "mcp_whitemagic_memory_recall", "args": {"query": "test"}}, "conversationId": "v450-m1"}),
            ("read_350_250", {"toolCall": {"name": "read_file", "args": {"Path": str(big400), "StartLine": 350, "EndLine": 250}}, "conversationId": "v450-r2"}),
            ("read_500_250", {"toolCall": {"name": "read_file", "args": {"Path": str(big400), "StartLine": 500, "EndLine": 250}}, "conversationId": "v450-r3"}),
            ("read_351_750", {"toolCall": {"name": "read_file", "args": {"Path": str(big400), "StartLine": 351, "EndLine": 750}}, "conversationId": "v450-r4"}),
            ("read_350_260", {"toolCall": {"name": "read_file", "args": {"Path": str(big400), "StartLine": 350, "EndLine": 260}}, "conversationId": "v450-r5"}),
            ("read_0_0", {"toolCall": {"name": "read_file", "args": {"Path": str(big400), "StartLine": 0, "EndLine": 0}}, "conversationId": "v450-r6"}),
            ("read_none_0", {"toolCall": {"name": "read_file", "args": {"Path": str(big400), "EndLine": 0}}, "conversationId": "v450-r7"}),
        ]
        py_res = {}
        rs_res = {}
        for label, pl in parity_cases:
            s_in = json.dumps(pl)
            p1 = subprocess.run(["python3", "-S", "-B", str(PRE_HOOK)], input=s_in, capture_output=True, text=True, env={**os.environ, "WM_HOOK_STATE_DIR": str(td_path / "st_py")}, timeout=5)
            p2 = subprocess.run([str(WM_SQUEEZE), "--pre-tool", "--py", str(PRE_HOOK)], input=s_in, capture_output=True, text=True, env={**os.environ, "WM_HOOK_STATE_DIR": str(td_path / "st_rs")}, timeout=5)
            py_res[label] = json.loads(p1.stdout)
            rs_res[label] = json.loads(p2.stdout)
        parity_all = all(
            py_res[k].get("decision") == rs_res[k].get("decision") and py_res[k].get("overwrite") == rs_res[k].get("overwrite")
            for k in py_res
        )
        check("Rust wm-squeeze --pre-tool 20-case exact parity vs Python", parity_all)
        check(
            "Rust --pre-tool read_file inverted/clamped ranges never emit StartLine > EndLine",
            rs_res["read_350_250"].get("overwrite") == {"EndLine": 549}
            and rs_res["read_500_250"].get("overwrite") == {"EndLine": 699}
            and rs_res["read_351_750"].get("overwrite") == {"EndLine": 550}
            and rs_res["read_350_260"].get("overwrite") == {"StartLine": 260, "EndLine": 350}
            and rs_res["read_0_0"].get("overwrite") == {"StartLine": 1, "EndLine": 200}
            and rs_res["read_none_0"].get("overwrite") == {"StartLine": 1, "EndLine": 200},
            str({k: rs_res[k] for k in ("read_350_250", "read_500_250", "read_351_750", "read_350_260", "read_0_0", "read_none_0")}),
        )
        check(">=160k ctx read_file clamps to EndLine=100", rs_res["read_165k"].get("overwrite") == {"StartLine": 1, "EndLine": 100}, str(rs_res["read_165k"]))
        check("swarm(add) fork>=80k / Neon omitted-model / >6KB brief force_ask", all(rs_res[k].get("decision") == "force_ask" for k in ("swarm_fork", "swarm_nomodel", "swarm_bloat")))
        check("post_message >4KB & invoke_subagent(research, inherit on Neon) force_ask", rs_res["post_bloat"].get("decision") == "force_ask" and rs_res["subagent_res"].get("decision") == "force_ask")

        p_auto = subprocess.run(
            [str(WM_SQUEEZE), "--pre-tool", "--py", str(PRE_HOOK)],
            input=json.dumps({"toolCall": {"name": "invoke_subagent", "args": {"TypeName": "research", "Model": "inherit", "Prompt": "scan"}}, "transcriptPath": str(tp), "conversationId": "v450-i2"}),
            capture_output=True, text=True, env={**os.environ, "WM_SUBAGENT_AUTO_FLASH": "1"}, timeout=5,
        )
        check("WM_SUBAGENT_AUTO_FLASH=1 rewrites research Model=flash", json.loads(p_auto.stdout).get("overwrite") == {"Model": "flash"}, p_auto.stdout)
        check("safe python3 tests/bench & read-only for squeezed; other/unsafe for untouched",
              "--pipe" in (rs_res["cmd_test"].get("overwrite") or {}).get("CommandLine", "")
              and "--pipe" in (rs_res["cmd_bench"].get("overwrite") or {}).get("CommandLine", "")
              and "overwrite" not in rs_res["cmd_other"]
              and "--pipe" in (rs_res["cmd_for_ro"].get("overwrite") or {}).get("CommandLine", "")
              and "overwrite" not in rs_res["cmd_for_rm"]
              and "overwrite" not in rs_res["cmd_for_redir"])

        hot_pl = json.dumps({"toolCall": {"name": "read_file", "args": {"Path": str(big400), "StartLine": 1, "EndLine": 80}}, "conversationId": "v450-hot"})
        pre_lats = []
        for _ in range(15):
            t0 = time.perf_counter()
            subprocess.run([str(WM_SQUEEZE), "--pre-tool", "--py", str(PRE_HOOK)], input=hot_pl, capture_output=True, text=True, timeout=5)
            pre_lats.append((time.perf_counter() - t0) * 1000.0)
        pre_lats.sort()
        pre_med = pre_lats[len(pre_lats) // 2]
        check(f"Rust wm-squeeze --pre-tool hot-path latency < 15ms ({pre_med:.1f}ms)", pre_med < 15.0)

        # 12b) Multi-turn HUD Ping-Pong Alert + crystal hint parity
        hcache = td_path / "hcache"
        hcache.mkdir()
        (hcache / "latest_hud_v450.md").write_text("# Crystal\n", encoding="utf-8")
        hp_tp = td_path / "hud_tp.jsonl"
        env_hp = {**os.environ, "WM_HOOK_STATE_DIR": str(td_path / "hp_py"), "WM_HANDOFF_CACHE_DIR": str(hcache)}
        env_hr = {**os.environ, "WM_HOOK_STATE_DIR": str(td_path / "hp_rs"), "WM_HANDOFF_CACHE_DIR": str(hcache)}
        hp_last = hr_last = {}
        for idx in range(1, 4):
            hp_tp.write_text("\n".join(json.dumps(r) for r in rows[1 : idx + 1]) + "\n", encoding="utf-8")
            h_in = json.dumps({"transcriptPath": str(hp_tp), "conversationId": "hud_v450"})
            hp_last = json.loads(subprocess.run(["python3", "-S", "-B", str(hud_hook)], input=h_in, capture_output=True, text=True, env=env_hp, timeout=5).stdout)
            hr_last = json.loads(subprocess.run([str(WM_SQUEEZE), "--hud"], input=h_in, capture_output=True, text=True, env=env_hr, timeout=5).stdout)
        hr_md = ((hr_last.get("injectSteps") or [{}])[0].get("systemMessage") or {}).get("metadata", {}).get("user_markdown", "")
        check("Multi-turn HUD Ping-Pong + Crystal hint Python/Rust byte parity", hp_last == hr_last and "Orchestrator Ping-Pong Alert" in hr_md and "wm-reset" in hr_md, hr_md[:140])

        # 12c) wm-handoff --transcript --emit-receipt (HMAC) & --swarm-map
        import hashlib, hmac
        c_md = td_path / "crystal_test.md"
        c_rec = td_path / "crystal_test.receipt.json"
        r_ho = subprocess.run(
            [str(handoff_bin), "--transcript", str(tp), "--cwd", str(KIT), "--out", str(c_md), "--emit-receipt", str(c_rec)],
            capture_output=True, text=True, env={**os.environ, "WM_CONFINEMENT_SIGNING_KEY": "v450_hmac_secret"}, timeout=10,
        )
        rec_j = json.loads(c_rec.read_text(encoding="utf-8"))
        sig_j = dict(rec_j)
        sig_obj = sig_j.pop("signature", {})
        exp_hmac = hmac.new(b"v450_hmac_secret", json.dumps(sig_j, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        check("wm-handoff wm-receipt/1.2 + HMAC-SHA256 signature valid", r_ho.returncode == 0 and rec_j.get("schema") == "wm-receipt/1.2" and rec_j.get("model_flips") == 2 and sig_obj.get("sig") == exp_hmac)

        r_sm = subprocess.run([str(handoff_bin), "--swarm-map", str(KIT)], capture_output=True, text=True, timeout=10)
        check("wm-handoff --swarm-map <=600 tok (<=2450B) with SHA", r_sm.returncode == 0 and len(r_sm.stdout.encode("utf-8")) <= 2450 and "swarm_map_sha256=" in r_sm.stdout)

        # 12d) stop_telemetry auto-crystal, fullyIdle=False skip, rate-limit skip, & wm-reset
        stop_cache = td_path / "stop_cache"
        env_st = {**os.environ, "WM_AGY_TELEMETRY": str(td_path / "tel.jsonl"), "WM_HANDOFF_CACHE_DIR": str(stop_cache), "WM_HANDOFF_BIN": str(handoff_bin)}
        run_hook(STOP_HOOK, {"transcriptPath": str(tp), "conversationId": "conv_v450_pingpong", "cwd": str(KIT), "fullyIdle": False, "terminationReason": "NO_TOOL_CALL"}, env_extra=env_st)
        check("stop_telemetry skips auto-crystal when fullyIdle=False", not (stop_cache / "latest_conv_v450_pingpong.md").exists())
        run_hook(STOP_HOOK, {"transcriptPath": str(tp), "conversationId": "conv_v450_pingpong", "cwd": str(KIT), "fullyIdle": True, "terminationReason": "NO_TOOL_CALL"}, env_extra=env_st)
        sc_rec = stop_cache / "latest_conv_v450_pingpong.receipt.json"
        m1 = sc_rec.stat().st_mtime_ns if sc_rec.is_file() else 0
        time.sleep(0.03)
        run_hook(STOP_HOOK, {"transcriptPath": str(tp), "conversationId": "conv_v450_pingpong", "cwd": str(KIT), "fullyIdle": True, "terminationReason": "NO_TOOL_CALL"}, env_extra=env_st)
        check("stop_telemetry auto-crystal + 120s/25KB rate-limit skip", sc_rec.is_file() and sc_rec.stat().st_mtime_ns == m1)

        r_rst = subprocess.run([str(reset_bin), "--cwd", str(KIT), "--dry-run"], capture_output=True, text=True, env=env_st, timeout=10)
        r_pc = subprocess.run([str(reset_bin), "--cwd", str(KIT), "--print-crystal"], capture_output=True, text=True, env=env_st, timeout=10)
        rst_j = json.loads(r_rst.stdout)
        check("wm-reset --dry-run & --print-crystal PASS", r_rst.returncode == 0 and rst_j.get("model") == "gemini-4-hydrogen-high" and rst_j.get("receipt_schema") == "wm-receipt/1.2" and "crystal_sha256=" in r_pc.stdout)

        # 12e) agy-token-audit --json orchestration metrics + 3 native agents
        fake_home = td_path / "home_aud"
        for cid, m_name, extra in [
            ("aud_1111", "gemini-4-neon-high", [{"name": "read_file", "args": {"Path": "/tmp/shared.py"}}, {"name": "exit_plan_mode", "args": {}}]),
            ("aud_2222", "gemini-4-hydrogen-high", [{"name": "read_file", "args": {"Path": "/tmp/shared.py"}}, {"name": "enter_plan_mode", "args": {}}]),
        ]:
            ltp = fake_home / ".gemini" / "antigravity" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
            ltp.parent.mkdir(parents=True)
            ltp.write_text(json.dumps({"type": "PLANNER_RESPONSE", "model": m_name, "input_tokens": 12000, "cache_read_tokens": 30000, "output_tokens": 500, "tool_calls": extra}) + "\n")
        r_aud = subprocess.run([str(audit_bin), "--recent", "5", "--json"], capture_output=True, text=True, env={**os.environ, "HOME": str(fake_home)}, timeout=10)
        aud_j = json.loads(r_aud.stdout)
        agg = aud_j.get("aggregate", {})
        check(
            "agy-token-audit --json orchestration metrics & no _read_files leak",
            r_aud.returncode == 0
            and agg.get("exit_plan_count") == 1
            and agg.get("one_turn_neon_plans") == 1
            and agg.get("enter_plan_count") == 1
            and agg.get("cross_session_dup_reads") == 1
            and all("_read_files" not in s for s in aud_j.get("sessions", [])),
            json.dumps(agg),
        )
        check("3 native plugin agents present", all((KIT / "plugin" / "agents" / f).is_file() for f in ("hydrogen-builder.md", "fresh-verifier.md", "swarm-coordinator.md")))


def test_v451_sidecar_cpr_bridge_and_plugin_scoping():
    print("\n13. v4.5.1 Resident Sidecar UI, Sangha CPR Bridge & Plugin Scoping:")
    import hashlib
    import hmac
    import signal
    import urllib.error
    import urllib.request

    sidecar_dir = KIT / "plugin" / "sidecars" / "wm-sidecar"
    sc_manifest = json.loads((sidecar_dir / "sidecar.json").read_text(encoding="utf-8"))
    check(
        "sidecar.json valid schema + AUX_PANE UI config",
        sc_manifest.get("has_web_ui") is True
        and any(v.get("entrypoint") == "SIDECAR_UI_ENTRYPOINT_AUX_PANE" for v in (sc_manifest.get("ui_config") or {}).get("views", [])),
    )

    handoff_bin = LOCAL_BIN / "wm-handoff"
    bridge_bin = LOCAL_BIN / "wm-cpr-bridge"
    sim_bin = LOCAL_BIN / "wm-sim"
    check("wm-cpr-bridge installed & executable", bridge_bin.is_file() and os.access(bridge_bin, os.X_OK))

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cache_dir = td_path / "cache"
        cache_dir.mkdir()
        srv_py = sidecar_dir / "server.py"
        env_sc = {
            **os.environ,
            "ANTIGRAVITY_SIDECAR_WEB_PORT": "0",
            "WM_HANDOFF_CACHE_DIR": str(cache_dir),
            "WM_CONFINEMENT_SIGNING_KEY": "v451_test_hmac_secret",
        }
        proc = subprocess.Popen(["python3", "-S", "-B", str(srv_py)], env=env_sc, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            disc = cache_dir / "sidecar.json"
            for _ in range(50):
                if disc.is_file():
                    break
                time.sleep(0.05)
            check("sidecar writes discovery sidecar.json", disc.is_file())
            info = json.loads(disc.read_text(encoding="utf-8"))
            url = info["url"]

            with urllib.request.urlopen(f"{url}/healthz", timeout=2) as r:
                hz = json.loads(r.read())
                check("sidecar GET /healthz 200 ok", r.status == 200 and hz.get("ok") is True and hz.get("version") == "4.7.0")
            with urllib.request.urlopen(f"{url}/", timeout=2) as r:
                html = r.read().decode("utf-8")
                check(
                    "sidecar GET / serves no-store UI with Declarative WebMCP controls",
                    r.status == 200
                    and "WhiteMagic AGY Telemetry" in html
                    and r.headers.get("Cache-Control") == "no-store"
                    and 'toolname="wm_sidecar_status"' in html
                    and 'toolname="wm_sidecar_verify_receipt"' in html
                    and "navigator.modelContext" in html,
                )
            for wmcp_path in ("/api/webmcp", "/.well-known/webmcp.json"):
                with urllib.request.urlopen(f"{url}{wmcp_path}", timeout=2) as r:
                    wmcp = json.loads(r.read())
                    tool_names = {t.get("name") for t in (wmcp.get("tools") or [])}
                    check(
                        f"sidecar GET {wmcp_path} returns webmcp/1.0 manifest",
                        r.status == 200
                        and wmcp.get("schema") == "webmcp/1.0"
                        and {"wm_sidecar_status", "wm_sidecar_crystals", "wm_sidecar_receipts", "wm_sidecar_verify_receipt"}.issubset(tool_names),
                    )
            with urllib.request.urlopen(f"{url}/api/status", timeout=2) as r:
                st_j = json.loads(r.read())
                check("sidecar GET /api/status returns valid payload", st_j.get("ok") is True and "telemetry" in st_j and "wm_squeeze" in st_j)
            with urllib.request.urlopen(f"{url}/api/crystals", timeout=2) as r:
                cr_j = json.loads(r.read())
                check("sidecar GET /api/crystals ok", cr_j.get("ok") is True and isinstance(cr_j.get("crystals"), list))

            # POST /api/receipt valid 201, bad HMAC 400, oversized 413
            core = {"schema": "wm-receipt/1.2", "kind": "handoff_crystal", "conv_id": "v451sc1", "model": "gemini-4-hydrogen-high", "crystal_sha256": "abcd12345678"}
            canon = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
            signed = {**core, "signature": {"alg": "hmac-sha256", "sig": hmac.new(b"v451_test_hmac_secret", canon, hashlib.sha256).hexdigest()}}
            req_ok = urllib.request.Request(f"{url}/api/receipt", data=json.dumps(signed).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req_ok, timeout=2) as r:
                check("sidecar POST /api/receipt valid HMAC -> 201", r.status == 201)

            bad_req = urllib.request.Request(f"{url}/api/receipt", data=json.dumps({**signed, "crystal_sha256": "000000000000"}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            code_bad = 0
            try:
                urllib.request.urlopen(bad_req, timeout=2)
            except urllib.error.HTTPError as e:
                code_bad = e.code
            check("sidecar POST /api/receipt tampered HMAC -> 400", code_bad == 400)

            big_req = urllib.request.Request(f"{url}/api/receipt", data=b"{}" + b" " * 17000, headers={"Content-Type": "application/json"}, method="POST")
            code_big = 0
            try:
                urllib.request.urlopen(big_req, timeout=2)
            except urllib.error.HTTPError as e:
                code_big = e.code
            check("sidecar POST /api/receipt oversized -> 413", code_big == 413)

            # wm-handoff --publish-sangha + wm-cpr-bridge verify/pointer/tamper/fail-open
            tp = td_path / "conv_bridge" / ".system_generated" / "logs" / "transcript.jsonl"
            tp.parent.mkdir(parents=True)
            tp.write_text("\n".join([
                json.dumps({"type": "USER_INPUT", "content": "Verify v4.5.1 CPR bridge"}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-hydrogen-high", "input_tokens": 42000, "cache_read_tokens": 80000, "tool_calls": [{"name": "edit_file", "args": {"Path": "server.py"}}]}),
            ]) + "\n", encoding="utf-8")

            mock_log = td_path / "sangha_calls.jsonl"
            mock_sangha = td_path / "mock_sangha.py"
            mock_sangha.write_text(
                f"#!/usr/bin/env python3\nimport json, sys\nfrom pathlib import Path\nPath({str(mock_log)!r}).open('a').write(json.dumps(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            mock_sangha.chmod(0o755)

            crystal_md = cache_dir / "latest_conv_bridge.md"
            receipt_js = cache_dir / "latest_conv_bridge.receipt.json"
            env_br = {**env_sc, "WM_SANGHA_BIN": str(mock_sangha)}
            r_ho = subprocess.run(
                [str(handoff_bin), "--transcript", str(tp), "--cwd", str(KIT), "--out", str(crystal_md), "--emit-receipt", str(receipt_js), "--publish-sangha", "--target", "@opencode", "--room", "core-dev"],
                capture_output=True, text=True, env=env_br, timeout=10,
            )
            calls = [json.loads(l) for l in mock_log.read_text(encoding="utf-8").splitlines()] if mock_log.is_file() else []
            body_txt = calls[0][calls[0].index("--body") + 1] if calls and "--body" in calls[0] else ""
            check(
                "wm-handoff --publish-sangha forwards <=600B CPR pointer to mock Sangha",
                r_ho.returncode == 0 and len(calls) == 1 and "@opencode" in calls[0] and "computed" in calls[0] and 0 < len(body_txt.encode("utf-8")) <= 600,
                f"{len(body_txt.encode('utf-8'))}B",
            )

            r_ver = subprocess.run([str(bridge_bin), "verify", str(receipt_js)], capture_output=True, text=True, env=env_br, timeout=5)
            r_ptr = subprocess.run([str(bridge_bin), "pointer", "--receipt", str(receipt_js)], capture_output=True, text=True, env=env_br, timeout=5)
            check("wm-cpr-bridge verify (HMAC) & pointer <=600B", r_ver.returncode == 0 and json.loads(r_ver.stdout).get("sig_status") == "verified" and r_ptr.returncode == 0 and len(r_ptr.stdout.encode("utf-8")) <= 600)

            r_pub_fo = subprocess.run([str(bridge_bin), "publish", "--receipt", str(receipt_js)], capture_output=True, text=True, env={**env_sc, "WM_SANGHA_BIN": "/nonexistent/sangha"}, timeout=5)
            fo_j = json.loads(r_pub_fo.stdout) if r_pub_fo.returncode == 0 else {}
            check("wm-cpr-bridge publish fail-open without Sangha + notifies sidecar", r_pub_fo.returncode == 0 and fo_j.get("sangha_posted") is False and fo_j.get("sidecar_notified") is True and fo_j.get("valid") is True)

            bad_hmac_js = td_path / "bad_hmac.receipt.json"
            rj = json.loads(receipt_js.read_text(encoding="utf-8"))
            rj["signature"]["sig"] = "f" * 64
            bad_hmac_js.write_text(json.dumps(rj), encoding="utf-8")
            r_bh = subprocess.run([str(bridge_bin), "verify", str(bad_hmac_js)], capture_output=True, text=True, env=env_br, timeout=5)
            check("wm-cpr-bridge verify fails closed on tampered HMAC (rc=2)", r_bh.returncode == 2)

            crystal_md.write_text("# tampered\n\n`crystal_sha256=deadbeef1234`\n", encoding="utf-8")
            r_bc = subprocess.run([str(bridge_bin), "verify", str(receipt_js)], capture_output=True, text=True, env=env_br, timeout=5)
            check("wm-cpr-bridge verify fails closed on tampered crystal file (rc=2)", r_bc.returncode == 2)
        finally:
            proc.send_signal(signal.SIGTERM)
            rc_sc = proc.wait(timeout=4)
        check("sidecar clean SIGTERM exit & removes sidecar.json", rc_sc == 0 and not (cache_dir / "sidecar.json").exists())

        # wm-sim audit --scope-plugins on isolated config dir (systems repo vs web repo)
        cfg_dir = td_path / "agy_cfg"
        for pname, desc in (("chrome-devtools-plugin", "web"), ("modern-web-guidance-plugin", "web"), ("google-antigravity-sdk", "sdk")):
            sk = cfg_dir / "plugins" / pname / "skills" / "s1" / "SKILL.md"
            sk.parent.mkdir(parents=True)
            sk.write_text(f"---\nname: {pname}-skill\ndescription: {'X' * 400}\n---\n", encoding="utf-8")
        (cfg_dir / "config.json").write_text(
            json.dumps({"plugins": {p: {"enabled": True} for p in ("chrome-devtools-plugin", "modern-web-guidance-plugin", "google-antigravity-sdk", "whitemagic-agy-kit")}}),
            encoding="utf-8",
        )
        env_sim = {**os.environ, "WM_AGY_CONFIG_DIR": str(cfg_dir)}

        sys_repo = td_path / "rust_cli_repo"
        sys_repo.mkdir()
        (sys_repo / "Cargo.toml").write_text('[package]\nname="sys"\n', encoding="utf-8")
        r_sys = subprocess.run([str(sim_bin), "audit", "--scope-plugins", str(sys_repo), "--json"], capture_output=True, text=True, env=env_sim, timeout=5)
        sys_j = json.loads(r_sys.stdout)
        check("wm-sim audit --scope-plugins systems repo -> matched=[] & savings>0", r_sys.returncode == 0 and sys_j.get("matched_domain_plugins") == [] and sys_j.get("savings_est_tokens", 0) > 200)

        web_repo = td_path / "react_web_repo"
        web_repo.mkdir()
        (web_repo / "index.html").write_text("<html></html>\n", encoding="utf-8")
        (web_repo / "package.json").write_text('{"dependencies":{"react":"^19.0.0"}}\n', encoding="utf-8")
        r_web = subprocess.run([str(sim_bin), "audit", "--scope-plugins", str(web_repo), "--write", "--json"], capture_output=True, text=True, env=env_sim, timeout=5)
        web_j = json.loads(r_web.stdout)
        pj_path = web_repo / ".agents" / "plugins.json"
        pj = json.loads(pj_path.read_text(encoding="utf-8")) if pj_path.is_file() else {}
        inc = ((pj.get("entries") or [{}])[0]).get("include_only", [])
        check("wm-sim audit --scope-plugins --write emits web .agents/plugins.json", r_web.returncode == 0 and pj_path.is_file() and "chrome-devtools-plugin" in inc and "modern-web-guidance-plugin" in inc and "google-antigravity-sdk" in web_j.get("unmatched_enabled_plugins", []))


def test_v460_multihost_codex_cursor_and_cpr_translate():
    import hashlib
    import hmac as _hmac
    import signal
    import urllib.request

    print("\n14. v4.6.0 Multi-Host (Codex + Cursor), Mandala CPR Translate, Multi-Launcher wm-reset & Installer:")
    SQ_BIN = LOCAL_BIN / "wm-squeeze"
    bridge_bin = KIT / "bin" / "wm-cpr-bridge"
    reset_bin = KIT / "bin" / "wm-reset"
    sidecar_py = KIT / "plugin" / "sidecars" / "wm-sidecar" / "server.py"

    # 14a) wm-squeeze --pre-tool --host codex
    def run_sq(args, payload, env_extra=None):
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        p = subprocess.run([str(SQ_BIN), *args], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=5)
        return p.returncode, json.loads(p.stdout.strip()) if p.stdout.strip().startswith("{") else p.stdout.strip()

    rc, cr = run_sq(["--pre-tool", "--host", "codex"], {"tool_name": "Bash", "tool_input": {"command": "git status"}, "session_id": "cx-1"})
    check(
        "wm-squeeze --host codex rewrites Bash git status -> hookSpecificOutput.updatedInput",
        rc == 0 and cr.get("hookSpecificOutput", {}).get("permissionDecision") == "allow" and "wm-squeeze --safe -- git status" in cr.get("hookSpecificOutput", {}).get("updatedInput", {}).get("command", ""),
        str(cr),
    )
    rc, cd = run_sq(["--pre-tool", "--host", "codex"], {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}})
    check(
        "wm-squeeze --host codex destructive git push --force origin main -> permissionDecision=ask",
        rc == 0 and cd.get("hookSpecificOutput", {}).get("permissionDecision") == "ask",
        str(cd),
    )
    rc, ck = run_sq(["--pre-tool", "--host", "codex"], {"tool_name": "Bash", "tool_input": {"command": "cat ~/.ssh/id_rsa"}})
    check(
        "wm-squeeze --host codex Kekkai .ssh/id_rsa -> ask (never AGY JSON)",
        rc == 0 and ck.get("hookSpecificOutput", {}).get("permissionDecision") == "ask" and "force_ask" not in json.dumps(ck),
        str(ck),
    )
    rc, ce = run_sq(["--pre-tool", "--host", "codex"], {"tool_name": "apply_patch", "tool_input": "*** Begin Patch\n*** Update File: app/.env\n+KEY=1\n"})
    check(
        "wm-squeeze --host codex apply_patch .env -> permissionDecision=ask",
        rc == 0 and ce.get("hookSpecificOutput", {}).get("permissionDecision") == "ask",
        str(ce),
    )

    # 14b) wm-squeeze --pre-tool --host cursor across events + --post-tool + sessionStart/stop
    rc, cu0 = run_sq(["--pre-tool", "--host", "cursor", "--event", "beforeShellExecution"], {"command": "git diff --stat", "conversation_id": "cur-1"})
    check(
        "wm-squeeze --host cursor beforeShellExecution safe -> permission=allow (no updated_input)",
        rc == 0 and cu0.get("permission") == "allow" and "updated_input" not in cu0,
        str(cu0),
    )
    rc, cu0_ask = run_sq(["--pre-tool", "--host", "cursor", "--event", "beforeShellExecution"], {"command": "git push --force origin main", "conversation_id": "cur-1"})
    check(
        "wm-squeeze --host cursor beforeShellExecution destructive -> permission=ask + user_message",
        rc == 0 and cu0_ask.get("permission") == "ask" and "user_message" in cu0_ask and "agent_message" in cu0_ask,
        str(cu0_ask),
    )
    rc, cu1 = run_sq(["--pre-tool", "--host", "cursor", "--event", "preToolUse"], {"tool_name": "Shell", "tool_input": {"command": "git diff --stat"}, "conversation_id": "cur-1"})
    check(
        "wm-squeeze --host cursor preToolUse Shell -> permission=allow + updated_input",
        rc == 0 and cu1.get("permission") == "allow" and "wm-squeeze --safe -- git diff --stat" in cu1.get("updated_input", {}).get("command", ""),
        str(cu1),
    )
    rc, cu2 = run_sq(["--pre-tool", "--host", "cursor", "--event", "preToolUse"], {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/sub/.env.local"}})
    check(
        "wm-squeeze --host cursor preToolUse Edit .env.local -> permission=ask",
        rc == 0 and cu2.get("permission") == "ask" and "agent_message" in cu2,
        str(cu2),
    )
    rc, cu3 = run_sq(["--pre-tool", "--host", "cursor", "--event", "beforeMCPExecution"], {"tool_name": "memory_recall", "tool_input": {"query": "cat ~/.ssh/id_ed25519"}})
    check("wm-squeeze --host cursor beforeMCPExecution Kekkai -> permission=ask", rc == 0 and cu3.get("permission") == "ask", str(cu3))
    rc, p1 = run_sq(["--post-tool", "--host", "codex"], {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    rc2, p2 = run_sq(["--post-tool", "--host", "cursor"], {"tool_name": "Shell", "tool_input": {"command": "ls"}})
    check("wm-squeeze --post-tool --host codex|cursor -> {}", rc == 0 and p1 == {} and rc2 == 0 and p2 == {})

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # Cursor preToolUse Read unbounded huge file -> clamped to default limit=250; explicit limit untouched
        big_file = td_path / "big.txt"
        big_file.write_text("\n".join(f"line_{i:04d} padding text for size" for i in range(520)) + "\n", encoding="utf-8")
        no_prof_env = {**os.environ, "WM_GRIMOIRE_PROFILE": str(td_path / "nonexistent_profile.json")}
        r1_p = subprocess.run([str(SQ_BIN), "--pre-tool", "--host", "cursor", "--event", "preToolUse"], input=json.dumps({"tool_name": "Read", "tool_input": {"path": str(big_file)}}), capture_output=True, text=True, env=no_prof_env, timeout=5)
        r2_p = subprocess.run([str(SQ_BIN), "--pre-tool", "--host", "cursor", "--event", "preToolUse"], input=json.dumps({"tool_name": "Read", "tool_input": {"path": str(big_file), "limit": 500}}), capture_output=True, text=True, env=no_prof_env, timeout=5)
        rc_r1, cu_r1 = r1_p.returncode, json.loads(r1_p.stdout.strip() or "{}")
        rc_r2, cu_r2 = r2_p.returncode, json.loads(r2_p.stdout.strip() or "{}")
        check(
            "wm-squeeze --host cursor preToolUse Read clamps unbounded >400L file but honors explicit limit",
            rc_r1 == 0 and cu_r1.get("updated_input", {}).get("limit") == 250 and cu_r1.get("updated_input", {}).get("path") == str(big_file)
            and rc_r2 == 0 and "updated_input" not in cu_r2,
            f"r1={cu_r1} r2={cu_r2}",
        )
        # Adaptive grimoire_profile.json overrides Read clamp thresholds dynamically
        med_file = td_path / "med.txt"
        med_file.write_text("\n".join(f"med_{i:04d} realistic source code line padding text" for i in range(220)) + "\n", encoding="utf-8")
        prof_file = td_path / "grimoire_profile.json"
        prof_file.write_text(json.dumps({"model_tuning": {"max_unbounded_read_lines": 180, "clamp_read_lines": 120, "wide_jsonl_lines": 30}}), encoding="utf-8")
        r_prof = subprocess.run(
            [str(SQ_BIN), "--pre-tool", "--host", "cursor", "--event", "preToolUse"],
            input=json.dumps({"tool_name": "Read", "tool_input": {"path": str(med_file)}}),
            capture_output=True, text=True, env={**os.environ, "WM_GRIMOIRE_PROFILE": str(prof_file)}, timeout=5,
        )
        cu_prof = json.loads(r_prof.stdout.strip() or "{}")
        check(
            "wm-squeeze --host cursor preToolUse Read adapts clamp dynamically from grimoire_profile.json",
            r_prof.returncode == 0 and cu_prof.get("updated_input", {}).get("limit") == 120,
            f"prof={cu_prof}",
        )
        # Cursor postToolUseFailure (error_message) -> sim_ledger.jsonl & postToolUse spill -> agy_telemetry.jsonl
        cu_sim = td_path / "cursor_sim.jsonl"
        cu_tel = td_path / "cursor_tel.jsonl"
        env_cu_pt = {**os.environ, "WM_SIM_LEDGER": str(cu_sim), "WM_AGY_TELEMETRY": str(cu_tel)}
        r_fail = subprocess.run(
            [str(SQ_BIN), "--post-tool", "--host", "cursor"],
            input=json.dumps({"tool_name": "Shell", "tool_input": {"command": "pytest -q"}, "error_message": "Command timed out after 30s", "failure_type": "timeout", "conversation_id": "cur-fail-1"}),
            capture_output=True, text=True, env=env_cu_pt, timeout=5,
        )
        r_sp = subprocess.run(
            [str(SQ_BIN), "--post-tool", "--host", "cursor"],
            input=json.dumps({"tool_name": "Shell", "tool_input": {"command": "cat huge.log"}, "tool_output": "x" * 17000, "conversation_id": "cur-sp-1"}),
            capture_output=True, text=True, env=env_cu_pt, timeout=5,
        )
        sim_txt = cu_sim.read_text(encoding="utf-8") if cu_sim.is_file() else ""
        tel_txt = cu_tel.read_text(encoding="utf-8") if cu_tel.is_file() else ""
        check(
            "wm-squeeze --post-tool --host cursor logs error_message->SIM and >16KB tool_output->telemetry",
            r_fail.returncode == 0 and '"host":"cursor"' in sim_txt and '"failure_type":"timeout"' in sim_txt
            and r_sp.returncode == 0 and '"event":"tool_spill"' in tel_txt and '"host":"cursor"' in tel_txt,
            f"sim={sim_txt!r} tel={tel_txt!r}",
        )
        # Cursor sessionStart & stop hooks
        r_ss = subprocess.run(
            [sys.executable, "-S", "-B", str(KIT / "plugin" / "hooks" / "session_start_brief.py"), "--host", "cursor"],
            input=json.dumps({"workspace_roots": [str(KIT)], "conversation_id": "cur-ss-1"}),
            capture_output=True, text=True, env=env_cu_pt, timeout=5,
        )
        ss_j = json.loads(r_ss.stdout) if r_ss.returncode == 0 and r_ss.stdout.strip() else {}
        r_st = subprocess.run(
            [sys.executable, "-S", "-B", str(KIT / "plugin" / "hooks" / "stop_telemetry.py"), "--host", "cursor"],
            input=json.dumps({"conversation_id": "cur-st-1", "model": "gemini-4-neon", "status": "completed", "loop_count": 0, "workspace_roots": [str(KIT)]}),
            capture_output=True, text=True, env=env_cu_pt, timeout=5,
        )
        tel_after_stop = cu_tel.read_text(encoding="utf-8") if cu_tel.is_file() else ""
        check(
            "session_start_brief.py & stop_telemetry.py --host cursor emit native Cursor schemas",
            r_ss.returncode == 0 and isinstance(ss_j.get("additional_context"), str) and "Workspace Snapshot" in ss_j.get("additional_context", "") and "injectSteps" not in ss_j
            and r_st.returncode == 0 and json.loads(r_st.stdout) == {} and '"conv_id": "cur-st-1"' in tel_after_stop,
            f"ss={list(ss_j.keys())} st={r_st.stdout!r}",
        )
        # 14c) wm-reset --launcher codex & cursor --dry-run
        cache_dir = td_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "latest_abc12345.md").write_text("# Handoff\nok\n", encoding="utf-8")
        (cache_dir / "latest_abc12345.receipt.json").write_text(json.dumps({"schema": "wm-receipt/1.2", "crystal_sha256": "deadbeef1234"}), encoding="utf-8")
        env_rs = {**os.environ, "WM_HANDOFF_CACHE_DIR": str(cache_dir)}
        r_cx = subprocess.run([str(reset_bin), "--launcher", "codex", "--conv", "abc12345", "--dry-run"], capture_output=True, text=True, env=env_rs, timeout=5)
        j_cx = json.loads(r_cx.stdout) if r_cx.returncode == 0 else {}
        r_cu = subprocess.run([str(reset_bin), "--launcher", "cursor", "--conv", "abc12345", "--print", "--dry-run"], capture_output=True, text=True, env=env_rs, timeout=5)
        j_cu = json.loads(r_cu.stdout) if r_cu.returncode == 0 else {}
        check(
            "wm-reset --launcher codex & cursor --dry-run resolve expected CLI flags",
            r_cx.returncode == 0 and j_cx.get("launcher") == "codex" and "-C " in j_cx.get("command_preview", "") and "gemini" not in j_cx.get("command_preview", "")
            and r_cu.returncode == 0 and j_cu.get("launcher") == "cursor" and "--workspace " in j_cu.get("command_preview", "") and " -p " in j_cu.get("command_preview", ""),
            f"codex={j_cx} cursor={j_cu}",
        )

        # 14d) wm-cpr-bridge verify & translate for mandala-cpr-pointer/0.5 + HMAC tamper fail-closed
        key = "unit-hmac-secret"
        ptr_core = {
            "schema": "mandala-cpr-pointer/0.5",
            "job_id": "job-1",
            "worker_id": "w1",
            "action": "mandala.exec",
            "slot_id": "slot-123456",
            "task_id": "urn:uuid:task-1",
            "sandbox_class": "landlock",
            "exit": 0,
            "stdout_hash": "sha256:" + ("a" * 64),
            "head_receipt_id": "urn:uuid:rcpt-4",
            "receipt_count": 4,
            "bundle_sha256": "sha256:" + ("b" * 64),
            "verdict": "TRUSTED",
        }
        canon = json.dumps(ptr_core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ptr_signed = {**ptr_core, "hmac_sha256": "hmac-sha256:" + _hmac.new(key.encode("utf-8"), canon, hashlib.sha256).hexdigest()}
        ptr_file = td_path / "mandala_ptr.json"
        ptr_file.write_text(json.dumps(ptr_signed), encoding="utf-8")
        env_hmac = {**os.environ, "WM_CPR_HMAC_KEY": key}

        rv_ok = subprocess.run([str(bridge_bin), "verify", str(ptr_file)], capture_output=True, text=True, env=env_hmac, timeout=5)
        rt_ok = subprocess.run([str(bridge_bin), "translate", str(ptr_file)], capture_output=True, text=True, env=env_hmac, timeout=5)
        translated = json.loads(rt_ok.stdout) if rt_ok.returncode == 0 else {}
        check(
            "wm-cpr-bridge verify & translate mandala-cpr-pointer/0.5 -> wm-receipt/1.2",
            rv_ok.returncode == 0 and json.loads(rv_ok.stdout).get("sig_status") == "verified"
            and rt_ok.returncode == 0 and translated.get("schema") == "wm-receipt/1.2" and translated.get("sandbox_class") == "landlock",
        )

        bad_ptr_file = td_path / "mandala_ptr_bad.json"
        bad_ptr_file.write_text(json.dumps({**ptr_signed, "hmac_sha256": "hmac-sha256:" + ("0" * 64)}), encoding="utf-8")
        rv_bad = subprocess.run([str(bridge_bin), "verify", str(bad_ptr_file)], capture_output=True, text=True, env=env_hmac, timeout=5)
        check("wm-cpr-bridge verify fails closed on tampered mandala pointer (rc=2)", rv_bad.returncode == 2)

        # Sidecar v4.6.0 accepts mandala-cpr-pointer/0.5
        sc_env = {
            **os.environ,
            "WM_HANDOFF_CACHE_DIR": str(cache_dir),
            "WM_CPR_BRIDGE_LEDGER": str(td_path / "ledger.jsonl"),
            "WM_CPR_HMAC_KEY": key,
            "ANTIGRAVITY_SIDECAR_WEB_PORT": "0",
        }
        sc_proc = subprocess.Popen([sys.executable, "-S", "-B", str(sidecar_py)], env=sc_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            disc = cache_dir / "sidecar.json"
            for _ in range(40):
                if disc.is_file():
                    break
                time.sleep(0.05)
            info = json.loads(disc.read_text(encoding="utf-8"))
            req = urllib.request.Request(
                f"{info['url']}/api/receipt",
                data=json.dumps(ptr_signed).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                sc_ok = resp.status == 201
            with urllib.request.urlopen(f"{info['url']}/api/status", timeout=2) as resp2:
                st_j = json.loads(resp2.read().decode("utf-8"))
            check("sidecar v4.7.0 accepts mandala-cpr-pointer/0.5 & reports version", sc_ok and st_j.get("version") == "4.7.0", str(st_j.get("version")))
        finally:
            sc_proc.send_signal(signal.SIGTERM)
            sc_proc.wait(timeout=4)

        # 14e) Isolated HOME install.sh --hosts codex,cursor preserves browser/whitemagic MCP
        fake_home = td_path / "fake_home"
        (fake_home / ".codex").mkdir(parents=True)
        (fake_home / ".cursor").mkdir(parents=True)
        (fake_home / ".codex" / "config.toml").write_text(
            '[plugins."unified-computer-use@openai-bundled"]\nenabled = true\n\n[mcp_servers.whitemagic]\ncommand = "/usr/bin/wm3"\nenabled = false\n',
            encoding="utf-8",
        )
        (fake_home / ".cursor" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"whitemagic": {"command": "/usr/bin/wm3", "args": ["serve"]}}}),
            encoding="utf-8",
        )
        inst_env = {**os.environ, "HOME": str(fake_home), "WM_SKIP_CARGO": "1"}
        r_inst = subprocess.run(
            ["bash", str(KIT / "install.sh"), "--hosts", "codex,cursor"],
            cwd=str(KIT),
            env=inst_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        cx_hooks = json.loads((fake_home / ".codex" / "hooks.json").read_text(encoding="utf-8")) if (fake_home / ".codex" / "hooks.json").is_file() else {}
        cx_toml = (fake_home / ".codex" / "config.toml").read_text(encoding="utf-8")
        cu_hooks = json.loads((fake_home / ".cursor" / "hooks.json").read_text(encoding="utf-8")) if (fake_home / ".cursor" / "hooks.json").is_file() else {}
        cu_mcp = json.loads((fake_home / ".cursor" / "mcp.json").read_text(encoding="utf-8")) if (fake_home / ".cursor" / "mcp.json").is_file() else {}
        check(
            "install.sh --hosts codex,cursor configures hooks+wm_fast+rules+agents while preserving existing entries",
            r_inst.returncode == 0
            and len(cx_hooks.get("hooks", {}).get("PreToolUse", [])) == 1
            and "[mcp_servers.wm_fast]" in cx_toml
            and '[plugins."unified-computer-use@openai-bundled"]\nenabled = true' in cx_toml
            and cu_hooks.get("version") == 1
            and all(ev in cu_hooks.get("hooks", {}) for ev in ("sessionStart", "beforeShellExecution", "beforeMCPExecution", "preToolUse", "postToolUse", "postToolUseFailure", "stop"))
            and (fake_home / ".cursor" / "rules" / "whitemagic-turbo.mdc").is_file()
            and (fake_home / ".cursor" / "agents" / "fresh-verifier.md").is_file()
            and (fake_home / ".cursor" / "agents" / "fast-builder.md").is_file()
            and (fake_home / ".cursor" / "agents" / "swarm-coordinator.md").is_file()
            and (fake_home / ".cursor" / "cli-config.json").is_file()
            and (fake_home / ".cursor" / "hooks" / "session_start_brief.py").is_file()
            and "whitemagic" in cu_mcp.get("mcpServers", {})
            and "wm_fast" in cu_mcp.get("mcpServers", {}),
            r_inst.stderr[-300:] if r_inst.returncode != 0 else "",
        )

        # 14f) Cursor Shell updated_input preserves sibling fields & delegates compound heredocs via --py
        rc_sib, cu_sib = run_sq(
            ["--pre-tool", "--host", "cursor", "--event", "preToolUse", "--py", str(KIT / "plugin" / "hooks" / "pre_tool_guard.py")],
            {
                "tool_name": "Shell",
                "tool_input": {"command": "git status", "working_directory": str(KIT), "block_until_ms": 15000},
                "conversation_id": "cur-sib-1",
            },
        )
        rc_hd, cu_hd = run_sq(
            ["--pre-tool", "--host", "cursor", "--event", "preToolUse", "--py", str(KIT / "plugin" / "hooks" / "pre_tool_guard.py")],
            {
                "tool_name": "Shell",
                "tool_input": {"command": "python3 - <<'PY'\nprint('ok')\nPY", "working_directory": str(KIT)},
                "conversation_id": "cur-hd-1",
            },
        )
        check(
            "wm-squeeze --host cursor preserves Shell sibling fields & wraps inline Python heredoc via --py",
            rc_sib == 0
            and "wm-squeeze --safe -- git status" in cu_sib.get("updated_input", {}).get("command", "")
            and cu_sib.get("updated_input", {}).get("working_directory") == str(KIT)
            and cu_sib.get("updated_input", {}).get("block_until_ms") == 15000
            and rc_hd == 0
            and "--pipe" in cu_hd.get("updated_input", {}).get("command", "")
            and cu_hd.get("updated_input", {}).get("working_directory") == str(KIT),
            f"sib={cu_sib} hd={cu_hd}",
        )


def test_c2_grimoire_profiles_mandala_exec_and_sidecar_telemetry():
    print("\n15. Phase C2 Per-Host Grimoire Profile, --mandala-exec & Sidecar Grimoire/wm_fast:")
    import signal
    import urllib.request

    reset_bin = LOCAL_BIN / "wm-reset"
    handoff_bin = LOCAL_BIN / "wm-handoff"
    bridge_bin = LOCAL_BIN / "wm-cpr-bridge"
    sidecar_py = KIT / "plugin" / "sidecars" / "wm-sidecar" / "server.py"

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        gp_path = td_path / "grimoire_profile.json"
        gp_path.write_text(
            json.dumps({
                "schema": "wm-grimoire-profile/1.1",
                "hardware_tier": "standard",
                "confinement": {"runner_profile": "bwrap-landlock-v1", "mandala_guard_mode": "advisory"},
                "model_tuning": {"resolved_family": "gemini4", "max_unbounded_read_lines": 350, "clamp_read_lines": 210, "wide_jsonl_lines": 40, "context_warn_tokens": 24000, "context_cliff_tokens": 48000, "enable_flip_guard": False, "turn0_brief_max_bytes": 1400},
                "host_profiles": {
                    "agy": {"resolved_family": "gemini4", "max_unbounded_read_lines": 220, "clamp_read_lines": 160, "wide_jsonl_lines": 45, "context_warn_tokens": 24000, "context_cliff_tokens": 48000, "enable_flip_guard": False, "turn0_brief_max_bytes": 1400},
                    "cursor": {"resolved_family": "claude4", "max_unbounded_read_lines": 180, "clamp_read_lines": 120, "wide_jsonl_lines": 35, "context_warn_tokens": 24000, "context_cliff_tokens": 48000, "enable_flip_guard": False, "turn0_brief_max_bytes": 1400},
                },
                "turbo_hosts": ["cursor", "codex", "agy"],
                "telemetry": {"recommendations": ["prefer compact slices"]},
            }),
            encoding="utf-8",
        )

        # 15a) Per-host Cursor read clamp (120L) vs AGY read clamp (160L)
        big_txt = td_path / "big_sample.py"
        big_txt.write_text("".join(f"x_{i} = {i}\n" for i in range(420)), encoding="utf-8")
        env_gp = {**os.environ, "WM_GRIMOIRE_PROFILE": str(gp_path)}

        r_cur = subprocess.run(
            [str(WM_SQUEEZE), "--pre-tool", "--host", "cursor", "--event", "preToolUse"],
            input=json.dumps({"tool_name": "Read", "tool_input": {"path": str(big_txt)}}),
            capture_output=True, text=True, env=env_gp, timeout=5,
        )
        cur_j = json.loads(r_cur.stdout) if r_cur.returncode == 0 else {}
        r_agy = subprocess.run(
            [str(WM_SQUEEZE), "--pre-tool", "--host", "agy"],
            input=json.dumps({"toolCall": {"name": "read_file", "args": {"Path": str(big_txt)}}}),
            capture_output=True, text=True, env=env_gp, timeout=5,
        )
        agy_j = json.loads(r_agy.stdout) if r_agy.returncode == 0 else {}
        check(
            "wm-squeeze honors per-host Grimoire 1.1 limits (Cursor=120L, AGY=160L)",
            cur_j.get("updated_input", {}).get("limit") == 120
            and agy_j.get("overwrite", {}).get("EndLine") == 160,
            f"cur={cur_j} agy={agy_j}",
        )

        # 15b) stop_telemetry fires early crystal at 50k ctx when local-small Grimoire thresholds (24k warn / 48k cliff) active
        tp_small = td_path / "small_tp.jsonl"
        tp_small.write_text(
            json.dumps({"type": "PLANNER_RESPONSE", "model": "qwen-local", "input_tokens": 50000, "cache_read_tokens": 0, "output_tokens": 200}) + "\n",
            encoding="utf-8",
        )
        cache_dir = td_path / "cache"
        cache_dir.mkdir()
        env_st = {
            **env_gp,
            "WM_AGY_TELEMETRY": str(td_path / "tel.jsonl"),
            "WM_HANDOFF_CACHE_DIR": str(cache_dir),
            "WM_HANDOFF_BIN": str(handoff_bin),
        }
        run_hook(STOP_HOOK, {"transcriptPath": str(tp_small), "conversationId": "conv_c2_small", "cwd": str(KIT), "fullyIdle": True, "terminationReason": "NO_TOOL_CALL"}, env_extra=env_st)
        check("stop_telemetry honors Grimoire tight warn/cliff thresholds (30k crystal)", (cache_dir / "latest_conv_c2_small.md").is_file())

        # 15c) wm-reset --launcher auto reads turbo_hosts + wm-handoff honors brief cap
        r_rst = subprocess.run([str(reset_bin), "--cwd", str(KIT), "--dry-run"], capture_output=True, text=True, env={**env_st, "WM_RESET_LAUNCHER": "auto"}, timeout=10)
        check(
            "wm-reset WM_RESET_LAUNCHER=auto selects turbo_hosts[0] from Grimoire",
            r_rst.returncode == 0 and json.loads(r_rst.stdout).get("launcher") == "cursor",
            r_rst.stdout or r_rst.stderr,
        )

        # 15d) Codex strict --mandala-guard hook rewrite + --mandala-exec -> sidecar + ledger
        r_mg_hook = subprocess.run(
            [str(WM_SQUEEZE), "--pre-tool", "--host", "codex", "--mandala-guard", "strict"],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo MANDALA_C2_OK"}}),
            capture_output=True, text=True, env=env_gp, timeout=5,
        )
        mg_hook_j = json.loads(r_mg_hook.stdout) if r_mg_hook.returncode == 0 else {}
        check(
            "wm-squeeze --mandala-guard strict rewrites shell command via --mandala-exec",
            "--mandala-exec" in mg_hook_j.get("hookSpecificOutput", {}).get("updatedInput", {}).get("command", ""),
            str(mg_hook_j),
        )

        fast_log = td_path / "wm_fast_stats.jsonl"
        fast_log.write_text('{"ts":1,"tool":"batch_search","out":128,"wall_ms":12,"err":false}\n', encoding="utf-8")
        sc_env = {
            **env_gp,
            "WM_HANDOFF_CACHE_DIR": str(cache_dir),
            "WM_CPR_BRIDGE_LEDGER": str(cache_dir / "cpr_bridge.jsonl"),
            "WM_FAST_STATS": str(fast_log),
            "WM_CPR_HMAC_KEY": "c2_mandala_hmac_secret",
            "WM_CPR_BRIDGE_BIN": str(bridge_bin),
            "WM_SANDBOX_BIN": "none",
            "ANTIGRAVITY_SIDECAR_WEB_PORT": "0",
        }
        sc_proc = subprocess.Popen([sys.executable, "-S", "-B", str(sidecar_py)], env=sc_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            disc = cache_dir / "sidecar.json"
            for _ in range(40):
                if disc.is_file():
                    break
                time.sleep(0.05)
            r_exec = subprocess.run(
                [str(WM_SQUEEZE), "--mandala-exec", "--cwd", str(td_path), "--", "echo", "C2_EXEC_OK"],
                capture_output=True, text=True, env=sc_env, timeout=10,
            )
            info = json.loads(disc.read_text(encoding="utf-8"))
            with urllib.request.urlopen(f"{info['url']}/api/status", timeout=2) as r_st:
                st_j = json.loads(r_st.read().decode("utf-8"))
            tail = st_j.get("cpr_ledger_tail") or []
            has_mandala_ptr = any(item.get("schema") == "mandala-cpr-pointer/0.5" and item.get("sig_status") == "verified" for item in tail)
            check(
                "wm-squeeze --mandala-exec emits verified HMAC mandala-cpr-pointer/0.5 -> sidecar /api/status (with grimoire+wm_fast)",
                r_exec.returncode == 0
                and "C2_EXEC_OK" in r_exec.stdout
                and has_mandala_ptr
                and st_j.get("grimoire", {}).get("present") is True
                and st_j.get("grimoire", {}).get("confinement_profile") == "bwrap-landlock-v1"
                and st_j.get("wm_fast", {}).get("calls") == 1,
                f"rc={r_exec.returncode} tail={tail} grimoire={st_j.get('grimoire')} fast={st_j.get('wm_fast')}",
            )
        finally:
            sc_proc.send_signal(signal.SIGTERM)
            sc_proc.wait(timeout=4)


def test_v470_ultra_efficiency_cockpit_neon_and_spool():
    print("\n16. v4.7.0 Live Cockpit, --breakdown, Neon 100k/120k Rollover & Crystal Spool:")
    import signal
    import urllib.request

    audit_bin = KIT / "bin" / "agy-token-audit"
    handoff_bin = KIT / "bin" / "wm-handoff"
    hud_py = _HOOKS_DIR / "longevity_hud.py"
    sidecar_py = KIT / "plugin" / "sidecars" / "wm-sidecar" / "server.py"

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        app_brain = td_path / "app_brain"
        coord_id = "11111111-2222-3333-4444-555555555555"
        worker_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        coord_tp = app_brain / coord_id / ".system_generated" / "logs" / "transcript.jsonl"
        worker_tp = app_brain / worker_id / ".system_generated" / "logs" / "transcript.jsonl"
        coord_tp.parent.mkdir(parents=True)
        worker_tp.parent.mkdir(parents=True)

        # Synthetic transcript exercising all 5 uncached buckets + swarm worker spawn
        coord_tp.write_text(
            "\n".join([
                json.dumps({"type": "USER_INPUT", "content": "Execute Ultra-Efficiency Wave v4.7.0"}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-xhigh", "input_tokens": 16000, "cache_read_tokens": 0, "output_tokens": 400, "created_at": "2026-11-26T20:00:00Z"}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-xhigh", "input_tokens": 4000, "cache_read_tokens": 86000, "output_tokens": 500, "created_at": "2026-11-26T20:00:10Z", "tool_calls": [{"name": "swarm", "args": {"action": "add", "name": "lane-1"}}]}),
                json.dumps({"type": "GENERIC", "content": json.dumps({"status": "added", "name": "lane-1", "conversation_id": worker_id})}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-hydrogen-xhigh", "input_tokens": 95000, "cache_read_tokens": 0, "output_tokens": 300, "created_at": "2026-11-26T20:00:20Z"}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-hydrogen-xhigh", "input_tokens": 80000, "cache_read_tokens": 15000, "output_tokens": 250, "created_at": "2026-11-26T20:04:00Z"}),
                json.dumps({"type": "CHECKPOINT", "content": "compacted"}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-xhigh", "input_tokens": 20000, "cache_read_tokens": 20000, "output_tokens": 200, "created_at": "2026-11-26T20:04:10Z"}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-xhigh", "input_tokens": 25000, "cache_read_tokens": 45000, "output_tokens": 200, "created_at": "2026-11-26T20:04:20Z"}),
                json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-xhigh", "input_tokens": 3000, "cache_read_tokens": 102000, "output_tokens": 200, "created_at": "2026-11-26T20:04:30Z"}),
            ]) + "\n",
            encoding="utf-8",
        )
        worker_tp.write_text(
            json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-hydrogen-high", "input_tokens": 13500, "cache_read_tokens": 32000, "output_tokens": 350, "created_at": "2026-11-26T20:04:25Z"}) + "\n",
            encoding="utf-8",
        )
        os.utime(worker_tp, (time.time() - 5, time.time() - 5))

        # 16a) agy-token-audit --breakdown --session --json exact accounting across all 5 buckets
        env_brain = {**os.environ, "WM_AGY_APP_BRAIN": str(app_brain), "WM_AGY_IDE_BRAIN": str(td_path / "none1"), "WM_AGY_CLI_BRAIN": str(td_path / "none2")}
        r_bd = subprocess.run([sys.executable, "-S", str(audit_bin), "--breakdown", "--session", coord_id[:8], "--json"], capture_output=True, text=True, env=env_brain, timeout=5)
        bd_j = json.loads(r_bd.stdout) if r_bd.returncode == 0 else {}
        bd_map = bd_j.get("breakdown") or {}
        tot_unc = (bd_j.get("aggregate") or {}).get("tot_unc", -1)
        tot_turns = (bd_j.get("aggregate") or {}).get("turns", -1)
        sum_b_tok = sum(int(v.get("tokens") or 0) for v in bd_map.values())
        sum_b_trn = sum(int(v.get("turns") or 0) for v in bd_map.values())
        check(
            "agy-token-audit --breakdown exact 5-bucket accounting",
            r_bd.returncode == 0
            and sum_b_tok == tot_unc
            and sum_b_trn == tot_turns
            and all(bd_map.get(k, {}).get("tokens", 0) > 0 for k in ("flips", "ttl_expiries", "warm_work", "compactions_partial", "turn_0")),
            f"tot={tot_unc} sum={sum_b_tok} bd={bd_map}",
        )
        r_bd_txt = subprocess.run([sys.executable, "-S", str(audit_bin), "--breakdown", "--session", coord_id[:8]], capture_output=True, text=True, env=env_brain, timeout=5)
        check(
            "agy-token-audit --breakdown table lists all 5 bucket labels",
            r_bd_txt.returncode == 0 and all(lbl in r_bd_txt.stdout for lbl in ("Flips", "TTL Expiries", "Warm Work", "Partial/Compactions", "Turn-0")),
        )

        # 16b) Neon 100k warn / 120k rollover parity (Python longevity_hud.py == Rust wm-squeeze --hud)
        neon_tp = td_path / ".gemini" / "antigravity" / "brain" / "neon-hud" / ".system_generated" / "logs" / "transcript.jsonl"
        neon_tp.parent.mkdir(parents=True)
        neon_state_py = td_path / "hud_py"
        neon_state_rs = td_path / "hud_rs"
        neon_tp.write_text(json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-xhigh", "input_tokens": 5000, "cache_read_tokens": 90000, "step_index": 1}) + "\n", encoding="utf-8")
        _, out_py_95, _, _ = run_hook(hud_py, {"conversationId": "neon-1", "transcriptPath": str(neon_tp), "invocationNum": 1}, env_extra={"WM_HOOK_STATE_DIR": str(neon_state_py)})
        p_rs_95 = subprocess.run([str(WM_SQUEEZE), "--hud"], input=json.dumps({"conversationId": "neon-1", "transcriptPath": str(neon_tp), "invocationNum": 1}), capture_output=True, text=True, env={**os.environ, "WM_HOOK_STATE_DIR": str(neon_state_rs)}, timeout=5)
        check("Neon HUD 95k -> silent {} (Py & Rust)", json.loads(out_py_95) == {} and json.loads(p_rs_95.stdout) == {})

        neon_tp.write_text(neon_tp.read_text(encoding="utf-8") + json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-xhigh", "input_tokens": 5000, "cache_read_tokens": 100000, "step_index": 2}) + "\n", encoding="utf-8")
        _, out_py_105, _, _ = run_hook(hud_py, {"conversationId": "neon-1", "transcriptPath": str(neon_tp), "invocationNum": 2}, env_extra={"WM_HOOK_STATE_DIR": str(neon_state_py)})
        p_rs_105 = subprocess.run([str(WM_SQUEEZE), "--hud"], input=json.dumps({"conversationId": "neon-1", "transcriptPath": str(neon_tp), "invocationNum": 2}), capture_output=True, text=True, env={**os.environ, "WM_HOOK_STATE_DIR": str(neon_state_rs)}, timeout=5)
        um_py_105 = json.loads(out_py_105).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", "")
        um_rs_105 = json.loads(p_rs_105.stdout).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", "")
        check("Neon HUD 105k -> Neon Context Watch (exact Py/Rust parity)", "Neon Context Watch" in um_py_105 and um_py_105 == um_rs_105, f"py={um_py_105!r} rs={um_rs_105!r}")

        neon_tp.write_text(neon_tp.read_text(encoding="utf-8") + json.dumps({"type": "PLANNER_RESPONSE", "model": "gemini-4-neon-xhigh", "input_tokens": 5000, "cache_read_tokens": 120000, "step_index": 3}) + "\n", encoding="utf-8")
        _, out_py_125, _, _ = run_hook(hud_py, {"conversationId": "neon-1", "transcriptPath": str(neon_tp), "invocationNum": 3}, env_extra={"WM_HOOK_STATE_DIR": str(neon_state_py)})
        p_rs_125 = subprocess.run([str(WM_SQUEEZE), "--hud"], input=json.dumps({"conversationId": "neon-1", "transcriptPath": str(neon_tp), "invocationNum": 3}), capture_output=True, text=True, env={**os.environ, "WM_HOOK_STATE_DIR": str(neon_state_rs)}, timeout=5)
        um_py_125 = json.loads(out_py_125).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", "")
        um_rs_125 = json.loads(p_rs_125.stdout).get("injectSteps", [{}])[0].get("systemMessage", {}).get("metadata", {}).get("user_markdown", "")
        check("Neon HUD 125k -> Neon Rollover Target (exact Py/Rust parity)", "Neon Rollover Target" in um_py_125 and um_py_125 == um_rs_125, f"py={um_py_125!r} rs={um_rs_125!r}")

        # 16c) stop_telemetry Neon 125k auto-crystal + pending_crystals.jsonl spool + --flush-wm3 dedup/fail-preserve
        spool_file = td_path / "pending_crystals.jsonl"
        cache_dir = td_path / "cache"
        cache_dir.mkdir()
        tel_file = td_path / "tel.jsonl"
        env_sp = {
            **env_brain,
            "WM_AGY_TELEMETRY": str(tel_file),
            "WM_HANDOFF_CACHE_DIR": str(cache_dir),
            "WM_HANDOFF_BIN": str(handoff_bin),
            "WM_PENDING_CRYSTALS": str(spool_file),
            "WM_GRIMOIRE_PROFILE": str(td_path / "no_grimoire.json"),
        }
        run_hook(STOP_HOOK, {"transcriptPath": str(neon_tp), "conversationId": "neon-stop-1", "cwd": str(KIT), "fullyIdle": True, "terminationReason": "NO_TOOL_CALL"}, env_extra=env_sp)
        tel_rec = json.loads(tel_file.read_text(encoding="utf-8").splitlines()[-1]) if tel_file.is_file() else {}
        spool_rows = [json.loads(l) for l in spool_file.read_text(encoding="utf-8").splitlines() if l.strip()] if spool_file.is_file() else []
        check(
            "stop_telemetry Neon 125k marks rollover, auto-crystallizes & spools to pending_crystals.jsonl",
            tel_rec.get("neon_rollover_recommended") is True
            and (cache_dir / "latest_neon-stop-1.md").is_file()
            and len(spool_rows) == 1
            and spool_rows[0].get("schema") == "wm-crystal-spool/1.0"
            and spool_rows[0].get("source") == "stop_telemetry",
            f"tel={tel_rec} spool_n={len(spool_rows)}",
        )

        # Mock wm3 binary supporting both remember & recall --inspect-promise strict-zero-write
        mock_store = td_path / "mock_gen3"
        mock_store.mkdir()
        wm3_log = td_path / "wm3_calls.jsonl"
        mock_wm3 = td_path / "mock_wm3.py"
        mock_wm3.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\nfrom pathlib import Path\n"
            "args = sys.argv[1:]\n"
            f"Path({str(wm3_log)!r}).open('a').write(json.dumps(args) + '\\n')\n"
            "if 'serve' in args:\n    sys.exit(1)\n"
            "if os.environ.get('MOCK_WM3_FAIL') == '1' and 'remember' in args:\n    sys.stderr.write('LMDB busy\\n')\n    sys.exit(1)\n"
            "if 'recall' in args:\n    print(json.dumps({'results': [{'id': 'rec-42', 'score': 0.99, 'source': 'gen3_mock', 'content': 'Ultra-Efficiency Wave cockpit note'}]}))\n    sys.exit(0)\n"
            "print('OK')\n",
            encoding="utf-8",
        )
        mock_wm3.chmod(0o755)
        env_wm3 = {**env_sp, "WM3_BIN": str(mock_wm3), "WM3_STORE": str(mock_store), "WM3_RECALL_MODE": "cli"}

        r_dry = subprocess.run([sys.executable, "-S", str(handoff_bin), "--flush-wm3", "--dry-run", "--json"], capture_output=True, text=True, env=env_wm3, timeout=5)
        dry_j = json.loads(r_dry.stdout) if r_dry.returncode == 0 else {}
        check("wm-handoff --flush-wm3 --dry-run previews pending without consuming spool", dry_j.get("to_flush") == 1 and spool_file.stat().st_size > 0)

        r_fl1 = subprocess.run([sys.executable, "-S", str(handoff_bin), "--flush-wm3", "--json"], capture_output=True, text=True, env=env_wm3, timeout=5)
        fl1_j = json.loads(r_fl1.stdout) if r_fl1.returncode == 0 else {}
        check("wm-handoff --flush-wm3 ingests spooled crystal & drains spool", r_fl1.returncode == 0 and fl1_j.get("flushed") == 1 and fl1_j.get("remaining") == 0)

        # Re-spool same crystal -> idempotent dedup skips duplicate
        subprocess.run([sys.executable, "-S", str(handoff_bin), "--transcript", str(neon_tp), "--cwd", str(KIT), "--out", str(cache_dir / "dup.md"), "--spool"], capture_output=True, text=True, env=env_wm3, timeout=5)
        r_fl2 = subprocess.run([sys.executable, "-S", str(handoff_bin), "--flush-wm3", "--json"], capture_output=True, text=True, env=env_wm3, timeout=5)
        fl2_j = json.loads(r_fl2.stdout) if r_fl2.returncode == 0 else {}
        check("wm-handoff --flush-wm3 deduplicates already-flushed sha256", r_fl2.returncode == 0 and fl2_j.get("flushed") == 0 and fl2_j.get("skipped_dup") == 1)

        # Failing wm3 write preserves crystal in pending_crystals.jsonl
        subprocess.run([sys.executable, "-S", str(handoff_bin), "--transcript", str(coord_tp), "--cwd", str(KIT), "--out", str(cache_dir / "keep.md"), "--spool"], capture_output=True, text=True, env=env_wm3, timeout=5)
        r_fl_fail = subprocess.run([sys.executable, "-S", str(handoff_bin), "--flush-wm3", "--json"], capture_output=True, text=True, env={**env_wm3, "MOCK_WM3_FAIL": "1"}, timeout=5)
        flf_j = json.loads(r_fl_fail.stdout) if r_fl_fail.stdout.strip().startswith("{") else {}
        check("wm-handoff --flush-wm3 preserves failed entries on LMDB error", r_fl_fail.returncode == 1 and flf_j.get("failed") == 1 and flf_j.get("remaining") == 1)

        # 16d) Live wm-sidecar v4.7.0 cockpit (/api/live + 4 one-click loopback POST actions)
        cfg_dir = td_path / "agy_cfg"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(json.dumps({"plugins": {"whitemagic-agy-kit": {"enabled": True}, "chrome-devtools-plugin": {"enabled": False}}}), encoding="utf-8")
        (cfg_dir / "mcp_config.json").write_text(
            json.dumps({"mcpServers": {"whitemagic": {"args": ["serve", "--readonly"], "tools": {"memory_recall": {"eager": True}, "session_checkpoint": {"eager": True}}}}}),
            encoding="utf-8",
        )
        sc_env = {**env_wm3, "ANTIGRAVITY_SIDECAR_WEB_PORT": "0", "WM_AGY_CONFIG_DIR": str(cfg_dir)}
        sc_proc = subprocess.Popen([sys.executable, "-S", "-B", str(sidecar_py)], env=sc_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            disc = cache_dir / "sidecar.json"
            for _ in range(50):
                if disc.is_file():
                    break
                time.sleep(0.05)
            info = json.loads(disc.read_text(encoding="utf-8"))
            url = info["url"]

            with urllib.request.urlopen(f"{url}/api/live", timeout=3) as r:
                lv = json.loads(r.read())
            act = lv.get("active") or {}
            worker_names = {w.get("name") for w in (lv.get("workers") or [])}
            check(
                "sidecar GET /api/live reports Neon ctx zone, TTL state, 5-bucket breakdown & swarm worker",
                lv.get("ok") is True
                and act.get("is_neon") is True
                and act.get("ctx_zone") == "amber"
                and act.get("ttl_state") in ("warm", "cooling", "cold")
                and set((act.get("breakdown") or {}).keys()) == {"flips", "ttl_expiries", "warm_work", "compactions_partial", "turn_0"}
                and "lane-1" in worker_names,
                f"act={act.get('ctx_zone')}/{act.get('last_ctx')} workers={worker_names}",
            )

            req_ho = urllib.request.Request(f"{url}/api/actions/handoff", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req_ho, timeout=6) as r:
                ho_j = json.loads(r.read())
            check(
                "sidecar POST /api/actions/handoff spools crystal & returns <=150-tok pointer",
                ho_j.get("ok") is True and ho_j.get("spooled") is True and 0 < len((ho_j.get("pointer_prompt") or "").encode("utf-8")) <= 600,
            )

            req_t0 = urllib.request.Request(f"{url}/api/actions/audit-turn0", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req_t0, timeout=5) as r:
                t0_j = json.loads(r.read())
            check(
                "sidecar POST /api/actions/audit-turn0 flags readonly eager conflict",
                t0_j.get("ok") is True and "whitemagic/session_checkpoint" in (t0_j.get("readonly_eager_conflicts") or []),
            )

            req_rc = urllib.request.Request(f"{url}/api/actions/recall", data=json.dumps({"query": "Ultra-Efficiency Wave", "limit": 5}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req_rc, timeout=6) as r:
                rc_j = json.loads(r.read())
            sources = {x.get("source") for x in (rc_j.get("results") or [])}
            check(
                "sidecar POST /api/actions/recall returns zero-LLM read-only gen3 + local spool hits",
                rc_j.get("ok") is True and rc_j.get("zero_llm_tokens") is True and "gen3_mock" in sources and any(str(s).startswith("spool:") for s in sources),
                f"sources={sources}",
            )

            req_fl = urllib.request.Request(f"{url}/api/actions/flush-spool", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req_fl, timeout=6) as r:
                fls_j = json.loads(r.read())
            check(
                "sidecar POST /api/actions/flush-spool drains pending spool to 0",
                fls_j.get("ok") is True and (fls_j.get("spool") or {}).get("pending_count") == 0,
            )
        finally:
            sc_proc.send_signal(signal.SIGTERM)
            sc_proc.wait(timeout=4)

    # 16e) install.sh preserves lean Turn-0 eager defaults (no session_checkpoint/firecrawl_scrape eager)
    inst_txt = (KIT / "install.sh").read_text(encoding="utf-8")
    check(
        "install.sh keeps session_checkpoint & firecrawl_scrape lazy (Turn-0 trim)",
        'tools.pop("session_checkpoint"' in inst_txt and 'fc_tools.pop("firecrawl_scrape"' in inst_txt,
    )


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
        test_v441_spill_cap_and_rust_hud()
        test_v442_self_heal_and_rust_post_tool()
        test_v450_rust_pretool_crystal_and_swarm_guards()
        test_v451_sidecar_cpr_bridge_and_plugin_scoping()
        test_v460_multihost_codex_cursor_and_cpr_translate()
        test_c2_grimoire_profiles_mandala_exec_and_sidecar_telemetry()
        test_v470_ultra_efficiency_cockpit_neon_and_spool()
    print(f"\n=== Wave 3/4/v4.7.0 Test Summary: {PASS} passed, {FAIL} failed ===")
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
