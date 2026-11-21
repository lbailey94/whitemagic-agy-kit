---
name: self-improvement-memory
description: >-
  Structured Self-Improvement Memory (SIM) workflow for Antigravity CLI and WhiteMagic R&D,
  grounded in arXiv:2611.18856 (SIM for Continual RSI), Harness2 (arXiv:2610.14224), and Model 801
  (No Second Run). Use before modifying harnesses, prompts, benchmarks, or training recipes to query
  past rationales and failure traces, after edits to record predicted vs observed outcomes, and
  periodically to audit GEMINI.md / AGENTS.md rule budgets and context hygiene.
---

# Self-Improvement Memory (`wm-sim`) & Harness Hygiene

Modern harnesses and training recipes fail silently when transferred across models or iterations
because the **rationale** and **observed failure context** behind past edits are lost.

## Core Loop (Propose → Probe → Record)

1. **Query before editing** (`proposer` & `selector` read past history grouped by `problem_group`):
   ```bash
   wm-sim query "<topic-or-subsystem>" --include-failures
   wm-sim failures --limit 10
   ```
2. **State a falsifiable prediction** before running an experiment or changing a harness, prompt,
   skill, or training hyperparameter.
3. **Record the outcome** immediately after verification (whether kept, reverted, or failed):
   ```bash
   wm-sim log \
     --domain "agy-cli" \
     --group "prompt-caching" \
     --target "~/.gemini/config/hooks.json" \
     --rationale "Move preflight brief injection to SessionStart so Turn 0 starts warm without mutating prefix caches." \
     --predicted "Zero cold-start subagent crawls and stable prefix cache across turns." \
     --observed "Verified SessionStart injects <1500B digest in <10ms." \
     --eval "hooks stdin/stdout test on agy 1.5.13" \
     --status kept \
     --model "gemini-4-neon"
   ```
4. **Distill into WhiteMagic Gen3** at milestone boundaries so Codex, Devin, and OpenCode share the same ledger:
   ```bash
   wm-sim sync-wm3
   ```

## Context & Rule Hygiene Audit

Stale or self-contradictory rules in `GEMINI.md` / `AGENTS.md` silently degrade coding agents over time.
Run the hygiene audit regularly (or after major skill/rule updates):
```bash
wm-sim audit
```
Checks:
- Per-file byte usage against Antigravity's `24,000 B` rule cap (`GEMINI.md`, `~/.gemini/GEMINI.md`)
- Duplicate rule files and JSON validity of `hooks.json` & `mcp_config.json`
- Active custom skills inventory and unsynced SIM records
