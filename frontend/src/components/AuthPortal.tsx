import { useState, type FormEvent } from "react";
import { ArrowRight, BookOpen, Check, Loader2 } from "lucide-react";
import type { User } from "@/types";

export default function AuthPortal({ message, onAuthenticated }: { message: string; onAuthenticated: (user: User) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(""); setBusy(true);
    try {
      const response = await fetch(`/api/auth/${mode}`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: email.trim(), password, ...(mode === "register" && name.trim() ? { name: name.trim() } : {}) }) });
      const result = await response.json().catch(() => ({})) as { user?: User; error?: string };
      if (!response.ok || !result.user) throw new Error(result.error || "Your account could not be opened. Please try again.");
      setPassword(""); onAuthenticated(result.user);
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }
  function switchMode(next: "login" | "register") { if (busy) return; setMode(next); setError(""); setPassword(""); }

  return <div className="auth-shell"><aside className="auth-story"><a className="auth-wordmark" href="/">STOVA<span>•</span></a><div><p className="auth-eyebrow">THE INDEPENDENT PUBLISHING STUDIO</p><h1>Good ideas deserve<br />a place to grow.</h1><p>Your company’s voice. Your next collection of stories. A quiet place to bring it all together.</p><div className="auth-bookmark" aria-hidden="true"><BookOpen size={90} strokeWidth={0.8} /><span>THOUGHTFULLY WRITTEN.<br />BEAUTIFULLY YOURS.</span></div></div><footer>Your words. Your brand. Your next edition.</footer></aside><main className="auth-main"><div className="auth-form-wrap"><div className="auth-tabs" role="tablist" aria-label="Account access"><button role="tab" aria-selected={mode === "login"} className={mode === "login" ? "is-active" : ""} disabled={busy} onClick={() => switchMode("login")}>Sign in</button><button role="tab" aria-selected={mode === "register"} className={mode === "register" ? "is-active" : ""} disabled={busy} onClick={() => switchMode("register")}>Create account</button></div><p className="journal-eyebrow">YOUR PUBLISHING DESK</p><h2>{mode === "login" ? "Welcome back." : "A new chapter starts here."}</h2><p className="auth-description">{mode === "login" ? "Sign in to return to your brands, drafts and publications." : "Make a home for your ideas, with a publication for every brand."}</p>{(error || message) && <div className={`journal-alert ${error ? "is-error" : "info"}`} role={error ? "alert" : "status"}>{error || message}</div>}<form onSubmit={submit}>{mode === "register" && <div className="journal-field"><label htmlFor="account-name">Your name <span>Optional</span></label><input id="account-name" autoComplete="name" maxLength={100} value={name} disabled={busy} onChange={event => setName(event.target.value)} placeholder="How should we address you?" /></div>}<div className="journal-field"><label htmlFor="account-email">Email address</label><input id="account-email" type="email" autoComplete="email" required maxLength={254} value={email} disabled={busy} onChange={event => setEmail(event.target.value)} placeholder="you@company.com" /></div><div className="journal-field"><label htmlFor="account-password">Password</label><input id="account-password" type="password" autoComplete={mode === "register" ? "new-password" : "current-password"} required minLength={mode === "register" ? 10 : 1} maxLength={128} value={password} disabled={busy} onChange={event => setPassword(event.target.value)} placeholder={mode === "register" ? "At least 10 characters" : "Your password"} />{mode === "register" && <p className="journal-help-text">Use 10–128 characters. A longer, unique password works well.</p>}</div><button className="journal-button primary auth-submit" type="submit" disabled={busy}>{busy ? <><Loader2 size={17} className="journal-spin" />{mode === "register" ? "Creating your account…" : "Signing in…"}</> : <>{mode === "register" ? "Create your studio account" : "Open your studio"}<ArrowRight size={17} /></>}</button></form><div className="auth-session-note"><Check size={15} /><span>Your publications stay private until you choose to publish.</span></div></div><footer>STOVA · INDEPENDENT PUBLISHING</footer></main></div>;
}
