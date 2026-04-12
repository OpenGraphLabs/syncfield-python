import { useCallback, useEffect, useState } from "react";

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
    const res = await fetch("/api/task/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: name }),
    });
    if (res.ok) {
      setCurrentTask(name);
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
