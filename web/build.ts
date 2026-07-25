/**
 * Bun build. Emits into ../static, which the server serves directly.
 *
 * Plotly is heavy, so it is split into its own chunk: the marketing pages and
 * the SQL editor never load it.
 */
import { rm, mkdir } from "node:fs/promises";

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

// Static art plates are copied verbatim; Bun does not bundle them.
const assets = new URL("./assets/", import.meta.url).pathname;
const { readdir, copyFile, mkdir: mk } = await import("node:fs/promises");
try {
  const names = await readdir(assets);
  await mk(outdir + "art", { recursive: true });
  for (const name of names) await copyFile(assets + name, outdir + "art/" + name);
  console.log(`copied ${names.length} art files`);
} catch { /* no assets is fine */ }

const outputs = [...cssResult.outputs, ...result.outputs];
const total = outputs.reduce((n, o) => n + o.size, 0);
console.log(
  `built ${outputs.length} files, ${(total / 1024).toFixed(1)} kB -> ${outdir}`,
);
for (const out of outputs) {
  console.log(`  ${out.path.split("/").pop()}  ${(out.size / 1024).toFixed(1)} kB`);
}
