---
name: grok-swarm-orchestrator
description: Dispatch 1-6 parallel Grok 4.7 (500K) workers via `grok_dispatch` / `wm-grok-swarm` or unlimited batch fleets via `wm-grok-fleet` (`cursor-fleet`) inside the 4-Tier Sovereign Compute Pyramid (Local Tricameral -> Grok/Hydrogen -> Neon), with zero-tool synthesis presets, isolated git worktrees, and zero wm3 store contention.
---

# Grok 4.7+ Fleet & Swarm Orchestrator (`cursor-fleet` / `wm-grok-fleet` / `grok_dispatch`)

## 4-Tier Sovereign Compute Pyramid
1. **Tier 0 — Local Tricameral (`$0`, `<175ms`):** `tricameral_dispatch` (`route`, `recall_rank`, `sim_audit`, `kekkai`, `squeeze`, `handoff`) + `wm-squeeze` + `code_graph`.
2. **Tier 1 — Grok 4.7+ Leaf Swarms (`grok_dispatch` & `wm-grok-fleet`):** Best for **zero-tool map-reduce synthesis (`--preset synthesis`, 18.8× cheaper)**, **parallel read-only audits (`--preset audit`)**, **architecture second opinions (`--preset plan`)**, and **disjoint isolated git-worktree edits (`--preset patch`)**. Never shares local `wm3 serve` Gen3 SQLite store.
3. **Tier 2 — Gemini 4 Hydrogen:** Warm AGY tool loops (`94.7%` cache hit) and `/swarm` or `run_workflow` domain leads that verify/merge Grok worktrees.
4. **Tier 3 — Gemini 4 Neon XHigh:** Apex planning, security synthesis, and final integration.

## High-Throughput Fleet CLI (`cursor-fleet` / `wm-grok-fleet`)
- **Preflight check:** `cursor-fleet doctor` (checks `cursor-agent`, login, live models, `bwrap`, host RAM/disk on macOS+Linux, presets).
- **Zero-manifest map-reduce (recommended for >6 items or corpus sweeps):**
  - `cursor-fleet map --campaign <id> --preset synthesis --items-jsonl <file> --batch-size 4 --max-usd 1.50 --parse-json --instruction "..." --out out.json --report-md report.md`
  - Add `--dry-run` first to preview worker count, estimated tokens, and estimated Cursor quota `%` for `$0`.
  - Also supports `--files "src/**/*.py"`, `--items-json`, `--stdin-items`, `--allow-any-model` (Composer, Codex, Claude, Auto), and `WM_GROK_FAMILY=grok-4.8`.
- **One-shot parallel prompts:** `cursor-fleet ask --preset synthesis "Prompt 1" "Prompt 2" --report-md out.md`
- **Inspect, report & apply patches:** `cursor-fleet campaigns`, `cursor-fleet inspect --campaign <id> --show-output`, `cursor-fleet report --campaign <id>`, `cursor-fleet patches --campaign <id> --apply <job> --repo <path> --check`.
- **Standalone install for other Cursor users:** `./install.sh --cursor-fleet-only [--wire-cursor-mcp]` (see `docs/CURSOR_FLEET.md`).

## Eager MCP Usage (`grok_dispatch`)
- **Zero-tool pure synthesis (18.8× cheaper, `~$0.05/worker`):** `grok_dispatch(action="ask", preset="synthesis", concurrency=4, tasks=[{name, prompt}, ...])` — auto-applies `empty_cwd=true`, `no_tools_guard=true`, and standard non-fast `grok-4.7-medium`.
- **Read-only parallel audit:** `grok_dispatch(action="ask", model="high", concurrency=3, tasks=[{name, cwd, prompt}, ...])` (defaults to standard non-fast `grok-4.7-high` = 50% less Cursor quota than `-fast`; pass `model="high-fast"` when latency matters).
- **Isolated parallel edits:** `grok_dispatch(action="worktree", model="high", tasks=[{name, cwd, worktree_name, prompt}, ...])` -> review diffs under `~/.cursor/worktrees/<repo>/<name>` before merging.
- **Confined `bwrap` verification (`gate-lite`):** Pass `confined=true` and optional `verify_cmd` (top-level or per-task) to execute post-edit verification inside `bwrap --ro-bind / / --bind <wt> <wt> --unshare-net` and emit a `wm-confinement-receipt/1.1` causal provenance receipt. Fails closed if `bwrap` is unavailable.
- **Signed Causal Provenance Receipts (IETF SCITT / `gate-lite`):** Supports `WM_CONFINEMENT_HMAC_KEY`, `WM_CONFINEMENT_ED25519_KEY_FILE`, `sign_receipt=true`, `sign_alg="auto"|"hmac-sha256"|"ed25519"`, `receipt_log`, `wm-grok-swarm verify-receipt`, and `attest_remote=true` (RFC 9421 POST to `/verify-confinement`).
- **Empirical rules:** Inline key file snippets (<15 KB) with `preset="synthesis"` so workers finish in 15–45s with `0` tool calls; pack `batch_size=4` items per worker on large fleets; prefer non-fast tiers for batch work.
- **Hybrid `run_workflow` script:** See [grok_hybrid_swarm.py](./scripts/grok_hybrid_swarm.py).
