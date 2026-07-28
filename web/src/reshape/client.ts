import { planIsLocallySupported } from "./compile";
import {
  RESHAPE_LIMITS,
  RESHAPE_PROTOCOL_VERSION,
  type FallbackReason,
  type LocalReshapeResult,
  type ReshapeRequest,
  type ReshapeResponse,
  type SchemaColumn,
  type Step,
} from "./protocol";

export const LOCAL_RESHAPE_STORAGE_KEY = "twohelixes.builder.local-reshape.v1";

const WORKER_URL = "/static/duckdb.worker.js";
const WASM_URL = "/static/duckdb/duckdb-mvp.wasm";
const DUCKDB_WORKER_URL = "/static/duckdb/duckdb-browser-mvp.worker.js";

interface SourceBuffer {
  sourceId: string;
  arrow: ArrayBuffer;
  schema: SchemaColumn[];
  rowCount: number;
}

interface PendingRequest {
  resolve: (response: ReshapeResponse) => void;
  timer: ReturnType<typeof setTimeout>;
}

export function localReshapeEnabled(): boolean {
  try {
    return localStorage.getItem(LOCAL_RESHAPE_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

export function setLocalReshapeEnabled(enabled: boolean): void {
  try {
    localStorage.setItem(LOCAL_RESHAPE_STORAGE_KEY, String(enabled));
  } catch {
    // Storage can be unavailable in private or embedded contexts; defaulting
    // off preserves the existing server preview.
  }
}

function browserSupportsLocalReshape(): boolean {
  return typeof Worker !== "undefined"
    && typeof WebAssembly !== "undefined"
    && typeof ArrayBuffer !== "undefined";
}

function fallback(fallbackReason: FallbackReason): LocalReshapeResult {
  return { ok: false, fallbackReason };
}

function normalizeValue(value: unknown): unknown {
  if (typeof value === "bigint") {
    const number = Number(value);
    return Number.isSafeInteger(number) ? number : value.toString();
  }
  if (value instanceof Date) return value.toISOString();
  if (Array.isArray(value)) return value.map(normalizeValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, normalizeValue(item)]),
    );
  }
  return value;
}

export class LocalReshaper {
  private worker: Worker | null = null;
  private initialized = false;
  private registeredSourceId = "";
  private source: SourceBuffer | null = null;
  private requestSequence = 0;
  private activeRunId = "";
  private reshapeInFlight = false;
  private unavailableReason: FallbackReason | null = null;
  private readonly pending = new Map<string, PendingRequest>();

  async setRows(
    rows: Record<string, unknown>[],
    schema: SchemaColumn[],
  ): Promise<LocalReshapeResult | { ok: true }> {
    if (!localReshapeEnabled()) return fallback("disabled");
    if (!browserSupportsLocalReshape()) return fallback("unsupported_browser");
    if (!rows.length) return fallback("semantic_mismatch");
    if (rows.length > RESHAPE_LIMITS.sourceRows) return fallback("too_large");

    try {
      const { tableFromJSON, tableToIPC } = await import("apache-arrow");
      const normalized = rows.map((row) =>
        Object.fromEntries(schema.map((column) => [column.name, row[column.name] ?? null])));
      const ipc = tableToIPC(tableFromJSON(normalized), "stream");
      const arrow = new Uint8Array(ipc).buffer;
      if (arrow.byteLength > RESHAPE_LIMITS.sourceBytes) return fallback("too_large");
      this.source = {
        sourceId: crypto.randomUUID(),
        arrow,
        schema,
        rowCount: rows.length,
      };
      this.registeredSourceId = "";
      this.unavailableReason = null;
      return { ok: true };
    } catch {
      this.source = null;
      return fallback("semantic_mismatch");
    }
  }

  async reshape(steps: Step[]): Promise<LocalReshapeResult> {
    if (!localReshapeEnabled()) return fallback("disabled");
    if (!browserSupportsLocalReshape()) return fallback("unsupported_browser");
    if (!this.source) return fallback("unsupported_source");
    if (this.unavailableReason) return fallback(this.unavailableReason);
    if (this.reshapeInFlight) return fallback("worker_crash");
    const eligibility = planIsLocallySupported(this.source.schema, steps);
    if (!eligibility.ok) return fallback(eligibility.fallbackReason);

    this.reshapeInFlight = true;
    try {
      let result = await this.executeOnce(steps);
      if (
        !result.ok
        && ["worker_crash", "wasm_unavailable"].includes(result.fallbackReason)
      ) {
        this.poisonWorker(result.fallbackReason);
        result = await this.executeOnce(steps);
      }
      if (
        !result.ok
        && ["worker_crash", "wasm_unavailable", "timeout"].includes(result.fallbackReason)
      ) {
        this.unavailableReason = result.fallbackReason;
      }
      return result;
    } finally {
      this.reshapeInFlight = false;
    }
  }

  dispose(): void {
    if (this.worker) {
      const requestId = this.nextRequestId();
      const message: ReshapeRequest = {
        version: RESHAPE_PROTOCOL_VERSION,
        requestId,
        type: "dispose",
      };
      this.worker.postMessage(message);
    }
    this.poisonWorker("worker_crash");
    this.source = null;
    this.unavailableReason = null;
  }

  private async executeOnce(steps: Step[]): Promise<LocalReshapeResult> {
    const initialized = await this.ensureWorker();
    if (!initialized.ok) return initialized;
    const registered = await this.ensureSourceRegistered();
    if (!registered.ok) return registered;

    if (this.activeRunId) this.cancel(this.activeRunId);
    const requestId = this.nextRequestId();
    this.activeRunId = requestId;
    const message: ReshapeRequest = {
      version: RESHAPE_PROTOCOL_VERSION,
      requestId,
      type: "runPlan",
      sourceId: this.source!.sourceId,
      steps,
    };
    const response = await this.send(message, RESHAPE_LIMITS.executionMs);
    if (this.activeRunId === requestId) this.activeRunId = "";
    if (!response) {
      this.poisonWorker("timeout");
      return fallback("timeout");
    }
    if (response.type === "error") return fallback(response.fallbackReason);
    if (response.type === "cancelled") return fallback("worker_crash");
    if (response.type !== "result") return fallback("worker_crash");
    if (response.rowCount === 0) return fallback("semantic_mismatch");

    try {
      const { tableFromIPC } = await import("apache-arrow");
      const table = tableFromIPC(new Uint8Array(response.arrow));
      const temporalColumns = new Set(
        table.schema.fields
          .filter((field) => /^(Timestamp|Date)/.test(String(field.type)))
          .map((field) => field.name),
      );
      const rows = table.toArray().map((row) => {
        const record = row.toJSON() as Record<string, unknown>;
        for (const column of temporalColumns) {
          const value = record[column];
          if (typeof value === "number" && Number.isFinite(value)) {
            record[column] = new Date(value).toISOString();
          }
        }
        return normalizeValue(record) as Record<string, unknown>;
      });
      return {
        ok: true,
        rows,
        columns: response.columns,
        rowCount: response.rowCount,
        elapsedMs: response.elapsedMs,
        warnings: response.warnings,
      };
    } catch {
      return fallback("semantic_mismatch");
    }
  }

  private async ensureWorker(): Promise<LocalReshapeResult | { ok: true }> {
    if (this.worker && this.initialized) return { ok: true };
    try {
      const worker = new Worker(WORKER_URL, { type: "module", name: "reshape" });
      this.worker = worker;
      worker.onmessage = (event: MessageEvent<ReshapeResponse>) => {
        const response = event.data;
        if (response.version !== RESHAPE_PROTOCOL_VERSION) return;
        const pending = this.pending.get(response.requestId);
        if (!pending) return;
        clearTimeout(pending.timer);
        this.pending.delete(response.requestId);
        pending.resolve(response);
      };
      worker.onerror = () => this.poisonWorker("worker_crash");
      worker.onmessageerror = () => this.poisonWorker("worker_crash");

      const requestId = this.nextRequestId();
      const response = await this.send({
        version: RESHAPE_PROTOCOL_VERSION,
        requestId,
        type: "init",
        wasmUrl: WASM_URL,
        duckdbWorkerUrl: DUCKDB_WORKER_URL,
      }, RESHAPE_LIMITS.initializationMs);
      if (!response) {
        this.poisonWorker("timeout");
        return fallback("timeout");
      }
      if (response.type === "error") {
        const reason = response.fallbackReason;
        this.poisonWorker(reason);
        return fallback(reason);
      }
      if (response.type !== "ready") {
        this.poisonWorker("worker_crash");
        return fallback("worker_crash");
      }
      this.initialized = true;
      return { ok: true };
    } catch {
      this.poisonWorker("worker_crash");
      return fallback("worker_crash");
    }
  }

  private async ensureSourceRegistered(): Promise<LocalReshapeResult | { ok: true }> {
    if (!this.source) return fallback("unsupported_source");
    if (this.registeredSourceId === this.source.sourceId) return { ok: true };
    const requestId = this.nextRequestId();
    const transfer = this.source.arrow.slice(0);
    const response = await this.send({
      version: RESHAPE_PROTOCOL_VERSION,
      requestId,
      type: "registerArrow",
      sourceId: this.source.sourceId,
      schema: this.source.schema,
      rowCount: this.source.rowCount,
      arrow: transfer,
    }, RESHAPE_LIMITS.initializationMs, [transfer]);
    if (!response) {
      this.poisonWorker("timeout");
      return fallback("timeout");
    }
    if (response.type === "error") return fallback(response.fallbackReason);
    if (response.type !== "registered") return fallback("worker_crash");
    this.registeredSourceId = this.source.sourceId;
    return { ok: true };
  }

  private send(
    message: ReshapeRequest,
    timeoutMs: number,
    transfer: Transferable[] = [],
  ): Promise<ReshapeResponse | null> {
    const worker = this.worker;
    if (!worker) return Promise.resolve(null);
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(message.requestId);
        resolve(null);
      }, timeoutMs);
      this.pending.set(message.requestId, { resolve, timer });
      try {
        worker.postMessage(message, transfer);
      } catch {
        clearTimeout(timer);
        this.pending.delete(message.requestId);
        resolve(null);
      }
    });
  }

  private cancel(targetRequestId: string): void {
    if (!this.worker) return;
    this.worker.postMessage({
      version: RESHAPE_PROTOCOL_VERSION,
      requestId: this.nextRequestId(),
      type: "cancel",
      targetRequestId,
    } satisfies ReshapeRequest);
  }

  private poisonWorker(_reason: FallbackReason): void {
    this.worker?.terminate();
    this.worker = null;
    this.initialized = false;
    this.registeredSourceId = "";
    this.activeRunId = "";
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.resolve({
        version: RESHAPE_PROTOCOL_VERSION,
        requestId: "",
        type: "error",
        fallbackReason: "worker_crash",
      });
    }
    this.pending.clear();
  }

  private nextRequestId(): string {
    this.requestSequence += 1;
    return `reshape-${this.requestSequence}`;
  }
}
