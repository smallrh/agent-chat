// 与后端 API 对齐的共享类型定义。

export interface StoredUser {
  user_uuid: string;
  username: string;
  email: string;
}

export interface AuthUserPublic {
  user_uuid: string;
  username: string;
  email: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  refresh_token: string;
  user: AuthUserPublic;
}

export interface RegisterResponse {
  user: AuthUserPublic;
}

export interface RefreshResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  refresh_token: string;
}

export interface ChatRequest {
  message: string;
  conversation_id?: string | null;
  /** P2 多模态：随消息提交的附件 id（先 POST /api/v1/attachments 上传） */
  attachment_ids?: string[];
}

export interface ChatResponse {
  content: string;
  conversation_id: string;
}

// SSE 事件（来自 /agent/chat/stream）
export interface SSEEvent {
  event: string;
  data: string;
}

// 前端对话消息模型
export type ChatRole = "user" | "assistant" | "system";

export interface ToolCallInfo {
  id: string;
  name: string;
  arguments?: string;
  result?: string;
  status: "running" | "done";
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  streaming?: boolean;
  error?: boolean;
  stopped?: boolean;
  tools?: ToolCallInfo[];
  /** P2 多模态：本条消息携带的附件（刷新后从 message.metadata.attachments 回显） */
  attachments?: AttachmentChip[];
}

// P2 多模态：消息里的附件卡片数据（精简元数据，不含正文）
export interface AttachmentChip {
  attachment_id: string;
  name: string;
  mime: string;
  size: number;
  is_image: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  messages: ChatMessage[];
}
