import {
  AsyncDuckDB,
  VoidLogger,
  type AsyncDuckDBConnection,
} from "@duckdb/duckdb-wasm";
import { tableToIPC } from "apache-arrow";
import { compilePlan } from "./compile";
import {
  RESHAPE_LIMITS,
  RESHAPE_PROTOCOL_VERSION,
  type ErrorResponse,
  type FallbackReason,
  type ReshapeRequest,
  type ReshapeResponse,
  type SchemaColumn,
} from "./protocol";

interface RegisteredSource {
  sourceId: string;
  tableName: string;
  schema: SchemaColumn[];
  rowCount: number;
}

const scope = self as unknown as {
  postMessage(message: ReshapeResponse, transfer?: Transferable[]): void;
  onmessage: ((event: MessageEvent<ReshapeRequest>) => void) | null;
  close(): void;
};
let database: AsyncDuckDB | null = null;
let connection: AsyncDuckDBConnection | null = null;
let source: RegisteredSource | null = null;
let latestRunRequestId = "";
const cancelled = new Set<string>();

function respond(response: ReshapeResponse, transfer: Transferable[] = []): void {
  scope.postMessage(response, transfer);
}

function error(
  requestId: string,
  fallbackReason: FallbackReason,
): void {
  const response: ErrorResponse = {
    version: RESHAPE_PROTOCOL_VERSION,
    requestId,
    type: "error",
    fallbackReason,
  };
  respond(response);
}

function internalTableName(): string {
  const random = crypto.randomUUID().replaceAll("-", "");
  return `reshape_${random}`;
}

function isSelfHostedAsset(value: string, pathname: string): boolean {
  try {
    const url = new URL(value, self.location.href);
    return url.origin === self.location.origin && url.pathname === pathname;
  } catch {
    return false;
  }
}

async function initialize(
  requestId: string,
  wasmUrl: string,
  duckdbWorkerUrl: string,
): Promise<void> {
  if (database && connection) {
    respond({ version: RESHAPE_PROTOCOL_VERSION, requestId, type: "ready" });
    return;
  }
  if (
    !isSelfHostedAsset(wasmUrl, "/static/duckdb/duckdb-mvp.wasm")
    || !isSelfHostedAsset(
      duckdbWorkerUrl,
      "/static/duckdb/duckdb-browser-mvp.worker.js",
    )
  ) {
    error(requestId, "wasm_unavailable");
    return;
  }
  try {
    const duckdbWorker = new Worker(duckdbWorkerUrl);
    database = new AsyncDuckDB(new VoidLogger(), duckdbWorker);
    await database.instantiate(wasmUrl);
    connection = await database.connect();
    respond({ version: RESHAPE_PROTOCOL_VERSION, requestId, type: "ready" });
  } catch {
    await disposeDatabase();
    error(requestId, "wasm_unavailable");
  }
}

async function dropSource(): Promise<void> {
  if (source && connection) {
    await connection.query(`DROP TABLE IF EXISTS "${source.tableName}"`);
  }
  source = null;
}

async function registerArrow(
  requestId: string,
  sourceId: string,
  schema: SchemaColumn[],
  rowCount: number,
  arrow: ArrayBuffer,
): Promise<void> {
  if (!connection) {
    error(requestId, "wasm_unavailable");
    return;
  }
  if (
    rowCount < 1
    || rowCount > RESHAPE_LIMITS.sourceRows
    || arrow.byteLength > RESHAPE_LIMITS.sourceBytes
  ) {
    error(requestId, "too_large");
    return;
  }

  try {
    await dropSource();
    const tableName = internalTableName();
    await connection.insertArrowFromIPCStream(new Uint8Array(arrow), {
      name: tableName,
      create: true,
    });
    source = { sourceId, tableName, schema, rowCount };
    respond({
      version: RESHAPE_PROTOCOL_VERSION,
      requestId,
      type: "registered",
      sourceId,
      schema,
      rowCount,
    });
  } catch {
    await dropSource().catch(() => undefined);
    error(requestId, "semantic_mismatch");
  }
}

async function executePlan(
  requestId: string,
  sourceId: string,
  steps: Extract<ReshapeRequest, { type: "runPlan" }>["steps"],
): Promise<void> {
  if (!connection || !source || source.sourceId !== sourceId) {
    error(requestId, "worker_crash");
    return;
  }
  latestRunRequestId = requestId;
  const compiled = compilePlan(source.tableName, source.schema, steps);
  if (!compiled.ok) {
    error(requestId, compiled.fallbackReason);
    return;
  }

  const started = performance.now();
  try {
    const prepared = await connection.prepare(compiled.sql);
    let table;
    try {
      table = await prepared.query(...compiled.params);
    } finally {
      await prepared.close();
    }
    if (cancelled.delete(requestId) || latestRunRequestId !== requestId) {
      respond({
        version: RESHAPE_PROTOCOL_VERSION,
        requestId,
        type: "cancelled",
      });
      return;
    }
    if (table.numRows > RESHAPE_LIMITS.outputRows) {
      error(requestId, "too_large");
      return;
    }
    const ipc = tableToIPC(table, "stream");
    const arrow = new Uint8Array(ipc).buffer;
    respond({
      version: RESHAPE_PROTOCOL_VERSION,
      requestId,
      type: "result",
      sourceId,
      arrow,
      columns: table.schema.fields.map((field) => field.name),
      rowCount: table.numRows,
      sourceRowCount: source.rowCount,
      elapsedMs: Math.round(performance.now() - started),
      warnings: [],
    }, [arrow]);
  } catch {
    error(requestId, "semantic_mismatch");
  }
}

async function disposeDatabase(): Promise<void> {
  try {
    await dropSource();
    await connection?.close();
    await database?.terminate();
  } catch {
    // Disposal is best-effort because the main thread terminates this worker
    // when the Wasm runtime no longer responds.
  } finally {
    connection = null;
    database = null;
  }
}

scope.onmessage = (event: MessageEvent<ReshapeRequest>) => {
  const message = event.data;
  if (message.version !== RESHAPE_PROTOCOL_VERSION) {
    error(message.requestId, "worker_crash");
    return;
  }

  switch (message.type) {
    case "init":
      void initialize(message.requestId, message.wasmUrl, message.duckdbWorkerUrl);
      break;
    case "registerArrow":
      void registerArrow(
        message.requestId,
        message.sourceId,
        message.schema,
        message.rowCount,
        message.arrow,
      );
      break;
    case "runPlan":
      void executePlan(message.requestId, message.sourceId, message.steps);
      break;
    case "cancel":
      cancelled.add(message.targetRequestId);
      if (latestRunRequestId === message.targetRequestId) {
        void connection?.cancelSent();
      }
      respond({
        version: RESHAPE_PROTOCOL_VERSION,
        requestId: message.requestId,
        type: "cancelled",
      });
      break;
    case "dispose":
      void disposeDatabase().finally(() => {
        respond({
          version: RESHAPE_PROTOCOL_VERSION,
          requestId: message.requestId,
          type: "cancelled",
        });
        scope.close();
      });
      break;
  }
};
