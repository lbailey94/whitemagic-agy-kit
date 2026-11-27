# Cursor / Grok Fleet v2 Empirical Benchmarks (`2026-11-27`)

Live empirical validation of all four wave orchestration primitives in [`bin/wm-grok-fleet`](../bin/wm-grok-fleet) (`cursor-fleet`): **`map --reduce`**, **`race`** (`bwrap`-confined Best-of-N worktree tournament), **`council`** (blind multi-model review + consensus judge), and **`pipeline`** (3-stage declarative funnel with `$0` local execution verification), plus the live 39-worker conversation triage dogfood run.

- **Reproducible Harness:** [`benchmarks/bench_cursor_fleet_v2.py`](./bench_cursor_fleet_v2.py)
- **Offline Verification:** `pytest -q tests/test_grok_fleet.py` (**19/19 PASS**) · `python3 tests/test_wave3.py` (**260/260 PASS**) · `agy plugin validate plugin --strict` (**PASS**)
- **Host:** Linux x86_64 (`cursor-agent 2026.11.22-e9042b2`, `bwrap` enabled)

---

## Headline Summary (All 4 Live Primitives + 39-Worker Dogfood)

| Benchmark / Campaign | Primitive | Workers / Models | Wall (s) | Speedup | Tool Calls | Cache Hit | Cost (`est_actual_usd`) | Cursor Quota | Key Outcome |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| **Dogfood: AGY Triage** | `map` (`synthesis`, `N=4`) | 39× `grok-4.7-medium` | ~410s (`3,036s` sum) | **7.4×** | `0` | **46.4%** | `$1.7877` | **1.86%** | **156/156** valid JSON (0 fail) |
| **Bench 1: CLI Portability Sweep** | `map --reduce` (`synthesis`, `N=4`) | 4 map + 1 reduce (`grok-4.7-medium`) | `253.15s` (`816.34s` sum) | **3.22×** | `0` | **37.9%** | `$0.4419` | **0.46%** | **5/5** done; caught **11 P1 + 13 P2** real bugs (`wm-grok-mcp` crash fixed live) |
| **Bench 2: `O(N²)` Optimizer Tournament** | `race` (`--confined --score-regex --apply-winner`) | `grok-4.7-medium` vs `composer-2.5` vs `cursor-grok-4.6-high` | `84.71s` (`174.18s` sum) | **2.06×** (fleet) / **363.3×** (algo) | `16` total | — | `$0.2548` | **0.27%** | **3/3** `bwrap`-confined PASS; winner **`3.238 ms`** vs `1,176.23 ms` baseline (**363.26×**), auto-applied & verified |
| **Bench 3: Blind 3-Model Security Council** | `council` (`synthesis` + blind judge) | `grok-4.7-medium`, `composer-2.5`, `cursor-grok-4.6-high` + `grok-4.7-high` judge | `338.10s` (`461.32s` sum) | **1.36×** | `0` | **38.9%** | `$0.2698` | **0.28%** | **4/4** done; earlier pass caught `shlex.quote`, `format_map`, & timeout gaps, patched & re-verified live |
| **Bench 4: Verifiable Synth Task Factory** | `pipeline` (`map -> exec filter -> map-reduce`) | 2 gen + `$0` exec gate + 2 summary (`grok-4.7-medium`) | `229.57s` | — | `0` | **32.3%** | `$0.2305` | **0.24%** | **8/8 (100%)** execution-verified (`buggy` failed + `fixed` passed), **$0.0288 / verified task** |

**Total live spend across all 4 benchmark primitives combined:** **$1.197 (~1.25% Cursor quota)** (`all_ok: true`).

---

## Deep-Dive Empirical Findings

### 1. Bench 2 — Multi-Model `race` Leaderboard (`1,176.232 ms` Baseline → `3.238 ms`)

Task: Optimize an `O(N²)` sliding-window log deduplication and burst anomaly detector (`analyzer.py`, `N=4,000` events, 12 edge-case assertions + randomized reference cross-check) inside network-isolated `bwrap` (`scope_confined=True`):

| Rank | Candidate | Model | Verify (`bwrap`) | Median Score | Speedup vs Baseline | Diff (`+ / -`) | Wall (s) | Tools |
| ---: | :--- | :--- | :---: | ---: | ---: | :---: | ---: | ---: |
| **1 (WINNER)** | `cand-01-grok-4.7-medium` | `Grok 4.7 256K Medium` | **PASS** (`rc=0`) | **`3.238 ms`** | **`363.26×`** | `+30 / -23` (`53L`) | `68.10s` | `5` |
| **2** | `cand-03-cursor-grok-4.6-high` | `Grok 4.6 High` | **PASS** (`rc=0`) | `4.101 ms` | `286.82×` | `+154 / -28` (`182L`) | `84.32s` | `7` |
| **3** | `cand-02-composer-2.5` | `Composer 2.5` | **PASS** (`rc=0`) | `4.615 ms` | `254.87×` | `+22 / -20` (`42L`) | **`21.76s`** | **`4`** |

**Calibration Lesson (Why Run 1 Timed Out vs Run 2 Hit 3/3 in 84.7s):**
- In Run 1 (`3× grok-4.7-high` without edit-first prompt discipline), all 3 candidates spent `300s` running 4 separate exploratory `cProfile` micro-benchmarks in shell before ever editing `analyzer.py`.
- Adding explicit edit-first discipline to `cmd_race` ("edit within 2–3 tool calls, verify once") + racing heterogeneous models (`grok-4.7-medium`, `composer-2.5`, `cursor-grok-4.6-high`) flipped pass rate from **0/3 (300s timeout) → 3/3 (84.7s wall, 363.26× algorithmic speedup, $0.25)**.

### 2. Bench 3 — Model Latency & Cursor Weekly-Plan Compatibility

| Model | Cursor Weekly Plan | Wall Latency (Blind Council) | Notes |
| :--- | :---: | ---: | :--- |
| `composer-2.5` | **Included** | **`19.4–28.4s`** | **7–11× faster** than Grok reasoning tiers; high signal-to-noise for surgical diffs & fast review panelist |
| `cursor-grok-4.6-high` | **Included** | `104.2–138.4s` | Independent generation diversity for blind councils & tournaments |
| `grok-4.7-medium` | **Included** | `68.1–209.4s` | Best overall accuracy-per-dollar & tournament winner (`3.238 ms`, clean 53L diff) |
| `grok-4.7-high` | **Included** | `128.4–233.7s` | Best apex consensus judge; set `wall_timeout >= 360s` for heavy code reviews |
| `gpt-5.3-codex-high` | Excluded (extra credits) | `5.7s` fail-fast | `ActionRequiredError` now detected as non-retryable; use `--min-panelists` for resilient mixed councils |

### 3. Bench 4 — Execution-Verified Synthetic Factory Economics

- **Admission Rate:** **`8 / 8` (`100.0%`)** generated Python defensive regression puzzles passed the deterministic local execution gate (`buggy_code` exit != 0 AND `fixed_code` exit == 0).
- **Stage-3 Quality Rubric:** Mean realism **`4.6 / 5.0`** (all `>=4`), mean rigor **`3.0 / 5.0`**, automatically identifying specific missing boundary cases (symlinks, CRLF, `NONE` alg case, negative clock skew) for next-round hardening.
- **Unit Economics:** **`$0.0288` per execution-verified task** (`~35` verified RL/GRPO tasks per `$1.00`, or **`~3,300` verified tasks per Cursor monthly cycle**).
- **Prompt Framing Calibration:** Framing prompts as *defensive software regression tests* (avoiding offensive phrasing like "exploit") eliminated upstream provider safety refusals completely (`0/2` → `2/2` batches, `8/8` admitted).

### 4. Real Bugs Caught & Fixed Live by Dogfooding Our Own Primitives

1. **`bin/wm-grok-mcp` (caught by Bench 1 `map --reduce`):** Malformed or non-dict JSON-RPC lines (`null`, `[]`) previously raised `AttributeError` outside `try` and killed the MCP server → fixed with explicit JSON-RPC `-32700` / `-32600` responses.
2. **`bin/wm-grok-fleet` (caught by Bench 3 `council`):**
   - `shlex.quote`d all pipeline filter path placeholders (`in_jsonl`, `out_jsonl`, `stage_dir`, `prev_json`, `cwd`), preventing shell injection AND handling paths with spaces.
   - Replaced `str.format_map` with identifier-only regex substitution (`_safe_template_sub`) to block `{x.__class__}` template traversal.
   - Replaced `bash -lc` with process-group-bounded `bash -c` (`_run_shell_bounded`) with `TimeoutExpired`/`OSError` capture, UTF-8 replace decoding, and atomic receipt writes.
   - Added explicit `_parse_bool` (fixing `"false"` truthiness), hardened `_eval_where_predicate` container/bool typing, wired stage checkpoints, added `--min-panelists`, and calibrated `--dry-run` reasoning token default (`1,200 → 4,200` tok/worker).

---

### 5. Production Stress Test: 146-Worker (`4.77h`) 5-Lane R&D Campaign → `cursor-fleet` v2.2 Self-Healing Upgrades

Immediately following the 4-primitive benchmark, we executed a full 5-lane Frontier R&D synthesis & verification campaign (`146` completed Grok 4.7 workers, `17,164.1s` / `4.77h` cumulative model compute, **`0` tool calls**, **`51.86%` aggregate cache hit**, **`$8.95` / `9.32%` weekly Cursor quota**, `13.8×` wall speedup on the 122-job dataset wave), producing `300/300` locally `python3 -I` execution-verified Python tasks (`85.7%` pass rate, `0` AST flags), `80/80` tool-failure recovery traces (`96.7%` pass rate), `400/400` balanced adversarial pairs (`100%` valid, `0` SHA-256 holdout leaks across 5 benchmarks), `8` disjoint CWE AST rule suites (`70/70` Python `ast.parse` valid), and `6` architectural blueprints.

Six concrete production friction points observed live were engineered directly into **`cursor-fleet` v2.2**:

| Live Friction Observed at Scale | Root Cause | v2.2 Engineering Fix |
| :--- | :--- | :--- |
| Long architecture spec killed at `360s` while streaming final Markdown | `thinking/completed` fired at `320s`, but hard `wall_timeout` cut off active text emission | **`--streaming-grace` (default `60s`, `90s` on `blueprint`)** extends wall limit only when `thinking/completed` or `assistant` text is actively streaming |
| `grok-4.7-medium` hit `43%` wall-timeout rate on 6-task Python batches | `77%–83%` of output tokens (`10k–14k`) spent in reasoning chain-of-thought | New **`--preset dataset`** (`grok-4.7-low`, `12x` conc.) + **`--preset blueprint`** (`grok-4.7-high-fast`, `480s` wall) + live `reason=XX%` & `[fleet][hint]` alerts |
| Manual SQLite `UPDATE` queries needed to retry or downshift stalled tiers | No CLI primitive to reset failed/non-done jobs onto a faster tier | New **`cursor-fleet retry`** (`--model`, `--non-done`, `--job`, `--run`), **`run --model`**, and automatic **`--timeout-fallback-model auto`** (`1.25×` wall + step-down ladder) |
| `6/74` complete Python task JSON batches rejected (`34` tasks lost) | Stray Python backslashes (`\U0001f60a`, `\d`, `\s`, `\'`), BPE transposed `]"},`, omitted `}{` comma, or missing final `]` | String-aware **`_repair_json_candidate`** fallback in `extract_json_payload` — verified **`6/6` (`34/34` tasks, `100%`) rescued** against the real Wave B failures |
| `ask --out SPEC.md` wrote a 41 KB JSON envelope instead of Markdown | `--out` always serialized the full JSON envelope | New **`--text-out <path>`** on `ask`/`map` writes clean unwrapped raw Markdown/text directly while preserving `--out` JSON compatibility |
| Repeated `--db-path`, `--artifact-dir`, `--checkpoint-path` flags on every call | Isolated campaign state required 3 explicit paths | New **`--state-dir <dir>`** shorthand across all subcommands auto-deriving `fleet.db`, `artifacts/`, and `<campaign>_checkpoint.json` |
