import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import {
  ChatCircle,
  FolderOpen,
  Brain,
  SignOut,
  Sun,
  Moon,
} from "@phosphor-icons/react";

type Theme = "light" | "dark";

function getInitialTheme(): Theme {
  try {
    const stored = localStorage.getItem("theme");
    if (stored === "dark" || stored === "light") return stored;
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  } catch {
    return "light";
  }
}

function applyTheme(t: Theme) {
  const root = document.documentElement;
  if (t === "dark") root.classList.add("dark");
  else root.classList.remove("dark");
  try {
    localStorage.setItem("theme", t);
  } catch {
    /* ignore */
  }
}

export function Navbar() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [theme, setTheme] = useState<Theme>(getInitialTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const handleLogout = async () => {
    await logout();
    navigate("/login");
  };

  const navItems = [
    { to: "/tasks", label: "任务", icon: FolderOpen },
    { to: "/memories", label: "记忆", icon: Brain },
  ];

  const isActive = (to: string) =>
    to === "/"
      ? location.pathname === "/"
      : location.pathname.startsWith(to);

  return (
    <header className="relative z-20 flex h-14 shrink-0 items-center justify-between border-b border-zinc-200 bg-white px-3 dark:border-zinc-800 dark:bg-zinc-900 sm:px-5">
      {/* Left: logo + brand */}
      <div className="flex min-w-0 items-center gap-2.5">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-brand-600 text-white shadow-sm shadow-brand-600/25">
          <ChatCircle weight="fill" size={16} />
        </div>
        <span className="truncate text-[15px] font-semibold tracking-tight text-zinc-900 dark:text-zinc-50 sm:text-base">
          AI Agent
        </span>
      </div>

      {/* Right: nav + theme + user */}
      <div className="flex shrink-0 items-center gap-1 sm:gap-1.5">
        {/* Nav items */}
        <nav className="hidden items-center gap-0.5 md:flex">
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = isActive(item.to);
            return (
              <button
                key={item.to}
                onClick={() => navigate(item.to)}
                className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-[13px] font-medium transition ${
                  active
                    ? "bg-brand-50 text-brand-700 dark:bg-brand-900/30 dark:text-brand-300"
                    : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-900 dark:hover:text-zinc-100"
                }`}
              >
                <Icon size={15} />
                {item.label}
              </button>
            );
          })}
        </nav>

        {/* Mobile icons */}
        <div className="flex items-center gap-0.5 md:hidden">
          {navItems.map((item) => {
            const Icon = item.icon;
            const active = isActive(item.to);
            return (
              <button
                key={item.to}
                onClick={() => navigate(item.to)}
                title={item.label}
                className={`inline-flex h-8 w-8 items-center justify-center rounded-full transition ${
                  active
                    ? "bg-brand-50 text-brand-700 dark:bg-brand-900/30 dark:text-brand-300"
                    : "text-zinc-500 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-900 dark:hover:text-zinc-100"
                }`}
              >
                <Icon size={17} />
              </button>
            );
          })}
        </div>

        {/* Theme toggle */}
        <button
          onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
          title={theme === "dark" ? "切换到浅色模式" : "切换到暗色模式"}
          className="inline-flex h-8 w-8 items-center justify-center rounded-full text-zinc-500 transition hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-900 dark:hover:text-zinc-100"
        >
          {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
        </button>

        {/* User */}
        {user && (
          <div className="ml-1 hidden items-center gap-2 border-l border-zinc-200 pl-2.5 sm:flex dark:border-zinc-800">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-zinc-100 text-[12px] font-semibold uppercase text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
              {(user.username || "?").slice(0, 1)}
            </span>
            <span className="max-w-[100px] truncate text-[13px] font-medium text-zinc-700 dark:text-zinc-300">
              {user.username}
            </span>
            <button
              onClick={handleLogout}
              title="登出"
              className="inline-flex h-8 w-8 items-center justify-center rounded-full text-zinc-500 transition hover:bg-red-50 hover:text-red-600 dark:text-zinc-400 dark:hover:bg-red-950/40 dark:hover:text-red-400"
            >
              <SignOut size={16} />
            </button>
          </div>
        )}

        {/* Mobile logout */}
        {user && (
          <button
            onClick={handleLogout}
            title="登出"
            className="ml-1 inline-flex h-8 w-8 items-center justify-center rounded-full text-zinc-500 transition hover:bg-red-50 hover:text-red-600 dark:text-zinc-400 dark:hover:bg-red-950/40 dark:hover:text-red-400 sm:hidden"
          >
            <SignOut size={16} />
          </button>
        )}
      </div>
    </header>
  );
}
