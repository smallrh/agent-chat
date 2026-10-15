// 附件 API（走 Go BFF /api/v1/attachments → Agent /attachments）。

import { getTokens } from "./tokenStore";

export interface AttachmentMeta {
  attachment_id: string;
  name: string;
  mime: string;
  size: number;
  is_image: boolean;
  /** 抽取出的文本字符数（图片为 0） */
  extracted_chars: number;
  /** 文本预览（前 300 字），用于让用户确认传对了文件 */
  preview: string;
}

export interface AttachmentChip {
  attachment_id: string;
  name: string;
  mime: string;
  size: number;
  is_image: boolean;
}

/**
 * 上传附件（multipart/form-data）。
 *
 * 注意：这里**不能**用统一的 request()（它强制 Content-Type: application/json），
 * 必须让浏览器自己带 multipart boundary，否则后端解析不出文件。
 */
export async function uploadAttachment(
  file: File,
  conversationId?: string | null
): Promise<AttachmentMeta> {
  const form = new FormData();
  form.append("file", file);
  if (conversationId) form.append("conversation_id", conversationId);

  const t = getTokens();
  const headers: Record<string, string> = {};
  if (t) headers["Authorization"] = `${t.token_type} ${t.access_token}`;

  const res = await fetch("/api/v1/attachments", {
    method: "POST",
    headers,
    body: form,
  });

  const text = await res.text();
  let json: any = null;
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {
      /* 非 JSON */
    }
  }
  if (!res.ok) {
    throw new Error(json?.message ?? `上传失败 (${res.status})`);
  }
  return (json?.data ?? null) as AttachmentMeta;
}

/** 附件访问地址。inline=1 让浏览器直接预览（图片）而不是下载。 */
export function attachmentUrl(id: string, inline = true): string {
  return `/api/v1/attachments/${id}${inline ? "?inline=1" : ""}`;
}

/** 人类可读的文件大小 */
export function humanSize(n: number): string {
  if (!n) return "0B";
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}KB`;
  return `${(n / 1024 / 1024).toFixed(1)}MB`;
}
