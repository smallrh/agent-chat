import type { ReactNode } from "react";
import { Navbar } from "./Navbar";

/**
 * 应用外壳。
 *
 * 关键点（这里是之前「右侧/底部大片空白、侧栏与内容不独立」的根因）：
 * 1. 外壳必须有一个**确定的高度**（h-[100dvh]），否则 h-full 的百分比高度
 *    无法解析，整页会塌缩成内容高度，输入框就会浮在半空、下方留白；
 * 2. main 必须是 **flex-col（纵向）**。之前写成 `flex`（横向），子页面没有
 *    w-full/flex-1，于是宽度退化成「适应内容宽度」——在宽屏上聊天列只有
 *    几百 px，右边自然是大量空白。改成 flex-col 后 align-items:stretch
 *    会把子页面在横向上撑满，纵向再由子页面自己的 h-full 撑满。
 * 3. overflow-hidden 把滚动收敛到各页面内部，避免 body 出现双滚动条。
 */
export function Layout({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-[100dvh] w-full flex-col overflow-hidden bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <Navbar />
      <main className="flex min-h-0 w-full flex-1 flex-col overflow-hidden">
        {children}
      </main>
    </div>
  );
}
