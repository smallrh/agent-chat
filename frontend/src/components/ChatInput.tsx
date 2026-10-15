import { useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";
import { uploadAttachment, humanSize } from "../api/attachments";
import type { AttachmentChip } from "../types";
import {
  Paperclip,
  PaperPlaneRight,
  StopCircle,
  X,
} from "@phosphor-icons/react";

interface ChatInputProps {
  onSend: (text: string, attachmentIds: string[]) => void;
  onStop?: () => void;
  disabled?: boolean;
  sending?: boolean;
  attachments: AttachmentChip[];
  onAttach: (chip: AttachmentChip) => void;
  onDetach: (attachmentId: string) => void;
}

export function ChatInput({
  onSend,
  onStop,
  disabled,
  sending,
  attachments,
  onAttach,
  onDetach,
}: ChatInputProps) {
  const [text, setText] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const submit = () => {
    const t = text.trim();
    if ((!t && attachments.length === 0) || disabled) return;
    onSend(t, attachments.map((a) => a.attachment_id));
    setText("");
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const handlePick = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (files.length === 0) return;

    setUploading(true);
    setUploadError(null);
    for (const f of files) {
      try {
        const meta = await uploadAttachment(f);
        onAttach({
          attachment_id: meta.attachment_id,
          name: meta.name,
          mime: meta.mime,
          size: meta.size,
          is_image: meta.is_image,
        });
      } catch (err) {
        setUploadError(err instanceof Error ? err.message : "上传失败");
      }
    }
    setUploading(false);
  };

  const canSend = !disabled && (!!text.trim() || attachments.length > 0);

  return (
    <div className="rounded-2xl border border-zinc-200 bg-white p-2 shadow-sm transition focus-within:border-brand-400 focus-within:ring-2 focus-within:ring-brand-400/10 dark:border-zinc-800 dark:bg-zinc-900 dark:focus-within:ring-brand-400/20">
      {attachments.length > 0 && (
        <div className="mb-1.5 flex flex-wrap gap-1.5 px-1">
          {attachments.map((a) => (
            <span
              key={a.attachment_id}
              className="flex max-w-[200px] items-center gap-1 rounded-xl bg-zinc-100 px-2 py-1 text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
            >
              <Paperclip size={12} />
              <span className="truncate">{a.name}</span>
              <span className="shrink-0 text-zinc-400">{humanSize(a.size)}</span>
              <button
                type="button"
                onClick={() => onDetach(a.attachment_id)}
                className="shrink-0 rounded-full p-0.5 text-zinc-400 transition hover:bg-zinc-300/50 hover:text-red-500 dark:hover:bg-zinc-700 dark:hover:text-red-400"
                aria-label={`移除 ${a.name}`}
              >
                <X size={10} />
              </button>
            </span>
          ))}
        </div>
      )}

      {uploadError && (
        <div className="mx-1 mb-1.5 rounded-xl bg-red-50 px-2 py-1 text-[11px] text-red-600 dark:bg-red-950/40 dark:text-red-300">
          {uploadError}
        </div>
      )}

      <div className="flex items-end gap-2 px-1">
        <input
          ref={fileRef}
          type="file"
          multiple
          hidden
          onChange={(e) => void handlePick(e)}
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          disabled={disabled || uploading}
          title="上传图片 / 文档"
          className="shrink-0 rounded-xl p-2 text-zinc-500 transition hover:bg-zinc-100 hover:text-zinc-900 disabled:opacity-40 dark:text-zinc-500 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
        >
          {uploading ? (
            <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-zinc-300 border-t-transparent dark:border-zinc-600" />
          ) : (
            <Paperclip size={18} />
          )}
        </button>

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
          disabled={disabled}
          placeholder={disabled ? "AI 正在回复" : "输入消息，Enter 发送"}
          className="max-h-32 min-w-0 flex-1 resize-none bg-transparent py-2 text-[14px] text-zinc-900 outline-none placeholder:text-zinc-400 dark:text-zinc-100 sm:max-h-40"
        />

        {sending ? (
          <button
            type="button"
            onClick={onStop}
            title="停止生成"
            className="press shrink-0 rounded-full border border-zinc-300 bg-white p-2 text-zinc-600 transition hover:border-red-300 hover:bg-red-50 hover:text-red-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:border-red-800 dark:hover:bg-red-950/40 dark:hover:text-red-400"
          >
            <StopCircle size={18} weight="fill" />
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={!canSend}
            title="发送"
            className="press shrink-0 rounded-full bg-brand-600 p-2 text-white shadow-sm shadow-brand-600/25 transition hover:bg-brand-700 disabled:opacity-40"
          >
            <PaperPlaneRight size={18} weight="fill" />
          </button>
        )}
      </div>
    </div>
  );
}
