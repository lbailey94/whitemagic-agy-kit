import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, it } from "node:test";
import {
  HOST_COMPAT_RULES,
  compareHostSemver,
  detectHostIdentity,
  formatHostSummary,
  formatSessionStartCompatNotice,
  parseHostOverride,
  parseHostSemver,
  readSavedEditsFormat,
  readSavedEditsFormatSync,
  resolveHostCompat,
  type HostCompatRule,
} from "../src/utils/host-compat.js";

describe("host-compat version & surface controller", () => {
  it("parses and compares host semver tuples (including 4-part & prerelease) while rejecting invalid strings", () => {
    assert.deepEqual(parseHostSemver("cli-1.5.13"), [1, 5, 13]);
    assert.deepEqual(parseHostSemver("cli-1.5.13.4"), [1, 5, 13]);
    assert.deepEqual(parseHostSemver("v2.34.0-beta.1"), [2, 34, 0]);
    assert.deepEqual(parseHostSemver("ide/2.7.0+build.4"), [2, 7, 0]);
    assert.equal(parseHostSemver("1.2.3.4.5"), null);
    assert.equal(parseHostSemver("1.5"), null);
    assert.equal(parseHostSemver("9999999.0.0"), null);

    assert.equal(compareHostSemver("1.5.12", "1.5.14"), -1);
    assert.equal(compareHostSemver("1.5.14", "1.5.14"), 0);
    assert.equal(compareHostSemver("1.5.14", "1.5.2"), 1);
    assert.equal(compareHostSemver("2.0.0", "1.99.99"), 1);
    assert.equal(compareHostSemver("1.5.16-rc.1", "1.5.16"), -1);
    assert.equal(compareHostSemver("1.5.16-rc.2", "1.5.16-rc.1"), 1);
  });

  it("detects CLI, Desktop 2.0, IDE, and unknown hosts without cross-surface contamination", () => {
    // Antigravity CLI via ANTIGRAVITY_LS_VERSION
    const cliLs = detectHostIdentity({ env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.13" } });
    assert.equal(cliLs.surface, "cli");
    assert.equal(cliLs.version, "1.5.13");
    assert.equal(cliLs.source, "env:ls-version");

    // Antigravity CLI via MCP clientInfo
    const cliClient = detectHostIdentity({
      clientInfo: { name: "antigravity-cli", version: "1.5.14" },
      env: {},
    });
    assert.equal(cliClient.surface, "cli");
    assert.equal(cliClient.version, "1.5.14");
    assert.equal(cliClient.source, "mcp:client-info");

    // Antigravity Desktop 2.0 via SessionStart transcriptPath + Windows backslash path
    const deskWin = detectHostIdentity({
      transcriptPath: "C:\\Users\\dev\\.gemini\\antigravity\\brain\\abc\\transcript.jsonl",
      env: { ANTIGRAVITY_LS_VERSION: "desktop-2.34.0" },
    });
    assert.equal(deskWin.surface, "desktop");
    assert.equal(deskWin.version, "2.34.0");
    assert.equal(deskWin.source, "hook:path");

    // Antigravity IDE via hook path MUST NOT inherit conflicting CLI LS version from parent shell
    const ideCross = detectHostIdentity({
      transcriptPath: "/Users/dev/.gemini/antigravity-ide/brain/abc/transcript.jsonl",
      env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.13" },
    });
    assert.equal(ideCross.surface, "ide");
    assert.equal(ideCross.version, null);
    assert.equal(ideCross.source, "hook:path");

    // Workspace folders literally named "antigravity", "antigravity-cli", or "antigravity-ide" must NOT misfire
    for (const fakePath of [
      "/Users/dev/projects/antigravity/logs/transcript.jsonl",
      "/Users/dev/projects/antigravity-cli/logs/transcript.jsonl",
      "/Users/dev/projects/antigravity-ide/logs/transcript.jsonl",
    ]) {
      const fakeWorkspace = detectHostIdentity({
        transcriptPath: fakePath,
        env: {},
      });
      assert.equal(fakeWorkspace.surface, "unknown", `False positive for ${fakePath}`);
      assert.equal(fakeWorkspace.version, null);
    }
    // Workspace folder named antigravity-ide must NOT suppress real ambient CLI LS version
    const realCliInIdeFolder = detectHostIdentity({
      transcriptPath: "/Users/dev/projects/antigravity-ide/logs/transcript.jsonl",
      env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.13" },
    });
    assert.equal(realCliInIdeFolder.surface, "cli");
    assert.equal(realCliInIdeFolder.version, "1.5.13");

    // Non-Antigravity clients must NEVER inherit ambient ANTIGRAVITY_LS_VERSION from an agy shell
    for (const name of ["claude-code", "cursor", "vscode", "mcp-inspector"]) {
      const id = detectHostIdentity({
        clientInfo: { name, version: "1.5.13" },
        env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.13" },
      });
      assert.equal(id.surface, "unknown", `Unexpected match for ${name}`);
      assert.equal(id.version, null);
    }
    const doctorId = detectHostIdentity({
      clientInfo: { name: "agy-fast-doctor", version: "1.5.13" },
      env: {},
    });
    assert.equal(doctorId.surface, "unknown");
    assert.equal(doctorId.version, null);

    // Explicit AGY_FAST_HOST override
    assert.deepEqual(parseHostOverride("cli@1.5.14"), { surface: "cli", version: "1.5.14" });
    assert.deepEqual(parseHostOverride("cli@1.5.13.4"), { surface: "cli", version: "1.5.13" });
    assert.deepEqual(parseHostOverride("app-2.34.0"), { surface: "desktop", version: "2.34.0" });
    assert.deepEqual(parseHostOverride("antigravity@2.34.0"), { surface: "desktop", version: "2.34.0" });
    assert.deepEqual(parseHostOverride("ide"), { surface: "ide", version: null });
    assert.equal(parseHostOverride("cli@bogus"), null);
    assert.equal(parseHostOverride("invalid-host"), null);
  });

  it("biases toward token-saving native arrays on eager-capable hosts (CLI >=1.5.8, Desktop, IDE) and activates JSON string on pre-eager CLI 1.5.0-1.5.7", () => {
    // Pre-eager CLI 1.5.0-1.5.7 (and 1.5.8 prereleases) -> JSON array string
    for (const ver of ["1.5.0", "1.5.4", "1.5.7", "1.5.8-rc.1"]) {
      const c = resolveHostCompat({ env: { ANTIGRAVITY_LS_VERSION: `cli-${ver}` } });
      assert.equal(c.editsFormat, "string");
      assert.equal(c.editsFormatSource, "compat-rule");
      assert.deepEqual(c.matchedRuleIds, ["agy-cli-1.5-call-mcp-tool-empty-edits"]);
      const notice = formatSessionStartCompatNotice(c);
      assert.ok(notice && notice.includes(`host=cli@${ver}`) && notice.includes("JSON array string"));
    }

    // Token-saving default (native array, 0 extra SessionStart bytes) on eager-capable CLI >=1.5.8,
    // Desktop 2.0, IDE, older CLI <1.5.0, unknown CLI version, and unknown hosts
    const tokenSavingCases = [
      { env: {} },
      { env: { ANTIGRAVITY_LS_VERSION: "cli-1.4.9" } },
      { env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.8" } },
      { env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.13" } },
      { env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.14" } },
      { transcriptPath: "/Users/dev/.gemini/antigravity-cli/brain/id/transcript.jsonl", env: {} },
      { clientInfo: { name: "Antigravity 2.0", version: "2.34.0" }, env: {} },
      { clientInfo: { name: "antigravity-ide", version: "2.7.0" }, env: {} },
      { clientInfo: { name: "claude-code", version: "1.5.7" }, env: {} },
    ];
    for (const sig of tokenSavingCases) {
      const c = resolveHostCompat(sig);
      assert.equal(c.editsFormat, "array", `Expected array for ${JSON.stringify(sig)}`);
      assert.equal(c.editsFormatSource, "token-saving-default");
      assert.equal(formatSessionStartCompatNotice(c), null);
    }

    // Custom rule table upper-bound (incl. prerelease) retires cleanly via maxVersionExclusive
    const futureRules: HostCompatRule[] = [
      { ...HOST_COMPAT_RULES[0]!, maxVersionExclusive: "1.5.16" },
    ];
    assert.equal(
      resolveHostCompat({ env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.15" } }, futureRules).editsFormat,
      "string",
    );
    assert.equal(
      resolveHostCompat({ env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.16-rc.1" } }, futureRules).editsFormat,
      "string",
    );
    assert.equal(
      resolveHostCompat({ env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.16" } }, futureRules).editsFormat,
      "array",
    );
  });

  it("enforces override precedence (AGY_FAST_EDITS_FORMAT > config.json > compat rules) and reads saved config", async () => {
    // Env override beats pre-eager CLI compat rule
    const forceArray = resolveHostCompat({
      env: { ANTIGRAVITY_LS_VERSION: "cli-1.5.7", AGY_FAST_EDITS_FORMAT: "array" },
      configEditsFormat: "string",
    });
    assert.equal(forceArray.editsFormat, "array");
    assert.equal(forceArray.editsFormatSource, "env-override");

    // Env "auto" explicitly overrides saved config.json "string" back to rule evaluation
    const envAutoOverridesCfg = resolveHostCompat({
      clientInfo: { name: "antigravity-ide", version: "2.7.0" },
      env: { AGY_FAST_EDITS_FORMAT: "auto" },
      configEditsFormat: "string",
    });
    assert.equal(envAutoOverridesCfg.editsFormat, "array");
    assert.equal(envAutoOverridesCfg.editsFormatSource, "token-saving-default");

    // Config override applies when env is unset
    const cfgString = resolveHostCompat({
      clientInfo: { name: "antigravity-ide", version: "2.7.0" },
      env: {},
      configEditsFormat: "string",
    });
    assert.equal(cfgString.editsFormat, "string");
    assert.equal(cfgString.editsFormatSource, "config-override");
    assert.match(formatHostSummary(cfgString), /ide@2\.7\.0 -> apply_edits\.edits=string \(config-override\)/);

    // Persisted config.json read helpers (sync & async)
    const tmpCache = await fs.mkdtemp(path.join(os.tmpdir(), "agy-fast-compat-cfg-"));
    try {
      const env = { AGY_FAST_CACHE_DIR: tmpCache };
      assert.equal(readSavedEditsFormatSync(env), undefined);
      assert.equal(await readSavedEditsFormat(env), undefined);

      await fs.writeFile(
        path.join(tmpCache, "config.json"),
        JSON.stringify({ editsFormat: "STRING" }),
        "utf8",
      );
      assert.equal(readSavedEditsFormatSync(env), "string");
      assert.equal(await readSavedEditsFormat(env), "string");
    } finally {
      await fs.rm(tmpCache, { recursive: true, force: true });
    }
  });
});
