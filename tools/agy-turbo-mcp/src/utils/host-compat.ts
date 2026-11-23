import fs from "node:fs";
import { promises as fsp } from "node:fs";
import os from "node:os";
import path from "node:path";
import { MCP_CALL_BUDGET_SEC } from "./exec-utils.js";
import { DEFAULT_INLINE_MAX_BYTES } from "./output-filter.js";

export type HostSurface = "cli" | "desktop" | "ide" | "unknown";
export type EditsFormatMode = "auto" | "array" | "string";
export type ResolvedEditsFormat = "array" | "string";

export interface McpClientInfoSignal {
  name?: unknown;
  version?: unknown;
}

export interface HostSignals {
  /** Standard MCP initialize params.clientInfo */
  clientInfo?: McpClientInfoSignal | null;
  /** Official SessionStart hook stdin metadata */
  transcriptPath?: string;
  artifactDirectoryPath?: string;
  modelName?: string;
  /** Process environment (defaults to process.env) */
  env?: NodeJS.ProcessEnv;
  /** Optional persisted editsFormat preference from ~/.gemini/cache/agy-fast/config.json */
  configEditsFormat?: EditsFormatMode | string | null;
}

export interface HostIdentity {
  surface: HostSurface;
  version: string | null;
  source:
    | "override"
    | "mcp:client-info"
    | "hook:path"
    | "env:ls-version"
    | "env:app-data-dir"
    | "unknown";
  rawClientName?: string;
  rawClientVersion?: string;
  modelName?: string;
}

export interface HostCompatRule {
  id: string;
  surface: HostSurface;
  /** Inclusive lower semver bound (e.g. "1.5.0") */
  minVersion?: string;
  /** Exclusive upper semver bound (e.g. "1.5.16"); omit until an upstream fix version is confirmed */
  maxVersionExclusive?: string;
  editsFormat?: ResolvedEditsFormat;
  callBudgetSec?: number;
  inlineMaxBytes?: number;
  reason: string;
}

export interface ResolvedHostCompat {
  identity: HostIdentity;
  editsFormat: ResolvedEditsFormat;
  editsFormatSource:
    | "env-override"
    | "config-override"
    | "compat-rule"
    | "token-saving-default";
  callBudgetSec: number;
  inlineMaxBytes: number;
  matchedRuleIds: string[];
  reason: string;
}

/**
 * Central declarative Antigravity surface & version compatibility matrix.
 *
 * Defaults bias toward the most token-efficient behavior (`editsFormat: "array"`,
 * which uses ~2.6-3.3x fewer output tokens than double-escaped JSON strings).
 * Surface/version rules activate targeted workarounds ONLY where empirically needed,
 * and can be retired in one line once an upstream fix version is verified.
 */
export const HOST_COMPAT_RULES: readonly HostCompatRule[] = [
  {
    id: "agy-cli-1.5-call-mcp-tool-empty-edits",
    surface: "cli",
    minVersion: "1.5.0",
    // Antigravity CLI >= 1.5.8 supports `"eager": true` in mcp_config.json,
    // which registers typed native mcp_agy-fast_fast_* tools and bypasses
    // lazy call_mcp_tool altogether (keeping token-saving native array edits).
    // Pre-eager CLI 1.5.0-1.5.7 keeps the JSON-string workaround automatically;
    // any lazy/custom setup can still opt in via AGY_FAST_EDITS_FORMAT=string.
    maxVersionExclusive: "1.5.8",
    editsFormat: "string",
    reason:
      "Pre-eager Antigravity CLI 1.5.0-1.5.7 loads tools lazily via call_mcp_tool where long native edits arrays can drop to []; JSON array string avoids payload loss",
  },
];

interface ParsedHostSemver {
  major: number;
  minor: number;
  patch: number;
  prerelease: string | null;
}

function parseHostSemverParts(raw: string): ParsedHostSemver | null {
  const m =
    /(?:^|[^0-9.])v?(\d+)\.(\d+)\.(\d+)(?:\.\d+)?(?:-([0-9A-Za-z.-]+))?(?:$|[^0-9.])/.exec(
      raw.trim(),
    );
  if (!m) return null;
  const major = Number.parseInt(m[1]!, 10);
  const minor = Number.parseInt(m[2]!, 10);
  const patch = Number.parseInt(m[3]!, 10);
  if (
    !Number.isSafeInteger(major) ||
    !Number.isSafeInteger(minor) ||
    !Number.isSafeInteger(patch) ||
    major > 999_999 ||
    minor > 999_999 ||
    patch > 999_999
  ) {
    return null;
  }
  return { major, minor, patch, prerelease: m[4] ?? null };
}

function formatHostSemverParts(parts: ParsedHostSemver): string {
  const base = `${parts.major}.${parts.minor}.${parts.patch}`;
  return parts.prerelease ? `${base}-${parts.prerelease}` : base;
}

export function parseHostSemver(raw: string): [number, number, number] | null {
  const parts = parseHostSemverParts(raw);
  return parts ? [parts.major, parts.minor, parts.patch] : null;
}

export function formatHostSemver(tuple: [number, number, number]): string {
  return `${tuple[0]}.${tuple[1]}.${tuple[2]}`;
}

function comparePrerelease(a: string, b: string): number {
  const aParts = a.split(".");
  const bParts = b.split(".");
  const len = Math.max(aParts.length, bParts.length);
  for (let i = 0; i < len; i++) {
    const ai = aParts[i];
    const bi = bParts[i];
    if (ai === undefined) return -1;
    if (bi === undefined) return 1;
    if (ai === bi) continue;
    const aNum = /^\d+$/.test(ai);
    const bNum = /^\d+$/.test(bi);
    if (aNum && bNum) {
      const diff = Number.parseInt(ai, 10) - Number.parseInt(bi, 10);
      if (diff !== 0) return diff > 0 ? 1 : -1;
    } else if (aNum !== bNum) {
      return aNum ? -1 : 1;
    } else {
      return ai < bi ? -1 : 1;
    }
  }
  return 0;
}

export function compareHostSemver(a: string, b: string): number {
  const pa = parseHostSemverParts(a);
  const pb = parseHostSemverParts(b);
  if (!pa || !pb) return a.localeCompare(b);
  if (pa.major !== pb.major) return pa.major > pb.major ? 1 : -1;
  if (pa.minor !== pb.minor) return pa.minor > pb.minor ? 1 : -1;
  if (pa.patch !== pb.patch) return pa.patch > pb.patch ? 1 : -1;
  if (pa.prerelease === pb.prerelease) return 0;
  if (pa.prerelease === null) return 1;
  if (pb.prerelease === null) return -1;
  return comparePrerelease(pa.prerelease, pb.prerelease);
}

export function parseEditsFormatMode(raw: unknown): EditsFormatMode | null {
  if (typeof raw !== "string") return null;
  const v = raw.trim().toLowerCase();
  if (v === "auto" || v === "array" || v === "string") return v;
  return null;
}

export function parseHostOverride(
  raw: unknown,
): { surface: HostSurface; version: string | null } | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim().toLowerCase();
  if (!trimmed) return null;
  const m = /^(cli|desktop|app|antigravity|ide|unknown)(?:[@\-/\s](.+))?$/.exec(trimmed);
  if (!m) return null;
  const surface: HostSurface =
    m[1] === "app" || m[1] === "antigravity" ? "desktop" : (m[1] as HostSurface);
  if (m[2] !== undefined) {
    const parts = parseHostSemverParts(m[2]);
    if (!parts) return null;
    return { surface, version: formatHostSemverParts(parts) };
  }
  return { surface, version: null };
}

const EXPLICIT_THIRD_PARTY_CLIENT_RE =
  /^(?:claude(?:[-_\s]|$)|cursor(?:[-_\s]|$)|windsurf(?:[-_\s]|$)|vscode(?:[-_\s]|$)|visual[\s_-]+studio[\s_-]+code|codex(?:[-_\s]|$)|zed(?:[-_\s]|$)|cline(?:[-_\s]|$)|roo[-_\s]?code|continue(?:[-_\s]|$)|opencode(?:[-_\s]|$)|aider(?:[-_\s]|$)|goose(?:[-_\s]|$)|jetbrains(?:[-_\s]|$)|neovim(?:[-_\s]|$)|nvim(?:[-_\s]|$)|emacs(?:[-_\s]|$)|mcp[-_\s]?inspector(?:[-_\s]|$))/i;

function isExplicitThirdPartyClient(rawName: unknown): boolean {
  if (typeof rawName !== "string") return false;
  return EXPLICIT_THIRD_PARTY_CLIENT_RE.test(rawName.trim());
}

function parseSurfaceFromClientName(rawName: unknown): HostSurface | null {
  if (typeof rawName !== "string") return null;
  const s = rawName.trim().toLowerCase();
  if (!s || s.startsWith("agy-fast")) return null;
  if (
    /^(?:google[-_\s]?)?antigravity[-_\s]?cli$/.test(s) ||
    /^agy(?:[-_\s]?cli)?$/.test(s)
  ) {
    return "cli";
  }
  if (/^(?:google[-_\s]?)?antigravity[-_\s]?ide$/.test(s)) {
    return "ide";
  }
  if (/^(?:google[-_\s]?)?antigravity(?:[-_\s]?(?:2\.0|app|desktop))?$/.test(s)) {
    return "desktop";
  }
  return null;
}

function parseLsVersion(
  raw: unknown,
): { surface: HostSurface | null; version: string | null } | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const parts = parseHostSemverParts(trimmed);
  const version = parts ? formatHostSemverParts(parts) : null;
  if (/^(?:antigravity[-_\s]?)?cli(?:[-_/@\s]|$)/i.test(trimmed)) {
    return { surface: "cli", version };
  }
  if (/^(?:antigravity[-_\s]?)?ide(?:[-_/@\s]|$)/i.test(trimmed)) {
    return { surface: "ide", version };
  }
  if (/^(?:antigravity[-_\s]?)?(?:desktop|app)(?:[-_/@\s]|$)/i.test(trimmed)) {
    return { surface: "desktop", version };
  }
  if (/^antigravity(?:[-_/@\s]|$)/i.test(trimmed)) {
    return { surface: "desktop", version };
  }
  if (version) {
    return { surface: null, version };
  }
  return null;
}

function parseSurfaceFromPath(
  rawPath: unknown,
  allowTrailingAppDir = false,
): HostSurface | null {
  if (typeof rawPath !== "string" || !rawPath.trim()) return null;
  const segs = rawPath
    .trim()
    .replace(/\\/g, "/")
    .split("/")
    .filter(Boolean)
    .map((s) => s.toLowerCase());
  // Highest confidence: official ~/.gemini/<product>/ directory convention
  for (let i = 0; i < segs.length - 1; i++) {
    if (segs[i] !== ".gemini") continue;
    const next = segs[i + 1];
    if (next === "antigravity-cli") return "cli";
    if (next === "antigravity-ide") return "ide";
    if (next === "antigravity") return "desktop";
  }
  // Fallback for explicit app-data dirs (ANTIGRAVITY_APP_DATA_DIR only): inspect trailing segment only
  // so arbitrary workspace folders named antigravity / antigravity-cli / antigravity-ide never misfire.
  if (allowTrailingAppDir && segs.length > 0) {
    const tail = segs[segs.length - 1];
    if (tail === "antigravity-cli") return "cli";
    if (tail === "antigravity-ide") return "ide";
    if (tail === "antigravity") return "desktop";
  }
  return null;
}

export function detectHostIdentity(signals: HostSignals = {}): HostIdentity {
  const env = signals.env ?? process.env;
  const rawClientName =
    typeof signals.clientInfo?.name === "string" && signals.clientInfo.name.trim()
      ? signals.clientInfo.name.trim()
      : undefined;
  const rawClientVersion =
    typeof signals.clientInfo?.version === "string" && signals.clientInfo.version.trim()
      ? signals.clientInfo.version.trim()
      : undefined;
  const modelName =
    typeof signals.modelName === "string" && signals.modelName.trim()
      ? signals.modelName.trim()
      : undefined;

  const override = parseHostOverride(env.AGY_FAST_HOST);
  if (override) {
    return {
      surface: override.surface,
      version: override.version,
      source: "override",
      rawClientName,
      rawClientVersion,
      modelName,
    };
  }

  // Known third-party MCP hosts (e.g. Claude Code / Cursor launched from an agy shell)
  // must not inherit ambient ANTIGRAVITY_LS_VERSION / ANTIGRAVITY_APP_DATA_DIR env vars.
  if (isExplicitThirdPartyClient(rawClientName)) {
    return {
      surface: "unknown",
      version: null,
      source: "mcp:client-info",
      rawClientName,
      rawClientVersion,
      modelName,
    };
  }

  const clientSurface = parseSurfaceFromClientName(rawClientName);
  const clientVerParts =
    clientSurface && rawClientVersion ? parseHostSemverParts(rawClientVersion) : null;
  const clientVersion = clientVerParts ? formatHostSemverParts(clientVerParts) : null;

  const hookSurface =
    parseSurfaceFromPath(signals.transcriptPath) ??
    parseSurfaceFromPath(signals.artifactDirectoryPath);
  const lsInfo = parseLsVersion(env.ANTIGRAVITY_LS_VERSION);
  const appDataSurface = parseSurfaceFromPath(env.ANTIGRAVITY_APP_DATA_DIR, true);

  let surface: HostSurface = "unknown";
  let source: HostIdentity["source"] = "unknown";

  if (clientSurface) {
    surface = clientSurface;
    source = "mcp:client-info";
  } else if (hookSurface) {
    surface = hookSurface;
    source = "hook:path";
  } else if (lsInfo?.surface) {
    surface = lsInfo.surface;
    source = "env:ls-version";
  } else if (appDataSurface) {
    surface = appDataSurface;
    source = "env:app-data-dir";
  }

  // Only adopt a version from a source that matches (or does not conflict with) the active surface.
  let version: string | null = null;
  if (clientSurface === surface && clientVersion) {
    version = clientVersion;
  } else if (
    surface !== "unknown" &&
    lsInfo?.version &&
    (lsInfo.surface === null || lsInfo.surface === surface)
  ) {
    version = lsInfo.version;
  }

  return {
    surface,
    version,
    source,
    rawClientName,
    rawClientVersion,
    modelName,
  };
}

function ruleMatchesIdentity(rule: HostCompatRule, identity: HostIdentity): boolean {
  if (rule.surface !== identity.surface) return false;
  if (rule.minVersion !== undefined || rule.maxVersionExclusive !== undefined) {
    if (!identity.version) return false;
    if (
      rule.minVersion !== undefined &&
      compareHostSemver(identity.version, rule.minVersion) < 0
    ) {
      return false;
    }
    if (
      rule.maxVersionExclusive !== undefined &&
      compareHostSemver(identity.version, rule.maxVersionExclusive) >= 0
    ) {
      return false;
    }
  }
  return true;
}

export function resolveHostCompat(
  signals: HostSignals = {},
  rules: readonly HostCompatRule[] = HOST_COMPAT_RULES,
): ResolvedHostCompat {
  const env = signals.env ?? process.env;
  const identity = detectHostIdentity(signals);

  let ruleEditsFormat: ResolvedEditsFormat | undefined;
  let callBudgetSec = MCP_CALL_BUDGET_SEC;
  let inlineMaxBytes = DEFAULT_INLINE_MAX_BYTES;
  const matchedRuleIds: string[] = [];
  const reasons: string[] = [];

  for (const rule of rules) {
    if (!ruleMatchesIdentity(rule, identity)) continue;
    matchedRuleIds.push(rule.id);
    if (rule.editsFormat !== undefined) ruleEditsFormat = rule.editsFormat;
    if (rule.callBudgetSec !== undefined) callBudgetSec = rule.callBudgetSec;
    if (rule.inlineMaxBytes !== undefined) inlineMaxBytes = rule.inlineMaxBytes;
    if (rule.reason) reasons.push(rule.reason);
  }

  const envMode = parseEditsFormatMode(env.AGY_FAST_EDITS_FORMAT);
  const cfgMode = parseEditsFormatMode(signals.configEditsFormat);

  let editsFormat: ResolvedEditsFormat = "array";
  let editsFormatSource: ResolvedHostCompat["editsFormatSource"] = "token-saving-default";
  let reason =
    reasons[0] ??
    "Defaulting to native array edits for lower output token usage (~2.6-3.3x cheaper than double-escaped JSON strings)";

  if (envMode === "array" || envMode === "string") {
    editsFormat = envMode;
    editsFormatSource = "env-override";
    reason = `Forced by AGY_FAST_EDITS_FORMAT=${envMode}`;
  } else if (envMode !== "auto" && (cfgMode === "array" || cfgMode === "string")) {
    editsFormat = cfgMode;
    editsFormatSource = "config-override";
    reason = `Configured in agy-fast config.json (editsFormat=${cfgMode})`;
  } else if (ruleEditsFormat !== undefined) {
    editsFormat = ruleEditsFormat;
    editsFormatSource = "compat-rule";
  }

  return {
    identity,
    editsFormat,
    editsFormatSource,
    callBudgetSec,
    inlineMaxBytes,
    matchedRuleIds,
    reason,
  };
}

export function formatHostLabel(identity: HostIdentity): string {
  return identity.version ? `${identity.surface}@${identity.version}` : identity.surface;
}

/**
 * Returns a single compact SessionStart directive ONLY when a non-default
 * compatibility workaround is active (0 extra bytes/tokens on unaffected hosts).
 */
export function formatSessionStartCompatNotice(
  compat: ResolvedHostCompat,
): string | null {
  if (compat.editsFormat !== "string") return null;
  const hostLabel = formatHostLabel(compat.identity);
  return `[agy-fast compat] host=${hostLabel} | apply_edits.edits (lazy call_mcp_tool mode): pass as JSON array string (not native array) to avoid call_mcp_tool [] drops.`;
}

export function formatHostSummary(compat: ResolvedHostCompat): string {
  const hostLabel = formatHostLabel(compat.identity);
  const detail =
    compat.editsFormatSource === "compat-rule" && compat.matchedRuleIds.length > 0
      ? `rule: ${compat.matchedRuleIds[0]}`
      : compat.editsFormatSource;
  return `${hostLabel} -> apply_edits.edits=${compat.editsFormat} (${detail})`;
}

function getConfigPath(env: NodeJS.ProcessEnv = process.env): string {
  const custom = env.AGY_FAST_CACHE_DIR?.trim();
  const home =
    process.platform === "win32"
      ? env.USERPROFILE || env.HOME || os.homedir()
      : env.HOME || env.USERPROFILE || os.homedir();
  const cacheDir = custom ? path.resolve(custom) : path.join(home, ".gemini", "cache", "agy-fast");
  return path.join(cacheDir, "config.json");
}

export function readSavedEditsFormatSync(
  env: NodeJS.ProcessEnv = process.env,
): EditsFormatMode | undefined {
  try {
    const raw = fs.readFileSync(getConfigPath(env), "utf8");
    const parsed = JSON.parse(raw) as { editsFormat?: unknown };
    return parseEditsFormatMode(parsed?.editsFormat) ?? undefined;
  } catch {
    return undefined;
  }
}

export async function readSavedEditsFormat(
  env: NodeJS.ProcessEnv = process.env,
): Promise<EditsFormatMode | undefined> {
  try {
    const raw = await fsp.readFile(getConfigPath(env), "utf8");
    const parsed = JSON.parse(raw) as { editsFormat?: unknown };
    return parseEditsFormatMode(parsed?.editsFormat) ?? undefined;
  } catch {
    return undefined;
  }
}
