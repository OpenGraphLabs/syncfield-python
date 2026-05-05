import { Activity, GripVertical, RotateCcw } from "lucide-react";
import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import {
  DASHBOARD_COLUMNS,
  DASHBOARD_GAP,
  DASHBOARD_ROW_HEIGHT,
  type DashboardLayout,
  type DashboardLayoutItem,
  applyDashboardInteraction,
  buildDefaultStreamDashboardLayout,
  cloneDashboardLayout,
  dashboardLayoutHeight,
  reconcileStreamDashboardLayout,
} from "./stream-dashboard-layout";
import {
  STREAM_PANEL_DASHBOARD_LAYOUT_STORAGE_KEY,
  readStoredDashboardLayout,
  writeStoredDashboardLayout,
} from "./stream-panel-dashboard-storage";

export type DashboardInteractionKind =
  | "move"
  | "resize-x"
  | "resize-y"
  | "resize-xy";

export interface DashboardSnapRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface StreamPanelDashboardItem {
  id: string;
  kind: string;
  title?: ReactNode;
  statusTone?: "success" | "warning" | "error" | "muted";
  accentTone?: "recording";
  badges?: ReactNode;
  headerActions?: ReactNode;
  body: ReactNode;
  footer?: ReactNode;
}

interface StreamPanelDashboardProps {
  items: StreamPanelDashboardItem[];
  title: ReactNode;
  subtitle?: ReactNode;
  className?: string;
  fillHeight?: boolean;
  layoutStorageKey?: string | null;
  layoutFallbackStorageKeys?: string[];
  resetTitle?: string;
}

interface InteractionState {
  kind: DashboardInteractionKind;
  id: string;
  startX: number;
  startY: number;
  currentX: number;
  currentY: number;
  origin: DashboardLayout;
  snap: DashboardSnapRect;
}

interface ReleaseAnimation {
  id: string;
  phase: "from" | "to";
  transformX: number;
  transformY: number;
  width?: number;
  height?: number;
}

export function computeDragSnap({
  origin,
  layout,
  kind,
  deltaColumns,
  deltaRows,
}: {
  origin: DashboardLayoutItem;
  layout: DashboardLayout;
  kind: DashboardInteractionKind;
  deltaColumns: number;
  deltaRows: number;
}): DashboardSnapRect {
  const next = applyDashboardInteraction({
    layout,
    activeId: origin.id,
    ...toLayoutInteraction(kind),
    deltaColumns,
    deltaRows,
  });
  const item = next[origin.id] ?? origin;
  return { x: item.x, y: item.y, w: item.w, h: item.h };
}

export function StreamPanelDashboard({
  items,
  title,
  subtitle,
  className,
  fillHeight = false,
  layoutStorageKey = STREAM_PANEL_DASHBOARD_LAYOUT_STORAGE_KEY,
  layoutFallbackStorageKeys,
  resetTitle = "Reset layout",
}: StreamPanelDashboardProps) {
  const [containerRef, containerWidth] = useElementWidth<HTMLDivElement>();
  const [layout, setLayout] = useState<DashboardLayout>(() =>
    reconcileStreamDashboardLayout(items, readLayout()),
  );
  const [interaction, setInteraction] = useState<InteractionState | null>(null);
  const interactionRef = useRef<InteractionState | null>(null);
  const [releaseAnimation, setReleaseAnimation] = useState<ReleaseAnimation | null>(
    null,
  );
  const releaseTimeoutRef = useRef<number | null>(null);

  const itemSignature = useMemo(
    () => items.map((item) => `${item.id}:${item.kind}`).join("|"),
    [items],
  );
  const itemById = useMemo(
    () => new Map(items.map((item) => [item.id, item])),
    [items],
  );
  const columnWidth = useMemo(() => {
    if (containerWidth <= 0) return 0;
    return (
      (containerWidth - DASHBOARD_GAP * (DASHBOARD_COLUMNS - 1)) /
      DASHBOARD_COLUMNS
    );
  }, [containerWidth]);
  const columnStep = columnWidth + DASHBOARD_GAP;
  const rowStep = DASHBOARD_ROW_HEIGHT + DASHBOARD_GAP;
  const visibleLayoutHeight = Math.max(
    dashboardLayoutHeight(layout),
    interaction ? interaction.snap.y + interaction.snap.h : 0,
  );
  const gridHeight =
    visibleLayoutHeight * DASHBOARD_ROW_HEIGHT +
    Math.max(0, visibleLayoutHeight - 1) * DASHBOARD_GAP;
  const canvasHeight = Math.max(250, gridHeight + 12);

  const clearReleaseAnimation = useCallback(() => {
    if (releaseTimeoutRef.current !== null) {
      window.clearTimeout(releaseTimeoutRef.current);
      releaseTimeoutRef.current = null;
    }
    setReleaseAnimation(null);
  }, []);

  const queueReleaseAnimation = useCallback(
    (animation: Omit<ReleaseAnimation, "phase">) => {
      if (releaseTimeoutRef.current !== null) {
        window.clearTimeout(releaseTimeoutRef.current);
      }
      setReleaseAnimation({ ...animation, phase: "from" });
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          setReleaseAnimation((current) =>
            current?.id === animation.id ? { ...current, phase: "to" } : current,
          );
        });
      });
      releaseTimeoutRef.current = window.setTimeout(() => {
        setReleaseAnimation((current) =>
          current?.id === animation.id ? null : current,
        );
        releaseTimeoutRef.current = null;
      }, 220);
    },
    [],
  );

  useEffect(() => {
    setLayout((current) => {
      const source = Object.keys(current).length > 0 ? current : readLayout();
      return reconcileStreamDashboardLayout(items, source);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemSignature]);

  useEffect(() => {
    if (items.length === 0 || layoutStorageKey === null) return;
    writeStoredDashboardLayout(layout, { storageKey: layoutStorageKey });
  }, [items.length, layout, layoutStorageKey]);

  useEffect(() => {
    interactionRef.current = interaction;
  }, [interaction]);

  useEffect(() => {
    return () => {
      if (releaseTimeoutRef.current !== null) {
        window.clearTimeout(releaseTimeoutRef.current);
      }
    };
  }, []);

  const activeInteractionId = interaction?.id ?? null;
  const activeInteractionKind = interaction?.kind ?? null;

  useEffect(() => {
    if (!activeInteractionId || columnStep <= 0) return;

    const updateInteractionFromPointer = (event: PointerEvent) => {
      const current = interactionRef.current;
      if (!current) return null;
      const originItem = current.origin[current.id];
      if (!originItem) return null;

      const deltaColumns = Math.round((event.clientX - current.startX) / columnStep);
      const deltaRows = Math.round((event.clientY - current.startY) / rowStep);
      const snap = computeDragSnap({
        origin: originItem,
        layout: current.origin,
        kind: current.kind,
        deltaColumns,
        deltaRows,
      });
      const next = {
        ...current,
        currentX: event.clientX,
        currentY: event.clientY,
        snap,
      };
      interactionRef.current = next;
      setInteraction((latest) =>
        latest?.id === current.id && latest.kind === current.kind ? next : latest,
      );
      return next;
    };

    const finishInteraction = (event: PointerEvent, commit: boolean) => {
      const current = updateInteractionFromPointer(event) ?? interactionRef.current;
      if (!current) return;
      const originItem = current.origin[current.id];
      if (!originItem) {
        setInteraction(null);
        return;
      }

      const deltaColumns = Math.round((event.clientX - current.startX) / columnStep);
      const deltaRows = Math.round((event.clientY - current.startY) / rowStep);
      const finalLayout = commit
        ? applyDashboardInteraction({
            layout: current.origin,
            activeId: current.id,
            ...toLayoutInteraction(current.kind),
            deltaColumns,
            deltaRows,
          })
        : current.origin;
      const finalItem = finalLayout[current.id] ?? originItem;
      const liveMetrics = computeLivePanelMetrics({
        interaction: current,
        origin: originItem,
        columnWidth,
      });
      const originLeft = originItem.x * columnStep;
      const originTop = originItem.y * rowStep;
      const finalLeft = finalItem.x * columnStep;
      const finalTop = finalItem.y * rowStep;
      const liveX = current.currentX - current.startX;
      const liveY = current.currentY - current.startY;

      setLayout(finalLayout);
      queueReleaseAnimation({
        id: current.id,
        transformX:
          current.kind === "move" ? liveX - (finalLeft - originLeft) : 0,
        transformY: current.kind === "move" ? liveY - (finalTop - originTop) : 0,
        width: liveMetrics.width,
        height: liveMetrics.height,
      });
      interactionRef.current = null;
      setInteraction(null);
    };

    const handlePointerMove = (event: PointerEvent) => {
      updateInteractionFromPointer(event);
    };
    const handlePointerUp = (event: PointerEvent) => {
      finishInteraction(event, true);
    };
    const handlePointerCancel = (event: PointerEvent) => {
      finishInteraction(event, false);
    };

    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp);
    window.addEventListener("pointercancel", handlePointerCancel);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerCancel);
    };
  }, [
    activeInteractionId,
    activeInteractionKind,
    columnStep,
    columnWidth,
    queueReleaseAnimation,
    rowStep,
  ]);

  if (items.length === 0) return null;

  const resetLayout = () => setLayout(buildDefaultStreamDashboardLayout(items));
  const startInteraction = (
    event: ReactPointerEvent<HTMLElement>,
    id: string,
    kind: InteractionState["kind"],
  ) => {
    if (event.button !== 0 || columnWidth <= 0) return;
    if (kind === "move" && isInteractiveTarget(event.target)) return;
    const origin = cloneDashboardLayout(layout);
    const originItem = origin[id];
    if (!originItem) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    clearReleaseAnimation();
    setInteraction({
      kind,
      id,
      startX: event.clientX,
      startY: event.clientY,
      currentX: event.clientX,
      currentY: event.clientY,
      origin,
      snap: {
        x: originItem.x,
        y: originItem.y,
        w: originItem.w,
        h: originItem.h,
      },
    });
  };

  function readLayout(): DashboardLayout | null {
    if (layoutStorageKey === null) return null;
    return readStoredDashboardLayout({
      storageKey: layoutStorageKey,
      fallbackStorageKeys: layoutFallbackStorageKeys,
    });
  }

  return (
    <section
      data-testid="stream-dashboard"
      className={cn("min-w-0", fillHeight && "flex min-h-0 flex-1 flex-col", className)}
    >
      <div className="mb-3 flex shrink-0 items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-muted" />
            <h2 className="text-sm font-semibold">{title}</h2>
          </div>
          {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
        </div>
        <button
          type="button"
          data-no-drag
          onClick={resetLayout}
          title={resetTitle}
          className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border bg-card text-muted shadow-sm transition-colors hover:bg-foreground/5 hover:text-foreground"
        >
          <RotateCcw className="h-3.5 w-3.5" />
        </button>
      </div>

      <div
        ref={containerRef}
        className={cn(
          "overflow-auto rounded-md bg-background-subtle p-0.5 ring-1 ring-border/70",
          fillHeight ? "min-h-0 flex-1" : "min-h-[250px]",
        )}
        style={fillHeight ? undefined : { height: canvasHeight }}
      >
        <div className="relative min-h-[250px]" style={{ height: canvasHeight }}>
          {columnWidth > 0 && (
            <div
              className="pointer-events-none absolute inset-0 z-0 rounded-md transition-opacity duration-[180ms] ease-out"
              style={{
                opacity: interaction ? 1 : 0,
                backgroundImage:
                  "radial-gradient(circle, hsl(0 0% 13% / 0.18) 1px, transparent 1.2px)",
                backgroundSize: `${columnStep}px ${rowStep}px`,
                backgroundPosition: "0 0",
              }}
            />
          )}
          {interaction && columnWidth > 0 && (
            <div
              className="pointer-events-none absolute z-10 rounded-lg border-[1.5px] border-dashed border-muted bg-foreground/[0.04]"
              style={{
                left: interaction.snap.x * columnStep,
                top: interaction.snap.y * rowStep,
                width:
                  interaction.snap.w * columnWidth +
                  (interaction.snap.w - 1) * DASHBOARD_GAP,
                height:
                  interaction.snap.h * DASHBOARD_ROW_HEIGHT +
                  (interaction.snap.h - 1) * DASHBOARD_GAP,
              }}
            />
          )}
          {Object.values(layout).map((layoutItem) => {
            const item = itemById.get(layoutItem.id);
            if (!item || columnWidth <= 0) return null;
            const width =
              layoutItem.w * columnWidth + (layoutItem.w - 1) * DASHBOARD_GAP;
            const height =
              layoutItem.h * DASHBOARD_ROW_HEIGHT +
              (layoutItem.h - 1) * DASHBOARD_GAP;
            const isActive = interaction?.id === item.id;
            const release =
              releaseAnimation?.id === item.id ? releaseAnimation : null;
            const isRaised = isActive || Boolean(release);
            const isRecording = item.accentTone === "recording";
            const activeMetrics =
              isActive && interaction
                ? computeLivePanelMetrics({
                    interaction,
                    origin: interaction.origin[item.id] ?? layoutItem,
                    columnWidth,
                  })
                : {};
            const panelWidth =
              release?.phase === "from" && release.width !== undefined
                ? release.width
                : activeMetrics.width ?? width;
            const panelHeight =
              release?.phase === "from" && release.height !== undefined
                ? release.height
                : activeMetrics.height ?? height;
            const transform = isActive
              ? interaction?.kind === "move"
                ? `translate3d(${interaction.currentX - interaction.startX}px, ${
                    interaction.currentY - interaction.startY
                  }px, 0)`
                : undefined
              : release
                ? `translate3d(${
                    release.phase === "from" ? release.transformX : 0
                  }px, ${release.phase === "from" ? release.transformY : 0}px, 0)`
                : undefined;

            return (
              <article
                key={item.id}
                data-testid={`stream-panel-${item.id}`}
                className={cn(
                  "group absolute z-[5] flex min-w-0 flex-col overflow-hidden rounded-lg border bg-card text-left shadow-sm transition-[box-shadow,border-color] duration-150",
                  isRaised && "z-20 border-border-strong",
                  isRaised &&
                    !isRecording &&
                    "shadow-[0_12px_28px_rgba(0,0,0,0.18)]",
                  release?.phase === "to" &&
                    "transition-[transform,width,height,box-shadow,border-color] duration-[180ms] ease-out",
                  isRecording &&
                    !isRaised &&
                    "border-recording/60 shadow-[0_0_0_1px_hsl(0_65%_48%/0.35)]",
                  isRecording &&
                    isRaised &&
                    "border-recording/70 shadow-[0_0_0_1px_hsl(0_65%_48%/0.35),0_12px_28px_rgba(0,0,0,0.18)]",
                )}
                style={{
                  left: layoutItem.x * columnStep,
                  top: layoutItem.y * rowStep,
                  width: panelWidth,
                  height: panelHeight,
                  transform,
                }}
              >
                <div
                  className={cn(
                    "group/header flex h-10 shrink-0 touch-none select-none items-center gap-2 px-3",
                    isActive ? "cursor-grabbing" : "cursor-grab",
                  )}
                  title="Drag to move"
                  onPointerDown={(event) => startInteraction(event, item.id, "move")}
                >
                  <GripVertical className="h-3.5 w-3.5 shrink-0 text-muted/60 transition-colors group-hover/header:text-foreground/70" />
                  <span
                    className={cn(
                      "h-2.5 w-2.5 shrink-0 rounded-full",
                      statusToneClass(item.statusTone),
                    )}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-mono text-xs font-semibold">
                      {item.title ?? item.id}
                    </div>
                  </div>
                  <span className="rounded-md bg-foreground/5 px-1.5 py-0.5 font-mono text-[10px] uppercase text-muted">
                    {item.kind}
                  </span>
                  {item.badges}
                  {item.headerActions}
                </div>

                <div className="min-h-0 flex-1 overflow-hidden border-t bg-background">
                  {item.body}
                </div>

                {item.footer && (
                  <div className="flex h-8 shrink-0 items-center gap-2 border-t px-3 text-[11px] text-muted">
                    {item.footer}
                  </div>
                )}

                <ResizeHandles
                  activeKind={isActive ? interaction?.kind : null}
                  onPointerDown={(event, kind) =>
                    startInteraction(event, item.id, kind)
                  }
                />
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function ResizeHandles({
  activeKind,
  onPointerDown,
}: {
  activeKind: DashboardInteractionKind | null | undefined;
  onPointerDown: (
    event: ReactPointerEvent<HTMLButtonElement>,
    kind: DashboardInteractionKind,
  ) => void;
}) {
  const widthActive = activeKind === "resize-x";
  const heightActive = activeKind === "resize-y";
  const cornerActive = activeKind === "resize-xy";

  return (
    <>
      <button
        type="button"
        data-no-drag
        title="Resize width"
        aria-label="Resize width"
        onPointerDown={(event) => onPointerDown(event, "resize-x")}
        className={cn(
          "absolute -right-1 top-2 bottom-2 z-30 flex w-2 cursor-ew-resize touch-none items-center justify-center opacity-0 transition-opacity duration-150 group-hover:opacity-100 focus-visible:opacity-100",
          widthActive && "opacity-100",
        )}
      >
        <span
          className={cn(
            "h-full w-[2px] rounded-full bg-foreground/10 transition-colors",
            widthActive && "bg-foreground/15",
          )}
        />
      </button>
      <button
        type="button"
        data-no-drag
        title="Resize height"
        aria-label="Resize height"
        onPointerDown={(event) => onPointerDown(event, "resize-y")}
        className={cn(
          "absolute -bottom-1 left-2 right-2 z-30 flex h-2 cursor-ns-resize touch-none items-center justify-center opacity-0 transition-opacity duration-150 group-hover:opacity-100 focus-visible:opacity-100",
          heightActive && "opacity-100",
        )}
      >
        <span
          className={cn(
            "h-[2px] w-full rounded-full bg-foreground/10 transition-colors",
            heightActive && "bg-foreground/15",
          )}
        />
      </button>
      <button
        type="button"
        data-no-drag
        title="Resize panel"
        aria-label="Resize panel"
        onPointerDown={(event) => onPointerDown(event, "resize-xy")}
        className={cn(
          "absolute bottom-0 right-0 z-30 h-4 w-4 cursor-nwse-resize touch-none opacity-0 transition-opacity duration-150 group-hover:opacity-100 focus-visible:opacity-100",
          cornerActive && "opacity-100",
        )}
      >
        <span
          className={cn(
            "absolute bottom-1 right-1 h-[2px] w-2 rounded-full bg-foreground/10 transition-colors",
            cornerActive && "bg-foreground/15",
          )}
        />
        <span
          className={cn(
            "absolute bottom-1 right-1 h-2 w-[2px] rounded-full bg-foreground/10 transition-colors",
            cornerActive && "bg-foreground/15",
          )}
        />
      </button>
    </>
  );
}

function toLayoutInteraction(kind: DashboardInteractionKind): {
  kind: "move" | "resize";
  axis?: "x" | "y" | "both";
} {
  if (kind === "move") return { kind: "move" };
  if (kind === "resize-x") return { kind: "resize", axis: "x" };
  if (kind === "resize-y") return { kind: "resize", axis: "y" };
  return { kind: "resize", axis: "both" };
}

function computeLivePanelMetrics({
  interaction,
  origin,
  columnWidth,
}: {
  interaction: InteractionState;
  origin: DashboardLayoutItem;
  columnWidth: number;
}): { width?: number; height?: number } {
  const dx = interaction.currentX - interaction.startX;
  const dy = interaction.currentY - interaction.startY;
  const metrics: { width?: number; height?: number } = {};

  if (interaction.kind === "resize-x" || interaction.kind === "resize-xy") {
    metrics.width = clampNumber(
      panelPixelWidth(origin.w, columnWidth) + dx,
      panelPixelWidth(3, columnWidth),
      panelPixelWidth(DASHBOARD_COLUMNS - origin.x, columnWidth),
    );
  }

  if (interaction.kind === "resize-y" || interaction.kind === "resize-xy") {
    metrics.height = Math.max(
      panelPixelHeight(2),
      panelPixelHeight(origin.h) + dy,
    );
  }

  return metrics;
}

function panelPixelWidth(columns: number, columnWidth: number): number {
  return columns * columnWidth + Math.max(0, columns - 1) * DASHBOARD_GAP;
}

function panelPixelHeight(rows: number): number {
  return rows * DASHBOARD_ROW_HEIGHT + Math.max(0, rows - 1) * DASHBOARD_GAP;
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function statusToneClass(
  tone: StreamPanelDashboardItem["statusTone"] = "muted",
): string {
  if (tone === "success") return "bg-success";
  if (tone === "warning") return "bg-warning";
  if (tone === "error") return "bg-destructive";
  return "bg-muted";
}

function useElementWidth<T extends HTMLElement>() {
  const [width, setWidth] = useState(0);
  const observerRef = useRef<ResizeObserver | null>(null);

  const ref = useCallback((node: T | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (!node) {
      setWidth(0);
      return;
    }
    setWidth(node.clientWidth);
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(node);
    observerRef.current = observer;
  }, []);

  useEffect(() => () => observerRef.current?.disconnect(), []);

  return [ref, width] as const;
}

function isInteractiveTarget(target: EventTarget): boolean {
  const element = target instanceof Element ? target : null;
  return Boolean(
    element?.closest(
      [
        "button",
        "a",
        "input",
        "textarea",
        "select",
        "option",
        "[role='button']",
        "[data-no-drag]",
      ].join(","),
    ),
  );
}
