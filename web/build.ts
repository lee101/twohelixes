/**
 * Bun build. Emits into ../static, which the server serves directly.
 *
 * Plotly is heavy, so it is split into its own chunk: the marketing pages and
 * the SQL editor never load it.
 */
import { access, copyFile, mkdir, readdir, rm } from "node:fs/promises";

const outdir = new URL("../static/", import.meta.url).pathname;
const watch = process.argv.includes("--watch");

await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });

// Two passes, and the stylesheet is called styles.css rather than app.css:
// the JS build derives its CSS output name from its entry (app.ts -> app.css),
// which silently overwrote the real stylesheet and dropped every rule.
const cssResult = await Bun.build({
  entrypoints: ["./src/styles.css"],
  outdir,
  minify: !watch,
  naming: { entry: "[name].[ext]" },
});
if (!cssResult.success) {
  for (const log of cssResult.logs) console.error(log);
  process.exit(1);
}

// The analytics tracker is its own IIFE bundle: it must load on other people's
// sites with a plain <script async>, so it cannot be an ES module chunk.
const trackerResult = await Bun.build({
  entrypoints: ["./src/th.js"],
  outdir,
  target: "browser",
  format: "iife",
  minify: true,
  naming: { entry: "[name].[ext]" },
});
if (!trackerResult.success) {
  for (const log of trackerResult.logs) console.error(log);
  process.exit(1);
}

const result = await Bun.build({
  entrypoints: ["./src/app.ts", "./src/marketing.ts"],
  outdir,
  target: "browser",
  format: "esm",
  splitting: true,
  minify: !watch,
  sourcemap: watch ? "inline" : "none",
  // Plotly ships its own stylesheet; without a distinct asset name it
  // collides with app.css under a flat [name].[ext] scheme.
  naming: {
    entry: "[name].[ext]",
    chunk: "chunk-[hash].[ext]",
    asset: "asset-[name]-[hash].[ext]",
  },
  define: {
    "process.env.NODE_ENV": watch ? '"development"' : '"production"',
    // plotly.js is published as CommonJS-ish source and reaches for Node's
    // `global`. Without this it throws "global is not defined" at import and
    // every chart silently fails to render.
    global: "globalThis",
  },
});

if (!result.success) {
  for (const log of result.logs) console.error(log);
  process.exit(1);
}

// Keeping the worker in a separate build prevents DuckDB and Arrow from
// becoming shared dependencies of the initial application chunk.
const workerResult = await Bun.build({
  entrypoints: ["./src/reshape/duckdb.worker.ts"],
  outdir,
  target: "browser",
  format: "esm",
  splitting: true,
  minify: !watch,
  sourcemap: watch ? "inline" : "none",
  naming: {
    entry: "[name].[ext]",
    chunk: "reshape-chunk-[hash].[ext]",
    asset: "reshape-asset-[name]-[hash].[ext]",
  },
});
if (!workerResult.success) {
  for (const log of workerResult.logs) console.error(log);
  process.exit(1);
}

const duckdbDist = new URL(
  "./node_modules/@duckdb/duckdb-wasm/dist/",
  import.meta.url,
).pathname;
const duckdbAssets = [
  "duckdb-mvp.wasm",
  "duckdb-browser-mvp.worker.js",
] as const;
const duckdbOut = outdir + "duckdb/";
await mkdir(duckdbOut, { recursive: true });
for (const name of duckdbAssets) {
  const source = duckdbDist + name;
  try {
    await access(source);
  } catch {
    throw new Error(`required self-hosted DuckDB asset is missing: ${source}`);
  }
  await copyFile(source, duckdbOut + name);
}

// Static art plates are copied verbatim; Bun does not bundle them.
const assets = new URL("./assets/", import.meta.url).pathname;
try {
  const names = await readdir(assets);
  await mkdir(outdir + "art", { recursive: true });
  for (const name of names) await copyFile(assets + name, outdir + "art/" + name);
  console.log(`copied ${names.length} art files`);
} catch { /* no assets is fine */ }

const outputs = [...cssResult.outputs, ...result.outputs, ...workerResult.outputs];
const total = outputs.reduce((n, o) => n + o.size, 0);
console.log(
  `built ${outputs.length} files, ${(total / 1024).toFixed(1)} kB -> ${outdir}`,
);
console.log(`copied ${duckdbAssets.length} self-hosted DuckDB files`);
for (const out of outputs) {
  console.log(`  ${out.path.split("/").pop()}  ${(out.size / 1024).toFixed(1)} kB`);
}
