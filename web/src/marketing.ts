/**
 * Marketing-page interactions: sign in and buy without leaving the page.
 *
 * Deliberately tiny and dependency-free. It ships on every marketing page, so
 * every kilobyte here is paid by someone who has not signed up yet. Stripe.js
 * is loaded lazily, only when a purchase actually starts.
 *
 * Both flows degrade: with JavaScript off or Stripe.js blocked, the buttons
 * are still ordinary links to /app and the hosted checkout still works.
 */

import { clearUser, persistUser, type UserData } from "./user";
import { identify, track } from "./analytics";

interface Me extends UserData {}

type AuthMode = "login" | "signup" | "forgot";

let cachedMe: Me | null = null;
let stripePromise: Promise<any> | null = null;
let authMode: AuthMode = "login";

const $ = <T extends HTMLElement>(sel: string): T | null =>
  document.querySelector<T>(sel);

// Fixed placement labels only: never collect the visitor's question or data.
// The existing tracker respects privacy signals and handles beacon delivery.
document.addEventListener("click", (event) => {
  const link = (event.target as Element | null)?.closest<HTMLElement>("[data-conversion]");
  if (!link) return;
  try {
    const tracker = (window as Window & {
      th?: (command: string, name: string, props: Record<string, string>) => void;
    }).th;
    tracker?.("track", "landing_cta_clicked", {
      placement: link.dataset.conversion || "unknown",
      variant: "helix-intelligence-v1",
    });
  } catch {
    // Analytics must never interfere with navigation or sign-up.
  }
});

async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new Error(payload?.detail || payload?.error || `HTTP ${response.status}`);
  }
  return payload as T;
}

async function me(force = false): Promise<Me> {
  if (cachedMe && !force) return cachedMe;
  try {
    cachedMe = await api<Me>("/v1/me");
    if (cachedMe.signed_in) persistUser(cachedMe);
    else clearUser();
  } catch {
    cachedMe = {
      signed_in: false,
      id: "",
      email: "",
      plan: "anon",
      paid: false,
      api_credits: 0,
      free_queries_left: 0,
      free_queries_total: 0,
      free_queries_used: 0,
      is_admin: false,
      is_subscribed: false,
    };
    clearUser();
  }
  return cachedMe;
}

// --------------------------------------------------------------------------
// Shared stylesheet — marketing inlines the server design system; this file
// carries the pastel token overrides so marketing and the app stay aligned.
// --------------------------------------------------------------------------

function ensureSharedStyles(): void {
  if (document.querySelector('link[href="/static/styles.css"]')) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/styles.css";
  document.head.append(link);
}

// --------------------------------------------------------------------------
// Overlay plumbing
// --------------------------------------------------------------------------

function overlay(id: string): HTMLElement | null {
  return document.getElementById(id);
}

let lastFocused: Element | null = null;

function open(id: string): void {
  const node = overlay(id);
  if (!node) return;
  lastFocused = document.activeElement;
  node.setAttribute("open", "");
  document.body.style.overflow = "hidden";
  node.querySelector<HTMLElement>("input,button")?.focus();
}

function close(id: string): void {
  const node = overlay(id);
  if (!node) return;
  node.removeAttribute("open");
  document.body.style.overflow = "";
  (lastFocused as HTMLElement | null)?.focus?.();
}

function closeAll(): void {
  for (const node of document.querySelectorAll(".overlay[open]")) {
    node.removeAttribute("open");
  }
  document.body.style.overflow = "";
}

// --------------------------------------------------------------------------
// Sign in
// --------------------------------------------------------------------------

/** Resolves when the user is signed in, or rejects if they dismiss it. */
function requireSignIn(reason = "", mode: AuthMode = "login"): Promise<Me> {
  track("sign_in_started", { surface: "marketing", reason: reason ? "gated_action" : "cta" });
  return new Promise((resolve, reject) => {
    const note = $("#signin-reason");
    if (note && reason) note.textContent = reason;

    pendingAuth = { resolve, reject };
    setAuthMode(mode);
    open("signin-overlay");
  });
}

let pendingAuth: { resolve: (m: Me) => void; reject: (e: Error) => void } | null = null;

function setAuthMode(mode: AuthMode): void {
  authMode = mode;
  const overlay = $("#signin-overlay");
  const title = $("#signin-title");
  const reason = $("#signin-reason");
  const submit = $<HTMLButtonElement>("#signin-submit");
  const passwordField = document.querySelector<HTMLElement>("[data-auth-field=password]");
  const password = $<HTMLInputElement>("#signin-password");
  const switchNote = $("#signin-switch");
  overlay?.setAttribute("data-mode", mode);

  if (mode === "signup") {
    if (title) title.textContent = "Create account";
    if (reason)
      reason.textContent = `No card — ${overlay?.dataset.freeCharts || "10"} free charts a month, sample data already loaded.`;
    if (submit) submit.textContent = "Create account";
    if (passwordField) passwordField.hidden = false;
    if (password) {
      password.required = true;
      password.autocomplete = "new-password";
    }
    if (switchNote) {
      switchNote.innerHTML =
        'Already have an account? <a href="#" data-auth-mode="login">Sign in</a>';
    }
  } else if (mode === "forgot") {
    if (title) title.textContent = "Forgot password";
    if (reason) {
      reason.textContent =
        "Enter your email and we will send a reset link if an account exists.";
    }
    if (submit) submit.textContent = "Send reset link";
    if (passwordField) passwordField.hidden = true;
    if (password) password.required = false;
    if (switchNote) {
      switchNote.innerHTML =
        '<a href="#" data-auth-mode="login">Back to sign in</a>';
    }
  } else {
    if (title) title.textContent = "Sign in";
    if (reason) {
      reason.textContent =
        "Sign in to keep your dashboards and collaborate with your team.";
    }
    if (submit) submit.textContent = "Sign in";
    if (passwordField) passwordField.hidden = false;
    if (password) {
      password.required = true;
      password.autocomplete = "current-password";
    }
    if (switchNote) {
      switchNote.innerHTML =
        '<a href="#" data-auth-mode="forgot">Forgot password?</a> · New here? ' +
        '<a href="#" data-auth-mode="signup">Create an account</a>';
    }
  }
}

function wireSignIn(): void {
  const form = $<HTMLFormElement>("#signin-form");
  if (!form) return;

  document.addEventListener("click", (event) => {
    const link = (event.target as HTMLElement).closest<HTMLElement>("[data-auth-mode]");
    if (!link) return;
    event.preventDefault();
    const mode = link.dataset.authMode as AuthMode;
    if (mode === "login" || mode === "signup" || mode === "forgot") setAuthMode(mode);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = form.querySelector<HTMLInputElement>("input[type=email]");
    const password = form.querySelector<HTMLInputElement>("input[type=password]");
    const submit = form.querySelector<HTMLButtonElement>("button[type=submit]");
    const error = $("#signin-error");
    if (!input || !input.value.trim()) return;
    if (authMode !== "forgot" && (!password || !password.value)) return;

    if (submit) {
      submit.disabled = true;
      submit.textContent =
        authMode === "forgot"
          ? "Sending…"
          : authMode === "signup"
            ? "Creating…"
            : "Signing in…";
    }
    if (error) error.textContent = "";

    try {
      if (authMode === "forgot") {
        const result = await api<{ detail?: string; reset_url?: string }>(
          "/v1/auth/forgot-password",
          { email: input.value.trim() },
        );
        if (error) error.textContent = "";
        const note = $("#signin-reason");
        if (note) {
          note.textContent =
            result.detail ||
            "If an account exists for that email, a reset link is on its way.";
        }
        if (result.reset_url) {
          // Dev-only: SMTP unset. Keep the link usable without leaving the sheet.
          if (note) {
            note.innerHTML =
              `${note.textContent} <a href="${result.reset_url}">Open reset link</a>`;
          }
        }
        setAuthMode("login");
        return;
      }

      const path = authMode === "signup" ? "/v1/auth/signup" : "/v1/auth/signin";
      const user = await api<Me>(path, {
        email: input.value.trim(),
        password: password!.value,
      });
      cachedMe = user;
      persistUser(user);
      close("signin-overlay");
      identify(user.user_id);
      track("sign_in_completed", { surface: "marketing" });
      refreshHeader(user);
      pendingAuth?.resolve(user);
      pendingAuth = null;
    } catch (exc) {
      const message = (exc as Error).message;
      if (error) error.textContent = message;
      if (authMode === "signup" && /already exists/i.test(message)) {
        setAuthMode("login");
      }
    } finally {
      if (submit) {
        submit.disabled = false;
        submit.textContent =
          authMode === "forgot"
            ? "Send reset link"
            : authMode === "signup"
              ? "Create account"
              : "Sign in";
      }
    }
  });
}

/** Swap the header CTA once we know who this is. */
function refreshHeader(user: Me): void {
  const cta = $<HTMLAnchorElement>("#header-cta");
  if (cta) {
    if (user.signed_in) {
      cta.textContent = "Account";
      // setAttribute: bun's minify has turned `cta.href = ...` + a following
      // statement into a bogus `cta.href(...)` call, which throws and aborts
      // the rest of the sign-in success path (including navigation to /app).
      cta.setAttribute("href", "/account");
      cta.removeAttribute("data-action");
    } else {
      cta.textContent = "Start free";
      cta.setAttribute("href", "/app");
      cta.dataset.action = "signin";
    }
  }
  const badge = $("#header-badge");
  if (badge && user.signed_in) {
    badge.textContent = user.paid
      ? `${user.api_credits.toLocaleString()} credits`
      : `${user.free_queries_left} free left`;
    badge.hidden = false;
  } else if (badge) {
    badge.hidden = true;
  }
}

function wireAccount(): void {
  const logout = $<HTMLButtonElement>("#account-logout");
  if (logout) {
    logout.addEventListener("click", async () => {
      logout.disabled = true;
      try {
        await api("/v1/auth/signout", {});
      } catch {
        /* cookie clear is best-effort */
      }
      clearUser();
      cachedMe = null;
      window.location.href = "/";
    });
  }

  const form = $<HTMLFormElement>("#reset-form");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const token = form.dataset.token || "";
    const email = $<HTMLInputElement>("#reset-email");
    const password = $<HTMLInputElement>("#reset-password");
    const error = $("#reset-error");
    const ok = $("#reset-ok");
    const submit = form.querySelector<HTMLButtonElement>("button[type=submit]");
    if (error) error.textContent = "";
    if (ok) ok.hidden = true;
    if (submit) submit.disabled = true;
    try {
      if (token) {
        if (!password?.value) return;
        const user = await api<Me>("/v1/auth/reset-password", {
          token,
          password: password.value,
        });
        persistUser(user);
        window.location.href = "/account";
        return;
      }
      if (!email?.value.trim()) return;
      const result = await api<{ detail?: string; reset_url?: string }>(
        "/v1/auth/forgot-password",
        { email: email.value.trim() },
      );
      if (ok) {
        ok.hidden = false;
        ok.textContent =
          result.detail ||
          "If an account exists for that email, a reset link is on its way.";
        if (result.reset_url) {
          ok.innerHTML =
            `${ok.textContent} <a href="${result.reset_url}">Open reset link</a>`;
        }
      }
    } catch (exc) {
      if (error) error.textContent = (exc as Error).message;
    } finally {
      if (submit) submit.disabled = false;
    }
  });
}

// --------------------------------------------------------------------------
// Checkout, embedded in the page
// --------------------------------------------------------------------------

function loadStripe(publishableKey: string): Promise<any> {
  if (stripePromise) return stripePromise;

  stripePromise = new Promise((resolve, reject) => {
    const existing = (window as any).Stripe;
    if (existing) return resolve(existing(publishableKey));

    const script = document.createElement("script");
    script.src = "https://js.stripe.com/v3/";
    script.async = true;
    script.onload = () => {
      const factory = (window as any).Stripe;
      factory ? resolve(factory(publishableKey)) : reject(new Error("Stripe.js unavailable"));
    };
    script.onerror = () => reject(new Error("Stripe.js failed to load"));
    document.head.append(script);
  });
  return stripePromise;
}

let mountedCheckout: any = null;

async function startCheckout(opts: {
  pack?: string;
  priceId?: string;
  plan?: string;
}): Promise<void> {
  const user = await me(true);
  if (!user.signed_in) {
    // Buying requires an account, so collect it inline rather than bouncing
    // the buyer to a sign-in page and losing the purchase.
    await requireSignIn("Sign in to continue — it takes a moment.", "signup");
  }

  track("checkout_started", {
    surface: "marketing",
    pack: opts.pack ?? "",
    plan: opts.plan ?? "",
  });

  const key = document.body.dataset.stripeKey || "";
  const mount = $("#checkout-mount");
  const status = $("#checkout-status");
  if (!mount) return;

  open("checkout-overlay");
  mount.innerHTML = "";
  if (status) status.textContent = "Preparing secure checkout…";

  let session: { mode: string; client_secret?: string; url?: string };
  try {
    session = await api("/v1/billing/checkout", {
      pack: opts.pack,
      // The plan, not the price: the server owns the mapping, so a price
      // change does not need a redeploy of this page.
      plan: opts.plan,
      price_id: opts.priceId,
      embedded: Boolean(key),
    });
  } catch (exc) {
    if (status) status.textContent = `Could not start checkout: ${(exc as Error).message}`;
    return;
  }

  // No publishable key, or the server chose hosted mode: redirect.
  if (session.mode !== "embedded" || !session.client_secret) {
    if (session.url) {
      window.location.href = session.url;
      return;
    }
    if (status) status.textContent = "Checkout is unavailable right now.";
    return;
  }

  try {
    const stripe = await loadStripe(key);
    mountedCheckout?.destroy?.();
    mountedCheckout = await stripe.initEmbeddedCheckout({
      clientSecret: session.client_secret,
    });
    if (status) status.textContent = "";
    mountedCheckout.mount("#checkout-mount");
  } catch (exc) {
    // Stripe.js blocked by an extension or a network policy: fall back to the
    // hosted page rather than leaving a dead modal.
    if (status) status.textContent = "Opening Stripe…";
    try {
      const hosted = await api<{ url?: string }>("/v1/billing/checkout", {
        pack: opts.pack,
        price_id: opts.priceId,
        embedded: false,
      });
      if (hosted.url) window.location.href = hosted.url;
    } catch {
      if (status) status.textContent = `Checkout unavailable: ${(exc as Error).message}`;
    }
  }
}

// --------------------------------------------------------------------------
// Wiring
// --------------------------------------------------------------------------

// --------------------------------------------------------------------------
// Theme and small page affordances
// --------------------------------------------------------------------------

/** Same storage key the app uses, so the choice survives the boundary. */
function wireTheme(): void {
  const toggle = $<HTMLButtonElement>("#theme-toggle");
  if (!toggle) return;
  toggle.addEventListener("click", () => {
    const stamped = document.documentElement.getAttribute("data-theme");
    const dark = stamped
      ? stamped === "dark"
      : window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
    const next = dark ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("th-theme", next);
    } catch {
      /* private mode: the choice just does not persist */
    }
  });
}

/** Copy buttons on the API examples. A curl command exists to be pasted. */
function wireCopy(): void {
  for (const node of document.querySelectorAll<HTMLButtonElement>("[data-copy]")) {
    node.addEventListener("click", async () => {
      const code = node.closest(".codeblock")?.querySelector("code");
      if (!code) return;
      try {
        await navigator.clipboard.writeText(code.textContent ?? "");
        node.textContent = "Copied";
        node.dataset.done = "1";
      } catch {
        node.textContent = "Press Ctrl+C";
      }
      setTimeout(() => {
        node.textContent = "Copy";
        delete node.dataset.done;
      }, 1800);
    });
  }
}

function wire(): void {
  ensureSharedStyles();
  wireSignIn();
  wireAccount();
  wireTheme();
  wireCopy();
  setAuthMode("login");

  document.addEventListener("click", (event) => {
    const target = (event.target as HTMLElement).closest<HTMLElement>("[data-action]");
    if (!target) return;
    const action = target.dataset.action;

    if (action === "signin") {
      event.preventDefault();
      void me().then((user) => {
        if (user.signed_in) window.location.href = "/account";
        else {
          const mode = (target.dataset.authMode as AuthMode) || "signup";
          requireSignIn("", mode === "login" ? "login" : "signup")
            .then(() => (window.location.href = "/app"))
            .catch(() => undefined);
        }
      });
    } else if (action === "buy") {
      event.preventDefault();
      void startCheckout({
        pack: target.dataset.pack,
        plan: target.dataset.plan,
        priceId: target.dataset.price,
      });
    } else if (action === "close") {
      event.preventDefault();
      const id = target.closest(".overlay")?.id;
      if (id) close(id);
      if (id === "signin-overlay" && pendingAuth) {
        pendingAuth.reject(new Error("dismissed"));
        pendingAuth = null;
      }
      if (id === "checkout-overlay") {
        mountedCheckout?.destroy?.();
        mountedCheckout = null;
      }
    }
  });

  // Click the backdrop or press Escape to dismiss.
  for (const node of document.querySelectorAll<HTMLElement>(".overlay")) {
    node.addEventListener("click", (event) => {
      if (event.target === node) {
        close(node.id);
        if (node.id === "checkout-overlay") {
          mountedCheckout?.destroy?.();
          mountedCheckout = null;
        }
      }
    });
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeAll();
      mountedCheckout?.destroy?.();
      mountedCheckout = null;
    }
  });

  // Reflect an existing session without blocking first paint.
  void me().then((user) => {
    identify(user.user_id);
    refreshHeader(user);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", wire);
} else {
  wire();
}
