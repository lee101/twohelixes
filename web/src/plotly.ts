/**
 * A Plotly bundle carrying exactly the trace types this product draws.
 *
 * We shipped `plotly.js-basic-dist-min` first, which contains only scatter,
 * bar and pie. Everything else silently drew nothing: a box plot rendered as
 * lines, a bar chart came out empty, and sankey/treemap/funnel/waterfall had
 * no renderer at all. Nothing errored - Plotly just ignores a trace type it
 * does not know, which is the worst possible failure mode.
 *
 * The full `plotly.js-dist` is ~3.5 MB. Registering the twelve traces we
 * actually support keeps it far smaller, and the import is lazy so no one
 * pays for it until a chart is drawn.
 *
 * If you add a chart form in `charts/forms.py`, register its trace here or it
 * will render as nothing.
 */

import Plotly from "plotly.js/lib/core";

import bar from "plotly.js/lib/bar";
import box from "plotly.js/lib/box";
import funnel from "plotly.js/lib/funnel";
import heatmap from "plotly.js/lib/heatmap";
import histogram from "plotly.js/lib/histogram";
import indicator from "plotly.js/lib/indicator";
import pie from "plotly.js/lib/pie";
import sankey from "plotly.js/lib/sankey";
import table from "plotly.js/lib/table";
import treemap from "plotly.js/lib/treemap";
import waterfall from "plotly.js/lib/waterfall";

// `scatter` is in core already; the rest have to be registered explicitly.
Plotly.register([
  bar,
  box,
  funnel,
  heatmap,
  histogram,
  indicator,
  pie,
  sankey,
  table,
  treemap,
  waterfall,
]);

/** Trace types this bundle can actually draw. */
export const SUPPORTED_TRACES = new Set([
  "scatter",
  "scattergl",
  "bar",
  "box",
  "funnel",
  "heatmap",
  "histogram",
  "indicator",
  "pie",
  "sankey",
  "table",
  "treemap",
  "waterfall",
]);

export default Plotly;
