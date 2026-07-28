/**
 * The double-helix mark, generated in the browser.
 *
 * Same construction as the server-side generator (`charts/helix.py` - keep the
 * two in step): two strands in opposition, each drawn as a filled ribbon whose
 * width swells at the front of the twist and pinches at the back, depth also
 * encoded as a periodic opacity gradient along the axis, and the spinner
 * translating by exactly one period so the loop is seamless. Duplicated here
 * rather than fetched so a spinner never waits on a network round trip.
 *
 * Colours come from the brand custom properties, so the mark follows the theme
 * without a second render.
 */

const MIN_ALPHA = 0.22;
const MIN_WIDTH_SCALE = 0.38;
const RUNG_ALPHA_SCALE = 0.3;
const STOPS_PER_TURN = 12;

export interface HelixOptions {
  size?: number;
  turns?: number;
  amplitude?: number;
  strokeWidth?: number;
  rungsPerTurn?: number;
  strandA?: string;
  strandB?: string;
  rung?: string;
  animated?: boolean;
  duration?: number;
  minAlpha?: number;
  taperEnds?: boolean;
}

let uidCounter = 0;

export function helixSvg(options: HelixOptions = {}): string {
  const size = options.size ?? 48;
  const baseTurns = options.turns ?? 1.75;
  const amplitude = options.amplitude ?? 0.33;
  const strokeWidth = options.strokeWidth ?? size / 11;
  const rungsPerTurn = options.rungsPerTurn ?? 3;
  const animated = options.animated ?? false;
  const duration = options.duration ?? 1.6;
  const minAlpha = options.minAlpha ?? MIN_ALPHA;
  const taperEnds = options.taperEnds ?? !animated;
  const strandA = options.strandA ?? "var(--brand, #2a78d6)";
  const strandB = options.strandB ?? "var(--brand-light, #6da7ec)";
  const rungColor = options.rung ?? "var(--brand, #2a78d6)";
  const uid = `th${++uidCounter}`;

  // One extra period gives the animation something to translate into.
  const turns = baseTurns + (animated ? 1 : 0);
  const span = size * (turns / baseTurns);
  const inset = strokeWidth / 2 + 0.5;
  const usable = Math.max(1, size - 2 * inset);
  const swing = amplitude * usable;

  const point = (t: number, phase: number) => {
    const theta = t * baseTurns * 2 * Math.PI + phase;
    return {
      x: size / 2 + Math.sin(theta) * swing,
      y: t * span,
      dx: Math.cos(theta) * swing * baseTurns * 2 * Math.PI,
      dy: span,
      theta,
    };
  };

  const halfWidth = (t: number, theta: number): number => {
    const depth = (Math.cos(theta) + 1) / 2;
    let half = (strokeWidth / 2) * (MIN_WIDTH_SCALE + (1 - MIN_WIDTH_SCALE) * depth);
    if (taperEnds) {
      const ease = 0.07;
      const edge = Math.min(t, 1 - t);
      if (edge < ease) half *= (1 - Math.cos((Math.PI * edge) / ease)) / 2;
    }
    return half;
  };

  const ribbon = (phase: number): string => {
    const steps = Math.max(24, Math.round(48 * turns));
    const forward: string[] = [];
    const backward: string[] = [];
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const p = point(t * (turns / baseTurns), phase);
      const length = Math.hypot(p.dx, p.dy) || 1;
      const nx = p.dy / length;
      const ny = -p.dx / length;
      const half = halfWidth(t, p.theta);
      forward.push(`${(p.x + nx * half).toFixed(2)} ${(p.y + ny * half).toFixed(2)}`);
      backward.push(`${(p.x - nx * half).toFixed(2)} ${(p.y - ny * half).toFixed(2)}`);
    }
    backward.reverse();
    return `M ${forward.concat(backward).join(" L ")} Z`;
  };

  const gradient = (phase: number, color: string, name: string): string => {
    const count = Math.max(6, Math.round(STOPS_PER_TURN * turns));
    const stops: string[] = [];
    for (let i = 0; i <= count; i++) {
      const fraction = i / count;
      const theta = fraction * turns * 2 * Math.PI + phase;
      const alpha = minAlpha + (1 - minAlpha) * ((Math.cos(theta) + 1) / 2);
      stops.push(
        `<stop offset="${fraction.toFixed(4)}" stop-color="${color}" stop-opacity="${alpha.toFixed(3)}"/>`,
      );
    }
    return (
      `<linearGradient id="${uid}-${name}" gradientUnits="userSpaceOnUse" ` +
      `x1="0" y1="0" x2="0" y2="${span.toFixed(2)}">${stops.join("")}</linearGradient>`
    );
  };

  const rungs: string[] = [];
  if (rungsPerTurn > 0) {
    const count = Math.max(2, Math.round(rungsPerTurn * turns));
    for (let i = 0; i < count; i++) {
      // Half-step, so no rung lands on an end and draws a shelf across the frame.
      const t = ((i + 0.5) / count) * (turns / baseTurns);
      const a = point(t, 0);
      const b = point(t, Math.PI);
      const separation = Math.abs(Math.sin(a.theta));
      if (separation < 0.4) continue;
      rungs.push(
        `<path d="M ${a.x.toFixed(2)} ${a.y.toFixed(2)} L ${b.x.toFixed(2)} ${b.y.toFixed(2)}" ` +
          `stroke="${rungColor}" stroke-width="${Math.max(0.9, size / 52).toFixed(2)}" ` +
          `stroke-opacity="${(separation * RUNG_ALPHA_SCALE).toFixed(3)}" stroke-linecap="round"/>`,
      );
    }
  }

  const style = animated
    ? `<style>@keyframes ${uid}-t{from{transform:translate(0,0)}to{transform:translate(0,${-size}px)}}` +
      `.${uid}-s{animation:${uid}-t ${duration}s linear infinite}` +
      `@media(prefers-reduced-motion:reduce){.${uid}-s{animation-duration:${duration * 4}s}}</style>`
    : "";

  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" ` +
    `width="${size}" height="${size}" fill="none" role="img" ` +
    `aria-label="${animated ? "Loading" : "twoHelixes"}">` +
    `<defs>${gradient(0, strandA, "a")}${gradient(Math.PI, strandB, "b")}` +
    `<clipPath id="${uid}-c"><rect width="${size}" height="${size}"/></clipPath></defs>` +
    style +
    `<g clip-path="url(#${uid}-c)"><g class="${animated ? `${uid}-s` : ""}">` +
    rungs.join("") +
    `<path d="${ribbon(0)}" fill="url(#${uid}-a)"/>` +
    `<path d="${ribbon(Math.PI)}" fill="url(#${uid}-b)"/>` +
    `</g></g></svg>`
  );
}

export function spinner(size = 40): HTMLElement {
  const wrap = document.createElement("span");
  wrap.className = "th-spinner";
  wrap.setAttribute("role", "status");
  wrap.setAttribute("aria-live", "polite");
  // An 18px inline spinner is the same drawing problem as the small logo: at
  // that size a strand fading to 0.24 is invisible for half its length.
  const small = size < 28;
  wrap.innerHTML = helixSvg({
    size,
    turns: small ? 1 : 1.5,
    amplitude: 0.35,
    strokeWidth: small ? size / 7 : size / 10,
    rungsPerTurn: small ? 0 : 3,
    minAlpha: small ? 0.4 : 0.24,
    animated: true,
  });
  return wrap;
}

export function logo(size = 30): HTMLElement {
  const wrap = document.createElement("span");
  wrap.className = "th-logo";
  // Below 30px the fine build is four crossings inside 26 pixels and reads as
  // texture, so the small mark trades turns for stroke and drops the rungs.
  const small = size < 30;
  wrap.innerHTML = helixSvg({
    size,
    turns: small ? 1.25 : 1.75,
    amplitude: small ? 0.35 : 0.33,
    strokeWidth: small ? size / 6.5 : size / 11,
    rungsPerTurn: small ? 0 : 3,
    minAlpha: small ? 0.4 : 0.26,
  });
  return wrap;
}
