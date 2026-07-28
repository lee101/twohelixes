export const RESHAPE_PROTOCOL_VERSION = 1 as const;

export const RESHAPE_LIMITS = {
  sourceBytes: 32 * 1024 * 1024,
  sourceRows: 200_000,
  outputRows: 5_000,
  executionMs: 5_000,
  initializationMs: 10_000,
  concurrentRequests: 1,
  maxSteps: 24,
} as const;

export type FallbackReason =
  | "disabled"
  | "unsupported_browser"
  | "unsupported_source"
  | "unsupported_step"
  | "unsupported_expr"
  | "semantic_mismatch"
  | "too_large"
  | "timeout"
  | "wasm_unavailable"
  | "worker_crash";

export interface Step {
  id: string;
  type: string;
  params: Record<string, any>;
  note?: string;
  author?: "agent" | "human";
  enabled?: boolean;
}

export interface SchemaColumn {
  name: string;
  dtype: string;
  nulls?: number;
}

interface RequestBase {
  version: typeof RESHAPE_PROTOCOL_VERSION;
  requestId: string;
}

export interface InitRequest extends RequestBase {
  type: "init";
  wasmUrl: string;
  duckdbWorkerUrl: string;
}

export interface RegisterArrowRequest extends RequestBase {
  type: "registerArrow";
  sourceId: string;
  schema: SchemaColumn[];
  rowCount: number;
  arrow: ArrayBuffer;
}

export interface RunPlanRequest extends RequestBase {
  type: "runPlan";
  sourceId: string;
  steps: Step[];
}

export interface CancelRequest extends RequestBase {
  type: "cancel";
  targetRequestId: string;
}

export interface DisposeRequest extends RequestBase {
  type: "dispose";
}

export type ReshapeRequest =
  | InitRequest
  | RegisterArrowRequest
  | RunPlanRequest
  | CancelRequest
  | DisposeRequest;

interface ResponseBase {
  version: typeof RESHAPE_PROTOCOL_VERSION;
  requestId: string;
}

export interface ReadyResponse extends ResponseBase {
  type: "ready";
}

export interface RegisteredResponse extends ResponseBase {
  type: "registered";
  sourceId: string;
  schema: SchemaColumn[];
  rowCount: number;
}

export interface ResultResponse extends ResponseBase {
  type: "result";
  sourceId: string;
  arrow: ArrayBuffer;
  columns: string[];
  rowCount: number;
  sourceRowCount: number;
  elapsedMs: number;
  warnings: string[];
}

export interface CancelledResponse extends ResponseBase {
  type: "cancelled";
}

export interface ErrorResponse extends ResponseBase {
  type: "error";
  fallbackReason: FallbackReason;
}

export type ReshapeResponse =
  | ReadyResponse
  | RegisteredResponse
  | ResultResponse
  | CancelledResponse
  | ErrorResponse;

export interface LocalReshapeSuccess {
  ok: true;
  rows: Record<string, unknown>[];
  columns: string[];
  rowCount: number;
  elapsedMs: number;
  warnings: string[];
}

export interface LocalReshapeFallback {
  ok: false;
  fallbackReason: FallbackReason;
}

export type LocalReshapeResult = LocalReshapeSuccess | LocalReshapeFallback;
