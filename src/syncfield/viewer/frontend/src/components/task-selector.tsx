import { useCallback, useState } from "react";
import { useTasks } from "@/hooks/use-tasks";
import { cn } from "@/lib/utils";

/**
 * Task selector for Recording mode — dropdown with inline create/delete.
 *
 * Displays above the control panel. Shows the current task and allows
 * selecting from the task list or creating a new one.
 */
export function TaskSelector() {
  const { tasks, currentTask, createTask, deleteTask, selectTask } =
    useTasks();
  const [isOpen, setIsOpen] = useState(false);
  const [newTaskName, setNewTaskName] = useState("");
  const [isCreating, setIsCreating] = useState(false);

  const handleCreate = useCallback(async () => {
    if (!newTaskName.trim()) return;
    setIsCreating(true);
    const ok = await createTask(newTaskName.trim());
    if (ok) {
      await selectTask(newTaskName.trim());
      setNewTaskName("");
    }
    setIsCreating(false);
    setIsOpen(false);
  }, [newTaskName, createTask, selectTask]);

  const handleDelete = useCallback(
    async (name: string, e: React.MouseEvent) => {
      e.stopPropagation();
      await deleteTask(name);
    },
    [deleteTask],
  );

  return (
    <div className="relative flex items-center gap-2 border-b px-4 py-1.5">
      <span className="text-[10px] font-medium uppercase tracking-wider text-muted">
        Task
      </span>

      {/* Dropdown trigger */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={cn(
          "flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs transition-colors",
          "hover:bg-foreground/5",
          currentTask ? "font-medium" : "text-muted",
        )}
      >
        {currentTask ?? "Select task…"}
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

      {/* Clear button */}
      {currentTask && (
        <button
          onClick={() => selectTask(null)}
          className="text-[10px] text-muted hover:text-foreground"
        >
          Clear
        </button>
      )}

      {/* Dropdown */}
      {isOpen && (
        <>
          <div
            className="fixed inset-0 z-30"
            onClick={() => setIsOpen(false)}
          />
          <div className="absolute left-14 top-full z-40 mt-1 w-64 rounded-lg border bg-card shadow-lg">
            {/* Task list */}
            {tasks.length > 0 ? (
              <ul className="max-h-48 overflow-y-auto py-1">
                {tasks.map((t) => (
                  <li
                    key={t.name}
                    onClick={() => {
                      selectTask(t.name);
                      setIsOpen(false);
                    }}
                    className={cn(
                      "flex items-center justify-between px-3 py-1.5 text-xs",
                      "cursor-pointer transition-colors hover:bg-foreground/5",
                      currentTask === t.name && "bg-primary/5 text-primary",
                    )}
                  >
                    <span className="truncate">{t.name}</span>
                    <button
                      onClick={(e) => handleDelete(t.name, e)}
                      className="shrink-0 rounded p-0.5 text-muted opacity-0 transition-opacity hover:text-destructive group-hover:opacity-100 [li:hover_&]:opacity-100"
                    >
                      <svg
                        width="12"
                        height="12"
                        viewBox="0 0 16 16"
                        fill="none"
                      >
                        <path
                          d="M4 4L12 12M12 4L4 12"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                        />
                      </svg>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="px-3 py-3 text-center text-[11px] text-muted">
                No tasks yet
              </p>
            )}

            {/* Create new task */}
            <div className="border-t px-2 py-2">
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
                  placeholder="New task name…"
                  className="flex-1 rounded-md border bg-transparent px-2 py-1 text-xs placeholder:text-muted focus:border-primary focus:outline-none"
                  autoFocus
                />
                <button
                  type="submit"
                  disabled={!newTaskName.trim() || isCreating}
                  className="rounded-md bg-primary px-2 py-1 text-[10px] font-medium text-primary-foreground disabled:opacity-40"
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
