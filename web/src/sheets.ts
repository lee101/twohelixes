/** A dependency-light spreadsheet with persistence, formulas, charts and AI edits. */

import { ApiError, api, type PlotlyFigure } from "./api";
import { button, el, renderFigure } from "./chart";

type CellValue = string | number | boolean | null;
interface Cell { value?: CellValue; formula?: string; }
interface SheetChart { id: string; type: "bar" | "line" | "pie"; range: string; title: string; }
interface SheetView { name: string; filter: string; sort: string; direction: "asc" | "desc"; }
interface WorkbookSheet { name: string; rows: number; cols: number; cells: Record<string, Cell>; charts: SheetChart[]; views: SheetView[]; }
interface Workbook { version: number; active: number; sheets: WorkbookSheet[]; }
interface SheetSummary { id: string; title: string; created_at: number; updated_at: number; }
interface SheetRecord extends SheetSummary { workbook: Workbook; }
interface SetCellsOp { op: "setCells"; sheet: string; cells: Array<{ ref: string; value?: CellValue; formula?: string }>; }
interface AddChartOp { op: "addChart"; sheet: string; type: "bar" | "line" | "pie"; range: string; title: string; }
type AgentOp = SetCellsOp | AddChartOp;

const VISIBLE_ROWS = 100;
const VISIBLE_COLS = 26;

function freshWorkbook(): Workbook {
  return { version: 1, active: 0, sheets: [{ name: "Sheet1", rows: 1000, cols: 26, cells: {}, charts: [], views: [{ name: "All records", filter: "", sort: "", direction: "asc" }] }] };
}

function normalizeWorkbook(value: unknown): Workbook {
  if (!value || typeof value !== "object") return freshWorkbook();
  const raw = value as Partial<Workbook>;
  if (!Array.isArray(raw.sheets) || !raw.sheets.length) return freshWorkbook();
  const sheets = raw.sheets.map((item, index) => {
    const sheet = item && typeof item === "object" ? item as Partial<WorkbookSheet> : {};
    const cells = sheet.cells && typeof sheet.cells === "object" && !Array.isArray(sheet.cells) ? sheet.cells : {};
    const charts = Array.isArray(sheet.charts)
      ? sheet.charts.filter((chart): chart is SheetChart => Boolean(
          chart && typeof chart === "object" && ["bar", "line", "pie"].includes(chart.type) && parseRange(String(chart.range ?? "")),
        )).map((chart) => ({ ...chart, id: chart.id || crypto.randomUUID() }))
      : [];
    const views: SheetView[] = Array.isArray(sheet.views)
      ? sheet.views.filter((view): view is SheetView => Boolean(
          view && typeof view === "object" && String(view.name || "").trim(),
        )).slice(0, 20).map((view) => ({
          name: String(view.name).slice(0, 80),
          filter: String(view.filter || "").slice(0, 200),
          sort: /^[A-Z]{1,3}[1-9][0-9]{0,6}$/.test(String(view.sort || "").toUpperCase()) ? String(view.sort).toUpperCase() : "",
          direction: view.direction === "desc" ? ("desc" as const) : ("asc" as const),
        }))
      : [];
    return {
      name: String(sheet.name || `Sheet${index + 1}`).slice(0, 80),
      rows: Math.max(1, Math.min(Number(sheet.rows) || 1000, 1_000_000)),
      cols: Math.max(1, Math.min(Number(sheet.cols) || 26, 18_278)),
      cells: cells as Record<string, Cell>,
      charts,
      views: views.length ? views : [{ name: "All records", filter: "", sort: "", direction: "asc" as const }],
    };
  });
  return {
    version: 1,
    active: Math.max(0, Math.min(Number(raw.active) || 0, sheets.length - 1)),
    sheets,
  };
}

function columnName(index: number): string {
  let out = "";
  for (let n = index + 1; n; n = Math.floor((n - 1) / 26)) out = String.fromCharCode(65 + ((n - 1) % 26)) + out;
  return out;
}

function cellRef(row: number, col: number): string { return `${columnName(col)}${row + 1}`; }

function parseRef(value: string): [number, number] | null {
  const match = /^([A-Z]{1,3})([1-9][0-9]{0,6})$/i.exec(value.replaceAll("$", "").trim());
  if (!match) return null;
  let col = 0;
  for (const char of match[1].toUpperCase()) col = col * 26 + char.charCodeAt(0) - 64;
  return [Number(match[2]) - 1, col - 1];
}

function parseRange(value: string): [number, number, number, number] | null {
  const [start, end = start] = value.split(":");
  const a = parseRef(start);
  const b = parseRef(end);
  return a && b ? [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])] : null;
}

function inputValue(raw: string): Cell {
  const value = raw.trim();
  if (!value) return {};
  if (value.startsWith("=")) return { formula: value.slice(1) };
  if (/^-?(?:\d+\.?\d*|\.\d+)$/.test(value)) return { value: Number(value) };
  if (/^(true|false)$/i.test(value)) return { value: value.toLowerCase() === "true" };
  return { value: raw };
}

function rawInput(cell: Cell | undefined): string {
  if (!cell) return "";
  return cell.formula !== undefined ? `=${cell.formula}` : String(cell.value ?? "");
}

/** Tiny safe arithmetic parser: formulas never reach eval or Function. */
function arithmetic(source: string): number {
  let at = 0;
  const space = () => { while (/\s/.test(source[at] ?? "")) at += 1; };
  const expression = (): number => {
    let value = term();
    for (;;) {
      space();
      const op = source[at];
      if (op !== "+" && op !== "-") return value;
      at += 1;
      const right = term();
      value = op === "+" ? value + right : value - right;
    }
  };
  const term = (): number => {
    let value = factor();
    for (;;) {
      space();
      const op = source[at];
      if (op !== "*" && op !== "/") return value;
      at += 1;
      const right = factor();
      value = op === "*" ? value * right : value / right;
    }
  };
  const factor = (): number => {
    space();
    if (source[at] === "+" || source[at] === "-") {
      const sign = source[at++] === "-" ? -1 : 1;
      return sign * factor();
    }
    if (source[at] === "(") {
      at += 1;
      const value = expression();
      space();
      if (source[at++] !== ")") throw new Error("missing parenthesis");
      return value;
    }
    const match = /^(?:\d+\.?\d*|\.\d+)/.exec(source.slice(at));
    if (!match) throw new Error("number expected");
    at += match[0].length;
    return Number(match[0]);
  };
  const value = expression();
  space();
  if (at !== source.length || !Number.isFinite(value)) throw new Error("invalid arithmetic");
  return value;
}

function csvRows(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (quoted) {
      if (char === '"' && text[i + 1] === '"') { field += '"'; i += 1; }
      else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { row.push(field); field = ""; }
    else if (char === "\n") { row.push(field.replace(/\r$/, "")); rows.push(row); row = []; field = ""; }
    else field += char;
  }
  row.push(field.replace(/\r$/, ""));
  if (row.length > 1 || row[0]) rows.push(row);
  return rows;
}

function csvCell(value: unknown): string {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export class SheetsView {
  readonly root = el("section", "sheets-workbench");
  private workbook = freshWorkbook();
  private currentId: string | null = null;
  private title = document.createElement("input");
  private openSelect = document.createElement("select");
  private formula = document.createElement("input");
  private selection = "A1";
  private gridHost = el("div", "sheet-grid-scroll");
  private viewSelect = document.createElement("select");
  private tabsHost = el("div", "sheet-tabs");
  private chartsHost = el("div", "sheet-charts");
  private status = el("p", "workbench-status");
  private agentInput = document.createElement("textarea");
  private agentReply = el("div", "sheet-agent-reply");
  private pendingOps: AgentOp[] = [];
  private applyAgentButton = button("Apply changes", () => this.applyAgent());
  private discardAgentButton = button("Discard", () => this.clearAgent());

  constructor() {
    this.root.append(this.toolbar(), this.formulaBar(), this.body(), this.tabsHost, this.status);
    this.render();
    void this.loadList();
    this.root.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void this.save();
      }
    });
  }

  private sheet(): WorkbookSheet { return this.workbook.sheets[this.workbook.active]; }

  private toolbar(): HTMLElement {
    const bar = el("header", "sheet-toolbar");
    this.title.className = "sheet-title";
    this.title.value = "Untitled spreadsheet";
    this.title.maxLength = 160;
    this.title.setAttribute("aria-label", "Spreadsheet title");
    this.title.addEventListener("input", () => this.dirty());

    this.openSelect.className = "control-input";
    this.openSelect.setAttribute("aria-label", "Open spreadsheet");
    this.openSelect.addEventListener("change", () => {
      if (this.openSelect.value) void this.open(this.openSelect.value);
    });

    const save = button("Save", () => void this.save());
    save.classList.add("btn-primary");
    const file = document.createElement("input");
    file.type = "file";
    file.accept = ".csv,text/csv";
    file.hidden = true;
    file.addEventListener("change", () => { if (file.files?.[0]) void this.importCsv(file.files[0]); file.value = ""; });
    const importButton = button("Import CSV", () => file.click());
    this.viewSelect.className = "control-input sheet-view-select";
    this.viewSelect.setAttribute("aria-label", "Saved sheet view");
    this.viewSelect.addEventListener("change", () => { this.renderViews(); this.renderGrid(); });
    bar.append(this.title, this.openSelect, this.viewSelect, button("New", () => this.newWorkbook()), save, importButton, button("Export CSV", () => this.exportCsv()), file);
    return bar;
  }

  private formulaBar(): HTMLElement {
    const bar = el("div", "formula-bar");
    const ref = el("span", "formula-ref");
    ref.dataset.formulaRef = "";
    ref.textContent = this.selection;
    this.formula.className = "formula-input";
    this.formula.setAttribute("aria-label", "Cell value or formula");
    this.formula.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); this.commitFormula(); }
    });
    this.formula.addEventListener("blur", () => this.commitFormula());
    bar.append(ref, this.formula);
    return bar;
  }

  private body(): HTMLElement {
    const body = el("div", "sheets-layout");
    const canvas = el("div", "sheet-canvas");
    const viewTools = el("div", "sheet-view-tools");
    const filter = document.createElement("input");
    filter.className = "control-input";
    filter.placeholder = "Filter rows…";
    filter.setAttribute("aria-label", "Filter visible rows");
    filter.addEventListener("input", () => { this.activeView().filter = filter.value.slice(0, 200); this.dirty(); this.renderGrid(); });
    const sort = document.createElement("input");
    sort.className = "control-input";
    sort.placeholder = "Sort by cell (e.g. B1)";
    sort.setAttribute("aria-label", "Sort rows by column");
    sort.addEventListener("change", () => { this.activeView().sort = sort.value.trim().toUpperCase(); this.dirty(); this.renderGrid(); });
    const direction = button("Ascending", () => { const view = this.activeView(); view.direction = view.direction === "asc" ? "desc" : "asc"; direction.textContent = view.direction === "asc" ? "Ascending" : "Descending"; this.dirty(); this.renderGrid(); });
    const addView = button("Save view", () => { const name = window.prompt("View name", `View ${this.sheet().views.length + 1}`)?.trim(); if (name) { this.sheet().views.push({ name: name.slice(0, 80), filter: filter.value.slice(0, 200), sort: sort.value.trim().toUpperCase(), direction: "asc" }); this.render(); } });
    viewTools.append(filter, sort, direction, addView);
    canvas.append(viewTools, this.gridHost);
    const side = el("aside", "sheet-side");
    const agentTitle = el("h2"); agentTitle.textContent = "Sheets agent";
    const agentDetail = el("p", "workbench-hint");
    agentDetail.textContent = "The agent proposes cell and chart operations. You approve them before they are applied.";
    this.agentInput.className = "ask-input";
    this.agentInput.rows = 3;
    this.agentInput.placeholder = "“Add a totals row and chart monthly revenue”";
    const ask = button("Propose changes", () => void this.askAgent());
    ask.classList.add("btn-primary");
    this.applyAgentButton.classList.add("btn-primary");
    this.applyAgentButton.hidden = true;
    this.discardAgentButton.hidden = true;
    const agentActions = el("div", "sheet-agent-actions");
    agentActions.append(ask, this.applyAgentButton, this.discardAgentButton);

    const chartTitle = el("h2"); chartTitle.textContent = "Charts";
    const chartBar = el("div", "sheet-chart-bar");
    for (const type of ["bar", "line", "pie"] as const) chartBar.append(button(`Add ${type}`, () => this.addChart(type)));
    side.append(agentTitle, agentDetail, this.agentInput, agentActions, this.agentReply, chartTitle, chartBar, this.chartsHost);
    body.append(canvas, side);
    return body;
  }

  private render(): void {
    this.renderViews();
    this.renderGrid();
    this.renderTabs();
    void this.renderCharts();
    this.select(this.selection);
  }

  private renderGrid(): void {
    this.gridHost.replaceChildren();
    const table = el("table", "sheet-grid") as HTMLTableElement;
    const head = table.createTHead().insertRow();
    head.append(document.createElement("th"));
    for (let col = 0; col < VISIBLE_COLS; col += 1) { const th = document.createElement("th"); th.textContent = columnName(col); head.append(th); }
    const body = table.createTBody();
    const rows = this.visibleRows();
    for (let row = 0; row < Math.min(VISIBLE_ROWS, rows.length); row += 1) {
      const sourceRow = rows[row];
      const tr = body.insertRow();
      const rowHead = document.createElement("th"); rowHead.textContent = String(sourceRow + 1); tr.append(rowHead);
      for (let col = 0; col < VISIBLE_COLS; col += 1) {
        const ref = cellRef(sourceRow, col);
        const td = tr.insertCell();
        const input = document.createElement("input");
        input.className = "sheet-cell-input";
        input.dataset.ref = ref;
        input.value = this.displayCell(ref);
        input.setAttribute("aria-label", ref);
        input.addEventListener("focus", () => { this.select(ref); input.value = rawInput(this.sheet().cells[ref]); });
        input.addEventListener("blur", () => this.commitCell(ref, input.value));
        input.addEventListener("keydown", (event) => {
          if (event.key === "Enter") { event.preventDefault(); input.blur(); this.focusCell(cellRef(Math.min(row + 1, VISIBLE_ROWS - 1), col)); }
          else if (event.key === "Tab") this.commitCell(ref, input.value);
        });
        td.append(input);
      }
    }
    this.gridHost.append(table);
  }

  private activeView(): SheetView { return this.sheet().views[Math.max(0, Math.min(Number(this.viewSelect.value || 0), this.sheet().views.length - 1))]; }

  private renderViews(): void {
    this.viewSelect.replaceChildren();
    this.sheet().views.forEach((view, index) => { const option = document.createElement("option"); option.value = String(index); option.textContent = view.name; this.viewSelect.append(option); });
    this.viewSelect.value = this.viewSelect.value || "0";
    const view = this.activeView();
    const filter = this.root.querySelector<HTMLInputElement>('input[aria-label="Filter visible rows"]');
    const sort = this.root.querySelector<HTMLInputElement>('input[aria-label="Sort rows by column"]');
    if (filter) filter.value = view.filter;
    if (sort) sort.value = view.sort;
  }

  private visibleRows(): number[] {
    const view = this.activeView();
    const rows = Array.from({ length: Math.max(VISIBLE_ROWS, this.dataRowCount()) }, (_, index) => index);
    const needle = view.filter.trim().toLowerCase();
    const filtered = needle ? rows.filter((row) => Array.from({ length: VISIBLE_COLS }, (_, col) => this.displayCell(cellRef(row, col))).some((value) => value.toLowerCase().includes(needle))) : rows;
    if (!view.sort) return filtered;
    const sortRef = parseRef(view.sort);
    if (!sortRef) return filtered;
    return filtered.sort((a, b) => { const left = this.displayCell(cellRef(a, sortRef[1])); const right = this.displayCell(cellRef(b, sortRef[1])); const numeric = Number(left) - Number(right); const comparison = Number.isNaN(numeric) ? left.localeCompare(right) : numeric; return view.direction === "desc" ? -comparison : comparison; });
  }

  private dataRowCount(): number {
    let last = 0;
    for (const ref of Object.keys(this.sheet().cells)) {
      const parsed = parseRef(ref);
      if (parsed) last = Math.max(last, parsed[0]);
    }
    return Math.min(Math.max(last + 1, 1), 10_000);
  }

  private select(ref: string): void {
    this.selection = parseRef(ref) ? ref.toUpperCase() : "A1";
    const cell = this.sheet().cells[this.selection];
    this.formula.value = rawInput(cell);
    const label = this.root.querySelector<HTMLElement>("[data-formula-ref]");
    if (label) label.textContent = this.selection;
    for (const input of this.gridHost.querySelectorAll<HTMLInputElement>(".is-active")) input.classList.remove("is-active");
    this.gridHost.querySelector<HTMLInputElement>(`[data-ref="${this.selection}"]`)?.classList.add("is-active");
  }

  private focusCell(ref: string): void { this.gridHost.querySelector<HTMLInputElement>(`[data-ref="${ref}"]`)?.focus(); }

  private commitCell(ref: string, value: string): void {
    const before = rawInput(this.sheet().cells[ref]);
    if (value !== before) {
      const parsed = inputValue(value);
      if (Object.keys(parsed).length) this.sheet().cells[ref] = parsed;
      else delete this.sheet().cells[ref];
      this.dirty();
      this.renderGrid();
      void this.renderCharts();
    }
    this.select(ref);
  }

  private commitFormula(): void { this.commitCell(this.selection, this.formula.value); this.focusCell(this.selection); }

  private valueAt(ref: string, stack = new Set<string>()): CellValue | string {
    const cell = this.sheet().cells[ref.toUpperCase()];
    if (!cell) return null;
    if (cell.formula === undefined) return cell.value ?? null;
    if (stack.has(ref)) return "#CYCLE";
    const next = new Set(stack); next.add(ref);
    try {
      let formula = cell.formula;
      formula = formula.replace(/\b(SUM|AVERAGE|MIN|MAX|COUNT)\(([A-Z]+\d+):([A-Z]+\d+)\)/gi, (_all, fn, start, end) => {
        const range = parseRange(`${start}:${end}`);
        if (!range) throw new Error("range");
        const values: number[] = [];
        const area = (range[2] - range[0] + 1) * (range[3] - range[1] + 1);
        if (area > 100_000) throw new Error("range too large");
        for (let row = range[0]; row <= range[2]; row += 1) for (let col = range[1]; col <= range[3]; col += 1) {
          const value = Number(this.valueAt(cellRef(row, col), next));
          if (Number.isFinite(value)) values.push(value);
        }
        if (String(fn).toUpperCase() === "COUNT") return String(values.length);
        if (!values.length) return "0";
        if (String(fn).toUpperCase() === "SUM") return String(values.reduce((a, b) => a + b, 0));
        if (String(fn).toUpperCase() === "AVERAGE") return String(values.reduce((a, b) => a + b, 0) / values.length);
        if (String(fn).toUpperCase() === "MIN") return String(Math.min(...values));
        return String(Math.max(...values));
      });
      formula = formula.replace(/\b([A-Z]{1,3}[1-9][0-9]{0,6})\b/gi, (_all, name) => {
        const value = Number(this.valueAt(String(name).toUpperCase(), next));
        return Number.isFinite(value) ? String(value) : "0";
      });
      return arithmetic(formula);
    } catch { return "#ERROR"; }
  }

  private displayCell(ref: string): string { const value = this.valueAt(ref); return value === null ? "" : String(value); }

  private renderTabs(): void {
    this.tabsHost.replaceChildren();
    this.workbook.sheets.forEach((sheet, index) => {
      const tab = button(sheet.name, () => { this.workbook.active = index; this.selection = "A1"; this.render(); });
      tab.classList.add("sheet-tab");
      tab.classList.toggle("is-active", index === this.workbook.active);
      this.tabsHost.append(tab);
    });
    this.tabsHost.append(
      button("+", () => {
        this.workbook.sheets.push({ name: `Sheet${this.workbook.sheets.length + 1}`, rows: 1000, cols: 26, cells: {}, charts: [], views: [{ name: "All records", filter: "", sort: "", direction: "asc" }] });
        this.workbook.active = this.workbook.sheets.length - 1; this.dirty(); this.render();
      }),
      button("Rename", () => {
        const name = window.prompt("Sheet name", this.sheet().name)?.trim();
        if (name) { this.sheet().name = name.slice(0, 80); this.dirty(); this.renderTabs(); }
      }),
      button("Delete", () => {
        if (this.workbook.sheets.length <= 1 || !window.confirm(`Delete ${this.sheet().name}?`)) return;
        this.workbook.sheets.splice(this.workbook.active, 1);
        this.workbook.active = Math.max(0, this.workbook.active - 1); this.dirty(); this.render();
      }),
    );
  }

  private async loadList(): Promise<void> {
    try {
      const result = await api.get<{ sheets: SheetSummary[] }>("/v1/sheets");
      this.openSelect.replaceChildren();
      const empty = document.createElement("option"); empty.value = ""; empty.textContent = "Open spreadsheet"; this.openSelect.append(empty);
      for (const item of result.sheets) { const option = document.createElement("option"); option.value = item.id; option.textContent = item.title; this.openSelect.append(option); }
      this.openSelect.value = this.currentId ?? "";
    } catch (error) { this.showError(error); }
  }

  private async open(id: string): Promise<void> {
    try {
      this.setStatus("Opening…");
      const result = await api.get<{ sheet: SheetRecord }>(`/v1/sheets/${encodeURIComponent(id)}`);
      this.currentId = result.sheet.id;
      this.title.value = result.sheet.title;
      this.workbook = normalizeWorkbook(result.sheet.workbook);
      this.selection = "A1";
      this.render(); this.openSelect.value = id; this.setStatus("Opened.");
    } catch (error) { this.showError(error); }
  }

  private newWorkbook(): void {
    this.currentId = null; this.title.value = "Untitled spreadsheet"; this.workbook = freshWorkbook(); this.selection = "A1"; this.openSelect.value = ""; this.clearAgent(); this.render(); this.setStatus("New spreadsheet.");
  }

  private async save(): Promise<void> {
    const title = this.title.value.trim() || "Untitled spreadsheet";
    try {
      this.setStatus("Saving…");
      const result = this.currentId
        ? await api.put<{ sheet: SheetRecord }>(`/v1/sheets/${encodeURIComponent(this.currentId)}`, { title, workbook: this.workbook })
        : await api.post<{ sheet: SheetRecord }>("/v1/sheets", { title, workbook: this.workbook });
      this.currentId = result.sheet.id; this.title.value = result.sheet.title; this.workbook = normalizeWorkbook(result.sheet.workbook);
      await this.loadList(); this.setStatus("Saved.");
    } catch (error) { this.showError(error); }
  }

  private async askAgent(): Promise<void> {
    const instruction = this.agentInput.value.trim();
    if (!instruction) { this.setStatus("Describe the changes you want.", true); return; }
    try {
      this.agentReply.textContent = "Agent is reviewing the workbook…";
      const result = await api.post<{ reply: string; ops: AgentOp[] }>("/v1/sheets/agent", { instruction, workbook: this.workbook });
      this.pendingOps = result.ops ?? [];
      this.agentReply.replaceChildren();
      const reply = el("p"); reply.textContent = result.reply || "Proposal ready.";
      const summary = el("p", "workbench-hint");
      summary.textContent = this.pendingOps.length ? `${this.pendingOps.length} proposed operation${this.pendingOps.length === 1 ? "" : "s"}.` : "No workbook changes proposed.";
      this.agentReply.append(reply, summary);
      this.applyAgentButton.hidden = !this.pendingOps.length;
      this.discardAgentButton.hidden = !this.pendingOps.length;
      this.setStatus("Review the proposal before applying it.");
    } catch (error) { this.agentReply.textContent = ""; this.showError(error); }
  }

  private applyAgent(): void {
    for (const op of this.pendingOps) {
      const sheet = this.workbook.sheets.find((item) => item.name === op.sheet) ?? this.sheet();
      if (op.op === "setCells") for (const cell of op.cells) {
        if (cell.formula !== undefined) sheet.cells[cell.ref] = { formula: cell.formula.replace(/^=/, "") };
        else sheet.cells[cell.ref] = { value: cell.value ?? null };
      }
      else sheet.charts.push({ id: crypto.randomUUID(), type: op.type, range: op.range, title: op.title });
    }
    this.clearAgent(); this.dirty(); this.render(); this.setStatus("Agent changes applied. Save to persist them.");
  }

  private clearAgent(): void { this.pendingOps = []; this.agentReply.replaceChildren(); this.applyAgentButton.hidden = true; this.discardAgentButton.hidden = true; }

  private addChart(type: "bar" | "line" | "pie"): void {
    const range = window.prompt("Chart range", `${this.selection}:${this.selection}`)?.toUpperCase();
    if (!range || !parseRange(range)) return;
    this.sheet().charts.push({ id: crypto.randomUUID(), type, range, title: `${type[0].toUpperCase()}${type.slice(1)} chart` });
    this.dirty(); void this.renderCharts();
  }

  private async renderCharts(): Promise<void> {
    this.chartsHost.replaceChildren();
    if (!this.sheet().charts.length) { const empty = el("p", "workbench-empty"); empty.textContent = "Select a range and add a chart, or ask the agent."; this.chartsHost.append(empty); return; }
    for (const chart of this.sheet().charts) {
      const range = parseRange(chart.range); if (!range) continue;
      const values: CellValue[][] = [];
      for (let row = range[0]; row <= range[2]; row += 1) {
        const line: CellValue[] = [];
        for (let col = range[1]; col <= range[3]; col += 1) line.push(this.valueAt(cellRef(row, col)) as CellValue);
        values.push(line);
      }
      const body = values.length > 1 ? values.slice(1) : values;
      const labels = body.map((row, index) => String(row[0] ?? index + 1));
      const data = chart.type === "pie"
        ? [{ type: "pie", labels, values: body.map((row) => Number(row[1] ?? row[0]) || 0) }]
        : Array.from({ length: Math.max(1, values[0]?.length - 1) }, (_, index) => ({
            type: chart.type === "line" ? "scatter" : "bar",
            mode: chart.type === "line" ? "lines+markers" : undefined,
            name: String(values[0]?.[index + 1] ?? `Series ${index + 1}`),
            x: labels,
            y: body.map((row) => Number(row[index + 1] ?? row[0]) || 0),
          }));
      const card = el("section", "sheet-chart-card");
      const head = el("div", "sheet-chart-head");
      const title = el("strong"); title.textContent = chart.title;
      head.append(title, button("Remove", () => { this.sheet().charts = this.sheet().charts.filter((item) => item.id !== chart.id); this.dirty(); void this.renderCharts(); }));
      const host = el("div", "sheet-chart-plot");
      card.append(head, host); this.chartsHost.append(card);
      void renderFigure(host, { data, layout: { title: { text: chart.title }, margin: { l: 42, r: 12, t: 48, b: 42 }, height: 260 } } as PlotlyFigure);
    }
  }

  private async importCsv(file: File): Promise<void> {
    const rows = csvRows(await file.text());
    this.sheet().cells = {};
    rows.forEach((row, r) => row.forEach((value, c) => { if (value !== "") this.sheet().cells[cellRef(r, c)] = inputValue(value); }));
    this.dirty(); this.render(); this.setStatus(`Imported ${rows.length.toLocaleString()} rows.`);
  }

  private exportCsv(): void {
    let lastRow = 0, lastCol = 0;
    for (const ref of Object.keys(this.sheet().cells)) { const parsed = parseRef(ref); if (parsed) { lastRow = Math.max(lastRow, parsed[0]); lastCol = Math.max(lastCol, parsed[1]); } }
    const lines: string[] = [];
    for (let row = 0; row <= lastRow; row += 1) lines.push(Array.from({ length: lastCol + 1 }, (_, col) => csvCell(this.valueAt(cellRef(row, col)))).join(","));
    const url = URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = `${this.title.value.trim().replace(/[^a-z0-9_-]+/gi, "-") || "spreadsheet"}.csv`; link.click(); URL.revokeObjectURL(url);
  }

  private dirty(): void { this.setStatus("Unsaved changes."); }
  private setStatus(message: string, error = false): void { this.status.textContent = message; this.status.classList.toggle("is-error", error); }
  private showError(error: unknown): void { this.setStatus(error instanceof ApiError ? error.message : String(error), true); }
}
