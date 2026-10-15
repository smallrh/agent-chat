import type { AttachmentChip, ChatMessage, ToolCallInfo } from "../types";
import { Markdown } from "./Markdown";
import { attachmentUrl, humanSize } from "../api/attachments";
import {
  Check,
  Spinner,
  Wrench,
  Paperclip,
  Copy,
  ChatCircleDots,
} from "@phosphor-icons/react";

const URL_RE = /https?:\/\/[^\s\]）)>\"]+/gi;

function extractUrls(message: ChatMessage): string[] {
  const bag = new Set<string>();
  for (const m of message.content.matchAll(URL_RE))
    bag.add(m[0].replace(/[.,;，。]+$/, ""));
  for (const t of message.tools ?? []) {
    for (const m of (t.result || "").matchAll(URL_RE)) {
      bag.add(m[0].replace(/[.,;，。]+$/, ""));
    }
  }
  return [...bag].slice(0, 8);
}

function ToolCard({ tool }: { tool: ToolCallInfo }) {
  const running = tool.status === "running";
  let args = tool.arguments ?? "";
  try {
    args = JSON.stringify(JSON.parse(args), null, 2);
  } catch {
    // non-JSON, leave as-is
  }
  const longArg = args.length > 200;
  const result = tool.result ?? "";
  const longRes = result.length > 200;

  return (
    <div className="my-1.5 overflow-hidden rounded-xl border border-zinc-200 bg-zinc-50 text-[12px] dark:border-zinc-800 dark:bg-zinc-900/50">
      <div className="flex items-center gap-2 px-3 py-2">
        <span
          className={`flex h-5 w-5 items-center justify-center rounded-full ${
            running
              ? "bg-amber-100 text-amber-600 dark:bg-amber-900/40 dark:text-amber-400"
              : "bg-brand-100 text-brand-600 dark:bg-brand-900/40 dark:text-brand-400"
          }`}
        >
          {running ? (
            <Spinner className="animate-spin" size={12} />
          ) : (
            <Check size={12} weight="bold" />
          )}
        </span>
        <span className="flex items-center gap-1 font-medium text-zinc-700 dark:text-zinc-300">
          <Wrench size={12} />
          {tool.name}
        </span>
        <span
          className={`ml-auto text-[11px] ${
            running
              ? "text-amber-600 dark:text-amber-400"
              : "text-brand-600 dark:text-brand-400"
          }`}
        >
          {running ? "调用中" : "完成"}
        </span>
      </div>

      {args && (
        <details
          open={!longArg}
          className="border-t border-zinc-200 px-3 py-2 dark:border-zinc-800"
        >
          <summary className="cursor-pointer select-none text-[11px] text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300">
            参数
          </summary>
          <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] text-zinc-600 dark:text-zinc-400">
            {args}
          </pre>
        </details>
      )}

      {tool.result != null && (
        <details
          open={!longRes}
          className="border-t border-zinc-200 px-3 py-2 dark:border-zinc-800"
        >
          <summary className="cursor-pointer select-none text-[11px] text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300">
            结果
          </summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] text-zinc-600 dark:text-zinc-400">
            {result}
          </pre>
        </details>
      )}
    </div>
  );
}

function AttachmentList({ items, dark }: { items: AttachmentChip[]; dark: boolean }) {
  return (
    <div className="mb-1.5 flex flex-wrap gap-1.5">
      {items.map((a) =>
        a.is_image ? (
          <a
            key={a.attachment_id}
            href={attachmentUrl(a.attachment_id, false)}
            target="_blank"
            rel="noreferrer"
            title={a.name}
            className={`block overflow-hidden rounded-xl border transition hover:opacity-90 ${
              dark ? "border-white/20" : "border-zinc-200"
            }`}
          >
            <img
              src={attachmentUrl(a.attachment_id)}
              alt={a.name}
              className="h-20 w-20 object-cover"
              loading="lazy"
            />
          </a>
        ) : (
          <a
            key={a.attachment_id}
            href={attachmentUrl(a.attachment_id, false)}
            target="_blank"
            rel="noreferrer"
            className={`flex max-w-[220px] items-center gap-1.5 rounded-xl px-2.5 py-1.5 text-[11px] transition ${
              dark
                ? "bg-white/10 text-white hover:bg-white/20"
                : "bg-zinc-100 text-zinc-700 hover:bg-zinc-200"
            }`}
          >
            <Paperclip size={12} />
            <span className="truncate">{a.name}</span>
            <span
              className={`shrink-0 ${dark ? "text-white/60" : "text-zinc-400"}`}
            >
              {humanSize(a.size)}
            </span>
          </a>
        )
      )}
    </div>
  );
}

export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
    } catch {
      /* ignore */
    }
  };

  return (
    <div
      className={`group flex animate-fade-in ${
        isUser ? "justify-end" : "justify-start"
      }`}
    >
      <div
        className={`relative max-w-[92%] overflow-hidden shadow-sm sm:max-w-[80%] ${
          isUser
            ? "rounded-2xl rounded-br-md bg-brand-600 text-white dark:bg-brand-500"
            : "rounded-2xl rounded-bl-md border border-zinc-200 bg-white text-zinc-900 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-100"
        } px-4 py-2.5 text-[14px] leading-relaxed`}
      >
        {message.attachments && message.attachments.length > 0 && (
          <AttachmentList items={message.attachments} dark={isUser} />
        )}

        {message.tools && message.tools.length > 0 && (
          <div className="mb-2 space-y-1">
            {message.tools.map((t) => (
              <ToolCard key={t.id} tool={t} />
            ))}
          </div>
        )}

        {message.content ? (
          isUser ? (
            <span className="whitespace-pre-wrap break-words">
              {message.content}
            </span>
          ) : (
            <>
              <div className="prose-rich">
                <Markdown content={message.content} />
              </div>
              {extractUrls(message).length > 0 && (
                <div className="mt-2 space-y-1 border-t border-zinc-200 pt-2 dark:border-zinc-800">
                  {extractUrls(message).map((u) => (
                    <a
                      key={u}
                      href={u}
                      target="_blank"
                      rel="noreferrer"
                      className="block truncate text-[11px] text-brand-600 hover:underline dark:text-brand-400"
                    >
                      {u}
                    </a>
                  ))}
                </div>
              )}
            </>
          )
        ) : message.streaming ? (
          <div className="flex items-center gap-1.5 text-zinc-400 dark:text-zinc-500">
            <ChatCircleDots size={14} className="animate-pulse" />
            <span>思考中</span>
          </div>
        ) : null}

        {message.stopped && !message.streaming && (
          <div className="mt-1 text-[11px] text-zinc-400 dark:text-zinc-500">
            已停止生成
          </div>
        )}

        {message.streaming && message.content && (
          <span className="ml-0.5 inline-block h-4 w-[3px] -mb-0.5 animate-blink bg-current opacity-60 align-middle" />
        )}

        {!isUser && (
          <button
            onClick={copy}
            title="复制"
            className="absolute -bottom-2.5 -right-2.5 hidden h-6 w-6 items-center justify-center rounded-full border border-zinc-200 bg-white text-zinc-400 shadow-sm transition hover:border-zinc-300 hover:text-brand-600 group-hover:flex dark:border-zinc-700 dark:bg-zinc-800 dark:hover:text-brand-400"
          >
            <Copy size={12} />
          </button>
        )}
      </div>
    </div>
  );
}
