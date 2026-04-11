import { cn } from "@/lib/utils";

export type ViewMode = "record" | "review";

interface SegmentControlProps {
  mode: ViewMode;
  onChange: (mode: ViewMode) => void;
}

export function SegmentControl({ mode, onChange }: SegmentControlProps) {
  return (
    <div className="flex rounded-lg bg-foreground/5 p-0.5">
      <button
        onClick={() => onChange("record")}
        className={cn(
          "rounded-md px-3 py-1 text-[11px] font-medium transition-all",
          mode === "record"
            ? "bg-card text-foreground shadow-sm"
            : "text-muted hover:text-foreground",
        )}
      >
        Record
      </button>
      <button
        onClick={() => onChange("review")}
        className={cn(
          "rounded-md px-3 py-1 text-[11px] font-medium transition-all",
          mode === "review"
            ? "bg-card text-foreground shadow-sm"
            : "text-muted hover:text-foreground",
        )}
      >
        Review
      </button>
    </div>
  );
}
