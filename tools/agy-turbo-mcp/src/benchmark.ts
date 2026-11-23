import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  analyzePaths,
  analyzePathsByUsage,
  formatCompareMarkdown,
  formatMetricsMarkdown,
  formatUsageSplitMarkdown,
  type AnalyzeOptions,
} from "./benchmark-core.js";

async function assertPathsExist(paths: string[]): Promise<boolean> {
  for (const p of paths) {
    try {
      await fs.stat(path.resolve(p));
    } catch {
      process.stderr.write(`Error: path does not exist: ${p}\n`);
      return false;
    }
  }
  return true;
}

function parsePositiveInt(raw: string): number | null {
  if (!/^\d+$/.test(raw)) return null;
  const n = Number(raw);
  if (!Number.isSafeInteger(n) || n < 1) return null;
  return n;
}

function parseIsoTimestamp(raw: string, endOfDayIfDateOnly = false): number | null {
  const trimmed = raw.trim();
  if (!trimmed || !/^\d{4}-\d{2}-\d{2}(?:[T\s].*)?$/.test(trimmed)) return null;
  const normalized =
    endOfDayIfDateOnly && /^\d{4}-\d{2}-\d{2}$/.test(trimmed)
      ? `${trimmed}T23:59:59.999Z`
      : trimmed;
  const ms = Date.parse(normalized);
  return Number.isFinite(ms) ? ms : null;
}

function getHomeDir(env: NodeJS.ProcessEnv = process.env): string {
  return process.platform === "win32"
    ? env.USERPROFILE || env.HOME || os.homedir()
    : env.HOME || env.USERPROFILE || os.homedir();
}

async function discoverLocalBrainDirs(
  env: NodeJS.ProcessEnv = process.env,
): Promise<string[]> {
  const home = getHomeDir(env);
  const seen = new Set<string>();
  const dirs: string[] = [];
  const addDir = (p: string): void => {
    const resolved = path.resolve(p);
    const key =
      process.platform === "win32" || process.platform === "darwin"
        ? resolved.toLowerCase()
        : resolved;
    if (seen.has(key)) return;
    seen.add(key);
    dirs.push(resolved);
  };

  addDir(path.join(home, ".gemini", "antigravity", "brain"));
  addDir(path.join(home, ".gemini", "antigravity-cli", "brain"));
  addDir(path.join(home, ".gemini", "antigravity-ide", "brain"));

  const addIfExistingDir = async (p: string): Promise<void> => {
    try {
      const st = await fs.stat(p);
      if (st.isDirectory()) addDir(p);
    } catch {
      // Ignore missing brain dir
    }
  };

  // Also discover ACP / T3 Code Antigravity provider brains under ~/.t3/userdata/providers/antigravity/*/antigravity-acp/brain
  const t3ProvidersDir = path.join(
    home,
    ".t3",
    "userdata",
    "providers",
    "antigravity",
  );
  try {
    const entries = await fs.readdir(t3ProvidersDir, { withFileTypes: true });
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      await addIfExistingDir(
        path.join(t3ProvidersDir, entry.name, "antigravity-acp", "brain"),
      );
    }
  } catch {
    // ~/.t3 not present
  }

  // Also discover brains under an explicit isolated GEMINI_HOME or ANTIGRAVITY_APP_DATA_DIR
  const envGeminiHome = env.GEMINI_HOME?.trim();
  if (envGeminiHome) {
    await addIfExistingDir(path.join(envGeminiHome, "antigravity-acp", "brain"));
    await addIfExistingDir(path.join(envGeminiHome, "antigravity-cli", "brain"));
  }
  const envAppDataDir = env.ANTIGRAVITY_APP_DATA_DIR?.trim();
  if (envAppDataDir) {
    await addIfExistingDir(path.join(envAppDataDir, "brain"));
  }

  return dirs;
}

function printUsage(): void {
  process.stdout.write(
    [
      "Usage:",
      "  node dist/benchmark.mjs analyze <path...> [--recent <N>] [--since <ISO>] [--until <ISO>] [--split-usage] [--json]",
      "  node dist/benchmark.mjs analyze --local [--recent <N>] [--since <ISO>] [--until <ISO>] [--split-usage] [--json]",
      "  node dist/benchmark.mjs compare --before <path> --after <path> [--json]",
      "",
      "Options:",
      "  --local        Scan local Antigravity brain dirs (Desktop 2.0, CLI, IDE, and ACP)",
      "  --recent <N>   Analyze only the N most recently modified transcripts",
      "  --since <ISO>  Include only steps at or after an ISO-8601 timestamp (e.g. 2026-11-19T12:51:00+08:00)",
      "  --until <ISO>  Include only steps at or before an ISO-8601 timestamp",
      "  --split-usage  Split metrics into sessions with vs. without agy-fast tool calls",
      "  --json         Output raw JSON metrics",
    ].join("\n") + "\n",
  );
}

async function main(argv: string[]): Promise<void> {
  const args = argv.slice(2);
  if (args.length === 0 || args.includes("-h") || args.includes("--help")) {
    printUsage();
    return;
  }

  const cmd = args[0];

  if (cmd === "analyze") {
    const targets: string[] = [];
    const explicitTargets: string[] = [];
    let recent: number | undefined;
    let sinceMs: number | undefined;
    let untilMs: number | undefined;
    let splitUsage = false;
    let asJson = false;
    let afterDoubleDash = false;
    for (let i = 1; i < args.length; i++) {
      const a = args[i]!;
      if (!afterDoubleDash && a === "--") {
        afterDoubleDash = true;
        continue;
      }
      if (!afterDoubleDash && a === "--json") {
        asJson = true;
        continue;
      }
      if (!afterDoubleDash && a === "--split-usage") {
        splitUsage = true;
        continue;
      }
      if (!afterDoubleDash && a === "--local") {
        targets.push(...(await discoverLocalBrainDirs()));
        continue;
      }
      if (!afterDoubleDash && (a === "--recent" || a.startsWith("--recent="))) {
        const raw = a === "--recent" ? args[++i] : a.slice("--recent=".length);
        const parsed = raw !== undefined ? parsePositiveInt(raw) : null;
        if (parsed === null) {
          process.stderr.write("Error: --recent must be a positive integer\n");
          process.exitCode = 1;
          return;
        }
        recent = parsed;
        continue;
      }
      if (!afterDoubleDash && (a === "--since" || a.startsWith("--since="))) {
        const raw = a === "--since" ? args[++i] : a.slice("--since=".length);
        const parsed = raw !== undefined ? parseIsoTimestamp(raw) : null;
        if (parsed === null) {
          process.stderr.write("Error: --since must be a valid ISO-8601 date or timestamp\n");
          process.exitCode = 1;
          return;
        }
        sinceMs = parsed;
        continue;
      }
      if (!afterDoubleDash && (a === "--until" || a.startsWith("--until="))) {
        const raw = a === "--until" ? args[++i] : a.slice("--until=".length);
        const parsed = raw !== undefined ? parseIsoTimestamp(raw, true) : null;
        if (parsed === null) {
          process.stderr.write("Error: --until must be a valid ISO-8601 date or timestamp\n");
          process.exitCode = 1;
          return;
        }
        untilMs = parsed;
        continue;
      }
      if (!afterDoubleDash && a.startsWith("-")) {
        process.stderr.write(`Error: unknown option: ${a}\n`);
        process.exitCode = 1;
        return;
      }
      targets.push(a);
      explicitTargets.push(a);
    }
    if (targets.length === 0) {
      printUsage();
      process.exitCode = 1;
      return;
    }
    if (sinceMs !== undefined && untilMs !== undefined && sinceMs > untilMs) {
      process.stderr.write("Error: --since must be earlier than or equal to --until\n");
      process.exitCode = 1;
      return;
    }
    if (explicitTargets.length > 0 && !(await assertPathsExist(explicitTargets))) {
      process.exitCode = 1;
      return;
    }
    const opts: AnalyzeOptions = {};
    if (recent !== undefined) opts.recent = recent;
    if (sinceMs !== undefined) opts.sinceMs = sinceMs;
    if (untilMs !== undefined) opts.untilMs = untilMs;

    if (splitUsage) {
      const split = await analyzePathsByUsage(targets, opts);
      process.stdout.write(
        asJson
          ? `${JSON.stringify(split, null, 2)}\n`
          : `${formatUsageSplitMarkdown(split)}\n`,
      );
      return;
    }

    const metrics = await analyzePaths(targets, opts);
    process.stdout.write(
      asJson
        ? `${JSON.stringify(metrics, null, 2)}\n`
        : `${formatMetricsMarkdown(metrics)}\n`,
    );
    return;
  }

  if (cmd === "compare") {
    let beforePath = "";
    let afterPath = "";
    let asJson = false;
    for (let i = 1; i < args.length; i++) {
      const a = args[i]!;
      if (a === "--json") {
        asJson = true;
      } else if (a === "--before" || a.startsWith("--before=")) {
        const val = a === "--before" ? args[++i] : a.slice("--before=".length);
        if (!val || (a === "--before" && val.startsWith("-"))) {
          process.stderr.write("Error: --before requires a path\n");
          process.exitCode = 1;
          return;
        }
        beforePath = val;
      } else if (a === "--after" || a.startsWith("--after=")) {
        const val = a === "--after" ? args[++i] : a.slice("--after=".length);
        if (!val || (a === "--after" && val.startsWith("-"))) {
          process.stderr.write("Error: --after requires a path\n");
          process.exitCode = 1;
          return;
        }
        afterPath = val;
      } else {
        process.stderr.write(`Error: unknown option: ${a}\n`);
        process.exitCode = 1;
        return;
      }
    }
    if (!beforePath || !afterPath) {
      printUsage();
      process.exitCode = 1;
      return;
    }
    if (!(await assertPathsExist([beforePath, afterPath]))) {
      process.exitCode = 1;
      return;
    }
    const [before, after] = await Promise.all([
      analyzePaths([beforePath]),
      analyzePaths([afterPath]),
    ]);
    process.stdout.write(
      asJson
        ? `${JSON.stringify({ before, after }, null, 2)}\n`
        : `${formatCompareMarkdown(before, after)}\n`,
    );
    return;
  }

  printUsage();
  process.exitCode = 1;
}

void main(process.argv);
