/**
 * The dashboard list.
 *
 * Dashboards existed before this file did - the grid, the tiles, the sharing -
 * but the only way to reach one was a test hook, which is the same as the
 * feature not shipping. This is the index: what you have, and one button that
 * makes another.
 */

import { api } from "./api";
import { button, el } from "./chart";

export interface DashboardSummary {
  id: string;
  title: string;
  is_public?: number;
  share_token?: string | null;
  created_at?: number;
  updated_at?: number;
}

export interface DashboardListOptions {
  onOpen: (id: string) => void;
}

export class DashboardListView {
  readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private readonly status: HTMLElement;
  private items: DashboardSummary[] = [];

  constructor(private readonly options: DashboardListOptions) {
    this.root = el("section", "dash-list");

    const bar = el("header", "dash-bar");
    const heading = el("h1", "dash-list-title");
    heading.textContent = "Dashboards";
    const create = button("New dashboard", () => void this.create());
    create.classList.add("btn-primary");
    bar.append(heading, create);

    this.status = el("div", "dash-status");
    this.status.hidden = true;
    this.list = el("ul", "dash-list-items");
    this.root.append(bar, this.status, this.list);
  }

  async load(): Promise<void> {
    this.setStatus("Loading…");
    try {
      const payload = await api.get<{ dashboards: DashboardSummary[] }>("/v1/dashboards");
      this.items = payload.dashboards ?? [];
      this.setStatus("");
      this.render();
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  private render(): void {
    this.list.replaceChildren();

    if (!this.items.length) {
      const empty = el("li", "dash-empty");
      empty.textContent =
        "No dashboards yet. Make one, then add panes by asking for them.";
      this.list.append(empty);
      return;
    }

    for (const item of this.items) {
      const row = el("li", "dash-list-item");

      const open = el("button", "dash-list-open") as HTMLButtonElement;
      open.type = "button";
      const name = el("span", "dash-list-name");
      name.textContent = item.title || "Untitled dashboard";
      const meta = el("span", "dash-list-meta");
      meta.textContent = [
        item.updated_at ? `edited ${relative(item.updated_at)}` : "",
        item.is_public ? "shared" : "",
      ]
        .filter(Boolean)
        .join(" · ");
      open.append(name, meta);
      open.addEventListener("click", () => this.options.onOpen(item.id));

      const remove = button("Delete", () => void this.remove(item));
      remove.classList.add("btn-small");

      row.append(open, remove);
      this.list.append(row);
    }
  }

  private async create(): Promise<void> {
    this.setStatus("Creating…");
    try {
      const created = await api.post<{ id: string }>("/v1/dashboards", {
        title: "Untitled dashboard",
      });
      this.setStatus("");
      // Straight into it. A new empty dashboard sitting in a list is a chore;
      // the reason anyone pressed the button is the next thing they want to do.
      this.options.onOpen(created.id);
    } catch (error) {
      this.setStatus((error as Error).message, true);
    }
  }

  private async remove(item: DashboardSummary): Promise<void> {
    const label = item.title || "this dashboard";
    if (!window.confirm(`Delete ${label}? The charts on it are kept.`)) return;
    try {
      await api.del(`/v1/dashboards/${item.id}`);
      this.items = this.items.filter((row) => row.id !== item.id);
      this.render();
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

function relative(seconds: number): string {
  const delta = Date.now() / 1000 - seconds;
  if (delta < 90) return "just now";
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}
