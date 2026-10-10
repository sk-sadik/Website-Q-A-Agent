import type { SSEvent, StatusResponse } from "./types";

const API = () =>
  (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");

export async function ingestSite(url: string, refresh = false): Promise<{ site_id: string; status: string }> {
  const res = await fetch(`${API()}/api/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, refresh }),
  });
  if (!res.ok) {
    const detail = await safeDetail(res);
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export async function fetchStatus(siteId: string): Promise<StatusResponse> {
  const res = await fetch(`${API()}/api/status/${siteId}`);
  if (!res.ok) {
    throw new ApiError(res.status, await safeDetail(res));
  }
  return res.json();
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function safeDetail(res: Response): Promise<string> {
  try {
    const j = await res.json();
    return j.detail || `Request failed (${res.status})`;
  } catch {
    return `Request failed (${res.status})`;
  }
}

/** Consume the POST /api/ask SSE stream. Calls onEvent per event. Supports abort. */
export async function askStream(
  siteId: string,
  question: string,
  history: { role: string; content: string }[],
  onEvent: (e: SSEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API()}/api/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ site_id: siteId, question, history }),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, await safeDetail(res));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split("\n\n");
    buf = parts.pop() ?? "";
    for (const part of parts) {
      for (const line of part.split("\n")) {
        const t = line.trim();
        if (!t.startsWith("data:")) continue;
        try {
          onEvent(JSON.parse(t.slice(5).trim()) as SSEvent);
        } catch {
          /* ignore malformed line */
        }
      }
    }
  }
}

export function isValidUrlInput(v: string): boolean {
  const s = v.trim();
  if (!s) return false;
  if (/\s/.test(s)) return false;
  const withScheme = s.includes("://") ? s : `https://${s}`;
  try {
    const u = new URL(withScheme);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}
