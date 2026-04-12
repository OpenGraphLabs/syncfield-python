import { describe, expect, it } from "vitest";
import { syncGrade, type SyncStreamResult } from "../src/lib/review-types";

describe("syncGrade", () => {
  it("returns 'primary' for primary streams", () => {
    const stream: SyncStreamResult = {
      role: "primary",
      host: "h",
      fps: 30,
      original_duration_sec: 60,
      original_frame_count: 1800,
    };
    expect(syncGrade(stream)).toBe("primary");
  });

  it("returns 'excellent' for high confidence", () => {
    const stream: SyncStreamResult = {
      role: "secondary",
      host: "h",
      fps: 30,
      original_duration_sec: 60,
      original_frame_count: 1800,
      confidence: 0.95,
    };
    expect(syncGrade(stream)).toBe("excellent");
  });

  it("returns 'good' for medium confidence", () => {
    const stream: SyncStreamResult = {
      role: "secondary",
      host: "h",
      fps: 30,
      original_duration_sec: 60,
      original_frame_count: 1800,
      confidence: 0.7,
    };
    expect(syncGrade(stream)).toBe("good");
  });

  it("returns 'fair' for low confidence", () => {
    const stream: SyncStreamResult = {
      role: "secondary",
      host: "h",
      fps: 30,
      original_duration_sec: 60,
      original_frame_count: 1800,
      confidence: 0.45,
    };
    expect(syncGrade(stream)).toBe("fair");
  });

  it("returns 'poor' for very low confidence", () => {
    const stream: SyncStreamResult = {
      role: "secondary",
      host: "h",
      fps: 30,
      original_duration_sec: 60,
      original_frame_count: 1800,
      confidence: 0.2,
    };
    expect(syncGrade(stream)).toBe("poor");
  });

  it("returns 'poor' when confidence is missing", () => {
    const stream: SyncStreamResult = {
      role: "secondary",
      host: "h",
      fps: 30,
      original_duration_sec: 60,
      original_frame_count: 1800,
    };
    expect(syncGrade(stream)).toBe("poor");
  });
});
