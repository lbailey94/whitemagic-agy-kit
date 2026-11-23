import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { runApplyEdits } from "../src/tools/apply-edits.js";
import { runBatchSearch } from "../src/tools/batch-search.js";
import { runCodeGraph } from "../src/tools/code-graph.js";
import { runChecks } from "../src/tools/run-checks.js";
import { parseSafeSqueezeCommand } from "../src/utils/wm-squeeze-bridge.js";
import { analyzePaths } from "../src/benchmark-core.js";

test("apply_edits: auto-relocates drifted line window when unique global exact match exists", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-v4-edit-"));
  const file = path.join(dir, "sample.ts");
  const lines = Array.from({ length: 40 }, (_, i) => `const line${i + 1} = ${i + 1};`);
  lines[29] = "const targetVal = 100;";
  await fs.writeFile(file, lines.join("\n") + "\n", "utf8");

  const res = await runApplyEdits({
    cwd: dir,
    edits: [
      {
        path: "sample.ts",
        hunks: [
          {
            target: "const targetVal = 100;",
            replacement: "const targetVal = 200;",
            startLine: 5,
            endLine: 15,
          },
        ],
      },
    ],
  });

  assert.equal(res.isError, false, res.text);
  assert.match(res.text, /auto-relocated L5-15 -> L30/);
  const updated = await fs.readFile(file, "utf8");
  assert.ok(updated.includes("const targetVal = 200;"));
  await fs.rm(dir, { recursive: true, force: true });
});

test("apply_edits: strictWindow=true refuses drifted line window even when unique", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-v4-strict-"));
  const file = path.join(dir, "sample.ts");
  const lines = Array.from({ length: 20 }, (_, i) => `const line${i + 1} = ${i + 1};`);
  lines[15] = "const targetVal = 100;";
  await fs.writeFile(file, lines.join("\n") + "\n", "utf8");

  const res = await runApplyEdits({
    cwd: dir,
    edits: [
      {
        path: "sample.ts",
        hunks: [
          {
            target: "const targetVal = 100;",
            replacement: "const targetVal = 200;",
            startLine: 1,
            endLine: 5,
            strictWindow: true,
          },
        ],
      },
    ],
  });

  assert.equal(res.isError, true);
  assert.match(res.text, /Exact match found outside range at L16/);
  await fs.rm(dir, { recursive: true, force: true });
});

test("apply_edits: refuses auto-relocate when multiple matches exist outside range", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-v4-multi-"));
  const file = path.join(dir, "sample.ts");
  await fs.writeFile(file, "a\nb\nDUP\nc\nDUP\n", "utf8");

  const res = await runApplyEdits({
    cwd: dir,
    edits: [
      {
        path: "sample.ts",
        hunks: [{ target: "DUP", replacement: "NEW", startLine: 1, endLine: 2 }],
      },
    ],
  });

  assert.equal(res.isError, true);
  assert.match(res.text, /Exact match found outside range at L3, L5/);
  await fs.rm(dir, { recursive: true, force: true });
});

test("apply_edits: trailing-whitespace unique fallback applies cleanly", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-v4-tw-"));
  const file = path.join(dir, "sample.py");
  await fs.writeFile(file, "def foo():   \n    return 1  \n", "utf8");

  const res = await runApplyEdits({
    cwd: dir,
    edits: [
      {
        path: "sample.py",
        hunks: [{ target: "def foo():\n    return 1", replacement: "def foo():\n    return 2" }],
      },
    ],
  });

  assert.equal(res.isError, false, res.text);
  assert.match(res.text, /trailing-ws normalized/);
  const updated = await fs.readFile(file, "utf8");
  assert.equal(updated, "def foo():\n    return 2\n");
  await fs.rm(dir, { recursive: true, force: true });
});

test("wm-squeeze-bridge: parseSafeSqueezeCommand allows safe commands + strips trailing 2>&1 and rejects unsafe", () => {
  const p1 = parseSafeSqueezeCommand("python3 -m pytest tests/ -v 2>&1", "/tmp");
  assert.ok(p1);
  assert.deepEqual(p1.cmdArgs, ["python3", "-m", "pytest", "tests/", "-v"]);

  const p2 = parseSafeSqueezeCommand("PYTHONPATH=. pytest -q", "/tmp");
  assert.ok(p2);
  assert.deepEqual(p2.envVars, ["PYTHONPATH=."]);
  assert.deepEqual(p2.cmdArgs, ["pytest", "-q"]);

  assert.equal(parseSafeSqueezeCommand("python3 -c 'print(1)'", "/tmp"), null);
  assert.equal(parseSafeSqueezeCommand("echo hi | grep hi", "/tmp"), null);
  assert.equal(parseSafeSqueezeCommand("LD_PRELOAD=/x.so pytest", "/tmp"), null);
});

test("run_checks: uses wm-squeeze in compact mode and bypasses in full mode", async () => {
  const sqBin = path.resolve(process.cwd(), "../wm-squeeze/target/release/wm-squeeze");
  const prev = process.env.WM_SQUEEZE_BIN;
  process.env.WM_SQUEEZE_BIN = sqBin;
  try {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-v4-checks-"));
    await fs.writeFile(path.join(dir, "a.txt"), "hello world\n", "utf8");

    const r1 = await runChecks({
      cwd: dir,
      commands: [{ name: "ls_compact", command: "ls -la" }],
      outputMode: "compact",
    });
    assert.equal(r1.isError, false, r1.text);
    assert.match(r1.text, /squeezed/);

    const r2 = await runChecks({
      cwd: dir,
      commands: [{ name: "ls_full", command: "ls -la" }],
      outputMode: "full",
    });
    assert.equal(r2.isError, false, r2.text);
    assert.ok(!r2.text.includes("squeezed+"));
    await fs.rm(dir, { recursive: true, force: true });
  } finally {
    if (prev === undefined) delete process.env.WM_SQUEEZE_BIN;
    else process.env.WM_SQUEEZE_BIN = prev;
  }
});

test("code_graph: runs overview via wm-codegraph release binary", async () => {
  const cgBin = path.resolve(process.cwd(), "../wm-codegraph/target/release/wm-codegraph");
  const prev = process.env.WM_CODEGRAPH_BIN;
  process.env.WM_CODEGRAPH_BIN = cgBin;
  try {
    const root = path.resolve(process.cwd(), "../wm-squeeze");
    const res = await runCodeGraph({ mode: "overview", root });
    assert.equal(res.isError, false, res.text);
    assert.match(res.text, /main\.rs|files/i);
  } finally {
    if (prev === undefined) delete process.env.WM_CODEGRAPH_BIN;
    else process.env.WM_CODEGRAPH_BIN = prev;
  }
});

test("batch_search: collapses Cargo.lock diff while preserving source diff", async () => {
  const { execFileSync } = await import("node:child_process");
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-v4-git-"));
  execFileSync("git", ["init"], { cwd: dir });
  execFileSync("git", ["config", "user.email", "t@t.local"], { cwd: dir });
  execFileSync("git", ["config", "user.name", "Test"], { cwd: dir });
  await fs.writeFile(path.join(dir, "Cargo.lock"), "v1\n", "utf8");
  await fs.writeFile(path.join(dir, "lib.rs"), "fn a() {}\n", "utf8");
  execFileSync("git", ["add", "."], { cwd: dir });
  execFileSync("git", ["commit", "-m", "init"], { cwd: dir });
  await fs.writeFile(path.join(dir, "Cargo.lock"), "v1\nv2\nv3\n", "utf8");
  await fs.writeFile(path.join(dir, "lib.rs"), "fn a() { let x = 1; }\n", "utf8");

  const res = await runBatchSearch({
    cwd: dir,
    queries: [{ type: "git", pattern: "diff" }],
  });
  assert.equal(res.isError, false, res.text);
  assert.match(res.text, /lockfile diff omitted: Cargo\.lock/);
  assert.match(res.text, /let x = 1/);
  await fs.rm(dir, { recursive: true, force: true });
});

test("benchmark-core: recognizes fused whitemagic-agy-kit tool names and SessionStart Cached Brief", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-v4-bench-"));
  const log = path.join(dir, "transcript.jsonl");
  const lines = [
    JSON.stringify({
      type: "SYSTEM_MESSAGE",
      content: "[SessionStart Cached Brief | source=/x]\n# Brief",
    }),
    JSON.stringify({
      type: "PLANNER_RESPONSE",
      input_tokens: 100,
      cache_read_tokens: 900,
      output_tokens: 50,
      tool_calls: [
        { name: "mcp_whitemagic-agy-kit_fast_code_graph", args: { mode: "overview" } },
      ],
    }),
  ];
  await fs.writeFile(log, lines.join("\n") + "\n", "utf8");
  const m = await analyzePaths([log]);
  assert.equal(m.sessionsWithAgyFast, 1);
  assert.equal(m.sessionsWithSnapshot, 1);
  await fs.rm(dir, { recursive: true, force: true });
});
