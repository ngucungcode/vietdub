import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") || requestHeaders.get("host") || "localhost:3000";
  const protocol = requestHeaders.get("x-forwarded-proto") || (host.startsWith("localhost") ? "http" : "https");
  const base = new URL(`${protocol}://${host}`);
  const description = "Điều khiển pipeline thuyết minh video tiếng Việt chạy cục bộ trên GPU.";
  return {
    metadataBase: base,
    title: "Lồng Tiếng GPU Studio",
    description,
    openGraph: {
      title: "Lồng Tiếng GPU Studio",
      description,
      type: "website",
      images: [{ url: new URL("/og.png", base).toString(), width: 1536, height: 1024 }],
    },
    twitter: {
      card: "summary_large_image",
      title: "Lồng Tiếng GPU Studio",
      description,
      images: [new URL("/og.png", base).toString()],
    },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="vi">
      <body>{children}</body>
    </html>
  );
}
