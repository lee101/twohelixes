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

const result = await Bun.build({
  entrypoints: ["./src/app.ts", "./src/app.css"],
  outdir,
  target: "browser",
  format: "esm",
  splitting: true,
  minify: !watch,
  sourcemap: watch ? "inline" : "none",
  naming: { entry: "[name].[ext]", chunk: "chunk-[hash].[ext]" },
  define: { "process.env.NODE_ENV": watch ? '"development"' : '"production"' },
});

if (!result.success) {
  for (const log of result.logs) console.error(log);
  process.exit(1);
}

const total = result.outputs.reduce((n, o) => n + o.size, 0);
console.log(
  `built ${result.outputs.length} files, ${(total / 1024).toFixed(1)} kB -> ${outdir}`,
);
for (const out of result.outputs) {
  console.log(`  ${out.path.split("/").pop()}  ${(out.size / 1024).toFixed(1)} kB`);
}
