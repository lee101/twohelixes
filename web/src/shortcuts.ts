export interface Shortcut {
  key: string;
  label: string;
  run: () => void;
}

const scopes = new Map<string, Shortcut[]>();
let installed = false;

export function registerShortcuts(scope: string, shortcuts: Shortcut[]): void {
  scopes.set(scope, shortcuts);
  install();
}

function typing(target: EventTarget | null): boolean {
  const node = target as HTMLElement | null;
  if (!node) return false;
  return node.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName);
}

function all(): Shortcut[] {
  return [
    { key: "/", label: "Focus the question box", run: focusAsk },
    { key: "?", label: "Show keyboard shortcuts", run: showHelp },
    ...[...scopes.values()].flat(),
  ];
}

function focusAsk(): void {
  const box = document.querySelector<HTMLElement>(".ask-input, .dash-composer-ask input, .dash-composer textarea");
  if (box && !box.offsetParent) document.documentElement.classList.remove("th-focus");
  box?.focus();
}

function install(): void {
  if (installed) return;
  installed = true;
  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) return;
    if (typing(event.target)) return;
    if (document.querySelector("dialog[open]:not(.shortcut-help)")) return;
    const match = all().find((s) => s.key === event.key);
    if (!match) return;
    event.preventDefault();
    match.run();
  });
}

export function showHelp(): void {
  const open = document.querySelector<HTMLDialogElement>("dialog.shortcut-help");
  if (open) {
    open.close();
    return;
  }
  const dialog = document.createElement("dialog");
  dialog.className = "pick-sheet shortcut-help";
  const heading = document.createElement("h2");
  heading.textContent = "Keyboard shortcuts";
  const list = document.createElement("dl");
  list.className = "shortcut-list";
  for (const s of all()) {
    const dt = document.createElement("dt");
    const kbd = document.createElement("kbd");
    kbd.textContent = s.key === " " ? "Space" : s.key;
    dt.append(kbd);
    const dd = document.createElement("dd");
    dd.textContent = s.label;
    list.append(dt, dd);
  }
  const extra = document.createElement("p");
  extra.className = "note";
  extra.textContent = "Enter asks, Shift+Enter adds a line. Esc closes panels and leaves full screen. On a dashboard, Alt+Arrow moves the focused tile.";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "btn btn-ghost btn-small";
  close.textContent = "Close";
  close.addEventListener("click", () => dialog.close());
  dialog.append(heading, list, extra, close);
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) dialog.close();
  });
  document.body.append(dialog);
  dialog.showModal();
}
