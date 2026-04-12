import { useEffect, useState } from "react";

interface FooterProps {
  outputDir: string;
}

/**
 * Footer bar — shows output folder path with icon, and wall clock.
 */
export function Footer({ outputDir }: FooterProps) {
  const [now, setNow] = useState(() => formatClock());

  useEffect(() => {
    const id = setInterval(() => setNow(formatClock()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <footer className="flex h-8 items-center justify-between gap-4 border-t px-4 text-[11px] text-muted">
      {/* Output directory with folder icon */}
      <div className="flex min-w-0 items-center gap-1.5">
        <svg
          width="14"
          height="14"
          viewBox="0 0 16 16"
          fill="none"
          className="shrink-0"
        >
          <path
            d="M2 4.5C2 3.67 2.67 3 3.5 3H6.29a1 1 0 0 1 .7.29L8 4.3h4.5c.83 0 1.5.67 1.5 1.5V12c0 .83-.67 1.5-1.5 1.5h-9A1.5 1.5 0 0 1 2 12V4.5Z"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeLinejoin="round"
          />
        </svg>
        <span className="truncate font-mono">{outputDir}</span>
      </div>

      {/* Wall clock */}
      <span className="shrink-0 font-mono">{now}</span>
    </footer>
  );
}

function formatClock(): string {
  return new Date().toISOString().replace("T", " ").slice(0, 19);
}
