import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Check, ChevronDown, Download, ExternalLink, FileText, FolderOpen, Globe, Loader2, MoreHorizontal, Pause, Plus, RefreshCw, Save, Search, Settings2, Trash2, Upload, X } from "lucide-react";
import type { Article, Project, ProjectSummary, User } from "@/types";
import TopicComposer, { type TopicInput } from "@/components/TopicComposer";
import ArticleStudio from "@/components/ArticleStudio";
import AuthPortal from "@/components/AuthPortal";
import PublicReader from "@/components/PublicReader";
import { canLeavePublication, publicationHasChanges, runExclusiveAction, warnBeforeUnload } from "@/lib/publication-work";

type View = "articles" | "publications" | "topics" | "settings";
type Brand = Pick<Project, "name" | "logo" | "color" | "audience" | "tone">;
type WritingUsage = { reservedNeurons: number; limitNeurons: number; remainingNeurons: number; resetsAt: string };
type WritingStatus = { configured: boolean; provider: "cloudflare" | null; status: "configured" | "setup_required" | "daily_limit" | "unavailable"; cloudflare: { configured: boolean; setupRequired: boolean; freeTierConfirmed: boolean; status: string; model: string; usage: WritingUsage | null }; groq: { configured: boolean }; checkedAt: string };
type Health = { worker: { status: string; durationMs: number }; database: { status: string; durationMs: number }; groq: { status: string }; writer?: { provider: "cloudflare" | "groq" | null; status: string }; checkedAt: string };
type StreamPhase = "writing" | "validating" | "revising";
const initialBrand: Brand = { name: "", logo: null, color: "#1B2A4A", audience: "", tone: "Warm & approachable" };
const initialTopics = (): TopicInput[] => [1, 2, 3].map(i => ({ id: `new-${i}`, title: "", tag: "", constraints: "" }));

class RequestError extends Error { constructor(message: string, public status: number) { super(message); } }
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...init, credentials: "include", headers: { "Content-Type": "application/json", ...init.headers } });
  const result = await response.json().catch(() => ({})) as { error?: string | { message?: string }; message?: string };
  if (!response.ok) throw new RequestError(typeof result.error === "string" ? result.error : result.error?.message || result.message || "The request could not be completed. Please try again.", response.status);
  return result as T;
}
function timestamp(value?: string) {
  if (!value) return "Not saved";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not available" : date.toLocaleString("en", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
function statusClass(status: string) { return ["ok", "healthy", "operational", "connected", "up"].includes(status) ? "is-good" : ["invalid", "unavailable", "error", "failed", "rate_limited", "daily_limit"].includes(status) ? "is-error" : "is-muted"; }
function readableKeyError(message: string) { return /sign in|signed in|sign-in/i.test(message); }
function utcReset(value?: string) { if (!value || Number.isNaN(Date.parse(value))) return "the next daily reset"; return `${new Date(value).toLocaleString("en-GB", { timeZone: "UTC", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false })} UTC`; }
function sharedAvailable(status: WritingStatus | null) { return !!status?.configured && status.provider === "cloudflare" && status.status === "configured" && !!status.cloudflare.freeTierConfirmed && !!status.cloudflare.usage && status.cloudflare.usage.remainingNeurons >= 300; }
function sharedPaused(status: WritingStatus | null) { return status?.status === "daily_limit" || !!status?.cloudflare.usage && status.cloudflare.usage.remainingNeurons < 300; }

function Workspace({ user, onLogout, onSessionExpired }: { user: User; onLogout: () => Promise<void>; onSessionExpired: () => void }) {
  const [view, setView] = useState<View>("articles");
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [brand, setBrand] = useState<Brand>(initialBrand);
  const [topics, setTopics] = useState<TopicInput[]>(initialTopics);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [keyInput, setKeyInput] = useState("");
  const [writingStatus, setWritingStatus] = useState<WritingStatus | null>(null);
  const [writingStatusError, setWritingStatusError] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthChecking, setHealthChecking] = useState(false);
  const [healthError, setHealthError] = useState(false);
  const [roundTrip, setRoundTrip] = useState<number | null>(null);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [editing, setEditing] = useState<Article | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ProjectSummary | null>(null);
  const [connectionModal, setConnectionModal] = useState(false);
  const [returnView, setReturnView] = useState<View | null>(null);
  const [generating, setGenerating] = useState<string | null>(null);
  const [streamText, setStreamText] = useState("");
  const [phase, setPhase] = useState<StreamPhase>("writing");
  const [pauseRequested, setPauseRequested] = useState(false);
  const [dragLogo, setDragLogo] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const pause = useRef(false);
  const keyRef = useRef("");
  const configuredRef = useRef(false);
  const healthRunning = useRef(false);
  const draftOperation = useRef(false);
  const serverConfigured = !writingStatusError && sharedAvailable(writingStatus);
  const managedPaused = sharedPaused(writingStatus);
  const managedReset = utcReset(writingStatus?.cloudflare.usage?.resetsAt);
  const writingUnavailableReason = managedPaused ? `Shared writing is paused until ${managedReset}. Your drafts and edits are safe.` : writingStatusError || writingStatus?.status === "unavailable" ? "Shared writing is temporarily unavailable. Your drafts and edits are safe; check the studio settings for its current status." : "The site owner needs to connect shared free writing. Visitors do not need a key. Your drafts and edits will stay here.";
  const working = busy || !!generating;
  const brandLocked = !!project?.articles.some(a => a.status === "published");
  const topicsLocked = !!project?.articles.some(a => !!a.content || a.status === "published") || working;
  const ready = project?.articles.filter(a => a.status === "ready" || a.status === "published").length || 0;
  const published = project?.articles.filter(a => a.status === "published").length || 0;
  const cleanTopics = topics.filter(topic => topic.title.trim());
  const dirty = publicationHasChanges(brand, topics, project, initialBrand);

  function failure(err: unknown) {
    const message = err instanceof Error ? err.message : "Something went wrong. Please try again.";
    setError(message);
    if ((err as RequestError).status === 401 && readableKeyError(message)) onSessionExpired();
  }
  function syncProject(next: Project) {
    setProject(next);
    setProjects(current => current.some(item => item.id === next.id) ? current.map(item => item.id === next.id ? next : item) : [next, ...current]);
  }
  function hydrate(next: Project) {
    syncProject(next);
    setBrand({ name: next.name, logo: next.logo, color: next.color, audience: next.audience, tone: next.tone });
    setTopics(next.articles.map(a => ({ id: a.id, title: a.title, tag: a.tag || "", constraints: a.constraints || "" })));
  }
  const refreshWritingStatus = useCallback(async (): Promise<WritingStatus | null> => {
    try {
      const next = await request<WritingStatus>("/api/writing-status");
      setWritingStatus(next); setWritingStatusError(false);
      configuredRef.current = next.configured && next.provider === "cloudflare";
      return next;
    } catch { setWritingStatusError(true); configuredRef.current = false; return null; }
  }, []);
  const checkHealth = useCallback(async (testConnection = false, overrideKey?: string, announce = false) => {
    if (healthRunning.current) return;
    if (testConnection && overrideKey !== undefined) { keyRef.current = overrideKey; setApiKey(overrideKey); }
    healthRunning.current = true;
    setHealthChecking(true);
    const start = performance.now();
    try {
      let next = await request<Health>("/api/health");
      setRoundTrip(Math.round(performance.now() - start));
      if (testConnection) {
        try {
          const probe = await request<Health>("/api/health", { method: "POST", body: JSON.stringify({ apiKey: overrideKey === undefined ? keyRef.current : overrideKey }) });
          next = { ...next, groq: probe.groq, writer: probe.writer, checkedAt: probe.checkedAt };
        } catch {
          next = { ...next, ...(keyRef.current ? { groq: { status: "unavailable" } } : { writer: { provider: "cloudflare" as const, status: "unavailable" } }), checkedAt: new Date().toISOString() };
        }
      }
      setHealth(next); setHealthError(false);
      if (announce && testConnection && (keyRef.current ? next.groq.status === "connected" : next.writer?.status === "connected")) setToast("Writing connection checked successfully.");
    } catch { setRoundTrip(Math.round(performance.now() - start)); setHealthError(true); }
    finally { healthRunning.current = false; setHealthChecking(false); await refreshWritingStatus(); }
  }, [refreshWritingStatus]);

  useEffect(() => {
    request<{ projects: ProjectSummary[] }>("/api/projects").then(result => { setProjects(result.projects); }).catch(failure).finally(() => setLoading(false));
    checkHealth();
    const timer = setInterval(() => { if (document.visibilityState === "visible") checkHealth(!!keyRef.current || configuredRef.current); }, 60000);
    return () => clearInterval(timer);
  }, [checkHealth]);
  useEffect(() => { if (toast) { const timer = setTimeout(() => setToast(""), 4500); return () => clearTimeout(timer); } }, [toast]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => warnBeforeUnload(dirty, event);
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => {
    if (!deleteTarget && !connectionModal) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) { setDeleteTarget(null); setConnectionModal(false); } };
    document.addEventListener("keydown", escape);
    return () => { document.body.style.overflow = previous; document.removeEventListener("keydown", escape); };
  }, [deleteTarget, connectionModal, busy]);

  function navigate(next: View) {
    if (working || draftOperation.current) return;
    if (editing) { if (next === "settings") setConnectionModal(true); return; }
    setView(next); setError("");
  }
  function confirmPublicationChange() {
    return canLeavePublication(dirty, () => window.confirm("You have unsaved brand or topic changes. Discard them and leave this publication?"));
  }
  function newPublication() {
    if (working || editing || draftOperation.current || !confirmPublicationChange()) return;
    setProject(null); setBrand(initialBrand); setTopics(initialTopics()); setError(""); setView("articles");
  }
  async function selectPublication(id: string) {
    if (id === "new") { newPublication(); return; }
    if (working || editing || draftOperation.current) return;
    if (id === project?.id) { setView("articles"); return; }
    if (!confirmPublicationChange()) return;
    setBusy(true); setError("");
    try { const result = await request<{ project: Project }>(`/api/projects/${id}`); hydrate(result.project); setView("articles"); }
    catch (err) { failure(err); }
    finally { setBusy(false); }
  }
  async function signOut() {
    if (working || editing || draftOperation.current || !confirmPublicationChange()) return;
    setBusy(true);
    try { await onLogout(); }
    catch (err) { failure(err); }
    finally { setBusy(false); }
  }
  function updateBrand<K extends keyof Brand>(key: K, value: Brand[K]) { setBrand(current => ({ ...current, [key]: value })); }
  async function uploadLogo(file?: File) {
    setDragLogo(false);
    if (!file || brandLocked) return;
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) return setError("Use a PNG, JPG or WebP logo.");
    if (file.size > 1048576) return setError("Your logo must be smaller than 1 MB.");
    const image = new Image(), url = URL.createObjectURL(file);
    image.onload = () => {
      const canvas = document.createElement("canvas"), scale = Math.min(1, 600 / Math.max(image.width, image.height));
      canvas.width = Math.round(image.width * scale); canvas.height = Math.round(image.height * scale);
      const context = canvas.getContext("2d");
      if (!context) { URL.revokeObjectURL(url); setError("The logo could not be processed. Try another image."); return; }
      context.drawImage(image, 0, 0, canvas.width, canvas.height);
      const encoded = canvas.toDataURL("image/webp", 0.88);
      URL.revokeObjectURL(url);
      if (encoded.length > 1300000) return setError("This logo is too detailed. Please choose a smaller image.");
      updateBrand("logo", encoded); setError("");
      if (fileInput.current) fileInput.current.value = "";
    };
    image.onerror = () => { URL.revokeObjectURL(url); setError("This image could not be opened. Please choose another file."); };
    image.src = url;
  }
  function validateDraft() {
    if (!brand.name.trim()) throw new Error("Enter your company name before saving.");
    if (!/^#[0-9a-f]{6}$/i.test(brand.color)) throw new Error("Enter a six-digit brand colour, such as #1B2A4A.");
    if (!cleanTopics.length) throw new Error("Add at least one blog topic.");
    if (project && cleanTopics.length !== topics.length) throw new Error("Each saved topic needs a title.");
    if (new Set(cleanTopics.map(t => t.title.trim().toLowerCase())).size !== cleanTopics.length) throw new Error("Use a different title for each topic.");
  }
  async function persistDraft(): Promise<Project | null> {
    setError("");
    try { validateDraft(); } catch (err) { failure(err); return null; }
    try {
      let next: Project;
      if (!project) {
        const result = await request<{ project: Project }>("/api/projects", { method: "POST", body: JSON.stringify({ ...brand, name: brand.name.trim(), topics: cleanTopics.map(({ title, tag, constraints }) => ({ title: title.trim(), tag, constraints })) }) });
        next = result.project;
      } else {
        next = project;
        if (!brandLocked) next = (await request<{ project: Project }>(`/api/projects/${project.id}`, { method: "PATCH", body: JSON.stringify(brand) })).project;
        if (!topicsLocked) next = (await request<{ project: Project }>(`/api/projects/${project.id}/topics`, { method: "PATCH", body: JSON.stringify({ articles: topics.map(({ id, title, tag, constraints }) => ({ id, title: title.trim(), tag, constraints })) }) })).project;
      }
      hydrate(next); setToast("Publication draft saved."); return next;
    } catch (err) { failure(err); return null; }
  }
  async function runDraftOperation<T>(action: () => Promise<T>): Promise<T | undefined> {
    return runExclusiveAction(draftOperation, async () => {
      setBusy(true);
      try { return await action(); }
      finally { setBusy(false); setGenerating(null); setStreamText(""); }
    });
  }
  async function saveDraft(): Promise<Project | null> {
    return (await runDraftOperation(persistDraft)) ?? null;
  }
  async function streamArticle(current: Project, article: Article): Promise<Article> {
    const response = await fetch(`/api/projects/${current.id}/articles/${article.id}/stream`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ apiKey: keyRef.current }) });
    if (!response.ok) { const result = await response.json().catch(() => ({})) as { error?: string }; throw new RequestError(result.error || "The writing connection could not start. Please retry.", response.status); }
    if (!response.body) throw new Error("Your browser could not open the article stream.");
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "", completed: Article | null = null;
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        buffer += decoder.decode(part.value, { stream: true });
        buffer = buffer.replace(/\r\n/g, "\n");
        let boundary: number;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
          const event = frame.split("\n").find(line => line.startsWith("event:"))?.slice(6).trim();
          const data = frame.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
          if (!data) continue;
          const payload = JSON.parse(data) as { phase?: StreamPhase; text?: string; article?: Article; error?: string };
          if (event === "status" && payload.phase) { setPhase(payload.phase); if (payload.phase === "revising") setStreamText(""); }
          if (event === "delta" && payload.text) setStreamText(text => text + payload.text);
          if (event === "complete" && payload.article) completed = payload.article;
          if (event === "error") throw new Error(payload.error || "Writing was interrupted. Your saved articles are safe.");
        }
      }
    } finally { reader.releaseLock(); }
    if (!completed) throw new Error("Writing was interrupted before the article was saved. Please retry this topic.");
    return completed;
  }
  async function generateDrafts(onlyId?: string) {
    try {
      await runDraftOperation(async () => {
        if (!apiKey.trim()) {
          const latest = await refreshWritingStatus();
          if (!sharedAvailable(latest)) {
            setReturnView(view); setView("settings");
            setError(sharedPaused(latest) ? `Shared writing is paused until ${utcReset(latest?.cloudflare.usage?.resetsAt)}. Your drafts are safe.` : "Shared free writing is not ready yet. The site owner can finish setup; visitors do not need a key.");
            return;
          }
        }
        const saved = await persistDraft();
        if (!saved) return;
        const pending = saved.articles.filter(article => onlyId ? article.id === onlyId : article.status !== "ready" && article.status !== "published");
        if (!pending.length) { setToast("All articles already have saved drafts."); return; }
        let current = saved;
        pause.current = false; setPauseRequested(false); setError(""); setView("articles");
        let failed = false;
        for (const article of pending) {
          if (pause.current) break;
          setGenerating(article.id); setStreamText(""); setPhase("writing");
          try {
            const next = await streamArticle(current, article);
            current = { ...current, updatedAt: next.updatedAt || current.updatedAt, articles: current.articles.map(item => item.id === article.id ? next : item) };
            syncProject(current);
          } catch (err) {
            failed = true; failure(err);
            try { const result = await request<{ project: Project }>(`/api/projects/${current.id}`); current = result.project; syncProject(current); } catch { /* The original connection error remains visible. */ }
            break;
          } finally {
            const latest = await refreshWritingStatus();
            if (!keyRef.current && !sharedAvailable(latest)) pause.current = true;
          }
        }
        const finalStatus = await refreshWritingStatus();
        if (!failed) setToast(!keyRef.current && sharedPaused(finalStatus) ? `Shared writing is paused until ${utcReset(finalStatus?.cloudflare.usage?.resetsAt)}. Finished articles are saved.` : pause.current ? "Writing paused. Finished articles are saved." : "Your article drafts are ready to review.");
      });
    } catch (err) { failure(err); }
  }
  async function publishArticle(article: Article) {
    if (!project) return;
    setBusy(true); setError("");
    try { const result = await request<{ project: Project }>(`/api/projects/${project.id}/articles/${article.id}/publish`, { method: "POST", body: "{}" }); syncProject(result.project); setToast("Article published."); }
    catch (err) { failure(err); }
    finally { setBusy(false); }
  }
  async function publishCollection() {
    if (!project) return;
    setBusy(true); setError("");
    try { const result = await request<{ project: Project }>(`/api/projects/${project.id}/publish`, { method: "POST", body: "{}" }); syncProject(result.project); setToast("Your publication is live."); }
    catch (err) { failure(err); }
    finally { setBusy(false); }
  }
  function download(article: Article) {
    const url = URL.createObjectURL(new Blob([`# ${article.title}\n\n${article.content}`], { type: "text/markdown;charset=utf-8" }));
    const link = document.createElement("a"); link.href = url; link.download = `${article.slug}.md`; link.click(); URL.revokeObjectURL(url);
  }
  async function deletePublication() {
    if (!deleteTarget) return;
    setBusy(true); setError("");
    try { await request(`/api/projects/${deleteTarget.id}`, { method: "DELETE", body: "{}" }); setProjects(items => items.filter(item => item.id !== deleteTarget.id)); if (project?.id === deleteTarget.id) { setProject(null); setBrand(initialBrand); setTopics(initialTopics()); } setDeleteTarget(null); setToast("Publication deleted."); }
    catch (err) { failure(err); }
    finally { setBusy(false); }
  }
  function applyKey() {
    const key = keyInput.trim(), alreadyChecked = key === keyRef.current && health?.groq.status === "connected";
    setApiKey(key); keyRef.current = key;
    if (!alreadyChecked) setHealth(current => current ? { ...current, groq: { status: key ? "not_checked" : "not_configured" } } : current);
    setToast(alreadyChecked ? "Verified personal Groq override ready for this visit." : key ? "Personal Groq override selected for this visit. Test it to check the connection." : "Personal override removed. Shared writing is selected.");
    setConnectionModal(false);
    if (returnView) { setView(returnView); setReturnView(null); }
  }
  const personalStatus = health?.groq.status || "not_checked";
  const sharedHealthStatus = health?.writer?.provider === "cloudflare" ? health.writer.status : null;
  const providerStatus = apiKey ? personalStatus : writingStatusError ? "unavailable" : managedPaused ? "daily_limit" : ["invalid", "unavailable", "rate_limited"].includes(sharedHealthStatus || "") ? sharedHealthStatus! : sharedHealthStatus === "connected" && serverConfigured ? "connected" : writingStatus?.status || "not_checked";
  const providerLabel = apiKey ? personalStatus === "connected" ? "Personal Groq connected" : personalStatus === "invalid" ? "Personal Groq key invalid" : personalStatus === "rate_limited" ? "Personal Groq rate limited" : personalStatus === "unavailable" ? "Personal Groq unavailable" : "Personal Groq not checked" : providerStatus === "daily_limit" ? "Shared writing paused" : providerStatus === "connected" ? "Shared writing connected" : providerStatus === "configured" ? "Shared writing ready" : providerStatus === "setup_required" ? "Shared writing needs owner setup" : providerStatus === "invalid" ? "Shared writing needs attention" : providerStatus === "rate_limited" ? "Shared writing rate limited" : providerStatus === "unavailable" ? "Shared writing unavailable" : "Checking shared writing";
  const rows: Article[] = project ? project.articles.map(article => { const topic = topics.find(item => item.id === article.id); return !article.content && topic ? { ...article, title: topic.title, tag: topic.tag, constraints: topic.constraints } : article; }) : cleanTopics.map(topic => ({ ...topic, slug: "", content: "", status: "draft" as const, wordCount: 0, error: null, updatedAt: "", dropCap: false }));
  const filteredProjects = projects.filter(item => item.name.toLowerCase().includes(search.toLowerCase()) && (filter === "all" || filter === "published" && item.articles.some(a => a.status === "published") || filter === "draft" && item.status === "draft"));
  const usage = writingStatus?.cloudflare.usage;
  const budgetSummary = <div className="journal-writing-budget" aria-live="polite">{usage ? <><div className="journal-budget-numbers"><span><strong>{usage.remainingNeurons.toLocaleString()}</strong> neurons remaining</span><span>{usage.reservedNeurons.toLocaleString()} / {usage.limitNeurons.toLocaleString()} reserved</span></div><progress value={Math.min(usage.limitNeurons, usage.reservedNeurons)} max={usage.limitNeurons} aria-label="Shared daily writing reservations" /><p>Shared daily allowance · Resets {utcReset(usage.resetsAt)}</p></> : <p>{writingStatusError || writingStatus?.status === "unavailable" ? "The shared allowance could not be checked. Writing stays paused until its status is available." : writingStatus?.status === "setup_required" ? "The daily allowance appears after the owner connects shared writing." : "Checking the shared daily allowance…"}</p>}</div>;
  const sharedWritingDetails = <><div className="journal-connection-panel"><span className={"journal-health-dot " + (serverConfigured ? "is-good" : managedPaused ? "is-error" : "is-muted")} /><div><strong>{serverConfigured ? "Free writing, shared by this studio" : managedPaused ? "Shared writing is paused for today" : writingStatusError || writingStatus?.status === "unavailable" ? "Shared writing is temporarily unavailable" : writingStatus ? "The site owner needs to finish setup" : "Checking shared writing…"}</strong><p>{serverConfigured ? "Articles and editorial polish use this studio’s Cloudflare allowance. Visitors do not need a key." : managedPaused ? "Finished articles and edits are safe. Writing resumes at the daily UTC reset." : "Visitors do not need to enter credentials. The owner connects the shared writing service once for everyone."}</p></div></div>{budgetSummary}{writingStatus?.status === "setup_required" && <details className="journal-owner-setup"><summary>For the site owner</summary><ol><li>Use a Cloudflare Workers Free account and confirm that the account is on the free tier.</li><li>Set <code>CLOUDFLARE_ACCOUNT_ID</code> and <code>CLOUDFLARE_API_TOKEN</code> in the backend environment. The token needs Workers AI Read and Edit permissions.</li><li>Set <code>CLOUDFLARE_AI_FREE_TIER_CONFIRMED</code> to <code>true</code>, then restart the backend.</li></ol><p>Keep account credentials in server settings. They are never entered on this visitor page.</p></details>}<div className="journal-shared-writing-actions"><button className="journal-button secondary" disabled={healthChecking} onClick={() => checkHealth(!!apiKey || !!writingStatus?.configured, undefined, true)}>{healthChecking ? <Loader2 size={15} className="journal-spin" /> : <RefreshCw size={15} />}Check writing status</button></div></>;
  const connectionFields = <>{sharedWritingDetails}<details className="journal-personal-override"><summary>Optional personal Groq override{apiKey && <span className="journal-chip">Selected</span>}</summary><p className="journal-help-text">Shared writing is the default. Select a personal Groq key only if you want this visit to use your own Groq allowance instead.</p><div className="journal-field"><label htmlFor={connectionModal ? "connection-key" : "settings-key"}>Personal Groq API key</label><input id={connectionModal ? "connection-key" : "settings-key"} type="password" autoComplete="off" spellCheck={false} value={keyInput} placeholder="gsk_…" onChange={event => setKeyInput(event.target.value)} /><p className="journal-help-text">Kept only in memory for this visit. Never saved in your publication or browser storage.</p></div><div className="journal-connection-actions"><a className="journal-text-link" href="https://console.groq.com/keys" target="_blank" rel="noopener noreferrer">Get a Groq key <ExternalLink size={14} /></a><button className="journal-button secondary" disabled={healthChecking || !keyInput.trim()} onClick={() => checkHealth(true, keyInput.trim(), true)}>{healthChecking ? <Loader2 size={15} className="journal-spin" /> : <RefreshCw size={15} />}Test & select override</button><button className="journal-button primary" onClick={applyKey} disabled={!keyInput.trim()}><Check size={15} />Select this override</button>{apiKey && <button className="journal-button secondary" onClick={() => { setApiKey(""); setKeyInput(""); keyRef.current = ""; setToast("Personal override removed. Shared writing is selected."); checkHealth(false); }}>Use shared writing</button>}</div><p className="journal-help-text">{apiKey ? "This visit explicitly uses your personal Groq key. The shared Cloudflare allowance is unchanged by Groq requests." : "No personal override is selected."}</p></details></>;
  const sharedWritingCard = <section className="journal-card journal-shared-writing-card"><div className="journal-card-heading"><h2>Shared free writing</h2><span className={"journal-badge " + (serverConfigured ? "ready" : "draft")}>{serverConfigured ? "Ready" : managedPaused ? "Paused" : writingStatusError || writingStatus?.status === "unavailable" ? "Unavailable" : writingStatus ? "Setup needed" : "Checking"}</span></div>{budgetSummary}<p className="journal-help-text">{apiKey ? "Personal Groq override selected for this visit." : managedPaused ? "Writing pauses until the UTC reset. Your saved work stays here." : serverConfigured ? "Included for every visitor. No personal key needed." : "The owner connects writing once. Visitors do not need a key."}</p><button className="journal-text-link" onClick={() => navigate("settings")}>Writing details <ArrowRight size={14} /></button></section>;

  const brandCard = <section className="journal-card"><div className="journal-card-heading"><h2>Brand essentials</h2><span className={`journal-chip ${project ? "is-saved" : ""}`}>{project ? dirty ? "Unsaved changes" : "Saved" : "New publication"}</span></div><div className="journal-brand-fields"><div className={`journal-logo-upload ${dragLogo ? "is-dragging" : ""}`} onDragOver={event => { if (!brandLocked && !working) { event.preventDefault(); setDragLogo(true); } }} onDragLeave={() => setDragLogo(false)} onDrop={event => { event.preventDefault(); uploadLogo(event.dataTransfer.files[0]); }}><input ref={fileInput} className="journal-sr-only" type="file" id="brand-logo" accept="image/png,image/jpeg,image/webp" disabled={brandLocked || working} onChange={event => uploadLogo(event.target.files?.[0])} />{brand.logo ? <img src={brand.logo} alt="Company logo" /> : <span className="journal-logo-placeholder"><Upload size={22} /></span>}<div><button className="journal-text-link" disabled={brandLocked || working} onClick={() => fileInput.current?.click()}>{brand.logo ? "Change logo" : "Upload your logo"}</button><p>PNG, JPG or WebP · Up to 1 MB</p></div>{brand.logo && <button className="journal-icon-button" disabled={brandLocked || working} onClick={() => updateBrand("logo", null)} aria-label="Remove company logo"><X size={16} /></button>}</div><div className="journal-field"><label htmlFor="company-name">Company name</label><input id="company-name" autoComplete="organization" value={brand.name} disabled={brandLocked || working} maxLength={80} placeholder="e.g. Monday Studio" onChange={event => updateBrand("name", event.target.value)} /></div><div className="journal-field"><label htmlFor="brand-color">Brand colour</label><div className="journal-color-field"><input type="color" value={/^#[0-9a-f]{6}$/i.test(brand.color) ? brand.color : "#1B2A4A"} disabled={brandLocked || working} aria-label="Choose your brand colour" onChange={event => updateBrand("color", event.target.value.toUpperCase())} /><input id="brand-color" value={brand.color} disabled={brandLocked || working} maxLength={7} spellCheck={false} placeholder="#1B2A4A" onChange={event => updateBrand("color", event.target.value.toUpperCase())} /></div></div><details className="journal-brand-options"><summary>Audience & editorial voice <ChevronDown size={14} /></summary><div className="journal-field"><label htmlFor="audience">Who are you writing for? <span>Optional</span></label><input id="audience" value={brand.audience} disabled={brandLocked || working} maxLength={200} placeholder="e.g. Independent business owners" onChange={event => updateBrand("audience", event.target.value)} /></div><div className="journal-field"><label htmlFor="tone">Editorial voice</label><select id="tone" value={brand.tone} disabled={brandLocked || working} onChange={event => updateBrand("tone", event.target.value)}>{["Warm & approachable", "Clear & professional", "Thoughtful & editorial", "Bold & conversational"].map(tone => <option key={tone}>{tone}</option>)}</select></div></details>{brandLocked && <p className="journal-help-text">Brand details are fixed after an article is published.</p>}<div className="journal-save-state"><span className={`journal-health-dot ${healthError ? "is-error" : statusClass(health?.database.status || "unknown")}`} /><span>{healthError ? "Database status unavailable" : health && statusClass(health.database.status) === "is-good" ? "Publication storage available" : health ? "Publication storage unavailable" : "Checking publication storage…"}</span></div></div></section>;
  const topicCard = <section className="journal-card"><div className="journal-card-heading"><h2>Topic input matrix</h2><span className="journal-chip">{cleanTopics.length} / 10</span></div><p className="journal-card-description">One topic, one article. Drag to reorder or use the arrows.</p><TopicComposer topics={topics} onChange={setTopics} locked={topicsLocked} existing={!!project} /><div className="journal-composer-actions"><button className="journal-button primary" disabled={working || !cleanTopics.length || project?.status === "published" || !apiKey && managedPaused} onClick={() => generateDrafts()}>{generating ? <Loader2 size={16} className="journal-spin" /> : <FileText size={16} />}{!apiKey && managedPaused ? "Shared writing paused" : "Generate Article Drafts"}<ArrowRight size={16} /></button><button className="journal-button secondary" disabled={working || !dirty || brandLocked} onClick={saveDraft}>{busy ? <Loader2 size={15} className="journal-spin" /> : <Save size={15} />}Save publication draft</button><p className="journal-help-text">Aiming for ~1,000 words per article. Review every draft before publishing.</p></div></section>;

  return <div className="journal-app"><header className="journal-header"><div className="journal-header-brand"><button className="journal-wordmark" onClick={() => navigate("articles")} aria-label="Stova editorial desk">STOVA<span>•</span></button><span className="journal-tagline">THE INDEPENDENT PUBLISHING STUDIO</span></div><div className="journal-project-switch"><label htmlFor="publication-select">PUBLICATION</label><div><select id="publication-select" value={project?.id || "new"} disabled={working || !!editing || loading} onChange={event => selectPublication(event.target.value)}><option value="new">New publication</option>{projects.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select><ChevronDown size={14} /></div></div><div className="journal-session-control"><span title={user.email}>{user.name}</span><button type="button" disabled={working || !!editing} onClick={signOut}>Sign out</button></div></header><nav className="journal-nav" aria-label="Workspace sections">{([['articles', 'Articles', FileText], ['publications', 'Publications', FolderOpen], ['topics', 'Topics', Settings2], ['settings', 'Settings', Settings2]] as const).map(([id, label, Icon]) => <button key={id} className={view === id && !editing ? "is-active" : ""} disabled={working || !!editing && id !== "settings"} onClick={() => navigate(id)}><Icon size={15} />{label}{id === "publications" && projects.length > 0 && <span>{projects.length}</span>}</button>)}<button className="journal-nav-new" disabled={working || !!editing} onClick={newPublication}><Plus size={15} />New publication</button></nav>
    <div className="journal-statusbar" aria-label="Service health"><button className={`journal-health-item ${statusClass(providerStatus)}`} onClick={() => editing ? setConnectionModal(true) : navigate("settings")}><span className="journal-health-dot" />{providerLabel}</button><span className={`journal-health-item ${healthError ? "is-error" : statusClass(health?.worker.status || "unknown")}`} title={health ? `API execution: ${health.worker.durationMs} ms` : undefined}><span className="journal-health-dot" />{healthError ? "API check unavailable" : roundTrip !== null && health ? `API ${roundTrip} ms` : "API not checked"}</span><span className={`journal-health-item ${healthError ? "is-error" : statusClass(health?.database.status || "unknown")}`}><span className="journal-health-dot" />{healthError ? "Database check unavailable" : health ? statusClass(health.database.status) === "is-good" ? `Database healthy · ${Math.round(health.database.durationMs)} ms` : "Database unavailable" : "Database not checked"}</span><button className="journal-health-refresh" disabled={healthChecking} onClick={() => checkHealth(!!apiKey || serverConfigured)} aria-label="Refresh service health"><RefreshCw size={13} className={healthChecking ? "journal-spin" : ""} /><span>{healthChecking ? "Checking…" : health ? `Checked ${timestamp(health.checkedAt)}` : "Check services"}</span></button></div>
    <main className="journal-main">{error && <div className="journal-alert is-error" role="alert"><span>{error}</span><button className="journal-icon-button" onClick={() => setError("")} aria-label="Dismiss error"><X size={16} /></button></div>}
      {editing && project ? <ArticleStudio project={project} article={editing} apiKey={apiKey} serverConfigured={serverConfigured} writingPaused={!apiKey && managedPaused} writingUnavailableReason={writingUnavailableReason} managedWritingLabel={managedPaused ? "Shared writing paused" : serverConfigured ? "Shared studio writing" : writingStatusError || writingStatus?.status === "unavailable" ? "Shared writing unavailable" : "Shared writing needs setup"} onWritingComplete={async () => { await refreshWritingStatus(); }} onClose={() => setEditing(null)} onSaved={article => { syncProject({ ...project, articles: project.articles.map(item => item.id === article.id ? article : item) }); setEditing(article); }} onPublished={next => { syncProject(next); const article = next.articles.find(item => item.id === editing.id); if (article) setEditing(article); setToast("Article published."); }} onNeedConnection={() => setConnectionModal(true)} /> : <>
      {view === "articles" && <><div className="journal-page-heading"><div><p className="journal-eyebrow">{project ? project.name : "YOUR WORKSPACE"}</p><h1>The editorial desk.</h1><p>Your brand, your topics, and a clear path from draft to published.</p></div><div className="journal-heading-actions">{project && published > 0 && <a className="journal-button secondary" href={`/blog/${project.id}`} target="_blank" rel="noopener noreferrer">View publication<ExternalLink size={15} /></a>}</div></div><div className="journal-workspace"><aside className="journal-composer">{brandCard}{sharedWritingCard}{topicCard}</aside><section className="journal-pipeline"><div className="journal-pipeline-toolbar"><div><h2>Article pipeline</h2><p>{project ? `${ready} of ${project.articles.length} drafts ready · ${published} published` : "Your topics will appear here as you add them."}</p></div><div>{generating ? <button className="journal-button secondary" disabled={pauseRequested} onClick={() => { pause.current = true; setPauseRequested(true); }}><Pause size={15} />{pauseRequested ? "Pausing after this article" : "Pause writing"}</button> : project && ready === project.articles.length && published < project.articles.length ? <button className="journal-button primary" disabled={working} onClick={publishCollection}><Globe size={15} />Publish all ready</button> : null}</div></div>{rows.length ? <div className="journal-table-scroll"><table className="journal-table"><thead><tr><th scope="col" className="journal-order-column">No.</th><th scope="col">ARTICLE / TOPIC</th><th scope="col">STATUS</th><th scope="col">WORDS</th><th scope="col">UPDATED</th><th scope="col"><span className="journal-sr-only">Actions</span></th></tr></thead><tbody>{rows.map((article, index) => <ArticleRow key={article.id} article={article} index={index} saved={!!project} generating={generating === article.id} phase={phase} streamText={streamText} busy={working} writingPaused={!apiKey && managedPaused} onEdit={() => { const saved = project?.articles.find(item => item.id === article.id); if (saved) { setEditing(saved); setError(""); } }} onDownload={() => download(article)} onPublish={() => publishArticle(article)} onGenerate={() => generateDrafts(article.id)} />)}</tbody></table></div> : <div className="journal-empty"><span className="journal-empty-icon"><FileText size={33} /></span><h3>Your next edition starts here.</h3><p>Add your brand details and a few topics. Each one becomes a separate article, ready for your review.</p><span className="journal-empty-rule" /><span>UP TO 10 TOPICS · ONE CONSISTENT PUBLICATION</span></div>}<div className="journal-pipeline-footer"><span><Save size={14} />{project ? `Last saved ${timestamp(project.updatedAt)}` : "Save a draft to keep your publication."}</span><span>Review first. Publish when ready.</span></div></section></div></>}
      {view === "topics" && <><div className="journal-page-heading"><div><p className="journal-eyebrow">EDITORIAL PLANNING</p><h1>Your topic list.</h1><p>Keep every article focused with tags and a few notes for the writer.</p></div><button className="journal-button secondary" onClick={() => setView("articles")}><ArrowLeft size={15} />Back to articles</button></div><div className="journal-topics-page">{topicCard}</div></>}
      {view === "publications" && <><div className="journal-page-heading"><div><p className="journal-eyebrow">THE LIBRARY</p><h1>Every brand, in one place.</h1><p>Keep separate publications for your clients, projects and ideas.</p></div><button className="journal-button primary" onClick={newPublication}><Plus size={16} />New publication</button></div><div className="journal-library-controls"><div className="journal-filter-tabs">{["all", "published", "draft"].map(value => <button key={value} className={filter === value ? "is-active" : ""} onClick={() => setFilter(value)}>{value === "all" ? "All publications" : value === "published" ? "Published" : "Drafts"}</button>)}</div><label className="journal-search"><Search size={16} /><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Find a publication" aria-label="Search publications" /></label></div>{loading ? <div className="journal-empty"><Loader2 size={24} className="journal-spin" /><p>Opening your publication library…</p></div> : filteredProjects.length ? <div className="journal-publication-grid">{filteredProjects.map(item => <article key={item.id} className="journal-publication-card"><div className="journal-publication-card-top"><span className="journal-publication-monogram" style={{ borderColor: item.color }}>{item.logo ? <img src={item.logo} alt="" loading="lazy" decoding="async" /> : item.name.charAt(0).toUpperCase()}</span><span className={`journal-badge ${item.status}`}>{item.status === "published" ? "Published" : item.articles.some(a => a.status === "published") ? "Partly published" : "Draft"}</span></div><h2>{item.name}</h2><p>{item.articles.length} {item.articles.length === 1 ? "article" : "articles"} · {item.articles.filter(a => a.status === "published").length} published</p><div className="journal-publication-color"><span style={{ background: item.color }} />{item.color}<span>Updated {timestamp(item.updatedAt)}</span></div><div className="journal-publication-card-actions"><button className="journal-button secondary" onClick={() => selectPublication(item.id)}>Open publication<ArrowRight size={15} /></button>{item.articles.some(a => a.status === "published") && <a className="journal-icon-button" href={`/blog/${item.id}`} target="_blank" rel="noopener noreferrer" aria-label={`View ${item.name}`}><ExternalLink size={16} /></a>}<button className="journal-icon-button" onClick={() => setDeleteTarget(item)} aria-label={`Delete ${item.name}`}><Trash2 size={16} /></button></div></article>)}</div> : <div className="journal-empty"><FolderOpen size={32} /><h3>{search || filter !== "all" ? "No matching publications." : "Your library is ready for its first edition."}</h3><p>{search || filter !== "all" ? "Try a different search or view all publications." : "Create a publication for your brand or a client. Your saved work will live here."}</p><button className="journal-button primary" onClick={search || filter !== "all" ? () => { setSearch(""); setFilter("all"); } : newPublication}>{search || filter !== "all" ? "View all publications" : "Create a publication"}<ArrowRight size={15} /></button></div>}</>}
      {view === "settings" && <><div className="journal-page-heading"><div><p className="journal-eyebrow">STUDIO SETTINGS</p><h1>Your writing connection.</h1><p>A real connection, checked when you need it.</p></div>{returnView && <button className="journal-button secondary" onClick={() => { setView(returnView); setReturnView(null); }}><ArrowLeft size={15} />Back to your draft</button>}</div><div className="journal-settings-grid"><section className="journal-card"><div className="journal-card-heading"><h2>Shared free writing</h2><span className={`journal-badge ${serverConfigured ? "ready" : "draft"}`}>{serverConfigured ? "Ready" : managedPaused ? "Paused" : "Setup"}</span></div>{connectionFields}</section><section className="journal-card"><div className="journal-card-heading"><h2>Workspace services</h2><button className="journal-icon-button" disabled={healthChecking} onClick={() => checkHealth(!!apiKey || serverConfigured)} aria-label="Refresh services"><RefreshCw size={16} className={healthChecking ? "journal-spin" : ""} /></button></div><dl className="journal-service-list"><div><dt>API round trip</dt><dd>{roundTrip === null ? "Not checked" : `${roundTrip} ms`}</dd></div><div><dt>Publication database</dt><dd>{healthError ? "Check unavailable" : health ? statusClass(health.database.status) === "is-good" ? `Healthy · ${Math.round(health.database.durationMs)} ms` : "Unavailable" : "Not checked"}</dd></div><div><dt>Writing connection</dt><dd>{providerLabel}</dd></div><div><dt>Last checked</dt><dd>{health ? timestamp(health.checkedAt) : "Not checked"}</dd></div></dl><p className="journal-help-text">Service status refreshes every minute while this page is visible. Times are measured from actual requests.</p><div className="journal-settings-note"><h3>One studio. More than one story.</h3><p>Each publication keeps its own logo, colour and article collection. Use the publication selector above to return to saved work.</p></div></section></div></>}
      </>}
    </main><footer className="journal-footer"><span>STOVA · INDEPENDENT PUBLISHING</span><span>Your words. Your brand. Your next edition.</span></footer>
    {toast && <div className="journal-toast" role="status"><Check size={16} /><span>{toast}</span><button className="journal-icon-button" onClick={() => setToast("")} aria-label="Dismiss notification"><X size={15} /></button></div>}
    {deleteTarget && <div className="journal-modal-backdrop"><section className="journal-modal" role="dialog" aria-modal="true" aria-labelledby="delete-title"><p className="journal-eyebrow">PUBLICATION LIBRARY</p><h2 id="delete-title">Delete this publication?</h2><p>“{deleteTarget.name}” and all its articles will be permanently removed. Published links will stop working.</p><div className="journal-modal-actions"><button autoFocus className="journal-button secondary" disabled={busy} onClick={() => setDeleteTarget(null)}>Keep publication</button><button className="journal-button danger" disabled={busy} onClick={deletePublication}>{busy ? <Loader2 size={15} className="journal-spin" /> : <Trash2 size={15} />}Delete publication</button></div></section></div>}
    {connectionModal && <div className="journal-modal-backdrop"><section className="journal-modal journal-connection-modal" role="dialog" aria-modal="true" aria-labelledby="connection-title"><div className="journal-card-heading"><h2 id="connection-title">Writing connection</h2><button autoFocus className="journal-icon-button" onClick={() => setConnectionModal(false)} aria-label="Close writing connection"><X size={18} /></button></div>{connectionFields}</section></div>}
  </div>;
}

function ArticleRow({ article, index, saved, generating, phase, streamText, busy, writingPaused, onEdit, onDownload, onPublish, onGenerate }: { article: Article; index: number; saved: boolean; generating: boolean; phase: StreamPhase; streamText: string; busy: boolean; writingPaused: boolean; onEdit: () => void; onDownload: () => void; onPublish: () => void; onGenerate: () => void }) {
  const status = generating ? "writing" : !saved ? "queued" : article.status === "draft" && !article.content ? "queued" : article.status;
  return <><tr className={generating ? "is-streaming" : ""}><td className="journal-order-column">{String(index + 1).padStart(2, "0")}</td><td><button className="journal-article-title" disabled={!article.content || busy} onClick={onEdit}>{article.title || "Untitled topic"}</button>{article.tag && <span className="journal-topic-tag">{article.tag}</span>}{article.error && article.status === "error" && <p className="journal-row-error">{article.error}</p>}</td><td><span className={`journal-badge ${status}`}>{generating && <Loader2 size={11} className="journal-spin" />}{status === "ready" ? "Ready to review" : status === "queued" ? "Queued" : status === "writing" ? phase === "validating" ? "Validating" : phase === "revising" ? "Revising" : "Writing" : status === "error" ? "Needs retry" : status === "published" ? "Published" : "Draft"}</span></td><td>{article.wordCount ? article.wordCount.toLocaleString() : <span className="journal-muted">~1,000</span>}</td><td className="journal-updated-column">{saved ? timestamp(article.updatedAt) : "Not saved"}</td><td><details className="journal-row-menu"><summary aria-label={`Actions for ${article.title || `topic ${index + 1}`}`}><MoreHorizontal size={18} /></summary><div>{article.content ? <><button disabled={busy} onClick={event => { event.currentTarget.closest("details")?.removeAttribute("open"); onEdit(); }}><FileText size={14} />Edit / review</button><button disabled={busy} onClick={event => { event.currentTarget.closest("details")?.removeAttribute("open"); onDownload(); }}><Download size={14} />Download Markdown</button><button disabled={busy || article.status !== "ready"} onClick={event => { event.currentTarget.closest("details")?.removeAttribute("open"); onPublish(); }}><Globe size={14} />{article.status === "published" ? "Already published" : "Publish article"}</button></> : <button disabled={busy || !saved || writingPaused} onClick={event => { event.currentTarget.closest("details")?.removeAttribute("open"); onGenerate(); }}><FileText size={14} />{article.status === "error" ? "Retry article" : "Generate draft"}</button>}</div></details></td></tr>{generating && <tr className="journal-stream-row"><td colSpan={6}><div className="journal-stream"><div><span><Loader2 size={14} className="journal-spin" />{phase === "validating" ? "Checking length and formatting" : phase === "revising" ? "Refining the draft to match your brief" : "Writing your article"}</span><span>Live draft · saved when complete</span></div><pre aria-live="off">{streamText || "Waiting for the first words…"}</pre></div></td></tr>}</>;
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [sessionMessage, setSessionMessage] = useState("");
  const publicPath = /^\/blog(?:\/|$)/.test(window.location.pathname);

  useEffect(() => {
    if (publicPath) { setChecking(false); return; }
    request<{ user: User }>("/api/auth/me").then(result => setUser(result.user)).catch(error => {
      if (!(error instanceof RequestError) || error.status !== 401) setSessionMessage("The studio could not check your session. Please try signing in again.");
    }).finally(() => setChecking(false));
  }, [publicPath]);

  if (publicPath) return <PublicReader />;
  if (checking) return <div className="session-loading"><span>STOVA</span><Loader2 size={22} className="journal-spin" /><p>Opening your publishing studio…</p></div>;
  if (!user) return <AuthPortal message={sessionMessage} onAuthenticated={next => { setUser(next); setSessionMessage(""); }} />;
  return <Workspace user={user} onSessionExpired={() => { setUser(null); setSessionMessage("Your session has ended. Sign in to return to your publications."); }} onLogout={async () => { await request("/api/auth/logout", { method: "POST", body: "{}" }); setUser(null); setSessionMessage(""); }} />;
}
