import { api } from "./api";
import { el } from "./chart";

export interface PublishLink {
  kind: "chart" | "dashboard";
  id: string;
  title: string;
  caption: string;
  public: boolean;
}

export interface PublishState {
  dataset_id: string;
  published: boolean;
  slug: string | null;
  title: string;
  description: string;
  tags: string[];
  license: string;
  author_name: string;
  allow_download: boolean;
  cover_chart_id: string | null;
  url: string | null;
  links: PublishLink[];
  licenses: Record<string, string>;
}

interface Available {
  charts: { id: string; title: string; is_public: number }[];
  dashboards: { id: string; title: string; is_public: number }[];
}

interface LinkResult {
  notice: string;
  links: PublishLink[];
}

const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\s][^*\n]*\*|\[[^\]\n]+\]\(https?:\/\/[^\s()]+\))/g;

function inline(parent: HTMLElement, text: string): void {
  for (const part of text.split(INLINE)) {
    if (!part) continue;
    let node: HTMLElement | null = null;
    if (part.startsWith("`") && part.length > 2) {
      node = el("code");
      node.textContent = part.slice(1, -1);
    } else if (part.startsWith("**") && part.length > 4) {
      node = el("strong");
      node.textContent = part.slice(2, -2);
    } else if (part.startsWith("*") && part.length > 2) {
      node = el("em");
      node.textContent = part.slice(1, -1);
    } else {
      const link = /^\[([^\]]+)\]\((https?:\/\/[^\s()]+)\)$/.exec(part);
      if (link) {
        const anchor = el("a") as HTMLAnchorElement;
        anchor.textContent = link[1];
        anchor.href = link[2];
        anchor.rel = "nofollow ugc noopener";
        anchor.target = "_blank";
        node = anchor;
      }
    }
    if (node) parent.append(node);
    else parent.append(document.createTextNode(part));
  }
}

export function renderPreview(target: HTMLElement, source: string): void {
  target.replaceChildren();
  let list: HTMLElement | null = null;
  let code: string[] | null = null;
  let paragraph: string[] = [];
  const flushParagraph = () => {
    if (!paragraph.length) return;
    const p = el("p");
    inline(p, paragraph.join(" "));
    target.append(p);
    paragraph = [];
  };
  for (const line of source.split("\n")) {
    if (code) {
      if (line.trim().startsWith("```")) {
        const pre = el("pre");
        const inner = el("code");
        inner.textContent = code.join("\n");
        pre.append(inner);
        target.append(pre);
        code = null;
      } else code.push(line);
      continue;
    }
    if (line.trim().startsWith("```")) {
      flushParagraph();
      list = null;
      code = [];
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      list = null;
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph();
      list = null;
      const h = el(`h${heading[1].length + 1}`);
      inline(h, heading[2]);
      target.append(h);
      continue;
    }
    const item = /^\s{0,3}(?:([-*+])|\d{1,3}[.)])\s+(.*)$/.exec(line);
    if (item) {
      flushParagraph();
      const tag = item[1] ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = el(tag);
        target.append(list);
      }
      const li = el("li");
      inline(li, item[2]);
      list.append(li);
      continue;
    }
    list = null;
    paragraph.push(line.trim());
  }
  flushParagraph();
}

function field(label: string, control: HTMLElement): HTMLElement {
  const wrap = el("label", "source-field");
  const text = el("span", "control-label");
  text.textContent = label;
  wrap.append(text, control);
  return wrap;
}

function input(value: string, max: number): HTMLInputElement {
  const node = document.createElement("input");
  node.className = "control-input";
  node.value = value;
  node.maxLength = max;
  return node;
}

function slugify(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
}

export async function openPublishPanel(
  body: HTMLElement,
  datasetId: string,
  datasetName: string,
  onChange: () => void,
): Promise<void> {
  const status = el("p", "source-status");
  status.textContent = "Loading…";
  body.replaceChildren(status);
  const base = `/v1/datasets/${encodeURIComponent(datasetId)}`;
  const fail = (error: unknown) => {
    status.textContent = error instanceof Error ? error.message : String(error);
    status.className = "source-status is-error";
  };

  let state: PublishState;
  let available: Available;
  try {
    state = await api.get<PublishState>(`${base}/publish`);
    available = (await api.get<{ available: Available }>(`${base}/links`)).available;
  } catch (error) {
    fail(error);
    return;
  }

  const title = input(state.title || datasetName, 120);
  const slug = input(state.slug ?? slugify(state.title || datasetName), 60);
  let slugTouched = !!state.slug;
  slug.addEventListener("input", () => (slugTouched = true));
  title.addEventListener("input", () => {
    if (!slugTouched) slug.value = slugify(title.value);
  });
  const author = input(state.author_name, 60);
  const tags = input(state.tags.join(", "), 200);
  const description = document.createElement("textarea");
  description.className = "control-input";
  description.rows = 8;
  description.maxLength = 10000;
  description.value = state.description;
  const preview = el("div", "publish-preview");
  const refreshPreview = () => renderPreview(preview, description.value);
  description.addEventListener("input", refreshPreview);
  refreshPreview();

  const license = document.createElement("select");
  license.className = "control-input";
  for (const [key, label] of Object.entries(state.licenses)) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = label;
    option.selected = key === state.license;
    license.append(option);
  }
  const download = document.createElement("input");
  download.type = "checkbox";
  download.checked = state.allow_download;
  const confirm = document.createElement("input");
  confirm.type = "checkbox";
  const confirmLabel = el("label", "source-field");
  confirmLabel.append(
    confirm,
    document.createTextNode(
      " I understand the schema, a 50-row preview and linked analyses become public.",
    ),
  );
  const downloadLabel = el("label", "source-field");
  downloadLabel.append(download, document.createTextNode(" Allow CSV download"));

  const links = el("div", "publish-links");
  const picker = document.createElement("select");
  picker.className = "control-input";
  const placeholder = document.createElement("option");
  placeholder.textContent = "Link a chart or dashboard…";
  placeholder.value = "";
  picker.append(placeholder);
  for (const chart of available.charts) {
    const option = document.createElement("option");
    option.value = `chart:${chart.id}`;
    option.textContent = `Chart: ${chart.title}`;
    picker.append(option);
  }
  for (const board of available.dashboards) {
    const option = document.createElement("option");
    option.value = `dashboard:${board.id}`;
    option.textContent = `Dashboard: ${board.title}`;
    picker.append(option);
  }
  const caption = input("", 140);
  caption.placeholder = "Short caption";

  const drawLinks = (current: PublishLink[]) => {
    links.replaceChildren();
    for (const link of current) {
      const row = el("div", "publish-link-row");
      const label = el("span");
      label.textContent = `${link.kind}: ${link.title}${link.caption ? ` - ${link.caption}` : ""}${link.public ? "" : " (not public)"}`;
      const remove = el("button", "btn btn-small") as HTMLButtonElement;
      remove.type = "button";
      remove.textContent = "Unlink";
      remove.addEventListener("click", async () => {
        try {
          const result = await api.del<{ links: PublishLink[] }>(
            `${base}/links?kind=${link.kind}&id=${encodeURIComponent(link.id)}`,
          );
          state.links = result.links;
          drawLinks(result.links);
        } catch (error) {
          fail(error);
        }
      });
      row.append(label, remove);
      links.append(row);
    }
  };
  drawLinks(state.links);

  const addLink = el("button", "btn btn-small") as HTMLButtonElement;
  addLink.type = "button";
  addLink.textContent = "Link";
  addLink.addEventListener("click", async () => {
    const [kind, id] = picker.value.split(":");
    if (!kind || !id) return;
    try {
      const result = await api.post<LinkResult>(`${base}/links`, {
        kind,
        id,
        caption: caption.value,
      });
      state.links = result.links;
      drawLinks(result.links);
      status.className = "source-status is-ok";
      status.textContent = result.notice || "Linked.";
      caption.value = "";
      picker.value = "";
    } catch (error) {
      fail(error);
    }
  });

  const urlRow = el("div", "share-link-row");
  const urlInput = input(state.url ?? "", 400);
  urlInput.readOnly = true;
  const copy = el("button", "btn btn-small") as HTMLButtonElement;
  copy.type = "button";
  copy.textContent = "Copy link";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(urlInput.value);
    } catch {
      urlInput.select();
      document.execCommand("copy");
    }
    status.className = "source-status is-ok";
    status.textContent = "Link copied.";
  });
  urlRow.append(urlInput, copy);
  urlRow.hidden = !state.url;

  const save = el("button", "btn btn-primary") as HTMLButtonElement;
  save.type = "button";
  const stop = el("button", "btn") as HTMLButtonElement;
  stop.type = "button";
  stop.textContent = "Unpublish";
  stop.hidden = !state.published;
  const sync = () => {
    save.textContent = state.published ? "Save changes" : "Publish";
    stop.hidden = !state.published;
    urlRow.hidden = !state.url;
    urlInput.value = state.url ?? "";
  };
  sync();

  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      state = await api.put<PublishState>(`${base}/publish`, {
        confirm_public: confirm.checked,
        title: title.value,
        slug: slug.value,
        description: description.value,
        tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean),
        license: license.value,
        author_name: author.value,
        allow_download: download.checked,
      });
      status.className = "source-status is-ok";
      status.textContent = "Published.";
      sync();
      onChange();
    } catch (error) {
      fail(error);
    } finally {
      save.disabled = false;
    }
  });
  stop.addEventListener("click", async () => {
    stop.disabled = true;
    try {
      await api.del(`${base}/publish`);
      state.published = false;
      state.url = null;
      status.className = "source-status is-ok";
      status.textContent = "Unpublished. The public page is gone.";
      sync();
      onChange();
    } catch (error) {
      fail(error);
    } finally {
      stop.disabled = false;
    }
  });

  const linkBar = el("div", "share-link-row");
  linkBar.append(picker, caption, addLink);
  const actions = el("div", "sheet-actions");
  actions.append(stop, save);
  body.replaceChildren(
    urlRow,
    field("Title", title),
    field("Slug (/datasets/u/...)", slug),
    field("Author name (shown publicly)", author),
    field("Description (markdown subset)", description),
    preview,
    field("Tags (comma separated)", tags),
    field("License", license),
    downloadLabel,
    el("h3"),
    field("Linked analyses (made public when linked)", links),
    linkBar,
    confirmLabel,
    status,
    actions,
  );
  status.textContent = "";
}
