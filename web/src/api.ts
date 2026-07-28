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

export interface LibraryFolder {
  id: string;
  name: string;
  parent_id: string | null;
}

export interface WorkbookSheet {
  name: string;
  rows?: number;
  columns?: number;
}

export interface DatasetShape {
  header_row: number;
  dropped_rows: number;
  notes: string[] | string;
  confidence: number | string;
  sheets: Array<string | WorkbookSheet>;
}

export interface LibraryDataset {
  id: string;
  name: string;
  folder_id: string | null;
  rows: number;
  columns: number;
  shape: DatasetShape | null;
  shared: boolean;
  description?: string;
}

export interface LibrarySource {
  id: string;
  name: string;
  kind: string;
  folder_id: string | null;
  status: string;
}

export interface LibraryResponse {
  folders: LibraryFolder[];
  datasets: LibraryDataset[];
  sources: LibrarySource[];
}

export interface DatasetPreview {
  schema: Array<{ name: string; type: string }>;
  rows: unknown[][];
  shape: DatasetShape | null;
}

export interface DatasetRestructure {
  header_row: number;
  skip_rows: number;
  sheet: string | null;
  renames: Record<string, string>;
  types: Record<string, string>;
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

function upload<T>(file: File, onProgress: (percent: number) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 20));
    };
    reader.onload = () => {
      if (typeof reader.result !== "string") {
        reject(new Error(`Could not read ${file.name}`));
        return;
      }

      const separator = reader.result.indexOf(",");
      const content = separator >= 0 ? reader.result.slice(separator + 1) : reader.result;
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/v1/upload");
      xhr.withCredentials = true;
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          onProgress(20 + Math.round((event.loaded / event.total) * 79));
        }
      };
      xhr.onerror = () => reject(new Error("The upload could not reach the server"));
      xhr.onload = () => {
        const text = xhr.responseText;
        let payload: unknown = null;
        try {
          payload = text ? JSON.parse(text) : null;
        } catch {
          payload = text;
        }

        if (xhr.status < 200 || xhr.status >= 300) {
          const body = payload as { error?: string; detail?: string } | null;
          reject(
            new ApiError(
              xhr.status,
              body?.error ?? "http_error",
              body?.detail ?? body?.error ?? `HTTP ${xhr.status}`,
            ),
          );
          return;
        }
        onProgress(100);
        resolve(payload as T);
      };
      xhr.send(JSON.stringify({ filename: file.name, content_base64: content }));
    };
    reader.readAsDataURL(file);
  });
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body ?? {}) }),
  del: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  upload: <T>(file: File, onProgress: (percent: number) => void) =>
    upload<T>(file, onProgress),

  me: () => request<User>("/v1/me"),
  signIn: (email: string) => request<User>("/v1/auth/signin", {
    method: "POST",
    body: JSON.stringify({ email }),
  }),
  signOut: () => request<{ signed_out: boolean }>("/v1/auth/signout", { method: "POST" }),

  library: () => request<LibraryResponse>("/v1/library"),
  createFolder: (name: string, parentId: string | null) =>
    request<LibraryFolder>("/v1/folders", {
      method: "POST",
      body: JSON.stringify({ name, parent_id: parentId }),
    }),
  updateFolder: (id: string, change: { name?: string; parent_id?: string | null }) =>
    request<LibraryFolder>(`/v1/folders/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(change),
    }),
  deleteFolder: (id: string) =>
    request<void>(`/v1/folders/${encodeURIComponent(id)}`, { method: "DELETE" }),
  updateDataset: (
    id: string,
    change: { name?: string; folder_id?: string | null; description?: string },
  ) =>
    request<LibraryDataset>(`/v1/datasets/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(change),
    }),
  updateSource: (id: string, change: { name?: string; folder_id?: string | null }) =>
    request<LibrarySource>(`/v1/sources/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(change),
    }),
  restructureDataset: (id: string, change: DatasetRestructure) =>
    request<DatasetPreview>(`/v1/datasets/${encodeURIComponent(id)}/restructure`, {
      method: "POST",
      body: JSON.stringify(change),
    }),
  previewDataset: (id: string, rows = 20) =>
    request<DatasetPreview>(
      `/v1/datasets/${encodeURIComponent(id)}/preview?rows=${encodeURIComponent(rows)}`,
    ),
  shareDataset: (id: string) =>
    request<{ url: string }>(`/v1/datasets/${encodeURIComponent(id)}/share`, {
      method: "POST",
      body: "{}",
    }),
  unshareDataset: (id: string) =>
    request<void>(`/v1/datasets/${encodeURIComponent(id)}/share`, { method: "DELETE" }),
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
