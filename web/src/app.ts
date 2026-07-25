/**
 * App shell: sign-in, the ask box, the live trace, the chart, and the SQL tab.
 *
 * Deliberately framework-free. The dynamic surface is small — one chart, one
 * trace, one editor — and a runtime would cost more than it saves here.
 */

import { ApiError, api, stream, type ChartConfig, type PipelineResult, type User } from "./api";
import { ChartView, button, el } from "./chart";
import { logo, spinner } from "./helix";
import { TraceView } from "./trace";

interface State {
  user: User | null;
  lastResult: PipelineResult | null;
  lastQuestion: string;
  sources: { id: string; name: string; kind: string; supports_sql: boolean }[];
  activeSource: string | null;
  cancel: (() => void) | null;
}

const state: State = {
  user: null,
  lastResult: null,
  lastQuestion: "",
  sources: [],
  activeSource: null,
  cancel: null,
};

const root = document.getElementById("root")!;
const trace = new TraceView();
const chart = new ChartView({
  onConfigChange: applyConfig,
  onRerun: (request) => ask(state.lastQuestion, request),
});

boot().catch((error) => showFatal(error));

async function boot(): Promise<void> {
  try {
    state.user = await api.me();
  } catch {
    state.user = null;
  }
  render();
  if (state.user?.signed_in) void loadSources();
}

// --------------------------------------------------------------------------
// Layout
// --------------------------------------------------------------------------

function render(): void {
  root.replaceChildren();
  root.append(header(), main());
}

function header(): HTMLElement {
  const bar = el("header", "app-header");
  const brand = el("a", "brand");
  (brand as HTMLAnchorElement).href = "/";
  brand.append(logo(28));
  const name = el("span");
  name.textContent = "twoHelixes";
  brand.append(name);

  const right = el("div", "app-header-right");
  if (state.user?.signed_in) {
    const meta = el("span", "user-meta");
    meta.textContent = state.user.paid
      ? `${state.user.api_credits.toLocaleString()} credits`
      : `${state.user.free_queries_left} free ${plural(state.user.free_queries_left, "query", "queries")} left`;
    right.append(meta, themeToggle(), button("Sign out", signOut));
  } else {
    right.append(themeToggle());
  }

  bar.append(brand, right);
  return bar;
}

function themeToggle(): HTMLElement {
  const node = button(prefersDark() ? "Light" : "Dark", () => {
    const next = prefersDark() ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("th-theme", next);
    render();
    if (state.lastResult) void chart.show(state.lastResult);
  });
  node.classList.add("theme-toggle");
  return node;
}

function prefersDark(): boolean {
  const stamped = document.documentElement.getAttribute("data-theme");
  if (stamped) return stamped === "dark";
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function main(): HTMLElement {
  const wrap = el("main", "app-main");
  if (!state.user?.signed_in) {
    wrap.append(signInPanel());
    return wrap;
  }

  const left = el("div", "app-left");
  left.append(askPanel(), trace.root);

  const right = el("div", "app-right");
  right.append(chart.root);

  wrap.append(left, right);
  return wrap;
}

function signInPanel(): HTMLElement {
  const panel = el("section", "signin");
  const heading = el("h1");
  heading.textContent = "Sign in to start asking";
  const lede = el("p", "signin-lede");
  lede.textContent =
    "New accounts get three free queries. Anonymous requests are not accepted — that is what keeps the free tier available for real users.";

  const form = el("form", "signin-form") as HTMLFormElement;
  const input = document.createElement("input");
  input.type = "email";
  input.required = true;
  input.placeholder = "you@company.com";
  input.className = "signin-input";
  input.autocomplete = "email";

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "btn btn-primary";
  submit.textContent = "Continue";

  const error = el("p", "note note-warn");
  error.hidden = true;

  form.append(input, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      state.user = await api.signIn(input.value.trim());
      render();
      void loadSources();
    } catch (exc) {
      error.hidden = false;
      error.textContent = (exc as Error).message;
      submit.disabled = false;
    }
  });

  panel.append(heading, lede, form, error);
  return panel;
}

function askPanel(): HTMLElement {
  const panel = el("section", "ask");
  const form = el("form", "ask-form") as HTMLFormElement;

  const input = document.createElement("textarea");
  input.className = "ask-input";
  input.rows = 3;
  input.placeholder =
    "Ask anything about your data — “which regions are shrinking, and by how much?”";
  input.value = state.lastQuestion;

  const row = el("div", "ask-row");
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "btn btn-primary";
  submit.textContent = "Ask";

  const stop = button("Stop", () => {
    state.cancel?.();
    state.cancel = null;
    render();
  });
  stop.hidden = !state.cancel;

  row.append(sourcePicker(), submit, stop);
  form.append(input, row);

  // Enter submits; Shift+Enter is a newline.
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (value) void ask(value);
  });

  panel.append(form);
  return panel;
}

function sourcePicker(): HTMLElement {
  const wrap = el("label", "control");
  const node = document.createElement("select");
  node.className = "control-input";

  const none = document.createElement("option");
  none.value = "";
  none.textContent = state.sources.length ? "Choose a source" : "No sources yet";
  node.append(none);

  for (const source of state.sources) {
    const item = document.createElement("option");
    item.value = source.id;
    item.textContent = `${source.name} (${source.kind})`;
    if (source.id === state.activeSource) item.selected = true;
    node.append(item);
  }
  node.addEventListener("change", () => {
    state.activeSource = node.value || null;
  });

  wrap.append(node);
  return wrap;
}

async function loadSources(): Promise<void> {
  try {
    const payload = await api.get<{ sources: State["sources"] }>("/v1/sources");
    state.sources = payload.sources ?? [];
    render();
  } catch {
    /* a missing source list must not block asking */
  }
}

// --------------------------------------------------------------------------
// Running a query
// --------------------------------------------------------------------------

function ask(question: string, edit = ""): void {
  if (!question) return;
  state.lastQuestion = question;
  state.cancel?.();
  trace.reset();
  render();

  const body: Record<string, unknown> = {
    q: question,
    mode: prefersDark() ? "dark" : "light",
  };
  if (edit) {
    body.edit = edit;
    if (state.lastResult) body.config = state.lastResult.config;
  }
  if (state.activeSource) body.source_id = state.activeSource;

  state.cancel = stream("/v1/query/stream", body, (event, data) => {
    trace.handle(event, data);

    if (event === "result") {
      state.lastResult = data as PipelineResult;
      void chart.show(state.lastResult);
    } else if (event === "user") {
      state.user = data as User;
      root.replaceChild(header(), root.firstChild!);
    } else if (event === "done") {
      state.cancel = null;
    } else if (event === "error") {
      showError(data);
      state.cancel = null;
    }
  });
}

/** Apply a manual control change without spending a query. */
async function applyConfig(config: ChartConfig): Promise<void> {
  const chartId = state.lastResult?.chart_id;
  if (!chartId) {
    showError({
      code: "not_saved",
      message: "Run a query first — there is no chart to reconfigure yet.",
    });
    return;
  }
  try {
    const payload = await api.post<{
      figure: PipelineResult["figure"];
      config: ChartConfig;
      warnings: string[];
      audit: PipelineResult["audit"];
    }>(`/v1/chart/${chartId}/config`, { ...config, mode: prefersDark() ? "dark" : "light" });

    state.lastResult = {
      ...(state.lastResult as PipelineResult),
      figure: payload.figure,
      config: payload.config,
      warnings: payload.warnings ?? [],
      audit: payload.audit ?? [],
    };
    await chart.show(state.lastResult);
  } catch (error) {
    showError({
      code: (error as ApiError).code,
      message: (error as Error).message,
    });
  }
}

function showError(data: { code?: string; message?: string }): void {
  const banner = el("div", "banner banner-error");
  const text = el("span");
  text.textContent = data.message ?? data.code ?? "Something went wrong";
  banner.append(text);

  if (data.code === "free_quota_exhausted" || data.code === "insufficient_credits") {
    const link = el("a", "btn btn-primary btn-small") as HTMLAnchorElement;
    link.href = "/pricing";
    link.textContent = "Add credits";
    banner.append(link);
  }

  banner.append(button("Dismiss", () => banner.remove()));
  root.prepend(banner);
}

function showFatal(error: unknown): void {
  root.replaceChildren();
  const panel = el("section", "signin");
  const heading = el("h1");
  heading.textContent = "twoHelixes could not start";
  const detail = el("p", "note note-warn");
  detail.textContent = String((error as Error)?.message ?? error);
  panel.append(heading, detail);
  root.append(panel);
}

async function signOut(): Promise<void> {
  await api.signOut().catch(() => undefined);
  state.user = null;
  state.lastResult = null;
  trace.reset();
  render();
}

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
}

// Restore the stored theme before first paint of dynamic content.
const storedTheme = localStorage.getItem("th-theme");
if (storedTheme) document.documentElement.setAttribute("data-theme", storedTheme);

/**
 * Test hook. Visualbench and the e2e suite drive the real UI through this so
 * a capture exercises the same code path a user does, rather than a parallel
 * fetch that never reaches the renderer.
 */
(window as unknown as Record<string, unknown>).__thAsk = (
  question: string,
  extra: Record<string, unknown> = {},
) => askWith(question, extra);

function askWith(question: string, extra: Record<string, unknown>): void {
  state.lastQuestion = question;
  state.cancel?.();
  trace.reset();
  render();

  const body: Record<string, unknown> = {
    q: question,
    mode: prefersDark() ? "dark" : "light",
    ...extra,
  };

  state.cancel = stream("/v1/query/stream", body, (event, data) => {
    trace.handle(event, data);
    if (event === "result") {
      state.lastResult = data as PipelineResult;
      void chart.show(state.lastResult);
    } else if (event === "done" || event === "error") {
      state.cancel = null;
    }
  });
}

export { spinner };
