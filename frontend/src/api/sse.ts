// 共享的 SSE 读取工具。
//
// 原先只有 /agent/chat/stream 一处手写 SSE 解析（在 api/chat.ts 里）。
// P2 加了 /api/v1/tasks/stream 之后就变成两处同样的代码 —— 抽出来共用，
// 否则「空行分隔」「多 data 行拼接」「注释心跳行忽略」这些细节只要漏一处，
// 表现就是事件丢失或解析出半截 JSON。

import type { SSEEvent } from "../types";

/** 解析单个 SSE 事件块（event: / data: 行）。 */
export function parseBlock(block: string): SSEEvent | null {
  let event = "message";
  let data = "";
  for (const line of block.split(/\r?\n/)) {
    // SSE 心跳（": ping"）与注释行直接跳过 —— 心跳只是为了让反向代理
    // 不按空闲超时掐断连接，不是业务事件。
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      let d = line.slice(5);
      if (d.startsWith(" ")) d = d.slice(1);
      data += d;
    }
  }
  if (!data) return null;
  return { event, data };
}

/**
 * 从一个已拿到的 Response 里持续读出 SSE 事件。
 *
 * 返回异步生成器；调用方 abort 或连接关闭时自然结束。
 */
export async function* readSSEStream(
  res: Response
): AsyncGenerator<SSEEvent> {
  if (!res.body) return;

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });

    // SSE 事件块以空行（\n\n 或 \r\n\r\n）分隔
    let sep: RegExpExecArray | null;
    while ((sep = /\r?\n\r?\n/.exec(buf)) !== null) {
      const block = buf.slice(0, sep.index);
      buf = buf.slice(sep.index + sep[0].length);
      const evt = parseBlock(block);
      if (evt) yield evt;
    }
  }
}
