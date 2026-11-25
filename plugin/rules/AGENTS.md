# WhiteMagic AGY Turbo+Fast Directives (v4.4)

## 1. Tool Routing & Latency Hygiene (5 Eager Tools + Fast Built-ins)

| Task | Preferred Tool | Why |
| :--- | :--- | :--- |
| >=2 searches, globs, or read-only `git` checks | `batch_search` | 1 remote check + concurrent `rg`/git (<70ms), <=8.8 KB inline |
| Symbol defs, callers/callees, impact, skeleton | `code_graph` | <6ms Rust AST/call-graph, saves 85-99% tokens |
| Outline unknown >300L files (10-20/batch) | `read_slices({ outlines })` | Maps exact line ranges in 1 call; piggyback <=120L slices |
| Read 1-4 small files (<300L) or <=200L ranges | `read_file` (parallel, bounded) | Local allowlist (~7ms, 0 remote check, 46 KB cap) |
| >=2 edits, multi-file, or edit + verify | `apply_edits` (`bestEffort: true` >=3 files) | Atomic hunks + drift auto-relocate + squeezed `verify` |
| >=2 build/lint/test cmds or noisy build/test | `run_checks` (`compact` default) | Concurrent execution + `wm-squeeze` + 300s git cache |

## 2. Context, Cache & Swarm Quota Control
- **Precision Read & Squeeze**: Locate targets via `batch_search`/`code_graph` first. `PreToolUse` auto-clamps >250L reads (<=150L >=120k ctx, <=120L burst, <=50L on wide-line JSON/JSONL), quote-aware squeezes `rg`/`find`/`git`/test runs + `python3 -c` + heredocs via `wm-squeeze`, and deduplicates unchanged re-reads.
- **Externalize & Cache Longevity**: Inspect large logs via `wm-local-digest --file <path> --query "<q>"` or `| head -n 60`. Keep Turn-0 prefixes immutable; watch zero-token `PostInvocation` HUD alerts (`120k`/`200k`/model flips) in App/IDE; reset cold `>120k` sessions via `wm-handoff` (`--surface all|app|ide|cli`) or `/compact`.
- **Swarm & Quota Discipline**: Pin `/swarm` coordinators to a single model (`agy-mode longevity`, avoiding `Neon <-> Hydrogen` flips >80k ctx). Use script-first generation instead of `>20 KB` `write_file` payloads and retire per-wave verifiers before `100k` ctx. Route bulk corpus work via `grok_dispatch` (`0%` Gemini quota) or `Hydrogen`.
- **4-Tier Compute Pyramid**: `tricameral_dispatch` ($0 local S0/S1) -> `whitemagic-agy-kit_fast` / `Hydrogen` -> `grok_dispatch` -> `Neon` (apex planning). Use `wm-vps` for VPS health and `wm-triage` for 1-shot estate sweeps.

## 3. SIM Continuity & Safety
- Query `wm-sim query "<topic>" --include-failures` before modifying harnesses/prompts/benchmarks; log verified results via `wm-sim log`.
- Check `session_continuity()` at startup and checkpoint milestones via `session_checkpoint`.
- Confirm before destructive edits, deletions, or table drops; keep clickable `file://` links for referenced paths.
