import { promises as fs } from "node:fs";
import path from "node:path";
import { execBinaryAsync } from "./exec-utils.js";
import { DEFAULT_IGNORE_DIRS } from "./fs-utils.js";
import { sliceSafeChars, sliceUtf8Bytes, truncateWithByteNotice } from "./output-filter.js";

export interface SnapshotOptions {
  maxChars?: number;
  maxBytes?: number;
  gitTimeoutMs?: number;
}

const HARDENED_GIT_BASE_ARGS = [
  "--no-optional-locks",
  "-c",
  "core.fsmonitor=false",
  "-c",
  "diff.external=",
  "-c",
  "gpg.program=false",
  "-c",
  "gpg.openpgp.program=false",
  "-c",
  "gpg.ssh.program=false",
  "-c",
  "gpg.x509.program=false",
  "--no-pager",
] as const;

async function getGitSummary(
  workspaceDir: string,
  timeoutMs: number,
): Promise<string | null> {
  const [branchRes, symRefRes, statusRes, logRes] = await Promise.all([
    execBinaryAsync("git", [...HARDENED_GIT_BASE_ARGS, "rev-parse", "--abbrev-ref", "HEAD"], {
      cwd: workspaceDir,
      timeoutMs,
      maxOutputBytes: 4096,
    }),
    execBinaryAsync("git", [...HARDENED_GIT_BASE_ARGS, "symbolic-ref", "--short", "HEAD"], {
      cwd: workspaceDir,
      timeoutMs,
      maxOutputBytes: 4096,
    }),
    execBinaryAsync("git", [...HARDENED_GIT_BASE_ARGS, "status", "--porcelain=v1"], {
      cwd: workspaceDir,
      timeoutMs,
      maxOutputBytes: 32_768,
    }),
    execBinaryAsync("git", [...HARDENED_GIT_BASE_ARGS, "log", "-n", "3", "--format=%h %s"], {
      cwd: workspaceDir,
      timeoutMs,
      maxOutputBytes: 8192,
    }),
  ]);

  const hasValidRepo =
    (!branchRes.error && branchRes.exitCode === 0) ||
    (!symRefRes.error && symRefRes.exitCode === 0) ||
    (!statusRes.error && statusRes.exitCode === 0);

  if (!hasValidRepo) {
    return null;
  }

  const branch =
    (branchRes.exitCode === 0 && branchRes.stdout.trim()) ||
    (symRefRes.exitCode === 0 && symRefRes.stdout.trim()) ||
    "HEAD";
  const isUnborn = branchRes.exitCode !== 0 && symRefRes.exitCode === 0;
  const statusLines = statusRes.exitCode === 0
    ? statusRes.stdout
        .split(/\r?\n/)
        .filter((l) => {
          const trimmed = l.trim();
          if (!trimmed) return false;
          if (trimmed.startsWith("?? ")) {
            const topSeg = trimmed.slice(3).split("/")[0]?.replace(/\/$/, "");
            if (topSeg && DEFAULT_IGNORE_DIRS.has(topSeg)) return false;
          }
          return true;
        })
    : [];
  const logLines = logRes.exitCode === 0
    ? logRes.stdout
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter(Boolean)
    : [];

  const parts: string[] = [
    isUnborn ? `Git: branch \`${branch}\` (unborn, no commits yet)` : `Git: branch \`${branch}\``,
  ];
  if (statusLines.length === 0) {
    parts.push("Working tree: clean");
  } else {
    const prefix = `Modified (${statusLines.length}): `;
    const maxModifiedChars = 260;
    const maxItems = 8;
    const shown: string[] = [];
    let currentLen = prefix.length;
    for (let i = 0; i < Math.min(statusLines.length, maxItems); i++) {
      const raw = statusLines[i]!.trim();
      const item = raw.length > 100 ? `${sliceSafeChars(raw, 97)}...` : raw;
      const sepLen = shown.length > 0 ? 2 : 0;
      const remainingAfter = statusLines.length - (i + 1);
      const moreReserve = remainingAfter > 0 ? ` (+${remainingAfter} more)`.length : 0;
      if (shown.length > 0 && currentLen + sepLen + item.length + moreReserve > maxModifiedChars) {
        break;
      }
      shown.push(item);
      currentLen += sepLen + item.length;
    }
    const more =
      shown.length < statusLines.length
        ? ` (+${statusLines.length - shown.length} more)`
        : "";
    parts.push(`${prefix}${shown.join(", ")}${more}`);
  }
  if (logLines.length > 0) {
    parts.push(
      `Recent commits:\n${logLines.map((l) => `  - ${sliceSafeChars(l, 72)}`).join("\n")}`,
    );
  }
  return parts.join("\n");
}

const SNAPSHOT_COLLAPSE_DIRS = new Set([
  "tmp",
  "temp",
  "output",
  "outputs",
  "artifacts",
  "ssl",
  "SSL",
  "playwright-report",
  "test-results",
  "coverage",
  "logs",
]);

const SNAPSHOT_PRIORITY_SUBDIRS = new Set([
  "src",
  "app",
  "lib",
  "packages",
  "server",
  "web",
  "client",
  "api",
  "tests",
  "test",
  "scripts",
]);

function isIgnoredSnapshotSubdir(name: string): boolean {
  if (name.startsWith(".") || DEFAULT_IGNORE_DIRS.has(name)) return true;
  if (
    name === "playwright-report" ||
    name === "test-results" ||
    name.startsWith("dist-") ||
    name.startsWith("build-")
  ) {
    return true;
  }
  return false;
}

const MANIFEST_SKIP_SUBDIRS = new Set([
  "docs",
  "doc",
  "assets",
  "scripts",
  "test",
  "tests",
  "fixtures",
  "examples",
  ...SNAPSHOT_COLLAPSE_DIRS,
]);

function shortEntryName(name: string, maxLen = 28): string {
  return name.length > maxLen ? `${sliceSafeChars(name, maxLen - 3)}...` : name;
}

async function getTwoLevelTree(workspaceDir: string): Promise<string | null> {
  let topEntries;
  try {
    topEntries = await fs.readdir(workspaceDir, { withFileTypes: true });
  } catch {
    return null;
  }

  const filteredTop = topEntries
    .filter((e) => {
      if (e.name === ".DS_Store") return false;
      if (e.isDirectory()) {
        if (e.name.startsWith(".")) return false;
        if (DEFAULT_IGNORE_DIRS.has(e.name)) return false;
      }
      return true;
    })
    .sort((a, b) => {
      if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1;
      const aCollapse = a.isDirectory() && SNAPSHOT_COLLAPSE_DIRS.has(a.name);
      const bCollapse = b.isDirectory() && SNAPSHOT_COLLAPSE_DIRS.has(b.name);
      if (aCollapse !== bCollapse) return aCollapse ? 1 : -1;
      return a.name.localeCompare(b.name);
    });

  if (filteredTop.length === 0) return null;

  const lines: string[] = [];
  const maxTop = 20;
  const shownTop = filteredTop.slice(0, maxTop);

  const subResults = await Promise.all(
    shownTop.map(async (entry) => {
      if (!entry.isDirectory()) {
        return {
          name: shortEntryName(entry.name),
          isDir: false,
          collapsedCount: null as number | null,
          children: [] as string[],
          more: 0,
        };
      }
      try {
        const sub = await fs.readdir(path.join(workspaceDir, entry.name), {
          withFileTypes: true,
        });
        const valid = sub
          .filter(
            (s) =>
              s.name !== ".DS_Store" &&
              !(s.isDirectory() && isIgnoredSnapshotSubdir(s.name)),
          )
          .sort((a, b) => {
            if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1;
            if (a.isDirectory() && b.isDirectory()) {
              const aPrio = SNAPSHOT_PRIORITY_SUBDIRS.has(a.name);
              const bPrio = SNAPSHOT_PRIORITY_SUBDIRS.has(b.name);
              if (aPrio !== bPrio) return aPrio ? -1 : 1;
            }
            return a.name.localeCompare(b.name);
          });
        if (SNAPSHOT_COLLAPSE_DIRS.has(entry.name)) {
          return {
            name: `${entry.name}/`,
            isDir: true,
            collapsedCount: valid.length,
            children: [] as string[],
            more: 0,
          };
        }
        const maxSub = 6;
        const children = valid.slice(0, maxSub).map((s) => {
          const base = shortEntryName(s.name);
          return s.isDirectory() ? `${base}/` : base;
        });
        return {
          name: `${entry.name}/`,
          isDir: true,
          collapsedCount: null as number | null,
          children,
          more: Math.max(0, valid.length - maxSub),
        };
      } catch {
        return {
          name: `${entry.name}/`,
          isDir: true,
          collapsedCount: null as number | null,
          children: [],
          more: 0,
        };
      }
    }),
  );

  for (const item of subResults) {
    if (!item.isDir) {
      lines.push(`  ${item.name}`);
    } else if (item.collapsedCount !== null) {
      lines.push(`  ${item.name} (${item.collapsedCount} entries)`);
    } else if (item.children.length === 0) {
      lines.push(`  ${item.name}`);
    } else {
      const more = item.more > 0 ? `, +${item.more}` : "";
      lines.push(`  ${item.name} (${item.children.join(", ")}${more})`);
    }
  }

  if (filteredTop.length > maxTop) {
    lines.push(`  ... (+${filteredTop.length - maxTop} more top-level entries)`);
  }

  return `Layout (2 levels):\n${lines.join("\n")}`;
}

async function detectManifestsInDir(
  dirPath: string,
  prefix: string,
): Promise<string[]> {
  const hints: string[] = [];

  await Promise.all([
    // package.json
    (async () => {
      try {
        const raw = await fs.readFile(path.join(dirPath, "package.json"), "utf8");
        const pkg = JSON.parse(raw) as {
          name?: string;
          scripts?: Record<string, string>;
        };
        const scriptNames = pkg.scripts ? Object.keys(pkg.scripts).slice(0, 12) : [];
        const label = pkg.name
          ? `${prefix}package.json (${pkg.name})`
          : `${prefix}package.json`;
        hints.push(
          scriptNames.length > 0
            ? `${label} scripts: ${scriptNames.join(", ")}`
            : label,
        );
      } catch {
        // not present
      }
    })(),

    // Makefile (root only to avoid noisy nested Makefiles)
    (async () => {
      if (prefix) return;
      for (const name of ["Makefile", "makefile"]) {
        try {
          const raw = await fs.readFile(path.join(dirPath, name), "utf8");
          const targets: string[] = [];
          for (const line of raw.split(/\r?\n/)) {
            const m = /^([A-Za-z0-9_.-]+)\s*:(?!=)/.exec(line);
            if (m && m[1] && !m[1].startsWith(".")) {
              if (!targets.includes(m[1])) targets.push(m[1]);
              if (targets.length >= 10) break;
            }
          }
          hints.push(
            targets.length > 0
              ? `${name} targets: ${targets.join(", ")}`
              : name,
          );
          break;
        } catch {
          // not present
        }
      }
    })(),

    // pom.xml (Maven)
    (async () => {
      try {
        const raw = await fs.readFile(path.join(dirPath, "pom.xml"), "utf8");
        const stripped = raw.replace(/<parent>[\s\S]*?<\/parent>/i, "");
        const m = /<artifactId>\s*([^<\s]+)\s*<\/artifactId>/i.exec(stripped);
        hints.push(
          m?.[1] ? `${prefix}pom.xml (${m[1]}, maven)` : `${prefix}pom.xml (maven)`,
        );
      } catch {
        // not present
      }
    })(),

    // build.gradle / build.gradle.kts
    (async () => {
      for (const name of ["build.gradle.kts", "build.gradle"]) {
        try {
          await fs.access(path.join(dirPath, name));
          hints.push(`${prefix}${name} (gradle)`);
          break;
        } catch {
          // not present
        }
      }
    })(),

    // pyproject.toml
    (async () => {
      try {
        const raw = await fs.readFile(path.join(dirPath, "pyproject.toml"), "utf8");
        const tools: string[] = [];
        if (/\[tool\.pytest/.test(raw)) tools.push("pytest");
        if (/\[tool\.ruff/.test(raw)) tools.push("ruff");
        if (/\[tool\.mypy/.test(raw)) tools.push("mypy");
        if (/\[tool\.poetry/.test(raw)) tools.push("poetry");
        if (/\[tool\.uv/.test(raw)) tools.push("uv");
        hints.push(
          tools.length > 0
            ? `${prefix}pyproject.toml (${tools.join(", ")})`
            : `${prefix}pyproject.toml`,
        );
      } catch {
        // not present
      }
    })(),

    // Cargo.toml
    (async () => {
      try {
        const raw = await fs.readFile(path.join(dirPath, "Cargo.toml"), "utf8");
        const isWorkspace = /\[workspace\]/.test(raw);
        hints.push(
          isWorkspace ? `${prefix}Cargo.toml (workspace)` : `${prefix}Cargo.toml`,
        );
      } catch {
        // not present
      }
    })(),

    // go.mod
    (async () => {
      try {
        const raw = await fs.readFile(path.join(dirPath, "go.mod"), "utf8");
        const m = /^module\s+(\S+)/m.exec(raw);
        hints.push(m?.[1] ? `${prefix}go.mod (${m[1]})` : `${prefix}go.mod`);
      } catch {
        // not present
      }
    })(),
  ]);

  return hints;
}

async function getProjectManifestSummary(workspaceDir: string): Promise<string | null> {
  const rootHints = await detectManifestsInDir(workspaceDir, "");
  const allHints = [...rootHints];

  // Also inspect 1-level child subproject directories (e.g. server/, web/, desktop/, SourcingAgent/)
  try {
    const entries = await fs.readdir(workspaceDir, { withFileTypes: true });
    const subdirs = entries
      .filter(
        (e) =>
          e.isDirectory() &&
          !e.name.startsWith(".") &&
          !DEFAULT_IGNORE_DIRS.has(e.name) &&
          !MANIFEST_SKIP_SUBDIRS.has(e.name),
      )
      .sort((a, b) => a.name.localeCompare(b.name))
      .slice(0, 8);

    const nested = await Promise.all(
      subdirs.map((d) =>
        detectManifestsInDir(path.join(workspaceDir, d.name), `${d.name}/`),
      ),
    );
    for (const list of nested) {
      allHints.push(...list);
    }
  } catch {
    // ignore
  }

  if (allHints.length === 0) return null;
  allHints.sort();
  return `Project:\n${allHints.slice(0, 6).map((h) => `  - ${h}`).join("\n")}`;
}

function fitSectionToChars(
  section: string,
  budget: number,
  marker: string,
): string {
  if (!Number.isFinite(budget) || budget <= 0) return "";
  const limit = Math.trunc(budget);
  if (limit <= 0) return "";
  if (section.length <= limit) return section;
  if (limit <= marker.length + 1) {
    return sliceSafeChars(section, limit);
  }
  const cutoff = limit - marker.length - 1;
  let trimmed = sliceSafeChars(section, cutoff);
  const lastNl = trimmed.lastIndexOf("\n");
  if (lastNl > 0) {
    trimmed = trimmed.slice(0, lastNl);
  }
  return `${trimmed}\n${marker}`;
}

function fitSectionToBytes(
  section: string,
  budgetBytes: number,
  marker: string,
): string {
  if (!Number.isFinite(budgetBytes) || budgetBytes <= 0) return "";
  const limit = Math.trunc(budgetBytes);
  if (limit <= 0) return "";
  if (Buffer.byteLength(section, "utf8") <= limit) return section;
  const markerBytes = Buffer.byteLength(marker, "utf8");
  if (limit <= markerBytes + 1) {
    return sliceUtf8Bytes(section, limit);
  }
  const cutoffBytes = limit - markerBytes - 1;
  let trimmed = sliceUtf8Bytes(section, cutoffBytes);
  const lastNl = trimmed.lastIndexOf("\n");
  if (lastNl > 0) {
    trimmed = trimmed.slice(0, lastNl);
  }
  const result = `${trimmed}\n${marker}`;
  return Buffer.byteLength(result, "utf8") <= limit
    ? result
    : sliceUtf8Bytes(result, limit);
}

export async function buildWorkspaceSnapshot(
  workspaceDir: string,
  options: SnapshotOptions = {},
): Promise<string | null> {
  const maxChars =
    typeof options.maxChars === "number" && Number.isFinite(options.maxChars)
      ? Math.max(0, Math.trunc(options.maxChars))
      : 1500;
  const maxBytes =
    typeof options.maxBytes === "number" && Number.isFinite(options.maxBytes)
      ? Math.max(0, Math.trunc(options.maxBytes))
      : maxChars;
  const gitTimeoutMs = options.gitTimeoutMs ?? 800;

  try {
    const stat = await fs.stat(workspaceDir);
    if (!stat.isDirectory()) return null;
  } catch {
    return null;
  }

  const [gitSummary, treeSummary, projectSummary] = await Promise.all([
    getGitSummary(workspaceDir, gitTimeoutMs),
    getTwoLevelTree(workspaceDir),
    getProjectManifestSummary(workspaceDir),
  ]);

  const header = `[agy-fast startup snapshot] Workspace: ${workspaceDir}`;
  const sections = [header, gitSummary, projectSummary, treeSummary].filter(
    (s): s is string => Boolean(s),
  );

  if (sections.length <= 1) return null;

  const full = sections.join("\n");
  if (full.length <= maxChars && Buffer.byteLength(full, "utf8") <= maxBytes) {
    return full;
  }

  const suffix = "... [snapshot capped]";
  const headerBytes = Buffer.byteLength(header, "utf8");
  const suffixBytes = Buffer.byteLength(suffix, "utf8");
  if (maxChars <= header.length + suffix.length + 4 || maxBytes <= headerBytes + suffixBytes + 4) {
    const charCapped = fitSectionToChars(full, maxChars, suffix);
    return Buffer.byteLength(charCapped, "utf8") <= maxBytes
      ? charCapped
      : truncateWithByteNotice(charCapped, maxBytes);
  }

  // Allocate per-section UTF-8 byte budgets so a large dirty git status or CJK paths
  // cannot erase Project or Layout while keeping total output <= maxBytes and <= maxChars.
  const remainingAfterHeader = maxBytes - headerBytes;
  const nonEmptyCount =
    (gitSummary ? 1 : 0) + (projectSummary ? 1 : 0) + (treeSummary ? 1 : 0);
  const availableForBodies = remainingAfterHeader - nonEmptyCount; // reserve \n separators

  let projectPart = "";
  let gitPart = "";
  let treePart = "";
  let budgetLeft = availableForBodies;

  if (projectSummary) {
    const otherPresent = Boolean(gitSummary || treeSummary);
    const projectBytes = Buffer.byteLength(projectSummary, "utf8");
    const projectCap = otherPresent
      ? Math.min(projectBytes, Math.max(80, Math.floor(availableForBodies * 0.25)))
      : budgetLeft;
    projectPart = fitSectionToBytes(
      projectSummary,
      Math.min(budgetLeft, projectCap),
      "  ... [project capped]",
    );
    budgetLeft -= Buffer.byteLength(projectPart, "utf8");
  }

  if (gitSummary && budgetLeft > 0) {
    const gitBytes = Buffer.byteLength(gitSummary, "utf8");
    const gitCap = treeSummary
      ? Math.min(gitBytes, Math.max(100, Math.floor(budgetLeft * 0.48)))
      : budgetLeft;
    gitPart = fitSectionToBytes(
      gitSummary,
      Math.min(budgetLeft, gitCap),
      "  ... [git capped]",
    );
    budgetLeft -= Buffer.byteLength(gitPart, "utf8");
  }

  if (treeSummary && budgetLeft > 0) {
    treePart = fitSectionToBytes(treeSummary, budgetLeft, suffix);
    budgetLeft -= Buffer.byteLength(treePart, "utf8");
  }

  let assembled = [header, gitPart, projectPart, treePart]
    .filter(Boolean)
    .join("\n");
  if (assembled.length > maxChars) {
    assembled = fitSectionToChars(assembled, maxChars, suffix);
  }
  if (Buffer.byteLength(assembled, "utf8") > maxBytes) {
    assembled = fitSectionToBytes(assembled, maxBytes, suffix);
  }
  return assembled;
}
