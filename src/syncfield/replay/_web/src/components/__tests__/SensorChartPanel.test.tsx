import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import SensorChartPanel from "../SensorChartPanel";
import type { SensorStreamData } from "../../hooks/useSensorData";

const SENSORS: SensorStreamData[] = [
  {
    id: "wrist_imu",
    channelNames: ["ax", "ay"],
    samples: [
      { t_ns: 0, channels: { ax: 0.1, ay: -0.2 } },
      { t_ns: 1_000_000, channels: { ax: 0.2, ay: -0.3 } },
      { t_ns: 2_000_000, channels: { ax: 0.4, ay: -0.1 } },
    ],
  },
];

describe("SensorChartPanel", () => {
  it("renders one chart group per sensor stream", () => {
    render(
      <SensorChartPanel sensors={SENSORS} masterTime={0} duration={1} />,
    );
    expect(screen.getByText("wrist_imu")).toBeInTheDocument();
    // One <svg> per channel
    const svgs = document.querySelectorAll("svg.sensor-chart");
    expect(svgs.length).toBe(2);
  });

  it("renders empty state when no sensors", () => {
    render(<SensorChartPanel sensors={[]} masterTime={0} duration={1} />);
    expect(screen.getByText(/no sensor streams/i)).toBeInTheDocument();
  });
});
