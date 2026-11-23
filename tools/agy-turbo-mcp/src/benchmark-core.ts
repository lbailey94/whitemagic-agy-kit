import { promises as fs } from "node:fs";
import path from "node:path";

export interface TranscriptToolCall {
  name?: string;
  args?: Record<string, unknown> | string;
  arguments?: Record<string, unknown> | string;
}

export interface AnalyzeOptions {
  recent?: number;
  sinceMs?: number;
  untilMs?: number;
}

export interface TranscriptStep {
  step_index?: number;
  source?: string;
  type?: string;
  status?: string;
  created_at?: string;
  input_tokens?: number;
  cache_read_tokens?: number;
  output_tokens?: number;
  tool_calls?: TranscriptToolCall[];
  content?: string;
  media?: Array<{ mime_type?: string; uri?: string }>;
}

export interface BenchmarkMetrics {
  sessions: number;
  sessionsWithAgyFast: number;
  sessionsWithSnapshot: number;
  llmTurns: number;
  toolTurns: number;
  totalToolCalls: number;
  singleToolTurnRatio: number;
  consecutiveSingleChainsExtraTurns: number;
  activeTimeSec: number;
  llmTimeSec: number;
  streamWaitSec: number;
  toolExecTimeSec: number;
  totalOutputTokens: number;
  effectiveTps: number;
  editFileCalls: number;
  redundantSameTurnEditFileCalls: number;
  runCommandCalls: number;
  readonlySearchRunCommandCalls: number;
  agyFastToolCalls: Record<string, number>;
  mcpSpillCalls: number;
  agyTruncatedCalls: number;
  agyErrorCalls: number;
  agyCorruptedArgCalls: number;
  agyCorruptedArgWastedSec: number;
  agyCorruptedArgWastedOutTokens: number;
  swarmWorkerSessions: number;
  swarmWorkerSessionsWithAgyFast: number;
  multiMcpTurns: number;
  multiRunCommandTurns: number;
  mediaLoadedCount: number;
  multimodalHighCtxTurns: number;
  avgContextTokens: number;
  over150kContextRatio: number;
  avgCacheHitRate: number;
  over60sGapRatio: number;
}

export interface UsageSplitMetrics {
  overall: BenchmarkMetrics;
  withAgyFast: BenchmarkMetrics;
  withoutAgyFast: BenchmarkMetrics;
}

const BLOCKING_TOOLS = new Set([
  "ask_question",
  "swarm_wait",
  "schedule",
  "manage_subagents",
  "wait_for_subagents",
]);

const READONLY_SIMPLE_CMDS = new Set([
  "rg",
  "grep",
  "egrep",
  "fgrep",
  "ls",
  "tree",
  "wc",
  "head",
  "tail",
  "cat",
  "stat",
  "file",
]);

const BENIGN_GLUE_CMDS = new Set([
  "cd",
  "true",
  ":",
  "echo",
  "printf",
  "pwd",
  "sort",
  "uniq",
  "cut",
  "tr",
  "nl",
  "column",
  "rev",
  "paste",
  "fold",
  "basename",
  "dirname",
  "less",
  "more",
]);

const FIND_MUTATING_ARGS = new Set([
  "-delete",
  "-exec",
  "-execdir",
  "-ok",
  "-okdir",
  "-fprint",
  "-fprint0",
  "-fprintf",
  "-fls",
]);

const FD_MUTATING_ARGS = new Set(["-x", "--exec", "-X", "--exec-batch"]);

const GIT_GLOBAL_VALUE_FLAGS = new Set([
  "-C",
  "-c",
  "--git-dir",
  "--work-tree",
  "--namespace",
  "--super-prefix",
  "--config-env",
]);

const GIT_GLOBAL_BOOL_FLAGS = new Set([
  "--no-pager",
  "-P",
  "--paginate",
  "-p",
  "--bare",
  "--no-replace-objects",
  "--literal-pathspecs",
  "--glob-pathspecs",
  "--noglob-pathspecs",
  "--icase-pathspecs",
  "--no-optional-locks",
]);

const GIT_ALWAYS_READONLY_SUBCMDS = new Set([
  "status",
  "diff",
  "log",
  "show",
  "rev-parse",
  "ls-files",
  "grep",
  "blame",
  "shortlog",
  "describe",
  "ls-tree",
  "cat-file",
  "rev-list",
  "show-ref",
  "for-each-ref",
  "name-rev",
  "merge-base",
  "check-ignore",
]);

const GIT_BRANCH_VALUE_FLAGS = new Set([
  "--contains",
  "--no-contains",
  "--merged",
  "--no-merged",
  "--points-at",
  "--sort",
  "--format",
]);

const KNOWN_SYSTEM_BIN_PREFIXES = [
  "/bin/",
  "/usr/bin/",
  "/usr/local/bin/",
  "/opt/homebrew/bin/",
];

const AGY_FAST_TOOL_RE =
  /(?:^|[_:./-])(batch_search|apply_edits|read_slices|run_checks|code_graph)$/;

function parseTimeMs(iso?: string): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

function unwrapJsonString(val: unknown): string {
  if (typeof val !== "string") return "";
  const trimmed = val.trim();
  if (trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"')) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (typeof parsed === "string") return parsed;
    } catch {
      // May be a middle-truncated JSON-encoded string (e.g. `"rg ... \n<truncated N bytes>\n... src/"`)
    }
  }
  if (trimmed.startsWith('"') && /<truncated \d+ bytes>/.test(trimmed)) {
    // Antigravity transcript.jsonl truncates long JSON-encoded string args either at the end or in the middle
    let inner = trimmed.slice(1);
    if (inner.endsWith('"')) inner = inner.slice(0, -1);
    const withoutMarkers = inner.replace(/\r?\n?<truncated \d+ bytes>\r?\n?/g, " ");
    return withoutMarkers.replace(
      /\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))/g,
      (_m, hex: string | undefined, ch: string | undefined) => {
        if (hex !== undefined) {
          return String.fromCharCode(Number.parseInt(hex, 16));
        }
        switch (ch) {
          case "n":
            return "\n";
          case "r":
            return "\r";
          case "t":
            return "\t";
          case "b":
            return "\b";
          case "f":
            return "\f";
          default:
            return ch ?? "";
        }
      },
    );
  }
  return val;
}

function getCallArgs(
  call: TranscriptToolCall,
): Record<string, unknown> | undefined {
  const raw = call.args ?? call.arguments;
  if (!raw) return undefined;
  if (typeof raw === "string") {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      return undefined;
    }
    return undefined;
  }
  if (typeof raw === "object" && !Array.isArray(raw)) {
    return raw;
  }
  return undefined;
}

function getCommandString(args?: Record<string, unknown>): string {
  if (!args) return "";
  const val =
    args.CommandLine ?? args.commandLine ?? args.command ?? args.cmd ?? "";
  return unwrapJsonString(val);
}

function getTargetPath(args?: Record<string, unknown>): string {
  if (!args) return "";
  const val =
    args.Path ??
    args.path ??
    args.TargetFile ??
    args.targetFile ??
    args.file_path ??
    args.filePath ??
    "";
  return unwrapJsonString(val);
}

interface ShellToken {
  value: string;
  quoted: boolean;
  isEnvAssign: boolean;
}

/**
 * Splits a shell command string into top-level command segments across
 * `&&`, `||`, `|`, `;`, and newlines while respecting quoting and redirections.
 * Returns null if the command contains unsafe/mutating shell constructs
 * (non-/dev/null output redirections, heredocs, command substitutions,
 * subshells, or backgrounding).
 */
function parseSafeShellSegments(cmd: string): ShellToken[][] | null {
  const segments: ShellToken[][] = [];
  let current: ShellToken[] = [];
  const len = cmd.length;
  let i = 0;

  const pushSegment = (): void => {
    if (current.length > 0) {
      segments.push(current);
      current = [];
    }
  };

  const readWord = (): ShellToken | null => {
    let value = "";
    let quoted = false;
    let hasChars = false;
    let isEnvAssign = false;
    while (i < len) {
      const ch = cmd[i]!;
      if (ch === "'") {
        quoted = true;
        hasChars = true;
        i += 1;
        while (i < len && cmd[i] !== "'") {
          value += cmd[i]!;
          i += 1;
        }
        if (i < len) i += 1; // closing quote
        continue;
      }
      if (ch === '"') {
        quoted = true;
        hasChars = true;
        i += 1;
        while (i < len && cmd[i] !== '"') {
          const inner = cmd[i]!;
          if (inner === "\\" && i + 1 < len) {
            const nxt = cmd[i + 1]!;
            if (nxt === "\n") {
              i += 2;
              continue;
            }
            if (nxt === '"' || nxt === "\\" || nxt === "$" || nxt === "`") {
              value += nxt;
              i += 2;
              continue;
            }
          }
          if (inner === "`" || (inner === "$" && cmd[i + 1] === "(")) {
            return null;
          }
          value += inner;
          i += 1;
        }
        if (i < len) i += 1; // closing quote
        continue;
      }
      if (ch === "\\") {
        if (i + 1 < len) {
          const nxt = cmd[i + 1]!;
          if (nxt === "\n") {
            i += 2;
            continue;
          }
          if (nxt === "\r" && cmd[i + 2] === "\n") {
            i += 3;
            continue;
          }
          value += nxt;
          hasChars = true;
          i += 2;
          continue;
        }
        value += ch;
        hasChars = true;
        i += 1;
        continue;
      }
      if (ch === "`" || (ch === "$" && cmd[i + 1] === "(")) {
        return null;
      }
      if (
        ch === " " ||
        ch === "\t" ||
        ch === "\r" ||
        ch === "\n" ||
        ch === "|" ||
        ch === "&" ||
        ch === ";" ||
        ch === "<" ||
        ch === ">" ||
        ch === "(" ||
        ch === ")"
      ) {
        break;
      }
      if (
        ch === "=" &&
        !quoted &&
        !isEnvAssign &&
        /^[A-Za-z_][A-Za-z0-9_]*$/.test(value)
      ) {
        isEnvAssign = true;
      }
      value += ch;
      hasChars = true;
      i += 1;
    }
    return hasChars
      ? { value, quoted, isEnvAssign }
      : { value: "", quoted: false, isEnvAssign: false };
  };

  while (i < len) {
    const ch = cmd[i]!;

    // Horizontal whitespace
    if (ch === " " || ch === "\t") {
      i += 1;
      continue;
    }

    // Line continuation outside quotes
    if (ch === "\\" && (cmd[i + 1] === "\n" || (cmd[i + 1] === "\r" && cmd[i + 2] === "\n"))) {
      i += cmd[i + 1] === "\r" ? 3 : 2;
      continue;
    }

    // Comments (at start of token)
    if (ch === "#") {
      while (i < len && cmd[i] !== "\n") i += 1;
      continue;
    }

    // Segment separators
    if (ch === "\n" || ch === "\r" || ch === ";") {
      pushSegment();
      i += 1;
      continue;
    }
    if (ch === "|") {
      pushSegment();
      i += cmd[i + 1] === "|" ? 2 : 1;
      continue;
    }
    if (ch === "&") {
      if (cmd[i + 1] === "&") {
        pushSegment();
        i += 2;
        continue;
      }
      if (cmd[i + 1] === ">") {
        // `&>` or `&>>` output redirection
        i += cmd[i + 2] === ">" ? 3 : 2;
        while (i < len && (cmd[i] === " " || cmd[i] === "\t")) i += 1;
        const target = readWord();
        if (!target || target.value !== "/dev/null") return null;
        continue;
      }
      // Single `&` (background job)
      return null;
    }

    // Subshells are not simple readonly search commands
    if (ch === "(" || ch === ")") {
      return null;
    }

    // Check for fd-prefixed redirection like `2>/dev/null`, `2>&1`, `1>file`
    let savedPos = i;
    let hasFdPrefix = false;
    while (i < len && cmd[i]! >= "0" && cmd[i]! <= "9") {
      i += 1;
      hasFdPrefix = true;
    }
    if (hasFdPrefix && (cmd[i] === ">" || cmd[i] === "<")) {
      // Fall through to redirection handling below
    } else {
      i = savedPos;
    }

    const op = cmd[i];
    if (op === ">") {
      i += 1;
      if (cmd[i] === "&") {
        // Fd duplication / close: `2>&1`, `>&2`, `2>&-`
        i += 1;
        while (i < len && (cmd[i] === " " || cmd[i] === "\t")) i += 1;
        const target = readWord();
        if (!target || !/^(?:\d+|-)$/.test(target.value)) return null;
        continue;
      }
      if (cmd[i] === ">" || cmd[i] === "|") i += 1;
      while (i < len && (cmd[i] === " " || cmd[i] === "\t")) i += 1;
      const target = readWord();
      if (!target || target.value !== "/dev/null") return null;
      continue;
    }

    if (op === "<") {
      if (cmd[i + 1] === "<") {
        if (cmd[i + 2] === "<") {
          // Herestring `<<<` is read-only
          i += 3;
          while (i < len && (cmd[i] === " " || cmd[i] === "\t")) i += 1;
          const word = readWord();
          if (!word) return null;
          continue;
        }
        // Heredoc `<<` / `<<-`
        return null;
      }
      if (cmd[i + 1] === ">") {
        // Read-write redirect `<>`
        return null;
      }
      // Input redirect `< file` or `<&fd`
      i += 1;
      if (cmd[i] === "&") i += 1;
      while (i < len && (cmd[i] === " " || cmd[i] === "\t")) i += 1;
      const target = readWord();
      if (!target) return null;
      continue;
    }

    const tok = readWord();
    if (!tok) return null;
    if (tok.value.length > 0 || tok.quoted) {
      current.push(tok);
    }
  }

  pushSegment();
  return segments;
}

function normalizeCommandName(raw: string): string {
  for (const prefix of KNOWN_SYSTEM_BIN_PREFIXES) {
    if (raw.startsWith(prefix)) {
      const rest = raw.slice(prefix.length);
      if (rest && !rest.includes("/")) return rest;
    }
  }
  return raw;
}

function isSedMutating(args: string[]): boolean {
  for (let i = 0; i < args.length; i++) {
    const a = args[i]!;
    if (a === "--") break;
    if (a === "--in-place" || a.startsWith("--in-place=")) return true;
    if (a === "-e" || a === "-f" || a === "--expression" || a === "--file") {
      i += 1;
      continue;
    }
    if (a.startsWith("-") && !a.startsWith("--") && a.length > 1) {
      for (let j = 1; j < a.length; j++) {
        const flag = a[j]!;
        if (flag === "e" || flag === "f") break;
        if (flag === "i") return true;
      }
    }
  }
  return false;
}

function isAwkMutating(args: string[]): boolean {
  for (let i = 0; i < args.length; i++) {
    const a = args[i]!;
    if (a === "--") break;
    if (a === "-i" && args[i + 1] === "inplace") return true;
    if (a === "-iinplace" || a === "--include=inplace") return true;
    if (a === "--include" && args[i + 1] === "inplace") return true;
  }
  return false;
}

function isReadonlyGitBranch(args: string[]): boolean {
  let explicitList = false;
  let positionalCount = 0;
  for (let i = 0; i < args.length; i++) {
    const a = args[i]!;
    if (a === "--") {
      positionalCount += args.length - i - 1;
      break;
    }
    if (a === "--list") {
      explicitList = true;
      continue;
    }
    if (
      a === "--delete" ||
      a === "--move" ||
      a === "--copy" ||
      a === "--force" ||
      a === "--unset-upstream" ||
      a === "--edit-description" ||
      a === "--create-reflog" ||
      a === "--track" ||
      a === "--no-track" ||
      a === "--set-upstream-to" ||
      a.startsWith("--set-upstream-to=")
    ) {
      return false;
    }
    if (GIT_BRANCH_VALUE_FLAGS.has(a)) {
      if (
        a === "--contains" ||
        a === "--no-contains" ||
        a === "--merged" ||
        a === "--no-merged" ||
        a === "--points-at"
      ) {
        explicitList = true;
      }
      if (args[i + 1] && !args[i + 1]!.startsWith("-")) {
        i += 1;
      }
      continue;
    }
    if (a.startsWith("--")) {
      const flagName = a.includes("=") ? a.slice(0, a.indexOf("=")) : a;
      if (
        flagName === "--contains" ||
        flagName === "--no-contains" ||
        flagName === "--merged" ||
        flagName === "--no-merged" ||
        flagName === "--points-at"
      ) {
        explicitList = true;
      }
      continue;
    }
    if (a.startsWith("-") && a.length > 1) {
      if (/[dDmMcCfu]/.test(a.slice(1))) return false;
      if (a.slice(1).includes("l")) explicitList = true;
      continue;
    }
    positionalCount += 1;
  }
  return explicitList || positionalCount === 0;
}

function isReadonlyGitRemote(args: string[]): boolean {
  for (const a of args) {
    if (a === "-v" || a === "--verbose") continue;
    if (a === "show" || a === "get-url") return true;
    return false;
  }
  return true;
}

function isReadonlyGitTag(args: string[]): boolean {
  let explicitList = false;
  let positionalCount = 0;
  for (let i = 0; i < args.length; i++) {
    const a = args[i]!;
    if (a === "--") {
      positionalCount += args.length - i - 1;
      break;
    }
    if (a === "-l" || a === "--list" || a === "--verify") {
      explicitList = true;
      continue;
    }
    if (
      a === "--delete" ||
      a === "--annotate" ||
      a === "--sign" ||
      a === "--force" ||
      a === "--message" ||
      a.startsWith("--message=") ||
      a === "--file" ||
      a.startsWith("--file=") ||
      a === "--local-user" ||
      a.startsWith("--local-user=")
    ) {
      return false;
    }
    if (GIT_BRANCH_VALUE_FLAGS.has(a)) {
      if (
        a === "--contains" ||
        a === "--no-contains" ||
        a === "--merged" ||
        a === "--no-merged" ||
        a === "--points-at"
      ) {
        explicitList = true;
      }
      if (args[i + 1] && !args[i + 1]!.startsWith("-")) {
        i += 1;
      }
      continue;
    }
    if (a.startsWith("-") && !a.startsWith("--") && a.length > 1) {
      if (/^-n\d*$/.test(a)) {
        explicitList = true;
        continue;
      }
      if (/[dasufmF]/.test(a.slice(1))) return false;
      if (/[lv]/.test(a.slice(1))) explicitList = true;
      continue;
    }
    if (a.startsWith("--")) {
      const flagName = a.includes("=") ? a.slice(0, a.indexOf("=")) : a;
      if (
        flagName === "--contains" ||
        flagName === "--no-contains" ||
        flagName === "--merged" ||
        flagName === "--no-merged" ||
        flagName === "--points-at"
      ) {
        explicitList = true;
      }
      continue;
    }
    positionalCount += 1;
  }
  return explicitList || positionalCount === 0;
}

function isReadonlyGit(args: string[]): boolean {
  let idx = 0;
  while (idx < args.length) {
    const a = args[idx]!;
    if (!a.startsWith("-")) break;
    if (GIT_GLOBAL_BOOL_FLAGS.has(a)) {
      idx += 1;
      continue;
    }
    if (GIT_GLOBAL_VALUE_FLAGS.has(a)) {
      idx += 2;
      continue;
    }
    if (
      (a.startsWith("-C") && a.length > 2) ||
      (a.startsWith("-c") && a.length > 2) ||
      a.startsWith("--git-dir=") ||
      a.startsWith("--work-tree=") ||
      a.startsWith("--namespace=") ||
      a.startsWith("--super-prefix=") ||
      a.startsWith("--config-env=")
    ) {
      idx += 1;
      continue;
    }
    return false;
  }

  const subcmd = args[idx];
  if (!subcmd) return false;
  const subArgs = args.slice(idx + 1);

  if (GIT_ALWAYS_READONLY_SUBCMDS.has(subcmd)) {
    if (subArgs.some((a) => a === "--output" || a.startsWith("--output="))) {
      return false;
    }
    return true;
  }
  if (subcmd === "branch") return isReadonlyGitBranch(subArgs);
  if (subcmd === "remote") return isReadonlyGitRemote(subArgs);
  if (subcmd === "tag") return isReadonlyGitTag(subArgs);
  return false;
}

function classifyShellSegment(
  tokens: ShellToken[],
): "readonly_search" | "benign_glue" | "other" {
  let idx = 0;
  // Skip leading inline KEY=VALUE env assignments
  while (idx < tokens.length && tokens[idx]!.isEnvAssign) {
    idx += 1;
  }
  if (idx >= tokens.length) return "benign_glue";

  // Handle simple `env KEY=VAL ... cmd` wrapper
  if (normalizeCommandName(tokens[idx]!.value) === "env") {
    idx += 1;
    while (idx < tokens.length) {
      const t = tokens[idx]!;
      if (t.value === "--") {
        idx += 1;
        break;
      }
      if (t.value === "-i") {
        idx += 1;
        continue;
      }
      if (t.value === "-u" && idx + 1 < tokens.length) {
        idx += 2;
        continue;
      }
      if (t.isEnvAssign) {
        idx += 1;
        continue;
      }
      if (t.value.startsWith("-")) return "other";
      break;
    }
    if (idx >= tokens.length) return "other";
  }

  const cmdName = normalizeCommandName(tokens[idx]!.value);
  const args = tokens.slice(idx + 1).map((t) => t.value);

  if (READONLY_SIMPLE_CMDS.has(cmdName)) return "readonly_search";
  if (cmdName === "find") {
    return args.some((a) => FIND_MUTATING_ARGS.has(a))
      ? "other"
      : "readonly_search";
  }
  if (cmdName === "fd") {
    return args.some(
      (a) =>
        FD_MUTATING_ARGS.has(a) ||
        a.startsWith("--exec=") ||
        a.startsWith("--exec-batch="),
    )
      ? "other"
      : "readonly_search";
  }
  if (cmdName === "sed") {
    return isSedMutating(args) ? "other" : "readonly_search";
  }
  if (cmdName === "awk" || cmdName === "gawk") {
    return isAwkMutating(args) ? "other" : "readonly_search";
  }
  if (cmdName === "git") {
    return isReadonlyGit(args) ? "readonly_search" : "other";
  }
  if (BENIGN_GLUE_CMDS.has(cmdName)) {
    return "benign_glue";
  }
  return "other";
}

function isReadonlySearchShell(cmd: string): boolean {
  if (!cmd.trim()) return false;
  const segments = parseSafeShellSegments(cmd);
  if (!segments || segments.length === 0) return false;
  let hasReadonlySearch = false;
  for (const seg of segments) {
    const kind = classifyShellSegment(seg);
    if (kind === "other") return false;
    if (kind === "readonly_search") hasReadonlySearch = true;
  }
  return hasReadonlySearch;
}

export function parseTranscriptJsonl(content: string): TranscriptStep[] {
  const steps: TranscriptStep[] = [];
  for (const rawLine of content.split(/\r?\n/)) {
    const trimmed = rawLine.trim();
    if (!trimmed) continue;
    try {
      const parsed = JSON.parse(trimmed) as TranscriptStep;
      if (parsed && typeof parsed === "object") {
        steps.push(parsed);
      }
    } catch {
      // Skip malformed line
    }
  }
  return steps;
}

function extractAgyFastToolName(call: TranscriptToolCall): string | null {
  const name = call.name ?? "";
  if (name === "call_mcp_tool") {
    const callArgs = getCallArgs(call);
    const serverName = unwrapJsonString(
      callArgs?.ServerName ?? callArgs?.serverName ?? callArgs?.server_name ?? "",
    );
    const toolName = unwrapJsonString(
      callArgs?.ToolName ?? callArgs?.toolName ?? callArgs?.tool_name ?? "",
    );
    const isFastServer =
      !serverName ||
      /(?:^|[_:/-])fast$/i.test(serverName) ||
      /agy-fast|whitemagic-agy-kit/i.test(serverName);
    const m = isFastServer ? AGY_FAST_TOOL_RE.exec(toolName) : null;
    return m?.[1] ?? null;
  }
  const m = AGY_FAST_TOOL_RE.exec(name);
  return m?.[1] ?? null;
}

function sessionHasStartupSnapshot(steps: TranscriptStep[]): boolean {
  const identitySteps = fullSessionStepsMap.get(steps) ?? steps;
  for (const step of identitySteps) {
    if (typeof step.content !== "string") continue;
    if (
      !step.content.includes("[agy-fast startup snapshot]") &&
      !step.content.includes("[SessionStart Cached Brief")
    ) {
      continue;
    }
    if (step.type === "SYSTEM_MESSAGE") return true;
    if (step.type === "USER_INPUT" && step.content.includes("SessionStart")) {
      return true;
    }
  }
  return false;
}

function isPlainRecord(val: unknown): val is Record<string, unknown> {
  return val !== null && typeof val === "object" && !Array.isArray(val);
}

function extractAgyFastInnerArgs(
  call: TranscriptToolCall,
): Record<string, unknown> | null {
  const outer = getCallArgs(call);
  if (!outer) return null;
  if ((call.name ?? "") !== "call_mcp_tool") return outer;
  let inner: unknown = outer.Arguments ?? outer.arguments;
  for (let i = 0; i < 2 && typeof inner === "string"; i++) {
    try {
      inner = JSON.parse(inner);
    } catch {
      break;
    }
  }
  return isPlainRecord(inner) ? inner : null;
}

function maybeParseJsonValue(val: unknown): unknown {
  if (typeof val !== "string") return val;
  const trimmed = val.trim();
  if (
    trimmed.startsWith("[") ||
    trimmed.startsWith("{") ||
    trimmed === "true" ||
    trimmed === "false" ||
    trimmed === "null"
  ) {
    try {
      return JSON.parse(trimmed);
    } catch {
      return val;
    }
  }
  return val;
}

function getCanonicalToolName(call: TranscriptToolCall): string {
  const name = call.name ?? "";
  if (name !== "call_mcp_tool") return name;
  const callArgs = getCallArgs(call);
  const serverName = unwrapJsonString(
    callArgs?.ServerName ?? callArgs?.serverName ?? callArgs?.server_name ?? "",
  );
  const toolName = unwrapJsonString(
    callArgs?.ToolName ?? callArgs?.toolName ?? callArgs?.tool_name ?? "",
  );
  if (toolName) {
    return serverName ? `call_mcp_tool:${serverName}/${toolName}` : `call_mcp_tool:${toolName}`;
  }
  return name;
}

function hasCorruptedAgyFastArgs(
  call: TranscriptToolCall,
  fastTool: string,
): boolean {
  const inner = extractAgyFastInnerArgs(call);
  if (!inner) return false;
  if (fastTool === "apply_edits") {
    const eds = maybeParseJsonValue(inner.edits);
    if (typeof eds === "boolean" || eds === null) return true;
    if (Array.isArray(eds)) {
      if (eds.length === 0) return true;
      return eds.some((e) => {
        if (!isPlainRecord(e)) return true;
        if (e.hunks !== undefined) {
          const hunks = maybeParseJsonValue(e.hunks);
          if (!Array.isArray(hunks)) return true;
          if (hunks.some((h) => !isPlainRecord(h))) {
            return true;
          }
        }
        return false;
      });
    }
    return false;
  }
  if (fastTool === "batch_search") {
    const qs = maybeParseJsonValue(inner.queries);
    if (typeof qs === "boolean" || qs === null) return true;
    return Array.isArray(qs) && qs.some((q) => !isPlainRecord(q));
  }
  if (fastTool === "read_slices") {
    const outlines = maybeParseJsonValue(inner.outlines);
    if (typeof outlines === "boolean" || outlines === null) return true;
    if (Array.isArray(outlines) && outlines.some((p) => typeof p !== "string")) {
      return true;
    }
    const slices = maybeParseJsonValue(inner.slices);
    if (typeof slices === "boolean" || slices === null) return true;
    if (Array.isArray(slices)) {
      return slices.some((s) => {
        if (!isPlainRecord(s)) return true;
        if (s.ranges !== undefined) {
          const ranges = maybeParseJsonValue(s.ranges);
          if (!Array.isArray(ranges)) return true;
          if (ranges.some((r) => typeof r === "boolean" || r === null)) return true;
        }
        return false;
      });
    }
    return false;
  }
  if (fastTool === "run_checks") {
    const cmds = maybeParseJsonValue(inner.commands);
    if (typeof cmds === "boolean" || cmds === null) return true;
    return (
      Array.isArray(cmds) &&
      cmds.some((c) => typeof c !== "string" && !isPlainRecord(c))
    );
  }
  return false;
}

const fullSessionStepsMap = new WeakMap<TranscriptStep[], TranscriptStep[]>();

function isSwarmWorkerSession(steps: TranscriptStep[]): boolean {
  const identitySteps = fullSessionStepsMap.get(steps) ?? steps;
  let hasUserRequest = false;
  let hasSwarmWorkerTool = false;
  for (const step of identitySteps) {
    if (step.type === "USER_INPUT") {
      const text = typeof step.content === "string" ? step.content : "";
      if (text.includes("<USER_REQUEST>") || text.trim().length > 0) {
        hasUserRequest = true;
      }
    }
    if (step.type === "PLANNER_RESPONSE" && Array.isArray(step.tool_calls)) {
      for (const tc of step.tool_calls) {
        const n = tc.name ?? "";
        if (n === "read_board" || n === "post_message" || n === "swarm_wait") {
          hasSwarmWorkerTool = true;
        }
      }
    }
  }
  return hasSwarmWorkerTool && !hasUserRequest;
}

export function sessionUsesAgyFast(steps: TranscriptStep[]): boolean {
  for (const step of steps) {
    if (step.type !== "PLANNER_RESPONSE" || !Array.isArray(step.tool_calls)) {
      continue;
    }
    for (const call of step.tool_calls) {
      if (extractAgyFastToolName(call) !== null) return true;
    }
  }
  return false;
}

export function filterStepsByTime(
  steps: TranscriptStep[],
  sinceMs?: number,
  untilMs?: number,
): TranscriptStep[] {
  if (sinceMs === undefined && untilMs === undefined) return steps;
  const out: TranscriptStep[] = [];
  for (const step of steps) {
    const ms = parseTimeMs(step.created_at);
    if (ms === null) continue;
    if (sinceMs !== undefined && ms < sinceMs) continue;
    if (untilMs !== undefined && ms > untilMs) continue;
    out.push(step);
  }
  fullSessionStepsMap.set(out, fullSessionStepsMap.get(steps) ?? steps);
  return out;
}

export function analyzeTranscriptSessions(
  sessions: TranscriptStep[][],
): BenchmarkMetrics {
  let validSessions = 0;
  let sessionsWithAgyFast = 0;
  let sessionsWithSnapshot = 0;
  let llmTurns = 0;
  let toolTurns = 0;
  let singleToolTurns = 0;
  let totalToolCalls = 0;
  let consecutiveSingleChainsExtraTurns = 0;
  let activeTimeSec = 0;
  let llmTimeSec = 0;
  let streamWaitSec = 0;
  let toolExecTimeSec = 0;
  let totalOutputTokens = 0;
  let editFileCalls = 0;
  let redundantSameTurnEditFileCalls = 0;
  let runCommandCalls = 0;
  let readonlySearchRunCommandCalls = 0;
  const agyFastToolCalls: Record<string, number> = {
    batch_search: 0,
    apply_edits: 0,
    read_slices: 0,
    run_checks: 0,
    code_graph: 0,
  };
  let mcpSpillCalls = 0;
  let agyTruncatedCalls = 0;
  let agyErrorCalls = 0;
  let agyCorruptedArgCalls = 0;
  let agyCorruptedArgWastedSec = 0;
  let agyCorruptedArgWastedOutTokens = 0;
  let swarmWorkerSessions = 0;
  let swarmWorkerSessionsWithAgyFast = 0;
  let multiMcpTurns = 0;
  let multiRunCommandTurns = 0;
  let mediaLoadedCount = 0;
  let multimodalHighCtxTurns = 0;

  let contextSum = 0;
  let contextCount = 0;
  let over150kCount = 0;
  let cacheHitSum = 0;
  let cacheHitCount = 0;
  let turnGapsTotal = 0;
  let turnGapsOver60s = 0;

  for (const steps of sessions) {
    if (steps.length === 0) continue;
    let hasPlanner = false;
    let sessionFastCalls = 0;
    let sessionMediaCount = 0;

    let prevSingleToolName: string | null = null;
    let prevPlannerMs: number | null = null;
    let prevTurnHadUserInputBetween = false;
    let prevTurnHadCalls = false;

    for (let i = 0; i < steps.length; i++) {
      const step = steps[i]!;
      if (Array.isArray(step.media) && step.media.length > 0) {
        sessionMediaCount += step.media.length;
      }
      if (step.type === "USER_INPUT") {
        prevSingleToolName = null;
        prevTurnHadUserInputBetween = true;
        prevTurnHadCalls = false;
        continue;
      }
      if (step.type === "SYSTEM_MESSAGE") {
        // An idle turn (calls=0) woken up later by an async background-task / subagent SYSTEM_MESSAGE
        // is an external wait, not active LLM latency or an active-turn gap.
        if (!prevTurnHadCalls) {
          prevSingleToolName = null;
          prevTurnHadUserInputBetween = true;
        }
        continue;
      }
      if (step.type !== "PLANNER_RESPONSE") continue;

      hasPlanner = true;
      llmTurns += 1;

      const tPlanner = parseTimeMs(step.created_at);
      if (
        prevPlannerMs !== null &&
        tPlanner !== null &&
        !prevTurnHadUserInputBetween
      ) {
        const gapSec = (tPlanner - prevPlannerMs) / 1000;
        if (gapSec >= 0 && gapSec <= 3600) {
          turnGapsTotal += 1;
          if (gapSec > 60) turnGapsOver60s += 1;
        }
      }
      prevTurnHadUserInputBetween = false;
      if (tPlanner !== null) prevPlannerMs = tPlanner;

      const outTok = Number(step.output_tokens ?? 0);
      if (Number.isFinite(outTok) && outTok > 0) {
        totalOutputTokens += outTok;
      }

      const inTok = Number(step.input_tokens ?? 0);
      const cacheTok = Number(step.cache_read_tokens ?? 0);
      const ctxTok =
        (Number.isFinite(inTok) ? inTok : 0) +
        (Number.isFinite(cacheTok) ? cacheTok : 0);
      if (ctxTok > 0) {
        contextSum += ctxTok;
        contextCount += 1;
        if (ctxTok >= 150_000) over150kCount += 1;
        if (sessionMediaCount > 0 && ctxTok >= 180_000) {
          multimodalHighCtxTurns += 1;
        }
        if (ctxTok >= 10_000) {
          cacheHitSum += cacheTok / ctxTok;
          cacheHitCount += 1;
        }
      }

      const calls = Array.isArray(step.tool_calls) ? step.tool_calls : [];
      const hasBlockingTool = calls.some(
        (c) => typeof c.name === "string" && BLOCKING_TOOLS.has(c.name),
      );

      const agyCallIndices: number[] = [];
      let turnCorruptedAgyCalls = 0;
      if (calls.length > 0) {
        toolTurns += 1;
        totalToolCalls += calls.length;

        if (calls.length === 1) {
          singleToolTurns += 1;
          const singleName = calls[0] ? getCanonicalToolName(calls[0]) : "";
          if (singleName && singleName === prevSingleToolName) {
            consecutiveSingleChainsExtraTurns += 1;
          }
          prevSingleToolName = singleName || null;
        } else {
          prevSingleToolName = null;
        }

        const editedFilesThisTurn = new Map<string, number>();
        let mcpCallsThisTurn = 0;
        let runCmdCallsThisTurn = 0;
        for (let callIdx = 0; callIdx < calls.length; callIdx++) {
          const call = calls[callIdx]!;
          const name = call.name ?? "";
          const callArgs = getCallArgs(call);
          if (name === "edit_file") {
            editFileCalls += 1;
            const p = getTargetPath(callArgs);
            if (p) {
              const cnt = (editedFilesThisTurn.get(p) ?? 0) + 1;
              editedFilesThisTurn.set(p, cnt);
              if (cnt > 1) redundantSameTurnEditFileCalls += 1;
            }
          } else if (name === "run_command") {
            runCommandCalls += 1;
            runCmdCallsThisTurn += 1;
            const cmd = getCommandString(callArgs);
            if (isReadonlySearchShell(cmd)) {
              readonlySearchRunCommandCalls += 1;
            }
          } else {
            const fastTool = extractAgyFastToolName(call);
            if (fastTool && agyFastToolCalls[fastTool] !== undefined) {
              agyFastToolCalls[fastTool] = (agyFastToolCalls[fastTool] ?? 0) + 1;
              sessionFastCalls += 1;
              mcpCallsThisTurn += 1;
              agyCallIndices.push(callIdx);
              if (hasCorruptedAgyFastArgs(call, fastTool)) {
                agyCorruptedArgCalls += 1;
                turnCorruptedAgyCalls += 1;
              }
            }
          }
        }
        if (mcpCallsThisTurn >= 2) multiMcpTurns += 1;
        if (runCmdCallsThisTurn >= 2) multiRunCommandTurns += 1;
      } else {
        prevSingleToolName = null;
      }

      prevTurnHadCalls = calls.length > 0;

      // Collect following tool/generic steps before next PLANNER_RESPONSE or USER_INPUT
      const genericTimes: number[] = [];
      const genericSteps: TranscriptStep[] = [];
      let nextPlannerMs: number | null = null;
      for (let j = i + 1; j < steps.length; j++) {
        const nxt = steps[j]!;
        if (nxt.type === "USER_INPUT") break;
        if (nxt.type === "SYSTEM_MESSAGE" && calls.length === 0) break;
        if (nxt.type === "PLANNER_RESPONSE") {
          nextPlannerMs = parseTimeMs(nxt.created_at);
          break;
        }
        if (nxt.type === "GENERIC") {
          genericSteps.push(nxt);
          const gt = parseTimeMs(nxt.created_at);
          if (gt !== null) genericTimes.push(gt);
        }
      }

      for (const callIdx of agyCallIndices) {
        const resStep = genericSteps[callIdx];
        if (!resStep) continue;
        const resText = typeof resStep.content === "string" ? resStep.content : "";
        const isSpill =
          resText.includes("The output was large and was saved to:") ||
          (/output was truncated/i.test(resText) && resText.includes("output.txt"));
        if (isSpill) {
          mcpSpillCalls += 1;
        } else if (
          resText.includes("[truncated:") ||
          resText.includes("[truncated at ") ||
          resText.includes("[truncated; showing ")
        ) {
          agyTruncatedCalls += 1;
        }
        if (
          resStep.status === "ERROR" ||
          resText.includes("Encountered error in step execution") ||
          resText.includes("Partial edit applied") ||
          resText.includes("Atomic edit aborted")
        ) {
          agyErrorCalls += 1;
        }
      }

      let turnActiveSec = 0;
      if (tPlanner !== null && !hasBlockingTool) {
        if (genericTimes.length > 0) {
          const firstGeneric = genericTimes[0]!;
          const lastGeneric = genericTimes[genericTimes.length - 1]!;
          const llmSec = Math.max(0, (firstGeneric - tPlanner) / 1000);
          const spreadSec = Math.max(0, (lastGeneric - firstGeneric) / 1000);
          const execSec =
            nextPlannerMs !== null
              ? Math.max(0, (nextPlannerMs - lastGeneric) / 1000)
              : 0;

          // Guard against extreme clock jumps / abandoned sessions
          if (llmSec <= 600) {
            llmTimeSec += llmSec;
            activeTimeSec += llmSec;
            turnActiveSec += llmSec;
          }
          if (spreadSec <= 600) {
            streamWaitSec += spreadSec;
            activeTimeSec += spreadSec;
            turnActiveSec += spreadSec;
          }
          if (execSec <= 600) {
            toolExecTimeSec += execSec;
            activeTimeSec += execSec;
            turnActiveSec += execSec;
          }
        } else if (nextPlannerMs !== null) {
          const spanSec = Math.max(0, (nextPlannerMs - tPlanner) / 1000);
          if (spanSec <= 600) {
            llmTimeSec += spanSec;
            activeTimeSec += spanSec;
            turnActiveSec += spanSec;
          }
        }
      }
      if (turnCorruptedAgyCalls > 0) {
        agyCorruptedArgWastedSec += turnActiveSec;
        if (Number.isFinite(outTok) && outTok > 0) {
          agyCorruptedArgWastedOutTokens += outTok;
        }
      }
    }

    if (hasPlanner) {
      validSessions += 1;
      mediaLoadedCount += sessionMediaCount;
      if (sessionFastCalls > 0) sessionsWithAgyFast += 1;
      if (sessionHasStartupSnapshot(steps)) sessionsWithSnapshot += 1;
      if (isSwarmWorkerSession(steps)) {
        swarmWorkerSessions += 1;
        if (sessionFastCalls > 0) swarmWorkerSessionsWithAgyFast += 1;
      }
    }
  }

  return {
    sessions: validSessions,
    sessionsWithAgyFast,
    sessionsWithSnapshot,
    llmTurns,
    toolTurns,
    totalToolCalls,
    singleToolTurnRatio: toolTurns > 0 ? singleToolTurns / toolTurns : 0,
    consecutiveSingleChainsExtraTurns,
    activeTimeSec: round2(activeTimeSec),
    llmTimeSec: round2(llmTimeSec),
    streamWaitSec: round2(streamWaitSec),
    toolExecTimeSec: round2(toolExecTimeSec),
    totalOutputTokens,
    effectiveTps: activeTimeSec > 0 ? round2(totalOutputTokens / activeTimeSec) : 0,
    editFileCalls,
    redundantSameTurnEditFileCalls,
    runCommandCalls,
    readonlySearchRunCommandCalls,
    agyFastToolCalls,
    mcpSpillCalls,
    agyTruncatedCalls,
    agyErrorCalls,
    agyCorruptedArgCalls,
    agyCorruptedArgWastedSec: round2(agyCorruptedArgWastedSec),
    agyCorruptedArgWastedOutTokens,
    swarmWorkerSessions,
    swarmWorkerSessionsWithAgyFast,
    multiMcpTurns,
    multiRunCommandTurns,
    mediaLoadedCount,
    multimodalHighCtxTurns,
    avgContextTokens: contextCount > 0 ? Math.round(contextSum / contextCount) : 0,
    over150kContextRatio: contextCount > 0 ? over150kCount / contextCount : 0,
    avgCacheHitRate: cacheHitCount > 0 ? cacheHitSum / cacheHitCount : 0,
    over60sGapRatio: turnGapsTotal > 0 ? turnGapsOver60s / turnGapsTotal : 0,
  };
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

function pct(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

const ALWAYS_SKIP_DIRS = new Set(["chunks", "node_modules", ".git"]);

const BRAIN_SKIP_DIRS = new Set([
  "scratch",
  "swarm",
  "media",
  ".tempmediaStorage",
  ".user_uploaded",
  "browser_sandbox",
  "browser_evidence",
]);

const SYSTEM_GENERATED_SKIP_DIRS = new Set(["steps", "messages", "tasks"]);

function isBrainPath(target: string): boolean {
  const parts = target.split(/[\\/]+/);
  return parts.includes(".system_generated") || parts.includes("brain");
}

export async function collectTranscriptFiles(
  inputs: string[],
): Promise<string[]> {
  const found: string[] = [];
  const seenRealFiles = new Set<string>();
  const visitedRealDirs = new Set<string>();

  async function recordFile(filePath: string): Promise<void> {
    let key: string;
    try {
      key = await fs.realpath(filePath);
    } catch {
      key = path.resolve(filePath);
    }
    if (seenRealFiles.has(key)) return;
    seenRealFiles.add(key);
    found.push(filePath);
  }

  async function walk(
    target: string,
    depth = 0,
    inBrainLayout = false,
  ): Promise<void> {
    if (depth > 10) return;
    let stat;
    try {
      stat = await fs.stat(target);
    } catch {
      return;
    }
    if (stat.isFile()) {
      if (target.endsWith(".jsonl")) await recordFile(target);
      return;
    }
    if (!stat.isDirectory()) return;

    let realDir: string;
    try {
      realDir = await fs.realpath(target);
    } catch {
      realDir = path.resolve(target);
    }
    if (visitedRealDirs.has(realDir)) return;
    visitedRealDirs.add(realDir);

    let entries;
    try {
      entries = await fs.readdir(target, { withFileTypes: true });
    } catch {
      return;
    }

    const resolvedEntries: Array<{
      name: string;
      fullPath: string;
      isDir: boolean;
      isFile: boolean;
    }> = [];
    for (const entry of entries) {
      const fullPath = path.join(target, entry.name);
      if (entry.isDirectory()) {
        resolvedEntries.push({
          name: entry.name,
          fullPath,
          isDir: true,
          isFile: false,
        });
      } else if (entry.isFile()) {
        resolvedEntries.push({
          name: entry.name,
          fullPath,
          isDir: false,
          isFile: true,
        });
      } else if (entry.isSymbolicLink()) {
        try {
          const linkStat = await fs.stat(fullPath);
          resolvedEntries.push({
            name: entry.name,
            fullPath,
            isDir: linkStat.isDirectory(),
            isFile: linkStat.isFile(),
          });
        } catch {
          // Ignore broken symlinks
        }
      }
    }

    const dirNames = new Set(
      resolvedEntries.filter((e) => e.isDir).map((e) => e.name),
    );
    const fileNames = new Set(
      resolvedEntries.filter((e) => e.isFile).map((e) => e.name),
    );

    const isSystemGeneratedDir = path.basename(target) === ".system_generated";
    const hasSystemGeneratedChild = dirNames.has(".system_generated");
    const brainContext =
      inBrainLayout ||
      isSystemGeneratedDir ||
      hasSystemGeneratedChild ||
      isBrainPath(target);

    // Prefer transcript_full.jsonl over transcript.jsonl if both exist in same dir,
    // while still recursing into nested subdirectories.
    const hasFull = fileNames.has("transcript_full.jsonl");
    const hasBasic = fileNames.has("transcript.jsonl");
    if (hasFull) {
      await recordFile(path.join(target, "transcript_full.jsonl"));
    } else if (hasBasic) {
      await recordFile(path.join(target, "transcript.jsonl"));
    }

    for (const entry of resolvedEntries) {
      if (entry.isDir) {
        if (ALWAYS_SKIP_DIRS.has(entry.name)) continue;
        if (brainContext && BRAIN_SKIP_DIRS.has(entry.name)) continue;
        // Inside an Antigravity session dir (<id>/), transcripts live under .system_generated
        if (hasSystemGeneratedChild && entry.name !== ".system_generated") {
          continue;
        }
        if (isSystemGeneratedDir && SYSTEM_GENERATED_SKIP_DIRS.has(entry.name)) {
          continue;
        }
        await walk(entry.fullPath, depth + 1, brainContext);
      } else if (
        entry.isFile &&
        entry.name.endsWith(".jsonl") &&
        entry.name !== "transcript_full.jsonl" &&
        entry.name !== "transcript.jsonl" &&
        !brainContext &&
        !hasFull &&
        !hasBasic
      ) {
        await recordFile(entry.fullPath);
      }
    }
  }

  for (const inp of inputs) {
    await walk(path.resolve(inp));
  }

  return found.sort();
}

async function sortFilesByRecentMtime(files: string[]): Promise<string[]> {
  const withMtime: Array<{ file: string; mtimeMs: number }> = [];
  const batchSize = 64;
  for (let i = 0; i < files.length; i += batchSize) {
    const slice = files.slice(i, i + batchSize);
    const results = await Promise.all(
      slice.map(async (file) => {
        try {
          const stat = await fs.stat(file);
          return { file, mtimeMs: stat.mtimeMs };
        } catch {
          return null;
        }
      }),
    );
    for (const item of results) {
      if (item !== null) withMtime.push(item);
    }
  }
  withMtime.sort(
    (a, b) =>
      b.mtimeMs - a.mtimeMs ||
      (a.file < b.file ? -1 : a.file > b.file ? 1 : 0),
  );
  return withMtime.map((x) => x.file);
}

async function loadFilteredSessions(
  inputs: string[],
  options: AnalyzeOptions = {},
): Promise<TranscriptStep[][]> {
  const { recent, sinceMs, untilMs } = options;
  if (recent !== undefined && (!Number.isSafeInteger(recent) || recent < 1)) {
    throw new RangeError("recent must be a positive integer");
  }
  if (sinceMs !== undefined && !Number.isFinite(sinceMs)) {
    throw new RangeError("sinceMs must be a valid finite timestamp");
  }
  if (untilMs !== undefined && !Number.isFinite(untilMs)) {
    throw new RangeError("untilMs must be a valid finite timestamp");
  }
  if (sinceMs !== undefined && untilMs !== undefined && sinceMs > untilMs) {
    throw new RangeError("sinceMs must be less than or equal to untilMs");
  }

  const files = await collectTranscriptFiles(inputs);
  const orderedFiles =
    recent !== undefined ? await sortFilesByRecentMtime(files) : files;

  const sessions: TranscriptStep[][] = [];
  for (const file of orderedFiles) {
    try {
      const content = await fs.readFile(file, "utf8");
      const rawSteps = parseTranscriptJsonl(content);
      const steps = filterStepsByTime(rawSteps, sinceMs, untilMs);
      if (!steps.some((s) => s.type === "PLANNER_RESPONSE")) continue;
      if (steps !== rawSteps) {
        fullSessionStepsMap.set(steps, rawSteps);
      }
      sessions.push(steps);
      if (recent !== undefined && sessions.length >= recent) break;
    } catch {
      // Skip unreadable file
    }
  }
  return sessions;
}

export async function analyzePaths(
  inputs: string[],
  options: AnalyzeOptions = {},
): Promise<BenchmarkMetrics> {
  const sessions = await loadFilteredSessions(inputs, options);
  return analyzeTranscriptSessions(sessions);
}

export async function analyzePathsByUsage(
  inputs: string[],
  options: AnalyzeOptions = {},
): Promise<UsageSplitMetrics> {
  const sessions = await loadFilteredSessions(inputs, options);
  const withFastSessions: TranscriptStep[][] = [];
  const withoutFastSessions: TranscriptStep[][] = [];
  for (const s of sessions) {
    if (sessionUsesAgyFast(s)) {
      withFastSessions.push(s);
    } else {
      withoutFastSessions.push(s);
    }
  }
  return {
    overall: analyzeTranscriptSessions(sessions),
    withAgyFast: analyzeTranscriptSessions(withFastSessions),
    withoutAgyFast: analyzeTranscriptSessions(withoutFastSessions),
  };
}

export function formatMetricsMarkdown(m: BenchmarkMetrics): string {
  const fastTotal = Object.values(m.agyFastToolCalls).reduce((a, b) => a + b, 0);
  const editRedundancy =
    m.editFileCalls > 0
      ? pct(m.redundantSameTurnEditFileCalls / m.editFileCalls)
      : "0.0%";
  const readonlyShellRatio =
    m.runCommandCalls > 0
      ? pct(m.readonlySearchRunCommandCalls / m.runCommandCalls)
      : "0.0%";

  return [
    "# agy-fast Transcript Benchmark",
    "",
    "| Metric | Value |",
    "| :--- | ---: |",
    `| Sessions | ${m.sessions} |`,
    `| Sessions using agy-fast | ${m.sessionsWithAgyFast}/${m.sessions} (snapshot: ${m.sessionsWithSnapshot}) |`,
    `| LLM turns | ${m.llmTurns} |`,
    `| Tool calls | ${m.totalToolCalls} |`,
    `| Active time (s) | ${m.activeTimeSec.toFixed(1)} |`,
    `| LLM wait & decode (s) | ${m.llmTimeSec.toFixed(1)} |`,
    `| Multi-tool stream buffer wait (s) | ${m.streamWaitSec.toFixed(1)} |`,
    `| Tool execution time (s) | ${m.toolExecTimeSec.toFixed(1)} |`,
    `| Effective throughput (tok/s) | ${m.effectiveTps.toFixed(1)} |`,
    `| Single-tool turn ratio | ${pct(m.singleToolTurnRatio)} |`,
    `| Consecutive same-tool extra turns | ${m.consecutiveSingleChainsExtraTurns} |`,
    `| Redundant same-turn edit_file ratio | ${editRedundancy} (${m.redundantSameTurnEditFileCalls}/${m.editFileCalls}) |`,
    `| Read-only search shell ratio | ${readonlyShellRatio} (${m.readonlySearchRunCommandCalls}/${m.runCommandCalls}) |`,
    `| Average context size (tokens) | ${m.avgContextTokens.toLocaleString("en-US")} |`,
    `| Turns >= 150k context | ${pct(m.over150kContextRatio)} |`,
    `| Average Prompt Cache hit rate | ${pct(m.avgCacheHitRate)} |`,
    `| Turn gaps > 60s | ${pct(m.over60sGapRatio)} |`,
    `| agy-fast tool calls | ${fastTotal} (search:${m.agyFastToolCalls.batch_search}, edit:${m.agyFastToolCalls.apply_edits}, slice:${m.agyFastToolCalls.read_slices}, check:${m.agyFastToolCalls.run_checks}, graph:${m.agyFastToolCalls.code_graph ?? 0}) |`,
    `| agy-fast health (spills / truncs / errs) | ${m.mcpSpillCalls} / ${m.agyTruncatedCalls} / ${m.agyErrorCalls} |`,
    ...(m.agyCorruptedArgCalls > 0
      ? [
          `| agy-fast corrupted-arg calls | ${m.agyCorruptedArgCalls} (wasted ${m.agyCorruptedArgWastedSec.toFixed(1)}s, ${m.agyCorruptedArgWastedOutTokens.toLocaleString("en-US")} out tok) |`,
        ]
      : []),
    ...(m.swarmWorkerSessions > 0
      ? [
          `| Swarm worker coverage | ${m.swarmWorkerSessionsWithAgyFast}/${m.swarmWorkerSessions} using agy-fast |`,
        ]
      : []),
    `| Parallel batching leaks (multi-MCP / multi-run_command turns) | ${m.multiMcpTurns} / ${m.multiRunCommandTurns} |`,
    `| Multimodal >=180k turns (media loaded) | ${m.multimodalHighCtxTurns} (${m.mediaLoadedCount} media) |`,
  ].join("\n");
}

function formatDelta(before: number, after: number): string {
  if (before === 0) return after === 0 ? "0.0%" : "n/a";
  const d = ((after - before) / before) * 100;
  return `${d >= 0 ? "+" : ""}${d.toFixed(1)}%`;
}

export function formatCompareMarkdown(
  before: BenchmarkMetrics,
  after: BenchmarkMetrics,
): string {
  return [
    "# agy-fast A/B Comparison",
    "",
    "| Metric | Before | After | Change |",
    "| :--- | ---: | ---: | ---: |",
    `| Sessions | ${before.sessions} | ${after.sessions} | - |`,
    `| LLM turns | ${before.llmTurns} | ${after.llmTurns} | ${formatDelta(before.llmTurns, after.llmTurns)} |`,
    `| Tool calls | ${before.totalToolCalls} | ${after.totalToolCalls} | ${formatDelta(before.totalToolCalls, after.totalToolCalls)} |`,
    `| Active time (s) | ${before.activeTimeSec.toFixed(1)} | ${after.activeTimeSec.toFixed(1)} | ${formatDelta(before.activeTimeSec, after.activeTimeSec)} |`,
    `| Tool execution time (s) | ${before.toolExecTimeSec.toFixed(1)} | ${after.toolExecTimeSec.toFixed(1)} | ${formatDelta(before.toolExecTimeSec, after.toolExecTimeSec)} |`,
    `| Effective throughput (tok/s) | ${before.effectiveTps.toFixed(1)} | ${after.effectiveTps.toFixed(1)} | ${formatDelta(before.effectiveTps, after.effectiveTps)} |`,
    `| Single-tool turn ratio | ${pct(before.singleToolTurnRatio)} | ${pct(after.singleToolTurnRatio)} | ${formatDelta(before.singleToolTurnRatio, after.singleToolTurnRatio)} |`,
    `| Average context (tokens) | ${before.avgContextTokens} | ${after.avgContextTokens} | ${formatDelta(before.avgContextTokens, after.avgContextTokens)} |`,
    `| Prompt Cache hit rate | ${pct(before.avgCacheHitRate)} | ${pct(after.avgCacheHitRate)} | ${formatDelta(before.avgCacheHitRate, after.avgCacheHitRate)} |`,
    `| Turn gaps > 60s | ${pct(before.over60sGapRatio)} | ${pct(after.over60sGapRatio)} | ${formatDelta(before.over60sGapRatio, after.over60sGapRatio)} |`,
    `| agy-fast MCP spills (output.txt) | ${before.mcpSpillCalls} | ${after.mcpSpillCalls} | ${formatDelta(before.mcpSpillCalls, after.mcpSpillCalls)} |`,
    `| agy-fast internal truncations | ${before.agyTruncatedCalls} | ${after.agyTruncatedCalls} | ${formatDelta(before.agyTruncatedCalls, after.agyTruncatedCalls)} |`,
    `| Multimodal >=180k turns | ${before.multimodalHighCtxTurns} | ${after.multimodalHighCtxTurns} | ${formatDelta(before.multimodalHighCtxTurns, after.multimodalHighCtxTurns)} |`,
  ].join("\n");
}

export function formatUsageSplitMarkdown(split: UsageSplitMetrics): string {
  const { overall, withoutAgyFast: wo, withAgyFast: wf } = split;
  const editRed = (m: BenchmarkMetrics): number =>
    m.editFileCalls > 0 ? m.redundantSameTurnEditFileCalls / m.editFileCalls : 0;
  const roShell = (m: BenchmarkMetrics): number =>
    m.runCommandCalls > 0 ? m.readonlySearchRunCommandCalls / m.runCommandCalls : 0;
  const fastSum = (m: BenchmarkMetrics): number =>
    Object.values(m.agyFastToolCalls).reduce((a, b) => a + b, 0);

  return [
    "# agy-fast Usage Split Benchmark",
    "",
    "| Metric | Overall | Without agy-fast | With agy-fast | With vs Without |",
    "| :--- | ---: | ---: | ---: | ---: |",
    `| Sessions | ${overall.sessions} | ${wo.sessions} | ${wf.sessions} | - |`,
    `| Sessions with snapshot | ${overall.sessionsWithSnapshot} | ${wo.sessionsWithSnapshot} | ${wf.sessionsWithSnapshot} | - |`,
    `| LLM turns | ${overall.llmTurns} | ${wo.llmTurns} | ${wf.llmTurns} | - |`,
    `| Tool calls | ${overall.totalToolCalls} | ${wo.totalToolCalls} | ${wf.totalToolCalls} | - |`,
    `| Active time (s) | ${overall.activeTimeSec.toFixed(1)} | ${wo.activeTimeSec.toFixed(1)} | ${wf.activeTimeSec.toFixed(1)} | - |`,
    `| Multi-tool stream wait (s) | ${overall.streamWaitSec.toFixed(1)} | ${wo.streamWaitSec.toFixed(1)} | ${wf.streamWaitSec.toFixed(1)} | - |`,
    `| Effective throughput (tok/s) | ${overall.effectiveTps.toFixed(1)} | ${wo.effectiveTps.toFixed(1)} | ${wf.effectiveTps.toFixed(1)} | ${formatDelta(wo.effectiveTps, wf.effectiveTps)} |`,
    `| Single-tool turn ratio | ${pct(overall.singleToolTurnRatio)} | ${pct(wo.singleToolTurnRatio)} | ${pct(wf.singleToolTurnRatio)} | ${formatDelta(wo.singleToolTurnRatio, wf.singleToolTurnRatio)} |`,
    `| Redundant same-turn edit_file ratio | ${pct(editRed(overall))} | ${pct(editRed(wo))} | ${pct(editRed(wf))} | ${formatDelta(editRed(wo), editRed(wf))} |`,
    `| Read-only search shell ratio | ${pct(roShell(overall))} | ${pct(roShell(wo))} | ${pct(roShell(wf))} | ${formatDelta(roShell(wo), roShell(wf))} |`,
    `| Average context (tokens) | ${overall.avgContextTokens.toLocaleString("en-US")} | ${wo.avgContextTokens.toLocaleString("en-US")} | ${wf.avgContextTokens.toLocaleString("en-US")} | ${formatDelta(wo.avgContextTokens, wf.avgContextTokens)} |`,
    `| Prompt Cache hit rate | ${pct(overall.avgCacheHitRate)} | ${pct(wo.avgCacheHitRate)} | ${pct(wf.avgCacheHitRate)} | ${formatDelta(wo.avgCacheHitRate, wf.avgCacheHitRate)} |`,
    `| Turn gaps > 60s | ${pct(overall.over60sGapRatio)} | ${pct(wo.over60sGapRatio)} | ${pct(wf.over60sGapRatio)} | ${formatDelta(wo.over60sGapRatio, wf.over60sGapRatio)} |`,
    `| agy-fast tool calls | ${fastSum(overall)} | 0 | ${fastSum(wf)} (search:${wf.agyFastToolCalls.batch_search}, edit:${wf.agyFastToolCalls.apply_edits}, slice:${wf.agyFastToolCalls.read_slices}, check:${wf.agyFastToolCalls.run_checks}) | - |`,
    `| agy-fast health (spills / truncs / errs) | ${overall.mcpSpillCalls} / ${overall.agyTruncatedCalls} / ${overall.agyErrorCalls} | 0 / 0 / 0 | ${wf.mcpSpillCalls} / ${wf.agyTruncatedCalls} / ${wf.agyErrorCalls} | - |`,
    ...(overall.agyCorruptedArgCalls > 0
      ? [
          `| agy-fast corrupted-arg calls | ${overall.agyCorruptedArgCalls} (${overall.agyCorruptedArgWastedSec.toFixed(1)}s) | 0 | ${wf.agyCorruptedArgCalls} (${wf.agyCorruptedArgWastedSec.toFixed(1)}s) | - |`,
        ]
      : []),
    ...(overall.swarmWorkerSessions > 0
      ? [
          `| Swarm worker sessions | ${overall.swarmWorkerSessions} | ${wo.swarmWorkerSessions} | ${wf.swarmWorkerSessions} | - |`,
        ]
      : []),
    `| Parallel leaks (multi-MCP / multi-run_command) | ${overall.multiMcpTurns} / ${overall.multiRunCommandTurns} | ${wo.multiMcpTurns} / ${wo.multiRunCommandTurns} | ${wf.multiMcpTurns} / ${wf.multiRunCommandTurns} | - |`,
    `| Multimodal >=180k turns (media) | ${overall.multimodalHighCtxTurns} (${overall.mediaLoadedCount}) | ${wo.multimodalHighCtxTurns} (${wo.mediaLoadedCount}) | ${wf.multimodalHighCtxTurns} (${wf.mediaLoadedCount}) | - |`,
  ].join("\n");
}
