# WhiteMagic AGY Turbo+Fast Directives (v4.0)

## 1. Tool Routing & Latency Hygiene (5 Eager Tools + Fast Built-ins)

| Task | Preferred Tool | Why |
| :--- | :--- | :--- |
| >=2 searches, globs, fuzzy paths, or read-only `git` checks | `batch_search` | 1 remote check + concurrent `rg`/git (<70ms), auto-collapses lockfile diffs, <=8.8 KB inline |
| Symbol defs, cross-file callers/callees, impact, or AST skeleton | `code_graph` | <6ms Rust AST/call-graph (`overview\|defs\|callers\|callees\|impact\|skeleton`), saves 85–99% tokens |
| Outline unknown >300L files (10–20/batch) | `read_slices({ outlines })` | Maps exact symbol line ranges in 1 call; piggyback only tiny <=120L slices |
| Read 1–4 small files (<300L) OR known <=200L line ranges | Built-in `read_file` (parallel, `StartLine`/`EndLine`) | Local allowlist (~7ms, **0 remote check**, 46 KB/call inline) |
| >=2 edits in one file, >=2 files, or edit + verify | `apply_edits` (`bestEffort: true` for >=3 files) | Atomic multi-hunk writes + unique line-drift auto-relocate + `wm-squeeze` inline `verify` |
| >=2 build/lint/test commands (or single noisy build/test) | `run_checks` (`compact` default) | Concurrent execution with `wm-squeeze` CoACT compression + 300s git-aware cache |

## 2. Context & Cache Control
- **Narrow Search, Precision Read**: Locate targets via `batch_search` or `code_graph` first; never blind-read unknown >200L files without `StartLine`/`EndLine` (`PreToolUse` auto-clamps >250L reads to L1–200 and deduplicates unchanged re-reads).
- **Throttle & Externalize**: Pipe exploratory `run_command` outputs through `| head -n 60` or `--stat`; inspect large corpora on disk via `wm-local-digest --file <path> --query "<q>"`.
- **Cache Longevity**: Keep Turn-0 prefixes immutable; reset cold `>120k` sessions via `wm-handoff`; launch via `agy-mode` (`longevity|orchestrator|deep|turbo`). At `>=150k` context, delegate image/screenshot inspection to a read-only subagent returning text-only findings.
- **4-Tier Compute Pyramid & Ops**: Use `tricameral_dispatch` (`$0` S0/S1 local reflex) -> `whitemagic-agy-kit_fast` / `Hydrogen` (warm AGY loops) -> `grok_dispatch` (parallel Grok 4.7 fan-out / isolated worktrees / cross-family audit) -> `Neon` (apex planning). Use `wm-vps` for read-only VPS probes and `wm-triage` for 1-shot estate sweeps.

## 3. SIM Continuity & Safety
- Before modifying harnesses, prompts, benchmarks, or training recipes, query `wm-sim query "<topic>" --include-failures`; log verified outcomes via `wm-sim log`.
- Retrieve recent handoff state via `session_continuity()` and record milestone checkpoints on completion.
- **Data Preservation**: Always obtain explicit user confirmation before destructive edits, deletions, or table drops; never overwrite existing archive destinations. Use clickable `file://` markdown links for referenced paths.
