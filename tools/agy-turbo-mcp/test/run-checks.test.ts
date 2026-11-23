import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { runChecks } from "../src/tools/run-checks.js";
import {
  buildPowerShellUtf8Command,
  decodeProcessOutput,
  formatPowerShellCommand,
  needsPowerShellOnWindows,
  parseWindowsCodePage,
  resetWindowsShellCache,
  resolveWindowsPowerShell,
} from "../src/utils/exec-utils.js";

describe("run_checks", () => {
  const nodeBin = JSON.stringify(process.execPath);

  it("executes multiple commands concurrently and compacts passing output", async () => {
    const start = Date.now();
    const res = await runChecks({
      concurrency: 3,
      outputMode: "compact",
      commands: [
        {
          name: "c1",
          command: `${nodeBin} -e "setTimeout(() => console.log('line1\\nline2\\nAll 10 tests passed'), 180)"`,
        },
        {
          name: "c2",
          command: `${nodeBin} -e "setTimeout(() => console.log('Build done in 0.2s'), 180)"`,
        },
        {
          name: "c3",
          command: `${nodeBin} -e "setTimeout(() => console.log('0 errors'), 180)"`,
        },
      ],
    });
    const elapsed = Date.now() - start;

    assert.equal(res.isError, false);
    assert.match(res.text, /Checks: 3\/3 passed/);
    assert.match(res.text, /\[PASS \d+ms\] c1/);
    assert.match(res.text, /All 10 tests passed/);
    // Should clearly execute concurrently rather than 3 * 180ms = 540ms+ serially
    assert.ok(elapsed < 480, `Expected concurrent execution <480ms, got ${elapsed}ms`);
  });

  it("extracts error lines on failure and strips ANSI escape codes", async () => {
    const script = [
      "for (let i = 0; i < 40; i++) console.log('info step ' + i);",
      "console.error('\\x1b[31mError: TS2322 type mismatch at src/a.ts:12\\x1b[0m');",
      "console.log('progress 10%\\rprogress 100%');",
      "process.exit(2);",
    ].join(" ");

    const res = await runChecks({
      outputMode: "compact",
      commands: [{ name: "typecheck", command: `${nodeBin} -e "${script}"` }],
    });

    assert.equal(res.isError, false);
    assert.match(res.text, /Checks: 0\/1 passed, 1 failed/);
    assert.match(res.text, /FAIL \(exit 2\)/);
    assert.match(res.text, /Error: TS2322 type mismatch/);
    assert.doesNotMatch(res.text, /\x1b\[31m/);
    assert.match(res.text, /progress 100%/);
  });

  it("enforces command timeouts cleanly", async () => {
    const res = await runChecks({
      timeoutSec: 1,
      commands: [
        {
          name: "hang",
          command: `${nodeBin} -e "setTimeout(() => console.log('never'), 10000)"`,
        },
      ],
    });

    assert.equal(res.isError, false);
    assert.match(res.text, /TIMEOUT/);
  });

  it("finishes within the shared call budget and reports commands that never started", async () => {
    const budgetMs = 2_000;
    const start = Date.now();
    const res = await runChecks(
      {
        concurrency: 1,
        timeoutSec: 5,
        commands: [
          { name: "quick", command: `${nodeBin} -e "setTimeout(() => console.log('ok'), 300)"` },
          { name: "hang", command: `${nodeBin} -e "setTimeout(() => {}, 10000)"` },
          { name: "late", command: `${nodeBin} -e "console.log('late')"` },
        ],
      },
      budgetMs,
    );
    const elapsed = Date.now() - start;

    assert.ok(elapsed < budgetMs + 1_000, `call took ${elapsed}ms`);
    assert.match(res.text, /^Checks: 1\/3 passed, 1 failed, 1 not run \(call time budget of 2s used up/);
    assert.match(res.text, /\[PASS \d+ms\] quick/);
    assert.match(res.text, /## \[TIMEOUT \d+ms\] hang/);
    assert.match(res.text, /## \[NOT RUN\] late/);
  });

  it("supports full output mode and rejects invalid inputs", async () => {
    const full = await runChecks({
      outputMode: "full",
      commands: [`${nodeBin} -e "console.log('alpha\\nbeta\\ngamma')"`],
    });
    assert.equal(full.isError, false);
    assert.match(full.text, /alpha\nbeta\ngamma/);

    const empty = await runChecks({ commands: [] });
    assert.equal(empty.isError, true);

    const badSubCwd = await runChecks({
      commands: [{ name: "badcwd", command: "echo hi", cwd: "/nonexistent-agy-fast-dir-12345" }],
    });
    assert.equal(badSubCwd.isError, true);
    assert.match(badSubCwd.text, /does not exist/);

    // Non-string commands[i].cwd returns a clean validation error; boolean timeoutSec falls back
    const nonStrSubCwd = await runChecks({
      commands: [{ name: "bad-type", command: `${nodeBin} -e "0"`, cwd: 123 as unknown as string }],
    });
    assert.equal(nonStrSubCwd.isError, true);
    assert.match(nonStrSubCwd.text, /commands\[0\]\.cwd must be a string/);

    const boolTimeout = await runChecks({
      timeoutSec: false as unknown as number,
      commands: [{ name: "bool-timeout", command: `${nodeBin} -e "setTimeout(() => console.log('ok'), 1150)"` }],
    });
    assert.equal(boolTimeout.isError, false);
    assert.match(boolTimeout.text, /1\/1 passed/);
  });

  it("captures npm ERR! lines and respects tight maxChars across multiple failures", async () => {
    const npmErrScript = [
      "for (let i = 0; i < 80; i++) console.log('verbose build step number ' + i + ' padding padding');",
      "console.error('npm ERR! code ELIFECYCLE');",
      "console.error('npm ERR! errno 1');",
      "for (let i = 0; i < 40; i++) console.log('trailing line ' + i + ' padding padding');",
      "process.exit(1);",
    ].join(" ");

    const res = await runChecks({
      outputMode: "compact",
      maxChars: 1200,
      commands: [
        { name: "npm-fail-1", command: `${nodeBin} -e "${npmErrScript}"` },
        { name: "npm-fail-2", command: `${nodeBin} -e "console.error('FAIL_TWO_MARKER'); process.exit(1)"` },
      ],
    });
    assert.equal(res.isError, false);
    assert.ok(res.text.length <= 1200, `Exceeded maxChars: ${res.text.length}`);
    assert.match(res.text, /npm ERR! code ELIFECYCLE/);
    assert.match(res.text, /FAIL_TWO_MARKER/);
  });

  it("circuit-breaks corrupted commands (boolean/null) and accepts stringified JSON arrays", async () => {
    const bad = await runChecks({
      commands: [true, null] as unknown as never[],
    });
    assert.equal(bad.isError, true);
    assert.match(bad.text, /nested argument serialization corruption/i);
    assert.match(bad.text, /commands\[0\]: received boolean \(true\)/);
    assert.match(bad.text, /commands\[1\]: received null/);

    const ok = await runChecks({
      commands: JSON.stringify([{ name: "json-ok", command: `${nodeBin} -e "console.log('ok')"` }]) as unknown as never[],
    });
    assert.equal(ok.isError, false);
    assert.match(ok.text, /\[PASS/);
  });

  it("detects PowerShell syntax vs cross-platform commands and formats quoted executables", () => {
    assert.equal(
      needsPowerShellOnWindows('node "$env:USERPROFILE\\.gemini\\dist\\benchmark.mjs" --help'),
      true,
    );
    assert.equal(
      needsPowerShellOnWindows('node "$Env:USERPROFILE\\dist\\benchmark.mjs" --help'),
      true,
    );
    assert.equal(
      needsPowerShellOnWindows("echo $ENV:PATH; echo $LastExitCode"),
      true,
    );
    assert.equal(
      needsPowerShellOnWindows("Get-ChildItem src | Select-Object -First 5"),
      true,
    );
    assert.equal(
      needsPowerShellOnWindows("if (Test-Path dist) { Write-Output $LASTEXITCODE }"),
      true,
    );
    assert.equal(needsPowerShellOnWindows("$x = 1; echo $x"), true);
    assert.equal(needsPowerShellOnWindows("echo $(Get-Date)"), true);
    assert.equal(needsPowerShellOnWindows('@("a", "b") -join ","'), true);

    // Cross-platform CLI tools and quoted patterns must NOT false-positive as PowerShell
    assert.equal(needsPowerShellOnWindows("npm test"), false);
    assert.equal(needsPowerShellOnWindows("npm run test-unit"), false);
    assert.equal(needsPowerShellOnWindows("start-server-and-test dev 3000 test"), false);
    assert.equal(needsPowerShellOnWindows("copy-webpack-plugin --version"), false);
    assert.equal(needsPowerShellOnWindows("wait-on http://localhost:3000 && npm test"), false);
    assert.equal(needsPowerShellOnWindows("start-storybook -p 6006"), false);
    assert.equal(needsPowerShellOnWindows("copy-files src dist"), false);
    assert.equal(needsPowerShellOnWindows("npm run build && test-runner"), false);
    assert.equal(needsPowerShellOnWindows('git log --grep="(set-cookie)"'), false);
    assert.equal(needsPowerShellOnWindows('git log --grep="(Get-Item)"'), false);
    assert.equal(needsPowerShellOnWindows('rg "(get-user|set-user)" src/'), false);
    assert.equal(
      needsPowerShellOnWindows('node -e "console.log(JSON.stringify({ msg: \\"(get-id)\\" }))"'),
      false,
    );
    assert.equal(needsPowerShellOnWindows("echo '$env:PATH'"), false);
    assert.equal(needsPowerShellOnWindows('rg "\\$env:FOO" src/'), false);
    assert.equal(
      needsPowerShellOnWindows('"D:\\Program Files\\nodejs\\node.exe" -e "console.log(1)"'),
      false,
    );
    assert.equal(needsPowerShellOnWindows("echo %USERPROFILE%"), false);
    assert.equal(
      needsPowerShellOnWindows('pwsh -NoProfile -Command "Write-Output ok"'),
      false,
    );

    // Quoted executable invocation vs PowerShell string expressions
    assert.equal(
      formatPowerShellCommand('"$Env:USERPROFILE\\bin\\tool.exe" --version'),
      '& "$Env:USERPROFILE\\bin\\tool.exe" --version',
    );
    assert.equal(
      formatPowerShellCommand("'C:\\Tools\\my`tool.exe' --help"),
      "& 'C:\\Tools\\my`tool.exe' --help",
    );
    assert.equal(formatPowerShellCommand('"hello" | Out-Null'), '"hello" | Out-Null');
    assert.equal(formatPowerShellCommand('"$Env:FOO" -eq "bar"'), '"$Env:FOO" -eq "bar"');
    assert.equal(formatPowerShellCommand('"{0}" -f $Env:USERNAME'), '"{0}" -f $Env:USERNAME');
    assert.equal(formatPowerShellCommand('"$Env:PATH".Split(\';\')'), '"$Env:PATH".Split(\';\')');
    assert.equal(formatPowerShellCommand('"$Env:USERPROFILE"[0]'), '"$Env:USERPROFILE"[0]');
    assert.equal(formatPowerShellCommand('"$Env:FOO" > out.txt'), '"$Env:FOO" > out.txt');
    assert.equal(formatPowerShellCommand('"$Env:FOO" 2> err.txt'), '"$Env:FOO" 2> err.txt');
    assert.equal(formatPowerShellCommand('"$Env:FOO" * 3'), '"$Env:FOO" * 3');
    assert.equal(formatPowerShellCommand('"$Env:A", "$Env:B"'), '"$Env:A", "$Env:B"');
    assert.equal(formatPowerShellCommand("'it''s' + $Env:FOO"), "'it''s' + $Env:FOO");
    assert.equal(formatPowerShellCommand('"a""b" -eq $Env:FOO'), '"a""b" -eq $Env:FOO');
    assert.equal(
      formatPowerShellCommand('node "$env:USERPROFILE\\a.js"'),
      'node "$env:USERPROFILE\\a.js"',
    );

    // UTF-8 prefix guards non-console hosts and avoids BOM
    const wrapped = buildPowerShellUtf8Command('Write-Output "ok"');
    assert.match(wrapped, /try \{ \[Console\]::OutputEncoding = \[System\.Text\.UTF8Encoding\]::new\(\$false\) \} catch \{\}/);
    assert.match(wrapped, /\$OutputEncoding = \[System\.Text\.UTF8Encoding\]::new\(\$false\)/);
    assert.ok(wrapped.endsWith('Write-Output "ok"'));
  });

  it("resolves pwsh -> powershell.exe -> null fallback chain and caches probe results", async () => {
    try {
      resetWindowsShellCache();
      const probed: string[] = [];
      const first = await resolveWindowsPowerShell(async (bin) => {
        probed.push(bin);
        return bin === "pwsh";
      });
      const cached = await resolveWindowsPowerShell(async (bin) => {
        probed.push(`unexpected-${bin}`);
        return false;
      });
      assert.equal(first, "pwsh");
      assert.equal(cached, "pwsh");
      assert.deepEqual(probed, ["pwsh"]);

      resetWindowsShellCache();
      const fallbackProbed: string[] = [];
      const second = await resolveWindowsPowerShell(async (bin) => {
        fallbackProbed.push(bin);
        return bin === "powershell.exe";
      });
      assert.equal(second, "powershell.exe");
      assert.deepEqual(fallbackProbed, ["pwsh", "powershell.exe"]);

      resetWindowsShellCache();
      const noneProbed: string[] = [];
      const third = await resolveWindowsPowerShell(async (bin) => {
        noneProbed.push(bin);
        return false;
      });
      assert.equal(third, null);
      assert.deepEqual(noneProbed, ["pwsh", "powershell.exe"]);
    } finally {
      resetWindowsShellCache();
    }
  });

  it("decodes UTF-8, truncated UTF-8 tails, and legacy codepage buffers cleanly", () => {
    assert.equal(parseWindowsCodePage("Active code page: 936\r\n"), "gb18030");
    assert.equal(parseWindowsCodePage("Active code page: 866\r\n"), "ibm866");
    assert.equal(parseWindowsCodePage("Active code page: 874\r\n"), "windows-874");
    assert.equal(parseWindowsCodePage("Active code page: 1253\r\n"), "windows-1253");
    assert.equal(parseWindowsCodePage("Active code page: 1255\r\n"), "windows-1255");
    assert.equal(parseWindowsCodePage("Active code page: 1257\r\n"), "windows-1257");
    assert.equal(parseWindowsCodePage("Active code page: 1258\r\n"), "windows-1258");
    assert.equal(parseWindowsCodePage("Active code page: 65001\r\n"), null);
    assert.equal(parseWindowsCodePage("no digits here"), null);

    const utf8 = Buffer.from("中文 UTF-8 输出 OK", "utf8");
    assert.equal(decodeProcessOutput(utf8, false, "gb18030"), "中文 UTF-8 输出 OK");

    // Simulate maxOutputBytes cutting a 3-byte UTF-8 character mid-sequence
    const midCut = Buffer.concat([Buffer.from("中文", "utf8"), Buffer.from([0xe8, 0xbe])]);
    assert.equal(decodeProcessOutput(midCut, true, "gb18030"), "中文");

    // GBK bytes for "'x' 不是"
    const gbk = Buffer.from("27782720b2bbcac7", "hex");
    assert.equal(decodeProcessOutput(gbk, false, "gb18030"), "'x' 不是");
  });

  if (process.platform === "win32") {
    it("executes PowerShell $env: variables, UTF-8 Unicode output, and cmdlets cleanly on Windows", async () => {
      const prev = process.env.AGY_FAST_PWSH_TEST;
      process.env.AGY_FAST_PWSH_TEST = "pwsh_env_expanded_ok";
      try {
        const res = await runChecks({
          outputMode: "full",
          commands: [
            {
              name: "win-ps-env",
              command: `${nodeBin} -e "console.log(process.argv[1])" "$Env:AGY_FAST_PWSH_TEST"`,
            },
            {
              name: "win-ps-unicode",
              command: 'Write-Output "✔ 🚀 $Env:AGY_FAST_PWSH_TEST"',
            },
            {
              name: "win-missing-cmd-encoding",
              command: "agy_fast_nonexistent_cmd_xyz --version",
            },
          ],
        });
        assert.equal(res.isError, false);
        assert.match(res.text, /Checks: 2\/3 passed, 1 failed/);
        assert.match(res.text, /✔ 🚀 pwsh_env_expanded_ok/);
        assert.doesNotMatch(res.text, /\uFFFD/);
      } finally {
        if (prev === undefined) {
          delete process.env.AGY_FAST_PWSH_TEST;
        } else {
          process.env.AGY_FAST_PWSH_TEST = prev;
        }
      }
    });
  }
});
