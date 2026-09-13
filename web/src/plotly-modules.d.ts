declare module "plotly.js/lib/core" {
  interface PlotlyCore {
    register(traces: unknown[]): void;
    purge(host: HTMLElement): void;
    react(host: HTMLElement, data: unknown, layout: unknown, config: unknown): Promise<unknown>;
    relayout(host: HTMLElement, update: Record<string, unknown>): Promise<unknown>;
  }

  const Plotly: PlotlyCore;
  export default Plotly;
}

declare module "plotly.js/lib/bar" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/box" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/candlestick" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/choropleth" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/funnel" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/heatmap" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/histogram" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/indicator" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/pie" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/sankey" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/scattergeo" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/sunburst" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/table" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/treemap" {
  const trace: unknown;
  export default trace;
}

declare module "plotly.js/lib/waterfall" {
  const trace: unknown;
  export default trace;
}
