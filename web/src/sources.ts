import {
  ApiError,
  api,
  type DatasetPreview,
  type DatasetShape,
  type LibraryDataset,
  type LibraryFolder,
  type LibraryResponse,
  type LibrarySource,
} from "./api";
import { el } from "./chart";

export interface SourceSummary {
  id: string;
  name: string;
  kind: string;
  display_name?: string;
  supports_sql: boolean;
  dialect?: string;
  last_error?: string | null;
}

export interface DatasetSummary {
  id: string;
  name: string;
  description: string;
  columns: string[];
  row_count: number;
  created_at: number;
}

interface ConnectorField {
  name: string;
  label: string;
  type: "text" | "number" | "password";
}

interface ConnectorKind {
  kind: string;
  display_name: string;
  supports_sql: boolean;
  dialect: string;
  fields: ConnectorField[];
}

interface ConnectionTest {
  ok: boolean;
  error: string;
  duration_ms: number;
  kind: string;
}

interface UploadResult {
  dataset_id: string;
  name: string;
  rows: number;
  columns: string[] | number;
  shape?: DatasetShape | null;
}

interface UploadItem {
  file: File;
  progress: number;
  state: "queued" | "uploading" | "success" | "error";
  result?: UploadResult;
  dataset?: LibraryDataset;
  preview?: DatasetPreview;
  error?: string;
}

type LibraryItem =
  | { type: "dataset"; value: LibraryDataset }
  | { type: "source"; value: LibrarySource };

export interface SourcesPanelOptions {
  onChange: (
    sources: SourceSummary[],
    datasets: DatasetSummary[],
    selected?: { type: "source" | "dataset"; id: string },
  ) => void;
}

const ACCEPTED_FILES = ".csv,.tsv,.xlsx,.xls,.ods,.parquet,.json";
const FOCUSABLE =
  'button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
  'textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

export class SourcesPanel {
  readonly root: HTMLElement;
  private readonly drawer: HTMLElement;
  private readonly inventory: HTMLElement;
  private readonly search: HTMLInputElement;
  private readonly fileInput: HTMLInputElement;
  private library: LibraryResponse = { folders: [], datasets: [], sources: [] };
  private sources: SourceSummary[] = [];
  private datasets: DatasetSummary[] = [];
  private uploads: UploadItem[] = [];
  private collapsed = new Set<string>();
  private selectedFolder: string | null = null;
  private libraryAvailable = true;
  private sheet: HTMLElement | null = null;
  private sheetBody: HTMLElement | null = null;
  private uploadSheetOpen = false;
  private previousFocus: HTMLElement | null = null;
  private historyToken = 0;
  private messageText = "";
  private messageError = false;
  private pendingDeletes = new Map<string, number>();
  private kinds: ConnectorKind[] = [];
  private connectKind: ConnectorKind | null = null;
  private connectStep = 0;
  private connectName = "";
  private connectValues: Record<string, string> = {};
  private connectStatus = "";
  private connectStatusState: "idle" | "ok" | "error" = "idle";
  private connectBusy = false;

  constructor(private readonly options: SourcesPanelOptions) {
    this.root = el("div", "library-shell");
    this.root.setAttribute("aria-hidden", "true");

    const backdrop = el("button", "library-backdrop") as HTMLButtonElement;
    backdrop.type = "button";
    backdrop.tabIndex = -1;
    backdrop.setAttribute("aria-label", "Close library");
    backdrop.addEventListener("click", () => this.close());

    this.drawer = el("aside", "library-drawer");
    this.drawer.tabIndex = -1;
    this.drawer.setAttribute("role", "dialog");
    this.drawer.setAttribute("aria-modal", "true");
    this.drawer.setAttribute("aria-labelledby", "library-title");

    const header = el("header", "library-header");
    const headingWrap = el("div");
    const eyebrow = el("span", "library-eyebrow");
    eyebrow.textContent = "Workspace";
    const heading = el("h2");
    heading.id = "library-title";
    heading.textContent = "Data library";
    headingWrap.append(eyebrow, heading);
    header.append(headingWrap, this.closeButton(() => this.close()));

    const searchWrap = el("label", "library-search");
    this.search = document.createElement("input");
    this.search.type = "search";
    this.search.className = "control-input";
    this.search.placeholder = "Search datasets and sources";
    this.search.setAttribute("aria-label", "Search data library");
    this.search.addEventListener("input", () => this.renderInventory());
    searchWrap.append(this.search);

    this.inventory = el("div", "library-scroll");

    const actions = el("footer", "library-action-bar");
    const upload = this.actionButton("Upload", () => this.openUploadSheet());
    upload.classList.add("btn-primary");
    const connect = this.actionButton("Connect", () => void this.openConnectSheet());
    actions.append(upload, connect);

    this.fileInput = document.createElement("input");
    this.fileInput.type = "file";
    this.fileInput.multiple = true;
    this.fileInput.accept = ACCEPTED_FILES;
    this.fileInput.className = "file-input";
    this.fileInput.addEventListener("change", () => {
      if (this.fileInput.files?.length) void this.addFiles([...this.fileInput.files]);
      this.fileInput.value = "";
    });

    this.drawer.append(header, searchWrap, this.inventory, actions, this.fileInput);
    this.root.append(backdrop, this.drawer);
    this.root.addEventListener("keydown", (event) => this.handleKeydown(event));
    window.addEventListener("popstate", () => {
      if (this.isOpen()) this.finishClose();
    });
    document.body.append(this.root);
  }

  button(): HTMLButtonElement {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "btn";
    node.textContent = "Library";
    node.addEventListener("click", () => void this.open());
    return node;
  }

  sync(sources: SourceSummary[], datasets: DatasetSummary[]): void {
    this.sources = sources;
    this.datasets = datasets;
    if (!this.library.folders.length && !this.library.datasets.length && !this.library.sources.length) {
      this.library = this.legacyLibrary(sources, datasets);
    }
    this.renderInventory();
  }

  private async open(): Promise<void> {
    if (this.isOpen()) return;
    this.previousFocus = document.activeElement as HTMLElement | null;
    this.root.classList.add("is-open");
    this.root.setAttribute("aria-hidden", "false");
    document.body.classList.add("library-open");
    this.historyToken = Date.now();
    history.pushState({ ...history.state, thLibrary: this.historyToken }, "");
    this.drawer.focus();
    await this.refresh();
    requestAnimationFrame(() => this.search.focus());
  }

  private close(): void {
    if (!this.isOpen()) return;
    if (history.state?.thLibrary === this.historyToken) {
      history.back();
      return;
    }
    this.finishClose();
  }

  private finishClose(): void {
    this.root.classList.remove("is-open");
    this.root.setAttribute("aria-hidden", "true");
    document.body.classList.remove("library-open");
    this.closeSheet();
    this.previousFocus?.focus();
    this.previousFocus = null;
  }

  private isOpen(): boolean {
    return this.root.classList.contains("is-open");
  }

  private handleKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      event.preventDefault();
      if (this.sheet) this.closeSheet();
      else this.close();
      return;
    }
    if (event.key !== "Tab") return;
    const scope = this.sheet ?? this.drawer;
    const nodes = [...scope.querySelectorAll<HTMLElement>(FOCUSABLE)]
      .filter((node) => !node.hidden && node.getClientRects().length > 0);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (!scope.contains(document.activeElement)) {
      event.preventDefault();
      first.focus();
      return;
    }
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  private async refresh(
    selected?: { type: "source" | "dataset"; id: string },
  ): Promise<void> {
    try {
      const payload = await api.library();
      this.libraryAvailable = true;
      this.library = this.normaliseLibrary(payload);
      this.sources = this.library.sources.map((source) => ({
        ...source,
        supports_sql: false,
        display_name: source.kind,
      }));
      this.datasets = this.library.datasets.map((dataset) => ({
        id: dataset.id,
        name: dataset.name,
        description: dataset.description ?? "",
        columns: Array.from({ length: dataset.columns }, (_, index) => `Column ${index + 1}`),
        row_count: dataset.rows,
        created_at: 0,
      }));
      this.propagate(selected);
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) {
        this.setMessage(error, true);
        this.renderInventory();
        return;
      }
      this.libraryAvailable = false;
      try {
        const [sourcePayload, datasetPayload] = await Promise.all([
          api.get<{ sources: SourceSummary[] }>("/v1/sources"),
          api.get<{ datasets: DatasetSummary[] }>("/v1/datasets"),
        ]);
        this.sources = sourcePayload.sources ?? [];
        this.datasets = datasetPayload.datasets ?? [];
        this.library = this.legacyLibrary(this.sources, this.datasets);
        this.propagate(selected);
      } catch (legacyError) {
        this.setMessage(legacyError, true);
      }
    }
    this.renderInventory();
  }

  private normaliseLibrary(payload: LibraryResponse): LibraryResponse {
    const pending = this.pendingDeletes;
    const flattenFolders = (
      values: unknown,
      parentId: string | null = null,
    ): LibraryFolder[] => {
      if (!Array.isArray(values)) return [];
      const result: LibraryFolder[] = [];
      for (const raw of values) {
        if (!raw || typeof raw !== "object") continue;
        const value = raw as Record<string, unknown>;
        const id = String(value.id ?? "");
        if (!id) continue;
        result.push({
          id,
          name: String(value.name ?? "Untitled folder"),
          parent_id: value.parent_id == null ? parentId : String(value.parent_id),
        });
        result.push(...flattenFolders(value.children, id));
      }
      return result;
    };
    const raw = payload as unknown as Record<string, unknown>;
    const datasetValues = Array.isArray(raw.datasets) ? raw.datasets : [];
    const sourceValues = Array.isArray(raw.sources) ? raw.sources : [];
    return {
      folders: flattenFolders(raw.folders),
      datasets: datasetValues.map((item) => {
        const value = item as Record<string, unknown>;
        const columns = Array.isArray(value.columns)
          ? value.columns.length
          : Number(value.column_count ?? 0);
        const share = value.share as Record<string, unknown> | undefined;
        return {
          id: String(value.id ?? ""),
          name: String(value.name ?? "Untitled dataset"),
          folder_id: value.folder_id == null ? null : String(value.folder_id),
          rows: Number(value.row_count ?? 0),
          columns,
          shape: this.normaliseShape(value.shape_report),
          shared: Boolean(share?.shared),
          description: value.description == null ? "" : String(value.description),
        };
      }).filter((item) => item.id && !pending.has(`dataset:${item.id}`)),
      sources: sourceValues.map((item) => {
        const value = item as Record<string, unknown>;
        return {
          id: String(value.id ?? ""),
          name: String(value.name ?? "Untitled source"),
          kind: String(value.display_name ?? value.kind ?? "Source"),
          folder_id: value.folder_id == null ? null : String(value.folder_id),
          status: String(value.status ?? (value.last_error ? "Needs attention" : "Connected")),
        };
      }).filter((item) => item.id && !pending.has(`source:${item.id}`)),
    };
  }

  private normaliseShape(value: unknown): DatasetShape | null {
    if (!value || typeof value !== "object") return null;
    const shape = value as Record<string, unknown>;
    // The server indexes raw rows from zero; a person counts spreadsheet rows
    // from one, and the override sheet converts back before posting.
    const rawHeader = Number(shape.header_row ?? -1);
    const headerRow = rawHeader < 0 ? 0 : rawHeader + 1;
    const dropped = Array.isArray(shape.dropped_rows)
      ? shape.dropped_rows.filter((row) => Number(row) < rawHeader).length
      : Number(shape.preamble_rows_dropped ?? 0);
    const notes = Array.isArray(shape.notes)
      ? shape.notes.map(String)
      : String(shape.notes ?? "");
    const sheets = Array.isArray(shape.sheets)
      ? shape.sheets.map((sheet) => {
        if (typeof sheet === "string") return sheet;
        const raw = sheet as Record<string, unknown>;
        return {
          name: String(raw.name ?? raw.sheet ?? "Sheet"),
          rows: raw.rows == null ? undefined : Number(raw.rows),
          columns: raw.columns == null ? undefined : Number(raw.columns),
        };
      })
      : [];
    return {
      header_row: Number.isFinite(headerRow) ? headerRow : 1,
      dropped_rows: Number.isFinite(dropped) ? dropped : 0,
      notes,
      confidence: typeof shape.confidence === "string"
        ? shape.confidence
        : Number(shape.confidence ?? 0),
      sheets,
    };
  }

  private normalisePreview(
    payload: unknown,
    fallback?: DatasetPreview,
  ): DatasetPreview {
    const value = payload && typeof payload === "object"
      ? payload as Record<string, unknown>
      : {};
    const rawSchema = Array.isArray(value.schema) ? value.schema : [];
    let schema = rawSchema.map((column) => {
      const raw = column as Record<string, unknown>;
      return {
        name: String(raw.name ?? raw.label ?? "column"),
        type: String(raw.dtype ?? "unknown"),
      };
    });
    if (!schema.length && Array.isArray(value.columns)) {
      schema = value.columns.map((name) => ({
        name: String(name),
        type: fallback?.schema.find((column) => column.name === String(name))?.type ?? "unknown",
      }));
    }
    if (!schema.length) schema = fallback?.schema ?? [];
    const rawRows = Array.isArray(value.rows)
      ? value.rows
      : Array.isArray(value.preview)
        ? value.preview
        : [];
    const rows = rawRows.map((row) => {
      if (Array.isArray(row)) return row;
      if (row && typeof row === "object") {
        const record = row as Record<string, unknown>;
        return schema.map((column) => record[column.name]);
      }
      return [row];
    });
    return {
      schema,
      rows,
      shape: this.normaliseShape(value.shape_report) ?? fallback?.shape ?? null,
    };
  }

  private legacyLibrary(
    sources: SourceSummary[],
    datasets: DatasetSummary[],
  ): LibraryResponse {
    return {
      folders: [],
      datasets: datasets.map((dataset) => ({
        id: dataset.id,
        name: dataset.name,
        folder_id: null,
        rows: dataset.row_count,
        columns: dataset.columns.length,
        shape: null,
        shared: false,
        description: dataset.description,
      })),
      sources: sources.map((source) => ({
        id: source.id,
        name: source.name,
        kind: source.display_name ?? source.kind,
        folder_id: null,
        status: source.last_error ? "Needs attention" : "Connected",
      })),
    };
  }

  private propagate(selected?: { type: "source" | "dataset"; id: string }): void {
    this.options.onChange(this.sources, this.datasets, selected);
  }

  private renderInventory(): void {
    this.inventory.replaceChildren();
    if (this.messageText) {
      const message = el("p", `source-message${this.messageError ? " is-error" : ""}`);
      message.textContent = this.messageText;
      this.inventory.append(message);
    }

    const folders = el("section", "library-folders");
    const folderHead = el("div", "library-section-head");
    const folderTitle = el("h3");
    folderTitle.textContent = "Folders";
    const add = this.smallButton("New folder", () => this.openFolderEditor());
    add.disabled = !this.libraryAvailable;
    folderHead.append(folderTitle, add);
    const tree = el("ul", "folder-tree");
    tree.setAttribute("role", "tree");
    tree.append(this.folderTreeRow(null, "All data", true));
    for (const folder of this.library.folders.filter((item) => !item.parent_id)) {
      tree.append(this.folderTreeNode(folder, 0));
    }
    folders.append(folderHead, tree);

    const contents = el("section", "library-contents");
    const contentHead = el("div", "library-section-head");
    const title = el("h3");
    const selected = this.library.folders.find((folder) => folder.id === this.selectedFolder);
    title.textContent = selected?.name ?? "All data";
    const folderActions = el("div", "library-folder-actions");
    if (selected) {
      folderActions.append(
        this.smallButton("Rename", () => this.openFolderEditor(selected)),
        this.smallButton("Delete", () => void this.deleteFolder(selected)),
      );
    }
    contentHead.append(title, folderActions);
    contents.append(contentHead);

    const query = this.search.value.trim().toLocaleLowerCase();
    const items = this.visibleItems().filter((item) => {
      if (!query) return true;
      const text = item.value.name + ("kind" in item.value ? ` ${item.value.kind}` : "");
      return text.toLocaleLowerCase().includes(query);
    });
    if (!items.length) {
      const empty = el("p", "library-empty");
      empty.textContent = query ? "No matching data." : "Nothing in this folder yet.";
      contents.append(empty);
    } else {
      const list = el("ul", "library-item-list");
      for (const item of items) list.append(this.itemRow(item));
      contents.append(list);
    }
    this.inventory.append(folders, contents);
  }

  private visibleItems(): LibraryItem[] {
    const datasets = this.library.datasets
      .filter((item) => !this.selectedFolder || item.folder_id === this.selectedFolder)
      .map<LibraryItem>((value) => ({ type: "dataset", value }));
    const sources = this.library.sources
      .filter((item) => !this.selectedFolder || item.folder_id === this.selectedFolder)
      .map<LibraryItem>((value) => ({ type: "source", value }));
    return [...datasets, ...sources].sort((a, b) => a.value.name.localeCompare(b.value.name));
  }

  private folderTreeNode(folder: LibraryFolder, depth: number): HTMLLIElement {
    const item = el("li", "folder-tree-branch") as HTMLLIElement;
    item.setAttribute("role", "treeitem");
    item.setAttribute("aria-expanded", String(!this.collapsed.has(folder.id)));
    const row = this.folderTreeRow(folder.id, folder.name, false, depth);
    item.append(row);
    const children = this.library.folders.filter((candidate) => candidate.parent_id === folder.id);
    if (children.length && !this.collapsed.has(folder.id)) {
      const group = el("ul", "folder-tree");
      group.setAttribute("role", "group");
      for (const child of children) group.append(this.folderTreeNode(child, depth + 1));
      item.append(group);
    }
    return item;
  }

  private folderTreeRow(
    id: string | null,
    name: string,
    all: boolean,
    depth = 0,
  ): HTMLLIElement {
    const row = el("li", "folder-tree-row") as HTMLLIElement;
    row.style.setProperty("--folder-depth", String(depth));
    const hasChildren = id
      ? this.library.folders.some((candidate) => candidate.parent_id === id)
      : false;
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "folder-toggle";
    toggle.textContent = hasChildren ? (this.collapsed.has(id!) ? "+" : "−") : "";
    toggle.setAttribute("aria-label", `${this.collapsed.has(id ?? "") ? "Expand" : "Collapse"} ${name}`);
    toggle.disabled = !hasChildren;
    toggle.addEventListener("click", () => {
      if (!id) return;
      if (this.collapsed.has(id)) this.collapsed.delete(id);
      else this.collapsed.add(id);
      this.renderInventory();
    });
    const button = document.createElement("button");
    button.type = "button";
    button.className = "folder-name";
    if ((all && !this.selectedFolder) || id === this.selectedFolder) button.classList.add("is-active");
    button.textContent = name;
    button.addEventListener("click", () => {
      this.selectedFolder = id;
      this.renderInventory();
    });
    row.append(toggle, button);
    return row;
  }

  private itemRow(item: LibraryItem): HTMLLIElement {
    const row = el("li", `library-item library-${item.type}`) as HTMLLIElement;
    row.dataset.itemId = item.value.id;
    const copy = el("div", "library-item-copy");
    const name = document.createElement("button");
    name.type = "button";
    name.className = "library-item-name";
    name.textContent = item.value.name;
    name.title = "Rename";
    name.addEventListener("click", () => this.startRename(item, name));
    const meta = el("span", "source-meta");
    if (item.type === "dataset") {
      meta.textContent =
        `${item.value.rows.toLocaleString()} rows · ${item.value.columns.toLocaleString()} columns`;
    } else {
      meta.textContent = `${item.value.kind} · ${item.value.status}`;
    }
    if (!this.selectedFolder && item.value.folder_id) {
      const folder = this.library.folders.find((candidate) => candidate.id === item.value.folder_id);
      if (folder) meta.textContent += ` · ${folder.name}`;
    }
    copy.append(name, meta);

    const actions = el("div", "library-item-actions");
    if (item.type === "dataset") {
      actions.append(
        this.smallButton("Preview", () => void this.openPreview(item.value)),
        this.smallButton("Share", () => void this.openShare(item.value)),
      );
    }
    actions.append(
      this.smallButton("Move", () => this.openMove(item)),
      this.smallButton("Delete", () => this.scheduleDelete(item)),
    );
    row.append(copy, actions);
    return row;
  }

  private startRename(item: LibraryItem, button: HTMLButtonElement): void {
    const form = el("form", "inline-rename") as HTMLFormElement;
    const input = document.createElement("input");
    input.className = "control-input";
    input.value = item.value.name;
    input.setAttribute("aria-label", `Rename ${item.value.name}`);
    form.append(input);
    button.replaceWith(form);
    input.select();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const name = input.value.trim();
      if (!name || name === item.value.name) {
        this.renderInventory();
        return;
      }
      input.disabled = true;
      try {
        if (item.type === "dataset") await api.updateDataset(item.value.id, { name });
        else await api.updateSource(item.value.id, { name });
        await this.refresh();
      } catch (error) {
        this.setMessage(this.featureMessage(error, "Rename"), true);
        this.renderInventory();
      }
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this.renderInventory();
    });
    input.addEventListener("blur", () => {
      if (document.body.contains(form)) form.requestSubmit();
    });
  }

  private openFolderEditor(folder?: LibraryFolder): void {
    const body = this.openSheet(folder ? "Rename folder" : "New folder", "folder-sheet");
    const form = el("form", "sheet-form") as HTMLFormElement;
    const label = el("label", "source-field");
    const text = el("span", "control-label");
    text.textContent = "Folder name";
    const input = document.createElement("input");
    input.className = "control-input";
    input.required = true;
    input.value = folder?.name ?? "";
    label.append(text, input);
    const status = el("p", "source-status");
    const save = this.actionButton(folder ? "Save" : "Create folder", () => undefined);
    save.type = "submit";
    save.classList.add("btn-primary");
    form.append(label, status, save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      try {
        if (folder) await api.updateFolder(folder.id, { name: input.value.trim() });
        else await api.createFolder(input.value.trim(), this.selectedFolder);
        this.closeSheet();
        await this.refresh();
      } catch (error) {
        status.textContent = this.featureMessage(error, "Folders");
        status.classList.add("is-error");
        save.disabled = false;
      }
    });
    body.append(form);
    input.focus();
  }

  private async deleteFolder(folder: LibraryFolder): Promise<void> {
    if (!window.confirm(
      `Delete the folder “${folder.name}”? Only empty folders can be deleted.`,
    )) return;
    try {
      await api.deleteFolder(folder.id);
      this.selectedFolder = null;
      await this.refresh();
    } catch (error) {
      this.setMessage(this.featureMessage(error, "Folder deletion"), true);
      this.renderInventory();
    }
  }

  private openMove(item: LibraryItem): void {
    const body = this.openSheet(`Move ${item.value.name}`, "move-sheet");
    const list = el("div", "folder-choice-list");
    const choices: Array<{ id: string | null; name: string }> = [
      { id: null, name: "No folder" },
      ...this.library.folders.map((folder) => ({ id: folder.id, name: folder.name })),
    ];
    for (const choice of choices) {
      const button = this.actionButton(choice.name, async () => {
        button.disabled = true;
        try {
          if (item.type === "dataset") {
            await api.updateDataset(item.value.id, { folder_id: choice.id });
          } else {
            await api.updateSource(item.value.id, { folder_id: choice.id });
          }
          this.closeSheet();
          await this.refresh();
        } catch (error) {
          button.disabled = false;
          this.showSheetError(this.featureMessage(error, "Move"));
        }
      });
      button.classList.add("folder-choice");
      if (item.value.folder_id === choice.id) button.setAttribute("aria-current", "true");
      list.append(button);
    }
    body.append(list);
  }

  private scheduleDelete(item: LibraryItem): void {
    const key = `${item.type}:${item.value.id}`;
    if (this.pendingDeletes.has(key)) return;
    if (item.type === "dataset") {
      this.library.datasets = this.library.datasets.filter((value) => value.id !== item.value.id);
    } else {
      this.library.sources = this.library.sources.filter((value) => value.id !== item.value.id);
      this.sources = this.sources.filter((value) => value.id !== item.value.id);
      this.propagate();
    }
    this.renderInventory();
    const toast = el("div", "library-toast");
    const copy = el("span");
    copy.textContent = `${item.value.name} will be deleted.`;
    const undo = this.smallButton("Undo", () => {
      const timer = this.pendingDeletes.get(key);
      if (timer) window.clearTimeout(timer);
      this.pendingDeletes.delete(key);
      toast.remove();
      if (item.type === "dataset") this.library.datasets.push(item.value);
      else {
        this.library.sources.push(item.value);
        this.sources.push({
          ...item.value,
          display_name: item.value.kind,
          supports_sql: false,
        });
        this.propagate();
      }
      this.renderInventory();
    });
    toast.append(copy, undo);
    this.drawer.append(toast);
    const timer = window.setTimeout(async () => {
      this.pendingDeletes.delete(key);
      toast.remove();
      try {
        await api.del(`/v1/${item.type === "dataset" ? "datasets" : "sources"}/${encodeURIComponent(item.value.id)}`);
      } catch (error) {
        if (item.type === "dataset") this.library.datasets.push(item.value);
        else {
          this.library.sources.push(item.value);
          this.sources.push({
            ...item.value,
            display_name: item.value.kind,
            supports_sql: false,
          });
          this.propagate();
        }
        this.setMessage(error, true);
        this.renderInventory();
      }
    }, 6000);
    this.pendingDeletes.set(key, timer);
  }

  private openUploadSheet(): void {
    this.openSheet("Upload datasets", "upload-sheet");
    this.uploadSheetOpen = true;
    this.renderUploads();
  }

  private renderUploads(): void {
    if (!this.uploadSheetOpen || !this.sheetBody) return;
    this.sheetBody.replaceChildren();
    const zone = el("div", "file-drop");
    zone.tabIndex = 0;
    zone.setAttribute("role", "button");
    zone.setAttribute("aria-label", "Choose data files to upload");
    const title = el("strong");
    title.textContent = "Drop files here or tap to choose";
    const detail = el("span");
    detail.textContent = "CSV, TSV, Excel, ODS, Parquet, or JSON";
    zone.append(title, detail);
    zone.addEventListener("click", () => this.fileInput.click());
    zone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        this.fileInput.click();
      }
    });
    for (const type of ["dragenter", "dragover"]) {
      zone.addEventListener(type, (event) => {
        event.preventDefault();
        zone.classList.add("is-over");
      });
    }
    for (const type of ["dragleave", "drop"]) {
      zone.addEventListener(type, (event) => {
        event.preventDefault();
        zone.classList.remove("is-over");
      });
    }
    zone.addEventListener("drop", (event) => {
      if (event.dataTransfer?.files.length) void this.addFiles([...event.dataTransfer.files]);
    });
    this.sheetBody.append(zone);

    if (!this.uploads.length) return;
    const list = el("ul", "upload-list");
    list.setAttribute("aria-live", "polite");
    for (const item of this.uploads) {
      const row = el("li", `upload-item is-${item.state}`);
      const copy = el("div", "upload-copy");
      const name = el("strong", "source-name");
      name.textContent = item.file.name;
      const status = el("span", "source-meta");
      if (item.state === "queued") status.textContent = "Waiting";
      if (item.state === "uploading") status.textContent = `Uploading · ${item.progress}%`;
      if (item.state === "error") status.textContent = item.error ?? "Upload failed";
      if (item.state === "success") status.textContent = this.shapeSentence(item);
      copy.append(name, status);
      row.append(copy);
      if (item.state === "uploading") {
        const progress = document.createElement("progress");
        progress.max = 100;
        progress.value = item.progress;
        progress.setAttribute("aria-label", `Upload progress for ${item.file.name}`);
        row.append(progress);
      }
      if (item.state === "success" && item.result) {
        const actions = el("div", "upload-result-actions");
        actions.append(
          this.smallButton("Not right?", () => void this.openOverrideForUpload(item)),
          this.smallButton("Preview", () => {
            if (item.dataset) void this.openPreview(item.dataset, item.preview);
          }),
        );
        row.append(actions);
      }
      list.append(row);
    }
    this.sheetBody.append(list);
  }

  private async addFiles(files: File[]): Promise<void> {
    const additions = files.map<UploadItem>((file) => ({
      file,
      progress: 0,
      state: "queued",
    }));
    this.uploads.push(...additions);
    this.renderUploads();
    await Promise.all(additions.map((item) => this.uploadOne(item)));
  }

  private async uploadOne(item: UploadItem): Promise<void> {
    item.state = "uploading";
    this.renderUploads();
    try {
      item.result = await api.upload<UploadResult>(item.file, (progress) => {
        item.progress = progress;
        this.renderUploads();
      });
      item.state = "success";
      item.progress = 100;
      await this.refresh({ type: "dataset", id: item.result.dataset_id });
      item.dataset = this.library.datasets.find((dataset) => dataset.id === item.result?.dataset_id);
      if (!item.dataset) {
        item.dataset = {
          id: item.result.dataset_id,
          name: item.result.name,
          folder_id: null,
          rows: item.result.rows,
          columns: Array.isArray(item.result.columns)
            ? item.result.columns.length
            : item.result.columns,
          shape: item.result.shape ?? null,
          shared: false,
        };
      }
      try {
        item.preview = this.normalisePreview(
          await api.previewDataset(item.result.dataset_id),
        );
        if (item.preview.shape) item.dataset.shape = item.preview.shape;
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 404) item.error = String(error);
      }
    } catch (error) {
      item.state = "error";
      item.error = error instanceof Error ? error.message : String(error);
    }
    this.renderUploads();
  }

  private shapeSentence(item: UploadItem): string {
    const shape = item.preview?.shape ?? item.dataset?.shape ?? item.result?.shape;
    const rows = item.dataset?.rows ?? item.result?.rows ?? 0;
    const columns = item.dataset?.columns ??
      (Array.isArray(item.result?.columns) ? item.result.columns.length : item.result?.columns) ?? 0;
    if (!shape) return `${columns.toLocaleString()} columns, ${rows.toLocaleString()} rows.`;
    const header = Number.isFinite(shape.header_row)
      ? shape.header_row
      : (shape.dropped_rows ?? 0) + 1;
    if (header <= 0) {
      return `Used the file’s column names, ${columns.toLocaleString()} columns, ` +
        `${rows.toLocaleString()} rows.`;
    }
    return `Read row ${header} as the header, dropped ${shape.dropped_rows ?? 0} preamble rows, ` +
      `${columns.toLocaleString()} columns, ${rows.toLocaleString()} rows.`;
  }

  private async openOverrideForUpload(item: UploadItem): Promise<void> {
    if (!item.dataset) return;
    let preview = item.preview;
    if (!preview) {
      try {
        preview = this.normalisePreview(
          await api.previewDataset(item.dataset.id),
        );
      } catch (error) {
        this.showSheetError(this.featureMessage(error, "Import overrides"));
        return;
      }
    }
    this.openOverride(item.dataset, preview, item);
  }

  private openOverride(
    dataset: LibraryDataset,
    preview: DatasetPreview,
    uploadItem?: UploadItem,
  ): void {
    this.uploadSheetOpen = false;
    const body = this.openSheet("Fix import settings", "override-sheet");
    const form = el("form", "sheet-form override-form") as HTMLFormElement;
    const intro = el("p", "sources-lede");
    intro.textContent = "Change how the original file is read, then rebuild the preview.";
    const header = this.numberField("Header row", preview.shape?.header_row ?? 1, 0);
    const skip = this.numberField("Skip rows", preview.shape?.dropped_rows ?? 0, 0);
    form.append(intro, header.label, skip.label);

    const sheets = (preview.shape?.sheets ?? dataset.shape?.sheets ?? [])
      .map((sheet) => typeof sheet === "string" ? sheet : sheet.name);
    let sheetSelect: HTMLSelectElement | null = null;
    if (sheets.length > 1) {
      const label = el("label", "source-field");
      const text = el("span", "control-label");
      text.textContent = "Worksheet";
      sheetSelect = document.createElement("select");
      sheetSelect.className = "control-input";
      for (const name of sheets) {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        sheetSelect.append(option);
      }
      label.append(text, sheetSelect);
      form.append(label);
    }

    if (preview.schema.length) {
      const typesTitle = el("h3", "sheet-section-title");
      typesTitle.textContent = "Column types";
      const types = el("div", "type-grid");
      for (const column of preview.schema) {
        const label = el("label", "source-field");
        const text = el("span", "control-label");
        text.textContent = column.name;
        const select = document.createElement("select");
        select.className = "control-input";
        select.dataset.column = column.name;
        for (const value of ["auto", "string", "number", "integer", "percent", "boolean", "date"]) {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = value === "auto" ? `Auto (${column.type})` : value;
          select.append(option);
        }
        label.append(text, select);
        types.append(label);
      }
      form.append(typesTitle, types);
    }
    const status = el("p", "source-status");
    const actions = el("div", "sheet-actions");
    if (uploadItem) {
      actions.append(this.actionButton("Back", () => this.openUploadSheet()));
    }
    const apply = this.actionButton("Apply and preview", () => undefined);
    apply.type = "submit";
    apply.classList.add("btn-primary");
    actions.append(apply);
    form.append(status, actions);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      apply.disabled = true;
      status.textContent = "Rebuilding preview…";
      const types: Record<string, string> = {};
      for (const select of form.querySelectorAll<HTMLSelectElement>("[data-column]")) {
        if (select.value !== "auto") types[select.dataset.column!] = select.value;
      }
      try {
        const result = this.normalisePreview(
          await api.restructureDataset(dataset.id, {
            header_row: Number(header.input.value) - 1,
            skip_rows: Number(skip.input.value),
            sheet: sheetSelect?.value ?? null,
            renames: {},
            types,
          }),
          preview,
        );
        if (uploadItem) {
          uploadItem.preview = result;
          if (result.shape) uploadItem.dataset!.shape = result.shape;
        }
        await this.refresh();
        void this.openPreview(dataset, result);
      } catch (error) {
        status.textContent = this.featureMessage(error, "Import overrides");
        status.classList.add("is-error");
        apply.disabled = false;
      }
    });
    body.append(form);
  }

  private async openPreview(
    dataset: LibraryDataset,
    supplied?: DatasetPreview,
  ): Promise<void> {
    this.uploadSheetOpen = false;
    const body = this.openSheet(dataset.name, "preview-sheet");
    const status = el("p", "source-status");
    status.textContent = "Loading preview…";
    body.append(status);
    let preview = supplied;
    if (!preview) {
      try {
        preview = this.normalisePreview(
          await api.previewDataset(dataset.id),
        );
      } catch (error) {
        status.textContent = this.featureMessage(error, "Preview");
        status.classList.add("is-error");
        return;
      }
    }
    body.replaceChildren(this.previewTable(preview));
    const footer = el("div", "sheet-actions");
    footer.append(this.actionButton("Not right?", () => this.openOverride(dataset, preview!)));
    body.append(footer);
  }

  private previewTable(preview: DatasetPreview): HTMLElement {
    const wrap = el("div", "dataset-preview-scroll");
    wrap.tabIndex = 0;
    const table = document.createElement("table");
    table.className = "dataset-preview";
    const head = document.createElement("thead");
    const names = document.createElement("tr");
    for (const column of preview.schema) {
      const cell = document.createElement("th");
      const name = el("span");
      name.textContent = column.name;
      const type = el("small");
      type.textContent = column.type;
      cell.append(name, type);
      names.append(cell);
    }
    head.append(names);
    const body = document.createElement("tbody");
    for (const values of preview.rows) {
      const row = document.createElement("tr");
      for (const value of values) {
        const cell = document.createElement("td");
        cell.textContent = value == null ? "—" : String(value);
        row.append(cell);
      }
      body.append(row);
    }
    table.append(head, body);
    wrap.append(table);
    return wrap;
  }

  private async openShare(dataset: LibraryDataset): Promise<void> {
    const body = this.openSheet(`Share ${dataset.name}`, "share-sheet");
    const status = el("p", "source-status");
    status.textContent = "Creating link…";
    body.append(status);
    try {
      const result = await api.shareDataset(dataset.id);
      const label = el("label", "source-field");
      const text = el("span", "control-label");
      text.textContent = "Anyone with this link can view";
      const row = el("div", "share-link-row");
      const input = document.createElement("input");
      input.className = "control-input";
      input.readOnly = true;
      input.value = result.url;
      const copy = this.actionButton("Copy", async () => {
        try {
          await navigator.clipboard.writeText(result.url);
        } catch {
          input.select();
          document.execCommand("copy");
        }
        status.textContent = "Link copied.";
        status.classList.add("is-ok");
      });
      row.append(input, copy);
      label.append(text, row);
      const stop = this.smallButton("Stop sharing", async () => {
        stop.disabled = true;
        try {
          await api.unshareDataset(dataset.id);
          this.closeSheet();
          await this.refresh();
        } catch (error) {
          status.textContent = this.featureMessage(error, "Sharing");
          status.classList.add("is-error");
          stop.disabled = false;
        }
      });
      body.replaceChildren(label, status, stop);
      input.select();
    } catch (error) {
      status.textContent = this.featureMessage(error, "Sharing");
      status.classList.add("is-error");
    }
  }

  private async openConnectSheet(): Promise<void> {
    this.uploadSheetOpen = false;
    this.connectStep = 0;
    this.connectKind = null;
    this.connectName = "";
    this.connectValues = {};
    this.connectStatus = "";
    this.connectStatusState = "idle";
    this.connectBusy = false;
    this.openSheet("Connect a source", "connect-sheet");
    this.renderConnectSheet();
    if (this.kinds.length) return;
    this.connectBusy = true;
    this.renderConnectSheet();
    try {
      const payload = await api.get<{ kinds: ConnectorKind[] }>("/v1/connectors/kinds");
      this.kinds = payload.kinds ?? [];
    } catch (error) {
      this.connectStatus = error instanceof Error ? error.message : String(error);
      this.connectStatusState = "error";
    } finally {
      this.connectBusy = false;
      this.renderConnectSheet();
    }
  }

  private renderConnectSheet(): void {
    if (!this.sheetBody || !this.sheet?.classList.contains("connect-sheet")) return;
    this.sheetBody.replaceChildren();
    const total = (this.connectKind?.fields.length ?? 0) + 3;
    const progress = el("p", "connect-progress");
    progress.textContent = this.connectStep === 0
      ? "Choose a connection type"
      : `Step ${this.connectStep + 1} of ${total}`;
    this.sheetBody.append(progress);

    if (this.connectStep === 0) {
      if (this.connectBusy) {
        const loading = el("p", "source-status");
        loading.textContent = "Loading connection types…";
        this.sheetBody.append(loading);
      } else {
        const kinds = el("div", "kind-list");
        for (const kind of this.kinds) {
          const button = this.actionButton(kind.display_name, () => {
            this.connectKind = kind;
            this.connectName = kind.display_name;
            this.connectStep = 1;
            this.connectStatus = "";
            this.renderConnectSheet();
            this.sheetBody?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
          });
          button.classList.add("kind-row");
          const detail = el("span");
          detail.textContent = kind.supports_sql ? kind.dialect || "SQL database" : "Connected source";
          button.append(detail);
          kinds.append(button);
        }
        this.sheetBody.append(kinds);
      }
    } else if (this.connectKind) {
      const lastStep = this.connectKind.fields.length + 2;
      if (this.connectStep === 1) {
        this.sheetBody.append(this.connectTextField(
          "Connection name",
          "A name you will recognise in the library",
          this.connectName,
          (value) => { this.connectName = value; },
        ));
      } else if (this.connectStep <= this.connectKind.fields.length + 1) {
        const field = this.connectKind.fields[this.connectStep - 2];
        this.sheetBody.append(this.connectTextField(
          field.label,
          `Enter ${field.label.toLocaleLowerCase()}`,
          this.connectValues[field.name] ?? "",
          (value) => { this.connectValues[field.name] = value; },
          field.type,
        ));
      } else if (this.connectStep === lastStep) {
        const review = el("div", "connect-review");
        const title = el("strong");
        title.textContent = this.connectName;
        const detail = el("span", "source-meta");
        detail.textContent = this.connectKind.display_name;
        review.append(title, detail);
        const test = this.actionButton("Test connection", () => void this.testConnection());
        const save = this.actionButton("Save source", () => void this.saveConnection());
        save.classList.add("btn-primary");
        test.disabled = this.connectBusy;
        save.disabled = this.connectBusy;
        review.append(test, save);
        this.sheetBody.append(review);
      }
      const nav = el("div", "connect-nav");
      nav.append(this.actionButton("Back", () => {
        this.connectStep -= 1;
        this.connectStatus = "";
        this.renderConnectSheet();
      }));
      if (this.connectStep < lastStep) {
        const next = this.actionButton("Continue", () => {
          this.connectStep += 1;
          this.renderConnectSheet();
        });
        next.classList.add("btn-primary");
        nav.append(next);
      }
      this.sheetBody.append(nav);
    }
    if (this.connectStatus) {
      const status = el("p", `source-status is-${this.connectStatusState}`);
      status.setAttribute("role", "status");
      status.textContent = this.connectStatus;
      this.sheetBody.append(status);
    }
  }

  private connectTextField(
    labelText: string,
    hint: string,
    value: string,
    change: (value: string) => void,
    type: ConnectorField["type"] = "text",
  ): HTMLElement {
    const label = el("label", "connect-step-field");
    const title = el("span", "connect-step-title");
    title.textContent = labelText;
    const detail = el("span", "sources-lede");
    detail.textContent = hint;
    const input = document.createElement("input");
    input.className = "control-input";
    input.type = type;
    input.value = value;
    input.autocomplete = type === "password" ? "new-password" : "off";
    input.addEventListener("input", () => change(input.value));
    label.append(title, detail, input);
    return label;
  }

  private connectionConfig(): Record<string, string | number> {
    const config: Record<string, string | number> = {};
    for (const field of this.connectKind?.fields ?? []) {
      const value = this.connectValues[field.name];
      if (!value) continue;
      config[field.name] = field.type === "number" ? Number(value) : value;
    }
    return config;
  }

  private async testConnection(): Promise<void> {
    if (!this.connectKind) return;
    this.connectBusy = true;
    this.connectStatus = "Testing connection…";
    this.connectStatusState = "idle";
    this.renderConnectSheet();
    try {
      const result = await api.post<ConnectionTest>("/v1/sources/test", {
        kind: this.connectKind.kind,
        config: this.connectionConfig(),
      });
      this.connectStatus = result.ok
        ? `Connection succeeded in ${result.duration_ms.toLocaleString()} ms.`
        : result.error || "Connection failed.";
      this.connectStatusState = result.ok ? "ok" : "error";
    } catch (error) {
      this.connectStatus = error instanceof Error ? error.message : String(error);
      this.connectStatusState = "error";
    } finally {
      this.connectBusy = false;
      this.renderConnectSheet();
    }
  }

  private async saveConnection(): Promise<void> {
    if (!this.connectKind) return;
    this.connectBusy = true;
    this.connectStatus = "Saving source…";
    this.connectStatusState = "idle";
    this.renderConnectSheet();
    try {
      const created = await api.post<{ id: string }>("/v1/sources", {
        kind: this.connectKind.kind,
        name: this.connectName.trim(),
        config: this.connectionConfig(),
      });
      this.closeSheet();
      await this.refresh({ type: "source", id: created.id });
    } catch (error) {
      this.connectStatus = error instanceof Error ? error.message : String(error);
      this.connectStatusState = "error";
      this.connectBusy = false;
      this.renderConnectSheet();
    }
  }

  private openSheet(titleText: string, className: string): HTMLElement {
    this.closeSheet();
    this.sheet = el("section", `library-sheet ${className}`);
    this.sheet.setAttribute("role", "dialog");
    this.sheet.setAttribute("aria-modal", "true");
    this.sheet.setAttribute("aria-label", titleText);
    const header = el("header", "library-sheet-header");
    const title = el("h2");
    title.textContent = titleText;
    header.append(title, this.closeButton(() => this.closeSheet()));
    this.sheetBody = el("div", "library-sheet-body");
    this.sheet.append(header, this.sheetBody);
    this.drawer.append(this.sheet);
    requestAnimationFrame(() => this.sheet?.querySelector<HTMLElement>(FOCUSABLE)?.focus());
    return this.sheetBody;
  }

  private closeSheet(): void {
    this.sheet?.remove();
    this.sheet = null;
    this.sheetBody = null;
    this.uploadSheetOpen = false;
    if (this.isOpen()) requestAnimationFrame(() => this.search.focus());
  }

  private showSheetError(message: string): void {
    if (!this.sheetBody) return;
    const status = el("p", "source-status is-error");
    status.textContent = message;
    this.sheetBody.prepend(status);
  }

  private numberField(
    labelText: string,
    value: number,
    min: number,
  ): { label: HTMLElement; input: HTMLInputElement } {
    const label = el("label", "source-field");
    const text = el("span", "control-label");
    text.textContent = labelText;
    const input = document.createElement("input");
    input.type = "number";
    input.min = String(min);
    input.required = true;
    input.value = String(value);
    input.className = "control-input";
    label.append(text, input);
    return { label, input };
  }

  private setMessage(value: unknown, error = false): void {
    this.messageText = value instanceof Error ? value.message : String(value);
    this.messageError = error;
  }

  private featureMessage(value: unknown, feature: string): string {
    if (value instanceof ApiError && value.status === 404) {
      return `${feature} is not available on this server yet. Your library is unchanged.`;
    }
    return value instanceof Error ? value.message : String(value);
  }

  private actionButton(
    text: string,
    action: () => void | Promise<void>,
  ): HTMLButtonElement {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "btn";
    node.textContent = text;
    node.addEventListener("click", () => void action());
    return node;
  }

  private smallButton(text: string, action: () => void): HTMLButtonElement {
    const node = this.actionButton(text, action);
    node.classList.add("btn-small");
    return node;
  }

  private closeButton(action: () => void): HTMLButtonElement {
    const node = this.actionButton("Close", action);
    node.classList.add("library-close");
    node.setAttribute("aria-label", "Close");
    return node;
  }
}
