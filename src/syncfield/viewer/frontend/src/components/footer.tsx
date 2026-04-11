import { formatPathTail } from "@/lib/format";

interface FooterProps {
  outputDir: string;
}

/**
 * Footer bar — shows output path and wall clock.
 */
export function Footer({ outputDir }: FooterProps) {
  const now = new Date().toISOString().replace("T", " ").slice(0, 19);

  return (
    <footer className="flex h-8 items-center justify-between border-t px-4 text-[11px] text-muted">
      <span className="truncate font-mono">{formatPathTail(outputDir)}</span>
      <span className="shrink-0 font-mono">{now}</span>
    </footer>
  );
}
