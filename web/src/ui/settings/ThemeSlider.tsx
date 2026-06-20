import { Moon, Sun } from "lucide-react";
import { useEffect, useRef } from "react";
import { interpolateWorkingScale } from "../theme/palettes";

/**
 * ============================================================================
 *  ПОЛЗУНОК ТЕМЫ — «ЖИДКОЕ СТЕКЛО» (WebGL)
 * ============================================================================
 *
 * Премиум-ползунок с оптической линзой:
 *   • В покое — изящный мини-овал на тонком треке.
 *   • При зажатии — раскрывается в горизонтальную капсулу-линзу с увеличением,
 *     хроматической аберрацией, бликами Френеля и specular.
 *   • Иконка-индикатор плавно морфирует между луной 🌙 и солнцем ☀️.
 *
 * **Визуальный трек — это сама шкала рабочих тем.** Цвет в каждой точке X
 * берётся из 1D-текстуры, в которую один раз запечён реальный результат
 * `interpolateWorkingScale(t)`. Трек проходит только по валидным рабочим темам
 * (Black -> Slate -> Sage -> Mocha -> Paper -> White) без хроматического
 * мусора. Левый конец — чистый Pro Black, правый — чистый Pure White.
 *
 * Управление темой:
 *   • `level` (0..1) — положение ползунка.
 *   • `auto` — режим следования за системной темой браузера.
 * ============================================================================
 */

interface ThemeSliderProps {
  level: number; // 0 = PURE_DARK, 1 = PURE_LIGHT
  auto: boolean;
  onLevelChange: (level: number) => void;
  onAutoChange: (auto: boolean) => void;
}

/* ----------------------------------------------------------- шейдер */

const FRAGMENT_SHADER = `
precision highp float;

uniform vec2       u_resolution;
uniform float      u_value;        // 0..1 — положение ползунка
uniform float      u_velocity;     // инерция для «squeeze»-деформации
uniform float      u_dragFactor;   // 0 = покой (мини-овал), 1 = раскрытая линза
uniform sampler2D  u_trackTex;     // 1D-текстура кривой перехода темы

float sdRoundRect(vec2 p, vec2 b, float r) {
  vec2 d = abs(p) - b + vec2(r);
  return min(max(d.x, d.y), 0.0) + length(max(d, 0.0)) - r;
}

float sdMoon(vec2 p, float r, vec2 offset) {
  float d1 = length(p) - r;
  float d2 = length(p - offset) - r * 0.95;
  return max(d1, -d2);
}

// Цвет кривой перехода темы в точке с нормированной координатой t (0..1).
// t зажимается в [0,1] — это «продлевает» трек за его физические границы:
// лупа у края через хроматическую аберрацию видит ближайший конец кривой
// (чистый тёмный или чистый светлый), а не чёрный фон.
vec3 trackColorAtT(float t) {
  return texture2D(u_trackTex, vec2(clamp(t, 0.0, 1.0), 0.5)).rgb;
}

void main() {
  vec2 st = gl_FragCoord.xy;

  float padding = 28.0;
  float trackHeight = 6.0;
  vec2 trackCenter = u_resolution * 0.5;
  float trackWidth = u_resolution.x - padding * 2.0;

  float thumbX = padding + u_value * trackWidth;
  vec2 thumbCenter = vec2(thumbX, trackCenter.y);

  // морфинг размеров
  vec2 idleSize    = vec2(11.0, 5.5);
  vec2 activeSize  = vec2(24.0, 12.0);
  vec2 capsuleSize = mix(idleSize, activeSize, u_dragFactor);

  // координаты относительно thumb с учётом инерции
  vec2 dropPos = st - thumbCenter;
  dropPos.x /= (1.0 + abs(u_velocity) * 0.035);
  dropPos.y *= (1.0 + abs(u_velocity) * 0.015);

  float dDrop = sdRoundRect(dropPos, capsuleSize, capsuleSize.y);

  vec2 trackP = st - trackCenter;
  float dTrack = sdRoundRect(trackP, vec2(trackWidth * 0.5, trackHeight * 0.5), trackHeight * 0.5);

  // --- аккумулируем (color, alpha) ---
  vec3 color = vec3(0.0);
  float alpha = 0.0;

  // 1. базовый трек + мягкое свечение вдоль него
  if (dTrack < 0.0) {
    color = trackColorAtT((st.x - 28.0) / max(trackWidth, 1.0));
    alpha = 1.0;
  } else if (dTrack < 8.0) {
    float g = 1.0 - dTrack / 8.0;
    color = trackColorAtT(u_value) * g * 0.35;
    alpha = g * 0.5;
  }

  // 2. оптическая линза (только при drag)
  if (dDrop < 0.0) {
    float spineW = max(0.0, capsuleSize.x - capsuleSize.y);
    vec2 spineP = vec2(clamp(dropPos.x, -spineW, spineW), 0.0);
    vec2 diffSpine = dropPos - spineP;
    float rSpine = length(diffSpine);
    float z = sqrt(max(0.0, capsuleSize.y * capsuleSize.y - rSpine * rSpine));
    vec3 normal = normalize(vec3(diffSpine.x, diffSpine.y, z));

    // увеличение + хроматическая аберрация
    float zoom = 1.0 + u_dragFactor * 0.35;
    float lensT = u_value + (dropPos.x / zoom) / max(trackWidth, 1.0);
    float refrStrength = mix(1.2, 11.0, u_dragFactor);
    vec3 colR = trackColorAtT(lensT - normal.x * refrStrength * 1.2 / max(trackWidth, 1.0));
    vec3 colG = trackColorAtT(lensT - normal.x * refrStrength * 1.0 / max(trackWidth, 1.0));
    vec3 colB = trackColorAtT(lensT - normal.x * refrStrength * 0.8 / max(trackWidth, 1.0));
    vec3 lensColor = vec3(colR.r, colG.g, colB.b);
    lensColor *= (1.0 + u_dragFactor * 0.15);

    // Френель-блики
    float fresnel = pow(1.0 - normal.z, 2.5);
    lensColor = mix(lensColor, vec3(0.95, 0.98, 1.0), fresnel * (0.3 + u_dragFactor * 0.3));

    vec3 lightDir = normalize(vec3(-1.0, 1.2, 1.5));
    float spec = pow(max(0.0, dot(normal, lightDir)), 32.0);
    lensColor += vec3(1.0) * spec * (0.3 + u_dragFactor * 0.5);

    // --- иконка: луна -> солнце ---
    vec3 iconColor = mix(vec3(0.97, 0.98, 1.0), vec3(0.12, 0.13, 0.15), u_value);
    float iconAlpha = pow(u_dragFactor, 2.0) * 0.95;

    float sunCore = length(dropPos) - capsuleSize.y * 0.32;
    float angle = atan(dropPos.y, dropPos.x);
    float rays = abs(sin(angle * 4.0));
    float rayRing = abs(length(dropPos) - capsuleSize.y * 0.62);
    float sun = min(sunCore, max(rayRing, rays - 0.55));
    float moon = sdMoon(dropPos, capsuleSize.y * 0.55, vec2(capsuleSize.y * 0.35, -capsuleSize.y * 0.3));

    float moonW = clamp(1.0 - u_value * 2.0, 0.0, 1.0);
    float sunW  = clamp(u_value * 2.0 - 1.0, 0.0, 1.0);
    if (moon < 0.0) lensColor = mix(lensColor, iconColor, iconAlpha * moonW);
    if (sun  < 0.0) lensColor = mix(lensColor, iconColor, iconAlpha * sunW);

    // линза перекрывает всё
    color = lensColor;
    alpha = 1.0;
  }

  gl_FragColor = vec4(color, alpha);
}
`;

const VERT_SHADER = `
attribute vec2 a_position;
void main() { gl_Position = vec4(a_position, 0.0, 1.0); }
`;

/* --------------------------------------------- запекание трека-текстуры */

/** Размер 1D-текстуры: достаточно, чтобы передать плавную кривую перехода. */
const TRACK_TEX_W = 256;

function parseHex(hex: string): [number, number, number] {
  const h = hex.replace("#", "").trim();
  const full =
    h.length === 3
      ? h
          .split("")
          .map((c) => c + c)
          .join("")
      : h;
  const n = parseInt(full, 16);
  return [
    ((n >> 16) & 255) / 255,
    ((n >> 8) & 255) / 255,
    (n & 255) / 255,
  ];
}

/**
 * Запекает шкалу рабочих тем WORKING_THEMES в 1D-текстуру.
 * Каждый пиксель по X — это реальный base_background, вычисленный через
 * `interpolateWorkingScale(t)` (чистая HSL-интерполяция по чекпоинтам).
 */
function buildTrackTextureData(): Uint8Array {
  const data = new Uint8Array(TRACK_TEX_W * 1 * 4);
  for (let i = 0; i < TRACK_TEX_W; i++) {
    const t = i / (TRACK_TEX_W - 1);
    const pal = interpolateWorkingScale(t);
    const [r, g, b] = parseHex(pal.base_background);
    data[i * 4 + 0] = Math.round(r * 255);
    data[i * 4 + 1] = Math.round(g * 255);
    data[i * 4 + 2] = Math.round(b * 255);
    data[i * 4 + 3] = 255;
  }
  return data;
}

/* ----------------------------------------------------------- класс */

class LiquidGlassSlider {
  private gl: WebGLRenderingContext | null = null;
  private program: WebGLProgram | null = null;
  private posBuf: WebGLBuffer | null = null;
  private trackTex: WebGLTexture | null = null;
  private uRes: WebGLUniformLocation | null = null;
  private uVal: WebGLUniformLocation | null = null;
  private uVel: WebGLUniformLocation | null = null;
  private uDragFactor: WebGLUniformLocation | null = null;
  private uTrackTex: WebGLUniformLocation | null = null;

  /** Отображаемое значение (сглаженное). */
  val = 0;
  /** Целевое значение (ставится снаружи или при драге). */
  targetVal = 0;

  private velocity = 0;
  private targetVelocity = 0;
  private dragFactor = 0;
  private targetDragFactor = 0;
  private lastX = 0;
  private dragging = false;

  private readonly canvas: HTMLCanvasElement;

  constructor(canvas: HTMLCanvasElement, initial: number) {
    this.canvas = canvas;
    this.val = initial;
    this.targetVal = initial;
    this.initWebGL();
    this.setupEvents();
    this.resize();
  }

  private initWebGL() {
    const gl = this.canvas.getContext("webgl", {
      antialias: true,
      alpha: true,
      premultipliedAlpha: true,
    });
    this.gl = gl;
    if (!gl) return;

    gl.clearColor(0, 0, 0, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);

    const compile = (type: number, src: string) => {
      const sh = gl.createShader(type);
      if (!sh) return null;
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
        console.error("Shader compile:", gl.getShaderInfoLog(sh));
      }
      return sh;
    };

    const vs = compile(gl.VERTEX_SHADER, VERT_SHADER);
    const fs = compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER);
    if (!vs || !fs) return;

    const prog = gl.createProgram();
    if (!prog) return;
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.linkProgram(prog);
    this.program = prog;

    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]),
      gl.STATIC_DRAW,
    );
    this.posBuf = buf;

    // 1D-текстура кривой перехода темы
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(
      gl.TEXTURE_2D,
      0,
      gl.RGBA,
      TRACK_TEX_W,
      1,
      0,
      gl.RGBA,
      gl.UNSIGNED_BYTE,
      buildTrackTextureData(),
    );
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    this.trackTex = tex;

    this.uRes = gl.getUniformLocation(prog, "u_resolution");
    this.uVal = gl.getUniformLocation(prog, "u_value");
    this.uVel = gl.getUniformLocation(prog, "u_velocity");
    this.uDragFactor = gl.getUniformLocation(prog, "u_dragFactor");
    this.uTrackTex = gl.getUniformLocation(prog, "u_trackTex");
  }

  resize() {
    if (!this.gl) return;
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    this.gl.viewport(0, 0, this.canvas.width, this.canvas.height);
  }

  setTarget(v: number) {
    this.targetVal = Math.max(0, Math.min(1, v));
  }

  private updateFromEvent(clientX: number, onChange: (v: number) => void) {
    const rect = this.canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const padding = 28;
    const trackWidth = rect.width - padding * 2;
    const raw = Math.max(
      0,
      Math.min(1, (x - padding) / Math.max(trackWidth, 1)),
    );
    this.targetVelocity = (x - this.lastX) * 2.0;
    this.lastX = x;
    this.targetVal = raw;
    this.val = raw;
    onChange(raw);
  }

  private setupEvents() {
    const onDown = (e: PointerEvent) => {
      this.dragging = true;
      this.targetDragFactor = 1.0;
      this.canvas.setPointerCapture(e.pointerId);
      const rect = this.canvas.getBoundingClientRect();
      this.lastX = e.clientX - rect.left;
      this.updateFromEvent(e.clientX, this.onChangeRef);
    };
    const onMove = (e: PointerEvent) => {
      if (!this.dragging) return;
      this.updateFromEvent(e.clientX, this.onChangeRef);
    };
    const onUp = (e: PointerEvent) => {
      if (!this.dragging) return;
      this.dragging = false;
      this.targetDragFactor = 0.0;
      this.targetVelocity = 0;
      try {
        this.canvas.releasePointerCapture(e.pointerId);
      } catch {
        /* noop */
      }
    };
    this.canvas.addEventListener("pointerdown", onDown);
    this.canvas.addEventListener("pointermove", onMove);
    this.canvas.addEventListener("pointerup", onUp);
    this.canvas.addEventListener("pointercancel", onUp);
    this._cleanup = () => {
      this.canvas.removeEventListener("pointerdown", onDown);
      this.canvas.removeEventListener("pointermove", onMove);
      this.canvas.removeEventListener("pointerup", onUp);
      this.canvas.removeEventListener("pointercancel", onUp);
    };
  }

  /** Колбэк изменения — переопределяется снаружи. */
  onChangeRef: (_v: number) => void = () => {};

  private _cleanup: () => void = () => {};

  destroy() {
    this._cleanup();
  }

  render() {
    this.val += (this.targetVal - this.val) * 0.28;
    this.velocity =
      this.velocity * 0.82 + (this.targetVelocity - this.velocity) * 0.18;
    this.targetVelocity *= 0.85;
    this.dragFactor =
      this.dragFactor * 0.78 + (this.targetDragFactor - this.dragFactor) * 0.22;

    const gl = this.gl;
    if (!gl || !this.program) return;
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.program);

    gl.bindBuffer(gl.ARRAY_BUFFER, this.posBuf);
    const aPos = gl.getAttribLocation(this.program, "a_position");
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    // биндим текстуру кривой темы на unit 0
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.trackTex);
    gl.uniform1i(this.uTrackTex, 0);

    gl.uniform2f(this.uRes, this.canvas.width, this.canvas.height);
    gl.uniform1f(this.uVal, this.val);
    gl.uniform1f(this.uVel, this.velocity);
    gl.uniform1f(this.uDragFactor, this.dragFactor);

    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
}

/* ----------------------------------------------------------- компонент */

export function ThemeSlider({
  level,
  auto,
  onLevelChange,
  onAutoChange,
}: ThemeSliderProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const sliderRef = useRef<LiquidGlassSlider | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const slider = new LiquidGlassSlider(canvas, level);
    sliderRef.current = slider;

    const loop = () => {
      slider.render();
      rafId = requestAnimationFrame(loop);
    };
    let rafId = requestAnimationFrame(loop);

    const onResize = () => slider.resize();
    window.addEventListener("resize", onResize);

    return () => {
      cancelAnimationFrame(rafId);
      window.removeEventListener("resize", onResize);
      slider.destroy();
      sliderRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    sliderRef.current?.setTarget(level);
  }, [level]);

  useEffect(() => {
    const slider = sliderRef.current;
    if (!slider) return;
    slider.onChangeRef = (v) => {
      if (auto) onAutoChange(false);
      onLevelChange(v);
    };
  }, [auto, onLevelChange, onAutoChange]);

  return (
    <div
      className={`theme-slider ${auto ? "theme-slider--auto" : ""}`}
      role="group"
      aria-label="Переключатель темы"
      title={
        auto
          ? "Следует за системой — нажмите, чтобы выбрать вручную"
          : undefined
      }
    >
      <Moon
        className="theme-slider__icon theme-slider__icon--moon"
        onClick={() => onLevelChange(0)}
        aria-label="Тёмная тема"
      />
      <canvas
        ref={canvasRef}
        className="theme-slider__canvas"
        style={{ opacity: auto ? 0.55 : 1 }}
      />
      <Sun
        className="theme-slider__icon theme-slider__icon--sun"
        onClick={() => onLevelChange(1)}
        aria-label="Светлая тема"
      />
    </div>
  );
}