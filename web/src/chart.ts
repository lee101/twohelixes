/**
 * Chart rendering plus the manual control panel.
 *
 * The controls exist because the agent's choice is a starting point, not a
 * verdict: swapping x and y, changing the chart type, or regrouping by a
 * different column must not require another model call. Only "re-run the
 * query" costs a query.
 */

import { api, type ChartConfig, type PipelineResult, type PlotlyFigure } from "./api";

type Plotly = typeof import("./plotly").default;
let plotlyPromise: Promise<Plotly> | null = null;

/** Plotly is large; load it only when a chart is actually rendered. */
function loadPlotly(): Promise<Plotly> {
  if (!plotlyPromise) {
    plotlyPromise = import("./plotly").then((m) => m.default as Plotly);
  }
  return plotlyPromise;
}

/**
 * Every form the pipeline can draw, in the order a person looks for them.
 *
 * This list used to stop at eleven while `figures.VALID_TYPES` had nineteen,
 * so the agent could choose a treemap or a sankey and the reader had no way to
 * ask for one - or to get back to one after changing the type. Forms that need
 * a channel the data does not have are downgraded by `validate_config` with a
 * note, which is the same thing that happens when the agent picks them.
 */
export const CHART_TYPES = [
  "line",
  "area",
  "bar",
  "hbar",
  "scatter",
  "bubble",
  "pie",
  "histogram",
  "box",
  "heatmap",
  "candlestick",
  "waterfall",
  "funnel",
  "treemap",
  "sunburst",
  "sankey",
  "map",
  "stat",
  "table",
] as const;

const AGGREGATIONS = ["sum", "mean", "median", "count", "min", "max"] as const;

export function currentMode(): "light" | "dark" {
  const stamped = document.documentElement.getAttribute("data-theme");
  if (stamped === "dark" || stamped === "light") return stamped;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export async function renderFigure(
  host: HTMLElement,
  figure: PlotlyFigure,
): Promise<void> {
  const Plotly = await loadPlotly();
  const { SUPPORTED_TRACES } = await import("./plotly");

  // Plotly ignores an unregistered trace type without erroring, which reads
  // as "the chart is broken" rather than "this build cannot draw that".
  const unknown = [
    ...new Set(
      (figure.data ?? [])
        .map((t) => String((t as any).type ?? "scatter"))
        .filter((t) => !SUPPORTED_TRACES.has(t)),
    ),
  ];
  if (unknown.length) {
    console.warn("twoHelixes: unregistered trace type(s)", unknown);
  }
  const layout = fitTop({ ...figure.layout, autosize: true }, figure.data ?? [], host.clientWidth);
  const config = { ...(figure.config ?? {}), responsive: true, displaylogo: false };

  // Plotly.react reuses the existing plot, which is what makes a redraw
  // cheap - but it cannot cross between forms that have cartesian axes and
  // forms that do not. Going from a treemap, sunburst or choropleth to a
  // scatter threw "Cannot read properties of undefined (reading 'range')" and
  // left the card empty, which is reachable from the controls in two clicks.
  // Purge when the trace types change; keep react for every ordinary redraw.
  const kinds = (figure.data ?? []).map((t) => String((t as any).type ?? "scatter")).join(",");
  const previous = (host as HTMLElement).dataset.thKinds;
  if (previous !== undefined && previous !== kinds) {
    Plotly.purge(host);
  }
  (host as HTMLElement).dataset.thKinds = kinds;

  await Plotly.react(host, figure.data as any, layout as any, config as any);
  await separateTitleAndLegend(Plotly, host);
}

/**
 * Push the plot down if the legend landed on the title.
 *
 * The estimate in `fitTop` gets the top margin close, but it is arithmetic
 * over guessed font metrics and it was still off by about twenty pixels for a
 * two-row legend. Measuring beats guessing: one relayout after the first draw
 * costs nothing and is right for any font, any zoom and any language.
 */
async function separateTitleAndLegend(Plotly: any, host: HTMLElement): Promise<void> {
  const title = host.querySelector<SVGGraphicsElement>(".infolayer .g-gtitle");
  const legend = host.querySelector<SVGGraphicsElement>(".infolayer .legend");
  if (!title || !legend) return;

  const titleBox = title.getBoundingClientRect();
  const legendBox = legend.getBoundingClientRect();
  if (!titleBox.height || !legendBox.height) return;

  const overlap = titleBox.bottom + 6 - legendBox.top;
  if (overlap <= 0) return;

  const current = (host as any)._fullLayout?.margin?.t ?? 56;
  await Plotly.relayout(host, { "margin.t": Math.ceil(current + overlap) });
}

/**
 * Make the top of the chart fit the width it is actually drawn at.
 *
 * Two things live in the top margin and neither is measured by the server,
 * which does not know the viewport: the title, which Plotly clips rather than
 * wraps, and the legend, which Plotly wraps onto as many rows as it needs and
 * then draws straight over the title. Our titles state the finding rather
 * than the column names, so on a phone both happen at once - the sentence
 * loses the words carrying its point, and a three-series legend lands on top
 * of what is left.
 *
 * So the wrap and the legend rows are estimated here from the same width, and
 * the top margin is sized for both together.
 */
function fitTop(
  layout: Record<string, any>,
  data: Record<string, unknown>[],
  width: number,
): Record<string, any> {
  if (!width) return layout;

  const title = layout.title;
  const text: string = typeof title === "string" ? title : (title?.text ?? "");
  const size: number = (typeof title === "object" && title?.font?.size) || 16;
  const margin = layout.margin ?? {};
  const usable = width - (margin.l ?? 56) - (margin.r ?? 24);

  const lines = text && !text.includes("<br>") ? wrap(text, usable, size) : [text];
  const rows = layout.showlegend ? legendRows(data, usable) : 0;
  if (lines.length < 2 && rows < 2) return layout;

  // Pad, the line boxes at Inter's 1.35 leading, and a gap before the legend.
  const titleBlock = text ? 12 + lines.length * size * 1.35 + 8 : 8;
  return {
    ...layout,
    title: {
      ...(typeof title === "object" ? title : {}),
      text: lines.join("<br>"),
      yref: "container",
      y: 1,
      yanchor: "top",
      pad: { ...(title?.pad ?? {}), t: 12 },
    },
    margin: { ...margin, t: Math.max(margin.t ?? 0, titleBlock + rows * LEGEND_ROW_PX) },
  };
}

/** Height of one horizontal legend row, matching charts/defaults.py. */
const LEGEND_ROW_PX = 24;

function wrap(text: string, usable: number, size: number): string[] {
  // Inter at 600 weight averages a little over half the point size per glyph.
  const perLine = Math.max(16, Math.floor(usable / (size * 0.53)));
  if (text.length <= perLine) return [text];

  const lines: string[] = [];
  let current = "";
  for (const word of text.split(" ")) {
    if (current && (current + " " + word).length > perLine) {
      lines.push(current);
      current = word;
    } else {
      current = current ? `${current} ${word}` : word;
    }
  }
  if (current) lines.push(current);
  return lines;
}

/**
 * How many rows Plotly will need for the horizontal legend. Approximate by
 * construction - the swatch, the gap and the glyph width are all estimates -
 * but wrong by a row is a tidy chart, while not estimating at all is a legend
 * printed over the title.
 */
function legendRows(data: Record<string, unknown>[], usable: number): number {
  const names = data
    .filter((trace) => trace.showlegend !== false && trace.name)
    .map((trace) => String(trace.name));
  if (!names.length || !usable) return 0;

  const SWATCH = 34;
  const GLYPH = 6.4;
  let rows = 1;
  let used = 0;
  for (const name of names) {
    const itemWidth = SWATCH + name.length * GLYPH;
    if (used && used + itemWidth > usable) {
      rows += 1;
      used = itemWidth;
    } else {
      used += itemWidth;
    }
  }
  return rows;
}

export interface ChartViewOptions {
  onConfigChange?: (config: ChartConfig) => void;
  onRerun?: (request: string) => void;
  /** Offer "Add to dashboard". Absent when there is nowhere to add it to. */
  onPin?: (result: PipelineResult) => void;
  chartId?: string;
  /** What was asked, so an export carries the question with it. */
  question?: () => string;
}

export class ChartView {
  readonly root: HTMLElement;
  private readonly plot: HTMLElement;
  private readonly controls: HTMLElement;
  private readonly notes: HTMLElement;
  private result: PipelineResult | null = null;

  constructor(private readonly options: ChartViewOptions = {}) {
    this.root = el("section", "chart-view");
    this.plot = el("div", "chart-plot");
    this.controls = el("div", "chart-controls");
    this.notes = el("div", "chart-notes");

    const header = el("header", "chart-header");
    header.append(this.notes);

    this.root.append(header, this.plot, this.controls);
  }

  async show(result: PipelineResult): Promise<void> {
    this.result = result;
    // A Plotly rejection used to take the notes and controls down with it, so
    // a browser-side render failure looked like the whole answer was lost. The
    // data, the warnings and the export buttons are still worth having.
    let renderError = "";
    try {
      await renderFigure(this.plot, result.figure);
    } catch (err) {
      renderError = err instanceof Error ? err.message : String(err);
      console.error("twoHelixes: chart render failed", err);
    }
    this.renderNotes(result, renderError);
    this.renderControls(result);
  }

  private renderNotes(result: PipelineResult, renderError = ""): void {
    this.notes.replaceChildren();

    if (renderError) {
      const note = el("p", "note note-warn");
      note.textContent =
        `This browser could not draw the chart (${renderError}). The data and exports below still work.`;
      this.notes.append(note);
    }

    const meta = el("div", "chart-meta");
    meta.textContent =
      `${result.row_count.toLocaleString()} rows · ${formatDuration(result.elapsed_ms)}`;
    this.notes.append(meta);

    for (const warning of result.warnings ?? []) {
      const note = el("p", "note note-warn");
      note.textContent = warning;
      this.notes.append(note);
    }
    for (const finding of result.audit ?? []) {
      const note = el("p", "note note-audit");
      note.textContent = `${finding.code}: ${finding.detail}`;
      this.notes.append(note);
    }

    const actions = el("div", "chart-actions");
    if (this.options.onPin) {
      const pin = button("Add to dashboard", () => this.options.onPin?.(result));
      pin.classList.add("btn-primary", "btn-small");
      actions.append(pin);
    }
    // Four export buttons is a row on a laptop and a wall on a phone, so the
    // exports fold into a disclosure and the one action that changes something
    // stays in the open.
    const exports = document.createElement("details");
    exports.className = "chart-exports";
    // Open where there is room. Folding four buttons away on a laptop hides
    // them for no gain; on a phone they were a wall between the chart and the
    // control most people want, which is changing it in words.
    exports.open = window.innerWidth >= 1080;
    const summary = document.createElement("summary");
    summary.textContent = "Export";
    exports.append(
      summary,
      button("SVG", () => this.export("svg")),
      button("PNG", () => this.export("png")),
      button("Notebook (.ipynb)", () => this.exportNotebook()),
      button("Copy data", () => this.copyData()),
    );
    actions.append(exports);
    this.notes.append(actions);
  }

  /**
   * Build the control row. Every column is offered on every channel: the
   * server validates and repairs the combination, so an impossible pick comes
   * back as an explained downgrade rather than a broken chart.
   */
  private renderControls(result: PipelineResult): void {
    this.controls.replaceChildren();
    const columns = result.columns ?? [];
    const config = result.config ?? ({} as ChartConfig);

    const update = (patch: Partial<ChartConfig>) => {
      const next = { ...config, ...patch };
      this.options.onConfigChange?.(next);
    };

    // The seven channel pickers are the power tool, not the first thing to
    // read. On a phone they were a screenful between the chart and the "change
    // it in words" box, which is the control most people actually want.
    const panel = document.createElement("details");
    panel.className = "chart-controls-panel";
    panel.open = window.innerWidth >= 1080;
    const summary = document.createElement("summary");
    summary.textContent = "Chart controls";
    const grid = el("div", "chart-controls-grid");
    panel.append(summary, grid);
    this.controls.append(panel);

    grid.append(
      select("Chart", [...CHART_TYPES], String(config.chart_type ?? "bar"), (v) =>
        update({ chart_type: v }),
      ),
      select("X", ["", ...columns], asString(config.x), (v) => update({ x: v || null })),
      select("Y", ["", ...columns], asString(config.y), (v) => update({ y: v || null })),
      select("Colour", ["", ...columns], asString(config.color), (v) =>
        update({ color: v || null }),
      ),
      select("Z", ["", ...columns], asString(config.z), (v) => update({ z: v || null })),
      select("Aggregate", ["", ...AGGREGATIONS], asString(config.agg), (v) =>
        update({ agg: v || null }),
      ),
      toggle("Stacked", Boolean(config.stacked), (v) => update({ stacked: v })),
    );

    if (this.options.onRerun) {
      const form = el("form", "chart-edit");
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "Describe a change — “show only the last 90 days”";
      input.className = "chart-edit-input";
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "btn btn-ghost";
      submit.textContent = "Apply";
      form.append(input, submit);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const value = input.value.trim();
        if (value) {
          this.options.onRerun?.(value);
          input.value = "";
        }
      });
      this.controls.append(form);
    }
  }

  private async export(format: "svg" | "png"): Promise<void> {
    if (!this.result) return;
    try {
      const payload = await api.post<{ format?: string; base64?: string } | string>(
        "/v1/export",
        { figure: this.result.figure, format, mode: currentMode() },
      );
      if (typeof payload === "string") {
        download(new Blob([payload], { type: "image/svg+xml" }), "chart.svg");
      } else if (payload?.base64) {
        const bytes = Uint8Array.from(atob(payload.base64), (c) => c.charCodeAt(0));
        download(new Blob([bytes], { type: "image/png" }), "chart.png");
      }
    } catch (error) {
      this.flash(`Export failed: ${(error as Error).message}`);
    }
  }

  /**
   * The answer, as a notebook someone can keep working in.
   *
   * The rows go with it: an export that points at a dataset the reader does
   * not have looks like it should work and does not. The server caps how many
   * it will inline.
   */
  private async exportNotebook(): Promise<void> {
    if (!this.result) return;
    try {
      const response = await fetch("/v1/notebook/export?format=ipynb", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: this.result.config?.title ?? "twoHelixes chart",
          question: this.options.question?.() ?? "",
          config: this.result.config,
          transform_code: this.result.transform_code,
          rows: this.result.preview ?? [],
        }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      download(await response.blob(), "chart.ipynb");
      this.flash("Downloaded chart.ipynb");
    } catch (error) {
      this.flash(`Export failed: ${(error as Error).message}`);
    }
  }

  private async copyData(): Promise<void> {
    if (!this.result) return;
    const rows = this.result.preview ?? [];
    if (!rows.length) return;
    const headers = Object.keys(rows[0]);
    const csv = [
      headers.join(","),
      ...rows.map((row) =>
        headers.map((h) => JSON.stringify(row[h] ?? "")).join(","),
      ),
    ].join("\n");
    await navigator.clipboard?.writeText(csv);
    this.flash("Copied as CSV");
  }

  private flash(message: string): void {
    const note = el("p", "note");
    note.textContent = message;
    this.notes.append(note);
    setTimeout(() => note.remove(), 4000);
  }
}

// --------------------------------------------------------------------------
// Small DOM helpers
// --------------------------------------------------------------------------

/**
 * Durations read as time, not as a raw millisecond count. A deep-research run
 * printing "184213 ms" makes the reader do the arithmetic; the agents here run
 * for minutes, so this is not a rare case.
 */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms)) return "";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function el(tag: string, className = ""): HTMLElement {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

export function button(label: string, onClick: () => void): HTMLButtonElement {
  const node = document.createElement("button");
  node.type = "button";
  node.className = "btn btn-ghost btn-small";
  node.textContent = label;
  node.addEventListener("click", onClick);
  return node;
}

function select(
  label: string,
  options: string[],
  value: string,
  onChange: (value: string) => void,
): HTMLElement {
  const wrap = el("label", "control");
  const caption = el("span", "control-label");
  caption.textContent = label;

  const node = document.createElement("select");
  node.className = "control-input";
  // A stable hook for tests and for the benchmark. Selecting "the first
  // .control-input" is a test that passes until someone adds a control.
  node.dataset.control = label.toLowerCase().replace(/[^a-z]+/g, "-");
  for (const option of options) {
    const item = document.createElement("option");
    item.value = option;
    item.textContent = option === "" ? "—" : option;
    if (option === value) item.selected = true;
    node.append(item);
  }
  node.addEventListener("change", () => onChange(node.value));

  wrap.append(caption, node);
  return wrap;
}

function toggle(
  label: string,
  value: boolean,
  onChange: (value: boolean) => void,
): HTMLElement {
  const wrap = el("label", "control control-toggle");
  const node = document.createElement("input");
  node.type = "checkbox";
  node.checked = value;
  node.addEventListener("change", () => onChange(node.checked));
  const caption = el("span", "control-label");
  caption.textContent = label;
  wrap.append(node, caption);
  return wrap;
}

function asString(value: unknown): string {
  if (Array.isArray(value)) return String(value[0] ?? "");
  return value == null ? "" : String(value);
}

function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
