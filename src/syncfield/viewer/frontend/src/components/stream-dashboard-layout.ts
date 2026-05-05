export const DASHBOARD_COLUMNS = 12;
export const DASHBOARD_ROW_HEIGHT = 86;
export const DASHBOARD_GAP = 10;

export interface DashboardLayoutItem {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export type DashboardLayout = Record<string, DashboardLayoutItem>;

export interface DashboardLayoutSource {
  id: string;
  kind: string;
}

export interface DashboardInteractionInput {
  layout: DashboardLayout;
  activeId: string;
  kind: "move" | "resize";
  axis?: "x" | "y" | "both";
  deltaColumns: number;
  deltaRows: number;
}

export function buildDefaultStreamDashboardLayout(
  streams: DashboardLayoutSource[],
): DashboardLayout {
  const layout: DashboardLayout = {};
  let cursorX = 0;
  let cursorY = 0;
  let rowHeight = 0;

  for (const stream of streams) {
    const size = defaultPanelSize(stream, streams.length);
    if (cursorX + size.w > DASHBOARD_COLUMNS) {
      cursorX = 0;
      cursorY += rowHeight;
      rowHeight = 0;
    }

    layout[stream.id] = {
      id: stream.id,
      x: cursorX,
      y: cursorY,
      w: size.w,
      h: size.h,
    };
    cursorX += size.w;
    rowHeight = Math.max(rowHeight, size.h);
  }

  return layout;
}

export function reconcileStreamDashboardLayout(
  streams: DashboardLayoutSource[],
  current?: DashboardLayout | null,
): DashboardLayout {
  if (!current || Object.keys(current).length === 0) {
    return buildDefaultStreamDashboardLayout(streams);
  }

  const next: DashboardLayout = {};
  for (const stream of streams) {
    const existing = current[stream.id];
    if (existing) {
      next[stream.id] = clampLayoutItem({ ...existing, id: stream.id });
      continue;
    }

    const size = defaultPanelSize(stream, streams.length);
    next[stream.id] = findNextSlot(next, stream.id, size.w, size.h);
  }

  return normalizeDashboardLayout(next);
}

export function cloneDashboardLayout(layout: DashboardLayout): DashboardLayout {
  return Object.fromEntries(
    Object.entries(layout).map(([id, item]) => [id, { ...item }]),
  );
}

export function normalizeDashboardLayout(layout: DashboardLayout): DashboardLayout {
  const next = cloneDashboardLayout(layout);
  for (const [id, item] of Object.entries(next)) {
    next[id] = clampLayoutItem(item);
  }

  for (const item of Object.values(next).sort(compareLayoutItems)) {
    resolveDashboardCollisions(next, item.id);
  }
  return next;
}

export function resolveDashboardCollisions(
  layout: DashboardLayout,
  activeId: string,
): DashboardLayout {
  const active = layout[activeId];
  if (!active) return layout;

  const queue: DashboardLayoutItem[] = [active];
  let guard = 0;
  while (queue.length > 0 && guard < 500) {
    guard += 1;
    const current = queue.shift();
    if (!current) continue;

    for (const other of Object.values(layout).sort(compareLayoutItems)) {
      if (other.id === current.id) continue;
      if (!layoutItemsOverlap(current, other)) continue;
      other.y = current.y + current.h;
      queue.push(other);
    }
  }

  return layout;
}

export function applyDashboardInteraction({
  layout,
  activeId,
  kind,
  axis = "both",
  deltaColumns,
  deltaRows,
}: DashboardInteractionInput): DashboardLayout {
  const originItem = layout[activeId];
  if (!originItem) return cloneDashboardLayout(layout);

  const next = cloneDashboardLayout(layout);
  const item = next[activeId];
  if (!item) return next;

  if (kind === "move") {
    item.x = clamp(originItem.x + deltaColumns, 0, DASHBOARD_COLUMNS - originItem.w);
    item.y = Math.max(0, originItem.y + deltaRows);
  } else {
    if (axis === "x" || axis === "both") {
      item.w = clamp(originItem.w + deltaColumns, 3, DASHBOARD_COLUMNS - originItem.x);
    }
    if (axis === "y" || axis === "both") {
      item.h = Math.max(2, originItem.h + deltaRows);
    }
  }

  return resolveDashboardCollisions(next, activeId);
}

export function layoutItemsOverlap(
  a: Pick<DashboardLayoutItem, "x" | "y" | "w" | "h">,
  b: Pick<DashboardLayoutItem, "x" | "y" | "w" | "h">,
): boolean {
  return (
    a.x < b.x + b.w &&
    a.x + a.w > b.x &&
    a.y < b.y + b.h &&
    a.y + a.h > b.y
  );
}

export function dashboardLayoutHeight(layout: DashboardLayout): number {
  return Math.max(0, ...Object.values(layout).map((item) => item.y + item.h));
}

function findNextSlot(
  layout: DashboardLayout,
  id: string,
  w: number,
  h: number,
): DashboardLayoutItem {
  for (let y = 0; y < 200; y += 1) {
    for (let x = 0; x <= DASHBOARD_COLUMNS - w; x += 1) {
      const candidate = { id, x, y, w, h };
      const collides = Object.values(layout).some((item) =>
        layoutItemsOverlap(candidate, item),
      );
      if (!collides) return candidate;
    }
  }
  return { id, x: 0, y: dashboardLayoutHeight(layout), w, h };
}

function defaultPanelSize(
  stream: DashboardLayoutSource,
  totalStreams: number,
): Pick<DashboardLayoutItem, "w" | "h"> {
  const dense = totalStreams >= 5;
  if (stream.kind === "video") return { w: 4, h: 3 };
  if (stream.kind === "sensor") return { w: dense ? 3 : 4, h: 3 };
  if (stream.kind === "audio") return { w: dense ? 3 : 4, h: 2 };
  return { w: 3, h: 2 };
}

function clampLayoutItem(item: DashboardLayoutItem): DashboardLayoutItem {
  const minW = 3;
  const minH = 2;
  const w = clamp(Math.round(item.w), minW, DASHBOARD_COLUMNS);
  const h = Math.max(minH, Math.round(item.h));
  const x = clamp(Math.round(item.x), 0, DASHBOARD_COLUMNS - w);
  const y = Math.max(0, Math.round(item.y));
  return { ...item, x, y, w, h };
}

function compareLayoutItems(a: DashboardLayoutItem, b: DashboardLayoutItem): number {
  return a.y - b.y || a.x - b.x || a.id.localeCompare(b.id);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
