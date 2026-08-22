import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const forwardedResponseHeaders = [
  "accept-ranges",
  "content-disposition",
  "content-length",
  "content-range",
  "content-type",
  "etag",
  "last-modified",
];

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  if (path[0]?.toLowerCase() === "admin") {
    return Response.json(
      {
        detail: {
          code: "admin_proxy_disabled",
          message:
            "API quản trị bị tắt trên proxy phát triển. Hãy dùng dashboard nhúng qua FastAPI và SSH tunnel localhost.",
          retryable: false,
        },
      },
      { status: 403 },
    );
  }
  const backend = process.env.DUB_API_URL || "http://127.0.0.1:8080";
  const base = backend.endsWith("/") ? backend : `${backend}/`;
  const destination = new URL(`v1/${path.map(encodeURIComponent).join("/")}`, base);
  destination.search = request.nextUrl.search;

  const requestHeaders = new Headers();
  const contentType = request.headers.get("content-type");
  const contentLength = request.headers.get("content-length");
  const range = request.headers.get("range");
  if (contentType) requestHeaders.set("content-type", contentType);
  if (contentLength && /^(0|[1-9]\d*)$/.test(contentLength)) {
    requestHeaders.set("content-length", contentLength);
  }
  if (range) requestHeaders.set("range", range);
  const backendToken = process.env.DUB_API_TOKEN;
  if (backendToken) requestHeaders.set("authorization", `Bearer ${backendToken}`);

  const hasBody = !["GET", "HEAD"].includes(request.method);
  try {
    const body = hasBody ? request.body : null;
    const requestInit: RequestInit & { duplex?: "half" } = {
      method: request.method,
      headers: requestHeaders,
      body,
      cache: "no-store",
      redirect: "manual",
    };
    if (body) requestInit.duplex = "half";
    const upstream = await fetch(destination, requestInit);
    const responseHeaders = new Headers();
    for (const name of forwardedResponseHeaders) {
      const value = upstream.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    return new Response(upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return Response.json(
      {
        detail: {
          code: "backend_unreachable",
          message: "Không kết nối được API GPU. Hãy kiểm tra DUB_API_URL và trạng thái stack.",
          retryable: true,
        },
      },
      { status: 503 },
    );
  }
}

export const GET = proxy;
export const HEAD = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
