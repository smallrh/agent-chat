// 本地 Token 持久化（localStorage）。
// 只存鉴权必需字段，绝不存密码等敏感信息。

export interface StoredUser {
  user_uuid: string;
  username: string;
  email: string;
}

export interface AuthTokens {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: StoredUser;
}

const STORAGE_KEY = "agent_auth_tokens";

export function getTokens(): AuthTokens | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as AuthTokens;
  } catch {
    return null;
  }
}

export function setTokens(tokens: AuthTokens): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(tokens));
}

export function clearTokens(): void {
  localStorage.removeItem(STORAGE_KEY);
}

export function getAccessToken(): string | null {
  return getTokens()?.access_token ?? null;
}

export function getRefreshToken(): string | null {
  return getTokens()?.refresh_token ?? null;
}
