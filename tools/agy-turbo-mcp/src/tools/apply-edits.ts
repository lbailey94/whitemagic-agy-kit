import { promises as fs } from "node:fs";
import path from "node:path";
import {
  formatCorruptedArrayError,
  isPlainObject,
  parseMaybeJsonArray,
  type CorruptedItem,
} from "../utils/arg-validation.js";
import { MCP_CALL_BUDGET_SEC, runShellCommandAsync } from "../utils/exec-utils.js";
import { readTextFile, resolvePath, toPosixPath } from "../utils/fs-utils.js";
import {
  DEFAULT_INLINE_MAX_BYTES,
  formatCompactCommandOutput,
  sliceSafeChars,
  truncateWithByteNotice,
} from "../utils/output-filter.js";
import { tryRunWithWmSqueeze } from "../utils/wm-squeeze-bridge.js";

export interface EditHunk {
  target: string;
  replacement: string;
  startLine?: number;
  endLine?: number;
  replaceAll?: boolean;
  strictWindow?: boolean;
}

export interface FileEdit {
  path: string;
  hunks?: EditHunk[];
  create?: string;
  overwrite?: boolean;
}

export interface VerifySpec {
  command: string;
  cwd?: string;
  timeoutSec?: number;
}

export interface ApplyEditsInput {
  cwd?: string;
  /** A JSON array string of file edits, or the array itself. */
  edits: string | FileEdit[];
  dryRun?: boolean;
  bestEffort?: boolean;
  strictWindow?: boolean;
  verify?: string | VerifySpec;
  verifyTimeoutSec?: number;
  maxVerifyChars?: number;
}

const DESTRUCTIVE_VERIFY_PATTERNS: Array<[RegExp, string]> = [
  [
    /(?:^|[;&|]\s*)rm\s+(?:-(?:[^\s]*r[^\s]*f|[^\s]*f[^\s]*r)[^\s]*|-[^\s]*r[^\s]*\s+-[^\s]*f[^\s]*|-[^\s]*f[^\s]*\s+-[^\s]*r[^\s]*)\s+(?:\/\s*$|\/\*|~|\$HOME(?:\s|$)|\/(?:home|Users)\/[^/\s]+(?:\s|$))/i,
    "Recursive force-delete targeting root or home directory",
  ],
  [/\bdrop\s+(?:table|database|schema|view)\b/i, "SQL DROP statement detected"],
  [/\btruncate\s+table\b/i, "SQL TRUNCATE TABLE detected"],
  [/\bgit\s+push\s+(?:--force|-f)\b.*\b(?:main|master)\b/i, "Force-push to main/master branch detected"],
  [/\bmkfs\.[a-z0-9]+\b|\bdd\s+if=.*\bof=\/dev\//i, "Raw disk/block device formatting or overwrite detected"],
];

interface MatchSpan {
  start: number;
  end: number;
  replacement: string;
  hunkIndex: number;
}

interface PlannedWrite {
  absPath: string;
  writeTargetPath: string;
  relPath: string;
  existed: boolean;
  originalBuffer: Buffer | null;
  originalMode: number | null;
  originalSymlinkTarget?: string | null;
  newContent: string;
  hunkCount: number;
  lineDelta: number;
  created: boolean;
  notes?: string[];
}

function computeLineStarts(text: string): number[] {
  const starts = [0];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === "\n") {
      starts.push(i + 1);
    }
  }
  return starts;
}

function offsetToLine(lineStarts: number[], offset: number): number {
  let lo = 0;
  let hi = lineStarts.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (lineStarts[mid]! <= offset) {
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return Math.max(1, hi + 1);
}

function getLineWindowOffsets(
  text: string,
  lineStarts: number[],
  startLine?: number,
  endLine?: number,
): { windowStart: number; windowEnd: number } {
  if (
    startLine !== undefined &&
    (typeof startLine !== "number" || !Number.isFinite(startLine) || startLine < 1)
  ) {
    throw new Error(`Invalid startLine: ${String(startLine)} (must be integer >= 1)`);
  }
  if (
    endLine !== undefined &&
    (typeof endLine !== "number" || !Number.isFinite(endLine) || endLine < 1)
  ) {
    throw new Error(`Invalid endLine: ${String(endLine)} (must be integer >= 1)`);
  }

  const totalLines = lineStarts.length;
  const sLine = startLine !== undefined ? Math.trunc(startLine) : 1;
  const eLine =
    endLine !== undefined ? Math.min(totalLines, Math.trunc(endLine)) : totalLines;

  if (sLine > totalLines) {
    return { windowStart: text.length, windowEnd: text.length };
  }
  if (eLine < sLine) {
    throw new Error(`Invalid line range: startLine (${sLine}) > endLine (${eLine})`);
  }

  const windowStart = lineStarts[sLine - 1]!;
  const windowEnd =
    eLine < totalLines ? lineStarts[eLine]! : text.length;
  return { windowStart, windowEnd };
}

const MAX_NEARBY_LINES = 26;

function formatNearbyLines(
  text: string,
  startLine?: number,
  endLine?: number,
): string {
  const lines = text.split("\n");
  if (startLine === undefined && endLine === undefined) return "";
  const from = Math.max(1, (startLine ?? endLine ?? 1) - 2);
  const rawTo = Math.min(lines.length, (endLine ?? startLine ?? 1) + 2);
  const truncated = rawTo - from + 1 > MAX_NEARBY_LINES;
  const to = truncated ? from + MAX_NEARBY_LINES - 1 : rawTo;
  const snippet: string[] = [];
  for (let ln = from; ln <= to; ln++) {
    const lineText = lines[ln - 1] ?? "";
    snippet.push(
      `    ${ln}: ${lineText.length > 160 ? `${sliceSafeChars(lineText, 157)}...` : lineText}`,
    );
  }
  const extra = truncated ? `\n    ... (${rawTo - to} more lines in range omitted)` : "";
  return `\n  Nearby lines (${from}-${to}):\n${snippet.join("\n")}${extra}`;
}

function extractLineTokens(trimmedLine: string): string[] {
  const tokens = trimmedLine.match(/[A-Za-z0-9_$]{3,}|[\u4e00-\u9fff]{2,}/g);
  if (tokens && tokens.length > 0) return tokens;
  return trimmedLine
    .split(/\s+/)
    .map((s) => s.trim())
    .filter((s) => s.length >= 2);
}

/**
 * Locate the closest matching region in the file (optionally bounded to a
 * 1-based line window) so the caller can fix a mismatched hunk immediately
 * without dumping an 80-line range or doing an extra read_file round-trip.
 */
function findClosestRegionSnippet(
  normalizedContent: string,
  normTarget: string,
  rangeStartLine?: number,
  rangeEndLine?: number,
): string {
  const fileLines = normalizedContent.split("\n");
  if (fileLines.length === 0) return "";

  const targetLines = normTarget
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  if (targetLines.length === 0) return "";

  const firstAnchor =
    targetLines.find((l) => l.length >= 6) ?? targetLines[0]!;
  let longestAnchor = targetLines[0]!;
  for (const tl of targetLines) {
    if (tl.length > longestAnchor.length) longestAnchor = tl;
  }
  const anchors = [firstAnchor];
  if (longestAnchor !== firstAnchor) anchors.push(longestAnchor);
  const lastAnchor = targetLines[targetLines.length - 1]!;
  if (lastAnchor.length >= 6 && !anchors.includes(lastAnchor)) {
    anchors.push(lastAnchor);
  }

  const primaryTokens = new Set(extractLineTokens(firstAnchor));
  const longestTokens = new Set(extractLineTokens(longestAnchor));
  const trimmedFileLines = fileLines.map((l) => l.trim());

  let bestIdx = -1;
  let bestScore = 0;

  const scanStart = Math.max(0, (rangeStartLine ?? 1) - 1);
  const scanEnd = Math.min(
    trimmedFileLines.length - 1,
    (rangeEndLine ?? trimmedFileLines.length) - 1,
  );

  for (let i = scanStart; i <= scanEnd; i++) {
    const line = trimmedFileLines[i]!;
    if (!line) continue;

    let score = 0;
    if (line === firstAnchor) {
      score += 10;
    } else if (firstAnchor.length >= 8 && (line.includes(firstAnchor) || firstAnchor.includes(line))) {
      score += 6;
    } else if (primaryTokens.size > 0) {
      const lineTokens = extractLineTokens(line);
      let hits = 0;
      for (const tok of lineTokens) {
        if (primaryTokens.has(tok)) hits += 1;
      }
      if (hits >= 2 || (hits >= 1 && primaryTokens.size <= 2 && hits === primaryTokens.size)) {
        score += 4 * (hits / primaryTokens.size);
      }
    }

    if (longestAnchor !== firstAnchor) {
      if (line === longestAnchor) {
        score += 8;
      } else if (longestAnchor.length >= 10 && line.includes(longestAnchor)) {
        score += 5;
      } else if (longestTokens.size >= 2) {
        const lineTokens = extractLineTokens(line);
        let hits = 0;
        for (const tok of lineTokens) {
          if (longestTokens.has(tok)) hits += 1;
        }
        if (hits >= 2) {
          score += 3 * (hits / longestTokens.size);
        }
      }
    }

    // Bonus when nearby subsequent lines also match subsequent target lines
    if (score >= 2 && targetLines.length > 1) {
      const lookahead = Math.min(targetLines.length, 6);
      for (let k = 1; k < lookahead && i + k < trimmedFileLines.length; k++) {
        const nextLine = trimmedFileLines[i + k]!;
        const tgtLine = targetLines[k]!;
        if (nextLine && tgtLine && (nextLine === tgtLine || (tgtLine.length >= 8 && nextLine.includes(tgtLine)))) {
          score += 3;
        }
      }
    }

    if (score > bestScore) {
      bestScore = score;
      bestIdx = i;
    }
  }

  if (bestIdx < 0 || bestScore < 2.5) return "";

  const span = Math.min(Math.max(targetLines.length, 4), 8);
  const from = Math.max(1, bestIdx + 1 - 2);
  const to = Math.min(fileLines.length, Math.min(from + 13, bestIdx + 1 + span + 2));
  const snippet: string[] = [];
  for (let ln = from; ln <= to; ln++) {
    const lineText = fileLines[ln - 1] ?? "";
    snippet.push(
      `    ${ln}: ${lineText.length > 160 ? `${sliceSafeChars(lineText, 157)}...` : lineText}`,
    );
  }
  return `\n  Closest match near line ${bestIdx + 1} (lines ${from}-${to}):\n${snippet.join("\n")}`;
}

function findTrailingWsUniqueMatch(
  normalizedContent: string,
  normTarget: string,
  windowStart: number,
  windowEnd: number,
): { start: number; end: number } | null {
  const targetLines = normTarget.split("\n");
  if (targetLines.length === 0) return null;
  const trimmedTargetLines = targetLines.map((l) => l.replace(/[ \t]+$/, ""));
  const contentLines = normalizedContent.split("\n");
  const lineStarts = computeLineStarts(normalizedContent);

  const matches: Array<{ start: number; end: number }> = [];
  for (let i = 0; i <= contentLines.length - trimmedTargetLines.length; i++) {
    const startOff = lineStarts[i]!;
    const endLineIdx = i + trimmedTargetLines.length - 1;
    const endOff = lineStarts[endLineIdx]! + contentLines[endLineIdx]!.length;
    if (startOff < windowStart || endOff > windowEnd) continue;

    let ok = true;
    for (let j = 0; j < trimmedTargetLines.length; j++) {
      if (contentLines[i + j]!.replace(/[ \t]+$/, "") !== trimmedTargetLines[j]) {
        ok = false;
        break;
      }
    }
    if (ok) {
      matches.push({ start: startOff, end: endOff });
      if (matches.length > 1) return null;
    }
  }
  return matches.length === 1 ? matches[0]! : null;
}

function planHunksForContent(
  normalizedContent: string,
  hunks: EditHunk[],
  displayPath: string,
  defaultStrictWindow = false,
): { newNormalized: string; appliedMatches: number; notes: string[]; errors: string[] } {
  const lineStarts = computeLineStarts(normalizedContent);
  const spans: MatchSpan[] = [];
  const notes: string[] = [];
  const errors: string[] = [];

  for (let idx = 0; idx < hunks.length; idx++) {
    const hunk = hunks[idx];
    if (!isPlainObject(hunk)) {
      errors.push(`${displayPath} hunk #${idx + 1}: must be an object with 'target' and 'replacement'`);
      continue;
    }
    if (typeof hunk.target !== "string" || hunk.target.length === 0) {
      errors.push(`${displayPath} hunk #${idx + 1}: 'target' must be a non-empty string`);
      continue;
    }
    if (typeof hunk.replacement !== "string") {
      errors.push(`${displayPath} hunk #${idx + 1}: 'replacement' must be a string`);
      continue;
    }

    const normTarget = hunk.target.replace(/\r\n/g, "\n");
    const normReplacement = hunk.replacement.replace(/\r\n/g, "\n");
    const strictWin = hunk.strictWindow ?? defaultStrictWindow;

    let windowStart = 0;
    let windowEnd = normalizedContent.length;
    try {
      const win = getLineWindowOffsets(
        normalizedContent,
        lineStarts,
        hunk.startLine,
        hunk.endLine,
      );
      windowStart = win.windowStart;
      windowEnd = win.windowEnd;
    } catch (err) {
      errors.push(
        `${displayPath} hunk #${idx + 1}: ${err instanceof Error ? err.message : String(err)}`,
      );
      continue;
    }

    const foundOffsets: number[] = [];
    let pos = windowStart;
    while (pos <= windowEnd - normTarget.length) {
      const found = normalizedContent.indexOf(normTarget, pos);
      if (found === -1 || found + normTarget.length > windowEnd) break;
      foundOffsets.push(found);
      pos = found + Math.max(1, normTarget.length);
    }

    if (foundOffsets.length === 0) {
      const hasRange = hunk.startLine !== undefined || hunk.endLine !== undefined;
      const rangeNote = hasRange
        ? ` within lines ${hunk.startLine ?? 1}-${hunk.endLine ?? lineStarts.length}`
        : "";

      // Check global exact matches when a line window was provided
      const globalOffsets: number[] = [];
      let gPos = 0;
      while (
        gPos <= normalizedContent.length - normTarget.length &&
        globalOffsets.length < 5
      ) {
        const found = normalizedContent.indexOf(normTarget, gPos);
        if (found === -1) break;
        globalOffsets.push(found);
        gPos = found + Math.max(1, normTarget.length);
      }

      if (hasRange && !strictWin && globalOffsets.length === 1) {
        const actualOff = globalOffsets[0]!;
        const actualLine = offsetToLine(lineStarts, actualOff);
        spans.push({
          start: actualOff,
          end: actualOff + normTarget.length,
          replacement: normReplacement,
          hunkIndex: idx,
        });
        notes.push(
          `hunk #${idx + 1} auto-relocated L${hunk.startLine ?? 1}-${hunk.endLine ?? lineStarts.length} -> L${actualLine}`,
        );
        continue;
      }

      // Safe trailing-whitespace unique fallback (only when 0 global exact hits & !strictWin)
      if (!strictWin && globalOffsets.length === 0) {
        const twMatch =
          findTrailingWsUniqueMatch(normalizedContent, normTarget, windowStart, windowEnd) ??
          (hasRange
            ? findTrailingWsUniqueMatch(normalizedContent, normTarget, 0, normalizedContent.length)
            : null);
        if (twMatch) {
          spans.push({
            start: twMatch.start,
            end: twMatch.end,
            replacement: normReplacement,
            hunkIndex: idx,
          });
          notes.push(`hunk #${idx + 1} trailing-ws normalized at L${offsetToLine(lineStarts, twMatch.start)}`);
          continue;
        }
      }

      let hint = "";
      if (hasRange) {
        if (globalOffsets.length > 0) {
          const hitLines = globalOffsets
            .map((o) => `L${offsetToLine(lineStarts, o)}`)
            .join(", ");
          hint = ` Exact match found outside range at ${hitLines} — adjust startLine/endLine or omit them.`;
        } else {
          const effStart = hunk.startLine ?? 1;
          const effEnd = hunk.endLine ?? lineStarts.length;
          if (effEnd - effStart + 1 > MAX_NEARBY_LINES - 4) {
            hint =
              findClosestRegionSnippet(normalizedContent, normTarget, effStart, effEnd) ||
              findClosestRegionSnippet(normalizedContent, normTarget) ||
              formatNearbyLines(normalizedContent, effStart, effEnd);
          } else {
            hint = formatNearbyLines(normalizedContent, hunk.startLine, hunk.endLine);
          }
        }
      } else {
        hint = findClosestRegionSnippet(normalizedContent, normTarget);
      }
      errors.push(
        `${displayPath} hunk #${idx + 1}: target text not found${rangeNote}.${hint}`,
      );
      continue;
    }

    if (foundOffsets.length > 1 && !hunk.replaceAll) {
      const lines = foundOffsets
        .slice(0, 10)
        .map((o) => offsetToLine(lineStarts, o))
        .join(", ");
      errors.push(
        `${displayPath} hunk #${idx + 1}: target matched ${foundOffsets.length} times (lines: ${lines}). Provide startLine/endLine, more surrounding context, or set replaceAll: true.`,
      );
      continue;
    }

    for (const start of foundOffsets) {
      spans.push({
        start,
        end: start + normTarget.length,
        replacement: normReplacement,
        hunkIndex: idx,
      });
    }
  }

  if (errors.length > 0) {
    return { newNormalized: normalizedContent, appliedMatches: 0, notes: [], errors };
  }

  spans.sort((a, b) => a.start - b.start || a.end - b.end);
  for (let i = 1; i < spans.length; i++) {
    const prev = spans[i - 1]!;
    const curr = spans[i]!;
    if (curr.start < prev.end) {
      const line = offsetToLine(lineStarts, curr.start);
      errors.push(
        `${displayPath}: hunk #${prev.hunkIndex + 1} and hunk #${curr.hunkIndex + 1} overlap near line ${line}.`,
      );
    }
  }

  if (errors.length > 0) {
    return { newNormalized: normalizedContent, appliedMatches: 0, notes: [], errors };
  }

  const pieces: string[] = [];
  let cursor = 0;
  for (const span of spans) {
    pieces.push(normalizedContent.slice(cursor, span.start));
    pieces.push(span.replacement);
    cursor = span.end;
  }
  pieces.push(normalizedContent.slice(cursor));

  return {
    newNormalized: pieces.join(""),
    appliedMatches: spans.length,
    notes,
    errors: [],
  };
}

function countLines(text: string): number {
  if (!text) return 0;
  const end = text.endsWith("\n") ? text.length - 1 : text.length;
  if (end === 0) return 1;
  let count = 1;
  for (let i = 0; i < end; i++) {
    if (text[i] === "\n") count++;
  }
  return count;
}

async function commitPlannedWrites(plans: PlannedWrite[]): Promise<void> {
  const written: PlannedWrite[] = [];

  try {
    for (const plan of plans) {
      const target = plan.writeTargetPath;
      const dir = path.dirname(target);
      await fs.mkdir(dir, { recursive: true });
      const tmpPath = `${target}.agy-fast-tmp-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      try {
        await fs.writeFile(tmpPath, plan.newContent, "utf8");
        if (plan.originalMode !== null && process.platform !== "win32") {
          await fs.chmod(tmpPath, plan.originalMode);
        }
      } catch (tmpErr) {
        try {
          await fs.unlink(tmpPath);
        } catch {
          // ignore cleanup failure
        }
        throw tmpErr;
      }

      try {
        await fs.rename(tmpPath, target);
        written.push(plan);
      } catch (renameErr) {
        // Fallback if rename across filesystems or directory permissions fails
        try {
          await fs.unlink(tmpPath);
        } catch {
          // ignore cleanup failure
        }
        written.push(plan);
        if (plan.originalSymlinkTarget) {
          // Replace dangling symlink itself instead of following it on fallback write
          try {
            await fs.unlink(target);
          } catch {
            // ignore unlink failure
          }
        }
        await fs.writeFile(target, plan.newContent, "utf8");
        if (plan.originalMode !== null && process.platform !== "win32") {
          try {
            await fs.chmod(target, plan.originalMode);
          } catch {
            // ignore chmod failure on unsupported filesystems
          }
        }
        void renameErr;
      }
    }
  } catch (err) {
    // Rollback all previously written files
    for (let i = written.length - 1; i >= 0; i--) {
      const item = written[i]!;
      const target = item.writeTargetPath;
      try {
        if (item.originalSymlinkTarget) {
          await fs.unlink(target).catch(() => {});
          await fs.symlink(item.originalSymlinkTarget, target);
        } else if (item.existed && item.originalBuffer !== null) {
          await fs.writeFile(target, item.originalBuffer);
          if (item.originalMode !== null && process.platform !== "win32") {
            await fs.chmod(target, item.originalMode);
          }
        } else if (!item.existed) {
          await fs.unlink(target);
        }
      } catch {
        // Best-effort rollback
      }
    }
    throw err;
  }
}

function corruptedEditsError(items: CorruptedItem[]): { text: string; isError: boolean } {
  return {
    text: formatCorruptedArrayError({
      header: "Validation failed (0 files modified)",
      toolName: "apply_edits",
      items,
      fallbackAdvice:
        "immediately fall back to parallel built-in edit_file / write_file calls for these file(s).",
    }),
    isError: true,
  };
}

export async function runApplyEdits(
  input: ApplyEditsInput,
): Promise<{ text: string; isError: boolean }> {
  const parsedEdits = parseMaybeJsonArray(input?.edits);
  if (!parsedEdits.ok) {
    if (
      parsedEdits.kind === "corrupted_literal" ||
      (parsedEdits.kind === "wrong_type" &&
        (typeof input?.edits === "boolean" || input?.edits === null))
    ) {
      return corruptedEditsError([
        {
          label: "edits",
          value: parsedEdits.kind === "corrupted_literal" ? parsedEdits.value : input.edits,
          expected: "an array of edit objects",
        },
      ]);
    }
    if (parsedEdits.kind === "invalid_json") {
      return {
        text: `Error: 'edits' is not a valid JSON array (0 files modified): ${parsedEdits.detail}`,
        isError: true,
      };
    }
    return {
      text: "Error: 'edits' must be a non-empty JSON array string (or array) of file edit objects.",
      isError: true,
    };
  }
  const rawEdits = parsedEdits.value;
  // A model never means to send zero edits; on agy CLI 1.5.x a long native
  // array of edit objects can reach the server as [] with the payload lost.
  if (rawEdits.length === 0) {
    return corruptedEditsError([
      { label: "edits", value: rawEdits, expected: "a non-empty array of edit objects" },
    ]);
  }
  if (rawEdits.length > 50) {
    return {
      text: "Error: 'edits' supports at most 50 file entries per call.",
      isError: true,
    };
  }

  // Detect nested-JSON serialization corruption (e.g. edits: [true], [false, true], [null], or hunks: [true])
  // before regular validation so the caller immediately falls back instead of retrying in a loop.
  const corruptedItems: CorruptedItem[] = [];
  const normalizedEdits: FileEdit[] = [];
  for (let i = 0; i < rawEdits.length; i++) {
    const rawEntry = rawEdits[i];
    if (!isPlainObject(rawEntry)) {
      corruptedItems.push({ label: `edits[${i}]`, value: rawEntry, expected: "an edit object" });
      continue;
    }
    let normalizedHunks: EditHunk[] | undefined;
    if (rawEntry.hunks !== undefined) {
      const parsedHunks = parseMaybeJsonArray(rawEntry.hunks);
      if (!parsedHunks.ok) {
        corruptedItems.push({
          label: `edits[${i}].hunks`,
          value: parsedHunks.kind === "corrupted_literal" ? parsedHunks.value : rawEntry.hunks,
          expected: "an array of hunk objects",
        });
        continue;
      }
      let hunkCorrupted = false;
      for (let j = 0; j < parsedHunks.value.length; j++) {
        if (!isPlainObject(parsedHunks.value[j])) {
          corruptedItems.push({
            label: `edits[${i}].hunks[${j}]`,
            value: parsedHunks.value[j],
            expected: "a hunk object",
          });
          hunkCorrupted = true;
        }
      }
      if (hunkCorrupted) continue;
      normalizedHunks = parsedHunks.value as unknown as EditHunk[];
    }
    normalizedEdits.push({
      path: rawEntry.path as string,
      hunks: normalizedHunks,
      create: rawEntry.create as string | undefined,
      overwrite: rawEntry.overwrite as boolean | undefined,
    });
  }
  if (corruptedItems.length > 0) {
    return corruptedEditsError(corruptedItems);
  }

  // Validate optional verify spec up front before touching any files on disk
  let validatedVerify: VerifySpec | null = null;
  if (input.verify !== undefined) {
    if (typeof input.verify === "string") {
      if (input.verify.trim()) {
        validatedVerify = { command: input.verify };
      }
    } else if (isPlainObject(input.verify)) {
      const cmd = input.verify.command;
      if (typeof cmd !== "string" || !cmd.trim()) {
        return {
          text: "Validation failed (0 files modified):\n- 'verify.command' must be a non-empty string.",
          isError: true,
        };
      }
      if (input.verify.cwd !== undefined && typeof input.verify.cwd !== "string") {
        return {
          text: "Validation failed (0 files modified):\n- 'verify.cwd' must be a string.",
          isError: true,
        };
      }
      const rawSpecTimeout = input.verify.timeoutSec as unknown;
      const parsedSpecTimeout =
        (typeof rawSpecTimeout === "number" ||
          (typeof rawSpecTimeout === "string" && rawSpecTimeout.trim() !== ""))
          ? Number(rawSpecTimeout)
          : undefined;
      validatedVerify = {
        command: cmd,
        cwd: input.verify.cwd,
        timeoutSec:
          parsedSpecTimeout !== undefined && Number.isFinite(parsedSpecTimeout)
            ? parsedSpecTimeout
            : undefined,
      };
    } else {
      return {
        text: "Validation failed (0 files modified):\n- 'verify' must be a command string or { command, cwd?, timeoutSec? } object.",
        isError: true,
      };
    }
  }

  const baseCwd = resolvePath(input.cwd || process.cwd());

  // Merge entries targeting the same resolved file (including symlink aliases and symlinked parent dirs)
  const mergedByPath = new Map<
    string,
    {
      absPath: string;
      writeTargetPath: string;
      relPath: string;
      create?: string;
      overwrite?: boolean;
      hunks: EditHunk[];
    }
  >();
  const errors: string[] = [];

  for (let i = 0; i < normalizedEdits.length; i++) {
    const entry = normalizedEdits[i]!;
    if (typeof entry.path !== "string" || !entry.path.trim()) {
      errors.push(`edits[${i}]: 'path' must be a non-empty string`);
      continue;
    }

    const absPath = resolvePath(entry.path, baseCwd);
    let writeTargetPath = absPath;
    try {
      // Canonicalize existing paths (including symlinks and symlinked parent dirs like /var -> /private/var)
      writeTargetPath = await fs.realpath(absPath);
    } catch {
      // File does not exist yet or dangling symlink — still canonicalize existing parent dir
      // so `sym_dir/new.ts` and `real_dir/new.ts` deduplicate cleanly.
      try {
        const realParent = await fs.realpath(path.dirname(absPath));
        writeTargetPath = path.join(realParent, path.basename(absPath));
      } catch {
        // Parent directory does not exist yet — keep absPath
      }
    }
    const relPath = toPosixPath(path.relative(baseCwd, absPath)) || path.basename(absPath);
    const hasCreate = typeof entry.create === "string";
    const hasHunks = Array.isArray(entry.hunks) && entry.hunks.length > 0;

    if (!hasCreate && !hasHunks) {
      errors.push(`${relPath}: must specify either non-empty 'hunks' or 'create'`);
      continue;
    }
    if (hasCreate && hasHunks) {
      errors.push(`${relPath}: cannot specify both 'create' and 'hunks' on the same entry`);
      continue;
    }

    const existing = mergedByPath.get(writeTargetPath);
    if (!existing) {
      mergedByPath.set(writeTargetPath, {
        absPath,
        writeTargetPath,
        relPath,
        create: hasCreate ? entry.create : undefined,
        overwrite: entry.overwrite,
        hunks: hasHunks ? [...entry.hunks!] : [],
      });
    } else {
      if (hasCreate || existing.create !== undefined) {
        errors.push(`${relPath}: duplicate entries cannot mix 'create' with other edits`);
        continue;
      }
      existing.hunks.push(...entry.hunks!);
    }
  }

  if (errors.length > 0) {
    return {
      text: `Validation failed (0 files modified):\n- ${errors.join("\n- ")}`,
      isError: true,
    };
  }

  // Phase 1: validate and plan every file in memory
  const plans: PlannedWrite[] = [];
  for (const item of mergedByPath.values()) {
    if (item.create !== undefined) {
      let exists = false;
      let originalBuffer: Buffer | null = null;
      let originalMode: number | null = null;
      let originalSymlinkTarget: string | null = null;
      try {
        const lst = await fs.lstat(item.writeTargetPath);
        if (lst.isSymbolicLink()) {
          try {
            const st = await fs.stat(item.writeTargetPath);
            if (st.isDirectory()) {
              errors.push(`${item.relPath}: target path is a directory`);
              continue;
            }
            exists = true;
            if (!item.overwrite) {
              errors.push(
                `${item.relPath}: file already exists (set overwrite: true to replace it)`,
              );
              continue;
            }
            originalMode = st.mode & 0o7777;
            originalBuffer = await fs.readFile(item.writeTargetPath);
          } catch (statErr) {
            if ((statErr as NodeJS.ErrnoException)?.code === "ENOENT") {
              // Dangling symlink: never clobber without overwrite: true, and preserve target for rollback
              exists = true;
              if (!item.overwrite) {
                errors.push(
                  `${item.relPath}: dangling symlink already exists (set overwrite: true to replace it)`,
                );
                continue;
              }
              originalSymlinkTarget = await fs.readlink(item.writeTargetPath);
            } else {
              errors.push(
                `${item.relPath}: ${statErr instanceof Error ? statErr.message : String(statErr)}`,
              );
              continue;
            }
          }
        } else {
          const st = await fs.stat(item.writeTargetPath);
          if (st.isDirectory()) {
            errors.push(`${item.relPath}: target path is a directory`);
            continue;
          }
          exists = true;
          if (!item.overwrite) {
            errors.push(
              `${item.relPath}: file already exists (set overwrite: true to replace it)`,
            );
            continue;
          }
          originalMode = st.mode & 0o7777;
          originalBuffer = await fs.readFile(item.writeTargetPath);
        }
      } catch (err) {
        if ((err as NodeJS.ErrnoException)?.code === "ENOENT") {
          exists = false;
          originalBuffer = null;
          originalMode = null;
        } else {
          errors.push(
            `${item.relPath}: ${err instanceof Error ? err.message : String(err)}`,
          );
          continue;
        }
      }

      const oldLines = originalBuffer ? countLines(originalBuffer.toString("utf8")) : 0;
      const newLines = countLines(item.create);
      plans.push({
        absPath: item.absPath,
        writeTargetPath: item.writeTargetPath,
        relPath: item.relPath,
        existed: exists,
        originalBuffer,
        originalMode,
        originalSymlinkTarget,
        newContent: item.create,
        hunkCount: 1,
        lineDelta: newLines - oldLines,
        created: !exists,
      });
      continue;
    }

    // Existing file hunks
    let fileData;
    try {
      fileData = await readTextFile(item.writeTargetPath);
    } catch (err) {
      errors.push(
        `${item.relPath}: ${err instanceof Error ? err.message : String(err)}`,
      );
      continue;
    }

    const normalizedOriginal = fileData.content.replace(/\r\n/g, "\n");
    const planned = planHunksForContent(
      normalizedOriginal,
      item.hunks,
      item.relPath,
      Boolean(input.strictWindow),
    );
    if (planned.errors.length > 0) {
      errors.push(...planned.errors);
      continue;
    }

    const finalContent =
      fileData.eol === "\r\n"
        ? planned.newNormalized.replace(/\n/g, "\r\n")
        : planned.newNormalized;

    const lineDelta =
      countLines(planned.newNormalized) - countLines(normalizedOriginal);

    plans.push({
      absPath: item.absPath,
      writeTargetPath: item.writeTargetPath,
      relPath: item.relPath,
      existed: true,
      originalBuffer: fileData.rawBuffer,
      originalMode: fileData.mode,
      newContent: finalContent,
      hunkCount: planned.appliedMatches,
      lineDelta,
      created: false,
      notes: planned.notes,
    });
  }

  const bestEffort = Boolean(input.bestEffort);

  if (errors.length > 0 && (!bestEffort || plans.length === 0)) {
    const prefix = bestEffort
      ? "Edit aborted (0 files modified):"
      : "Atomic edit aborted (0 files modified):";
    return {
      text: truncateWithByteNotice(
        `${prefix}\n- ${errors.join("\n- ")}`,
        DEFAULT_INLINE_MAX_BYTES,
      ),
      isError: true,
    };
  }

  // Phase 2: write valid planned files (unless dryRun)
  if (!input.dryRun) {
    try {
      await commitPlannedWrites(plans);
    } catch (err) {
      return {
        text: `Write failed (changes rolled back): ${err instanceof Error ? err.message : String(err)}`,
        isError: true,
      };
    }
  }

  const summaryLines = plans.map((p) => {
    const deltaStr = p.lineDelta >= 0 ? `+${p.lineDelta}` : `${p.lineDelta}`;
    const noteStr = p.notes && p.notes.length > 0 ? `; ${p.notes.join("; ")}` : "";
    const action = p.created
      ? "created"
      : `updated (${p.hunkCount} match${p.hunkCount === 1 ? "" : "es"}${noteStr})`;
    return `- ${p.relPath}: ${action}, lines ${deltaStr}`;
  });

  if (bestEffort && errors.length > 0) {
    const totalFiles = mergedByPath.size;
    const failedFileCount = Math.max(1, totalFiles - plans.length);
    const partialHeader = input.dryRun
      ? `Partial dry-run (${plans.length}/${totalFiles} file(s) valid; ${failedFileCount} file(s) failed):`
      : `Partial edit applied (${plans.length}/${totalFiles} file(s) written; ${failedFileCount} file(s) failed — do NOT re-send applied files):`;
    const partialParts = [
      partialHeader,
      `Applied:\n${summaryLines.join("\n")}`,
      `Failed (fix only these):\n- ${errors.join("\n- ")}`,
    ];
    if (!input.dryRun && input.verify) {
      partialParts.push(
        "Verify skipped because failed file(s) remain; fix only the failed file(s) and re-run verify.",
      );
    }
    return {
      text: truncateWithByteNotice(
        partialParts.join("\n\n"),
        DEFAULT_INLINE_MAX_BYTES,
      ),
      isError: true,
    };
  }

  const header = input.dryRun
    ? `Dry run validated ${plans.length} file(s) (0 files modified):`
    : `Applied edits to ${plans.length} file(s):`;
  const parts = [`${header}\n${summaryLines.join("\n")}`];

  // Phase 3: optional inline verify
  if (!input.dryRun && validatedVerify) {
    const verifySpec = validatedVerify;
    const verifyCwd = verifySpec.cwd ? resolvePath(verifySpec.cwd, baseCwd) : baseCwd;
    const timeoutCandidate: unknown = verifySpec.timeoutSec ?? input.verifyTimeoutSec;
    const rawTimeoutSec =
      (typeof timeoutCandidate === "number" ||
        (typeof timeoutCandidate === "string" && timeoutCandidate.trim() !== ""))
        ? Number(timeoutCandidate)
        : 60;
    const timeoutMs =
      (Number.isFinite(rawTimeoutSec)
        ? Math.max(1, Math.min(MCP_CALL_BUDGET_SEC, Math.trunc(rawTimeoutSec)))
        : 60) * 1000;
    const rawMaxVerifyChars = Number(input.maxVerifyChars ?? 4000);
    const maxVerifyChars = Number.isFinite(rawMaxVerifyChars)
      ? Math.max(300, Math.min(7_500, Math.trunc(rawMaxVerifyChars)))
      : 4000;

    for (const [pat, reason] of DESTRUCTIVE_VERIFY_PATTERNS) {
      if (pat.test(verifySpec.command)) {
        parts.push(`\nVerify [BLOCKED]: ${reason} in verify command \`${verifySpec.command}\`.`);
        return {
          text: truncateWithByteNotice(parts.join("\n"), DEFAULT_INLINE_MAX_BYTES),
          isError: true,
        };
      }
    }

    const sq = await tryRunWithWmSqueeze(
      verifySpec.command,
      verifyCwd,
      timeoutMs,
      maxVerifyChars,
    );
    if (sq) {
      const res = sq.result;
      const status = res.timedOut
        ? "TIMEOUT"
        : res.exitCode === 0
          ? "PASS"
          : `FAIL (exit ${res.exitCode ?? "err"})`;
      const combined = [res.stdout.trim(), res.stderr.trim()].filter(Boolean).join("\n");
      const sqTag = combined.includes("cache-hit") ? "squeezed+cached" : "squeezed";
      const formatted = sliceSafeChars(combined || "(no output)", maxVerifyChars);
      parts.push(
        `\nVerify [${status}, ${sqTag} in ${res.durationMs}ms] \`${verifySpec.command}\`:\n${formatted}`,
      );
    } else {
      const res = await runShellCommandAsync(verifySpec.command, {
        cwd: verifyCwd,
        timeoutMs,
      });

      const status = res.timedOut
        ? "TIMEOUT"
        : res.exitCode === 0
          ? "PASS"
          : `FAIL (exit ${res.exitCode ?? "err"})`;
      const formatted = formatCompactCommandOutput({
        stdout: res.stdout,
        stderr: res.stderr,
        exitCode: res.exitCode,
        timedOut: res.timedOut,
        error: res.error,
        maxChars: maxVerifyChars,
      });

      parts.push(
        `\nVerify [${status} in ${res.durationMs}ms] \`${verifySpec.command}\`:\n${formatted}`,
      );
    }
  }

  const outText = parts.join("\n");
  const finalText = truncateWithByteNotice(outText, DEFAULT_INLINE_MAX_BYTES);
  return { text: finalText, isError: false };
}
