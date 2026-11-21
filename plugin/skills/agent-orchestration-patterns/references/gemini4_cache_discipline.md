# Gemini 4 Implicit Prefix-Cache & Credit-Longevity Discipline

Empirical telemetry across **40 local Antigravity CLI sessions (`9,221` turns, `1.81B` input tokens)** + `@bebuu_ai` (`2,031` calls):
- Gemini 4 uses **implicit prefix caching** (no explicit TTL cache objects).
- **Per-Model Empirical Longevity (40 local sessions):**
  - **Gemini 4 Hydrogen (XHigh)**: **`94.7%` cache hit**, **`11.0k` uncached tokens/turn** — **3.15× better credit longevity** on mixed tool loops.
  - **Gemini 4 Argon (High)**: **`93.1%` overall**, holding **`97.7%` even across `30–60s` gaps**.
  - **Gemini 4 Neon (XHigh)**: **`86.8%` overall**, **`34.6k` uncached tokens/turn** (`94.9%` at `<15s` → `88.9%` at `30–60s` → `36.1%` at `>=180s`).
- **Inter-Turn Gap Cliff (all models, prior context `>=25k`):**
  - `<15s`: `95.6%` | `15–30s`: `94.8%` | `30–60s`: `85.2%` | `60–180s`: `83.5%` | `>=180s`: **`38.5%`** (`60.4%` miss rate).

## Practical Rules for Antigravity CLI (`agy 1.5.14+`)

1. **Match model profile to workload (`agy-mode`):**
   - `agy-mode longevity` (**Hydrogen XHigh**): long investigative sessions, multi-minute builds, or heavy R&D loops.
   - `agy-mode orchestrator` (**agy 1.5.14 Planner + Executor split**): frontier Planner paired with high-cache Executor (`/model` → `Orchestrator`).
   - `agy-mode deep` (**Argon High**): deep reasoning with 30–60s tool intervals.
   - `agy-mode turbo` (**Neon XHigh**): rapid `<15s` coding bursts.
2. **Batch independent tool calls & compress noisy shell outputs:**
   - Use `agy-fast` (`batch_search`, `apply_edits`, `read_slices`, `run_checks`) and let `PreToolUse` route noisy read-only shell commands through `wm-squeeze --safe` (`<8.2 KB` inline, zero `output.txt` spills).
3. **Never block foreground turns past `~10–15s`:**
   - Background heavy builds/benchmarks (`WaitMsBeforeAsync: 1000–5000`) and wait via reactive wakeup or `schedule`.
4. **Keep prefixes immutable & offload giant logs locally (`$0`):**
   - Inject startup state once via `SessionStart` (`<1.8 KB`), never rewrite `GEMINI.md` mid-session, isolate screenshots at `>=150k` ctx, and digest >20 KB logs locally via `wm-local-digest` (`:18898`).
