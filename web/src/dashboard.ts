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

import { api } from "./api";
import { button, el, renderFigure } from "./chart";

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
  private readonly grid: HTMLElement;
  private readonly heading: HTMLInputElement;
  private readonly status: HTMLElement;

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
      actions.append(
        button("Refresh", () => this.refresh()),
        button("Share", () => this.share()),
        button("Save", () => this.save()),
      );
    }
    bar.append(this.heading, actions);

    this.status = el("div", "dash-status");
    this.grid = el("div", "dash-grid");
    this.root.append(bar, this.status, this.grid);
  }

  async open(dashboardId: string): Promise<void> {
    this.id = dashboardId;
    this.setStatus("Loading…");
    try {
      const payload = await api.get<DashboardPayload>(`/v1/dashboards/${dashboardId}`);
      this.adopt(payload);
      await this.render();
      this.setStatus("");
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  /** Open a shared dashboard by token: no account, read only. */
  async openShared(token: string): Promise<void> {
    this.setStatus("Loading…");
    try {
      const payload = await api.get<{ object: DashboardPayload }>(`/v1/shared/${token}`);
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
    card.tabIndex = 0;
    card.setAttribute("role", "group");
    card.setAttribute("aria-label", chart.title || "Chart");

    const head = el("figcaption", "dash-tile-head");
    const name = el("span", "dash-tile-title");
    name.textContent = chart.title || "Untitled";
    head.append(name);

    if (!this.options.readOnly) {
      const controls = el("div", "dash-tile-controls");
      for (const width of [1, 2, 3] as const) {
        const size = button(`${width}×`, () => this.resize(index, width));
        size.classList.add("btn-small", "dash-size");
        if ((tile.w ?? 1) === width) size.classList.add("is-active");
        size.title = `${width} column${width > 1 ? "s" : ""}`;
        controls.append(size);
      }
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

    const plot = el("div", "dash-plot");
    card.append(head, plot);

    if (chart.spec?.data?.length) {
      // Render after the card is in the DOM, or Plotly sizes to zero width.
      queueMicrotask(() => void renderFigure(plot, chart.spec));
    } else {
      const blank = el("p", "note");
      blank.textContent = "This chart has no saved figure. Refresh to rebuild it.";
      plot.append(blank);
    }

    return card;
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
    this.layout = this.layout.filter((_, i) => i !== index);
    void this.render();
    void this.save();
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
