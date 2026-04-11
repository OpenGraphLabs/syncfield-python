import { cn } from "@/lib/utils";

export type ViewMode = "record" | "review";

interface NavLinksProps {
  mode: ViewMode;
  onChange: (mode: ViewMode) => void;
}

export function NavLinks({ mode, onChange }: NavLinksProps) {
  return (
    <nav className="flex items-center gap-1">
      <NavLink
        active={mode === "record"}
        onClick={() => onChange("record")}
      >
        Record
      </NavLink>
      <NavLink
        active={mode === "review"}
        onClick={() => onChange("review")}
      >
        Review
      </NavLink>
    </nav>
  );
}

function NavLink({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "rounded-lg px-3 py-1.5 text-sm font-medium transition-colors",
        active
          ? "bg-foreground/8 text-foreground"
          : "text-muted hover:text-foreground hover:bg-foreground/4",
      )}
    >
      {children}
    </button>
  );
}

/** @deprecated Use NavLinks instead */
export function SegmentControl({ mode, onChange }: NavLinksProps) {
  return <NavLinks mode={mode} onChange={onChange} />;
}
