// 对话 API 封装（经 Go 代理 /agent/* → Python Agent）。

import { getTokens } from "./tokenStore";
import { refreshAuth, ApiError, request } from "./client";
import { readSSEStream } from "./sse";
import type { ChatRequest, ChatResponse, SSEEvent } from "../types";

/** 非流式对话 */
export function chat(req: ChatRequest): Promise<ChatResponse> {
  return request<ChatResponse>("POST", "/agent/chat", req);
}

/**
 * 停止一个正在进行的流式生成。
 * 通过 run_id 通知服务端取消对应 run；服务端在生成过程中检查到取消标志会提前结束。
 * 失败（run 已结束 / 网络错误）不阻断前端本地的 abort。
 */
export async function cancelRun(runId: string): Promise<void> {
  try {
    await request("POST", "/agent/chat/cancel", { run_id: runId });
  } catch {
    // 取消失败不阻断
  }
}

/**
 * 流式对话（SSE）。
 * 返回异步生成器，逐个产出 SSE 事件（event + data）。
 * data 是后端 sse() 序列化的 JSON 字符串，由调用方按 event 类型解析。
 */
export async function* streamChat(
  req: ChatRequest,
  signal?: AbortSignal
): AsyncGenerator<SSEEvent> {
  const buildHeaders = () => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    const t = getTokens();
    if (t) headers["Authorization"] = `${t.token_type} ${t.access_token}`;
    return headers;
  };

  const doFetch = () =>
    fetch("/agent/chat/stream", {
      method: "POST",
      headers: buildHeaders(),
      body: JSON.stringify(req),
      signal,
    });

  let res = await doFetch();

  // 流式 401 时尝试刷新并重试一次
  if (res.status === 401) {
    const ok = await refreshAuth();
    if (ok) {
      res = await doFetch();
    }
  }

  if (!res.ok || !res.body) {
    let msg = `流式请求失败 (${res.status})`;
    try {
      const j = await res.json();
      msg = j?.message ?? msg;
    } catch {
      // 忽略非 JSON
    }
    throw new ApiError(res.status, "STREAM_ERROR", msg);
  }

  // SSE 解析逻辑抽到 api/sse.ts（任务流 /api/v1/tasks/stream 复用同一份）
  yield* readSSEStream(res);
}
