"""
Hybrid Sovereign Pyramid Template for `run_workflow`:
Runs Tier-1 parallel Grok 4.7 (`wm-grok-swarm`) audits/drafts alongside Tier-2 Hydrogen verification.
Pass Args: {"cwd": "/path/to/repo", "lanes": [{"name": "...", "prompt": "..."}]}
"""
import asyncio
import json
import subprocess


async def main():
    cwd = args.get("cwd") or (WORKSPACES[0] if WORKSPACES else ".")
    lanes = args.get("lanes") or []
    if not lanes:
        log("No lanes provided in Args.")
        return

    phase("Tier 1: Parallel Grok 4.7 Swarm")
    payload = {
        "action": args.get("action", "ask"),
        "model": args.get("model", "high"),
        "concurrency": min(6, len(lanes)),
        "timeout_sec": int(args.get("timeout_sec", 120)),
        "tasks": [{"cwd": cwd, **lane} for lane in lanes[:6]],
    }
    proc = await asyncio.to_thread(
        subprocess.run,
        ["wm-grok-swarm", "run", "--json-stdin"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    log(f"wm-grok-swarm exit={proc.returncode}")
    grok_out = proc.stdout.strip() or proc.stderr.strip()

    phase("Tier 2: Hydrogen Synthesis & Verification")
    summary = await agent(
        f"Synthesize and verify these Grok 4.7 worker outputs in {cwd}. Keep only verified facts/diffs:\n\n{grok_out[:7000]}",
        schema={"type": "string"},
        model="flash",
        role="Hydrogen Verifier",
    )
    log(summary)


await main()
