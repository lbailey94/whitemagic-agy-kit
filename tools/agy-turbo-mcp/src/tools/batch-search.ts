import { promises as fs } from "node:fs";
import path from "node:path";
import {
  formatCorruptedArrayError,
  isPlainObject,
  parseMaybeJsonArray,
  type CorruptedItem,
} from "../utils/arg-validation.js";
import { execBinaryAsync, isRipgrepAvailable, type ExecResult } from "../utils/exec-utils.js";
import { rankFuzzyPaths } from "../utils/fuzzy-match.js";
import {
  globToRegExp,
  isLikelyBinary,
  resolvePath,
  toPosixPath,
  walkFiles,
} from "../utils/fs-utils.js";
import {
  DEFAULT_INLINE_MAX_BYTES,
  fitSectionsToByteBudget,
  sliceSafeChars,
} from "../utils/output-filter.js";

export type SearchQueryType = "text" | "regex" | "glob" | "fuzzy" | "git";

export interface SearchQuery {
  type: SearchQueryType;
  pattern: string;
  path?: string;
  glob?: string;
  fileType?: string;
  multiline?: boolean;
  outputMode?: "content" | "files_with_matches" | "count";
  sortBy?: "path" | "mtime";
  caseSensitive?: boolean;
  contextLines?: number;
  maxResults?: number;
}

export interface BatchSearchInput {
  cwd?: string;
  queries: SearchQuery[];
  maxChars?: number;
}

export interface BatchSearchRuntimeOptions {
  forceNodeFallback?: boolean;
}

const READONLY_GIT_SUBCOMMANDS = new Set([
  "status",
  "diff",
  "log",
  "show",
  "branch",
  "rev-parse",
  "ls-files",
  "blame",
  "shortlog",
  "describe",
  "tag",
  "remote",
]);

const FORBIDDEN_GIT_GLOBAL_FLAGS = new Set([
  "-c",
  "--config",
  "--config-env",
  "--exec-path",
  "--ext-diff",
  "--textconv",
  "--output",
  "--exec",
  "--upload-pack",
  "--git-dir",
  "--work-tree",
]);

const ALLOWED_BRANCH_BOOL_FLAGS = new Set([
  "--all",
  "--remotes",
  "--verbose",
  "--list",
  "--show-current",
  "--no-abbrev",
  "--no-color",
  "--ignore-case",
  "--omit-empty",
  "--no-column",
  "--quiet",
]);

const ALLOWED_BRANCH_EQ_ONLY_OPTIONAL_FLAGS = new Set([
  "--abbrev",
  "--color",
  "--column",
]);

const ALLOWED_BRANCH_COMMIT_OPTIONAL_FLAGS = new Set([
  "--merged",
  "--no-merged",
]);

const ALLOWED_BRANCH_COMMIT_VALUE_FLAGS = new Set([
  "--contains",
  "--no-contains",
  "--points-at",
]);

const ALLOWED_BRANCH_VALUE_FLAGS = new Set([
  "--contains",
  "--no-contains",
  "--points-at",
  "--sort",
  "--format",
]);

const ALLOWED_BRANCH_SHORT_CHARS = new Set(["a", "r", "v", "l", "q", "i"]);

const ALLOWED_TAG_BOOL_FLAGS = new Set([
  "--list",
  "--no-color",
  "--ignore-case",
  "--omit-empty",
  "--no-column",
]);

const ALLOWED_TAG_EQ_ONLY_OPTIONAL_FLAGS = new Set([
  "--color",
  "--column",
]);

const ALLOWED_TAG_COMMIT_OPTIONAL_FLAGS = new Set([
  "--merged",
  "--no-merged",
]);

const ALLOWED_TAG_COMMIT_VALUE_FLAGS = new Set([
  "--contains",
  "--no-contains",
  "--points-at",
]);

const ALLOWED_TAG_VALUE_FLAGS = new Set([
  "--contains",
  "--no-contains",
  "--points-at",
  "--sort",
  "--format",
]);

const ALLOWED_TAG_SHORT_CHARS = new Set(["l", "i"]);

const ALLOWED_REMOTE_SUBCOMMANDS = new Set(["show", "get-url"]);

export function tokenizeArgs(input: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let quote: "'" | '"' | null = null;
  let escape = false;

  for (let i = 0; i < input.length; i++) {
    const ch = input[i]!;
    if (escape) {
      current += ch;
      escape = false;
      continue;
    }
    if (ch === "\\" && quote !== "'") {
      escape = true;
      continue;
    }
    if (quote) {
      if (ch === quote) {
        quote = null;
      } else {
        current += ch;
      }
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      continue;
    }
    if (/\s/.test(ch)) {
      if (current.length > 0) {
        tokens.push(current);
        current = "";
      }
      continue;
    }
    current += ch;
  }

  if (quote) {
    throw new Error("Unterminated quote in git query");
  }
  if (current.length > 0) {
    tokens.push(current);
  }
  return tokens;
}

export function validateReadonlyGitArgs(pattern: string): string[] {
  const rawTokens = tokenizeArgs(pattern.trim());
  const tokens = rawTokens[0] === "git" ? rawTokens.slice(1) : rawTokens;
  if (tokens.length === 0) {
    throw new Error("Empty git query. Example: 'status --short' or 'log -n 5 --oneline'");
  }

  const subcmd = tokens[0]!;
  if (subcmd.startsWith("-")) {
    throw new Error(
      `Git query must start with a read-only subcommand (got '${subcmd}')`,
    );
  }
  if (!READONLY_GIT_SUBCOMMANDS.has(subcmd)) {
    throw new Error(
      `Git subcommand '${subcmd}' is not allowed in batch_search (read-only only: ${[...READONLY_GIT_SUBCOMMANDS].join(", ")})`,
    );
  }

  const rest = tokens.slice(1);
  for (const arg of rest) {
    if (arg === "&&" || arg === "||" || arg === ";" || arg === "|") {
      throw new Error(
        `Shell operator '${arg}' is not supported in git queries; split chained commands into separate batch_search queries`,
      );
    }
    const flagName = arg.includes("=") ? arg.slice(0, arg.indexOf("=")) : arg;
    if (
      FORBIDDEN_GIT_GLOBAL_FLAGS.has(flagName) ||
      arg.startsWith("--output=") ||
      flagName === "--ext" ||
      flagName.startsWith("--ext-") ||
      flagName.startsWith("--textc") ||
      (arg.startsWith("-c") && arg.length > 2)
    ) {
      throw new Error(`Flag '${arg}' is not allowed in read-only git query`);
    }
  }

  if (subcmd === "branch") {
    let hasListFlag = false;
    let expectValue = false;
    let expectNonFlagCommit = false;
    for (const arg of rest) {
      if (expectValue) {
        expectValue = false;
        continue;
      }
      if (expectNonFlagCommit) {
        expectNonFlagCommit = false;
        if (arg.startsWith("-")) {
          throw new Error(
            `Expected commit ref after commit filter flag (got '${arg}')`,
          );
        }
        continue;
      }
      if (arg === "--") {
        if (!hasListFlag) {
          throw new Error(
            "Positional branch arguments are only allowed with --list in read-only mode",
          );
        }
        break;
      }
      if (arg.startsWith("--")) {
        const eqIdx = arg.indexOf("=");
        const flagName = eqIdx !== -1 ? arg.slice(0, eqIdx) : arg;
        if (eqIdx !== -1) {
          if (
            !ALLOWED_BRANCH_VALUE_FLAGS.has(flagName) &&
            !ALLOWED_BRANCH_EQ_ONLY_OPTIONAL_FLAGS.has(flagName) &&
            !ALLOWED_BRANCH_COMMIT_OPTIONAL_FLAGS.has(flagName)
          ) {
            throw new Error(`Flag '${arg}' is not allowed in read-only git branch query`);
          }
        } else if (ALLOWED_BRANCH_COMMIT_VALUE_FLAGS.has(flagName)) {
          expectNonFlagCommit = true;
          hasListFlag = true;
        } else if (ALLOWED_BRANCH_COMMIT_OPTIONAL_FLAGS.has(flagName)) {
          expectNonFlagCommit = true;
          hasListFlag = true;
        } else if (ALLOWED_BRANCH_VALUE_FLAGS.has(flagName)) {
          expectValue = true;
        } else if (
          !ALLOWED_BRANCH_BOOL_FLAGS.has(flagName) &&
          !ALLOWED_BRANCH_EQ_ONLY_OPTIONAL_FLAGS.has(flagName)
        ) {
          throw new Error(`Flag '${arg}' is not allowed in read-only git branch query`);
        }
        if (
          flagName === "--list" ||
          ALLOWED_BRANCH_COMMIT_OPTIONAL_FLAGS.has(flagName) ||
          ALLOWED_BRANCH_COMMIT_VALUE_FLAGS.has(flagName)
        ) {
          hasListFlag = true;
        }
      } else if (arg.startsWith("-") && arg.length > 1) {
        for (const ch of arg.slice(1)) {
          if (!ALLOWED_BRANCH_SHORT_CHARS.has(ch)) {
            throw new Error(`Flag '${arg}' is not allowed in read-only git branch query`);
          }
          if (ch === "l") hasListFlag = true;
        }
      } else if (!hasListFlag) {
        throw new Error(
          `Positional branch argument '${arg}' is only allowed with --list in read-only mode`,
        );
      }
    }
  } else if (subcmd === "tag") {
    let allowsPositionals = false;
    let expectValue = false;
    let expectNonFlagCommit = false;
    for (const arg of rest) {
      if (expectValue) {
        expectValue = false;
        continue;
      }
      if (expectNonFlagCommit) {
        expectNonFlagCommit = false;
        if (arg.startsWith("-")) {
          throw new Error(
            `Expected commit ref after commit filter flag (got '${arg}')`,
          );
        }
        continue;
      }
      if (arg === "--") {
        if (!allowsPositionals) {
          throw new Error(
            "Positional tag arguments are only allowed with --list in read-only mode",
          );
        }
        break;
      }
      if (arg.startsWith("--")) {
        const eqIdx = arg.indexOf("=");
        const flagName = eqIdx !== -1 ? arg.slice(0, eqIdx) : arg;
        if (eqIdx !== -1) {
          if (
            !ALLOWED_TAG_VALUE_FLAGS.has(flagName) &&
            !ALLOWED_TAG_EQ_ONLY_OPTIONAL_FLAGS.has(flagName) &&
            !ALLOWED_TAG_COMMIT_OPTIONAL_FLAGS.has(flagName)
          ) {
            throw new Error(`Flag '${arg}' is not allowed in read-only git tag query`);
          }
        } else if (ALLOWED_TAG_COMMIT_VALUE_FLAGS.has(flagName)) {
          expectNonFlagCommit = true;
          allowsPositionals = true;
        } else if (ALLOWED_TAG_COMMIT_OPTIONAL_FLAGS.has(flagName)) {
          expectNonFlagCommit = true;
          allowsPositionals = true;
        } else if (ALLOWED_TAG_VALUE_FLAGS.has(flagName)) {
          expectValue = true;
        } else if (
          !ALLOWED_TAG_BOOL_FLAGS.has(flagName) &&
          !ALLOWED_TAG_EQ_ONLY_OPTIONAL_FLAGS.has(flagName)
        ) {
          throw new Error(`Flag '${arg}' is not allowed in read-only git tag query`);
        }
        if (
          flagName === "--list" ||
          ALLOWED_TAG_COMMIT_OPTIONAL_FLAGS.has(flagName) ||
          ALLOWED_TAG_COMMIT_VALUE_FLAGS.has(flagName)
        ) {
          allowsPositionals = true;
        }
      } else if (arg.startsWith("-") && arg.length > 1) {
        if (/^-n\d*$/.test(arg)) {
          allowsPositionals = true;
        } else {
          for (const ch of arg.slice(1)) {
            if (!ALLOWED_TAG_SHORT_CHARS.has(ch)) {
              throw new Error(`Flag '${arg}' is not allowed in read-only git tag query`);
            }
            if (ch === "l") allowsPositionals = true;
          }
        }
      } else if (!allowsPositionals) {
        throw new Error(
          `Positional tag argument '${arg}' is only allowed with --list in read-only mode`,
        );
      }
    }
  } else if (subcmd === "remote") {
    let remoteSubcmd: string | null = null;
    for (const arg of rest) {
      if (!remoteSubcmd) {
        if (arg === "-v" || arg === "--verbose") continue;
        if (arg.startsWith("-")) {
          throw new Error(`Flag '${arg}' is not allowed in read-only git remote query`);
        }
        if (!ALLOWED_REMOTE_SUBCOMMANDS.has(arg)) {
          throw new Error(`Mutating git remote subcommand '${arg}' is not allowed`);
        }
        remoteSubcmd = arg;
      } else if (arg.startsWith("-")) {
        const okFlag =
          (remoteSubcmd === "show" && arg === "-n") ||
          (remoteSubcmd === "get-url" && (arg === "--push" || arg === "--all"));
        if (!okFlag) {
          throw new Error(
            `Flag '${arg}' is not allowed for read-only git remote ${remoteSubcmd}`,
          );
        }
      } else if (
        arg.includes("://") ||
        arg.includes(":") ||
        arg.includes("/") ||
        arg.includes("\\")
      ) {
        throw new Error(
          `Remote target '${arg}' must be a configured remote name, not a URL or path`,
        );
      }
    }
  }

  return ["--no-pager", subcmd, ...rest];
}

const FILE_TYPE_GLOBS: Record<string, string[]> = {
  ts: ["*.ts", "*.tsx", "*.mts", "*.cts"],
  typescript: ["*.ts", "*.tsx", "*.mts", "*.cts"],
  js: ["*.js", "*.jsx", "*.mjs", "*.cjs"],
  javascript: ["*.js", "*.jsx", "*.mjs", "*.cjs"],
  py: ["*.py", "*.pyi"],
  python: ["*.py", "*.pyi"],
  go: ["*.go"],
  gd: ["*.gd"],
  gdscript: ["*.gd"],
  rust: ["*.rs"],
  rs: ["*.rs"],
  java: ["*.java"],
  kt: ["*.kt", "*.kts"],
  kotlin: ["*.kt", "*.kts"],
  c: ["*.c", "*.h"],
  cpp: ["*.cc", "*.cpp", "*.cxx", "*.hpp", "*.hh", "*.hxx", "*.h"],
  cs: ["*.cs"],
  csharp: ["*.cs"],
  swift: ["*.swift"],
  rb: ["*.rb"],
  ruby: ["*.rb"],
  php: ["*.php"],
  css: ["*.css", "*.scss", "*.sass", "*.less"],
  html: ["*.html", "*.htm"],
  vue: ["*.vue"],
  svelte: ["*.svelte"],
  md: ["*.md", "*.mdx", "*.markdown"],
  markdown: ["*.md", "*.mdx", "*.markdown"],
  json: ["*.json", "*.jsonc"],
  yaml: ["*.yml", "*.yaml"],
  yml: ["*.yml", "*.yaml"],
  toml: ["*.toml"],
  xml: ["*.xml"],
  sql: ["*.sql"],
  sh: ["*.sh", "*.bash", "*.zsh"],
  shell: ["*.sh", "*.bash", "*.zsh"],
};

function resolveFileTypeGlobs(fileType?: string): string[] | null {
  if (fileType === undefined) return null;
  if (typeof fileType !== "string") throw new Error("fileType must be a string");
  const key = fileType.trim().toLowerCase().replace(/^\./, "");
  if (!key) return null;
  const mapped = FILE_TYPE_GLOBS[key];
  if (Object.hasOwn(FILE_TYPE_GLOBS, key)) return mapped!;
  if (/^[a-z0-9]+$/i.test(key)) return [`*.${key}`];
  throw new Error(`Unsupported fileType '${fileType}'`);
}

function fileTypeMatcher(fileType?: string): (file: string) => boolean {
  const matchers = (resolveFileTypeGlobs(fileType) ?? []).map(globToRegExp);
  return (file) => matchers.length === 0 || matchers.some((matcher) => matcher.test(path.basename(file)));
}

interface SearchLine {
  lineNumber: number;
  text: string;
  isMatch: boolean;
}

interface FileMatches {
  file: string;
  lines: SearchLine[];
  count?: number;
}

async function runTextOrRegexWithRg(
  query: SearchQuery,
  searchRoot: string,
  baseCwd: string,
): Promise<{ files: FileMatches[]; totalMatches: number; truncated: boolean }> {
  const maxResults = clampInt(query.maxResults ?? 40, 1, 200);
  const contextLines = clampInt(query.contextLines ?? 0, 0, 5);
  const summary = query.outputMode === "count" || query.outputMode === "files_with_matches";
  const typeGlobs = resolveFileTypeGlobs(query.fileType);
  const matchesType = fileTypeMatcher(query.fileType);
  const fileStat = await fs.stat(searchRoot);
  const isFileTarget = fileStat.isFile();
  if (isFileTarget) {
    if (typeGlobs && !matchesType(searchRoot)) {
      return { files: [], totalMatches: 0, truncated: false };
    }
    if (fileStat.size > 2 * 1024 * 1024) {
      return { files: [], totalMatches: 0, truncated: false };
    }
    if (query.glob) {
      const negated = query.glob.startsWith("!");
      const rawGlob = negated ? query.glob.slice(1) : query.glob;
      const globRe = globToRegExp(rawGlob);
      const relPosix = relativeFile(baseCwd, searchRoot);
      const matched = globRe.test(relPosix) || globRe.test(path.basename(searchRoot));
      if (negated ? matched : !matched) {
        return { files: [], totalMatches: 0, truncated: false };
      }
    }
    const fh = await fs.open(searchRoot, "r");
    try {
      const head = Buffer.alloc(Math.min(fileStat.size, 8192));
      const { bytesRead } = await fh.read(head, 0, head.length, 0);
      if (isLikelyBinary(head.subarray(0, bytesRead))) {
        return { files: [], totalMatches: 0, truncated: false };
      }
    } finally {
      await fh.close();
    }
  }
  const perFileCap = isFileTarget ? maxResults : Math.min(maxResults, 50);

  const args = ["--no-config", "--no-messages", "--max-filesize", "2M", "--crlf"];
  if (query.multiline) args.push("--multiline", "--multiline-dotall");
  if (typeGlobs) {
    for (const glob of typeGlobs) args.push("--type-add", `agyfilter:${glob}`);
    args.push("--type", "agyfilter");
  }

  if (query.type === "text") {
    args.push("--fixed-strings");
  }
  if (query.caseSensitive === false) {
    args.push("--ignore-case");
  }
  if (query.glob) {
    args.push(`--glob=${query.glob}`);
  }
  // Always skip heavy vendor/build noise directories unless explicitly targeted
  const normRoot = toPosixPath(searchRoot);
  for (const dir of [
    "node_modules",
    ".git",
    "dist",
    "build",
    "target",
    "coverage",
    "__pycache__",
    ".venv",
    ".next",
  ]) {
    if (!normRoot.split("/").includes(dir)) {
      args.push(`--glob=!**/${dir}/**`);
    }
  }

  const runRg = async (
    runArgs: string[],
    opts?: { allowPartialError?: boolean },
  ): Promise<ExecResult> => {
    const res = await execBinaryAsync("rg", runArgs, {
      cwd: baseCwd,
      timeoutMs: 15_000,
      maxOutputBytes: 2 * 1024 * 1024,
    });
    if (res.error) {
      throw new Error(`rg spawn error: ${res.error}`);
    }
    // rg exit code 0 = matches found, 1 = no matches, 2 = error (e.g. bad regex or ENOENT).
    // In Pass 2 over explicit files from Pass 1, the regex is already validated; exit code 2
    // with empty stderr (--no-messages) means a listed file vanished mid-search while stdout
    // still carries valid JSON matches for the remaining files.
    if (res.exitCode === 2 && !(opts?.allowPartialError && !res.stderr.trim())) {
      throw new Error(res.stderr.trim() || `rg failed for pattern: ${query.pattern}`);
    }
    return res;
  };
  const cutShort = (res: ExecResult): boolean =>
    res.truncated || res.timedOut || res.exitCode === null;

  if (summary) {
    const res = await runRg([
      ...args,
      ...(query.outputMode === "count"
        ? [query.multiline ? "--count-matches" : "--count", "--with-filename", "--null"]
        : ["--files-with-matches", "--null"]),
      "--",
      query.pattern,
      searchRoot,
    ]);
    const files: FileMatches[] = [];
    const out = res.stdout;
    let pos = 0;
    while (pos < out.length) {
      const nul = out.indexOf("\0", pos);
      if (nul === -1) break;
      const rawFile = out.slice(pos, nul);
      let count = 0;
      if (query.outputMode === "count") {
        const nl = out.indexOf("\n", nul + 1);
        if (nl === -1) break;
        const rawCount = out.slice(nul + 1, nl).trim();
        count = Number(rawCount);
        pos = nl + 1;
      } else {
        pos = nul + 1;
      }
      if (!rawFile || !matchesType(rawFile)) continue;
      files.push({ file: relativeFile(baseCwd, rawFile), lines: [], count: Number.isFinite(count) ? count : 0 });
    }
    files.sort((a, b) => (a.file < b.file ? -1 : a.file > b.file ? 1 : 0));
    return {
      files: files.slice(0, maxResults),
      totalMatches: 0,
      truncated: files.length > maxResults || cutShort(res),
    };
  }

  // Request perFileCap + 1 so hitting the per-file cap still shows as `truncated`.
  const maxCountArgs = query.multiline ? [] : ["--max-count", String(perFileCap + 1)];
  let chosen: Array<{ rawFile: string; relFile: string; count: number }>;
  let truncated = false;

  if (isFileTarget) {
    // Single-file searches have no multi-file thread ordering nondeterminism;
    // skip the counting pass to avoid spawning rg twice.
    chosen = [
      {
        rawFile: searchRoot,
        relFile: relativeFile(baseCwd, searchRoot),
        count: Number.POSITIVE_INFINITY,
      },
    ];
  } else {
    // rg reports files in thread order, so the kept files are chosen from per-file
    // counts in path order, and only those files are searched for lines.
    const counts = await runRg([
      ...args,
      ...maxCountArgs,
      "--count",
      "--with-filename",
      "--null",
      "--",
      query.pattern,
      searchRoot,
    ]);
    const counted: Array<{ rawFile: string; relFile: string; count: number }> = [];
    // The text after the last newline is empty or a record cut by the output budget.
    for (const record of counts.stdout.split("\n").slice(0, -1)) {
      const sep = record.lastIndexOf("\0");
      const count = Number(record.slice(sep + 1));
      if (sep <= 0 || !Number.isInteger(count) || count <= 0) continue;
      const rawFile = record.slice(0, sep);
      if (!matchesType(rawFile)) continue;
      counted.push({ rawFile, relFile: relativeFile(baseCwd, rawFile), count });
    }
    counted.sort((a, b) => (a.relFile < b.relFile ? -1 : a.relFile > b.relFile ? 1 : 0));

    // A multiline count is a lower bound on the matched lines, so the chosen files
    // still cover maxResults.
    chosen = [];
    let coveredMatches = 0;
    for (const item of counted) {
      if (coveredMatches >= maxResults) break;
      chosen.push(item);
      coveredMatches += query.multiline ? item.count : Math.min(item.count, perFileCap);
    }
    truncated = cutShort(counts) || chosen.length < counted.length;
    if (chosen.length === 0) {
      return { files: [], totalMatches: 0, truncated };
    }
  }

  // Pass compact relative paths (when inside baseCwd) and chunk arguments so
  // large maxResults batches stay well below Windows' 32 KB CreateProcess limit.
  const toRgArgPath = (rawFile: string): string => {
    const rel = path.relative(baseCwd, rawFile);
    const chosenPath =
      rel && !rel.startsWith("..") && !path.isAbsolute(rel) ? rel : rawFile;
    // Never pass bare "-" as a path argument (rg interprets "-" as stdin)
    return chosenPath === "-" ? `.${path.sep}-` : chosenPath;
  };
  const rgChunks: string[][] = [];
  let currentChunk: string[] = [];
  let currentChunkChars = 0;
  const maxArgChars = 20_000;
  for (const item of chosen) {
    const argPath = toRgArgPath(item.rawFile);
    const est = argPath.length + 3;
    if (currentChunk.length > 0 && currentChunkChars + est > maxArgChars) {
      rgChunks.push(currentChunk);
      currentChunk = [];
      currentChunkChars = 0;
    }
    currentChunk.push(argPath);
    currentChunkChars += est;
  }
  if (currentChunk.length > 0) rgChunks.push(currentChunk);

  const byFile = new Map<string, Map<number, SearchLine>>();
  for (const chunk of rgChunks) {
    const res = await runRg(
      [
        ...args,
        ...maxCountArgs,
        "--json",
        ...(contextLines > 0 ? ["-C", String(contextLines)] : []),
        "--",
        query.pattern,
        ...chunk,
      ],
      { allowPartialError: !isFileTarget },
    );
    if (cutShort(res)) truncated = true;

    for (const rawLine of res.stdout.split("\n")) {
      if (!rawLine.trim()) continue;
      let ev: {
        type?: string;
        data?: {
          path?: { text?: string; bytes?: string };
          line_number?: number;
          lines?: { text?: string; bytes?: string };
        };
      };
      try {
        ev = JSON.parse(rawLine);
      } catch {
        continue;
      }
      if (ev.type !== "match" && ev.type !== "context") continue;
      const rawFile =
        ev.data?.path?.text ??
        (ev.data?.path?.bytes ? Buffer.from(ev.data.path.bytes, "base64").toString("utf8") : undefined);
      const lineNumber = ev.data?.line_number;
      const rawText =
        ev.data?.lines?.text ??
        (ev.data?.lines?.bytes ? Buffer.from(ev.data.lines.bytes, "base64").toString("utf8") : "");
      if (!rawFile || typeof lineNumber !== "number") continue;

      const relFile = relativeFile(baseCwd, rawFile);
      let lineMap = byFile.get(relFile);
      if (!lineMap) {
        lineMap = new Map<number, SearchLine>();
        byFile.set(relFile, lineMap);
      }
      const isMatch = ev.type === "match";
      const lineText = rawText.replace(/\r?\n$/, "");
      // A multiline match event spans several lines; each one counts as a match.
      const segments = isMatch && query.multiline ? lineText.split(/\r?\n/) : [lineText];
      for (const [offset, text] of segments.entries()) {
        const line = lineNumber + offset;
        if (!lineMap.get(line)?.isMatch) lineMap.set(line, { lineNumber: line, text, isMatch });
      }
    }
  }

  // Keep matches in path and line order up to maxResults. Without multiline, a
  // file keeps at most its counted matches: rg searches an explicitly named
  // binary file past the NUL byte at which the counting pass stopped.
  const files: FileMatches[] = [];
  let totalMatches = 0;
  for (const { relFile, count } of chosen) {
    if (totalMatches >= maxResults) {
      truncated = true;
      break;
    }
    const fileCap = query.multiline ? maxResults : Math.min(count, perFileCap);
    const budget = Math.min(fileCap, maxResults - totalMatches);
    const sorted = [...(byFile.get(relFile)?.values() ?? [])].sort(
      (a, b) => a.lineNumber - b.lineNumber,
    );
    const lines: SearchLine[] = [];
    let kept = 0;
    let lastKeptMatchLine = -1;
    for (const line of sorted) {
      if (line.isMatch) {
        if (kept === budget) {
          truncated = true;
          if (
            lastKeptMatchLine !== -1 &&
            line.lineNumber <= lastKeptMatchLine + contextLines
          ) {
            lines.push({ ...line, isMatch: false });
          }
          continue;
        }
        kept += 1;
        lastKeptMatchLine = line.lineNumber;
        lines.push(line);
      } else if (kept < budget || line.lineNumber <= lastKeptMatchLine + contextLines) {
        // Before the budget is spent, a context line belongs to a kept match;
        // afterwards only the after-context of the last kept match remains.
        lines.push(line);
      }
    }
    if (kept === 0) continue;
    files.push({ file: relFile, lines });
    totalMatches += kept;
  }

  return { files, totalMatches, truncated };
}

function* multilineMatches(content: string, regex: RegExp): Generator<RegExpMatchArray> {
  let previousEnd = -1;
  for (const match of content.matchAll(regex)) {
    const start = match.index!;
    if (start === content.length && (content.length === 0 || content.endsWith("\n"))) break;
    if (match[0] === "") {
      // Match rg's CRLF boundaries and omit empty matches adjacent to the prior match.
      if (start === previousEnd || (content[start - 1] === "\r" && content[start] === "\n")) continue;
    }
    previousEnd = start + match[0].length;
    yield match;
  }
}

function multilineMatchLines(content: string, regex: RegExp, limit: number): number[] {
  const indices: number[] = [];
  let line = 0;
  let nextBreak = content.indexOf("\n");

  // Matches arrive in offset order. Track line boundaries as the matches advance.
  for (const match of multilineMatches(content, regex)) {
    const start = match.index!;
    const end = start + Math.max(0, match[0].length - 1);
    while (nextBreak !== -1 && nextBreak < start) {
      line++;
      nextBreak = content.indexOf("\n", nextBreak + 1);
    }
    while (true) {
      if (indices.at(-1) !== line) indices.push(line);
      if (indices.length >= limit) return indices;
      if (nextBreak === -1 || nextBreak >= end) break;
      line++;
      nextBreak = content.indexOf("\n", nextBreak + 1);
    }
  }
  return indices;
}

async function runTextOrRegexWithNode(
  query: SearchQuery,
  searchRoot: string,
  baseCwd: string,
): Promise<{ files: FileMatches[]; totalMatches: number; truncated: boolean }> {
  const maxResults = clampInt(query.maxResults ?? 40, 1, 200);
  const contextLines = clampInt(query.contextLines ?? 0, 0, 5);
  const flags = (query.caseSensitive === false ? "i" : "") + (query.multiline ? "gmsu" : "");

  let regex: RegExp;
  if (query.type === "regex") {
    try {
      regex = new RegExp(query.pattern, flags);
    } catch (err) {
      // Retry without strict Unicode 'u' mode so ripgrep-valid identity escapes
      // (e.g. `\:` or `\#`) remain compatible in multiline Node fallback.
      if (flags.includes("u")) {
        try {
          regex = new RegExp(query.pattern, flags.replace("u", ""));
        } catch {
          throw new Error(
            `Invalid regex '${query.pattern}': ${err instanceof Error ? err.message : String(err)}`,
          );
        }
      } else {
        throw new Error(
          `Invalid regex '${query.pattern}': ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    }
  } else {
    const escaped = query.pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    regex = new RegExp(escaped, flags);
  }

  const summary = query.outputMode === "count" || query.outputMode === "files_with_matches";
  const matchesType = fileTypeMatcher(query.fileType);
  let discoveryTruncated = false;
  const globNegated = Boolean(query.glob && query.glob.startsWith("!"));
  const rawGlob = query.glob ? (globNegated ? query.glob.slice(1) : query.glob) : null;
  const globRegex = rawGlob ? globToRegExp(rawGlob) : null;
  const globMatchesFile = (relPosix: string): boolean => {
    if (!matchesType(relPosix)) return false;
    if (!globRegex) return true;
    let matched = globRegex.test(relPosix);
    // Also match basename when pattern has no slash (like '*.ts' or '!*.ts')
    if (!matched && !rawGlob!.includes("/")) {
      const base = relPosix.split("/").pop() ?? relPosix;
      matched = globRegex.test(base);
    }
    return globNegated ? !matched : matched;
  };

  const stat = await fs.stat(searchRoot);
  const perFileCap = stat.isFile() ? maxResults : Math.min(maxResults, 50);
  let targetFiles: string[];
  if (stat.isFile()) {
    const relPosix =
      toPosixPath(path.relative(baseCwd, searchRoot)) || path.basename(searchRoot);
    targetFiles = globMatchesFile(relPosix) ? [searchRoot] : [];
  } else {
    const includeHidden = Boolean(
      rawGlob && !globNegated && (rawGlob.startsWith(".") || rawGlob.includes("/.")),
    );
    const walked = await walkFiles(searchRoot, {
      maxFiles: 8000,
      includeHidden,
      fileFilter: globMatchesFile,
    });
    discoveryTruncated = walked.truncated;
    // Subdirectories are walked concurrently; scan in path order like the rg backend.
    targetFiles = walked.files.map((rel) => path.join(searchRoot, rel));
    targetFiles.sort((a, b) => {
      const ra = relativeFile(baseCwd, a);
      const rb = relativeFile(baseCwd, b);
      return ra < rb ? -1 : ra > rb ? 1 : 0;
    });
  }

  const files: FileMatches[] = [];
  let totalMatches = 0;
  let truncated = false;

  const buildFileMatches = (
    relFile: string,
    allLines: string[],
    keptIndices: number[],
  ): FileMatches => {
    const lineMap = new Map<number, SearchLine>();
    for (const mIdx of keptIndices) {
      const from = Math.max(0, mIdx - contextLines);
      const to = Math.min(allLines.length - 1, mIdx + contextLines);
      for (let j = from; j <= to; j++) {
        const ln = j + 1;
        const prev = lineMap.get(ln);
        lineMap.set(ln, {
          lineNumber: ln,
          text: allLines[j]!,
          isMatch: j === mIdx || Boolean(prev?.isMatch),
        });
      }
    }
    return {
      file: relFile,
      lines: [...lineMap.values()].sort((a, b) => a.lineNumber - b.lineNumber),
    };
  };

  const batchSize = 16;
  let anyFileTruncated = false;
  for (let i = 0; i < targetFiles.length && !truncated; i += batchSize) {
    const batch = targetFiles.slice(i, i + batchSize);
    const results = await Promise.all(
      batch.map(async (absFile): Promise<{ relFile: string; lines: string[]; matchedIndices: number[]; count: number; fileTruncated: boolean } | null> => {
        try {
          const st = await fs.stat(absFile);
          if (st.size > 2 * 1024 * 1024) return null;
          const buf = await fs.readFile(absFile);
          if (isLikelyBinary(buf)) return null;
          const content = buf.toString("utf8");
          const lines = content.split(/\r?\n/);
          if (lines.length > 0 && lines.at(-1) === "") lines.pop();
          const relFile = relativeFile(baseCwd, absFile);
          let matchedIndices: number[] = [];
          let count = 0;
          let fileTruncated = false;
          if (query.multiline && summary) {
            for (const _match of multilineMatches(content, regex)) {
              count++;
              if (query.outputMode === "files_with_matches") break;
            }
          } else if (query.multiline) {
            matchedIndices = multilineMatchLines(content, regex, maxResults + 1);
            count = matchedIndices.length;
          } else {
            for (let idx = 0; idx < lines.length; idx++) {
              if (regex.test(lines[idx]!)) {
                if (!summary && matchedIndices.length >= perFileCap) {
                  fileTruncated = true;
                  break;
                }
                matchedIndices.push(idx);
                if (query.outputMode === "files_with_matches") break;
              }
            }
            count = matchedIndices.length;
          }
          return count ? { relFile, lines, matchedIndices, count, fileTruncated } : null;
        } catch {
          return null;
        }
      }),
    );

    for (const item of results) {
      if (!item) continue;
      if (summary) {
        if (files.length === maxResults) {
          truncated = true;
          break;
        }
        files.push({ file: item.relFile, lines: [], count: item.count });
        continue;
      }
      if (item.fileTruncated) {
        anyFileTruncated = true;
      }
      if (totalMatches + item.matchedIndices.length > maxResults) {
        const remaining = maxResults - totalMatches;
        if (remaining > 0) {
          const kept = item.matchedIndices.slice(0, remaining);
          files.push(buildFileMatches(item.relFile, item.lines, kept));
          totalMatches += kept.length;
        }
        truncated = true;
        break;
      }
      files.push(buildFileMatches(item.relFile, item.lines, item.matchedIndices));
      totalMatches += item.matchedIndices.length;
    }
  }

  return {
    files,
    totalMatches,
    truncated: truncated || anyFileTruncated || ((summary || query.multiline === true) && discoveryTruncated),
  };
}

interface GlobFallbackPlan {
  match: (relPosix: string, base: string) => boolean;
  matchedNote: string;
  emptyMessage: string;
}

function buildGlobFallbackPlan(pattern: string): GlobFallbackPlan | null {
  // Case 1: Regex alternation (`a|b|c`) accidentally passed to a glob query
  if (!pattern.includes("{") && pattern.includes("|")) {
    const terms = pattern
      .split("|")
      .map((part) => part.trim())
      .filter(Boolean);
    if (terms.length >= 2) {
      const termMatchers = terms.map((term) => {
        const hasGlobMeta = /[*?[\]{}]/.test(term);
        if (hasGlobMeta) {
          try {
            const re = globToRegExp(term);
            const matchBase = !term.includes("/");
            return (relPosix: string, base: string) =>
              re.test(relPosix) || (matchBase && re.test(base));
          } catch {
            // Fall through to case-insensitive substring matching
          }
        }
        const needle = term.toLowerCase();
        return (relPosix: string) => relPosix.toLowerCase().includes(needle);
      });
      const allBare = terms.every((t) => !/[*?[\]{}/]/.test(t));
      const label = allBare ? `**/*{${terms.join(",")}}*` : `{${terms.join(",")}}`;
      return {
        match: (relPosix, base) => termMatchers.some((fn) => fn(relPosix, base)),
        matchedNote: `[note: exact glob matched 0 files; auto-expanded regex '|' to "${label}" (glob uses * and {a,b}, not |)]`,
        emptyMessage: "(no files matched; hint: glob uses * and {a,b}, not regex |)",
      };
    }
  }

  // Case 2: Bare keyword without glob wildcards, slashes, or extension dot (e.g. "proxy")
  if (/^[A-Za-z0-9_-]+$/.test(pattern)) {
    const needle = pattern.toLowerCase();
    return {
      match: (relPosix) => relPosix.toLowerCase().includes(needle),
      matchedNote: `[note: exact glob "${pattern}" matched 0 files; showing case-insensitive path matches for "*${pattern}*"]`,
      emptyMessage: "(no files matched)",
    };
  }

  return null;
}

async function formatGlobWalkResult(
  walked: { files: string[]; truncated: boolean },
  searchRoot: string,
  baseCwd: string,
  maxResults: number,
  sortBy: SearchQuery["sortBy"],
  notePrefix?: string,
): Promise<string> {
  const relPrefix = toPosixPath(path.relative(baseCwd, searchRoot));
  const sortedFiles = [...walked.files].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  if (sortBy === "mtime") {
    const mtimes = new Map<string, number>();
    for (let i = 0; i < sortedFiles.length; i += 32) {
      await Promise.all(sortedFiles.slice(i, i + 32).map(async (file) => {
        const stat = await fs.lstat(path.join(searchRoot, file)).catch(() => undefined);
        mtimes.set(file, stat?.mtimeMs ?? -Infinity);
      }));
    }
    sortedFiles.sort((a, b) => mtimes.get(b)! - mtimes.get(a)! || (a < b ? -1 : a > b ? 1 : 0));
  }
  const items = sortedFiles.slice(0, maxResults).map((f) => {
    if (!relPrefix || relPrefix === ".") return f;
    return `${relPrefix}/${f}`;
  });
  const truncated = walked.truncated || sortedFiles.length > maxResults;
  const header = notePrefix ? `${notePrefix}\n` : "";

  if (!truncated) {
    return `${header}${items.join("\n")}`;
  }

  // Summarize omitted matches by top-level directory so callers can scope follow-ups
  const omitted = sortedFiles.slice(maxResults);
  const dirCounts = new Map<string, number>();
  for (const f of omitted) {
    const slash = f.indexOf("/");
    const bucket = slash === -1 ? "(root)" : `${f.slice(0, slash)}/`;
    dirCounts.set(bucket, (dirCounts.get(bucket) ?? 0) + 1);
  }
  const topDirs = [...dirCounts.entries()]
    .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .slice(0, 6)
    .map(([dir, cnt]) => `${dir} (${cnt})`);
  const totalLabel = walked.truncated
    ? `${walked.files.length}+`
    : String(walked.files.length);
  const dirSummary =
    topDirs.length > 0 ? `; omitted dirs: ${topDirs.join(", ")}` : "";
  const suffix = `\n... [truncated at ${maxResults} of ${totalLabel} files${dirSummary}]`;
  return `${header}${items.join("\n")}${suffix}`;
}

async function runGlobQuery(
  query: SearchQuery,
  searchRoot: string,
  baseCwd: string,
  isFileTarget: boolean,
): Promise<string> {
  const maxResults = clampInt(query.maxResults ?? 80, 1, 300);
  const pattern = query.pattern.trim() || "**/*";
  const matcher = globToRegExp(pattern);
  const matchBasename = !pattern.includes("/");
  const includeHidden = pattern.startsWith(".") || pattern.includes("/.");

  if (isFileTarget) {
    const rel = toPosixPath(path.relative(baseCwd, searchRoot)) || path.basename(searchRoot);
    const base = path.basename(searchRoot);
    const matched = matcher.test(rel) || (matchBasename && matcher.test(base));
    if (matched) return rel;
    const fallback = buildGlobFallbackPlan(pattern);
    if (fallback) {
      return fallback.match(rel, base)
        ? `${fallback.matchedNote}\n${rel}`
        : fallback.emptyMessage;
    }
    return "(no files matched)";
  }

  const scanLimit = Math.max(maxResults + 1, 2000);
  const walked = await walkFiles(searchRoot, {
    maxFiles: scanLimit,
    includeHidden,
    fileFilter: (relPosix) => {
      if (matcher.test(relPosix)) return true;
      if (matchBasename) {
        const base = relPosix.split("/").pop() ?? relPosix;
        return matcher.test(base);
      }
      return false;
    },
  });

  if (walked.files.length > 0) {
    return formatGlobWalkResult(walked, searchRoot, baseCwd, maxResults, query.sortBy);
  }

  const fallback = buildGlobFallbackPlan(pattern);
  if (fallback) {
    const fallbackWalked = await walkFiles(searchRoot, {
      maxFiles: scanLimit,
      includeHidden,
      fileFilter: (relPosix) => {
        const base = relPosix.split("/").pop() ?? relPosix;
        return fallback.match(relPosix, base);
      },
    });
    if (fallbackWalked.files.length > 0) {
      return formatGlobWalkResult(
        fallbackWalked,
        searchRoot,
        baseCwd,
        maxResults,
        query.sortBy,
        fallback.matchedNote,
      );
    }
    return fallback.emptyMessage;
  }

  return "(no files matched)";
}

function relativeFile(baseCwd: string, file: string): string {
  return toPosixPath(path.relative(baseCwd, path.resolve(baseCwd, file))) || path.basename(file);
}

async function runFuzzyQuery(
  query: SearchQuery,
  searchRoot: string,
  baseCwd: string,
  isFileTarget: boolean,
): Promise<string> {
  const pattern = query.pattern.trim();
  if (!pattern) throw new Error("Fuzzy pattern must not be blank");
  const matchesType = fileTypeMatcher(query.fileType);
  const maxResults = clampInt(query.maxResults ?? 30, 1, 200);
  const walked = isFileTarget
    ? { files: [searchRoot], truncated: false }
    : await walkFiles(searchRoot, { maxFiles: 8000, fileFilter: matchesType });
  const candidates = walked.files
    .filter(matchesType)
    .map((file) => relativeFile(baseCwd, path.resolve(searchRoot, file)));
  const { matches, totalMatched } = rankFuzzyPaths(pattern, candidates, maxResults);
  const body = matches.map((match) => match.path).join("\n") || "(no files matched)";
  const footer = walked.truncated || totalMatched > maxResults
    ? `\n... [truncated; showing ${matches.length} files]`
    : "";
  return body + footer;
}

const SHORTLOG_VALUE_FLAGS = new Set([
  "--group",
  "--format",
  "--since",
  "--after",
  "--until",
  "--before",
  "--author",
  "--committer",
  "--grep",
  "--max-count",
  "--skip",
]);

const SHORTLOG_REV_SELECTOR_FLAGS = new Set([
  "--all",
  "--branches",
  "--tags",
  "--remotes",
  "--glob",
  "--stdin",
]);

function hardenGitExecArgs(validatedArgs: string[]): string[] {
  // validatedArgs is ["--no-pager", subcmd, ...rest]
  const subcmd = validatedArgs[1];
  const rest = validatedArgs.slice(2);
  let subArgs = rest;

  if (subcmd === "diff" || subcmd === "log" || subcmd === "show") {
    subArgs = ["--no-ext-diff", "--no-textconv", ...rest];
  } else if (subcmd === "blame") {
    subArgs = ["--no-textconv", ...rest];
  } else if (subcmd === "remote") {
    const remoteSubIdx = rest.findIndex((a) => !a.startsWith("-"));
    if (remoteSubIdx !== -1 && rest[remoteSubIdx] === "show" && !rest.includes("-n")) {
      subArgs = [...rest.slice(0, remoteSubIdx + 1), "-n", ...rest.slice(remoteSubIdx + 1)];
    }
  } else if (subcmd === "shortlog") {
    // With stdin ignored (/dev/null), `git shortlog` without a revision reads empty stdin
    // unless HEAD (or an explicit rev/selector) is provided.
    let hasRev = false;
    let expectVal = false;
    let dashDashIdx = -1;
    for (let i = 0; i < rest.length; i++) {
      const a = rest[i]!;
      if (expectVal) {
        expectVal = false;
        continue;
      }
      if (a === "--") {
        dashDashIdx = i;
        break;
      }
      if (a.startsWith("-")) {
        const flagName = a.includes("=") ? a.slice(0, a.indexOf("=")) : a;
        if (SHORTLOG_REV_SELECTOR_FLAGS.has(flagName)) {
          hasRev = true;
        } else if (!a.includes("=") && SHORTLOG_VALUE_FLAGS.has(flagName)) {
          expectVal = true;
        }
        continue;
      }
      hasRev = true;
    }
    if (!hasRev) {
      subArgs =
        dashDashIdx === -1
          ? [...rest, "HEAD"]
          : [...rest.slice(0, dashDashIdx), "HEAD", ...rest.slice(dashDashIdx)];
    }
    subArgs = ["--no-ext-diff", "--no-textconv", ...subArgs];
  }

  return [
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.external=",
    "-c",
    "gpg.program=false",
    "-c",
    "gpg.openpgp.program=false",
    "-c",
    "gpg.ssh.program=false",
    "-c",
    "gpg.x509.program=false",
    "--no-pager",
    ...(subcmd ? [subcmd, ...subArgs] : subArgs),
  ];
}

const LOCKFILE_NAMES = new Set([
  "Cargo.lock",
  "package-lock.json",
  "yarn.lock",
  "pnpm-lock.yaml",
  "uv.lock",
  "poetry.lock",
  "go.sum",
  "Gemfile.lock",
]);

function collapseLockfileDiffs(diffText: string, rawPattern: string): string {
  // If user explicitly asked about a lockfile, preserve full diff
  for (const lf of LOCKFILE_NAMES) {
    if (rawPattern.includes(lf)) return diffText;
  }
  if (!diffText.includes("diff --git ")) return diffText;

  const lines = diffText.split("\n");
  const out: string[] = [];
  let skippingLock: string | null = null;
  let skippedLines = 0;

  const flushSkip = () => {
    if (skippingLock !== null) {
      out.push(`[lockfile diff omitted: ${skippingLock} (${skippedLines} lines)]`);
      skippingLock = null;
      skippedLines = 0;
    }
  };

  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      flushSkip();
      const base = line.split(/\s+/).pop()?.split("/").pop() ?? "";
      if (LOCKFILE_NAMES.has(base)) {
        skippingLock = base;
        skippedLines = 1;
        continue;
      }
    }
    if (skippingLock !== null) {
      skippedLines++;
      continue;
    }
    out.push(line);
  }
  flushSkip();
  return out.join("\n");
}

async function runGitQuery(query: SearchQuery, searchRoot: string): Promise<string> {
  const gitArgs = validateReadonlyGitArgs(query.pattern);
  const execArgs = hardenGitExecArgs(gitArgs);
  const res = await execBinaryAsync("git", execArgs, {
    cwd: searchRoot,
    timeoutMs: 10_000,
    maxOutputBytes: 512 * 1024,
  });

  if (res.error) {
    throw new Error(`git execution error: ${res.error}`);
  }
  if (res.timedOut) {
    throw new Error("git query timed out");
  }
  if (res.exitCode !== 0) {
    const msg = (res.stderr || res.stdout).trim();
    throw new Error(msg || `git exited with code ${res.exitCode}`);
  }

  let out = res.stdout.trimEnd();
  const subcmd = gitArgs[1];
  if (out && (subcmd === "diff" || subcmd === "show")) {
    out = collapseLockfileDiffs(out, query.pattern);
  }
  return out || "(empty output)";
}

function formatFileMatches(
  files: FileMatches[],
  totalMatches: number,
  truncated: boolean,
): string {
  if (files.length === 0) {
    return "(no matches)";
  }
  const blocks: string[] = [];
  for (const fm of files) {
    const lines = fm.lines.map((l) => {
      const sep = l.isMatch ? ":" : "-";
      const text = l.text.length > 240 ? `${sliceSafeChars(l.text, 240)}...` : l.text;
      return `  ${l.lineNumber}${sep} ${text}`;
    });
    blocks.push(`${fm.file}\n${lines.join("\n")}`);
  }
  const footer = truncated ? `\n... [truncated at ${totalMatches} matches]` : "";
  return `${blocks.join("\n")}${footer}`;
}

async function executeSingleQuery(
  query: SearchQuery,
  index: number,
  baseCwd: string,
  useRg: boolean,
): Promise<string> {
  const header = `## Query ${index + 1} [${query.type}] ${query.pattern}${query.path ? ` (in ${query.path})` : ""}`;
  try {
    if (!query.pattern || typeof query.pattern !== "string") {
      throw new Error("Query 'pattern' must be a non-empty string");
    }
    if (query.multiline !== undefined && typeof query.multiline !== "boolean") {
      throw new Error("multiline must be a boolean");
    }
    if (query.outputMode !== undefined && !["content", "files_with_matches", "count"].includes(query.outputMode)) {
      throw new Error("Invalid outputMode");
    }
    if (query.sortBy !== undefined && !["path", "mtime"].includes(query.sortBy)) {
      throw new Error("Invalid sortBy");
    }
    const searchRoot = query.path ? resolvePath(query.path, baseCwd) : baseCwd;
    let targetStat;
    try {
      targetStat = await fs.stat(searchRoot);
    } catch {
      throw new Error(`Path does not exist: ${query.path || baseCwd}`);
    }

    if (query.type === "git") {
      const gitCwd = targetStat.isFile() ? path.dirname(searchRoot) : searchRoot;
      const out = await runGitQuery(query, gitCwd);
      return `${header}\n${out}`;
    }

    if (query.type === "glob") {
      const out = await runGlobQuery(query, searchRoot, baseCwd, targetStat.isFile());
      return `${header}\n${out}`;
    }

    if (query.type === "fuzzy") {
      const out = await runFuzzyQuery(query, searchRoot, baseCwd, targetStat.isFile());
      return `${header}\n${out}`;
    }

    if (query.type === "text" || query.type === "regex") {
      let res;
      if (useRg) {
        try {
          res = await runTextOrRegexWithRg(query, searchRoot, baseCwd);
        } catch (err) {
          // Only fallback on spawn error; surface regex errors directly
          if (err instanceof Error && err.message.startsWith("rg spawn error:")) {
            res = await runTextOrRegexWithNode(query, searchRoot, baseCwd);
          } else {
            throw err;
          }
        }
      } else {
        res = await runTextOrRegexWithNode(query, searchRoot, baseCwd);
      }
      if (query.outputMode === "count" || query.outputMode === "files_with_matches") {
        const body = res.files.map(({ file, count }) => query.outputMode === "count"
          ? `${file}: ${count} ${query.multiline ? "matches" : "matching lines"}`
          : file).join("\n") || "(no matches)";
        const footer = res.truncated ? `\n... [truncated; showing ${res.files.length} files]` : "";
        return `${header}\n${body}${footer}`;
      }
      return `${header}\n${formatFileMatches(res.files, res.totalMatches, res.truncated)}`;
    }

    throw new Error(`Unsupported query type: ${String(query.type)}`);
  } catch (err) {
    return `${header}\nERROR: ${err instanceof Error ? err.message : String(err)}`;
  }
}

export async function runBatchSearch(
  input: BatchSearchInput,
  options: BatchSearchRuntimeOptions = {},
): Promise<{ text: string; isError: boolean }> {
  const parsedQueries = parseMaybeJsonArray(input?.queries);
  if (!parsedQueries.ok) {
    if (
      parsedQueries.kind === "corrupted_literal" ||
      (parsedQueries.kind === "wrong_type" &&
        (typeof input?.queries === "boolean" || input?.queries === null))
    ) {
      return {
        text: formatCorruptedArrayError({
          header: "Validation failed",
          toolName: "batch_search",
          items: [
            {
              label: "queries",
              value:
                parsedQueries.kind === "corrupted_literal" ? parsedQueries.value : input.queries,
              expected: "an array of query objects",
            },
          ],
          fallbackAdvice:
            "split into smaller queries or use a single compound read-only run_command.",
        }),
        isError: true,
      };
    }
    return {
      text: "Error: 'queries' must be a non-empty array of search queries.",
      isError: true,
    };
  }
  const rawQueries = parsedQueries.value;
  if (rawQueries.length === 0) {
    return {
      text: "Error: 'queries' must be a non-empty array of search queries.",
      isError: true,
    };
  }
  if (rawQueries.length > 20) {
    return {
      text: "Error: 'queries' supports at most 20 queries per call.",
      isError: true,
    };
  }

  const corruptedQueries: CorruptedItem[] = [];
  for (let i = 0; i < rawQueries.length; i++) {
    if (!isPlainObject(rawQueries[i])) {
      corruptedQueries.push({
        label: `queries[${i}]`,
        value: rawQueries[i],
        expected: "a query object ({ type, pattern, ... })",
      });
    }
  }
  if (corruptedQueries.length > 0) {
    return {
      text: formatCorruptedArrayError({
        header: "Validation failed",
        toolName: "batch_search",
        items: corruptedQueries,
        fallbackAdvice:
          "split into smaller queries or use a single compound read-only run_command.",
      }),
      isError: true,
    };
  }
  const queries = rawQueries as unknown as SearchQuery[];

  const baseCwd = resolvePath(input.cwd || process.cwd());
  try {
    const st = await fs.stat(baseCwd);
    if (!st.isDirectory()) {
      return { text: `Error: cwd is not a directory: ${baseCwd}`, isError: true };
    }
  } catch {
    return { text: `Error: cwd does not exist: ${baseCwd}`, isError: true };
  }

  const maxChars = clampInt(input.maxChars ?? 8_500, 500, DEFAULT_INLINE_MAX_BYTES);
  const maxBytes = DEFAULT_INLINE_MAX_BYTES;
  const needsTextSearch = queries.some(
    (q) => q.type === "text" || q.type === "regex",
  );
  const useRg =
    needsTextSearch && !options.forceNodeFallback && (await isRipgrepAvailable());

  const sections = await Promise.all(
    queries.map((q, idx) => executeSingleQuery(q, idx, baseCwd, useRg)),
  );

  // Allocate budget fairly across queries via water-filling within maxChars & inline UTF-8 byte ceiling
  return {
    text: fitSectionsToByteBudget(sections, maxChars, maxBytes, "\n\n"),
    isError: false,
  };
}

function clampInt(val: number, min: number, max: number): number {
  if (!Number.isFinite(val)) return min;
  return Math.max(min, Math.min(max, Math.trunc(val)));
}
