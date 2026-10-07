import type { ActionFunctionArgs, LoaderFunctionArgs } from "react-router";

async function proxyRequest(request: Request) {
  const backendUrl = process.env.BACKEND_URL || "http://localhost:8000";
  const url = new URL(request.url);
  const targetUrl = `${backendUrl}${url.pathname}${url.search}`;

  const headers = new Headers(request.headers);
  headers.delete("host");

  const init: RequestInit = {
    method: request.method,
    headers,
    // @ts-expect-error duplex is required in Node.js fetch with body stream
    duplex: "half",
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  const response = await fetch(targetUrl, init);

  const responseHeaders = new Headers(response.headers);
  const contentType = responseHeaders.get("content-type") || "";
  if (contentType.includes("text/event-stream")) {
    responseHeaders.set("Cache-Control", "no-cache, no-transform");
    responseHeaders.set("Content-Encoding", "identity");
    responseHeaders.set("x-no-compression", "1");
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
}

export async function loader({ request }: LoaderFunctionArgs) {
  return proxyRequest(request);
}

export async function action({ request }: ActionFunctionArgs) {
  return proxyRequest(request);
}
