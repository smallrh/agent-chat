import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { ApiError } from "../api/client";
import { ChatCircle, Eye, EyeSlash } from "@phosphor-icons/react";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!username.trim() || !password) {
      setError("请输入用户名和密码");
      return;
    }
    setError(null);
    setLoading(true);
    try {
      await login(username.trim(), password);
      navigate("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "登录失败，请重试");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative flex min-h-[100dvh] items-center justify-center bg-zinc-50 px-4 py-10 dark:bg-zinc-950">
      {/* 背景：细网格 + 中心径向渐隐。比通用的「两团模糊光斑」克制，也更像控制台 */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.5] dark:opacity-20"
        style={{
          backgroundImage:
            "linear-gradient(to right, rgb(228 228 231 / 0.8) 1px, transparent 1px), linear-gradient(to bottom, rgb(228 228 231 / 0.8) 1px, transparent 1px)",
          backgroundSize: "32px 32px",
          maskImage:
            "radial-gradient(ellipse 70% 55% at 50% 38%, #000 0%, transparent 72%)",
          WebkitMaskImage:
            "radial-gradient(ellipse 70% 55% at 50% 38%, #000 0%, transparent 72%)",
        }}
      />

      <div className="relative w-full max-w-md">
        {/* Brand header */}
        <div className="mb-6 flex flex-col items-center">
          <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-600 shadow-lg shadow-brand-600/25">
            <ChatCircle weight="fill" size={22} className="text-white" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            欢迎回来
          </h1>
          <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
            登录到 AI Agent 控制台
          </p>
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-zinc-200 bg-white p-6 shadow-xl shadow-zinc-900/5 sm:p-7 dark:border-zinc-800 dark:bg-zinc-900/80 dark:shadow-black/40">
          {error && (
            <div className="mb-4 rounded-xl border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-600 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-300">
              {error}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-zinc-700 dark:text-zinc-300">
                用户名
              </label>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                className="w-full rounded-xl border border-zinc-200 bg-white px-3.5 py-2.5 text-[14px] text-zinc-900 outline-none transition focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-50 dark:focus:border-brand-400 dark:focus:ring-brand-400/20"
                placeholder="请输入用户名"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-[13px] font-medium text-zinc-700 dark:text-zinc-300">
                密码
              </label>
              <div className="relative">
                <input
                  type={showPassword ? "text" : "password"}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  className="w-full rounded-xl border border-zinc-200 bg-white px-3.5 py-2.5 pr-10 text-[14px] text-zinc-900 outline-none transition focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-50 dark:focus:border-brand-400 dark:focus:ring-brand-400/20"
                  placeholder="请输入密码"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  className="absolute right-2 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-full text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-600 dark:hover:bg-zinc-700 dark:hover:text-zinc-300"
                >
                  {showPassword ? <EyeSlash size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>
            <button
              type="submit"
              disabled={loading}
              className="w-full rounded-full bg-brand-600 py-2.5 text-[14px] font-medium text-white shadow-lg shadow-brand-600/20 transition hover:bg-brand-700 active:scale-[0.98] disabled:opacity-60"
            >
              {loading ? "登录中…" : "登录"}
            </button>
          </form>

          <p className="mt-5 text-center text-[13px] text-zinc-500 dark:text-zinc-400">
            还没有账号？{" "}
            <Link
              to="/register"
              className="font-medium text-brand-600 hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
            >
              立即注册
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
