interface CollectVideosBarProps {
  onCollect: () => void;
}

/**
 * Top-of-Review bar that walks the user through USB-C video collection
 * for the Insta360 Go3S.
 *
 * WiFi-based aggregation was attempted and abandoned: macOS rejects the
 * 802.11 association with -3925 kCWAssociationDeniedErr unless an app
 * holds Insta360's proprietary BLE wake command (their iOS / Mac SDK).
 * USB Mass Storage is the production-stable path — the SD card just
 * mounts as a disk and we copy files. No WiFi switching, no Location
 * permission, no camera-side standby quirks.
 */
export function CollectVideosBar({ onCollect }: CollectVideosBarProps) {
  return (
    <div className="border-b bg-card">
      <div className="flex items-center gap-4 px-4 py-3">
        <div className="flex flex-col gap-1 min-w-0 flex-1">
          <div className="text-sm font-medium">Collect videos from Go3S</div>
          <div className="text-xs text-muted leading-relaxed">
            Connect the Go3S to this computer with a{" "}
            <strong className="text-foreground">USB-C cable</strong>. On the
            camera screen, choose{" "}
            <code className="rounded bg-foreground/5 px-1 font-mono text-[11px]">
              USB / Mass Storage
            </code>
            . The SD card will mount as a disk in Finder, then click below
            to download all pending recordings into their episode folders.
          </div>
        </div>
        <button
          type="button"
          onClick={onCollect}
          className={
            "shrink-0 rounded-lg bg-primary px-4 py-2 text-sm font-medium " +
            "text-primary-foreground transition-colors hover:bg-primary/90"
          }
        >
          Collect Videos
        </button>
      </div>
    </div>
  );
}
