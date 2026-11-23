import fsSync from "node:fs";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { execBinaryAsync } from "../utils/exec-utils.js";
import { resolvePath } from "../utils/fs-utils.js";
import { DEFAULT_INLINE_MAX_BYTES, truncateWithByteNotice } from "../utils/output-filter.js";

export type CodeGraphMode = "overview" | "defs" | "callers" | "callees" | "impact" | "skeleton";

const VALID_MODES = new Set<string>(["overview", "defs", "callers", "callees", "impact", "skeleton"]);

export interface CodeGraphInput {
  mode: CodeGraphMode;
  target?: string;
  root?: string;
  cwd?: string;
  depth?: number;
  maxChars?: number;
}

export function findWmCodegraphBin(env: NodeJS.ProcessEnv = process.env): string | null {
  if (env.WM_CODEGRAPH_BIN !== undefined) {
    const explicit = env.WM_CODEGRAPH_BIN.trim();
    if (!explicit) return null;
    try {
      fsSync.accessSync(explicit, fsSync.constants.X_OK);
      return explicit;
    } catch {
      return null;
    }
  }
  const home = env.HOME || env.USERPROFILE || os.homedir();
  const candidates = [path.join(home, ".local", "bin", "wm-codegraph")];
  for (const dir of (env.PATH || "").split(path.delimiter)) {
    if (dir) candidates.push(path.join(dir, "wm-codegraph"));
  }
  for (const cand of candidates) {
    try {
      if (fsSync.statSync(cand).isFile()) {
        fsSync.accessSync(cand, fsSync.constants.X_OK);
        return cand;
      }
    } catch {
      // continue
    }
  }
  return null;
}

export async function runCodeGraph(
  input: CodeGraphInput,
): Promise<{ text: string; isError: boolean }> {
  const mode = typeof input?.mode === "string" ? input.mode.trim() : "";
  if (!VALID_MODES.has(mode)) {
    return {
      text: `Error: 'mode' must be one of overview, defs, callers, callees, impact, skeleton (got ${JSON.stringify(input?.mode)}).`,
      isError: true,
    };
  }

  const bin = findWmCodegraphBin();
  if (!bin) {
    return {
      text: "Error: 'wm-codegraph' binary not found in ~/.local/bin or PATH. Run whitemagic-agy-kit install.sh to build it.",
      isError: true,
    };
  }

  const baseDir = resolvePath(input.cwd || process.cwd());
  const rootRaw = typeof input.root === "string" && input.root.trim() ? input.root.trim() : baseDir;
  const resolvedRoot = resolvePath(rootRaw, baseDir);
  try {
    const st = await fs.stat(resolvedRoot);
    if (!st.isDirectory()) {
      return { text: `Error: root is not a directory: ${resolvedRoot}`, isError: true };
    }
  } catch {
    return { text: `Error: root directory does not exist: ${resolvedRoot}`, isError: true };
  }

  const target = typeof input.target === "string" ? input.target.trim() : "";
  const rawDepth = Number(input.depth ?? 2);
  const depth = Number.isFinite(rawDepth) ? Math.max(1, Math.min(3, Math.trunc(rawDepth))) : 2;
  const rawMaxChars = Number(input.maxChars ?? 8500);
  const maxBytes = Number.isFinite(rawMaxChars)
    ? Math.max(500, Math.min(DEFAULT_INLINE_MAX_BYTES, Math.trunc(rawMaxChars)))
    : 8500;

  const args: string[] = [mode];
  if (target) args.push(target);
  args.push("--root", resolvedRoot, "--depth", String(depth));

  const res = await execBinaryAsync(bin, args, {
    cwd: resolvedRoot,
    timeoutMs: 15_000,
    maxOutputBytes: 512 * 1024,
  });

  if (res.error) {
    return { text: `Error running wm-codegraph: ${res.error}`, isError: true };
  }
  if (res.timedOut) {
    return { text: `Error: wm-codegraph timed out after 15s on ${resolvedRoot}`, isError: true };
  }
  if (res.exitCode !== 0) {
    return {
      text: `Error: wm-codegraph exited with code ${res.exitCode}: ${(res.stderr || res.stdout).trim()}`,
      isError: true,
    };
  }

  const text = truncateWithByteNotice(res.stdout.trimEnd() || "(no output)", maxBytes);
  return { text, isError: false };
}
