/** A complete, read-only SQL workbench over the /v1/sql API. */

import { autocompletion, type CompletionContext, type CompletionResult } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { sql } from "@codemirror/lang-sql";
import { EditorState } from "@codemirror/state";
import { drawSelection, EditorView, highlightActiveLine, keymap, lineNumbers } from "@codemirror/view";
import { ApiError, api } from "./api";
import { button, el } from "./chart";

export interface SqlSource {
  id: string;
  name: string;
  kind: string;
  supports_sql: boolean;
}

interface Column { name: string; type: string; nullable: boolean; }
interface Table { name: string; qualified: string; schema: string; columns: Column[]; }
interface Schema { dialect: string; supports_sql: boolean; tables: Table[]; }
interface QueryResult {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  duration_ms: number;
  truncated: boolean;
}
interface SavedQuery {
  id: string;
  name: string;
  sql: string;
  source_id: string | null;
  description?: string;
}
interface HistoryItem {
  sql: string;
  ok: number | boolean;
  row_count: number;
  duration_ms: number;
  error: string;
  created_at: number;
}

export class SQLWorkbench {
  readonly root = el("section", "sql-workbench");
  private sources: SqlSource[];
  private sourceId = "";
  private editor!: EditorView;
  private schema: Schema | null = null;
  private schemaHost = el("div", "sql-schema-body");
  private resultHost = el("div", "sql-results");
  private savedHost = el("div", "sql-side-list");
  private historyHost = el("div", "sql-side-list");
  private status = el("p", "workbench-status");
  private sourceSelect = document.createElement("select");
  private prompt = document.createElement("textarea");
  private saveName = document.createElement("input");

  constructor(sources: SqlSource[]) {
    this.sources = sources;
    this.root.append(this.header(), this.layout());
    this.mountEditor();
    this.setSources(sources);
    void this.refreshLists();
  }

  setSources(sources: SqlSource[]): void {
    const previous = this.sourceId;
    this.sources = sources.filter((source) => source.supports_sql);
    this.sourceSelect.replaceChildren();
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = this.sources.length ? "Choose a SQL source" : "Connect a SQL source first";
    this.sourceSelect.append(empty);
    for (const source of this.sources) {
      const option = document.createElement("option");
      option.value = source.id;
      option.textContent = `${source.name} · ${source.kind}`;
      this.sourceSelect.append(option);
    }
    this.sourceId = this.sources.some((source) => source.id === previous)
      ? previous
      : (this.sources[0]?.id ?? "");
    this.sourceSelect.value = this.sourceId;
    if (this.sourceId) void this.loadSchema();
    else this.renderSchema();
  }

  private header(): HTMLElement {
    const header = el("header", "workbench-head");
    const copy = el("div");
    const title = el("h1");
    title.textContent = "SQL workbench";
    const detail = el("p");
    detail.textContent = "Explore a connected database with read-only queries.";
    copy.append(title, detail);
    this.sourceSelect.className = "control-input sql-source-select";
    this.sourceSelect.setAttribute("aria-label", "SQL data source");
    this.sourceSelect.addEventListener("change", () => {
      this.sourceId = this.sourceSelect.value;
      void this.loadSchema();
    });
    header.append(copy, this.sourceSelect);
    return header;
  }

  private layout(): HTMLElement {
    const layout = el("div", "sql-layout");
    const side = el("aside", "sql-side");
    const schemaTitle = el("h2");
    schemaTitle.textContent = "Schema";
    side.append(schemaTitle, this.schemaHost, this.sideDisclosure("Saved queries", this.savedHost, true), this.sideDisclosure("History", this.historyHost));

    const main = el("div", "sql-main");
    const agent = el("section", "sql-agent");
    const agentCopy = el("div", "sql-agent-copy");
    const agentTitle = el("strong");
    agentTitle.textContent = "Draft with Helix";
    const agentHint = el("span");
    agentHint.textContent = "Uses the selected schema. Review before running.";
    agentCopy.append(agentTitle, agentHint);
    this.prompt.className = "ask-input";
    this.prompt.rows = 2;
    this.prompt.placeholder = "Ask the agent to write SQL — “monthly revenue by region this year”";
    const generate = button("Generate SQL", () => void this.generate());
    generate.classList.add("btn-primary");
    agent.append(agentCopy, this.prompt, generate);

    const editorCard = el("section", "sql-editor-card");
    const editorBar = el("div", "sql-editor-bar");
    const hint = el("span", "workbench-hint");
    hint.textContent = "⌘/Ctrl + Enter to run";
    const run = button("Run", () => void this.run());
    run.classList.add("btn-primary");
    editorBar.append(hint, run);
    const editorHost = el("div", "sql-editor-host");
    editorHost.dataset.editor = "";
    editorCard.append(editorBar, editorHost);

    const saveBar = el("div", "sql-save-bar");
    this.saveName.className = "control-input";
    this.saveName.placeholder = "Query name";
    this.saveName.maxLength = 160;
    saveBar.append(this.saveName, button("Save query", () => void this.save()));

    this.status.setAttribute("role", "status");
    this.status.textContent = "Choose a source to begin.";
    main.append(agent, editorCard, saveBar, this.status, this.resultHost);
    layout.append(main, side);
    return layout;
  }

  private sideDisclosure(label: string, content: HTMLElement, open = false): HTMLElement {
    const details = document.createElement("details");
    details.className = "sql-side-section";
    details.open = open;
    const summary = document.createElement("summary");
    summary.textContent = label;
    details.append(summary, content);
    return details;
  }

  private mountEditor(): void {
    const parent = this.root.querySelector<HTMLElement>("[data-editor]")!;
    this.editor = new EditorView({
      parent,
      state: EditorState.create({
        doc: "SELECT *\nFROM ",
        extensions: [
          lineNumbers(),
          history(),
          drawSelection(),
          highlightActiveLine(),
          sql(),
          autocompletion({ override: [(context) => this.complete(context)] }),
          keymap.of([
            { key: "Mod-Enter", run: () => { void this.run(); return true; } },
            { key: "Mod-s", run: () => { void this.save(); return true; } },
            ...defaultKeymap,
            ...historyKeymap,
          ]),
          EditorView.lineWrapping,
          EditorView.theme({
            "&": { height: "17rem" },
            ".cm-scroller": { overflow: "auto", fontFamily: "var(--mono, ui-monospace, monospace)" },
            ".cm-content": { padding: "0.75rem 0" },
            ".cm-gutters": { backgroundColor: "transparent", border: "0" },
          }),
        ],
      }),
    });
  }

  private complete(context: CompletionContext): CompletionResult | null {
    const word = context.matchBefore(/[A-Za-z_][A-Za-z0-9_.]*/);
    if (!word && !context.explicit) return null;
    const options: { label: string; detail: string; type: string }[] = [];
    for (const table of this.schema?.tables ?? []) {
      options.push({ label: table.qualified, detail: `${table.columns.length} columns`, type: "class" });
      for (const column of table.columns) {
        options.push({ label: column.name, detail: `${table.name} · ${column.type}`, type: "property" });
      }
    }
    for (const keyword of ["SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "LIMIT", "JOIN", "COUNT", "SUM", "AVG", "WITH"]) {
      options.push({ label: keyword, detail: "keyword", type: "keyword" });
    }
    return { from: word?.from ?? context.pos, options };
  }

  private query(): string { return this.editor.state.doc.toString().trim(); }

  private setQuery(value: string): void {
    this.editor.dispatch({ changes: { from: 0, to: this.editor.state.doc.length, insert: value } });
    this.editor.focus();
  }

  private async loadSchema(): Promise<void> {
    if (!this.sourceId) { this.schema = null; this.renderSchema(); return; }
    this.setStatus("Loading schema…");
    try {
      this.schema = await api.get<Schema>(`/v1/sql/schema?source_id=${encodeURIComponent(this.sourceId)}`);
      this.renderSchema();
      this.setStatus(`${this.schema.dialect} · ${this.schema.tables.length} tables`);
    } catch (error) {
      this.schema = null;
      this.renderSchema();
      this.showError(error);
    }
  }

  private renderSchema(): void {
    this.schemaHost.replaceChildren();
    if (!this.sourceId) {
      const empty = el("p", "workbench-empty");
      empty.textContent = "Connect or choose a SQL source.";
      this.schemaHost.append(empty);
      return;
    }
    for (const table of this.schema?.tables ?? []) {
      const details = document.createElement("details");
      details.className = "sql-table";
      const summary = document.createElement("summary");
      summary.textContent = table.qualified;
      const list = el("ul");
      for (const column of table.columns) {
        const item = el("li");
        const name = el("button") as HTMLButtonElement;
        name.type = "button";
        name.textContent = column.name;
        name.addEventListener("click", () => this.editor.dispatch({ changes: { from: this.editor.state.selection.main.head, insert: column.name } }));
        const kind = el("span");
        kind.textContent = column.type;
        item.append(name, kind);
        list.append(item);
      }
      details.append(summary, list);
      this.schemaHost.append(details);
    }
  }

  private async generate(): Promise<void> {
    const question = this.prompt.value.trim();
    if (!this.sourceId || !question) {
      this.setStatus(!this.sourceId ? "Choose a SQL source first." : "Describe the query you want.", true);
      return;
    }
    this.setStatus("Agent is writing a read-only query…");
    try {
      const result = await api.post<{ sql: string; explanation: string }>("/v1/sql/generate", {
        source_id: this.sourceId,
        question,
      });
      this.setQuery(result.sql);
      this.setStatus(result.explanation || "Query generated. Review it, then run.");
    } catch (error) { this.showError(error); }
  }

  private async run(): Promise<void> {
    const query = this.query();
    if (!this.sourceId || !query) {
      this.setStatus(!this.sourceId ? "Choose a SQL source first." : "Write a query first.", true);
      return;
    }
    this.setStatus("Running…");
    try {
      const result = await api.post<QueryResult>("/v1/sql/run?limit=1000", { source_id: this.sourceId, sql: query });
      this.renderResult(result);
      this.setStatus(`${result.row_count.toLocaleString()} rows · ${result.duration_ms.toLocaleString()} ms${result.truncated ? " · truncated" : ""}`);
      void this.loadHistory();
    } catch (error) { this.showError(error); }
  }

  private renderResult(result: QueryResult): void {
    this.resultHost.replaceChildren();
    const scroll = el("div", "sql-result-scroll");
    const table = el("table", "sql-result-table") as HTMLTableElement;
    const head = table.createTHead().insertRow();
    for (const column of result.columns) {
      const cell = document.createElement("th");
      cell.textContent = column;
      head.append(cell);
    }
    const body = table.createTBody();
    for (const row of result.rows) {
      const tr = body.insertRow();
      for (const value of row) {
        const cell = tr.insertCell();
        cell.textContent = value === null ? "NULL" : typeof value === "object" ? JSON.stringify(value) : String(value);
        if (value === null) cell.className = "is-null";
        cell.title = cell.textContent;
      }
    }
    scroll.append(table);
    this.resultHost.append(scroll);
  }

  private async save(): Promise<void> {
    const name = this.saveName.value.trim();
    const query = this.query();
    if (!name || !query) { this.setStatus("Enter a query name before saving.", true); return; }
    try {
      await api.post("/v1/queries", { name, sql: query, source_id: this.sourceId });
      this.saveName.value = "";
      this.setStatus(`Saved “${name}”.`);
      await this.loadSaved();
    } catch (error) { this.showError(error); }
  }

  private async refreshLists(): Promise<void> { await Promise.all([this.loadSaved(), this.loadHistory()]); }

  private async loadSaved(): Promise<void> {
    try {
      const result = await api.get<{ queries: SavedQuery[] }>("/v1/queries");
      this.savedHost.replaceChildren(...result.queries.map((item) => this.queryItem(item.name, item.sql, () => {
        if (item.source_id && this.sources.some((source) => source.id === item.source_id)) {
          this.sourceId = item.source_id;
          this.sourceSelect.value = item.source_id;
          void this.loadSchema();
        }
        this.setQuery(item.sql);
      }, () => void this.deleteSaved(item.id))));
      if (!result.queries.length) this.savedHost.append(this.empty("No saved queries."));
    } catch { this.savedHost.replaceChildren(this.empty("Saved queries unavailable.")); }
  }

  private async deleteSaved(id: string): Promise<void> {
    try { await api.del(`/v1/queries/${encodeURIComponent(id)}`); await this.loadSaved(); }
    catch (error) { this.showError(error); }
  }

  private async loadHistory(): Promise<void> {
    try {
      const result = await api.get<{ history: HistoryItem[] }>("/v1/sql/history?limit=30");
      this.historyHost.replaceChildren(...result.history.map((item) => this.queryItem(
        `${item.ok ? "Ran" : "Failed"} · ${item.duration_ms ?? 0} ms`, item.sql, () => this.setQuery(item.sql),
      )));
      if (!result.history.length) this.historyHost.append(this.empty("No query history."));
    } catch { this.historyHost.replaceChildren(this.empty("History unavailable.")); }
  }

  private queryItem(label: string, sqlText: string, open: () => void, remove?: () => void): HTMLElement {
    const row = el("div", "sql-query-item");
    const node = el("button") as HTMLButtonElement;
    node.type = "button";
    const strong = el("strong");
    strong.textContent = label;
    const code = el("code");
    code.textContent = sqlText.replace(/\s+/g, " ").slice(0, 110);
    node.append(strong, code);
    node.addEventListener("click", open);
    row.append(node);
    if (remove) {
      const del = button("Delete", remove);
      del.classList.add("btn-small");
      row.append(del);
    }
    return row;
  }

  private empty(message: string): HTMLElement { const node = el("p", "workbench-empty"); node.textContent = message; return node; }
  private setStatus(message: string, error = false): void { this.status.textContent = message; this.status.classList.toggle("is-error", error); }
  private showError(error: unknown): void { this.setStatus(error instanceof ApiError ? error.message : String(error), true); }
}
