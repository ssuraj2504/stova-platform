import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import type { Article, Project } from "@/types";
import { ArticleMarkdown, articleOutline, articleWordCount, safeArticleUrl, type OutlineItem } from "@/lib/article-markdown";
import "./article-studio.css";

export type ArticleStudioProps = {
  project: Project;
  article: Article;
  apiKey: string;
  serverConfigured: boolean;
  writingPaused?: boolean;
  writingUnavailableReason?: string;
  managedWritingLabel?: string;
  onWritingComplete?: () => void | Promise<void>;
  onClose: () => void;
  onSaved: (article: Article) => void;
  onPublished: (project: Project) => void;
  onNeedConnection: () => void;
};
type Selection = { start: number; end: number; top: number; left: number };
type Action = "rewrite" | "expand" | "style";

async function api<T>(path: string, body: unknown, method = "POST"): Promise<T> {
  const response = await fetch(path, { method, credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const result = await response.json().catch(() => ({})) as Record<string, unknown>;
  if (!response.ok) throw new Error(typeof result.error === "string" ? result.error : "This change could not be completed. Please try again.");
  return result as T;
}

function Glyph({ name, size = 18 }: { name: "back" | "download" | "check" | "arrow" | "quote" | "link" | "close" | "refresh" | "expand"; size?: number }) {
  const paths: Record<string, ReactNode> = {
    back: <path d="M19 12H5m6-6-6 6 6 6" />,
    download: <path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5" />,
    check: <path d="m5 12 4 4L19 6" />,
    arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
    quote: <><path d="M10 5H4v7h6v-7ZM20 5h-6v7h6V5ZM4 19c4 0 6-3 6-7m4 7c4 0 6-3 6-7" /></>,
    link: <><path d="m9 15 6-6m-5-3 2-2a5 5 0 0 1 7 7l-3 3M14 18l-2 2a5 5 0 0 1-7-7l3-3" /></>,
    close: <path d="m6 6 12 12M6 18 18 6" />,
    refresh: <path d="M20 9a8 8 0 0 0-14-4L3 8m0-5v5h5M4 15a8 8 0 0 0 14 4l3-3m0 5v-5h-5" />,
    expand: <path d="M12 3v18m-4-4 4 4 4-4M8 7l4-4 4 4M3 12h3m12 0h3" />,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function caretPosition(element: HTMLTextAreaElement, offset: number) {
  const rect = element.getBoundingClientRect();
  const computed = getComputedStyle(element);
  const mirror = document.createElement("div");
  Object.assign(mirror.style, {
    position: "fixed", visibility: "hidden", pointerEvents: "none", whiteSpace: "pre-wrap", overflowWrap: "break-word",
    width: `${rect.width}px`, boxSizing: computed.boxSizing, padding: computed.padding, border: computed.border,
    font: computed.font, lineHeight: computed.lineHeight, letterSpacing: computed.letterSpacing, tabSize: computed.tabSize,
    top: `${rect.top - element.scrollTop}px`, left: `${rect.left - element.scrollLeft}px`,
  });
  mirror.textContent = element.value.slice(0, offset);
  const marker = document.createElement("span"); marker.textContent = element.value.slice(offset, offset + 1) || "."; mirror.appendChild(marker);
  document.body.appendChild(mirror); const position = marker.getBoundingClientRect(); mirror.remove();
  return { top: position.top, left: position.left, height: position.height || 28 };
}

export default function ArticleStudio({ project, article, apiKey, serverConfigured, writingPaused = false, writingUnavailableReason, managedWritingLabel, onWritingComplete, onClose, onSaved, onPublished, onNeedConnection }: ArticleStudioProps) {
  const [saved, setSaved] = useState(article);
  const [content, setContent] = useState(article.content);
  const [dropCap, setDropCap] = useState(Boolean(article.dropCap));
  const [mode, setMode] = useState<"read" | "write">(article.content ? "read" : "write");
  const [selectedHeading, setSelectedHeading] = useState<string | null>(null);
  const [style, setStyle] = useState("Clear and direct");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmClose, setConfirmClose] = useState(false);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [linkOpen, setLinkOpen] = useState(false);
  const [linkUrl, setLinkUrl] = useState("");
  const [linkError, setLinkError] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);
  const reading = useRef<HTMLDivElement>(null);
  const toolbar = useRef<HTMLDivElement>(null);
  const urlInput = useRef<HTMLInputElement>(null);
  const selectionFrame = useRef<number | null>(null);
  const outline = useMemo(() => articleOutline(content), [content]);
  const words = articleWordCount(content);
  const minutes = Math.max(1, Math.ceil(words / 220));
  const readOnly = saved.status === "published" || article.status === "published";
  const dirty = content !== saved.content || dropCap !== Boolean(saved.dropCap);
  const canSave = words >= 100 && words <= 3000 && content.length <= 30000;
  const currentSection = outline.find(item => item.id === selectedHeading);
  const basePath = `/api/projects/${project.id}/articles/${article.id}`;

  useEffect(() => {
    const previous = document.body.style.overflow; document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previous; };
  }, []);
  useEffect(() => {
    setSaved(article); setContent(article.content); setDropCap(Boolean(article.dropCap)); setMode(article.content ? "read" : "write");
    setSelectedHeading(null); setError(""); setNotice(""); setConfirmClose(false); setSelection(null);
  }, [article.id]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty && !readOnly) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn);
  }, [dirty, readOnly]);
  useEffect(() => {
    if (!selection) return;
    const outside = (event: PointerEvent) => { if (!toolbar.current?.contains(event.target as Node) && event.target !== textarea.current) { setSelection(null); setLinkOpen(false); } };
    document.addEventListener("pointerdown", outside); document.addEventListener("scroll", scrollSelection, true);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("scroll", scrollSelection, true); };
  }, [selection, linkOpen, busy]);
  useEffect(() => () => { if (selectionFrame.current !== null) cancelAnimationFrame(selectionFrame.current); }, []);
  useEffect(() => { if (linkOpen) urlInput.current?.focus(); }, [linkOpen]);

  function close() { if (busy) return; if (dirty && !readOnly) setConfirmClose(true); else onClose(); }
  function selectText() {
    const element = textarea.current; if (!element || element.selectionStart === element.selectionEnd || busy) { if (!linkOpen) setSelection(null); return; }
    const caret = caretPosition(element, element.selectionEnd);
    const top = Math.max(76, Math.min(window.innerHeight - 100, caret.top - 53));
    const left = Math.max(150, Math.min(window.innerWidth - 150, caret.left));
    setSelection({ start: element.selectionStart, end: element.selectionEnd, top, left });
  }
  function scrollSelection() {
    if (linkOpen) return;
    if (selectionFrame.current !== null) cancelAnimationFrame(selectionFrame.current);
    selectionFrame.current = requestAnimationFrame(() => {
      selectionFrame.current = null;
      const element = textarea.current;
      if (element && document.activeElement === element && element.selectionStart !== element.selectionEnd) selectText();
      else setSelection(null);
    });
  }
  function replaceSelection(next: string, start: number, end: number) {
    setContent(next); setError(""); setNotice(""); setSelection(null); setLinkOpen(false);
    requestAnimationFrame(() => { const element = textarea.current; if (element) { element.focus(); element.setSelectionRange(start, end); } });
  }
  function format(kind: "bold" | "italic" | "quote" | "link", url?: string) {
    const element = textarea.current; if (!element || readOnly || busy) return;
    const start = selection?.start ?? element.selectionStart; const end = selection?.end ?? element.selectionEnd;
    if (start === end) return;
    const selected = content.slice(start, end);
    if (kind === "quote") {
      const lineStart = content.lastIndexOf("\n", start - 1) + 1; const nextLine = content.indexOf("\n", end); const lineEnd = nextLine < 0 ? content.length : nextLine;
      const lines = content.slice(lineStart, lineEnd).split("\n"); const allQuoted = lines.every(line => /^>\s?/.test(line));
      const replacement = lines.map(line => allQuoted ? line.replace(/^>\s?/, "") : `> ${line}`).join("\n");
      replaceSelection(content.slice(0, lineStart) + replacement + content.slice(lineEnd), lineStart, lineStart + replacement.length); return;
    }
    const open = kind === "bold" ? "**" : kind === "italic" ? "*" : "[";
    const ending = kind === "link" ? `](${url?.replace(/\(/g, "%28").replace(/\)/g, "%29")})` : open;
    const leading = selected.match(/^\s*/)?.[0] || "";
    const trailing = selected.match(/\s*$/)?.[0] || "";
    const core = selected.slice(leading.length, selected.length - trailing.length);
    if (!core) return;
    const replacement = `${leading}${open}${core}${ending}${trailing}`;
    const selectedStart = start + leading.length + open.length;
    replaceSelection(content.slice(0, start) + replacement + content.slice(end), selectedStart, selectedStart + core.length);
  }
  function insertLink() {
    const url = safeArticleUrl(linkUrl);
    if (!url) { setLinkError("Enter a complete http:// or https:// address."); return; }
    format("link", url); setLinkUrl(""); setLinkError("");
  }
  function keydown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.ctrlKey || event.metaKey) && ["b", "i"].includes(event.key.toLowerCase())) { event.preventDefault(); format(event.key.toLowerCase() === "b" ? "bold" : "italic"); }
    if (event.key === "Escape" && (selection || linkOpen)) { event.preventDefault(); event.stopPropagation(); setSelection(null); setLinkOpen(false); }
  }
  function dialogKeys(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation();
      if (selection || linkOpen) { setSelection(null); setLinkOpen(false); }
      else if (confirmClose) setConfirmClose(false);
      else close();
    }
    if (event.key === "Tab") {
      const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled])')).filter(element => element.offsetParent !== null);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }
  function jump(item?: OutlineItem) {
    setSelectedHeading(item?.id ?? null); setSelection(null);
    if (mode === "write" && !readOnly) {
      const element = textarea.current; if (!element) return;
      element.focus(); const offset = item?.offset ?? 0; element.setSelectionRange(offset, offset);
      const caret = caretPosition(element, offset); const bounds = element.getBoundingClientRect(); element.scrollTop += caret.top - bounds.top - 75;
    } else if (item) reading.current?.querySelector<HTMLElement>(`#${item.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    else reading.current?.querySelector<HTMLElement>("#as-article-title")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  async function saveDraft(): Promise<Article | null> {
    if (readOnly || !canSave) return null;
    const result = await api<{ article: Article }>(basePath, { content, dropCap }, "PATCH");
    setSaved(result.article); setContent(result.article.content); setDropCap(Boolean(result.article.dropCap)); onSaved(result.article); return result.article;
  }
  async function save() {
    setBusy("save"); setError(""); setNotice("");
    try { if (await saveDraft()) setNotice("Draft saved to your publication."); } catch (error) { setError((error as Error).message); } finally { setBusy(null); }
  }
  async function publish() {
    if (readOnly || !canSave) return; setBusy("publish"); setError(""); setNotice("");
    try {
      if (dirty || saved.status !== "ready") { if (!await saveDraft()) return; }
      const result = await api<{ project: Project; article: Article; url: string }>(`${basePath}/publish`, {});
      setSaved(result.article); setContent(result.article.content); setDropCap(Boolean(result.article.dropCap)); setMode("read"); setSelection(null);
      onPublished(result.project); setNotice("This article is live. Its address is ready to share.");
    } catch (error) { setError((error as Error).message); } finally { setBusy(null); }
  }
  async function polish(action: Action) {
    if (readOnly || busy) return;
    if (!apiKey.trim() && !serverConfigured) { setError(writingUnavailableReason || "Shared writing is not ready yet. Your edits will stay here; visitors do not need to enter a key."); onNeedConnection(); await onWritingComplete?.(); return; }
    if (!canSave) { setError("Keep the draft between 100 and 3,000 words before polishing it."); return; }
    if (action === "expand" && !currentSection) { setError("Choose a section in the outline first."); return; }
    setBusy(action); setError(""); setNotice(""); setSelection(null);
    try {
      const result = await api<{ content: string; wordCount: number }>(`${basePath}/polish`, { apiKey: apiKey.trim(), action, content, ...(action === "expand" ? { section: currentSection!.rawText } : {}), ...(action === "style" ? { style } : {}) });
      setContent(result.content); setMode("read"); setNotice("The revised draft is ready to read. Save it when you’re happy with it.");
    } catch (error) { setError((error as Error).message); } finally { await onWritingComplete?.(); setBusy(null); }
  }
  function download() {
    const blob = new Blob([`# ${article.title}\n\n${content}\n`], { type: "text/markdown;charset=utf-8" }); const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${article.slug}.md`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 500);
  }
  const lastSaved = saved.updatedAt && !Number.isNaN(Date.parse(saved.updatedAt)) ? new Date(saved.updatedAt).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) : null;

  return <section className="article-studio" role="dialog" aria-modal="true" aria-label={`Article studio: ${article.title}`} onKeyDown={dialogKeys}>
    <header className="as-header">
      <div className="as-header-left"><button type="button" autoFocus className="as-back" onClick={close} disabled={Boolean(busy)} aria-label="Return to publication"><Glyph name="back" /></button><div className="as-studio-title"><span>STOVA <i>/</i> THE EDITING DESK</span><strong>{project.name}</strong></div></div>
      <div className="as-live-metrics" aria-live="polite"><b>{words.toLocaleString("en-IN")}</b> words<span>·</span>{minutes} min read</div>
      <div className="as-header-actions"><button type="button" className="as-download" onClick={download} title="Download current draft as Markdown"><Glyph name="download" /><span>Download</span></button>{readOnly ? <a className="as-primary" href={`/blog/${project.id}/${article.slug}`} target="_blank" rel="noopener noreferrer">View live <Glyph name="arrow" size={16} /></a> : <><button type="button" className="as-save" onClick={save} disabled={Boolean(busy) || !canSave || !dirty}>{busy === "save" ? <span className="as-spinner" /> : null}Save draft</button><button type="button" className="as-primary" onClick={publish} disabled={Boolean(busy) || !canSave}>{busy === "publish" ? <span className="as-spinner" /> : null}Publish live <Glyph name="arrow" size={16} /></button></>}</div>
    </header>
    <div className="as-workspace">
      <aside className="as-sidebar"><div className="as-sidebar-heading"><span className="as-kicker">A LITTLE STRUCTURE</span><h2>The shape of a story.</h2><p>Good writing begins with a clear path.</p></div>
        <nav className="as-outline" aria-label="Article outline"><button type="button" className={!selectedHeading ? "as-outline-item as-outline-title selected" : "as-outline-item as-outline-title"} onClick={() => jump()}><span>H1</span><strong>{article.title}</strong></button>{outline.map(item => <button type="button" key={item.id} className={`as-outline-item${selectedHeading === item.id ? " selected" : ""}`} onClick={() => jump(item)} aria-current={selectedHeading === item.id ? "location" : undefined}><span>H{item.level}</span><span>{item.text}</span></button>)}{!outline.length && <p className="as-outline-empty">Your section headings will appear here as the story takes shape.</p>}</nav>
        <div className="as-polish"><div className="as-polish-title"><span className="as-kicker">THE FINAL TOUCH</span><h3>A second pair of eyes.</h3><p>Refine with your studio’s writing service. Changes stay in your draft until you save them.</p></div><button className="as-polish-button" type="button" onClick={() => polish("rewrite")} disabled={Boolean(busy) || readOnly || !canSave || writingPaused && !apiKey}><Glyph name="refresh" /><span>{busy === "rewrite" ? "Rewriting the draft…" : "Rewrite article"}</span>{busy === "rewrite" && <span className="as-spinner" />}</button><button className="as-polish-button" type="button" onClick={() => polish("expand")} disabled={Boolean(busy) || readOnly || !canSave || !currentSection || writingPaused && !apiKey}><Glyph name="expand" /><span>{busy === "expand" ? "Developing the section…" : "Expand selected section"}</span>{busy === "expand" && <span className="as-spinner" />}</button><p className="as-section-hint">{currentSection ? <>Selected: <strong>{currentSection.text}</strong></> : "Select a heading in the outline to expand that section."}</p><label className="as-field-label" htmlFor="as-writing-style">Adjust the writing style</label><select id="as-writing-style" value={style} onChange={event => setStyle(event.target.value)} disabled={Boolean(busy) || readOnly}><option>Clear and direct</option><option>Warm and conversational</option><option>Formal and authoritative</option><option>Concise and practical</option></select><button type="button" className="as-style-button" onClick={() => polish("style")} disabled={Boolean(busy) || readOnly || !canSave || writingPaused && !apiKey}>{busy === "style" ? <><span className="as-spinner" />Adjusting the style…</> : <>Apply style <Glyph name="arrow" size={15} /></>}</button><div className="as-connection"><span className={!apiKey && serverConfigured ? "as-connection-dot available" : "as-connection-dot"} /><span>{apiKey ? "Personal Groq override selected" : managedWritingLabel || (serverConfigured ? "Shared studio writing" : "Shared writing needs setup")}</span><button type="button" onClick={onNeedConnection} disabled={Boolean(busy)}>Writing settings</button></div>{writingPaused && !apiKey && <p className="as-writing-pause" role="status">{writingUnavailableReason}</p>}</div>
        <div className="as-page-details"><label><input type="checkbox" checked={dropCap} onChange={event => setDropCap(event.target.checked)} disabled={Boolean(busy) || readOnly} /><span>Opening drop cap<small>A little character for the first paragraph.</small></span><span className="as-dropcap-sample" aria-hidden="true">Aa</span></label></div>
        <div className="as-sidebar-foot"><span>YOUR WORDS, WITH INTENTION.</span><p>Read it slowly.<br />Make it sound like you.</p></div>
      </aside>
      <div className="as-document-area"><div className="as-document-toolbar"><div className="as-mode-tabs" role="group" aria-label="Editor mode"><button type="button" className={mode === "read" ? "active" : ""} aria-pressed={mode === "read"} onClick={() => { setMode("read"); setSelection(null); }}>Read</button>{!readOnly && <button type="button" className={mode === "write" ? "active" : ""} aria-pressed={mode === "write"} onClick={() => setMode("write")}>Write</button>}</div><div className="as-save-state"><span className={`as-state-dot${dirty && !readOnly ? " dirty" : ""}`} />{readOnly ? "Published article" : dirty ? "Unsaved changes" : lastSaved ? `Saved at ${lastSaved}` : "Saved draft"}</div></div>
        {(error || notice) && <div className={`as-feedback${error ? " error" : ""}`} role={error ? "alert" : "status"}><span>{error || notice}</span><button type="button" onClick={() => { setError(""); setNotice(""); }} aria-label="Dismiss message"><Glyph name="close" size={15} /></button></div>}
        {confirmClose && <div className="as-close-confirm" role="alert"><div><strong>Keep these changes?</strong><p>You have edits that haven’t been saved.</p></div><button type="button" onClick={() => setConfirmClose(false)}>Keep editing</button><button type="button" className="as-discard" onClick={onClose}>Discard & leave</button></div>}
        <div className="as-paper-scroll" ref={reading}><article className="as-paper" style={{ "--article-brand": project.color } as CSSProperties}><header className="as-paper-header"><div className="as-paper-kicker"><span>{project.name} <i>/</i> THE JOURNAL</span><span>{readOnly ? "PUBLISHED EDITION" : "WORKING DRAFT"}</span></div><h1 id="as-article-title">{article.title}</h1><div className="as-article-byline">{project.logo ? <img className="as-author-logo" src={project.logo} alt={`${project.name} logo`} /> : <span className="as-author-mark">{project.name.slice(0, 1).toUpperCase()}</span>}<span>{project.name}<small>{minutes} minute read · {words.toLocaleString("en-IN")} words</small></span><span className="as-paper-rule" /></div></header>
          {mode === "write" && !readOnly ? <div className="as-write-area"><label className="as-write-hint" htmlFor="as-article-content">Select text to format it. Use ## for section headings.</label><textarea ref={textarea} id="as-article-content" aria-label="Article content" className="as-markdown-input" value={content} onChange={event => { setContent(event.target.value); setError(""); setNotice(""); setSelection(null); }} onSelect={selectText} onKeyDown={keydown} onScroll={scrollSelection} disabled={Boolean(busy)} spellCheck placeholder="Every story starts with a first sentence…" /></div> : content ? <ArticleMarkdown content={content} dropCap={dropCap} /> : <div className="as-empty-page"><span>01 — A BLANK PAGE</span><p>There’s a story in here.<br />Start writing to find it.</p><button type="button" onClick={() => setMode("write")}>Write the first sentence <Glyph name="arrow" size={16} /></button></div>}
          <footer className="as-paper-footer"><span>{project.name}</span><span>STORIES WITH SUBSTANCE.</span><span>— 01 —</span></footer>
        </article></div>
        <footer className="as-workspace-footer"><span>{mode === "write" && !readOnly ? "A working draft, ready for your point of view." : "A quiet space to give every sentence its due."}</span><span>{readOnly ? "Live and ready to read" : canSave ? "Save when it sounds like you" : "Save drafts between 100 and 3,000 words"}</span></footer>
      </div>
    </div>
    {selection && mode === "write" && !busy && <div ref={toolbar} className={`as-selection-toolbar${linkOpen ? " with-link" : ""}`} style={{ top: selection.top, left: selection.left }} role="toolbar" aria-label="Format selected text"><div className="as-format-actions"><button type="button" onMouseDown={event => event.preventDefault()} onClick={() => format("bold")} aria-label="Bold selected text" title="Bold · Ctrl+B"><b>B</b></button><button type="button" onMouseDown={event => event.preventDefault()} onClick={() => format("italic")} aria-label="Italicise selected text" title="Italic · Ctrl+I"><i>I</i></button><span /><button type="button" onMouseDown={event => event.preventDefault()} onClick={() => format("quote")} aria-label="Turn selected lines into a blockquote" title="Blockquote"><Glyph name="quote" size={17} /></button><button type="button" onMouseDown={event => event.preventDefault()} onClick={() => { setLinkOpen(value => !value); setLinkError(""); }} aria-label="Link selected text" aria-expanded={linkOpen} title="Add link"><Glyph name="link" size={17} /></button></div>{linkOpen && <form className="as-link-form" onSubmit={event => { event.preventDefault(); insertLink(); }}><label htmlFor="as-link-url">Link address</label><div><input ref={urlInput} id="as-link-url" type="url" value={linkUrl} onChange={event => { setLinkUrl(event.target.value); setLinkError(""); }} placeholder="https://example.com" autoComplete="off" /><button type="submit">Add</button></div>{linkError && <p role="alert">{linkError}</p>}</form>}</div>}
  </section>;
}
