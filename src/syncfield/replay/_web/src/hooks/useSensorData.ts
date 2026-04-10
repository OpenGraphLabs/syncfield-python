import { useEffect, useState } from "react";
import { parseSensorJsonl, type SensorSample } from "../lib/sensorParser";
import type { ReplayStream } from "../types";

export interface SensorStreamData {
  id: string;
  samples: SensorSample[];
  channelNames: string[];
}

export function useSensorData(streams: ReplayStream[]): SensorStreamData[] {
  const [data, setData] = useState<SensorStreamData[]>([]);

  useEffect(() => {
    let cancelled = false;
    const sensorStreams = streams.filter(
      (s) => s.kind === "sensor" && s.data_url,
    );
    Promise.all(
      sensorStreams.map(async (s) => {
        const r = await fetch(s.data_url!);
        if (!r.ok) return null;
        const samples = parseSensorJsonl(await r.text());
        const channelNames =
          samples.length > 0 ? Object.keys(samples[0].channels) : [];
        return { id: s.id, samples, channelNames };
      }),
    ).then((results) => {
      if (cancelled) return;
      setData(results.filter((r): r is SensorStreamData => r !== null));
    });
    return () => {
      cancelled = true;
    };
  }, [streams]);

  return data;
}
