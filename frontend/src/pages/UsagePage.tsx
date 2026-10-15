// 用量页（P3 计费）：让用户看得见自己今天用了多少、还剩多少。
//
// 为什么必须有这个页面：计费功能如果没有自助查询，用户的体验就是
// 「某天突然被 429 拦住，完全不知道为什么」。把用量摆出来，
// 超额就从「莫名报错」变成「看得见的预期内消耗」。

import { useCallback, useEffect, useState } from "react";
import {
  getMyUsage,
  METRIC_META,
  METRIC_ORDER,
  type QuotaItem,
  type UsageResponse,
} from "../api/usage";

function pct(item: QuotaItem): number {
  if (!item.limit) return 0; // 不限
  return Math.max(0, Math.min(100, Math.round((item.used / item.limit) * 100)));
}

function barColor(item: QuotaItem): string {
  if (!item.limit) return "bg-slate-300";
  const p = pct(item);
  if (p >= 100) return "bg-red-500";
  if (p >= 80) return "bg-amber-500";
  return "bg-brand-600";
}

export function UsagePage() {
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setUsage(await getMyUsage());
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载用量失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-white px-3 py-2 sm:px-6 sm:py-2.5">
        <div className="min-w-0 text-sm font-medium text-slate-700">
          用量与配额
          <span className="ml-2 text-xs font-normal text-slate-400">
            每日额度次日 0 点（UTC）重置
          </span>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="ml-auto rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
        >
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-4 sm:px-6 sm:py-6">
        <div className="mx-auto w-full max-w-3xl space-y-3">
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-600">
              {error}
            </div>
          )}

          {loading && !usage && (
            <div className="py-16 text-center text-sm text-slate-400">加载中…</div>
          )}

          {usage && (
            <>
              <div className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs text-slate-500 shadow-sm">
                <span className="rounded bg-brand-50 px-1.5 py-0.5 font-medium text-brand-700">
                  {usage.plan_code}
                </span>
                <span>统计日 {usage.day}</span>
                <span className="text-slate-300">·</span>
                <span>统计月 {usage.month}</span>
              </div>

              {METRIC_ORDER.map((key) => {
                const item = usage.items?.[key];
                if (!item) return null;
                const meta = METRIC_META[key] ?? {
                  label: key,
                  hint: "",
                  icon: "•",
                };
                const unlimited = !item.limit;
                const exhausted = !unlimited && item.remain <= 0;

                return (
                  <div
                    key={key}
                    className="rounded-xl border border-slate-200 bg-white p-3 shadow-sm sm:p-4"
                  >
                    <div className="mb-1.5 flex items-center gap-2">
                      <span className="text-base">{meta.icon}</span>
                      <span className="text-sm font-medium text-slate-800">
                        {meta.label}
                      </span>
                      <span className="ml-auto text-xs text-slate-500">
                        {unlimited ? (
                          <span className="text-slate-400">不限</span>
                        ) : (
                          <>
                            <span className="tabular-nums">{item.used}</span>
                            <span className="text-slate-400"> / {item.limit}</span>
                          </>
                        )}
                      </span>
                    </div>

                    <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
                      <div
                        className={`h-full rounded-full transition-all duration-500 ${barColor(item)}`}
                        style={{ width: unlimited ? "100%" : `${pct(item)}%` }}
                      />
                    </div>

                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-400">
                      <span>
                        今日剩余{" "}
                        <span className="text-slate-600">
                          {unlimited ? "不限" : item.remain}
                        </span>
                      </span>
                      <span>
                        本月累计{" "}
                        <span className="text-slate-600">{item.month}</span>
                      </span>
                      {exhausted && (
                        <span className="text-red-500">
                          今日额度已用完，明天再来或联系管理员调整套餐
                        </span>
                      )}
                    </div>

                    <div className="mt-1 text-[11px] text-slate-400">
                      {meta.hint}
                    </div>
                  </div>
                );
              })}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
