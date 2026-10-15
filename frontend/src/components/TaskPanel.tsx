// 会话内任务面板：常驻一条 SSE 订阅，实时显示本会话后台任务的进度。
//
// 为什么面板自己管 SSE 而不是让 ChatPage 管：
//   - 任务可以在「用户已经关掉对话流」之后才跑完（结果由后端写回库），
//     面板的订阅生命周期和对话流不同，混在一起很容易漏事件；
//   - 面板要能在切换会话时整体重建订阅，独立组件更好收敛。

import { FolderOpen, X } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelTask,
  createTask,
  getTask,
  listTasks,
  retryTask,
  streamTasks,
  type TaskItem,
} from "../api/tasks";
import { TaskCard } from "./TaskCard";

interface TaskPanelProps {
  conversationId: string | null;
  /** 任务完成时回调：由 ChatPage 把结果插进对话流 */
  onCompleted?: (task: TaskItem) => void;
  /** 小屏以抽屉形式呈现时，提供一个关闭入口 */
  onClose?: () => void;
}

function upsert(list: TaskItem[], patch: Partial<TaskItem> & { task_id: string }) {
  const idx = list.findIndex((t) => t.task_id === patch.task_id);
  if (idx === -1) {
    // 没见过的任务（例如在别处创建的）插到最前面
    return [{ ...(patch as TaskItem) }, ...list];
  }
  const next = [...list];
  next[idx] = { ...next[idx], ...patch };
  return next;
}

export function TaskPanel({ conversationId, onCompleted, onClose }: TaskPanelProps) {
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [kind, setKind] = useState("research");
  const completedRef = useRef<Set<string>>(new Set());
  // onCompleted 放进 ref：避免父组件每次重渲染都重建 SSE 订阅
  const onCompletedRef = useRef(onCompleted);
  onCompletedRef.current = onCompleted;

  // ---- 拉取本会话任务 ----
  useEffect(() => {
    if (!conversationId) {
      setTasks([]);
      return;
    }
    let alive = true;
    setLoading(true);
    listTasks({ conversation_id: conversationId, limit: 50 })
      .then((res) => {
        if (!alive) return;
        setTasks(res.items ?? []);
        // 已完成的不要重复插入对话（刷新页面后不应再插一遍）
        for (const t of res.items ?? []) {
          if (t.status === "succeeded") completedRef.current.add(t.task_id);
        }
      })
      .catch(() => {
        if (alive) setError("任务列表加载失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [conversationId]);

  // ---- 订阅 SSE 实时进度 ----
  useEffect(() => {
    if (!conversationId) return;
    const ac = new AbortController();

    (async () => {
      try {
        for await (const evt of streamTasks(conversationId, ac.signal)) {
          const d = JSON.parse(evt.data || "{}");
          const id = d?.task_id as string | undefined;
          if (!id) continue;

          if (evt.event === "task.progress") {
            setTasks((prev) =>
              upsert(prev, {
                task_id: id,
                progress: Number(d.progress ?? 0),
                step: d.step ?? "",
                steps_done: Number(d.steps_done ?? 0),
                steps_total: Number(d.steps_total ?? 0),
                status: "running",
              })
            );
          } else if (evt.event === "task.created") {
            setTasks((prev) =>
              upsert(prev, {
                task_id: id,
                title: d.title ?? "任务",
                goal: d.goal ?? "",
                kind: d.kind ?? "demo",
                status: "pending",
                progress: 0,
                step: d.step ?? "",
                conversation_id: conversationId,
              })
            );
          } else if (evt.event === "task.running") {
            // 重跑时先清掉旧结果，避免进度条下面还挂着上一轮的报告
            setTasks((prev) =>
              upsert(prev, { task_id: id, status: "running", error: null, result: null })
            );
            completedRef.current.delete(id);
          } else if (evt.event === "task.completed") {
            setTasks((prev) =>
              upsert(prev, {
                task_id: id,
                status: "succeeded",
                progress: 100,
                result: d.result ?? null,
                error: null,
              })
            );
            if (!completedRef.current.has(id)) {
              completedRef.current.add(id);
              // 事件里只有 task_id，插入对话需要完整结果（含 report）
              const full = await getTask(id).catch(() => null);
              onCompletedRef.current?.(
                full ?? ({ task_id: id, status: "succeeded" } as TaskItem)
              );
            }
          } else if (evt.event === "task.failed") {
            setTasks((prev) =>
              upsert(prev, {
                task_id: id,
                status: "failed",
                error: d.error ?? "执行失败",
              })
            );
          } else if (evt.event === "task.cancelled") {
            setTasks((prev) => upsert(prev, { task_id: id, status: "cancelled" }));
          }
        }
      } catch {
        // 断流不报错：面板退化成「靠刷新看结果」，不打断聊天
      }
    })();

    return () => ac.abort();
  }, [conversationId]);

  const handleCancel = useCallback(async (taskId: string) => {
    setBusyId(taskId);
    try {
      await cancelTask(taskId);
      setTasks((prev) =>
        prev.map((t) => (t.task_id === taskId ? { ...t, status: "cancelled" } : t))
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "取消失败");
    } finally {
      setBusyId(null);
    }
  }, []);

  const handleRetry = useCallback(async (taskId: string) => {
    setBusyId(taskId);
    try {
      completedRef.current.delete(taskId);
      await retryTask(taskId);
      setTasks((prev) =>
        prev.map((t) =>
          t.task_id === taskId
            ? { ...t, status: "pending", progress: 0, error: null, result: null }
            : t
        )
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "重跑失败");
    } finally {
      setBusyId(null);
    }
  }, []);

  const handleCreate = async () => {
    if (!conversationId || !title.trim()) return;
    setBusyId("__create__");
    setError(null);
    try {
      await createTask({
        conversation_id: conversationId,
        title: title.trim(),
        goal: goal.trim(),
        kind,
      });
      setTitle("");
      setGoal("");
      setShowForm(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "创建任务失败");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* 头部：h-12 与聊天头部对齐；小屏抽屉形态下多一个关闭按钮 */}
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-zinc-200 px-3 dark:border-zinc-800">
        <span className="text-[13px] font-semibold text-zinc-800 dark:text-zinc-100">
          后台任务
        </span>
        <span className="rounded-full bg-zinc-100 px-1.5 py-0.5 text-[11px] text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
          {tasks.filter((t) => t.status === "running" || t.status === "pending").length} 进行中
        </span>
        <button
          type="button"
          onClick={() => setShowForm((v) => !v)}
          disabled={!conversationId}
          className="ml-auto rounded-full border border-zinc-200 px-3 py-0.5 text-xs text-zinc-600 transition hover:bg-zinc-100 disabled:opacity-40 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-800"
        >
          {showForm ? "收起" : "+ 新建"}
        </button>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭任务面板"
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-700 lg:hidden dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
          >
            <X size={16} />
          </button>
        )}
      </div>

      {error && (
        <div className="mx-3 mt-2 rounded-full border border-red-200 bg-red-50 px-2.5 py-1.5 text-[11px] text-red-600 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-400">
          {error}
        </div>
      )}

      {showForm && (
        <div className="space-y-2 border-b border-zinc-200 px-3 py-2 dark:border-zinc-800">
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="任务标题，如：调研广州天河区的酒店"
            className="w-full rounded-full border border-zinc-200 bg-white px-3 py-1.5 text-xs outline-none focus:border-brand-400 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200 dark:placeholder:text-zinc-500"
          />
          <input
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="目标 / 要求（可选，越具体越好）"
            className="w-full rounded-full border border-zinc-200 bg-white px-3 py-1.5 text-xs outline-none focus:border-brand-400 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-200 dark:placeholder:text-zinc-500"
          />
          <div className="flex items-center gap-2">
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              className="rounded-full border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-600 outline-none dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300"
            >
              <option value="research">调研（联网检索并汇总）</option>
              <option value="demo">演示（模拟步骤）</option>
            </select>
            <button
              type="button"
              onClick={() => void handleCreate()}
              disabled={busyId === "__create__" || !title.trim()}
              className="ml-auto rounded-full bg-brand-600 px-3 py-1 text-xs font-medium text-white transition hover:bg-brand-700 disabled:opacity-40"
            >
              {busyId === "__create__" ? "创建中…" : "创建"}
            </button>
          </div>
        </div>
      )}

      <div className="flex-1 space-y-2 overflow-y-auto px-3 py-3">
        {loading && tasks.length === 0 && (
          <div className="py-8 text-center text-xs text-zinc-400 dark:text-zinc-500">加载中…</div>
        )}
        {!loading && tasks.length === 0 && (
          <div className="py-8 text-center text-zinc-400 dark:text-zinc-500">
            <div className="mb-2 flex justify-center">
              <FolderOpen size={32} weight="duotone" className="text-zinc-300 dark:text-zinc-600" />
            </div>
            <p className="text-xs">这个会话还没有后台任务</p>
            <p className="mt-1 text-[11px]">
              让 AI 「去调研一下…」或点右上角新建
            </p>
          </div>
        )}
        {tasks.map((t) => (
          <TaskCard
            key={t.task_id}
            task={t}
            compact
            busy={busyId === t.task_id}
            onCancel={(id) => void handleCancel(id)}
            onRetry={(id) => void handleRetry(id)}
          />
        ))}
      </div>
    </div>
  );
}
