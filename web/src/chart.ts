/**
 * Chart rendering plus the manual control panel.
 *
 * The controls exist because the agent's choice is a starting point, not a
 * verdict: swapping x and y, changing the chart type, or regrouping by a
 * different column must not require another model call. Only "re-run the
 * query" costs a query.
 */

import { api, type ChartConfig, type PipelineResult, type PlotlyFigure } from "./api";
import {
  applyView,
  dataTable,
  figureCsv,
  isBarLike,
  isCartesian,
  isEmptyView,
  rowsCsv,
  seriesNames,
  slug,
  type ChartViewState,
} from "./chart-tools";
import { registerShortcuts } from "./shortcuts";

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
  "wordcloud",
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
  const { width: _w, height: _h, ...rest } = figure.layout ?? {};
  const layout = fitTop({ ...rest, autosize: true }, figure.data ?? [], host.clientWidth);
  const topojsonURL =
    document.body.dataset.topojsonUrl ||
    `${window.location.origin}/static/libs/topojson/`;
  const config = {
    ...(host.clientWidth < 720 || window.matchMedia?.("(hover: none)").matches ? { displayModeBar: false } : {}),
    ...(figure.config ?? {}),
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ["toImage", "lasso2d", "select2d"],
    topojsonURL,
  };

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

const HEIGHT_KEY = "th-chart-height";
const FOCUS_KEY = "th-focus";
let activeView: ChartView | null = null;

function stored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function store(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* private mode */
  }
}

if (typeof document !== "undefined" && stored(FOCUS_KEY) === "1") {
  document.documentElement.classList.add("th-focus");
}

export function toggleFocusMode(force?: boolean): void {
  const on = document.documentElement.classList.toggle("th-focus", force);
  store(FOCUS_KEY, on ? "1" : null);
}

registerShortcuts("chart", [
  { key: "f", label: "Chart full screen", run: () => activeView?.toggleFullscreen() },
  { key: "t", label: "Switch between chart and table", run: () => activeView?.toggleTable() },
  { key: "o", label: "Format options", run: () => activeView?.toggleFormat() },
  { key: "c", label: "Chart controls", run: () => activeView?.toggleControls() },
  { key: "e", label: "Export PNG", run: () => void activeView?.exportImage("png") },
  { key: "d", label: "Download CSV", run: () => activeView?.downloadCsv() },
  { key: "p", label: "Hide or show the side panel", run: () => toggleFocusMode() },
  { key: "r", label: "Reset chart height", run: () => activeView?.resetHeight() },
]);

export class ChartView {
  readonly root: HTMLElement;
  private readonly plot: HTMLElement;
  private readonly body: HTMLElement;
  private readonly table: HTMLElement;
  private readonly controls: HTMLElement;
  private readonly notes: HTMLElement;
  private readonly toolbar: HTMLElement;
  private readonly format: HTMLElement;
  private result: PipelineResult | null = null;
  private view: ChartViewState = {};
  private mode: "chart" | "table" = "chart";
  private persistTimer = 0;
  private redrawFrame = 0;
  private lastSize = "";

  constructor(private readonly options: ChartViewOptions = {}) {
    this.root = el("section", "chart-view");
    this.toolbar = el("div", "chart-toolbar");
    this.format = el("div", "chart-format");
    this.format.hidden = true;
    this.plot = el("div", "chart-plot");
    this.table = el("div", "chart-table");
    this.table.hidden = true;
    this.body = el("div", "chart-body");
    this.controls = el("div", "chart-controls");
    this.notes = el("div", "chart-notes");

    const saved = Number(stored(HEIGHT_KEY));
    if (saved >= 240) this.body.style.height = `${saved}px`;
    const grip = el("div", "chart-resize");
    grip.title = "Drag to resize. Double-click to reset.";
    grip.setAttribute("role", "separator");
    grip.setAttribute("aria-orientation", "horizontal");
    grip.addEventListener("pointerdown", (event) => this.dragHeight(event, grip));
    grip.addEventListener("dblclick", () => this.resetHeight());
    this.body.append(this.plot, this.table, grip);

    const header = el("header", "chart-header");
    header.append(this.toolbar, this.format, this.notes);
    this.root.append(header, this.body, this.controls);

    new ResizeObserver(() => this.scheduleRedraw()).observe(this.plot);
    this.root.addEventListener("fullscreenchange", () => {
      this.root.classList.toggle("is-fullscreen", document.fullscreenElement === this.root);
    });
    document.addEventListener("click", (e) => {
      const menu = this.toolbar.querySelector<HTMLDetailsElement>(".chart-exports");
      if (menu?.open && !menu.contains(e.target as Node)) menu.open = false;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!this.format.hidden) this.toggleFormat(false);
      if (this.root.classList.contains("is-max")) this.toggleFullscreen();
    });
  }

  async show(result: PipelineResult): Promise<void> {
    if (this.result?.chart_id !== result.chart_id || !result.chart_id) this.view = {};
    this.result = result;
    activeView = this;
    const persisted = (result.config as any)?.view;
    if (persisted && typeof persisted === "object") this.view = { ...persisted };
    let renderError = "";
    try {
      await this.draw();
    } catch (err) {
      renderError = err instanceof Error ? err.message : String(err);
      console.error("twoHelixes: chart render failed", err);
    }
    this.renderToolbar(result);
    this.renderNotes(result, renderError);
    this.renderControls(result);
    if (!this.format.hidden) this.renderFormat();
    if (this.mode === "table") this.renderTable();
  }

  private figure(): PlotlyFigure | null {
    return this.result ? applyView(this.result.figure, this.view) : null;
  }

  private async draw(): Promise<void> {
    const figure = this.figure();
    if (!figure) return;
    await renderFigure(this.plot, figure);
    this.lastSize = `${this.plot.clientWidth}x${this.plot.clientHeight}`;
  }

  private scheduleRedraw(): void {
    if (!this.result || this.mode !== "chart" || this.redrawFrame) return;
    this.redrawFrame = requestAnimationFrame(() => {
      this.redrawFrame = 0;
      const size = `${this.plot.clientWidth}x${this.plot.clientHeight}`;
      if (!this.plot.clientWidth || !this.plot.clientHeight || size === this.lastSize) return;
      this.lastSize = size;
      void loadPlotly()
        .then((Plotly) => (Plotly as any).Plots.resize(this.plot))
        .then(() => this.draw())
        .catch(() => undefined);
    });
  }

  toggleFullscreen(): void {
    if (document.fullscreenElement === this.root) {
      void document.exitFullscreen();
      return;
    }
    if (this.root.classList.contains("is-max")) {
      this.root.classList.remove("is-max");
      document.documentElement.classList.remove("th-chart-max");
      return;
    }
    if (this.root.requestFullscreen && document.fullscreenEnabled) {
      void this.root.requestFullscreen().catch(() => this.maximise());
    } else this.maximise();
  }

  private maximise(): void {
    this.root.classList.add("is-max");
    document.documentElement.classList.add("th-chart-max");
  }

  toggleTable(force?: "chart" | "table"): void {
    this.mode = force ?? (this.mode === "chart" ? "table" : "chart");
    this.plot.hidden = this.mode !== "chart";
    this.table.hidden = this.mode !== "table";
    this.toolbar.querySelectorAll<HTMLButtonElement>(".seg-btn").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.mode === this.mode));
    });
    if (this.mode === "table") this.renderTable();
    else void this.draw();
  }

  toggleFormat(force?: boolean): void {
    const open = force ?? this.format.hidden;
    this.format.hidden = !open;
    this.toolbar.querySelector(".chart-format-btn")?.setAttribute("aria-expanded", String(open));
    if (open) {
      this.renderFormat();
      this.format.querySelector<HTMLElement>("input, select")?.focus();
    }
  }

  toggleControls(): void {
    const panel = this.controls.querySelector<HTMLDetailsElement>(".chart-controls-panel");
    if (panel) panel.open = !panel.open;
  }

  resetHeight(): void {
    this.body.style.height = "";
    store(HEIGHT_KEY, null);
  }

  private dragHeight(event: PointerEvent, grip: HTMLElement): void {
    event.preventDefault();
    const startY = event.clientY;
    const start = this.body.getBoundingClientRect().height;
    grip.setPointerCapture(event.pointerId);
    this.root.classList.add("is-resizing");
    const move = (e: PointerEvent) => {
      const next = Math.max(240, Math.min(window.innerHeight * 2, start + e.clientY - startY));
      this.body.style.height = `${Math.round(next)}px`;
    };
    const up = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
      this.root.classList.remove("is-resizing");
      store(HEIGHT_KEY, String(Math.round(this.body.getBoundingClientRect().height)));
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
  }

  private renderToolbar(result: PipelineResult): void {
    this.toolbar.replaceChildren();
    const config = result.config ?? ({} as ChartConfig);

    const meta = el("div", "chart-meta");
    const bits: string[] = [];
    if (result.row_count) bits.push(`${result.row_count.toLocaleString()} rows`);
    if (result.elapsed_ms) bits.push(formatDuration(result.elapsed_ms));
    meta.textContent = bits.join(" · ");

    const type = select("Chart", [...CHART_TYPES], String(config.chart_type ?? "bar"), (v) =>
      this.options.onConfigChange?.({ ...config, chart_type: v }),
    );
    type.classList.add("chart-type-pick");
    type.title = "Chart type";

    const seg = el("div", "seg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "View");
    for (const [mode, label] of [["chart", "Chart"], ["table", "Table"]] as const) {
      const b = button(label, () => this.toggleTable(mode));
      b.className = "seg-btn";
      b.dataset.mode = mode;
      b.setAttribute("aria-pressed", String(this.mode === mode));
      b.title = "Switch between chart and table (T)";
      seg.append(b);
    }

    const fmt = tool("Format", "Format options (O)", () => this.toggleFormat());
    fmt.classList.add("chart-format-btn");
    fmt.setAttribute("aria-expanded", String(!this.format.hidden));
    if (!isEmptyView(this.view)) fmt.classList.add("is-on");

    const exports = document.createElement("details");
    exports.className = "chart-exports menu";
    const summary = document.createElement("summary");
    summary.className = "tool";
    summary.innerHTML = `${icon("Export")}<span class="tool-label">Export</span>`;
    summary.title = "Download or copy";
    const list = el("div", "menu-list");
    list.append(
      menuItem("PNG image", "E", () => void this.exportImage("png")),
      menuItem("SVG vector", "", () => void this.exportImage("svg")),
      menuItem("CSV of plotted values", "D", () => this.downloadCsv()),
      menuItem("Notebook (.ipynb)", "", () => void this.exportNotebook()),
      menuItem("Copy data as CSV", "", () => void this.copyData()),
    );
    if (result.chart_id) list.append(menuItem("Copy share link", "", () => void this.copyLink(result.chart_id!)));
    exports.append(summary, list);
    list.addEventListener("click", () => (exports.open = false));

    const full = tool("Full screen", "Full screen (F)", () => this.toggleFullscreen());
    full.classList.add("chart-full-btn");
    const focus = tool("Panel", "Hide or show the side panel (P)", () => toggleFocusMode());
    focus.classList.add("chart-focus-btn");

    const spacer = el("div", "chart-toolbar-spacer");
    this.toolbar.append(meta, spacer, type, seg, fmt, exports, focus, full);

    if (result.chart_id) {
      const share = tool("Share", "Share this chart", () => void this.shareChart(result.chart_id!));
      this.toolbar.append(share);
    }
    if (this.options.onPin) {
      const pin = button("Add to dashboard", () => this.options.onPin?.(result));
      pin.className = "btn btn-primary btn-small chart-pin";
      this.toolbar.append(pin);
    }
  }

  private renderNotes(result: PipelineResult, renderError = ""): void {
    this.notes.replaceChildren();
    if (renderError) {
      const note = el("p", "note note-warn");
      note.textContent =
        `This browser could not draw the chart (${renderError}). The data and exports still work.`;
      this.notes.append(note);
    }
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
    this.notes.hidden = !this.notes.childElementCount;
  }

  private renderTable(): void {
    if (!this.result) return;
    this.table.replaceChildren(dataTable(this.result.preview ?? [], this.result.row_count ?? 0));
  }

  private renderFormat(): void {
    if (!this.result) return;
    const figure = this.result.figure;
    const cartesian = isCartesian(figure);
    const bars = isBarLike(figure);
    const v = this.view;
    const set = (patch: Partial<ChartViewState>) => {
      this.view = { ...this.view, ...patch };
      void this.draw();
      this.toolbar.querySelector(".chart-format-btn")?.classList.toggle("is-on", !isEmptyView(this.view));
      this.persistView();
    };
    this.format.replaceChildren();

    const grid = el("div", "chart-format-grid");
    const title = document.createElement("input");
    title.type = "text";
    title.className = "control-input";
    title.placeholder = "Chart title";
    const current = (figure.layout as any)?.title;
    title.value = v.title ?? (typeof current === "string" ? current : current?.text ?? "");
    title.addEventListener("change", () => set({ title: title.value.trim() }));
    const titleWrap = el("label", "control chart-format-title");
    const titleCap = el("span", "control-label");
    titleCap.textContent = "Title";
    titleWrap.append(titleCap, title);
    grid.append(titleWrap);

    if (bars) {
      grid.append(
        labelled("Sort", [["", "As drawn"], ["desc", "Largest first"], ["asc", "Smallest first"], ["label", "A to Z"]], v.sort ?? "", (x) => set({ sort: x as ChartViewState["sort"] })),
        labelled("Show", [["", "All"], ["5", "Top 5"], ["10", "Top 10"], ["20", "Top 20"]], String(v.top ?? ""), (x) => set({ top: Number(x) || 0 })),
      );
    }
    if (cartesian) {
      grid.append(
        labelled("Numbers", [["", "Auto"], ["compact", "1.2k"], ["comma", "1,234"], ["fixed2", "1,234.56"], ["percent", "12%"], ["currency", "$1,234"]], v.format ?? "", (x) => set({ format: x as ChartViewState["format"] })),
      );
    }
    grid.append(
      labelled("Legend", [["", "Auto"], ["top", "Top"], ["right", "Right"], ["bottom", "Bottom"], ["off", "Hidden"]], v.legend ?? "", (x) => set({ legend: x as ChartViewState["legend"] })),
    );
    const toggles = el("div", "chart-format-toggles");
    if (cartesian) {
      toggles.append(
        toggle("Log scale", Boolean(v.logY), (x) => set({ logY: x })),
        toggle("Data labels", Boolean(v.labels), (x) => set({ labels: x })),
        toggle("Gridlines", v.grid !== false, (x) => set({ grid: x ? undefined : false })),
      );
      if (!bars) toggles.append(toggle("Range slider", Boolean(v.slider), (x) => set({ slider: x })));
    }
    grid.append(toggles);
    this.format.append(grid);

    const names = seriesNames(figure);
    if (names.length > 1) {
      const series = el("fieldset", "chart-format-series");
      const legend = el("legend", "control-label");
      legend.textContent = "Series";
      series.append(legend);
      const hidden = new Set(v.hidden ?? []);
      for (const name of names) {
        series.append(toggle(name, !hidden.has(name), (on) => {
          const next = new Set(this.view.hidden ?? []);
          if (on) next.delete(name);
          else next.add(name);
          set({ hidden: [...next] });
        }));
      }
      this.format.append(series);
    }

    const foot = el("div", "chart-format-foot");
    const hint = el("span", "note");
    hint.textContent = this.result.chart_id ? "Saved with the chart. Costs no query." : "Costs no query.";
    foot.append(
      hint,
      button("Reset", () => {
        this.view = {};
        void this.draw();
        this.renderFormat();
        this.toolbar.querySelector(".chart-format-btn")?.classList.remove("is-on");
        this.persistView();
      }),
      button("Done", () => this.toggleFormat(false)),
    );
    this.format.append(foot);
  }

  private persistView(): void {
    const result = this.result;
    if (!result?.chart_id) return;
    result.config = { ...(result.config ?? {}), view: this.view } as ChartConfig;
    clearTimeout(this.persistTimer);
    this.persistTimer = window.setTimeout(() => {
      const body: Record<string, unknown> = { ...result.config, view: this.view, mode: currentMode() };
      if (this.view.title) {
        body.title = this.view.title;
        body.title_locked = true;
      }
      void api.post(`/v1/chart/${result.chart_id}/config`, body).catch((error) =>
        this.flash(`Could not save the format: ${(error as Error).message}`),
      );
    }, 600);
  }

  /**
   * Build the control row. Every column is offered on every channel: the
   * server validates and repairs the combination, so an impossible pick comes
   * back as an explained downgrade rather than a broken chart.
   */
  private renderControls(result: PipelineResult): void {
    const wasOpen = this.controls.querySelector<HTMLDetailsElement>("details")?.open;
    this.controls.replaceChildren();
    const columns = result.columns ?? [];
    const config = result.config ?? ({} as ChartConfig);

    const update = (patch: Partial<ChartConfig>) => {
      const next = { ...config, ...patch };
      this.options.onConfigChange?.(next);
    };

    const panel = document.createElement("details");
    panel.className = "chart-controls-panel";
    panel.open = wasOpen ?? window.innerWidth >= 1080;
    const summary = document.createElement("summary");
    summary.textContent = "Axes and series";
    const grid = el("div", "chart-controls-grid");
    panel.append(summary, grid);
    this.controls.append(panel);

    grid.append(
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
      input.placeholder = "Describe a change — “only the last 90 days”, “top 5 regions”";
      input.className = "chart-edit-input";
      input.setAttribute("aria-label", "Describe a change");
      const submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "btn btn-ghost btn-small";
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

  async exportImage(format: "svg" | "png"): Promise<void> {
    if (!this.result) return;
    const name = slug(this.view.title || String(this.result.config?.title ?? ""));
    if (this.mode === "chart" && (this.plot as any)._fullLayout) {
      try {
        const Plotly = await loadPlotly();
        const width = Math.max(this.plot.clientWidth, 640);
        const height = Math.max(this.plot.clientHeight, 360);
        await (Plotly as any).downloadImage(this.plot, { format, filename: name, width, height, scale: format === "png" ? 2 : 1 });
        return;
      } catch (error) {
        console.warn("twoHelixes: browser export failed, using the server", error);
      }
    }
    try {
      const payload = await api.post<{ format?: string; base64?: string } | string>(
        "/v1/export",
        { figure: this.figure(), format, mode: currentMode() },
      );
      if (typeof payload === "string") {
        download(new Blob([payload], { type: "image/svg+xml" }), `${name}.svg`);
      } else if (payload?.base64) {
        const bytes = Uint8Array.from(atob(payload.base64), (c) => c.charCodeAt(0));
        download(new Blob([bytes], { type: "image/png" }), `${name}.png`);
      }
    } catch (error) {
      this.flash(`Export failed: ${(error as Error).message}`);
    }
  }

  downloadCsv(): void {
    if (!this.result) return;
    const csv = figureCsv(this.result.figure) || rowsCsv(this.result.preview ?? []);
    download(new Blob([csv], { type: "text/csv;charset=utf-8" }), `${slug(String(this.result.config?.title ?? ""))}.csv`);
  }

  private async copyLink(chartId: string): Promise<void> {
    try {
      const shared = await api.post<{ url: string }>("/v1/shares", { kind: "chart", object_id: chartId });
      await navigator.clipboard.writeText(shared.url);
      this.flash("Share link copied");
    } catch (error) {
      this.flash(`Could not copy the link: ${(error as Error).message}`);
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
    const csv = rowsCsv(this.result.preview ?? []) || figureCsv(this.result.figure);
    if (!csv) return;
    try {
      await navigator.clipboard.writeText(csv);
      this.flash("Copied as CSV");
    } catch {
      this.flash("The clipboard is blocked here; use Export CSV.");
    }
  }

  private async shareChart(chartId: string): Promise<void> {
    const dialog = document.createElement("dialog");
    dialog.className = "pick-sheet share-dialog";
    const heading = el("h2");
    heading.textContent = "Share graph";
    const options = el("div", "share-options");
    const status = el("p", "note");
    status.textContent = "Loading teams…";
    const close = button("Close", () => dialog.close());
    dialog.append(heading, options, status, close);
    dialog.addEventListener("close", () => dialog.remove());
    document.body.append(dialog);
    dialog.showModal();

    const publicLink = button("Copy read-only link", () => {
      void api.post<{ url: string }>("/v1/shares", {
        kind: "chart",
        object_id: chartId,
      }).then(async (result) => {
        try {
          await navigator.clipboard.writeText(result.url);
          status.textContent = "Graph link copied.";
        } catch {
          status.textContent = result.url;
        }
      }).catch((error) => {
        status.textContent = (error as Error).message;
      });
    });
    publicLink.classList.add("pick-item");
    options.append(publicLink);

    try {
      const result = await api.get<{
        teams: { id: string; name: string; role: string }[];
      }>("/v1/teams");
      const teams = result.teams ?? [];
      if (teams.length) {
        const label = el("p", "share-label");
        label.textContent = "Or share with a team";
        options.append(label);
      }
      for (const team of teams) {
        const node = button(`${team.name} · ${team.role}`, () => {
          void api.post(`/v1/teams/${team.id}/objects`, {
            kind: "chart",
            object_id: chartId,
          }).then(() => {
            status.textContent = `Shared with ${team.name}.`;
          }).catch((error) => {
            status.textContent = (error as Error).message;
          });
        });
        node.classList.add("pick-item");
        options.append(node);
      }
      status.textContent = "Choose who can see this graph.";
    } catch (error) {
      status.textContent = (error as Error).message;
    }
  }

  private flash(message: string): void {
    const note = el("p", "note note-flash");
    note.textContent = message;
    this.notes.hidden = false;
    this.notes.append(note);
    setTimeout(() => {
      note.remove();
      this.notes.hidden = !this.notes.childElementCount;
    }, 4000);
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

const ICONS: Record<string, string> = {
  Format: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  Export: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  Panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  "Full screen": '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  Share: '<circle cx="6" cy="12" r="2.4"/><circle cx="18" cy="6" r="2.4"/><circle cx="18" cy="18" r="2.4"/><path d="M8.2 11 15.8 7M8.2 13l7.6 4"/>',
};

function icon(name: string): string {
  const path = ICONS[name];
  return path
    ? `<svg class="tool-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`
    : "";
}

function tool(label: string, title: string, onClick: () => void): HTMLButtonElement {
  const node = button("", onClick);
  node.className = "tool";
  node.title = title;
  node.setAttribute("aria-label", label);
  node.innerHTML = `${icon(label)}<span class="tool-label">${label}</span>`;
  return node;
}

function menuItem(label: string, key: string, onClick: () => void): HTMLButtonElement {
  const node = button(label, onClick);
  node.className = "menu-item";
  if (key) {
    const k = document.createElement("kbd");
    k.textContent = key;
    node.append(k);
  }
  return node;
}

function labelled(
  label: string,
  options: [string, string][],
  value: string,
  onChange: (value: string) => void,
): HTMLElement {
  const wrap = el("label", "control");
  const caption = el("span", "control-label");
  caption.textContent = label;
  const node = document.createElement("select");
  node.className = "control-input";
  for (const [v, text] of options) {
    const item = document.createElement("option");
    item.value = v;
    item.textContent = text;
    item.selected = v === value;
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
