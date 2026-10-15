import { request } from "./client";

export interface ConversationItem {
  conversation_id: string;
  title: string;
  status: number;
  last_message_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface MessageItem {
  message_id: string;
  role: string;
  content: string;
  sequence_id: number;
  created_at?: string | null;
  metadata?: {
    tools?: Array<{
      id?: string;
      name?: string;
      result?: string;
      status?: string;
      arguments?: string;
    }>;
    // P2 多模态：随消息提交的附件（刷新后靠它把图片 / 文件卡片渲染回来）
    attachments?: Array<{
      attachment_id: string;
      name: string;
      mime: string;
      size: number;
      is_image: boolean;
    }>;
  } | null;
}

export function listConversations(): Promise<{ items: ConversationItem[] }> {
  return request<{ items: ConversationItem[] }>("GET", "/api/v1/conversations");
}

export function listMessages(
  conversationId: string
): Promise<{ items: MessageItem[] }> {
  return request<{ items: MessageItem[] }>(
    "GET",
    `/api/v1/conversations/${conversationId}/messages`
  );
}

export function renameConversation(
  conversationId: string,
  title: string
): Promise<ConversationItem> {
  return request<ConversationItem>(
    "PATCH",
    `/api/v1/conversations/${conversationId}`,
    { title }
  );
}

export function deleteConversation(conversationId: string): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>(
    "DELETE",
    `/api/v1/conversations/${conversationId}`
  );
}
