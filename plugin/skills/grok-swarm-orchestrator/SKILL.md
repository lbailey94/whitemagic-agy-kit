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
- **Confined `bwrap` verification (`gate-lite`):** Pass `confined=true` and optional `verify_cmd` (top-level or per-task) to execute post-edit verification inside `bwrap --ro-bind / / --bind <wt> <wt> --unshare-net` and emit a `wm-confinement-receipt/1.1` causal provenance receipt (`receipt_version`, `issued_at_utc`, `bwrap_bin`, `network_isolated`, `worktree_path`, `worktree_diff_sha256`, `verify_cmd_sha256`, `bwrap_flags`, `verify_exit_code`, `scope_confined`, `signature_alg`, `signature_b64`). Fails closed if `bwrap` is unavailable.
- **Signed Causal Provenance Receipts (IETF SCITT / `gate-lite`):**
  - **Keys & Auto-Sign:** Set `WM_CONFINEMENT_HMAC_KEY` (`UTF-8`, `hex:<hex>`, or `base64:<b64>`), `WM_CONFINEMENT_HMAC_KEY_FILE`, or `WM_CONFINEMENT_ED25519_KEY_FILE` (PEM PKCS8 or raw 32-byte key). Auto-signs when any key env is set unless explicitly disabled (`sign_receipt=false` or `WM_CONFINEMENT_SIGN=0`).
  - **Explicit Controls & Fail-Closed Enforcement:** Pass `sign_receipt=true`, `sign_alg="auto"|"hmac-sha256"|"ed25519"`, or `WM_CONFINEMENT_SIGN=1`. Missing/unreadable keys, missing `cryptography` for Ed25519, or algorithm mismatch fail closed (`ok=false`, `signature_alg=null`, `signature_b64=null`) before spawning workers or `bwrap`.
  - **Canonicalization & Ledger:** `canonical_receipt_bytes` signs sorted compact JSON excluding `signature_b64` while binding `signature_alg` to prevent downgrade tampering. Set `WM_CONFINEMENT_RECEIPT_LOG` or `receipt_log` to append compact JSONL records under `fcntl.flock(LOCK_EX)`.
  - **Verification CLI:** `wm-grok-swarm verify-receipt --file <path|-> [--hmac-key <key>] [--pubkey-file <path>] [--attest-remote] [--attest-url <url>]` (uses `WM_CONFINEMENT_ED25519_PUBKEY_FILE` or key env fallback).
  - **Remote RFC 9421 Attestation:** Set `attest_remote: true` (or `--attest-remote`) on `grok_dispatch` / `wm-grok-swarm` (`run`, `worktree`, `verify-receipt`) to POST signed receipts to `attest_url` / `WM_CONFINEMENT_ATTEST_URL` (default `https://api.whitemagic.dev/verify-confinement?receipt=1`).
  - **Trusted Agent Keyring:** Signs HTTP requests with RFC 9421 (`@method`, `@path`, `@authority`) from `WM_TRUSTED_AGENT_KEY_FILE` (default `~/.config/whitemagic/trusted_agent_key.json`), falling back cleanly to `X-WM-Trial: 1` or `WM_API_KEY`; fails closed if remote attestation rejects or errors.
- **Tip:** Inline key file snippets (<15 KB) or exact `file:Lstart-Lend` targets into each worker's prompt so `ask` workers finish in 15–45s instead of spending turns searching.
- **Hybrid `run_workflow` script:** See [grok_hybrid_swarm.py](./scripts/grok_hybrid_swarm.py).
