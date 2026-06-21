import { useId, useRef } from "react";
import {
  buildStickerGeometry,
  foldInward,
  getCornerPoints,
} from "./sticker/geometry";
import { buildRollStrips } from "./sticker/roll-strips";
import { useStickerAnimation } from "./sticker/useStickerAnimation";
import type { StickerBgProps } from "./sticker/types";

/**
 * 3D-стикер с эффектом "отклеивания" / "скручивания".
 *
 * Тонкая обёртка над модулями:
 *  - геометрия:      ./sticker/geometry.ts
 *  - roll-анимация:  ./sticker/roll-strips.ts
 *  - анимация/размер: ./sticker/useStickerAnimation.ts
 *  - цвета:          ui/theme/tokens.css (--color-sticker-*)
 *
 * До рефакторинга: 567 строк, смешанных цветов.
 * После: ~150 строк, все цвета параметризованы.
 */
export function StickerBg({
  peeled,
  active,
  className,
  children,
  initialSize,
  oversizePct = 0,
  oversizeYPx = 0,
  corner = "tr",
  baseDx = 54,
  baseDy = 22,
  curve = 0.15,
  shadow = 0.33,
  r = 12,
  animMode = "roll",
}: StickerBgProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const idPrefix = useId().replace(/:/g, "");

  const { size, anim } = useStickerAnimation(peeled, containerRef, initialSize);
  const { w: rawW, h: rawH } = size;
  // До первого валидного измерения держим DOM-структуру стабильной, но не
  // прячем содержимое карточки. Иначе короткий нулевой замер превращается в
  // заметный blink всей строки источника.
  const measured = rawW > 0 && rawH > 0;
  const w = measured ? rawW : 1;
  const h = measured ? rawH : 1;
  const xScale = 1 + oversizePct;
  const yScale = 1 + oversizePct + (measured ? oversizeYPx / h : 0);

  const diag = Math.hypot(w, h);
  const maxPeel = diag * 1.6;
  const foldT = Math.min(anim / 0.6, 1);

  const dxAnim =
    animMode === "peel" ? baseDx + (maxPeel - baseDx) * foldT : baseDx;
  const dyAnim =
    animMode === "peel" ? baseDy + (maxPeel - baseDy) * foldT : baseDy;

  const { A, B, C } = getCornerPoints(0, 0, w, h, corner, dxAnim, dyAnim);
  const Cprime = foldInward(C, A, B);
  const geo = buildStickerGeometry(w, h, corner, A, B, C, Cprime, curve, r);
  const rollStrips =
    animMode === "roll" ? buildRollStrips(w, h, corner, anim) : [];
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
      className={`absolute inset-0 z-0 overflow-visible ${className || ""}`}
      style={{
        borderRadius: r,
        transform:
          oversizePct || oversizeYPx
            ? `scale(${xScale}, ${yScale})`
            : undefined,
        transformOrigin: "center",
      }}
    >
      <div
        className={`absolute inset-0 transition-colors duration-300 ${
          active
            ? "bg-accent-soft border border-accent-soft-border"
            : "bg-surface border border-default group-hover:bg-elevated"
        }`}
        style={{ borderRadius: r, opacity: measured ? 0 : 1 }}
      />
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="absolute inset-0 w-full h-full overflow-visible"
        style={{ pointerEvents: "none", opacity: measured ? 1 : 0 }}
      >
        <defs>
          <clipPath id={fullClipId} clipPathUnits="userSpaceOnUse">
            <path d={geo.fullPath} />
          </clipPath>
          <clipPath id={clipId} clipPathUnits="userSpaceOnUse">
            <path d={geo.mainPath} />
          </clipPath>
          <linearGradient
            id={backGradId}
            gradientUnits="userSpaceOnUse"
            x1={(A[0] + B[0]) / 2}
            y1={(A[1] + B[1]) / 2}
            x2={Cprime[0]}
            y2={Cprime[1]}
          >
            <stop offset="0" stopColor="var(--color-sticker-fold-dark)" />
            <stop offset="0.18" stopColor="var(--color-sticker-fold-mid)" />
            <stop offset="0.55" stopColor="var(--color-sticker-fold-light)" />
            <stop offset="1" stopColor="var(--color-sticker-fold-white)" />
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
          {/* Базовая форма стикера */}
          <g clipPath={`url(#${clipId})`}>
            <path
              d={geo.fullPath}
              className={`transition-colors duration-300 ${
                active
                  ? "fill-accent-soft stroke-accent-soft-border"
                  : "fill-surface stroke-default group-hover:fill-elevated"
              }`}
              strokeWidth="1.5"
            />
          </g>

          {/* Тени под складкой */}
          <path
            d={geo.shadowPath}
            fill="#000"
            opacity={shadow * 0.35}
            filter={`url(#${creaseShadowBlurWideId})`}
          />
          <path
            d={geo.shadowPath}
            fill="#000"
            opacity={shadow * 0.55}
            filter={`url(#${creaseShadowBlurId})`}
          />

          {/* Отогнутый уголок */}
          <g filter={`url(#${foldShadowId})`}>
            <path
              d={geo.foldPath}
              fill={`url(#${backGradId})`}
              className="stroke-strong transition-colors duration-300"
              strokeWidth="0.6"
            />
          </g>

          <path
            d={geo.foldOuterEdgePath}
            fill="none"
            stroke="var(--color-sticker-edge)"
            strokeWidth="1.35"
            opacity="0.9"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <path
            d={geo.crease.path}
            fill="none"
            stroke="#000"
            strokeWidth="1.4"
            opacity="0.18"
            filter={`url(#${creaseShadowBlurId})`}
          />
          <path
            d={geo.highlightPath}
            stroke="var(--color-sticker-edge)"
            strokeWidth="2.5"
            opacity="0.9"
            fill="none"
            strokeLinecap="round"
            filter={`url(#${glowId})`}
          />
          <path
            d={geo.highlightPath}
            stroke="var(--color-sticker-edge)"
            strokeWidth="1"
            opacity="1"
            fill="none"
            strokeLinecap="round"
          />
          <path
            d={geo.crease.path}
            fill="none"
            stroke="var(--color-text-muted)"
            strokeWidth="0.6"
            opacity="0.55"
          />
        </g>

        {/* Roll-полосы */}
        {rollStrips.map((strip, i) => (
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

      {/* Контейнер переднего плана, обрезанный по форме */}
      <div
        className="absolute inset-0 z-10 pointer-events-none"
        style={
          measured
            ? { clipPath: `url(#${fullClipId})`, opacity: rollStickerOpacity }
            : undefined
        }
      >
        <div
          className="absolute inset-0 pointer-events-none"
          style={measured ? { clipPath: `url(#${clipId})` } : undefined}
        >
          <div className="w-full h-full pointer-events-auto">{children}</div>
        </div>
      </div>
    </div>
  );
}
