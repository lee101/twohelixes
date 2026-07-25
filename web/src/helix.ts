/**
 * The double-helix mark, generated in the browser.
 *
 * Same construction as the server-side generator: two strands in opposition,
 * depth encoded as a periodic opacity gradient along the axis, and the spinner
 * translating by exactly one period so the loop is seamless. Duplicated here
 * rather than fetched so a spinner never waits on a network round trip.
 */

const MIN_ALPHA = 0.16;
const STOPS_PER_TURN = 10;

export interface HelixOptions {
  size?: number;
  turns?: number;
  amplitude?: number;
  strokeWidth?: number;
  strandA?: string;
  strandB?: string;
  animated?: boolean;
  duration?: number;
  minAlpha?: number;
}

let uidCounter = 0;

export function helixSvg(options: HelixOptions = {}): string {
  const size = options.size ?? 48;
  const baseTurns = options.turns ?? 1.5;
  const amplitude = options.amplitude ?? 0.36;
  const strokeWidth = options.strokeWidth ?? Math.max(2, size / 16);
  const animated = options.animated ?? false;
  const duration = options.duration ?? 1.6;
  const minAlpha = options.minAlpha ?? MIN_ALPHA;
  const strandA = options.strandA ?? "var(--series-1, #2a78d6)";
  const strandB = options.strandB ?? "var(--series-3, #1baf7a)";
  const uid = `th${++uidCounter}`;

  // One extra period gives the animation something to translate into.
  const turns = baseTurns + (animated ? 1 : 0);
  const span = size * (turns / baseTurns);
  const inset = strokeWidth / 2 + 0.5;
  const usable = Math.max(1, size - 2 * inset);

  const point = (t: number, phase: number): [number, number] => {
    const theta = t * baseTurns * 2 * Math.PI + phase;
    return [size / 2 + Math.sin(theta) * amplitude * usable, t * span];
  };

  const path = (phase: number): string => {
    const steps = Math.max(12, Math.round(40 * turns));
    const points: string[] = [];
    for (let i = 0; i <= steps; i++) {
      const [x, y] = point((i / steps) * (turns / baseTurns), phase);
      points.push(`${x.toFixed(2)} ${y.toFixed(2)}`);
    }
    return `M ${points.join(" L ")}`;
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
    `<path d="${path(0)}" stroke="url(#${uid}-a)" stroke-width="${strokeWidth}" ` +
    `stroke-linecap="round" stroke-linejoin="round"/>` +
    `<path d="${path(Math.PI)}" stroke="url(#${uid}-b)" stroke-width="${strokeWidth}" ` +
    `stroke-linecap="round" stroke-linejoin="round"/>` +
    `</g></g></svg>`
  );
}

export function spinner(size = 40): HTMLElement {
  const wrap = document.createElement("span");
  wrap.className = "th-spinner";
  wrap.setAttribute("role", "status");
  wrap.setAttribute("aria-live", "polite");
  wrap.innerHTML = helixSvg({ size, animated: true });
  return wrap;
}

export function logo(size = 30): HTMLElement {
  const wrap = document.createElement("span");
  wrap.className = "th-logo";
  wrap.innerHTML = helixSvg({ size, turns: 2, animated: false });
  return wrap;
}
