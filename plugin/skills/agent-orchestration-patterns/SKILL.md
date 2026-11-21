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

## Gemini 4 Prefix-Cache & Quota Discipline

Read [references/gemini4_cache_discipline.md](./references/gemini4_cache_discipline.md) before running
long tool chains on Gemini 4 Neon/Carbon (implicit caching drops sharply past 30s inter-turn delays;
batch independent reads/commands in parallel and keep long-running commands asynchronous).
