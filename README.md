# WhiteMagic AGY Turbo Kit (`whitemagic-agy-kit`)

Cross-platform (**macOS Apple Silicon/Intel + Linux**) Antigravity CLI (`agy`), Desktop 2.0, and IDE optimization suite built from empirical transcript benchmarking (`30` sessions, `6,589` turns, `13,549` tool calls) and Nov 2026 agent-harness research.

## What's Included

1. **Native Antigravity Plugin (`plugin/` — passes `agy plugin validate --strict`):**
   - **`rules/AGENTS.md`**: High-priority context hygiene (`@leoshen0` `-43%` cost / `-76%` uncached-input rules: narrow search before read, `<=3` uncertain files/turn, `<=200`-line slices, shell output throttling, RLM external-context discipline, `>150k` multimodal subagent isolation, Gemini 4 implicit cache discipline, and strict no-clobber data preservation).
   - **`hooks.json` + fast `python3 -S -B` hooks (`~20–26 ms`)**:
     - `SessionStart`: injects compact cached preflight brief (when present) + top SIM lessons.
     - `PreToolUse`: enforces data-loss guard (`force_ask`) across `run_command`, `mcp_agy-fast_fast_run_checks`, and `mcp_agy-fast_fast_apply_edits` (Linux + macOS `diskutil`).
     - `PostToolUse`: zero-overhead lazy-import secret-redacted failure capture into SIM ledger.
   - **4 Portable Custom Skills**:
     - `agent-orchestration-patterns` (5 Google Cloud `run_workflow` scripts + Gemini 4 cache guide)
     - `self-improvement-memory` (`wm-sim` continual harness memory)
     - `inference-optimisation` (ceiling-first interleaved benchmarking — first-class **Apple Silicon MLX** + Linux/CUDA/CPU support)
     - `colab-operator` (headless Google Colab GPU/TPU orchestration)
2. **Hardened `agy-fast` (`v0.2.8`, `--auto-update off`)**: Eager 4-tool local Stdio MCP (`batch_search`, `apply_edits`, `read_slices`, `run_checks`) + `<85 ms` `SessionStart` workspace snapshot.
3. **Plugin Prompt Bloat Pruning (`-91.9%` / `~10.4k` tokens/session saved)**: Disables unused heavy official plugins (`science`, `data-agent-kit-plugin`, `flutter`, `firebase`, `google_maps_platform`, `android-cli-plugin`), 100% reversible via `agy plugin enable <name>`.
4. **Portable CLI & TUI Utilities (`bin/`):**
   - `wm-sim`: Self-Improvement Memory CLI + automated rule-duplication / plugin-bloat / disk hygiene auditor.
   - `agy-bench`: Offline transcript A/B benchmark CLI (`agy-bench --save --label baseline` → `agy-bench --compare`).
   - `statusline.py`: `<25 ms` live TUI status bar showing model/effort, color-coded context cliff (`120k`/`180k`), active subagents, git branch, and root disk free.

## Quickstart (macOS & Linux)

```bash
# Optional macOS prerequisites for maximum speed:
# brew install node ripgrep

git clone https://github.com/lbailey94/whitemagic-agy-kit.git ~/.local/src/whitemagic-agy-kit
cd ~/.local/src/whitemagic-agy-kit
./install.sh
```

Ensure `~/.local/bin` is on your `PATH` (add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` on macOS), then launch a fresh `agy` session.

## Measuring Your Speedup (100% Offline)

```bash
# Save baseline before/right after install:
agy-bench --recent 30 --save --label pre_kit

# After 5–10 sessions with the kit active:
agy-bench --since 2026-11-21T12:00:00-05:00 --split-usage --save --label post_kit
agy-bench --compare
```
