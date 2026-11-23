import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

export const DEFAULT_IGNORE_DIRS = new Set([
  ".git",
  ".hg",
  ".svn",
  "node_modules",
  "dist",
  "build",
  "out",
  "target",
  ".next",
  ".nuxt",
  ".svelte-kit",
  ".cache",
  ".turbo",
  "coverage",
  "__pycache__",
  ".venv",
  "venv",
  ".tox",
  ".pytest_cache",
  ".mypy_cache",
  ".ruff_cache",
  ".gradle",
  ".idea",
  "vendor",
]);

export function expandHome(inputPath: string): string {
  if (inputPath === "~") return os.homedir();
  if (inputPath.startsWith("~/") || inputPath.startsWith("~\\")) {
    return path.join(os.homedir(), inputPath.slice(2));
  }
  return inputPath;
}

export function resolvePath(inputPath: string, baseDir?: string): string {
  const expanded = expandHome(inputPath.trim());
  const root = baseDir ? path.resolve(expandHome(baseDir)) : process.cwd();
  return path.isAbsolute(expanded) ? path.normalize(expanded) : path.resolve(root, expanded);
}

export function toPosixPath(p: string): string {
  return p.split(path.sep).join("/");
}

export function isLikelyBinary(buf: Buffer): boolean {
  const limit = Math.min(buf.length, 8192);
  for (let i = 0; i < limit; i++) {
    if (buf[i] === 0) return true;
  }
  return false;
}

export async function readTextFile(
  filePath: string,
  maxBytes = 10 * 1024 * 1024,
): Promise<{
  content: string;
  eol: "\r\n" | "\n";
  size: number;
  mode: number;
  rawBuffer: Buffer;
}> {
  const stat = await fs.stat(filePath);
  if (!stat.isFile()) {
    throw new Error(`Not a regular file: ${filePath}`);
  }
  if (stat.size > maxBytes) {
    throw new Error(
      `File exceeds maximum size (${stat.size} bytes > ${maxBytes} bytes): ${filePath}`,
    );
  }
  const buf = await fs.readFile(filePath);
  if (isLikelyBinary(buf)) {
    throw new Error(`Binary file cannot be read as text: ${filePath}`);
  }
  const content = buf.toString("utf8");
  let crlfCount = 0;
  let lfOnlyCount = 0;
  for (let i = 0; i < content.length; i++) {
    if (content[i] === "\n") {
      if (i > 0 && content[i - 1] === "\r") crlfCount++;
      else lfOnlyCount++;
    }
  }
  const eol: "\r\n" | "\n" = crlfCount > lfOnlyCount ? "\r\n" : "\n";
  return { content, eol, size: stat.size, mode: stat.mode & 0o7777, rawBuffer: buf };
}

/**
 * Convert a glob pattern (supporting **, *, ?, {a,b}, [abc]) to a RegExp
 * matching POSIX-normalized relative paths.
 */
export function globToRegExp(pattern: string): RegExp {
  const normalized = pattern.trim().replace(/\\/g, "/").replace(/^\.\//, "");
  let re = "^";
  let i = 0;

  while (i < normalized.length) {
    const ch = normalized[i]!;
    if (ch === "*") {
      if (normalized[i + 1] === "*") {
        if (normalized[i + 2] === "/") {
          re += "(?:.*/)?";
          i += 3;
        } else {
          re += ".*";
          i += 2;
        }
      } else {
        re += "[^/]*";
        i += 1;
      }
    } else if (ch === "?") {
      re += "[^/]";
      i += 1;
    } else if (ch === "[") {
      const close = normalized.indexOf("]", i + 1);
      if (close === -1) {
        re += "\\[";
        i += 1;
      } else {
        let inner = normalized.slice(i + 1, close).replace(/\\/g, "\\\\");
        if (inner.startsWith("!")) inner = "^" + inner.slice(1);
        re += `[${inner}]`;
        i = close + 1;
      }
    } else if (ch === "{") {
      const close = findMatchingBrace(normalized, i);
      if (close === -1) {
        re += "\\{";
        i += 1;
      } else {
        const parts = splitBraceAlternatives(normalized.slice(i + 1, close));
        const sub = parts
          .map((part) => globToRegExp(part).source.slice(1, -1))
          .join("|");
        re += `(?:${sub})`;
        i = close + 1;
      }
    } else if (".+^$()|\\".includes(ch)) {
      re += "\\" + ch;
      i += 1;
    } else {
      re += ch;
      i += 1;
    }
  }

  re += "$";
  try {
    return new RegExp(re);
  } catch (err) {
    throw new Error(
      `Invalid glob pattern '${pattern}': ${err instanceof Error ? err.message : String(err)}`,
    );
  }
}

function findMatchingBrace(str: string, openIdx: number): number {
  let depth = 0;
  for (let i = openIdx; i < str.length; i++) {
    if (str[i] === "{") depth++;
    else if (str[i] === "}") {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

function splitBraceAlternatives(inner: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < inner.length; i++) {
    const ch = inner[i];
    if (ch === "{") depth++;
    else if (ch === "}") depth--;
    else if (ch === "," && depth === 0) {
      parts.push(inner.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(inner.slice(start));
  return parts;
}

export interface WalkOptions {
  maxFiles?: number;
  maxDepth?: number;
  includeHidden?: boolean;
  ignoreDirs?: Set<string>;
  fileFilter?: (relPosixPath: string) => boolean;
}

export interface WalkResult {
  files: string[];
  truncated: boolean;
}

export async function walkFiles(
  rootDir: string,
  options: WalkOptions = {},
): Promise<WalkResult> {
  const maxFiles = options.maxFiles ?? 5000;
  const maxDepth = options.maxDepth ?? 32;
  const includeHidden = options.includeHidden ?? false;
  const ignoreDirs = options.ignoreDirs ?? DEFAULT_IGNORE_DIRS;
  const fileFilter = options.fileFilter;

  const files: string[] = [];
  let truncated = false;

  async function visit(dir: string, relDir: string, depth: number): Promise<void> {
    if (truncated || depth > maxDepth) return;

    let entries;
    try {
      entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      return;
    }

    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));

    const subdirs: Array<{ abs: string; rel: string }> = [];

    for (const entry of entries) {
      if (truncated) return;
      const name = entry.name;
      if (!includeHidden && name.startsWith(".")) {
        if (!(entry.isDirectory() && name === ".agents")) {
          continue;
        }
      }

      const absPath = path.join(dir, name);
      const relPosix = relDir ? `${relDir}/${name}` : name;

      if (entry.isDirectory()) {
        if (ignoreDirs.has(name)) continue;
        subdirs.push({ abs: absPath, rel: relPosix });
      } else if (entry.isFile()) {
        if (fileFilter && !fileFilter(relPosix)) continue;
        files.push(relPosix);
        if (files.length >= maxFiles) {
          truncated = true;
          return;
        }
      }
    }

    // Visit subdirectories in bounded batches for speed without exhausting FDs
    const batchSize = 8;
    for (let i = 0; i < subdirs.length && !truncated; i += batchSize) {
      const slice = subdirs.slice(i, i + batchSize);
      await Promise.all(slice.map((sub) => visit(sub.abs, sub.rel, depth + 1)));
    }
  }

  await visit(rootDir, "", 0);
  return { files, truncated };
}
