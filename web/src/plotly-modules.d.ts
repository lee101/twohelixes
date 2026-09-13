/** Plotly's per-trace CommonJS entrypoints do not publish TypeScript types. */
declare module "plotly.js/lib/*" {
  const module: any;
  export default module;
}
