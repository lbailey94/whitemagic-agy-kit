import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execBinaryAsync, type ExecResult } from "./exec-utils.js";

let cachedWmSqueezeBin: string | null | undefined;

export function resetWmSqueezeCache(): void {
  cachedWmSqueezeBin = undefined;
}

export function findWmSqueezeBin(env: NodeJS.ProcessEnv = process.env): string | null {
  if (env.WM_SQUEEZE_BIN !== undefined) {
    const explicit = env.WM_SQUEEZE_BIN.trim();
    if (!explicit) return null;
    try {
      fs.accessSync(explicit, fs.constants.X_OK);
      return explicit;
    } catch {
      return null;
    }
  }
  if (cachedWmSqueezeBin !== undefined) return cachedWmSqueezeBin;

  const home = env.HOME || env.USERPROFILE || os.homedir();
  const candidates = [path.join(home, ".local", "bin", "wm-squeeze")];
  const pathEnv = env.PATH || "";
  for (const dir of pathEnv.split(path.delimiter)) {
    if (dir) candidates.push(path.join(dir, "wm-squeeze"));
  }
  for (const cand of candidates) {
    try {
      if (fs.statSync(cand).isFile()) {
        fs.accessSync(cand, fs.constants.X_OK);
        cachedWmSqueezeBin = cand;
        return cand;
      }
    } catch {
      // keep searching
    }
  }
  cachedWmSqueezeBin = null;
  return null;
}

const SHELL_META_RE = /[|><;`\n$*?\[\]{}]|(?<!&)&(?!&)|(?:^|\s)~/;
const SAFE_ENV_KEYS = new Set([
  "PYTHONPATH",
  "RUST_BACKTRACE",
  "RUST_LOG",
  "NO_COLOR",
  "CARGO_TERM_COLOR",
]);

function splitSimpleArgs(raw: string): string[] | null {
  const tokens: string[] = [];
  let cur = "";
  let quote: "'" | '"' | null = null;
  let hasToken = false;

  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i]!;
    if (quote === "'") {
      if (ch === "'") {
        quote = null;
      } else {
        cur += ch;
      }
      continue;
    }
    if (quote === '"') {
      if (ch === "\\" && i + 1 < raw.length) {
        const nxt = raw[i + 1]!;
        if (nxt === '"' || nxt === "\\" || nxt === "$" || nxt === "`") {
          cur += nxt;
          i++;
          continue;
        }
      }
      if (ch === '"') {
        quote = null;
      } else {
        cur += ch;
      }
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      hasToken = true;
      continue;
    }
    if (ch === "\\" && i + 1 < raw.length) {
      cur += raw[i + 1]!;
      hasToken = true;
      i++;
      continue;
    }
    if (/\s/.test(ch)) {
      if (hasToken) {
        tokens.push(cur);
        cur = "";
        hasToken = false;
      }
      continue;
    }
    cur += ch;
    hasToken = true;
  }

  if (quote !== null) return null;
  if (hasToken) tokens.push(cur);
  return tokens;
}

export function isSafeSqueezableArgs(cmdBase: string, cmdArgs: string[]): boolean {
  if (cmdBase === "git") {
    let i = 0;
    while (i < cmdArgs.length) {
      const a = cmdArgs[i]!;
      if (
        a === "-c" ||
        a === "--paginate" ||
        a.startsWith("-c") ||
        a.startsWith("--exec-path") ||
        a.startsWith("--config-env") ||
        a.startsWith("--upload-pack")
      ) {
        return false;
      }
      if (a === "-C") {
        i += 2;
        continue;
      }
      if (a.startsWith("-")) {
        i += 1;
        continue;
      }
      const sub = a;
      const nxt = cmdArgs[i + 1] ?? "";
      if (
        ["status", "diff", "log", "show", "ls-files", "rev-parse", "blame", "shortlog", "describe"].includes(sub)
      ) {
        return true;
      }
      if (sub === "branch" && !cmdArgs.slice(i + 1).some((x) => ["-d", "-D", "-m", "-M", "--delete"].includes(x))) {
        return true;
      }
      if (sub === "tag" && !cmdArgs.slice(i + 1).some((x) => x === "-d" || x === "--delete")) {
        return true;
      }
      if (sub === "stash" && nxt === "list") return true;
      if (sub === "worktree" && nxt === "list") return true;
      if (sub === "remote" && (nxt === "-v" || nxt === "")) return true;
      return false;
    }
    return false;
  }

  if (cmdBase === "cargo") {
    if (cmdArgs.some((a) => a === "--config" || a.startsWith("--config="))) return false;
    const sub = cmdArgs.find((a) => !a.startsWith("-") && !a.startsWith("+")) ?? "";
    return ["check", "test", "clippy", "build", "tree", "metadata"].includes(sub);
  }

  if (cmdBase === "go") {
    const sub = cmdArgs[0] ?? "";
    return ["test", "vet", "list", "build"].includes(sub);
  }

  if (cmdBase === "pytest") return true;

  if (cmdBase === "python3" || cmdBase === "python") {
    return cmdArgs[0] === "-m" && ["pytest", "unittest", "mypy", "ruff"].includes(cmdArgs[1] ?? "");
  }

  if (cmdBase === "npm" || cmdBase === "pnpm" || cmdBase === "yarn") {
    const s0 = cmdArgs[0] ?? "";
    const s1 = cmdArgs[1] ?? "";
    return (
      ["test", "ls", "outdated"].includes(s0) ||
      (s0 === "run" && ["typecheck", "lint", "build", "test", "check"].includes(s1))
    );
  }

  if (cmdBase === "rg") {
    return !cmdArgs.some((a) => a === "--pre" || a.startsWith("--pre="));
  }

  if (cmdBase === "grep") return true;

  if (cmdBase === "find") {
    const bad = new Set(["-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls", "-fprint", "-fprint0", "-fprintf"]);
    return !cmdArgs.some((a) => bad.has(a));
  }

  if (["ls", "tree", "wc", "du", "df", "stat", "file", "cat", "head", "tail", "nl", "ruff", "mypy", "eslint", "tsc"].includes(cmdBase)) {
    return true;
  }

  if (cmdBase === "wm3" || cmdBase === "wm-gen3") {
    let sub = "";
    for (let i = 0; i < cmdArgs.length; i++) {
      const a = cmdArgs[i]!;
      if (a === "--store" || a === "--galaxy") {
        i++;
        continue;
      }
      if (a.startsWith("-")) continue;
      sub = a;
      break;
    }
    return (
      ["recall", "evidence-recall", "stats", "status", "session", "list", "build-info", "inspect"].includes(sub) ||
      cmdArgs.includes("--version")
    );
  }

  if (cmdBase === "gh") {
    const s0 = cmdArgs[0] ?? "";
    const s1 = cmdArgs[1] ?? "";
    return (
      ["pr", "issue", "repo", "run", "workflow", "release"].includes(s0) &&
      ["list", "view", "status", "diff", "checks"].includes(s1)
    );
  }

  return false;
}

export interface ParsedSqueezePlan {
  cwd?: string;
  envVars: string[];
  cmdArgs: string[];
}

export function parseSafeSqueezeCommand(rawCmd: string, baseCwd: string): ParsedSqueezePlan | null {
  let s = (rawCmd || "").trim();
  if (!s || s.includes("wm-squeeze")) return null;
  // Strip harmless trailing stderr-to-stdout redirection (wm-squeeze already merges stdout+stderr)
  s = s.replace(/\s+2>&1\s*$/, "").trim();
  if (!s) return null;

  let cwdOverride: string | undefined;
  let toks: string[] | null = null;

  if (s.includes("&&")) {
    const parts = s.split("&&");
    if (parts.length !== 2) return null;
    const left = parts[0]!.trim();
    const right = parts[1]!.trim();
    if (SHELL_META_RE.test(left) || SHELL_META_RE.test(right)) return null;
    const ltoks = splitSimpleArgs(left);
    const rtoks = splitSimpleArgs(right);
    if (!ltoks || !rtoks || ltoks.length !== 2 || ltoks[0] !== "cd" || rtoks.length === 0) {
      return null;
    }
    cwdOverride = path.resolve(baseCwd, ltoks[1]!);
    toks = rtoks;
  } else {
    if (SHELL_META_RE.test(s)) return null;
    toks = splitSimpleArgs(s);
    if (!toks || toks.length === 0) return null;
  }

  const envVars: string[] = [];
  while (toks.length > 0 && toks[0]!.includes("=")) {
    const eq = toks[0]!.indexOf("=");
    const key = toks[0]!.slice(0, eq);
    if (!SAFE_ENV_KEYS.has(key)) return null;
    envVars.push(toks.shift()!);
  }
  if (toks.length === 0) return null;

  const cmdBase = path.basename(toks[0]!);
  if (!isSafeSqueezableArgs(cmdBase, toks.slice(1))) return null;

  return {
    cwd: cwdOverride,
    envVars,
    cmdArgs: toks,
  };
}

export async function tryRunWithWmSqueeze(
  rawCmd: string,
  cwd: string,
  timeoutMs: number,
  maxBytes: number,
  env: NodeJS.ProcessEnv = process.env,
): Promise<{ squeezed: true; result: ExecResult } | null> {
  if (env.WM_SQUEEZE_OFF === "1") return null;
  const sqBin = findWmSqueezeBin(env);
  if (!sqBin) return null;

  const plan = parseSafeSqueezeCommand(rawCmd, cwd);
  if (!plan) return null;

  const clampedMaxBytes = Math.max(512, Math.min(8200, Math.trunc(maxBytes)));
  const args: string[] = ["--safe", "--cwd", plan.cwd ?? cwd, "--max-bytes", String(clampedMaxBytes)];
  for (const ev of plan.envVars) {
    args.push("--env", ev);
  }
  args.push("--", ...plan.cmdArgs);

  const res = await execBinaryAsync(sqBin, args, {
    cwd: plan.cwd ?? cwd,
    timeoutMs,
    maxOutputBytes: 256 * 1024,
    env,
  });

  if (res.error) return null;
  if (res.exitCode === 2 && res.stderr.includes("wm-squeeze --safe refused:")) {
    return null;
  }
  return { squeezed: true, result: res };
}
