import type { SyncMode } from "../hooks/useBeforeAfter";

interface Props {
  mode: SyncMode;
  disabled: boolean;
  onChange: (next: SyncMode) => void;
}

const BASE =
  "px-4 py-1.5 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50";
const ACTIVE = "bg-zinc-900 text-white";
const INACTIVE = "text-zinc-500 hover:text-zinc-800";

export default function BeforeAfterToggle({ mode, disabled, onChange }: Props) {
  return (
    <div className="inline-flex rounded-full border border-zinc-200 bg-white p-0.5">
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange("before")}
        className={`${BASE} rounded-full ${mode === "before" ? ACTIVE : INACTIVE}`}
      >
        Before
      </button>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange("after")}
        className={`${BASE} rounded-full ${mode === "after" ? ACTIVE : INACTIVE}`}
      >
        After
      </button>
    </div>
  );
}
