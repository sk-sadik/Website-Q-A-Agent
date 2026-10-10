import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SiteAsk — Ask questions about any website",
  description: "Crawl any site, then chat with its real content. Grounded answers with citations.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `try{if(localStorage.getItem('siteask-theme')==='dark'||(!localStorage.getItem('siteask-theme')&&matchMedia('(prefers-color-scheme: dark)').matches)){document.documentElement.classList.add('dark')}}catch(e){}`,
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
