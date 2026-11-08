/** The coding agent: describe a page, get a sandboxed single-file artifact, keep editing it. */

import { ApiError, api } from "./api";
import { button, el } from "./chart";

interface ArtifactSummary {
  id: string;
  title: string;
  model: string | null;
  version: number;
  updated_at: number;
  view_url: string;
}
interface AgentModel { alias: string; model: string; default: boolean; }
interface AgentResponse { artifact: ArtifactSummary; reply: string; model: string; notes: string[]; }

/**
 * `sandbox="allow-scripts"` with no `allow-same-origin` gives the frame an
 * opaque origin, so artifact code cannot read this app's cookies or call its
 * API as the user. The server sends the same restriction as a CSP header, so
 * it holds even if someone opens the view URL directly.
 */
const FRAME_SANDBOX = "allow-scripts";

export class CodeView {
  readonly root = el("section", "code-view");
  private artifacts: ArtifactSummary[] = [];
  private models: AgentModel[] = [];
  private activeId: string | null = null;
  private busy = false;
  private status = el("p", "code-status");
  private list = el("ul", "code-list");
  private stage = el("div", "code-stage");
  private prompt = document.createElement("textarea");
  private picker = document.createElement("select");

  constructor() {
    this.root.setAttribute("aria-label", "Coding agent");
    this.prompt.className = "code-prompt";
    this.prompt.rows = 3;
    this.prompt.maxLength = 12000;
    this.prompt.placeholder = "Describe a page to build, or what to change in the open one";
    this.prompt.setAttribute("aria-label", "Request");
    this.prompt.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) void this.send();
    });
    this.picker.className = "code-model";
    this.picker.setAttribute("aria-label", "Model");

    const bar = el("div", "code-bar");
    const send = document.createElement("button");
    send.type = "button";
    send.className = "btn btn-primary";
    send.textContent = "Build";
    send.addEventListener("click", () => void this.send());
    bar.append(this.picker, send, button("New", () => this.open(null)));
    this.status.setAttribute("role", "status");
    this.status.setAttribute("aria-live", "polite");

    const side = el("aside", "code-side");
    side.append(this.list);
    const main = el("div", "code-main");
    main.append(this.prompt, bar, this.status, this.stage);
    this.root.append(side, main);
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      const [models, list] = await Promise.all([
        api.get<{ default: string; models: AgentModel[] }>("/v1/code-agent/models"),
        api.get<{ artifacts: ArtifactSummary[] }>("/v1/artifacts"),
      ]);
      this.models = models.models;
      this.picker.replaceChildren(
        ...this.models.map((m) => {
          const option = document.createElement("option");
          option.value = m.alias;
          option.textContent = m.default ? `${m.alias} (default)` : m.alias;
          option.selected = m.default;
          return option;
        }),
      );
      this.artifacts = list.artifacts;
      this.renderList();
      this.renderStage();
    } catch (error) {
      this.say(this.describe(error));
    }
  }

  private say(message: string): void {
    this.status.textContent = message;
  }

  private describe(error: unknown): string {
    if (error instanceof ApiError) return error.message || "Request failed";
    return error instanceof Error ? error.message : "Request failed";
  }

  private renderList(): void {
    this.list.replaceChildren(
      ...this.artifacts.map((artifact) => {
        const item = el("li", "code-item");
        const open = document.createElement("button");
        open.type = "button";
        open.className = "code-open";
        open.textContent = artifact.title;
        open.title = `v${artifact.version}${artifact.model ? ` - ${artifact.model}` : ""}`;
        if (artifact.id === this.activeId) open.setAttribute("aria-current", "true");
        open.addEventListener("click", () => this.open(artifact.id));
        item.append(open, button("Delete", () => void this.remove(artifact.id)));
        return item;
      }),
    );
  }

  private renderStage(): void {
    const active = this.artifacts.find((a) => a.id === this.activeId);
    if (!active) {
      const empty = el("p", "code-empty");
      empty.textContent = "Nothing open. Describe what to build above.";
      this.stage.replaceChildren(empty);
      return;
    }
    const frame = document.createElement("iframe");
    frame.className = "code-frame";
    frame.title = active.title;
    frame.setAttribute("sandbox", FRAME_SANDBOX);
    frame.setAttribute("referrerpolicy", "no-referrer");
    // The version in the query string defeats the iframe cache after an edit.
    frame.src = `${active.view_url}?v=${active.version}`;
    const open = document.createElement("a");
    open.href = frame.src;
    open.target = "_blank";
    open.rel = "noopener noreferrer";
    open.textContent = "Open in a new tab";
    this.stage.replaceChildren(frame, open);
  }

  private open(id: string | null): void {
    this.activeId = id;
    this.renderList();
    this.renderStage();
  }

  private async send(): Promise<void> {
    const text = this.prompt.value.trim();
    if (!text || this.busy) return;
    this.busy = true;
    this.say(this.activeId ? "Updating the open page" : "Building");
    try {
      const response = await api.post<AgentResponse>("/v1/code-agent", {
        prompt: text,
        model: this.picker.value,
        artifact_id: this.activeId,
      });
      const next = response.artifact;
      this.artifacts = [next, ...this.artifacts.filter((a) => a.id !== next.id)];
      this.activeId = next.id;
      this.prompt.value = "";
      this.renderList();
      this.renderStage();
      const notes = response.notes.length ? ` (${response.notes.length} automatic repair)` : "";
      this.say(`${response.reply || "Done."}${notes}`);
    } catch (error) {
      this.say(this.describe(error));
    } finally {
      this.busy = false;
    }
  }

  private async remove(id: string): Promise<void> {
    try {
      await api.del(`/v1/artifacts/${id}`);
      this.artifacts = this.artifacts.filter((a) => a.id !== id);
      if (this.activeId === id) this.activeId = null;
      this.renderList();
      this.renderStage();
    } catch (error) {
      this.say(this.describe(error));
    }
  }
}
