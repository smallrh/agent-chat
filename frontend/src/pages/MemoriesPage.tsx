// 记忆中心：列出 AI 记住的用户事实/偏好，支持按类型筛选、溯源与删除。
// 后端 /memories 接口早已就绪，这里把它在前端露出来（对应《现状和不足.md》Frontend 缺点 1）。

import { Brain } from "@phosphor-icons/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  deleteMemory,
  getMemorySource,
  listMemories,
  type MemoryItem,
  type MemorySourceResponse,
} from "../api/memories";

const TYPE_LABELS: Record<string, string> = {
  fact: "事实",
  preference: "偏好",
  name: "姓名",
  profile: "画像",
  experience: "经历",
  knowledge: "知识",
};

function typeLabel(t: string): string {
  return TYPE_LABELS[t] ?? t;
}

function formatTime(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("zh-CN", { hour12: false });
}

export function MemoriesPage() {
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<string>("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [sources, setSources] = useState<Record<string, MemorySourceResponse>>({});
  const [sourceLoadingId, setSourceLoadingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await listMemories({
        memory_type: typeFilter || undefined,
        limit: 100,
      });
      setItems(res.items ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载记忆失败");
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [typeFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  // 筛选项来自已有数据的类型集合（后端没有单独的类型枚举接口）
  const types = useMemo(
    () => Array.from(new Set(items.map((i) => i.memory_type))).sort(),
    [items]
  );

  const toggleSource = async (id: string) => {
    if (expandedId === id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(id);
    if (sources[id]) return;
    setSourceLoadingId(id);
    try {
      const s = await getMemorySource(id);
      setSources((prev) => ({ ...prev, [id]: s }));
    } catch {
      // 溯源失败不阻断：显示一个空态提示即可
      setSources((prev) => ({
        ...prev,
        [id]: { memory_id: id, content: "", messages: [] },
      }));
    } finally {
      setSourceLoadingId(null);
    }
  };

  const handleDelete = async (item: MemoryItem) => {
    if (!window.confirm(`确定删除这条记忆吗？\n\n${item.content}`)) return;
    setDeletingId(item.memory_id);
    try {
      await deleteMemory(item.memory_id);
      setItems((prev) => prev.filter((x) => x.memory_id !== item.memory_id));
      if (expandedId === item.memory_id) setExpandedId(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "删除失败");
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      {/* 头部：不在滚动容器内，去掉无效的 sticky */}
      <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-zinc-200 bg-zinc-50/85 px-3 py-2 backdrop-blur-md dark:border-zinc-800 dark:bg-zinc-950/85 sm:px-6">

        <div className="flex min-w-0 items-center gap-2 text-[13px] font-semibold text-zinc-800 dark:text-zinc-100">
          <Brain size={18} className="text-brand-600 dark:text-brand-400" />
          记忆中心
          <span className="hidden text-xs font-normal text-zinc-400 sm:inline dark:text-zinc-500">
            AI 在对话中记住的关于你的事实与偏好
          </span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="rounded-full border border-zinc-200 bg-white px-3 py-1 text-xs text-zinc-600 outline-none focus:border-brand-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-300"
          >
            <option value="">全部类型</option>
            {types.map((t) => (
              <option key={t} value={t}>
                {typeLabel(t)}
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
                <Brain size={48} weight="duotone" className="text-zinc-300 dark:text-zinc-600" />
              </div>
              <p className="text-sm text-zinc-500 dark:text-zinc-400">还没有记住任何内容</p>
              <p className="mt-1 text-xs">
                在对话里告诉 AI 你的偏好或信息，它会在这里沉淀下来
              </p>
            </div>
          )}

          {items.map((m) => {
            const open = expandedId === m.memory_id;
            const src = sources[m.memory_id];
            return (
              <div
                key={m.memory_id}
                className="rounded-2xl border border-zinc-200 bg-white p-3 shadow-sm sm:p-4 dark:border-zinc-800 dark:bg-zinc-900/60"
              >
                <div className="flex items-start gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="mb-1 flex flex-wrap items-center gap-1.5">
                      <span className="rounded-full bg-zinc-100 px-2 py-0.5 text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
                        {typeLabel(m.memory_type)}
                      </span>
                      {m.slot && (
                        <span className="rounded-full bg-brand-50 px-2 py-0.5 text-[11px] text-brand-700 dark:bg-brand-950/60 dark:text-brand-300">
                          {m.slot}
                        </span>
                      )}
                      <span className="text-[11px] text-zinc-400 dark:text-zinc-500">
                        重要度 {Number(m.importance ?? 0).toFixed(2)}
                      </span>
                      {m.confidence != null && (
                        <span className="text-[11px] text-zinc-400 dark:text-zinc-500">
                          置信度 {Number(m.confidence).toFixed(2)}
                        </span>
                      )}
                    </div>
                    <div className="whitespace-pre-wrap break-words text-sm text-zinc-800 dark:text-zinc-100">
                      {m.content}
                    </div>
                    <div className="mt-1 text-[11px] text-zinc-400 dark:text-zinc-500">
                      {formatTime(m.created_at)}
                    </div>
                  </div>

                  <div className="flex shrink-0 flex-col items-end gap-1.5">
                    <button
                      type="button"
                      onClick={() => void toggleSource(m.memory_id)}
                      className="rounded-full border border-zinc-200 px-3 py-0.5 text-[11px] text-zinc-500 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800"
                    >
                      {open ? "收起" : "溯源"}
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleDelete(m)}
                      disabled={deletingId === m.memory_id}
                      className="rounded-full border border-zinc-200 px-3 py-0.5 text-[11px] text-red-500 transition hover:bg-red-50 disabled:opacity-50 dark:border-red-900/50 dark:hover:bg-red-950/40"
                    >
                      {deletingId === m.memory_id ? "删除中…" : "删除"}
                    </button>
                  </div>
                </div>

                {open && (
                  <div className="mt-3 border-t border-zinc-100 pt-2 dark:border-zinc-800">
                    {sourceLoadingId === m.memory_id ? (
                      <div className="text-xs text-zinc-400 dark:text-zinc-500">加载来源…</div>
                    ) : src && src.messages.length > 0 ? (
                      <div className="space-y-1.5">
                        <div className="text-[11px] text-zinc-400 dark:text-zinc-500">
                          抽取自以下对话
                          {src.extract_model ? `（模型：${src.extract_model}）` : ""}
                        </div>
                        {src.messages.map((msg) => (
                          <div
                            key={msg.message_id}
                            className="rounded-2xl bg-zinc-50 px-3 py-1.5 text-xs dark:bg-zinc-800/60"
                          >
                            <span className="mr-1.5 text-zinc-400 dark:text-zinc-500">
                              {msg.role === "user" ? "我" : "AI"}
                            </span>
                            <span className="whitespace-pre-wrap break-words text-zinc-600 dark:text-zinc-300">
                              {msg.content}
                            </span>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="text-xs text-zinc-400 dark:text-zinc-500">
                        没有找到来源消息（可能来源会话已删除）
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
