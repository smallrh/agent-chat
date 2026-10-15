// 统一 API 客户端：
// - 统一响应解析（code/message/data/request_id）
// - 自动注入 Authorization（Bearer）
// - access_token 过期时自动用 refresh_token 刷新一次并重试
// - 刷新失败 / 刷新后仍 401 → 清 token + 派发 auth:unauthorized 事件（AuthContext 监听后跳登录）

import {
  getTokens,
  setTokens,
  clearTokens,
  getRefreshToken,
} from "./tokenStore";

export class ApiError extends Error {
  status: number;
  code: string;
  requestId?: string;
  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

// 这些端点本身用于登录/注册/刷新，不应触发自动刷新逻辑（避免死循环）
const NO_REFRESH_PATHS = new Set<string>([
  "/api/v1/auth/refresh",
  "/api/v1/auth/login",
  "/api/v1/auth/register",
]);

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

/** 主动尝试刷新 token；返回是否成功。供 client 内部与流式接口复用。 */
export async function refreshAuth(): Promise<boolean> {
  const rt = getRefreshToken();
  if (!rt) {
    clearTokens();
    return false;
  }
  try {
    const res = await fetch("/api/v1/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!res.ok) {
      clearTokens();
      window.dispatchEvent(new Event("auth:unauthorized"));
      return false;
    }
    const json = await res.json();
    const data = json?.data;
    if (!data?.access_token) {
      clearTokens();
      return false;
    }
    const prev = getTokens();
    setTokens({
      access_token: data.access_token,
      refresh_token: data.refresh_token,
      token_type: data.token_type ?? "Bearer",
      expires_in: data.expires_in ?? 0,
      user: prev?.user ?? data.user,
    });
    return true;
  } catch {
    clearTokens();
    window.dispatchEvent(new Event("auth:unauthorized"));
    return false;
  }
}

function buildHeaders(auth: boolean): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (auth) {
    const t = getTokens();
    if (t) headers["Authorization"] = `${t.token_type} ${t.access_token}`;
  }
  return headers;
}

export async function request<T>(
  method: Method,
  path: string,
  body?: unknown,
  auth = true
): Promise<T> {
  const doFetch = () =>
    fetch(path, {
      method,
      headers: buildHeaders(auth),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });

  let res = await doFetch();

  if (res.status === 401 && auth && !NO_REFRESH_PATHS.has(path)) {
    const refreshed = await refreshAuth();
    if (refreshed) {
      res = await doFetch();
      if (res.status === 401) {
        clearTokens();
        window.dispatchEvent(new Event("auth:unauthorized"));
        throw new ApiError(401, "UNAUTHORIZED", "登录已失效，请重新登录");
      }
    } else {
      throw new ApiError(401, "UNAUTHORIZED", "登录已失效，请重新登录");
    }
  }

  const text = await res.text();
  let json: any = null;
  if (text) {
    try {
      json = JSON.parse(text);
    } catch {
      // 非 JSON 响应体（如 502 网关返回的文本）
    }
  }

  if (!res.ok) {
    const message = json?.message ?? res.statusText ?? "请求失败";
    const code = json?.code ?? "ERROR";
    throw new ApiError(res.status, code, message, json?.request_id);
  }

  return (json?.data ?? null) as T;
}
