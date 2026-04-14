import { useCallback, useEffect, useRef, useState } from "react";

export interface Task {
  name: string;
}

interface UseTasksReturn {
  tasks: Task[];
  currentTask: string | null;
  isLoading: boolean;
  refresh: () => Promise<void>;
  createTask: (name: string) => Promise<boolean>;
  deleteTask: (name: string) => Promise<boolean>;
  selectTask: (name: string | null) => Promise<void>;
}

export function useTasks(): UseTasksReturn {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [currentTask, setCurrentTask] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  // Holds the pre-click currentTask so ``selectTask`` can revert its
  // optimistic update if the server POST fails.
  const prevTaskRef = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    try {
      const [tasksRes, currentRes] = await Promise.all([
        fetch("/api/tasks"),
        fetch("/api/task/current"),
      ]);
      if (tasksRes.ok) {
        const data = await tasksRes.json();
        setTasks(data.tasks ?? []);
      }
      if (currentRes.ok) {
        const data = await currentRes.json();
        setCurrentTask(data.task ?? null);
      }
    } catch {
      // Silent fail
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const createTask = useCallback(
    async (name: string): Promise<boolean> => {
      const res = await fetch("/api/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (res.ok) {
        await refresh();
        return true;
      }
      return false;
    },
    [refresh],
  );

  const deleteTask = useCallback(
    async (name: string): Promise<boolean> => {
      const res = await fetch(`/api/tasks/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });
      if (res.ok) {
        if (currentTask === name) {
          await selectTask(null);
        }
        await refresh();
        return true;
      }
      return false;
    },
    [refresh, currentTask],
  );

  const selectTask = useCallback(async (name: string | null) => {
    // Optimistic update — flip the UI immediately so the click feels
    // instant, then fire the POST. If the server rejects the change
    // we revert; otherwise we leave the state where it already is.
    //
    // Why optimistic: when the viewer is running video/sensor/audio
    // streams at the same time, the browser's per-origin HTTP/1.1
    // connection pool (6 in Chrome/Safari) can fill up with long-lived
    // MJPEG and SSE subscriptions. A brand-new ``fetch`` gets queued
    // until a slot frees, which the user perceives as "clicking the
    // task does nothing". Updating state first decouples the UI
    // response from the network pool's availability.
    setCurrentTask((prev) => {
      // Store previous value on the ref so we can revert on failure
      // without fighting stale closures.
      prevTaskRef.current = prev;
      return name;
    });
    try {
      const res = await fetch("/api/task/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: name }),
        keepalive: true,
      });
      if (!res.ok) {
        setCurrentTask(prevTaskRef.current);
      }
    } catch {
      setCurrentTask(prevTaskRef.current);
    }
  }, []);

  return {
    tasks,
    currentTask,
    isLoading,
    refresh,
    createTask,
    deleteTask,
    selectTask,
  };
}
