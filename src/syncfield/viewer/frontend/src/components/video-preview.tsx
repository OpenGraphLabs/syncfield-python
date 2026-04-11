interface VideoPreviewProps {
  streamId: string;
}

/**
 * MJPEG video preview — renders as a plain `<img>` tag pointed at
 * the server's MJPEG endpoint. The browser handles frame decoding
 * natively with zero JavaScript overhead.
 */
export function VideoPreview({ streamId }: VideoPreviewProps) {
  return (
    <div className="relative overflow-hidden bg-black">
      <img
        src={`/stream/video/${streamId}`}
        alt={`${streamId} preview`}
        className="aspect-video w-full object-contain"
        loading="lazy"
      />
    </div>
  );
}
