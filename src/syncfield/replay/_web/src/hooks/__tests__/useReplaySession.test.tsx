import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useReplaySession } from "../useReplaySession";

const SESSION_FIXTURE = {
  host_id: "test_rig",
  sync_point: {},
  has_frame_map: false,
  streams: [
    { id: "cam_ego", kind: "video", media_url: "/media/cam_ego", data_url: null, frame_count: 60 },
  ],
};

const REPORT_FIXTURE = {
  streams: {
    cam_ego: { offset_seconds: 0.012, confidence: 0.97, quality: "excellent" },
  },
};

describe("useReplaySession", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/session")) {
          return new Response(JSON.stringify(SESSION_FIXTURE), { status: 200 });
        }
        if (url.endsWith("/api/sync-report")) {
          return new Response(JSON.stringify(REPORT_FIXTURE), { status: 200 });
        }
        return new Response("not found", { status: 404 });
      }),
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads the session manifest and sync report", async () => {
    const { result } = renderHook(() => useReplaySession());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.session?.host_id).toBe("test_rig");
    expect(result.current.syncReport?.streams.cam_ego.quality).toBe("excellent");
  });

  it("treats a 404 sync report as null", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/session")) {
          return new Response(JSON.stringify(SESSION_FIXTURE), { status: 200 });
        }
        return new Response("not found", { status: 404 });
      }),
    );

    const { result } = renderHook(() => useReplaySession());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.session?.host_id).toBe("test_rig");
    expect(result.current.syncReport).toBeNull();
  });
});
