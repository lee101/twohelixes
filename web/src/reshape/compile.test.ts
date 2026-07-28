import { describe, expect, test } from "bun:test";
import { compilePlan } from "./compile";
import type { SchemaColumn, Step } from "./protocol";

const schema: SchemaColumn[] = [
  { name: "when", dtype: "datetime64[ns]", nulls: 0 },
  { name: "region", dtype: "object", nulls: 0 },
  { name: "sales", dtype: "float64", nulls: 0 },
  { name: "cost", dtype: "float64", nulls: 0 },
  { name: "odd \" name", dtype: "object", nulls: 0 },
];

function step(type: string, params: Record<string, unknown>): Step {
  return { id: `test-${type}`, type, params, enabled: true };
}

function compiled(steps: Step[]) {
  const result = compilePlan("reshape_fixture", schema, steps);
  expect(result.ok).toBe(true);
  if (!result.ok) throw new Error(result.fallbackReason);
  return result;
}

describe("typed reshape compiler", () => {
  test("quotes known identifiers and binds scalar filter values", () => {
    const value = `north' OR TRUE --`;
    const result = compiled([
      step("filter", { column: "odd \" name", op: "eq", value }),
    ]);
    expect(result.sql).toContain(`"odd "" name" = ?`);
    expect(result.sql).not.toContain(value);
    expect(result.params).toEqual([value]);
  });

  test("binds every set and text filter value", () => {
    const setResult = compiled([
      step("filter", { column: "region", op: "not_in", value: ["north", "south"] }),
    ]);
    expect(setResult.sql).toContain(`NOT (coalesce("region" IN (?, ?), FALSE))`);
    expect(setResult.params).toEqual(["north", "south"]);

    const textResult = compiled([
      step("filter", { column: "region", op: "contains", value: "or" }),
    ]);
    expect(textResult.sql).toContain(
      `contains(lower(CAST("region" AS VARCHAR)), lower(?))`,
    );
    expect(textResult.params).toEqual(["or"]);
  });

  test("covers comparison, text, set, and empty-output filter forms", () => {
    for (const [op, operator] of Object.entries({
      eq: "=",
      ne: "<>",
      gt: ">",
      ge: ">=",
      lt: "<",
      le: "<=",
    })) {
      const result = compiled([
        step("filter", { column: "sales", op, value: 10 }),
      ]);
      expect(result.sql).toContain(`"sales" ${operator} ?`);
      expect(result.params).toEqual([10]);
    }

    expect(compiled([
      step("filter", { column: "region", op: "starts_with", value: "n" }),
    ]).sql).toContain(`starts_with(CAST("region" AS VARCHAR), ?)`);
    expect(compiled([
      step("filter", { column: "region", op: "ends_with", value: "h" }),
    ]).sql).toContain(`ends_with(CAST("region" AS VARCHAR), ?)`);

    const empty = compiled([
      step("filter", { column: "region", op: "in", value: [] }),
    ]);
    expect(empty.sql).toContain("WHERE FALSE");
    expect(empty.params).toEqual([]);

    const nullMember = compiled([
      step("filter", { column: "region", op: "in", value: [null, "north"] }),
    ]);
    expect(nullMember.sql).toContain(`"region" IS NULL`);
    expect(nullMember.params).toEqual(["north"]);
  });

  test("compiles null filters and drop-null semantics", () => {
    const isNull = compiled([
      step("filter", { column: "sales", op: "is_null" }),
      step("dropna", { columns: ["region", "cost"] }),
    ]);
    expect(isNull.sql).toContain(`"sales" IS NULL`);
    expect(isNull.sql).toContain(`"region" IS NOT NULL AND "cost" IS NOT NULL`);

    const allColumns = compiled([step("dropna", { columns: [] })]);
    for (const column of schema) {
      const quoted = `"${column.name.replaceAll('"', '""')}" IS NOT NULL`;
      expect(allColumns.sql).toContain(quoted);
    }
  });

  test("compiles aggregate names and aliases", () => {
    const result = compiled([
      step("aggregate", {
        by: ["region"],
        metrics: [
          { column: "sales", agg: "sum", as: "revenue" },
          { column: "cost", agg: "mean", as: "average_cost" },
        ],
      }),
    ]);
    expect(result.sql).toContain(`coalesce(sum("sales"), 0) AS "revenue"`);
    expect(result.sql).toContain(`avg("cost") AS "average_cost"`);
    expect(result.sql).toContain(`GROUP BY "region"`);
    expect(result.columns).toEqual(["region", "revenue", "average_cost"]);
  });

  test("matches the backend alias for a blank count column", () => {
    const result = compiled([
      step("aggregate", {
        by: [],
        metrics: [{ column: "", agg: "count" }],
      }),
    ]);
    expect(result.sql).toContain(`count("when") AS "_count"`);
    expect(result.columns).toEqual(["_count"]);
  });

  test("uses pandas-compatible aggregation spellings", () => {
    const aggregations = [
      ["median", `median("sales")`],
      ["count", `count("sales")`],
      ["nunique", `count(DISTINCT "sales")`],
      ["min", `min("sales")`],
      ["max", `max("sales")`],
      ["std", `stddev_samp("sales")`],
      ["first", `first("sales") FILTER (WHERE "sales" IS NOT NULL)`],
      ["last", `last("sales") FILTER (WHERE "sales" IS NOT NULL)`],
    ];
    for (const [agg, sql] of aggregations) {
      expect(compiled([
        step("aggregate", {
          by: ["region"],
          metrics: [{ column: "sales", agg, as: `value_${agg}` }],
        }),
      ]).sql).toContain(sql);
    }
  });

  test("compiles calendar grains with pandas period endpoints", () => {
    const expected: Record<string, string> = {
      hour: `date_trunc('hour'`,
      day: `date_trunc('day'`,
      week: `INTERVAL 6 DAY`,
      month: "last_day(",
      quarter: `INTERVAL 3 MONTH - INTERVAL 1 DAY`,
      year: `INTERVAL 1 YEAR - INTERVAL 1 DAY`,
    };
    for (const [grain, fragment] of Object.entries(expected)) {
      const result = compiled([
        step("resample", {
          time_column: "when",
          grain,
          by: ["region"],
          metrics: [{ column: "sales", agg: "sum", as: "revenue" }],
        }),
      ]);
      expect(result.sql).toContain(fragment);
      expect(result.columns).toEqual(["when", "region", "revenue"]);
    }
  });

  test("compiles top-N with an explicit conditional Other bucket", () => {
    const result = compiled([
      step("top_n", {
        category: "region",
        measure: "sales",
        n: 3,
        agg: "sum",
      }),
    ]);
    expect(result.sql).toContain(`SELECT 'Other', sum(measure_value), 4`);
    expect(result.sql).toContain("WHERE rank_value > 3 HAVING count(*) > 0");
    expect(result.columns).toEqual(["region", "sales"]);
  });

  test("compiles sort, limit, select, and rename against the evolving schema", () => {
    const result = compiled([
      step("rename", { map: { sales: "revenue" } }),
      step("sort", { column: "revenue", desc: true }),
      step("limit", { n: 12 }),
      step("select", { columns: ["region", "revenue"] }),
    ]);
    expect(result.sql).toContain(`"sales" AS "revenue"`);
    expect(result.sql).toContain(`ORDER BY "revenue" DESC NULLS LAST`);
    expect(result.sql).toContain("LIMIT 12");
    expect(result.columns).toEqual(["region", "revenue"]);
  });

  test("compiles a terminal pivot", () => {
    const result = compiled([
      step("pivot", {
        index: "when",
        columns: "region",
        values: "sales",
        agg: "sum",
      }),
    ]);
    expect(result.sql).toContain(
      `PIVOT "reshape_fixture" ON "region" USING coalesce(sum("sales"), 0) GROUP BY "when"`,
    );
    expect(result.columns).toBeNull();
  });

  test("permits only parsed arithmetic derive expressions", () => {
    const result = compiled([
      step("derive", { as: "margin", expr: "sqrt(abs((sales - cost) / sales * 100))" }),
    ]);
    expect(result.sql).toContain(`sqrt(abs(`);
    expect(result.sql).toContain(`AS "margin"`);

    for (const expr of [
      "sales; DROP TABLE source",
      "unknown + 1",
      "where(sales > 0, sales, 0)",
      "round(sales, 2)",
      "sales % cost",
      "sales.__class__",
    ]) {
      const unsupported = compilePlan("reshape_fixture", schema, [
        step("derive", { as: "unsafe", expr }),
      ]);
      expect(unsupported).toEqual({ ok: false, fallbackReason: "unsupported_expr" });
    }
  });

  test("rejects unknown columns, steps after a pivot, and duplicate metric sources", () => {
    expect(compilePlan("reshape_fixture", schema, [
      step("filter", { column: "missing", op: "eq", value: 1 }),
    ])).toEqual({ ok: false, fallbackReason: "semantic_mismatch" });

    expect(compilePlan("reshape_fixture", schema, [
      step("pivot", {
        index: "when",
        columns: "region",
        values: "sales",
        agg: "sum",
      }),
      step("limit", { n: 2 }),
    ])).toEqual({ ok: false, fallbackReason: "unsupported_step" });

    expect(compilePlan("reshape_fixture", schema, [
      step("aggregate", {
        by: ["region"],
        metrics: [
          { column: "sales", agg: "sum", as: "one" },
          { column: "sales", agg: "max", as: "two" },
        ],
      }),
    ])).toEqual({ ok: false, fallbackReason: "semantic_mismatch" });
  });

  test("disabled steps do not change the plan", () => {
    const result = compiled([{
      ...step("filter", { column: "missing", op: "eq", value: "ignored" }),
      enabled: false,
    }]);
    expect(result.params).toEqual([]);
    expect(result.columns).toEqual(schema.map((column) => column.name));
  });
});
