/**
 * The live reasoning trace.
 *
 * Stages appear the moment they start and fill in as events arrive, so the
 * user watches the agent work instead of waiting on a spinner. Each stage is
 * collapsible and keeps its thoughts, warnings and timing after the run so the
 * chart stays auditable.
 */

import { el, formatDuration } from "./chart";

import { spinner } from "./helix";

const STAGE_LABELS: Record<string, string> = {
  search: "Finding the data",
  join: "Joining datasets",
  transform: "Shaping the data",
  chart: "Choosing the chart",
  render: "Applying defaults",
  plan: "Planning the dashboard",
};

interface StageNode {
  root: HTMLElement;
  body: HTMLElement;
  /** One line that survives collapsing, so a shut stage still says what it did. */
  summary: HTMLElement;
  status: HTMLElement;
  startedAt: number;
}

export class TraceView {
  readonly root: HTMLElement;
  private readonly list: HTMLElement;
  private readonly count: HTMLElement;
  private readonly stages = new Map<string, StageNode>();
  private active: StageNode | null = null;
  /** The user opened or closed it by hand; stop opening it for them. */
  private pinned = false;
  private startedAt = 0;

  constructor() {
    // A disclosure rather than a panel. The trace is the product's proof of
    // work, not its output: it opens itself while a run is happening so you
    // can watch, and closes to one line once the chart is there - which is
    // the moment the chart, not the reasoning, is what you came for.
    const root = document.createElement("details");
    root.className = "trace";
    this.root = root;
    const header = document.createElement("summary");
    header.className = "trace-header";
    const title = el("h2", "trace-title");
    title.textContent = "Reasoning";
    this.count = el("span", "trace-detail");
    header.append(title, this.count);
    root.addEventListener("toggle", () => {
      // Only a real click counts: opening it ourselves must not pin it. The
      // flag is cleared *here* rather than after the assignment, because
      // `toggle` is dispatched asynchronously - clearing it straight after
      // setting `open` meant every one of our own opens read as the user's,
      // and the trace then never folded itself away again.
      if (this.programmatic) this.programmatic = false;
      else this.pinned = true;
    });

    const scroll = el("div", "trace-scroll");
    this.list = el("ol", "trace-list is-top");
    // The list is a live region: on a phone the panel is often scrolled out of
    // sight, and a screen reader should still hear the run progress.
    this.list.setAttribute("aria-live", "polite");
    this.list.setAttribute("aria-relevant", "additions");
    // The top mask exists to signal "there is more above". At the top there is
    // nothing above, and fading the first stage out then looks like a bug.
    this.list.addEventListener("scroll", () => {
      this.list.classList.toggle("is-top", this.list.scrollTop < 4);
    });

    scroll.append(this.list);
    this.root.append(header, scroll);
  }

  private programmatic = false;

  /** Open or close without the toggle listener reading it as the user's doing. */
  private setOpen(open: boolean): void {
    const node = this.root as HTMLDetailsElement;
    if (node.open === open) return;
    this.programmatic = true;
    node.open = open;
  }

  reset(): void {
    this.stages.clear();
    this.active = null;
    this.list.replaceChildren();
    this.list.classList.add("is-top");
    this.count.textContent = "Working…";
    this.startedAt = performance.now();
    this.root.classList.add("is-running");
    if (!this.pinned) this.setOpen(true);
  }

  handle(event: string, data: any): void {
    switch (event) {
      case "accepted":
        this.note("Question accepted", "trace-accepted");
        break;
      case "datasets":
        this.note(
          `Data loaded: ${Object.entries(data ?? {})
            .map(([name, info]: [string, any]) => `${name} (${info.rows} rows)`)
            .join(", ")}`,
        );
        break;
      case "stage":
        if (data.status === "start") this.startStage(data.stage);
        else this.finishStage(data.stage, data);
        break;
      case "thought":
        this.addThought(data.stage, String(data.text ?? ""));
        break;
      case "delta":
        this.appendDelta(data.stage, String(data.text ?? ""));
        break;
      case "warning":
        this.addLine(String(data.text ?? ""), "trace-warn");
        break;
      case "partial":
        if (data.name === "config") this.addConfig(data.value);
        break;
      case "finding":
        this.addLine(String(data.text ?? ""), "trace-finding");
        break;
      case "code":
        this.addCode(String(data.code ?? ""));
        break;
      case "output":
        this.addOutput(data);
        break;
      case "error":
        this.addLine(
          String(data.message ?? data.code ?? "Something went wrong"),
          "trace-error",
        );
        // A failure is the one time the reasoning is the answer, so it stays
        // open and the stage that failed stays unfolded.
        this.active?.root.classList.remove("is-collapsed");
        this.finish(true);
        break;
      case "done":
        this.finish(false);
        break;
    }
  }

  private startStage(name: string): void {
    if (this.stages.has(name)) return;

    const root = el("li", "trace-stage is-running");
    const head = el("div", "trace-stage-head");

    const label = el("span", "trace-stage-label");
    label.textContent = STAGE_LABELS[name] ?? name;

    const status = el("span", "trace-stage-status");
    status.append(spinner(18));

    head.append(label, status);
    const summary = el("p", "trace-stage-summary");
    summary.hidden = true;
    const body = el("div", "trace-stage-body");
    root.append(head, summary, body);

    // Collapsing a finished stage keeps a long trace readable.
    head.addEventListener("click", () => root.classList.toggle("is-collapsed"));

    this.list.append(root);
    const node: StageNode = {
      root, body, summary, status, startedAt: performance.now(),
    };
    this.stages.set(name, node);
    this.active = node;
    this.count.textContent = STAGE_LABELS[name] ?? name;
    root.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  private finishStage(name: string, data: any): void {
    const node = this.stages.get(name);
    if (!node) return;

    node.root.classList.remove("is-running");
    node.root.classList.add("is-done");
    node.status.replaceChildren();
    node.status.textContent = formatDuration(performance.now() - node.startedAt);

    const detail = summarise(data);
    if (detail) {
      node.summary.textContent = detail;
      node.summary.hidden = false;
    }
    // A finished stage folds to its one-line result. The thoughts, the code
    // and the output are still there behind a click; leaving them all open
    // buried the chart under a screen of transcript on every run.
    node.root.classList.add("is-collapsed");
    if (this.active === node) this.active = null;
  }

  private addThought(stage: string | undefined, text: string): void {
    if (!text) return;
    const node = (stage && this.stages.get(stage)) || this.active;
    const line = el("p", "trace-thought");
    line.textContent = text;
    (node?.body ?? this.list).append(line);
  }

  private appendDelta(stage: string | undefined, text: string): void {
    const node = (stage && this.stages.get(stage)) || this.active;
    if (!node) return;
    let live = node.body.querySelector<HTMLElement>(".trace-live");
    if (!live) {
      live = el("p", "trace-thought trace-live");
      node.body.append(live);
    }
    live.textContent = (live.textContent ?? "") + text;
  }

  private addConfig(config: any): void {
    const node = this.stages.get("chart") ?? this.active;
    if (!node || !config) return;
    const line = el("p", "trace-detail");
    const parts = [`type: ${config.chart_type}`];
    if (config.x) parts.push(`x: ${config.x}`);
    if (config.y) parts.push(`y: ${config.y}`);
    if (config.color) parts.push(`colour: ${config.color}`);
    line.textContent = parts.join(" · ");
    node.body.append(line);
  }

  private addCode(code: string): void {
    if (!code.trim()) return;
    const block = el("pre", "trace-code");
    block.textContent = code;
    (this.active?.body ?? this.list).append(block);
  }

  private addOutput(data: any): void {
    const block = el("pre", data.ok ? "trace-output" : "trace-output is-error");
    block.textContent = String(data.stdout ?? data.error ?? "").slice(-2000);
    if (block.textContent.trim()) (this.active?.body ?? this.list).append(block);
  }

  private addLine(text: string, className: string): void {
    const line = el("p", `trace-line ${className}`);
    line.textContent = text;
    (this.active?.body ?? this.list).append(line);
  }

  private note(text: string, className = "trace-note"): void {
    const line = el("li", className);
    line.textContent = text;
    this.list.append(line);
  }

  /** The run ended. Say what it took in one line, and get out of the way. */
  private finish(failed: boolean): void {
    this.stopSpinners();
    this.root.classList.remove("is-running");
    const stages = this.stages.size;
    const elapsed = this.startedAt ? formatDuration(performance.now() - this.startedAt) : "";
    this.count.textContent = failed
      ? "Did not finish"
      : [`${stages} ${stages === 1 ? "stage" : "stages"}`, elapsed]
          .filter(Boolean)
          .join(" · ");
    if (!this.pinned && !failed) this.setOpen(false);
  }

  private stopSpinners(): void {
    for (const node of this.stages.values()) {
      if (node.root.classList.contains("is-running")) {
        node.root.classList.remove("is-running");
        node.status.replaceChildren();
      }
    }
  }
}

function summarise(data: any): string {
  const parts: string[] = [];
  if (Array.isArray(data.datasets)) parts.push(`datasets: ${data.datasets.join(", ")}`);
  if (typeof data.confidence === "number")
    parts.push(`confidence: ${(data.confidence * 100).toFixed(0)}%`);
  if (data.chart_type) parts.push(`chart: ${data.chart_type}`);
  if (typeof data.rows === "number") parts.push(`${data.rows.toLocaleString()} rows`);
  if (typeof data.issues === "number" && data.issues > 0)
    parts.push(`${data.issues} issue(s)`);
  if (data.fallback) parts.push(`fallback: ${data.fallback}`);
  return parts.join(" · ");
}
