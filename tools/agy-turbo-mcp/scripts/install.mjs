#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");
const PLUGIN_NAME = "agy-fast";

const RUNTIME_FILES = [
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
];

const OPTIONAL_DOC_FILES = [
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
];

function getHomeDir() {
  return process.platform === "win32"
    ? process.env.USERPROFILE || process.env.HOME || os.homedir()
    : process.env.HOME || process.env.USERPROFILE || os.homedir();
}

function getGlobalPluginDir() {
  return path.join(getHomeDir(), ".gemini", "config", "plugins", PLUGIN_NAME);
}

function getUpdaterCacheDir() {
  const custom = process.env.AGY_FAST_CACHE_DIR?.trim();
  return custom
    ? path.resolve(custom)
    : path.join(getHomeDir(), ".gemini", "cache", PLUGIN_NAME);
}

async function readJsonSafe(filePath) {
  try {
    const raw = await fs.readFile(filePath, "utf8");
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : null;
  } catch {
    return null;
  }
}

async function writeUpdaterConfigFile(patch) {
  const cacheDir = getUpdaterCacheDir();
  const cfgPath = path.join(cacheDir, "config.json");
  await fs.mkdir(cacheDir, { recursive: true });
  const existing = (await readJsonSafe(cfgPath)) || {};
  const merged = { ...existing, ...patch };
  const tmpPath = `${cfgPath}.tmp.${process.pid}.${Date.now()}`;
  await fs.writeFile(tmpPath, `${JSON.stringify(merged, null, 2)}\n`, "utf8");
  try {
    await fs.rename(tmpPath, cfgPath);
  } catch {
    try {
      await fs.copyFile(tmpPath, cfgPath);
    } finally {
      await fs.rm(tmpPath, { force: true }).catch(() => {});
    }
  }
  return { cfgPath, config: merged };
}

function parseArgs(argv) {
  const args = argv.slice(2);
  let command = "install";
  let targetDir = "";
  let projectDir = "";
  let useLink = false;
  let skipEnable = false;
  let skipAcp = false;
  let quiet = false;
  let showHelp = false;
  let checkOnly = false;
  let force = false;
  let autoUpdate = "";
  let intervalHours = null;
  let channel = "";
  let repoUrl = "";
  let editsFormat = "";

  const positional = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === "-h" || a === "--help") {
      showHelp = true;
    } else if (a === "--link") {
      useLink = true;
    } else if (a === "--no-enable") {
      skipEnable = true;
    } else if (a === "--no-acp") {
      skipAcp = true;
    } else if (a === "--quiet" || a === "-q") {
      quiet = true;
    } else if (a === "--check") {
      checkOnly = true;
    } else if (a === "--force") {
      force = true;
    } else if (a === "--auto-update") {
      const next = args[i + 1];
      if (!next || !["auto", "notify", "off"].includes(next.toLowerCase())) {
        throw new Error("Invalid value after --auto-update (expected: auto | notify | off)");
      }
      autoUpdate = args[++i].toLowerCase();
    } else if (a === "--interval") {
      const next = args[i + 1];
      const parsed = Number.parseFloat(next || "");
      if (!Number.isFinite(parsed) || parsed < 1 || parsed > 720) {
        throw new Error("Invalid value after --interval (expected hours between 1 and 720)");
      }
      intervalHours = parsed;
      i++;
    } else if (a === "--channel") {
      const next = args[i + 1];
      if (!next || !["release", "main"].includes(next.toLowerCase())) {
        throw new Error("Invalid value after --channel (expected: release | main)");
      }
      channel = args[++i].toLowerCase();
    } else if (a === "--repo") {
      const next = args[i + 1];
      if (!next || next.startsWith("-")) {
        throw new Error("Missing repository URL after --repo");
      }
      repoUrl = args[++i];
    } else if (a === "--edits-format") {
      const next = args[i + 1];
      if (!next || !["auto", "array", "string"].includes(next.toLowerCase())) {
        throw new Error(
          "Invalid value after --edits-format (expected: auto | array | string)",
        );
      }
      editsFormat = args[++i].toLowerCase();
    } else if (a === "--target") {
      const next = args[i + 1];
      if (!next || next.startsWith("-")) {
        throw new Error("Missing directory path after --target");
      }
      targetDir = path.resolve(args[++i]);
    } else if (a === "--project") {
      const next = args[i + 1];
      if (next && !next.startsWith("-")) {
        projectDir = path.resolve(args[++i]);
      } else {
        projectDir = process.cwd();
      }
    } else if (!a.startsWith("-")) {
      positional.push(a);
    } else {
      throw new Error(`Unknown option: ${a}`);
    }
  }

  if (positional.length > 1) {
    throw new Error(
      `Unexpected extra argument: '${positional[1]}'. Run with --help for usage.`,
    );
  }
  if (positional.length === 1) {
    command = positional[0];
  }

  const isCustomTarget = Boolean(targetDir);
  if (!targetDir) {
    targetDir = projectDir
      ? path.join(projectDir, ".agents", "plugins", PLUGIN_NAME)
      : getGlobalPluginDir();
  }

  return {
    command,
    targetDir,
    isProject: Boolean(projectDir),
    isCustomTarget,
    useLink,
    skipEnable,
    skipAcp,
    quiet,
    showHelp,
    checkOnly,
    force,
    autoUpdate,
    intervalHours,
    channel,
    repoUrl,
    editsFormat,
  };
}

function printHelp() {
  console.log(`agy-fast installer

Usage:
  node scripts/install.mjs [command] [options]

Commands:
  install      Install agy-fast plugin (default)
  update       Check for and apply plugin updates from GitHub releases
  config       View or persist auto-update preferences
  uninstall    Remove installed agy-fast plugin
  status       Check installation and run live health checks
  doctor       Alias for status

Options:
  --project [dir]               Install/update <dir>/.agents/plugins/agy-fast
  --target <dir>                Use a custom plugin directory
  --link                        Create a symlink to this repository (dev mode)
  --no-enable                   Skip running 'agy plugin enable agy-fast'
  --no-acp                      Skip syncing isolated T3 Code ACP / GEMINI_HOME profiles
  --check                       (update) Check for newer versions without applying
  --force                       (update) Re-apply even if already on the same version
  --auto-update auto|notify|off Configure background auto-update mode
  --interval <hours>            Configure background check interval in hours (default 12)
  --channel release|main        Configure update channel (default release)
  --repo <url>                  Override git repository URL for updates
  --edits-format auto|array|string
                                Configure apply_edits.edits schema preference (default auto)
  -h, --help                    Show this help
`);
}

function checkNodeVersion() {
  const major = Number.parseInt(process.versions.node.split(".")[0] || "0", 10);
  if (major < 20) {
    throw new Error(
      `Node.js >= 20 is required (current version: v${process.versions.node})`,
    );
  }
  return process.versions.node;
}

function spawnCliSync(bin, args, opts = {}) {
  const baseOpts = { windowsHide: true, ...opts };
  const direct = spawnSync(bin, args, baseOpts);
  if (process.platform !== "win32" || direct.error?.code !== "ENOENT") {
    return direct;
  }
  const whereRes = spawnSync("where.exe", [bin], {
    encoding: "utf8",
    timeout: 2000,
    windowsHide: true,
  });
  if (whereRes.error || whereRes.status !== 0) {
    return direct;
  }
  const candidates = (whereRes.stdout || "")
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean);
  const resolved = candidates.find((p) => /\.(exe|cmd|bat)$/i.test(p));
  if (!resolved) {
    return direct;
  }
  if (/\.exe$/i.test(resolved)) {
    return spawnSync(resolved, args, baseOpts);
  }
  const quoteWin = (s) => `"${String(s).replace(/"/g, '""')}"`;
  const cmdLine = [quoteWin(resolved), ...args.map(quoteWin)].join(" ");
  return spawnSync("cmd.exe", ["/d", "/s", "/c", `"${cmdLine}"`], {
    ...baseOpts,
    windowsVerbatimArguments: true,
  });
}

function hasCommand(bin, args = ["--version"]) {
  const res = spawnCliSync(bin, args, {
    stdio: "ignore",
    timeout: 3000,
  });
  return !res.error && res.status === 0;
}

async function pathExists(p) {
  try {
    await fs.lstat(p);
    return true;
  } catch {
    return false;
  }
}

async function lstatSafe(p) {
  try {
    return await fs.lstat(p);
  } catch (err) {
    if (err?.code === "ENOENT" || err?.code === "ENOTDIR") {
      return null;
    }
    throw err;
  }
}

function toComparePath(p) {
  const normalized = path.resolve(p);
  return process.platform === "win32" || process.platform === "darwin"
    ? normalized.toLowerCase()
    : normalized;
}

function isStrictAncestorPath(ancestorPath, descendantPath) {
  const rel = path.relative(ancestorPath, descendantPath);
  return (
    rel !== "" &&
    rel !== ".." &&
    !rel.startsWith(`..${path.sep}`) &&
    !path.isAbsolute(rel)
  );
}

async function assertRemovableTarget(targetPath, stat) {
  if (!stat || stat.isSymbolicLink()) {
    return;
  }
  if (!stat.isDirectory()) {
    throw new Error(
      `Refusing to delete ${targetPath}: target exists and is not an ${PLUGIN_NAME} directory or symlink.`,
    );
  }
  const entries = await fs.readdir(targetPath);
  if (entries.length === 0) {
    return;
  }
  const manifest = await readJsonSafe(path.join(targetPath, "plugin.json"));
  if (manifest?.name !== PLUGIN_NAME) {
    throw new Error(
      `Refusing to delete non-empty directory ${targetPath}: missing plugin.json with name "${PLUGIN_NAME}".`,
    );
  }
}

async function removeTargetPath(targetPath, stat) {
  if (!stat) {
    return;
  }
  if (stat.isSymbolicLink()) {
    await fs.unlink(targetPath);
    return;
  }
  await fs.rm(targetPath, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
}

async function verifyMcpServerLive(mcpScriptPath) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [mcpScriptPath], {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });

    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("MCP server smoke test timed out after 5s"));
    }, 5000);
    timer.unref();

    child.stdout.on("data", (d) => {
      stdout += d.toString("utf8");
    });
    child.stderr.on("data", (d) => {
      stderr += d.toString("utf8");
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      const lines = stdout
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter(Boolean);
      if (lines.length < 2) {
        reject(
          new Error(
            `MCP server returned incomplete response (exit ${code}): ${stderr || stdout}`,
          ),
        );
        return;
      }
      try {
        const initResp = JSON.parse(lines[0]);
        const listResp = JSON.parse(lines[1]);
        const tools = listResp?.result?.tools;
        if (!initResp?.result?.serverInfo || !Array.isArray(tools)) {
          reject(new Error("Invalid MCP JSON-RPC responses"));
          return;
        }
        const applyEditsTool = tools.find((t) => t.name === "apply_edits");
        const applyEditsSchemaType =
          applyEditsTool?.inputSchema?.properties?.edits?.type || "unknown";
        resolve({
          protocolVersion: initResp.result.protocolVersion,
          tools: tools.map((t) => t.name),
          applyEditsSchemaType,
        });
      } catch (err) {
        reject(err);
      }
    });

    child.stdin.write(
      JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2025-03-26",
          capabilities: {},
          clientInfo: { name: "agy-fast-doctor", version: "1.0" },
        },
      }) + "\n",
    );
    child.stdin.write(
      JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }) + "\n",
    );
    child.stdin.write(
      JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" }) + "\n",
    );
    child.stdin.end();
  });
}

async function verifySessionStartLive(hookScriptPath, sampleWorkspace) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [hookScriptPath], {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
      env: { ...process.env, AGY_FAST_AUTO_UPDATE: "off" },
    });
    let stdout = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error("SessionStart hook smoke test timed out after 5s"));
    }, 5000);
    timer.unref();

    child.stdout.on("data", (d) => {
      stdout += d.toString("utf8");
    });
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) {
        reject(new Error(`SessionStart hook exited with code ${code}`));
        return;
      }
      try {
        const parsed = JSON.parse(stdout.trim());
        const msg = parsed?.injectSteps?.[0]?.systemMessage?.systemMessage || "";
        resolve({
          durationMs: Date.now() - start,
          bytes: Buffer.byteLength(msg, "utf8"),
        });
      } catch (err) {
        reject(err);
      }
    });

    child.stdin.end(
      JSON.stringify({
        cwd: sampleWorkspace,
        workspacePaths: [sampleWorkspace],
        hookEventName: "SessionStart",
      }),
    );
  });
}

async function assertSourceFilesPresent(srcRoot) {
  for (const rel of RUNTIME_FILES) {
    const src = path.join(srcRoot, rel);
    if (!(await pathExists(src))) {
      throw new Error(
        `Missing required file: ${rel}. Run 'npm run build' first if installing from source.`,
      );
    }
  }
}

async function copyRuntimeFiles(srcRoot, dstRoot) {
  await assertSourceFilesPresent(srcRoot);
  await fs.mkdir(dstRoot, { recursive: true });
  const toCopy = [...RUNTIME_FILES];
  for (const opt of OPTIONAL_DOC_FILES) {
    if (await pathExists(path.join(srcRoot, opt))) {
      toCopy.push(opt);
    }
  }
  for (const rel of toCopy) {
    const src = path.join(srcRoot, rel);
    const dst = path.join(dstRoot, rel);
    await fs.mkdir(path.dirname(dst), { recursive: true });
    await fs.copyFile(src, dst);
  }
}

async function isDirSafe(p) {
  try {
    return (await fs.stat(p)).isDirectory();
  } catch {
    return false;
  }
}

async function discoverIsolatedPluginDirs(primaryGlobalDir) {
  const home = getHomeDir();
  const primaryCompare = toComparePath(primaryGlobalDir);
  const defaultGeminiCompare = toComparePath(path.join(home, ".gemini"));
  const seen = new Set([primaryCompare]);
  const results = [];

  const addCandidate = (geminiRoot, label) => {
    const resolvedRoot = path.resolve(geminiRoot);
    if (toComparePath(resolvedRoot) === defaultGeminiCompare) return;
    const pluginDir = path.join(resolvedRoot, "config", "plugins", PLUGIN_NAME);
    const key = toComparePath(pluginDir);
    if (seen.has(key)) return;
    seen.add(key);
    results.push({ geminiRoot: resolvedRoot, pluginDir, label });
  };

  // 1. T3 Code isolated Antigravity ACP provider profiles:
  //    ~/.t3/userdata/providers/antigravity/<hash>/
  const t3ProvidersRoot = path.join(home, ".t3", "userdata", "providers", "antigravity");
  try {
    const entries = await fs.readdir(t3ProvidersRoot, { withFileTypes: true });
    entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const profileRoot = path.join(t3ProvidersRoot, entry.name);
      const hasConfig = await isDirSafe(path.join(profileRoot, "config"));
      const hasAcp = await isDirSafe(path.join(profileRoot, "antigravity-acp"));
      if (hasConfig || hasAcp) {
        addCandidate(profileRoot, `T3 Code ACP (${entry.name.slice(0, 8)})`);
      }
    }
  } catch {
    // ~/.t3 not present
  }

  // 2. Explicit GEMINI_HOME env var when pointing to an isolated Antigravity / ACP home
  const envGeminiHome = process.env.GEMINI_HOME?.trim();
  if (envGeminiHome) {
    const resolvedEnv = path.resolve(envGeminiHome);
    if (await isDirSafe(resolvedEnv)) {
      const hasConfig = await isDirSafe(path.join(resolvedEnv, "config"));
      const hasAcp = await isDirSafe(path.join(resolvedEnv, "antigravity-acp"));
      if (hasConfig || hasAcp) {
        addCandidate(resolvedEnv, "GEMINI_HOME");
      }
    }
  }

  return results;
}

async function inspectIsolatedPluginDir(isolated) {
  const stat = await lstatSafe(isolated.pluginDir);
  if (!stat) {
    return { ...isolated, status: "missing", mode: null, linkTarget: null, version: null };
  }
  const configJson = await readJsonSafe(path.join(isolated.geminiRoot, "config", "config.json"));
  const disabledInConfig = configJson?.plugins?.[PLUGIN_NAME]?.enabled === false;
  const manifest = await readJsonSafe(path.join(isolated.pluginDir, "plugin.json"));
  const mcpExists = await pathExists(path.join(isolated.pluginDir, "dist", "mcp-server.mjs"));
  const valid = manifest?.name === PLUGIN_NAME && mcpExists;
  const version = typeof manifest?.version === "string" ? manifest.version : null;

  if (stat.isSymbolicLink()) {
    let linkTarget = null;
    try {
      linkTarget = await fs.readlink(isolated.pluginDir);
    } catch {
      // ignore
    }
    if (!valid) {
      return { ...isolated, status: "broken", mode: "symlink", linkTarget, version: null };
    }
    return {
      ...isolated,
      status: disabledInConfig ? "disabled" : "ok",
      mode: "symlink",
      linkTarget,
      version,
    };
  }

  if (!valid) {
    return { ...isolated, status: "broken", mode: "copied", linkTarget: null, version: null };
  }
  return {
    ...isolated,
    status: disabledInConfig ? "disabled" : "ok",
    mode: "copied",
    linkTarget: null,
    version,
  };
}

async function syncIsolatedPluginDir(resolvedSrc, primaryGlobalDir, isolated, opts) {
  const isoStat = await lstatSafe(isolated.pluginDir);
  const srcCompare = toComparePath(resolvedSrc);
  const primaryCompare = toComparePath(primaryGlobalDir);
  if (isoStat && !isoStat.isSymbolicLink()) {
    const isoCompare = toComparePath(await fs.realpath(isolated.pluginDir));
    if (isoCompare === srcCompare || isoCompare === primaryCompare) {
      return { synced: true, mode: "same" };
    }
    if (isStrictAncestorPath(isoCompare, srcCompare) || isStrictAncestorPath(isoCompare, primaryCompare)) {
      throw new Error(
        `Refusing to sync into ${isolated.pluginDir}: target is an ancestor directory of ${resolvedSrc}.`,
      );
    }
  }

  await assertRemovableTarget(isolated.pluginDir, isoStat);
  await fs.mkdir(path.dirname(isolated.pluginDir), { recursive: true });
  await removeTargetPath(isolated.pluginDir, isoStat);

  // Link directly to resolvedSrc in --link dev mode (avoids chained NTFS junctions on Windows),
  // otherwise link to primaryGlobalDir so auto-updates to ~/.gemini/config/plugins/agy-fast
  // propagate immediately to every isolated ACP profile.
  const linkSource = opts.useLink ? resolvedSrc : primaryGlobalDir;
  const linkType = process.platform === "win32" ? "junction" : "dir";
  let mode = "symlink";
  try {
    await fs.symlink(linkSource, isolated.pluginDir, linkType);
  } catch (err) {
    const code = err?.code;
    if (code === "EPERM" || code === "ENOTSUP" || code === "EXDEV") {
      await copyRuntimeFiles(resolvedSrc, isolated.pluginDir);
      mode = "copied";
    } else {
      throw err;
    }
  }

  if (!opts.quiet) {
    if (mode === "symlink") {
      console.log(`Synced ${isolated.label}: ${isolated.pluginDir} -> ${linkSource}`);
    } else {
      console.log(`Synced ${isolated.label} (copied): ${isolated.pluginDir}`);
    }
    const configJson = await readJsonSafe(path.join(isolated.geminiRoot, "config", "config.json"));
    if (configJson?.plugins?.[PLUGIN_NAME]?.enabled === false) {
      console.log(
        `Note: '${PLUGIN_NAME}' is explicitly disabled in ${path.join(isolated.geminiRoot, "config", "config.json")}.`,
      );
    }
  }
  return { synced: true, mode };
}

async function runDoctor(targetDir, quiet = false, doctorOpts = {}) {
  const nodeVer = checkNodeVersion();
  const hasRg = hasCommand("rg", ["--version"]);
  const hasAgy = hasCommand("agy", ["--version"]);
  const exists = await pathExists(targetDir);

  const report = {
    nodeVersion: nodeVer,
    ripgrep: hasRg,
    agyCli: hasAgy,
    targetDir,
    installed: exists,
    mcpTools: [],
    hookLatencyMs: null,
    hookBytes: null,
    agyValidated: null,
  };

  if (!exists) {
    if (!quiet) {
      console.log(`agy-fast is not installed at: ${targetDir}`);
    }
    return { ok: false, report };
  }

  const mcpPath = path.join(targetDir, "dist", "mcp-server.mjs");
  const hookPath = path.join(targetDir, "dist", "session-start.mjs");
  const mcpCheck = await verifyMcpServerLive(mcpPath);
  const hookCheck = await verifySessionStartLive(hookPath, targetDir);

  report.mcpTools = mcpCheck.tools;
  report.hookLatencyMs = hookCheck.durationMs;
  report.hookBytes = hookCheck.bytes;

  if (hasAgy) {
    const valRes = spawnCliSync("agy", ["plugin", "validate", "--strict", targetDir], {
      encoding: "utf8",
      timeout: 10_000,
    });
    report.agyValidated = !valRes.error && valRes.status === 0;
  }

  const manifest = await readJsonSafe(path.join(targetDir, "plugin.json"));
  const installedVersion =
    manifest && typeof manifest.version === "string" ? manifest.version : "unknown";
  const cacheDir = getUpdaterCacheDir();
  const updaterCfg = (await readJsonSafe(path.join(cacheDir, "config.json"))) || {};
  const updaterState = (await readJsonSafe(path.join(cacheDir, "update-state.json"))) || {};
  const effectiveAutoMode =
    process.env.AGY_FAST_AUTO_UPDATE?.trim().toLowerCase() ||
    updaterCfg.autoUpdate ||
    "auto";
  const effectiveInterval =
    process.env.AGY_FAST_UPDATE_INTERVAL_HOURS?.trim() ||
    updaterCfg.checkIntervalHours ||
    12;
  const effectiveChannel =
    process.env.AGY_FAST_UPDATE_CHANNEL?.trim().toLowerCase() ||
    updaterCfg.channel ||
    "release";
  const rawEnvEditsFormat = process.env.AGY_FAST_EDITS_FORMAT?.trim().toLowerCase() || "";
  const validEnvEditsFormat = ["auto", "array", "string"].includes(rawEnvEditsFormat)
    ? rawEnvEditsFormat
    : "";
  const validCfgEditsFormat =
    typeof updaterCfg.editsFormat === "string" &&
    ["auto", "array", "string"].includes(updaterCfg.editsFormat.trim().toLowerCase())
      ? updaterCfg.editsFormat.trim().toLowerCase()
      : "";
  const effectiveEditsFormat = validEnvEditsFormat || validCfgEditsFormat || "auto";
  const rawHostOverride = process.env.AGY_FAST_HOST?.trim() || "";
  const validHostOverride =
    /^(?:cli|desktop|app|antigravity|ide|unknown)(?:[@\-/\s]v?\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?)?$/i.test(
      rawHostOverride,
    )
      ? rawHostOverride
      : "";
  const hostEnvLabel =
    validHostOverride || process.env.ANTIGRAVITY_LS_VERSION?.trim() || "auto";

  const isolatedProfiles = doctorOpts.checkIsolated
    ? await Promise.all(
        (await discoverIsolatedPluginDirs(targetDir)).map((iso) =>
          inspectIsolatedPluginDir(iso),
        ),
      )
    : [];
  report.isolatedProfiles = isolatedProfiles;

  if (!quiet) {
    console.log("agy-fast health check:");
    console.log(`  - Plugin path : ${targetDir}`);
    console.log(`  - Version     : v${installedVersion}`);
    console.log(`  - Node.js     : v${nodeVer} (OK)`);
    console.log(
      `  - ripgrep (rg): ${hasRg ? "available (fast search active)" : "not found (automatic Node.js fallback active)"}`,
    );
    console.log(
      `  - MCP server  : OK (${mcpCheck.tools.length} tools: ${mcpCheck.tools.join(", ")})`,
    );
    console.log(
      `  - Host compat : editsFormat=${effectiveEditsFormat} (active edits schema: ${mcpCheck.applyEditsSchemaType}; host=${hostEnvLabel})`,
    );
    console.log(
      `  - SessionStart: ${hookCheck.bytes > 0 ? "OK" : "EMPTY (FAILED)"} (${hookCheck.durationMs}ms, ${hookCheck.bytes} bytes)`,
    );
    const checkSummary = updaterState.availableVersion
      ? `update available: v${updaterState.availableVersion}`
      : updaterState.lastCheckAt
        ? `last check: ${updaterState.lastCheckAt}`
        : "not checked yet";
    console.log(
      `  - Auto-update : ${effectiveAutoMode} (interval: ${effectiveInterval}h, channel: ${effectiveChannel}; ${checkSummary})`,
    );
    if (hasAgy) {
      console.log(
        `  - agy validate: ${report.agyValidated ? "PASSED (--strict)" : "FAILED"}`,
      );
    }
    for (const iso of isolatedProfiles) {
      const desc =
        iso.status === "ok"
          ? iso.mode === "symlink"
            ? `OK (linked -> ${iso.linkTarget})`
            : `OK (installed v${iso.version || "unknown"})`
          : iso.status === "disabled"
            ? "DISABLED in profile config.json"
            : iso.status === "broken"
              ? `BROKEN (${iso.pluginDir})`
              : `MISSING (${iso.pluginDir} — run 'install' to sync)`;
      console.log(`  - ${iso.label}: ${desc}`);
    }
  }

  const hasBrokenIsolated = isolatedProfiles.some((iso) => iso.status === "broken");
  const ok =
    mcpCheck.tools.length === 4 &&
    hookCheck.bytes > 0 &&
    !hasBrokenIsolated &&
    (report.agyValidated === null || report.agyValidated === true);
  return { ok, report };
}

async function runInstall(opts) {
  checkNodeVersion();

  const resolvedSrc = await fs.realpath(REPO_ROOT);
  const resolvedDst = path.resolve(opts.targetDir);
  const dstStat = await lstatSafe(resolvedDst);

  let sameDirectory = false;
  if (dstStat && !dstStat.isSymbolicLink()) {
    const srcCompare = toComparePath(resolvedSrc);
    const dstCompare = toComparePath(await fs.realpath(resolvedDst));
    sameDirectory = srcCompare === dstCompare;
    if (isStrictAncestorPath(dstCompare, srcCompare)) {
      throw new Error(
        `Refusing to install into ${resolvedDst}: target is an ancestor directory of ${resolvedSrc}.`,
      );
    }
  }

  await assertSourceFilesPresent(resolvedSrc);

  if (sameDirectory) {
    if (!opts.quiet) {
      console.log(`Repository is already at target plugin directory: ${resolvedDst}`);
    }
  } else if (opts.useLink) {
    await assertRemovableTarget(resolvedDst, dstStat);
    await fs.mkdir(path.dirname(resolvedDst), { recursive: true });
    await removeTargetPath(resolvedDst, dstStat);
    const linkType = process.platform === "win32" ? "junction" : "dir";
    await fs.symlink(resolvedSrc, resolvedDst, linkType);
    if (!opts.quiet) {
      console.log(`Linked ${resolvedDst} -> ${resolvedSrc}`);
    }
  } else {
    // Preserve clean target directory when re-installing / upgrading
    await assertRemovableTarget(resolvedDst, dstStat);
    await removeTargetPath(resolvedDst, dstStat);
    await copyRuntimeFiles(resolvedSrc, resolvedDst);
    if (!opts.quiet) {
      console.log(`Installed agy-fast to ${resolvedDst}`);
    }
  }

  const hasAgy = hasCommand("agy", ["--version"]);
  const isGlobalTarget = !opts.isProject && !opts.isCustomTarget;
  const syncIsolated = isGlobalTarget && !opts.skipAcp;
  const isolatedTargets = syncIsolated ? await discoverIsolatedPluginDirs(resolvedDst) : [];
  for (const isolated of isolatedTargets) {
    await syncIsolatedPluginDir(resolvedSrc, resolvedDst, isolated, opts);
  }

  if (hasAgy && !opts.skipEnable && isGlobalTarget) {
    const enableRes = spawnCliSync("agy", ["plugin", "enable", PLUGIN_NAME], {
      encoding: "utf8",
      timeout: 10_000,
    });
    if (!opts.quiet) {
      if (!enableRes.error && enableRes.status === 0) {
        console.log(`Enabled plugin '${PLUGIN_NAME}' via agy CLI.`);
      } else {
        console.log(
          `Note: could not auto-enable via 'agy plugin enable ${PLUGIN_NAME}'. Enable it in Antigravity Customizations UI.`,
        );
      }
    }
  }

  if (
    opts.autoUpdate ||
    opts.intervalHours !== null ||
    opts.channel ||
    opts.repoUrl ||
    opts.editsFormat
  ) {
    const patch = {};
    if (opts.autoUpdate) patch.autoUpdate = opts.autoUpdate;
    if (opts.intervalHours !== null) patch.checkIntervalHours = opts.intervalHours;
    if (opts.channel) patch.channel = opts.channel;
    if (opts.repoUrl) patch.repoUrl = opts.repoUrl;
    if (opts.editsFormat) patch.editsFormat = opts.editsFormat;
    await writeUpdaterConfigFile(patch);
  }

  const { ok } = await runDoctor(resolvedDst, opts.quiet, { checkIsolated: syncIsolated });
  if (!ok) {
    throw new Error("Post-install health check failed.");
  }

  if (!opts.quiet) {
    console.log("");
    console.log("Installation complete. Next steps:");
    console.log("  - Antigravity CLI (agy)    : Start a new session to activate agy-fast.");
    console.log("  - Antigravity Desktop / IDE: Open Customizations -> Installed and ensure 'agy-fast' is enabled.");
    if (isolatedTargets.length > 0) {
      console.log(
        `  - T3 Code / ACP (${isolatedTargets.length})        : Start a new thread (or reload provider) to activate agy-fast.`,
      );
    }
  }
}

async function runConfig(opts) {
  const patch = {};
  if (opts.autoUpdate) patch.autoUpdate = opts.autoUpdate;
  if (opts.intervalHours !== null) patch.checkIntervalHours = opts.intervalHours;
  if (opts.channel) patch.channel = opts.channel;
  if (opts.repoUrl) patch.repoUrl = opts.repoUrl;
  if (opts.editsFormat) patch.editsFormat = opts.editsFormat;

  let cfgPath = path.join(getUpdaterCacheDir(), "config.json");
  let cfg;
  if (Object.keys(patch).length > 0) {
    const res = await writeUpdaterConfigFile(patch);
    cfgPath = res.cfgPath;
    cfg = res.config;
  } else {
    cfg = (await readJsonSafe(cfgPath)) || {};
  }

  if (!opts.quiet) {
    console.log(`agy-fast updater config (${cfgPath}):`);
    console.log(`  - autoUpdate        : ${cfg.autoUpdate || "auto"}`);
    console.log(`  - checkIntervalHours: ${cfg.checkIntervalHours || 12}`);
    console.log(`  - channel           : ${cfg.channel || "release"}`);
    console.log(`  - repoUrl           : ${cfg.repoUrl || "https://github.com/INK-Q/agy-fast.git"}`);
    console.log(`  - editsFormat       : ${cfg.editsFormat || "auto"}`);
  }
}

async function runUpdate(opts) {
  checkNodeVersion();
  const resolvedDst = path.resolve(opts.targetDir);
  if (!(await pathExists(resolvedDst))) {
    throw new Error(
      `agy-fast is not installed at ${resolvedDst}. Run 'install' first.`,
    );
  }

  if (opts.autoUpdate || opts.intervalHours !== null) {
    const patch = {};
    if (opts.autoUpdate) patch.autoUpdate = opts.autoUpdate;
    if (opts.intervalHours !== null) patch.checkIntervalHours = opts.intervalHours;
    await writeUpdaterConfigFile(patch);
  }

  const installedUpdater = path.join(resolvedDst, "dist", "updater.mjs");
  const repoUpdater = path.join(REPO_ROOT, "dist", "updater.mjs");
  const updaterPath = (await pathExists(installedUpdater))
    ? installedUpdater
    : repoUpdater;
  if (!(await pathExists(updaterPath))) {
    throw new Error("Missing dist/updater.mjs. Run 'npm run build' or reinstall agy-fast.");
  }

  const updaterArgs = [updaterPath, "--plugin-dir", resolvedDst, "--json"];
  if (opts.checkOnly) updaterArgs.push("--check");
  if (opts.force) updaterArgs.push("--force");
  if (opts.channel) updaterArgs.push("--channel", opts.channel);
  if (opts.repoUrl) updaterArgs.push("--repo", opts.repoUrl);

  const res = spawnSync(process.execPath, updaterArgs, {
    encoding: "utf8",
    timeout: 60_000,
    windowsHide: true,
  });

  let parsed = null;
  try {
    parsed = JSON.parse((res.stdout || "").trim());
  } catch {
    // Non-JSON error output handled below
  }

  if (!parsed) {
    const detail = (res.stderr || res.stdout || res.error?.message || "updater failed").trim();
    throw new Error(detail);
  }

  if (!opts.quiet) {
    console.log(parsed.message);
  }

  if (parsed.status === "error") {
    throw new Error(parsed.error || parsed.message);
  }

  const isGlobalTarget = !opts.isProject && !opts.isCustomTarget;
  const syncIsolated = isGlobalTarget && !opts.skipAcp;
  if (
    syncIsolated &&
    !opts.checkOnly &&
    (parsed.status === "updated" || parsed.status === "up-to-date" || opts.force)
  ) {
    const isolatedTargets = await discoverIsolatedPluginDirs(resolvedDst);
    const dstStat = await lstatSafe(resolvedDst);
    const linkTarget = dstStat?.isSymbolicLink() ? await fs.realpath(resolvedDst) : resolvedDst;
    for (const isolated of isolatedTargets) {
      const info = await inspectIsolatedPluginDir(isolated);
      const alreadyLinkedToGlobal =
        (info.status === "ok" || info.status === "disabled") &&
        info.mode === "symlink" &&
        info.linkTarget &&
        toComparePath(path.resolve(path.dirname(isolated.pluginDir), info.linkTarget)) ===
          toComparePath(linkTarget);
      if (!alreadyLinkedToGlobal) {
        await syncIsolatedPluginDir(linkTarget, linkTarget, isolated, {
          ...opts,
          useLink: false,
        });
      }
    }
  }

  if (parsed.status === "updated") {
    const { ok } = await runDoctor(resolvedDst, opts.quiet, { checkIsolated: syncIsolated });
    if (!ok) {
      throw new Error("Post-update health check failed.");
    }
    if (!opts.quiet) {
      console.log("");
      console.log("Update complete. Start a new session (or reload Desktop/IDE window) to load the updated MCP server.");
    }
  }
}

async function runUninstall(opts) {
  const resolvedDst = path.resolve(opts.targetDir);
  const dstStat = await lstatSafe(resolvedDst);
  const isGlobalTarget = !opts.isProject && !opts.isCustomTarget;
  const syncIsolated = isGlobalTarget && !opts.skipAcp;
  const isolatedTargets = syncIsolated ? await discoverIsolatedPluginDirs(resolvedDst) : [];
  const existingIsolated = [];
  for (const isolated of isolatedTargets) {
    const isoStat = await lstatSafe(isolated.pluginDir);
    if (isoStat) {
      existingIsolated.push({ isolated, stat: isoStat });
    }
  }

  if (!dstStat && existingIsolated.length === 0) {
    if (!opts.quiet) {
      console.log(`Nothing to remove (${resolvedDst} does not exist).`);
    }
    return;
  }

  const resolvedSrc = await fs.realpath(REPO_ROOT);
  const srcCompare = toComparePath(resolvedSrc);
  const hasSrcDir = await pathExists(path.join(resolvedSrc, "src"));

  const guardTarget = async (targetPath, stat) => {
    if (!stat.isSymbolicLink()) {
      const targetCompare = toComparePath(await fs.realpath(targetPath));
      if (srcCompare === targetCompare && hasSrcDir) {
        throw new Error(
          `Refusing to delete the active source repository at ${targetPath}. Remove it manually if desired.`,
        );
      }
      if (isStrictAncestorPath(targetCompare, srcCompare)) {
        throw new Error(
          `Refusing to delete ${targetPath}: target is an ancestor directory of ${resolvedSrc}.`,
        );
      }
    }
    await assertRemovableTarget(targetPath, stat);
  };

  if (dstStat) {
    await guardTarget(resolvedDst, dstStat);
  }
  for (const { isolated, stat } of existingIsolated) {
    await guardTarget(isolated.pluginDir, stat);
  }

  if (dstStat && hasCommand("agy", ["--version"]) && !opts.skipEnable && isGlobalTarget) {
    spawnCliSync("agy", ["plugin", "disable", PLUGIN_NAME], {
      stdio: "ignore",
      timeout: 10_000,
    });
  }

  for (const { isolated, stat } of existingIsolated) {
    await removeTargetPath(isolated.pluginDir, stat);
    if (!opts.quiet) {
      console.log(`Removed agy-fast from ${isolated.pluginDir} (${isolated.label})`);
    }
  }

  if (dstStat) {
    await removeTargetPath(resolvedDst, dstStat);
    if (!opts.quiet) {
      console.log(`Removed agy-fast from ${resolvedDst}`);
    }
  }
}

async function main() {
  const opts = parseArgs(process.argv);
  if (opts.showHelp) {
    printHelp();
    return;
  }

  switch (opts.command) {
    case "install":
      await runInstall(opts);
      break;
    case "update":
      await runUpdate(opts);
      break;
    case "config":
      await runConfig(opts);
      break;
    case "uninstall":
      await runUninstall(opts);
      break;
    case "status":
    case "doctor": {
      const checkIsolated = !opts.isProject && !opts.isCustomTarget && !opts.skipAcp;
      const { ok } = await runDoctor(opts.targetDir, opts.quiet, { checkIsolated });
      if (!ok) process.exitCode = 1;
      break;
    }
    default:
      throw new Error(`Unknown command: ${opts.command}. Run with --help for usage.`);
  }
}

main().catch((err) => {
  console.error(`Error: ${err instanceof Error ? err.message : String(err)}`);
  process.exit(1);
});
