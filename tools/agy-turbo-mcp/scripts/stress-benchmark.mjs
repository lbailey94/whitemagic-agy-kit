#!/usr/bin/env node
/**
 * agy-fast Multi-Scenario Stress & Performance Benchmark Suite
 *
 * Exercises the compiled production artifacts (`dist/mcp-server.mjs` and
 * `dist/session-start.mjs`) over real JSON-RPC stdio across 7 heavy scenarios:
 *   1. Large multi-language monorepo (2,500+ files + git + node_modules noise)
 *      `batch_search` with ripgrep AND with Node.js fallback (no rg in PATH).
 *   2. Massive files (5,000 lines x 5 languages) + 40-file dense `read_slices`
 *      + ReDoS / pathological input + CJK UTF-8 / emoji boundary integrity.
 *   3. 50-file full-capacity `apply_edits` atomic writes, `$&`/`$$` safety,
 *      atomic rollback verification, 50-file `bestEffort` partial commits,
 *      2 MB noisy `verify` filtering, and corrupted `call_mcp_tool` args matrix.
 *   4. 12-way concurrent `run_checks` speedup, 5 MB ANSI + CR log flood,
 *      and timeout process-tree cleanup (verifying grandchild PIDs are killed).
 *   5. `SessionStart` hook cold/warm P50/P95/Max latency on a dirty 2,500-file
 *      monorepo.
 *   6. Real MCP stdio concurrency torture test (40 concurrent mixed requests,
 *      both NDJSON and Content-Length framing, JSON-RPC batches, malformed
 *      frames, RSS memory check).
 *   7. Cross-platform edge paths (spaces, parentheses, CJK filenames, 12-level
 *      deep nesting, symlink dedup).
 *
 * Usage:
 *   node scripts/stress-benchmark.mjs [--json] [--scale=standard|heavy]
 */
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");
const MCP_SERVER = path.join(REPO_ROOT, "dist", "mcp-server.mjs");
const SESSION_START = path.join(REPO_ROOT, "dist", "session-start.mjs");
const INLINE_MAX_BYTES = 8_800;

const argvList = process.argv.slice(2);
const args = new Set(argvList);
const JSON_MODE = args.has("--json");
const HEAVY = args.has("--scale=heavy");
const REAL_REPO_ARG = argvList.find((a) => a.startsWith("--real-repo="))?.slice("--real-repo=".length) || "";
const FILE_COUNT = HEAVY ? 5000 : 2500;
const NOISE_COUNT = HEAVY ? 1000 : 500;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function percentile(values, p) {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return +sorted[idx].toFixed(2);
}

function stats(values) {
  if (values.length === 0) return { min: 0, p50: 0, p95: 0, max: 0, avg: 0 };
  const sum = values.reduce((a, b) => a + b, 0);
  return {
    min: +Math.min(...values).toFixed(2),
    p50: percentile(values, 50),
    p95: percentile(values, 95),
    max: +Math.max(...values).toFixed(2),
    avg: +(sum / values.length).toFixed(2),
  };
}

function hasLoneSurrogate(text) {
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      i++;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function assertInlineSafe(text, label) {
  const bytes = Buffer.byteLength(text, "utf8");
  assert.ok(
    bytes <= INLINE_MAX_BYTES,
    `[${label}] exceeded ${INLINE_MAX_BYTES} UTF-8 bytes: got ${bytes}`,
  );
  assert.ok(
    !hasLoneSurrogate(text),
    `[${label}] UTF-8 byte slicing produced a lone UTF-16 surrogate`,
  );
  return bytes;
}

/**
 * Minimal production-grade MCP stdio client supporting both NDJSON and
 * Content-Length framing plus concurrent in-flight JSON-RPC requests.
 */
class McpTestClient {
  constructor(options = {}) {
    this.framing = options.framing ?? "ndjson"; // "ndjson" | "content-length"
    this.env = options.env ?? process.env;
    this.nodeArgs = options.nodeArgs ?? [];
    this.nextId = 1;
    this.pending = new Map();
    this.stderrBuf = "";
    this.rawBuf = Buffer.alloc(0);
    this.child = null;
  }

  async start() {
    this.child = spawn(process.execPath, [...this.nodeArgs, MCP_SERVER], {
      env: this.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child.stderr.on("data", (chunk) => {
      this.stderrBuf += chunk.toString("utf8");
    });
    this.child.stdout.on("data", (chunk) => {
      this.rawBuf = Buffer.concat([this.rawBuf, chunk]);
      this.#drain();
    });
    this.child.on("exit", (code, sig) => {
      for (const [, p] of this.pending) {
        p.reject(new Error(`MCP server exited early (code=${code}, sig=${sig}): ${this.stderrBuf}`));
      }
      this.pending.clear();
    });

    const init = await this.request("initialize", {
      protocolVersion: "2025-03-26",
      capabilities: {},
      clientInfo: { name: "stress-benchmark", version: "1.0.0" },
    });
    assert.ok(init.result?.serverInfo?.name === "agy-fast", "initialize failed");
    this.notify("notifications/initialized", {});
    return init.result;
  }

  #drain() {
    while (this.rawBuf.length > 0) {
      if (this.framing === "content-length") {
        const headerEnd = this.rawBuf.indexOf("\r\n\r\n");
        if (headerEnd === -1) return;
        const header = this.rawBuf.subarray(0, headerEnd).toString("utf8");
        const m = /content-length:\s*(\d+)/i.exec(header);
        if (!m) throw new Error(`Invalid Content-Length header: ${header}`);
        const len = Number(m[1]);
        const bodyStart = headerEnd + 4;
        if (this.rawBuf.length < bodyStart + len) return;
        const body = this.rawBuf.subarray(bodyStart, bodyStart + len).toString("utf8");
        this.rawBuf = this.rawBuf.subarray(bodyStart + len);
        this.#dispatch(body);
      } else {
        const nl = this.rawBuf.indexOf(0x0a);
        if (nl === -1) return;
        const line = this.rawBuf.subarray(0, nl).toString("utf8").trim();
        this.rawBuf = this.rawBuf.subarray(nl + 1);
        if (line) this.#dispatch(line);
      }
    }
  }

  #dispatch(jsonText) {
    const parsed = JSON.parse(jsonText);
    const items = Array.isArray(parsed) ? parsed : [parsed];
    for (const msg of items) {
      const key = msg.id ?? "__null__";
      const waiter = this.pending.get(key);
      if (waiter) {
        this.pending.delete(key);
        waiter.resolve(msg);
      }
    }
  }

  #writeRaw(payloadStr) {
    if (this.framing === "content-length") {
      const bytes = Buffer.byteLength(payloadStr, "utf8");
      this.child.stdin.write(`Content-Length: ${bytes}\r\n\r\n${payloadStr}`);
    } else {
      this.child.stdin.write(`${payloadStr}\n`);
    }
  }

  notify(method, params) {
    this.#writeRaw(JSON.stringify({ jsonrpc: "2.0", method, params }));
  }

  request(method, params, explicitId) {
    const id = explicitId === undefined ? this.nextId++ : explicitId;
    const key = id ?? "__null__";
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(key);
        reject(new Error(`RPC timeout for ${method} (id=${id})`));
      }, 30_000);
      timer.unref();
      this.pending.set(key, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      this.#writeRaw(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    });
  }

  sendRawFrame(rawString, expectNullId = false) {
    if (!expectNullId) {
      this.#writeRaw(rawString);
      return Promise.resolve(null);
    }
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete("__null__");
        reject(new Error("RPC timeout waiting for parse error response"));
      }, 5_000);
      timer.unref();
      this.pending.set("__null__", {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      this.#writeRaw(rawString);
    });
  }

  async callTool(name, toolArgs) {
    const t0 = performance.now();
    const res = await this.request("tools/call", { name, arguments: toolArgs });
    const ms = +(performance.now() - t0).toFixed(2);
    assert.ok(!res.error, `JSON-RPC error on ${name}: ${JSON.stringify(res.error)}`);
    const text = res.result?.content?.[0]?.text ?? "";
    const isError = Boolean(res.result?.isError);
    return { text, isError, ms, bytes: Buffer.byteLength(text, "utf8") };
  }

  getPid() {
    return this.child?.pid ?? null;
  }

  async close() {
    if (!this.child) return;
    const child = this.child;
    this.child = null;
    await new Promise((resolve) => {
      const timer = setTimeout(() => {
        try { child.kill("SIGKILL"); } catch {}
        resolve();
      }, 3000);
      timer.unref();
      child.once("exit", () => { clearTimeout(timer); resolve(); });
      try { child.stdin.end(); } catch {}
    });
  }
}

function getProcessRssMb(pid) {
  if (!pid || process.platform === "win32") return null;
  try {
    const out = execFileSync("ps", ["-o", "rss=", "-p", String(pid)], { encoding: "utf8" }).trim();
    const kb = Number(out);
    return Number.isFinite(kb) ? +(kb / 1024).toFixed(1) : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Synthetic Large Monorepo Generator
// ---------------------------------------------------------------------------

async function buildLargeMonorepo(rootDir) {
  const t0 = performance.now();
  const dirs = [
    "packages/api/src/routes",
    "packages/api/src/services",
    "packages/web/src/views",
    "packages/web/src/components",
    "services/java/src/main/java/com/acme/billing",
    "services/py/app/workers",
    "services/go/internal/engine",
    "services/rust/src/core",
    "game/scripts/actors",
    "docs/guides",
    "config",
    "node_modules/fake-huge-pkg/dist",
  ];
  await Promise.all(dirs.map((d) => fs.mkdir(path.join(rootDir, d), { recursive: true })));

  await fs.writeFile(
    path.join(rootDir, "package.json"),
    JSON.stringify(
      {
        name: "stress-monorepo",
        version: "1.0.0",
        scripts: {
          build: "echo built",
          test: "echo tested",
          typecheck: "echo typed",
          lint: "echo linted",
        },
      },
      null,
      2,
    ),
  );

  // Batch write files in parallel chunks to keep fixture creation fast (~1-2s)
  const writeTasks = [];
  const langs = [
    { dir: "packages/api/src/routes", ext: ".ts" },
    { dir: "packages/api/src/services", ext: ".ts" },
    { dir: "packages/web/src/views", ext: ".vue" },
    { dir: "packages/web/src/components", ext: ".js" },
    { dir: "services/java/src/main/java/com/acme/billing", ext: ".java" },
    { dir: "services/py/app/workers", ext: ".py" },
    { dir: "services/go/internal/engine", ext: ".go" },
    { dir: "services/rust/src/core", ext: ".rs" },
    { dir: "game/scripts/actors", ext: ".gd" },
    { dir: "docs/guides", ext: ".md" },
  ];

  for (let i = 0; i < FILE_COUNT; i++) {
    const slot = langs[i % langs.length];
    const rel = path.join(slot.dir, `mod_${String(i).padStart(4, "0")}${slot.ext}`);
    const isHot = i % 10 === 0;
    const isRare = i === 1234 || i === 2345;
    let body;
    if (slot.ext === ".ts" || slot.ext === ".js") {
      body = [
        `// Module ${i} — 中文模块描述 🚀`,
        `export interface ModuleConfig_${i} { id: number; enabled: boolean; }`,
        `export function computeMetrics_${i}(input: number): number {`,
        `  const tag = "${isHot ? "HOT_STRESS_TOKEN" : "normal_tok"}";`,
        isRare ? `  const needle = "RARE_MULTILINE_START"\n    + "_RARE_MULTILINE_END_${i}";` : `  const v = input * ${(i % 7) + 1};`,
        `  return ${i} + (tag.length > 0 ? input : 0);`,
        `}`,
        `export class WorkerService_${i} {`,
        `  run(payload: string) { return payload + "_${i}"; }`,
        `}`,
      ].join("\n");
    } else if (slot.ext === ".vue") {
      body = `<template><div class="panel-${i}">${isHot ? "HOT_STRESS_TOKEN" : "item"} 中文视图 ${i}</div></template>\n<script setup lang="ts">\nfunction onClick_${i}() { return ${i}; }\n</script>\n<style scoped>\n.panel-${i} { color: #333; }\n</style>\n`;
    } else if (slot.ext === ".java") {
      body = `package com.acme.billing;\npublic class BillingService_${i} {\n  public int calculate_${i}(int cents) {\n    // ${isHot ? "HOT_STRESS_TOKEN" : "std"} 计费逻辑\n    return cents + ${i};\n  }\n}\n`;
    } else if (slot.ext === ".py") {
      body = `class Worker_${i}:\n    def process_${i}(self, x: int) -> int:\n        # ${isHot ? "HOT_STRESS_TOKEN" : "std"} 中文处理\n        return x + ${i}\n`;
    } else if (slot.ext === ".go") {
      body = `package engine\nfunc RunEngine_${i}(n int) int {\n\t// ${isHot ? "HOT_STRESS_TOKEN" : "std"}\n\treturn n + ${i}\n}\n`;
    } else if (slot.ext === ".rs") {
      body = `pub struct Core_${i} { pub id: usize }\npub fn execute_${i}(v: usize) -> usize {\n    // ${isHot ? "HOT_STRESS_TOKEN" : "std"}\n    v + ${i}\n}\n`;
    } else if (slot.ext === ".gd") {
      body = `class_name Actor_${i}\nsignal moved_${i}(pos: Vector2)\nfunc tick_${i}(delta: float) -> void:\n\t# ${isHot ? "HOT_STRESS_TOKEN" : "std"}\n\tpass\n`;
    } else {
      body = `# Guide ${i}\n## Section A\n${isHot ? "HOT_STRESS_TOKEN" : "Guide body"} 文档内容 ${i}\n`;
    }
    writeTasks.push({ file: path.join(rootDir, rel), body });
  }

  // Noise inside node_modules (must be skipped by search/glob!)
  for (let i = 0; i < NOISE_COUNT; i++) {
    writeTasks.push({
      file: path.join(rootDir, "node_modules/fake-huge-pkg/dist", `bundle_${i}.js`),
      body: `var NOISE_ONLY_TOKEN = "HOT_STRESS_TOKEN_${i}";`,
    });
  }

  // Minified 100 KB single-line file + CJK heavy file
  writeTasks.push({
    file: path.join(rootDir, "packages/web/src/components/minified-100k.js"),
    body: `/* minified */ var a="${"x".repeat(50_000)}";function minTarget(){return "HOT_STRESS_TOKEN";}var b="${"y".repeat(50_000)}";\n`,
  });
  writeTasks.push({
    file: path.join(rootDir, "docs/guides/cjk-emoji-flood.md"),
    body: Array.from(
      { length: 200 },
      (_, i) => `第${i + 1}条：🔥🚀 并发压测多字节锚点 CJK_FLOOD_TOKEN — 汉字与表情混排边界校验_${"中文测试".repeat(10)}`,
    ).join("\n"),
  });

  // Flush writes in batches of 128 to avoid EMFILE
  for (let i = 0; i < writeTasks.length; i += 128) {
    const slice = writeTasks.slice(i, i + 128);
    await Promise.all(slice.map((t) => fs.writeFile(t.file, t.body, "utf8")));
  }

  // Initialize git repo with commits + dirty files so git queries & SessionStart are realistic
  execFileSync("git", ["init", "-q"], { cwd: rootDir });
  execFileSync("git", ["config", "user.email", "stress@agy-fast.local"], { cwd: rootDir });
  execFileSync("git", ["config", "user.name", "Stress Bench"], { cwd: rootDir });
  execFileSync("git", ["add", "package.json", "docs/guides/cjk-emoji-flood.md", "packages/api/src/routes/mod_0000.ts"], { cwd: rootDir });
  execFileSync("git", ["commit", "-q", "-m", "chore: initial stress fixture"], { cwd: rootDir });
  for (let c = 1; c <= 12; c++) {
    const p = path.join(rootDir, "config", `release_${c}.json`);
    await fs.writeFile(p, JSON.stringify({ step: c, tag: `v0.${c}` }), "utf8");
    execFileSync("git", ["add", p], { cwd: rootDir });
    execFileSync("git", ["commit", "-q", "-m", `feat: add release config ${c}`], { cwd: rootDir });
  }
  // Leave some dirty + untracked files for realistic git status
  await fs.appendFile(path.join(rootDir, "packages/api/src/routes/mod_0000.ts"), "\n// dirty line\n", "utf8");

  return +(performance.now() - t0).toFixed(1);
}

// ---------------------------------------------------------------------------
// Scenario 1: Large Monorepo batch_search (rg vs Node fallback + flood + serial comparison)
// ---------------------------------------------------------------------------

async function runScenario1BatchSearch(client, nodeFallbackClient, monoDir) {
  const mixed20Queries = [
    { type: "text", pattern: "HOT_STRESS_TOKEN", outputMode: "files_with_matches", maxResults: 100 },
    { type: "text", pattern: "HOT_STRESS_TOKEN", outputMode: "count", fileType: "ts", maxResults: 50 },
    { type: "regex", pattern: "computeMetrics_\\d+\\(", fileType: "ts", maxResults: 40 },
    { type: "regex", pattern: "RARE_MULTILINE_START.*RARE_MULTILINE_END_\\d+", multiline: true, fileType: "ts" },
    { type: "text", pattern: "BillingService_1004", fileType: "java" },
    { type: "text", pattern: "def process_1005", fileType: "py" },
    { type: "text", pattern: "RunEngine_1006", fileType: "go" },
    { type: "text", pattern: "execute_1007", fileType: "rust" },
    { type: "text", pattern: "Actor_1008", fileType: "gd" },
    { type: "text", pattern: "CJK_FLOOD_TOKEN", path: "docs/guides/cjk-emoji-flood.md", maxResults: 150 },
    { type: "glob", pattern: "**/*.ts", path: "packages/api", maxResults: 50 },
    { type: "glob", pattern: "**/*.json", path: "config", sortBy: "mtime", maxResults: 20 },
    { type: "glob", pattern: "mod_000|mod_001", path: "packages/api/src/routes" }, // triggers auto-recovery
    { type: "fuzzy", pattern: "billmod1004", fileType: "java", maxResults: 10 },
    { type: "fuzzy", pattern: "actmod1008", fileType: "gd", maxResults: 10 },
    { type: "git", pattern: "status --short" },
    { type: "git", pattern: "log -n 10 --oneline" },
    { type: "git", pattern: "diff --stat" },
    { type: "text", pattern: "NOISE_ONLY_TOKEN", maxResults: 20 }, // must NOT leak node_modules
    { type: "regex", pattern: "minTarget\\(\\)", path: "packages/web/src/components" },
  ];

  // 1a. 20-query concurrent batch (rg) — 5 runs for P50/P95
  const rgTimes = [];
  let lastRg;
  for (let i = 0; i < 5; i++) {
    lastRg = await client.callTool("batch_search", { cwd: monoDir, queries: mixed20Queries });
    assert.equal(lastRg.isError, false);
    assertInlineSafe(lastRg.text, `S1-rg-run${i}`);
    assert.match(lastRg.text, /## Query 1 /);
    assert.match(lastRg.text, /## Query 20 /);
    assert.doesNotMatch(lastRg.text, /fake-huge-pkg/, "node_modules noise leaked into search!");
    assert.match(lastRg.text, /RARE_MULTILINE_START/);
    rgTimes.push(lastRg.ms);
  }

  // 1b. Compare against 20 serial single-query calls (pure local + simulated 1.8s remote check)
  const serialStart = performance.now();
  for (const q of mixed20Queries) {
    const r = await client.callTool("batch_search", { cwd: monoDir, queries: [q] });
    assert.equal(r.isError, false);
    assertInlineSafe(r.text, "S1-serial");
  }
  const serialLocalMs = +(performance.now() - serialStart).toFixed(1);
  const rgP50 = percentile(rgTimes, 50);
  const simulatedRemoteSavedSec = +(((20 - 1) * 1.8)).toFixed(1);

  // 1c. Extreme CJK/emoji flood (200 heavy matches in single query)
  const floodRes = await client.callTool("batch_search", {
    cwd: monoDir,
    maxChars: 50_000, // deliberately huge — must still hard-cap <= 8800 UTF-8 bytes
    queries: [
      { type: "text", pattern: "CJK_FLOOD_TOKEN", maxResults: 200 },
      { type: "text", pattern: "HOT_STRESS_TOKEN", maxResults: 200 },
    ],
  });
  assert.equal(floodRes.isError, false);
  const floodBytes = assertInlineSafe(floodRes.text, "S1-flood");

  // 1d. Node.js fallback (no rg in PATH) on 2,500+ files
  const nodeFallbackRes = await nodeFallbackClient.callTool("batch_search", {
    cwd: monoDir,
    queries: [
      { type: "text", pattern: "HOT_STRESS_TOKEN", outputMode: "count", fileType: "ts", maxResults: 40 },
      { type: "regex", pattern: "RARE_MULTILINE_START.*RARE_MULTILINE_END_\\d+", multiline: true, fileType: "ts" },
      { type: "fuzzy", pattern: "billmod1004", fileType: "java", maxResults: 5 },
    ],
  });
  assert.equal(nodeFallbackRes.isError, false);
  assertInlineSafe(nodeFallbackRes.text, "S1-node-fallback");
  assert.match(nodeFallbackRes.text, /RARE_MULTILINE_START/);
  assert.doesNotMatch(nodeFallbackRes.text, /fake-huge-pkg/);

  // 1e. Corrupted queries + stringified JSON array
  const corruptRes = await client.callTool("batch_search", {
    cwd: monoDir,
    queries: [null, true, false],
  });
  assert.equal(corruptRes.isError, true);
  assert.match(corruptRes.text, /nested argument serialization corruption/i);

  const strRes = await client.callTool("batch_search", {
    cwd: monoDir,
    queries: JSON.stringify([{ type: "text", pattern: "Actor_1008", fileType: "gd" }]),
  });
  assert.equal(strRes.isError, false);
  assert.match(strRes.text, /Actor_1008/);

  return {
    files: FILE_COUNT + NOISE_COUNT,
    rgBatch20Ms: stats(rgTimes),
    serial20LocalMs: serialLocalMs,
    localSpeedupX: +(serialLocalMs / Math.max(1, rgP50)).toFixed(2),
    estWithRemoteCheckSec: {
      serial20: +((serialLocalMs / 1000) + 20 * 1.8).toFixed(1),
      batch1: +((rgP50 / 1000) + 1.8).toFixed(1),
      savedSec: simulatedRemoteSavedSec,
    },
    nodeFallback3qMs: nodeFallbackRes.ms,
    floodMaxBytes: floodBytes,
    allInlineSafe: true,
  };
}

// ---------------------------------------------------------------------------
// Scenario 2: Huge Files + 40-file Dense Outlines + ReDoS / Pathological Inputs
// ---------------------------------------------------------------------------

async function runScenario2ReadSlices(client, workDir) {
  const dir = path.join(workDir, "s2-slices");
  await fs.mkdir(dir, { recursive: true });

  // 2a. Generate 5,000-line files in 5 languages
  const makeBigTs = (n) =>
    Array.from({ length: n }, (_, i) =>
      `export function tsHandler_${i}(arg: string): number {\n  const s = "line_${i}_中文";\n  return s.length + arg.length;\n}\n`,
    ).join("\n");
  const makeBigJava = (n) =>
    `public class MegaBilling {\n` +
    Array.from({ length: n }, (_, i) =>
      `  public int compute_${i}(int v) {\n    // comment ${i}\n    return v + ${i};\n  }\n`,
    ).join("\n") +
    `}\n`;
  const makeBigPy = (n) =>
    `class MegaWorker:\n` +
    Array.from({ length: n }, (_, i) =>
      `    def step_${i}(self, x):\n        val = x + ${i}\n        return val\n`,
    ).join("\n");
  const makeBigGd = (n) =>
    `class_name MegaActor extends Node2D\n` +
    Array.from({ length: n }, (_, i) =>
      `func act_${i}(delta: float) -> void:\n\tvar x := delta * ${i}.0\n\tpass\n`,
    ).join("\n");
  const makeBigVue = (n) =>
    `<template>\n<div>\n${Array.from({ length: 100 }, (_, i) => `  <p>Row ${i} 中文</p>`).join("\n")}\n</div>\n</template>\n<script setup lang="ts">\n` +
    Array.from({ length: n }, (_, i) =>
      `function vueAction_${i}() {\n  return ${i};\n}\n`,
    ).join("\n") +
    `</script>\n<style scoped>\n.box { color: red; }\n</style>\n`;

  const bigFiles = {
    "mega.ts": makeBigTs(1000),      // ~5,000 lines
    "MegaBilling.java": makeBigJava(1000), // ~5,000 lines
    "mega_worker.py": makeBigPy(1250), // ~5,000 lines
    "mega_actor.gd": makeBigGd(1250), // ~5,000 lines
    "MegaPane.vue": makeBigVue(1000), // ~4,200 lines
  };
  await Promise.all(
    Object.entries(bigFiles).map(([f, c]) => fs.writeFile(path.join(dir, f), c, "utf8")),
  );

  // Pathological / ReDoS files
  const pathoFiles = {
    "patho-long-line.ts":
      `export const huge = "${"a".repeat(120_000)}";\nexport function afterHuge() { return 1; }\n`,
    "patho-deep-parens.ts":
      `export function deep(${"(".repeat(1500)}x${")".repeat(1500)}) { return 1; }\nexport const okAfter = 2;\n`,
    "patho-unclosed-comment.ts":
      `/* ${" * ".repeat(20_000)}\nexport function insideUnclosed() { return 0; }\n`,
    "patho-cjk-emoji.md":
      Array.from({ length: 300 }, (_, i) => `## 标题_${i}_🔥🚀_多字节截断测试_${"测试".repeat(8)}\n正文内容\n`).join("\n"),
    "patho-empty.ts": "",
    "patho-binary.bin": Buffer.from([0x00, 0x01, 0x02, 0xff, 0xfe, 0x00, 0x89, 0x50, 0x4e, 0x47]),
  };
  await Promise.all(
    Object.entries(pathoFiles).map(([f, c]) => fs.writeFile(path.join(dir, f), c)),
  );

  // 40 dense files for max-batch outline stress
  const dense40 = [];
  for (let i = 0; i < 40; i++) {
    const f = `dense_${String(i).padStart(2, "0")}.ts`;
    dense40.push(f);
    const body = Array.from(
      { length: 40 },
      (_, k) => `export function svc_${i}_op_${k}_longIdentifierName() { return ${k}; }`,
    ).join("\n");
    await fs.writeFile(path.join(dir, f), body, "utf8");
  }

  // Test 2a: 5 huge files (24,000+ total lines) in one outline call
  const hugeRes = await client.callTool("read_slices", {
    cwd: dir,
    outlines: Object.keys(bigFiles),
  });
  assert.equal(hugeRes.isError, false);
  const hugeBytes = assertInlineSafe(hugeRes.text, "S2-huge5");
  assert.match(hugeRes.text, /MegaBilling\.java/);
  assert.match(hugeRes.text, /mega_actor\.gd/);

  // Test 2b: 40-file max-batch outline
  const dense40Res = await client.callTool("read_slices", {
    cwd: dir,
    outlines: dense40,
  });
  assert.equal(dense40Res.isError, false);
  const dense40Bytes = assertInlineSafe(dense40Res.text, "S2-dense40");
  assert.match(dense40Res.text, /dense_39\.ts/);
  assert.match(dense40Res.text, /batch 10-20 files per call/);

  // Test 2c: Pathological / ReDoS resilience (must finish fast, < 1000ms)
  const pathoRes = await client.callTool("read_slices", {
    cwd: dir,
    outlines: Object.keys(pathoFiles),
  });
  assert.equal(pathoRes.isError, false);
  const pathoBytes = assertInlineSafe(pathoRes.text, "S2-patho");
  assert.ok(pathoRes.ms < 1500, `Pathological outline too slow (ReDoS?): ${pathoRes.ms}ms`);
  assert.match(pathoRes.text, /afterHuge/);

  // Test 2d: 30-file slices flood + out-of-bounds + corrupted args
  const slice30Res = await client.callTool("read_slices", {
    cwd: dir,
    slices: dense40.slice(0, 30).map((f) => ({
      path: f,
      ranges: [[1, 30], [9999, 10050]],
    })),
  });
  assert.equal(slice30Res.isError, false);
  const slice30Bytes = assertInlineSafe(slice30Res.text, "S2-slice30");
  assert.match(slice30Res.text, /use parallel read_file/);

  const badOut = await client.callTool("read_slices", {
    cwd: dir,
    outlines: [true, null],
  });
  assert.equal(badOut.isError, true);
  assert.match(badOut.text, /nested argument serialization corruption/i);

  return {
    huge5Files24kLinesMs: hugeRes.ms,
    huge5Bytes: hugeBytes,
    dense40FilesMs: dense40Res.ms,
    dense40Bytes,
    pathologicalReDoSMs: pathoRes.ms,
    pathoBytes,
    slice30FilesMs: slice30Res.ms,
    slice30Bytes,
  };
}

// ---------------------------------------------------------------------------
// Scenario 3: 50-file apply_edits (atomic, rollback, $& safety, bestEffort, verify flood, corruption)
// ---------------------------------------------------------------------------

async function runScenario3ApplyEdits(client, workDir) {
  const dir = path.join(workDir, "s3-edits");
  await fs.mkdir(dir, { recursive: true });

  // Prepare 50 files (mix of LF and CRLF)
  for (let i = 0; i < 50; i++) {
    const eol = i % 2 === 0 ? "\n" : "\r\n";
    const content = Array.from(
      { length: 20 },
      (_, ln) => `export const f${i}_v${ln} = "val_${i}_${ln}"; // 行${ln}`,
    ).join(eol) + eol;
    await fs.writeFile(path.join(dir, `file_${String(i).padStart(2, "0")}.ts`), content, "utf8");
  }

  // 3a. 50 files x 5 hunks = 250 hunks atomic edit, including special replacement tokens ($&, $$, $1, $', $`)
  const edits50 = [];
  for (let i = 0; i < 50; i++) {
    edits50.push({
      path: `file_${String(i).padStart(2, "0")}.ts`,
      hunks: [0, 4, 9, 14, 19].map((ln) => ({
        target: `export const f${i}_v${ln} = "val_${i}_${ln}";`,
        replacement: `export const f${i}_v${ln} = "updated_$&_$$_$1_${i}_${ln}_🚀";`,
        startLine: Math.max(1, ln),
        endLine: ln + 2,
      })),
    });
  }
  const res50 = await client.callTool("apply_edits", { cwd: dir, edits: edits50 });
  assert.equal(res50.isError, false, `50-file atomic edit failed: ${res50.text}`);
  const bytes50 = assertInlineSafe(res50.text, "S3-atomic50");

  // Verify special replacement chars ($&, $$, $1) were written literally AND CRLF was preserved on odd files
  const sample0 = await fs.readFile(path.join(dir, "file_00.ts"), "utf8");
  const sample1 = await fs.readFile(path.join(dir, "file_01.ts"), "utf8");
  assert.ok(sample0.includes('"updated_$&_$$_$1_0_0_🚀"'), "Special $ replacement chars were mangled!");
  assert.ok(!sample0.includes("\r\n"), "LF file was accidentally converted to CRLF");
  assert.ok(sample1.includes("\r\n"), "CRLF file lost CRLF line endings");

  // 3b. Atomic rollback test: 40 modifications + 9 creations + 1 deliberate failure on file #50
  const snapBefore = await fs.readFile(path.join(dir, "file_00.ts"), "utf8");
  const rollbackEdits = [];
  for (let i = 0; i < 40; i++) {
    rollbackEdits.push({
      path: `file_${String(i).padStart(2, "0")}.ts`,
      hunks: [{ target: `f${i}_v1 = "val_${i}_1"`, replacement: `f${i}_v1 = "SHOULD_ROLLBACK"` }],
    });
  }
  for (let i = 0; i < 9; i++) {
    rollbackEdits.push({
      path: `brand_new_${i}.ts`,
      create: `export const temp = ${i};\n`,
    });
  }
  rollbackEdits.push({
    path: "file_49.ts",
    hunks: [{ target: "THIS_TARGET_DOES_NOT_EXIST_ANYWHERE", replacement: "boom" }],
  });
  const rollbackRes = await client.callTool("apply_edits", { cwd: dir, edits: rollbackEdits });
  assert.equal(rollbackRes.isError, true);
  assertInlineSafe(rollbackRes.text, "S3-rollback");
  assert.equal(await fs.readFile(path.join(dir, "file_00.ts"), "utf8"), snapBefore, "Atomic rollback failed: file_00 mutated!");
  const newExists = await fs.access(path.join(dir, "brand_new_0.ts")).then(() => true).catch(() => false);
  assert.equal(newExists, false, "Atomic rollback failed: newly created file was not prevented/unlinked!");

  // 3c. bestEffort: true with 40 valid + 10 failing (drifted line + missing hunk) -> verify <= 8800B
  const bestEffortEdits = [];
  for (let i = 0; i < 50; i++) {
    if (i < 40) {
      bestEffortEdits.push({
        path: `file_${String(i).padStart(2, "0")}.ts`,
        hunks: [{ target: `f${i}_v2 = "val_${i}_2"`, replacement: `f${i}_v2 = "best_effort_${i}"` }],
      });
    } else if (i < 45) {
      // Drifted line window (exact match is at line 3, window says 10-15)
      bestEffortEdits.push({
        path: `file_${String(i).padStart(2, "0")}.ts`,
        hunks: [{ target: `f${i}_v2 = "val_${i}_2"`, replacement: "x", startLine: 10, endLine: 15 }],
      });
    } else {
      // Mismatched target -> closest-line hint
      bestEffortEdits.push({
        path: `file_${String(i).padStart(2, "0")}.ts`,
        hunks: [{ target: `export const f${i}_v2 = "WRONG_VALUE_${i}";`, replacement: "x" }],
      });
    }
  }
  const beRes = await client.callTool("apply_edits", {
    cwd: dir,
    bestEffort: true,
    edits: bestEffortEdits,
  });
  assert.equal(beRes.isError, true);
  const beBytes = assertInlineSafe(beRes.text, "S3-bestEffort50");
  assert.match(beRes.text, /Partial edit applied \(40\/50 file\(s\) written; 10 file\(s\) failed/);
  assert.ok((await fs.readFile(path.join(dir, "file_39.ts"), "utf8")).includes("best_effort_39"));

  // 3d. Corrupted call_mcp_tool payload matrix
  const corruptMatrix = [
    { label: "[true]", edits: [true] },
    { label: "[false,true]", edits: [false, true] },
    { label: "[null,null,null,null]", edits: [null, null, null, null] },
    { label: "hunks:[true]", edits: [{ path: "file_00.ts", hunks: [true] }] },
    { label: "edits:true", edits: true },
  ];
  for (const c of corruptMatrix) {
    const r = await client.callTool("apply_edits", { cwd: dir, edits: c.edits });
    assert.equal(r.isError, true, `Corrupt case ${c.label} should error`);
    assert.match(r.text, /nested argument serialization corruption/i, `Missing circuit breaker on ${c.label}`);
    assert.match(r.text, /Do NOT retry/i);
    assertInlineSafe(r.text, `S3-corrupt-${c.label}`);
  }

  // 3e. 2 MB noisy verify output filtering
  const noisyScript = [
    `for (let i = 0; i < 4000; i++) process.stdout.write("\\x1b[32m[INFO] compiling module " + i + " " + "x".repeat(450) + "\\x1b[0m\\n");`,
    `console.error("src/billing/ChargeService.ts(42,17): error TS2345: Argument of type 'string' is not assignable to parameter of type 'number'.");`,
    `process.exit(1);`,
  ].join(" ");
  const verifyRes = await client.callTool("apply_edits", {
    cwd: dir,
    edits: [{ path: "file_00.ts", hunks: [{ target: `f0_v3 = "val_0_3"`, replacement: `f0_v3 = "verified"` }] }],
    verify: `${JSON.stringify(process.execPath)} -e ${JSON.stringify(noisyScript)}`,
    maxVerifyChars: 7500,
  });
  assert.equal(verifyRes.isError, false);
  const verifyBytes = assertInlineSafe(verifyRes.text, "S3-verify2MB");
  assert.match(verifyRes.text, /error TS2345/);

  return {
    atomic50Files250HunksMs: res50.ms,
    atomic50Bytes: bytes50,
    rollback50Ms: rollbackRes.ms,
    rollbackIntact: true,
    bestEffort40Pass10FailMs: beRes.ms,
    bestEffortBytes: beBytes,
    verify2MbNoiseMs: verifyRes.ms,
    verify2MbBytes: verifyBytes,
    corruptMatrixCircuitBroken: `${corruptMatrix.length}/${corruptMatrix.length}`,
  };
}

// ---------------------------------------------------------------------------
// Scenario 4: run_checks 12-way concurrency, 5 MB ANSI+CR log flood, grandchild kill
// ---------------------------------------------------------------------------

async function runScenario4RunChecks(client, workDir) {
  const dir = path.join(workDir, "s4-checks");
  await fs.mkdir(dir, { recursive: true });
  const nodeBin = JSON.stringify(process.execPath);

  // 4a. 12 commands x 250ms with concurrency=8 (serial would be ~3000ms; concurrent should be ~550-750ms)
  const cmds12 = Array.from({ length: 12 }, (_, i) => ({
    name: `task-${i + 1}`,
    command: `${nodeBin} -e "setTimeout(() => console.log('done ${i + 1}'), 250)"`,
  }));
  const concRes = await client.callTool("run_checks", {
    cwd: dir,
    concurrency: 8,
    commands: cmds12,
  });
  assert.equal(concRes.isError, false);
  assert.match(concRes.text, /Checks: 12\/12 passed/);
  assertInlineSafe(concRes.text, "S4-conc12");

  // 4b. 5 MB ANSI + carriage-return progress flood across failing & passing commands
  const flood5Mb = [
    `for (let i = 0; i < 8000; i++) process.stdout.write("\\r\\x1b[36mProgress " + i + "/8000 " + "#".repeat(600) + "\\x1b[0m\\n");`,
    `console.error("FATAL: IntegrationCheckFailed at line 999 (中文错误详情)");`,
    `process.exit(1);`,
  ].join(" ");
  const floodCompact = await client.callTool("run_checks", {
    cwd: dir,
    outputMode: "compact",
    commands: [
      { name: "flood-fail", command: `${nodeBin} -e ${JSON.stringify(flood5Mb)}` },
      { name: "quick-ok", command: `${nodeBin} -e "console.log('ALL GREEN')"` },
    ],
  });
  assert.equal(floodCompact.isError, false);
  const floodCompactBytes = assertInlineSafe(floodCompact.text, "S4-flood-compact");
  assert.match(floodCompact.text, /IntegrationCheckFailed/);

  const floodFull = await client.callTool("run_checks", {
    cwd: dir,
    outputMode: "full",
    maxChars: 8800,
    commands: [
      { name: "flood-full", command: `${nodeBin} -e ${JSON.stringify(flood5Mb)}` },
    ],
  });
  assert.equal(floodFull.isError, false);
  const floodFullBytes = assertInlineSafe(floodFull.text, "S4-flood-full");

  // 4c. Grandchild process-tree kill verification on timeout
  const pidFile = path.join(dir, "grandchild.pid");
  const spawnerCode = `
    const { spawn } = require("node:child_process");
    const fs = require("node:fs");
    const gc = spawn(process.execPath, ["-e", "setTimeout(() => {}, 30000)"], { stdio: "ignore" });
    fs.writeFileSync(${JSON.stringify(pidFile)}, String(gc.pid));
    setTimeout(() => {}, 30000);
  `.replace(/\n/g, " ");
  const timeoutRes = await client.callTool("run_checks", {
    cwd: dir,
    commands: [{ name: "hang-tree", command: `${nodeBin} -e ${JSON.stringify(spawnerCode)}`, timeoutSec: 1 }],
  });
  assert.equal(timeoutRes.isError, false);
  assert.match(timeoutRes.text, /TIMEOUT/);

  // Wait 600ms after return for SIGKILL grace and verify grandchild is completely dead
  await new Promise((r) => setTimeout(r, 600));
  let grandchildDead = true;
  if (await fs.access(pidFile).then(() => true).catch(() => false)) {
    const gcPid = Number((await fs.readFile(pidFile, "utf8")).trim());
    if (Number.isFinite(gcPid) && gcPid > 0) {
      try {
        process.kill(gcPid, 0);
        grandchildDead = false;
        try { process.kill(gcPid, "SIGKILL"); } catch {}
      } catch {
        grandchildDead = true;
      }
    }
  }
  assert.equal(grandchildDead, true, "Timed-out grandchild process survived killChildTree!");

  // 4d. Corrupted commands circuit breaker
  const badCmds = await client.callTool("run_checks", { cwd: dir, commands: [true, null] });
  assert.equal(badCmds.isError, true);
  assert.match(badCmds.text, /nested argument serialization corruption/i);

  return {
    concurrent12x250msWallMs: concRes.ms,
    speedupVsSerial3000msX: +(3000 / Math.max(1, concRes.ms)).toFixed(2),
    flood5MbCompactMs: floodCompact.ms,
    flood5MbCompactBytes: floodCompactBytes,
    flood5MbFullBytes: floodFullBytes,
    timeoutWithKillMs: timeoutRes.ms,
    grandchildReaped: grandchildDead,
  };
}

// ---------------------------------------------------------------------------
// Scenario 5: SessionStart Hook Cold/Warm Latency on Large Dirty Monorepo
// ---------------------------------------------------------------------------

async function runScenario5SessionStart(monoDir) {
  const times = [];
  let bytes = 0;
  let chars = 0;
  for (let i = 0; i < 15; i++) {
    const t0 = performance.now();
    const out = execFileSync(process.execPath, [SESSION_START], {
      input: JSON.stringify({ cwd: monoDir, hookEventName: "SessionStart" }),
      encoding: "utf8",
      env: { ...process.env, AGY_FAST_AUTO_UPDATE: "off" },
      timeout: 5000,
    });
    const ms = +(performance.now() - t0).toFixed(2);
    times.push(ms);
    const parsed = JSON.parse(out);
    const ctx =
      parsed?.injectSteps?.[0]?.systemMessage?.systemMessage ??
      parsed?.hookSpecificOutput?.additionalContext ??
      "";
    assert.ok(ctx.includes("[agy-fast startup snapshot]"), "Missing startup snapshot header");
    assert.ok(ctx.length <= 1600, `SessionStart snapshot too large: ${ctx.length} chars`);
    bytes = Buffer.byteLength(ctx, "utf8");
    chars = ctx.length;
  }
  return {
    runs: times.length,
    latencyMs: stats(times),
    snapshotChars: chars,
    snapshotBytes: bytes,
  };
}

// ---------------------------------------------------------------------------
// Scenario 6: MCP Stdio Concurrency Torture Test (NDJSON + Content-Length)
// ---------------------------------------------------------------------------

async function runScenario6McpProtocol(monoDir, workDir) {
  const results = {};
  for (const framing of ["ndjson", "content-length"]) {
    const client = new McpTestClient({ framing });
    await client.start();
    const rssBefore = getProcessRssMb(client.getPid());

    // Fire 40 concurrent mixed requests simultaneously!
    const t0 = performance.now();
    const promises = [];
    for (let i = 0; i < 40; i++) {
      const kind = i % 5;
      if (kind === 0) {
        promises.push(client.callTool("batch_search", {
          cwd: monoDir,
          queries: [{ type: "text", pattern: `computeMetrics_${i * 10}`, fileType: "ts" }],
        }));
      } else if (kind === 1) {
        promises.push(client.callTool("read_slices", {
          cwd: monoDir,
          outlines: [`packages/api/src/routes/mod_${String((i * 10) % FILE_COUNT).padStart(4, "0")}.ts`],
        }));
      } else if (kind === 2) {
        promises.push(client.callTool("run_checks", {
          cwd: workDir,
          commands: [{ name: `p-${i}`, command: `${JSON.stringify(process.execPath)} -e "console.log(${i})"` }],
        }));
      } else if (kind === 3) {
        // Intentional corrupted args in the middle of valid traffic
        promises.push(client.callTool("apply_edits", { cwd: workDir, edits: [true] }));
      } else {
        promises.push(client.request("ping", {}).then((r) => ({ ok: Boolean(r.result), bytes: 2, isError: false })));
      }
    }
    const settled = await Promise.all(promises);
    const totalMs = +(performance.now() - t0).toFixed(1);
    assert.equal(settled.length, 40);

    // Send malformed JSON and verify server returns -32700 Parse error and stays alive
    const parseErr = await client.sendRawFrame("{bad-json", true);
    assert.equal(parseErr?.error?.code, -32700);
    const afterPing = await client.request("ping", {});
    assert.deepEqual(afterPing.result, {});

    const rssAfter = getProcessRssMb(client.getPid());
    assert.equal(client.stderrBuf.trim(), "", `Unexpected stderr output on ${framing}: ${client.stderrBuf}`);
    await client.close();

    results[framing] = {
      concurrent40WallMs: totalMs,
      parseErrorRecovered: true,
      rssBeforeMb: rssBefore,
      rssAfterMb: rssAfter,
      stderrClean: true,
    };
  }
  return results;
}

// ---------------------------------------------------------------------------
// Scenario 7: Cross-Platform Edge Paths (spaces, parens, CJK, 12-deep, symlinks)
// ---------------------------------------------------------------------------

async function runScenario7EdgePaths(client, workDir) {
  const edgeDir = path.join(workDir, "s7 edge (中文 目录)");
  const deepDir = path.join(edgeDir, "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l");
  await fs.mkdir(deepDir, { recursive: true });
  const targetFile = path.join(deepDir, "目标 文件 (v2).ts");
  await fs.writeFile(targetFile, "export function edgeTarget() {\n  return 'EDGE_OK_1';\n}\n", "utf8");

  const linkPath = path.join(edgeDir, "alias-link.ts");
  let symlinkTested = false;
  try {
    await fs.symlink(targetFile, linkPath);
    symlinkTested = true;
  } catch {
    symlinkTested = false;
  }

  const searchRes = await client.callTool("batch_search", {
    cwd: edgeDir,
    queries: [
      { type: "text", pattern: "EDGE_OK_1" },
      { type: "glob", pattern: "**/*.ts" },
      { type: "fuzzy", pattern: "目标" },
    ],
  });
  assert.equal(searchRes.isError, false);
  assert.match(searchRes.text, /EDGE_OK_1/);

  const outlineRes = await client.callTool("read_slices", {
    cwd: edgeDir,
    outlines: ["a/b/c/d/e/f/g/h/i/j/k/l/目标 文件 (v2).ts"],
  });
  assert.equal(outlineRes.isError, false);
  assert.match(outlineRes.text, /edgeTarget/);

  // Simultaneous edit via real path + symlink alias (must merge atomically without clobbering!)
  const editEntries = [
    {
      path: "a/b/c/d/e/f/g/h/i/j/k/l/目标 文件 (v2).ts",
      hunks: [{ target: "EDGE_OK_1", replacement: "EDGE_OK_2" }],
    },
  ];
  if (symlinkTested) {
    editEntries.push({
      path: "alias-link.ts",
      hunks: [{ target: "export function edgeTarget()", replacement: "export function edgeTargetRenamed()" }],
    });
  }
  const editRes = await client.callTool("apply_edits", { cwd: edgeDir, edits: editEntries });
  assert.equal(editRes.isError, false, `Edge path edit failed: ${editRes.text}`);
  const finalContent = await fs.readFile(targetFile, "utf8");
  assert.ok(finalContent.includes("EDGE_OK_2"));
  if (symlinkTested) {
    assert.ok(finalContent.includes("edgeTargetRenamed"), "Symlink alias dedup failed!");
  }

  return {
    deep12LevelsAndCjkSpaces: true,
    symlinkDedupVerified: symlinkTested,
    searchMs: searchRes.ms,
    outlineMs: outlineRes.ms,
    editMs: editRes.ms,
  };
}

// ---------------------------------------------------------------------------
// Scenario 8: 200-request Sustained Concurrency Soak & Memory Stability
// ---------------------------------------------------------------------------

async function runScenario8SoakTest(monoDir, workDir) {
  // Enforce a strict 128 MB V8 old-space cap so any real memory leak triggers OOM or monotonic growth
  const client = new McpTestClient({
    framing: "ndjson",
    nodeArgs: ["--max-old-space-size=128"],
  });
  await client.start();
  const rssStart = getProcessRssMb(client.getPid());
  const waveP95s = [];
  const waveRss = [];
  const totalWaves = 10;

  for (let wave = 0; wave < totalWaves; wave++) {
    const waveTimes = [];
    const reqs = [];
    for (let i = 0; i < 40; i++) {
      const modIdx = String(((wave * 40 + i) * 7) % FILE_COUNT).padStart(4, "0");
      if (i % 4 === 0) {
        reqs.push(
          client.callTool("batch_search", {
            cwd: monoDir,
            queries: [
              { type: "text", pattern: `HOT_STRESS_TOKEN`, outputMode: "count", fileType: "ts", maxResults: 20 },
              { type: "fuzzy", pattern: `mod_${modIdx}`, maxResults: 5 },
            ],
          }),
        );
      } else if (i % 4 === 1) {
        reqs.push(
          client.callTool("read_slices", {
            cwd: monoDir,
            outlines: [`packages/api/src/routes/mod_${String((i * 10) % FILE_COUNT).padStart(4, "0")}.ts`],
          }),
        );
      } else if (i % 4 === 2) {
        reqs.push(
          client.callTool("run_checks", {
            cwd: workDir,
            commands: [{ name: `soak-${wave}-${i}`, command: `${JSON.stringify(process.execPath)} -e "console.log('ok')"` }],
          }),
        );
      } else {
        reqs.push(
          client.callTool("apply_edits", { cwd: workDir, edits: [false, true] }),
        );
      }
    }
    const settled = await Promise.all(reqs);
    for (const r of settled) {
      assertInlineSafe(r.text, `S8-w${wave}`);
      waveTimes.push(r.ms);
    }
    waveP95s.push(percentile(waveTimes, 95));
    const rss = getProcessRssMb(client.getPid());
    if (rss !== null) waveRss.push(rss);
  }

  const rssEnd = getProcessRssMb(client.getPid());
  assert.equal(client.stderrBuf.trim(), "", "Soak test produced stderr noise");
  await client.close();

  const rssWarm = waveRss.length >= 7 ? waveRss[6] : null;
  const steadyDeltaMb = rssWarm !== null && rssEnd !== null ? +(rssEnd - rssWarm).toFixed(1) : null;
  if (steadyDeltaMb !== null) {
    assert.ok(
      steadyDeltaMb < 40,
      `Potential memory leak in 400-req soak: post-warmup RSS grew ${steadyDeltaMb}MB (wave7=${rssWarm}MB -> wave10=${rssEnd}MB)`,
    );
  }
  if (rssEnd !== null) {
    assert.ok(rssEnd < 320, `Unexpectedly high RSS under 128MB heap cap: ${rssEnd}MB`);
  }

  return {
    totalRequests: totalWaves * 40,
    waves: totalWaves,
    waveP95Ms: waveP95s,
    rssStartMb: rssStart,
    rssWarmWave7Mb: rssWarm,
    rssEndMb: rssEnd,
    steadyDeltaMb,
  };
}

// ---------------------------------------------------------------------------
// Scenario 9: Phase-2 Real Disk Rollback + 10,000-line File Edit + Overlap Guard
// ---------------------------------------------------------------------------

async function runScenario9DiskRollbackAndLargeEdit(client, workDir) {
  const dir = path.join(workDir, "s9-disk-rollback");
  await fs.mkdir(dir, { recursive: true });

  // 9a. 10,000-line file multi-hunk edit
  const bigLines = Array.from({ length: 10_000 }, (_, i) => `export const row_${i + 1} = ${i + 1};`);
  const bigPath = path.join(dir, "ten-thousand.ts");
  await fs.writeFile(bigPath, bigLines.join("\n") + "\n", "utf8");
  const bigRes = await client.callTool("apply_edits", {
    cwd: dir,
    edits: [
      {
        path: "ten-thousand.ts",
        hunks: [
          { target: "export const row_1 = 1;", replacement: "export const row_1 = 1001;", startLine: 1, endLine: 3 },
          { target: "export const row_5000 = 5000;", replacement: "export const row_5000 = 15000;", startLine: 4998, endLine: 5002 },
          { target: "export const row_10000 = 10000;", replacement: "export const row_10000 = 20000;", startLine: 9998, endLine: 10000 },
        ],
      },
    ],
  });
  assert.equal(bigRes.isError, false, `10k-line edit failed: ${bigRes.text}`);
  const bigAfter = await fs.readFile(bigPath, "utf8");
  assert.ok(bigAfter.includes("row_1 = 1001;") && bigAfter.includes("row_5000 = 15000;") && bigAfter.includes("row_10000 = 20000;"));

  // 9b. Overlapping hunk conflict must be cleanly rejected with 0 changes
  const overlapRes = await client.callTool("apply_edits", {
    cwd: dir,
    edits: [
      {
        path: "ten-thousand.ts",
        hunks: [
          { target: "export const row_2 = 2;\nexport const row_3 = 3;", replacement: "A" },
          { target: "export const row_3 = 3;\nexport const row_4 = 4;", replacement: "B" },
        ],
      },
    ],
  });
  assert.equal(overlapRes.isError, true);
  assert.match(overlapRes.text, /overlap/i);

  // 9c. Phase-2 real disk-write failure rollback (only enforceable on non-root POSIX)
  let phase2RollbackVerified = false;
  if (process.platform !== "win32" && typeof process.getuid === "function" && process.getuid() !== 0) {
    const f1 = path.join(dir, "p2_existing_1.ts");
    const f2 = path.join(dir, "p2_existing_2.ts");
    const lockedDir = path.join(dir, "locked_dir");
    await fs.writeFile(f1, "const orig = 1;\n", "utf8");
    await fs.writeFile(f2, "const orig = 2;\n", "utf8");
    await fs.chmod(f1, 0o640);
    await fs.mkdir(lockedDir, { recursive: true });
    await fs.chmod(lockedDir, 0o555); // read-only dir: creating a new file inside fails in Phase-2 write!

    try {
      const failRes = await client.callTool("apply_edits", {
        cwd: dir,
        edits: [
          { path: "p2_existing_1.ts", hunks: [{ target: "orig = 1", replacement: "mutated = 1" }] },
          { path: "p2_new_temp.ts", create: "export const created = true;\n" },
          { path: "p2_existing_2.ts", hunks: [{ target: "orig = 2", replacement: "mutated = 2" }] },
          { path: "locked_dir/will_fail_write.ts", create: "export const blocked = true;\n" },
        ],
      });
      assert.equal(failRes.isError, true, "Expected Phase-2 disk write failure");
      assert.equal(await fs.readFile(f1, "utf8"), "const orig = 1;\n", "Phase-2 rollback failed to restore f1!");
      assert.equal(await fs.readFile(f2, "utf8"), "const orig = 2;\n", "Phase-2 rollback failed to restore f2!");
      const mode1 = (await fs.stat(f1)).mode & 0o777;
      assert.equal(mode1, 0o640, `Phase-2 rollback altered file mode: got 0o${mode1.toString(8)}`);
      const tempExists = await fs.access(path.join(dir, "p2_new_temp.ts")).then(() => true).catch(() => false);
      assert.equal(tempExists, false, "Phase-2 rollback left behind newly created p2_new_temp.ts!");
      phase2RollbackVerified = true;
    } finally {
      await fs.chmod(lockedDir, 0o755).catch(() => {});
    }
  }

  return {
    edit10kLineMs: bigRes.ms,
    overlapRejected: true,
    phase2DiskRollbackVerified: phase2RollbackVerified,
  };
}

// ---------------------------------------------------------------------------
// Scenario 10: Red-Team Read-Only Git Guard (20 write / injection payloads)
// ---------------------------------------------------------------------------

async function runScenario10GitSecurity(client, monoDir) {
  const headBefore = execFileSync("git", ["rev-parse", "HEAD"], { cwd: monoDir, encoding: "utf8" }).trim();
  const markerPath = path.join(monoDir, "pwned_marker.txt");

  const hostilePayloads = [
    "commit -m pwn",
    "push origin main",
    "reset --hard HEAD~1",
    "checkout -- .",
    "clean -fd",
    "branch -D main",
    "branch -f hack HEAD",
    "tag -d v1",
    "tag v999",
    "remote add evil https://example.com/a.git",
    "remote set-url origin https://example.com/a.git",
    "stash drop",
    "stash push",
    `diff --output=${markerPath}`,
    `log --output=${markerPath}`,
    "log --exec=id",
    "-c core.pager=sh status",
    "status; touch pwned_marker.txt",
    "status && echo pwned",
    "log $(touch pwned_marker.txt)",
  ];

  const res = await client.callTool("batch_search", {
    cwd: monoDir,
    queries: hostilePayloads.map((p) => ({ type: "git", pattern: p })),
  });
  assert.equal(res.isError, false);
  assertInlineSafe(res.text, "S10-git-security");

  // Every single one of the 20 hostile queries must return ERROR (rejected by allowlist)
  const errCount = (res.text.match(/ERROR:/g) || []).length;
  assert.equal(errCount, hostilePayloads.length, `Expected all ${hostilePayloads.length} hostile git queries blocked, got ${errCount}:\n${res.text}`);
  const headAfter = execFileSync("git", ["rev-parse", "HEAD"], { cwd: monoDir, encoding: "utf8" }).trim();
  assert.equal(headAfter, headBefore, "Git HEAD changed during hostile red-team test!");
  const markerExists = await fs.access(markerPath).then(() => true).catch(() => false);
  assert.equal(markerExists, false, "Hostile git command created side-effect file!");

  return {
    hostilePayloadsTested: hostilePayloads.length,
    blocked: errCount,
    zeroSideEffects: true,
    wallMs: res.ms,
  };
}

// ---------------------------------------------------------------------------
// Scenario 11: All-12-Language Outline Coverage + 4-byte Emoji Truncation Safety
// ---------------------------------------------------------------------------

async function runScenario11AllLanguagesAndEmoji(client, workDir) {
  const dir = path.join(workDir, "s11-all-langs");
  await fs.mkdir(dir, { recursive: true });

  const fixtures = {
    "a.ts": "export interface Alpha { id: string }\nexport class AlphaSvc { run() {} }\n",
    "b.js": "export function betaFn() { return 1; }\n",
    "c.vue": "<template><div/></template>\n<script setup>\nfunction vueFn() {}\n</script>\n<style>.x{color:red}</style>\n",
    "d.svelte": "<script>\nexport function svelteFn() {}\n</script>\n<main>Hi</main>\n<style>.s{}</style>\n",
    "e.css": ":root { --a: 1; }\n@media (max-width: 600px) { .card { padding: 8px; } }\n",
    "f.py": "class PyCls:\n    def py_method(self):\n        return 1\n",
    "g.gd": "class_name GdActor\nsignal hit\nfunc gd_tick():\n\tpass\n",
    "h.go": "package main\ntype GoCfg struct{}\nfunc GoRun() {}\n",
    "i.rs": "pub struct RsItem {}\npub fn rs_exec() {}\n",
    "j.java": "public class JavaSvc { public void javaRun() {} }\n",
    "k.md": "# Title\n## SubSection\n### Detail\n",
    "l.json": '{\n  "name": "demo",\n  "scripts": { "build": "tsc" }\n}\n',
    "m.yaml": "service:\n  name: demo\n  port: 8080\n",
    "n.sh": "deploy_all() {\n  echo hi\n}\n",
  };
  await Promise.all(
    Object.entries(fixtures).map(([f, c]) => fs.writeFile(path.join(dir, f), c, "utf8")),
  );

  const allRes = await client.callTool("read_slices", {
    cwd: dir,
    outlines: Object.keys(fixtures),
  });
  assert.equal(allRes.isError, false);
  const allBytes = assertInlineSafe(allRes.text, "S11-all-14-files");
  for (const sym of ["AlphaSvc", "betaFn", "vueFn", "svelteFn", "PyCls", "GdActor", "GoRun", "rs_exec", "JavaSvc", "SubSection", "deploy_all"]) {
    assert.match(allRes.text, new RegExp(sym), `Missing outline symbol ${sym}`);
  }

  // 4-byte ZWJ / surrogate-pair emoji boundary truncation torture across tight budgets
  const emojiFile = path.join(dir, "emoji-wall.md");
  const emojiLines = Array.from(
    { length: 250 },
    (_, i) => `## 节点${i} 👨‍👩‍👧‍👦🚀🔥🧪𠀀_${"🎯中文".repeat(8)}`,
  );
  await fs.writeFile(emojiFile, emojiLines.join("\n"), "utf8");
  for (const budget of [1000, 1500, 2345, 4096, 8800]) {
    const r = await client.callTool("read_slices", {
      cwd: dir,
      maxChars: budget,
      slices: [{ path: "emoji-wall.md", ranges: [[1, 250]] }],
    });
    assert.equal(r.isError, false);
    assertInlineSafe(r.text, `S11-emoji-${budget}`);
  }

  return {
    languagesVerified: Object.keys(fixtures).length,
    allOutlineMs: allRes.ms,
    allOutlineBytes: allBytes,
    emojiBudgetsSafe: 5,
  };
}

// ---------------------------------------------------------------------------
// Optional Scenario 12: Real-Repository Live Read-Only Benchmark (--real-repo=<path>)
// ---------------------------------------------------------------------------

async function runScenario12RealRepo(client, repoPath) {
  const abs = path.resolve(repoPath);
  const searchRes = await client.callTool("batch_search", {
    cwd: abs,
    queries: [
      { type: "git", pattern: "status --short" },
      { type: "git", pattern: "log -n 5 --oneline" },
      { type: "glob", pattern: "**/*", maxResults: 80 },
      { type: "text", pattern: "export", outputMode: "files_with_matches", maxResults: 60 },
      { type: "text", pattern: "function", outputMode: "count", maxResults: 40 },
    ],
  });
  assert.equal(searchRes.isError, false);
  const searchBytes = assertInlineSafe(searchRes.text, "S12-real-search");

  const t0 = performance.now();
  const ssOut = execFileSync(process.execPath, [SESSION_START], {
    input: JSON.stringify({ cwd: abs, hookEventName: "SessionStart" }),
    encoding: "utf8",
    env: { ...process.env, AGY_FAST_AUTO_UPDATE: "off" },
    timeout: 5000,
  });
  const ssMs = +(performance.now() - t0).toFixed(2);
  const ssParsed = JSON.parse(ssOut);
  const ssCtx = ssParsed?.injectSteps?.[0]?.systemMessage?.systemMessage ?? "";

  return {
    repo: path.basename(abs),
    batchSearch5qMs: searchRes.ms,
    batchSearchBytes: searchBytes,
    sessionStartMs: ssMs,
    sessionStartBytes: Buffer.byteLength(ssCtx, "utf8"),
  };
}

// ---------------------------------------------------------------------------
// Main Runner
// ---------------------------------------------------------------------------

async function main() {
  const totalStart = performance.now();
  const workDir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-fast-stress-"));
  const monoDir = path.join(workDir, "monorepo");
  await fs.mkdir(monoDir, { recursive: true });

  const client = new McpTestClient({ framing: "ndjson" });
  // Empty PATH forces Node.js fallback (no rg binary discoverable)
  const nodeFallbackClient = new McpTestClient({
    framing: "ndjson",
    env: { ...process.env, PATH: "" },
  });

  try {
    const setupMs = await buildLargeMonorepo(monoDir);
    await client.start();
    await nodeFallbackClient.start();

    const report = {
      timestamp: new Date().toISOString(),
      scale: HEAVY ? "heavy" : "standard",
      nodeVersion: process.version,
      platform: `${process.platform}/${process.arch}`,
      fixtureSetupMs: setupMs,
      scenarios: {},
    };

    report.scenarios.s1_batch_search_monorepo = await runScenario1BatchSearch(client, nodeFallbackClient, monoDir);
    report.scenarios.s2_read_slices_huge_and_redos = await runScenario2ReadSlices(client, workDir);
    report.scenarios.s3_apply_edits_50_atomic_and_corrupt = await runScenario3ApplyEdits(client, workDir);
    report.scenarios.s4_run_checks_flood_and_kill = await runScenario4RunChecks(client, workDir);
    report.scenarios.s5_session_start_latency = await runScenario5SessionStart(monoDir);
    report.scenarios.s6_mcp_stdio_concurrency = await runScenario6McpProtocol(monoDir, workDir);
    report.scenarios.s7_edge_paths_and_symlinks = await runScenario7EdgePaths(client, workDir);
    report.scenarios.s8_soak_200_requests = await runScenario8SoakTest(monoDir, workDir);
    report.scenarios.s9_disk_rollback_and_10k_file = await runScenario9DiskRollbackAndLargeEdit(client, workDir);
    report.scenarios.s10_git_security_redteam = await runScenario10GitSecurity(client, monoDir);
    report.scenarios.s11_all_languages_and_emoji = await runScenario11AllLanguagesAndEmoji(client, workDir);
    if (REAL_REPO_ARG) {
      report.scenarios.s12_real_repo = await runScenario12RealRepo(client, REAL_REPO_ARG);
    }
    report.totalElapsedSec = +((performance.now() - totalStart) / 1000).toFixed(2);

    if (JSON_MODE) {
      console.log(JSON.stringify(report, null, 2));
    } else {
      const s1 = report.scenarios.s1_batch_search_monorepo;
      const s2 = report.scenarios.s2_read_slices_huge_and_redos;
      const s3 = report.scenarios.s3_apply_edits_50_atomic_and_corrupt;
      const s4 = report.scenarios.s4_run_checks_flood_and_kill;
      const s5 = report.scenarios.s5_session_start_latency;
      const s6 = report.scenarios.s6_mcp_stdio_concurrency;
      const s7 = report.scenarios.s7_edge_paths_and_symlinks;
      const s8 = report.scenarios.s8_soak_200_requests;
      const s9 = report.scenarios.s9_disk_rollback_and_10k_file;
      const s10 = report.scenarios.s10_git_security_redteam;
      const s11 = report.scenarios.s11_all_languages_and_emoji;
      const s12 = report.scenarios.s12_real_repo;

      console.log(`# agy-fast Stress Benchmark (${report.scale}, ${report.platform}, Node ${report.nodeVersion})`);
      console.log(`Fixture: ${s1.files} files (${FILE_COUNT} source + ${NOISE_COUNT} node_modules noise) built in ${setupMs}ms\n`);
      console.log("| Scenario | Key Measurement | Status |");
      console.log("| :--- | :--- | :---: |");
      console.log(`| S1: batch_search 20-query monorepo (rg) | P50=${s1.rgBatch20Ms.p50}ms, P95=${s1.rgBatch20Ms.p95}ms vs serial ${s1.serial20LocalMs}ms (${s1.localSpeedupX}x local; ~${s1.estWithRemoteCheckSec.batch1}s vs ~${s1.estWithRemoteCheckSec.serial20}s w/ 1.8s check); flood=${s1.floodMaxBytes}B | PASS |`);
      console.log(`| S1b: batch_search Node fallback (no rg) | 3 mixed queries (incl. multiline+fuzzy) in ${s1.nodeFallback3qMs}ms, 0 noise leak | PASS |`);
      console.log(`| S2: read_slices 5x5000L + 40 dense + ReDoS | 5 huge=${s2.huge5Files24kLinesMs}ms (${s2.huge5Bytes}B); 40 dense=${s2.dense40FilesMs}ms (${s2.dense40Bytes}B); ReDoS=${s2.pathologicalReDoSMs}ms; 30 slices=${s2.slice30FilesMs}ms (${s2.slice30Bytes}B) | PASS |`);
      console.log(`| S3: apply_edits 50-file atomic/rollback/corrupt | 50 files/250 hunks=${s3.atomic50Files250HunksMs}ms (${s3.atomic50Bytes}B); rollback=${s3.rollback50Ms}ms; bestEffort 40/50=${s3.bestEffort40Pass10FailMs}ms (${s3.bestEffortBytes}B); 2MB verify=${s3.verify2MbBytes}B; corrupt=${s3.corruptMatrixCircuitBroken} | PASS |`);
      console.log(`| S4: run_checks 12-way + 5MB flood + kill | 12x250ms=${s4.concurrent12x250msWallMs}ms (${s4.speedupVsSerial3000msX}x); 5MB compact=${s4.flood5MbCompactMs}ms (${s4.flood5MbCompactBytes}B); full=${s4.flood5MbFullBytes}B; timeout=${s4.timeoutWithKillMs}ms, grandchildReaped=${s4.grandchildReaped} | PASS |`);
      console.log(`| S5: SessionStart 15x on dirty monorepo | P50=${s5.latencyMs.p50}ms, P95=${s5.latencyMs.p95}ms, Max=${s5.latencyMs.max}ms (${s5.snapshotChars} chars / ${s5.snapshotBytes}B) | PASS |`);
      console.log(`| S6: MCP stdio 40x concurrent (both framings) | ndjson=${s6.ndjson.concurrent40WallMs}ms (RSS ${s6.ndjson.rssBeforeMb}->${s6.ndjson.rssAfterMb}MB); content-length=${s6["content-length"].concurrent40WallMs}ms (RSS ${s6["content-length"].rssBeforeMb}->${s6["content-length"].rssAfterMb}MB) | PASS |`);
      console.log(`| S7: Edge paths (12-deep, CJK, spaces, symlink) | search=${s7.searchMs}ms, outline=${s7.outlineMs}ms, edit=${s7.editMs}ms, symlinkDedup=${s7.symlinkDedupVerified} | PASS |`);
      console.log(`| S8: 400-req soak (128MB heap cap) | wave P95s=[${s8.waveP95Ms.join(", ")}]ms; RSS ${s8.rssStartMb}->w7:${s8.rssWarmWave7Mb}->w10:${s8.rssEndMb}MB (steady delta ${s8.steadyDeltaMb}MB) | PASS |`);
      console.log(`| S9: Phase-2 disk rollback + 10k-line edit | 10k-line 3-hunk=${s9.edit10kLineMs}ms; overlapRejected=${s9.overlapRejected}; phase2DiskRollback=${s9.phase2DiskRollbackVerified} | PASS |`);
      console.log(`| S10: Read-only git red-team (20 attacks) | blocked=${s10.blocked}/${s10.hostilePayloadsTested} in ${s10.wallMs}ms, zeroSideEffects=${s10.zeroSideEffects} | PASS |`);
      console.log(`| S11: All 14 file formats + 4-byte emoji truncation | 14 formats=${s11.allOutlineMs}ms (${s11.allOutlineBytes}B); 5 tight emoji budgets 0 lone surrogates | PASS |`);
      if (s12) {
        console.log(`| S12: Real repo live benchmark (${s12.repo}) | 5-query batch_search=${s12.batchSearch5qMs}ms (${s12.batchSearchBytes}B); SessionStart=${s12.sessionStartMs}ms (${s12.sessionStartBytes}B) | PASS |`);
      }
      const totalCount = s12 ? 12 : 11;
      console.log(`\nAll ${totalCount} stress scenarios PASSED in ${report.totalElapsedSec}s (every inline response <= ${INLINE_MAX_BYTES} UTF-8 bytes, 0 lone surrogates).`);
    }
  } finally {
    await client.close();
    await nodeFallbackClient.close();
    await fs.rm(workDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error("STRESS BENCHMARK FAILED:", err);
  process.exit(1);
});
