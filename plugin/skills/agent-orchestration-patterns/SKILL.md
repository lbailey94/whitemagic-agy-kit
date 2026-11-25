---
name: agent-orchestration-patterns
description: >-
  Five research-backed multi-agent orchestration patterns for Antigravity CLI (`run_workflow` &
  subagents) plus Gemini 4 implicit prefix-cache discipline. Use when deciding whether a task
  benefits from a single thread vs parallel agents, running Builder+Verifier diffs, splitting work
  into clean non-overlapping lanes, running Best-of-N worktree optimization, convening a blind
  Council of Models, or dispatching specialized read-only reviewer teams.
---

# Research-Backed Agent Orchestration Patterns (`run_workflow`)

Empirical findings (Google Cloud / MIT / SWE benchmarks) show multi-agent orchestration beats a single
agent **only when matched to the task structure**: overlapping file edits drop success ~30%, and
sequential planning dependencies drop 39–70% under naive parallelism.

## Decision Matrix: Single Thread vs. Pattern

| Task Shape | Best Pattern | Script Template |
|---|---|---|
| Tightly coupled sequential edit or small bug fix | **Single thread** (do not fan out) | N/A |
| Non-trivial change needing unbiased diff audit | **1. Builder + Fresh Read-Only Verifier** | [builder_verifier.py](./scripts/builder_verifier.py) |
| Cleanly separable directories (`web/`, `api/`, `docs/`) | **2. Clean-Lane Split** (`workspace="share"`) | [lane_split.py](./scripts/lane_split.py) |
| Hard optimization / bug with automated test suite | **3. Best of N** (`workspace="branch"`) | [best_of_n.py](./scripts/best_of_n.py) |
| Architecture / UX / strategy with no automated test | **4. Blind Council of Models** | [council_of_models.py](./scripts/council_of_models.py) |
| Pre-merge / release audit across orthogonal risks | **5. Specialized Reviewer Team** (+35% bugs) | [reviewer_team.py](./scripts/reviewer_team.py) |

## Running a Template with `run_workflow`

Invoke `run_workflow` with `ScriptPath` pointing to one of the scripts in `./scripts/` and pass `Args`:
- `builder_verifier.py`: `{"task": "...", "diff_cmd": "git diff"}`
- `lane_split.py`: `{"lanes": [{"name": "api", "scope": "src/api/", "task": "..."}], "integration_cmd": "pytest"}`
- `best_of_n.py`: `{"task": "...", "eval_cmd": "cargo test --release", "n": 3}`
- `council_of_models.py`: `{"prompt": "...", "criteria": ["correctness", "simplicity", "sovereignty"]}`
- `reviewer_team.py`: `{"target": "git diff HEAD~1", "roles": ["security", "edge_cases", "performance", "simplicity"]}`

## Gemini 4 Prefix-Cache & Orchestrator Discipline (v4.5.0)

1. **`<80k` Plan / `>100k` Pin (`Orchestrator -> Hydrogen`):** Use `/model` or `agy-mode orchestrator` for bounded planning below `80k` context; switch/pin directly to a single model (`gemini-4-hydrogen-high` or `/compact` -> `wm-reset`) once context crosses `100k` or after `>=2` ping-pong flips.
2. **Use `/btw` for Side-Channel Checks:** Ask clarification or status questions with `/btw` so ephemeral asides never mutate or bust the main prefix cache.
3. **Shared Codemap & Pointer Receipts (`<=150` tok):** Generate one shared codebase map with `wm-handoff --swarm-map .` (`<=600` tok), assign workers `workspace="share"` + explicit `model: "gemini-4-hydrogen-high"` (never `fork` at `>=80k`), and post `<=150`-token CPR pointers on the board instead of inline logs (`>4 KB`).
4. **Native Plugin Agents (`plugin/agents/`):** Dispatch `hydrogen-builder` for cache-pinned coding, `fresh-verifier` for unbiased read-only diff/test checks (`<60k` ctx, never forked), and `swarm-coordinator` for multi-worker swarm orchestration.
5. **Inter-Turn Gaps:** Implicit caching drops sharply past `30s` idle gaps on Neon/Carbon; batch independent reads/commands in parallel and run `>10s` commands asynchronously. See [references/gemini4_cache_discipline.md](./references/gemini4_cache_discipline.md).
