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

emit({{"type": "thinking", "subtype": "delta", "text": "analyzing", "session_id": sid}})
if "RECORD_ENV" in prompt:
    reply = json.dumps({{"cwd": os.getcwd(), "args": args, "prompt": prompt}})
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
