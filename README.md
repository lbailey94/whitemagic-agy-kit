# WhiteMagic AGY Turbo Kit (`whitemagic-agy-kit` v2.0.0)

Cross-platform (**macOS Apple Silicon/Intel + Linux**) Antigravity CLI (`agy 1.5.14+`), Desktop 2.0, and IDE optimization suite focused on **KV/prefix-cache preservation, birth-time token compression, latency reduction, and credit longevity**.

Built from empirical offline forensics across `40` real sessions (`9,221` turns, `1.81B` input tokens) and designed strictly within Antigravity's documented extension interfaces (`plugin.json`, `hooks.json`, `mcp_config.json`, `rules/AGENTS.md`, `statusLine`, and `--model` / `--settings`).

---

## What's Included in v2.0.0

1. **`wm-squeeze` — Zero-Dependency Rust CLI Output Compressor (`tools/wm-squeeze/`):**
   - Single stripped `std`-only Rust binary (`~465 KB`, `<5 ms`) wired via `PreToolUse` `overwrite` for strictly allowlisted read-only and local build/test commands (`git`, `cargo`, `pytest`, `npm`/`pnpm`/`yarn`, `rg`, `grep`, `find`, `ls`, `tree`, `gh`, `wm3`, `ruff`, `mypy`, `eslint`, `tsc`).
   - **Zero permission bypass:** `--safe` executes directly via `execvp` (no `sh -c`), enforces a compiled-in safe subcommand allowlist, and refuses dangerous flags (`find -exec/-delete`, `git -c/--exec-path`, `rg --pre`, `cargo --config`, etc.) with exit code `2`.
   - **Prefix-cache-friendly compression at birth:** strips ANSI/progress spam, collapses passing test/compile boilerplate while preserving 100% of errors/traces/exit codes, skips lockfile diff bloat, deduplicates repeated lines (`[xN]`), caps inline stdout at `<= 8,200 B` (below AGY's `10 KB` disk-spill threshold), and saves full overflow to `/tmp/wm-squeeze/` only when clipped.
2. **Deterministic `PreToolUse` Safety & Anti-Bloat Engine (`plugin/hooks/pre_tool_guard.py`):**
   - **Data-Loss Guard (`force_ask`)**: prompts explicitly on destructive shell patterns across `run_command` and `agy-fast` tools (Linux + macOS `diskutil`).
   - **Large Unbounded `read_file` Clamp (`overwrite`)**: clamps omitted-range reads on `> 250`-line text files to `L1–200`.
   - **Unchanged Duplicate-Read Dedup (`deny`)**: blocks identical unchanged file-slice re-reads within 20 steps (auto-resets on file edit or context compaction).
   - **Multimodal Cache-Cliff Guard (`force_ask`)**: prompts before loading images/PDFs inline once session context reaches `>= 150k` tokens.
   - **WhiteMagic MCP Auto-Budget (`overwrite`)**: defaults `session_continuity` to `token_budget: 1200` and `memory_recall` to `limit <= 5`, `max_content_bytes <= 900` so Turn 1 stays inline without disk spills.
3. **Live Cache-Warmth `statusLine`, `Stop` Telemetry & `agy-bench --credits`:**
   - [bin/statusline.py](bin/statusline.py): `<20 ms` O(1) tail-seek rendering `Model | ctx:124k | hit:95% warm:12s unc:145k | git | disk`.
   - [plugin/hooks/stop_telemetry.py](plugin/hooks/stop_telemetry.py): zero per-tool overhead `Stop` hook logging rolling cache/credit telemetry to `~/.local/share/whitemagic/agy_telemetry.jsonl`.
   - [bin/agy-bench](bin/agy-bench): offline A/B comparator with `--credits` (per-model uncached/turn longevity table, inter-turn cache decay buckets, and `wm-squeeze` cumulative savings).
4. **Non-Destructive Per-Session Model Profile Launcher (`bin/agy-mode`):**
   - `agy-mode longevity` (Hydrogen XHigh — `94.7%` cache hit, `~11k` uncached/turn in local telemetry), `agy-mode orchestrator` (`agy 1.5.14` Planner+Executor split), `agy-mode deep` (Argon High — `97.7%` hit across 30–60s gaps), `agy-mode turbo` (Neon XHigh for `<15s` bursts), and `agy-mode status`. Never alters `settings.json`.
5. **Cache-Safe `$0` Local Offload (`bin/wm-local-digest`) & Consolidation (`bin/wm-dream`):**
   - `wm-local-digest`: defaults to sub-2s hybrid local embedding (`:18899`) + lexical extraction compressing huge logs `-90%` at `$0` cloud cost without breaking Gemini 4's `15–30s` prefix-cache window (opt-in `--llm` for local `llama-server :18898` synthesis).
   - `wm-dream`: offline consolidation distilling recurring `wm-sim` tool failures, cache trends, and `wm-squeeze` savings into a `<480 B` immutable Turn-0 digest.
6. **Integration with Hardened `INK-Q/agy-fast` (`v0.2.8`), Plugin Bloat Pruning (`-10.4k` tok), `wm-sim`, and 4 Portable Skills.**

---

## Quickstart (macOS & Linux)

```bash
git clone https://github.com/lbailey94/whitemagic-agy-kit.git ~/.local/src/whitemagic-agy-kit
cd ~/.local/src/whitemagic-agy-kit
./install.sh                  # installs as native AGY plugin (or --sync-global to merge into ~/.gemini/config)
```

Ensure `~/.local/bin` is on your `PATH`, then launch a fresh `agy` session (or `agy-mode longevity` / `agy-mode orchestrator`).

## Measuring Your Speedup & Credit Longevity (100% Offline)

```bash
agy-bench --credits --recent 30 --save --label pre_wave3
# After 5–10 sessions:
agy-bench --credits --since 2026-11-21T16:30:00-05:00 --save --label post_wave3
agy-bench --compare
wm-squeeze --stats
python3 tests/test_wave3.py
```

---

## Credits, Provenance & Upstream Research

This repository stands on the shoulders of open-source tool builders, community benchmarkers, and systems researchers. We want to be completely transparent about what is **installed from upstream**, what is **bundled directly**, what is **inspired clean-room code**, and what is **original work**:

| Upstream Project / Author / Paper | Relationship | Specific Contribution Credited |
| :--- | :--- | :--- |
| **[`INK-Q/agy-fast`](https://github.com/INK-Q/agy-fast)** (`@star_flow_ai`, MIT) | **Installed from upstream** by `install.sh` (unmodified) + parsed by `agy-bench` | Built the 4-tool eager Stdio MCP (`batch_search`, `apply_edits`, `read_slices`, `run_checks`), `SessionStart` workspace snapshot, `dist/benchmark.mjs`, and the 6-finding empirical Antigravity latency analysis (`ANALYSIS_REPORT.md`). |
| **[`sybil-solutions/inference-optimisation-skill`](https://github.com/sybil-solutions/inference-optimisation-skill)** (`@0xSero`) | **Bundled upstream skill** under `plugin/skills/inference-optimisation/` | Hardware-ceiling-first (`ceiling.py`) + thermally interleaved A/B (`queue.sh`) local inference optimization methodology. |
| **[`@leoshen0`](https://x.com/leoshen0/status/2123861275486945745)** | **Inspired rules & `read_file` guard** | Diagnosed AGY's built-in speculative pre-read reflex and empirically demonstrated the 3-line narrow-search / `<=200`-line ranged-read / shell-throttling rule (`-43%` cost, `-75.9%` uncached input). |
| **[`rtk-ai/rtk`](https://github.com/rtk-ai/rtk)** (Rust Token Killer) | **Inspired clean-room design** | Pioneered deterministic command-aware CLI output compression via `PreToolUse` hooks. `wm-squeeze` is an independent, clean-room, zero-dependency Rust implementation written specifically for AGY's `PreToolUse` `overwrite` contract and `--safe` `execvp` allowlist. |
| **[`@bebuu_ai`](https://x.com/bebuu_ai/status/2123414001912861014)** & **[`@dheeraja486`](https://x.com/dheeraja486/status/2123432395374514516)** | **Research & telemetry inspiration** | Identified Gemini 4 implicit prefix-cache decay across inter-turn gaps (`Neon`/`Carbon` vs `Hydrogen`), which guided our local 40-session decay forensics, `statusline.py` cache-warmth timer, and `agy-mode`. |
| **Yu et al. — *The Physical Cost of Context*** ([`arXiv:2610.34763`](https://arxiv.org/abs/2610.34763), Oct 2026) | **Theoretical foundation** | Proved post-hoc history masking breaks KV-cache prefixes (`+608.5%` uncached prefill), directly motivating our append-only **birth-time** compression (`wm-squeeze`, `read_file` clamping, MCP auto-budget). |
| **Chen et al. — *CoACT*** ([`arXiv:2607.02911`](https://arxiv.org/abs/2607.02911)) & **Nguyen, Dettmers et al. — *CliffCompaction*** ([`arXiv:2609.26779`](https://arxiv.org/abs/2609.26779)) | **Research inspiration** | Action-preserving observation compression and truncation-only long-horizon compaction principles. |
| **Google Cloud (`@googlecloud`)**, **Google Research ([`Harness²`](https://github.com/google-research/harness2), `arXiv:2610.14224`)**, **Google SIM (`arXiv:2611.18856`)**, & **GAIR-NLP (*No Second Run / Model 801*)** | **Research inspiration** | 5 multi-agent orchestration patterns (`plugin/skills/agent-orchestration-patterns/`), structured continual self-improvement memory (`wm-sim`), and negative-knowledge failure logging (`post_tool_sim.py`). |
| **Alex Zhang / RLM (`@a1zhang` / `@himanshutwtxs`)**, **Anthropic `/dream`**, **`@mary_nekyia`**, **`@u1`**, & **`@Astronaut_1216`** | **Research inspiration** | Externalized corpus querying (`wm-local-digest`), periodic offline memory/rule consolidation (`wm-dream`), `AGENTS.md` hygiene auditing, and Planner/Executor routing (`agy-mode`). |

**Original to `whitemagic-agy-kit`:** `wm-squeeze` (clean-room zero-dependency Rust `--safe` `execvp` compressor), the unified `PreToolUse` `overwrite` guard (`read_file` auto-clamp + compaction-aware unchanged-slice dedup + `>=150k` multimodal cache guard + WhiteMagic MCP auto-budget + data-loss guard), cache-warmth/uncached-burn `statusline.py`, `stop_telemetry.py`, `agy-bench --credits`, `agy-mode`, `wm-sim`, `wm-local-digest`, `wm-dream`, cross-platform `install.sh`, and the 26-test verification suite (`tests/test_wave3.py`).

## License

MIT (see bundled upstream skills for their respective upstream licenses).
