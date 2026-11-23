import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";
import { extractSymbols, runReadSlices } from "../src/tools/read-slices.js";

describe("read_slices", () => {
  let tmpDir: string;

  before(async () => {
    tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-fast-slices-"));

    await fs.writeFile(
      path.join(tmpDir, "sample.ts"),
      [
        "export interface Config {",
        "  port: number;",
        "}",
        "",
        "export class Server {",
        "  start() {",
        "    return '{not a brace close}';",
        "  }",
        "",
        "  async stop(force: boolean): Promise<void> {",
        "    if (force) {",
        "      return;",
        "    }",
        "  }",
        "}",
        "",
        "export const createApp = (cfg: Config) => {",
        "  return new Server();",
        "};",
      ].join("\n"),
      "utf8",
    );

    await fs.writeFile(
      path.join(tmpDir, "sample.py"),
      [
        "class Worker:",
        "    def run(self):",
        "        return 42",
        "",
        "async def helper():",
        "    return True",
      ].join("\n"),
      "utf8",
    );

    await fs.writeFile(
      path.join(tmpDir, "sample.go"),
      [
        "package main",
        "",
        "type Engine struct {",
        "  Name string",
        "}",
        "",
        "func (e *Engine) Start() {",
        "  println(e.Name)",
        "}",
      ].join("\n"),
      "utf8",
    );

    await fs.writeFile(
      path.join(tmpDir, "sample.rs"),
      [
        "pub struct Cache {",
        "  size: usize,",
        "}",
        "",
        "impl Cache {",
        "  pub fn new(size: usize) -> Self {",
        "    Self { size }",
        "  }",
        "}",
      ].join("\n"),
      "utf8",
    );

    await fs.writeFile(
      path.join(tmpDir, "README.md"),
      [
        "# Title",
        "Intro line.",
        "```bash",
        "# not a heading",
        "```",
        "## Usage",
        "Details here.",
      ].join("\n"),
      "utf8",
    );
  });

  after(async () => {
    await fs.rm(tmpDir, { recursive: true, force: true });
  });

  it("extracts accurate multi-language symbol outlines with line spans", async () => {
    const tsSyms = extractSymbols(
      "sample.ts",
      await fs.readFile(path.join(tmpDir, "sample.ts"), "utf8"),
    );
    assert.deepEqual(
      tsSyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      [
        "interface:Config:1-3",
        "class:Server:5-15",
        "method:start:6-8",
        "method:stop:10-14",
        "function:createApp:17-19",
      ],
    );

    const pySyms = extractSymbols(
      "sample.py",
      await fs.readFile(path.join(tmpDir, "sample.py"), "utf8"),
    );
    assert.deepEqual(
      pySyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      [
        "class:Worker:1-3",
        "method:run:2-3",
        "function:helper:5-6",
      ],
    );

    const goSyms = extractSymbols(
      "sample.go",
      await fs.readFile(path.join(tmpDir, "sample.go"), "utf8"),
    );
    assert.equal(goSyms.length, 2);
    assert.equal(goSyms[0]?.name, "Engine");
    assert.equal(goSyms[1]?.kind, "method");

    const rsSyms = extractSymbols(
      "sample.rs",
      await fs.readFile(path.join(tmpDir, "sample.rs"), "utf8"),
    );
    assert.ok(rsSyms.some((s) => s.kind === "struct" && s.name === "Cache"));
    assert.ok(rsSyms.some((s) => s.kind === "fn" && s.name === "new"));

    const mdSyms = extractSymbols(
      "README.md",
      await fs.readFile(path.join(tmpDir, "README.md"), "utf8"),
    );
    assert.deepEqual(
      mdSyms.map((s) => s.name),
      ["Title", "Usage"],
    );
  });

  it("returns combined outlines and multi-range slices with clamping and bounds handling", async () => {
    const res = await runReadSlices({
      cwd: tmpDir,
      outlines: ["sample.ts", "sample.py"],
      slices: [
        {
          path: "sample.ts",
          ranges: [
            [1, 3],
            [17, 50], // clamped to EOF (19)
            [99, 120], // out of bounds
          ],
        },
        {
          path: "missing.ts",
          ranges: [[1, 5]],
        },
      ],
    });

    assert.equal(res.isError, false);
    assert.match(res.text, /## Outline: sample\.ts \(19 lines\)/);
    assert.match(res.text, /L5-L15 \[class\] Server/);
    assert.match(res.text, /--- L1-L3 \(of 19\) ---/);
    assert.match(res.text, /1: export interface Config \{/);
    assert.match(res.text, /--- L17-L19 \(of 19\) ---/);
    assert.match(res.text, /out of bounds; file has 19 lines/);
    assert.match(res.text, /## Slice: missing\.ts\nERROR:/);
  });

  it("rejects empty requests", async () => {
    const res = await runReadSlices({ cwd: tmpDir });
    assert.equal(res.isError, true);
  });

  it("handles regex/comment braces, multiline TS methods, Java/C functions, Python docstrings, and 4-space JSON", async () => {
    const tsComplex = [
      "export class Multi {",
      "  async send(",
      "    payload: string,",
      "  ): Promise<void> {",
      "    const re = /\\{not_a_brace\\}/;",
      "    /* { block comment brace }",
      "       another line { */",
      "    return;",
      "  }",
      "}",
      "",
      "export const build = (",
      "  a: number,",
      "  b: number,",
      ") => {",
      "  return a + b;",
      "};",
    ].join("\n");
    const tsSyms = extractSymbols("complex.ts", tsComplex);
    assert.deepEqual(
      tsSyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      [
        "class:Multi:1-10",
        "method:send:2-9",
        "function:build:12-17",
      ],
    );

    const javaSyms = extractSymbols(
      "Service.java",
      [
        "public class Service {",
        "  public int add(int a, int b) {",
        "    return a + b;",
        "  }",
        "}",
      ].join("\n"),
    );
    assert.deepEqual(
      javaSyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["class:Service:1-5", "method:add:2-4"],
    );

    const cSyms = extractSymbols(
      "main.c",
      [
        "int main(int argc, char **argv) {",
        "  return 0;",
        "}",
        "static void helper(void) {",
        "}",
      ].join("\n"),
    );
    assert.deepEqual(
      cSyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["function:main:1-3", "function:helper:4-5"],
    );

    const pySyms = extractSymbols(
      "doc.py",
      [
        "def foo():",
        '    """',
        "Column 0 docstring line",
        '    """',
        "    return 1",
        "",
        "def bar():",
        "    return 2",
      ].join("\n"),
    );
    assert.deepEqual(
      pySyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["function:foo:1-5", "function:bar:7-8"],
    );

    const jsonSyms = extractSymbols(
      "pkg.json",
      [
        "{",
        '    "name": "demo",',
        '    "scripts": {',
        '        "test": "node --test"',
        "    }",
        "}",
      ].join("\n"),
    );
    assert.deepEqual(
      jsonSyms.map((s) => `${s.kind}:${s.name}`),
      ["key:name", "key:scripts"],
    );
  });

  it("ends Python symbols after multi-line signatures and includes decorators", () => {
    const pySyms = extractSymbols(
      "model.py",
      [
        "class Model:",
        "    def __init__(",
        "        self,",
        "        size: int,",
        "    ) -> None:",
        "        self.size = size",
        "        self.table = {",
        "    'k': 1,",
        "        }",
        "",
        "    @property",
        "    @functools.lru_cache(",
        "        maxsize=8,",
        "    )",
        "    def area(self) -> int:",
        "        return self.size \\",
        "* 2",
        "",
        "# --- helpers ---",
        "",
        "def build(",
        "    size: int,",
        "):",
        "    return Model(size)",
      ].join("\n"),
    );
    assert.deepEqual(
      pySyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      [
        "class:Model:1-17",
        "method:__init__:2-9",
        "method:area:11-17",
        "function:build:21-24",
      ],
    );

    // Comment between stacked decorators still preserves the full decorator range
    const commentBetweenDecos = extractSymbols(
      "decos.py",
      [
        "@outer",
        "# keep outer decorator attached",
        "@inner",
        "def run():",
        '    """',
        "# not a trailing comment",
        '    """',
        "    return 1",
      ].join("\n"),
    );
    assert.deepEqual(
      commentBetweenDecos.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["function:run:1-8"],
    );

    // Backslash continuation inside a quoted string must not split the function or emit fake symbols
    const quotedBackslash = extractSymbols(
      "str_cont.py",
      [
        "def f():",
        '    s = "hello \\',
        'def fake(): pass" + (',
        "        1",
        "    )",
        "    return s",
        "",
        "def g():",
        "    return 2",
      ].join("\n"),
    );
    assert.deepEqual(
      quotedBackslash.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["function:f:1-6", "function:g:8-9"],
    );

    // Python 3.12+ PEP 701 f-strings (same-quote reuse, multiline expr, quote fill format spec)
    const pep701 = extractSymbols(
      "pep701.py",
      [
        "def first(s):",
        '    a = f"{s.split("(")[0]}"',
        '    b = f"{s:\'>10}"',
        '    c = f"a {(',
        "        1 + 2",
        '    )} b"',
        "    return a, b, c",
        "",
        "def second():",
        "    return 2",
      ].join("\n"),
    );
    assert.deepEqual(
      pep701.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["function:first:1-7", "function:second:9-10"],
    );

    // Mid-edit unclosed bracket recovers on subsequent def/class (including decorator)
    const midEdit = extractSymbols(
      "midedit.py",
      [
        "def editing():",
        "    x = compute(a, b",
        "",
        "@decorator",
        "def intact_after():",
        "    return 1",
      ].join("\n"),
    );
    assert.deepEqual(
      midEdit.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["function:editing:1-2", "function:intact_after:4-6"],
    );
  });

  it("extracts Vue SFC blocks, CSS structure, TS no-semi types, multiline object-param functions, and Fastify routes", () => {
    const vueSyms = extractSymbols(
      "App.vue",
      [
        '<script setup lang="ts">',
        "interface NavItem { label: string }",
        "function toggleMenu() {",
        "  return true",
        "}",
        "</script>",
        "",
        "<template>",
        "  <header class=\"top-bar\">",
        "    <template #slotA>nested</template>",
        "  </header>",
        ...Array.from({ length: 32 }, (_, i) => `  <div>row ${i}</div>`),
        "  <!-- Modal Section -->",
        "  <ConfirmModal />",
        "</template>",
        "",
        "<style scoped>",
        "/* Layout */",
        "@media (max-width: 768px) {",
        "  .top-bar { display: none; }",
        "}",
        "</style>",
      ].join("\n"),
    );
    assert.ok(vueSyms.some((s) => s.kind === "script" && s.startLine === 1 && s.endLine === 6));
    assert.ok(vueSyms.some((s) => s.kind === "function" && s.name === "toggleMenu" && s.startLine === 3));
    assert.ok(vueSyms.some((s) => s.kind === "template" && s.startLine === 8 && s.endLine === 46));
    assert.ok(vueSyms.some((s) => s.kind === "style" && s.startLine === 48 && s.endLine === 53));
    assert.ok(vueSyms.some((s) => s.kind === "@media" && s.startLine === 50));

    const cssSyms = extractSymbols(
      "base.css",
      [
        "/* === Design Tokens === */",
        ":root {",
        "  --accent: #111;",
        "}",
        '[data-theme="dark"] {',
        "  --accent: #fff;",
        "}",
        "@keyframes fadeIn {",
        "  from { opacity: 0; }",
        "  to { opacity: 1; }",
        "}",
        "@media (max-width: 900px) {",
        "  .grid { display: block; }",
        "}",
      ].join("\n"),
    );
    assert.ok(cssSyms.some((s) => s.kind === "section" && s.name === "Design Tokens"));
    assert.ok(cssSyms.some((s) => s.kind === "tokens" && s.name === ":root" && s.endLine === 4));
    assert.ok(cssSyms.some((s) => s.kind === "tokens" && s.name.includes("data-theme") && s.endLine === 7));
    assert.ok(cssSyms.some((s) => s.kind === "@keyframes" && s.name === "fadeIn"));
    assert.ok(cssSyms.some((s) => s.kind === "@media"));

    const tsNoSemiSyms = extractSymbols(
      "runtime.ts",
      [
        "export type RuntimeMode = 'simulation' | 'live'",
        "",
        "export interface EnvConfig {",
        "  dbPath: string",
        "}",
        "",
        "export async function startServer(",
        "  options: { port: number; host?: string },",
        "): Promise<{ ok: boolean }> {",
        ...Array.from({ length: 42 }, (_, i) => `  const line${i} = ${i}`),
        "  app.get('/api/health', async () => {",
        "    return { ok: true }",
        "  })",
        "  return { ok: true }",
        "}",
      ].join("\n"),
    );
    const modeSym = tsNoSemiSyms.find((s) => s.name === "RuntimeMode");
    const fnSym = tsNoSemiSyms.find((s) => s.name === "startServer");
    const routeSym = tsNoSemiSyms.find((s) => s.kind === "route" && s.name === "GET /api/health");
    assert.ok(modeSym && modeSym.startLine === 1 && modeSym.endLine === 1, `Bad type span: ${JSON.stringify(modeSym)}`);
    assert.ok(fnSym && fnSym.startLine === 7 && fnSym.endLine === 56, `Bad fn span: ${JSON.stringify(fnSym)}`);
    assert.ok(routeSym && routeSym.startLine === 52 && routeSym.endLine === 54, `Bad route: ${JSON.stringify(routeSym)}`);
  });

  it("supports up to 40 outlines with multi-file fairness and hard-caps explicit maxChars <= 8800 UTF-8 bytes", async () => {
    const manyDir = path.join(tmpDir, "many-outlines");
    await fs.mkdir(manyDir, { recursive: true });
    const paths: string[] = [];
    for (let i = 0; i < 24; i++) {
      const rel = `many-outlines/f${i}.ts`;
      paths.push(rel);
      const body =
        i === 0
          ? [
              "export class HugeStore {",
              ...Array.from({ length: 60 }, (_, m) => `  method${m}() { return ${m}; }`),
              "}",
              "export function tailHelper() { return 1; }",
            ].join("\n")
          : `export function fn${i}() { return ${i}; }\n`;
      await fs.writeFile(path.join(tmpDir, rel), body, "utf8");
    }

    const outlineRes = await runReadSlices({ cwd: tmpDir, outlines: paths });
    assert.equal(outlineRes.isError, false);
    assert.ok(Buffer.byteLength(outlineRes.text, "utf8") <= 8_800);
    // Depth-0 tailHelper must survive even though HugeStore has 60 methods
    assert.match(outlineRes.text, /tailHelper/);
    assert.match(outlineRes.text, /fn23/);
    assert.match(outlineRes.text, /\(\+52 more symbols\)/);

    const tooMany = await runReadSlices({
      cwd: tmpDir,
      outlines: Array.from({ length: 41 }, () => paths[0]!),
    });
    assert.equal(tooMany.isError, true);
    assert.match(tooMany.text, /max 40 outlines/);

    // Explicit maxChars: 42000 must still stay <= 8800 bytes and surface read_file tip
    const bigSliceFile = path.join(tmpDir, "big-slice.ts");
    await fs.writeFile(
      bigSliceFile,
      Array.from({ length: 300 }, (_, i) => `export const row_${i} = "${"中文测试".repeat(10)}";`).join("\n"),
      "utf8",
    );
    const capped = await runReadSlices({
      cwd: tmpDir,
      slices: [{ path: "big-slice.ts", ranges: [[1, 300]] }],
      maxChars: 42_000,
    });
    assert.equal(capped.isError, false);
    assert.ok(
      Buffer.byteLength(capped.text, "utf8") <= 8_800,
      `Expected <= 8800B, got ${Buffer.byteLength(capped.text, "utf8")}`,
    );
    assert.match(capped.text, /use parallel read_file/);

    // Outlining >20 dense files stays <= 8800 bytes and surfaces batch-size tip when over budget
    const denseDir = path.join(tmpDir, "dense-outlines");
    await fs.mkdir(denseDir, { recursive: true });
    const densePaths: string[] = [];
    for (let i = 0; i < 30; i++) {
      const rel = `dense-outlines/module_${String(i).padStart(2, "0")}.ts`;
      densePaths.push(rel);
      const body = Array.from(
        { length: 25 },
        (_, k) => `export function handler_${i}_${k}_veryLongSymbolNameForBudgetTest() { return ${k}; }`,
      ).join("\n");
      await fs.writeFile(path.join(tmpDir, rel), body, "utf8");
    }
    const denseRes = await runReadSlices({ cwd: tmpDir, outlines: densePaths });
    assert.equal(denseRes.isError, false);
    assert.ok(
      Buffer.byteLength(denseRes.text, "utf8") <= 8_800,
      `Expected <= 8800B, got ${Buffer.byteLength(denseRes.text, "utf8")}`,
    );
    assert.match(denseRes.text, /module_29\.ts/);
    assert.match(denseRes.text, /batch 10-20 files per call/);
  });

  it("extracts Godot GDScript (.gd) outlines across comments, triple strings, nested inner classes, multiline enums, and Windows BOM/CRLF", async () => {
    const gdLines = [
      "\uFEFF@tool",
      "class_name CharacterController extends CharacterBody2D",
      "## Base controller for playable characters.",
      "",
      "signal health_changed(current: int, maximum: int)",
      "signal combo_triggered(",
      "\tcombo_name: StringName,",
      "\thit_count: int,",
      ")",
      "",
      "enum MoveState { IDLE, RUN, AIR }",
      "",
      "enum DamageType {",
      "\tPHYSICAL = 0,",
      "\tMAGICAL = 1, # ignores { fake } braces in comment",
      "\tTRUE_DMG = 2,",
      "}",
      "",
      "enum {",
      "\tLAYER_WORLD = 1,",
      "\tLAYER_ACTOR = 2,",
      "}",
      "",
      'const SHADER_STUB := """',
      "class FakeShaderClass:",
      "\tfunc fake_func():",
      "\t\tpass",
      '"""',
      "",
      "const DOC_SINGLE := '''",
      "signal fake_signal",
      "enum FakeEnum { A, B }",
      "'''",
      "",
      "# func commented_out_func():",
      "# \tpass",
      "",
      "static func calc_mitigation(raw: float, armor: float) -> float:",
      "\treturn raw * (100.0 / (100.0 + armor))",
      "",
      "## Applies damage to this character.",
      '@rpc("any_peer", "call_local")',
      "func apply_damage(",
      "\tamount: int,",
      "\tkind: DamageType = DamageType.PHYSICAL,",
      ") -> void:",
      '\tvar note := """',
      "unindented multiline text inside function",
      '"""',
      "\tif amount > 0:",
      "\t\thealth_changed.emit(0, 100)",
      "",
      "class StateMachine:",
      "\tsignal state_entered(state_name: StringName)",
      "",
      "\tclass SubState:",
      "\t\tfunc tick(delta: float) -> void:",
      "\t\t\tpass",
      "",
      "\t## Transitions to target state.",
      "\tfunc transition_to(target: StringName) -> void:",
      "\t\tstate_entered.emit(target)",
    ];

    const gdContentCrlf = gdLines.join("\r\n");
    const gdSyms = extractSymbols("player.gd", gdContentCrlf);
    assert.deepEqual(
      gdSyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}:d${s.depth}`),
      [
        "class_name:CharacterController:2-2:d0",
        "signal:health_changed:5-5:d0",
        "signal:combo_triggered:6-9:d0",
        "enum:MoveState:11-11:d0",
        "enum:DamageType:13-17:d0",
        "enum:<anonymous>:19-22:d0",
        "function:calc_mitigation:38-39:d0",
        "function:apply_damage:43-51:d0",
        "class:StateMachine:53-62:d0",
        "signal:state_entered:54-54:d1",
        "class:SubState:56-58:d1",
        "method:tick:57-58:d2",
        "method:transition_to:61-62:d1",
      ],
    );

    // Also verify BOM on line 1 inline `@tool class_name` + 4-space indentation + runReadSlices formatting
    const spaceGdPath = path.join(tmpDir, "inventory.gd");
    await fs.writeFile(
      spaceGdPath,
      [
        "\uFEFF@tool class_name Inventory",
        "class Slot:",
        "    static func empty() -> Slot:",
        "        return Slot.new()",
        "",
        '@rpc("authority") func sync_slots() -> void:',
        "    pass",
      ].join("\r\n"),
      "utf8",
    );

    const sliceRes = await runReadSlices({
      cwd: tmpDir,
      outlines: ["inventory.gd"],
    });
    assert.equal(sliceRes.isError, false);
    assert.match(sliceRes.text, /L1 \[class_name\] Inventory/);
    assert.match(sliceRes.text, /L2-L4 \[class\] Slot/);
    assert.match(sliceRes.text, /  L3-L4 \[method\] empty/);
    assert.match(sliceRes.text, /L6-L7 \[function\] sync_slots/);
  });

  it("circuit-breaks corrupted outlines/slices arguments cleanly without throwing", async () => {
    const badOutlines = await runReadSlices({
      cwd: tmpDir,
      outlines: [true, null] as unknown as string[],
    });
    assert.equal(badOutlines.isError, true);
    assert.match(badOutlines.text, /nested argument serialization corruption/i);
    assert.match(badOutlines.text, /outlines\[0\]: received boolean \(true\)/);

    const badSlices = await runReadSlices({
      cwd: tmpDir,
      slices: [false] as unknown as never[],
    });
    assert.equal(badSlices.isError, true);
    assert.match(badSlices.text, /slices\[0\]: received boolean \(false\)/);
    assert.match(badSlices.text, /read_file/);

    const stringified = await runReadSlices({
      cwd: tmpDir,
      outlines: JSON.stringify(["inventory.gd"]) as unknown as string[],
    });
    assert.equal(stringified.isError, false);
    assert.match(stringified.text, /L1 \[class_name\] Inventory/);
  });

  it("handles JSX tags, TS template literals, Python multiline signatures, Rust lifetimes, Shell ${#var}, Go raw strings, non-finite maxChars, and lone surrogates", async () => {
    // 1. TSX closing </Tag> and self-closing /> must not enter regex mode and leak brace depth
    const tsxClose = extractSymbols(
      "App.tsx",
      [
        "export function A({ xs }: { xs: string[] }) {",
        "  return <ul>{xs.map((x) => <li>{x}</li>)}</ul>;",
        "}",
        "export function B() { return <div />; }",
      ].join("\n"),
    );
    assert.deepEqual(
      tsxClose.map((s) => `${s.name}:${s.startLine}-${s.endLine}`),
      ["A:1-3", "B:4-4"],
    );

    const tsxSelfClose = extractSymbols(
      "Icon.tsx",
      [
        "export function A() {",
        "  return <div>{ok && <Icon s={1} />}</div>;",
        "}",
        "export function B() { return null; }",
      ].join("\n"),
    );
    assert.deepEqual(
      tsxSelfClose.map((s) => `${s.name}:${s.startLine}-${s.endLine}`),
      ["A:1-3", "B:4-4"],
    );

    // JS/TS regex literals starting with `>` (e.g. `/>/g`, `/>}/`) must still enter regex mode
    for (const fname of ["escape.ts", "escape.tsx"]) {
      const gtRegex = extractSymbols(
        fname,
        [
          "export function escapeHtml(s: string) {",
          '  return s.replace(/>/g, "&gt;").replace(/{/g, "&#123;");',
          "}",
          "export function closeBraceRegex(s: string) {",
          "  return />}/.test(s);",
          "}",
          "export function after() { return 1; }",
        ].join("\n"),
      );
      assert.deepEqual(
        gtRegex.map((s) => `${s.name}:${s.startLine}-${s.endLine}`),
        ["escapeHtml:1-3", "closeBraceRegex:4-6", "after:7-7"],
      );
    }

    // 2. TS multiline backtick template must not emit fake symbols or swallow real classes below
    const tplSyms = extractSymbols(
      "tpl.ts",
      ["const s = `", "export class Fake {", "`;", "export class Real {", "  m() {}", "}"].join("\n"),
    );
    assert.deepEqual(
      tplSyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["class:Real:4-6", "method:m:5-5"],
    );

    // 3. Python multiline signatures & .pyi stubs
    const pyMulti = extractSymbols(
      "stub.pyi",
      ["def f(", "    a: int,", ") -> int:", "    return a"].join("\n"),
    );
    assert.deepEqual(
      pyMulti.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["function:f:1-4"],
    );

    // 4. Rust lifetimes, impl<T>, trait bounds, and block comments
    const rsSyms = extractSymbols(
      "lib.rs",
      [
        "/* pub fn commented_out() {} */",
        "pub struct View<'a> {",
        "  buf: &'a [u8],",
        "}",
        "impl<T> Cache<T> {",
        "  pub fn next<U: Clone + Send>() -> usize { 1 }",
        "}",
      ].join("\n"),
    );
    assert.deepEqual(
      rsSyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["struct:View<'a>:2-4", "impl:<T> Cache<T>:5-7", "fn:next<U: Clone + Send>:6-6"],
    );

    // 5. Shell parameter expansion with `#` inside `${...}`
    const shSyms = extractSymbols(
      "s.sh",
      ["f() {", "  local b=${1##*/}", "  local n=${#b}", '  echo "$b $n"', "}", "g() { :; }"].join("\n"),
    );
    assert.deepEqual(
      shSyms.map((s) => `${s.name}:${s.startLine}-${s.endLine}`),
      ["f:1-5", "g:6-6"],
    );

    // 6. Go raw backtick string ending in `\` & wrapped `interface{}` params & generic struct
    const goSyms = extractSymbols(
      "a.go",
      [
        "type Box[T any] struct {",
        "  Val T",
        "}",
        "func a() {",
        "  s := `dir\\`",
        "  _ = s",
        "}",
        "func c(",
        "  v interface{},",
        ") {",
        "  a()",
        "}",
      ].join("\n"),
    );
    assert.deepEqual(
      goSyms.map((s) => `${s.kind}:${s.name}:${s.startLine}-${s.endLine}`),
      ["struct:Box[T any]:1-3", "function:a:4-7", "function:c:8-12"],
    );

    // 7. Non-finite maxChars falls back cleanly instead of returning empty output, and single-line emoji truncation avoids lone surrogates
    const longEmoji = path.join(tmpDir, "long-emoji.md");
    await fs.writeFile(longEmoji, "a".repeat(938) + "🚀".repeat(50) + "\n", "utf8");
    const nanRes = await runReadSlices({
      cwd: tmpDir,
      outlines: ["sample.ts"],
      maxChars: "auto" as unknown as number,
    });
    assert.equal(nanRes.isError, false);
    assert.match(nanRes.text, /\[class\] Server/);

    const surrRes = await runReadSlices({
      cwd: tmpDir,
      slices: [{ path: "long-emoji.md", ranges: [[1, 1]] }],
      maxChars: 1000,
    });
    assert.equal(surrRes.isError, false);
    assert.equal(
      /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(surrRes.text),
      false,
      "read_slices output must not contain lone UTF-16 surrogates",
    );
  });
});
