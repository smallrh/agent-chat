// 任务 API（走 Go BFF /api/v1/tasks → Agent /tasks）。

import { getTokens } from "./tokenStore";
import { refreshAuth, ApiError, request } from "./client";
import { readSSEStream } from "./sse";
import type { SSEEvent } from "../types";

export type TaskStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface TaskItem {
  task_id: string;
  conversation_id: string;
  title?: string | null;
  goal?: string | null;
  /** 任务类型：demo（模拟）/ research（真实联网调研） */
  kind?: string;
  status: TaskStatus;
  progress: number;
  /** 当前步骤文案，如「检索资料（2/3）：广州天河酒店」 */
  step?: string | null;
  steps_done?: number;
  steps_total?: number;
  result?: unknown;
  error?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TaskListResponse {
  items: TaskItem[];
}

export interface CreateTaskResponse {
  task_id: string;
  status: string;
}

export interface CreateTaskPayload {
  conversation_id: string;
  title: string;
  goal?: string;
  kind?: string;
}

function authHeaders(): Record<string, string> {
  const t = getTokens();
  return t ? { Authorization: `${t.token_type} ${t.access_token}` } : {};
}

/** 任务列表。带 conversation_id = 会话内；不带 = 任务中心（全部会话）。 */
export function listTasks(params?: {
  conversation_id?: string;
  status?: string;
  kind?: string;
  limit?: number;
}): Promise<TaskListResponse> {
  const qs = new URLSearchParams();
  if (params?.conversation_id) qs.set("conversation_id", params.conversation_id);
  if (params?.status) qs.set("status", params.status);
  if (params?.kind) qs.set("kind", params.kind);
  if (params?.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<TaskListResponse>("GET", `/api/v1/tasks${suffix}`);
}

/** 创建任务并立即后台运行（返回 task_id，不阻塞）。 */
export function createTask(
  payload: CreateTaskPayload
): Promise<CreateTaskResponse> {
  return request<CreateTaskResponse>("POST", "/api/v1/tasks", payload);
}

export function cancelTask(
  taskId: string
): Promise<{ ok: boolean; cancelled: boolean }> {
  return request("POST", `/api/v1/tasks/${taskId}/cancel`);
}

/** 重跑一个已终态任务（failed / cancelled / 重启中断）。 */
export function retryTask(
  taskId: string
): Promise<{ ok: boolean; retried: boolean }> {
  return request("POST", `/api/v1/tasks/${taskId}/retry`);
}

/** 单个任务详情（SSE 事件里只有 task_id，要完整 result 得再拉一次）。 */
export function getTask(taskId: string): Promise<TaskItem> {
  return request<TaskItem>("GET", `/api/v1/tasks/${taskId}`);
}

export function listTaskKinds(): Promise<{ items: string[] }> {
  return request<{ items: string[] }>("GET", "/api/v1/tasks/kinds");
}

/**
 * 订阅某会话的任务事件流（SSE）。
 *
 * 事件：task.created / task.running / task.progress /
 *       task.completed / task.failed / task.cancelled
 *
 * 后端每 30s 发一次 ": ping" 心跳，parseBlock 会跳过它，
 * 所以这里不会产出空事件。
 */
export async function* streamTasks(
  conversationId: string,
  signal?: AbortSignal
): AsyncGenerator<SSEEvent> {
  const doFetch = () =>
    fetch(`/api/v1/tasks/stream?conversation_id=${encodeURIComponent(conversationId)}`, {
      method: "GET",
      headers: { Accept: "text/event-stream", ...authHeaders() },
      signal,
    });

  let res = await doFetch();

  if (res.status === 401) {
    const ok = await refreshAuth();
    if (ok) res = await doFetch();
  }

  if (!res.ok || !res.body) {
    let msg = `任务流连接失败 (${res.status})`;
    try {
      const j = await res.json();
      msg = j?.message ?? msg;
    } catch {
      /* 忽略非 JSON */
    }
    throw new ApiError(res.status, "STREAM_ERROR", msg);
  }

  yield* readSSEStream(res);
}
