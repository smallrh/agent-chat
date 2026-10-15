// 用量 / 配额 API（走 Go BFF /api/v1/usage → Redis 计数 + user_plans）。

import { request } from "./client";

export interface QuotaItem {
  /** 今日已用 */
  used: number;
  /** 每日额度；0 = 不限 */
  limit: number;
  /** 本月累计（仅展示与对账，不做拦截） */
  month: number;
  /** 今日剩余；不限时为 -1 */
  remain: number;
}

export interface UsageResponse {
  user_uuid: string;
  plan_code: string;
  day: string; // YYYYMMDD
  month: string; // YYYYMM
  items: Record<string, QuotaItem>;
}

export function getMyUsage(): Promise<UsageResponse> {
  return request<UsageResponse>("GET", "/api/v1/usage/me");
}

/** 计量维度的中文名与说明（与后端 metric 常量一一对应） */
export const METRIC_META: Record<
  string,
  { label: string; hint: string; icon: string }
> = {
  chat: {
    label: "对话",
    hint: "每完成一轮问答计 1 次；被取消或中途失败不计数",
    icon: "💬",
  },
  web_search: {
    label: "联网检索",
    hint: "每次真实发起联网搜索计 1 次（命中缓存复用不重复计）",
    icon: "🔍",
  },
  task: {
    label: "后台任务",
    hint: "每创建一个后台任务计 1 次，重跑同样计 1 次",
    icon: "🗂️",
  },
};

export const METRIC_ORDER = ["chat", "web_search", "task"];
