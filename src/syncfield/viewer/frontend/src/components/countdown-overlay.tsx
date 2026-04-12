interface CountdownOverlayProps {
  count: number;
}

/**
 * Full-screen countdown overlay — shows 3, 2, 1 before recording starts.
 *
 * Triggered by WebSocket `countdown` events. The pop animation gives
 * clear visual feedback for each tick. Browser audio feedback (C6 tick)
 * is played by the App component, not here.
 */
export function CountdownOverlay({ count }: CountdownOverlayProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/60 backdrop-blur-sm">
      <span
        key={count}
        className="animate-countdown-pop text-[8rem] font-bold leading-none text-white drop-shadow-lg"
      >
        {count}
      </span>
    </div>
  );
}
