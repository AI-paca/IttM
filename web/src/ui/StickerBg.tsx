import React, { useEffect, useRef, useState, useId } from "react";

export type Corner = "tr" | "tl" | "br" | "bl";
export type Shape = "circle" | "rect";

function clamp(v: number, min: number, max: number) {
  return Math.max(min, Math.min(max, v));
}

function easeInOut(t: number) {
  return t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
}

function getCornerPoints(
  x: number,
  y: number,
  w: number,
  h: number,
  corner: Corner,
  dx: number,
  dy: number,
) {
  switch (corner) {
    case "tr":
      return {
        A: [x + w - dx, y] as [number, number],
        B: [x + w, y + dy] as [number, number],
        C: [x + w, y] as [number, number],
      };
    case "tl":
      return {
        A: [x + dx, y] as [number, number],
        B: [x, y + dy] as [number, number],
        C: [x, y] as [number, number],
      };
    case "br":
      return {
        A: [x + w - dx, y + h] as [number, number],
        B: [x + w, y + h - dy] as [number, number],
        C: [x + w, y + h] as [number, number],
      };
    case "bl":
      return {
        A: [x + dx, y + h] as [number, number],
        B: [x, y + h - dy] as [number, number],
        C: [x, y + h] as [number, number],
      };
  }
}

function foldInward(
  p: [number, number],
  a: [number, number],
  b: [number, number],
): [number, number] {
  const abx = b[0] - a[0],
    aby = b[1] - a[1];
  const apx = p[0] - a[0],
    apy = p[1] - a[1];
  const len2 = abx * abx + aby * aby;
  if (len2 === 0) return p;
  const t = (apx * abx + apy * aby) / len2;
  const proj: [number, number] = [a[0] + t * abx, a[1] + t * aby];
  return [proj[0] - (p[0] - proj[0]), proj[1] - (p[1] - proj[1])];
}

function getCrease(
  A: [number, number],
  B: [number, number],
  C: [number, number],
  curve: number,
) {
  const mx = (A[0] + B[0]) / 2,
    my = (A[1] + B[1]) / 2;
  const dxC = C[0] - mx,
    dyC = C[1] - my;
  const len = Math.hypot(dxC, dyC) || 1;
  const nx = dxC / len,
    ny = dyC / len;
  const off = curve * len;
  const c1: [number, number] = [
    A[0] + (B[0] - A[0]) * 0.33 + nx * off * 0.4,
    A[1] + (B[1] - A[1]) * 0.33 + ny * off * 0.4,
  ];
  const c2: [number, number] = [
    A[0] + (B[0] - A[0]) * 0.66 + nx * off * 0.4,
    A[1] + (B[1] - A[1]) * 0.66 + ny * off * 0.4,
  ];
  return {
    c1,
    c2,
    path: `M ${A[0]} ${A[1]} C ${c1[0]} ${c1[1]} ${c2[0]} ${c2[1]} ${B[0]} ${B[1]}`,
  };
}

function buildFullPath(x: number, y: number, w: number, h: number, r: number) {
  const R = Math.min(r, w / 2, h / 2);
  return `M ${x + R} ${y} L ${x + w - R} ${y} Q ${x + w} ${y} ${x + w} ${y + R} L ${x + w} ${y + h - R} Q ${x + w} ${y + h} ${x + w - R} ${y + h} L ${x + R} ${y + h} Q ${x} ${y + h} ${x} ${y + h - R} L ${x} ${y + R} Q ${x} ${y} ${x + R} ${y} Z`;
}

function buildMainPath(
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
  A: [number, number],
  B: [number, number],
  corner: Corner,
) {
  if (corner === "tr")
    return `M ${A[0]} ${A[1]} L ${B[0]} ${B[1]} L 10000 ${B[1]} L 10000 10000 L -10000 10000 L -10000 ${A[1]} Z`;
  if (corner === "tl")
    return `M ${A[0]} ${A[1]} L ${B[0]} ${B[1]} L -10000 ${B[1]} L -10000 10000 L 10000 10000 L 10000 ${A[1]} Z`;
  if (corner === "br")
    return `M ${A[0]} ${A[1]} L ${B[0]} ${B[1]} L 10000 ${B[1]} L 10000 -10000 L -10000 -10000 L -10000 ${A[1]} Z`;
  return `M ${A[0]} ${A[1]} L ${B[0]} ${B[1]} L -10000 ${B[1]} L -10000 -10000 L 10000 -10000 L 10000 ${A[1]} Z`; // bl
}

function buildFoldOuterEdge(
  A: [number, number],
  B: [number, number],
  Cprime: [number, number],
  curve: number,
) {
  const bulge = curve * 1.4;
  const abx = B[0] - A[0],
    aby = B[1] - A[1];
  const len = Math.hypot(abx, aby) || 1;
  const nx = -aby / len,
    ny = abx / len;
  const mid: [number, number] = [(A[0] + B[0]) / 2, (A[1] + B[1]) / 2];
  const sign =
    (Cprime[0] - mid[0]) * nx + (Cprime[1] - mid[1]) * ny > 0 ? 1 : -1;
  const q1x = (A[0] + Cprime[0]) / 2 + sign * nx * bulge * 15;
  const q1y = (A[1] + Cprime[1]) / 2 + sign * ny * bulge * 15;
  const q2x = (Cprime[0] + B[0]) / 2 + sign * nx * bulge * 15;
  const q2y = (Cprime[1] + B[1]) / 2 + sign * ny * bulge * 15;
  return `M ${A[0]} ${A[1]} Q ${q1x} ${q1y} ${Cprime[0]} ${Cprime[1]} Q ${q2x} ${q2y} ${B[0]} ${B[1]}`;
}

function buildFoldPath(
  A: [number, number],
  B: [number, number],
  C: [number, number],
  Cprime: [number, number],
  curve: number,
) {
  const cr = getCrease(A, B, C, curve);
  const edge = buildFoldOuterEdge(A, B, Cprime, curve);
  return `${edge} C ${cr.c2[0]} ${cr.c2[1]} ${cr.c1[0]} ${cr.c1[1]} ${A[0]} ${A[1]} Z`;
}

function buildFoldShadow(
  A: [number, number],
  B: [number, number],
  C: [number, number],
  curve: number,
) {
  const cr = getCrease(A, B, C, curve);
  const off = -4;
  const dxC = C[0] - (A[0] + B[0]) / 2,
    dyC = C[1] - (A[1] + B[1]) / 2;
  const len = Math.hypot(dxC, dyC) || 1;
  const nx = dxC / len,
    ny = dyC / len;
  const A2: [number, number] = [A[0] + nx * off, A[1] + ny * off];
  const B2: [number, number] = [B[0] + nx * off, B[1] + ny * off];
  return `M ${A2[0]} ${A2[1]} C ${cr.c1[0] + nx * off} ${cr.c1[1] + ny * off} ${cr.c2[0] + nx * off} ${cr.c2[1] + ny * off} ${B2[0]} ${B2[1]} C ${cr.c2[0]} ${cr.c2[1]} ${cr.c1[0]} ${cr.c1[1]} ${A2[0]} ${A2[1]} Z`;
}

function buildHighlight(
  A: [number, number],
  Cprime: [number, number],
  B: [number, number],
) {
  const t = 0.15;
  const p1: [number, number] = [
    A[0] + (Cprime[0] - A[0]) * t,
    A[1] + (Cprime[1] - A[1]) * t,
  ];
  const p2: [number, number] = [
    B[0] + (Cprime[0] - B[0]) * t,
    B[1] + (Cprime[1] - B[1]) * t,
  ];
  return `M ${p1[0]} ${p1[1]} L ${p2[0]} ${p2[1]}`;
}

export type AnimMode = "peel" | "roll" | "none";

export interface StickerBgProps {
  peeled: boolean;
  active: boolean;
  className?: string;
  children?: React.ReactNode;

  corner?: Corner;
  baseDx?: number;
  baseDy?: number;
  curve?: number;
  shadow?: number;
  r?: number;
  animMode?: AnimMode;
}

const CORNER_DIR: Record<Corner, [number, number]> = {
  tr: [1, -1],
  tl: [-1, -1],
  br: [1, 1],
  bl: [-1, 1],
};

export function StickerBg({
  peeled,
  active,
  className,
  children,
  corner = "tr",
  baseDx = 54,
  baseDy = 22,
  curve = 0.15,
  shadow = 0.33,
  r = 12,
  animMode = "roll",
}: StickerBgProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const idPrefix = useId().replace(/:/g, "");

  const [anim, setAnim] = useState(0);
  const animRef = useRef(0);
  const rafRef = useRef(0);

  useEffect(() => {
    if (!containerRef.current) return;
    const ob = new ResizeObserver((entries) => {
      for (const btn of entries) {
        setSize({ w: btn.contentRect.width, h: btn.contentRect.height });
      }
    });
    ob.observe(containerRef.current);
    const initialRect = containerRef.current.getBoundingClientRect();
    setSize({ w: initialRect.width, h: initialRect.height });
    return () => ob.disconnect();
  }, []);

  useEffect(() => {
    const target = peeled ? 1 : 0;
    const DURATION = 450;
    const startVal = animRef.current;
    const startTime = performance.now();
    cancelAnimationFrame(rafRef.current);
    const tick = (now: number) => {
      const t = Math.min(1, (now - startTime) / DURATION);
      const val = startVal + (target - startVal) * easeInOut(t);
      animRef.current = val;
      setAnim(val);
      if (t < 1) rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [peeled]);

  const { w, h } = size;
  if (w === 0 || h === 0)
    return (
      <div ref={containerRef} className={`absolute inset-0 ${className || ""}`}>
        {children}
      </div>
    );

  const diag = Math.hypot(w, h);
  const maxPeel = diag * 1.6;
  const foldT = Math.min(anim / 0.6, 1);

  const dxAnim =
    animMode === "peel" ? baseDx + (maxPeel - baseDx) * foldT : baseDx;
  const dyAnim =
    animMode === "peel" ? baseDy + (maxPeel - baseDy) * foldT : baseDy;

  const { A, B, C } = getCornerPoints(0, 0, w, h, corner, dxAnim, dyAnim);
  const Cprime = foldInward(C, A, B);

  const fullPath = buildFullPath(0, 0, w, h, r);
  const mainPath = buildMainPath(0, 0, w, h, r, A, B, corner);
  const foldPath = buildFoldPath(A, B, C, Cprime, curve);
  const foldOuterEdgePath = buildFoldOuterEdge(A, B, Cprime, curve);
  const shadowPath = buildFoldShadow(A, B, C, curve);
  const highlightPath = buildHighlight(A, Cprime, B);
  const crease = getCrease(A, B, C, curve);

  const scx = w / 2;
  const scy = h / 2;
  const dir = CORNER_DIR[corner];
  const axisAngle = Math.atan2(dir[1], dir[0]) + Math.PI / 2;
  const axisX = Math.cos(axisAngle);
  const axisY = Math.sin(axisAngle);
  const STRIPS = 18;

  const rollStrips: {
    path: string;
    transform: string;
    opacity: number;
    shade: number;
  }[] = [];
  if (animMode === "roll" && anim > 0) {
    const perpX = -dir[1];
    const perpY = dir[0];

    for (let i = 0; i < STRIPS; i++) {
      const t0 = i / STRIPS;
      const t1 = (i + 1) / STRIPS;
      const tMid = (t0 + t1) / 2;

      const rollFront = anim;
      const localT = clamp((rollFront - (1 - tMid)) / 0.5, 0, 1);

      if (localT <= 0) continue;

      const rollAngle = localT * Math.PI;
      const cosA = Math.cos(rollAngle);
      const shade = cosA > 0 ? 1 - cosA * 0.5 : 0;
      const scaleAlongPerp = Math.abs(cosA);
      const isBack = cosA < 0;

      const px0 =
        scx + perpX * (t0 - 0.5) * (Math.abs(perpX) * w + Math.abs(perpY) * h);
      const py0 =
        scy + perpY * (t0 - 0.5) * (Math.abs(perpX) * w + Math.abs(perpY) * h);
      const px1 =
        scx + perpX * (t1 - 0.5) * (Math.abs(perpX) * w + Math.abs(perpY) * h);
      const py1 =
        scy + perpY * (t1 - 0.5) * (Math.abs(perpX) * w + Math.abs(perpY) * h);

      const hw = (Math.abs(axisX) * w + Math.abs(axisY) * h) / 2;
      const corners: [number, number][] = [
        [px0 - axisX * hw, py0 - axisY * hw],
        [px0 + axisX * hw, py0 + axisY * hw],
        [px1 + axisX * hw, py1 + axisY * hw],
        [px1 - axisX * hw, py1 - axisY * hw],
      ];
      const stripPath = `M ${corners[0][0]} ${corners[0][1]} L ${corners[1][0]} ${corners[1][1]} L ${corners[2][0]} ${corners[2][1]} L ${corners[3][0]} ${corners[3][1]} Z`;

      const midX = (px0 + px1) / 2;
      const midY = (py0 + py1) / 2;
      const transform = `translate(${midX} ${midY}) scale(${
        Math.abs(perpX) < 0.5 ? 1 : scaleAlongPerp
      } ${
        Math.abs(perpY) < 0.5 ? 1 : scaleAlongPerp
      }) translate(${-midX} ${-midY})`;

      rollStrips.push({
        path: stripPath,
        transform,
        opacity: 1,
        shade: isBack ? 0.7 : shade,
      });
    }
  }

  const rollStickerOpacity =
    animMode === "peel" ? 1 : Math.max(1 - (anim - 0.7) / 0.3, 0);

  const fullClipId = `fullClip-${idPrefix}`;
  const backGradId = `backGrad-${idPrefix}`;
  const creaseShadowBlurWideId = `crBlurW-${idPrefix}`;
  const creaseShadowBlurId = `crBlur-${idPrefix}`;
  const foldShadowId = `fShadow-${idPrefix}`;
  const glowId = `glow-${idPrefix}`;
  const clipId = `clip-${idPrefix}`;

  return (
    <div
      ref={containerRef}
      className={`absolute inset-0 z-0 overflow-visible ${className || ""}
        [--fold-dark:#cbd5e1] dark:[--fold-dark:#0f172a]
        [--fold-mid:#e2e8f0] dark:[--fold-mid:#1e293b]
        [--fold-light:#f1f5f9] dark:[--fold-light:#334155]
        [--fold-white:#ffffff] dark:[--fold-white:#475569]
      `}
    >
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="absolute inset-0 w-full h-full overflow-visible"
        style={{ pointerEvents: "none" }}
      >
        <defs>
          <clipPath id={fullClipId} clipPathUnits="userSpaceOnUse">
            <path d={fullPath} />
          </clipPath>
          <clipPath id={clipId} clipPathUnits="userSpaceOnUse">
            <path d={mainPath} />
          </clipPath>
          <linearGradient
            id={backGradId}
            gradientUnits="userSpaceOnUse"
            x1={(A[0] + B[0]) / 2}
            y1={(A[1] + B[1]) / 2}
            x2={Cprime[0]}
            y2={Cprime[1]}
          >
            <stop offset="0" stopColor="var(--fold-dark)" />
            <stop offset="0.18" stopColor="var(--fold-mid)" />
            <stop offset="0.55" stopColor="var(--fold-light)" />
            <stop offset="1" stopColor="var(--fold-white)" />
          </linearGradient>
          <filter
            id={creaseShadowBlurId}
            x="-50%"
            y="-50%"
            width="200%"
            height="200%"
          >
            <feGaussianBlur stdDeviation="2.8" />
          </filter>
          <filter
            id={creaseShadowBlurWideId}
            x="-80%"
            y="-80%"
            width="260%"
            height="260%"
          >
            <feGaussianBlur stdDeviation="5" />
          </filter>
          <filter
            id={foldShadowId}
            x="-40%"
            y="-40%"
            width="180%"
            height="180%"
          >
            <feDropShadow
              dx="1"
              dy="2"
              stdDeviation="2"
              floodColor="#000"
              floodOpacity="0.25"
            />
          </filter>
          <filter id={glowId} x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="1.2" />
          </filter>
        </defs>

        <g opacity={rollStickerOpacity} clipPath={`url(#${fullClipId})`}>
          {/* Base sticker shape */}
          <g clipPath={`url(#${clipId})`}>
            <path
              d={fullPath}
              className={`transition-colors duration-300 ${
                active
                  ? "fill-blue-50 dark:fill-blue-900 stroke-blue-200 dark:stroke-blue-700"
                  : "fill-white dark:fill-gray-800 stroke-gray-200 dark:stroke-gray-700 group-hover:fill-gray-50 dark:group-hover:fill-gray-700"
              }`}
              strokeWidth="1.5"
            />
          </g>

          {/* Shadows under fold */}
          <path
            d={shadowPath}
            fill="#000"
            opacity={shadow * 0.35}
            filter={`url(#${creaseShadowBlurWideId})`}
          />
          <path
            d={shadowPath}
            fill="#000"
            opacity={shadow * 0.55}
            filter={`url(#${creaseShadowBlurId})`}
          />

          {/* Folded ear itself */}
          <g filter={`url(#${foldShadowId})`}>
            <path
              d={foldPath}
              fill={`url(#${backGradId})`}
              className="stroke-gray-300 dark:stroke-gray-600 transition-colors duration-300"
              strokeWidth="0.6"
            />
          </g>

          <path
            d={foldOuterEdgePath}
            fill="none"
            className="stroke-white dark:stroke-slate-500"
            strokeWidth="1.35"
            opacity="0.9"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <path
            d={crease.path}
            fill="none"
            className="stroke-black dark:stroke-black"
            strokeWidth="1.4"
            opacity="0.18"
            filter={`url(#${creaseShadowBlurId})`}
          />

          <path
            d={highlightPath}
            className="stroke-white dark:stroke-slate-400"
            strokeWidth="2.5"
            opacity="0.9"
            fill="none"
            strokeLinecap="round"
            filter={`url(#${glowId})`}
          />
          <path
            d={highlightPath}
            className="stroke-white dark:stroke-slate-300"
            strokeWidth="1"
            opacity="1"
            fill="none"
            strokeLinecap="round"
          />

          <path
            d={crease.path}
            fill="none"
            className="stroke-gray-400 dark:stroke-gray-600"
            strokeWidth="0.6"
            opacity="0.55"
          />
        </g>

        {/* Roll Strips */}
        {animMode === "roll" &&
          rollStrips.map((strip, i) => (
            <g
              key={i}
              clipPath={`url(#${clipId})`}
              transform={strip.transform}
              opacity={strip.opacity}
            >
              <path
                d={strip.path}
                fill={
                  strip.shade > 0.5
                    ? `rgba(200,200,200,${strip.shade})`
                    : `rgba(255,255,255,${1 - strip.shade})`
                }
              />
              <path
                d={strip.path}
                fill={`rgba(0,0,0,${(1 - strip.shade) * 0.5})`}
              />
            </g>
          ))}
      </svg>

      {/* Foreground Container clipped by both the remaining peeled shape and full constraints */}
      <div
        className="absolute inset-0 z-10 pointer-events-none"
        style={{ clipPath: `url(#${fullClipId})`, opacity: rollStickerOpacity }}
      >
        <div
          className="absolute inset-0 pointer-events-none"
          style={{ clipPath: `url(#${clipId})` }}
        >
          <div className="w-full h-full pointer-events-auto">{children}</div>
        </div>
      </div>
    </div>
  );
}
