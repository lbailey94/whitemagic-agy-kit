#!/usr/bin/env python3
"""Offline unit & integration tests for wm-grok-fleet (using a stub cursor-agent binary)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FLEET_BIN = REPO_ROOT / "bin" / "wm-grok-fleet"


def _load_fleet_mod():
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader = importlib.machinery.SourceFileLoader("wm_grok_fleet_testmod", str(FLEET_BIN))
        spec = importlib.util.spec_from_loader("wm_grok_fleet_testmod", loader)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.dont_write_bytecode = prev


def _make_stub_cursor(stub_path: Path, state_dir: Path) -> Path:
    script = f'''#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path({str(state_dir)!r})
STATE_DIR.mkdir(parents=True, exist_ok=True)

args = sys.argv[1:]
if args == ["--version"]:
    print("2026.11.25-stub")
    sys.exit(0)
if args == ["status"]:
    print("✓ Logged in as stub@example.com")
    sys.exit(0)
if args == ["models"]:
    print("Available models\\n\\ngrok-4.7-medium - Grok 4.7 Medium\\ngrok-4.7-high - Grok 4.7 High\\ncomposer-2.5 - Composer 2.5\\n")
    sys.exit(0)
model = "grok-4.7-medium"
wt_name = None
i = 0
while i < len(args):
    if args[i] == "--model" and i + 1 < len(args):
        model = args[i + 1]
        i += 2
    elif args[i] == "-w" and i + 1 < len(args):
        wt_name = args[i + 1]
        i += 2
    else:
        i += 1
prompt = args[-1] if args else ""

def emit(obj):
    print(json.dumps(obj), flush=True)

sid = f"stub-sid-{{abs(hash(prompt)) % 100000}}"
emit({{"type": "system", "subtype": "init", "session_id": sid, "model": model}})

if "STALL_HANG" in prompt:
    time.sleep(10)
    sys.exit(0)

if "WALL_HANG" in prompt:
    time.sleep(3)
    sys.exit(0)

if "STREAM_GRACE" in prompt:
    emit({{"type": "thinking", "subtype": "delta", "text": "planning", "session_id": sid}})
    time.sleep(0.5)
    emit({{"type": "thinking", "subtype": "completed", "session_id": sid}})
    emit({{"type": "assistant", "message": {{"role": "assistant", "content": [{{"type": "text", "text": "partial "}}]}}, "session_id": sid}})
    time.sleep(0.9)
    emit({{"type": "assistant", "message": {{"role": "assistant", "content": [{{"type": "text", "text": "completed"}}]}}, "session_id": sid}})
    emit({{
        "type": "result",
        "subtype": "success",
        "duration_ms": 1400,
        "duration_api_ms": 1350,
        "is_error": False,
        "result": "partial completed",
        "session_id": sid,
        "usage": {{"inputTokens": 500, "cacheReadTokens": 100, "cacheWriteTokens": 0, "outputTokens": 200, "reasoningTokens": 120}},
    }})
    sys.exit(0)

if "SLOW_HIGH_ONLY" in prompt and model == "grok-4.7-high":
    time.sleep(3)
    sys.exit(0)

if "FAIL_ON_MEDIUM" in prompt and model == "grok-4.7-medium":
    time.sleep(3)
    sys.exit(0)

if "TOOL_BURST" in prompt:
    emit({{"type": "tool_call", "subtype": "started", "session_id": sid}})
    emit({{"type": "tool_call", "subtype": "completed", "session_id": sid}})
    emit({{"type": "tool_call", "subtype": "started", "session_id": sid}})

if "FLAKY_429" in prompt:
    marker = STATE_DIR / "flaky_429.seen"
    if not marker.exists():
        marker.write_text("1", encoding="utf-8")
        sys.stderr.write("HTTP 429 rate limit exceeded\\n")
        sys.stderr.flush()
        sys.exit(1)

if "QUOTA_HIT" in prompt:
    emit({{
        "type": "result",
        "subtype": "error",
        "duration_ms": 50,
        "duration_api_ms": 40,
        "is_error": True,
        "result": "Cursor usage limit reached: quota exceeded",
        "session_id": sid,
        "usage": {{"inputTokens": 200, "cacheReadTokens": 0, "cacheWriteTokens": 0, "outputTokens": 10, "reasoningTokens": 0}},
    }})
    sys.exit(1)

if "WORKTREE_EDIT" in prompt and wt_name:
    repo_root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    wt_dir = Path.home() / ".cursor" / "worktrees" / repo_root.name / wt_name
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    if not wt_dir.exists():
        subprocess.check_call(["git", "-C", str(repo_root), "worktree", "add", "--detach", str(wt_dir), "HEAD"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    target = wt_dir / "calc.py"
    target.write_text("def add(a, b):\\n    return a + b\\n", encoding="utf-8")
    (wt_dir / "new_helper.py").write_text("def ok():\\n    return True\\n", encoding="utf-8")
    emit({{"type": "tool_call", "subtype": "started", "session_id": sid}})
    emit({{"type": "tool_call", "subtype": "completed", "session_id": sid}})

if "RACE_BENCH" in prompt and wt_name:
    repo_root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    wt_dir = Path.home() / ".cursor" / "worktrees" / repo_root.name / wt_name
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    if not wt_dir.exists():
        subprocess.check_call(["git", "-C", str(repo_root), "worktree", "add", "--detach", str(wt_dir), "HEAD"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if "CANDIDATE 1/" in prompt:
        (wt_dir / "speed.py").write_text("SCORE_MS = 99.0\\ndef solve(x):\\n    return x - 1\\n", encoding="utf-8")
    elif "CANDIDATE 2/" in prompt:
        (wt_dir / "speed.py").write_text("SCORE_MS = 12.4\\ndef solve(x):\\n    return x * 2\\n", encoding="utf-8")
    else:
        (wt_dir / "speed.py").write_text("SCORE_MS = 3.1\\ndef solve(x):\\n    return x * 2\\n", encoding="utf-8")
    emit({{"type": "tool_call", "subtype": "started", "session_id": sid}})
    emit({{"type": "tool_call", "subtype": "completed", "session_id": sid}})

emit({{"type": "thinking", "subtype": "delta", "text": "analyzing", "session_id": sid}})
if "RECORD_ENV" in prompt:
    reply = json.dumps({{"cwd": os.getcwd(), "args": args, "prompt": prompt}})
elif "PIPE_SCREEN" in prompt:
    import re as _re
    ids = _re.findall(r"=== ITEM ([^ ]+) ===", prompt)
    out_arr = [{{"id": iid, "score": 0.9 if iid.endswith(("1", "2")) else 0.2, "summary": f"finding-{{iid}}"}} for iid in ids]
    reply = json.dumps(out_arr)
elif "BIG_REDUCE_CHUNK" in prompt:
    reply = "FINDING_BLOCK: " + ("x" * 1800)
elif "INTERMEDIATE MERGE STEP" in prompt or "FINAL_REDUCE_SYNTH" in prompt:
    reply = json.dumps({{"reduced": True, "parts_seen": prompt.count("=== PART "), "model": model}})
elif "blind consensus judge" in prompt:
    reply = json.dumps({{"consensus": True, "panelists_seen": prompt.count("=== PANELIST "), "judge_model": model}})
elif "RAW_MD_OUT" in prompt:
    reply = "# Raw Blueprint Title\\n\\nExact unwrapped markdown body."
else:
    reply = json.dumps({{"ok": True, "echo": prompt[:40], "model": model}})
emit({{"type": "assistant", "message": {{"role": "assistant", "content": [{{"type": "text", "text": reply}}]}}, "session_id": sid}})
out_toks = 100000 if "BIG_USAGE" in prompt else 45
emit({{
    "type": "result",
    "subtype": "success",
    "duration_ms": 120,
    "duration_api_ms": 110,
    "is_error": False,
    "result": reply,
    "session_id": sid,
    "usage": {{
        "inputTokens": 1200,
        "cacheReadTokens": 800,
        "cacheWriteTokens": 0,
        "outputTokens": out_toks,
        "reasoningTokens": 15,
    }},
}})
'''
    stub_path.write_text(script, encoding="utf-8")
    stub_path.chmod(0o755)
    return stub_path


def test_model_resolver_defaults_non_fast_and_grok48_compat(monkeypatch):
    mod = _load_fleet_mod()
    monkeypatch.delenv("WM_GROK_FAMILY", raising=False)
    assert mod.resolve_fleet_model("low") == "grok-4.7-low"
    assert mod.resolve_fleet_model("medium") == "grok-4.7-medium"
    assert mod.resolve_fleet_model("high") == "grok-4.7-high"
    assert mod.resolve_fleet_model("xhigh") == "grok-4.7-xhigh"
    assert mod.resolve_fleet_model("medium-fast") == "grok-4.7-medium-fast"
    assert mod.resolve_fleet_model("grok-4.7-high-fast") == "grok-4.7-high-fast"

    monkeypatch.setenv("WM_GROK_FAMILY", "grok-4.8")
    assert mod.resolve_fleet_model("high") == "grok-4.8-high"
    assert mod.resolve_fleet_model("xhigh-fast") == "grok-4.8-xhigh-fast"
    assert mod.resolve_fleet_model("grok-4.8-medium") == "grok-4.8-medium"

    with pytest.raises(ValueError):
        mod.resolve_fleet_model("gpt-5")


def test_enqueue_run_and_crash_safe_resume(tmp_path, monkeypatch):
    mod = _load_fleet_mod()
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    manifest = tmp_path / "jobs.jsonl"
    jobs = [
        {"id": "j1", "name": "ok1", "stage": "s1", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "Task one"},
        {"id": "j2", "name": "ok2", "stage": "s1", "action": "plan", "model": "high", "cwd": str(tmp_path), "prompt": "Task two"},
        {"id": "j3", "name": "flaky", "stage": "s1", "action": "ask", "model": "low", "cwd": str(tmp_path), "prompt": "FLAKY_429 task"},
        {"id": "j4", "name": "ok4", "stage": "s1", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "Task four"},
    ]
    manifest.write_text("\n".join(json.dumps(j) for j in jobs) + "\n", encoding="utf-8")

    # Speed up backoff in test via monkeypatch if invoked in-process; via CLI patch max_attempts=1 first to test resume
    # Enqueue with max_attempts=1 on j3 first so it fails once, then test idempotent re-enqueue + reset/resume
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    p_enq = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_resume", "--manifest", str(manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        capture_output=True, text=True, env=env, check=True,
    )
    enq_json = json.loads(p_enq.stdout)
    assert enq_json["inserted"] == 4

    # Re-enqueueing is idempotent
    p_enq2 = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_resume", "--manifest", str(manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        capture_output=True, text=True, env=env, check=True,
    )
    assert json.loads(p_enq2.stdout)["skipped_existing"] == 4

    # Mark j1 done before run to prove resume doesn't re-run already done jobs, and shorten BackoffCoordinator in-process
    store = mod.FleetStore(db_path)
    # Run via in-process main with fast BackoffCoordinator
    orig_backoff = mod.BackoffCoordinator

    class FastBackoff(orig_backoff):
        def __init__(self):
            super().__init__(base_sec=0.05, cap_sec=0.1)

    monkeypatch.setattr(mod, "BackoffCoordinator", FastBackoff)
    monkeypatch.setenv("WM_CURSOR_AGENT_BIN", str(stub))
    monkeypatch.setattr(
        sys,
        "argv",
        [str(FLEET_BIN), "run", "--campaign", "c_resume", "--read-concurrency", "4", "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
    )
    rc = mod.main()
    assert rc == 0

    exported = store.export_campaign("c_resume")
    assert exported["summary"]["counts"]["done"] == 4
    by_id = {j["id"]: j for j in exported["jobs"]}
    assert by_id["j3"]["attempts"] == 2
    assert by_id["j1"]["attempts"] == 1
    assert Path(by_id["j1"]["output_path"]).is_file()
    assert Path(by_id["j1"]["stream_log_path"]).is_file()
    assert by_id["j1"]["input_tokens"] == 1200
    assert by_id["j1"]["cache_read_tokens"] == 800


def test_stream_stall_watchdog_kills_hung_worker(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    manifest = tmp_path / "stall.jsonl"
    manifest.write_text(
        json.dumps({"id": "stall1", "name": "stall", "stage": "s", "action": "ask", "model": "low", "cwd": str(tmp_path), "prompt": "STALL_HANG"}) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_stall", "--manifest", str(manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    t0 = time.monotonic()
    p_run = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "run", "--campaign", "c_stall", "--stall-timeout", "2", "--wall-timeout", "15", "--max-retries", "0", "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        env=env, capture_output=True, text=True,
    )
    elapsed = time.monotonic() - t0
    assert p_run.returncode == 1
    assert elapsed < 7.0
    summary = json.loads(p_run.stdout)
    assert summary["counts"].get("failed") == 1
    mod = _load_fleet_mod()
    exp = mod.FleetStore(db_path).export_campaign("c_stall")
    job = exp["jobs"][0]
    assert "stall timeout" in (job["error"] or "")
    stream_txt = Path(job["stream_log_path"]).read_text(encoding="utf-8")
    assert '"subtype": "init"' in stream_txt


def test_worktree_harvest_bwrap_verify_and_auto_prune(tmp_path):
    repo = tmp_path / "wt_test_repo_uniq"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True, capture_output=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "calc.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    manifest = tmp_path / "wt.jsonl"
    manifest.write_text(
        json.dumps({
            "id": "wt1",
            "name": "wt_fix",
            "stage": "wt",
            "action": "worktree",
            "model": "medium",
            "cwd": str(repo),
            "prompt": "WORKTREE_EDIT fix calc.py",
            "confined": True,
            "verify_cmd": "python3 -c 'import calc, new_helper; assert calc.add(2, 3) == 5 and new_helper.ok(); print(123)'",
        }) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_wt", "--manifest", str(manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    p_run = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "run", "--campaign", "c_wt", "--write-concurrency", "1", "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        env=env, capture_output=True, text=True,
    )
    assert p_run.returncode == 0, p_run.stderr + "\n" + p_run.stdout

    mod = _load_fleet_mod()
    job = mod.FleetStore(db_path).export_campaign("c_wt")["jobs"][0]
    assert job["status"] == "done"
    assert job["verify_exit_code"] == 0
    assert job["tool_calls"] == 1

    patch_text = Path(job["patch_path"]).read_text(encoding="utf-8")
    assert "return a + b" in patch_text
    assert "new_helper.py" in patch_text

    receipt = json.loads(Path(job["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["scope_confined"] is True
    assert receipt["network_isolated"] is True
    assert receipt["verify_exit_code"] == 0

    # Must auto-prune worktree immediately!
    leaked_dir = Path.home() / ".cursor" / "worktrees" / repo.name
    assert not leaked_dir.exists() or not any(leaked_dir.iterdir())


def test_host_health_governor_blocks_low_headroom(tmp_path):
    mod = _load_fleet_mod()
    ok_mem = "MemTotal:       16000000 kB\nMemAvailable:    4096000 kB\n"
    low_mem = "MemTotal:       16000000 kB\nMemAvailable:     512000 kB\n"

    healthy, snap = mod.check_host_health(
        min_free_ram_mb=1500.0,
        min_free_root_gb=8.0,
        min_free_artifact_gb=1.5,
        artifact_dir=tmp_path,
        meminfo_text=ok_mem,
        root_free_bytes=int(15 * (1024 ** 3)),
        artifact_free_bytes=int(10 * (1024 ** 3)),
    )
    assert healthy is True
    assert snap["reasons"] == []

    healthy_low_ram, snap_ram = mod.check_host_health(
        min_free_ram_mb=1500.0,
        min_free_root_gb=8.0,
        min_free_artifact_gb=1.5,
        artifact_dir=tmp_path,
        meminfo_text=low_mem,
        root_free_bytes=int(15 * (1024 ** 3)),
        artifact_free_bytes=int(10 * (1024 ** 3)),
    )
    assert healthy_low_ram is False
    assert any("MemAvailable" in r for r in snap_ram["reasons"])

    healthy_low_disk, snap_disk = mod.check_host_health(
        min_free_ram_mb=1500.0,
        min_free_root_gb=8.0,
        min_free_artifact_gb=1.5,
        artifact_dir=tmp_path,
        meminfo_text=ok_mem,
        root_free_bytes=int(4 * (1024 ** 3)),
        artifact_free_bytes=int(10 * (1024 ** 3)),
    )
    assert healthy_low_disk is False
    assert any("root / free" in r for r in snap_disk["reasons"])


def test_prompt_hardening_prefix_exclusions_and_record_win(tmp_path):
    mod = _load_fleet_mod()
    # Backward compatibility: no hardening blocks returns base_prompt verbatim
    assert mod.compose_worker_prompt("Inspect auth.ts") == "Inspect auth.ts"

    # Hardening puts static blocks BEFORE [TASK] to preserve prefix caching
    composed = mod.compose_worker_prompt(
        "Inspect auth.ts",
        intended_design_exclusions=["Defense-in-depth existsBy check in auth.service.ts"],
        forbidden_actions=["Do not modify .env files"],
        winning_trajectories=[
            {
                "target": "mcp-ts-sdk",
                "vuln_class": "oauth-issuer-confusion",
                "summary": "discoverOAuthMetadata omits RFC 8414 issuer check",
                "steps": ["read auth.ts:1850", "trace callers in index.ts"],
            }
        ],
    )
    assert composed.index("[INTENDED_DESIGN_EXCLUSIONS]") < composed.index("[FORBIDDEN_ACTIONS]")
    assert composed.index("[FORBIDDEN_ACTIONS]") < composed.index("[WINNING_TRAJECTORY_EXEMPLARS]")
    assert composed.index("[WINNING_TRAJECTORY_EXEMPLARS]") < composed.index("[TASK]")
    assert composed.endswith("[TASK]\nInspect auth.ts")

    # record-win appends JSONL exemplar, and enqueue composes prompts cleanly without touching source prompt_path
    traj_file = tmp_path / "wins.jsonl"
    rec_proc = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "record-win",
            "--campaign", "c_hard",
            "--target", "edge-runtime",
            "--vuln-class", "ssrf",
            "--summary", "Confirmed unguarded fetch redirect",
            "--step", "grep fetch calls",
            "--step", "verify zero Next.js callers",
            "--out", str(traj_file),
        ],
        check=True, capture_output=True, text=True,
    )
    rec_out = json.loads(rec_proc.stdout)
    assert rec_out["ok"] is True
    assert traj_file.is_file()

    excl_file = tmp_path / "exclusions.md"
    excl_file.write_text("# Exclusions\n- Intentional localhost dev proxy\n", encoding="utf-8")

    raw_prompt = tmp_path / "raw_task.txt"
    raw_prompt.write_text("Audit target handler", encoding="utf-8")

    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    manifest = tmp_path / "jobs.jsonl"
    manifest.write_text(
        json.dumps({"id": "h1", "name": "scout", "stage": "s1", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt_path": str(raw_prompt)}) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "enqueue",
            "--campaign", "c_hard",
            "--manifest", str(manifest),
            "--db-path", str(db_path),
            "--artifact-dir", str(art_dir),
            "--exclusions-file", str(excl_file),
            "--forbidden", "Never run destructive git commands",
            "--trajectories-file", str(traj_file),
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    # Original prompt_path must stay unmodified; composed copy lives under artifacts
    assert raw_prompt.read_text(encoding="utf-8") == "Audit target handler"
    job = mod.FleetStore(db_path).export_campaign("c_hard")["jobs"][0]
    composed_text = Path(job["prompt_path"]).read_text(encoding="utf-8")
    assert "Intentional localhost dev proxy" in composed_text
    assert "Never run destructive git commands" in composed_text
    assert "Confirmed unguarded fetch redirect" in composed_text
    assert "[TASK]\nAudit target handler" in composed_text

    run_proc = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "run", "--campaign", "c_hard", "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    assert json.loads(run_proc.stdout)["counts"]["done"] == 1


def test_empty_cwd_no_tools_guard_and_max_tool_calls_kills_worker(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    manifest = tmp_path / "tools.jsonl"
    jobs = [
        {"id": "rec1", "name": "rec1", "stage": "s1", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "RECORD_ENV test"},
        {"id": "tb1", "name": "tb1", "stage": "s1", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "TOOL_BURST test"},
    ]
    manifest.write_text("\n".join(json.dumps(j) for j in jobs) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_tools", "--manifest", str(manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    p_run = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "run", "--campaign", "c_tools",
            "--read-concurrency", "2", "--max-retries", "1",
            "--empty-cwd", "--no-tools-guard", "--max-tool-calls", "0",
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ],
        env=env, capture_output=True, text=True,
    )
    assert p_run.returncode == 1
    summary = json.loads(p_run.stdout)
    assert summary["counts"]["done"] == 1
    assert summary["counts"]["failed"] == 1

    mod = _load_fleet_mod()
    by_id = {j["id"]: j for j in mod.FleetStore(db_path).export_campaign("c_tools")["jobs"]}
    rec_out = json.loads(Path(by_id["rec1"]["output_path"]).read_text(encoding="utf-8"))
    expected_empty = str(art_dir / "_empty_cwd")
    assert rec_out["cwd"] == expected_empty
    assert "--workspace" in rec_out["args"] and expected_empty in rec_out["args"]
    assert rec_out["prompt"].startswith("PURE SYNTHESIS MODE:")

    assert by_id["tb1"]["attempts"] == 1  # tool budget exceeded is NOT retryable!
    assert "tool budget exceeded" in (by_id["tb1"]["error"] or "")


def test_max_usd_budget_cap_and_quota_pct(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    manifest = tmp_path / "budget.jsonl"
    jobs = [
        {"id": f"b{i}", "name": f"b{i}", "stage": "s1", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": f"BIG_USAGE {i}", "priority": 100 - i}
        for i in range(4)
    ]
    manifest.write_text("\n".join(json.dumps(j) for j in jobs) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_budget", "--manifest", str(manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    # 1 job with 100k outputTokens = $0.60256 > $0.50 cap, so concurrency=1 halts after job 1
    p_run = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "run", "--campaign", "c_budget",
            "--read-concurrency", "1", "--max-usd", "0.50",
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ],
        env=env, capture_output=True, text=True,
    )
    assert p_run.returncode == 0
    summary = json.loads(p_run.stdout)
    assert summary["budget_capped"] is True
    assert summary["counts"]["done"] == 1
    assert summary["counts"]["pending"] == 3
    assert summary["est_cursor_quota_pct"] == round(summary["est_standard_usd"] / _load_fleet_mod().CURSOR_USD_PER_1PCT, 2)


def test_quota_exhausted_pauses_cleanly_and_writes_checkpoints_for_resume(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    ckpt_json = tmp_path / "fleet_checkpoint.json"
    ckpt_jsonl = tmp_path / "completed_jobs.jsonl"
    manifest = tmp_path / "quota.jsonl"
    jobs = [
        {"id": "q1", "name": "q1", "stage": "s1", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "Q1 ok", "priority": 100},
        {"id": "q2", "name": "q2", "stage": "s1", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "QUOTA_HIT q2", "priority": 90},
        {"id": "q3", "name": "q3", "stage": "s1", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "Q3 ok", "priority": 80},
    ]
    manifest.write_text("\n".join(json.dumps(j) for j in jobs) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_quota", "--manifest", str(manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    run_cmd = [
        sys.executable, "-B", str(FLEET_BIN), "run", "--campaign", "c_quota",
        "--read-concurrency", "1", "--max-retries", "2",
        "--checkpoint-path", str(ckpt_json), "--checkpoint-jsonl", str(ckpt_jsonl),
        "--db-path", str(db_path), "--artifact-dir", str(art_dir),
    ]
    p1 = subprocess.run(run_cmd, env=env, capture_output=True, text=True)
    assert p1.returncode == 0
    s1 = json.loads(p1.stdout)
    assert s1["quota_halted"] is True
    assert s1["counts"].get("done") == 1
    assert s1["counts"].get("pending") == 2
    assert s1["resumable_pending"] == 2
    assert ckpt_json.is_file() and ckpt_jsonl.is_file()
    lines1 = [json.loads(l) for l in ckpt_jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [x["job_id"] for x in lines1] == ["q1"]
    snap1 = json.loads(ckpt_json.read_text(encoding="utf-8"))
    by_id1 = {j["id"]: j for j in snap1["jobs"]}
    assert by_id1["q1"]["status"] == "done"
    assert by_id1["q2"]["status"] == "pending"
    assert by_id1["q3"]["status"] == "pending"

    Path(by_id1["q2"]["prompt_path"]).write_text("Q2 resumed cleanly", encoding="utf-8")
    p2 = subprocess.run(run_cmd, env=env, capture_output=True, text=True)
    assert p2.returncode == 0
    s2 = json.loads(p2.stdout)
    assert s2["quota_halted"] is False
    assert s2["counts"].get("done") == 3
    assert s2["counts"].get("pending", 0) == 0
    lines2 = [json.loads(l) for l in ckpt_jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines2) == 3
    mod = _load_fleet_mod()
    by_id2 = {j["id"]: j for j in mod.FleetStore(db_path).export_campaign("c_quota")["jobs"]}
    assert by_id2["q1"]["attempts"] == 1
    assert by_id2["q2"]["error"] is None
    assert by_id2["q3"]["attempts"] == 1


def test_wall_timeout_non_retryable_by_default(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    manifest = tmp_path / "wall.jsonl"
    manifest.write_text(
        json.dumps({"id": "w1", "name": "w1", "stage": "s", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "WALL_HANG"}) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_wall", "--manifest", str(manifest), "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    p_run = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "run", "--campaign", "c_wall",
            "--wall-timeout", "1", "--stall-timeout", "10", "--max-retries", "2",
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ],
        env=env, capture_output=True, text=True,
    )
    assert p_run.returncode == 1
    mod = _load_fleet_mod()
    j = mod.FleetStore(db_path).export_campaign("c_wall")["jobs"][0]
    assert j["attempts"] == 1
    assert "wall timeout" in (j["error"] or "")


def test_doctor_campaigns_inspect_and_report(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)

    p_doc = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "doctor", "--json", "--db-path", str(db_path), "--artifact-dir", str(art_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    doc = json.loads(p_doc.stdout)
    assert doc["ok"] is True
    assert doc["auth_ok"] is True
    assert doc["grok_models_count"] == 2
    assert doc["other_models_count"] == 1
    assert "synthesis" in doc["presets"]

    # Zero-manifest ask with synthesis preset
    out_json = tmp_path / "ask_out.json"
    out_md = tmp_path / "ask_out.md"
    p_ask = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "ask",
            "--campaign", "c_ask_test",
            "--preset", "synthesis",
            "--parse-json",
            "--out", str(out_json),
            "--report-md", str(out_md),
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
            "RECORD_ENV prompt one",
            "RECORD_ENV prompt two",
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    ask_res = json.loads(p_ask.stdout)
    assert ask_res["ok"] is True
    assert ask_res["summary"]["counts"]["done"] == 2
    assert out_json.is_file() and out_md.is_file()
    full_out = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(full_out["results"]) == 2
    for r in full_out["results"]:
        # Verify synthesis preset enforced empty_cwd + NO_TOOLS_BANNER + non-fast medium
        assert r["model"] == "grok-4.7-medium"
        assert r["parsed"]["cwd"].endswith("_empty_cwd")
        assert r["parsed"]["prompt"].startswith("PURE SYNTHESIS MODE:")

    # campaigns / inspect / report
    p_ls = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "campaigns", "--json", "--db-path", str(db_path)],
        check=True, env=env, capture_output=True, text=True,
    )
    ls_data = json.loads(p_ls.stdout)
    assert len(ls_data["campaigns"]) == 1
    assert ls_data["campaigns"][0]["id"] == "c_ask_test"

    p_ins = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "inspect", "--campaign", "c_ask_test", "--show-output", "--json", "--db-path", str(db_path)],
        check=True, env=env, capture_output=True, text=True,
    )
    ins_data = json.loads(p_ins.stdout)
    assert len(ins_data["jobs"]) == 2
    assert "output_preview" in ins_data["jobs"][0]

    rep_path = tmp_path / "rep.json"
    subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "report", "--campaign", "c_ask_test", "--format", "json", "--parse-json", "--out", str(rep_path), "--db-path", str(db_path)],
        check=True, env=env, capture_output=True, text=True,
    )
    rep_data = json.loads(rep_path.read_text(encoding="utf-8"))
    assert rep_data["ok"] is True and len(rep_data["results"]) == 2


def test_map_batching_dry_run_files_and_jsonl(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)

    src = tmp_path / "src"
    src.mkdir()
    for i in range(5):
        (src / f"mod_{i}.py").write_text(f"def fn_{i}():\n    return {i}\n", encoding="utf-8")

    # Dry-run over 5 files packed N=2 -> 3 workers, zero DB created
    p_dry = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "map",
            "--preset", "synthesis",
            "--cwd", str(tmp_path),
            "--files", "src/*.py",
            "--batch-size", "2",
            "--instruction", "Audit each file",
            "--dry-run",
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    dry = json.loads(p_dry.stdout)
    assert dry["dry_run"] is True
    assert dry["estimate"]["workers"] == 3
    assert dry["estimate"]["model"] == "grok-4.7-medium"
    assert not db_path.exists()

    # Live map over JSONL items packed N=4 -> 2 workers for 6 items
    jsonl_p = tmp_path / "items.jsonl"
    jsonl_p.write_text(
        "\n".join(json.dumps({"cid": f"c-{i}", "text": f"Conversation {i}"}) for i in range(6)) + "\n",
        encoding="utf-8",
    )
    out_p = tmp_path / "map_out.json"
    md_p = tmp_path / "map_out.md"
    p_map = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "map",
            "--campaign", "c_map_jsonl",
            "--preset", "synthesis",
            "--items-jsonl", str(jsonl_p),
            "--id-field", "cid",
            "--batch-size", "4",
            "--instruction", "RECORD_ENV summarize",
            "--parse-json",
            "--out", str(out_p),
            "--report-md", str(md_p),
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    m_res = json.loads(p_map.stdout)
    assert m_res["ok"] is True
    assert m_res["summary"]["counts"]["done"] == 2
    assert m_res["summary"]["total_tool_calls"] == 0
    full = json.loads(out_p.read_text(encoding="utf-8"))
    # Batch 1 has 4 items (c-0..c-3), Batch 2 has 2 items (c-4..c-5)
    p0 = full["results"][0]["parsed"]["prompt"]
    p1 = full["results"][1]["parsed"]["prompt"]
    assert "=== ITEM c-0 ===" in p0 and "=== ITEM c-3 ===" in p0
    assert "=== ITEM c-4 ===" in p1 and "=== ITEM c-5 ===" in p1
    assert md_p.is_file() and "c_map_jsonl" in md_p.read_text(encoding="utf-8")


def test_allow_any_model_and_host_health_unknown_ram(tmp_path, monkeypatch):
    mod = _load_fleet_mod()
    with pytest.raises(ValueError):
        mod.resolve_fleet_model("composer-2.5")
    assert mod.resolve_fleet_model("composer-2.5", allow_any_model=True) == "composer-2.5"
    assert mod.resolve_fleet_model("gpt-5.3-codex-high", allow_any_model=True) == "gpt-5.3-codex-high"
    with pytest.raises(ValueError):
        mod.resolve_fleet_model("bad model; rm -rf /", allow_any_model=True)

    # Simulate non-Linux host where read_mem_available_mb returns None -> does not falsely block
    monkeypatch.setattr(mod, "read_mem_available_mb", lambda _t=None: None)
    healthy, snap = mod.check_host_health(
        min_free_ram_mb=1500.0,
        min_free_root_gb=8.0,
        min_free_artifact_gb=1.5,
        artifact_dir=tmp_path,
        root_free_bytes=int(20 * (1024 ** 3)),
        artifact_free_bytes=int(20 * (1024 ** 3)),
    )
    assert healthy is True
    assert snap["mem_available_mb"] is None


def test_patches_check_and_apply_roundtrip(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    repo = tmp_path / "patch_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)

    p_run = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "ask",
            "--campaign", "c_patch_apply",
            "--preset", "patch",
            "--cwd", str(repo),
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
            "WORKTREE_EDIT fix calc",
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    run_out = json.loads(p_run.stdout)
    assert run_out["ok"] is True
    jid = run_out["results"][0]["id"]

    # List patches
    p_list = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "patches", "--campaign", "c_patch_apply", "--json", "--db-path", str(db_path)],
        check=True, env=env, capture_output=True, text=True,
    )
    plist = json.loads(p_list.stdout)
    assert len(plist["patches"]) == 1 and plist["patches"][0]["patch_bytes"] > 0

    # Dry-run --check leaves repo untouched
    p_chk = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "patches", "--campaign", "c_patch_apply", "--apply", jid, "--repo", str(repo), "--check", "--db-path", str(db_path)],
        check=True, env=env, capture_output=True, text=True,
    )
    assert json.loads(p_chk.stdout)["dry_run"] is True
    assert "return a - b" in (repo / "calc.py").read_text(encoding="utf-8")

    # Actual apply updates calc.py and creates new_helper.py
    p_app = subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "patches", "--campaign", "c_patch_apply", "--apply", jid, "--repo", str(repo), "--db-path", str(db_path)],
        check=True, env=env, capture_output=True, text=True,
    )
    assert json.loads(p_app.stdout)["applied"] is True
    assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")
    assert (repo / "new_helper.py").is_file()


def test_swarm_and_mcp_v2_presets_and_non_fast_defaults():
    swarm_bin = REPO_ROOT / "bin" / "wm-grok-swarm"
    mcp_bin = REPO_ROOT / "bin" / "wm-grok-mcp"
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader = importlib.machinery.SourceFileLoader("wm_grok_swarm_v2_mod", str(swarm_bin))
        spec = importlib.util.spec_from_loader("wm_grok_swarm_v2_mod", loader)
        assert spec is not None and spec.loader is not None
        smod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smod)
    finally:
        sys.dont_write_bytecode = prev

    assert smod.resolve_model("high") == "grok-4.7-high"
    assert smod.resolve_model("medium") == "grok-4.7-medium"
    assert smod.resolve_model("high-fast") == "grok-4.7-high-fast"
    assert smod.resolve_model("composer-2.5", allow_any_model=True) == "composer-2.5"
    with pytest.raises(ValueError, match="Unsupported Grok 4.7 model"):
        smod.resolve_model("claude-opus")

    p_mcp = subprocess.run(
        [sys.executable, "-B", str(mcp_bin)],
        input='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n',
        capture_output=True, text=True, check=True,
    )
    schema = json.loads(p_mcp.stdout.strip())["result"]["tools"][0]["inputSchema"]
    props = schema["properties"]
    assert {"preset", "empty_cwd", "no_tools_guard", "allow_any_model"}.issubset(props.keys())
    task_props = props["tasks"]["items"]["properties"]
    assert {"empty_cwd", "no_tools_guard"}.issubset(task_props.keys())


def test_map_tree_reduce_end_to_end(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)

    items_p = tmp_path / "items.jsonl"
    items_p.write_text(
        "\n".join(json.dumps({"id": f"doc-{i}", "text": f"content {i}"}) for i in range(6)) + "\n",
        encoding="utf-8",
    )
    out_p = tmp_path / "mr.json"
    md_p = tmp_path / "mr.md"
    red_p = tmp_path / "reduced.txt"

    # 6 items at batch_size=1 -> 6 map outputs of ~1800 chars (~10.8KB) > 4000 chunk cap -> forces >=2 reduce levels!
    p = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "map",
            "--campaign", "c_tree_reduce",
            "--preset", "synthesis",
            "--items-jsonl", str(items_p),
            "--batch-size", "1",
            "--instruction", "BIG_REDUCE_CHUNK analyze",
            "--reduce-instruction", "FINAL_REDUCE_SYNTH merge all",
            "--reduce-max-chunk-chars", "4000",
            "--reduce-out", str(red_p),
            "--parse-json",
            "--out", str(out_p),
            "--report-md", str(md_p),
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    res = json.loads(p.stdout)
    assert res["ok"] is True
    assert res["reduced"]["ok"] is True
    assert res["reduced"]["levels"] >= 2
    assert red_p.is_file()
    reduced_json = json.loads(red_p.read_text(encoding="utf-8"))
    assert reduced_json["reduced"] is True and reduced_json["parts_seen"] >= 2
    md_text = md_p.read_text(encoding="utf-8")
    assert "## Executive Synthesis" in md_text


def test_race_tournament_ranking_score_regex_and_apply_winner(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    repo = tmp_path / "race_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "speed.py").write_text("SCORE_MS = 50.0\ndef solve(x):\n    return x\n", encoding="utf-8")
    (repo / "verify.py").write_text(
        "import speed\nassert speed.solve(7) == 14\nprint(f'median={speed.SCORE_MS}ms')\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    out_p = tmp_path / "race.json"
    md_p = tmp_path / "race.md"
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)

    p = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "race",
            "--campaign", "c_race_test",
            "--cwd", str(repo),
            "--n", "3",
            "--verify-cmd", "python3 verify.py",
            "--score-regex", r"median=([0-9.]+)ms",
            "--score-goal", "min",
            "--apply-winner",
            "--prompt", "RACE_BENCH optimize solve",
            "--out", str(out_p),
            "--report-md", str(md_p),
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    data = json.loads(p.stdout)
    assert data["ok"] is True
    assert data["applied_winner"] is True
    assert data["ranking"]["passed_count"] == 2
    assert data["winner"]["name"] == "cand-03"
    assert data["winner"]["score"] == 3.1
    assert data["ranking"]["leaderboard"][1]["score"] == 12.4
    assert data["ranking"]["leaderboard"][2]["passed"] is False
    # Winner patch applied cleanly to repo
    assert "SCORE_MS = 3.1" in (repo / "speed.py").read_text(encoding="utf-8")
    assert md_p.is_file() and "cand-03" in md_p.read_text(encoding="utf-8")


def test_council_blind_multi_model_and_judge(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    target_f = tmp_path / "auth.py"
    target_f.write_text("def check(tok):\n    return tok == 'secret'\n", encoding="utf-8")
    out_p = tmp_path / "council.json"
    md_p = tmp_path / "council.md"
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)

    p = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "council",
            "--campaign", "c_council_test",
            "--cwd", str(tmp_path),
            "--files", "auth.py",
            "--models", "grok-4.7-high,composer-2.5,claude-sonnet-5-thinking-high",
            "--judge-model", "grok-4.7-high",
            "--prompt", "Review auth.py for timing attacks",
            "--parse-json",
            "--out", str(out_p),
            "--report-md", str(md_p),
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    res = json.loads(p.stdout)
    assert res["ok"] is True
    assert res["panel_done"] == 3
    assert res["judge_ok"] is True
    full = json.loads(out_p.read_text(encoding="utf-8"))
    assert {r["model"] for r in full["panelists"]} == {
        "grok-4.7-high",
        "composer-2.5",
        "claude-sonnet-5-thinking-high",
    }
    assert full["judge"]["parsed"]["consensus"] is True
    assert full["judge"]["parsed"]["panelists_seen"] == 3
    assert "## Executive Synthesis" in md_p.read_text(encoding="utf-8")


def test_pipeline_three_stage_map_filter_audit(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    db_path = tmp_path / "fleet.db"
    art_dir = tmp_path / "artifacts"
    seeds = tmp_path / "seeds.jsonl"
    seeds.write_text(
        "\n".join(json.dumps({"id": f"s-{i}", "body": f"file {i}"}) for i in (1, 2, 3, 4)) + "\n",
        encoding="utf-8",
    )
    spec = {
        "id": "pipe-test",
        "cwd": str(tmp_path),
        "stages": [
            {
                "name": "screen",
                "type": "map",
                "preset": "synthesis",
                "batch_size": 2,
                "items_jsonl": str(seeds),
                "parse_json": True,
                "instruction": "PIPE_SCREEN evaluate items",
            },
            {
                "name": "gate",
                "type": "filter",
                "where": {"field": "score", "gte": 0.75},
            },
            {
                "name": "deep",
                "type": "map",
                "preset": "synthesis",
                "from_prev": True,
                "batch_size": 1,
                "prompt_template": "Deep verify {id}: {summary}",
                "instruction": "Verify candidate",
            },
        ],
    }
    spec_p = tmp_path / "spec.json"
    spec_p.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)

    p = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "pipeline", str(spec_p),
            "--db-path", str(db_path), "--artifact-dir", str(art_dir),
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    res = json.loads(p.stdout)
    assert res["ok"] is True
    assert len(res["stages"]) == 3
    assert res["stages"][0]["items_emitted"] == 4
    assert res["stages"][1]["in_items"] == 4 and res["stages"][1]["kept_items"] == 2
    assert res["stages"][2]["items_emitted"] == 2


def test_streaming_grace_protects_active_output_from_wall_kill(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    state_dir = tmp_path / "state"
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)

    p = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "ask",
            "--preset", "blueprint", "--campaign", "c_grace",
            "--wall-timeout", "1", "--streaming-grace", "4", "--stall-timeout", "10",
            "--state-dir", str(state_dir),
            "STREAM_GRACE finish writing after thinking",
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    out = json.loads(p.stdout)
    assert out["ok"] is True
    assert out["results"][0]["output"] == "partial completed"
    assert "[grace]" in p.stderr


def test_presets_dataset_and_blueprint_parity_across_cli_swarm_mcp():
    mod = _load_fleet_mod()
    assert "dataset" in mod.FLEET_PRESETS and mod.FLEET_PRESETS["dataset"]["model"] == "low"
    assert mod.FLEET_PRESETS["dataset"]["read_concurrency"] == 12
    assert "blueprint" in mod.FLEET_PRESETS and mod.FLEET_PRESETS["blueprint"]["model"] == "high-fast"
    assert mod.FLEET_PRESETS["blueprint"]["wall_timeout"] == 480
    assert mod.FLEET_PRESETS["blueprint"]["streaming_grace"] == 90

    swarm_bin = REPO_ROOT / "bin" / "wm-grok-swarm"
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader = importlib.machinery.SourceFileLoader("wm_grok_swarm_v22_mod", str(swarm_bin))
        spec = importlib.util.spec_from_loader("wm_grok_swarm_v22_mod", loader)
        assert spec is not None and spec.loader is not None
        smod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smod)
    finally:
        sys.dont_write_bytecode = prev
    assert {"dataset", "blueprint"}.issubset(smod.SWARM_PRESETS.keys())

    mcp_bin = REPO_ROOT / "bin" / "wm-grok-mcp"
    p_mcp = subprocess.run(
        [sys.executable, "-B", str(mcp_bin)],
        input='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n',
        capture_output=True, text=True, check=True,
    )
    preset_enum = json.loads(p_mcp.stdout.strip())["result"]["tools"][0]["inputSchema"]["properties"]["preset"]["enum"]
    assert {"dataset", "blueprint"}.issubset(set(preset_enum))


def test_timeout_fallback_model_auto_escalation(tmp_path):
    mod = _load_fleet_mod()
    assert mod.step_down_timeout_model("grok-4.7-high", "auto") == "grok-4.7-high-fast"
    assert mod.step_down_timeout_model("grok-4.7-medium", "auto") == "grok-4.7-medium-fast"
    assert mod.step_down_timeout_model("grok-4.7-low", "auto") == "grok-4.7-low-fast"
    assert mod.step_down_timeout_model("grok-4.7-low-fast", "auto") is None
    assert mod.step_down_timeout_model("grok-4.7-medium", "low") == "grok-4.7-low"

    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    state_dir = tmp_path / "state"
    manifest = tmp_path / "m.jsonl"
    manifest.write_text(
        json.dumps({"id": "fb1", "name": "fb1", "stage": "s", "action": "ask", "model": "high", "cwd": str(tmp_path), "prompt": "SLOW_HIGH_ONLY spec"}) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_fb", "--manifest", str(manifest), "--state-dir", str(state_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    p_run = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "run", "--campaign", "c_fb",
            "--wall-timeout", "1", "--streaming-grace", "0", "--stall-timeout", "10",
            "--timeout-fallback-model", "auto", "--max-retries", "2",
            "--state-dir", str(state_dir),
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    res = json.loads(p_run.stdout)
    assert res["ok"] is True
    assert "[fallback]" in p_run.stderr
    j = mod.FleetStore(state_dir / "fleet.db").export_campaign("c_fb")["jobs"][0]
    assert j["status"] == "done"
    assert j["attempts"] == 2
    assert j["resolved_model"] == "grok-4.7-high-fast"


def test_retry_subcommand_and_run_model_override(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    state_dir = tmp_path / "state"
    manifest = tmp_path / "m.jsonl"
    manifest.write_text(
        "\n".join([
            json.dumps({"id": "j1", "name": "j1", "stage": "s", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "FAIL_ON_MEDIUM batch 1"}),
            json.dumps({"id": "j2", "name": "j2", "stage": "s", "action": "ask", "model": "medium", "cwd": str(tmp_path), "prompt": "ok batch 2"}),
        ]) + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)
    subprocess.run(
        [sys.executable, "-B", str(FLEET_BIN), "enqueue", "--campaign", "c_ret", "--manifest", str(manifest), "--state-dir", str(state_dir)],
        check=True, env=env, capture_output=True, text=True,
    )
    p1 = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "run", "--campaign", "c_ret",
            "--wall-timeout", "1", "--streaming-grace", "0", "--stall-timeout", "10",
            "--state-dir", str(state_dir),
        ],
        env=env, capture_output=True, text=True,
    )
    assert p1.returncode == 1
    s1 = json.loads(p1.stdout)
    assert s1["counts"].get("done") == 1 and s1["counts"].get("failed") == 1
    assert any("cursor-fleet retry" in h for h in s1.get("hints", []))

    p2 = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "retry", "--campaign", "c_ret",
            "--model", "low", "--run", "--preset", "dataset",
            "--state-dir", str(state_dir),
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    s2 = json.loads(p2.stdout)
    assert s2["ok"] is True
    assert s2["counts"].get("done") == 2
    mod = _load_fleet_mod()
    by_id = {j["id"]: j for j in mod.FleetStore(state_dir / "fleet.db").export_campaign("c_ret")["jobs"]}
    assert by_id["j1"]["resolved_model"] == "grok-4.7-low"
    assert by_id["j2"]["resolved_model"] == "grok-4.7-medium"


def test_extract_json_payload_repairs_python_escapes_and_trailing_commas():
    mod = _load_fleet_mod()
    # Invalid JSON escapes (\U0001f60a, \d+, \s, \') plus trailing comma inside array
    tricky = r'''[{"id": "t1", "code": "assert f('a\U0001f60ab') == 1 and re.match(r'\d+\s+', '12 ')", "quote": "it\'s ok",},]'''
    parsed = mod.extract_json_payload(tricky)
    assert isinstance(parsed, list) and len(parsed) == 1
    assert parsed[0]["id"] == "t1"
    assert r"\U0001f60a" in parsed[0]["code"]
    assert r"\d+\s+" in parsed[0]["code"]

    # Valid JSON with \n, \t, \u00e9 remains 100% untouched on pass 1
    valid = '{"ok": true, "msg": "caf\\u00e9\\nline2\\tok"}'
    parsed_v = mod.extract_json_payload(valid)
    assert parsed_v == {"ok": True, "msg": "caf\u00e9\nline2\tok"}

    # Real Grok 4.7 BPE quirks: transposed `]"},`, omitted comma `}{`, missing final `]`
    quirks = '[{"id": "a", "tests": ["assert x == 1]"}, {"id": "b", "tests": ["assert y == 2"]}{"id": "c", "tests": ["assert z == 3"]}'
    parsed_q = mod.extract_json_payload(quirks)
    assert isinstance(parsed_q, list) and [x["id"] for x in parsed_q] == ["a", "b", "c"]


def test_ask_text_out_and_state_dir_shorthand(tmp_path):
    stub = _make_stub_cursor(tmp_path / "stub_cursor", tmp_path / "stub_state")
    state_dir = tmp_path / "isolated_fleet"
    md_out = tmp_path / "SPEC.md"
    env = os.environ.copy()
    env["WM_CURSOR_AGENT_BIN"] = str(stub)

    p = subprocess.run(
        [
            sys.executable, "-B", str(FLEET_BIN), "ask",
            "--preset", "dataset", "--campaign", "c_txt",
            "--state-dir", str(state_dir),
            "--text-out", str(md_out),
            "RAW_MD_OUT generate spec",
        ],
        check=True, env=env, capture_output=True, text=True,
    )
    meta = json.loads(p.stdout)
    assert meta["ok"] is True
    assert meta["text_out"] == str(md_out)
    assert md_out.read_text(encoding="utf-8") == "# Raw Blueprint Title\n\nExact unwrapped markdown body.\n"
    assert (state_dir / "fleet.db").is_file()
    assert (state_dir / "artifacts").is_dir()
    assert (state_dir / "c_txt_checkpoint.json").is_file()
    assert "reasoning_pct" in meta["summary"]["tokens"]
    assert meta["summary"]["tokens"]["reasoning_pct"] > 0.0
