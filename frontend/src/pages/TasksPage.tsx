// 任务中心：跨会话查看「我全部后台任务」的状态、进度与结果。
// 会话内实时进度看 ChatPage 右侧的任务面板；这里管的是全局视角
// （在别的会话跑了任务、想回来看一眼结果，不必一个个会话翻）。

import { CheckSquareOffset, ListChecks } from "@phosphor-icons/react";
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  cancelTask,
  listTasks,
  retryTask,
  type TaskItem,
} from "../api/tasks";
import { TaskCard, isActive } from "../components/TaskCard";

type StatusFilter = "" | "running" | "succeeded" | "failed" | "cancelled";

const FILTERS: { value: StatusFilter; label: string }[] = [
  { value: "", label: "全部" },
  { value: "running", label: "进行中" },
  { value: "succeeded", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
];

export function TasksPage() {
  const navigate = useNavigate();
  const [items, setItems] = useState<TaskItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<StatusFilter>("");
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // 不带 conversation_id → 后端按 user 跨会话返回
      const res = await listTasks({ status: status || undefined, limit: 100 });
      setItems(res.items ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载任务失败");
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    void load();
  }, [load]);

  // 有任务在跑时自动轮询（任务中心没有常驻 SSE，用轻量轮询代替：
  // 只有「存在进行中任务」时才 3 秒一刷，全部终态后不再打扰后端）
  const hasActive = items.some((t) => isActive(t.status));
  useEffect(() => {
    if (!hasActive) return;
    const timer = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(timer);
  }, [hasActive, load]);

  const handleCancel = async (taskId: string) => {
    setBusyId(taskId);
    try {
      await cancelTask(taskId);
      setItems((prev) =>
        prev.map((t) => (t.task_id === taskId ? { ...t, status: "cancelled" } : t))
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "取消失败");
    } finally {
      setBusyId(null);
    }
  };

  const handleRetry = async (taskId: string) => {
    setBusyId(taskId);
    try {
      await retryTask(taskId);
      setItems((prev) =>
        prev.map((t) =>
          t.task_id === taskId
            ? { ...t, status: "pending", progress: 0, error: null, result: null }
            : t
        )
      );
      // 重跑后立刻进入轮询（hasActive 会变 true）
      setTimeout(() => void load(), 500);
    } catch (e) {
      setError(e instanceof Error ? e.message : "重跑失败");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      {/* 头部：这里不在滚动容器内，不需要 sticky（之前写着 sticky 是无效的） */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-zinc-200 bg-zinc-50/85 px-3 py-2 backdrop-blur-md dark:border-zinc-800 dark:bg-zinc-950/85 sm:px-6">

        <div className="flex min-w-0 items-center gap-2 text-[13px] font-semibold text-zinc-800 dark:text-zinc-100">
          <ListChecks size={18} className="text-brand-600 dark:text-brand-400" />
          任务中心
          <span className="hidden text-xs font-normal text-zinc-400 sm:inline dark:text-zinc-500">
            后台任务的进度、结果与重跑
          </span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as StatusFilter)}
            className="rounded-full border border-zinc-200 bg-white px-3 py-1 text-xs text-zinc-600 outline-none focus:border-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300"
          >
            {FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="rounded-full border border-zinc-200 px-3 py-1 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
          >
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-4 sm:px-6 sm:py-6">
        <div className="mx-auto w-full max-w-3xl space-y-3">
          {error && (
            <div className="rounded-2xl border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-600 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-400">
              {error}
            </div>
          )}

          {loading && items.length === 0 && (
            <div className="py-16 text-center text-sm text-zinc-400 dark:text-zinc-500">加载中…</div>
          )}

          {!loading && items.length === 0 && !error && (
            <div className="py-16 text-center text-zinc-400 dark:text-zinc-500">
              <div className="mb-3 flex justify-center">
                <CheckSquareOffset size={48} weight="duotone" className="text-zinc-300 dark:text-zinc-600" />
              </div>
              <p className="text-sm text-zinc-500 dark:text-zinc-400">还没有任何后台任务</p>
              <p className="mt-1 text-xs">
                在对话里让 AI 去「调研…」，或在会话右侧任务面板手动新建
              </p>
            </div>
          )}

          {items.map((t) => (
            <TaskCard
              key={t.task_id}
              task={t}
              busy={busyId === t.task_id}
              onCancel={(id) => void handleCancel(id)}
              onRetry={(id) => void handleRetry(id)}
              onOpenConversation={(cid) => navigate(`/?conversation=${cid}`)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
