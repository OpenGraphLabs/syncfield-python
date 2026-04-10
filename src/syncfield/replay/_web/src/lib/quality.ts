import type { SyncQuality } from "../types";

export function qualityColor(quality: SyncQuality | string): string {
  switch (quality) {
    case "excellent":
      return "bg-green-100 text-green-700";
    case "good":
      return "bg-blue-100 text-blue-700";
    case "fair":
      return "bg-amber-100 text-amber-700";
    case "poor":
      return "bg-red-100 text-red-700";
    default:
      return "bg-zinc-100 text-zinc-600";
  }
}
