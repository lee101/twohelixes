"""Homepage-only presentation. Real SVG output, no heavyweight chart runtime."""

from twohelixes.charts import helix, palette


CSS = """
.intelligence-home { --landing-ink:var(--text-primary); }
.intelligence-home .hero { position:relative; overflow:hidden; padding:72px 0 48px;
  background:radial-gradient(ellipse at 85% 25%,var(--accent-soft),transparent 65%); }
.intelligence-home .hero-layout { grid-template-columns:1fr 1fr; gap:36px; align-items:center; }
.intelligence-home .eyebrow { display:inline-flex; gap:9px; align-items:center;
  font:600 11px var(--mono); letter-spacing:.1em; text-transform:uppercase; }
.intelligence-home .eyebrow::before { content:""; width:7px; height:7px;
  border-radius:50%; background:var(--accent); box-shadow:0 0 0 4px var(--accent-soft); }
.intelligence-home .hero h1 { font-size:clamp(44px,5.6vw,72px); line-height:1.02;
  letter-spacing:-.06em; max-width:12ch; margin:24px 0; font-weight:650; }
.intelligence-home h1 em { font-family:Georgia,serif; font-weight:400; color:var(--accent-strong); }
.intelligence-home .hero .lede { font-size:17px; max-width:43ch; line-height:1.7; }
.intelligence-home .hero .btn { min-height:48px; padding-inline:22px; }
.intelligence-home .hero-note { max-width:43ch; color:var(--text-secondary); font-size:12px; }
.helix-scene { position:relative; min-height:440px; display:grid; place-items:center; }
.helix-scene::before { content:""; position:absolute; inset:5%; border:1px solid var(--border);
  border-radius:50%; transform:rotate(-24deg); }
.helix-scene::after { content:""; position:absolute; inset:15% -3%; border:1px dashed var(--border-strong);
  border-radius:50%; transform:rotate(25deg); pointer-events:none; }
.intelligence-helix { width:280px; height:430px; transform:rotate(22deg); }
.intelligence-helix svg { width:100%; height:100%; }
.orbit-card { position:absolute; z-index:2; background:var(--panel); border:1px solid var(--border);
  border-radius:12px; padding:14px 18px; box-shadow:var(--sh-2); font-size:12px; min-width:142px; }
.orbit-card small { display:block; font:10px var(--mono); letter-spacing:.08em;
  color:var(--text-secondary); text-transform:uppercase; margin-bottom:7px; }
.orbit-card strong { font-weight:600; }
.orbit-source { left:0; top:55px; }
.orbit-query { right:0; top:180px; }
.orbit-answer { left:24px; bottom:20px; }
.orbit-answer .mini-bars { display:flex; gap:5px; align-items:end; height:38px; margin-top:12px; }
.mini-bars i { width:20px; background:var(--accent); border-radius:3px 3px 0 0; }
.orbit-label { position:absolute; right:0; bottom:15px; font:10px var(--mono);
  color:var(--text-secondary); text-transform:uppercase; letter-spacing:.09em; }
.intelligence-home .source-strip { padding:23px 0; border-block:1px solid var(--border); }
.source-strip .shell { display:flex; flex-wrap:wrap; align-items:center; gap:16px 30px; }
.source-strip span { color:var(--text-secondary); font-size:12px; }
.source-strip b { font-size:13px; font-weight:550; }
.source-strip a { margin-left:auto; font-size:12px; }
.intelligence-home section { padding-block:64px; }
.intelligence-home .section-heading { display:flex; justify-content:space-between; gap:24px;
  align-items:end; margin-bottom:28px; }
.intelligence-home .section-heading h2 { font-size:clamp(28px,3.4vw,42px); letter-spacing:-.04em; max-width:20ch; }
.section-heading p.sub { font-size:14px; max-width:36ch; }
.workspace-preview { border:1px solid var(--border-strong); border-radius:16px;
  box-shadow:var(--sh-3); background:var(--panel); overflow:hidden; }
.workspace-bar { display:flex; gap:10px; align-items:center; padding:15px 22px;
  border-bottom:1px solid var(--border); font:11px var(--mono); color:var(--text-secondary); }
.workspace-bar .preview-label { margin-left:auto; }
.workspace-dot { width:7px; height:7px; border:1px solid var(--border-strong); border-radius:50%; }
.workspace-body { display:grid; grid-template-columns:250px minmax(0,1fr); }
.workspace-sidebar { background:var(--panel-2); border-right:1px solid var(--border); padding:24px; }
.workspace-sidebar .kicker { font-size:10px; }
.workspace-sidebar h3 { font-size:17px; margin:16px 0; letter-spacing:-.02em; }
.workspace-sidebar p { font-size:12px; color:var(--text-secondary); line-height:1.7; }
.workspace-sidebar ol { margin:24px 0; display:grid; gap:14px; }
.workspace-sidebar li { font-size:12px; }
.workspace-sidebar li::before { content:"✓"; color:var(--accent); margin-right:9px; }
.workspace-sidebar a { font-size:12px; font-weight:600; }
.workspace-main { min-width:0; padding:24px; }
.workspace-main .figure { border:0; box-shadow:none; background:transparent; }
.workspace-main .figure svg { width:100%; height:auto; }
.workspace-question { padding:13px 16px; border:1px solid var(--border); border-radius:8px;
  font-size:13px; margin-bottom:12px; background:var(--panel-2); }
.workspace-question span { color:var(--accent-strong); margin-right:8px; }
.workspace-insight { border-top:1px solid var(--border); padding-top:16px;
  font-size:12px; color:var(--text-secondary); line-height:1.7; }
.workspace-insight b { color:var(--text-primary); }
.intelligence-home .use-cases { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }
.use-case { border:1px solid var(--border); padding:26px; border-radius:12px; background:var(--panel); }
.use-case .case-icon { font:18px var(--mono); color:var(--accent-strong); margin-bottom:24px; }
.use-case h3 { font-size:18px; letter-spacing:-.03em; margin-bottom:10px; }
.use-case p { font-size:13px; color:var(--text-secondary); line-height:1.7; }
.use-case a { display:inline-block; font-size:12px; font-weight:600; margin-top:20px; }
.intelligence-home .model-note { margin-top:24px; display:flex; gap:12px 24px;
  flex-wrap:wrap; align-items:center; padding:20px 0; border-block:1px solid var(--border); }
.model-note b { font-size:13px; }
.model-note p { color:var(--text-secondary); font-size:12px; max-width:65ch; }
@media(max-width:899px) {
  .intelligence-home .hero { padding-top:42px; }
  .intelligence-home .hero-layout { grid-template-columns:1fr; gap:20px; }
  .intelligence-home .hero h1 { max-width:14ch; }
  .helix-scene { min-height:350px; max-width:500px; width:100%; margin:auto; }
  .intelligence-helix { height:330px; width:230px; }
  .intelligence-home .use-cases { grid-template-columns:1fr; }
  .workspace-body { grid-template-columns:190px minmax(0,1fr); }
  .workspace-sidebar,.workspace-main { padding:16px; }
}
@media(max-width:599px) {
  .intelligence-home .hero h1 { font-size:48px; }
  .intelligence-home .hero .lede { font-size:16px; }
  .helix-scene { min-height:330px; }
  .orbit-card { min-width:120px; padding:11px; font-size:11px; }
  .orbit-query { top:145px; }
  .orbit-source { top:28px; }
  .orbit-answer { left:0; }
  .orbit-label { font-size:8px; }
  .intelligence-home section { padding-block:40px; }
  .intelligence-home .section-heading { display:block; }
  .section-heading p.sub { margin-top:16px; }
  .workspace-body { grid-template-columns:1fr; }
  .workspace-sidebar { border-right:0; border-bottom:1px solid var(--border); }
  .workspace-sidebar ol { display:flex; flex-wrap:wrap; margin:14px 0; gap:10px; }
  .workspace-sidebar h3 { margin:8px 0; }
  .workspace-main { padding:12px; }
  .workspace-bar { padding:12px; font-size:9px; }
  .source-strip .shell { gap:12px 20px; }
  .source-strip span { flex-basis:100%; }
  .source-strip a { margin-left:0; }
}
"""


def artwork(mode: str) -> str:
    mode = "dark" if mode == "dark" else "light"
    style = helix.HelixStyle(width=280, height=430, turns=1.5,
                             strand_width=19, rung_width=2, rungs_per_turn=9,
                             strand_a=palette.BRAND[mode],
                             strand_b=palette.BRAND_LIGHT[mode], rung=palette.BRAND[mode])
    return helix.render(style, uid="intelligence-helix")
