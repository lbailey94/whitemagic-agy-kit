# WhiteMagic AGY Context Hygiene, Memory & Engineering Directives

## 1. Context, Latency & Cache Control (High Priority — Overrides Speculative Pre-Reading)
- **Narrow Search, Precision Read**: Never carpet-bomb speculative whole-file reads. Locate target files and line numbers first (`batch_search` or `rg -l` / `rg -n`), then read only the direct call chain or files being edited (max `<= 3` uncertain files per parallel `read_file` turn).
- **Chunk Large Files (`> 200` lines)**: Do not call `read_file` without `StartLine`/`EndLine` on unknown files over 200 lines. Map symbols via `read_slices({ outlines })` or `rg -n` first, then fetch exact `<= 200`-line slices with parallel `read_file`.
- **Throttle Shell Output**: Pipe exploratory `run_command` outputs (`rg`, `find`, `git diff`, `ls`) through `| head -n 60`, `-l`, or `--stat` (or use `batch_search` / `run_checks`) to avoid 10 KB truncation and secondary `output.txt` disk spills.
- **Externalize Large Corpora (RLM / LID Discipline)**: Keep large logs, dumps, and corpora on disk and inspect them via targeted Python/shell filters or zero-credit local `wm-local-digest --file <path> --query "<q>"` rather than dumping raw multi-KB text into chat history.
- **Deterministic CLI, CodeGraph & Cache Hygiene**: `PreToolUse` auto-compresses/caches safe noisy shell reads (`wm-squeeze --safe`), clamps unbounded `>250`-line reads to `1–200`, deduplicates unchanged re-reads, guards `>=150k` media loads, and budgets `wm3` recall/continuity. Use `wm-codegraph overview|callers|impact|skeleton` before multi-file exploration, `wm-handoff` to reset cold `>120k` sessions cleanly, and `agy-mode` (`longevity|orchestrator|deep|turbo`) per session.
- **Multimodal & Prefix-Cache Hygiene**: Keep prefixes immutable; batch independent tool calls; background >10 s commands; and in `> 150k`-token coding sessions, delegate screenshot/image inspection to a read-only subagent returning text-only findings to avoid cache invalidation and 3–7x TTFT spikes.

## 2. Self-Improvement Memory (SIM) & Continuity Discipline
- Before modifying harnesses, prompts, benchmarks, or training recipes, query `wm-sim query "<topic>" --include-failures`.
- After verified harness/R&D changes, record `(rationale, predicted, observed, eval_context)` via `wm-sim log`.
- When WhiteMagic (`wm3`) is configured, retrieve recent handoff state via `session_continuity()` (last 3 checkpoints) and record milestone checkpoints on completion.

## 3. Operator Policies & Non-Negotiable Directives
- **Zero Rationalist / Corrigibility Funding**: Under NO circumstances propose, pursue, draft, or recommend Corrigibility R2, Lightcone Commons, or any LessWrong / Effective Altruism / Rationalist-adjacent grant tracks or rhetoric. Deterministic confinement (`Landlock`), cryptographic provenance (`CPR`), and IETF SCITT are rigorous systems security engineering.
- **Data Preservation & Loss Prevention**: Always obtain explicit user confirmation before destructive file modifications, irreversible deletions, or dropping database tables. Never overwrite existing verified archive destinations (strict no-clobber).
- **Formatting & Links**: Always use clickable `file://` markdown links for referenced files and line ranges.
