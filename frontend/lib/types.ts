export type SiteStatus = "queued" | "crawling" | "indexing" | "ready" | "failed";

export interface StatusResponse {
  status: SiteStatus;
  pages_crawled: number;
  pages_found: number;
  chunk_count: number;
  error?: string | null;
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
