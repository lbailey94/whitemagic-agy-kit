# WhiteMagic AGY Turbo+Fast Directives (v4.1)

## 1. Tool Routing & Latency Hygiene (5 Eager Tools + Fast Built-ins)

| Task | Preferred Tool | Why |
| :--- | :--- | :--- |
| >=2 searches, globs, fuzzy paths, or read-only `git` checks | `batch_search` | 1 remote check + concurrent `rg`/git (<70ms), auto-collapses lockfile diffs, <=8.8 KB inline |
| Symbol defs, cross-file callers/callees, impact, or AST skeleton | `code_graph` | <6ms Rust AST/call-graph (`overview\|defs\|callers\|callees\|impact\|skeleton`), saves 85–99% tokens |
| Outline unknown >300L files (10–20/batch) | `read_slices({ outlines })` | Maps exact symbol line ranges in 1 call; piggyback only tiny <=120L slices |
| Read 1–4 small files (<300L) OR known <=200L line ranges | Built-in `read_file` (parallel, `StartLine`/`EndLine`) | Local allowlist (~7ms, **0 remote check**, 46 KB/call inline) |
| >=2 edits in one file, >=2 files, or edit + verify | `apply_edits` (`bestEffort: true` for >=3 files) | Atomic multi-hunk writes + unique line-drift auto-relocate + `wm-squeeze` inline `verify` |
| >=2 build/lint/test commands (or single noisy build/test) | `run_checks` (`compact` default) | Concurrent execution with `wm-squeeze` CoACT compression + 300s git-aware cache |

## 2. Context, Cache & Swarm Quota Control
- **Narrow Search, Precision Read**: Locate targets via `batch_search` or `code_graph` first; v4.1 `PreToolUse` auto-clamps >250L slices (<=150L above `120k` ctx, <=120L on >=4-file read bursts), squeezes multiline `python3 - <<'PY'` heredocs via `wm-squeeze`, and deduplicates unchanged re-reads.
- **Throttle & Externalize**: Pipe exploratory `run_command` outputs through `| head -n 60` or `--stat`; inspect large corpora on disk via `wm-local-digest --file <path> --query "<q>"`.
- **Cache Longevity**: Keep Turn-0 prefixes immutable; reset cold `>120k` sessions via `wm-handoff`; launch via `agy-mode` (`longevity|orchestrator|deep|turbo`). At `>=150k` context, delegate image/screenshot inspection to a read-only subagent returning text-only findings.
- **Swarm & Quota Discipline**:
  - Run `/swarm` from a **single-model coordinator** (avoid Orchestrator `Neon <-> Hydrogen` flips on `>100k` contexts, which bust Gemini prefix cache to `0%`).
  - Enforce **script-first JSON generation** on workers (write large JSON/docs via Python scripts rather than `>20 KB` `write_file` output tokens) and **fresh per-wave verifiers** (retire after each wave; never carry a verifier past `100k` ctx).
  - Route heavy read-only corpus waves through `grok_dispatch` (`0%` Gemini quota) or `hydrogen-medium`/`high`, reserving `xhigh` for apex design/verification; audit burn via `agy-token-audit --swarm <id>`.
- **4-Tier Compute Pyramid & Ops**: Use `tricameral_dispatch` (`$0` S0/S1 local reflex) -> `whitemagic-agy-kit_fast` / `Hydrogen` (warm AGY loops) -> `grok_dispatch` (parallel Grok 4.7 fan-out / isolated worktrees / cross-family audit) -> `Neon` (apex planning). Use `wm-vps` for read-only VPS probes and `wm-triage` for 1-shot estate sweeps.

## 3. SIM Continuity & Safety
- Before modifying harnesses, prompts, benchmarks, or training recipes, query `wm-sim query "<topic>" --include-failures`; log verified outcomes via `wm-sim log`.
- Retrieve recent handoff state via `session_continuity()` and record milestone checkpoints on completion.
- **Data Preservation**: Always obtain explicit user confirmation before destructive edits, deletions, or table drops; never overwrite existing archive destinations. Use clickable `file://` markdown links for referenced paths.
