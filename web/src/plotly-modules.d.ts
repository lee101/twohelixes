/* Plotly publishes its trace entry points without TypeScript declarations.
 * The app deliberately imports those modules individually to avoid shipping
 * the full Plotly bundle, so describe the package boundary once here. */
declare module "plotly.js/lib/*" {
  const plotlyModule: any;
  export default plotlyModule;
}
