# WhiteMagic AGY Turbo Kit (`whitemagic-agy-kit` v4.5.0 — Rust `PreToolUse`, Ping-Pong Auto-Crystal & Orchestrator Guards)

Cross-platform (**macOS Apple Silicon/Intel + Linux**) Antigravity App 2.0, Antigravity IDE, and CLI (`agy 1.5.14+`) optimization suite fusing `whitemagic-agy-kit` and `INK-Q/agy-fast` (MIT) into a single unified plugin — **works standalone with zero external dependencies** (plus optional WhiteMagic Gen3 & Tricameral integrations):
- **Rust `PreToolUse`, Auto-Crystal & Swarm Guards (`v4.5.0`):** Compiled zero-dep Rust `wm-squeeze --pre-tool` hot path (**`2.8–4.8 ms`** median, **`31.2–46.5×`** faster than Python with `100%` parity); `>=160k` ctx `<=100L` `read_file` clamp; `/swarm` & `invoke_subagent` cost guards (`fork` block at `>=80k`, omitted-model Neon guard, `>6 KB` brief / `>4 KB` board bloat guard, `research` inherit-on-Neon guard); safe `python3 tests/test_*.py` / `bench_*.py` & read-only `for` loop squeezing; `Orchestrator Ping-Pong Alert` (`>=2` flips) + safe `fullyIdle` `Stop` auto-crystal (`wm-handoff --emit-receipt` canonical `wm-receipt/1.2` + optional HMAC-SHA256) + `wm-reset` fresh session launcher; `wm-handoff --swarm-map` (`<=600` tok shared codemap); **3 native plugin agents** (`hydrogen-builder`, `fresh-verifier`, `swarm-coordinator`); and `agy-token-audit` orchestration cadence / cross-session dup-read telemetry.
- **Self-Healing & Sub-3ms Rust `--post-tool` (`v4.4.2`):** Automatic `PreToolUse` recovery of inverted/line-count `read_file` ranges (`10/10` historical failures self-healed) and `[Path Hint]` sibling/parent suggestions on missing files; compiled zero-dep Rust `wm-squeeze --post-tool` (`1.6 ms` median, `24.2×` faster than Python); lazy-import `pre_tool_guard.py` fallback (`-25.1%` `read_file` latency); windowed legacy-filtered `wm-dream` (`stops` vs `spills`); word-boundary-safe `session_start_brief.py`; and quoted-`cd`-with-spaces heredoc, backslash-newline continuation, and read-only `xargs` spill capture (`1,397` squeezed, `+171.8%` vs `v4.3.0`).
- **Compound Spill Shield & Rust HUD (`v4.4.1`):** Compound/multiline read-only bash, uncapped pipeline, and complex heredoc (`cd ... && python3 - <<'PY'`) `wm-squeeze --pipe` wrapping (zero false `SIGPIPE` via `(set +o pipefail ...)`), compiled zero-dep Rust `wm-squeeze --hud` `PostInvocation` hook with idle-gap attribution, JSON-encoded `transcript.jsonl` arg unwrapping across companion CLIs, and disabled-plugin rule filtering in `wm-sim audit`.
- **App/IDE Parity & Longevity Shield (`v4.4.0`):** Quote-aware `PreToolUse` `wm-squeeze` (`+38.6%` more commands squeezed across `3,497` real commands, eliminating quoted `rg`/`find` bypasses), standalone `python3 -c` `wm-squeeze --pipe` wrapping, wide-line JSON/JSONL `<=50L` `read_file` clamping, Firecrawl/Research Engine MCP auto-budgeting, zero-token `PostInvocation` App/IDE Longevity HUD (`0` model tokens), multi-surface (`app`+`ide`+`cli`) companion CLIs, hardened Node/MCP wiring, deduplicated `AGENTS.md` (`-39.1%` vs `v4.3.0`), and [`benchmarks/RESULTS_2026-11-25_v44.md`](./benchmarks/RESULTS_2026-11-25_v44.md).
- **Token-Burn Shield (`v4.1.0`):** Forensic `agy-token-audit` CLI (`--recent`, `--conv`, `--swarm`, `--report-md`), wide-span & burst `read_file` auto-clamping (`>250L` / `150L` above `120k` ctx / `120L` burst), multiline inline `python3 - <<'PY'` heredoc `wm-squeeze --pipe` auto-wrapping, portable `[Env-Write Guard]` (`force_ask` on `.env*` secrets, fixing AGY sandbox relative deny-rule errors), and automatic user/plugin skill deduplication.
- **5-Tool Eager Stdio MCP (`whitemagic-agy-kit_fast`):** `batch_search` (auto-collapses lockfile diffs), `apply_edits` (safe unique line-drift auto-relocate + trailing-whitespace unique fallback + `wm-squeeze` inline `verify`), `read_slices`, `run_checks` (CoACT `wm-squeeze` compression + 300s git-aware cache in `compact` mode), and `code_graph` (`wm-codegraph` AST/call-graph).
- **Unified Zero-Jitter `SessionStart` (`session_start_brief.py`):** Single `<2.6 KB` Turn-0 digest combining cached preflight brief + SIM ledger + Dream digest + compact workspace snapshot (suppressing `$HOME` noise).
- **Deterministic `PreToolUse` Guards, `wm-squeeze v3`, `wm-handoff` + `wm-reset`, `Tricameral CyberBrain`, & SIM Memory.**

Built from empirical offline forensics across `50` real sessions (`12,740` turns, `2.27B` input tokens), controlled 4-level live `agy -p` A/B benchmarks ([`benchmarks/RESULTS_2026-11-21.md`](./benchmarks/RESULTS_2026-11-21.md)), and live 19-session `/swarm` before/after telemetry ([`benchmarks/AGY_SWARM_TOKEN_TELEMETRY_REPORT_2026-11-23.md`](./benchmarks/AGY_SWARM_TOKEN_TELEMETRY_REPORT_2026-11-23.md)), designed strictly within Antigravity's documented extension interfaces (`plugin.json`, `hooks.json`, `mcp_config.json`, `rules/AGENTS.md`, `agents/*.md`, `statusLine`, and `--model` / `--settings`).

---

## Headline Empirical Results ([v4.5.0 Benchmarks](./benchmarks/RESULTS_2026-11-25_v450.md) · [v4.4.2 Benchmarks](./benchmarks/RESULTS_2026-11-25_v442.md) · [v4.4.1 Benchmarks](./benchmarks/RESULTS_2026-11-25_v441.md) · [v4.4.0 Benchmarks](./benchmarks/RESULTS_2026-11-25_v44.md) · [v3/Wave4 Benchmarks](./benchmarks/RESULTS_2026-11-21.md) · [v4.1.0 Swarm Telemetry](./benchmarks/AGY_SWARM_TOKEN_TELEMETRY_REPORT_2026-11-23.md))

- **Rust `PreToolUse`, Ping-Pong Auto-Crystal & Orchestration Replay (`v4.5.0`, [RESULTS_2026-11-25_v450.md](./benchmarks/RESULTS_2026-11-25_v450.md)):**
  - **Rust `wm-squeeze --pre-tool` vs Python (`30` runs each, exact parity=`True`):** `read_file` **`2.8 ms`** vs `116.5 ms` (**`41.4×`**), `run_command` **`3.2 ms`** vs `147.2 ms` (**`46.5×`**), `swarm_add` **`3.6 ms`** vs `134.4 ms` (**`37.8×`**), `mcp_noop` **`4.8 ms`** vs `148.1 ms` (**`31.2×`**).
  - **19-Session Orchestration & Auto-Crystal Corpus Replay:** Identified **`214` cross-model flips (`38.97M` uncached tokens burned)** across `10/19` ping-pong sessions, `11/19` auto-crystal eligible sessions (`wm-receipt/1.2`), `110` `exit_plan_mode` transitions (`22` 1-turn Neon plans), and `86` cross-session duplicate read paths; `wm-handoff --swarm-map .` emits a SHA-256-pinned codebase map in **`2,235 B` (~`558` tokens)**.
  - **Full Regression & Validation:** `tests/test_wave3.py` passes **`198/198`** twice idempotently (`2×` clean), `agy plugin validate plugin --strict` passes (`skills 8 · agents 3 · rules 1 · hooks 5 · mcpServers 1`), and live `agy --model gemini-4-hydrogen-high --effort high -p ...` passes (`V450_OK`).
- **Self-Healing, Sub-3ms Rust Hooks & 4-Way Replay (`v4.4.2`, [RESULTS_2026-11-25_v442.md](./benchmarks/RESULTS_2026-11-25_v442.md)):**
  - **4-Way 22-Session Replay (`3,370` `run_command` calls):** `514` (`v4.3.0`) → `724` (`v4.4.0`) → `1,322` (`v4.4.1`) → **`1,397` (`v4.4.2+`, `+171.8%` vs `v4.3.0`)** commands auto-squeezed; historical spill capture climbed `42/219 → 55/219 → 114/219 → 119/219` (**`54.3%`**).
  - **`read_file` Self-Heal & Latency:** **`10/10` (`100%`)** historical `invalid line range` crashes self-healed; Rust `wm-squeeze --post-tool` runs in **`1.6 ms` median (`3.0 ms` p95)** vs Python `39.6 ms` (**`24.2×` faster**); Rust `wm-squeeze --hud` runs in **`3.6 ms` median (`6.4 ms` p95)** vs Python `98.1 ms` (**`27.5×` faster**, `0` model tokens, `2.72%` alert rate across `6,986` turns).
- **Live `agy -p` & `v4.4.x` Compound Verification:** `v4.4.0` A/B cut wall-time **`-29.9%`** (`28.68s → 20.11s`) & spills **`1 → 0`**; `v4.4.1` multiline compound smoke test rewrote & ran cleanly (`rc=0`, `0` spills, `0` hook errors).

- **Live 19-Session `/swarm` Before vs. After (`v4.1.0` Token-Burn Shield, `agy-token-audit --swarm`):**
  - **Avg uncached input / worker:** `0.57M → 0.175M` (**`-69.4%`**) · **Avg total input / worker:** `8.99M → 1.89M` (**`-79.0%`**) · **Avg output / worker:** `111.5k → 52.6k` (**`-52.8%`**)
  - **Peak single-turn uncached spike:** `384.2k → 34.7k` (**`-91.0%`**) · **Auto-compactions:** `4 → 0` (**`-100%`**) · **Cross-model flips:** `14 → 0` (**`-100%`**)
  - **Verifier head-to-head (identical 6-check rigor):** `66 → 11` turns (**`-83.3%`**), `0.90M → 0.10M` uncached (**`-88.9%`**), `16.1M → 0.66M` total input (**`-95.9%`**)

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

## What's Included (Core Architecture — v4.5.0)

1. **`wm-squeeze` — Zero-Dependency Rust CoACT CLI Compressor, Cache & Lifecycle Engine (`tools/wm-squeeze/`):**
   - Single stripped `std`-only Rust binary wired via `PreToolUse`, `PostToolUse`, and `PostInvocation` (`2.8–4.8 ms` `PreToolUse`, `1.6 ms` `PostToolUse`, `3.6 ms` `PostInvocation`).
   - Compresses allowlisted commands via `--safe` (`execvp`, no `sh -c`) and wraps safe compound/multiline/read-only `for` loops, `xargs`, `python3 -c`, `python3 tests/test_*.py`, and quoted-`cd` Python heredocs (`--pipe`).
   - Content-addressed git-aware result cache (`300s` TTL) prevents build/test wait gaps from evicting Gemini 4's prefix cache.
2. **`wm-codegraph` — Zero-Dependency Rust Incremental Symbol & Call-Graph Engine (`tools/wm-codegraph/`):**
   - Indexes Rust, Python, TS/JS, Go, C/C++, and Shell incrementally (`~/.cache/wm-codegraph/`).
   - Runs as CLI (`wm-codegraph overview|defs|callers|callees|impact|skeleton <target>`) or inside the 5-tool eager MCP (`code_graph`).
3. **Deterministic `PreToolUse` Safety, Self-Healing & Swarm/Anti-Bloat Engine (`wm-squeeze --pre-tool` + `plugin/hooks/pre_tool_guard.py`):**
   - Data-Loss & `.env*` secret guards (`force_ask`), **self-healing `read_file` line ranges**, `[Path Hint]` sibling/parent suggestions, context-aware `read_file` auto-clamp (`>250L`, `<=150L >=120k`, `<=100L >=160k`, `<=50L` wide JSON/JSONL), duplicate-read dedup (`deny`), `>=150k` multimodal guard, `/swarm` & `invoke_subagent` cost/fork/bloat guards, and MCP auto-budgeting.
4. **`wm-handoff` & `wm-reset` (*CliffCompaction* Auto-Crystal & Clean Session Reset, `bin/wm-handoff`, `bin/wm-reset`):**
   - Multi-surface (`--surface all|app|ide|cli`) verbatim SHA-256-pinned `<1.8k`-tok handoff crystals (`wm-receipt/1.2` + optional HMAC-SHA256), `--swarm-map` (`<=600` tok shared codemap), automatic `fullyIdle` `Stop` crystal generation on ping-pong / `>=130k` sessions, and 1-command `wm-reset` pinned session relaunch.
5. **Native Plugin Agents (`plugin/agents/`), Zero-Jitter `SessionStart`, `statusLine`, Telemetry & Multi-Surface CLIs:**
   - Preconfigured `hydrogen-builder`, `fresh-verifier`, and `swarm-coordinator` agents; `agy-token-audit` orchestration & cross-session read auditing; `agy-mode`; `wm-local-digest`; `wm-dream`; and `198`-check regression suite (`tests/test_wave3.py` + `81` `agy-turbo-mcp` TS tests).

---

## What's New in v3.1.0 (Portable Tricameral Edition)

- **macOS Apple Silicon + Gen3 Parity (PR #1):** Atomic `install_bin` (`cp` + `mv -f`) fixes Apple Silicon AMFI `SIGKILL` (`exit 137`) & Linux `ETXTBSY`; `wm-codegraph --mcp` honors `depth`; official `whitemagic-plugins` Gen3 (`wm-gen3` / `whitemagic_gen3`) auto-budgeting + eager promotion; `rm -fr` guard; `wm-local-digest` Ollama fallback; `wm-handoff --save-wm3`.
- **Vendored Safe-Rust Tricameral Engine (`tools/wm-tricameral-engine/` -> `muninn-engine`):** Cross-platform (`x86_64` AVX2 + `aarch64` Apple Silicon scalar fallback) `T-MAC` 4-bit LUT inference engine with a 24-entry **Unified Radix Cache (URC)** and zero-cold-load `--daemon <unix_socket>`.
- **Portable Sovereign Micro-Organ CLIs & 1-Tool Eager MCP (`wm-huginn`, `wm-muninn`, `wm-tricameral`, `wm-tricameral-mcp`):**
  - `Huginn-1 School-S3` (`S0`, ~122 MB RSS): `100%` 6-System score (`-93.8%` tokens) — 6D Galactic recall reranking, 12-token Sutra glyphs, `[UPD_ERASE]` state compression, `.hms` estate holograms, and `< 175 ms` `PreToolUse` Kekkai neural guard (100% fail-open when daemon is off).
  - `Muninn-4 School-v2-S3` (`S1`, ~357 MB RSS): `100%` 6-Layer Hippocampal score (`-97.4%` tokens) — episodic crystallization, Catuskoti conflict reconciliation, SIM auditing, `1.31 MB` `.hms` session handoff, and executable file-filter synthesis.
  - `tricameral_dispatch`: Single ultra-compact eager Stdio MCP tool wired automatically alongside `code_graph` (`-98.5%` / `66.25×` token reduction across 6 chained workflows).
- **Zero-Bloat Weight Registry (`models/manifest.json` + `wm-models`):** Keeps git `< 1 MB`; pulls `.tm7b` spores cleanly on Mac/Linux via HuggingFace (`HF_TOKEN` for private repos or public URLs) or `rclone` (`--source gdrive`).

---

## Standalone Quickstart & Upgrading (App, IDE & CLI — Zero WhiteMagic Required)

Works out-of-the-box for any Antigravity App 2.0, Antigravity IDE, or `agy` CLI user on **macOS** & **Linux** (Rust `cargo`/`rustc` optional for native binaries; Node >=20 optional for the 5-tool MCP):

```bash
# 1. Clone & install (automatically wires App, IDE & CLI via ~/.gemini/config/)
git clone https://github.com/lbailey94/whitemagic-agy-kit.git ~/.local/src/whitemagic-agy-kit
cd ~/.local/src/whitemagic-agy-kit
./install.sh --skip-tricameral-engine   # standalone AGY kit (omit flag to include Tricameral engine)

# 2. Upgrading later (1-command sync)
git pull && ./install.sh --skip-tricameral-engine

# Optional: pull local Tricameral micro-organ spores (~122 MB for Huginn, ~479 MB for both Ravens)
wm-models pull huginn            # set HF_TOKEN=... for private HF repo, or add --source gdrive
wm-models list
```

> **Turn-0 Context Tip:** Disable domain-specific marketplace plugins globally (e.g., `chrome-devtools-plugin`, `modern-web-guidance-plugin`, `google-antigravity-sdk` ≈ `~1,003` Turn-0 tokens) and re-enable them only in web/SDK repos via `.agents/plugins.json`.

Verify and benchmark offline or live:

```bash
python3 tests/test_wave3.py
python3 benchmarks/run_progressive_bench.py --track1
agy-token-audit --recent 10
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
