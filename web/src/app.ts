/**
 * App shell: sign-in, the ask box, the live trace, the chart, and the SQL tab.
 *
 * Deliberately framework-free. The dynamic surface is small — one chart, one
 * trace, one editor — and a runtime would cost more than it saves here.
 */

import { ApiError, api, stream, type ChartConfig, type PipelineResult, type PlotlyFigure, type User } from "./api";
import { ChartView, button, el, renderFigure } from "./chart";
import { logo, spinner } from "./helix";
import { Builder } from "./builder";
import { DashboardView } from "./dashboard";
import { DashboardListView } from "./dashboards";
import { SourcesPanel } from "./sources";
import { TraceView } from "./trace";

interface Sample {
  key: string;
  name: string;
  description: string;
  questions: string[];
  rows: number;
}

type View = "chat" | "builder" | "dashboard" | "dashboards";

interface State {
  view: View;
  builder: Builder | null;
  dashboard: DashboardView | null;
  user: User | null;
  lastResult: PipelineResult | null;
  lastQuestion: string;
  sources: { id: string; name: string; kind: string; supports_sql: boolean }[];
  activeSource: string | null;
  /** The sample a signed-out visitor is trying. The only data the trial can
   *  reach, so it is sent by key rather than attached to an account. */
  activeSample: string | null;
  cancel: (() => void) | null;
  samples: Sample[];
  suggestions: string[];
  ran: boolean;
  /** The visitor asked for the sign-in form before spending the trial. */
  showSignIn: boolean;
}

const state: State = {
  view: "chat",
  builder: null,
  dashboard: null,
  user: null,
  lastResult: null,
  lastQuestion: "",
  sources: [],
  activeSource: null,
  activeSample: null,
  cancel: null,
  samples: [],
  suggestions: [],
  ran: false,
  showSignIn: false,
};

const root = document.getElementById("root")!;
const trace = new TraceView();
const chart = new ChartView({
  onConfigChange: applyConfig,
  onRerun: (request) => ask(state.lastQuestion, request),
  onPin: (result) => void pinToDashboard(result),
  question: () => state.lastQuestion,
});

boot().catch((error) => showFatal(error));

async function boot(): Promise<void> {
  try {
    state.user = await api.me();
  } catch {
    state.user = null;
  }

  // A dataset page links here with the question it just showed. Arriving with
  // an empty ask box after clicking "ask this yourself" loses the thread the
  // visitor was following, and they have to retype what they just read.
  const params = new URLSearchParams(window.location.search);
  const sample = params.get("sample");
  const question = params.get("q");
  if (sample) state.activeSample = sample;
  // Through state, not through the DOM: loadSources() and loadSamples() both
  // re-render, and a value written straight onto the textarea is gone by the
  // time either of them lands.
  if (question) state.lastQuestion = question;

  render();
  if (state.user?.signed_in) void loadSources();
  // The samples are what an anonymous visitor can ask about, so they have to
  // load before sign-in rather than after it.
  else void loadSamples();

  if (question) {
    document.querySelector<HTMLTextAreaElement>(".ask-input")?.focus();
    // Prefilled, not submitted: the run costs the visitor an allowance, and
    // spending it before they have even read the box is not theirs to spend.
    history.replaceState(null, "", window.location.pathname);
  }
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
    // Credits are a balance the account holder spends and can run out of, so
    // it stays. A running total of free queries was a countdown to a wall in
    // the corner of every screen, which is not what the header is for.
    if (state.user.paid) {
      const meta = el("span", "user-meta");
      meta.textContent = `${state.user.api_credits.toLocaleString()} credits`;
      right.append(meta);
    }
    right.append(themeToggle(), button("Sign out", signOut));
  } else {
    // A visitor who has decided to sign up should not have to spend the trial
    // first to find the form.
    right.append(
      themeToggle(),
      button("Sign in", () => {
        state.showSignIn = true;
        render();
        document.querySelector<HTMLInputElement>(".signin-input")?.focus();
      }),
    );
  }

  bar.append(brand);
  if (state.user?.signed_in) bar.append(nav());
  bar.append(right);
  return bar;
}

/**
 * Where you are and where else you can be. Before this the dashboard existed
 * but was only reachable from a test hook, which is the same as not existing.
 */
function nav(): HTMLElement {
  const wrap = el("nav", "app-nav");
  wrap.setAttribute("aria-label", "Sections");

  const items: { label: string; view: View; go: () => void }[] = [
    { label: "Ask", view: "chat", go: () => { state.view = "chat"; render(); } },
    {
      label: "Dashboards",
      view: "dashboards",
      go: () => void openDashboardList(),
    },
  ];

  for (const item of items) {
    const node = el("button", "app-nav-item") as HTMLButtonElement;
    node.type = "button";
    node.textContent = item.label;
    const here =
      state.view === item.view ||
      (item.view === "dashboards" && state.view === "dashboard");
    node.classList.toggle("is-active", here);
    if (here) node.setAttribute("aria-current", "page");
    node.addEventListener("click", item.go);
    wrap.append(node);
  }
  return wrap;
}

// Same mark and the same storage key as the marketing header, so the control
// does not change shape when a visitor crosses into the product.
const THEME_ICONS =
  '<svg class="i-moon" viewBox="0 0 24 24" aria-hidden="true"><path' +
  ' d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/></svg>' +
  '<svg class="i-sun" viewBox="0 0 24 24" aria-hidden="true"><circle' +
  ' cx="12" cy="12" r="4.2"/><path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2' +
  'M19.2 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6' +
  'M6.9 17.1l-1.6 1.6"/></svg>';

function themeToggle(): HTMLElement {
  const node = document.createElement("button");
  node.type = "button";
  node.className = "icon-btn theme-toggle";
  node.setAttribute("aria-label", "Switch between light and dark");
  node.innerHTML = THEME_ICONS;
  node.addEventListener("click", () => {
    const next = prefersDark() ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("th-theme", next);
    } catch {
      /* private mode: the choice just does not persist */
    }
    render();
    // Plotly bakes theme colours into the figure, so it has to be redrawn.
    if (state.lastResult) void chart.show(state.lastResult);
  });
  return node;
}

function prefersDark(): boolean {
  const stamped = document.documentElement.getAttribute("data-theme");
  if (stamped) return stamped === "dark";
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function main(): HTMLElement {
  const wrap = el("main", "app-main");
  // Anonymous visitors get the product, not a form. They can ask one question
  // on the sample data; the sign-in panel appears when they have seen it work,
  // which is the only moment an email is worth asking for.
  if (!state.user?.signed_in && (state.ran || state.showSignIn)) {
    wrap.append(
      signInPanel(state.ran ? "You have seen it work. Keep going?" : ""),
    );
    return wrap;
  }

  // These views own their width and their own state, so they are appended
  // rather than rebuilt - re-rendering would discard the plan or the layout.
  if (state.view === "builder" && state.builder) {
    wrap.classList.add("app-main-wide");
    wrap.append(state.builder.root);
    return wrap;
  }
  if (state.view === "dashboard" && state.dashboard) {
    wrap.classList.add("app-main-wide");
    wrap.append(state.dashboard.root);
    return wrap;
  }
  if (state.view === "dashboards") {
    wrap.classList.add("app-main-wide");
    wrap.append(dashboardList().root);
    return wrap;
  }

  const left = el("div", "app-left");
  left.append(askPanel());
  if (state.suggestions.length) left.append(suggestionPanel());
  // An empty "Reasoning" panel before the first run is a stub, not an
  // affordance - there is nothing to reason about yet.
  if (state.ran) left.append(trace.root);

  const right = el("div", "app-right");
  // Before the first run there is no chart, and an empty panel is a dead end.
  // The samples are the only thing a brand-new account can actually ask about.
  right.append(state.ran || !state.samples.length ? chart.root : starterPanel());

  wrap.append(left, right);
  return wrap;
}

/**
 * The first-run path. A new account has no source connected, so the picker
 * reads "no sources yet" and the ask box has nothing to answer from - the
 * fastest way to lose someone in their first minute. These are the sample
 * datasets the marketing pages promise; one tap attaches one and fills the
 * ask box with a question that suits it.
 */
function starterPanel(): HTMLElement {
  const panel = el("section", "chart-view starter");
  const heading = el("h2");
  heading.textContent = "Start with a sample";
  const lede = el("p", "starter-lede");
  lede.textContent =
    "Nothing connected yet. Load one of these and ask it something — or connect your own source from the picker above.";

  const list = el("ul", "sample-list");
  for (const sample of state.samples.slice(0, 6)) {
    const item = el("li");
    const node = el("button", "sample") as HTMLButtonElement;
    node.type = "button";

    const text = el("span");
    const name = el("span", "sample-name");
    name.textContent = sample.name;
    const detail = el("p", "trace-detail");
    detail.textContent = sample.description;
    text.append(name, detail);

    const meta = el("span", "sample-meta");
    meta.textContent = `${sample.rows.toLocaleString()} rows`;

    node.append(text, meta);
    node.addEventListener("click", () => void attachSample(sample, node));
    item.append(node);
    list.append(item);
  }

  panel.append(heading, lede, list);
  return panel;
}

async function attachSample(sample: Sample, node: HTMLButtonElement): Promise<void> {
  // Signed out, there is no account to attach anything to. The sample is sent
  // by key with the question instead, which is exactly what the trial allows.
  if (!state.user?.signed_in) {
    state.activeSample = sample.key;
    state.suggestions = sample.questions.slice(0, 4);
    render();
    const box = document.querySelector<HTMLTextAreaElement>(".ask-input");
    if (box) {
      box.value = sample.questions[0] ?? "";
      box.focus();
    }
    return;
  }

  node.disabled = true;
  const meta = node.querySelector<HTMLElement>(".sample-meta");
  if (meta) meta.textContent = "Loading…";
  try {
    await api.post(`/v1/samples/${sample.key}/attach`, {});
    state.suggestions = sample.questions.slice(0, 4);
    await loadSources();
    const box = document.querySelector<HTMLTextAreaElement>(".ask-input");
    if (box && !box.value) {
      box.value = sample.questions[0] ?? "";
      box.focus();
    }
  } catch (error) {
    node.disabled = false;
    if (meta) meta.textContent = "Failed";
    showError({
      code: (error as ApiError).code,
      message: (error as Error).message,
    });
  }
}

function suggestionPanel(): HTMLElement {
  const panel = el("section", "ask starter");
  const heading = el("h2");
  heading.textContent = "Try one of these";
  const row = el("div", "suggestions");
  for (const question of state.suggestions) {
    const node = el("button", "suggestion") as HTMLButtonElement;
    node.type = "button";
    node.textContent = question;
    node.addEventListener("click", () => void ask(question));
    row.append(node);
  }
  panel.append(heading, row);
  return panel;
}

async function loadSamples(): Promise<void> {
  try {
    const payload = await api.get<{ samples: Sample[] }>("/v1/samples");
    state.samples = payload.samples ?? [];
    render();
  } catch {
    /* the samples are an offer, not a requirement */
  }
}

function signInPanel(prompt = ""): HTMLElement {
  const panel = el("section", "signin");
  const heading = el("h1");
  heading.textContent = prompt || "Sign in to start asking";
  const lede = el("p", "signin-lede");
  lede.textContent =
    "An email, and nothing else — no password, no card. Then point it at your own data.";

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
    "Ask for a chart — “which regions are shrinking, and by how much?”";
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

  row.append(sourcePicker(), sourcesPanel().button(), submit, stop);
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

let panel: SourcesPanel | null = null;

/**
 * One panel instance for the session: it owns two `<dialog>` elements appended
 * to the body, so rebuilding it on every render would leak a pair each time.
 */
function sourcesPanel(): SourcesPanel {
  panel ??= new SourcesPanel({
    onChange: (sources, _datasets, selected) => {
      state.sources = sources;
      // Whatever was just added is almost certainly what the next question is
      // about, so select it rather than making the user find it in the picker.
      if (selected?.type === "source") state.activeSource = selected.id;
      else if (sources.length === 1) state.activeSource = sources[0].id;
      render();
    },
  });
  return panel;
}

async function loadSources(): Promise<void> {
  try {
    const payload = await api.get<{ sources: State["sources"] }>("/v1/sources");
    state.sources = payload.sources ?? [];
    // Not when a sample was named in the URL: someone who arrived from a
    // dataset page asked about that dataset, not about whatever source they
    // happen to have connected.
    if (!state.activeSource && !state.activeSample && state.sources.length === 1) {
      // One source is not a choice. Pre-selecting it removes a step that only
      // ever has one right answer.
      state.activeSource = state.sources[0].id;
    }
    render();
    if (!state.sources.length && !state.samples.length) void loadSamples();
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
  state.ran = true;
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
  else if (state.activeSample) body.sample = state.activeSample;

  state.cancel = stream("/v1/query/stream", body, (event, data) => {
    trace.handle(event, data);

    if (event === "result") {
      state.lastResult = data as PipelineResult;
      void chart.show(state.lastResult).then(revealChart);
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

/**
 * On a phone the chart sits below the whole trace, so a finished run lands
 * off-screen and looks like nothing happened. On desktop the chart is already
 * beside the trace and moving the page would be rude.
 */
function revealChart(): void {
  if (window.innerWidth >= 1080) return;
  chart.root.scrollIntoView({ block: "start", behavior: "smooth" });
}

function showError(data: { code?: string; message?: string }): void {
  const banner = el("div", "banner banner-error");
  const text = el("span");
  text.textContent = data.message ?? data.code ?? "Something went wrong";
  banner.append(text);

  // A wall that only says "no" is a wall. Each refusal carries the one action
  // that clears it, and the two are different actions: a visitor who has used
  // the trial needs an account, not a price list.
  if (data.code === "signin_required" || data.code === "trial_used") {
    banner.append(
      button("Sign in — free", () => {
        state.ran = false;
        render();
        document.querySelector<HTMLInputElement>(".signin-input")?.focus();
      }),
    );
  } else if (
    data.code === "out_of_allowance" ||
    data.code === "free_quota_exhausted" ||
    data.code === "insufficient_credits"
  ) {
    const link = el("a", "btn btn-primary btn-small") as HTMLAnchorElement;
    link.href = "/pricing";
    link.textContent =
      data.code === "insufficient_credits" ? "Add credits" : "See plans";
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
  state.sources = [];
  state.activeSource = null;
  state.samples = [];
  state.suggestions = [];
  state.ran = false;
  trace.reset();
  render();
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

/**
 * The renderer, on its own. The chart benchmark draws every form through this
 * rather than through a page of its own, so what it screenshots is the bundle,
 * the theme tokens and the CSS a user gets - a harness with its own copy of
 * Plotly would pass while the product was broken.
 */
/**
 * The whole answer panel - chart, controls, warnings, export row - fed a
 * result directly. The visual benchmark uses it to photograph every chart
 * form inside the real card at real viewport widths, which is where clipping,
 * wrapping and contrast problems actually live; a bare figure in a bare div
 * does not have a card to overflow.
 */
(window as unknown as Record<string, unknown>).__thShowResult = async (
  result: PipelineResult,
) => {
  state.lastResult = result;
  state.ran = true;
  render();
  await chart.show(result);
};

(window as unknown as Record<string, unknown>).__thRenderFigure = (
  host: HTMLElement,
  figure: PlotlyFigure,
) => renderFigure(host, figure);

function askWith(question: string, extra: Record<string, unknown>): void {
  state.lastQuestion = question;
  state.ran = true;
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

/** Open the chart builder on a dataset or sample. */
(window as unknown as Record<string, unknown>).__thBuilder = async (
  source: Record<string, unknown>,
) => {
  await openBuilder(source as never);
  return true;
};

/** Switch to the builder view on a data source. */
async function openBuilder(source: Record<string, unknown>): Promise<Builder> {
  const builder = new Builder();
  state.builder = builder;
  state.view = "builder";
  // Stash the instance rather than returning it through evaluate(): a Builder
  // is not structured-cloneable.
  (window as unknown as Record<string, unknown>).__thBuilderInstance = builder;
  render();
  await builder.open(source as never);
  return builder;
}

/** Replace the plan and re-preview. Used by the visual bench and e2e tests. */
(window as unknown as Record<string, unknown>).__thSetPlan = async (plan: unknown) => {
  const builder = (window as unknown as Record<string, any>).__thBuilderInstance;
  if (!builder) return false;
  await builder.setPlan(plan);
  return true;
};

/** Open a dashboard by id, or a shared one by token. */
(window as unknown as Record<string, unknown>).__thDashboard = async (
  idOrToken: string,
  shared = false,
) => {
  await openDashboard(idOrToken, shared);
  return true;
};

let list: DashboardListView | null = null;

function dashboardList(): DashboardListView {
  list ??= new DashboardListView({ onOpen: (id) => void openDashboard(id) });
  return list;
}

async function openDashboardList(): Promise<void> {
  state.view = "dashboards";
  state.dashboard = null;
  render();
  await dashboardList().load();
}

/**
 * Put the chart that is on screen onto a dashboard.
 *
 * The chart already exists server-side by the time it is drawn, so this is an
 * attach rather than a re-run: no second model call, no second charge for the
 * same answer.
 */
async function pinToDashboard(result: PipelineResult): Promise<void> {
  if (!state.user?.signed_in) {
    showError({ code: "signin_required", message: "Sign in to keep charts on a dashboard." });
    return;
  }
  if (!result.chart_id) {
    showError({ code: "not_saved", message: "This chart was not saved, so it cannot be pinned." });
    return;
  }

  try {
    const payload = await api.get<{ dashboards: { id: string; title: string }[] }>(
      "/v1/dashboards",
    );
    const existing = payload.dashboards ?? [];
    // One dashboard is not a choice, and none is not a question - it is a
    // dashboard waiting to be made.
    const target = existing.length
      ? await chooseDashboard(existing)
      : await api.post<{ id: string }>("/v1/dashboards", { title: state.lastQuestion.slice(0, 80) });
    if (!target) return;

    await api.post(`/v1/dashboards/${target.id}/charts`, { chart_id: result.chart_id, w: 1 });
    await openDashboard(target.id);
  } catch (error) {
    showError({ code: (error as ApiError).code, message: (error as Error).message });
  }
}

/** Which board. A sheet, because a browser prompt cannot list anything. */
function chooseDashboard(
  options: { id: string; title: string }[],
): Promise<{ id: string } | null> {
  return new Promise((resolve) => {
    const sheet = document.createElement("dialog");
    sheet.className = "pick-sheet";
    const heading = el("h2");
    heading.textContent = "Add to which dashboard?";
    const list = el("ul", "pick-list");

    const close = (value: { id: string } | null) => {
      sheet.close();
      sheet.remove();
      resolve(value);
    };

    for (const option of options) {
      const item = el("li");
      const node = el("button", "pick-item") as HTMLButtonElement;
      node.type = "button";
      node.textContent = option.title || "Untitled dashboard";
      node.addEventListener("click", () => close({ id: option.id }));
      item.append(node);
      list.append(item);
    }

    const fresh = el("li");
    const create = el("button", "pick-item is-new") as HTMLButtonElement;
    create.type = "button";
    create.textContent = "New dashboard";
    create.addEventListener("click", async () => {
      try {
        const made = await api.post<{ id: string }>("/v1/dashboards", {
          title: state.lastQuestion.slice(0, 80) || "Untitled dashboard",
        });
        close({ id: made.id });
      } catch {
        close(null);
      }
    });
    fresh.append(create);
    list.append(fresh);

    const cancel = button("Cancel", () => close(null));
    sheet.append(heading, list, cancel);
    sheet.addEventListener("cancel", () => close(null));
    document.body.append(sheet);
    sheet.showModal();
  });
}

async function openDashboard(idOrToken: string, shared = false): Promise<DashboardView> {
  const view = new DashboardView({ readOnly: shared });
  state.dashboard = view;
  state.builder = null;
  state.view = "dashboard";
  (window as unknown as Record<string, unknown>).__thDashboardInstance = view;
  render();
  if (shared) await view.openShared(idOrToken);
  else await view.open(idOrToken);
  return view;
}

export { spinner };
