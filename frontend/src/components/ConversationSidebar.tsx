import { useEffect, type MouseEvent as ReactMouseEvent } from "react";
import type { ConversationItem } from "../api/conversations";
import {
  Plus,
  CaretLeft,
  CaretRight,
  X,
  Pencil,
  Trash,
  ChatCircleDots,
} from "@phosphor-icons/react";

interface ConversationSidebarProps {
  conversations: ConversationItem[];
  conversationId: string | null;
  loadingList: boolean;
  sending: boolean;
  editingId: string | null;
  editValue: string;
  collapsed: boolean;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onToggleCollapsed: () => void;
  onNewChat: () => void;
  onLoad: (id: string) => void;
  onDelete: (id: string, e: ReactMouseEvent) => void;
  onStartRename: (c: ConversationItem) => void;
  onEditValue: (v: string) => void;
  onCommitRename: () => void;
  onCancelRename: () => void;
}

/** 会话列表项的时间戳：今天只显示时分，其余显示月/日，更省横向空间 */
function shortTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  return sameDay
    ? d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
    : d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

export function ConversationSidebar({
  conversations,
  conversationId,
  loadingList,
  sending,
  editingId,
  editValue,
  collapsed,
  mobileOpen,
  onCloseMobile,
  onToggleCollapsed,
  onNewChat,
  onLoad,
  onDelete,
  onStartRename,
  onEditValue,
  onCommitRename,
  onCancelRename,
}: ConversationSidebarProps) {
  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseMobile();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mobileOpen, onCloseMobile]);

  // 侧栏是「独立面板」：有自己的表面色、自己的滚动区、自己的头部，
  // 与右侧内容区在视觉上明确分层（浅色白 vs zinc-50，深色 zinc-900 vs zinc-950）。
  const panel = (
    <aside
      className={`flex h-full w-full flex-col border-r border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900 ${
        collapsed ? "md:items-center" : ""
      }`}
    >
      {/* 头部：与右侧内容头部同为 h-12，两条分隔线在视觉上对齐 */}
      <div className="flex h-12 shrink-0 items-center gap-1.5 border-b border-zinc-200 px-3 dark:border-zinc-800">
        {!collapsed && (
          <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-zinc-800 dark:text-zinc-100">
            会话
          </span>
        )}
        <button
          onClick={onNewChat}
          disabled={sending}
          title="新对话"
          className={`press inline-flex shrink-0 items-center gap-1 rounded-full bg-brand-600 text-xs font-medium text-white shadow-sm shadow-brand-600/25 transition hover:bg-brand-700 disabled:opacity-50 ${
            collapsed ? "h-7 w-7 justify-center" : "px-2.5 py-1.5"
          }`}
        >
          <Plus size={14} weight="bold" />
          {!collapsed && "新对话"}
        </button>
        <button
          type="button"
          onClick={onToggleCollapsed}
          title={collapsed ? "展开侧栏" : "收起侧栏"}
          className="hidden h-7 w-7 shrink-0 items-center justify-center rounded-full text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-700 md:flex dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
        >
          {collapsed ? <CaretRight size={14} /> : <CaretLeft size={14} />}
        </button>
        <button
          type="button"
          onClick={onCloseMobile}
          className="flex h-7 w-7 items-center justify-center rounded-full text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-700 md:hidden dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
          aria-label="关闭会话列表"
        >
          <X size={16} />
        </button>
      </div>

      {/* 列表：独立滚动区 */}
      <div className="flex-1 overflow-y-auto overscroll-contain px-2 py-2">
        {loadingList && !collapsed && (
          <div className="px-2 py-6 text-center text-xs text-zinc-400">加载中…</div>
        )}

        {!loadingList && conversations.length === 0 && !collapsed && (
          <div className="px-3 py-10 text-center text-xs text-zinc-400">
            <div className="mb-2.5 flex justify-center text-zinc-300 dark:text-zinc-600">
              <ChatCircleDots size={26} />
            </div>
            暂无历史会话
          </div>
        )}

        {conversations.map((c) => {
          const active = conversationId === c.conversation_id;
          const editing = editingId === c.conversation_id;
          const initial = (c.title || "新").trim().slice(0, 1);

          // 折叠态：只留首字方块
          if (collapsed) {
            return (
              <button
                key={c.conversation_id}
                title={c.title}
                onClick={() => onLoad(c.conversation_id)}
                className={`mb-1 flex h-9 w-9 items-center justify-center rounded-xl text-[13px] font-medium transition ${
                  active
                    ? "bg-brand-600 text-white"
                    : "text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800"
                }`}
              >
                {initial}
              </button>
            );
          }

          return (
            <div
              key={c.conversation_id}
              className={`group relative mb-0.5 flex items-center gap-1.5 rounded-xl pr-1.5 transition ${
                active
                  ? "bg-brand-50 dark:bg-brand-500/10"
                  : "hover:bg-zinc-100 dark:hover:bg-zinc-800/70"
              }`}
            >
              {/* 激活态左侧强调条 */}
              <span
                className={`absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-full bg-brand-600 transition-opacity dark:bg-brand-400 ${
                  active ? "opacity-100" : "opacity-0"
                }`}
              />

              {editing ? (
                <input
                  autoFocus
                  value={editValue}
                  onChange={(e) => onEditValue(e.target.value)}
                  onBlur={onCommitRename}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") onCommitRename();
                    else if (e.key === "Escape") onCancelRename();
                  }}
                  className="my-1 ml-2 flex-1 rounded-lg border border-brand-300 bg-white px-2 py-1 text-sm text-zinc-800 outline-none focus:border-brand-500 dark:border-brand-700 dark:bg-zinc-900 dark:text-zinc-100"
                />
              ) : (
                <>
                  <button
                    onClick={() => onLoad(c.conversation_id)}
                    className="min-w-0 flex-1 py-1.5 pl-2.5 text-left"
                  >
                    <div
                      className={`truncate text-[13px] font-medium ${
                        active
                          ? "text-brand-700 dark:text-brand-200"
                          : "text-zinc-700 dark:text-zinc-200"
                      }`}
                    >
                      {c.title || "新对话"}
                    </div>
                    <div className="truncate text-[11px] text-zinc-400 dark:text-zinc-500">
                      {shortTime(c.last_message_at)}
                    </div>
                  </button>

                  {/* hover 才出现的操作；触屏设备常显，否则手机根本点不到 */}
                  <div className="flex shrink-0 items-center gap-0.5 opacity-100 transition md:opacity-0 md:group-hover:opacity-100">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onStartRename(c);
                      }}
                      title="重命名"
                      className="flex h-6 w-6 items-center justify-center rounded-full text-zinc-400 transition hover:bg-zinc-200 hover:text-brand-600 dark:hover:bg-zinc-700 dark:hover:text-brand-300"
                    >
                      <Pencil size={12} />
                    </button>
                    <button
                      onClick={(e) => onDelete(c.conversation_id, e)}
                      title="删除"
                      className="flex h-6 w-6 items-center justify-center rounded-full text-zinc-400 transition hover:bg-red-50 hover:text-red-500 dark:hover:bg-red-950/40 dark:hover:text-red-400"
                    >
                      <Trash size={12} />
                    </button>
                  </div>
                </>
              )}
            </div>
          );
        })}
      </div>
    </aside>
  );

  return (
    <>
      {/* 移动端遮罩 */}
      {mobileOpen && (
        <button
          type="button"
          aria-label="关闭遮罩"
          className="fixed inset-0 z-30 bg-zinc-950/50 backdrop-blur-sm md:hidden"
          onClick={onCloseMobile}
        />
      )}
      <div
        className={`fixed inset-y-0 left-0 z-40 shrink-0 shadow-2xl shadow-zinc-950/20 transition-transform duration-200 ease-out md:static md:z-auto md:shadow-none ${
          mobileOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0"
        } ${collapsed ? "md:w-[68px]" : "md:w-[268px]"} w-[min(19rem,86vw)]`}
      >
        {panel}
      </div>
    </>
  );
}
