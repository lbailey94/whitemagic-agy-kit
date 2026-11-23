import { spawn, spawnSync } from "node:child_process";
import path from "node:path";

export interface ExecResult {
  exitCode: number | null;
  signal: NodeJS.Signals | null;
  timedOut: boolean;
  durationMs: number;
  stdout: string;
  stderr: string;
  truncated: boolean;
  error?: string;
}

export interface ExecOptions {
  cwd: string;
  timeoutMs: number;
  maxOutputBytes?: number;
  env?: NodeJS.ProcessEnv;
}

const DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024; // 1 MiB

const WINDOWS_CODEPAGE_LABELS: Record<number, string> = {
  866: "ibm866",
  874: "windows-874",
  936: "gb18030",
  54936: "gb18030",
  950: "big5",
  932: "shift_jis",
  949: "euc-kr",
  1250: "windows-1250",
  1251: "windows-1251",
  1252: "windows-1252",
  1253: "windows-1253",
  1254: "windows-1254",
  1255: "windows-1255",
  1256: "windows-1256",
  1257: "windows-1257",
  1258: "windows-1258",
  65001: "utf-8",
};

const strictUtf8Decoder = new TextDecoder("utf-8", { fatal: true });
let cachedWindowsFallbackEncoding: string | null | undefined;
const fallbackDecoderCache = new Map<string, TextDecoder>();

export function parseWindowsCodePage(output: string): string | null {
  const match = /\b(\d{3,5})\b/.exec(output);
  if (!match) return null;
  const cp = Number.parseInt(match[1]!, 10);
  const mapped = WINDOWS_CODEPAGE_LABELS[cp];
  return mapped && mapped !== "utf-8" ? mapped : null;
}

function stripTrailingIncompleteUtf8(buf: Buffer): Buffer {
  if (buf.length === 0) return buf;
  let i = buf.length - 1;
  const minIdx = Math.max(0, buf.length - 4);
  while (i >= minIdx && (buf[i]! & 0xc0) === 0x80) {
    i--;
  }
  if (i < minIdx) return buf;
  const lead = buf[i]!;
  if ((lead & 0x80) === 0) return buf;
  let expectedLen = 0;
  if ((lead & 0xe0) === 0xc0) expectedLen = 2;
  else if ((lead & 0xf0) === 0xe0) expectedLen = 3;
  else if ((lead & 0xf8) === 0xf0) expectedLen = 4;
  else return buf;
  return buf.length - i < expectedLen ? buf.subarray(0, i) : buf;
}

function getWindowsSystem32Binary(name: string): { bin: string; cwd: string } {
  const systemRoot = process.env.SystemRoot || process.env.WINDIR || "";
  if (systemRoot) {
    return {
      bin: path.join(systemRoot, "System32", name),
      cwd: systemRoot,
    };
  }
  return { bin: name, cwd: process.cwd() };
}

function getWindowsFallbackEncoding(): string | null {
  if (cachedWindowsFallbackEncoding !== undefined) {
    return cachedWindowsFallbackEncoding;
  }
  try {
    const { bin: chcpBin, cwd } = getWindowsSystem32Binary("chcp.com");
    const res = spawnSync(chcpBin, [], {
      cwd,
      encoding: "latin1",
      timeout: 1500,
      windowsHide: true,
    });
    if (!res.error && res.status === 0) {
      const mapped = parseWindowsCodePage(res.stdout || "");
      if (mapped) {
        cachedWindowsFallbackEncoding = mapped;
        return cachedWindowsFallbackEncoding;
      }
    }
  } catch {
    // Ignore probe errors and fall back to UTF-8
  }
  cachedWindowsFallbackEncoding = null;
  return null;
}

/**
 * Decodes child-process stdout/stderr buffers as UTF-8 first, falling back to
 * the Windows active console codepage (e.g. `gb18030` for CP936) only when
 * non-UTF-8 bytes are present.
 */
export function decodeProcessOutput(
  buf: Buffer,
  truncated = false,
  fallbackEncoding?: string,
): string {
  if (buf.length === 0) return "";
  const candidate = truncated ? stripTrailingIncompleteUtf8(buf) : buf;
  try {
    return strictUtf8Decoder.decode(candidate);
  } catch {
    const label =
      fallbackEncoding ??
      (process.platform === "win32" ? getWindowsFallbackEncoding() : null);
    if (!label || label === "utf-8") {
      return buf.toString("utf8");
    }
    try {
      let decoder = fallbackDecoderCache.get(label);
      if (!decoder) {
        decoder = new TextDecoder(label);
        fallbackDecoderCache.set(label, decoder);
      }
      return decoder.decode(buf);
    } catch {
      return buf.toString("utf8");
    }
  }
}

/**
 * agy aborts an MCP tool call after 180 s ("timed out after 3m0s") and drops
 * its result. All commands of one tool call share this budget; the remaining
 * 10 s cover the kill grace period below and response formatting.
 */
export const MCP_CALL_BUDGET_SEC = 170;

function killChildTree(pid: number | undefined, detached: boolean): void {
  if (!pid) return;
  if (process.platform === "win32") {
    try {
      const killer = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
      killer.unref();
    } catch {
      try {
        process.kill(pid, "SIGTERM");
      } catch {
        // Already exited
      }
    }
    return;
  }

  try {
    if (detached) {
      process.kill(-pid, "SIGTERM");
    } else {
      process.kill(pid, "SIGTERM");
    }
  } catch {
    // Process may already have exited
  }
  setTimeout(() => {
    try {
      if (detached) {
        process.kill(-pid, "SIGKILL");
      } else {
        process.kill(pid, "SIGKILL");
      }
    } catch {
      // Already exited
    }
  }, 400).unref();
}

export function execBinaryAsync(
  bin: string,
  args: string[],
  options: ExecOptions,
): Promise<ExecResult> {
  return spawnAndCollect(bin, args, false, options);
}

export type WindowsPowerShellBin = "pwsh" | "powershell.exe";
export type WindowsShellProbe = (bin: WindowsPowerShellBin) => Promise<boolean>;

const WINDOWS_PS_DOUBLE_QUOTABLE_VAR_RE =
  /(?<![\\`])(?:\$env:[A-Za-z_][A-Za-z0-9_]*|\$(?:LASTEXITCODE|PSVersionTable|PSScriptRoot|PROFILE)\b)/i;
const WINDOWS_PS_UNQUOTED_SYNTAX_RE =
  /(?<![\\`])\$(?:HOME|PWD|null|true|false|_)\b|\$\([^)]|@\(|(?:^|[;\r\n])\s*\$[A-Za-z_][A-Za-z0-9_]*\s*=/im;
const WINDOWS_PS_CMDLET_RE =
  /(?:^|[;|&({\r\n]\s*)(?:Get|Set|New|Remove|Test|Select|Where|ForEach|Sort|Group|Measure|Compare|ConvertTo|ConvertFrom|Export|Import|Invoke|Start|Stop|Restart|Wait|Read|Write|Out|Format|Resolve|Join|Split|Copy|Move|Rename|Clear|Add|Push|Pop|Enable|Disable)-[A-Z][A-Za-z0-9]*\b(?!-)/;
const EXPLICIT_POWERSHELL_INVOCATION_RE =
  /^\s*(?:["']?)(?:pwsh|powershell)(?:\.exe)?(?:["']?)(?:\s|$)/i;
const PS_NON_INVOCATION_TAIL_RE =
  /^(?:[|;&+}=),*?]|(?:[1-6*])?>|-(?:c|i)?(?:eq|ne|gt|ge|lt|le|like|notlike|match|notmatch|replace|split|join|in|notin|contains|notcontains|is|isnot|as|and|or|xor|shl|shr|band|bor|bxor)\b|-f\b|$)/i;
const PS_UTF8_PREFIX =
  "try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}; $OutputEncoding = [System.Text.UTF8Encoding]::new($false);";

function stripQuotedSpans(command: string, stripDoubleQuotes: boolean): string {
  let out = "";
  let quote: '"' | "'" | null = null;
  for (let i = 0; i < command.length; i++) {
    const ch = command[i]!;
    const next = command[i + 1];
    if (quote === "'") {
      if (ch === "'") {
        if (next === "'") {
          i++;
        } else {
          quote = null;
          out += " ";
        }
      }
      continue;
    }
    if (quote === '"') {
      if ((ch === "\\" || ch === "`") && next === '"') {
        i++;
        if (!stripDoubleQuotes) out += ch + next;
        continue;
      }
      if (ch === '"') {
        if (next === '"') {
          i++;
          if (!stripDoubleQuotes) out += '""';
        } else {
          quote = null;
          out += stripDoubleQuotes ? " " : '"';
        }
        continue;
      }
      if (!stripDoubleQuotes) out += ch;
      continue;
    }
    if (ch === "'") {
      quote = "'";
      out += " ";
      continue;
    }
    if (ch === '"') {
      quote = '"';
      out += stripDoubleQuotes ? " " : '"';
      continue;
    }
    out += ch;
  }
  return out;
}

/**
 * Detects whether a Windows command uses PowerShell syntax (`$env:VAR`, automatic
 * variables, subexpressions, or PascalCase `Verb-Noun` cmdlets outside quotes)
 * so cross-platform/cmd commands keep fast `cmd.exe` execution without altering
 * quoting behavior.
 */
export function needsPowerShellOnWindows(command: string): boolean {
  if (!command || EXPLICIT_POWERSHELL_INVOCATION_RE.test(command)) {
    return false;
  }
  const withoutSingle = stripQuotedSpans(command, false);
  if (WINDOWS_PS_DOUBLE_QUOTABLE_VAR_RE.test(withoutSingle)) {
    return true;
  }
  const unquoted = stripQuotedSpans(command, true);
  return (
    WINDOWS_PS_UNQUOTED_SYNTAX_RE.test(unquoted) ||
    WINDOWS_PS_CMDLET_RE.test(unquoted)
  );
}

/**
 * Prepends `&` when a PowerShell command starts with a quoted executable path
 * followed by whitespace + arguments (e.g. `"C:\Program Files\nodejs\node.exe" -v`),
 * while leaving PowerShell string expressions (`"$Env:PATH".Split(';')`,
 * `"$Env:FOO" > out.txt`, `'it''s' + $Env:FOO`, etc.) untouched.
 */
export function formatPowerShellCommand(command: string): string {
  const trimmed = command.trim();
  if (!trimmed || (trimmed[0] !== '"' && trimmed[0] !== "'")) {
    return command;
  }
  const quote = trimmed[0];
  let idx = 1;
  while (idx < trimmed.length) {
    if (quote === '"' && trimmed[idx] === "`" && idx + 1 < trimmed.length) {
      idx += 2;
      continue;
    }
    if (trimmed[idx] === quote) {
      // Both PowerShell '...' (`''`) and "..." (`""`) escape doubled quotes
      if (trimmed[idx + 1] === quote) {
        idx += 2;
        continue;
      }
      const afterQuote = trimmed.slice(idx + 1);
      if (!/^\s+/.test(afterQuote)) {
        return command;
      }
      const rest = afterQuote.trimStart();
      if (rest && !PS_NON_INVOCATION_TAIL_RE.test(rest)) {
        return `& ${trimmed}`;
      }
      return command;
    }
    idx++;
  }
  return command;
}

export function buildPowerShellUtf8Command(command: string): string {
  return `${PS_UTF8_PREFIX} ${formatPowerShellCommand(command)}`;
}

let cachedWindowsPowerShell: WindowsPowerShellBin | null | undefined;
let cachedWindowsPowerShellExecPath: string | null = null;
let pendingWindowsPowerShellProbe: Promise<WindowsPowerShellBin | null> | null = null;

async function defaultProbeWindowsShell(bin: WindowsPowerShellBin): Promise<boolean> {
  const { bin: whereBin, cwd } = getWindowsSystem32Binary("where.exe");
  const exeQuery = bin.toLowerCase().endsWith(".exe") ? bin : `${bin}.exe`;
  const whereRes = await execBinaryAsync(whereBin, [exeQuery], {
    cwd,
    timeoutMs: 2000,
    maxOutputBytes: 4096,
  });
  if (!whereRes.error && !whereRes.timedOut) {
    if (whereRes.exitCode !== 0) return false;
    const resolved = whereRes.stdout
      .split(/\r?\n/)
      .map((line) => line.trim())
      .find((line) => /\.exe$/i.test(line));
    if (!resolved) return false;
    cachedWindowsPowerShellExecPath = resolved;
    return true;
  }
  const res = await execBinaryAsync(
    exeQuery,
    ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "exit 0"],
    {
      cwd,
      timeoutMs: 3000,
      maxOutputBytes: 4096,
    },
  );
  if (!res.error && !res.timedOut && res.exitCode === 0) {
    cachedWindowsPowerShellExecPath = exeQuery;
    return true;
  }
  return false;
}

/**
 * Resolves the preferred PowerShell executable on Windows (`pwsh` -> `powershell.exe`
 * -> `null`), caching the probe result across calls.
 */
export async function resolveWindowsPowerShell(
  probe: WindowsShellProbe = defaultProbeWindowsShell,
): Promise<WindowsPowerShellBin | null> {
  if (cachedWindowsPowerShell !== undefined) {
    return cachedWindowsPowerShell;
  }
  if (pendingWindowsPowerShellProbe) {
    return pendingWindowsPowerShellProbe;
  }

  pendingWindowsPowerShellProbe = (async () => {
    try {
      cachedWindowsPowerShellExecPath = null;
      if (await probe("pwsh")) {
        cachedWindowsPowerShell = "pwsh";
        return cachedWindowsPowerShell;
      }
      cachedWindowsPowerShellExecPath = null;
      if (await probe("powershell.exe")) {
        cachedWindowsPowerShell = "powershell.exe";
        return cachedWindowsPowerShell;
      }
      cachedWindowsPowerShellExecPath = null;
      cachedWindowsPowerShell = null;
      return null;
    } finally {
      pendingWindowsPowerShellProbe = null;
    }
  })();

  return pendingWindowsPowerShellProbe;
}

export function resetWindowsShellCache(): void {
  cachedWindowsPowerShell = undefined;
  cachedWindowsPowerShellExecPath = null;
  pendingWindowsPowerShellProbe = null;
}

export async function runShellCommandAsync(
  command: string,
  options: ExecOptions,
): Promise<ExecResult> {
  if (process.platform === "win32" && needsPowerShellOnWindows(command)) {
    const psBin = await resolveWindowsPowerShell();
    if (psBin) {
      const utf8Command = buildPowerShellUtf8Command(command);
      return spawnAndCollect(
        cachedWindowsPowerShellExecPath ?? psBin,
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", utf8Command],
        false,
        options,
      );
    }
  }
  return spawnAndCollect(command, [], true, options);
}

function spawnAndCollect(
  commandOrBin: string,
  args: string[],
  shell: boolean,
  options: ExecOptions,
): Promise<ExecResult> {
  const startMs = Date.now();
  const maxOutputBytes = options.maxOutputBytes ?? DEFAULT_MAX_OUTPUT_BYTES;
  const detached = process.platform !== "win32";

  return new Promise<ExecResult>((resolve) => {
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let stdoutTruncated = false;
    let stderrTruncated = false;
    let timedOut = false;
    let settled = false;

    const stdoutChunks: Buffer[] = [];
    const stderrChunks: Buffer[] = [];

    let child;
    try {
      child = spawn(commandOrBin, args, {
        cwd: options.cwd,
        shell,
        detached,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
        env: options.env ?? process.env,
      });
    } catch (err) {
      resolve({
        exitCode: null,
        signal: null,
        timedOut: false,
        durationMs: Date.now() - startMs,
        stdout: "",
        stderr: "",
        truncated: false,
        error: err instanceof Error ? err.message : String(err),
      });
      return;
    }

    let settleFallbackTimer: NodeJS.Timeout | null = null;
    const timer = setTimeout(() => {
      timedOut = true;
      killChildTree(child.pid, detached);
      settleFallbackTimer = setTimeout(() => {
        child.stdout?.destroy();
        child.stderr?.destroy();
        finish(null, "SIGKILL");
      }, 1500);
      settleFallbackTimer.unref();
    }, Math.max(100, options.timeoutMs));
    timer.unref();

    const finish = (
      exitCode: number | null,
      signal: NodeJS.Signals | null,
      error?: string,
    ) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (settleFallbackTimer) clearTimeout(settleFallbackTimer);
      resolve({
        exitCode,
        signal,
        timedOut,
        durationMs: Date.now() - startMs,
        stdout: decodeProcessOutput(Buffer.concat(stdoutChunks), stdoutTruncated),
        stderr: decodeProcessOutput(Buffer.concat(stderrChunks), stderrTruncated),
        truncated: stdoutTruncated || stderrTruncated,
        error,
      });
    };

    child.stdout?.on("data", (chunk: Buffer) => {
      if (stdoutBytes >= maxOutputBytes) {
        stdoutTruncated = true;
        return;
      }
      const remaining = maxOutputBytes - stdoutBytes;
      if (chunk.length > remaining) {
        stdoutChunks.push(chunk.subarray(0, remaining));
        stdoutBytes += remaining;
        stdoutTruncated = true;
      } else {
        stdoutChunks.push(chunk);
        stdoutBytes += chunk.length;
      }
    });

    child.stderr?.on("data", (chunk: Buffer) => {
      if (stderrBytes >= maxOutputBytes) {
        stderrTruncated = true;
        return;
      }
      const remaining = maxOutputBytes - stderrBytes;
      if (chunk.length > remaining) {
        stderrChunks.push(chunk.subarray(0, remaining));
        stderrBytes += remaining;
        stderrTruncated = true;
      } else {
        stderrChunks.push(chunk);
        stderrBytes += chunk.length;
      }
    });

    child.on("error", (err) => {
      finish(null, null, err.message);
    });

    child.on("close", (code, signal) => {
      finish(code, signal);
    });
  });
}

let cachedRgAvailable: boolean | null = null;

export async function isRipgrepAvailable(): Promise<boolean> {
  if (cachedRgAvailable !== null) return cachedRgAvailable;
  const res = await execBinaryAsync("rg", ["--version"], {
    cwd: process.cwd(),
    timeoutMs: 2000,
    maxOutputBytes: 4096,
  });
  cachedRgAvailable = !res.error && res.exitCode === 0;
  return cachedRgAvailable;
}

export function resetRipgrepCache(): void {
  cachedRgAvailable = null;
}
