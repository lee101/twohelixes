/**
 * The chart builder.
 *
 * One object is shared between the person and the agent: a list of transform
 * steps plus a chart configuration. The agent proposes; the person reorders,
 * edits, disables or deletes. Neither has to ask the other, and a human edit
 * never spends a query - only asking the agent does.
 *
 * The server remains authoritative for every chart. An opt-in worker may
 * reshape typed inline rows first, but any uncertainty returns to the original
 * full server preview.
 */

import { api, type ChartConfig } from "./api";
import { CHART_TYPES, button, el, renderFigure } from "./chart";
import {
  LocalReshaper,
  localReshapeEnabled,
  setLocalReshapeEnabled,
} from "./reshape/client";
import type { LocalReshapeResult, SchemaColumn, Step } from "./reshape/protocol";

export type { Step } from "./reshape/protocol";

interface ColumnInfo {
  name: string;
  dtype: string;
  role: "time" | "measure" | "category" | "identifier";
  unique?: number;
  nulls?: number;
}

interface PreviewResponse {
  ok: boolean;
  figure?: any;
  config?: ChartConfig;
  steps?: Step[];
  descriptions?: string[];
  notes?: string[];
  warnings?: string[];
  audit?: { code: string; detail: string }[];
  columns?: string[];
  row_count?: number;
  preview?: Record<string, unknown>[];
  python?: string;
  problems?: { step: string; error: string }[];
  elapsed_ms?: number;
}

const ROLE_ICON: Record<string, string> = {
  time: "◷",
  measure: "#",
  category: "A",
  identifier: "⚿",
};

const AGGREGATIONS = ["sum", "mean", "median", "count", "nunique", "min", "max"];
const GRAINS = ["hour", "day", "week", "month", "quarter", "year"];
const FILTER_OPS = [
  "eq", "ne", "gt", "ge", "lt", "le",
  "in", "not_in", "contains", "starts_with", "ends_with",
  "is_null", "not_null",
];

/** Channels a column can be dropped onto. */
const CHANNELS = ["x", "y", "color", "size", "facet"] as const;
type Channel = (typeof CHANNELS)[number];

export interface BuilderSource {
  dataset_id?: string;
  sample?: string;
  source_id?: string;
  sql?: string;
  data?: Record<string, unknown>[];
}

export interface BuilderOptions {
  /** Save the chart onto this dashboard, and tell the caller when it lands. */
  dashboardId?: string;
  onSaved?: (chartId: string) => void;
}

export class Builder {
  readonly root: HTMLElement;

  private columns: ColumnInfo[] = [];
  private steps: Step[] = [];
  private config: ChartConfig = { chart_type: "bar" };
  private source: BuilderSource = {};
  private history: { steps: Step[]; config: ChartConfig }[] = [];
  private future: { steps: Step[]; config: ChartConfig }[] = [];
  private lastPreview: PreviewResponse | null = null;
  private pending = false;
  private queued = false;
  private previewVersion = 0;
  private localSourcePrepared = false;
  private localSourceResult: LocalReshapeResult | { ok: true } | null = null;
  private readonly localReshaper = new LocalReshaper();

  private readonly fieldList: HTMLElement;
  private readonly shelves: HTMLElement;
  private readonly stepList: HTMLElement;
  private readonly plot: HTMLElement;
  private readonly status: HTMLElement;
  private readonly askInput: HTMLInputElement;

  constructor(private readonly options: BuilderOptions = {}) {
    this.root = el("section", "builder");

    // --- left rail: the columns you have -------------------------------
    const rail = el("aside", "builder-rail");
    const fieldsTitle = el("h3", "builder-heading");
    fieldsTitle.textContent = "Columns";
    const localControl = el("label", "builder-local-toggle");
    const localToggle = document.createElement("input");
    localToggle.type = "checkbox";
    localToggle.checked = localReshapeEnabled();
    const localLabel = el("span");
    localLabel.textContent = "Reshape previews in this browser (experimental)";
    localControl.append(localToggle, localLabel);
    localToggle.addEventListener("change", () => {
      setLocalReshapeEnabled(localToggle.checked);
      this.localSourcePrepared = false;
      this.localSourceResult = null;
      if (!localToggle.checked) this.localReshaper.dispose();
      void this.preview();
    });
    this.fieldList = el("ul", "field-list");
    rail.append(fieldsTitle, localControl, this.fieldList);

    // --- centre: chart and channels ------------------------------------
    const centre = el("div", "builder-centre");
    this.shelves = el("div", "shelves");
    this.plot = el("div", "chart-plot");
    this.status = el("div", "builder-status");
    centre.append(this.shelves, this.plot, this.status);

    // --- right: the plan ------------------------------------------------
    const side = el("aside", "builder-side");
    const planTitle = el("h3", "builder-heading");
    planTitle.textContent = "Transformation";
    const undoRow = el("div", "builder-undo");
    undoRow.append(
      button("Undo", () => this.undo()),
      button("Redo", () => this.redo()),
      button("Add step", () => this.showStepMenu()),
    );
    this.stepList = el("ol", "step-list");

    const askForm = el("form", "builder-ask") as HTMLFormElement;
    this.askInput = document.createElement("input");
    this.askInput.type = "text";
    this.askInput.className = "chart-edit-input";
    this.askInput.placeholder = "Ask the agent — “only last 90 days, by week”";
    const askButton = document.createElement("button");
    askButton.type = "submit";
    askButton.className = "btn btn-primary btn-small";
    askButton.textContent = "Ask";
    askForm.append(this.askInput, askButton);
    askForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.ask();
    });

    const exports = el("div", "builder-exports");
    exports.append(
      button("Notebook (.ipynb)", () => this.exportNotebook("ipynb")),
      button("marimo", () => this.exportNotebook("marimo")),
      button("Show code", () => this.toggleCode()),
      button(options.dashboardId ? "Add to dashboard" : "Save", () => this.save()),
    );

    side.append(planTitle, undoRow, this.stepList, askForm, exports);

    this.root.append(rail, centre, side);
  }

  // -- lifecycle ---------------------------------------------------------

  async open(source: BuilderSource): Promise<void> {
    this.source = source;
    this.steps = [];
    this.history = [];
    this.future = [];
    this.localReshaper.dispose();
    this.localSourcePrepared = false;
    this.localSourceResult = null;

    this.setStatus("Reading the data…");
    try {
      const schema = await api.post<{
        columns: ColumnInfo[];
        suggested: ChartConfig;
        rows: number;
      }>("/v1/builder/schema", source);
      this.columns = schema.columns ?? [];
      this.config = schema.suggested ?? { chart_type: "bar" };
      this.renderFields();
      this.renderShelves();
      await this.preview();
    } catch (error) {
      this.setStatus(`Could not read the data: ${(error as Error).message}`, true);
    }
  }

  // -- rendering ---------------------------------------------------------

  private renderFields(): void {
    this.fieldList.replaceChildren();
    for (const column of this.columns) {
      const item = el("li", `field field-${column.role}`);
      item.draggable = true;
      item.title = `${column.dtype} · ${column.role}`;

      const icon = el("span", "field-icon");
      icon.textContent = ROLE_ICON[column.role] ?? "?";
      const name = el("span", "field-name");
      name.textContent = column.name;
      item.append(icon, name);

      item.addEventListener("dragstart", (event) => {
        event.dataTransfer?.setData("text/plain", column.name);
        item.classList.add("is-dragging");
      });
      item.addEventListener("dragend", () => item.classList.remove("is-dragging"));

      // Clicking is the accessible path: dragging is a nice-to-have, not the
      // only way to assign a column.
      item.addEventListener("click", () => this.assignSmart(column));

      this.fieldList.append(item);
    }
  }

  private renderShelves(): void {
    this.shelves.replaceChildren();

    const typePicker = el("label", "shelf shelf-type");
    const typeLabel = el("span", "shelf-label");
    typeLabel.textContent = "Chart";
    const select = document.createElement("select");
    select.className = "control-input";
    for (const type of CHART_TYPES) {
      const option = document.createElement("option");
      option.value = type;
      option.textContent = type;
      if (type === this.config.chart_type) option.selected = true;
      select.append(option);
    }
    select.addEventListener("change", () => {
      this.push();
      this.config = { ...this.config, chart_type: select.value };
      void this.preview();
    });
    typePicker.append(typeLabel, select);
    this.shelves.append(typePicker);

    for (const channel of CHANNELS) {
      this.shelves.append(this.shelf(channel));
    }
  }

  private shelf(channel: Channel): HTMLElement {
    const wrap = el("div", "shelf");
    const label = el("span", "shelf-label");
    label.textContent = channel === "color" ? "Colour" : channel.toUpperCase();

    const slot = el("div", "shelf-slot");
    const current = (this.config as any)[channel];

    if (current) {
      const pill = el("span", "shelf-pill");
      pill.textContent = String(current);
      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "shelf-clear";
      clear.textContent = "×";
      clear.setAttribute("aria-label", `Clear ${channel}`);
      clear.addEventListener("click", () => this.assign(channel, null));
      pill.append(clear);
      slot.append(pill);
    } else {
      slot.classList.add("is-empty");
      slot.textContent = "drop a column";
    }

    slot.addEventListener("dragover", (event) => {
      event.preventDefault();
      slot.classList.add("is-over");
    });
    slot.addEventListener("dragleave", () => slot.classList.remove("is-over"));
    slot.addEventListener("drop", (event) => {
      event.preventDefault();
      slot.classList.remove("is-over");
      const name = event.dataTransfer?.getData("text/plain");
      if (name) this.assign(channel, name);
    });

    wrap.append(label, slot);
    return wrap;
  }

  private renderSteps(descriptions: string[] = []): void {
    this.stepList.replaceChildren();

    if (!this.steps.length) {
      const empty = el("li", "step-empty");
      empty.textContent =
        "No transformation yet. Add a step, or ask the agent for one.";
      this.stepList.append(empty);
      return;
    }

    this.steps.forEach((step, index) => {
      const item = el("li", `step step-${step.author ?? "agent"}`);
      if (step.enabled === false) item.classList.add("is-disabled");

      const head = el("div", "step-head");
      const badge = el("span", "step-badge");
      badge.textContent = step.author === "human" ? "you" : "AI";
      badge.title = step.author === "human" ? "You added this" : "The agent added this";

      const title = el("span", "step-title");
      title.textContent = descriptions[index] ?? step.type;

      head.append(badge, title);

      const actions = el("div", "step-actions");
      actions.append(
        this.iconButton("↑", "Move up", () => this.move(index, -1), index === 0),
        this.iconButton("↓", "Move down", () => this.move(index, 1),
          index === this.steps.length - 1),
        this.iconButton(
          step.enabled === false ? "◻" : "◼",
          step.enabled === false ? "Enable" : "Disable",
          () => this.toggleStep(index),
        ),
        this.iconButton("✕", "Remove", () => this.removeStep(index)),
      );
      head.append(actions);
      item.append(head);

      if (step.note) {
        const note = el("p", "step-note");
        note.textContent = step.note;
        item.append(note);
      }

      item.append(this.stepEditor(step, index));
      this.stepList.append(item);
    });
  }

  /** A small typed editor per step, so params are not free text. */
  private stepEditor(step: Step, index: number): HTMLElement {
    const wrap = el("div", "step-params");
    const names = this.columns.map((c) => c.name);

    const update = (patch: Record<string, any>) => {
      this.push();
      this.steps[index] = {
        ...step,
        params: { ...step.params, ...patch },
        author: "human",
      };
      void this.preview();
    };

    if (step.type === "filter") {
      wrap.append(
        this.pick("Column", names, step.params.column, (v) => update({ column: v })),
        this.pick("Test", FILTER_OPS, step.params.op ?? "eq", (v) => update({ op: v })),
        this.text("Value", String(step.params.value ?? ""), (v) => update({ value: v })),
      );
    } else if (step.type === "sort") {
      wrap.append(
        this.pick("Column", names, step.params.column, (v) => update({ column: v })),
        this.pick("Order", ["ascending", "descending"],
          step.params.desc ? "descending" : "ascending",
          (v) => update({ desc: v === "descending" })),
      );
    } else if (step.type === "limit") {
      wrap.append(this.text("Rows", String(step.params.n ?? 100),
        (v) => update({ n: Number(v) || 100 })));
    } else if (step.type === "resample") {
      wrap.append(
        this.pick("Time column", names, step.params.time_column,
          (v) => update({ time_column: v })),
        this.pick("Grain", GRAINS, step.params.grain ?? "month",
          (v) => update({ grain: v })),
      );
    } else if (step.type === "top_n") {
      wrap.append(
        this.pick("Category", names, step.params.category, (v) => update({ category: v })),
        this.pick("Measure", names, step.params.measure, (v) => update({ measure: v })),
        this.text("Keep", String(step.params.n ?? 10), (v) => update({ n: Number(v) || 10 })),
      );
    } else if (step.type === "derive") {
      wrap.append(
        this.text("Name", String(step.params.as ?? ""), (v) => update({ as: v })),
        this.text("Expression", String(step.params.expr ?? ""), (v) => update({ expr: v })),
      );
    } else if (step.type === "aggregate") {
      const metric = (step.params.metrics ?? [{}])[0] ?? {};
      wrap.append(
        this.pick("Group by", ["", ...names], (step.params.by ?? [])[0] ?? "",
          (v) => update({ by: v ? [v] : [] })),
        this.pick("Aggregate", AGGREGATIONS, metric.agg ?? "sum",
          (v) => update({ metrics: [{ ...metric, agg: v }] })),
        this.pick("Of column", names, metric.column ?? "",
          (v) => update({ metrics: [{ ...metric, column: v }] })),
      );
    }

    return wrap;
  }

  // -- actions -----------------------------------------------------------

  /** Put a column on the channel its role best fits. */
  private assignSmart(column: ColumnInfo): void {
    if (column.role === "time") this.assign("x", column.name);
    else if (column.role === "measure") this.assign("y", column.name);
    else if (!(this.config as any).x) this.assign("x", column.name);
    else this.assign("color", column.name);
  }

  private assign(channel: Channel, value: string | null): void {
    this.push();
    this.config = { ...this.config, [channel]: value } as ChartConfig;
    this.renderShelves();
    void this.preview();
  }

  private move(index: number, delta: number): void {
    const target = index + delta;
    if (target < 0 || target >= this.steps.length) return;
    this.push();
    const copy = [...this.steps];
    [copy[index], copy[target]] = [copy[target], copy[index]];
    this.steps = copy;
    void this.preview();
  }

  private toggleStep(index: number): void {
    this.push();
    this.steps[index] = {
      ...this.steps[index],
      enabled: this.steps[index].enabled === false,
    };
    void this.preview();
  }

  private removeStep(index: number): void {
    this.push();
    this.steps = this.steps.filter((_, i) => i !== index);
    void this.preview();
  }

  private showStepMenu(): void {
    const menu = el("div", "step-menu");
    const types: [string, string][] = [
      ["filter", "Keep only some rows"],
      ["aggregate", "Group and summarise"],
      ["resample", "Bucket by time"],
      ["top_n", "Top N, rest as Other"],
      ["derive", "Add a calculated column"],
      ["sort", "Sort"],
      ["limit", "Keep the first N rows"],
    ];
    for (const [type, label] of types) {
      const option = button(label, () => {
        this.push();
        this.steps = [...this.steps, {
          id: `s${this.steps.length + 1}`,
          type,
          params: this.defaultParams(type),
          author: "human",
          enabled: true,
        }];
        menu.remove();
        void this.preview();
      });
      option.classList.add("step-menu-item");
      menu.append(option);
    }
    this.stepList.append(menu);
  }

  private defaultParams(type: string): Record<string, any> {
    const time = this.columns.find((c) => c.role === "time")?.name;
    const measure = this.columns.find((c) => c.role === "measure")?.name;
    const category = this.columns.find((c) => c.role === "category")?.name;

    switch (type) {
      case "filter":
        return { column: this.columns[0]?.name, op: "eq", value: "" };
      case "aggregate":
        return { by: category ? [category] : [], metrics: [{ column: measure, agg: "sum" }] };
      case "resample":
        return { time_column: time, grain: "month",
                 metrics: [{ column: measure, agg: "sum" }] };
      case "top_n":
        return { category, measure, n: 10, agg: "sum" };
      case "derive":
        return { as: "new_column", expr: measure ? `${measure} * 1` : "" };
      case "sort":
        return { column: measure ?? this.columns[0]?.name, desc: true };
      case "limit":
        return { n: 100 };
      default:
        return {};
    }
  }

  private push(): void {
    this.history.push({
      steps: this.steps.map((s) => ({ ...s, params: { ...s.params } })),
      config: { ...this.config },
    });
    if (this.history.length > 50) this.history.shift();
    this.future = [];
  }

  private undo(): void {
    const previous = this.history.pop();
    if (!previous) return;
    this.future.push({ steps: this.steps, config: this.config });
    this.steps = previous.steps;
    this.config = previous.config;
    this.renderShelves();
    void this.preview();
  }

  private redo(): void {
    const next = this.future.pop();
    if (!next) return;
    this.history.push({ steps: this.steps, config: this.config });
    this.steps = next.steps;
    this.config = next.config;
    this.renderShelves();
    void this.preview();
  }

  /** Replace the whole plan, as the agent or a test would. */
  async setPlan(plan: unknown): Promise<void> {
    this.push();
    this.steps = (Array.isArray(plan) ? plan : []) as Step[];
    await this.preview();
  }

  // -- server round trips -------------------------------------------------

  /** Re-run the plan. Coalesced: a burst of edits produces one request. */
  private async preview(): Promise<void> {
    const version = ++this.previewVersion;
    if (this.pending) {
      this.queued = true;
      return;
    }
    this.pending = true;
    this.setStatus("Updating…");

    try {
      const outcome = await this.previewLocalThenRender(version);
      if (version !== this.previewVersion) return;
      const response = outcome.response;

      this.lastPreview = response;

      if (!response.ok) {
        this.renderSteps();
        const first = response.problems?.[0];
        this.setStatus(first ? `${first.step}: ${first.error}` : "That plan does not fit the data", true);
        return;
      }

      if (response.config) this.config = response.config;
      if (response.figure && !outcome.rendered) {
        await renderFigure(this.plot, response.figure);
      }
      this.renderSteps(response.descriptions ?? []);
      this.renderShelves();

      const bits = [`${(response.row_count ?? 0).toLocaleString()} rows`];
      if (response.elapsed_ms !== undefined) bits.push(`${response.elapsed_ms} ms`);
      for (const warning of response.warnings ?? []) bits.push(warning);
      for (const finding of response.audit ?? []) bits.push(`${finding.code}: ${finding.detail}`);
      this.setStatus(bits.join(" · "), (response.audit ?? []).length > 0);
    } catch (error) {
      this.setStatus((error as Error).message, true);
    } finally {
      this.pending = false;
      if (this.queued) {
        this.queued = false;
        void this.preview();
      }
    }
  }

  private previewBackend(): Promise<PreviewResponse> {
    return api.post<PreviewResponse>("/v1/builder/preview", {
      ...this.source,
      steps: this.steps,
      config: this.config,
      mode: document.documentElement.getAttribute("data-theme") ?? "light",
    });
  }

  private async backendPreviewOutcome(): Promise<{
    response: PreviewResponse;
    rendered: false;
  }> {
    return { response: await this.previewBackend(), rendered: false };
  }

  private async previewLocalThenRender(
    version: number,
  ): Promise<{ response: PreviewResponse; rendered: boolean }> {
    if (!localReshapeEnabled()) {
      return this.backendPreviewOutcome();
    }

    try {
      const sourceReady = await this.ensureLocalSource();
      if (!sourceReady.ok) {
        return this.backendPreviewOutcome();
      }
      const shaped = await this.localReshaper.reshape(this.steps);
      if (!shaped.ok) {
        return this.backendPreviewOutcome();
      }
      const response = await api.post<PreviewResponse>("/v1/builder/preview", {
        data: shaped.rows,
        steps: [],
        config: this.config,
        mode: document.documentElement.getAttribute("data-theme") ?? "light",
      });
      if (version !== this.previewVersion) {
        return { response, rendered: false };
      }
      if (!response.ok || !response.figure) {
        return this.backendPreviewOutcome();
      }
      await renderFigure(this.plot, response.figure);
      return {
        response: {
          ...response,
          steps: this.steps,
          elapsed_ms: (response.elapsed_ms ?? 0) + shaped.elapsedMs,
          warnings: [...shaped.warnings, ...(response.warnings ?? [])],
        },
        rendered: true,
      };
    } catch {
      return this.backendPreviewOutcome();
    }
  }

  private async ensureLocalSource(): Promise<LocalReshapeResult | { ok: true }> {
    if (this.localSourcePrepared) {
      return this.localSourceResult ?? { ok: false, fallbackReason: "unsupported_source" };
    }
    this.localSourcePrepared = true;
    const rows = this.source.data;
    if (!Array.isArray(rows)) {
      this.localSourceResult = { ok: false, fallbackReason: "unsupported_source" };
      return this.localSourceResult;
    }
    const schema: SchemaColumn[] = this.columns.map((column) => ({
      name: column.name,
      dtype: column.dtype,
      nulls: column.nulls,
    }));
    this.localSourceResult = await this.localReshaper.setRows(rows, schema);
    return this.localSourceResult;
  }

  private async ask(): Promise<void> {
    const request = this.askInput.value.trim();
    if (!request) return;
    this.askInput.value = "";
    this.setStatus("Asking the agent…");

    try {
      const response = await api.post<{
        steps: Step[];
        config: ChartConfig;
        explanation: string;
      }>("/v1/builder/suggest", {
        ...this.source,
        steps: this.steps,
        config: this.config,
        request,
      });

      // The agent's plan is a normal edit: undo puts the old one back.
      this.push();
      this.steps = response.steps ?? this.steps;
      if (response.config && Object.keys(response.config).length) {
        this.config = { ...this.config, ...response.config };
      }
      this.renderShelves();
      await this.preview();
      if (response.explanation) this.setStatus(response.explanation);
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  private async save(): Promise<void> {
    try {
      const response = await api.post<{ chart_id: string }>("/v1/builder/save", {
        ...this.source,
        steps: this.steps,
        config: this.config,
        figure: this.lastPreview?.figure,
        dashboard_id: this.options.dashboardId,
      });
      this.setStatus(`Saved as ${response.chart_id.slice(0, 8)}`);
      this.options.onSaved?.(response.chart_id);
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  private async exportNotebook(format: "ipynb" | "marimo"): Promise<void> {
    try {
      const response = await fetch(`/v1/builder/notebook?format=${format}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...this.source, steps: this.steps, config: this.config }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      const name = format === "ipynb" ? "chart.ipynb" : "chart.py";
      link.href = url;
      link.download = name;
      link.click();
      URL.revokeObjectURL(url);
      this.setStatus(
        format === "ipynb"
          ? `Downloaded ${name} — open it in Jupyter, or run it here`
          : `Downloaded ${name} — run it with \`marimo edit ${name}\``,
      );
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  private toggleCode(): void {
    const existing = this.root.querySelector(".builder-code");
    if (existing) {
      existing.remove();
      return;
    }
    const block = el("pre", "builder-code trace-code");
    block.textContent = this.lastPreview?.python ?? "# no plan yet";
    this.status.after(block);
  }

  // -- small helpers ------------------------------------------------------

  private setStatus(text: string, warn = false): void {
    this.status.textContent = text;
    this.status.classList.toggle("is-warn", warn);
  }

  private iconButton(
    glyph: string, label: string, onClick: () => void, disabled = false,
  ): HTMLButtonElement {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "step-icon";
    node.textContent = glyph;
    node.title = label;
    node.setAttribute("aria-label", label);
    node.disabled = disabled;
    node.addEventListener("click", onClick);
    return node;
  }

  private pick(
    label: string, options: string[], value: any, onChange: (v: string) => void,
  ): HTMLElement {
    const wrap = el("label", "control");
    const caption = el("span", "control-label");
    caption.textContent = label;
    const select = document.createElement("select");
    select.className = "control-input";
    for (const option of options) {
      const node = document.createElement("option");
      node.value = option;
      node.textContent = option === "" ? "—" : option;
      if (String(value ?? "") === option) node.selected = true;
      select.append(node);
    }
    select.addEventListener("change", () => onChange(select.value));
    wrap.append(caption, select);
    return wrap;
  }

  private text(label: string, value: string, onChange: (v: string) => void): HTMLElement {
    const wrap = el("label", "control");
    const caption = el("span", "control-label");
    caption.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "control-input";
    input.value = value;
    // Commit on blur or Enter, not on every keystroke: each commit is a
    // server round trip and an undo entry.
    input.addEventListener("change", () => onChange(input.value));
    wrap.append(caption, input);
    return wrap;
  }
}
