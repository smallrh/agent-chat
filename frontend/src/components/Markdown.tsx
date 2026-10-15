// 零依赖的轻量 Markdown 渲染（覆盖对话场景常见语法）：
// 代码块(带语言标签 + 复制)、行内代码、粗体、斜体、链接、标题、引用、有序/无序列表、段落。
// 安全：全部用 React 元素构造，绝不使用 dangerouslySetInnerHTML。
import { useState, type ReactNode } from "react";

/** 行内格式化：**粗体**、`代码`、*斜体*、[文本](url)。 */
function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  const regex = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*]+\*)|(\[[^\]]+\]\([^)]+\))/g;
  let lastIndex = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  while ((m = regex.exec(text)) !== null) {
    if (m.index > lastIndex) {
      nodes.push(text.slice(lastIndex, m.index));
    }
    const token = m[0];
    if (token.startsWith("`")) {
      nodes.push(
        <code key={key++} className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[12px] text-pink-600">
          {token.slice(1, -1)}
        </code>
      );
    } else if (token.startsWith("**")) {
      nodes.push(
        <strong key={key++} className="font-semibold">
          {token.slice(2, -2)}
        </strong>
      );
    } else if (token.startsWith("*")) {
      nodes.push(<em key={key++}>{token.slice(1, -1)}</em>);
    } else if (token.startsWith("[")) {
      const lm = /\[([^\]]+)\]\(([^)]+)\)/.exec(token);
      if (lm) {
        nodes.push(
          <a key={key++} href={lm[2]} target="_blank" rel="noreferrer" className="text-brand-600 underline">
            {lm[1]}
          </a>
        );
      } else {
        nodes.push(token);
      }
    }
    lastIndex = regex.lastIndex;
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return nodes;
}

function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // 忽略复制失败（如非安全上下文）
    }
  };
  return (
    <div className="my-2 overflow-hidden rounded-lg border border-slate-200 bg-slate-900">
      <div className="flex items-center justify-between bg-slate-800 px-3 py-1.5 text-[11px] text-slate-300">
        <span className="font-mono">{lang || "code"}</span>
        <button
          onClick={copy}
          className="rounded px-2 py-0.5 text-slate-300 transition hover:bg-slate-700"
        >
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="overflow-x-auto px-3 py-2 text-[12px] leading-relaxed text-slate-100">
        <code className="font-mono whitespace-pre">{code}</code>
      </pre>
    </div>
  );
}

export function Markdown({ content }: { content: string }) {
  const lines = content.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let key = 0;

  const isFence = (l: string) => l.trimStart().startsWith("```");
  const isHeading = (l: string) => /^#{1,3}\s+/.test(l);
  const isQuote = (l: string) => l.startsWith(">");
  const isUl = (l: string) => /^[-*]\s+/.test(l);
  const isOl = (l: string) => /^\d+\.\s+/.test(l);

  while (i < lines.length) {
    const line = lines[i];

    // 代码块
    if (isFence(line)) {
      const lang = line.trimStart().slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !isFence(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // 跳过结束的 ```
      blocks.push(<CodeBlock key={key++} lang={lang} code={codeLines.join("\n")} />);
      continue;
    }

    // 标题
    if (isHeading(line)) {
      const h = /^(#{1,3})\s+(.*)$/.exec(line)!;
      const level = h[1].length;
      const cls =
        level === 1
          ? "text-lg font-bold mt-2 mb-1"
          : level === 2
            ? "text-base font-semibold mt-2 mb-1"
            : "text-sm font-semibold mt-1 mb-0.5";
      blocks.push(
        <div key={key++} className={cls}>
          {renderInline(h[2])}
        </div>
      );
      i++;
      continue;
    }

    // 引用
    if (isQuote(line)) {
      const quoteLines: string[] = [];
      while (i < lines.length && isQuote(lines[i])) {
        quoteLines.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      blocks.push(
        <blockquote key={key++} className="my-1 border-l-4 border-slate-300 pl-3 text-slate-500">
          {renderInline(quoteLines.join(" "))}
        </blockquote>
      );
      continue;
    }

    // 无序列表
    if (isUl(line)) {
      const items: string[] = [];
      while (i < lines.length && isUl(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ""));
        i++;
      }
      blocks.push(
        <ul key={key++} className="my-1 list-disc space-y-0.5 pl-5">
          {items.map((it, idx) => (
            <li key={idx}>{renderInline(it)}</li>
          ))}
        </ul>
      );
      continue;
    }

    // 有序列表
    if (isOl(line)) {
      const items: string[] = [];
      while (i < lines.length && isOl(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ""));
        i++;
      }
      blocks.push(
        <ol key={key++} className="my-1 list-decimal space-y-0.5 pl-5">
          {items.map((it, idx) => (
            <li key={idx}>{renderInline(it)}</li>
          ))}
        </ol>
      );
      continue;
    }

    // 空行
    if (line.trim() === "") {
      i++;
      continue;
    }

    // 段落
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !isFence(lines[i]) &&
      !isHeading(lines[i]) &&
      !isQuote(lines[i]) &&
      !isUl(lines[i]) &&
      !isOl(lines[i])
    ) {
      para.push(lines[i]);
      i++;
    }
    blocks.push(
      <p key={key++} className="my-1 leading-relaxed">
        {renderInline(para.join(" "))}
      </p>
    );
  }

  return <div className="text-sm leading-relaxed">{blocks}</div>;
}
