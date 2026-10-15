import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";
import { useSearchParams } from "react-router-dom";
import {
  List,
  SquaresFour,
  Sparkle,
  Compass,
  CloudSun,
  Brain,
} from "@phosphor-icons/react";
import { streamChat, cancelRun } from "../api/chat";
import {
  deleteConversation,
  listConversations,
  listMessages,
  renameConversation,
  type ConversationItem,
} from "../api/conversations";
import type { AttachmentChip, ChatMessage, ChatRequest, ToolCallInfo } from "../types";
import { MessageBubble } from "../components/MessageBubble";
import { ChatInput } from "../components/ChatInput";
import { ConversationSidebar } from "../components/ConversationSidebar";
import { TaskPanel } from "../components/TaskPanel";
import type { TaskItem } from "../api/tasks";

function safeParse(s: string): Record<string, any> | null {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

/** 把任务结果渲染成一条 assistant 消息，插入到对话流里。 */
function taskResultMessage(task: TaskItem): ChatMessage {
  const result = (task.result ?? {}) as Record<string, any>;
  const report = typeof result.report === "string" ? result.report : "";
  const summary = typeof result.summary === "string" ? result.summary : "";
  const parts = [`【任务完成】${task.title || "后台任务"}`];
  if (report) {
    parts.push(report);
  } else if (summary) {
    parts.push(summary);
  }
  const sources = Array.isArray(result.sources) ? result.sources : [];
  if (sources.length > 0) {
    parts.push(
      "参考来源：\n" +
        sources
          .slice(0, 8)
          .map((s: any) => `- ${s.title || s.url}: ${s.url}`)
          .join("\n")
    );
  }
  return {
    id: `task-${task.task_id}`,
    role: "assistant",
    content: parts.join("\n\n"),
  };
}

/** 空态快捷入口：点一下直接作为消息发出去，省去用户从零想 prompt */
const SUGGESTIONS: { icon: typeof Sparkle; label: string; prompt: string }[] = [
  {
    icon: Sparkle,
    label: "介绍一下你的能力",
    prompt: "介绍一下你都能帮我做什么，用简短的列表说明。",
  },
  {
    icon: Compass,
    label: "调研一个话题",
    prompt: "帮我调研一下 2026 年 AI Agent 的主流技术栈，给一份带对比的简报。",
  },
  {
    icon: CloudSun,
    label: "查一下天气",
    prompt: "帮我查一下北京今天的天气，并给出穿衣建议。",
  },
  {
    icon: Brain,
    label: "记住我的偏好",
    prompt: "请记住：回答尽量简洁，先给结论，再补充细节。",
  },
];

export function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<ConversationItem[]>([]);
  const [sending, setSending] = useState(false);
  const [loadingList, setLoadingList] = useState(true);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [attachments, setAttachments] = useState<AttachmentChip[]>([]);
  const [panelOpen, setPanelOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("sidebar-collapsed") === "1";
    } catch {
      return false;
    }
  });
  const [mobileOpen, setMobileOpen] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const runIdRef = useRef<string | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();

  const refreshList = useCallback(async () => {
    try {
      const res = await listConversations();
      setConversations(res.items ?? []);
    } catch {
      // 列表失败不阻断聊天
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  // 从任务中心「前往会话」跳过来时带上 ?conversation=<id>，这里自动加载它
  const targetConv = searchParams.get("conversation");
  useEffect(() => {
    if (!targetConv || targetConv === conversationId) return;
    // 用掉一次就清掉，避免后续刷新又把用户拽回这个会话
    setSearchParams({}, { replace: true });
    void loadConversationById(targetConv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetConv]);

  useEffect(() => {
    try {
      localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [collapsed]);

  useEffect(() => {
    const mq = window.matchMedia("(min-width: 768px)");
    const onChange = () => {
      if (mq.matches) setMobileOpen(false);
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const loadConversationById = async (id: string) => {
    if (sending || editingId) return;
    setConversationId(id);
    setMobileOpen(false);
    try {
      const res = await listMessages(id);
      setMessages(
        (res.items ?? []).map((m) => ({
          id: m.message_id,
          role: m.role as ChatMessage["role"],
          content: m.content,
          // P0：工具轨迹（刷新后工具卡片不丢）
          tools: (m.metadata?.tools ?? []).map((t, i) => ({
            id: t.id || `${m.message_id}-tool-${i}`,
            name: t.name || "tool",
            result: t.result,
            arguments: t.arguments,
            status: (t.status === "running" ? "running" : "done") as "running" | "done",
          })),
          // P2：附件（刷新后图片/文件卡片不丢）
          attachments: (m.metadata?.attachments ?? []) as AttachmentChip[],
        }))
      );
    } catch {
      setMessages([]);
    }
  };

  const loadConversation = async (id: string) => {
    await loadConversationById(id);
  };

  const handleSend = async (text: string, attachmentIds: string[]) => {
    if ((!text.trim() && attachmentIds.length === 0) || sending) return;
    const trimmed = text.trim();
    const assistantId = crypto.randomUUID();

    setMessages((prev) => [
      ...prev,
      {
        id: crypto.randomUUID(),
        role: "user",
        content: trimmed,
        attachments: attachments.length > 0 ? attachments : undefined,
      },
      { id: assistantId, role: "assistant", content: "", streaming: true, tools: [] },
    ]);
    setAttachments([]);
    setSending(true);

    const req: ChatRequest = {
      message: trimmed,
      conversation_id: conversationId,
      attachment_ids: attachmentIds,
    };

    const ac = new AbortController();
    abortRef.current = ac;
    runIdRef.current = null;

    try {
      for await (const evt of streamChat(req, ac.signal)) {
        const d = safeParse(evt.data);
        switch (evt.event) {
          case "agent.start":
            if (d?.conversation_id) {
              runIdRef.current = d.run_id ?? null;
              setConversationId(d.conversation_id);
              // 新会话：立即在侧栏占位，避免直到 done 才高亮
              setConversations((prev) =>
                prev.some((c) => c.conversation_id === d.conversation_id)
                  ? prev
                  : [
                      {
                        conversation_id: d.conversation_id,
                        title: "新对话",
                        status: 1,
                        last_message_at: null,
                        created_at: null,
                        updated_at: null,
                      },
                      ...prev,
                    ]
              );
            }
            break;
          case "message.end":
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? {
                      ...m,
                      streaming: false,
                      stopped: d?.finish_reason === "cancelled",
                    }
                  : m
              )
            );
            break;
          case "message.delta":
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? { ...m, content: m.content + (d?.content ?? "") }
                  : m
              )
            );
            break;
          case "tool.start":
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantId) return m;
                const tools: ToolCallInfo[] = [...(m.tools ?? [])];
                tools.push({
                  id: d?.tool_call_id ?? crypto.randomUUID(),
                  name: d?.tool_name ?? "tool",
                  arguments:
                    typeof d?.arguments === "string"
                      ? d.arguments
                      : JSON.stringify(d?.arguments ?? ""),
                  status: "running",
                });
                return { ...m, tools };
              })
            );
            break;
          case "tool.result":
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantId) return m;
                const tools = (m.tools ?? []).map((t) =>
                  t.id === d?.tool_call_id
                    ? { ...t, result: String(d?.result ?? ""), status: "done" as const }
                    : t
                );
                return { ...m, tools };
              })
            );
            break;
          case "done":
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId ? { ...m, streaming: false } : m
              )
            );
            void refreshList();
            break;
          case "error":
            setMessages((prev) =>
              prev.map((m) =>
                m.id === assistantId
                  ? {
                      ...m,
                      streaming: false,
                      error: true,
                      content: m.content || (d?.error ?? "出错了"),
                    }
                  : m
              )
            );
            break;
          default:
            break;
        }
      }
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === "AbortError";
      const msg = aborted
        ? "已停止生成"
        : err instanceof Error
          ? err.message
          : "请求失败";
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                streaming: false,
                error: !aborted,
                content: m.content || msg,
              }
            : m
        )
      );
    } finally {
      abortRef.current = null;
      setSending(false);
      // 停止/中断时 done 不会到达，这里兜底刷新侧栏，保证历史与标题一致
      void refreshList();
    }
  };

  const handleStop = () => {
    // 1) 通知服务端取消对应 run（真正中断生成）
    const rid = runIdRef.current;
    if (rid) void cancelRun(rid);
    // 2) 同时中断本地读取（断连），让 UI 立即停下
    abortRef.current?.abort();
  };

  const handleNewChat = () => {
    if (sending) return;
    setMessages([]);
    setConversationId(null);
    setAttachments([]);
    setMobileOpen(false);
  };

  // 后台任务跑完：把结果作为一条 assistant 消息插进当前会话
  const handleTaskCompleted = useCallback(
    (task: TaskItem) => {
      if (!task || task.status !== "succeeded") return;
      setMessages((prev) => {
        // 幂等：同一任务重复回调（重跑后再次完成）时替换而不是堆两条
        const id = `task-${task.task_id}`;
        const rest = prev.filter((m) => m.id !== id);
        return [...rest, taskResultMessage(task)];
      });
    },
    []
  );

  const handleDelete = async (id: string, e: ReactMouseEvent) => {
    e.stopPropagation();
    if (sending) return;
    try {
      await deleteConversation(id);
    } catch {
      // 忽略删除失败
    }
    if (conversationId === id) {
      setConversationId(null);
      setMessages([]);
    }
    void refreshList();
  };

  // ---- 重命名 ----
  const startRename = (c: ConversationItem) => {
    if (sending) return;
    setEditingId(c.conversation_id);
    setEditValue(c.title);
  };

  const commitRename = async () => {
    const id = editingId;
    const title = editValue.trim();
    setEditingId(null);
    if (!id || !title) return;
    try {
      const updated = await renameConversation(id, title);
      setConversations((prev) =>
        prev.map((c) =>
          c.conversation_id === id ? { ...c, title: updated.title } : c
        )
      );
    } catch {
      // 忽略重命名失败
    }
  };

  const activeTitle =
    (conversationId &&
      conversations.find((c) => c.conversation_id === conversationId)?.title) ||
    "新对话";

  const isEmpty = messages.length === 0;

  return (
    <div className="flex h-full min-h-0 w-full">
      <ConversationSidebar
        conversations={conversations}
        conversationId={conversationId}
        loadingList={loadingList}
        sending={sending}
        editingId={editingId}
        editValue={editValue}
        collapsed={collapsed}
        mobileOpen={mobileOpen}
        onCloseMobile={() => setMobileOpen(false)}
        onToggleCollapsed={() => setCollapsed((v) => !v)}
        onNewChat={handleNewChat}
        onLoad={(id) => void loadConversation(id)}
        onDelete={(id, e) => void handleDelete(id, e)}
        onStartRename={startRename}
        onEditValue={setEditValue}
        onCommitRename={() => void commitRename()}
        onCancelRename={() => setEditingId(null)}
      />

      {/* 内容面板：与侧栏是两块独立表面（浅色 zinc-50 / 深色 zinc-950） */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col bg-zinc-50 dark:bg-zinc-950">
        {/* 顶部条：h-12 与侧栏头部对齐，两条分隔线连成一条 */}
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-zinc-200 bg-zinc-50/85 px-2.5 backdrop-blur-md dark:border-zinc-800 dark:bg-zinc-950/85 sm:px-4">
          <button
            type="button"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-zinc-500 transition hover:bg-zinc-200/70 hover:text-zinc-900 md:hidden dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            onClick={() => setMobileOpen(true)}
            aria-label="打开会话列表"
          >
            <List size={18} />
          </button>

          <div className="min-w-0 flex-1 truncate text-[13px] font-semibold text-zinc-800 dark:text-zinc-100">
            {activeTitle}
          </div>

          <button
            type="button"
            onClick={() => setPanelOpen((v) => !v)}
            aria-pressed={panelOpen}
            className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12px] font-medium transition ${
              panelOpen
                ? "border-brand-200 bg-brand-50 text-brand-700 dark:border-brand-800 dark:bg-brand-500/10 dark:text-brand-300"
                : "border-transparent text-zinc-500 hover:bg-zinc-200/70 hover:text-zinc-900 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            }`}
          >
            <SquaresFour size={14} />
            <span className="hidden sm:inline">后台任务</span>
          </button>
        </header>

        <div className="flex min-h-0 flex-1">
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            {/* 消息区：空态垂直居中；有消息时正常流式排布 */}
            <div className="flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
              <div
                className={`mx-auto flex w-full max-w-3xl flex-1 flex-col px-4 py-6 sm:px-6 ${
                  isEmpty ? "" : "gap-4"
                }`}
              >
                {isEmpty && (
                  <div className="m-auto flex w-full flex-col items-center text-center">
                    <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-brand-600 text-white shadow-lg shadow-brand-600/25">
                      <Sparkle size={26} weight="fill" />
                    </div>
                    <h2 className="text-lg font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
                      开始对话
                    </h2>
                    <p className="mt-1.5 max-w-md text-[13px] leading-relaxed text-zinc-500 dark:text-zinc-400">
                      支持多会话、流式回复、工具调用、附件与后台任务。选一个开始，或直接在下方输入。
                    </p>

                    <div className="mt-7 grid w-full grid-cols-1 gap-2 sm:grid-cols-2">
                      {SUGGESTIONS.map((s) => {
                        const Icon = s.icon;
                        return (
                          <button
                            key={s.label}
                            type="button"
                            onClick={() => void handleSend(s.prompt, [])}
                            className="group flex items-center gap-2.5 rounded-2xl border border-zinc-200 bg-white px-3.5 py-3 text-left transition hover:border-brand-300 hover:shadow-sm dark:border-zinc-800 dark:bg-zinc-900/60 dark:hover:border-brand-700"
                          >
                            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-zinc-100 text-zinc-500 transition group-hover:bg-brand-50 group-hover:text-brand-600 dark:bg-zinc-800 dark:text-zinc-400 dark:group-hover:bg-brand-500/10 dark:group-hover:text-brand-300">
                              <Icon size={16} />
                            </span>
                            <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-zinc-700 dark:text-zinc-200">
                              {s.label}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}

                {messages.map((m) => (
                  <MessageBubble key={m.id} message={m} />
                ))}
                <div ref={endRef} />
              </div>
            </div>

            {/* 输入区：固定在内容面板底部 */}
            <div className="shrink-0 border-t border-zinc-200 bg-zinc-50/85 px-3 py-3 backdrop-blur-sm dark:border-zinc-800 dark:bg-zinc-950/85 sm:px-6 sm:py-4">
              <div className="mx-auto w-full max-w-3xl">
                <ChatInput
                  onSend={(text, ids) => void handleSend(text, ids)}
                  onStop={handleStop}
                  disabled={sending}
                  sending={sending}
                  attachments={attachments}
                  onAttach={(chip) =>
                    setAttachments((prev) =>
                      prev.some((a) => a.attachment_id === chip.attachment_id)
                        ? prev
                        : [...prev, chip]
                    )
                  }
                  onDetach={(id) =>
                    setAttachments((prev) => prev.filter((a) => a.attachment_id !== id))
                  }
                />
              </div>
            </div>
          </div>

          {/* 任务面板：lg 以上是常驻右栏；小屏改成浮层抽屉，否则点了按钮什么都不会出现 */}
          {panelOpen && (
            <>
              <button
                type="button"
                aria-label="关闭任务面板"
                onClick={() => setPanelOpen(false)}
                className="fixed inset-0 z-30 bg-zinc-950/50 backdrop-blur-sm lg:hidden"
              />
              <aside className="fixed inset-y-0 right-0 z-40 w-[min(22rem,90vw)] border-l border-zinc-200 bg-white shadow-2xl shadow-zinc-950/20 dark:border-zinc-800 dark:bg-zinc-900 lg:static lg:z-auto lg:w-72 lg:shrink-0 lg:shadow-none xl:w-80">
                <TaskPanel
                  conversationId={conversationId}
                  onCompleted={handleTaskCompleted}
                  onClose={() => setPanelOpen(false)}
                />
              </aside>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
