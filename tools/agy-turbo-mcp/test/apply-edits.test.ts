import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";
import { runApplyEdits } from "../src/tools/apply-edits.js";

describe("apply_edits", () => {
  let tmpDir: string;

  before(async () => {
    tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "agy-fast-edits-"));
  });

  after(async () => {
    await fs.rm(tmpDir, { recursive: true, force: true });
  });

  it("applies multiple non-contiguous hunks across multiple files atomically", async () => {
    const fileA = path.join(tmpDir, "a.ts");
    const fileB = path.join(tmpDir, "b.ts");
    await fs.writeFile(
      fileA,
      ["const a = 1;", "const b = 2;", "const c = 3;", "const d = 4;"].join("\n"),
      "utf8",
    );
    await fs.writeFile(fileB, "export const flag = false;\n", "utf8");

    const res = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "a.ts",
          hunks: [
            { target: "const a = 1;", replacement: "const a = 10;" },
            { target: "const d = 4;", replacement: "const d = 40;" },
          ],
        },
        {
          path: "b.ts",
          hunks: [
            { target: "flag = false", replacement: "flag = true" },
          ],
        },
        {
          path: "nested/created.txt",
          create: "hello new file\n",
        },
      ],
    });

    assert.equal(res.isError, false);
    assert.equal(
      await fs.readFile(fileA, "utf8"),
      ["const a = 10;", "const b = 2;", "const c = 3;", "const d = 40;"].join("\n"),
    );
    assert.equal(await fs.readFile(fileB, "utf8"), "export const flag = true;\n");
    assert.equal(
      await fs.readFile(path.join(tmpDir, "nested", "created.txt"), "utf8"),
      "hello new file\n",
    );
  });

  it("modifies zero files when any hunk fails across any file (all-or-nothing)", async () => {
    const goodFile = path.join(tmpDir, "good.txt");
    const badFile = path.join(tmpDir, "bad.txt");
    await fs.writeFile(goodFile, "original good\n", "utf8");
    await fs.writeFile(badFile, "original bad\n", "utf8");

    const res = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "good.txt",
          hunks: [{ target: "original good", replacement: "MODIFIED GOOD" }],
        },
        {
          path: "bad.txt",
          hunks: [{ target: "nonexistent text", replacement: "nope" }],
        },
      ],
    });

    assert.equal(res.isError, true);
    assert.match(res.text, /0 files modified/);
    assert.equal(await fs.readFile(goodFile, "utf8"), "original good\n");
    assert.equal(await fs.readFile(badFile, "utf8"), "original bad\n");
  });

  it("uses startLine/endLine to disambiguate duplicate text and rejects ambiguous matches", async () => {
    const dupFile = path.join(tmpDir, "dup.ts");
    await fs.writeFile(
      dupFile,
      [
        "function one() {",
        "  return value;",
        "}",
        "function two() {",
        "  return value;",
        "}",
      ].join("\n"),
      "utf8",
    );

    const ambig = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "dup.ts",
          hunks: [{ target: "return value;", replacement: "return 2;" }],
        },
      ],
    });
    assert.equal(ambig.isError, true);
    assert.match(ambig.text, /matched 2 times \(lines: 2, 5\)/);

    const anchored = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "dup.ts",
          hunks: [
            {
              target: "return value;",
              replacement: "return 2;",
              startLine: 4,
              endLine: 6,
            },
          ],
        },
      ],
    });
    assert.equal(anchored.isError, false);
    const updated = await fs.readFile(dupFile, "utf8");
    assert.match(updated, /one\(\) \{\n  return value;/);
    assert.match(updated, /two\(\) \{\n  return 2;/);
  });

  it("supports replaceAll, merges duplicate file entries, and rejects overlapping hunks", async () => {
    const f = path.join(tmpDir, "merge.txt");
    await fs.writeFile(f, "alpha beta alpha gamma\n", "utf8");

    const overlap = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "merge.txt",
          hunks: [
            { target: "alpha beta", replacement: "X" },
            { target: "beta alpha", replacement: "Y" },
          ],
        },
      ],
    });
    assert.equal(overlap.isError, true);
    assert.match(overlap.text, /overlap/);

    const merged = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "merge.txt",
          hunks: [{ target: "alpha", replacement: "ALPHA", replaceAll: true }],
        },
        {
          path: "merge.txt",
          hunks: [{ target: "gamma", replacement: "GAMMA" }],
        },
      ],
    });
    assert.equal(merged.isError, false);
    assert.equal(await fs.readFile(f, "utf8"), "ALPHA beta ALPHA GAMMA\n");
  });

  it("preserves CRLF line endings and supports dryRun", async () => {
    const crlfFile = path.join(tmpDir, "win.txt");
    await fs.writeFile(crlfFile, "line1\r\nline2\r\nline3\r\n", "utf8");

    const dry = await runApplyEdits({
      cwd: tmpDir,
      dryRun: true,
      edits: [
        {
          path: "win.txt",
          hunks: [{ target: "line1\nline2", replacement: "LINE1\nLINE2" }],
        },
      ],
    });
    assert.equal(dry.isError, false);
    assert.match(dry.text, /Dry run/);
    assert.equal(await fs.readFile(crlfFile, "utf8"), "line1\r\nline2\r\nline3\r\n");

    const real = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "win.txt",
          hunks: [{ target: "line1\nline2", replacement: "LINE1\nLINE2" }],
        },
      ],
    });
    assert.equal(real.isError, false);
    assert.equal(await fs.readFile(crlfFile, "utf8"), "LINE1\r\nLINE2\r\nline3\r\n");
  });

  it("runs inline verify command on pass and fail", async () => {
    const f = path.join(tmpDir, "check.js");
    await fs.writeFile(f, "const x = 1;\n", "utf8");

    const passRes = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        { path: "check.js", hunks: [{ target: "x = 1", replacement: "x = 2" }] },
      ],
      verify: `${JSON.stringify(process.execPath)} -e "console.log('12 tests passed')"`,
    });
    assert.equal(passRes.isError, false);
    assert.match(passRes.text, /Verify \[PASS/);
    assert.match(passRes.text, /12 tests passed/);

    const failRes = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        { path: "check.js", hunks: [{ target: "x = 2", replacement: "x = 3" }] },
      ],
      verify: `${JSON.stringify(process.execPath)} -e "console.error('TypeError: bad value'); process.exit(1)"`,
    });
    assert.equal(failRes.isError, false);
    assert.match(failRes.text, /Verify \[FAIL \(exit 1\)/);
    assert.match(failRes.text, /TypeError: bad value/);
    assert.equal(await fs.readFile(f, "utf8"), "const x = 3;\n");
  });

  it("refuses overwriting existing file with create unless overwrite is true", async () => {
    const f = path.join(tmpDir, "exist.txt");
    await fs.writeFile(f, "keep me\n", "utf8");

    const blocked = await runApplyEdits({
      cwd: tmpDir,
      edits: [{ path: "exist.txt", create: "new\n" }],
    });
    assert.equal(blocked.isError, true);
    assert.equal(await fs.readFile(f, "utf8"), "keep me\n");

    const allowed = await runApplyEdits({
      cwd: tmpDir,
      edits: [{ path: "exist.txt", create: "replaced\n", overwrite: true }],
    });
    assert.equal(allowed.isError, false);
    assert.equal(await fs.readFile(f, "utf8"), "replaced\n");
  });

  it("preserves file permissions, follows symlinks, reports accurate line counts, and validates line bounds", async () => {
    const scriptPath = path.join(tmpDir, "tool.sh");
    await fs.writeFile(scriptPath, "#!/bin/sh\necho v1\n", "utf8");
    if (process.platform !== "win32") {
      await fs.chmod(scriptPath, 0o755);
    }

    const realFile = path.join(tmpDir, "real-target.txt");
    const symFile = path.join(tmpDir, "sym-link.txt");
    await fs.writeFile(realFile, "hello link\n", "utf8");
    await fs.symlink(realFile, symFile);

    const editRes = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        { path: "tool.sh", hunks: [{ target: "v1", replacement: "v2" }] },
        { path: "sym-link.txt", hunks: [{ target: "hello", replacement: "updated" }] },
        { path: "real-target.txt", hunks: [{ target: "link", replacement: "target" }] },
        { path: "single-line.txt", create: "only one line\n" },
      ],
    });

    assert.equal(editRes.isError, false);
    assert.match(editRes.text, /single-line\.txt: created, lines \+1/);
    assert.equal(await fs.readFile(scriptPath, "utf8"), "#!/bin/sh\necho v2\n");
    if (process.platform !== "win32") {
      assert.equal((await fs.stat(scriptPath)).mode & 0o777, 0o755);
    }
    assert.equal((await fs.lstat(symFile)).isSymbolicLink(), true);
    assert.equal(await fs.readFile(realFile, "utf8"), "updated target\n");

    const badLine = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "real-target.txt",
          hunks: [{ target: "updated", replacement: "x", startLine: Number.NaN }],
        },
      ],
    });
    assert.equal(badLine.isError, true);
    assert.match(badLine.text, /Invalid startLine/);
  });

  it("rolls back content and permissions if a later write fails in Phase 2", async () => {
    if (process.platform === "win32" || process.getuid?.() === 0) return;
    const okPath = path.join(tmpDir, "rb-ok.sh");
    const lockedDir = path.join(tmpDir, "rb-locked");
    const lockedFile = path.join(lockedDir, "blocked.txt");
    await fs.mkdir(lockedDir, { recursive: true });
    await fs.writeFile(okPath, "#!/bin/sh\necho orig\n", "utf8");
    await fs.chmod(okPath, 0o755);
    await fs.writeFile(lockedFile, "locked orig\n", "utf8");
    await fs.chmod(lockedFile, 0o444);
    await fs.chmod(lockedDir, 0o555);

    try {
      const res = await runApplyEdits({
        cwd: tmpDir,
        edits: [
          { path: "rb-ok.sh", hunks: [{ target: "orig", replacement: "changed" }] },
          { path: "rb-locked/blocked.txt", hunks: [{ target: "orig", replacement: "changed" }] },
        ],
      });
      assert.equal(res.isError, true);
      assert.match(res.text, /rolled back/);
      assert.equal(await fs.readFile(okPath, "utf8"), "#!/bin/sh\necho orig\n");
      assert.equal((await fs.stat(okPath)).mode & 0o777, 0o755);
    } finally {
      await fs.chmod(lockedDir, 0o755);
      await fs.chmod(lockedFile, 0o644);
    }
  });

  it("includes closest-match line snippets when target misses without startLine/endLine and supports bestEffort partial commits", async () => {
    const okFile = path.join(tmpDir, "be-ok.java");
    const missFile = path.join(tmpDir, "be-miss.java");
    await fs.writeFile(okFile, "public class Ok {\n  int value = 1;\n}\n", "utf8");
    await fs.writeFile(
      missFile,
      [
        "public class ShopIncome {",
        "  public static String label(String source) {",
        "    return switch (source) {",
        '      case "ERP" -> "实付";',
        '      case "EST" -> "估算";',
        '      default -> "未知";',
        "    };",
        "  }",
        "}",
      ].join("\n") + "\n",
      "utf8",
    );

    // Without startLine/endLine, atomic mode still reports closest match snippet
    const atomicMiss = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "be-miss.java",
          hunks: [
            {
              target: '    return switch (source) {\n      case "ERP" -> "WRONG";\n    };',
              replacement: "    return source;",
            },
          ],
        },
      ],
    });
    assert.equal(atomicMiss.isError, true);
    assert.match(atomicMiss.text, /Closest match near line 3/);
    assert.match(atomicMiss.text, /case "ERP" -> "实付"/);

    // bestEffort: true writes valid files, keeps failed file intact with snippet, and skips verify
    const partial = await runApplyEdits({
      cwd: tmpDir,
      bestEffort: true,
      verify: `${JSON.stringify(process.execPath)} -e "console.log('SHOULD_NOT_RUN')"`,
      edits: [
        {
          path: "be-ok.java",
          hunks: [{ target: "int value = 1;", replacement: "int value = 2;" }],
        },
        {
          path: "be-miss.java",
          hunks: [
            {
              target: '    return switch (source) {\n      case "ERP" -> "WRONG";\n    };',
              replacement: "    return source;",
            },
          ],
        },
      ],
    });
    assert.equal(partial.isError, true);
    assert.match(partial.text, /Partial edit applied \(1\/2 file\(s\) written; 1 file\(s\) failed/);
    assert.match(partial.text, /be-ok\.java: updated/);
    assert.match(partial.text, /Closest match near line 3/);
    assert.match(partial.text, /Verify skipped/);
    assert.doesNotMatch(partial.text, /SHOULD_NOT_RUN/);
    assert.equal(await fs.readFile(okFile, "utf8"), "public class Ok {\n  int value = 2;\n}\n");
    assert.match(await fs.readFile(missFile, "utf8"), /case "ERP" -> "实付"/);
  });

  it("detects line-drifted exact matches, bounds wide-range failure snippets, and filters Spring/Pino verify noise", async () => {
    const bigPath = path.join(tmpDir, "drift.ts");
    const lines = [
      ...Array.from({ length: 85 }, (_, i) => `const filler_${i + 1} = ${i + 1};`),
      "function targetBlock() {",
      "  const answer = 42;",
      "  return answer;",
      "}",
    ];
    await fs.writeFile(bigPath, lines.join("\n") + "\n", "utf8");

    // 1. Exact target exists outside startLine..endLine -> reports exact drifted line
    const driftRes = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "drift.ts",
          hunks: [
            {
              startLine: 1,
              endLine: 40,
              strictWindow: true,
              target: "function targetBlock() {\n  const answer = 42;",
              replacement: "function targetBlock() {\n  const answer = 99;",
            },
          ],
        },
      ],
    });
    assert.equal(driftRes.isError, true);
    assert.match(driftRes.text, /Exact match found outside range at L86/);

    // 2. Wide range (>26 lines) mismatch uses closest-region snippet instead of dumping 85 lines
    const wideRes = await runApplyEdits({
      cwd: tmpDir,
      edits: [
        {
          path: "drift.ts",
          hunks: [
            {
              startLine: 1,
              endLine: 89,
              target: "function targetBlock() {\n  const answer = 999;",
              replacement: "function targetBlock() {\n  const answer = 100;",
            },
          ],
        },
      ],
    });
    assert.equal(wideRes.isError, true);
    assert.match(wideRes.text, /Closest match near line 86/);
    assert.ok(wideRes.text.length < 1500, `Expected compact hint, got ${wideRes.text.length} chars`);

    // 3. Verify compact output strips Pino level:30 JSON noise + Maven Help footer and preserves Spring Description + Caused by
    const noisyVerifyScript = [
      'for (let i = 0; i < 80; i++) console.log(JSON.stringify({ level: 30, msg: "incoming request", reqId: i, payload: "x".repeat(60) }));',
      'console.log("Description:");',
      'console.log("Parameter 1 of constructor in AfterSaleController required a bean of type AfterSaleQueryService.");',
      'console.log("Action:");',
      'console.log("Consider defining a bean of type AfterSaleQueryService.");',
      'for (let i = 0; i < 40; i++) console.log("    at com.example.Framework.invoke(Framework.java:" + i + ")");',
      'console.log("Caused by: java.sql.SQLSyntaxErrorException: Unknown column shop_avatar");',
      'console.log("[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.");',
      'console.log("[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoFailureException");',
      "process.exit(1);",
    ].join(" ");
    const verifyRes = await runApplyEdits({
      cwd: tmpDir,
      maxVerifyChars: 20_000,
      verify: `${JSON.stringify(process.execPath)} -e ${JSON.stringify(noisyVerifyScript)}`,
      edits: [
        {
          path: "drift.ts",
          hunks: [{ target: "const filler_1 = 1;", replacement: "const filler_1 = 10;" }],
        },
      ],
    });
    const verifyBody = verifyRes.text.split("`:\n")[1] ?? "";
    assert.equal(verifyRes.isError, false);
    assert.ok(Buffer.byteLength(verifyRes.text, "utf8") <= 8_800);
    assert.match(verifyBody, /AfterSaleQueryService/);
    assert.match(verifyBody, /Caused by: java\.sql\.SQLSyntaxErrorException/);
    assert.doesNotMatch(verifyBody, /incoming request/);
    assert.doesNotMatch(verifyBody, /\[Help 1\]/);
  });

  it("circuit-breaks corrupted call_mcp_tool nested edits (booleans/null) and accepts stringified JSON arrays", async () => {
    const file = path.join(tmpDir, "corrupt-test.ts");
    await fs.writeFile(file, "export const x = 1;\n", "utf8");

    // Client serialization bug: edits collapsed to [false, true] or [null]
    const boolRes = await runApplyEdits({
      cwd: tmpDir,
      edits: [false, true] as unknown as never[],
    });
    assert.equal(boolRes.isError, true);
    assert.match(boolRes.text, /nested argument serialization corruption/i);
    assert.match(boolRes.text, /edits\[0\]: received boolean \(false\)/);
    assert.match(boolRes.text, /edits\[1\]: received boolean \(true\)/);
    assert.match(boolRes.text, /Do NOT retry the same apply_edits payload/);
    assert.match(boolRes.text, /edit_file/);

    const nullRes = await runApplyEdits({
      cwd: tmpDir,
      edits: [null, null] as unknown as never[],
    });
    assert.equal(nullRes.isError, true);
    assert.match(nullRes.text, /edits\[0\]: received null/);

    // Nested hunks collapsed to boolean/null
    const hunkRes = await runApplyEdits({
      cwd: tmpDir,
      edits: [{ path: "corrupt-test.ts", hunks: [true] }] as unknown as never[],
    });
    assert.equal(hunkRes.isError, true);
    assert.match(hunkRes.text, /edits\[0\]\.hunks\[0\]: received boolean \(true\)/);

    const hunksBoolRes = await runApplyEdits({
      cwd: tmpDir,
      edits: [{ path: "corrupt-test.ts", hunks: true }] as unknown as never[],
    });
    assert.equal(hunksBoolRes.isError, true);
    assert.match(hunksBoolRes.text, /edits\[0\]\.hunks: received boolean \(true\)/);

    // Top-level edits collapsed to boolean
    const topBool = await runApplyEdits({
      cwd: tmpDir,
      edits: true as unknown as never[],
    });
    assert.equal(topBool.isError, true);
    assert.match(topBool.text, /edits: received boolean \(true\)/);

    // Verify file remained untouched
    assert.equal(await fs.readFile(file, "utf8"), "export const x = 1;\n");

    // Stringified JSON array tolerance works cleanly
    const stringifiedRes = await runApplyEdits({
      cwd: tmpDir,
      edits: JSON.stringify([
        {
          path: "corrupt-test.ts",
          hunks: [{ target: "export const x = 1;", replacement: "export const x = 42;" }],
        },
      ]) as unknown as never[],
    });
    assert.equal(stringifiedRes.isError, false);
    assert.equal(await fs.readFile(file, "utf8"), "export const x = 42;\n");
  });

  it("treats an empty edits array as lost arguments and reports invalid JSON strings", async () => {
    const file = path.join(tmpDir, "empty-edits.ts");
    await fs.writeFile(file, "export const y = 1;\n", "utf8");

    // agy CLI 1.5.x can deliver a long native edits array as [] (or stringified "[]", "true", "null").
    for (const emptyPayload of [[], "[]"]) {
      const emptyRes = await runApplyEdits({ cwd: tmpDir, edits: emptyPayload as unknown as never[] });
      assert.equal(emptyRes.isError, true);
      assert.match(emptyRes.text, /nested argument serialization corruption/i);
      assert.match(emptyRes.text, /edits: received array \(len=0\)/);
      assert.match(emptyRes.text, /Do NOT retry the same apply_edits payload/);
      assert.match(emptyRes.text, /edit_file/);
    }

    for (const [lit, expectedLabel] of [
      ["true", /boolean \(true\)/],
      ["false", /boolean \(false\)/],
      ["null", /received null/],
    ] as const) {
      const litRes = await runApplyEdits({ cwd: tmpDir, edits: lit });
      assert.equal(litRes.isError, true);
      assert.match(litRes.text, /nested argument serialization corruption/i);
      assert.match(litRes.text, expectedLabel);
    }

    // Truncated JSON, markdown-fenced JSON, and single-object JSON all surface invalid_json clearly
    for (const badPayload of [
      '[{"path": "empty-edits.ts", "hunks": [{"target": "1;", "replacement": "2;"}]',
      '```json\n[{"path": "empty-edits.ts"}]\n```',
      '{"path": "empty-edits.ts", "hunks": []}',
    ]) {
      const badJson = await runApplyEdits({ cwd: tmpDir, edits: badPayload });
      assert.equal(badJson.isError, true);
      assert.match(badJson.text, /^Error: 'edits' is not a valid JSON array \(0 files modified\): \S/);
    }

    const emptyStr = await runApplyEdits({ cwd: tmpDir, edits: "   " });
    assert.equal(emptyStr.isError, true);
    assert.match(emptyStr.text, /non-empty JSON array string \(or array\)/);
    assert.equal(await fs.readFile(file, "utf8"), "export const y = 1;\n");
  });

  function makeErrnoError(code: string, message: string): NodeJS.ErrnoException {
    const err = new Error(message) as NodeJS.ErrnoException;
    err.code = code;
    return err;
  }

  it("does not fall back to direct write or leave tmp files when temp write fails in Phase 2", async () => {
    const subDir = path.join(tmpDir, "phase2-tmp-fail");
    await fs.mkdir(subDir, { recursive: true });
    const realSubDir = await fs.realpath(subDir);
    const firstFile = path.join(realSubDir, "first.txt");
    const targetFile = path.join(realSubDir, "target.txt");
    await fs.writeFile(firstFile, "first orig\n", "utf8");
    await fs.writeFile(targetFile, "target orig\n", "utf8");

    const origWriteFile = fs.writeFile;
    let directWriteAttempted = false;
    fs.writeFile = (async (...args: Parameters<typeof origWriteFile>) => {
      const p = String(args[0]);
      if (p.startsWith(`${targetFile}.agy-fast-tmp-`)) {
        await origWriteFile(p, "partial");
        throw makeErrnoError("ENOSPC", "ENOSPC: no space left on device");
      }
      if (p === targetFile) {
        directWriteAttempted = true;
        await origWriteFile(p, "");
        throw makeErrnoError("ENOSPC", "ENOSPC: no space left on device");
      }
      return origWriteFile(...args);
    }) as typeof fs.writeFile;

    try {
      const res = await runApplyEdits({
        cwd: realSubDir,
        edits: [
          { path: "first.txt", hunks: [{ target: "first orig", replacement: "first mod" }] },
          { path: "target.txt", hunks: [{ target: "target orig", replacement: "target mod" }] },
        ],
      });
      assert.equal(res.isError, true);
      assert.match(res.text, /Write failed \(changes rolled back\)/);
      assert.match(res.text, /ENOSPC/);
      assert.equal(directWriteAttempted, false);
    } finally {
      fs.writeFile = origWriteFile;
    }

    assert.equal(await fs.readFile(firstFile, "utf8"), "first orig\n");
    assert.equal(await fs.readFile(targetFile, "utf8"), "target orig\n");
    assert.equal((await fs.readdir(realSubDir)).some((n) => n.includes(".agy-fast-tmp-")), false);
  });

  it("rolls back target file and removes partial created file when rename and fallback direct write both fail", async () => {
    const subDir = path.join(tmpDir, "phase2-fallback-fail");
    await fs.mkdir(subDir, { recursive: true });
    const realSubDir = await fs.realpath(subDir);
    const firstFile = path.join(realSubDir, "first.txt");
    const targetFile = path.join(realSubDir, "target.txt");
    const newFile = path.join(realSubDir, "created-fail.txt");
    await fs.writeFile(firstFile, "first orig\n", "utf8");
    await fs.writeFile(targetFile, "target orig\n", "utf8");

    const origRename = fs.rename;
    const origWriteFile = fs.writeFile;
    let targetWriteCalls = 0;

    fs.rename = (async (...args: Parameters<typeof origRename>) => {
      if (String(args[1]) === targetFile || String(args[1]) === newFile) {
        throw makeErrnoError("EXDEV", "EXDEV: cross-device link not permitted");
      }
      return origRename(...args);
    }) as typeof fs.rename;

    fs.writeFile = (async (...args: Parameters<typeof origWriteFile>) => {
      const p = String(args[0]);
      if (p === targetFile && ++targetWriteCalls === 1) {
        await origWriteFile(p, "");
        throw makeErrnoError("EIO", "EIO: i/o error");
      }
      if (p === newFile) {
        await origWriteFile(p, "");
        throw makeErrnoError("EIO", "EIO: i/o error");
      }
      return origWriteFile(...args);
    }) as typeof fs.writeFile;

    try {
      const existingRes = await runApplyEdits({
        cwd: realSubDir,
        edits: [
          { path: "first.txt", hunks: [{ target: "first orig", replacement: "first mod" }] },
          { path: "target.txt", hunks: [{ target: "target orig", replacement: "target mod" }] },
        ],
      });
      assert.equal(existingRes.isError, true);
      assert.match(existingRes.text, /Write failed \(changes rolled back\)/);
      assert.match(existingRes.text, /EIO/);
      assert.equal(targetWriteCalls, 2);

      const createdRes = await runApplyEdits({
        cwd: realSubDir,
        edits: [{ path: "created-fail.txt", create: "new content\n" }],
      });
      assert.equal(createdRes.isError, true);
      assert.match(createdRes.text, /Write failed \(changes rolled back\)/);
      assert.match(createdRes.text, /EIO/);
    } finally {
      fs.rename = origRename;
      fs.writeFile = origWriteFile;
    }

    assert.equal(await fs.readFile(firstFile, "utf8"), "first orig\n");
    assert.equal(await fs.readFile(targetFile, "utf8"), "target orig\n");
    await assert.rejects(() => fs.stat(newFile), { code: "ENOENT" });
    assert.equal((await fs.readdir(realSubDir)).some((n) => n.includes(".agy-fast-tmp-")), false);
  });

  it("fails fast on non-ENOENT errors in create without overwriting or deleting existing files", async () => {
    const subDir = path.join(tmpDir, "create-non-enoent");
    await fs.mkdir(subDir, { recursive: true });
    const realSubDir = await fs.realpath(subDir);
    const statErrFile = path.join(realSubDir, "stat-err.txt");
    const busyFile = path.join(realSubDir, "busy.txt");
    const otherFile = path.join(realSubDir, "other.txt");
    await fs.writeFile(statErrFile, "keep stat-err\n", "utf8");
    await fs.writeFile(busyFile, "precious original\n", "utf8");
    await fs.writeFile(otherFile, "other orig\n", "utf8");

    const origStat = fs.stat;
    fs.stat = (async (...args: Parameters<typeof origStat>) => {
      if (String(args[0]) === statErrFile) {
        throw makeErrnoError("EACCES", `EACCES: permission denied, stat '${statErrFile}'`);
      }
      return origStat(...args);
    }) as typeof fs.stat;

    try {
      const statRes = await runApplyEdits({
        cwd: realSubDir,
        edits: [{ path: "stat-err.txt", create: "clobbered\n" }],
      });
      assert.equal(statRes.isError, true);
      assert.match(statRes.text, /0 files modified/);
      assert.match(statRes.text, /EACCES/);
    } finally {
      fs.stat = origStat;
    }
    assert.equal(await fs.readFile(statErrFile, "utf8"), "keep stat-err\n");

    const origReadFile = fs.readFile;
    fs.readFile = (async (...args: Parameters<typeof origReadFile>) => {
      if (String(args[0]) === busyFile) {
        throw makeErrnoError("EBUSY", `EBUSY: resource busy or locked, open '${busyFile}'`);
      }
      return origReadFile(...args);
    }) as typeof fs.readFile;

    try {
      const atomicRes = await runApplyEdits({
        cwd: realSubDir,
        edits: [
          { path: "busy.txt", create: "replaced\n", overwrite: true },
          { path: "other.txt", hunks: [{ target: "other orig", replacement: "other mod" }] },
        ],
      });
      assert.equal(atomicRes.isError, true);
      assert.match(atomicRes.text, /0 files modified/);
      assert.match(atomicRes.text, /EBUSY/);

      const partialRes = await runApplyEdits({
        cwd: realSubDir,
        bestEffort: true,
        edits: [
          { path: "busy.txt", create: "replaced\n", overwrite: true },
          { path: "other.txt", hunks: [{ target: "other orig", replacement: "other mod" }] },
        ],
      });
      assert.equal(partialRes.isError, true);
      assert.match(partialRes.text, /Partial edit applied \(1\/2 file\(s\) written; 1 file\(s\) failed/);
      assert.match(partialRes.text, /EBUSY/);
    } finally {
      fs.readFile = origReadFile;
    }

    assert.equal(await fs.readFile(busyFile, "utf8"), "precious original\n");
    assert.equal(await fs.readFile(otherFile, "utf8"), "other mod\n");
  });

  it("protects dangling symlinks on create, preserves majority LF EOL, validates verify before writes, and avoids lone surrogates", async () => {
    const subDir = path.join(tmpDir, "hardening-edge");
    await fs.mkdir(subDir, { recursive: true });
    const realSubDir = await fs.realpath(subDir);

    // 1. Dangling symlink protection (overwrite: false refuses; overwrite: true restores symlink on Phase-2 rollback)
    const danglingLink = path.join(realSubDir, "dangling.txt");
    let createdFileSymlink = false;
    try {
      await fs.symlink("missing-target.txt", danglingLink);
      createdFileSymlink = true;
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (process.platform !== "win32" || (code !== "EPERM" && code !== "ENOTSUP")) {
        throw err;
      }
    }
    if (createdFileSymlink) {
      const blockedSym = await runApplyEdits({
        cwd: realSubDir,
        edits: [{ path: "dangling.txt", create: "clobbered\n", overwrite: false }],
      });
      assert.equal(blockedSym.isError, true);
      assert.match(blockedSym.text, /dangling symlink/);
      assert.equal((await fs.lstat(danglingLink)).isSymbolicLink(), true);

      const failTarget = path.join(realSubDir, "fail-after.txt");
      await fs.writeFile(failTarget, "orig\n", "utf8");
      const origRename = fs.rename;
      const origWriteFile = fs.writeFile;
      fs.rename = (async (...args: Parameters<typeof origRename>) => {
        if (String(args[1]) === failTarget) throw makeErrnoError("EXDEV", "EXDEV");
        return origRename(...args);
      }) as typeof fs.rename;
      fs.writeFile = (async (...args: Parameters<typeof origWriteFile>) => {
        if (String(args[0]) === failTarget) throw makeErrnoError("EIO", "EIO");
        return origWriteFile(...args);
      }) as typeof fs.writeFile;
      try {
        const rbSym = await runApplyEdits({
          cwd: realSubDir,
          edits: [
            { path: "dangling.txt", create: "replaced\n", overwrite: true },
            { path: "fail-after.txt", hunks: [{ target: "orig", replacement: "mod" }] },
          ],
        });
        assert.equal(rbSym.isError, true);
        assert.match(rbSym.text, /rolled back/);
      } finally {
        fs.rename = origRename;
        fs.writeFile = origWriteFile;
      }
      assert.equal((await fs.lstat(danglingLink)).isSymbolicLink(), true);
      assert.equal(await fs.readlink(danglingLink), "missing-target.txt");
    }

    // 2. Symlinked parent directory deduplication for new files
    const realChildDir = path.join(realSubDir, "real-dir");
    const symChildDir = path.join(realSubDir, "sym-dir");
    await fs.mkdir(realChildDir, { recursive: true });
    await fs.symlink(realChildDir, symChildDir, process.platform === "win32" ? "junction" : "dir");
    const dupCreate = await runApplyEdits({
      cwd: realSubDir,
      edits: [
        { path: "real-dir/new.ts", create: "a\n" },
        { path: "sym-dir/new.ts", create: "b\n" },
      ],
    });
    assert.equal(dupCreate.isError, true);
    assert.match(dupCreate.text, /duplicate entries cannot mix 'create'/);

    // 3. Single CRLF in an LF-majority script must NOT rewrite the whole file to CRLF
    const scriptPath = path.join(realSubDir, "run.sh");
    await fs.writeFile(scriptPath, "#!/usr/bin/env bash\necho 1\r\necho 2\necho 3\n", "utf8");
    const lfRes = await runApplyEdits({
      cwd: realSubDir,
      edits: [{ path: "run.sh", hunks: [{ target: "echo 3", replacement: "echo 4" }] }],
    });
    assert.equal(lfRes.isError, false);
    const scriptAfter = await fs.readFile(scriptPath, "utf8");
    assert.ok(scriptAfter.startsWith("#!/usr/bin/env bash\n"));
    assert.ok(!scriptAfter.includes("\r\n"));

    // 4. Malformed verify fails validation BEFORE writing files; non-finite verifyTimeoutSec/maxVerifyChars stay safe
    const verifyFile = path.join(realSubDir, "v.ts");
    await fs.writeFile(verifyFile, "export const v = 1;\n", "utf8");
    const badVerify = await runApplyEdits({
      cwd: realSubDir,
      edits: [{ path: "v.ts", hunks: [{ target: "v = 1", replacement: "v = 2" }] }],
      verify: { command: true as unknown as string },
    });
    assert.equal(badVerify.isError, true);
    assert.match(badVerify.text, /0 files modified/);
    assert.equal(await fs.readFile(verifyFile, "utf8"), "export const v = 1;\n");

    const nanVerify = await runApplyEdits({
      cwd: realSubDir,
      edits: [{ path: "v.ts", hunks: [{ target: "v = 1", replacement: "v = 2" }] }],
      verify: `${JSON.stringify(process.execPath)} -e "console.log('verify ok')"`,
      verifyTimeoutSec: Number.NaN,
      maxVerifyChars: Number.NaN,
    });
    assert.equal(nanVerify.isError, false);
    assert.match(nanVerify.text, /Verify \[PASS/);
    assert.match(nanVerify.text, /verify ok/);

    // 5. Long emoji line in mismatch snippet does not emit a lone surrogate
    const emojiFile = path.join(realSubDir, "emoji.ts");
    await fs.writeFile(emojiFile, `const token = "${"x".repeat(142)}${"🚀".repeat(30)}";\n`, "utf8");
    const emojiMiss = await runApplyEdits({
      cwd: realSubDir,
      edits: [{ path: "emoji.ts", hunks: [{ startLine: 1, endLine: 1, target: "nonexistent", replacement: "y" }] }],
    });
    assert.equal(emojiMiss.isError, true);
    assert.equal(
      /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(emojiMiss.text),
      false,
    );
  });
});
