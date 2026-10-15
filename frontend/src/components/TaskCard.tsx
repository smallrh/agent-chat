// 任务卡片：任务中心页与会话内任务面板共用同一个渲染。
// 保持了原有的业务逻辑（状态、进度条、重跑/取消判断），只升级视觉。

import { useState } from "react";
import type { TaskItem } from "../api/tasks";
import { CheckCircle, XCircle, Clock, Spinner } from "@phosphor-icons/react";

const STATUS_META: Record<
  string,
  { label: string; text: string; bg: string; bar: string; icon?: string }
> = {
  pending: {
    label: "等待中",
    text: "text-zinc-500 dark:text-zinc-400",
    bg: "bg-zinc-100 dark:bg-zinc-800",
    bar: "bg-zinc-400 dark:bg-zinc-600",
  },
  running: {
    label: "进行中",
    text: "text-brand-600 dark:text-brand-400",
    bg: "bg-brand-50 dark:bg-brand-900/30",
    bar: "bg-brand-500",
  },
  succeeded: {
    label: "已完成",
    text: "text-emerald-600 dark:text-emerald-400",
    bg: "bg-emerald-50 dark:bg-emerald-950/40",
    bar: "bg-emerald-500",
  },
  failed: {
    label: "失败",
    text: "text-red-600 dark:text-red-400",
    bg: "bg-red-50 dark:bg-red-950/40",
    bar: "bg-red-500",
  },
  cancelled: {
    label: "已取消",
    text: "text-zinc-500 dark:text-zinc-400",
    bg: "bg-zinc-100 dark:bg-zinc-800",
    bar: "bg-zinc-400 dark:bg-zinc-600",
  },
};

const KIND_LABELS: Record<string, string> = {
  demo: "演示",
  research: "调研",
};

export function statusMeta(status: string) {
  return STATUS_META[status] ?? STATUS_META.pending;
}

export function kindLabel(kind?: string): string {
  if (!kind) return "任务";
  return KIND_LABELS[kind] ?? kind;
}

export function isActive(status: string): boolean {
  return status === "pending" || status === "running";
}

export function formatTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("zh-CN", { hour12: false });
}

interface TaskCardProps {
  task: TaskItem;
  compact?: boolean;
  onCancel?: (taskId: string) => void;
  onRetry?: (taskId: string) => void;
  busy?: boolean;
  onOpenConversation?: (conversationId: string) => void;
}

export function TaskCard({
  task,
  compact,
  onCancel,
  onRetry,
  busy,
  onOpenConversation,
}: TaskCardProps) {
  const [open, setOpen] = useState(false);
  const meta = statusMeta(task.status);
  const active = isActive(task.status);
  const pct = Math.max(0, Math.min(100, Number(task.progress ?? 0)));

  const result = task.result as Record<string, unknown> | null;
  const report = typeof result?.report === "string" ? result.report : "";
  const summary = typeof result?.summary === "string" ? result.summary : "";
  const sources = Array.isArray(result?.sources) ? result.sources : [];

  const StatusIcon = (() => {
    if (task.status === "running")
      return <Spinner className="animate-spin" size={14} />;
    if (task.status === "succeeded")
      return <CheckCircle size={14} weight="fill" />;
    if (task.status === "failed") return <XCircle size={14} weight="fill" />;
    if (task.status === "cancelled")
      return <XCircle size={14} weight="fill" />;
    return <Clock size={14} />;
  })();

  return (
    <div className="rounded-2xl border border-zinc-200 bg-white p-3.5 shadow-sm transition hover:shadow-md dark:border-zinc-800 dark:bg-zinc-900/60">
      <div className="flex items-start gap-2.5">
        <div className="min-w-0 flex-1">
          {/* Status row */}
          <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
            <span
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${meta.bg} ${meta.text}`}
            >
              {StatusIcon}
              {meta.label}
            </span>
            <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
              {kindLabel(task.kind)}
            </span>
            {task.steps_total ? (
              <span className="text-[11px] text-zinc-400">
                {task.steps_done ?? 0}/{task.steps_total}
              </span>
            ) : null}
          </div>
          {/* Title */}
          <div className="break-words text-[14px] font-medium text-zinc-900 dark:text-zinc-100">
            {task.title || "未命名任务"}
          </div>
          {!compact && task.goal ? (
            <div className="mt-0.5 break-words text-[12px] text-zinc-500 dark:text-zinc-400">
              目标：{task.goal}
            </div>
          ) : null}
        </div>

        {/* Actions */}
        <div className="flex shrink-0 items-center gap-1">
          {active && onCancel ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => onCancel(task.task_id)}
              className="rounded-full border border-zinc-200 px-2.5 py-1 text-[11px] text-zinc-600 transition hover:bg-zinc-50 hover:text-red-500 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-red-400"
            >
              取消
            </button>
          ) : null}
          {!active && onRetry ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => onRetry(task.task_id)}
              title="重新执行这个任务"
              className="rounded-full border border-zinc-200 px-2.5 py-1 text-[11px] text-zinc-600 transition hover:bg-zinc-50 hover:text-brand-600 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-brand-400"
            >
              重跑
            </button>
          ) : null}
        </div>
      </div>

      {/* Progress bar */}
      <div className="mt-3">
        <div className="h-1 w-full overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800">
          <div
            className={`h-full rounded-full transition-all duration-500 ${meta.bar}`}
            style={{ width: `${active && pct === 0 ? 8 : pct}%` }}
          />
        </div>
        <div className="mt-1 flex items-center gap-2 text-[11px] text-zinc-500 dark:text-zinc-400">
          <span className="tabular-nums">{pct}%</span>
          {task.step ? (
            <span className="min-w-0 flex-1 truncate">{task.step}</span>
          ) : (
            <span className="flex-1" />
          )}
        </div>
      </div>

      {task.error ? (
        <div className="mt-2 rounded-xl bg-red-50 px-2.5 py-1.5 text-[11px] text-red-600 dark:bg-red-950/40 dark:text-red-300">
          {task.error}
        </div>
      ) : null}

      {/* Result toggle */}
      {(report || summary || sources.length > 0) && (
        <>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="mt-2 text-[11px] font-medium text-brand-600 transition hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
          >
            {open ? "收起结果" : "查看结果"}
          </button>
          {open && (
            <div className="mt-2 max-h-64 overflow-auto rounded-xl border border-zinc-100 bg-zinc-50 px-3 py-2 text-[12px] text-zinc-700 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-300">
              {report ? (
                <div className="whitespace-pre-wrap break-words">{report}</div>
              ) : (
                <div className="whitespace-pre-wrap break-words">{summary}</div>
              )}
              {sources.length > 0 && (
                <div className="mt-2 border-t border-zinc-200 pt-2 dark:border-zinc-700">
                  <div className="mb-1 text-[11px] text-zinc-400">参考来源</div>
                  {sources.slice(0, 10).map((s, i) => {
                    const item = s as Record<string, string>;
                    return (
                      <a
                        key={`${item.url}-${i}`}
                        href={item.url}
                        target="_blank"
                        rel="noreferrer"
                        className="block truncate text-[11px] text-brand-600 hover:underline dark:text-brand-400"
                      >
                        {item.title || item.url}
                      </a>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </>
      )}

      <div className="mt-2 flex items-center justify-between text-[11px] text-zinc-400 dark:text-zinc-500">
        <span>{formatTime(task.created_at)}</span>
        {onOpenConversation ? (
          <button
            type="button"
            onClick={() => onOpenConversation(task.conversation_id)}
            className="font-medium text-brand-600 transition hover:underline dark:text-brand-400"
          >
            前往会话
          </button>
        ) : null}
      </div>
    </div>
  );
}
