import type { ReactNode } from "react";

export type OutlineItem = { id: string; text: string; rawText: string; level: number; offset: number; line: number };

export function articleWordCount(content: string) {
  return content.replace(/```[\s\S]*?```/g, " ").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[#*_`>|~]/g, " ").match(/\S+/g)?.length || 0;
}

export function articleOutline(content: string): OutlineItem[] {
  let offset = 0;
  let fenced = false;
  const items: OutlineItem[] = [];
  content.split("\n").forEach((line, index) => {
    if (/^\s*```/.test(line)) fenced = !fenced;
    const match = !fenced && /^(#{1,2})\s+(.+?)\s*$/.exec(line);
    if (match) items.push({ id: `as-heading-${index}`, text: match[2].replace(/[*_`]/g, ""), rawText: match[2], level: match[1].length, offset, line: index });
    offset += line.length + 1;
  });
  return items;
}

export function safeArticleUrl(value: string): string | null {
  if (value.length > 2048) return null;
  try {
    const url = new URL(value.trim());
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return null;
    return url.href;
  } catch { return null; }
}

function inline(text: string, prefix: string, depth = 0): ReactNode[] {
  if (depth > 4) return [text];
  const pattern = /(\[([^\]\n]+)\]\(([^\s)]+)\)|\*\*([^*\n]+)\*\*|__([^_\n]+)__|`([^`\n]+)`|\*([^*\n]+)\*|_([^_\n]+)_|~~([^~\n]+)~~)/g;
  const result: ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text))) {
    if (match.index > cursor) result.push(text.slice(cursor, match.index));
    const key = `${prefix}-${match.index}`;
    if (match[2]) {
      const href = safeArticleUrl(match[3]);
      result.push(href ? <a key={key} href={href} target="_blank" rel="noopener noreferrer">{inline(match[2], key, depth + 1)}</a> : match[0]);
    } else if (match[4] || match[5]) result.push(<strong key={key}>{inline(match[4] || match[5], key, depth + 1)}</strong>);
    else if (match[6]) result.push(<code key={key}>{match[6]}</code>);
    else if (match[7] || match[8]) result.push(<em key={key}>{inline(match[7] || match[8], key, depth + 1)}</em>);
    else if (match[9]) result.push(<s key={key}>{inline(match[9], key, depth + 1)}</s>);
    cursor = match.index + match[0].length;
  }
  if (cursor < text.length) result.push(text.slice(cursor));
  return result;
}

export function ArticleMarkdown({ content, dropCap = false }: { content: string; dropCap?: boolean }) {
  const blocks: ReactNode[] = [];
  const lines = content.split(/\r?\n/);
  let paragraph: string[] = [];
  let list: string[] = [];
  let ordered = false;
  let quote: string[] = [];
  let code: string[] = [];
  let fenced = false;
  const flushParagraph = () => { if (paragraph.length) { blocks.push(<p key={`p${blocks.length}`}>{inline(paragraph.join(" "), `p${blocks.length}`)}</p>); paragraph = []; } };
  const flushList = () => { if (list.length) { const children = list.map((item, i) => <li key={i}>{inline(item, `li${blocks.length}-${i}`)}</li>); blocks.push(ordered ? <ol key={`l${blocks.length}`}>{children}</ol> : <ul key={`l${blocks.length}`}>{children}</ul>); list = []; } };
  const flushQuote = () => { if (quote.length) { blocks.push(<blockquote key={`q${blocks.length}`}>{inline(quote.join(" "), `q${blocks.length}`)}</blockquote>); quote = []; } };
  const flush = () => { flushParagraph(); flushList(); flushQuote(); };
  lines.forEach((line, index) => {
    if (/^\s*```/.test(line)) { flush(); if (fenced) { blocks.push(<pre key={`c${blocks.length}`}><code>{code.join("\n")}</code></pre>); code = []; } fenced = !fenced; return; }
    if (fenced) { code.push(line); return; }
    if (!line.trim()) { flush(); return; }
    const heading = /^(#{1,6})\s+(.+?)\s*$/.exec(line);
    if (heading) {
      flush(); const children = inline(heading[2], `h${index}`); const id = `as-heading-${index}`;
      blocks.push(heading[1].length < 3 ? <h2 id={id} key={id}>{children}</h2> : <h3 id={id} key={id}>{children}</h3>);
      return;
    }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { flush(); blocks.push(<hr key={`r${index}`} />); return; }
    const item = /^(?:\s*[-*+]\s+|\s*\d+\.\s+)(.+)$/.exec(line);
    if (item) { flushParagraph(); flushQuote(); const nextOrdered = /^\s*\d+\./.test(line); if (list.length && ordered !== nextOrdered) flushList(); ordered = nextOrdered; list.push(item[1]); return; }
    const quoted = /^>\s?(.*)$/.exec(line);
    if (quoted) { flushParagraph(); flushList(); quote.push(quoted[1]); return; }
    flushList(); flushQuote(); paragraph.push(line);
  });
  flush();
  if (code.length) blocks.push(<pre key="code-end"><code>{code.join("\n")}</code></pre>);
  return <div className={`as-reading-content${dropCap ? " as-dropcap" : ""}`}>{blocks}</div>;
}
