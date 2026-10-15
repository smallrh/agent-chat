import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { getTokens, setTokens, clearTokens } from "../api/tokenStore";
import { login as apiLogin, logout as apiLogout, me as apiMe } from "../api/auth";
import type { AuthUserPublic } from "../types";

interface AuthContextValue {
  user: AuthUserPublic | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUserPublic | null>(null);
  const [loading, setLoading] = useState(true);

  // 启动时若有本地 token，调用 /me 校验有效性并恢复用户态
  useEffect(() => {
    const tokens = getTokens();
    if (!tokens) {
      setLoading(false);
      return;
    }
    apiMe()
      .then((u) => setUser(u))
      .catch(() => {
        clearTokens();
        setUser(null);
      })
      .finally(() => setLoading(false));
  }, []);

  // 监听自动刷新失败事件 → 强制清登出
  useEffect(() => {
    const onUnauthorized = () => {
      clearTokens();
      setUser(null);
    };
    window.addEventListener("auth:unauthorized", onUnauthorized);
    return () => window.removeEventListener("auth:unauthorized", onUnauthorized);
  }, []);

  const login = async (username: string, password: string) => {
    const resp = await apiLogin({ username, password });
    setTokens({
      access_token: resp.access_token,
      refresh_token: resp.refresh_token,
      token_type: resp.token_type,
      expires_in: resp.expires_in,
      user: resp.user,
    });
    setUser(resp.user);
  };

  const logout = async () => {
    try {
      await apiLogout();
    } catch {
      // 即使后端失败也清本地状态
    }
    clearTokens();
    setUser(null);
  };

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
