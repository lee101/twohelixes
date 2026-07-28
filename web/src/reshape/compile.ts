import {
  RESHAPE_LIMITS,
  type FallbackReason,
  type SchemaColumn,
  type Step,
} from "./protocol";

export interface CompiledPlan {
  ok: true;
  sql: string;
  params: unknown[];
  columns: string[] | null;
}

export interface CompileFailure {
  ok: false;
  fallbackReason: Extract<
    FallbackReason,
    "unsupported_step" | "unsupported_expr" | "semantic_mismatch"
  >;
}

export type CompileResult = CompiledPlan | CompileFailure;

const FILTER_OPS = new Set([
  "eq", "ne", "gt", "ge", "lt", "le",
  "in", "not_in", "contains", "starts_with", "ends_with",
  "is_null", "not_null",
]);

const AGGREGATIONS: Record<string, (column: string) => string> = {
  sum: (column) => `coalesce(sum(${column}), 0)`,
  mean: (column) => `avg(${column})`,
  median: (column) => `median(${column})`,
  count: (column) => `count(${column})`,
  nunique: (column) => `count(DISTINCT ${column})`,
  min: (column) => `min(${column})`,
  max: (column) => `max(${column})`,
  std: (column) => `stddev_samp(${column})`,
  first: (column) => `first(${column}) FILTER (WHERE ${column} IS NOT NULL)`,
  last: (column) => `last(${column}) FILTER (WHERE ${column} IS NOT NULL)`,
};

const COMPARISONS: Record<string, string> = {
  eq: "=",
  ne: "<>",
  gt: ">",
  ge: ">=",
  lt: "<",
  le: "<=",
};

const quoteIdentifier = (name: string): string => `"${name.replaceAll('"', '""')}"`;

function failure(
  fallbackReason: CompileFailure["fallbackReason"],
): CompileFailure {
  return { ok: false, fallbackReason };
}

function stringList(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.map((item) => String(item));
}

function integer(value: unknown, fallbackValue: number): number | null {
  const number = value === undefined || value === null || value === ""
    ? fallbackValue
    : Number(value);
  return Number.isInteger(number) && number > 0 ? number : null;
}

function dtypeKind(dtype: string | undefined): "numeric" | "boolean" | "text" | "other" {
  const normalized = (dtype ?? "").toLowerCase();
  if (/bool/.test(normalized)) return "boolean";
  if (/(?:^|[^a-z])(int|uint|float|double|decimal|number)/.test(normalized)) {
    return "numeric";
  }
  if (/(object|string|str|category|categorical)/.test(normalized)) return "text";
  return "other";
}

class KnownSchema {
  constructor(
    readonly columns: string[],
    readonly dtypes: Map<string, string>,
    readonly nulls: Map<string, number>,
  ) {}

  static from(columns: SchemaColumn[]): KnownSchema | null {
    const names = columns.map((column) => column.name);
    if (!names.length || new Set(names).size !== names.length) return null;
    return new KnownSchema(
      names,
      new Map(columns.map((column) => [column.name, column.dtype])),
      new Map(columns.map((column) => [column.name, column.nulls ?? -1])),
    );
  }

  has(name: string): boolean {
    return this.columns.includes(name);
  }

  require(value: unknown): string | null {
    const name = String(value ?? "");
    return name && this.has(name) ? name : null;
  }

  withColumns(columns: string[]): KnownSchema | null {
    if (!columns.length || new Set(columns).size !== columns.length) return null;
    return new KnownSchema(
      columns,
      new Map(columns.map((name) => [name, this.dtypes.get(name) ?? "unknown"])),
      new Map(columns.map((name) => [name, -1])),
    );
  }
}

interface StepSql {
  sql: string;
  params: unknown[];
  schema: KnownSchema | null;
}

function aggregateExpression(agg: unknown, column: string): string | null {
  const compiler = AGGREGATIONS[String(agg || "sum")];
  return compiler ? compiler(quoteIdentifier(column)) : null;
}

function compileFilter(step: Step, schema: KnownSchema, input: string): StepSql | null {
  const column = schema.require(step.params.column);
  const op = String(step.params.op || "eq");
  if (!column || !FILTER_OPS.has(op)) return null;
  const identifier = quoteIdentifier(column);

  if (op === "is_null" || op === "not_null") {
    return {
      sql: `SELECT * FROM ${input} WHERE ${identifier} IS ${op === "not_null" ? "NOT " : ""}NULL`,
      params: [],
      schema,
    };
  }

  if (op === "in" || op === "not_in") {
    const raw = Array.isArray(step.params.value) ? step.params.value : [step.params.value];
    if (!raw.length) {
      return {
        sql: `SELECT * FROM ${input} WHERE ${op === "in" ? "FALSE" : "TRUE"}`,
        params: [],
        schema,
      };
    }
    const hasNull = raw.some((value) => value === null || value === undefined);
    const values = raw.filter((value) => value !== null && value !== undefined);
    const kind = dtypeKind(schema.dtypes.get(column));
    if (
      (kind === "numeric" && values.some((value) =>
        typeof value !== "number" || !Number.isFinite(value)))
      || (kind === "boolean" && values.some((value) => typeof value !== "boolean"))
      || (kind === "text" && values.some((value) => typeof value !== "string"))
    ) {
      return null;
    }
    const nullMatches = hasNull && kind === "text";
    const matches = [
      ...(values.length
        ? [`coalesce(${identifier} IN (${values.map(() => "?").join(", ")}), FALSE)`]
        : []),
      ...(nullMatches ? [`${identifier} IS NULL`] : []),
    ].join(" OR ") || "FALSE";
    return {
      sql: `SELECT * FROM ${input} WHERE ${op === "not_in" ? `NOT (${matches})` : `(${matches})`}`,
      params: values,
      schema,
    };
  }

  if (op === "contains") {
    if ((schema.nulls.get(column) ?? -1) !== 0) return null;
    return {
      sql: `SELECT * FROM ${input} WHERE coalesce(contains(lower(CAST(${identifier} AS VARCHAR)), lower(?)), FALSE)`,
      params: [String(step.params.value ?? "")],
      schema,
    };
  }

  if (op === "starts_with" || op === "ends_with") {
    if ((schema.nulls.get(column) ?? -1) !== 0) return null;
    if (dtypeKind(schema.dtypes.get(column)) !== "text") return null;
    return {
      sql: `SELECT * FROM ${input} WHERE coalesce(${op}(CAST(${identifier} AS VARCHAR), ?), FALSE)`,
      params: [String(step.params.value ?? "")],
      schema,
    };
  }

  const comparison = `${identifier} ${COMPARISONS[op]} ?`;
  return {
    sql: `SELECT * FROM ${input} WHERE coalesce(${comparison}, ${op === "ne" ? "TRUE" : "FALSE"})`,
    params: [step.params.value],
    schema,
  };
}

interface Metric {
  column: string;
  agg: string;
  alias: string;
}

function metrics(
  raw: unknown,
  schema: KnownSchema,
  countFallback?: string,
): Metric[] | null {
  if (!Array.isArray(raw) || !raw.length) return null;
  const out: Metric[] = [];
  const sourceColumns = new Set<string>();
  const aliases = new Set<string>();

  for (const item of raw) {
    if (!item || typeof item !== "object") return null;
    const metric = item as Record<string, unknown>;
    const agg = String(metric.agg || "sum");
    const requestedColumn = String(metric.column || "");
    let column = requestedColumn;
    if (agg === "count" && !column) column = countFallback ?? "";
    if (!schema.has(column) || !AGGREGATIONS[agg]) return null;
    const alias = String(metric.as || `${requestedColumn}_${agg}`);
    if (!alias || sourceColumns.has(column) || aliases.has(alias)) return null;
    sourceColumns.add(column);
    aliases.add(alias);
    out.push({ column, agg, alias });
  }
  return out;
}

function compileAggregate(step: Step, schema: KnownSchema, input: string): StepSql | null {
  const by = stringList(step.params.by);
  if (!by || by.some((column) => !schema.has(column))) return null;
  const compiledMetrics = metrics(
    step.params.metrics,
    schema,
    by[0] ?? schema.columns[0],
  );
  if (!compiledMetrics) return null;
  if (!by.length && compiledMetrics.some((metric) =>
    metric.agg === "first" || metric.agg === "last")) {
    return null;
  }
  const output = [...by, ...compiledMetrics.map((metric) => metric.alias)];
  if (new Set(output).size !== output.length) return null;

  const selections = [
    ...by.map(quoteIdentifier),
    ...compiledMetrics.map((metric) =>
      `${aggregateExpression(metric.agg, metric.column)} AS ${quoteIdentifier(metric.alias)}`),
  ];
  const group = by.length
    ? ` GROUP BY ${by.map(quoteIdentifier).join(", ")}`
    : "";
  const nextSchema = schema.withColumns(output);
  if (!nextSchema) return null;
  return {
    sql: `SELECT ${selections.join(", ")} FROM ${input}${group}`,
    params: [],
    schema: nextSchema,
  };
}

type DeriveToken =
  | { type: "number"; value: string }
  | { type: "identifier"; value: string }
  | { type: "operator"; value: string }
  | { type: "left" }
  | { type: "right" }
  | { type: "comma" };

function tokenizeExpression(expression: string): DeriveToken[] | null {
  const tokens: DeriveToken[] = [];
  let index = 0;
  while (index < expression.length) {
    const rest = expression.slice(index);
    const whitespace = rest.match(/^\s+/);
    if (whitespace) {
      index += whitespace[0].length;
      continue;
    }
    const number = rest.match(/^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?/);
    if (number) {
      tokens.push({ type: "number", value: number[0] });
      index += number[0].length;
      continue;
    }
    const identifier = rest.match(/^[A-Za-z_][A-Za-z0-9_]*/);
    if (identifier) {
      tokens.push({ type: "identifier", value: identifier[0] });
      index += identifier[0].length;
      continue;
    }
    const character = rest[0];
    if ("+-*/".includes(character)) {
      tokens.push({ type: "operator", value: character });
    } else if (character === "(") {
      tokens.push({ type: "left" });
    } else if (character === ")") {
      tokens.push({ type: "right" });
    } else if (character === ",") {
      tokens.push({ type: "comma" });
    } else {
      return null;
    }
    index += 1;
  }
  return tokens.length ? tokens : null;
}

class ArithmeticParser {
  private index = 0;

  constructor(
    private readonly tokens: DeriveToken[],
    private readonly schema: KnownSchema,
  ) {}

  parse(): string | null {
    const output = this.additive();
    return output && this.index === this.tokens.length ? output : null;
  }

  private additive(): string | null {
    let left = this.multiplicative();
    if (!left) return null;
    while (this.operator("+", "-")) {
      const operator = (this.tokens[this.index - 1] as { value: string }).value;
      const right = this.multiplicative();
      if (!right) return null;
      left = `(${left} ${operator} ${right})`;
    }
    return left;
  }

  private multiplicative(): string | null {
    let left = this.unary();
    if (!left) return null;
    while (this.operator("*", "/")) {
      const operator = (this.tokens[this.index - 1] as { value: string }).value;
      const right = this.unary();
      if (!right) return null;
      left = `(${left} ${operator} ${right})`;
    }
    return left;
  }

  private unary(): string | null {
    if (this.operator("+", "-")) {
      const operator = (this.tokens[this.index - 1] as { value: string }).value;
      const value = this.unary();
      return value ? `(${operator}${value})` : null;
    }
    return this.primary();
  }

  private primary(): string | null {
    const token = this.tokens[this.index];
    if (!token) return null;
    if (token.type === "number") {
      this.index += 1;
      return token.value;
    }
    if (token.type === "identifier") {
      this.index += 1;
      if (this.tokens[this.index]?.type === "left") {
        return this.call(token.value);
      }
      return this.schema.has(token.value) ? quoteIdentifier(token.value) : null;
    }
    if (token.type === "left") {
      this.index += 1;
      const value = this.additive();
      if (!value || this.tokens[this.index]?.type !== "right") return null;
      this.index += 1;
      return `(${value})`;
    }
    return null;
  }

  private call(name: string): string | null {
    if (!["abs", "sqrt", "log"].includes(name.toLowerCase())) return null;
    this.index += 1;
    const args: string[] = [];
    if (this.tokens[this.index]?.type === "right") return null;
    for (;;) {
      const value = this.additive();
      if (!value) return null;
      args.push(value);
      if (this.tokens[this.index]?.type === "comma") {
        this.index += 1;
        continue;
      }
      if (this.tokens[this.index]?.type !== "right") return null;
      this.index += 1;
      break;
    }
    if (args.length !== 1) return null;
    const sqlName = name.toLowerCase() === "log" ? "ln" : name.toLowerCase();
    return `${sqlName}(${args[0]})`;
  }

  private operator(...values: string[]): boolean {
    const token = this.tokens[this.index];
    if (token?.type !== "operator" || !values.includes(token.value)) return false;
    this.index += 1;
    return true;
  }
}

function compileDerive(
  step: Step,
  schema: KnownSchema,
  input: string,
): StepSql | CompileFailure {
  const alias = String(step.params.as || "");
  const expression = String(step.params.expr || "");
  if (!alias || alias.length > 200 || expression.length > 400) {
    return failure("unsupported_expr");
  }
  const tokens = tokenizeExpression(expression);
  const derived = tokens ? new ArithmeticParser(tokens, schema).parse() : null;
  if (!derived) return failure("unsupported_expr");
  const output = schema.columns.filter((column) => column !== alias);
  output.push(alias);
  const nextSchema = schema.withColumns(output);
  if (!nextSchema) return failure("semantic_mismatch");
  const retained = schema.columns
    .filter((column) => column !== alias)
    .map(quoteIdentifier);
  return {
    sql: `SELECT ${[...retained, `${derived} AS ${quoteIdentifier(alias)}`].join(", ")} FROM ${input}`,
    params: [],
    schema: nextSchema,
  };
}

function calendarBucket(grain: string, column: string): string | null {
  const timestamp = `try_cast(${quoteIdentifier(column)} AS TIMESTAMP)`;
  switch (grain) {
    case "hour":
      return `date_trunc('hour', ${timestamp})`;
    case "day":
      return `date_trunc('day', ${timestamp})`;
    case "week":
      return `date_trunc('week', ${timestamp}) + INTERVAL 6 DAY`;
    case "month":
      return `last_day(${timestamp})`;
    case "quarter":
      return `date_trunc('quarter', ${timestamp}) + INTERVAL 3 MONTH - INTERVAL 1 DAY`;
    case "year":
      return `date_trunc('year', ${timestamp}) + INTERVAL 1 YEAR - INTERVAL 1 DAY`;
    default:
      return null;
  }
}

function compileResample(step: Step, schema: KnownSchema, input: string): StepSql | null {
  const timeColumn = schema.require(step.params.time_column);
  const by = stringList(step.params.by ?? []);
  const bucket = timeColumn
    ? calendarBucket(String(step.params.grain || "month"), timeColumn)
    : null;
  if (!timeColumn || !by || by.some((column) => !schema.has(column)) || !bucket) {
    return null;
  }
  const compiledMetrics = metrics(step.params.metrics, schema);
  if (!compiledMetrics) return null;
  const output = [timeColumn, ...by, ...compiledMetrics.map((metric) => metric.alias)];
  if (new Set(output).size !== output.length) return null;
  const bucketAlias = quoteIdentifier(timeColumn);
  const selections = [
    `${bucket} AS ${bucketAlias}`,
    ...by.map(quoteIdentifier),
    ...compiledMetrics.map((metric) =>
      `${aggregateExpression(metric.agg, metric.column)} AS ${quoteIdentifier(metric.alias)}`),
  ];
  const groups = [bucket, ...by.map(quoteIdentifier)];
  const nextSchema = schema.withColumns(output);
  return nextSchema ? {
    sql: `SELECT ${selections.join(", ")} FROM ${input} WHERE ${bucket} IS NOT NULL GROUP BY ${groups.join(", ")}`,
    params: [],
    schema: nextSchema,
  } : null;
}

function compileTopN(step: Step, schema: KnownSchema, input: string): StepSql | null {
  const category = schema.require(step.params.category);
  const measure = schema.require(step.params.measure);
  const n = integer(step.params.n, 10);
  const agg = String(step.params.agg || "sum");
  if (!category || !measure || !n || !AGGREGATIONS[agg]) return null;
  const categoryId = quoteIdentifier(category);
  const measureId = quoteIdentifier(measure);
  const aggregate = aggregateExpression(agg, measure);
  const nextSchema = schema.withColumns([category, measure]);
  if (!aggregate || !nextSchema) return null;

  return {
    sql: `WITH grouped AS (
      SELECT CAST(${categoryId} AS VARCHAR) AS category_value,
             ${aggregate} AS measure_value
      FROM ${input}
      GROUP BY ${categoryId}
    ), ranked AS (
      SELECT *, row_number() OVER (ORDER BY measure_value DESC NULLS LAST) AS rank_value,
             count(*) OVER () AS group_count
      FROM grouped
    ), combined AS (
      SELECT category_value, measure_value, rank_value FROM ranked WHERE rank_value <= ${n}
      UNION ALL
      SELECT 'Other', sum(measure_value), ${n + 1} FROM ranked
      WHERE rank_value > ${n} HAVING count(*) > 0
    )
    SELECT category_value AS ${categoryId}, measure_value AS ${measureId}
    FROM combined ORDER BY rank_value`,
    params: [],
    schema: nextSchema,
  };
}

function compilePivot(step: Step, schema: KnownSchema, input: string): StepSql | null {
  const index = schema.require(step.params.index);
  const columns = schema.require(step.params.columns);
  const values = schema.require(step.params.values);
  const agg = String(step.params.agg || "sum");
  if (!index || !columns || !values || !AGGREGATIONS[agg]) return null;
  const aggregate = aggregateExpression(agg, values);
  return aggregate ? {
    sql: `PIVOT ${input} ON ${quoteIdentifier(columns)} USING ${aggregate} GROUP BY ${quoteIdentifier(index)}`,
    params: [],
    // Pivot values become identifiers at execution time, so no later typed
    // step can safely refer to the resulting schema.
    schema: null,
  } : null;
}

function compileStep(
  step: Step,
  schema: KnownSchema,
  input: string,
): StepSql | CompileFailure {
  switch (step.type) {
    case "filter":
      return compileFilter(step, schema, input) ?? failure("semantic_mismatch");
    case "aggregate":
      return compileAggregate(step, schema, input) ?? failure("semantic_mismatch");
    case "derive":
      return compileDerive(step, schema, input);
    case "sort": {
      const column = schema.require(step.params.column);
      if (!column) return failure("semantic_mismatch");
      return {
        sql: `SELECT * FROM ${input} ORDER BY ${quoteIdentifier(column)} ${step.params.desc ? "DESC" : "ASC"} NULLS LAST`,
        params: [],
        schema,
      };
    }
    case "limit": {
      const n = integer(step.params.n, 100);
      return n
        ? { sql: `SELECT * FROM ${input} LIMIT ${n}`, params: [], schema }
        : failure("semantic_mismatch");
    }
    case "select": {
      const columns = stringList(step.params.columns);
      if (!columns || columns.some((column) => !schema.has(column))) {
        return failure("semantic_mismatch");
      }
      if (!columns.length) return { sql: `SELECT * FROM ${input}`, params: [], schema };
      const nextSchema = schema.withColumns(columns);
      return nextSchema
        ? {
            sql: `SELECT ${columns.map(quoteIdentifier).join(", ")} FROM ${input}`,
            params: [],
            schema: nextSchema,
          }
        : failure("semantic_mismatch");
    }
    case "rename": {
      if (!step.params.map || typeof step.params.map !== "object" || Array.isArray(step.params.map)) {
        return failure("semantic_mismatch");
      }
      const mapping = step.params.map as Record<string, unknown>;
      if (Object.keys(mapping).some((column) => !schema.has(column))) {
        return failure("semantic_mismatch");
      }
      const output = schema.columns.map((column) =>
        Object.hasOwn(mapping, column) ? String(mapping[column]) : column);
      const nextSchema = schema.withColumns(output);
      return nextSchema
        ? {
            sql: `SELECT ${schema.columns.map((column, index) =>
              `${quoteIdentifier(column)} AS ${quoteIdentifier(output[index])}`).join(", ")} FROM ${input}`,
            params: [],
            schema: nextSchema,
          }
        : failure("semantic_mismatch");
    }
    case "dropna": {
      const columns = stringList(step.params.columns ?? []);
      if (!columns || columns.some((column) => !schema.has(column))) {
        return failure("semantic_mismatch");
      }
      const checked = columns.length ? columns : schema.columns;
      return {
        sql: `SELECT * FROM ${input} WHERE ${checked.map((column) =>
          `${quoteIdentifier(column)} IS NOT NULL`).join(" AND ")}`,
        params: [],
        schema,
      };
    }
    case "resample":
      return compileResample(step, schema, input) ?? failure("semantic_mismatch");
    case "top_n":
      return compileTopN(step, schema, input) ?? failure("semantic_mismatch");
    case "pivot":
      return compilePivot(step, schema, input) ?? failure("semantic_mismatch");
    default:
      return failure("unsupported_step");
  }
}

export function compilePlan(
  tableName: string,
  sourceColumns: SchemaColumn[],
  steps: Step[],
): CompileResult {
  if (!/^reshape_[A-Za-z0-9_]+$/.test(tableName)) {
    return failure("semantic_mismatch");
  }
  if (steps.length > RESHAPE_LIMITS.maxSteps) return failure("unsupported_step");
  let schema = KnownSchema.from(sourceColumns);
  if (!schema) return failure("semantic_mismatch");

  const ctes: string[] = [];
  const params: unknown[] = [];
  let input = quoteIdentifier(tableName);
  const enabled = steps.filter((step) => step.enabled !== false);

  for (let index = 0; index < enabled.length; index += 1) {
    if (!schema) return failure("unsupported_step");
    const compiled = compileStep(enabled[index], schema, input);
    if ("ok" in compiled) return compiled;
    const name = `reshape_step_${index}`;
    ctes.push(`${quoteIdentifier(name)} AS (${compiled.sql})`);
    params.push(...compiled.params);
    schema = compiled.schema;
    input = quoteIdentifier(name);
  }

  const prefix = ctes.length ? `WITH ${ctes.join(",\n")}\n` : "";
  return {
    ok: true,
    sql: `${prefix}SELECT * FROM ${input} LIMIT ${RESHAPE_LIMITS.outputRows + 1}`,
    params,
    columns: schema?.columns ?? null,
  };
}

export function planIsLocallySupported(
  sourceColumns: SchemaColumn[],
  steps: Step[],
): CompileResult {
  return compilePlan("reshape_eligibility", sourceColumns, steps);
}
