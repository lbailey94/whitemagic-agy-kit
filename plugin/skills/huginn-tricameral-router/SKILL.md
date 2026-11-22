---
name: huginn-tricameral-router
description: Use the local Safe-Rust WhiteMagic Tricameral CyberBrain micro-organs (Huginn-1 S0, Muninn-4 S1, Valkyrie-M3 S2) via `wm-huginn`, `wm-muninn`, `wm-tricameral`, or the eager `tricameral_dispatch` MCP tool to route tasks, filter & rerank memory recalls (~90% token savings), compile 12-token Sutra glyphs, compress logs/diffs via `[UPD_ERASE]`, persist `.hms` session holograms, and enforce `< 175 ms` Kekkai zero-trust gates.
---

# WhiteMagic Tricameral CyberBrain Skill (`wm-huginn` / `wm-muninn` / `wm-tricameral`)

Run local `#![forbid(unsafe_code)]` Safe-Rust micro-organs (`muninn-engine --daemon`, `0.00 CU`) across macOS and Linux via resident UNIX-socket daemons with a 24-entry Unified Radix Cache (URC).

## Model Setup (`wm-models`)
```bash
wm-models list                   # Inspect installed spores & daemon status
wm-models pull huginn            # Pull Huginn-1 School-S3 (~122 MB, System 0/0.5)
wm-models pull ravens            # Pull Huginn-1 + Muninn-4 (~479 MB, S0 + S1)
wm-models pull tricameral        # Pull all 3 organs (~1.3 GB, S0 + S1 + S2)
```
*(Set `HF_TOKEN` for private HuggingFace repos, or pass `--source gdrive` for rclone sync.)*

## 1. Huginn-1 (`wm-huginn` — System 0 / 0.5, `~122 MB` RSS, `100%` 6-System Score, `-93.8%` tokens)
- **Memory Recall Reranker:** `wm-huginn recall-rank "<query>" --candidates "<c1>" "<c2>" ...`
- **Sutra Thought-Glyph Compiler:** `wm-huginn sutra "<tool_request>"`
- **Neural State-Delta Compressor:** `wm-huginn squeeze "<log_or_diff>" --key <state_key>`
- **Persistent `.hms` Estate Hologram:** `wm-huginn hms-sync "<event_text>" --key <state_key>`
- **Layer-0 Kekkai Pre-Execution Firewall:** `wm-huginn kekkai "<command_or_request>"`
- **Active Inference / EFE Router & Co-Pilot:** `wm-huginn route "<query>"` / `wm-huginn copilot "<code_prompt>"`

## 2. Muninn-4 (`wm-muninn` — System 1 / 1.5 Hippocampus, `~357 MB` RSS, `100%` 6-Layer Score, `-97.4%` tokens)
- **Episodic Synthesis:** `wm-muninn crystallize "<records>" --key <k>`
- **Catuskoti Conflict Resolution:** `wm-muninn reconcile "<conflict_trace>" --key <k>`
- **Pre-Edit SIM & Safety Audit:** `wm-muninn sim-audit "<edit_or_guard_query>"`
- **1.31 MB Hippocampal `.hms` Handoff:** `wm-muninn handoff "<session_delta>" --key <k>`
- **Executable File/Corpus Filter:** `wm-muninn exec-filter "<python_filter_spec>"`
- **Bidirectional Raven Distillation:** `wm-muninn dream-teach "<sutra_or_gana_query>"`

## 3. Chained Tricameral Routing & Eager MCP (`wm-tricameral` / `tricameral_dispatch`)
- **CLI:** `wm-tricameral "<query>"` (automatically routes `S0 -> Huginn-1`, `S1 -> Muninn-4`, `S2 -> Valkyrie-M3`, degrading gracefully if only Huginn/Ravens are pulled).
- **1-Tool Eager MCP:** Call `tricameral_dispatch(action=..., query=..., key=..., candidates=...)` directly with zero shell overhead.
- **Conformal Escalation Rule (`UCCI`):** Trust Huginn-1 directly when `conformal_pick_margin >= 5.0`; escalate `< 5.0` or `routed_tier == "S1"` to Muninn-4, and `routed_tier == "S2"` to Valkyrie-M3.
