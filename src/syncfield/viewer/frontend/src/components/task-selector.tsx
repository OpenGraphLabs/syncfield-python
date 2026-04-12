import { useCallback, useState } from "react";
import type { Task } from "@/hooks/use-tasks";
import { cn } from "@/lib/utils";

interface TaskSelectorProps {
  tasks: Task[];
  currentTask: string | null;
  createTask: (name: string) => Promise<boolean>;
  deleteTask: (name: string) => Promise<boolean>;
  selectTask: (name: string | null) => Promise<void>;
}

/**
 * Task selector for Recording mode.
 *
 * Receives task state from the parent so it shares the same hook
 * instance as the Record button's hasTask gate.
 */
export function TaskSelector({
  tasks,
  currentTask,
  createTask,
  deleteTask,
  selectTask,
}: TaskSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [newTaskName, setNewTaskName] = useState("");

  const handleCreate = useCallback(async () => {
    const name = newTaskName.trim();
    if (!name) return;
    const ok = await createTask(name);
    if (ok) {
      await selectTask(name);
      setNewTaskName("");
      setIsOpen(false);
    }
  }, [newTaskName, createTask, selectTask]);

  return (
    <div className="relative flex items-center gap-2 border-b px-4 py-1.5">
      <span className="text-[10px] font-medium uppercase tracking-wider text-muted">
        Task
      </span>

      <button
        onClick={() => setIsOpen(!isOpen)}
        className={cn(
          "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs transition-colors",
          currentTask
            ? "bg-primary/8 font-medium text-primary"
            : "border border-dashed border-foreground/20 text-muted",
          "hover:bg-foreground/5",
        )}
      >
        {currentTask ?? "Select…"}
        <svg
          width="10"
          height="10"
          viewBox="0 0 16 16"
          fill="none"
          className={cn("transition-transform", isOpen && "rotate-180")}
        >
          <path
            d="M4 6L8 10L12 6"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {currentTask && (
        <button
          onClick={() => selectTask(null)}
          className="rounded-md p-0.5 text-muted transition-colors hover:text-foreground"
        >
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
            <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
      )}

      {/* Dropdown */}
      {isOpen && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setIsOpen(false)} />
          <div className="absolute left-14 top-full z-40 mt-1 w-56 overflow-hidden rounded-xl border bg-card shadow-lg">
            {tasks.length > 0 && (
              <ul className="max-h-48 overflow-y-auto py-1">
                {tasks.map((t) => (
                  <li
                    key={t.name}
                    onClick={() => {
                      selectTask(t.name);
                      setIsOpen(false);
                    }}
                    className={cn(
                      "group flex items-center justify-between px-3 py-2 text-xs",
                      "cursor-pointer transition-colors hover:bg-foreground/5",
                      currentTask === t.name && "bg-primary/5 font-medium text-primary",
                    )}
                  >
                    <span className="truncate">{t.name}</span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        deleteTask(t.name);
                      }}
                      className="shrink-0 rounded p-0.5 text-muted opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100"
                    >
                      <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                        <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                      </svg>
                    </button>
                  </li>
                ))}
              </ul>
            )}

            <div className={cn("px-2 py-2", tasks.length > 0 && "border-t")}>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  handleCreate();
                }}
                className="flex gap-1.5"
              >
                <input
                  type="text"
                  value={newTaskName}
                  onChange={(e) => setNewTaskName(e.target.value)}
                  placeholder={tasks.length === 0 ? "Create first task…" : "New task…"}
                  className="flex-1 rounded-md border bg-transparent px-2 py-1.5 text-xs placeholder:text-muted focus:border-primary focus:outline-none"
                  autoFocus
                />
                <button
                  type="submit"
                  disabled={!newTaskName.trim()}
                  className="rounded-md bg-primary px-2.5 py-1.5 text-[10px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-40"
                >
                  Add
                </button>
              </form>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
