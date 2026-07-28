/**
 * The dashboard grid.
 *
 * A grid of saved charts you can resize and reorder, built without a grid
 * library: the layout is a column span per tile and CSS grid does the rest.
 * That keeps the saved layout a handful of integers rather than pixel
 * geometry that breaks the moment someone opens it on a different screen.
 *
 * Reordering works by keyboard as well as by drag, because drag-and-drop
 * alone is not an accessible way to move something.
 */

import { api, stream, type ChartConfig, type PlotlyFigure } from "./api";
import { Builder } from "./builder";
import { CHART_TYPES, button, currentMode, el, renderFigure } from "./chart";
import { voiceField } from "./voice";

export interface Tile {
  chart_id: string;
  w: 1 | 2 | 3;
  h?: number;
}

interface Chart {
  id: string;
  title: string;
  spec: any;
  graph_args: any;
  updated_at?: number;
}

interface SourceOption {
  /** The body key the query and builder endpoints expect for this data. */
  key: "source_id" | "sample";
  id: string;
  label: string;
}

const AGGREGATIONS = ["sum", "mean", "median", "min", "max", "count"];

interface DashboardPayload {
  id: string;
  title: string;
  layout: Tile[];
  charts: Chart[];
  is_public?: number;
  share_token?: string | null;
}

export class DashboardView {
  readonly root: HTMLElement;

  private id = "";
  private title = "";
  private layout: Tile[] = [];
  private charts = new Map<string, Chart>();
  private dragging: number | null = null;
  private sources: SourceOption[] = [];
  private active: SourceOption | null = null;
  private cancel: (() => void) | null = null;
  private readonly grid: HTMLElement;
  private readonly heading: HTMLInputElement;
  private readonly status: HTMLElement;
  private readonly composer: HTMLElement;

  constructor(private readonly options: { readOnly?: boolean } = {}) {
    this.root = el("section", "dash");

    const bar = el("header", "dash-bar");
    this.heading = document.createElement("input");
    this.heading.className = "dash-title";
    this.heading.placeholder = "Untitled dashboard";
    this.heading.disabled = Boolean(options.readOnly);
    this.heading.addEventListener("change", () => void this.save());

    const actions = el("div", "dash-actions");
    if (!options.readOnly) {
      const add = button("Add pane", () => this.toggleComposer());
      add.classList.add("btn-primary", "dash-add");
      actions.append(
        add,
        button("Refresh", () => this.refresh()),
        button("Share", () => this.share()),
      );
    }
    bar.append(this.heading, actions);

    this.status = el("div", "dash-status");
    this.status.hidden = true;
    this.composer = el("div", "dash-composer");
    this.composer.hidden = true;
    this.grid = el("div", "dash-grid");
    this.root.append(bar, this.status, this.composer, this.grid);
  }

  async open(dashboardId: string): Promise<void> {
    this.id = dashboardId;
    this.setStatus("Loading…");
    try {
      // The mode goes with the request: a tile's colours are baked into the
      // saved figure, and the server restyles it for whoever is looking.
      const payload = await api.get<DashboardPayload>(
        `/v1/dashboards/${dashboardId}?mode=${currentMode()}`,
      );
      this.adopt(payload);
      await this.render();
      this.setStatus("");
      void this.loadSources();
      // An empty dashboard with a hidden composer is a blank page with a
      // button on it. If there is nothing here yet, the way to add something
      // is the page.
      if (!this.layout.length) this.toggleComposer(true);
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  /**
   * What this dashboard can ask questions of. Sources first, then the samples,
   * because a signed-in account with nothing connected still has to be able to
   * put a pane on the board.
   */
  private async loadSources(): Promise<void> {
    const options: SourceOption[] = [];
    try {
      const payload = await api.get<{
        sources: { id: string; name: string; kind: string }[];
      }>("/v1/sources");
      for (const source of payload.sources ?? []) {
        options.push({ key: "source_id", id: source.id, label: `${source.name} (${source.kind})` });
      }
    } catch {
      /* a missing source list must not stop the samples being offered */
    }
    if (!options.length) {
      try {
        const payload = await api.get<{ samples: { key: string; name: string }[] }>(
          "/v1/samples",
        );
        for (const sample of payload.samples ?? []) {
          options.push({ key: "sample", id: sample.key, label: `${sample.name} (sample)` });
        }
      } catch {
        /* nothing to offer; the composer says so */
      }
    }
    this.sources = options;
    this.active ??= options[0] ?? null;
    if (!this.composer.hidden) this.renderComposer();
  }

  private body(): Record<string, unknown> {
    if (!this.active) return {};
    return { [this.active.key]: this.active.id };
  }

  // -- adding a pane -------------------------------------------------------

  private toggleComposer(force?: boolean): void {
    this.composer.hidden = force === undefined ? !this.composer.hidden : !force;
    if (!this.composer.hidden) {
      this.renderComposer();
      this.composer.querySelector<HTMLInputElement>("input[type=text]")?.focus();
    }
  }

  private renderComposer(): void {
    this.composer.replaceChildren();

    const heading = el("h2", "dash-composer-title");
    heading.textContent = "Add a pane";
    this.composer.append(heading);

    if (!this.sources.length) {
      const note = el("p", "note");
      note.textContent =
        "No data to chart yet. Connect a source or load a sample from the Ask view first.";
      this.composer.append(note);
      return;
    }

    const row = el("div", "dash-composer-row");
    row.append(
      pick("Data", this.sources.map((s) => s.label), this.active?.label ?? "", (value) => {
        this.active = this.sources.find((s) => s.label === value) ?? this.active;
      }),
    );

    const { form } = voiceField(
      "Ask for a chart — “revenue by month, this year”",
      (text) => void this.addByQuestion(text),
    );
    form.classList.add("dash-composer-ask");
    row.append(form);
    this.composer.append(row);

    const manual = el("div", "dash-composer-manual");
    const build = button("Build it by hand instead", () => void this.openBuilder());
    build.classList.add("btn-small");
    const hint = el("span", "trace-detail");
    hint.textContent = "Pick the columns, the transformation and the chart type yourself.";
    manual.append(build, hint);
    this.composer.append(manual);
  }

  /** Ask for one chart and land it on the board. */
  private async addByQuestion(question: string): Promise<void> {
    if (!question || !this.id) return;
    this.cancel?.();
    this.setStatus(`Working on “${question}”…`);

    await new Promise<void>((resolve) => {
      this.cancel = stream(
        "/v1/query/stream",
        {
          q: question,
          dashboard_id: this.id,
          mode: currentMode(),
          ...this.body(),
        },
        (event, data) => {
          if (event === "stage" && data?.status === "start") {
            this.setStatus(`${question} — ${String(data.stage)}…`);
          } else if (event === "result") {
            void this.attach(String(data.chart_id ?? ""));
          } else if (event === "error") {
            this.setStatus(String(data?.message ?? data?.code ?? "That did not work"), true);
            this.cancel = null;
            resolve();
          } else if (event === "done") {
            this.cancel = null;
            resolve();
          }
        },
      );
    });
  }

  /** Put a chart that already exists onto this board and redraw. */
  private async attach(chartId: string): Promise<void> {
    if (!chartId) {
      this.setStatus("That chart could not be saved — sign in to keep charts.", true);
      return;
    }
    try {
      await api.post(`/v1/dashboards/${this.id}/charts`, { chart_id: chartId, w: 1 });
      await this.open(this.id);
      this.setStatus("Added");
      setTimeout(() => this.setStatus(""), 1500);
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  /**
   * The manual path: the chart builder, in a sheet, saving onto this board.
   * Same component the Ask view uses, so the plan, the steps and the channel
   * shelves behave identically wherever someone meets them.
   */
  private async openBuilder(): Promise<void> {
    if (!this.active) return;
    const sheet = document.createElement("dialog");
    sheet.className = "dash-sheet";

    const bar = el("header", "dash-sheet-bar");
    const title = el("h2");
    title.textContent = "Build a pane";
    const close = button("Close", () => sheet.close());
    bar.append(title, close);

    const builder = new Builder({
      dashboardId: this.id,
      onSaved: () => {
        sheet.close();
        void this.open(this.id);
      },
    });
    sheet.append(bar, builder.root);
    sheet.addEventListener("close", () => sheet.remove());
    document.body.append(sheet);
    sheet.showModal();

    await builder.open(this.body() as never);
  }

  /** Open a shared dashboard by token: no account, read only. */
  async openShared(token: string): Promise<void> {
    this.setStatus("Loading…");
    try {
      const payload = await api.get<{ object: DashboardPayload }>(
        `/v1/shared/${token}?mode=${currentMode()}`,
      );
      this.adopt(payload.object);
      await this.render();
      this.setStatus("");
    } catch (error) {
      this.setStatus("This dashboard is not shared, or the link expired.", true);
    }
  }

  private adopt(payload: DashboardPayload): void {
    this.id = payload.id ?? this.id;
    this.title = payload.title ?? "";
    this.heading.value = this.title;
    this.charts = new Map((payload.charts ?? []).map((c) => [c.id, c]));

    const layout = Array.isArray(payload.layout) ? payload.layout : [];
    // A chart with no layout entry still has to appear, or saving would
    // quietly drop it.
    const placed = new Set(layout.map((t) => t.chart_id));
    const orphans: Tile[] = (payload.charts ?? [])
      .filter((c) => !placed.has(c.id))
      .map((c) => ({ chart_id: c.id, w: 1 as const }));

    this.layout = [...layout.filter((t) => this.charts.has(t.chart_id)), ...orphans];
  }

  private async render(): Promise<void> {
    this.grid.replaceChildren();

    if (!this.layout.length) {
      const empty = el("p", "dash-empty");
      empty.textContent =
        "No charts yet. Build one and save it to this dashboard.";
      this.grid.append(empty);
      return;
    }

    for (const [index, tile] of this.layout.entries()) {
      const chart = this.charts.get(tile.chart_id);
      if (!chart) continue;
      this.grid.append(await this.renderTile(tile, chart, index));
    }
  }

  private async renderTile(tile: Tile, chart: Chart, index: number): Promise<HTMLElement> {
    const card = el("figure", `dash-tile span-${tile.w ?? 1}`);
    // Which chart this tile is, for tests, the benchmark and anyone reading
    // the DOM to work out why a board looks wrong.
    card.dataset.chartId = chart.id;
    card.dataset.width = String(tile.w ?? 1);
    card.tabIndex = 0;
    card.setAttribute("role", "group");
    card.setAttribute("aria-label", chart.title || "Chart");

    const head = el("figcaption", "dash-tile-head");
    const name = el("span", "dash-tile-title");
    name.textContent = chart.title || "Untitled";
    head.append(name);

    if (!this.options.readOnly) {
      const controls = el("div", "dash-tile-controls");
      // Below 720px the grid is one column, so a width control there changes
      // nothing at all. The buttons are hidden by CSS at that width rather
      // than by a JS width check, which would be wrong the moment the window
      // is resized.
      const sizes = el("div", "dash-sizes");
      for (const width of [1, 2, 3] as const) {
        const size = button(`${width}×`, () => this.resize(index, width));
        size.classList.add("btn-small", "dash-size");
        size.dataset.size = String(width);
        if ((tile.w ?? 1) === width) size.classList.add("is-active");
        size.title = `${width} column${width > 1 ? "s" : ""}`;
        sizes.append(size);
      }
      controls.append(sizes);
      const edit = button("Edit", () => void this.toggleEditor(card, chart));
      edit.classList.add("btn-small");
      edit.title = "Change this chart";
      controls.append(edit);

      const remove = button("✕", () => this.remove(index));
      remove.classList.add("btn-small");
      remove.title = "Remove from dashboard";
      controls.append(remove);
      head.append(controls);

      card.draggable = true;
      card.addEventListener("dragstart", () => { this.dragging = index; });
      card.addEventListener("dragover", (event) => {
        event.preventDefault();
        card.classList.add("is-over");
      });
      card.addEventListener("dragleave", () => card.classList.remove("is-over"));
      card.addEventListener("drop", (event) => {
        event.preventDefault();
        card.classList.remove("is-over");
        if (this.dragging !== null) this.reorder(this.dragging, index);
        this.dragging = null;
      });

      // Drag is not reachable by keyboard, so the same move is bound to keys.
      card.addEventListener("keydown", (event) => {
        if (!event.altKey) return;
        if (event.key === "ArrowLeft") {
          event.preventDefault();
          this.reorder(index, Math.max(0, index - 1));
        } else if (event.key === "ArrowRight") {
          event.preventDefault();
          this.reorder(index, Math.min(this.layout.length - 1, index + 1));
        }
      });
    }

    // A table is the one form that cannot be made to fit: ten columns squeezed
    // into a phone-width tile is ten columns of ellipsis. It gets a scroller of
    // its own instead - the tile keeps its width, the page never moves.
    const scroll = el("div", "dash-plot-scroll");
    const plot = el("div", "dash-plot");
    scroll.append(plot);
    card.append(head, scroll);

    if (chart.spec?.data?.length) {
      sizeForTable(plot, chart.spec);
      // Render after the card is in the DOM, or Plotly sizes to zero width.
      queueMicrotask(() => void renderFigure(plot, tileFigure(chart.spec)));
    } else {
      const blank = el("p", "note");
      blank.textContent = "This chart has no saved figure. Refresh to rebuild it.";
      plot.append(blank);
    }

    return card;
  }

  // -- editing one pane ----------------------------------------------------

  /**
   * The tile's own editor: say what to change, or set the channels by hand.
   *
   * The two are not the same transaction. A worded change re-runs the pipeline
   * and costs a query; moving a column onto the Y channel is a re-render of
   * data we already have and costs nothing, which is worth keeping visibly
   * separate rather than hiding both behind one "edit".
   */
  private async toggleEditor(card: HTMLElement, chart: Chart): Promise<void> {
    const existing = card.querySelector(".dash-tile-editor");
    if (existing) {
      existing.remove();
      return;
    }

    const panel = el("div", "dash-tile-editor");
    const status = el("p", "dash-tile-status");
    const plot = card.querySelector<HTMLElement>(".dash-plot");

    const say = (text: string, warn = false) => {
      status.textContent = text;
      status.classList.toggle("is-warn", warn);
    };

    const redraw = (figure: PlotlyFigure) => {
      chart.spec = figure;
      if (!plot) return;
      sizeForTable(plot, figure);
      void renderFigure(plot, tileFigure(figure));
    };

    const rename = (title: string) => {
      if (!title || title === chart.title) return;
      chart.title = title;
      const name = card.querySelector<HTMLElement>(".dash-tile-title");
      if (name) name.textContent = title;
      card.setAttribute("aria-label", title);
    };

    const { form } = voiceField("Change it — “group by month, top 10 only”", async (text) => {
      say("Re-running…");
      try {
        const response = await api.post<{
          figure: PlotlyFigure;
          config: ChartConfig;
          title?: string;
        }>(`/v1/chart/${chart.id}/edit`, { edit: text, mode: currentMode() });
        chart.graph_args = response.config;
        rename(String(response.title ?? ""));
        redraw(response.figure);
        say("Updated");
      } catch (error) {
        say((error as Error).message, true);
      }
    });
    panel.append(form, status);

    // The channel controls need the column names, which the grid payload does
    // not carry - it holds finished figures, not frames.
    let columns: string[] = [];
    try {
      const detail = await api.get<{ columns: string[]; graph_args: ChartConfig }>(
        `/v1/chart/${chart.id}`,
      );
      columns = detail.columns ?? [];
      chart.graph_args = detail.graph_args ?? chart.graph_args;
    } catch {
      /* the worded edit still works without them */
    }

    if (columns.length) {
      const config = () => (chart.graph_args ?? {}) as ChartConfig;
      const apply = async (patch: Partial<ChartConfig>) => {
        say("Redrawing…");
        try {
          const response = await api.post<{ figure: PlotlyFigure; config: ChartConfig }>(
            `/v1/chart/${chart.id}/config`,
            { ...config(), ...patch, mode: currentMode() },
          );
          chart.graph_args = response.config;
          // The caption follows the chart. Changing the x channel changes what
          // the pane is about, and a tile captioned "Revenue by region" over a
          // chart of channels is worse than one with no caption at all.
          rename(String(response.config?.title ?? ""));
          redraw(response.figure);
          say("");
        } catch (error) {
          say((error as Error).message, true);
        }
      };

      const grid = el("div", "dash-tile-controls-grid");
      grid.append(
        pick("Chart", [...CHART_TYPES], String(config().chart_type ?? "bar"), (v) =>
          void apply({ chart_type: v }),
        ),
        pick("X", ["", ...columns], asText(config().x), (v) => void apply({ x: v || null })),
        pick("Y", ["", ...columns], asText(config().y), (v) => void apply({ y: v || null })),
        pick("Colour", ["", ...columns], asText(config().color), (v) =>
          void apply({ color: v || null }),
        ),
        pick("Aggregate", ["", ...AGGREGATIONS], asText(config().agg), (v) =>
          void apply({ agg: v || null }),
        ),
      );
      panel.append(grid);
    }

    card.append(panel);
    panel.querySelector<HTMLInputElement>("input[type=text]")?.focus();
  }

  // -- mutations ---------------------------------------------------------

  private resize(index: number, width: 1 | 2 | 3): void {
    this.layout[index] = { ...this.layout[index], w: width };
    void this.render();
    void this.save();
  }

  private reorder(from: number, to: number): void {
    if (from === to) return;
    const copy = [...this.layout];
    const [moved] = copy.splice(from, 1);
    copy.splice(to, 0, moved);
    this.layout = copy;
    void this.render().then(() => {
      // Keep focus on the tile that moved, so a keyboard user does not lose
      // their place after every step.
      const tiles = this.grid.querySelectorAll<HTMLElement>(".dash-tile");
      tiles[to]?.focus();
    });
    void this.save();
  }

  private remove(index: number): void {
    const tile = this.layout[index];
    this.layout = this.layout.filter((_, i) => i !== index);
    void this.render();
    // Detaching the chart, not just dropping the layout entry: the board
    // returns every chart pointing at it, and this view places any that has
    // no entry - so a removal that only edited the layout came straight back
    // on the next load.
    if (!tile) return;
    void api
      .del(`/v1/dashboards/${this.id}/charts/${tile.chart_id}`)
      .then(() => this.setStatus("Removed"))
      .catch((error: Error) => this.setStatus(error.message, true));
  }

  // -- server ------------------------------------------------------------

  private async save(): Promise<void> {
    if (this.options.readOnly || !this.id) return;
    try {
      await api.put(`/v1/dashboards/${this.id}`, {
        title: this.heading.value,
        layout: this.layout,
      });
      this.setStatus("Saved");
      setTimeout(() => this.setStatus(""), 1500);
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  private async refresh(): Promise<void> {
    this.setStatus("Re-running every tile…");
    try {
      const result = await api.post<{ refreshed: number; failed: string[] }>(
        `/v1/dashboards/${this.id}/refresh`,
      );
      await this.open(this.id);
      const failed = result.failed?.length ?? 0;
      this.setStatus(
        failed
          ? `Refreshed ${result.refreshed}; ${failed} could not re-run.`
          : `Refreshed ${result.refreshed} tiles.`,
        failed > 0,
      );
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  private async share(): Promise<void> {
    try {
      const result = await api.post<{ url: string; token: string }>("/v1/shares", {
        kind: "dashboard",
        object_id: this.id,
      });
      await navigator.clipboard?.writeText(result.url).catch(() => undefined);
      this.setStatus(`Share link copied: ${result.url}`);
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  private setStatus(text: string, warn = false): void {
    this.status.textContent = text;
    this.status.classList.toggle("is-warn", warn);
    this.status.hidden = !text;
  }
}

/**
 * A tile is a display, not a workbench. Plotly's modebar - camera, zoom, pan,
 * reset - sat on top of every pane on the board and, at phone width, over the
 * data itself. The chart card in the Ask view keeps it; there, zooming into a
 * result is the point.
 */
/** Room per column for a table tile; anything less is a column of ellipsis. */
const TABLE_COLUMN_WIDTH = 96;

function sizeForTable(plot: HTMLElement, figure: PlotlyFigure): void {
  const trace = (figure.data ?? [])[0] as { type?: string; header?: any } | undefined;
  const headers = trace?.type === "table" ? trace.header?.values : null;
  plot.style.minWidth = Array.isArray(headers)
    ? `${headers.length * TABLE_COLUMN_WIDTH}px`
    : "";
}

function tileFigure(figure: PlotlyFigure): PlotlyFigure {
  // The title comes off here as well as on the board payload. A tile redrawn
  // by the config route gets its figure straight from that endpoint, which
  // knows nothing about tiles - so without this the heading the board had
  // stripped came back the moment someone changed a channel.
  const { title: _title, ...layout } = figure.layout ?? {};
  return {
    ...figure,
    layout,
    config: { ...(figure.config ?? {}), displayModeBar: false },
  };
}

function pick(
  label: string,
  options: string[],
  value: string,
  onChange: (value: string) => void,
): HTMLElement {
  const wrap = el("label", "control");
  const caption = el("span", "control-label");
  caption.textContent = label;
  const select = document.createElement("select");
  select.className = "control-input";
  // The same stable hook the chart card's controls carry. "The first
  // .control-input" is a selector that passes until someone adds a control.
  select.dataset.control = label.toLowerCase().replace(/[^a-z]+/g, "-");
  for (const option of options) {
    const node = document.createElement("option");
    node.value = option;
    node.textContent = option === "" ? "—" : option;
    if (option === value) node.selected = true;
    select.append(node);
  }
  select.addEventListener("change", () => onChange(select.value));
  wrap.append(caption, select);
  return wrap;
}

/** A channel can hold a list; the picker shows one. */
function asText(value: unknown): string {
  if (Array.isArray(value)) return value.length ? String(value[0]) : "";
  return value == null ? "" : String(value);
}
