import type { PlotlyFigure } from "./api";

export interface ChartViewState {
  title?: string;
  sort?: "" | "desc" | "asc" | "label";
  top?: number;
  format?: "" | "compact" | "comma" | "fixed2" | "percent" | "currency";
  logY?: boolean;
  logX?: boolean;
  labels?: boolean;
  legend?: "" | "top" | "right" | "bottom" | "off";
  grid?: boolean;
  slider?: boolean;
  hidden?: string[];
}

const FORMATS: Record<string, string> = {
  compact: "~s",
  comma: ",.0f",
  fixed2: ",.2f",
  percent: ".0%",
  currency: "$,.0f",
};

const CARTESIAN = new Set(["bar", "scatter", "scattergl", "histogram", "box", "waterfall", "funnel", "candlestick"]);

export function isCartesian(figure: PlotlyFigure): boolean {
  const data = figure.data ?? [];
  return data.length > 0 && data.every((t) => CARTESIAN.has(String(t.type ?? "scatter")));
}

export function isBarLike(figure: PlotlyFigure): boolean {
  const data = figure.data ?? [];
  return data.length > 0 && data.every((t) => String(t.type ?? "") === "bar");
}

export function seriesNames(figure: PlotlyFigure): string[] {
  const names = (figure.data ?? [])
    .filter((t) => t.showlegend !== false && t.name)
    .map((t) => String(t.name));
  return [...new Set(names)];
}

export function isEmptyView(view: ChartViewState | undefined): boolean {
  if (!view) return true;
  return !Object.values(view).some((v) => (Array.isArray(v) ? v.length : Boolean(v)));
}

export function applyView(figure: PlotlyFigure, view: ChartViewState | undefined): PlotlyFigure {
  if (!view || isEmptyView(view)) return figure;
  const data = (figure.data ?? []).map((t) => ({ ...t })) as Record<string, any>[];
  const layout: Record<string, any> = { ...(figure.layout ?? {}) };
  const out: PlotlyFigure = { ...figure, data, layout };

  if (view.title !== undefined && view.title !== "") {
    const title = layout.title;
    layout.title = typeof title === "object" && title ? { ...title, text: view.title } : { text: view.title };
  }

  const hidden = new Set(view.hidden ?? []);
  if (hidden.size) {
    for (const t of data) if (t.name && hidden.has(String(t.name))) t.visible = "legendonly";
  }

  if (view.legend === "off") layout.showlegend = false;
  else if (view.legend === "right") {
    layout.showlegend = true;
    layout.legend = { ...(layout.legend ?? {}), orientation: "v", x: 1.02, xanchor: "left", y: 1, yanchor: "top" };
  } else if (view.legend === "bottom") {
    layout.showlegend = true;
    layout.legend = { ...(layout.legend ?? {}), orientation: "h", x: 0, xanchor: "left", y: -0.18, yanchor: "top" };
    layout.margin = { ...(layout.margin ?? {}), b: Math.max(Number(layout.margin?.b ?? 0), 96) };
  }

  if (!isCartesian(out)) return out;

  const horizontal = data.some((t) => t.orientation === "h");
  const catKey = horizontal ? "y" : "x";
  const valKey = horizontal ? "x" : "y";
  const catAxis = horizontal ? "yaxis" : "xaxis";
  const valAxis = horizontal ? "xaxis" : "yaxis";
  const axis = (name: string) => (layout[name] = { ...(layout[name] ?? {}) });

  if (isBarLike(out) && categorical(data, catKey)) {
    if (view.top && view.top > 0) topN(data, catKey, valKey, view.top);
    if (view.sort === "desc") axis(catAxis).categoryorder = horizontal ? "total ascending" : "total descending";
    else if (view.sort === "asc") axis(catAxis).categoryorder = horizontal ? "total descending" : "total ascending";
    else if (view.sort === "label") axis(catAxis).categoryorder = horizontal ? "category descending" : "category ascending";
  }

  const fmt = view.format ? FORMATS[view.format] : "";
  if (fmt) axis(valAxis).tickformat = fmt;
  if (view.logY) axis(valAxis).type = "log";
  if (view.logX && !horizontal && !categorical(data, "x")) axis("xaxis").type = "log";
  if (view.grid === false) {
    axis("xaxis").showgrid = false;
    axis("yaxis").showgrid = false;
  }
  if (view.slider && !horizontal && !isBarLike(out)) {
    axis("xaxis").rangeslider = { visible: true, thickness: 0.07 };
  }
  if (view.labels) {
    const spec = `%{${valKey}${fmt ? `:${fmt}` : ""}}`;
    for (const t of data) {
      const type = String(t.type ?? "scatter");
      if (type === "bar") {
        t.texttemplate = spec;
        t.textposition = data.length > 1 && layout.barmode === "stack" ? "inside" : "outside";
        t.cliponaxis = false;
      } else if (type === "scatter" || type === "scattergl") {
        const mode = String(t.mode ?? "lines");
        if (!mode.includes("text")) t.mode = `${mode}+text`;
        t.texttemplate = spec;
        t.textposition = "top center";
      }
    }
  }
  return out;
}

function categorical(data: Record<string, any>[], key: string): boolean {
  const values = data.flatMap((t) => (Array.isArray(t[key]) ? t[key].slice(0, 20) : []));
  if (!values.length) return false;
  return values.every((v) => typeof v === "string" && Number.isNaN(Date.parse(v)));
}

function topN(data: Record<string, any>[], catKey: string, valKey: string, n: number): void {
  const totals = new Map<string, number>();
  for (const t of data) {
    const cats = t[catKey] ?? [];
    const vals = t[valKey] ?? [];
    cats.forEach((c: unknown, i: number) => totals.set(String(c), (totals.get(String(c)) ?? 0) + (Number(vals[i]) || 0)));
  }
  if (totals.size <= n) return;
  const keep = new Set([...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(([k]) => k));
  for (const t of data) {
    const cats: unknown[] = t[catKey] ?? [];
    const idx = cats.map((c, i) => (keep.has(String(c)) ? i : -1)).filter((i) => i >= 0);
    for (const key of [catKey, valKey, "text", "customdata", "hovertext"]) {
      if (Array.isArray(t[key])) t[key] = idx.map((i) => t[key][i]);
    }
    if (t.marker && Array.isArray(t.marker.color)) t.marker = { ...t.marker, color: idx.map((i) => t.marker.color[i]) };
  }
}

export function figureCsv(figure: PlotlyFigure): string {
  const rows: string[][] = [];
  const data = figure.data ?? [];
  const first = data[0] as Record<string, any> | undefined;
  if (first?.type === "table") {
    const header = (first.header?.values ?? []).map(String);
    const cols: unknown[][] = first.cells?.values ?? [];
    rows.push(header);
    const n = Math.max(0, ...cols.map((c) => c.length));
    for (let i = 0; i < n; i++) rows.push(cols.map((c) => String(c[i] ?? "")));
    return toCsv(rows);
  }
  rows.push(["series", "x", "y", "z"]);
  for (const t of data as Record<string, any>[]) {
    const name = String(t.name ?? t.type ?? "");
    const xs = t.x ?? t.labels ?? t.lat ?? [];
    const ys = t.y ?? t.values ?? t.lon ?? [];
    const zs = t.z ?? t.parents ?? [];
    const n = Math.max(xs.length ?? 0, ys.length ?? 0);
    for (let i = 0; i < n; i++) rows.push([name, cell(xs[i]), cell(ys[i]), cell(zs[i])]);
  }
  return toCsv(rows);
}

export function rowsCsv(rows: Record<string, unknown>[]): string {
  if (!rows.length) return "";
  const headers = Object.keys(rows[0]);
  return toCsv([headers, ...rows.map((r) => headers.map((h) => cell(r[h])))]);
}

function cell(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function toCsv(rows: string[][]): string {
  return rows
    .map((r) => r.map((v) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v)).join(","))
    .join("\n");
}

export function slug(text: string): string {
  return (text || "chart").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "chart";
}

export function dataTable(rows: Record<string, unknown>[], total: number): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "data-table-wrap";
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "note data-table-empty";
    empty.textContent = "No row preview for this chart. Export CSV for the plotted values.";
    wrap.append(empty);
    return wrap;
  }
  const headers = Object.keys(rows[0]);
  const numeric = headers.map((h) => rows.every((r) => r[h] == null || typeof r[h] === "number"));
  let sortBy = -1;
  let dir = 1;
  const table = document.createElement("table");
  table.className = "data-table";
  const thead = document.createElement("thead");
  const tbody = document.createElement("tbody");
  const head = document.createElement("tr");
  const nf = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

  const draw = () => {
    const sorted = sortBy < 0 ? rows : [...rows].sort((a, b) => {
      const h = headers[sortBy];
      const x = a[h];
      const y = b[h];
      if (x == null) return 1;
      if (y == null) return -1;
      return (numeric[sortBy] ? Number(x) - Number(y) : String(x).localeCompare(String(y))) * dir;
    });
    tbody.replaceChildren(
      ...sorted.map((r) => {
        const tr = document.createElement("tr");
        headers.forEach((h, i) => {
          const td = document.createElement("td");
          const v = r[h];
          td.textContent = v == null ? "" : numeric[i] ? nf.format(Number(v)) : String(v);
          if (numeric[i]) td.className = "num";
          tr.append(td);
        });
        return tr;
      }),
    );
    head.querySelectorAll("th").forEach((th, i) => {
      th.setAttribute("aria-sort", i === sortBy ? (dir > 0 ? "ascending" : "descending") : "none");
    });
  };

  headers.forEach((h, i) => {
    const th = document.createElement("th");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = h;
    b.addEventListener("click", () => {
      if (sortBy === i) dir = -dir;
      else {
        sortBy = i;
        dir = numeric[i] ? -1 : 1;
      }
      draw();
    });
    th.append(b);
    if (numeric[i]) th.className = "num";
    head.append(th);
  });
  thead.append(head);
  table.append(thead, tbody);
  draw();
  const foot = document.createElement("p");
  foot.className = "data-table-foot";
  foot.textContent = total > rows.length
    ? `First ${rows.length.toLocaleString()} of ${total.toLocaleString()} rows. Export CSV for every plotted value.`
    : `${rows.length.toLocaleString()} rows`;
  wrap.append(table, foot);
  return wrap;
}
