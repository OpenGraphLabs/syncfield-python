import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import SyncReportPanel from "../SyncReportPanel";
import type { SyncReport } from "../../types";

const REPORT: SyncReport = {
  streams: {
    cam_ego: { offset_seconds: 0.012, confidence: 0.97, quality: "excellent" },
    wrist_imu: { offset_seconds: -0.034, confidence: 0.81, quality: "good" },
  },
};

describe("SyncReportPanel", () => {
  it("renders one card per stream with offset and quality", () => {
    render(<SyncReportPanel report={REPORT} />);
    expect(screen.getByText("cam_ego")).toBeInTheDocument();
    expect(screen.getByText("+0.012s")).toBeInTheDocument();
    expect(screen.getByText("wrist_imu")).toBeInTheDocument();
    expect(screen.getByText("-0.034s")).toBeInTheDocument();
    expect(screen.getByText("excellent")).toBeInTheDocument();
    expect(screen.getByText("good")).toBeInTheDocument();
  });

  it("renders an empty placeholder when no report is provided", () => {
    render(<SyncReportPanel report={null} />);
    expect(screen.getByText(/sync not run/i)).toBeInTheDocument();
  });
});
