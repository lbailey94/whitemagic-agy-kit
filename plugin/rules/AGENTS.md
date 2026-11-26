# WhiteMagic AGY Turbo+Fast Directives (v4.7.0)

## 1. Tool Routing & Latency Hygiene (5 Eager Tools + Fast Built-ins)

| Task | Preferred Tool | Why |
| :--- | :--- | :--- |
| >=2 searches, globs, or read-only `git` | `batch_search` | 1 remote check + concurrent `rg`/git (<70ms), <=8.8 KB |
| Symbol defs, callers, impact, skeleton | `code_graph` | <6ms Rust AST/call-graph, saves 85-99% tokens |
| Outline unknown >300L files (10-20/batch) | `read_slices({ outlines })` | Maps exact line ranges in 1 call; <=120L slices |
| Read 1-4 small files (<300L) or <=200L | `read_file` (parallel, bounded) | Local allowlist (~7ms, 0 remote check, 46 KB cap) |
| >=2 edits, multi-file, or edit + verify | `apply_edits` (`bestEffort: true` >=3) | Atomic hunks + drift relocate + squeezed `verify` |
| >=2 build/lint/test or noisy commands | `run_checks` (`compact` default) | Concurrent execution + `wm-squeeze` + 300s git cache |

## 2. Context, Cache & Swarm Orchestration
- **Rust PreToolUse & Squeeze:** `wm-squeeze --pre-tool` clamps >250L reads (<=150L >=120k, <=100L >=160k, <=50L wide JSONL), wraps `rg`/`find`/`git`/test/`python3 -c`/safe `for`/heredocs, and deduplicates re-reads.
- **Longevity, Sidecar & CPR Bridge:** Keep Turn-0 fixed (`wm-sim audit --scope-plugins` scopes optional plugins). Watch HUD Neon 100k/120k rollover & ping-pong (`>=2` flips) / live Aux-Pane cockpit; auto-crystal spools to `pending_crystals.jsonl` (`wm-handoff --flush-wm3`); verify/publish `<=150`-tok pointers via `wm-cpr-bridge` & reset via `wm-reset`.
- **Swarm & Native Agents:** Generate `wm-handoff --swarm-map .` (<=600 tok); dispatch `hydrogen-builder`, `fresh-verifier` (<60k), or `swarm-coordinator` (<80k). Pass `model="gemini-4-hydrogen-high"` + `workspace="share"` (never `fork` >=80k); keep board posts <=4 KB (`<=150`-tok CPRs).
- **4-Tier Pyramid:** `tricameral_dispatch` ($0 local) -> `whitemagic-agy-kit_fast` / `Hydrogen` -> `grok_dispatch` -> `Neon` (plan `<80k`, pin `>100k` via `/model`, `/btw` for asides).

## 3. SIM Continuity & Safety
- Query `wm-sim query "<topic>" --include-failures` before modifying harnesses/prompts/benchmarks; log verified outcomes via `wm-sim log`.
- Check `session_continuity()` at startup, checkpoint via `session_checkpoint`, confirm destructive edits, and keep clickable `file://` links.
