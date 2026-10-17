"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { ApiError, askStream, fetchStatus, ingestSite, isValidUrlInput } from "@/lib/api";
import type { ChatMessage, SiteStatus, Source, StatusResponse } from "@/lib/types";
import { errorHeadline } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button, Card, Input } from "@/components/ui";

const EXAMPLES = ["https://example.com", "https://www.python.org", "https://nextjs.org"];
const STARTERS = [
  "What is this website about?",
  "What are the main features or pricing?",
  "How do I get started?",
  "What are the contact details?",
];
const NOT_FOUND = "I couldn't find this on the website";

type Phase = "empty" | "loading-site" | "crawling" | "ready" | "error";

interface SiteState {
  url: string;
  siteId: string | null;
  status: StatusResponse | null;
  phase: Phase;
  error: string | null;
  errorCode: string | null;
}

function themeInit(): "light" | "dark" {
  if (typeof window === "undefined") return "light";
  const saved = localStorage.getItem("siteask-theme");
  if (saved === "dark" || saved === "light") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function Home() {
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [mounted, setMounted] = useState(false);
  const [urlInput, setUrlInput] = useState("");
  const [urlError, setUrlError] = useState<string | null>(null);
  const [site, setSite] = useState<SiteState>({ url: "", siteId: null, status: null,
phase: "empty", error: null, errorCode: null });
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [askStatus, setAskStatus] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setTheme(themeInit());
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!mounted) return;
    document.documentElement.classList.toggle("dark", theme === "dark");
    localStorage.setItem("siteask-theme", theme);
  }, [theme, mounted]);

  // restore conversation per site
  useEffect(() => {
    if (!site.siteId) return;
    try {
      const raw = localStorage.getItem(`siteask-chat-${site.siteId}`);
      setMessages(raw ? JSON.parse(raw) : []);
    } catch {
      setMessages([]);
    }
  }, [site.siteId]);

  useEffect(() => {
    if (!site.siteId) return;
    try {
      localStorage.setItem(`siteask-chat-${site.siteId}`, JSON.stringify(messages.slice(-50)));
    } catch { /* quota */ }
  }, [messages, site.siteId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, askStatus]);

  const stopPolling = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
  };

  const pollStatus = useCallback(async (siteId: string) => {
    try {
      const s = await fetchStatus(siteId);
      setSite((prev) => ({ ...prev, status: s, phase: s.status === "ready" ? "ready" : "crawling", error: s.error ?? null, errorCode: s.error_code ?? null }));
      if (s.status === "ready" || s.status === "failed") {
        stopPolling();
        if (s.status === "failed") {
          setSite((prev) => ({ ...prev, phase: "error", error: s.error || "Crawling failed.", errorCode: s.error_code ?? null }));
        }
      }
    } catch (e) {
      const msg = e instanceof ApiError ? friendlyApiError(e) : "Backend unreachable. Is it running at " + (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000") + "?";
      setSite((prev) => ({ ...prev, phase: "error", error: msg }));
      stopPolling();
    }
  }, []);

  const startIngest = async (url: string, refresh = false) => {
    if (!isValidUrlInput(url)) {
      setUrlError("Enter a valid URL, e.g. https://example.com");
      return;
    }
    setUrlError(null);
    stopPolling();
    abortRef.current?.abort();
    setAsking(false);
    setSite({ url: url.trim(), siteId: null, status: null, phase: "loading-site", error: null, errorCode: null });
    try {
      const r = await ingestSite(url.trim(), refresh);
      setSite((prev) => ({ ...prev, siteId: r.site_id }));
      if (r.status === "ready") {
        await pollStatus(r.site_id);
      } else {
        setSite((prev) => ({ ...prev, phase: "crawling" }));
        await pollStatus(r.site_id);
        pollRef.current = setInterval(() => pollStatus(r.site_id), 1500);
      }
    } catch (e) {
      const msg = e instanceof ApiError ? friendlyApiError(e) : "Backend unreachable. Start it with `docker compose up --build`.";
      setSite((prev) => ({ ...prev, phase: "error", error: msg }));
    }
  };

  const sendQuestion = async (text?: string) => {
    const q = (text ?? question).trim();
    if (!q || !site.siteId || asking) return;
    const userMsg: ChatMessage = { id: crypto.randomUUID(), role: "user", content: q };
    const asstId = crypto.randomUUID();
    setMessages((m) => [...m, userMsg, { id: asstId, role: "assistant", content: "", pending: true }]);
    setQuestion("");
    setAsking(true);
    setAskStatus("Searching site...");
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const history = [...messages, userMsg].slice(-6).map((m) => ({ role: m.role, content: m.content }));
    let acc = "";
    let sources: Source[] = [];
    try {
      await askStream(site.siteId, q, history, (ev) => {
        if (ev.type === "status" && ev.message) setAskStatus(ev.message);
        else if (ev.type === "token" && ev.text) {
          acc += ev.text;
          const snapshot = acc;
          setMessages((m) => m.map((x) => (x.id === asstId ? { ...x, content: snapshot, pending: false } : x)));
        } else if (ev.type === "sources" && ev.sources) {
          sources = ev.sources;
          setMessages((m) => m.map((x) => (x.id === asstId ? { ...x, sources } : x)));
        } else if (ev.type === "error") {
          throw new Error(ev.message || "Ask failed");
        }
      }, ctrl.signal);
      if (!acc.trim()) acc = NOT_FOUND;
      setMessages((m) => m.map((x) => (x.id === asstId ? { ...x, content: acc, pending: false, sources } : x)));
    } catch (e) {
      if ((e as Error).name === "AbortError" || ctrl.signal.aborted) {
        setMessages((m) => m.map((x) => (x.id === asstId ? { ...x, content: acc || "_Stopped._", pending: false, sources } : x)));
      } else if (e instanceof ApiError && (e.status === 409)) {
        setMessages((m) => m.map((x) => (x.id === asstId ? { ...x, content: "", pending: false, error: "Site is not ready yet — wait for indexing to finish." } : x)));
      } else if (e instanceof ApiError && e.status === 429) {
        setMessages((m) => m.map((x) => (x.id === asstId ? { ...x, content: "", pending: false, error: "Rate limited — wait a minute and try again." } : x)));
      } else {
        const msg = e instanceof ApiError ? friendlyApiError(e) : (e as Error).message || "Backend unreachable.";
        setMessages((m) => m.map((x) => (x.id === asstId ? { ...x, content: "", pending: false, error: msg } : x)));
      }
    } finally {
      setAsking(false);
      setAskStatus(null);
    }
  };

  const crawling = site.phase === "crawling" || site.phase === "loading-site";
  const st = site.status;

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b border-zinc-200 dark:border-zinc-800 bg-white/80 dark:bg-zinc-950/80 backdrop-blur">
        <div className="mx-auto max-w-4xl px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-zinc-900 text-white dark:bg-white dark:text-zinc-900 font-bold">S</span>
            <span className="font-semibold text-lg">SiteAsk</span>
          </div>
          {mounted && (
            <Button variant="ghost" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Toggle theme">
              {theme === "dark" ? "☀️ Light" : "🌙 Dark"}
            </Button>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-4xl px-4 py-8 space-y-6">
        {/* URL bar */}
        <Card className="p-5">
          <form
            onSubmit={(e) => { e.preventDefault(); startIngest(urlInput); }}
            className="flex flex-col sm:flex-row gap-2"
          >
            <Input
              placeholder="Enter any website URL, e.g. https://example.com"
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              aria-label="Website URL"
            />
            <div className="flex gap-2">
              <Button type="submit" disabled={crawling}>Analyze</Button>
              {site.siteId && site.phase === "ready" && (
                <Button type="button" variant="outline" onClick={() => startIngest(site.url, true)} disabled={crawling}>
                  Refresh site
                </Button>
              )}
            </div>
          </form>
          {urlError && <p className="mt-2 text-sm text-red-600">{urlError}</p>}
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            <span className="text-zinc-500 self-center">Try:</span>
            {EXAMPLES.map((ex) => (
              <button
                key={ex}
                onClick={() => { setUrlInput(ex); startIngest(ex); }}
                className="rounded-full border border-zinc-300 dark:border-zinc-700 px-3 py-1 hover:bg-zinc-100 dark:hover:bg-zinc-800"
              >
                {ex}
              </button>
            ))}
          </div>
        </Card>

        {/* Progress / error */}
        {crawling && (
          <Card className="p-5">
            <div className="flex items-center gap-2 text-sm font-medium">
              <span className="typing-dot inline-block h-2 w-2 rounded-full bg-zinc-500" />
              {site.phase === "loading-site" ? "Starting crawl…" : `Crawling — ${st?.status ?? "queued"}…`}
            </div>
            <div className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
              Pages: {st?.pages_crawled ?? 0} / {st?.pages_found ?? "…"} · Chunks: {st?.chunk_count ?? 0}
            </div>
            <div className="mt-3 h-2 rounded bg-zinc-200 dark:bg-zinc-800 overflow-hidden">
              <div
                className="h-full bg-zinc-900 dark:bg-zinc-100 transition-all"
                style={{ width: `${st && st.pages_found ? Math.min(100, (st.pages_crawled / Math.max(1, st.pages_found)) * 100) : 5}%` }}
              />
            </div>
          </Card>
        )}

        {site.phase === "error" && (
          <Card className="p-5 border-red-300 dark:border-red-800">
            <p className="text-sm font-medium text-red-700 dark:text-red-300">Couldn&apos;t analyze that site</p>
            <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">{errorHeadline(site.errorCode, site.error)}</p>
            <div className="mt-3">
              <Button variant="outline" onClick={() => startIngest(site.url || urlInput)}>Retry</Button>
            </div>
          </Card>
        )}

        {/* Chat */}
        {site.phase === "ready" && site.siteId && (
          <Card className="p-5">
            <p className="text-xs text-zinc-500 mb-3">Asking about <span className="font-medium text-zinc-700 dark:text-zinc-300">{site.url}</span></p>
            <div className="space-y-4 max-h-[55vh] overflow-y-auto pr-1">
              {messages.length === 0 && (
                <div>
                  <p className="text-sm text-zinc-500">No questions yet. Try one of these:</p>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {STARTERS.map((s) => (
                      <button key={s} onClick={() => sendQuestion(s)} disabled={asking}
                        className="rounded-full border border-zinc-300 dark:border-zinc-700 px-3 py-1 text-xs hover:bg-zinc-100 dark:hover:bg-zinc-800 disabled:opacity-50">
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {messages.map((m) => (
                <div key={m.id} className={cn("flex", m.role === "user" ? "justify-end" : "justify-start")}>
                  <div className={cn(
                    "max-w-[85%] rounded-xl px-4 py-2.5 text-sm",
                    m.role === "user" ? "bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900"
                      : "bg-zinc-100 dark:bg-zinc-900 border border-zinc-200 dark:border-zinc-800",
                  )}>
                    {m.role === "assistant" ? (
                      m.error ? <p className="text-red-600 dark:text-red-400">{m.error}</p>
                      : m.pending && !m.content ? (
                        <span className="flex gap-1 items-center">Thinking
                          <span className="typing-dot">●</span><span className="typing-dot" style={{ animationDelay: ".2s" }}>●</span><span className="typing-dot" style={{ animationDelay: ".4s" }}>●</span>
                        </span>
                      ) : (
                        <>
                          <div className="prose-siteask"><ReactMarkdown>{m.content}</ReactMarkdown></div>
                          {!!m.sources?.length && (
                            <div className="mt-2 flex flex-wrap gap-1.5">
                              {m.sources.map((s) => (
                                <a key={s.url} href={s.url} target="_blank" rel="noreferrer"
                                  className="rounded-full bg-white dark:bg-zinc-800 border border-zinc-300 dark:border-zinc-700 px-2.5 py-0.5 text-xs underline decoration-dotted">
                                  {hostOf(s.url)}
                                </a>
                              ))}
                            </div>
                          )}
                        </>
                      )
                    ) : (
                      <p className="whitespace-pre-wrap">{m.content}</p>
                    )}
                  </div>
                </div>
              ))}
              {asking && askStatus && <p className="text-xs text-zinc-500">{askStatus}</p>}
              <div ref={bottomRef} />
            </div>
            <div className="mt-4 flex gap-2">
              <textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendQuestion(); }
                }}
                placeholder="Ask a question about this site… (Enter to send, Shift+Enter for newline)"
                rows={2}
                className="flex-1 rounded-lg border border-zinc-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-zinc-500"
              />
              {asking ? (
                <Button variant="outline" onClick={() => abortRef.current?.abort()}>Stop</Button>
              ) : (
                <Button onClick={() => sendQuestion()} disabled={!question.trim()}>Send</Button>
              )}
            </div>
          </Card>
        )}

        {site.phase === "empty" && (
          <p className="text-center text-sm text-zinc-500">
            Enter a URL above — SiteAsk crawls the site, then answers your questions using only its real content, with citations.
          </p>
        )}
      </main>
    </div>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function friendlyApiError(e: ApiError): string {
  if (e.status === 429) return "Rate limited — wait a minute and try again.";
  if (e.status === 409) return "Site is not ready yet — wait for indexing to finish.";
  if (e.status === 400) return e.message;
  if (e.status === 404) return e.message;
  return e.message || "Something went wrong.";
}
