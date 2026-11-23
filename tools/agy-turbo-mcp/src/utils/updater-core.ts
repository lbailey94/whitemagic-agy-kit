import { spawn } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { execBinaryAsync } from "./exec-utils.js";

export const DEFAULT_REPO_URL = "https://github.com/INK-Q/agy-fast.git";
export const DEFAULT_GITHUB_SLUG = "INK-Q/agy-fast";
export const DEFAULT_CHECK_INTERVAL_HOURS = 12;
export const LOCK_STALE_MS = 10 * 60 * 1000;

export const UPDATER_RUNTIME_FILES = [
  "plugin.json",
  "mcp_config.json",
  "hooks.json",
  "rules/AGENTS.md",
  "assets/logo.svg",
  "dist/mcp-server.mjs",
  "dist/session-start.mjs",
  "dist/benchmark.mjs",
  "dist/updater.mjs",
  "scripts/install.mjs",
  "LICENSE",
] as const;

export const UPDATER_OPTIONAL_FILES = [
  "assets/logo-light.svg",
  "assets/logo-dark.svg",
  "assets/architecture-light.svg",
  "assets/architecture-dark.svg",
  "assets/architecture-zh-light.svg",
  "assets/architecture-zh-dark.svg",
  "README.md",
  "README.zh-CN.md",
  "CHANGELOG.md",
  "docs/ANALYSIS_REPORT.md",
  "docs/ANALYSIS_REPORT.zh-CN.md",
] as const;

export type AutoUpdateMode = "auto" | "notify" | "off";
export type UpdateChannel = "release" | "main";
export type InstallKind = "copied" | "git-clone" | "git-dirty" | "symlink" | "dev-source";

export interface UpdaterConfig {
  autoUpdate?: AutoUpdateMode;
  checkIntervalHours?: number;
  channel?: UpdateChannel;
  repoUrl?: string;
  editsFormat?: "auto" | "array" | "string";
}

export interface ResolvedUpdaterSettings {
  mode: AutoUpdateMode;
  checkIntervalHours: number;
  checkIntervalMs: number;
  channel: UpdateChannel;
  repoUrl: string;
}

export interface UpdateState {
  lastCheckAt?: string;
  lastCheckAttemptAt?: string;
  latestVersion?: string;
  latestRef?: string;
  availableVersion?: string;
  availableReason?: string;
  lastAppliedVersion?: string;
  lastAppliedFrom?: string;
  lastAppliedAt?: string;
  lastAppliedCommit?: string;
  notifiedAppliedVersion?: string;
  notifiedAvailableVersion?: string;
  lastError?: string;
}

export interface UpdaterPaths {
  cacheDir: string;
  stateFile: string;
  configFile: string;
  lockFile: string;
}

export interface RemoteVersionInfo {
  ref: string;
  version: string | null;
  commitSha?: string;
  source: "git-tags" | "git-main" | "github-api";
}

export interface UpdateRunOptions {
  pluginDir: string;
  checkOnly?: boolean;
  force?: boolean;
  background?: boolean;
  channel?: UpdateChannel;
  repoUrl?: string;
  timeoutMs?: number;
  env?: NodeJS.ProcessEnv;
}

export interface UpdateRunResult {
  status:
    | "up-to-date"
    | "available"
    | "updated"
    | "skipped-disabled"
    | "skipped-locked"
    | "error";
  currentVersion: string;
  latestVersion: string | null;
  installKind: InstallKind;
  message: string;
  error?: string;
}

export function getHomeDir(env: NodeJS.ProcessEnv = process.env): string {
  return process.platform === "win32"
    ? env.USERPROFILE || env.HOME || os.homedir()
    : env.HOME || env.USERPROFILE || os.homedir();
}

export function getUpdaterPaths(env: NodeJS.ProcessEnv = process.env): UpdaterPaths {
  const custom = env.AGY_FAST_CACHE_DIR?.trim();
  const cacheDir = custom
    ? path.resolve(custom)
    : path.join(getHomeDir(env), ".gemini", "cache", "agy-fast");
  return {
    cacheDir,
    stateFile: path.join(cacheDir, "update-state.json"),
    configFile: path.join(cacheDir, "config.json"),
    lockFile: path.join(cacheDir, "update.lock"),
  };
}

export function parseSemver(raw: string): [number, number, number] | null {
  const m = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(raw.trim());
  if (!m) return null;
  return [
    Number.parseInt(m[1]!, 10),
    Number.parseInt(m[2]!, 10),
    Number.parseInt(m[3]!, 10),
  ];
}

export function compareSemver(a: string, b: string): number {
  const pa = parseSemver(a);
  const pb = parseSemver(b);
  if (!pa || !pb) return a.localeCompare(b);
  for (let i = 0; i < 3; i++) {
    if (pa[i]! !== pb[i]!) return pa[i]! > pb[i]! ? 1 : -1;
  }
  return 0;
}

export function pickLatestSemverTag(
  lsRemoteOutput: string,
): { tag: string; version: string; commitSha: string } | null {
  let best: { tag: string; version: string; commitSha: string } | null = null;
  for (const rawLine of lsRemoteOutput.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const parts = line.split(/\s+/);
    if (parts.length < 2) continue;
    const sha = parts[0]!;
    const ref = parts[1]!;
    if (!ref.startsWith("refs/tags/") || ref.endsWith("^{}")) continue;
    const tag = ref.slice("refs/tags/".length);
    const parsed = parseSemver(tag);
    if (!parsed) continue;
    const version = `${parsed[0]}.${parsed[1]}.${parsed[2]}`;
    if (!best || compareSemver(version, best.version) > 0) {
      best = { tag, version, commitSha: sha };
    }
  }
  return best;
}

export function parseAutoUpdateMode(raw: string | undefined): AutoUpdateMode | null {
  if (!raw) return null;
  const norm = raw.trim().toLowerCase();
  if (norm === "auto" || norm === "on" || norm === "1" || norm === "true") {
    return "auto";
  }
  if (norm === "notify" || norm === "check") {
    return "notify";
  }
  if (norm === "off" || norm === "0" || norm === "false" || norm === "disabled") {
    return "off";
  }
  return null;
}

export function parseUpdateChannel(raw: string | undefined): UpdateChannel | null {
  if (!raw) return null;
  const norm = raw.trim().toLowerCase();
  if (norm === "release" || norm === "stable") return "release";
  if (norm === "main" || norm === "nightly" || norm === "edge") return "main";
  return null;
}

async function readJsonFile<T>(filePath: string): Promise<T | null> {
  try {
    const raw = await fs.readFile(filePath, "utf8");
    const parsed = JSON.parse(raw) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as T;
    }
  } catch {
    // Missing or corrupt file: treat as empty
  }
  return null;
}

async function writeJsonAtomically(filePath: string, data: unknown): Promise<void> {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.tmp.${process.pid}.${Date.now()}`;
  await fs.writeFile(tmpPath, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  try {
    await fs.rename(tmpPath, filePath);
  } catch {
    try {
      // Windows fallback when destination is briefly locked
      await fs.copyFile(tmpPath, filePath);
    } finally {
      await fs.rm(tmpPath, { force: true }).catch(() => {});
    }
  }
}

export async function readUpdaterConfig(
  env: NodeJS.ProcessEnv = process.env,
): Promise<UpdaterConfig> {
  const paths = getUpdaterPaths(env);
  return (await readJsonFile<UpdaterConfig>(paths.configFile)) ?? {};
}

export async function writeUpdaterConfig(
  patch: UpdaterConfig,
  env: NodeJS.ProcessEnv = process.env,
): Promise<UpdaterConfig> {
  const paths = getUpdaterPaths(env);
  const existing = (await readJsonFile<UpdaterConfig>(paths.configFile)) ?? {};
  const merged: UpdaterConfig = { ...existing, ...patch };
  await writeJsonAtomically(paths.configFile, merged);
  return merged;
}

export async function readUpdateState(
  env: NodeJS.ProcessEnv = process.env,
): Promise<UpdateState> {
  const paths = getUpdaterPaths(env);
  return (await readJsonFile<UpdateState>(paths.stateFile)) ?? {};
}

export async function writeUpdateState(
  patch: Partial<UpdateState>,
  env: NodeJS.ProcessEnv = process.env,
): Promise<UpdateState> {
  const paths = getUpdaterPaths(env);
  const existing = (await readJsonFile<UpdateState>(paths.stateFile)) ?? {};
  const merged: UpdateState = { ...existing, ...patch };
  await writeJsonAtomically(paths.stateFile, merged);
  return merged;
}

export async function resolveUpdaterSettings(
  env: NodeJS.ProcessEnv = process.env,
): Promise<ResolvedUpdaterSettings> {
  const cfg = await readUpdaterConfig(env);
  const envMode = parseAutoUpdateMode(env.AGY_FAST_AUTO_UPDATE);
  const cfgMode = parseAutoUpdateMode(cfg.autoUpdate);
  const mode: AutoUpdateMode = envMode ?? cfgMode ?? "auto";

  const envHours = Number.parseFloat(env.AGY_FAST_UPDATE_INTERVAL_HOURS ?? "");
  const rawHours =
    Number.isFinite(envHours) && envHours >= 0.01
      ? envHours
      : typeof cfg.checkIntervalHours === "number" && cfg.checkIntervalHours >= 0.01
        ? cfg.checkIntervalHours
        : DEFAULT_CHECK_INTERVAL_HOURS;
  const checkIntervalHours = Math.max(0.01, Math.min(24 * 30, rawHours));

  const envChan = parseUpdateChannel(env.AGY_FAST_UPDATE_CHANNEL);
  const cfgChan = parseUpdateChannel(cfg.channel);
  const channel: UpdateChannel = envChan ?? cfgChan ?? "release";

  const repoUrl =
    env.AGY_FAST_REPO?.trim() || cfg.repoUrl?.trim() || DEFAULT_REPO_URL;

  return {
    mode,
    checkIntervalHours,
    checkIntervalMs: Math.round(checkIntervalHours * 3600 * 1000),
    channel,
    repoUrl,
  };
}

export async function readInstalledVersion(pluginDir: string): Promise<string> {
  const manifest = await readJsonFile<{ version?: unknown }>(
    path.join(pluginDir, "plugin.json"),
  );
  if (manifest && typeof manifest.version === "string" && manifest.version.trim()) {
    return manifest.version.trim();
  }
  return "0.0.0";
}

export function shouldTriggerBackgroundCheck(
  state: UpdateState,
  nowMs: number,
  intervalMs: number,
): boolean {
  const lastCompleted = state.lastCheckAt ? Date.parse(state.lastCheckAt) : NaN;
  const lastAttempt = state.lastCheckAttemptAt
    ? Date.parse(state.lastCheckAttemptAt)
    : NaN;
  if (Number.isFinite(lastCompleted) && nowMs - lastCompleted < intervalMs) {
    return false;
  }
  // Prevent burst spawns when parallel subagents start in the same minute
  if (Number.isFinite(lastAttempt) && nowMs - lastAttempt < Math.min(intervalMs, 5 * 60 * 1000)) {
    return false;
  }
  return true;
}

export async function consumeSnapshotUpdateNotice(
  pluginDir: string,
  currentVersion: string,
  env: NodeJS.ProcessEnv = process.env,
): Promise<string | null> {
  const state = await readUpdateState(env);

  // 1. Just auto-updated to currentVersion -> announce once
  if (
    state.lastAppliedVersion &&
    state.lastAppliedVersion === currentVersion &&
    state.lastAppliedFrom &&
    state.lastAppliedFrom !== currentVersion &&
    state.notifiedAppliedVersion !== currentVersion
  ) {
    await writeUpdateState({ notifiedAppliedVersion: currentVersion }, env).catch(
      () => {},
    );
    return `[agy-fast] Auto-updated v${state.lastAppliedFrom} -> v${currentVersion} (rules/hooks active now; reload Desktop/IDE window if MCP was already running).`;
  }

  // 2. Update available (notify mode, symlink, or dirty git repo) -> announce once per version
  if (
    state.availableVersion &&
    compareSemver(state.availableVersion, currentVersion) > 0 &&
    state.notifiedAvailableVersion !== state.availableVersion
  ) {
    await writeUpdateState(
      { notifiedAvailableVersion: state.availableVersion },
      env,
    ).catch(() => {});
    const reason = state.availableReason ? ` (${state.availableReason})` : "";
    const installerPath = path.join(pluginDir, "scripts", "install.mjs");
    return `[agy-fast] Update available: v${currentVersion} -> v${state.availableVersion}${reason}. Run: node "${installerPath}" update`;
  }

  return null;
}

async function pathExists(p: string): Promise<boolean> {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

export async function detectInstallKind(pluginDir: string): Promise<InstallKind> {
  const resolved = path.resolve(pluginDir);
  try {
    const lst = await fs.lstat(resolved);
    if (lst.isSymbolicLink()) return "symlink";
  } catch {
    return "copied";
  }

  const hasGit = await pathExists(path.join(resolved, ".git"));
  if (hasGit) {
    const statusRes = await execBinaryAsync("git", ["status", "--porcelain"], {
      cwd: resolved,
      timeoutMs: 4_000,
    });
    if (statusRes.error || statusRes.exitCode !== 0 || statusRes.stdout.trim() !== "") {
      return "git-dirty";
    }
    // Only auto-update git clones inside a plugins/agy-fast directory unless forced manually;
    // a standalone source repo outside plugins/ is treated as a developer workspace.
    const norm = resolved.split(path.sep).join("/").toLowerCase();
    const isPluginInstallDir =
      norm.endsWith("/plugins/agy-fast") || norm.includes("/plugins/agy-fast/");
    if (!isPluginInstallDir) {
      return "dev-source";
    }
    return "git-clone";
  }

  if (await pathExists(path.join(resolved, "src"))) {
    return "dev-source";
  }
  return "copied";
}

function extractGithubSlug(repoUrl: string): string | null {
  const m = /github\.com[:/]([^/]+\/[^/.]+?)(?:\.git)?\/?$/i.exec(repoUrl.trim());
  return m?.[1] ?? null;
}

export async function fetchLatestRemoteVersion(opts: {
  repoUrl: string;
  channel: UpdateChannel;
  timeoutMs?: number;
}): Promise<RemoteVersionInfo> {
  const timeoutMs = opts.timeoutMs ?? 8_000;
  const gitEnv = { ...process.env, GIT_TERMINAL_PROMPT: "0" };

  if (opts.channel === "release") {
    const tagsRes = await execBinaryAsync(
      "git",
      ["ls-remote", "--tags", "--refs", opts.repoUrl],
      { cwd: os.tmpdir(), timeoutMs, env: gitEnv },
    );
    if (!tagsRes.error && !tagsRes.timedOut && tagsRes.exitCode === 0) {
      const latest = pickLatestSemverTag(tagsRes.stdout);
      if (latest) {
        return {
          ref: latest.tag,
          version: latest.version,
          commitSha: latest.commitSha,
          source: "git-tags",
        };
      }
    }

    // Fallback to GitHub Releases API if git is unavailable and URL is GitHub
    const slug = extractGithubSlug(opts.repoUrl);
    if (slug && typeof fetch === "function") {
      try {
        const res = await fetch(
          `https://api.github.com/repos/${slug}/releases/latest`,
          {
            headers: {
              Accept: "application/vnd.github+json",
              "User-Agent": "agy-fast-updater",
            },
            signal: AbortSignal.timeout(timeoutMs),
          },
        );
        if (res.ok) {
          const json = (await res.json()) as { tag_name?: unknown };
          if (typeof json.tag_name === "string") {
            const parsed = parseSemver(json.tag_name);
            if (parsed) {
              return {
                ref: json.tag_name.trim(),
                version: `${parsed[0]}.${parsed[1]}.${parsed[2]}`,
                source: "github-api",
              };
            }
          }
        }
      } catch {
        // Fall through to git main check or error
      }
    }
  }

  // Main channel (or fallback when repo has no semver tags yet)
  const headRes = await execBinaryAsync(
    "git",
    ["ls-remote", opts.repoUrl, "refs/heads/main", "HEAD"],
    { cwd: os.tmpdir(), timeoutMs, env: gitEnv },
  );
  if (!headRes.error && !headRes.timedOut && headRes.exitCode === 0) {
    const firstLine = headRes.stdout.trim().split(/\r?\n/)[0] ?? "";
    const sha = firstLine.split(/\s+/)[0] || undefined;
    if (sha) {
      return {
        ref: "main",
        version: null,
        commitSha: sha,
        source: "git-main",
      };
    }
  }

  throw new Error(
    headRes.stderr.trim() ||
      headRes.error ||
      `Unable to reach update source: ${opts.repoUrl}`,
  );
}

export async function verifyStagedPlugin(stagingDir: string): Promise<{
  ok: boolean;
  version: string;
  reason?: string;
}> {
  for (const rel of UPDATER_RUNTIME_FILES) {
    if (!(await pathExists(path.join(stagingDir, rel)))) {
      return { ok: false, version: "0.0.0", reason: `Missing required file: ${rel}` };
    }
  }

  const manifest = await readJsonFile<{ name?: unknown; version?: unknown }>(
    path.join(stagingDir, "plugin.json"),
  );
  if (
    !manifest ||
    manifest.name !== "agy-fast" ||
    typeof manifest.version !== "string" ||
    !parseSemver(manifest.version)
  ) {
    return { ok: false, version: "0.0.0", reason: "Invalid staged plugin.json manifest" };
  }

  // Live MCP handshake smoke test on staged bundle before replacing live files
  const mcpPath = path.join(stagingDir, "dist", "mcp-server.mjs");
  const smokeOk = await new Promise<boolean>((resolve) => {
    const child = spawn(process.execPath, [mcpPath], {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    let stdout = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve(false);
    }, 6_000);
    timer.unref();

    child.stdout.on("data", (d: Buffer | string) => {
      stdout += d.toString();
    });
    child.on("error", () => {
      clearTimeout(timer);
      resolve(false);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        resolve(false);
        return;
      }
      try {
        const lines = stdout
          .trim()
          .split("\n")
          .filter(Boolean)
          .map((l) => JSON.parse(l) as { id?: number; result?: { tools?: Array<{ name?: string }> } });
        const listResp = lines.find((m) => m.id === 2);
        const tools = listResp?.result?.tools?.map((t) => t.name) ?? [];
        resolve(tools.length >= 4);
      } catch {
        resolve(false);
      }
    });

    const initMsg = JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "updater", version: "1" } },
    });
    const listMsg = JSON.stringify({
      jsonrpc: "2.0",
      id: 2,
      method: "tools/list",
      params: {},
    });
    child.stdin.write(`${initMsg}\n${listMsg}\n`);
    child.stdin.end();
  });

  if (!smokeOk) {
    return {
      ok: false,
      version: manifest.version,
      reason: "Staged MCP server smoke check failed",
    };
  }

  return { ok: true, version: manifest.version };
}

export async function syncRuntimeFilesAtomically(
  srcRoot: string,
  dstRoot: string,
): Promise<void> {
  await fs.mkdir(dstRoot, { recursive: true });
  // Sync plugin.json LAST so a partial failure never advances the installed version marker
  // while dist/*.mjs remain on the previous version.
  const runtimeWithoutManifest = UPDATER_RUNTIME_FILES.filter((rel) => rel !== "plugin.json");
  const toCopy: string[] = [...runtimeWithoutManifest];
  for (const opt of UPDATER_OPTIONAL_FILES) {
    if (await pathExists(path.join(srcRoot, opt))) {
      toCopy.push(opt);
    }
  }
  toCopy.push("plugin.json");

  let seq = 0;
  for (const rel of toCopy) {
    const src = path.join(srcRoot, rel);
    const dst = path.join(dstRoot, rel);
    await fs.mkdir(path.dirname(dst), { recursive: true });
    const tmp = `${dst}.agy-tmp.${process.pid}.${seq++}`;
    try {
      await fs.copyFile(src, tmp);
    } catch (err) {
      await fs.rm(tmp, { force: true }).catch(() => {});
      throw err;
    }
    try {
      await fs.rename(tmp, dst);
    } catch {
      try {
        // Fallback on Windows if target file is briefly open
        await fs.copyFile(tmp, dst);
      } finally {
        await fs.rm(tmp, { force: true }).catch(() => {});
      }
    }
  }
}

function isProcessDead(pid: number): boolean {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return false;
  } catch (err) {
    return (err as NodeJS.ErrnoException)?.code === "ESRCH";
  }
}

async function acquireUpdateLock(lockPath: string): Promise<boolean> {
  await fs.mkdir(path.dirname(lockPath), { recursive: true });
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const handle = await fs.open(lockPath, "wx");
      try {
        await handle.writeFile(
          JSON.stringify({ pid: process.pid, createdAt: new Date().toISOString() }),
          "utf8",
        );
      } finally {
        await handle.close();
      }
      return true;
    } catch (err) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code !== "EEXIST") return false;
      try {
        const st = await fs.stat(lockPath);
        let stale = Date.now() - st.mtimeMs > LOCK_STALE_MS;
        if (!stale) {
          const info = await readJsonFile<{ pid?: unknown }>(lockPath);
          if (
            typeof info?.pid === "number" &&
            info.pid !== process.pid &&
            isProcessDead(info.pid)
          ) {
            stale = true;
          }
        }
        if (stale) {
          await fs.rm(lockPath, { force: true });
          continue;
        }
      } catch {
        continue;
      }
      return false;
    }
  }
  return false;
}

export async function runPluginUpdate(
  opts: UpdateRunOptions,
): Promise<UpdateRunResult> {
  const env = opts.env ?? process.env;
  const pluginDir = path.resolve(opts.pluginDir);
  const paths = getUpdaterPaths(env);
  const settings = await resolveUpdaterSettings(env);
  const repoUrl = opts.repoUrl?.trim() || settings.repoUrl;
  const channel = opts.channel ?? settings.channel;
  const timeoutMs = opts.timeoutMs ?? 12_000;

  const currentVersion = await readInstalledVersion(pluginDir);
  const installKind = await detectInstallKind(pluginDir);

  if (opts.background && settings.mode === "off") {
    return {
      status: "skipped-disabled",
      currentVersion,
      latestVersion: null,
      installKind,
      message: "Auto-update is disabled.",
    };
  }

  const locked = await acquireUpdateLock(paths.lockFile);
  if (!locked) {
    return {
      status: "skipped-locked",
      currentVersion,
      latestVersion: null,
      installKind,
      message: "Another update check is already in progress.",
    };
  }

  const nowIso = new Date().toISOString();
  let stagingDir = "";

  try {
    const remote = await fetchLatestRemoteVersion({ repoUrl, channel, timeoutMs });
    const prevState = await readUpdateState(env);

    const hasNewerSemver =
      remote.version !== null && compareSemver(remote.version, currentVersion) > 0;
    const hasNewerMainCommit =
      remote.version === null &&
      Boolean(remote.commitSha) &&
      remote.commitSha !== prevState.lastAppliedCommit;

    if (!opts.force && !hasNewerSemver && !hasNewerMainCommit) {
      await writeUpdateState(
        {
          lastCheckAt: nowIso,
          latestVersion: remote.version ?? currentVersion,
          latestRef: remote.ref,
          availableVersion: undefined,
          availableReason: undefined,
          lastError: undefined,
        },
        env,
      );
      return {
        status: "up-to-date",
        currentVersion,
        latestVersion: remote.version ?? currentVersion,
        installKind,
        message: `agy-fast v${currentVersion} is already up to date.`,
      };
    }

    const effectiveNotifyOnly =
      opts.checkOnly ||
      (opts.background && settings.mode === "notify") ||
      installKind === "symlink" ||
      installKind === "git-dirty" ||
      installKind === "dev-source";

    if (effectiveNotifyOnly) {
      const reason =
        installKind === "symlink"
          ? "linked dev install"
          : installKind === "git-dirty"
            ? "uncommitted local changes"
            : installKind === "dev-source"
              ? "local source repo"
              : settings.mode === "notify"
                ? "notify mode"
                : undefined;

      await writeUpdateState(
        {
          lastCheckAt: nowIso,
          latestVersion: remote.version ?? undefined,
          latestRef: remote.ref,
          availableVersion: remote.version ?? undefined,
          availableReason: reason,
          lastError: undefined,
        },
        env,
      );

      return {
        status: "available",
        currentVersion,
        latestVersion: remote.version,
        installKind,
        message: remote.version
          ? `Update available: v${currentVersion} -> v${remote.version}${reason ? ` (${reason})` : ""}.`
          : `Update available on branch '${remote.ref}'${reason ? ` (${reason})` : ""}.`,
      };
    }

    // Apply update: stage into temp dir and validate before touching live plugin files
    stagingDir = path.join(
      paths.cacheDir,
      `staging-${process.pid}-${Date.now()}`,
    );
    await fs.mkdir(paths.cacheDir, { recursive: true });
    const gitEnv = { ...env, GIT_TERMINAL_PROMPT: "0" };

    const cloneArgs =
      remote.ref === "main"
        ? ["clone", "--depth", "1", "--branch", "main", repoUrl, stagingDir]
        : ["clone", "--depth", "1", "--branch", remote.ref, repoUrl, stagingDir];
    const cloneRes = await execBinaryAsync("git", cloneArgs, {
      cwd: paths.cacheDir,
      timeoutMs: Math.max(timeoutMs, 30_000),
      env: gitEnv,
    });
    if (cloneRes.error || cloneRes.timedOut || cloneRes.exitCode !== 0) {
      throw new Error(
        cloneRes.stderr.trim() ||
          cloneRes.error ||
          `git clone failed for ${remote.ref}`,
      );
    }

    const stagedCheck = await verifyStagedPlugin(stagingDir);
    if (!stagedCheck.ok) {
      throw new Error(stagedCheck.reason || "Staged plugin validation failed");
    }

    if (
      !opts.force &&
      remote.version === null &&
      (compareSemver(stagedCheck.version, currentVersion) < 0 ||
        (compareSemver(stagedCheck.version, currentVersion) === 0 &&
          !prevState.lastAppliedCommit))
    ) {
      await writeUpdateState(
        {
          lastCheckAt: nowIso,
          latestVersion: stagedCheck.version,
          latestRef: remote.ref,
          lastAppliedCommit: remote.commitSha,
          lastError: undefined,
        },
        env,
      );
      return {
        status: "up-to-date",
        currentVersion,
        latestVersion: stagedCheck.version,
        installKind,
        message: `agy-fast v${currentVersion} is already up to date.`,
      };
    }

    if (installKind === "git-clone") {
      // Fetch directly from the already smoke-tested local stagingDir so we honor
      // --repo/AGY_FAST_REPO overrides and avoid TOCTOU mismatch with a second network fetch.
      const fetchRes = await execBinaryAsync(
        "git",
        ["fetch", "--depth", "1", stagingDir, "HEAD"],
        { cwd: pluginDir, timeoutMs: Math.max(timeoutMs, 20_000), env: gitEnv },
      );
      if (fetchRes.error || fetchRes.timedOut || fetchRes.exitCode !== 0) {
        throw new Error(
          fetchRes.stderr.trim() ||
            fetchRes.error ||
            (fetchRes.timedOut ? "git fetch timed out in plugin clone" : "git fetch failed in plugin clone"),
        );
      }
      const checkoutRes = await execBinaryAsync(
        "git",
        ["checkout", "-q", "FETCH_HEAD"],
        { cwd: pluginDir, timeoutMs: 10_000, env: gitEnv },
      );
      if (checkoutRes.error || checkoutRes.timedOut || checkoutRes.exitCode !== 0) {
        throw new Error(
          checkoutRes.stderr.trim() ||
            checkoutRes.error ||
            (checkoutRes.timedOut ? "git checkout timed out in plugin clone" : "git checkout failed in plugin clone"),
        );
      }
    } else {
      await syncRuntimeFilesAtomically(stagingDir, pluginDir);
    }

    await writeUpdateState(
      {
        lastCheckAt: nowIso,
        latestVersion: stagedCheck.version,
        latestRef: remote.ref,
        availableVersion: undefined,
        availableReason: undefined,
        lastAppliedVersion: stagedCheck.version,
        lastAppliedFrom: currentVersion,
        lastAppliedAt: nowIso,
        lastAppliedCommit: remote.commitSha,
        notifiedAppliedVersion: opts.background ? undefined : stagedCheck.version,
        lastError: undefined,
      },
      env,
    );

    return {
      status: "updated",
      currentVersion,
      latestVersion: stagedCheck.version,
      installKind,
      message: `Updated agy-fast v${currentVersion} -> v${stagedCheck.version} (${installKind}).`,
    };
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    await writeUpdateState(
      {
        lastCheckAt: nowIso,
        lastError: errMsg.slice(0, 300),
      },
      env,
    ).catch(() => {});
    return {
      status: "error",
      currentVersion,
      latestVersion: null,
      installKind,
      message: `Update check failed: ${errMsg}`,
      error: errMsg,
    };
  } finally {
    if (stagingDir) {
      await fs.rm(stagingDir, { recursive: true, force: true }).catch(() => {});
    }
    await fs.rm(paths.lockFile, { force: true }).catch(() => {});
  }
}

export function spawnBackgroundUpdateCheck(
  pluginDir: string,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  const updaterPath = path.join(pluginDir, "dist", "updater.mjs");
  try {
    const child = spawn(
      process.execPath,
      [updaterPath, "--background", "--plugin-dir", pluginDir],
      {
        detached: true,
        stdio: "ignore",
        windowsHide: true,
        env,
      },
    );
    child.unref();
    return true;
  } catch {
    return false;
  }
}
