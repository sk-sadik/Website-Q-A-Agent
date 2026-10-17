export type SiteStatus = "queued" | "crawling" | "indexing" | "ready" | "failed";

export interface StatusResponse {
  status: SiteStatus;
  pages_crawled: number;
  pages_found: number;
  chunk_count: number;
  error?: string | null;
  error_code?: string | null; // LOGIN_REQUIRED | EMPTY_CONTENT | UNREACHABLE
}

/** Tailored headline per ingest error code; falls back to the backend message. */
export function errorHeadline(code: string | null | undefined, fallback: string | null): string {
  if (code === "LOGIN_REQUIRED")
    return "This page is a login portal with no public content. Try the organization's public website instead.";
  if (code === "EMPTY_CONTENT") return "We couldn't find readable text on this site.";
  return fallback || "Crawling failed.";
}

export interface Source {
  url: string;
  title: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: Source[];
  pending?: boolean;
  error?: string | null;
}

export interface SSEvent {
  type: "status" | "token" | "sources" | "done" | "error";
  message?: string;
  text?: string;
  sources?: Source[];
}
