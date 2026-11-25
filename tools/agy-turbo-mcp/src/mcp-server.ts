import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { McpStdioServer, type McpToolDefinition } from "./protocol/mcp.js";
import { MCP_CALL_BUDGET_SEC } from "./utils/exec-utils.js";
import {
  readSavedEditsFormatSync,
  resolveHostCompat,
  type EditsFormatMode,
  type ResolvedHostCompat,
} from "./utils/host-compat.js";
import { runApplyEdits, type ApplyEditsInput } from "./tools/apply-edits.js";
import { runBatchSearch, type BatchSearchInput } from "./tools/batch-search.js";
import { runCodeGraph, type CodeGraphInput } from "./tools/code-graph.js";
import { runReadSlices, type ReadSlicesInput } from "./tools/read-slices.js";
import { runChecks, type RunChecksInput } from "./tools/run-checks.js";

const SERVER_VERSION = "4.0.0";

function recordFastTelemetry(
  tool: string,
  outText: string,
  wallMs: number,
  isError: boolean,
): void {
  if (process.env.WM_FAST_NO_STATS === "1") return;
  try {
    const statsPath =
      process.env.WM_FAST_STATS ||
      path.join(os.homedir(), ".local", "share", "whitemagic", "wm_fast_stats.jsonl");
    fs.mkdirSync(path.dirname(statsPath), { recursive: true });
    const row = JSON.stringify({
      ts: Math.floor(Date.now() / 1000),
      tool,
      out: Buffer.byteLength(outText, "utf8"),
      wall_ms: Math.max(0, Math.round(wallMs)),
      err: isError,
    });
    fs.appendFileSync(statsPath, row + "\n", "utf8");
  } catch {
    // Telemetry is strictly fail-open.
  }
}

async function wrapFastTool(
  tool: string,
  fn: () => Promise<{ text: string; isError?: boolean }>,
): Promise<{ text: string; isError?: boolean }> {
  const t0 = Date.now();
  const res = await fn();
  recordFastTelemetry(tool, res.text ?? "", Date.now() - t0, Boolean(res.isError));
  return res;
}

export interface AgyFastServerOptions {
  env?: NodeJS.ProcessEnv;
  configEditsFormat?: EditsFormatMode;
}

function buildApplyEditsToolDefinition(
  compat: ResolvedHostCompat,
): McpToolDefinition {
  const preferStringEdits = compat.editsFormat === "string";
  const editsSchema = preferStringEdits
    ? {
        type: "string",
        description:
          'JSON array string of 1-50 files to edit or create: [{"path", "hunks"?: [{"target", "replacement", "startLine"?, "endLine"?, "replaceAll"?}], "create"?, "overwrite"?}]. A hunk replaces exact target text (optional 1-based line window; replaceAll replaces every occurrence in it); "create" writes the whole file instead of hunks, and "overwrite" allows replacing an existing file. A native array is also accepted, but this host version can deliver a long array of objects in call_mcp_tool Arguments as [] while a string arrives intact.',
      }
    : {
        type: "array",
        minItems: 1,
        maxItems: 50,
        description:
          "Files to edit or create (a JSON array string of the same shape is also accepted).",
        items: {
          type: "object",
          properties: {
            path: { type: "string" },
            hunks: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  target: { type: "string", description: "Exact text to replace." },
                  replacement: { type: "string", description: "Replacement text." },
                  startLine: {
                    type: "integer",
                    minimum: 1,
                    description: "Optional 1-based start line to narrow matching.",
                  },
                  endLine: {
                    type: "integer",
                    minimum: 1,
                    description: "Optional 1-based end line to narrow matching.",
                  },
                  replaceAll: {
                    type: "boolean",
                    description: "Replace every occurrence in range (default false).",
                  },
                  strictWindow: {
                    type: "boolean",
                    description: "Disable safe unique auto-relocate / trailing-whitespace fallback for this hunk (default false).",
                  },
                },
                required: ["target", "replacement"],
              },
            },
            create: {
              type: "string",
              description: "Create file with this full content instead of hunks.",
            },
            overwrite: {
              type: "boolean",
              description: "Allow overwriting existing file when using 'create'.",
            },
          },
          required: ["path"],
        },
      };

  return {
    name: "apply_edits",
    description: preferStringEdits
      ? "Atomically apply multiple non-contiguous hunks across one or more files (all-or-nothing) with optional file creation and inline verify command. Pass 'edits' as one JSON array string."
      : "Atomically apply multiple non-contiguous hunks across one or more files (all-or-nothing) with optional file creation and inline verify command.",
    inputSchema: {
      type: "object",
      properties: {
        cwd: {
          type: "string",
          description:
            "Base directory for relative file paths and verify command (defaults to the first workspace root).",
        },
        edits: editsSchema,
        dryRun: {
          type: "boolean",
          description: "Validate all hunks without writing files (default false).",
        },
        bestEffort: {
          type: "boolean",
          description:
            "When true, write valid files even if other files fail validation (each file remains atomic; failed files return closest-line snippets and verify is skipped until fixed). Default false (all-or-nothing).",
        },
        verify: {
          type: "string",
          description:
            "Optional shell command run right after writing (e.g. 'npm run typecheck').",
        },
        verifyTimeoutSec: {
          type: "integer",
          minimum: 1,
          maximum: MCP_CALL_BUDGET_SEC,
          description: `Optional timeout in seconds for the verify command (default 60, max ${MCP_CALL_BUDGET_SEC}: agy drops MCP results after 180s).`,
        },
        maxVerifyChars: {
          type: "integer",
          minimum: 300,
          maximum: 7500,
          description:
            "Optional character cap for verify output (default 4000; full response hard-capped <= 8800 UTF-8 bytes).",
        },
      },
      required: ["edits"],
    },
  };
}

export function createAgyFastServer(
  options: AgyFastServerOptions = {},
): McpStdioServer {
  const env = options.env ?? process.env;
  const savedEditsFormat =
    options.configEditsFormat ?? readSavedEditsFormatSync(env);
  let hostCompat = resolveHostCompat({
    env,
    configEditsFormat: savedEditsFormat,
  });

  // agy starts plugin servers in the plugin directory; resolve omitted or relative
  // tool `cwd` values against the first valid workspace root without mutating process.cwd().
  let defaultCwd: string | undefined;

  const resolveToolArgs = <T extends { cwd?: string }>(
    args: Record<string, unknown>,
  ): { ok: true; value: T } | { ok: false; error: string } => {
    if (args.cwd !== undefined && args.cwd !== null && typeof args.cwd !== "string") {
      return {
        ok: false,
        error: `Error: 'cwd' must be a string path (received ${Array.isArray(args.cwd) ? "array" : typeof args.cwd}).`,
      };
    }
    const rawCwd = typeof args.cwd === "string" ? args.cwd.trim() : "";
    if (!rawCwd) {
      return {
        ok: true,
        value: (defaultCwd ? { ...args, cwd: defaultCwd } : { ...args, cwd: undefined }) as unknown as T,
      };
    }
    const isHomePath = rawCwd === "~" || rawCwd.startsWith("~/") || rawCwd.startsWith("~\\");
    if (defaultCwd && !path.isAbsolute(rawCwd) && !isHomePath) {
      return {
        ok: true,
        value: { ...args, cwd: path.resolve(defaultCwd, rawCwd) } as unknown as T,
      };
    }
    return { ok: true, value: { ...args, cwd: rawCwd } as unknown as T };
  };

  const server = new McpStdioServer({
    name: "whitemagic-agy-fast",
    version: SERVER_VERSION,
    onInitialize: ({ clientInfo }) => {
      hostCompat = resolveHostCompat({
        clientInfo,
        env,
        configEditsFormat: savedEditsFormat,
      });
    },
    onWorkspaceRoots: (paths) => {
      for (const candidate of paths) {
        try {
          if (fs.statSync(candidate).isDirectory()) {
            defaultCwd = candidate;
            return;
          }
        } catch {
          // Ignore nonexistent or inaccessible root paths
        }
      }
    },
  });

  server.registerTool(
    {
      name: "batch_search",
      description:
        "Run multiple read-only text/regex searches, file globs, fuzzy path lookups, and read-only git queries (status/diff/log/show/branch) concurrently in one call with bounded output.",
      inputSchema: {
        type: "object",
        properties: {
          cwd: {
            type: "string",
            description: "Base directory for relative paths (defaults to the first workspace root).",
          },
          queries: {
            type: "array",
            minItems: 1,
            maxItems: 20,
            description: "List of search/glob/git queries to run concurrently.",
            items: {
              type: "object",
              properties: {
                type: {
                  type: "string",
                  enum: ["text", "regex", "glob", "fuzzy", "git"],
                },
                pattern: {
                  type: "string",
                  description:
                    "Literal text, regex, fuzzy path query, glob pattern (use * and {a,b}, not regex |), or read-only git subcommand (e.g. 'status --short', 'log -n 5 --oneline').",
                },
                path: {
                  type: "string",
                  description: "Optional subdirectory or file relative to cwd.",
                },
                glob: {
                  type: "string",
                  description: "Optional file filter glob for text/regex queries (e.g. '*.ts').",
                },
                multiline: {
                  type: "boolean",
                  description: "Allow text/regex to span lines. In multiline regex, dot matches newlines.",
                },
                outputMode: {
                  type: "string",
                  enum: ["content", "files_with_matches", "count"],
                  description: "Text/regex output (default content). Count reports matching lines per file, or occurrences in multiline mode.",
                },
                fileType: {
                  type: "string",
                  description: "Language or extension filter for text, regex, or fuzzy queries (e.g. typescript or ts).",
                },
                sortBy: {
                  type: "string",
                  enum: ["path", "mtime"],
                  description: "Glob order (default path). Use mtime for newest first.",
                },
                caseSensitive: {
                  type: "boolean",
                  description: "Case sensitivity for text/regex (default true).",
                },
                contextLines: {
                  type: "integer",
                  minimum: 0,
                  maximum: 5,
                  description: "Context lines around matches (default 0).",
                },
                maxResults: {
                  type: "integer",
                  minimum: 1,
                  maximum: 200,
                  description: "Max matches/files for this query (default 40).",
                },
              },
              required: ["type", "pattern"],
            },
          },
          maxChars: {
            type: "integer",
            minimum: 500,
            maximum: 8800,
            description: "Total character cap (default 8500; hard inline UTF-8 cap <= 8800 bytes to avoid disk spills).",
          },
        },
        required: ["queries"],
      },
    },
    async (args) => {
      const resolved = resolveToolArgs<BatchSearchInput>(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("batch_search", () => runBatchSearch(resolved.value));
    },
  );

  server.registerTool(
    () => buildApplyEditsToolDefinition(hostCompat),
    async (args) => {
      const resolved = resolveToolArgs<ApplyEditsInput>(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("apply_edits", () => runApplyEdits(resolved.value));
    },
  );

  server.registerTool(
    {
      name: "read_slices",
      description:
        "Batch-extract symbol/section outlines (TS/JS/Vue/Svelte/CSS/Python/GDScript/Go/Rust/Java/MD/JSON/YAML) across files (prefer 10-20 files/batch, max 40) in one call, plus optional tiny <=120-line slices. For reading >150 lines of known ranges, prefer parallel built-in read_file(StartLine, EndLine) (46KB/call inline, 0 remote check).",
      inputSchema: {
        type: "object",
        properties: {
          cwd: {
            type: "string",
            description: "Base directory for relative paths (defaults to the first workspace root).",
          },
          outlines: {
            type: "array",
            maxItems: 40,
            items: { type: "string" },
            description: "File paths to extract top-level symbol/section line ranges from (prefer 10-20 per batch for fuller outlines; max 40).",
          },
          slices: {
            type: "array",
            maxItems: 30,
            description: "Optional tiny 1-based inclusive [startLine, endLine] ranges to piggyback (total response hard-capped <= 8800 UTF-8 bytes).",
            items: {
              type: "object",
              properties: {
                path: { type: "string" },
                ranges: {
                  type: "array",
                  minItems: 1,
                  items: {
                    type: "array",
                    minItems: 2,
                    maxItems: 2,
                    items: { type: "integer", minimum: 1 },
                  },
                },
              },
              required: ["path", "ranges"],
            },
          },
          maxChars: {
            type: "integer",
            minimum: 1000,
            maximum: 8800,
            description: "Total character cap (default 8500; hard inline UTF-8 cap <= 8800 bytes to prevent output.txt spills).",
          },
        },
      },
    },
    async (args) => {
      const resolved = resolveToolArgs<ReadSlicesInput>(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("read_slices", () => runReadSlices(resolved.value));
    },
  );

  server.registerTool(
    {
      name: "run_checks",
      description:
        "Run multiple independent non-interactive shell commands concurrently under one check (build/typecheck/lint/test with outputMode 'compact' using wm-squeeze CoACT compression + git caching, or diagnostic/audit scripts with outputMode 'full').",
      inputSchema: {
        type: "object",
        properties: {
          cwd: {
            type: "string",
            description: "Base directory for relative paths (defaults to the first workspace root).",
          },
          commands: {
            type: "array",
            minItems: 1,
            maxItems: 12,
            description: "Commands to execute concurrently.",
            items: {
              type: "object",
              properties: {
                name: { type: "string", description: "Short label (e.g. 'typecheck')." },
                command: { type: "string", description: "Shell command to run." },
                cwd: { type: "string", description: "Optional working directory override." },
                timeoutSec: { type: "integer", minimum: 1, maximum: MCP_CALL_BUDGET_SEC },
              },
              required: ["command"],
            },
          },
          concurrency: {
            type: "integer",
            minimum: 1,
            maximum: 8,
            description: "Max parallel commands (default 4).",
          },
          timeoutSec: {
            type: "integer",
            minimum: 1,
            maximum: MCP_CALL_BUDGET_SEC,
            description: `Default per-command timeout in seconds (default 60). All commands of one call share a ${MCP_CALL_BUDGET_SEC}s budget because agy drops MCP results after 180s; commands that cannot start within it are reported as not run.`,
          },
          outputMode: {
            type: "string",
            enum: ["compact", "full"],
            description: "'compact' hides verbose pass logs and uses wm-squeeze CoACT compression+cache (default); 'full' preserves stdout for diagnostic/inspection commands.",
          },
          noSqueeze: {
            type: "boolean",
            description: "Bypass wm-squeeze compression/caching in compact mode (default false).",
          },
          maxChars: {
            type: "integer",
            minimum: 500,
            maximum: 8800,
            description: "Total character cap (default 8000; hard inline UTF-8 cap <= 8800 bytes).",
          },
        },
        required: ["commands"],
      },
    },
    async (args) => {
      const resolved = resolveToolArgs<RunChecksInput>(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("run_checks", () => runChecks(resolved.value));
    },
  );

  server.registerTool(
    {
      name: "code_graph",
      description:
        "Incremental AST/symbol & cross-file call-graph query via wm-codegraph (<6ms, saves 85-99% tokens vs grep+read loops). Modes: overview, defs, callers, callees, impact, skeleton.",
      inputSchema: {
        type: "object",
        properties: {
          mode: {
            type: "string",
            enum: ["overview", "defs", "callers", "callees", "impact", "skeleton"],
          },
          target: {
            type: "string",
            description: "Symbol name or file path fragment (optional for overview).",
          },
          root: {
            type: "string",
            description: "Repository root directory (defaults to cwd / workspace root).",
          },
          cwd: {
            type: "string",
            description: "Base directory for relative root paths.",
          },
          depth: {
            type: "integer",
            minimum: 1,
            maximum: 3,
            description: "BFS depth for callers (1-3, default 2).",
          },
          maxChars: {
            type: "integer",
            minimum: 500,
            maximum: 8800,
            description: "Total output cap (default 8500; hard inline UTF-8 cap <= 8800 bytes).",
          },
        },
        required: ["mode"],
      },
    },
    async (args) => {
      const resolved = resolveToolArgs<CodeGraphInput>(args);
      if (!resolved.ok) return { text: resolved.error, isError: true };
      return wrapFastTool("code_graph", () => runCodeGraph(resolved.value));
    },
  );

  return server;
}

function main(): void {
  const server = createAgyFastServer();
  const exitImmediately = () => {
    process.exit(0);
  };
  const flushAndExit = () => {
    // Wait for buffered stdout pipe writes (>64KB) to drain before exiting
    process.stdout.end(() => {
      process.exit(0);
    });
  };
  process.stdout.on("error", (err: NodeJS.ErrnoException) => {
    if (err.code === "EPIPE" || err.code === "ERR_STREAM_DESTROYED") {
      process.exit(0);
    }
  });
  process.on("SIGINT", exitImmediately);
  process.on("SIGTERM", exitImmediately);
  server.listen(process.stdin, process.stdout, flushAndExit);
}

main();
