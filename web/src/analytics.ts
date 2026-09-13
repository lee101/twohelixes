/** Small, failure-isolated bridge to the optional first-party tracker. */

type Tracker = (command: string, ...args: unknown[]) => void;

declare global {
  interface Window {
    th?: Tracker;
  }
}

export function track(name: string, properties: Record<string, unknown> = {}): void {
  try {
    window.th?.("track", name, properties);
  } catch {
    // Analytics must never affect the product flow.
  }
}

export function identify(userId: string | null | undefined): void {
  if (userId === undefined) return;
  try {
    window.th?.("identify", userId);
  } catch {
    // Analytics must never affect the product flow.
  }
}
