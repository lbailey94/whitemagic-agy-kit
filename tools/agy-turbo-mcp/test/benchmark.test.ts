import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";
import {
  analyzePaths,
  analyzePathsByUsage,
  analyzeTranscriptSessions,
  collectTranscriptFiles,
  filterStepsByTime,
  parseTranscriptJsonl,
} from "../src/benchmark-core.js";

const BENCHMARK_CLI = path.resolve(process.cwd(), "..", "..", "plugin", "dist", "benchmark.mjs");

describe("benchmark analyzer & CLI", () => {
  let tmpDir: string;

  before(async () => {
    tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-fast-bench-"));
  });

  after(async () => {
    await fs.rm(tmpDir, { recursive: true, force: true });
  });

  it("computes accurate latency, fragmentation, shell search, and cache metrics", () => {
    const jsonl = [
      JSON.stringify({
        step_index: 0,
        type: "USER_INPUT",
        created_at: "2026-10-01T10:00:00Z",
      }),
      // Turn 1: single run_command (rg)
      JSON.stringify({
        step_index: 1,
        type: "PLANNER_RESPONSE",
        created_at: "2026-10-01T10:00:00Z",
        input_tokens: 20_000,
        cache_read_tokens: 0,
        output_tokens: 200,
        tool_calls: [{ name: "run_command", args: { CommandLine: "rg foo src/" } }],
      }),
      JSON.stringify({
        step_index: 2,
        type: "GENERIC",
        created_at: "2026-10-01T10:00:08Z",
      }),
      // Turn 2: consecutive single run_command (git status) -> 1 extra chain turn
      JSON.stringify({
        step_index: 3,
        type: "PLANNER_RESPONSE",
        created_at: "2026-10-01T10:00:10Z",
        input_tokens: 5_000,
        cache_read_tokens: 20_000,
        output_tokens: 150,
        tool_calls: [{ name: "run_command", args: { CommandLine: "git status" } }],
      }),
      JSON.stringify({
        step_index: 4,
        type: "GENERIC",
        created_at: "2026-10-01T10:00:17Z",
      }),
      // Turn 3: 3 edit_file calls on same file (2 redundant) with stream spread 10s + exec 5s
      JSON.stringify({
        step_index: 5,
        type: "PLANNER_RESPONSE",
        created_at: "2026-10-01T10:00:19Z",
        input_tokens: 10_000,
        cache_read_tokens: 150_000,
        output_tokens: 1000,
        tool_calls: [
          { name: "edit_file", args: { Path: "/repo/a.ts" } },
          { name: "edit_file", args: { Path: "/repo/a.ts" } },
          { name: "edit_file", args: { Path: "/repo/a.ts" } },
        ],
      }),
      JSON.stringify({
        step_index: 6,
        type: "GENERIC",
        created_at: "2026-10-01T10:00:25Z",
      }),
      JSON.stringify({
        step_index: 7,
        type: "GENERIC",
        created_at: "2026-10-01T10:00:35Z",
      }),
      // Turn 4: blocking ask_question (wait excluded from active time)
      JSON.stringify({
        step_index: 8,
        type: "PLANNER_RESPONSE",
        created_at: "2026-10-01T10:00:40Z",
        input_tokens: 5_000,
        cache_read_tokens: 155_000,
        output_tokens: 100,
        tool_calls: [{ name: "ask_question", args: {} }],
      }),
      JSON.stringify({
        step_index: 9,
        type: "GENERIC",
        created_at: "2026-10-01T10:05:40Z",
      }),
      // Turn 5: agy-fast batch_search after >60s gap
      JSON.stringify({
        step_index: 10,
        type: "PLANNER_RESPONSE",
        created_at: "2026-10-01T10:05:45Z",
        input_tokens: 10_000,
        cache_read_tokens: 160_000,
        output_tokens: 300,
        tool_calls: [{ name: "mcp_agy-fast_fast_batch_search", args: {} }],
      }),
      "not valid json line to ignore",
    ].join("\n");

    const steps = parseTranscriptJsonl(jsonl);
    const metrics = analyzeTranscriptSessions([steps]);

    assert.equal(metrics.sessions, 1);
    assert.equal(metrics.llmTurns, 5);
    assert.equal(metrics.totalToolCalls, 7);
    assert.equal(metrics.consecutiveSingleChainsExtraTurns, 1);
    assert.equal(metrics.editFileCalls, 3);
    assert.equal(metrics.redundantSameTurnEditFileCalls, 2);
    assert.equal(metrics.runCommandCalls, 2);
    assert.equal(metrics.readonlySearchRunCommandCalls, 2);
    assert.equal(metrics.agyFastToolCalls.batch_search, 1);
    // Turn 1: llm 8 + exec 2 = 10
    // Turn 2: llm 7 + exec 2 = 9
    // Turn 3: llm 6 + spread 10 + exec 5 = 21
    // Turn 4: excluded (ask_question)
    assert.equal(metrics.llmTimeSec, 21);
    assert.equal(metrics.streamWaitSec, 10);
    assert.equal(metrics.toolExecTimeSec, 9);
    assert.equal(metrics.activeTimeSec, 40);
    assert.ok(metrics.over150kContextRatio > 0.5);
    assert.ok(metrics.over60sGapRatio > 0);
  });

  it("runs CLI analyze and compare commands cleanly", async () => {
    const beforeFile = path.join(tmpDir, "before.jsonl");
    const afterFile = path.join(tmpDir, "after.jsonl");

    await fs.writeFile(
      beforeFile,
      [
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-10-01T10:00:00Z",
          input_tokens: 10000,
          cache_read_tokens: 50000,
          output_tokens: 500,
          tool_calls: [{ name: "run_command", args: { CommandLine: "rg a" } }],
        }),
        JSON.stringify({ type: "GENERIC", created_at: "2026-10-01T10:00:10Z" }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-10-01T10:00:15Z",
          input_tokens: 10000,
          cache_read_tokens: 50000,
          output_tokens: 500,
          tool_calls: [],
        }),
      ].join("\n"),
      "utf8",
    );

    await fs.writeFile(
      afterFile,
      [
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-10-01T10:00:00Z",
          input_tokens: 10000,
          cache_read_tokens: 50000,
          output_tokens: 500,
          tool_calls: [{ name: "fast:batch_search", args: {} }],
        }),
        JSON.stringify({ type: "GENERIC", created_at: "2026-10-01T10:00:07Z" }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-10-01T10:00:08Z",
          input_tokens: 10000,
          cache_read_tokens: 50000,
          output_tokens: 500,
          tool_calls: [],
        }),
      ].join("\n"),
      "utf8",
    );

    const analyzeJson = execFileSync(
      process.execPath,
      [BENCHMARK_CLI, "analyze", beforeFile, "--json"],
      { encoding: "utf8" },
    );
    const parsed = JSON.parse(analyzeJson);
    assert.equal(parsed.sessions, 1);
    assert.equal(parsed.activeTimeSec, 15);

    const compareMd = execFileSync(
      process.execPath,
      [BENCHMARK_CLI, "compare", "--before", beforeFile, "--after", afterFile],
      { encoding: "utf8" },
    );
    assert.match(compareMd, /# agy-fast A\/B Comparison/);
    assert.match(compareMd, /Active time \(s\) \| 15\.0 \| 8\.0 \| -46\.7%/);
  });

  it("recurses nested session dirs, recognizes slash/dot MCP tool names, and errors on missing explicit paths", async () => {
    const rootSessionDir = path.join(tmpDir, "nested-root");
    const subSessionDir = path.join(rootSessionDir, "child");
    await fs.mkdir(subSessionDir, { recursive: true });

    const turnLine = (toolName: string) =>
      JSON.stringify({
        type: "PLANNER_RESPONSE",
        created_at: "2026-10-01T10:00:00Z",
        output_tokens: 100,
        tool_calls: [{ name: toolName, args: {} }],
      });

    await fs.writeFile(
      path.join(rootSessionDir, "transcript.jsonl"),
      turnLine("fast/apply_edits") + "\n",
      "utf8",
    );
    await fs.writeFile(
      path.join(subSessionDir, "transcript.jsonl"),
      turnLine("fast.run_checks") + "\n",
      "utf8",
    );

    const outJson = execFileSync(
      process.execPath,
      [BENCHMARK_CLI, "analyze", rootSessionDir, "--json"],
      { encoding: "utf8" },
    );
    const parsed = JSON.parse(outJson);
    assert.equal(parsed.sessions, 2);
    assert.equal(parsed.agyFastToolCalls.apply_edits, 1);
    assert.equal(parsed.agyFastToolCalls.run_checks, 1);

    // Also recognize Antigravity lazy-loaded call_mcp_tool entries (including JSON-encoded ServerName/ToolName)
    const lazyMetrics = analyzeTranscriptSessions([
      [
        {
          type: "PLANNER_RESPONSE",
          created_at: "2026-10-01T10:00:00Z",
          output_tokens: 100,
          tool_calls: [
            {
              name: "call_mcp_tool",
              args: {
                ServerName: '"agy-fast_fast"',
                ToolName: '"batch_search"',
                Arguments: "{}",
              },
            },
            {
              name: "call_mcp_tool",
              args: {
                ServerName: '"agy-fast_fast"',
                ToolName: '"read_slices"',
                Arguments: "{}",
              },
            },
            {
              name: "call_mcp_tool",
              args: {
                ServerName: '"other_server"',
                ToolName: '"batch_search"',
                Arguments: "{}",
              },
            },
          ],
        },
      ],
    ]);
    assert.equal(lazyMetrics.agyFastToolCalls.batch_search, 1);
    assert.equal(lazyMetrics.agyFastToolCalls.read_slices, 1);

    assert.throws(() => {
      execFileSync(
        process.execPath,
        [BENCHMARK_CLI, "analyze", path.join(tmpDir, "does-not-exist.jsonl")],
        { stdio: "pipe" },
      );
    });
  });

  it("skips chunks/, swarm/board.jsonl, and scratch/results.jsonl in brain directories while preserving custom dirs", async () => {
    const brainDir = path.join(tmpDir, "brain");
    const sess1Logs = path.join(brainDir, "sess-1", ".system_generated", "logs");
    const sess1ChunksFull = path.join(sess1Logs, "chunks", "transcript_full");
    const sess1ChunksBasic = path.join(sess1Logs, "chunks", "transcript");
    const sess1Swarm = path.join(brainDir, "sess-1", ".system_generated", "swarm");
    const sess1Scratch = path.join(brainDir, "sess-1", "scratch", "run1");
    const sess2Logs = path.join(brainDir, "sess-2", ".system_generated", "logs");
    const sess2Chunks = path.join(sess2Logs, "chunks", "transcript");
    const orphanScratch = path.join(brainDir, "sess-orphan", "scratch");

    await Promise.all([
      fs.mkdir(sess1ChunksFull, { recursive: true }),
      fs.mkdir(sess1ChunksBasic, { recursive: true }),
      fs.mkdir(sess1Swarm, { recursive: true }),
      fs.mkdir(sess1Scratch, { recursive: true }),
      fs.mkdir(sess2Chunks, { recursive: true }),
      fs.mkdir(orphanScratch, { recursive: true }),
    ]);

    const plannerLine = (tok: number) =>
      JSON.stringify({
        type: "PLANNER_RESPONSE",
        created_at: "2026-10-01T10:00:00Z",
        output_tokens: tok,
        tool_calls: [],
      }) + "\n";

    // Session 1 has full, basic, chunks, board.jsonl, and scratch/results.jsonl
    await fs.writeFile(path.join(sess1Logs, "transcript_full.jsonl"), plannerLine(500), "utf8");
    await fs.writeFile(path.join(sess1Logs, "transcript.jsonl"), plannerLine(100), "utf8");
    await fs.writeFile(path.join(sess1ChunksFull, "00000000.jsonl"), plannerLine(500), "utf8");
    await fs.writeFile(path.join(sess1ChunksFull, "00000001.jsonl"), plannerLine(500), "utf8");
    await fs.writeFile(path.join(sess1ChunksBasic, "00000000.jsonl"), plannerLine(100), "utf8");
    await fs.writeFile(path.join(sess1Swarm, "board.jsonl"), plannerLine(999), "utf8");
    await fs.writeFile(path.join(sess1Scratch, "results.jsonl"), plannerLine(999), "utf8");

    // Session 2 only has transcript.jsonl + chunk duplicate
    await fs.writeFile(path.join(sess2Logs, "transcript.jsonl"), plannerLine(300), "utf8");
    await fs.writeFile(path.join(sess2Chunks, "00000000.jsonl"), plannerLine(300), "utf8");

    // Orphan brain dir without .system_generated
    await fs.writeFile(path.join(orphanScratch, "results.jsonl"), plannerLine(999), "utf8");

    const brainFiles = await collectTranscriptFiles([brainDir]);
    assert.deepEqual(brainFiles, [
      path.join(sess1Logs, "transcript_full.jsonl"),
      path.join(sess2Logs, "transcript.jsonl"),
    ]);

    const brainMetrics = await analyzePaths([brainDir]);
    assert.equal(brainMetrics.sessions, 2);
    assert.equal(brainMetrics.totalOutputTokens, 800);

    // Passing a single session dir (<id>/) also skips chunks/swarm/scratch
    const singleSessionFiles = await collectTranscriptFiles([
      path.join(brainDir, "sess-1"),
    ]);
    assert.deepEqual(singleSessionFiles, [
      path.join(sess1Logs, "transcript_full.jsonl"),
    ]);

    // Passing an orphan brain session dir directly still ignores non-transcript jsonl
    const orphanDirect = path.join(brainDir, "sess-orphan");
    await fs.writeFile(path.join(orphanDirect, "notes.jsonl"), plannerLine(999), "utf8");
    const orphanFiles = await collectTranscriptFiles([orphanDirect]);
    assert.deepEqual(orphanFiles, []);

    // Custom non-brain dir still collects custom *.jsonl files (including scratch/ and swarm/) while skipping chunks/
    const customDir = path.join(tmpDir, "custom-runs");
    await fs.mkdir(path.join(customDir, "chunks"), { recursive: true });
    await fs.mkdir(path.join(customDir, "scratch"), { recursive: true });
    await fs.mkdir(path.join(customDir, "swarm"), { recursive: true });
    await fs.writeFile(path.join(customDir, "run-a.jsonl"), plannerLine(50), "utf8");
    await fs.writeFile(path.join(customDir, "run-b.jsonl"), plannerLine(60), "utf8");
    await fs.writeFile(path.join(customDir, "scratch", "run-c.jsonl"), plannerLine(70), "utf8");
    await fs.writeFile(path.join(customDir, "swarm", "run-d.jsonl"), plannerLine(80), "utf8");
    await fs.writeFile(path.join(customDir, "chunks", "00000000.jsonl"), plannerLine(50), "utf8");

    const customFiles = await collectTranscriptFiles([customDir]);
    assert.deepEqual(customFiles, [
      path.join(customDir, "run-a.jsonl"),
      path.join(customDir, "run-b.jsonl"),
      path.join(customDir, "scratch", "run-c.jsonl"),
      path.join(customDir, "swarm", "run-d.jsonl"),
    ]);

    // Symlinked transcript files & session dirs are collected, deduplicated, and cycle-safe
    const symlinkDir = path.join(tmpDir, "symlink-runs");
    await fs.mkdir(symlinkDir, { recursive: true });
    await fs.symlink(
      path.join(sess1Logs, "transcript_full.jsonl"),
      path.join(symlinkDir, "s1-link.jsonl"),
    );
    await fs.symlink(
      path.join(brainDir, "sess-1"),
      path.join(symlinkDir, "s1-dir-link"),
    );
    await fs.symlink(
      path.join(brainDir, "sess-2"),
      path.join(symlinkDir, "s2-dir-link"),
    );
    await fs.symlink(symlinkDir, path.join(symlinkDir, "self-loop"));

    const symlinkFiles = await collectTranscriptFiles([symlinkDir]);
    assert.equal(symlinkFiles.length, 2);
    const symlinkMetrics = await analyzePaths([symlinkDir]);
    assert.equal(symlinkMetrics.sessions, 2);
    assert.equal(symlinkMetrics.totalOutputTokens, 800);
  });

  it("unwraps double-JSON-encoded args, supports call.arguments, and matches git -C and env-prefixed readonly commands", () => {
    const readonlyCmds = [
      // Double-JSON-encoded readonly commands (real transcript.jsonl format)
      { args: { CommandLine: JSON.stringify("ls -la /repo") } },
      { args: { CommandLine: JSON.stringify("git -C /repo status -s") } },
      { args: { CommandLine: JSON.stringify('git -C "/my repo" --no-pager diff --stat') } },
      { args: { CommandLine: JSON.stringify('HOME=/tmp LC_ALL=C grep -rn "foo" src/') } },
      { args: { CommandLine: JSON.stringify('FOO="bar baz" env LC_ALL=C rg -n "Map<String, Object>" src/') } },
      { args: { CommandLine: JSON.stringify("cd /repo && GIT_PAGER=cat git -C /other log -n 5") } },
      { args: { CommandLine: JSON.stringify("cd /repo; rg foo src/ || true") } },
      { args: { CommandLine: JSON.stringify('find . -name "*.ts" 2>/dev/null | sort | head -n 20') } },
      { args: { CommandLine: JSON.stringify("git branch --show-current && git remote -v && git tag --list") } },
      { args: { CommandLine: JSON.stringify("git branch --color=always") } },
      { args: { CommandLine: JSON.stringify("git branch --color --list") } },
      { args: { CommandLine: JSON.stringify("git branch --abbrev=8 --list") } },
      { args: { CommandLine: JSON.stringify("git branch --column=never") } },
      { args: { CommandLine: JSON.stringify("git tag --color --list") } },
      { args: { CommandLine: JSON.stringify("git tag --column=never") } },
      { args: { CommandLine: JSON.stringify("wc -l < src/index.ts") } },
      // call.arguments object and JSON-string fallbacks
      { arguments: { CommandLine: JSON.stringify("wc -l src/index.ts") } },
      { arguments: JSON.stringify({ CommandLine: "rg hello src/" }) },
      // Truncated double-JSON-encoded commands (including \uXXXX escapes) from transcript.jsonl
      { args: { CommandLine: '"grep -rn \\"pattern\\" src/ ...\n<truncated 400 bytes>' } },
      { args: { CommandLine: '"cd /repo \\u0026\\u0026 rg foo src/\n<truncated 120 bytes>' } },
    ];

    const nonReadonlyCmds = [
      // Heredoc, file redirects (with or without space), and in-place edits
      { args: { CommandLine: JSON.stringify("cat << 'EOF' > /tmp/out.py\nprint(1)\nEOF") } },
      { args: { CommandLine: JSON.stringify("rg foo src/ > out.txt") } },
      { args: { CommandLine: JSON.stringify("cat a>b") } },
      { args: { CommandLine: JSON.stringify("sed -i '' 's/a/b/' file.ts") } },
      { args: { CommandLine: JSON.stringify("sed -Ei 's/a/b/' file.ts") } },
      { args: { CommandLine: JSON.stringify("sed --in-place 's/a/b/' file.ts") } },
      { args: { CommandLine: JSON.stringify("awk -i inplace '{print}' file.ts") } },
      // Mutating git, find, and fd invocations
      { args: { CommandLine: JSON.stringify('git -C /repo commit -m "msg"') } },
      { args: { CommandLine: JSON.stringify("git -C /repo branch -D old") } },
      { args: { CommandLine: JSON.stringify("git branch -f main HEAD") } },
      { args: { CommandLine: JSON.stringify("git branch new-feature") } },
      { args: { CommandLine: JSON.stringify("git branch --color new-branch") } },
      { args: { CommandLine: JSON.stringify("git branch --column new-branch") } },
      { args: { CommandLine: JSON.stringify("git branch --abbrev new-branch") } },
      { args: { CommandLine: JSON.stringify("git branch --color -D --column target") } },
      { args: { CommandLine: JSON.stringify("git branch --merged -D old") } },
      { args: { CommandLine: JSON.stringify("git remote add origin https://example.com") } },
      { args: { CommandLine: JSON.stringify("git remote set-head origin main") } },
      { args: { CommandLine: JSON.stringify("git diff --output=patch.diff") } },
      { args: { CommandLine: JSON.stringify("git tag v1.0.0") } },
      { args: { CommandLine: JSON.stringify("git tag --color evil-tag") } },
      { args: { CommandLine: JSON.stringify("git tag --column evil-tag") } },
      { args: { CommandLine: JSON.stringify("git tag --color -d --column v0.2.2") } },
      { args: { CommandLine: JSON.stringify("find . -name '.DS_Store' -delete") } },
      { args: { CommandLine: JSON.stringify("find . -name '*.tmp' -exec rm {} +") } },
      // Chained, piped, and multi-line mutating commands
      { args: { CommandLine: JSON.stringify("git remote -v && git push --follow-tags origin main") } },
      { args: { CommandLine: JSON.stringify("cd /repo && git status --short && npm run verify") } },
      { args: { CommandLine: JSON.stringify("ls -la /tmp\nmkdir -p /tmp/out") } },
      { args: { CommandLine: JSON.stringify("cat file.txt | tee out.txt") } },
      { args: { CommandLine: JSON.stringify("find . -name '*.log' | xargs rm") } },
      { args: { CommandLine: JSON.stringify("ls && (pdflatex main.tex)") } },
      { args: { CommandLine: JSON.stringify("ls $(rm -rf /tmp/x)") } },
      // Non-search executables sharing prefix with search commands, and glue-only commands
      { args: { CommandLine: JSON.stringify("tree-sitter build") } },
      { args: { CommandLine: JSON.stringify("file-watcher --run") } },
      { args: { CommandLine: JSON.stringify("ls/run.sh") } },
      { args: { CommandLine: JSON.stringify("echo hello") } },
    ];

    const steps = parseTranscriptJsonl(
      [
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-10-01T10:00:00Z",
          output_tokens: 200,
          tool_calls: [
            ...readonlyCmds.map((c) => ({ name: "run_command", ...c })),
            ...nonReadonlyCmds.map((c) => ({ name: "run_command", ...c })),
            // Double-JSON-encoded edit_file Path matching plain Path and arguments fallback
            { name: "edit_file", args: { Path: JSON.stringify("/repo/a.ts") } },
            { name: "edit_file", args: { Path: "/repo/a.ts" } },
            { name: "edit_file", arguments: { Path: JSON.stringify("/repo/a.ts") } },
            // Empty double-JSON-encoded paths must not falsely collide as redundant
            { name: "edit_file", args: { Path: JSON.stringify("") } },
            { name: "edit_file", args: { Path: JSON.stringify("") } },
          ],
        }),
      ].join("\n"),
    );

    const metrics = analyzeTranscriptSessions([steps]);
    assert.equal(metrics.runCommandCalls, readonlyCmds.length + nonReadonlyCmds.length);
    assert.equal(metrics.readonlySearchRunCommandCalls, readonlyCmds.length);
    assert.equal(metrics.editFileCalls, 5);
    assert.equal(metrics.redundantSameTurnEditFileCalls, 2);

    const readonlyOnly = analyzeTranscriptSessions([
      [{ type: "PLANNER_RESPONSE", tool_calls: readonlyCmds.map((c) => ({ name: "run_command", ...c })) }],
    ]);
    assert.equal(readonlyOnly.readonlySearchRunCommandCalls, readonlyCmds.length);
    const nonReadonlyOnly = analyzeTranscriptSessions([
      [{ type: "PLANNER_RESPONSE", tool_calls: nonReadonlyCmds.map((c) => ({ name: "run_command", ...c })) }],
    ]);
    assert.equal(nonReadonlyOnly.readonlySearchRunCommandCalls, 0);
  });

  it("supports --recent <N> to select the N most recently modified transcripts and rejects invalid CLI flags", async () => {
    const recentDir = path.join(tmpDir, "recent-sessions");
    await fs.mkdir(recentDir, { recursive: true });

    const makeTurn = (tokens: number) =>
      JSON.stringify({
        type: "PLANNER_RESPONSE",
        created_at: "2026-10-01T10:00:00Z",
        output_tokens: tokens,
        tool_calls: [],
      }) + "\n";

    const fOld = path.join(recentDir, "s1-old.jsonl");
    const fMid = path.join(recentDir, "s2-mid.jsonl");
    const fNew = path.join(recentDir, "s3-new.jsonl");
    const fEmpty = path.join(recentDir, "s4-empty.jsonl");

    await fs.writeFile(fOld, makeTurn(100), "utf8");
    await fs.writeFile(fMid, makeTurn(200), "utf8");
    await fs.writeFile(fNew, makeTurn(400), "utf8");
    await fs.writeFile(fEmpty, JSON.stringify({ type: "USER_INPUT" }) + "\n", "utf8");

    // Set distinct mtimes: old < mid < new < empty
    await fs.utimes(fOld, new Date("2026-10-01T01:00:00Z"), new Date("2026-10-01T01:00:00Z"));
    await fs.utimes(fMid, new Date("2026-10-01T02:00:00Z"), new Date("2026-10-01T02:00:00Z"));
    await fs.utimes(fNew, new Date("2026-10-01T03:00:00Z"), new Date("2026-10-01T03:00:00Z"));
    await fs.utimes(fEmpty, new Date("2026-10-01T04:00:00Z"), new Date("2026-10-01T04:00:00Z"));

    const recent2 = JSON.parse(
      execFileSync(
        process.execPath,
        [BENCHMARK_CLI, "analyze", recentDir, "--recent", "2", "--json"],
        { encoding: "utf8" },
      ),
    );
    assert.equal(recent2.sessions, 2);
    assert.equal(recent2.totalOutputTokens, 600); // s3-new (400) + s2-mid (200)

    const recent1 = JSON.parse(
      execFileSync(
        process.execPath,
        [BENCHMARK_CLI, "analyze", recentDir, "--recent=1", "--json"],
        { encoding: "utf8" },
      ),
    );
    assert.equal(recent1.sessions, 1);
    assert.equal(recent1.totalOutputTokens, 400); // s3-new only

    const recent10 = await analyzePaths([recentDir], { recent: 10 });
    assert.equal(recent10.sessions, 3);
    assert.equal(recent10.totalOutputTokens, 700);

    // Deterministic tie-breaking when two transcripts share identical mtimeMs
    const tieDir = path.join(tmpDir, "tie-sessions");
    await fs.mkdir(tieDir, { recursive: true });
    const tieA = path.join(tieDir, "a.jsonl");
    const tieB = path.join(tieDir, "b.jsonl");
    await fs.writeFile(tieA, makeTurn(111), "utf8");
    await fs.writeFile(tieB, makeTurn(222), "utf8");
    const sameDate = new Date("2026-10-01T05:00:00Z");
    await fs.utimes(tieA, sameDate, sameDate);
    await fs.utimes(tieB, sameDate, sameDate);
    const tieRes = await analyzePaths([tieDir], { recent: 1 });
    assert.equal(tieRes.sessions, 1);
    assert.equal(tieRes.totalOutputTokens, 111); // deterministic ascending path tie-break

    // compare supports --before=<path> and --after=<path>
    const cmpOut = JSON.parse(
      execFileSync(
        process.execPath,
        [BENCHMARK_CLI, "compare", `--before=${fOld}`, `--after=${fNew}`, "--json"],
        { encoding: "utf8" },
      ),
    );
    assert.equal(cmpOut.before.totalOutputTokens, 100);
    assert.equal(cmpOut.after.totalOutputTokens, 400);

    // --local with --recent on a fresh HOME without brain dirs returns 0 sessions cleanly
    const freshHome = path.join(tmpDir, "fresh-home");
    await fs.mkdir(freshHome, { recursive: true });
    const freshEnv: NodeJS.ProcessEnv = { ...process.env, HOME: freshHome, USERPROFILE: freshHome };
    delete freshEnv.GEMINI_HOME;
    delete freshEnv.ANTIGRAVITY_APP_DATA_DIR;
    const freshOut = JSON.parse(
      execFileSync(
        process.execPath,
        [BENCHMARK_CLI, "analyze", "--local", "--recent", "5", "--json"],
        {
          encoding: "utf8",
          env: freshEnv,
        },
      ),
    );
    assert.equal(freshOut.sessions, 0);

    await assert.rejects(() => analyzePaths([recentDir], { recent: 0 }), RangeError);
    await assert.rejects(() => analyzePaths([recentDir], { recent: 1e20 }), RangeError);

    for (const badArgs of [
      ["analyze", recentDir, "--recent"],
      ["analyze", recentDir, "--recent", "0"],
      ["analyze", recentDir, "--recent", "-2"],
      ["analyze", recentDir, "--recent", "abc"],
      ["analyze", recentDir, "--recent=1.5"],
      ["analyze", recentDir, "--since"],
      ["analyze", recentDir, "--since", "not-a-date"],
      ["analyze", recentDir, "--until", "12345"],
      ["analyze", recentDir, "--since", "2026-11-20T00:00:00Z", "--until", "2026-11-19T00:00:00Z"],
      ["analyze", recentDir, "--unknown-flag"],
      ["compare", "--before", "--after", fNew],
      ["compare", "--before", fOld, "--after", fNew, "--unknown"],
    ]) {
      assert.throws(() => {
        execFileSync(process.execPath, [BENCHMARK_CLI, ...badArgs], {
          stdio: "pipe",
        });
      });
    }
  });

  it("supports --since, --until, and --split-usage for resumed sessions and accurate snapshot detection", async () => {
    const splitDir = path.join(tmpDir, "split-usage-sessions");
    await fs.mkdir(splitDir, { recursive: true });

    // Resumed session: old pre-plugin steps (Oct 26) + post-plugin steps (Nov 19) using agy-fast
    // Also includes a GENERIC read_file output containing the literal string "[agy-fast startup snapshot]"
    // which must NOT be miscounted as a SessionStart hook injection.
    const resumedFile = path.join(splitDir, "resumed-fast.jsonl");
    await fs.writeFile(
      resumedFile,
      [
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-10-26T10:00:00Z",
          output_tokens: 900,
          tool_calls: [{ name: "run_command", args: { CommandLine: "rg old src/" } }],
        }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-10-26T10:00:10Z",
          content: "old output",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-19T05:30:00Z",
          output_tokens: 400,
          tool_calls: [
            {
              name: "call_mcp_tool",
              args: {
                ServerName: '"agy-fast_fast"',
                ToolName: '"apply_edits"',
                Arguments: "{}",
              },
            },
          ],
        }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-19T05:30:06Z",
          content: "source code reading `[agy-fast startup snapshot]` literal",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-19T05:30:08Z",
          output_tokens: 200,
          tool_calls: [],
        }),
      ].join("\n") + "\n",
      "utf8",
    );

    // Post-plugin session that got SessionStart snapshot but made 0 agy-fast calls (e.g. /boost subagent)
    const snapOnlyFile = path.join(splitDir, "snapshot-no-fast.jsonl");
    await fs.writeFile(
      snapOnlyFile,
      [
        JSON.stringify({
          type: "SYSTEM_MESSAGE",
          created_at: "2026-11-19T05:05:00Z",
          content: "The following system message was injected by a SessionStart hook:\n[agy-fast startup snapshot] Workspace: /repo",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-19T05:05:02Z",
          output_tokens: 300,
          tool_calls: [
            { name: "edit_file", args: { Path: "/repo/a.ts" } },
            { name: "edit_file", args: { Path: "/repo/a.ts" } },
          ],
        }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-19T05:05:10Z",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-19T05:05:14Z",
          output_tokens: 100,
          tool_calls: [],
        }),
      ].join("\n") + "\n",
      "utf8",
    );

    // Pure old session completely before --since
    const oldOnlyFile = path.join(splitDir, "old-only.jsonl");
    await fs.writeFile(
      oldOnlyFile,
      JSON.stringify({
        type: "PLANNER_RESPONSE",
        created_at: "2026-10-20T08:00:00Z",
        output_tokens: 777,
        tool_calls: [],
      }) + "\n",
      "utf8",
    );

    const splitJson = JSON.parse(
      execFileSync(
        process.execPath,
        [
          BENCHMARK_CLI,
          "analyze",
          splitDir,
          "--since",
          "2026-11-19T05:00:00Z",
          "--until",
          "2026-11-19",
          "--split-usage",
          "--json",
        ],
        { encoding: "utf8" },
      ),
    );

    // old-only excluded, Oct 26 turn in resumed-fast excluded
    assert.equal(splitJson.overall.sessions, 2);
    assert.equal(splitJson.overall.sessionsWithAgyFast, 1);
    assert.equal(splitJson.overall.sessionsWithSnapshot, 1); // only snapOnlyFile, NOT GENERIC read_file
    assert.equal(splitJson.overall.totalOutputTokens, 1000); // 400+200 + 300+100
    assert.equal(splitJson.overall.readonlySearchRunCommandCalls, 0); // Oct 26 rg excluded

    assert.equal(splitJson.withAgyFast.sessions, 1);
    assert.equal(splitJson.withAgyFast.agyFastToolCalls.apply_edits, 1);
    assert.equal(splitJson.withAgyFast.sessionsWithSnapshot, 0);
    assert.equal(splitJson.withAgyFast.totalOutputTokens, 600);

    assert.equal(splitJson.withoutAgyFast.sessions, 1);
    assert.equal(splitJson.withoutAgyFast.sessionsWithSnapshot, 1);
    assert.equal(splitJson.withoutAgyFast.redundantSameTurnEditFileCalls, 1);
    assert.equal(splitJson.withoutAgyFast.totalOutputTokens, 400);

    const splitMd = execFileSync(
      process.execPath,
      [
        BENCHMARK_CLI,
        "analyze",
        splitDir,
        "--since=2026-11-19T05:00:00Z",
        "--split-usage",
      ],
      { encoding: "utf8" },
    );
    assert.match(splitMd, /# agy-fast Usage Split Benchmark/);
    assert.match(splitMd, /\| Sessions \| 2 \| 1 \| 1 \| - \|/);

    const directSplit = await analyzePathsByUsage([splitDir], {
      sinceMs: Date.parse("2026-11-19T05:00:00Z"),
      untilMs: Date.parse("2026-11-19T23:59:59Z"),
    });
    assert.equal(directSplit.withAgyFast.sessions, 1);
    assert.equal(directSplit.withoutAgyFast.sessions, 1);

    // Async idle wait (calls=0 followed by delayed SYSTEM_MESSAGE callback) must NOT be counted as llmTimeSec or >60s gap
    const asyncWaitSteps = parseTranscriptJsonl(
      [
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-19T08:05:00Z",
          output_tokens: 200,
          tool_calls: [],
          content: "Waiting for background task...",
        }),
        JSON.stringify({
          type: "SYSTEM_MESSAGE",
          created_at: "2026-11-19T08:09:30Z",
          content: "Background task finished",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-19T08:09:30Z",
          output_tokens: 300,
          tool_calls: [{ name: "read_file", args: { Path: "/repo/a.ts" } }],
        }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-19T08:09:38Z",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-19T08:09:39Z",
          output_tokens: 100,
          tool_calls: [],
        }),
      ].join("\n"),
    );
    const asyncMetrics = analyzeTranscriptSessions([asyncWaitSteps]);
    assert.equal(asyncMetrics.llmTimeSec, 8); // NOT 278s (270s async sleep excluded)
    assert.equal(asyncMetrics.toolExecTimeSec, 1);
    assert.equal(asyncMetrics.over60sGapRatio, 0);

    // Health & multimodal diagnostics: MCP spills, internal truncations, errors, multi-MCP, multi-run_command, and >=180k multimodal turns
    const healthSteps = parseTranscriptJsonl(
      [
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-19T12:00:00Z",
          media: [{ mime_type: "image/png", uri: "file:///tmp/shot1.png" }],
          content: "image loaded",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-19T12:00:05Z",
          input_tokens: 10_000,
          cache_read_tokens: 185_000,
          output_tokens: 400,
          tool_calls: [
            { name: "call_mcp_tool", arguments: { ServerName: "agy-fast_fast", ToolName: "read_slices", Arguments: {} } },
            { name: "call_mcp_tool", arguments: { ServerName: "agy-fast_fast", ToolName: "batch_search", Arguments: {} } },
          ],
        }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-19T12:00:08Z",
          content: "The output was large and was saved to: file:///tmp/output.txt",
        }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-19T12:00:09Z",
          content: "## Query 1\n... [truncated: showed 4000 of 9000 bytes]",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-19T12:00:10Z",
          input_tokens: 5_000,
          cache_read_tokens: 195_000,
          output_tokens: 300,
          tool_calls: [
            { name: "run_command", arguments: { CommandLine: "echo a" } },
            { name: "run_command", arguments: { CommandLine: "echo b" } },
            { name: "call_mcp_tool", arguments: { ServerName: "agy-fast_fast", ToolName: "apply_edits", Arguments: {} } },
          ],
        }),
        JSON.stringify({ type: "GENERIC", created_at: "2026-11-19T12:00:11Z", content: "a" }),
        JSON.stringify({ type: "GENERIC", created_at: "2026-11-19T12:00:12Z", content: "b" }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-19T12:00:13Z",
          content: "Encountered error in step execution: Partial edit applied (1/2 file(s) written; 1 file(s) failed)",
        }),
      ].join("\n"),
    );
    const hm = analyzeTranscriptSessions([healthSteps]);
    assert.equal(hm.mcpSpillCalls, 1);
    assert.equal(hm.agyTruncatedCalls, 1);
    assert.equal(hm.agyErrorCalls, 1);
    assert.equal(hm.multiMcpTurns, 1);
    assert.equal(hm.multiRunCommandTurns, 1);
    assert.equal(hm.mediaLoadedCount, 1);
    assert.equal(hm.multimodalHighCtxTurns, 2);

    // Corrupted MCP argument tracking + swarm worker coverage tracking
    const corruptedSession = parseTranscriptJsonl(
      [
        JSON.stringify({
          type: "USER_INPUT",
          created_at: "2026-11-20T21:54:59Z",
          content: "<USER_REQUEST>fix bug</USER_REQUEST>",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-20T21:55:00Z",
          output_tokens: 9000,
          tool_calls: [
            {
              name: "call_mcp_tool",
              args: {
                ServerName: "agy-fast_fast",
                ToolName: "apply_edits",
                Arguments: { cwd: "/repo", edits: [false, true] },
              },
            },
          ],
        }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-20T21:56:10Z",
          status: "ERROR",
          content: "Encountered error in step execution: Validation failed (0 files modified)",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-20T21:56:12Z",
          output_tokens: 200,
          tool_calls: [],
        }),
      ].join("\n"),
    );
    const swarmWorkerSession = parseTranscriptJsonl(
      [
        JSON.stringify({
          type: "SYSTEM_MESSAGE",
          created_at: "2026-11-21T01:03:00Z",
          content: "[Message] @builder do work",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-21T01:03:05Z",
          output_tokens: 300,
          tool_calls: [{ name: "read_board", args: {} }],
        }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-21T01:03:06Z",
        }),
      ].join("\n"),
    );
    const cm = analyzeTranscriptSessions([corruptedSession, swarmWorkerSession]);
    assert.equal(cm.agyCorruptedArgCalls, 1);
    // An empty edits array is the lost-payload form seen on agy's Flash worker.
    const emptyEditsSession = parseTranscriptJsonl(
      [
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-20T22:00:01Z",
          output_tokens: 3000,
          tool_calls: [
            {
              name: "call_mcp_tool",
              args: { ServerName: "agy-fast_fast", ToolName: "apply_edits", Arguments: { edits: [] } },
            },
          ],
        }),
      ].join("\n"),
    );
    assert.equal(analyzeTranscriptSessions([emptyEditsSession]).agyCorruptedArgCalls, 1);
    assert.equal(cm.agyCorruptedArgWastedSec, 72);
    assert.equal(cm.agyCorruptedArgWastedOutTokens, 9000);
    assert.equal(cm.swarmWorkerSessions, 1);
    assert.equal(cm.swarmWorkerSessionsWithAgyFast, 0);
  });

  it("discovers IDE/ACP --local brains, handles middle-truncated cmds, direct MCP arg corruption, git tag parity, and --since coordinator identity", async () => {
    // 1. --local discovers Antigravity IDE and ACP/T3 provider brains
    const multiHome = path.join(tmpDir, "multi-surface-home");
    const ideLogs = path.join(multiHome, ".gemini", "antigravity-ide", "brain", "ide-1", ".system_generated", "logs");
    const acpLogs = path.join(
      multiHome,
      ".t3",
      "userdata",
      "providers",
      "antigravity",
      "hash123",
      "antigravity-acp",
      "brain",
      "acp-1",
      ".system_generated",
      "logs",
    );
    const customGeminiHome = path.join(tmpDir, "custom-gemini-home");
    const customGeminiLogs = path.join(customGeminiHome, "antigravity-acp", "brain", "gh-1", ".system_generated", "logs");
    await fs.mkdir(ideLogs, { recursive: true });
    await fs.mkdir(acpLogs, { recursive: true });
    await fs.mkdir(customGeminiLogs, { recursive: true });
    const simpleTurn = (tok: number) =>
      JSON.stringify({ type: "PLANNER_RESPONSE", created_at: "2026-11-20T10:00:00Z", output_tokens: tok, tool_calls: [] }) + "\n";
    await fs.writeFile(path.join(ideLogs, "transcript.jsonl"), simpleTurn(150), "utf8");
    await fs.writeFile(path.join(acpLogs, "transcript.jsonl"), simpleTurn(250), "utf8");
    await fs.writeFile(path.join(customGeminiLogs, "transcript.jsonl"), simpleTurn(100), "utf8");

    const multiEnv: NodeJS.ProcessEnv = {
      ...process.env,
      HOME: multiHome,
      USERPROFILE: multiHome,
      GEMINI_HOME: customGeminiHome,
    };
    delete multiEnv.ANTIGRAVITY_APP_DATA_DIR;
    const localOut = JSON.parse(
      execFileSync(process.execPath, [BENCHMARK_CLI, "analyze", "--local", "--json"], {
        encoding: "utf8",
        env: multiEnv,
      }),
    );
    assert.equal(localOut.sessions, 3);
    assert.equal(localOut.totalOutputTokens, 500);

    // 2. Middle-truncated JSON CommandLine, git tag/branch parity, direct MCP corrupted args,
    //    distinct call_mcp_tool chaining, and `[truncated; showing ...` detection
    const edgeSteps = parseTranscriptJsonl(
      [
        JSON.stringify({
          type: "USER_INPUT",
          created_at: "2026-11-20T09:00:00Z",
          content: "<USER_REQUEST>audit repo</USER_REQUEST>",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-20T10:00:00Z",
          output_tokens: 100,
          tool_calls: [
            { name: "run_command", args: { CommandLine: '"rg foo src/\n<truncated 400 bytes>\n... bar/"' } },
            { name: "run_command", args: { CommandLine: JSON.stringify("git tag --contains HEAD") } },
            { name: "run_command", args: { CommandLine: JSON.stringify("git tag --merged main") } },
            { name: "run_command", args: { CommandLine: JSON.stringify("git tag -v v0.2.5") } },
            { name: "run_command", args: { CommandLine: JSON.stringify("git branch --merged main") } },
          ],
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-20T10:00:05Z",
          output_tokens: 100,
          tool_calls: [
            { name: "call_mcp_tool", args: { ServerName: '"agy-fast_fast"', ToolName: '"read_slices"', Arguments: '{"slices":[{"path":"a.ts","ranges":true}]}' } },
          ],
        }),
        JSON.stringify({
          type: "GENERIC",
          created_at: "2026-11-20T10:00:06Z",
          content: "## Query 1\n... [truncated; showing 30 files]",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-20T10:00:08Z",
          output_tokens: 100,
          tool_calls: [
            { name: "call_mcp_tool", args: { ServerName: '"agy-fast_fast"', ToolName: '"apply_edits"', Arguments: "{}" } },
          ],
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-20T10:00:10Z",
          output_tokens: 100,
          tool_calls: [
            { name: "mcp_agy-fast_fast_apply_edits", args: { edits: "[false, true]" } },
            { name: "read_board", args: {} },
          ],
        }),
      ].join("\n"),
    );

    const edgeMetrics = analyzeTranscriptSessions([edgeSteps]);
    assert.equal(edgeMetrics.readonlySearchRunCommandCalls, 5);
    assert.equal(edgeMetrics.agyCorruptedArgCalls, 2); // read_slices ranges:true + direct stringified edits:[false, true]
    assert.equal(edgeMetrics.agyTruncatedCalls, 1); // `[truncated; showing 30 files]`
    assert.equal(edgeMetrics.consecutiveSingleChainsExtraTurns, 0); // read_slices -> apply_edits are different tools

    // 3. --since slicing after coordinator's initial USER_INPUT must NOT misclassify coordinator as a swarm worker,
    //    and direct filterStepsByTime also preserves worker + startup-snapshot identity
    const coordFile = path.join(tmpDir, "resumed-coord.jsonl");
    await fs.writeFile(coordFile, edgeSteps.map((s) => JSON.stringify(s)).join("\n") + "\n", "utf8");
    const slicedCoord = await analyzePaths([coordFile], { sinceMs: Date.parse("2026-11-20T09:30:00Z") });
    assert.equal(slicedCoord.sessions, 1);
    assert.equal(slicedCoord.swarmWorkerSessions, 0);

    const workerSteps = parseTranscriptJsonl(
      [
        JSON.stringify({
          type: "SYSTEM_MESSAGE",
          created_at: "2026-11-20T09:00:00Z",
          content: "[agy-fast startup snapshot] Workspace: /tmp/repo",
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-20T09:05:00Z",
          output_tokens: 50,
          tool_calls: [{ name: "read_board", args: {} }],
        }),
        JSON.stringify({
          type: "PLANNER_RESPONSE",
          created_at: "2026-11-20T10:00:00Z",
          output_tokens: 80,
          tool_calls: [{ name: "read_file", args: { Path: "/tmp/a.ts" } }],
        }),
      ].join("\n"),
    );
    const directSlicedWorker = filterStepsByTime(workerSteps, Date.parse("2026-11-20T09:30:00Z"));
    const directWorkerMetrics = analyzeTranscriptSessions([directSlicedWorker]);
    assert.equal(directWorkerMetrics.sessions, 1);
    assert.equal(directWorkerMetrics.swarmWorkerSessions, 1);
    assert.equal(directWorkerMetrics.sessionsWithSnapshot, 1);
  });
});
