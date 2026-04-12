import { cn } from "@/lib/utils";

export type ViewMode = "record" | "review";

interface NavLinksProps {
  mode: ViewMode;
  onChange: (mode: ViewMode) => void;
}

export function NavLinks({ mode, onChange }: NavLinksProps) {
  return (
    <nav className="flex items-center">
      <NavLink active={mode === "record"} onClick={() => onChange("record")}>
        Record
      </NavLink>
      <NavLink active={mode === "review"} onClick={() => onChange("review")}>
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
        "relative px-3 py-3 text-[13px] font-medium tracking-tight transition-colors",
        active
          ? "text-foreground"
          : "text-muted/60 hover:text-foreground",
      )}
    >
      {children}
      {active && (
        <span className="absolute inset-x-3 bottom-0 h-[2px] rounded-full bg-foreground" />
      )}
    </button>
  );
}
