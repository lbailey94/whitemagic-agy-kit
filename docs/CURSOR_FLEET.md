# Cursor CLI / Grok 4.7+ Fleet Runner (`cursor-fleet` / `wm-grok-fleet`)

A **zero-dependency Python 3.10+ stdlib** parallel orchestrator, map-reduce runner, worktree patch factory, and 1-tool MCP server (`wm-grok-mcp`) for anyone using **Cursor CLI (`cursor-agent`)** on **macOS or Linux**.

Built from empirical calibration across live multi-worker Grok 4.7 campaigns:
- **18.8× fewer input+cache tokens** and **4.1× lower cost** (`~$0.05/worker`, ~`0.05%` Cursor quota) in zero-tool pure-synthesis mode (`--preset synthesis`: isolated `--empty-cwd` + single-turn `--no-tools-guard` + `--max-tool-calls 0`).
- **2× quota savings** by defaulting to standard non-fast `grok-4.7-medium` / `grok-4.7-high` (matching `*-fast` F1=1.00 accuracy at half the Cursor quota burn).
- **Crash-safe & quota-safe SQLite WAL queue**: survives Ctrl+C, crashes, `429` bursts (shared exponential backoff), low host RAM/disk (cross-platform Linux + macOS governor), and Cursor quota limits (auto-pauses pending jobs cleanly for next-cycle resume).
- **Zero WhiteMagic / Antigravity / Rust / Node dependencies** in standalone mode.

---

## 60-Second Quickstart

Prerequisites: Python 3.10+ and Cursor CLI (`cursor-agent`).

```bash
# 1. Install Cursor CLI & sign in (skip if you already use cursor-agent)
curl https://cursor.com/install -fsS | bash
cursor-agent login

# 2. Install ONLY the standalone Cursor Fleet tools (~1 second, no Rust/Node needed)
git clone https://github.com/lbailey94/whitemagic-agy-kit.git ~/.local/src/whitemagic-agy-kit
cd ~/.local/src/whitemagic-agy-kit
./install.sh --cursor-fleet-only           # add --wire-cursor-mcp to also register MCP in ~/.cursor/mcp.json

# 3. Verify auth, available models, host headroom, and presets
cursor-fleet doctor                        # `cursor-fleet` is an alias for `wm-grok-fleet`
```

Make sure `~/.local/bin` is on your `PATH`.

---

## Efficiency Presets (`--preset`)

| Preset | Action | Default Model | Tool Budget | Best For |
| :--- | :--- | :--- | :--- | :--- |
| `synthesis` | `ask` | `grok-4.7-medium` | `0` (`empty_cwd` + no-tools guard) | Map-reduce triage, corpus/log/transcript digestion, batch JSON classification (**18.8× cheaper**) |
| `fast` | `ask` | `grok-4.7-medium-fast` | `0` (`empty_cwd` + no-tools guard) | Interactive latency-sensitive pure synthesis (~2× faster wall, ~2× quota) |
| `audit` | `ask` | `grok-4.7-high` | `15` | Read-only codebase/security inspection across a repo |
| `plan` | `plan` | `grok-4.7-high` | `15` | Architecture second opinions & refactor plans |
| `patch` | `worktree` | `grok-4.7-high` | `40` | Parallel isolated git-worktree edits + automatic `.patch` harvest & auto-prune (add `--confined` for Linux `bwrap` verification) |

---

## 5 Copy-Paste Workflows

### 1. One-Shot Parallel Fan-Out (`ask`)

```bash
cursor-fleet ask --preset synthesis \
  "Compare SQLite WAL vs DuckDB for a 500MB local analytics cache in 5 bullets." \
  "Design a minimal RFC 9421 webhook verifier in Python stdlib." \
  --report-md /tmp/quick_answers.md
```

### 2. Parallel Map-Reduce Over Repo Files (`map --files`)

Preview batch count, estimated tokens, and estimated Cursor quota **before spending anything** with `--dry-run`, then execute:

```bash
# Step A: Free dry-run cost estimate (N=4 items/worker is the empirical Grok 4.7 sweet spot)
cursor-fleet map \
  --preset synthesis \
  --files "src/**/*.py" \
  --batch-size 4 \
  --instruction "Return a JSON array of {file, severity, issue, line_hint} for real bugs or race conditions only." \
  --dry-run

# Step B: Execute with hard USD cap + crash-safe campaign ID + merged JSON & Markdown reports
cursor-fleet map \
  --campaign repo-bug-sweep \
  --preset synthesis \
  --files "src/**/*.py" \
  --batch-size 4 \
  --max-usd 1.50 \
  --parse-json \
  --instruction "Return a JSON array of {file, severity, issue, line_hint} for real bugs or race conditions only." \
  --out /tmp/bug_sweep.json \
  --report-md /tmp/bug_sweep.md
```

### 3. Map-Reduce Over Any JSONL Dataset (Logs, PRs, Issues, Transcripts)

```bash
cursor-fleet map \
  --campaign ticket-triage \
  --preset synthesis \
  --items-jsonl ./tickets.jsonl \
  --id-field ticket_id \
  --batch-size 4 \
  --max-usd 2.00 \
  --parse-json \
  --instruction-file ./triage_prompt.txt \
  --out ./triage_results.json \
  --report-md ./triage_report.md
```

### 4. Isolated Parallel Worktree Edits + Clean Patch Application (`patch`)

Every `worktree` worker edits inside its own isolated `~/.cursor/worktrees/<repo>/<id>` checkout, harvests a unified `.patch` (including untracked files), optionally runs a verification command inside Linux `bwrap --ro-bind / / --bind <wt> <wt> --unshare-net` (`--confined`), and immediately prunes the worktree so disk never bloats:

```bash
# Run parallel isolated patches
cursor-fleet ask --preset patch --campaign perf-fixes --cwd /path/to/repo \
  --verify-cmd "pytest -q" \
  "Optimize parser.py hot loop and add a unit test" \
  "Fix edge case in cache.py eviction and add a unit test"

# Inspect harvested patches & dry-run check before applying
cursor-fleet patches --campaign perf-fixes
cursor-fleet patches --campaign perf-fixes --apply <job_id> --repo /path/to/repo --check
cursor-fleet patches --campaign perf-fixes --apply <job_id> --repo /path/to/repo
```

### 5. Use Any Other Cursor Model (`--allow-any-model`) or Future Grok Families

```bash
# Future-proof Grok family switch (e.g. when Grok 4.8 ships)
WM_GROK_FAMILY=grok-4.8 cursor-fleet ask --preset synthesis "Hello Grok 4.8"

# Dispatch any model shown in `cursor-agent models` (Composer, Codex, Claude, Auto)
cursor-fleet ask --preset synthesis --allow-any-model --model composer-2.5 \
  "Summarize the pros and cons of zero-copy parsing"
```

---

### 6. Automatic Map-Reduce (`map --reduce-instruction`)

Collapse 10–500 parallel map batches into a single deduplicated executive synthesis (using automatic recursive tree-reduce whenever outputs exceed `--reduce-max-chunk-chars`):

```bash
cursor-fleet map \
  --campaign portability-sweep \
  --preset synthesis \
  --files "bin/*" \
  --batch-size 4 \
  --instruction "List macOS/Linux portability bugs, missing subprocess timeouts, and silent error swallows as JSON." \
  --reduce-instruction "Deduplicate and rank all findings into P0/P1/P2 with exact files and one-line fixes." \
  --reduce-out /tmp/portability_summary.md \
  --out /tmp/portability.json \
  --report-md /tmp/portability_report.md
```

### 7. Best-of-N Worktree Tournament (`race`)

Spawn `N` parallel isolated git worktrees with distinct strategies (or competing models via `--models`), verify each against `--verify-cmd` (optionally inside network-isolated Linux `bwrap` via `--confined`), extract a numeric benchmark score via `--score-regex`, and rank survivors deterministically:

```bash
cursor-fleet race \
  --campaign hot-loop-race \
  --cwd /path/to/repo \
  --n 3 \
  --verify-cmd "pytest -q && python3 bench.py" \
  --score-regex "median=([0-9.]+)ms" \
  --score-goal min \
  --confined \
  --prompt "Optimize parser.py hot loop while keeping 100% test pass rate." \
  --report-md /tmp/race_leaderboard.md
# Add --apply-winner to auto-apply the #1 verified patch cleanly, or apply later with `cursor-fleet patches`.
```

### 8. Blind Multi-Model Council (`council`)

Dispatch multiple distinct Cursor model families **blind in parallel** on the same spec, disclosure, or diff, then run a consensus judge that separates **Consensus (`>=2` models agree)** from unique findings and contradictions:

```bash
cursor-fleet council \
  --campaign security-council \
  --files "src/auth.py" \
  --models "grok-4.7-high,composer-2.5,claude-sonnet-5-thinking-high" \
  --judge-model "grok-4.7-high" \
  --prompt "Review this auth module for bypasses, race conditions, and edge cases." \
  --report-md /tmp/council_report.md
```

### 9. Declarative Multi-Stage Funnels (`pipeline`)

Chain cheap Stage-1 synthesis screening → `$0` local deterministic Python/shell filtering or execution verification → Stage-2 high-effort audit/patch/race/council waves. See ready-to-use JSON templates in [`examples/cursor-fleet/`](../examples/cursor-fleet/):

```bash
cursor-fleet pipeline examples/cursor-fleet/security_funnel.json --dry-run
cursor-fleet pipeline examples/cursor-fleet/security_funnel.json --cwd /path/to/repo --max-usd 2.50
```

---

## Managing Campaigns & Crash-Safe Resume

```bash
cursor-fleet campaigns                         # list all campaigns, status, spend, quota %
cursor-fleet status --campaign ticket-triage   # live summary + host RAM/disk headroom
cursor-fleet inspect --campaign ticket-triage --failed-only --show-output
cursor-fleet report --campaign ticket-triage --format md --out report.md

# Interrupted by Ctrl+C, --max-usd, or Cursor quota reset? Resume cleanly — done jobs are skipped:
cursor-fleet run --campaign ticket-triage --preset synthesis --max-usd 3.00
cursor-fleet prune-worktrees --dry-run
```

---

## 1-Tool Stdio MCP Server (`wm-grok-mcp`)

Add `grok_dispatch` to **Cursor** (`~/.cursor/mcp.json`), **Antigravity**, or **Claude Code** so your primary coding agent can fan out 1–6 parallel Grok workers directly:

```json
{
  "mcpServers": {
    "grok": {
      "command": "/home/YOU/.local/bin/wm-grok-mcp",
      "args": []
    }
  }
}
```

Example MCP call (zero-tool pure synthesis, 18.8× cheaper):

```json
{
  "action": "ask",
  "preset": "synthesis",
  "concurrency": 4,
  "tasks": [
    {"name": "lane-1", "prompt": "Analyze snippet A..."},
    {"name": "lane-2", "prompt": "Analyze snippet B..."}
  ]
}
```

---

## Tuning Tips From Empirical Calibration

1. **Always prefer `--preset synthesis` with inlined context** whenever workers do not need to explore a repository interactively. Unconstrained `ask` workers run 10–25 workspace search tool calls (~230k tokens/worker), whereas `--preset synthesis` runs `0` tool calls (~12k tokens/worker).
2. **Pack 4 items per worker (`--batch-size 4`)** for structured triage/classification: beats `N=1` (overhead per worker) and `N=8` (reasoning wall timeouts) on both tokens and wall-time per item.
3. **Stick to standard non-fast tiers (`medium` / `high`)** for batch fleets; reserve `*-fast` for latency-critical interactive checks.
4. **Static instructions always go first**: `map` and `enqueue` place guardrails and `--instruction` before varying `[TASK]` / `=== ITEM ===` blocks to maximize Grok 4.7 prefix cache reuse.

