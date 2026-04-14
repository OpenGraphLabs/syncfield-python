interface PosePanelProps {
  /** Rolling per-channel buffers from :hook:`useSensorStream`. */
  channels: Record<string, number[]>;
  /** Outer-wrapper sizing. ``"aspect"`` (default) enforces 16:9 — right
   *  for the Record-mode stream cards where the grid row height comes
   *  from the card's intrinsic content. ``"fill"`` takes ``h-full
   *  w-full`` — right for Review where the parent grid cell has an
   *  explicit height budget and the panel must shrink inside it. */
  variant?: "aspect" | "fill";
}

/**
 * Live 3D orientation preview for IMU-class sensor streams.
 *
 * Renders a unit cube plus its body axes rotated by the sensor's
 * current (roll, pitch, yaw) Euler angles. Pure SVG with a hand-rolled
 * 3D projection — no Three.js dependency — matching the rest of the
 * viewer's minimal SVG visual language.
 *
 * The panel is intentionally a pure-props component: its parent
 * (:component:`SensorPanel`) owns the single ``EventSource`` subscription
 * and hands us the latest values via the rolling ``channels`` buffer.
 * Opening a second subscription from inside this component was subtle
 * enough to deadlock the browser under certain proxy/cache conditions,
 * so we keep data flow one-directional and let React's render loop
 * naturally throttle to the display refresh rate.
 */

const W = 228;
const H = 120;
const CUBE_CENTER_X = W / 2;
const CUBE_CENTER_Y = H / 2 - 8;
const CUBE_SCALE = 32;           // pixels per unit half-edge
const AXIS_LENGTH = 1.35;

const AXIS_COLOR = {
  x: "#DC2626", // red
  y: "#059669", // emerald
  z: "#4F46E5", // indigo
} as const;

const CUBE_VERTS: [number, number, number][] = [
  [+0.5, +0.5, +0.5], [+0.5, +0.5, -0.5],
  [+0.5, -0.5, +0.5], [+0.5, -0.5, -0.5],
  [-0.5, +0.5, +0.5], [-0.5, +0.5, -0.5],
  [-0.5, -0.5, +0.5], [-0.5, -0.5, -0.5],
];

const CUBE_EDGES: [number, number][] = [
  [0, 1], [0, 2], [0, 4], [1, 3], [1, 5], [2, 3],
  [2, 6], [3, 7], [4, 5], [4, 6], [5, 7], [6, 7],
];

// Indices of the +X face — filled subtly so the viewer can tell which
// way is "forward" even when the cube is nearly edge-on.
const FRONT_FACE: number[] = [0, 1, 3, 2];

// ---------------------------------------------------------------------
// 3D math — flat row-major Mat3 avoids the
// `noUncheckedIndexedAccess` undefined dance that nested arrays trip.
// ---------------------------------------------------------------------

type Mat3 = readonly [
  number, number, number,
  number, number, number,
  number, number, number,
];
type Vec3 = readonly [number, number, number];

const d2r = (d: number) => (d * Math.PI) / 180;

function mul(a: Mat3, b: Mat3): Mat3 {
  return [
    a[0] * b[0] + a[1] * b[3] + a[2] * b[6],
    a[0] * b[1] + a[1] * b[4] + a[2] * b[7],
    a[0] * b[2] + a[1] * b[5] + a[2] * b[8],
    a[3] * b[0] + a[4] * b[3] + a[5] * b[6],
    a[3] * b[1] + a[4] * b[4] + a[5] * b[7],
    a[3] * b[2] + a[4] * b[5] + a[5] * b[8],
    a[6] * b[0] + a[7] * b[3] + a[8] * b[6],
    a[6] * b[1] + a[7] * b[4] + a[8] * b[7],
    a[6] * b[2] + a[7] * b[5] + a[8] * b[8],
  ];
}

function apply(m: Mat3, v: Vec3): Vec3 {
  return [
    m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
    m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
    m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
  ];
}

function rx(a: number): Mat3 {
  const c = Math.cos(a), s = Math.sin(a);
  return [1, 0, 0,  0, c, -s,  0, s, c];
}
function ry(a: number): Mat3 {
  const c = Math.cos(a), s = Math.sin(a);
  return [c, 0, s,  0, 1, 0,  -s, 0, c];
}
function rz(a: number): Mat3 {
  const c = Math.cos(a), s = Math.sin(a);
  return [c, -s, 0,  s, c, 0,  0, 0, 1];
}

/** ZYX intrinsic Euler (degrees) → 3×3 rotation matrix. */
function eulerMatrix(roll: number, pitch: number, yaw: number): Mat3 {
  return mul(mul(rz(d2r(yaw)), ry(d2r(pitch))), rx(d2r(roll)));
}

// Static camera tilt: mild ¾ view so the all-zero orientation still
// has depth cues. Kept subtle to avoid a "game view" feel.
const VIEW: Mat3 = mul(rx(d2r(18)), ry(d2r(-28)));

function project([x, y, z]: Vec3): [number, number] {
  // Mild foreshortening — closer points render slightly bigger.
  const f = 1 - z * 0.12;
  return [
    CUBE_CENTER_X + x * CUBE_SCALE * f,
    CUBE_CENTER_Y - y * CUBE_SCALE * f,
  ];
}

// ---------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------

export function PosePanel({ channels, variant = "aspect" }: PosePanelProps) {
  const roll = latest(channels, "roll");
  const pitch = latest(channels, "pitch");
  const yaw = latest(channels, "yaw");

  const body = eulerMatrix(roll, pitch, yaw);
  const transform = mul(VIEW, body);

  const projected: [number, number][] = CUBE_VERTS.map(
    (v) => project(apply(transform, v)),
  );

  const axes: { key: "x" | "y" | "z"; color: string; tip: [number, number] }[] = [
    { key: "x", color: AXIS_COLOR.x, tip: project(apply(transform, [AXIS_LENGTH, 0, 0])) },
    { key: "y", color: AXIS_COLOR.y, tip: project(apply(transform, [0, AXIS_LENGTH, 0])) },
    { key: "z", color: AXIS_COLOR.z, tip: project(apply(transform, [0, 0, AXIS_LENGTH])) },
  ];
  const origin = project(apply(transform, [0, 0, 0]));

  const frontPath =
    "M " +
    FRONT_FACE
      .map((i) => {
        const p = projected[i]!;
        return `${p[0]},${p[1]}`;
      })
      .join(" L ") +
    " Z";

  const wrapperClass =
    variant === "fill" ? "h-full w-full px-3 py-2" : "aspect-video px-3 py-2";

  return (
    <div className={wrapperClass}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="h-full w-full"
        preserveAspectRatio="xMidYMid meet"
      >
        {/* Faint ground line for vertical reference. */}
        <line
          x1={8}
          y1={CUBE_CENTER_Y + CUBE_SCALE * 0.9}
          x2={W - 8}
          y2={CUBE_CENTER_Y + CUBE_SCALE * 0.9}
          stroke="currentColor"
          strokeOpacity={0.06}
          strokeDasharray="2 3"
        />

        {/* Front-face fill — subtle cue for orientation. */}
        <path
          d={frontPath}
          fill="currentColor"
          fillOpacity={0.04}
          stroke="none"
        />

        {/* Cube edges. */}
        {CUBE_EDGES.map(([a, b], i) => {
          const pa = projected[a]!;
          const pb = projected[b]!;
          return (
            <line
              key={i}
              x1={pa[0]}
              y1={pa[1]}
              x2={pb[0]}
              y2={pb[1]}
              stroke="currentColor"
              strokeOpacity={0.7}
              strokeWidth={1}
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
          );
        })}

        {/* Body axes emanating from the sensor origin. */}
        {axes.map(({ key, color, tip }) => (
          <g key={key}>
            <line
              x1={origin[0]}
              y1={origin[1]}
              x2={tip[0]}
              y2={tip[1]}
              stroke={color}
              strokeOpacity={0.75}
              strokeWidth={1.2}
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
            <circle
              cx={tip[0]}
              cy={tip[1]}
              r={1.8}
              fill={color}
              fillOpacity={0.9}
            />
          </g>
        ))}

        <text
          x={W / 2}
          y={H - 6}
          textAnchor="middle"
          className="fill-muted font-mono"
          fontSize={7}
        >
          {`roll ${fmtDeg(roll)}  ·  pitch ${fmtDeg(pitch)}  ·  yaw ${fmtDeg(yaw)}`}
        </text>
      </svg>
    </div>
  );
}

function latest(channels: Record<string, number[]>, name: string): number {
  const buf = channels[name];
  if (!buf || buf.length === 0) return 0;
  const v = buf[buf.length - 1];
  return typeof v === "number" && !Number.isNaN(v) ? v : 0;
}

function fmtDeg(v: number): string {
  const sign = v >= 0 ? "+" : "−";
  return `${sign}${Math.abs(v).toFixed(1)}°`;
}
