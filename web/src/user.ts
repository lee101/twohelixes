/** Client-side user snapshot, same idea as netwrck's `netwrck_user`. */

export interface UserData {
  id: string;
  email: string;
  plan: string;
  api_credits: number;
  free_queries_left: number;
  free_queries_total: number;
  free_queries_used: number;
  paid: boolean;
  is_admin: boolean;
  is_subscribed: boolean;
  signed_in: boolean;
}

const STORAGE_KEY = "th_user";

declare global {
  interface Window {
    userData: UserData | Record<string, never>;
  }
}

export function loadUser(): UserData | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as UserData;
    if (parsed?.id && parsed?.email) {
      window.userData = parsed;
      return parsed;
    }
  } catch {
    /* private mode / corrupt blob */
  }
  return null;
}

export function persistUser(user: UserData | null): void {
  if (!user || !user.signed_in) {
    clearUser();
    return;
  }
  const snapshot: UserData = {
    id: user.id || "",
    email: user.email || "",
    plan: user.plan || "free",
    api_credits: user.api_credits || 0,
    free_queries_left: user.free_queries_left || 0,
    free_queries_total: user.free_queries_total || 0,
    free_queries_used: user.free_queries_used || 0,
    paid: !!user.paid,
    is_admin: !!user.is_admin,
    is_subscribed: !!user.is_subscribed,
    signed_in: true,
  };
  window.userData = snapshot;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot));
  } catch {
    /* private mode */
  }
  window.dispatchEvent(new CustomEvent("userLoggedIn", { detail: snapshot }));
}

export function clearUser(): void {
  window.userData = {};
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* private mode */
  }
}

if (!loadUser()) {
  window.userData = window.userData || {};
}
