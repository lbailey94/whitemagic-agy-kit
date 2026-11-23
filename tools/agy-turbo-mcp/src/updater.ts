import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  parseUpdateChannel,
  runPluginUpdate,
  type UpdateChannel,
} from "./utils/updater-core.js";

function parseCliArgs(argv: string[]): {
  pluginDir: string;
  background: boolean;
  checkOnly: boolean;
  force: boolean;
  json: boolean;
  channel?: UpdateChannel;
  repoUrl?: string;
} {
  const args = argv.slice(2);
  const defaultPluginDir = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "..",
  );
  let pluginDir = defaultPluginDir;
  let background = false;
  let checkOnly = false;
  let force = false;
  let json = false;
  let channel: UpdateChannel | undefined;
  let repoUrl: string | undefined;

  for (let i = 0; i < args.length; i++) {
    const a = args[i]!;
    if (a === "--background") {
      background = true;
    } else if (a === "--check") {
      checkOnly = true;
    } else if (a === "--force") {
      force = true;
    } else if (a === "--json") {
      json = true;
    } else if (a === "--plugin-dir" && args[i + 1]) {
      pluginDir = path.resolve(args[++i]!);
    } else if (a === "--channel" && args[i + 1]) {
      const parsed = parseUpdateChannel(args[++i]);
      if (!parsed) throw new Error(`Invalid --channel value (expected 'release' or 'main')`);
      channel = parsed;
    } else if (a === "--repo" && args[i + 1]) {
      repoUrl = args[++i]!;
    }
  }

  return { pluginDir, background, checkOnly, force, json, channel, repoUrl };
}

async function main(): Promise<void> {
  const opts = parseCliArgs(process.argv);
  const result = await runPluginUpdate({
    pluginDir: opts.pluginDir,
    background: opts.background,
    checkOnly: opts.checkOnly,
    force: opts.force,
    channel: opts.channel,
    repoUrl: opts.repoUrl,
  });

  if (opts.background) {
    return;
  }

  if (opts.json) {
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } else {
    process.stdout.write(`${result.message}\n`);
  }

  if (result.status === "error") {
    process.exitCode = 1;
  }
}

main().catch((err) => {
  if (!process.argv.includes("--background")) {
    process.stderr.write(
      `Error: ${err instanceof Error ? err.message : String(err)}\n`,
    );
    process.exitCode = 1;
  }
});
