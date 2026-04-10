import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import VideoArea from "../VideoArea";
import type { ReplayStream } from "../../types";

const STREAMS: ReplayStream[] = [
  { id: "cam_ego", kind: "video", media_url: "/media/cam_ego", data_url: null, frame_count: 60 },
  { id: "wrist_left", kind: "video", media_url: "/media/wrist_left", data_url: null, frame_count: 60 },
];

// jsdom does not implement HTMLMediaElement playback methods.
beforeEach(() => {
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    configurable: true,
    value: vi.fn().mockResolvedValue(undefined),
  });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", {
    configurable: true,
    value: vi.fn(),
  });
});

describe("VideoArea", () => {
  it("renders one <video> per video stream", () => {
    render(
      <VideoArea
        streams={STREAMS}
        mode="after"
        offsetFor={() => 0}
        masterTime={0}
        isPlaying={false}
        seekVersion={0}
      />,
    );
    const videos = screen.getAllByTestId("replay-video");
    expect(videos).toHaveLength(2);
    expect(videos[0]).toHaveAttribute("src", "/media/cam_ego");
  });
});
