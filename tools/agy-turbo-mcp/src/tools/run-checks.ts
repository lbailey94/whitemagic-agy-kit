import { promises as fs } from "node:fs";
import { performance } from "node:perf_hooks";
import {
  formatCorruptedArrayError,
  isPlainObject,
  parseMaybeJsonArray,
  type CorruptedItem,
} from "../utils/arg-validation.js";
import { MCP_CALL_BUDGET_SEC, runShellCommandAsync, type ExecResult } from "../utils/exec-utils.js";
import { resolvePath } from "../utils/fs-utils.js";
import {
  DEFAULT_INLINE_MAX_BYTES,
  fitSectionsToByteBudget,
  formatCompactCommandOutput,
  normalizeTerminalOutput,
  truncateWithByteNotice,
  truncateWithNotice,
} from "../utils/output-filter.js";
import { tryRunWithWmSqueeze } from "../utils/wm-squeeze-bridge.js";

export interface CheckCommandSpec {
  name?: string;
  command: string;
  cwd?: string;
  timeoutSec?: number;
}

export interface RunChecksInput {
  cwd?: string;
  commands: Array<string | CheckCommandSpec>;
  concurrency?: number;
  timeoutSec?: number;
  outputMode?: "compact" | "full";
  noSqueeze?: boolean;
  maxChars?: number;
}

interface NormalizedCommand {
  index: number;
  name: string;
  command: string;
  cwd: string;
  timeoutMs: number;
}

interface CheckRunOutcome {
  spec: NormalizedCommand;
  /** null when the call budget ran out before the command could start. */
  result: ExecResult | null;
  passed: boolean;
  squeezed?: boolean;
}

async function mapWithConcurrency<T, R>(
  items: T[],
  concurrency: number,
  worker: (item: T) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let nextIdx = 0;

  async function runWorker(): Promise<void> {
    while (true) {
      const idx = nextIdx++;
      if (idx >= items.length) return;
      results[idx] = await worker(items[idx]!);
    }
  }

  const workerCount = Math.min(concurrency, items.length);
  await Promise.all(Array.from({ length: workerCount }, () => runWorker()));
  return results;
}

export async function runChecks(
  input: RunChecksInput,
  budgetMs = MCP_CALL_BUDGET_SEC * 1000,
): Promise<{ text: string; isError: boolean }> {
  const parsedCommands = parseMaybeJsonArray(input?.commands);
  if (!parsedCommands.ok) {
    if (
      parsedCommands.kind === "corrupted_literal" ||
      (parsedCommands.kind === "wrong_type" &&
        (typeof input?.commands === "boolean" || input?.commands === null))
    ) {
      return {
        text: formatCorruptedArrayError({
          header: "Validation failed",
          toolName: "run_checks",
          items: [
            {
              label: "commands",
              value:
                parsedCommands.kind === "corrupted_literal" ? parsedCommands.value : input.commands,
              expected: "an array of command specs",
            },
          ],
          fallbackAdvice: "immediately fall back to a single compound run_command call.",
        }),
        isError: true,
      };
    }
    return {
      text: "Error: 'commands' must be a non-empty array of check commands.",
      isError: true,
    };
  }
  const rawCommands = parsedCommands.value;
  if (rawCommands.length === 0) {
    return {
      text: "Error: 'commands' must be a non-empty array of check commands.",
      isError: true,
    };
  }
  if (rawCommands.length > 12) {
    return {
      text: "Error: 'commands' supports at most 12 commands per call.",
      isError: true,
    };
  }

  const corruptedCommands: CorruptedItem[] = [];
  for (let i = 0; i < rawCommands.length; i++) {
    const raw = rawCommands[i];
    if (typeof raw !== "string" && !isPlainObject(raw)) {
      corruptedCommands.push({
        label: `commands[${i}]`,
        value: raw,
        expected: "a command string or object ({ name?, command, cwd?, timeoutSec? })",
      });
    }
  }
  if (corruptedCommands.length > 0) {
    return {
      text: formatCorruptedArrayError({
        header: "Validation failed",
        toolName: "run_checks",
        items: corruptedCommands,
        fallbackAdvice: "immediately fall back to a single compound run_command call.",
      }),
      isError: true,
    };
  }

  const baseCwd = resolvePath(input.cwd || process.cwd());
  try {
    const st = await fs.stat(baseCwd);
    if (!st.isDirectory()) {
      return { text: `Error: cwd is not a directory: ${baseCwd}`, isError: true };
    }
  } catch {
    return { text: `Error: cwd does not exist: ${baseCwd}`, isError: true };
  }

  const concurrency = clampInt(input.concurrency, 1, 8, 4);
  const defaultTimeoutSec = clampInt(input.timeoutSec, 1, MCP_CALL_BUDGET_SEC, 60);
  const outputMode = input.outputMode === "full" ? "full" : "compact";
  const maxChars = clampInt(input.maxChars, 500, DEFAULT_INLINE_MAX_BYTES, 8_000);
  const maxBytes = DEFAULT_INLINE_MAX_BYTES;

  const normalized: NormalizedCommand[] = [];
  for (let i = 0; i < rawCommands.length; i++) {
    const raw = rawCommands[i];
    const spec: CheckCommandSpec =
      typeof raw === "string" ? { command: raw } : (raw as unknown as CheckCommandSpec);
    if (!spec || typeof spec.command !== "string" || !spec.command.trim()) {
      return {
        text: `Error: commands[${i}] must contain a non-empty 'command' string.`,
        isError: true,
      };
    }
    if (spec.cwd !== undefined && spec.cwd !== null && typeof spec.cwd !== "string") {
      return {
        text: `Error: commands[${i}].cwd must be a string path.`,
        isError: true,
      };
    }
    const trimmedSpecCwd = spec.cwd?.trim() || "";
    const cmdCwd = trimmedSpecCwd ? resolvePath(trimmedSpecCwd, baseCwd) : baseCwd;
    if (trimmedSpecCwd) {
      try {
        const st = await fs.stat(cmdCwd);
        if (!st.isDirectory()) {
          return {
            text: `Error: commands[${i}].cwd is not a directory: ${cmdCwd}`,
            isError: true,
          };
        }
      } catch {
        return {
          text: `Error: commands[${i}].cwd does not exist: ${cmdCwd}`,
          isError: true,
        };
      }
    }
    const cmdTimeoutSec = clampInt(
      spec.timeoutSec,
      1,
      MCP_CALL_BUDGET_SEC,
      defaultTimeoutSec,
    );
    normalized.push({
      index: i,
      name: spec.name?.trim() || `check-${i + 1}`,
      command: spec.command.trim(),
      cwd: cmdCwd,
      timeoutMs: cmdTimeoutSec * 1000,
    });
  }

  const safeBudgetMs =
    Number.isFinite(budgetMs) && budgetMs > 0
      ? Math.max(100, Math.trunc(budgetMs))
      : MCP_CALL_BUDGET_SEC * 1000;
  const wallStart = performance.now();
  const deadline = wallStart + safeBudgetMs;
  const outcomes = await mapWithConcurrency<NormalizedCommand, CheckRunOutcome>(
    normalized,
    concurrency,
    async (spec) => {
      const remainingMs = deadline - performance.now();
      // exec-utils clamps timeouts to >=100ms plus kill grace; avoid starting doomed commands
      if (remainingMs < 100) return { spec, result: null, passed: false };
      const effTimeoutMs = Math.min(spec.timeoutMs, remainingMs);
      if (outputMode === "compact" && !input.noSqueeze) {
        const sq = await tryRunWithWmSqueeze(
          spec.command,
          spec.cwd,
          effTimeoutMs,
          Math.min(4000, maxChars),
        );
        if (sq) {
          const passed = !sq.result.error && !sq.result.timedOut && sq.result.exitCode === 0;
          return { spec, result: sq.result, passed, squeezed: true };
        }
      }
      const result = await runShellCommandAsync(spec.command, {
        cwd: spec.cwd,
        timeoutMs: effTimeoutMs,
      });
      const passed = !result.error && !result.timedOut && result.exitCode === 0;
      return { spec, result, passed, squeezed: false };
    },
  );
  const wallMs = Math.round(performance.now() - wallStart);

  const passedCount = outcomes.filter((o) => o.passed).length;
  const notRunCount = outcomes.filter((o) => !o.result).length;
  const failedCount = outcomes.length - passedCount - notRunCount;

  const counts = [`${passedCount}/${outcomes.length} passed`];
  if (failedCount > 0) counts.push(`${failedCount} failed`);
  if (notRunCount > 0) {
    counts.push(
      `${notRunCount} not run (call time budget of ${Math.round(safeBudgetMs / 1000)}s used up; run them in another call)`,
    );
  }
  const summaryHeader = `Checks: ${counts.join(", ")} (wall: ${wallMs}ms)`;

  // Allocate larger output share to failing checks than passing checks
  const usableChars = Math.max(200, maxChars - summaryHeader.length - 4);
  const failedBudget =
    failedCount > 0
      ? Math.max(
          160,
          Math.floor((usableChars - passedCount * 120 - notRunCount * 100) / failedCount),
        )
      : 320;
  const fullPerCmdBudget = Math.max(
    160,
    Math.floor(usableChars / outcomes.length),
  );

  const blocks = outcomes.map(({ spec, result, passed, squeezed }) => {
    if (!result) return `## [NOT RUN] ${spec.name}\n$ ${spec.command}`;
    const badge = result.error
      ? "ERROR"
      : result.timedOut
        ? "TIMEOUT"
        : passed
          ? "PASS"
          : `FAIL (exit ${result.exitCode ?? "err"})`;

    if (outputMode === "full") {
      const combined = [
        normalizeTerminalOutput(result.stdout),
        normalizeTerminalOutput(result.stderr),
      ]
        .filter(Boolean)
        .join("\n");
      const body = result.error
        ? `spawn error: ${result.error}`
        : truncateWithNotice(combined || "(no output)", fullPerCmdBudget);
      return `## [${badge}] ${spec.name} (${result.durationMs}ms)\n$ ${spec.command}\n${body}`;
    }

    if (squeezed) {
      const rawCombined = [result.stdout.trim(), result.stderr.trim()]
        .filter(Boolean)
        .join("\n");
      const sqTag = rawCombined.includes("cache-hit") ? "squeezed+cached" : "squeezed";
      if (passed) {
        const oneLine = rawCombined
          ? truncateWithNotice(rawCombined.replace(/\s*\n\s*/g, " | "), 280)
          : "ok";
        return `- [PASS ${result.durationMs}ms, ${sqTag}] ${spec.name} (\`${spec.command}\`): ${oneLine}`;
      }
      const failBody = truncateWithNotice(rawCombined || "(no output)", failedBudget);
      return `## [${badge} ${result.durationMs}ms, ${sqTag}] ${spec.name}\n$ ${spec.command}\n${failBody}`;
    }

    const body = formatCompactCommandOutput({
      stdout: result.stdout,
      stderr: result.stderr,
      exitCode: result.exitCode,
      timedOut: result.timedOut,
      error: result.error,
      maxChars: passed ? 300 : failedBudget,
    });

    if (passed) {
      return `- [PASS ${result.durationMs}ms] ${spec.name} (\`${spec.command}\`): ${body}`;
    }
    return `## [${badge} ${result.durationMs}ms] ${spec.name}\n$ ${spec.command}\n${body}`;
  });

  const bodyBudget = Math.max(0, maxChars - summaryHeader.length - 2);
  const bodyByteBudget =
    maxBytes !== undefined
      ? Math.max(0, maxBytes - Buffer.byteLength(summaryHeader, "utf8") - 2)
      : undefined;
  const fittedBlocks = fitSectionsToByteBudget(
    blocks,
    bodyBudget,
    bodyByteBudget,
    "\n\n",
  );
  let text = truncateWithNotice(
    `${summaryHeader}\n\n${fittedBlocks}`,
    maxChars,
  );
  if (maxBytes !== undefined) {
    text = truncateWithByteNotice(text, maxBytes);
  }
  return { text, isError: false };
}

function clampInt(val: unknown, min: number, max: number, fallback: number): number {
  if (typeof val !== "number" && typeof val !== "string") return fallback;
  if (typeof val === "string" && val.trim() === "") return fallback;
  const num = Number(val);
  if (!Number.isFinite(num)) return fallback;
  return Math.max(min, Math.min(max, Math.trunc(num)));
}
