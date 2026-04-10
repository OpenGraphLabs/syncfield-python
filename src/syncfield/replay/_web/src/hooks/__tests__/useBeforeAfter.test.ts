import { describe, expect, it } from "vitest";
import { computeStreamTime } from "../useBeforeAfter";

describe("computeStreamTime", () => {
  it("returns the master time unchanged in 'before' mode", () => {
    expect(computeStreamTime(5.0, 0.3, "before")).toBe(5.0);
  });

  it("subtracts the offset in 'after' mode", () => {
    expect(computeStreamTime(5.0, 0.3, "after")).toBeCloseTo(4.7, 5);
  });

  it("clamps negative results to 0", () => {
    expect(computeStreamTime(0.1, 0.5, "after")).toBe(0);
  });

  it("treats a missing offset as 0", () => {
    expect(computeStreamTime(5.0, undefined, "after")).toBe(5.0);
  });
});
