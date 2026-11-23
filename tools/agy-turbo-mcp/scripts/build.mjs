import { build } from "esbuild";
import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const distDir = path.resolve(rootDir, "..", "..", "plugin", "dist");

const entrypoints = [
  {
    in: path.join(rootDir, "src", "mcp-server.ts"),
    out: path.join(distDir, "mcp-server.mjs"),
    banner: "// whitemagic-agy-kit Fused Turbo+Fast MCP Stdio Server (bundled zero-dependency ESM)\n",
  },
  {
    in: path.join(rootDir, "src", "benchmark.ts"),
    out: path.join(distDir, "benchmark.mjs"),
    banner: "#!/usr/bin/env node\n// whitemagic-agy-kit Offline Transcript Benchmark CLI\n",
  },
];

await fs.mkdir(distDir, { recursive: true });

for (const entry of entrypoints) {
  await build({
    entryPoints: [entry.in],
    outfile: entry.out,
    bundle: true,
    platform: "node",
    target: "node20",
    format: "esm",
    minify: false,
    sourcemap: false,
    legalComments: "none",
    banner: { js: entry.banner },
  });
  const stat = await fs.stat(entry.out);
  const rel = path.relative(rootDir, entry.out);
  console.log(`Built ${rel} (${(stat.size / 1024).toFixed(1)} KB)`);
}
