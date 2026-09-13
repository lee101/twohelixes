/**
 * App shell: sign-in, the ask box, the live trace, the chart, and the SQL tab.
 *
 * Deliberately framework-free. The dynamic surface is small — one chart, one
 * trace, one editor — and a runtime would cost more than it saves here.
 */

import { ApiError, api, stream, type ChartConfig, type PipelineResult, type PlotlyFigure, type User } from "./api";
import { identify, track } from "./analytics";
import { ChartView, button, el, renderFigure } from "./chart";
import { logo, spinner } from "./helix";
import { Builder } from "./builder";
import { DashboardView } from "./dashboard";
import { DashboardListView } from "./dashboards";
import { SourcesPanel, type DatasetSummary, type UploadResult } from "./sources";
import { TraceView } from "./trace";
import type { SQLWorkbench } from "./sql-workbench";
import type { SheetsView } from "./sheets";
import { clearUser, persistUser } from "./user";

interface Sample {
  key: string;
  name: string;
  description: string;
  questions: string[];
  rows: number;
}

type View = "chat" | "builder" | "dashboard" | "dashboards" | "sql" | "sheets";

interface State {
  view: View;
  builder: Builder | null;
  dashboard: DashboardView | null;
  sql: SQLWorkbench | null;
  sheets: SheetsView | null;
  user: User | null;
  lastResult: PipelineResult | null;
  lastQuestion: string;
  sources: { id: string; name: string; kind: string; supports_sql: boolean }[];
  datasets: DatasetSummary[];
  activeSource: string | null;
  activeDataset: string | null;
  /** The sample a signed-out visitor is trying. The only data the trial can
   *  reach, so it is sent by key rather than attached to an account. */
  activeSample: string | null;
  cancel: (() => void) | null;
  samples: Sample[];
  suggestions: string[];
  ran: boolean;
  /** The visitor asked for the sign-in form before spending the trial. */
  showSignIn: boolean;
  /** Team invite carried through account creation/sign-in. */
  pendingJoin: string | null;
}

const state: State = {
  view: "chat",
  builder: null,
  dashboard: null,
  sql: null,
  sheets: null,
  user: null,
  lastResult: null,
  lastQuestion: "",
  sources: [],
  datasets: [],
  activeSource: null,
  activeDataset: null,
  activeSample: null,
  cancel: null,
  samples: [],
  suggestions: [],
  ran: false,
  showSignIn: false,
  pendingJoin: null,
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
installGlobalFileDrop();

async function boot(): Promise<void> {
  const sharedToken = root.dataset.share;
  const sharedKind = root.dataset.shareKind;
  if (sharedToken) {
    state.user = null;
    if (sharedKind === "dashboard") {
      await openDashboard(sharedToken, true);
      return;
    }
    if (sharedKind === "chart") {
      const payload = await api.get<{
        object: { id: string; title: string; spec: PlotlyFigure; graph_args: ChartConfig };
      }>(`/v1/shared/${sharedToken}?mode=${prefersDark() ? "dark" : "light"}`);
      const sharedChart = new ChartView();
      const main = el("main", "app-main shared-chart-page");
      main.append(sharedChart.root);
      root.replaceChildren(header(), main);
      await sharedChart.show({
        figure: payload.object.spec,
        config: payload.object.graph_args ?? {},
        preview: [],
        columns: [],
        row_count: 0,
        warnings: [],
        audit: [],
        transform_code: "",
        elapsed_ms: 0,
      });
      return;
    }
  }

  try {
    state.user = await api.me();
    identify(state.user.user_id);
    if (state.user?.signed_in) persistUser(state.user);
    else clearUser();
  } catch {
    state.user = null;
    clearUser();
  }

  // A dataset page links here with the question it just showed. Arriving with
  // an empty ask box after clicking "ask this yourself" loses the thread the
  // visitor was following, and they have to retype what they just read.
  const params = new URLSearchParams(window.location.search);
  const sample = params.get("sample");
  const question = params.get("q");
  state.pendingJoin = params.get("join");
  if (sample) state.activeSample = sample;
  // Through state, not through the DOM: loadSources() and loadSamples() both
  // re-render, and a value written straight onto the textarea is gone by the
  // time either of them lands.
  if (question) state.lastQuestion = question;

  const joined = state.user?.signed_in ? await acceptPendingInvite() : false;
  if (state.pendingJoin && !state.user?.signed_in) state.showSignIn = true;
  if (joined) state.view = "dashboards";
  render();
  if (joined) void dashboardList().load();
  else if (state.user?.signed_in) void loadSources();
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
    const account = el("a", "btn btn-ghost") as HTMLAnchorElement;
    account.href = "/account";
    account.textContent = "Account";
    right.append(themeToggle(), account);
  } else {
    // A visitor who has decided to sign up should not have to spend the trial
    // first to find the form.
    right.append(
      themeToggle(),
      button("Sign in", () => {
        track("sign_in_started", { surface: "app_header" });
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
    { label: "SQL", view: "sql", go: () => void openSqlWorkbench() },
    { label: "Sheets", view: "sheets", go: () => void openSheets() },
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
  if (!state.user?.signed_in && state.showSignIn) {
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
  if (state.view === "sql" && state.sql) {
    wrap.classList.add("app-main-wide");
    wrap.append(state.sql.root);
    return wrap;
  }
  if (state.view === "sheets" && state.sheets) {
    wrap.classList.add("app-main-wide");
    wrap.append(state.sheets.root);
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
  if (!state.user?.signed_in && state.lastResult) {
    const invite = el("div", "card");
    const note = el("p");
    note.textContent = "Your first answer is ready. Create a free account to keep exploring.";
    invite.append(note, button("Keep exploring — create account", () => {
      state.showSignIn = true;
      render();
    }));
    right.append(invite);
  }

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
  lede.textContent = state.user?.signed_in
    ? "Nothing connected yet. Load one of these and ask it something — or connect your own source from the picker above."
    : `Ask one question now without an account. Sign in for ${state.user?.free_queries_total || 10} free charts a month — no card, and the sample data is already loaded.`;

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
    "Sign in once and this browser stays signed in until you sign out.";

  let mode: "login" | "signup" | "forgot" = "signup";

  const form = el("form", "signin-form") as HTMLFormElement;
  const input = document.createElement("input");
  input.type = "email";
  input.required = true;
  input.placeholder = "you@company.com";
  input.className = "signin-input";
  input.autocomplete = "email";

  const password = document.createElement("input");
  password.type = "password";
  password.required = true;
  password.minLength = 6;
  password.maxLength = 1024;
  password.placeholder = "Password (at least 6 characters)";
  password.className = "signin-input";
  password.autocomplete = "new-password";

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "btn btn-primary";
  submit.textContent = "Create account";

  const switcher = el("p", "note");
  const syncMode = (): void => {
    if (mode === "signup") {
      lede.textContent = `${state.user?.free_queries_total || 10} free charts a month — no card. The sample data is already loaded.`;
      password.hidden = false;
      password.required = true;
      password.autocomplete = "new-password";
      submit.textContent = "Create account";
      switcher.innerHTML =
        'Already have an account? <a href="#" data-mode="login">Sign in</a>';
    } else if (mode === "forgot") {
      heading.textContent = "Forgot password";
      lede.textContent =
        "Enter your email and we will send a reset link if an account exists.";
      password.hidden = true;
      password.required = false;
      submit.textContent = "Send reset link";
      switcher.innerHTML = '<a href="#" data-mode="login">Back to sign in</a>';
    } else {
      heading.textContent = prompt || "Sign in to start asking";
      lede.textContent =
        "Sign in once and this browser stays signed in until you sign out.";
      password.hidden = false;
      password.required = true;
      password.autocomplete = "current-password";
      submit.textContent = "Sign in";
      switcher.innerHTML =
        '<a href="#" data-mode="forgot">Forgot password?</a> · ' +
        '<a href="#" data-mode="signup">Create an account</a>';
    }
  };
  switcher.addEventListener("click", (event) => {
    const link = (event.target as HTMLElement).closest<HTMLElement>("[data-mode]");
    if (!link) return;
    event.preventDefault();
    mode = link.dataset.mode as typeof mode;
    syncMode();
  });
  syncMode();

  const error = el("p", "note note-warn");
  error.hidden = true;

  form.append(input, password, submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    error.hidden = true;
    try {
      if (mode === "forgot") {
        const result = await api.forgotPassword(input.value.trim());
        error.hidden = false;
        error.className = "note";
        error.textContent =
          result.detail ||
          "If an account exists for that email, a reset link is on its way.";
        if (result.reset_url) {
          error.innerHTML =
            `${error.textContent} <a href="${result.reset_url}">Open reset link</a>`;
        }
        mode = "login";
        syncMode();
        return;
      }
      state.user =
        mode === "signup"
          ? await api.signUp(input.value.trim(), password.value)
          : await api.signIn(input.value.trim(), password.value);
      persistUser(state.user);
      const joined = await acceptPendingInvite();
      if (joined) state.view = "dashboards";
      render();
      identify(state.user.user_id);
      track("sign_in_completed", { surface: "app" });
      if (joined) void dashboardList().load();
      else void loadSources();
    } catch (exc) {
      error.hidden = false;
      error.className = "note note-warn";
      error.textContent = (exc as Error).message;
      if (mode === "signup" && /already exists/i.test(error.textContent)) {
        mode = "login";
        syncMode();
      }
    } finally {
      submit.disabled = false;
    }
  });

  panel.append(heading, lede, form, switcher, error);
  return panel;
}

async function acceptPendingInvite(): Promise<boolean> {
  const token = state.pendingJoin;
  if (!token) return false;
  try {
    await api.post(`/v1/teams/join/${encodeURIComponent(token)}`);
    state.pendingJoin = null;
    history.replaceState(null, "", "/app");
    return true;
  } catch (error) {
    // Keep the form usable, but do not keep retrying a dead or mismatched
    // invite on every render. The user is signed in and can request a fresh
    // invitation from their teammate.
    state.pendingJoin = null;
    history.replaceState(null, "", "/app");
    window.setTimeout(
      () => showError({ code: (error as ApiError).code, message: (error as Error).message }),
      0,
    );
    return false;
  }
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

  const dropHint = el("span", "ask-upload-hint");
  dropHint.textContent = "or drop a file anywhere";

  row.append(
    sourcePicker(),
    sourcesPanel().uploadButton(),
    dropHint,
    sourcesPanel().button(),
    submit,
    stop,
  );
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
  none.textContent = state.datasets.length
    ? `Auto-detect from ${state.datasets.length} dataset${state.datasets.length === 1 ? "" : "s"}`
    : state.sources.length
      ? "Choose a dataset or source"
      : "No datasets yet";
  node.append(none);

  for (const dataset of state.datasets) {
    const item = document.createElement("option");
    item.value = `dataset:${dataset.id}`;
    item.textContent = `${dataset.name} (${dataset.row_count.toLocaleString()} rows)`;
    if (dataset.id === state.activeDataset) item.selected = true;
    node.append(item);
  }

  for (const source of state.sources) {
    const item = document.createElement("option");
    item.value = `source:${source.id}`;
    item.textContent = `${source.name} (${source.kind})`;
    if (source.id === state.activeSource) item.selected = true;
    node.append(item);
  }
  node.addEventListener("change", () => {
    const [kind, id] = node.value.split(":", 2);
    state.activeSample = null;
    state.activeDataset = kind === "dataset" ? id : null;
    state.activeSource = kind === "source" ? id : null;
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
    onChange: (sources, datasets, selected) => {
      state.sources = sources;
      state.datasets = datasets;
      state.sql?.setSources(sources);
      if (state.activeDataset && !datasets.some((item) => item.id === state.activeDataset)) {
        state.activeDataset = null;
      }
      if (state.activeSource && !sources.some((item) => item.id === state.activeSource)) {
        state.activeSource = null;
      }
      // Whatever was just added is almost certainly what the next question is
      // about, so select it rather than making the user find it in the picker.
      if (selected?.type === "source") {
        state.activeSample = null;
        state.activeSource = selected.id;
        state.activeDataset = null;
      } else if (selected?.type === "dataset") {
        state.activeSample = null;
        state.activeDataset = selected.id;
        state.activeSource = null;
      }
      render();
    },
    onUploaded: (datasets) => void buildDashboardForUploads(datasets),
  });
  return panel;
}

function installGlobalFileDrop(): void {
  const overlay = el("div", "global-drop-overlay");
  overlay.setAttribute("aria-hidden", "true");
  const card = el("div", "global-drop-card");
  const title = el("strong");
  title.textContent = "Drop files to upload";
  const detail = el("span");
  detail.textContent = "We’ll build a dashboard automatically.";
  card.append(title, detail);
  overlay.append(card);
  document.body.append(overlay);

  let dragDepth = 0;
  const hasFiles = (event: DragEvent): boolean =>
    [...(event.dataTransfer?.types ?? [])].includes("Files");
  const hide = (): void => {
    dragDepth = 0;
    overlay.classList.remove("is-visible");
    overlay.setAttribute("aria-hidden", "true");
  };

  document.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    if (!state.user?.signed_in) return;
    dragDepth += 1;
    overlay.classList.add("is-visible");
    overlay.setAttribute("aria-hidden", "false");
  });
  document.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  });
  document.addEventListener("dragleave", (event) => {
    if (event.relatedTarget === null) {
      hide();
      return;
    }
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) hide();
  });
  document.addEventListener("drop", (event) => {
    const alreadyHandled = event.defaultPrevented;
    const files = [...(event.dataTransfer?.files ?? [])];
    hide();
    if (!files.length) return;
    event.preventDefault();
    if (alreadyHandled) return;
    if (!state.user?.signed_in) {
      showError({
        code: "signin_required",
        message: "Sign in before uploading this file so it can stay in your workspace.",
      });
      return;
    }
    void sourcesPanel().acceptFiles(files);
  });
  window.addEventListener("dragend", hide);
}

function buildDashboardForUploads(datasets: UploadResult[]): void {
  const names = datasets.map((dataset) => dataset.name);
  const label = names.length === 1 ? names[0] : `${names.length} uploaded files`;
  const banner = el("div", "banner banner-progress");
  const icon = spinner(18);
  const text = el("span");
  text.textContent = `Uploaded ${label}. Building its dashboard…`;
  banner.append(icon, text);

  // The progress sheet has done its job once the upload lands. Put the user
  // back in the app while the dashboard tiles are being generated.
  sourcesPanel().dismiss();
  root.prepend(banner);

  const goal = names.length === 1
    ? `Build a useful overview dashboard for the newly uploaded dataset “${names[0]}”. ` +
      "Show the most informative headline metrics, trends, breakdowns, and notable patterns."
    : `Build one useful overview dashboard for these newly uploaded datasets: ${names.join(", ")}. ` +
      "Use them together when their schemas are meaningfully related; otherwise show the clearest " +
      "headline metrics, trends, and breakdowns across the files.";

  let dashboardId = "";
  let finished = false;
  stream(
    "/v1/dashboard/stream",
    {
      goal,
      dataset_ids: datasets.map((dataset) => dataset.dataset_id),
      mode: prefersDark() ? "dark" : "light",
    },
    (event, data) => {
      if (event === "dashboard") {
        dashboardId = String(data.id ?? "");
        text.textContent = `Building ${data.title || `${label} dashboard`}…`;
      } else if (event === "tile") {
        text.textContent = `Building ${label} dashboard… adding charts`;
      } else if (event === "complete") {
        if (finished) return;
        finished = true;
        banner.remove();
        const id = String(data.dashboard_id ?? dashboardId);
        if (id) {
          void openDashboard(id).catch((error) => showError({
            message: `The dashboard was built, but could not be opened: ${String(error)}`,
          }));
        }
      } else if (event === "error") {
        if (finished) return;
        finished = true;
        banner.remove();
        showError({
          code: data.code,
          message: `Uploaded ${label}, but the automatic dashboard could not be built. ` +
            `${data.message ?? data.code ?? "Please try again."}`,
        });
      }
    },
  );
}

async function loadSources(): Promise<void> {
  try {
    const [sourcePayload, datasetPayload] = await Promise.all([
      api.get<{ sources: State["sources"] }>("/v1/sources"),
      api.get<{ datasets: DatasetSummary[] }>("/v1/datasets"),
    ]);
    state.sources = sourcePayload.sources ?? [];
    state.datasets = datasetPayload.datasets ?? [];
    sourcesPanel().sync(state.sources, state.datasets);
    state.sql?.setSources(state.sources);
    // Not when a sample was named in the URL: someone who arrived from a
    // dataset page asked about that dataset, not about whatever source they
    // happen to have connected.
    if (
      !state.activeSource &&
      !state.activeSample &&
      !state.datasets.length &&
      state.sources.length === 1
    ) {
      // One source is not a choice. Pre-selecting it removes a step that only
      // ever has one right answer.
      state.activeSource = state.sources[0].id;
    }
    render();
    if (!state.sources.length && !state.datasets.length && !state.samples.length) {
      void loadSamples();
    }
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
  else if (state.activeDataset) body.dataset_ids = [state.activeDataset];
  else if (state.activeSample) body.sample = state.activeSample;

  track("query_started", {
    surface: "app",
    mode: edit ? "edit" : "new",
    sample: state.activeSample ?? "",
    source: state.activeSource ?? "",
  });

  state.cancel = stream("/v1/query/stream", body, (event, data) => {
    trace.handle(event, data);

    if (event === "result") {
      state.lastResult = data as PipelineResult;
      track("query_completed", { surface: "app", chart: Boolean(state.lastResult.chart_id) });
      // Keep the result mounted for the anonymous trial; invite signup below
      // it instead of replacing the chart with a form before it can be seen.
      if (!state.user?.signed_in) render();
      void chart.show(state.lastResult).then(revealChart);
    } else if (event === "user") {
      state.user = data as User;
      identify(state.user.user_id);
      persistUser(state.user);
      root.replaceChild(header(), root.firstChild!);
    } else if (event === "done") {
      state.cancel = null;
    } else if (event === "error") {
      track("query_failed", { surface: "app" });
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

function showError(data: { code?: string; message?: string; candidates?: string[] }): void {
  const banner = el("div", "banner banner-error");
  const text = el("span");
  text.textContent = data.message ?? data.code ?? "Something went wrong";
  if (data.candidates?.length) {
    text.textContent += ` Closest datasets: ${data.candidates.join(", ")}.`;
  }
  banner.append(text);

  // A wall that only says "no" is a wall. Each refusal carries the one action
  // that clears it, and the two are different actions: a visitor who has used
  // the trial needs an account, not a price list.
  if (data.code === "signin_required" || data.code === "trial_used") {
    banner.append(
      button("Sign in — free", () => {
        state.ran = false;
        state.showSignIn = true;
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
  } else if (
    data.code === "no_datasets" ||
    data.code === "dataset_not_selected" ||
    data.code === "dataset_not_found" ||
    data.code === "source_query_required"
  ) {
    banner.append(button("Open Library", () => sourcesPanel().button().click()));
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
  track("sign_out", { surface: "app" });
  identify(null);
  state.user = null;
  clearUser();
  state.view = "chat";
  state.sql = null;
  state.sheets = null;
  state.lastResult = null;
  state.sources = [];
  state.datasets = [];
  state.activeSource = null;
  state.activeDataset = null;
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

  track("query_started", { surface: "app_test_hook", mode: "new" });

  state.cancel = stream("/v1/query/stream", body, (event, data) => {
    trace.handle(event, data);
    if (event === "result") {
      state.lastResult = data as PipelineResult;
      track("query_completed", { surface: "app_test_hook", chart: Boolean(state.lastResult.chart_id) });
      void chart.show(state.lastResult);
    } else if (event === "done" || event === "error") {
      if (event === "error") track("query_failed", { surface: "app_test_hook" });
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

async function openSqlWorkbench(): Promise<void> {
  if (!state.sql) {
    const { SQLWorkbench } = await import("./sql-workbench");
    state.sql = new SQLWorkbench(state.sources);
  } else {
    state.sql.setSources(state.sources);
  }
  state.view = "sql";
  render();
}

async function openSheets(): Promise<void> {
  if (!state.sheets) {
    const { SheetsView } = await import("./sheets");
    state.sheets = new SheetsView();
  }
  state.view = "sheets";
  render();
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
    const payload = await api.get<{
      dashboards: { id: string; title: string; can_edit?: boolean }[];
    }>("/v1/dashboards");
    const existing = (payload.dashboards ?? []).filter(
      (dashboard) => dashboard.can_edit !== false,
    );
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
