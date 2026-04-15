/**
 * Quest3PosePanel — 3-D hand skeleton + head rig renderer for the
 * MetaQuestHandStream data.
 *
 * Ported from the opengraph-studio/showcase implementation with
 * viewer-specific trim:
 *  - consumes ``pose`` payload from :hook:`useSensorStream` instead of
 *    a prop-drilled Quest3Frame,
 *  - drops the allocentric X-mirror (showcase flipped to match a third-
 *    person rig; viewer shows the user's own egocentric frame),
 *  - renders into a flex container so the parent sensor panel can size
 *    it alongside the other stream cards.
 */

import type { Quest3Frame } from "@/lib/types";
import { Line, OrbitControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { useMemo } from "react";
import * as THREE from "three";

const JOINTS_PER_HAND = 26;
const HAND_FLOATS = JOINTS_PER_HAND * 3;
const WRIST_INDEX = 1;

// OpenXR 26-joint connection graph (Palm → Wrist → 5 fingers).
const HAND_CONNECTIONS: [number, number][] = [
  [0, 1], [0, 6], [0, 11], [0, 16], [0, 21],
  [1, 2], [2, 3], [3, 4], [4, 5],
  [1, 6], [6, 7], [7, 8], [8, 9], [9, 10],
  [1, 11], [11, 12], [12, 13], [13, 14], [14, 15],
  [1, 16], [16, 17], [17, 18], [18, 19], [19, 20],
  [1, 21], [21, 22], [22, 23], [23, 24], [24, 25],
];

const COLORS = {
  left: "#22d3ee",
  right: "#fb923c",
  head: "#111827",
  bg: "#FAF8F6",
  grid: "#e5e0d8",
  gridStrong: "#d8d0c5",
  floor: "#f5f0e7",
  wall: "#fcfaf6",
  sideWall: "#f2ece1",
  x: "#f87171",
  y: "#34d399",
  z: "#fbbf24",
} as const;

type Vec3 = [number, number, number];

interface ParsedPoseData {
  leftTracked: boolean;
  rightTracked: boolean;
  leftJoints: Vec3[];
  rightJoints: Vec3[];
  head: { position: Vec3; rotation: [number, number, number, number] } | null;
  sceneCenter: Vec3;
}

function parseQuest3Frame(frame: Quest3Frame | null): ParsedPoseData | null {
  if (!frame?.hand_joints || frame.hand_joints.length < HAND_FLOATS * 2) return null;

  const leftJoints = splitJointArray(frame.hand_joints.slice(0, HAND_FLOATS));
  const rightJoints = splitJointArray(frame.hand_joints.slice(HAND_FLOATS, HAND_FLOATS * 2));

  const leftTracked = countValidJoints(leftJoints) > 0;
  const rightTracked = countValidJoints(rightJoints) > 0;

  const head = frame.head_pose && frame.head_pose.length >= 7
    ? {
        position: [
          frame.head_pose[0] ?? 0,
          frame.head_pose[1] ?? 0,
          frame.head_pose[2] ?? 0,
        ] as Vec3,
        rotation: normalizeQuaternion([
          frame.head_pose[3] ?? 0, frame.head_pose[4] ?? 0,
          frame.head_pose[5] ?? 0, frame.head_pose[6] ?? 1,
        ]),
      }
    : null;

  const anchors = [
    head?.position,
    leftJoints[WRIST_INDEX],
    rightJoints[WRIST_INDEX],
  ].filter((v): v is Vec3 => Boolean(v) && !isZeroVector(v!));

  const allPoints = [
    ...leftJoints.filter((j) => !isZeroVector(j) && isFiniteVector(j)),
    ...rightJoints.filter((j) => !isZeroVector(j) && isFiniteVector(j)),
    ...anchors,
  ];
  const avg = averagePoints(anchors.length > 0 ? anchors : [[0, 1.4, 0]]);
  const minY = allPoints.length > 0 ? Math.min(...allPoints.map((p) => p[1])) : 0;
  const sceneCenter: Vec3 = [avg[0], minY, avg[2]];

  return { leftTracked, rightTracked, leftJoints, rightJoints, head, sceneCenter };
}

function splitJointArray(values: number[]): Vec3[] {
  const result: Vec3[] = [];
  for (let i = 0; i < JOINTS_PER_HAND; i++) {
    const o = i * 3;
    result.push([values[o] ?? 0, values[o + 1] ?? 0, values[o + 2] ?? 0]);
  }
  return result;
}

function normalizeQuaternion(v: number[]): [number, number, number, number] {
  const [x = 0, y = 0, z = 0, w = 1] = v;
  const len = Math.hypot(x, y, z, w);
  if (len < 1e-6) return [0, 0, 0, 1];
  return [x / len, y / len, z / len, w / len];
}

function countValidJoints(joints: Vec3[]): number {
  return joints.filter((j) => !isZeroVector(j) && isFiniteVector(j)).length;
}

function averagePoints(points: Vec3[]): Vec3 {
  const s = points.reduce<Vec3>((a, p) => [a[0] + p[0], a[1] + p[1], a[2] + p[2]], [0, 0, 0]);
  return [s[0] / points.length, s[1] / points.length, s[2] / points.length];
}

function vectorSubtract(a: Vec3, b: Vec3): Vec3 {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}

function vectorScale(v: Vec3, s: number): Vec3 {
  return [v[0] * s, v[1] * s, v[2] * s];
}

function isFiniteVector(v: Vec3): boolean {
  return v.every((n) => Number.isFinite(n) && Math.abs(n) < 10);
}

function isZeroVector(v: Vec3): boolean {
  return v.every((n) => Math.abs(n) < 1e-5);
}

// ---------------------------------------------------------------------------
// Three.js sub-components
// ---------------------------------------------------------------------------

function Workspace() {
  const floorLines = useMemo(() => {
    const lines: Vec3[][] = [];
    for (let o = -0.42; o <= 0.4201; o += 0.1) {
      lines.push([[-0.42, 0, o], [0.42, 0, o]]);
      lines.push([[o, 0, -0.42], [o, 0, 0.42]]);
    }
    return lines;
  }, []);

  const backWallLines = useMemo(() => {
    const lines: Vec3[][] = [];
    for (let o = -0.42; o <= 0.4201; o += 0.1) {
      lines.push([[-0.42, o, -0.42], [0.42, o, -0.42]]);
      lines.push([[o, -0.42, -0.42], [o, 0.42, -0.42]]);
    }
    return lines;
  }, []);

  const sideWallLines = useMemo(() => {
    const lines: Vec3[][] = [];
    for (let o = -0.42; o <= 0.4201; o += 0.1) {
      lines.push([[0.42, -0.42, o], [0.42, 0.42, o]]);
      lines.push([[0.42, o, -0.42], [0.42, o, 0.42]]);
    }
    return lines;
  }, []);

  return (
    <group>
      <mesh rotation={[-Math.PI / 2, 0, 0]}>
        <planeGeometry args={[0.84, 0.84]} />
        <meshBasicMaterial color={COLORS.floor} opacity={0.45} transparent />
      </mesh>
      <mesh position={[0, 0, -0.42]}>
        <planeGeometry args={[0.84, 0.84]} />
        <meshBasicMaterial color={COLORS.wall} opacity={0.35} transparent />
      </mesh>
      <mesh position={[0.42, 0, 0]} rotation={[0, -Math.PI / 2, 0]}>
        <planeGeometry args={[0.84, 0.84]} />
        <meshBasicMaterial color={COLORS.sideWall} opacity={0.25} transparent />
      </mesh>

      {floorLines.map((pts, i) => (
        <Line key={`f-${i}`} points={pts} color={i % 2 === 0 ? COLORS.grid : COLORS.gridStrong} lineWidth={0.9} opacity={0.5} transparent />
      ))}
      {backWallLines.map((pts, i) => (
        <Line key={`b-${i}`} points={pts} color={i % 2 === 0 ? COLORS.grid : COLORS.gridStrong} lineWidth={0.9} opacity={0.4} transparent />
      ))}
      {sideWallLines.map((pts, i) => (
        <Line key={`s-${i}`} points={pts} color={COLORS.grid} lineWidth={0.8} opacity={0.25} transparent />
      ))}
    </group>
  );
}

function HandSkeleton({ joints, color, scale }: { joints: Vec3[]; color: string; scale: number }) {
  const validJoints = joints.filter((j) => j.some((v) => Math.abs(v) > 1e-5)).length;
  if (validJoints < 2) return null;

  return (
    <group>
      {HAND_CONNECTIONS.map(([from, to]) => {
        const a = joints[from];
        const b = joints[to];
        if (!a || !b || !isFiniteVector(a) || !isFiniteVector(b) || isZeroVector(a) || isZeroVector(b)) return null;
        return <Line key={`${from}-${to}-${color}`} points={[a, b]} color={color} lineWidth={4} />;
      })}
      {joints.map((joint, i) => {
        if (!isFiniteVector(joint) || isZeroVector(joint)) return null;
        const radius = (i === WRIST_INDEX ? 0.012 : 0.008) * scale;
        return (
          <mesh key={`${color}-${i}`} position={joint}>
            <sphereGeometry args={[radius, 18, 18]} />
            <meshStandardMaterial color={color} emissive={color} emissiveIntensity={0.18} />
          </mesh>
        );
      })}
    </group>
  );
}

function HeadRig({ position, rotation, length, scale }: {
  position: Vec3;
  rotation: [number, number, number, number];
  length: number;
  scale: number;
}) {
  const quaternion = useMemo(
    () => new THREE.Quaternion(rotation[0], rotation[1], rotation[2], rotation[3]).normalize(),
    [rotation]
  );

  const axisLines = useMemo(() => {
    const x = new THREE.Vector3(length, 0, 0).applyQuaternion(quaternion);
    const y = new THREE.Vector3(0, length, 0).applyQuaternion(quaternion);
    const z = new THREE.Vector3(0, 0, length).applyQuaternion(quaternion);
    return {
      x: [position, [position[0] + x.x, position[1] + x.y, position[2] + x.z] as Vec3],
      y: [position, [position[0] + y.x, position[1] + y.y, position[2] + y.z] as Vec3],
      z: [position, [position[0] + z.x, position[1] + z.y, position[2] + z.z] as Vec3],
    };
  }, [length, position, quaternion]);

  return (
    <group>
      <Line points={axisLines.x} color={COLORS.x} lineWidth={3.5} />
      <Line points={axisLines.y} color={COLORS.y} lineWidth={3.5} />
      <Line points={axisLines.z} color={COLORS.z} lineWidth={3.5} />
      <mesh position={position}>
        <sphereGeometry args={[0.015 * scale, 16, 16]} />
        <meshStandardMaterial color={COLORS.head} />
      </mesh>
    </group>
  );
}

function SceneContent({ pose }: { pose: ParsedPoseData }) {
  const rawPoints = [
    ...pose.leftJoints.map((j) => vectorSubtract(j, pose.sceneCenter)),
    ...pose.rightJoints.map((j) => vectorSubtract(j, pose.sceneCenter)),
  ];
  if (pose.head) rawPoints.push(vectorSubtract(pose.head.position, pose.sceneCenter));

  const maxRadius = Math.max(
    0.15,
    ...rawPoints.filter(isFiniteVector).map((p) => Math.hypot(p[0], p[1], p[2]))
  );
  // Scale down aggressively so content stays inside the 0.84-m workspace box.
  const fitScale = Math.min(0.8, Math.max(0.2, 0.18 / maxRadius));
  const headAxisLength = Math.max(0.03, 0.06 * fitScale);
  const markerScale = Math.max(0.6, Math.min(1.2, fitScale * 1.5));

  const transformed = (p: Vec3): Vec3 => vectorScale(vectorSubtract(p, pose.sceneCenter), fitScale);
  const leftJoints = pose.leftJoints.map(transformed);
  const rightJoints = pose.rightJoints.map(transformed);
  const headPos = pose.head ? transformed(pose.head.position) : null;

  return (
    <group rotation={[-0.22, -0.72, 0]}>
      {pose.leftTracked && <HandSkeleton joints={leftJoints} color={COLORS.left} scale={markerScale} />}
      {pose.rightTracked && <HandSkeleton joints={rightJoints} color={COLORS.right} scale={markerScale} />}
      {headPos && pose.head && (
        <HeadRig position={headPos} rotation={pose.head.rotation} length={headAxisLength} scale={markerScale} />
      )}
    </group>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface Quest3PosePanelProps {
  /** Pose payload as delivered by :hook:`useSensorStream`. */
  pose: Record<string, number[]> | null;
}

export function Quest3PosePanel({ pose: poseData }: Quest3PosePanelProps) {
  const parsed = useMemo(() => {
    if (!poseData) return null;
    const frame: Quest3Frame = {
      hand_joints: poseData.hand_joints,
      joint_rotations: poseData.joint_rotations,
      head_pose: poseData.head_pose,
    };
    return parseQuest3Frame(frame);
  }, [poseData]);

  if (!parsed) {
    return (
      <div className="flex aspect-video items-center justify-center text-xs text-muted">
        Waiting for hand / head pose…
      </div>
    );
  }

  return (
    <div className="aspect-video" style={{ background: COLORS.bg }}>
      <Canvas camera={{ position: [0.5, 0.35, 0.5], fov: 50 }}>
        <color attach="background" args={[COLORS.bg]} />
        <ambientLight intensity={1.0} />
        <directionalLight position={[3, 4, 2]} intensity={1.0} color="#fffdf8" />
        <directionalLight position={[-2, 2, -3]} intensity={0.35} color="#c5d4de" />
        <pointLight position={[0.3, 0.3, 0.25]} intensity={0.25} color={COLORS.left} />
        <pointLight position={[-0.3, 0.22, 0.18]} intensity={0.22} color={COLORS.right} />

        <Workspace />
        <SceneContent pose={parsed} />

        <OrbitControls enablePan={false} target={[0, 0.1, 0]} minDistance={0.2} maxDistance={2} />
      </Canvas>
    </div>
  );
}
