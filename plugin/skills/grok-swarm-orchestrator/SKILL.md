---
name: grok-swarm-orchestrator
description: Dispatch 1-6 parallel Grok 4.7 (500K) workers via `grok_dispatch` / `wm-grok-swarm` inside the 4-Tier Sovereign Compute Pyramid (Local Tricameral -> Grok/Hydrogen -> Neon), with isolated git worktrees and zero wm3 store contention.
---

# Grok 4.7 Swarm Orchestrator (`grok_dispatch` / `wm-grok-swarm`)

## 4-Tier Sovereign Compute Pyramid
1. **Tier 0 — Local Tricameral (`$0`, `<175ms`):** `tricameral_dispatch` (`route`, `recall_rank`, `sim_audit`, `kekkai`, `squeeze`, `handoff`) + `wm-squeeze` + `code_graph`.
2. **Tier 1 — Grok 4.7 Leaf Swarms (`grok_dispatch`):** Best for **parallel read-only audits (`ask`)**, **architecture second opinions (`plan`)**, and **disjoint isolated git-worktree edits (`worktree`)**. Never shares local `wm3 serve` Gen3 SQLite store.
3. **Tier 2 — Gemini 4 Hydrogen:** Warm AGY tool loops (`94.7%` cache hit) and `/swarm` or `run_workflow` domain leads that verify/merge Grok worktrees.
4. **Tier 3 — Gemini 4 Neon XHigh:** Apex planning, security synthesis, and final integration.

## Eager MCP Usage (`grok_dispatch`)
- **Read-only parallel audit:** `grok_dispatch(action="ask", model="high", concurrency=3, tasks=[{name, cwd, prompt}, ...])`
- **Isolated parallel edits:** `grok_dispatch(action="worktree", model="high", tasks=[{name, cwd, worktree_name, prompt}, ...])` -> review diffs under `~/.cursor/worktrees/<repo>/<name>` before merging.
- **Confined `bwrap` verification (`gate-lite`):** Pass `confined=true` and optional `verify_cmd` (top-level or per-task) to execute post-edit verification inside `bwrap --ro-bind / / --bind <wt> <wt> --unshare-net` and emit a `confinement_receipt` (`bwrap_bin`, `network_isolated`, `worktree_path`, `worktree_diff_sha256`, `verify_exit_code`, `scope_confined`). Fails closed if `bwrap` is unavailable.
- **Tip:** Inline key file snippets (<15 KB) or exact `file:Lstart-Lend` targets into each worker's prompt so `ask` workers finish in 15–45s instead of spending turns searching.
- **Hybrid `run_workflow` script:** See [grok_hybrid_swarm.py](./scripts/grok_hybrid_swarm.py).
