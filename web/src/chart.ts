/**
 * Chart rendering plus the manual control panel.
 *
 * The controls exist because the agent's choice is a starting point, not a
 * verdict: swapping x and y, changing the chart type, or regrouping by a
 * different column must not require another model call. Only "re-run the
 * query" costs a query.
 */

import { api, type ChartConfig, type PipelineResult, type PlotlyFigure } from "./api";

type Plotly = typeof import("plotly.js-basic-dist-min");
let plotlyPromise: Promise<Plotly> | null = null;

/** Plotly is ~1 MB; load it only when a chart is actually rendered. */
function loadPlotly(): Promise<Plotly> {
  if (!plotlyPromise) {
    plotlyPromise = import("plotly.js-basic-dist-min").then((m) => (m.default ?? m) as Plotly);
  }
  return plotlyPromise;
}

export const CHART_TYPES = [
  "line",
  "bar",
  "hbar",
  "area",
  "scatter",
  "pie",
  "heatmap",
  "box",
  "histogram",
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
  const layout = fitTitle({ ...figure.layout, autosize: true }, host.clientWidth);
  const config = { ...(figure.config ?? {}), responsive: true, displaylogo: false };
  await Plotly.react(host, figure.data as any, layout as any, config as any);
}

/**
 * Wrap a title that will not fit the plot.
 *
 * Plotly clips titles rather than wrapping them, and our titles state the
 * finding ("Revenue increased across all regions in Q1 2024") rather than the
 * column names, so they are long by design. On a phone that means the sentence
 * loses its last few words - exactly the words carrying the point. The server
 * cannot fix this because it does not know the viewport width.
 */
function fitTitle(layout: Record<string, any>, width: number): Record<string, any> {
  const title = layout.title;
  const text: string = typeof title === "string" ? title : (title?.text ?? "");
  if (!text || !width || text.includes("<br>")) return layout;

  const size: number = (typeof title === "object" && title?.font?.size) || 16;
  const margin = layout.margin ?? {};
  const usable = width - (margin.l ?? 56) - (margin.r ?? 24);
  // Inter at 600 weight averages a little over half the point size per glyph.
  const perLine = Math.max(16, Math.floor(usable / (size * 0.53)));
  if (text.length <= perLine) return layout;

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
  if (lines.length < 2) return layout;

  // The title and the legend both live in the top margin, and the legend is
  // anchored to the plot. Simply growing the margin slides the legend down
  // into the second title line, so the title gets pinned to the top of the
  // container and the margin is sized for both.
  const legendRoom = layout.showlegend ? 26 : 6;
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
    margin: { ...margin, t: 12 + lines.length * (size + 4) + legendRoom },
  };
}

export interface ChartViewOptions {
  onConfigChange?: (config: ChartConfig) => void;
  onRerun?: (request: string) => void;
  chartId?: string;
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
    await renderFigure(this.plot, result.figure);
    this.renderNotes(result);
    this.renderControls(result);
  }

  private renderNotes(result: PipelineResult): void {
    this.notes.replaceChildren();

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
    actions.append(
      button("Export SVG", () => this.export("svg")),
      button("Export PNG", () => this.export("png")),
      button("Copy data", () => this.copyData()),
    );
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

    this.controls.append(
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
