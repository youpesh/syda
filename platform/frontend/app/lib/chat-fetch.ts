/** Keep proxy error pages out of chat messages. Successful SSE bodies pass through. */
export function createChatFetch(fetchImpl: typeof fetch = fetch): typeof fetch {
  return async (input, init) => {
    const response = await fetchImpl(input, init);
    if (response.ok) return response;

    // Do not echo an untrusted response body: an intermediary can return a full
    // HTML error page (including diagnostics) instead of the backend's JSON.
    await response.body?.cancel();
    if ([502, 503, 504, 524].includes(response.status)) {
      throw new Error(
        "The connection to Syda was interrupted. Your draft may still finish in the background. Reopen this chat to check before retrying.",
      );
    }
    if (response.status === 401) {
      throw new Error("Your session has expired. Log in again to continue.");
    }
    if (response.status === 429) {
      throw new Error("Too many chat requests. Wait a moment before trying again.");
    }
    throw new Error(`Syda could not process the chat request (HTTP ${response.status}).`);
  };
}
