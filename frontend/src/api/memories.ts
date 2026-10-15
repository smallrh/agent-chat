// 记忆 API（走 Go BFF /api/v1/memories → Agent /memories）。

import { request } from "./client";

export interface MemoryItem {
  memory_id: string;
  conversation_id?: string | null;
  memory_type: string;
  slot?: string | null;
  content: string;
  importance: number;
  confidence?: number | null;
  sync_status: string;
  created_at: string;
  updated_at: string;
  is_active: boolean;
}

export interface MemoryListResponse {
  items: MemoryItem[];
  total?: number | null;
}

export interface MemorySourceMessage {
  message_id: string;
  role: string;
  content: string;
  sequence_id?: number | null;
  created_at?: string | null;
}

export interface MemorySourceResponse {
  memory_id: string;
  content: string;
  memory_type?: string | null;
  slot?: string | null;
  source_conversation_id?: string | null;
  extract_model?: string | null;
  created_at?: string | null;
  messages: MemorySourceMessage[];
}

export interface MemoryDeleteResponse {
  pg: boolean;
  qdrant: boolean;
  redis: boolean;
}

export function listMemories(params?: {
  memory_type?: string;
  limit?: number;
  offset?: number;
}): Promise<MemoryListResponse> {
  const qs = new URLSearchParams();
  if (params?.memory_type) qs.set("memory_type", params.memory_type);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<MemoryListResponse>("GET", `/api/v1/memories${suffix}`);
}

export function deleteMemory(memoryId: string): Promise<MemoryDeleteResponse> {
  return request<MemoryDeleteResponse>("DELETE", `/api/v1/memories/${memoryId}`);
}

/** 记忆溯源：这条记忆是从哪几句话里抽出来的。 */
export function getMemorySource(memoryId: string): Promise<MemorySourceResponse> {
  return request<MemorySourceResponse>("GET", `/api/v1/memories/${memoryId}/source`);
}
