export interface SensorSample {
  t_ns: number;
  channels: Record<string, number>;
}

/** Parse a JSONL response body into typed sensor samples. */
export function parseSensorJsonl(text: string): SensorSample[] {
  const out: SensorSample[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const obj = JSON.parse(trimmed);
      if (typeof obj.t_ns === "number" && typeof obj.channels === "object") {
        out.push({ t_ns: obj.t_ns, channels: obj.channels });
      }
    } catch {
      // skip malformed lines
    }
  }
  return out;
}
