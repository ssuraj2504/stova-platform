import { useEffect, useState, type CSSProperties } from "react";
import { ArrowLeft, ArrowRight, Loader2 } from "lucide-react";
import type { Article, Project } from "@/types";
import { ArticleMarkdown } from "@/lib/article-markdown";

function readerPath() {
  try {
    const parts = window.location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
    if (parts[0] !== "blog" || parts.length < 2 || parts.length > 3) return null;
    return { id: parts[1], slug: parts[2] || null };
  } catch { return null; }
}
function publishDate(value: string) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString("en", { day: "numeric", month: "long", year: "numeric" }); }
function readableAccent(hex: string) {
  const channels = [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16) / 255).map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722 > 0.16 ? "#1B2A4A" : hex;
}

export default function PublicReader() {
  const [project, setProject] = useState<Project | null>(null);
  const [article, setArticle] = useState<Article | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [path] = useState(readerPath);

  useEffect(() => {
    if (!path) { setError("This page could not be found."); setLoading(false); return; }
    const controller = new AbortController();
    const url = `/api/publications/${encodeURIComponent(path.id)}${path.slug ? `/articles/${encodeURIComponent(path.slug)}` : ""}`;
    fetch(url, { credentials: "include", signal: controller.signal }).then(async response => {
      const result = await response.json().catch(() => ({})) as { project?: Project; article?: Article; error?: string };
      if (!response.ok || !result.project) throw new Error(response.status === 404 ? "This story is not available. It may still be a draft or its publication may have been removed." : result.error || "The publication could not be opened. Please try again.");
      setProject(result.project); setArticle(result.article || null);
      document.title = `${result.article ? `${result.article.title} — ` : ""}${result.project.name} | The Journal`;
    }).catch(err => { if (!controller.signal.aborted) setError((err as Error).message); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [path]);

  if (loading) return <div className="session-loading"><span>THE JOURNAL</span><Loader2 size={22} className="journal-spin" /><p>Opening the publication…</p></div>;
  if (error || !project || !path) return <main className="reader-error"><p className="journal-eyebrow">THE JOURNAL</p><h1>A page yet to be found.</h1><p>{error || "This publication is not available."}</p><a className="journal-button secondary" href={path?.slug ? `/blog/${encodeURIComponent(path.id)}` : "/"}><ArrowLeft size={16} />{path?.slug ? "Back to the journal" : "Open the studio"}</a></main>;
  const accent = /^#[0-9a-f]{6}$/i.test(project.color) ? project.color : "#1B2A4A";
  const home = `/blog/${encodeURIComponent(project.id)}`;
  return <div className="public-journal" style={{ "--publication-accent": accent, "--publication-text-accent": readableAccent(accent) } as CSSProperties}><header className="reader-header"><a className="reader-brand" href={home}>{project.logo ? <img src={project.logo} alt="" /> : <span className="reader-monogram">{project.name.charAt(0).toUpperCase()}</span>}<span>{project.name}</span></a><a href={home} className="reader-journal-link">THE JOURNAL</a></header><main className={article ? "reader-article-main" : "reader-index-main"}>{article ? <><a className="reader-back" href={home}><ArrowLeft size={15} />All stories</a><article className="reader-article"><header><p className="reader-kicker">{article.tag || "IDEAS & PERSPECTIVES"}</p><h1>{article.title}</h1><div className="reader-byline">{project.logo ? <img src={project.logo} alt="" /> : <span className="reader-author-initial">{project.name.charAt(0).toUpperCase()}</span>}<div><strong>{project.name}</strong><span>{publishDate(article.updatedAt)}<i>·</i>{Math.max(1, Math.ceil(article.wordCount / 220))} min read<i>·</i>{article.wordCount.toLocaleString()} words</span></div></div></header><ArticleMarkdown content={article.content} dropCap={article.dropCap} /></article><div className="reader-article-end"><span>—</span><p>A story from {project.name}.</p><a href={home}>Explore the journal<ArrowRight size={15} /></a></div></> : <><div className="reader-index-heading"><p className="reader-kicker">THE JOURNAL</p><h1>Ideas with<br /><em>something to say.</em></h1><p>Stories, perspectives and useful thoughts from {project.name}.</p></div><div className="reader-index-meta"><span>{project.articles.length} {project.articles.length === 1 ? "STORY" : "STORIES"}</span><span>A PUBLICATION BY {project.name.toUpperCase()}</span></div>{project.articles.length ? <div className="reader-story-grid">{project.articles.map((story, index) => <article key={story.id} className="reader-story-card"><span className="reader-story-number">{String(index + 1).padStart(2, "0")}</span><div><p className="reader-kicker">{story.tag || "FROM THE JOURNAL"}</p><h2><a href={`${home}/${encodeURIComponent(story.slug)}`}>{story.title}</a></h2><p className="reader-story-excerpt">{story.content.replace(/[#*_`>]/g, "").split(/\n\s*\n/).find(block => block.trim())?.slice(0, 200)}…</p><footer><span>{Math.max(1, Math.ceil(story.wordCount / 220))} min read · {publishDate(story.updatedAt)}</span><a href={`${home}/${encodeURIComponent(story.slug)}`} aria-label={`Read ${story.title}`}><ArrowRight size={19} /></a></footer></div></article>)}</div> : <div className="reader-no-stories"><h2>A new edition is on its way.</h2><p>There are no published stories in this journal yet.</p></div>}</>}</main><footer className="reader-footer"><a href={home}>{project.name}</a><span>STORIES WITH SUBSTANCE.</span><span>© {new Date().getFullYear()} {project.name}</span></footer></div>;
}
