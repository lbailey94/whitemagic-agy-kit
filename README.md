# WhiteMagic AGY Turbo Kit (`whitemagic-agy-kit` v3.0.0)

Cross-platform (**macOS Apple Silicon/Intel + Linux**) Antigravity CLI (`agy 1.5.14+`), Desktop 2.0, and IDE optimization suite focused on **KV/prefix-cache preservation, birth-time token compression, structural code-graph navigation, latency reduction, and credit longevity**.

Built from empirical offline forensics across `50` real sessions (`12,740` turns, `2.27B` input tokens) plus controlled 4-level live `agy -p` A/B benchmarks ([`benchmarks/RESULTS_2026-11-21.md`](./benchmarks/RESULTS_2026-11-21.md)) and designed strictly within Antigravity's documented extension interfaces (`plugin.json`, `hooks.json`, `mcp_config.json`, `rules/AGENTS.md`, `statusLine`, and `--model` / `--settings`).

---

## Headline Empirical Results ([Full Report & Raw Receipts](./benchmarks/RESULTS_2026-11-21.md))

- **Live 4-Level Controlled A/B (`agy -p`, incremental on top of Wave 1/2 rules):**
  - **100% elimination of `run_command` disk spills** (`0` spills across all Wave 4 runs vs `4` on controls)
  - **L1 Simple:** `-12.4%` credit index, `-23%` wall-time (`43.9s → 33.7s`), `5 → 4` turns
  - **L2 Medium (2-hop cross-file refactor):** **`-18.2%`** credit, **`-51%`** wall-time (`49.4s → 24.4s`) with `Hydrogen`
  - **L3 Complex (65 KB noisy CI log + 3 bugs):** **`-15.7%`** credit (`Neon`), **`-43%`** wall-time (`Hydrogen`)
  - **L4 Hard Rust Crate (`220` noisy `cargo test` + 3 bugs):** **`-28.1%`** cold (`Hydrogen`, `-40%` wall-time, `-50%` tool bytes), **`-26.9%` cold-adjusted / `-46.6%` with verified 85.2% Turn-0 cross-session prefix cache hit** (`Neon`)
  - **L5 Long-Horizon 5-Phase Monorepo Stress (`17–20` continuous turns, Python+Rust + 80 KB log + 50 KB `Cargo.lock` diff):** **`-50.2%` wall-time (`244s → 122s`, `2.01×` faster)**, **`-43%` tool calls (`37 → 21`)**, **`-25%` tool bytes**, **`-16.7%` uncached tokens**, **`-12.2%` credit** (`Hydrogen`, incremental on top of Wave 1/2 rules), and **`678`-token `wm-handoff` crystal (`-98.6%` vs `48k` cold resume)**
- **Component Micro-Benchmarks & Full 2.27B-Token Corpus Forensics:**
  - `wm-squeeze v3`: **`-98.8%`** noisy `cargo test`, **`-99.5%`** lockfile `git diff`, **`-80.1%`** 420-diagnostic linter output, **`31.1×`** faster repeat checks via content-addressed cache (`9.2 ms`)
  - `wm-codegraph`: **`-99.65%` (`289×`)** token reduction tracing all 14 2-hop callers across a 13.4k-line Rust harness in `6.0 ms` (`133,468 → 462` tokens)
  - `wm-handoff` (*CliffCompaction*): targets **`30.5%` (`45.10M` uncached tokens)** of all historical uncached burn lost to cold `>=5m` user-idle session resumes
  - `agy-mode longevity`: `Gemini 4 Hydrogen (XHigh)` averages **`11.0k` uncached/turn (`94.7%` hit)** vs `Neon` **`34.4k` (`86.9%` hit)** — **`3.13×`** higher credit longevity

---

## What's Included in v3.0.0

1. **`wm-squeeze v3` — Zero-Dependency Rust CoACT CLI Compressor & Cache (`tools/wm-squeeze/`):**
   - Single stripped `std`-only Rust binary (`<500 KB`, `<3 ms`) wired via `PreToolUse` `overwrite` for strictly allowlisted read-only and local build/test commands (`git`, `cargo`, `go test`, `pytest`, `npm`/`pnpm`/`yarn`, `rg`, `grep`, `find`, `ls`, `tree`, `gh`, `wm3`, `ruff`, `mypy`, `eslint`, `tsc`).
   - **Zero permission bypass:** `--safe` executes directly via `execvp` (no `sh -c`), enforces a compiled-in safe allowlist, and refuses dangerous flags (`find -exec/-delete`, `git -c/--exec-path`, `rg --pre`, `cargo --config`) with exit code `2`.
   - **Content-addressed git-aware result cache** (`300s` TTL, auto-invalidated on any file edit) prevents 20–60 s repeat rebuild gaps from evicting Gemini 4's prefix cache.
2. **`wm-codegraph v3` — Zero-Dependency Rust Incremental Symbol & Call-Graph Engine (`tools/wm-codegraph/`):**
   - Indexes Rust, Python, TS/JS, Go, C/C++, and Shell incrementally (`~/.cache/wm-codegraph/`).
   - Runs as CLI (`wm-codegraph overview|defs|callers|callees|impact|skeleton <target>`) or 1-tool local Stdio MCP (`wm-codegraph --mcp`).
3. **Deterministic `PreToolUse` Safety & Anti-Bloat Engine (`plugin/hooks/pre_tool_guard.py`):**
   - Data-Loss Guard (`force_ask`), `wm-squeeze --safe` `overwrite`, `>250`-line unbounded `read_file` auto-clamp (`L1–200`), compaction-aware unchanged duplicate-read dedup (`deny`), `>=150k` multimodal cache-cliff guard (`force_ask`), and WhiteMagic MCP `token_budget`/`max_content_bytes` auto-caps.
4. **`wm-handoff` (*CliffCompaction* Verbatim Session Reset Crystalizer, `bin/wm-handoff`):**
   - Zero-paraphrase, truncation-only, SHA-256-pinned `<1.8k`-token session handoff crystal for cleanly resetting cold `>100k` sessions instead of paying 200k+ uncached re-prefills.
5. **Zero-Jitter Turn-0 Prefix (`ContextCacheToken`), Cache-Warmth `statusLine`, `Stop` Telemetry & `agy-bench --credits`:**
   - Stable `SessionStart` header (no volatile per-minute `age=Xm`) enables cross-session & subagent implicit prefix cache hits; `wm-sim prefix-hash` audits Turn-0 drift; `statusline.py` displays live `hit:XX% warm:12s unc:Xk`.
6. **Non-Destructive `agy-mode` Launcher (`longevity|orchestrator|deep|turbo|status`), `$0` Local Offload (`wm-local-digest`), `wm-dream` Consolidation, Hardened `agy-fast`, Plugin Pruning (`-10.4k` tok), and 34-Test Suite (`tests/test_wave3.py`).**

---

## Quickstart (macOS & Linux)

```bash
git clone https://github.com/lbailey94/whitemagic-agy-kit.git ~/.local/src/whitemagic-agy-kit
cd ~/.local/src/whitemagic-agy-kit
./install.sh                  # native AGY plugin (or --sync-global to merge into ~/.gemini/config)
```

Verify and benchmark offline or live:

```bash
python3 tests/test_wave3.py
python3 benchmarks/run_progressive_bench.py --track1
agy-bench --credits --recent 30
wm-sim prefix-hash
wm-squeeze --stats
```

---

## Credits, Provenance & Upstream Research

This repository stands on the shoulders of open-source tool builders, community benchmarkers, and systems researchers. We are transparent about what is **installed from upstream**, **bundled**, **inspired clean-room code**, and **original engineering**:

| Upstream Project / Author / Paper | Relationship | Specific Contribution Credited |
| :--- | :--- | :--- |
| **[`INK-Q/agy-fast`](https://github.com/INK-Q/agy-fast)** (`@star_flow_ai`, MIT) | **Installed from upstream** by `install.sh` (unmodified) + parsed by `agy-bench` | Built the 4-tool eager Stdio MCP (`batch_search`, `apply_edits`, `read_slices`, `run_checks`), `SessionStart` workspace snapshot, `dist/benchmark.mjs`, and the 6-finding empirical Antigravity latency analysis (`ANALYSIS_REPORT.md`). |
| **[`sybil-solutions/inference-optimisation-skill`](https://github.com/sybil-solutions/inference-optimisation-skill)** (`@0xSero`) | **Bundled upstream skill** under `plugin/skills/inference-optimisation/` | Hardware-ceiling-first (`ceiling.py`) + thermally interleaved A/B (`queue.sh`) local inference optimization methodology. |
| **[`@leoshen0`](https://x.com/leoshen0/status/2123861275486945745)** | **Inspired rules & `read_file` guard** | Diagnosed AGY's built-in speculative pre-read reflex and empirically demonstrated the 3-line narrow-search / `<=200`-line ranged-read / shell-throttling rule (`-43%` cost, `-75.9%` uncached input). |
| **[`rtk-ai/rtk`](https://github.com/rtk-ai/rtk)** (Rust Token Killer) | **Inspired clean-room design** | Pioneered deterministic command-aware CLI output compression via `PreToolUse` hooks. `wm-squeeze` is an independent, clean-room, zero-dependency Rust implementation built for AGY's `PreToolUse` `overwrite` contract, `--safe` `execvp` allowlist, and content-addressed git caching. |
| **[`@bebuu_ai`](https://x.com/bebuu_ai/status/2123414001912861014)** & **[`@dheeraja486`](https://x.com/dheeraja486/status/2123432395374514516)** | **Research & telemetry inspiration** | Identified Gemini 4 implicit prefix-cache decay across inter-turn gaps (`Neon`/`Carbon` vs `Hydrogen`), guiding our 50-session forensics, `statusline.py` cache-warmth timer, and `agy-mode`. |
| **Yu et al. — *The Physical Cost of Context*** ([`arXiv:2610.34763`](https://arxiv.org/abs/2610.34763), Oct 2026) | **Theoretical foundation** | Proved post-hoc history masking breaks KV-cache prefixes (`+608.5%` uncached prefill), directly motivating append-only **birth-time** compression (`wm-squeeze`, `read_file` clamp, `wm-codegraph`, MCP auto-budget). |
| **Chen et al. — *CoACT*** ([`arXiv:2607.02911`](https://arxiv.org/abs/2607.02911)) & **Nguyen, Dettmers et al. — *CliffCompaction*** ([`arXiv:2609.26779`](https://arxiv.org/abs/2609.26779)) | **Research inspiration** | Action-preserving diagnostic grouping (`wm-squeeze v3`) and zero-paraphrase truncation-only handoffs (`wm-handoff`). |
| **Google Cloud (`@googlecloud`)**, **Google Research ([`Harness²`](https://github.com/google-research/harness2), `arXiv:2610.14224`)**, **Google SIM (`arXiv:2611.18856`)**, & **GAIR-NLP (*No Second Run / Model 801*)** | **Research inspiration** | 5 multi-agent orchestration patterns (`plugin/skills/agent-orchestration-patterns/`), structured continual self-improvement memory (`wm-sim`), and negative-knowledge failure capture (`post_tool_sim.py`). |
| **Alex Zhang / RLM (`@a1zhang` / `@himanshutwtxs`)**, **Anthropic `/dream`**, **`@mary_nekyia`**, **`@u1`**, & **`@Astronaut_1216`** | **Research inspiration** | Externalized corpus querying (`wm-local-digest`), offline consolidation (`wm-dream`), `AGENTS.md` hygiene auditing, and Planner/Executor routing (`agy-mode`). |

**Original to `whitemagic-agy-kit`:** `wm-squeeze v3` (clean-room zero-dependency Rust `--safe` `execvp` compressor + git content-addressed cache), `wm-codegraph v3` (clean-room zero-dependency Rust incremental symbol/call-graph CLI + 1-tool MCP), `wm-handoff` (CliffCompaction verbatim crystalizer), unified `PreToolUse` `overwrite` guard, zero-jitter `SessionStart` + `wm-sim prefix-hash` (`ContextCacheToken`), cache-warmth `statusline.py`, `stop_telemetry.py`, `agy-bench --credits`, `agy-mode`, `wm-local-digest`, `wm-dream`, cross-platform `install.sh`, 34-test verification suite, and the progressive 4-level benchmark harness (`benchmarks/run_progressive_bench.py`).

## License

MIT (see bundled upstream skills for their respective upstream licenses).
