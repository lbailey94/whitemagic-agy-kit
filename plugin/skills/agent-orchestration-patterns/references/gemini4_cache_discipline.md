# Gemini 4 Implicit Prefix-Cache & Quota Discipline

Empirical telemetry (`2,031` API calls across `195` sessions) and Gemini 4 caching semantics:
- Gemini 4 relies on **implicit prefix caching** (no explicit TTL cache objects).
- On **Gemini 4 Neon & Carbon**, prompt cache hit rates stay around `~85%` when inter-turn gaps stay `<15s`, drop to `~33.6%` past `30s`, and fall to `~9%` after `1–2 minutes`.
- **Gemini 4 Hydrogen** maintains `~90.9%` cache hits across longer gaps.

## Practical Rules for Antigravity CLI (`agy`)

1. **Match model tier to turn cadence:**
   - Use **Hydrogen (XHigh)** for long investigative sessions, multi-minute builds, or slow external waits.
   - Use **Neon / Carbon** for tight, high-velocity coding bursts where each step completes in `<15s`.
2. **Batch independent tool calls in a single step:**
   - Issue parallel `read_file`, `search_web`, and chained shell commands in one planner step instead of serial 1-call turns.
3. **Never block foreground turns past ~10s:**
   - Background heavy builds/benchmarks (`WaitMsBeforeAsync: 1000–5000`) and park cleanly with reactive wakeup or `schedule` instead of polling loops.
4. **Keep prefixes immutable:**
   - Inject startup state once via `SessionStart` (`~/.gemini/config/hooks.json`), never rewrite global `GEMINI.md` mid-session, and keep `GEMINI.md` concise (`wm-sim audit`).
