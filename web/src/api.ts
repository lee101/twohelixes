/** HTTP + SSE client. */

export interface User {
  signed_in: boolean;
  email: string;
  plan: string;
  api_credits: number;
  free_queries_used: number;
  free_queries_total: number;
  free_queries_left: number;
  paid: boolean;
  is_admin: boolean;
}

export interface ChartConfig {
  chart_type: string;
  x?: string | null;
  y?: string | string[] | null;
  z?: string | null;
  color?: string | null;
  size?: string | null;
  facet?: string | null;
  agg?: string | null;
  stacked?: boolean;
  orientation?: string;
  title?: string;
  x_title?: string;
  y_title?: string;
  [key: string]: unknown;
}

export interface PipelineResult {
  figure: PlotlyFigure;
  config: ChartConfig;
  preview: Record<string, unknown>[];
  columns: string[];
  row_count: number;
  warnings: string[];
  audit: { code: string; detail: string }[];
  transform_code: string;
  chart_id?: string;
  elapsed_ms: number;
}

export interface PlotlyFigure {
  data: Record<string, unknown>[];
  layout: Record<string, unknown>;
  config?: Record<string, unknown>;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    ...init,
  });

  const text = await response.text();
  let payload: unknown = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }

  if (!response.ok) {
    const body = payload as { error?: string; detail?: string } | null;
    throw new ApiError(
      response.status,
      body?.error ?? "http_error",
      body?.detail ?? body?.error ?? `HTTP ${response.status}`,
    );
  }
  return payload as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),

  me: () => request<User>("/v1/me"),
  signIn: (email: string) => request<User>("/v1/auth/signin", {
    method: "POST",
    body: JSON.stringify({ email }),
  }),
  signOut: () => request<{ signed_out: boolean }>("/v1/auth/signout", { method: "POST" }),
};

export type SseHandler = (event: string, data: any) => void;

/**
 * Read an SSE response body.
 *
 * `fetch` rather than `EventSource` because the stream endpoints are POSTs
 * carrying a JSON body, which EventSource cannot send. Returns an abort
 * function so a navigation can cancel the run.
 */
export function stream(
  path: string,
  body: unknown,
  onEvent: SseHandler,
): () => void {
  const controller = new AbortController();

  (async () => {
    let response: Response;
    try {
      response = await fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (error) {
      if (!controller.signal.aborted) {
        onEvent("error", { message: String(error) });
      }
      return;
    }

    if (!response.body) {
      onEvent("error", { message: "no response body" });
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Frames are separated by a blank line; keep the partial tail.
        let split: number;
        while ((split = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const parsed = parseFrame(frame);
          if (parsed) onEvent(parsed.event, parsed.data);
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        onEvent("error", { message: String(error) });
      }
    }
  })();

  return () => controller.abort();
}

function parseFrame(frame: string): { event: string; data: any } | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue; // comment / keep-alive
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }

  if (!dataLines.length) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return { event, data: dataLines.join("\n") };
  }
}
