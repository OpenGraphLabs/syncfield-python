import { useState } from "react";
import type { IncidentSnapshot, Severity } from "../lib/types";

const SEVERITY_ICON: Record<Severity, string> = {
  info: "·",
  warning: "⚠",
  error: "⛔",
  critical: "⛔",
};

const SEVERITY_COLOR: Record<Severity, string> = {
  info: "text-slate-400",
  warning: "text-yellow-400",
  error: "text-orange-400",
  critical: "text-red-500",
};

function formatAgo(s: number): string {
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function IncidentCard({ inc, isOpen }: { inc: IncidentSnapshot; isOpen: boolean }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <button
      className="block w-full text-left px-3 py-2 rounded border border-slate-800 hover:bg-slate-900 mb-1"
      onClick={() => setExpanded((v) => !v)}
    >
      <div className="flex items-start gap-2">
        <span className={`text-lg leading-none ${SEVERITY_COLOR[inc.severity]}`}>
          {SEVERITY_ICON[inc.severity]}
        </span>
        <div className="flex-1 min-w-0">
          <div className="text-sm text-slate-100 truncate">
            <span className="text-slate-400 font-mono">{inc.stream_id}</span>
            {" · "}
            {inc.title}
          </div>
          <div className="text-xs text-slate-500">
            {isOpen ? "opened " : "recovered "}
            {formatAgo(inc.ago_s)}
            {" · "}
            {inc.event_count} event{inc.event_count === 1 ? "" : "s"}
            {inc.artifacts.length > 0 &&
              inc.artifacts.map((a) => ` · ${a.kind} attached`).join("")}
          </div>
          {expanded && inc.detail && (
            <div className="mt-1 text-xs text-slate-400 font-mono break-all">
              {inc.detail}
            </div>
          )}
          {expanded &&
            inc.artifacts.map((a) => (
              <div key={a.path} className="mt-1 text-xs text-slate-400 font-mono break-all">
                {a.kind}: {a.path}
              </div>
            ))}
        </div>
      </div>
    </button>
  );
}

export function IncidentPanel({
  active,
  resolved,
}: {
  active: IncidentSnapshot[];
  resolved: IncidentSnapshot[];
}) {
  return (
    <section className="p-3 border border-slate-800 rounded">
      <header className="text-xs uppercase tracking-wide text-slate-400 mb-2">
        Active Issues ({active.length})
      </header>
      {active.length === 0 ? (
        <div className="text-xs text-slate-600 mb-3">None — all clear.</div>
      ) : (
        <div className="mb-3">
          {active.map((inc) => (
            <IncidentCard key={inc.id} inc={inc} isOpen />
          ))}
        </div>
      )}
      <header className="text-xs uppercase tracking-wide text-slate-400 mb-2">
        Resolved this session ({resolved.length})
      </header>
      {resolved.length === 0 ? (
        <div className="text-xs text-slate-600">None.</div>
      ) : (
        resolved.map((inc) => <IncidentCard key={inc.id} inc={inc} isOpen={false} />)
      )}
    </section>
  );
}
