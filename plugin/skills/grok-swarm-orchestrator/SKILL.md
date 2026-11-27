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
- **Preflight check:** `cursor-fleet doctor` (checks `cursor-agent`, login, live models, `bwrap`, host RAM/disk on macOS+Linux, 7 presets: `synthesis`, `dataset`, `blueprint`, `fast`, `audit`, `plan`, `patch`).
- **Zero-manifest map-reduce (recommended for >6 items or corpus sweeps):**
  - `cursor-fleet map --campaign <id> --preset synthesis --state-dir ./_fleet --items-jsonl <file> --batch-size 4 --max-usd 1.50 --parse-json --instruction "..." --reduce-instruction "..." --out out.json --report-md report.md`
  - Add `--dry-run` first to preview worker count, estimated tokens, and estimated Cursor quota `%` for `$0`.
  - For bulk locally-verified JSON/code dataset synthesis, use **`--preset dataset`** (`grok-4.7-low`, `12x` conc., ~4× faster & ~3–4× cheaper than `medium` with `85.7%–96.7%` exec pass rates); for long multi-section specs, use **`--preset blueprint`** (`grok-4.7-high-fast`, `480s` wall + `90s` streaming grace).
- **Self-healing retries & streaming grace:** Default `--streaming-grace 60` protects actively streaming answers from mid-sentence wall kills; `--timeout-fallback-model auto` auto-steps down timed-out jobs (`high -> high-fast -> medium-fast -> low -> low-fast`); `cursor-fleet retry --campaign <id> [--non-done] --model <tier> --run` switches tiers without manual SQLite edits.
- **Best-of-N Worktree Tournament (`race`):** `cursor-fleet race --cwd <repo> --n 3 --verify-cmd "pytest -q" --score-regex "median=([0-9.]+)ms" --confined --prompt "..."` (ranks by pass → score → diff size → speed; `--apply-winner` or `patches --apply`).
- **Blind Multi-Model Council (`council`):** `cursor-fleet council --files "auth.py" --models "grok-4.7-high,composer-2.5,claude-sonnet-5-thinking-high" --prompt "..."` (blind panelists + consensus judge).
- **Multi-Stage Funnel (`pipeline`):** `cursor-fleet pipeline examples/cursor-fleet/security_funnel.json` (`map -> deterministic local filter/exec-gate -> audit/patch/race/council`).
- **One-shot parallel prompts:** `cursor-fleet ask --preset blueprint --prompt-file spec.txt --text-out SPEC.md` (clean unwrapped raw text) or `--report-md out.md`.
- **Inspect, report & apply patches:** `cursor-fleet campaigns`, `cursor-fleet inspect --campaign <id> --show-output`, `cursor-fleet report --campaign <id>`, `cursor-fleet patches --campaign <id> --apply <job> --repo <path> --check`.
- **Standalone install for other Cursor users:** `./install.sh --cursor-fleet-only [--wire-cursor-mcp]` (see `docs/CURSOR_FLEET.md`).

## Eager MCP Usage (`grok_dispatch`)
- **Zero-tool pure synthesis (18.8× cheaper, `~$0.05/worker`):** `grok_dispatch(action="ask", preset="synthesis", concurrency=4, tasks=[{name, prompt}, ...])` — also supports `preset="dataset"` (`low`, ultra-fast bulk JSON/code) and `preset="blueprint"` (`high-fast`).
- **Read-only parallel audit:** `grok_dispatch(action="ask", model="high", concurrency=3, tasks=[{name, cwd, prompt}, ...])` (defaults to standard non-fast `grok-4.7-high` = 50% less Cursor quota than `-fast`; pass `model="high-fast"` when latency matters).
- **Isolated parallel edits:** `grok_dispatch(action="worktree", model="high", tasks=[{name, cwd, worktree_name, prompt}, ...])` -> review diffs under `~/.cursor/worktrees/<repo>/<name>` before merging.
- **Confined `bwrap` verification (`gate-lite`):** Pass `confined=true` and optional `verify_cmd` (top-level or per-task) to execute post-edit verification inside `bwrap --ro-bind / / --bind <wt> <wt> --unshare-net` and emit a `wm-confinement-receipt/1.1` causal provenance receipt. Fails closed if `bwrap` is unavailable.
- **Signed Causal Provenance Receipts (IETF SCITT / `gate-lite`):** Supports `WM_CONFINEMENT_HMAC_KEY`, `WM_CONFINEMENT_ED25519_KEY_FILE`, `sign_receipt=true`, `sign_alg="auto"|"hmac-sha256"|"ed25519"`, `receipt_log`, `wm-grok-swarm verify-receipt`, and `attest_remote=true` (RFC 9421 POST to `/verify-confinement`).
- **Empirical rules:** Inline key file snippets (<15 KB) with `preset="synthesis"`/`dataset` so workers finish fast with `0` tool calls; watch `reason=XX%` telemetry; pack `batch_size=4` items per worker on triage fleets; use `--state-dir` for isolated campaign DBs.
- **Hybrid `run_workflow` script:** See [grok_hybrid_swarm.py](./scripts/grok_hybrid_swarm.py).
